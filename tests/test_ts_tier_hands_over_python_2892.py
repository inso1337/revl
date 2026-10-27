"""`revl test --backend ts` hands its own interpreter to the vitest child (#2892).

`backends/typescript/scripts/emit-fixtures.ts` runs `emit.py` with the
interpreter named by `REVL_PYTHON`, and falls back to a bare `python3` /
`python`. The ts tier is started by a Python that already works, so it passes
that one down instead of leaving the child to guess. An explicit
`REVL_PYTHON` in the caller's environment wins.

No real vitest runs: `subprocess.Popen` is replaced by a recorder.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import test as T  # noqa: E402
from revl.compiler import compile_files  # noqa: E402

EXAMPLE = ROOT / "examples" / "outcome.rvl"


class _Recorder:
    calls: "list[tuple[list, dict]]" = []

    def __init__(self, argv, **kwargs):
        _Recorder.calls.append((list(argv), kwargs))
        self.pid = 4242
        self.returncode = 0

    def communicate(self, timeout=None):
        return "", ""


@pytest.fixture(autouse=True)
def _no_real_vitest(monkeypatch):
    _Recorder.calls = []
    monkeypatch.setattr(subprocess, "Popen", _Recorder)
    monkeypatch.setattr(T, "vitest_command", lambda: ["vitest"])
    monkeypatch.setattr(T, "_node_version", lambda: None)
    yield
    _Recorder.calls = []


def _vitest_env() -> dict:
    T._RAW_RUNNERS["ts"](compile_files([str(EXAMPLE)]))
    vitest = [kw for argv, kw in _Recorder.calls if "vitest" in argv]
    assert len(vitest) == 1, _Recorder.calls
    return vitest[0]["env"]


def test_unset_revl_python_becomes_the_running_interpreter(monkeypatch):
    monkeypatch.delenv("REVL_PYTHON", raising=False)
    assert _vitest_env()["REVL_PYTHON"] == sys.executable


def test_empty_revl_python_becomes_the_running_interpreter(monkeypatch):
    monkeypatch.setenv("REVL_PYTHON", "")
    assert _vitest_env()["REVL_PYTHON"] == sys.executable


def test_explicit_revl_python_is_kept(monkeypatch):
    monkeypatch.setenv("REVL_PYTHON", "/opt/elsewhere/python")
    assert _vitest_env()["REVL_PYTHON"] == "/opt/elsewhere/python"
