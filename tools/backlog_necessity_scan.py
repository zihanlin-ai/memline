#!/usr/bin/env python3
"""One-off backlog necessity scan (lifecycle design §6.4 pilot).

Marks only — never invalidates, TTLs, or deletes. Three phases so a human
review sits between the LLM and any store write, and a mid-flight prompt
adjustment never leaves stale marks:

    plan    snapshot the ordered target list (active, unpinned, not yet
            necessity-judged for the current text version) to a JSONL file
    judge   LLM-judge one batch from the plan -> JSONL report, NO store writes
    commit  after review, write PairStore marks from a report
            (flagged verdicts open suspicions; DURABLE rows are cached);
            --skip-ids drops reviewer-rejected rows

Usage (store venv):
    python tools/backlog_necessity_scan.py plan   --dir /workspace/tmp/necessity_backlog
    python tools/backlog_necessity_scan.py judge  --dir ... --batch 0 [--batch-size 100]
    python tools/backlog_necessity_scan.py commit --dir ... --batch 0 [--skip-ids id1,id2]
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))

SCAN_SESSION = "backlog-necessity-scan-202607"
PROMPT_TAG = "prod-v4-backlog"
LLM_WORKERS = 6


def make_llm():
    from mem0_local.runtime import setup_env
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
            "max_tokens": 600,
            "top_p": 0.1,
            "is_reasoning_model": False,
        },
    )


def plan_path(dir_: Path) -> Path:
    return dir_ / "plan.jsonl"


def report_path(dir_: Path, batch: int) -> Path:
    return dir_ / f"report-batch{batch:03d}.jsonl"


def load_jsonl(path: Path) -> list[dict]:
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


def cmd_plan(args: argparse.Namespace) -> None:
    # Listing goes through the CLI (daemon path): a direct client would
    # block forever on cli.lock while the daemon runs, and this script
    # needs no Memory client anyway — LLM calls plus SQLite marks only.
    import subprocess

    from mem0_local.staleness import (
        KIND_NECESSITY,
        STALE_PIN,
        is_ttl_expired,
        pair_store,
        result_item_superseded,
        result_item_expired,
    )

    out = subprocess.run(
        ["mem0-local", "--json", "list", "--page-size", "100000"],
        capture_output=True, text=True,
    )
    if out.returncode != 0:
        raise SystemExit(f"mem0-local list failed: {out.stderr[:300]}")
    items = json.loads(out.stdout)["data"]
    store = pair_store()

    rows, skipped = [], Counter()
    for item in items or []:
        if not isinstance(item, dict):
            continue
        mid = str(item.get("id") or "")
        meta = item.get("metadata") or {}
        text = str(item.get("memory") or "")
        if not mid or not text:
            skipped["empty"] += 1
            continue
        if result_item_superseded(item):
            skipped["superseded"] += 1
            continue
        if result_item_expired(item) or is_ttl_expired(meta):
            skipped["ttl_expired"] += 1
            continue
        if meta.get(STALE_PIN) or item.get(STALE_PIN):
            skipped["pinned"] += 1
            continue
        if store.has_judgment(mid, mid, text, kind=KIND_NECESSITY):
            skipped["already_judged"] += 1
            continue
        rows.append(
            {
                "id": mid,
                "date": str(item.get("created_at") or "")[:10],
                "ingested_at": str(meta.get("ingested_at") or item.get("created_at") or ""),
                "source": meta.get("source"),
                "origin": meta.get("origin"),
                "text": text,
            }
        )
    rows.sort(key=lambda r: r["ingested_at"])

    args.dir.mkdir(parents=True, exist_ok=True)
    plan_path(args.dir).write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n"
    )
    n_batches = (len(rows) + args.batch_size - 1) // args.batch_size
    print(json.dumps({
        "planned": len(rows),
        "batches": n_batches,
        "batch_size": args.batch_size,
        "skipped": dict(skipped),
        "plan": str(plan_path(args.dir)),
    }, ensure_ascii=False, indent=2))


def cmd_judge(args: argparse.Namespace) -> None:
    from mem0_local.judge import judge_necessity

    rows = load_jsonl(plan_path(args.dir))
    start = args.batch * args.batch_size
    batch = rows[start : start + args.batch_size]
    if not batch:
        print(json.dumps({"batch": args.batch, "empty": True}))
        return
    llm = make_llm()

    def work(row: dict) -> dict:
        last: Exception | None = None
        for _ in range(2):
            try:
                verdict = judge_necessity(llm, {"id": row["id"], "text": row["text"], "date": row["date"]})
                return {**row, **verdict}
            except Exception as exc:  # noqa: BLE001
                last = exc
        return {**row, "verdict": "ERROR", "confidence": 0.0, "reason": str(last)[:200]}

    with ThreadPoolExecutor(max_workers=LLM_WORKERS) as pool:
        results = list(pool.map(work, batch))

    out = report_path(args.dir, args.batch)
    out.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in results) + "\n")
    counts = Counter(r["verdict"] for r in results)
    flagged = [r for r in results if r["verdict"] not in {"DURABLE", "ERROR"}]
    print(json.dumps({
        "batch": args.batch,
        "judged": len(results),
        "verdicts": dict(counts),
        "flag_rate": round(len(flagged) / len(results), 3),
        "report": str(out),
    }, ensure_ascii=False, indent=2))


def cmd_commit(args: argparse.Namespace) -> None:
    from mem0_local.staleness import KIND_NECESSITY, pair_store

    rows = load_jsonl(report_path(args.dir, args.batch))
    skip = {s.strip() for s in (args.skip_ids or "").split(",") if s.strip()}
    store = pair_store()
    written, opened, skipped = 0, 0, Counter()
    for row in rows:
        if row["id"] in skip or any(row["id"].startswith(s) for s in skip):
            skipped["reviewer_skip"] += 1
            continue
        if row["verdict"] == "ERROR":
            skipped["error"] += 1
            continue
        rec = store.record_judgment(
            kind=KIND_NECESSITY,
            new_id=row["id"],
            old_id=row["id"],
            old_text=row["text"],
            verdict=row["verdict"],
            confidence=row["confidence"],
            reason=row["reason"],
            judge_model=PROMPT_TAG,
            new_session_id=SCAN_SESSION,
        )
        if rec["inserted"]:
            written += 1
            if rec["disposition"] == "open":
                opened += 1
        else:
            skipped["already_present"] += 1
    print(json.dumps({
        "batch": args.batch,
        "written": written,
        "opened": opened,
        "skipped": dict(skipped),
        "scan_session": SCAN_SESSION,
    }, ensure_ascii=False, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["plan", "judge", "commit"])
    parser.add_argument("--dir", type=Path, default=Path("/workspace/tmp/necessity_backlog"))
    parser.add_argument("--batch", type=int, default=0)
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--skip-ids", default="")
    args = parser.parse_args()
    {"plan": cmd_plan, "judge": cmd_judge, "commit": cmd_commit}[args.mode](args)


if __name__ == "__main__":
    main()
