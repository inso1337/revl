"""WAL torn-tail data integrity (issue #535).

A durable WAL is written one fsync'd ``json + "\\n"`` at a time, so it always
ends in a newline. A crash mid-write (a real ``kill -9``) can leave a torn final
line with no trailing newline. Two coupled bugs followed from appending onto
such a file WITHOUT first closing that torn line off:

* R-C1 — ``recover`` appended its ``replay-fence`` straight onto the torn tail,
  merging the two into one unparseable line. The reader dropped it as torn, so a
  later recovery never saw the fence and RE-RAN the undeclared inverse: at most
  once became once-per-recovery.
* R-C2 — reopening a torn WAL in append mode (a ``--watch`` reload on the same
  ``--wal``) merged the torn tail with the NEXT record. Once records followed,
  that unparseable line was mid-file corruption, which the item 413 gate refuses
  forever.

The fix: every WAL appender seals the torn tail first — it truncates the
never-acknowledged partial trailing write back to the last clean newline
boundary, so nothing merges into it and a completed (newline-terminated) record
is never touched. These tests pin both bug fixes and the seal's own contract.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))
BACKEND = ROOT / "backends" / "python"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import pytest  # noqa: E402

import replay  # noqa: E402
from revl import _deploy_participant, recovery, wal as wal_core  # noqa: E402


def _header_line() -> str:
    return json.dumps(
        {"record": "header", "walVersion": 1, "generation": 1,
         "guarantee": wal_core.WAL_GUARANTEE}, sort_keys=True) + "\n"


def _append_torn(path, text='{"record": "eff'):
    """Append a torn partial record (no trailing newline) — the crash itself."""
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(text)


# --- the seal helper's own contract -----------------------------------------

def test_seal_leaves_a_clean_newline_terminated_wal_byte_identical(tmp_path):
    path = tmp_path / "clean.wal"
    path.write_text(_header_line() + '{"record": "effect", "seq": 0}\n',
                    encoding="utf-8")
    before = path.read_bytes()
    wal_core.seal_torn_tail(str(path))
    assert path.read_bytes() == before  # a clean boundary is never touched


def test_seal_removes_only_the_torn_tail_and_keeps_prior_records(tmp_path):
    path = tmp_path / "torn.wal"
    body = _header_line() + '{"record": "effect", "seq": 0}\n'
    path.write_text(body, encoding="utf-8")
    _append_torn(str(path))  # a torn partial with no newline

    wal_core.seal_torn_tail(str(path))

    # the torn partial is gone; every completed record before it survives intact
    assert path.read_text(encoding="utf-8") == body
    assert path.read_bytes().endswith(b"\n")
    loaded = wal_core.read_wal(str(path))
    assert loaded["torn"] is False
    assert len(loaded["records"]) == 1


def test_seal_of_a_single_unterminated_line_truncates_to_empty(tmp_path):
    path = tmp_path / "only-torn.wal"
    path.write_text('{"record": "hea', encoding="utf-8")  # never finished a line
    wal_core.seal_torn_tail(str(path))
    assert path.read_bytes() == b""


def test_seal_is_a_noop_on_a_missing_or_empty_file(tmp_path):
    missing = tmp_path / "nope.wal"
    wal_core.seal_torn_tail(str(missing))  # must not raise
    assert not missing.exists()
    empty = tmp_path / "empty.wal"
    empty.write_bytes(b"")
    wal_core.seal_torn_tail(str(empty))
    assert empty.read_bytes() == b""


def test_the_reader_and_backend_seal_copies_agree(tmp_path):
    """revl.wal, the py backend and the deploy participant each carry a standalone
    seal (the backend and participant run without importing revl). They must not
    drift — identical bytes out for the same torn input."""
    body = _header_line() + '{"record": "effect", "seq": 0}\n'
    torn = body + '{"record": "ef'
    results = []
    for seal in (wal_core.seal_torn_tail, replay._seal_torn_tail,
                 _deploy_participant._seal_torn_tail):
        p = tmp_path / f"drift-{len(results)}.wal"
        p.write_text(torn, encoding="utf-8")
        seal(str(p))
        results.append(p.read_bytes())
    assert results[0] == results[1] == results[2] == body.encode("utf-8")


# --- R-C2: reopen a torn WAL in append mode ---------------------------------

def test_reopen_torn_wal_appends_cleanly_without_mid_file_corruption(tmp_path):
    """A --watch reload reopens the same torn WAL in append mode; the next
    generation's records must land on a clean boundary and read back whole,
    never merge into the torn tail to make permanent mid-file corruption."""
    path = str(tmp_path / "reopen.wal")
    wal = replay.WriteAheadLog(path, ir={}, generation=1).open()
    wal.record_discharge_descriptor(
        "transactional", receiver="db", method="delete", args=["row#1"],
        origin={"key": "db", "method": "insert", "args": ["row#1"]})
    wal.close()
    _append_torn(path)  # crash left a torn trailing write

    # reopen and keep going, as a watch reload does
    wal2 = replay.WriteAheadLog(path, ir={}, generation=2).open()
    wal2.record_discharge_descriptor(
        "transactional", receiver="db", method="delete", args=["row#2"],
        origin={"key": "db", "method": "insert", "args": ["row#2"]})
    wal2.close()

    loaded = replay.WriteAheadLog.read(path)  # must NOT raise WALIntegrityError
    assert loaded["torn"] is False
    # the torn partial is gone, both real descriptors survive
    assert len(loaded["records"]) == 2
    # the two reader copies still agree exactly on the sealed-then-appended file
    assert wal_core.read_wal(path) == loaded


# --- R-C1: recover's fence survives a torn tail -----------------------------

def _witnessed_undeclared(path: str) -> None:
    """A witnessed transactional inverse with NO discharge and NO
    activation-complete (the crash), inverse left UNDECLARED so recover fences it
    after its single at-most-once attempt."""
    wal = replay.WriteAheadLog(path, ir={}, generation=1).open()
    wal.record_discharge_descriptor(
        "transactional", receiver="db", method="delete", args=["row#1"],
        origin={"key": "db", "method": "insert", "args": ["row#1"],
                "site": "svc.rvl:9"},
        witness={"row": "row#1"})
    wal.close()


def test_undeclared_inverse_is_at_most_once_even_over_a_torn_tail(tmp_path):
    """The R-C1 headline. With a torn trailing line present, the FIRST recover
    takes the single at-most-once attempt and its fence must survive on its own
    line; the SECOND recover must find that fence and REFUSE to re-apply. Before
    the fix the fence merged into the torn tail and was dropped, so the second
    run re-ran the undeclared inverse."""
    path = str(tmp_path / "undeclared-torn.wal")
    _witnessed_undeclared(path)
    _append_torn(path)  # a torn trailing write sits under recover's fence

    first = recovery.recover(path)
    [entry] = first["transactionalRolledBack"]
    assert entry["replay"] == "fenced"

    # the fence is durable on its OWN line (not swallowed by the torn tail)
    reread = replay.WriteAheadLog.read(path)
    assert reread["torn"] is False
    assert any(r.get("record") == "replay-fence" for r in reread["records"])

    # the second pass finds the fence and does NOT re-apply: at most once holds
    second = recovery.recover(path)
    assert second["transactionalRolledBack"] == []
    [fenced] = second["fencedDeferred"]
    assert fenced["referent"] == "db:row#1"


def test_recover_over_a_torn_tail_does_not_raise_mid_file_corruption(tmp_path):
    """Fail-safe framing: a torn-tail WAL that recover appends onto must never
    turn into the mid-file corruption the item 413 gate refuses forever."""
    path = str(tmp_path / "no-corruption.wal")
    _witnessed_undeclared(path)
    _append_torn(path)
    recovery.recover(path)
    recovery.recover(path)  # a second pass reads the sealed file with no raise
    loaded = wal_core.read_wal(path)
    assert loaded["torn"] is False
