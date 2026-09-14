"""#1021: no test may hand the next one its import state.

Two order-dependent failures were found independently in one day, both of the
same shape -- a test mutates process-global import state, never restores it, and
a later test reads it as though its own file had run alone:

  * `tests/test_packaging.py` prepended the repository root to `sys.path` to
    reach `hatch_build.py`. That root is also the working directory
    `pytest tests/` runs from, so what stayed at `sys.path[0]` was a SECOND
    working-directory entry, and `drop_cwd_entry()` removes exactly one. Two
    tests in `tests/test_317_cwd_import_shadowing.py` -- the file that exists to
    prove cwd import shadowing is closed -- then failed, or passed, purely on
    collection order. A green run was not evidence the isolation held.

  * `_Driver._emit_module` registers a `revl_run_gen{N}` module per generation,
    numbered from a per-driver counter into the process-global `sys.modules`.
    A leftover from another file collides by NAME with a later session's own
    generations, so `tests/test_issue_541_admit_module_reclaim.py` counted one
    fewer than it emitted and failed in a multi-file session.

The fix is the autouse `_isolate_import_state` fixture in `tests/conftest.py`.
This file is its non-vacuity anchor. The subprocess sessions below are the only
way to prove it: the fixture's whole job is to make one test's state invisible to
the next, which cannot be observed from inside a single test.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_CONFTEST = _ROOT / "tests" / "conftest.py"

#: loads the REAL fixture under test rather than a copy of it, so this file
#: cannot drift away from what `pytest tests/` actually installs.
_BORROW_THE_REAL_FIXTURE = f"""\
import importlib.util

_spec = importlib.util.spec_from_file_location(
    "_revl_root_conftest", r"{_CONFTEST}")
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

# a fixture bound in a conftest namespace is a registered fixture
_isolate_import_state = _mod._isolate_import_state
"""

_LEAKS_SYS_PATH = """\
import os
import sys


def test_leaks_a_sys_path_entry():
    sys.path.insert(0, os.environ["REVL_1021_SENTINEL"])
"""

_READS_SYS_PATH = """\
import os
import sys


def test_sees_no_entry_from_the_previous_file():
    assert os.environ["REVL_1021_SENTINEL"] not in sys.path, (
        "a previous test left an entry on sys.path")
"""

_LEAKS_A_GENERATION_MODULE = """\
import sys
import types


def test_leaks_a_generation_module():
    sys.modules["revl_run_gen1"] = types.ModuleType("revl_run_gen1")
"""

_READS_GENERATION_MODULES = """\
import sys


def test_sees_no_generation_module_from_the_previous_file():
    assert [m for m in sys.modules if m.startswith("revl_run_gen")] == [], (
        "a previous test left a generation module registered")
"""


def _child_env() -> dict:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(_ROOT / "src")] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else []))
    return env


def _sentinel(root: Path) -> Path:
    """A path that is on `sys.path` ONLY if the leak crossed the boundary.

    Not the temporary root itself: that is the child's working directory and its
    rootdir, so `python -m pytest` and pytest's own prepend both put it there
    regardless, and a reader asserting on it would fail for the wrong reason.
    """
    sentinel = root / "sentinel_not_on_sys_path"
    sentinel.mkdir(exist_ok=True)
    return sentinel


def _write_suite(root: Path, leaker: str, reader: str) -> Path:
    """A two-file suite in collection order: the leaker, then the reader."""
    suite = root / "suite"
    suite.mkdir()
    (suite / "test_a_leaker.py").write_text(leaker, encoding="utf-8")
    (suite / "test_b_reader.py").write_text(reader, encoding="utf-8")
    return suite


def _run_suite(suite: Path, *, guarded: bool, sentinel: Path) -> subprocess.CompletedProcess:
    if guarded:
        (suite.parent / "conftest.py").write_text(
            _BORROW_THE_REAL_FIXTURE, encoding="utf-8")
    env = _child_env()
    env["REVL_1021_SENTINEL"] = str(sentinel)
    return subprocess.run(
        [sys.executable, "-m", "pytest", str(suite), "-q", "-p", "no:cacheprovider"],
        cwd=str(suite.parent), env=env, capture_output=True, text=True, timeout=300)


@pytest.mark.parametrize(
    "leaker,reader,what",
    [
        (_LEAKS_SYS_PATH, _READS_SYS_PATH, "sys.path"),
        (_LEAKS_A_GENERATION_MODULE, _READS_GENERATION_MODULES, "generation module"),
    ],
    ids=["sys_path", "generation_modules"],
)
def test_the_leak_really_reaches_the_next_file_without_the_fixture(
        tmp_path, leaker, reader, what):
    """The control. Without `_isolate_import_state` the reader file fails, which
    is what makes the guarded run below mean something."""
    suite = _write_suite(tmp_path, leaker, reader)
    proc = _run_suite(suite, guarded=False, sentinel=_sentinel(tmp_path))
    assert proc.returncode != 0, (
        f"the {what} leak did not reach the next file, so the guarded run below "
        f"proves nothing:\n{proc.stdout}\n{proc.stderr}")
    assert "test_b_reader" in proc.stdout


@pytest.mark.parametrize(
    "leaker,reader,what",
    [
        (_LEAKS_SYS_PATH, _READS_SYS_PATH, "sys.path"),
        (_LEAKS_A_GENERATION_MODULE, _READS_GENERATION_MODULES, "generation module"),
    ],
    ids=["sys_path", "generation_modules"],
)
def test_the_fixture_stops_the_leak_at_the_test_boundary(
        tmp_path, leaker, reader, what):
    """The same two files with `tests/conftest.py`'s fixture installed: the
    reader sees the state its file would see running alone."""
    suite = _write_suite(tmp_path, leaker, reader)
    proc = _run_suite(suite, guarded=True, sentinel=_sentinel(tmp_path))
    assert proc.returncode == 0, (
        f"a {what} leak crossed a test boundary with _isolate_import_state "
        f"installed:\n{proc.stdout}\n{proc.stderr}")


def test_the_cwd_shadowing_file_passes_in_the_order_that_broke_it():
    """The reported symptom, pinned end to end.

    `tests/test_packaging.py` before `tests/test_317_cwd_import_shadowing.py`,
    with `revl.__main__` already imported. That precondition is what made the
    defect intermittent: `test_packaging.py` itself does
    `from revl.__main__ import main`, and importing that module runs
    `drop_cwd_entry()`, which happened to scrub the leaked entry -- but only on
    the FIRST import in a process. In any session large enough for another file
    to have imported it already, the scrub never ran and the two
    `drop_cwd_entry` tests failed.
    """
    driver = (
        "import revl.__main__  # the accidental scrub has already happened\n"
        "import sys\n"
        "import pytest\n"
        "sys.exit(pytest.main([\n"
        "    'tests/test_packaging.py',\n"
        "    'tests/test_317_cwd_import_shadowing.py',\n"
        "    '-q', '-p', 'no:cacheprovider',\n"
        "]))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", driver],
        cwd=str(_ROOT), env=_child_env(), capture_output=True, text=True, timeout=600)
    assert proc.returncode == 0, (
        "test_317_cwd_import_shadowing is order-dependent again (#1021):\n"
        f"{proc.stdout}\n{proc.stderr}")
