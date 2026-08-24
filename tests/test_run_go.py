"""`revl run --backend go` end-to-end (docs/v2.0-roadmap.md §2, "Toward early
production" — roadmap item 77(e) / FR-8: `revl run` gains the go tier, the last
first-class test tier without a run driver).

The go tier is wired behind the same driver contract the py tier uses
(src/revl/run.py), but boots the composition as a *separate process* — the
stc-go placement runner (backends/go/placement_runner) in its degenerate
single-process once form — rather than in-process. This file drives the real
CLI as a subprocess, exactly as test_run.py does for py and test_run_rust.py /
test_run_java.py / test_run_wasm.py for the other non-py tiers.

The same two honesty rules apply as for the other tiers:

* the boot/exit assertion runs only where a go toolchain with the pinned stc-go
  is actually present (`needs_cordis_go`); a machine with no go, or no cached
  stc-go and no network, *skips with the reason the driver reports* — a skipped
  tier is never green, and never a spurious red;
* the tier is no longer a flat "not wired yet" refusal — that regression is
  guarded on every interpreter, toolchain or not.
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
from revl.run_go import go_runtime_reason  # noqa: E402

# A minimal Int-only provider/consumer pair (no config, no strings, no ADTs) —
# so the test does not lean on any richer emitter feature, and the two
# components make the LIFO teardown order observable. The go placement bridge
# serves v1/v2 live stc-go components, which is exactly what this is.
PAIR = str(ROOT / "examples" / "counter_pair.rvl")

_GO_REASON = go_runtime_reason()
needs_cordis_go = pytest.mark.skipif(
    _GO_REASON is not None,
    reason=f"needs a resolvable stc-go toolchain: {_GO_REASON}")


def _run_cli(args, input_text: str = "") -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT / "src")
    return subprocess.run(
        [sys.executable, "-m", "revl", "run", *args],
        capture_output=True, text=True, input=input_text, env=env,
        check=False)


# ------------------------------------------------------ runtime-independent
#
# These run on every interpreter, go toolchain present or not.


def test_go_is_a_runnable_backend():
    """The tier is wired: `go` joins `py` in RUNNABLE_BACKENDS. Additive —
    py stays runnable."""
    assert "go" in RUNNABLE_BACKENDS
    assert "py" in RUNNABLE_BACKENDS


def test_go_is_no_longer_a_flat_refusal():
    """`--backend go` must never be the flat `not wired yet` / exit-2 refusal.
    It either boots (toolchain present) or skips with a runtime reason and exit
    3 (toolchain absent) — but never rc 2."""
    result = _run_cli([PAIR, "--backend", "go", "--once"], input_text="")
    assert result.returncode != 2, result.stdout + result.stderr
    assert "not wired yet" not in result.stderr


def test_go_plan_reports_the_tier_as_runnable():
    """`--plan` needs no runtime, and now shows go without the
    `(not runnable yet)` caveat."""
    result = _run_cli([PAIR, "--backend", "go", "--plan"], input_text="")
    assert result.returncode == 0, result.stderr
    assert "backend: go" in result.stdout
    assert "not runnable yet" not in result.stdout


# --------------------------------------------------------- with the runtime
#
# The golden path: emit go -> go build -> boot the composition as an stc-go
# process -> LIFO teardown -> prove no residue -> exit 0.


@needs_cordis_go
def test_run_go_once_boots_tears_down_lifo_and_proves_no_residue():
    result = _run_cli([PAIR, "--backend", "go", "--once"], input_text="")
    assert result.returncode == 0, result.stderr + result.stdout
    out = result.stdout

    # the composition boots as a real stc-go process: both components reach
    # active, providers first (CounterSvc provides `counter`, CounterUser
    # requires it)
    assert "== load composition (go tier) ==" in out
    assert "CounterSvc" in out and "CounterUser" in out
    assert "state=active" in out
    assert "[run] UP" in out

    # LIFO teardown: the consumer (CounterUser) is disposed before the provider
    # (CounterSvc) — reverse load order, the same contract the py driver's
    # _dispose_all enforces
    down_user = out.index("swap  | CounterUser")
    down_svc = out.index("swap  | CounterSvc")
    assert down_user < down_svc, "consumer must tear down before its provider"

    # the no-residue proof, read off the live runtime after teardown (the
    # stc-go mirror of the py driver's registry/reflect check): no fiber left
    # in the registry and no provided key still resolving
    assert "0 live plugin(s)" in out
    assert "0 service(s) still provided" in out
    assert "NO-RESIDUE" in out
    assert "[run] DOWN" in out


@needs_cordis_go
def test_run_go_leaves_the_checkout_clean():
    """A run regenerates the runner's `emitted` package and binary per
    composition (both gitignored — the go runner is codegen, not a committed
    golden), so a run must leave no *tracked* file modified."""
    before = subprocess.run(
        ["git", "-C", str(ROOT), "status", "--porcelain"],
        capture_output=True, text=True, check=True).stdout
    result = _run_cli([PAIR, "--backend", "go", "--once"], input_text="")
    assert result.returncode == 0, result.stderr + result.stdout
    after = subprocess.run(
        ["git", "-C", str(ROOT), "status", "--porcelain"],
        capture_output=True, text=True, check=True).stdout
    assert after == before, \
        "the run modified tracked files:\n" + after
