"""Long-lived local daemon for mem0-local.

The CLI is intentionally still usable without this daemon.  When the daemon is
running, commands can reuse one initialized Mem0 client instead of paying the
FastEmbed/ONNX cold-start cost for every command.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Any

from mem0_local.audit import append_live_audit
from mem0_local.queue import EventQueue
from mem0_local.config import (
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
    VECTOR_STORE_HOST,
    VECTOR_STORE_MODE,
    VECTOR_STORE_PORT,
    vector_store_config,
)

SOCKET_PATH = STORE_DIR / "daemon.sock"
PID_PATH = STORE_DIR / "daemon.pid"
LOG_PATH = STORE_DIR / "daemon.log"
REQUEST_TIMEOUT_SECONDS = 300
CONNECT_TIMEOUT_SECONDS = 1
CPU_SAMPLE_SECONDS = 0.05
# Workers are cheap (network-wait bound), so the pool is sized well above the
# LLM semaphore: adds waiting for an LLM slot occupy workers, and reads must
# still find a free one instead of queueing behind a write burst.
MAX_WORKERS = int(os.environ.get("MEM0_LOCAL_DAEMON_WORKERS", "32"))
LLM_CONCURRENCY = int(os.environ.get("MEM0_LOCAL_LLM_CONCURRENCY", "4"))
ASYNC_WORKERS = int(os.environ.get("MEM0_LOCAL_ASYNC_WORKERS", "2"))

_lock_handle = None
_lifetime_lock_handle = None
_event_queue: EventQueue | None = None


class _RWGate:
    """Shared/exclusive gate over the memory store.

    Normal operations (reads and writes alike) take the shared side and run
    concurrently; only ``delete --all`` takes the exclusive side so it cannot
    interleave with in-flight writes. Exclusive acquisition waits for current
    shared holders but does not block new ones from queueing behind it being
    rare; delete-all is an infrequent, human-triggered operation.
    """

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._shared = 0
        self._exclusive = False

    def acquire_shared(self) -> None:
        with self._cond:
            while self._exclusive:
                self._cond.wait()
            self._shared += 1

    def release_shared(self) -> None:
        with self._cond:
            self._shared -= 1
            if self._shared == 0:
                self._cond.notify_all()

    def acquire_exclusive(self) -> None:
        with self._cond:
            while self._exclusive or self._shared:
                self._cond.wait()
            self._exclusive = True

    def release_exclusive(self) -> None:
        with self._cond:
            self._exclusive = False
            self._cond.notify_all()


_store_gate = _RWGate()
# Caps concurrent OpenRouter-bound operations (add-with-infer, rerank search)
# so parallel agent writes do not trip provider rate limits.
_llm_slots = threading.BoundedSemaphore(LLM_CONCURRENCY)


def _serialize_spacy_inference() -> None:
    """Wrap shared-spaCy-singleton entry points with one inference lock.

    spaCy ``Language`` objects are not guaranteed safe for concurrent calls on
    the same instance. mem0 imports these functions by name into its own
    namespaces, so both the defining modules and ``mem0.memory.main`` must be
    patched. Calls are millisecond-scale, so one lock costs nothing.
    """
    lock = threading.Lock()

    def locked(fn):
        def wrapper(*args, **kwargs):
            with lock:
                return fn(*args, **kwargs)

        wrapper.__name__ = getattr(fn, "__name__", "spacy_call")
        return wrapper

    import mem0.memory.main as mem0_main
    import mem0.utils.entity_extraction as mem0_entities
    import mem0.utils.lemmatization as mem0_lemma

    for module, name in (
        (mem0_lemma, "lemmatize_for_bm25"),
        (mem0_entities, "extract_entities"),
        (mem0_entities, "extract_entities_batch"),
        (mem0_main, "lemmatize_for_bm25"),
        (mem0_main, "extract_entities"),
        (mem0_main, "extract_entities_batch"),
    ):
        fn = getattr(module, name, None)
        if callable(fn):
            setattr(module, name, locked(fn))


def _qdrant_reachable(timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((str(VECTOR_STORE_HOST), int(VECTOR_STORE_PORT)), timeout=timeout):
            return True
    except OSError:
        return False


def _ensure_qdrant() -> None:
    """Best-effort self-healing: bring the local qdrant server up if it is
    down (e.g. after a WSL restart). No-op in local-path mode or when the
    managed control script is absent."""
    if VECTOR_STORE_MODE != "qdrant-server" or _qdrant_reachable():
        return
    # A qdrant process that exists but is unreachable means we are in a
    # network-isolated context (e.g. a sandbox namespace). Starting a second
    # qdrant against the same storage directory is the one genuinely hazardous
    # outcome here — refuse to continue instead.
    try:
        if subprocess.run(["pgrep", "-x", "qdrant"], capture_output=True, timeout=5).returncode == 0:
            print(
                "qdrant process exists but is unreachable (network-isolated context?); refusing to start a duplicate",
                file=sys.stderr,
                flush=True,
            )
            sys.exit(1)
    except (subprocess.SubprocessError, OSError):
        pass
    ctl = STORE_DIR / "qdrant-server" / "qdrantctl.sh"
    if not ctl.exists():
        print(f"qdrant server {VECTOR_STORE_HOST}:{VECTOR_STORE_PORT} unreachable and {ctl} missing", file=sys.stderr, flush=True)
        return
    print("qdrant server down; starting via qdrantctl.sh ...", file=sys.stderr, flush=True)
    try:
        subprocess.run(["bash", str(ctl), "start"], check=True, timeout=60, capture_output=True)
    except (subprocess.SubprocessError, OSError) as exc:
        print(f"qdrant auto-start failed: {exc}", file=sys.stderr, flush=True)


def _prewarm(client: Any) -> None:
    """Eagerly initialize lazy singletons so worker threads never race on them
    and the first request does not pay their load cost."""
    try:
        client.entity_store
    except Exception as exc:  # noqa: BLE001 - prewarm is best-effort.
        print(f"prewarm: entity_store init failed: {exc}", file=sys.stderr, flush=True)
    try:
        from mem0.utils.spacy_models import get_nlp_full, get_nlp_lemma

        get_nlp_full()
        get_nlp_lemma()
    except Exception as exc:  # noqa: BLE001
        print(f"prewarm: spaCy load failed: {exc}", file=sys.stderr, flush=True)


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
        "vector_store": vector_store_config(),
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

    if op == "ping":
        return {"status": "ok", "result": {"pid": os.getpid(), "socket": str(SOCKET_PATH)}}

    # Queue-plane ops never touch the memory store: no gate, no LLM slot.
    if op == "add" and args.get("async"):
        if _event_queue is None:
            raise RuntimeError("event queue unavailable; use --wait for the synchronous path")
        payload = {k: v for k, v in args.items() if k != "async"}
        event_id = _event_queue.enqueue("add", payload)
        return {"status": "ok", "result": {"event_id": event_id, "status": "queued"}}
    if op in {"event_list", "event_get", "event_retry", "event_ack"}:
        queue = _event_queue or EventQueue()
        if op == "event_list":
            result: Any = queue.list(
                status=args.get("status"),
                limit=args.get("limit", 50),
                offset=args.get("offset", 0),
            )
        elif op == "event_get":
            result = queue.get(args["event_id"])
        elif op == "event_retry":
            result = {"event_id": args["event_id"], "retried": queue.retry(args["event_id"])}
            queue.refresh_alerts()
        else:
            result = {"acked": queue.ack(args.get("event_id"))}
            queue.refresh_alerts()
        return {"status": "ok", "result": result}

    exclusive = op == "delete" and bool(args.get("all"))
    llm_bound = (op == "add" and args.get("infer", True)) or (op == "search" and args.get("rerank"))

    if exclusive:
        _store_gate.acquire_exclusive()
    else:
        _store_gate.acquire_shared()
    try:
        if llm_bound:
            with _llm_slots:
                return _dispatch(client, op, args)
        return _dispatch(client, op, args)
    finally:
        if exclusive:
            _store_gate.release_exclusive()
        else:
            _store_gate.release_shared()


def _dispatch(client: Any, op: str, args: dict[str, Any]) -> dict[str, Any]:
    started = time.perf_counter()

    if op == "get":
        result = client.get(args["memory_id"])
    elif op == "search":
        result = client.search(
            args["query"],
            top_k=args["top_k"],
            filters=args["filters"],
            threshold=args["threshold"],
            rerank=args["rerank"],
            keyword=args.get("keyword", False),
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
    elif op == "entity_list":
        from mem0_local.entity_ops import list_entities

        rows = list_entities(
            client.entity_store,
            entity_type=args.get("entity_type"),
            contains=args.get("contains"),
            scan_limit=args.get("scan_limit", 50000),
        )
        result = rows[args.get("start", 0) : args.get("end")]
    elif op == "entity_get":
        from mem0_local.entity_ops import row_to_dict

        row = client.entity_store.get(args["entity_id"])
        result = row_to_dict(row) if row else None
    elif op == "entity_delete":
        client.entity_store.delete(args["entity_id"])
        result = {"id": args["entity_id"], "deleted": True}
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


def write_json_line(conn: socket.socket, payload: dict[str, Any]) -> bool:
    try:
        conn.sendall(json.dumps(payload, default=str).encode() + b"\n")
    except (BrokenPipeError, ConnectionResetError, OSError):
        return False
    return True


def _process_event(client: Any, queue: EventQueue, item: dict[str, Any]) -> None:
    args = item["args"]
    started = time.perf_counter()
    started_at = _utc_now_iso()
    result: Any = None
    error: str | None = None

    _store_gate.acquire_shared()
    try:
        if args.get("infer", True):
            with _llm_slots:
                result = _dispatch(client, "add", args)
        else:
            result = _dispatch(client, "add", args)
        result = result.get("result") if isinstance(result, dict) and "result" in result else result
    except Exception as exc:  # noqa: BLE001 - failures become event state.
        error = str(exc)
    finally:
        _store_gate.release_shared()

    finished_at = _utc_now_iso()
    terminal = True
    if error is None:
        queue.complete(item["id"], result)
    else:
        terminal = queue.fail(item["id"], error, item["attempts"])
    queue.refresh_alerts()

    # One manifest row per terminal outcome, mirroring the synchronous CLI path.
    if error is None or terminal:
        try:
            metadata = args.get("metadata") or {}
            append_live_audit(
                operation="add",
                input_payload={
                    "content": args.get("content"),
                    "infer": args.get("infer", True),
                    "event_id": item["id"],
                    "attempts": item["attempts"],
                },
                metadata=metadata,
                result={"error": error} if error is not None else result,
                started_at=started_at,
                finished_at=finished_at,
                duration_ms=int((time.perf_counter() - started) * 1000),
                scope={
                    k: v
                    for k, v in {
                        "user_id": args.get("user_id"),
                        "agent_id": args.get("agent_id"),
                        "run_id": args.get("run_id"),
                    }.items()
                    if v
                },
            )
        except Exception as exc:  # noqa: BLE001
            print(f"async add audit failed for {item['id']}: {exc}", file=sys.stderr, flush=True)


def _utc_now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _worker_loop(client: Any, queue: EventQueue, stop_event: threading.Event) -> None:
    while not stop_event.is_set():
        item = queue.claim_next()
        if item is None:
            with queue.notify:
                queue.notify.wait(timeout=1.0)
            continue
        _process_event(client, queue, item)


def _serve_connection(client: Any, conn: socket.socket) -> None:
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


def serve() -> None:
    STORE_DIR.mkdir(parents=True, exist_ok=True)
    # Lifetime lock: exactly one daemon may hold it. It is released by the
    # kernel on process death (crash-safe), so a holder is alive or gone —
    # never stale. Spurious extra spawns exit here before touching anything.
    global _lifetime_lock_handle
    _lifetime_lock_handle = (STORE_DIR / "daemon.lock").open("a+")
    try:
        import fcntl

        fcntl.flock(_lifetime_lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("another mem0-local daemon is running or initializing; exiting", file=sys.stderr, flush=True)
        sys.exit(0)
    except ImportError:
        pass

    # Holding the lifetime lock, any existing socket file is stale by
    # definition; bind() failing anyway (EADDRINUSE) is the kernel backstop.
    if SOCKET_PATH.exists():
        SOCKET_PATH.unlink()
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        server.bind(str(SOCKET_PATH))
    except OSError as exc:
        print(f"could not bind daemon socket ({exc}); exiting", file=sys.stderr, flush=True)
        sys.exit(0)
    SOCKET_PATH.chmod(0o600)

    _ensure_qdrant()
    client = memory_client()
    _serialize_spacy_inference()
    _prewarm(client)

    global _event_queue
    _event_queue = EventQueue()
    recovered = _event_queue.recover_stale()
    if recovered:
        print(f"requeued {recovered} stale in-flight events", file=sys.stderr, flush=True)
    purged = _event_queue.purge()
    if purged:
        print(f"purged {purged} terminal queue rows past retention", file=sys.stderr, flush=True)
    _event_queue.refresh_alerts()
    worker_stop = threading.Event()
    workers = [
        threading.Thread(
            target=_worker_loop,
            args=(client, _event_queue, worker_stop),
            daemon=True,
            name=f"mem0-event-{i}",
        )
        for i in range(ASYNC_WORKERS)
    ]
    for worker in workers:
        worker.start()

    PID_PATH.write_text(str(os.getpid()))
    server.listen(64)

    stop_event = threading.Event()

    def shutdown(_signum: int, _frame: Any) -> None:
        # Closing the socket unblocks accept(); in-flight requests drain below.
        stop_event.set()
        server.close()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    executor = concurrent.futures.ThreadPoolExecutor(
        max_workers=MAX_WORKERS, thread_name_prefix="mem0-req"
    )
    try:
        while not stop_event.is_set():
            try:
                conn, _ = server.accept()
            except OSError:
                if stop_event.is_set():
                    break
                raise
            executor.submit(_serve_connection, client, conn)
    finally:
        # Unlink the socket first so new CLI calls fall back to the direct
        # path instead of queueing on a draining daemon.
        for path in (SOCKET_PATH, PID_PATH):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        executor.shutdown(wait=True)
        # Give event workers a grace window; anything still 'processing' after
        # exit is requeued by recover_stale() on the next start.
        worker_stop.set()
        with _event_queue.notify:
            _event_queue.notify.notify_all()
        for worker in workers:
            worker.join(timeout=30.0)
        server.close()


def request(
    payload: dict[str, Any],
    *,
    timeout: float = REQUEST_TIMEOUT_SECONDS,
    connect_timeout: float = CONNECT_TIMEOUT_SECONDS,
) -> Any:
    if not SOCKET_PATH.exists():
        raise DaemonUnavailable("daemon socket does not exist")
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(connect_timeout)
            client.connect(str(SOCKET_PATH))
            client.settimeout(timeout)
            if not write_json_line(client, payload):
                raise DaemonUnavailable("failed to write request to daemon")
            response = read_json_line(client)
    except OSError as exc:
        raise DaemonUnavailable(str(exc)) from exc

    if response.get("status") != "ok":
        raise RuntimeError(response.get("error") or "daemon request failed")
    return response.get("result")


def pid_state(pid: int) -> str | None:
    try:
        status = (Path("/proc") / str(pid) / "status").read_text()
    except OSError:
        return None
    for line in status.splitlines():
        if line.startswith("State:"):
            parts = line.split()
            return parts[1] if len(parts) > 1 else None
    return None


def is_pid_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return pid_state(pid) != "Z"


def read_pid_cmdline(pid: int) -> str:
    try:
        return (Path("/proc") / str(pid) / "cmdline").read_bytes().replace(b"\x00", b" ").decode(errors="replace")
    except OSError:
        return ""


def is_daemon_pid(pid: int) -> bool:
    return "mem0_local.daemon" in read_pid_cmdline(pid)


def process_cpu_seconds(pid: int) -> float | None:
    try:
        raw = (Path("/proc") / str(pid) / "stat").read_text()
        fields_after_comm = raw[raw.rfind(")") + 2 :].split()
        utime = int(fields_after_comm[11])
        stime = int(fields_after_comm[12])
        ticks_per_second = os.sysconf("SC_CLK_TCK")
    except (FileNotFoundError, IndexError, OSError, ValueError):
        return None
    return (utime + stime) / ticks_per_second


def sample_process_cpu_percent(pid: int | None, *, sample_seconds: float = CPU_SAMPLE_SECONDS) -> float | None:
    if pid is None or not is_pid_running(pid):
        return None
    start_cpu = process_cpu_seconds(pid)
    start_wall = time.monotonic()
    if start_cpu is None:
        return None
    time.sleep(sample_seconds)
    end_cpu = process_cpu_seconds(pid)
    end_wall = time.monotonic()
    if end_cpu is None or end_wall <= start_wall:
        return None
    return round(((end_cpu - start_cpu) / (end_wall - start_wall)) * 100, 3)


def unlink_runtime_files() -> None:
    for path in (SOCKET_PATH, PID_PATH):
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def terminate_daemon_pid(pid: int, *, wait_seconds: float = 5.0, force: bool = False) -> bool:
    if not is_pid_running(pid):
        return True
    if not is_daemon_pid(pid):
        return False

    os.kill(pid, signal.SIGTERM)
    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        if not is_pid_running(pid):
            return True
        time.sleep(0.2)

    if force and is_pid_running(pid):
        os.kill(pid, signal.SIGKILL)
        deadline = time.time() + 2.0
        while time.time() < deadline:
            if not is_pid_running(pid):
                return True
            time.sleep(0.1)

    return not is_pid_running(pid)


def terminate_process(proc: subprocess.Popen[Any], *, wait_seconds: float = 5.0, force: bool = True) -> bool:
    if proc.poll() is not None:
        return True
    proc.terminate()
    try:
        proc.wait(timeout=wait_seconds)
        return True
    except subprocess.TimeoutExpired:
        if not force:
            return False
    proc.kill()
    try:
        proc.wait(timeout=2.0)
    except subprocess.TimeoutExpired:
        return False
    return True


def read_pid() -> int | None:
    try:
        return int(PID_PATH.read_text().strip())
    except (FileNotFoundError, ValueError):
        return None


def ping(timeout: float = 5.0) -> dict[str, Any] | None:
    # A generous read timeout matters: a busy-but-alive daemon answering ping
    # slowly must not be mistaken for a dead one (that misdiagnosis is what
    # causes spurious extra spawns).
    try:
        return request({"op": "ping"}, timeout=timeout)
    except Exception:
        return None


def start_daemon(wait_seconds: float = 90.0) -> dict[str, Any]:
    existing = ping()
    if existing:
        return {"started": False, **existing}

    STORE_DIR.mkdir(parents=True, exist_ok=True)
    # Serialize concurrent starters (e.g. parallel CLI calls auto-starting
    # after a reboot): one spawns, the rest wait here and find it via ping.
    start_lock = (STORE_DIR / "daemon-start.lock").open("a+")
    try:
        import fcntl

        fcntl.flock(start_lock.fileno(), fcntl.LOCK_EX)
    except ImportError:
        pass
    try:
        existing = ping()
        if existing:
            return {"started": False, **existing}
        return _start_daemon_locked(wait_seconds)
    finally:
        start_lock.close()


def _start_daemon_locked(wait_seconds: float) -> dict[str, Any]:
    pid = read_pid()
    recovered_stale_pid: int | None = None
    if pid is not None:
        if is_pid_running(pid) and is_daemon_pid(pid):
            recovered = terminate_daemon_pid(pid, wait_seconds=5.0, force=True)
            if not recovered:
                raise RuntimeError(f"stale daemon pid {pid} is running but not responding")
            recovered_stale_pid = pid
        elif not is_pid_running(pid):
            recovered_stale_pid = pid
        else:
            recovered_stale_pid = pid
    if SOCKET_PATH.exists() or recovered_stale_pid is not None:
        unlink_runtime_files()

    log = LOG_PATH.open("ab")
    proc = subprocess.Popen(
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
            if current.get("pid") != proc.pid and proc.poll() is None:
                # Another daemon answered: ours is a redundant spawn — reap it
                # so it cannot linger blocked on shared locks.
                terminate_process(proc, wait_seconds=5.0, force=True)
                return {"started": False, **current}
            return {"started": True, **current}
        if proc.poll() is not None:
            raise RuntimeError(f"daemon exited during startup with code {proc.returncode}; see {LOG_PATH}")
        time.sleep(0.5)
    terminate_process(proc, wait_seconds=5.0, force=True)
    if read_pid() == proc.pid:
        unlink_runtime_files()
    raise TimeoutError(f"daemon did not become ready within {wait_seconds:.0f}s; see {LOG_PATH}")


def stop_daemon(wait_seconds: float = 10.0) -> dict[str, Any]:
    pid = read_pid()
    if pid is None:
        if SOCKET_PATH.exists():
            SOCKET_PATH.unlink()
        return {"stopped": False, "reason": "pid file missing"}

    if not is_pid_running(pid):
        unlink_runtime_files()
        return {"stopped": False, "pid": pid, "reason": "process was not running"}

    if not is_daemon_pid(pid):
        unlink_runtime_files()
        return {"stopped": False, "pid": pid, "reason": "pid file did not point to mem0-local daemon"}

    stopped = terminate_daemon_pid(pid, wait_seconds=wait_seconds, force=False)
    if stopped:
        unlink_runtime_files()
    return {"stopped": stopped, "pid": pid}


def status() -> dict[str, Any]:
    pid = read_pid()
    pong = ping()
    pid_running = is_pid_running(pid) if pid is not None else False
    pid_is_daemon = is_daemon_pid(pid) if pid is not None and pid_running else False
    cpu_percent = sample_process_cpu_percent(pid) if pid_running else None
    return {
        "running": pid_running and pid_is_daemon,
        "responsive": bool(pong),
        "pid": pid,
        "pid_running": pid_running,
        "pid_is_daemon": pid_is_daemon,
        "cpu_percent": cpu_percent,
        "cpu_sample_seconds": CPU_SAMPLE_SECONDS if cpu_percent is not None else None,
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
