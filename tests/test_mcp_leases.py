"""Component leases — the composition as a multi-agent workspace (item 61).

A lease is an operator-scoped (item 55), TTL-bound claim on a component *name*
that governs who may *replace* it — never a lock on the running component,
which keeps serving. It is advisory at plan/swap by default and refused at
admission under a boundary policy (item 33) that declares `leases enforced`.

Like the operator gate, almost all of this is a pure decision over the lease
book, `session.ir` and the bound operator/policy — no cordis runtime needed.
Only the end-to-end "an authorized swap actually lands and the trace names the
lease" test boots a runtime, and is skipped where it is not installed.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402
from revl.mcp import leases as L  # noqa: E402
from revl.mcp import server  # noqa: E402
from revl.mcp.leases import LeaseBook, LeaseError  # noqa: E402
from revl.mcp.operator import Operator  # noqa: E402
from revl.policy import parse_policy  # noqa: E402

needs_runtime = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="the live swap needs the cordis-py runtime — install it with "
           "`sh backends/python/setup.sh`",
)

TWO = """
service Cache { fn get(k: Str) -> Opt[Str]
                fn size() -> Int }
component UserCache provides uc: Cache {
  let s = effect Map.new() undo s.drop()
  provide uc { fn get(k) = s.get(k)
               fn size() = 0 }
}
component OtherCache provides oc: Cache {
  let s = effect Map.new() undo s.drop()
  provide oc { fn get(k) = s.get(k)
               fn size() = 0 }
}
"""


def _swap_user():
    return TWO.replace("provide uc { fn get(k) = s.get(k)\n               fn size() = 0 }",
                       "provide uc { fn get(k) = s.get(k)\n               fn size() = 42 }")


class _FakeSession:
    """Enough of a session for the lease decisions: `ir`, `operator`, `sandbox`
    (the item-33 policy), a real `LeaseBook`, and `loaded`."""

    def __init__(self, ir, operator=None, sandbox=None):
        self.ir = ir
        self.operator = operator
        self.sandbox = sandbox
        self.leases = LeaseBook()
        self.loaded = True


# ------------------------------------------------------- the book: claim/etc.


def test_claim_renew_release_roundtrip():
    book = LeaseBook()
    lease = book.claim("UserCache", "alice", ttl=100, now=1000.0)
    assert lease.holder == "alice" and lease.expiry == 1100.0
    assert book.holder_of("UserCache", now=1050.0) == "alice"
    # renew by the holder extends the expiry, keeps the original acquired time
    renewed = book.renew("UserCache", "alice", ttl=100, now=1050.0)
    assert renewed.expiry == 1150.0 and renewed.acquired == 1000.0
    # release drops it
    assert book.release("UserCache", "alice", now=1060.0) is True
    assert book.holder_of("UserCache", now=1060.0) is None
    # releasing an absent lease is a quiet no-op, not an error
    assert book.release("UserCache", "alice", now=1060.0) is False


def test_ttl_expiry_frees_the_name():
    book = LeaseBook()
    book.claim("UserCache", "alice", ttl=30, now=1000.0)
    assert book.holder_of("UserCache", now=1020.0) == "alice"  # still live
    # past the TTL the lease is gone and the name is free to re-claim
    assert book.holder_of("UserCache", now=1031.0) is None
    assert book.document(now=1031.0) == []
    # an expiry event was stamped for the trace
    assert any(e["action"] == "expired" and e["subject"] == "UserCache"
               for e in book.events)
    # a different operator may now claim the freed name
    book.claim("UserCache", "bob", ttl=30, now=1031.0)
    assert book.holder_of("UserCache", now=1040.0) == "bob"


def test_claim_refuses_a_name_another_operator_holds_live():
    book = LeaseBook()
    book.claim("UserCache", "alice", ttl=100, now=1000.0)
    with pytest.raises(LeaseError) as exc:
        book.claim("UserCache", "bob", ttl=100, now=1050.0)
    assert "already leased by `alice`" in str(exc.value)
    # a non-holder cannot renew or release it either
    with pytest.raises(LeaseError):
        book.renew("UserCache", "bob", now=1050.0)
    with pytest.raises(LeaseError):
        book.release("UserCache", "bob", now=1050.0)


def test_reclaiming_your_own_live_lease_is_a_renewal():
    book = LeaseBook()
    book.claim("UserCache", "alice", ttl=100, now=1000.0)
    again = book.claim("UserCache", "alice", ttl=200, now=1010.0)
    assert again.expiry == 1210.0 and again.acquired == 1000.0
    assert [e["action"] for e in book.events] == ["claim", "renew"]


def test_renew_without_a_lease_is_refused():
    with pytest.raises(LeaseError):
        LeaseBook().renew("UserCache", "alice", now=1000.0)


# ------------------------------------------------------------- identity/state


def test_holder_identity_uses_the_operator_token_or_a_default():
    assert L.holder_identity(_FakeSession(None, Operator("alice"))) == "alice"
    assert L.holder_identity(_FakeSession(None, None)) == L.DEFAULT_HOLDER


def test_document_surfaces_holder_component_and_expiry():
    book = LeaseBook()
    book.claim("UserCache", "alice", ttl=90, now=1000.0)
    doc = book.document(now=1000.0)
    assert doc == [{"component": "UserCache", "holder": "alice",
                    "acquired": 1000.0, "expiry": 1090.0,
                    "expiresInSeconds": 90.0}]


# ---------------------------------------------------------------- advisory


def test_advise_warns_on_a_name_another_operator_leases():
    sess = _FakeSession(compile_source(TWO), Operator("alice"))
    sess.leases.claim("UserCache", "bob", ttl=600)
    warnings = L.advise(sess, ["UserCache"])
    assert len(warnings) == 1
    assert warnings[0]["component"] == "UserCache"
    assert warnings[0]["leasedBy"] == "bob"
    assert "race" in warnings[0]["message"]


def test_advise_is_silent_on_your_own_lease():
    sess = _FakeSession(compile_source(TWO), Operator("alice"))
    sess.leases.claim("UserCache", "alice", ttl=600)
    assert L.advise(sess, ["UserCache"]) == []


def test_advise_plan_derives_the_targets_from_the_candidate():
    # a swap that only touches UserCache warns only about UserCache's lease,
    # not OtherCache's — the advisory is scoped to what the swap replaces.
    sess = _FakeSession(compile_source(TWO), Operator("alice"))
    sess.leases.claim("UserCache", "bob", ttl=600)
    sess.leases.claim("OtherCache", "carol", ttl=600)
    warnings = L.advise_plan(sess, {"source": _swap_user()})
    assert [w["component"] for w in warnings] == ["UserCache"]


# ---------------------------------------------------------------- enforcement


ENFORCING = parse_policy("leases enforced")


def test_policy_parses_leases_enforced_in_dsl_and_json():
    assert parse_policy("leases enforced").leases_enforced is True
    assert parse_policy('{"leases": {"enforced": true}}').leases_enforced is True
    assert parse_policy("").leases_enforced is False
    # the flag alone makes a policy non-empty (it carries authority)
    assert parse_policy("leases enforced").is_empty() is False


def test_enforced_swap_of_another_operators_lease_is_refused():
    sess = _FakeSession(compile_source(TWO), Operator("alice"), sandbox=ENFORCING)
    sess.leases.claim("UserCache", "bob", ttl=600)
    refusal = L.check_swap(sess, {"source": _swap_user()})
    assert refusal is not None
    assert refusal.component == "UserCache" and refusal.heldBy == "bob"
    assert refusal.holder == "alice"
    # the why-trace names the operator and the leased component it may not touch
    assert refusal.why.path() == ["alice", "UserCache"]
    assert "may not replace" in refusal.message


def test_self_operator_swap_of_its_own_lease_is_allowed():
    sess = _FakeSession(compile_source(TWO), Operator("alice"), sandbox=ENFORCING)
    sess.leases.claim("UserCache", "alice", ttl=600)
    assert L.check_swap(sess, {"source": _swap_user()}) is None


def test_advisory_by_default_never_refuses_a_swap():
    # same collision as the enforced test, but with NO enforcing policy: the
    # swap is only advised (a warning), never refused.
    sess = _FakeSession(compile_source(TWO), Operator("alice"), sandbox=None)
    sess.leases.claim("UserCache", "bob", ttl=600)
    assert L.check_swap(sess, {"source": _swap_user()}) is None
    assert L.advise(sess, ["UserCache"])  # but it is warned


def test_enforced_swap_untouching_the_leased_name_is_allowed():
    # bob leases OtherCache; alice's swap only replaces UserCache -> no collision
    sess = _FakeSession(compile_source(TWO), Operator("alice"), sandbox=ENFORCING)
    sess.leases.claim("OtherCache", "bob", ttl=600)
    assert L.check_swap(sess, {"source": _swap_user()}) is None


# ----------------------------------------------------- server verb + payloads


def _call(tool, arguments):
    resp = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                          "params": {"name": tool, "arguments": arguments}})
    return resp["result"]["structuredContent"]


@pytest.fixture()
def _fresh_session():
    """A clean server SESSION for one test, restored afterwards."""
    prior_op = server.SESSION.operator
    prior_sb = server.SESSION.sandbox
    prior_leases = server.SESSION.leases
    server.SESSION.operator = None
    server.SESSION.sandbox = None
    server.SESSION.leases = LeaseBook()
    yield server.SESSION
    if server.SESSION.loaded:
        server.SESSION.unload()
    server.SESSION.operator = prior_op
    server.SESSION.sandbox = prior_sb
    server.SESSION.leases = prior_leases


def test_lease_verb_claims_and_state_shows_it(_fresh_session):
    claimed = _call("revl_lease", {"action": "claim", "component": "UserCache",
                                   "ttl": 600})
    assert claimed["ok"] is True and claimed["holder"] == L.DEFAULT_HOLDER
    assert claimed["leases"][0]["component"] == "UserCache"
    # revl_state surfaces the active lease even with nothing loaded
    state = _call("revl_state", {})
    assert any(l["component"] == "UserCache" for l in state["leases"])
    # a lease event was produced for the causal trace
    assert any(e["channel"] == "lease" and e["action"] == "claim"
               for e in claimed["leaseEvents"])


def test_lease_verb_release_and_unknown_action(_fresh_session):
    _call("revl_lease", {"action": "claim", "component": "UserCache"})
    released = _call("revl_lease", {"action": "release", "component": "UserCache"})
    assert released["ok"] is True and released["released"] is True
    assert released["leases"] == []
    bad = _call("revl_lease", {"action": "steal", "component": "UserCache"})
    assert bad["ok"] is False


def test_refused_by_lease_payload_leaves_the_system_untouched():
    sess = _FakeSession(compile_source(TWO), Operator("alice"), sandbox=ENFORCING)
    sess.leases.claim("UserCache", "bob", ttl=600)
    refusal = L.check_swap(sess, {"source": _swap_user()})
    payload = server._refused_by_lease(refusal)
    assert payload["ok"] is False and payload["swapped"] is False
    assert payload["authorized"] is False
    assert payload["lease"] == {"component": "UserCache", "heldBy": "bob",
                                "expiry": refusal.expiry, "operator": "alice"}
    assert payload["why"]["subject"] == "alice"
    assert "untouched" in payload["note"]


# ------------------------------------------------------- persistence (item 15)


def test_active_leases_survive_a_snapshot_and_stale_ones_do_not():
    from revl.mcp import persist

    src = _FakeSession(None)
    src.leases.claim("UserCache", "alice", ttl=1e9)   # far-future: still live
    src.leases.claim("OtherCache", "bob", ttl=1e-6)   # already expired
    meta_leases = src.leases.document()
    # only the live one is in the document a snapshot would carry
    assert [l["component"] for l in meta_leases] == ["UserCache"]

    dst = _FakeSession(None)
    persist._restore_leases(dst, meta_leases)
    assert dst.leases.holder_of("UserCache") == "alice"
    assert dst.leases.holder_of("OtherCache") is None


# --------------------------------------------- end-to-end through the server


@needs_runtime
def test_enforced_refusal_end_to_end_and_advisory_warning(_fresh_session):
    server.SESSION.sandbox = ENFORCING
    server.SESSION.operator = Operator("alice")
    loaded = _call("revl_load", {"source": TWO})
    assert loaded["ok"] is True
    # bob (a different operator) holds UserCache
    server.SESSION.leases.claim("UserCache", "bob", ttl=600)
    # a plan warns about the race but is still produced
    plan = _call("revl_plan", {"source": _swap_user()})
    assert any(w["component"] == "UserCache" for w in plan.get("leaseWarnings", []))
    # the swap itself is REFUSED under the enforcing policy, system untouched
    refused = _call("revl_swap", {"source": _swap_user()})
    assert refused["ok"] is False and refused["swapped"] is False
    assert refused["lease"]["heldBy"] == "bob"
    # alice may still swap the name she holds herself
    server.SESSION.leases.claim("UserCache", "alice", ttl=600)
    ok = _call("revl_swap", {"source": _swap_user()})
    assert ok["ok"] is True and ok["swapped"] is True
