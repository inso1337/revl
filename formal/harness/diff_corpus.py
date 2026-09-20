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
       `requires` bindings grant it, and its activation-body spawn edges;
     - a config field's declared TYPE, decomposed into the nodes it
       reaches and each node's data classification. That one is not
       about a crossing at all: the G4 guarantee also forbids a config
       field whose type can carry a live callable or a capability (item
       378), and with no type-shape fact the model could not see such a
       refusal at all (issue 1161).

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
   (`revl.compiler.compile_files`, the path the CLI takes, so a `use`
   resolves) and compare its refusal codes against the formal verdicts.
   Every DISAGREEING bucket is a gate failure (`FATAL_BUCKETS`): the
   `missed-*` ones because the checker refusing where the model sees
   nothing is the model being weaker than what revl enforces, and
   `formal-strict` / `formal-found-other` because the model refusing what
   revl accepts, or for a reason revl does not give, is a claim about a
   different language than the one that ships (issue #1169). The
   `out-of-fragment-G5` and `out-of-fragment-G6` buckets record an ABSENCE,
   which cannot disagree with anything, so they are gated on MEMBERSHIP
   instead: `formal/out_of_fragment_ledger.json` names the files in each,
   shrinks only, and a file joining one without a line in it fails the
   gate. Only `agree-*` and the generic `out-of-fragment` are purely
   informational.
5. render the census and those buckets into `formal/STATUS.md` between the
   `GENERATED alignment` markers, and fail the gate when the checked-in
   block is not what this run produced. The document's "0 formal-strict"
   is then this run's own output rather than a sentence somebody typed.

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
import textwrap
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
from revl.compiler import compile_files
from revl.diagnostics import classify
from revl.errors import RevlError
from revl.typecheck import _HOST_ACQUIRE_VERBS  # the shipped acquire-verb table
from revl.typecheck import parse_type  # the shipped type-head splitter
# The config-is-data tables (item 378), imported rather than restated: the
# classification a `CN` node carries is the SHIPPED checker's, so the harness
# cannot drift from it by spelling a head into the wrong bucket.
from revl.typecheck import (
    _CONFIG_DATA_CONTAINERS,
    _CONFIG_DATA_SCALARS,
    _CONFIG_ERASED,
    FN_HEAD,
    _is_type_expression,
    structural_fields,
)
from revl.taint import strip_qualifiers  # the shipped qualifier normalization
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
    RouteStmt,
    SpawnExpr,
    StreamIterStmt,
    TimerStmt,
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


# A crossing has TWO names, and they live in different namespaces. The
# exporter ships both, because the two surfaces that read a crossing disagree
# about which one names it:
#
#   * the BOUND rule (`P`) asks whether a provide method stayed inside its own
#     service's `emission[...]` declaration, and the reference names the
#     crossing there by the WIRING KEY it went through — "`Cache.put` is
#     declared `emission[db]`, but this implementation emits through `bus`"
#     (`examples/rejections/g4_capability_not_declared.rvl`). `_canon_cap`
#     below is that namespace and is right for it;
#   * the ATTENUATION fold (`W`) compares a parent's grant against a child's
#     demand ACROSS a component boundary, and two components wire the same
#     boundary under whatever key each likes. `covers` clause 1 is a boundary
#     IDENTITY test, so the fold element must be the DECLARED token — exactly
#     what `lower._cap_keyed` says, and what `_declared_cap` builds here.
#
# Spelling both sides of the attenuation fold in the key namespace is a
# LAUNDERING hole: `Supervisor requires kv: KvA` spawning
# `Leaker requires kv: KvB` reaches a different boundary under the same key,
# and the fold saw `kv` on both sides and derived no widening
# (`tests/formal_corpus/g4_spawn_widens_capability_same_key.rvl`).
_WIRE_NS = "key:"


def _wire_cap(key: str) -> str:
    """`lower._wire_cap`: the attenuation-fold element for a wiring key that no
    declaration tokens (a bare `emission`, a plain or unresolvable service).
    The key gets its OWN namespace so a key spelling can never masquerade as a
    declared token, and so two such boundaries still compare by name."""
    return _WIRE_NS + key


def _declared_cap(declared: str) -> str:
    """`lower._cap_keyed`: the attenuation-fold element for a crossing that a
    declaration DOES token. The boundary is the declared token and its
    valuation, never the local wiring key it was reached through, canonicalized
    by the checker's own parser (so `net(requests=100)` and `net(calls=100)`
    are one element and not two).

    A malformed stored spelling degrades to the unnameable `*`, fail-closed on
    both sides exactly as the bridge does: covered by nothing as a reach
    element, covering nothing but `*` as a held one."""
    try:
        return parse_cap(declared).to_str()
    except cap_order.CapError:
        return "*"


def _canon_cap(root: str, declared: str) -> str:
    """The BOUND namespace (`P` row only — see the note above): token the
    wiring key, params from the declared valuation, so a declaring
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


def _reach_call(node: ExprCall, out: "set[tuple[str, str]]", region: str,
                requires: dict, handles: dict, psvc: dict, bounds: dict,
                em_set: set, emitting: set, aliases: dict | None,
                externs: "set[str] | None") -> None:
    """The crossing ONE call head contributes (see `walk_reach`). The
    arguments are the caller's to walk, under whatever region encloses them:
    a call evaluated to produce an argument is not the marked crossing."""
    rt = _route(node.callee)
    if not rt:
        return
    root, chain = rt
    res = _resolve_emission(root, chain, requires, handles, psvc, aliases)
    if res is not None and region == "all":
        svc, meth = res
        if (svc, meth) in em_set:
            if root in handles or (aliases and root in aliases):
                out.add(("*", "*"))
            else:
                mode, entries = bounds[(svc, meth)]
                if mode == "any":
                    # No declared token: the wiring key names the
                    # boundary, in its own namespace for the fold and
                    # bare for the bound.
                    out.add((_wire_cap(root), root))
                else:
                    for e in entries:
                        out.add((_declared_cap(e), _canon_cap(root, e)))
    elif res is None and region == "all" and root in emitting:
        # A host emission. The two namespaces part company here (#1169 F3):
        # the attenuation fold gives it the unnameable `*` whatever the
        # extern is called (`_emit_step_caps_pairs`: a non-`req` target is
        # `Cap("*")`), but the provide-method BOUND names a DIRECT emission
        # extern by the extern — `_emitting_capabilities` seeds the fixed
        # point with `{wire}` for `extern emission fn wire`, and
        # `_method_emissions` measures that name against the declared
        # `emission[...]` entries, which is why `Db.execute` can be declared
        # `emission[wire, ...]` at all. A transitively-emitting named fn
        # stays `*` on both sides (STATUS.md, "known fidelity limits").
        bound = root if externs is not None and root in externs else "*"
        out.add(("*", bound))


def walk_reach(node, out: "set[tuple[str, str]]", region: str, requires: dict,
               handles: dict, psvc: dict, bounds: dict, em_set: set,
               emitting: set, aliases: dict | None = None,
               externs: "set[str] | None" = None) -> None:
    """Collect the emission caps `node` crosses, each as the PAIR
    `(attenuation spelling, bound spelling)` — the two namespaces a crossing
    has (see `_canon_cap` / `_declared_cap`). The caller keeps whichever half
    its surface reads; nothing downstream has to re-derive the other.

    `region` is "emit-step" (count only MARKED crossings — the attenuation
    surface, like `_collect_emit_caps_pairs`) or "all" (also count any
    resolved emission call — a provide method's reach for the bound, like
    `_method_emissions.walk`). A spawn-handle emission is the unnameable
    `*` in both namespaces; an emitting-fn call too; a DIRECT emission-extern
    call is `*` for the fold and the extern's name for the bound
    (`_reach_call`). `externs` is the file's emission-extern name set.

    An `emit` marks its HEAD call only: `_emit_step_caps_pairs` reads the
    step's `expr.target` and nothing beneath it, so the arguments (and a
    `compensate` slot or `with` clause) keep the ENCLOSING region. For the
    F row that region is already "all" and nothing moves; for the A surface
    it stops a call evaluated inside an emit's argument list from counting
    as a marked crossing (#1169 F2, the `walk_calls` leak's twin)."""
    if node is None or isinstance(node, (str, int, float, bool)):
        return
    args = (requires, handles, psvc, bounds, em_set, emitting, aliases, externs)
    if isinstance(node, (EmitStmt, EmitExpr)) and not isinstance(node, type):
        expr = getattr(node, "expr", None)
        if isinstance(expr, ExprCall):
            _reach_call(expr, out, "all", *args)
            for a in expr.args:
                walk_reach(a, out, region, *args)
        else:
            walk_reach(expr, out, "all", *args)
        if dataclasses.is_dataclass(node):
            for f in dataclasses.fields(node):
                if f.name != "expr":
                    walk_reach(getattr(node, f.name), out, region, *args)
        return
    if isinstance(node, ExprCall):
        _reach_call(node, out, region, *args)
        for a in node.args:
            walk_reach(a, out, region, *args)
        return
    if dataclasses.is_dataclass(node) and not isinstance(node, type):
        for f in dataclasses.fields(node):
            walk_reach(getattr(node, f.name), out, region, *args)
        return
    if isinstance(node, (list, tuple)):
        for x in node:
            walk_reach(x, out, region, *args)


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

    ctx is 'emit' for the HEAD call an emit marks, 'emitarg' for a call
    evaluated inside that head's argument list (judged as a plain position:
    one marker covers one crossing, issue #1175), 'emitnested' for the head
    of an `emit` EXPRESSION written inside that argument list (refused
    outright: the marker admits one crossing), 'plain' everywhere else — including
    under `effect ... undo ...`: the g4_unmarked_emission fixture shows the
    checker refuses an emission call whose pairing is an inverse, because a
    boundary crossing cannot be reverted by pairing. Only `emit` legalizes
    an emission, and (two-sided) `emit` around a non-emission method is
    itself a refusal ('emission not declared')."""
    if node is None or isinstance(node, (str, int, float, bool)):
        return
    if isinstance(node, (EmitStmt, EmitExpr)):
        if dataclasses.is_dataclass(node) and not isinstance(node, type):
            # The marker JUDGES the head call (`_lower_emit_step` asks
            # `_is_emission_call` of the lowered expression's own node) and
            # covers that call alone (issue #1175): the head's arguments
            # lower in the mode the `emit` sits in (`lower._emit_head_args`),
            # so a call evaluated to build an argument is judged as a plain
            # position is. An unmarked `b.fetch()` emission inside
            # `emit a.send(...)` is refused for its missing marker, and
            # `ranking.strategy()` inside NotesConsole's `emit
            # webui.add_entry(...)` compiles because `Ranker` declares it
            # plain (#1169 F2 handed `emit` to the whole subtree and refused
            # it). `emitarg` names that position in the U row so the fact
            # stays readable; the rule judges it exactly as `plain`.
            # A marker written inside another marker's argument list is the
            # shape `lower._refuse_nested_emit` refuses before it judges the
            # inner call: its head is recorded as `emitnested`, a violation
            # whatever the method declares.
            head_ctx = "emitnested" if ctx == "emitarg" else "emit"
            expr = getattr(node, "expr", None)
            if isinstance(expr, ExprCall):
                route = _route(expr.callee)
                if route is not None:
                    out.append((*route, head_ctx))
                for a in expr.args:
                    walk_calls(a, out, "emitarg")
            else:
                walk_calls(expr, out, "emit")
            for f in dataclasses.fields(node):
                if f.name != "expr":
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
    per-realm at each leg rather than pinning one realm, which the model's
    one-realm-per-key `LComponent.realm` cannot express. Its legs are not
    modeled: the routed REQUIREMENT is elided from the V row's manifest on
    both sides (`Oracle.toLComponent`, `reference_from_tsv`) rather than
    mis-spelled into the shared realm, where it would read as the
    component's own provision (a phantom G3 self-provision), and the route
    is carried only as the A9 installation fact (`PR`).
    `tests/formal_corpus/a9_routes_installs_key.rvl` is the corpus file that
    uses one."""
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



# ---------------------------------------------------------------- config-data
#
# The THIRD rule under the G4 guarantee, and the first that is not about a
# crossing at all. `g4OK` (the marker rule) and `hostAcquireOK` (the acquire
# rule) both judge something a body DOES; config-is-data (item 378,
# `typecheck.check_config_field_is_data`) judges a config field's declared
# TYPE — a config value is injected as static data at plug/spawn/load time, so
# its type must be built, transitively, out of data. An arrow field is a live
# callable invoked past every authority fold; a `service` field is a capability
# handed over with no wiring at all.
#
# The model had no type-shape facts, so a refusal of this class was invisible
# to it and reported as `missed-G4` — fatal — wherever a fixture for it was
# placed (issue 1161). The export now decomposes a config field's declared type
# the way `Z`/`Y` decompose a capability: the SHIPPED tables and the shipped
# splitter classify each node the type reaches, and the JUDGMENT — that every
# reached node is a data form — is stated on both verdict sides.
#
# The classification is an ALLOWLIST on both sides, matching the checker's own
# discipline (`_walk_config_type` refuses a head that is not *provably* data
# rather than denying two known-bad ones). A form neither side has heard of is
# therefore refused, which is the SAFE direction: the model can only become
# stricter than the checker, never blind to one of its refusals.
CONFIG_DATA_FORMS = frozenset(
    {"scalar", "container", "record", "variant", "struct", "tparam"})


def config_type_defs(prog) -> dict[str, dict]:
    """The lightweight type table `check_config_field_is_data` resolves nominal
    heads through — `lower._check_config`'s own construction, clause for
    clause, so a record/ADT/alias resolves here exactly as it does there."""
    out: dict[str, dict] = {}
    for decl in prog.type_decls:
        if decl.fields:
            out.setdefault(
                decl.name,
                {"kind": "record", "params": list(decl.params or ()),
                 "fields": {f.name: f.type for f in decl.fields}})
        else:
            out.setdefault(
                decl.name,
                {"kind": "variant", "params": list(decl.params or ()),
                 "cases": [{"name": c.name, "payload": c.payload}
                           for c in decl.cases]})
    return out


def config_shape(type_name: str | None, *, service_names: set[str],
                 type_defs: dict, visited: frozenset = frozenset(),
                 tparams: frozenset = frozenset(),
                 out: "list[tuple[str, str]] | None" = None
                 ) -> list[tuple[str, str]]:
    """Every node `type_name` reaches, as `(form, spelling)` in walk order.

    A transcription of `typecheck._walk_config_type` with one difference: the
    checker RAISES at the first offender, and this walk records it and carries
    on with its siblings. The two are equivalent for the verdict — "some node
    is not a data form" is exactly "the checker's descent raises somewhere" —
    and recording all of them makes the fact set independent of the order the
    descent happens to take.

    An offender is never descended into, which the checker does not do either
    (it has already raised), so the walk terminates on the same `visited`
    guard the checker uses for a recursive type."""
    if out is None:
        out = []
    # `taint.extract_and_normalize` runs before the checker and STRIPS every
    # `Secret[T]`/`Untrusted[T]`/`Trusted[T]`/`Retained[T, p]` qualifier off a
    # declared type in place, so `check_config_field_is_data` is handed the
    # bare type and `config { api_key: Secret[Str] }` is a `Str` field by the
    # time it is judged. This export parses the corpus and does NOT run the
    # taint pass, so it applies that ONE normalization with the shipped
    # function — idempotent, and byte-identical on a type carrying no
    # qualifier. Without it, every `Secret[T]` config field in the tree would
    # read as an opaque head and the model would refuse three files revl
    # accepts.
    type_name = strip_qualifiers(type_name)
    if not type_name:
        return out
    type_name = type_name.strip()

    def walk(target, *, visited=visited, tparams=tparams):
        config_shape(target, service_names=service_names, type_defs=type_defs,
                     visited=visited, tparams=tparams, out=out)

    sfields = structural_fields(type_name)
    if sfields is not None:
        out.append(("struct", type_name))
        for ftype in sfields.values():
            walk(ftype)
        return out
    head, args = parse_type(type_name)
    if head == FN_HEAD:
        out.append(("arrow", type_name))
        return out
    if head in service_names:
        out.append(("service", type_name))
        return out
    if head in tparams:
        out.append(("tparam", type_name))
        return out
    if head in _CONFIG_DATA_CONTAINERS:
        out.append(("container", type_name))
        for arg in args:
            walk(arg)
        return out
    if head in _CONFIG_DATA_SCALARS:
        out.append(("scalar", type_name))
        for arg in args:
            walk(arg)
        return out
    info = type_defs.get(head or "")
    if info is None:
        # Nothing here proves the field is data. The checker splits the
        # diagnostic between an erased head (`Any`/`Value`/`Never`, each a
        # `compatible` wildcard in some direction) and any other unresolvable
        # one; both refuse, and the two forms are kept apart so the fact says
        # WHICH shape reopened the hole.
        out.append(("erased" if head in _CONFIG_ERASED else "opaque",
                    type_name))
        return out
    out.append((info.get("kind") or "variant", type_name))
    if head not in visited:
        child_visited = visited | {head}
        child_tparams = frozenset(info.get("params") or ())
        if info.get("kind") == "record":
            for ftype in (info.get("fields") or {}).values():
                walk(ftype, visited=child_visited, tparams=child_tparams)
        else:
            for case in info.get("cases") or []:
                payload = case.get("payload")
                if payload is not None:
                    walk(payload, visited=child_visited, tparams=child_tparams)
                    continue
                name = case.get("name")
                if _is_type_expression(name, type_defs, child_tparams):
                    walk(name, visited=child_visited, tparams=child_tparams)
    # A user generic head carries data in its type arguments too; walked with
    # the OUTER type-parameter scope, since they are written at this use site.
    for arg in args:
        walk(arg)
    return out


def config_rows(prog, rel: str) -> list[str]:
    """`CF` (a config field is declared) + `CN` (one node its type reaches)
    for every config field in the file — a component's and an extern's, the
    two `lower._check_config` is called for."""
    svc_names = {svc.name for svc in prog.services}
    tdefs = config_type_defs(prog)
    owners = [("component", c.name, c.config) for c in prog.components]
    owners += [("extern", e.name, e.config or ()) for e in prog.externs]
    rows: list[str] = []
    for kind, owner, fields in owners:
        for cfg in fields:
            rows.append("\t".join(
                ["CF", rel, kind, owner, cfg.name, cfg.type or "-"]))
            nodes = config_shape(cfg.type, service_names=svc_names,
                                 type_defs=tdefs)
            for i, (form, spelling) in enumerate(nodes):
                rows.append("\t".join(
                    ["CN", rel, kind, owner, cfg.name, str(i), form,
                     spelling]))
    return rows


def _a2_step(stmt: object) -> str:
    """One activation-body statement as the A2 rule sees it (issue 1166):
    the four statement forms `lower._dispatch_action` refuses once
    `provide_seen_line` is set are `acquire`; a `provide` block is what sets
    it; everything else moves nothing. A component `if` arm admits only
    `fail` and nested `if` (`_lower_component_guard_stmts`), so the body is
    flat for this rule and no acquisition can hide inside an arm."""
    if isinstance(stmt, (LetEffect, EffectStmt, TimerStmt, StreamIterStmt)):
        return "acquire"
    if isinstance(stmt, ProvideStmt):
        return "provide"
    return "other"



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
        # The DIRECT emission externs, for the F row's bound column: the one
        # host crossing the reference can name (`_reach_call`).
        emission_externs = {e.name for e in prog.externs
                            if getattr(e, "classification", "") == "emission"}
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

        # config-is-data facts (CF/CN), file-wide: the declared config fields
        # and, decomposed by the shipped tables, the type nodes each one
        # reaches. An extern's config is judged at the same bar as a
        # component's, so both owners ship rows (issue 1161).
        tsv.extend(config_rows(prog, rel))

        ff: dict = {"components": {}}
        for c in prog.components:
            # A routed requirement (`isolate k in realms(...)`, item 162) is
            # exported as a `PR` fact below. M stays the faithful manifest;
            # it is the V-row MODEL on both sides that elides a routed
            # requirement (`Oracle.toLComponent`, `reference_from_tsv`),
            # because the linker resolves it per leg and never through the
            # single-realm table.
            routed = [stmt.key for stmt in c.body if isinstance(stmt, RouteStmt)]
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
            # PB: one row per installed provide BLOCK, in body order (issue
            # 1167). C above reads the `provides` CLAUSE; A9 is the rule that
            # the two agree, so the A9 row needs the block as its own fact —
            # read off the same AST node `lower._lower_provide` refuses on.
            # A double install is a repeated row, not a collapsed one.
            for stmt in c.body:
                if isinstance(stmt, ProvideStmt):
                    tsv.append("\t".join(["PB", rel, c.name, stmt.key]))
            # PR: one row per `isolate k in realms(...)` bind (issue #1172).
            # The converse of A9 exempts a routed key from needing a block,
            # so the exemption is exported as DATA off the `RouteStmt` the
            # checker records into `routes`, never inferred here.
            for key in routed:
                tsv.append("\t".join(["PR", rel, c.name, key]))

            # require-held capability facts (K): the boundaries a requires
            # binding hands this component — the structured valuations of the
            # service's emission declarations (the held side of attenuation).
            # K feeds the attenuation fold and nothing else, so it carries the
            # ATTENUATION spelling only: the declared token where the service
            # declares one, the namespaced wiring key where it does not
            # (`lower._held_capabilities_pairs`, clause for clause).
            krows: list[tuple[str, str]] = []
            for local, svc in requires:
                em = [(mode, ents) for (s, _m), (mode, ents) in bounds.items()
                      if s == svc and mode != "plain"]
                if not em:
                    krows.append((local, _wire_cap(local)))
                    continue
                for mode, ents in em:
                    if mode == "any":
                        krows.append((local, _wire_cap(local)))
                    else:
                        for e in ents:
                            krows.append((local, _declared_cap(e)))
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
            # A feeds the attenuation fold and nothing else, so — like K — it
            # carries the attenuation spelling only.
            act_reach: "set[tuple[str, str]]" = set()
            for stmt in c.body:
                walk_reach(stmt, act_reach, "emit-step", require_map, handles,
                           psvc, bounds, em_set, emitting, aliases,
                           emission_externs)
            act_caps = {cap for cap, _bound in act_reach}
            caps_seen.update(act_caps)
            for cap in sorted(act_caps):
                tsv.append("\t".join(["A", rel, c.name, cap]))

            # host acquisition facts (HA): each host-family acquisition the
            # component's reachable code names, with the POSITION that decides
            # its legality. The model owns the verb table and states the rule
            # (issue 334); the exporter carries where each acquisition sits.
            for verb, position in _host_acquire_facts(c, fns_by_name):
                tsv.append("\t".join(["HA", rel, c.name, verb, position]))

            # provide-method reach (F): emission caps a method's body crosses
            # (all-call) — the bound check's left side and, with A, the
            # component surface for attenuation. It is the ONE row both
            # surfaces read, so it carries BOTH spellings: `cap` is the
            # attenuation element (the declared boundary) and `bound` is the
            # bound element (the wiring key the crossing went through). They
            # differ, and collapsing them is the laundering hole.
            for stmt in c.body:
                if isinstance(stmt, ProvideStmt):
                    svc = psvc.get(c.name, {}).get(stmt.key)
                    if svc is None:
                        continue
                    for pm in stmt.methods:
                        reach: "set[tuple[str, str]]" = set()
                        for inner in pm.body:
                            walk_reach(inner, reach, "all", require_map, handles,
                                       psvc, bounds, em_set, emitting, aliases,
                                       emission_externs)
                        for cap, bound in sorted(reach):
                            caps_seen.add(cap)
                            caps_seen.add(bound)
                            tsv.append("\t".join(
                                ["F", rel, c.name, stmt.key, svc, pm.name,
                                 cap, bound]))

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
                    # `emitarg` is judged as `plain` is: the marker covers the
                    # head call alone (issue #1175), so an emission evaluated
                    # inside the head's argument list needs its own marker,
                    # and a marker written there (`emitnested`) is refused.
                    bad = ctx == "emitnested" or (ctx == "emit") != em
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
            # body-step facts (AQ, issue 1166): the activation body in
            # order, one row per statement, each as the A2 rule sees it. The
            # oracle folds `RevL.A2.a2B` over them and the reference folds
            # the checker's rule over the same rows.
            for ord_, stmt in enumerate(c.body):
                tsv.append("\t".join(["AQ", rel, c.name, str(ord_),
                                       _a2_step(stmt)]))
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


#: non-vacuity ratchet for the A2 row (issue 1166). Filled by
#: `reference_from_tsv`, read by `a2_coverage`: `(file, comp) ->
#: (admitted, acquisitions, provisions)` for every component's body.
_A2_BODIES: dict = {}


def a2_coverage() -> list[str]:
    """The non-vacuity ratchet for the `A2` row (issue 1166).

    Same discipline as the ratchets above: a row that says `ok` over bodies
    with no provision, or no acquisition, certifies nothing about the
    ordering — the fold's flag is never set, or never tested. So the corpus
    must EXERCISE the rule on the reference's own fold:

      * some body is ADMITTED with at least one provision AND at least one
        acquisition — the ordinary `let x = effect … undo …; provide k { … }`
        shape, where the flag is set and every acquisition sits above it;
      * some body is REFUSED — an acquisition after the first `provide`
        (`examples/rejections/a2_acquire_after_provide.rvl`).

    A refused body is a `fail` on both sides (the oracle's `a2OKB` and this
    fold are the same rule), so the row bites through
    `RevL.A2.a2_not_vacuous` / `RevL.A2.fixture_refused`: the verdict flips
    between the two shapes, and a reference that drifted to accept the
    second would diverge from the Lean row here. Returns findings, treated
    as gate failures."""
    admitted = refused = None
    for key, (ok, n_acq, n_prov) in _A2_BODIES.items():
        if ok and n_acq > 0 and n_prov > 0:
            admitted = admitted or key
        if not ok:
            refused = refused or key
    findings: list[str] = []
    for label, witness in (
            ("an admitted body with both a provision and an acquisition",
             admitted),
            ("a body refused for an acquisition after a provision", refused)):
        if witness is None:
            findings.append(f"a2 coverage: NO witness of {label} — "
                            "the A2 row would agree vacuously")
    if not findings:
        print(f"a2 coverage: {len(_A2_BODIES)} bodies; "
              f"admitted={admitted} refused={refused}")
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
    rows (G5: an effect's teardown registration count), `a9` A9 rows (every
    installed provide block's key is declared in the `provides` clause, and
    every declared key is installed by a block or a `realms(...)` route),
    `configs` CD rows (G4 config-is-data: a config field's declared type is
    built out of data), `a2` A2 rows (A2: no acquisition after a provision in
    a component's activation body)."""
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
    a9: dict[tuple[str, str], str]

    configs: dict[tuple[str, str, str, str], str]
    a2: dict[tuple[str, str], str]


    def total(self) -> int:
        return (len(self.files) + len(self.comps) + len(self.providers)
                + len(self.spawns) + len(self.refused)
                + len(self.dispositions) + len(self.recoveries)
                + len(self.confinements) + len(self.g8surface)

                + len(self.g5reg) + len(self.a9) + len(self.configs)
                + len(self.a2))



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
    a9: dict[tuple[str, str], str] = {}

    configs: dict[tuple[str, str, str, str], str] = {}
    a2: dict[tuple[str, str], str] = {}

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
        elif parts[0] == "A9" and len(parts) == 4:
            # A9 provide-block declaration, both directions: (file, comp) ->
            # ok|fail.
            a9[(parts[1], parts[2])] = parts[3].split("=", 1)[1]

        elif parts[0] == "CD" and len(parts) == 6:
            # G4 config-is-data: (file, owner kind, owner, field) -> ok|fail.
            configs[(parts[1], parts[2], parts[3], parts[4])] = \
                parts[5].split("=", 1)[1]
        elif parts[0] == "A2" and len(parts) == 4:
            # A2 ordering: (file, comp) -> ok|fail.
            a2[(parts[1], parts[2])] = parts[3].split("=", 1)[1]
        else:
            raise SystemExit(f"differential oracle: malformed verdict row {line!r}")
    return Verdicts(files, comps, providers, spawns, refused, dispositions,
                    recoveries, confinements, g8surface, g5reg, a9, configs,
                    a2)



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
    (item 66/294). CD rows are PER-CONFIG-FIELD config-is-data: every node
    the field's declared type reaches must be a data form (item 378).
    X rows carry a parse refusal through."""
    rows = [r.split("\t") for r in tsv]
    mrows = [r for r in rows if r and r[0] == "M" and len(r) == 7]
    xrows = [r for r in rows if r and r[0] == "X" and len(r) == 3]
    urows = [r for r in rows if r and r[0] == "U" and len(r) == 7]
    brows = [r for r in rows if r and r[0] == "B" and len(r) == 5]
    qrows = [r for r in rows if r and r[0] == "Q" and len(r) == 5]
    arows = [r for r in rows if r and r[0] == "A" and len(r) == 4]
    frows = [r for r in rows if r and r[0] == "F" and len(r) == 8]
    krows = [r for r in rows if r and r[0] == "K" and len(r) == 5]
    srows = [r for r in rows if r and r[0] == "S" and len(r) == 4]
    harows = [r for r in rows if r and r[0] == "HA" and len(r) == 5]
    irows = [r for r in rows if r and r[0] == "I" and len(r) == 7]
    exrows = [r for r in rows if r and r[0] == "EX" and len(r) == 7]
    fnrows = [r for r in rows if r and r[0] == "FN" and len(r) == 5]
    pbrows = [r for r in rows if r and r[0] == "PB" and len(r) == 4]
    prrows = [r for r in rows if r and r[0] == "PR" and len(r) == 4]
    cfrows = [r for r in rows if r and r[0] == "CF" and len(r) == 6]
    cnrows = [r for r in rows if r and r[0] == "CN" and len(r) == 8]

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

    # Routed requirements (`PR` rows, item 162 binds) are resolved by the
    # linker per leg, never through the single-realm table; the V-row model
    # elides them from `requires` exactly as `Oracle.toLComponent` does.
    routed_by_comp: dict[tuple[str, str], list[str]] = {}
    for r in prrows:
        routed_by_comp.setdefault((r[1], r[2]), []).append(r[3])

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
        shaped = [([k for k in r[3].split(",")
                    if k and k not in routed_by_comp.get((rel, r[2]), [])],
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
        # U row: [U, file, comp, ctx, root, svc, meth]. A call inside an emit
        # head's argument list (`emitarg`) is judged as a plain one: the
        # checker's marker covers the head call alone (issue #1175), so only
        # the `emit` context legalizes an emission, and a marker written
        # inside the argument list (`emitnested`) is a violation outright.
        raw = any(
            u[2] == compn
            and (u[3] == "emitnested"
                 or ((u[3] == "emit") != ((u[5], u[6]) in ems)))
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
    # The BOUND surface reads the F row's bound spelling (column 8): the
    # reference names a crossing by the wiring key it went through when it
    # measures a provide method against its own service's declaration.
    fmethods: dict[tuple[str, str, str, str, str], set[str]] = {}
    for r in frows:
        fmethods.setdefault((r[1], r[2], r[3], r[4], r[5]), set()).add(r[7])
    for k, caps in fmethods.items():
        mode, ents = bounds_by_file.get((k[0], k[3], k[4]), ("plain", set()))
        if mode == "any":
            ok = True
        elif mode == "plain":
            ok = not caps
        else:
            ok = {parse_cap(c).token for c in caps} <= ents
        providers[k] = "ok" if ok else "fail"

    # ... and the ATTENUATION surface reads the same row's column 7, the
    # declared boundary. A and K carry that spelling and no other.
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

    # CD verdicts (G4 config-is-data, issue 1161). The rule is the ALLOWLIST
    # and nothing else: a config field is data iff every node its declared type
    # reaches is a data form. The decomposition is the exporter's (the shipped
    # tables did the classifying); the judgment is stated here and, separately,
    # in `Oracle.configDataOK`.
    config_nodes: dict[tuple[str, str, str, str], list[str]] = {}
    for r in cnrows:
        config_nodes.setdefault((r[1], r[2], r[3], r[4]), []).append(r[6])
    configs: dict[tuple[str, str, str, str], str] = {}
    for r in cfrows:
        key = (r[1], r[2], r[3], r[4])
        forms = config_nodes.get(key, [])
        configs[key] = ("ok" if all(f in CONFIG_DATA_FORMS for f in forms)
                        else "fail")
        _CONFIG_FIELDS[key] = (configs[key], tuple(forms))

    # A9 rows (issues 1167 / #1172), both directions: every installed provide
    # BLOCK's key is declared in the `provides` CLAUSE, and every declared
    # key is installed by a block or by a `realms(...)` route. The clause
    # comes off the M row, the blocks off the PB rows and the routes off the
    # PR rows — three facts the exporter reads off three different AST nodes
    # — so this is membership between lists, recomputed here without the
    # Lean side's `Installed` structure. One row per component that declares
    # or installs anything: a component with neither would agree vacuously.
    _A9_ROWS.clear()
    provides_by_comp: dict[tuple[str, str], list[str]] = {}
    for r in mrows:
        provides_by_comp[(r[1], r[2])] = [k for k in r[4].split(",") if k]
    blocks_by_comp: dict[tuple[str, str], list[str]] = {}
    for r in pbrows:
        blocks_by_comp.setdefault((r[1], r[2]), []).append(r[3])
    a9: dict[tuple[str, str], str] = {}
    for key in sorted(set(provides_by_comp) | set(blocks_by_comp)):
        declared = provides_by_comp.get(key, [])
        blocks = blocks_by_comp.get(key, [])
        routed = routed_by_comp.get(key, [])
        if not declared and not blocks:
            continue
        undeclared = [k for k in blocks if k not in declared]
        uninstalled = [k for k in declared if k not in blocks and k not in routed]
        a9[key] = "ok" if not (undeclared or uninstalled) else "fail"
        _A9_ROWS[key] = (not undeclared, not uninstalled, len(blocks), len(routed))

    # A2 rows (no acquisition after a provision, issue 1166), recomputed
    # INDEPENDENTLY from the AQ rows: the checker's own rule
    # (`lower._dispatch_action`) folded over the body in index order — a flag
    # set at the first `provide`, an `acquire` refused while it is set. One
    # verdict per component the M rows name, so a body with no statements is
    # a (vacuous) `ok` on both sides rather than a missing row.
    aqrows = [r for r in rows if r and r[0] == "AQ" and len(r) == 5]
    bodies: dict[tuple[str, str], list[tuple[int, str]]] = {}
    for r in aqrows:
        bodies.setdefault((r[1], r[2]), []).append((int(r[3]), r[4]))
    _A2_BODIES.clear()
    a2: dict[tuple[str, str], str] = {}
    for r in mrows:
        key = (r[1], r[2])
        seen = False
        ok = True
        n_acq = n_prov = 0
        for _ord, kind in sorted(bodies.get(key, [])):
            if kind == "provide":
                seen = True
                n_prov += 1
            elif kind == "acquire":
                n_acq += 1
                if seen:
                    ok = False
        a2[key] = "ok" if ok else "fail"
        _A2_BODIES[key] = (ok, n_acq, n_prov)

    return Verdicts(files, comps, providers, spawns, refused, dispositions,

                    recoveries, confinements, g8surface, g5reg, a9,
                    configs, a2)


#: What the REFERENCE decided for each config field, for the CD row's
#: non-vacuity ratchet: (verdict, the forms its type reached). Filled by
#: `reference_from_tsv`, read by `config_coverage`. Evidence that the row
#: BITES, not a claim either side makes — so it is kept beside the compared
#: verdict rather than inside it, the same way `_CONFINEMENTS` is.
_CONFIG_FIELDS: dict = {}


def config_coverage() -> list[str]:
    """The non-vacuity ratchet for the `CD` row (G4 config-is-data, 1161).

    Every config field in the corpus belongs to a file somebody wrote to
    compile, so a row that only ever said `ok` would agree over nothing — the
    same vacuity `attenuation_coverage` and `confinement_coverage` guard. This
    states, and enforces, that the corpus exercises BOTH verdicts, and that the
    admitting side is not trivial either: an `ok` over an empty node list would
    certify nothing about the walk."""
    findings: list[str] = []
    if not _CONFIG_FIELDS:
        return ["config coverage: no CD rows at all — the row is vacuous"]
    admitted = [k for k, (v, _f) in _CONFIG_FIELDS.items() if v == "ok"]
    refused = [k for k, (v, _f) in _CONFIG_FIELDS.items() if v == "fail"]
    nonempty = [k for k, (v, f) in _CONFIG_FIELDS.items() if v == "ok" and f]
    if not refused:
        findings.append("config coverage: NO refused config field — the CD "
                        "row would agree vacuously")
    if not nonempty:
        findings.append("config coverage: NO admitted config field whose type "
                        "reaches a node — the walk is never exercised")
    if not findings:
        forms = sorted({f for _v, fs in _CONFIG_FIELDS.values() for f in fs})
        print(f"config coverage: {len(_CONFIG_FIELDS)} config fields, "
              f"{len(admitted)} data / {len(refused)} refused; "
              f"forms={','.join(forms)}")
    return findings



#: What the REFERENCE computed for each A9 row, for the non-vacuity ratchet:
#: (every block declared, every declared key installed, block count, routed
#: count). Filled by `reference_from_tsv`; read by `a9_coverage`. Evidence the
#: row BITES, not a claim either side makes.
_A9_ROWS: dict = {}


def a9_coverage() -> list[str]:
    """The non-vacuity ratchet for the `A9` row (issues 1167 / #1172).

    Same discipline as `confinement_coverage`: a row every corpus component
    satisfies certifies nothing. So the corpus must carry every verdict the
    rule can give, on the reference's own computation:

      * some component installs at least one block and is admitted — the
        `ok` that is a real check in direction 1;
      * some component installs a block whose key the clause never declared
        — the refused shape of direction 1
        (`examples/rejections/a9_provide_key_not_declared.rvl`);
      * some component declares a key that no block and no route installs,
        with every block it does install declared — the refused shape of
        direction 2 ALONE (`examples/rejections/a9_provides_without_block.rvl`);
      * some component is admitted with a ROUTED key — the exemption
        exercised (`tests/formal_corpus/a9_routes_installs_key.rvl`), without
        which the `PR` fact could be dropped and nothing would move.

    Returns findings, which the caller treats as gate failures.
    """
    admitted = undeclared = uninstalled = routed = None
    for key, (blocks_ok, declared_ok, n_blocks, n_routed) in _A9_ROWS.items():
        ok = blocks_ok and declared_ok
        if ok and n_blocks > 0:
            admitted = admitted or key
        if not blocks_ok:
            undeclared = undeclared or key
        # Direction 2 ALONE: the first fixture fails both directions (its
        # `skin1` is declared and uninstalled too) and must not stand in for
        # the shape whose only defect is a declared key nothing installs.
        if blocks_ok and not declared_ok:
            uninstalled = uninstalled or key
        if ok and n_routed > 0:
            routed = routed or key
    findings: list[str] = []
    for label, witness in (
            ("a component installing a block under a declared key", admitted),
            ("a component installing a block the clause never declared",
             undeclared),
            ("a component declaring a key nothing installs", uninstalled),
            ("a component admitted with a routed key and no block", routed)):
        if witness is None:
            findings.append(f"a9 coverage: NO witness of {label} — "
                            "the A9 row would agree vacuously")
    if not findings:
        print(f"a9 coverage: {len(_A9_ROWS)} declaring/installing components; "
              f"admitted={admitted} undeclared={undeclared} "
              f"uninstalled={uninstalled} routed={routed}")
    return findings


# The buckets that are GATE FAILURES, not findings (item 418 step 7).
#
# The `missed-*` half is the DANGEROUS direction: the real checker REFUSES a
# file and the model sees nothing wrong with it, so the model is weaker than
# what revl enforces and the "the model agrees with the checker" claim would
# be false. `missed-A9` (issues 1167 / #1172) is that direction for the
# provide-block rule, `missed-A2` (issue 1166) for the A2 ordering rule, and
# `missed-G5` (issue #1169 F4) for a teardown crossing the `Prog` CAN resolve.
#
# `formal-strict` and `formal-found-other` were informational until issue
# #1169, and that is how three files sat in them for a year: agreement failed
# loudly, strictness did not, so nobody read them. They are the OTHER
# direction — the model refusing what revl accepts (`formal-strict`), or
# refusing a file revl refuses for a different reason (`formal-found-other`) —
# and that direction is not harmless: a model stricter than the checker is a
# model of a different language, and every theorem proved over it is proved
# about that other language. Both are 0 on the corpus, so both are fatal; a
# genuine fragment gap has `out-of-fragment*` to land in, which is the bucket
# that says "the model has no fact here" rather than "the model disagrees".
FATAL_BUCKETS = ("missed-G4", "missed-G2", "missed-G5", "missed-A9",
                 "missed-A2", "formal-strict", "formal-found-other")


def checker_code(rel: str) -> tuple[str, str]:
    """The shipped checker's verdict on one corpus file: `("accept", "")`, or
    the refusal's `(code, category)`.

    Asked through `compile_files`, the path `revl check` and every other CLI
    verb take, so a `use "stdlib/http.rvl"` resolves against the file's own
    directory and the search path. `compile_source(text, rel)` reads a bare
    string and refuses ANY `use` before checking a thing (`REVL`: "`use`
    declarations need `modules=` ... or compile_files"), so a use-bearing
    file was filed under a refusal that says nothing about its composition,
    and whatever the model said about it sank into `formal-found-other`
    (#1169 F1). The same door resolves an extern body file, a `ref` and an
    `asset`, which the bare-string door refuses for the same reason."""
    try:
        compile_files([str(REPO / rel)])
        return "accept", ""
    except RevlError as e:
        info = classify(e)
        return (info.get("code") or "UNCODED"), (info.get("category") or "")


#: The last `checker_alignment` run's buckets and per-bucket file lists, for
#: the STATUS.md renderer. Filled by `checker_alignment`, read by
#: `status_block`: the document's numbers are this run's own output, never a
#: second count.
_ALIGN: dict[str, int] = {}
_ALIGN_SAMPLES: dict[str, list[str]] = {}
#: `rel -> "U5" | "G"`, which witness carried a file into `agree-G5`.
_G5_WITNESS: dict[str, str] = {}


def g5_files_the_prog_resolves(tsv) -> set[str]:
    """Corpus files carrying an effect statement whose `undo` the model's
    `Prog` can RESOLVE, for the G5 arm of `checker_alignment`.

    G5 is stated over a `Prog` — the extern table plus the fn call graph — so
    the U5 row can only count a teardown crossing it reaches through a NAMED
    fn or extern. An `undo w.task.run(...)` (a spawn handle), an
    `undo store.drop()` (a host receiver), an `undo f()` (an arrow parameter)
    and an `undo dispatch1(...)` whose `dispatch1` calls its own parameter all
    leave the `Prog` at the first hop: the fold has no declaration to follow
    and counts nothing. A zero there is the model having no fact, not the
    model disagreeing, and filing it as `missed-G5` would red the gate over a
    documented fragment boundary.

    A statement is resolvable when some inverse head is a declared fn or
    extern AND its whole transitive callee closure is declared too — exactly
    the condition under which `_prog_reach`'s answer is a judgment rather than
    a fail-open default. A file is resolvable when ANY of its effect
    statements is, so a clean `undo store.drop()` beside a real
    `undo wrap(...)` does not exempt the file.

    Empty `tsv` (the no-toolchain tests call `checker_alignment` with the
    verdicts alone) means no `Prog` facts at all, hence nothing resolvable —
    the fail-closed reading for a caller that supplied no program."""
    rows = [r.split("\t") for r in tsv]
    externs: dict[str, dict[str, tuple[str, list[str]]]] = {}
    fns: dict[str, dict[str, list[str]]] = {}
    for r in rows:
        if r[0] == "EX" and len(r) >= 7:
            externs.setdefault(r[1], {})[r[2]] = (
                r[3], [c for c in r[6].split(",") if c])
        elif r[0] == "FN" and len(r) >= 5:
            fns.setdefault(r[1], {})[r[2]] = [c for c in r[3].split(",") if c]
    resolved: set[str] = set()
    cache: dict[str, tuple[dict[str, set[str]], set[str]]] = {}
    for r in rows:
        if r[0] != "I" or len(r) < 7 or r[4] != "effect" or r[1] in resolved:
            continue
        rel = r[1]
        if rel not in cache:
            _caps, _crosses, names = _prog_reach(externs.get(rel, {}),
                                                 fns.get(rel, {}))
            cache[rel] = (names, set(externs.get(rel, {})) | set(fns.get(rel, {})))
        names, declared = cache[rel]
        for h in (x for x in r[6].split(",") if x):
            if h in names and names[h] <= declared:
                resolved.add(rel)
                break
    return resolved


def checker_alignment(file_facts: dict, componentless: list[str],
                      v: Verdicts, tsv=()) -> list[str]:
    """Compile each file with the real checker and compare refusal codes
    against the formal verdicts. Returns the fatal-bucket findings.

    Requirement CLOSURE (and hence linkability, which subsumes it) is
    deliberately NOT part of `formal_clean`. `checker_code` type-checks
    and links ONE file: a requirement no in-file component provides is
    resolved against the rest of the composition at `revl link` time, and
    `lower._link` reports nothing for it. Reading the V row's `closed`
    column as a checker-visible refusal made 32 files look like the model
    being stricter than the checker when the model was answering a
    different question. `disjoint` and `link` ARE checker-visible (G2
    provision conflict, G3 self-provision and cycles) and are compared."""
    align: dict[str, int] = {}
    samples: dict[str, list[str]] = {}
    _ALIGN.clear()
    _ALIGN_SAMPLES.clear()
    _G5_WITNESS.clear()
    g5_resolved = g5_files_the_prog_resolves(tsv)

    def record(key: str, rel: str) -> None:
        align[key] = align.get(key, 0) + 1
        samples.setdefault(key, []).append(rel)

    # The model covers ALL THREE G4 rules now. The MARKER rule — a classified
    # statement's marker presence against the interface's declared emission,
    # over crossings resolved to a (service, method) — is `Oracle.g4OK`. The
    # ACQUIRE rule — a HOST acquire verb (`Pool.open`) legal only as the
    # acquisition of an `effect … undo …` bracket, where its release is
    # registered — is `Oracle.hostAcquireOK` over the `HA` position facts
    # (issue 334). The CONFIG-IS-DATA rule — a config field's declared type
    # must be built, transitively, out of data, so it can carry neither a live
    # callable nor a capability (item 378) — is `Oracle.configDataOK` over the
    # `CN` type-shape facts (issue 1161); it is the one G4 rule that judges a
    # declaration rather than a body, which is why it needed facts of a new
    # kind rather than a case in an existing rule.
    # So a G4 refusal is fatal in EVERY category again: there is
    # no out-of-fragment exemption. The two G4-coded refusals the model still
    # cannot see — `g4_missing_undo.rvl` and `v2_extern_acquire_no_undo.rvl` —
    # never reach this loop: one is refused at PARSE and one declares no
    # component, so both are reported by the no-manifest census below, not
    # bucketed here.

    for rel in file_facts:
        comp_rows = [(k, x) for k, x in v.comps.items() if k[0] == rel]
        prov_rows = [(k, x) for k, x in v.providers.items() if k[0] == rel]
        spawn_rows = [(k, x) for k, x in v.spawns.items() if k[0] == rel]
        a9_rows = [(k, x) for k, x in v.a9.items() if k[0] == rel]

        # The CD row is the third rule under the G4 guarantee (issue 1161), so
        # it joins the two crossing rules in BOTH directions: it can clear a
        # G4 refusal the model would otherwise have missed, and a CD failure
        # over a file the checker accepts is `formal-strict` like any other.
        cfg_rows = [(k, x) for k, x in v.configs.items() if k[0] == rel]
        g4_rows = comp_rows + prov_rows + spawn_rows + cfg_rows
        # The A2 row (issue 1166) is checker-visible: `lower._dispatch_action`
        # refuses the shape with code A2, so a model `fail` on an accepted
        # file is `formal-strict` and a checker A2 with the row `ok` is the
        # fatal `missed-A2`.
        a2_rows = [(k, x) for k, x in v.a2.items() if k[0] == rel]
        vrow = v.files.get(rel, ("ok", "ok", "ok"))
        formal_clean = vrow[0] == "ok" and vrow[2] == "ok" and all(
            x == "ok" for _, x in g4_rows + a9_rows + a2_rows)
        a2_found = any(x == "fail" for _, x in a2_rows)
        raw_found = any(x == "fail" for _, x in g4_rows)

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
        elif code == "A9":
            # The A9 row is the model's `a9B` over the component's clause,
            # installed blocks and routes (issues 1167 / #1172): a checker A9
            # refusal the row does not see is the model being weaker than
            # what revl enforces, and fatal.
            a9_fail = any(x == "fail" for _, x in a9_rows)
            record("agree-A9" if a9_fail else "missed-A9", rel)
        elif code == "A2":
            record("agree-A2" if a2_found else "missed-A2", rel)
        elif code == "G5":
            # G5 (issue #1169 F4). The model sees a teardown crossing two
            # ways, and the bucket says WHICH: the `U5` row counting a
            # registration (the row the G5 guarantee is stated over), or the
            # `G` row refusing the component outright — `undo w.task.run(...)`
            # is an unmarked call to an `emission` method, so the marker rule
            # reaches the same file by a different door.
            u5_rows = [x for k, x in v.g5reg.items() if k[0] == rel]
            comp_fail = any(x == "fail" for _, x in comp_rows)
            if any(isinstance(x, int) and x > 0 for x in u5_rows):
                record("agree-G5", rel)
                _G5_WITNESS[rel] = "U5"
            elif comp_fail:
                record("agree-G5", rel)
                _G5_WITNESS[rel] = "G"
            elif rel not in g5_resolved:
                # Every `undo` in the file leaves the `Prog` at the first hop
                # (a handle, a host receiver, an arrow or a dispatched
                # parameter), so the U5 row has no declaration to follow and
                # its zero is an absence of fact. Named in full below, never
                # counted silently.
                record("out-of-fragment-G5" if formal_clean
                       else "formal-found-other", rel)
            else:
                # The `undo` resolves inside the `Prog` and the fold still
                # counted nothing: that IS the model being weaker than the
                # checker, and fatal.
                record("missed-G5", rel)
        elif code == "G6":
            # DELIBERATELY not an `agree-G6` on a `C` row fail, which is what
            # issue #1169 F4 proposed. revl's G6 is "purity outside effect
            # forms" and the duplicate-binding refusal (`diagnostics.py`); the
            # model's `C` row is the issue-276 CONFINEMENT surface
            # (`Oracle.confinedB`: every statement head root is a declared
            # require local or require-held binding). They are different
            # judgments about different things, and the `C` row `fail`s on a
            # hundred-odd corpus files the checker ACCEPTS — a host root like
            # `Map.new` is not a declared require, and STATUS.md says so. An
            # agreement keyed on it could not fail, which is the informational
            # bucket this issue is about, one level down. The model states no
            # rule about purity outside an effect form or about a duplicate
            # binding, so a G6 refusal is honestly outside its fragment.
            record("out-of-fragment-G6" if formal_clean
                   else "formal-found-other", rel)
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

    _ALIGN.update(align)
    _ALIGN_SAMPLES.update(samples)

    total = sum(align.values())
    print(f"checker alignment ({total} modeled files; every disagreeing "
          f"bucket is FATAL: {'/'.join(FATAL_BUCKETS)}):")
    for k in sorted(set(align) | set(FATAL_BUCKETS)):
        n = align.get(k, 0)
        mark = "  FATAL" if k in FATAL_BUCKETS and n else ""
        print(f"  {k:20} {n}{mark}")
    for k in (*FATAL_BUCKETS, "out-of-fragment-G5", "out-of-fragment-G6"):
        for rel in samples.get(k, []):
            print(f"  ALIGN {k}: {rel}")
    for rel in samples.get("agree-G5", []):
        print(f"  ALIGN agree-G5 via {_G5_WITNESS.get(rel, '?')}: {rel}")

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
    # A clean checkout has no out/ yet (the gate creates it when the oracle
    # runs); the no-toolchain tests reach this writer first.
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text("".join(
        f"{code}\t{rel}\n" for code in sorted(nm_codes)
        for rel in sorted(nm_codes[code])), encoding="utf-8")
    print(f"  (complete list: {full.relative_to(FORMAL)})")

    return [f"{k}: {rel}" for k in FATAL_BUCKETS for rel in samples.get(k, [])]


# ------------------------------- the out-of-fragment membership ratchet
#
# `out-of-fragment-G5` and `out-of-fragment-G6` say "the model has no fact
# here", and issue #1169's own work could not name an input that makes
# either of them FAIL: by construction they record an ABSENCE, and an
# absence has no wrong answer to catch. That is the same shape as the two
# buckets #1169 promoted to fatal, one level down — a bucket that cannot
# fire is not a gate — and it is the reason eleven files can sit in these
# two and nothing in the tree notices.
#
# What can be judged without judging the contents is MEMBERSHIP. The ledger
# below names the corpus files in each bucket today, and the gate holds the
# tree to it in BOTH directions:
#
#   * a file that JOINS one of these buckets and is not in the ledger fails
#     the gate. A new `undo` shape the `Prog` cannot resolve, or a new
#     G6-coded fixture, can no longer arrive while the model stays silent:
#     somebody has to model it, or write its name down and own the hole.
#   * a ledger entry that is NO LONGER in its bucket fails the gate too and
#     must be DELETED. So the list shrinks only, and a file cannot be parked
#     in it once the model does have a fact about it.
#
# The inputs that make it fail, named: dropping a new
# `examples/rejections/g5_undo_*.rvl` whose `undo` reads its crossing off a
# handle into the corpus reds the gate with `joined out-of-fragment-G5`;
# teaching the `U5` fold to follow a handle reds it with `left
# out-of-fragment-G5` on each of the ten files it newly resolves, until
# their lines go. Deleting the ledger reds it as well — a missing ratchet
# reads as a failure, never as nothing to check.
#
# It records NAMES ONLY — no counts, no totals, no line numbers — so the
# file is byte-identical whether it is written under CI's python 3.11 or a
# 3.14 developer venv, which is the shape PR #1214's construct-reach ledger
# settled on for the same reason.
#
# `--write-status` does NOT write it. Regenerating the census is routine and
# a ratchet that widens itself as a side effect of a routine regeneration is
# not a ratchet; widening it takes `--write-ledger` and shows up as its own
# diff hunk.
#
# NOT extended to the generic `out-of-fragment` bucket, deliberately. That
# one collects every checker code the model states no row about at all (G1,
# G7, T1, REVL, HOST-METHOD, ...) and grows with any new type-error fixture
# anywhere in revl, so a ratchet there would red the formal gate on work
# that never touched the formal layer. G5 and G6 are different in kind: the
# model carries a row aimed at each of them — the `U5` registration fold and
# the `C` confinement surface — so "no fact about this file" is a claim
# about a specific row that exists, and that is the claim worth pinning.
OOF_LEDGER_PATH = FORMAL / "out_of_fragment_ledger.json"
OOF_RATCHET_BUCKETS = ("out-of-fragment-G5", "out-of-fragment-G6")
OOF_LEDGER_ABOUT = [
    "The corpus files the checker refuses G5 or G6 and the model has NO",
    "fact about: `out-of-fragment-G5` and `out-of-fragment-G6` in",
    "`formal/harness/diff_corpus.py`'s checker-alignment buckets.",
    "",
    "Both buckets record an absence, so neither can disagree with anything",
    "and neither could fail the gate on its own (issue #1169). This ledger",
    "is what makes them fire: MEMBERSHIP is checkable even when the",
    "contents are not. A file that joins a bucket without a line here is a",
    "gate failure, and a line that is no longer in its bucket is a gate",
    "failure that must be DELETED -- so the lists shrink only, and every",
    "name left is a hole someone still owes the model a row for.",
    "",
    "Regenerate with `python3 formal/harness/diff_corpus.py --write-ledger`",
    "and read the diff: a new name is a new hole, not a formality.",
    "`--write-status` deliberately does not touch this file.",
    "",
    "It records NAMES only -- never counts, totals or line numbers -- so it",
    "is identical under CI's python 3.11 and a 3.14 developer venv.",
]


def _oof_ledger_path() -> Path:
    """Read the module attribute at call time so a test can repoint it."""
    return OOF_LEDGER_PATH


def _shown(path: Path) -> str:
    """The path a finding names: repo-relative in the tree, and whatever it
    is when a test has pointed the ledger at a scratch directory."""
    try:
        return str(path.relative_to(REPO))
    except ValueError:
        return str(path)


def out_of_fragment_ledger(samples: dict[str, list[str]]) -> dict:
    """The ledger this run's buckets would produce, ready to serialize."""
    doc: dict = {"_about": list(OOF_LEDGER_ABOUT)}
    for bucket in OOF_RATCHET_BUCKETS:
        doc[bucket] = sorted(set(samples.get(bucket, [])))
    return doc


def out_of_fragment_ratchet(samples: dict[str, list[str]],
                            write: bool = False) -> list[str]:
    """Hold this run's `out-of-fragment-G5`/`-G6` membership to the committed
    ledger, in both directions. Returns gate-failure strings; `write`
    regenerates the ledger instead and returns nothing.

    Called from `main()` over the WHOLE corpus, never from
    `checker_alignment`: the ratchet is a statement about the corpus, and a
    single-file run of the alignment arms would read every other name in the
    ledger as stale."""
    path = _oof_ledger_path()
    doc = out_of_fragment_ledger(samples)
    text = json.dumps(doc, indent=2, ensure_ascii=True) + "\n"
    if write:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        print(f"{_shown(path)}: out-of-fragment ledger rewritten "
              + " ".join(f"{b}={len(doc[b])}" for b in OOF_RATCHET_BUCKETS))
        return []
    try:
        committed = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return [f"{_shown(path)} is missing — the out-of-fragment "
                "buckets have no ratchet, so a file can join them silently; "
                "regenerate with `python3 formal/harness/diff_corpus.py "
                "--write-ledger`"]
    except json.JSONDecodeError as e:
        return [f"{_shown(path)} is not readable JSON ({e})"]
    findings: list[str] = []
    for bucket in OOF_RATCHET_BUCKETS:
        have = set(doc[bucket])
        listed = committed.get(bucket)
        if not isinstance(listed, list) or any(
                not isinstance(x, str) for x in listed):
            findings.append(f"{_shown(path)} has no list of names "
                            f"under {bucket!r}")
            continue
        was = set(listed)
        for rel in sorted(have - was):
            findings.append(
                f"joined {bucket}: {rel} — the model has no fact about a "
                "file it did not have to cover before. Give the row that "
                "should see it a case, or record the hole with "
                "`--write-ledger`")
        for rel in sorted(was - have):
            findings.append(
                f"left {bucket}: {rel} — no longer in the bucket, so the "
                "ledger line is stale and must be deleted "
                "(`--write-ledger`)")
    if not findings:
        print("out-of-fragment ratchet: "
              + ", ".join(f"{b} {len(doc[b])}" for b in OOF_RATCHET_BUCKETS)
              + f" — held to {_shown(path)} (shrink-only)")
    return findings


# --------------------------------------------- the census STATUS.md prints
#
# `formal/STATUS.md` used to STATE the census and the alignment buckets in
# prose somebody typed after reading a gate run. It drifted, as prose does:
# at issue #1169 the document claimed "0 formal-strict, 0 formal-found-other"
# while the gate printed 1 and 2, and quoted 654 verdicts over 305 files while
# the gate printed 4526 over 216. Nothing compared the two, so the claim was
# unbacked in both directions at once.
#
# The block between these markers is now RENDERED from the same run that
# prints the buckets, and `main` fails the gate when the checked-in text is
# not what this run produced. Generating is what makes the drift impossible;
# the check is what makes forgetting to regenerate loud.
STATUS_PATH = FORMAL / "STATUS.md"
STATUS_BEGIN = ("<!-- BEGIN GENERATED alignment: regenerate with "
                "`python3 formal/harness/diff_corpus.py --write-status` -->")
STATUS_END = "<!-- END GENERATED alignment -->"


def status_block(census: dict, file_facts: dict, componentless: list[str],
                 refusals: dict, ref: Verdicts, align: dict,
                 mismatches: int = 0) -> str:
    """The generated census + alignment section of `formal/STATUS.md`.

    `mismatches` is the differential's own count, which only `main` has (it
    needs the Lean side). `--write-status` renders 0, and that is not a claim
    it measured: the gate returns non-zero on ANY mismatch, so a census can
    only reach a green main saying zero, and a `main` run with a mismatch
    renders the real number and reports the drift as well."""
    parts = [
        f"{len(ref.files)} files", f"{len(ref.comps)} components",
        f"{len(ref.providers)} provide methods", f"{len(ref.spawns)} spawn edges",
        f"{len(ref.refused)} parse refusals",
        f"{len(ref.dispositions)} teardown scenarios",
        f"{len(ref.recoveries)} recoveries",
        f"{len(ref.confinements)} confinements", f"{len(ref.g8surface)} surfaces",
        f"{len(ref.g5reg)} teardowns",
        f"{len(ref.a9)} provide-clause components",
        f"{len(ref.configs)} config fields", f"{len(ref.a2)} A2 bodies",
    ]
    def para(text: str) -> str:
        # The document is hand-wrapped at 72; a generated block that is not
        # would show up as a wall of diff noise every time the corpus grows.
        # `break_on_hyphens` off, or `out-of-fragment*` splits mid-token and
        # markdown stops reading the code span.
        return textwrap.fill(" ".join(text.split()), width=72,
                             break_on_hyphens=False, break_long_words=False)

    lines = [
        STATUS_BEGIN,
        "",
        para(
            f"**{census['files']} .rvl files -> {census['components']} "
            f"components -> {census['statements']} statements = "
            f"{len(file_facts)} modeled + {len(componentless)} componentless "
            f"+ {len(refusals)} refused at parse**, and **{ref.total()} "
            f"verdicts compared ({' + '.join(parts)}), "
            f"{ref.total() - mismatches} agree, {mismatches} mismatches**."),
        "",
        para(
            f"Checker alignment over the {len(file_facts)} modeled files. "
            "Every bucket recording a DISAGREEMENT fails the gate, in both "
            "directions: `missed-*` is the model weaker than the checker, "
            "`formal-strict` and `formal-found-other` are the model stricter "
            "than the language that ships. `out-of-fragment*` means the "
            "model has no fact about the rule the checker refused under, not "
            "that it disagrees."),
        "",
        para(
            "An absence cannot disagree, so the two buckets aimed at a row "
            "the model does carry are `ratcheted` instead: "
            f"`{'` and `'.join(OOF_RATCHET_BUCKETS)}` are held to the names "
            f"in `{OOF_LEDGER_PATH.relative_to(REPO)}`, which shrinks only. "
            "A file that JOINS one fails the gate, and a line no longer in "
            "its bucket fails it until it is deleted. So a new `undo` shape "
            "the `Prog` cannot resolve, or a new G6 fixture, cannot arrive "
            "while the model stays silent about it. `agree-*` and the "
            "generic `out-of-fragment` stay informational; that one collects "
            "every code the model states no row about at all, so it grows "
            "with corpus work that never touched this layer."),
        "",
        "| bucket | files | gate |",
        "| --- | --- | --- |",
    ]
    for k in sorted(set(align) | set(FATAL_BUCKETS)):
        gate = ("**FATAL**" if k in FATAL_BUCKETS else
                "ratcheted" if k in OOF_RATCHET_BUCKETS else "informational")
        lines.append(f"| `{k}` | {align.get(k, 0)} | {gate} |")
    lines.append("")
    named = [(k, rel)
             for k in ("out-of-fragment-G5", "out-of-fragment-G6",
                       *FATAL_BUCKETS)
             for rel in sorted(_ALIGN_SAMPLES.get(k, []))]
    if named:
        lines.append(para("Nothing is counted without being named; the files "
                          "in the non-`agree` buckets are:"))
        lines.append("")
        for k, rel in named:
            lines.append(f"- `{k}`: `{rel}`")
        lines.append("")
    if _G5_WITNESS:
        lines.append(para(
            "`agree-G5` says which row saw the crossing: the `U5` "
            "registration count, or the `G` row refusing the component "
            "through the marker rule."))
        lines.append("")
        for rel in sorted(_G5_WITNESS):
            lines.append(f"- `{_G5_WITNESS[rel]}`: `{rel}`")
        lines.append("")
    lines.append(STATUS_END)
    return "\n".join(lines)


def sync_status(block: str, write: bool) -> str | None:
    """Compare the generated block against `formal/STATUS.md`, rewriting it
    when `write`. Returns a gate-failure string on drift, else `None`."""
    text = STATUS_PATH.read_text(encoding="utf-8")
    if STATUS_BEGIN not in text or STATUS_END not in text:
        return (f"formal/STATUS.md has no {STATUS_BEGIN!r} .. {STATUS_END!r} "
                "block to hold the generated census")
    head, rest = text.split(STATUS_BEGIN, 1)
    _old, tail = rest.split(STATUS_END, 1)
    current = STATUS_BEGIN + _old + STATUS_END
    if current == block:
        return None
    if write:
        STATUS_PATH.write_text(head + block + tail, encoding="utf-8")
        print("formal/STATUS.md: generated alignment block rewritten")
        return None
    return ("formal/STATUS.md's generated census is not what this run "
            "produced — rerun `python3 formal/harness/diff_corpus.py "
            "--write-status` and commit the result")


def write_status(ledger: bool = False) -> int:
    """`--write-status`: regenerate the block without the Lean toolchain.
    `--write-ledger`: regenerate the out-of-fragment membership ratchet the
    same way, and NOTHING else.

    The alignment arms read verdicts, and the gate's own differential proves
    the reference and the oracle produce the SAME ones, so the reference side
    alone is enough to render the document. A divergence between them is not
    a STATUS.md question; it fails `main` long before this.

    The two writers are separate on purpose. Rewriting the census is routine
    housekeeping; widening the set of files the model admits it has no fact
    about is not, and must not ride along on it."""
    tsv, file_facts, census = export()
    if not tsv:
        print("nothing extracted — nothing to write")
        return 1
    ref = reference_from_tsv(tsv)
    checker_alignment(file_facts, census["componentless"], ref, tsv)
    if ledger:
        out_of_fragment_ratchet(_ALIGN_SAMPLES, write=True)
        return 0
    block = status_block(census, file_facts, census["componentless"],
                         census["refusals"], ref, _ALIGN)
    problem = sync_status(block, write=True)
    if problem:
        print(f"  GATE-FAILURE {problem}")
        return 1
    return 0


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
            ("g5_registration", ref.g5reg, formal.g5reg),
            ("a9", ref.a9, formal.a9),

            ("config_data", ref.configs, formal.configs),
            ("a2", ref.a2, formal.a2)):

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
        f"{len(ref.g5reg)} teardowns + "
        f"{len(ref.a9)} provide-clause components + "
        f"{len(ref.configs)} config fields + "
        f"{len(ref.a2)} a2 bodies) — "

        f"{compared - len(mismatches)} agree, {len(mismatches)} mismatch(es)"
    )
    mismatches.extend(teardown_coverage(ref.dispositions))
    mismatches.extend(recovery_coverage(ref.recoveries))
    mismatches.extend(attenuation_coverage())
    mismatches.extend(confinement_coverage())
    mismatches.extend(prog_coverage())
    mismatches.extend(a9_coverage())

    mismatches.extend(config_coverage())
    mismatches.extend(a2_coverage())

    for m in mismatches[:10]:
        print(f"  MISMATCH {m}")
    if len(mismatches) > 10:
        print(f"  ... and {len(mismatches) - 10} more")

    fatal = checker_alignment(file_facts, componentless, formal, tsv)
    # The two buckets that record an absence rather than a disagreement, held
    # to their committed membership so they can fail at all.
    fatal.extend(out_of_fragment_ratchet(_ALIGN_SAMPLES))
    drift = sync_status(
        status_block(census, file_facts, componentless, refusals, ref, _ALIGN,
                     len(mismatches)),
        write=False)
    if drift:
        fatal.append(drift)
    for f in fatal:
        print(f"  GATE-FAILURE {f}")
    return 1 if (mismatches or fatal) else 0


if __name__ == "__main__":
    _argv = sys.argv[1:]
    if "--write-ledger" in _argv:
        sys.exit(write_status(ledger=True))
    sys.exit(write_status() if "--write-status" in _argv else main())
