"""The tier-agnostic WAL core (`revl.wal`, item 322) and the py in-process
writer (`backends/python/replay.py`) must agree byte-for-byte.

Item 322 factored the WAL READER and the schema constants out of the py backend
into :mod:`revl.wal` so `revl recover` reads any tier's WAL with no backend on
the path. The py backend keeps its own copies (its writer and its self-contained
tests depend on them). Two copies can drift; these tests pin them together so a
change to one that is not mirrored to the other fails here, not in production:

* the schema constants (``WAL_VERSION``, ``WAL_GUARANTEE``) are identical — they
  are written verbatim into every WAL header, so a drift would make a py-written
  header and a go-written header disagree on the guarantee text;
* the reader is behaviourally identical on a real py-written WAL — a WAL the py
  writer produced reads the same through ``revl.wal.read_wal`` as through
  ``replay.WriteAheadLog.read``, header, records, complete and torn.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
BACKEND = ROOT / "backends" / "python"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import replay  # noqa: E402
from revl import wal as wal_core  # noqa: E402


def test_the_schema_constants_are_identical():
    assert wal_core.WAL_VERSION == replay.WAL_VERSION
    assert wal_core.WAL_GUARANTEE == replay.WAL_GUARANTEE


def test_the_reader_matches_py_replay_read_on_a_real_wal(tmp_path):
    """Write a WAL through the py in-process writer, then read it back both ways
    and require the same {header, records, complete, torn}. Exercises the header
    line, a discharge-descriptor, a discharge record and the terminal marker."""
    path = str(tmp_path / "agree.wal")
    wal = replay.WriteAheadLog(path, ir={}, generation=5).open()
    wal.record_discharge_descriptor(
        "transactional", receiver="db", method="delete", args=["row#1"],
        origin={"key": "db", "method": "insert", "args": ["row#1"]},
        witness={"row": "row#1"})
    wal.record_discharge([0])
    wal.commit_activation(components=["Svc"])
    wal.close()

    assert wal_core.read_wal(path) == replay.WriteAheadLog.read(path)


def test_the_reader_tolerates_a_torn_final_record_like_py_replay(tmp_path):
    """A half-written trailing line (a real kill -9 can leave one): both readers
    must flag ``torn`` and keep the good records, never raise."""
    path = tmp_path / "torn.wal"
    wal = replay.WriteAheadLog(str(path), ir={}, generation=1).open()
    wal.record_discharge_descriptor(
        "transactional", receiver="db", method="delete", args=["a"],
        origin={"key": "db", "method": "insert", "args": ["a"]})
    wal.close()
    with open(path, "a", encoding="utf-8") as handle:
        handle.write('{"record": "discharge-descriptor", "seq": 1, "call": ')  # torn

    core = wal_core.read_wal(str(path))
    legacy = replay.WriteAheadLog.read(str(path))
    assert core == legacy
    assert core["torn"] is True
    assert len(core["records"]) == 1
