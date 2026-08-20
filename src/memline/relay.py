"""Ask a configured endpoint for one JSON answer, and say what actually happened.

`memline.llm` exists for mem0's own judge calls, which are short and text
shaped. This module is for the other kind of call: a long structured one, where
the answer is a JSON document, the prompt is large, and the caller needs to know
which endpoint produced the result.

Three failure modes are handled here because each is silent otherwise:

* **A slow first byte.** The relay path drops a request that takes too long to
  start answering, so requests always stream — a reasoning model emits its
  thinking immediately and keeps the connection warm even when the answer is
  minutes away.
* **A truncated answer.** ``finish_reason == "length"`` returns JSON-shaped text
  that is not JSON. That is a retry, not a parse error to report.
* **A refusal.** A moderation refusal arrives as an HTTP error inside an
  event-stream response; read naively it looks like an empty answer, and a
  retry only spends the quota again. It is reported as terminal.
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from memline.config import llm_endpoint_specs, llm_knobs
from memline.llm import Endpoint
from memline.proxy import client_for_base_url
from memline.runtime import setup_env


class RefusedError(RuntimeError):
    """The endpoint declined the content. Retrying will not help."""


def _heartbeat_interval() -> float:
    """Seconds between stream heartbeats; a 40-minute review and a 30-second
    profile call want different cadences."""
    raw = os.environ.get("MEMLINE_STREAM_HEARTBEAT", "30")
    try:
        value = float(raw)
    except ValueError:
        return 30.0
    return value if value > 0 else 30.0


class _StreamProgress:
    """Report stream activity without exposing any generated text.

    The OpenAI iterator blocks while the endpoint is quiet, so reporting only
    from inside its loop cannot distinguish a live stream from a silent one.
    A small daemon heartbeat snapshots counters every ``interval`` seconds.
    The callback receives metadata only: no prompt, delta, or completed text.
    """

    def __init__(
        self,
        endpoint: Endpoint,
        attempt: int,
        report: Callable[[str], None],
        *,
        interval: float | None = None,
    ) -> None:
        self.endpoint = endpoint
        self.attempt = attempt
        self.report = report
        self.interval = interval if interval is not None else _heartbeat_interval()
        self.started = time.monotonic()
        self.last_chunk_at: float | None = None
        self.chunks = 0
        self.content_chars = 0
        self.reasoning_chars = 0
        # Deltas are the only token signal available while the stream runs; one
        # usually carries one token, which is why they are reported with a ~.
        self.content_deltas = 0
        self.reasoning_deltas = 0
        # Exact, but most endpoints send usage in the final chunk only.
        self.prompt_tokens: int | None = None
        self.completion_tokens: int | None = None
        # A rate over the whole call cannot show a stall: it decays instead of
        # dropping. Each line reports what arrived since the previous one.
        self._marked_tokens = 0
        self._marked_at = self.started
        self._lock = threading.Lock()
        self._stopped = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self.report(self._line("started"))
        self._thread = threading.Thread(
            target=self._heartbeat,
            name=f"memline-stream-{self.endpoint.name}",
            daemon=True,
        )
        self._thread.start()

    def observe(self, chunk: Any) -> None:
        content_chars = 0
        reasoning_chars = 0
        content_deltas = 0
        reasoning_deltas = 0
        for choice in getattr(chunk, "choices", None) or []:
            delta = getattr(choice, "delta", None)
            if delta is None:
                continue
            content = getattr(delta, "content", None)
            if isinstance(content, str):
                content_chars += len(content)
                content_deltas += 1
            for field_name in ("reasoning_content", "reasoning"):
                reasoning = getattr(delta, field_name, None)
                if isinstance(reasoning, str):
                    reasoning_chars += len(reasoning)
                    reasoning_deltas += 1
        chunk_usage = getattr(chunk, "usage", None)
        with self._lock:
            self.chunks += 1
            self.content_chars += content_chars
            self.reasoning_chars += reasoning_chars
            self.content_deltas += content_deltas
            self.reasoning_deltas += reasoning_deltas
            if chunk_usage is not None:
                prompt = getattr(chunk_usage, "prompt_tokens", None)
                completion = getattr(chunk_usage, "completion_tokens", None)
                if isinstance(prompt, int):
                    self.prompt_tokens = prompt
                if isinstance(completion, int):
                    self.completion_tokens = completion
            self.last_chunk_at = time.monotonic()

    def emit(self) -> None:
        self.report(self._line("active"))

    def stop(
        self,
        status: str,
        *,
        finish_reason: str | None = None,
        usage: dict[str, Any] | None = None,
    ) -> None:
        self._stopped.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=0.1)
        if usage:
            with self._lock:
                if isinstance(usage.get("prompt_tokens"), int):
                    self.prompt_tokens = usage["prompt_tokens"]
                if isinstance(usage.get("completion_tokens"), int):
                    self.completion_tokens = usage["completion_tokens"]
        self.report(self._line(status) + f" finish_reason={finish_reason or 'none'}")

    def _heartbeat(self) -> None:
        while not self._stopped.wait(self.interval):
            self.emit()

    def _line(self, status: str) -> str:
        now = time.monotonic()
        with self._lock:
            chunks = self.chunks
            content_chars = self.content_chars
            reasoning_chars = self.reasoning_chars
            content_deltas = self.content_deltas
            reasoning_deltas = self.reasoning_deltas
            prompt_tokens = self.prompt_tokens
            completion_tokens = self.completion_tokens
            last_chunk_at = self.last_chunk_at
            est = content_deltas + reasoning_deltas
            since_tokens = est - self._marked_tokens
            since_seconds = max(0.0, now - self._marked_at)
            self._marked_tokens = est
            self._marked_at = now
        last_chunk = "none" if last_chunk_at is None else f"{max(0.0, now - last_chunk_at):.1f}s"
        elapsed = max(0.0, now - self.started)
        exact = ""
        if prompt_tokens is not None or completion_tokens is not None:
            exact = (f" prompt_tokens={prompt_tokens if prompt_tokens is not None else 'unknown'}"
                     f" completion_tokens={completion_tokens if completion_tokens is not None else 'unknown'}")
        return (
            f"llm stream: status={status} endpoint={self.endpoint.name} "
            f"model={self.endpoint.model} attempt={self.attempt} "
            f"elapsed={elapsed:.1f}s chunks={chunks} "
            f"out_tokens~{est} (content~{content_deltas} reasoning~{reasoning_deltas}) "
            f"since_last_line={since_tokens}tok/{since_seconds:.0f}s{exact} "
            f"content_chars={content_chars} reasoning_chars={reasoning_chars} "
            f"last_chunk_age={last_chunk}"
        )


@dataclass
class CallResult:
    text: str
    endpoint: str
    model: str
    attempt: int
    seconds: float
    usage: dict[str, Any] = field(default_factory=dict)
    finish_reason: str | None = None
    # Why the endpoints before this one were not used. A fallback that
    # succeeds is still a primary that failed, and the caller has to be able
    # to see that: silence here reads as "the primary was fine".
    earlier_failures: list[str] = field(default_factory=list)

    @property
    def provenance(self) -> dict[str, Any]:
        return {"endpoint": self.endpoint, "model": self.model, "attempt": self.attempt,
                "seconds": round(self.seconds), "usage": self.usage,
                "earlier_failures": self.earlier_failures}


def _stream_once(
    endpoint: Endpoint,
    prompt: str,
    max_tokens: int,
    timeout: float,
    *,
    progress: Callable[[str], None] | None = None,
    attempt: int = 1,
) -> tuple[str, dict, str | None]:
    from openai import OpenAI

    parts: list[str] = []
    usage: dict[str, Any] = {}
    finish: str | None = None
    observer = _StreamProgress(endpoint, attempt, progress) if progress else None
    if observer:
        observer.start()
    try:
        http_client = client_for_base_url(endpoint.base_url)
        if http_client is not None:
            http_client.timeout = timeout
        client = OpenAI(api_key=endpoint.api_key(), base_url=endpoint.base_url,
                        timeout=timeout, **({"http_client": http_client} if http_client else {}))
        stream = client.chat.completions.create(
            model=endpoint.model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=max_tokens,
            stream=True,
            **({"extra_body": endpoint.extra_body} if endpoint.extra_body else {}),
        )
        for chunk in stream:
            if observer:
                observer.observe(chunk)
            if getattr(chunk, "usage", None):
                usage = chunk.usage.model_dump() if hasattr(chunk.usage, "model_dump") else dict(chunk.usage)
            for choice in getattr(chunk, "choices", None) or []:
                finish = getattr(choice, "finish_reason", None) or finish
                delta = getattr(choice, "delta", None)
                if delta is not None and getattr(delta, "content", None):
                    parts.append(delta.content)
    except Exception:
        if observer:
            observer.stop("error", finish_reason=finish, usage=usage)
        raise
    if observer:
        observer.stop("complete", finish_reason=finish, usage=usage)
    return "".join(parts), usage, finish


def _looks_refused(exc: Exception) -> bool:
    text = str(exc).lower()
    return "high risk" in text or "content" in text and "reject" in text



def call_json(
    prompt: str,
    *,
    job: str,
    endpoints: list[Endpoint] | None = None,
    max_tokens: int = 128000,
    attempts_per_endpoint: int = 4,
    timeout: float = 2400.0,
    backoff: float = 30.0,
    progress: Callable[[str], None] | None = None,
) -> tuple[Any, CallResult]:
    """Return ``(parsed_json, CallResult)``.

    Tries each endpoint in order. A transport failure, a truncation, or an
    unparseable answer costs one attempt; a refusal ends the whole call, since
    every endpoint of the same vendor will refuse the same content.

    ``job`` names the config table to read endpoints from, and it is required:
    a default here would mean any new caller that forgot to say what it was
    doing quietly spent some other job's budget on some other job's model.
    Config may also state this job's knobs, and when it does they win over the
    caller's — the numbers below are what a caller assumes when nobody has
    tuned the job, not a policy the configuration has to argue with.

    The primary is retried several times before the fallback is reached: a
    relay path drops connections often enough that one flake would otherwise
    move the work — and its cost — to a metered vendor for no reason.

    ``timeout`` is httpx's, so it bounds the wait for *each read*, not the call
    as a whole. A stream that goes quiet mid-answer therefore hangs for this
    long per silent gap; one such gap held a drafting run for 55 minutes with
    nothing to show and no error.
    """
    # The credential file has to be loaded even when nothing else in this
    # process touched the store: a CLI that reads its data through the daemon
    # never builds a client, so without this the fallback endpoint finds no key
    # and a primary outage turns into a total outage.
    setup_env()
    knobs = llm_knobs(job)
    max_tokens = int(knobs.get("max_tokens", max_tokens))
    attempts_per_endpoint = int(knobs.get("attempts_per_endpoint", attempts_per_endpoint))
    timeout = float(knobs.get("timeout", timeout))
    backoff = float(knobs.get("backoff", backoff))
    endpoints = endpoints or [Endpoint(**spec) for spec in llm_endpoint_specs(job)]
    errors: list[str] = []
    for endpoint in endpoints:
        for attempt in range(1, attempts_per_endpoint + 1):
            started = time.time()
            try:
                text, usage, finish = _stream_once(endpoint, prompt, max_tokens, timeout,
                                                   progress=progress, attempt=attempt)
            except Exception as exc:  # noqa: BLE001 - every transport error is a retry candidate
                if _looks_refused(exc):
                    raise RefusedError(f"{endpoint.name}: {exc}") from exc
                errors.append(f"{endpoint.name} attempt {attempt}: {type(exc).__name__}: {exc}")
                time.sleep(backoff * attempt)
                continue
            result = CallResult(text=text, endpoint=endpoint.name, model=endpoint.model,
                                attempt=attempt, seconds=time.time() - started,
                                usage=usage, finish_reason=finish,
                                earlier_failures=list(errors))
            if finish == "length":
                errors.append(f"{endpoint.name} attempt {attempt}: truncated at {max_tokens} tokens")
                continue
            try:
                return parse_json(text), result
            except ValueError as exc:
                errors.append(f"{endpoint.name} attempt {attempt}: {exc}")
                time.sleep(backoff)
    raise RuntimeError("no endpoint produced valid JSON:\n  " + "\n  ".join(errors))


def parse_json(text: str) -> Any:
    """Parse a model's answer, tolerating a code fence but nothing else."""
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        stripped = stripped.removeprefix("json").strip()
    if not stripped:
        raise ValueError("empty answer")
    try:
        return json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise ValueError(f"not JSON: {exc}") from exc
