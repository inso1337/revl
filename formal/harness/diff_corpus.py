"""Differential harness: the formal models vs the extracted corpus + checker.

Pipeline (formal/STATUS.md, "differential oracle"):

1. parse every .rvl in the corpus with revl's real parser (`revl.parser`);
2. extract FACTS, not verdicts: component manifests, require-binding ->
   service resolution, the file's declared emission methods, per-statement
   classification (effect/emit are marked strata by grammar), and unmarked
   call facts (a call in a non-effect, non-emit statement whose receiver
   is a declared require binding);
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
)



def corpus_files() -> list[Path]:
    out: list[Path] = []
    for d in CORPUS_DIRS:
        root = REPO / d
        if root.is_dir():
            out.extend(sorted(root.rglob("*.rvl")))
    return out


# Marked strata: the grammar pairs the inverse or places the marker here,
# so a call underneath one of these nodes is never an unmarked call site.
_MARKED = (EffectStmt, LetEffect, EmitStmt, EmitExpr)


def _head(callee: object) -> tuple[str, str] | None:
    """Receiver root + method of a call callee, None when not that shape."""
    if isinstance(callee, ExprField) and isinstance(callee.target, ExprVar):
        return callee.target.name, callee.name
    if isinstance(callee, ExprVar) and "." in callee.name:
        root, meth = callee.name.split(".", 1)
        return root, meth
    return None


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
        head = _head(node.callee)
        if head is not None:
            out.append((*head, ctx))
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
        services = {
            svc.name: {m: md.emission for m, md in svc.methods.items()}
            for svc in prog.services
        }
        emissions = [
            (svc, m)
            for svc, methods in services.items()
            for m, em in methods.items()
            if em
        ]
        for svc, meth in emissions:
            tsv.append("\t".join(["E", rel, svc, meth]))

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
                for root, meth, ctx in local_calls:
                    svc = require_map.get(root)
                    if svc is None:
                        continue  # host/local/provide receiver: not a crossing
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

            for stmt in c.body:
                classify_stmt(stmt)
            for stmt in c.body:
                if type(stmt).__name__ == "ProvideStmt":
                    for pm in getattr(stmt, "methods", []):
                        for inner in getattr(pm, "body", []):
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
                                       dict[tuple[str, str], str]]:
    """Parse oracle output: V rows (file manifest verdicts) and G rows
    (per-component G4-shaped verdicts)."""
    files: dict[str, tuple[str, str]] = {}
    comps: dict[tuple[str, str], str] = {}
    for line in text.splitlines():
        parts = line.split("\t")
        if parts[0] == "V" and len(parts) == 4:
            files[parts[1]] = (parts[2].split("=", 1)[1],
                               parts[3].split("=", 1)[1])
        elif parts[0] == "G" and len(parts) == 4:
            comps[(parts[1], parts[2])] = parts[3].split("=", 1)[1]
        else:
            raise SystemExit(f"differential oracle: malformed verdict row {line!r}")
    return files, comps


def reference_from_tsv(tsv: list[str]) -> tuple[dict[str, tuple[str, str]],
                                                dict[tuple[str, str], str]]:
    """Reference verdicts, recomputed from the same TSV the oracle
    consumed — plain Python set logic, mirroring the Lean oracle's
    semantics exactly so the diff is zero by construction unless one side
    drifts.

    V rows are FILE-WIDE: provision-disjoint means no key is provided
    twice anywhere in the composition, and requirement-closure means every
    required key in the file appears among the composition's provisions
    (a component's requirements need not be its own provisions). G rows
    are PER-COMPONENT and G4-shaped: a call to a declared emission
    method is legal iff its marker context is `emit`, and an `emit`'d
    call to a non-emission method is refused — marker presence must equal
    the interface's declaration (the `bad` rule the exporter computes)."""
    rows = [r.split("\t") for r in tsv]
    mrows = [r for r in rows if r and r[0] == "M"]
    urows = [r for r in rows if r and r[0] == "U" and len(r) == 7]
    erows = [r for r in rows if r and r[0] == "E"]
    ems_by_file: dict[str, set[tuple[str, str]]] = {}
    for r in erows:
        ems_by_file.setdefault(r[1], set()).add((r[2], r[3]))
    files: dict[str, tuple[str, str]] = {}
    for rel in sorted({r[1] for r in mrows}):
        fm = [r for r in mrows if r[1] == rel]
        provides = [k for r in fm for k in r[4].split(",") if k]
        requires = [k for r in fm for k in r[3].split(",") if k]
        disjoint = len(provides) == len(set(provides))
        closed = all(k in set(provides) for k in requires)
        files[rel] = ("ok" if disjoint else "fail",
                      "ok" if closed else "fail")
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
    return files, comps


def checker_alignment(file_facts: dict, formal_files: dict,
                      formal_comps: dict) -> None:
    """Compile each file with the real checker and compare refusal codes
    against the formal verdicts. Informational (STATUS.md): mismatches
    here are findings to investigate, not gate failures."""
    align: dict[str, int] = {}
    samples: dict[str, list[str]] = {}

    def record(key: str, rel: str) -> None:
        align[key] = align.get(key, 0) + 1
        samples.setdefault(key, []).append(rel)

    for rel in file_facts:
        g_rows = [(k, v) for k, v in formal_comps.items() if k[0] == rel]
        formal_clean = formal_files.get(rel) == ("ok", "ok") and all(
            v == "ok" for _, v in g_rows)
        raw_found = any(v == "fail" for _, v in g_rows)
        try:
            compile_source((REPO / rel).read_text(encoding="utf-8"), rel)
            code = "accept"
        except RevlError as e:
            code = classify(e).get("code") or "UNCODED"
        if code == "accept":
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
    formal_files, formal_comps = parse_verdicts(formal_text)
    ref_files, ref_comps = reference_from_tsv(tsv)

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
    compared = len(ref_files) + len(ref_comps)
    print(
        f"differential oracle: {compared} verdicts compared "
        f"({len(ref_files)} files + {len(ref_comps)} components) — "
        f"{compared - len(mismatches)} agree, {len(mismatches)} mismatch(es)"
    )
    for m in mismatches[:10]:
        print(f"  MISMATCH {m}")
    if len(mismatches) > 10:
        print(f"  ... and {len(mismatches) - 10} more")

    checker_alignment(file_facts, formal_files, formal_comps)
    return 1 if mismatches else 0


if __name__ == "__main__":
    sys.exit(main())

