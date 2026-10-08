#!/usr/bin/env python3
"""Issue #2165: a base-branch landing that moves a census input under a green
pull request run.

THE FAILURE, TWICE
------------------
`main`'s census record is keyed on `checker_version()`, a digest over exactly
the five files in `tools/census_artifact.py`'s `CHECKER_SOURCES`, and the
record for a version lives at `tests/fixtures/census_crate_reproduction/
<version>.json`. So every landing that moves a member of that set creates a
fresh obligation on the tree it lands on.

Branch protection on `main` is non-strict. `gh api
repos/inso1337/revl/branches/main/protection` reports
`required_status_checks.strict = false` and `required_status_checks.contexts =
[lint, backend-python, backend-typescript, backend-wasm, backend-rust,
backend-java, backend-go]` -- `census-artifact` is absent, so a pull request
may merge while its base moved underneath it. GitHub recomputes
*mergeability* when the base moves; it does not rebuild `refs/pull/N/merge`
for a run already in flight, and it does not re-run that run's checks. So a
`census-artifact` run can be green against a tree that will not be the tree
that lands.

That is exactly what happened twice, most recently when `#2159` (`fb1a9916e`)
and `#2162` (`f18462966`) each moved `selfhost/lower.rvl`:

    version at 651c55292 (the run's base)  GATE-CENSUS-1+9cdb91ced73b
    version at fb1a9916e (after #2159)     GATE-CENSUS-1+e5f2b02ef7b0
    version at 4973c0c5b (#2162's head)    GATE-CENSUS-1+360ef1b1dfbe
    version at f18462966 (after #2162)     GATE-CENSUS-1+4c1bae174601

`#2162`'s run measured `merge(651c55292, 4973c0c5b)` and was green, because
that PR had recorded `GATE-CENSUS-1+360ef1b1dfbe` -- the version of ITS tip.
When it landed, `main`'s tree had both moves folded into one digest with no
record at it, and `--verify --strict` on `f18462966` exits 3, "no crate
reproduction is recorded at the current checker version".

THE GUARD
---------
On a `pull_request` event only, in the `census-artifact` job, and only when
the pull request's own diff moves at least one `CHECKER_SOURCES` member:
compare the checker version the base branch has NOW against the one it had at
the run's own base sha (`github.event.pull_request.base.sha`). A difference
means the base landing moved an input of the digest after this run's base, so
the record this run verified is not the record the landed tree needs. Fail,
name the files, and state the remedy: merge the base forward, re-record LAST.

The base ref is dynamic (`github.event.pull_request.base.ref`), because a
stacked pull request's base is not always `main`, and the guard cannot run on
a non-pull_request event at all: `base.sha` does not exist there, and on
`push`/`merge_group` the checked-out tree IS the tree that lands.

WHY THE DIGEST, AND NOT THE FILE THE DIFF MOVED
-----------------------------------------------
The narrow shape -- "the base moved a file this diff also moved" -- is what
`#2159`/`#2162` looks like, and it fires there. It is not the whole class,
because the digest folds five files into ONE version: the failure also happens
when the base moves a DIFFERENT member from the one this diff moves. The
pull request's own file is untouched on the base, so a per-file comparison
over the moved set says nothing, and the landed tree still needs a record
nobody wrote. Driven by hand on the real history (see the module's tests):

    PR head e5095e57b (origin/agent/1268-census-artifact) moves
    tools/gate_reference_census.py; run base 3f7dc04d8; base tip 75f7f8622
    moved selfhost/lower.rvl
      per-file over the moved set: sha256(3f7dc04d8, gate_reference_census.py)
        == sha256(75f7f8622, gate_reference_census.py)  (239c853c2eb017d4)
                                                             -> SILENT
      digest: GATE-CENSUS-1+dc8a83e87d59 (base)
           != GATE-CENSUS-1+469840fe4507 (tip)               -> FIRES

The two OBSERVED occurrences are the narrow shape -- `#2159` and `#2162` both
moved `selfhost/lower.rvl`, so a per-file comparison fires on them too. The
instance above is real history from the same repository and shows what the
per-file version misses, which is why the guard is not written that way: it is
strictly less sensitive than the digest on a class the digest covers exactly.

The predicate that is exactly right is "does the tree that will be produced
differ from the tree this run verified, in any `CHECKER_SOURCES` member?".
With `R` the run's base, `H` what the run checked out (`merge(R, head)`) and
`B` the base tip: the produced tree takes `B`'s copy of a member exactly when
`H`'s copy equals `R`'s, which for `H = merge(R, head)` is exactly the members
this diff did NOT move. So

    produced != H  <=>  some member has content(B) != content(R)
                   <=>  checker_version(B) != checker_version(R)

The "this diff moves a `CHECKER_SOURCES` member" condition is what keeps it
narrow, and it is not a convenience: when the diff moves none, the base
landing alone created the obligation, `main` is already red for it, and this
pull request is not the one that has to repair it. A base that moved for
unrelated reasons -- any path outside `CHECKER_SOURCES` -- leaves the digest
alone and stays silent, so a lane that is merely behind gets no false red.

WHAT THIS IS NOT
----------------
This is the in-repo half of issue #2165, and it deliberately depends on no
settings change. The other half stays human-owned and is not applied here:
`strict: true` on `main`'s required status checks, and adding the
`census-artifact` context to `required_status_checks.contexts`. Nothing in
this file reads or writes repository settings.

The check is not weakened either. A missing record stays a failure
(`--verify --strict`), the record stays keyed on all five sources, and no
record is pruned: this guard only makes an already-wrong green arrive as a
red on the pull request that would land it.

PATH 1 ONLY
-----------
The artifact has more than one way to go red under a green run, and this
guard closes exactly one of them. What it closes is the VERSION KEY: the
declared `CHECKER_SOURCES` digest that names the crate reproduction record.

The other is measured rather than declared. A census run also reads files
that are not in `CHECKER_SOURCES` -- `backends/python/emit.py`,
`tests/test_selfhost_lower.py`, the `GATE_CRATE_GLOBS` rust sources -- and
pins them by sha256 through an audit hook (`tools/census_artifact.py`, "the
pins"). A change to one of those does not move `checker_version()`, so this
guard is blind to it by construction, and widening it to cover that path
would mean keying on per-record, per-run pins: a bigger, different guard, and
not this one.

Measured on this tree, so the limit is stated with what was observed rather
than assumed. Appending a comment to `backends/python/emit.py` (pinned, in
`decides_verdicts`) and running
`PYTHONPATH=src python3.13 tools/census_artifact.py --verify --strict`
exits 0 and prints `REPRODUCED`. So does the same edit to
`tests/test_selfhost_lower.py`, and `--check` exits 0 as well. The reason is
that the committed records store no digest: `hydrate()` re-derives every
pinned file's sha256 from the checkout under test, so against
`docs/census-artifact/` the pin-hash arm compares a value with itself. The
pin arm that does bite a records directory is the pin SET (`missing`,
`added`/UNPINNED INPUT), and a change to a measured file that MOVES A VERDICT
is caught by the row-by-row comparison as `refuted`, exit 1. The pin hash
itself bites a RENDERED copy, which stores the digests: `--json` a clean
tree, edit `backends/python/emit.py`, `--verify <report>.json` reports
`decides_verdicts: moved: backends/python/emit.py` and exits 3.

So: this guard closes the version-key path, and the measured-pin path stays
where it already is, at verify time. The failure message says so rather than
claiming the whole class.
"""
from __future__ import annotations

import argparse
import hashlib
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

# `census_artifact` owns `CHECKER_SOURCES` and the digest's exact shape; the
# guard imports both rather than restating them, so a sixth source or a change
# to the length-prefixing cannot leave this file measuring a different thing.
_TOOLS = Path(__file__).resolve().parent
if str(_TOOLS) not in sys.path:
    sys.path.insert(0, str(_TOOLS))
import census_artifact  # noqa: E402

CHECKER_SOURCES = census_artifact.CHECKER_SOURCES

# The two halves of the remedy, as literal text so a test can pin that the
# message actually carries both (issue #2165 asks for both halves by name).
REMEDY_MERGE = "merge the base branch forward"
REMEDY_RECORD = "re-record the census LAST"

# What a `CHECKER_SOURCES` member that is ABSENT at a revision hashes to. It
# is not a path a checkout can contain, and it is used only to keep a source
# that one side lacks from reading as "identical": the digest is over the
# five members' bytes, and "not there" is a different set of bytes from any
# content. In practice every member exists at every revision this runs on.
ABSENT = b"\x00<census-stale-base: absent>\n"


class Unusable(Exception):
    """The guard could not decide, and says so instead of guessing."""


def _git(*args: str) -> bytes:
    proc = subprocess.run(("git", *args), capture_output=True, check=False)
    if proc.returncode != 0:
        raise Unusable(
            f"git {' '.join(args)} failed: "
            f"{proc.stderr.decode(errors='replace').strip()}")
    return proc.stdout


def _resolves(rev: str) -> bool:
    proc = subprocess.run(("git", "rev-parse", "--verify", "--quiet",
                           f"{rev}^{{commit}}"), capture_output=True,
                          check=False)
    return proc.returncode == 0


def blob_at(rev: str, rel: str) -> bytes | None:
    """`rel`'s bytes at `rev`, or None when `rel` does not exist there."""
    proc = subprocess.run(("git", "show", f"{rev}:{rel}"), capture_output=True,
                          check=False)
    return proc.stdout if proc.returncode == 0 else None


def version_at(rev: str) -> str:
    """`checker_version()`'s value for the tree at `rev`.

    Same digest, same names, same order and same length-prefixing as
    `census_artifact._digest`, taken out of the git object store instead of
    the working tree, so it is the version `rev`'s tree HAS rather than the
    one the checkout has. Equal to `checker_version()[0]` at HEAD.
    """
    h = hashlib.sha256()
    for rel in CHECKER_SOURCES:
        blob = blob_at(rev, rel)
        if blob is None:
            blob = ABSENT
        h.update(f"{rel}:{len(blob)}\n".encode())
        h.update(blob)
    return f"{census_artifact.CENSUS_SCHEMA}+{h.hexdigest()[:12]}"


def changed_paths(base_sha: str, head: str) -> list[str]:
    """The pull request's OWN diff: `base_sha` to `head`, two-dot.

    `head` is the tree the run checked out, which on a `pull_request` event is
    `merge(base_sha, the PR head)`, so this is exactly the paths the PR
    changed -- not the base's own landings since `base_sha`.
    """
    out = _git("diff", "--name-only", base_sha, head)
    return [line for line in out.decode(errors="replace").splitlines() if line]


def moved_members(paths) -> list[str]:
    """The `CHECKER_SOURCES` members among the changed `paths`."""
    return [rel for rel in CHECKER_SOURCES if rel in set(paths)]


def members_moved_on_base(base_sha: str, base_tip: str) -> list[str]:
    """The `CHECKER_SOURCES` members whose bytes differ between the run's base
    and the base tip -- i.e. the ones the base landing moved."""
    return [rel for rel in CHECKER_SOURCES
            if blob_at(base_sha, rel) != blob_at(base_tip, rel)]


@dataclass(frozen=True)
class Verdict:
    """The decision, with every input it was made from named."""

    fires: bool
    silent_reason: str
    base_sha: str
    base_tip: str
    base_ref: str
    moved: tuple[str, ...]
    moved_on_base: tuple[str, ...]
    version_base: str
    version_tip: str

    def text(self) -> str:
        if not self.fires:
            return (f"census-artifact stale-base guard: OK -- {self.silent_reason}")
        return "\n".join([
            "census-artifact stale-base guard: the base branch moved a census",
            "input after this run's base, so the tree this run verified is not",
            "the tree that will be produced.",
            "",
            f"  run's base sha ({self.base_ref}): {self.base_sha}",
            f"  the base tip now:                 {self.base_tip}",
            f"  this diff moves:                  "
            f"{', '.join(self.moved) or '(none)'}",
            f"  the base moved:                   "
            f"{', '.join(self.moved_on_base) or '(none)'}",
            f"  checker version at the run's base: {self.version_base}",
            f"  checker version at the base tip:   {self.version_tip}",
            "",
            f"The census record this run verified is keyed on "
            f"{self.version_base}; the tree that will land needs "
            f"{self.version_tip}, and no record exists at it.",
            f"To fix it: {REMEDY_MERGE}, then {REMEDY_RECORD} "
            "(python3 tools/census_artifact.py --write), so the record is",
            "written for the version the merged tree actually has.",
            "",
            "Scope: this closes the version-key path only. A change to a file",
            "the census run reads but CHECKER_SOURCES does not declare (a",
            "measured pin, e.g. backends/python/emit.py) does not move the",
            "version, is not seen here, and is caught at verify time instead.",
        ])


def judge(base_sha: str, base_tip: str, head: str = "HEAD",
          base_ref: str = "") -> Verdict:
    """The decision, from three revisions and nothing else.

    Silent when the pull request's own diff moves no `CHECKER_SOURCES`
    member: that base landing created the obligation on `main` by itself, and
    this pull request is not the one that repairs it. Otherwise it fires iff
    the checker version at `base_tip` differs from the one at `base_sha`.
    """
    for rev in (base_sha, base_tip, head):
        if not _resolves(rev):
            raise Unusable(
                f"cannot resolve {rev!r}: the guard cannot tell which tree "
                "this run verified, so it does not report a verdict")
    moved = tuple(moved_members(changed_paths(base_sha, head)))
    if not moved:
        return Verdict(False,
                       f"this diff moves no CHECKER_SOURCES member "
                       f"({len(CHECKER_SOURCES)} of them), so the base "
                       f"landing's obligation is not this pull request's",
                       base_sha, base_tip, base_ref, (), (), "", "")
    version_base = version_at(base_sha)
    version_tip = version_at(base_tip)
    if version_base == version_tip:
        return Verdict(False,
                       f"the base tip has the same checker version as this "
                       f"run's base ({version_base})",
                       base_sha, base_tip, base_ref, moved, (),
                       version_base, version_tip)
    return Verdict(True, "", base_sha, base_tip, base_ref, moved,
                   tuple(members_moved_on_base(base_sha, base_tip)),
                   version_base, version_tip)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        description="Issue #2165: fail a pull request whose base branch moved "
                    "a CHECKER_SOURCES member after the run's own base sha, "
                    "because the census record the run verified is then not "
                    "the one the landed tree needs. Exit 0 silent, 1 fires, "
                    "2 could not decide.")
    ap.add_argument("--base-sha", required=True,
                    help="github.event.pull_request.base.sha: the base this "
                         "run's checkout was built from")
    ap.add_argument("--base-tip", required=True,
                    help="the base branch's tip NOW, e.g. origin/main or "
                         "origin/${github.event.pull_request.base.ref}")
    ap.add_argument("--head", default="HEAD",
                    help="the tree this run verified (default: HEAD, which on "
                         "a pull_request event is refs/pull/N/merge)")
    ap.add_argument("--base-ref",
                    help="the base ref's name, for the message only")
    args = ap.parse_args(argv)
    base_ref = args.base_ref or args.base_tip
    try:
        verdict = judge(args.base_sha, args.base_tip, args.head, base_ref)
    except Unusable as exc:
        print(f"census-artifact stale-base guard: UNUSABLE -- {exc}")
        return 2
    print(verdict.text())
    return 1 if verdict.fires else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
