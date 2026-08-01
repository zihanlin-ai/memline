"""Compile and validate an evidence-grounded review of a wiki draft.

The program performs the joins; the reviewer performs the judgement.  Every
citation occurrence is paired with the exact sanitized material it resolves
to before anything is sent to the external reviewer.  Missing and ambiguous
references remain explicit failures -- no model is allowed to repair them by
similarity.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any, Callable

from mem0_local.relay import CallResult, call_json
from mem0_local.wiki_verify import CITATION, verify

SCHEMA_VERSION = 1
CLAIM_VERDICTS = {
    "supported", "partially_supported", "contradicted", "unverifiable",
    "superseded_misused",
}
SCOPE_VERDICTS = {"in_scope", "minor_drift", "major_drift", "unverifiable"}
OVERALL_VERDICTS = {"pass", "revise", "reject"}
CONFIDENCE = {"high", "medium", "low"}
SEVERITIES = {"info", "warning", "error", "critical"}


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _content_hash(value: dict[str, Any], field: str) -> str:
    copy = dict(value)
    copy.pop(field, None)
    return _sha256_text(_canonical_json(copy))


def load_approved_topic(path: Path | None, topic_key: str | None) -> dict[str, Any] | None:
    """Return the accepted topic matching ``topic_key``, if the ledger is available."""
    if not path or not path.is_file() or not topic_key:
        return None
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        item = json.loads(line)
        if topic_key in (item.get("topic_key"), item.get("id")):
            return item
    return None


def _evidence_index(bundle: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], list[str]]:
    index: dict[str, dict[str, Any]] = {}
    memory_refs: list[str] = []
    for memory in bundle.get("memories") or []:
        ref = f"mem:{memory['id']}"
        memory_refs.append(ref)
        index[ref] = {
            "ref": ref,
            "kind": "memory",
            "text": memory.get("text") or "",
            "sha256": memory.get("sha256"),
            "created_at": memory.get("created_at"),
            "writer": memory.get("writer"),
            "superseded": memory.get("superseded"),
        }
    for source in bundle.get("source_sections") or []:
        ref = source["ref"]
        index[ref] = {
            "ref": ref,
            "kind": "source_section",
            "text": source.get("text") or "",
            "sha256": source.get("sha256"),
            "document": source.get("document"),
            "heading": source.get("heading"),
            "superseded": False,
        }
    return index, memory_refs


def resolve_reference(ref: str, index: dict[str, dict[str, Any]],
                      memory_refs: list[str]) -> dict[str, Any]:
    """Resolve one exact or abbreviated ref without guessing."""
    if ref in index:
        return {"requested_ref": ref, "resolved_ref": ref, "status": "exact",
                "evidence": index[ref]}
    if ref.startswith("mem:"):
        stem = ref[4:]
        matches = [candidate for candidate in memory_refs if candidate[4:].startswith(stem)]
        if len(matches) == 1:
            resolved = matches[0]
            return {"requested_ref": ref, "resolved_ref": resolved,
                    "status": "abbreviated", "evidence": index[resolved]}
        if len(matches) > 1:
            return {"requested_ref": ref, "resolved_ref": None, "status": "ambiguous",
                    "candidates": sorted(matches), "evidence": None}
    return {"requested_ref": ref, "resolved_ref": None, "status": "missing",
            "evidence": None}


def _blocks(markdown: str) -> list[tuple[int, str]]:
    """Markdown blocks with their byte offsets, preserving exact review locations."""
    blocks: list[tuple[int, str]] = []
    for match in re.finditer(r"(?ms)(?:\A|\n[ \t]*\n)(.*?)(?=\n[ \t]*\n|\Z)", markdown):
        text = match.group(1)
        if text.strip():
            blocks.append((match.start(1), text))
    return blocks


def _citation_groups(block: str) -> list[list[re.Match[str]]]:
    matches = list(CITATION.finditer(block))
    groups: list[list[re.Match[str]]] = []
    for match in matches:
        if groups and not block[groups[-1][-1].end():match.start()].strip():
            groups[-1].append(match)
        else:
            groups.append([match])
    return groups


def _plain_passage(text: str) -> bool:
    stripped = text.strip()
    if not stripped or stripped.startswith("```"):
        return False
    lines = [line.strip() for line in stripped.splitlines() if line.strip()]
    return any(not line.startswith("#") for line in lines)


def build_review_bundle(
    draft_markdown: str,
    bundle: dict[str, Any],
    claims: dict[str, Any] | None = None,
    approved_topic: dict[str, Any] | None = None,
    *,
    draft_name: str | None = None,
) -> dict[str, Any]:
    """Compile the draft into claim-local evidence packets for an independent reviewer."""
    index, memory_refs = _evidence_index(bundle)
    deterministic = verify(draft_markdown, bundle, claims)
    packets: list[dict[str, Any]] = []
    uncited: list[dict[str, Any]] = []
    cited_resolved: set[str] = set()

    for block_offset, block in _blocks(draft_markdown):
        groups = _citation_groups(block)
        if not groups:
            if _plain_passage(block):
                uncited.append({
                    "passage_id": f"uncited-{len(uncited) + 1:04d}",
                    "line": draft_markdown.count("\n", 0, block_offset) + 1,
                    "text": block.strip(),
                })
            continue
        cursor = 0
        for group in groups:
            claim_text = block[cursor:group[0].start()].strip()
            citations = [resolve_reference(match.group(1), index, memory_refs) for match in group]
            cited_resolved.update(c["resolved_ref"] for c in citations if c["resolved_ref"])
            absolute = block_offset + group[0].start()
            packets.append({
                "claim_id": f"claim-{len(packets) + 1:04d}",
                "line": draft_markdown.count("\n", 0, absolute) + 1,
                "text": claim_text,
                "citations": citations,
            })
            cursor = group[-1].end()
        tail = block[cursor:].strip()
        if _plain_passage(tail):
            tail_offset = block_offset + cursor
            uncited.append({
                "passage_id": f"uncited-{len(uncited) + 1:04d}",
                "line": draft_markdown.count("\n", 0, tail_offset) + 1,
                "text": tail,
            })

    uncited_evidence = [item for ref, item in sorted(index.items()) if ref not in cited_resolved]
    review_bundle: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "draft_name": draft_name,
        "article_sha256": _sha256_text(draft_markdown),
        "evidence_bundle_sha256": _sha256_text(_canonical_json(bundle)),
        "approved_topic": approved_topic,
        "article_markdown": draft_markdown,
        "deterministic_report": deterministic,
        "claim_packets": packets,
        "uncited_passages": uncited,
        "uncited_evidence": uncited_evidence,
        "claims_manifest": claims,
    }
    review_bundle["review_bundle_sha256"] = _content_hash(review_bundle, "review_bundle_sha256")
    return review_bundle


def validate_review_report(report: dict[str, Any], review_bundle: dict[str, Any]) -> dict[str, Any]:
    """Validate reviewer coverage, hashes, enum values and evidence joins."""
    findings: list[dict[str, Any]] = []
    expected_hash = _content_hash(review_bundle, "review_bundle_sha256")
    if review_bundle.get("review_bundle_sha256") != expected_hash:
        findings.append({"kind": "review_bundle_hash_invalid"})
    for field in ("article_sha256", "review_bundle_sha256"):
        if report.get(field) != review_bundle.get(field):
            findings.append({"kind": "review_hash_mismatch", "field": field})
    if not isinstance(report.get("summary"), str) or not report.get("summary", "").strip():
        findings.append({"kind": "review_summary_missing"})

    expected = {packet["claim_id"]: packet for packet in review_bundle.get("claim_packets") or []}
    reviews = report.get("claim_reviews")
    if not isinstance(reviews, list):
        findings.append({"kind": "claim_reviews_missing"})
        reviews = []
    seen: set[str] = set()
    for item in reviews:
        if not isinstance(item, dict):
            findings.append({"kind": "claim_review_not_object"})
            continue
        claim_id = item.get("claim_id")
        if claim_id in seen:
            findings.append({"kind": "claim_review_duplicate", "claim_id": claim_id})
        seen.add(claim_id)
        packet = expected.get(claim_id)
        if packet is None:
            findings.append({"kind": "claim_review_unknown", "claim_id": claim_id})
            continue
        if item.get("verdict") not in CLAIM_VERDICTS:
            findings.append({"kind": "claim_review_bad_verdict", "claim_id": claim_id})
        if item.get("confidence") not in CONFIDENCE:
            findings.append({"kind": "claim_review_bad_confidence", "claim_id": claim_id})
        if not isinstance(item.get("reason"), str) or not item.get("reason", "").strip():
            findings.append({"kind": "claim_review_reason_missing", "claim_id": claim_id})
        if not isinstance(item.get("suggested_rewrite"), str):
            findings.append({"kind": "claim_review_rewrite_invalid", "claim_id": claim_id})
        allowed = {c["resolved_ref"] for c in packet["citations"] if c["resolved_ref"]}
        used = item.get("evidence_refs") or []
        if not isinstance(used, list) or any(ref not in allowed for ref in used):
            findings.append({"kind": "claim_review_evidence_outside_packet", "claim_id": claim_id})
        elif item.get("verdict") == "supported" and not used:
            findings.append({"kind": "supported_claim_without_evidence", "claim_id": claim_id})
    missing = sorted(set(expected) - seen)
    if missing:
        findings.append({"kind": "claim_reviews_incomplete", "claim_ids": missing,
                         "count": len(missing)})

    scope = report.get("scope_review")
    if not isinstance(scope, dict) or scope.get("verdict") not in SCOPE_VERDICTS:
        findings.append({"kind": "scope_review_invalid"})
    if report.get("overall_verdict") not in OVERALL_VERDICTS:
        findings.append({"kind": "overall_verdict_invalid"})

    known_refs = {item["ref"] for item in review_bundle.get("uncited_evidence") or []}
    for packet in review_bundle.get("claim_packets") or []:
        known_refs.update(c["resolved_ref"] for c in packet["citations"] if c["resolved_ref"])
    omissions = report.get("omission_reviews")
    if not isinstance(omissions, list):
        findings.append({"kind": "omission_reviews_missing"})
        omissions = []
    for index, item in enumerate(omissions):
        if not isinstance(item, dict):
            findings.append({"kind": "omission_review_not_object", "index": index})
            continue
        if item.get("severity") not in SEVERITIES:
            findings.append({"kind": "omission_review_bad_severity", "index": index})
        if not isinstance(item.get("finding"), str) or not item.get("finding", "").strip():
            findings.append({"kind": "omission_review_finding_missing", "index": index})
        refs = item.get("evidence_refs") or []
        if not isinstance(refs, list) or any(ref not in known_refs for ref in refs):
            findings.append({"kind": "omission_review_unknown_evidence", "index": index})

    verdicts = [item.get("verdict") for item in reviews if isinstance(item, dict)]
    verdict_counts = dict(sorted(Counter(
        verdict if isinstance(verdict, str) else "<invalid>" for verdict in verdicts
    ).items()))
    severity_counts = dict(sorted(Counter(
        item.get("severity") if isinstance(item.get("severity"), str) else "<invalid>"
        for item in omissions if isinstance(item, dict)
    ).items()))
    semantic_clean = len(verdicts) == len(expected) and all(v == "supported" for v in verdicts)
    omission_clean = all(item.get("severity") == "info"
                         for item in omissions if isinstance(item, dict))
    scope_clean = isinstance(scope, dict) and scope.get("verdict") == "in_scope"
    report_valid = not findings
    return {
        "findings": findings,
        "report_valid": report_valid,
        "deterministic_clean": bool(
            (review_bundle.get("deterministic_report") or {}).get("clean")),
        "semantic_clean": semantic_clean,
        "omission_clean": omission_clean,
        "scope_clean": scope_clean,
        "ready_for_agent_review": report_valid,
        "claims_expected": len(expected),
        "claims_reviewed": len(seen & set(expected)),
        "claim_verdict_counts": verdict_counts,
        "omission_severity_counts": severity_counts,
        "clean": report_valid
        and bool((review_bundle.get("deterministic_report") or {}).get("clean"))
        and semantic_clean and omission_clean and scope_clean
        and report.get("overall_verdict") == "pass",
        "agent_review_required": True,
    }


def render_review_prompt(template: str, review_bundle: dict[str, Any]) -> str:
    return template.replace(
        "{review_bundle}", json.dumps(review_bundle, ensure_ascii=False, indent=1))


def run_external_review(
    review_bundle: dict[str, Any],
    template: str,
    *,
    max_tokens: int = 64000,
    caller: Callable[..., tuple[Any, CallResult]] = call_json,
) -> dict[str, Any]:
    """Ask a fresh wiki endpoint call to review, then validate its report locally."""
    data, result = caller(render_review_prompt(template, review_bundle), job="review",
                          max_tokens=max_tokens)
    if not isinstance(data, dict):
        data = {"invalid_model_output": data}
    data["review_provenance"] = result.provenance
    data["validation"] = validate_review_report(data, review_bundle)
    return data
