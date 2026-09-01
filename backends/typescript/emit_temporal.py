"""The Temporal emission target (roadmap item 253, Slice 1).

`revl emit --target temporal` is a RENDERING MODE of the existing TypeScript
emitter, not an eighth runtime and not a new tier under `backends/`
(docs/design/253-temporal-target.md §4). It reuses this backend's IR walk and
its `_expr`/`_ident`/`_literal` machinery, and changes only the SINK: where the
cordis target renders an activation body against `ctx.effect` and a `Frame`, the
Temporal target renders it against `proxyActivities` plus an explicit
compensation stack drained in the G7 LIFO order.

Slice 1 bakes in the six adversarial-review fixes from the v2 design note:

  * WRITE-AHEAD saga registration (CRITICAL 1). Each provisional compensation is
    pushed onto `saga[]` BEFORE its forward activity is awaited, keyed so it is a
    safe no-op if the forward effect never landed. A Temporal activity is
    at-least-once with an unreliable ack: it can COMMIT its host effect and still
    report failure, and with `maximumAttempts: 1` Temporal does not retry — it
    throws at the `await`. Registering after the await would leave a charged card
    with no refund on the stack. This mirrors recovery.py::_roll_back's
    referent-seeded, no-op-if-absent drain.

  * DERIVED CLOSED-ALLOWLIST refusal (CRITICAL 2). Any IR step or expression kind
    outside an explicit Slice-1 allowlist is refused with a why-trace naming the
    construct and its source line. A closed allowlist is the only posture
    consistent with the roadmap's "refused with a why-trace, never silently
    narrowed" contract: `await approval`, `session` commit/abort, `cache
    external`, `witnessed[fs]` and host-pinned coeffects, `spawn`, realms,
    reactive/lease coeffects, hot-swap, and signals/timers-as-control-flow all
    fall OUTSIDE the allowlist and are refused, never silently emitted.

  * WORKFLOW-SIDE budget, per-call `startToCloseTimeout` (HIGH 1). revl's
    `budgetMs` is a single Phase-2 total, checked workflow-side BETWEEN
    compensations (`Date.now()`, frozen to workflow-task time and replay-safe on
    the TS SDK). revl's `perCallMs` is the compensation activity's
    `startToCloseTimeout` (per-call, Temporal-honored). The budget is NEVER a
    schedule-to-close timeout.

  * `maximumAttempts: 1` on every activity and compensation (attack 3). No
    evidence-derived retries in Slice 1, so nothing can double-apply. This is
    FAITHFUL to revl: the TS runtime does not auto-retry forward emissions.

  * The determinism guard (attack 4) ships as a test asserting the pure-builtin
    table is a subset of a reviewed deterministic allowlist; see the temporal
    target test module.

  * A residue SINK (attack 7). On abort the drain runs a final `recordResidue`
    activity, attaches the `outstanding`/`worldRemaining`/`proof` envelope to the
    workflow-failure `details`, and exposes it through a query handler, so a
    failed run still carries the proof `revl recover` guarantees.

This is a CODE-GENERATION target. It emits the workflow/activity split, the
derived saga, and the two-phase drain revl proves at compile time. It does NOT
provide durable execution; a Temporal cluster runs it (§5).
"""

from __future__ import annotations

from typing import Any

# The Temporal renderer is a SINK VARIANT of this backend, so it borrows the
# backend's identifier / literal / scope / type machinery unchanged. emit.py is
# loaded under several names across the repo (`emit` for the standalone CLI,
# `revl_bundle_typescript_emit` for the bundle), so `emit.py::emit` registers
# itself as `sys.modules["emit"]` and puts backends/typescript on the path
# BEFORE it imports this module — the import below then resolves to that one
# shared module, keeping a single copy of the emitter machinery (`EmitError`
# identity, the `_Ctx` counter cell) no matter which entry point dispatched.
from emit import (  # type: ignore[import-not-found]  # noqa: E402
    EmitError,
    _Ctx,
    _Scope,
    _ident,
    _literal,
    _mangle,
    _string,
    _ts_type,
)

# revl `budgetMs` default (runtime.ts:880): a SINGLE Phase-2 total budget,
# checked workflow-side BETWEEN compensations. Never a per-activity timeout.
_COMPENSATION_BUDGET_MS = 5000

# revl `perCallMs`: the per-compensation cutoff, mapped to each compensation
# activity's `startToCloseTimeout` (Temporal-honored, per call).
_START_TO_CLOSE_TIMEOUT = "1 minute"

# ------------------------------------------------------------ closed allowlist

# The Slice-1 mappable STEP kinds (docs/design/253-temporal-target.md §5):
# pure/effect statements in workflow position, an emission crossing, and the
# bracket/compensation registrations. Everything else is refused.
_STEP_ALLOWLIST = frozenset({"emit", "effect", "let-effect", "if"})

# The Slice-1 mappable EXPRESSION kinds. This set is exactly what `_t_expr`
# below can render into a Temporal workflow, so the allowlist and the renderer
# never disagree. A richer pure expression is refused by default until it is
# added here (the fail-closed posture the design requires): `spawn`,
# `instance-get`, `host`, and every un-listed 2.0 kind fall outside.
_EXPR_ALLOWLIST = frozenset({
    "lit", "config", "name", "var", "req", "field", "bin", "un", "fn", "call",
})

# A `witnessed` extern carrying any of these coeffects is HOST-PINNED: its
# inverse must run on the worker that performed the forward mutation, which
# `proxyActivities` cannot guarantee (HIGH 2). Refused in Slice 1; the
# host-affinity task-queue pattern is a later lift.
_HOST_PINNED_COEFFECTS = frozenset({
    "fs", "proc", "exec", "socket", "net_local", "process", "clock",
})


class TemporalRefusal(EmitError):
    """A construct outside the Slice-1 allowlist, refused with a why-trace.

    A distinct subclass so a caller can tell a derived-allowlist refusal apart
    from an ordinary emit error, while it still travels this tier's existing
    `EmitError` channel (so `revl bundle` records it as a skip, not a crash)."""


def _line_of(node: Any) -> object:
    """The source line for a why-trace, best-effort. Steps rarely carry a line
    of their own, so fall through to the acquire/expr/undo/compensate child the
    lowerer stamped (a `spawn` acquire and an `approval` step both carry one)."""
    if isinstance(node, dict):
        if node.get("line") is not None:
            return node["line"]
        for key in ("acquire", "expr", "undo", "compensate", "cond"):
            child = node.get(key)
            child_line = _line_of(child)
            if child_line != "?":
                return child_line
    return "?"


def _refuse(construct: str, component: str, source: str, line: object,
            why: str) -> None:
    raise TemporalRefusal(
        f"--target temporal refuses `{construct}` in component {component!r} "
        f"({source}:{line}): {why}. Slice 1 emits a derived CLOSED ALLOWLIST — "
        f"a construct outside the mappable subset is refused with this why-trace, "
        f"never silently narrowed to a workflow that drops its behaviour "
        f"(docs/design/253-temporal-target.md §5)."
    )


def _witnessed_by_name(ir: dict) -> dict:
    return {ext["name"]: ext for ext in (ir.get("externs") or [])
            if ext.get("class") == "witnessed"}


def _check_expr(node: Any, component: str, source: str, witnessed: dict) -> None:
    """Refuse any expression node kind outside the closed allowlist, and any
    host-pinned witnessed crossing reached through a call/fn node."""
    if isinstance(node, list):
        for item in node:
            _check_expr(item, component, source, witnessed)
        return
    if not isinstance(node, dict):
        return
    kind = node.get("kind")
    if kind is None:
        for value in node.values():
            _check_expr(value, component, source, witnessed)
        return
    if kind == "spawn":
        _refuse("spawn", component, source, node.get("line", "?"),
                "spawn isolates each provision into a fresh local realm under a "
                "supervision tree; revl's disposable-handle spawn semantics do "
                "not correspond to a Temporal child-workflow lifecycle (attack 1)")
    if kind == "instance-get":
        _refuse("spawn instance access", component, source, node.get("line", "?"),
                "reading a spawned instance is instance-parametric and has no "
                "Temporal mapping in Slice 1 (attack 1)")
    if kind == "host":
        _refuse("host builtin", component, source, node.get("line", "?"),
                "a host-root call is IO in workflow position, which Temporal "
                "forbids and revl keeps out of the workflow stratum")
    if kind not in _EXPR_ALLOWLIST:
        _refuse(str(kind), component, source, node.get("line", "?"),
                "this expression kind is outside the Slice-1 mappable allowlist")
    # a witnessed extern reached as a crossing — host affinity (HIGH 2).
    if kind == "fn" and node.get("name") in witnessed:
        _check_host_pinned(witnessed[node["name"]], component, source,
                           node.get("line", "?"))
    # recurse into children (args, operands, targets, ...)
    for value in node.values():
        if isinstance(value, (dict, list)):
            _check_expr(value, component, source, witnessed)


def _check_host_pinned(ext: dict, component: str, source: str,
                       line: object) -> None:
    caps = set(ext.get("capabilities") or [])
    pinned = caps & _HOST_PINNED_COEFFECTS
    # A witnessed extern with any coeffect is treated as host-affine for Slice 1
    # (the exit saga uses only remote resources, so nothing in scope needs one).
    offending = pinned or caps
    if offending:
        _refuse(f"witnessed[{','.join(sorted(offending))}] `{ext.get('name')}`",
                component, source, line,
                "a host-local inverse must run on the worker that performed the "
                "forward mutation, but proxyActivities dispatches to any worker "
                "on the task queue (HIGH 2); the host-affinity task-queue "
                "pattern is a later slice")


def _check_crossing_target(call: dict, component: str, source: str,
                           line: object) -> None:
    """A crossing must be a service method call (`req.method(...)`). Anything
    else (a bare fn, a 2.0-shaped `callee` call) is outside the Slice-1 mapping."""
    if call.get("kind") != "call" or "target" not in call:
        _refuse("non-service crossing", component, source, line,
                "Slice 1 maps only a service-method emission (`key.method(...)`) "
                "to an activity")
    target = call.get("target") or {}
    if target.get("kind") != "req":
        _refuse("crossing on a non-requirement target", component, source, line,
                "the activity name is derived from the requirement key and the "
                "method; a crossing on another target has no Slice-1 mapping")


def _refuse_outside_allowlist(ir: dict) -> None:
    """Walk every component activation body and refuse any step or expression
    outside the Slice-1 closed allowlist, with a why-trace naming the construct
    and line. Called at the top of `emit_temporal` so refusal precedes emission."""
    witnessed = _witnessed_by_name(ir)
    for comp in ir.get("components") or []:
        name = comp.get("name") or "?"
        source = comp.get("source") or comp.get("file") or "<source>"
        _walk_steps(comp.get("body") or [], name, source, witnessed)


def _walk_steps(steps: list, component: str, source: str,
                witnessed: dict) -> None:
    for step in steps or []:
        kind = step.get("step")
        # `await approval` (item 246) surfaces either as its own `approval` step
        # or as an `emit` step carrying an `approval` edge — refuse both, since
        # each maps only to a Temporal signal, which is out of Slice-1 scope.
        if kind == "approval" or (kind == "emit" and step.get("approval") is not None):
            _refuse("await approval", component, source, _line_of(step),
                    "a typed approval gates an irreversible crossing and maps "
                    "only to a Temporal signal (out of Slice-1 scope); emitting "
                    "it as an ordinary activity would silently drop a human gate")
        if kind not in _STEP_ALLOWLIST:
            _refuse(kind or "<unknown>", component, source, _line_of(step),
                    "this activation step is outside the Slice-1 mappable subset "
                    "(mappable: an emission crossing, a bracket/compensation "
                    "registration, a pure guard)")
        if kind == "emit":
            _check_crossing_target(step.get("expr") or {}, component, source,
                                   _line_of(step))
            _check_expr(step.get("expr"), component, source, witnessed)
            if step.get("compensate") is not None:
                _check_crossing_target(step["compensate"], component, source,
                                       _line_of(step))
                _check_expr(step["compensate"], component, source, witnessed)
        elif kind in ("effect", "let-effect"):
            _check_expr(step.get("acquire"), component, source, witnessed)
            if step.get("undo") is not None:
                _check_expr(step.get("undo"), component, source, witnessed)
        elif kind == "if":
            _check_expr(step.get("cond"), component, source, witnessed)
            _walk_steps(step.get("then") or [], component, source, witnessed)
            _walk_steps(step.get("else") or [], component, source, witnessed)


# ------------------------------------------------------------ expression sink

_BIN_OPS = {
    "+": "+", "-": "-", "*": "*", "%": "%",
    "<": "<", "<=": "<=", ">": ">", ">=": ">=",
    "and": "&&", "or": "||", "&&": "&&", "||": "||",
}


def _t_expr(node: Any, scope: "_Scope") -> str:
    """Render a Slice-1-allowlisted expression into Temporal workflow text.

    This is the sink's own small renderer, matched exactly to `_EXPR_ALLOWLIST`.
    It differs from the cordis `_expr` in only two places: a `config` field reads
    off the workflow `input` object (a Temporal workflow's argument), and a
    crossing `call` is emitted as an activity call rather than a `ctx.<key>`
    method call. A literal, name, field, or arithmetic node renders identically."""
    if not isinstance(node, dict):
        raise EmitError(f"malformed expression: {node!r}")
    kind = node.get("kind")
    if kind == "lit":
        return _literal(node.get("value"))
    if kind == "config":
        return f"input.{_ident(node.get('field'), 'config field')}"
    if kind == "name":
        return _mangle(_ident(node.get("id"), "name"))
    if kind == "var":
        return _mangle(_ident(node.get("name"), "name"))
    if kind == "field":
        return f"{_t_expr(node.get('target'), scope)}.{_ident(node.get('name'), 'field')}"
    if kind == "un":
        op = node.get("op")
        if op == "!":
            return f"(!{_t_expr(node.get('operand'), scope)})"
        if op == "-":
            return f"(-{_t_expr(node.get('operand'), scope)})"
        raise EmitError(f"unsupported unary operator {op!r} for --target temporal")
    if kind == "bin":
        op = _BIN_OPS.get(node.get("op"))
        if op is None:
            raise EmitError(
                f"unsupported binary operator {node.get('op')!r} for --target temporal")
        return f"({_t_expr(node.get('left'), scope)} {op} {_t_expr(node.get('right'), scope)})"
    if kind == "call" and "target" in node:
        # a nested crossing used as an argument — render as an activity call
        return _activity_call(node, scope)
    if kind == "fn":
        args = ", ".join(_t_expr(a, scope) for a in node.get("args") or [])
        return f"{_mangle(_ident(node.get('name'), 'function'))}({args})"
    raise EmitError(
        f"expression kind {kind!r} has no --target temporal rendering "
        f"(it should have been refused by the closed allowlist)")


def _activity_name(call: dict) -> str:
    """The activity name derived from a crossing `key.method(...)`: the
    requirement key joined to the capitalised method (`flights` + `reserve` ->
    `flightsReserve`). Deterministic, so the workflow and its `activities.ts`
    agree without a table."""
    key = _ident((call.get("target") or {}).get("name"), "requirement")
    method = _ident(call.get("method"), "method")
    return _mangle(key + method[:1].upper() + method[1:])


def _crossing_label(call: dict) -> str:
    key = (call.get("target") or {}).get("name")
    return f"{key}.{call.get('method')}"


def _activity_call(call: dict, scope: "_Scope") -> str:
    args = ", ".join(_t_expr(a, scope) for a in call.get("args") or [])
    return f"{_activity_name(call)}({args})"


# ------------------------------------------------------------ activity registry

class _Activity:
    __slots__ = ("name", "params", "returns")

    def __init__(self, name: str, params: str, returns: str) -> None:
        self.name = name
        self.params = params
        self.returns = returns


def _crossing_signature(call: dict, comp: dict, services: dict,
                        known_types: frozenset) -> _Activity:
    """The activity signature for a crossing, read off the service method the
    requirement key resolves to (params and return type), so the emitted
    `activities.ts` interface is typed."""
    key = (call.get("target") or {}).get("name")
    method = call.get("method")
    svc_name = (comp.get("requires") or {}).get(key)
    method_spec = (((services.get(svc_name) or {}).get("methods") or {})
                   .get(method) or {})
    params = ", ".join(
        f"{_ident(p.get('name'), 'parameter')}: {_ts_type(p.get('type'), known_types)}"
        for p in method_spec.get("params") or [])
    returns = (_ts_type(method_spec["returns"], known_types)
               if method_spec.get("returns") else "void")
    return _Activity(_activity_name(call), params, f"Promise<{returns}>")


# ------------------------------------------------------------ component render

def _render_component(comp: dict, ir: dict, ctx: "_Ctx", services: dict,
                      activities: dict) -> list[str]:
    name = _ident(comp.get("name"), "component")
    scope = _Scope(comp)
    for local in (comp.get("requires") or {}):
        _ident(local, "requirement")
    known_types = frozenset(ir.get("types") or {})
    config_fields = comp.get("config") or []

    def register(call: dict) -> None:
        act = _crossing_signature(call, comp, services, known_types)
        activities.setdefault(act.name, act)

    body: list[str] = []

    def emit_steps(steps: list, indent: str) -> None:
        for step in steps or []:
            kind = step.get("step")
            if kind == "emit":
                fwd = step["expr"]
                register(fwd)
                if step.get("compensate") is not None:
                    comp_call = step["compensate"]
                    register(comp_call)
                    # WRITE-AHEAD: the provisional inverse is pushed BEFORE the
                    # forward is awaited, so an acked-but-lost forward effect is
                    # still compensated (CRITICAL 1). Its args are keys, so the
                    # compensation is a safe no-op if the forward never landed.
                    body.append(
                        f"{indent}saga.push({{ name: {_string(_crossing_label(comp_call))}, "
                        f"run: () => {_activity_call(comp_call, scope)} }})  "
                        f"// write-ahead: registered before the forward await")
                body.append(f"{indent}await {_activity_call(fwd, scope)}")
            elif kind in ("effect", "let-effect"):
                acquire = step["acquire"]
                bind = step.get("bind") if kind == "let-effect" else None
                if bind is not None:
                    bind = scope.bind(bind)
                if step.get("undo") is not None and bind is not None:
                    # A bracket whose inverse names the bound result: declare the
                    # holder first (undefined), push a no-op-if-absent inverse
                    # BEFORE the await, then assign — the write-ahead, referent-
                    # seeded pattern recovery.py uses so a mid-effect failure is
                    # still covered.
                    body.append(f"{indent}let {bind}: unknown = undefined")
                    body.append(
                        f"{indent}saga.push({{ name: {_string(bind + '.undo')}, "
                        f"run: async () => {{ if ({bind} !== undefined) "
                        f"{{ {_t_expr(step['undo'], scope)} }} }} }})  "
                        f"// write-ahead: no-op if the acquire never landed")
                    body.append(f"{indent}{bind} = await {_t_expr(acquire, scope)}")
                elif step.get("undo") is not None:
                    body.append(
                        f"{indent}saga.push({{ name: {_string('undo')}, "
                        f"run: async () => {{ {_t_expr(step['undo'], scope)} }} }})  "
                        f"// write-ahead: registered before the forward await")
                    body.append(f"{indent}await {_t_expr(acquire, scope)}")
                elif bind is not None:
                    body.append(f"{indent}const {bind} = await {_t_expr(acquire, scope)}")
                else:
                    body.append(f"{indent}await {_t_expr(acquire, scope)}")
            elif kind == "if":
                body.append(f"{indent}if ({_t_expr(step['cond'], scope)}) {{")
                emit_steps(step.get("then") or [], indent + "  ")
                if step.get("else"):
                    body.append(f"{indent}}} else {{")
                    emit_steps(step["else"], indent + "  ")
                body.append(f"{indent}}}")
            else:  # pragma: no cover — the allowlist walk refuses everything else
                raise EmitError(f"unmappable step reached the sink: {kind!r}")

    emit_steps(comp.get("body") or [], "    ")

    signature = f"input: {name}Input" if config_fields else ""
    lines: list[str] = []
    if config_fields:
        lines.append(f"export interface {name}Input {{")
        for field in config_fields:
            lines.append(
                f"  {_ident(field['name'], 'config field')}: "
                f"{_ts_type(field.get('type'), known_types)}")
        lines.append("}")
        lines.append("")
    lines.append(
        f"// The residue envelope revl recover guarantees "
        f"(outstanding/worldRemaining/proof), exposed for live inspection while "
        f"an aborting {name} run drains (attack 7).")
    lines.append(f"export const {name}Residue = defineQuery<Residue[]>({_string(name + '.residue')})")
    lines.append("")
    lines.append(f"export async function {name}({signature}): Promise<void> {{")
    lines.append("  // The compensation stack IS the derived saga: pushed write-ahead,")
    lines.append("  // drained LIFO in the G7 order recovery.py uses, not hand-authored.")
    lines.append("  const saga: SagaStep[] = []")
    lines.append("  const residue: Residue[] = []")
    lines.append(f"  setHandler({name}Residue, () => residue)")
    lines.append("  try {")
    lines.extend(body)
    lines.append("    // clean completion: the saga stack is discharged, never run.")
    lines.append("  } catch (err) {")
    lines.append("    // ABORT. Phase 2: pop LIFO, continue-and-record, honour the")
    lines.append("    // WORKFLOW-SIDE budget between compensations. A byte-for-byte")
    lines.append("    // port of runtime.ts::runPhase2 (HIGH 1).")
    lines.append("    const deadline = Date.now() + COMPENSATION_BUDGET_MS  "
                 "// frozen to workflow-task time; replay-safe")
    lines.append("    for (const step of saga.reverse()) {")
    lines.append("      if (Date.now() >= deadline) {")
    lines.append("        residue.push({ kind: 'compensation-residue', name: step.name,")
    lines.append("                       reason: 'deadline-expired', outcome: 'not-attempted' })")
    lines.append("        continue  // record and skip; the budget stops the phase, not one call")
    lines.append("      }")
    lines.append("      // the per-call cutoff is each compensation activity's")
    lines.append("      // startToCloseTimeout, NOT this budget (HIGH 1).")
    lines.append("      try { await step.run() }")
    lines.append("      catch (e) { residue.push({ kind: 'compensation-residue', "
                 "name: step.name, error: String(e) }) }")
    lines.append("    }")
    lines.append("    // Residue sink (attack 7): a durable record, plus the same")
    lines.append("    // envelope on the workflow-failure details, so a failed run")
    lines.append("    // still carries the proof revl recover produces.")
    lines.append("    const report = { outstanding: residue, worldRemaining: saga.length, "
                 "proof: 'revl-saga-abort' }")
    lines.append("    await recordResidue(report)")
    lines.append("    throw ApplicationFailure.create({ message: 'saga aborted', "
                 "type: 'SagaAbort', nonRetryable: true, details: [report] })")
    lines.append("  }")
    lines.append("}")
    return lines


# ------------------------------------------------------------ module render

def emit_temporal(ir: dict, *, runtime_import: str = "@temporalio/workflow") -> str:
    """Emit a Temporal TypeScript-SDK workflow module for an IR document.

    `runtime_import` names the workflow SDK import (defaulting to the real
    package); it exists so a test can pin the shape without a node install."""
    if not isinstance(ir, dict):
        raise EmitError("IR document must be an object")
    if ir.get("ir_version") not in (1, 2, 3):
        raise EmitError(f"unsupported ir_version: {ir.get('ir_version')!r}")
    components = ir.get("components") or []
    if not components:
        raise EmitError(
            "--target temporal needs at least one component: a Temporal workflow "
            "is a rendering of a component activation (docs/design/253-temporal-target.md §1)")

    # Refusal precedes emission (the closed allowlist, CRITICAL 2).
    _refuse_outside_allowlist(ir)

    services = ir.get("services") or {}
    doc_ctx = _Ctx(ir.get("types") or {}, ir.get("functions") or [],
                   ir.get("externs") or [], services=services)

    activities: dict = {}
    rendered: list[str] = []
    seen: set = set()
    for comp in components:
        if comp.get("name") in seen:
            raise EmitError(f"duplicate component name: {comp.get('name')!r}")
        seen.add(comp.get("name"))
        rendered.extend(_render_component(comp, ir, doc_ctx, services, activities))
        rendered.append("")

    # `recordResidue` is the durable residue sink activity every workflow drains
    # into on abort (attack 7).
    activities.setdefault(
        "recordResidue",
        _Activity("recordResidue", "report: SagaReport", "Promise<void>"))

    proxy_names = ", ".join(sorted(activities))

    out: list[str] = [
        "// Generated by revl backends/typescript/emit.py (--target temporal) — do not edit.",
        "// Target: the Temporal TypeScript SDK. This is a CODE-GENERATION target: it",
        "// emits the workflow/activity split, the derived write-ahead saga, and the",
        "// two-phase drain revl proves at compile time. It does NOT provide durable",
        "// execution; a Temporal cluster runs it (docs/design/253-temporal-target.md §5).",
        f"import {{ proxyActivities, ApplicationFailure, setHandler, defineQuery }} "
        f"from {_string(runtime_import)}",
        "import type * as activities from './activities'",
        "",
        "// Slice 1: every activity and compensation is at-most-once "
        "(maximumAttempts: 1).",
        "// FAITHFUL to revl — the TS runtime does not auto-retry forward emissions",
        "// either. Evidence-derived retries are Slice 2 "
        "(docs/design/253-temporal-target.md §3).",
        f"const {{ {proxy_names} }} = proxyActivities<typeof activities>({{",
        f"  startToCloseTimeout: {_string(_START_TO_CLOSE_TIMEOUT)},  "
        f"// revl perCallMs — per-call, Temporal-honoured (HIGH 1)",
        "  retry: { maximumAttempts: 1 },",
        "})",
        "",
        "// revl budgetMs (runtime.ts:880): a SINGLE Phase-2 total budget, checked",
        "// workflow-side BETWEEN compensations (HIGH 1). Never a per-activity timeout.",
        f"const COMPENSATION_BUDGET_MS = {_COMPENSATION_BUDGET_MS}",
        "",
        "type SagaStep = { name: string; run: () => Promise<unknown> }",
        "type Residue = Record<string, unknown>",
        "type SagaReport = { outstanding: Residue[]; worldRemaining: number; proof: string }",
        "",
    ]
    out.extend(rendered)

    # The companion `activities.ts` contract: the crossing implementations the
    # workflow proxies, typed off each service method. The impl is host code the
    # operator writes; revl emits the SHAPE it must satisfy.
    out.append("// ---- activities.ts (implement the crossings; host IO lives here) ----")
    out.append("export interface RevlActivities {")
    for act in sorted(activities.values(), key=lambda a: a.name):
        out.append(f"  {act.name}({act.params}): {act.returns}")
    out.append("}")

    return "\n".join(out).rstrip() + "\n"
