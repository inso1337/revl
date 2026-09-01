"""Tests for the Temporal emission target (roadmap item 253, Slice 1).

The Temporal target is a rendering MODE of this TypeScript emitter
(`emit(ir, target="temporal")`, docs/design/253-temporal-target.md §4). These
tests pin the six adversarial-review fixes the v2 design bakes into Slice 1:

  * a mappable component emits a Temporal TS workflow (golden-compared shape);
  * WRITE-AHEAD saga registration — each compensation is pushed BEFORE its
    forward activity is awaited (CRITICAL 1);
  * the derived CLOSED-ALLOWLIST refusal — `await approval`, `witnessed[fs]`,
    and `spawn` are refused with a why-trace naming the construct and line
    (CRITICAL 2);
  * the deadline is a WORKFLOW-SIDE `Date.now()` budget plus a per-call
    `startToCloseTimeout`, never a schedule-to-close (HIGH 1);
  * `maximumAttempts: 1` on every activity (attack 3);
  * the `_BUILTIN_SIG` determinism guard (attack 4);
  * `--target temporal` absent leaves the normal cordis emit byte-identical.

Full Temporal-SDK runtime execution is deferred (the roadmap exit test runs the
saga on a real dev server); Slice 1 goldens the generated code SHAPE.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent.parent
if str(_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_ROOT / "src"))
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from revl import compile_source  # noqa: E402


def _emit_module():
    """Load emit.py under the canonical name `emit`, the same name its sibling
    `emit_temporal.py` imports from (so both share one `EmitError`)."""
    if "emit" in sys.modules and getattr(sys.modules["emit"], "__file__", "") \
            == str(_HERE / "emit.py"):
        return sys.modules["emit"]
    spec = importlib.util.spec_from_file_location("emit", _HERE / "emit.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["emit"] = module
    spec.loader.exec_module(module)
    return module


EMIT = _emit_module()


def _emit_temporal_module():
    return importlib.import_module("emit_temporal")


# A mappable saga: two remote-resource crossings, each with an independent-key
# compensation. Nothing host-pinned, nothing outside the Slice-1 allowlist.
_BOOKTRIP = """
service Flights { emission fn reserve(itinerary: Str) -> Str
                  emission fn cancel(key: Str) -> Str }
service Payments { emission fn charge(card: Str, total: Int) -> Str
                   emission fn refund(card: Str, total: Int) -> Str }
component BookTrip requires flights: Flights, payments: Payments {
  emit flights.reserve("ABC") compensate flights.cancel("ABC")
  emit payments.charge("visa", 100) compensate payments.refund("visa", 100)
}
"""


def _booktrip_ir():
    return compile_source(_BOOKTRIP, "booktrip.revl")


# --------------------------------------------------------------- mappable golden

def test_mappable_component_emits_temporal_workflow_golden():
    """A mappable component emits a Temporal TS workflow whose shape is frozen
    against a golden (full runtime execution deferred to the exit test)."""
    got = EMIT.emit(_booktrip_ir(), target="temporal")
    golden = (_HERE / "golden" / "temporal_booktrip.ts").read_text(encoding="utf-8")
    assert got == golden, (
        "the emitted Temporal workflow drifted from the golden; regenerate "
        "backends/typescript/golden/temporal_booktrip.ts if the change is intended")
    # spot the load-bearing shape the golden encodes:
    assert "import { proxyActivities" in got
    assert "export async function BookTrip(): Promise<void>" in got
    assert "proxyActivities<typeof activities>" in got
    assert "export interface RevlActivities {" in got


# --------------------------------------------------------------- write-ahead (CRITICAL 1)

def test_compensation_registered_write_ahead_before_forward_await():
    """The provisional compensation is pushed onto the saga BEFORE the forward
    activity is awaited, so a forward that commits its host effect and then
    reports failure (at-least-once, unreliable ack, maximumAttempts: 1) is still
    compensated. Assert the ORDER in the emitted code."""
    got = EMIT.emit(_booktrip_ir(), target="temporal")
    lines = got.splitlines()

    def line_of(needle: str) -> int:
        for i, line in enumerate(lines):
            if needle in line:
                return i
        raise AssertionError(f"not emitted: {needle!r}")

    # flights: the cancel push precedes the reserve await.
    push_cancel = line_of("saga.push({ name: \"flights.cancel\"")
    await_reserve = line_of("await flightsReserve(")
    assert push_cancel < await_reserve, "compensation must register before the forward await"

    # payments: the refund push precedes the charge await.
    push_refund = line_of("saga.push({ name: \"payments.refund\"")
    await_charge = line_of("await paymentsCharge(")
    assert push_refund < await_charge

    # and the write-ahead intent is documented at the push site.
    assert "write-ahead: registered before the forward await" in got


# --------------------------------------------------------------- closed allowlist (CRITICAL 2)

def _refusal():
    return _emit_temporal_module().TemporalRefusal


def test_await_approval_refused_with_why_trace():
    src = (
        "extern emission fn charge(sink: Str, msg: Str) requires approval = @py "
        "{ return }\n"
        "component Biller {\n"
        "  let a = await approval[\"charge\"] { amount: 1 }\n"
        "  emit charge(\"s\", \"m\") with a\n"
        "}\n"
    )
    ir = compile_source(src, "biller.rvl")
    with pytest.raises(_refusal()) as exc:
        EMIT.emit(ir, target="temporal")
    msg = str(exc.value)
    assert "await approval" in msg
    assert "biller.rvl" in msg           # names the source
    assert "signal" in msg               # the why: maps only to a Temporal signal


def test_witnessed_fs_refused_with_why_trace():
    src = (
        "type Stash = { path: Str, bak: Str }\n"
        "type FsError = { code: Str }\n"
        "extern pure fn unstash(w: Stash) -> Unit = @py { return }\n"
        "extern witnessed[fs] fn stash_path(p: Str)"
        " -> Result[Stash, FsError] undo unstash(result) = @py "
        "{ return Ok({'path': p, 'bak': p}) }\n"
        "component W { effect stash_path(\"/tmp/x\") }\n"
    )
    ir = compile_source(src, "wfs.rvl")
    with pytest.raises(_refusal()) as exc:
        EMIT.emit(ir, target="temporal")
    msg = str(exc.value)
    assert "witnessed" in msg and "fs" in msg
    assert "wfs.rvl" in msg
    assert "worker" in msg               # the why: host-affinity (HIGH 2)


def test_spawn_refused_with_why_trace_and_line():
    # D (the spawning component) is declared first so the allowlist walk reaches
    # its `spawn` acquire before Worker's own (separately refused) provide body.
    src = (
        "service Inner { emission fn touch(x: Str) }\n"
        "component D {\n"
        "  let w = effect spawn Worker with { } undo w.dispose()\n"
        "}\n"
        "component Worker provides inner: Inner {\n"
        "  provide inner { fn touch(x) { return } }\n"
        "}\n"
    )
    ir = compile_source(src, "spawn.rvl")
    with pytest.raises(_refusal()) as exc:
        EMIT.emit(ir, target="temporal")
    msg = str(exc.value)
    assert "spawn" in msg
    assert "spawn.rvl:3" in msg          # names the construct's source line


def test_mappable_component_is_accepted():
    """The control: a mappable component does NOT trip the closed allowlist."""
    got = EMIT.emit(_booktrip_ir(), target="temporal")
    assert "export async function BookTrip" in got


# --------------------------------------------------------------- deadline (HIGH 1)

def test_deadline_is_workflow_side_budget_plus_per_call_timeout():
    got = EMIT.emit(_booktrip_ir(), target="temporal")
    # a WORKFLOW-SIDE budget: a single total, checked with Date.now() between
    # compensations (frozen to workflow-task time on the TS SDK, replay-safe).
    assert "const COMPENSATION_BUDGET_MS = 5000" in got
    assert "const deadline = Date.now() + COMPENSATION_BUDGET_MS" in got
    assert "if (Date.now() >= deadline)" in got
    assert "reason: 'deadline-expired', outcome: 'not-attempted'" in got
    # the per-call cutoff is the compensation activity's startToCloseTimeout.
    assert "startToCloseTimeout:" in got
    # and it is NOT a schedule-to-close (the wrong mapping the review rejected).
    assert "scheduleToClose" not in got and "scheduleToCloseTimeout" not in got


def test_continue_and_record_does_not_abort_the_drain():
    got = EMIT.emit(_booktrip_ir(), target="temporal")
    # a failed compensation is recorded and the loop CONTINUES (LIFO drain).
    assert "for (const step of saga.reverse())" in got
    assert "catch (e) { residue.push(" in got
    assert "continue  // record and skip" in got


def test_every_activity_is_at_most_once():
    got = EMIT.emit(_booktrip_ir(), target="temporal")
    assert "retry: { maximumAttempts: 1 }" in got


def test_residue_sink_present():
    got = EMIT.emit(_booktrip_ir(), target="temporal")
    # the residue sink: a durable recordResidue activity, the envelope on the
    # failure details, and a query handler for live inspection (attack 7).
    assert "await recordResidue(report)" in got
    assert "ApplicationFailure.create(" in got and "details: [report]" in got
    assert "defineQuery<Residue[]>" in got and "setHandler(" in got
    assert "worldRemaining" in got and "proof: 'revl-saga-abort'" in got


# --------------------------------------------------------------- byte-identical default

def test_target_absent_is_byte_identical_cordis_emit():
    ir = _booktrip_ir()
    default = EMIT.emit(ir)
    cordis = EMIT.emit(ir, target="cordis")
    assert default == cordis
    # the default path is the native cordis runtime, not the Temporal sink.
    assert "proxyActivities" not in default
    assert "Target runtime: cordis v4" in default


def test_unknown_target_refused():
    with pytest.raises(EMIT.EmitError, match="unknown emit target"):
        EMIT.emit(_booktrip_ir(), target="dworkin")


# --------------------------------------------------------------- determinism guard (attack 4)

# The reviewed DETERMINISTIC allowlist for the pure-builtin table
# (typecheck.py::_BUILTIN_SIG). Every entry was audited: string / list / map /
# int-conversion operations, none wall-clock / random / uuid-shaped, and map key
# iteration is pinned to ascending canonical Str order. This guard is FAIL-CLOSED:
# a new builtin lands red here until it is classified deterministic (and added)
# or reclassified as an emission / refused for --target temporal, because a
# non-deterministic builtin lowered into workflow position would break Temporal's
# replay determinism while revl still called it "pure"
# (docs/design/253-temporal-target.md §6 attack 4).
_DETERMINISTIC_BUILTIN_ALLOWLIST = frozenset({
    "length", "push", "slice", "charAt", "charCodeAt", "codepoint_at", "concat",
    "indexOf", "split", "join", "repeat", "startsWith", "endsWith", "is_alnum",
    "is_digit", "is_alpha", "is_space", "has", "keys", "list", "lookup",
    "remove", "set", "size", "field", "str", "to_int", "to_int32", "to_str",
    "div_trunc", "div_floor", "div_euclid", "checked_div_trunc",
    "checked_div_floor", "checked_div_euclid", "checked_mod",
})


def test_builtin_sig_determinism_guard_fail_closed():
    from revl.typecheck import _BUILTIN_SIG

    unclassified = set(_BUILTIN_SIG) - _DETERMINISTIC_BUILTIN_ALLOWLIST
    assert not unclassified, (
        "a new pure builtin is not on the reviewed deterministic allowlist for "
        f"--target temporal: {sorted(unclassified)}. Classify it: if it is "
        "deterministic (no wall-clock / random / uuid; canonical iteration), add "
        "it to _DETERMINISTIC_BUILTIN_ALLOWLIST here; otherwise it must be "
        "reclassified as an emission or refused for the Temporal target, since a "
        "non-deterministic builtin in workflow position breaks Temporal replay "
        "determinism (docs/design/253-temporal-target.md §6 attack 4).")
