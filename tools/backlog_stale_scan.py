#!/usr/bin/env python3
"""One-off backlog staleness scan over the existing store (design step 5).

Direction correctness by construction — the runtime judge only answers
"does the NEW entry displace the old one", so this script always assigns
roles by date: for each probe entry, candidates OLDER than the probe are
judged in one batched call (probe as new); candidates NEWER than the probe
are judged in per-candidate swapped calls (candidate as new, probe as the
sole existing entry). Suspicion pairs therefore always point the right way,
and every similar pair is discoverable from either side's probe.

Runs against the live daemon for search/list (the daemon holds cli.lock, so
no direct Memory client here); judging uses a dedicated LLM handle; pairs
are written straight into the shared PairStore (WAL, multi-process safe).
Resume-safe: probed ids are recorded in a progress file and judged pairs
are skipped via the version-scoped pair cache.

Usage:
    python tools/backlog_stale_scan.py --limit 100            # next batch, newest first
    python tools/backlog_stale_scan.py --limit 50 --order oldest
    python tools/backlog_stale_scan.py --status                # progress only

All opened suspicions carry session None: per disposition authority, only
interactive (user-present) sessions can confirm them.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))

PROGRESS_FILE = Path("/workspace/.agent-memory/store/backlog-scan-progress.json")
TOP_K = 10
BATCH_TAG = "backlog-scan"


def cli(*args) -> dict:
    out = subprocess.run(["mem0-local", "--json", *args], capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(f"mem0-local {' '.join(args[:2])} failed: {out.stderr[:300]}")
    return json.loads(out.stdout)["data"]


def load_progress() -> set[str]:
    try:
        return set(json.loads(PROGRESS_FILE.read_text())["probed"])
    except (FileNotFoundError, json.JSONDecodeError, KeyError):
        return set()


def save_progress(probed: set[str]) -> None:
    PROGRESS_FILE.write_text(json.dumps({"probed": sorted(probed)}))


def make_judge_llm():
    from mem0_local.cli import setup_env
    from mem0_local.config import LLM_APP_NAME, LLM_BASE_URL, LLM_MODEL, LLM_SITE_URL

    setup_env()
    from mem0.utils.factory import LlmFactory

    return LlmFactory.create(
        "openai",
        {
            "model": LLM_MODEL,
            "openrouter_base_url": LLM_BASE_URL,
            "site_url": LLM_SITE_URL,
            "app_name": LLM_APP_NAME,
            "temperature": 0.0,
            "max_tokens": 4096,
            "top_p": 0.1,
            "is_reasoning_model": False,
        },
    )


def candidate_rows(probe: dict) -> list[dict]:
    """Search neighbors of the probe (active pool only, annotated)."""
    from mem0_local.staleness import STALE_PIN, result_item_superseded

    query = str(probe.get("memory") or "")[:1500]
    result = cli("search", query, "--top-k", str(TOP_K * 2))
    rows = []
    for item in result.get("results") or []:
        meta = item.get("metadata") or {}
        if (
            not item.get("id")
            or item["id"] == probe["id"]
            or result_item_superseded(item)
            or meta.get(STALE_PIN)
        ):
            continue
        rows.append(item)
        if len(rows) >= TOP_K:
            break
    return rows


def entry(row: dict) -> dict:
    return {
        "id": row["id"],
        "text": str(row.get("memory") or ""),
        "date": str(row.get("created_at") or "")[:10],
    }


def scan_probe(llm, store, probe: dict, stats: dict) -> None:
    from mem0_local.judge import judge

    probe_date = str(probe.get("created_at") or "")
    probe_entry = entry(probe)
    rows = candidate_rows(probe)

    older = [r for r in rows if str(r.get("created_at") or "") <= probe_date]
    newer = [r for r in rows if str(r.get("created_at") or "") > probe_date]

    def record(new_e: dict, judgments: list[dict], text_by_id: dict[str, str]) -> None:
        for v in judgments:
            row = store.record_judgment(
                new_id=new_e["id"],
                old_id=v["id"],
                old_text=text_by_id.get(v["id"], ""),
                verdict=v["verdict"],
                confidence=v["confidence"],
                reason=v["reason"],
                judge_model=BATCH_TAG,
                new_session_id=None,
            )
            stats["judged"] += 1
            if row["disposition"] == "open" and row["inserted"]:
                stats["opened"] += 1

    # Probe as new vs its older neighbors (one batched call).
    unjudged_older = [
        r for r in older
        if r["id"] not in store.judged_either(
            probe["id"], probe_entry["text"], [(r["id"], str(r.get("memory") or "")) for r in older]
        )
    ] if older else []
    if unjudged_older:
        cands = [entry(r) for r in unjudged_older]
        record(
            probe_entry,
            judge(llm, probe_entry, cands),
            {c["id"]: c["text"] for c in cands},
        )
        stats["calls"] += 1

    # Newer neighbors take the "new" role, probe becomes the candidate.
    for r in newer:
        if store.judged_either(probe["id"], probe_entry["text"], [(r["id"], str(r.get("memory") or ""))]):
            stats["cached"] += 1
            continue
        record(
            entry(r),
            judge(llm, entry(r), [probe_entry]),
            {probe_entry["id"]: probe_entry["text"]},
        )
        stats["calls"] += 1
    stats["cached"] += len(older) - len(unjudged_older)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=100, help="Probes this run.")
    parser.add_argument("--order", choices=["newest", "oldest"], default="newest")
    parser.add_argument("--status", action="store_true", help="Show progress and exit.")
    args = parser.parse_args()

    from mem0_local.staleness import pair_store, result_item_superseded

    probed = load_progress()
    rows = cli("list", "--page-size", "10000")
    active = [r for r in rows if not result_item_superseded(r)]
    active.sort(key=lambda r: r.get("created_at") or "", reverse=(args.order == "newest"))
    remaining = [r for r in active if r["id"] not in probed]

    store = pair_store()
    if args.status:
        print(
            f"store: {len(rows)} entries, {len(active)} active; "
            f"probed: {len(probed)}; remaining: {len(remaining)}; "
            f"open pairs: {store.open_count()}"
        )
        return

    batch = remaining[: args.limit]
    if not batch:
        print("backlog scan complete: nothing left to probe")
        return
    print(f"probing {len(batch)} of {len(remaining)} remaining ({args.order} first)")

    llm = make_judge_llm()
    stats = {"judged": 0, "opened": 0, "cached": 0, "calls": 0, "errors": 0}
    start = time.time()
    for n, probe in enumerate(batch, 1):
        try:
            scan_probe(llm, store, probe, stats)
            probed.add(probe["id"])
        except Exception as exc:  # noqa: BLE001 - keep scanning; probe retried next run.
            stats["errors"] += 1
            print(f"  [{n}] ERROR {probe['id'][:8]}: {str(exc)[:120]}")
        if n % 10 == 0 or n == len(batch):
            save_progress(probed)
            rate = (time.time() - start) / n
            print(
                f"  [{n}/{len(batch)}] calls={stats['calls']} judged={stats['judged']} "
                f"opened={stats['opened']} cached={stats['cached']} errors={stats['errors']} "
                f"({rate:.1f}s/probe, ~{rate * (len(remaining) - n) / 60:.0f}min left overall)"
            )
    save_progress(probed)
    print(
        f"batch done: {stats['calls']} LLM calls, {stats['judged']} judgments, "
        f"{stats['opened']} suspicions opened, {stats['cached']} cache hits, "
        f"{stats['errors']} errors; open pairs now {store.open_count()}"
    )


if __name__ == "__main__":
    main()
