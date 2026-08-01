"""One config, six jobs, and no identity that came from the code.

The judges, the reranker, the wiki's profiling, its drafting and its audit all
read the same file and must not share a model choice, a reasoning budget, or a
fallback vendor by accident. These tests pin the resolution rules that keep
them apart — the inheritance that keeps a job from restating the whole
endpoint, and the two things inheritance must never carry.
"""

from __future__ import annotations

import unittest
from unittest import mock

from mem0_local import config


def resolve(llm_section, job=None):
    with mock.patch.object(config, "section", lambda name: llm_section if name == "llm" else {}):
        return config.llm_endpoint_specs(job)


def knobs(llm_section, job=None):
    with mock.patch.object(config, "section", lambda name: llm_section if name == "llm" else {}):
        return config.llm_knobs(job)


BASE = {
    "base_url": "https://gateway.example/v1",
    "api_key_env": "KEY",
    "app_name": "mem0",
    "extra_body": {"reasoning": {"enabled": False}},
    "judge": {
        "model": "judge-model",
        "fallback": {"model": "judge-fallback", "base_url": "https://other/v1",
                     "api_key_env": "K2"},
    },
    "draft": {
        "model": "draft-model",
        "stream": True,
        "extra_body": {"reasoning_effort": "medium"},
    },
    "review": {"model": "review-model"},
}


class JobTest(unittest.TestCase):
    def test_each_job_selects_its_own_model(self):
        self.assertEqual(resolve(BASE, "draft")[0]["model"], "draft-model")
        self.assertEqual(resolve(BASE, "review")[0]["model"], "review-model")

    def test_job_inherits_the_shared_endpoint_it_does_not_state(self):
        spec = resolve(BASE, "review")[0]
        self.assertEqual(spec["base_url"], "https://gateway.example/v1")
        self.assertEqual(spec["api_key_env"], "KEY")
        self.assertEqual(spec["app_name"], "mem0")

    def test_job_extra_body_replaces_rather_than_merges(self):
        # A half-merged reasoning config is worse than either: the caller would
        # get one job's effort with the other's flags.
        self.assertEqual(resolve(BASE, "draft")[0]["extra_body"],
                         {"reasoning_effort": "medium"})
        self.assertEqual(resolve(BASE, "review")[0]["extra_body"],
                         {"reasoning": {"enabled": False}})

    def test_a_job_never_inherits_another_jobs_fallback(self):
        # The failure this prevents: drafting quietly falls through to the
        # cheap model chosen for judging and returns a thinner article that
        # still reads as a success.
        self.assertEqual([s["name"] for s in resolve(BASE, "draft")], ["primary"])
        self.assertEqual([s["model"] for s in resolve(BASE, "judge")],
                         ["judge-model", "judge-fallback"])

    def test_shared_fallback_is_not_inherited_either(self):
        shared = {**BASE, "fallback": {"model": "shared-fb", "base_url": "https://x/v1",
                                       "api_key_env": "K3"}}
        self.assertEqual([s["name"] for s in resolve(shared, "draft")], ["primary"])

    def test_a_job_that_names_no_model_anywhere_fails_loudly(self):
        # No built-in model, no built-in vendor: an unnamed identity is an
        # error, not a default. Before this, a missing table resolved to
        # whatever the code happened to ship with.
        thin = {"base_url": "https://gateway.example/v1", "api_key_env": "KEY",
                "profile": {}}
        with self.assertRaises(config.ConfigError) as caught:
            resolve(thin, "profile")
        self.assertIn("model", str(caught.exception))

    def test_a_job_naming_a_file_credential_does_not_inherit_the_env_one(self):
        # Endpoint.api_key consults api_key_env first, so inheriting the shared
        # env variable makes a file-credentialled job send the *other*
        # endpoint's key. It shows up as a 401 from a relay whose token is
        # sitting correctly on disk.
        mixed = {**BASE, "review": {"model": "m", "base_url": "http://relay/v1",
                                    "api_key_json": "~/auth.json",
                                    "api_key_json_path": "vendor.key"}}
        spec = resolve(mixed, "review")[0]
        self.assertNotIn("api_key_env", spec)
        self.assertEqual(spec["api_key_json"], "~/auth.json")

    def test_a_job_naming_an_env_credential_does_not_inherit_a_file_one(self):
        shared = {"base_url": "https://gw/v1", "api_key_json": "~/auth.json",
                  "api_key_json_path": "vendor.key",
                  "draft": {"model": "m", "api_key_env": "KEY"}}
        spec = resolve(shared, "draft")[0]
        self.assertEqual(spec["api_key_env"], "KEY")
        self.assertNotIn("api_key_json", spec)
        self.assertNotIn("api_key_json_path", spec)

    def test_a_job_naming_no_credential_still_inherits_the_shared_one(self):
        self.assertEqual(resolve(BASE, "review")[0]["api_key_env"], "KEY")

    def test_missing_credential_is_named_as_the_missing_key(self):
        thin = {"base_url": "https://gateway.example/v1", "draft": {"model": "m"}}
        with self.assertRaises(config.ConfigError) as caught:
            resolve(thin, "draft")
        self.assertIn("api_key_env", str(caught.exception))

    def test_an_unknown_job_raises_instead_of_resolving_to_something_else(self):
        # A typo used to fall through to [llm] so the caller was never
        # disabled. That traded a loud failure for a silent bill on a model
        # nobody picked for the work.
        with self.assertRaises(config.ConfigError) as caught:
            resolve(BASE, "wiki")
        self.assertIn("known jobs", str(caught.exception))

    def test_every_declared_job_is_resolvable_from_the_shipped_config(self):
        # Guards the rename that adds a job to LLM_JOBS and forgets the table.
        for job in config.LLM_JOBS:
            with self.subTest(job=job):
                self.assertTrue(config.llm_endpoint_specs(job)[0]["model"])

    def test_draft_and_review_do_not_share_a_model(self):
        # An audit that runs on the writer's own model shares its blind spots.
        self.assertNotEqual(config.llm_endpoint_specs("draft")[0]["model"],
                            config.llm_endpoint_specs("review")[0]["model"])


class KnobTest(unittest.TestCase):
    """Budgets and patience may default in code; identities may not."""

    def test_absent_knobs_leave_the_caller_in_charge(self):
        self.assertEqual(knobs(BASE, "draft"), {})

    def test_a_job_may_state_its_own_budget(self):
        tuned = {**BASE, "draft": {**BASE["draft"], "max_tokens": 200000, "timeout": 600.0}}
        self.assertEqual(knobs(tuned, "draft"), {"max_tokens": 200000, "timeout": 600.0})

    def test_knobs_are_not_mistaken_for_endpoint_fields(self):
        tuned = {**BASE, "draft": {**BASE["draft"], "max_tokens": 200000}}
        self.assertNotIn("max_tokens", resolve(tuned, "draft")[0])


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


if __name__ == "__main__":
    unittest.main()
