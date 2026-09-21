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

USAGE
    python3 tools/check_merged_prs_landed.py                  # live, via gh
    python3 tools/check_merged_prs_landed.py --from-json f.json  # offline
Exit 0 when every merged PR is accounted for, 1 on any finding, 2 when the
question could not be asked (no `gh`, no network, an absent merge commit). An
unanswered question is never reported as a pass.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "tools" / "merged_pr_landing_baseline.json"
DEFAULT_REPO = "inso1337/revl"
DEFAULT_MAIN = "origin/main"
# The GitHub fields this needs. `mergeCommit` is the load-bearing one; the rest
# are for the report, so a finding names the PR a human can go and look at.
GH_FIELDS = "number,title,state,baseRefName,headRefName,mergeCommit,mergedAt"


def load_baseline(path: Path) -> dict[str, str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    entries = data.get("unreachable", {})
    return {str(k): str(v) for k, v in entries.items()}


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


def audit(prs, root: Path, main_ref: str, baseline: dict[str, str]):
    """Returns (findings, known, unanswerable), each a list of report lines."""
    findings: list[str] = []
    known: list[str] = []
    unanswerable: list[str] = []
    for pr in prs:
        num = str(pr.get("number"))
        title = (pr.get("title") or "").strip()
        base = pr.get("baseRefName") or "?"
        merge = (pr.get("mergeCommit") or {}).get("oid")
        if not merge:
            unanswerable.append(
                f"#{num}: GitHub records no merge commit, so whether its work "
                f"reached {main_ref} cannot be decided here ({title})")
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
        if not object_exists(root, merge):
            unanswerable.append(
                f"#{num}: merge commit {merge[:12]} is not in this clone, so "
                f"reachability from {main_ref} is unknown. Fetch it "
                f"(`git fetch origin {merge}`) or check out with full history.")
            continue
        line = (f"#{num}: MERGED into `{base}`, but its merge commit "
                f"{merge[:12]} is NOT reachable from {main_ref} ({title})")
        if num in baseline:
            known.append(f"{line} [known: {baseline[num]}]")
        else:
            findings.append(line)
    return findings, known, unanswerable


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo", default=DEFAULT_REPO)
    ap.add_argument("--limit", type=int, default=200,
                    help="how many merged PRs to read, newest first")
    ap.add_argument("--main-ref", default=DEFAULT_MAIN)
    ap.add_argument("--from-json", type=Path, default=None,
                    help="read the PR list from a file instead of calling gh")
    ap.add_argument("--baseline", type=Path, default=BASELINE)
    ap.add_argument("--root", type=Path, default=ROOT)
    args = ap.parse_args(argv)

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
            return 2

    baseline = load_baseline(args.baseline)
    findings, known, unanswerable = audit(
        prs, args.root, args.main_ref, baseline)

    print(f"read {len(prs)} merged pull request(s) against {args.main_ref}")
    for line in known:
        print(f"  known  {line}")
    for line in findings:
        print(f"  FOUND  {line}")
    for line in unanswerable:
        print(f"  UNKNOWN {line}")

    if findings:
        print(f"\n{len(findings)} merged pull request(s) whose work is not in "
              f"{args.main_ref}.", file=sys.stderr)
        return 1
    if unanswerable:
        print(f"\n{len(unanswerable)} merged pull request(s) could not be "
              f"decided.", file=sys.stderr)
        return 2
    if known:
        print(f"\nno new finding: {len(known)} merged pull request(s) have an "
              f"unreachable merge commit and are accounted for in "
              f"{args.baseline}.")
    else:
        print(f"every merged pull request's merge commit is reachable from "
              f"{args.main_ref}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
