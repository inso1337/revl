"""A seq-less record must not settle a seq-less emission effect (issue #2231)."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "backends" / "python"))
import replay  # noqa: E402
from revl.recovery import recover  # noqa: E402
from revl.wal import seqs_of, settled_seqs  # noqa: E402


def _wal_with_seqless_tail(path):
    wal = replay.WriteAheadLog(path, ir={}, generation=1).open()
    wal.commit_activation(["Svc"])
    timeline = replay.Timeline("Svc")
    timeline.record_emission("bus", "send", ("x",), "Bus", ("<f>", 0))
    wal.append_timeline(timeline)
    wal.close()
    lines = [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines()]
    effect = next(r for r in lines if r.get("record") == "effect")
    effect.pop("seq", None)
    lines.append({"record": "emission-complete"})
    Path(path).write_text("".join(json.dumps(r) + "\n" for r in lines))


def test_settled_seqs_ignores_records_without_an_int_seq():
    records = [{"record": "emission-complete"},
               {"record": "operator-resolved", "seq": "3"},
               {"record": "emission-complete", "seq": 4}]
    assert settled_seqs(records) == {4}


def test_seqless_emission_complete_does_not_settle_a_seqless_effect(tmp_path):
    path = str(tmp_path / "seqless.wal")
    _wal_with_seqless_tail(path)
    report = recover(path)
    assert report["residue"]["clean"] is False
    crossing = report["residue"]["outstanding"][0]["crossing"]
    assert (crossing["key"], crossing["method"]) == ("bus", "send")


def test_bool_seq_does_not_settle_seq_one():
    assert settled_seqs([{"record": "emission-complete", "seq": True}]) == set()
    assert seqs_of([{"record": "flushed", "seq": True}], ("flushed",)) == set()


def test_seqless_flushed_does_not_flush_a_seqless_deferred_emission():
    from revl.recovery import _deferral_crosscheck
    records = [{"record": "deferred-emission",
                "call": {"receiver": "s", "method": "m", "args": []}},
               {"record": "commit-approved"},
               {"record": "flushed"}]
    out = _deferral_crosscheck(records)
    assert out["flushed"] == []
    assert len(out["owed"]) == 1
