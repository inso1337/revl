"""Resource-typedness: the one shared definition of which types are resource
handles (roadmap item 308).

A *resource type* is the value returned by an `extern acquire` (a `Sock`, a
`Pool`: a handle whose lifetime is tied to the acquiring activation, so it
crosses a seam by proxy, not by copy), closed transitively over any
record/variant that carries one. R0 (below, enforced at extern admission in
`lower.py`) guarantees an acquire return is a NOMINAL OPAQUE HANDLE type, never
a primitive (`Int`) or a structural carrier (`Result[..]`, `List[..]`, a
function type, a record literal); without that guarantee, promoting the base
set into the frontend would make every `Int` a handle and the language
unwritable (design doc 308, R0).

This module is the SINGLE implementation both the seam analysis
(`distribute.py` / `placement.py`) and the frontend ownership checks
(`lower.py`: O1 no-hand-close, B1 borrow-does-not-escape) read, so the two
cannot drift apart. `distribute.py` re-exports the ir-dict wrappers under their
original names for backward compatibility.
"""

from __future__ import annotations

import re

from .typecheck import FN_HEAD, parse_type

# The primitive/builtin scalar type names. An acquire return spelled as one of
# these is refused by R0: a primitive carries no identity, and it would poison
# the taint base (every `Int` becoming a handle).
PRIMITIVE_TYPE_NAMES = frozenset({
    "Int", "Int32", "Float", "F64", "Num", "Str", "Bool", "Bytes",
    "Unit", "Any", "Never", "Value",
})

# The builtin generic carriers. A bare or applied one is structural, never an
# opaque handle: copying it copies whatever it wraps, not a live handle.
_STRUCTURAL_HEADS = frozenset({"Opt", "List", "Map", "Result"})

# item 442 (issue #121): the head of a typed delegated reference `Delegate[S]`
# — a bounded, revocable view of an authority the caller holds, handed to a
# callee for one call (docs/design/442-typed-delegation.md). It is NOT an
# acquire-return handle, so it is deliberately absent from the acquire-derived
# taint base (`resource_base`); the frontend borrow walk adds it to its own
# taint set (`lower._resource_ctx`) so a delegated reference is confined by
# item 308's B1 exactly as a borrowed handle is (design §3.3 D2, §4.3 C3/C4).
# Kept here as the single spelling both the type-wellformedness rule and the
# frontend confinement read, so the two cannot drift.
DELEGATE_HEAD = "Delegate"


# An acquire that yields NO handle value: a lock-style acquire
# (`lockOp() -> Unit undo unlockOp()`) whose teardown takes no handle argument.
# It carries no resource identity, so R0 permits it and the taint base excludes
# it (a `Unit` in the base would poison every `Unit`, exactly the R0 hazard).
NO_HANDLE_RETURNS = frozenset({"Unit"})


def acquire_return_is_nominal_handle(returns: str | None) -> bool:
    """R0 predicate: is `returns` a nominal opaque handle type?

    True only for a bare nominal name (an uppercase head with no type
    arguments that is not a builtin primitive or generic carrier). A primitive
    (`Int`/`Str`/...), a generic application (`Result[W, E]`, `Opt[T]`,
    `List[T]`, a user generic `Conn[T]`), a function type, or a structural
    record literal all return False.
    """
    if not returns:
        return False
    head, args = parse_type(returns)
    if head is None or head == FN_HEAD:
        return False
    if args:
        return False
    if head in PRIMITIVE_TYPE_NAMES or head in _STRUCTURAL_HEADS:
        return False
    if not head[:1].isupper():
        return False
    if head.startswith("{") or "->" in head:
        return False
    return True


def resource_base(externs: list | None) -> set[str]:
    """The acquire-return handle types: the taint base. Post-R0 these are all
    nominal opaque handles, so promoting this set into the frontend cannot
    poison a primitive."""
    return {
        ext["returns"]
        for ext in externs or []
        if ext.get("class") == "acquire"
        and ext.get("returns")
        and ext["returns"] not in NO_HANDLE_RETURNS
    }


# Back-compat alias for the seam analysis's original private name.
resource_types = resource_base


def resource_in(type_str: str | None, resources: set[str]) -> str | None:
    """The first resource type named anywhere in `type_str` (so it fires for a
    nested `Opt[Sock]` / `List[Sock]` / `Conn[Sock]`), or None."""
    if not type_str:
        return None
    # sorted, not set order: the answer is reported in diagnostics and in the
    # F10 audit surface, so a type naming two resources must not name a
    # different one run to run.
    for resource in sorted(resources):
        if re.search(rf"\b{re.escape(resource)}\b", type_str):
            return resource
    return None


def resource_taint(externs: list | None, types: dict | None) -> set[str]:
    """The transitive closure of resource-typedness over the type table (item
    363 F1 hardening, promoted to the frontend for item 308).

    The base is the `extern acquire` return handles (`resource_base`). A
    record/variant type any of whose fields or case payloads mentions an
    already-tainted type is itself resource-typed, recursively to a fixpoint:
    a handle nested in a user record (`type Session = { conn: Sock }`) makes
    `Session` resource-typed too."""
    tainted = set(resource_base(externs))
    types = types or {}
    changed = True
    while changed:
        changed = False
        for name, spec in types.items():
            if not isinstance(spec, dict) or name in tainted:
                continue
            if spec.get("kind") == "record":
                member_types = list((spec.get("fields") or {}).values())
            elif spec.get("kind") == "variant":
                member_types = [c.get("payload") for c in spec.get("cases") or []]
            else:
                member_types = []
            if any(resource_in(t, tainted) for t in member_types):
                tainted.add(name)
                changed = True
    return tainted


def _callee_name(expr) -> str | None:
    """The bare callee name of a lowered call/fn expression, or None. Handles
    both the extern-undo shape (`{"callee": {"name": ..}}`) and the
    component-body call shape (`{"kind": "fn", "name": ..}` /
    `{"kind": "call", "callee": {"kind": "var", "name": ..}}`)."""
    if not isinstance(expr, dict):
        return None
    if expr.get("kind") == "fn" and isinstance(expr.get("name"), str):
        return expr["name"]
    callee = expr.get("callee")
    if isinstance(callee, dict) and isinstance(callee.get("name"), str):
        return callee["name"]
    return None


def closing_ops(externs: list | None) -> set[str]:
    """O1: the declared inverse callees. The `undo` clause of every `acquire`
    (and `witnessed`) extern names the operation that closes the handle; a
    hand-call to any of these on a resource-typed argument is a double-close,
    refused everywhere except the acquiring binding's own `undo`."""
    ops: set[str] = set()
    for ext in externs or []:
        if ext.get("class") in ("acquire", "witnessed"):
            name = _callee_name(ext.get("undo"))
            if name:
                ops.add(name)
    return ops


# ---------------------------------------------------------------------------
# F10: the retaining-extern audit (item 308, design §B1 "The honest limitation:
# retaining externs").
# ---------------------------------------------------------------------------
#
# B1's flow walk sees revl positions only. An `emission fn register(c: Conn)`
# that keeps the handle host-side, or a service whose implementation lives
# across a bridge, escapes through a surface no clause can refuse: the
# declaration does not say "retains", and the type system cannot know host
# semantics. That is the same declaration-is-the-proof-surface line O1 draws
# for an out-of-band close, and the design's named mitigation is REPORT-ONLY.
#
# So this is deliberately not a check. It enumerates the FRONTIER — every
# declared position at which a resource handle leaves revl's sight — so a human
# reviewing a composition can see the whole retention surface at once instead
# of rediscovering it one extern at a time.
#
# It is declaration-derived and whole-program: a row exists because a signature
# names a resource-carrying type in a parameter, not because some body was
# walked. That makes the surface stable (it moves only when a declaration
# moves), enumerable, and diffable — the properties `revl audit --diff` needs.
#
# WHAT IS EXCLUDED, and why. A declared inverse (`closing_ops`) is the SANCTIONED
# close: teardown calls it with the handle exactly once, so its resource-typed
# parameter is the contract working, not a retention hazard. Everything else
# that can be handed a handle is listed, including an `acquire` extern that
# takes one (a pool checkout) — that IS a retention question, just a defensible
# one.
#
# WHAT THIS FACT IS NOT. Every row is a MAY-retain. Nothing here proves a host
# body keeps anything, and nothing here proves one does not; a reader who wants
# "does not retain" needs a declaration surface revl has not got (see the
# design's deferred list). Consumers must not read an absent row as a
# non-retention proof — absence only means no resource-typed parameter is
# DECLARED, which is exactly what the checker already assumes everywhere.


def retention_surface(externs: list | None, types: dict | None,
                      services: dict | None = None) -> list:
    """F10: every declared position where a resource handle leaves revl's sight.

    One row per resource-carrying parameter of a non-inverse extern or of a
    service method (a service reached across a bridge is implemented host-side,
    so its parameters are the same frontier). Sorted, so the surface is stable
    across runs and diffable; `[]` for a composition that declares no resource
    type at all, which keeps the audit document byte-identical for it.
    """
    taint = resource_taint(externs, types)
    if not taint:
        return []
    inverses = closing_ops(externs)
    rows: list = []
    for ext in externs or []:
        if ext.get("name") in inverses:
            continue
        for index, param in enumerate(ext.get("params") or []):
            carried = resource_in(param.get("type"), taint)
            if carried is None:
                continue
            rows.append({
                "kind": "extern", "callee": ext.get("name"),
                "class": ext.get("class"), "param": param.get("name"),
                "index": index, "type": param.get("type"),
                "resource": carried,
            })
    for service, spec in sorted((services or {}).items()):
        for method, decl in sorted((spec.get("methods") or {}).items()):
            for index, param in enumerate(decl.get("params") or []):
                carried = resource_in(param.get("type"), taint)
                if carried is None:
                    continue
                rows.append({
                    "kind": "service-method", "callee": f"{service}.{method}",
                    "class": "emission" if decl.get("emission") else "plain",
                    "param": param.get("name"), "index": index,
                    "type": param.get("type"), "resource": carried,
                })
    rows.sort(key=lambda row: (row["kind"], row["callee"] or "", row["index"]))
    return rows
