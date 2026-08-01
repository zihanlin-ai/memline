"""Check a draft against the bundle it was written from.

This runs before a human reads the draft, and it only asks questions a program
can answer honestly:

* Does every citation point at material that was actually in the bundle? An
  invented id is the one failure that looks exactly like diligence. A
  *shortened* id is different in kind — the material is real and the writer
  merely abbreviated it — so it is repaired and reported separately, and only
  when the abbreviation names exactly one memory.
* Does every section rest on something? A section with no citation is either
  the writer's own opinion or a paraphrase of material it declined to name.
* Did a redaction survive? A placeholder replaced by a plausible address means
  the writer filled in what it could not know.
* Which citations point at *superseded* evidence? Not an error — history has
  to be narrated — but exactly the sentences a reviewer must read first.

What it cannot answer is whether the cited evidence actually supports the
sentence, at the strength the sentence claims. That stays with the reviewer,
and the point of this pass is to leave them only that question.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

CITATION = re.compile(r"\^\[(mem:[0-9a-fA-F-]{8,}|sources/[^\]]+)\]")
CITATION_TOKEN = re.compile(r"\^\[([^\]]+)\]")
BARE_CITATION = re.compile(r"(?<!\^)\[(mem:[0-9a-fA-F-]{8,}|sources/[^\]]+)\]")
PLACEHOLDER = re.compile(r"<(?:HOST|USER|INTERNAL_HOST|INTERNAL_REPO|JOB)-\d+>")
LEAKS = (
    ("ipv4", re.compile(r"\b(?!127\.0\.0\.1\b)(?:\d{1,3}\.){3}\d{1,3}\b")),
    ("account_id", re.compile(r"\b[a-zA-Z]\d{8}\b")),
    ("internal_host", re.compile(r"\b[\w.-]+\.(?:huawei\.com|inhuawei\.com|hisilicon\.cn)\b")),
)
# Numbers worth tracing back. Small integers appear as counts, step numbers and
# prose, and checking them produces noise rather than findings.
# A decimal point only belongs to the number when a digit follows it, or a
# sentence-ending period gets read as part of the value and never matches.
TRACEABLE_NUMBER = re.compile(r"(?<![\w.-])\d[\d,]*(?:\.\d+)?(?:e-?\d+)?(?![\w.-]*\d)")


def _bundle_index(bundle: dict[str, Any]) -> tuple[set[str], set[str], str]:
    memory_refs = {f"mem:{m['id']}" for m in bundle.get("memories", [])}
    source_refs = {s["ref"] for s in bundle.get("source_sections", [])}
    corpus = "\n".join([m["text"] for m in bundle.get("memories", [])]
                       + [s["text"] for s in bundle.get("source_sections", [])])
    return memory_refs, source_refs, corpus


def resolve_prefix(ref: str, memory_refs: set[str]) -> str | None:
    """The full ref a shortened memory id unambiguously names, if any.

    Writers abbreviate uuids. An abbreviation that matches exactly one memory
    in the bundle is a formatting slip and can be repaired; one that matches
    none is a different thing entirely, and one that matches several cannot be
    repaired without guessing which was meant.
    """
    if not ref.startswith("mem:"):
        return None
    stem = ref[4:]
    matches = [r for r in memory_refs if r[4:].startswith(stem)]
    return matches[0] if len(matches) == 1 else None


def collect_sidecar_refs(value: Any, findings: list[dict[str, Any]],
                         path: str = "$") -> set[str]:
    """Collect every structured ``evidence_refs`` entry in a claims sidecar."""
    refs: set[str] = set()
    if isinstance(value, list):
        for index, item in enumerate(value):
            refs |= collect_sidecar_refs(item, findings, f"{path}[{index}]")
        return refs
    if not isinstance(value, dict):
        return refs
    for key, item in value.items():
        item_path = f"{path}.{key}"
        if key == "evidence_refs":
            if not isinstance(item, list):
                findings.append({"kind": "sidecar_evidence_refs_not_list", "detail": item_path})
                continue
            for ref in item:
                if not isinstance(ref, str) or not (
                    ref.startswith("mem:") or ref.startswith("sources/")
                ):
                    findings.append({"kind": "sidecar_ref_invalid_format", "ref": ref,
                                     "detail": item_path})
                else:
                    refs.add(ref)
        else:
            refs |= collect_sidecar_refs(item, findings, item_path)
    return refs


def verify(draft_markdown: str, bundle: dict[str, Any], claims: dict[str, Any] | None = None
           ) -> dict[str, Any]:
    memory_refs, source_refs, corpus = _bundle_index(bundle)
    known = memory_refs | source_refs
    cited = CITATION.findall(draft_markdown)

    findings: list[dict[str, Any]] = []
    for ref in dict.fromkeys(BARE_CITATION.findall(draft_markdown)):
        findings.append({"kind": "citation_missing_caret", "ref": ref})
    for token in dict.fromkeys(CITATION_TOKEN.findall(draft_markdown)):
        if CITATION.fullmatch(f"^[{token}]") is None:
            findings.append({"kind": "citation_invalid_format", "ref": token})
    abbreviated: dict[str, str] = {}
    for ref in dict.fromkeys(cited):
        if ref in known:
            continue
        full = resolve_prefix(ref, memory_refs)
        if full:
            abbreviated[ref] = full
            findings.append({"kind": "citation_abbreviated", "ref": ref, "detail": full})
        else:
            findings.append({"kind": "citation_not_in_bundle", "ref": ref})

    sections = re.split(r"\n(?=#{1,6} )", draft_markdown)
    for section in sections:
        lines = section.splitlines()
        heading = lines[0] if section.strip() else ""
        # A hierarchy-only heading has no claim to support. Requiring a
        # citation on it encourages meaningless footnotes; only sections with
        # actual body text are evidence-bearing.
        body = "\n".join(lines[1:]).strip()
        if heading.startswith("#") and body and not CITATION.search(body):
            findings.append({"kind": "section_without_citation", "detail": heading[:80]})

    for kind, pattern in LEAKS:
        for value in dict.fromkeys(pattern.findall(draft_markdown)):
            findings.append({"kind": f"redaction_lost_{kind}", "detail": value})

    # Citations are stripped before numbers are read: a uuid is full of digit
    # runs, and checking them buries the real findings in hex fragments.
    prose = CITATION.sub(" ", draft_markdown)
    # A number the article states that appears nowhere in its material is
    # either a typo or an invention; both need a human before publication.
    unsupported = [n for n in dict.fromkeys(TRACEABLE_NUMBER.findall(prose))
                   if len(n.replace(",", "").replace(".", "")) >= 3
                   and n not in corpus and n.replace(",", "") not in corpus]
    for number in unsupported[:40]:
        findings.append({"kind": "number_not_in_material", "detail": number})

    claim_items = (claims or {}).get("claims") or []
    if not isinstance(claim_items, list):
        findings.append({"kind": "claims_not_list"})
    claim_refs = collect_sidecar_refs(claims or {}, findings)
    resolved_claim_refs = {resolve_prefix(r, memory_refs) or r for r in claim_refs}
    for ref in sorted(claim_refs):
        if ref not in known and resolve_prefix(ref, memory_refs) is None:
            findings.append({"kind": "claim_ref_not_in_bundle", "ref": ref})

    unused_raw = (claims or {}).get("unused_evidence_refs") or []
    if not isinstance(unused_raw, list):
        findings.append({"kind": "unused_refs_not_list"})
        unused_raw = []
    unused_refs = {r for r in unused_raw if isinstance(r, str)}
    if any(not isinstance(r, str) for r in unused_raw):
        findings.append({"kind": "unused_ref_not_string"})
    resolved_unused = {resolve_prefix(r, memory_refs) or r for r in unused_refs}
    for ref in sorted(unused_refs):
        if ref not in known and resolve_prefix(ref, memory_refs) is None:
            findings.append({"kind": "unused_ref_not_in_bundle", "ref": ref})

    superseded = {f"mem:{m['id']}" for m in bundle.get("memories", []) if m.get("superseded")}
    resolved = {abbreviated.get(c, c) for c in cited}
    both = sorted((resolved & known) & (resolved_unused & known))
    if both:
        findings.append({"kind": "evidence_both_cited_and_unused", "refs": both,
                         "count": len(both)})
    unaccounted = sorted(known - resolved - resolved_unused)
    if claims is not None and unaccounted:
        findings.append({"kind": "evidence_neither_cited_nor_unused", "refs": unaccounted,
                         "count": len(unaccounted)})
    return {
        "citations": len(cited),
        "distinct_citations": len(set(cited)),
        "abbreviated_citations": abbreviated,
        "bundle_refs": len(known),
        "coverage": round(len(resolved & known) / max(1, len(known)), 3),
        "cited_superseded": sorted(resolved & superseded),
        "uncited_material": len(known - resolved),
        "claim_refs": len(resolved_claim_refs & known),
        "unused_refs": len(resolved_unused & known),
        "findings": findings,
        "clean": not findings,
    }
