"""The shipped prompts must load, and the Domain section is configuration.

Regression: after profile.py moved into the wiki package, PROMPT_DIR silently
pointed at a directory that does not exist; nothing failed until the first
real profiling call. Loading every shipped prompt here keeps that path honest.
"""

from __future__ import annotations

import unittest
from unittest import mock

from memline import config
from memline.wiki.profile import DOMAIN_MARKER, PROMPT_DIR, default_prompt

SHIPPED = ("wiki-profile-session", "wiki-profile-source", "wiki-draft", "wiki-review")


class PromptLoadingTest(unittest.TestCase):
    def test_prompt_dir_exists(self):
        self.assertTrue(PROMPT_DIR.is_dir(), PROMPT_DIR)

    def test_every_shipped_prompt_loads_nonempty(self):
        for name in SHIPPED:
            with mock.patch.object(config, "WIKI_DOMAIN_PROFILE", ""):
                text = default_prompt(name)
            self.assertTrue(text.strip(), name)

    def test_configured_domain_is_injected(self):
        with mock.patch.object(config, "WIKI_DOMAIN_PROFILE", "Widget factory ops."):
            text = default_prompt("wiki-profile-session")
        self.assertIn("Widget factory ops.", text)
        self.assertNotIn(DOMAIN_MARKER, text)

    def test_without_domain_the_section_is_dropped_whole(self):
        with mock.patch.object(config, "WIKI_DOMAIN_PROFILE", ""):
            text = default_prompt("wiki-profile-session")
        self.assertNotIn("## Domain", text)
        self.assertNotIn(DOMAIN_MARKER, text)

    def test_no_marker_survives_in_any_shipped_prompt(self):
        for name in SHIPPED:
            with mock.patch.object(config, "WIKI_DOMAIN_PROFILE", "X"):
                self.assertNotIn(DOMAIN_MARKER, default_prompt(name), name)


if __name__ == "__main__":
    unittest.main()
