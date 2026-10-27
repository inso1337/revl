#!/usr/bin/env python3
"""Run the pre-PR checks against this branch and its merge with origin/main.

The branch checks run first. If they pass, a disposable worktree merges the
current origin/main into the branch and runs the same checks there. Since the
branch tree is green, output added by the merged-tree run identifies the
main-side or merge-interaction findings that CI would otherwise discover.
"""

from __future__ import annotations

import difflib
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import List, Optional, Tuple

MAIN_REF = "refs/remotes/origin/main"
FETCH_REFSPEC = f"+refs/heads/main:{MAIN_REF}"
TREE_WIDE_CHECKS = (
    (
        "encoding guards",
        (
            "-m",
            "pytest",
            "tests/test_read_text_encoding_guard.py",
            "tests/test_open_encoding_guard.py",
            "-q",
        ),
    ),
    (
        "census reproduction witness",
        ("tools/census_artifact.py", "--verify", "--strict"),
    ),
    ("selector mapping pins", ("-m", "pytest", "tests/test_affected_tests.py", "-q")),
)


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        text=True,
        capture_output=True,
        check=False,
    )


def _combined_output(proc: subprocess.CompletedProcess) -> str:
    return (proc.stdout or "") + (proc.stderr or "")


def _check_environment(
    root: Path, branch: str, *, tool_root: Path, merged: bool
) -> dict[str, str]:
    env = os.environ.copy()
    env.pop("PRE_PR_HEAD_BRANCH", None)
    if merged:
        # The temporary worktree is detached; preserve the real branch context
        # for checks that reject stale in-flight roadmap markers.
        env["PRE_PR_HEAD_BRANCH"] = branch

    python_path = str(root)
    if env.get("PYTHONPATH"):
        python_path += os.pathsep + env["PYTHONPATH"]
    env["PYTHONPATH"] = python_path

    # Use this worktree's environment for tools such as ruff/uvx when present.
    venv_bin = tool_root / ".venv" / "bin"
    if venv_bin.is_dir():
        env["PATH"] = str(venv_bin) + os.pathsep + env.get("PATH", "")
    return env


def _python(tool_root: Path) -> str:
    candidate = tool_root / ".venv" / "bin" / "python"
    return str(candidate) if candidate.is_file() else sys.executable


def _run_census_check(
    root: Path, branch: str, *, tool_root: Path, python: str, merged: bool
):
    args = [python, "tools/census_artifact.py", "--verify", "--strict"]
    if merged:
        env = _check_environment(root, branch, tool_root=tool_root, merged=True)
        return subprocess.run(
            args,
            cwd=root,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )

    # A venv inside the contributor's checkout can appear as unpinned input to
    # the census. Reproduce the branch commit in a clean worktree so ignored
    # environment files cannot change the published measurement.
    with tempfile.TemporaryDirectory(prefix="revl-census-ref-") as scratch:
        census_tree = Path(scratch) / "branch"
        added = _git(root, "worktree", "add", "--detach", str(census_tree), "HEAD")
        if added.returncode:
            output = "could not create a clean census worktree:\n" + _combined_output(
                added
            )
            return subprocess.CompletedProcess(args, 2, output, "")
        try:
            env = _check_environment(
                census_tree, branch, tool_root=tool_root, merged=False
            )
            proc = subprocess.run(
                args,
                cwd=census_tree,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                check=False,
            )
        finally:
            removed = _git(root, "worktree", "remove", "--force", str(census_tree))

        if removed.returncode:
            output = _combined_output(proc)
            output += "\ncould not remove the temporary census worktree:\n"
            output += _combined_output(removed)
            return subprocess.CompletedProcess(args, 2, output, "")
        return proc


def _run_pre_pr(root: Path, branch: str, *, tool_root: Path, merged: bool):
    env = _check_environment(root, branch, tool_root=tool_root, merged=merged)

    return subprocess.run(
        ["bash", "tools/pre_pr.sh"],
        cwd=root,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )


def _run_tree_wide_checks(root: Path, branch: str, *, tool_root: Path, merged: bool):
    env = _check_environment(root, branch, tool_root=tool_root, merged=merged)
    python = _python(tool_root)
    results = []
    for name, args in TREE_WIDE_CHECKS:
        if name == "census reproduction witness":
            proc = _run_census_check(
                root, branch, tool_root=tool_root, python=python, merged=merged
            )
        else:
            proc = subprocess.run(
                [python, *args],
                cwd=root,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                check=False,
            )
        results.append((name, proc.returncode, _combined_output(proc)))
    return results


def _has_failures(pre_pr, tree_wide) -> bool:
    return pre_pr.returncode != 0 or any(code != 0 for _, code, _ in tree_wide)


def _report_tree_failures(pre_pr, tree_wide) -> None:
    if pre_pr.returncode:
        print("  pre-PR checks failed:")
        print(_combined_output(pre_pr).rstrip())
    for name, code, output in tree_wide:
        if code:
            print(f"  {name} failed:")
            print(output.rstrip())


def _added_output(before: str, after: str) -> List[Tuple[str, str]]:
    """Return newly printed merged-tree lines with their check section."""
    before_lines = before.splitlines()
    after_lines = after.splitlines()
    added_indices = set()
    for tag, _i1, _i2, j1, j2 in difflib.SequenceMatcher(
        a=before_lines, b=after_lines, autojunk=False
    ).get_opcodes():
        if tag in ("insert", "replace"):
            added_indices.update(range(j1, j2))

    added = []
    section = ""
    for index, line in enumerate(after_lines):
        if line.startswith("== ") and line.endswith(" =="):
            section = line[3:-3]
        if index in added_indices:
            added.append((section, line))
    return added


def _repo_root() -> Optional[Path]:
    proc = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        text=True,
        capture_output=True,
        check=False,
    )
    if proc.returncode:
        return None
    return Path(proc.stdout.strip()).resolve()


def main(argv: Optional[List[str]] = None) -> int:
    if argv:
        print("usage: ./tools/pre_pr.sh --merge-ref", file=sys.stderr)
        return 2

    root = _repo_root()
    if root is None:
        print("pre-PR merge-ref: run this inside a Git worktree.", file=sys.stderr)
        return 2

    branch_proc = _git(root, "symbolic-ref", "--quiet", "--short", "HEAD")
    if branch_proc.returncode:
        print(
            "pre-PR merge-ref: check out a branch before running this check.",
            file=sys.stderr,
        )
        return 2
    branch = branch_proc.stdout.strip()

    status = _git(root, "status", "--porcelain", "--untracked-files=all")
    if status.returncode:
        print("pre-PR merge-ref: could not read Git worktree status.", file=sys.stderr)
        return 2
    if status.stdout.strip():
        print(
            "pre-PR merge-ref: commit or remove worktree changes first.",
            file=sys.stderr,
        )
        print(status.stdout.rstrip(), file=sys.stderr)
        return 2

    remote = _git(root, "remote", "get-url", "origin")
    if remote.returncode:
        print("pre-PR merge-ref: remote 'origin' is not configured.", file=sys.stderr)
        return 2

    print("pre-PR merge-ref: fetching current origin/main.", flush=True)
    fetch = _git(root, "fetch", "--no-tags", "origin", FETCH_REFSPEC)
    if fetch.returncode:
        print("pre-PR merge-ref: could not fetch origin/main.", file=sys.stderr)
        print(_combined_output(fetch).rstrip(), file=sys.stderr)
        return 2

    print(f"pre-PR merge-ref: checking branch tree ({branch}).", flush=True)
    branch_check = _run_pre_pr(root, branch, tool_root=root, merged=False)
    branch_output = _combined_output(branch_check)
    branch_invariants = _run_tree_wide_checks(
        root, branch, tool_root=root, merged=False
    )
    if _has_failures(branch_check, branch_invariants):
        print(
            "pre-PR merge-ref: branch-tree checks failed; fix them before "
            "checking the merge tree."
        )
        _report_tree_failures(branch_check, branch_invariants)
        return 1
    print("pre-PR merge-ref: branch-tree checks passed.", flush=True)

    with tempfile.TemporaryDirectory(prefix="revl-merge-ref-") as scratch:
        merged_tree = Path(scratch) / "merged"
        added = _git(root, "worktree", "add", "--detach", str(merged_tree), "HEAD")
        if added.returncode:
            print(
                "pre-PR merge-ref: could not create a temporary worktree.",
                file=sys.stderr,
            )
            print(_combined_output(added).rstrip(), file=sys.stderr)
            return 2

        result = 2
        try:
            merge = _git(
                merged_tree, "merge", "--no-commit", "--no-ff", "--no-edit", MAIN_REF
            )
            if merge.returncode:
                print(
                    "pre-PR merge-ref: origin/main does not merge cleanly "
                    f"into {branch}; resolve the merge before pushing.",
                    file=sys.stderr,
                )
                print(_combined_output(merge).rstrip(), file=sys.stderr)
            else:
                print("pre-PR merge-ref: checking the merged tree.", flush=True)
                merged_check = _run_pre_pr(
                    merged_tree, branch, tool_root=root, merged=True
                )
                merged_output = _combined_output(merged_check)
                merged_invariants = _run_tree_wide_checks(
                    merged_tree, branch, tool_root=root, merged=True
                )
                if not _has_failures(merged_check, merged_invariants):
                    print(
                        "pre-PR merge-ref: merged-tree checks passed; no new findings."
                    )
                    result = 0
                else:
                    print(
                        "pre-PR merge-ref: merged-tree checks failed; the "
                        "branch tree passed, so these findings arise from "
                        "current main or its interaction with the branch."
                    )
                    if merged_check.returncode:
                        delta = _added_output(branch_output, merged_output)
                        if delta:
                            print("New pre-PR output relative to the branch tree:")
                            for section, line in delta:
                                label = f"[{section}] " if section else ""
                                print(f"  {label}+ {line}")
                        else:
                            print(
                                "Pre-PR output did not add separable lines; "
                                "full merged-tree output follows:"
                            )
                            print(merged_output.rstrip())
                    for name, code, output in merged_invariants:
                        if code:
                            print(f"New merged-tree finding: {name} failed.")
                            print(output.rstrip())
                    result = 1
        finally:
            removed = _git(root, "worktree", "remove", "--force", str(merged_tree))
            if removed.returncode:
                print(
                    "pre-PR merge-ref: could not fully remove the temporary "
                    "worktree; run `git worktree prune`.",
                    file=sys.stderr,
                )
                print(_combined_output(removed).rstrip(), file=sys.stderr)
                result = 2
        return result


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv[1:]))
