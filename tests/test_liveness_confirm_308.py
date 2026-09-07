"""Process-liveness confirmation primitive and the `shared` crash path
(roadmap item 308 S1; issue #96).

The primitive (`revl.liveness_confirm`) is the foundation `shared` teardown was
blocked on: a positive, probe-gated confirmation that a holder is alive, and —
on confirmed loss — an EXACTLY-ONCE reclaim that fires the declared inverse out
of frame and reports a `reclaim` residue record. It is neither of the two
existing surfaces: `revl.liveness` is static Petri-net deadlock analysis, and
`revl.mcp.leases.LeaseBook` is fail-safe (drops a grant, never fires anything).

These tests pin the discipline the design (S1) specifies:

  * a ttl lapse ARMS a reclaim but fires nothing (fail-safe);
  * an armed holder confirmed STILL ALIVE is pinned, not closed (a slow holder
    pins its handle — the correct bias for a fail-dangerous close);
  * an armed holder confirmed GONE fires its inverse exactly once, and a second
    sweep / re-run fires nothing (the fence);
  * for `shared`: peer alive -> handoff proceeds; peer lost -> the zero-crossing
    inverse fires once and the residue is clean (no leaked handle).
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.liveness_confirm import (  # noqa: E402
    DictProbe,
    LivenessError,
    LivenessProbe,
    LivenessRegistry,
    SharedGrantBook,
)


# ---------------------------------------------------------------------------
# The primitive: registration, heartbeat, confirmation
# ---------------------------------------------------------------------------

def test_registered_holder_is_confirmed_alive_within_ttl():
    reg = LivenessRegistry(ttl=10.0)
    reg.register("a", now=0.0)
    assert reg.confirmed_alive("a", now=5.0) is True
    # within the deadline no probe is even consulted
    assert reg.confirmed_alive("a", probe=DictProbe(), now=9.9) is True


def test_beat_pushes_the_deadline_out():
    reg = LivenessRegistry(ttl=10.0)
    reg.register("a", now=0.0)
    reg.beat("a", now=8.0)  # renews to 18.0
    probe = DictProbe()
    assert reg.confirmed_alive("a", probe=probe, now=15.0) is True


def test_unknown_holder_is_not_alive_and_cannot_beat():
    reg = LivenessRegistry(ttl=10.0)
    assert reg.confirmed_alive("ghost", now=0.0) is False
    with pytest.raises(LivenessError):
        reg.beat("ghost", now=0.0)


def test_past_ttl_but_probe_confirms_alive_is_still_alive():
    """Slow-but-alive: past the deadline, the probe's positive 'alive' wins."""
    reg = LivenessRegistry(ttl=10.0)
    reg.register("a", now=0.0)
    probe = DictProbe()  # a knows nothing => presumed alive
    assert reg.confirmed_alive("a", probe=probe, now=20.0) is True


def test_past_ttl_and_probe_confirms_gone_is_not_alive():
    reg = LivenessRegistry(ttl=10.0)
    reg.register("a", now=0.0)
    probe = DictProbe()
    probe.kill("a")
    assert reg.confirmed_alive("a", probe=probe, now=20.0) is False


def test_default_probe_is_fail_safe_presumes_alive():
    """An unconfigured probe pins the handle rather than closing it."""
    reg = LivenessRegistry(ttl=10.0)
    reg.register("a", now=0.0)
    assert reg.confirmed_alive("a", probe=LivenessProbe(), now=999.0) is True


# ---------------------------------------------------------------------------
# Arm / gate / fire — the reclaim discipline
# ---------------------------------------------------------------------------

def test_ttl_lapse_only_arms_never_fires():
    reg = LivenessRegistry(ttl=10.0)
    reg.register("a", now=0.0)
    closed = []
    # no probe confirmation of loss: reclaim must fire NOTHING even past ttl
    report = reg.reclaim({"a": lambda: closed.append("a")}, probe=DictProbe(),
                         now=100.0)
    assert closed == []            # fail-safe: armed, not closed
    assert report.fired == []
    assert report.pinned == ["a"]  # armed but confirmed alive => pinned
    assert report.clean is True


def test_confirmed_gone_fires_inverse_once_clean_residue():
    reg = LivenessRegistry(ttl=10.0)
    reg.register("a", now=0.0)
    probe = DictProbe()
    probe.kill("a")
    closed = []
    report = reg.reclaim({"a": lambda: closed.append("a")}, probe=probe,
                         now=100.0)
    assert closed == ["a"]
    assert report.fired == ["a"]
    assert report.clean is True
    residue = report.residue()
    assert residue["record"] == "reclaim"
    assert residue["clean"] is True
    assert residue["outstanding"] == []


def test_reclaim_is_fenced_second_sweep_fires_nothing():
    reg = LivenessRegistry(ttl=10.0)
    reg.register("a", now=0.0)
    probe = DictProbe()
    probe.kill("a")
    closed = []
    reg.reclaim({"a": lambda: closed.append("a")}, probe=probe, now=100.0)
    # a duplicate crash signal / a `revl recover` re-run over the same ledger
    report2 = reg.reclaim({"a": lambda: closed.append("a")}, probe=probe,
                          now=200.0)
    assert closed == ["a"]          # fired exactly once
    assert report2.fired == []
    assert report2.clean is True


def test_reclaimed_holder_cannot_beat_or_reregister():
    reg = LivenessRegistry(ttl=10.0)
    reg.register("a", now=0.0)
    probe = DictProbe()
    probe.kill("a")
    reg.reclaim({"a": lambda: None}, probe=probe, now=100.0)
    with pytest.raises(LivenessError):
        reg.beat("a", now=110.0)       # a late heartbeat cannot reopen the close
    with pytest.raises(LivenessError):
        reg.register("a", now=110.0)


def test_armed_holder_that_beats_disarms():
    reg = LivenessRegistry(ttl=10.0)
    reg.register("a", now=0.0)
    reg.arm_expired(now=20.0)          # past ttl, armed
    assert reg.holder("a").state == "armed"
    reg.beat("a", now=21.0)            # came back before confirmation
    assert reg.holder("a").state == "alive"
    # and now a reclaim sweep leaves it alone
    report = reg.reclaim({"a": lambda: None}, probe=DictProbe(), now=25.0)
    assert report.fired == [] and report.pinned == []


def test_reclaim_inverse_that_raises_is_reclaim_fault_not_bracket_fault():
    reg = LivenessRegistry(ttl=10.0)
    reg.register("a", now=0.0)
    probe = DictProbe()
    probe.kill("a")

    def boom():
        raise RuntimeError("close failed")

    report = reg.reclaim({"a": boom}, probe=probe, now=100.0)
    assert report.clean is False
    assert report.fired == []
    residue = report.residue()
    assert residue["outstanding"] == ["a"]
    assert residue["faults"][0]["kind"] == "reclaim-fault"
    # still fenced: does not re-fire on the next sweep
    again = reg.reclaim({"a": boom}, probe=probe, now=200.0)
    assert again.faults == []


def test_confirmed_gone_with_no_bound_inverse_is_unbound_residue():
    reg = LivenessRegistry(ttl=10.0)
    reg.register("a", now=0.0)
    probe = DictProbe()
    probe.kill("a")
    report = reg.reclaim({}, probe=probe, now=100.0)  # nothing bound
    assert report.clean is False
    assert report.residue()["faults"][0]["kind"] == "unbound-inverse"


def test_orderly_release_owes_no_reclaim():
    reg = LivenessRegistry(ttl=10.0)
    reg.register("a", now=0.0)
    reg.release("a", now=5.0)
    probe = DictProbe()
    probe.kill("a")  # even confirmed gone AFTER an orderly release
    report = reg.reclaim({"a": lambda: pytest.fail("must not fire")},
                         probe=probe, now=100.0)
    assert report.fired == [] and report.clean is True


# ---------------------------------------------------------------------------
# `shared` mode crash path (S1) on the primitive
# ---------------------------------------------------------------------------

def test_shared_peer_alive_handoff_proceeds():
    book = SharedGrantBook(ttl=10.0)
    closed = []
    book.acquire("db", lambda: closed.append("db"), "A", now=0.0)
    book.acquire("db", lambda: closed.append("db"), "B", now=0.0)
    # A may hand the shared handle to peer B while B is confirmed alive
    assert book.handoff_ok("db", "B", probe=DictProbe(), now=5.0) is True
    assert closed == []  # nothing torn down while holders live


def test_shared_peer_lost_handoff_refused():
    book = SharedGrantBook(ttl=10.0)
    book.acquire("db", lambda: None, "A", now=0.0)
    book.acquire("db", lambda: None, "B", now=0.0)
    probe = DictProbe()
    probe.kill("B")
    assert book.handoff_ok("db", "B", probe=probe, now=20.0) is False


def test_shared_orderly_last_release_returns_the_inverse():
    book = SharedGrantBook(ttl=10.0)
    inverse = lambda: None
    book.acquire("db", inverse, "A", now=0.0)
    book.acquire("db", inverse, "B", now=0.0)
    assert book.release("db", "A", now=1.0) is None       # not the last
    got = book.release("db", "B", now=2.0)                 # zero crossing
    assert got is inverse                                  # run in releaser LIFO
    assert book.grant("db").fired is True


def test_shared_crash_of_last_holder_fires_inverse_once_no_residue():
    """The headline S1 crash path: peer lost -> inverse fires, no residue."""
    book = SharedGrantBook(ttl=10.0)
    closed = []
    book.acquire("db", lambda: closed.append("db"), "A", now=0.0)
    book.acquire("db", lambda: closed.append("db"), "B", now=0.0)
    # A released orderly; B then crashed without releasing.
    book.release("db", "A", now=1.0)
    probe = DictProbe()
    probe.kill("B")
    report = book.reclaim_crashed(probe=probe, now=100.0)
    assert closed == ["db"]            # fired exactly once, out of frame
    assert report.fired == ["B"]
    assert report.clean is True        # no leaked handle
    assert report.residue()["record"] == "reclaim"
    assert book.grant("db").fired is True
    # a second sweep / recover re-run over the same ledger fires nothing
    again = book.reclaim_crashed(probe=probe, now=200.0)
    assert closed == ["db"] and again.fired == []


def test_shared_crash_with_surviving_holder_pins_the_handle():
    """A dead holder does not tear down a grant a live holder still owns."""
    book = SharedGrantBook(ttl=10.0)
    closed = []
    book.acquire("db", lambda: closed.append("db"), "A", now=0.0)
    book.acquire("db", lambda: closed.append("db"), "B", now=0.0)
    probe = DictProbe()
    probe.kill("A")            # A gone, B still alive (B keeps beating)
    book.beat("B", now=90.0)   # B is demonstrably alive
    book.reclaim_crashed(probe=probe, now=100.0)
    assert closed == []               # B still owns it => no teardown
    assert book.grant("db").fired is False
    # B still holds the handle and can hand it off / release later
    got = book.release("db", "B", now=110.0)
    assert got is not None            # now B is the zero crossing


def test_shared_all_holders_crash_fires_exactly_one_inverse():
    book = SharedGrantBook(ttl=10.0)
    closed = []
    book.acquire("db", lambda: closed.append("x"), "A", now=0.0)
    book.acquire("db", lambda: closed.append("x"), "B", now=0.0)
    book.acquire("db", lambda: closed.append("x"), "C", now=0.0)
    probe = DictProbe()
    for h in ("A", "B", "C"):
        probe.kill(h)
    report = book.reclaim_crashed(probe=probe, now=100.0)
    assert closed == ["x"]            # ONE inverse for the count, not per holder
    assert book.grant("db").fired is True
    assert report.clean is True


def test_shared_cannot_join_a_torn_down_grant():
    book = SharedGrantBook(ttl=10.0)
    book.acquire("db", lambda: None, "A", now=0.0)
    book.release("db", "A", now=1.0)          # zero crossing, grant fired
    with pytest.raises(LivenessError):
        book.acquire("db", lambda: None, "B", now=2.0)
