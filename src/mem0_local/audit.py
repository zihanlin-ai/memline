"""Append-only JSONL audit manifests for live memory mutations."""

from __future__ import annotations

import hashlib
import json
import sys
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from mem0_local.config import MANIFEST_DIR, MANIFEST_LOCK


LIVE_AUDIT_SCHEMA_VERSION = 1


def _json_default(value: Any) -> str:
    if isinstance(value, Path):
        return str(value)
    return str(value)


def _memory_results(result: Any) -> list[dict[str, Any]]:
    if not isinstance(result, dict):
        return []
    raw = result.get("results")
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


def _stable_payload_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=_json_default).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def live_manifest_path(timestamp_iso: str) -> Path:
    month = timestamp_iso[:7] if len(timestamp_iso) >= 7 else datetime.now(timezone.utc).strftime("%Y-%m")
    return MANIFEST_DIR / f"live-{month}.jsonl"


def append_live_audit(
    *,
    operation: str,
    input_payload: dict[str, Any],
    metadata: dict[str, Any] | None,
    result: Any,
    started_at: str,
    finished_at: str,
    duration_ms: int,
    scope: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Append one live mutation audit event and return compact write metadata."""

    memories = _memory_results(result)
    item: dict[str, Any] = {
        "schema_version": LIVE_AUDIT_SCHEMA_VERSION,
        "audit_id": str(uuid.uuid4()),
        "operation": operation,
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_ms": duration_ms,
        "input": input_payload,
        "metadata": metadata or {},
        "scope": scope or {},
        "memory_result": result,
        "result_count": len(memories),
        "events": [item.get("event") for item in memories if item.get("event")],
        "memory_ids": [item.get("id") for item in memories if item.get("id")],
        "result_memories": [item.get("memory") for item in memories if item.get("memory")],
    }
    item["payload_sha256"] = _stable_payload_hash(item)

    path = live_manifest_path(finished_at)
    path.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST_LOCK.parent.mkdir(parents=True, exist_ok=True)

    with MANIFEST_LOCK.open("a+", encoding="utf-8") as lock_fh:
        try:
            import fcntl

            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
        except ImportError:
            pass
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(item, ensure_ascii=False, sort_keys=True, default=_json_default) + "\n")

    _record_session_add(operation, metadata or {}, result, finished_at)

    return {"path": str(path), "audit_id": item["audit_id"], "payload_sha256": item["payload_sha256"]}


def _record_session_add(
    operation: str, metadata: dict[str, Any], result: Any, finished_at: str
) -> None:
    """Count successful live adds per writer session (handoff-pressure banner).

    Ledger imports replay historical sessions and are excluded; error outcomes
    did not accumulate a write and are excluded too.
    """
    if operation != "add":
        return
    if isinstance(result, dict) and result.get("error") is not None:
        return
    if metadata.get("origin") == "ledger_import":
        return
    session_id = metadata.get("session_id")
    if not session_id:
        return
    try:
        from mem0_local.session_stats import session_stats_store

        session_stats_store().record_add(str(session_id), finished_at)
    except Exception as exc:  # noqa: BLE001 - the counter must never break a write.
        print(f"session add counter failed: {exc}", file=sys.stderr)


@dataclass
class AuditSpan:
    """Mutable record the caller fills in while the audited block runs."""

    input_payload: dict[str, Any]
    metadata: dict[str, Any] | None = None
    scope: dict[str, Any] | None = None
    result: Any = None


@contextmanager
def audited(
    operation: str,
    *,
    input_payload: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
    scope: dict[str, Any] | None = None,
) -> Iterator[AuditSpan]:
    """Audit one mutation: set ``span.result`` inside the block.

    Success appends a manifest row with the result; an escaping exception
    appends ``{"error": ...}`` (unless the caller already recorded a result)
    and re-raises. On the failure path the manifest write itself is
    best-effort so it can never mask the original error.
    """
    span = AuditSpan(input_payload=dict(input_payload or {}), metadata=metadata, scope=scope)
    start = time.perf_counter()
    started_at = datetime.now(timezone.utc).isoformat()
    error: BaseException | None = None
    try:
        yield span
    except BaseException as exc:
        error = exc
        raise
    finally:
        result = span.result
        if error is not None and result is None:
            result = {"error": str(error)}
        try:
            append_live_audit(
                operation=operation,
                input_payload=span.input_payload,
                metadata=span.metadata,
                result=result,
                started_at=started_at,
                finished_at=datetime.now(timezone.utc).isoformat(),
                duration_ms=int((time.perf_counter() - start) * 1000),
                scope=span.scope or {},
            )
        except Exception as audit_exc:  # noqa: BLE001
            if error is None:
                raise
            print(f"audit append failed for {operation}: {audit_exc}", file=sys.stderr)
