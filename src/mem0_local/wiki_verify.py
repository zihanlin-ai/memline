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


def verify(draft_markdown: str, bundle: dict[str, Any], claims: dict[str, Any] | None = None
           ) -> dict[str, Any]:
    memory_refs, source_refs, corpus = _bundle_index(bundle)
    known = memory_refs | source_refs
    cited = CITATION.findall(draft_markdown)

    findings: list[dict[str, Any]] = []
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
        heading = section.splitlines()[0] if section.strip() else ""
        if heading.startswith("#") and not CITATION.search(section):
            findings.append({"kind": "section_without_citation", "detail": heading[:80]})

    for kind, pattern in LEAKS:
        for value in dict.fromkeys(pattern.findall(draft_markdown)):
            findings.append({"kind": f"redaction_lost_{kind}", "detail": value})

    # A number the article states that appears nowhere in its material is
    # either a typo or an invention; both need a human before publication.
    unsupported = [n for n in dict.fromkeys(TRACEABLE_NUMBER.findall(draft_markdown))
                   if len(n.replace(",", "").replace(".", "")) >= 3
                   and n not in corpus and n.replace(",", "") not in corpus]
    for number in unsupported[:40]:
        findings.append({"kind": "number_not_in_material", "detail": number})

    claim_refs = {r for c in (claims or {}).get("claims") or [] for r in c.get("evidence_refs") or []}
    for ref in sorted(claim_refs - known):
        if resolve_prefix(ref, memory_refs) is None:
            findings.append({"kind": "claim_ref_not_in_bundle", "ref": ref})

    superseded = {f"mem:{m['id']}" for m in bundle.get("memories", []) if m.get("superseded")}
    resolved = {abbreviated.get(c, c) for c in cited}
    return {
        "citations": len(cited),
        "distinct_citations": len(set(cited)),
        "abbreviated_citations": abbreviated,
        "bundle_refs": len(known),
        "coverage": round(len(resolved & known) / max(1, len(known)), 3),
        "cited_superseded": sorted(resolved & superseded),
        "uncited_material": len(known - resolved),
        "findings": findings,
        "clean": not findings,
    }
