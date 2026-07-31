"""Draft one accepted topic from its evidence, and keep what it was drafted from.

Generation runs on an endpoint outside this machine, which fixes what this
module has to guarantee:

* **The bundle is the whole world the writer sees.** Everything the article may
  claim has to be in it, and nothing that must not leave may be. So the bundle
  is assembled and sanitized here rather than by the caller.
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

from mem0_local.bundle import build_bundle
from mem0_local.relay import call_json

REQUIRED_FIELDS = ("title", "article_markdown", "claims", "open_questions",
                   "unused_evidence_refs")


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
    max_tokens: int = 64000,
    log: Callable[[str], None] = print,
) -> dict[str, Any]:
    """Bundle, draft, and write ``<slug>.md`` plus its sidecars. Returns a summary."""
    out_dir.mkdir(parents=True, exist_ok=True)
    slug = topic.get("topic_key") or topic["id"]
    refs = [e["ref"] for e in topic.get("evidence", [])]
    bundle, mapping = build_bundle(refs, execute, wiki_root=wiki_root)

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
    data, result = call_json(prompt, max_tokens=max_tokens)
    missing = [f for f in REQUIRED_FIELDS if f not in data]
    if missing:
        raise ValueError(f"{slug}: draft is missing {missing}")

    (out_dir / f"{slug}.md").write_text(
        f"# {data['title']}\n\n{data['article_markdown']}\n", encoding="utf-8")
    (out_dir / f"{slug}.claims.json").write_text(json.dumps({
        "topic": topic.get("id"), "topic_key": slug,
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
        "claims": len(data.get("claims") or []),
        "open_questions": len(data.get("open_questions") or []),
        "retraction_arcs": len(data.get("retraction_arcs") or []),
        "unused_evidence": len(data.get("unused_evidence_refs") or []),
        "superseded_evidence": len(superseded), "unresolved_evidence": len(unresolved),
        "provenance": result.provenance,
    }
