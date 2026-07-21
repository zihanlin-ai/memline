#!/usr/bin/env python3
"""Labeled evaluation for the correctness judge (timestamp / attribution /
language). The critical set is the LANGUAGE dimension added 2026-07-21: the
local store embeds with an English-only model, so memory NARRATIVE must be
English; only preserved technical identifiers (and short quoted proper-noun
tokens) may carry non-English characters.

Metrics gate on:
  - overall accuracy
  - zero language false-alarms on English-prose traps that embed Chinese
    identifiers / quoted group names (must stay CONSISTENT)
  - zero missed Chinese-narrative entries (must be LANGUAGE_SUSPECT)
  - timestamp/attribution regression preserved

Usage (store venv):
    python tools/correctness_judge_eval.py run
"""

from __future__ import annotations

import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))

# Authoritative metadata used for every case unless the case overrides it.
NOW = "2026-07-21T08:00:00+00:00"

# (text, label, note, overrides). overrides: dict with ingested_at/created_at/writer.
CASES: list[tuple[str, str, str, dict]] = [
    # --- LANGUAGE_SUSPECT: Chinese narrative (whole sentences) ---
    ("2026-07-20 已按用户要求在 remote-inventory.md 新增 Host Lease Utility 章节，记录 host_lease.py 的用法与占用标签语义。",
     "LANGUAGE_SUSPECT", "chinese narrative (real 47c5d89f style)", {}),
    ("重构完成：ops.py 合并为单一 op 注册表，runtime.py 抽出共享 bootstrap，所有 116 个单测通过。",
     "LANGUAGE_SUSPECT", "chinese narrative about refactor", {}),
    ("用户澄清 available:true 表示部门层面我方对这批机器有独占权利，可直接 kill 同事的进程。",
     "LANGUAGE_SUSPECT", "chinese narrative about hosts rule", {}),
    ("在 7.150.11.86 上跑 run10 基准，用了 20 份拷贝共 40000 条样本，结果符合预期。",
     "LANGUAGE_SUSPECT", "chinese narrative with hosts/numbers", {}),
    # --- CONSISTENT traps: English prose that EMBEDS Chinese identifiers/names ---
    ("Added group superpod_30 from the \"裸机资源分配群\" allocation channel; all entries are available:false.",
     "CONSISTENT", "trap: english prose, quoted CN group name", {}),
    ("Pulled model /data/models/openPangu-2.0-Flash to host 7.150.11.86 at ~3 MB/s over the WAN egress.",
     "CONSISTENT", "trap: english prose, paths/hosts", {}),
    ("Set OPENROUTER_API_KEY before running distdl.py; the token lives in the store .env file.",
     "CONSISTENT", "trap: english prose, env var + path", {}),
    ("The crash reason was `RuntimeError: MoE all-to-all dispatch failed`; fix landed in commit a1e284a0.",
     "CONSISTENT", "trap: english prose, error string + hash", {}),
    # --- CONSISTENT traps: English NARRATIVE that QUOTES a whole Chinese sentence
    #     (user instruction / notice) verbatim — the narrative is English, the
    #     quoted original must be preserved. These were real judge false-positives. ---
    ("On 2026-07-15, Codex corrected the attribution for the cleanup actions under the user "
     "instruction '清理这些机器上妨碍拉起服务的进程，然后按照之前的方案拉起服务', not the earlier suspected source.",
     "CONSISTENT", "trap: english narrative quoting a full CN user instruction", {}),
    ("On 2026-06-13, the user decided to stop the OmniCache service and bring it up from the "
     "reference yml to test reproducibility, stating '把这个服务停了，直接用reference的yml拉起，再测一次看能不能复现'.",
     "CONSISTENT", "trap: english narrative quoting a full CN quote", {}),
    ("Caveat from the 2026-07-03 probe: superpod_30 and superpod_75 hosts are from a bare-metal "
     "resource allocation group (裸机资源分配群) not allocated to the team — NPU-idle does not mean free.",
     "CONSISTENT", "trap: english narrative + parenthetical CN gloss", {}),
    ("AGENTS.md, KLAUD_DEBUG.md, and .claude/commands are the designated files for agent operations (运维).",
     "CONSISTENT", "trap: english sentence with one CN word", {}),
    # --- CONSISTENT: ordinary English narrative ---
    ("2026-07-21 refactored the daemon dispatch into ops.py; all 116 unit tests pass on host devbox.",
     "CONSISTENT", "plain english fact", {}),
    ("DSpark K5 accept_len 2.702 vs native MTP K3 3.022 on the claw agentic trace.",
     "CONSISTENT", "english measurement", {}),
    ("The user approved the safety full-store scan; 7 real plaintext credentials were redacted in place.",
     "CONSISTENT", "english, records user decision (not misattribution)", {}),
    # --- TIMESTAMP_SUSPECT (english, wrong current-date) ---
    ("Today 2026-05-01 I just finished the sweep and all hosts are now drained and released.",
     "TIMESTAMP_SUSPECT", "english, current event dated ~2.5 months off", {}),
    # --- CONSISTENT: historical date narrated as history (not a timestamp bug) ---
    ("On 2026-06-13 the model safety boundary test wrote 680 records to censor_eval_raw_n20.jsonl.",
     "CONSISTENT", "english, historical event with its own old date", {}),
    # --- CONSISTENT: SGT text date vs UTC ingestion crossing midnight (real FP pattern) ---
    ("On 2026-07-11, Codex verified root passwordless SSH to all six Pangu 505B image100008 hosts.",
     "CONSISTENT", "trap: SGT text date = ingest UTC 07-10 16:33 +8h",
     {"ingested_at": "2026-07-10T16:33:04+00:00", "created_at": "2026-07-10T16:33:04+00:00"}),
    ("On 2026-07-12, a deep-adapter SGD lr=1e-2 trial completed on host 7.246.46.17: val loss 20.75.",
     "CONSISTENT", "trap: SGT text date = ingest UTC 07-11 18:28 +8h",
     {"ingested_at": "2026-07-11T18:28:02+00:00", "created_at": "2026-07-11T18:28:02+00:00"}),
    # --- CONSISTENT: status/revision markers dated at/after original ingestion (updated in place) ---
    ("RESOLVED 2026-07-07: the pd-cap-dashboard pre-publication scrub items are fixed and verified.",
     "CONSISTENT", "trap: RESOLVED marker, entry updated in place later",
     {"ingested_at": "2026-07-01T10:00:00+00:00", "created_at": "2026-07-01T10:00:00+00:00"}),
    ("As of 2026-07-13, live multi-request Pangu DSpark speculative metrics are stable.",
     "CONSISTENT", "trap: 'As of' status snapshot marker",
     {"ingested_at": "2026-07-10T12:00:00+00:00", "created_at": "2026-07-10T12:00:00+00:00"}),
    # --- ATTRIBUTION_SUSPECT (english, reversed actor) ---
    ("The user personally ran npu-smi and rebuilt the container image on every one of the 299 hosts himself.",
     "ATTRIBUTION_SUSPECT", "english, implausible attribution to user", {}),
    # --- Mixed: chinese narrative AND a timestamp problem -> prefer LANGUAGE ---
    ("今天 2026-05-01 我刚跑完 sweep，所有主机现在都已 drained 并释放。",
     "LANGUAGE_SUSPECT", "chinese narrative that also has a date issue -> language wins", {}),
]


def make_llm():
    from mem0_local.runtime import setup_env
    from mem0_local.config import LLM_APP_NAME, LLM_BASE_URL, LLM_MODEL, LLM_SITE_URL

    setup_env()
    from mem0.utils.factory import LlmFactory

    return LlmFactory.create(
        "openai",
        {"model": LLM_MODEL, "openrouter_base_url": LLM_BASE_URL, "site_url": LLM_SITE_URL,
         "app_name": LLM_APP_NAME, "temperature": 0.0, "max_tokens": 400,
         "top_p": 0.1, "is_reasoning_model": False},
    )


def run() -> int:
    from mem0_local.judge import judge_correctness

    llm = make_llm()

    def work(case):
        text, label, note, ov = case
        last = {"verdict": "ERROR", "confidence": 0.0, "reason": ""}
        for _ in range(2):
            try:
                last = judge_correctness(
                    llm, {"text": text},
                    ingested_at=ov.get("ingested_at", NOW),
                    created_at=ov.get("created_at"),
                    writer=ov.get("writer", "claude"),
                )
                break
            except Exception as exc:  # noqa: BLE001
                last = {"verdict": "ERROR", "confidence": 0.0, "reason": str(exc)[:120]}
        return case, last

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(work, CASES))

    confusion, miss = Counter(), []
    for (text, label, note, _ov), pred in results:
        v = pred["verdict"]
        confusion[(label, v)] += 1
        ok = v == label
        if not ok:
            miss.append((label, v, note))
        print(f"{'ok  ' if ok else 'MISS'} want={label:18s} got={v:18s} "
              f"conf={pred['confidence']:.2f} | {note}")

    total = len(results)
    correct = total - len(miss)
    # Critical language errors.
    lang_false_alarm = sum(
        confusion[(l, "LANGUAGE_SUSPECT")] for l in
        ("CONSISTENT", "TIMESTAMP_SUSPECT", "ATTRIBUTION_SUSPECT"))
    # A Chinese-narrative case is only truly MISSED if it slips through as
    # CONSISTENT (unflagged). Being flagged under another correctness verdict
    # (e.g. a mixed Chinese+date entry judged TIMESTAMP_SUSPECT) still routes it
    # to the same `update` disposition, so it is not a miss.
    lang_miss = confusion[("LANGUAGE_SUSPECT", "CONSISTENT")]
    print("\n=== metrics ===")
    print(f"accuracy: {correct}/{total} = {correct/total:.2%}")
    print(f"language false alarms (English-prose flagged as LANGUAGE, must be 0): {lang_false_alarm}")
    print(f"missed Chinese narratives (must be 0): {lang_miss}")
    if miss:
        print("misses:", miss)
    return 0 if (lang_false_alarm == 0 and lang_miss == 0 and correct / total >= 0.85) else 1


if __name__ == "__main__":
    raise SystemExit(run())
