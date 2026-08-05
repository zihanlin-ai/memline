"""Deterministic non-Latin gate on raw memory adds."""

from __future__ import annotations

import unittest
from unittest.mock import patch

import click

from memline import cli
from memline.config import MAX_RAW_TEXT_CHARS


class RawLanguageGateTests(unittest.TestCase):
    def test_latin_symbols_and_emoji_pass(self) -> None:
        text = "Café déjà vu: /srv/model-v2 → ready 🚀"
        self.assertEqual(cli.non_latin_letters(text), [])
        self.assertEqual(cli.check_raw_language(text), [])

    def test_non_latin_letters_are_rejected_with_force_hint(self) -> None:
        with self.assertRaises(click.ClickException) as raised:
            cli.check_raw_language("2026-08-03 已完成 test")
        message = str(raised.exception)
        self.assertIn("Non-English input detected", message)
        self.assertIn("Rewrite the memory in English", message)
        self.assertIn("--force", message)

    def test_other_non_latin_scripts_are_detected(self) -> None:
        for text in ("Δelta result", "Готово", "テスト complete", "테스트 complete"):
            with self.subTest(text=text):
                self.assertTrue(cli.non_latin_letters(text))

    def test_force_bypasses_only_the_language_gate(self) -> None:
        detected = cli.check_raw_language("必须保留的中文文件名", force=True)
        self.assertTrue(detected)

    def test_forced_add_reaches_store_and_audits_the_override(self) -> None:
        result = {"results": [{"id": "memory-1", "memory": "必须保留", "event": "ADD"}]}
        with (
            patch.object(cli._support, "audited") as audited,
            patch.object(cli._support, "execute", return_value=result) as execute,
            patch.object(cli._support, "output"),
            patch("memline.queue.EventQueue") as event_queue,
        ):
            cli.add(
                text="必须保留",
                user_id="workspace",
                agent_id="codex",
                app_id=None,
                run_id="session-1",
                messages=None,
                file=None,
                metadata=[],
                timestamp=None,
                ledger_timestamp=None,
                infer_opt=None,
                supersedes=None,
                force=True,
                wait=False,
                json_flag=True,
                output_format="json",
            )

        execute.assert_called_once()
        self.assertEqual(execute.call_args.args[1]["content"], "必须保留")
        audit_input = audited.call_args.kwargs["input_payload"]
        self.assertTrue(audit_input["language_override"])
        self.assertEqual(audit_input["non_latin_character_count"], 4)
        event_queue.return_value.enqueue.assert_called_once()

    def test_add_rejects_before_store_daemon_or_audit_work(self) -> None:
        with (
            patch.object(cli._support, "audited") as audited,
            patch.object(cli._support, "execute") as execute,
            patch.object(cli._support, "maybe_daemon_request") as daemon_request,
        ):
            with self.assertRaises(click.ClickException):
                cli.add(
                    text="中文记忆",
                    user_id="workspace",
                    agent_id=None,
                    app_id=None,
                    run_id=None,
                    messages=None,
                    file=None,
                    metadata=[],
                    timestamp=None,
                    ledger_timestamp=None,
                    infer_opt=None,
                    supersedes=None,
                    force=False,
                    wait=False,
                    json_flag=True,
                    output_format="json",
                )
        audited.assert_not_called()
        execute.assert_not_called()
        daemon_request.assert_not_called()

    def test_force_cannot_bypass_raw_length_cap(self) -> None:
        with self.assertRaises(click.ClickException) as raised:
            cli.add(
                text="中" + "x" * MAX_RAW_TEXT_CHARS,
                user_id="workspace",
                agent_id=None,
                app_id=None,
                run_id=None,
                messages=None,
                file=None,
                metadata=[],
                timestamp=None,
                ledger_timestamp=None,
                infer_opt=None,
                supersedes=None,
                force=True,
                wait=False,
                json_flag=True,
                output_format="json",
            )
        self.assertIn("hard cap", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
