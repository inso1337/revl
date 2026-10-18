"""A go lifecycle test that stops early leaves nothing for the next one (issue #2505).

On the go tier the host resource counter and the clock's timers are
package-global. A lifecycle test that stopped before its `unload` steps (a
failed `assert`, a failed load, a panic recovered since #2481) never released
what its components acquired, so the NEXT lifecycle test in the document failed
`assert no_residue` on a timer it never armed.

py's driver disposes, LIFO, every fiber the test loaded and did not unload, in
the `finally` around the test body (backends/python/emit.py
`_emit_lifecycle_test`), the failed-load fiber included. ts does the same and
also judges residue against a per-test baseline; rust keeps its counter
thread-local and `cargo test` runs each test on its own thread. The go driver
now defers the same LIFO dispose.

Every run test below has a survivor after the test that stops early. It loads
and unloads `Agent`, which holds nothing, and asserts `no_residue`. On base each
survivor failed with `residue — 0 fiber(s), 1 host resource(s)`.

The run tests build and run the emitted package with the real toolchain.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.compiler import compile_source  # noqa: E402

GO = shutil.which("go")
GO_SCENARIOS = ROOT / "backends" / "go" / "scenarios"


def _go_emit():
    spec = importlib.util.spec_from_file_location(
        "revl_go_emit_2505", ROOT / "backends" / "go" / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_lifecycle(tmp_path: Path, source: str, package: str) -> str:
    """Emit `source` (its lifecycle tests become go tests) into a copy of the
    scenarios module and run every test in it, in document order."""
    src = _go_emit().emit(compile_source(source, f"{package}.rvl"), package)
    if GO is None:
        pytest.skip("go toolchain not available")
    module = tmp_path / "mod"
    module.mkdir()
    for name in ("go.mod", "go.sum"):
        shutil.copy(GO_SCENARIOS / name, module / name)
    (module / package).mkdir()
    (module / package / f"gen_{package}_test.go").write_text(src, encoding="utf-8")
    env = {**os.environ, "GOFLAGS": "-mod=mod", "GOPROXY": "off"}
    argv = [GO, "test", "-count=1", "-v", "-run", ".", f"./{package}/"]
    proc = subprocess.run(argv, cwd=str(module), capture_output=True, text=True, env=env)
    return proc.stdout + proc.stderr


#: `Ticker` arms a timer, the host resource the issue's count named. `Agent`'s
#: `run` panics, so a call to it, a timer body that calls it, or an activation
#: that emits it each stop a test early in a different way.
COMPONENTS = """
extern emission fn boom() -> Int = @go {
	panic("the provider gave up")
}

service Ops {
  emission fn run(region: Str) -> Int
  emission fn ping() -> Int
}

component Agent provides ops: Ops {
  provide ops {
    fn run(region) { return emit boom() }
    fn ping() { return 1 }
  }
}

component Ticker requires ops: Ops {
  every 10ms { emit ops.ping() }
}

component PanicTicker requires ops: Ops {
  every 10ms { emit ops.run("tick") }
}

component Broken requires ops: Ops {
  emit ops.run("load")
}

component ArmsThenFails requires ops: Ops {
  every 10ms { emit ops.ping() }
  emit ops.run("load")
}
"""

SURVIVOR = """
lifecycle test "holds nothing" {
  load Agent
  unload Agent
  assert no_residue
}
"""

SURVIVOR_PASSED = "--- PASS: TestHoldsNothing"


def _doc(first: str) -> str:
    return COMPONENTS + first + SURVIVOR


# ---- the issue's repro: a failed `assert` --------------------------------

def test_a_failed_assert_leaves_nothing_for_the_next_test(tmp_path):
    output = _run_lifecycle(tmp_path, _doc("""
lifecycle test "fails before unload" {
  load Agent
  load Ticker
  assert 1 == 2
  unload Ticker
  unload Agent
}
"""), "lassert")
    assert 'lifecycle test "fails before unload": assertion failed' in output, output
    assert "--- FAIL: TestFailsBeforeUnload" in output, output
    assert SURVIVOR_PASSED in output, output
    assert "residue" not in output, output


# ---- a failed load: the loads before it stay behind ------------------------

def test_a_failed_load_leaves_nothing_for_the_next_test(tmp_path):
    output = _run_lifecycle(tmp_path, _doc("""
lifecycle test "a failed load" {
  load Agent
  load Ticker
  load Broken
  unload Broken
  unload Ticker
  unload Agent
}
"""), "lload")
    assert ('lifecycle test "a failed load": load Broken: '
            'panic: the provider gave up') in output, output
    assert "--- FAIL: TestAFailedLoad" in output, output
    assert SURVIVOR_PASSED in output, output
    assert "residue" not in output, output


def test_a_load_that_arms_a_timer_then_fails_leaves_nothing_for_the_next_test(tmp_path):
    """The failing component itself armed a timer before its activation
    failed. The fiber ends `Failed`, not `Gone`; disposing it must still
    release that timer, or the survivor counts it."""
    output = _run_lifecycle(tmp_path, _doc("""
lifecycle test "arms then fails" {
  load Agent
  load ArmsThenFails
  unload ArmsThenFails
  unload Agent
}
"""), "larms")
    assert ('lifecycle test "arms then fails": load ArmsThenFails: '
            'panic: the provider gave up') in output, output
    assert "--- FAIL: TestArmsThenFails" in output, output
    assert SURVIVOR_PASSED in output, output
    assert "residue" not in output, output


# ---- a panic recovered since #2481 ----------------------------------------

def test_a_recovered_call_panic_leaves_nothing_for_the_next_test(tmp_path):
    output = _run_lifecycle(tmp_path, _doc("""
lifecycle test "a panicking call" {
  load Agent
  load Ticker
  call ops.run("r")
  unload Ticker
  unload Agent
}
"""), "lcall")
    assert ('lifecycle test "a panicking call": call ops.run: '
            'panic: the provider gave up') in output, output
    assert "--- FAIL: TestAPanickingCall" in output, output
    assert SURVIVOR_PASSED in output, output
    assert "residue" not in output, output


def test_a_recovered_timer_panic_leaves_nothing_for_the_next_test(tmp_path):
    output = _run_lifecycle(tmp_path, _doc("""
lifecycle test "a panicking timer" {
  load Agent
  load PanicTicker
  advance 10ms
  unload PanicTicker
  unload Agent
}
"""), "ltimer")
    assert ('lifecycle test "a panicking timer": advance 10ms: '
            'panic: the provider gave up') in output, output
    assert "--- FAIL: TestAPanickingTimer" in output, output
    assert SURVIVOR_PASSED in output, output
    assert "residue" not in output, output


# ---- positive control: the teardown does not hide a real leak --------------

def test_a_test_that_really_leaks_still_fails_no_residue(tmp_path):
    """The leftover dispose runs after the test body, so a test whose own
    `assert no_residue` sees what it still holds keeps failing on it, and the
    survivor after it still passes."""
    output = _run_lifecycle(tmp_path, _doc("""
lifecycle test "really leaks" {
  load Agent
  load Ticker
  assert no_residue
}
"""), "lleak")
    assert ('lifecycle test "really leaks": residue — 2 fiber(s), '
            '1 host resource(s) (R4/R1)') in output, output
    assert "--- FAIL: TestReallyLeaks" in output, output
    assert SURVIVOR_PASSED in output, output


# ---- emitted shape ---------------------------------------------------------

def test_the_leftover_dispose_is_deferred_before_the_recover():
    """Go runs defers LIFO: deferring the dispose FIRST makes it run LAST, after
    the #2481 recover has already turned a panic into the test's failure. And
    a load is tracked before `Ready`, so a failed load is disposed too."""
    doc = _doc("""
lifecycle test "a failed load" {
  load Agent
  load Broken
}
""")
    src = _go_emit().emit(compile_source(doc, "shape.rvl"), "shape")
    test = src[src.index("func TestAFailedLoad("):]
    test = test[:test.index("\n}\n")]
    dispose = test.index("_f.Dispose()")
    recover = test.index("if r := recover(); r != nil {")
    assert dispose < recover, test
    load = test[test.index('_revlAt = "load Broken"'):]
    assert load.index('_fibers["Broken"] = _f') < load.index("_f.Ready("), load
