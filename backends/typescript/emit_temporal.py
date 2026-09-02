"""The Temporal emission target (roadmap item 253, Slices 1-2).

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

  * `maximumAttempts: 1` as the FALL-THROUGH for every activity and
    compensation (attack 3), never as a blanket. Slice 2 derives each activity's
    retry policy from the item-309/440 idempotency ledger the crossing's
    declaration already carries (§3, and `_forward_register` below), and only a
    crossing whose evidence PROVES re-delivery is safe — today, a declaration
    carrying `idempotent(key: <param>)` — leaves the at-most-once class. Every
    other class stays at 1, which is FAITHFUL to revl: the TS runtime does not
    auto-retry forward emissions either.

Slice 2 also widens the crossing mapping to a DIRECT EMISSION-EXTERN call
(`emit charge(...)`, not only `emit key.method(...)`). That is not a
convenience: a service-interface method carries only the bare `idempotent`
modifier (`parser.py` still has `TODO(309-slice1)` for the keyed form), so an
extern is the only crossing that can carry the ledger the derivation reads.
Without it the derivation would have no input that ever clears the bar.

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

# ------------------------------------------------- retry classes (Slice 2, §3)

#: The two retry classes an activity can land in. A class is a NAME, not a
#: number, so the derivation below decides membership and the renderer decides
#: the policy text; nothing in the walk hard-codes an attempt count.
_AT_MOST_ONCE = "at-most-once"
_KEYED = "keyed"

#: The registers that earn a Temporal retry. This is item 440's
#: `recovery.REDISPATCH_FREE` — "may be issued AGAIN without spending a fence"
#: — read at the FORWARD position, which is the only question a Temporal
#: `RetryPolicy` asks. `shape-proven` is listed because the partial order names
#: it a peer of `keyed`; `lower.py::_idempotent_register` cannot yet produce it
#: (TODO(309-slice4)), so today the set is reachable only through `keyed`.
_RETRY_EARNING_REGISTERS = frozenset({"keyed", "shape-proven"})

# The bounded backoff a retry-earning crossing gets. BOUNDED, not unbounded: a
# forward activity that retries forever never throws, so the workflow's catch
# never runs and the derived compensation phase never drains. The saga's abort
# path is the reason an attempt ceiling exists at all.
_RETRY_INITIAL_INTERVAL = "1 second"
_RETRY_BACKOFF_COEFFICIENT = 2
_RETRY_MAXIMUM_INTERVAL = "30 seconds"
_RETRY_MAXIMUM_ATTEMPTS = 5

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


def _check_expr(node: Any, component: str, source: str, witnessed: dict,
                index: "_Index") -> None:
    """Refuse any expression node kind outside the closed allowlist, and any
    host-pinned witnessed crossing reached through a call/fn node."""
    if isinstance(node, list):
        for item in node:
            _check_expr(item, component, source, witnessed, index)
        return
    if not isinstance(node, dict):
        return
    kind = node.get("kind")
    if kind is None:
        for value in node.values():
            _check_expr(value, component, source, witnessed, index)
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
            _check_expr(value, component, source, witnessed, index)


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
                           line: object, index: "_Index") -> None:
    """A crossing must be a service-method call (`req.method(...)`) or, since
    Slice 2, a direct emission-extern call (`charge(...)`).

    The extern form is what makes §3's retry derivation reachable at all: a
    service-interface method carries only the bare `idempotent` modifier (the
    `declared` register), while an extern carries the whole item-309/440 ledger
    — `idempotent(key: p)`, `undo idempotent`, `undo pure`. Anything else (a
    2.0-shaped `callee` call, a crossing on a non-requirement target) is still
    outside the mapping."""
    if call.get("kind") == "fn":
        if not index.is_emission_extern(call.get("name")):
            _refuse(f"crossing to `{call.get('name')}`", component, source, line,
                    "a direct crossing must name an EMISSION extern; this name "
                    "is not one, so there is no activity to dispatch it to")
        return
    if call.get("kind") != "call" or "target" not in call:
        _refuse("non-service crossing", component, source, line,
                "this target maps a service-method emission (`key.method(...)`) "
                "or a direct emission-extern call (`extern(...)`) to an activity")
    target = call.get("target") or {}
    if target.get("kind") != "req":
        _refuse("crossing on a non-requirement target", component, source, line,
                "the activity name is derived from the requirement key and the "
                "method; a crossing on another target has no mapping")


def _refuse_outside_allowlist(ir: dict, index: "_Index") -> None:
    """Walk every component activation body and refuse any step or expression
    outside the closed allowlist, with a why-trace naming the construct and
    line. Called at the top of `emit_temporal` so refusal precedes emission."""
    witnessed = _witnessed_by_name(ir)
    for comp in ir.get("components") or []:
        name = comp.get("name") or "?"
        source = comp.get("source") or comp.get("file") or "<source>"
        _walk_steps(comp.get("body") or [], name, source, witnessed, index)


def _walk_steps(steps: list, component: str, source: str,
                witnessed: dict, index: "_Index") -> None:
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
                                   _line_of(step), index)
            _check_expr(step.get("expr"), component, source, witnessed, index)
            if step.get("compensate") is not None:
                _check_crossing_target(step["compensate"], component, source,
                                       _line_of(step), index)
                _check_expr(step["compensate"], component, source, witnessed, index)
        elif kind in ("effect", "let-effect"):
            _check_expr(step.get("acquire"), component, source, witnessed, index)
            if step.get("undo") is not None:
                _check_expr(step.get("undo"), component, source, witnessed, index)
        elif kind == "if":
            _check_expr(step.get("cond"), component, source, witnessed, index)
            _walk_steps(step.get("then") or [], component, source, witnessed, index)
            _walk_steps(step.get("else") or [], component, source, witnessed, index)


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
    """The activity name derived from a crossing.

    A service-method crossing (`key.method(...)`) joins the requirement key to
    the capitalised method (`flights` + `reserve` -> `flightsReserve`); an
    emission-extern crossing (`charge(...)`, Slice 2) is the extern's own name.
    Deterministic either way, so the workflow and its `activities.ts` agree
    without a table."""
    if call.get("kind") == "fn":
        return _mangle(_ident(call.get("name"), "emission extern"))
    key = _ident((call.get("target") or {}).get("name"), "requirement")
    method = _ident(call.get("method"), "method")
    return _mangle(key + method[:1].upper() + method[1:])


def _crossing_label(call: dict) -> str:
    if call.get("kind") == "fn":
        return str(call.get("name"))
    key = (call.get("target") or {}).get("name")
    return f"{key}.{call.get('method')}"


def _activity_call(call: dict, scope: "_Scope") -> str:
    args = ", ".join(_t_expr(a, scope) for a in call.get("args") or [])
    return f"{_activity_name(call)}({args})"


def _redaction_args(node: Any, scope: "_Scope") -> str:
    """A lazy TS thunk over the runtime values `node` (a compensate call or an
    undo expression) touches, for the residue-error redaction funnel (item 421
    F7): a failed compensation's error text can embed any of these values, so
    `redactResidueError` scrubs them out of it before the residue record, the
    `ApplicationFailure.details` Temporal persists, and the residue query ever
    see the text.

    Walks every `name`/`var`/`config`/`field` leaf reachable in the node (the
    same shape `_check_expr` already walks for the allowlist refusal), so a
    literal argument contributes nothing (an author-written constant is not a
    secret) while a bound variable — including the write-ahead referent itself,
    e.g. `undo r_close(h)` — does. Rendered as a closure (`() => [...]`), not a
    value captured at push time: the write-ahead pattern pushes an undo's
    registration BEFORE the acquire it guards has run, so the referent it
    closes over is only assigned afterwards, and reading it eagerly here would
    freeze it at its pre-acquire `undefined`."""
    exprs: list[str] = []
    seen: set = set()

    def walk(n: Any) -> None:
        if isinstance(n, list):
            for item in n:
                walk(item)
            return
        if not isinstance(n, dict):
            return
        kind = n.get("kind")
        if kind in ("name", "var", "config", "field"):
            text = _t_expr(n, scope)
            if text not in seen:
                seen.add(text)
                exprs.append(text)
            return  # `_t_expr` already rendered a field's target chain whole
        for value in n.values():
            if isinstance(value, (dict, list)):
                walk(value)

    walk(node)
    return "() => [" + ", ".join(exprs) + "]"


# ------------------------------------------------- evidence-derived retries

def _forward_register(call: dict, comp: dict, index: "_Index") -> str | None:
    """The register governing whether the crossing `call` may be ISSUED AGAIN.

    This is the ONLY question a Temporal `RetryPolicy` asks, and it is NOT the
    same question `lower.py::_idempotent_register` answers. That function folds
    the forward-side and the inverse-side claims into ONE value, and its FIRST
    branch is the inverse-side one::

        if decl.undo_read:          return "read"      # item 440: `undo pure`
        if decl.idempotency_key:    return "keyed"     # item 309: forward key
        return "declared"

    So an extern written `emission fn charge(...) undo pure receipt(x)` carries
    `register: "read"` while saying NOTHING about re-delivering `charge`. Reading
    the folded `register` here would enable Temporal retries on that forward
    activity and double-charge the card — the exact silent double-apply the
    at-most-once default exists to prevent. This function therefore reads the
    two FORWARD-side facts (`idempotency_key`, `idempotent`) directly and never
    touches `register`.

    Returns `"keyed"` (dedup-safe by construction), `"declared"` (the author's
    unverified claim) or `None` (no claim at all, at-most-once).
    """
    if call.get("kind") == "fn":                      # an emission-extern crossing
        ext = index.externs.get(call.get("name")) or {}
        if ext.get("idempotency_key") is not None:
            return "keyed"
        return "declared" if ext.get("idempotent") else None
    spec = index.method_spec(call, comp)
    # A service-interface method carries only the BARE `idempotent` modifier —
    # `parser.py` still has `TODO(309-slice1): accept the idempotent(key: p)
    # keyed form` for a method — so the strongest register reachable through a
    # `key.method(...)` crossing today is `declared`.
    return "declared" if spec.get("idempotent") else None


def _retry_class(call: dict, comp: dict, index: "_Index") -> str:
    """The retry class of the activity rendering the crossing `call`.

    The whole of Slice 2's safety argument is in the three refusals below, so
    they are spelled out rather than compressed into a table lookup:

    * **`validated` (item 257) pins to at-most-once, register or not.** A
      `validated` crossing's `retry N` is NOT item 44's idempotent-delivery
      retry — `lower.py` says so at the declaration: "a completion is a read
      with a cost, not an idempotent write". It re-issues a completion THUNK
      revl-side. Handing it to Temporal as a `RetryPolicy` would let the
      platform re-bill a model call as if it were idempotent, so a `validated`
      method keeps `maximumAttempts: 1` even when it also carries a key, and
      its 257 retry stays workflow-side (design §3, last paragraph).

    * **`declared` does not earn a retry.** It is the author's claim over an
      opaque host body, machine-checked for shape only. revl's own forward
      re-issue seam already refuses to act on it unhelped:
      `recovery._reissue_permitted(register="declared", strength=None)` is
      False, and only an operator writing `recovery may re-issue owed emissions
      (strength: declared)` admits it. A Temporal `RetryPolicy` is baked into
      the emitted workflow with no operator knob at the run, so there is nowhere
      for that acceptance to be expressed — and Temporal WILL retry on every
      transient failure, which turns one false claim into a production
      double-apply. At-most-once.

    * **No register at all is never promoted.** The fail-closed direction is the
      fall-through, exactly as `recovery._replay_tier` has it.
    """
    if index.is_validated(call, comp):
        return _AT_MOST_ONCE
    register = _forward_register(call, comp, index)
    return _KEYED if register in _RETRY_EARNING_REGISTERS else _AT_MOST_ONCE


# ------------------------------------------------------------ activity registry

class _Index:
    """The name lookups the crossing walk needs: services, externs, and the
    requirement bindings of the component currently being rendered."""

    __slots__ = ("services", "externs")

    def __init__(self, ir: dict) -> None:
        self.services: dict = ir.get("services") or {}
        self.externs: dict = {ext.get("name"): ext
                              for ext in (ir.get("externs") or [])}

    def method_spec(self, call: dict, comp: dict) -> dict:
        """The service-method spec a `key.method(...)` crossing resolves to."""
        key = (call.get("target") or {}).get("name")
        svc_name = (comp.get("requires") or {}).get(key)
        return (((self.services.get(svc_name) or {}).get("methods") or {})
                .get(call.get("method")) or {})

    def is_emission_extern(self, name: object) -> bool:
        return (self.externs.get(name) or {}).get("class") == "emission"

    def is_validated(self, call: dict, comp: dict) -> bool:
        """item 257: does this crossing reach a `validated` declaration?"""
        if call.get("kind") == "fn":
            return bool((self.externs.get(call.get("name")) or {}).get("validated"))
        return bool(self.method_spec(call, comp).get("validated"))


class _Activity:
    __slots__ = ("name", "params", "returns", "retry")

    def __init__(self, name: str, params: str, returns: str,
                 retry: str = _AT_MOST_ONCE) -> None:
        self.name = name
        self.params = params
        self.returns = returns
        self.retry = retry


def _crossing_signature(call: dict, comp: dict, index: "_Index",
                        known_types: frozenset) -> _Activity:
    """The activity signature for a crossing, read off the declaration it
    resolves to — the service method behind the requirement key, or the emission
    extern it names — so the emitted `activities.ts` interface is typed, and
    carrying the evidence-derived retry class the crossing earned (§3)."""
    if call.get("kind") == "fn":
        decl = index.externs.get(call.get("name")) or {}
    else:
        decl = index.method_spec(call, comp)
    params = ", ".join(
        f"{_ident(p.get('name'), 'parameter')}: {_ts_type(p.get('type'), known_types)}"
        for p in decl.get("params") or [])
    returns = (_ts_type(decl["returns"], known_types)
               if decl.get("returns") else "void")
    return _Activity(_activity_name(call), params, f"Promise<{returns}>",
                     _retry_class(call, comp, index))


# ------------------------------------------------------------ component render

def _render_component(comp: dict, ir: dict, ctx: "_Ctx", index: "_Index",
                      activities: dict) -> list[str]:
    name = _ident(comp.get("name"), "component")
    scope = _Scope(comp)
    for local in (comp.get("requires") or {}):
        _ident(local, "requirement")
    known_types = frozenset(ir.get("types") or {})
    config_fields = comp.get("config") or []

    def register(call: dict) -> None:
        act = _crossing_signature(call, comp, index, known_types)
        seen = activities.get(act.name)
        if seen is None:
            activities[act.name] = act
            return
        # One activity NAME, two crossings. The signature is a function of the
        # declaration so it cannot disagree, but the retry class is a function
        # of the CALL, so take the weaker of the two: a name reached even once
        # without retry-earning evidence stays at-most-once. Fail-closed, and
        # the direction that can only remove retries, never add them.
        if seen.retry != act.retry:
            seen.retry = _AT_MOST_ONCE

    def register_reachable(node: Any) -> None:
        """Register every emission-extern crossing reachable in `node`.

        An `effect`/`undo` slot reaches its crossing through a plain `fn` node
        rather than an `emit` step, and `_t_expr` renders that node as a bare
        call to the extern's own name — which is the activity name. Registering
        it here is what makes that call resolve to a proxied activity instead of
        an undefined symbol."""
        if isinstance(node, list):
            for item in node:
                register_reachable(item)
            return
        if not isinstance(node, dict):
            return
        if node.get("kind") == "fn" and index.is_emission_extern(node.get("name")):
            register(node)
        for value in node.values():
            if isinstance(value, (dict, list)):
                register_reachable(value)

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
                        f"run: () => {_activity_call(comp_call, scope)}, "
                        f"args: {_redaction_args(comp_call.get('args') or [], scope)} }})  "
                        f"// write-ahead: registered before the forward await")
                body.append(f"{indent}await {_activity_call(fwd, scope)}")
            elif kind in ("effect", "let-effect"):
                acquire = step["acquire"]
                register_reachable(acquire)
                register_reachable(step.get("undo"))
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
                        f"{{ {_t_expr(step['undo'], scope)} }} }}, "
                        f"args: {_redaction_args(step['undo'], scope)} }})  "
                        f"// write-ahead: no-op if the acquire never landed")
                    body.append(f"{indent}{bind} = await {_t_expr(acquire, scope)}")
                elif step.get("undo") is not None:
                    body.append(
                        f"{indent}saga.push({{ name: {_string('undo')}, "
                        f"run: async () => {{ {_t_expr(step['undo'], scope)} }}, "
                        f"args: {_redaction_args(step['undo'], scope)} }})  "
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
                 "name: step.name, error: redactResidueError(e, step.args()) }) }")
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


# ------------------------------------------------------------ retry rendering

def _render_retry_policies(activities: dict) -> list[str]:
    """The `proxyActivities` groups, one per retry class the walk derived.

    Temporal's TS SDK carries `RetryPolicy` in the PROXY options, not per call,
    so a per-activity policy is spelled as one `proxyActivities` call per class
    with the names of that class destructured out of it. Every activity appears
    in exactly one group.

    The at-most-once group is always emitted, even when empty of user crossings,
    because `recordResidue` lives in it. The retryable group is emitted ONLY
    when some crossing earned it, so a document with no idempotency evidence
    renders the same single at-most-once proxy Slice 1 did."""
    groups: dict = {}
    for act in activities.values():
        groups.setdefault(act.retry, []).append(act.name)
    # Totality. A class the renderer does not know would leave its activities
    # undestructured — an undefined symbol in the emitted workflow, and an
    # activity silently NOT dispatched. Fail at emit instead.
    unknown = set(groups) - {_AT_MOST_ONCE, _KEYED}
    if unknown:  # pragma: no cover — a derivation/renderer disagreement
        raise EmitError(
            f"no proxy group renders the retry class(es) {sorted(unknown)}; "
            f"the derivation and the renderer disagree")

    lines = [
        "// item 253 §3: RETRY POLICIES ARE DERIVED, NEVER AUTHORED. Every",
        "// activity below sits in the class the item-309/440 idempotency ledger",
        "// put it in, and `at-most-once` is the fall-through: a crossing whose",
        "// evidence does not PROVE re-delivery is safe keeps revl's own",
        "// at-most-once semantics, because a Temporal retry fires on every",
        "// transient failure and would turn an unproven claim into a",
        "// double-apply. Raising one of these by hand defeats the derivation.",
        "const AT_MOST_ONCE = { maximumAttempts: 1 }",
    ]
    if _KEYED in groups:
        lines += [
            "// A crossing whose declaration carries `idempotent(key: <param>)`:",
            "// dedup-safe BY CONSTRUCTION (item 309's `keyed` register, the",
            "// forward half of item 440's REDISPATCH_FREE set), so a redelivery",
            "// is the remote's dedup, not a second effect. BOUNDED on purpose —",
            "// an unbounded forward retry never reaches the catch, so the",
            "// compensation phase would never run and the saga could not abort.",
            f"const DEDUP_SAFE_RETRY = {{ initialInterval: {_string(_RETRY_INITIAL_INTERVAL)}, "
            f"backoffCoefficient: {_RETRY_BACKOFF_COEFFICIENT}, "
            f"maximumInterval: {_string(_RETRY_MAXIMUM_INTERVAL)}, "
            f"maximumAttempts: {_RETRY_MAXIMUM_ATTEMPTS} }}",
        ]
    for retry_class, policy in ((_AT_MOST_ONCE, "AT_MOST_ONCE"),
                                (_KEYED, "DEDUP_SAFE_RETRY")):
        names = groups.get(retry_class)
        if not names:
            continue
        lines += [
            f"const {{ {', '.join(sorted(names))} }} = "
            f"proxyActivities<typeof activities>({{",
            f"  startToCloseTimeout: {_string(_START_TO_CLOSE_TIMEOUT)},  "
            f"// revl perCallMs — per-call, Temporal-honoured (HIGH 1)",
            f"  retry: {policy},",
            "})",
        ]
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

    services = ir.get("services") or {}
    index = _Index(ir)

    # Refusal precedes emission (the closed allowlist, CRITICAL 2).
    _refuse_outside_allowlist(ir, index)

    doc_ctx = _Ctx(ir.get("types") or {}, ir.get("functions") or [],
                   ir.get("externs") or [], services=services)

    activities: dict = {}
    rendered: list[str] = []
    seen: set = set()
    for comp in components:
        if comp.get("name") in seen:
            raise EmitError(f"duplicate component name: {comp.get('name')!r}")
        seen.add(comp.get("name"))
        rendered.extend(_render_component(comp, ir, doc_ctx, index, activities))
        rendered.append("")

    # `recordResidue` is the durable residue sink activity every workflow drains
    # into on abort (attack 7). It is revl-EMITTED but host-IMPLEMENTED, and no
    # declaration carries evidence about it, so it takes the fail-closed default
    # like any other crossing with no register: at-most-once.
    activities.setdefault(
        "recordResidue",
        _Activity("recordResidue", "report: SagaReport", "Promise<void>"))

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
    ]
    out.extend(_render_retry_policies(activities))
    out += [
        "",
        "// revl budgetMs (runtime.ts:880): a SINGLE Phase-2 total budget, checked",
        "// workflow-side BETWEEN compensations (HIGH 1). Never a per-activity timeout.",
        f"const COMPENSATION_BUDGET_MS = {_COMPENSATION_BUDGET_MS}",
        "",
        "type SagaStep = { name: string; run: () => Promise<unknown>; "
        "args: () => unknown[] }",
        "type Residue = Record<string, unknown>",
        "type SagaReport = { outstanding: Residue[]; worldRemaining: number; proof: string }",
        "",
        "// A compensation activity's error text is HOST TEXT this workflow did not",
        "// write, and it crosses into ApplicationFailure.details, which PERSISTS IN",
        "// TEMPORAL HISTORY for the namespace retention period, plus the residue",
        "// record and the live residue query (item 421 F7). `Secret[Str]` erases to",
        "// plain `string` in RevlActivities, so a compensation implementer gets no",
        "// type-level warning before a confidential value ends up embedded in a",
        "// thrown Error's message (e.g. `throw new Error('close failed for '+h)`).",
        "// Mirror of backends/typescript/bridge.ts's seamFailure/REDACTED_ARG (item",
        "// 421 F5): the values this compensation call was made with (SagaStep.args,",
        "// evaluated lazily so a write-ahead referent is read at failure time, not",
        "// frozen at its pre-acquire undefined) are scrubbed out of the error text",
        "// before it is kept anywhere, so the exception TYPE and the sentence around",
        "// it survive but the caller's own bytes do not.",
        "const REDACTED_ARG = '<redacted:arg>'",
        "const MIN_MATCHABLE_ARG = 3",
        "function argNeedles(value: unknown, into: Set<string>): void {",
        "  if (value === null || value === undefined || typeof value === 'boolean') return",
        "  if (typeof value === 'string') {",
        "    if (value.length >= MIN_MATCHABLE_ARG) into.add(value)",
        "    return",
        "  }",
        "  if (typeof value === 'number' || typeof value === 'bigint') {",
        "    const form = String(value)",
        "    if (form.length >= MIN_MATCHABLE_ARG) into.add(form)",
        "    return",
        "  }",
        "  if (Array.isArray(value)) {",
        "    for (const item of value) argNeedles(item, into)",
        "    return",
        "  }",
        "  if (typeof value === 'object') {",
        "    for (const item of Object.values(value as Record<string, unknown>)) "
        "argNeedles(item, into)",
        "  }",
        "}",
        "function redactResidueError(error: unknown, args: unknown[]): string {",
        "  let text = String(error)",
        "  const needles = new Set<string>()",
        "  argNeedles(args ?? [], needles)",
        "  for (const needle of [...needles].sort((a, b) => b.length - a.length)) {",
        "    if (needle && text.includes(needle)) text = text.split(needle).join(REDACTED_ARG)",
        "  }",
        "  return text",
        "}",
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
