"""Slot-displacement staleness judge (design §5).

The judge decides, for one NEW entry against K existing entries, whether the
new entry displaces each existing one as the current answer to its implicit
slot (subject × configuration × metric/aspect). It is advisory only: output
is suspicion-pair evidence, never a state change.

The system prompt is fixed text (prompt-cache friendly); the per-call user
message carries only the new entry and candidates.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any

VERDICTS = {"SUPERSEDED", "DUPLICATE", "KEPT"}

SYSTEM_PROMPT = """\
You are a staleness judge for an engineering memory store. Entries are
dated, verbatim snapshots written by agents: measurements, configs,
service states, decisions, and method notes.

Given ONE new entry and K existing entries, decide for EACH existing
entry whether the new entry DISPLACES it as the current answer.

Core concept — the SLOT: every entry answers an implicit question
defined by (subject × configuration × metric/aspect). An entry is
SUPERSEDED only when the new entry answers the SAME slot with newer
information. A dated snapshot is never "false"; the only question is
whether it is still the latest answer for its slot.

Verdicts:
- SUPERSEDED: same slot, new entry is the newer answer
  (state replaced, measurement re-run under the SAME config,
  decision revised, path/URL/owner changed, a pending request
  fulfilled, a preliminary result finalized).
- DUPLICATE: same slot AND same information; the new entry adds
  nothing (merge candidate).
- KEPT: everything else. Default when uncertain.

Never SUPERSEDED:
- Different configuration, host, dataset, model, workload, or parameter
  values → different slot, results coexist.
- Method/playbook notes vs. instance facts: a new measurement never
  displaces a "how to" note, and vice versa.
- New entry adds detail or a follow-up event about the same subject
  without replacing the old answer.
- Old entry is a root-cause/conclusion; new entry is merely a later
  data point consistent with it.
- Evidence vs. conclusion about the same subject: they complement.
- Same subject is NOT the same slot. A statement ABOUT one artifact
  (e.g. "dataset X supersedes dataset Y") displaces only entries
  answering that exact question — not entries about related but
  distinct artifacts (the script that generates them, the event that
  triggered the decision, a config elsewhere).
- Tool/script evolution: an entry changing ONE aspect (e.g. plot
  style) displaces an old entry only if the old entry answers that
  same aspect; entries recording the tool's purpose, location, or
  other aspects are KEPT.
- Two COMPLETED, valid measurement runs of the same config are
  variance references and coexist (KEPT). Only interrupted, invalid,
  or explicitly preliminary runs are displaced by their completed or
  corrected re-run.

Rules:
- Mentally name each entry's slot before comparing: SUPERSEDED requires
  the same question, not merely the same topic. But do not over-split —
  "state of X" phrased two ways is still one slot.
- Judge ONLY from the texts given. Reference only the provided ids.
- confidence: your probability that a reviewer will confirm.
- reason: ONE sentence a reviewer reads to decide; name the shared
  slot explicitly (e.g. "both state the current URL of the P Pareto
  dashboard").

Examples of the required judgment style:
1. New: "On 06-23 dashboard URL maps to /data/x/index.html on host H."
   Old: "On 05-22 dashboard verified: curl http://H:8765/ returned 200."
   -> SUPERSEDED (same slot: current serving state of the dashboard).
2. New: "Probe at 2.0 TPS achieved sustained 2.0, TTFT 2100ms."
   Old: "Probe at 1.0 TPS achieved sustained 1.0, TTFT 1755ms."
   -> KEPT (different operating point of the same sweep; results coexist).
3. New: "On 07-04 grid counts: done=36, running=2, pending=24."
   Old: "Request-count methodology (CURRENT): per point reqs = min(...)."
   -> KEPT (instance snapshot vs. method note; different slot kinds).

Output JSON only:
{"judgments":[{"id":"<id>","verdict":"SUPERSEDED|DUPLICATE|KEPT",
"confidence":0.0,"reason":"..."}]}
"""


def build_user_message(
    new_entry: dict[str, Any],
    candidates: list[dict[str, Any]],
) -> str:
    """new_entry/candidates: dicts with 'id', 'text', optional 'date'."""
    lines = [
        "## New entry (written {})".format(new_entry.get("date") or "unknown date"),
        new_entry.get("text", ""),
        "",
        "## Existing entries",
        json.dumps(
            [
                {
                    "id": str(c["id"]),
                    "date": c.get("date") or "unknown",
                    "text": c.get("text", ""),
                }
                for c in candidates
            ],
            ensure_ascii=False,
        ),
    ]
    return "\n".join(lines)


def parse_judgments(
    response: str | None,
    allowed_ids: set[str],
) -> list[dict[str, Any]]:
    """Parse and validate the judge's JSON; drop hallucinated ids, clamp fields.

    Tolerates truncated output (max_tokens cut-offs) by salvaging every
    complete judgment object — a partial batch is still useful evidence and
    the missed candidates stay unjudged (re-judgeable later).
    """
    if not response:
        raise ValueError("judge returned an empty response")
    text = response.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n|```$", "", text).strip()
    raw: Any = None
    try:
        data = json.loads(text, strict=False)
        raw = data.get("judgments") if isinstance(data, dict) else None
    except json.JSONDecodeError:
        # Salvage complete objects from truncated/malformed output.
        raw = [
            obj
            for chunk in re.findall(r"\{[^{}]*?\"verdict\"[^{}]*?\}", text, flags=re.DOTALL)
            for obj in [_try_json(chunk)]
            if obj is not None
        ]
        if not raw:
            raise ValueError(f"judge returned non-JSON output: {text[:200]}")
    if not isinstance(raw, list):
        raise ValueError("judge output missing 'judgments' list")

    judgments: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        mem_id = str(item.get("id", ""))
        verdict = str(item.get("verdict", "")).upper()
        if mem_id not in allowed_ids or verdict not in VERDICTS:
            continue
        try:
            confidence = max(0.0, min(1.0, float(item.get("confidence", 0.0))))
        except (TypeError, ValueError):
            confidence = 0.0
        judgments.append(
            {
                "id": mem_id,
                "verdict": verdict,
                "confidence": confidence,
                "reason": str(item.get("reason") or "")[:500],
            }
        )
    return judgments


def _try_json(chunk: str) -> Any:
    try:
        return json.loads(chunk, strict=False)
    except json.JSONDecodeError:
        return None


def judge(
    llm: Any,
    new_entry: dict[str, Any],
    candidates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """One batched LLM call: the new entry vs up-to-K candidates."""
    if not candidates:
        return []
    response = llm.generate_response(
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": build_user_message(new_entry, candidates)},
        ],
        response_format={"type": "json_object"},
    )
    return parse_judgments(response, {str(c["id"]) for c in candidates})


# ---------------------------------------------------------------------------
# Necessity judge (lifecycle design R1)
# ---------------------------------------------------------------------------

NECESSITY_VERDICTS = {"DURABLE", "BORN_UNNECESSARY", "EXPIRING"}

# Prompt "v5" — two flag verdicts matching the two real disposition
# families; the fine-grained recognition patterns from v4 live on as
# bullets under each verdict. Adds the pointer/constraint protection
# rules distilled from the 2026-07-21 backlog-scan review (14 wrong-
# direction cases, all one family). Gated by tools/necessity_judge_eval.py.
NECESSITY_PROMPT = """\
You are a memory-necessity judge for an engineering memory store. Entries
are dated snapshots written by coding agents during infrastructure work.
Given ONE entry, decide whether it deserves long-term memory, or belongs
to a category that should be flagged for review.

Verdicts:
- DURABLE: worth keeping. THE DEFAULT — use whenever uncertain.
- BORN_UNNECESSARY: the entry never deserved long-term memory. Patterns:
  * activity narration: it narrates that an agent performed or should
    perform routine actions ("agent read X then searched Y", "deleted
    directory as requested", "updated file at 11:39", "A recommended B
    record their findings") with no reusable technical fact beyond the
    action itself;
  * commit restatement: its central content is what a git commit/PR
    contains, changed, or its metadata (hash, title, author,
    timestamps), or that changes are now committed/pushed — git
    log/show already stores this, and attached context like "tests
    passed" does not rescue it (review rewrites to keep such
    fragments); only when the commit reference is incidental to a
    finding that stands on its own ("root cause was X; fixed in commit
    Y") is the entry DURABLE;
  * repo-readable fact: it restates what an agent could get by simply
    opening a named checked-in file (a doc's content, a config default,
    what a script does); phrasing it as a "mechanism" or "design
    decision" does not rescue it if the file says the same thing.
- EXPIRING: legitimate to record, but its usefulness decays with an
  ongoing process or event; it should expire when that closes. Patterns:
  * progress tick: the IN-FLIGHT state of a still-running process —
    percent complete, counts still changing (done/running/pending),
    current load/traffic over a window, "launched with PID N", mid-run
    metrics at step N — and observations of live OPERATIONAL state
    (queue depth, which hosts are idle/busy, service health at a
    moment) even when the probe itself completed: the observed state
    churns on its own;
  * event-scoped coordination: open dependencies and handoff state —
    "pending", "blocked until", "awaiting", "not yet done", "next gate
    is", "requires X before Y", "machines held/locked for the run",
    staging/preparation for an upcoming batch.

Hard rules — these override the patterns above. A flag verdict is only
allowed when NO hard rule applies; when an entry mixes a flag pattern
with any hard-rule payload, the hard rule wins:
1. Judge by RE-ACQUISITION COST, not theoretical derivability. A
   conclusion distilled from hours of auditing, debugging, instrumented
   experiments, or sweeps is DURABLE even if re-derivable from code
   ("audited the call chain and proved X", "mechanism investigation
   found Y").
2. Corrections, retractions, invalidation-of-evidence records ("those
   runs are void because X"), and hard-won lessons are ALWAYS DURABLE:
   entries marking earlier conclusions wrong/superseded/withdrawn, and
   learned-the-hard-way rules (footguns, gotchas, mandatory
   safeguards). They prevent future agents from repeating mistakes.
3. COMPLETED measurements/comparisons with their configuration — even
   partial sub-results carrying substantive numbers — decisions/rules
   with rationale, root-cause findings, and external-world facts
   (hosts, auth quirks, hashes) are DURABLE.
4. An entry is DURABLE when its payload is the canonical location of a
   LASTING asset — where a credential, key, or token lives; a
   repository, endpoint, or community-post URL; an archive or delivery
   path; which executor/host a procedure must run on — even when most
   of the entry narrates routine actions around that location. This
   does NOT cover working files of an in-flight investigation (logs,
   draft responses, scratch scripts): those follow the patterns above.
5. Standing constraints and authorizations that BIND future behavior
   are DURABLE while in force: resource whitelists, scope boundaries,
   user-set rules — even when stated during one campaign. A wait is
   not a constraint: "machines must be freed first", "requires X
   before proceeding", "closure needs Y" are EXPIRING coordination
   state. Closure/handoff summaries stating a final outcome
   ("verification closed, results at X", "current baseline is Y") are
   DURABLE conclusions.
6. When uncertain between a flag verdict and DURABLE, choose DURABLE.

Examples of the required judgment style:
1. "Attempted to clone repo X: the HTTPS URL <url> failed 504 behind
   the corporate proxy; after NO_PROXY=<host>, the ssh:2222 URL
   worked." -> DURABLE (canonical repo URLs + proxy footgun, rules
   4+2), despite the attempt narration.
2. "Pressure tests so far: 65 clean probes, 0 corruption, spontaneous
   repro rate ~0. Awaiting the eviction run (N=400)." -> DURABLE
   (completed sub-result with substantive numbers, rule 3), despite
   the awaiting tail.
3. "User confirmed the current baseline is run X step 3300 (72.5k
   rows, accept_len 4.1)." -> DURABLE (final-outcome statement,
   rule 5).
4. "Grid config X at 31.8% progress, TPS=1.50, scheduler PID alive."
   -> EXPIRING (progress tick).
5. "Committed and pushed commit 080f2a5 titled 'Support scoped
   search', adding search/list filters." -> BORN_UNNECESSARY (commit
   restatement).
6. "After creating the shareable key /path/login_<host>_ed25519 for
   root@<host>, deleted the temporary key files, kept only the
   shareable key, re-verified login." -> DURABLE (credential
   location, rule 4), despite the cleanup narration dominating the
   text.

confidence: probability a human reviewer confirms your verdict.
reason: ONE short sentence naming the matched pattern. Never quote long
fragments of the entry.

Output JSON only:
{"verdict":"...","confidence":0.0,"reason":"..."}
"""


def parse_single_judgment(
    response: str | None,
    allowed_verdicts: set[str],
    *,
    default_verdict: str,
) -> dict[str, Any]:
    """Parse a one-entry judgment; unknown verdicts fall back to the default."""
    if not response:
        raise ValueError("judge returned an empty response")
    text = response.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n|```$", "", text).strip()
    try:
        data = json.loads(text, strict=False)
    except json.JSONDecodeError:
        m = re.search(r'"verdict"\s*:\s*"([A-Za-z_]+)"', text)
        if not m:
            raise ValueError(f"judge returned non-JSON output: {text[:200]}") from None
        data = {"verdict": m.group(1), "confidence": 0.0, "reason": "<truncated output>"}
    verdict = str(data.get("verdict", "")).upper()
    if verdict not in allowed_verdicts:
        verdict = default_verdict
    try:
        confidence = max(0.0, min(1.0, float(data.get("confidence", 0.0))))
    except (TypeError, ValueError):
        confidence = 0.0
    return {
        "verdict": verdict,
        "confidence": confidence,
        "reason": str(data.get("reason") or "")[:500],
    }


def judge_necessity(llm: Any, entry: dict[str, Any]) -> dict[str, Any]:
    """Judge whether one entry deserves long-term memory (advisory only)."""
    user = "## Entry (written {})\n{}".format(
        entry.get("date") or "unknown date", entry.get("text", "")
    )
    response = llm.generate_response(
        messages=[
            {"role": "system", "content": NECESSITY_PROMPT},
            {"role": "user", "content": user},
        ],
        response_format={"type": "json_object"},
    )
    return parse_single_judgment(
        response, NECESSITY_VERDICTS, default_verdict="DURABLE"
    )


# ---------------------------------------------------------------------------
# Timestamp / attribution mismatch judge (lifecycle design R2)
# ---------------------------------------------------------------------------

# Correctness judge — timestamp/attribution mismatches and non-English
# narrative. The `correctness` suspicion kind; verdict values are kept stable
# for stored-row compatibility (LANGUAGE_SUSPECT added 2026-07-21).
CORRECTNESS_VERDICTS = {
    "CONSISTENT", "TIMESTAMP_SUSPECT", "ATTRIBUTION_SUSPECT", "LANGUAGE_SUSPECT",
}

CORRECTNESS_PROMPT = """\
You check one engineering memory entry for three defects: timestamp mismatch,
actor misattribution, and non-English narrative. The CLI stamps authoritative
metadata; compare the entry against it and against the store's writing rules.

You are given: the entry text, the authoritative ingestion time, the recorded
creation time, and the writer identity. ALL times given to you are already in
the workspace timezone Asia/Singapore (SGT, UTC+8); entry text dates are also
written in SGT. Compare same-timezone dates only — do not reintroduce any UTC
offset.

Output exactly one verdict. CONSISTENT is the default — only flag an
unmistakable case.
- CONSISTENT: no clear defect.
- TIMESTAMP_SUSPECT: the text narrates a CURRENT or just-completed event
  (present/just-finished tense) under a date that contradicts the ingestion
  time (both in SGT) by more than roughly one day. NOT suspect, all CONSISTENT:
  narrating a historical event with its own past date; a same-day or one-day
  boundary difference; and status/revision markers written when the entry was
  created OR later updated in place — phrases like "As of <date>", "RESOLVED
  <date>", "REVISED <date>", "updated <date>", "(updated <date>)". Such markers
  legitimately carry a date at or slightly after the original ingestion time
  because the entry was edited later; never flag them.
- ATTRIBUTION_SUSPECT: the text attributes an action to an actor in clear
  contradiction with the writer identity and context. Agents legitimately
  record the user's decisions and other agents' actions — flag only when
  the attribution is plainly impossible or reversed.
- LANGUAGE_SUSPECT: the ENTRY'S OWN NARRATION — the author's connecting
  sentences — is written in Chinese instead of English. The local store embeds
  with an English-only model, so Chinese prose retrieves poorly; entries must
  be AUTHORED in English. Decide by the language the author writes IN, not by
  whether any Chinese characters are present. Stay CONSISTENT when the narration
  is English even if it also contains:
    * preserved technical identifiers — paths, shell commands, env var names,
      host names, model/artifact names, error strings;
    * VERBATIM QUOTES of Chinese source material — a user instruction, a
      broadcast notice, a channel/group name, a document heading — kept inside
      quotes or parentheses. Quoting the original Chinese is CORRECT and does
      not make the entry Chinese; a single Chinese gloss word likewise does not.
  Flag ONLY when the author's own sentences (outside quotes and identifiers)
  are Chinese.

  LANGUAGE examples (decide by the author's own sentences):
    * "On 2026-07-15 Codex corrected the attribution under the user instruction
      '清理这些机器上妨碍拉起服务的进程'." -> CONSISTENT (English narration; the
      Chinese is a verbatim quote).
    * "superpod_30 hosts are from the bare-metal pool (裸机资源分配群); NPU-idle
      does not mean free." -> CONSISTENT (English narration; Chinese gloss).
    * "The designated files for agent operations (运维) are AGENTS.md and
      KLAUD_DEBUG.md." -> CONSISTENT (English sentence, one Chinese word).
    * "2026-07-20 训练已到 step652，loss=0.62，容器 running，accept_len 2.68。"
      -> LANGUAGE_SUSPECT (the narration itself is Chinese).
    * "已按用户要求在 remote-inventory.md 新增 Host Lease Utility 章节。"
      -> LANGUAGE_SUSPECT (Chinese narration around English identifiers).

If more than one defect applies, prefer LANGUAGE_SUSPECT (rewriting the entry
in English is the fix and subsumes the rest).

confidence: probability a human reviewer confirms the flagged defect.
reason: one short sentence naming the defect (or "consistent").

Output JSON only:
{"verdict":"...","confidence":0.0,"reason":"..."}
"""


# ---------------------------------------------------------------------------
# Safety judge — plaintext-credential audit (workspace rule: entries must
# reference a credential's location, never contain its value)
# ---------------------------------------------------------------------------

SAFETY_VERDICTS = {"CLEAN", "SECRET_SUSPECT"}

SAFETY_PROMPT = """\
You audit one engineering memory entry for embedded live credentials.
Store rule: entries must NEVER contain plaintext passwords, API keys,
tokens, private keys, or one-time authorization values — they must
reference the credential's file or secret location instead.

Verdicts:
- CLEAN: no credential value present. THE DEFAULT.
- SECRET_SUSPECT: the entry embeds what looks like an actual credential
  VALUE: a password string, a bearer/API token (sk-..., ghp_..., long
  random strings in an auth context), private key material, a
  connection string with an inline password, a one-time code.

NOT violations (all CLEAN):
- pointers to credential locations: file paths ("token stored in
  utils/modelscope-token.env"), env var NAMES, "password in hosts.yaml";
- public identifiers: hostnames, IPs, ports, usernames, repo/endpoint
  URLs without embedded credentials;
- content hashes (SHA256 of artifacts), commit hashes, UUIDs, PIDs;
- placeholder/example values: <token>, xxx, ****, "your-key-here".

CRITICAL OUTPUT RULE: never reproduce the suspected secret (or any part
of it) in your reason. Describe only its TYPE and approximate position,
e.g. "password value appears after 'rejects password'".

confidence: probability a human reviewer confirms the finding.
reason: one short sentence (type + position; never the value).

Output JSON only:
{"verdict":"...","confidence":0.0,"reason":"..."}
"""


def judge_safety(llm: Any, entry: dict[str, Any]) -> dict[str, Any]:
    """Flag entries that embed plaintext credential values (advisory only)."""
    response = llm.generate_response(
        messages=[
            {"role": "system", "content": SAFETY_PROMPT},
            {"role": "user", "content": f"## Entry text\n{entry.get('text', '')}"},
        ],
        response_format={"type": "json_object"},
    )
    return parse_single_judgment(response, SAFETY_VERDICTS, default_verdict="CLEAN")


_SGT = timezone(timedelta(hours=8))


def _to_sgt(iso: str | None) -> str | None:
    """Render a UTC/ISO timestamp in Asia/Singapore (UTC+8), so the judge
    compares SGT text dates against an SGT clock (workspace convention)."""
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return str(iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_SGT).strftime("%Y-%m-%d %H:%M:%S SGT")


def judge_correctness(
    llm: Any,
    entry: dict[str, Any],
    *,
    ingested_at: str,
    created_at: str | None,
    writer: str | None,
) -> dict[str, Any]:
    """Flag timestamp/attribution mismatches and non-English narrative (advisory only)."""
    user = (
        f"## Authoritative metadata\n"
        f"ingested_at (SGT, UTC+8): {_to_sgt(ingested_at)}\n"
        f"created_at (SGT): {_to_sgt(created_at) or 'same as ingested_at'}\n"
        f"writer identity: {writer or 'unknown'}\n\n"
        f"## Entry text\n{entry.get('text', '')}"
    )
    response = llm.generate_response(
        messages=[
            {"role": "system", "content": CORRECTNESS_PROMPT},
            {"role": "user", "content": user},
        ],
        response_format={"type": "json_object"},
    )
    return parse_single_judgment(
        response, CORRECTNESS_VERDICTS, default_verdict="CONSISTENT"
    )
