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
3. compute reference verdicts in Python set logic (the G2/G3/G4-shaped
   spec) AND run the Lean oracle (`formal/harness/Oracle.lean`, the
   machine-checked models, coded independently) over the same TSV. The
   formal-vs-reference diff is the HARD gate: a mismatch is definitional
   drift between model and spec/extraction, and it fails `make formal`.
4. report checker alignment: compile each file with the real checker
   (`revl.compiler.compile_source`) and compare its refusal codes against
   the formal verdicts. Informational in this version (STATUS.md):
   mismatches here are findings, not gate failures.

Parse failures skip LOUDLY (counted, never silently dropped).
"""

import dataclasses
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
FORMAL = Path(__file__).resolve().parents[1]
CORPUS_DIRS = ("examples", "tck", "tests")

sys.path.insert(0, str(REPO / "src"))

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
# Canonical capability grammar (mirrors cap_order.py's (T, P) fold as a
# string): `token` or `token(n1=v1,n2=v2)`; a value is `"string"`, `/a/b`
# path components, or an int (a ceiling). Covers = same token (unless `*`),
# per-param value <= (path: component-prefix; int: <=; discrete: equality) —
# the same clauses cap_order.Cap.covers implements.

def _split_canon_cap(s: str) -> tuple[str, dict[str, tuple[str, object]]]:
    oi = s.find("(")
    if oi < 0:
        return s, {}
    tok = s[:oi]
    inner = s[oi + 1:]
    if inner.endswith(")"):
        inner = inner[:-1]
    params: dict[str, tuple[str, object]] = {}
    for chunk in inner.split(","):
        if not chunk.strip():
            continue
        name, _, raw = chunk.partition("=")
        raw = raw.strip()
        if raw.startswith('"'):
            params[name.strip()] = ("str", raw[1:-1])
        elif raw.startswith("/"):
            params[name.strip()] = ("path", tuple(raw.split("/")[1:]))
        else:
            params[name.strip()] = ("int", int(raw))
    return tok, params


def _param_leq(narrow: tuple[str, object], wide: tuple[str, object]) -> bool:
    (kn, vn), (kw, vw) = narrow, wide
    if kn != kw:
        return False
    if kn == "path":
        return vn[: len(vw)] == vw
    if kn == "int":
        return vn <= vw
    return vn == vw


def cap_covers(held: str, reach: str) -> bool:
    """`held` covers `reach` iff same token (unless `*`) and reach narrows
    every param held binds — cap_order.covers clause-for-clause, on the
    canonical string form."""
    th, ph = _split_canon_cap(held)
    tr, pr = _split_canon_cap(reach)
    if th == "*":
        return tr == "*"
    if tr == "*":
        return False
    if th != tr:
        return False
    for k, av in ph.items():
        if k not in pr or not _param_leq(pr[k], av):
            return False
    return True


def cap_covers_set(held: set[str], reach: set[str]) -> list[str]:
    """Reach elements NOT covered by any held element — the attenuation
    check's `extra`. Empty means admitted."""
    return sorted(c for c in reach if not any(cap_covers(h, c) for h in held))


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


def export() -> tuple[list[str], dict[str, dict], dict[str, int]]:
    """Parse the corpus; return (tsv rows, per-file facts, census)."""
    tsv: list[str] = []
    file_facts: dict[str, dict] = {}
    files = comps = stmts = skipped = 0
    for path in corpus_files():
        files += 1
        try:
            prog = Parser(path.read_text(encoding="utf-8"), str(path)).parse()
        except RevlError:
            skipped += 1
            continue
        rel = str(path.relative_to(REPO))
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
        # provide-key -> service, file-wide (children resolve handle receivers).
        psvc = {c.name: {k: s for k, s, _ln in c.provides} for c in prog.components}

        ff: dict = {"components": {}}
        for c in prog.components:
            requires = [(local, svc) for local, svc, _line in c.requires]
            provides = [key for key, _svc, _line in c.provides]
            require_map = dict(requires)
            comps += 1
            tsv.append(
                "\t".join(["M", rel, c.name,
                           ",".join(r for r, _s in requires),
                           ",".join(provides)])
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
        if prog.components:
            file_facts[rel] = ff
    return tsv, file_facts, {
        "files": files, "components": comps, "statements": stmts,
        "parse_errors": skipped,
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


def parse_verdicts(text: str) -> tuple[dict[str, tuple[str, str]],
                                       dict[tuple[str, str], str],
                                       dict[tuple[str, str, str, str, str], str],
                                       dict[tuple[str, str, str], str]]:
    """Parse oracle output into verdict maps: V rows (file manifests), G
    rows (per-component marker rule), P rows (per provide-method bound),
    W rows (per spawn-edge attenuation)."""
    files: dict[str, tuple[str, str]] = {}
    comps: dict[tuple[str, str], str] = {}
    providers: dict[tuple[str, str, str, str, str], str] = {}
    spawns: dict[tuple[str, str, str], str] = {}
    for line in text.splitlines():
        parts = line.split("\t")
        if parts[0] == "V" and len(parts) == 4:
            files[parts[1]] = (parts[2].split("=", 1)[1],
                               parts[3].split("=", 1)[1])
        elif parts[0] == "G" and len(parts) == 4:
            comps[(parts[1], parts[2])] = parts[3].split("=", 1)[1]
        elif parts[0] == "P" and len(parts) == 7:
            providers[(parts[1], parts[2], parts[3], parts[4], parts[5])] = \
                parts[6].split("=", 1)[1]
        elif parts[0] == "W" and len(parts) == 5:
            spawns[(parts[1], parts[2], parts[3])] = parts[4].split("=", 1)[1]
        else:
            raise SystemExit(f"differential oracle: malformed verdict row {line!r}")
    return files, comps, providers, spawns


def reference_from_tsv(tsv: list[str]) -> tuple[dict[str, tuple[str, str]],
                                                dict[tuple[str, str], str],
                                                dict[tuple[str, str, str, str, str], str],
                                                dict[tuple[str, str, str], str]]:
    """Reference verdicts, recomputed from the same TSV the oracle
    consumed — plain Python set logic, mirroring the Lean oracle's
    semantics exactly so the diff is zero by construction unless one side
    drifts.

    V rows are FILE-WIDE (disjoint + closed over the manifest). G rows
    are PER-COMPONENT marker-rule (marker presence == interface
    declaration, incl. spawn-handle receivers). P rows are PER-PROVIDE-
    METHOD: a service declaration is an upper bound — the method's
    reached emission tokens must be within its declared bound (plain =>
    none; any => free; scoped => the declared entries). W rows are
    PER-SPAWN-EDGE attenuation: a spawned child's closed reach must be
    covered by the spawner's held capabilities (item 66/294)."""
    rows = [r.split("\t") for r in tsv]
    mrows = [r for r in rows if r and r[0] == "M"]
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

    files: dict[str, tuple[str, str]] = {}
    for rel in sorted({r[1] for r in mrows}):
        fm = [r for r in mrows if r[1] == rel]
        provides = [k for r in fm for k in r[4].split(",") if k]
        requires = [k for r in fm for k in r[3].split(",") if k]
        files[rel] = ("ok" if len(provides) == len(set(provides)) else "fail",
                      "ok" if all(k in set(provides) for k in requires) else "fail")

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
            ok = {_split_canon_cap(c)[0] for c in caps} <= ents
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
        extra = cap_covers_set(held.get((rel, parent), set()),
                               closed.get((rel, child), set()))
        spawns[(rel, parent, child)] = "ok" if not extra else "fail"

    return files, comps, providers, spawns


def checker_alignment(file_facts: dict, formal_files: dict,
                      formal_comps: dict, formal_providers: dict,
                      formal_spawns: dict) -> None:
    """Compile each file with the real checker and compare refusal codes
    against the formal verdicts. Informational (STATUS.md): mismatches
    here are findings to investigate, not gate failures."""
    align: dict[str, int] = {}
    samples: dict[str, list[str]] = {}

    def record(key: str, rel: str) -> None:
        align[key] = align.get(key, 0) + 1
        samples.setdefault(key, []).append(rel)

    for rel in file_facts:
        comp_rows = [(k, v) for k, v in formal_comps.items() if k[0] == rel]
        prov_rows = [(k, v) for k, v in formal_providers.items() if k[0] == rel]
        spawn_rows = [(k, v) for k, v in formal_spawns.items() if k[0] == rel]
        formal_clean = formal_files.get(rel) == ("ok", "ok") and all(
            v == "ok" for _, v in comp_rows + prov_rows + spawn_rows)
        raw_found = any(v == "fail"
                        for _, v in comp_rows + prov_rows + spawn_rows)
        try:
            compile_source((REPO / rel).read_text(encoding="utf-8"), rel)
            code = "accept"
        except RevlError as e:
            code = classify(e).get("code") or "UNCODED"
        if code == "accept":
            # `formal-strict`: the checker ACCEPTS the file but the shaped
            # model does not — the model is stricter than the fragment it
            # covers, which is a finding to chase, not a licence to relax it.
            record("agree-accept" if formal_clean else "formal-strict", rel)
        elif code == "G4":
            record("agree-G4" if raw_found else "missed-G4", rel)
        elif code == "G2":
            disjoint_fail = formal_files.get(rel, ("ok",))[0] == "fail"
            record("agree-G2" if disjoint_fail else "missed-G2", rel)
        else:
            record("out-of-fragment" if formal_clean else "formal-found-other",
                   rel)

    total = sum(align.values())
    print(f"checker alignment ({total} files, informational):")
    for k in sorted(align):
        print(f"  {k:20} {align[k]}")
    for k in ("formal-strict", "missed-G4", "missed-G2"):
        for rel in samples.get(k, [])[:5]:
            print(f"  ALIGN-SAMPLE {k}: {rel}")


def main() -> int:
    tsv, file_facts, census = export()
    print(
        f"corpus census: {census['files']} .rvl files, "
        f"{census['components']} components, {census['statements']} statements, "
        f"{census['parse_errors']} parse-error skip(s)"
    )
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
    formal_files, formal_comps, formal_providers, formal_spawns = parse_verdicts(formal_text)
    ref_files, ref_comps, ref_providers, ref_spawns = reference_from_tsv(tsv)

    mismatches: list[str] = []
    for rel, ref in ref_files.items():
        got = formal_files.get(rel)
        if got is None:
            mismatches.append(f"file {rel}: no formal V row")
        elif got != ref:
            mismatches.append(f"file {rel}: reference={ref} formal={got}")
    for key, ref in ref_comps.items():
        got = formal_comps.get(key)
        if got is None:
            mismatches.append(f"comp {key}: no formal G row")
        elif got != ref:
            mismatches.append(f"comp {key}: reference={ref} formal={got}")
    for key, ref in ref_providers.items():
        got = formal_providers.get(key)
        if got is None:
            mismatches.append(f"provider {key}: no formal P row")
        elif got != ref:
            mismatches.append(f"provider {key}: reference={ref} formal={got}")
    for key, ref in ref_spawns.items():
        got = formal_spawns.get(key)
        if got is None:
            mismatches.append(f"spawn {key}: no formal W row")
        elif got != ref:
            mismatches.append(f"spawn {key}: reference={ref} formal={got}")
    compared = (len(ref_files) + len(ref_comps) + len(ref_providers)
                + len(ref_spawns))
    print(
        f"differential oracle: {compared} verdicts compared "
        f"({len(ref_files)} files + {len(ref_comps)} comps + "
        f"{len(ref_providers)} methods + {len(ref_spawns)} spawns) — "
        f"{compared - len(mismatches)} agree, {len(mismatches)} mismatch(es)"
    )
    for m in mismatches[:10]:
        print(f"  MISMATCH {m}")
    if len(mismatches) > 10:
        print(f"  ... and {len(mismatches) - 10} more")

    checker_alignment(file_facts, formal_files, formal_comps,
                      formal_providers, formal_spawns)
    return 1 if mismatches else 0


if __name__ == "__main__":
    sys.exit(main())

