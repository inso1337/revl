#!/usr/bin/env python3
"""Assert that every pull request GitHub reports as MERGED is actually in `main`.

WHY THIS EXISTS (issue #1342). Four pull requests -- #1318, #1320, #1296 and
#1319 -- read MERGED in the GitHub UI with none of their work in `main`. Each
was merged into a BASE BRANCH that had itself already been squash-merged, so
the merge landed on a branch nothing would ever merge again. `main` has no
`src/revl/ui_action.py`, no ladder rungs and no target binding, and every check
on every one of those PRs was green. The merge automation now refuses a base
that is not `main` and retargets a PR whose base branch has landed, but that is
a habit applied going forward; this is the check that reads the record.

Run over the newest 200 merged pull requests it named EIGHT, not four. Three
more are stranded the same way (#1305, #1308, #1309, measured by a file each
merge added and main does not have) and one more was carried under a different
commit. The four in the issue were the four somebody happened to look at.

WHY `--is-ancestor` ON THE PR HEAD CANNOT ANSWER IT. A squash merge writes the
PR's content into ONE NEW COMMIT on the base branch and leaves the head commit
exactly where it was. The head is therefore unreachable from `main` after a
perfectly healthy squash merge, and it is also unreachable after a stranding,
so the head test returns the same answer for both and carries no information.
Worse, the stranded base branches are still alive on the remote with the
PR head reachable from them, so nothing about the head looks wrong.

WHAT IS ASSERTED INSTEAD. `mergeCommit` is the commit GitHub created when it
merged the PR, on the BASE BRANCH, whatever strategy was used -- the squash
commit for a squash merge, the merge commit for a merge commit, the replayed
tip for a rebase merge. That commit is the PR's work in its landed form, so:

    git merge-base --is-ancestor <mergeCommit> origin/main

is the honest question. Verified against the record: false for all four PRs
the issue names and for four more it did not, true for all 192 other merged
PRs in the newest 200, including #1335 and #1346, which landed normally on the
same day. One git call per PR.

A "content is in main" test -- diffing trees, or matching patch-ids -- was
considered and rejected as the primary assertion. Later commits routinely
modify the same lines, so a content match goes false for work that DID land,
which is the fail-loud-on-healthy direction; and a cherry-picked subset would
match while the rest of the PR was still missing, which is the fail-open
direction. The merge commit is a fact GitHub records, not an inference.

WHAT THIS DELIBERATELY DOES NOT DECIDE. A STACKED pull request merged into a
live parent branch that is LATER squash-merged to main has its work in main
under the parent's squash commit, and its own merge commit still unreachable.
Ancestry cannot tell that apart from a stranding, because a squash breaks the
link in both cases. So this reports it, and the baseline note records which one
it was and on what evidence. That is the honest split: the tool asserts a fact
it can check, and a human states the judgement it cannot. Measured on the
record at the time of writing: eight merged PRs have an unreachable merge
commit, four of them stranded, four carried or reworked elsewhere.

RATCHET. `tools/merged_pr_landing_baseline.json` holds the PRs already known to
be unreachable, so this fails on a NEW one rather than on the backlog. It
shrinks only: a baselined PR whose merge commit becomes reachable was merged
properly after all, and that is a FAILURE too, with the instruction to delete
the entry. A baseline that may grow silently is not a ratchet.

FETCH BEFORE JUDGING (issue #1377). The PR list is read from GitHub and the
ancestry is read from a local clone, so the two are snapshots of the same
repository taken at different moments. A PR that merges AFTER the clone and
BEFORE `gh pr list` returns names a merge commit the clone has never heard of,
and reporting that as anything but "refresh and look again" is a false alarm.
Pre-fetching in the workflow does not close it: the fetch runs before the PR
list is read, so the list is always the fresher of the two. The refresh has to
happen on the MISS, after the question is asked. So a merge commit that does
not resolve refreshes the remote-tracking refs once, then asks the remote for
that one commit, and only then gives an answer. `main` is re-read after the
refresh too, because the commit that just merged is on it.

THREE ANSWERS, THREE WORDS. The failure paths used to share the word UNKNOWN,
which is what made the false alarm indistinguishable from the real thing:

    UNREACHABLE  the merge commit resolves, and is not an ancestor of `main`.
                 This is the finding. It is stated as the fact it is: the
                 baseline note, not this tool, says whether that means the
                 work is stranded or was carried under another commit.
    UNRESOLVED   the merge commit could not be resolved even after a refresh.
                 Nothing has been decided about `main`; the clone is stale, or
                 the object is gone from the remote.
    UNRECORDED   GitHub records no merge commit for the PR at all.

The distinction is not cosmetic. A real stranding whose base branch was
deleted is absent from a fresh clone exactly like a commit from the future is,
so before this the gate's own evidence read as noise. Asking the remote for
the commit by sha resolves the stranding and leaves only the genuinely
unanswerable as UNRESOLVED.

THE NOTE IS RE-MEASURED TOO (issue #1434). Ancestry is the one property of
a stranded PR that never changes, so on its own this check stayed green while
five of eight baseline notes went stale. Each entry therefore carries a
structured witness beside its prose, and `tools/landing_witness.py` re-checks
it on every run: the merge's ADDED inventory, the byte-identity of every file
claimed CARRIED at the commit that carried it (not at HEAD, so later edits
cost nothing), and the tests named for a file that was carried modified.
The recorded `merge` sha is also cross-checked against the one GitHub reports,
so the witness is never measured against the wrong commit. An entry whose
claim cannot be checked (REWORKED) is printed as UNCHECKED, never as verified.

USAGE
    python3 tools/check_merged_prs_landed.py                  # live, via gh
    python3 tools/check_merged_prs_landed.py --from-json f.json  # offline
    python3 tools/check_merged_prs_landed.py --no-fetch       # never touch the network
    python3 tools/check_merged_prs_landed.py --witness-only   # no gh; notes only
    python3 tools/check_merged_prs_landed.py --pinned run     # run the pins (CI)
Exit 0 when every merged PR is accounted for, 1 on any finding, 2 when the
question could not be asked (no `gh`, no network, an absent merge commit). An
unanswered question is never reported as a pass.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "tools" / "merged_pr_landing_baseline.json"
DEFAULT_REPO = "inso1337/revl"
DEFAULT_MAIN = "origin/main"
# The GitHub fields this needs. `mergeCommit` is the load-bearing one; the rest
# are for the report, so a finding names the PR a human can go and look at.
GH_FIELDS = "number,title,state,baseRefName,headRefName,mergeCommit,mergedAt"


def load_entries(path: Path) -> dict:
    """The raw `unreachable` entries, each an object with a witness."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {str(k): v for k, v in data.get("unreachable", {}).items()}


def load_baseline(path: Path) -> dict[str, str]:
    """{PR number: prose note}, which is all the ancestry audit reads."""
    return {k: str(v.get("note", "")) if isinstance(v, dict) else str(v)
            for k, v in load_entries(path).items()}


def load_witness_module():
    """`tools/landing_witness.py`, loaded by PATH under a name nothing else
    uses. A bare `import` would bind whichever same-named module is found
    first, and it would also need `tools/` on `sys.path`, which a test that
    loads this file by path does not have."""
    spec = importlib.util.spec_from_file_location(
        "revl_tools_landing_witness",
        Path(__file__).resolve().with_name("landing_witness.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def merge_sha_mismatches(prs, entries: dict) -> list[str]:
    """A baselined PR whose recorded `merge` is not the commit GitHub reports.
    The witness is measured against the recorded sha, so a wrong one would
    check some other commit's files and call it this PR's."""
    out = []
    for pr in prs:
        num = str(pr.get("number"))
        raw = entries.get(num)
        oid = (pr.get("mergeCommit") or {}).get("oid")
        if isinstance(raw, dict) and oid and raw.get("merge") != oid:
            out.append(f"#{num}: the baseline records merge commit "
                       f"{str(raw.get('merge'))[:12]}, GitHub reports "
                       f"{oid[:12]}. The witness would be measured against "
                       f"the wrong commit")
    return out


def fetch_merged_prs(repo: str, limit: int) -> list[dict]:
    """Merged PRs, newest first, straight from `gh`."""
    proc = subprocess.run(
        ["gh", "pr", "list", "--repo", repo, "--state", "merged",
         "--limit", str(limit), "--json", GH_FIELDS],
        capture_output=True, text=True, check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"gh pr list failed: {proc.stderr.strip()}")
    return json.loads(proc.stdout)


def is_ancestor(root: Path, sha: str, main_ref: str) -> bool:
    """The one git call per PR."""
    return subprocess.run(
        ["git", "-C", str(root), "merge-base", "--is-ancestor", sha, main_ref],
        capture_output=True, text=True, check=False,
    ).returncode == 0


def object_exists(root: Path, sha: str) -> bool:
    """Only ever called on the failure path, to tell `not in main` apart from
    `not in this clone`. A shallow checkout must not be read as a stranding."""
    return subprocess.run(
        ["git", "-C", str(root), "cat-file", "-e", f"{sha}^{{commit}}"],
        capture_output=True, text=True, check=False,
    ).returncode == 0


def remote_of(main_ref: str) -> str | None:
    """`origin/main` -> `origin`. A ref with no slash names a local branch, so
    there is no remote to refresh from and nothing to do."""
    head, sep, rest = main_ref.partition("/")
    return head if sep and rest else None


class Remote:
    """The clone's link to the remote, refreshed lazily and at most once.

    Every call here is on the MISS path. A run where every merge commit
    resolves makes no network call at all, which is the common case and keeps
    this as cheap as it was. `enabled=False` is the offline mode: the tests
    drive a synthetic repository with no remote, and a gate that quietly
    reaches the network during a unit test is its own kind of dishonest.
    """

    def __init__(self, root: Path, main_ref: str, enabled: bool = True,
                 timeout: float = 120.0) -> None:
        self.root = root
        self.name = remote_of(main_ref) if enabled else None
        self.timeout = timeout
        self._refreshed: bool | None = None
        self.note = ""

    def _git(self, *args: str) -> bool:
        try:
            proc = subprocess.run(
                ["git", "-C", str(self.root), *args],
                capture_output=True, text=True, check=False,
                timeout=self.timeout)
        except (OSError, subprocess.SubprocessError) as exc:
            self.note = str(exc)
            return False
        if proc.returncode != 0:
            self.note = (proc.stderr or proc.stdout).strip().splitlines()[-1:]
            self.note = self.note[0] if self.note else "git fetch failed"
        return proc.returncode == 0

    def refresh(self) -> bool:
        """Bring every branch up to date, once per run. The merge commits sit
        on the base branches rather than on `main`, so refreshing `main` alone
        would leave exactly the commits this reads behind."""
        if self.name is None:
            return False
        if self._refreshed is None:
            self._refreshed = self._git(
                "fetch", "--no-tags", "--quiet", self.name,
                f"+refs/heads/*:refs/remotes/{self.name}/*")
        return self._refreshed

    def fetch_commit(self, sha: str) -> bool:
        """Last resort: ask the remote for one commit by sha. This is what
        rescues a real finding whose base branch has since been deleted, which
        no branch refspec will ever bring in."""
        if self.name is None:
            return False
        return self._git("fetch", "--no-tags", "--quiet", self.name, sha)

    def resolve(self, sha: str) -> bool:
        """True once `sha` is a commit in this clone, refreshing to get there."""
        if object_exists(self.root, sha):
            return True
        self.refresh()
        if object_exists(self.root, sha):
            return True
        self.fetch_commit(sha)
        return object_exists(self.root, sha)

    def why_unresolved(self) -> str:
        """What a human should do about a commit that would not resolve. The
        three cases want three different actions, so they get three sentences
        rather than one that covers them all and helps with none."""
        if self.name is None:
            return ("no refresh was attempted: fetching is off, or "
                    "--main-ref names a local branch with no remote behind it")
        if not self.refresh():
            reason = f": {self.note}" if self.note else ""
            return f"the refresh did not reach `{self.name}`{reason}"
        return (f"`{self.name}` was refreshed and the commit is still not "
                f"there, so it is on no ref the remote still serves")


class Audit(NamedTuple):
    """Four outcomes, kept apart on purpose (issue #1377).

    `findings` and `known` are answers. `unresolved` and `unrecorded` are the
    two ways the question does not get answered, and they are separate because
    a human does different things about them: refresh a clone, or go and look
    at a pull request GitHub has no merge commit for.
    """

    findings: list[str]
    known: list[str]
    unresolved: list[str]
    unrecorded: list[str]


def audit(prs, root: Path, main_ref: str, baseline: dict[str, str],
          remote: "Remote | None" = None) -> Audit:
    """Read every PR's merge commit against `main_ref`. One git call per PR on
    the happy path; a refresh only where a commit does not resolve."""
    findings: list[str] = []
    known: list[str] = []
    unresolved: list[str] = []
    unrecorded: list[str] = []
    for pr in prs:
        num = str(pr.get("number"))
        title = (pr.get("title") or "").strip()
        base = pr.get("baseRefName") or "?"
        merge = (pr.get("mergeCommit") or {}).get("oid")
        if not merge:
            unrecorded.append(
                f"#{num}: GitHub records no merge commit, so whether its work "
                f"reached {main_ref} cannot be decided here ({title})")
            continue
        if not is_ancestor(root, merge, main_ref):
            # The miss path, and the only place that touches the network. A
            # commit that does not resolve has decided nothing yet; one that
            # merged since this clone was made becomes an ancestor the moment
            # `main_ref` itself is refreshed, which is the false alarm.
            if not object_exists(root, merge):
                if remote is None or not remote.resolve(merge):
                    why = ("no refresh was attempted" if remote is None
                           else remote.why_unresolved())
                    unresolved.append(
                        f"#{num}: merge commit {merge[:12]} could not be "
                        f"resolved ({why}). NOTHING is claimed about "
                        f"{main_ref} here: this is a clone that cannot see "
                        f"the commit, not a pull request whose work is "
                        f"missing ({title})")
                    continue
        if is_ancestor(root, merge, main_ref):
            if num in baseline:
                findings.append(
                    f"#{num}: baselined as unreachable, but its merge "
                    f"commit {merge[:12]} IS now reachable from {main_ref}. It "
                    f"was merged properly after all; delete the entry from "
                    f"{BASELINE.relative_to(ROOT)} so the ratchet keeps "
                    f"shrinking.")
            continue
        line = (f"#{num}: MERGED into `{base}`, but its merge commit "
                f"{merge[:12]} is NOT reachable from {main_ref} ({title})")
        if num in baseline:
            known.append(f"{line} [known: {baseline[num]}]")
        else:
            findings.append(line)
    return Audit(findings, known, unresolved, unrecorded)


def _parse_args(argv):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo", default=DEFAULT_REPO)
    ap.add_argument("--limit", type=int, default=200,
                    help="how many merged PRs to read, newest first")
    ap.add_argument("--main-ref", default=DEFAULT_MAIN)
    ap.add_argument("--from-json", type=Path, default=None,
                    help="read the PR list from a file instead of calling gh")
    ap.add_argument("--baseline", type=Path, default=BASELINE)
    ap.add_argument("--root", type=Path, default=ROOT)
    ap.add_argument("--no-fetch", action="store_true",
                    help="never refresh from the remote. A merge commit this "
                         "clone cannot resolve is then reported UNRESOLVED "
                         "rather than looked up, which is honest but weaker.")
    ap.add_argument("--witness-only", action="store_true",
                    help="skip the ancestry audit (no gh) and re-measure only "
                         "the baseline's witnesses")
    ap.add_argument("--pinned", choices=("collect", "run"), default="collect",
                    help="for the tests an entry names: `collect` proves they "
                         "exist, `run` proves they pass (CI runs them)")
    ap.add_argument("--python", default=sys.executable,
                    help="the interpreter that runs pytest over the pins")
    return ap.parse_args(argv)


def _read_prs(args) -> list[dict] | None:
    """The merged PR list, or None when the question could not be asked."""
    if args.from_json is not None:
        prs = json.loads(args.from_json.read_text(encoding="utf-8"))
    else:
        try:
            prs = fetch_merged_prs(args.repo, args.limit)
        except (OSError, RuntimeError, ValueError) as exc:
            print(f"could not read the merged pull requests: {exc}",
                  file=sys.stderr)
            print("the question was not asked, so it has not been answered "
                  "green", file=sys.stderr)
            return None
    # An empty list is not "nothing is stranded". A repository this old has
    # hundreds of merged pull requests, so no rows means the read failed in a
    # way that still exited 0 -- a token without `pull-requests: read`, a
    # rewritten API response, a `--limit 0`. Reporting that green is the exact
    # fail-open shape this check exists to remove.
    if not prs:
        print("read NO merged pull requests. That is not a result: this "
              "repository has hundreds, so an empty list means the read "
              "failed. Check the token's `pull-requests: read` scope and "
              "`--limit`.", file=sys.stderr)
        return None
    return prs


def _print_ancestry(result: Audit, n_prs: int, main_ref: str) -> None:
    print(f"read {n_prs} merged pull request(s) against {main_ref}")
    for line in result.known:
        print(f"  known       {line}")
    for line in result.findings:
        print(f"  UNREACHABLE {line}")
    for line in result.unresolved:
        print(f"  UNRESOLVED  {line}")
    for line in result.unrecorded:
        print(f"  UNRECORDED  {line}")


def _print_witness(report, n_entries: int, root: Path, mode: str) -> None:
    print(f"\nre-measured the witness of {n_entries} baselined entr"
          f"{'y' if n_entries == 1 else 'ies'} against the tree at {root} "
          f"(pinned tests: {mode})")
    for line in report.verified:
        print(f"  verified    {line}")
    for line in report.unchecked:
        print(f"  UNCHECKED   {line}")
    for line in report.findings:
        print(f"  WITNESS     {line}")
    for line in report.unresolved:
        print(f"  UNRESOLVED  {line}")
    print(f"  {len(report.verified)} verified, {len(report.unchecked)} "
          f"unchecked, {len(report.findings)} finding(s), "
          f"{len(report.unresolved)} unresolved")


def main(argv=None) -> int:
    args = _parse_args(argv)
    entries = load_entries(args.baseline)
    remote = Remote(args.root, args.main_ref, enabled=not args.no_fetch)

    findings: list[str] = []
    unanswered: list[str] = []
    known: list[str] = []
    if not args.witness_only:
        prs = _read_prs(args)
        if prs is None:
            return 2
        result = audit(prs, args.root, args.main_ref, load_baseline(
            args.baseline), remote=remote)
        _print_ancestry(result, len(prs), args.main_ref)
        mismatches = merge_sha_mismatches(prs, entries)
        for line in mismatches:
            print(f"  MERGE SHA   {line}")
        findings += result.findings + mismatches
        known = result.known
        if result.unresolved:
            unanswered.append(f"{len(result.unresolved)} whose merge commit "
                              f"this clone could not resolve")
        if result.unrecorded:
            unanswered.append(f"{len(result.unrecorded)} for which GitHub "
                              f"records no merge commit")

    witness = load_witness_module().audit(
        entries, args.root, remote.resolve, mode=args.pinned,
        python=args.python)
    if entries:
        _print_witness(witness, len(entries), args.root, args.pinned)
    findings += witness.findings
    if witness.unresolved:
        unanswered.append(f"{len(witness.unresolved)} baselined entr"
                          f"{'y' if len(witness.unresolved) == 1 else 'ies'}"
                          f" whose witness could not be measured")

    if findings:
        print(f"\n{len(findings)} finding(s): a merged pull request whose "
              f"work is not in {args.main_ref}, or a baseline entry whose "
              f"recorded claim is false.", file=sys.stderr)
        return 1
    # Not a finding and not a pass: the check did not get an answer, and the
    # line says which part it was.
    if unanswered:
        print(f"\nnothing is claimed for {', '.join(unanswered)}.",
              file=sys.stderr)
        return 2
    if args.witness_only:
        print(f"\nno finding: {len(witness.verified)} witness(es) verified, "
              f"{len(witness.unchecked)} entr"
              f"{'y' if len(witness.unchecked) == 1 else 'ies'} not "
              f"machine-checkable (listed UNCHECKED above).")
    elif known:
        print(f"\nno new finding: {len(known)} merged pull request(s) "
              f"have an unreachable merge commit and are accounted for in "
              f"{args.baseline}; {len(witness.verified)} of their witnesses "
              f"verified, {len(witness.unchecked)} not machine-checkable.")
    else:
        print(f"every merged pull request's merge commit is reachable from "
              f"{args.main_ref}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
