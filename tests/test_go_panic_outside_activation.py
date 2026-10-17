"""A go provide method that panics OUTSIDE an activation (issue #2481).

Issue #2464 taught the emitted `Apply` to recover a panic into the error it
returns, so a provide method that panicked during an activation failed the
load instead of the process. Every other entry point into author code still let
the panic through, and an unrecovered go panic kills the whole process:

  * a lifecycle driver step (`call ops.run(..)`) calls the provide method on
    the test goroutine, outside any `Apply`;
  * a timer body fires from `advance`, long after the `Apply` that armed it
    returned;

Each now reports the failure the way an activation failure is reported (the
step it ran plus `panic: <v>`), and the next lifecycle test still runs. That
report also carries the go stack (`debug.Stack()`): it reaches only the
author's own test run. The `Apply` recover keeps the one line `panic: <v>`,
because its error reaches the placement runner's log and FATAL lines, one
redacted record per line (issue #814), and a stack's host paths and argument
words cannot be redacted.

A panic in a bridge crossing (`RevlInvoke`) stays fatal by design: the
placement runner's guard turns it into one redacted FATAL line
(tests/test_go_runner_fatal_funnel.py, issue #814).

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
        "revl_go_emit_2481", ROOT / "backends" / "go" / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _need_go() -> None:
    if GO is None:
        pytest.skip("go toolchain not available")


def _go_test(module: Path, package: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "GOFLAGS": "-mod=mod", "GOPROXY": "off"}
    argv = [GO, "test", "-count=1", "-v", "-run", ".", f"./{package}/"]
    return subprocess.run(argv, cwd=str(module), capture_output=True, text=True, env=env)


def _run_lifecycle(tmp_path: Path, source: str, package: str) -> str:
    """Emit `source` (its lifecycle tests become go tests) into a copy of the
    scenarios module and run every test in it."""
    src = _go_emit().emit(compile_source(source, f"{package}.rvl"), package)
    _need_go()
    module = tmp_path / "mod"
    module.mkdir()
    for name in ("go.mod", "go.sum"):
        shutil.copy(GO_SCENARIOS / name, module / name)
    (module / package).mkdir()
    (module / package / f"gen_{package}_test.go").write_text(src, encoding="utf-8")
    proc = _go_test(module, package)
    return proc.stdout + proc.stderr


PROVIDER = """
extern emission fn boom() -> Int = @go {
	panic("the provider gave up")
}

service Ops {
  emission fn run(region: Str) -> Int
}

component Agent provides ops: Ops {
  provide ops {
    fn run(region) {
      return emit boom()
    }
  }
}
"""

#: A second test after the one that panics: it only runs when the panic did
#: not take the test binary down with it.
SURVIVOR = """
lifecycle test "the next test still runs" {
  load Agent
  unload Agent
  assert no_residue
}
"""


# ---- entry point 1: a lifecycle driver step --------------------------------

CALL_DOC = PROVIDER + """
lifecycle test "a panicking call" {
  load Agent
  let got = call ops.run("r")
  assert got == 1
  unload Agent
}
""" + SURVIVOR


def test_a_provide_method_panicking_in_a_lifecycle_call_fails_that_test_only(tmp_path):
    output = _run_lifecycle(tmp_path, CALL_DOC, "lcall")
    # reported like a failed load: the test, the step, then `panic: <v>`
    assert ('lifecycle test "a panicking call": call ops.run: '
            'panic: the provider gave up') in output, output
    assert "--- FAIL: TestAPanickingCall" in output, output
    # the binary outlived the panic: the next test ran and passed
    assert "--- PASS: TestTheNextTestStillRuns" in output, output


def test_the_recovered_panic_carries_the_go_stack(tmp_path):
    output = _run_lifecycle(tmp_path, CALL_DOC, "lstack")
    report = output[output.index("panic: the provider gave up"):]
    # debug.Stack() output: the goroutine header and the frames down to the
    # extern that panicked
    assert "goroutine " in report, output
    assert "runtime/debug.Stack()" in report, output
    assert ".Boom(" in report or "boom" in report.lower(), output


#: The plain (non-binding) form of the step: `let got = call ..` above and
#: `call ..` here are the two spellings a driver call has.
PLAIN_CALL_DOC = PROVIDER + """
lifecycle test "a panicking plain call" {
  load Agent
  call ops.run("r")
  unload Agent
}
""" + SURVIVOR


def test_a_panicking_plain_call_is_blamed_on_that_call(tmp_path):
    output = _run_lifecycle(tmp_path, PLAIN_CALL_DOC, "lplain")
    assert ('lifecycle test "a panicking plain call": call ops.run: '
            'panic: the provider gave up') in output, output
    assert "--- PASS: TestTheNextTestStillRuns" in output, output


def test_every_step_names_itself_before_it_runs():
    """A panic is blamed on the step that set `_revlAt` last, so every step
    kind sets it, `assert` and `assert no_residue` included."""
    src = _go_emit().emit(compile_source(CALL_DOC, "lcall.rvl"), "lcall")
    test = src[src.index("func TestAPanickingCall("):]
    test = test[:test.index("\n}\n")]
    labels = [line.strip() for line in test.splitlines()
              if line.strip().startswith("_revlAt = ")]
    assert labels == ['_revlAt = "load Agent"', '_revlAt = "call ops.run"',
                      '_revlAt = "assert"', '_revlAt = "unload Agent"'], labels
    survivor = src[src.index("func TestTheNextTestStillRuns("):]
    survivor = survivor[:survivor.index("\n}\n")]
    assert '_revlAt = "assert no_residue"' in survivor


def test_the_activation_error_stays_one_line():
    """The `Apply` recover's error reaches the placement runner's log and
    FATAL lines, so it carries no stack, and a document without a lifecycle
    test does not import `runtime/debug`."""
    doc = PROVIDER + """
component Caller requires ops: Ops {
  emit ops.run("r")
}
"""
    src = _go_emit().emit(compile_source(doc, "apply.rvl"), "apply")
    assert '_revlApplyInv, _revlApplyErr = nil, fmt.Errorf("panic: %v", r)' in src
    assert "revldebug" not in src


# ---- entry point 2: a timer body fired from `advance` ----------------------

TIMER_DOC = """
extern emission fn boom() -> Int = @go {
	var xs []int64
	return xs[3]
}

service Ops {
  emission fn run(region: Str) -> Int
}

component Agent provides ops: Ops {
  provide ops {
    fn run(region) {
      return emit boom()
    }
  }
}

component Ticker requires ops: Ops {
  every 10ms { emit ops.run("tick") }
}

lifecycle test "a panicking timer" {
  load Agent
  load Ticker
  advance 10ms
  unload Ticker
  unload Agent
}

lifecycle test "the next test still runs" {
  load Agent
  unload Agent
}
"""
# (no `assert no_residue` in this survivor: the failed test stopped before its
# `unload Ticker`, so the package-global host counter still holds its timer,
# the same leak any failed step leaves on this tier)


def test_a_timer_body_panicking_on_advance_fails_that_test_only(tmp_path):
    output = _run_lifecycle(tmp_path, TIMER_DOC, "ltimer")
    # a runtime error (index out of range) from inside the provide method
    assert ('lifecycle test "a panicking timer": advance 10ms: '
            'panic: runtime error: index out of range') in output, output
    assert "--- FAIL: TestAPanickingTimer" in output, output
    assert "--- PASS: TestTheNextTestStillRuns" in output, output
