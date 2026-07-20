#!/usr/bin/env python3
"""Labeled evaluation for the memory-necessity judge (lifecycle design R1).

Entries are real rows from the live workspace store, labeled during the
2026-07-20 lifecycle-design session survey. Ground-truth policy: label what
we WANT review to see. DURABLE traps (lessons, corrections, high-cost audit
conclusions) are the critical set — flagging one of those is the worst
error class, since review budget and trust both depend on flag precision.

Usage (run inside the store venv):
    python tools/necessity_judge_eval.py build
    python tools/necessity_judge_eval.py run --prompt v1
    python tools/necessity_judge_eval.py run --prompt v2 --set <file>

Gate: zero false flags on the trap subset; flag precision > accuracy on
recall (missed flags just fall back to the weekly sweep).
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_SET = HERE / "necessity_judge_eval_set.jsonl"

VERDICTS = {
    "DURABLE",
    "PROGRESS_TICK",
    "ACTIVITY_LOG",
    "COMMIT_RECORD",
    "REPO_FACT",
    "EVENT_SCOPED",
}

# (id_prefix, label, trap, note)
LABELED_ENTRIES: list[tuple[str, str, bool, str]] = [
    # --- PROGRESS_TICK: point-in-time progress/load numbers ---
    ("3e4e0a0d", "PROGRESS_TICK", False, "grid plan counts done/failed/running/pending"),
    ("baedb913", "PROGRESS_TICK", False, "run progress 31.82% TPS=1.50"),
    ("bf45b01d", "PROGRESS_TICK", False, "proxy load snapshot over last 60s"),
    ("4fd2b81f", "PROGRESS_TICK", False, "proxy no-queue-buildup snapshot"),
    ("5d38f02b", "PROGRESS_TICK", False, "run launched with PID, being monitored"),
    ("b8692997", "PROGRESS_TICK", False, "trace run progress 3488/10156 rows"),
    ("dffdd538", "PROGRESS_TICK", False, "collection progress at timestamp"),
    ("7eee3f0f", "PROGRESS_TICK", False, "process paused via SIGSTOP, progress at pause"),
    ("24f6590f", "PROGRESS_TICK", False, "self-declared session-scoped idle-probe snapshot"),
    # --- ACTIVITY_LOG: narration of routine agent actions ---
    ("ab89e12f", "ACTIVITY_LOG", False, "agent read memory files as asked; no facts"),
    ("34963c08", "ACTIVITY_LOG", False, "agent deleted directory as requested"),
    ("74c84eb4", "ACTIVITY_LOG", False, "harness files updated at times X and Y"),
    ("12710987", "ACTIVITY_LOG", False, "one agent recommended another write a ledger"),
    ("338778ea", "ACTIVITY_LOG", False, "audit-response iteration completed narration"),
    ("6447c20c", "ACTIVITY_LOG", False, "agent reread audit and appended response file"),
    # --- COMMIT_RECORD: restates git commit content/metadata ---
    ("0d413aae", "COMMIT_RECORD", False, "commit author/committer/timestamp metadata"),
    ("67b3d9bc", "COMMIT_RECORD", False, "committed-and-pushed with title and flags"),
    ("8ae4bd8c", "COMMIT_RECORD", False, "what the CI-fix commit modified"),
    ("647d2d76", "COMMIT_RECORD", False, "changes are now part of committed history"),
    ("1f03b117", "COMMIT_RECORD", False, "commit added early-stop args (mixed: tests note)"),
    # --- REPO_FACT: restates repo-file-readable content ---
    ("d803a6c8", "REPO_FACT", False, "verbatim digest of a checked-in SKILL.md"),
    ("d8e1ac9d", "REPO_FACT", False, "code-readable default value of a flag"),
    ("2c6c2b0a", "REPO_FACT", False, "README-readable changelog trigger semantics"),
    # --- EVENT_SCOPED: legitimate but tied to an ongoing event ---
    ("374e1ddf", "EVENT_SCOPED", False, "launch blocked until staging done"),
    ("231ef112", "EVENT_SCOPED", False, "fix passed tests, next gate re-measure"),
    ("1c163dbd", "EVENT_SCOPED", False, "pending config update after sweep"),
    ("4f5f21b5", "EVENT_SCOPED", False, "pending fixes list"),
    ("40980cf6", "EVENT_SCOPED", False, "hosts file not yet updated, hosts held"),
    ("52708c43", "EVENT_SCOPED", False, "closure requires logs to be established"),
    ("784a8456", "EVENT_SCOPED", False, "grid awaits freed machines"),
    ("c751d4b5", "EVENT_SCOPED", False, "env prep for newly-freed batch"),
    # --- DURABLE: measurements with config ---
    ("648c0c42", "DURABLE", False, "measured prefix_hit for workload"),
    ("048926e0", "DURABLE", False, "per-dataset accept_len measurement"),
    ("1bf900b6", "DURABLE", False, "DSpark vs MTP accept_len comparison"),
    ("5135a55b", "DURABLE", False, "parity result workers=1 vs 4"),
    ("70caccee", "DURABLE", False, "HBM baseline per die for config"),
    # --- DURABLE: decisions / rules with rationale ---
    ("04ed9f76", "DURABLE", False, "AB sweep config rule"),
    ("73c7d5ef", "DURABLE", False, "feature mutex constraint"),
    ("3f4ba441", "DURABLE", False, "user-clarified project objective"),
    ("128226e9", "DURABLE", False, "user-defined correctness gate"),
    ("245daabb", "DURABLE", False, "hard resource constraint for experiments"),
    ("d35030e7", "DURABLE", False, "oversampling conclusion shaping future sweeps"),
    ("3dbb45f5", "DURABLE", False, "machine-occupation tagging rule"),
    ("57e48382", "DURABLE", False, "user preference: small single-fact adds"),
    ("8dd37414", "DURABLE", False, "host locking convention semantics"),
    # --- DURABLE: root causes ---
    ("3fb3d1aa", "DURABLE", False, "crash 507018 root cause"),
    ("e33134ea", "DURABLE", False, "optimizer/mixed-precision root cause"),
    ("101c54e5", "DURABLE", False, "HCCL hang solved via env vars"),
    ("fa79af88", "DURABLE", False, "regression introduction window"),
    ("991ffd82", "DURABLE", False, "repetition is intrinsic attractor conclusion"),
    # --- DURABLE traps: hard-won lessons (look like env trivia) ---
    ("84f7f739", "DURABLE", True, "lesson: back up Gitee PR body first"),
    ("8d6194eb", "DURABLE", True, "lesson: curl hijacked by proxy, use --noproxy"),
    ("e4f4db3b", "DURABLE", True, "lesson: pkill -f self-kill footgun"),
    # --- DURABLE traps: corrections/retractions (look like stale status) ---
    ("658d3d03", "DURABLE", True, "marks earlier audit conclusion as wrong"),
    ("521569eb", "DURABLE", True, "honest retraction of false positive"),
    ("7764f178", "DURABLE", True, "correction: feature is supported, not a bug"),
    ("a0d7ee65", "DURABLE", True, "superseded premature reading, kept as warning"),
    ("33eb66de", "DURABLE", True, "withdrawn root-cause hypothesis record"),
    # --- DURABLE traps: high-cost audit conclusions (look code-derivable) ---
    ("39a994d2", "DURABLE", True, "call-chain audit conclusion"),
    ("aadaf581", "DURABLE", True, "instrumented mechanism investigation finding"),
    ("bded800f", "DURABLE", True, "serving-capture boundary audit"),
    ("63d76f0d", "DURABLE", True, "scope synthesis of customization surface"),
    # --- DURABLE: artifact pointers ---
    ("93b95ba3", "DURABLE", False, "curves/metrics archive path"),
    ("195519fe", "DURABLE", False, "checkpoint archive path with size"),
    ("e09e99ee", "DURABLE", False, "sweep logs archived path"),
    ("41308b73", "DURABLE", False, "hidden-state files storage path"),
    # --- DURABLE: external/ops facts ---
    ("fdea50e1", "DURABLE", False, "SSH auth fact: must use root on host"),
    ("2f2b56a7", "DURABLE", False, "model quant config incompatibility fact"),
]


PROMPTS: dict[str, str] = {}

PROMPTS["v1"] = """\
You are a memory-necessity judge for an engineering memory store. Entries
are dated snapshots written by coding agents during infrastructure work.
Given ONE entry, decide whether it deserves long-term memory, or belongs
to a category that should be flagged for review.

Verdicts:
- DURABLE: worth keeping. THE DEFAULT — use whenever uncertain.
- PROGRESS_TICK: a point-in-time progress/status/load number that will be
  worthless within hours: percent complete, queue/plan counts, requests-
  per-minute snapshots, "launched with PID N", mid-run metrics at step N.
- ACTIVITY_LOG: narrates that an agent performed routine actions ("agent
  read X then searched Y", "deleted directory as requested", "updated the
  file at 11:39") with no reusable fact beyond the action itself.
- COMMIT_RECORD: restates what a git commit/PR contains or its metadata
  (hash, title, author, what it changed). git log/show already has this.
- REPO_FACT: restates content plainly readable in a repository file (a
  doc's content, a config default, what a script does).
- EVENT_SCOPED: legitimate to record, but usefulness is tied to an ongoing
  event or handoff (pending task, "blocked until X", "next gate is Y",
  machines held for a campaign). Should expire when the event ends.

Hard rules — these override everything above:
1. Judge by RE-ACQUISITION COST, not theoretical derivability. A
   conclusion distilled from hours of auditing, debugging, instrumented
   experiments, or sweeps is DURABLE even if re-derivable from code
   ("audited the call chain and proved X", "mechanism investigation
   found Y").
2. Corrections, retractions, and hard-won lessons are ALWAYS DURABLE:
   entries marking earlier conclusions wrong/superseded/withdrawn, and
   learned-the-hard-way rules (footguns, gotchas, mandatory safeguards).
   They prevent future agents from repeating mistakes.
3. Measurements with their configuration, decisions/rules with rationale,
   root-cause findings, external-world facts (hosts, auth quirks,
   artifact paths, hashes), and pointers to archives are DURABLE.
4. Mixed entries: flag the dominant category (review rewrites rather than
   discards), but if the non-derivable part is the entry's real payload,
   choose DURABLE.
5. When uncertain, DURABLE.

confidence: probability a human reviewer confirms your verdict.
reason: one sentence naming what the entry is.

Output JSON only:
{"verdict":"...","confidence":0.0,"reason":"..."}
"""


PROMPTS["v2"] = """\
You are a memory-necessity judge for an engineering memory store. Entries
are dated snapshots written by coding agents during infrastructure work.
Given ONE entry, decide whether it deserves long-term memory, or belongs
to a category that should be flagged for review.

Verdicts:
- DURABLE: worth keeping. THE DEFAULT — use whenever uncertain.
- PROGRESS_TICK: the IN-FLIGHT state of a still-running process, worthless
  within hours: percent complete, counts that are still changing
  (done/running/pending), current load/traffic over a window, "launched
  with PID N", "paused at step N", mid-run metrics. NOT this category:
  the COMPLETED result of an experiment/benchmark/comparison together
  with its configuration — that is a DURABLE measurement even if dated
  and later re-measured.
- ACTIVITY_LOG: narrates that an agent performed or should perform routine
  actions ("agent read X then searched Y", "deleted directory as
  requested", "updated file at 11:39", "A recommended B record their
  findings") with no reusable technical fact beyond the action itself.
- COMMIT_RECORD: the entry's central content is what a git commit/PR
  contains, changed, or its metadata (hash, title, author, timestamps),
  or an assertion that changes are now committed/pushed. git log/show
  already stores all of this. Attached context like "tests passed" does
  not rescue it — review rewrites to keep such fragments. Only when the
  commit reference is incidental to a finding that stands on its own
  ("root cause was X; fixed in commit Y") is the entry DURABLE.
- REPO_FACT: restates what an agent could get by simply opening a named
  checked-in file: a doc/skill's content, a config default, what a script
  does. Phrasing it as a "mechanism" or "design decision" does not rescue
  it if the file says the same thing. Only hard-won insight beyond the
  file's text (a footgun, a proven interaction) makes it DURABLE.
- EVENT_SCOPED: legitimate record whose usefulness is tied to an ongoing
  event, campaign, or handoff. Cues: "pending", "blocked until",
  "awaiting", "not yet done", "next gate is", "requires X before Y",
  "machines held/locked for the run", staging/preparation state for an
  upcoming batch. The payload is an open dependency or coordination
  state, not a finding; it should expire when the event closes. (An
  in-flight NUMBER is PROGRESS_TICK; an open TO-DO/dependency is
  EVENT_SCOPED.)

Hard rules — these override everything above:
1. Judge by RE-ACQUISITION COST, not theoretical derivability. A
   conclusion distilled from hours of auditing, debugging, instrumented
   experiments, or sweeps is DURABLE even if re-derivable from code
   ("audited the call chain and proved X", "mechanism investigation
   found Y").
2. Corrections, retractions, and hard-won lessons are ALWAYS DURABLE:
   entries marking earlier conclusions wrong/superseded/withdrawn, and
   learned-the-hard-way rules (footguns, gotchas, mandatory safeguards).
   They prevent future agents from repeating mistakes.
3. COMPLETED measurements/comparisons with their configuration,
   decisions/rules with rationale, root-cause findings, external-world
   facts (hosts, auth quirks, artifact paths, hashes), and pointers to
   archives are DURABLE.
4. When uncertain between a flag category and DURABLE, choose DURABLE.
   When certain it should be flagged but torn between two flag
   categories, pick the one matching the entry's dominant content.

confidence: probability a human reviewer confirms your verdict.
reason: ONE short sentence naming what the entry is. Never quote long
fragments of the entry.

Output JSON only:
{"verdict":"...","confidence":0.0,"reason":"..."}
"""


# "prod" = whatever judge.py currently ships, so a rerun of this eval always
# gates the deployed prompt against the labeled set.
sys.path.insert(0, str(HERE.parent / "src"))
from mem0_local.judge import NECESSITY_PROMPT as _PROD_NECESSITY_PROMPT  # noqa: E402

PROMPTS["prod"] = _PROD_NECESSITY_PROMPT

# v3 = v2 with two surgical deltas: (a) operational-state snapshots are ticks
# even when "complete"; (b) suppress the insight-rescue of single repo facts.
PROMPTS["v3"] = PROMPTS["v2"].replace(
    """  the COMPLETED result of an experiment/benchmark/comparison together
  with its configuration — that is a DURABLE measurement even if dated
  and later re-measured.""",
    """  the COMPLETED result of a controlled experiment/benchmark/comparison
  together with its configuration — that is a DURABLE measurement even
  if dated. But an observation of live OPERATIONAL state — current
  traffic/latency over a window, queue depth, which hosts are idle/busy,
  service health at a moment — is still PROGRESS_TICK even when the
  probe itself completed: the observed state churns on its own.""",
).replace(
    """  file's text (a footgun, a proven interaction) makes it DURABLE.""",
    """  file's text (a footgun, a proven interaction) makes it DURABLE.
  "Could inform future work" is NOT such insight: a single config
  default or a doc's stated behavior stays REPO_FACT.""",
)

# v4 = v2 + ONLY the operational-state delta (v3's REPO_FACT suppression
# caused a flag-precision regression on a user-preference entry).
PROMPTS["v4"] = PROMPTS["v2"].replace(
    """  the COMPLETED result of an experiment/benchmark/comparison together
  with its configuration — that is a DURABLE measurement even if dated
  and later re-measured.""",
    """  the COMPLETED result of a controlled experiment/benchmark/comparison
  together with its configuration — that is a DURABLE measurement even
  if dated. But an observation of live OPERATIONAL state — current
  traffic/latency over a window, queue depth, which hosts are idle/busy,
  service health at a moment — is still PROGRESS_TICK even when the
  probe itself completed: the observed state churns on its own.""",
)


def cli_list_index(prefixes: set[str]) -> dict[str, dict]:
    out = subprocess.run(
        ["mem0-local", "--json", "list", "--page-size", "10000"],
        capture_output=True,
        text=True,
    )
    rows = json.loads(out.stdout)["data"]
    index: dict[str, dict] = {}
    for row in rows:
        prefix = str(row.get("id", ""))[:8]
        if prefix in prefixes:
            index[prefix] = row
    return index


def build(set_path: Path) -> None:
    index = cli_list_index({e[0] for e in LABELED_ENTRIES})
    rows, missing = [], []
    for prefix, label, trap, note in LABELED_ENTRIES:
        row = index.get(prefix)
        if row is None:
            missing.append(prefix)
            continue
        rows.append(
            {
                "id": row["id"],
                "date": (row.get("created_at") or "")[:10],
                "text": row.get("memory") or "",
                "label": label,
                "trap": trap,
                "note": note,
            }
        )
    set_path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n")
    print(f"wrote {len(rows)} entries to {set_path}")
    if missing:
        print(f"WARNING: {len(missing)} unresolved prefixes: {missing}")


def make_llm():
    sys.path.insert(0, str(HERE.parent / "src"))
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


def judge_one(llm: object, prompt: str, entry: dict) -> dict:
    user = f"## Entry (written {entry['date'] or 'unknown date'})\n{entry['text']}"
    response = llm.generate_response(
        messages=[
            {"role": "system", "content": prompt},
            {"role": "user", "content": user},
        ],
        response_format={"type": "json_object"},
    )
    text = (response or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n|```$", "", text).strip()
    try:
        data = json.loads(text, strict=False)
    except json.JSONDecodeError:
        # Salvage a truncated response: the verdict field comes first.
        m = re.search(r'"verdict"\s*:\s*"([A-Z_]+)"', text)
        if not m:
            raise
        data = {"verdict": m.group(1), "confidence": 0.0, "reason": "<truncated output>"}
    verdict = str(data.get("verdict", "")).upper()
    if verdict not in VERDICTS:
        verdict = "PARSE_ERROR"
    return {
        "verdict": verdict,
        "confidence": float(data.get("confidence", 0.0) or 0.0),
        "reason": str(data.get("reason") or "")[:300],
    }


def run(set_path: Path, prompt_name: str) -> int:
    prompt = PROMPTS[prompt_name]
    entries = [json.loads(l) for l in set_path.read_text().splitlines() if l.strip()]
    llm = make_llm()

    def work(entry: dict) -> tuple[dict, dict]:
        last_exc: Exception | None = None
        for _ in range(2):  # one retry: empty/transient responses happen.
            try:
                return entry, judge_one(llm, prompt, entry)
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
        return entry, {"verdict": "ERROR", "confidence": 0.0, "reason": str(last_exc)[:200]}

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(work, entries))

    confusion: Counter = Counter()
    misses: list[tuple[dict, dict]] = []
    trap_flags = 0
    for entry, pred in results:
        confusion[(entry["label"], pred["verdict"])] += 1
        ok = pred["verdict"] == entry["label"]
        if not ok:
            misses.append((entry, pred))
        if entry["trap"] and pred["verdict"] != "DURABLE":
            trap_flags += 1
        print(
            f"{'ok  ' if ok else 'MISS'} label={entry['label']:13s} "
            f"pred={pred['verdict']:13s} conf={pred['confidence']:.2f} | {entry['note'][:55]}"
        )

    total = len(results)
    correct = total - len(misses)
    flagged_pred = sum(1 for _, p in results if p["verdict"] not in {"DURABLE", "ERROR", "PARSE_ERROR"})
    flagged_hit = sum(
        1
        for e, p in results
        if p["verdict"] not in {"DURABLE", "ERROR", "PARSE_ERROR"} and e["label"] != "DURABLE"
    )
    flag_true = sum(1 for e, _ in results if e["label"] != "DURABLE")
    print("\n=== metrics ===")
    print(f"prompt: {prompt_name}")
    print(f"accuracy (exact verdict): {correct}/{total} = {correct/total:.2%}")
    if flagged_pred:
        print(f"flag precision (non-DURABLE): {flagged_hit}/{flagged_pred} = {flagged_hit/flagged_pred:.2%}")
    print(f"flag recall: {flagged_hit}/{flag_true} = {flagged_hit/flag_true:.2%}")
    print(f"trap false-flags (CRITICAL, must be 0): {trap_flags}")
    if misses:
        print(f"\n{len(misses)} misses:")
        for entry, pred in misses:
            print(f"  want {entry['label']:13s} got {pred['verdict']:13s} | {entry['note']}")
            print(f"    judge reason: {pred['reason'][:160]}")
    return 0 if (trap_flags == 0 and correct / total >= 0.85) else 1


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["build", "run"])
    parser.add_argument("--set", type=Path, default=DEFAULT_SET, dest="set_path")
    parser.add_argument("--prompt", default="v1", choices=sorted(PROMPTS))
    args = parser.parse_args()
    if args.mode == "build":
        build(args.set_path)
    else:
        raise SystemExit(run(args.set_path, args.prompt))


if __name__ == "__main__":
    main()
