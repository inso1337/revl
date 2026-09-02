"""WAL integrity hardening on the recovery read path (roadmap item 413).

The approval gate's write-ahead log used to be read as plain JSON Lines: the
reader never checked ``walVersion`` (despite the header docstring claiming it
did) and skipped an unparseable line ANYWHERE in the file, so a version-mismatch
or a MID-FILE corrupt line was read as valid and silently dropped records. A
dropped ``discharge`` makes recovery replay a committed transaction's rollback;
a dropped ``flushed`` makes it re-owe a fired emission. This is cleanup-integrity
defense-in-depth: authority injection was, and stays, structurally impossible
because approval and grant records are never read back on recovery.

These tests pin the two gates and confirm the sound properties survive:

* an unsupported ``walVersion`` is REFUSED, not read as current;
* a MID-FILE corrupt line fails closed (raises), never silently skipped;
* a crash-torn TRAILING line stays tolerated (the normal clean-shutdown-less
  case) and reports ``torn``;
* a normal write/read/recover cycle still works;
* the py backend's own reader copy behaves identically (no drift).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))
BACKEND = ROOT / "backends" / "python"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import replay  # noqa: E402
from revl import recovery, wal as wal_core  # noqa: E402


def _write_valid_wal(path: str) -> None:
    """A complete, well-formed WAL: header, a discharge descriptor + discharge,
    and the terminal activation-complete marker."""
    wal = replay.WriteAheadLog(path, ir={}, generation=1).open()
    wal.record_discharge_descriptor(
        "transactional", receiver="db", method="delete", args=["row#1"],
        origin={"key": "db", "method": "insert", "args": ["row#1"]},
        witness={"row": "row#1"})
    wal.record_discharge([0])
    wal.commit_activation(components=["Svc"])
    wal.close()


# --- gate 1: version --------------------------------------------------------

def test_unsupported_wal_version_is_refused(tmp_path):
    """A header naming a walVersion this reader does not support must raise, not
    be read as if it were the current format."""
    path = tmp_path / "future.wal"
    path.write_text(
        '{"record": "header", "walVersion": 999, "generation": 1}\n'
        '{"record": "discharge", "discharged": [0]}\n',
        encoding="utf-8")

    with pytest.raises(wal_core.WALIntegrityError) as excinfo:
        wal_core.read_wal(str(path))
    assert "999" in str(excinfo.value)

    # the py backend's own reader copy agrees (no drift)
    with pytest.raises(replay.WALIntegrityError):
        replay.WriteAheadLog.read(str(path))


def test_current_wal_version_is_accepted(tmp_path):
    """The current version reads without complaint."""
    path = str(tmp_path / "ok.wal")
    _write_valid_wal(path)
    loaded = wal_core.read_wal(path)
    assert loaded["header"]["walVersion"] == wal_core.WAL_VERSION
    assert loaded["complete"] is True
    assert loaded["torn"] is False


def test_missing_header_is_not_a_version_failure(tmp_path):
    """An empty or pre-header WAL is a valid nothing-to-recover input, not a
    version violation; the gate only fires when a header record exists."""
    empty = tmp_path / "empty.wal"
    empty.write_text("", encoding="utf-8")
    assert wal_core.read_wal(str(empty)) == {
        "header": {}, "records": [], "complete": False, "torn": False}


# --- gate 2: mid-file corruption vs torn trailing ---------------------------

def test_mid_file_corruption_fails_closed(tmp_path):
    """An unparseable line with valid records AFTER it is real corruption and
    must raise, never be silently skipped (which would drop a committed
    record and corrupt cleanup replay)."""
    path = tmp_path / "corrupt.wal"
    _write_valid_wal(str(path))
    good = path.read_text(encoding="utf-8").splitlines()
    # splice a torn line into the MIDDLE (after the header), good lines follow
    spliced = [good[0], '{"record": "discharge", "disc']  # truncated JSON
    spliced += good[1:]
    path.write_text("\n".join(spliced) + "\n", encoding="utf-8")

    with pytest.raises(wal_core.WALIntegrityError) as excinfo:
        wal_core.read_wal(str(path))
    assert "line 2" in str(excinfo.value)

    with pytest.raises(replay.WALIntegrityError):
        replay.WriteAheadLog.read(str(path))


def test_recover_fails_closed_on_mid_file_corruption(tmp_path):
    """The recovery entry point propagates the integrity failure rather than
    proceeding on a WAL that dropped a record."""
    path = tmp_path / "corrupt2.wal"
    _write_valid_wal(str(path))
    good = path.read_text(encoding="utf-8").splitlines()
    spliced = [good[0], "not json at all", good[1], good[2], good[3]]
    path.write_text("\n".join(spliced) + "\n", encoding="utf-8")

    with pytest.raises(wal_core.WALIntegrityError):
        recovery.recover(str(path))


def test_crash_torn_trailing_line_is_still_tolerated(tmp_path):
    """The normal clean-shutdown-less case: a write interrupted mid-line leaves
    a torn LAST line. It must be tolerated and reported as torn, never raised,
    and the good records before it must survive."""
    path = tmp_path / "torn.wal"
    wal = replay.WriteAheadLog(str(path), ir={}, generation=1).open()
    wal.record_discharge_descriptor(
        "transactional", receiver="db", method="delete", args=["a"],
        origin={"key": "db", "method": "insert", "args": ["a"]})
    wal.close()
    with open(path, "a", encoding="utf-8") as handle:
        handle.write('{"record": "discharge-descriptor", "seq": 1, "call": ')  # torn

    loaded = wal_core.read_wal(str(path))
    assert loaded["torn"] is True
    assert len(loaded["records"]) == 1
    # the two reader copies still agree exactly on the tolerated case
    assert loaded == replay.WriteAheadLog.read(str(path))


def test_torn_trailing_line_after_complete_marker(tmp_path):
    """A completed WAL with a torn line appended after the terminal marker: the
    torn line is the last content, so it is tolerated and complete stays true."""
    path = tmp_path / "complete_then_torn.wal"
    _write_valid_wal(str(path))
    with open(path, "a", encoding="utf-8") as handle:
        handle.write('{"record": "eff')  # torn trailing write

    loaded = wal_core.read_wal(str(path))
    assert loaded["complete"] is True
    assert loaded["torn"] is True


# --- sound-property regression guard ----------------------------------------

def test_normal_cycle_still_recovers(tmp_path):
    """A normal write then recover cycle is unaffected by the integrity gate: a
    completed WAL rolls forward as before."""
    path = str(tmp_path / "cycle.wal")
    _write_valid_wal(path)
    result = recovery.recover(path)
    assert result["verdict"] == "rolled-forward"


# --- forged records never yield a MORE-permissive cleanup -------------------

def test_forged_discharge_mid_file_fails_closed(tmp_path):
    """A forged/torn `discharge` spliced mid-file must fail closed, not be
    silently skipped. A dropped-or-mangled discharge would otherwise let recover
    replay a committed transaction's rollback (or skip a real inverse); refusing
    keeps a tampered WAL from ever producing a MORE-permissive cleanup."""
    path = tmp_path / "forged_discharge.wal"
    _write_valid_wal(str(path))
    good = path.read_text(encoding="utf-8").splitlines()
    # a truncated (unparseable) forged discharge, with real records after it
    spliced = [good[0], '{"record": "discharge", "discharged": [0'] + good[1:]
    path.write_text("\n".join(spliced) + "\n", encoding="utf-8")

    with pytest.raises(wal_core.WALIntegrityError):
        wal_core.read_wal(str(path))
    with pytest.raises(wal_core.WALIntegrityError):
        recovery.recover(str(path))


def test_truncation_removing_terminal_marker_rolls_back(tmp_path):
    """Truncation is the one tamper an unauthenticated log cannot detect, but it
    can only ever REMOVE trailing records, and the terminal `activation-complete`
    marker's ABSENCE is roll-back. So dropping it flips a completed WAL from
    rolled-forward to the CONSERVATIVE rolled-back, never the other way: tamper
    by truncation stays strictly less permissive, upholding the invariant."""
    path = tmp_path / "truncated.wal"
    _write_valid_wal(str(path))
    assert recovery.recover(str(path))["verdict"] == "rolled-forward"

    good = path.read_text(encoding="utf-8").splitlines()
    # drop the terminal activation-complete line (it is the last record written)
    assert '"activation-complete"' in good[-1]
    path.write_text("\n".join(good[:-1]) + "\n", encoding="utf-8")

    loaded = wal_core.read_wal(str(path))
    assert loaded["complete"] is False   # the forged/absent marker is not trusted
    assert recovery.recover(str(path))["verdict"] == "rolled-back"


# --- item 413.4: durable default WAL directory ------------------------------

def test_default_wal_path_is_durable_not_tempdir(tmp_path, monkeypatch):
    """The approval-WAL default lands under a durable per-user state directory,
    not the reboot-wiped tempdir it used to. With HOME pointed at a scratch dir
    (no XDG override), the path resolves under ~/.local/state, and the created
    directory is owner-only (0o700) so no other local account can splice it."""
    import os as _os

    monkeypatch.delenv("REVL_WAL_DIR", raising=False)
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr("sys.platform", "linux")

    # Durable means: resolved under the per-user XDG state home (relative to
    # HOME), not the old reboot-wiped tempfile fallback. The exact-path equality
    # is the real check; a naive "not under gettempdir()" assertion is unsound
    # because a test HOME under tmp_path is itself under the tempdir on CI.
    directory = wal_core.default_wal_dir()
    assert directory == str(tmp_path / ".local" / "state" / "revl" / "approval-wal")
    assert directory.startswith(str(tmp_path))
    assert _os.path.isdir(directory)
    assert (_os.stat(directory).st_mode & 0o777) == 0o700

    path = wal_core.default_wal_path("sess-1")
    assert path == _os.path.join(directory, "revl-approval-sess-1.wal")


def test_default_wal_dir_honours_explicit_override(tmp_path, monkeypatch):
    """An embedder that sets REVL_WAL_DIR pins the WAL there verbatim, so a host
    with its own durable location (or a test) can steer the gate's authority."""
    override = tmp_path / "custom-wal-home"
    monkeypatch.setenv("REVL_WAL_DIR", str(override))
    assert wal_core.default_wal_dir() == str(override)
    assert override.is_dir()


def test_default_wal_dir_prefers_xdg_state_home(tmp_path, monkeypatch):
    """XDG_STATE_HOME wins over the platform default when set (the standard
    per-user state location on an XDG host)."""
    monkeypatch.delenv("REVL_WAL_DIR", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    directory = wal_core.default_wal_dir()
    assert directory == str(tmp_path / "xdg" / "revl" / "approval-wal")


def test_session_default_wal_uses_durable_helper():
    """The MCP session's auto-opened approval WAL routes through the durable
    helper, not the old tempdir literal (item 413.4 wiring guard).

    `_configure_owner_approvals` used to inline the open; it now delegates to
    `_ensure_wal_open`, the session's single opener (load and the typed-approval
    path both call it), so the guard follows the opener rather than pinning the
    caller. Both are read, so wherever the call lives the tempdir literal stays
    out and the durable helper stays in."""
    import inspect

    from revl.mcp.session import Session
    src = (inspect.getsource(Session._configure_owner_approvals)
           + inspect.getsource(Session._ensure_wal_open))
    assert "default_wal_path" in src
    assert "gettempdir" not in src


@pytest.mark.skipif(
    __import__("importlib.util", fromlist=["util"]).find_spec("cordis") is None,
    reason="opening a session's approval WAL needs a live cordis-py composition")
def test_session_default_wal_lands_in_the_durable_dir(monkeypatch, tmp_path):
    """The behavioural half of the same guard, immune to a refactor: a policy
    session that names no `wal_path` opens its approval WAL UNDER the durable
    per-user state directory."""
    monkeypatch.setenv("REVL_WAL_DIR", str(tmp_path / "state"))
    from revl.compiler import compile_source
    from revl.mcp.session import Session
    source = (
        "extern emission fn announce(msg: Str) = @py { return }\n"
        "service Ops { emission fn shout(msg: Str) }\n"
        "component Agent provides ops: Ops {\n"
        "  provide ops { fn shout(msg) { emit announce(msg) } }\n"
        "}\n"
    )
    session = Session()
    session.approval_policy = "auto"
    session.load(compile_source(source, "wal-dir.rvl"), record=True)
    assert session.recorder.wal is not None
    assert session.recorder.wal.path.startswith(str(tmp_path / "state"))
