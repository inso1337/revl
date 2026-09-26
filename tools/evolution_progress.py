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

This module supplies the missing half. `tools/evolution_reward.py` registers it
as the `progress` component, one more conjunct in the same `all()`.

THE RULE: A CANDIDATE IS RETAINED ONLY WHEN IT IMPROVED SOMETHING
-----------------------------------------------------------------
The `progress` component verifies when every counter was read on both sides,
none regressed, AND AT LEAST ONE STRICTLY IMPROVED. So the empty diff, which
preserves everything and improves nothing, fails this component and is not
retained. That is the point of the item.

This was a decision, and it reverses the first slice of this module, which made
the component a non-regression check and put the improvement requirement one
level up, as an existential over a generation. The product owner decided on
issue #1224 that retention itself must require an improvement: a reward whose
per-candidate maximum is still the empty diff still teaches the per-candidate
policy "propose nothing", whatever the generation rule says afterwards.

Progress is ADDED to preservation, never traded against it. It is one conjunct
among the others, so a candidate that improves three counters and fails `tests`
is not retained, and a candidate that improves one counter and regresses another
fails this component on its own. No number is compared against a bar anywhere.

THE COST, STATED RATHER THAN HIDDEN
-----------------------------------
A candidate that makes a real improvement no counter measures is now rejected.
The counters see three things: the census allowance, the native-chain residual
and the construct-reach gaps. They do NOT see, among others:

  * a bug fix whose reproducer was not already a named entry in one of them;
  * a performance improvement (item 434's allocation counts are in no ledger);
  * a new language feature, a new stdlib function, a new tier construct;
  * a new test, a new corpus document the chain already reproduces, a new
    formal theorem, a documentation correction;
  * a refactor, a deletion of dead code, a clearer error message;
  * a security fix that is not a census divergence;
  * any improvement to a measuring tool itself (see CHEAP PROGRESS below: a
    counter whose instrument moved in the same diff cannot be credited).

And one counter can no longer move at all: the census allowance is 0 on
`origin/main`, so it is at its floor and can witness nothing until something
regresses it. Every one of those candidates now fails `progress`. That is the
price of a reward whose maximum is not the empty diff, and the way to lower it
is to add a counter, not to relax the rule.

THE COUNTERS ARE REPOSITORY FACTS, NOT THE MODEL'S ACCOUNT OF ITS OWN WORK
--------------------------------------------------------------------------
Every counter is read out of a checked-in artifact with `git show`, on both
sides of the change. No counter reads a candidate's prose, a commit message, a
changelog, or a self-report. A counter that cannot be read is `unreadable`, and
`unreadable` is a failure (see FAILURE DIRECTION).

Each counter is a pair of SETS, the failing members over the measured surface:

    census-allowance        case ids in the census baseline, over the `.rvl`
                            documents in the census corpus
    native-chain-residual   `(tier, document)` pairs in `LOWER_GAP_DOCS`, over
                            the pairs in every `test_selfhost_emit_<tier>.py`
                            `CORPUS` the residual is recomputed over
    reach-gaps              `(oracle, construct)` pairs in the reach ledger,
                            over the reference dispatches of each emitter

CHEAP PROGRESS, AND WHAT STOPS IT
---------------------------------
A reward that padding can satisfy is the gate that cannot fire, in the other
direction. The first slice compared COUNTS, and a count is defeated by a
substitution: delete a failing document and add any passing one, and the value
falls while the universe holds. So the rules are on member identity:

  1. No base member may leave the measured surface. Deleting, renaming or
     moving a document, a corpus entry or a dispatch arm is a REGRESSION,
     whatever else moved, because it removes the thing being measured.
  2. No member may newly fail.
  3. An improvement is a base-failing member that is now passing, and is
     therefore still on the surface.
  4. The document that crossed must be byte-identical on both sides. Gutting a
     failing document until it passes is the same act as deleting it.
  5. The instruments must hold still. Each counter measures a distance from a
     reference, with a tool, under a test harness. When any of those changed in
     the same diff that shrank the failing set, the counter cannot tell a
     better system from a weaker measurement, and it reads `unreadable`. The
     instruments are the reference compiler (`src/revl/`), the pytest
     configuration, and each counter's own tools (listed in `INSTRUMENTS`).
     Where the failing TABLE lives inside an instrument file, the table's own
     literal may change and nothing else in that file may.
  6. The scorer's own code reads both sides. The reach universe is sized with
     the scorer's copy of `tools/selfhost_coverage.py`, never with the
     candidate's, so no candidate code runs inside the scoring process.

What these rules cannot see, per counter, is a TABLE EDITED TO A LIE: a failing
member deleted from its table with nothing fixed. The counter reads that as an
improvement. It is refused by the ratchet that holds each table to the tree,
which the `tests` and `no-new-false-admits` components run: the census `--check`
(a divergence that is not baselined), `tests/test_selfhost_compile.py::
test_the_residual_is_located_in_lower_not_in_the_emitter` (a residual that is
recomputed, not read), and `tests/test_oracle_construct_reach.py::
test_the_committed_ledger_matches_this_tree`. That is why progress is a
conjunct and not a replacement: this component alone would pay for the lie.

WHICH BASE
----------
Progress is judged against the candidate's own base: the merge base of the
candidate's `HEAD` and the ref its record names, not that ref's tip at scoring
time. Two failures that rule closes:

  * the ref moved on after the candidate forked, carrying an improvement the
    candidate does not have. Against the tip, the candidate would read as having
    regressed a counter it never touched; against the merge base, it is judged
    on its own diff;
  * the candidate merged the ref, carrying somebody else's improvement into its
    tree. Against the fork point it would be credited for that work; against the
    merge base, that work is on both sides and credits nobody.

The record's `base` must name the trunk the candidate integrates with. A record
that names an OLDER commit than the newest trunk commit its tree contains would
move the merge base back and credit the difference, and nothing here can tell
that a named commit is stale, so the rule is only as sound as whoever writes the
record's `base`. That has to be the loop, never the candidate.

FAILURE DIRECTION
-----------------
Fail-closed, with no third value. A counter is in exactly one of four
directions:

    improved     a base-failing member crossed, nothing left, nothing newly failed
    unchanged    the failing set is the same set, nothing left the surface
    regressed    a base member left the surface, OR a member newly failed, OR a
                 crossing member's document was edited
    unreadable   either side could not be read, OR an instrument changed in the
                 same diff that shrank the failing set

`unreadable` is NOT `unchanged`. A progress term that reads "I could not measure
this" as "nothing got worse" is the fail-open shape this repository has already
measured eleven times: a check that ran on every PR and could not fail.

The surface GROWING with the failing set unmoved is `unchanged`, not `improved`:
adding corpus documents that all pass is good work, but it is not this counter
moving, and crediting it would make "add passing fixtures" the cheapest advance.

USAGE
-----
    python3 tools/evolution_progress.py --tree . --base origin/main
    python3 tools/evolution_progress.py --tree . --base origin/main --json out.json
    python3 tools/evolution_progress.py --generation generation.json

`--tree/--base` prints the counter ledger and the component's verdict, exit 0
only when it verified. `--generation` reads a JSON list of scorecards in the
shape `tools/evolution_reward.py --json` writes, and exits 0 only when the
generation is promoted.

Nothing here exports a top-level number, and `tests/test_evolution_progress.py`
asserts it, as `tests/test_evolution_reward.py` does for the conservation half.
"""

from __future__ import annotations

import argparse
import ast
import json
import posixpath
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools"))

# `Verdict` is item 536's, IMPORTED rather than restated (issue #1332). This
# module's own copy of it had the same four fields and the same `as_dict`
# spelling, with nothing holding the two in step -- and the third copy of the
# shape, in `tools/evolution_controller.py`, had ALREADY grown a fifth field
# (`code`) that neither of these two carries. A component answer that
# serialises differently from the components it is scored beside is exactly the
# translation layer the controller's own docstring says not to introduce.
from evolution_reward import Verdict  # noqa: E402

# ---------------------------------------------------------------- artifacts

CENSUS_BASELINE = "tools/gate_reference_census_baseline.json"
CENSUS_TOOL = "tools/gate_reference_census.py"
REACH_LEDGER = "tests/fixtures/oracle_construct_reach_ledger.json"
REACH_TOOL = "tools/oracle_construct_reach.py"
REACH_TEST = "tests/test_oracle_construct_reach.py"
COVERAGE_TOOL = "tools/selfhost_coverage.py"
NATIVE_CHAIN_TEST = "tests/test_selfhost_compile.py"

# The residual is recomputed over each tier's oracle corpus, which is the
# `CORPUS` list in this module (`test_selfhost_compile._tier_corpus`).
EMIT_TEST = "tests/test_selfhost_emit_{tier}.py"
EMIT_TEST_PATTERN = r"tests/test_selfhost_emit_[^/]+\.py"

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

# The component name this module contributes to item 536's conjunction.
COMPONENT = "progress"

# The four directions. There is no fifth, and in particular no `unknown`.
IMPROVED = "improved"
UNCHANGED = "unchanged"
REGRESSED = "regressed"
UNREADABLE = "unreadable"

# The directions that do not fail the component. Stated as a set so the
# fail-closed rule is a membership test rather than a chain of negations.
NON_REGRESSING = frozenset({IMPROVED, UNCHANGED})

# --------------------------------------------------------------- instruments

# What every counter measures against: the reference compiler. A counter whose
# failing set shrank in a diff that also moved the reference cannot say whether
# the subject caught up with the reference or the reference was moved to meet
# it, and the second is the cheap one (weaken the checker until it agrees with
# the gate; the census allowance falls and no gate got better).
_REFERENCE = (r"src/revl/.*",)

# The test harness configuration. A ratchet deselected from outside its own file
# holds nothing, and a table edited to a lie is only refused by its ratchet.
_HARNESS = (r"pyproject\.toml", r"pytest\.ini", r"setup\.cfg", r"tox\.ini",
            r"(.*/)?conftest\.py")

#: Per counter: the paths that decide which side of the line a member is on.
#: A change to any of them in the diff that shrank the failing set makes the
#: counter `unreadable`. Regular expressions over repository paths, anchored.
INSTRUMENTS = {
    "census-allowance": _REFERENCE + _HARNESS + (re.escape(CENSUS_TOOL),),
    "native-chain-residual": _REFERENCE + _HARNESS + (
        # the other side of the comparison the residual is recomputed from
        r"backends/[^/]+/emit\.py",
        re.escape(NATIVE_CHAIN_TEST), EMIT_TEST_PATTERN),
    "reach-gaps": _REFERENCE + _HARNESS + (
        re.escape(REACH_TOOL), re.escape(COVERAGE_TOOL), re.escape(REACH_TEST),
        EMIT_TEST_PATTERN),
}

#: Instrument files that also HOLD a counter's table. Only the named top-level
#: literal assignments may change in them; anything else is an instrument change.
#: `names` is a predicate over the assigned name.
TABLE_FILES = (
    (re.escape(NATIVE_CHAIN_TEST), lambda name: name.endswith("_DOCS")),
    (EMIT_TEST_PATTERN, lambda name: name == "CORPUS"),
)


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
    """One counter on one side: the failing members over the measured surface.

    `failing is None` means it could not be read. Members are strings, and a
    member's identity is its name: `examples/x.rvl`, `py:tests/fixtures/...`,
    `emit_go:case=Ok`. `subjects` maps a failing member to the document it
    names, for the members that are documents, so a crossing can be checked
    for having been edited rather than fixed.
    """

    counter: str
    failing: frozenset | None
    members: frozenset | None
    detail: str
    evidence: tuple = ()
    subjects: tuple = ()

    @property
    def readable(self) -> bool:
        return self.failing is not None and self.members is not None

    @property
    def value(self) -> int | None:
        return None if self.failing is None else len(self.failing)

    @property
    def universe(self) -> int | None:
        return None if self.members is None else len(self.members)

    def subject(self, member: str) -> str | None:
        return dict(self.subjects).get(member)

    def as_dict(self) -> dict:
        return {"counter": self.counter, "value": self.value,
                "universe": self.universe, "detail": self.detail,
                "failing": sorted(self.failing or ()),
                "evidence": list(self.evidence)}


def reading(counter: str, failing, members, detail: str, evidence=(),
            subjects=None) -> Reading:
    """A readable `Reading`. The surface always contains the failing set.

    A failing member that is not on the surface (a stale residual entry, a
    ledger name no dispatch table carries) is still something the table claims
    is measured, so it is counted as surface. Deleting it is then a member
    leaving the surface, which is a regression, rather than a free crossing.
    """
    failing = frozenset(failing)
    members = frozenset(members) | failing
    pairs = tuple(sorted((m, p) for m, p in (subjects or {}).items()
                         if m in failing))
    return Reading(counter, failing, members, detail, tuple(evidence), pairs)


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
    crossed: tuple = ()

    def as_dict(self) -> dict:
        return {"counter": self.counter, "direction": self.direction,
                "detail": self.detail, "crossed": list(self.crossed),
                "base": self.base.as_dict(), "head": self.head.as_dict()}


def _some(names, limit: int = 4) -> str:
    names = sorted(names)
    shown = ", ".join(names[:limit])
    return shown + (f" and {len(names) - limit} more" if len(names) > limit else "")


def direction_of(base: Reading, head: Reading) -> Delta:
    """The four-way rule over member identity, in the order failure requires.

    Unreadability is decided FIRST, so a counter that vanished from the tree can
    never be reported as an unchanged counter. The surface is decided SECOND, so
    a failing set that shrank because the measured surface lost a member is a
    regression and not an improvement. A newly failing member is decided THIRD.
    Only then can a crossing be an improvement.

    This rule sees names only. Whether a crossing was bought by editing its
    document or by moving an instrument is `credit`'s question, because it needs
    both trees and this function sees two readings.
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
    left = base.members - head.members
    if left:
        return Delta(
            counter, REGRESSED, base, head,
            f"{len(left)} member(s) LEFT the measured surface ({_some(left)}): "
            f"deleting, renaming or moving what is measured is not progress, "
            f"whatever the failing set did ({base.value} to {head.value})")
    entered = head.failing - base.failing
    if entered:
        return Delta(counter, REGRESSED, base, head,
                     f"{len(entered)} member(s) newly failing: {_some(entered)}")
    crossed = tuple(sorted(base.failing - head.failing))
    if crossed:
        return Delta(counter, IMPROVED, base, head,
                     f"{base.value} to {head.value} over {head.universe}: "
                     f"{_some(crossed)} crossed", crossed)
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
    """Baselined gate/reference divergences, over the census corpus.

    FAILING is every case id in `tools/gate_reference_census_baseline.json`.
    That file is the gate's allowance, and the census tool's own note states the
    property this counter needs: the allowance "can only shrink in a diff
    somebody reads", because `--check` fails both on a divergence that is not
    baselined and on a baseline entry that no longer diverges.

    SURFACE is every `.rvl` document the census walks, computed from
    `CORPUS_DIRS` and `_SKIP_DIRS` read out of the census tool's own source so
    the two cannot drift apart silently. A corpus case id IS a document path,
    so a crossing's document can be checked for having been edited.
    """
    name = "census-allowance"
    baseline, why = _json(view, CENSUS_BASELINE)
    if baseline is None:
        return unreadable(name, why, [CENSUS_BASELINE])
    buckets = baseline.get("buckets")
    if not isinstance(buckets, dict):
        return unreadable(name, f"{CENSUS_BASELINE} at {view.label} has no "
                                f"`buckets` object", [CENSUS_BASELINE])
    failing = set()
    for ids in buckets.values():
        if not isinstance(ids, list):
            return unreadable(name, f"{CENSUS_BASELINE} at {view.label}: a "
                                    f"bucket is not a list", [CENSUS_BASELINE])
        failing |= {str(i) for i in ids}

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
    members = {p for p in paths
               if p.endswith(".rvl") and p.startswith(prefixes)
               and not (skip & set(p.split("/")))}
    subjects = {m: m for m in failing if m in members}
    return reading(name, failing, members,
                   f"{len(failing)} baselined divergence(s) over {len(members)} "
                   f"corpus document(s)", [CENSUS_BASELINE, CENSUS_TOOL],
                   subjects)


def _module(view: TreeView, path: str):
    source = view.read(path)
    if source is None:
        return None, f"{path} is not present at {view.label}"
    try:
        return ast.parse(source), ""
    except SyntaxError as exc:
        return None, f"{path} at {view.label} does not parse: {exc}"


def _corpus_dir(node: ast.AST) -> str | None:
    """`ROOT / "tests" / "fixtures" / "x"` as the repository path it names."""
    parts = []
    while isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        if not (isinstance(node.right, ast.Constant)
                and isinstance(node.right.value, str)):
            return None
        parts.append(node.right.value)
        node = node.left
    if not (isinstance(node, ast.Name) and node.id == "ROOT") or not parts:
        return None
    return "/".join(reversed(parts))


def _tier_corpus(view: TreeView, tier: str):
    """`(corpus_dir, names)` for one tier's oracle, or `(None, reason)`."""
    path = EMIT_TEST.format(tier=tier)
    tree, why = _module(view, path)
    if tree is None:
        return None, why
    directory = names = None
    for node in tree.body:
        target = _assigned_name(node)
        if target == "CORPUS_DIR":
            directory = _corpus_dir(node.value)
        elif target == "CORPUS":
            try:
                names = ast.literal_eval(node.value)
            except ValueError:
                names = None
    if directory is None or not isinstance(names, (list, tuple)):
        return None, (f"{path} at {view.label} does not define CORPUS_DIR and "
                      f"CORPUS as literals, so the corpus cannot be read")
    return (directory, [str(n) for n in names]), ""


def native_chain_residual(view: TreeView, scratch: Path) -> Reading:
    """Roadmap item 146: the documents the fully-native chain does NOT reproduce.

    FAILING is every entry of `LOWER_GAP_DOCS` in `tests/test_selfhost_compile.py`,
    as a `tier:path` pair. That table is a ratchet: the test recomputes the
    residual over the tier's whole oracle corpus rather than sampling it, so a
    stale entry is a RED and a document leaves the list the day
    `selfhost/lower.rvl` grows the surface it needed.

    SURFACE is that same corpus: every `CORPUS` entry of every tier's
    `tests/test_selfhost_emit_<tier>.py`, resolved against its `CORPUS_DIR`.
    Resolving the path, not just the name, is what makes repointing `CORPUS_DIR`
    at a directory of easier documents a surface change rather than a crossing.
    """
    name = "native-chain-residual"
    tree, why = _module(view, NATIVE_CHAIN_TEST)
    if tree is None:
        return unreadable(name, why, [NATIVE_CHAIN_TEST])
    table = None
    for node in tree.body:
        if _assigned_name(node) == "LOWER_GAP_DOCS":
            try:
                table = ast.literal_eval(node.value)
            except ValueError:
                table = None
    if not isinstance(table, dict) or not table:
        return unreadable(
            name,
            f"{NATIVE_CHAIN_TEST} at {view.label} defines no literal "
            f"LOWER_GAP_DOCS mapping, so the residual is absent rather than zero",
            [NATIVE_CHAIN_TEST])

    failing, members, subjects = set(), set(), {}
    evidence = [NATIVE_CHAIN_TEST]
    for tier, residual in sorted(table.items()):
        corpus, why = _tier_corpus(view, str(tier))
        if corpus is None:
            return unreadable(name, why, evidence + [EMIT_TEST.format(tier=tier)])
        directory, names = corpus
        evidence.append(EMIT_TEST.format(tier=tier))

        def member(doc, tier=tier, directory=directory):
            path = posixpath.normpath(posixpath.join(directory, str(doc)))
            return f"{tier}:{path}", path

        for doc in names:
            members.add(member(doc)[0])
        if not isinstance(residual, (list, tuple)):
            return unreadable(name, f"{NATIVE_CHAIN_TEST} at {view.label}: "
                                    f"LOWER_GAP_DOCS[{tier!r}] is not a list",
                              evidence)
        for doc in residual:
            key, path = member(doc)
            failing.add(key)
            subjects[key] = path
    return reading(name, failing, members,
                   f"{len(failing)} residual document(s) over a "
                   f"{len(members | failing)}-document chain corpus", evidence,
                   subjects)


def reach_gaps(view: TreeView, scratch: Path) -> Reading:
    """Named reference constructs no corpus document reaches (issue #1203).

    FAILING is every name in `tests/fixtures/oracle_construct_reach_ledger.json`
    under the emitter oracles, as an `oracle:construct` pair. That ledger is
    shrink-only by construction: a construct that becomes unreached and is not
    listed is a RED, and a listed entry that is no longer unreached is a RED
    whose fix is to DELETE the line.

    SURFACE is the reference dispatch tables those gaps are drawn from, read
    from `backends/<tier>/emit.py` on BOTH sides by the SCORER's copy of
    `selfhost_coverage.reference_constructs`. Not the candidate's copy: loading
    that would execute candidate code inside the scoring process, where it could
    reach every other component's verdict. With one reader on both sides, a
    change to the reader cannot move the surface either.
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
    coverage = _coverage()
    if coverage is None:
        return unreadable(name, f"the scorer's {COVERAGE_TOOL} could not be "
                                f"loaded, so the reference tables cannot be read")
    failing, members = set(), set()
    for oracle in present:
        entry = ledger[oracle]
        if not isinstance(entry, list):
            return unreadable(name, f"{REACH_LEDGER} at {view.label}: entry "
                                    f"`{oracle}` is not a list", [REACH_LEDGER])
        failing |= {f"{oracle}:{construct}" for construct in entry}
        spec = coverage.TIERS.get(oracle[len("emit_"):])
        if spec is None:
            return unreadable(name, f"{COVERAGE_TOOL} knows no tier for "
                                    f"`{oracle}`", [COVERAGE_TOOL])
        emit = view.materialise(f"backends/{spec[0]}/emit.py", scratch)
        if emit is None:
            return unreadable(name, f"backends/{spec[0]}/emit.py is not present "
                                    f"at {view.label}")
        try:
            members |= {f"{oracle}:{c}" for c in coverage.reference_constructs(emit)}
        except (SyntaxError, OSError, ValueError) as exc:
            return unreadable(name, f"backends/{spec[0]}/emit.py at "
                                    f"{view.label} could not be surveyed: {exc}")
    return reading(name, failing, members,
                   f"{len(failing)} unreached construct(s) over "
                   f"{len(members | failing)} reference dispatch(es) across "
                   f"{len(present)} emitter oracle(s)",
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


_COVERAGE_CACHE: dict = {}


def _coverage():
    """The SCORER's `tools/selfhost_coverage.py`, never the candidate's.

    Loaded by path from this file's own checkout, under a private name and
    WITHOUT registering it in `sys.modules`: a bare `import selfhost_coverage`
    would bind whatever copy some earlier caller registered under that name,
    and a scorer that measured with a copy it did not choose is the shape this
    function exists to rule out. The module declares no dataclass, so it does
    not need a `sys.modules` entry to finish executing.
    """
    if "module" in _COVERAGE_CACHE:
        return _COVERAGE_CACHE["module"]
    import importlib.util  # noqa: PLC0415

    module = None
    path = ROOT / COVERAGE_TOOL
    spec = importlib.util.spec_from_file_location(
        "_evolution_progress_selfhost_coverage", path)
    if spec is not None and spec.loader is not None:
        candidate = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(candidate)
        except Exception:
            candidate = None
        if candidate is not None and hasattr(candidate, "reference_constructs") \
                and hasattr(candidate, "TIERS"):
            module = candidate
    _COVERAGE_CACHE["module"] = module
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
            result = fn(view, scratch)
        except Exception as exc:  # fail-closed: a raising counter is unreadable
            result = unreadable(name, f"reading raised "
                                      f"{type(exc).__name__}: {exc}")
        if result.counter != name:
            result = unreadable(name, f"counter answered for "
                                      f"{result.counter!r}, not {name!r}")
        out[name] = result
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


# ------------------------------------------------------------------ credit

def changed_paths(tree: Path, base: str):
    """Every path that differs between `base` and the tree as it stands.

    `--no-renames` so a file moved out of an instrument directory is reported
    under its OLD name too; with rename detection on, `--name-only` prints only
    the destination and the move would look like an addition somewhere else.
    """
    diff = _git(tree, ["diff", "--no-renames", "--name-only", base])
    untracked = _git(tree, ["ls-files", "--others", "--exclude-standard"])
    if diff is None or untracked is None:
        return None
    return frozenset(line for line in (diff + untracked).splitlines() if line)


def _table_blanked(source: str | None, keep):
    """The module's AST with the counter's table literals blanked, or `None`."""
    if source is None:
        return None
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    for node in tree.body:
        name = _assigned_name(node)
        if name is not None and keep(name):
            node.value = ast.Constant(value=None)
    return ast.dump(tree)


def moved_instruments(counter: str, changed, base: TreeView,
                      head: TreeView) -> list:
    """The instrument paths of `counter` that changed, table edits excepted."""
    patterns = [re.compile(p + r"\Z") for p in INSTRUMENTS.get(counter, ())]
    moved = []
    for path in sorted(changed):
        if not any(p.match(path) for p in patterns):
            continue
        table = next((keep for pattern, keep in TABLE_FILES
                      if re.match(pattern + r"\Z", path)), None)
        if table is not None:
            before = _table_blanked(base.read(path), table)
            after = _table_blanked(head.read(path), table)
            if before is not None and before == after:
                continue  # only the counter's own table literal changed
        moved.append(path)
    return moved


def credit(delta: Delta, changed, base: TreeView, head: TreeView) -> Delta:
    """An `improved` delta that survives rules 4 and 5, or the reason it does not.

    Rule 4: every crossing member that is a document is byte-identical on both
    sides. Rule 5: none of the counter's instruments moved in this diff. A delta
    in any other direction is returned unchanged: an instrument change that did
    NOT shrink the failing set claims no credit, and is the conservation
    components' business rather than this one's.
    """
    if delta.direction != IMPROVED:
        return delta
    if changed is None:
        return Delta(delta.counter, UNREADABLE, delta.base, delta.head,
                     "the changed-file set could not be read, so the "
                     "instruments cannot be shown to have held still")
    edited = []
    for member in delta.crossed:
        path = delta.base.subject(member)
        if path is not None and base.read(path) != head.read(path):
            edited.append(path)
    if edited:
        return Delta(
            delta.counter, REGRESSED, delta.base, delta.head,
            f"the document that crossed was EDITED in the same diff "
            f"({_some(edited)}): making a failing document pass by changing the "
            f"document is deleting it in place", delta.crossed)
    moved = moved_instruments(delta.counter, changed, base, head)
    if moved:
        return Delta(
            delta.counter, UNREADABLE, delta.base, delta.head,
            f"the failing set shrank ({delta.detail}) in a diff that also moved "
            f"this counter's instruments ({_some(moved)}), so a better system "
            f"cannot be told from a weaker measurement; land the instrument "
            f"change on its own first", delta.crossed)
    return delta


# ------------------------------------------------------------- measuring

def resolve_base(tree: Path, base: str):
    """`(sha, detail)`: the candidate's own base, or `(None, why)`.

    The merge base of the candidate's `HEAD` and the named ref. See WHICH BASE
    in the module docstring for the two failures this closes.
    """
    sha = _git(tree, ["merge-base", "HEAD", base])
    if sha is None or not sha.strip():
        return None, (f"no merge base between HEAD and {base!r} in {tree}, so "
                      f"the candidate's base cannot be named")
    sha = sha.strip()
    return sha, f"merge-base(HEAD, {base}) = {sha[:12]}"


def measure(tree: Path, base: str, counters=None) -> list:
    """The deltas for a candidate tree against its own base (see `resolve_base`)."""
    return measure_ledger(tree, base, counters)[1]


def measure_ledger(tree: Path, base: str, counters=None):
    """`(resolved_base_or_None, deltas)` for a candidate tree."""
    table = COUNTERS if counters is None else counters
    sha, why = resolve_base(tree, base)
    if sha is None:
        return None, [direction_of(unreadable(n, why), unreadable(n, why))
                      for n in table]
    with tempfile.TemporaryDirectory(prefix="evolution-progress-") as raw:
        scratch = Path(raw)
        head_dir = scratch / "head"
        base_dir = scratch / "base"
        head_dir.mkdir()
        base_dir.mkdir()
        head_view = WorkingTreeView(tree)
        base_view = RefView(tree, sha)
        head = read_counters(head_view, head_dir, table)
        before = read_counters(base_view, base_dir, table)
        changed = changed_paths(tree, sha)
        deltas = [credit(d, changed, base_view, head_view)
                  for d in compare(before, head, table)]
    return sha, deltas


# -------------------------------------------------- the progress component

@dataclass(frozen=True)
class ProgressVerdict(Verdict):
    """Item 536's `Verdict`, plus the counter ledger it was decided on.

    `as_dict` is inherited, so the component serialises with exactly the four
    fields every other component does. The ledger travels beside it, and
    `tools/evolution_reward.py` writes it into the scorecard as the `progress`
    block `promote` reads, so the generation rule re-reads measured directions
    rather than a verdict somebody wrote.
    """

    deltas: tuple = ()
    base: str = ""

    def ledger(self) -> dict:
        return {"base": self.base,
                "deltas": [d.as_dict() for d in self.deltas],
                "improved": list(improvements(self.deltas))}


def progress_verdict(deltas, base: str = "") -> ProgressVerdict:
    """RETENTION's progress conjunct: nothing regressed AND something improved.

    Both halves are required. The first is conservation; the second is the
    decision on issue #1224 that the empty diff must not attain the maximum.
    """
    deltas = tuple(deltas)
    evidence = tuple(f"{d.counter}: {d.direction} ({d.detail})" for d in deltas)
    if base:
        evidence += (f"base {base}",)
    bad = [d for d in deltas if d.direction not in NON_REGRESSING]
    if bad:
        return ProgressVerdict(
            COMPONENT, False,
            "; ".join(f"{d.counter} {d.direction}: {d.detail}" for d in bad),
            evidence, deltas, base)
    if not deltas:
        return ProgressVerdict(COMPONENT, False, "no counter was measured",
                               evidence, deltas, base)
    gained = improvements(deltas)
    if not gained:
        return ProgressVerdict(
            COMPONENT, False,
            f"did not advance: all {len(deltas)} repository counter(s) are "
            f"unmoved. That is what an empty diff looks like, and a candidate "
            f"that preserves everything and improves nothing is not retained",
            evidence, deltas, base)
    return ProgressVerdict(
        COMPONENT, True,
        f"improved {', '.join(gained)}; {len(deltas)} counter(s) read on both "
        f"sides, none regressed", evidence, deltas, base)


def probe(tree: Path, base: str, counters=None) -> ProgressVerdict:
    """The whole component for one candidate: measure, then decide."""
    sha, deltas = measure_ledger(tree, base, counters)
    return progress_verdict(deltas, sha or "")


def advanced(deltas) -> bool:
    """At least one counter strictly improved, and none broke.

    Since issue #1224's decision this is exactly `progress_verdict(...).verified`
    and the tests hold the two to each other. It stays as its own name because
    `promote` restates it over the SERIALISED ledger and the pair is pinned.
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

    Every candidate `tools/evolution_reward.py` retains has, by construction,
    improved a counter. The existential is still stated here over the
    SERIALISED ledger rather than trusted from `retained`, so a scorecard from
    an older scorer, or one assembled by hand, cannot promote a generation on
    the strength of a flag.
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
    if verdict.base:
        lines.insert(1, f"base {verdict.base}")
    for delta in deltas:
        mark = {IMPROVED: "DOWN", UNCHANGED: "flat", REGRESSED: "UP  ",
                UNREADABLE: "????"}[delta.direction]
        lines.append(f"  {mark}  {delta.counter:<24} {delta.detail}")
    lines.append("")
    lines.append(("ok   " if verdict.verified else "FAIL ") + COMPONENT
                 + ": " + verdict.reason)
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--tree", type=Path, help="the candidate tree")
    ap.add_argument("--base", help="the trunk ref the candidate forked from")
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

    verdict = probe(args.tree, args.base)
    print(render(verdict.deltas, verdict))
    if args.json:
        args.json.write_text(json.dumps(
            {"progress": verdict.ledger(), "verdict": verdict.as_dict()},
            indent=1, sort_keys=True) + "\n")
    return 0 if verdict.verified else 1


if __name__ == "__main__":
    raise SystemExit(main())
