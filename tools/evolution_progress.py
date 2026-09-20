#!/usr/bin/env python3
"""The progress half of the self-evolution reward: monotone repository counters.

WHY THIS EXISTS
---------------
Roadmap item 536 (issue #1206) enumerated eight reward components and every one
of them is a PRESERVATION check: compiles, tests pass, no new false admits, no
cross-tier divergence, byte stability of unrelated goldens, formal status, scope
discipline, documentation accuracy. Each is correct and each is necessary.

Together they define a reward whose maximum is attained by the EMPTY DIFF.
Nothing in the set rises when the system gets better, and everything in it falls
when the system breaks. The cheapest policy that satisfies that signal is to
propose nothing, so a loop trained against it learns caution rather than
capability. That is issue #1224 / roadmap item 545.

This module supplies the missing half. It does NOT replace the conjunction in
`tools/evolution_reward.py`; it adds one conjunct to it and one rule above it.

THE COUNTERS ARE REPOSITORY FACTS, NOT THE MODEL'S ACCOUNT OF ITS OWN WORK
--------------------------------------------------------------------------
Every counter here is read out of a checked-in artifact with `git show`, on both
sides of the change. No counter reads a candidate's prose, a commit message, a
changelog, or a self-report. A counter that cannot be read is `unreadable`, and
`unreadable` is a failure (see FAILURE DIRECTION).

Each counter is a PAIR, `value` over `universe`:

    census-allowance        baselined gate/reference divergences over the number
                            of `.rvl` documents in the census corpus
    native-chain-residual   documents the fully-native chain does not reproduce,
                            over the whole corpus it is measured on
    reach-gaps              named reference constructs no corpus document reaches,
                            over the size of the reference dispatch tables

The pair is the anti-gaming mechanism and it is the same one in all three cases.
Every one of these counters shrinks when the work is done AND shrinks when the
measured surface is deleted. A bare value would reward the deletion. Requiring
the universe not to shrink means the ONLY way to record an improvement is to
move a document, or a construct, across the line from the failing side to the
passing side, which is the change somebody has to read.

HOW PROGRESS COMPOSES WITH A CONJUNCTION (AND DOES NOT BECOME A SCALAR)
-----------------------------------------------------------------------
`tools/evolution_reward.py` decided conjunction, not scalar, with a measured
argument: no cargo scores 7/8 = 0.875 and clears any threshold, so a scalar
rewards the fail-open shape. That decision stands, and a progress component that
could be TRADED against a conservation component would undo it.

So progress enters at two levels, with two different quantifiers, and at neither
level is a number compared against a bar:

  * At the CANDIDATE level it is a NON-REGRESSION predicate, `progress`, and it
    is simply a ninth conjunct. `verified` iff every counter was read on both
    sides and none regressed. This is a conservation check like the other eight,
    it composes into the existing `all()` unchanged, and it cannot be traded,
    because a conjunction has nothing to trade with.

  * At the GENERATION level it is an EXISTENTIAL over the retained candidates:
    a generation is promoted iff at least one candidate was retained (all nine
    components verified) AND that same candidate improved at least one counter.
    That is a quantifier over a set, not a weighted sum over components.

The two levels are what keeps progress out of the trade. A candidate's
improvement is never summed with, subtracted from, or compared against its
conservation verdicts: it is only consulted for candidates that already passed
every one of them. A candidate that improves three counters and breaks the
census is not retained, contributes nothing to the existential, and the
generation is no more promoted for its existence than if it had never run.

WHAT THE ALTERNATIVES WOULD HAVE PERMITTED, CONCRETELY
------------------------------------------------------
  * A SCALAR with weight on progress. The census allowance on `origin/main` is
    nine entries. A candidate that retires two of them and introduces one new
    `false-admit` moves the progress counter down by two and fails exactly one
    conservation component; under any weighting with positive progress weight it
    outscores a clean no-op. The gate itself refuses that trade -- the census
    `--check` is a hard exit 1 -- so a reward that permits it is satisfiable in a
    way the underlying tool is not, which is a defect.

  * PER-CANDIDATE ADVANCEMENT, i.e. making `advanced` a tenth conjunct. Then a
    correct refactor, a documentation fix and a bug fix that closes no counter
    are all unretainable, and the cheapest way to be retained is to pick the
    counter that is easiest to move rather than the work that matters. The
    existential is deliberately at the generation level so that a generation of
    ten honest non-advancing candidates and one real advance is promoted, and a
    generation of eleven empty diffs is recorded as a generation that did not
    advance.

FAILURE DIRECTION
-----------------
Fail-closed, with no third value. A counter is in exactly one of four
directions, and only `improved` and `unchanged` satisfy the ninth conjunct:

    improved     universe did not shrink AND value strictly fell
    unchanged    universe did not shrink AND value did not change
    regressed    value rose, OR the universe shrank (whatever the value did)
    unreadable   either side could not be read at all

`unreadable` is NOT `unchanged`. A progress term that reads "I could not measure
this" as "nothing got worse" is the fail-open shape this repository has already
measured eleven times: a check that ran on every PR and could not fail. So a
counter whose artifact is missing, malformed, or absent at `base` FAILS the
component, and it can never witness the generation-level existential either.

A universe that GREW with an unchanged value is `unchanged`, not `improved`:
adding corpus documents that all pass is good work, but it is not this counter
moving, and crediting it would make "add passing fixtures" the cheapest advance.

USAGE
-----
    python3 tools/evolution_progress.py --tree . --base origin/main
    python3 tools/evolution_progress.py --tree . --base origin/main --json out.json
    python3 tools/evolution_progress.py --generation generation.json

`--tree/--base` prints the counter ledger and the ninth conjunct's verdict, exit
0 only when it verified. `--generation` reads a JSON list of scorecards in the
shape `tools/evolution_reward.py` writes, each extended with the `progress` block
this tool produces, and exits 0 only when the generation is promoted.

Nothing here exports a top-level number, and `tests/test_evolution_progress.py`
asserts it, as `tests/test_evolution_reward.py` does for the conservation half.
"""

from __future__ import annotations

import argparse
import ast
import json
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

# ---------------------------------------------------------------- artifacts

CENSUS_BASELINE = "tools/gate_reference_census_baseline.json"
CENSUS_TOOL = "tools/gate_reference_census.py"
REACH_LEDGER = "tests/fixtures/oracle_construct_reach_ledger.json"
COVERAGE_TOOL = "tools/selfhost_coverage.py"
NATIVE_CHAIN_TEST = "tests/test_selfhost_compile.py"

# The ledger keys whose reference table this module can read from FILES on both
# sides of a change. `tools/oracle_construct_reach.py` builds the emit_<tier>
# tables with `selfhost_coverage.reference_constructs(backends/<lang>/emit.py)`,
# which takes a path and is therefore usable against a materialised base blob.
# Its `lower_ir` and `compile` tables are built by private helpers that hardcode
# the repository root, so they are not readable at `base` without a second
# checkout; those oracles are excluded from BOTH halves of this counter rather
# than counted on one side only. See the design doc for the residual that leaves.
REACH_ORACLES = ("emit_py", "emit_ts", "emit_go", "emit_java", "emit_rust",
                 "emit_wasm")

# The component name this module contributes to the eight of roadmap item 536.
COMPONENT = "progress"

# The four directions. There is no fifth, and in particular no `unknown`.
IMPROVED = "improved"
UNCHANGED = "unchanged"
REGRESSED = "regressed"
UNREADABLE = "unreadable"

# The directions the ninth conjunct accepts. Stated as a set so the fail-closed
# rule is a membership test rather than a chain of negations somebody can extend.
NON_REGRESSING = frozenset({IMPROVED, UNCHANGED})


# -------------------------------------------------------------------- views

class TreeView:
    """One side of the comparison. Reads artifacts, never runs a candidate tool.

    Both sides go through this interface so a counter cannot accidentally be
    computed one way at `head` and another way at `base`: the counter functions
    below see a view and nothing else.
    """

    label = "tree"

    def read(self, path: str) -> str | None:
        raise NotImplementedError

    def paths(self) -> tuple[str, ...] | None:
        raise NotImplementedError

    def materialise(self, path: str, into: Path) -> Path | None:
        """A real file holding this side's `path`, for a helper that takes one."""
        text = self.read(path)
        if text is None:
            return None
        target = into / path.replace("/", "__")
        target.write_text(text)
        return target


def _git(tree: Path, args, timeout: int = 120):
    try:
        proc = subprocess.run(["git", "-C", str(tree)] + [str(a) for a in args],
                              capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, OSError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


class WorkingTreeView(TreeView):
    """The candidate's tree as it stands, committed changes and uncommitted alike.

    A trajectory is scored on the tree it produced, not on how tidily it was
    committed, which is the rule `tools/evolution_reward.py` already applies to
    the changed-file set.
    """

    def __init__(self, tree: Path):
        self.tree = Path(tree)
        self.label = str(tree)

    def read(self, path: str) -> str | None:
        target = self.tree / path
        try:
            return target.read_text()
        except OSError:
            return None

    def paths(self) -> tuple[str, ...] | None:
        tracked = _git(self.tree, ["ls-files"])
        untracked = _git(self.tree, ["ls-files", "--others", "--exclude-standard"])
        if tracked is None or untracked is None:
            return None
        names = {line for line in tracked.splitlines() if line}
        names |= {line for line in untracked.splitlines() if line}
        # `git ls-files` lists the INDEX. A document the candidate deleted is
        # still in it, and counting it would hide exactly the corpus deletion
        # the universe exists to catch, so the walk is filtered to what is on
        # disk: the tree the trajectory actually produced.
        return tuple(sorted(n for n in names if (self.tree / n).is_file()))

    def materialise(self, path: str, into: Path) -> Path | None:
        target = self.tree / path
        return target if target.is_file() else None


class RefView(TreeView):
    """A git ref, read with `git show` and `git ls-tree`. No checkout is made.

    This is what makes "the counter moved" a fact about the diff rather than a
    fact about two machines: the base side is the bytes the ref names, read from
    the same object store the reviewer reads.
    """

    def __init__(self, tree: Path, ref: str):
        self.tree = Path(tree)
        self.ref = str(ref)
        self.label = str(ref)

    def read(self, path: str) -> str | None:
        return _git(self.tree, ["show", f"{self.ref}:{path}"])

    def paths(self) -> tuple[str, ...] | None:
        out = _git(self.tree, ["ls-tree", "-r", "--name-only", self.ref])
        if out is None:
            return None
        return tuple(sorted(line for line in out.splitlines() if line))


# ----------------------------------------------------------------- readings

@dataclass(frozen=True)
class Reading:
    """One counter on one side. `value is None` means it could not be read."""

    counter: str
    value: int | None
    universe: int | None
    detail: str
    evidence: tuple = ()

    @property
    def readable(self) -> bool:
        return self.value is not None and self.universe is not None

    def as_dict(self) -> dict:
        return {"counter": self.counter, "value": self.value,
                "universe": self.universe, "detail": self.detail,
                "evidence": list(self.evidence)}


def unreadable(counter: str, detail: str, evidence=()) -> Reading:
    return Reading(counter, None, None, detail, tuple(evidence))


@dataclass(frozen=True)
class Delta:
    """One counter across the change. `direction` is one of the four constants."""

    counter: str
    direction: str
    base: Reading
    head: Reading
    detail: str

    def as_dict(self) -> dict:
        return {"counter": self.counter, "direction": self.direction,
                "detail": self.detail,
                "base": self.base.as_dict(), "head": self.head.as_dict()}


def direction_of(base: Reading, head: Reading) -> Delta:
    """The four-way rule, in the order the failure direction requires.

    Unreadability is decided FIRST, so a counter that vanished from the tree can
    never be reported as an unchanged counter. The universe is decided SECOND, so
    a value that fell because the measured surface was deleted is a regression
    and not an improvement. Only then does the value decide.
    """
    counter = head.counter
    if not base.readable and not head.readable:
        return Delta(counter, UNREADABLE, base, head,
                     f"neither side could be read: {base.detail}; {head.detail}")
    if not base.readable:
        return Delta(counter, UNREADABLE, base, head,
                     f"not readable at base: {base.detail}")
    if not head.readable:
        return Delta(counter, UNREADABLE, base, head,
                     f"not readable at head: {head.detail}")
    if head.universe < base.universe:
        return Delta(
            counter, REGRESSED, base, head,
            f"the measured surface SHRANK, {base.universe} to {head.universe}: "
            f"a smaller denominator is not progress whatever the value did "
            f"({base.value} to {head.value})")
    if head.value > base.value:
        return Delta(counter, REGRESSED, base, head,
                     f"{base.value} to {head.value} over {head.universe}")
    if head.value < base.value:
        return Delta(counter, IMPROVED, base, head,
                     f"{base.value} to {head.value} over {head.universe}")
    return Delta(counter, UNCHANGED, base, head,
                 f"{head.value} over {head.universe}, unmoved")


# ----------------------------------------------------------------- counters

def _json(view: TreeView, path: str):
    text = view.read(path)
    if text is None:
        return None, f"{path} is not present at {view.label}"
    try:
        return json.loads(text), ""
    except ValueError as exc:
        return None, f"{path} at {view.label} is not readable JSON: {exc}"


def census_allowance(view: TreeView, scratch: Path) -> Reading:
    """Baselined gate/reference divergences, over the census corpus size.

    VALUE is every case id in `tools/gate_reference_census_baseline.json`. That
    file is the gate's allowance, and the census tool's own note states the
    property this counter needs: the allowance "can only shrink in a diff
    somebody reads", because `--check` fails both on a divergence that is not
    baselined and on a baseline entry that no longer diverges.

    UNIVERSE is the number of `.rvl` documents the census walks, computed from
    `CORPUS_DIRS` and `_SKIP_DIRS` read out of the census tool's own source so
    the two cannot drift apart silently. It is the denominator because the value
    also falls when a divergent document is DELETED, and deleting the evidence is
    the cheapest possible way to retire an allowance entry.
    """
    name = "census-allowance"
    baseline, why = _json(view, CENSUS_BASELINE)
    if baseline is None:
        return unreadable(name, why, [CENSUS_BASELINE])
    buckets = baseline.get("buckets")
    if not isinstance(buckets, dict):
        return unreadable(name, f"{CENSUS_BASELINE} at {view.label} has no "
                                f"`buckets` object", [CENSUS_BASELINE])
    value = sum(len(ids) for ids in buckets.values())

    source = view.read(CENSUS_TOOL)
    if source is None:
        return unreadable(name, f"{CENSUS_TOOL} is not present at {view.label}",
                          [CENSUS_TOOL])
    dirs = _literal_constant(source, "CORPUS_DIRS")
    skip = _literal_constant(source, "_SKIP_DIRS")
    if dirs is None or skip is None:
        return unreadable(
            name,
            f"{CENSUS_TOOL} at {view.label} does not define CORPUS_DIRS and "
            f"_SKIP_DIRS as literals, so the corpus cannot be counted",
            [CENSUS_TOOL])
    paths = view.paths()
    if paths is None:
        return unreadable(name, f"the file list at {view.label} could not be read")
    prefixes = tuple(str(d).rstrip("/") + "/" for d in dirs)
    skip = {str(s) for s in skip}
    universe = sum(
        1 for p in paths
        if p.endswith(".rvl") and p.startswith(prefixes)
        and not (skip & set(p.split("/"))))
    return Reading(name, value, universe,
                   f"{value} baselined divergence(s) over {universe} corpus "
                   f"document(s)", [CENSUS_BASELINE, CENSUS_TOOL])


def native_chain_residual(view: TreeView, scratch: Path) -> Reading:
    """Roadmap item 146: the documents the fully-native chain does NOT reproduce.

    VALUE is every entry of `LOWER_GAP_DOCS` in `tests/test_selfhost_compile.py`.
    That table is already a ratchet: the test recomputes the residual set rather
    than sampling it, so a stale entry is a RED and a document leaves the list
    the day `selfhost/lower.rvl` grows the surface it needed.

    UNIVERSE is that residual plus every corpus table in the same module whose
    name ends `_DOCS`, i.e. the whole surface the chain is measured over. The
    identity is the point: moving a document from the residual into a corpus
    table leaves the universe fixed and drops the value, which is an improvement;
    DELETING a residual document drops both, which is not.

    Both halves are one `ast.parse` of one checked-in file, so they read the same
    way at `base` as at `head`.
    """
    name = "native-chain-residual"
    source = view.read(NATIVE_CHAIN_TEST)
    if source is None:
        return unreadable(name, f"{NATIVE_CHAIN_TEST} is not present at "
                                f"{view.label}", [NATIVE_CHAIN_TEST])
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return unreadable(name, f"{NATIVE_CHAIN_TEST} at {view.label} does not "
                                f"parse: {exc}", [NATIVE_CHAIN_TEST])

    residual = None
    corpus = 0
    for node in tree.body:
        target = _assigned_name(node)
        if target is None:
            continue
        value = node.value
        if target == "LOWER_GAP_DOCS":
            if not isinstance(value, ast.Dict):
                continue
            residual = sum(len(v.elts) for v in value.values
                           if isinstance(v, (ast.List, ast.Tuple)))
        elif target.endswith("_DOCS") and isinstance(value, (ast.List, ast.Tuple)):
            corpus += len(value.elts)

    if residual is None:
        return unreadable(
            name,
            f"{NATIVE_CHAIN_TEST} at {view.label} defines no LOWER_GAP_DOCS "
            f"mapping, so the residual is absent rather than zero",
            [NATIVE_CHAIN_TEST])
    universe = residual + corpus
    return Reading(name, residual, universe,
                   f"{residual} residual document(s) over a {universe}-document "
                   f"chain corpus", [NATIVE_CHAIN_TEST])


def reach_gaps(view: TreeView, scratch: Path) -> Reading:
    """Named reference constructs no corpus document reaches (issue #1203).

    VALUE is every name in `tests/fixtures/oracle_construct_reach_ledger.json`
    under the emitter oracles. That ledger is shrink-only by construction: a
    construct that becomes unreached and is not listed is a RED, and a listed
    entry that is no longer unreached is a RED whose fix is to DELETE the line.

    UNIVERSE is the size of the reference dispatch tables those gaps are drawn
    from, recomputed here from `backends/<tier>/emit.py` with the repository's
    own `selfhost_coverage.reference_constructs`, on BOTH sides. Without it the
    cheapest way to shrink the ledger is to delete an unreached dispatch arm, and
    that deletion is the one no conservation component reliably catches, because
    an arm nothing reaches emits nothing and so breaks no golden.
    """
    name = "reach-gaps"
    ledger, why = _json(view, REACH_LEDGER)
    if ledger is None:
        return unreadable(name, why, [REACH_LEDGER])
    present = [o for o in REACH_ORACLES if o in ledger]
    if not present:
        return unreadable(
            name,
            f"{REACH_LEDGER} at {view.label} names none of the emitter oracles, "
            f"so the ledger is absent rather than empty", [REACH_LEDGER])
    value = 0
    for oracle in present:
        entry = ledger[oracle]
        if not isinstance(entry, list):
            return unreadable(name, f"{REACH_LEDGER} at {view.label}: entry "
                                    f"`{oracle}` is not a list", [REACH_LEDGER])
        value += len(entry)

    coverage = _load_coverage(view, scratch)
    if coverage is None:
        return unreadable(name, f"{COVERAGE_TOOL} at {view.label} could not be "
                                f"loaded, so the reference tables cannot be "
                                f"sized", [COVERAGE_TOOL])
    universe = 0
    for oracle in present:
        tier = oracle[len("emit_"):]
        spec = getattr(coverage, "TIERS", {}).get(tier)
        if spec is None:
            return unreadable(name, f"{COVERAGE_TOOL} at {view.label} knows no "
                                    f"tier `{tier}`", [COVERAGE_TOOL])
        emit = view.materialise(f"backends/{spec[0]}/emit.py", scratch)
        if emit is None:
            return unreadable(name, f"backends/{spec[0]}/emit.py is not present "
                                    f"at {view.label}")
        try:
            universe += len(coverage.reference_constructs(emit))
        except (SyntaxError, OSError, ValueError) as exc:
            return unreadable(name, f"backends/{spec[0]}/emit.py at "
                                    f"{view.label} could not be surveyed: {exc}")
    return Reading(name, value, universe,
                   f"{value} unreached construct(s) over {universe} reference "
                   f"dispatch(es) across {len(present)} emitter oracle(s)",
                   [REACH_LEDGER, COVERAGE_TOOL])


COUNTERS = {
    "census-allowance": census_allowance,
    "native-chain-residual": native_chain_residual,
    "reach-gaps": reach_gaps,
}


# ------------------------------------------------------------------ helpers

def _assigned_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Assign) and len(node.targets) == 1 \
            and isinstance(node.targets[0], ast.Name):
        return node.targets[0].id
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) \
            and node.value is not None:
        return node.target.id
    return None


def _literal_constant(source: str, name: str):
    """A module-level literal, read without importing. `None` when it is absent.

    The census tool imports the compiler, so importing it to read two tuples of
    strings would make a counter depend on a working frontend. An `ast` read is
    the same answer with none of that coupling, and it works against a base blob
    that was never installed anywhere.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    for node in tree.body:
        if _assigned_name(node) != name:
            continue
        value = node.value
        if isinstance(value, ast.Call) and value.args:
            value = value.args[0]
        try:
            return ast.literal_eval(value)
        except ValueError:
            return None
    return None


def _load_coverage(view: TreeView, scratch: Path):
    """`tools/selfhost_coverage.py` as it stands on this side of the change.

    Loaded from the side being measured, so `base` is sized by `base`'s parser
    and `head` by `head`'s. A parser improvement that finds more dispatches then
    raises the universe on one side only, which reads as a universe that grew,
    which is `unchanged` rather than `improved` -- the conservative answer.
    """
    import importlib.util

    path = view.materialise(COVERAGE_TOOL, scratch)
    if path is None:
        return None
    spec = importlib.util.spec_from_file_location(
        f"evolution_progress_coverage_{abs(hash(view.label)) % 10 ** 8}", path)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception:
        return None
    if not hasattr(module, "reference_constructs") or not hasattr(module, "TIERS"):
        return None
    return module


# ------------------------------------------------------------- the readings

def read_counters(view: TreeView, scratch: Path, counters=None) -> dict:
    """One `Reading` per registered counter. A raising counter is unreadable.

    A counter that raises is a counter that could not be read, which is a
    failure; it is never an absent entry, because an absent entry is what a
    later `all()` would silently pass over.
    """
    table = COUNTERS if counters is None else counters
    out = {}
    for name, fn in table.items():
        try:
            reading = fn(view, scratch)
        except Exception as exc:  # fail-closed: a raising counter is unreadable
            reading = unreadable(name, f"reading raised "
                                       f"{type(exc).__name__}: {exc}")
        if reading.counter != name:
            reading = unreadable(name, f"counter answered for "
                                       f"{reading.counter!r}, not {name!r}")
        out[name] = reading
    return out


def compare(base_readings: dict, head_readings: dict, counters=None) -> list:
    """One `Delta` per REGISTERED counter, not per counter that happened to read.

    Iterating the registry rather than the readings is the same fail-closed shape
    `tools/evolution_reward.py` uses for its components: a counter missing from
    either side yields an `unreadable` delta instead of dropping out of the list.
    """
    table = COUNTERS if counters is None else counters
    deltas = []
    for name in table:
        base = base_readings.get(name) or unreadable(
            name, "no reading was taken at base")
        head = head_readings.get(name) or unreadable(
            name, "no reading was taken at head")
        deltas.append(direction_of(base, head))
    return deltas


def measure(tree: Path, base: str, counters=None) -> list:
    """The deltas for a candidate tree against a base ref."""
    with tempfile.TemporaryDirectory(prefix="evolution-progress-") as raw:
        scratch = Path(raw)
        head_dir = scratch / "head"
        base_dir = scratch / "base"
        head_dir.mkdir()
        base_dir.mkdir()
        head = read_counters(WorkingTreeView(tree), head_dir, counters)
        before = read_counters(RefView(tree, base), base_dir, counters)
        return compare(before, head, counters)


# -------------------------------------------------- the ninth conjunct

@dataclass(frozen=True)
class ProgressVerdict:
    """The `progress` component, in the vocabulary of `tools/evolution_reward.py`.

    Same four fields, same meaning, same single truth value, so registering this
    in that module's `PROBES` table needs no adaptation and changes nothing about
    how retention is computed: it stays `all()` over the components.
    """

    component: str
    verified: bool
    reason: str
    evidence: tuple = ()

    def as_dict(self) -> dict:
        return {"component": self.component,
                "verdict": "verified" if self.verified else "failed",
                "reason": self.reason, "evidence": list(self.evidence)}


def progress_verdict(deltas) -> ProgressVerdict:
    """RETENTION's half: no counter regressed and no counter was unreadable.

    This is a conservation check. It does not ask whether the candidate improved
    anything, because requiring that of every candidate would make a correct
    refactor unretainable and would make the easiest counter the target.
    """
    bad = [d for d in deltas if d.direction not in NON_REGRESSING]
    evidence = tuple(f"{d.counter}: {d.direction} ({d.detail})" for d in deltas)
    if bad:
        return ProgressVerdict(
            COMPONENT, False,
            "; ".join(f"{d.counter} {d.direction}: {d.detail}" for d in bad),
            evidence)
    return ProgressVerdict(
        COMPONENT, True,
        f"{len(deltas)} repository counter(s) read on both sides, none regressed",
        evidence)


def advanced(deltas) -> bool:
    """PROMOTION's half: at least one counter strictly improved, and none broke.

    Deliberately NOT a component. It is consulted only for candidates that were
    already retained, which is what keeps it out of any trade against the
    conservation half.
    """
    deltas = list(deltas)
    if not deltas:
        return False
    if any(d.direction not in NON_REGRESSING for d in deltas):
        return False
    return any(d.direction == IMPROVED for d in deltas)


def improvements(deltas) -> tuple:
    return tuple(d.counter for d in deltas if d.direction == IMPROVED)


# ------------------------------------------------------- generation promotion

@dataclass(frozen=True)
class Promotion:
    """Whether a generation advanced, and the witness if it did."""

    promoted: bool
    reason: str
    witnesses: tuple = ()
    retained: int = 0
    considered: int = 0

    def as_dict(self) -> dict:
        return {"promoted": self.promoted, "reason": self.reason,
                "witnesses": list(self.witnesses),
                "retained": self.retained, "considered": self.considered}

    def render(self) -> str:
        head = "PROMOTE" if self.promoted else "DO NOT PROMOTE"
        return f"{head}: {self.reason}"


def _entry_retained(entry) -> bool:
    """`True` only for the literal boolean. Fail-closed on every other shape.

    A scorecard whose `retained` is a string, a number, or absent has not told us
    it was retained, and a generation rule that coerced it would promote on a
    truthy artefact of whoever serialised the record.
    """
    return isinstance(entry, dict) and entry.get("retained") is True


def _entry_advanced(entry) -> bool:
    """The candidate's own recorded counter directions, re-checked here.

    Reads `progress.deltas[*].direction` only. It does not read a boolean the
    producer wrote, because then the rule would be verifying a claim rather than
    a measurement, which is the failure item 536 named first.
    """
    block = entry.get("progress") if isinstance(entry, dict) else None
    if not isinstance(block, dict):
        return False
    deltas = block.get("deltas")
    if not isinstance(deltas, list) or not deltas:
        return False
    directions = []
    for delta in deltas:
        if not isinstance(delta, dict):
            return False
        directions.append(delta.get("direction"))
    if any(d not in NON_REGRESSING for d in directions):
        return False
    return IMPROVED in directions


def promote(entries) -> Promotion:
    """A generation is promoted iff a RETAINED candidate improved a counter.

    The existential, stated once. Not a fraction of the generation, not a mean
    improvement, not a threshold: one witness that passed every conservation
    component and moved a repository counter down.
    """
    entries = list(entries)
    considered = len(entries)
    retained = [e for e in entries if _entry_retained(e)]
    if not entries:
        return Promotion(False, "the generation is empty, so nothing advanced "
                                "it", (), 0, 0)
    if not retained:
        return Promotion(
            False,
            f"no candidate of {considered} was retained, so no candidate is "
            f"eligible to witness an advance", (), 0, considered)
    witnesses = tuple(str(e.get("candidate", f"candidate-{i}"))
                      for i, e in enumerate(retained) if _entry_advanced(e))
    if not witnesses:
        return Promotion(
            False,
            f"{len(retained)} of {considered} candidate(s) retained and none "
            f"improved a repository counter: this generation did not advance",
            (), len(retained), considered)
    return Promotion(
        True,
        f"{len(witnesses)} retained candidate(s) improved a repository counter: "
        + ", ".join(witnesses),
        witnesses, len(retained), considered)


# ---------------------------------------------------------------------- cli

def render(deltas, verdict: ProgressVerdict) -> str:
    lines = ["progress counters", ""]
    for delta in deltas:
        mark = {IMPROVED: "DOWN", UNCHANGED: "flat", REGRESSED: "UP  ",
                UNREADABLE: "????"}[delta.direction]
        lines.append(f"  {mark}  {delta.counter:<24} {delta.detail}")
    lines.append("")
    lines.append(("ok   " if verdict.verified else "FAIL ") + COMPONENT
                 + ": " + verdict.reason)
    gained = improvements(deltas)
    if verdict.verified and gained:
        lines.append("ADVANCED: " + ", ".join(gained))
    elif verdict.verified:
        lines.append("did not advance: every counter is unmoved, which is what "
                     "an empty diff looks like")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--tree", type=Path, help="the candidate tree")
    ap.add_argument("--base", help="the ref to compare it against")
    ap.add_argument("--generation", type=Path,
                    help="a JSON list of scorecards; apply the promotion rule")
    ap.add_argument("--json", type=Path, help="write the result here")
    args = ap.parse_args(argv)

    if args.generation is not None:
        try:
            entries = json.loads(args.generation.read_text())
        except (OSError, ValueError) as exc:
            print(f"evolution_progress: cannot read {args.generation}: {exc}",
                  file=sys.stderr)
            return 1
        if not isinstance(entries, list):
            print("evolution_progress: --generation must hold a JSON list of "
                  "scorecards", file=sys.stderr)
            return 1
        result = promote(entries)
        print(result.render())
        if args.json:
            args.json.write_text(
                json.dumps(result.as_dict(), indent=1, sort_keys=True) + "\n")
        return 0 if result.promoted else 1

    if args.tree is None or args.base is None:
        ap.error("pass --tree and --base, or --generation")

    deltas = measure(args.tree, args.base)
    verdict = progress_verdict(deltas)
    print(render(deltas, verdict))
    if args.json:
        args.json.write_text(json.dumps(
            {"progress": {"verdict": verdict.as_dict(),
                          "deltas": [d.as_dict() for d in deltas],
                          "improved": list(improvements(deltas))}},
            indent=1, sort_keys=True) + "\n")
    return 0 if verdict.verified else 1


if __name__ == "__main__":
    raise SystemExit(main())
