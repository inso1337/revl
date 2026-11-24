"""`revl run` exits nonzero when a component ends FAILED (#3436).

A composition whose component fails at boot (an `acquire` host body that
raises) used to print the FAILED transition, prove no residue, and exit 0 —
so a script or service manager read start-up failure as success. The same
applied to a component whose `undo` raises at teardown.

The driver now collects every fiber that settles FAILED (`_Driver._failed`,
recorded in the fiber-state observer) and ends the run with exit 1 naming the
components on stderr. These tests drive the real CLI as a subprocess, the
way tests/test_run.py does.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

try:  # noqa: SIM105
    import cordis  # noqa: F401
    HAVE_CORDIS = True
except ModuleNotFoundError:  # pragma: no cover
    HAVE_CORDIS = False

needs_cordis = pytest.mark.skipif(
    not HAVE_CORDIS, reason="the cordis-py runtime is not installed here")


def _run_cli(args) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT / "src")
    return subprocess.run([sys.executable, "-m", "revl", "run", *args],
                          capture_output=True, text=True, input="", env=env,
                          check=False, timeout=120)


BOOTFAIL = """\
extern acquire fn boom_open() -> H undo boom_close(result) = @py {
    raise RuntimeError("boot boom")
}

extern pure fn boom_close(h: H) -> Unit = @py {
    pass
}

service S {
  fn ping() -> Str
}

component Boom provides s: S {
  let h = effect boom_open() undo boom_close(h)
  provide s {
    fn ping() = h
  }
}
"""

UNDOFAIL = """\
extern acquire fn ok_open() -> H undo boom_close(result) = @py {
    return "handle"
}

extern pure fn boom_close(h: H) -> Unit = @py {
    raise RuntimeError("undo boom")
}

service S {
  fn ping() -> Str
}

component UndoBoom provides s: S {
  let h = effect ok_open() undo boom_close(h)
  provide s {
    fn ping() = h
  }
}
"""

OK = """\
service S {
  fn ping() -> Str
}

component Fine provides s: S {
  provide s {
    fn ping() = "pong"
  }
}
"""


@needs_cordis
def test_a_component_failing_at_boot_exits_nonzero_and_names_it(tmp_path):
    src = tmp_path / "bootfail.rvl"
    src.write_text(BOOTFAIL, encoding="utf-8")
    result = _run_cli([str(src), "--backend", "py", "--once"])
    assert result.returncode != 0, result.stdout + result.stderr
    assert "Boom" in result.stderr, result.stderr
    assert "FAILED" in result.stdout  # the transition is still traced


@needs_cordis
def test_an_undo_raising_at_teardown_exits_nonzero_and_names_it(tmp_path):
    src = tmp_path / "undofail.rvl"
    src.write_text(UNDOFAIL, encoding="utf-8")
    result = _run_cli([str(src), "--backend", "py", "--once"])
    assert result.returncode != 0, result.stdout + result.stderr
    assert "UndoBoom" in result.stderr, result.stderr


@needs_cordis
def test_a_clean_composition_still_exits_zero(tmp_path):
    src = tmp_path / "fine.rvl"
    src.write_text(OK, encoding="utf-8")
    result = _run_cli([str(src), "--backend", "py", "--once"])
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ended FAILED" not in result.stderr
