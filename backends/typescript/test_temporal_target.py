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
