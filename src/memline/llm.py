"""Judge LLM endpoints: one primary, one independent fallback.

Every memline judge (staleness, necessity, safety, correctness) and the
optional LLM reranker run through here. The primary is the company internal
relay; it is only reachable through the corporate proxy, so a laptop off the
corporate network — or a relay hiccup — would otherwise stall the whole
hygiene pipeline. Each call therefore retries once on a second, fully
independent endpoint (different vendor, different key, different network
path) before giving up.

Two mem0 behaviours force the construction below:

* ``mem0.llms.openai.OpenAILLM`` switches its client to OpenRouter whenever
  ``OPENROUTER_API_KEY`` is in the environment. The fallback needs that
  variable, so the primary's ``base_url`` would be silently hijacked. Both
  endpoints therefore get an explicitly constructed client.
* ``_get_common_params`` passes ``extra_body`` straight through to the SDK,
  which is how a per-endpoint body patch (e.g. OpenRouter provider pinning)
  reaches the wire without every call site knowing about it.

The relay also constrains the transport, in two ways handled here:

* Its ``base_url`` is plain HTTP, which the corporate proxy forwards rather
  than tunnels — and it strips the body on the way, so every call came back
  ``400 invalid JSON request body`` and every judge quietly ran on the
  fallback. ``memline.proxy`` supplies a CONNECT-tunnelling client.
* That path also drops any request whose first response byte takes longer
  than about 30 seconds, regardless of size. A streaming request starts
  emitting immediately, so endpoints on such a path set ``stream = true`` and
  get a client that streams and reassembles under mem0's synchronous call.
"""

from __future__ import annotations

import json
import os
import sys
import threading
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from memline.config import llm_endpoint_specs
from memline.proxy import client_for_base_url


@dataclass(frozen=True)
class Endpoint:
    """One OpenAI-compatible chat endpoint."""

    name: str
    model: str
    base_url: str
    api_key_env: str | None = None
    api_key_json: str | None = None
    api_key_json_path: str | None = None
    site_url: str | None = None
    app_name: str | None = None
    # Request the answer as a token stream and reassemble it before returning.
    # Only needed on a network path that times out on slow first bytes; the
    # answer handed back to the caller is identical either way.
    stream: bool = False
    # Merged under every call's own extra_body (the caller wins on conflict).
    extra_body: dict[str, Any] = field(default_factory=dict)

    def api_key(self) -> str:
        """Resolve the credential at call-build time, never at import time.

        Reading it fresh keeps the secret out of config files and out of this
        process's long-lived state: a rotated key takes effect on the next
        client build, and nothing here copies it anywhere.
        """
        if self.api_key_env:
            key = os.environ.get(self.api_key_env)
            if key:
                return key
        if self.api_key_json:
            path = os.path.expanduser(self.api_key_json)
            try:
                with open(path, encoding="utf-8") as fh:
                    data: Any = json.load(fh)
            except OSError as exc:
                raise RuntimeError(f"llm endpoint '{self.name}': cannot read {path}: {exc}") from exc
            for part in (self.api_key_json_path or "").split("."):
                if not part:
                    continue
                if not isinstance(data, dict) or part not in data:
                    raise RuntimeError(
                        f"llm endpoint '{self.name}': {path} has no key path "
                        f"'{self.api_key_json_path}'"
                    )
                data = data[part]
            if isinstance(data, str) and data:
                return data
        raise RuntimeError(
            f"llm endpoint '{self.name}': no credential. Set ${self.api_key_env} "
            "or point api_key_json/api_key_json_path at a readable file."
        )


class _StreamingCompletions:
    """``create()`` that asks for a stream and returns the assembled answer.

    mem0 calls ``client.chat.completions.create(**params)`` and reads a
    complete ``ChatCompletion`` off the result, so the streaming stays entirely
    inside this call: the chunks are folded back into one completion object by
    the SDK's own accumulator, and the caller cannot tell the difference.
    """

    def __init__(self, completions: Any) -> None:
        self._completions = completions

    def create(self, **params: Any) -> Any:
        if params.get("stream"):
            # A caller that wants the raw stream gets the raw stream.
            return self._completions.create(**params)
        from openai.lib.streaming.chat import ChatCompletionStreamState

        # Both are optional; passing them lets the accumulator rebuild tool
        # calls and parsed content exactly as the non-streamed call would.
        state_args = {
            key: params[field]
            for key, field in (("input_tools", "tools"), ("response_format", "response_format"))
            if params.get(field)
        }
        state = ChatCompletionStreamState(**state_args)
        for chunk in self._completions.create(**{**params, "stream": True}):
            state.handle_chunk(chunk)
        return state.get_final_completion()


class _StreamingClient:
    """An OpenAI client whose chat completions stream; the rest passes through."""

    def __init__(self, client: Any) -> None:
        self._client = client
        self.chat = SimpleNamespace(
            completions=_StreamingCompletions(client.chat.completions)
        )

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)


def build_endpoint_llm(endpoint: Endpoint, max_tokens: int) -> Any:
    """A mem0 OpenAILLM bound to exactly this endpoint."""
    from mem0.utils.factory import LlmFactory
    from openai import OpenAI

    llm = LlmFactory.create(
        "openai",
        {
            "model": endpoint.model,
            "openai_base_url": endpoint.base_url,
            "site_url": endpoint.site_url,
            "app_name": endpoint.app_name,
            "temperature": 0.0,
            "max_tokens": max_tokens,
            "top_p": 0.1,
            "is_reasoning_model": False,
        },
    )
    # Overwrite the client mem0 built: its OPENROUTER_API_KEY sniffing ignores
    # openai_base_url whenever that variable exists, which it does here. The
    # explicit http_client is what keeps a plain-HTTP base_url off the
    # body-stripping forward-proxy path (see memline.proxy).
    client = OpenAI(
        api_key=endpoint.api_key(),
        base_url=endpoint.base_url,
        http_client=client_for_base_url(endpoint.base_url),
    )
    llm.client = _StreamingClient(client) if endpoint.stream else client
    return llm


def _is_empty_answer(response: Any) -> bool:
    """True when an endpoint returned HTTP 200 but no answer.

    Thinking models reached through a relay that ignores reasoning-disable can
    spend the whole max_tokens budget on reasoning and return empty content
    with finish_reason=length. That is a failed call wearing a success code:
    the parse layer raises on it, so without this check the fallback would sit
    idle while every judge failed. Tool-call responses are dicts and legally
    carry empty content, so only plain text answers are inspected.
    """
    if response is None:
        return True
    return isinstance(response, str) and not response.strip()


class FallbackLLM:
    """Primary endpoint with a one-shot retry on an independent fallback.

    Transport failures and empty answers fall through; a judge that returns
    *malformed* JSON does not, because the callers already salvage partial
    output and a second call would cost without adding information.
    ``active_model`` names whoever actually answered last, so the pair store
    records real provenance rather than the configured preference.
    """

    def __init__(self, endpoints: list[Endpoint], max_tokens: int) -> None:
        if not endpoints:
            raise RuntimeError("no llm endpoints configured")
        self._endpoints = endpoints
        self._max_tokens = max_tokens
        self._llms: dict[str, Any] = {}
        self._lock = threading.Lock()
        self.active_model: str = endpoints[0].model

    @property
    def model(self) -> str:
        return self._endpoints[0].model

    def _llm_for(self, endpoint: Endpoint) -> Any:
        with self._lock:
            llm = self._llms.get(endpoint.name)
            if llm is None:
                llm = build_endpoint_llm(endpoint, self._max_tokens)
                self._llms[endpoint.name] = llm
            return llm

    def generate_response(self, messages: list[dict[str, str]], **kwargs: Any) -> Any:
        errors: list[str] = []
        for index, endpoint in enumerate(self._endpoints):
            call_kwargs = dict(kwargs)
            if endpoint.extra_body:
                merged = dict(endpoint.extra_body)
                merged.update(call_kwargs.get("extra_body") or {})
                call_kwargs["extra_body"] = merged
            try:
                response = self._llm_for(endpoint).generate_response(messages, **call_kwargs)
            except Exception as exc:  # noqa: BLE001 - any transport failure is a fallover.
                errors.append(f"{endpoint.name}({endpoint.model}): {type(exc).__name__}: {exc}")
                # Drop the client so a half-broken one is not reused.
                with self._lock:
                    self._llms.pop(endpoint.name, None)
                continue
            if _is_empty_answer(response):
                errors.append(f"{endpoint.name}({endpoint.model}): empty response")
                continue
            if index:
                print(
                    f"llm fallback: {self._endpoints[0].name} failed, served by "
                    f"{endpoint.name} ({endpoint.model}); {errors[-1]}",
                    file=sys.stderr,
                    flush=True,
                )
            self.active_model = endpoint.model
            return response
        raise RuntimeError("all llm endpoints failed: " + " | ".join(errors))


def build_llm(max_tokens: int, *, job: str) -> FallbackLLM:
    """This job's endpoint chain: primary first, fallbacks in config order.

    ``job`` is required. There is no sensible endpoint to assume for a caller
    that has not said what work it is doing, and assuming one is how a call
    ends up on a model nobody chose for it.
    """
    endpoints = [Endpoint(**spec) for spec in llm_endpoint_specs(job)]
    return FallbackLLM(endpoints, max_tokens)


def active_model(llm: Any, default: str | None = None) -> str | None:
    """Model that answered the most recent call on ``llm``, if it tracks one."""
    return getattr(llm, "active_model", None) or default
