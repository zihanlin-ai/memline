from memline.backfill_metadata import desired_patch
from memline.config import LEDGER_IMPORT_AGENT_ID, LEDGER_IMPORT_SESSION_ID


def test_ledger_backfill_uses_synthetic_opencode_scope() -> None:
    patch = desired_patch(
        {"source": "agent-memory-ledger", "origin": "ledger_import"},
        backfilled_at="2026-07-30T00:00:00+00:00",
    )

    assert LEDGER_IMPORT_AGENT_ID == "opencode"
    assert LEDGER_IMPORT_SESSION_ID.startswith("ses_")
    assert patch["agent_id"] == LEDGER_IMPORT_AGENT_ID
    assert patch["writer_agent_id"] == LEDGER_IMPORT_AGENT_ID
    assert patch["run_id"] == LEDGER_IMPORT_SESSION_ID
    assert patch["session_id"] == LEDGER_IMPORT_SESSION_ID


def test_live_backfill_keeps_existing_writer_scope() -> None:
    patch = desired_patch(
        {"origin": "live_agent", "agent_id": "codex", "run_id": "session-1"},
        backfilled_at="2026-07-30T00:00:00+00:00",
    )

    assert patch["writer_agent_id"] == "codex"
    assert patch["session_id"] == "session-1"
