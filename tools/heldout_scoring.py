#!/usr/bin/env python3
"""Held-out scoring: judge a candidate on a set of programs it could not read.

WHY THIS EXISTS
---------------
Every corpus that scores a change in this repository is a file in this
repository. The differential oracle's programs are two literal lists in
`tests/test_selfhost_lower.py`; the census corpus is the eight directories
`tools/gate_reference_census.py` names; the issued-admission programs are
`ADMISSION_PROGRAMS` in that same tool; the gate's fixtures are under
`tests/fixtures/`. A candidate that emits a diff against this tree has read
all of them, so the reward is a function of the candidate's own input.

A repository cannot hide a file from a process that has the repository. So the
thing held out here is NOT a file and NOT the mechanism. Everything below is
public and meant to be read: the grammar, the scorer, the buckets, the fence.
What is held out is the DRAW -- the specific finite set of programs one scoring
run uses. It does not exist until score time, it is never written to the tree,
and its identity is fixed by a seed that is proved absent from the repository
before a single program is generated.

See `docs/design/535-held-out-scoring.md` for the options that were rejected
and the attacks each one left open.

THE FOUR ENFORCEMENTS
---------------------
1. THE SET IS NOT A FILE. `draw()` generates programs in memory. Nothing here
   writes a `.rvl` anywhere, which also keeps the draw out of the globbed
   fixture directories that would make it readable on the next run.

2. THE SEED LIVES OUTSIDE THE TREE, IN A FILE ONLY ITS OWNER CAN READ.
   `--seed-file` / `REVL_HELDOUT_SEED_FILE` names a file, never a literal
   seed. The file must sit outside every git repository and carry no group or
   other permission bits; its CONTENT is then checked with
   `git grep --fixed-strings` over the tracked tree, and a seed found there is
   REFUSED. This is the enforcement that makes "the candidate cannot read it"
   a check instead of a promise: the repository is readable, so the seed file
   is required to be outside it and that requirement is tested rather than
   asserted.

3. THE SEED IS CONSUMED ONCE, AND THE CONSUMPTION IS VISIBLE. Every scored run
   appends the seed's digest (never the seed) to a rotation ledger outside the
   tree, records the rotation count in the verdict, and REFUSES a digest the
   ledger has already seen. A leaked seed can be replayed unless the replay is
   visible; this makes it visible and refuses it. Rotation itself stays the
   operator's act: writing a new seed file is what rotates.

4. THE CANDIDATE'S DIFF MUST NOT REACH THE SCORER. `HELD_OUT_FENCE` is the
   `held_out` view of the one evaluator enumeration (issue #2802), and it lists
   the paths that decide what is drawn and what counts as a divergence. The
   candidate's changed-file set is required, and a diff that touches a fenced
   path is REFUSED rather than scored, because a candidate that edited the
   scorer has not been scored by it. Refused is not failed: it is the verdict
   that routes a semantic change to a human, which is what item 537 asks for.
   The fence is checked BEFORE the diff-size bound, so a large diff that
   reaches the scorer keeps the fence refusal; on a diff that does not reach
   it, more than `MAX_DIFF_FILES` changed files or more than `MAX_DIFF_LINES`
   non-regenerated lines is REFUSED with the limit named. Regenerated
   artifacts (the census records and catalogue, the crate's generated
   `selfhost.rs`, the corpus provenance manifest) are left out of the line
   count because their churn is not the candidate's edit.

WHICH PROTOCOL PRODUCED A VERDICT
---------------------------------
Every record carries `protocol_digest`: `sha256:<hex>` over the evaluator's
tracked bytes in the scorer's own checkout (issue #2802). A scorecard and a
controller verdict from the same protocol carry the same digest, so two rounds
can show they were scored by the same protocol, and a record from a different
checkout is refused by the controller instead of read. The digest proves
identity, not correctness: custody and rotation of the expected value are issue
#2762's question.

THE FAILURE DIRECTION IS CLOSED
-------------------------------
Every unknown refuses. No seed file, a seed file inside a repository or readable
by another user, a seed found in the tracked tree, a seed digest already
consumed, no diff, a diff over the size limit, a diff that reaches the fence,
an engine that will not build, a draw that raises -- each exits non-zero with
a named refusal and NO score. There is deliberately no path on which this tool
falls back to scoring against the in-tree corpus: a gate that silently scores
a readable set when the held-out one is unavailable is the fail-open shape,
and it is the shape this tool refuses to have.

WHAT IS SCORED
--------------
Two directions, and they are scored against different things because the
census treats them differently.

The ZERO-TOLERANCE direction, against zero: `false-admission` (the gate ISSUED
an admission for a program the reference refuses) and `gate-fault` (the gate
crashed). Those are the two buckets `tools/gate_reference_census.py` gives no
allowance at all.

The BYPASS direction, against the baseline's families: `false-admit/<tag>`, the
no-objection bypass. It has an allowance in the census and it gets the same one
here, read from `tools/gate_reference_census_baseline.json` rather than copied.
A tag the baseline already carries is the known gap re-found on new programs; a
tag it does not carry is a bypass family nobody has read, on a program nobody
could read, and it is a divergence. Scoring this direction against ZERO would
red on `false-admit/TYPE`, which a prototype draw hit with `type Str = Str` --
the same family as an already-baselined fixture.

WHAT IS MEASURED BUT NOT SCORED
-------------------------------
The surface tells. `SURFACE_TELLS` makes the attack design 2.1.4 names
executable: a predicate over a program's bytes that a candidate could key a
shortcut on. SEPARATION -- the gap between a tell's rate on the `near:` half
and on the `inside:` half -- is enforced, because a tell that separates answers
the whole scoring question without parsing anything. RECOGNITION is reported
and not enforced, because `draw()` reads no file and so its vocabulary is a
literal in a public file: a candidate can always tell a drawn program from a
real one, and that is the residual this tool does not close.

USAGE
-----
    tools/heldout_scoring.py --seed-file /run/revl/heldout.seed \\
        --diff-base origin/main --count 200
    REVL_HELDOUT_SEED_FILE=... tools/heldout_scoring.py \\
        --diff-paths src/revl/lower.py
    tools/heldout_scoring.py --seed-file ... --diff-base origin/main \\
        --json verdict.json

Exit status: 0 scored clean, 1 scored and a zero-tolerance bucket is
non-empty, 2 REFUSED (no score was produced).
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import random
import subprocess
import sys
from pathlib import Path

try:  # POSIX. A platform without it still enforces reuse, just not atomically.
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None

ROOT = Path(__file__).resolve().parents[1]


def _git_env():
    """The environment every git call here gets.

    `GIT_DIR` and its friends are inherited from hooks and address a different
    repository than `-C` names, which would let a hook redirect the seed
    search. They are removed. `LC_ALL=C` because the location check reads one
    English phrase from git's stderr and a translated message must not turn a
    refusal into an accept.
    """
    env = {key: value for key, value in os.environ.items()
           if not key.startswith("GIT_")}
    env["LC_ALL"] = "C"
    env["LANG"] = "C"
    return env


def _evaluator_protocol():
    """`tools/evaluator_protocol.py` from THIS checkout, loaded by path.

    The scorer runs from its own checkout; it must not import the candidate's
    compiler, and `src/revl/kernel_boundary.py` is loaded by the helper rather
    than through `revl/__init__.py` for that reason. Loading the helper by path
    keeps this module's import closure to files it names, which the scorer's
    own closure test classifies: `tools/evaluator_protocol.py` is on the fence
    and the one file it loads is under `src/`. Registered in `sys.modules`
    before `exec_module` because `kernel_boundary.py` declares a dataclass.
    """
    name = ("_revl_heldout_evaluator_protocol_"
            + hashlib.sha1(str(ROOT).encode("utf-8")).hexdigest()[:12])
    existing = sys.modules.get(name)
    if existing is not None:
        return existing
    path = ROOT / "tools" / "evaluator_protocol.py"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load the evaluator protocol from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return module


_PROTOCOL = _evaluator_protocol()

# Exit statuses. `REFUSED` is distinct from `DIVERGENT` on purpose: a promotion
# treats both as not-a-pass, but a reader has to be able to tell "the gate ran
# and found something" from "the gate declined to run", and a single non-zero
# status collapses exactly the distinction this tool exists to make.
OK = 0
DIVERGENT = 1
REFUSED = 2

# ------------------------------------------------------------------- the fence

# The paths that decide WHAT IS DRAWN and WHAT COUNTS AS A DIVERGENCE. A
# candidate diff that touches one of these is refused rather than scored.
#
# This is the `held_out` VIEW of `EVALUATOR_PATHS` (issue #2802). The one
# enumeration lives next to `KERNEL_PATHS` in `src/revl/kernel_boundary.py`, and
# `evaluator_protocol` loads it without importing the compiler. It is not a copy
# and it is not a second list: a path added to the enumeration and tagged
# `held_out` arrives here.
#
# What is NOT here is as load-bearing as what is: `selfhost/`, `src/revl/`,
# `backends/` and `crates/` are the SUBJECT. A candidate improving the gate has
# to be able to change them, and fencing them would forbid the work this loop
# exists to produce. The fence is the scorer, never the subject.
HELD_OUT_FENCE = _PROTOCOL.HELD_OUT_FENCE

#: `sha256:<hex>` over this scorer's evaluator, recorded in every verdict.
protocol_digest = _PROTOCOL.protocol_digest

# The subject: the top-level directories a candidate is allowed to change and
# that the gate measures. `tests` is one of them on purpose -- a candidate that
# improves the compiler changes tests with it, and the two test files the fence
# names are exempted by the fence rather than by forbidding the directory.
# Every in-repo file a scoring run loads has to fall in one category or the
# other; `tests/test_heldout_scoring.py` asserts the closure carries no third,
# so a dependency added without a decision is a RED rather than a silent hole.
SUBJECT_ROOTS = (
    "src",
    "selfhost",
    "backends",
    "crates",
    "stdlib",
    "tests",
    "examples",
    "tck",
    "dogfood",
    "demo",
)

# Repo files a scoring run loads that `sys.modules` does not show, because they
# are read as data or executed through `spec_from_file_location` without being
# registered. They are listed rather than discovered so the closure check can
# be complete; `tests/test_heldout_scoring.py` classifies each one.
DATA_INPUTS = (
    "selfhost/lower.rvl",
    "backends/python/emit.py",
    "tools/build_gate_crate.py",
)

# Repo paths the fenced files name that a scoring run never reaches: they
# belong to `--check`, `--record`, `--fuzz`, `--engine crate` and crate
# generation, modes `run()` does not call. Declared rather than assumed, so a
# future edit that starts reaching one fails the classification test instead of
# widening the fence silently.
#
# `corpus_provenance.py` is loaded by `gate_reference_census.py::_provenance`,
# which only `provenance_report()` calls and only the census's own `main()`
# calls that; `run()` reaches neither, and the census says in its own docstring
# that provenance is reporting-only and moves no verdict.
#
# `pyproject.toml` is read by `build_gate_crate.py::language_version()`, called
# from `build()` when the crate is generated. A scoring run executes that
# module for its frontier and admission tables and never calls `build()`.
#
# `affected_tests.py` is loaded by `tests/test_heldout_scoring.py` to hold the
# pre-merge selection, never by `run()`. It is NOT fenced: it decides nothing
# about what is drawn or what counts as a divergence, and a candidate that
# adds a test has to be able to change it, which is the line
# `test_the_fence_is_not_the_subject` draws.
#
# `gate_reference_line_ledger.json` (issue #1965) is read by the census's
# `--check` / `--record-lines` and by `tests/test_gate_reference_census.py`,
# never by `run()`: the scorer counts verdict divergences, not lines, so the
# ledger decides nothing a scoring run draws.
#
# `.github/workflows/ci.yml` is read by `tests/test_heldout_scoring.py` to pin
# how CI treats the `diff-too-large` refusal, never by `run()`: the workflow
# decides how a refusal is reported, not what is drawn or what counts as a
# divergence.
SCORING_UNREACHED = (
    "tools/fuzz_frontend.py",
    "tools/corpus_provenance.py",
    "tools/affected_tests.py",
    "pyproject.toml",
    "tools/gate_reference_line_ledger.json",
    ".github/workflows/ci.yml",
)


def classify(path: str):
    """`"fence"`, `"subject"`, `"unreached"` or None for a repo-relative path.

    None is the answer that matters: an in-repo file a scoring run loads and
    that is in no category is a hole in the fence, and the classification test
    turns it into a RED rather than leaving it to be noticed.
    """
    norm = path.replace("\\", "/").strip("/")
    if norm in HELD_OUT_FENCE:
        return "fence"
    if norm in SCORING_UNREACHED:
        return "unreached"
    if norm.split("/", 1)[0] in SUBJECT_ROOTS:
        return "subject"
    return None

# A seed shorter than this is not searched for in the tree: a short string
# matches somewhere by accident and the refusal would be noise rather than a
# finding. It is refused outright instead.
MIN_SEED_CHARS = 16

# The seed arrives as a FILE PATH, never as a literal value. A path is the only
# shape that can be checked against the tree and against its own permission
# bits; a literal seed passed on a command line or through an environment
# variable leaves nothing to check, so it is not accepted at all.
SEED_FILE_ENV = "REVL_HELDOUT_SEED_FILE"

# A seed is a short string. A file bigger than this is a path the operator did
# not mean to point at, and it is refused rather than read.
MAX_SEED_FILE_BYTES = 4096

GRAMMAR = "admission-surface/v1"

# The rotation this slice implements, recorded in the verdict so the claim is
# in the artifact rather than in prose: a scored run consumes the seed's
# digest in a ledger outside the tree, and a digest the ledger has already seen
# is refused. Rotating is still the operator's act -- writing a new seed file
# is what rotates -- but it can no longer happen silently.
ROTATION = (
    "per scored run; the seed is a file outside every git repository, its digest is "
    "recorded, and a digest the rotation ledger has already seen is refused")


# ------------------------------------------------------------- the refusal type

class Refusal(Exception):
    """A named reason this run produced no score."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


# --------------------------------------------------------------------- the seed

def seed_file_material(argv_seed_file, env):
    """The seed file's path and where it came from, or a refusal."""
    if argv_seed_file:
        return Path(argv_seed_file).expanduser(), "argv"
    from_env = env.get(SEED_FILE_ENV, "")
    if from_env:
        return Path(from_env).expanduser(), "env"
    raise Refusal("no-seed")


def _resolved(path: Path) -> Path:
    """`path.resolve()`, or a refusal when the path cannot be canonicalised."""
    try:
        return path.resolve()
    except (OSError, RuntimeError) as exc:
        raise Refusal("seed-file-unresolvable:" + type(exc).__name__)


def _in_git_work_tree(path: Path, root: Path) -> bool:
    """True when `path` is inside this checkout or any other git repository.

    `path` need not exist: the nearest existing ancestor is the directory git
    is asked about, which is also what catches a seed path an operator has not
    created inside a checkout. The PROBE runs from the resolved path, so a
    symlink whose target is inside a repository is refused rather than slipping
    through on the spelling of the link. `root` is checked lexically as well, so
    a git that cannot be queried never turns the answer into a false negative
    for the checkout this run is actually scoring. Only git's explicit "not a
    git repository" means outside; any other non-zero status is a refusal,
    because "the repository could not be inspected" is not "there is none".
    """
    root = root.resolve()
    resolved = _resolved(path)
    if resolved == root or root in resolved.parents:
        return True
    probe = resolved
    while not probe.is_dir():
        if probe.parent == probe:
            return False
        probe = probe.parent
    try:
        done = subprocess.run(
            ["git", "-C", str(probe), "rev-parse", "--git-dir"],
            capture_output=True, text=True, timeout=120, env=_git_env())
    except (OSError, subprocess.SubprocessError) as exc:
        raise Refusal("seed-location-unavailable:" + type(exc).__name__)
    if done.returncode == 0:
        return True
    if done.returncode == 128 and "not a git repository" in (done.stderr or ""):
        return False
    raise Refusal("seed-location-unavailable:git-exit-" + str(done.returncode))


def seed_is_in_tree(seed: str, root: Path) -> bool:
    """True when `seed` occurs verbatim in the tracked tree.

    `git grep` over the index rather than a walk of the working directory: the
    question is whether a candidate reading the repository can read the seed,
    and what a candidate reads is what is tracked.
    """
    try:
        done = subprocess.run(
            ["git", "-C", str(root), "grep", "--fixed-strings", "--quiet",
             "--no-color", "-I", "--", seed],
            capture_output=True, text=True, timeout=120, env=_git_env())
    except (OSError, subprocess.SubprocessError) as exc:
        raise Refusal("seed-check-unavailable:" + type(exc).__name__)
    if done.returncode == 0:
        return True
    if done.returncode == 1:
        return False
    raise Refusal("seed-check-unavailable:git-exit-" + str(done.returncode))


def checked_seed(argv_seed_file, env, root: Path):
    """`(seed, source, seed_file)` for a seed that is usable, or a refusal.

    These refusals are the whole of enforcement 2, and they are all
    fail-closed: no seed file refuses, a file inside a repository refuses, a
    file another user can read refuses, a file that cannot be read refuses, a
    short seed refuses, a seed that is in the tree refuses, and a tree that
    cannot be searched refuses.
    """
    path, source = seed_file_material(argv_seed_file, env)
    if _in_git_work_tree(path, root):
        raise Refusal("seed-file-in-tree")
    path = _resolved(path)
    if not path.is_file():
        raise Refusal("seed-file-missing")
    try:
        info = path.stat()
    except OSError as exc:
        raise Refusal("seed-file-unreadable:" + type(exc).__name__)
    if info.st_mode & 0o077:
        raise Refusal("seed-file-readable-by-others")
    if info.st_size > MAX_SEED_FILE_BYTES:
        raise Refusal("seed-file-too-large")
    try:
        seed = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError) as exc:
        raise Refusal("seed-file-unreadable:" + type(exc).__name__)
    if not seed:
        raise Refusal("seed-empty")
    if len(seed) < MIN_SEED_CHARS:
        raise Refusal("seed-too-short")
    if seed_is_in_tree(seed, root):
        raise Refusal("seed-in-tree")
    return seed, source, path


def seed_digest(seed: str) -> str:
    return "sha256:" + hashlib.sha256(seed.encode("utf-8")).hexdigest()


# ------------------------------------------------------------- the rotation

# The ledger lives beside the seed file, which enforcement 2 already requires to
# be outside every git repository. It holds digests only, never seeds.
ROTATION_LEDGER_SUFFIX = ".rotations"


def rotation_ledger_path(seed_path: Path) -> Path:
    return seed_path.with_name(seed_path.name + ROTATION_LEDGER_SUFFIX)


def _ledger_digests(handle):
    digests = []
    for line in handle:
        digest = line.strip()
        if not digest:
            continue
        if (not digest.startswith("sha256:")
                or len(digest) != len("sha256:") + 64):
            raise Refusal("rotation-ledger-corrupt")
        digests.append(digest)
    return digests


def seed_digest_was_used(ledger: Path, digest: str) -> bool:
    """True when `digest` is already in the ledger.

    A missing ledger is an empty history, not a refusal: the first scored run
    creates it. An unreadable or corrupt ledger refuses, because a history the
    tool cannot read cannot answer the reuse question.
    """
    if not ledger.exists():
        return False
    try:
        fd = os.open(str(ledger), os.O_RDONLY)
    except OSError as exc:
        raise Refusal("rotation-ledger-unavailable:" + type(exc).__name__)
    try:
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_SH)
        with os.fdopen(fd, "r", encoding="utf-8") as handle:
            return digest in _ledger_digests(handle)
    except OSError as exc:
        raise Refusal("rotation-ledger-unavailable:" + type(exc).__name__)


def record_rotation(ledger: Path, digest: str) -> int:
    """Append `digest` and return the rotation count, or refuse.

    Checked and appended under one exclusive lock so two runs of the same seed
    cannot both claim it. The count is the number of consumed digests,
    including this one, and it is what the verdict records.
    """
    try:
        fd = os.open(str(ledger), os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o600)
    except OSError as exc:
        raise Refusal("rotation-ledger-unavailable:" + type(exc).__name__)
    try:
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_EX)
        with os.fdopen(fd, "r+", encoding="utf-8") as handle:
            handle.seek(0)
            digests = _ledger_digests(handle)
            if digest in digests:
                raise Refusal("seed-digest-reused")
            handle.write(digest + "\n")
            handle.flush()
            os.fsync(handle.fileno())
            return len(digests) + 1
    except OSError as exc:
        raise Refusal("rotation-ledger-unavailable:" + type(exc).__name__)


# --------------------------------------------------------------------- the fence

# A diff is bounded before it is scored. The bound is on the changed-file set
# the fence already receives and, when a base is available, on the number of
# lines added and removed. An unbounded diff is a review problem the held-out
# draw cannot answer, and it is refused with the limit named rather than
# silently scored.
MAX_DIFF_FILES = 200
MAX_DIFF_LINES = 2000

# Regenerated artifacts are left out of the LINE count: regenerating one is not
# a candidate's work and can add thousands of lines by itself (the census
# reproduction records and the crate's generated selfhost module are the
# measured offenders). The changed-FILE count still includes them, because a
# diff that touches two hundred files is a review problem whatever they are.
# `selfhost/lower.rvl` is deliberately NOT here: it is a source, and the
# regeneration registry's entry for it is a sort, not a generation.
REGENERATED_PATHS = (
    "crates/revl-gate/src/selfhost.rs",
    "tests/fixtures/corpus_provenance.json",
)
REGENERATED_PREFIXES = (
    "docs/census-artifact/",
    "tests/fixtures/census_crate_reproduction/",
)


def is_regenerated(path: str) -> bool:
    """True for a path whose line churn is a regeneration, not an edit."""
    norm = path.replace("\\", "/").strip()
    return (norm in REGENERATED_PATHS
            or any(norm.startswith(prefix) for prefix in REGENERATED_PREFIXES))


def fence_verdict(changed, fence=HELD_OUT_FENCE, diff_lines=None):
    """The fenced paths a candidate's changed-file set reaches, in order.

    `changed` is repo-relative. A path outside the fence is not inspected: this
    is a reachability question about the scorer, not a review of the diff. The
    FENCE is checked before the size bound, so a large diff that reaches the
    scorer keeps the `diff-reaches-fence` refusal (which CI treats as a
    warning) instead of becoming a size error. A changed-file set or a line
    count over the size limit, on a diff that does not reach the fence, refuses
    with the limit named; the line half is skipped when no base supplied it
    (`--diff-paths`), never assumed to be zero.
    """
    reached = []
    for path in changed:
        norm = path.replace("\\", "/").lstrip("./")
        if norm in fence:
            reached.append(norm)
    if reached:
        return reached
    if len(changed) > MAX_DIFF_FILES:
        raise Refusal("diff-too-large:files:" + str(len(changed))
                      + ">" + str(MAX_DIFF_FILES))
    if diff_lines is not None and diff_lines > MAX_DIFF_LINES:
        raise Refusal("diff-too-large:lines:" + str(diff_lines)
                      + ">" + str(MAX_DIFF_LINES))
    return reached


def changed_from_base(root: Path, base: str):
    """The candidate's changed-file set, from git, or a refusal.

    Two dots, not three: a three-dot diff is against the merge base, which
    hides a scorer edit that arrived by merging main. The question this fence
    asks is "what does the tree the gate is about to run differ from", and that
    is the two-dot form.
    """
    try:
        done = subprocess.run(
            ["git", "-C", str(root), "diff", "--name-only", base + "..HEAD"],
            capture_output=True, text=True, timeout=120, env=_git_env())
    except (OSError, subprocess.SubprocessError) as exc:
        raise Refusal("diff-unavailable:" + type(exc).__name__)
    if done.returncode != 0:
        raise Refusal("diff-unavailable:git-exit-" + str(done.returncode))
    return [line for line in done.stdout.splitlines() if line.strip()]


def diff_lines_from_base(root: Path, base: str):
    """Lines added plus lines removed in `base..HEAD`, or a refusal.

    `--numstat` rather than `--shortstat` so a binary file (which `git` reports
    as `-`) can be counted as zero lines instead of poisoning the sum. Paths in
    `REGENERATED_PATHS` / `REGENERATED_PREFIXES` are skipped: their churn is a
    regeneration, not a candidate's edit. The bound this feeds is a review
    bound, not an exactness claim: a candidate that changes ten thousand lines
    is not one a 200-program draw can judge.
    """
    try:
        done = subprocess.run(
            ["git", "-C", str(root), "diff", "--numstat", base + "..HEAD"],
            capture_output=True, text=True, timeout=120, env=_git_env())
    except (OSError, subprocess.SubprocessError) as exc:
        raise Refusal("diff-unavailable:" + type(exc).__name__)
    if done.returncode != 0:
        raise Refusal("diff-unavailable:git-exit-" + str(done.returncode))
    total = 0
    for line in done.stdout.splitlines():
        fields = line.split("\t")
        if len(fields) < 3:
            continue
        if is_regenerated(fields[2]):
            continue
        for field in fields[:2]:
            if field.isdigit():
                total += int(field)
    return total


# ------------------------------------------------------------------- the grammar

# The admission surface: the region `crates/revl-gate` will ISSUE an admission
# for. The generator below draws from its INTERFACE half — service signatures and
# transparent scalar aliases, the part that carries no term at all. The surface
# also holds components whose provide-method bodies the certifier types itself
# (docs/design/457 T6); drawing those is a separate generator and not this one,
# so a program from here is inside the surface but does not exhaust it.
_SCALARS = ("Int", "Int32", "Float", "Str", "Bool", "Bytes")

# Deliberately ordinary words. A generated program that is obviously generated
# invites a candidate to recognise the shape rather than to be correct over it;
# that is not a defence and the design doc says so, but there is no reason to
# hand the recognition away for free either.
# Every one is checked against `revl.lexer.KEYWORDS` by the test suite: the
# first draft of this list carried `emit`, and eight of two hundred programs
# that claimed to be inside the admission surface were parse errors instead --
# a generator that quietly emits programs outside its own surface shrinks the
# guard without saying so.
_WORDS = (
    "store", "clock", "log", "cache", "queue", "meter", "vault", "index",
    "router", "ledger", "probe", "relay", "spool", "tally", "warden",
    "beacon", "cursor", "digest", "signal", "fold", "shard", "anchor",
    "ripple", "gantry", "lantern", "kernel", "pilot", "quay",
)
_SUFFIXES = ("", "_a", "_b", "_c", "_hi", "_lo", "2", "3", "x")

# The lexer's keyword table, FROZEN HERE rather than imported.
#
# The invariant is the one the bring-up defect taught: a generated identifier
# that is a keyword is a parse error wearing the label `inside:`, and eight of
# two hundred programs were exactly that before the word list was checked. The
# check has to survive the language growing a keyword, so it is a check and not
# a one-time audit -- `_ident` refuses a reserved word at generation time.
#
# It is a literal copy and NOT `from revl.lexer import KEYWORDS` on purpose.
# `draw()` is a pure function of `(seed, count)` that reads no file, which is
# what answers the attack "the candidate makes the generator depend on
# something it controls" (design 2.1.2): `src/revl/lexer.py` is SUBJECT, so a
# candidate that could make the draw depend on it could narrow the draw by
# editing it. The copy is held against the real table by
# `test_the_reserved_set_covers_every_lexer_keyword`, so a new keyword reds the
# suite here instead of silently degrading a draw.
_RESERVED = frozenset((
    "acquire", "after", "as", "assert", "async", "await", "break",
    "commutative", "compensate", "component", "config", "continue", "effect",
    "else", "emission", "emit", "every", "extern", "fail", "false", "fn",
    "for", "handoff", "hole", "idempotent", "if", "in", "intercept",
    "isolate", "let", "match", "null", "of", "provide", "provides", "pub",
    "pure", "realm", "requires", "return", "service", "spawn", "subscribe",
    "test", "true", "try", "type", "undo", "use", "var", "verified", "while",
    "with",
))

# The near-miss families. Each is one token outside the admission surface, and
# each is a refusal the reference issues, so an issued admission for any of
# them is a `false-admission` -- the bucket with no allowance at all. These
# generalise the fourteen hand-written `ADMISSION_PROGRAMS` in the census tool
# from a list a candidate can memorise into a space it has to be correct over.
NEAR_MISS_FAMILIES = (
    "duplicate_service",
    "duplicate_method",
    "fn_body",
    "generic_type",
    "record_alias",
    "unknown_type",
    "component",
)


def _ident(rng, used):
    for _ in range(64):
        word = rng.choice(_WORDS) + rng.choice(_SUFFIXES)
        if word in _RESERVED or word in used:
            continue
        used.add(word)
        return word
    raise Refusal("draw-exhausted-identifiers")


def _cap(word: str) -> str:
    return word[0].upper() + word[1:]


def _inside(rng):
    """`(source, service_names, used_identifiers)` strictly inside the surface.

    `used` is returned rather than discarded because `_near_miss` builds on
    this program and has to draw its own identifiers from the same vocabulary
    without colliding with the names already declared here. Slice 1 spelled the
    near-miss names as literals; see `SURFACE_TELLS` for why that was the
    tell that mattered and this is not.
    """
    used = set()
    lines = []
    aliases = []
    for _ in range(rng.randint(0, 3)):
        name = _cap(_ident(rng, used))
        aliases.append(name)
        lines.append("type " + name + " = " + rng.choice(_SCALARS))
    if aliases:
        lines.append("")
    spellings = _SCALARS + tuple(aliases)
    services = []
    for _ in range(rng.randint(1, 3)):
        name = _cap(_ident(rng, used))
        services.append(name)
        method_names = set()
        body = []
        for _ in range(rng.randint(0, 4)):
            method = _ident(rng, method_names)
            params = ", ".join(
                _ident(rng, method_names) + ": " + rng.choice(spellings)
                for _ in range(rng.randint(0, 3)))
            ret = ""
            if rng.random() < 0.6:
                ret = " -> " + rng.choice(spellings)
            body.append("  fn " + method + "(" + params + ")" + ret)
        lines.append("service " + name + " {")
        lines.extend(body)
        lines.append("}")
        lines.append("")
    if rng.random() < 0.3:
        lines.insert(0, "// " + rng.choice(_WORDS) + " " + rng.choice(_WORDS))
    return "\n".join(lines).rstrip() + "\n", services, used


def _near_miss(rng, family, base, services, used):
    """`base` pushed one token outside the surface, or None when `base` has no
    site for `family` (a draw with no service cannot duplicate one).

    EVERY NAME HERE IS DRAWN, none is a literal. Slice 1 spelled them out --
    `twice`, `Box`, `Row`, `Mystery`, `Edge`, `Comp`, `dup`, `probe`, `id`,
    `all` -- and those ten words separated `near:` from `inside:` perfectly,
    with a regex and without parsing anything. That is the whole scoring
    question answered by a lookup, which is the `SURFACE_TELLS` entry
    `near-miss-name-literals` and the reason it is now measured.

    What stays fixed is STRUCTURE, not names: a generic application, a record
    body, a fn body, a component block, a duplicate declaration. Those are what
    puts the program outside the admission surface, so a gate that keys on them
    is giving the right answer for the right reason rather than shortcutting.
    """
    if family == "duplicate_service":
        if not services:
            return None
        name = rng.choice(services)
        return (base + "\nservice " + name + " {\n  fn " + _ident(rng, used)
                + "(" + _ident(rng, used) + ": " + rng.choice(_SCALARS)
                + ") -> " + rng.choice(_SCALARS) + "\n}\n")
    if family == "duplicate_method":
        marker = "\n  fn "
        if marker not in base:
            return None
        head, rest = base.split(marker, 1)
        name = rest.split("(", 1)[0]
        line = (marker + name + "(" + _ident(rng, used) + ": "
                + rng.choice(_SCALARS) + ") -> " + rng.choice(_SCALARS))
        return head + line + marker + rest
    if family == "fn_body":
        arg = _ident(rng, used)
        return (base + "\nfn " + _ident(rng, used) + "(" + arg
                + ": Int) -> Int { return " + arg + " + " + arg + " }\n")
    if family == "generic_type":
        return (base + "\nservice " + _cap(_ident(rng, used)) + " {\n  fn "
                + _ident(rng, used) + "() -> List[" + rng.choice(_SCALARS)
                + "]\n}\n")
    if family == "record_alias":
        return (base + "\ntype " + _cap(_ident(rng, used)) + " = { "
                + _ident(rng, used) + ": " + rng.choice(_SCALARS) + " }\n")
    if family == "unknown_type":
        return (base + "\nservice " + _cap(_ident(rng, used)) + " {\n  fn "
                + _ident(rng, used) + "(" + _ident(rng, used) + ": "
                + _cap(_ident(rng, used)) + ") -> " + rng.choice(_SCALARS)
                + "\n}\n")
    if family == "component":
        if not services:
            return None
        name = services[0]
        port = _ident(rng, used)
        return (base + "\ncomponent " + _cap(_ident(rng, used)) + " provides "
                + port + ": " + name + " {\n  provide " + port + " {\n  }\n}\n")
    raise Refusal("draw-unknown-family:" + family)


def draw(seed: str, count: int):
    """`[(case_id, source)]` -- the held-out set for this run.

    A pure function of `(seed, count)` that touches no file. An auditor handed
    the seed reproduces the draw byte for byte; a candidate without it cannot,
    and that asymmetry is the whole of the property.
    """
    if count < 2:
        raise Refusal("draw-too-small")
    rng = random.Random(int.from_bytes(hashlib.sha256(seed.encode("utf-8")).digest(), "big"))
    cases = []
    index = 0
    while len(cases) < count:
        base, services, used = _inside(rng)
        cases.append(("inside:" + str(index), base))
        if len(cases) == count:
            break
        family = rng.choice(NEAR_MISS_FAMILIES)
        variant = _near_miss(rng, family, base, services, used)
        if variant is not None:
            cases.append(("near:" + family + ":" + str(index), variant))
        index += 1
    return cases


def draw_digest(cases) -> str:
    """A digest over the draw, so a verdict's claim about what was scored is
    checkable by anyone later handed the seed."""
    hasher = hashlib.sha256()
    for case_id, source in cases:
        hasher.update(case_id.encode("utf-8"))
        hasher.update(b"\0")
        hasher.update(source.encode("utf-8"))
        hasher.update(b"\0")
    return "sha256:" + hasher.hexdigest()


# -------------------------------------------------------- the surface tells

# The attack design 2.1.4 names and slice 1 left untested: a candidate that
# reads the public grammar and SPECIAL-CASES ITS SURFACE would score clean
# while being wrong on everything else. A tell is that attack made executable
# -- a predicate over a program's bytes, computable by anyone holding this
# file, that a candidate could key a shortcut on.
#
# Two different quantities come out of a tell, and conflating them is how this
# stayed unmeasured. RECOGNITION is how often the tell fires on the draw at
# all: "is this program generated". SEPARATION is the gap between its rate on
# the `near:` half and its rate on the `inside:` half: "is this program the one
# I am supposed to refuse".
#
# SEPARATION is the dangerous one and it is the one that is enforced. A tell
# that separates answers the entire scoring question without parsing anything,
# so a gate keying on it scores clean with no correctness at all. A draw
# carrying one is not the set it claims to be, so it is REFUSED -- the same
# treatment `draw-inside-refused-by-reference` gives a draw that mislabels
# itself.
#
# RECOGNITION is measured and reported and deliberately NOT enforced, because
# it cannot be driven down by this mechanism and a bound nobody can meet is a
# bound somebody deletes. `draw()` reads no file, by construction (2.1.2), so
# its vocabulary is a literal in a public file and `closed-vocabulary` fires on
# every program it will ever emit. Closing that needs the draw to be
# in-distribution with real programs, which is a property of the grammar and
# needs the type layer; it stays the named residual and the number is now in
# the record instead of in prose.

# The ten names slice 1 spelled as literals in `_near_miss`. Kept here, after
# they were removed from the generator, precisely so the tell stays executable:
# a measurement whose subject has been deleted measures nothing, and this one
# has to keep reding if a later edit reintroduces a fixed near-miss name.
_SLICE1_NEAR_MISS_NAMES = (
    "twice", "Box", "Row", "Mystery", "Edge", "Comp", "dup", "probe",
    "id", "all",
)

_IDENT_CHARS = set(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_")


def _identifiers(source: str):
    """Every identifier-shaped token in `source`, comments included.

    Deliberately a scanner and not the real lexer: the question a tell asks is
    what a candidate can compute from the bytes cheaply, and a candidate
    writing a shortcut writes a regex, not a parser.
    """
    out = []
    token = []
    for char in source:
        if char in _IDENT_CHARS:
            token.append(char)
            continue
        if token:
            out.append("".join(token))
            token = []
    if token:
        out.append("".join(token))
    return [t for t in out if t and not t[0].isdigit()]


def _tell_near_miss_name_literals(source: str) -> bool:
    """A fixed name that only ever appeared in a near miss."""
    words = set(_identifiers(source))
    return any(name in words for name in _SLICE1_NEAR_MISS_NAMES)


def _closed_vocabulary():
    out = set()
    for word in _WORDS:
        for suffix in _SUFFIXES:
            out.add(word + suffix)
            out.add(_cap(word + suffix))
    return out


def _tell_closed_vocabulary(source: str) -> bool:
    """Every identifier comes from this file's own word list.

    The un-closeable one. `_WORDS` x `_SUFFIXES` is 252 spellings and it is
    printed above, so a candidate builds the lookup table in one line.
    """
    vocabulary = _closed_vocabulary() | set(_SCALARS) | {"List"} | _RESERVED
    words = [w for w in _identifiers(source) if w not in _RESERVED]
    return bool(words) and all(w in vocabulary for w in words)


def _tell_fixed_comment_template(source: str) -> bool:
    """A comment matching the one template slice 1 emitted."""
    for line in source.splitlines():
        stripped = line.strip()
        if stripped.startswith("//") and stripped.endswith(" surface"):
            return True
    return False


SURFACE_TELLS = {
    "near-miss-name-literals": _tell_near_miss_name_literals,
    "closed-vocabulary": _tell_closed_vocabulary,
    "fixed-comment-template": _tell_fixed_comment_template,
}

# How far a tell's `near:` rate may sit from its `inside:` rate before the draw
# is refused. A tell that separates perfectly scores 1.0; slice 1's
# `near-miss-name-literals` did exactly that. The bound is not 0.0 because a
# tell can correlate slightly by chance on a small draw -- a near miss is a
# longer program and any length-sensitive predicate drifts -- and a bound that
# reds on noise is a bound somebody widens until it means nothing.
SEPARATION_BOUND = 0.25


def tell_report(cases):
    """`{tell: {recognition, inside_rate, near_rate, separation}}` over a draw.

    Pure string work over the draw, so it costs nothing next to a scoring run
    and can be computed on every invocation rather than in a test that a
    candidate's CI might not run.
    """
    report = {}
    inside = [src for cid, src in cases if cid.startswith("inside:")]
    near = [src for cid, src in cases if cid.startswith("near:")]
    for name, predicate in sorted(SURFACE_TELLS.items()):
        inside_hits = sum(1 for src in inside if predicate(src))
        near_hits = sum(1 for src in near if predicate(src))
        inside_rate = (inside_hits / len(inside)) if inside else 0.0
        near_rate = (near_hits / len(near)) if near else 0.0
        total = len(inside) + len(near)
        report[name] = {
            "recognition": ((inside_hits + near_hits) / total) if total else 0.0,
            "inside_rate": inside_rate,
            "near_rate": near_rate,
            "separation": abs(near_rate - inside_rate),
        }
    return report


def separating_tells(report, bound=SEPARATION_BOUND):
    """The tells that answer the scoring question on their own, worst first."""
    over = [(name, stats["separation"]) for name, stats in report.items()
            if stats["separation"] > bound]
    return [name for name, _ in sorted(over, key=lambda pair: -pair[1])]


# -------------------------------------------------------------------- the score

def load_census():
    """`tools/gate_reference_census.py`, imported by path.

    The bucket vocabulary, the reference and the fast gate engine are the
    census's, not a second copy: two classifiers would be free to drift into
    disagreeing about what a divergence even is, and this tool's whole claim is
    that it measures the same thing the census measures on a set the candidate
    could not read.
    """
    path = ROOT / "tools" / "gate_reference_census.py"
    spec = importlib.util.spec_from_file_location("heldout_census", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:  # a scorer that will not build must not score
        raise Refusal("engine-unavailable:" + type(exc).__name__)
    return module


# The buckets with no allowance anywhere in the census: `false-admission` is in
# its `NEVER_BASELINED`, and a `gate-fault` is a crash rather than a verdict.
ZERO_TOLERANCE = ("false-admission", "gate-fault")

# ------------------------------------------------------ the bypass direction

# `false-admit/<tag>` is the census's no-objection bypass: the reference
# refuses in-slice and the gate raises no objection. Unlike `false-admission`
# it HAS an allowance, and slice 1 left it out of the predicate for a measured
# reason -- a prototype family drew `type Str = Str`, produced
# `false-admit/TYPE`, and that is the same tag and family as the in-tree
# `examples/rejections/t18_type_alias_cycle.rvl` that is already one of the
# baseline's three. Scoring this direction against ZERO would red on a gap the
# repository has already recorded and read, which is a finding about the
# baseline and not about the candidate.
#
# So the predicate is the same one `tests/test_gate_crate_admit.py` uses on the
# crate: compare against the BASELINE'S FAMILIES, by name. A `false-admit/<tag>`
# whose tag the baseline already carries is the known gap re-found on new
# programs and is reported without being a finding. A tag the baseline does NOT
# carry is a bypass family nobody has read, found on a program nobody could
# read, and it is a divergence.
BYPASS_BUCKET = "false-admit"


def bypass_allowance(root=ROOT):
    """The `false-admit` tags the census baseline already allows.

    Read from the baseline FILE rather than copied into this tool: a second
    list would drift, and the claim this makes is precisely "no family beyond
    the ones the repository has already recorded and read". The baseline is on
    `HELD_OUT_FENCE` for the same reason, so a candidate cannot widen its own
    allowance without being refused.

    An empty allowance is a legitimate answer and is not a refusal; an
    unreadable one is, because a scorer that cannot find its allowance would
    otherwise have to choose between failing every candidate and passing every
    one, and both of those are worse than declining to answer.
    """
    path = root / "tools" / "gate_reference_census_baseline.json"
    try:
        blob = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise Refusal("bypass-allowance-unavailable:" + type(exc).__name__)
    buckets = blob.get("buckets")
    if not isinstance(buckets, dict):
        raise Refusal("bypass-allowance-unavailable:no-buckets")
    return frozenset(tag for tag in (_bypass_tag(name) for name in buckets)
                     if tag)


def _bypass_tag(name: str):
    """The `<tag>` of a `false-admit/<tag>` bucket, else None.

    Split on the separator, never `startswith`: `false-admission` carries
    `false-admit` as a string prefix and the census's own helper documents the
    same trap.
    """
    head, sep, tag = name.partition("/")
    if head == BYPASS_BUCKET and sep and tag:
        return tag
    return None


def bypass_families(buckets):
    """`{tag: [case_id]}` for every `false-admit/<tag>` on the draw."""
    out = {}
    for name, ids in buckets.items():
        tag = _bypass_tag(name)
        if tag:
            out.setdefault(tag, []).extend(ids)
    return {tag: sorted(ids) for tag, ids in sorted(out.items())}


def score(cases, census, engine=None, reference=None):
    """`(buckets, liveness)` over the draw.

    `liveness` is the half that keeps a clean verdict from being a vacuum. The
    census bucket `agree-admit` conflates two different gate answers -- an
    ISSUED admission and a mere no-objection where the reference also admits --
    so an empty `false-admission` bucket is consistent both with a sound gate
    and with a draw that never reached the admission arm at all. The counters
    below separate those, and `run()` refuses on the second.
    """
    try:
        if reference is None:
            reference, _oracle = census._reference()
        if engine is None:
            engine = census.SelfhostEngine()
    except Refusal:
        raise
    except Exception as exc:
        raise Refusal("engine-unavailable:" + type(exc).__name__)
    sources = [src for _, src in cases]
    buckets = {}
    issued = 0
    refused_by_reference = 0
    near_miss_refused = 0
    in_slice_refused = 0
    inside_refused = []
    for (case_id, source), verdict in zip(cases, engine.verdicts(sources)):
        ref = reference(source)
        name = census.bucket(ref, verdict)
        buckets.setdefault(name, []).append(case_id)
        if verdict[0] == "admitted":
            issued += 1
        if ref[0] != "":
            refused_by_reference += 1
            # `false-admit` is reachable ONLY from an IN-SLICE reference
            # refusal: `bucket()` routes a no-objection against an `OUT:`
            # refusal to `no-objection-out-of-slice`, because a no-objection is
            # explicitly not a green. Counting these separately is what keeps
            # the bypass direction from looking live when it is not.
            if not ref[0].startswith("OUT:"):
                in_slice_refused += 1
            if case_id.startswith("near:"):
                near_miss_refused += 1
            else:
                inside_refused.append(case_id)
    liveness = {
        "issued_admissions": issued,
        "reference_refusals": refused_by_reference,
        "near_miss_reference_refusals": near_miss_refused,
        "in_slice_reference_refusals": in_slice_refused,
        "inside_reference_refusals": len(inside_refused),
    }
    return buckets, liveness


def _protocol_or_none():
    """This checkout's protocol digest, or None when even that cannot be read.

    A refusal record is the tool's report that it did not answer the question.
    It must still be writable when git is missing or the evaluator enumeration
    names a file that is not there, so the identity is reported as None rather
    than turning the refusal into a traceback."""
    try:
        return protocol_digest(ROOT)
    except (OSError, RuntimeError):
        return None


def verdict_record(buckets, liveness, *, seed, source, count, fence,
                   changed_count, allowance, tells, rotation_count, protocol):
    findings = {name: buckets.get(name, []) for name in ZERO_TOLERANCE}
    families = bypass_families(buckets)
    new_families = {tag: ids for tag, ids in families.items()
                    if tag not in allowance}
    clean = not any(findings.values()) and not new_families
    return {
        "verdict": "clean" if clean else "divergent",
        "refusal": None,
        "grammar": GRAMMAR,
        "size": count,
        "rotation": ROTATION,
        "rotation_count": rotation_count,
        "protocol_digest": protocol,
        "seed_digest": seed,
        "seed_source": source,
        "fence": list(fence),
        "candidate_changed_files": changed_count,
        "liveness": liveness,
        "zero_tolerance": findings,
        "bypass": {
            "allowance": sorted(allowance),
            "families": {tag: len(ids) for tag, ids in families.items()},
            "new_families": new_families,
            # Whether this direction COULD have fired on this draw, recorded
            # next to its result so a reader cannot mistake an empty bucket for
            # a live guard. On `admission-surface/v1` it is false: every
            # refusal the reference issues over the draw is `OUT:`, so
            # `bucket()` routes the whole direction to
            # `no-objection-out-of-slice` and `false-admit` cannot be reached.
            # The predicate is implemented and tested; the grammar it needs to
            # become live is slice 3's. Stated in the artifact rather than in
            # prose because "the check ran" and "the check could have failed"
            # are the two things this repository has most often confused.
            "reachable": liveness.get("in_slice_reference_refusals", 0) > 0,
        },
        "tells": tells,
        "buckets": {name: len(ids) for name, ids in sorted(buckets.items())},
    }


def refusal_record(reason: str):
    return {
        "verdict": "refused",
        "refusal": reason,
        "grammar": GRAMMAR,
        "size": 0,
        "rotation": ROTATION,
        "rotation_count": None,
        "protocol_digest": _protocol_or_none(),
        "seed_digest": None,
        "seed_source": None,
        "fence": list(HELD_OUT_FENCE),
        "candidate_changed_files": None,
        "liveness": {},
        "zero_tolerance": {name: [] for name in ZERO_TOLERANCE},
        "bypass": {"allowance": [], "families": {}, "new_families": {},
                   "reachable": False},
        "tells": {},
        "buckets": {},
    }


def run(*, seed_file=None, env, count, changed, diff_lines=None,
        root=ROOT, census=None, engine=None, reference=None):
    """The whole gate as one function: a verdict record and an exit status.

    Every branch that cannot answer the question returns a refusal record. No
    branch returns a score computed from anything other than the draw. The seed
    arrives only as a file path; a scored run consumes its digest in the
    rotation ledger beside that path and records the resulting count.
    """
    try:
        protocol = protocol_digest(ROOT)
    except (OSError, RuntimeError) as exc:
        # The scorer cannot name the protocol it is about to score under, so it
        # must not produce a score under it. This is a refusal, not a crash.
        return (refusal_record("protocol-digest-unavailable:"
                              + type(exc).__name__), REFUSED)
    try:
        seed, source, seed_path = checked_seed(seed_file, env, root)
        digest = seed_digest(seed)
        ledger = rotation_ledger_path(seed_path)
        if seed_digest_was_used(ledger, digest):
            raise Refusal("seed-digest-reused")
        if changed is None:
            raise Refusal("no-diff")
        reached = fence_verdict(changed, diff_lines=diff_lines)
        if reached:
            raise Refusal("diff-reaches-fence:" + ",".join(sorted(reached)))
        allowance = bypass_allowance(root)
        cases = draw(seed, count)
        # The slice-3 enforcement, before any engine is built because it is
        # pure string work and a draw that answers its own question is not
        # worth scoring. A tell that separates `near:` from `inside:` hands a
        # candidate the whole verdict without parsing anything.
        tells = tell_report(cases)
        separating = separating_tells(tells)
        if separating:
            raise Refusal("draw-separable-by-tell:" + ",".join(separating))
        if census is None:
            census = load_census()
        buckets, liveness = score(cases, census, engine=engine,
                                  reference=reference)
        # A clean verdict on a draw that never reached the guard is the
        # vacuous-pass shape, so it is a refusal rather than a pass. The gate
        # has to have ISSUED at least one admission (the arm ran) and the draw
        # has to carry at least one near miss the reference refuses (there was
        # something the arm could have got wrong).
        if liveness["issued_admissions"] == 0:
            raise Refusal("draw-reached-no-admission-arm")
        if liveness["near_miss_reference_refusals"] == 0:
            raise Refusal("draw-carries-no-refused-near-miss")
        # A program the generator labelled `inside:` that the reference refuses
        # is a generator defect, not a gate finding: the draw is not the set it
        # says it is, so it does not get to score anything.
        if liveness["inside_reference_refusals"] != 0:
            raise Refusal("draw-inside-refused-by-reference")
        # Only a run that produced a score consumes the seed. A refusal is
        # retryable with the same seed; a reused digest is not, and that is the
        # whole of the rotation enforcement.
        rotation_count = record_rotation(ledger, digest)
    except Refusal as exc:
        return refusal_record(exc.reason), REFUSED
    record = verdict_record(
        buckets,
        liveness,
        seed=digest,
        source=source,
        count=len(cases),
        fence=HELD_OUT_FENCE,
        changed_count=len(changed),
        allowance=allowance,
        tells=tells,
        rotation_count=rotation_count,
        protocol=protocol,
    )
    record["draw_digest"] = draw_digest(cases)
    return record, OK if record["verdict"] == "clean" else DIVERGENT


def render(record) -> str:
    lines = []
    if record["verdict"] == "refused":
        lines.append("held-out scoring REFUSED: " + str(record["refusal"]))
        lines.append("  no score was produced, and none was substituted.")
        return "\n".join(lines)
    lines.append("held-out scoring " + record["verdict"].upper()
                 + " over " + str(record["size"]) + " programs"
                 + " (" + record["grammar"] + ")")
    lines.append("  seed " + str(record["seed_digest"])
                 + " from " + str(record["seed_source"]))
    lines.append("  rotation " + str(record.get("rotation_count"))
                 + "  (" + str(record.get("rotation")) + ")")
    lines.append("  protocol " + str(record.get("protocol_digest")))
    lines.append("  draw " + str(record.get("draw_digest")))
    live = record.get("liveness", {})
    lines.append("  liveness: " + str(live.get("issued_admissions"))
                 + " issued admissions, "
                 + str(live.get("near_miss_reference_refusals"))
                 + " near misses the reference refuses")
    for name, ids in record["zero_tolerance"].items():
        mark = "!!" if ids else "  "
        lines.append("  " + mark + " " + name + ": " + str(len(ids)))
        for case_id in ids[:6]:
            lines.append("        " + case_id)
    bypass = record.get("bypass", {})
    new_families = bypass.get("new_families", {})
    mark = "!!" if new_families else "  "
    lines.append("  " + mark + " false-admit families beyond the baseline: "
                 + str(len(new_families))
                 + "  (allowance: "
                 + (", ".join(bypass.get("allowance", ())) or "none")
                 + "; direction "
                 + ("live" if bypass.get("reachable") else
                    "NOT REACHABLE on this grammar") + ")")
    for tag, ids in sorted(new_families.items()):
        lines.append("        " + tag + ": " + str(len(ids)) + " "
                     + ", ".join(ids[:4]))
    for tag, size in sorted(bypass.get("families", {}).items()):
        if tag not in new_families:
            lines.append("        baselined family " + tag + ": " + str(size))
    for name, stats in sorted(record.get("tells", {}).items()):
        lines.append("     tell " + name
                     + ": separation " + ("%.2f" % stats["separation"])
                     + ", recognition " + ("%.2f" % stats["recognition"]))
    for name, size in record["buckets"].items():
        lines.append("     " + str(size).rjust(6) + "  " + name)
    return "\n".join(lines)


def main(argv):
    parser = argparse.ArgumentParser(
        description="score a candidate against a set it could not read")
    parser.add_argument(
        "--seed-file", default="",
        help="owner-only seed file outside every git repository; "
             "REVL_HELDOUT_SEED_FILE otherwise")
    parser.add_argument("--count", type=int, default=200,
                        help="programs in the draw (default 200)")
    parser.add_argument("--diff-base", default="",
                        help="git ref; the candidate's changed files are "
                             "<base>..HEAD")
    parser.add_argument("--diff-paths", default="",
                        help="comma-separated changed files, instead of "
                             "--diff-base")
    parser.add_argument("--json", default="",
                        help="write the verdict record here")
    args = parser.parse_args(argv)

    changed = None
    diff_lines = None
    try:
        if args.diff_paths:
            changed = [p for p in args.diff_paths.split(",") if p.strip()]
        elif args.diff_base:
            changed = changed_from_base(ROOT, args.diff_base)
            diff_lines = diff_lines_from_base(ROOT, args.diff_base)
        record, status = run(
            seed_file=args.seed_file or None, env=os.environ,
            count=args.count, changed=changed, diff_lines=diff_lines)
    except Refusal as exc:
        record, status = refusal_record(exc.reason), REFUSED

    print(render(record))
    if args.json:
        Path(args.json).write_text(
            json.dumps(record, indent=2, sort_keys=True) + "\n",
            encoding="utf-8")
    return status


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
