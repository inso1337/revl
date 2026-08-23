"""`revl run --backend rust` end-to-end (docs/v2.0-roadmap.md §2, "Toward early
production": the exit test wants `git clone && revl run` to print a running
composition on *every* supported tier).

The rust tier is wired behind the same driver contract the py tier uses
(src/revl/run.py), but boots the composition as a *separate cordis-rs process*
over the language-agnostic bridge seam (roadmap item 23), rather than
in-process. This file drives the real CLI as a subprocess, exactly as
test_run.py does for py.

The same two honesty rules test_run.py applies to cordis-py apply here to
cordis-rs:

* the boot/exit assertion runs only where a cordis-rs toolchain is actually
  present and resolvable (`needs_cordis_rs`); a machine with no cargo, or no
  resolvable cordis-rs and no network, *skips with the reason the driver
  reports* — a skipped tier is never green, and never a spurious red;
* the tier is no longer a flat "not wired yet" refusal — that regression is
  guarded on every interpreter, runtime or not.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.run import RUNNABLE_BACKENDS  # noqa: E402
from revl.run_rust import rust_runtime_reason  # noqa: E402

# A simple, non-string-heavy composition (records + an ADT + a Result), so the
# test does not depend on the string-wave emitter changes in flight. Two
# components — a provider and a consumer — so the LIFO teardown order is
# observable. Config-free, so no --config is needed.
OUTCOME = str(ROOT / "examples" / "outcome.rvl")

_RUST_REASON = rust_runtime_reason()
needs_cordis_rs = pytest.mark.skipif(
    _RUST_REASON is not None,
    reason=f"needs a resolvable cordis-rs toolchain: {_RUST_REASON}")


def _run_cli(args, input_text: str = "") -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT / "src")
    return subprocess.run(
        [sys.executable, "-m", "revl", "run", *args],
        capture_output=True, text=True, input=input_text, env=env,
        check=False)


# ------------------------------------------------------ runtime-independent
#
# These run on every interpreter, cordis-rs present or not.


def test_rust_is_a_runnable_backend():
    """The tier is wired: `rust` joins `py` in RUNNABLE_BACKENDS. Additive —
    py stays runnable."""
    assert "rust" in RUNNABLE_BACKENDS
    assert "py" in RUNNABLE_BACKENDS


def test_rust_is_no_longer_a_flat_refusal():
    """`--backend rust` must never be the flat `not wired yet` / exit-2 refusal
    ts and java still get. It either boots (toolchain present) or skips with a
    runtime reason and exit 3 (toolchain absent) — but never rc 2."""
    result = _run_cli([OUTCOME, "--backend", "rust", "--once"], input_text="")
    assert result.returncode != 2, result.stdout + result.stderr
    assert "not wired yet" not in result.stderr


def test_rust_plan_reports_the_tier_as_runnable():
    """`--plan` needs no runtime, and now shows rust without the
    `(not runnable yet)` caveat."""
    result = _run_cli([OUTCOME, "--backend", "rust", "--plan"], input_text="")
    assert result.returncode == 0, result.stderr
    assert "backend: rust" in result.stdout
    assert "not runnable yet" not in result.stdout


# --------------------------------------------------------- with the runtime
#
# The golden path: emit rust -> cargo build -> boot the composition as a
# cordis-rs process -> LIFO teardown -> prove no residue -> exit 0.


@needs_cordis_rs
def test_run_rust_once_boots_tears_down_lifo_and_proves_no_residue():
    result = _run_cli([OUTCOME, "--backend", "rust", "--once"], input_text="")
    assert result.returncode == 0, result.stderr + result.stdout
    out = result.stdout

    # the composition boots as a real cordis-rs process: both components reach
    # Active, providers first (DirSvc provides `dir`, DirUser requires it)
    assert "== load composition (rust tier) ==" in out
    assert "dir_svc" in out and "dir_user" in out
    assert "state=Active" in out
    assert "[run] UP" in out

    # LIFO teardown: the consumer (dir_user) is disposed before the provider
    # (dir_svc) — reverse load order, the same contract the py driver's
    # _dispose_all enforces
    down_user = out.index("swap  | dir_user")
    down_svc = out.index("swap  | dir_svc")
    assert down_user < down_svc, "consumer must tear down before its provider"

    # the no-residue proof, read off the live runtime after teardown (the
    # cordis-rs mirror of the py driver's registry/reflect check)
    assert "0 live plugin(s)" in out
    assert "0 service(s) provided" in out
    assert "NO-RESIDUE" in out
    assert "[run] DOWN" in out


@needs_cordis_rs
def test_run_rust_restores_the_committed_components_module(tmp_path):
    """A run regenerates the runner's src/components.rs per composition, then
    restores it — a run must leave the checkout as it found it (that module is
    a committed golden, not scratch)."""
    components = (ROOT / "backends" / "rust" / "placement_runner"
                 / "src" / "components.rs")
    before = components.read_bytes()
    result = _run_cli([OUTCOME, "--backend", "rust", "--once"], input_text="")
    assert result.returncode == 0, result.stderr + result.stdout
    assert components.read_bytes() == before, \
        "the run left src/components.rs modified"
