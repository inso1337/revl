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

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent.parent
if str(_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_ROOT / "src"))
# tests/ is APPENDED so it resolves only `_load_by_path`, nothing earlier on
# sys.path provides (the same arrangement as backends/java's tests).
if str(_ROOT / "tests") not in sys.path:
    sys.path.append(str(_ROOT / "tests"))

from revl import compile_source  # noqa: E402
from _load_by_path import load_by_path  # noqa: E402

# Issue #1575: this module used to register the typescript emitter as the bare
# `sys.modules["emit"]`, and put backends/typescript on sys.path, for the rest
# of the session. Every backend directory has an `emit.py`, and the py tier
# reaches its own through that bare name, so any later test that booted a py
# composition refused with "another backend's emitter was imported under the
# bare name `emit`" (tests/test_realm_placement_over_the_transport.py, when it
# ran after this file). The emitter is loaded under a name nothing else binds,
# and neither global is touched: `emit(..., target="temporal")` binds the bare
# name only for the duration of its own import of the Temporal sink
# (`emit.py::_emit_temporal`) and restores it.
EMIT = load_by_path("revl_ts_emit_temporal_tests", _HERE / "emit.py")


def _emit_temporal_module():
    """The Temporal sink module the emitter itself renders through, so its
    `TemporalRefusal` and `EmitError` are the classes the emitter raises. The
    emitter imports it as `emit_temporal`, inside a window where `emit` names
    this emitter; the first temporal render here opens that window."""
    module = sys.modules.get("emit_temporal")
    if module is None or Path(module.__file__).resolve() != _HERE / "emit_temporal.py":
        EMIT.emit(_booktrip_ir(), target="temporal")
        module = sys.modules["emit_temporal"]
    return module


# A mappable saga: two remote-resource crossings with independent-key
# compensations, plus (Slice 2) a third crossing that is a KEYED emission
# extern, so the one golden exercises both derived retry classes at once and
# `tsc --noEmit` typechecks both proxy groups. Nothing host-pinned, nothing
# outside the closed allowlist. Committed beside the other emitter fixtures so
# `tools/regen_goldens.py` emits the golden from the same bytes this test does.
_BOOKTRIP = (_HERE / "tests" / "fixtures" / "booktrip.revl").read_text(encoding="utf-8")


def _booktrip_ir():
    return compile_source(_BOOKTRIP, "booktrip.revl")


# --------------------------------------------------------------- mappable golden

def test_mappable_component_emits_temporal_workflow_golden():
    """A mappable component emits a Temporal TS workflow whose shape is frozen
    against a golden (full runtime execution deferred to the exit test)."""
    got = EMIT.emit(_booktrip_ir(), target="temporal")
    golden = (_HERE / "golden" / "temporal_booktrip.ts").read_text(encoding="utf-8")
    assert got == golden, (
        "the emitted Temporal workflow drifted from the golden. If the change is "
        "intended: python3 tools/regen_goldens.py typescript, then review the diff.")
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


def test_a_document_with_no_evidence_is_wholly_at_most_once():
    """A document that declares no idempotency evidence anywhere renders the
    ONE at-most-once group and no retryable group at all — the Slice-1 shape,
    now reached by DERIVATION rather than by a hard-coded constant. This is the
    default the derivation must fall through to."""
    src = (
        'service Flights { emission fn reserve(itinerary: Str) -> Str\n'
        '                  emission fn cancel(key: Str) -> Str }\n'
        'component Trip requires flights: Flights {\n'
        '  emit flights.reserve("ABC") compensate flights.cancel("ABC")\n'
        '}\n'
    )
    got = _emit_temporal_module().emit_temporal(compile_source(src, "trip.revl"))
    assert "const AT_MOST_ONCE = { maximumAttempts: 1 }" in got
    assert "retry: AT_MOST_ONCE," in got
    assert "DEDUP_SAFE_RETRY" not in got
    assert got.count("proxyActivities<typeof activities>({") == 1


def test_residue_sink_present():
    got = EMIT.emit(_booktrip_ir(), target="temporal")
    # the residue sink: a durable recordResidue activity, the envelope on the
    # failure details, and a query handler for live inspection (attack 7).
    assert "await recordResidue(report)" in got
    assert "ApplicationFailure.create(" in got and "details: [report]" in got
    assert "defineQuery<Residue[]>" in got and "setHandler(" in got
    assert "worldRemaining" in got and "proof: 'revl-saga-abort'" in got


# ------------------------------------------- reserved scaffolding names (#3187)

def _extern_crossing_src(name: str) -> str:
    return (
        f'extern emission fn {name}(card: Str) -> Str = @ts {{ return "ok" }}\n'
        f'component Pay {{ emit {name}("visa") }}\n'
    )


@pytest.mark.parametrize("name", [
    "recordResidue",       # the abort sink's own activity name
    "proxyActivities",     # SDK import; destructuring it is a TDZ error
    "ApplicationFailure",  # SDK import; `throw ApplicationFailure.create`
    "setHandler",          # SDK import
    "defineQuery",         # SDK import
    "redactResidueError",  # emitted module-level helper
    "argNeedles",          # emitted module-level helper
    "AT_MOST_ONCE",        # emitted retry policy
    # the activation body's own bindings shadow the destructured activity at
    # its call site even though the destructuring itself is legal
    "saga", "residue", "input", "step",
])
def test_extern_named_temporal_scaffolding_refused(name):
    """An extern name becomes a module-scope activity binding AND is called
    inside the workflow function, so it may not land on a name the workflow
    module or its activation body already binds (issue #3187)."""
    ir = compile_source(_extern_crossing_src(name), f"{name}.rvl")
    with pytest.raises(EMIT.EmitError, match="reserves"):
        EMIT.emit(ir, target="temporal")


def test_activity_named_input_refused_in_a_component_with_config():
    """A configured component does not change the rule: `input` is refused for
    the extern before rendering, so the generated `input: PayInput` parameter
    cannot shadow an activity of that name. The body-name check in `register`
    is defensive: a service-derived activity name always carries the uppercase
    key/method join, and every extern is already refused here."""
    src = (
        'extern emission fn input(card: Str) -> Str = @ts { return "ok" }\n'
        "component Pay {\n"
        '  config { level: Str = "x" }\n'
        '  emit input("visa")\n'
        "}\n"
    )
    ir = compile_source(src, "input_activity.rvl")
    with pytest.raises(EMIT.EmitError, match="reserves"):
        EMIT.emit(ir, target="temporal")


@pytest.mark.parametrize("name", [
    "recordResidue", "proxyActivities", "setHandler", "COMPENSATION_BUDGET_MS",
])
def test_component_named_temporal_scaffolding_refused(name):
    ir = compile_source(f"component {name} {{ }}\n", f"{name}.rvl")
    with pytest.raises(EMIT.EmitError, match="reserves"):
        EMIT.emit(ir, target="temporal")


def test_component_whose_residue_query_hits_the_sink_refused():
    """`{component}Residue` is the component's `defineQuery` const, so a
    component named `record` would emit a second `recordResidue` beside the
    abort sink."""
    ir = compile_source("component record { }\n", "record.rvl")
    with pytest.raises(EMIT.EmitError, match="residue query"):
        EMIT.emit(ir, target="temporal")


def test_component_colliding_with_another_components_residue_query_refused():
    ir = compile_source("component Trip { }\ncomponent TripResidue { }\n",
                        "trip_residue.rvl")
    with pytest.raises(EMIT.EmitError, match="reserves"):
        EMIT.emit(ir, target="temporal")


def test_service_method_activity_name_colliding_with_sink_refused():
    """The activity name is DERIVED from a service-method crossing
    (`record` + `residue` -> `recordResidue`), so the extern-only check is not
    enough: the derived name is refused when the crossing registers."""
    src = (
        "service Ledger { emission fn residue(card: Str) -> Str }\n"
        "component Pay requires record: Ledger {\n"
        '  emit record.residue("visa")\n'
        "}\n"
    )
    ir = compile_source(src, "ledger.rvl")
    with pytest.raises(EMIT.EmitError, match="reserves"):
        EMIT.emit(ir, target="temporal")


@pytest.mark.parametrize("name", ["saga", "residue", "input", "step"])
def test_binding_named_activation_scaffolding_refused(name):
    """The activation body binds `saga` (the compensation stack), `input` (the
    config object) and the drain's `residue`/`step` itself. `saga`/`input`
    shadow a value the body reads today; the whole set is reserved so a
    template change cannot turn a silent shadow back on."""
    src = (
        "extern pure fn r_close(h: RHandle) = @ts { return }\n"
        "extern acquire fn r_open(n: Int) -> RHandle undo r_close(result)"
        ' = @ts { return "h" }\n'
        "component Pay {\n"
        f"  let {name} = effect r_open(0) undo r_close({name})\n"
        "}\n"
    )
    ir = compile_source(src, f"{name}_binding.rvl")
    with pytest.raises(EMIT.EmitError, match="reserves"):
        EMIT.emit(ir, target="temporal")


def test_near_miss_extern_name_is_emitted_distinct_from_the_sink():
    """Refusal is exact, not a prefix rule: `recordResidue_` is a different
    activity and must keep working beside the `recordResidue` sink."""
    ir = compile_source(_extern_crossing_src("recordResidue_"),
                        "near_miss.rvl")
    got = EMIT.emit(ir, target="temporal")
    assert 'await recordResidue_("visa")' in got
    assert "await recordResidue(report)" in got
    assert "const { recordResidue, recordResidue_ } = proxyActivities" in got
    assert "recordResidue_(card: string): Promise<string>" in got
    assert "recordResidue(report: SagaReport): Promise<void>" in got


def test_near_miss_binding_does_not_shadow_the_saga_stack():
    src = (
        "extern pure fn r_close(h: RHandle) = @ts { return }\n"
        "extern acquire fn r_open(n: Int) -> RHandle undo r_close(result)"
        ' = @ts { return "h" }\n'
        "component Pay {\n"
        "  let saga_ = effect r_open(0) undo r_close(saga_)\n"
        "}\n"
    )
    got = _emit_temporal_module().emit_temporal(
        compile_source(src, "near_miss_binding.rvl"))
    assert "const saga: SagaStep[] = []" in got
    assert "let saga_: unknown = undefined" in got
    assert "saga_ = await r_open(0n)" in got
    assert 'saga.push({ name: "saga_.undo"' in got


# The host globals the emitted workflow module itself reads: `Date.now()` in
# the abort path, `String`/`Set`/`Array`/`Object` in the redaction helpers,
# and `Promise`, which the async functions read as a value once compiled
# below ES2017 (`__awaiter`). No `@ts` body is emitted into this module, so a
# global it does not read is not shadowed by anything.
_JS_GLOBALS_AT_MODULE_SCOPE = [
    "Date", "String", "Set", "Promise", "Object", "Array",
]

# Host globals the emitted workflow module never reads: an activity or a
# component may take these spellings on this target, as on the main TS target.
_JS_GLOBALS_THE_MODULE_NEVER_READS = ["fetch", "console", "setTimeout", "Map"]


@pytest.mark.parametrize("name", _JS_GLOBALS_AT_MODULE_SCOPE)
def test_extern_named_a_js_global_refused(name):
    """An extern destructured as `Date` out of `proxyActivities` replaces the
    global for the whole module: the abort path's `Date.now()` then calls the
    activity proxy and throws before the drain loop, so no compensation runs
    and `recordResidue` is never reached."""
    ir = compile_source(_extern_crossing_src(name), f"{name}.rvl")
    with pytest.raises(EMIT.EmitError, match="reserves"):
        EMIT.emit(ir, target="temporal")


@pytest.mark.parametrize("name", _JS_GLOBALS_AT_MODULE_SCOPE)
def test_component_named_a_js_global_refused(name):
    """A component is declared `export async function <name>()` at module
    scope, so a component named `String` or `Set` shadows the global the
    redaction helper calls the first time a compensation fails."""
    ir = compile_source(f"component {name} {{ }}\n", f"{name}.rvl")
    with pytest.raises(EMIT.EmitError, match="reserves"):
        EMIT.emit(ir, target="temporal")


@pytest.mark.parametrize("name", _JS_GLOBALS_THE_MODULE_NEVER_READS)
def test_extern_named_an_unread_js_global_emits(name):
    """Only the globals the module reads are reserved: an activity named
    `fetch` is destructured and called like any other, since nothing in the
    workflow module reads the host's `fetch`."""
    got = EMIT.emit(compile_source(_extern_crossing_src(name), f"{name}.rvl"),
                    target="temporal")
    assert f'await {name}("visa")' in got
    assert f"  {name}(card: string): Promise<string>" in got


def test_component_named_an_unread_js_global_emits():
    got = EMIT.emit(compile_source("component fetch { }\n", "fetch.rvl"),
                    target="temporal")
    assert "export async function fetch(): Promise<void>" in got


def _bracket_src(undo: str, acquire: str = "r_open") -> str:
    return (
        f"extern pure fn {undo}(h: RHandle) = @ts {{ return }}\n"
        f"extern acquire fn {acquire}(n: Int) -> RHandle undo {undo}(result)"
        ' = @ts { return "h" }\n'
        "component Pay {\n"
        f"  let h = effect {acquire}(0) undo {undo}(h)\n"
        "}\n"
    )


@pytest.mark.parametrize("name", ["fetch", "console", "process"])
def test_pure_extern_named_a_host_global_refused(name):
    """A pure extern is never declared in the workflow module: `undo fetch(h)`
    renders as a bare `fetch(h)`, which would call the host's `fetch` instead
    of the extern. So a name the module reads free avoids the globals listed
    in `JS_GLOBAL_RESERVED`, not only the ones the scaffolding reads."""
    ir = compile_source(_bracket_src(name), f"{name}_pure.rvl")
    with pytest.raises(EMIT.EmitError, match="reserves"):
        EMIT.emit(ir, target="temporal")


@pytest.mark.parametrize("name", ["setTimeout", "fetch"])
def test_acquire_extern_named_a_host_global_refused(name):
    ir = compile_source(_bracket_src("r_close", acquire=name),
                        f"{name}_acquire.rvl")
    with pytest.raises(EMIT.EmitError, match="reserves"):
        EMIT.emit(ir, target="temporal")


def test_witnessed_extern_named_a_host_global_refused():
    """A witnessed extern is not an activity either: `effect fetch("a")`
    renders as a bare `fetch("a")` the module never declares."""
    src = (
        "type WT = { id: Str }\n"
        "extern pure fn zqChk(w: WT) -> Int = @ts { return 1 }\n"
        "extern witnessed fn fetch(x: Str) -> Result[WT, Str] undo pure"
        " zqChk(result) = @ts { return x }\n"
        "component Res {\n"
        '  effect fetch("a")\n'
        "}\n"
    )
    ir = compile_source(src, "fetch_witnessed.rvl")
    with pytest.raises(EMIT.EmitError, match="reserves the name 'fetch' for a witnessed extern"):
        EMIT.emit(ir, target="temporal")


def test_unused_local_extern_named_a_host_global_refused():
    """The check covers every extern in the program, reached by a component
    or not, so an unused `local` extern named `fetch` is refused too (it fails
    safe)."""
    src = (
        "extern local fn fetch(n: Int) -> Unit = @ts { return }\n"
        "component Res { }\n"
    )
    ir = compile_source(src, "fetch_local.rvl")
    with pytest.raises(EMIT.EmitError, match="reserves the name 'fetch' for a local extern"):
        EMIT.emit(ir, target="temporal")


def test_free_read_refusal_names_the_host_call_not_scaffolding():
    """`fetch` is not scaffolding: the refusal says the bare call would reach
    the host's own function, with the article the class takes."""
    ir = compile_source(_bracket_src("r_close", acquire="fetch"),
                        "fetch_acquire_msg.rvl")
    with pytest.raises(EMIT.EmitError) as info:
        EMIT.emit(ir, target="temporal")
    text = str(info.value)
    assert "for an acquire extern" in text
    assert "would reach the host's own 'fetch'" in text
    assert "scaffolding" not in text


# Names the emitted module uses ONLY as types: the TS lib `Record` and the
# module's own `SagaStep`/`Residue`/`SagaReport` aliases and `RevlActivities`
# interface. A value binding lives in another namespace, so an activity or a
# component may take these spellings.
_TYPE_ONLY_NAMES = ["Record", "SagaStep", "Residue", "SagaReport",
                    "RevlActivities"]


@pytest.mark.parametrize("name", _TYPE_ONLY_NAMES)
def test_emission_extern_named_a_type_only_name_emits(name):
    got = EMIT.emit(compile_source(_extern_crossing_src(name), f"{name}.rvl"),
                    target="temporal")
    assert f'await {name}("visa")' in got
    assert f"  {name}(card: string): Promise<string>" in got


@pytest.mark.parametrize("name", _TYPE_ONLY_NAMES)
def test_component_named_a_type_only_name_emits(name):
    got = EMIT.emit(compile_source(f"component {name} {{ }}\n", f"{name}.rvl"),
                    target="temporal")
    assert f"export async function {name}(): Promise<void>" in got


def test_free_read_global_gets_the_free_read_reason():
    """`Date` is also in the module scope, but a pure extern named `Date` is
    never bound, so the refusal gives the free-read reason, not the shadow
    one."""
    ir = compile_source(_bracket_src("Date"), "Date_pure.rvl")
    with pytest.raises(EMIT.EmitError) as info:
        EMIT.emit(ir, target="temporal")
    text = str(info.value)
    assert "reserves the name 'Date' for a pure extern" in text
    assert "would reach the host's own 'Date'" in text
    assert "scaffolding" not in text


# ------------------------------- reserved names derived from the emitted module

# Every author-chosen name in this workflow starts with `zq`, so whatever else
# the emitted module names is the target's own scaffolding. Its two components
# render every step branch a source program can reach on this target: a
# service crossing with a compensation, a keyed extern (the second retry
# group), an acquire bracket bound to a name, an acquire whose site undo names
# no binding, config reads, and a unary operand. The renderer's other branches
# are not reachable from source: a component `if` guard may only hold `fail`,
# which the step allowlist refuses, and an `acquire` needs a site `undo`, so an
# effect without one never compiles. A template change in one of those
# branches is not seen here.
_SCAFFOLDING_PROBE = (
    "service ZqLedger { emission fn zqCharge(zqCard: Str) -> Str\n"
    "                   emission fn zqRefund(zqCard: Str) -> Str }\n"
    "extern emission idempotent(key: zqCard) fn zqSettle(zqCard: Str) -> Str\n"
    "  = @ts { return zqCard }\n"
    "extern pure fn zqClose(zqH: RHandle) = @ts { return }\n"
    "extern pure fn zqDrop(zqN: Int) = @ts { return }\n"
    "extern acquire fn zqOpen(zqN: Int) -> RHandle undo zqClose(result)"
    ' = @ts { return "h" }\n'
    "extern acquire fn zqLease(zqN: Int) -> RHandle undo zqDrop(0)"
    ' = @ts { return "h" }\n'
    "component ZqPay requires zqLedger: ZqLedger {\n"
    '  config { zqLevel: Str = "x" }\n'
    "  let zqH = effect zqOpen(0) undo zqClose(zqH)\n"
    '  emit zqLedger.zqCharge("visa") compensate zqLedger.zqRefund("visa")\n'
    "  emit zqSettle(config.zqLevel)\n"
    "}\n"
    "component ZqDrain {\n"
    "  config { zqCount: Int = 2 }\n"
    "  effect zqLease(-1) undo zqDrop(config.zqCount)\n"
    "}\n"
)

_TS_IDENT = r"[A-Za-z_$][\w$]*"

# Syntax, not bindings: JS keywords plus the TS/contextual words the template
# writes. Nothing an author can name shadows one of these.
_TS_SYNTAX = frozenset(EMIT.JS_RESERVED) | {
    "as", "async", "from", "of", "type", "interface", "string", "number",
    "boolean", "bigint", "unknown", "any", "never",
}


def _strip_ts_comments_and_strings(code: str) -> str:
    return re.sub(
        r"//[^\n]*|'(?:\\.|[^'\\\n])*'|\"(?:\\.|[^\"\\\n])*\"|`(?:\\.|[^`\\])*`",
        lambda m: " " * len(m.group(0)), code)


_TS_TYPE = r"[A-Za-z_$][\w$.]*(?:\[\])*"


def _blank_type_positions(code: str) -> str:
    """`code` with every TYPE position blanked (same length, so offsets keep):
    generic arguments, `type` aliases, `interface` blocks, annotations on a
    `const`/`let`, on a function parameter or on a return, and `as` casts. A
    name the module reads only there is not a value read, and a value binding
    with that spelling shadows nothing."""
    def blank(m):
        return " " * len(m.group(0))
    code = re.sub(r"(?m)^(?:export\s+)?type\s[^\n]*", blank, code)
    for m in list(re.finditer(r"(?m)^(?:export\s+)?interface\s", code))[::-1]:
        a, b = _brace_span(code, m.start())
        code = code[:a] + " " * (b - a) + code[b:]
    while True:  # innermost generic arguments first, so nesting unwinds
        new = re.sub(r"(?<=[\w$])<[^<>()=]*>", blank, code)
        if new == code:
            break
        code = new
    code = re.sub(rf"(?<=\))\s*:\s*{_TS_TYPE}", blank, code)
    code = re.sub(rf"(\b(?:const|let)\s+{_TS_IDENT})(\s*:\s*{_TS_TYPE})",
                  lambda m: m.group(1) + " " * len(m.group(2)), code)
    code = re.sub(rf"(?<![*\s])\s+as\s+{_TS_TYPE}", blank, code)

    def params(m):
        inner = re.sub(rf"(:\s*{_TS_TYPE})", blank, m.group(2))
        return m.group(1) + inner + ")"
    return re.sub(rf"(\bfunction\s+{_TS_IDENT}\s*\()([^()]*)\)", params, code)


def _brace_span(code: str, start: int) -> tuple[int, int]:
    """[start, end) from `start` through the brace that closes the first `{`
    after it."""
    depth = 0
    for j in range(code.index("{", start), len(code)):
        depth += {"{": 1, "}": -1}.get(code[j], 0)
        if depth == 0:
            return start, j + 1
    raise AssertionError("unbalanced braces in the emitted module")


def _declared_in(text: str) -> set:
    names = set(re.findall(rf"\b(?:const|let|var)\s+({_TS_IDENT})", text))
    names |= set(re.findall(rf"\bcatch\s*\(\s*({_TS_IDENT})", text))
    params = (re.findall(rf"\bfunction\s+{_TS_IDENT}\s*\(([^()]*)\)", text)
              + re.findall(r"\(([^()]*)\)\s*=>", text))
    for group in params:
        for param in group.split(","):
            names |= set(re.findall(rf"^\s*({_TS_IDENT})", param))
    return names


def _scaffolding_identifiers():
    """(names an extern must not take, names a component must not take),
    read off the emitted module rather than a hand list.

    Collected: every bare identifier in code (not a comment or string, not a
    `.property`, not an object key or annotated name), minus syntax and the
    probe's own `zq` names. Left out: names DECLARED in a sealed scope (the
    redaction helpers and each workflow's catch block) and used nowhere else;
    no author code is emitted there, so an author name cannot meet them. A
    component also may take a name the workflow function binds for itself
    (`saga`, `input`, ...): its function is never called from its own body."""
    code = _blank_type_positions(_strip_ts_comments_and_strings(EMIT.emit(
        compile_source(_SCAFFOLDING_PROBE, "zq_probe.rvl"), target="temporal")))
    seen: dict = {}
    for m in re.finditer(rf"(?<![\w$])({_TS_IDENT})", code):
        before = code[:m.start()]
        if before.endswith(".") and not before.endswith("..."):
            continue  # a property access, not a binding
        if re.match(r"\s*\??:", code[m.end():]):
            continue  # an object/type key or an annotated name
        seen.setdefault(m.group(1), []).append(m.start())
    sealed = [_brace_span(code, m.start())
              for m in re.finditer(r"(?m)^function\s", code)]
    sealed += [_brace_span(code, m.start())
               for m in re.finditer(r"\bcatch\s*\(err\)", code)]
    workflows = [_brace_span(code, m.start())
                 for m in re.finditer(r"(?m)^export async function\s", code)]

    def only_in(spans, positions):
        return all(any(a <= pos < b for a, b in spans) for pos in positions)

    sealed_local = set().union(*(_declared_in(code[a:b]) for a, b in sealed))
    workflow_local = set().union(*(_declared_in(code[a:b]) for a, b in workflows))
    externs = {
        name for name, positions in seen.items()
        if name not in _TS_SYNTAX and not name.lower().startswith("zq")
        and not (name in sealed_local and only_in(sealed, positions))
    }
    return frozenset(externs), frozenset(externs - workflow_local)


def _free_author_names() -> frozenset:
    """The probe's own `zq` names the emitted module reads but never binds:
    no destructuring, declaration, parameter or function names them, so each
    one resolves to whatever the host has under that spelling."""
    code = _strip_ts_comments_and_strings(EMIT.emit(
        compile_source(_SCAFFOLDING_PROBE, "zq_probe.rvl"), target="temporal"))
    bound = _declared_in(code)
    bound |= set(re.findall(
        rf"\b(?:function|interface|type)\s+({_TS_IDENT})", code))
    for group in re.findall(r"const\s*\{([^}]*)\}\s*=\s*proxyActivities", code):
        bound |= {name.strip() for name in group.split(",")}
    read = set()
    for m in re.finditer(r"(?<![\w$.])[zZ]q[\w$]*", code):
        if not re.match(r"\s*\??:", code[m.end():]):
            read.add(m.group(0))
    return frozenset(read - bound)


def test_every_author_name_read_free_is_refused_as_a_host_global():
    """The derived tests above skip the probe's own names, so they cannot see
    an author name the module reads free. This one finds those names in the
    emitted module (externs rendered as a bare call and never declared) and
    respells each as the host global `fetch`, which the scaffolding never
    reads: each respelling must be refused, since the bare call would reach
    the host's `fetch`."""
    free = _free_author_names()
    assert {"zqClose", "zqOpen", "zqDrop", "zqLease"} <= free  # positive control
    emitted = []
    for name in sorted(free):
        src = re.sub(rf"\b{name}\b", "fetch", _SCAFFOLDING_PROBE)
        try:
            EMIT.emit(compile_source(src, f"{name}_as_fetch.rvl"), target="temporal")
        except EMIT.EmitError as exc:
            if "reserves" not in str(exc):
                emitted.append(f"{name}: {exc}")
        else:
            emitted.append(name)
    assert not emitted, f"free author names that may take a host global: {emitted}"


def test_scaffolding_identifier_collector_sees_every_kind_of_name():
    """Positive control for the derived tests below: the collector finds an
    SDK import, a module helper, a host global read only in the abort path, a
    workflow-body binding, and it leaves out the catch-local `deadline`."""
    externs, components = _scaffolding_identifiers()
    assert {"proxyActivities", "redactResidueError", "Date", "String", "Set",
            "recordResidue", "saga", "input"} <= externs
    assert "deadline" not in externs and "zqSettle" not in externs
    # names read only in a type position are skipped (`Record<...>`, `SagaStep[]`)
    assert not {"Record", "SagaStep", "Residue", "SagaReport", "RevlActivities",
                "Promise"} & externs
    assert "activities" in externs  # a type-only import, but TS2440 binds it
    assert "Date" in components and "saga" not in components


def test_every_scaffolding_identifier_is_refused_as_an_extern_name():
    """An identifier the emitted module reads or binds where an extern's
    activity is destructured (module scope) or called (the workflow body) is
    refused as an extern name. Derived from the emitted module, so a name the
    template starts using later fails here until it is reserved."""
    externs, _ = _scaffolding_identifiers()
    emitted = []
    for name in sorted(externs):
        ir = compile_source(_extern_crossing_src(name), f"{name}.rvl")
        try:
            EMIT.emit(ir, target="temporal")
        except EMIT.EmitError as exc:
            if "reserves" not in str(exc):
                emitted.append(f"{name}: {exc}")
        else:
            emitted.append(name)
    assert not emitted, f"scaffolding names an extern can still take: {emitted}"


def test_every_module_scope_scaffolding_identifier_is_refused_as_a_component():
    _, components = _scaffolding_identifiers()
    emitted = []
    for name in sorted(components):
        ir = compile_source(f"component {name} {{ }}\n", f"{name}.rvl")
        try:
            EMIT.emit(ir, target="temporal")
        except EMIT.EmitError as exc:
            if "reserves" not in str(exc):
                emitted.append(f"{name}: {exc}")
        else:
            emitted.append(name)
    assert not emitted, f"scaffolding names a component can still take: {emitted}"


# --------------------------------------------------------------- residue-error redaction (item 421 F7)

def test_residue_error_static_shape_no_longer_bare_string_of_e():
    """`error: String(e)` (the finding's own quote) must be gone, replaced by
    the redaction funnel; the args thunk must be wired at the push site so the
    write-ahead referent (`h`, only assigned AFTER this push runs) is read
    lazily rather than frozen at its pre-acquire `undefined`."""
    src = (
        'extern pure fn r_close(h: RHandle) = @py { return }\n'
        'extern acquire fn r_open(n: Int) -> RHandle undo r_close(result)'
        ' = @py { return "h" }\n'
        'component Res {\n'
        '  let h = effect r_open(0) undo r_close(h)\n'
        '}\n'
    )
    ir = compile_source(src, "res_421_f7_shape.rvl")
    got = _emit_temporal_module().emit_temporal(ir)
    assert "error: String(e)" not in got
    assert "error: redactResidueError(e, step.args())" in got
    assert "args: () => [h]" in got
    assert "const REDACTED_ARG = '<redacted:arg>'" in got


def test_residue_error_redacts_host_text_from_all_three_sinks_end_to_end(tmp_path):
    """Item 421 F7, run for real: a compensation (`r_close`) that echoes the
    handle value it was called with into a thrown Error's message — exactly
    what `Secret[Str]` erasing to plain `string` in `RevlActivities` invites,
    with no type-level warning to the implementer. Runs the ACTUAL emitted
    module (not a hand-written stand-in) end to end against a stub Temporal
    SDK, and asserts the canary is ABSENT from all three sinks — the durable
    `recordResidue` record, `ApplicationFailure.details` (the one that
    PERSISTS IN TEMPORAL HISTORY for the namespace retention period), and the
    live residue query — while the redaction marker IS present in each, so
    the test cannot pass merely because nothing was emitted."""
    if shutil.which("node") is None:
        pytest.skip("node is not on PATH")

    # A second, always-failing crossing (`boom.trigger`) is needed to reach
    # compensation: if the ACQUIRE itself failed, `h` would still be
    # `undefined` and the write-ahead no-op guard would skip the compensation
    # call entirely (that guard is the point of the write-ahead pattern), so
    # the canary would never reach `r_close` at all.
    src = (
        'service Boom { emission fn trigger() -> Str }\n'
        'extern pure fn r_close(h: RHandle) = @py { return }\n'
        'extern acquire fn r_open(n: Int) -> RHandle undo r_close(result)'
        ' = @py { return "h" }\n'
        'component Res requires boom: Boom {\n'
        '  let h = effect r_open(0) undo r_close(h)\n'
        '  emit boom.trigger()\n'
        '}\n'
    )
    ir = compile_source(src, "res_421_f7.rvl")
    workflow_src = _emit_temporal_module().emit_temporal(
        ir, runtime_import="./temporal-stub.mjs")
    assert "args: () => [h]" in workflow_src
    assert "error: redactResidueError(e, step.args())" in workflow_src

    stub = """
export function proxyActivities() {
  globalThis.__proxyCalls = globalThis.__proxyCalls || []
  return new Proxy({}, {
    get(_target, prop) {
      return async (...args) => {
        globalThis.__proxyCalls.push({ name: prop, args })
        // boomTrigger is the forward step this fixture needs to fail, so the
        // saga aborts AFTER h has already been assigned the real (canary)
        // value by the acquire that landed just before it.
        if (prop === "boomTrigger") throw new Error("boom activity failed")
      }
    },
  })
}
export class ApplicationFailure extends Error {
  static create({ message, type, nonRetryable, details }) {
    const f = new ApplicationFailure(message)
    f.type = type
    f.nonRetryable = nonRetryable
    f.details = details
    return f
  }
}
const handlers = new Map()
export function setHandler(query, fn) { handlers.set(query, fn) }
export function defineQuery(name) { return { name } }
export function __runQuery(query) { return handlers.get(query)() }
"""
    (tmp_path / "temporal-stub.mjs").write_text(stub, encoding="utf-8")

    canary = "SEKRIT-CANARY-421-F7"
    driver = f"""
import {{ __runQuery }} from "./temporal-stub.mjs"
{workflow_src}
const CANARY = {json.dumps(canary)}
async function r_open(n) {{ return CANARY }}
function r_close(h) {{ throw new Error(`r_close failed for handle ${{h}}`) }}

async function main() {{
  try {{
    await Res()
    console.log(JSON.stringify({{ error: "Res() did not throw" }}))
  }} catch (e) {{
    const proxyCalls = globalThis.__proxyCalls ?? []
    const recordResidueCall = proxyCalls.find((c) => c.name === "recordResidue")
    console.log(JSON.stringify({{
      sink1_recordResidue: recordResidueCall,
      sink2_applicationFailureDetails: e.details,
      sink3_residueQuery: __runQuery(ResResidue),
    }}))
  }}
}}
main()
"""
    driver_file = tmp_path / "driver.ts"
    driver_file.write_text(driver, encoding="utf-8")

    result = subprocess.run(
        ["node", str(driver_file)], capture_output=True, text=True,
        cwd=tmp_path, timeout=30)
    assert result.returncode == 0, (
        f"driver script failed:\nstdout={result.stdout}\nstderr={result.stderr}")
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    blob = json.dumps(payload)

    # the canary must be ABSENT from every sink...
    assert canary not in blob, f"canary leaked into a sink: {payload}"
    # ...and the redaction marker must be PRESENT in every one of them, so
    # this cannot pass merely because recordResidue/details/query came back
    # empty or unreached.
    assert "<redacted:arg>" in json.dumps(payload["sink1_recordResidue"])
    assert "<redacted:arg>" in json.dumps(payload["sink2_applicationFailureDetails"])
    assert "<redacted:arg>" in json.dumps(payload["sink3_residueQuery"])

    # (3): ApplicationFailure.details is the sink that PERSISTS IN TEMPORAL
    # HISTORY — confirm it specifically, not just the bundle.
    details_blob = json.dumps(payload["sink2_applicationFailureDetails"])
    assert canary not in details_blob
    assert "<redacted:arg>" in details_blob
    assert "r_close failed for handle" in details_blob  # the TYPE/sentence survive

    # an operator still sees the error TYPE and the surrounding sentence —
    # only the caller's own bytes are gone.
    residue_entry = payload["sink3_residueQuery"][0]
    assert residue_entry["error"] == "Error: r_close failed for handle <redacted:arg>"
    assert residue_entry["name"] == "h.undo"
    assert residue_entry["kind"] == "compensation-residue"


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
    "to_char",
    "div_trunc", "div_floor", "div_euclid", "mod", "checked_div_trunc",
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


# ------------------------------------------------- evidence-derived retries (Slice 2)
#
# The whole of Slice 2's safety argument is "the retry count is DERIVED from
# evidence revl already has, and the derivation refuses to promote anything it
# cannot prove". These tests pin both halves: what earns a retry, and — the
# larger half — what deliberately does not.

_KEYED_SRC = (
    'extern emission idempotent(key: card) fn charge(card: Str, total: Int)'
    ' -> Str = @ts { return "ok" }\n'
    'extern emission fn refund(card: Str, total: Int) -> Str'
    ' = @ts { return "ok" }\n'
    'component Pay {\n'
    '  emit charge("visa", 100) compensate refund("visa", 100)\n'
    '}\n'
)


def _groups(text: str) -> tuple:
    """(at-most-once names, retryable names) from the emitted proxy groups."""
    at_most, retryable = set(), set()
    blocks = text.split("proxyActivities<typeof activities>({")
    header = blocks[0]
    for block in blocks[1:]:
        names = {n.strip() for n in
                 header.rsplit("const {", 1)[1].split("}", 1)[0].split(",")
                 if n.strip()}
        body, header = block.split("})", 1)
        (retryable if "DEDUP_SAFE_RETRY" in body else at_most).update(names)
    return at_most, retryable


def test_emission_extern_crossing_renders_as_an_activity():
    """Slice 2 widens the crossing mapping to a direct emission-extern call.

    Not a convenience: a service-interface METHOD carries only the bare
    `idempotent` modifier (parser.py still has `TODO(309-slice1)` for the keyed
    form), so an extern is the only crossing that can carry the item-309/440
    ledger the retry derivation reads. Without this the derivation would have
    no input that ever clears the bar."""
    got = _emit_temporal_module().emit_temporal(compile_source(_KEYED_SRC, "pay.revl"))
    assert "await charge(\"visa\", 100n)" in got
    assert 'saga.push({ name: "refund", run: () => refund("visa", 100n)' in got
    assert "  charge(card: string, total: bigint): Promise<string>" in got


def test_keyed_crossing_earns_a_bounded_retry():
    """`idempotent(key: <param>)` — item 309's `keyed` register, dedup-safe BY
    CONSTRUCTION — is the one class that earns a Temporal RetryPolicy."""
    got = _emit_temporal_module().emit_temporal(compile_source(_KEYED_SRC, "pay.revl"))
    at_most, retryable = _groups(got)
    assert retryable == {"charge"}
    assert {"refund", "recordResidue"} <= at_most
    assert "const DEDUP_SAFE_RETRY = {" in got
    assert "maximumAttempts: 5" in got


def test_retry_is_bounded_so_the_saga_can_still_abort():
    """The retry ceiling is not a timidity knob, it is a correctness one: a
    forward activity that retried forever would never throw, so the workflow's
    catch — and with it the whole derived compensation drain — would never
    run."""
    got = _emit_temporal_module().emit_temporal(compile_source(_KEYED_SRC, "pay.revl"))
    policy = got.split("const DEDUP_SAFE_RETRY = ", 1)[1].split("\n", 1)[0]
    assert "maximumAttempts:" in policy
    attempts = int(policy.split("maximumAttempts:", 1)[1].split("}", 1)[0].strip())
    assert 1 < attempts < 100, policy
    assert "backoffCoefficient" in policy and "maximumInterval" in policy


def test_declared_idempotent_does_not_earn_a_retry():
    """A bare `idempotent` — extern or service method — is the author's claim
    over an opaque host body, machine-checked for SHAPE only. Temporal retries
    on every transient failure, so promoting a claim here turns one unverified
    sentence into a production double-apply."""
    src = (
        'service Mail { emission idempotent fn send(to: Str) -> Str }\n'
        'extern emission idempotent fn ping(peer: Str) -> Str'
        ' = @ts { return "ok" }\n'
        'component Notify requires mail: Mail {\n'
        '  emit mail.send("a@b")\n'
        '  emit ping("h")\n'
        '}\n'
    )
    got = _emit_temporal_module().emit_temporal(compile_source(src, "notify.revl"))
    at_most, retryable = _groups(got)
    assert retryable == set()
    assert {"mailSend", "ping"} <= at_most
    assert "DEDUP_SAFE_RETRY" not in got


def test_undo_idempotent_does_not_earn_a_forward_retry():
    """item 309's INVERSE-side claim says nothing about re-delivering the
    forward, and is itself only a `declared` register."""
    temporal = _emit_temporal_module()
    src = (
        'extern pure fn r_close(h: RH) = @ts { return }\n'
        'extern acquire fn r_open(n: Int) -> RH undo idempotent r_close(result)'
        ' = @ts { return "h" }\n'
        'component Res {\n'
        '  let h = effect r_open(0) undo r_close(h)\n'
        '}\n'
    )
    ir = compile_source(src, "res.revl")
    entry = next(e for e in ir["externs"] if e["name"] == "r_open")
    assert entry["undo_idempotent"] is True and entry["register"] == "declared"
    index = temporal._Index(ir)
    call = {"kind": "fn", "name": "r_open"}
    assert temporal._forward_register(call, ir["components"][0], index) is None
    assert temporal._retry_class(call, ir["components"][0], index) \
        == temporal._AT_MOST_ONCE


def test_folded_register_read_never_promotes_a_forward_crossing():
    """THE trap this derivation exists to avoid.

    `lower.py::_idempotent_register` folds the forward-side and inverse-side
    claims into ONE `register` field, and its FIRST branch is the inverse-side
    one: `if decl.undo_read: return "read"`. So `undo pure` — a claim about the
    INVERSE — stamps `register: "read"`, and `read` is a member of item 440's
    `REDISPATCH_FREE`. A derivation that read the folded field would hand the
    FORWARD crossing a retry policy on the strength of a claim about its
    inverse, and re-run a mutation nobody said was safe to re-run.

    Compiled from real source, so the trap is the shipped fold, not a fixture."""
    temporal = _emit_temporal_module()
    src = (
        'type WT = { id: Str }\n'
        'extern pure fn chk(w: WT) -> Int = @ts { return 1 }\n'
        'extern witnessed fn w_write(x: Str) -> Result[WT, Str] undo pure'
        ' chk(result) = @ts { return x }\n'
        'component Res { }\n'
    )
    ir = compile_source(src, "read.revl")
    entry = next(e for e in ir["externs"] if e["name"] == "w_write")
    # the fold really does stamp the strongest tier in the order...
    assert entry["register"] == "read" and entry["undo_read"] is True
    from revl.recovery import REDISPATCH_FREE
    assert entry["register"] in REDISPATCH_FREE
    # ...and the forward derivation still refuses to promote it, because
    # nothing here says re-delivering `w_write` is safe.
    index = temporal._Index(ir)
    call = {"kind": "fn", "name": "w_write"}
    assert temporal._forward_register(call, ir["components"][0], index) is None
    assert temporal._retry_class(call, ir["components"][0], index) \
        == temporal._AT_MOST_ONCE


def test_validated_pins_to_at_most_once_even_when_keyed():
    """item 257: a `validated` crossing's `retry N` re-issues a completion
    THUNK revl-side. `lower.py` says it plainly — a completion is a read with a
    cost, not an idempotent write — so it must never be lowered to a Temporal
    RetryPolicy, even when the same declaration also carries a key that would
    otherwise earn one."""
    src = (
        'extern emission[model] validated retry 2 idempotent(key: card)'
        ' fn ask(card: Str) -> Str = @ts { return "ok" }\n'
        'component Pay {\n'
        '  emit ask("visa")\n'
        '}\n'
    )
    ir = compile_source(src, "ask.revl")
    entry = next(e for e in ir["externs"] if e["name"] == "ask")
    assert entry["register"] == "keyed" and entry["validated"] is True
    got = _emit_temporal_module().emit_temporal(ir)
    at_most, retryable = _groups(got)
    assert retryable == set() and "ask" in at_most
    # and the 257 retry budget is not smuggled into the policy either
    assert "maximumAttempts: 2" not in got


def test_record_residue_sink_is_at_most_once():
    """The residue sink is revl-EMITTED but host-IMPLEMENTED; no declaration
    carries evidence about it, so it takes the fail-closed default."""
    got = _emit_temporal_module().emit_temporal(compile_source(_KEYED_SRC, "pay.revl"))
    at_most, _ = _groups(got)
    assert "recordResidue" in at_most


def test_every_activity_lands_in_exactly_one_retry_group():
    """Total and disjoint: an activity the derivation forgot would be an
    undefined symbol in the emitted workflow."""
    got = _emit_temporal_module().emit_temporal(compile_source(_KEYED_SRC, "pay.revl"))
    at_most, retryable = _groups(got)
    assert not (at_most & retryable)
    declared = {line.strip().split("(", 1)[0]
                for line in got.split("export interface RevlActivities {", 1)[1]
                .splitlines() if line.startswith("  ")}
    assert at_most | retryable == declared


def test_derivation_agrees_with_revls_own_reissue_seam():
    """The retry classes are not a second, parallel idempotency vocabulary:
    they are item 440's `REDISPATCH_FREE` read at the forward position, and the
    `declared` refusal is `recovery._reissue_permitted`'s own answer when no
    operator strength knob is supplied — which a baked-in RetryPolicy can never
    supply, because there is no operator at the run."""
    temporal = _emit_temporal_module()
    from revl.recovery import REDISPATCH_FREE, _reissue_permitted

    assert temporal._RETRY_EARNING_REGISTERS <= REDISPATCH_FREE
    assert not _reissue_permitted("declared", None)
    assert not _reissue_permitted("keyed", None)
    assert _reissue_permitted("keyed", "keyed")
    # `read` is REDISPATCH_FREE and still not retry-earning here. That
    # asymmetry is the point of the test above: `read` is stamped by an
    # INVERSE-side claim, and this position asks a forward-side question.
    assert "read" in REDISPATCH_FREE
    assert "read" not in temporal._RETRY_EARNING_REGISTERS


def test_one_activity_name_reached_by_two_classes_downgrades():
    """Two crossings can derive one activity NAME (an extern named exactly what
    a `key.method` crossing mangles to). The signature cannot disagree, but the
    retry class can, so the weaker wins: a name reached even once without
    retry-earning evidence goes back to at-most-once. The only direction that
    can remove a retry rather than add one."""
    src = (
        'service Pay { emission fn charge(card: Str) -> Str }\n'
        'extern emission idempotent(key: card) fn payCharge(card: Str) -> Str'
        ' = @ts { return "ok" }\n'
        'component ViaExtern {\n'
        '  emit payCharge("visa")\n'
        '}\n'
        'component ViaService requires pay: Pay {\n'
        '  emit pay.charge("visa")\n'
        '}\n'
    )
    got = _emit_temporal_module().emit_temporal(compile_source(src, "clash.revl"))
    at_most, retryable = _groups(got)
    assert "payCharge" in at_most and "payCharge" not in retryable
