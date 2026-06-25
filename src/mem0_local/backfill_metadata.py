#!/usr/bin/env python3
"""Backfill normalized metadata/scope fields for existing local memories."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from qdrant_client import QdrantClient

from mem0_local import cli as mem0_memory


MANIFEST_DIR = mem0_memory.MEMORY_ROOT / "manifests"


def iter_points(client: QdrantClient):
    offset = None
    while True:
        points, offset = client.scroll(
            collection_name=mem0_memory.COLLECTION,
            limit=256,
            offset=offset,
            with_payload=True,
            with_vectors=False,
        )
        for point in points:
            yield point
        if offset is None:
            break


def classify_origin(payload: dict[str, Any]) -> str:
    if payload.get("source") == "agent-memory-ledger" or payload.get("ledger_file"):
        return "ledger_import"
    if payload.get("run_id") or payload.get("session_id"):
        return "live_agent"
    return "legacy_live_agent"


def default_agent_id(payload: dict[str, Any]) -> str:
    return (
        str(payload.get("agent_id") or "").strip()
        or str(payload.get("source") or "").strip()
        or "legacy-unknown"
    )


def default_session_id(payload: dict[str, Any], origin: str, agent_id: str) -> str:
    existing = str(payload.get("session_id") or payload.get("run_id") or "").strip()
    if existing:
        return existing
    if origin == "ledger_import":
        import_batch = str(payload.get("import_batch") or "").strip()
        if import_batch:
            return import_batch
        ledger_month = str(payload.get("ledger_month") or "").strip()
        if ledger_month:
            return f"ledger-{ledger_month}"
        return "legacy-ledger-import"
    return f"legacy-{agent_id}"


def desired_patch(payload: dict[str, Any], *, backfilled_at: str) -> dict[str, Any]:
    origin = str(payload.get("origin") or "").strip() or classify_origin(payload)
    agent_id = default_agent_id(payload)
    session_id = default_session_id(payload, origin, agent_id)

    desired = {
        "agent_id": agent_id,
        "run_id": str(payload.get("run_id") or "").strip() or session_id,
        "session_id": session_id,
        "writer_agent_id": str(payload.get("writer_agent_id") or "").strip() or agent_id,
        "origin": origin,
        "memory_schema_version": payload.get("memory_schema_version") or mem0_memory.MEMORY_SCHEMA_VERSION,
        "metadata_backfilled_at": payload.get("metadata_backfilled_at") or backfilled_at,
    }
    return {key: value for key, value in desired.items() if payload.get(key) != value}


def project_fields(payload: dict[str, Any]) -> dict[str, Any]:
    keys = [
        "agent_id",
        "run_id",
        "source",
        "session_id",
        "writer_agent_id",
        "origin",
        "memory_schema_version",
        "metadata_backfilled_at",
        "ledger_month",
        "import_batch",
        "ledger_file",
        "ledger_id",
    ]
    return {key: payload.get(key) for key in keys if key in payload}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Only report planned changes")
    parser.add_argument(
        "--manifest",
        type=Path,
        default=None,
        help="JSONL audit manifest path; defaults under .agent-memory/manifests",
    )
    args = parser.parse_args()

    mem0_memory.setup_env()
    mem0_memory.acquire_cli_lock()
    client = QdrantClient(path=str(mem0_memory.QDRANT_DIR))

    backfilled_at = datetime.now(timezone.utc).isoformat()
    manifest = args.manifest
    if manifest is None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        manifest = MANIFEST_DIR / f"metadata-backfill-{stamp}.jsonl"
    if not manifest.is_absolute():
        manifest = mem0_memory.ROOT / manifest

    total = 0
    changed = 0
    changes_by_field: dict[str, int] = {}
    rows: list[dict[str, Any]] = []

    for point in iter_points(client):
        total += 1
        payload = dict(point.payload or {})
        patch = desired_patch(payload, backfilled_at=backfilled_at)
        if not patch:
            continue
        changed += 1
        for key in patch:
            changes_by_field[key] = changes_by_field.get(key, 0) + 1
        after = {**payload, **patch}
        rows.append(
            {
                "memory_id": point.id,
                "changed_fields": sorted(patch),
                "before": project_fields(payload),
                "patch": patch,
                "after": project_fields(after),
            }
        )
        if not args.dry_run:
            client.set_payload(
                collection_name=mem0_memory.COLLECTION,
                payload=patch,
                points=[point.id],
                wait=True,
            )

    if rows and not args.dry_run:
        manifest.parent.mkdir(parents=True, exist_ok=True)
        with manifest.open("w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    print(
        json.dumps(
            {
                "dry_run": args.dry_run,
                "total": total,
                "changed": changed,
                "changes_by_field": changes_by_field,
                "manifest": str(manifest) if (rows and not args.dry_run) else None,
                "sample": rows[:5],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
