"""The client-construction path, which every command crosses before its work.

This module had no tests when a real failure ran through it: during the
mem0-local -> memline rename, the vendored mem0ai's editable install was
rewritten a few minutes after the old one died, and two `memline add` calls in
that window failed with `No module named 'mem0'`. The gates below are what
stood between that window and worse outcomes — a wrong package imported
silently, two processes sharing one local Qdrant path — so they are the parts
worth pinning, and all of them are testable without a store.
"""

from __future__ import annotations

import unittest
from unittest import mock

from memline import runtime


class VendoredMem0GateTest(unittest.TestCase):
    def test_the_official_package_is_refused_by_name(self):
        # The official build imports fine and breaks at runtime in non-obvious
        # ways; a loud refusal that names the fix beats any of them.
        with mock.patch("importlib.metadata.version", return_value="2.0.12"):
            with self.assertRaises(RuntimeError) as caught:
                runtime.check_vendored_mem0()
        self.assertIn("vendored", str(caught.exception))

    def test_the_workspace_build_passes(self):
        with mock.patch("importlib.metadata.version",
                        return_value="2.0.12+workspace.1"):
            runtime.check_vendored_mem0()

    def test_missing_metadata_defers_to_the_import(self):
        # No metadata is the mid-reinstall window. The import decides — and
        # fails with the module's own error, which is the truthful one.
        with mock.patch("importlib.metadata.version",
                        side_effect=Exception("no metadata")):
            runtime.check_vendored_mem0()


class NormalizeItemsTest(unittest.TestCase):
    def test_a_result_dict_yields_its_results_list(self):
        self.assertEqual(
            runtime.normalize_items({"results": [{"id": "a"}, {"id": "b"}]}),
            [{"id": "a"}, {"id": "b"}])

    def test_a_bare_list_passes_through(self):
        self.assertEqual(runtime.normalize_items([{"id": "a"}]), [{"id": "a"}])

    def test_non_dict_entries_are_dropped_not_raised(self):
        self.assertEqual(runtime.normalize_items([{"id": "a"}, "junk", None]),
                         [{"id": "a"}])

    def test_anything_else_is_an_empty_list(self):
        for value in (None, "text", 7, {"no_results_key": 1}):
            self.assertEqual(runtime.normalize_items(value), [])


class BuildConfigTest(unittest.TestCase):
    def test_the_placeholder_llm_is_the_real_primary(self):
        # If the validated config named an invented endpoint, a
        # construction-time error would point at a server that never runs a
        # call — the debugging trap the docstring warns about.
        spec = {"model": "m1", "base_url": "http://one", "site_url": None,
                "app_name": None, "env": "K", "timeout": 60}
        with mock.patch.object(runtime, "llm_endpoint_specs",
                               return_value=[spec]):
            config = runtime.build_config()
        self.assertEqual(config["llm"]["config"]["model"], "m1")
        self.assertEqual(config["llm"]["config"]["openai_base_url"], "http://one")
        self.assertEqual(
            config["reranker"]["config"]["llm"]["config"]["model"], "m1")

    def test_the_two_jobs_keep_their_separate_budgets(self):
        spec = {"model": "m", "base_url": "http://x", "site_url": None,
                "app_name": None, "env": "K", "timeout": 60}
        with mock.patch.object(runtime, "llm_endpoint_specs",
                               return_value=[spec]):
            config = runtime.build_config()
        self.assertEqual(config["llm"]["config"]["max_tokens"],
                         runtime.CLIENT_LLM_MAX_TOKENS)
        self.assertEqual(config["reranker"]["config"]["max_tokens"],
                         runtime.RERANKER_MAX_TOKENS)


class InstallLlmTest(unittest.TestCase):
    def test_both_consumers_get_their_own_chain(self):
        client = mock.Mock()
        client.reranker.llm = object()
        with mock.patch("memline.llm.build_llm",
                        side_effect=lambda tokens, job: (job, tokens)) as build:
            out = runtime.install_llm(client)
        self.assertIs(out, client)
        self.assertEqual(client.llm, ("infer", runtime.CLIENT_LLM_MAX_TOKENS))
        self.assertEqual(client.reranker.llm,
                         ("rerank", runtime.RERANKER_MAX_TOKENS))
        self.assertEqual(build.call_count, 2)

    def test_a_client_without_a_reranker_is_left_whole(self):
        client = mock.Mock(spec=["llm"])
        with mock.patch("memline.llm.build_llm", return_value="chain"):
            runtime.install_llm(client)
        self.assertEqual(client.llm, "chain")


if __name__ == "__main__":
    unittest.main()
