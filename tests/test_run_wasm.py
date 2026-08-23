"""`revl run --backend wasm` end-to-end (docs/v2.0-roadmap.md §2, "Toward early
production": the exit test wants `git clone && revl run` to print a running
composition on *every* supported tier).

The wasm (substrate) tier is wired behind the same driver contract the py tier
uses (src/revl/run.py), but boots the composition as a *separate process* — the
once-mode harness (backends/wasm/run_harness.py) on the cordis-wasm runtime,
which is backed by wasmtime — rather than in-process. This file drives the real
CLI as a subprocess, exactly as test_run.py does for py and test_run_rust.py for
rust.

The same two honesty rules apply as for the other tiers:

* the boot/exit assertion runs only where the cordis-wasm runtime is actually
  present (`needs_cordis_wasm`); a machine with no wasmtime/runtime *skips with
  the reason the driver reports* — a skipped tier is never green, and never a
  spurious red;
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
from revl.run_wasm import wasm_runtime_reason  # noqa: E402

# A minimal Int-only provider/consumer pair — the substrate tier is the
# strictest emitter (no config, no strings-in-templates, no host builtins), so
# the example is deliberately plain: two Int-only components, provider first, so
# the LIFO teardown order is observable.
PAIR = str(ROOT / "examples" / "counter_pair.rvl")

_WASM_REASON = wasm_runtime_reason()
needs_cordis_wasm = pytest.mark.skipif(
    _WASM_REASON is not None,
    reason=f"needs the cordis-wasm runtime (wasmtime): {_WASM_REASON}")


def _run_cli(args, input_text: str = "") -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT / "src")
    return subprocess.run(
        [sys.executable, "-m", "revl", "run", *args],
        capture_output=True, text=True, input=input_text, env=env,
        check=False)


# ------------------------------------------------------ runtime-independent
#
# These run on every interpreter, cordis-wasm present or not.


def test_wasm_is_a_runnable_backend():
    """The tier is wired: `wasm` joins `py` in RUNNABLE_BACKENDS. Additive —
    py stays runnable."""
    assert "wasm" in RUNNABLE_BACKENDS
    assert "py" in RUNNABLE_BACKENDS


def test_wasm_is_no_longer_a_flat_refusal():
    """`--backend wasm` must never be the flat `not wired yet` / exit-2 refusal.
    It either boots (runtime present) or skips with a runtime reason and exit 3
    (runtime absent) — but never rc 2."""
    result = _run_cli([PAIR, "--backend", "wasm", "--once"], input_text="")
    assert result.returncode != 2, result.stdout + result.stderr
    assert "not wired yet" not in result.stderr


def test_wasm_plan_reports_the_tier_as_runnable():
    """`--plan` needs no runtime, and now shows wasm without the
    `(not runnable yet)` caveat."""
    result = _run_cli([PAIR, "--backend", "wasm", "--plan"], input_text="")
    assert result.returncode == 0, result.stderr
    assert "backend: wasm" in result.stdout
    assert "not runnable yet" not in result.stdout


# --------------------------------------------------------- with the runtime
#
# The golden path: emit WAT -> boot the composition on the cordis-wasm runtime
# -> LIFO teardown -> prove no residue -> exit 0.


@needs_cordis_wasm
def test_run_wasm_once_boots_tears_down_lifo_and_proves_no_residue():
    result = _run_cli([PAIR, "--backend", "wasm", "--once"], input_text="")
    assert result.returncode == 0, result.stderr + result.stdout
    out = result.stdout

    # the composition boots on a real cordis-wasm Runtime: both components reach
    # active, providers first (CounterSvc provides `counter`, CounterUser
    # requires it)
    assert "== load composition (wasm tier) ==" in out
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
    # substrate mirror of the py driver's registry/reflect check)
    assert "0 live plugin(s)" in out
    assert "0 service(s) provided" in out
    assert "NO-RESIDUE" in out
    assert "[run] DOWN" in out
