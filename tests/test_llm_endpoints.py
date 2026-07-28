from __future__ import annotations

import importlib
import json
import os
import tempfile
import unittest
from pathlib import Path

from mem0_local import config


class _FakeLLM:
    """Stands in for mem0's OpenAILLM: records calls, fails on demand."""

    def __init__(self, tag: str, fail: bool = False, answer: object = None):
        self.tag = tag
        self.fail = fail
        self._answer = answer
        self.calls: list[dict] = []

    def generate_response(self, messages, **kwargs):
        self.calls.append(kwargs)
        if self.fail:
            raise RuntimeError(f"{self.tag} is down")
        if self._answer is not None:
            return self._answer
        return f"answer from {self.tag}"


class EndpointSpecTests(unittest.TestCase):
    """[llm] + [llm.fallback] -> an ordered list of endpoint specs."""

    def setUp(self):
        self._orig_env = os.environ.get("MEM0_LOCAL_CONFIG")
        self._tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        if self._orig_env is None:
            os.environ.pop("MEM0_LOCAL_CONFIG", None)
        else:
            os.environ["MEM0_LOCAL_CONFIG"] = self._orig_env
        importlib.reload(config)
        self._tmp.cleanup()

    def _reload_with(self, toml_text: str):
        cfg_path = Path(self._tmp.name) / "config.toml"
        cfg_path.write_text(toml_text)
        os.environ["MEM0_LOCAL_CONFIG"] = str(cfg_path)
        return importlib.reload(config)

    def test_primary_only_when_no_fallback_configured(self):
        cfg = self._reload_with(
            '[llm]\nmodel = "m1"\nbase_url = "https://a/v1"\napi_key_env = "K1"\n'
        )
        specs = cfg.llm_endpoint_specs()
        self.assertEqual([s["name"] for s in specs], ["primary"])
        self.assertEqual(specs[0]["model"], "m1")

    def test_fallback_follows_primary_in_order(self):
        cfg = self._reload_with(
            '[llm]\nmodel = "m1"\nbase_url = "https://a/v1"\napi_key_env = "K1"\n'
            '\n[llm.fallback]\nmodel = "m2"\nbase_url = "https://b/v1"\napi_key_env = "K2"\n'
        )
        specs = cfg.llm_endpoint_specs()
        self.assertEqual([s["model"] for s in specs], ["m1", "m2"])
        self.assertEqual([s["api_key_env"] for s in specs], ["K1", "K2"])

    def test_multiple_fallbacks_keep_declaration_order(self):
        cfg = self._reload_with(
            '[llm]\nmodel = "m1"\napi_key_env = "K1"\n'
            '\n[[llm.fallback]]\nmodel = "m2"\napi_key_env = "K2"\n'
            '\n[[llm.fallback]]\nmodel = "m3"\napi_key_env = "K3"\n'
        )
        specs = cfg.llm_endpoint_specs()
        self.assertEqual([s["name"] for s in specs], ["primary", "fallback1", "fallback2"])
        self.assertEqual([s["model"] for s in specs], ["m1", "m2", "m3"])

    def test_file_credentialed_primary_does_not_inherit_the_legacy_env_default(self):
        """The regression that would send the relay an OpenRouter key."""
        cfg = self._reload_with(
            '[llm]\nmodel = "m1"\nbase_url = "http://relay/v1"\n'
            'api_key_json = "/tmp/auth.json"\napi_key_json_path = "vendor.key"\n'
        )
        primary = cfg.llm_endpoint_specs()[0]
        self.assertNotIn("api_key_env", primary)

    def test_fallback_must_declare_its_own_credential(self):
        cfg = self._reload_with(
            '[llm]\nmodel = "m1"\napi_key_env = "K1"\n\n[llm.fallback]\nmodel = "m2"\n'
        )
        with self.assertRaises(ValueError):
            cfg.llm_endpoint_specs()

    def test_attribution_headers_are_inherited_but_extra_body_is_not(self):
        cfg = self._reload_with(
            '[llm]\nmodel = "m1"\napi_key_env = "K1"\n'
            'site_url = "http://site"\napp_name = "app"\n'
            '\n[llm.fallback]\nmodel = "m2"\napi_key_env = "K2"\n'
            '\n[llm.fallback.extra_body.provider]\nonly = ["deepseek"]\n'
        )
        primary, fallback = cfg.llm_endpoint_specs()
        self.assertEqual(fallback["site_url"], "http://site")
        self.assertEqual(fallback["app_name"], "app")
        self.assertEqual(fallback["extra_body"], {"provider": {"only": ["deepseek"]}})
        self.assertNotIn("extra_body", primary)


class EndpointCredentialTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self._tmp.cleanup()

    def test_env_credential_is_read_at_call_time(self):
        from mem0_local.llm import Endpoint

        endpoint = Endpoint(name="e", model="m", base_url="https://a/v1", api_key_env="TEST_KEY_X")
        os.environ.pop("TEST_KEY_X", None)
        with self.assertRaises(RuntimeError):
            endpoint.api_key()
        os.environ["TEST_KEY_X"] = "sk-live"
        try:
            self.assertEqual(endpoint.api_key(), "sk-live")
        finally:
            os.environ.pop("TEST_KEY_X", None)

    def test_json_credential_follows_a_dotted_path(self):
        from mem0_local.llm import Endpoint

        auth = Path(self._tmp.name) / "auth.json"
        auth.write_text(json.dumps({"vendor": {"key": "sk-from-file"}}))
        endpoint = Endpoint(
            name="e",
            model="m",
            base_url="https://a/v1",
            api_key_json=str(auth),
            api_key_json_path="vendor.key",
        )
        self.assertEqual(endpoint.api_key(), "sk-from-file")

    def test_missing_json_path_is_a_clear_error(self):
        from mem0_local.llm import Endpoint

        auth = Path(self._tmp.name) / "auth.json"
        auth.write_text(json.dumps({"other": {"key": "x"}}))
        endpoint = Endpoint(
            name="e",
            model="m",
            base_url="https://a/v1",
            api_key_json=str(auth),
            api_key_json_path="vendor.key",
        )
        with self.assertRaisesRegex(RuntimeError, "vendor.key"):
            endpoint.api_key()


class FallbackLLMTests(unittest.TestCase):
    def _fallback(self, llms: dict[str, _FakeLLM]):
        from mem0_local.llm import Endpoint, FallbackLLM

        endpoints = [
            Endpoint(name="primary", model="m1", base_url="https://a/v1", api_key_env="K1"),
            Endpoint(
                name="fallback",
                model="m2",
                base_url="https://b/v1",
                api_key_env="K2",
                extra_body={"provider": {"only": ["deepseek"]}},
            ),
        ]
        llm = FallbackLLM(endpoints, 4096)
        llm._llms = dict(llms)
        return llm

    def test_primary_serves_and_fallback_is_never_built(self):
        primary, fallback = _FakeLLM("m1"), _FakeLLM("m2")
        llm = self._fallback({"primary": primary, "fallback": fallback})
        self.assertEqual(llm.generate_response([]), "answer from m1")
        self.assertEqual(llm.active_model, "m1")
        self.assertEqual(fallback.calls, [])

    def test_primary_failure_falls_over_and_records_who_answered(self):
        primary, fallback = _FakeLLM("m1", fail=True), _FakeLLM("m2")
        llm = self._fallback({"primary": primary, "fallback": fallback})
        self.assertEqual(llm.generate_response([]), "answer from m2")
        self.assertEqual(llm.active_model, "m2")

    def test_endpoint_extra_body_is_merged_under_the_callers(self):
        primary, fallback = _FakeLLM("m1", fail=True), _FakeLLM("m2")
        llm = self._fallback({"primary": primary, "fallback": fallback})
        llm.generate_response([], extra_body={"reasoning": {"enabled": False}})
        self.assertEqual(
            fallback.calls[0]["extra_body"],
            {"provider": {"only": ["deepseek"]}, "reasoning": {"enabled": False}},
        )

    def test_caller_wins_on_conflicting_extra_body_keys(self):
        primary, fallback = _FakeLLM("m1", fail=True), _FakeLLM("m2")
        llm = self._fallback({"primary": primary, "fallback": fallback})
        llm.generate_response([], extra_body={"provider": {"only": ["other"]}})
        self.assertEqual(fallback.calls[0]["extra_body"], {"provider": {"only": ["other"]}})

    def test_primary_call_is_left_untouched_when_it_has_no_extra_body(self):
        primary, fallback = _FakeLLM("m1"), _FakeLLM("m2")
        llm = self._fallback({"primary": primary, "fallback": fallback})
        llm.generate_response([], extra_body={"reasoning": {"enabled": False}})
        self.assertEqual(primary.calls[0]["extra_body"], {"reasoning": {"enabled": False}})

    def test_empty_answer_counts_as_a_failure_and_falls_over(self):
        """A thinking model that spends max_tokens on reasoning returns 200
        with no content; the parse layer raises on it, so it must fall over."""
        primary, fallback = _FakeLLM("m1", answer="   \n "), _FakeLLM("m2")
        llm = self._fallback({"primary": primary, "fallback": fallback})
        self.assertEqual(llm.generate_response([]), "answer from m2")
        self.assertEqual(llm.active_model, "m2")

    def test_none_answer_counts_as_a_failure(self):
        primary, fallback = _FakeLLM("m1", answer=""), _FakeLLM("m2")
        llm = self._fallback({"primary": primary, "fallback": fallback})
        self.assertEqual(llm.generate_response([]), "answer from m2")

    def test_tool_call_dict_with_empty_content_is_a_valid_answer(self):
        answer = {"content": "", "tool_calls": [{"name": "t", "arguments": {}}]}
        primary, fallback = _FakeLLM("m1", answer=answer), _FakeLLM("m2")
        llm = self._fallback({"primary": primary, "fallback": fallback})
        self.assertEqual(llm.generate_response([]), answer)
        self.assertEqual(fallback.calls, [])

    def test_every_endpoint_returning_empty_raises(self):
        primary, fallback = _FakeLLM("m1", answer=""), _FakeLLM("m2", answer="")
        llm = self._fallback({"primary": primary, "fallback": fallback})
        with self.assertRaisesRegex(RuntimeError, "empty response"):
            llm.generate_response([])

    def test_every_endpoint_failing_raises_with_all_reasons(self):
        primary, fallback = _FakeLLM("m1", fail=True), _FakeLLM("m2", fail=True)
        llm = self._fallback({"primary": primary, "fallback": fallback})
        with self.assertRaises(RuntimeError) as ctx:
            llm.generate_response([])
        self.assertIn("m1 is down", str(ctx.exception))
        self.assertIn("m2 is down", str(ctx.exception))

    def test_failed_endpoint_client_is_discarded_for_the_next_call(self):
        primary, fallback = _FakeLLM("m1", fail=True), _FakeLLM("m2")
        llm = self._fallback({"primary": primary, "fallback": fallback})
        llm.generate_response([])
        self.assertNotIn("primary", llm._llms)

    def test_model_property_names_the_configured_preference(self):
        llm = self._fallback({"primary": _FakeLLM("m1"), "fallback": _FakeLLM("m2")})
        self.assertEqual(llm.model, "m1")

    def test_active_model_helper_falls_back_to_the_given_default(self):
        from mem0_local.llm import active_model

        self.assertEqual(active_model(object(), "configured"), "configured")
        llm = self._fallback({"primary": _FakeLLM("m1"), "fallback": _FakeLLM("m2")})
        llm.generate_response([])
        self.assertEqual(active_model(llm, "configured"), "m1")


if __name__ == "__main__":
    unittest.main()
