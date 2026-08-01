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
    article = review_bundle.get("article_markdown") or ""
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
        # A reviewer sees the article and the material it was written from in
        # one document, and the material is *supposed* to be full of ticket
        # numbers and people's names. The first audit run reported four of them
        # as leaks — including a name the article had written as "a colleague",
        # complete with an invented line number. A claim that a string is in
        # the article is one a program can settle, so it does.
        quotes = item.get("article_quotes")
        if item.get("kind") == "sensitivity" and not quotes:
            findings.append({"kind": "sensitivity_finding_without_quotes", "index": index})
        elif quotes is not None:
            if not isinstance(quotes, list) or not all(isinstance(q, str) for q in quotes):
                findings.append({"kind": "article_quotes_not_strings", "index": index})
            else:
                for quote in quotes:
                    if quote not in article:
                        findings.append({"kind": "article_quote_not_in_article",
                                         "index": index, "detail": quote[:80]})

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


# Strictest wins when passes disagree, in both directions a verdict can go.
_VERDICT_RANK = {"supported": 0, "unverifiable": 1, "partially_supported": 2,
                 "superseded_misused": 3, "contradicted": 4}
_OVERALL_RANK = {"pass": 0, "revise": 1, "reject": 2}


def merge_reviews(reports: list[dict[str, Any]]) -> dict[str, Any]:
    """Fold several independent passes over one draft into a union.

    One pass is not a measurement. Five audits of one unchanged article flagged
    17, 1, 1, 5 and 19 claims — and the last two ran on the *same* prompt as
    each other. The union across three of them is 21, which no single pass
    reached. Passes also surface claims no earlier pass did, so this is not one
    thorough reading plus four lazy ones; each reading stops somewhere
    different.

    So the merge takes the union, not the majority. A claim flagged once is
    flagged, and ``flagged_in`` records how many passes saw it — the number a
    reviewer should read as strength of signal, since a finding every pass
    agrees on is a different thing from one that surfaced once. Voting would
    discard precisely the findings this exists to buy: the ones easy to miss.
    With this much spread, a majority rule over three passes would have thrown
    away most of what was found.

    The reverse never applies. A pass cannot clear a claim another flagged,
    because "I did not notice this" and "this is fine" are not the same
    statement, and nothing in the report distinguishes them.

    What the spread does *not* license is reading a low ``flagged_in`` as a
    weak finding. The one claim flagged by every pass was also the only one any
    pass marked high-confidence; everything else, including a genuine causal
    error confirmed by hand against the evidence, moved in and out of view
    between runs.
    """
    if not reports:
        raise ValueError("no review passes to merge")
    claims: dict[str, dict[str, Any]] = {}
    for index, report in enumerate(reports):
        for item in report.get("claim_reviews") or []:
            if not isinstance(item, dict) or not item.get("claim_id"):
                continue
            claim_id = item["claim_id"]
            entry = claims.setdefault(claim_id, {
                "claim_id": claim_id, "verdict": "supported", "flagged_in": 0,
                "of_passes": len(reports), "findings": [],
            })
            verdict = item.get("verdict")
            if _VERDICT_RANK.get(verdict, 0) > _VERDICT_RANK.get(entry["verdict"], 0):
                entry["verdict"] = verdict
            if verdict != "supported":
                entry["flagged_in"] += 1
                entry["findings"].append({
                    "pass": index + 1, "verdict": verdict,
                    "confidence": item.get("confidence"), "reason": item.get("reason"),
                    "suggested_rewrite": item.get("suggested_rewrite"),
                    "evidence_refs": item.get("evidence_refs"),
                })

    omissions = [{**item, "pass": index + 1}
                 for index, report in enumerate(reports)
                 for item in report.get("omission_reviews") or []
                 if isinstance(item, dict)]

    overall = max((r.get("overall_verdict") for r in reports),
                  key=lambda v: _OVERALL_RANK.get(v, 0), default=None)
    validations = [r.get("validation") or {} for r in reports]
    flagged = [c for c in claims.values() if c["flagged_in"]]
    return {
        "passes": len(reports),
        "overall_verdict": overall,
        "claim_reviews": sorted(claims.values(), key=lambda c: c["claim_id"]),
        "omission_reviews": omissions,
        "flagged_claims": len(flagged),
        # How much a second opinion actually bought. All passes agreeing is a
        # reason to trust a finding; only one pass seeing it is a reason to run
        # more passes, not to discount it.
        "unanimous_claims": sum(1 for c in flagged if c["flagged_in"] == len(reports)),
        "single_pass_claims": sum(1 for c in flagged if c["flagged_in"] == 1),
        "validation": {
            "report_valid": all(v.get("report_valid") for v in validations),
            "deterministic_clean": all(v.get("deterministic_clean") for v in validations),
            "scope_clean": all(v.get("scope_clean") for v in validations),
            "claims_expected": max((v.get("claims_expected") or 0 for v in validations), default=0),
            "per_pass": [{"pass": i + 1, "report_valid": v.get("report_valid"),
                          "findings": v.get("findings") or [],
                          "claim_verdict_counts": v.get("claim_verdict_counts")}
                         for i, v in enumerate(validations)],
            "agent_review_required": True,
        },
        "review_provenance": [r.get("review_provenance") for r in reports],
    }


def run_review_passes(
    review_bundle: dict[str, Any],
    template: str,
    *,
    passes: int = 3,
    max_tokens: int = 64000,
    caller: Callable[..., tuple[Any, CallResult]] = call_json,
    log: Callable[[str], None] = lambda _: None,
) -> dict[str, Any]:
    """Several independent passes over one draft, merged into their union."""
    reports = []
    for index in range(max(1, passes)):
        report = run_external_review(review_bundle, template,
                                     max_tokens=max_tokens, caller=caller)
        counts = (report.get("validation") or {}).get("claim_verdict_counts") or {}
        flagged = sum(n for v, n in counts.items() if v != "supported")
        log(f"pass {index + 1}/{passes}: {flagged} flagged, "
            f"verdict {report.get('overall_verdict')}")
        reports.append(report)
    return merge_reviews(reports)
