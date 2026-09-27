"""No test registers a by-path module in `sys.modules` except through the helper.

Issue #1449. `tests/test_evolution_reward.py` loaded `tools/evolution_reward.py`
by path and REPLACED the `sys.modules` entry `tools/evolution_controller.py` had
already imported, so a subclass check in `tests/test_evolution_controller.py`
failed whenever the reward tests ran first. Thirty-one more loaders in tests/
had the same four lines, several of them for a name another file also loads
(`gate_reference_census`, `formal_diff_corpus`). They now go through
`tests/_load_by_path.py`, which reuses an entry loaded from the same file.

The guard below reads every module in tests/ and fails on a new
`sys.modules[...] = <module_from_spec result>` outside the helper. The sites
it still allows are listed with the reason each one keeps its own loader.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

from _load_by_path import load_by_path

ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"

_BRIDGE = ("backends/python/bridge.py, a runtime module rather than a repo tool, "
           "under a name only this file uses; nothing else imports that name")
_RUNTIME = ("a copy of backends/python/runtime.py whose class-level state the "
            "test resets or relies on being its own; not a repo tool")

# (file, enclosing function) -> why it keeps an inline registration.
ALLOWED = {
    ("_load_by_path.py", "load_by_path"): "the helper itself",
    ("_backend_import.py", "backend_emitter"):
        "a caching loader for backend emitters under per-tier unique names; "
        "it returns the registered module before loading",
    ("test_tier_host_trace_secret_421_f6.py", "_backend"):
        "returns the registered module before loading, and needs the emitter's "
        "directory on sys.path while it executes",
    ("_net_gate_provider.py", "_bridge"): _BRIDGE,
    ("test_deploy_118.py", "_bridge"): _BRIDGE,
    ("test_hostile_wire_tck.py", "_bridge"): _BRIDGE,
    ("test_network_placement.py", "_bridge"): _BRIDGE,
    ("test_seam_admission.py", "_bridge"): _BRIDGE,
    ("test_seam_deadlines.py", "_bridge"): _BRIDGE,
    ("test_ts_correlation_seal.py", "_bridge"): _BRIDGE,
    ("test_insert_if_absent.py",
     "test_concurrency_exactly_one_true_on_the_reference_runtime"): _RUNTIME,
    ("test_time_coeffect.py", "rt"): _RUNTIME,
    ("test_time_coeffect.py", "test_emitted_python_body_reverts_cleanly"): _RUNTIME,
}


def _scopes(tree: ast.Module):
    yield "<module>", [n for n in tree.body if not isinstance(
        n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node.name, node.body


def _is_module_from_spec(node: ast.AST) -> bool:
    return (isinstance(node, ast.Call)
            and ast.unparse(node.func).endswith("module_from_spec"))


def by_path_registrations(source: str) -> list[tuple[str, int]]:
    """(enclosing function, line) of every `sys.modules[...] = X` where X is a
    `module_from_spec(...)` result, directly or through a name bound to one in
    the same function."""
    out = []
    for scope, body in _scopes(ast.parse(source)):
        nodes = [n for stmt in body for n in ast.walk(stmt)]
        bound = {ast.unparse(n.targets[0]) for n in nodes
                 if isinstance(n, ast.Assign) and len(n.targets) == 1
                 and _is_module_from_spec(n.value)}
        for n in nodes:
            if not isinstance(n, ast.Assign):
                continue
            if not any(isinstance(t, ast.Subscript) and ast.unparse(t.value) == "sys.modules"
                       for t in n.targets):
                continue
            if _is_module_from_spec(n.value) or ast.unparse(n.value) in bound:
                out.append((scope, n.lineno))
    return out


def test_no_new_inline_by_path_registration_in_tests():
    found = {}
    for path in sorted(TESTS.glob("*.py")):
        for scope, line in by_path_registrations(path.read_text(encoding="utf-8")):
            found.setdefault((path.name, scope), line)
    new = sorted(f"tests/{f}:{line} in {s}()" for (f, s), line in found.items()
                 if (f, s) not in ALLOWED)
    assert not new, (
        "a test registers a by-path module in sys.modules itself. Use "
        "`from _load_by_path import load_by_path`, which reuses the module "
        "already loaded from the same file instead of replacing it:\n  "
        + "\n  ".join(new))
    stale = sorted(f"{f}::{s}" for f, s in ALLOWED if (f, s) not in found)
    assert not stale, f"allowed sites that no longer exist: {stale}"


def test_the_guard_sees_the_inline_shape_it_replaced():
    """Non-vacuity: the exact four lines the old loaders used."""
    source = (
        "import importlib.util, sys\n"
        "def _tool():\n"
        "    spec = importlib.util.spec_from_file_location('t', 'tools/t.py')\n"
        "    module = importlib.util.module_from_spec(spec)\n"
        "    sys.modules[spec.name] = module\n"
        "    spec.loader.exec_module(module)\n"
        "    return module\n")
    assert by_path_registrations(source) == [("_tool", 5)]


def _write(tmp_path: Path, name: str, body: str) -> Path:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


@pytest.fixture
def scratch_name():
    name = "revl_load_by_path_probe"
    previous = sys.modules.pop(name, None)
    yield name
    sys.modules.pop(name, None)
    if previous is not None:
        sys.modules[name] = previous


def test_the_same_file_is_one_module_and_one_class(tmp_path, scratch_name):
    path = _write(tmp_path, "probe.py", "class Verdict:\n    pass\n")
    first = load_by_path(scratch_name, path)
    second = load_by_path(scratch_name, path)
    assert first is second
    assert first.Verdict is second.Verdict
    assert sys.modules[scratch_name] is first


def test_a_different_file_under_the_name_is_loaded(tmp_path, scratch_name):
    one = load_by_path(scratch_name, _write(tmp_path, "one.py", "WHO = 1\n"))
    two = load_by_path(scratch_name, _write(tmp_path, "two.py", "WHO = 2\n"))
    assert (one.WHO, two.WHO) == (1, 2)
    assert sys.modules[scratch_name] is two


def test_a_module_that_fails_to_execute_leaves_the_entry_as_it_was(
        tmp_path, scratch_name):
    good = load_by_path(scratch_name, _write(tmp_path, "good.py", "OK = 1\n"))
    with pytest.raises(ZeroDivisionError):
        load_by_path(scratch_name, _write(tmp_path, "bad.py", "1 / 0\n"))
    assert sys.modules[scratch_name] is good
