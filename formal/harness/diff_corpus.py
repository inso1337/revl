"""Differential harness: the formal models vs the extracted corpus + checker.

Pipeline (formal/STATUS.md, "differential oracle"):

1. parse every .rvl in the corpus with revl's real parser (`revl.parser`);
2. extract FACTS, not verdicts. The manifest facts (component requires /
   provides, require-binding -> service, provide-key -> service) and the
   marker facts (per-statement classification, and a call fact carrying its
   marker context) are joined by a REACHABILITY model of the bodies the
   marker rule alone cannot see:

     - a service method's emission bound (plain / any / scoped, plus the
       scoped declaration's entries) — the upper bound a provider may not
       exceed;
     - a provide method's REACH: the canonical capabilities its body
       crosses, resolved through require bindings, spawn handles
       (`w.task.run` reads the child's `task` provision), emission externs
       and the transitively-emitting named functions;
     - a component's activation emit-step surface, the capabilities its
       `requires` bindings grant it, and its activation-body spawn edges.

   That is what lets the shaped model see a provider exceeding its
   declaration and a spawn widening a child's authority, not just a missing
   `emit` marker.
2b. for G7 the facts are of a different kind, and the reference is a RUN.
   A teardown disposition is a property of an execution, not of a
   manifest, so the corpus is an enumerated set of activation scenarios —
   one activation's LIFO stack (the three entry kinds, registered through
   the reference's five seams) and the verdict it unwound under — and the
   reference side DRIVES `backends/python/runtime.py` over each of them,
   reading each entry's fate off the runtime's own state rather than
   recomputing it. See `teardown_scenarios` / `teardown_observation`, and
   `teardown_coverage` for the non-vacuity ratchet that keeps the row
   from agreeing over shapes the corpus never reaches.
3. compute reference verdicts here AND run the Lean oracle
   (`formal/harness/Oracle.lean`) over the same TSV, then diff them. This
   is the HARD gate, and since item 418 step 6 the two sides are no longer
   two hand-written restatements of the same understanding:

     - the LEAN side `decide`s the PROVED model — `RevL.Manifest`'s
       `ProvidesDisjoint` / `RequiresClosed` / `LinkOK` over its
       `(key, realm)` slots, and `RevL.CapCeilings.Attenuates` over the
       proved `Covers`/`budgetOf` development. Change an L0 definition and
       the verdicts move;
     - the PYTHON side calls the SHIPPED checker's own algebra
       (`src/revl/cap_order.parse_cap` / `covers` / `split_ceilings`) and
       the real parser, and computes nothing about capabilities itself.

   A mismatch is therefore drift between the machine-checked model and
   what revl actually does, and it fails `make formal`.
4. report checker alignment: compile each file with the real checker
   (`revl.compiler.compile_source`) and compare its refusal codes against
   the formal verdicts. Informational, EXCEPT `missed-G4` and `missed-G2`
   (`FATAL_BUCKETS`) — the checker refusing where the model sees nothing
   is the dangerous direction and fails the gate.

Nothing is skipped. A parse-time REFUSAL is a verdict (revl rejecting the
file IS the answer) and is carried through as an `X` row; a parsed file
with no component has no composition to model and is named in the
`no-manifest` report rather than dropped from every count.
"""

import dataclasses
import itertools
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import NamedTuple

REPO = Path(__file__).resolve().parents[2]
FORMAL = Path(__file__).resolve().parents[1]
CORPUS_DIRS = ("examples", "tck", "tests")

sys.path.insert(0, str(REPO / "src"))
# The G7 row RUNS the reference runtime rather than reading a manifest, so the
# python backend's module directory joins the path exactly as the runtime's own
# suites (`tests/test_estop_443.py`) put it there.
sys.path.insert(0, str(REPO / "backends" / "python"))

from revl import cap_order
from revl import recovery
from revl.compiler import compile_source
from revl.diagnostics import classify
from revl.errors import RevlError
from revl.typecheck import _HOST_ACQUIRE_VERBS  # the shipped acquire-verb table
from revl.typecheck import parse_type  # the shipped type-head splitter
from revl.wal import WAL_GUARANTEE, WAL_VERSION
import runtime as _rt  # backends/python/runtime.py — the reference teardown
from revl.parser import (
    EffectStmt,
    EmitExpr,
    EmitStmt,
    ExprArrow,
    ExprCall,
    ExprField,
    ExprVar,
    IsolateStmt,
    LetEffect,
    Parser,
    ProvideStmt,
    SpawnExpr,
)



def corpus_files() -> list[Path]:
    out: list[Path] = []
    for d in CORPUS_DIRS:
        root = REPO / d
        if root.is_dir():
            out.extend(sorted(root.rglob("*.rvl")))
    return out


def _route(callee: object) -> tuple[str, str] | None:
    """(root, chain) for a call callee, incl. nested field chains.

    `w.task.run(...)` parses as field(field(var w, task), run): the root is
    the outermost variable and the chain is the dotted method path joined
    right-to-left — `("w", "task.run")`. A plain dotted var callee keeps its
    single hop. Other shapes return None (not a boundary-typed receiver).
    """
    if isinstance(callee, ExprField):
        parts: list[str] = []
        cur = callee
        while isinstance(cur, ExprField):
            parts.append(cur.name)
            cur = cur.target
        if isinstance(cur, ExprVar):
            return cur.name, ".".join(reversed(parts))
        return None
    if isinstance(callee, ExprVar) and "." in callee.name:
        root, _, rest = callee.name.partition(".")
        return root, rest
    if isinstance(callee, ExprVar):
        return callee.name, ""
    return None


# ---------------------------------------------------------------- caps
#
# There is NO capability grammar here (item 418 step 6). A canonical
# capability string is read by `src/revl/cap_order.parse_cap` — the checker's
# own parser, the one place the (T, P) algebra is implemented — and the
# order is `cap_order.covers`. The harness used to carry a third
# re-implementation of both; the point of the differential is to compare
# the PROVED model against the SHIPPED checker, and a private Python copy
# of the rules made the Python side a third opinion instead of the real one.

_CAP_CACHE: dict[str, cap_order.Cap] = {}


def parse_cap(s: str) -> cap_order.Cap:
    """`cap_order.parse_cap`, memoized. A malformed capability is a hard
    error: it means the exporter built a spelling the checker cannot read."""
    hit = _CAP_CACHE.get(s)
    if hit is None:
        hit = _CAP_CACHE[s] = cap_order.parse_cap(s)
    return hit


def cap_decomposition_rows(caps: "set[str]") -> list[str]:
    """Z/Y rows: the canonical caps the corpus mentions, decomposed by
    `cap_order` into the model's `(token, valuation)` shape so the Lean
    side never re-reads the grammar. A value's KIND comes from the closed
    registry's own canonicalization: a path canonicalizes to a component
    tuple, a ceiling to a base-unit int, a discrete resource to a str."""
    rows: list[str] = []
    for s in sorted(caps):
        cap = parse_cap(s)
        rows.append("\t".join(["Z", s, cap.token]))
        for name, value in cap.params:
            if isinstance(value, tuple):
                rows.append("\t".join(["Y", s, name, "path", "/".join(value)]))
            elif isinstance(value, bool):  # pragma: no cover - not a cap value
                raise SystemExit(f"differential oracle: bool cap value in {s!r}")
            elif isinstance(value, int):
                rows.append("\t".join(["Y", s, name, "ceiling", str(value)]))
            else:
                rows.append("\t".join(["Y", s, name, "discrete", str(value)]))
    return rows


def attenuation_halves(held: "set[str]", reach: "set[str]") -> "tuple[bool, bool]":
    """`RevL.CapCeilings.Attenuates` as its two halves, computed with the
    checker's own algebra: the RESOURCE fold over ceiling-stripped
    capabilities (`covers_set` empty), and the CEILING budget check —
    wherever the parent declares a budget for the child's token and
    parameter, the child must declare one too and no larger (a dropped
    ceiling is `+inf`, hence a widening).

    The halves are returned separately, not because the verdict needs them
    apart (it is their conjunction) but because `attenuation_coverage` has to
    know which half decided an edge: the formal-layer audit found the ceiling
    half agreeing VACUOUSLY over a corpus that bound no integer parameter, so
    "the W row agrees" said nothing about it (issue 210)."""
    hcaps = [parse_cap(h) for h in held]
    rcaps = [parse_cap(c) for c in reach]
    hsplit = [cap_order.split_ceilings(h) for h in hcaps]
    rsplit = [cap_order.split_ceilings(c) for c in rcaps]
    resource = not cap_order.covers_set([h for h, _ in hsplit],
                                        [c for c, _ in rsplit])
    for cap, (_stripped, ceilings) in zip(rcaps, rsplit):
        # budgetOf: the MOST GENEROUS declaration the parent holds under
        # this token for this parameter (RevL.Lemmas.budgetOf).
        budgets: dict[str, int] = {}
        for hcap, (_hs, hceils) in zip(hcaps, hsplit):
            if hcap.token != cap.token:
                continue
            for name, bound in hceils.items():
                budgets[name] = max(budgets.get(name, bound), bound)
        for name, bound in budgets.items():
            if name not in ceilings or ceilings[name] > bound:
                return resource, False
    return resource, True


def attenuates(held: "set[str]", reach: "set[str]") -> bool:
    """`RevL.CapCeilings.Attenuates`: both halves must hold."""
    resource, ceiling = attenuation_halves(held, reach)
    return resource and ceiling


#: Which half decided each spawn edge, for `attenuation_coverage`. Filled by
#: `reference_from_tsv`; never compared.
_ATTENUATION_HALVES: dict = {}


def attenuation_coverage() -> list[str]:
    """The non-vacuity ratchet for the `W` row's CEILING half (issue 210).

    The formal-layer audit's finding, verbatim: the capability-ceiling half of
    the oracle agreed **vacuously**, because no corpus file declared an integer
    parameter, so `ceilingOKB` / `RevL.Lemmas.budgetOf` — the whole `budgetOf`
    development the `attenuatesB_iff` bridge rests on — was never entered. An
    agreeing row over an unexercised shape is the same defect class as a
    byte-agreement over a corpus that never reaches the logic (item 429).

    So the corpus must EXERCISE the branch, and this says so and enforces it:

      * some edge's capabilities bind a ceiling parameter at all;
      * some edge is ADMITTED with the resource half satisfied and a real
        budget compared (`examples/budget_attenuation.rvl`, 50 <= 100);
      * some edge is REFUSED BY THE CEILING HALF ALONE — the resource fold
        finds nothing uncovered and only the budget check flags it
        (`examples/rejections/g4_spawn_widens_budget.rvl`, 1000 > 100). This
        is the clause the pre-210 corpus could not satisfy.
    """
    bound = admitted = refused = None
    for key, (resource, ceiling, has_ceiling) in _ATTENUATION_HALVES.items():
        if not has_ceiling:
            continue
        bound = bound or key
        if resource and ceiling:
            admitted = admitted or key
        if resource and not ceiling:
            refused = refused or key
    findings: list[str] = []
    for label, witness in (("any edge binds a ceiling parameter", bound),
                           ("a budget is compared and ADMITTED", admitted),
                           ("a budget is REFUSED by the ceiling half alone",
                            refused)):
        if witness is None:
            findings.append(f"attenuation coverage: NO witness that {label} — "
                            "the W row's ceiling half would agree vacuously")
    if not findings:
        print(f"attenuation coverage: {len(_ATTENUATION_HALVES)} spawn edges, "
              f"ceiling half entered; admitted={admitted[0]} refused={refused[0]}")
    return findings


def _canon_cap(root: str, declared: str) -> str:
    """Token the wiring key, params from the declared valuation: a declaring
    `fs.write(path="/tmp")` crossed through key `fs` renders `fs(path="/tmp")`,
    the spelling the checker's diagnostics use. A bare declared token keeps
    the bare key."""
    oi = declared.find("(")
    return root if oi < 0 else root + declared[oi:]


def _bound_index(services_by_name: dict) -> dict[tuple[str, str], tuple[str, tuple[str, ...]]]:
    """(svc, method) -> ('plain'|'any'|'scoped', declared entries) — a
    service's emission declaration, the upper bound on a provider's reach.
    `plain` for `fn`, `any` for bare `emission`, `scoped` for `emission[...]`."""
    out: dict[tuple[str, str], tuple[str, tuple[str, ...]]] = {}
    for svc in services_by_name.values():
        for meth, md in svc.methods.items():
            if not md.emission:
                out[(svc.name, meth)] = ("plain", ())
            elif md.capabilities is None:
                out[(svc.name, meth)] = ("any", ())
            else:
                out[(svc.name, meth)] = ("scoped", tuple(md.capabilities))
    return out


def _fn_emitting(prog) -> set[str]:
    """Least fixed point of named functions/externs whose body (transitively)
    reaches an emission extern — the `env.emitting_fns` analog. A call to
    one of these contributes the unnameable `*` to a reach."""
    emission_externs = {e.name for e in prog.externs
                        if getattr(e, "classification", "") == "emission"}
    calls: dict[str, set[str]] = {}
    for fn in prog.fn_decls:
        found: set[str] = set()

        def walk(node: object) -> None:
            if isinstance(node, ExprCall):
                rt = _route(node.callee)
                if rt:
                    found.add(rt[0])
                for a in node.args:
                    walk(a)
                return
            if dataclasses.is_dataclass(node) and not isinstance(node, type):
                for f in dataclasses.fields(node):
                    walk(getattr(node, f.name))
                return
            if isinstance(node, (list, tuple)):
                for x in node:
                    walk(x)

        for stmt in fn.body:
            walk(stmt)
        calls[fn.name] = found
    emitting = set(emission_externs)
    changed = True
    while changed:
        changed = False
        for fn, cals in list(calls.items()):
            if fn not in emitting and cals & emitting:
                emitting.add(fn)
                changed = True
    return emitting


# -------------------------------------------------- whole-Prog export (#276)
#
# G5 (teardown purity) and G8 (boundary surface) are stated over a `Prog` —
# the extern table plus the fn call graph — not over one statement (design
# docs/design/456-ambient-and-prog-export.md, slice C). The `I` row carries a
# statement's call heads and nothing about what those heads REACH, so it
# cannot feed `RevL.G5Classified.registrations` / `RevL.G8Classified.stmtSurface`.
# The `EX`/`FN`/`PG` rows below carry the reach graph itself; the oracle
# rebuilds `RevL.Lemmas.Prog` from them, and `reference_from_tsv` recomputes
# the same reach INDEPENDENTLY in Python (a small fixed point) so the diff is
# two implementations of the model's fold over one set of facts.


def _undo_callee(expr: object) -> str | None:
    """The bare name an extern `undo`/`compensate` slot's top-level call
    names, mirroring `lower._undo_callee_name` (item 440): a plain call to a
    bare name, or a bare var. Any other shape (an arrow, a field chain) is
    unresolvable and reported as `-`, which is the fail-closed reading the
    reach fold then gives it (`lookupExtern`/`lookupFn` both miss)."""
    if isinstance(expr, ExprCall) and isinstance(expr.callee, ExprVar):
        return expr.callee.name
    if isinstance(expr, ExprVar):
        return expr.name
    return None


def _fn_body_calls(body: object) -> tuple[list[str], set[str]]:
    """`(bare-name callees in order, value-position names)` for one fn body,
    over the PARSER ast. A callee is bare when `_route` gives it an empty
    chain — a `send(x)` or `wrap(x)`, the shape `RevL.Lemmas.calleesOf`
    resolves; a `store.insert(...)` field crossing is not a fn/extern call and
    is skipped. A value-position name is a bare var that is NOT the callee of
    its call — the first-class reference `_emitting_capabilities` records in
    its `passed` set, from which the `star` marker (D9) is derived."""
    calls: list[str] = []
    values: set[str] = set()

    def walk(node: object, callee_pos: bool = False) -> None:
        if isinstance(node, ExprCall):
            rt = _route(node.callee)
            if rt and rt[1] == "" and rt[0] not in calls:
                calls.append(rt[0])
            walk(node.callee, callee_pos=True)
            for a in node.args:
                walk(a)
            return
        if isinstance(node, ExprVar):
            if not callee_pos and "." not in node.name:
                values.add(node.name)
            return
        if dataclasses.is_dataclass(node) and not isinstance(node, type):
            for f in dataclasses.fields(node):
                walk(getattr(node, f.name))
            return
        if isinstance(node, (list, tuple)):
            for x in node:
                walk(x)

    for stmt in body:
        walk(stmt)
    return calls, values


def _prog_reach(externs: dict[str, tuple[str, list[str]]],
                fns: dict[str, list[str]]
                ) -> tuple[dict[str, frozenset[str]], dict[str, bool],
                           dict[str, set[str]]]:
    """The model's reach fold, recomputed in Python from the `EX`/`FN` facts.

    `externs` is `name -> (class, caps)`, `fns` is `name -> [callees]`. Returns
    `(reach_caps, reach_crosses, reach_names)`:

      * `reach_caps[n]` mirrors `RevL.Lemmas.reachCaps`: the union of
        `capsOfDecl` over every name reachable from `n` (an emission
        contributes its own name; a witnessed extern its declared scope, or
        its own name when unscoped; nothing else contributes);
      * `reach_crosses[n]` mirrors `(RevL.Lemmas.reachCls n).crosses`: some
        reachable name is `witnessed`/`emission` classified;
      * `reach_names[n]` is the transitive callee closure incl. `n`, stopping
        at externs exactly as `calleesOf` returns `[]` for one.

    Computed as a true fixed point (iterate to stability): the model uses a
    bounded `fuel = len(fns)`, which is exactly enough to reach this closure
    (a shortest path to a crossing visits each fn at most once), so the two
    agree and an under-fuelled oracle would show up as a mismatch."""
    def calleesof(n: str) -> list[str]:
        if n in externs:
            return []
        return fns.get(n, [])

    def declcaps(n: str) -> set[str]:
        if n in externs:
            cls, caps = externs[n]
            if cls == "emission":
                return {n}
            if cls == "witnessed":
                return set(caps) if caps else {n}
        return set()

    def declcrosses(n: str) -> bool:
        return n in externs and externs[n][0] in ("witnessed", "emission")

    names = set(externs) | set(fns)
    reach: dict[str, set[str]] = {n: {n} for n in names}
    changed = True
    while changed:
        changed = False
        for n in names:
            if n in externs:
                continue
            before = len(reach[n])
            for c in calleesof(n):
                reach[n] |= reach.get(c, {c})
            if len(reach[n]) != before:
                changed = True
    reach_caps: dict[str, frozenset[str]] = {}
    reach_crosses: dict[str, bool] = {}
    for n in names:
        caps: set[str] = set()
        crosses = False
        for m in reach[n]:
            caps |= declcaps(m)
            crosses = crosses or declcrosses(m)
        reach_caps[n] = frozenset(caps)
        reach_crosses[n] = crosses
    return reach_caps, reach_crosses, reach


def _star_tainted(fns_star: dict[str, bool], reach_names: dict[str, set[str]]
                  ) -> set[str]:
    """Names that reach a first-class-dispatch (`star`) fn (D9). A `star` fn
    hands an emitting callable to a dispatcher, so what it runs is not
    statically boundable and sits OUTSIDE the Lean model; every row touching
    such a name prints `n/a` on both sides. Transitive: a fn calling a star fn
    is tainted too."""
    star = {n for n, s in fns_star.items() if s}
    return {n for n, ns in reach_names.items() if ns & star}


def _resolve_emission(root: str, chain: str, requires: dict, handles: dict,
                      psvc: dict, aliases: dict | None = None
                      ) -> tuple[str, str] | None:
    """(svc, method) a call reaches, or None when not a boundary-typed
    receiver: a require binding (single-hop chain), a spawn handle
    (`w.task.run` => child's provide key `task` -> service, method `run`), or
    a local aliasing one of that handle's provisions.

    The ALIAS arm is the same crossing one binding later: `let t = w.task`
    then `t.run(p)` reads the same provision `w.task.run(p)` does, so the
    receiver is boundary-typed and the marker rule applies. Without it the
    model saw a plain local call and reported nothing, which is the shape the
    checker used to miss too (`g4_unmarked_alias_emission.rvl`)."""
    if root in requires:
        return requires[root], chain
    if root in handles and "." in chain:
        head, _, rest = chain.partition(".")
        if not rest:
            return None
        svc = psvc.get(handles[root], {}).get(head)
        return (svc, rest) if svc else None
    if aliases and root in aliases and chain and "." not in chain:
        comp, key = aliases[root]
        svc = psvc.get(comp, {}).get(key)
        return (svc, chain) if svc else None
    return None


def collect_provision_aliases(node, handles: dict, aliases: dict) -> None:
    """Fill `aliases` (var -> (component, provide key)) from `let t = w.task`
    bindings, where `w` is a spawn handle. Runs after `collect_spawns`, whose
    `handles` it reads."""
    if node is None or isinstance(node, (str, int, float, bool)):
        return
    if type(node).__name__ == "LetStmt":
        value = getattr(node, "value", None)
        name = getattr(node, "name", None)
        if isinstance(value, ExprField) and isinstance(value.target, ExprVar) \
                and value.target.name in handles and isinstance(name, str):
            aliases[name] = (handles[value.target.name], value.name)
        elif isinstance(value, ExprVar) and value.name in aliases \
                and isinstance(name, str):
            aliases[name] = aliases[value.name]  # a second hop
    if dataclasses.is_dataclass(node) and not isinstance(node, type):
        for f in dataclasses.fields(node):
            collect_provision_aliases(getattr(node, f.name), handles, aliases)
        return
    if isinstance(node, (list, tuple)):
        for x in node:
            collect_provision_aliases(x, handles, aliases)


def _arg_provision(arg, handles: dict, aliases: dict) -> "tuple[str, str] | None":
    """The (component, provide key) a call ARGUMENT reads, or None: a direct
    spawn-handle provision (`w.task`) or a local aliasing one (`let t = w.task;
    f(t, ...)`). The same resolution `collect_provision_aliases` records for a
    `let` binding, applied to an argument expression."""
    if isinstance(arg, ExprField) and isinstance(arg.target, ExprVar) \
            and arg.target.name in handles:
        return (handles[arg.target.name], arg.name)
    if isinstance(arg, ExprVar) and arg.name in aliases:
        return aliases[arg.name]
    return None


def collect_arrow_param_aliases(body, handles: dict, aliases: dict,
                                services: dict) -> None:
    """Follow a spawn-handle provision across an ARROW PARAMETER binding at the
    application site, the sibling of `collect_provision_aliases` one indirection
    further (GHSA-wg4v-r47x-52p2 residual, examples/rejections/
    g4_arrow_param_emission.rvl).

    `let f = (t: Task, s: Str) => t.run(s)` then `f(w.task, prompt)` reaches the
    SAME crossing `w.task.run` is: the parameter's declared service type is the
    provenance, so the application binds `w.task` into `t` and the arrow body's
    `t.run` reads the provision. The checker follows exactly this
    (`lower._check_arrow_param_crossings`); the exporter records the parameter
    as a provision alias so `_resolve_emission` resolves the body crossing and
    the model's G4 marker rule judges it as the direct/`let`-aliased spellings
    are judged.

    Two passes over the component body: collect `let`-bound arrows (var ->
    arrow), then, at each application of one, alias every service-typed
    parameter that receives a provision argument. Runs after
    `collect_provision_aliases`, whose `aliases` it reads (an aliased argument)
    and extends (the parameter)."""
    arrows: dict[str, object] = {}

    def collect_arrows(node) -> None:
        if node is None or isinstance(node, (str, int, float, bool)):
            return
        if type(node).__name__ == "LetStmt" \
                and isinstance(getattr(node, "value", None), ExprArrow) \
                and isinstance(getattr(node, "name", None), str):
            arrows[node.name] = node.value
        if dataclasses.is_dataclass(node) and not isinstance(node, type):
            for f in dataclasses.fields(node):
                collect_arrows(getattr(node, f.name))
            return
        if isinstance(node, (list, tuple)):
            for x in node:
                collect_arrows(x)

    def apply_bindings(node) -> None:
        if node is None or isinstance(node, (str, int, float, bool)):
            return
        if isinstance(node, ExprCall) and isinstance(node.callee, ExprVar) \
                and node.callee.name in arrows:
            arrow = arrows[node.callee.name]
            params = list(getattr(arrow, "params", None) or [])
            ptypes = list(getattr(arrow, "param_types", None)
                          or getattr(arrow, "written_param_types", None) or [])
            ptypes += [None] * (len(params) - len(ptypes))
            for param, ptype, arg in zip(params, ptypes, node.args):
                head, _ = parse_type(ptype or "")
                if head not in services:
                    continue
                prov = _arg_provision(arg, handles, aliases)
                if prov is not None:
                    aliases[param] = prov
        if dataclasses.is_dataclass(node) and not isinstance(node, type):
            for f in dataclasses.fields(node):
                apply_bindings(getattr(node, f.name))
            return
        if isinstance(node, (list, tuple)):
            for x in node:
                apply_bindings(x)

    for stmt in body:
        collect_arrows(stmt)
    for stmt in body:
        apply_bindings(stmt)


def walk_reach(node, out: set[str], region: str, requires: dict, handles: dict,
               psvc: dict, bounds: dict, em_set: set, emitting: set,
               aliases: dict | None = None) -> None:
    """Collect the canonical emission caps `node` crosses.

    `region` is "emit-step" (count only MARKED crossings — the attenuation
    surface, like `_collect_emit_caps_pairs`) or "all" (also count any
    resolved emission call — a provide method's reach for the bound, like
    `_method_emissions.walk`). A spawn-handle emission is the unnameable
    `*`; an emission extern / emitting-fn call contributes `*` too."""
    if node is None or isinstance(node, (str, int, float, bool)):
        return
    if isinstance(node, (EmitStmt, EmitExpr)) and not isinstance(node, type):
        if dataclasses.is_dataclass(node):
            for f in dataclasses.fields(node):
                walk_reach(getattr(node, f.name), out, "all", requires,
                           handles, psvc, bounds, em_set, emitting, aliases)
        return
    if isinstance(node, ExprCall):
        rt = _route(node.callee)
        if rt:
            root, chain = rt
            res = _resolve_emission(root, chain, requires, handles, psvc,
                                    aliases)
            if res is not None and region == "all":
                svc, meth = res
                if (svc, meth) in em_set:
                    if root in handles or (aliases and root in aliases):
                        out.add("*")
                    else:
                        mode, entries = bounds[(svc, meth)]
                        if mode == "any":
                            out.add(root)
                        else:
                            for e in entries:
                                out.add(_canon_cap(root, e))
            elif res is None and region == "all" and root in emitting:
                out.add("*")
        for a in node.args:
            walk_reach(a, out, region, requires, handles, psvc, bounds,
                       em_set, emitting, aliases)
        return
    if dataclasses.is_dataclass(node) and not isinstance(node, type):
        for f in dataclasses.fields(node):
            walk_reach(getattr(node, f.name), out, region, requires, handles,
                       psvc, bounds, em_set, emitting, aliases)
        return
    if isinstance(node, (list, tuple)):
        for x in node:
            walk_reach(x, out, region, requires, handles, psvc, bounds,
                       em_set, emitting, aliases)


def collect_spawns(node, handles: dict, rows: list) -> None:
    """Fill `handles` (var -> spawned component) and `rows` (`(bind, comp)`
    spawn payloads) from spawn acquisitions."""
    if node is None or isinstance(node, (str, int, float, bool)):
        return
    if type(node).__name__ in ("LetEffect", "EffectStmt"):
        acq = getattr(node, "acquire", None)
        if isinstance(acq, SpawnExpr):
            bind = getattr(node, "bind", None)
            if bind is not None:
                handles[bind] = acq.component
                rows.append((bind, acq.component))
        for f in dataclasses.fields(node):
            collect_spawns(getattr(node, f.name), handles, rows)
        return
    if dataclasses.is_dataclass(node) and not isinstance(node, type):
        for f in dataclasses.fields(node):
            collect_spawns(getattr(node, f.name), handles, rows)
        return
    if isinstance(node, (list, tuple)):
        for x in node:
            collect_spawns(x, handles, rows)


def walk_calls(node: object, out: list[tuple[str, str, str]], ctx: str) -> None:
    """Collect (receiver-root, method, marker-context) call facts.

    ctx is 'emit' under an emit marker, 'plain' everywhere else — including
    under `effect ... undo ...`: the g4_unmarked_emission fixture shows the
    checker refuses an emission call whose pairing is an inverse, because a
    boundary crossing cannot be reverted by pairing. Only `emit` legalizes
    an emission, and (two-sided) `emit` around a non-emission method is
    itself a refusal ('emission not declared')."""
    if node is None or isinstance(node, (str, int, float, bool)):
        return
    if isinstance(node, (EmitStmt, EmitExpr)):
        if dataclasses.is_dataclass(node) and not isinstance(node, type):
            for f in dataclasses.fields(node):
                walk_calls(getattr(node, f.name), out, "emit")
        return
    if isinstance(node, ExprCall):
        route = _route(node.callee)
        if route is not None:
            out.append((*route, ctx))
        for a in node.args:
            walk_calls(a, out, ctx)
        return
    if dataclasses.is_dataclass(node) and not isinstance(node, type):
        for f in dataclasses.fields(node):
            walk_calls(getattr(node, f.name), out, ctx)
        return
    if isinstance(node, (list, tuple)):
        for x in node:
            walk_calls(x, out, ctx)


def _spawn_templates(prog) -> set[str]:
    """Every component named by a `spawn` anywhere in the program — the
    linker's `templates` set (`lower._link`). A spawn target is a RUNTIME
    instance, not a static composition member: it is excluded from the
    G2/G3 table and from `loadOrder`, because each instance is created in
    its own fresh local realm. Without this the model would see two
    per-tenant worker templates as one G2 provision conflict, which is not
    what revl decides."""
    found: set[str] = set()

    def walk(node: object) -> None:
        if isinstance(node, SpawnExpr):
            found.add(node.component)
        if dataclasses.is_dataclass(node) and not isinstance(node, type):
            for f in dataclasses.fields(node):
                walk(getattr(node, f.name))
            return
        if isinstance(node, (list, tuple)):
            for x in node:
                walk(x)

    for c in prog.components:
        walk(c.body)
    for fn in prog.fn_decls:
        walk(fn.body)
    return found


def _isolate_map(comp) -> dict[str, str]:
    """The component's `isolate <key> in realm(<r>)` clauses — `lower._realm`'s
    table. A key with no clause stays in the shared realm, which is
    `RevL.Manifest.sharedRealm` (the empty string) on the model's side.

    `isolate <key> in realms(...)` (the multi-realm ROUTE, item 162) is a
    different construct and is NOT folded in here: a routed key resolves
    per-realm at each leg rather than pinning one realm. No corpus file uses
    one today; if one appears its route legs are simply not modeled, and the
    key falls back to the shared realm."""
    out: dict[str, str] = {}
    for stmt in comp.body:
        if isinstance(stmt, IsolateStmt):
            out[stmt.key] = stmt.realm
    return out


# --------------------------------------------------------- host acquisition
#
# A HOST acquire verb (`Pool.open`, `Map.new`, `Stream.source`) opens a host
# resource whose release is a SEPARATE verb, so it is legal ONLY as the
# acquisition of an `effect <acquire> undo <release>` bracket — the one
# construct that registers the release with the activation's teardown
# accumulator (`typecheck._HOST_ACQUIRE_VERBS`,
# `lower._refuse_unbracketed_host_acquire`). Anywhere else — a plain `let`, an
# `emit` expression, a teardown slot, or a `fn` body a component reaches — the
# resource is acquired irreversibly (G4, category `acquire`).
#
# This is the SAME G4 guarantee the marker rule serves, over a different fact:
# not "is this crossing marked" but "does this host acquisition sit where its
# release is registered". The exporter carries the fact and its POSITION; the
# model owns the verb table and states the rule (`Oracle.hostAcquireOK`),
# exactly as the exporter carries a call's `emit` context and the model owns
# the marker rule (issue 334).


def _host_dotted(callee: object) -> str | None:
    """The dotted verb of a `Type.method(..)` call — a host-object family
    surface — or None. Only a CAPITALISED root is a host family constructor
    (`Pool.open`, `Map.new`); a lower-cased receiver (`pool.close`) is a
    method on a bound local, never a host acquisition."""
    rt = _route(callee)
    if rt and rt[1] and rt[0][:1].isupper():
        return f"{rt[0]}.{rt[1]}"
    return None


def _host_calls(node, pos: str, out: list[tuple[str, str]]) -> None:
    """Collect (verb, position) for every host-family call in `node`.

    Position is `bracket` when the verb is the ROOT of an `effect`'s
    acquisition expression (the only legal site), and otherwise the site the
    checker names: `undo` for a teardown slot, `emit` for an emit expression,
    `plain` for everything else. Identity, not shape: only the bracket's own
    root call is `bracket`, so `effect wrap(Pool.open(..)) undo ..` — a pool
    the inverse never names — is `plain`, refused exactly as the checker
    refuses it."""
    if node is None or isinstance(node, (str, int, float, bool)):
        return
    if isinstance(node, (EmitStmt, EmitExpr)):
        for f in dataclasses.fields(node):
            _host_calls(getattr(node, f.name), "emit", out)
        return
    if type(node).__name__ in ("EffectStmt", "LetEffect"):
        acq = getattr(node, "acquire", None)
        undo = getattr(node, "undo", None)
        if isinstance(acq, ExprCall):
            verb = _host_dotted(acq.callee)
            if verb is not None:
                out.append((verb, "bracket"))
            for a in acq.args:
                _host_calls(a, "plain", out)
        else:
            _host_calls(acq, "plain", out)
        _host_calls(undo, "undo", out)
        return
    if isinstance(node, ExprCall):
        verb = _host_dotted(node.callee)
        if verb is not None:
            out.append((verb, pos))
        for a in node.args:
            _host_calls(a, pos, out)
        return
    if dataclasses.is_dataclass(node) and not isinstance(node, type):
        for f in dataclasses.fields(node):
            _host_calls(getattr(node, f.name), pos, out)
        return
    if isinstance(node, (list, tuple)):
        for x in node:
            _host_calls(x, pos, out)


def _host_calls_flat(node, out: list[tuple[str, str]]) -> None:
    """Every host-family call in `node` as position `fn`, IGNORING effect
    structure: a reached `fn` body is refused for any host acquisition
    regardless of a bracket, because it has no teardown accumulator to hold
    the release (`lower._scan`)."""
    if node is None or isinstance(node, (str, int, float, bool)):
        return
    if isinstance(node, ExprCall):
        verb = _host_dotted(node.callee)
        if verb is not None:
            out.append((verb, "fn"))
        for a in node.args:
            _host_calls_flat(a, out)
        return
    if dataclasses.is_dataclass(node) and not isinstance(node, type):
        for f in dataclasses.fields(node):
            _host_calls_flat(getattr(node, f.name), out)
        return
    if isinstance(node, (list, tuple)):
        for x in node:
            _host_calls_flat(x, out)


def _named_call_roots(node, out: set[str]) -> None:
    """The receiver roots of every call in `node` — the fn-call graph seed."""
    if node is None or isinstance(node, (str, int, float, bool)):
        return
    if isinstance(node, ExprCall):
        rt = _route(node.callee)
        if rt:
            out.add(rt[0])
        for a in node.args:
            _named_call_roots(a, out)
        return
    if dataclasses.is_dataclass(node) and not isinstance(node, type):
        for f in dataclasses.fields(node):
            _named_call_roots(getattr(node, f.name), out)
        return
    if isinstance(node, (list, tuple)):
        for x in node:
            _named_call_roots(x, out)


def _reached_fns(comp, fns: dict) -> set[str]:
    """The named functions a component body reaches, transitively — the
    checker's `_scan` reach (`lower.py`). A host acquisition in one of these
    is refused because residue is defined against the ACTIVATION whose
    teardown the helper contributes nothing to; a `pub fn` no component reaches
    is a library entry point revl promises nothing about, and is not here."""
    frontier: set[str] = set()
    for stmt in comp.body:
        _named_call_roots(stmt, frontier)
    frontier &= set(fns)
    reached: set[str] = set()
    while frontier:
        name = frontier.pop()
        if name in reached:
            continue
        reached.add(name)
        callees: set[str] = set()
        _named_call_roots(fns[name].body, callees)
        frontier |= (callees & set(fns)) - reached
    return reached


def _host_acquire_facts(comp, fns: dict) -> list[tuple[str, str]]:
    """Every host-family acquisition a component's reachable code names, with
    its position. The component's own statements keep their structural
    position (a bracket root is legal); a reached `fn` body has no teardown
    accumulator at all, so every host acquisition in one is `fn` — illegal
    wherever it sits, matching the checker's blanket refusal in a reached fn."""
    out: list[tuple[str, str]] = []
    for stmt in comp.body:
        _host_calls(stmt, "plain", out)
    for name in sorted(_reached_fns(comp, fns)):
        for stmt in fns[name].body:
            _host_calls_flat(stmt, out)
    return out


def export() -> tuple[list[str], dict[str, dict], dict[str, object]]:
    """Parse the corpus; return (tsv rows, per-file facts, census)."""
    tsv: list[str] = []
    file_facts: dict[str, dict] = {}
    caps_seen: set[str] = set()
    refusals: dict[str, str] = {}
    componentless: list[str] = []
    files = comps = stmts = 0
    for path in corpus_files():
        files += 1
        rel = str(path.relative_to(REPO))
        try:
            prog = Parser(path.read_text(encoding="utf-8"), str(path)).parse()
        except RevlError as e:
            # A parse-time REFUSAL is a VERDICT, not a skip (item 418 step 7):
            # revl rejecting the file IS the answer, and dropping it hid
            # `g4_missing_undo.rvl` (literally the shape G4 forbids) and both
            # G6 fixtures from every count in this harness.
            refusals[rel] = classify(e).get("code") or "UNCODED"
            tsv.append("\t".join(["X", rel, refusals[rel]]))
            continue
        if not prog.components:
            # Parsed, but there is no composition to model. Recorded by name
            # (item 418 step 7) rather than dropped: the file still reaches
            # the checker-alignment report, where its refusal code — G1 for
            # `g1_template_undeclared.rvl`, and five `g4_extern_*` fixtures —
            # is named as OUTSIDE the model's fragment instead of vanishing.
            componentless.append(rel)
            tsv.append("\t".join(["N", rel]))
            continue
        svc_objs = {svc.name: svc for svc in prog.services}
        services = {n: {m: md.emission for m, md in s.methods.items()}
                    for n, s in svc_objs.items()}
        bounds = _bound_index(svc_objs)
        em_set = {k for k, (mode, _e) in bounds.items() if mode != "plain"}
        # service-method emission bound facts: B (mode) + Q (scoped entries).
        for (svc, meth), (mode, entries) in sorted(bounds.items()):
            tsv.append("\t".join(["B", rel, svc, meth, mode]))
            for e in sorted(entries):
                tsv.append("\t".join(["Q", rel, svc, meth, e]))
        emitting = _fn_emitting(prog)
        templates = _spawn_templates(prog)
        fns_by_name = {fn.name: fn for fn in prog.fn_decls}
        # provide-key -> service, file-wide (children resolve handle receivers).
        psvc = {c.name: {k: s for k, s, _ln in c.provides} for c in prog.components}

        # whole-Prog export (#276): the extern table (EX), the fn call graph
        # (FN, with the first-class-dispatch `star` marker), and the fuel the
        # oracle folds under (PG). File-wide, once, so the oracle rebuilds one
        # `RevL.Lemmas.Prog` per file. `fuel = len(fns)`: a shortest reach path
        # to a crossing visits each fn at most once, so that many unrollings
        # reach the fixed point (D8). Parsed but UNUSED until the oracle's
        # deciders read them (the #268 land-the-export-first discipline).
        ex_norm: dict[str, tuple[str, list[str]]] = {
            e.name: (e.classification, list(e.capabilities or ()))
            for e in prog.externs}
        fn_calls: dict[str, list[str]] = {}
        fn_values: dict[str, set[str]] = {}
        for fn in prog.fn_decls:
            fn_calls[fn.name], fn_values[fn.name] = _fn_body_calls(fn.body)
        _rc, reach_crosses, _rn = _prog_reach(ex_norm, fn_calls)
        crossing_names = {n for n, x in reach_crosses.items() if x}
        star_fns = {name: bool(vals & crossing_names)
                    for name, vals in fn_values.items()}
        tsv.append("\t".join(["PG", rel, str(len(prog.fn_decls))]))
        for e in prog.externs:
            tsv.append("\t".join([
                "EX", rel, e.name, e.classification,
                _undo_callee(e.undo) or "-", _undo_callee(e.compensate) or "-",
                ",".join(e.capabilities or ())]))
        for fn in prog.fn_decls:
            tsv.append("\t".join([
                "FN", rel, fn.name, ",".join(fn_calls[fn.name]),
                "star" if star_fns.get(fn.name) else "plain"]))

        ff: dict = {"components": {}}
        for c in prog.components:
            requires = [(local, svc) for local, svc, _line in c.requires]
            provides = [key for key, _svc, _line in c.provides]
            require_map = dict(requires)
            realms = _isolate_map(c)
            comps += 1
            # M carries the REALM map and the template flag, the two facts
            # `RevL.Manifest` needs to state revl's actual G2/G3 rule: the
            # unit is the `(key, realm)` SLOT, and a spawn target is not a
            # member of the static composition at all.
            tsv.append(
                "\t".join(["M", rel, c.name,
                           ",".join(r for r, _s in requires),
                           ",".join(provides),
                           ",".join(f"{k}={v}" for k, v in sorted(realms.items())),
                           "template" if c.name in templates else "member"])
            )
            for local, svc in requires:
                tsv.append("\t".join(["R", rel, c.name, local, svc]))
            for key, svc, _ln in c.provides:
                tsv.append("\t".join(["C", rel, c.name, key, svc]))

            # require-held capability facts (K): the boundaries a requires
            # binding hands this component — the structured valuations of the
            # service's emission declarations (the held side of attenuation).
            krows: list[tuple[str, str]] = []
            for local, svc in requires:
                em = [(mode, ents) for (s, _m), (mode, ents) in bounds.items()
                      if s == svc and mode != "plain"]
                if not em:
                    krows.append((local, local))
                    continue
                for mode, ents in em:
                    if mode == "any":
                        krows.append((local, local))
                    else:
                        for e in ents:
                            krows.append((local, _canon_cap(local, e)))
            for local, cap in sorted(krows):
                caps_seen.add(cap)
                tsv.append("\t".join(["K", rel, c.name, local, cap]))

            # spawn facts. S = attenuation edge, and ONLY an activation-body
            # spawn is one: a provide-method spawn is already bounded by that
            # method's `emission[...]` clause, so the activation body is the
            # hole attenuation closes (lower._activation_spawn_sites). H = a
            # spawn binding, collected EVERYWHERE, because a `w.task.run(...)`
            # receiver must resolve wherever the handle was bound.
            handles: dict[str, str] = {}
            act_spawns: list[tuple[str, str]] = []
            for stmt in c.body:
                if isinstance(stmt, ProvideStmt):
                    collect_spawns(stmt, handles, [])
                else:
                    collect_spawns(stmt, handles, act_spawns)
            for _bind, child in sorted(dict(act_spawns).items()):
                tsv.append("\t".join(["S", rel, c.name, child]))
            for bind, child in sorted(handles.items()):
                tsv.append("\t".join(["H", rel, c.name, bind, child]))
            # ... and the locals that ALIAS one of those handles' provisions
            # (`let t = w.task`). Collected everywhere `handles` is, and for
            # the same reason: the receiver must resolve wherever it was bound.
            # No TSV row: an alias is a spelling of the H binding it resolves
            # to, and the U rows it produces already carry the resolved
            # (service, method), so both sides read the same crossing.
            aliases: dict[str, tuple[str, str]] = {}
            for stmt in c.body:
                collect_provision_aliases(stmt, handles, aliases)
            # ... and the SERVICE-TYPED arrow parameters an application binds a
            # provision into (`let f = (t: Task) => t.run(p); f(w.task, p)`),
            # one indirection past the `let` alias above (GHSA-wg4v-r47x-52p2).
            collect_arrow_param_aliases(c.body, handles, aliases, services)

            # activation emit-step surface (A): the component's OWN marked
            # crossings — the base of the attenuation reach.
            act_reach: set[str] = set()
            for stmt in c.body:
                walk_reach(stmt, act_reach, "emit-step", require_map, handles,
                           psvc, bounds, em_set, emitting, aliases)
            caps_seen.update(act_reach)
            for cap in sorted(act_reach):
                tsv.append("\t".join(["A", rel, c.name, cap]))

            # host acquisition facts (HA): each host-family acquisition the
            # component's reachable code names, with the POSITION that decides
            # its legality. The model owns the verb table and states the rule
            # (issue 334); the exporter carries where each acquisition sits.
            for verb, position in _host_acquire_facts(c, fns_by_name):
                tsv.append("\t".join(["HA", rel, c.name, verb, position]))

            # provide-method reach (F): emission caps a method's body crosses
            # (all-call) — the bound check's left side and, with A, the
            # component surface for attenuation.
            for stmt in c.body:
                if isinstance(stmt, ProvideStmt):
                    svc = psvc.get(c.name, {}).get(stmt.key)
                    if svc is None:
                        continue
                    for pm in stmt.methods:
                        reach: set[str] = set()
                        for inner in pm.body:
                            walk_reach(inner, reach, "all", require_map, handles,
                                       psvc, bounds, em_set, emitting, aliases)
                        caps_seen.update(reach)
                        for cap in sorted(reach):
                            tsv.append("\t".join(
                                ["F", rel, c.name, stmt.key, svc, pm.name, cap]))

            calls: list[tuple[str, str, str, str]] = []
            kinds: list[str] = []
            terms: list[tuple[int, str, list[str], list[str]]] = []

            def _term_heads(node: object, ctx: str = "plain") -> list[str]:
                found: list[tuple[str, str, str]] = []
                walk_calls(node, found, ctx)
                return [f"{root}.{chain}" if chain else root
                        for root, chain, _ in found]

            def classify_stmt(stmt: object) -> bool:
                """Classify one statement; True when it holds a G4-shaped
                violation: marker-presence != interface-declared emission."""
                nonlocal stmts
                stmts += 1
                if isinstance(stmt, (EffectStmt, LetEffect)):
                    kinds.append("effect")
                    primary = _term_heads(getattr(stmt, "acquire"))
                    inverse = _term_heads(getattr(stmt, "undo"))
                    terms.append((len(terms), "effect", primary, inverse))
                    # effect-form calls are STILL plain context: an emission
                    # call whose pairing is an inverse is refused (the
                    # g4_unmarked_emission fixture) — only `emit` marks a
                    # crossing. So walk them into the record, not a discard.
                    local_calls: list[tuple[str, str, str]] = []
                    walk_calls(stmt, local_calls, "plain")
                    return _record(local_calls)
                if isinstance(stmt, EmitStmt):
                    kinds.append("emit")
                    terms.append((len(terms), "emit", _term_heads(stmt.expr, "emit"), []))
                    local_calls = []
                    walk_calls(stmt, local_calls, "emit")
                    return _record(local_calls)
                local_calls: list[tuple[str, str, str]] = []
                kind = "pure"
                if type(stmt).__name__ == "CallStmt":
                    for a in getattr(stmt, "args", []):
                        walk_calls(a, local_calls, "plain")
                    root, meth = getattr(stmt, "key"), getattr(stmt, "method")
                    local_calls.append((root, meth, "plain"))
                else:
                    walk_calls(stmt, local_calls, "plain")
                if type(stmt).__name__ not in ("CallStmt", "LetStmt"):
                    kind = "raw"
                terms.append((len(terms), kind, _term_heads(stmt), []))
                return _record(local_calls)

            def _record(local_calls: list[tuple[str, str, str]]) -> bool:
                saw_raw = False
                for root, chain, ctx in local_calls:
                    res = _resolve_emission(root, chain, require_map, handles,
                                            psvc, aliases)
                    if res is None:
                        continue  # host/local/provide receiver: not a crossing
                    svc, meth = res
                    if meth not in services.get(svc, {}):
                        continue  # unknown method: the checker's business
                    em = services[svc][meth]
                    bad = (ctx == "emit") != em
                    calls.append((root, svc, meth, ctx))
                    tsv.append(
                        "\t".join(["U", rel, c.name, ctx, root, svc, meth]))
                    if bad:
                        saw_raw = True
                return saw_raw

            # Activation body, then each provide method's body. The two passes
            # are DISJOINT: `classify_stmt` recurses generically, so letting the
            # first pass descend into a `provide` would classify every method
            # statement twice — a doubled census and a duplicated U row for
            # every provide-body crossing.
            for stmt in c.body:
                if not isinstance(stmt, ProvideStmt):
                    classify_stmt(stmt)
            for stmt in c.body:
                if isinstance(stmt, ProvideStmt):
                    for pm in stmt.methods:
                        for inner in pm.body:
                            classify_stmt(inner)
            tsv.extend(f"T\t{rel}\t{c.name}\t{k}" for k in kinds)
            for idx, kind, heads, inverse in terms:
                tsv.append("\t".join(["I", rel, c.name, str(idx), kind,
                                       ",".join(heads), ",".join(inverse)]))
            ff["components"][c.name] = {"calls": calls, "kinds": kinds}
        file_facts[rel] = ff
    # Z/Y decomposition rows go FIRST so the oracle can build its table in
    # one pass; the harness refuses a capability the checker cannot re-read.
    # The G7 scenario corpus is not extracted from `.rvl` text: a teardown
    # disposition is a property of a RUN, not of a manifest, so the facts are
    # the shape of one activation's stack and the verdict it unwound under
    # (`teardown_scenario_rows`). Both sides read them from this same TSV.
    return (cap_decomposition_rows(caps_seen) + tsv + teardown_scenario_rows()
            + recovery_scenario_rows(),
            file_facts, {
        "files": files, "components": comps, "statements": stmts,
        "refusals": refusals, "componentless": componentless,
    })


# ------------------------------------------------- G7 teardown dispositions

#: The registration seams a scenario can use, as (seam, model kind). The
#: model has ONE per-activation LIFO stack and no seam distinction; the
#: reference has two, and they are different code paths in
#: `backends/python/runtime.py`:
#:
#:  * `body` — the activation body yields the disposer, so cordis holds it
#:    and unwinds it LIFO. A `bracket` entry exists only here: an emitted
#:    bracket is a bare `lambda: <undo>` with no entry object.
#:  * `method` — a provide-method registered it (`transactional_method` /
#:    `compensation_method`), so it is parked on `_deferred_transactional` /
#:    `_deferred_compensations` and disposed by `drain` itself, newest-first
#:    (item 369's `reversed`). That loop is revl's OWN LIFO, not cordis's,
#:    which is why the seam is in the corpus at all.
_G7_SHAPES: tuple = (
    ("body", "bracket"),
    ("body", "transactional"),
    ("body", "compensation"),
    ("method", "transactional"),
    ("method", "compensation"),
)

_G7_CODE = {("body", "bracket"): "b", ("body", "transactional"): "t",
            ("body", "compensation"): "c",
            ("method", "transactional"): "T",
            ("method", "compensation"): "C"}

_G7_VERDICTS = ("commit", "abort", "halted")

#: Longest stack the scenario corpus enumerates. Three is the shortest
#: length at which the Phase-1/Phase-2 split, the LIFO order WITHIN a phase
#: and a mixed-seam stack are all observable at once.
_G7_DEPTH = 3


def _g7_stacks() -> list:
    """Every registration sequence up to `_G7_DEPTH`, body seams first.

    Enumerated, not hand-picked: an oracle row over cases someone chose is
    an oracle row over the cases they thought of. The body-before-method
    constraint is temporal, not cosmetic — a provide method runs AFTER its
    component activated, so a method-registered entry is always NEWER than
    every activation-body one, and a stack that interleaves them is a run
    that cannot happen.
    """
    body = [s for s in _G7_SHAPES if s[0] == "body"]
    method = [s for s in _G7_SHAPES if s[0] == "method"]
    out: list = []
    for total in range(1, _G7_DEPTH + 1):
        for nbody in range(total + 1):
            for bseq in itertools.product(body, repeat=nbody):
                for mseq in itertools.product(method, repeat=total - nbody):
                    out.append(list(bseq) + list(mseq))
    return out


def teardown_scenarios() -> list:
    """The G7 scenario corpus: `(scenario id, stack, verdict)` triples."""
    out = []
    for stack in _g7_stacks():
        code = "".join(_G7_CODE[s] for s in stack)
        for verdict in _G7_VERDICTS:
            out.append((f"g7/{verdict}/{code}", stack, verdict))
    return out


def teardown_scenario_rows() -> list:
    """The G7 fact rows: the stack shape and the verdict, nothing decided."""
    rows: list = []
    for scen, stack, verdict in teardown_scenarios():
        for i, (seam, kind) in enumerate(stack):
            rows.append(f"E\t{scen}\te{i}\t{kind}\t{seam}")
        rows.append(f"J\t{scen}\t{verdict}")
    return rows


class _G7Ctx:
    """The minimum a `Frame` reads off its context on a run with no WAL —
    the shape `tests/test_estop_443.py` drives it with. No timeline, so
    every entry carries `seq is None`, which is what a plain `revl run`
    really has."""


def _g7_inverse(label: str, ran: list):
    """One author inverse, named after its entry.

    It calls a CLOSURE variable, so its code object loads no globals and no
    attributes — which is what makes `runtime._named_call_method` /
    `_inverse_label` read the label back off `__name__` / `_revl_method`
    instead of off some incidental bytecode name. `_revl_method` is the
    same field `Frame.acquire` stamps on a bracket inverse, and it is the
    only way a bracket (which has no entry object) can be NAMED on the
    E-Stop inventory."""
    def _undo(*_args, **_kwargs):
        ran(label)
    _undo.__name__ = label
    _undo._revl_method = label
    return _undo


def teardown_observation(stack: list, verdict: str) -> tuple:
    """Drive `backends/python/runtime.py` over one scenario and report what
    the REFERENCE did: the labels whose inverse ran (in the order they
    ran), the labels it discharged, and the labels it stranded.

    Nothing here decides anything. The dispositions are read off the
    reference's own state — `_Transactional.discharged` / the E-Stop
    inventory `runtime.estop_residue()` builds — and the replay order is
    observed by the inverses themselves as they run.

    The teardown is driven exactly as the emitted body drives it:

      * `drain` is yielded LAST, so it is disposed FIRST — it settles the
        commit bit and disposes the method-registered entries;
      * the activation-body disposers then unwind newest-first, which is
        cordis's LIFO and the one part of the walk revl does not own (the
        harness stands in for cordis here, and says so);
      * `begin` is yielded FIRST, so it is disposed LAST — it is the
        post-unwind hook that drains Phase 2.

    An `abort` is the session-level flavour (`Frame.abort()` then `drain`),
    which is the only one a method-registered entry can reach: a mid-body
    raise never yields `drain`, so there are no method entries yet.
    """
    _g7_reset()
    ran: list = []
    frame = _rt.Frame(_G7Ctx(), "G7Probe")
    disposers: list = []          # the cordis disposer stack, in yield order
    entries: list = []            # (label, entry) for the ones with an object
    for i, (seam, kind) in enumerate(stack):
        label = f"e{i}"
        undo = _g7_inverse(label, ran.append)
        if seam == "body" and kind == "bracket":
            disposers.append(frame._guard(undo))
        elif seam == "body" and kind == "transactional":
            entry = frame.transactional(undo, {"witness": label})
            entries.append((label, entry))
            disposers.append(frame._guard(entry))
        elif seam == "body" and kind == "compensation":
            entry = frame.compensation(undo)
            entries.append((label, entry))
            disposers.append(frame._guard(entry))
        elif seam == "method" and kind == "transactional":
            entries.append((label, frame.transactional_method(undo, {"witness": label})))
        elif seam == "method" and kind == "compensation":
            entries.append((label, frame.compensation_method(undo)))
        else:  # pragma: no cover — the shape table is closed
            raise SystemExit(f"differential oracle: unknown G7 seam {seam}/{kind}")

    if verdict == "halted":
        _rt.estop("differential oracle scenario", operator="oracle")
    elif verdict == "abort":
        frame.abort()
    frame.drain()
    for disposer in reversed(disposers):
        disposer()
    frame.begin()

    discharged = sorted(l for l, e in entries if getattr(e, "discharged", False))
    stranded = sorted(r.get("method") for r in _rt.estop_residue()
                      if r.get("method") is not None)
    _g7_reset()
    return list(ran), discharged, stranded


def _g7_reset() -> None:
    """No halt and no frame leaks between scenarios. The E-Stop is
    process-global BY DESIGN (a halt that stopped one activation would not
    be a halt), so the live-frame registry has to be reset too or one
    scenario's frames land on the next scenario's inventory —
    `tests/test_estop_443.py` keeps the same discipline."""
    _rt.clear_estop()
    _rt.arm_estop_latch(None)
    _rt._LIVE_FRAMES.clear()


def teardown_coverage(observed: dict) -> list[str]:
    """The non-vacuity ratchet for the G7 row (roadmap item 429's lesson).

    An oracle row that has never been seen to fail is not evidence, and the
    cheapest way for a row to never fail is to agree over a shape the
    corpus does not reach. The formal-layer audit found exactly that: the
    capability-ceiling half of the `W` row agreed VACUOUSLY, because no
    corpus file declared an integer parameter, so the `ceilingOKB` branch
    the theorems are about was never entered.

    So this row states, and enforces, what the corpus must actually have
    EXERCISED — measured on the REFERENCE's observations, not on the
    model's predictions, because a model that computed nothing would
    otherwise satisfy its own coverage claim. Each clause below is a
    property some plausible defect would remove:

      * every verdict and every entry kind reached at all;
      * both registration seams reached, so `drain`'s own `reversed` loop
        (item 369) is under the row and not just cordis's unwind;
      * a replay of length >= 2 whose order is NOT registration order, so
        LIFO is distinguishable from FIFO — the direct analogue of the
        missing integer parameter;
      * a compensation that ran strictly AFTER a phase-1 inverse, so the
        two-phase split is distinguishable from one interleaved pass;
      * a non-empty discharge and a non-empty stranded column, so the two
        non-replay dispositions are inhabited;
      * a scenario whose replay set is a PROPER subset of its stack, so
        "everything replays" would be visible.

    Returns findings, which the caller treats as gate failures — a corpus
    that stopped covering a clause is a row that quietly stopped biting.
    """
    seen_verdicts: set = set()
    seen_kinds: set = set()
    seen_seams: set = set()
    order_witness = phase_witness = discharge_witness = None
    strand_witness = proper_subset_witness = None
    for scen, stack, verdict in teardown_scenarios():
        row = observed.get(scen)
        if row is None:
            return [f"teardown coverage: no observation for {scen}"]
        ran, discharged, stranded = row
        seen_verdicts.add(verdict)
        for seam, kind in stack:
            seen_kinds.add(kind)
            seen_seams.add(seam)
        labels = [f"e{i}" for i in range(len(stack))]
        if len(ran) >= 2 and list(ran) != [l for l in labels if l in set(ran)]:
            order_witness = order_witness or (scen, ran)
        comp = {f"e{i}" for i, (_s, k) in enumerate(stack) if k == "compensation"}
        other = {f"e{i}" for i, (_s, k) in enumerate(stack) if k != "compensation"}
        if comp & set(ran) and other & set(ran):
            first_comp = min(ran.index(x) for x in comp & set(ran))
            last_other = max(ran.index(x) for x in other & set(ran))
            if first_comp > last_other:
                phase_witness = phase_witness or (scen, ran)
        if discharged:
            discharge_witness = discharge_witness or (scen, discharged)
        if stranded:
            strand_witness = strand_witness or (scen, stranded)
        if ran and len(ran) < len(stack):
            proper_subset_witness = proper_subset_witness or (scen, ran)

    findings: list[str] = []
    if seen_verdicts != set(_G7_VERDICTS):
        findings.append(f"teardown coverage: verdicts {sorted(seen_verdicts)} "
                        f"!= {sorted(_G7_VERDICTS)}")
    want_kinds = {k for _s, k in _G7_SHAPES}
    if seen_kinds != want_kinds:
        findings.append(f"teardown coverage: kinds {sorted(seen_kinds)} "
                        f"!= {sorted(want_kinds)}")
    if seen_seams != {"body", "method"}:
        findings.append(f"teardown coverage: seams {sorted(seen_seams)} "
                        "!= ['body', 'method']")
    for label, witness in (("LIFO is not FIFO", order_witness),
                           ("phase 2 runs after phase 1", phase_witness),
                           ("some entry is discharged", discharge_witness),
                           ("some entry is stranded", strand_witness),
                           ("some replay set is a proper subset",
                            proper_subset_witness)):
        if witness is None:
            findings.append(f"teardown coverage: NO witness that {label} — "
                            "the row would agree vacuously")
    if not findings:
        print(f"teardown coverage: {len(observed)} scenarios, all "
              f"{len(_G7_VERDICTS)} verdicts x {len(want_kinds)} kinds x 2 "
              f"seams; LIFO={order_witness[0]} phase2={phase_witness[0]} "
              f"discharge={discharge_witness[0]} strand={strand_witness[0]} "
              f"subset={proper_subset_witness[0]}")
    return findings


# ------------------------------------------- A8/R4 crash-recovery dispositions
#
# The G7 rows above are about a teardown that RUNS in one process. These are
# about what a FRESH process concludes from a durable log after the old one
# died: A8's commit/abort discharge across a crash cut, and R4's residue
# surface. Both had real Lean theorems and no oracle row until item 210, so
# both were checked against the design documents rather than against
# `src/revl` (`formal/STATUS.md`).
#
# The corpus is therefore not extracted from `.rvl` text either. A recovery
# verdict is a property of a durable LOG, so the facts are the records of one
# WAL — the constructors of `RevL.Lemmas.Rec`, one row each, in append order —
# plus the re-issue oracle 243 rule 6 makes fallible. The reference side
# WRITES those records as a real JSON-Lines WAL and calls
# `revl.recovery.recover` over it; nothing here decides anything.

#: One content record shape, as (code, builder). `dx` differs from `dT` only in
#: that its re-issue FAILS, which is what puts `Disp.residue .restoreFailed`
#: under the row (243 rule 6: the inverse is fallible, and the model carries
#: that as the `ok` oracle rather than assuming success).
_WAL_SHAPES: tuple = (
    ("dt", ("descriptor", "transactional", False)),   # undeclared inverse
    ("dT", ("descriptor", "transactional", True)),    # declared idempotent
    ("dx", ("descriptor", "transactional", True)),    # ... whose re-issue fails
    ("dc", ("descriptor", "compensation", False)),    # 247: never confirmed
    ("em", ("effect", False, False, False)),          # in-process: moot
    ("er", ("effect", True, True, False)),            # reconstructible, undeclared
    ("eR", ("effect", True, True, True)),             # reconstructible, declared
    ("eu", ("effect", True, False, False)),           # closure-only: residue
    ("dq", ("deferred",)),                            # 245 class-(b) queue entry
)

#: The seqs whose inverse fails on re-issue are exactly the `dx` ones. Only the
#: transactional Phase-1 path in `_roll_back` guards the apply with a `try`
#: (243 rule 6), so a failing inverse in any other family would crash recover
#: rather than be reported — which is itself the reference's answer, and not
#: the branch this row is about.
_WAL_FAILING_SHAPE = "dx"

#: Longest log the scenario corpus enumerates. Two content records is the
#: shortest length at which one seq can be committed while another is rolled
#: back in the same run (`RevL.A8.mixed_disposition_admitted`).
_WAL_DEPTH = 2

#: The trailing decision record, if any. `recover`'s if-chain reads them in
#: this order: fork-frozen, then the terminal marker, then commit-approved,
#: then roll back. `aborted` is not a decision — it is the in-process abort's
#: COMPLETION record, which is what tells a completed abort from a crashed one
#: for a fenced inverse (item 309 follow-up).
_WAL_TRAILERS = ("none", "aborted", "complete", "approved", "forkfrozen")


def _wal_logs() -> list:
    """Every log the corpus enumerates: `(name, records, failing seqs)`.

    Enumerated, not hand-picked, for the reason `_g7_stacks` is: an oracle row
    over cases someone chose is an oracle row over the cases they thought of.
    The content records come first (a body's own records), then the recovery
    bookkeeping a runtime writes over them — a durable `discharge` set, the
    at-most-once fences, and the trailing decision record. That IS the append
    order a real run produces, and every prefix of it is a crash cut, which is
    what `RevL.A8.crash_cut_converges` quantifies over.
    """
    out: list = []
    for depth in range(1, _WAL_DEPTH + 1):
        for content in itertools.combinations_with_replacement(_WAL_SHAPES, depth):
            seqs = list(range(1, depth + 1))
            base = [(shape[1], seq) for shape, seq in zip(content, seqs)]
            failing = [seq for shape, seq in zip(content, seqs)
                       if shape[0] == _WAL_FAILING_SHAPE]
            code = "".join(shape[0] for shape in content)
            for dis_name, dis in (("d0", ()), ("d1", (seqs[:1],)), ("dA", (seqs,))):
                for fen_name, fen in (("f0", ()), ("fA", tuple(seqs))):
                    for trailer in _WAL_TRAILERS:
                        records = list(base)
                        records += [(("discharge", tuple(d)), None) for d in dis]
                        records += [(("fence",), s) for s in fen]
                        if trailer != "none":
                            records.append((("marker", trailer), None))
                        name = f"a8r4/{code}/{dis_name}/{fen_name}/{trailer}"
                        out.append((name, records, failing))
    return out


def recovery_scenario_rows() -> list:
    """The A8/R4 fact rows: the WAL's records and the re-issue oracle, in
    append order. Nothing decided."""
    rows: list = []
    for name, records, failing in _wal_logs():
        for spec, seq in records:
            kind = spec[0]
            if kind == "descriptor":
                rows.append(f"L\t{name}\tdescriptor\t{seq}\t{spec[1]}\t"
                            f"{int(spec[2])}")
            elif kind == "effect":
                rows.append(f"L\t{name}\teffect\t{seq}\t{int(spec[1])}\t"
                            f"{int(spec[2])}\t{int(spec[3])}")
            elif kind == "deferred":
                rows.append(f"L\t{name}\tdeferred\t{seq}")
            elif kind == "discharge":
                rows.append(f"L\t{name}\tdischarge\t"
                            + ",".join(str(s) for s in spec[1]))
            elif kind == "fence":
                rows.append(f"L\t{name}\tfence\t{seq}")
            elif kind == "marker":
                rows.append(f"L\t{name}\tmarker\t{spec[1]}")
            else:  # pragma: no cover — the shape table is closed
                raise SystemExit(f"differential oracle: unknown WAL record {spec}")
        for seq in failing:
            rows.append(f"L\t{name}\tfails\t{seq}")
        rows.append(f"L\t{name}\trun")
    return rows


class _ProbeWorld(recovery.DictWorld):
    """`recovery.DictWorld`, watching what recover actually APPLIES.

    The report names what recover DECIDED; this names what it DID. The
    distinction is load-bearing exactly once: a fenced inverse resolved by a
    durable `aborted` record lands on `transactionalRolledBack` and is NOT
    applied (re-applying a completed abort's Phase 1 would be the double-apply
    the fence exists to prevent), so reading the applied set off the report
    would report an apply that never happened.

    A seq in `failing` raises on re-issue — 243 rule 6's fallible inverse, the
    `ok` oracle the model carries as a parameter. The attempt is recorded
    BEFORE the raise, because the attempt is what happened.
    """

    def __init__(self, failing) -> None:
        super().__init__()
        self.failing = set(failing)
        self.applied: list = []

    @staticmethod
    def _seq(op: dict) -> int:
        # The seq rides in the receiver the corpus built the call from, so it
        # is transported by the reference's own descriptor rather than by a
        # parallel bookkeeping list.
        return int(str(op.get("receiver"))[1:])

    def apply_inverse(self, op: dict) -> None:
        seq = self._seq(op)
        self.applied.append(seq)
        if seq in self.failing:
            raise RuntimeError(f"the re-issued inverse for seq {seq} failed")
        super().apply_inverse(op)

    def apply_compensation(self, op: dict) -> None:
        self.applied.append(self._seq(op))
        super().apply_compensation(op)


def _wal_record_json(spec: tuple, seq) -> dict:
    """One `RevL.Lemmas.Rec` as the JSON-Lines record `revl.wal.read_wal`
    reads. The seq rides in three places the reference itself carries through:
    the call's `receiver` (so `World.key` names it), the record's
    `origin.method` (so the residue schema's `crossing.method` names it), and
    the `label`. Nothing else about these records is load-bearing."""
    kind = spec[0]
    if kind == "descriptor":
        return {"record": "discharge-descriptor", "seq": seq, "entry": spec[1],
                "call": {"receiver": f"r{seq}", "method": "undo", "args": []},
                "origin": {"key": f"k{seq}", "method": str(seq), "args": []},
                "undo_idempotent": spec[2]}
    if kind == "effect":
        _k, boundary, reconstructible, idem = spec
        out = {
            "record": "effect", "seq": seq, "component": "Probe",
            "label": str(seq), "kind": "boundary",
            "boundary": {"class": "b",
                         "referent": "process-crossing" if boundary
                                     else "in-process",
                         "detail": {"key": f"k{seq}", "method": str(seq),
                                    "args": []}},
            "origin": {"key": f"k{seq}", "method": str(seq), "args": []},
        }
        out["inverse"] = (
            {"reconstructible": True, "undo_idempotent": idem,
             "op": {"receiver": f"r{seq}", "method": "undo", "args": []}}
            if reconstructible else
            {"reconstructible": False, "reason": "closure-only inverse"})
        return out
    if kind == "deferred":
        return {"record": "deferred-emission", "seq": seq,
                "call": {"receiver": f"r{seq}", "method": "fire", "args": []},
                "origin": {"key": f"k{seq}", "method": str(seq), "args": []}}
    if kind == "discharge":
        return {"record": "discharge", "discharged": list(spec[1])}
    if kind == "fence":
        return {"record": "replay-fence", "seq": seq}
    if kind == "marker":
        return {"record": {"approved": "commit-approved", "aborted": "aborted",
                           "forkfrozen": "fork-frozen",
                           "complete": "activation-complete"}[spec[1]]}
    raise SystemExit(f"differential oracle: unknown WAL record {spec}")   # pragma: no cover


#: `recover`'s verdict string -> the model's `RevL.Lemmas.Outcome` name. Only
#: these three; a fourth verdict (`roll-forward-refused`,
#: `roll-forward-needs-approval`) needs a session and a snapshot, which this
#: corpus never supplies, and is outside the model.
_WAL_VERDICTS = {"rolled-back": "rolledBack", "rolled-forward": "rolledForward",
                 "fork-retired": "forkRetired"}

#: What the REFERENCE was seen to do, per scenario, for the non-vacuity
#: ratchet. Filled by `recovery_observation`; read by `recovery_coverage`.
#: Kept beside the compared verdicts rather than inside them because these are
#: evidence the row BITES, not claims either side makes.
_RECOVERY_MARKS: dict = {}


def recovery_observation(name: str, records: list, failing: list) -> tuple:
    """Write one scenario's records as a real WAL and run `revl.recovery`
    over it; report `(outcome, applied seqs, residue seqs)`.

    Nothing here decides anything. The outcome is recover's own verdict, the
    applied set is what it actually put through `World.apply_inverse` /
    `apply_compensation`, and the residue is the seq of every record in
    `residue.outstanding` — read off the merged residue schema's own
    `crossing.method`.

    The residue column is `None` for a verdict other than `rolled-back`:
    `RevL.Lemmas.reported` models the ROLL-BACK path's surface and R4 is
    stated under `outcome L = .rolledBack`, so the roll-forward window's
    `flush-residue` is a surface neither side claims and is not compared.
    """
    handle, path = tempfile.mkstemp(suffix=".wal", prefix="revl-oracle-")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as out:
            out.write(json.dumps({"record": "header", "walVersion": WAL_VERSION,
                                  "generation": 1,
                                  "guarantee": WAL_GUARANTEE}) + "\n")
            for spec, seq in records:
                out.write(json.dumps(_wal_record_json(spec, seq)) + "\n")
        world = _ProbeWorld(failing)
        report = recovery.recover(path, world=world)
    finally:
        os.unlink(path)

    verdict = _WAL_VERDICTS.get(report["verdict"])
    if verdict is None:  # pragma: no cover — the corpus supplies no session
        raise SystemExit(
            f"differential oracle: recover returned {report['verdict']!r} for "
            f"{name}, which is outside the model's three outcomes")
    residue = None
    if verdict == "rolledBack":
        residue = []
        for rec in report["residue"]["outstanding"]:
            seq = (rec.get("crossing") or {}).get("method")
            if seq is None:  # pragma: no cover — every record carries a crossing
                raise SystemExit(
                    f"differential oracle: unnamed residue record in {name}")
            residue.append(int(seq))
    _RECOVERY_MARKS[name] = _recovery_marks(report, world, records)
    return verdict, sorted(world.applied), (None if residue is None
                                            else sorted(residue))


def _recovery_marks(report: dict, world: "_ProbeWorld", records: list) -> frozenset:
    """The behaviours this scenario was SEEN to exercise, off the reference's
    own report. Evidence for `recovery_coverage`, never a compared verdict."""
    marks = set()
    outstanding = report.get("residue", {}).get("outstanding") or []
    kinds = {rec.get("kind") for rec in outstanding}
    if world.applied:
        marks.add("applied")
    if report.get("fencedDeferred"):
        marks.add("fenced-refusal")
    if any(d.get("retained") for d in report.get("dischargedSkipped") or []):
        marks.add("committed-retained")
    if any(d.get("replay") == "free"
           for d in (report.get("ran") or [])
           + (report.get("transactionalRolledBack") or [])):
        marks.add("free-replay")
    if any(d.get("replay") == "abort-phase1"
           for d in report.get("transactionalRolledBack") or []):
        marks.add("abort-resolved")
    if report.get("moot"):
        marks.add("moot")
    if report.get("droppedDeferred"):
        marks.add("dropped")
    if report.get("compensationsReissued"):
        marks.add("compensation-reissued")
    marks |= {f"residue:{k}" for k in kinds if k}
    if report["verdict"] == "rolled-back" and not outstanding and records:
        marks.add("clean-abort")
    return frozenset(marks)


def recovery_coverage(observed: dict) -> list[str]:
    """The non-vacuity ratchet for the A8/R4 row (roadmap items 429 / 210).

    Same discipline as `teardown_coverage`, and for the same reason: the
    formal-layer audit found the `W` row's capability-ceiling half agreeing
    VACUOUSLY over a corpus that declared no integer parameter, and a new row
    is worth nothing until it is known to bite. Each clause below is a
    behaviour of the REFERENCE — never of the model, which would otherwise
    satisfy its own coverage claim by computing nothing — that some plausible
    defect would remove:

      * all three outcomes reached, and both roll-forward routes (the terminal
        marker and item 245's approved window), so `outcome`'s if-chain is
        exercised rather than assumed;
      * a run that APPLIED an inverse and a roll-back that applied NONE, so
        "replays the abort" is distinguishable from "replays nothing";
      * a committed seq RETAINED (`A8.committed_transaction_is_retained`) and
        a fenced undeclared inverse REFUSED (item 309 §3a's at-most-once),
        which are the two ways the applied set shrinks below the log;
      * a declared-idempotent inverse applied FREELY over a durable fence,
        which is the other half of 309 and the only thing that tells the
        `idem` field apart from a constant;
      * a fenced inverse RESOLVED by a durable `aborted` record, the branch
        that tells a completed abort from a crashed one;
      * every residue kind the model can produce — unreconstructible,
        compensation, fenced, restore-failed — inhabited, and a CLEAN abort
        over a non-empty log, which is R4's headline
        (`abort_leaves_no_residue`) and the one shape a model that reported
        everything would fail.

    Returns findings, which the caller treats as gate failures.
    """
    want_outcomes = {"rolledBack", "rolledForward", "forkRetired"}
    seen_outcomes: set = set()
    seen_marks: set = set()
    forward_routes: set = set()
    empty_rollback = None
    for name, records, _failing in _wal_logs():
        row = observed.get(name)
        if row is None:
            return [f"recovery coverage: no observation for {name}"]
        outcome, applied, _residue = row
        seen_outcomes.add(outcome)
        seen_marks |= _RECOVERY_MARKS.get(name, frozenset())
        if outcome == "rolledForward":
            forward_routes.add(name.rsplit("/", 1)[1])
        if outcome == "rolledBack" and not applied and records:
            empty_rollback = empty_rollback or name

    findings: list[str] = []
    if seen_outcomes != want_outcomes:
        findings.append(f"recovery coverage: outcomes {sorted(seen_outcomes)} "
                        f"!= {sorted(want_outcomes)}")
    if forward_routes != {"complete", "approved"}:
        findings.append("recovery coverage: roll-forward routes "
                        f"{sorted(forward_routes)} != ['approved', 'complete']")
    if empty_rollback is None:
        findings.append("recovery coverage: NO roll-back that applied nothing "
                        "over a non-empty log — the row would agree vacuously")
    want_marks = {
        "applied", "fenced-refusal", "committed-retained", "free-replay",
        "abort-resolved", "moot", "dropped", "compensation-reissued",
        "clean-abort", "residue:unreconstructible", "residue:fenced-residue",
        "residue:restore-residue", "residue:compensation-residue",
    }
    for mark in sorted(want_marks - seen_marks):
        findings.append(f"recovery coverage: NO witness of `{mark}` — the row "
                        "would agree over a branch the corpus never reaches")
    if not findings:
        print(f"recovery coverage: {len(observed)} WAL scenarios, all 3 outcomes "
              f"x 2 roll-forward routes x {len(want_marks)} reference "
              f"behaviours; empty-rollback={empty_rollback}")
    return findings


#: What the REFERENCE computed for each reconstructed statement, for the G6
#: non-vacuity ratchet: (confined, head count, leaked-root count). Filled by
#: `reference_from_tsv`; read by `confinement_coverage`. Kept beside the
#: compared verdict rather than inside it because these are evidence the row
#: BITES, not a claim either side makes.
_CONFINEMENTS: dict = {}


def confinement_coverage() -> list[str]:
    """The non-vacuity ratchet for the `C` row (G6, issue 276).

    Same discipline as `attenuation_coverage`, and for the same reason: the
    whole point of #276 is that a confinement row every admitted component
    satisfies trivially certifies nothing. Every exported `I` row IS from an
    admitted component, so if the row only ever said `ok` it would agree
    vacuously. So this states, and enforces, that the corpus actually
    EXERCISES both verdicts on the REFERENCE's own computation:

      * some statement is confined with a NON-EMPTY reach surface -- an `ok`
        that is a real confinement (every crossing declared), not the empty
        statement's free pass;
      * some statement LEAKS -- a head whose root is outside the component's
        declared context, which the row scores `fail`. This is the caught
        violation #276 requires: without one, `confinedB` would be a constant
        `true` over the corpus and the differential would prove nothing.

    A leaking statement is a `fail` on BOTH sides (both read head-roots against
    the same declared context), so the row bites without an admitted violation
    to point at -- the checker refuses those at parse (the G6 fixtures), so
    none reaches an `I` row. The bite is instead that the verdict is
    mutation-sensitive: `RevL.G6.g6_row_not_vacuous` proves the check flips
    when a leaking head is accepted, so a reference that drifted to accept one
    would diverge from the Lean row here.

    Returns findings, which the caller treats as gate failures.
    """
    confined_witness = leak_witness = None
    caught = 0
    for key, (confined, n_heads, n_leaked) in _CONFINEMENTS.items():
        if confined and n_heads > 0:
            confined_witness = confined_witness or key
        if not confined:
            caught += 1
            leak_witness = leak_witness or key
    findings: list[str] = []
    for label, witness in (
            ("a statement confined over a non-empty reach surface",
             confined_witness),
            ("a statement whose head leaks outside the declared context",
             leak_witness)):
        if witness is None:
            findings.append(f"confinement coverage: NO witness of {label} — "
                            "the C row would agree vacuously")
    if not findings:
        print(f"confinement coverage: {len(_CONFINEMENTS)} statements, "
              f"{caught} caught violations; confined={confined_witness} "
              f"leak={leak_witness}")
    return findings


#: non-vacuity ratchet for the S8/U5 rows (G8/G5, issue 276). Filled by
#: `reference_from_tsv`, read by `prog_coverage`. `_G8_SURFACES` is
#: `key -> caps tuple` for every compared (non-`n/a`) surface; `_G5_REGS` is
#: `key -> crossing count` for every compared (non-`n/a`) teardown.
_G8_SURFACES: dict = {}
_G5_REGS: dict = {}


def prog_coverage() -> list[str]:
    """The non-vacuity ratchet for the `S8`/`U5` rows (G8/G5, issue 276).

    Same discipline as `confinement_coverage`: a surface row that is empty on
    every statement, or a teardown row that is `0` on every effect, certifies
    nothing. The differential is model-vs-model (the Lean fold vs the Python
    fold over one `Prog`), so it agrees vacuously unless the corpus EXERCISES
    both classes of each verdict:

      * G8: some statement has a NON-EMPTY boundary surface (a crossing the
        reach fold actually enumerates) AND some statement has an empty one;
      * G5: some effect's teardown registers ZERO crossings (a clean inverse)
        AND some registers a POSITIVE count — the caught violation. That
        second witness cannot come from an admitted file (an admitted
        witnessed inverse registers nothing, by G5), so it comes from a
        refused fixture that still parses and exports rows
        (`examples/rejections/g5_undo_fn_emission.rvl`).

    The Lean side's `g5_row_not_vacuous` / `g8_row_not_vacuous` prove the same
    verdicts are mutation-sensitive, so a reference that drifted would diverge
    from the oracle here. Returns findings, treated as gate failures."""
    surf_nonempty = surf_empty = None
    for key, caps in _G8_SURFACES.items():
        if caps:
            surf_nonempty = surf_nonempty or key
        else:
            surf_empty = surf_empty or key
    reg_zero = reg_pos = None
    caught = 0
    for key, n in _G5_REGS.items():
        if n == 0:
            reg_zero = reg_zero or key
        elif n > 0:
            caught += 1
            reg_pos = reg_pos or key
    findings: list[str] = []
    for label, witness in (
            ("a statement with a non-empty G8 boundary surface", surf_nonempty),
            ("a statement with an empty G8 boundary surface", surf_empty),
            ("an effect whose teardown registers no crossings", reg_zero),
            ("an effect whose teardown registers a crossing (caught G5 "
             "violation)", reg_pos)):
        if witness is None:
            findings.append(f"prog coverage: NO witness of {label} — "
                            "the S8/U5 rows would agree vacuously")
    if not findings:
        print(f"prog coverage: {len(_G8_SURFACES)} surfaces, "
              f"{len(_G5_REGS)} teardowns, {caught} caught G5 violations; "
              f"surface={surf_nonempty} teardown_pos={reg_pos}")
    return findings


def run_oracle(tsv_path: Path, out_path: Path) -> str | None:
    """Run the Lean oracle over the corpus TSV; None if lake is absent."""
    if shutil.which("lake") is None:
        print("SKIP (loud): lake not on PATH — formal verdicts NOT computed")
        return None
    proc = subprocess.run(
        ["lake", "env", "lean", "--run", str(FORMAL / "harness" / "Oracle.lean"),
         str(tsv_path), str(out_path)],
        cwd=FORMAL, capture_output=True, text=True, check=False,
    )
    if proc.returncode != 0:
        print(proc.stdout[-2000:])
        print(proc.stderr[-2000:])
        raise SystemExit("differential oracle: Lean oracle failed")
    return out_path.read_text(encoding="utf-8")


class Verdicts(NamedTuple):
    """One side's verdicts. `files` are V rows (disjoint, closed, link),
    `comps` G rows, `providers` P rows, `spawns` W rows, `refused` X rows,
    `dispositions` D rows (G7 teardown: replayed / discharged / stranded),
    `recoveries` O rows (A8/R4 crash recovery: outcome / applied / residue),
    `confinements` C rows (G6: a reconstructed statement's reach surface is
    within its component's declared context), `g8surface` S8 rows (G8: a
    statement's boundary surface over the reconstructed `Prog`), `g5reg` U5
    rows (G5: an effect's teardown registration count)."""
    files: dict[str, tuple[str, str, str]]
    comps: dict[tuple[str, str], str]
    providers: dict[tuple[str, str, str, str, str], str]
    spawns: dict[tuple[str, str, str], str]
    refused: dict[str, str]
    dispositions: dict[str, tuple[tuple, tuple, tuple]]
    recoveries: dict[str, tuple]
    confinements: dict[tuple[str, str, str], str]
    g8surface: dict[tuple[str, str, str], object]
    g5reg: dict[tuple[str, str, str], object]

    def total(self) -> int:
        return (len(self.files) + len(self.comps) + len(self.providers)
                + len(self.spawns) + len(self.refused)
                + len(self.dispositions) + len(self.recoveries)
                + len(self.confinements) + len(self.g8surface)
                + len(self.g5reg))


def _cols(field: str) -> list[str]:
    """One `key=a,b,c` verdict column as a label list; empty for `key=`."""
    body = field.split("=", 1)[1]
    return [x for x in body.split(",") if x]


def parse_verdicts(text: str) -> Verdicts:
    """Parse oracle output into verdict maps."""
    files: dict[str, tuple[str, str, str]] = {}
    comps: dict[tuple[str, str], str] = {}
    providers: dict[tuple[str, str, str, str, str], str] = {}
    spawns: dict[tuple[str, str, str], str] = {}
    refused: dict[str, str] = {}
    dispositions: dict[str, tuple[tuple, tuple, tuple]] = {}
    recoveries: dict[str, tuple] = {}
    confinements: dict[tuple[str, str, str], str] = {}
    g8surface: dict[tuple[str, str, str], object] = {}
    g5reg: dict[tuple[str, str, str], object] = {}
    for line in text.splitlines():
        parts = line.split("\t")
        if parts[0] == "V" and len(parts) == 5:
            files[parts[1]] = (parts[2].split("=", 1)[1],
                               parts[3].split("=", 1)[1],
                               parts[4].split("=", 1)[1])
        elif parts[0] == "G" and len(parts) == 4:
            comps[(parts[1], parts[2])] = parts[3].split("=", 1)[1]
        elif parts[0] == "P" and len(parts) == 7:
            providers[(parts[1], parts[2], parts[3], parts[4], parts[5])] = \
                parts[6].split("=", 1)[1]
        elif parts[0] == "W" and len(parts) == 5:
            spawns[(parts[1], parts[2], parts[3])] = parts[4].split("=", 1)[1]
        elif parts[0] == "X" and len(parts) == 3:
            refused[parts[1]] = parts[2].split("=", 1)[1]
        elif parts[0] == "D" and len(parts) == 5:
            # The replayed column is ORDERED (that is the LIFO claim); the
            # other two are not — the reference flips `discharged` in place
            # and builds the E-Stop inventory in two passes, so neither has
            # an order the model claims. Compared sorted, and said so.
            dispositions[parts[1]] = (
                tuple(_cols(parts[2])),
                tuple(sorted(_cols(parts[3]))),
                tuple(sorted(_cols(parts[4]))),
            )
        elif parts[0] == "O" and len(parts) == 5:
            # `replayed` is compared as a SET: the model walks the log in
            # append order and `_roll_back` walks each record family
            # newest-first, and neither order is a claim the other makes (the
            # ordered LIFO claim is G7's, checked by the D row). `residue` is
            # `n/a` outside a roll-back, which is the model's own scope.
            body = parts[4].split("=", 1)[1]
            recoveries[parts[1]] = (
                parts[2].split("=", 1)[1],
                tuple(sorted(int(x) for x in _cols(parts[3]))),
                None if body == "n/a" else tuple(sorted(int(x)
                                                        for x in _cols(parts[4]))),
            )
        elif parts[0] == "C" and len(parts) == 5:
            # G6 confinement: (file, comp, statement index) -> ok|fail.
            confinements[(parts[1], parts[2], parts[3])] = parts[4].split("=", 1)[1]
        elif parts[0] == "S8" and len(parts) == 5:
            # G8 boundary surface: (file, comp, index) -> sorted cap set, or
            # 'n/a' for a first-class-dispatch statement. Compared as a SET:
            # the model's `stmtSurface` folds heads in source order, which is
            # not an order either side claims, so both sort before comparing.
            body = parts[4].split("=", 1)[1]
            g8surface[(parts[1], parts[2], parts[3])] = (
                "n/a" if body == "n/a" else tuple(sorted(_cols(parts[4]))))
        elif parts[0] == "U5" and len(parts) == 5:
            # G5 teardown registrations: (file, comp, index) -> crossing count,
            # or 'n/a'. An integer, so a widened teardown is a larger number.
            body = parts[4].split("=", 1)[1]
            g5reg[(parts[1], parts[2], parts[3])] = (
                "n/a" if body == "n/a" else int(body))
        else:
            raise SystemExit(f"differential oracle: malformed verdict row {line!r}")
    return Verdicts(files, comps, providers, spawns, refused, dispositions,
                    recoveries, confinements, g8surface, g5reg)


def _slots(provides: list[str], realms: dict[str, str]) -> list[tuple[str, str]]:
    """`RevL.Manifest.slots` — the `(key, realm)` pairs a component fills.
    An unisolated key sits in the shared realm (the empty string)."""
    return [(k, realms.get(k, "")) for k in provides]


def _link_ok(comps: list[tuple[list[str], list[str], dict[str, str]]]) -> bool:
    """`RevL.Manifest.LinkOK` over the LOCAL composition, decided the same
    way the oracle decides it (see `Oracle.linkVerdict`): elide the
    requirements no in-file component provides — the linker adds no edge for
    a key with no provider — then admit components one at a time, each with
    distinct slots, none re-providing an admitted slot, and every consumed
    slot already admitted. A component that requires a key it provides
    itself keeps that requirement and can never be admitted, which is the
    linker's G3 self-provision refusal."""
    provided_all: set[tuple[str, str]] = set()
    for _reqs, provs, realms in comps:
        provided_all.update(_slots(provs, realms))
    local = [
        ([k for k in reqs if (k, realms.get(k, "")) in provided_all], provs, realms)
        for reqs, provs, realms in comps
    ]
    admitted: set[tuple[str, str]] = set()
    remaining = list(local)
    while remaining:
        for i, (reqs, provs, realms) in enumerate(remaining):
            if all((k, realms.get(k, "")) in admitted for k in reqs):
                mine = _slots(provs, realms)
                if len(mine) != len(set(mine)) or admitted & set(mine):
                    return False
                admitted.update(mine)
                remaining.pop(i)
                break
        else:
            return False
    return True


def reference_from_tsv(tsv: list[str]) -> Verdicts:
    """Reference verdicts, recomputed from the same TSV the oracle
    consumed. The capability half calls `src/revl/cap_order.py` — the real
    checker's algebra, not a restatement of it — so the diff compares the
    PROVED model (the Lean side) against the SHIPPED checker.

    V rows are FILE-WIDE: provision disjointness over `(key, realm)` slots,
    requirement closure, and linkability (`LinkOK`). G rows are
    PER-COMPONENT marker-rule (marker presence == interface declaration,
    incl. spawn-handle receivers). P rows are PER-PROVIDE-METHOD: a service
    declaration is an upper bound — the method's reached emission tokens
    must be within its declared bound (plain => none; any => free; scoped
    => the declared entries). W rows are PER-SPAWN-EDGE attenuation
    (item 66/294). X rows carry a parse refusal through."""
    rows = [r.split("\t") for r in tsv]
    mrows = [r for r in rows if r and r[0] == "M" and len(r) == 7]
    xrows = [r for r in rows if r and r[0] == "X" and len(r) == 3]
    urows = [r for r in rows if r and r[0] == "U" and len(r) == 7]
    brows = [r for r in rows if r and r[0] == "B" and len(r) == 5]
    qrows = [r for r in rows if r and r[0] == "Q" and len(r) == 5]
    arows = [r for r in rows if r and r[0] == "A" and len(r) == 4]
    frows = [r for r in rows if r and r[0] == "F" and len(r) == 7]
    krows = [r for r in rows if r and r[0] == "K" and len(r) == 5]
    srows = [r for r in rows if r and r[0] == "S" and len(r) == 4]
    harows = [r for r in rows if r and r[0] == "HA" and len(r) == 5]
    irows = [r for r in rows if r and r[0] == "I" and len(r) == 7]
    pgrows = [r for r in rows if r and r[0] == "PG" and len(r) == 3]
    exrows = [r for r in rows if r and r[0] == "EX" and len(r) == 7]
    fnrows = [r for r in rows if r and r[0] == "FN" and len(r) == 5]

    ems_by_file: dict[str, set[tuple[str, str]]] = {}
    bounds_by_file: dict[tuple[str, str, str], tuple[str, set[str]]] = {}
    for r in brows:
        # Only non-plain emissions (any/scoped) count for the G4 marker rule.
        # "plain" means no emission bound - it's a regular method, not a crossing.
        if r[4] != "plain":
            ems_by_file.setdefault(r[1], set()).add((r[2], r[3]))
        bounds_by_file[(r[1], r[2], r[3])] = (r[4], set())
    for r in qrows:
        key = (r[1], r[2], r[3])
        mode, ents = bounds_by_file.get(key, ("plain", set()))
        bounds_by_file[key] = (mode, ents | {r[4]})

    def _realms(row: list[str]) -> dict[str, str]:
        out: dict[str, str] = {}
        for chunk in row[5].split(","):
            if chunk:
                k, _, r = chunk.partition("=")
                out[k] = r
        return out

    files: dict[str, tuple[str, str, str]] = {}
    for rel in sorted({r[1] for r in mrows}):
        # Spawn TEMPLATES are runtime instances, not composition members
        # (`lower._link`'s `templates` exclusion), so they take no part in
        # the static G2/G3 table.
        fm = [r for r in mrows if r[1] == rel and r[6] != "template"]
        shaped = [([k for k in r[3].split(",") if k],
                   [k for k in r[4].split(",") if k], _realms(r)) for r in fm]
        prov_slots = [s for _rq, pv, rl in shaped for s in _slots(pv, rl)]
        need_slots = [s for rq, _pv, rl in shaped for s in _slots(rq, rl)]
        files[rel] = (
            "ok" if len(prov_slots) == len(set(prov_slots)) else "fail",
            "ok" if all(s in set(prov_slots) for s in need_slots) else "fail",
            "ok" if _link_ok(shaped) else "fail",
        )

    comps: dict[tuple[str, str], str] = {}
    for r in mrows:
        rel, compn = r[1], r[2]
        ems = ems_by_file.get(rel, set())
        # U row: [U, file, comp, ctx, root, svc, meth]
        raw = any(
            u[2] == compn and ((u[3] == "emit") != ((u[5], u[6]) in ems))
            for u in urows if u[1] == rel
        )
        # HA row: [HA, file, comp, verb, position]. The same G4 guarantee over
        # the acquisition fact: a host acquire verb outside a `bracket` is
        # refused (issue 334). The verb table is the SHIPPED one — this side is
        # the checker's, and the Lean oracle carries its own matching copy.
        acquire = any(
            h[2] == compn and h[3] in _HOST_ACQUIRE_VERBS and h[4] != "bracket"
            for h in harows if h[1] == rel
        )
        comps[(rel, compn)] = "fail" if (raw or acquire) else "ok"

    providers: dict[tuple[str, str, str, str, str], str] = {}
    fmethods: dict[tuple[str, str, str, str, str], set[str]] = {}
    for r in frows:
        fmethods.setdefault((r[1], r[2], r[3], r[4], r[5]), set()).add(r[6])
    for k, caps in fmethods.items():
        mode, ents = bounds_by_file.get((k[0], k[3], k[4]), ("plain", set()))
        if mode == "any":
            ok = True
        elif mode == "plain":
            ok = not caps
        else:
            ok = {parse_cap(c).token for c in caps} <= ents
        providers[k] = "ok" if ok else "fail"

    owns: dict[tuple[str, str], set[str]] = {}
    for r in arows:
        owns.setdefault((r[1], r[2]), set()).add(r[3])
    for r in frows:
        owns.setdefault((r[1], r[2]), set()).add(r[6])
    held: dict[tuple[str, str], set[str]] = {k: set(v) for k, v in owns.items()}
    for r in krows:
        held.setdefault((r[1], r[2]), set()).add(r[4])
    edges_by_file: dict[str, list[tuple[str, str]]] = {}
    for r in srows:
        edges_by_file.setdefault(r[1], []).append((r[2], r[3]))
    closed: dict[tuple[str, str], set[str]] = {k: set(v) for k, v in owns.items()}
    changed = True
    while changed:
        changed = False
        for rel, edges in edges_by_file.items():
            for parent, child in edges:
                before = len(closed.get((rel, parent), set()))
                closed.setdefault((rel, parent), set()).update(
                    closed.get((rel, child), set()))
                if len(closed[(rel, parent)]) != before:
                    changed = True
    spawns: dict[tuple[str, str, str], str] = {}
    _ATTENUATION_HALVES.clear()
    for r in srows:
        rel, parent, child = r[1], r[2], r[3]
        hset = held.get((rel, parent), set())
        rset = closed.get((rel, child), set())
        resource, ceiling = attenuation_halves(hset, rset)
        spawns[(rel, parent, child)] = "ok" if resource and ceiling else "fail"
        # Whether this edge binds a ceiling parameter AT ALL is what tells an
        # agreeing row from an unexercised one (`attenuation_coverage`).
        has_ceiling = any(
            cap_order.is_ceiling(name)
            for cap in (parse_cap(c) for c in hset | rset)
            for name, _v in cap.params)
        _ATTENUATION_HALVES[(rel, parent, child)] = (resource, ceiling,
                                                     has_ceiling)

    refused = {r[1]: r[2] for r in xrows}

    # D rows: the G7 teardown disposition, OBSERVED. The stack shape and the
    # verdict come off the same TSV the Lean side read; what each entry's fate
    # was comes from actually running `backends/python/runtime.py` over it.
    erows = [r for r in rows if r and r[0] == "E" and len(r) == 5]
    jrows = [r for r in rows if r and r[0] == "J" and len(r) == 3]
    stacks: dict[str, list] = {}
    for r in erows:
        stacks.setdefault(r[1], []).append((r[4], r[3]))
    dispositions: dict[str, tuple[tuple, tuple, tuple]] = {}
    for r in jrows:
        ran, discharged, stranded = teardown_observation(
            stacks.get(r[1], []), r[2])
        dispositions[r[1]] = (tuple(ran), tuple(sorted(discharged)),
                              tuple(sorted(stranded)))

    # O rows: the A8/R4 crash-recovery disposition, OBSERVED. The records come
    # off the same TSV the Lean side read; what recover DID with them comes
    # from actually running `src/revl/recovery.py` over a WAL carrying them.
    _RECOVERY_MARKS.clear()
    recoveries: dict[str, tuple] = {}
    for name, records, failing in _wal_logs():
        outcome, applied, residue = recovery_observation(name, records, failing)
        recoveries[name] = (outcome, tuple(applied),
                            None if residue is None else tuple(residue))

    # C rows: G6 confinement, computed INDEPENDENTLY of admission. A
    # component's declared context is its require locals (M) together with the
    # roots its require-held caps bind (K) -- the names a body may legitimately
    # reach through. A reconstructed statement is confined iff every head-root
    # it reaches is one of those declared roots. This is the same head-roots
    # membership the Lean side decides with `confinedB`, computed here from the
    # SAME TSV rather than from either side's admission judgment: a leaking
    # head is a `fail` on both sides, so the row bites without needing an
    # admitted violation (which the checker refuses at parse, see the G6
    # fixtures) to point at.
    def _root(h: str) -> str:
        return h.split(".", 1)[0]

    requires_by_comp: dict[tuple[str, str], list[str]] = {}
    for r in mrows:
        requires_by_comp[(r[1], r[2])] = [k for k in r[3].split(",") if k]
    kbinds_by_comp: dict[tuple[str, str], set[str]] = {}
    for r in krows:
        kbinds_by_comp.setdefault((r[1], r[2]), set()).add(r[3])

    _CONFINEMENTS.clear()
    confinements: dict[tuple[str, str, str], str] = {}
    for r in irows:
        rel, compn, index, kind = r[1], r[2], r[3], r[4]
        heads = [h for h in r[5].split(",") if h]
        inverse = [h for h in r[6].split(",") if h]
        reach = heads + inverse if kind == "effect" else heads
        declared = set(requires_by_comp.get((rel, compn), [])) \
            | kbinds_by_comp.get((rel, compn), set())
        leaked = {_root(h) for h in reach} - declared
        confinements[(rel, compn, index)] = "ok" if not leaked else "fail"
        _CONFINEMENTS[(rel, compn, index)] = (not leaked, len(reach), len(leaked))

    # S8/U5 rows (G8 boundary surface, G5 teardown purity; issue 276),
    # recomputed INDEPENDENTLY from the EX/FN/PG rows: the same model fold the
    # oracle runs, implemented a second time in Python, so the diff is two
    # implementations over one `Prog`. `_prog_reach` is the reach fixed point;
    # `star`-tainted statements are `n/a` (outside the model), decided from
    # the same FN `star` column both sides read.
    externs_by_file: dict[str, dict[str, tuple[str, list[str]]]] = {}
    fns_by_file: dict[str, dict[str, list[str]]] = {}
    fnstar_by_file: dict[str, dict[str, bool]] = {}
    for r in exrows:
        externs_by_file.setdefault(r[1], {})[r[2]] = (
            r[3], [c for c in r[6].split(",") if c])
    for r in fnrows:
        fns_by_file.setdefault(r[1], {})[r[2]] = [c for c in r[3].split(",") if c]
        fnstar_by_file.setdefault(r[1], {})[r[2]] = r[4] == "star"

    _G8_SURFACES.clear()
    _G5_REGS.clear()
    g8surface: dict[tuple[str, str, str], object] = {}
    g5reg: dict[tuple[str, str, str], object] = {}
    reach_cache: dict[str, tuple] = {}
    for r in irows:
        rel, compn, index, kind = r[1], r[2], r[3], r[4]
        heads = [h for h in r[5].split(",") if h]
        inverse = [h for h in r[6].split(",") if h]
        if rel not in reach_cache:
            rc, rx, rn = _prog_reach(externs_by_file.get(rel, {}),
                                     fns_by_file.get(rel, {}))
            reach_cache[rel] = (rc, rx, _star_tainted(
                fnstar_by_file.get(rel, {}), rn))
        reach_caps, reach_crosses, tainted = reach_cache[rel]
        stmt_heads = heads + inverse if kind == "effect" else heads
        if any(h in tainted for h in stmt_heads):
            g8surface[(rel, compn, index)] = "n/a"
        else:
            caps: set[str] = set()
            for h in stmt_heads:
                caps |= reach_caps.get(h, frozenset())
            surf = tuple(sorted(caps))
            g8surface[(rel, compn, index)] = surf
            _G8_SURFACES[(rel, compn, index)] = surf
        if kind == "effect":
            if any(h in tainted for h in inverse):
                g5reg[(rel, compn, index)] = "n/a"
            else:
                n = sum(1 for h in inverse if reach_crosses.get(h, False))
                g5reg[(rel, compn, index)] = n
                _G5_REGS[(rel, compn, index)] = n

    return Verdicts(files, comps, providers, spawns, refused, dispositions,
                    recoveries, confinements, g8surface, g5reg)


# The buckets that are GATE FAILURES, not findings (item 418 step 7). Both
# are the DANGEROUS direction: the real checker REFUSES a file and the model
# sees nothing wrong with it, so the model is weaker than what revl enforces
# and the "the model agrees with the checker" claim would be false.
# `formal-strict` — the model refusing what the checker accepts — stays
# informational: it is the safe direction and names fragment gaps.
FATAL_BUCKETS = ("missed-G4", "missed-G2")


def checker_alignment(file_facts: dict, componentless: list[str],
                      v: Verdicts) -> list[str]:
    """Compile each file with the real checker and compare refusal codes
    against the formal verdicts. Returns the fatal-bucket findings.

    Requirement CLOSURE (and hence linkability, which subsumes it) is
    deliberately NOT part of `formal_clean`. `compile_source` type-checks
    and links ONE file: a requirement no in-file component provides is
    resolved against the rest of the composition at `revl link` time, and
    `lower._link` reports nothing for it. Reading the V row's `closed`
    column as a checker-visible refusal made 32 files look like the model
    being stricter than the checker when the model was answering a
    different question. `disjoint` and `link` ARE checker-visible (G2
    provision conflict, G3 self-provision and cycles) and are compared."""
    align: dict[str, int] = {}
    samples: dict[str, list[str]] = {}

    def record(key: str, rel: str) -> None:
        align[key] = align.get(key, 0) + 1
        samples.setdefault(key, []).append(rel)

    # The model covers BOTH G4 rules now. The MARKER rule — a classified
    # statement's marker presence against the interface's declared emission,
    # over crossings resolved to a (service, method) — is `Oracle.g4OK`. The
    # ACQUIRE rule — a HOST acquire verb (`Pool.open`) legal only as the
    # acquisition of an `effect … undo …` bracket, where its release is
    # registered — is `Oracle.hostAcquireOK` over the `HA` position facts
    # (issue 334). So a G4 refusal is fatal in EVERY category again: there is
    # no out-of-fragment exemption. The two G4-coded refusals the model still
    # cannot see — `g4_missing_undo.rvl` and `v2_extern_acquire_no_undo.rvl` —
    # never reach this loop: one is refused at PARSE and one declares no
    # component, so both are reported by the no-manifest census below, not
    # bucketed here.

    def checker_code(rel: str) -> tuple[str, str]:
        try:
            compile_source((REPO / rel).read_text(encoding="utf-8"), rel)
            return "accept", ""
        except RevlError as e:
            info = classify(e)
            return (info.get("code") or "UNCODED"), (info.get("category") or "")

    for rel in file_facts:
        comp_rows = [(k, x) for k, x in v.comps.items() if k[0] == rel]
        prov_rows = [(k, x) for k, x in v.providers.items() if k[0] == rel]
        spawn_rows = [(k, x) for k, x in v.spawns.items() if k[0] == rel]
        vrow = v.files.get(rel, ("ok", "ok", "ok"))
        formal_clean = vrow[0] == "ok" and vrow[2] == "ok" and all(
            x == "ok" for _, x in comp_rows + prov_rows + spawn_rows)
        raw_found = any(x == "fail"
                        for _, x in comp_rows + prov_rows + spawn_rows)
        code, category = checker_code(rel)
        if code == "accept":
            # `formal-strict`: the checker ACCEPTS the file but the shaped
            # model does not — the model is stricter than the fragment it
            # covers, which is a finding to chase, not a licence to relax it.
            record("agree-accept" if formal_clean else "formal-strict", rel)
        elif code == "G4":
            record("agree-G4" if raw_found else "missed-G4", rel)
        elif code in ("G2", "G3"):
            manifest_fail = vrow[0] == "fail" or vrow[2] == "fail"
            record(f"agree-{code}" if manifest_fail else f"missed-{code}", rel)
        else:
            record("out-of-fragment" if formal_clean else "formal-found-other",
                   rel)

    # Files with no composition to model, and files revl refused at parse:
    # named, not omitted. Neither carries a computed verdict, so neither can
    # agree or disagree with the model — but the code the checker gives them
    # is reported, which is how `g1_template_undeclared.rvl` (a G1 the model
    # never sees) and `g4_missing_undo.rvl` (the shape G4 forbids) stop being
    # invisible.
    nm_codes: dict[str, list[str]] = {}
    for rel in componentless:
        nm_codes.setdefault(checker_code(rel)[0], []).append(rel)

    total = sum(align.values())
    print(f"checker alignment ({total} modeled files, informational except "
          f"{'/'.join(FATAL_BUCKETS)}):")
    for k in sorted(align):
        mark = "  FATAL" if k in FATAL_BUCKETS and align[k] else ""
        print(f"  {k:20} {align[k]}{mark}")
    for k in ("formal-strict", "formal-found-other", *FATAL_BUCKETS):
        for rel in samples.get(k, []):
            print(f"  ALIGN {k}: {rel}")

    print(f"no-manifest ({len(componentless)} files parsed with no component, "
          f"outside the model's fragment):")
    for code in sorted(nm_codes):
        names = sorted(nm_codes[code])
        print(f"  checker={code:8} {len(names)}")
        if code != "accept":
            # An ACCEPTed componentless file is a backend emit corpus with no
            # composition in it — nothing to say. A REFUSED one is a rejection
            # fixture whose guarantee the model never gets to see, which is
            # the interesting half, so those are named here in full.
            for rel in names:
                print(f"    NO-MANIFEST {code}: {rel}")
    full = FORMAL / "harness" / "out" / "no_manifest.txt"
    full.write_text("".join(
        f"{code}\t{rel}\n" for code in sorted(nm_codes)
        for rel in sorted(nm_codes[code])), encoding="utf-8")
    print(f"  (complete list: {full.relative_to(FORMAL)})")

    return [f"{k}: {rel}" for k in FATAL_BUCKETS for rel in samples.get(k, [])]


def main() -> int:
    tsv, file_facts, census = export()
    refusals: dict[str, str] = census["refusals"]
    componentless: list[str] = census["componentless"]
    print(
        f"corpus census: {census['files']} .rvl files, "
        f"{census['components']} components, {census['statements']} statements "
        f"= {len(file_facts)} modeled + {len(componentless)} componentless "
        f"+ {len(refusals)} refused at parse"
    )
    # Skips are LISTED, not counted (item 418 step 7). Counting them is how
    # `g4_missing_undo.rvl` — literally the shape G4 forbids — and both G6
    # fixtures sat inside a "(28 parse-error skips, loud)" parenthesis.
    for rel in sorted(refusals):
        print(f"  REFUSED-AT-PARSE {refusals[rel]:8} {rel}")
    if not tsv:
        print("differential oracle: nothing extracted — nothing to diff")
        return 0

    out_dir = FORMAL / "harness" / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    tsv_path = out_dir / "corpus.tsv"
    tsv_path.write_text("\n".join(tsv) + "\n", encoding="utf-8")

    formal_text = run_oracle(tsv_path, out_dir / "formal_verdicts.tsv")
    if formal_text is None:
        return 0
    formal = parse_verdicts(formal_text)
    ref = reference_from_tsv(tsv)

    mismatches: list[str] = []
    for label, refmap, gotmap in (
            ("file", ref.files, formal.files),
            ("comp", ref.comps, formal.comps),
            ("provider", ref.providers, formal.providers),
            ("spawn", ref.spawns, formal.spawns),
            ("refusal", ref.refused, formal.refused),
            ("teardown", ref.dispositions, formal.dispositions),
            ("recovery", ref.recoveries, formal.recoveries),
            ("confinement", ref.confinements, formal.confinements),
            ("g8_surface", ref.g8surface, formal.g8surface),
            ("g5_registration", ref.g5reg, formal.g5reg)):
        for key, want in refmap.items():
            got = gotmap.get(key)
            if got is None:
                mismatches.append(f"{label} {key}: no formal row")
            elif got != want:
                mismatches.append(f"{label} {key}: reference={want} formal={got}")
    compared = ref.total()
    print(
        f"differential oracle: {compared} verdicts compared "
        f"({len(ref.files)} files + {len(ref.comps)} comps + "
        f"{len(ref.providers)} methods + {len(ref.spawns)} spawns + "
        f"{len(ref.refused)} parse refusals + "
        f"{len(ref.dispositions)} teardowns + "
        f"{len(ref.recoveries)} recoveries + "
        f"{len(ref.confinements)} confinements + "
        f"{len(ref.g8surface)} surfaces + "
        f"{len(ref.g5reg)} teardowns) — "
        f"{compared - len(mismatches)} agree, {len(mismatches)} mismatch(es)"
    )
    mismatches.extend(teardown_coverage(ref.dispositions))
    mismatches.extend(recovery_coverage(ref.recoveries))
    mismatches.extend(attenuation_coverage())
    mismatches.extend(confinement_coverage())
    mismatches.extend(prog_coverage())
    for m in mismatches[:10]:
        print(f"  MISMATCH {m}")
    if len(mismatches) > 10:
        print(f"  ... and {len(mismatches) - 10} more")

    fatal = checker_alignment(file_facts, componentless, formal)
    for f in fatal:
        print(f"  GATE-FAILURE {f}")
    return 1 if (mismatches or fatal) else 0


if __name__ == "__main__":
    sys.exit(main())
