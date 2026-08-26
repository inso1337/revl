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
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402
from revl.run import RUNNABLE_BACKENDS  # noqa: E402
from revl.run_go import go_runtime_reason  # noqa: E402
from revl.test import RUNNERS  # noqa: E402

# A minimal Int-only provider/consumer pair (no config, no strings, no ADTs) —
# so the test does not lean on any richer emitter feature, and the two
# components make the LIFO teardown order observable. The go placement bridge
# serves v1/v2 live stc-go components, which is exactly what this is.
PAIR = str(ROOT / "examples" / "counter_pair.rvl")

# A v3 typed-core composition (records, an ADT-typed service boundary, and an
# ADT `match` in a provide-method body): the go placement path learned to
# carry the typed-core tier next to the live stc-go components, so this
# composition places and round-trips like the v1/v2 pair above.
V3_STEP = str(ROOT / "examples" / "v3_step_scheduler.rvl")

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


@needs_cordis_go
def test_run_go_v3_typed_core_places_and_round_trips():
    """A v3 typed-core composition — a record service return, an ADT-typed
    service boundary, and an ADT `match` in a provide-method body — places and
    runs the same boot -> LIFO teardown -> no-residue round-trip as the v1/v2
    pair. This was the FR-8 follow-up gap: the go placement path was wired for
    the v1/v2 component dialect and refused v3 typed-core documents at emit
    ("placement on the go backend needs v1/v2 services")."""
    result = _run_cli([V3_STEP, "--backend", "go", "--once"], input_text="")
    assert result.returncode == 0, result.stderr + result.stdout
    out = result.stdout

    # the typed-core composition boots as a real stc-go process: the provider
    # (Sched) and its consumer (SchedUser) both reach active
    assert "== load composition (go tier) ==" in out
    assert "Sched" in out and "SchedUser" in out
    assert "state=active" in out
    assert "[run] UP" in out

    # LIFO teardown: the consumer (SchedUser) is disposed before its provider
    # (Sched) — reverse load order, the same contract as the v1/v2 pair. The
    # runner pads the subject to 16 columns, so "swap  | Sched " (with the
    # separating space) matches only the provider, not SchedUser.
    down_user = out.index("swap  | SchedUser")
    down_svc = out.index("swap  | Sched ")
    assert down_user < down_svc, "consumer must tear down before its provider"

    # the no-residue proof: no fiber left and no provided key still resolving
    assert "0 live plugin(s)" in out
    assert "0 service(s) still provided" in out
    assert "NO-RESIDUE" in out
    assert "[run] DOWN" in out


# A wildcard-domain build regression (roadmap item 304, follow-up to 280): a
# concrete `match` arm that binds a payload the arm body never reads. The pure
# v3 go emitter wrote `_v := _m.Value` with no following use, so `go build`
# rejected the emitted test with `declared and not used: _v` — an admitted
# program the go tier could emit but not build. The fix pins the bound payload
# (`_ = _v`) so an unused arm payload compiles, matching the component-body
# match path that already did. This runs `go test` directly (no stc-go), so it
# gates only on a `go` toolchain being installed, not on the placement runner.
FUZZ_GO_WILDCARD_PAYLOAD = str(
    ROOT / "examples" / "regressions" / "fuzz_go_ead437e4.rvl")


@pytest.mark.skipif(shutil.which("go") is None, reason="needs a go toolchain")
def test_go_unused_match_arm_payload_still_builds_and_runs():
    """`examples/regressions/fuzz_go_ead437e4.rvl` binds `_v` in two concrete
    match arms that never read it. Before item 304 the go tier emitted the
    binds with no use and `go build` failed (`declared and not used: _v`);
    now it builds, runs, and passes — agreeing with the py reference."""
    ir = compile_source(
        Path(FUZZ_GO_WILDCARD_PAYLOAD).read_text(encoding="utf-8"),
        "fuzz_go_ead437e4.rvl")
    status, message = RUNNERS["go"](ir)
    if status == "skip":  # no toolchain the runner can use
        pytest.skip(f"go: {message}")
    assert status == "pass", f"go tier did not pass: {message}"
    # the py reference admits and passes the same program — the tiers agree
    assert RUNNERS["py"](ir)[0] == "pass"


# item 313: a match on an Opt/Result CONSTRUCTOR-LITERAL scrutinee
# (`match Ok(1) { ... }`) lowered to `switch _m := RevlOk[..]{..}.(type)` — an
# unparenthesized composite literal in the type-switch init clause (Go reads the
# `{` as the switch body) that is also a concrete struct where `.(type)` needs
# an interface. The fix binds the scrutinee to an interface-typed temp before
# the switch. A variable scrutinee already worked (it is an identifier).
FUZZ_GO_MATCHLIT = str(
    ROOT / "examples" / "regressions" / "fuzz_go_matchlit_typeswitch.rvl")


@pytest.mark.skipif(shutil.which("go") is None, reason="needs a go toolchain")
def test_go_match_on_constructor_literal_builds_and_runs():
    """`fuzz_go_matchlit_typeswitch.rvl` matches on an `Ok(1)` literal. Before
    item 313 the go emitter put the composite literal straight in the
    type-switch init clause (`expected '}', found Value`); now it binds an
    interface-typed temp first, so it builds, runs, and agrees with py."""
    ir = compile_source(
        Path(FUZZ_GO_MATCHLIT).read_text(encoding="utf-8"),
        "fuzz_go_matchlit_typeswitch.rvl")
    status, message = RUNNERS["go"](ir)
    if status == "skip":
        pytest.skip(f"go: {message}")
    assert status == "pass", f"go tier did not pass: {message}"
    assert RUNNERS["py"](ir)[0] == "pass"


# item 314: revl admits redundant boolean ops (`false || false`, `x && x`) and
# the py reference evaluates them, but `go test` runs `go vet`, whose `bools`
# analyzer rejects identical operands as `redundant or`/`redundant and`. The
# architect decision is `-vet=off` in the go runner: the cross-tier contract is
# "the emitter's output runs", not "it passes a go-specific style lint".
FUZZ_GO_VET_BOOL = str(
    ROOT / "examples" / "regressions" / "fuzz_go_vet_redundant_bool.rvl")


@pytest.mark.skipif(shutil.which("go") is None, reason="needs a go toolchain")
def test_go_redundant_boolean_op_runs_under_vet_off():
    """`fuzz_go_vet_redundant_bool.rvl` returns `(false || false)`. Before item
    314 `go test` failed it under `go vet` (`redundant or`); the go runner now
    passes `-vet=off`, so the admitted program runs and agrees with py."""
    ir = compile_source(
        Path(FUZZ_GO_VET_BOOL).read_text(encoding="utf-8"),
        "fuzz_go_vet_redundant_bool.rvl")
    status, message = RUNNERS["go"](ir)
    if status == "skip":
        pytest.skip(f"go: {message}")
    assert status == "pass", f"go tier did not pass: {message}"
    assert RUNNERS["py"](ir)[0] == "pass"


# item 320: a bound `let x = effect <non-host call>` whose acquisition returns a
# VALUE type was always declared `var x *T` by the go bracket codegen, which
# fails to compile for a plain fn / service-method acquisition. The fix decides
# pointer-vs-value by the acquisition's actual return type. This is a lifecycle
# test over live stc-go, so it needs the resolvable go runtime, not just a
# toolchain.
FUZZ_GO_LETBIND_VALUE = str(
    ROOT / "examples" / "regressions" / "fuzz_go_letbind_valuetype.rvl")


@needs_cordis_go
def test_go_value_typed_bracket_acquisition_builds_and_runs():
    """`fuzz_go_letbind_valuetype.rvl` binds `let handle = effect openHandle()`
    where `openHandle` returns Int. Before item 320 the go bracket codegen
    emitted `var handle *int64` (and a `*int64` struct field) — `cannot use
    openHandle() (int64) as *int64`; now `handle` is declared by its value
    type, so it loads, serves the value, and reverts cleanly."""
    ir = compile_source(
        Path(FUZZ_GO_LETBIND_VALUE).read_text(encoding="utf-8"),
        "fuzz_go_letbind_valuetype.rvl")
    status, message = RUNNERS["go"](ir)
    if status == "skip":
        pytest.skip(f"go: {message}")
    assert status == "pass", f"go tier did not pass: {message}"
