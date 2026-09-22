"""tools/check_merged_prs_landed.py: a merged PR whose work is not in main.

Issue #1342. Four pull requests read MERGED with their work absent from `main`,
because each was merged into a base branch that had already been squash-merged.
Every check on every one of them was green.

These tests drive the checker against a SYNTHETIC repository built here, so the
assertions are about the mechanism rather than about whatever the real record
happens to say today. The synthetic tree reproduces the exact topology of the
defect: a squash-merged base branch that is still alive, with a second merge
landing on it after the squash.

The negative control matters as much as the positive one. `--is-ancestor` on
the PR HEAD returns "not in main" for a healthy squash merge too, so a test
that only shows the checker firing would not distinguish this check from the
one the issue says cannot work.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "revl_check_merged_prs", ROOT / "tools" / "check_merged_prs_landed.py")
chk = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(chk)

BASELINE = ROOT / "tools" / "merged_pr_landing_baseline.json"


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, check=True)
    return proc.stdout.strip()


def _commit(repo: Path, name: str, body: str, msg: str | None = None) -> str:
    (repo / name).write_text(body, encoding="utf-8")
    _git(repo, "add", name)
    _git(repo, "commit", "-m", msg or f"add {name}")
    return _git(repo, "rev-parse", "HEAD")


def _build_repo(tmp_path: Path) -> dict[str, str]:
    """A tree with one healthy squash merge and one stranded merge.

        main      A --- S(squash of feature)
        feature   A --- f1 --- f2 --- M(merge of stacked)
                                  ^ still alive, never merged again

    `S` is what a squash merge of `feature` produces on main: the content, a
    new commit, and no ancestry link to f1/f2. `M` is the stranding: a second
    PR merged into `feature` AFTER the squash, so its merge commit lives only
    on a branch nothing will merge again.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "t")
    _commit(repo, "base.txt", "base\n")

    _git(repo, "checkout", "-q", "-b", "feature")
    head_feature = _commit(repo, "feature.txt", "feature\n")

    # The squash merge of `feature` onto main: the same content, a new commit,
    # no ancestry link. The message differs from the branch commit's on
    # purpose: a squash commit is a different object, and two commits with the
    # same tree, parent, message and timestamp would be the SAME sha, which
    # would make this fixture claim an ancestry that squashing never gives.
    _git(repo, "checkout", "-q", "main")
    squash = _commit(repo, "feature.txt", "feature\n",
                     msg="feature (#1)")

    # a stacked branch merged into `feature` AFTER the squash -> stranded
    _git(repo, "checkout", "-q", "-b", "stacked", "feature")
    _commit(repo, "stacked.txt", "stacked\n")
    _git(repo, "checkout", "-q", "feature")
    _git(repo, "merge", "-q", "--no-ff", "-m", "merge stacked", "stacked")
    stranded_merge = _git(repo, "rev-parse", "HEAD")

    _git(repo, "checkout", "-q", "main")
    return {
        "repo": str(repo),
        "squash": squash,
        "head_feature": head_feature,
        "stranded_merge": stranded_merge,
    }


def _prs(t: dict[str, str]) -> list[dict]:
    return [
        {"number": 1, "title": "healthy squash merge", "baseRefName": "main",
         "mergeCommit": {"oid": t["squash"]}},
        {"number": 2, "title": "merged into a squashed base",
         "baseRefName": "feature",
         "mergeCommit": {"oid": t["stranded_merge"]}},
    ]


def test_the_check_fires_on_a_pr_merged_into_a_squashed_base(tmp_path):
    t = _build_repo(tmp_path)
    r = chk.audit(_prs(t), Path(t["repo"]), "main", {})
    assert r.known == [] and r.unresolved == [] and r.unrecorded == []
    assert len(r.findings) == 1, r.findings
    assert r.findings[0].startswith("#2:"), r.findings
    assert "not_a_real_pr" not in r.findings[0]


def test_the_healthy_squash_merge_is_not_a_finding(tmp_path):
    """The negative control. A squash merge onto main leaves the PR head
    unreachable, so a head-based check would report this one too."""
    t = _build_repo(tmp_path)
    assert chk.audit(_prs(t)[:1], Path(t["repo"]), "main", {}).findings == []


def test_the_pr_head_cannot_tell_the_two_apart(tmp_path):
    """Why `git merge-base --is-ancestor <head> main` is not the check. It is
    false for the healthy squash merge and false for the stranding, so it
    answers the same for a PR that landed and one that did not."""
    t = _build_repo(tmp_path)
    repo = Path(t["repo"])
    assert not chk.is_ancestor(repo, t["head_feature"], "main")
    assert not chk.is_ancestor(repo, t["stranded_merge"], "main")
    # and the merge commit does tell them apart
    assert chk.is_ancestor(repo, t["squash"], "main")


def test_a_baselined_pr_is_known_and_not_a_finding(tmp_path):
    t = _build_repo(tmp_path)
    r = chk.audit(_prs(t), Path(t["repo"]), "main", {"2": "carried by PR #99"})
    assert r.findings == []
    assert len(r.known) == 1 and "carried by PR #99" in r.known[0]


def test_the_ratchet_shrinks_only(tmp_path):
    """A baselined PR whose merge commit HAS reached main is a failure: the
    entry is stale, and a baseline allowed to keep entries it no longer needs
    stops being evidence of anything."""
    t = _build_repo(tmp_path)
    r = chk.audit(_prs(t), Path(t["repo"]), "main", {"1": "was never stranded"})
    assert r.known == []
    assert len(r.findings) == 2, r.findings
    stale = [f for f in r.findings if f.startswith("#1:")]
    assert len(stale) == 1 and "IS now reachable" in stale[0]


def test_the_two_unanswerable_cases_are_kept_apart(tmp_path):
    """Two ways the question cannot be asked, and they are NOT the same thing
    (issue #1377). GitHub recording no merge commit is a fact about the pull
    request; a commit this clone cannot resolve is a fact about the clone. The
    first needs somebody to look at the PR, the second needs a fetch. Neither
    may report green, and neither may be reported as the other."""
    t = _build_repo(tmp_path)
    repo = Path(t["repo"])
    no_merge = [{"number": 3, "title": "no merge commit",
                 "baseRefName": "main", "mergeCommit": None}]
    r = chk.audit(no_merge, repo, "main", {})
    assert r.unresolved == []
    assert len(r.unrecorded) == 1 and "no merge commit" in r.unrecorded[0]

    absent = [{"number": 4, "title": "not in this clone", "baseRefName": "main",
               "mergeCommit": {"oid": "0" * 40}}]
    r = chk.audit(absent, repo, "main", {})
    assert r.findings == [] and r.unrecorded == []
    assert len(r.unresolved) == 1
    assert "could not be resolved" in r.unresolved[0]
    # and it must not be worded as a claim about main
    assert "NOTHING is claimed" in r.unresolved[0]


def test_exit_status_separates_finding_from_unanswerable(tmp_path):
    t = _build_repo(tmp_path)
    src = tmp_path / "prs.json"
    src.write_text(json.dumps(_prs(t)), encoding="utf-8")
    empty = tmp_path / "empty.json"
    empty.write_text(json.dumps({"unreachable": {}}), encoding="utf-8")
    rc = chk.main(["--from-json", str(src), "--root", t["repo"],
                   "--main-ref", "main", "--baseline", str(empty)])
    assert rc == 1

    unknown = tmp_path / "unknown.json"
    unknown.write_text(json.dumps(
        [{"number": 5, "title": "x", "baseRefName": "main",
          "mergeCommit": None}]), encoding="utf-8")
    rc = chk.main(["--from-json", str(unknown), "--root", t["repo"],
                   "--main-ref", "main", "--baseline", str(empty)])
    assert rc == 2


def test_the_baseline_is_well_formed_and_documented():
    """Every entry is a pull request number mapped to a reason. A bare number
    with no note is a line somebody added to make a check green, which is the
    one thing this ratchet exists to prevent."""
    data = json.loads(BASELINE.read_text(encoding="utf-8"))
    assert isinstance(data.get("comment"), list) and data["comment"]
    entries = data["unreachable"]
    assert entries, "an empty baseline should be deleted, not kept"
    for num, note in entries.items():
        assert num.isdigit(), num
        assert len(note) > 40, f"#{num}: the note says too little: {note!r}"
        assert "agent/" in note or "#" in note, (
            f"#{num}: the note names neither the base branch it went to nor "
            f"the PR that carried it: {note!r}")


def test_an_empty_pull_request_list_is_not_a_pass(tmp_path):
    """The vacuous green. `gh pr list` exiting 0 with no rows -- a token
    missing `pull-requests: read`, a changed API shape -- would otherwise
    report that every merged PR is accounted for, having read none."""
    t = _build_repo(tmp_path)
    src = tmp_path / "none.json"
    src.write_text("[]", encoding="utf-8")
    empty = tmp_path / "empty.json"
    empty.write_text(json.dumps({"unreachable": {}}), encoding="utf-8")
    rc = chk.main(["--from-json", str(src), "--root", t["repo"],
                   "--main-ref", "main", "--baseline", str(empty)])
    assert rc == 2


# --------------------------------------------------------------------------
# Issue #1377: the merge that lands during the runner's clone.
#
# The PR list comes from GitHub and the ancestry comes from a local clone, so
# they are snapshots taken at different moments. Pre-fetching in the workflow
# does not close the window, because the fetch runs BEFORE the PR list is
# read. These build that race deterministically -- clone, then merge -- rather
# than waiting for it to happen again on a runner.


def _build_remote_and_clone(tmp_path: Path) -> dict[str, str]:
    """A remote with the usual topology, plus a clone taken at a known sha.

    `clone` is what the runner has: every branch, fetched, and then the world
    moves on. `stranded_deleted` is a second clone taken after the base branch
    was deleted upstream, which is the state a real finding is usually in by
    the time anybody looks.
    """
    remote = tmp_path / "remote"
    remote.mkdir()
    _git(remote, "init", "-q", "-b", "main")
    _git(remote, "config", "user.email", "t@example.invalid")
    _git(remote, "config", "user.name", "t")
    _commit(remote, "base.txt", "base\n")

    _git(remote, "checkout", "-q", "-b", "feature")
    _commit(remote, "feature.txt", "feature\n")
    _git(remote, "checkout", "-q", "main")
    squash = _commit(remote, "feature.txt", "feature\n", msg="feature (#1)")
    _git(remote, "checkout", "-q", "-b", "stacked", "feature")
    _commit(remote, "stacked.txt", "stacked\n")
    _git(remote, "checkout", "-q", "feature")
    _git(remote, "merge", "-q", "--no-ff", "-m", "merge stacked", "stacked")
    stranded = _git(remote, "rev-parse", "HEAD")
    _git(remote, "checkout", "-q", "main")

    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", "--no-local", str(remote),
                    str(clone)], check=True, capture_output=True)

    # the base branch is deleted upstream, as it is after a normal merge
    _git(remote, "branch", "-q", "-D", "stacked")
    _git(remote, "update-ref", "-d", "refs/heads/feature")
    gone = tmp_path / "clone-deleted"
    subprocess.run(["git", "clone", "-q", "--no-local", str(remote),
                    str(gone)], check=True, capture_output=True)

    # AND NOW a pull request merges, after both clones were taken
    late = _commit(remote, "late.txt", "late\n", msg="merged during the clone")

    return {"remote": str(remote), "clone": str(clone),
            "clone_deleted": str(gone), "squash": squash,
            "stranded": stranded, "late": late}


def _pr(num: int, oid: str, base: str = "main") -> list[dict]:
    return [{"number": num, "title": "t", "baseRefName": base,
             "mergeCommit": {"oid": oid}}]


def test_a_merge_during_the_clone_is_not_reported_without_a_refresh(tmp_path):
    """The defect, stated as the tool saw it. With no refresh the commit does
    not resolve, and the run ends with something that is not a pass."""
    t = _build_remote_and_clone(tmp_path)
    r = chk.audit(_pr(1372, t["late"]), Path(t["clone"]), "origin/main", {})
    assert r.findings == []
    assert len(r.unresolved) == 1, r


def test_a_merge_during_the_clone_resolves_after_the_refresh(tmp_path):
    """The fix. Refreshing on the miss makes the commit resolve AND makes
    `origin/main` current, and the pull request is then plainly fine. This is
    the false alarm from the issue, gone."""
    t = _build_remote_and_clone(tmp_path)
    remote = chk.Remote(Path(t["clone"]), "origin/main")
    r = chk.audit(_pr(1372, t["late"]), Path(t["clone"]), "origin/main", {},
                  remote=remote)
    assert r == chk.Audit([], [], [], []), r


def test_the_refresh_does_not_disarm_a_real_finding(tmp_path):
    """The repair must not buy quiet by admitting everything. The stranded
    merge commit is in this clone already, and stays a finding."""
    t = _build_remote_and_clone(tmp_path)
    remote = chk.Remote(Path(t["clone"]), "origin/main")
    r = chk.audit(_pr(2, t["stranded"], base="feature"), Path(t["clone"]),
                  "origin/main", {}, remote=remote)
    assert len(r.findings) == 1 and r.findings[0].startswith("#2:")
    assert r.unresolved == [] and r.unrecorded == []


def test_the_refresh_recovers_a_finding_whose_base_branch_was_deleted(tmp_path):
    """The case that made the two answers look alike. Once the base branch is
    gone from the remote, a fresh clone has no more of the stranded merge
    commit than it has of one from the future, so before the refresh both read
    as `not in this clone`. Asking the remote for the commit by sha resolves
    it, and the finding is reported as the finding it is."""
    t = _build_remote_and_clone(tmp_path)
    gone = Path(t["clone_deleted"])
    assert not chk.object_exists(gone, t["stranded"])

    blind = chk.audit(_pr(2, t["stranded"], base="feature"), gone,
                      "origin/main", {})
    assert blind.findings == [] and len(blind.unresolved) == 1

    seeing = chk.audit(_pr(2, t["stranded"], base="feature"), gone,
                       "origin/main", {},
                       remote=chk.Remote(gone, "origin/main"))
    assert len(seeing.findings) == 1, seeing
    assert seeing.unresolved == []


def test_the_refresh_is_only_attempted_on_a_miss(tmp_path):
    """A run where every merge commit resolves makes no network call at all.
    A gate that fetches once per pull request would be its own problem."""
    t = _build_remote_and_clone(tmp_path)
    clone = Path(t["clone"])
    calls: list[tuple] = []

    class Counting(chk.Remote):
        def _git(self, *args):
            calls.append(args)
            return super()._git(*args)

    r = chk.audit(_pr(1, t["squash"]), clone, "origin/main", {},
                  remote=Counting(clone, "origin/main"))
    assert r == chk.Audit([], [], [], []), r
    assert calls == [], calls


def test_the_refresh_happens_once_however_many_misses(tmp_path):
    t = _build_remote_and_clone(tmp_path)
    clone = Path(t["clone"])
    fetches: list[tuple] = []

    class Counting(chk.Remote):
        def _git(self, *args):
            fetches.append(args)
            return super()._git(*args)

    prs = _pr(1372, t["late"]) + _pr(1373, t["late"]) + _pr(1374, t["late"])
    r = chk.audit(prs, clone, "origin/main", {}, remote=Counting(clone,
                                                                "origin/main"))
    assert r == chk.Audit([], [], [], []), r
    refreshes = [c for c in fetches if any(a.startswith("+refs/heads/")
                                           for a in c)]
    assert len(refreshes) == 1, fetches


def test_no_fetch_never_touches_the_remote(tmp_path):
    """`--no-fetch` has to mean it, or the offline mode is a lie."""
    t = _build_remote_and_clone(tmp_path)
    clone = Path(t["clone"])
    remote = chk.Remote(clone, "origin/main", enabled=False)
    assert remote.name is None
    assert remote.refresh() is False
    assert remote.fetch_commit(t["late"]) is False
    assert not chk.object_exists(clone, t["late"])


def test_a_local_main_ref_has_no_remote_to_refresh_from():
    """`--main-ref main` names a local branch. There is nothing behind it to
    fetch from, and inventing `origin` would fetch from somewhere nobody
    asked for."""
    assert chk.remote_of("origin/main") == "origin"
    assert chk.remote_of("upstream/main") == "upstream"
    assert chk.remote_of("main") is None
    assert chk.remote_of("origin/") is None


def test_the_three_outcomes_print_three_different_words(tmp_path, capsys):
    """Issue #1377's third ask, asserted on the output rather than trusted.
    A real finding and a stale clone used to share the word UNKNOWN, which is
    what made the false alarm indistinguishable from the thing the gate is
    for."""
    t = _build_remote_and_clone(tmp_path)
    src = tmp_path / "prs.json"
    src.write_text(json.dumps(
        _pr(2, t["stranded"], base="feature")
        + [{"number": 3, "title": "t", "baseRefName": "main",
            "mergeCommit": None}]
        + _pr(4, "0" * 40)), encoding="utf-8")
    empty = tmp_path / "empty.json"
    empty.write_text(json.dumps({"unreachable": {}}), encoding="utf-8")

    rc = chk.main(["--from-json", str(src), "--root", t["clone"],
                   "--main-ref", "origin/main", "--baseline", str(empty),
                   "--no-fetch"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "UNREACHABLE #2:" in out
    assert "UNRECORDED  #3:" in out
    assert "UNRESOLVED  #4:" in out
    # the old word is gone from every one of them
    assert "UNKNOWN" not in out


def test_the_baseline_ratchet_did_not_absorb_the_false_alarm():
    """Issue #1377 point 4. The repair must not have quietly added an entry to
    make the run green: #1372 merged normally and its work is in main."""
    entries = json.loads(BASELINE.read_text(encoding="utf-8"))["unreachable"]
    assert "1372" not in entries
    assert len(entries) == 8, sorted(entries)
