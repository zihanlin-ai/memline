"""Run one profiling pass over a batch plan and keep everything it produced.

What a profile *says* is a wiki question, so the prompt lives with the wiki
skill and arrives here as a template. What this module owns is the mechanical
part, and specifically the parts that are easy to get wrong once and then never
notice:

* **The material is sanitized before it leaves.** Profiling sends memory text to
  a model outside this machine; internal addresses and account ids must be
  placeholders by then, and the map back to the real values stays here.
* **Every answer is kept.** The raw model output is written per batch before
  anything is derived from it, so a later disagreement about a topic can be
  traced to what the model actually said rather than re-run and re-argued.
* **A finished batch is never re-run.** Passes are resumed constantly during
  iteration; re-profiling a batch would spend quota to overwrite an artifact
  that is already the record.
* **A refusal is recorded, not retried.** Some material is declined by the
  endpoint's moderation. That batch needs a human or a local agent, and the
  plan should say so rather than silently missing memories.

Concurrency is bounded low on purpose: the relay queues, and a queued request
is one whose first byte arrives too late to survive the path.
"""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

from mem0_local.bundle import Sanitizer, review_flags
from mem0_local.relay import RefusedError, call_json

DEFAULT_CONCURRENCY = 2


def render(template: str, batch: dict[str, Any], material: str) -> str:
    """Fill a prompt template. Unknown placeholders are left alone."""
    fields = {
        "batch_id": batch["batch_id"],
        "kind": batch["kind"],
        "span": "..".join(batch["span"]),
        "memory_count": str(batch["memory_count"]),
        "session_count": str(len(batch.get("sessions") or batch.get("session_ids") or [])),
        "session_ids": ", ".join(batch.get("session_ids") or []) or "(none)",
        "part": f"{batch.get('part', 1)}/{batch.get('part_count', 1)}",
        "material": material,
    }
    out = template
    for key, value in fields.items():
        out = out.replace("{" + key + "}", value)
    return out


def _material(batch: dict[str, Any], texts: dict[str, dict[str, Any]], sanitizer: Sanitizer) -> str:
    """The batch's memories as the model sees them: sanitized, session-labelled."""
    if batch.get("sessions"):
        groups = [{"session_id": s["session_id"], "span": "..".join(s["span"]),
                   "memories": [_one(mid, texts, sanitizer) for mid in s["memory_ids"]]}
                  for s in batch["sessions"]]
        return json.dumps(groups, ensure_ascii=False, indent=1)
    return json.dumps([_one(mid, texts, sanitizer) for mid in batch["memory_ids"]],
                      ensure_ascii=False, indent=1)


def _one(memory_id: str, texts: dict[str, dict[str, Any]], sanitizer: Sanitizer) -> dict[str, Any]:
    record = texts.get(memory_id) or {}
    return {"id": memory_id, "date": (record.get("created_at") or "")[:10],
            "text": sanitizer.scrub(record.get("memory") or "")}


def profile_batches(
    plan: list[dict[str, Any]],
    texts: dict[str, dict[str, Any]],
    template: str,
    out_dir: Path,
    *,
    kinds: tuple[str, ...] = ("session", "pack", "session-part"),
    concurrency: int = DEFAULT_CONCURRENCY,
    max_tokens: int = 32000,
    log: Callable[[str], None] = print,
) -> dict[str, Any]:
    """Profile every batch of the given kinds. Resumable; returns a summary."""
    out_dir.mkdir(parents=True, exist_ok=True)
    sanitizer = Sanitizer()
    todo = [b for b in plan if b["kind"] in kinds
            and not (out_dir / f"{b['batch_id']}.json").exists()]
    skipped = sum(1 for b in plan if b["kind"] in kinds) - len(todo)
    log(f"{len(todo)} batches to profile, {skipped} already done")

    def run(batch: dict[str, Any]) -> dict[str, Any]:
        started = time.time()
        prompt = render(template, batch, _material(batch, texts, sanitizer))
        try:
            data, result = call_json(prompt, max_tokens=max_tokens)
        except RefusedError as exc:
            log(f"{batch['batch_id']}: REFUSED by endpoint — needs local handling")
            record = {"batch_id": batch["batch_id"], "status": "refused", "detail": str(exc)}
        except Exception as exc:  # noqa: BLE001 - a failed batch must not stop the pass
            log(f"{batch['batch_id']}: FAILED {exc}")
            return {"batch_id": batch["batch_id"], "status": "failed", "detail": str(exc)}
        else:
            record = {"batch_id": batch["batch_id"], "status": "ok", "profile": data,
                      "provenance": result.provenance, "batch": {
                          k: batch[k] for k in ("kind", "span", "memory_count", "session_ids")}}
            log(f"{batch['batch_id']}: ok in {time.time()-started:.0f}s "
                f"({result.usage.get('prompt_tokens')}/{result.usage.get('completion_tokens')} tok)")
        (out_dir / f"{batch['batch_id']}.json").write_text(
            json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        return record

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        results = list(pool.map(run, todo))

    (out_dir / "_sanitization.json").write_text(json.dumps({
        "placeholder_counts": sanitizer.counts,
        "review_flags": review_flags({mid: (rec.get("memory") or "")
                                      for mid, rec in texts.items()})[:200],
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "profiled": sum(1 for r in results if r["status"] == "ok"),
        "refused": [r["batch_id"] for r in results if r["status"] == "refused"],
        "failed": [r["batch_id"] for r in results if r["status"] == "failed"],
        "skipped_existing": skipped,
        "placeholder_counts": sanitizer.counts,
        "out_dir": str(out_dir),
    }


def profile_sources(
    source_dir: Path,
    template: str,
    out_dir: Path,
    *,
    concurrency: int = DEFAULT_CONCURRENCY,
    max_tokens: int = 32000,
    log: Callable[[str], None] = print,
) -> dict[str, Any]:
    """Profile designated Markdown sources into the same shape as a memory batch.

    A source document is already curated prose, so it needs no session framing —
    but it does need the same sanitization and the same raw-artifact keeping,
    and it produces the same profile so both feed one association step.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    sanitizer = Sanitizer()
    docs = sorted(p for p in source_dir.rglob("*.md") if not p.name.startswith("."))
    todo = [p for p in docs if not (out_dir / f"{p.stem}.json").exists()]
    log(f"{len(todo)} source documents to profile, {len(docs) - len(todo)} already done")

    def run(path: Path) -> dict[str, Any]:
        rel = str(path.relative_to(source_dir))
        batch = {"batch_id": path.stem, "kind": "source", "span": ["", ""],
                 "memory_count": 0, "session_ids": [], "sessions": []}
        material = json.dumps({"path": f"sources/{rel}",
                               "text": sanitizer.scrub(path.read_text(encoding="utf-8"))},
                              ensure_ascii=False)
        try:
            data, result = call_json(render(template, batch, material), max_tokens=max_tokens)
        except RefusedError as exc:
            log(f"{rel}: REFUSED — needs local handling")
            record = {"batch_id": path.stem, "status": "refused", "detail": str(exc)}
        except Exception as exc:  # noqa: BLE001
            log(f"{rel}: FAILED {exc}")
            return {"batch_id": path.stem, "status": "failed", "detail": str(exc)}
        else:
            record = {"batch_id": path.stem, "status": "ok", "profile": data,
                      "provenance": result.provenance,
                      "batch": {"kind": "source", "source_path": f"sources/{rel}"}}
            log(f"{rel}: ok ({result.usage.get('prompt_tokens')}/"
                f"{result.usage.get('completion_tokens')} tok)")
        (out_dir / f"{path.stem}.json").write_text(
            json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        return record

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        results = list(pool.map(run, todo))
    return {
        "profiled": sum(1 for r in results if r["status"] == "ok"),
        "refused": [r["batch_id"] for r in results if r["status"] == "refused"],
        "failed": [r["batch_id"] for r in results if r["status"] == "failed"],
        "skipped_existing": len(docs) - len(todo),
        "out_dir": str(out_dir),
    }
