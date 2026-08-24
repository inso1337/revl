"""`lifecycle test` executed on the real cordis-py runtime (§7.1).

The proof of this feature is execution, so these tests drive `revl test`
through the backend's own venv — the one with cordis-py installed — and check
both directions: a composition that reverts cleanly passes, and a component
whose `undo` is not the inverse of its acquisition is *caught*. An assertion
that can only pass is not an assertion.

Set up the runtime with `sh backends/python/setup.sh`; without it these skip
(with a reason — never reported as passing).
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples"
CORDIS_PY = ROOT / "backends" / "python" / ".venv" / "bin" / "python"

pytestmark = pytest.mark.skipif(
    not CORDIS_PY.exists(),
    reason="cordis-py runtime not installed (run `sh backends/python/setup.sh`)")


def _revl_test(*args: str) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run([str(CORDIS_PY), "-m", "revl", "test", *args],
                          cwd=ROOT, env=env, capture_output=True, text=True, timeout=300)


def test_a_composition_that_reverts_cleanly_passes():
    result = _revl_test(str(EXAMPLES / "lifecycle_cache.rvl"))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASS cache reverts cleanly" in result.stdout
    assert "PASS a reloaded cache starts empty" in result.stdout
    assert "[py] pass: 2 test(s) passed" in result.stdout


def test_a_leaky_undo_is_caught():
    """examples/lifecycle_leak.rvl passes every static check — G4 sees an
    acquisition with an `undo` and cannot know it is not the inverse. The
    lifecycle assertion catches it at runtime."""
    result = _revl_test(str(EXAMPLES / "lifecycle_leak.rvl"))
    assert result.returncode == 1
    assert "FAIL a leaky undo leaves residue" in result.stdout
    assert "host resources never released" in result.stdout
    assert "open() with no close()" in result.stdout
    assert "(R1)" in result.stdout


def test_a_composition_left_loaded_is_caught(tmp_path):
    """The other half of `no_residue`: R4's introspection delta, which fires
    when the test itself forgets to unload something."""
    source = (EXAMPLES / "lifecycle_cache.rvl").read_text(encoding="utf-8")
    head, _, _ = source.partition('lifecycle test "a reloaded cache starts empty"')
    forgetful = head.replace("  unload PgDatabase\n", "")
    path = tmp_path / "forgetful.rvl"
    path.write_text(forgetful, encoding="utf-8")

    result = _revl_test(str(path))
    assert result.returncode == 1
    assert "host runtime still holds" in result.stdout
    assert "provisions: [] -> ['db']" in result.stdout
    assert "(R4)" in result.stdout


def test_the_composition_really_ran(tmp_path):
    """Guard against a driver that emits an inert test: the assertions inside
    the body must actually observe the live composition."""
    source = (EXAMPLES / "lifecycle_cache.rvl").read_text(encoding="utf-8")
    head, _, _ = source.partition('lifecycle test "a reloaded cache starts empty"')
    wrong = head.replace('assert hit == Some("v")', 'assert hit == Some("WRONG")')
    path = tmp_path / "wrong.rvl"
    path.write_text(wrong, encoding="utf-8")

    result = _revl_test(str(path))
    assert result.returncode == 1
    assert "FAIL cache reverts cleanly" in result.stdout
    assert "assertion failed" in result.stdout


def test_pure_and_lifecycle_tests_coexist_in_one_document(tmp_path):
    source = (EXAMPLES / "lifecycle_cache.rvl").read_text(encoding="utf-8")
    path = tmp_path / "mixed.rvl"
    path.write_text(source + '\ntest "pure still works" { assert 1 + 1 == 2 }\n',
                    encoding="utf-8")

    result = _revl_test(str(path))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "[py] pass: 3 test(s) passed" in result.stdout
