"""Reference-tier `shared` runtime minting: the durable ledger the crash reclaim
reads back (item 308 S1, issue #96).

`test_liveness_confirm_308.py` pins the in-memory primitive (`SharedGrantBook`:
counted holders, the orderly zero crossing returning the inverse, the
live-process liveness-gated reclaim). `test_shared_recover_308.py` pins the
crash reader (`recover_shared_grants`) over HAND-WRITTEN WAL records. This module
pins the piece between them that was owed to close #96: the runtime that MINTS
the counted grant as a program runs and JOURNALS the exact ledger the reader
consumes, so the whole `shared` teardown loop — mint, consume, release, orderly
last-release, crash reclaim, operator surface — is proven end to end on the py
reference tier.

The go/rust/java/wasm emitters producing the same ledger writes are sequenced
with item 294 (docs/design/308-effect-ownership-modes.md, "Honest scope");
the reference tier here proves the property the issue asks for.
"""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_files  # noqa: E402
from revl.liveness_confirm import DictProbe, SharedGrantBook  # noqa: E402
from revl.placement import resource_crossing_refusal  # noqa: E402
from revl.recovery import DictWorld, recover, render  # noqa: E402
from revl.shared_runtime import JournaledSharedGrantBook, ledger_count  # noqa: E402
from revl.wal import WAL_VERSION, read_wal  # noqa: E402

_INVERSE = {"receiver": "pool", "method": "close", "args": ["db#1"]}
_REFERENT = "pool:db#1"  # DictWorld.key(_INVERSE)
_INVERSE2 = {"receiver": "pool", "method": "close", "args": ["db#2"]}
_REFERENT2 = "pool:db#2"


class _CountingWorld(DictWorld):
    """A world that records every inverse referent it is asked to apply, so a
    test can prove an inverse fired EXACTLY the expected number of times across
    the live path and a later recover over the same durable ledger."""

    def __init__(self) -> None:
        super().__init__()
        self.fires: list[str] = []

    def apply_inverse(self, op) -> None:
        self.fires.append(self.key(op))
        super().apply_inverse(op)


def _new_wal(path):
    """A WAL with only a header — the runtime appends its own ledger records."""
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(
            {"record": "header", "walVersion": WAL_VERSION, "generation": 1,
             "guarantee": "x"}, sort_keys=True) + "\n")


def _kinds(path):
    return [r.get("record") for r in read_wal(path)["records"]]


# ---------------------------------------------------------------------------
# the orderly path: mint -> consume -> release -> the last release fires once
# ---------------------------------------------------------------------------


def test_orderly_last_release_fires_the_inverse_once_and_journals_complete(tmp_path):
    path = str(tmp_path / "s.wal")
    _new_wal(path)
    world = DictWorld()
    world.seed(_REFERENT)                       # the handle is live
    book = JournaledSharedGrantBook(path, world=world)

    book.mint("db", _INVERSE, "A")              # holder #1
    book.consume("db", "B")                     # a crossing into B's scope
    assert book.count("db") == 2

    assert book.release("db", "A") is False     # not the zero crossing
    assert world.present(_REFERENT) is True     # handle still open
    assert book.release("db", "B") is True      # the zero crossing, orderly

    # exactly one close, run in the last releaser's frame
    assert world.present(_REFERENT) is False
    assert book.fired("db") is True
    # and the ledger names the orderly completion, so a later recover owes nothing
    assert "shared-complete" in _kinds(path)


def test_orderly_completion_leaves_recover_owing_nothing(tmp_path):
    path = str(tmp_path / "s.wal")
    _new_wal(path)
    book = JournaledSharedGrantBook(path, world=DictWorld())
    book.mint("db", _INVERSE, "A")
    book.release("db", "A")                     # single holder releases: orderly

    # a fresh world proves recover does NOT re-run the orderly inverse
    world = DictWorld()
    world.seed(_REFERENT)
    report = recover(path, world=world)
    assert report["shared"]["reclaims"] == []
    assert world.present(_REFERENT) is True


def test_release_order_permuted_still_one_fire_by_the_last_releaser(tmp_path):
    # the acquiring frame releasing FIRST is the interesting permutation: the
    # inverse must run in whoever releases LAST, not in the owner.
    for order in (["A", "B", "C"], ["C", "A", "B"], ["B", "C", "A"]):
        path = str(tmp_path / f"s-{''.join(order)}.wal")
        _new_wal(path)
        world = DictWorld()
        world.seed(_REFERENT)
        book = JournaledSharedGrantBook(path, world=world)
        book.mint("db", _INVERSE, "A")
        book.consume("db", "B")
        book.consume("db", "C")
        fires = [book.release("db", h) for h in order]
        assert fires == [False, False, True]     # exactly the last release fires
        assert world.present(_REFERENT) is False


# ---------------------------------------------------------------------------
# the count is a durable LEDGER field, not an in-memory refcount
# ---------------------------------------------------------------------------


def test_every_consume_and_release_is_a_durable_ledger_write(tmp_path):
    path = str(tmp_path / "s.wal")
    _new_wal(path)
    book = JournaledSharedGrantBook(path, world=DictWorld())
    book.mint("db", _INVERSE, "A")              # write 1
    book.consume("db", "B")                     # write 2
    book.consume("db", "C")                     # write 3
    book.release("db", "B")                     # write 4
    # four ledger writes, each a `shared-grant` record
    assert _kinds(path).count("shared-grant") == 4


def test_count_reconstructed_from_the_ledger_equals_the_live_holder_set(tmp_path):
    path = str(tmp_path / "s.wal")
    _new_wal(path)
    book = JournaledSharedGrantBook(path, world=DictWorld())
    book.mint("db", _INVERSE, "A")
    book.consume("db", "B")
    book.consume("db", "C")
    book.release("db", "B")                     # live set is now {A, C}

    # a fresh reader reconstructing from the durable log alone (a simulated
    # mid-count restart) sees exactly the live holders — the consume-before-fire
    # discipline, pinned.
    from_ledger = ledger_count(read_wal(path)["records"], "db")
    assert set(from_ledger) == book.holders("db") == {"A", "C"}


# ---------------------------------------------------------------------------
# a whole-process crash of a REAL journal is reclaimed exactly once
# ---------------------------------------------------------------------------


def test_whole_process_crash_from_a_real_journal_reclaims_once(tmp_path):
    path = str(tmp_path / "s.wal")
    _new_wal(path)
    live = DictWorld()
    live.seed(_REFERENT)
    book = JournaledSharedGrantBook(path, world=live)
    book.mint("db", _INVERSE, "A")
    book.consume("db", "B")
    # ...crash: the process dies with both holders still counted, no release.

    # recover reads the durable ledger the runtime wrote (not a hand-built WAL)
    crash_world = DictWorld()
    crash_world.seed(_REFERENT)                 # the remote referent survived
    report = recover(path, world=crash_world)

    rec = report["shared"]["reclaims"][0]
    assert rec["record"] == "reclaim"
    assert rec["basis"] == "whole-process"
    assert rec["holders"] == 2                  # the over-reported count, R4-safe
    assert rec["ok"] is True
    assert crash_world.present(_REFERENT) is False   # re-fired exactly once
    # fenced, so a second recover re-fires nothing
    assert "shared-reclaim-fence" in _kinds(path)


# ---------------------------------------------------------------------------
# E-Stop mid-count: the latch names the basis; the count is over-reported
# ---------------------------------------------------------------------------


def test_estop_latch_makes_the_reclaim_basis_estop_stranded(tmp_path):
    path = str(tmp_path / "s.wal")
    _new_wal(path)
    book = JournaledSharedGrantBook(path, world=DictWorld())
    book.mint("db", _INVERSE, "A")
    book.consume("db", "B")
    # an operator halt leaves a latch beside the WAL (the same rendezvous
    # `revl recover --wal` reconciles against, item 443).
    Path(path + ".estop").write_text(
        json.dumps({"halted": True, "operator": "op"}), encoding="utf-8")

    world = DictWorld()
    world.seed(_REFERENT)
    report = recover(path, world=world)
    rec = report["shared"]["reclaims"][0]
    assert rec["basis"] == "estop-stranded"     # not whole-process
    assert rec["holders"] == 2                   # stranded, over-reported


# ---------------------------------------------------------------------------
# NO wall-clock inverse: a ttl lapse while the holder is ALIVE fires nothing
# ---------------------------------------------------------------------------


def test_ttl_lapse_while_the_holder_is_alive_does_not_fire_the_inverse():
    # the fail-DANGEROUS close S1 refuses to make: firing on wall-clock while a
    # slow-but-alive holder still holds. The handle stays open until release/death.
    fired = []
    book = SharedGrantBook(ttl=30.0)
    book.acquire("db", lambda: fired.append("db"), "slow", now=0.0)

    probe = DictProbe()                          # "slow" is NOT killed: still alive
    report = book.reclaim_crashed(probe=probe, now=1000.0)   # long past ttl

    assert fired == []                           # nothing fired on wall-clock
    assert book.count("db") == 1                 # the holder is still counted
    assert book.grant("db").fired is False       # the handle stays open
    assert report.clean is True


def test_a_confirmed_dead_holder_past_ttl_is_the_one_that_reclaims():
    # the other side of the gate: once the holder is confirmed GONE, the reclaim
    # fires — the liveness gate arms on ttl and fires only on confirmed death.
    fired = []
    book = SharedGrantBook(ttl=30.0)
    book.acquire("db", lambda: fired.append("db"), "dead", now=0.0)
    probe = DictProbe()
    probe.kill("dead")
    book.reclaim_crashed(probe=probe, now=1000.0)
    assert fired == ["db"]
    assert book.grant("db").fired is True


# ---------------------------------------------------------------------------
# the operator surface: a failed reclaim moves the recover verdict, end to end
# ---------------------------------------------------------------------------


def test_a_failed_shared_reclaim_moves_the_recover_residue_and_renders(tmp_path):
    path = str(tmp_path / "s.wal")
    _new_wal(path)
    book = JournaledSharedGrantBook(path, world=DictWorld())
    book.mint("db", _INVERSE, "A")               # crash with the holder counted

    class _BoomWorld(DictWorld):
        def apply_inverse(self, op):
            raise RuntimeError("remote close refused")

    report = recover(path, world=_BoomWorld())
    # the shared block is honest residue...
    assert report["shared"]["clean"] is False
    # ...and it MOVES the verdict's residue proof, so `revl recover` exits 1
    assert report["residue"]["clean"] is False
    assert "shared reclaim" in report["residue"]["proof"]
    # ...and the operator sees a RECLAIM! line naming the handle
    text = render(report)
    assert "RECLAIM!" in text and "db" in text


def test_a_clean_reclaim_renders_a_reclaim_row_without_moving_the_verdict(tmp_path):
    path = str(tmp_path / "s.wal")
    _new_wal(path)
    book = JournaledSharedGrantBook(path, world=DictWorld())
    book.mint("db", _INVERSE, "A")
    world = DictWorld()
    world.seed(_REFERENT)
    report = recover(path, world=world)
    assert report["shared"]["clean"] is True
    text = render(report)
    assert "reclaim" in text and "whole-process" in text


# ---------------------------------------------------------------------------
# a shared handle is resource-typed, so a cross-process seam refuses it
# ---------------------------------------------------------------------------


_SHARED_SEAM_APP = """
type Socket = { fd: Int }
extern pure fn close_sock(h: Socket) = @py { return None }
extern acquire fn open_sock() -> Socket undo close_sock(result) = @py { return {"fd": 1} }

service PoolSvc { async fn get() -> Socket }
service Ctl { async fn go() -> Str }
component Pool provides p: PoolSvc {
  let s = effect shared open_sock() undo close_sock(s)
  provide p { async fn get() = s }
}
"""


def test_a_shared_resource_handle_crossing_a_process_seam_is_refused(tmp_path):
    # `shared` is admitted at the acquire binding (parser/lower, PR #677) and the
    # handle is resource-typed, so the tier-agnostic seam gate refuses it crossing
    # a process boundary by copy — exactly as an owned handle is. Cross-process
    # `shared` is a distributed count, not a refcount (design, "Reconciling with
    # the seam"), so the refusal is correct and permanent.
    p = tmp_path / "app.rvl"
    p.write_text(_SHARED_SEAM_APP, encoding="utf-8")
    ir = compile_files([str(p)])

    requires = {"c": {"k": "PoolSvc"}}
    provides = {"c": {"ctl": "Ctl"}, "w": {"k": "PoolSvc"}}
    owner = {"k": "w", "ctl": "c"}
    backends = {"c": "py", "w": "py"}
    problem = resource_crossing_refusal(ir, requires, provides, owner, backends)
    assert problem is not None
    assert "PoolSvc" in problem and "Socket" in problem


# ---------------------------------------------------------------------------
# #710: an orderly last release records completion only AFTER the inverse
# confirms. A raising inverse (or a crash after the effect but before the
# completion write) must stay unknown residue, never a concealed clean 'done'.
# ---------------------------------------------------------------------------


def test_710_confirmed_orderly_release_fires_once_and_recover_owes_nothing(tmp_path):
    path = str(tmp_path / "s.wal")
    _new_wal(path)
    live = _CountingWorld()
    live.seed(_REFERENT)
    book = JournaledSharedGrantBook(path, world=live)
    book.mint("db", _INVERSE, "A")
    assert book.release("db", "A") is True         # zero crossing, confirmed
    assert live.fires == [_REFERENT]               # fired exactly once, live
    # completion is journaled AFTER the fence and the fire; recover owes nothing
    kinds = _kinds(path)
    assert "shared-reclaim-fence" in kinds and "shared-complete" in kinds
    fresh = _CountingWorld()
    fresh.seed(_REFERENT)
    report = recover(path, world=fresh)
    assert report["shared"]["reclaims"] == []
    assert fresh.fires == []                        # not executed a second time


def test_710_orderly_release_with_a_raising_inverse_is_unknown_not_concealed(tmp_path):
    path = str(tmp_path / "s.wal")
    _new_wal(path)

    class _Boom(DictWorld):
        # the inverse refuses BEFORE any effect (a crash boundary before the
        # effect is confirmed): completion must not be written.
        def apply_inverse(self, op):
            raise RuntimeError("orderly close refused")

    book = JournaledSharedGrantBook(path, world=_Boom())
    book.mint("db", _INVERSE, "A")
    with pytest.raises(RuntimeError):
        book.release("db", "A")                     # zero crossing, inverse raises
    kinds = _kinds(path)
    assert "shared-reclaim-fence" in kinds          # the attempt is fenced...
    assert "shared-complete" not in kinds           # ...but never marked done
    # recover reads the fence as outcome-unknown residue and re-fires NOTHING.
    fresh = _CountingWorld()
    fresh.seed(_REFERENT)
    report = recover(path, world=fresh)
    rec = report["shared"]["reclaims"][0]
    assert rec["ok"] is False and rec["outcome"] == "unknown"
    assert report["shared"]["clean"] is False
    assert fresh.fires == []                        # not blindly retried


def test_710_crash_after_the_effect_but_before_completion_stays_unknown(tmp_path):
    path = str(tmp_path / "s.wal")
    _new_wal(path)

    class _CrashAfterEffect(DictWorld):
        # the inverse's effect really lands, then the process dies before the
        # runtime can append `shared-complete` (the crash window the fence
        # guards). The durable ledger cannot prove the effect confirmed.
        def apply_inverse(self, op):
            super().apply_inverse(op)
            raise SystemExit("process died before the completion write")

    live = _CrashAfterEffect()
    live.seed(_REFERENT)
    book = JournaledSharedGrantBook(path, world=live)
    book.mint("db", _INVERSE, "A")
    with pytest.raises(SystemExit):
        book.release("db", "A")
    assert live.present(_REFERENT) is False         # the effect DID land, live
    kinds = _kinds(path)
    assert "shared-reclaim-fence" in kinds and "shared-complete" not in kinds
    # a fresh recover cannot know the effect confirmed, so it is unknown residue
    # and is NOT re-fired (fail-closed, no double-close).
    fresh = _CountingWorld()
    fresh.seed(_REFERENT)
    report = recover(path, world=fresh)
    assert report["shared"]["reclaims"][0]["outcome"] == "unknown"
    assert fresh.fires == []


# ---------------------------------------------------------------------------
# #709: a successful LIVE reclaim finalizes the durable ledger, so a later
# recover over the same journal does NOT invoke the inverse a second time.
# ---------------------------------------------------------------------------


def test_709_clean_live_reclaim_then_recover_does_not_fire_twice(tmp_path):
    path = str(tmp_path / "s.wal")
    _new_wal(path)
    live = _CountingWorld()
    live.seed(_REFERENT)
    book = JournaledSharedGrantBook(path, world=live)
    book.mint("db", _INVERSE, "A", now=0.0)
    probe = DictProbe()
    probe.kill("A")                                 # the sole holder is gone
    book.reclaim_crashed(probe=probe, now=1000.0)   # live reclaim fires once
    assert live.fires == [_REFERENT]
    assert live.present(_REFERENT) is False
    # the ledger now names the completion, so recover finds nothing owed.
    assert "shared-complete" in _kinds(path)
    crash = _CountingWorld()
    crash.seed(_REFERENT)                           # a fresh process, referent back
    report = recover(path, world=crash)
    assert report["shared"]["reclaims"] == []
    assert crash.fires == []                         # NOT fired a second time
    assert crash.present(_REFERENT) is True


def test_709_faulted_live_reclaim_retains_unknown_and_recover_does_not_replay(tmp_path):
    path = str(tmp_path / "s.wal")
    _new_wal(path)

    class _Boom(DictWorld):
        def apply_inverse(self, op):
            raise RuntimeError("remote close refused")

    book = JournaledSharedGrantBook(path, world=_Boom())
    book.mint("db", _INVERSE, "A", now=0.0)
    probe = DictProbe()
    probe.kill("A")
    report = book.reclaim_crashed(probe=probe, now=1000.0)
    assert report.clean is False                    # the primitive records residue
    kinds = _kinds(path)
    assert "shared-complete" not in kinds            # no false clean completion
    assert "shared-reclaim-fence" in kinds           # failed/unknown is retained
    # a later recover reads the fence as unknown and re-fires NOTHING.
    fresh = _CountingWorld()
    fresh.seed(_REFERENT)
    rep = recover(path, world=fresh)
    rec = rep["shared"]["reclaims"][0]
    assert rec["ok"] is False and rec["outcome"] == "unknown"
    assert fresh.fires == []
    assert fresh.present(_REFERENT) is True


def test_709_multihandle_reclaim_settles_each_handle_independently(tmp_path):
    # one owner holding two handles: the clean handle completes, the faulting
    # handle stays unknown — closing (or failing to close) one never settles the
    # other (the #671/#691 per-handle independence, end to end through recover).
    path = str(tmp_path / "s.wal")
    _new_wal(path)

    class _BoomTwo(DictWorld):
        def apply_inverse(self, op):
            if self.key(op) == _REFERENT2:
                raise RuntimeError("db#2 close refused")
            super().apply_inverse(op)

    world = _BoomTwo()
    world.seed(_REFERENT)
    world.seed(_REFERENT2)
    book = JournaledSharedGrantBook(path, world=world)
    book.mint("db1", _INVERSE, "H", now=0.0)
    book.mint("db2", _INVERSE2, "H", now=0.0)        # same owner, two handles
    probe = DictProbe()
    probe.kill("H")
    book.reclaim_crashed(probe=probe, now=1000.0)
    assert world.present(_REFERENT) is False          # db1 closed cleanly, live
    fresh = DictWorld()
    fresh.seed(_REFERENT)
    fresh.seed(_REFERENT2)
    rep = recover(path, world=fresh)
    by = {r["handle"]: r for r in rep["shared"]["reclaims"]}
    assert "db1" not in by                             # completed, owes nothing
    assert by["db2"]["outcome"] == "unknown"           # faulted, fenced-unknown
    assert fresh.present(_REFERENT) is True            # db1 not fired again
