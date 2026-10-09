"""Durability and settlement regressions for plain Python emissions."""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "backends" / "python"))
import replay
from revl.wal import read_wal, unresolved_crossings, resolve_crossing


def test_restart_gate_refuses_before_loading_composition(tmp_path, capsys):
    from types import SimpleNamespace
    from revl.run import run_command
    path, wal, timeline = setup_crossing(tmp_path)
    step = timeline.record_emission("db", "send", (), None, ("test", 1))
    wal.close()
    args = SimpleNamespace(require_settled_wal=True, wal=str(path),
                           placement=None, backend="py")
    assert run_command(args) == 1
    assert str(step.wal_seq) in capsys.readouterr().err


@pytest.mark.asyncio
async def test_async_completion_waits_for_host_return(tmp_path):
    path, wal, timeline = setup_crossing(tmp_path)
    step = timeline.record_emission("db", "send", (), None, ("test", 1))
    deliveries = []
    async def host():
        deliveries.append(1)
        return 7
    pending = replay._call_within(replay._crossing_ref(timeline, step), host, (), {})
    assert unresolved_crossings(read_wal(str(path))["records"])
    assert await pending == 7
    assert deliveries == [1]
    assert unresolved_crossings(read_wal(str(path))["records"]) == []
    wal.close()


def test_write_failure_prevents_delivery(tmp_path):
    path, wal, timeline = setup_crossing(tmp_path)
    original = wal._handle
    class BrokenWriter:
        def write(self, record):
            raise OSError("write failed")
    wal._handle = BrokenWriter()
    deliveries = []
    try:
        with pytest.raises(OSError, match="write failed"):
            step = timeline.record_emission("db", "send", (), None, ("test", 1))
            replay._call_within(replay._crossing_ref(timeline, step), deliveries.append, (1,), {})
        assert deliveries == []
    finally:
        wal._handle = original
        wal.close()


def setup_crossing(tmp_path):
    path = tmp_path / "run.wal"
    wal = replay.WriteAheadLog(str(path), ir={}, generation=1).open()
    timeline = replay.Timeline("Svc")
    timeline.attach_wal(wal, {})
    return path, wal, timeline


def test_return_records_completion(tmp_path):
    path, wal, timeline = setup_crossing(tmp_path)
    step = timeline.record_emission("db", "send", (), None, ("test", 1))
    deliveries = []
    replay._call_within(replay._crossing_ref(timeline, step), deliveries.append, (1,), {})
    assert deliveries == [1]
    assert unresolved_crossings(read_wal(str(path))["records"]) == []
    wal.close()


def test_fsync_failure_prevents_delivery(tmp_path, monkeypatch):
    path, wal, timeline = setup_crossing(tmp_path)
    deliveries = []
    def fail(_):
        raise OSError("disk failure")
    monkeypatch.setattr(replay.os, "fsync", fail)
    with pytest.raises(replay.ReplayError):
        step = timeline.record_emission("db", "send", (), None, ("test", 1))
        replay._call_within(replay._crossing_ref(timeline, step), deliveries.append, (1,), {})
    assert deliveries == []
    wal.close()


def test_crash_after_return_stays_unresolved_until_operator_ack(tmp_path, monkeypatch):
    path, wal, timeline = setup_crossing(tmp_path)
    step = timeline.record_emission("db", "send", (), None, ("test", 1))
    deliveries = []
    def crash(*args, **kwargs):
        raise SystemExit("crash at completion write")
    monkeypatch.setattr(wal, "record_emission_outcome", crash)
    with pytest.raises(SystemExit):
        replay._call_within(replay._crossing_ref(timeline, step), deliveries.append, (1,), {})
    wal.close()
    assert deliveries == [1]
    assert unresolved_crossings(read_wal(str(path))["records"]) == [
        {"kind": "emission", "seq": step.wal_seq}]
    resolve_crossing(str(path), step.wal_seq)
    assert unresolved_crossings(read_wal(str(path))["records"]) == []
    assert deliveries == [1]


def test_failed_host_remains_unresolved(tmp_path):
    path, wal, timeline = setup_crossing(tmp_path)
    step = timeline.record_emission("db", "send", (), None, ("test", 1))
    def fail():
        raise ValueError("external outcome unknown")
    with pytest.raises(ValueError):
        replay._call_within(replay._crossing_ref(timeline, step), fail, (), {})
    assert unresolved_crossings(read_wal(str(path))["records"])
    wal.close()


def test_resolution_seals_a_crash_torn_tail(tmp_path):
    path, wal, timeline = setup_crossing(tmp_path)
    step = timeline.record_emission("db", "send", (), None, ("test", 1))
    wal.close()
    with path.open("ab") as handle:
        handle.write(b'{"record":"emission-complete"')
    assert read_wal(str(path))["torn"]
    resolve_crossing(str(path), step.wal_seq)
    loaded = read_wal(str(path))
    assert not loaded["torn"]
    assert unresolved_crossings(loaded["records"]) == []


def test_activation_completion_does_not_settle_emission_approval():
    records = [{"record": "approval-consumed", "requestId": "r", "use": 1, "seq": 2},
               {"record": "activation-complete"}]
    assert unresolved_crossings(records) == [
        {"kind": "approval-consumed", "seq": 2, "requestId": "r", "use": 1}]
    records[0]["scope"] = "activation"
    assert unresolved_crossings(records) == []


def test_later_generation_cannot_settle_failed_activation_approval():
    records = [{"record": "approval-consumed", "requestId": "r", "use": 1,
                "seq": 2, "scope": "activation"},
               {"record": "generation", "fromSeq": 3},
               {"record": "activation-complete"}]
    assert unresolved_crossings(records)


def test_legacy_approval_without_sequence_stays_unresolved():
    records = [{"record": "approval-consumed", "requestId": "r"},
               {"record": "emission-complete"}]
    assert unresolved_crossings(records)


def test_approved_deferred_emission_requires_flush_or_resolution():
    records = [{"record": "deferred-emission", "seq": 2},
               {"record": "commit-approved"}]
    assert unresolved_crossings(records) == [{"kind": "deferred-emission", "seq": 2}]
    records.append({"record": "operator-resolved", "seq": 2})
    assert unresolved_crossings(records) == []


def test_opt_out_is_explicitly_recorded(tmp_path, monkeypatch):
    monkeypatch.setattr(replay.os, "fsync", lambda _: (_ for _ in ()).throw(OSError("pipe")))
    path = tmp_path / "run.wal"
    with replay.WriteAheadLog(str(path), allow_unsynced=True):
        pass
    loaded = read_wal(str(path))
    assert loaded["header"]["durability"] == "unsynced-opt-out"
    assert any(r["record"] == "durability-opt-out" for r in loaded["records"])
