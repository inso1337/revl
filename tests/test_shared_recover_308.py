"""`shared` crash recovery: the `shared-reclaim-fence` WAL entry (item 308 S1,
issue #96).

The orderly `shared` teardown (last release runs the inverse in its own LIFO
bracket) and the liveness-gated crash reclaim over a LIVE process both live in
`revl.liveness_confirm` (the primitive, #631) and are pinned by
`test_liveness_confirm_308.py`. This module pins the OTHER crash shape the
design (docs/design/308-effect-ownership-modes.md, "Crash reclaim (the two
shapes that remain)") owed the shared slice: a WHOLE-PROCESS crash, where the
handle, every holder and the reclaimer die together, and `revl recover` reads
the durable ledger and re-fires the declared inverse EXACTLY ONCE.

The exactly-once discipline is the `shared-reclaim-fence`, the same
consume-before-fire fence `replay-fence`/`reissue-fence` use: fsync'd BEFORE the
single attempt, so a second recover run over the same WAL re-fires nothing and
reports 'fenced-before-attempt, outcome unknown' — no double-close. The outcome
is a `reclaim` residue record (basis `whole-process`), DISTINCT from a
`bracket-fault`.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from revl.recovery import recover, recover_shared_grants, DictWorld  # noqa: E402
from revl.wal import WAL_VERSION, read_wal  # noqa: E402

_INVERSE = {"receiver": "pool", "method": "close", "args": ["db#1"]}
_REFERENT = "pool:db#1"  # World.key(_INVERSE)


def _write_wal(path, records, *, complete=False):
    """Write a minimal JSONL WAL: a header, the given records, and optionally the
    terminal activation-complete marker."""
    lines = [{"record": "header", "walVersion": WAL_VERSION, "generation": 1,
              "guarantee": "x"}]
    lines.extend(records)
    if complete:
        lines.append({"record": "activation-complete", "components": ["C"]})
    with open(path, "w", encoding="utf-8") as handle:
        for line in lines:
            handle.write(json.dumps(line, sort_keys=True) + "\n")


def _grant(holders):
    return {"record": "shared-grant", "handle": "db",
            "inverse": _INVERSE, "holders": list(holders)}


# ---------------------------------------------------------------------------
# whole-process crash: re-fire the inverse exactly once
# ---------------------------------------------------------------------------


def test_whole_process_crash_refires_the_inverse_once(tmp_path):
    path = str(tmp_path / "shared.wal")
    _write_wal(path, [_grant(["A", "B"])])  # holders still counted, no completion
    world = DictWorld()
    world.seed(_REFERENT)  # the remote session persisted the crash

    report = recover(path, world=world)

    shared = report["shared"]
    assert len(shared["reclaims"]) == 1
    rec = shared["reclaims"][0]
    assert rec["record"] == "reclaim"
    assert rec["basis"] == "whole-process"
    assert rec["holders"] == 2
    assert rec["ok"] is True
    assert shared["clean"] is True
    # the inverse actually ran against the world (the referent was cleared)
    assert world.present(_REFERENT) is False


def test_reclaim_record_is_not_a_bracket_fault(tmp_path):
    path = str(tmp_path / "shared.wal")
    _write_wal(path, [_grant(["A"])])
    report = recover(path, world=DictWorld())
    rec = report["shared"]["reclaims"][0]
    # a reclaim ran OUTSIDE any activation, so it is its own residue kind, never
    # the in-frame `bracket-fault`.
    assert rec["record"] == "reclaim"
    assert rec["record"] != "bracket-fault"


# ---------------------------------------------------------------------------
# the fence: exactly once, even across a re-run
# ---------------------------------------------------------------------------


def test_shared_reclaim_fence_is_durably_appended(tmp_path):
    path = str(tmp_path / "shared.wal")
    _write_wal(path, [_grant(["A"])])
    recover(path, world=DictWorld())
    kinds = [r.get("record") for r in read_wal(path)["records"]]
    assert "shared-reclaim-fence" in kinds


def test_a_second_recover_run_does_not_double_close(tmp_path):
    path = str(tmp_path / "shared.wal")
    _write_wal(path, [_grant(["A"])])

    recover(path, world=DictWorld())  # first run: fires + fences

    # a fresh world for the re-run; if the fence did not hold, the inverse would
    # fire again and pop the (re-seeded) referent.
    world2 = DictWorld()
    world2.seed(_REFERENT)
    report2 = recover(path, world=world2)

    rec = report2["shared"]["reclaims"][0]
    assert rec["outcome"] == "unknown"
    assert rec["ok"] is False
    assert rec["error"]["type"] == "fenced-before-attempt"
    # the second run fired NOTHING — the referent is untouched
    assert world2.present(_REFERENT) is True


# ---------------------------------------------------------------------------
# the orderly path already ran the inverse — nothing owed
# ---------------------------------------------------------------------------


def test_a_completed_grant_owes_no_reclaim(tmp_path):
    path = str(tmp_path / "shared.wal")
    _write_wal(path, [_grant(["A"]),
                      {"record": "shared-complete", "handle": "db"}])
    world = DictWorld()
    world.seed(_REFERENT)
    report = recover(path, world=world)
    assert report["shared"]["reclaims"] == []
    assert report["shared"]["clean"] is True
    # the orderly last release ran the inverse; recover must not re-run it
    assert world.present(_REFERENT) is True


def test_a_grant_whose_count_reached_zero_owes_no_reclaim(tmp_path):
    path = str(tmp_path / "shared.wal")
    _write_wal(path, [_grant([])])  # every holder released
    report = recover(path, world=DictWorld())
    assert report["shared"]["reclaims"] == []


def test_a_zero_count_grant_fenced_but_not_completed_is_unknown_residue(tmp_path):
    # the #710 crash window at the reader: a zero crossing whose attempt was
    # durably fenced (holders == []) but whose `shared-complete` never landed —
    # the inverse raised, or the process died after the effect but before the
    # completion write. The empty count must NOT be read as a clean balance; the
    # unresolved attempt stays outcome-unknown residue, never re-fired.
    path = str(tmp_path / "shared.wal")
    _write_wal(path, [_grant([]),
                      {"record": "shared-reclaim-fence", "handle": "db"}])
    world = DictWorld()
    world.seed(_REFERENT)
    report = recover(path, world=world)
    rec = report["shared"]["reclaims"][0]
    assert rec["ok"] is False and rec["outcome"] == "unknown"
    assert report["shared"]["clean"] is False
    assert world.present(_REFERENT) is True     # not re-fired (fail-closed)


# ---------------------------------------------------------------------------
# no shared grant — byte-identical report
# ---------------------------------------------------------------------------


def test_a_wal_with_no_shared_grant_has_no_shared_block(tmp_path):
    path = str(tmp_path / "plain.wal")
    _write_wal(path, [], complete=True)
    report = recover(path, world=DictWorld())
    assert "shared" not in report
    # the direct helper returns None so the caller attaches nothing
    assert recover_shared_grants(read_wal(path), wal_path=path,
                                 world=DictWorld()) is None


# ---------------------------------------------------------------------------
# a failing reclaim inverse is honest residue (ok: false), still fenced once
# ---------------------------------------------------------------------------


def test_a_failing_reclaim_inverse_is_residue_not_a_silent_success(tmp_path):
    path = str(tmp_path / "shared.wal")
    _write_wal(path, [_grant(["A"])])

    class _BoomWorld(DictWorld):
        def apply_inverse(self, op):
            raise RuntimeError("remote close refused")

    report = recover(path, world=_BoomWorld())
    rec = report["shared"]["reclaims"][0]
    assert rec["ok"] is False
    assert rec["outcome"] == "err"
    assert "remote close refused" in rec["error"]["message"]
    assert report["shared"]["clean"] is False
    # still fenced: a failed reclaim does not re-fire on the next run
    kinds = [r.get("record") for r in read_wal(path)["records"]]
    assert "shared-reclaim-fence" in kinds
