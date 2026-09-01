"""Frontend support for the session commit protocol (roadmap item 245).

Slice 1 is the py foundation: the `deferred` modifier, the class-(b) checker
obligations, the class tag on the G8 crossing surface, and the py runtime's
session owner / deferral queue / commit verbs. This module carries the one piece
of Slice 1 the FRONTEND owns that the five ownerless tiers will consume in
Slice 2 — the tier gate's reachability check and its single canonical
diagnostic.

Decision 2's tier gate (docs/design/245-session-commit.md): class (b)'s lowering
needs a session owner at runtime (the deferral queue, the escrow, a commit verb
to flush or drop it). Only the py tier has one today. Until a tier grows its own
owner, its emitter must REFUSE any CALL to a `deferred` extern at emit time,
because both available degradations lie: firing at call time crosses a boundary
the program was typed to withhold, and enqueueing with no owner drops the action
on the floor (no verdict ever comes). The refusal keys off the CALL SITE — a
declared-but-never-called deferred extern does not poison the build.

    Slice 2 (landed): :func:`refuse_deferred_on_ownerless_tier` is wired into each
    of the five ownerless backends' emit refusal channels
    (backends/{rust,go,java,wasm,typescript}/emit.py, each via a thin
    `_refuse_deferred_emissions` wrapper that calls this guard from `emit()` and
    re-raises the canonical diagnostic through that tier's existing `EmitError`).
    All six backends share this one wording instead of inventing six. A `deferred`
    extern remains a py-only construct at runtime; the py driver is the only
    session owner, so a CALL to one on any other tier is refused at emit time.
"""

from __future__ import annotations

from .errors import RevlError

#: The tiers with no session owner runtime today (Decision 2's tier gate). The py
#: tier is deliberately absent — it has the `revl run` / MCP driver as owner.
OWNERLESS_TIERS = ("rust", "go", "java", "wasm", "typescript")


def deferred_extern_names(ir: dict) -> set:
    """The names of `deferred` emission externs declared in ``ir``."""
    return {ext["name"] for ext in ir.get("externs") or []
            if ext.get("class") == "emission" and ext.get("deferred")}


def _reached_deferred_calls(ir: dict, deferred: set) -> list:
    """Every CALL SITE that emits a deferred extern, as ``(component, name)``.

    Walks the component bodies and provide-method bodies for an `emit` step whose
    expression calls a deferred extern by name — the exact shape the py emitter's
    enqueue lowering keys off. Reachability, not declaration: a declared but
    never-called deferred extern is not flagged."""
    reached: list = []
    seen: set = set()

    def _walk(node, component: str) -> None:
        if isinstance(node, dict):
            if node.get("step") == "emit":
                expr = node.get("expr") or {}
                if expr.get("kind") == "fn" and expr.get("name") in deferred:
                    mark = (component, expr["name"])
                    if mark not in seen:
                        seen.add(mark)
                        reached.append(mark)
            for value in node.values():
                _walk(value, component)
        elif isinstance(node, list):
            for item in node:
                _walk(item, component)

    for comp in ir.get("components") or []:
        _walk(comp, comp.get("name") or "?")
    return reached


def _diagnostic(component: str, name: str, tier: str) -> str:
    """The one wording for all five tiers (Decision 2), so six backends do not
    invent six messages."""
    return (
        f"{component}: `deferred` emission `{name}` needs a session owner "
        f"runtime (the deferral queue and the commit verb), which the {tier} "
        f"tier does not have yet; deferred emissions run on the python tier "
        f"only. Refusing rather than degrading: firing at call time would break "
        f"the declaration's promise that nothing crosses before the session "
        f"commit. Either target the python tier, or drop `deferred` from the "
        f"extern to make it an immediate emission (class (c): fires mid-session, "
        f"prompted per 246).")


def refuse_deferred_on_ownerless_tier(ir: dict, tier: str,
                                      filename: str = "<emit>") -> None:
    """Refuse a `deferred` emission CALL on one of the five ownerless tiers
    (Decision 2's tier gate). A no-op on the py tier and for any IR that never
    calls a deferred extern. Raises :class:`RevlError` on the first reached
    deferred call, with the single canonical diagnostic.

    Slice 2 wires this into each ownerless backend's emit refusal channel; Slice
    1 ships it tested and ready (see the module TODO)."""
    if tier == "python" or tier == "py":
        return
    deferred = deferred_extern_names(ir)
    if not deferred:
        return
    for component, name in _reached_deferred_calls(ir, deferred):
        raise RevlError(filename, 0, _diagnostic(component, name, tier),
                        code="G8", category="deferred")


def _reached_approval_crossings(ir: dict) -> list:
    """Every `emit … with a` crossing, as ``(component,)`` markers — an emit step
    carrying an `approval` edge (item 246, Slice 3). The exact shape the py
    emitter's `approval_crossing` lowering keys off."""
    reached: list = []
    seen: set = set()

    def _walk(node, component: str) -> None:
        if isinstance(node, dict):
            if node.get("step") == "emit" and node.get("approval") is not None:
                if component not in seen:
                    seen.add(component)
                    reached.append(component)
            if node.get("step") == "approval" and component not in seen:
                seen.add(component)
                reached.append(component)
            for value in node.values():
                _walk(value, component)
        elif isinstance(node, list):
            for item in node:
                _walk(item, component)

    for comp in ir.get("components") or []:
        _walk(comp, comp.get("name") or "?")
    return reached


def _approval_diagnostic(component: str, tier: str) -> str:
    """The one wording for all five ownerless tiers (Decision 2's tier gate,
    Slice 3), so six backends do not invent six messages."""
    return (
        f"{component}: a typed approval (`await approval[C]` / `emit … with a`) "
        f"needs a session owner runtime to route the approval request to and to "
        f"record the durable consume-before-fire spend, which the {tier} tier "
        f"does not have yet; typed approvals run on the python tier only. "
        f"Refusing rather than degrading: firing without the durable spend would "
        f"break the single-use, consume-before-fire guarantee (item 246, "
        f"invariant 5). Target the python tier for approval-bound crossings.")


def refuse_approval_on_ownerless_tier(ir: dict, tier: str,
                                      filename: str = "<emit>") -> None:
    """Refuse a typed-approval crossing on one of the five ownerless tiers (item
    246, Slice 3's tier gate — the 245 stance). A no-op on py and for any IR that
    uses no `await approval` / `with` surface. The STATIC obligation (the checker
    in `_lower_provide`, the non-persistence rule) still holds on every tier; only
    the runtime suspension + durable spend is py-only, so it is refused at emit."""
    if tier == "python" or tier == "py":
        return
    for component in _reached_approval_crossings(ir):
        raise RevlError(filename, 0, _approval_diagnostic(component, tier),
                        code="G8", category="approval")
