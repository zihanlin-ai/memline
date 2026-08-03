"""Every CLI command's deferred imports actually resolve.

Commands import their modules inside the function body to keep startup fast,
which means a renamed function is invisible to both the linter and the unit
tests: `wiki review-draft` shipped calling a name its import line did not
bring in, and the failure surfaced only as a NameError in front of a user
whose audit had already been paid for.

This walks each command's source for the names it imports from memline and
the module-level names it calls, and checks they exist. It is cheap, and it
catches the one class of break that testing modules in isolation cannot.
"""

from __future__ import annotations

import ast
import builtins
import importlib
import inspect
import textwrap
import unittest

from memline import cli
from memline.cli import _support, daemon, entity, events, memory, stale, wiki

_COMMAND_MODULES = (memory, stale, wiki, daemon, events, entity, _support)


def _commands():
    for module in _COMMAND_MODULES:
        for name, obj in vars(module).items():
            if callable(obj) and not name.startswith("_")                     and getattr(obj, "__module__", "") == module.__name__:
                try:
                    inspect.getsource(obj)
                except (OSError, TypeError):
                    continue
                yield name, obj


class DeferredImportTest(unittest.TestCase):
    def test_every_deferred_import_names_something_that_exists(self):
        checked = 0
        for name, func in _commands():
            tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
            for node in ast.walk(tree):
                if not isinstance(node, ast.ImportFrom) or not (node.module or "").startswith("memline"):
                    continue
                module = importlib.import_module(node.module)
                for alias in node.names:
                    with self.subTest(command=name, imports=f"{node.module}.{alias.name}"):
                        self.assertTrue(hasattr(module, alias.name),
                                        f"{name}() imports {alias.name} from {node.module}, "
                                        "which does not define it")
                        checked += 1
        self.assertGreater(checked, 20, "the walker stopped finding commands")

    def test_names_used_in_a_command_are_imported_or_global(self):
        """A name the function never bound is the same bug wearing a different
        hat: the import line was edited and the use site was not. Checked for
        every Load-context name, not just calls — `os.environ[...]` shipped
        broken once because the caller-only version of this test could not see
        an attribute root."""
        for name, func in _commands():
            source = textwrap.dedent(inspect.getsource(func))
            tree = ast.parse(source)
            bound = {a.asname or a.name
                     for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))
                     for a in n.names}
            # Everything a function body can bind: assignments, loop and with
            # targets, comprehension variables, walrus, local defs, exceptions.
            for n in ast.walk(tree):
                if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
                    bound.add(n.id)
                elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    bound.add(n.name)
                elif isinstance(n, ast.ExceptHandler) and n.name:
                    bound.add(n.name)
                elif isinstance(n, ast.arguments):
                    for a in n.args + n.posonlyargs + n.kwonlyargs +                             [x for x in (n.vararg, n.kwarg) if x]:
                        bound.add(a.arg)
            for node in ast.walk(tree):
                if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                    used = node.id
                    if used in bound:
                        continue
                    # A function resolves globals from the module it lives
                    # in, so that module is the only namespace worth checking.
                    home = importlib.import_module(func.__module__)
                    with self.subTest(command=name, uses=used):
                        self.assertTrue(hasattr(home, used) or hasattr(builtins, used),
                                        f"{name}() uses {used}, which is neither imported "
                                        "nor defined at module level")


if __name__ == "__main__":
    unittest.main()
