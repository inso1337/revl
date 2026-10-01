"""A test run started by a git hook must not write to the repository committing.

git exports the committing repository into its hooks (GIT_DIR and
GIT_INDEX_FILE, measured in a linked worktree on git 2.50, along with the
author identity and date of the commit), and tools/hooks/pre-commit runs
pytest. A fixture that shells out to `git init`,
`git config` or `git commit` in its tmp dir inherited them and acted on the
committing repository instead: the shared `.git/config` was set to
`core.bare = true` and `user.name = t`, and a worktree's index was replaced by a
fixture's README. `tests/conftest.py` now strips git's repository-local
variables when it is imported, and the hook strips them before it runs pytest.

The central test here builds a bystander repository with a linked worktree,
points GIT_DIR and GIT_INDEX_FILE at that worktree exactly as git does for a
hook, runs a fixture that does `git init` + `git config` + `git commit` in its
own tmp dir under that environment, and requires the bystander's config,
indexes and refs to be byte-for-byte what they were. The same run with
`--noconftest` is the control: it must CHANGE the bystander, which is what
shows the simulated environment really leaks and the strip is what stops it.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFTEST = ROOT / "tests" / "conftest.py"
HOOK = ROOT / "tools" / "hooks" / "pre-commit"
PROBE = "test_a_fixture_repository_lands_in_its_own_tmp_dir"

# A git that reads no user or system config, so the bystander and the probe
# behave the same on every machine (no signing, no default-branch surprises).
_HERMETIC = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}


def _git(cwd: Path, *args: str, env: dict | None = None) -> str:
    proc = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True,
                          text=True, env=env)
    assert proc.returncode == 0, f"git {' '.join(args)}: {proc.stderr}"
    return proc.stdout.strip()


def _stripped_list(name: str = "REPOSITORY_LOCAL_GIT_ENV") -> tuple[str, ...]:
    """A tuple of variable names as conftest.py spells it, read without
    importing conftest a second time."""
    for node in ast.parse(CONFTEST.read_text(encoding="utf-8")).body:
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and getattr(node.targets[0], "id", "") == name):
            return tuple(ast.literal_eval(node.value))
    raise AssertionError(f"tests/conftest.py no longer defines {name}")


# --------------------------------------------------------------------------- #
# The probe: what every git-using fixture in tests/ does, in miniature.        #
# --------------------------------------------------------------------------- #

def test_a_fixture_repository_lands_in_its_own_tmp_dir(tmp_path):
    """`git init`, a local identity, `git add`, `git commit`, all in tmp_path.

    Run directly this is an ordinary passing test. Run under a leaked hook
    environment it is the shape that set `core.bare = true` and
    `user.name = t` on the shared config."""
    repo = tmp_path / "fixture"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "README.md").write_text("fixture\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "fixture")
    assert (repo / ".git" / "HEAD").is_file(), "git init wrote somewhere else"
    assert Path(_git(repo, "rev-parse", "--show-toplevel")).resolve() == repo.resolve()
    assert _git(repo, "log", "--format=%an <%ae> %s") == "t <t@example.com> fixture"


# --------------------------------------------------------------------------- #
# The bystander: the repository a hook is running for.                         #
# --------------------------------------------------------------------------- #

def _bystander(tmp_path: Path) -> tuple[Path, Path]:
    """A main checkout with one commit and a linked worktree to commit from.

    Returns (main checkout, linked worktree)."""
    env = {**os.environ, **_HERMETIC}
    main = tmp_path / "bystander"
    main.mkdir()
    _git(main, "init", "-q", "-b", "main", env=env)
    _git(main, "config", "user.name", "Owner", env=env)
    _git(main, "config", "user.email", "owner@example.invalid", env=env)
    (main / "tracked.txt").write_text("owner's work\n", encoding="utf-8")
    _git(main, "add", "-A", env=env)
    _git(main, "commit", "-qm", "owner", env=env)
    linked = tmp_path / "linked"
    _git(main, "worktree", "add", "-q", "-b", "work", str(linked), env=env)
    (linked / "staged.txt").write_text("staged work\n", encoding="utf-8")
    _git(linked, "add", "staged.txt", env=env)
    return main, linked


def _snapshot(gitdir: Path) -> dict[str, bytes]:
    """Every file under the shared gitdir except objects: config, both
    indexes, HEADs, refs, reflogs and the worktree records."""
    out = {}
    for path in sorted(gitdir.rglob("*")):
        rel = path.relative_to(gitdir).as_posix()
        if path.is_file() and not rel.startswith("objects/"):
            out[rel] = path.read_bytes()
    return out


def _hook_env(main: Path, linked: Path) -> dict[str, str]:
    """The environment git gives a pre-commit hook in `linked`, measured on
    git 2.50: GIT_DIR is the worktree's gitdir and GIT_INDEX_FILE its index,
    plus the author identity and date of the commit being made. GIT_WORK_TREE
    is NOT exported, which is what lets `git init` guess bare."""
    gitdir = main / ".git" / "worktrees" / linked.name
    assert gitdir.is_dir(), gitdir
    return {**os.environ, **_HERMETIC,
            "GIT_DIR": str(gitdir), "GIT_INDEX_FILE": str(gitdir / "index"),
            "GIT_PREFIX": "", "GIT_EDITOR": ":",
            "GIT_AUTHOR_NAME": "Owner", "GIT_AUTHOR_EMAIL": "owner@example.invalid",
            "GIT_AUTHOR_DATE": "@1700000000 +0000"}


def _run_probe(env: dict, basetemp: Path, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
         f"--basetemp={basetemp}", *extra, f"{Path(__file__)}::{PROBE}"],
        cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=300)


def test_a_simulated_hook_environment_leaves_the_committing_repository_untouched(
        tmp_path):
    main, linked = _bystander(tmp_path)
    before = _snapshot(main / ".git")
    assert b"bare = false" in before["config"]

    proc = _run_probe(_hook_env(main, linked), tmp_path / "run")

    after = _snapshot(main / ".git")
    changed = sorted(k for k in before.keys() | after.keys()
                     if before.get(k) != after.get(k))
    assert not changed, (
        "a fixture run under the hook's environment wrote to the committing "
        f"repository: {changed}\nconfig now:\n{after.get('config', b'').decode()}")
    assert "1 passed" in proc.stdout, proc.stdout + proc.stderr


def test_without_the_conftest_strip_the_same_run_writes_to_it(tmp_path):
    """The control. `--noconftest` drops exactly the strip, and the same probe
    now re-initialises the bystander: its shared config goes bare and takes the
    fixture's identity. If this ever stops changing the bystander, the test
    above has stopped proving anything."""
    main, linked = _bystander(tmp_path)
    before = _snapshot(main / ".git")

    proc = _run_probe(_hook_env(main, linked), tmp_path / "run", "--noconftest")
    assert "1 failed" in proc.stdout, proc.stdout + proc.stderr

    config = (main / ".git" / "config").read_text(encoding="utf-8")
    assert "bare = true" in config, config
    assert "name = t" in config, config
    assert _snapshot(main / ".git") != before


# --------------------------------------------------------------------------- #
# The two places the strip lives.                                               #
# --------------------------------------------------------------------------- #

def test_the_stripped_list_covers_gits_own_and_this_session_is_clean():
    """git's `--local-env-vars` is the set it clears itself before acting on
    another repository. A git release that adds one shows up here."""
    ours = _stripped_list()
    gits = subprocess.run(["git", "rev-parse", "--local-env-vars"],
                          capture_output=True, text=True, check=True).stdout.split()
    assert gits, "git printed no repository-local variables"
    assert not set(gits) - set(ours), sorted(set(gits) - set(ours))
    identity = _stripped_list("HOOK_COMMIT_IDENTITY_ENV")
    assert "GIT_AUTHOR_NAME" in identity
    assert not [v for v in ours + identity if v in os.environ]


def test_the_hook_clears_them_after_selecting_and_before_any_pytest_run():
    """The selector needs GIT_INDEX_FILE (a `commit -a` stages into a temporary
    index), so the hook may only clear it after the selection, and must clear
    it before the first step that runs tests."""
    lines = HOOK.read_text(encoding="utf-8").splitlines()

    def first(pred) -> int:
        return next(i for i, line in enumerate(lines) if pred(line.strip()))

    select = first(lambda s: "tools/affected_tests.py" in s and s.startswith("SEL="))
    unset = first(lambda s: s == "unset $(git rev-parse --local-env-vars)")
    identity = first(lambda s: s.startswith("unset GIT_AUTHOR_NAME"))
    pytest_step = first(lambda s: s.startswith("step ") and '"$PY" -P -m pytest' in s)
    assert select < unset < pytest_step, (select, unset, pytest_step)
    assert select < identity < pytest_step, (select, identity, pytest_step)


def test_the_hook_runs_no_tests_for_a_commit_that_stages_nothing():
    """Issue #1449. The selector answers an empty change set with the FULL
    suite, its fail-safe for a diff it could not read, so a no-op commit ran
    every test under the 60s limit. The hook asks git first, and only git's
    positive answer (exit 0) skips; that check must run before the variables
    it reads through are cleared."""
    lines = [line.strip() for line in HOOK.read_text(encoding="utf-8").splitlines()]
    check = lines.index("if git diff --cached --quiet 2>/dev/null; then")
    assert lines[check + 1] == "NOTHING_STAGED=1"
    assert lines[check + 2] == 'else'
    assert lines[check + 3].startswith('SEL=$("$PY" tools/affected_tests.py')
    assert check < lines.index("unset $(git rev-parse --local-env-vars)")
    skip = lines.index('if [ -n "$NOTHING_STAGED" ]; then')
    first_pytest = next(i for i, s in enumerate(lines)
                        if s.startswith("step ") and '"$PY" -P -m pytest' in s)
    assert skip < first_pytest
