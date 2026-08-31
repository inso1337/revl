"""Emission cardinality bounds for `revl audit` (docs/design/260-emission-cardinality-bounds.md).

Where `distribute.distributability` says which services may cross a process
seam, `cardinality` says HOW MANY TIMES per activation a component may cross
each capability boundary, read off the IR before anything runs. The verdict is
honest: a crossing whose multiplicity the analysis cannot prove is `unbounded`
(mirroring `distributability`'s named-verdict pattern), never a silent 0 and
never an optimistic bound.

Slice 1 (this module) implements §2.1's exact count for non-looping,
non-recursive bodies plus §2.3's `unbounded` for every loop and every recursion.
The §2.2 bounded-iteration certification is a later slice: here EVERY recursion
and EVERY loop reports `unbounded`, which is sound (it never over-claims).

The soundness tie (§5.1): the count fold reuses the EXACT reach `_boundary`
computes. It mirrors `_boundary`'s `walk_expr` arms line-for-line so the three
crossing shapes are all counted - the `emit` step, the `req`-target emission
call, and the spawn-handle `instance-get` provision-method call (the item-246
seam) - and it treats every host-extern / first-class-dispatch crossing
`_boundary` surfaces as `unbounded`. So cardinality can never disagree with the
boundary surface it is joined to.
"""

from __future__ import annotations


def cardinality(ir: dict) -> dict:
    """component name -> {"per_capability": {token: {...}}, "verdict": ...}.

    Present only for components that actually cross an emission boundary (the
    same gate `_boundary`'s render uses), so a crossing-free composition yields
    an empty map - the top-level key is still present in `audit_report`, but no
    component entry is (docs/design/260 §1, the LOW-finding precision).

    Each per-capability entry is `{"bound": int, "kind": "bounded"}` for a
    proved exact count, or `{"bound": None, "kind": "unbounded", "reason": str}`
    for a crossing whose multiplicity is not statically provable. The component
    verdict is the roll-up: `unbounded` dominates `bounded` (§2.3, §5.4 - the
    honest worst case wins, never averaged away).
    """
    # Lazy imports mirror `_boundary`/`audit_report`: cardinality is imported by
    # both `__main__` and `audit_diff`, so importing their internals at module
    # load would cycle.
    from .__main__ import (  # noqa: PLC0415
        _UNKNOWN_DISPATCH, _extern_reachability, _fn_call_names)
    from .emission_analysis import _calls_in, _emitting_capabilities  # noqa: PLC0415
    from .lower import _find_loop_step  # noqa: PLC0415

    reach = _extern_reachability(ir)
    externs = reach["__externs__"]

    fns = ir.get("functions") or []
    if isinstance(fns, dict):
        fns = list(fns.values())
    fn_names = {fn.get("name") for fn in fns}
    fn_by_name = {fn.get("name"): fn for fn in fns}
    fn_caps_map = _emitting_capabilities(fns, ir.get("externs") or [])

    # --- the fn call graph over the IR, for recursion and loop classification.
    # `_fn_call_names` records both component-body (`{kind: fn, name}`) and
    # pure-fn-body (`{kind: call, callee: {kind: var, name}}`) call shapes.
    direct: dict[str, set] = {}
    for name, decl in fn_by_name.items():
        called: set = set()
        _fn_call_names(decl.get("body") or [], called)
        direct[name] = called & fn_names

    def _reaches_self(start: str) -> bool:
        seen: set = set()
        stack = [start]
        while stack:
            node = stack.pop()
            for succ in direct.get(node, ()):
                if succ == start:
                    return True
                if succ not in seen:
                    seen.add(succ)
                    stack.append(succ)
        return False

    recursive = {name for name in fn_names if _reaches_self(name)}
    has_loop = {name for name, decl in fn_by_name.items()
                if _find_loop_step(decl.get("body") or []) is not None}

    def _closure(start: str) -> set:
        """Every fn transitively callable from `start`, including `start`."""
        seen = {start}
        stack = [start]
        while stack:
            node = stack.pop()
            for succ in direct.get(node, ()):
                if succ not in seen:
                    seen.add(succ)
                    stack.append(succ)
        return seen

    def _classify(name: str) -> tuple[str, str]:
        """Why a crossing reached through fn `name` is `unbounded` in Slice 1.

        Recursion outranks a loop outranks a plain host-extern reach, so the
        loudest true cause is named. Deterministic: the culprit is the
        lexicographically-first member of the reachable closure with that
        property."""
        closure = _closure(name)
        rec = sorted(closure & recursive)
        if rec:
            return ("recursion", rec[0])
        loops = sorted(closure & has_loop)
        if loops:
            return ("loop", loops[0])
        return ("host", name)

    def _reason(kind: str, detail: str) -> str:
        if kind == "recursion":
            return (f"recursion through `{detail}` - Slice 1 reports all "
                    "recursion as unbounded (no decreasing-fuel certification "
                    "yet, docs/design/260 §2.3)")
        if kind == "loop":
            return (f"a `while`/`for` loop in `{detail}` - the iteration count "
                    "is not statically bounded in Slice 1 (docs/design/260 §2.3)")
        if kind == "dispatch":
            return ("reached through a first-class emitting arrow - the "
                    "dispatched callable is not statically nameable, so its "
                    "multiplicity is unknown (docs/design/260 §5.1)")
        return (f"reached through host code `{detail}` - the extern body's "
                "crossing multiplicity is unchecked; a declared `calls` ceiling "
                "would bound it (docs/design/260 §3.2, §5.1)")

    services = ir.get("services") or {}

    report: dict[str, dict] = {}
    for comp in ir.get("components") or []:
        requires = comp.get("requires") or {}

        # ---- the exact, countable crossings: the three `_boundary` walk_expr
        # arms (emit step / req-target call / spawn-handle instance-get call),
        # counted with straight-line SUM and branch MAX (docs/design/260 §2.1).
        def count_expr(node, requires=requires):
            total: dict[str, int] = {}

            def _add(caps):
                for cap in caps:
                    total[cap] = total.get(cap, 0) + 1

            if isinstance(node, dict):
                kind = node.get("kind")
                # arm 1: a `req`-target emission call (`emit db.execute(...)` or
                # `let r = db.execute(...)`), keyed on the required KEY.
                target = node.get("target")
                if kind == "call" and isinstance(target, dict) \
                        and target.get("kind") == "req":
                    service = requires.get(target.get("name"))
                    spec = (((services.get(service) or {}).get("methods") or {})
                            .get(node.get("method")) or {})
                    if spec.get("emission"):
                        declared = spec.get("capabilities")
                        _add(sorted(declared) if declared is not None else ["*"])
                # arm 2: a spawn-handle provision-method call
                # `s.<key>.<method>(...)` reached through an `instance-get`
                # (item 246). This is the MEDIUM count-soundness fix.
                if kind == "call":
                    callee = node.get("callee")
                    if isinstance(callee, dict) and callee.get("kind") == "field":
                        recv = callee.get("target")
                        if isinstance(recv, dict) \
                                and recv.get("kind") == "instance-get":
                            service = recv.get("service")
                            mname = callee.get("name")
                            spec = (((services.get(service) or {})
                                     .get("methods") or {}).get(mname) or {})
                            if spec.get("emission"):
                                declared = spec.get("capabilities")
                                _add(sorted(declared)
                                     if declared is not None else ["*"])
                # branch nodes take the MAX over arms (a proved upper bound must
                # hold on every path, so the worst arm is the ceiling); the
                # scrutinee/cond is evaluated once, so it SUMS.
                if kind == "match":
                    _merge_sum(total, count_expr(node.get("scrutinee")))
                    _merge_max(total, [count_expr(arm.get("body"))
                                       for arm in node.get("arms") or []])
                    return total
                if kind == "if":
                    _merge_sum(total, count_expr(node.get("cond")))
                    _merge_max(total, [count_expr(node.get("then")),
                                       count_expr(node.get("otherwise"))])
                    return total
                # otherwise every child is evaluated: SUM. (An emission call's
                # own args sum in here too, counting a nested crossing once.)
                for value in node.values():
                    _merge_sum(total, count_expr(value))
            elif isinstance(node, list):
                for value in node:
                    _merge_sum(total, count_expr(value))
            return total

        def count_steps(steps, requires=requires, count_expr=count_expr):
            total: dict[str, int] = {}
            for step in steps or []:
                kind = step.get("step")
                if kind == "if":
                    _merge_sum(total, count_expr(step.get("cond")))
                    _merge_max(total, [
                        count_steps(step.get("then") or []),
                        count_steps(step.get("else")
                                    or step.get("otherwise") or [])])
                elif kind in ("while", "for"):
                    # loops never appear in a component/provide body (the frontend
                    # refuses them). If one somehow does, its body still counts;
                    # the multiplicity is caught as unbounded by the fn-reach pass
                    # when it reaches an emission. Sum defensively.
                    _merge_sum(total, count_steps(step.get("body") or []))
                elif kind == "provide":
                    for method in step.get("methods") or []:
                        _merge_sum(total, count_steps(method.get("body") or []))
                else:
                    _merge_sum(total, count_expr(step))
            return total

        counted = count_steps(comp.get("body") or [])

        # ---- the crossings whose multiplicity Slice 1 cannot prove: every
        # host-extern / fn-reach / first-class-dispatch crossing `_boundary`
        # surfaces. Mirror `_boundary`'s post-walk reach loop exactly so the two
        # surfaces are join-able and cannot disagree (§5.1, §5.4).
        unbounded: dict[str, str] = {}
        rank = {"dispatch": 1, "host": 1, "loop": 2, "recursion": 3}

        def _mark(token: str, kind: str, detail: str):
            candidate = _reason(kind, detail)
            existing_rank = _mark.ranks.get(token, 0)
            if rank[kind] >= existing_rank:
                _mark.ranks[token] = rank[kind]
                unbounded[token] = candidate
        _mark.ranks = {}

        called: set = set()
        _fn_call_names(comp.get("body") or [], called)
        dispatch = False
        for name in called:
            if name in externs:
                # a host extern reached directly from the component body
                _mark(name, "host", name)
                continue
            kind, detail = _classify(name)
            for ext in reach.get(name, set()):
                _mark(ext, kind, detail)
            fn_caps = fn_caps_map.get(name) or set()
            for cap in fn_caps:
                if cap == _UNKNOWN_DISPATCH:
                    dispatch = True
                    continue
                _mark(cap, kind, detail)

        # the first-class launder: an emitting callable handed to a dispatcher is
        # named in no call position; `_calls_in`'s value channel records it.
        value_refs: set = set()
        _calls_in(comp.get("body") or [], set(), values=value_refs)
        for ref in value_refs:
            ref_caps = fn_caps_map.get(ref) or set()
            for cap in ref_caps:
                if cap == _UNKNOWN_DISPATCH:
                    dispatch = True
                    continue
                # a value-position emitting reference is itself an unnameable
                # dispatch: what runs is not statically boundable (§5.1).
                _mark(cap, "dispatch", ref)
            if ref_caps:
                dispatch = True

        if dispatch:
            _mark(_UNKNOWN_DISPATCH, "dispatch", "*")

        # ---- assemble the per-capability surface. `unbounded` dominates: a token
        # both counted and unbounded is reported unbounded (§2.3).
        per_capability: dict[str, dict] = {}
        for token in sorted(counted):
            if token in unbounded:
                continue
            per_capability[token] = {"bound": counted[token], "kind": "bounded"}
        for token in sorted(unbounded):
            per_capability[token] = {"bound": None, "kind": "unbounded",
                                     "reason": unbounded[token]}

        if not per_capability:
            # no emission crossing: this component says nothing about cardinality,
            # and is omitted so a crossing-free composition yields an empty map.
            continue

        verdict = "unbounded" if unbounded else "bounded"
        report[comp["name"]] = {"per_capability": per_capability,
                                "verdict": verdict}
    return report


def _merge_sum(into: dict, other: dict) -> None:
    for token, count in other.items():
        into[token] = into.get(token, 0) + count


def _merge_max(into: dict, others: list) -> None:
    """Fold the MAX over a set of branch counts into `into` (added to whatever
    non-branch total `into` already holds - the scrutinee/cond side)."""
    branch: dict[str, int] = {}
    for other in others:
        for token, count in other.items():
            if count > branch.get(token, 0):
                branch[token] = count
    _merge_sum(into, branch)
