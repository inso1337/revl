"""A module-scope skip must not strand tests that were already defined.

`pytest.importorskip(...)` (and `pytest.skip(..., allow_module_level=True)`,
and a module-level `pytestmark`) abort *collection* of the file they sit in.
Placed at the top, before anything is defined, that is exactly right: the
whole module needs the thing, so the whole module skips. Placed halfway down,
it is a trap — every test defined above it stops being collected too, and the
file reports `0 items collected / 1 skipped`, which reads as "nothing to run
here" rather than "eleven tests were silently deleted".

That is not hypothetical. `tests/test_mcp_session.py` carried an
`importorskip("cordis")` on line 102 with five pure in-memory tests above it,
so on every machine without the cordis-py runtime — including the default
`.venv` this suite installs — the entire file collected zero tests. Nothing
said so; `pytest -q` printed a dot for the skip and moved on.

The rule this file pins is narrow on purpose: a module-scope skip is fine, as
long as no test is defined above it. Gate the individual tests with a marker
(`@pytest.mark.skipif(...)`) when only some of them need the dependency.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
FILES = sorted(p for p in TESTS.glob("test_*.py") if p.name != Path(__file__).name)

# Calls that abort collection of the module they appear in, at module scope.
ABORTING = {"importorskip", "skip", "exit"}


def _module_scope_aborts(tree: ast.Module) -> list[tuple[int, str]]:
    found = []
    for node in tree.body:  # module scope only — not nested in a def/class
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
            call = node.value.func
            if (isinstance(call, ast.Attribute) and call.attr in ABORTING
                    and isinstance(call.value, ast.Name) and call.value.id == "pytest"):
                found.append((node.lineno, f"pytest.{call.attr}(...)"))
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "pytestmark":
                    found.append((node.lineno, "pytestmark = ..."))
    return found


def _tests_defined_before(tree: ast.Module, line: int) -> list[str]:
    return [node.name for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            and node.name.startswith(("test_", "Test"))
            and node.lineno < line]


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_no_module_scope_skip_strands_earlier_tests(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
    for line, form in _module_scope_aborts(tree):
        stranded = _tests_defined_before(tree, line)
        assert not stranded, (
            f"{path.name}:{line}: `{form}` at module scope aborts collection of "
            f"the whole file, so these {len(stranded)} test(s) defined above it "
            f"never run — on any machine where it fires, the file reports "
            f"`0 items collected`:\n  " + "\n  ".join(stranded)
            + "\n\nMove the guard above every definition if the module really "
              "needs it wholesale, or turn it into a marker on the tests that "
              "do:\n"
              "  needs_runtime = pytest.mark.skipif(\n"
              "      importlib.util.find_spec('cordis') is None, reason=...)\n"
              "  @needs_runtime\n"
              "  def test_...():")


def test_the_sweep_sees_the_suite():
    """A glob that stops matching would make every assertion above vacuous."""
    assert len(FILES) >= 15, f"only {len(FILES)} test modules found"
