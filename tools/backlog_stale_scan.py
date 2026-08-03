#!/usr/bin/env python3
"""One-off backlog staleness scan over the existing store (design step 5).

Scan design (user-specified, 2026-07-18): probe entries OLDEST -> NEWEST,
and for each probe restrict the candidate top-k to entries strictly OLDER
than the probe (server-side `created_at lt` filter through the daemon's
search op). Consequences:

- The probe is always the newest side of every comparison, so the runtime
  judge's single-direction question ("does the new entry displace the old
  one") is always correctly oriented — no role swapping, exactly one
  batched LLM call per probe.
- Newer entries can never crowd older ones out of the top-k, so the
  candidate pool is exactly the history the probe could displace.
- Every (newer, older) pair gets exactly one examination, from the newer
  side, when the newer entry is probed.

Runs against the live daemon for list/search (the daemon holds cli.lock, so
no direct Memory client here); judging uses a dedicated 4096-token LLM
handle; pairs are written straight into the shared PairStore (WAL,
multi-process safe). Resume-safe: probed ids live in a progress file and
judged pairs are skipped via the version-scoped both-orientation cache.

Usage:
    python tools/backlog_stale_scan.py --limit 200     # next batch
    python tools/backlog_stale_scan.py --status        # progress only

All opened suspicions carry session None: per disposition authority, only
interactive (user-present) sessions can confirm them.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import subprocess
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))

from memline.config import STORE_DIR  # noqa: E402  (sys.path set above)

PROGRESS_FILE = STORE_DIR / "backlog-scan-progress.json"
TOP_K = 10
BATCH_TAG = "backlog-scan"


def cli(*args) -> dict:
    out = subprocess.run(["memline", "--json", *args], capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(f"memline {' '.join(args[:2])} failed: {out.stderr[:300]}")
    return json.loads(out.stdout)["data"]


def daemon_search(query: str, older_than: str, top_k: int) -> list[dict]:
    """Search via the daemon op so arbitrary filters (created_at lt) pass through."""
    from memline.daemon import request

    result = request(
        {
            "op": "search",
            "args": {
                "query": query,
                "top_k": top_k,
                "filters": {"user_id": "workspace", "created_at": {"lt": older_than}},
                "threshold": 0.1,
                "rerank": False,
                "keyword": False,
                "explain": False,
                "include_superseded": False,
            },
        },
        timeout=60.0,
    )
    return (result or {}).get("results") or []


def load_progress() -> set[str]:
    try:
        return set(json.loads(PROGRESS_FILE.read_text())["probed"])
    except (FileNotFoundError, json.JSONDecodeError, KeyError):
        return set()


def save_progress(probed: set[str]) -> None:
    PROGRESS_FILE.write_text(json.dumps({"probed": sorted(probed)}))


def make_judge_llm():
    from memline.runtime import setup_env
    from memline.config import LLM_APP_NAME, LLM_BASE_URL, LLM_MODEL, LLM_SITE_URL

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


def entry(row: dict) -> dict:
    return {
        "id": row["id"],
        "text": str(row.get("memory") or ""),
        "date": str(row.get("created_at") or "")[:10],
    }


def scan_probe(llm, store, probe: dict, stats: dict) -> None:
    from memline.judge import judge
    from memline.staleness import STALE_PIN

    probe_created = str(probe.get("created_at") or "")
    if not probe_created:
        stats["skipped"] += 1
        return
    probe_entry = entry(probe)

    rows = daemon_search(probe_entry["text"][:1500], probe_created, TOP_K * 2)
    candidates = []
    for item in rows:
        meta = item.get("metadata") or {}
        if not item.get("id") or item["id"] == probe["id"] or meta.get(STALE_PIN):
            continue
        candidates.append(item)
        if len(candidates) >= TOP_K:
            break
    if not candidates:
        return

    already = store.judged_either(
        probe["id"],
        probe_entry["text"],
        [(r["id"], str(r.get("memory") or "")) for r in candidates],
    )
    candidates = [r for r in candidates if r["id"] not in already]
    stats["cached"] += len(already)
    if not candidates:
        return

    cands = [entry(r) for r in candidates]
    judgments = judge(llm, probe_entry, cands)
    stats["calls"] += 1
    text_by_id = {c["id"]: c["text"] for c in cands}
    for v in judgments:
        row = store.record_judgment(
            new_id=probe["id"],
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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=200, help="Probes this run.")
    parser.add_argument("--workers", type=int, default=1, help="Concurrent judge calls (default serial).")
    parser.add_argument("--status", action="store_true", help="Show progress and exit.")
    args = parser.parse_args()

    from memline.staleness import pair_store, result_item_superseded

    probed = load_progress()
    rows = cli("list", "--page-size", "10000")
    active = [r for r in rows if not result_item_superseded(r)]
    active.sort(key=lambda r: r.get("created_at") or "")  # oldest first
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
    print(f"probing {len(batch)} of {len(remaining)} remaining (oldest first, older-only candidates)")

    llm = make_judge_llm()
    stats = {"judged": 0, "opened": 0, "cached": 0, "calls": 0, "errors": 0, "skipped": 0}
    lock = threading.Lock()
    start = time.time()
    done_count = 0

    def worker(probe: dict) -> None:
        nonlocal done_count
        local = {"judged": 0, "opened": 0, "cached": 0, "calls": 0, "errors": 0, "skipped": 0}
        error = None
        try:
            scan_probe(llm, store, probe, local)
        except Exception as exc:  # noqa: BLE001 - keep scanning; probe retried next run.
            local["errors"] += 1
            error = str(exc)[:120]
        with lock:
            for key, value in local.items():
                stats[key] += value
            if error is None:
                probed.add(probe["id"])
            done_count += 1
            n = done_count
            if error:
                print(f"  [{n}] ERROR {probe['id'][:8]}: {error}")
            if n % 20 == 0 or n == len(batch):
                save_progress(probed)
                rate = (time.time() - start) / n
                print(
                    f"  [{n}/{len(batch)}] calls={stats['calls']} judged={stats['judged']} "
                    f"opened={stats['opened']} cached={stats['cached']} errors={stats['errors']} "
                    f"({rate:.1f}s/probe, ~{rate * (len(remaining) - n) / 3600:.1f}h left overall)",
                    flush=True,
                )

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        list(pool.map(worker, batch))
    save_progress(probed)
    print(
        f"batch done: {stats['calls']} LLM calls, {stats['judged']} judgments, "
        f"{stats['opened']} suspicions opened, {stats['cached']} cache hits, "
        f"{stats['errors']} errors; open pairs now {store.open_count()}"
    )


if __name__ == "__main__":
    main()
