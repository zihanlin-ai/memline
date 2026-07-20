"""Single definition of every memory-store operation.

Both execution paths run the same handlers: the daemon routes socket
requests to :func:`dispatch`, and the CLI's direct path (daemon not
running) calls the same :func:`dispatch` on a locally built client.
Per-op transport metadata — request timeout, whether the op occupies an
LLM slot, whether it needs exclusive store access — lives in the same
registry, so adding an op is one entry here instead of parallel edits in
the CLI and the daemon.

Queue-plane ops (async enqueue, event inspection) are not in this
registry: they never touch the memory store and are handled by the daemon
directly against its event queue. They appear only in
``QUEUE_OP_TIMEOUTS`` for their transport timeout.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any, Callable

from mem0_local.runtime import normalize_items

Handler = Callable[[Any, dict[str, Any]], Any]

DEFAULT_TIMEOUT_SECONDS = 300.0


@dataclass(frozen=True)
class OpSpec:
    handler: Handler
    timeout: float | Callable[[dict[str, Any]], float] = DEFAULT_TIMEOUT_SECONDS
    llm_bound: Callable[[dict[str, Any]], bool] | None = None
    exclusive: Callable[[dict[str, Any]], bool] | None = None


def _get(client: Any, args: dict[str, Any]) -> Any:
    return client.get(args["memory_id"])


def _search(client: Any, args: dict[str, Any]) -> Any:
    from mem0_local.staleness import search_with_staleness

    return search_with_staleness(
        client,
        query=args["query"],
        top_k=args["top_k"],
        filters=args["filters"],
        threshold=args["threshold"],
        rerank=args["rerank"],
        keyword=args.get("keyword", False),
        explain=args["explain"],
        include_superseded=args.get("include_superseded", False),
    )


def _list(client: Any, args: dict[str, Any]) -> Any:
    raw = client.get_all(filters=args["filters"], top_k=args["top_k"])
    return normalize_items(raw)[args["start"] : args["end"]]


def _add(client: Any, args: dict[str, Any]) -> Any:
    started = time.perf_counter()
    result = client.add(
        args["content"],
        user_id=args["user_id"],
        agent_id=args["agent_id"],
        run_id=args["run_id"],
        metadata=args["metadata"],
        infer=args["infer"],
    )
    if isinstance(result, dict):
        result.setdefault("duration_ms", int((time.perf_counter() - started) * 1000))
    return result


def _update(client: Any, args: dict[str, Any]) -> Any:
    return client.update(args["memory_id"], args["text"], metadata=args["metadata"])


def _delete(client: Any, args: dict[str, Any]) -> Any:
    if args.get("all"):
        return client.delete_all(
            user_id=args["user_id"],
            agent_id=args.get("agent_id"),
            run_id=args.get("run_id"),
        )
    return client.delete(args["memory_id"])


def _history(client: Any, args: dict[str, Any]) -> Any:
    return client.history(args["memory_id"])


def _invalidate(client: Any, args: dict[str, Any]) -> Any:
    from mem0_local.staleness import invalidate

    return invalidate(
        client,
        args["memory_id"],
        args["by_ids"],
        reason=args.get("reason"),
        actor_id=args.get("actor_id"),
        session_id=args.get("session_id"),
    )


def _revive(client: Any, args: dict[str, Any]) -> Any:
    from mem0_local.staleness import revive

    return revive(
        client,
        args["memory_id"],
        actor_id=args.get("actor_id"),
        session_id=args.get("session_id"),
    )


def _stale_pin(client: Any, args: dict[str, Any]) -> Any:
    client.vector_store.update(
        vector_id=args["memory_id"], vector=None, payload={"stale_check_pin": True}
    )
    return {"id": args["memory_id"], "pinned": True}


def _resolve_head(client: Any, args: dict[str, Any]) -> Any:
    from mem0_local.staleness import resolve_head

    def _payload(mid: str) -> dict[str, Any] | None:
        point = client.vector_store.get(mid)
        payload = getattr(point, "payload", None) if point is not None else None
        return dict(payload) if payload else ({} if point is not None else None)

    return resolve_head(_payload, args["memory_id"])


def _entity_list(client: Any, args: dict[str, Any]) -> Any:
    from mem0_local.entity_ops import list_entities

    rows = list_entities(
        client.entity_store,
        entity_type=args.get("entity_type"),
        contains=args.get("contains"),
        scan_limit=args.get("scan_limit", 50000),
    )
    return rows[args.get("start", 0) : args.get("end")]


def _entity_get(client: Any, args: dict[str, Any]) -> Any:
    from mem0_local.entity_ops import row_to_dict

    row = client.entity_store.get(args["entity_id"])
    return row_to_dict(row) if row else None


def _entity_delete(client: Any, args: dict[str, Any]) -> Any:
    client.entity_store.delete(args["entity_id"])
    return {"id": args["entity_id"], "deleted": True}


OPS: dict[str, OpSpec] = {
    "get": OpSpec(_get, timeout=30.0),
    "search": OpSpec(
        _search,
        timeout=lambda a: 180.0 if a.get("rerank") else 30.0,
        llm_bound=lambda a: bool(a.get("rerank")),
    ),
    "list": OpSpec(_list, timeout=30.0),
    "add": OpSpec(
        _add,
        timeout=lambda a: 300.0 if a.get("infer") else 30.0,
        llm_bound=lambda a: bool(a.get("infer", True)),
    ),
    "update": OpSpec(_update),
    "delete": OpSpec(_delete, timeout=30.0, exclusive=lambda a: bool(a.get("all"))),
    "history": OpSpec(_history, timeout=30.0),
    "invalidate": OpSpec(_invalidate, timeout=30.0),
    "revive": OpSpec(_revive, timeout=30.0),
    "stale_pin": OpSpec(_stale_pin, timeout=30.0),
    "resolve_head": OpSpec(_resolve_head, timeout=30.0),
    "entity_list": OpSpec(_entity_list, timeout=30.0),
    "entity_get": OpSpec(_entity_get, timeout=30.0),
    "entity_delete": OpSpec(_entity_delete, timeout=30.0),
}

QUEUE_OP_TIMEOUTS: dict[str, float] = {
    "event_list": 30.0,
    "event_get": 30.0,
    "event_retry": 30.0,
    "event_ack": 30.0,
}


def dispatch(client: Any, op: str, args: dict[str, Any]) -> Any:
    spec = OPS.get(op)
    if spec is None:
        raise ValueError(f"Unsupported daemon op: {op}")
    return spec.handler(client, args)


def op_timeout(op: str, args: dict[str, Any]) -> float:
    raw = os.environ.get("MEM0_LOCAL_DAEMON_TIMEOUT")
    if raw:
        try:
            return float(raw)
        except ValueError:
            pass
    spec = OPS.get(op)
    if spec is not None:
        return spec.timeout(args) if callable(spec.timeout) else spec.timeout
    return QUEUE_OP_TIMEOUTS.get(op, DEFAULT_TIMEOUT_SECONDS)


def is_llm_bound(op: str, args: dict[str, Any]) -> bool:
    spec = OPS.get(op)
    return bool(spec and spec.llm_bound and spec.llm_bound(args))


def is_exclusive(op: str, args: dict[str, Any]) -> bool:
    spec = OPS.get(op)
    return bool(spec and spec.exclusive and spec.exclusive(args))
