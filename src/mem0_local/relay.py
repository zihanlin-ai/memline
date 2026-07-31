"""Ask a configured endpoint for one JSON answer, and say what actually happened.

`mem0_local.llm` exists for mem0's own judge calls, which are short and text
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
import time
from dataclasses import dataclass, field
from typing import Any

from mem0_local.config import llm_endpoint_specs
from mem0_local.llm import Endpoint
from mem0_local.proxy import client_for_base_url
from mem0_local.runtime import setup_env


class RefusedError(RuntimeError):
    """The endpoint declined the content. Retrying will not help."""


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


def _stream_once(endpoint: Endpoint, prompt: str, max_tokens: int, timeout: float) -> tuple[str, dict, str | None]:
    from openai import OpenAI

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
    parts: list[str] = []
    usage: dict[str, Any] = {}
    finish: str | None = None
    for chunk in stream:
        if getattr(chunk, "usage", None):
            usage = chunk.usage.model_dump() if hasattr(chunk.usage, "model_dump") else dict(chunk.usage)
        for choice in getattr(chunk, "choices", None) or []:
            finish = getattr(choice, "finish_reason", None) or finish
            delta = getattr(choice, "delta", None)
            if delta is not None and getattr(delta, "content", None):
                parts.append(delta.content)
    return "".join(parts), usage, finish


def _looks_refused(exc: Exception) -> bool:
    text = str(exc).lower()
    return "high risk" in text or "content" in text and "reject" in text


def call_json(
    prompt: str,
    *,
    profile: str | None = "wiki",
    endpoints: list[Endpoint] | None = None,
    max_tokens: int = 64000,
    attempts_per_endpoint: int = 4,
    timeout: float = 2400.0,
    backoff: float = 30.0,
) -> tuple[Any, CallResult]:
    """Return ``(parsed_json, CallResult)``.

    Tries each endpoint in order. A transport failure, a truncation, or an
    unparseable answer costs one attempt; a refusal ends the whole call, since
    every endpoint of the same vendor will refuse the same content.

    ``profile`` names the config section to read endpoints from, so long
    structured work can run on a different model from mem0's own judges.

    The primary is retried several times before the fallback is reached: this
    path drops connections often enough that one flake would otherwise move
    the work — and its cost — to a metered vendor for no reason.
    """
    # The credential file has to be loaded even when nothing else in this
    # process touched the store: a CLI that reads its data through the daemon
    # never builds a client, so without this the fallback endpoint finds no key
    # and a primary outage turns into a total outage.
    setup_env()
    endpoints = endpoints or [Endpoint(**spec) for spec in llm_endpoint_specs(profile)]
    errors: list[str] = []
    for endpoint in endpoints:
        for attempt in range(1, attempts_per_endpoint + 1):
            started = time.time()
            try:
                text, usage, finish = _stream_once(endpoint, prompt, max_tokens, timeout)
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
