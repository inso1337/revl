"""Emission cardinality bounds for `revl audit` (docs/design/260-emission-cardinality-bounds.md).

Where `distribute.distributability` says which services may cross a process
seam, `cardinality` says HOW MANY TIMES per activation a component may cross
each capability boundary, read off the IR before anything runs. The verdict is
honest: a crossing whose multiplicity the analysis cannot prove is `unbounded`
(mirroring `distributability`'s named-verdict pattern), never a silent 0 and
never an optimistic bound.

Slice 1 implements §2.1's exact count for non-looping, non-recursive bodies plus
§2.3's `unbounded` for every loop and every recursion. Slice 2 adds §2.2's
bounded-iteration certification: a single-fn LINEAR self-recursion whose fuel
strictly decreases by a positive literal under a dominating base guard, with a
statically-resolvable initial fuel AND at most one in-SCC call site per path
(clause 4), reports the finite ceiling `base + max_iters * per_iter` instead of
`unbounded`. Everything the recognizer cannot certify still reports `unbounded`;
in particular a fan-out (`f(n-1); f(n-1)`) crosses `2^n - 1` times and is NEVER
reported as the linear ceiling `<= n` (the CRITICAL clause-4 fix). Multi-fn /
mutual recursion stays `unbounded` this slice (§5.2 OPEN).

The soundness tie (§5.1): the count fold reuses the EXACT reach `_boundary`
computes. It mirrors `_boundary`'s `walk_expr` arms line-for-line so the three
crossing shapes are all counted - the `emit` step, the `req`-target emission
call, and the spawn-handle `instance-get` provision-method call (the item-246
seam) - and it treats every host-extern / first-class-dispatch crossing
`_boundary` surfaces as `unbounded`. So cardinality can never disagree with the
boundary surface it is joined to.
"""

from __future__ import annotations

import math

# --------------------------------------------------------------------------
# Slice 2: bounded-iteration certification (docs/design/260 §2.2).
#
# A single-fn self-recursion whose fuel strictly decreases by a positive literal
# on every back-edge, that is dominated by a base guard, whose initial fuel is a
# literal or a config field, AND that has AT MOST ONE in-SCC call site per path
# (clause 4, the CRITICAL fix) is a LINEAR iteration: its per-activation crossing
# count is `base + max_iters * per_iter`, finite. Anything the recognizer cannot
# certify stays `unbounded`; a fan-out (`f(n-1); f(n-1)`) crosses `2^n - 1` times
# and MUST NOT be reported as the linear ceiling `<= n`.
# --------------------------------------------------------------------------


def _retry_mult(spec: dict) -> int:
    """Item 257 (Slice 2, HIGH-1): a `validated retry N` emission crossing may
    fire up to `N + 1` times per activation (the first attempt plus N validation
    retries), so its contribution to the cardinality count is multiplied by
    `N + 1`.

    This is a STATIC MULTIPLIER on the SINGLE crossing node, NOT a loop and NOT a
    recursion: the retry is a constant factor on one countable crossing, so item
    260's bounded-iteration recognizer is not involved and no decreasing-fuel
    certification is needed. The exact `<= N + 1` ceiling is therefore
    by-construction, closing the false-LOW `<= 1` the 257 review flagged. A
    non-retry crossing (no `retry` key, the byte-identical default) multiplies by
    1, exactly as before."""
    return (spec.get("retry") or 0) + 1


def _int_lit(node) -> int | None:
    """The integer a `{kind: lit}` node carries, or None. `bool` is not an int
    literal here (revl has no bool<->int coercion in a fuel position)."""
    if isinstance(node, dict) and node.get("kind") == "lit":
        value = node.get("value")
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


def _bare_name(node) -> str | None:
    """The name a bare variable reference carries. Fn bodies lower a variable to
    `{kind: var, name}`; component bodies to `{kind: name, id}`."""
    if not isinstance(node, dict):
        return None
    if node.get("kind") == "var" and isinstance(node.get("name"), str):
        return node["name"]
    if node.get("kind") == "name" and isinstance(node.get("id"), str):
        return node["id"]
    return None


def _is_self_call(node, fname: str) -> bool:
    """A direct call to `fname` (a fn-body `{kind: call, callee: {var}}` or a
    component-body `{kind: fn, name}`)."""
    if not isinstance(node, dict):
        return False
    if node.get("kind") == "fn" and node.get("name") == fname:
        return True
    if node.get("kind") == "call":
        callee = node.get("callee")
        if isinstance(callee, dict) and callee.get("kind") == "var" \
                and callee.get("name") == fname:
            return True
    return False


def _is_param_call(node, pname: str) -> bool:
    """A call whose callee is the parameter `pname` invoked directly (the
    first-class emitting arrow being run, `step(msgs)`)."""
    if not isinstance(node, dict) or node.get("kind") != "call":
        return False
    callee = node.get("callee")
    return isinstance(callee, dict) and callee.get("kind") == "var" \
        and callee.get("name") == pname


def _path_max_calls(node, predicate) -> int:
    """Max number of `predicate`-matching call nodes reachable on ONE execution
    path: a straight-line list SUMS, an `if`/`match` takes the MAX over its arms
    (only one arm runs) plus the once-evaluated cond/scrutinee. This is the
    over-a-path count clause 4 and the per-iteration multiplicity both need - two
    self-calls in different arms is still linear; two sequentially reachable on
    one path fans out."""
    if isinstance(node, list):
        return sum(_path_max_calls(item, predicate) for item in node)
    if not isinstance(node, dict):
        return 0
    step, kind = node.get("step"), node.get("kind")
    if step == "if" or kind == "if":
        cond = _path_max_calls(node.get("cond"), predicate)
        then = node.get("then")
        other = node.get("else")
        if other is None:
            other = node.get("otherwise")
        return cond + max(_path_max_calls(then, predicate),
                          _path_max_calls(other, predicate))
    if kind == "match":
        base = _path_max_calls(node.get("scrutinee"), predicate)
        arms = [_path_max_calls(arm.get("body"), predicate)
                for arm in node.get("arms") or []]
        return base + (max(arms) if arms else 0)
    total = 1 if predicate(node) else 0
    for value in node.values():
        total += _path_max_calls(value, predicate)
    return total


def _has_top_return(steps) -> bool:
    """A branch that exits: a `return` at its own statement level, or an `if`
    whose branches both exit. Used only to pick the base branch of a guard when
    both branches lack a self-call (the base is the one that returns)."""
    for step in steps or []:
        if not isinstance(step, dict):
            continue
        if step.get("step") == "return":
            return True
        if step.get("step") == "if":
            other = step.get("else")
            if other is None:
                other = step.get("otherwise")
            if _has_top_return(step.get("then")) and _has_top_return(other):
                return True
    return False


def _fuel_cmp(cond) -> tuple[str, int, str] | None:
    """`(param, c, op)` for a base-guard comparison `n <= c` / `n < c` (or the
    literal-on-the-left mirror), op normalized so the guarded/base region is
    `param <op> c`. None when the cond is not a param-vs-literal comparison."""
    if not isinstance(cond, dict) or cond.get("kind") != "bin":
        return None
    op = cond.get("op")
    left, right = cond.get("left"), cond.get("right")
    lname, lint = _bare_name(left), _int_lit(left)
    rname, rint = _bare_name(right), _int_lit(right)
    if lname is not None and rint is not None and op in ("<=", "<"):
        return (lname, rint, op)
    # mirror: `c >= n` is `n <= c`; `c > n` is `n < c`.
    if rname is not None and lint is not None and op in (">=", ">"):
        return (rname, lint, "<=" if op == ">=" else "<")
    return None


def _max_iters(n0: int, c: int, k: int, op: str) -> int:
    """The number of times the recursive arm runs before the base guard fires.
    `op` is the guard's base-region test: `<=` bases when `n <= c` (recurse while
    `n > c`), `<` bases when `n < c` (recurse while `n >= c`)."""
    if k <= 0:
        return 0
    if op == "<=":
        return max(0, math.ceil((n0 - c) / k)) if n0 > c else 0
    return max(0, (n0 - c) // k + 1) if n0 >= c else 0


def _certify_recursion(fname, decl, direct, recursive, closure,
                       reach, fn_caps_map, has_loop, unknown_dispatch):
    """Decide whether the self-recursive fn `fname` is a certifiable LINEAR
    iteration (docs/design/260 §2.2 clauses 1,2,4). On success returns a record
    `{ok: True, params, fuel_index, fuel_op, c, k, base, cont}`; on refusal
    `{ok: False, reason_kind}`. The scope is deliberately narrow - single-fn
    self-recursion whose only crossings ride invoked arrow parameters - so every
    refusal is sound (it can only widen a verdict to `unbounded`, never claim a
    false ceiling)."""

    def refuse(reason_kind):
        return {"ok": False, "reason_kind": reason_kind}

    # --- scope: single-fn self-recursion only. Mutual recursion (fname does not
    # call itself directly, or another recursive fn is reachable) is out of scope
    # this slice (§5.2 OPEN) and stays `unbounded`.
    if fname not in direct.get(fname, set()):
        return refuse("mutual")
    if any(g != fname and g in recursive for g in closure):
        return refuse("mutual")

    # --- the crossings must ride invoked parameters only. A host-extern reach or
    # a named host capability inside the body is uncountable (§2.3), and a call
    # to any other fn could launder a crossing we cannot fold; refuse so the
    # Slice-1 reach loop keeps its honest `unbounded`.
    if reach.get(fname):
        return refuse("host")
    caps = fn_caps_map.get(fname) or set()
    if caps - {unknown_dispatch}:
        return refuse("host")
    if fname in has_loop:
        return refuse("host")
    # F may invoke its own parameters (the arrows) and recurse, but calling any
    # OTHER named fn could launder a crossing the fold cannot follow; refuse.
    other_fn_calls = set()
    _local_call_names(decl.get("body") or [], other_fn_calls)
    if (other_fn_calls & set(direct)) - {fname}:
        return refuse("host")

    params = [p.get("name") for p in decl.get("params") or []]
    body = decl.get("body") or []

    def is_self(node):
        return _is_self_call(node, fname)

    # --- clause 2: a dominating base guard. Find the first top-level `if` whose
    # cond is a fuel comparison and one branch has no self-call; nothing before
    # it may recurse (else it does not dominate).
    guard_i = None
    for i, step in enumerate(body):
        if isinstance(step, dict) and step.get("step") == "if" \
                and _fuel_cmp(step.get("cond")) is not None:
            guard_i = i
            break
    if guard_i is None:
        return refuse("noguard")
    if _path_max_calls(body[:guard_i], is_self) != 0:
        return refuse("noguard")

    guard = body[guard_i]
    pname, c, op = _fuel_cmp(guard.get("cond"))
    if pname not in params:
        return refuse("noguard")
    fuel_index = params.index(pname)

    then_b = guard.get("then") or []
    else_b = guard.get("else")
    if else_b is None:
        else_b = guard.get("otherwise") or []
    self_then = _path_max_calls(then_b, is_self)
    self_else = _path_max_calls(else_b, is_self)
    # the base branch is the guarded branch with no self-call (and, when both
    # qualify, the one that exits); the other branch plus the steps after the
    # guard form the recursive continuation.
    if self_then == 0 and (self_else != 0 or _has_top_return(then_b)
                           or not _has_top_return(else_b)):
        base, other = then_b, else_b
    elif self_else == 0:
        base, other = else_b, then_b
    else:
        return refuse("noguard")
    cont = list(other) + list(body[guard_i + 1:])

    self_calls = []
    _collect_self_calls(body, fname, self_calls)
    if not self_calls:
        return refuse("mutual")

    # --- clause 4 (the CRITICAL): at most one in-SCC call site per path. Two
    # self-calls sequentially reachable on one path fan out to `2^n - 1`.
    if _path_max_calls(cont, is_self) > 1:
        return refuse("fanout")
    if _path_max_calls(base, is_self) != 0:
        return refuse("noguard")

    # --- clause 1: every self-call strictly decreases the SAME fuel param by a
    # positive literal, and threads every OTHER argument by identity (so an arrow
    # parameter cannot be swapped for a wider dispatch on the back-edge).
    k_min = None
    for call in self_calls:
        args = call.get("args") or []
        if len(args) != len(params):
            return refuse("nonfuel")
        fuel_arg = args[fuel_index]
        if not (isinstance(fuel_arg, dict) and fuel_arg.get("kind") == "bin"
                and fuel_arg.get("op") == "-"
                and _bare_name(fuel_arg.get("left")) == pname):
            return refuse("nonfuel")
        k = _int_lit(fuel_arg.get("right"))
        if k is None or k < 1:
            return refuse("nonfuel")
        k_min = k if k_min is None else min(k_min, k)
        for j, arg in enumerate(args):
            if j == fuel_index:
                continue
            if _bare_name(arg) != params[j]:
                return refuse("nonfuel")

    return {"ok": True, "params": params, "fuel_index": fuel_index,
            "fuel_op": op, "c": c, "k": k_min, "base": base, "cont": cont}


def _local_call_names(node, out: set) -> None:
    """Names called in fn-body position (`{kind: call, callee: {var}}`) or
    component position (`{kind: fn, name}`). Unlike `_fn_call_names` this is not
    filtered against known fns yet - the caller intersects."""
    if isinstance(node, dict):
        if node.get("kind") == "fn" and isinstance(node.get("name"), str):
            out.add(node["name"])
        if node.get("kind") == "call":
            callee = node.get("callee")
            if isinstance(callee, dict) and callee.get("kind") == "var" \
                    and isinstance(callee.get("name"), str):
                out.add(callee["name"])
        for value in node.values():
            _local_call_names(value, out)
    elif isinstance(node, list):
        for value in node:
            _local_call_names(value, out)


def _collect_self_calls(node, fname: str, out: list) -> None:
    if isinstance(node, dict):
        if _is_self_call(node, fname):
            out.append(node)
        for value in node.values():
            _collect_self_calls(value, fname, out)
    elif isinstance(node, list):
        for value in node:
            _collect_self_calls(value, fname, out)


def _cert_reason(kind: str, fname: str) -> str:
    """Why a bounded-SHAPED self-recursion is still `unbounded` (docs/design/260
    §2.2/§2.3). The fan-out reason is the load-bearing one: it is the CRITICAL a
    prior review missed, so it names the `2^n` blow-up explicitly."""
    reasons = {
        "fanout": (f"branching/tree recursion through `{fname}` reaches more than "
                   "one recursive call on a single path (it fans out to ~2^n "
                   "crossings); it fails the linear-iteration restriction and is "
                   "unbounded, never the linear ceiling `base + max_iters * "
                   "per_iter` (docs/design/260 §2.2 clause 4)"),
        "nonfuel": (f"recursion through `{fname}` has no fuel that strictly "
                    "decreases by a positive literal on every back-edge "
                    "(docs/design/260 §2.2 clause 1)"),
        "noguard": (f"recursion through `{fname}` has no dominating base guard "
                    "`if (n <= c)` with a non-recursive base branch "
                    "(docs/design/260 §2.2 clause 2)"),
        "nonresolvable": (f"recursion through `{fname}` is bounded-shaped but its "
                          "initial fuel is not a literal or a config field, so the "
                          "ceiling is not statically provable (docs/design/260 "
                          "§2.2 clause 3)"),
        "mutual": (f"recursion through `{fname}` is multi-fn or mutual recursion; "
                   "Slice 2 certifies only single-fn self-recursion "
                   "(docs/design/260 §5.2)"),
        "host": (f"recursion through `{fname}` reaches a crossing whose per-call "
                 "multiplicity is not countable (a host extern or an un-nameable "
                 "dispatch), so it stays unbounded (docs/design/260 §2.3)"),
    }
    return reasons.get(kind, reasons["mutual"])


def _collect_arrows(node, out: dict) -> None:
    """Every `let <name> = (…) => …` arrow binding in a component/method body,
    keyed by the name its application's callee resolves to. This is the SAME
    collection `_boundary` does for the arrow-parameter seam, so the cardinality
    count follows the crossing identically (GHSA-wg4v-r47x-52p2 residual, #396)."""
    if isinstance(node, dict):
        if node.get("step") == "let" and isinstance(node.get("value"), dict) \
                and node["value"].get("kind") == "arrow":
            out[node.get("name")] = node["value"]
        for value in node.values():
            _collect_arrows(value, out)
    elif isinstance(node, list):
        for value in node:
            _collect_arrows(value, out)


def _count_arrow_body(body, bind_service: dict, services: dict) -> dict:
    """Count emission crossings that ride a service-typed arrow PARAMETER (item
    396): a `t.run(s)` inside an arrow body whose receiver `t` is a parameter
    the application bound to a provision (`bind_service` maps that param name to
    the provided service). Mirrors `count_expr`'s exact-count discipline —
    straight-line SUM, branch MAX, the `retry` multiplier on the single crossing
    — so the arrow-param spelling `let f = (t: Task, s) => t.run(s); f(w.task, s)`
    reports the SAME per-activation bound as the direct `emit w.<key>.<method>`
    spelling (docs/design/260 §2.1, and the §5.1 tie to `_boundary`)."""
    total: dict[str, int] = {}
    if isinstance(body, dict):
        kind = body.get("kind")
        tgt = body.get("target")
        if kind == "call" and isinstance(tgt, dict) \
                and tgt.get("kind") == "name" and tgt.get("id") in bind_service:
            spec = (((services.get(bind_service[tgt["id"]]) or {})
                     .get("methods") or {}).get(body.get("method")) or {})
            if spec.get("emission"):
                declared = spec.get("capabilities")
                mult = _retry_mult(spec)
                for cap in (sorted(declared) if declared is not None else ["*"]):
                    total[cap] = total.get(cap, 0) + mult
        if kind == "match":
            _merge_sum(total, _count_arrow_body(
                body.get("scrutinee"), bind_service, services))
            _merge_max(total, [_count_arrow_body(arm.get("body"),
                                                 bind_service, services)
                               for arm in body.get("arms") or []])
            return total
        if kind == "if":
            _merge_sum(total, _count_arrow_body(
                body.get("cond"), bind_service, services))
            _merge_max(total, [
                _count_arrow_body(body.get("then"), bind_service, services),
                _count_arrow_body(body.get("otherwise"), bind_service, services)])
            return total
        for value in body.values():
            _merge_sum(total, _count_arrow_body(value, bind_service, services))
    elif isinstance(body, list):
        for value in body:
            _merge_sum(total, _count_arrow_body(value, bind_service, services))
    return total


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

    # --- Slice 2: bounded-iteration certification, memoized per fn. A fn is
    # certifiable only if it is a single-fn linear self-recursion (§2.2); every
    # other recursion (mutual, fan-out, non-decreasing, host-reaching) refuses
    # and stays `unbounded`, exactly as Slice 1.
    certify_cache: dict[str, dict] = {}

    def _certify(name: str) -> dict:
        if name not in certify_cache:
            decl = fn_by_name.get(name)
            if decl is None or name not in recursive:
                certify_cache[name] = {"ok": False, "reason_kind": "mutual"}
            else:
                certify_cache[name] = _certify_recursion(
                    name, decl, direct, recursive, _closure(name), reach,
                    fn_caps_map, has_loop, _UNKNOWN_DISPATCH)
        return certify_cache[name]

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

        # local arrow bindings in scope (`let f = (…) => …`), so the count fold
        # can follow a service-typed arrow-parameter application exactly as
        # `_boundary` does (arm 3 below, item 396).
        arrow_defs: dict = {}
        _collect_arrows(comp.get("body") or [], arrow_defs)

        # ---- Slice 2 resolution state: a certified/refused recursive-loop call
        # in this component body is resolved once (below) into either a folded
        # integer contribution (`resolutions`), a symbolic per-iteration ceiling
        # (`cert_symbolic`), or an `unbounded` mark (`cert_unbounded`). `count_expr`
        # short-circuits a resolved call node so the arrow it carries is folded
        # `max_iters` times, never counted once by the generic descent.
        resolutions: dict[int, dict] = {}
        cert_symbolic: dict[str, dict] = {}
        cert_unbounded: dict[str, str] = {}

        # ---- the exact, countable crossings: the three `_boundary` walk_expr
        # arms (emit step / req-target call / spawn-handle instance-get call),
        # counted with straight-line SUM and branch MAX (docs/design/260 §2.1).
        def count_expr(node, requires=requires):
            total: dict[str, int] = {}

            def _add(caps, mult=1):
                for cap in caps:
                    total[cap] = total.get(cap, 0) + mult

            if isinstance(node, dict):
                resolved = resolutions.get(id(node))
                if resolved is not None:
                    return dict(resolved["int"])
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
                        _add(sorted(declared) if declared is not None else ["*"],
                             _retry_mult(spec))
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
                                     if declared is not None else ["*"],
                                     _retry_mult(spec))
                # arm 3: an application of a local arrow whose parameter is
                # service-typed and receives a provision (`instance-get`) as its
                # argument — the emission rides the parameter inside the arrow
                # body (`let f = (t: Task, s) => t.run(s); f(w.task, s)`). Bind
                # each service-typed parameter to the provided service and count
                # the arrow body's crossings, so this spelling reports the SAME
                # bound as the direct `emit w.<key>.<method>` (GHSA-wg4v residual,
                # #396). Mirrors `_boundary`'s arrow-parameter arm (the §5.1 tie).
                if kind == "call":
                    callee = node.get("callee")
                    if isinstance(callee, dict) and callee.get("kind") == "name" \
                            and callee.get("id") in arrow_defs:
                        arrow = arrow_defs[callee["id"]]
                        svc_params = arrow.get("service_params") or {}
                        params = arrow.get("params") or []
                        call_args = node.get("args") or []
                        bind_service: dict[str, str] = {}
                        for i, param in enumerate(params):
                            service = svc_params.get(param)
                            if service is None or i >= len(call_args):
                                continue
                            arg = call_args[i]
                            if isinstance(arg, dict) \
                                    and arg.get("kind") == "instance-get":
                                bind_service[param] = arg.get("service") or service
                        if bind_service:
                            _merge_sum(total, _count_arrow_body(
                                arrow.get("body"), bind_service, services))
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

        # ---- Slice 2: resolve every self-recursive call reached from this
        # component body into a bounded / bounded-symbolic / unbounded verdict
        # (docs/design/260 §2.2), BEFORE the generic count so the resolved node
        # short-circuits and the arrow it carries is not counted once.
        def _record_cert_unbounded(cap: str, reason: str):
            if cap not in cert_unbounded:
                cert_unbounded[cap] = reason

        def _resolve_loop_call(node):
            if node.get("kind") == "fn":
                fname = node.get("name")
            else:
                fname = (node.get("callee") or {}).get("name")
            args = node.get("args") or []
            arg_vecs = [count_expr(arg) for arg in args]
            rec = _certify(fname)
            if not rec["ok"]:
                resolutions[id(node)] = {"int": {}}
                reason = _cert_reason(rec["reason_kind"], fname)
                for vec in arg_vecs:
                    for cap in vec:
                        _record_cert_unbounded(cap, reason)
                return

            params = rec["params"]
            per_iter = {p: _path_max_calls(
                rec["cont"], lambda n, pp=p: _is_param_call(n, pp))
                for p in params}
            base_m = {p: _path_max_calls(
                rec["base"], lambda n, pp=p: _is_param_call(n, pp))
                for p in params}
            fuel_index = rec["fuel_index"]
            fuel_arg = args[fuel_index] if fuel_index < len(args) else None

            n0 = _int_lit(fuel_arg)
            is_config = (isinstance(fuel_arg, dict)
                         and fuel_arg.get("kind") == "config"
                         and isinstance(fuel_arg.get("field"), str))
            if n0 is not None:
                iters = _max_iters(n0, rec["c"], rec["k"], rec["fuel_op"])
                vec: dict[str, int] = {}
                for i, p in enumerate(params):
                    mult = base_m[p] + iters * per_iter[p]
                    if mult == 0:
                        continue
                    for cap, cnt in arg_vecs[i].items():
                        vec[cap] = vec.get(cap, 0) + mult * cnt
                resolutions[id(node)] = {"int": vec}
            elif is_config:
                expr = f"config.{fuel_arg['field']}"
                base_vec: dict[str, int] = {}
                for i, p in enumerate(params):
                    for cap, cnt in arg_vecs[i].items():
                        if base_m[p]:
                            base_vec[cap] = base_vec.get(cap, 0) + base_m[p] * cnt
                        if per_iter[p]:
                            entry = cert_symbolic.setdefault(
                                cap, {"expr": expr, "per_iter": 0})
                            entry["per_iter"] += per_iter[p] * cnt
                resolutions[id(node)] = {"int": base_vec}
            else:
                # bounded-shaped but the initial fuel is a param / host value /
                # arithmetic: the ceiling is not provable (clause 3), unbounded.
                resolutions[id(node)] = {"int": {}}
                reason = _cert_reason("nonresolvable", fname)
                for vec in arg_vecs:
                    for cap in vec:
                        _record_cert_unbounded(cap, reason)

        def _walk_resolve(node):
            if isinstance(node, dict):
                callee = node.get("callee")
                if (node.get("kind") == "fn" and node.get("name") in recursive) \
                        or (node.get("kind") == "call"
                            and isinstance(callee, dict)
                            and callee.get("kind") == "var"
                            and callee.get("name") in recursive):
                    _resolve_loop_call(node)
                    return  # args already folded; do not descend into them
                for value in node.values():
                    _walk_resolve(value)
            elif isinstance(node, list):
                for value in node:
                    _walk_resolve(value)

        _walk_resolve(comp.get("body") or [])

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

        # ---- fold in the Slice 2 certification verdicts. A refused loop's
        # arrow-mediated cap is `unbounded` with its precise §2.2 reason (the
        # fan-out CRITICAL among them); a certified symbolic loop contributes a
        # `bounded-symbolic` per-iteration ceiling. `unbounded` still dominates.
        for cap, reason in cert_unbounded.items():
            unbounded[cap] = reason
        symbolic = {cap: entry for cap, entry in cert_symbolic.items()
                    if cap not in unbounded}

        # ---- assemble the per-capability surface. Precedence: `unbounded`
        # dominates `bounded-symbolic` dominates `bounded` (§2.3, §5.4 - the
        # honest worst case wins).
        per_capability: dict[str, dict] = {}
        for token in sorted(counted):
            if token in unbounded or token in symbolic:
                continue
            per_capability[token] = {"bound": counted[token], "kind": "bounded"}
        for token in sorted(symbolic):
            per_capability[token] = {"bound": None, "kind": "bounded-symbolic",
                                     "expr": symbolic[token]["expr"],
                                     "per_iter": symbolic[token]["per_iter"]}
        for token in sorted(unbounded):
            per_capability[token] = {"bound": None, "kind": "unbounded",
                                     "reason": unbounded[token]}

        if not per_capability:
            # no emission crossing: this component says nothing about cardinality,
            # and is omitted so a crossing-free composition yields an empty map.
            continue

        verdict = ("unbounded" if unbounded
                   else "bounded-symbolic" if symbolic else "bounded")
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
