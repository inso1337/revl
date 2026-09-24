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
that got given. `_budget` in the ledger is the bound: a per-half, per-tier count
that `--write` does not write and that the gate holds the recorded mass to
EXACTLY, so recording costs an integer raised by hand in the diff and an
improvement permanently lowers the ceiling instead of leaving headroom. The
target it shrinks toward is zero.

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
LEDGER = ROOT / "tests" / "fixtures" / "selfhost_uncovered_lines.json"


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

    Measured on `bb22be665`, driving the one rust document here moved three
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


def _load_ledger() -> dict:
    raw = json.loads(LEDGER.read_text()) if LEDGER.exists() else {}
    return {k: v for k, v in raw.items() if not k.startswith("_")}


def _load_budget() -> dict:
    """`_budget`: what the ledger is allowed to hold, per half and tier.

    WHY A SECOND COPY OF A NUMBER THE LEDGER ALREADY IMPLIES. Because the ledger
    is regenerable and this is not. `--write` re-measures every per-function
    count and rewrites them all; it does not touch `_budget`, and it is not
    supposed to. So recording a newly unreached statement takes two edits in two
    places, and the second one is a single integer going UP in a diff a reviewer
    reads in one second. Before this, recording took `--write` plus a sentence,
    and a sentence is easy to write and hard to count.

    The gate holds the budget to the mass EXACTLY, in both directions:

      mass ABOVE the budget: statements were recorded that the budget does not
      have. Reach them, or raise the number and say why in the same diff.

      mass BELOW the budget: an improvement landed and left headroom. Lower the
      number. Headroom is the form in which a recorded improvement gets quietly
      spent again on the next unreached region, which is exactly how a ratchet
      stops being one.

    So the budget is monotone DOWN except where a human deliberately raises it,
    and the target it is shrinking toward is zero: every statement here is one
    that some document should reach, or that should not exist. The 2026-09-24
    value is 9086. It has been as high as 10321 (`1d0d8748f`, 2026-09-05) and the
    five triage commits that followed took it to 8087, so a falling direction is
    something this ledger has done and not an aspiration.
    """
    raw = json.loads(LEDGER.read_text()) if LEDGER.exists() else {}
    budget = raw.get("_budget")
    return budget if isinstance(budget, dict) else {}


def _budget_problems(ledger: dict, budget: dict) -> list[str]:
    problems: list[str] = []
    for half in ("reference", "selfhost"):
        side = ledger.get(half)
        declared = budget.get(half)
        if not isinstance(side, dict):
            continue
        if not isinstance(declared, dict):
            problems.append(
                f"{half}: no `_budget` declared in {LEDGER.name}. Every recorded "
                f"statement has to come out of a number someone raised on "
                f"purpose, or recording is free and the ledger is an inventory.")
            continue
        for tier in TIERS:
            entry = side.get(tier)
            if not isinstance(entry, dict):
                continue
            mass = sum(_flatten(entry).values())
            allowed = declared.get(tier)
            if type(allowed) is not int or allowed < 0:
                problems.append(
                    f"{half}/{tier}: `_budget` is missing or not a count. It is "
                    f"{mass} today; write that number and the gate holds you to it.")
            elif mass > allowed:
                problems.append(
                    f"{half}/{tier}: the ledger records {mass} uncovered "
                    f"statement(s) and `_budget` allows {allowed}. Reach the "
                    f"{mass - allowed} extra statement(s) with a corpus or refusal "
                    f"document, or raise `_budget.{half}.{tier}` to {mass} in this "
                    f"same diff and say in the commit message what bought the rise.")
            elif mass < allowed:
                problems.append(
                    f"{half}/{tier}: the ledger records {mass} uncovered "
                    f"statement(s) and `_budget` still allows {allowed}. Lower "
                    f"`_budget.{half}.{tier}` to {mass}. Unspent budget is budget "
                    f"the next unreached region spends without anyone noticing, "
                    f"which is how a two-way ratchet becomes a one-way inventory.")
    return problems


def _flatten(entry: dict) -> dict[str, int]:
    if not isinstance(entry, dict) or not isinstance(entry.get("uncovered"), dict):
        return {}
    flat: dict[str, int] = {}
    for functions in entry.get("uncovered", {}).values():
        if isinstance(functions, dict):
            for name, count in functions.items():
                if isinstance(name, str) and type(count) is int and count >= 0:
                    flat[name] = count
    return flat


_GENERIC_REASONS = (
    "NEVER ENTERED",
    "PARTIALLY EXERCISED",
    "NOT TRIAGED",
    "UNDECLARED GAP",
)


def _closure_problems(ledger: dict) -> list[str]:
    """Reject generic or structurally incomplete line-coverage baselines."""
    problems: list[str] = []
    for half in ("reference", "selfhost"):
        side = ledger.get(half)
        if not isinstance(side, dict):
            problems.append(f"{half}: missing line-coverage side")
            continue
        for tier in TIERS:
            entry = side.get(tier)
            if not isinstance(entry, dict):
                problems.append(f"{half}/{tier}: missing line-coverage tier")
                continue
            reasons = entry.get("uncovered")
            if not isinstance(reasons, dict):
                problems.append(f"{half}/{tier}: missing uncovered reason map")
                continue
            seen: dict[str, str] = {}
            for reason, functions in reasons.items():
                if not isinstance(reason, str) or not reason.strip():
                    problems.append(f"{half}/{tier}: missing reason")
                elif any(marker in reason.upper() for marker in _GENERIC_REASONS):
                    problems.append(f"{half}/{tier}: generic reason `{reason}`")
                if not isinstance(functions, dict) or not functions:
                    problems.append(f"{half}/{tier}: reason has no functions")
                    continue
                for function in functions:
                    previous = seen.get(function)
                    if previous is not None:
                        problems.append(
                            f"{half}/{tier}: `{function}` appears in multiple "
                            f"reasons ({previous}, {reason})")
                    else:
                        seen[function] = reason
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
    problems += _budget_problems(ledger, _load_budget())
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
                raw_reasons = raw_entry.get("uncovered")
                if isinstance(raw_reasons, dict):
                    for functions in raw_reasons.values():
                        if isinstance(functions, dict):
                            for name, count in functions.items():
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
                        f"corpus document executes, and is not in {LEDGER.name}. "
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
                        f"`_budget` raise in the same diff.")
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


def write_ledger(data: dict) -> None:
    """Rewrite the per-function counts. `_budget` and `_about` are NOT rewritten.

    That omission is the point. `--write` is the fastest green, it is always
    available, and it will stay both of those things; what it cannot do is
    finish the job. It leaves `_budget` exactly where it was, so a `--write` that
    records a new region leaves `--check` RED with a message naming the tier and
    the number, and the only way out is one integer edited by hand. Issue #1419
    asked for a mechanism that survives `--write`. This is it: not a harder
    `--write`, a `--write` that is no longer sufficient.
    """
    raw = json.loads(LEDGER.read_text()) if LEDGER.exists() else {}
    for half in ("reference", "selfhost"):
        out: dict[str, dict] = {}
        for tier in TIERS:
            found = data[half][tier]
            # Counts always come from the fresh measurement; the GROUPING is
            # preserved, so a reason someone wrote by hand survives a
            # regeneration and only the numbers move. Functions that are no
            # longer uncovered fall out of their bucket on their own.
            previous = raw.get(half, {}).get(tier, {}).get("uncovered", {})
            grouped: dict[str, dict[str, int]] = {}
            claimed: set[str] = set()
            for reason, names in previous.items():
                kept = {n: found["functions"][n] for n in names
                        if n in found["functions"]}
                if kept:
                    grouped[reason] = kept
                    claimed |= set(kept)
            for name, count in found["functions"].items():
                if name in claimed:
                    continue
                reason = _group_for(name, count, found["sizes"].get(name, count))
                grouped.setdefault(reason, {})[name] = count
            out[tier] = {
                "statements": found["statements"],
                "uncovered_statements": sum(found["functions"].values()),
                "uncovered": {reason: dict(sorted(entries.items()))
                              for reason, entries in sorted(grouped.items())},
            }
        raw[half] = out
    LEDGER.write_text(json.dumps(raw, indent=2, sort_keys=True) + "\n")


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
        for problem in _budget_problems(_load_ledger(), _load_budget()):
            print(f"STILL RED {problem}")
        print("`_budget` was not touched; run --check.")
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
