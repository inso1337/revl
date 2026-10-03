"""The per-file descriptor budget in tests/conftest.py fails by name (issue #1720).

A test file that leaves descriptors open must error, naming itself and what it
left; one that closes what it opens, or keeps less than the budget, must not.
Each case runs in its own pytest subprocess with this repository's conftest
loaded as a plugin, so the check is exercised exactly as the suite runs it.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

import _fd_budget  # noqa: E402

pytestmark = pytest.mark.skipif(not _fd_budget.available(),
                                reason="descriptors cannot be listed here")

LEAKY = '''
import socket
KEEP = []

def test_one():
    KEEP.extend(socket.socketpair())

def test_two():
    KEEP.extend(socket.socketpair())

def test_three():
    KEEP.extend(socket.socketpair())
'''

TIDY = '''
import socket

def test_one():
    a, b = socket.socketpair()
    a.close()
    b.close()
'''

SMALL = '''
import socket
KEEP = []

def test_one():
    KEEP.extend(socket.socketpair())   # two: a module-lifetime resource
'''


def _run(tmp_path: Path, body: str) -> subprocess.CompletedProcess:
    test = tmp_path / "test_case.py"
    test.write_text(body, encoding="utf-8")
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(
        [str(ROOT / "tests"), str(ROOT / "src"), os.environ.get("PYTHONPATH", "")]))
    return subprocess.run(
        [sys.executable, "-m", "pytest", str(test), "-q", "-p", "no:cacheprovider",
         "-p", "conftest", "--rootdir", str(tmp_path)],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=300,
        check=False)


def test_a_file_that_leaks_descriptors_errors_by_name(tmp_path):
    proc = _run(tmp_path, LEAKY)
    out = proc.stdout + proc.stderr
    assert proc.returncode != 0, out
    assert "test_case.py left 6 file descriptors open" in out, out
    assert "socket x6" in out and "issue #1720" in out, out
    assert "3 passed, 1 error" in out, out


def test_a_file_that_closes_what_it_opens_passes(tmp_path):
    proc = _run(tmp_path, TIDY)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_a_module_lifetime_resource_within_budget_passes(tmp_path):
    proc = _run(tmp_path, SMALL)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_an_allowance_names_its_reason():
    for name, (count, why) in _fd_budget.ALLOWANCES.items():
        assert count > _fd_budget.BUDGET and why.strip(), name
