"""The pre-commit FULL fallback is bounded in wall-clock time (issue #2059).

A `FULL` selection — every unmapped path, `tests/fixtures/**`, a structural
change — used to run `pytest tests/` unsharded and unbounded: about an hour on a
loaded box, pinning cores and starving other lanes (one measured run drove
`uptime` to load 59). The selector is right to fail safe; the execution was the
problem.

The hook now gives that step a wall-clock ceiling (`REVL_HOOK_FULL_BUDGET`,
default 60s) and, when it is exceeded, kills the run and SKIPS with the
selector's `FULL <reason>` and the `--no-verify` instruction instead of blocking
the commit. A partial run is not a gate, and `make pre-merge`/CI still runs
everything.

This builds a throwaway repository whose selector answers `FULL` (or nothing at
all, the crash fallback) and whose pytest never returns, and times the commit:
it must finish inside the budget, report SKIPPED with the reason, and leave the
commit clean.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
import venv
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HOOKS = ROOT / "tools" / "hooks"

if sys.platform == "win32":  # the hook is a POSIX shell script
    pytest.skip("the pre-commit hook runs under sh", allow_module_level=True)

# The stub selector: always FULL, with a reason and the whole suite as the node
# list — what tests/fixtures/**-only diffs produce.
_SELECTOR = (
    "print('FULL 1')\n"
    "print('REASON tests/fixtures/** is a structural change')\n"
    "print('PYTEST tests/')\n"
)
# The stub pytest: `--help` must answer instantly (the hook probes it for
# pytest-timeout); any real run records that it started, sleeps well past the
# budget, and would record a finish the hook must never let it reach.
_PYTEST_MAIN = (
    "import os, sys, time\n"
    "if '--help' in sys.argv:\n"
    "    sys.exit(0)\n"
    "with open(os.environ['HOOK_LOG'], 'a') as f:\n"
    "    f.write('pytest-started\\n')\n"
    "time.sleep(30)\n"
    "with open(os.environ['HOOK_LOG'], 'a') as f:\n"
    "    f.write('pytest-finished\\n')\n"
)
_CONFORMANCE = (
    "import os\n"
    "with open(os.environ['HOOK_LOG'], 'a') as f:\n"
    "    f.write('conformance\\n')\n"
)

BUDGET = 2
SLEEP = 30


def _run(args, cwd, env=None, timeout=120):
    return subprocess.run(args, cwd=str(cwd), env=env, capture_output=True,
                          text=True, timeout=timeout)


def _git(cwd, *args, env):
    proc = _run(["git", *args], cwd, env)
    assert proc.returncode == 0, f"git {' '.join(args)}: {proc.stderr}"
    return proc.stdout.strip()


def _make_python_with_pytest(path: Path) -> Path:
    """A real venv whose `pytest` is the never-returning stub above."""
    venv.create(path, with_pip=False, symlinks=True)
    python = path / "bin" / "python"
    purelib = Path(_run([str(python), "-c",
                         "import sysconfig; print(sysconfig.get_paths()['purelib'])"],
                        path).stdout.strip())
    package = purelib / "pytest"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "__main__.py").write_text(_PYTEST_MAIN, encoding="utf-8")
    script = path / "bin" / "pytest"
    script.write_text(f"#!{python}\nimport runpy\n"
                      "runpy.run_module('pytest', run_name='__main__')\n",
                      encoding="utf-8")
    script.chmod(0o755)
    return python


@pytest.fixture
def checkout(tmp_path):
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("GIT_") and k not in ("VIRTUAL_ENV", "PYTHONPATH",
                                                     "REVL_HOOK_PYTHON")}
    env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1",
               HOOK_LOG=str(tmp_path / "hook.log"))
    repo = tmp_path / "repo"
    (repo / "tools" / "hooks").mkdir(parents=True)
    for hook in HOOKS.iterdir():
        target = repo / "tools" / "hooks" / hook.name
        shutil.copy2(hook, target)
        target.chmod(0o755)
    (repo / "tools" / "affected_tests.py").write_text(_SELECTOR, encoding="utf-8")
    (repo / "tools" / "conformance.py").write_text(_CONFORMANCE, encoding="utf-8")
    (repo / "src" / "revl").mkdir(parents=True)
    (repo / "src" / "revl" / "__init__.py").write_text("", encoding="utf-8")
    (repo / ".gitignore").write_text(".venv/\n", encoding="utf-8")
    _git(repo, "init", "-q", "-b", "main", env=env)
    _git(repo, "config", "user.email", "t@example.com", env=env)
    _git(repo, "config", "user.name", "t", env=env)
    _git(repo, "add", "-A", env=env)
    _git(repo, "commit", "-qm", "base", "--no-verify", env=env)
    _git(repo, "config", "core.hooksPath", "tools/hooks", env=env)
    python = _make_python_with_pytest(tmp_path / "venv")
    return {"env": env, "repo": repo, "python": python, "tmp": tmp_path,
            "log": tmp_path / "hook.log"}


@pytest.mark.parametrize("selector,expected", [
    (_SELECTOR, "selector: FULL tests/fixtures/** is a structural change"),
    # The selector crashed or printed nothing: the same whole-suite fail-safe,
    # and the same ceiling.
    ("", "selector: unavailable"),
])
def test_a_full_selection_is_bounded_and_skipped_with_the_reason(checkout, selector,
                                                                 expected):
    c = checkout
    (c["repo"] / "tools" / "affected_tests.py").write_text(selector, encoding="utf-8")
    (c["repo"] / "tests" / "fixtures").mkdir(parents=True)
    (c["repo"] / "tests" / "fixtures" / "probe.json").write_text("{}\n",
                                                                 encoding="utf-8")
    _git(c["repo"], "add", "-A", env=c["env"])
    env = dict(c["env"], REVL_HOOK_PYTHON=str(c["python"]),
               REVL_HOOK_FULL_BUDGET=str(BUDGET))
    start = time.monotonic()
    proc = _run(["git", "commit", "-qm", "fixtures-only change"], c["repo"], env,
                timeout=90)
    elapsed = time.monotonic() - start
    out = proc.stdout + proc.stderr

    assert proc.returncode == 0, out
    assert elapsed < SLEEP / 2, (
        f"the FULL step was not bounded: the commit took {elapsed:.1f}s, so the "
        f"hook waited out the {SLEEP}s stub instead of its {BUDGET}s budget")
    assert "SKIPPED (FULL selection exceeded the %ds budget)" % BUDGET in out, out
    assert expected in out, out
    assert "--no-verify" in out, out

    ran = c["log"].read_text(encoding="utf-8").splitlines() if c["log"].exists() else []
    assert "pytest-started" in ran, ran
    assert "pytest-finished" not in ran, (
        f"the never-returning pytest was allowed to finish: {ran}")
    assert "conformance" in ran, ran
