"""FR-5: lifecycle tests executed on the non-py tiers' native runners.

The no-residue claim is asserted on every tier that has a live composition
runtime: rust (`#[test]` over cordis-rs), ts (async vitest over cordis), go
(`go test` over stc-go) — and, as the reference, py (in test_lifecycle_exec).
Each tier is toolchain-gated (skip with a reason, never a fake pass); when
the toolchain is present, both the positive (reverts cleanly) and the
negative (leaky undo caught) directions must hold.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples"

if str(ROOT / "src") not in sys.path:  # gate imports resolve in this process
    sys.path.insert(0, str(ROOT / "src"))

needs_rust = pytest.mark.skipif(shutil.which("cargo") is None,
                                reason="cargo not installed")
needs_vitest = pytest.mark.skipif(
    not (ROOT / "backends" / "typescript" / "node_modules" / ".bin" / "vitest").exists(),
    reason="vitest not installed in backends/typescript")
needs_go = pytest.mark.skipif(shutil.which("go") is None, reason="go not installed")


def _wasm_unavailable_reason() -> str | None:
    """None when the wasm lifecycle driver can actually run here (wasmtime +
    the cordis-wasm runtime present), else the reason to skip the test."""
    if shutil.which("wasmtime") is None:
        return "wasmtime not installed"
    from revl.run_wasm import wasm_runtime_reason  # noqa: PLC0415

    return wasm_runtime_reason()


needs_wasm = pytest.mark.skipif(_wasm_unavailable_reason() is not None,
                                reason=_wasm_unavailable_reason() or "")


def _revl_test(*args: str) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run([sys.executable, "-m", "revl", "test", *args],
                          cwd=ROOT, env=env, capture_output=True, text=True,
                          timeout=900)


def _lifecycle_on_tier(backend: str):
    """The tier must pass the clean example and fail the leaky one — the
    no-residue claim asserted on the tier (FR-5)."""
    clean = _revl_test("--backend", backend, str(EXAMPLES / "lifecycle_cache.rvl"))
    assert clean.returncode == 0, clean.stdout + clean.stderr
    assert f"[{backend}] pass:" in clean.stdout

    leak = _revl_test("--backend", backend, str(EXAMPLES / "lifecycle_leak.rvl"))
    assert leak.returncode == 1, leak.stdout + leak.stderr
    assert f"[{backend}] fail:" in leak.stdout + leak.stderr


@needs_rust
def test_rust_tier_lifecycle_tests_pass_and_catch_the_leak():
    _lifecycle_on_tier("rust")


@needs_vitest
def test_ts_tier_lifecycle_tests_pass_and_catch_the_leak():
    _lifecycle_on_tier("ts")


@needs_go
def test_go_tier_lifecycle_tests_pass_and_catch_the_leak():
    _lifecycle_on_tier("go")


# ------------------------------------------------------------------ wasm (142)
# The wasm substrate carries lifecycle tests over its scalar instruction set
# (Int is i64, Bool is i32): it boots the emitted components on the live
# cordis-wasm runtime, calls through provision keys, unloads LIFO, and checks
# R4/R1 residue — instead of the former blanket skip. The leaky example
# (lifecycle_cache.rvl / lifecycle_leak.rvl) leans on config/Pool/Map, so its
# no-residue *negative* direction is only expressible on the hosted tiers; on
# wasm the failure direction is pinned with a broken scalar assertion instead
# (a real trap through the driver, never a swallowed pass).

_WASM_SCALAR_FAIL = """
service Status { fn shared() -> Int }
component Beacon provides status: Status { provide status { fn shared() = 42 } }
lifecycle test "wrong on purpose" {
  load Beacon
  let seen = call status.shared()
  assert seen == 999
  unload Beacon
  assert no_residue
}
"""


@needs_wasm
def test_wasm_tier_runs_a_scalar_lifecycle_test():
    """A scalar (Int-only) lifecycle mesh boots, serves, unloads and proves
    no residue on the live cordis-wasm runtime — a real run, not a skip."""
    ran = _revl_test("--backend", "wasm", str(EXAMPLES / "lifecycle_wasm.rvl"))
    assert ran.returncode == 0, ran.stdout + ran.stderr
    assert "[wasm] pass:" in ran.stdout
    assert "lifecycle test(s) ran" in ran.stdout
    # both tests in the example executed (boot -> call -> unload -> no-residue)
    assert ran.stdout.count("PASS ") >= 2


@needs_wasm
def test_wasm_tier_fails_a_broken_lifecycle_assertion(tmp_path):
    """The driver surfaces a failed lifecycle assertion as a tier failure —
    the substrate runs the test for real, it does not rubber-stamp it."""
    doc = tmp_path / "bad.rvl"
    doc.write_text(_WASM_SCALAR_FAIL, encoding="utf-8")
    bad = _revl_test("--backend", "wasm", str(doc))
    assert bad.returncode == 1, bad.stdout + bad.stderr
    assert "[wasm] fail:" in bad.stdout + bad.stderr
    assert "assertion failed" in bad.stdout + bad.stderr


@needs_wasm
def test_wasm_tier_skips_non_scalar_lifecycle_per_test_with_a_reason():
    """A lifecycle test the substrate cannot express (config/Pool/Map, a
    non-scalar boundary) is skipped honestly with a reason — never faked as a
    pass. lifecycle_cache.rvl loads a `config` component, so it skips."""
    skip = _revl_test("--backend", "wasm", str(EXAMPLES / "lifecycle_cache.rvl"))
    assert "[wasm] skip:" in skip.stdout + skip.stderr
    assert "config" in skip.stdout + skip.stderr
