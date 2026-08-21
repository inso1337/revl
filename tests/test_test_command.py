"""`revl test --backend`: one source's `test` blocks, proven across tiers.

The dispatch logic is tested deterministically (toolchain-gated runners are
monkeypatched); the real toolchains are exercised when present, matching how
the per-tier suites gate themselves.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402
from revl import test as test_module  # noqa: E402

needs_wasmtime = pytest.mark.skipif(
    shutil.which("wasmtime") is None, reason="wasmtime not installed")


def _cli(tmp_path: Path, *args: str) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run(
        [sys.executable, "-m", "revl", "test", *args],
        cwd=ROOT, env=env, capture_output=True, text=True,
    )


def _write_rvl(tmp_path: Path, name: str, body: str) -> Path:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


def test_default_backend_is_py(tmp_path):
    passing = _write_rvl(tmp_path, "passing.rvl", 'test "passes" { assert true }')
    result = _cli(tmp_path, str(passing))
    assert result.returncode == 0, result.stderr
    assert "PASS passes" in result.stdout
    assert "[py] ok: 1 test(s) passed" in result.stdout

    failing = _write_rvl(tmp_path, "failing.rvl", 'test "fails" { assert 1 == 2 }')
    result = _cli(tmp_path, str(failing))
    assert result.returncode == 1
    assert "FAIL fails" in result.stdout


@needs_wasmtime
def test_backend_wasm_runs_tests(tmp_path):
    source = _write_rvl(
        tmp_path, "x.rvl",
        'fn one() -> Int { return 1 }\n'
        'test "one is one" { assert one() == 1 }\n'
        'test "empty body passes" { }\n')
    result = _cli(tmp_path, str(source), "--backend", "wasm")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASS one is one" in result.stdout
    assert "[wasm] ok: wasmtime: 2 test(s) passed" in result.stdout


@needs_wasmtime
def test_backend_wasm_failing_assert_exits_1(tmp_path):
    source = _write_rvl(tmp_path, "x.rvl", 'test "boom" { assert 1 == 2 }')
    result = _cli(tmp_path, str(source), "--backend", "wasm")
    assert result.returncode == 1
    assert "FAIL boom" in result.stdout
    # the trap that the failed `assert` lowered to is surfaced, not swallowed
    assert "unreachable" in (result.stdout + result.stderr)


def test_backend_wasm_skips_without_wasmtime(monkeypatch):
    """Graceful degradation: no wasmtime binary -> skip with a reason, never a
    fake pass (the same gate every other toolchain-bound runner applies)."""
    monkeypatch.setattr(test_module.shutil, "which", lambda name: None)
    ir = compile_source('test "t" { assert true }')
    outcome, message = test_module.run_wasm(ir)
    assert outcome == "skip"
    assert "wasmtime not installed" in message


_VITEST = ROOT / "backends" / "typescript" / "node_modules" / ".bin" / "vitest"


@pytest.mark.skipif(not _VITEST.exists(), reason="vitest not installed in backends/typescript")
def test_backend_ts_runs_vitest(tmp_path):
    source = _write_rvl(tmp_path, "x.rvl", 'test "add works" { assert 1 + 2 == 3 }')
    result = _cli(tmp_path, str(source), "--backend", "ts")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "[ts] ok:" in result.stdout
    # the temp emitted test file is cleaned up afterwards
    generated = ROOT / "backends" / "typescript" / "tests" / "generated"
    assert not list(generated.glob("revl_test_*.test.ts"))


@pytest.mark.skipif(not _VITEST.exists(), reason="vitest not installed in backends/typescript")
def test_backend_ts_failing_assert_exits_1(tmp_path):
    source = _write_rvl(tmp_path, "x.rvl", 'test "boom" { assert 1 == 2 }')
    result = _cli(tmp_path, str(source), "--backend", "ts")
    assert result.returncode == 1


def test_all_aggregates_tiers(monkeypatch, capsys):
    """`--all` runs every tier and fails only on a tier that actually failed;
    skipped tiers (missing toolchains) never fail the run."""
    ir = compile_source('test "t" { assert true }')

    def _skipping(_ir):
        return ("skip", "not available in this test")

    for name in ("ts", "rust", "java", "wasm"):
        monkeypatch.setitem(test_module.RUNNERS, name, _skipping)

    # py genuinely runs — it needs no toolchain
    assert test_module.test_command(ir, "all") == 0
    out = capsys.readouterr().out
    assert "[py] ok:" in out
    assert "[ts] skipped:" in out
    assert "[wasm] skipped:" in out
    assert "all tiers passed" in out


def test_all_fails_when_a_tier_fails(monkeypatch, capsys):
    ir = compile_source('test "t" { assert true }')

    def _failing(_ir):
        return ("fail", "boom")

    for name in ("ts", "rust", "java", "wasm"):
        monkeypatch.setitem(test_module.RUNNERS, name, _failing)
    assert test_module.test_command(ir, "all") == 1
    assert "4 tier(s) failed" in capsys.readouterr().err
