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
import json
import shutil
import subprocess
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
# Committed beside the other emitter fixtures so `tools/regen_goldens.py` emits
# the golden from the same bytes this test does.
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
