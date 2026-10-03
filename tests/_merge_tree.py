"""Replay two independent changes through `git merge-tree` (issue #1768).

GitHub decides whether a pull request can merge with git's ordinary line merge
and ignores custom merge drivers, so that is the merge these helpers run. Each
call builds a throwaway repository: one commit holding `base`, then `left` and
`right` committed on top of it separately, and `git merge-tree --write-tree`
reports whether the two merge and which files conflict.

The environment is scrubbed of every `GIT_*` variable and of the user's git
configuration. A pre-commit hook exports `GIT_DIR` and `GIT_INDEX_FILE`, and a
`git init` that inherits them re-initialises the repository the hook runs in
instead of the scratch one (the shared-checkout incident fixed by #1485).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path


def _env(home: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update({
        "HOME": str(home),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_AUTHOR_NAME": "merge probe",
        "GIT_AUTHOR_EMAIL": "merge-probe@example.invalid",
        "GIT_COMMITTER_NAME": "merge probe",
        "GIT_COMMITTER_EMAIL": "merge-probe@example.invalid",
    })
    return env


def git_has_merge_tree() -> bool:
    """`git merge-tree --write-tree` arrived in git 2.38."""
    if shutil.which("git") is None:
        return False
    out = subprocess.run(["git", "merge-tree", "-h"], capture_output=True,
                         text=True)
    return "--write-tree" in (out.stdout + out.stderr)


def _write(root: Path, files: dict[str, str | None]) -> None:
    for rel, text in files.items():
        path = root / rel
        if text is None:
            if path.exists():
                path.unlink()
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def merge(base: dict[str, str], left: dict[str, str | None],
          right: dict[str, str | None]) -> tuple[bool, list[str]]:
    """`(merges cleanly, conflicted files)` for two changes to `base`.

    Each argument maps a repo-relative path to its text; in `left` and
    `right` a path maps to its new text, or to None to delete it, and a path
    that is absent is unchanged."""
    scratch = Path(tempfile.mkdtemp(prefix="merge-tree-"))
    try:
        repo = scratch / "repo"
        repo.mkdir()
        env = _env(scratch)

        def git(*args: str) -> subprocess.CompletedProcess:
            return subprocess.run(["git", "-C", str(repo), *args], env=env,
                                  capture_output=True, text=True)

        def commit(files: dict, message: str) -> str:
            _write(repo, files)
            git("add", "-A")
            done = git("commit", "-q", "--allow-empty", "-m", message)
            assert done.returncode == 0, done.stderr
            return git("rev-parse", "HEAD").stdout.strip()

        assert git("init", "-q", "-b", "main").returncode == 0
        root = commit(base, "base")
        tip_left = commit(left, "left")
        assert git("checkout", "-q", root).returncode == 0
        tip_right = commit(right, "right")
        out = git("merge-tree", "--write-tree", "--name-only", "--no-messages",
                  tip_left, tip_right)
        assert out.returncode in (0, 1), out.stderr
        conflicted = [line for line in out.stdout.splitlines()[1:] if line]
        return out.returncode == 0, conflicted
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
