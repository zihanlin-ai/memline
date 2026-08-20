"""Every endpoint gets its full retry budget before the next one is tried.

The primary is deliberately the cheap path, so a flake there must not hand the
work — and its cost — to a metered endpoint on the first stumble. That holds
whatever the failure looks like: the budget is spent before moving on, and a
fast-path exit for failures that "look permanent" was tried and taken back out,
because guessing which errors are permanent from their text is exactly the kind
of cleverness that drops a retry the endpoint would have answered.
"""

from __future__ import annotations

import unittest
from unittest import mock
from types import SimpleNamespace

from memline import relay
from memline.llm import Endpoint


def endpoint(name, model):
    return Endpoint(name=name, model=model, base_url="https://x/v1", api_key_env="K")


class FailoverTest(unittest.TestCase):
    def setUp(self):
        self.calls: list[str] = []
        # This suite states a retry policy, so it must not read the developer's
        # own config: `call_json` lets a job's configured knobs win over its
        # arguments, and a workspace that pins `attempts_per_endpoint = 1` for
        # `draft` would silently rewrite every budget asserted below.
        for target, replacement in (("setup_env", lambda: None),
                                    ("llm_knobs", lambda job: {})):
            patcher = mock.patch.object(relay, target, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)
        sleeper = mock.patch.object(relay.time, "sleep", lambda _: None)
        sleeper.start()
        self.addCleanup(sleeper.stop)

    def _run(self, failure, **kwargs):
        def stream_once(ep, prompt, max_tokens, timeout, progress=None, attempt=1):
            self.calls.append(ep.name)
            if ep.name == "primary":
                raise RuntimeError(failure)
            return '{"ok": true}', {}, "stop"
        with mock.patch.object(relay, "_stream_once", stream_once):
            return relay.call_json("p", job="draft",
                                   endpoints=[endpoint("primary", "a"),
                                              endpoint("fallback", "b")],
                                   backoff=0, **kwargs)

    def test_a_transport_hiccup_spends_every_attempt_before_failing_over(self):
        self._run("APIConnectionError: connection reset", attempts_per_endpoint=3)
        self.assertEqual(self.calls, ["primary", "primary", "primary", "fallback"])

    def test_a_missing_model_is_retried_too(self):
        # A relay whose channel is being restored answers this way while it
        # comes back, so the attempts are not wasted on principle.
        self._run("Error code: 404 - {'code': 'model_not_found'}", attempts_per_endpoint=3)
        self.assertEqual(self.calls, ["primary", "primary", "primary", "fallback"])

    def test_the_fallback_answers_and_says_what_it_replaced(self):
        _, result = self._run("model_not_found", attempts_per_endpoint=2)
        self.assertEqual(result.endpoint, "fallback")
        self.assertEqual(len(result.earlier_failures), 2)
        self.assertIn("model_not_found", result.earlier_failures[0])


class StreamProgressTest(unittest.TestCase):
    def test_progress_reports_counts_without_exposing_generated_text(self):
        messages: list[str] = []
        progress = relay._StreamProgress(
            endpoint("primary", "model-a"),
            2,
            messages.append,
            interval=60,
        )
        secret_content = "generated-secret-text"
        secret_reasoning = "hidden-reasoning-text"
        chunk = SimpleNamespace(
            choices=[SimpleNamespace(delta=SimpleNamespace(
                content=secret_content,
                reasoning_content=secret_reasoning,
            ))]
        )

        progress.observe(chunk)
        progress.emit()

        self.assertIn("chunks=1", messages[-1])
        self.assertIn(f"content_chars={len(secret_content)}", messages[-1])
        self.assertIn(f"reasoning_chars={len(secret_reasoning)}", messages[-1])
        self.assertNotIn(secret_content, messages[-1])
        self.assertNotIn(secret_reasoning, messages[-1])

    def test_progress_distinguishes_a_silent_live_stream(self):
        messages: list[str] = []
        progress = relay._StreamProgress(
            endpoint("primary", "model-a"),
            1,
            messages.append,
            interval=60,
        )

        progress.emit()

        self.assertIn("chunks=0", messages[-1])
        self.assertIn("last_chunk_age=none", messages[-1])


if __name__ == "__main__":
    unittest.main()
