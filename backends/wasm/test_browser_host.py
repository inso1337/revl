"""The browser host: gating, activation and Float calls, plus the artifact gate.

`cordis-host.mjs` is exercised on a real WebAssembly engine through node, and
the checked-in demo artifacts must match a fresh emit. Both tests skip when
their toolchain is absent; CI's wasm job installs node and wasmtime.
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
HOST = ROOT / "backends" / "wasm" / "host"


def _have_wasm_tool() -> bool:
    if shutil.which("wat2wasm"):
        return True
    return importlib.util.find_spec("wasmtime") is not None


@pytest.mark.skipif(not _have_wasm_tool(),
                    reason="neither wat2wasm nor the wasmtime package is installed")
def test_demo_artifacts_are_current():
    result = subprocess.run(
        [sys.executable, str(HOST / "build_demo.py"), "--check"],
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "current" in result.stdout


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_host_gates_activates_and_calls_float_export():
    result = subprocess.run(
        ["node", str(HOST / "test_host.mjs")],
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "browser host: ok" in result.stdout
