"""Issue #2857: a content-keyed cache for selected pytest files.

A selected test file is cacheable only when it declares the inputs it
observes::

    CACHE_INPUTS = ("tests/fixtures/input.json",)

The cache key is the test file's content hash plus those declared input hashes
plus the runner context. A green entry is reused; a failure, an unknown
declaration, a missing input or an input that cannot be hashed is executed
every time. These tests exercise the user-facing entry point in a throwaway
tree, so the cache decision is measured by whether the tiny test wrote its
marker, not by a mock.

The end-to-end cases are the acceptance cases: first run executes and writes,
second unchanged run executes nothing and says so, a changed test file reruns
exactly that entry, a changed declared input reruns exactly that entry, a
failure is never cached as green, and an unhashable input reruns. Five more
cases pin the conservative edges review found: a PYTEST_* control must not
produce a green entry for a file whose other tests fail, a nested redeclaration
of CACHE_INPUTS is unknown, a pytest config above --root makes the target
uncacheable, and both a pytest.toml at the root and a config between the root
and the test are part of the key.
"""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "result_cache.py"

# This module observes the tool it drives, so a change to the tool invalidates
# its own cached result as well.
CACHE_INPUTS = ("tools/result_cache.py",)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text).lstrip(), encoding="utf-8")


def _test_source(marker: str, *, inputs: tuple[str, ...] | None = ()) -> str:
    """A tiny pytest file that records each execution in `marker`.

    `inputs=None` writes no declaration at all: the cache must treat the test
    as having unknown inputs and rerun it. An empty tuple is the explicit
    declaration that it observes nothing beyond its own source.
    """
    declaration = "" if inputs is None else f"CACHE_INPUTS = {inputs!r}\n"
    return f'''\
from pathlib import Path
import os

{declaration}
def test_runs():
    marker = Path(__file__).with_name({marker!r})
    with marker.open("a", encoding="utf-8") as handle:
        handle.write("run\\n")
    assert os.environ.get("REVL_TEST_CACHE_FAIL") != "1"
'''


def _mixed_source(marker: str) -> str:
    """One passing and one failing test, both recording an execution."""
    return f'''\
from pathlib import Path

CACHE_INPUTS = ()


def _record():
    marker = Path(__file__).with_name({marker!r})
    with marker.open("a", encoding="utf-8") as handle:
        handle.write("run\\n")


def test_ok():
    _record()
    assert True


def test_bad():
    _record()
    assert False
'''


def _nested_source(marker: str) -> str:
    """A top-level literal plus a nested reassignment the parser must reject."""
    return f'''\
from pathlib import Path

CACHE_INPUTS = ()


def test_runs():
    marker = Path(__file__).with_name({marker!r})
    with marker.open("a", encoding="utf-8") as handle:
        handle.write("run\\n")
    if False:
        CACHE_INPUTS = ("data/missing.txt",)
    assert True
'''


def _run(root: Path, cache: Path, selection: list[str],
         env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    run_env = os.environ.copy()
    run_env.pop("REVL_TEST_CACHE_FAIL", None)
    if env:
        run_env.update(env)
    return subprocess.run(
        [sys.executable, str(TOOL), "--root", str(root),
         "--cache-dir", str(cache), "--selection", *selection],
        cwd=root, env=run_env, text=True, capture_output=True, check=False,
    )


def _runs(path: Path) -> int:
    if not path.is_file():
        return 0
    return len(path.read_text(encoding="utf-8").splitlines())


def _assert_ok(proc: subprocess.CompletedProcess) -> None:
    assert proc.returncode == 0, proc.stdout + proc.stderr


def _entries(cache: Path) -> list[Path]:
    return sorted(cache.glob("*.json"))


def test_first_run_writes_entries_and_second_unchanged_run_executes_nothing(
        tmp_path):
    tests = tmp_path / "tests"
    _write(tests / "test_a.py", _test_source("a.runs"))
    cache = tmp_path / "cache"

    first = _run(tmp_path, cache, ["tests/test_a.py"])
    _assert_ok(first)
    assert "executed=1" in first.stdout
    assert "reused=0" in first.stdout
    assert "wrote=1" in first.stdout
    assert _runs(tests / "a.runs") == 1
    assert len(_entries(cache)) == 1

    # A selection file is the other accepted input shape, and the selector's
    # machine output is one of the files it can read.
    selection = tmp_path / "selection.txt"
    selection.write_text(
        "FULL 0\nREASON example\nPYTEST tests/test_a.py\n", encoding="utf-8")
    second = _run(tmp_path, cache, [str(selection)])
    _assert_ok(second)
    assert "reused=1" in second.stdout
    assert "executed=0" in second.stdout
    assert "executed nothing" in second.stdout
    assert _runs(tests / "a.runs") == 1


def test_a_changed_test_file_reruns_exactly_that_entry(tmp_path):
    tests = tmp_path / "tests"
    _write(tests / "test_a.py", _test_source("a.runs"))
    _write(tests / "test_b.py", _test_source("b.runs"))
    cache = tmp_path / "cache"

    first = _run(tmp_path, cache, ["tests/test_a.py", "tests/test_b.py"])
    _assert_ok(first)
    assert "executed=2" in first.stdout
    before = {path.name for path in _entries(cache)}
    assert len(before) == 2

    with (tests / "test_a.py").open("a", encoding="utf-8") as handle:
        handle.write("\n# a content change with the same behaviour\n")

    second = _run(tmp_path, cache, ["tests/test_a.py", "tests/test_b.py"])
    _assert_ok(second)
    assert "reused=1" in second.stdout
    assert "executed=1" in second.stdout
    assert _runs(tests / "a.runs") == 2
    assert _runs(tests / "b.runs") == 1
    assert {path.name for path in _entries(cache)} != before


def test_a_changed_declared_input_reruns_exactly_that_entry(tmp_path):
    tests = tmp_path / "tests"
    data = tmp_path / "data"
    _write(tests / "test_a.py",
           _test_source("a.runs", inputs=("data/a.txt",)))
    _write(tests / "test_b.py",
           _test_source("b.runs", inputs=("data/b.txt",)))
    data.mkdir()
    (data / "a.txt").write_text("one\n", encoding="utf-8")
    (data / "b.txt").write_text("one\n", encoding="utf-8")
    cache = tmp_path / "cache"

    first = _run(tmp_path, cache, ["tests/test_a.py", "tests/test_b.py"])
    _assert_ok(first)
    assert "executed=2" in first.stdout

    (data / "a.txt").write_text("two\n", encoding="utf-8")

    second = _run(tmp_path, cache, ["tests/test_a.py", "tests/test_b.py"])
    _assert_ok(second)
    assert "reused=1" in second.stdout
    assert "executed=1" in second.stdout
    assert _runs(tests / "a.runs") == 2
    assert _runs(tests / "b.runs") == 1


def test_a_failure_is_never_cached_as_green(tmp_path):
    tests = tmp_path / "tests"
    _write(tests / "test_a.py", _test_source("a.runs"))
    cache = tmp_path / "cache"

    failed = _run(tmp_path, cache, ["tests/test_a.py"],
                  env={"REVL_TEST_CACHE_FAIL": "1"})
    assert failed.returncode != 0, failed.stdout + failed.stderr
    assert _runs(tests / "a.runs") == 1
    assert _entries(cache) == []

    passed = _run(tmp_path, cache, ["tests/test_a.py"])
    _assert_ok(passed)
    assert _runs(tests / "a.runs") == 2
    assert len(_entries(cache)) == 1

    reused = _run(tmp_path, cache, ["tests/test_a.py"])
    _assert_ok(reused)
    assert "executed nothing" in reused.stdout
    assert _runs(tests / "a.runs") == 2


def test_an_unhashable_declared_input_always_reruns(tmp_path):
    tests = tmp_path / "tests"
    _write(tests / "test_a.py",
           _test_source("a.runs", inputs=("data/missing.txt",)))
    cache = tmp_path / "cache"

    first = _run(tmp_path, cache, ["tests/test_a.py"])
    _assert_ok(first)
    second = _run(tmp_path, cache, ["tests/test_a.py"])
    _assert_ok(second)

    assert _runs(tests / "a.runs") == 2
    assert _entries(cache) == []
    assert "uncacheable=1" in first.stdout
    assert "uncacheable=1" in second.stdout
    assert "executed=1" in second.stdout


def test_a_test_without_a_declaration_always_reruns(tmp_path):
    tests = tmp_path / "tests"
    _write(tests / "test_a.py", _test_source("a.runs", inputs=None))
    cache = tmp_path / "cache"

    first = _run(tmp_path, cache, ["tests/test_a.py"])
    _assert_ok(first)
    second = _run(tmp_path, cache, ["tests/test_a.py"])
    _assert_ok(second)

    assert _runs(tests / "a.runs") == 2
    assert _entries(cache) == []
    assert "uncacheable=1" in first.stdout
    assert "executed=1" in second.stdout


def test_pytest_control_environment_cannot_cache_a_deselected_failure(tmp_path):
    tests = tmp_path / "tests"
    _write(tests / "test_a.py", _mixed_source("a.runs"))
    cache = tmp_path / "cache"

    first = _run(tmp_path, cache, ["tests/test_a.py"],
                 env={"PYTEST_ADDOPTS": "-k test_ok"})
    assert first.returncode != 0, first.stdout + first.stderr
    assert _runs(tests / "a.runs") == 2
    assert _entries(cache) == []

    second = _run(tmp_path, cache, ["tests/test_a.py"])
    assert second.returncode != 0, second.stdout + second.stderr
    assert _runs(tests / "a.runs") == 4
    assert _entries(cache) == []


def test_a_nested_redeclaration_is_unknown_and_reruns(tmp_path):
    tests = tmp_path / "tests"
    _write(tests / "test_a.py", _nested_source("a.runs"))
    cache = tmp_path / "cache"

    first = _run(tmp_path, cache, ["tests/test_a.py"])
    _assert_ok(first)
    second = _run(tmp_path, cache, ["tests/test_a.py"])
    _assert_ok(second)

    assert _runs(tests / "a.runs") == 2
    assert _entries(cache) == []
    assert "uncacheable=1" in first.stdout
    assert "uncacheable=1" in second.stdout


def test_a_pytest_config_above_root_is_uncacheable(tmp_path):
    root = tmp_path / "root"
    tests = root / "tests"
    # A parent pyproject can still supply addopts when --rootdir is pinned:
    # pytest finds it as the inifile above the root. The tool must refuse to
    # cache rather than write a green entry under a deselecting option.
    _write(tmp_path / "pyproject.toml", """\
[tool.pytest.ini_options]
addopts = "-k test_ok"
""")
    _write(tests / "test_a.py", _mixed_source("a.runs"))
    cache = tmp_path / "cache"

    first = _run(root, cache, ["tests/test_a.py"])
    _assert_ok(first)
    second = _run(root, cache, ["tests/test_a.py"])
    _assert_ok(second)

    assert _runs(tests / "a.runs") == 2
    assert _entries(cache) == []
    assert "pytest config above root" in first.stdout
    assert "uncacheable=1" in second.stdout


def test_a_pytest_toml_config_is_part_of_the_key(tmp_path):
    tests = tmp_path / "tests"
    config = tmp_path / "pytest.toml"
    _write(config, """\
[pytest]
addopts = ["-k", "test_ok"]
""")
    _write(tests / "test_a.py", _mixed_source("a.runs"))
    cache = tmp_path / "cache"

    first = _run(tmp_path, cache, ["tests/test_a.py"])
    _assert_ok(first)
    assert "wrote=1" in first.stdout
    assert _runs(tests / "a.runs") == 1

    config.unlink()
    second = _run(tmp_path, cache, ["tests/test_a.py"])
    assert second.returncode != 0, second.stdout + second.stderr
    assert "reused=0" in second.stdout
    assert _runs(tests / "a.runs") == 3


def test_a_config_between_root_and_test_is_part_of_the_key(tmp_path):
    tests = tmp_path / "tests"
    config = tests / "pytest.ini"
    _write(config, """\
[pytest]
addopts = -k test_ok
""")
    _write(tests / "test_a.py", _mixed_source("a.runs"))
    cache = tmp_path / "cache"

    first = _run(tmp_path, cache, ["tests/test_a.py"])
    _assert_ok(first)
    assert "wrote=1" in first.stdout
    assert _runs(tests / "a.runs") == 1

    config.unlink()
    second = _run(tmp_path, cache, ["tests/test_a.py"])
    assert second.returncode != 0, second.stdout + second.stderr
    assert "reused=0" in second.stdout
    assert _runs(tests / "a.runs") == 3
