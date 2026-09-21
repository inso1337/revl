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
    findings, known, unanswerable = chk.audit(
        _prs(t), Path(t["repo"]), "main", {})
    assert known == [] and unanswerable == []
    assert len(findings) == 1, findings
    assert findings[0].startswith("#2:"), findings
    assert "not_a_real_pr" not in findings[0]


def test_the_healthy_squash_merge_is_not_a_finding(tmp_path):
    """The negative control. A squash merge onto main leaves the PR head
    unreachable, so a head-based check would report this one too."""
    t = _build_repo(tmp_path)
    findings, _, _ = chk.audit(_prs(t)[:1], Path(t["repo"]), "main", {})
    assert findings == []


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
    findings, known, _ = chk.audit(
        _prs(t), Path(t["repo"]), "main", {"2": "carried by PR #99"})
    assert findings == []
    assert len(known) == 1 and "carried by PR #99" in known[0]


def test_the_ratchet_shrinks_only(tmp_path):
    """A baselined PR whose merge commit HAS reached main is a failure: the
    entry is stale, and a baseline allowed to keep entries it no longer needs
    stops being evidence of anything."""
    t = _build_repo(tmp_path)
    findings, known, _ = chk.audit(
        _prs(t), Path(t["repo"]), "main", {"1": "was never stranded"})
    assert known == []
    assert len(findings) == 2, findings
    stale = [f for f in findings if f.startswith("#1:")]
    assert len(stale) == 1 and "IS now reachable" in stale[0]


def test_a_missing_merge_commit_is_unknown_and_never_a_pass(tmp_path):
    """Two ways the question cannot be asked: GitHub records no merge commit,
    or the commit is not in this clone. Neither may report green."""
    t = _build_repo(tmp_path)
    repo = Path(t["repo"])
    no_merge = [{"number": 3, "title": "no merge commit",
                 "baseRefName": "main", "mergeCommit": None}]
    _, _, unanswerable = chk.audit(no_merge, repo, "main", {})
    assert len(unanswerable) == 1 and "no merge commit" in unanswerable[0]

    absent = [{"number": 4, "title": "not in this clone", "baseRefName": "main",
               "mergeCommit": {"oid": "0" * 40}}]
    findings, _, unanswerable = chk.audit(absent, repo, "main", {})
    assert findings == []
    assert len(unanswerable) == 1 and "not in this clone" in unanswerable[0]


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
