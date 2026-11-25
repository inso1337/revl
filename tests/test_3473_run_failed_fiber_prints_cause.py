"""Issue #3473: `revl run` prints the cause when a fiber settles in FAILED.

WHAT WAS WRONG. `_on_fiber` logged the transition (`UNLOADING -> FAILED`) and
dropped the fiber's `_error` on the floor outside the `--withdraw` observation
window: no exception name, no message, no traceback, on stdout or stderr, with
or without `--trace`. A typo in a host body was indistinguishable from a
refused requirement, a type error or a real runtime fault - all printed the
same two transitions - so the only way left was to bisect with full boots.

WHAT HOLDS NOW. The moment a fiber settles FAILED, one line goes to stderr
naming the component and the cause: `NameError: name 'true' is not defined`
for a host body that raised, the refusal text and diagnostic code for a
RevlError. The full traceback is printed only behind the causal-trace flag
(`--trace FILE` / `--withdraw`). The exit status is untouched - that defect
is filed separately as #3436.

Checked here:
  * the issue's exact repro (`Ok(true)` in a @py body) prints the FAILED
    transition on stdout AND `cause | Once | FAILED: NameError: ...` on
    stderr, and no traceback without the flag;
  * with `--trace FILE` the same run additionally prints the traceback;
  * `_log_failure` formats a RevlError (refusal message + code), a bare host
    exception, and a fiber that carries no error at all, without raising.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="the py tier needs the cordis-py runtime (backends/python/setup.sh)",
)

from revl.errors import RevlError  # noqa: E402
from revl.run import _Driver  # noqa: E402

#: the issue's exact repro: the @py body spells `true` (a JS name), so the
#: host body raises NameError and the `Once` fiber settles FAILED.
FRICTION = """pub extern emission[store] fn broken() -> Result[Bool, Str]
  = @py {
    return Ok(true)
  }
  = @ts { return { kind: "Ok", value: true } }
service Run { emission fn once() -> Result[Bool, Str] }
component R provides run: Run {
  provide run {
    fn once() { return emit broken() }
  }
}
component Once requires run: Run { emit run.once() }
"""

DEADLINE_S = 60


def _run_once(tmp_path, *flags):
    rvl = tmp_path / "app.rvl"
    rvl.write_text(FRICTION, encoding="utf-8")
    env = {**os.environ,
           "PYTHONPATH": os.pathsep.join(
               p for p in (str(ROOT / "src"), os.environ.get("PYTHONPATH", "")) if p)}
    proc = subprocess.run(
        [sys.executable, "-m", "revl", "run", "--once", *flags, str(rvl)],
        stdin=subprocess.DEVNULL, capture_output=True, text=True,
        timeout=DEADLINE_S, cwd=str(tmp_path), env=env)
    return proc.returncode, proc.stdout, proc.stderr


def test_failed_fiber_transition_still_on_stdout(tmp_path):
    code, out, _ = _run_once(tmp_path)
    assert "fiber  | Once" in out and "UNLOADING -> FAILED" in out, out


def test_failed_fiber_prints_cause_on_stderr(tmp_path):
    _, _, err = _run_once(tmp_path)
    assert "cause  | Once" in err, err
    assert "FAILED: NameError" in err, err
    assert "true" in err, err


def test_cause_line_carries_no_traceback_without_the_flag(tmp_path):
    _, _, err = _run_once(tmp_path)
    assert "Traceback (most recent call last)" not in err, err


def test_trace_flag_adds_the_traceback(tmp_path):
    _, _, err = _run_once(tmp_path, "--trace", str(tmp_path / "trace.jsonl"))
    assert "FAILED: NameError" in err, err
    assert "Traceback (most recent call last)" in err, err
    assert (tmp_path / "trace.jsonl").exists()


# ------------------------------------------------- the line's formatting

def test_log_failure_formats_a_revl_error_with_its_code(capsys):
    fake = SimpleNamespace(tracing=False)
    err = RevlError("app.rvl", 3, "no provider for `run`", code="G2",
                    category="link")
    _Driver._log_failure(fake, "Once", err)
    out = capsys.readouterr().err
    assert "cause  | Once" in out
    assert "no provider for `run`" in out
    assert "(G2)" in out


def test_log_failure_formats_a_bare_host_exception(capsys):
    fake = SimpleNamespace(tracing=False)
    _Driver._log_failure(fake, "Once", ValueError("boom"))
    out = capsys.readouterr().err
    assert "FAILED: ValueError: boom" in out
    assert "Traceback" not in out


def test_log_failure_says_so_when_no_error_is_recorded(capsys):
    fake = SimpleNamespace(tracing=False)
    _Driver._log_failure(fake, "Once", None)
    out = capsys.readouterr().err
    assert "no error recorded" in out


def test_log_failure_traceback_only_when_tracing(capsys):
    try:
        raise KeyError("k")
    except KeyError as e:
        fake = SimpleNamespace(tracing=True)
        _Driver._log_failure(fake, "Once", e)
        out = capsys.readouterr().err
        assert "FAILED: KeyError" in out
        assert "Traceback (most recent call last)" in out
