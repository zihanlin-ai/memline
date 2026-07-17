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
