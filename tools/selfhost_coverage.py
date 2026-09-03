#!/usr/bin/env python3
"""Blind-spot gate for the self-host byte-agreement oracles (roadmap item 429).

WHAT THE ORACLES CANNOT DO. `tests/test_selfhost_emit_*.py` run the reference
emitter (`backends/<tier>/emit.py`) and its revl port (`selfhost/emit_<tier>.rvl`)
over a corpus of `.rvl` documents and demand the emitted bytes be identical.
That catches DIVERGENCE, and only on inputs the corpus reaches. It cannot:

  (i)   say which side is right when both agree and both are wrong;
  (ii)  demand a feature, or a fix, on a construct the corpus never spells;
  (iii) notice that the two sides mirror DIFFERENT source-of-truth sets.

Item 429 records five same-day instances of (ii)/(iii). The one with teeth:
`selfhost/emit_py.rvl` could emit NEITHER end of the `Secret[T]` redaction
markings the reference emits, so the self-hosted emitter produced a program that
DOES NOT REDACT — and every oracle stayed green, because the corpus contained no
`Secret[` at all. A green oracle over a corpus that never spells a construct is
not evidence about that construct. It is silence.

THIS IS THE CHEAP CHECK, NOT THE GATE THE SURFACE RESTS ON. A construct is a
DISPATCH ARM, and an arm the corpus reaches can have nearly all of its body
unexercised, so a construct table is itself a proxy — a signal that certifies
less than it appears to, which is the failure mode this item is about, one level
up. Measured both ways: this file reports 19% of the mirrored constructs blind
where `tools/selfhost_line_coverage.py` reports 53.7% of the reference emitter
STATEMENTS never executed. Keep this one for what it is good at — it is fast and
it names a construct (`kind=optfield`) rather than a function — and read the
line gate for the size of the hole.

WHAT THIS GATE DOES. It measures that silence and refuses to let it grow.

For each tier it extracts a CONSTRUCT TABLE from both sides: every discriminant
the emitter branches on. A construct is a `field=value` pair (`kind=optfield`,
`step=let-effect`, `op=??`, `method=keys`, `name=Some`) or a boolean IR flag the
emitter tests directly (`secret_return=<true>`). Reference constructs come from
`ast`; self-host constructs come from the fixed `value_field` / `node_kind`
spellings the ported emitters use. Then it compiles the tier's corpus — the
exact `CORPUS` list its oracle parametrises over, not the directory — and
collects every `field=value` pair and truthy flag the corpus IR actually
exhibits.

Two populations fall out, both gated against the recorded ledger
`tests/fixtures/selfhost_blind_spots.json`:

  BLIND (`blind`)  — constructs BOTH sides implement that the corpus never
      exercises. The oracle asserts byte-agreement here and proves nothing: edit
      either side's branch and the gate stays green. Each one needs a written
      reason, because someone had to decide it was not worth a corpus case.

  UNPORTED (`unported`) — constructs the REFERENCE implements, the self-host
      does not, and the corpus does not reach. This is where the `Secret[T]`
      gap lived. A plain ratchet baseline: adding a branch to a reference
      emitter without a corpus case that reaches it fails this gate, and the
      fix is a corpus case (which then also makes the missing port visible), or
      a deliberate baseline entry.

Both populations RATCHET. A ledger entry that is no longer blind — because a
corpus case now reaches it, or because the branch is gone — fails the gate as
STALE and must be deleted. The ledger can therefore only shrink on its own; it
grows only when a human writes the entry and the reason.

WHAT THIS GATE CANNOT KNOW, and will not pretend to know.

  * It does not check that a covered construct is covered WELL. One corpus
    document touching `kind=bin` clears every `kind=bin` branch in the file.
    Coverage here is "the corpus can reach this dispatch arm at all", which is
    the weakest useful claim and the one the oracles were silently assuming.
  * It cannot catch category (i). When both sides agree and both are wrong, no
    differential and no coverage number says so. Item 429's standing rule is
    the only instrument for that: a change to logic mirrored in `selfhost/*` is
    checked against the SELF-HOST SOURCE directly, never via the oracle.
  * It cannot catch a defect that lives in the DATA of a branch the corpus
    already reaches — the non-injective keyword mangling of item 421 F3 is a
    collision between two names inside a `mangle` every corpus document calls.
    Only reading both sources finds those.

So a green run here means "no NEW silence", never "the port is correct".

Usage:
    python3 tools/selfhost_coverage.py            # human-readable report
    python3 tools/selfhost_coverage.py --check    # the gate (exit 1 on drift)
    python3 tools/selfhost_coverage.py --write    # regenerate the ledger
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LEDGER = ROOT / "tests" / "fixtures" / "selfhost_blind_spots.json"

# tier -> (reference emitter package, self-host port, corpus dir, oracle test)
TIERS = {
    "py": ("python", "emit_py", "emit_py_corpus"),
    "ts": ("typescript", "emit_ts", "emit_ts_corpus"),
    "go": ("go", "emit_go", "emit_go_corpus"),
    "java": ("java", "emit_java", "emit_java_corpus"),
    "rust": ("rust", "emit_rust", "emit_rust_corpus"),
    "wasm": ("wasm", "emit_wasm", "emit_wasm_corpus"),
}

TRUE = "<true>"  # the value side of a boolean-flag construct


# --------------------------------------------------------------- reference

def _field_of(node: ast.AST) -> str | None:
    """`x.get("F")` / `x["F"]` -> "F"; anything else -> None."""
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get" and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)):
        return node.args[0].value
    if (isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant)
            and isinstance(node.slice.value, str)):
        return node.slice.value
    return None


def _str_operands(node: ast.AST) -> list[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return [e.value for e in node.elts
                if isinstance(e, ast.Constant) and isinstance(e.value, str)]
    return []


def reference_constructs(path: Path) -> dict[str, int]:
    """Every discriminant `backends/<tier>/emit.py` branches on -> first line.

    A local bound from a discriminant read (`kind = node.get("step")`) carries
    the field to its comparisons. The binding in force is the nearest one ABOVE
    the comparison: these emitters rebind `kind` per dispatcher, and a
    file-wide union would credit `_fn_stmt`'s `step=let` to `_expr`'s `kind`.
    """
    tree = ast.parse(path.read_text())
    # name -> [(line, field-or-None)]. A binding from something that is NOT a
    # discriminant read records None, which INVALIDATES the name from that line
    # on. Without that, a `for index, (kind, value) in ...` unpacking shadows an
    # earlier `kind = node.get("kind")` and every `kind == "arg"` below it is
    # credited to the IR as a node kind it never had.
    binds: dict[str, list[tuple[int, str | None]]] = {}

    def bind(name: str, line: int, value: ast.AST | None) -> None:
        binds.setdefault(name, []).append(
            (line, _field_of(value) if value is not None else None))

    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    bind(target.id, node.lineno, node.value)
                else:
                    for inner in ast.walk(target):
                        if isinstance(inner, ast.Name):
                            bind(inner.id, node.lineno, None)
        elif isinstance(node, ast.NamedExpr) and isinstance(node.target, ast.Name):
            bind(node.target.id, node.lineno, node.value)
        elif isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)):
            target = node.target
            line = getattr(node, "lineno", getattr(target, "lineno", 0))
            for inner in ast.walk(target):
                if isinstance(inner, ast.Name):
                    bind(inner.id, line, None)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for argument in [*node.args.args, *node.args.kwonlyargs,
                             *node.args.posonlyargs]:
                bind(argument.arg, node.lineno, None)
    for entries in binds.values():
        entries.sort()

    def resolve(node: ast.AST) -> str | None:
        field = _field_of(node)
        if field:
            return field
        if isinstance(node, ast.Name):
            in_force = [f for line, f in binds.get(node.id, ()) if line <= node.lineno]
            if in_force:
                return in_force[-1]
        return None

    out: dict[str, int] = {}

    def record(field: str, value: str, lineno: int) -> None:
        out.setdefault(f"{field}={value}", lineno)

    for node in ast.walk(tree):
        if isinstance(node, ast.Compare) and len(node.ops) == 1 and isinstance(
                node.ops[0], (ast.Eq, ast.NotEq, ast.In, ast.NotIn)):
            field = resolve(node.left)
            if field:
                for value in _str_operands(node.comparators[0]):
                    record(field, value, node.lineno)
        # A boolean IR flag BRANCHED ON: `if node.get("secret"):`. Only a real
        # test position counts. `cond = loop.get("cond") or {}` is a
        # default-value idiom, not a branch on the flag, and counting it put
        # three phantom constructs in the go table before this was tightened.
        conditions: list[ast.AST] = []
        if isinstance(node, (ast.If, ast.IfExp, ast.While)):
            conditions = [node.test]
        elif isinstance(node, ast.Assert):
            conditions = [node.test]
        elif isinstance(node, ast.comprehension):
            # `[i for i, p in enumerate(params) if p.get("secret")]` is a branch
            # on the flag as surely as an `if` statement is, and the python
            # reference reads the item-421-F6 param marking exactly this way.
            conditions = list(node.ifs)
        for condition in conditions:
            for test in _test_operands(condition):
                field = _field_of(test)
                if field:
                    record(field, TRUE, getattr(test, "lineno", 0))
    return out


def _test_operands(test: ast.AST) -> list[ast.AST]:
    """A condition, flattened through `and`/`or`/`not` to the leaves it tests."""
    if isinstance(test, ast.BoolOp):
        return [leaf for value in test.values for leaf in _test_operands(value)]
    if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
        return _test_operands(test.operand)
    return [test]


# ---------------------------------------------------------------- selfhost

_HELPER = re.compile(
    r'fn\s+(\w+)\s*\([^)]*\)\s*->\s*Str\s*\{\s*return\s+'
    r'value_str\(value_field\(\w+,\s*"(\w+)"\)\)\s*\}')
_LET_HELPER = re.compile(r'let\s+(\w+)\s*=\s*(\w+)\(')
_LET_DIRECT = re.compile(r'let\s+(\w+)\s*=\s*value_str\(value_field\([^,]+,\s*"(\w+)"\)\)')
_INLINE = re.compile(r'value_str\(value_field\([^,]+,\s*"(\w+)"\)\)\s*[!=]=\s*"([^"]*)"')
_CALL_CMP = re.compile(r'\b(\w+)\((?:[^()]|\([^()]*\))*\)\s*[!=]=\s*"([^"]*)"')
_VAR_CMP = re.compile(r'\b(\w+)\s*[!=]=\s*"([^"]*)"')
_BOOL_FLAG = re.compile(r'value_bool\(value_field\([^,]+,\s*"(\w+)"\)\)')


def selfhost_constructs(path: Path) -> set[str]:
    """Every discriminant `selfhost/emit_<tier>.rvl` branches on.

    The ported emitters read the IR through one fixed spelling set (item 185's
    `value_*` navigation), so a small grammar is exact enough: the `node_kind` /
    `node_step` / `node_op` accessor helpers, `let` bindings off them, and the
    inline `value_str(value_field(n, "f")) == "v"` form. As on the reference
    side a bound name resolves to the NEAREST binding above the comparison.
    """
    src = path.read_text()
    helpers = {m.group(1): m.group(2) for m in _HELPER.finditer(src)}
    binds: dict[str, list[tuple[int, str]]] = {}

    def line_of(offset: int) -> int:
        return src.count("\n", 0, offset) + 1

    for match in _LET_HELPER.finditer(src):
        field = helpers.get(match.group(2))
        if field:
            binds.setdefault(match.group(1), []).append((line_of(match.start()), field))
    for match in _LET_DIRECT.finditer(src):
        binds.setdefault(match.group(1), []).append((line_of(match.start()), match.group(2)))
    for entries in binds.values():
        entries.sort()

    out: set[str] = set()
    for match in _INLINE.finditer(src):
        out.add(f"{match.group(1)}={match.group(2)}")
    for match in _CALL_CMP.finditer(src):
        field = helpers.get(match.group(1))
        if field:
            out.add(f"{field}={match.group(2)}")
    for match in _VAR_CMP.finditer(src):
        entries = binds.get(match.group(1))
        if not entries:
            continue
        line = line_of(match.start())
        in_force = [f for at, f in entries if at <= line]
        if in_force:
            out.add(f"{in_force[-1]}={match.group(2)}")
    for match in _BOOL_FLAG.finditer(src):
        out.add(f"{match.group(1)}={TRUE}")
    return out


# ------------------------------------------------------------------ corpus

def corpus_documents(tier: str) -> list[Path]:
    """The oracle's OWN corpus list, parsed out of the test module.

    Not the directory: a fixture present on disk but absent from `CORPUS` is a
    document the oracle never runs, so it proves nothing and must not be
    credited with coverage here either.
    """
    test = (ROOT / "tests" / f"test_selfhost_emit_{tier}.py").read_text()
    block = re.search(r"^CORPUS\s*=\s*\[(.*?)^\]", test, re.S | re.M)
    if block is None:  # pragma: no cover - shape change in an oracle
        raise SystemExit(f"cannot find a CORPUS list in test_selfhost_emit_{tier}.py")
    names = sorted(set(re.findall(r'"([^"]+\.rvl)"', block.group(1))))
    directory = ROOT / "tests" / "fixtures" / TIERS[tier][2]
    return [directory / name for name in names]


def _pairs(node: object, into: set[str]) -> set[str]:
    if isinstance(node, dict):
        for key, value in node.items():
            if isinstance(value, str):
                into.add(f"{key}={value}")
            elif value is True:
                into.add(f"{key}={TRUE}")
            else:
                _pairs(value, into)
    elif isinstance(node, list):
        for value in node:
            _pairs(value, into)
    return into


def corpus_constructs(tier: str) -> set[str]:
    sys.path.insert(0, str(ROOT / "src"))
    from revl import compile_files  # noqa: PLC0415 - keep the import off `--help`

    exhibited: set[str] = set()
    for document in corpus_documents(tier):
        _pairs(compile_files([str(document)]), exhibited)
    return exhibited


# ------------------------------------------------------------------- gate

def survey() -> dict[str, dict[str, object]]:
    result: dict[str, dict[str, object]] = {}
    for tier, (package, port, _) in TIERS.items():
        reference = reference_constructs(ROOT / "backends" / package / "emit.py")
        ported = selfhost_constructs(ROOT / "selfhost" / f"{port}.rvl")
        covered = corpus_constructs(tier)
        mirrored = set(reference) & ported
        result[tier] = {
            "reference": sorted(reference),
            "mirrored": sorted(mirrored),
            "blind": sorted(mirrored - covered),
            "unported": sorted(set(reference) - ported - covered),
            "documents": len(corpus_documents(tier)),
        }
    return result


def _ledger() -> dict:
    """The recorded blind spots, minus the `_`-prefixed prose keys JSON has no
    comment syntax for."""
    raw = json.loads(LEDGER.read_text())
    return {k: v for k, v in raw.items() if not k.startswith("_")}


def _reasoned(entry: dict) -> dict[str, str]:
    """`blind` is stored reason-first (one reason, many constructs) so the
    ledger reads as an argument rather than as a list of strings."""
    flat: dict[str, str] = {}
    for reason, constructs in entry.items():
        for construct in constructs:
            flat[construct] = reason
    return flat


def check(data: dict) -> list[str]:
    ledger = _ledger()
    problems: list[str] = []
    for tier, found in data.items():
        recorded = ledger.get(tier, {})
        waived = _reasoned(recorded.get("blind", {}))
        baseline = set(_reasoned(recorded.get("unported", {})))

        blind = set(found["blind"])
        for construct in sorted(blind - set(waived)):
            problems.append(
                f"{tier}: `{construct}` is mirrored in selfhost/{TIERS[tier][1]}.rvl "
                f"and the corpus never reaches it, so the byte-agreement oracle "
                f"asserts nothing about it. Add a corpus document that spells it "
                f"(see it FAIL before porting anything, item 429 exit (3)), or "
                f"record it under `blind` in {LEDGER.name} with the reason.")
        for construct in sorted(set(waived) - blind):
            problems.append(
                f"{tier}: `{construct}` is recorded blind in {LEDGER.name} but is "
                f"not blind any more (the corpus reaches it, or the branch is "
                f"gone). Delete the entry: this ledger only shrinks.")

        unported = set(found["unported"])
        for construct in sorted(unported - baseline):
            problems.append(
                f"{tier}: `{construct}` is a NEW reference-only construct that no "
                f"corpus document reaches — the exact shape of the item-429(d) "
                f"`Secret[T]` gap, where the reference redacted, the self-host did "
                f"not, and every oracle stayed green. Add a corpus case, port it, "
                f"or add it to `unported` in {LEDGER.name} on purpose.")
        for construct in sorted(baseline - unported):
            problems.append(
                f"{tier}: `{construct}` is in the `unported` baseline of "
                f"{LEDGER.name} but is now ported or covered. Delete the entry.")
    return problems


UNPORTED_DEFAULT = (
    "NOT TRIAGED - the reference implements this and the port does not, and no "
    "corpus document reaches it, so nothing would notice either way. Recorded "
    "by `--write` so the set is NAMED and cannot grow in silence. Moving an "
    "entry out of this bucket means someone decided what it is.")


def _regroup(existing: dict, present: set[str], default: str) -> dict:
    """Keep every hand-written reason, drop what is no longer true, file the
    rest under `default`. The ledger's shape survives a regeneration; only its
    contents move."""
    grouped = {reason: sorted(set(items) & present)
               for reason, items in existing.items()}
    claimed = {item for items in grouped.values() for item in items}
    leftover = sorted(present - claimed)
    if leftover:
        grouped.setdefault(default, [])
        grouped[default] = sorted(set(grouped[default]) | set(leftover))
    return {reason: items for reason, items in sorted(grouped.items()) if items}


def write_ledger(data: dict) -> None:
    ledger = json.loads(LEDGER.read_text()) if LEDGER.exists() else {}
    for tier, found in data.items():
        entry = ledger.setdefault(tier, {})
        entry["blind"] = _regroup(
            entry.get("blind", {}), set(found["blind"]),
            "NOT TRIAGED - both sides implement this and no corpus document "
            "reaches it. Recorded by `--write`; say WHY, or add the corpus case.")
        entry["unported"] = _regroup(
            entry.get("unported", {}) if isinstance(entry.get("unported"), dict) else {},
            set(found["unported"]), UNPORTED_DEFAULT)
    LEDGER.write_text(json.dumps(ledger, indent=2, sort_keys=True) + "\n")


def report(data: dict) -> None:
    total_mirrored = total_blind = total_unported = 0
    print(f"{'tier':6s} {'docs':>4s} {'ref':>5s} {'mirrored':>9s} "
          f"{'BLIND':>6s} {'unported':>9s}")
    for tier, found in data.items():
        mirrored, blind = len(found["mirrored"]), len(found["blind"])
        unported = len(found["unported"])
        total_mirrored += mirrored
        total_blind += blind
        total_unported += unported
        share = 100.0 * blind / mirrored if mirrored else 0.0
        print(f"{tier:6s} {found['documents']:4d} {len(found['reference']):5d} "
              f"{mirrored:9d} {blind:6d} ({share:3.0f}%) {unported:9d}")
    share = 100.0 * total_blind / total_mirrored if total_mirrored else 0.0
    print(f"{'TOTAL':6s} {'':4s} {'':5s} {total_mirrored:9d} "
          f"{total_blind:6d} ({share:3.0f}%) {total_unported:9d}")
    print()
    print("BLIND    = both sides implement it, no corpus document reaches it:")
    print("           the oracle proves nothing there.")
    print("unported = the reference implements it, the self-host does not, and")
    print("           no corpus document would notice.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true",
                        help="fail on any blind construct the ledger does not record")
    parser.add_argument("--write", action="store_true",
                        help="regenerate the ledger from the current tree")
    parser.add_argument("--json", action="store_true", help="dump the survey as JSON")
    args = parser.parse_args(argv)

    data = survey()
    if args.write:
        write_ledger(data)
        print(f"wrote {LEDGER.relative_to(ROOT)}")
        return 0
    if args.json:
        json.dump(data, sys.stdout, indent=1)
        return 0
    if args.check:
        problems = check(data)
        for problem in problems:
            print(f"FAIL {problem}")
        if problems:
            print(f"\n{len(problems)} self-host coverage problem(s).")
            return 1
        print("self-host construct coverage matches the recorded blind spots.")
        return 0
    report(data)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
