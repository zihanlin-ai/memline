"""Long-lived local daemon for mem0-local.

The CLI is intentionally still usable without this daemon.  When the daemon is
running, commands can reuse one initialized Mem0 client instead of paying the
FastEmbed/ONNX cold-start cost for every command.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import time
import traceback
from pathlib import Path
from typing import Any

from mem0_local.config import (
    COLLECTION,
    EMBEDDING_DIMS,
    EMBEDDING_MODEL,
    EMBEDDING_PROVIDER,
    ENV_FILE,
    FASTEMBED_CACHE,
    HISTORY_DB,
    LLM_APP_NAME,
    LLM_BASE_URL,
    LLM_MODEL,
    LLM_PROVIDER,
    LLM_SITE_URL,
    LOCK_FILE,
    MEM0_DIR,
    MEM0_HOME,
    QDRANT_DIR,
    STORE_DIR,
)

SOCKET_PATH = STORE_DIR / "daemon.sock"
PID_PATH = STORE_DIR / "daemon.pid"
LOG_PATH = STORE_DIR / "daemon.log"
REQUEST_TIMEOUT_SECONDS = 300
CONNECT_TIMEOUT_SECONDS = 1

_lock_handle = None


class DaemonUnavailable(RuntimeError):
    """Raised when the local daemon is not accepting requests."""


def setup_env() -> None:
    for path in (QDRANT_DIR, MEM0_DIR, MEM0_HOME, FASTEMBED_CACHE):
        path.mkdir(parents=True, exist_ok=True)

    os.environ.setdefault("HOME", str(MEM0_HOME))
    os.environ.setdefault("MEM0_DIR", str(MEM0_DIR))
    os.environ.setdefault("FASTEMBED_CACHE_PATH", str(FASTEMBED_CACHE))
    os.environ.setdefault("MEM0_TELEMETRY", "False")

    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(ENV_FILE)


def acquire_cli_lock() -> None:
    global _lock_handle
    if _lock_handle is not None:
        return
    STORE_DIR.mkdir(parents=True, exist_ok=True)
    _lock_handle = LOCK_FILE.open("a+")
    try:
        import fcntl

        fcntl.flock(_lock_handle.fileno(), fcntl.LOCK_EX)
    except ImportError:
        return


def build_config() -> dict[str, Any]:
    openrouter_llm = {
        "provider": "openai",
        "config": {
            "model": LLM_MODEL,
            "openrouter_base_url": LLM_BASE_URL,
            "site_url": LLM_SITE_URL,
            "app_name": LLM_APP_NAME,
            "temperature": 0.0,
            "max_tokens": 2000,
            "top_p": 0.1,
            "is_reasoning_model": False,
        },
    }

    return {
        "vector_store": {
            "provider": "qdrant",
            "config": {
                "collection_name": COLLECTION,
                "path": str(QDRANT_DIR),
                "embedding_model_dims": EMBEDDING_DIMS,
                "on_disk": True,
            },
        },
        "embedder": {
            "provider": EMBEDDING_PROVIDER,
            "config": {
                "model": EMBEDDING_MODEL,
                "embedding_dims": EMBEDDING_DIMS,
            },
        },
        "llm": openrouter_llm,
        "reranker": {
            "provider": "llm_reranker",
            "config": {
                "top_k": 8,
                "temperature": 0.0,
                "max_tokens": 100,
                "llm": openrouter_llm,
            },
        },
        "history_db_path": str(HISTORY_DB),
    }


def memory_client():
    setup_env()
    acquire_cli_lock()
    from mem0 import Memory

    return Memory.from_config(build_config())


def normalize_items(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        for key in ("results", "memories"):
            value = data.get(key)
            if isinstance(value, list):
                return [x for x in value if isinstance(x, dict)]
    return []


def handle_request(client: Any, request: dict[str, Any]) -> dict[str, Any]:
    op = request.get("op")
    args = request.get("args") or {}
    started = time.perf_counter()

    if op == "ping":
        result: Any = {"pid": os.getpid(), "socket": str(SOCKET_PATH)}
    elif op == "get":
        result = client.get(args["memory_id"])
    elif op == "search":
        result = client.search(
            args["query"],
            top_k=args["top_k"],
            filters=args["filters"],
            threshold=args["threshold"],
            rerank=args["rerank"],
            explain=args["explain"],
        )
    elif op == "list":
        raw = client.get_all(filters=args["filters"], top_k=args["top_k"])
        items = normalize_items(raw)
        result = items[args["start"] : args["end"]]
    elif op == "add":
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
    elif op == "update":
        result = client.update(args["memory_id"], args["text"], metadata=args["metadata"])
    elif op == "delete":
        if args.get("all"):
            result = client.delete_all(
                user_id=args["user_id"],
                agent_id=args.get("agent_id"),
                run_id=args.get("run_id"),
            )
        else:
            result = client.delete(args["memory_id"])
    elif op == "history":
        result = client.history(args["memory_id"])
    else:
        raise ValueError(f"Unsupported daemon op: {op}")

    return {"status": "ok", "result": result}


def read_json_line(conn: socket.socket) -> dict[str, Any]:
    chunks: list[bytes] = []
    while True:
        chunk = conn.recv(65536)
        if not chunk:
            break
        chunks.append(chunk)
        if b"\n" in chunk:
            break
    raw = b"".join(chunks).split(b"\n", 1)[0]
    if not raw:
        raise ValueError("empty request")
    return json.loads(raw.decode())


def write_json_line(conn: socket.socket, payload: dict[str, Any]) -> None:
    conn.sendall(json.dumps(payload, default=str).encode() + b"\n")


def serve() -> None:
    STORE_DIR.mkdir(parents=True, exist_ok=True)
    if SOCKET_PATH.exists():
        SOCKET_PATH.unlink()
    client = memory_client()
    PID_PATH.write_text(str(os.getpid()))

    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(SOCKET_PATH))
    SOCKET_PATH.chmod(0o600)
    server.listen(16)

    def shutdown(_signum: int, _frame: Any) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    try:
        while True:
            conn, _ = server.accept()
            with conn:
                try:
                    request = read_json_line(conn)
                    response = handle_request(client, request)
                except Exception as exc:  # noqa: BLE001 - daemon must return errors.
                    response = {
                        "status": "error",
                        "error": str(exc),
                        "traceback": traceback.format_exc(),
                    }
                write_json_line(conn, response)
    except KeyboardInterrupt:
        pass
    finally:
        server.close()
        for path in (SOCKET_PATH, PID_PATH):
            try:
                path.unlink()
            except FileNotFoundError:
                pass


def request(payload: dict[str, Any], *, timeout: float = REQUEST_TIMEOUT_SECONDS) -> Any:
    if not SOCKET_PATH.exists():
        raise DaemonUnavailable("daemon socket does not exist")
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(timeout)
            client.connect(str(SOCKET_PATH))
            write_json_line(client, payload)
            response = read_json_line(client)
    except OSError as exc:
        raise DaemonUnavailable(str(exc)) from exc

    if response.get("status") != "ok":
        raise RuntimeError(response.get("error") or "daemon request failed")
    return response.get("result")


def is_pid_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def read_pid() -> int | None:
    try:
        return int(PID_PATH.read_text().strip())
    except (FileNotFoundError, ValueError):
        return None


def ping() -> dict[str, Any] | None:
    try:
        return request({"op": "ping"}, timeout=CONNECT_TIMEOUT_SECONDS)
    except Exception:
        return None


def start_daemon(wait_seconds: float = 90.0) -> dict[str, Any]:
    existing = ping()
    if existing:
        return {"started": False, **existing}

    STORE_DIR.mkdir(parents=True, exist_ok=True)
    log = LOG_PATH.open("ab")
    subprocess.Popen(
        [sys.executable, "-m", "mem0_local.daemon", "--serve"],
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=log,
        start_new_session=True,
        env=os.environ.copy(),
    )

    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        current = ping()
        if current:
            return {"started": True, **current}
        time.sleep(0.5)
    raise TimeoutError(f"daemon did not become ready within {wait_seconds:.0f}s; see {LOG_PATH}")


def stop_daemon(wait_seconds: float = 10.0) -> dict[str, Any]:
    pid = read_pid()
    if pid is None:
        if SOCKET_PATH.exists():
            SOCKET_PATH.unlink()
        return {"stopped": False, "reason": "pid file missing"}

    if not is_pid_running(pid):
        for path in (SOCKET_PATH, PID_PATH):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        return {"stopped": False, "pid": pid, "reason": "process was not running"}

    os.kill(pid, signal.SIGTERM)
    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        if not is_pid_running(pid):
            break
        time.sleep(0.2)
    stopped = not is_pid_running(pid)
    if stopped:
        for path in (SOCKET_PATH, PID_PATH):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
    return {"stopped": stopped, "pid": pid}


def status() -> dict[str, Any]:
    pid = read_pid()
    pong = ping()
    return {
        "running": bool(pong),
        "pid": pid,
        "pid_running": is_pid_running(pid) if pid is not None else False,
        "socket_path": str(SOCKET_PATH),
        "socket_exists": SOCKET_PATH.exists(),
        "log_path": str(LOG_PATH),
        "ping": pong,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--serve", action="store_true")
    args = parser.parse_args()
    if args.serve:
        serve()
        return
    print(json.dumps(status(), default=str))


if __name__ == "__main__":
    main()
