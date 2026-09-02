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
import shutil
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

REPO = Path(__file__).resolve().parents[2]
FORMAL = Path(__file__).resolve().parents[1]
CORPUS_DIRS = ("examples", "tck", "tests")

sys.path.insert(0, str(REPO / "src"))

from revl import cap_order
from revl.compiler import compile_source
from revl.diagnostics import classify
from revl.errors import RevlError
from revl.parser import (
    EffectStmt,
    EmitExpr,
    EmitStmt,
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


def attenuates(held: "set[str]", reach: "set[str]") -> bool:
    """`RevL.CapCeilings.Attenuates`, computed with the checker's own
    algebra: the resource fold over ceiling-stripped capabilities
    (`covers_set` empty), AND the ceiling budget check — wherever the
    parent declares a budget for the child's token and parameter, the child
    must declare one too and no larger (a dropped ceiling is `+inf`, hence
    a widening)."""
    hcaps = [parse_cap(h) for h in held]
    rcaps = [parse_cap(c) for c in reach]
    hsplit = [cap_order.split_ceilings(h) for h in hcaps]
    rsplit = [cap_order.split_ceilings(c) for c in rcaps]
    if cap_order.covers_set([h for h, _ in hsplit], [c for c, _ in rsplit]):
        return False
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
                return False
    return True


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


def _resolve_emission(root: str, chain: str, requires: dict, handles: dict,
                      psvc: dict) -> tuple[str, str] | None:
    """(svc, method) a call reaches, or None when not a boundary-typed
    receiver: a require binding (single-hop chain) or a spawn handle
    (`w.task.run` => child's provide key `task` -> service, method `run`)."""
    if root in requires:
        return requires[root], chain
    if root in handles and "." in chain:
        head, _, rest = chain.partition(".")
        if not rest:
            return None
        svc = psvc.get(handles[root], {}).get(head)
        return (svc, rest) if svc else None
    return None


def walk_reach(node, out: set[str], region: str, requires: dict, handles: dict,
               psvc: dict, bounds: dict, em_set: set, emitting: set) -> None:
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
                           handles, psvc, bounds, em_set, emitting)
        return
    if isinstance(node, ExprCall):
        rt = _route(node.callee)
        if rt:
            root, chain = rt
            res = _resolve_emission(root, chain, requires, handles, psvc)
            if res is not None and region == "all":
                svc, meth = res
                if (svc, meth) in em_set:
                    if root in handles:
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
                       em_set, emitting)
        return
    if dataclasses.is_dataclass(node) and not isinstance(node, type):
        for f in dataclasses.fields(node):
            walk_reach(getattr(node, f.name), out, region, requires, handles,
                       psvc, bounds, em_set, emitting)
        return
    if isinstance(node, (list, tuple)):
        for x in node:
            walk_reach(x, out, region, requires, handles, psvc, bounds,
                       em_set, emitting)


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
        # provide-key -> service, file-wide (children resolve handle receivers).
        psvc = {c.name: {k: s for k, s, _ln in c.provides} for c in prog.components}

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

            # activation emit-step surface (A): the component's OWN marked
            # crossings — the base of the attenuation reach.
            act_reach: set[str] = set()
            for stmt in c.body:
                walk_reach(stmt, act_reach, "emit-step", require_map, handles,
                           psvc, bounds, em_set, emitting)
            caps_seen.update(act_reach)
            for cap in sorted(act_reach):
                tsv.append("\t".join(["A", rel, c.name, cap]))

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
                                       psvc, bounds, em_set, emitting)
                        caps_seen.update(reach)
                        for cap in sorted(reach):
                            tsv.append("\t".join(
                                ["F", rel, c.name, stmt.key, svc, pm.name, cap]))

            calls: list[tuple[str, str, str, str]] = []
            kinds: list[str] = []

            def classify_stmt(stmt: object) -> bool:
                """Classify one statement; True when it holds a G4-shaped
                violation: marker-presence != interface-declared emission."""
                nonlocal stmts
                stmts += 1
                if isinstance(stmt, (EffectStmt, LetEffect)):
                    kinds.append("effect")
                    # effect-form calls are STILL plain context: an emission
                    # call whose pairing is an inverse is refused (the
                    # g4_unmarked_emission fixture) — only `emit` marks a
                    # crossing. So walk them into the record, not a discard.
                    local_calls: list[tuple[str, str, str]] = []
                    walk_calls(stmt, local_calls, "plain")
                    return _record(local_calls)
                if isinstance(stmt, EmitStmt):
                    kinds.append("emit")
                    local_calls = []
                    walk_calls(stmt, local_calls, "emit")
                    return _record(local_calls)
                local_calls: list[tuple[str, str, str]] = []
                if type(stmt).__name__ == "CallStmt":
                    for a in getattr(stmt, "args", []):
                        walk_calls(a, local_calls, "plain")
                    root, meth = getattr(stmt, "key"), getattr(stmt, "method")
                    local_calls.append((root, meth, "plain"))
                else:
                    walk_calls(stmt, local_calls, "plain")
                return _record(local_calls)

            def _record(local_calls: list[tuple[str, str, str]]) -> bool:
                saw_raw = False
                for root, chain, ctx in local_calls:
                    res = _resolve_emission(root, chain, require_map, handles,
                                            psvc)
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
            ff["components"][c.name] = {"calls": calls, "kinds": kinds}
        file_facts[rel] = ff
    # Z/Y decomposition rows go FIRST so the oracle can build its table in
    # one pass; the harness refuses a capability the checker cannot re-read.
    return cap_decomposition_rows(caps_seen) + tsv, file_facts, {
        "files": files, "components": comps, "statements": stmts,
        "refusals": refusals, "componentless": componentless,
    }


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
    `comps` G rows, `providers` P rows, `spawns` W rows, `refused` X rows."""
    files: dict[str, tuple[str, str, str]]
    comps: dict[tuple[str, str], str]
    providers: dict[tuple[str, str, str, str, str], str]
    spawns: dict[tuple[str, str, str], str]
    refused: dict[str, str]

    def total(self) -> int:
        return (len(self.files) + len(self.comps) + len(self.providers)
                + len(self.spawns) + len(self.refused))


def parse_verdicts(text: str) -> Verdicts:
    """Parse oracle output into verdict maps."""
    files: dict[str, tuple[str, str, str]] = {}
    comps: dict[tuple[str, str], str] = {}
    providers: dict[tuple[str, str, str, str, str], str] = {}
    spawns: dict[tuple[str, str, str], str] = {}
    refused: dict[str, str] = {}
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
        else:
            raise SystemExit(f"differential oracle: malformed verdict row {line!r}")
    return Verdicts(files, comps, providers, spawns, refused)


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
        comps[(rel, compn)] = "fail" if raw else "ok"

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
    for r in srows:
        rel, parent, child = r[1], r[2], r[3]
        ok = attenuates(held.get((rel, parent), set()),
                        closed.get((rel, child), set()))
        spawns[(rel, parent, child)] = "ok" if ok else "fail"

    refused = {r[1]: r[2] for r in xrows}
    return Verdicts(files, comps, providers, spawns, refused)


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

    def checker_code(rel: str) -> str:
        try:
            compile_source((REPO / rel).read_text(encoding="utf-8"), rel)
            return "accept"
        except RevlError as e:
            return classify(e).get("code") or "UNCODED"

    for rel in file_facts:
        comp_rows = [(k, x) for k, x in v.comps.items() if k[0] == rel]
        prov_rows = [(k, x) for k, x in v.providers.items() if k[0] == rel]
        spawn_rows = [(k, x) for k, x in v.spawns.items() if k[0] == rel]
        vrow = v.files.get(rel, ("ok", "ok", "ok"))
        formal_clean = vrow[0] == "ok" and vrow[2] == "ok" and all(
            x == "ok" for _, x in comp_rows + prov_rows + spawn_rows)
        raw_found = any(x == "fail"
                        for _, x in comp_rows + prov_rows + spawn_rows)
        code = checker_code(rel)
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
        nm_codes.setdefault(checker_code(rel), []).append(rel)

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
            ("refusal", ref.refused, formal.refused)):
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
        f"{len(ref.refused)} parse refusals) — "
        f"{compared - len(mismatches)} agree, {len(mismatches)} mismatch(es)"
    )
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

