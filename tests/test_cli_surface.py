"""The CLI surface, frozen: every command, flag and default, plus the scripts.

This exists to make one promise enforceable during internal refactoring: the
interface does not move. Modules may split, logic may sink out of the command
layer, files may become packages — and none of it is allowed to rename a
command, drop a flag, or change a default, because those are the things a
user's shell history and every skill document depend on.

The snapshot is the *rendered* surface — the click command tree typer builds,
the same one ``--help`` shows — not the source. A refactor that moves a
command function to another file leaves this file untouched; one that changes
what the user can type turns it red.

To bless an intentional interface change, regenerate and review the diff:

    REGEN_CLI_SURFACE=1 python -m unittest tests.test_cli_surface
"""

from __future__ import annotations

import json
import os
import unittest
from pathlib import Path

import click
import typer.main

SNAPSHOT = Path(__file__).with_name("cli_surface.json")


def _param(p: click.Parameter) -> dict:
    return {
        "name": p.name,
        "opts": sorted(p.opts) + sorted(p.secondary_opts),
        "required": bool(p.required),
        # repr, because defaults include Paths and enums; stability is what
        # matters here, not round-tripping.
        "default": repr(p.default() if callable(p.default) else p.default),
    }


def _walk(cmd: click.Command, path: str, out: dict) -> None:
    out[path or "<root>"] = [_param(p) for p in cmd.params if p.name != "help"]
    # Duck-typed: typer's TyperGroup is a Group from its own click compat
    # layer, which an isinstance against this interpreter's click misses.
    for name, sub in sorted(getattr(cmd, "commands", {}).items()):
        _walk(sub, f"{path} {name}".strip(), out)


def render_surface() -> dict:
    from memline import cli

    surface: dict = {}
    _walk(typer.main.get_command(cli.app), "", surface)

    scripts = {}
    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    in_scripts = False
    for line in pyproject.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("["):
            in_scripts = line == "[project.scripts]"
        elif in_scripts and "=" in line:
            name, _, target = line.partition("=")
            scripts[name.strip()] = target.strip().strip('"')
    return {"commands": surface, "scripts": scripts}


class CliSurfaceTest(unittest.TestCase):
    def test_the_surface_matches_the_blessed_snapshot(self):
        current = render_surface()
        if os.environ.get("REGEN_CLI_SURFACE") or not SNAPSHOT.is_file():
            SNAPSHOT.write_text(json.dumps(current, indent=1, sort_keys=True) + "\n",
                                encoding="utf-8")
            if not os.environ.get("REGEN_CLI_SURFACE"):
                self.fail("no snapshot existed; one was written — review and commit it")
            return
        blessed = json.loads(SNAPSHOT.read_text(encoding="utf-8"))

        cur_cmds, old_cmds = current["commands"], blessed["commands"]
        self.assertEqual(sorted(old_cmds), sorted(cur_cmds),
                         "commands appeared or vanished")
        for cmd in old_cmds:
            self.assertEqual(old_cmds[cmd], cur_cmds[cmd],
                             f"parameters of `{cmd}` changed")
        self.assertEqual(blessed["scripts"], current["scripts"],
                         "console scripts changed")

    def test_the_script_names_agents_type_still_exist(self):
        # The workspace skills and shell history call these by name; the
        # snapshot would catch a rename too, but this states the contract.
        scripts = render_surface()["scripts"]
        self.assertIn("memline", scripts)
        self.assertIn("memline-ingest-ledger", scripts)
        self.assertIn("memline-backfill-metadata", scripts)


if __name__ == "__main__":
    unittest.main()
