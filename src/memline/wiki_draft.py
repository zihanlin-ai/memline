"""Draft one accepted topic from its evidence, and keep what it was drafted from.

Generation runs on an endpoint outside this machine, which fixes what this
module has to guarantee:

* **The bundle is the whole world the writer sees.** Everything the article may
  claim has to be in it, and nothing that must not leave may be. So the bundle
  is assembled and sanitized here rather than by the caller.
* **Nothing unreviewed leaves.** The sanitizer replaces what it can recognize
  by shape; a personal name it cannot. Those land in ``review_flags``, and a
  flag nobody has ruled on blocks the call — the first run of this pipeline
  sent two real names and a set of addresses to a third party because the
  flags were merely *reported*. A ruling is durable: values are either
  redacted from then on or recorded as false positives, so review is paid once
  rather than every run.
* **Retired evidence is surfaced before the call, not after.** A topic accepted
  weeks ago can rest on a memory that has since been superseded or deleted; the
  draft must narrate the first as history and must not silently absorb the loss
  of the second.
* **The bundle is kept beside the draft.** A disagreement about a sentence is
  settled by looking at what the writer was given, not by re-running it.

What the article should *say* is the prompt's business, and the prompt ships
with this package because the JSON shape parsed here is the shape it asks for.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from memline.bundle import build_bundle
from memline.relay import call_json

REQUIRED_FIELDS = ("title", "summary", "article_markdown", "claims", "open_questions",
                   "unused_evidence_refs")

# Kinds that name a person or reach one. A shape the sanitizer cannot judge
# and a human has not judged either must never be the thing that leaves.
BLOCKING_FLAG_KINDS = ("cjk_personal_name", "email")


class UnreviewedMaterialError(RuntimeError):
    """Sensitive-looking values nobody has ruled on. Rule on them, then retry."""


def load_review(path: Path | None) -> tuple[dict[str, str], set[str]]:
    """``(redactions, cleared)`` from a review file.

    ``redact`` maps a value to the placeholder category it becomes;
    ``cleared`` lists values a human looked at and judged harmless. Both are
    kept because "we decided this is fine" is as much a decision as "replace
    this", and losing it means re-deciding every run.
    """
    if not path or not path.is_file():
        return {}, set()
    data = json.loads(path.read_text(encoding="utf-8"))
    return dict(data.get("redact") or {}), set(data.get("cleared") or ())


def render(template: str, topic: dict[str, Any], material: str) -> str:
    fields = {
        "title": topic.get("title") or "",
        "scope": topic.get("scope") or "(no scope recorded)",
        "evidence_gaps": topic.get("evidence_gaps") or "none seen",
        "conflicts": topic.get("conflicts") or "none seen",
        "sensitive": topic.get("sensitive") or "none seen",
        "material": material,
    }
    out = template
    for key, value in fields.items():
        out = out.replace("{" + key + "}", str(value))
    return out


def material_of(bundle: dict[str, Any]) -> str:
    """The bundle as the writer sees it: memories with their provenance, then documents."""
    memories = [{"id": m["id"], "date": (m.get("created_at") or "")[:10],
                 "writer": m.get("writer"), "superseded": m.get("superseded"),
                 "text": m["text"]} for m in bundle.get("memories", [])]
    sections = [{"ref": s["ref"], "document": s.get("document"),
                 "heading": s.get("heading"), "text": s["text"]}
                for s in bundle.get("source_sections", [])]
    return json.dumps({"memories": memories, "source_sections": sections},
                      ensure_ascii=False, indent=1)


def draft_topic(
    topic: dict[str, Any],
    execute: Callable[[str, dict[str, Any]], Any],
    template: str,
    out_dir: Path,
    *,
    wiki_root: Path,
    review_file: Path | None = None,
    max_tokens: int = 128000,
    log: Callable[[str], None] = print,
) -> dict[str, Any]:
    """Bundle, draft, and write ``<slug>.md`` plus its sidecars. Returns a summary."""
    out_dir.mkdir(parents=True, exist_ok=True)
    slug = topic.get("topic_key") or topic["id"]
    refs = [e["ref"] for e in topic.get("evidence", [])]
    redactions, cleared = load_review(review_file)
    bundle, mapping = build_bundle(refs, execute, wiki_root=wiki_root,
                                   redactions=redactions, cleared=cleared)

    blocking = sorted({f["value"] for f in bundle["sanitization"]["review_flags"]
                       if f["kind"] in BLOCKING_FLAG_KINDS})
    if blocking:
        raise UnreviewedMaterialError(
            f"{slug}: {len(blocking)} value(s) nobody has ruled on. Add each to "
            f"`redact` or `cleared` in {review_file or '--review-file'} and retry: "
            + ", ".join(repr(v) for v in blocking[:12])
            + (" …" if len(blocking) > 12 else ""))

    superseded = [m["id"] for m in bundle["memories"] if m.get("superseded")]
    unresolved = bundle["unresolved"]
    log(f"{slug}: {bundle['memory_count']} memories + "
        f"{bundle['source_section_count']} source sections"
        + (f", {len(superseded)} superseded" if superseded else "")
        + (f", {len(unresolved)} unresolved" if unresolved else ""))

    (out_dir / f"{slug}.bundle.json").write_text(
        json.dumps(bundle, ensure_ascii=False, indent=1), encoding="utf-8")
    (out_dir / f"{slug}.placeholders.json").write_text(
        json.dumps(mapping, ensure_ascii=False, indent=1), encoding="utf-8")

    prompt = render(template, topic, material_of(bundle))
    data, result = call_json(prompt, job="draft", max_tokens=max_tokens)
    missing = [f for f in REQUIRED_FIELDS if f not in data]
    if missing:
        raise ValueError(f"{slug}: draft is missing {missing}")
    if not isinstance(data.get("summary"), str) or not data["summary"].strip():
        raise ValueError(f"{slug}: draft summary is empty")

    (out_dir / f"{slug}.md").write_text(
        f"# {data['title']}\n\n{data['article_markdown']}\n", encoding="utf-8")
    (out_dir / f"{slug}.claims.json").write_text(json.dumps({
        "topic": topic.get("id"), "topic_key": slug,
        "summary": data.get("summary"),
        "claims": data.get("claims"), "sections": data.get("sections"),
        "open_questions": data.get("open_questions"),
        "retraction_arcs": data.get("retraction_arcs"),
        "unused_evidence_refs": data.get("unused_evidence_refs"),
        "superseded_evidence": superseded, "unresolved_evidence": unresolved,
        "generation": result.provenance,
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"{slug}: drafted {len(data['article_markdown'])} chars in {result.seconds:.0f}s "
        f"on {result.model} ({result.usage.get('prompt_tokens')}/"
        f"{result.usage.get('completion_tokens')} tok)")
    return {
        "topic_key": slug, "chars": len(data["article_markdown"]),
        "summary": data.get("summary"),
        "claims": len(data.get("claims") or []),
        "open_questions": len(data.get("open_questions") or []),
        "retraction_arcs": len(data.get("retraction_arcs") or []),
        "unused_evidence": len(data.get("unused_evidence_refs") or []),
        "superseded_evidence": len(superseded), "unresolved_evidence": len(unresolved),
        "provenance": result.provenance,
    }
