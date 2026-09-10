"""The commit-citation lane, seen to report what it did and did not examine.

Rule 3 of tools/check_roadmap_markers.py (a cited sha must be a commit in THIS
repo, reachable from the base ref) is the one rule that needs HISTORY to
answer, and CI's `lint` job checks out at depth 1, where there is none. The lane
then fell through the documented "a hex token that is not a commit here is
ignored" case for EVERY citation and printed

    roadmap markers OK: 0 in-progress marker(s) with a named branch, all
    consistent with origin/main.

having resolved nothing at all: a green admission gate that had examined zero
admissions. That is issue #876, and measured on a real depth-1 checkout of this
repo the old line was a bare OK with 0 of 86 unique citations resolved.

The two admissions the old code conflated are "we looked, and this token is not
this repository's history" (a pass: the roadmap pins foreign repos as
`inso1337/cordis-py@... 1c5e6f1`, and `ed25519` is a valid hex string) and "we
could not look" (a shallow or unreachable checkout). These tests pin them
apart, in BOTH directions, and pin the counts the OK line has to carry.

Unlike tests/test_roadmap_gate_bites.py, whose fixtures are synthetic on
purpose, every fixture here is a REAL git repository built in a temp directory,
and two of the tests build a REAL depth-1 clone. They touch no network: the
clone's origin is a local path.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOLS = REPO_ROOT / "tools"
sys.path.insert(0, str(TOOLS))

import check_roadmap_markers as gate  # noqa: E402

TOOL = TOOLS / "check_roadmap_markers.py"

# The exact invocation `.github/workflows/ci.yml`'s `lint` job runs on a push
# to main (`github.head_ref` is empty there, so --head-branch is empty too).
CI_ARGS = [
    "tools/check_roadmap_markers.py",
    "--check-contradiction",
    "--check-delegation",
    "--check-duplicate-headers",
    "--check-orphan",
    "--require-issue",
    "--head-branch",
    "",
]

OPEN_BRANCH = "fix/citation-lane"


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(cwd), *args],
                          capture_output=True, text=True)
    assert proc.returncode == 0, f"git {' '.join(args)}: {proc.stderr}"
    return proc.stdout.strip()


def _copy_tool_into(repo: Path) -> None:
    """`main()` resolves ROOT from the script's own path, so the tool has to
    live inside the checkout it is judging, exactly as it does in CI."""
    (repo / "tools").mkdir(exist_ok=True)
    shutil.copyfile(TOOL, repo / "tools" / "check_roadmap_markers.py")


def _run(repo: Path, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, *CI_ARGS, *extra],
                          cwd=repo, capture_output=True, text=True)


class Fixture:
    def __init__(self, origin: Path, work: Path, seed: str, open_tip: str) -> None:
        self.origin = origin
        self.work = work
        self.seed = seed          # a commit on main
        self.open_tip = open_tip  # a commit on an OPEN branch, not on main


def _fixture(tmp_path: Path, *, marker: bool = True, dangling: bool = False,
             foreign: bool = False) -> Fixture:
    """A real origin/checkout pair whose main cites commits in its roadmap.

    `dangling` cites a real commit of this repository that is NOT an ancestor
    of main (the tip of an open branch), and `foreign` cites a token that is
    not a commit here at all. `marker` writes the citation inside an
    in-progress marker, which is what makes `scanned` non-zero.
    """
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-q", "--initial-branch=main",
                    str(origin)], check=True)
    work = tmp_path / "work"
    subprocess.run(["git", "init", "-q", "--initial-branch=main", str(work)],
                   check=True)
    for key, value in (("user.email", "t@example.com"), ("user.name", "t"),
                       ("commit.gpgsign", "false")):
        _git(work, "config", key, value)
    (work / "docs").mkdir()
    (work / "docs" / "v2.0-roadmap.md").write_text(
        "# roadmap\n\n## Open, in rough priority order\n\n"
        "876. \u2705 **seed** landed.\n",
        encoding="utf-8")
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", "seed")
    seed = _git(work, "rev-parse", "HEAD")

    _git(work, "checkout", "-q", "-b", OPEN_BRANCH)
    (work / "side.txt").write_text("open work\n", encoding="utf-8")
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", "open work")
    open_tip = _git(work, "rev-parse", "HEAD")

    _git(work, "checkout", "-q", "main")
    cites = [f"`{seed}`"]
    if dangling:
        cites.append(f"`{open_tip}`")
    if foreign:
        cites.append("`deadbee`")
    body = f"cites {' and '.join(cites)}"
    if marker:
        item = (f"876. \u25d1 **citation lane demo**, in flight "
                f"(`{OPEN_BRANCH}`, issue #876), {body}.")
    else:
        item = f"876. \u25d1 **citation lane demo** (issue #876), {body}."
    (work / "docs" / "v2.0-roadmap.md").write_text(
        "# roadmap\n\n## Open, in rough priority order\n\n" + item + "\n",
        encoding="utf-8")
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", "roadmap")

    _git(work, "remote", "add", "origin", f"file://{origin}")
    _git(work, "push", "-q", "origin", "main", OPEN_BRANCH)
    # A clone leaves remote-tracking refs; a push does not. These are the refs
    # every one of this gate's ancestry answers is read from.
    _git(work, "update-ref", "refs/remotes/origin/main", "refs/heads/main")
    _git(work, "update-ref", f"refs/remotes/origin/{OPEN_BRANCH}",
         f"refs/heads/{OPEN_BRANCH}")
    return Fixture(origin, work, seed, open_tip)


def _shallow_clone(tmp_path: Path, origin: Path) -> Path:
    """A real depth-1 clone: one commit, and none of the cited ones."""
    shallow = tmp_path / "shallow"
    subprocess.run(["git", "clone", "-q", "--depth", "1",
                    f"file://{origin}", str(shallow)], check=True)
    return shallow


def _audit(repo: Path, *, allow_fetch: bool = True) -> dict:
    return gate.citation_audit(
        (repo / "docs" / "v2.0-roadmap.md").read_text(encoding="utf-8"),
        gate.Git(repo, "origin/main", allow_fetch=allow_fetch),
    )


def test_complete_history_passes_a_reachable_citation_and_counts_the_foreign_pin(
        tmp_path):
    fx = _fixture(tmp_path, foreign=True)
    audit = _audit(fx.work)
    assert audit["full_history"] is True
    assert audit["total"] == 2
    assert audit["reachable"] == 1
    assert audit["findings"] == []
    assert audit["unexamined"] == []
    # Not a commit in this repository: the documented ignore, counted rather
    # than silent. A pass, not a finding and not a skip-note.
    assert audit["foreign"] == ["deadbee"]


def test_complete_history_reports_a_real_commit_that_is_not_on_the_base(tmp_path):
    fx = _fixture(tmp_path, dangling=True)
    audit = _audit(fx.work)
    assert audit["full_history"] is True
    assert audit["reachable"] == 1
    assert audit["foreign"] == []
    assert audit["unexamined"] == []
    assert len(audit["findings"]) == 1
    (finding,) = audit["findings"]
    assert f"cites commit `{fx.open_tip}`" in finding
    assert "NOT reachable from origin/main" in finding
    assert f"reachable from: origin/{OPEN_BRANCH}" in finding


def test_a_depth_one_checkout_is_deepened_so_the_citation_is_judged(tmp_path):
    """The RED-before shape, in the direction that has to end green.

    Nothing on the sha path used to call `Git.fetch`, so in this same clone
    every citation was unresolved, every one was ignored, and the gate said OK.
    """
    fx = _fixture(tmp_path, dangling=True)
    shallow = _shallow_clone(tmp_path, fx.origin)
    assert _git(shallow, "rev-parse", "--is-shallow-repository") == "true"
    judged = subprocess.run(["git", "-C", str(shallow), "cat-file", "-t", fx.seed],
                            capture_output=True, text=True)
    assert judged.returncode != 0, "the depth-1 clone must NOT hold the cited commit"
    shallow_unjudged = subprocess.run(
        ["git", "-C", str(shallow), "cat-file", "-t", fx.open_tip],
        capture_output=True, text=True)
    assert shallow_unjudged.returncode != 0

    audit = _audit(shallow)
    assert audit["full_history"] is True, "the citation lane must deepen once"
    assert audit["reachable"] == 1
    assert audit["unexamined"] == []
    assert len(audit["findings"]) == 1
    assert f"cites commit `{fx.open_tip}`" in audit["findings"][0]


def test_a_shallow_checkout_that_cannot_deepen_says_so_and_never_prints_ok(
        tmp_path):
    """The false green itself: no network, so no history, so no OK.

    `git fetch --unshallow` cannot reach a remote that is not there, and the
    gate must not turn that into consent. It exits 0 (this is an admission
    about the checkout, not a contradiction in the roadmap, and CI's lint job
    is a required check) but it does not print the word OK and it names the
    count it could not examine.
    """
    fx = _fixture(tmp_path, marker=False, dangling=True)
    shallow = _shallow_clone(tmp_path, fx.origin)
    _git(shallow, "remote", "set-url", "origin", f"file://{tmp_path}/gone.git")
    _copy_tool_into(shallow)

    proc = _run(shallow)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    out = proc.stdout
    assert "roadmap markers OK" not in out
    assert "NOT FULLY CHECKED" in out
    assert "2 of the 2 commit citation(s)" in out
    assert "could NOT be examined" in out
    assert "DECLINING TO ANSWER" in out
    assert "did NOT " in out, "the run has to say what it did not check"


def test_no_fetch_in_a_shallow_checkout_declines_instead_of_passing(tmp_path):
    fx = _fixture(tmp_path, marker=False)
    shallow = _shallow_clone(tmp_path, fx.origin)
    _copy_tool_into(shallow)

    proc = _run(shallow, "--no-fetch")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "roadmap markers OK" not in proc.stdout
    assert "NOT FULLY CHECKED" in proc.stdout
    assert "1 of the 1 commit citation(s)" in proc.stdout
    assert "git fetch --unshallow" in proc.stdout
    assert _git(shallow, "rev-parse", "--is-shallow-repository") == "true", \
        "--no-fetch must not deepen anything"


def test_the_ok_line_carries_the_counts_and_the_ci_flags_still_pass(tmp_path):
    fx = _fixture(tmp_path, foreign=True)
    _copy_tool_into(fx.work)
    proc = _run(fx.work)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    out = proc.stdout
    assert "roadmap markers OK: 1 in-progress marker(s) with a named branch" in out
    assert "Checked 1 of the 2 commit citation(s)" in out
    assert "1 reachable from origin/main, 0 not." in out
    assert "1 backticked hex token(s) are not commits in this repository" in out


def test_the_ok_line_is_impossible_when_nothing_could_be_judged(tmp_path):
    """A roadmap whose every citation is unexamined cannot read as OK.

    This is the invariant: "checked 0" is allowed to mean "there was nothing
    to check", never "we checked nothing"."""
    fx = _fixture(tmp_path, marker=False, dangling=True, foreign=True)
    shallow = _shallow_clone(tmp_path, fx.origin)
    _git(shallow, "remote", "set-url", "origin", f"file://{tmp_path}/gone.git")
    _copy_tool_into(shallow)
    proc = _run(shallow)
    assert proc.returncode == 0
    assert "3 of the 3 commit citation(s)" in proc.stdout
    assert "OK" not in proc.stdout.replace("NOT FULLY CHECKED", "")


@pytest.mark.parametrize("allow_fetch", [True, False])
def test_a_complete_checkout_reports_the_same_counts_offline(tmp_path, allow_fetch):
    """On a complete history the answer does not depend on the network."""
    fx = _fixture(tmp_path, dangling=True, foreign=True)
    audit = _audit(fx.work, allow_fetch=allow_fetch)
    assert audit["full_history"] is True
    assert audit["total"] == 3
    assert audit["reachable"] == 1
    assert audit["foreign"] == ["deadbee"]
    assert audit["unexamined"] == []
    assert len(audit["findings"]) == 1
