#!/usr/bin/env python3
"""LINE coverage of the mirrored reference emitters under the self-host corpus.

WHY THIS EXISTS, given `tools/selfhost_coverage.py` already ships a construct
gate. That gate counts DISPATCH ARMS: 72 of 360 constructs implemented on both
sides are never reached by any corpus document. A construct is a coarse unit. A
construct counted as covered can have most of its body unexercised — an inner
branch, an error arm, a fallback. Item 429 is about a green signal that
certifies less than it appears to, and a construct table is that shape one level
up.

So this measures the thing itself: which STATEMENTS of the reference emitter no
corpus document executes. The reference is Python, so `coverage.py` gives it
directly, with the tier's own `CORPUS` list as the workload — the exact
documents `tests/test_selfhost_emit_<tier>.py` holds to byte agreement, not the
fixture directory.

MEASURED (2026-09-02, re-taken on current main). The reference emitter under
the tier's own corpus, the self-host port under the same corpus, and — to price
the fix — the reference under EVERY `.rvl` in the tree:

    tier  docs  ref stmts  ref cov   port stmts  port cov   whole tree
    py      20       2151    56.4%         1990     73.2%        78.9%
    ts      32       1811    65.9%         2084     77.8%        83.5%
    go      10       3984    25.2%         1509     67.9%        74.5%
    java    22       2403    55.7%         2423     72.9%        81.5%
    rust    19       3154    49.8%         1865     79.8%        85.9%
    wasm    11       2534    42.8%         1346     78.9%        84.1%
    TOTAL  114      16037    46.2%        11217     75.1%        80.9%

**MORE THAN HALF of the reference emitter statements the byte-agreement oracles
run against are never executed by the corpus those oracles use: 8633 of 16037,
53.8%.** The construct gate says 19% blind. It is optimistic by nearly three
times, in the direction that matters, which is the same failure mode item 429 is
about, one level up.

WHERE THE UNCOVERED MASS SITS, and why a construct table cannot see it:

    3899 statements in 263 functions no corpus document CALLS AT ALL
    2994 statements in 318 functions the corpus DOES call and leaves unrun
    1740 statements on declared exclusions and named open gaps

That middle row is the point. The dispatch arm is reached, so the construct
counts as covered, while the branch, error arm or fallback below it never runs.

AUTHOR CASES, OR POINT THE ORACLE AT MORE INPUTS? Measured, because the two
answers cost very differently. The one-off agreement survey behind this
paragraph was taken when the tree held 867 `.rvl` documents of which 619
compiled (it now holds 1023 of which 766 compile, and the whole-tree column
above is re-taken; the agreement split below is not, so read it as the shape,
not as today's count). Running all six ports over them gave ~2950 (tier,
document) pairs the reference emits, of which **1136 ALREADY AGREE
byte-for-byte and the rest DIVERGE**. Adopting every agreeing document — a
tenfold corpus, 1136 documents, zero authoring — moved reference coverage from
46.3% to only **51.0%**. The whole tree reaches 80.9%, so the remaining ~30
points live ENTIRELY on the diverging pairs.

The binding constraint is therefore neither corpus size nor authoring: it is
TRIAGE of those divergences, each of which is either a real self-host gap to
port or a declared out-of-slice shape to record. Free adoption buys about five
points and is worth taking; the rest has to be decided divergence by divergence,
which is item 429's exit (2) and the standing rule in `docs/process.md`, not a
corpus-authoring exercise.

NOT AFFECTED BY THE `_infile_programs()` TRUNCATION BUG. That harvester in
`tests/test_selfhost_lower.py` scans a plain string literal to the next `"` and
does not honour `\"`, so a program containing an escaped quote is silently cut
short. Nothing here goes through it: `corpus_documents()` parses the tier's
`CORPUS` list for FILENAMES and reads those `.rvl` files off disk. (Checked at
the source anyway: 50 programs are harvested there and none is currently
truncated, because every `\"` in that section sits inside a `\"\"\"` literal,
which the harvester scans correctly. The bug is real and latent, not biting.)

WHAT THE LEDGER IS KEYED BY, and why not line numbers. A ratchet keyed by line
NUMBER churns on every edit above it — insert one statement at the top of a file
and eight hundred entries move. So the unit here is (qualified function, count
of uncovered statements). It is still a LINE measurement: the number is a count
of statements coverage.py did not see execute. It survives edits elsewhere in
the file, it names the region a reader has to go look at, and it moves in one
direction.

RATCHET. A function whose uncovered count RISES fails: new logic arrived that no
corpus document reaches. A function whose count FALLS also fails, with the
command to record the improvement — that is what keeps the number monotone
rather than merely bounded. A function that appears with uncovered statements
and is not in the ledger fails.

AND A BUDGET, because the ratchet above was not enough on its own (issue #1419).
It moves both ways and has moved both ways (21 of the 88 changes to the ledger
LOWERED the mass, and the 2026-09-05 corpus triage took 2234 statements out of
it in five commits), but nothing bounded it, so `--write` plus a written reason was
a complete answer to the gate firing, and over 2026-09-16..24 that is the answer
that got given. The budget in the ledger is the bound: a count that `--write`
does not write and that the gate holds the recorded count to EXACTLY, so
recording costs an integer raised by hand in the diff and an improvement
permanently lowers the ceiling instead of leaving headroom. It was per half and
tier until issue #1768 and is per function now. The target it shrinks toward is
zero.

THE THIRD OPTION, same issue. `tests/fixtures/emit_<tier>_refusals/` holds
documents the tier's reference REFUSES BY NAME. Both halves are driven over them.
A refusal is logic both halves carry and no CORPUS document can reach, because a
corpus document is one the reference emits and a refusal path runs only where it
does not: there are no reference bytes to agree with. Before this directory the
gate's instruction ("add a corpus document, or record the count") offered that
population nothing but `--write`. See `refusal_documents()`.

WHAT THIS CANNOT KNOW. Statement coverage is not branch coverage: a line that
executed once, on one shape of input, counts as covered here. BOTH sides are
measured, but the port's emitted Python statements map to `.rvl` FUNCTIONS,
not `.rvl` source lines: source-line provenance is still absent. And a covered
line is not a correct line: when both sides agree and both are wrong, no
coverage number says so.

THE LAYOUT, AND WHY IT IS ONE RECORD PER LINE (issue #1768). The ledger used to
be one JSON file with a per-half, per-tier `_budget` block and per-tier
`statements`/`uncovered_statements` totals. Every pull request that moved a
count rewrote one of six adjacent budget lines and a total, so two pull requests
that touched different functions of the same emitter, or even of different
emitters, conflicted after every landing. The totals were never checked, and
they had drifted. Now the ledger is `tests/fixtures/selfhost_uncovered_lines/`,
with one file per half and tier, `<half>/<tier>.jsonl`, holding two kinds of
record, one per line, a blank line between records, sorted by reason id:

    ["reason", "<reason id>", "<why these statements are unreached>"]
    ["function", "<reason id>", "<qualified function>", <uncovered>, <budget>]

The budget is PER FUNCTION now. It means what `_budget` meant, at a finer grain:
`--write` rewrites `<uncovered>` and never `<budget>`, and the gate holds each
function's recorded count to its budget EXACTLY, in both directions. Raising one
is still a single integer edited by hand, and it now sits beside the function it
pays for. A per-function budget is at least as strict as the per-tier one it
replaces, which let one function's rise hide behind another's fall. Totals are
computed when the report prints them, never stored. Two pull requests now
conflict in the ledger only when both touched the same record.

A merge conflict in the ledger is resolved with
`python3 tools/selfhost_line_coverage.py --write`: it reads through the conflict
markers (the first copy of a record that appears twice wins), re-measures every
count and rewrites the files clean. It never writes a budget, so `--check` then
names any budget the merged counts disagree with.

Usage:
    python3 tools/selfhost_line_coverage.py            # the report
    python3 tools/selfhost_line_coverage.py --check    # the gate
    python3 tools/selfhost_line_coverage.py --write    # record the current state
    python3 tools/selfhost_line_coverage.py --frontend # also measure src/revl
"""

from __future__ import annotations

import argparse
import ast
import importlib.util
import json
import re
import sys
import tempfile
import types
from collections import Counter
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# The ledger is a directory (issue #1768): `README.md` says what it is, and one
# `<half>/<tier>.jsonl` per half and tier holds its records. See
# "THE LAYOUT, AND WHY IT IS ONE RECORD PER LINE" above.
LEDGER = ROOT / "tests" / "fixtures" / "selfhost_uncovered_lines"
HALVES = ("reference", "selfhost")


# The self-host module's entry function, per tier. ALL SIX tiers now spell it
# `emit_<tier>_src`: that rename is what admits a tier into selfhost/compile.rvl's
# composition, which flattens public decls by bare name, and item 146 gap 2
# finished it for go/java/wasm after py/rust (item 230) and ts (#209). The legacy
# `emit_src` fallback is kept so a newly ported tier measures before it is wired.
# It still fails LOUDLY — never silently measuring nothing — when neither name is
# present.
def _entry(module, tier: str):
    for name in (f"emit_{tier}_src", "emit_src"):
        found = getattr(module, name, None)
        if found is not None:
            return found
    raise AttributeError(
        f"the emitted selfhost/emit_{tier}.rvl declares neither "
        f"`emit_{tier}_src` nor `emit_src`: the port's entry point was renamed "
        f"and tools/selfhost_line_coverage.py has to be told which function to "
        f"drive, or this gate measures nothing")


TIERS = {
    "py": "python",
    "ts": "typescript",
    "go": "go",
    "java": "java",
    "rust": "rust",
    "wasm": "wasm",
}

# The shared frontend the selfhost/lower.rvl and selfhost/checker.rvl ports
# mirror. Measured and reported, NOT gated: their oracles drive a different
# corpus (inline programs in the test modules), so the emit corpora understate
# them. Numbers here are context, not a verdict.
FRONTEND = ("lower.py", "typecheck.py", "parser.py", "lexer.py")

# The two fallback buckets, and the reason the split is worth making. A
# function the corpus NEVER ENTERS is a hole the construct table could in
# principle have seen. A function the corpus enters and leaves half unexecuted
# is the hole it structurally CANNOT see: its dispatch arm is reached, so the
# construct counts as covered, while the arm's body is not exercised at all.
# That second bucket is the measurement this file exists to produce.
NEVER_ENTERED = (
    "NEVER ENTERED - no corpus document calls this function at all. Not on any "
    "declared exclusion list either, so this is an untriaged hole: someone has "
    "to decide whether it needs a corpus document or an exclusion.")
PARTIAL = (
    "PARTIALLY EXERCISED - the corpus DOES call this function and leaves these "
    "statements unexecuted. This is the bucket a construct table cannot see: "
    "the dispatch arm is reached, so the construct counts as covered, while the "
    "branch, error arm or fallback below it never runs.")


def corpus_documents(tier: str) -> list[Path]:
    """The oracle's OWN corpus list (see tools/selfhost_coverage.py)."""
    test = (ROOT / "tests" / f"test_selfhost_emit_{tier}.py").read_text()
    block = re.search(r"^CORPUS\s*=\s*\[(.*?)^\]", test, re.S | re.M)
    if block is None:  # pragma: no cover - shape change in an oracle
        raise SystemExit(f"cannot find a CORPUS list in test_selfhost_emit_{tier}.py")
    directory = ROOT / "tests" / "fixtures" / f"emit_{tier}_corpus"
    return [directory / name
            for name in sorted(set(re.findall(r'"([^"]+\.rvl)"', block.group(1))))]


def refusal_documents(tier: str) -> list[Path]:
    """Documents this tier's REFERENCE REFUSES BY NAME, driven through both halves.

    THE THIRD OPTION, and the reason it had to exist. Until this directory, the
    gate's message offered two responses to an uncovered statement: reach it with
    a corpus document, or record it with a reason. For a whole population of
    statements only the second was possible, and that population is not small:
    every refusal a tier states by name has a mirror in the port, and NEITHER
    half can be reached by a byte-agreement document. A corpus document is one
    the reference EMITS; a refusal path runs only on a document the reference
    REFUSES, so there are no reference bytes for the oracle to agree with and the
    document cannot be in `CORPUS` at all. `--write` was the only move available,
    so `--write` is the move that got made, and the ledger accumulated a class of
    entry that no amount of corpus authoring could ever have retired.

    Measured on `9615b9199`, driving the one rust document here moved three
    counts, all downward and all in exactly that class:

        reference/rust `_refuse_required_stream`   1 -> 0
        selfhost/rust  `require_ty`                1 -> 0   (issue #1419's red)
        selfhost/rust  `render_inner`              5 -> 2

    THE NON-VACUITY GUARD, because a directory of documents nobody checks is a
    way to cover anything. `measure()` asserts that this tier's reference REFUSES
    every document here. One it emits is a byte-agreement case and belongs in
    `CORPUS`, where the oracle will hold its bytes; dropping it here instead
    would buy coverage with no agreement assertion behind it, which is the shape
    item 429 exists to rule out. The refusal's TEXT is not this gate's business:
    that is the tier oracle's, and for the document here it is
    `tests/test_selfhost_emit_rust.py::
    test_a_required_stream_coeffect_the_reference_refuses_is_named_here_too`.
    """
    directory = ROOT / "tests" / "fixtures" / f"emit_{tier}_refusals"
    if not directory.is_dir():
        return []
    return sorted(directory.glob("*.rvl"))


def _owners(path: Path) -> dict[int, str]:
    """line -> the qualified function that owns it (`Class.method`, `func`)."""
    owners: dict[int, str] = {}

    def walk(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = prefix + child.name
                for line in range(child.lineno, (child.end_lineno or child.lineno) + 1):
                    owners[line] = name
                walk(child, name + ".")

    walk(ast.parse(path.read_text()), "")
    return owners


def _per_function(cov, path: Path) -> dict:
    _, statements, _, missing, _ = cov.analysis2(str(path))
    owners = _owners(path)
    total: Counter[str] = Counter()
    absent: Counter[str] = Counter()
    for line in statements:
        total[owners.get(line, "<module>")] += 1
    for line in missing:
        absent[owners.get(line, "<module>")] += 1
    return {
        "statements": len(statements),
        "uncovered": len(missing),
        "functions": {name: absent[name] for name in sorted(absent)},
        "sizes": {name: total[name] for name in sorted(total)},
    }


def load_reference(tier: str):
    """Load this checkout's emitter by path, as the differential oracles do."""
    sys.path.insert(0, str(ROOT / "src"))
    spec = importlib.util.spec_from_file_location(
        f"oracle_reference_{tier}", ROOT / "backends" / TIERS[tier] / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@contextmanager
def selfhost_module(tier: str, python_reference, scratch: Path):
    """Compile a port through Python, with the oracle's inert runtime stub.

    The caller controls tracing, including import-time statements. Registration
    supports emitted dataclasses; all module state is restored on failure too.
    """
    sys.path.insert(0, str(ROOT / "src"))
    from revl import compile_files  # noqa: PLC0415

    emitted = python_reference.emit(
        compile_files([str(ROOT / "selfhost" / f"emit_{tier}.rvl")]))
    path = scratch / f"selfhost_emit_{tier}.py"
    path.write_text(emitted)
    name = f"selfhost_emit_{tier}"
    module = types.ModuleType(name)
    module.__file__ = str(path)
    stub = types.ModuleType("runtime")
    stub.__file__ = "<runtime-stub>"

    def stub_attr(attr):
        if attr.startswith("__"):
            raise AttributeError(attr)
        return lambda *a, **k: None

    stub.__getattr__ = stub_attr
    previous = {key: sys.modules[key] for key in ("runtime", name) if key in sys.modules}
    sys.modules["runtime"], sys.modules[name] = stub, module
    try:
        exec(compile(emitted, str(path), "exec"), module.__dict__)
        yield module, path
    finally:
        for key in ("runtime", name):
            if key in previous:
                sys.modules[key] = previous[key]
            else:
                sys.modules.pop(key, None)


def measure(frontend: bool = False) -> dict:
    """Run every tier's corpus through its reference emitter under coverage.

    One process and one coverage session for all six tiers: each reference is
    imported FRESH by path (as the oracles import it), so module-level
    statements are traced rather than counted missing for having run before the
    session started.
    """
    import coverage  # noqa: PLC0415 - optional at import time, required to measure

    targets = [str(ROOT / "backends" / package / "emit.py") for package in TIERS.values()]
    targets += [str(ROOT / "src" / "revl" / name) for name in FRONTEND]
    cov = coverage.Coverage(data_file=None, include=targets)
    cov.start()
    emitted_a_refusal: list[str] = []
    try:
        sys.path.insert(0, str(ROOT / "src"))
        from revl import compile_files  # noqa: PLC0415 - must be traced

        emitters = {}
        for tier, package in TIERS.items():
            spec = importlib.util.spec_from_file_location(
                f"line_coverage_reference_{tier}", ROOT / "backends" / package / "emit.py")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            emitters[tier] = module
        for tier in TIERS:
            for document in corpus_documents(tier):
                emitters[tier].emit(compile_files([str(document)]))
            # The refusal corpus. Every line the reference ran before raising is
            # reference logic, and it is the only way these lines run at all. A
            # document this tier does NOT refuse is collected and reported after
            # the session: see refusal_documents() for why that has to fail.
            for document in refusal_documents(tier):
                try:
                    emitters[tier].emit(compile_files([str(document)]))
                except Exception:  # noqa: BLE001 - the refusal IS the measured path
                    continue
                emitted_a_refusal.append(f"{tier}: {document.name}")
    finally:
        cov.stop()
    if emitted_a_refusal:
        raise SystemExit(
            "these documents are in tests/fixtures/emit_<tier>_refusals/ and the "
            "tier's reference EMITS them: " + ", ".join(sorted(emitted_a_refusal))
            + ". A document the reference emits is a byte-agreement case and "
            "belongs in the tier's CORPUS list, where the oracle holds its bytes. "
            "Left here it buys line coverage with no agreement assertion behind "
            "it, which is the certifying-less-than-it-appears shape item 429 "
            "exists to rule out.")

    result: dict[str, dict] = {}
    for tier, package in TIERS.items():
        result[tier] = _per_function(cov, ROOT / "backends" / package / "emit.py")
    if frontend:
        result["_frontend"] = {
            name: _per_function(cov, ROOT / "src" / "revl" / name) for name in FRONTEND
        }
    return result


def measure_full_tree() -> dict:
    """The SAME reference measurement, with the corpus replaced by THE WHOLE TREE.

    The question this answers is which fix the numbers indicate. If the curated
    corpus covers 47% of the reference emitters and every `.rvl` in the tree
    covers 85%, the cheap fix is to point the oracles at more inputs. If both
    are low, the gap has to be authored case by case. The two answers have very
    different costs, so measure rather than guess.

    Every document is compiled ONCE and fed to all six emitters. A document the
    frontend refuses, or an emitter refuses, still counts every line it executed
    before raising — a refusal path is reference logic too.
    """
    import coverage  # noqa: PLC0415

    targets = [str(ROOT / "backends" / package / "emit.py") for package in TIERS.values()]
    targets += [str(ROOT / "src" / "revl" / name) for name in FRONTEND]
    documents = sorted(p for p in ROOT.rglob("*.rvl") if ".git" not in p.parts)
    cov = coverage.Coverage(data_file=None, include=targets)
    cov.start()
    stats = {"documents": len(documents), "compiled": 0, "emitted": 0, "emit_refused": 0}
    try:
        sys.path.insert(0, str(ROOT / "src"))
        from revl import compile_files  # noqa: PLC0415

        emitters = {}
        for tier, package in TIERS.items():
            spec = importlib.util.spec_from_file_location(
                f"full_tree_reference_{tier}", ROOT / "backends" / package / "emit.py")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            emitters[tier] = module
        for document in documents:
            try:
                ir = compile_files([str(document)])
            except BaseException:  # noqa: BLE001 - a refusal is data here
                continue
            stats["compiled"] += 1
            for tier in TIERS:
                try:
                    emitters[tier].emit(ir)
                    stats["emitted"] += 1
                except BaseException:  # noqa: BLE001
                    stats["emit_refused"] += 1
    finally:
        cov.stop()

    result: dict[str, dict] = {"_stats": stats}
    for tier, package in TIERS.items():
        result[tier] = _per_function(cov, ROOT / "backends" / package / "emit.py")
    result["_frontend"] = {
        name: _per_function(cov, ROOT / "src" / "revl" / name) for name in FRONTEND
    }
    return result


def measure_selfhost() -> dict:
    """The SAME measurement, taken on the self-host side.

    `selfhost/emit_<tier>.rvl` is compiled by revl through the reference python
    backend into a python module — that is how its own oracle runs it — so the
    emitted module can be traced by `coverage.py` like any other python. The
    emitted `def <name>` keeps the `.rvl` `fn <name>`, so an uncovered statement
    attributes back to the self-host FUNCTION that produced it exactly.

    What is NOT recovered here is the `.rvl` LINE. `selfhost/*.rvl` carries no
    line provenance into its emitted output: `src/revl/lower.py` drops the
    parser's `.line` for all but three IR node kinds (`break`, `continue`,
    `hole`), so there is nothing to thread through the emitter. The counts below
    are therefore per-function counts of unexecuted EMITTED statements — the
    same unit as the reference ledger, one degree short of a source line. See
    the roadmap entry for what closing that last degree costs.
    """
    import coverage  # noqa: PLC0415

    sys.path.insert(0, str(ROOT / "src"))
    from revl import compile_files  # noqa: PLC0415

    reference = load_reference("py")
    result: dict[str, dict] = {}
    with tempfile.TemporaryDirectory(prefix="selfhost-coverage-") as temporary:
        scratch = Path(temporary)
        for tier in TIERS:
            source_rvl = ROOT / "selfhost" / f"emit_{tier}.rvl"
            module_path = scratch / f"selfhost_emit_{tier}.py"
            declared = set(re.findall(r"^\s*(?:pub\s+)?fn\s+(\w+)",
                                      source_rvl.read_text(), re.M))
            cov = coverage.Coverage(data_file=None, include=[str(module_path)])
            cov.start()
            try:
                with selfhost_module(tier, reference, scratch) as (module, _):
                    for document in corpus_documents(tier):
                        _entry(module, tier)(compile_files([str(document)]))
                    # The refusal corpus, driven the same way and NOT wrapped.
                    # The port's contract on a document the reference refuses is
                    # to answer it by name, not to crash: a traceback out of here
                    # is a finding, and burying it under an `except` would turn a
                    # port that falls over into a port with good coverage.
                    for document in refusal_documents(tier):
                        _entry(module, tier)(compile_files([str(document)]))
            finally:
                cov.stop()
            found = _per_function(cov, module_path)
            # Keep only names that are `.rvl` functions: the emitted module also
            # carries scaffolding and nested closures that no `.rvl` `fn`
            # declares, and attributing those to the port would be a lie.
            found["functions"] = {n: c for n, c in found["functions"].items()
                                  if n in declared}
            found["sizes"] = {n: c for n, c in found["sizes"].items() if n in declared}
            found["declared"] = len(declared)
            # The ledger population is declared .rvl functions only. Do not
            # headline generated scaffolding statements that cannot receive a
            # source-function residual or be triaged in the ledger.
            found["statements"] = sum(found["sizes"].values())
            found["uncovered"] = sum(found["functions"].values())
            found["never_entered"] = sorted(
                n for n, c in found["functions"].items()
                if c >= found["sizes"].get(n, c) - 1)
            result[tier] = found
    return result


# ------------------------------------------------------------------- ledger

# How the uncovered mass is grouped into written reasons. A function matches the
# FIRST pattern whose regex hits its qualified name; anything left over lands in
# UNTRIAGED, which is a statement about our knowledge, not about the code.
GROUPS: tuple[tuple[str, str], ...] = (
    (r"lifecycle|fault_test|_emit_tests|_test_|REVL_TESTS",
     "declared out of every self-host slice: in-file `test` / `fault_test` / "
     "`lifecycle test` emission."),
    (r"placement|realm|isolate|intercept|router|routed|routes|_spawn|instance",
     "declared out of every self-host slice: realm placements, routers, "
     "spawn/instances."),
    (r"bridge|marshal|serde|abi|_canonical",
     "declared out of every self-host slice: the bridge / marshalling / "
     "canonical-ABI surface."),
    (r"_v1|_v2|_stc|legacy",
     "the v1/v2 live-component path: a component routes to the older runtime, "
     "not through the v3 emitter the self-host mirrors."),
    (r"async|await|_colored",
     "declared out of every self-host slice: async coloring."),
    (r"stdlib|helper|preamble|_ftoa|_revl_div|checked_",
     "the demand-pulled helper preambles: emitted only for a document that "
     "reaches them, and the corpus reaches few."),
)


# --------------------------------------------------------------- the ledger
#
# In memory, per half and tier: `{"reasons": {id: text}, "functions": {name:
# {"uncovered": n, "budget": b, "reason": id}}}`, plus the format problems the
# reader found. On disk, `<half>/<tier>.jsonl`, one record per line.

def _tier_file(base: Path, half: str, tier: str) -> Path:
    return Path(base) / half / f"{tier}.jsonl"


def _load_tier(path: Path) -> tuple[dict | None, list[str]]:
    """One tier file, parsed. A record that is not one of the two shapes is a
    problem rather than an exception, so the gate fails closed and names it."""
    if not path.is_file():
        return None, []
    entry: dict = {"reasons": {}, "functions": {}}
    problems: list[str] = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        where = f"{path.parent.name}/{path.name}:{lineno}"
        if line.startswith(("<<<<<<<", "=======", ">>>>>>>", "|||||||")):
            problems.append(f"{where}: unresolved merge conflict marker")
            continue
        try:
            record = json.loads(line)
        except ValueError:
            problems.append(f"{where}: unreadable record")
            continue
        if (isinstance(record, list) and len(record) == 3
                and record[0] == "reason" and isinstance(record[1], str)
                and isinstance(record[2], str)):
            _, rid, text = record
            if rid in entry["reasons"]:
                problems.append(f"{where}: reason `{rid}` is declared twice")
            entry["reasons"][rid] = text
        elif (isinstance(record, list) and len(record) == 5
                and record[0] == "function" and isinstance(record[1], str)
                and isinstance(record[2], str)):
            _, rid, name, uncovered, budget = record
            if name in entry["functions"]:
                previous = entry["functions"][name]["reason"]
                problems.append(
                    f"{path.parent.name}/{path.stem}: `{name}` appears in "
                    f"multiple reasons ({previous}, {rid})")
                continue
            entry["functions"][name] = {"uncovered": uncovered,
                                        "budget": budget, "reason": rid}
        else:
            problems.append(f"{where}: not a reason or function record")
    return entry, problems


def _load_ledger(base: Path | None = None) -> dict:
    """`{half: {tier: entry}}`, and the reader's problems under `_problems`."""
    base = LEDGER if base is None else Path(base)
    ledger: dict = {"_problems": []}
    for half in HALVES:
        side = {}
        for tier in TIERS:
            entry, problems = _load_tier(_tier_file(base, half, tier))
            ledger["_problems"] += problems
            if entry is not None:
                side[tier] = entry
        if side:
            ledger[half] = side
    return ledger


def _record_key(record: list) -> tuple:
    return (record[1], 0 if record[0] == "reason" else 1,
            record[2] if record[0] == "function" else "")


def tier_text(entry: dict) -> str:
    """One tier's records as `--write` lays them out: sorted by reason id, a
    reason before its functions, functions by name, a blank line between."""
    records = [["reason", rid, text] for rid, text in entry["reasons"].items()]
    records += [["function", f["reason"], name, f["uncovered"], f["budget"]]
                for name, f in entry["functions"].items()]
    records.sort(key=_record_key)
    return "\n\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n"


def dump_ledger(ledger: dict, base: Path | None = None) -> None:
    """Write `{half: {tier: entry}}` in the on-disk layout."""
    base = LEDGER if base is None else Path(base)
    for half in HALVES:
        for tier, entry in ledger.get(half, {}).items():
            path = _tier_file(base, half, tier)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(tier_text(entry), encoding="utf-8")


def _flatten(entry: dict) -> dict[str, int]:
    """`{function: recorded uncovered count}` over the well-formed counts."""
    if not isinstance(entry, dict):
        return {}
    return {name: f["uncovered"] for name, f in entry.get("functions", {}).items()
            if type(f.get("uncovered")) is int and f["uncovered"] >= 0}


def _budget_problems(ledger: dict) -> list[str]:
    """Each function's recorded count against its budget, EXACTLY.

    WHY A SECOND COPY OF A NUMBER THE LEDGER ALREADY HOLDS. Because the count
    is regenerable and the budget is not. `--write` re-measures every count
    and rewrites them all; it does not touch a budget, and it is not supposed
    to. So recording a newly unreached statement takes two edits, and the
    second one is a single integer going UP in a diff a reviewer reads in one
    second. Before #1419, recording took `--write` plus a sentence, and a
    sentence is easy to write and hard to count.

      count ABOVE the budget: statements were recorded that the budget does
      not have. Reach them, or raise the number and say why in the same diff.

      count BELOW the budget: an improvement landed and left headroom. Lower
      the number. Headroom is the form in which a recorded improvement gets
      quietly spent again on the next unreached region, which is exactly how a
      ratchet stops being one.

    So the budget is monotone DOWN except where a human deliberately raises it,
    and the target it is shrinking toward is zero. It was one number per half
    and tier until issue #1768; it is one per function now, so it is at least
    as strict (one function's rise can no longer hide behind another's fall)
    and two pull requests that pay for different functions do not edit the
    same line.
    """
    problems: list[str] = []
    for half in HALVES:
        side = ledger.get(half)
        if not isinstance(side, dict):
            continue
        for tier in TIERS:
            entry = side.get(tier)
            if not isinstance(entry, dict):
                continue
            for name, f in sorted(entry.get("functions", {}).items()):
                mass, allowed = f.get("uncovered"), f.get("budget")
                if type(mass) is not int or mass < 0:
                    continue
                if type(allowed) is not int or allowed < 0:
                    problems.append(
                        f"{half}/{tier}: `{name}` has no budget, or it is not a "
                        f"count. It records {mass} today; write that number as "
                        f"its budget and the gate holds you to it.")
                elif mass > allowed:
                    problems.append(
                        f"{half}/{tier}: `{name}` records {mass} uncovered "
                        f"statement(s) and its budget allows {allowed}. Reach the "
                        f"{mass - allowed} extra statement(s) with a corpus or "
                        f"refusal document, or raise its budget to {mass} in this "
                        f"same diff and say in the commit message what bought "
                        f"the rise.")
                elif mass < allowed:
                    problems.append(
                        f"{half}/{tier}: `{name}` records {mass} uncovered "
                        f"statement(s) and its budget still allows {allowed}. "
                        f"Lower its budget to {mass}. Unspent budget is budget "
                        f"the next unreached region spends without anyone "
                        f"noticing, which is how a two-way ratchet becomes a "
                        f"one-way inventory.")
    return problems


_GENERIC_REASONS = (
    "NEVER ENTERED",
    "PARTIALLY EXERCISED",
    "NOT TRIAGED",
    "UNDECLARED GAP",
)


def _closure_problems(ledger: dict) -> list[str]:
    """Reject generic or structurally incomplete line-coverage baselines."""
    problems: list[str] = list(ledger.get("_problems", []))
    for half in HALVES:
        side = ledger.get(half)
        if not isinstance(side, dict):
            problems.append(f"{half}: missing line-coverage side")
            continue
        for tier in TIERS:
            entry = side.get(tier)
            if not isinstance(entry, dict):
                problems.append(f"{half}/{tier}: missing line-coverage tier")
                continue
            reasons = entry.get("reasons", {})
            used = {f["reason"] for f in entry.get("functions", {}).values()}
            for rid, reason in sorted(reasons.items()):
                if not reason.strip():
                    problems.append(f"{half}/{tier}: missing reason for `{rid}`")
                elif any(marker in reason.upper() for marker in _GENERIC_REASONS):
                    problems.append(f"{half}/{tier}: generic reason `{reason}`")
                if rid not in used:
                    problems.append(f"{half}/{tier}: reason `{rid}` has no functions")
            for rid in sorted(used - set(reasons)):
                problems.append(
                    f"{half}/{tier}: functions name reason `{rid}`, which is "
                    f"not declared. If that is the reason's text, "
                    f"`python3 tools/selfhost_line_coverage.py --write` files "
                    f"it under the reason's id, minting one if it is new")
    return problems


def _group_for(name: str, missing: int, size: int) -> str:
    for pattern, reason in GROUPS:
        if re.search(pattern, name, re.I):
            return reason
    # `def` and decorator lines execute at import even when nobody calls the
    # function, so "never entered" is size minus that header, not size.
    return NEVER_ENTERED if missing >= size - 1 else PARTIAL


# What each half is, in the failure message. `reference` is the python emitter
# the oracle treats as ground truth; `selfhost` is the port, measured through
# the python module it compiles to.
WHERE = {
    "reference": "backends/<tier>/emit.py",
    "selfhost": "selfhost/emit_<tier>.rvl (measured through its emitted python)",
}


def check(data: dict) -> list[str]:
    ledger = _load_ledger()
    problems = _closure_problems(ledger)
    problems += _budget_problems(ledger)
    if not isinstance(data, dict):
        return problems + ["survey data is not a map"]
    for half in ("reference", "selfhost"):
        recorded_half = ledger.get(half, {})
        if not isinstance(recorded_half, dict):
            recorded_half = {}
        for tier in TIERS:
            try:
                found = data[half][tier]["functions"]
            except (KeyError, TypeError):
                problems.append(f"{half}/{tier}: survey data is missing")
                continue
            if not isinstance(found, dict):
                problems.append(f"{half}/{tier}: survey functions are not a map")
                continue
            recorded = _flatten(recorded_half.get(tier, {}))
            raw_entry = recorded_half.get(tier)
            if isinstance(raw_entry, dict):
                for name, f in sorted(raw_entry.get("functions", {}).items()):
                    count = f.get("uncovered")
                    if type(count) is not int or count < 0:
                        problems.append(
                            f"{half}/{tier}: invalid uncovered count for `{name}`")
            for name in sorted(set(found) | set(recorded)):
                now, before = found.get(name, 0), recorded.get(name)
                if type(now) is not int or now < 0:
                    problems.append(f"{half}/{tier}: invalid measured count for `{name}`")
                    continue
                if before is None:
                    problems.append(
                        f"{half}/{tier}: `{name}` has {now} statement(s) that no "
                        f"corpus document executes, and is not in "
                        f"{LEDGER.name}/{half}/{tier}.jsonl. "
                        f"The byte-agreement oracle runs {WHERE[half]} and never "
                        f"runs these lines. THREE responses, in order of "
                        f"preference. (1) A corpus document that reaches them. "
                        f"(2) If they only run on a document this tier's "
                        f"reference REFUSES, no corpus document can ever reach "
                        f"them, because there are no reference bytes to agree with, so "
                        f"add the document to tests/fixtures/emit_{tier}_refusals/ "
                        f"instead, and assert the refusal's text in the tier "
                        f"oracle. (3) If they run on nothing at all, delete them. "
                        f"Recording the count is the LAST resort and costs a "
                        f"budget written by hand in the same diff.")
                elif now > before:
                    problems.append(
                        f"{half}/{tier}: `{name}` went from {before} to {now} "
                        f"uncovered statement(s). Logic arrived in a mirrored "
                        f"emitter that no corpus document reaches — exactly how "
                        f"the item-429(d) `Secret[T]` gap opened, and the oracle "
                        f"will stay green over it. Add the corpus case, or the "
                        f"refusal document if the reference refuses what reaches "
                        f"it (tests/fixtures/emit_{tier}_refusals/).")
                elif now < before:
                    problems.append(
                        f"{half}/{tier}: `{name}` is down to {now} uncovered "
                        f"statement(s) from {before}. Good news, and the ratchet "
                        f"only holds if it is recorded: run "
                        f"`python3 tools/selfhost_line_coverage.py --write`.")
    return problems


def _reason_id(text: str, taken: set[str]) -> str:
    """A stable id for a reason `--write` has to file a function under: the
    first words of its text, made unique within the tier."""
    words = re.findall(r"[a-z0-9]+", text.lower())
    stem = "-".join(words[:8])[:60].strip("-") or "reason"
    rid, n = stem, 2
    while rid in taken:
        rid, n = f"{stem}-{n}", n + 1
    return rid


def write_ledger(data: dict) -> None:
    """Rewrite the per-function counts. Budgets and reasons are NOT rewritten.

    That omission is the point. `--write` is the fastest green, it is always
    available, and it will stay both of those things; what it cannot do is
    finish the job. It leaves every budget exactly where it was, so a `--write`
    that records a new region leaves `--check` RED with a message naming the
    function and the number, and the only way out is one integer edited by
    hand. Issue #1419 asked for a mechanism that survives `--write`. This is
    it: not a harder `--write`, a `--write` that is no longer sufficient.
    """
    previous = _load_ledger()
    out: dict = {}
    for half in HALVES:
        out[half] = {}
        for tier in TIERS:
            found = data[half][tier]
            # Counts always come from the fresh measurement; the GROUPING and
            # the budgets are preserved, so a reason someone wrote by hand
            # survives a regeneration and only the counts move. Functions that
            # are no longer uncovered fall out of their reason on their own.
            before = previous.get(half, {}).get(tier, {"reasons": {}, "functions": {}})
            reasons = dict(before["reasons"])
            by_text = {text: rid for rid, text in reasons.items()}

            def reason_for(text: str) -> str:
                """The id `text` is filed under: an existing reason's id when
                the text is that reason's, else a new id with the text
                recorded once."""
                rid = by_text.get(text)
                if rid is None:
                    rid = _reason_id(text, set(reasons))
                    reasons[rid] = text
                    by_text[text] = rid
                return rid

            functions = {}
            for name, count in found["functions"].items():
                old = before["functions"].get(name)
                if old is not None:
                    # A record whose reason field is not a declared id holds
                    # the reason's TEXT, the way the single-file ledger keyed
                    # it and the way a record carried across from that layout
                    # arrives. Resolve it, so the budget is the only hand edit.
                    rid = (old["reason"] if old["reason"] in reasons
                           else reason_for(old["reason"]))
                    functions[name] = {"uncovered": count, "budget": old["budget"],
                                       "reason": rid}
                    continue
                text = _group_for(name, count, found["sizes"].get(name, count))
                functions[name] = {"uncovered": count, "budget": None,
                                   "reason": reason_for(text)}
            used = {f["reason"] for f in functions.values()}
            out[half][tier] = {
                "reasons": {rid: t for rid, t in reasons.items() if rid in used},
                "functions": functions,
            }
    dump_ledger(out)


def _table(title: str, data: dict, docs: dict[str, int]) -> tuple[int, int]:
    print(f"\n{title}")
    print(f"  {'tier':6s} {'docs':>5s} {'statements':>11s} {'uncovered':>10s} {'covered':>8s}")
    statements = uncovered = 0
    for tier in TIERS:
        found = data[tier]
        statements += found["statements"]
        uncovered += found["uncovered"]
        share = 100.0 * (1 - found["uncovered"] / found["statements"])
        print(f"  {tier:6s} {docs.get(tier, 0):5d} {found['statements']:11d} "
              f"{found['uncovered']:10d} {share:7.1f}%")
    share = 100.0 * (1 - uncovered / statements)
    print(f"  {'TOTAL':6s} {'':5s} {statements:11d} {uncovered:10d} {share:7.1f}%")
    return statements, uncovered


def report(data: dict, full_tree: dict | None = None) -> None:
    docs = {tier: len(corpus_documents(tier)) for tier in TIERS}
    _table("REFERENCE (backends/<tier>/emit.py) under the oracle's own corpus:",
           data["reference"], docs)
    _table("SELF-HOST (selfhost/emit_<tier>.rvl, through its emitted python):",
           data["selfhost"], docs)

    print("\n  self-host functions the corpus NEVER ENTERS "
          "(the oracle asserts byte agreement without running them):")
    for tier in TIERS:
        found = data["selfhost"][tier]
        print(f"  {tier:6s} {len(found['never_entered']):3d} of {found['declared']:3d}"
              f"   {', '.join(found['never_entered'][:6])}"
              f"{' ...' if len(found['never_entered']) > 6 else ''}")

    if "_frontend" in data["reference"]:
        print("\n  shared frontend (mirrored by selfhost/lower.rvl and checker.rvl,")
        print("  measured under the EMIT corpora, which is not their own corpus):")
        for name, found in data["reference"]["_frontend"].items():
            share = 100.0 * (1 - found["uncovered"] / found["statements"])
            print(f"    src/revl/{name:14s} {found['statements']:6d} statements "
                  f"{found['uncovered']:6d} uncovered {share:6.1f}% covered")

    if full_tree:
        stats = full_tree["_stats"]
        _table(f"REFERENCE under THE WHOLE TREE instead "
               f"({stats['compiled']} of {stats['documents']} `.rvl` documents "
               f"compile):", full_tree, {})
        print("\n  The difference between those two tables is the answer to "
              "\"author cases or\n  point the oracle at more inputs?\" — see the "
              "roadmap entry for the verdict.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="the gate")
    parser.add_argument("--write", action="store_true", help="record the current state")
    parser.add_argument("--full-tree", action="store_true",
                        help="also measure the reference against every `.rvl` in the "
                             "tree, to price 'more inputs' against 'author cases'")
    args = parser.parse_args(argv)

    reporting = not (args.check or args.write)
    data = {"reference": measure(frontend=reporting), "selfhost": measure_selfhost()}
    if args.write:
        write_ledger(data)
        print(f"wrote {LEDGER.relative_to(ROOT)}")
        for problem in _budget_problems(_load_ledger()):
            print(f"STILL RED {problem}")
        print("no budget was touched; run --check.")
        return 0
    if args.check:
        problems = check(data)
        for problem in problems:
            print(f"FAIL {problem}")
        if problems:
            print(f"\n{len(problems)} line-coverage problem(s).")
            return 1
        print("reference and self-host line coverage match the recorded state.")
        return 0
    report(data, measure_full_tree() if args.full_tree else None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
