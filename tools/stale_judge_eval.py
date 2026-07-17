#!/usr/bin/env python3
"""Labeled evaluation for the staleness judge (design §5 gate).

Pairs are real entries from the live workspace store, labeled during the
2026-07-17 design session. Ground-truth policy for borderline cases: label
what we WANT the system to do (e.g. a finalized sweep result displaces its
intermediate probes; a fulfilled request displaces the pending-request entry).

Usage (run inside the store venv, daemon or direct):
    python tools/stale_judge_eval.py build   # fetch texts, write pairs JSONL
    python tools/stale_judge_eval.py run     # judge every pair, report metrics
    python tools/stale_judge_eval.py run --pairs <file>

Gate (design §5): precision on SUPERSEDED matters more than recall — false
suspicions spend the reviewer's handoff budget; misses fall back to the
status quo.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_PAIRS = HERE / "stale_judge_eval_pairs.jsonl"

# (new_id, old_id, label, note)
LABELED_PAIRS: list[tuple[str, str, str, str]] = [
    # --- SUPERSEDED: same slot, newer answer ---
    ("221788b6", "aed2a628", "SUPERSEDED", "script default tokenizer restored after change"),
    ("ab60148d", "0c044b01", "SUPERSEDED", "root-cause mechanism corrected"),
    ("855bac81", "fb59a3bc", "SUPERSEDED", "consolidated root cause displaces early hypothesis"),
    ("bc3fcc1a", "fb59a3bc", "SUPERSEDED", "sampler vindicated vs sampler blamed"),
    ("a538c954", "fdfb8327", "SUPERSEDED", "results archive location moved"),
    ("4af45720", "26544c6e", "SUPERSEDED", "dashboard serving state: newer mapping"),
    ("070b56b5", "f4b0174b", "SUPERSEDED", "final sweep result displaces intermediate probe (borderline by policy)"),
    ("d9d9bb24", "6cbfb0d5", "SUPERSEDED", "grid progress snapshot: newer state"),
    ("e736b0b1", "bc97e693", "SUPERSEDED", "validation outcome fulfills pending rerun request (borderline by policy)"),
    ("253c90b7", "bc97e693", "SUPERSEDED", "supersession decision fulfills the same pending request (label fixed 2026-07-17)"),
    ("90fd5b90", "a2016825", "SUPERSEDED", "corrected ceiling is the newer answer to the same slot (label fixed 2026-07-17)"),
    # --- DUPLICATE: same slot, same information ---
    ("0c044b01", "fb59a3bc", "DUPLICATE", "same root-cause statement recorded twice"),
    # --- KEPT: different slot / complementary ---
    ("4a9501e1", "0c044b01", "KEPT", "closure event vs root-cause content"),
    ("6cbfb0d5", "f5b53c0b", "KEPT", "instance snapshot vs methodology note"),
    ("2dd3bdaa", "6a0addbc", "KEPT", "conclusion vs its evidence"),
    ("42c715e4", "6cbfb0d5", "KEPT", "run review vs grid plan counts: different aspects"),
    ("bee42dcb", "26544c6e", "KEPT", "different dashboards (pd-cap Pareto vs 8K P Pareto)"),
    ("29f935eb", "7749052d", "KEPT", "script refinement, core facts of old entry still valid"),
    ("f4b0174b", "001363de", "KEPT", "2.0 TPS probe vs 1.0 TPS probe: different operating points"),
    ("070b56b5", "86d7f2ef", "KEPT", "ocON final vs ocOFF final: different config"),
    ("253c90b7", "aed2a628", "KEPT", "dataset version vs script config: different slots"),
    ("2f93c334", "001363de", "KEPT", "128K workload vs agentic workload: different slot"),
    ("fb59a3bc", "f5b53c0b", "KEPT", "unrelated topics (MTP root cause vs pd-cap methodology)"),
    ("221788b6", "26544c6e", "KEPT", "unrelated topics (tokenizer vs dashboard)"),
]


def cli_get(memory_id: str) -> dict:
    out = subprocess.run(
        ["mem0-local", "--json", "get", memory_id], capture_output=True, text=True
    )
    if out.returncode != 0:
        raise SystemExit(f"mem0-local get {memory_id} failed: {out.stderr[:200]}")
    return json.loads(out.stdout)["data"]


def resolve_full_id(short_id: str) -> dict:
    """Ids in LABELED_PAIRS are 8-char prefixes; resolve via list scan once."""
    return _prefix_index()[short_id]


_index_cache: dict[str, dict] | None = None


def _prefix_index() -> dict[str, dict]:
    global _index_cache
    if _index_cache is None:
        out = subprocess.run(
            ["mem0-local", "--json", "list", "--page-size", "10000"],
            capture_output=True,
            text=True,
        )
        rows = json.loads(out.stdout)["data"]
        _index_cache = {}
        wanted = {p for pair in LABELED_PAIRS for p in (pair[0], pair[1])}
        for row in rows:
            prefix = str(row.get("id", ""))[:8]
            if prefix in wanted:
                _index_cache[prefix] = row
    return _index_cache


def build(pairs_path: Path) -> None:
    rows = []
    missing = []
    for new_pfx, old_pfx, label, note in LABELED_PAIRS:
        try:
            new_row = resolve_full_id(new_pfx)
            old_row = resolve_full_id(old_pfx)
        except KeyError as exc:
            missing.append((new_pfx, old_pfx, str(exc)))
            continue
        rows.append(
            {
                "new_id": new_row["id"],
                "old_id": old_row["id"],
                "new_date": (new_row.get("created_at") or "")[:10],
                "old_date": (old_row.get("created_at") or "")[:10],
                "new_text": new_row.get("memory") or "",
                "old_text": old_row.get("memory") or "",
                "label": label,
                "note": note,
            }
        )
    pairs_path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n"
    )
    print(f"wrote {len(rows)} pairs to {pairs_path}")
    if missing:
        print(f"WARNING: {len(missing)} pairs unresolved: {missing}")


def make_llm():
    sys.path.insert(0, str(HERE.parent / "src"))
    from mem0_local.cli import setup_env
    from mem0_local.config import (
        LLM_APP_NAME,
        LLM_BASE_URL,
        LLM_MODEL,
        LLM_SITE_URL,
    )

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
            "max_tokens": 2000,
            "top_p": 0.1,
            "is_reasoning_model": False,
        },
    )


def run(pairs_path: Path) -> int:
    sys.path.insert(0, str(HERE.parent / "src"))
    from mem0_local.judge import judge

    pairs = [json.loads(l) for l in pairs_path.read_text().splitlines() if l.strip()]
    llm = make_llm()

    confusion: Counter = Counter()
    failures: list[dict] = []
    for pair in pairs:
        new_entry = {"id": pair["new_id"], "text": pair["new_text"], "date": pair["new_date"]}
        candidate = {"id": pair["old_id"], "text": pair["old_text"], "date": pair["old_date"]}
        try:
            verdicts = judge(llm, new_entry, [candidate])
        except Exception as exc:  # noqa: BLE001
            confusion[(pair["label"], "ERROR")] += 1
            failures.append({**pair, "predicted": "ERROR", "error": str(exc)})
            continue
        predicted = verdicts[0]["verdict"] if verdicts else "KEPT"
        confidence = verdicts[0]["confidence"] if verdicts else 0.0
        reason = verdicts[0]["reason"] if verdicts else ""
        confusion[(pair["label"], predicted)] += 1
        status = "ok" if predicted == pair["label"] else "MISS"
        print(
            f"{status:4s} label={pair['label']:10s} pred={predicted:10s} "
            f"conf={confidence:.2f} | {pair['note'][:60]}"
        )
        if predicted != pair["label"]:
            failures.append({**pair, "predicted": predicted, "confidence": confidence, "reason": reason})

    total = sum(confusion.values())
    correct = sum(v for (label, pred), v in confusion.items() if label == pred)
    sup_pred = sum(v for (_, pred), v in confusion.items() if pred == "SUPERSEDED")
    sup_true = sum(v for (label, _), v in confusion.items() if label == "SUPERSEDED")
    sup_hit = confusion[("SUPERSEDED", "SUPERSEDED")]
    print("\n=== metrics ===")
    print(f"accuracy: {correct}/{total} = {correct/total:.2%}")
    if sup_pred:
        print(f"SUPERSEDED precision: {sup_hit}/{sup_pred} = {sup_hit/sup_pred:.2%}")
    if sup_true:
        print(f"SUPERSEDED recall:    {sup_hit}/{sup_true} = {sup_hit/sup_true:.2%}")
    print("confusion:", dict(confusion))
    if failures:
        print(f"\n{len(failures)} misses:")
        for f in failures:
            print(f"  want {f['label']} got {f['predicted']}: {f['note']}")
            if f.get("reason"):
                print(f"    judge reason: {f['reason'][:150]}")
    return 0 if not failures else 1


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["build", "run"])
    parser.add_argument("--pairs", type=Path, default=DEFAULT_PAIRS)
    args = parser.parse_args()
    if args.mode == "build":
        build(args.pairs)
    else:
        raise SystemExit(run(args.pairs))


if __name__ == "__main__":
    main()
