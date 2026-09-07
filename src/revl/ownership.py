"""Unique ownership of accumulation locals — the frontend fact six tiers share.

WHY THIS IS IN THE FRONTEND (roadmap item 445). `out = out.push(v)` is a
PERSISTENT write: it copies the whole receiver and rebinds the copy, so the
ordinary `var out = []; for (..) { out = out.push(..) }` loop a developer wrote
as O(n) is EMITTED as O(n^2) on every tier whose containers are mutable. Every
tier's fix is the same statement — write in place — and every tier's fix needs
the same PROOF, which is an aliasing question about the source and not about
the target language: *the object this binding names is reachable through no
other name, so no reader can hold the pre-image.*

That proof was written twice before this module existed, independently, in
`backends/go/emit.py` (`_v3_self_rebind_locals`, item 434) and
`backends/python/emit.py` (`_py_inplace_locals`, item 436 F1) — and ts, rust,
java and wasm would each have written it a third through sixth time. Six
derivations of one aliasing rule is six chances at a SILENT value-semantics
bug, because a wrong answer here is a wrong ANSWER and not a crash. So the
proof runs once, here, and rides the IR.

WHAT RIDES THE IR IS A FACT, NOT AN INSTRUCTION. The marker says the binding
uniquely owns its object at that write; it does not say "mutate". Each tier
still decides what to do with it, and the tiers genuinely differ: rust needs
none of it (the borrow checker discharges the same obligation on the code that
already compiles, item 437), a Go or Python `Str` cannot be mutated at all, and
a tier that has not been audited against these rules simply ignores the marker
and keeps the copying form. Ignoring the marker is always correct.

THE MARKERS.

  `assign` step, key `unique`:
      `True`   — the binding owns its object outright at this write; the
                 persistent copy is unobservable and an in-place write is the
                 faithful lowering.
      `"copy"` — the binding owns its object only if the backend materialised a
                 private copy at the `let` that introduced it (see below). A
                 backend that does not implement that birth copy must ignore
                 these writes and emit the persistent form.

  `let` step, key `unique_birth`:
      `"List"` / `"Map"` — this `let` binds ANOTHER name (`var out = m`), and a
                 backend honouring `unique: "copy"` must materialise a private
                 copy of the source here: ONE copy, where the persistent form
                 made one per write. The value names the container shape, which
                 is read off the methods that rebind the local (`push` is
                 List-only) so no receiver type has to be recovered.

Absent on every node that does not qualify, so an IR document for a program
with no in-place accumulation is byte-identical to before.

THE ANALYSIS IS FLOW-SENSITIVE, which is the second half of item 445 and the
reason a shared implementation was worth building rather than a shared copy of
what the two tiers had. Both prior versions asked "does this name EVER escape
in this body", so one escape anywhere refused the local everywhere. That is
what left `stdlib/list.rvl`'s `list_sort` at its emitted cubic: its inner
accumulator `var res = []` is pushed into and then handed out with `out = res`,
and that one escape refused every push — even though the NEXT outer iteration
re-declares `res` from a fresh literal, so the escaped object can never reach a
later write.

Here the question is asked PER WRITE instead, over a forward dataflow with a
fixpoint on every loop back edge:

  LATTICE, per name: `FRESH` (owns its object outright) < `COPY` (owns it given
  the birth copy) < absent (does not own it). Join is the maximum, so a name
  owned on one path into a merge and escaped on another is escaped after it.

  BIRTH kills an escape. `let out = []` / `Map.empty()` / a record literal
  binds a brand-new object: whatever the previous object was reachable through,
  the new one is not. `let out = m` binds another name's object, which is
  `COPY`: owned exactly when the backend copies it here.

  ESCAPE gens. Every occurrence of the name in a slot that could RETAIN the
  object — a call argument, a list or record element, an arrow capture, a `for`
  iterable, a plain alias — drops it out of the lattice from that point on.
  The complement, the slots that read the value and cannot keep it, is
  `_NON_RETAINING` below.

  A SELF-REBIND ALWAYS KILLS THE ESCAPE, whichever form the backend picks, and
  that is what makes the per-write question well-founded. Marked unique, the
  write is in-place on a value nothing else reaches, so the binding still owns
  it. NOT marked, the backend emits the persistent copy, which rebinds the name
  to a brand-new object — so the name owns THAT outright. Either way the state
  after the write is "owned", and the decision never has to be revisited.

  A `return` needs no rule at all. The function ends there, so no write can
  follow it on that path and the state after it is unreachable; flow
  sensitivity subsumes the special case both prior versions had to state.

CONSERVATIVE FALLBACK, EXPLICITLY. Nothing here is required to be precise, only
to be right, and every uncertainty resolves to "not owned", which emits the
copying form the tiers had before. A parameter is never owned (it is the
caller's object). A name is not owned until a birth this function made. Any
statement shape this walker does not know KILLS every name mentioned in its
subtree. Any expression slot not listed in `_NON_RETAINING` is assumed to
retain. Any builtin whose receiver is not in `_NON_RETAINING_METHODS` is
assumed to hand back an alias of it (a Go `slice` header does, so the whitelist
is the narrower of the two tiers' and not the union). A call is assumed to
retain every argument it is given, which is what leaves `list_dedup`'s
`if (!list_contains(out, x))` quadratic — closing that needs a whole-program
"does this fn retain a parameter" summary, filed as item 445 (b).
"""

from __future__ import annotations

from collections import deque
from typing import Any

# The lattice, low to high; a name absent from a state is NOT owned.
_FRESH = 0
_COPY = 1

# What a solely-owned container is born from: an allocation this function made.
_FRESH_KINDS = frozenset({"list", "maplit", "record"})

# The persistent methods with a destructive equivalent, and their arity, keyed
# to the container shape the birth copy would have to materialise. `concat` is
# deliberately absent: it is defined on both Str and List, the receiver type is
# not known at this node, and no tier can mutate a `Str` in place anyway.
_WRITE_METHODS = {"push": ("List", 1), "set": ("Map", 2), "remove": ("Map", 1)}

# Builtins whose RECEIVER is read and not retained. Deliberately a whitelist:
# `xs.slice(a, b)` is a fresh list on the python tier but a slice header over
# the SAME backing array on the go tier, so "any builtin returns a new
# container" is a python-only truth and this analysis has to hold on six tiers.
# This is `backends/go/emit.py`'s `_V3_LINEAR_READ_METHODS`, the narrower of the
# two rules that were merged here, plus the write methods (whose persistent form
# returns a new container and keeps nothing).
_NON_RETAINING_METHODS = frozenset({
    "length", "indexOf", "slice", "concat", "join", "size", "keys",
    "lookup", "has", "get", "contains",
    "push", "set", "remove",
})

# Slots in which a bare `var` is READ but not RETAINED: the value is consumed on
# the spot (a length, an index, a field, a comparison, an interpolation) or
# handed to a persistent builtin. Every OTHER position may leave a second name
# holding the object, so the default for an unlisted kind is "retains".
_NON_RETAINING = {
    "builtin": ("target",),
    "index": ("target", "index"),
    "len": ("target",),
    "field": ("target",),
    "optfield": ("target",),
    "optcall": ("target",),
    "bin": ("left", "right"),
    "un": ("operand",),
    "interp": ("parts",),
    # `{**base, ..}` spreads the base's ENTRIES into a fresh record; the base
    # object itself is not kept, exactly as `(xs + [v])` does not keep `xs`
    "record_update": ("base",),
}

# The statement shapes this walker models. Anything else kills every name in its
# subtree — see `_unknown_step`.
_KNOWN_STEPS = frozenset({
    "let", "assign", "let_pattern", "if", "while", "for",
    "return", "break", "continue",
})


# --------------------------------------------------------------- the shapes

def _write_shape(name: str, value: Any) -> str | None:
    """`"List"`/`"Map"` when `value` is a persistent copy of `name` with one
    entry changed — the `out = out.push(v)` shape whose result rebinds its own
    receiver — else None."""
    if not isinstance(value, dict):
        return None
    if value.get("kind") == "builtin":
        entry = _WRITE_METHODS.get(value.get("method"))
        target = value.get("target")
        if (entry is not None
                and len(value.get("args") or []) == entry[1]
                and isinstance(target, dict)
                and target.get("kind") == "var"
                and target.get("name") == name):
            return entry[0]
        return None
    if value.get("kind") == "record_update":
        base = value.get("base")
        if (isinstance(base, dict) and base.get("kind") == "var"
                and base.get("name") == name and bool(value.get("updates"))):
            return "Map"
        return None
    return None


def _rebind_receiver(value: dict) -> dict:
    """The `var` node a self-rebind reads its own receiver through. It is the
    write itself, not a second reader, so it never gens an escape."""
    return value.get("target") if value.get("kind") == "builtin" else value.get("base")


# ------------------------------------------------------------- the gen walk

def _gen(node: Any, state: dict, retains: bool, summary: dict,
         skip: Any = None) -> None:
    """Drop from `state` every name `node` may leave a second holder of."""
    if isinstance(node, list):
        for item in node:
            _gen(item, state, retains, summary, skip)
        return
    if not isinstance(node, dict) or node is skip:
        return
    kind = node.get("kind")
    if kind == "var":
        if retains:
            state.pop(node.get("name"), None)
        return
    if kind == "arrow":
        # a closure outlives the expression that built it, so everything it can
        # see is retained — its captures, its parameter names (which shadow),
        # and every name its body reads
        for key in ("captures", "params"):
            for name in node.get(key) or []:
                if isinstance(name, str):
                    state.pop(name, None)
        for child in node.values():
            _gen(child, state, True, summary, skip)
        return
    if kind == "call":
        keeps = _arg_retention(node, summary)
        for key, child in node.items():
            if key == "args":
                for arg, retained in zip(child or [], keeps):
                    _gen(arg, state, retained, summary, skip)
            else:
                _gen(child, state, True, summary, skip)
        return
    if kind == "match":
        for arm in node.get("arms") or []:
            bind = arm.get("bind") if isinstance(arm, dict) else None
            if isinstance(bind, str):
                state.pop(bind, None)
    safe: tuple = _NON_RETAINING.get(kind, ())
    if kind == "builtin" and node.get("method") not in _NON_RETAINING_METHODS:
        safe = ()  # an unaudited builtin may hand back an alias of its receiver
    elif kind == "bin" and node.get("op") == "??":
        safe = ()  # `a ?? b` YIELDS `a`, so that operand IS retained
    for key, child in node.items():
        _gen(child, state, key not in safe, summary, skip)


def _kill_subtree(node: Any, state: dict) -> None:
    """The fallback for a statement shape this walker does not model: forget
    every name reachable from it, bound or read. Deliberately blind to the
    retention summary — an unmodelled shape gets no benefit of the doubt."""
    if isinstance(node, list):
        for item in node:
            _kill_subtree(item, state)
        return
    if not isinstance(node, dict):
        return
    for key in ("name", "bind", "rest"):
        if isinstance(node.get(key), str):
            state.pop(node[key], None)
    for name in node.get("names") or []:
        if isinstance(name, str):
            state.pop(name, None)
    _gen(node, state, True, {})
    for child in node.values():
        _kill_subtree(child, state)


# ------------------------------------------------------------- the dataflow

def _join(states: list) -> dict | None:
    """Meet of the reachable predecessors: a name survives only where every one
    of them owns it, at the weaker of their two levels."""
    live = [s for s in states if s is not None]
    if not live:
        return None
    out = dict(live[0])
    for other in live[1:]:
        for name in list(out):
            if name not in other:
                del out[name]
            else:
                out[name] = max(out[name], other[name])
    return out


class _Walk:
    """One function body's forward pass. `marks` is written on EVERY visit of a
    write, never only added to, so the last (fixpoint) iteration's answer is the
    one that survives an optimistic earlier one."""

    def __init__(self, summary: dict) -> None:
        self.summary = summary
        self.marks: dict[int, int] = {}
        self.breaks: list = []
        self.conts: list = []

    # -- statements ---------------------------------------------------------

    def stmts(self, steps: Any, state: dict | None) -> dict | None:
        for node in steps or []:
            if state is None:
                break  # unreachable: nothing after a return/break/continue
            if isinstance(node, dict):
                state = self.stmt(node, state)
        return state

    def stmt(self, node: dict, state: dict) -> dict | None:
        step = node.get("step")
        if step not in _KNOWN_STEPS:
            _kill_subtree(node, state)
            return state
        handler = getattr(self, f"_step_{step}")
        return handler(node, state)

    def _step_let(self, node: dict, state: dict) -> dict:
        name = node.get("name")
        value = node.get("value")
        _gen(value, state, True, self.summary)
        if not isinstance(name, str):
            return state
        kind = value.get("kind") if isinstance(value, dict) else None
        if kind in _FRESH_KINDS:
            state[name] = _FRESH
        elif kind == "var":
            # born off another name: owned exactly if the backend materialises
            # a private copy right here
            state[name] = _COPY
        else:
            state.pop(name, None)
        return state

    def _step_assign(self, node: dict, state: dict) -> dict:
        name = node.get("name")
        value = node.get("value")
        shape = _write_shape(name, value) if isinstance(name, str) else None
        if shape is None:
            _gen(value, state, True, self.summary)
            if isinstance(name, str):
                kind = value.get("kind") if isinstance(value, dict) else None
                # a fresh literal is a birth on an `assign` exactly as on a `let`
                if kind in _FRESH_KINDS:
                    state[name] = _FRESH
                else:
                    state.pop(name, None)
            return state
        # the receiver `var` is the write, not a second reader; every other
        # operand is evaluated BEFORE the write and gens normally
        _gen(value, state, True, self.summary, skip=_rebind_receiver(value))
        level = state.get(name)
        self.marks[id(node)] = level
        # whichever form the backend picks, the name owns its object afterwards:
        # in place on something nothing else reaches, or the persistent copy's
        # brand-new container
        state[name] = _FRESH if level is None else level
        return state

    def _step_let_pattern(self, node: dict, state: dict) -> dict:
        _gen(node.get("value"), state, True, self.summary)
        for bind in list(node.get("names") or []) + [node.get("rest")]:
            if isinstance(bind, str):
                state.pop(bind, None)
        return state

    def _step_if(self, node: dict, state: dict) -> dict | None:
        _gen(node.get("cond"), state, True, self.summary)
        then_out = self.stmts(node.get("then"), dict(state))
        else_out = self.stmts(node.get("else"), dict(state))
        return _join([then_out, else_out])

    def _step_return(self, node: dict, state: dict) -> None:
        return None

    def _step_break(self, node: dict, state: dict) -> None:
        self.breaks.append(state)
        return None

    def _step_continue(self, node: dict, state: dict) -> None:
        self.conts.append(state)
        return None

    def _step_for(self, node: dict, state: dict) -> dict:
        # the iterable is held for the loop's whole extent, so an in-place write
        # to it inside the body would change what the loop is walking
        _gen(node.get("iterable"), state, True, self.summary)
        bind = node.get("bind")
        if isinstance(bind, str):
            state.pop(bind, None)
        return self._loop(node.get("body"), state, None)

    def _step_while(self, node: dict, state: dict) -> dict:
        return self._loop(node.get("body"), state, node.get("cond"))

    def _loop(self, body: Any, entry: dict, cond: Any) -> dict:
        """Fixpoint over the back edge. The lattice is finite (three levels per
        name, and the name set only shrinks), the transfer is monotone, so the
        iteration terminates; the LAST pass is the one whose marks stand."""
        outer_breaks, outer_conts = self.breaks, self.conts
        head = entry
        # the chain is finite: `head`'s name set only shrinks and each surviving
        # name only rises FRESH -> COPY, so 2n+1 steps bound it
        for _ in range(2 * len(entry) + 3):
            self.breaks, self.conts = [], []
            top = dict(head)
            _gen(cond, top, True, self.summary)
            body_out = self.stmts(body, dict(top))
            merged = _join([head, body_out] + self.conts)
            assert merged is not None  # `head` is reachable by construction
            if merged == head:
                break
            head = merged
        else:  # pragma: no cover - the bound is #names + 2, the chain is #names
            head = {}
            self.breaks, self.conts = [], []
            top = dict(head)
            _gen(cond, top, True, self.summary)
            self.stmts(body, dict(top))
        exits = [top] + self.breaks
        self.breaks, self.conts = outer_breaks, outer_conts
        out = _join(exits)
        return out if out is not None else {}


# ------------------------------------------------------------------- stamps

def _apply(body: Any, marks: dict[int, int]) -> None:
    """Write the decided markers onto the IR nodes, and nowhere else."""
    copies: dict[str, str] = {}
    for node, level in _writes(body, marks):
        shape = _write_shape(node.get("name"), node.get("value"))
        if level is None:
            node.pop("unique", None)
        elif level == _FRESH:
            node["unique"] = True
        else:
            node["unique"] = "copy"
            name = node["name"]
            # `push` names a List; `set`/`remove`/a record update name a Map, so
            # the birth copy's container needs no receiver type
            if copies.get(name) != "List":
                copies[name] = shape
    if copies:
        _stamp_births(body, copies)


def _writes(node: Any, marks: dict[int, int]):
    if isinstance(node, list):
        for item in node:
            yield from _writes(item, marks)
    elif isinstance(node, dict):
        if node.get("step") == "assign" and id(node) in marks:
            yield node, marks[id(node)]
        for child in node.values():
            yield from _writes(child, marks)


def _stamp_births(node: Any, copies: dict[str, str]) -> None:
    if isinstance(node, list):
        for item in node:
            _stamp_births(item, copies)
        return
    if not isinstance(node, dict):
        return
    if node.get("step") == "let" and node.get("name") in copies:
        value = node.get("value")
        if isinstance(value, dict) and value.get("kind") == "var":
            node["unique_birth"] = copies[node["name"]]
    for child in node.values():
        _stamp_births(child, copies)


# --------------------------------------------------- the retention summary
# Item 445 (b). An intraprocedural analysis must assume a call KEEPS what it is
# handed, and that assumption is what left `stdlib/list.rvl`'s `list_dedup`
# quadratic: `if (!list_contains(out, x)) { out = out.push(x) }` hands `out` to
# a call one statement before the write, and `list_contains` demonstrably keeps
# nothing — it walks the list and answers a Bool.
#
# So one whole-program question is answered first: DOES THIS FUNCTION RETAIN
# PARAMETER i — can the object the caller passed still be reached through
# anything, once the call has returned? A parameter that reaches only slots the
# object cannot outlive does not. The rules are the same `_NON_RETAINING` slots,
# with two differences that follow from asking about the CALLER's object rather
# than about a local:
#
#   * `return e` RETAINS. A local's return needs no rule (nothing runs after it
#     in that function), but a returned parameter is handed straight back to the
#     caller, which is an alias by any definition.
#   * a `for` ITERABLE does not retain. The iterator dies with the call, so
#     iterating a parameter leaves the caller nothing new — where inside ONE
#     body the same iterable is held for the loop's whole extent and a write to
#     it would change what the loop is walking.
#
# The fixpoint starts optimistic (nothing retains) and only ever ADDS retention,
# so it is a least fixpoint of a may-property and mutual recursion converges to
# the truth rather than under-approximating it: passing a parameter along a
# cycle retains nothing unless some function in that cycle puts it in a slot
# that keeps it, and that slot is what the iteration finds.
#
# EVERYTHING UNRESOLVED RETAINS. A callee that is not a module `fn` of this
# program — an extern, a host root, a service method, a call through a function
# VALUE — has no summary, so every argument to it retains. So does any call
# whose argument count does not match the callee's parameter count (a default
# argument the call site did not expand), and every argument of a callee named
# by anything but a plain `var`.

# Statement slots in which a bare `var` is read and not retained ACROSS THE CALL.
_STEP_NON_RETAINING = {"for": ("iterable",)}


def _arg_retention(call: dict, summary: dict) -> tuple:
    """Per-argument "does the callee keep this", defaulting to yes."""
    args = call.get("args") or []
    callee = call.get("callee")
    if isinstance(callee, dict) and callee.get("kind") == "var":
        keeps = summary.get(callee.get("name"))
        if keeps is not None and len(keeps) == len(args):
            return keeps
    return (True,) * len(args)


def _is_stmt_list(value: Any) -> bool:
    return (isinstance(value, list) and bool(value)
            and all(isinstance(item, dict) and "step" in item for item in value))


def _summary_walk(node: Any, state: dict, summary: dict) -> None:
    """Drop from `state` every parameter this body may hand to something that
    outlives the call."""
    if isinstance(node, list):
        for item in node:
            _summary_walk(item, state, summary)
        return
    if not isinstance(node, dict):
        return
    if "step" not in node:
        _gen(node, state, True, summary)
        return
    safe = _STEP_NON_RETAINING.get(node["step"], ())
    for key, child in node.items():
        if _is_stmt_list(child):
            _summary_walk(child, state, summary)
        elif key not in safe:
            _gen(child, state, True, summary)


def _bound_names(node: Any, found: set) -> set:
    """Every name this body binds: a parameter, a `let`, a `for` bind, a
    destructure, an arrow parameter, a match-arm payload.

    A local MAY shadow a module `fn` (`let helper = g` over a `fn helper`), and
    the call node is spelled identically either way, so a callee name this body
    binds is not the function the summary describes and gets no summary at all.
    """
    if isinstance(node, list):
        for item in node:
            _bound_names(item, found)
        return found
    if not isinstance(node, dict):
        return found
    if "step" in node:
        # a statement BINDS its `name`/`bind`; an expression's `name` is a READ
        for key in ("name", "bind", "rest"):
            if isinstance(node.get(key), str):
                found.add(node[key])
        for name in node.get("names") or []:
            if isinstance(name, str):
                found.add(name)
    elif node.get("kind") == "arrow":
        for name in list(node.get("params") or []) + list(node.get("captures") or []):
            if isinstance(name, str):
                found.add(name)
    elif node.get("kind") == "match":
        for arm in node.get("arms") or []:
            bind = arm.get("bind") if isinstance(arm, dict) else None
            if isinstance(bind, str):
                found.add(bind)
    for child in node.values():
        _bound_names(child, found)
    return found


def _visible(summary: dict, fn_like: dict) -> dict:
    """`summary` with every name this body binds removed."""
    shadowed = _bound_names(fn_like.get("body"), set())
    shadowed.update(p.get("name") for p in fn_like.get("params") or [])
    return {name: keeps for name, keeps in summary.items() if name not in shadowed}


def _callee_names(node: Any, found: set) -> set:
    """Every name this body calls through a plain `var` callee — exactly the
    names `_summary_walk` may look up in a summary (`_arg_retention` reads no
    other). Collected once so a recompute needs only these entries, not the
    whole summary."""
    if isinstance(node, list):
        for item in node:
            _callee_names(item, found)
        return found
    if not isinstance(node, dict):
        return found
    if node.get("kind") == "call":
        callee = node.get("callee")
        if isinstance(callee, dict) and callee.get("kind") == "var":
            name = callee.get("name")
            if isinstance(name, str):
                found.add(name)
    for child in node.values():
        _callee_names(child, found)
    return found


def retention_summary(functions: Any) -> dict:
    """`{fn name: (retains param 0, retains param 1, ...)}` over one program.

    A monotone least fixpoint over a finite lattice (each entry only rises
    False -> True), so the answer is independent of evaluation order. Rather
    than recompute every body every round and rebuild the whole visible summary
    each time — O(F^2) per round — this drives a WORKLIST: a body is recomputed
    only when a callee's retention actually changed, and each recompute reads a
    SHADOW-VIEW patched onto a copy of just that body's callee summaries. The
    least fixpoint reached is byte-identical to the round-based one.
    """
    bodies: dict = {}
    for fn in functions or []:
        if not isinstance(fn, dict):
            continue
        name = fn.get("name")
        params = [p.get("name") for p in fn.get("params") or []]
        if isinstance(name, str) and name not in bodies and all(
                isinstance(p, str) for p in params):
            bodies[name] = (fn.get("body"), params)
    summary = {name: (False,) * len(params) for name, (_, params) in bodies.items()}
    # The callee names each body may look up, minus anything it binds (a local
    # that shadows a module `fn` is not that `fn`, exactly as `visible` dropped
    # the shadowed keys). Intersected with the module fns, these are the summary
    # entries a recompute of this body actually depends on.
    fn_names = set(bodies)
    depends: dict = {}
    callers: dict = {name: set() for name in bodies}
    for name, (body, params) in bodies.items():
        shadowed = _bound_names(body, {p for p in params})
        deps = (_callee_names(body, set()) & fn_names) - shadowed
        depends[name] = deps
        for dep in deps:
            callers[dep].add(name)
    # Every body starts on the worklist; a body re-enters only when a callee it
    # depends on gains retention. Finitely many False -> True flips, so it drains.
    worklist = deque(bodies)
    queued = set(bodies)
    while worklist:
        name = worklist.popleft()
        queued.discard(name)
        body, params = bodies[name]
        # the shadow-view: a copy holding just this body's dependencies' current
        # retention. Every other lookup misses and defaults to "retains", which
        # is what the full visible summary did for a non-fn or shadowed callee.
        visible = {dep: summary[dep] for dep in depends[name]}
        state = {param: _FRESH for param in params}
        _summary_walk(body, state, visible)
        keeps = tuple(
            held or param not in state
            for held, param in zip(summary[name], params))
        if keeps != summary[name]:
            summary[name] = keeps
            for caller in callers[name]:
                if caller not in queued:
                    queued.add(caller)
                    worklist.append(caller)
    return summary


# -------------------------------------------------------------------- entry

def annotate(fn_like: dict, summary: dict | None = None) -> None:
    """Stamp `unique` / `unique_birth` on one function, test or method body.

    A parameter is never owned: it is the CALLER's object, and writing through
    it would destructively update a binding this function does not own. Absent a
    `summary`, every call retains every argument it is given.
    """
    body = fn_like.get("body")
    if not body:
        return
    walk = _Walk(_visible(summary or {}, fn_like))
    state: dict = {}
    walk.stmts(body, state)
    _apply(body, walk.marks)


def annotate_ir(ir: dict) -> None:
    """Stamp every function-shaped body in a lowered IR document."""
    summary = retention_summary(ir.get("functions"))
    for key in ("functions", "tests", "fault_tests", "prop_tests"):
        for entry in ir.get(key) or []:
            if isinstance(entry, dict):
                annotate(entry, summary)
