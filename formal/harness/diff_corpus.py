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
5. render those buckets into `formal/STATUS.md` between the
   `GENERATED alignment` markers, and fail the gate when the checked-in
   block is not what this run produced. The document's "0 formal-strict"
   is then this run's own output rather than a sentence somebody typed.
   The block names the files in every failing or ratcheted bucket and
   stores no count that moves with the corpus (issue #1768); the census
   totals are printed by the run.

Nothing is skipped. A parse-time REFUSAL is a verdict (revl rejecting the
file IS the answer) and is carried through as an `X` row; a parsed file
with no component has no composition to model and is named in the
`no-manifest` report rather than dropped from every count.
"""

import dataclasses
import itertools
import json
import os
import re
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
# The approval floor's two pieces of algebra (item 246), imported rather than
# restated: the scope an `Approval[C]` type carries, and whether a scope
# covers a token. The reference's `AP` verdicts are the shipped ones.
from revl.lower import _approval_covers, _approval_scope_of
# The host families the checker resolves by name, for the G1 row (#1807).
from revl.lower import _HOST_CALLABLES
import runtime as _rt  # backends/python/runtime.py — the reference teardown
from revl.parser import (
    EffectStmt,
    EmitExpr,
    EmitStmt,
    ExprArrow,
    ExprBlockArm,
    ExprCall,
    ExprField,
    ExprIf,
    ExprIndex,
    ExprList,
    ExprLit,
    ExprMatch,
    ExprRecord,
    ExprVar,
    IsolateStmt,
    LetApprovalStmt,
    LetEffect,
    LetStmt,
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


def _route_values(callee: object) -> tuple[str, str] | None:
    """`_route`, reading through a list element as well: `ps[0].charge(...)`
    is `("ps", "[].charge")`. Used only where a call is resolved against the
    provision aliases (`_resolve_emission`), whose list entries are keyed
    `<name>[]` (issue #1509)."""
    if isinstance(callee, ExprField):
        parts: list[str] = []
        cur = callee
        while isinstance(cur, (ExprField, ExprIndex)):
            parts.append(cur.name if isinstance(cur, ExprField) else "[]")
            cur = cur.target
        if isinstance(cur, ExprVar):
            return cur.name, ".".join(reversed(parts))
        # a receiver written in place (issue #1681): keyed by the expression,
        # which `collect_provision_aliases` resolved to the provision it holds
        return _expr_key(callee.target), callee.name
    return _route(callee)


def _expr_key(e) -> str:
    """The alias key of a receiver expression written in place."""
    return f"@expr{id(e)}"


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
# The same hole one declaration weaker: `Supervisor requires net: Net` spawning
# `Worker requires net: Kv` where NEITHER service declares a token. The key is
# not the boundary's name there either, so the element is the SERVICE
# (`lower._undeclared_cap`, item 561).
_UNDECLARED_NS = "svc:"


def _undeclared_cap(service: str) -> str:
    """`lower._undeclared_cap`: the attenuation-fold element for an emission
    that no declaration tokens (a bare `emission`, a plain or unresolvable
    service), named by the SERVICE it is declared on. It gets its OWN namespace
    so a derived spelling can never masquerade as a declared token, and two
    such boundaries compare by the declaration rather than by a consumer's
    local key."""
    return _UNDECLARED_NS + service


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


#: The pseudo-service a HOST emission's `U` row names (see `_record`). The
#: oracle's `g4OK` reads a `U` row on it as a call to an emission, because the
#: exporter writes one only for a callee `_fn_emitting` put in the set.
HOST_SERVICE = "@host"

#: The marker contexts a host emission is judged in: every one but a teardown
#: slot. The checker holds the extern carrier to the marker wherever it holds a
#: service emission (issue #1437, docs/design/1437-emit-marks-every-crossing.md),
#: so a host emission in a `plain` position is refused as an unmarked service
#: emission is. (Issue #1427 had judged only the argument-list contexts.) A
#: bracket's `undo` is recorded as `undo` (see `walk_calls`) and gets no row:
#: the checker lowers it in teardown mode, and an emission there is G5's.
HOST_MARKER_CONTEXTS = ("emit", "emitarg", "emitnested", "plain")


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


def _emitting_tokens(prog) -> dict[str, set[str]]:
    """`emission_analysis._emitting_capabilities` over the parser AST: name ->
    the capability TOKENS a call to it reaches.

    An `emission` or `witnessed` extern is seeded with its declared scope,
    or its own name when it declares none ("a scope replaces the name; it
    does not join it", docs/capabilities.md section 2). A module `fn`
    carries the union of what its bare-name callees reach, and a
    first-class reference to an emitting callable in its body adds `*` (the
    token no `emission[...]` list can name) beside that callable's tokens,
    exactly as the checker's `passed` channel does. The call and value
    channels are `_fn_body_calls`, the same split the `FN` row's `star`
    marker is read from.

    Two readers: the F row's bound column (`_reach_call`), which the checker
    measures against a service operation's `emission[...]` entries, and the
    approval floor's crossing tokens (`_approval_tokens`), which the checker
    measures against `requires approval`. Both key the requirement by token,
    so a scoped host emission carries its scope into the model rather than
    its name or `*` (issue #1455)."""
    caps: dict[str, set[str]] = {
        e.name: set(e.capabilities or ()) or {e.name}
        for e in prog.externs
        if getattr(e, "classification", "") in ("emission", "witnessed")}
    calls: dict[str, list[str]] = {}
    passed: dict[str, set[str]] = {}
    for fn in prog.fn_decls:
        calls[fn.name], passed[fn.name] = _fn_body_calls(fn.body)
    changed = True
    while changed:
        changed = False
        for name, called in calls.items():
            reached: set[str] = set()
            for callee in called:
                reached |= caps.get(callee, set())
            for ref in sorted(passed.get(name, ())):
                if caps.get(ref):
                    reached.add("*")
                    reached |= caps[ref]
            if reached and not reached <= caps.get(name, set()):
                caps.setdefault(name, set()).update(reached)
                changed = True
    return caps


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


_UNBOUND = object()


class _InverseCtx(NamedTuple):
    """What one bracket inverse's indirections resolve against."""
    bindings: dict           # `let` name -> the value it was bound to
    emitting: set            # `_fn_emitting`: emission externs + fns reaching one
    externs: dict            # extern name -> classification
    requires: dict           # require local -> service
    handles: dict            # spawn handle var -> child component
    psvc: dict               # component -> provide key -> service
    aliases: dict            # provision aliases, as the marker rule reads them
    services: dict           # service -> op -> declared `emission`


def _inverse_project(e, ctx: _InverseCtx, seen: frozenset = frozenset()):
    """The value `e` holds with every `let`-bound name replaced by what it was
    bound to, `_UNBOUND` when nothing is known (`lower._walk_inverse_emissions`'
    `_project`): through a name, a second name, a record field and a list
    element; an `if`/`match` value is handed back whole, so every arm is read."""
    if isinstance(e, ExprVar):
        if e.name in seen or e.name not in ctx.bindings:
            return _UNBOUND
        value = ctx.bindings[e.name]
        held = _inverse_project(value, ctx, seen | {e.name})
        return value if held is _UNBOUND else held
    if isinstance(e, ExprField):
        base = _inverse_project(e.target, ctx, seen)
        if isinstance(base, ExprRecord):
            for key, item in base.fields:
                if key == e.name:
                    held = _inverse_project(item, ctx, seen)
                    return item if held is _UNBOUND else held
            return None
        return _UNBOUND
    if isinstance(e, ExprIndex):
        base = _inverse_project(e.target, ctx, seen)
        if isinstance(base, ExprList):
            index = e.index
            if isinstance(index, ExprLit) and isinstance(index.value, int) \
                    and not isinstance(index.value, bool):
                if not 0 <= index.value < len(base.items):
                    return None
                item = base.items[index.value]
                held = _inverse_project(item, ctx, seen)
                return item if held is _UNBOUND else held
            return base
        return _UNBOUND
    return _UNBOUND


def _inverse_op(e: ExprField, ctx: _InverseCtx) -> "tuple[str, str] | None":
    """(service, op) when the field read `e` names an `emission` service
    operation: off a require binding (`net.send`), a spawn handle
    (`w.task.run`) or a local aliasing a provision (`t.run`)."""
    rv = _route_values(e)
    if rv is None or not rv[1]:
        return None
    res = _resolve_emission(rv[0], rv[1], ctx.requires, ctx.handles, ctx.psvc,
                            ctx.aliases)
    if res is None or not ctx.services.get(res[0], {}).get(res[1], False):
        return None
    return res


def inverse_reach_heads(expr: object, ctx: _InverseCtx) -> list[str]:
    """The heads a bracket inverse reaches through an INDIRECTION, issue
    #1792: `lower._walk_inverse_emissions`' arms that read a value rather
    than a call written in the slot.

      * a call to a `let`-bound arrow reaches what the arrow's body reaches;
      * an arrow literal's body is slot code, wherever it is dispatched;
      * a first-class reference to an emitting callable in value position
        (`app(wrap, key)`) reaches what it names;
      * a read of an `emission` service operation in value position
        (`dispatch1(w.task.run)`) is that crossing, one indirection later;
      * a `let`-bound name is read as its value, through a second name, a
        record field, a list element or an `if`/`match` arm.

    A call read out of a binding the body already evaluated is a value, not a
    crossing in the slot (`let t = emit mint(u)` then `undo store.remove(t)`),
    as the checker's `_computed` flag has it. A provision operation CALLED in
    the slot (`undo w.task.run(k)`) is not repeated here: it is an unmarked
    call to an `emission` operation, which the `G` row refuses.

    Returns names in first-reach order: a declared fn or extern, or the
    `<Service>.<op>` spelling of a service operation, which the exporter
    declares in the `Prog` as an `emission` boundary (`EX`)."""
    out: list[str] = []

    def add(name: str) -> None:
        if name not in out:
            out.append(name)

    def walk(e, seen_arrows: tuple, computed: bool) -> None:
        if e is None or isinstance(e, (str, int, float, bool)):
            return
        held = _inverse_project(e, ctx)
        if held is not _UNBOUND and held is not e:
            walk(held, seen_arrows, True)
            return
        if isinstance(e, ExprVar):
            if e.name in ctx.emitting:
                add(e.name)
            return
        if isinstance(e, ExprCall):
            callee = e.callee
            if not computed and isinstance(callee, ExprVar):
                name = callee.name
                cls = ctx.externs.get(name)
                if cls in ("emission", "witnessed") or (
                        cls is None and name in ctx.emitting):
                    add(name)
                elif isinstance(ctx.bindings.get(name), ExprArrow) \
                        and name not in seen_arrows:
                    walk(ctx.bindings[name].body, seen_arrows + (name,),
                         computed)
            for a in e.args:
                walk(a, seen_arrows, computed)
            return
        if isinstance(e, ExprField):
            op = _inverse_op(e, ctx)
            if op is not None:
                add(f"{op[0]}.{op[1]}")
            else:
                walk(e.target, seen_arrows, computed)
            return
        if isinstance(e, ExprArrow):
            walk(e.body, seen_arrows, False)
            return
        if dataclasses.is_dataclass(e) and not isinstance(e, type):
            for f in dataclasses.fields(e):
                walk(getattr(e, f.name), seen_arrows, computed)
            return
        if isinstance(e, (list, tuple)):
            for x in e:
                walk(x, seen_arrows, computed)

    walk(expr, (), False)
    return out


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
    hit = _alias_hit(root, chain, aliases)
    if hit is not None:
        (comp, key), meth = hit
        if comp == SERVICE_PARAM:
            return key, meth  # a service-typed method parameter (#1682)
        svc = psvc.get(comp, {}).get(key)
        return (svc, meth) if svc else None
    return None


#: The alias marker for a provide method's own SERVICE-TYPED parameter (issue
#: #1682): `aliases[p] = (SERVICE_PARAM, <Service>)`. A call through it is a
#: crossing of that service's declared scopes, judged in the method, so it
#: resolves to the service directly rather than through a spawn handle.
SERVICE_PARAM = "@service-param"


def collect_service_params(c, psvc: dict, svc_objs: dict, aliases: dict) -> None:
    """Record every provide method's service-typed parameters as aliases of
    their service (issue #1682). Keyed by surface name over the whole
    component, as `collect_provision_aliases` is."""
    for stmt in c.body:
        if not isinstance(stmt, ProvideStmt):
            continue
        svc = svc_objs.get(psvc.get(c.name, {}).get(stmt.key))
        if svc is None:
            continue
        for pm in stmt.methods:
            decl = svc.methods.get(pm.name)
            if decl is None:
                continue
            for pname, (_dn, ptype) in zip(pm.params, decl.params):
                head, _ = parse_type(ptype or "")
                if head in svc_objs:
                    aliases[pname] = (SERVICE_PARAM, head)


def _alias_hit(root: str, chain: str, aliases: dict | None):
    """`((component, key), method)` when the receiver of `root.chain` is a
    provision alias, else None. The receiver is `root` itself (`t.run`), or a
    field or element read off it (`r.p.run`, `ps[0].run`), whose alias is
    keyed by the path (`r.p`, `ps[]`, issue #1509)."""
    if not aliases or not chain:
        return None
    parts = chain.split(".")
    name = root
    for part in parts[:-1]:
        name += part if part == "[]" else f".{part}"
    held = aliases.get(name)
    if isinstance(held, tuple):
        return held, parts[-1]
    return None


def collect_provision_aliases(node, handles: dict, aliases: dict) -> None:
    """Fill `aliases` (var -> (component, provide key)) from `let t = w.task`
    bindings, where `w` is a spawn handle. Runs after `collect_spawns`, whose
    `handles` it reads."""
    if node is None or isinstance(node, (str, int, float, bool)):
        return
    if type(node).__name__ == "LetStmt" and isinstance(getattr(node, "name", None), str):
        _note_value_aliases(node.name, getattr(node, "value", None), handles,
                            aliases)
    if isinstance(node, ExprCall) and isinstance(node.callee, ExprField) \
            and _alias_path(node.callee.target) is None:
        # a receiver written in place (issue #1681): what it holds, keyed by
        # the expression itself, which `_route_values` reads back
        held = _value_provision(node.callee.target, handles, aliases)
        if held is not None:
            aliases[_expr_key(node.callee.target)] = held
    if dataclasses.is_dataclass(node) and not isinstance(node, type):
        for f in dataclasses.fields(node):
            collect_provision_aliases(getattr(node, f.name), handles, aliases)
        return
    if isinstance(node, (list, tuple)):
        for x in node:
            collect_provision_aliases(x, handles, aliases)


def _alias_path(e) -> str | None:
    """`r`, `r.p`, `ps[]` for a value written as a name read through fields
    and elements, else None."""
    if isinstance(e, ExprVar):
        return e.name
    if isinstance(e, ExprField):
        base = _alias_path(e.target)
        return None if base is None else f"{base}.{e.name}"
    if isinstance(e, ExprIndex):
        base = _alias_path(e.target)
        return None if base is None else f"{base}[]"
    return None


def _value_provision(value, handles: dict, aliases: dict):
    """The (component, provide key) a value holds: the direct read off a
    spawn handle, a name (or a field or element read off one) that already
    aliases one, or an `if` whose two arms hold the same one. The checker
    reads the same thing as the value's static type (issue #1509)."""
    if isinstance(value, ExprField) and isinstance(value.target, ExprVar) \
            and value.target.name in handles:
        return (handles[value.target.name], value.name)
    if isinstance(value, ExprIf):
        then = _value_provision(value.then, handles, aliases)
        return then if then is not None and then == _value_provision(
            value.otherwise, handles, aliases) else None
    if isinstance(value, ExprBlockArm):
        # a statement-block match arm (issue #1729): its value is its tail,
        # read with the arm's own `let`s in scope, as `infer_ir` types the
        # `do` node the checker lowers it to
        inner = dict(aliases)
        for st in value.stmts:
            if isinstance(st, LetStmt):
                _note_value_aliases(st.name, st.value, handles, inner)
        return _value_provision(value.tail, handles, inner)
    if isinstance(value, ExprMatch):
        arms = [_value_provision(arm[-1], handles, aliases) for arm in value.arms]
        return arms[0] if arms and arms[0] is not None \
            and all(a == arms[0] for a in arms) else None
    if isinstance(value, ExprField) and isinstance(value.target, ExprRecord):
        for key, item in value.target.fields:
            if key == value.name:
                return _value_provision(item, handles, aliases)
        return None
    if isinstance(value, ExprIndex) and isinstance(value.target, ExprList):
        items = [_value_provision(i, handles, aliases) for i in value.target.items]
        return items[0] if items and items[0] is not None \
            and all(i == items[0] for i in items) else None
    path = _alias_path(value)
    held = aliases.get(path) if path is not None else None
    return held if isinstance(held, tuple) else None


def _note_value_aliases(name: str, value, handles: dict, aliases: dict) -> None:
    """Record what a `let name = value` binding holds: the provision itself
    (`let t = w.task`, a second hop, an `if` of one), or a record field or
    list element holding one, keyed `name.field` / `name[]` (issue #1509), and
    what a copy of such a record or list holds (`let r2 = r`)."""
    held = _value_provision(value, handles, aliases)
    if held is not None:
        aliases[name] = held
        return
    if isinstance(value, ExprRecord):
        for key, item in value.fields:
            held = _value_provision(item, handles, aliases)
            if held is not None:
                aliases[f"{name}.{key}"] = held
        return
    if isinstance(value, ExprList):
        items = [_value_provision(i, handles, aliases) for i in value.items]
        if items and items[0] is not None and all(i == items[0] for i in items):
            aliases[f"{name}[]"] = items[0]
        return
    path = _alias_path(value)
    if path is not None:
        for key in [k for k in aliases if k.startswith(f"{path}.")
                    or k.startswith(f"{path}[]")]:
            aliases[name + key[len(path):]] = aliases[key]


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
                host_tokens: "dict[str, set[str]] | None") -> None:
    """The crossing ONE call head contributes (see `walk_reach`). The
    arguments are the caller's to walk, under whatever region encloses them:
    a call evaluated to produce an argument is not the marked crossing."""
    rt = _route_values(node.callee)
    if not rt:
        return
    root, chain = rt
    res = _resolve_emission(root, chain, requires, handles, psvc, aliases)
    if res is not None and region == "all":
        svc, meth = res
        hit = _alias_hit(root, chain, aliases)
        if (svc, meth) in em_set and (root in handles or hit is not None):
            # a crossing through a resolved receiver: a spawn handle, an alias
            # of one, a service-typed local, or a method's own service-typed
            # parameter (issues #1682, #1508). The BOUND column reads the op's
            # declared scope, `*` when bare, which is what the checker's
            # provider bound reads (`_resolved_crossings`); the attenuation
            # column stays `*`, as `_emit_step_caps_pairs` reads a non-`req`
            # head.
            mode, entries = bounds[(svc, meth)]
            if mode == "any":
                out.add(("*", "*"))
            else:
                for e in entries:
                    out.add(("*", e))
        elif (svc, meth) in em_set:
            mode, entries = bounds[(svc, meth)]
            if mode == "any":
                # No declared token: the SERVICE names the boundary
                # for the fold, in its own namespace; the BOUND
                # column still reads the wiring key (item 561).
                out.add((_undeclared_cap(svc), root))
            else:
                for e in entries:
                    out.add((_declared_cap(e), _canon_cap(root, e)))
    elif res is None and region == "all" and root in emitting:
        # A host emission. The two namespaces part company here (#1169 F3):
        # the attenuation fold gives it the unnameable `*` whatever the
        # extern is called (`_emit_step_caps_pairs`: a non-`req` target is
        # `Cap("*")`), but the provide-method BOUND names the capability
        # TOKENS the call reaches, read off `_emitting_capabilities`' fixed
        # point (`_emitting_tokens`), which `_method_emissions` measures
        # against the declared `emission[...]` entries. An unscoped
        # `extern emission fn wire` is the token `wire`, which is why
        # `Db.execute` can be declared `emission[wire, ...]` at all; a SCOPED
        # `extern emission[pay] fn charge` is `pay` and not `charge` ("a
        # scope replaces the name", issue #1455); a named fn reaching either
        # carries the tokens it reaches, and `*` beside them only for a
        # first-class reference, as the checker's fold adds it.
        reached = (host_tokens or {}).get(root) or {"*"}
        for token in sorted(reached):
            out.add(("*", token))


def walk_reach(node, out: "set[tuple[str, str]]", region: str, requires: dict,
               handles: dict, psvc: dict, bounds: dict, em_set: set,
               emitting: set, aliases: dict | None = None,
               host_tokens: "dict[str, set[str]] | None" = None) -> None:
    """Collect the emission caps `node` crosses, each as the PAIR
    `(attenuation spelling, bound spelling)` — the two namespaces a crossing
    has (see `_canon_cap` / `_declared_cap`). The caller keeps whichever half
    its surface reads; nothing downstream has to re-derive the other.

    `region` is "emit-step" (count only MARKED crossings — the attenuation
    surface, like `_collect_emit_caps_pairs`) or "all" (also count any
    resolved emission call — a provide method's reach for the bound, like
    `_method_emissions.walk`). A spawn-handle emission is the unnameable
    `*` in both namespaces; a host emission (an emission extern, or a fn
    reaching one) is `*` for the fold and the capability tokens it reaches
    for the bound (`_reach_call`). `host_tokens` is the file's
    `_emitting_tokens` table.

    An `emit` marks its HEAD call only: `_emit_step_caps_pairs` reads the
    step's `expr.target` and nothing beneath it, so the arguments (and a
    `compensate` slot or `with` clause) keep the ENCLOSING region. For the
    F row that region is already "all" and nothing moves; for the A surface
    it stops a call evaluated inside an emit's argument list from counting
    as a marked crossing (#1169 F2, the `walk_calls` leak's twin)."""
    if node is None or isinstance(node, (str, int, float, bool)):
        return
    args = (requires, handles, psvc, bounds, em_set, emitting, aliases,
            host_tokens)
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


# ---------------------------------------- the approval floor (issue #1455)
#
# Item 246's declaration-owned floor (`lower._require_declared_approval`): a
# marked crossing that reaches a capability TOKEN some extern declared
# `requires approval` for must carry a `with` edge whose `Approval[C]` scope
# covers it. The model states the rule (`RevL.G4Approval.CrossingOK`); the
# exporter carries the three facts it is stated over:
#
#   AR <file> <token>               an approval-required token
#                                   (`lower._approval_index`'s `required`)
#   AX <file> <comp> <ord> <token>  one token marked crossing `ord` reaches
#                                   (`lower._approval_crossed_caps`)
#   AE <file> <comp> <ord> <scope>  that crossing's `with` edge, absent when
#                                   it has none
#
# One crossing per `emit` head (step or value form), plus one per emission
# crossing in a step's `compensate` slot, which shares the step's edge
# (`lower._compensate_crossings`). Only files that declare an
# approval-required token carry the rows: elsewhere every crossing is
# admitted by construction and a row would agree about nothing.


class _ApprovalCtx(NamedTuple):
    """What one component's crossings resolve against."""
    requires: dict           # require local -> service
    bounds: dict             # (service, op) -> (mode, declared entries)
    em_set: set              # (service, op) pairs declared `emission`
    handles: dict            # spawn handle var -> child component
    psvc: dict               # component -> provide key -> service
    aliases: dict            # provision aliases, as the marker rule reads them
    extern_caps: dict        # emission extern -> its tokens
    host_tokens: dict        # `_emitting_tokens`


def _approval_required(prog) -> list[str]:
    """`lower._approval_index`'s `required`: every extern that declares
    `requires approval` contributes its capability TOKENS, its declared scope
    when it has one and its name otherwise. Keyed by token, so the
    requirement belongs to the capability, not to the extern."""
    return sorted({token for e in prog.externs
                   if getattr(e, "requires_approval", False)
                   for token in (list(e.capabilities or ()) or [e.name])})


def _op_tokens(bounds: dict, svc: str, meth: str) -> "list[str] | None":
    """A service operation's declared scope, `*` when it declares none; None
    when the service has no such operation."""
    bound = bounds.get((svc, meth))
    if bound is None:
        return None
    _mode, entries = bound
    return list(entries) if entries else ["*"]


def _approval_tokens(call: object, ctx: _ApprovalCtx) -> list[str]:
    """`lower._approval_crossed_caps` over the AST: the tokens one marked
    crossing reaches, in the checker's order of resolution.

      1. a required service operation: its `emission[...]` scope, `*` when
         bare or unresolvable (`_emit_crossed_caps`, the `req` arm);
      2. a direct emission extern: its scope, or its name;
      3. a provision's op, however the receiver holds it: off a spawn
         handle, through a `let` alias, a field or element read off one, a
         receiver written in place (an `if`, a `match`, a record or list
         literal), a provide method's service-typed parameter, or an arrow's
         service-typed parameter that an application binds a provision into:
         the op's scope, `*` when bare (`_instance_get_call`,
         `_service_receiver_decl`, `_check_arrow_param_crossings`);
      4. a module `fn` (or a witnessed extern): the tokens it reaches
         (`env.emitting_caps`).

    Arm 3 reads the same alias table the marker rule does, through
    `_route_values`, so a receiver the `G` row resolves is the receiver the
    `AP` row resolves (issue #1455). An arrow never applied to a provision
    gets no alias and so reaches no token, as the checker admits it."""
    if not isinstance(call, ExprCall):
        return []
    rt = _route(call.callee)
    if rt is not None:
        root, chain = rt
        if root in ctx.requires and chain and "." not in chain:
            tokens = _op_tokens(ctx.bounds, ctx.requires[root], chain)
            return tokens if tokens is not None else ["*"]
        if not chain and root in ctx.extern_caps:
            return list(ctx.extern_caps[root])
    res = _provision_op(call, ctx)
    if res is not None:
        return _op_tokens(ctx.bounds, *res) or []
    if rt is not None and not rt[1] and rt[0] in ctx.host_tokens:
        return sorted(ctx.host_tokens[rt[0]])
    return []


def _provision_op(call: ExprCall, ctx: _ApprovalCtx) -> "tuple[str, str] | None":
    """(service, op) a call reaches through a provision receiver, read the
    way the marker rule reads it (`_route_values` over the alias table)."""
    rv = _route_values(call.callee)
    if rv is None:
        return None
    return _resolve_emission(rv[0], rv[1], {}, ctx.handles, ctx.psvc,
                             ctx.aliases)


def _is_emission_call_ast(call: ExprCall, ctx: _ApprovalCtx) -> bool:
    """`lower._is_emission_call` over the AST, for the crossings a
    `compensate` slot holds: a host callable that reaches a crossing, or an
    `emission` operation through a required service or a spawn handle."""
    rt = _route(call.callee)
    if rt is not None:
        root, chain = rt
        if not chain:
            return root in ctx.host_tokens
        if root in ctx.requires and "." not in chain:
            bound = ctx.bounds.get((ctx.requires[root], chain))
            return bound is not None and bound[0] != "plain"
    res = _provision_op(call, ctx)
    return res is not None and res in ctx.em_set


def _compensate_calls(node: object, ctx: _ApprovalCtx, out: list) -> None:
    """`lower._compensate_crossings`: every emission crossing in a
    `compensate` slot, outermost first. The slot is lowered bare, so a
    crossing there carries no marker and is found by walking."""
    if node is None or isinstance(node, (str, int, float, bool)):
        return
    if isinstance(node, ExprCall) and _is_emission_call_ast(node, ctx):
        out.append(node)
    if dataclasses.is_dataclass(node) and not isinstance(node, type):
        for f in dataclasses.fields(node):
            _compensate_calls(getattr(node, f.name), ctx, out)
        return
    if isinstance(node, (list, tuple)):
        for x in node:
            _compensate_calls(x, ctx, out)


def _approval_edges(node: object, edges: dict) -> None:
    """Fill `edges` (name -> the scope of the `Approval[C]` it holds) from the
    bindings that produce one: `let a = await approval[C] { ... }`, a `let`
    annotated `Approval[C]`, and a `let` aliasing a name already held."""
    if node is None or isinstance(node, (str, int, float, bool)):
        return
    if isinstance(node, LetApprovalStmt):
        edges[node.bind] = node.request.capability
    elif type(node).__name__ == "LetStmt":
        scope = _approval_scope_of(getattr(node, "type", None))
        value = getattr(node, "value", None)
        if scope is not None:
            edges[node.name] = scope
        elif isinstance(value, ExprVar) and value.name in edges:
            edges[node.name] = edges[value.name]
    if dataclasses.is_dataclass(node) and not isinstance(node, type):
        for f in dataclasses.fields(node):
            _approval_edges(getattr(node, f.name), edges)
        return
    if isinstance(node, (list, tuple)):
        for x in node:
            _approval_edges(x, edges)


def _approval_edge(expr: object, edges: dict) -> "str | None":
    """The scope a `with` clause's value carries. An expression the exporter
    cannot name is read as NO edge, the fail-closed direction: were the
    checker to admit it, the file would land in the fatal `formal-strict`."""
    if isinstance(expr, ExprVar):
        return edges.get(expr.name)
    return None


def _approval_crossings(node: object, ctx: _ApprovalCtx, edges: dict,
                        out: list) -> None:
    """Append `(tokens, edge)` for every marked crossing under `node`, in
    source order: an `emit` step's head under its `with` edge, then each
    crossing in its `compensate` slot under the same edge, and every `emit`
    value form with no edge (it has no `with` clause)."""
    if node is None or isinstance(node, (str, int, float, bool)):
        return
    if isinstance(node, EmitStmt):
        edge = _approval_edge(node.approval, edges)
        out.append((_approval_tokens(node.expr, ctx), edge))
        comp: list = []
        _compensate_calls(node.compensate, ctx, comp)
        for call in comp:
            out.append((_approval_tokens(call, ctx), edge))
        head = node.expr
        _approval_crossings(head.args if isinstance(head, ExprCall) else head,
                            ctx, edges, out)
        return
    if isinstance(node, EmitExpr):
        out.append((_approval_tokens(node.expr, ctx), None))
        head = node.expr
        _approval_crossings(head.args if isinstance(head, ExprCall) else head,
                            ctx, edges, out)
        return
    if dataclasses.is_dataclass(node) and not isinstance(node, type):
        for f in dataclasses.fields(node):
            _approval_crossings(getattr(node, f.name), ctx, edges, out)
        return
    if isinstance(node, (list, tuple)):
        for x in node:
            _approval_crossings(x, ctx, edges, out)


def approval_rows(rel: str, comp, ctx: _ApprovalCtx, svc_objs: dict,
                  psvc: dict) -> list[str]:
    """The `AX`/`AE` rows of one component: its activation body under the
    approvals it mints, then each provide method under those plus its own
    `Approval[C]`-typed parameters (read off the service declaration, by
    position). A crossing that reaches no token gets no row: it cannot meet
    the floor."""
    act_edges: dict[str, str] = {}
    for stmt in comp.body:
        if not isinstance(stmt, ProvideStmt):
            _approval_edges(stmt, act_edges)
    crossings: list[tuple[list[str], "str | None"]] = []
    for stmt in comp.body:
        if not isinstance(stmt, ProvideStmt):
            _approval_crossings(stmt, ctx, act_edges, crossings)
    for stmt in comp.body:
        if not isinstance(stmt, ProvideStmt):
            continue
        svc = svc_objs.get(psvc.get(comp.name, {}).get(stmt.key))
        for pm in stmt.methods:
            edges = dict(act_edges)
            decl = svc.methods.get(pm.name) if svc is not None else None
            declared = [t for _n, t in (decl.params if decl is not None else [])]
            written = list(getattr(pm, "param_types", None) or [])
            for i, name in enumerate(pm.params):
                ptype = (written[i] if i < len(written) and written[i]
                         else declared[i] if i < len(declared) else None)
                scope = _approval_scope_of(ptype)
                if scope is not None:
                    edges[name] = scope
                else:
                    edges.pop(name, None)  # a parameter shadows the binding
            _approval_edges(pm.body, edges)
            _approval_crossings(pm.body, ctx, edges, crossings)
    rows: list[str] = []
    ord_ = 0
    for tokens, edge in crossings:
        if not tokens:
            continue
        for token in sorted(set(tokens)):
            rows.append("\t".join(["AX", rel, comp.name, str(ord_), token]))
        if edge is not None:
            rows.append("\t".join(["AE", rel, comp.name, str(ord_), edge]))
        ord_ += 1
    return rows


# ---------------------------------------- G-MODEL-PLACE (issue #1811)

#: The placement refusals the `MPV` row decides: a role, or a council member,
#: placed off the device for a confidentiality origin. The other
#: `model-placement` refusals (a duplicate arm, an unknown action, a council
#: among candidates) are shape rules, not this one.
MODEL_PLACE_MESSAGE = "may not leave"
#
# Placement: a `route model` arm may not send a confidentiality origin to a
# model role declared `off_device`, directly or through a council member that
# receives it (`model_route.check`). Reach (item 519): a component that
# consults a model role is held to the role's `reaches [...]`, so the role's
# reach must be covered by what the component holds
# (`lower._check_model_attenuation`). The model states the placement rule
# (`RevL.ModelPlace`) and decides the reach with the spawn rule's proved
# `attenuatesB`; the exporter carries the placed arms (`MP`, `MO`), the roles
# a component consults (`ME`) and each role's reach (`MRC`).

from revl import model_council as _model_council  # noqa: E402
from revl import model_route as _model_route  # noqa: E402
from revl.lower import (  # noqa: E402
    _consults_a_model as _lower_consults_a_model,
    _model_reach_caps as _lower_model_reach_caps,
)


def _model_tables(prog, rel: str) -> tuple[dict, dict]:
    """The file's validated model roles and councils, or empty tables when
    the declarations are themselves refused (another rule's business)."""
    try:
        roles = _model_route.roles(prog, rel)
    except RevlError:
        return {}, {}
    try:
        councils = _model_council.check(prog, rel)
    except RevlError:
        councils = {}
    return roles, councils


def model_place_rows(rel: str, comp, roles: dict, councils: dict
                     ) -> tuple[list[str], list[str]]:
    """The `MP` rows of one component's route arms, and the roles the arms
    name (every candidate, a council's every member) for the reach edges."""
    from revl.parser import ModelRouteStmt  # noqa: PLC0415

    rows: list[str] = []
    named: list[str] = []
    for stmt in comp.body:
        if not isinstance(stmt, ModelRouteStmt):
            continue
        for arm in stmt.arms:
            for name in list(getattr(arm, "candidates", None) or (arm.role,)):
                council = councils.get(name)
                if council is not None:
                    placement = _model_route._council_placement(council, roles)
                    named.extend(placement["member_roles"])
                    for m in _model_route.receiving_members(placement, arm.origin):
                        rows.append("\t".join(["MP", rel, comp.name, stmt.action,
                                               arm.origin, m["role"],
                                               m["residence"]]))
                elif name in roles:
                    named.append(name)
                    rows.append("\t".join(["MP", rel, comp.name, stmt.action,
                                           arm.origin, name,
                                           roles[name].residence]))
    return rows, named


def model_reach_rows(rel: str, comp, roles: dict, named: list[str],
                     held: set[str], crossed_names: set[str],
                     host_tokens: dict, caps_seen: set) -> list[str]:
    """The `ME` and `MRC` rows of one component: the roles it consults,
    by a block naming them or a crossing placed on them by a `model.<role>`
    token, when it consults a model at all (`lower._consults_a_model` over
    its held set)."""
    held_caps = {parse_cap(c) for c in held}
    if not roles or not _lower_consults_a_model(held_caps):
        return []
    crossed = {c.token for c in held_caps}
    for name in crossed_names:
        crossed.update(host_tokens.get(name) or ())
    edges = set(named)
    for token in crossed:
        role = _model_route.role_of_crossing(token, roles)
        if role is not None:
            edges.add(role)
    rows: list[str] = []
    for role in sorted(edges):
        if role not in roles:
            continue
        rows.append("\t".join(["ME", rel, comp.name, role]))
    return rows


def model_role_reach_rows(rel: str, roles: dict, caps_seen: set) -> list[str]:
    """The `MRC` rows of a file: each role's reach, `*` when undeclared."""
    rows: list[str] = []
    for name in sorted(roles):
        for cap in sorted({c.to_str() for c in _lower_model_reach_caps(roles[name])}):
            caps_seen.add(cap)
            rows.append("\t".join(["MRC", rel, name, cap]))
    return rows


# ------------------------------- out of scope by kind (issue #1810)
#
# `out-of-fragment` collects refusals under a rule the model states no row
# about. Some are rules the model could carry (unbuilt work); others are out
# of scope BY KIND: the type checker, which STATUS.md places outside the
# guarantee backbone, and name resolution of declarations and of the
# lifecycle test DSL. The second kind is routed to an informational
# `out-of-scope` bucket by an explicit rule, never by a list of files, so
# `out-of-fragment` holds only unbuilt work.

#: The type-checker codes: out of scope whatever the message.
OUT_OF_SCOPE_CODES = frozenset({"T1", "T2", "T3"})

#: The uncoded (`REVL`) refusals that are name resolution, by message: the
#: lifecycle test DSL's names, and a declaration naming an unknown or
#: duplicate service.
OUT_OF_SCOPE_MESSAGES = (
    re.compile(r"is not a config field of "),
    re.compile(r"^`[^`]+` is already loaded$"),
    re.compile(r"^unknown lifecycle assertion `"),
    re.compile(r"^unknown component `"),
    re.compile(r"is not an operation of service "),
    re.compile(r"^unknown service `[^`]+` in `requires`"),
    re.compile(r"^duplicate service `"),
)


def out_of_scope(code: str, message: str) -> bool:
    """Whether a refusal is out of the model's scope by kind. A refusal with
    a guarantee code (anything but the type-checker codes and the uncoded
    `REVL`) never is."""
    if code in OUT_OF_SCOPE_CODES:
        return True
    return code == "REVL" and any(p.search(message) for p in OUT_OF_SCOPE_MESSAGES)


# ------------------------ three declaration rules (issue #1809)
#
# Prelude ordering (`isolate`, `intercept`, `handoff`, a `realms(...)` route
# and a model route precede every action), intercept target (an `intercept`
# names a required key, not a provision) and method in service (a named
# operation is one its service declares, A6). The model states each
# (`RevL.Prelude`); the exporter carries `PS` (the activation body as
# preludes and actions), `IT` (intercept targets) and `MC` (the operations a
# component names).

#: The statement kinds the checker's activation loop treats as preludes.
PRELUDE_KINDS = ("IsolateStmt", "InterceptStmt", "HandoffStmt", "RouteStmt",
                 "ModelRouteStmt")

#: The uncoded refusals these rows decide, matched by message.
PRELUDE_MESSAGE = "must precede every effect, emit, await, and provide"
INTERCEPT_MESSAGE = "`intercept` applies to required keys only"
#: The A6 refusal the `MS` row decides (A6 also covers arity and signature).
METHOD_MESSAGE = "is not a method of service"


def prelude_rows(rel: str, comp, op_calls: list, svc_of_key: dict) -> list[str]:
    """The `PS`, `IT` and `MC` rows of one component. `op_calls` are the
    (service, operation) crossings the component's statements resolve."""
    rows: list[str] = []
    for ord_, stmt in enumerate(comp.body):
        kind = "prelude" if type(stmt).__name__ in PRELUDE_KINDS else "action"
        rows.append("\t".join(["PS", rel, comp.name, str(ord_), kind]))
        if type(stmt).__name__ == "InterceptStmt":
            rows.append("\t".join(["IT", rel, comp.name, stmt.key]))
    ops = list(op_calls)
    for stmt in comp.body:
        if isinstance(stmt, ProvideStmt):
            svc = svc_of_key.get(stmt.key)
            if svc is not None:
                ops.extend((svc, pm.name) for pm in stmt.methods)
    for svc, meth in sorted(set(ops)):
        rows.append("\t".join(["MC", rel, comp.name, svc, meth]))
    return rows


def prelude_ok(steps: list[str]) -> bool:
    """The reference's prelude rule: no prelude after the first action."""
    seen_action = False
    for kind in steps:
        if kind == "action":
            seen_action = True
        elif seen_action:
            return False
    return True


# ------------------------------------------ async colour (issue #1808)
#
# The checker's A1 rules (`lower._admit_effect_async`, `_admit_emit_async`,
# the provide-method admission): a sync method, an unawaited step and a
# teardown slot may not reach an async operation, and an awaited step must.
# The model states them over SITES (`RevL.A1Async.SiteOK`); the exporter
# carries the async names (`AN`), one row per site with the heads it calls
# (`AS`), and each provide method's two colours (`AG`). The reach itself is
# the model's, over the `FN` call graph.

#: The A1 refusal the model cannot state: an arrow's type has no colour.
A1_ARROW_MESSAGE = "but its type carries no async color"
#: The uncoded signature refusal the `A1S` row decides.
A1_SIGNATURE_MESSAGE = "is not async but service"


def async_names(prog) -> list[str]:
    """The file's async names: async externs, and async service operations
    spelled `<Service>.<op>`."""
    names = {e.name for e in prog.externs if getattr(e, "async_", False)}
    for svc in prog.services:
        for meth, decl in svc.methods.items():
            if getattr(decl, "async_", False):
                names.add(f"{svc.name}.{meth}")
    return sorted(names)


class _AsyncCtx(NamedTuple):
    """What a site's heads resolve against."""
    requires: dict
    handles: dict
    psvc: dict
    aliases: dict


def _async_heads(node: object, ctx: _AsyncCtx) -> list[str]:
    """The heads a site calls, in first-call order: a bare-name callee as
    itself, a service operation reached through a requirement, a spawn
    handle or an alias as `<Service>.<op>`."""
    found: list[tuple[str, str, str]] = []
    walk_calls(node, found, "plain")
    out: list[str] = []
    for root, chain, _c in found:
        if not chain:
            name = root
        else:
            res = _resolve_emission(root, chain, ctx.requires, ctx.handles,
                                    ctx.psvc, ctx.aliases)
            if res is None:
                continue
            name = f"{res[0]}.{res[1]}"
        if name not in out:
            out.append(name)
    return out


def _slot_sites(stmts, ctx: _AsyncCtx, out: list) -> None:
    """The teardown slots of the effect and emit steps under `stmts`."""
    for node in _walk_nodes(stmts):
        if isinstance(node, (EffectStmt, LetEffect)) \
                and getattr(node, "undo", None) is not None:
            out.append(("undo", _async_heads(node.undo, ctx)))
        if isinstance(node, EmitStmt) \
                and getattr(node, "compensate", None) is not None:
            out.append(("compensate", _async_heads(node.compensate, ctx)))


def _walk_nodes(node):
    """Every AST node under `node`, outermost first."""
    if node is None or isinstance(node, (str, int, float, bool)):
        return
    if dataclasses.is_dataclass(node) and not isinstance(node, type):
        yield node
        for f in dataclasses.fields(node):
            yield from _walk_nodes(getattr(node, f.name))
    elif isinstance(node, (list, tuple)):
        for x in node:
            yield from _walk_nodes(x)


def async_rows(rel: str, comp, ctx: _AsyncCtx, svc_objs: dict,
               psvc: dict) -> list[str]:
    """The `AS` and `AG` rows of one component."""
    sites: list[tuple[str, list[str]]] = []
    for stmt in comp.body:
        if isinstance(stmt, ProvideStmt):
            continue
        if isinstance(stmt, (EffectStmt, LetEffect)):
            heads = _async_heads(stmt.acquire, ctx)
            for h in _async_heads(getattr(stmt, "setup", None), ctx):
                if h not in heads:
                    heads.append(h)
            sites.append(("effectAwait" if stmt.is_async else "effect", heads))
        elif isinstance(stmt, EmitStmt):
            sites.append(("emitAwait" if stmt.is_async else "emit",
                          _async_heads(stmt.expr, ctx)))
        _slot_sites([stmt], ctx, sites)
    sigs: list[str] = []
    for stmt in comp.body:
        if not isinstance(stmt, ProvideStmt):
            continue
        svc = svc_objs.get(psvc.get(comp.name, {}).get(stmt.key))
        for pm in stmt.methods:
            decl = svc.methods.get(pm.name) if svc is not None else None
            if decl is None:
                continue
            declared = bool(getattr(decl, "async_", False))
            sites.append(("asyncMethod" if declared else "syncMethod",
                          _async_heads(pm.body, ctx)))
            _slot_sites(pm.body, ctx, sites)
            sigs.append("\t".join([
                "AG", rel, comp.name, f"{stmt.key}.{pm.name}",
                "async" if declared else "sync",
                "async" if getattr(pm, "async_", False) else "sync"]))
    rows = ["\t".join(["AS", rel, comp.name, str(i), kind, ",".join(heads)])
            for i, (kind, heads) in enumerate(sites)]
    return rows + sigs


def async_reaching(names: set[str], graph: dict[str, list[str]]) -> set[str]:
    """The reference's reach: the names that are async or call, through the
    `fn` graph, one that is. A true fixed point."""
    reached = set(names)
    changed = True
    while changed:
        changed = False
        for fn, callees in graph.items():
            if fn not in reached and any(c in reached for c in callees):
                reached.add(fn)
                changed = True
    return reached


def async_site_ok(kind: str, reaches: bool) -> bool:
    """The reference's A1 rule for one site."""
    if kind in ("effectAwait", "emitAwait"):
        return reaches
    if kind == "asyncMethod":
        return True
    return not reaches


def checker_message(rel: str) -> str:
    """The shipped checker's refusal message on one corpus file, or ""."""
    try:
        compile_files([str(REPO / rel)])
        return ""
    except RevlError as e:
        return e.message


# ---------------------------------------- declared access (issue #1807)
#
# The checker refuses a call head whose root names no declared requirement
# (G1, "`db` is not a declared requirement of C"). The model states the rule
# over a component's ACCESS roots (`RevL.G1Access.AccessOK`); the exporter
# carries them, one `GA <file> <comp> <root>` row each: every call head's
# root, at every nesting depth, less the roots the checker resolves without a
# requirement. The requirements themselves are NOT dropped here, so the
# model is what checks a root against the declared ones.

#: The builtin constructors every program can name.
_BUILTIN_CTORS = frozenset({"Ok", "Err", "Some", "None"})

#: Binding forms whose `name`/`bind` puts a local in view.
_BINDING_NODES = ("LetStmt", "LetEffect", "ForStmt", "LetApprovalStmt",
                  "StreamIterStmt", "CallStmt")


def _component_locals(comp) -> set[str]:
    """Every name a binding puts in view anywhere in the component: `let`,
    `var`, `let ... = effect`, a loop or stream binder, an approval, a
    provide method's parameters, an arrow's parameters and a `match` arm's
    binder. Read component-wide, not per scope (see `G1_KeyAccess.lean`)."""
    out: set[str] = set()

    def walk(node) -> None:
        if node is None or isinstance(node, (str, int, float, bool)):
            return
        kind = type(node).__name__
        if kind in _BINDING_NODES:
            for attr in ("name", "bind"):
                value = getattr(node, attr, None)
                if isinstance(value, str):
                    out.add(value)
        if kind == "LetPatternStmt":
            _pattern_names(getattr(node, "pattern", None), out)
        if isinstance(node, ExprArrow):
            out.update(node.params)
        if isinstance(node, ExprMatch):
            for arm in node.arms:
                if len(arm) > 1 and isinstance(arm[1], str):
                    out.add(arm[1])
        if isinstance(node, ProvideStmt):
            for pm in node.methods:
                out.update(pm.params)
        if dataclasses.is_dataclass(node) and not isinstance(node, type):
            for f in dataclasses.fields(node):
                walk(getattr(node, f.name))
        elif isinstance(node, (list, tuple)):
            for x in node:
                walk(x)

    walk(comp.body)
    return out


def _pattern_names(pattern, out: set[str]) -> None:
    """The names a destructuring pattern binds."""
    if isinstance(pattern, str):
        out.add(pattern)
    elif dataclasses.is_dataclass(pattern) and not isinstance(pattern, type):
        for f in dataclasses.fields(pattern):
            _pattern_names(getattr(pattern, f.name), out)
    elif isinstance(pattern, (list, tuple)):
        for x in pattern:
            _pattern_names(x, out)


def _file_resolved_names(prog) -> set[str]:
    """The roots a call head may name without a requirement, file-wide: a
    module `fn` or `extern`, a name or namespace a `use` imports, a host
    family, and a type or variant constructor."""
    names = {f.name for f in prog.fn_decls} | {e.name for e in prog.externs}
    for use in prog.uses:
        names.update(use.names or ())
        if use.alias:
            names.add(use.alias)
    for td in prog.type_decls:
        names.add(td.name)
        names.update(case.name for case in td.cases or ())
    return names | set(_HOST_CALLABLES) | _BUILTIN_CTORS


#: Per component, which kinds of root the export dropped: `(file, comp) ->
#: set of "local" / "callable" / "host"`. Read by `access_coverage`.
_ACCESS_DROPPED: dict = {}


def _value_names(node) -> set[str]:
    """The root of every name the component reads (`ExprVar`), dotted names
    by their first segment."""
    out: set[str] = set()

    def walk(n) -> None:
        if n is None or isinstance(n, (str, int, float, bool)):
            return
        if isinstance(n, ExprVar):
            out.add(n.name.partition(".")[0])
            return
        if dataclasses.is_dataclass(n) and not isinstance(n, type):
            for f in dataclasses.fields(n):
                walk(getattr(n, f.name))
        elif isinstance(n, (list, tuple)):
            for x in n:
                walk(x)

    walk(node)
    return out


def access_rows(rel: str, comp, roots: set[str], resolved: set[str],
                callables: set[str]) -> list[str]:
    """The `GA` rows of one component from the call-head roots it makes and
    the names it reads in value position (`nope + v` in a provide body is
    refused as `nope` is not a declared requirement, issue #1699's block-arm
    fixture), less the roots the checker resolves without a requirement."""
    local = _component_locals(comp)
    provided = {key for key, _svc, _line in comp.provides}
    access: set[str] = set()
    dropped: set[str] = set()
    for root in set(roots) | (_value_names(comp.body) - {"config"}):
        if not root or root.startswith("@"):
            continue  # a receiver written in place: its own heads are walked
        if root in local:
            dropped.add("local")
        elif root in callables:
            dropped.add("callable")
        elif root in _HOST_CALLABLES:
            dropped.add("host")
        elif root not in resolved:
            access.add(root)
    for stmt in comp.body:
        if type(stmt).__name__ == "InterceptStmt" and stmt.key not in provided:
            # an `intercept` target is a dependency key (Def. 30); one on a
            # provision is a different refusal (issue #1809)
            access.add(stmt.key)
    _ACCESS_DROPPED[(rel, comp.name)] = dropped
    return ["\t".join(["GA", rel, comp.name, root]) for root in sorted(access)]


# ------------------------------------ binding uniqueness (issue #1812)
#
# The checker's G6 `binding` refusal (`Env.bind_local` in the activation body,
# `_check_rebind` in a provide method): a binding whose name is already in
# view. The model states the rule over a scope's steps
# (`RevL.G6Binding.ScopeOK`); the exporter carries them, one `BE` row each:
#
#   BE <file> <comp> <scope> <ord> seed  <name>  a name in view at the start
#   BE <file> <comp> <scope> <ord> bind  <name>  a binding
#   BE <file> <comp> <scope> <ord> enter -       a block opens
#   BE <file> <comp> <scope> <ord> leave -       the innermost block closes
#
# `@act` is the activation body, seeded with the `requires` locals. A provide
# method `<key>.<method>` is seeded with the activation bindings made before
# its `provide` block and with its own parameters; a `requires` local is not
# in view there (`_check_rebind` consults params, method locals and
# `env.locals`). Blocks are the method's `if` arms and `while`/`for` bodies
# (`_lower_scoped_block`), a `for` binder living in its loop's block, and in
# the activation body an effect's `setup { ... }` block and a stream
# iteration's body.

ACT_SCOPE = "@act"


def _act_binding_events(stmt, out: list) -> None:
    """The `BE` events one activation-body statement contributes."""
    setup = getattr(stmt, "setup", None)
    if isinstance(stmt, (LetEffect, EffectStmt)) and setup:
        out.append(("enter", "-"))
        for inner in setup:
            if type(inner).__name__ == "LetStmt":
                out.append(("bind", inner.name))
        out.append(("leave", "-"))
    if isinstance(stmt, (LetEffect, LetApprovalStmt)) \
            and isinstance(getattr(stmt, "bind", None), str):
        out.append(("bind", stmt.bind))
    elif isinstance(stmt, StreamIterStmt):
        out.append(("enter", "-"))
        out.append(("bind", stmt.bind))
        for inner in stmt.body or ():
            _act_binding_events(inner, out)
        out.append(("leave", "-"))


def _method_binding_events(stmts, out: list) -> None:
    """The `BE` events a provide-method body contributes, in source order."""
    for ms in stmts or ():
        kind = type(ms).__name__
        if kind == "LetStmt":
            out.append(("bind", ms.name))
        elif isinstance(ms, LetEffect) and isinstance(ms.bind, str):
            out.append(("bind", ms.bind))
        elif kind == "IfStmt":
            out.append(("enter", "-"))
            _method_binding_events(ms.then, out)
            out.append(("leave", "-"))
            if ms.otherwise is not None:
                out.append(("enter", "-"))
                _method_binding_events(ms.otherwise, out)
                out.append(("leave", "-"))
        elif kind == "WhileStmt":
            out.append(("enter", "-"))
            _method_binding_events(ms.body, out)
            out.append(("leave", "-"))
        elif kind == "ForStmt":
            out.append(("enter", "-"))
            out.append(("bind", ms.bind))
            _method_binding_events(ms.body, out)
            out.append(("leave", "-"))


def binding_rows(rel: str, comp) -> list[str]:
    """The `BE` rows of one component: its activation body, then each
    provide method."""
    rows: list[str] = []

    def emit(scope: str, seed: list, events: list) -> None:
        steps = [("seed", n) for n in seed] + events
        for ord_, (kind, name) in enumerate(steps):
            rows.append("\t".join(["BE", rel, comp.name, scope, str(ord_),
                                   kind, name]))

    act: list = []
    bound: list[str] = []   # the activation's top-level bindings so far
    methods: list = []
    for stmt in comp.body:
        if isinstance(stmt, ProvideStmt):
            for pm in stmt.methods:
                events: list = []
                _method_binding_events(pm.body, events)
                methods.append((f"{stmt.key}.{pm.name}",
                                list(bound) + list(pm.params), events))
            continue
        mine: list = []
        _act_binding_events(stmt, mine)
        depth = 0
        for kind, name in mine:
            depth += {"enter": 1, "leave": -1}.get(kind, 0)
            if kind == "bind" and depth == 0:
                bound.append(name)
        act.extend(mine)
    emit(ACT_SCOPE, [local for local, _svc, _line in comp.requires], act)
    for scope, seed, events in methods:
        emit(scope, seed, events)
    return rows


def binding_verdict(seed: list[str], events: list[tuple[str, str]]) -> bool:
    """The reference's binding rule over one scope, recomputed from the `BE`
    rows: every bind's name is in no open frame, and a block's bindings are
    dropped when it closes. The outermost frame is never dropped."""
    frames: list[set[str]] = [set(seed)]
    for kind, name in events:
        if kind == "bind":
            if any(name in f for f in frames):
                return False
            frames[-1].add(name)
        elif kind == "enter":
            frames.append(set())
        elif kind == "leave" and len(frames) > 1:
            frames.pop()
    return True


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
    itself a refusal ('emission not declared'). A bracket's `undo` slot is
    recorded as 'undo': judged as 'plain' for a service crossing, and given no
    host-emission row, because the checker lowers it in teardown mode."""
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
                route = _route_values(expr.callee)
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
        route = _route_values(node.callee)
        if route is not None:
            out.append((*route, ctx))
        for a in node.args:
            walk_calls(a, out, ctx)
        return
    if isinstance(node, (EffectStmt, LetEffect)):
        # A bracket's `undo` is a teardown slot. The checker lowers it in
        # "undo" mode, where the marker rule does not apply (an emission there
        # is G5's to refuse), so its calls are recorded as `undo`: judged as
        # `plain` for a service crossing, as before, and given no host row.
        for f in dataclasses.fields(node):
            walk_calls(getattr(node, f.name), out,
                       "undo" if f.name == "undo" else ctx)
        return
    if isinstance(node, ExprArrow) and ctx == "emitarg":
        # An arrow's body runs when the arrow is CALLED, not while the
        # enclosing emit's arguments are evaluated: it leaves the argument
        # list (`lower.py` clears `_in_emit_args` for the body), so a marked
        # crossing written inline there is the same crossing it is when the
        # arrow is bound by `let` and passed by name.
        walk_calls(node.body, out, "plain")
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


#: The positions a reach of a `deferred` emission extern can sit in (`DR`
#: rows, issue #1742), and the scopes. Read off the same AST walk the checker
#: makes (`lower._refuse_teardown_externs_in_fn_bodies`): a CALL is the extern
#: in a call's callee position, `arrow` when that call sits inside an arrow,
#: and a VALUE is any other reference to it. The rule over them is stated in
#: the model (`RevL.G4Deferred`), not here.
DEFERRED_SCOPES = ("fn", "test", "component")
DEFERRED_POSITIONS = ("call", "arrow", "value")


def _deferred_reaches(node, deferred: set, out: list, in_arrow: bool = False) -> None:
    """Every reach of a `deferred` extern under `node`, as (extern, position)."""
    if isinstance(node, ExprArrow):
        in_arrow = True
    if isinstance(node, ExprVar) and node.name in deferred:
        out.append((node.name, "value"))
    if isinstance(node, ExprCall) and isinstance(node.callee, ExprVar) \
            and node.callee.name in deferred:
        out.append((node.callee.name, "arrow" if in_arrow else "call"))
    if dataclasses.is_dataclass(node) and not isinstance(node, type):
        for f in dataclasses.fields(node):
            child = getattr(node, f.name)
            if f.name == "callee" and isinstance(node, ExprCall) \
                    and isinstance(child, ExprVar):
                continue  # the callee position is the CALL, recorded above
            _deferred_reaches(child, deferred, out, in_arrow)
    elif isinstance(node, (list, tuple)):
        for x in node:
            _deferred_reaches(x, deferred, out, in_arrow)


def deferred_reach_rows(prog, rel: str) -> list[str]:
    """`DR <file> <scope> <owner> <extern> <position>`: one row per reach of a
    `deferred` emission extern, in the body of a `fn`, a `test` or a
    component (issue #1742). Facts only: which reaches are legal is the
    model's `RevL.G4Deferred.legalB`."""
    deferred = {e.name for e in prog.externs if getattr(e, "deferred", False)}
    if not deferred:
        return []
    rows: list[str] = []
    scopes = ([("fn", fn.name, fn.body) for fn in prog.fn_decls]
              + [("test", t.name, t.body) for t in prog.tests]
              + [("component", c.name, c.body) for c in prog.components])
    for scope, owner, body in scopes:
        found: list = []
        _deferred_reaches(body, deferred, found)
        for ext, pos in found:
            rows.append("\t".join(["DR", rel, scope, owner, ext, pos]))
    return rows


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
        # The capability TOKENS each host callable reaches, for the F row's
        # bound column (`_reach_call`) and the approval floor's crossings.
        host_tokens = _emitting_tokens(prog)
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
        extern_class_of = {e.name: e.classification for e in prog.externs}
        resolved_names = _file_resolved_names(prog)
        # model roles and councils (issue #1811), file-wide
        model_roles, model_councils = _model_tables(prog, str(path))
        file_has_mp = False
        tsv.extend(model_role_reach_rows(rel, model_roles, caps_seen))
        # async names (AN, issue #1808), file-wide
        for name in async_names(prog):
            tsv.append("\t".join(["AN", rel, name]))
        file_callables = {f.name for f in prog.fn_decls} \
            | {e.name for e in prog.externs}
        # The service operations a bracket inverse READS as a value
        # (`undo dispatch1(w.task.run)`, issue #1792), each declared in the
        # `Prog` below as an `emission` boundary named `<Service>.<op>`. No
        # extern can be spelled with a dot, so the names cannot collide.
        op_boundaries: dict[str, list[str]] = {}
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
        # deferred-position facts (DR, issue #1742), file-wide: every reach of
        # a `deferred` emission extern and where it sits.
        tsv.extend(deferred_reach_rows(prog, rel))

        # approval-floor facts (AR), file-wide: the approval-required tokens.
        # The per-crossing AX/AE rows follow each component below.
        approval_required = _approval_required(prog)
        for token in approval_required:
            tsv.append("\t".join(["AR", rel, token]))
        extern_caps = {e.name: list(e.capabilities or ()) or [e.name]
                       for e in prog.externs
                       if getattr(e, "classification", "") == "emission"}

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
            # declares one, the namespaced SERVICE where it does not
            # (`lower._held_capabilities_pairs`, clause for clause).
            krows: list[tuple[str, str]] = []
            for local, svc in requires:
                em = [(mode, ents) for (s, _m), (mode, ents) in bounds.items()
                      if s == svc and mode != "plain"]
                if not em:
                    krows.append((local, _undeclared_cap(svc)))
                    continue
                for mode, ents in em:
                    if mode == "any":
                        krows.append((local, _undeclared_cap(svc)))
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
            # the method parameters first, so a receiver written in place over
            # one (`(if c { p } else { p }).charge(n)`) resolves through it
            collect_service_params(c, psvc, svc_objs, aliases)
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
                           host_tokens)
            act_caps = {cap for cap, _bound in act_reach}
            caps_seen.update(act_caps)
            for cap in sorted(act_caps):
                tsv.append("\t".join(["A", rel, c.name, cap]))
            # the component's held set, for the model-reach edges (#1811)
            held_strs: set[str] = set(act_caps) | {cap for _l, cap in krows}

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
                                       host_tokens)
                        for cap, bound in sorted(reach):
                            caps_seen.add(cap)
                            caps_seen.add(bound)
                            held_strs.add(cap)
                            tsv.append("\t".join(
                                ["F", rel, c.name, stmt.key, svc, pm.name,
                                 cap, bound]))

            # approval-floor facts (AX/AE): each marked crossing's tokens and
            # its `with` edge, in a file that declares an approval-required
            # token (issue #1455).
            if approval_required:
                tsv.extend(approval_rows(rel, c, _ApprovalCtx(
                    require_map, bounds, em_set, handles, psvc, aliases,
                    extern_caps, host_tokens), svc_objs, psvc))
            # binding-uniqueness facts (BE, issue #1812): each scope's seed
            # names and its bind/enter/leave steps.
            tsv.extend(binding_rows(rel, c))

            calls: list[tuple[str, str, str, str]] = []
            kinds: list[str] = []
            terms: list[tuple[int, str, list[str], list[str]]] = []
            head_roots: set[str] = set()
            head_calls: list[tuple[str, str]] = []

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
                if type(stmt).__name__ == "LetStmt" \
                        and isinstance(getattr(stmt, "name", None), str):
                    inv_lets[stmt.name] = stmt.value
                if isinstance(stmt, (EffectStmt, LetEffect)):
                    kinds.append("effect")
                    primary = _term_heads(getattr(stmt, "acquire"))
                    inverse = _term_heads(getattr(stmt, "undo"))
                    # what the inverse reaches through an indirection (a
                    # dispatched arrow, a passed reference, a bound value),
                    # as `lower._walk_inverse_emissions` resolves it (#1792)
                    for head in inverse_reach_heads(
                            getattr(stmt, "undo"), _InverseCtx(
                                inv_lets, emitting, extern_class_of,
                                require_map, handles, psvc, aliases,
                                services)):
                        if head not in inverse:
                            inverse.append(head)
                        if "." in head:
                            op_boundaries[head] = bounds.get(
                                tuple(head.split(".", 1)), ("", []))[1]
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
                head_roots.update(root for root, _chain, _ctx in local_calls)
                head_calls.extend((root, chain) for root, chain, _c in local_calls)
                for root, chain, ctx in local_calls:
                    res = _resolve_emission(root, chain, require_map, handles,
                                            psvc, aliases)
                    if res is None and not chain and root in emitting \
                            and ctx in HOST_MARKER_CONTEXTS:
                        # A HOST emission: a named call to an `emission`
                        # extern or to a fn reaching one. It has no service
                        # to resolve, so the row names the pseudo-service
                        # `@host`, which the oracle's `g4OK` reads as an
                        # emission; `_fn_emitting` never admits a witnessed,
                        # acquire or pure extern, so every such row IS one.
                        calls.append((root, HOST_SERVICE, root, ctx))
                        tsv.append("\t".join(
                            ["U", rel, c.name, ctx, root, HOST_SERVICE, root]))
                        if ctx != "emit":
                            saw_raw = True
                        continue
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
            # `inv_lets`: the `let` bindings in scope, read by an inverse that
            # dispatches a bound value. A method sees the activation's lets
            # and its own, its parameters shadowing both.
            inv_lets: dict[str, object] = {}
            for stmt in c.body:
                if not isinstance(stmt, ProvideStmt):
                    classify_stmt(stmt)
            act_lets = dict(inv_lets)
            for stmt in c.body:
                if isinstance(stmt, ProvideStmt):
                    for pm in stmt.methods:
                        inv_lets = {k: v for k, v in act_lets.items()
                                    if k not in pm.params}
                        for inner in pm.body:
                            classify_stmt(inner)
            tsv.extend(f"T\t{rel}\t{c.name}\t{k}" for k in kinds)
            # model placement and reach facts (MP/ME, issue #1811)
            mp_rows, mp_named = model_place_rows(rel, c, model_roles,
                                                 model_councils)
            tsv.extend(mp_rows)
            if mp_rows:
                file_has_mp = True
            tsv.extend(model_reach_rows(
                rel, c, model_roles, mp_named, held_strs,
                {root for root, chain in head_calls
                 if not chain and root in host_tokens},
                host_tokens, caps_seen))
            # declaration-rule facts (PS/IT/MC, issue #1809)
            op_calls = []
            for root, chain in head_calls:
                res = _resolve_emission(root, chain, require_map, handles,
                                        psvc, aliases)
                if res is not None and res[0] in services \
                        and "." not in res[1] and "[]" not in res[1]:
                    op_calls.append(res)
            tsv.extend(prelude_rows(rel, c, op_calls, psvc.get(c.name, {})))
            # async-colour facts (AS/AG, issue #1808)
            tsv.extend(async_rows(rel, c, _AsyncCtx(
                require_map, handles, psvc, aliases), svc_objs, psvc))
            # declared-access facts (GA, issue #1807)
            tsv.extend(access_rows(rel, c, head_roots, resolved_names,
                                   file_callables))
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
        if file_has_mp:
            for origin in _model_route.CONFIDENTIALITY_ORIGINS:
                tsv.append("\t".join(["MO", rel, origin]))
        for name, caps in sorted(op_boundaries.items()):
            tsv.append("\t".join(["EX", rel, name, "emission", "-", "-",
                                   ",".join(caps)]))
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


#: What the REFERENCE computed for each DF row: (admitted, the file's
#: (scope, position) reaches). Filled by `reference_from_tsv`; read by
#: `deferred_coverage`.
_DEFERRED_FILES: dict = {}


def deferred_coverage() -> list[str]:
    """The non-vacuity ratchet for the `DF` row (issue #1742).

    A row that said `ok` over files with no `deferred` extern would agree
    over nothing. The corpus must EXERCISE the rule on both sides of it:

      * a file ADMITTED with at least one reach (`ok_emit_step.rvl`: the
        extern called in a component);
      * a file REFUSED for a call in a `fn` or `test` body
        (`g4_call_in_fn_body.rvl`), one for a call inside an arrow there
        (`g4_call_in_arrow_in_fn_body.rvl`), and one for a value reference
        in a component (`g4_value_in_component.rvl`).

    The verdict is the model's `RevL.G4Deferred.deferredB` on the Lean side,
    so a reference that drifted to admit one of those shapes diverges from
    the Lean row. Returns findings, treated as gate failures."""
    witnesses = {
        "an admitted file that reaches a deferred extern":
            lambda ok, reach: ok and bool(reach),
        "a file refused for a call in a fn or test body":
            lambda ok, reach: not ok and any(
                sc in ("fn", "test") and pos == "call" for sc, pos in reach),
        "a file refused for a call inside an arrow in a fn or test body":
            lambda ok, reach: not ok and any(
                sc in ("fn", "test") and pos == "arrow" for sc, pos in reach),
        "a file refused for a value reference in a component":
            lambda ok, reach: not ok and ("component", "value") in reach,
    }
    findings: list[str] = []
    found: dict[str, str] = {}
    for label, test in witnesses.items():
        hit = next((rel for rel, (ok, reach) in sorted(_DEFERRED_FILES.items())
                    if test(ok, reach)), None)
        if hit is None:
            findings.append(f"deferred coverage: NO witness of {label} — "
                            "the DF row would agree vacuously")
        else:
            found[label] = hit
    if not findings:
        reaching = sum(1 for _ok, reach in _DEFERRED_FILES.values() if reach)
        print(f"deferred coverage: {len(_DEFERRED_FILES)} files, {reaching} "
              f"reach a deferred extern; witnesses "
              + ", ".join(sorted(set(found.values()))))
    return findings


#: What the REFERENCE decided for each marked crossing under the approval
#: floor: (admitted, carries an edge, reaches an approval-required token).
#: Filled by `reference_from_tsv`, read by `approval_coverage`.
_APPROVAL_ROWS: dict = {}


def approval_coverage() -> list[str]:
    """The non-vacuity ratchet for the `AP` row (issue #1455).

    Every approval refusal in the corpus is a crossing with NO edge, and a
    row that only ever refused edgeless crossings could not tell the floor
    from "an approval-required token is never crossed". So the corpus must
    exercise the edge on the reference's own coverage test:

      * a crossing ADMITTED because its edge covers the approval-required
        token it reaches (the head of
        `examples/rejections/g4_approval_compensate_other_edge.rvl`);
      * a crossing REFUSED although it carries an edge, because the edge's
        scope does not reach the token (that file's compensation);
      * a crossing REFUSED with no edge at all (the value form, and every
        other approval fixture);
      * a crossing ADMITTED with no edge because it reaches no
        approval-required token (the head of
        `examples/rejections/g4_approval_compensate.rvl`): the floor is keyed
        by token, not by the file.

    Returns findings, treated as gate failures."""
    witnesses = {"covered": None, "other-edge": None, "no-edge": None,
                 "unrequired": None}
    for key, (ok, has_edge, needed) in sorted(_APPROVAL_ROWS.items()):
        if ok and has_edge and needed:
            witnesses["covered"] = witnesses["covered"] or key
        if not ok and has_edge:
            witnesses["other-edge"] = witnesses["other-edge"] or key
        if not ok and not has_edge:
            witnesses["no-edge"] = witnesses["no-edge"] or key
        if ok and not needed:
            witnesses["unrequired"] = witnesses["unrequired"] or key
    labels = {
        "covered": "a crossing admitted because its edge covers the token",
        "other-edge": "a crossing refused under an edge that does not cover it",
        "no-edge": "a crossing refused for carrying no edge",
        "unrequired": "a crossing admitted with no edge, reaching no "
                      "approval-required token",
    }
    findings = [f"approval coverage: NO witness of {labels[k]} — the AP row "
                "would agree vacuously" for k, w in witnesses.items() if w is None]
    if not findings:
        print(f"approval coverage: {len(_APPROVAL_ROWS)} crossings; "
              + " ".join(f"{k}={w}" for k, w in witnesses.items()))
    return findings


#: What the REFERENCE decided for each model placement and reach edge:
#: `("place", file, comp) -> (admitted, routes a confidentiality origin)`,
#: `("reach", file, comp, role) -> (admitted, True)`.
_MODEL_ROWS: dict = {}


def model_coverage() -> list[str]:
    """The non-vacuity ratchet for the `MPV` and `MAV` rows (issue #1811):
    a confidentiality origin placed on the device and one placed off it, and
    a consulted role whose reach the component covers and one it does not.
    Returns findings, treated as gate failures."""
    def hit(kind: str, ok: bool, need_conf: bool = False) -> bool:
        return any(k[0] == kind and v[0] is ok and (v[1] or not need_conf)
                   for k, v in _MODEL_ROWS.items())

    witnesses = {
        "an admitted confidential placement": hit("place", True, True),
        "a refused off-device placement": hit("place", False, True),
        "an admitted model reach": hit("reach", True),
        "a refused model reach": hit("reach", False),
    }
    findings = [f"model coverage: NO witness of {k} — the row would agree "
                "vacuously" for k, ok in witnesses.items() if not ok]
    if not findings:
        print(f"model coverage: {len(_MODEL_ROWS)} placements and reach "
              "edges, each admitted and refused")
    return findings


#: What the REFERENCE read for each component's declaration rules: (steps,
#: intercept targets, provides, operations). Read by `prelude_coverage`.
_PRELUDE_ROWS: dict = {}


def prelude_coverage(ref) -> list[str]:
    """The non-vacuity ratchet for the `PL`, `IC` and `MS` rows (issue
    #1809). Each must be exercised on both sides: a prelude before an action
    admitted and one after refused; an intercept of a requirement admitted
    and one of a provision refused; a declared operation admitted and an
    undeclared one refused. Returns findings, treated as gate failures."""
    def find(rows: dict, want: str, test) -> object:
        return next((k for k in sorted(rows) if rows[k] == want
                     and test(_PRELUDE_ROWS.get(k))), None)

    has_steps = lambda x: x is not None and "prelude" in x[0] and "action" in x[0]  # noqa: E731
    has_targets = lambda x: x is not None and bool(x[1])  # noqa: E731
    has_calls = lambda x: x is not None and bool(x[3])  # noqa: E731
    witnesses = {
        "an admitted prelude before an action": find(ref.preludes, "ok", has_steps),
        "a refused prelude after an action": find(ref.preludes, "fail", has_steps),
        "an admitted intercept of a requirement": find(ref.intercepts, "ok", has_targets),
        "a refused intercept of a provision": find(ref.intercepts, "fail", has_targets),
        "an admitted declared operation": find(ref.methods, "ok", has_calls),
        "a refused undeclared operation": find(ref.methods, "fail", has_calls),
    }
    findings = [f"declaration-rule coverage: NO witness of {k} — the row would "
                "agree vacuously" for k, w in witnesses.items() if w is None]
    if not findings:
        print(f"declaration-rule coverage: {len(_PRELUDE_ROWS)} components, "
              "each rule admitted and refused at least once")
    return findings


#: What the REFERENCE decided for each A1 site: (admitted, kind, reaches).
#: Filled by `reference_from_tsv`, read by `async_coverage`.
_ASYNC_ROWS: dict = {}


def async_coverage() -> list[str]:
    """The non-vacuity ratchet for the `A1` row (issue #1808).

    For each rule the corpus must carry the admitted shape and the refused
    one: an awaited step that reaches async and one that does not, an
    unawaited step that reaches nothing and one that reaches async, a sync
    method that reaches nothing and one that reaches async, and a teardown
    slot that reaches nothing and one that suspends.

    Returns findings, treated as gate failures."""
    groups = {"awaited": ("effectAwait", "emitAwait"),
              "unawaited": ("effect", "emit"),
              "sync method": ("syncMethod",),
              "teardown": ("undo", "compensate")}
    findings: list[str] = []
    seen: dict[str, str] = {}
    for label, kinds in groups.items():
        for want in (True, False):
            hit = next((k for k, (ok, kind, _r) in sorted(_ASYNC_ROWS.items())
                        if kind in kinds and ok is want), None)
            name = f"{'an admitted' if want else 'a refused'} {label} site"
            if hit is None:
                findings.append(f"async coverage: NO witness of {name} — the "
                                "A1 row would agree vacuously")
            else:
                seen[name] = hit[0]
    if not findings:
        print(f"async coverage: {len(_ASYNC_ROWS)} sites, every rule admitted "
              "and refused at least once")
    return findings


#: What the REFERENCE decided for each component's access: (admitted, access
#: roots). Filled by `reference_from_tsv`, read by `access_coverage`.
_ACCESS_ROWS: dict = {}


def access_coverage() -> list[str]:
    """The non-vacuity ratchet for the `G1` row (issue #1807).

    A row that only ever saw requirement roots could not tell the rule from
    "every head is a requirement". The corpus must carry:

      * a component REFUSED for an undeclared access root
        (`examples/rejections/g1_undeclared_access.rvl`);
      * an ADMITTED component with an access root that is declared;
      * an ADMITTED component whose dropped roots include a local, a module
        callable and a host family, so each exclusion is exercised where the
        checker admits.

    Returns findings, treated as gate failures."""
    witnesses = {"refused": None, "declared": None, "excluded": None}
    for key, (ok, roots) in sorted(_ACCESS_ROWS.items()):
        if not ok:
            witnesses["refused"] = witnesses["refused"] or key
        if ok and roots:
            witnesses["declared"] = witnesses["declared"] or key
        if ok and {"local", "callable", "host"} <= _ACCESS_DROPPED.get(key, set()):
            witnesses["excluded"] = witnesses["excluded"] or key
    labels = {"refused": "a component refused for an undeclared access root",
              "declared": "an admitted component with a declared access root",
              "excluded": "an admitted component dropping a local, a callable "
                          "and a host family"}
    findings = [f"access coverage: NO witness of {labels[k]} — the G1 row "
                "would agree vacuously" for k, w in witnesses.items() if w is None]
    if not findings:
        print(f"access coverage: {len(_ACCESS_ROWS)} components; "
              + " ".join(f"{k}={w}" for k, w in witnesses.items()))
    return findings


#: What the REFERENCE decided for each binding scope: (admitted, binds,
#: opens a block). Filled by `reference_from_tsv`, read by `binding_coverage`.
_BINDING_ROWS: dict = {}


def binding_coverage() -> list[str]:
    """The non-vacuity ratchet for the `BU` row (issue #1812).

    A row that said `ok` over scopes that bind nothing, or never saw a block,
    would agree vacuously. The corpus must carry:

      * a scope REFUSED for a binding that reuses a name in view (the
        method of `examples/rejections/g6_method_local_shadows_component.rvl`);
      * an ADMITTED scope that binds a name and opens a block, so the block
        scoping is exercised on the admitted side.

    Returns findings, treated as gate failures."""
    witnesses = {"refused": None, "scoped": None}
    for key, (ok, binds, blocks) in sorted(_BINDING_ROWS.items()):
        if not ok:
            witnesses["refused"] = witnesses["refused"] or key
        if ok and binds and blocks:
            witnesses["scoped"] = witnesses["scoped"] or key
    labels = {"refused": "a scope refused for rebinding a name in view",
              "scoped": "an admitted scope that binds a name and opens a block"}
    findings = [f"binding coverage: NO witness of {labels[k]} — the BU row "
                "would agree vacuously" for k, w in witnesses.items() if w is None]
    if not findings:
        print(f"binding coverage: {len(_BINDING_ROWS)} scopes; "
              + " ".join(f"{k}={w}" for k, w in witnesses.items()))
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
    a component's activation body), `deferred` DF rows (G4 deferred position:
    a `deferred` emission is reached only by a call in a component),
    `approvals` AP rows (the G4 approval floor: every approval-required token
    a marked crossing reaches is covered by its `with` edge, issue #1455),
    `bindings` BU rows (G6 binding uniqueness: no binding reuses a name in
    view, issue #1812), `access` G1 rows (G1 declared access: every access
    root is a declared requirement, issue #1807), `async_sites` A1 rows and
    `async_sigs` A1S rows (A1 async colour, issue #1808), and `preludes`
    PL, `intercepts` IC and `methods` MS rows (prelude ordering, intercept
    target and method in service, issue #1809), `places` MPV and
    `model_reach` MAV rows (G-MODEL-PLACE, issue #1811)."""
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
    deferred: dict[str, str]
    approvals: dict[tuple[str, str, str], str]
    bindings: dict[tuple[str, str, str], str]
    access: dict[tuple[str, str], str]
    async_sites: dict[tuple[str, str, str], str]
    async_sigs: dict[tuple[str, str, str], str]
    preludes: dict[tuple[str, str], str]
    intercepts: dict[tuple[str, str], str]
    methods: dict[tuple[str, str], str]
    places: dict[tuple[str, str], str]
    model_reach: dict[tuple[str, str, str], str]

    def total(self) -> int:
        return (len(self.files) + len(self.comps) + len(self.providers)
                + len(self.spawns) + len(self.refused)
                + len(self.dispositions) + len(self.recoveries)
                + len(self.confinements) + len(self.g8surface)

                + len(self.g5reg) + len(self.a9) + len(self.configs)
                + len(self.a2) + len(self.deferred)
                + len(self.approvals) + len(self.bindings)
                + len(self.access) + len(self.async_sites)
                + len(self.async_sigs) + len(self.preludes)
                + len(self.intercepts) + len(self.methods)
                + len(self.places) + len(self.model_reach))



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
    deferred: dict[str, str] = {}
    approvals: dict[tuple[str, str, str], str] = {}
    bindings: dict[tuple[str, str, str], str] = {}
    access: dict[tuple[str, str], str] = {}
    async_sites: dict[tuple[str, str, str], str] = {}
    async_sigs: dict[tuple[str, str, str], str] = {}
    preludes: dict[tuple[str, str], str] = {}
    intercepts: dict[tuple[str, str], str] = {}
    methods: dict[tuple[str, str], str] = {}
    places: dict[tuple[str, str], str] = {}
    model_reach: dict[tuple[str, str, str], str] = {}

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
        elif parts[0] == "DF" and len(parts) == 3:
            # G4 deferred position (issue #1742): file -> ok|fail.
            deferred[parts[1]] = parts[2].split("=", 1)[1]
        elif parts[0] == "AP" and len(parts) == 5:
            # The approval floor: (file, comp, crossing ord) -> ok|fail.
            approvals[(parts[1], parts[2], parts[3])] = \
                parts[4].split("=", 1)[1]
        elif parts[0] == "A1" and len(parts) == 5:
            # A1 async colour: (file, comp, site ord) -> ok|fail.
            async_sites[(parts[1], parts[2], parts[3])] = \
                parts[4].split("=", 1)[1]
        elif parts[0] == "A1S" and len(parts) == 5:
            # A1 signature colour: (file, comp, key.method) -> ok|fail.
            async_sigs[(parts[1], parts[2], parts[3])] = \
                parts[4].split("=", 1)[1]
        elif parts[0] == "MPV" and len(parts) == 4:
            places[(parts[1], parts[2])] = parts[3].split("=", 1)[1]
        elif parts[0] == "MAV" and len(parts) == 5:
            model_reach[(parts[1], parts[2], parts[3])] = \
                parts[4].split("=", 1)[1]
        elif parts[0] in ("PL", "IC", "MS") and len(parts) == 4:
            # the three declaration rules: (file, comp) -> ok|fail.
            {"PL": preludes, "IC": intercepts, "MS": methods}[parts[0]][
                (parts[1], parts[2])] = parts[3].split("=", 1)[1]
        elif parts[0] == "G1" and len(parts) == 4:
            # G1 declared access: (file, comp) -> ok|fail.
            access[(parts[1], parts[2])] = parts[3].split("=", 1)[1]
        elif parts[0] == "BU" and len(parts) == 5:
            # G6 binding uniqueness: (file, comp, scope) -> ok|fail.
            bindings[(parts[1], parts[2], parts[3])] = \
                parts[4].split("=", 1)[1]
        else:
            raise SystemExit(f"differential oracle: malformed verdict row {line!r}")
    return Verdicts(files, comps, providers, spawns, refused, dispositions,
                    recoveries, confinements, g8surface, g5reg, a9, configs,
                    a2, deferred, approvals, bindings, access,
                    async_sites, async_sigs, preludes, intercepts, methods,
                    places, model_reach)



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
        # A row on the `@host` pseudo-service is a host emission (see
        # `_record`): it is an emission by construction, as `Oracle.g4OK`
        # reads it.
        raw = any(
            u[2] == compn
            and (u[3] == "emitnested"
                 or ((u[3] == "emit")
                     != (u[5] == HOST_SERVICE or (u[5], u[6]) in ems)))
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
    # MPV / MAV verdicts (G-MODEL-PLACE, issue #1811): the placed arms
    # against the confidentiality origins, and each consulted role's reach
    # within the component's held set, by the spawn rule's own halves.
    conf_by_file: dict[str, set[str]] = {}
    mp_by: dict[tuple[str, str], list[tuple[str, str]]] = {}
    reach_by: dict[tuple[str, str], set[str]] = {}
    for r in rows:
        if r and r[0] == "MO" and len(r) == 3:
            conf_by_file.setdefault(r[1], set()).add(r[2])
        elif r and r[0] == "MP" and len(r) == 7:
            mp_by.setdefault((r[1], r[2]), []).append((r[4], r[6]))
        elif r and r[0] == "MRC" and len(r) == 4:
            reach_by.setdefault((r[1], r[2]), set()).add(r[3])
    places: dict[tuple[str, str], str] = {}
    model_reach: dict[tuple[str, str, str], str] = {}
    _MODEL_ROWS.clear()
    for key, arms in mp_by.items():
        conf = conf_by_file.get(key[0], set())
        ok = all(o not in conf or res == "on_device" for o, res in arms)
        places[key] = "ok" if ok else "fail"
        _MODEL_ROWS[("place",) + key] = (ok, any(o in conf for o, _r in arms))
    for r in rows:
        if r and r[0] == "ME" and len(r) == 4:
            hset = held.get((r[1], r[2]), set())
            rset = reach_by.get((r[1], r[3]), set())
            resource, ceiling = attenuation_halves(hset, rset)
            model_reach[(r[1], r[2], r[3])] = "ok" if resource and ceiling else "fail"
            _MODEL_ROWS[("reach", r[1], r[2], r[3])] = (resource and ceiling, True)

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

    # DF rows (G4 deferred position, issue #1742), recomputed INDEPENDENTLY
    # from the DR rows with the checker's own rule (`lower.
    # _refuse_teardown_externs_in_fn_bodies`): a component walk records
    # value references only, so a call (bare or inside an arrow) is legal
    # there, and in a `fn` or `test` body every reach is refused. One verdict
    # per file the M rows name.
    drrows = [r for r in rows if r and r[0] == "DR" and len(r) == 6]
    _DEFERRED_FILES.clear()
    deferred: dict[str, str] = {}
    for rel in sorted({r[1] for r in mrows}):
        mine = [(r[2], r[5]) for r in drrows if r[1] == rel]
        bad = [(sc, pos) for sc, pos in mine
               if sc != "component" or pos == "value"]
        deferred[rel] = "fail" if bad else "ok"
        _DEFERRED_FILES[rel] = (not bad, frozenset(mine))

    # AP rows (the approval floor, issue #1455), recomputed from the AR/AX/AE
    # rows with the checker's own coverage test (`lower._approval_covers`):
    # every token the crossing reaches that the file requires approval for
    # must be covered by the crossing's edge. One verdict per crossing that
    # has an AX row.
    required_by_file: dict[str, set[str]] = {}
    for r in rows:
        if r and r[0] == "AR" and len(r) == 3:
            required_by_file.setdefault(r[1], set()).add(r[2])
    crossing_tokens: dict[tuple[str, str, str], set[str]] = {}
    crossing_edge: dict[tuple[str, str, str], str] = {}
    for r in rows:
        if r and r[0] == "AX" and len(r) == 5:
            crossing_tokens.setdefault((r[1], r[2], r[3]), set()).add(r[4])
        elif r and r[0] == "AE" and len(r) == 5:
            crossing_edge[(r[1], r[2], r[3])] = r[4]
    _APPROVAL_ROWS.clear()
    approvals: dict[tuple[str, str, str], str] = {}
    bindings: dict[tuple[str, str, str], str] = {}
    access: dict[tuple[str, str], str] = {}
    async_sites: dict[tuple[str, str, str], str] = {}
    async_sigs: dict[tuple[str, str, str], str] = {}
    preludes: dict[tuple[str, str], str] = {}
    intercepts: dict[tuple[str, str], str] = {}
    methods: dict[tuple[str, str], str] = {}
    for key, tokens in crossing_tokens.items():
        needed = tokens & required_by_file.get(key[0], set())
        edge = crossing_edge.get(key)
        ok = all(edge is not None and _approval_covers(edge, t) for t in needed)
        approvals[key] = "ok" if ok else "fail"
        _APPROVAL_ROWS[key] = (ok, edge is not None, bool(needed))

    # BU verdicts (G6 binding uniqueness, issue #1812), recomputed from the BE
    # rows: one verdict per scope.
    scope_steps: dict[tuple[str, str, str], list[tuple[int, str, str]]] = {}
    for r in rows:
        if r and r[0] == "BE" and len(r) == 7:
            scope_steps.setdefault((r[1], r[2], r[3]), []).append(
                (int(r[4]), r[5], r[6]))
    _BINDING_ROWS.clear()
    for key, steps in scope_steps.items():
        steps.sort()
        seed = [n for _o, k, n in steps if k == "seed"]
        events = [(k, n) for _o, k, n in steps if k != "seed"]
        ok = binding_verdict(seed, events)
        bindings[key] = "ok" if ok else "fail"
        _BINDING_ROWS[key] = (ok, sum(1 for k, _n in events if k == "bind"),
                              any(k == "enter" for k, _n in events))

    # G1 verdicts (declared access, issue #1807), recomputed from the M and
    # GA rows: every access root of a component is one of its requires.
    access_roots: dict[tuple[str, str], set[str]] = {}
    for r in rows:
        if r and r[0] == "GA" and len(r) == 4:
            access_roots.setdefault((r[1], r[2]), set()).add(r[3])
    _ACCESS_ROWS.clear()
    for r in mrows:
        key = (r[1], r[2])
        declared = {x for x in r[3].split(",") if x}
        roots = access_roots.get(key, set())
        ok = roots <= declared
        access[key] = "ok" if ok else "fail"
        _ACCESS_ROWS[key] = (ok, len(roots))

    # A1 verdicts (async colour, issue #1808), recomputed from the AN, FN and
    # AS rows with a true fixed point, and the AG signature rows.
    anames: dict[str, set[str]] = {}
    for r in rows:
        if r and r[0] == "AN" and len(r) == 3:
            anames.setdefault(r[1], set()).add(r[2])
    reach_by_file: dict[str, set[str]] = {}
    _ASYNC_ROWS.clear()
    for r in rows:
        if r and r[0] == "AS" and len(r) == 6:
            rel = r[1]
            if rel not in reach_by_file:
                reach_by_file[rel] = async_reaching(
                    anames.get(rel, set()), fns_by_file.get(rel, {}))
            heads = [h for h in r[5].split(",") if h]
            reaches = any(h in reach_by_file[rel] for h in heads)
            ok = async_site_ok(r[4], reaches)
            async_sites[(r[1], r[2], r[3])] = "ok" if ok else "fail"
            _ASYNC_ROWS[(r[1], r[2], r[3])] = (ok, r[4], reaches)
        elif r and r[0] == "AG" and len(r) == 6:
            async_sigs[(r[1], r[2], r[3])] = "ok" if r[4] == r[5] else "fail"

    # PL / IC / MS verdicts (issue #1809), recomputed from the PS, IT, MC, M
    # and B rows.
    steps_by: dict[tuple[str, str], list[tuple[int, str]]] = {}
    targets_by: dict[tuple[str, str], set[str]] = {}
    calls_by: dict[tuple[str, str], set[tuple[str, str]]] = {}
    for r in rows:
        if r and r[0] == "PS" and len(r) == 5:
            steps_by.setdefault((r[1], r[2]), []).append((int(r[3]), r[4]))
        elif r and r[0] == "IT" and len(r) == 4:
            targets_by.setdefault((r[1], r[2]), set()).add(r[3])
        elif r and r[0] == "MC" and len(r) == 5:
            calls_by.setdefault((r[1], r[2]), set()).add((r[3], r[4]))
    table_by: dict[str, set[tuple[str, str]]] = {}
    for r in brows:
        table_by.setdefault(r[1], set()).add((r[2], r[3]))
    _PRELUDE_ROWS.clear()
    for r in mrows:
        key = (r[1], r[2])
        requires = {x for x in r[3].split(",") if x}
        provides = {x for x in r[4].split(",") if x}
        steps = [k for _o, k in sorted(steps_by.get(key, []))]
        preludes[key] = "ok" if prelude_ok(steps) else "fail"
        bad_targets = {t for t in targets_by.get(key, set())
                       if t in provides and t not in requires}
        intercepts[key] = "fail" if bad_targets else "ok"
        unknown = calls_by.get(key, set()) - table_by.get(r[1], set())
        methods[key] = "fail" if unknown else "ok"
        _PRELUDE_ROWS[key] = (steps, targets_by.get(key, set()), provides,
                              calls_by.get(key, set()))

    return Verdicts(files, comps, providers, spawns, refused, dispositions,

                    recoveries, confinements, g8surface, g5reg, a9,
                    configs, a2, deferred, approvals, bindings, access,
                    async_sites, async_sigs, preludes, intercepts, methods,
                    places, model_reach)


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
FATAL_BUCKETS = ("missed-G1", "missed-G4", "missed-G2", "missed-G5",
                 "missed-G6", "missed-A1", "missed-A6", "missed-A9",
                 "missed-A2", "missed-prelude", "missed-intercept",
                 "missed-G-MODEL-PLACE",
                 "formal-strict", "formal-found-other")


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
    fn or extern. An `undo store.drop()` (a host receiver) and an `undo
    f(key)` through a function-typed parameter leave the `Prog` at the first
    hop: the fold has no declaration to follow and counts nothing. (A
    dispatched `let`-bound arrow, a passed emitting fn and a service
    operation read off a spawn handle no longer do: `inverse_reach_heads`
    adds what they reach to the inverse heads, issue #1792.) A zero there is the model having no fact, not the
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
    # kind rather than a case in an existing rule. The APPROVAL FLOOR — a
    # marked crossing of an approval-required capability token carries a
    # covering `with` edge (item 246) — is `RevL.G4Approval.crossingB` over the
    # `AR`/`AX`/`AE` facts, one `AP` row per crossing (issue #1455). It was a
    # ratcheted `out-of-fragment-approval` bucket until then.
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
        # The AP row is the approval floor (issue #1455), the fourth rule the
        # checker reports under G4 (category `approval`), so it joins the
        # fold the same way.
        ap_rows = [(k, x) for k, x in v.approvals.items() if k[0] == rel]
        # The BU row is G6 binding uniqueness (issue #1812): checker-visible
        # in both directions, like A2.
        bu_fail = any(x == "fail" for k, x in v.bindings.items() if k[0] == rel)
        # The G1 row is declared access (issue #1807), checker-visible both ways.
        g1_fail = any(x == "fail" for k, x in v.access.items() if k[0] == rel)
        # The A1 rows are async colour (issue #1808): checker-visible both ways.
        pl_fail = any(x == "fail" for k, x in v.preludes.items() if k[0] == rel)
        mp_fail = any(x == "fail" for k, x in v.places.items() if k[0] == rel)
        ma_fail = any(x == "fail" for k, x in v.model_reach.items() if k[0] == rel)
        ic_fail = any(x == "fail" for k, x in v.intercepts.items() if k[0] == rel)
        ms_fail = any(x == "fail" for k, x in v.methods.items() if k[0] == rel)
        a1_fail = any(x == "fail" for k, x in v.async_sites.items()
                      if k[0] == rel) or any(
            x == "fail" for k, x in v.async_sigs.items() if k[0] == rel)
        g4_rows = comp_rows + prov_rows + spawn_rows + cfg_rows + ap_rows
        # The A2 row (issue 1166) is checker-visible: `lower._dispatch_action`
        # refuses the shape with code A2, so a model `fail` on an accepted
        # file is `formal-strict` and a checker A2 with the row `ok` is the
        # fatal `missed-A2`.
        a2_rows = [(k, x) for k, x in v.a2.items() if k[0] == rel]
        vrow = v.files.get(rel, ("ok", "ok", "ok"))
        # The DF row is the fourth rule under the G4 guarantee (issue #1742):
        # a DF failure clears a G4 `deferred` refusal, and a DF failure over
        # a file the checker accepts is `formal-strict` like any other.
        df_fail = v.deferred.get(rel, "ok") == "fail"
        formal_clean = vrow[0] == "ok" and vrow[2] == "ok" and all(
            x == "ok" for _, x in g4_rows + a9_rows + a2_rows) \
            and not df_fail and not bu_fail and not g1_fail and not a1_fail \
            and not pl_fail and not ic_fail and not ms_fail \
            and not mp_fail and not ma_fail
        a2_found = any(x == "fail" for _, x in a2_rows)
        raw_found = any(x == "fail" for _, x in g4_rows)

        code, category = checker_code(rel)
        if code == "accept":
            # `formal-strict`: the checker ACCEPTS the file but the shaped
            # model does not — the model is stricter than the fragment it
            # covers, which is a finding to chase, not a licence to relax it.
            record("agree-accept" if formal_clean else "formal-strict", rel)
        elif code == "G4" and category == "deferred":
            # The deferred-position rule (item 400, issue #1457, `lower.
            # _deferred_value_refusal` and its call arms) is the model's
            # `RevL.G4Deferred` over the `DR` position facts, decided as the
            # `DF` row (issue #1742). It asks WHERE a `deferred` emission
            # extern is reached, not whether a crossing is marked, so it is
            # the DF row that must see it: a checker refusal the row does not
            # fail is the model being weaker than revl, and fatal.
            record("agree-G4" if df_fail else "missed-G4", rel)
        elif code == "G4" and category == "inverse":
            # The host release rule (issue #1859, `lower._check_host_release`)
            # carries the G4 code, but it is not the marker rule the `G` row
            # states: it asks whether a host bracket's `undo` is the family's
            # release on the bound handle, and the model's HA row carries no
            # inverse fact yet (issue #1859's formal slice adds the `inv`
            # column). Absence of fact, ratcheted by name as the approval
            # floor is, until that column lands.
            record("out-of-fragment-inverse" if formal_clean
                   else "formal-found-other", rel)
        elif code == "G4" and category == "witnessed":
            # A witnessed extern called with a site `undo` (issue #1963,
            # `lower._lower_effect_step`) carries the G4 code, but it is not
            # the marker rule either: it asks whether the call site spells an
            # inverse the witnessed extern already declares, and the model has
            # no witnessed-extern fact. Absence of fact, ratcheted by name as
            # the host release rule is, until the model grows the fact.
            record("out-of-fragment-witnessed" if formal_clean
                   else "formal-found-other", rel)
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
                # (a host receiver, or a call through a function-typed
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
        elif code == "A1" and A1_ARROW_MESSAGE not in checker_message(rel):
            # Async colour (issue #1808): the `A1` rows decide
            # `RevL.A1Async` per site, so an A1 refusal the rows admit is the
            # model being weaker, and fatal. The arrow-type refusal is not
            # this rule (the model has no arrow types) and falls through.
            record("agree-A1" if a1_fail else "missed-A1", rel)
        elif code == "REVL" and A1_SIGNATURE_MESSAGE in checker_message(rel):
            # The uncoded signature-colour refusal, decided by the `A1S` row.
            record("agree-A1" if a1_fail else "missed-A1", rel)
        elif code == "G-MODEL-PLACE" and category == "model-placement" \
                and MODEL_PLACE_MESSAGE in checker_message(rel):
            # Model placement (issue #1811): a confidentiality origin placed
            # off the device, decided by the `MPV` row.
            record("agree-G-MODEL-PLACE" if mp_fail else "missed-G-MODEL-PLACE",
                   rel)
        elif code == "G-MODEL-PLACE" and category == "capability-attenuation":
            # Model reach (issue #1811, item 519), decided by the `MAV` row.
            record("agree-G-MODEL-PLACE" if ma_fail else "missed-G-MODEL-PLACE",
                   rel)
        elif code == "A6" and METHOD_MESSAGE in checker_message(rel):
            # Method in service (issue #1809), the call-site half of A6.
            record("agree-A6" if ms_fail else "missed-A6", rel)
        elif code == "REVL" and PRELUDE_MESSAGE in checker_message(rel):
            # Prelude ordering (issue #1809), an uncoded refusal.
            record("agree-prelude" if pl_fail else "missed-prelude", rel)
        elif code == "REVL" and INTERCEPT_MESSAGE in checker_message(rel):
            # Intercept target (issue #1809), an uncoded refusal.
            record("agree-intercept" if ic_fail else "missed-intercept", rel)
        elif code == "G1":
            # Declared access (issue #1807): the `G1` row decides
            # `RevL.G1Access` over the component's access roots, so a G1
            # refusal the row admits is the model being weaker, and fatal.
            record("agree-G1" if g1_fail else "missed-G1", rel)
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
            # rule about purity outside an effect form, so a G6 PURITY refusal
            # is honestly outside its fragment.
            #
            # The duplicate-binding refusal (category `binding`) IS modelled
            # since issue #1812: the `BU` row decides `RevL.G6Binding` over
            # each scope's bindings, so a binding refusal the row admits is
            # the model being weaker than the checker, and fatal.
            if category == "binding":
                record("agree-G6" if bu_fail else "missed-G6", rel)
            else:
                record("out-of-fragment-G6" if formal_clean
                       else "formal-found-other", rel)
        elif out_of_scope(code, checker_message(rel)):
            # Out of scope BY KIND (issue #1810): a type-checker refusal, or
            # name resolution of declarations and of the lifecycle test DSL.
            # Modelling them buys no guarantee, so they are not holes and are
            # not ratcheted; keeping them apart is what makes
            # `out-of-fragment` mean unbuilt work.
            record("out-of-scope" if formal_clean else "formal-found-other",
                   rel)
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
    for k in sorted(set(align) | set(FATAL_BUCKETS) | set(OOF_RATCHET_BUCKETS)):
        n = align.get(k, 0)
        mark = "  FATAL" if k in FATAL_BUCKETS and n else ""
        print(f"  {k:20} {n}{mark}")
    for k in (*FATAL_BUCKETS, *OOF_RATCHET_BUCKETS):
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
#     G6 purity fixture, can no longer arrive while the model stays silent:
#     somebody has to model it, or write its name down and own the hole.
#   * a ledger entry that is NO LONGER in its bucket fails the gate too and
#     must be DELETED. So the list shrinks only, and a file cannot be parked
#     in it once the model does have a fact about it.
#
# The inputs that make it fail, named: dropping a new
# `examples/rejections/g5_undo_*.rvl` whose `undo` calls a function-typed
# parameter into the corpus reds the gate with `joined out-of-fragment-G5`;
# teaching the exporter to follow a handle's method reference, a dispatched
# arrow and a passed fn (issue #1792) red it with `left out-of-fragment-G5`
# on each of the ten files it newly resolved, until their lines went. Deleting the ledger reds it as well — a missing ratchet
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
# one collects every checker code the model states no row about at all (A1,
# G7, T1, REVL, HOST-METHOD, ...) and grows with any new type-error fixture
# anywhere in revl, so a ratchet there would red the formal gate on work
# that never touched the formal layer. G5 and G6 are different in kind: the
# model carries a row aimed at each of them — the `U5` registration fold and
# the `C` confinement surface — so "no fact about this file" is a claim
# about a specific row that exists, and that is the claim worth pinning.
OOF_LEDGER_PATH = FORMAL / "out_of_fragment_ledger.json"
OOF_RATCHET_BUCKETS = ("out-of-fragment-G5", "out-of-fragment-G6",
                       "out-of-fragment-inverse", "out-of-fragment-witnessed")
OOF_LEDGER_ABOUT = [
    "The corpus files the checker refuses G5, G6 or with the G4 host",
    "release rule, and the model has NO fact about: `out-of-fragment-G5`,",
    "`out-of-fragment-G6` and `out-of-fragment-inverse` in",
    "`formal/harness/diff_corpus.py`'s checker-alignment buckets.",
    "(The G4 deferred-position rule left this ledger in issue #1742: the",
    "model states it as `RevL.G4Deferred`, decided as the `DF` row. The",
    "G4 approval floor left it in issue #1455: `RevL.G4Approval`, decided",
    "as the `AP` row. The G5 list emptied in issue #1792, when the exporter",
    "began resolving an inverse's indirections, and the G6 list in issue",
    "#1812, when binding uniqueness became `RevL.G6Binding` (the `BU`",
    "row). Both buckets stay, so a new unresolvable `undo` or a new G6",
    "purity refusal still reds the gate.)",
    "`out-of-fragment-witnessed` (issue #1963) holds the files the checker",
    "refuses for a witnessed extern called with a site `undo`: the model",
    "has no witnessed-extern fact yet.",
    "",
    "Each bucket records an absence, so none can disagree with anything",
    "and none could fail the gate on its own (issue #1169). This ledger",
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

    It names every file in a bucket that fails or ratchets the gate, and it
    carries NO count that moves with the corpus (issue #1768). The census
    totals and the per-bucket file counts are printed by every gate run, and
    they used to be rendered here too, so every pull request that added a
    `.rvl` anywhere in the census directories rewrote the same lines of this
    block and conflicted with every other one, and resolving that took a real
    `lake build`. A count that is a function of the corpus is checked by the
    gate that computes it; storing it here only added a line every pull
    request rewrote. What stays is what a reader cannot get from the run's
    summary at a glance and what only moves when the model's relation to a
    named file moves: the bucket names, their gate class, and the named
    members. The FATAL rows keep their count because it is zero on every run
    that passes the gate, so it never churns, and a non-zero one is a red gate
    with its files named below. The ratcheted rows keep theirs because it is
    the size of a membership ledger that only shrinks: it moves only in a
    diff that edits `formal/out_of_fragment_ledger.json` too.

    `census`, `file_facts`, `componentless`, `refusals`, `ref` and
    `mismatches` are still accepted, because the gate's own printout renders
    them; this block deliberately does not."""
    del census, file_facts, componentless, refusals, ref, mismatches

    def para(text: str) -> str:
        # The document is hand-wrapped at 72; a generated block that is not
        # would show up as a wall of diff noise. `break_on_hyphens` off, or
        # `out-of-fragment*` splits mid-token and markdown stops reading the
        # code span.
        return textwrap.fill(" ".join(text.split()), width=72,
                             break_on_hyphens=False, break_long_words=False)

    lines = [
        STATUS_BEGIN,
        "",
        para(
            "The census totals (files, components, statements, verdicts "
            "compared and agreeing) and the file count of every informational "
            "bucket are printed by every gate run, `make formal` and `python3 "
            "formal/harness/diff_corpus.py`, and are not stored here: a count "
            "that moves with the corpus made every pull request that added a "
            "`.rvl` rewrite this block (issue #1768). The block changes only "
            "when a named file joins or leaves a bucket below."),
        "",
        para(
            "Checker alignment over the modeled files. "
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
            "the `Prog` cannot resolve, or a new G6 purity fixture, cannot arrive "
            "while the model stays silent about it. `agree-*` and the "
            "generic `out-of-fragment` stay informational; that one collects "
            "every refusal under a rule the model states no row about, so it "
            "is the list of unbuilt work. `out-of-scope` is informational "
            "too and is not a hole: a type-checker refusal (T1, T2, T3) or "
            "name resolution of declarations and of the lifecycle test DSL, "
            "routed by an explicit rule (`out_of_scope`), so it grows with "
            "corpus work that never touched this layer."),
        "",
        "| bucket | files | gate |",
        "| --- | --- | --- |",
    ]
    for k in sorted(set(align) | set(FATAL_BUCKETS) | set(OOF_RATCHET_BUCKETS)):
        if k in FATAL_BUCKETS:
            lines.append(f"| `{k}` | {align.get(k, 0)} | **FATAL** |")
        elif k in OOF_RATCHET_BUCKETS:
            lines.append(f"| `{k}` | {align.get(k, 0)} | ratcheted |")
        else:
            lines.append(f"| `{k}` | printed by the gate | informational |")
    lines.append("")
    # Every ratcheted bucket gets its own list, written even when it is empty
    # (`- none`), so a file joining one bucket edits only that bucket's lines:
    # two pull requests filling two different buckets touch lines that a
    # stable heading separates, and git merges them (issue #1768's property,
    # which an empty ledger list would otherwise lose: both sides would add
    # the same opening lines). A FATAL bucket is listed only when it is
    # non-empty, which is a red gate in any case.
    lines.append(para("Nothing is counted without being named; the files "
                      "in the non-`agree` buckets are:"))
    lines.append("")
    for k in OOF_RATCHET_BUCKETS:
        lines.append(f"`{k}`:")
        lines.append("")
        members = sorted(_ALIGN_SAMPLES.get(k, []))
        lines.extend(f"- `{rel}`" for rel in members)
        if not members:
            lines.append("- none")
        lines.append("")
    for k in FATAL_BUCKETS:
        for rel in sorted(_ALIGN_SAMPLES.get(k, [])):
            lines.append(f"- `{k}`: `{rel}`")
    if any(_ALIGN_SAMPLES.get(k) for k in FATAL_BUCKETS):
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
            ("a2", ref.a2, formal.a2),
            ("deferred", ref.deferred, formal.deferred),
            ("approval", ref.approvals, formal.approvals),
            ("binding", ref.bindings, formal.bindings),
            ("access", ref.access, formal.access),
            ("async_site", ref.async_sites, formal.async_sites),
            ("async_sig", ref.async_sigs, formal.async_sigs),
            ("prelude", ref.preludes, formal.preludes),
            ("intercept", ref.intercepts, formal.intercepts),
            ("method", ref.methods, formal.methods),
            ("model_place", ref.places, formal.places),
            ("model_reach", ref.model_reach, formal.model_reach)):

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
        f"{len(ref.a2)} a2 bodies + "
        f"{len(ref.deferred)} deferred-position files + "
        f"{len(ref.approvals)} approval crossings + "
        f"{len(ref.bindings)} binding scopes + "
        f"{len(ref.access)} access components + "
        f"{len(ref.async_sites)} async sites + "
        f"{len(ref.async_sigs)} async signatures + "
        f"{len(ref.preludes)} x 3 declaration-rule components + "
        f"{len(ref.places)} model placements + "
        f"{len(ref.model_reach)} model reach edges) — "

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
    mismatches.extend(deferred_coverage())
    mismatches.extend(approval_coverage())
    mismatches.extend(binding_coverage())
    mismatches.extend(access_coverage())
    mismatches.extend(async_coverage())
    mismatches.extend(prelude_coverage(ref))
    mismatches.extend(model_coverage())

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


def census_json() -> int:
    """`--census-json`: the oracle census, as one JSON object on stdout.

    This is the source `revl.cert` reads for the component certificate's
    oracle requirement (issue #1768). The census used to be read back out of
    the counts this harness rendered into `formal/STATUS.md`; the block no
    longer stores counts that move with the corpus, so the certificate asks
    the run that computes them. Reference side only, like `--write-status`:
    `verdicts_compared` is every verdict the differential compares, and the
    agreement half is the gate's (`make formal` fails on any mismatch)."""
    import contextlib  # noqa: PLC0415
    import io  # noqa: PLC0415
    import warnings  # noqa: PLC0415

    with warnings.catch_warnings(), contextlib.redirect_stdout(io.StringIO()):
        warnings.simplefilter("ignore")
        tsv, _facts, census = export()
        if not tsv:
            print("nothing extracted: no oracle census", file=sys.stderr)
            return 1
        ref = reference_from_tsv(tsv)
    json.dump({"files": census["files"], "components": census["components"],
               "statements": census["statements"],
               "verdicts_compared": ref.total()}, sys.stdout, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    _argv = sys.argv[1:]
    if "--census-json" in _argv:
        sys.exit(census_json())
    if "--write-ledger" in _argv:
        sys.exit(write_status(ledger=True))
    sys.exit(write_status() if "--write-status" in _argv else main())
