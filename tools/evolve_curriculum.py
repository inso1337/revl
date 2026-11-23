#!/usr/bin/env python3
"""Derive the evolution curriculum from repository artifacts, with a computed tier.

WHY THIS EXISTS (roadmap item 535, issue #1205). A self-evolution loop needs a
stream of tasks. A task stream written by the loop's operators is one the loop
can overfit and one whose difficulty is asserted rather than measured. Two
properties make a curriculum auditable, and both are checkable here:

  * every task names the repository artifact it was derived from, so a task is
    REGENERATED from the tree and audited rather than invented;
  * every task carries a difficulty tier, and each tier's population is
    COMPUTED from the tree rather than declared, so "the engine is at rung 3"
    is a measurement of the corpus and not a claim about the engine.

Nothing in this tree assigned a difficulty tier to anything before this module.
The tier is therefore the interesting half, and the rule below is deliberately
NOT a table of "file X is hard" that somebody maintains by hand -- a hand
maintained table is exactly the asserted-difficulty problem this exists to
remove. What is measured is a triple, read off the VERIFYING MECHANISM that
decides whether the task is done:

  impls    how many independently maintained implementations of revl semantics
           that mechanism makes agree. The tree maintains four: the `reference`
           (src/revl/** and backends/*/emit.py), the `selfhost` port
           (selfhost/*.rvl), the `native` gate crate (crates/revl-gate), and
           the `formal` model (formal/RevL/**).
  breadth  how many top-level repository directories the mechanism's corpus
           spans. One means a curated fixture directory the task author can
           read in full; more than one means the mechanism re-decides the task
           over programs spread across the tree, which the author does not
           choose and cannot enumerate cheaply.
  proved   the mechanism has no finite corpus at all because it quantifies over
           every program (formal/). Nothing is sampled, so nothing can be
           tuned to the sample.

and the ladder over that triple is five lines (`tier`), each line carrying the
review's own words for the rung it produces. See
docs/design/533-evolution-curriculum.md for the argument, the measured
populations, and the cases the signal gets wrong.

SOURCES. The review names eight. This module reads seven of them; the rest are
staged in the design doc's slice plan. Each adapter below states the artifact
it reads, and every task it emits carries that artifact by path, so a reader
can go and look.
SOURCES. The review names eight. This module reads five of them; the rest are
staged in the design doc's slice plan. It also reads one the review does not
name, mutation testing, because a suite nobody has measured for sensitivity is
a scoring set of unknown value. Each adapter below states the artifact it
reads, and every task it emits carries that artifact by path, so a reader can
go and look.

USAGE
    python3 tools/evolve_curriculum.py              # human summary
    python3 tools/evolve_curriculum.py --json out.json
    python3 tools/evolve_curriculum.py --check      # the gate, see `check`
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# The four independently maintained implementations of revl semantics in this
# tree. `impls` below is always a subset of these, never a free-form string.
REFERENCE = "reference"
SELFHOST = "selfhost"
NATIVE = "native"
FORMAL = "formal"

TIERS = ("easy", "medium", "hard", "expert")


# --------------------------------------------------------------------------- #
# The measured signal and the ladder over it.                                   #
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Signal:
    """What the verifying mechanism makes agree, over how much of the tree."""

    impls: tuple[str, ...]
    breadth: int
    proved: bool = False

    def as_dict(self) -> dict:
        return {"impls": list(self.impls), "breadth": self.breadth,
                "proved": self.proved}


def tier(signal: Signal) -> str:
    """The rung, as a total function of the measured signal.

    Each line names the review's own row for the rung it returns. This is the
    only place a rung is decided; no adapter assigns one.
    """
    if signal.proved:
        # "formal theorem extensions": the obligation quantifies over every
        # program, so there is no corpus to tune against.
        return "expert"
    if len(signal.impls) >= 3:
        # "reference/native admission agreement": three engines, one verdict.
        return "expert"
    if len(signal.impls) <= 1:
        # "formatter changes, isolated diagnostics": one implementation owns
        # the answer, so nothing else has to be taught the same thing.
        return "easy"
    if signal.breadth > 1:
        # "self-host parity, cross-tier lowering": two engines agreeing over
        # programs spread across the tree rather than one curated directory.
        return "hard"
    # "IR fields, a single-backend change, stdlib": two engines over one
    # curated corpus.
    return "medium"


def breadth_of(paths) -> int:
    """Top-level repository directories a corpus spans."""
    tops = set()
    for p in paths:
        rel = Path(p)
        if rel.is_absolute():
            rel = rel.relative_to(ROOT)
        parts = rel.parts
        if not parts:
            continue
        # `tests/fixtures/emit_py_corpus` and `tests/test_x.py` are the same
        # top-level directory; the census spanning `examples` AND `stdlib` AND
        # `backends` is what breadth is measuring.
        tops.add(parts[0])
    return len(tops)


# --------------------------------------------------------------------------- #
# Tasks.                                                                        #
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Task:
    task_id: str
    source: str          # which of the review's eight sources this came from
    artifact: str        # repo-relative path the task was DERIVED from
    locator: str         # where inside the artifact (a line, a key, a name)
    gap: str             # one line: what the artifact shows is missing
    subject: str         # the change site the task implies
    mechanism: str       # the named mechanism that decides the task is done
    verify: str          # the command that mechanism runs
    signal: Signal
    tier: str = field(default="")

    def as_dict(self) -> dict:
        return {
            "id": self.task_id, "source": self.source, "artifact": self.artifact,
            "locator": self.locator, "gap": self.gap, "subject": self.subject,
            "mechanism": self.mechanism, "verify": self.verify,
            "tier": self.tier, "signal": self.signal.as_dict(),
        }


def _task(**kw) -> Task:
    t = Task(**kw)
    return Task(**{**kw, "tier": tier(t.signal)})


@dataclass(frozen=True)
class Derivation:
    """What one source measured at HEAD: the rungs it reaches, and what it found.

    The two halves are measured separately and that separation is the point
    (issue #1410). `signals` is every signal shape this source stamps, read off
    the artifacts it derives from and computed whether or not the source finds
    a single instance; `tasks` is the instances. So a source that is READING
    ITS ARTIFACTS AND FINDING NOTHING is distinguishable from one that could
    not read them at all, and an empty rung no longer looks like a broken
    generator. A source that raised carries `error` and, necessarily, no
    signal: it measured nothing, so it reaches nothing.
    """

    source: str
    signals: tuple[Signal, ...] = ()
    tasks: tuple[Task, ...] = ()
    error: str = ""

    def rungs(self) -> set[str]:
        """The rungs this source can stamp at HEAD, empty population or not."""
        return {tier(s) for s in self.signals}


# --------------------------------------------------------------------------- #
# Reading the declaring artifacts (no table restated here).                     #
# --------------------------------------------------------------------------- #
def _load(name: str, rel: str):
    import importlib.util
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _dict_keys(path: Path, name: str) -> list[str]:
    """Keys of a module-level dict literal, without importing the module."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
        if name in targets and isinstance(node.value, ast.Dict):
            return [k.value for k in node.value.keys
                    if isinstance(k, ast.Constant) and isinstance(k.value, str)]
    raise KeyError(f"{name} is not a module-level dict literal in {path}")


def _census_corpus() -> list[str]:
    """Every document the census re-decides, read from the census's own dirs."""
    census = _load("_ec_census", "tools/gate_reference_census.py")
    out: list[str] = []
    for sub in census.CORPUS_DIRS:
        base = ROOT / sub
        if not base.is_dir():
            continue
        for p in base.rglob("*.rvl"):
            if any(part in census._SKIP_DIRS for part in p.parts):
                continue
            out.append(str(p.relative_to(ROOT)))
    return sorted(out)


# --------------------------------------------------------------------------- #
# Adapter 1 -- known gaps: tools/oracle_construct_reach.py                       #
# --------------------------------------------------------------------------- #
# The report names, per differential oracle, the reference dispatches the
# oracle's corpus never reaches. An unreached dispatch is a construct two
# implementations have never been made to agree about on any document, which is
# precisely a task: write the document.
#
# We consume `survey()` rather than the CLI text so that a concurrent change to
# the tool's printing or its ledger (issue #1203) does not move this adapter.
_REACH_SUBJECT = {
    "lower_ir": ("src/revl/lower.py", "selfhost/lower.rvl",
                 "tests/test_selfhost_lower_ir.py"),
}


def adapter_construct_reach() -> Derivation:
    reach = _load("_ec_reach", "tools/oracle_construct_reach.py")
    coverage = _load("_ec_coverage", "tools/selfhost_coverage.py")
    report = reach.survey()
    tasks: list[Task] = []
    signals: list[Signal] = []
    for oracle in sorted(report):
        unreached = report[oracle]["unreached"]
        if oracle.startswith("emit_"):
            tier_key = oracle[len("emit_"):]
            if tier_key not in coverage.TIERS:
                continue
            backend = coverage.TIERS[tier_key][0]
            subject = f"backends/{backend}/emit.py"
            port = f"selfhost/{oracle}.rvl"
            test = f"tests/test_selfhost_{oracle}.py"
            corpus = [str(p.relative_to(ROOT))
                      for p in coverage.corpus_documents(tier_key)]
        elif oracle in _REACH_SUBJECT:
            subject, port, test = _REACH_SUBJECT[oracle]
            corpus = [str(p.relative_to(ROOT))
                      for p in coverage.corpus_documents("py")]
        else:
            # `compile` and `gate_census` have no per-tier byte-agreement
            # oracle behind them; the design doc's slice 2 owns them.
            continue
        if not (ROOT / subject).is_file() or not (ROOT / port).is_file():
            continue
        # Measured before the instances are looked at: this oracle's corpus is
        # in the tree whether or not it currently leaves a dispatch unreached.
        signal = Signal(impls=(REFERENCE, SELFHOST), breadth=breadth_of(corpus))
        signals.append(signal)
        for construct in unreached:
            tasks.append(_task(
                task_id=f"reach/{oracle}/{construct}",
                source="known gaps",
                artifact="tools/oracle_construct_reach.py",
                locator=f"{oracle}.unreached[{construct!r}]",
                gap=(f"no document in the {oracle} corpus reaches the reference "
                     f"dispatch {construct} in {subject}, so {port} has never "
                     f"been made to agree about it"),
                subject=f"{subject} + {port} (via a new corpus document)",
                mechanism=test,
                verify=("python3 tools/oracle_construct_reach.py --json - "
                        f"| no longer lists {construct} under {oracle}.unreached; "
                        f"python3 -m pytest {test}"),
                signal=signal,
            ))
    return Derivation("construct-reach", tuple(dict.fromkeys(signals)),
                      tuple(tasks))


# --------------------------------------------------------------------------- #
# Adapter 2 -- known historical bugs: the census false-admit buckets             #
# --------------------------------------------------------------------------- #
# A `false-admit/<tag>` entry is a program the reference refuses and the
# self-host admission engine does not object to. It is the strongest kind of
# derived task available here: the defect is recorded, the program exists, and
# the mechanism that decides the fix already runs in both directions.
CENSUS_BASELINE = "tools/gate_reference_census_baseline.json"


def adapter_census_bypass(baseline_path=None) -> Derivation:
    census = _load("_ec_census", "tools/gate_reference_census.py")
    corpus = _census_corpus()
    # The two shapes this source stamps, read off the census's own bucket
    # vocabulary and the corpus it re-decides, BEFORE the recorded entries are
    # looked at. Both are properties of the tree, not of any open defect: they
    # are what this source would put on an entry if it had one. An empty
    # baseline therefore still says which rungs this source reaches, which is
    # what keeps `false-admit` going to zero (PR #1396, PR #1404) from reading
    # like a generator that stopped working.
    engines = {
        census.HARD: ((REFERENCE, SELFHOST),
                      "selfhost/lower.rvl (the self-host admission engine)"),
        # `false-admission` is the native crate issuing an admission the
        # reference refuses: three engines, and NEVER_BASELINED, so a member
        # here is a live defect rather than a recorded one. It is declared
        # here because it is the shape this source stamps on such an entry,
        # not as a claim that one can be recorded: `--check` refuses to write
        # it, so this rung is fed by formal-obligation in practice.
        census.ADMISSION: ((REFERENCE, SELFHOST, NATIVE),
                           "crates/revl-gate (the native admission surface)"),
    }
    signals = {head: Signal(impls=impls, breadth=breadth_of(corpus))
               for head, (impls, _) in engines.items()}
    baseline = json.loads(
        (Path(baseline_path) if baseline_path
         else ROOT / CENSUS_BASELINE).read_text(encoding="utf-8"))
    tasks: list[Task] = []
    for bucket, cases in sorted(baseline.get("buckets", {}).items()):
        head = bucket.split("/")[0]
        if head not in engines:
            continue
        engine = engines[head][1]
        signal = signals[head]
        for case in sorted(cases):
            tasks.append(_task(
                task_id=f"census/{bucket}/{case}",
                source="known historical bugs",
                artifact=case,
                locator=f"tools/gate_reference_census_baseline.json buckets[{bucket!r}]",
                gap=(f"the reference refuses {case} with {bucket.split('/')[-1]} "
                     f"and {engine} raises no objection to it"),
                subject=engine,
                mechanism="tools/gate_reference_census.py --check",
                verify=("PYTHONPATH=src python3 tools/gate_reference_census.py "
                        f"--check  # {bucket} no longer contains {case}, and "
                        "no bucket gained a member"),
                signal=signal,
            ))
    return Derivation("census-bypass", tuple(signals.values()), tuple(tasks))


# --------------------------------------------------------------------------- #
# The refusal codes the reference actually stamps.                              #
# --------------------------------------------------------------------------- #
# Shared by adapters 3 and 4. "The reference enforces code C" is derived from
# the raise sites themselves, never from a list kept here:
#
#   * `RevlError(..., code="C")` -- the code the refusal carries, read off the
#     constructor call by AST so that a `code=` keyword on some unrelated call
#     cannot be mistaken for one (an earlier, looser scan picked up `route=`,
#     `session=` and `method=` in src/revl/mcp/http_face.py as refusal codes);
#   * a `(C)` guarantee tag inside a string literal that is part of a `raise`
#     statement, which is the convention src/revl/diagnostics.py's own `_TAG`
#     reads back out of the message.
#
# Both are deliberately narrow. A code enforced only in prose, in a docstring
# or in a comment is NOT counted, because a task derived from prose is the
# invented task this whole module exists to exclude.
_RAISE_TAG = re.compile(r"\((G[1-9]|A[1-9]|R[1-5]|T[1-9])\)")


def _callee(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def enforced_codes() -> dict[str, tuple[str, int]]:
    """`code -> (file, line)` of the first site the reference refuses with it."""
    diag = ROOT / "src/revl/diagnostics.py"
    sites: dict[str, tuple[str, int]] = {}

    def record(code: str, rel: str, lineno: int) -> None:
        if code not in sites:
            sites[code] = (rel, lineno)

    for path in sorted((ROOT / "src/revl").rglob("*.py")):
        if path == diag:
            continue
        rel = str(path.relative_to(ROOT))
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:      # a fixture that is not importable python
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _callee(node).endswith("RevlError"):
                for kw in node.keywords:
                    if (kw.arg == "code" and isinstance(kw.value, ast.Constant)
                            and isinstance(kw.value.value, str)):
                        record(kw.value.value, rel, node.lineno)
            if isinstance(node, ast.Raise):
                for sub in ast.walk(node):
                    if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                        for match in _RAISE_TAG.finditer(sub.value):
                            record(match.group(1), rel, node.lineno)
    return sites


# --------------------------------------------------------------------------- #
# Adapter 3 -- the tree's own refusals: a stamped code with no explanation       #
# --------------------------------------------------------------------------- #
# src/revl/diagnostics.py is the agent-facing projection of a refusal: a code,
# the guarantee it enforces, and the fix. `explain(code)` answers
# `{"ok": False, "message": "no diagnostic code ..."}` for anything outside
# GUARANTEES, and `classify` attaches neither `guarantee` nor `fix`. So a code
# the reference STAMPS that GUARANTEES does not hold is a refusal an agent
# receives and cannot look up.
#
# One implementation owns the answer: no port, no crate and no proof has to be
# taught anything, which is the easy rung and the review's own example for it
# ("isolated diagnostics").
def adapter_unexplained_refusal() -> Derivation:
    explained = set(_dict_keys(ROOT / "src/revl/diagnostics.py", "GUARANTEES"))
    signal = Signal(impls=(REFERENCE,), breadth=breadth_of(["src/revl"]))
    tasks: list[Task] = []
    for code, (where, lineno) in sorted(enforced_codes().items()):
        if code in explained:
            continue
        tasks.append(_task(
            task_id=f"diagnostic/{code}",
            source="the tree's own programs",
            artifact=where,
            locator=f"line {lineno}",
            gap=(f"the reference stamps refusals with the code {code!r} and "
                 "src/revl/diagnostics.py's GUARANTEES table does not hold it, "
                 f"so `revl explain {code}` answers `no diagnostic code` and the "
                 "structured diagnostic carries no guarantee and no fix"),
            subject="src/revl/diagnostics.py",
            mechanism="revl.diagnostics.explain",
            verify=("python3 -c \"from revl.diagnostics import explain; "
                    f"assert explain({code!r})['ok']\"; "
                    "python3 -m pytest tests/test_diagnostics.py"),
            signal=signal,
        ))
    return Derivation("unexplained-refusal", (signal,), tuple(tasks))


# --------------------------------------------------------------------------- #
# Adapter 4 -- formal obligations: a guarantee the model does not name           #
# --------------------------------------------------------------------------- #
# formal/ proves statements about the language rather than checking them on a
# corpus, so it has no corpus to overfit and `proved` is what the tier reads.
# A guarantee the reference enforces, that diagnostics.py describes as a
# guarantee, and that no .lean file in the tree so much as NAMES, is an
# obligation with no model. Naming is a deliberately weak test in the direction
# that matters: a code mentioned anywhere in any .lean file is excluded, so the
# adapter under-reports rather than claiming a proof is missing when one exists
# (G-SECRET and G-SECRET-FLOW are excluded this way -- formal/RevL/Lemmas/
# TaintLemmas.lean names them, though no Theorems/ file does).
_CODE_WORD = "(?<![A-Za-z0-9_-]){}(?![A-Za-z0-9_-])"


def adapter_formal_obligation() -> Derivation:
    explained = set(_dict_keys(ROOT / "src/revl/diagnostics.py", "GUARANTEES"))
    lean = [p.read_text(encoding="utf-8")
            for p in sorted((ROOT / "formal").rglob("*.lean"))]
    signal = Signal(impls=(REFERENCE, FORMAL), breadth=0, proved=True)
    tasks: list[Task] = []
    for code, (where, lineno) in sorted(enforced_codes().items()):
        if code not in explained:
            continue
        word = re.compile(_CODE_WORD.format(re.escape(code)))
        if any(word.search(text) for text in lean):
            continue
        tasks.append(_task(
            task_id=f"formal/{code}",
            source="formal obligations",
            artifact=where,
            locator=f"line {lineno}",
            gap=(f"the reference enforces {code}, src/revl/diagnostics.py calls "
                 "it a guarantee, and no .lean file under formal/ names it: the "
                 "guarantee has a checker and no model"),
            subject="formal/RevL/Theorems/",
            mechanism="formal/scripts/run_gate.sh",
            verify=(f"grep -rl {code} formal --include='*.lean'  # non-empty; "
                    "then sh formal/scripts/run_gate.sh"),
            signal=signal,
        ))
    return Derivation("formal-obligation", (signal,), tuple(tasks))


# --------------------------------------------------------------------------- #
# Adapter 5 -- cross-tier differential probes: the rejection corpus vs the census
# --------------------------------------------------------------------------- #
# The lower oracle's probes (tests/test_selfhost_lower.py's REJECTED_PROGRAMS)
# are curated to be IN SLICE: every row there is a program whose reference
# refusal selfhost/lower.rvl decides with the same tag and message. The census
# records the other half, the programs the reference refuses and the port raises
# NO OBJECTION to: `no-objection-out-of-slice`. Those are the differential
# probes the lower oracle cannot carry yet, and teaching the port the family is
# the task.
#
# The programs are read from the committed census record (docs/census-artifact),
# not from a fresh run: the record is what `tools/census_artifact.py --verify
# --strict` holds current, and a fresh census over the eight-directory corpus
# costs more than the whole curriculum does today. The RECORD is the artifact
# the locator names; the program it classifies is what the task carries.
CENSUS_CASES = ROOT / "docs" / "census-artifact" / "cases"
NO_OBJECTION_OUT_OF_SLICE = "no-objection-out-of-slice"
# A program under this directory exists to be REFUSED, which is what makes the
# reference's verdict a probe. A single-source compile of a library file the
# census cannot supply its `use`d modules for is refused too, and teaching the
# port that refusal would teach it nothing.
PROBE_CORPUS = "examples/rejections"


def _census_record_paths(cases_dir=None) -> list[Path]:
    """Every committed census record, or none when the directory is not there.

    `Path.rglob` on a missing directory returns nothing, which is why the
    adapter must tell "no records at all" apart from "records, none out of
    slice": the first is a source that cannot read its artifact, and the empty
    derivation the adapter returns for it reds in `check`. The second is a
    measured empty rung and stays green.
    """
    base = Path(cases_dir) if cases_dir else CENSUS_CASES
    return sorted(base.rglob("*.json")) if base.is_dir() else []


def _census_out_of_slice_programs(paths) -> list[tuple[str, str]]:
    """`(program, record)` for every rejection the census parks out of slice.

    A record that is not an object, or whose `case` is not a string, is refused
    WITH ITS PATH rather than skipped or left to raise AttributeError: the
    source did not read its artifact, and `derive` turns the refusal into a
    named `check` problem instead of a traceback.
    """
    found: set[tuple[str, str]] = set()
    for path in paths:
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ValueError(f"{path}: unreadable census record ({exc})") from exc
        if not isinstance(record, dict):
            raise ValueError(
                f"{path}: census record is {type(record).__name__}, not an object")
        program = record.get("case", "")
        if not isinstance(program, str):
            raise ValueError(
                f"{path}: census record's case is {type(program).__name__}, "
                "not a string")
        buckets = record.get("buckets") or ()
        if (not isinstance(buckets, (list, tuple))
                or NO_OBJECTION_OUT_OF_SLICE not in buckets):
            continue
        if not program.startswith(PROBE_CORPUS + "/"):
            continue
        if not (ROOT / program).is_file():
            continue
        try:
            record_path = str(path.relative_to(ROOT))
        except ValueError:
            # A staged record the test owns, outside the checkout. The program
            # is still named relative to ROOT, which is what a task carries.
            record_path = str(path)
        found.add((program, record_path))
    return sorted(found)


def adapter_cross_tier_probe(cases_dir=None) -> Derivation:
    records = _census_record_paths(cases_dir)
    if not records:
        # No record to read is not an empty bucket. Returning no signal makes
        # `check` say the source measured nothing, which is the case a renamed
        # docs/census-artifact directory must not hide.
        return Derivation("cross-tier-probes", (), ())
    census = _load("_ec_census", "tools/gate_reference_census.py")
    sys.path.insert(0, str(ROOT / "src"))
    from revl.compiler import compile_source
    from revl.errors import RevlError

    # Measured before the instances are looked at, off the mechanism's own
    # corpus table: the census re-decides over all eight of its directories,
    # whether or not a single program is parked out of slice here. The census
    # is the mechanism (it is what records the bucket), so `mechanism` below
    # names it; the lower oracle is the change site the task carries.
    signal = Signal(impls=(REFERENCE, SELFHOST),
                    breadth=breadth_of(census.CORPUS_DIRS))
    tasks: list[Task] = []
    for program, record in _census_out_of_slice_programs(records):
        # The census compiles an IN-MEMORY source, so a program with a `use`
        # declaration is refused by the harness (`use` declarations need
        # `modules=`) before any language rule runs. That refusal is not a
        # port gap: the lower oracle's `_ref` uses the same in-memory call and
        # its corpus has no REJECTED_PROGRAMS row for one. Compiling the same
        # way as the census is what keeps this re-derivation the recorded one.
        try:
            compile_source((ROOT / program).read_text(encoding="utf-8"),
                           "census.rvl")
        except RevlError as exc:
            if "modules=" in exc.message:
                continue
        else:
            # The record says the reference refuses it and the reference admits
            # it: the record is stale for this program, and a task derived from
            # it would be an invented one.
            continue
        tasks.append(_task(
            task_id=f"probe/{Path(program).stem}",
            source="cross-tier differential probes",
            artifact=program,
            locator=f"{record} buckets[{NO_OBJECTION_OUT_OF_SLICE!r}]",
            gap=(f"the reference refuses {program} and selfhost/lower.rvl "
                 "raises no objection to it; the census parks it in "
                 f"{NO_OBJECTION_OUT_OF_SLICE} and tests/test_selfhost_lower.py "
                 "carries no REJECTED_PROGRAMS row for it"),
            subject=("selfhost/lower.rvl + "
                     "tests/test_selfhost_lower.py (REJECTED_PROGRAMS)"),
            mechanism="tools/census_artifact.py (records in docs/census-artifact)",
            verify=("python3 tools/census_artifact.py --write && "
                    "python3 tools/census_artifact.py --check  "
                    f"# the record for {program} no longer lists "
                    f"{NO_OBJECTION_OUT_OF_SLICE}; "
                    "python3 -m pytest tests/test_selfhost_lower.py  "
                    "# a REJECTED_PROGRAMS row for it compares tag and message"),
            signal=signal,
        ))
    return Derivation("cross-tier-probes", (signal,), tuple(tasks))


# --------------------------------------------------------------------------- #
# Adapter 6 -- design documents: a slice table row whose oracle is not there     #
# Adapter 6 -- mutation testing: a refusal a mutant removes and no test notices  #
# --------------------------------------------------------------------------- #
# docs/design/*.md states its own acceptance. A slice table (header cell opening
# with "Slice", "Step", "Phase" or "Stage") whose row names a test under
# `tests/` is a promise: this oracle decides the slice. When the named file is
# not in the tree, the slice has a stated exit and no mechanism to verify it,
# which is precisely a task: write the oracle the doc already names.
# tools/mutation_runner.py mutates the refusal arms of one narrow module and runs
# that module's tests against each mutant. A mutant that survives is a refusal
# nothing pins in one direction: delete it (or negate its guard) and every test
# stays green, so a candidate that removed the refusal could not be told from one
# that kept it. Closing the hole is writing the test that kills the mutant, which
# is a task with a verifier the tool already runs.
#
# Only a path under `tests/` in a slice table's own row counts. A path in prose,
# in a table about something else (a commit table, a fork-status table), or in a
# row of a table whose first column is not a slice, is not a promise made by a
# slice plan, and a task derived from one would be an invented task.
DESIGN_DIR = "docs/design"
_SLICE_HEADER = re.compile(r"(?i)^\W*(slice|step|phase|stage)\b")
_TABLE_RULE = re.compile(r"^\s*\|[\s:|-]+\|\s*$")
_ORACLE_PATH = re.compile(
    r"(?<![\w/.-])(tests/[A-Za-z0-9_./-]*?\.(?:py|ts|rs|go|java))")
# The survivors are recorded in `tests/fixtures/mutation_survivors.json`, which
# `mutation_runner.py --check` holds equal to a fresh run. This source reads the
# ledger rather than running the mutants (each is a test-suite run), and keeps a
# survivor only while its arm is still in the target with the same source, so a
# rewritten arm retires its entry instead of leaving a task about code that is
# gone. The mutants and the ledger are readable by a candidate: the first version
# is in the tree, and where hidden mutants live is issue #1207's question.
#
# The rung is the mechanism's own. The mutation run makes one implementation
# agree with its own tests, over the test files the ledger names, so it lands on
# `easy`. The design expected `expert` for this source; the ladder has no line
# for "measures a suite", and the signal, not the expectation, decides.
MUTATION_LEDGER = "tests/fixtures/mutation_survivors.json"
MUTATION_RUNNER = "tools/mutation_runner.py"


def _oracle_signal(path: str) -> Signal:
    """The signal of an oracle that may NOT EXIST YET, off its named path.

    A mechanism that is not in the tree has no corpus to read, so the signal is
    what the path itself says the oracle will make agree, by the naming the
    tree already uses for its differential oracles: `tests/test_selfhost_*.py`
    compare `src/revl` with `selfhost/*.rvl`, a `crate` or `native` test adds
    `crates/revl-gate`, and everything else is one implementation checking
    itself. Breadth is 1: one test file is one curated corpus, the reading
    `construct-reach` gives an emitter oracle's fixture directory. It is an
    approximation and is stated as one; when the oracle is written its real
    corpus replaces this reading, and no table of "this doc is hard" exists.
    """
    name = Path(path).name
    impls = [REFERENCE]
    if "selfhost" in name:
        impls.append(SELFHOST)
    if "crate" in name or "native" in name:
        impls.append(NATIVE)
    return Signal(impls=tuple(impls), breadth=1)


def _slice_table_rows(text: str):
    """`(line, slice cell, row)` for each row of each slice table in `text`."""
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        is_table = (lines[i].lstrip().startswith("|") and i + 1 < len(lines)
                    and _TABLE_RULE.match(lines[i + 1]))
        if not is_table:
            i += 1
            continue
        header = [c.strip() for c in lines[i].strip().strip("|").split("|")]
        j = i + 2
        rows = []
        while j < len(lines) and lines[j].lstrip().startswith("|"):
            rows.append((j + 1, lines[j]))
            j += 1
        if header and _SLICE_HEADER.match(header[0]):
            for lineno, row in rows:
                cells = row.strip().strip("|").split("|")
                yield lineno, cells[0].strip(), row
        i = j


def adapter_design_docs(design_dir=None) -> Derivation:
    base = Path(design_dir) if design_dir else ROOT / DESIGN_DIR
    docs = sorted(base.glob("*.md")) if base.is_dir() else []
    if not docs:
        # No design document to read is not "every slice has its oracle". It is
        # the source failing to read its artifact, and an empty derivation
        # makes `check` say so.
        return Derivation("design-docs", (), ())
def adapter_mutation_survivors(ledger=None, root=None) -> Derivation:
    base = Path(root) if root else ROOT       # a staged tree the test owns
    path = Path(ledger) if ledger else base / MUTATION_LEDGER
    if not path.is_file():
        # No ledger is not "no survivors". It is the source failing to read its
        # artifact, and an empty derivation makes `check` say so.
        return Derivation("mutation-survivors", (), ())
    runner = _load("_ec_mutation", MUTATION_RUNNER)
    data = json.loads(path.read_text(encoding="utf-8"))
    target, tests = data["target"], list(data["tests"])
    if not (base / target).is_file():
        raise FileNotFoundError(f"{path} names {target}, which is not in the tree")
    # Measured before the survivors are looked at: a mutation run over this
    # target and these tests is a property of the ledger's configuration.
    signal = Signal(impls=(REFERENCE,), breadth=breadth_of(tests))
    current = {a.id: a for a in runner.arms(
        (base / target).read_text(encoding="utf-8"))}
    module = runner.module_name(target)
    tasks: list[Task] = []
    signals: list[Signal] = []
    for doc in docs:
        try:
            rel = str(doc.relative_to(ROOT))
        except ValueError:
            rel = str(doc)           # a staged document the test owns
        for lineno, cell, row in _slice_table_rows(
                doc.read_text(encoding="utf-8")):
            for oracle in dict.fromkeys(_ORACLE_PATH.findall(row)):
                # Measured before the instance is looked at: a slice that
                # already has its oracle stamps the same rung a missing one
                # would, so a doc with no gap left is an empty rung, not a
                # source that stopped reading.
                signal = _oracle_signal(oracle)
                signals.append(signal)
                if (ROOT / oracle).exists():
                    continue
                slug = re.sub(r"[^A-Za-z0-9]+", "-", cell).strip("-") or "row"
                tasks.append(_task(
                    task_id=f"design/{doc.stem}/{slug}/{Path(oracle).stem}",
                    source="design documents",
                    artifact=rel,
                    locator=f"line {lineno}, slice {cell}",
                    gap=(f"{rel} names {oracle} as what decides slice {cell}, "
                         "and that file is not in the tree, so the slice's "
                         "stated exit has no mechanism that verifies it"),
                    subject=oracle,
                    mechanism=oracle,
                    verify=f"test -f {oracle} && python3 -m pytest {oracle}",
                    signal=signal,
                ))
    return Derivation("design-docs", tuple(dict.fromkeys(signals)),
                      tuple(tasks))


# --------------------------------------------------------------------------- #
# Adapter 7 -- the tracker: an open issue that names an in-tree verifier        #
# --------------------------------------------------------------------------- #
# The tracker is the review's last source, and the one that needs the network.
# The curriculum must stay computable in a checkout with none, so this adapter
# never calls GitHub and never imports anything that does: it reads
# `tests/fixtures/tracker_snapshot.json`, which `tools/tracker_snapshot.py
# --write` produces and `--check` validates offline.
#
# An open issue is a task when it names, as a verifier, a file under `tests/` or
# `tools/` that is in the tree: that is the issue saying which mechanism decides
# it is done, which every task here has to name. An issue that names none, or
# only files that are not there, is a task nobody can verify, and deriving one
# would be inventing the verifier. The rung is that verifier's, by the same
# reading slice 4 gives an oracle (`_oracle_signal`); the issue's labels (its
# priority, its area) say nothing about how many implementations must agree and
# are not read.
#
# What the snapshot cannot know is stated in `tools/tracker_snapshot.py`: an
# issue closed after it was taken stays a task until it is regenerated.
TRACKER_SNAPSHOT = "tests/fixtures/tracker_snapshot.json"


def adapter_tracker(snapshot=None) -> Derivation:
    path = Path(snapshot) if snapshot else ROOT / TRACKER_SNAPSHOT
    if not path.is_file():
        # No snapshot is not "no open work": it is the source failing to read
        # its artifact, and an empty derivation makes `check` say so.
        return Derivation("tracker", (), ())
    data = json.loads(path.read_text(encoding="utf-8"))
    issues = data.get("issues") if isinstance(data, dict) else None
    if not isinstance(issues, list):
        raise ValueError(f"{path}: the snapshot has no `issues` list")
    try:
        artifact = str(path.relative_to(ROOT))
    except ValueError:
        artifact = str(path)             # a staged snapshot the test owns
    # Measured before the instances are looked at: every verifier the snapshot
    # names declares the rung it would land on, in the tree or not, so a tracker
    # whose named files have all gone is an empty rung and not silence.
    signals: list[Signal] = []
    tasks: list[Task] = []
    for issue in issues:
        named = list(issue.get("verifiers", ()))
        signals.extend(_oracle_signal(v) for v in named)
        present = [v for v in named if (ROOT / v).is_file()]
        if not present:
            continue
        number = issue["number"]
        verifier = present[0]
    for survivor in data["survivors"]:
        arm = current.get(survivor["arm"])
        if arm is None or arm.digest != survivor["digest"]:
            continue                      # the arm was rewritten or removed
        operator = survivor["operator"]
        tasks.append(_task(
            task_id=f"tracker/{number}",
            source="the tracker",
            artifact=artifact,
            locator=f"issues[#{number}]",
            gap=(f"open issue #{number} ({issue['title']}) names {verifier} "
                 "as its verifier and is still open in the snapshot"),
            subject=f"inso1337/revl#{number}",
            mechanism=verifier,
            verify=(f"gh issue view {number} --json state  # closed; then "
                    "python3 tools/tracker_snapshot.py --write"),
            signal=_oracle_signal(verifier),
            task_id=f"mutant/{module}/{survivor['arm']}/{operator}",
            source="mutation testing",
            artifact=target,
            locator=f"{survivor['arm']}, line {arm.line}",
            gap=(f"the {operator} mutant of the refusal arm {survivor['arm']} "
                 f"of {target} survives: {runner.EFFECT[operator]} and no test "
                 f"in {', '.join(tests)} fails"),
            subject=", ".join(tests),
            mechanism=f"{MUTATION_RUNNER} --check",
            verify=(f"python3 {MUTATION_RUNNER} --check  # the mutant is "
                    f"killed; then python3 {MUTATION_RUNNER} --write to drop "
                    f"its entry from {MUTATION_LEDGER}"),
            signal=signal,
        ))
    return Derivation("tracker", tuple(dict.fromkeys(signals)), tuple(tasks))
    return Derivation("mutation-survivors", (signal,), tuple(tasks))


ADAPTERS = {
    "construct-reach": adapter_construct_reach,
    "census-bypass": adapter_census_bypass,
    "unexplained-refusal": adapter_unexplained_refusal,
    "formal-obligation": adapter_formal_obligation,
    "cross-tier-probes": adapter_cross_tier_probe,
    "design-docs": adapter_design_docs,
    "tracker": adapter_tracker,
    "mutation-survivors": adapter_mutation_survivors,
}


# --------------------------------------------------------------------------- #
# The curriculum, and the gate over it.                                         #
# --------------------------------------------------------------------------- #
def derive(adapters=None) -> list[Derivation]:
    """Every source's measurement at HEAD, one `Derivation` each.

    A source that raises is RECORDED rather than propagated. To a caller
    reading a count, a traceback and an empty rung are the same event, and
    issue #1410 is that they must never be: the Derivation a raising source
    produces carries the exception and no signal, so it reds by name and the
    rungs it feeds red as unreachable rather than passing as finished.
    """
    names = list(ADAPTERS) if adapters is None else list(adapters)
    out: list[Derivation] = []
    for name in names:
        try:
            out.append(ADAPTERS[name]())
        except Exception as exc:                          # noqa: BLE001
            out.append(Derivation(name, error=f"{type(exc).__name__}: {exc}"))
    return out


def tasks_of(derivations) -> list[Task]:
    return [task for d in derivations for task in d.tasks]


def generate(adapters=None) -> list[Task]:
    """The tasks alone; `derive` is the same run with the measurement kept."""
    return tasks_of(derive(adapters))


def populations(tasks) -> dict[str, int]:
    """The measured population of each rung. Computed, never declared."""
    counts = {name: 0 for name in TIERS}
    for task in tasks:
        counts[task.tier] += 1
    return counts


def reached_by(derivations) -> dict[str, list[str]]:
    """`rung -> the sources that stamp it at HEAD`, population aside.

    Read off the signals each source measures from its own artifacts, so a
    rung is reachable while some source is still deriving the shape that lands
    on it, whether or not that source currently has an instance to put there.
    """
    out: dict[str, list[str]] = {name: [] for name in TIERS}
    for d in derivations:
        for rung in sorted(d.rungs()):
            if d.source not in out[rung]:
                out[rung].append(d.source)
    return out


def empty_rungs(derivations) -> dict[str, list[str]]:
    """`rung -> sources` for the rungs a source reaches and found nothing on.

    Deliberately NOT a problem and deliberately not silent. This is the state
    the tree reached when PR #1396 met item 391's exit and PR #1404 took the go
    carried set to zero: `hard` is derived from recorded gate bypasses, the
    census baseline records none, and the rung is empty because the bypasses
    were closed. Reporting that as a failure would make the gate fire on the
    repository improving, and the only way to clear it would be to re-open a
    closed gap, which is the trade issue #1407 forbids.
    """
    counts = populations(tasks_of(derivations))
    who = reached_by(derivations)
    return {name: who[name] for name in TIERS if who[name] and not counts[name]}


def check(derivations) -> list[str]:
    """Reasons the curriculum is not admissible. Empty means green.

    REACHABILITY is what is gated; population is what is reported. A rung no
    source can stamp is a decorative rung, and that is the failure the review
    meant when it called an unpopulated tier a RED: the ladder has a line on it
    that no derivation in the tree feeds, so the curriculum needs a new source.
    A rung some source reaches and finds nothing on is a different event, it is
    the tree having no such work open, and `empty_rungs` reports it with the
    count visible instead.

    Four ways to fail, and each of them can fire:

      * a source that raised. It measured nothing, so what it feeds is unknown
        rather than finished, and the message names the exception.
      * a source that ran and measured no signal at all, which is what a
        renamed or deleted artifact looks like from here: the adapter skips
        every branch and returns empty. Without this, a source that stopped
        reading the tree would be indistinguishable from one whose work is
        done.
      * a rung no source stamps.
      * a task naming an artifact that is not in the tree, which is the failure
        mode of an INVENTED task and the one thing "derived from the tree" is
        supposed to exclude; or two tasks with the same id, which would let one
        source's population silently stand in for another's.
    """
    problems: list[str] = []
    for d in derivations:
        if d.error:
            problems.append(f"source {d.source!r} could not be derived at "
                            f"HEAD: {d.error}")
        elif not d.signals:
            problems.append(
                f"source {d.source!r} ran and measured no signal at HEAD: it "
                "reached none of the artifacts it derives from, so the rungs "
                "it feeds are unknown rather than empty")
    who = reached_by(derivations)
    for name in TIERS:
        if not who[name]:
            problems.append(
                f"tier {name!r} is unreachable at HEAD: no source stamps a "
                "signal that lands on it (a rung no derivation reaches is a "
                "RED, and needs a new source rather than a shorter ladder)")
    seen: set[str] = set()
    for task in tasks_of(derivations):
        if not (ROOT / task.artifact).exists():
            problems.append(f"{task.task_id}: artifact {task.artifact} "
                            "is not in the tree")
        if task.task_id in seen:
            problems.append(f"duplicate task id {task.task_id}")
        seen.add(task.task_id)
    return problems


def _report(derivations) -> str:
    tasks = tasks_of(derivations)
    counts = populations(tasks)
    who = reached_by(derivations)
    lines = [f"{len(tasks)} derived tasks at HEAD"]
    for name in TIERS:
        sources = ", ".join(who[name])
        if not sources:
            note = "UNREACHABLE, no source at HEAD stamps this rung"
        elif counts[name]:
            note = f"from {sources}"
        else:
            note = (f"empty, and reachable: {sources} derives this rung and "
                    "found no instance at HEAD")
        lines.append(f"  {name:<7} {counts[name]:>4}  {note}")
    for d in derivations:
        if d.error:
            lines.append(f"  source {d.source} FAILED: {d.error}")
    lines.append("")
    by_source: dict[str, list[Task]] = {}
    for task in tasks:
        by_source.setdefault(task.source, []).append(task)
    for source in sorted(by_source):
        group = by_source[source]
        rungs = sorted({t.tier for t in group})
        lines.append(f"{source}: {len(group)} task(s), tier(s) {', '.join(rungs)}")
        for task in group[:3]:
            lines.append(f"  [{task.tier}] {task.task_id}")
            lines.append(f"      artifact {task.artifact} ({task.locator})")
            lines.append(f"      gap      {task.gap}")
            lines.append(f"      verify   {task.mechanism}")
        if len(group) > 3:
            lines.append(f"  ... {len(group) - 3} more")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--json", type=Path, help="write the curriculum as JSON")
    ap.add_argument("--check", action="store_true",
                    help="fail when a source cannot be derived, when a rung is "
                         "unreachable, or when a task names no artifact; an "
                         "empty but reachable rung is reported, not failed")
    ap.add_argument("--adapter", action="append", choices=sorted(ADAPTERS),
                    help="restrict to one adapter (repeatable); default is all")
    args = ap.parse_args(argv)

    derivations = derive(args.adapter)
    tasks = tasks_of(derivations)
    if args.json:
        args.json.write_text(json.dumps(
            {"tasks": [t.as_dict() for t in tasks],
             "populations": populations(tasks),
             "reached_by": reached_by(derivations),
             "empty_rungs": empty_rungs(derivations),
             "failed_sources": {d.source: d.error
                                for d in derivations if d.error}},
            indent=2, sort_keys=True) + "\n")
    else:
        print(_report(derivations))
    if args.check:
        # Two prefixes, never one. CURRICULUM-EMPTY is a measurement of the
        # tree and exits 0; CURRICULUM-RED is the gate failing.
        for rung, sources in sorted(empty_rungs(derivations).items()):
            print(f"CURRICULUM-EMPTY tier {rung!r} has 0 tasks at HEAD, and is "
                  f"reachable: {', '.join(sources)} derives this rung and "
                  "found no instance. The tree has no such work open.",
                  file=sys.stderr)
        problems = check(derivations)
        for problem in problems:
            print(f"CURRICULUM-RED {problem}", file=sys.stderr)
        return 1 if problems else 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
