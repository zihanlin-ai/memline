"""Shared helpers for entity-graph CLI operations.

Used by both the daemon dispatch and the CLI direct path so the two stay
semantically identical.
"""

from __future__ import annotations

from typing import Any


def row_to_dict(row: Any) -> dict[str, Any]:
    payload = getattr(row, "payload", None) or {}
    return {
        "id": str(getattr(row, "id", "")),
        "entity_type": payload.get("entity_type"),
        "data": payload.get("data"),
        "linked_memory_ids": payload.get("linked_memory_ids") or [],
        "user_id": payload.get("user_id"),
    }


def list_entities(
    entity_store: Any,
    *,
    entity_type: str | None = None,
    contains: str | None = None,
    scan_limit: int = 50000,
) -> list[dict[str, Any]]:
    """List entity-graph rows, most-linked first.

    ``contains`` is matched client-side (case-insensitive substring) because
    the entity payload has no full-text index; ``scan_limit`` bounds how many
    rows are fetched before filtering.
    """
    from mem0.memory.main import _vector_store_list_rows

    filters = {"entity_type": entity_type} if entity_type else None
    listed = entity_store.list(filters=filters, top_k=scan_limit)

    rows: list[dict[str, Any]] = []
    needle = contains.lower() if contains else None
    for row in _vector_store_list_rows(listed):
        item = row_to_dict(row)
        if not item["data"]:
            continue
        if needle and needle not in str(item["data"]).lower():
            continue
        rows.append(item)

    rows.sort(key=lambda r: (-len(r["linked_memory_ids"]), str(r["data"])))
    return rows
