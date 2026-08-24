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

needs_rust = pytest.mark.skipif(shutil.which("cargo") is None,
                                reason="cargo not installed")
needs_vitest = pytest.mark.skipif(
    not (ROOT / "backends" / "typescript" / "node_modules" / ".bin" / "vitest").exists(),
    reason="vitest not installed in backends/typescript")
needs_go = pytest.mark.skipif(shutil.which("go") is None, reason="go not installed")


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
