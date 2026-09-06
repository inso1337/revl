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


def test_the_step_kind_vocabulary_is_identical():
    """Item 250 Slice 2 mirrored the step-kind vocabulary into the core so the
    offline branch surface can classify a durable tail with no backend on the
    path. A kind added on one side and not the other would silently land in the
    other's catch-all bucket, so the two copies are pinned together."""
    assert wal_core.KINDS == replay.KINDS
    for name in ("KIND_EFFECT", "KIND_PROVISION", "KIND_EMISSION",
                 "KIND_COMPENSATION", "KIND_BOUNDARY", "KIND_HINGE",
                 "KIND_OPAQUE"):
        assert getattr(wal_core, name) == getattr(replay, name)


def test_the_fork_scope_gate_is_identical():
    """The scope gate decides whether a fork rewind may RUN an inverse (item 250,
    Decision 2). The live fork asks the backend's copy and the offline branch
    surface asks the core's; a drift would let one of them offer an outbound
    inverse as rewindable, which is the CRITICAL the design exists to close."""
    assert wal_core.HOST_CONFINED_CAPS == replay.HOST_CONFINED_CAPS
    for scope in (None, {}, {"caps": []}, {"caps": ["fs"]}, {"caps": ["net"]},
                  {"caps": ["fs", "net"]}, {"caps": ["unknown-token"]},
                  {"confined": True, "caps": ["net"]},
                  {"sandbox": True, "caps": ["net"]}, {"confined": False}):
        assert wal_core.scope_host_confined(scope) \
            == replay.scope_host_confined(scope), scope


def test_the_model_decision_record_kind_is_identical(tmp_path):
    """Item 250 Slice 3a: the py writer names the durable model-decision record
    and the core indexes it. A drift on the name would leave every decision on
    the WAL invisible to `revl compare` while the writer kept writing it."""
    assert wal_core.RECORD_MODEL_DECISION == replay.RECORD_MODEL_DECISION

    path = str(tmp_path / "decision.wal")
    wal = replay.WriteAheadLog(path, ir={}, generation=2).open()
    llm = {"model": "m", "tokensIn": 1, "tokensOut": 2, "latencySeconds": 0.5,
           "attempts": 1, "attemptCeiling": 3}
    written = wal.record_model_decision(component="Agent", step_index=4,
                                        llm=llm, outcome="validated")
    wal.close()
    assert written["record"] == wal_core.RECORD_MODEL_DECISION
    core = wal_core.read_wal(path)
    assert core == replay.WriteAheadLog.read(path)
    assert wal_core.model_decisions(core["records"]) == {("Agent", 4): written}


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
