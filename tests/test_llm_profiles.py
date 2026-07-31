"""Endpoint profiles: one config, several kinds of caller.

The judges and the wiki pipeline read the same file but must not share a
model choice, a reasoning budget, or a fallback vendor. These tests pin the
resolution rules that keep them apart — and the inheritance that keeps a
profile from having to restate the whole endpoint.
"""

from __future__ import annotations

import unittest
from unittest import mock

from mem0_local import config


def resolve(llm_section, profile=None):
    with mock.patch.object(config, "section", lambda name: llm_section if name == "llm" else {}):
        return config.llm_endpoint_specs(profile)


BASE = {
    "model": "judge-model",
    "base_url": "http://relay.example/v1",
    "api_key_env": "KEY",
    "app_name": "mem0",
    "extra_body": {"reasoning_effort": "low"},
    "fallback": {"model": "judge-fallback", "base_url": "https://other/v1", "api_key_env": "K2"},
    "wiki": {
        "model": "wiki-model",
        "stream": True,
        "extra_body": {"reasoning_effort": "high"},
        "fallback": {"model": "wiki-fallback", "base_url": "https://other/v1", "api_key_env": "K2"},
    },
}


class ProfileTest(unittest.TestCase):
    def test_default_profile_is_untouched_by_a_configured_one(self):
        specs = resolve(BASE)
        self.assertEqual([s["model"] for s in specs], ["judge-model", "judge-fallback"])
        self.assertEqual(specs[0]["extra_body"], {"reasoning_effort": "low"})

    def test_profile_selects_its_own_model_and_fallback(self):
        specs = resolve(BASE, "wiki")
        self.assertEqual([s["model"] for s in specs], ["wiki-model", "wiki-fallback"])

    def test_profile_extra_body_replaces_rather_than_merges(self):
        # A half-merged reasoning config is worse than either: the caller would
        # get one profile's effort with the other's flags.
        self.assertEqual(resolve(BASE, "wiki")[0]["extra_body"], {"reasoning_effort": "high"})

    def test_profile_inherits_scalars_it_does_not_state(self):
        specs = resolve(BASE, "wiki")
        self.assertEqual(specs[0]["base_url"], "http://relay.example/v1")
        self.assertEqual(specs[0]["api_key_env"], "KEY")

    def test_unknown_profile_falls_back_to_the_default_endpoints(self):
        # A typo in a profile name must not silently disable the caller.
        self.assertEqual([s["model"] for s in resolve(BASE, "nope")],
                         [s["model"] for s in resolve(BASE)])

    def test_profile_may_override_only_the_model(self):
        thin = {**BASE, "wiki": {"model": "just-a-model"}}
        specs = resolve(thin, "wiki")
        self.assertEqual(specs[0]["model"], "just-a-model")
        self.assertEqual(specs[0]["base_url"], "http://relay.example/v1")
        self.assertEqual([s["model"] for s in specs][1], "judge-fallback")


if __name__ == "__main__":
    unittest.main()


class FallbackVisibilityTest(unittest.TestCase):
    """A fallback that succeeds is still a primary that failed."""

    def test_result_carries_why_earlier_endpoints_were_skipped(self):
        from mem0_local.relay import CallResult
        r = CallResult(text="{}", endpoint="fallback", model="m", attempt=1, seconds=1.0,
                       earlier_failures=["primary attempt 1: APIConnectionError"])
        self.assertEqual(r.provenance["earlier_failures"],
                         ["primary attempt 1: APIConnectionError"])

    def test_provenance_of_a_clean_primary_call_is_empty_not_absent(self):
        from mem0_local.relay import CallResult
        r = CallResult(text="{}", endpoint="primary", model="m", attempt=1, seconds=1.0)
        self.assertEqual(r.provenance["earlier_failures"], [])
