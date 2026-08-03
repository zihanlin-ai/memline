"""Compile the evidence packet for a draft, and drive the external review.

The program performs the joins; the reviewer performs the judgement.  Every
citation occurrence is paired with the exact sanitized material it resolves
to before anything is sent to the external reviewer.  Missing and ambiguous
references remain explicit failures -- no model is allowed to repair them by
similarity.

Grading what comes back lives in ``wiki_review_report``: the code that obtains
a report must not be able to influence how that report is judged, and the
judgement has to stay answerable with no endpoint in reach.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import Any, Callable

from memline.relay import CallResult, call_json
from memline.wiki.review_report import (
    _canonical_json,
    _content_hash,
    _sha256_text,
    merge_reviews,
    validate_merged_review_report,
    validate_review_report,
)
from memline.wiki.page import CITATION
from memline.wiki.verify import verify

SCHEMA_VERSION = 1


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



def load_prior_review(
    path: Path, article_sha256: str, review_bundle_sha256: str | None = None
) -> dict[str, Any] | None:
    """A previous merged review of this exact article and evidence packet.

    Both hashes are needed when the bundle hash is available. Claims, approved
    scope or evidence can change without changing the article text; pooling
    passes across that boundary would produce a report about no single packet.
    """
    if not path.is_file():
        return None
    try:
        prior = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(prior, dict) or not prior.get("passes"):
        return None
    if prior.get("article_sha256") != article_sha256:
        return None
    if (review_bundle_sha256 is not None
            and prior.get("review_bundle_sha256") != review_bundle_sha256):
        return None
    return prior


def run_review_passes(
    review_bundle: dict[str, Any],
    template: str,
    *,
    passes: int = 3,
    max_tokens: int = 64000,
    prior: dict[str, Any] | None = None,
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
    merged = merge_reviews(reports, prior=prior)
    # Taken from the bundle this run compiled, not from the model's echo of it:
    # the echo is checked but optional, and a pass that omitted it would leave
    # the next run unable to recognise its own article and silently start over.
    merged["article_sha256"] = review_bundle.get("article_sha256")
    merged["review_bundle_sha256"] = review_bundle.get("review_bundle_sha256")
    return merged
