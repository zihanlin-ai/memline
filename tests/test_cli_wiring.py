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


def _commands():
    for name, obj in vars(cli).items():
        if callable(obj) and not name.startswith("_") and getattr(obj, "__module__", "") == cli.__name__:
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

    def test_names_called_in_a_command_are_imported_or_global(self):
        """A call to a name the function never imported is the same bug wearing
        a different hat: the import line was edited and the call site was not."""
        for name, func in _commands():
            source = textwrap.dedent(inspect.getsource(func))
            tree = ast.parse(source)
            imported = {a.asname or a.name
                        for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))
                        for a in n.names}
            assigned = {t.id for n in ast.walk(tree) if isinstance(n, ast.Assign)
                        for t in n.targets if isinstance(t, ast.Name)}
            # Commands define local helpers and call them; those are bound too.
            assigned |= {n.name for n in ast.walk(tree)
                         if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
            args = {a.arg for n in ast.walk(tree) if isinstance(n, ast.arguments) for a in
                    n.args + n.posonlyargs + n.kwonlyargs}
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                    called = node.func.id
                    if called in imported or called in assigned or called in args:
                        continue
                    with self.subTest(command=name, calls=called):
                        self.assertTrue(hasattr(cli, called) or hasattr(builtins, called),
                                        f"{name}() calls {called}(), which is neither imported "
                                        "nor defined at module level")


if __name__ == "__main__":
    unittest.main()
