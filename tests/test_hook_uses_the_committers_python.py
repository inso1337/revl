"""tools/hooks/pre-commit runs every check on the committer's python (issue #1608).

In a linked worktree with no `.venv` of its own, the hook used to run pytest
from the PRIMARY checkout's `.venv`, ignoring the virtualenv the person
committing had active. Measured from a lane worktree: python 3.14 instead of
the active 3.12, no cordis-py runtime (so every runtime test skipped), and an
editable revl naming the primary checkout's src/, which a child started without
the hook's PYTHONPATH imported. The selector and the conformance step ran on
`python3` from PATH, a third interpreter.

These tests build a throwaway repository with a linked worktree, copy the
hook in, and replace pytest, the selector and the conformance matrix with stubs
that only record which python ran them. Then they commit from the worktree and
check who ran: always the committer's python, and never another checkout's.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import venv
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HOOKS = ROOT / "tools" / "hooks"

if sys.platform == "win32":  # the hook is a POSIX shell script
    pytest.skip("the pre-commit hook runs under sh", allow_module_level=True)

# Each stub appends "<sys.executable>\t<what ran>" to $HOOK_LOG.
_RECORD = (
    "import os, sys\n"
    "with open(os.environ['HOOK_LOG'], 'a') as f:\n"
    "    f.write(sys.executable + '\\t' + {what!r} + '\\n')\n"
)
_SELECTOR = _RECORD.format(what="selector") + "print('FULL 0')\nprint('PYTEST tests')\n"
_CONFORMANCE = _RECORD.format(what="conformance")
_PYTEST_MAIN = _RECORD.format(what="pytest")


def _run(args, cwd, env=None):
    proc = subprocess.run(args, cwd=str(cwd), env=env, capture_output=True, text=True,
                          timeout=120)
    return proc


def _git(cwd, *args, env):
    proc = _run(["git", *args], cwd, env)
    assert proc.returncode == 0, f"git {' '.join(args)}: {proc.stderr}"
    return proc.stdout.strip()


def _make_venv(path: Path, *, with_pytest: bool, revl_from: Path | None = None) -> Path:
    """A real venv. `with_pytest` installs a stub pytest that records who ran
    it; `revl_from` adds an editable-style `.pth` naming that tree's src/."""
    venv.create(path, with_pip=False, symlinks=True)
    python = path / "bin" / "python"
    purelib = Path(_run([str(python), "-c",
                         "import sysconfig; print(sysconfig.get_paths()['purelib'])"],
                        path).stdout.strip())
    if with_pytest:
        package = purelib / "pytest"
        package.mkdir()
        (package / "__init__.py").write_text("", encoding="utf-8")
        (package / "__main__.py").write_text(_PYTEST_MAIN, encoding="utf-8")
        script = path / "bin" / "pytest"
        script.write_text(f"#!{python}\nimport runpy\n"
                          "runpy.run_module('pytest', run_name='__main__')\n",
                          encoding="utf-8")
        script.chmod(0o755)
    if revl_from is not None:
        (purelib / "_revl.pth").write_text(f"{revl_from / 'src'}\n", encoding="utf-8")
    return python


@pytest.fixture
def checkouts(tmp_path):
    """A primary checkout whose `.venv` has an editable revl of its own src/,
    and a linked worktree with no venv, holding a staged change."""
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("GIT_") and k not in ("VIRTUAL_ENV", "PYTHONPATH",
                                                      "REVL_HOOK_PYTHON")}
    env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1",
               HOOK_LOG=str(tmp_path / "hook.log"))
    primary = tmp_path / "primary"
    (primary / "tools" / "hooks").mkdir(parents=True)
    for hook in HOOKS.iterdir():
        target = primary / "tools" / "hooks" / hook.name
        shutil.copy2(hook, target)
        target.chmod(0o755)
    (primary / "tools" / "affected_tests.py").write_text(_SELECTOR, encoding="utf-8")
    (primary / "tools" / "conformance.py").write_text(_CONFORMANCE, encoding="utf-8")
    (primary / "src" / "revl").mkdir(parents=True)
    (primary / "src" / "revl" / "__init__.py").write_text("", encoding="utf-8")
    (primary / ".gitignore").write_text(".venv/\n", encoding="utf-8")
    _git(primary, "init", "-q", "-b", "main", env=env)
    _git(primary, "config", "user.email", "t@example.com", env=env)
    _git(primary, "config", "user.name", "t", env=env)
    _git(primary, "add", "-A", env=env)
    _git(primary, "commit", "-qm", "base", "--no-verify", env=env)
    _git(primary, "config", "core.hooksPath", "tools/hooks", env=env)
    primary_python = _make_venv(primary / ".venv", with_pytest=True, revl_from=primary)

    worktree = tmp_path / "lane"
    _git(primary, "worktree", "add", "-q", "-b", "lane", str(worktree), env=env)
    (worktree / "src" / "revl" / "change.py").write_text("X = 1\n", encoding="utf-8")
    _git(worktree, "add", "-A", env=env)
    return {"env": env, "primary": primary, "primary_python": primary_python,
            "worktree": worktree, "log": tmp_path / "hook.log", "tmp": tmp_path}


def _commit(c, **extra):
    env = dict(c["env"], **extra)
    return _run(["git", "commit", "-qm", "change"], c["worktree"], env)


def _ran(c) -> list[tuple[str, str]]:
    if not c["log"].exists():
        return []
    return [tuple(line.split("\t")) for line in
            c["log"].read_text(encoding="utf-8").splitlines()]


def test_a_worktree_commit_runs_every_step_on_the_active_virtualenv(checkouts):
    c = checkouts
    lane_python = _make_venv(c["tmp"] / "lane-venv", with_pytest=True)
    proc = _commit(c, VIRTUAL_ENV=str(c["tmp"] / "lane-venv"))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    ran = _ran(c)
    assert {what for _, what in ran} == {"selector", "pytest", "conformance"}, ran
    pythons = {Path(python) for python, _ in ran}
    assert pythons == {lane_python}, (
        f"the hook ran {ran}; every step should run on the active virtualenv "
        f"{lane_python}, not the primary checkout's {c['primary_python']}")


def test_the_hook_refuses_another_checkouts_venv(checkouts):
    """Activating the primary checkout's venv and committing in the worktree:
    that venv's revl is the primary's src/, so the hook refuses, by name."""
    c = checkouts
    proc = _commit(c, VIRTUAL_ENV=str(c["primary"] / ".venv"))
    out = proc.stdout + proc.stderr
    assert proc.returncode != 0, out
    assert "imports revl from" in out and str(c["primary"] / "src" / "revl") in out, out
    assert _ran(c) == [], "a refused hook must run nothing"


def test_no_python_with_pytest_is_refused_not_replaced(checkouts):
    """No virtualenv active, no `.venv` in the worktree, and the python3 on
    PATH has no pytest. The hook used to fall back to the primary checkout's
    `.venv`; it now refuses."""
    c = checkouts
    bare = _make_venv(c["tmp"] / "bare", with_pytest=False)
    path = f"{bare.parent}{os.pathsep}{c['env']['PATH']}"
    proc = _commit(c, PATH=path)
    out = proc.stdout + proc.stderr
    assert proc.returncode != 0, out
    assert "cannot import pytest" in out, out
    assert _ran(c) == []


def test_revl_hook_python_outranks_the_active_virtualenv(checkouts):
    c = checkouts
    _make_venv(c["tmp"] / "lane-venv", with_pytest=True)
    chosen = _make_venv(c["tmp"] / "chosen", with_pytest=True)
    proc = _commit(c, VIRTUAL_ENV=str(c["tmp"] / "lane-venv"),
                   REVL_HOOK_PYTHON=str(chosen))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert {Path(python) for python, _ in _ran(c)} == {chosen}
