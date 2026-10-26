"""Issue #2932: pin the consumer build to its private target directory."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from _load_by_path import load_by_path  # noqa: E402


def test_crate_engine_pins_its_target_dir_when_cargo_target_dir_is_set(
    tmp_path, monkeypatch
):
    census = load_by_path(
        "gate_reference_census", ROOT / "tools" / "gate_reference_census.py"
    )
    workdir = tmp_path / "consumer"
    monkeypatch.setenv("CARGO_TARGET_DIR", str(tmp_path / "shared-target"))
    commands = []

    def build(argv, *, cwd, capture_output, text):
        commands.append((argv, cwd))
        target_dir = Path(argv[argv.index("--target-dir") + 1])
        binary = target_dir / "debug" / "revl_gate_census"
        binary.parent.mkdir(parents=True, exist_ok=True)
        binary.touch()
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(census.subprocess, "run", build)
    engine = object.__new__(census.CrateEngine)
    engine._dir = workdir

    binary = engine._build()

    assert len(commands) == 1
    argv, cwd = commands[0]
    assert cwd == workdir
    assert argv[argv.index("--target-dir") + 1] == str(workdir / "target")
    assert binary == workdir / "target" / "debug" / "revl_gate_census"
    assert binary.is_file()
