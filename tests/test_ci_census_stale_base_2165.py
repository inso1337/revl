"""A base-branch landing can move a census input under a green pull request
run (issue #2165).

THE FAILURE, TWICE, AND WHY NOTHING REPORTED IT
-----------------------------------------------
`main`'s census record is keyed on `checker_version()`, a digest over exactly
the five files in `tools/census_artifact.py`'s `CHECKER_SOURCES`, and the
record for a version lives at
`tests/fixtures/census_crate_reproduction/<version>.json`. Every landing that
moves one of those five creates a fresh obligation on the tree it lands on.

Branch protection on `main` is non-strict and `census-artifact` is not a
required context, so a pull request may merge while its base moves underneath
it. GitHub recomputes *mergeability* when the base moves; it does not rebuild
`refs/pull/N/merge` for a run already in flight, and it does not re-run that
run's checks. So `census-artifact` can be green against a tree that will not
be the tree that lands. Measured on this repository:

    #2159 fb1a9916e moved selfhost/lower.rvl
    #2162 f18462966 then landed green, having verified
          merge(651c55292, 4973c0c5b) -- whose base predates #2159

After both landed, `main`'s tree had two moves folded into one digest with no
record at it, and `--verify --strict` exits 3 there. Neither author's run
could see it: each one verified a merge ref whose main parent was stale.

THE GUARD, AND WHAT THIS FILE PINS
----------------------------------
`tools/census_stale_base.py`, called from the `census-artifact` job on a
`pull_request` event only. It fires when BOTH hold:

  * the pull request's own diff (`base.sha`..HEAD, two-dot) moves at least one
    `CHECKER_SOURCES` member -- so the obligation is this pull request's to
    repair; and
  * the checker version at the base tip NOW differs from the one at this run's
    own base sha -- so the tree that will be produced is not the tree this run
    verified.

A base that moved for unrelated reasons leaves the digest alone and stays
silent, which is what keeps a lane that is merely behind from getting a false
red. This module pins four things:

  * all four rows, driven through real `git` in a scratch repository the test
    builds itself: behind + moves a member fires; behind + moves no member is
    silent; behind + the base moved only non-members is silent; up to date is
    silent;
  * that the digest is not a per-file comparison over the moved set. The row
    that fires moves member A while the base moved member B, so a per-file
    predicate is silent on it and the digest is not. `#2159`/`#2162` happened
    to move the SAME file, so a per-file predicate would have caught those two
    -- it is the general shape it misses, and the digest covers the class
    exactly;
  * that the message names both halves of the remedy, so an author is told to
    merge the base forward and re-record the census LAST rather than being
    handed a bare red;
  * the workflow structure: the guard step is in the `census-artifact` job,
    that job still runs on a pull request, the step is `pull_request`-gated,
    and it neither weakens nor replaces `--verify --strict`.

PATH 1 ONLY
-----------
This closes the version-key path. The artifact has a second red path that
this guard is blind to by construction: the pins are MEASURED, not declared
(`tools/census_artifact.py`, "the pins"), so a change to a file the census run
reads but `CHECKER_SOURCES` does not list -- `backends/python/emit.py`,
`tests/test_selfhost_lower.py` -- does not move the version. That path stays
where it is, at verify time, and the guard's message says so rather than
claiming the whole class. `test_the_guard_says_which_path_it_closes` pins that
limit so a later reader does not read the message as a broader claim.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
CI_YML = ROOT / ".github/workflows/ci.yml"

sys.path.insert(0, str(ROOT / "tools"))
import census_artifact  # noqa: E402
import census_stale_base as guard  # noqa: E402

# Two distinct members of the set the digest folds together. The failure is
# about the digest, so the rows below use different members on the two sides.
MEMBER_A = guard.CHECKER_SOURCES[0]
MEMBER_B = guard.CHECKER_SOURCES[1]
UNRELATED = "docs/notes.txt"


def _run(cwd: Path, *args: str) -> str:
    proc = subprocess.run(("git", *args), cwd=cwd, capture_output=True,
                          text=True, check=False)
    assert proc.returncode == 0, f"git {' '.join(args)}: {proc.stderr}"
    return proc.stdout


def _write(repo: Path, rel: str, text: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _commit(repo: Path, message: str, **files: str) -> str:
    for rel, text in files.items():
        _write(repo, rel, text)
    _run(repo, "add", "-A")
    _run(repo, "-c", "user.email=t@example.invalid",
         "-c", "user.name=test", "commit", "-q", "-m", message)
    return _run(repo, "rev-parse", "HEAD").strip()


class Scratch:
    """A repository whose history is exactly the four shapes under test.

        c0  every CHECKER_SOURCES member at "v1"
        c1  (main)      the base landing: moves MEMBER_B
        c2  (pr)        moves MEMBER_A
        c3  (quiet)     moves only UNRELATED
        c4  (unrelated) moves only UNRELATED, and does NOT carry c1
    """

    def __init__(self, repo: Path) -> None:
        self.repo = repo
        self.base = _commit(repo, "c0", **{rel: "v1\n"
                                           for rel in guard.CHECKER_SOURCES},
                            **{UNRELATED: "v1\n"})
        _run(repo, "checkout", "-q", "-b", "pr", self.base)
        self.pr_moves_member = _commit(repo, "c2", **{MEMBER_A: "v2\n"})
        _run(repo, "checkout", "-q", "-b", "quiet", self.base)
        self.pr_moves_nothing = _commit(repo, "c3", **{UNRELATED: "v2\n"})
        _run(repo, "checkout", "-q", "-b", "unrelated", self.base)
        self.tip_unrelated = _commit(repo, "c4", **{UNRELATED: "v2\n"})
        _run(repo, "checkout", "-q", "main")
        self.tip = _commit(repo, "c1", **{MEMBER_B: "v2\n"})


@pytest.fixture
def scratch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Scratch:
    repo = tmp_path / "census"
    repo.mkdir()
    _run(repo, "init", "-q", "-b", "main")
    made = Scratch(repo)
    monkeypatch.chdir(repo)
    return made


# --------------------------------------------------------- the four rows
#
# Each row runs the real predicate against real revisions. `judge` shells out
# to `git`, so the fixture's commits are the inputs, not a stub of them.

def test_behind_and_the_diff_moves_a_member_fires(scratch: Scratch) -> None:
    """Row (a): behind base, and this diff moves a CHECKER_SOURCES member."""
    verdict = guard.judge(scratch.base, scratch.tip, scratch.pr_moves_member,
                          "main")
    assert verdict.fires is True
    assert verdict.moved == (MEMBER_A,)
    assert verdict.version_base != verdict.version_tip
    # The two sides moved DIFFERENT members. That is the general shape, and it
    # is why the predicate is the digest and not a per-file comparison.
    assert verdict.moved_on_base == (MEMBER_B,)


def test_the_digest_sees_what_a_per_file_predicate_cannot(
        scratch: Scratch) -> None:
    """Row (a), the load-bearing half: two different members, one version.

    A per-file predicate over the moved set compares MEMBER_A's content at the
    base sha with its content at the base tip. The base never touched
    MEMBER_A, so the two are byte-identical and that predicate is silent --
    while the tree that will land still needs a record nobody wrote. Driven
    here rather than argued: the file bytes are read out of both revisions.
    """
    per_file = [rel for rel in (MEMBER_A,)
                if guard.blob_at(scratch.base, rel)
                != guard.blob_at(scratch.tip, rel)]
    assert per_file == [], "the base did not touch MEMBER_A, so per-file is silent"
    verdict = guard.judge(scratch.base, scratch.tip, scratch.pr_moves_member,
                          "main")
    assert verdict.fires is True
    assert verdict.moved_on_base == (MEMBER_B,)


def test_behind_and_the_diff_moves_no_member_is_silent(
        scratch: Scratch) -> None:
    """Row (b): behind base, but the diff moves nothing in CHECKER_SOURCES.

    The base landing alone created the obligation; `main` is already red for
    it and this pull request is not the one that has to repair it. Silence
    here is the narrowness the guard depends on.
    """
    verdict = guard.judge(scratch.base, scratch.tip, scratch.pr_moves_nothing,
                          "main")
    assert verdict.fires is False
    assert verdict.moved == ()
    assert "CHECKER_SOURCES" in verdict.silent_reason


def test_behind_but_the_base_moved_only_non_members_is_silent(
        scratch: Scratch) -> None:
    """A base that moved for unrelated reasons leaves the digest alone.

    `docs/notes.txt` is not in `CHECKER_SOURCES`, so `c4` moved the base tip
    without moving the version. This is the row that keeps a lane which is
    merely behind from getting a false red.
    """
    assert guard.version_at(scratch.tip_unrelated) == guard.version_at(
        scratch.base)
    verdict = guard.judge(scratch.base, scratch.tip_unrelated,
                          scratch.pr_moves_member, "main")
    assert verdict.fires is False
    assert verdict.moved == (MEMBER_A,)
    assert "same checker version" in verdict.silent_reason


def test_up_to_date_is_silent(scratch: Scratch) -> None:
    """Row (c): the base has not moved since this run's base sha."""
    verdict = guard.judge(scratch.tip, scratch.tip, scratch.pr_moves_member,
                          "main")
    assert verdict.fires is False
    assert MEMBER_A in verdict.moved
    assert "same checker version" in verdict.silent_reason


# --------------------------------------------------------- the message

def test_the_message_names_both_halves_of_the_remedy(scratch: Scratch) -> None:
    """A bare red is not a remedy. Both halves have to be in the text."""
    text = guard.judge(scratch.base, scratch.tip, scratch.pr_moves_member,
                       "main").text()
    assert guard.REMEDY_MERGE in text
    assert guard.REMEDY_RECORD in text
    assert "merge the base branch forward" in text
    assert "re-record the census LAST" in text
    assert "python3 tools/census_artifact.py --write" in text


def test_the_message_names_the_files_on_both_sides(scratch: Scratch) -> None:
    text = guard.judge(scratch.base, scratch.tip, scratch.pr_moves_member,
                       "main").text()
    assert MEMBER_A in text
    assert MEMBER_B in text
    assert guard.version_at(scratch.base) in text
    assert guard.version_at(scratch.tip) in text


def test_the_guard_says_which_path_it_closes(scratch: Scratch) -> None:
    """The scope limit, pinned so the message is not read as a wider claim.

    The artifact also goes red on a MEASURED pin -- a file the census run
    reads that `CHECKER_SOURCES` does not declare -- which does not move the
    version and which this guard does not see. The message has to say so.
    """
    text = guard.judge(scratch.base, scratch.tip, scratch.pr_moves_member,
                       "main").text()
    assert "version-key path only" in text
    assert "measured pin" in text
    assert "backends/python/emit.py" in text


# --------------------------------------------------------- the workflow

def _census_job() -> dict:
    workflow = yaml.safe_load(CI_YML.read_text(encoding="utf-8"))
    return workflow["jobs"]["census-artifact"]


def _guard_step() -> dict:
    steps = [s for s in _census_job()["steps"]
             if "census_stale_base.py" in (s.get("run") or "")]
    assert len(steps) == 1, (
        "the census-artifact job must call tools/census_stale_base.py exactly "
        f"once, found {len(steps)}")
    return steps[0]


def _flat(condition: str) -> str:
    return " ".join((condition or "").split())


def test_the_census_artifact_job_still_runs_on_a_pull_request() -> None:
    """A guard in a job that never starts is not a guard."""
    job = _census_job()
    assert "github.event_name == 'pull_request'" in _flat(job.get("if"))
    runs = [(s.get("run") or "") for s in job["steps"]]
    assert any("tools/census_artifact.py --verify --strict" in r
               for r in runs), "the census check itself must still be here"


def test_the_guard_step_is_pull_request_gated() -> None:
    """`base.sha` does not exist on a non-pull_request event.

    On `push` and `merge_group` the checked-out tree IS the tree that lands,
    so there is nothing to compare and the step must not run.
    """
    step = _guard_step()
    assert "github.event_name == 'pull_request'" in _flat(step.get("if"))


def test_the_guard_step_reads_the_base_from_the_event() -> None:
    """The base ref is dynamic: a stacked pull request's base is not `main`."""
    env = _guard_step().get("env") or {}
    assert "${{ github.event.pull_request.base.sha }}" in env.get("BASE_SHA", "")
    assert "${{ github.event.pull_request.base.ref }}" in env.get("BASE_REF", "")


def test_the_guard_step_keeps_the_event_out_of_the_shell() -> None:
    """`github.event.*` is attacker-controlled text; it reaches the shell
    through `env:` only, the rule `changes`, `held-out` and `decide` follow."""
    run = _guard_step()["run"]
    assert "${{" not in run
    assert "set -euo pipefail" in run


def test_the_guard_step_cannot_be_softened() -> None:
    """A guard that reports green when it fails is worse than no guard."""
    step = _guard_step()
    assert not step.get("continue-on-error")
    assert "|| true" not in step["run"]
    assert "|| status=0" not in step["run"]
    # It fetches the base tip itself, so it does not depend on `decide` having
    # fetched it on this event.
    assert "refs/heads/${BASE_REF}" in step["run"]


def test_every_member_the_guard_watches_sets_decide_to_run() -> None:
    """The step is gated on `decide`'s `run` output, so that gate has to be
    unable to suppress the guard in the case the guard exists for.

    `decide` publishes `run=false` when `--moved-inputs` says the diff moves no
    input. If a `CHECKER_SOURCES` member were not one of those inputs, a diff
    that moves it would skip this step and the guard would never run. Read out
    of `moved_inputs` itself, over the committed records, so the two lists
    cannot drift apart silently.
    """
    records = census_artifact.filter_view(census_artifact.load_records())
    for rel in guard.CHECKER_SOURCES:
        assert census_artifact.moved_inputs([rel], records) == [rel], (
            f"{rel} is in CHECKER_SOURCES but not a --moved-inputs input, so "
            f"`decide` would publish run=false for a diff that moves it")


def test_the_guard_does_not_replace_the_verify_step() -> None:
    """The new step adds a check; it does not trade one red for a weaker one."""
    runs = [(s.get("run") or "") for s in _census_job()["steps"]]
    assert any("--verify --strict" in r for r in runs)
    assert any("--moved-inputs" in r for r in runs)
