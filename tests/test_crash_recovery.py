"""Crash recovery: the accumulator as a write-ahead log (roadmap item 47).

The paradigm's core structure — accumulated effects paired with their inverses —
IS a write-ahead log. `backends/python/replay.py`'s :class:`WriteAheadLog`
persists it; `src/revl/recovery.py` reads it back on restart and proves a way
back. These tests establish, in order:

* the WAL persists each committed effect (identity + boundary class + inverse
  DESCRIPTOR) as it commits, and a completed activation stamps a terminal
  marker whose presence/absence is the whole roll-forward/roll-back decision;
* the honest analysis is enforced: an inverse the recorder holds only as a
  *closure* is flagged unreconstructible, never claimed to have run; a boundary
  inverse with a reconstructible descriptor (a durable file to unlink) runs;
* a simulated `kill -9` mid-activation (write the WAL, drop all in-memory
  state, recover from the file alone) rolls back to a stated verdict + residue
  proof;
* the production path (`@needs_cordis`) builds the WAL from a *real* recorded
  activation and recovers from it.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
BACKEND = ROOT / "backends" / "python"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import replay  # noqa: E402
from revl import compile_source  # noqa: E402
from revl.recovery import DictWorld, recover, render  # noqa: E402


PG_CONFIG = {"PgDatabase": {"url": "postgres://recovery-test"}}
USER_CACHE = (ROOT / "examples" / "user_cache.rvl").read_text(encoding="utf-8")


# --------------------------------------------------------------- WAL primitives


def _closure_effect_timeline() -> replay.Timeline:
    """A timeline whose only inverse is an author closure — the honest
    unreconstructible case. Driving `record_yield` with a bare lambda produces a
    KIND_EFFECT whose inverse is a closure the recorder cannot re-issue."""
    tl = replay.Timeline("Svc")
    tl.record_yield(lambda: None, "activation")  # KIND_EFFECT, closure inverse
    return tl


def test_the_wal_records_a_committed_effects_identity_boundary_and_inverse(tmp_path):
    tl = _closure_effect_timeline()
    path = str(tmp_path / "gen.wal")
    with replay.WriteAheadLog(path, ir={}, generation=1) as wal:
        wal.append_timeline(tl)

    loaded = replay.WriteAheadLog.read(path)
    assert loaded["header"]["walVersion"] == replay.WAL_VERSION
    assert loaded["complete"] is False  # no activation-complete marker written
    [record] = [r for r in loaded["records"] if r["record"] == "effect"]
    assert record["kind"] == "effect"
    assert record["boundary"]["class"] == "in-process"
    # the load-bearing honesty: an author closure is NOT reconstructible
    assert record["inverse"]["reconstructible"] is False
    assert "closure over in-process memory" in record["inverse"]["reason"]


def test_the_wal_appends_each_step_as_it_commits(tmp_path):
    """Write-ahead: a step is on disk the moment it is recorded, before the WAL
    is committed or closed. Attach the log to a live timeline and watch it grow
    line by line."""
    path = tmp_path / "live.wal"
    wal = replay.WriteAheadLog(str(path), ir={}, generation=1).open()
    tl = replay.Timeline("Svc")
    tl.attach_wal(wal, {})

    tl.record_emission("db", "execute", ("INSERT",), "Database", ("<f>", 10))
    assert len([r for r in replay.WriteAheadLog.read(str(path))["records"]
                if r["record"] == "effect"]) == 1  # already durable, mid-run
    tl.record_yield(lambda: None, "activation")
    assert len([r for r in replay.WriteAheadLog.read(str(path))["records"]
                if r["record"] == "effect"]) == 2
    wal.close()


def _boundary_op(name: str) -> dict:
    return {"receiver": "fs", "method": "remove", "args": [name]}


def test_reopening_a_wal_resumes_the_seq_space_instead_of_resetting_it(tmp_path):
    """Item 325 regression. A --watch reload builds a FRESH `WriteAheadLog`
    over the existing file in append mode. The seq counter must RESUME from the
    log's maximum seq, never restart at 0 — a reset collides in seq-space with
    the records the prior generation already wrote, and recover keys discharge
    descriptors by seq. This drives the real reload path (`Recorder.open_wal`
    again on the same path) and asserts the post-reopen seqs strictly continue
    the pre-reopen sequence."""
    path = str(tmp_path / "session.wal")
    recorder = replay.Recorder({})

    wal1 = recorder.open_wal(path, generation=1)
    r0 = wal1.record_boundary("C", "a", resource="file:/a",
                              inverse_op=_boundary_op("/a"))
    r1 = wal1.record_boundary("C", "b", resource="file:/b",
                              inverse_op=_boundary_op("/b"))
    assert [r0["seq"], r1["seq"]] == [0, 1]

    # --watch reload: same path, a brand-new WriteAheadLog instance.
    wal2 = recorder.open_wal(path, generation=2)
    assert wal2 is not wal1  # genuinely a fresh instance, not the same object
    r2 = wal2.record_boundary("C", "c", resource="file:/c",
                              inverse_op=_boundary_op("/c"))
    r3 = wal2.record_boundary("C", "d", resource="file:/d",
                              inverse_op=_boundary_op("/d"))
    # the whole point: 2 and 3, NOT a reset to 0 and 1.
    assert [r2["seq"], r3["seq"]] == [2, 3]
    wal2.close()

    # the durable log carries one strictly increasing, collision-free seq space.
    seqs = [r["seq"] for r in replay.WriteAheadLog.read(path)["records"]
            if "seq" in r]
    assert seqs == [0, 1, 2, 3]
    assert len(seqs) == len(set(seqs))  # no duplicate seq across the reload


def test_a_fresh_empty_wal_still_starts_its_seq_space_at_zero(tmp_path):
    """The resume must not perturb the ordinary first open: a new (or empty)
    log begins at seq 0."""
    path = str(tmp_path / "new.wal")
    wal = replay.WriteAheadLog(path, ir={}, generation=1).open()
    first = wal.record_boundary("C", "a", resource="file:/a",
                                inverse_op=_boundary_op("/a"))
    assert first["seq"] == 0
    wal.close()


def test_recover_keys_discharge_across_a_watch_reload_by_distinct_seqs(tmp_path):
    """Item 325, recover-path regression. A transactional descriptor written
    before a --watch reload and one written after it must land at DISTINCT seqs,
    so a discharge record naming the post-reload seq skips exactly that
    (committed) transaction and leaves the pre-reload (aborted) one to roll
    back. With the seq-reset bug both descriptors share seq 0 and the discharge
    becomes ambiguous — recover would skip the aborted rollback or replay the
    committed one."""
    path = str(tmp_path / "session.wal")
    recorder = replay.Recorder({})

    wal1 = recorder.open_wal(path, generation=1)
    before = wal1.record_discharge_descriptor(
        "transactional", receiver="ledgerA", method="rollback", args=["A"])

    wal2 = recorder.open_wal(path, generation=2)  # --watch reload
    after = wal2.record_discharge_descriptor(
        "transactional", receiver="ledgerB", method="rollback", args=["B"])
    # only the post-reload transaction committed before the crash.
    wal2.record_discharge([after["seq"]])
    wal2.close()

    assert before["seq"] != after["seq"]  # the collision the bug caused

    report = recover(path, world=DictWorld())
    assert report["verdict"] == "rolled-back"
    rolled_back = {e["seq"] for e in report["transactionalRolledBack"]}
    retained = {e["seq"] for e in report["dischargedSkipped"] if e["retained"]}
    assert rolled_back == {before["seq"]}   # aborted A rolled back
    assert retained == {after["seq"]}       # committed B retained, not replayed


def test_an_emission_is_a_process_crossing_with_no_inverse():
    tl = replay.Timeline("Svc")
    step = tl.record_emission("db", "execute", ("INSERT",), "Database", ("<f>", 3))
    boundary = replay.boundary_of(step)
    assert boundary["class"] == "emission"
    assert boundary["referent"] == replay.REFERENT_PROCESS_CROSSING
    assert replay.inverse_descriptor(step)["reconstructible"] is False


def test_an_explicit_boundary_descriptor_is_reconstructible(tmp_path):
    path = str(tmp_path / "gen.wal")
    with replay.WriteAheadLog(path, ir={}, generation=1) as wal:
        record = wal.record_boundary(
            "Store", "open logfile", resource="File",
            inverse_op={"receiver": "fs", "method": "unlink",
                        "args": ["/var/db/PeerWall/gen1.tmp"]})
    assert record["boundary"]["class"] == "acquire"
    assert record["boundary"]["referent"] == replay.REFERENT_OUTLIVES  # a File persists
    assert record["inverse"]["reconstructible"] is True
    assert record["inverse"]["op"]["method"] == "unlink"


def test_a_socket_referent_dies_with_the_process_a_file_outlives_it(tmp_path):
    """The extern-classification distinction the WAL leans on: an acquired
    Socket dies with the process (undo is moot); an acquired File outlives it."""
    with replay.WriteAheadLog(str(tmp_path / "s.wal"), ir={}) as wal:
        sock = wal.record_boundary("Net", "dial", resource="Socket",
                                   inverse_op={"receiver": "net", "method": "close",
                                               "args": ["sock#1"]})
        file = wal.record_boundary("Log", "open", resource="File",
                                   inverse_op={"receiver": "fs", "method": "unlink",
                                               "args": ["/tmp/x"]})
    assert sock["boundary"]["referent"] == replay.REFERENT_IN_PROCESS
    assert file["boundary"]["referent"] == replay.REFERENT_OUTLIVES


# ------------------------------------------------------------- roll-back / verdict


def test_a_crash_mid_activation_rolls_back_and_clears_a_durable_referent(tmp_path):
    """The simulated `kill -9`: write a WAL with a durable acquire (a file that
    persists) and NO activation-complete marker, then recover from the file
    alone — every in-memory object is gone. Recovery reconstructs the file's
    inverse from its descriptor and runs it, ending clean."""
    path = str(tmp_path / "crash.wal")
    wal = replay.WriteAheadLog(path, ir={}, generation=7).open()
    wal.record_boundary("Store", "create scratch", resource="File",
                        inverse_op={"receiver": "fs", "method": "unlink",
                                    "args": ["/var/db/PeerWall/gen7.scratch"]})
    wal.close()  # <-- crash here: no commit_activation()

    del wal  # simulate the process dying: nothing survives but the WAL file

    report = recover(path)
    assert report["verdict"] == "rolled-back"
    assert [e["op"]["method"] for e in report["ran"]] == ["unlink"]
    assert report["residue"]["clean"] is True
    assert "no residue" in report["residue"]["proof"]
    assert "LIFO" in report["decision"]


def test_roll_back_reports_a_closure_only_inverse_as_honest_residue(tmp_path):
    """A boundary inverse the recorder holds only as a closure cannot be
    reconstructed. Recovery says so — it does not pretend a dead lambda ran, and
    the durable referent is reported as still out in the world."""
    path = str(tmp_path / "crash.wal")
    tl = replay.Timeline("Cache")
    tl.record_emission("db", "execute", ("INSERT INTO log",), "Database", ("<f>", 9))
    with replay.WriteAheadLog(path, ir={}, generation=1) as wal:
        wal.append_timeline(tl)  # a bare emission — one-way, no inverse

    report = recover(path)
    assert report["verdict"] == "rolled-back"
    assert report["ran"] == []
    assert len(report["unreconstructible"]) == 1
    assert report["residue"]["clean"] is False
    assert "RESIDUE" in report["residue"]["proof"]


def test_an_in_process_inverse_is_moot_not_residue(tmp_path):
    """After a crash, an inverse over in-process memory is a no-op — the memory
    is gone. Recovery classifies it as moot, distinct from residue."""
    path = str(tmp_path / "crash.wal")
    with replay.WriteAheadLog(path, ir={}, generation=1) as wal:
        wal.append_timeline(_closure_effect_timeline())

    report = recover(path)
    assert report["ran"] == []
    assert len(report["moot"]) == 1
    assert report["residue"]["clean"] is True  # nothing durable was orphaned


def test_a_mix_rolls_back_running_what_it_can_and_flagging_what_it_cannot(tmp_path):
    path = str(tmp_path / "crash.wal")
    tl = replay.Timeline("Cache")
    tl.record_yield(lambda: None, "activation")           # in-process -> moot
    tl.record_emission("bus", "send", ("hi",), "Bus", ("<f>", 4))  # bare -> residue
    wal = replay.WriteAheadLog(path, ir={}, generation=3).open()
    wal.append_timeline(tl)
    wal.record_boundary("Store", "scratch", resource="File",
                        inverse_op={"receiver": "fs", "method": "unlink",
                                    "args": ["/tmp/gen3"]})            # durable -> ran
    wal.close()

    report = recover(path)
    assert len(report["ran"]) == 1
    assert len(report["moot"]) == 1
    assert len(report["unreconstructible"]) == 1  # the bare emission
    assert report["residue"]["clean"] is False
    # rendering names all three lanes
    text = render(report)
    assert "ran" in text and "moot" in text and "RESIDUE" in text


# ------------------------------------------------------------- roll-forward


def test_a_completed_activation_rolls_forward(tmp_path):
    """When the WAL carries `activation-complete`, the crash happened after
    activation finished — the shape is durable (item 15), so recovery rolls
    forward with nothing to undo."""
    path = str(tmp_path / "done.wal")
    tl = _closure_effect_timeline()
    with replay.WriteAheadLog(path, ir={}, generation=1) as wal:
        wal.append_timeline(tl)
        wal.commit_activation(["Svc"])

    report = recover(path)
    assert report["verdict"] == "rolled-forward"
    assert report["components"] == ["Svc"]
    assert report["resumed"] is False  # no snapshot passed to resume from
    assert report["residue"]["clean"] is True


def test_a_torn_final_record_is_tolerated_not_crashed_on(tmp_path):
    """A real `kill -9` can leave a half-written final line. Recovery — whose
    whole job is that crash — reads what it can and marks the tear, rather than
    dying on the malformed JSON."""
    path = Path(tmp_path / "torn.wal")
    with replay.WriteAheadLog(str(path), ir={}, generation=1) as wal:
        wal.record_boundary("Store", "scratch", resource="File",
                            inverse_op={"receiver": "fs", "method": "unlink",
                                        "args": ["/tmp/x"]})
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"record": "effect", "seq": 9, "boun')  # torn mid-write

    loaded = replay.WriteAheadLog.read(str(path))
    assert loaded["torn"] is True
    report = recover(str(path))
    assert report["verdict"] == "rolled-back"
    assert report["torn"] is True


# --------------------------------------------------------------- production path

try:  # noqa: SIM105
    import cordis  # noqa: F401
    HAVE_CORDIS = True
except ModuleNotFoundError:  # pragma: no cover — depends on the interpreter
    HAVE_CORDIS = False

needs_cordis = pytest.mark.skipif(
    not HAVE_CORDIS,
    reason="needs the cordis-py runtime (run under backends/python/.venv/bin/python)")

from revl.mcp.session import Session  # noqa: E402


@pytest.fixture
def session():
    live = Session()
    yield live
    if live.loaded:
        live.unload()


@needs_cordis
def test_a_real_recorded_activation_persists_and_recovers(session, tmp_path):
    """The WAL over a real cordis activation: load UserCache, put a key (which
    emits `db.execute`), persist the real accumulator, drop the process, and
    recover. The real emission is a process-crossing with no inverse, so a crash
    rolls back and reports it honestly as still out in the world."""
    session.load(compile_source(USER_CACHE, "<recovery>.rvl"), PG_CONFIG, record=True)
    session.call("cache", "put", ["k", "v"])

    # persist the REAL recorded accumulator (real provision-by-identity, real
    # captured emission args) to a durable WAL, then simulate the crash.
    path = str(tmp_path / "real.wal")
    tl = session.recorder.timeline("UserCache")
    with replay.WriteAheadLog(path, ir=session.ir, generation=1) as wal:
        wal.append_timeline(tl)  # NO commit -> crashed mid-activation

    loaded = replay.WriteAheadLog.read(path)
    kinds = [r["boundary"]["class"] for r in loaded["records"]
             if r["record"] == "effect"]
    assert "emission" in kinds  # the real db.execute crossing was captured

    report = recover(path)
    assert report["verdict"] == "rolled-back"
    # the real emission has no inverse: honest residue, not a pretended undo
    assert any(e["kind"] == "emission" for e in report["unreconstructible"])


@needs_cordis
def test_roll_forward_resumes_the_persisted_generation(session, tmp_path):
    """Roll-forward composes with item 15: a completed activation's WAL, plus
    the snapshot, resumes the persisted generation through `persist.resume` —
    the same admission gate a live restore runs."""
    session.load(compile_source(USER_CACHE, "<recovery>.rvl"), PG_CONFIG,
                 record=True, origin={"source": USER_CACHE})
    snap = session.snapshot()
    session.unload()  # the process "died"; only the snapshot + WAL survive

    path = str(tmp_path / "done.wal")
    with replay.WriteAheadLog(path, ir=session.ir or {}, generation=1) as wal:
        wal.commit_activation(["PgDatabase", "UserCache"])

    fresh = Session()
    try:
        report = recover(path, session=fresh, snapshot=snap)
        assert report["verdict"] == "rolled-forward"
        assert report["resumed"] is True
        assert report["resume"]["resumedForCrashRecovery"] is True
        assert fresh.loaded
    finally:
        if fresh.loaded:
            fresh.unload()
