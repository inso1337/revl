"""The suite never writes approval WALs into the real home directory (issue #1771).

`revl.wal.default_wal_dir()` resolves `$REVL_WAL_DIR` first, then a per-user
state directory under `HOME`. tests/conftest.py defaults `REVL_WAL_DIR` to a
directory owned by the test session, so a session that opens a WAL without a
path of its own writes there; the tests that assert the platform default
unset it and point `HOME` at their own `tmp_path`.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import wal as wal_core  # noqa: E402

REAL_HOME = Path(os.path.expanduser("~")).resolve()


def _under(path: str, root: Path) -> bool:
    try:
        Path(path).resolve().relative_to(root)
        return True
    except ValueError:
        return False


def test_the_default_wal_dir_under_the_suite_is_not_in_the_real_home():
    directory = wal_core.default_wal_dir()
    assert directory == os.environ["REVL_WAL_DIR"], directory
    assert not _under(directory, REAL_HOME / "Library"), directory
    assert not _under(directory, REAL_HOME / ".local"), directory


def test_a_session_wal_path_lands_in_the_suite_directory():
    path = wal_core.default_wal_path("sess-1771")
    assert Path(path).parent == Path(os.environ["REVL_WAL_DIR"]), path


def test_a_child_python_inherits_the_suite_directory():
    """Subprocess tests write WALs too; they must inherit the same default."""
    proc = subprocess.run(
        [sys.executable, "-c",
         "from revl import wal; print(wal.default_wal_dir())"],
        capture_output=True, text=True, timeout=120, check=False)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == os.environ["REVL_WAL_DIR"], proc.stdout


def test_the_platform_default_is_computed_under_a_scratch_home(tmp_path, monkeypatch):
    """The shape every default-path test uses: no override, `HOME` moved."""
    monkeypatch.delenv("REVL_WAL_DIR", raising=False)
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    directory = wal_core.default_wal_dir()
    assert _under(directory, tmp_path.resolve()), directory
