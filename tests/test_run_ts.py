"""`revl run --backend ts` end-to-end (docs/v2.0-roadmap.md §2, "Toward early
production", item 77(b); FEATURE-REQUESTS.md FR-2).

The ts tier is wired behind the same driver contract the py tier uses
(src/revl/run.py), but boots the composition as a *separate node process* on
cordis v4 over the language-agnostic bridge seam (roadmap item 23), rather
than in-process — the same shape as the rust/java/wasm drivers
(test_run_rust.py / test_run_java.py / test_run_wasm.py). This file drives the
real CLI as a subprocess, exactly as test_run.py does for py.

The same two honesty rules the other tiers apply here:

* the boot/exit assertion runs only where node and a resolvable cordis-ts are
  actually present (`needs_node`); a machine with no node, or no cordis-ts, or
  a node too old to strip types natively *skips with the reason the driver
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
from revl.run_ts import ts_runtime_reason  # noqa: E402

# A simple, non-string-heavy composition (records + an ADT + a Result), so the
# test does not depend on the string-wave emitter changes in flight. Two
# components — a provider and a consumer — so the LIFO teardown order is
# observable. Config-free, so no --config is needed.
OUTCOME = str(ROOT / "examples" / "outcome.rvl")

_TS_REASON = ts_runtime_reason()
needs_node = pytest.mark.skipif(
    _TS_REASON is not None,
    reason=f"needs node + cordis-ts: {_TS_REASON}")


def _run_cli(args, input_text: str = "") -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT / "src")
    return subprocess.run(
        [sys.executable, "-m", "revl", "run", *args],
        capture_output=True, text=True, input=input_text, env=env,
        check=False)


# ------------------------------------------------------ runtime-independent
#
# These run on every interpreter, node present or not.


def test_ts_is_a_runnable_backend():
    """The tier is wired: `ts` joins `py` in RUNNABLE_BACKENDS. Additive —
    py stays runnable."""
    assert "ts" in RUNNABLE_BACKENDS
    assert "py" in RUNNABLE_BACKENDS


def test_ts_is_no_longer_a_flat_refusal():
    """`--backend ts` must never be the flat `not wired yet` / exit-2 refusal
    the tier used to get. It either boots (node + cordis-ts present) or skips
    with a runtime reason and exit 3 (toolchain absent) — but never rc 2."""
    result = _run_cli([OUTCOME, "--backend", "ts", "--once"], input_text="")
    assert result.returncode != 2, result.stdout + result.stderr
    assert "not wired yet" not in result.stderr


def test_ts_plan_reports_the_tier_as_runnable():
    """`--plan` needs no runtime, and now shows ts without the
    `(not runnable yet)` caveat."""
    result = _run_cli([OUTCOME, "--backend", "ts", "--plan"], input_text="")
    assert result.returncode == 0, result.stderr
    assert "backend: ts" in result.stdout
    assert "not runnable yet" not in result.stdout


# --------------------------------------------------------- with the runtime
#
# The golden path: emit the cordis-ts module -> boot the composition as a node
# process -> LIFO teardown -> prove no residue -> exit 0.


@needs_node
def test_run_ts_once_boots_tears_down_lifo_and_proves_no_residue():
    result = _run_cli([OUTCOME, "--backend", "ts", "--once"], input_text="")
    assert result.returncode == 0, result.stderr + result.stdout
    out = result.stdout

    # the composition boots as a real cordis-ts process: both components reach
    # Active, providers first (DirSvc provides `dir`, DirUser requires it)
    assert "== load composition (ts tier) ==" in out
    assert "DirSvc" in out and "DirUser" in out
    assert "state=ACTIVE" in out
    assert "[run] UP" in out

    # LIFO teardown: the consumer (DirUser) is disposed before the provider
    # (DirSvc) — reverse load order, the same contract the py driver's
    # _dispose_all enforces
    down_user = out.index("swap  | DirUser")
    down_svc = out.index("swap  | DirSvc")
    assert down_user < down_svc, "consumer must tear down before its provider"

    # the no-residue proof, read off the live runtime after teardown (the
    # cordis-ts mirror of the py driver's registry/reflect check and the rust
    # runner's registry()/reflect() check)
    assert "0 live plugin(s)" in out
    assert "0 service(s) provided" in out
    assert "NO-RESIDUE" in out
    assert "[run] DOWN" in out


@needs_node
def test_run_ts_leaves_no_emitted_module_behind():
    """A run emits the cordis-ts module into backends/typescript/_gen/ (so its
    `../runtime.ts` / `cordis` imports resolve) and must remove it afterwards —
    a run leaves the checkout as it found it (that dir is gitignored scratch)."""
    gen = ROOT / "backends" / "typescript" / "_gen"
    before = set(gen.glob("mod_*.ts")) if gen.exists() else set()
    result = _run_cli([OUTCOME, "--backend", "ts", "--once"], input_text="")
    assert result.returncode == 0, result.stderr + result.stdout
    after = set(gen.glob("mod_*.ts")) if gen.exists() else set()
    assert after == before, \
        "the run left an emitted module in backends/typescript/_gen/"
