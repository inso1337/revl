"""`revl_edit` runs the gates that swap runs — and a restored lease cannot
exempt the operator its own document names.

An edit that compiles clean is hot-swapped in, so an edit *is* a swap: the
same component replaced, the same running composition mutated, a different
verb on the wire. `docs/component-leases.md` says enforcement covers "every
path that swaps a component candidate, not just `revl_swap`", and
`docs/quarantine-tier.md` says a required quarantine gates admission to a
hosted tier. Both were wired only into `server._tool_swap` — the lease check
was also in `server._tool_repair`, the quarantine gate was not — so `revl_edit`
was a way around an enforced lease and around a required quarantine.
`_tool_repair` now carries both, so the two documents speak for it too; the
guards that would fail if that copy were deleted are in
`tests/test_mcp_authority_gate.py`:
`test_revl_repair_hands_the_quarantine_gate_the_swap_it_performs` and
`test_a_required_quarantine_refuses_the_repair_and_nothing_swaps`.

The restore half is the same shape of mistake one layer down: `_restore_leases`
re-seated the `holder` field of a *client-supplied* snapshot as authoritative,
and `check_swap` exempts a lease the acting operator holds — so a client could
forge the holder to itself and walk through the fence its own restore had just
put back. A document may carry a fence; it may not mint a claim.

Almost all of this is a pure decision over the lease book, the patched source
and the bound policy — no cordis runtime. Only the end-to-end parity test (the
same bytes sent as a document and as a delta) boots one.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402
from revl.mcp import edit as E  # noqa: E402
from revl.mcp import leases as L  # noqa: E402
from revl.mcp import persist  # noqa: E402
from revl.mcp import server  # noqa: E402
from revl.mcp.leases import LeaseBook  # noqa: E402
from revl.mcp.operator import Grant, Operator  # noqa: E402
from revl.policy import parse_policy  # noqa: E402

needs_runtime = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="the live swap needs the cordis-py runtime — install it with "
           "`sh backends/python/setup.sh`")

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

# The two-line snippet that is UserCache's provider — and nothing else. The
# swap below resends the whole file with it changed; the edit sends the same
# change as a delta against the server-side buffer, so the patched bytes are
# identical and the two verbs must reach the same verdict.
_PROVIDER = ("provide uc { fn get(k) = s.get(k)\n"
             "               fn size() = 0 }")
_PROVIDER_FIXED = ("provide uc { fn get(k) = s.get(k)\n"
                   "                   fn size() = 42 }")

ENFORCING = parse_policy("leases enforced")
QUARANTINE = parse_policy("quarantine required")


def _swap_user():
    return TWO.replace(_PROVIDER, _PROVIDER_FIXED)


def _edit_user():
    return {"edits": [{"anchor": _PROVIDER, "replacement": _PROVIDER_FIXED}]}


class _FakeSession:
    """Enough of a session for `apply_edit`: a compiled `ir`, the admission
    inputs in `origin`, a real `LeaseBook`, the bound operator/policy, and a
    `swap` that records instead of booting."""

    def __init__(self, operator=None, sandbox=None):
        self.ir = compile_source(TWO)
        self.origin = {"source": TWO, "modules": {}}
        self.draft = None
        self.loaded = True
        self.operator = operator
        self.sandbox = sandbox
        self.leases = LeaseBook()
        self.swaps = []

    def swap(self, ir, origin=None):
        self.swaps.append(ir)
        return {"generation": len(self.swaps)}


# ------------------------------------------- the edge: an edit is a swap


def test_an_enforced_lease_refuses_the_edit_and_nothing_swaps():
    sess = _FakeSession(Operator("alice"), sandbox=ENFORCING)
    sess.leases.claim("UserCache", "bob", ttl=600)
    out = E.apply_edit(sess, _edit_user())
    assert out["ok"] is False and out["swapped"] is False
    assert out["authorized"] is False
    assert out["lease"]["heldBy"] == "bob"
    assert "untouched" in out["note"]
    # the running composition and the working buffer are exactly as they were
    assert sess.swaps == [] and sess.draft is None


def test_the_edit_hands_the_gate_the_same_bytes_the_swap_hands_it(monkeypatch):
    """The delta and the document are the same candidate, so the gate sees the
    same bytes and derives the same targets — that is *why* the two verdicts are
    identical, rather than a coincidence of two parallel code paths."""
    seen = {}
    real = L.check_swap

    def _spy(session, arguments, now=None):
        seen["args"] = arguments
        return real(session, arguments, now)

    monkeypatch.setattr(server._leases, "check_swap", _spy)
    sess = _FakeSession(Operator("alice"), sandbox=ENFORCING)
    sess.leases.claim("UserCache", "bob", ttl=600)

    out = E.apply_edit(sess, _edit_user())
    assert out["ok"] is False and out["lease"]["heldBy"] == "bob"

    swap_arguments = {"source": _swap_user()}
    assert seen["args"]["source"] == swap_arguments["source"]
    assert (L._swap_targets(sess, seen["args"])
            == L._swap_targets(sess, swap_arguments) == ["UserCache"])


def test_an_advisory_policy_still_lets_the_edit_land():
    """No enforcement => the default path pays nothing and the edit swaps."""
    sess = _FakeSession(Operator("alice"), sandbox=parse_policy(""))
    sess.leases.claim("UserCache", "bob", ttl=600)
    out = E.apply_edit(sess, _edit_user())
    assert out["ok"] is True and out["admitted"] is True and out["swapped"] is True
    assert len(sess.swaps) == 1


def test_the_edit_gate_sees_the_patched_source_not_the_running_one(monkeypatch):
    """The quarantine gate has to grade the candidate — the patched bytes — not
    the composition the server happens to be holding. Mirror of the compile in
    step (3), which is why the gate is handed the patched source set."""
    sess = _FakeSession(Operator("alice"), sandbox=QUARANTINE)
    seen = []

    def _stub(session, arguments):
        seen.append(arguments.get("source"))
        return None

    monkeypatch.setattr(server._quarantine, "gate_swap", _stub)
    out = E.apply_edit(sess, _edit_user())
    assert out["swapped"] is True
    assert seen == [TWO.replace(_PROVIDER, _PROVIDER_FIXED)]
    assert seen != [TWO]


def test_a_required_quarantine_refuses_the_edit_and_nothing_swaps(monkeypatch):
    sess = _FakeSession(Operator("alice"), sandbox=QUARANTINE)
    refusal = {"ok": False, "admitted": False, "swapped": False,
               "note": "the candidate did not pass a required quarantine",
               "quarantine": {"verdict": "trapped"}}
    monkeypatch.setattr(server._quarantine, "gate_swap",
                        lambda session, arguments: dict(refusal))

    out = E.apply_edit(sess, _edit_user())
    assert out["ok"] is False and out["swapped"] is False
    assert out["quarantine"]["verdict"] == "trapped"
    # an edit that never swaps reports itself as not edited, like every other arm
    assert out["edited"] is False
    assert sess.swaps == [] and sess.draft is None


@needs_runtime
def test_the_swap_and_ship_paths_run_the_gate_they_claim(monkeypatch,
                                                         _fresh_session):
    """`docs/quarantine-tier.md` names `revl_swap` and `revl_ship --apply`. The
    ship handler fuses the stages and calls the swap handler for the last one
    (`ship.py` hands it the same arguments), so both must ask the gate.
    Deleting the `gate_swap` call in `server._tool_swap` fails both arms here
    with `seen == []`."""
    server.SESSION.sandbox = QUARANTINE
    server.SESSION.operator = Operator(
        "alice", (Grant(("swap", "edit", "load"), ("*",), True),))
    assert _call("revl_load", {"source": TWO})["ok"] is True
    seen = []

    def _stub(session, arguments):
        seen.append(arguments)
        return {"ok": False, "admitted": False, "swapped": False,
                "note": "the candidate did not pass a required quarantine",
                "quarantine": {"verdict": "trapped"}}

    monkeypatch.setattr(server._quarantine, "gate_swap", _stub)

    swapped = _call("revl_swap", {"source": _swap_user()})
    assert swapped["ok"] is False and swapped["swapped"] is False
    assert swapped["quarantine"]["verdict"] == "trapped"

    shipped = _call("revl_ship", {"source": _swap_user(), "apply": True})
    assert shipped["ok"] is False and shipped["shipped"] is False
    assert seen == [{"source": _swap_user()},
                    {"source": _swap_user(), "apply": True}], \
        "the gate a swap runs is not the gate revl_swap and revl_ship reach"
    assert server.SESSION.ir is not None, "the load stands, nothing swapped"


def test_none_of_the_gates_run_when_the_edit_still_has_holes():
    """A draft swaps nothing, so it is gated by nothing: the holes come back and
    the working buffer advances, exactly as when no lease is live."""
    sess = _FakeSession(Operator("alice"), sandbox=ENFORCING)
    sess.leases.claim("UserCache", "bob", ttl=600)
    holed = TWO.replace("fn size() = 0", "fn size() = hole")
    sess.origin = {"source": holed, "modules": {}}

    out = E.apply_edit(sess, {"edits": [
        {"anchor": _PROVIDER.replace("fn size() = 0", "fn size() = hole"),
         "replacement": _PROVIDER_FIXED}]})

    assert out["ok"] is True and out["swapped"] is False
    assert out["holes"]                      # OtherCache's hole is still open
    assert sess.draft is not None and sess.swaps == []


# ------------------------------------- the fence: a document mints no claim


def test_a_restored_lease_does_not_exempt_the_operator_its_document_names():
    """The bypass: a client supplies a snapshot whose lease names *it*, and
    `check_swap`'s self-holder exemption then waves it through the fence its own
    restore had just re-seated."""
    sess = _FakeSession(Operator("bob"), sandbox=ENFORCING)
    persist._restore_leases(sess, [{"component": "UserCache", "holder": "bob",
                                    "acquired": 1.0, "expiry": 1e12}])
    assert sess.leases.holder_of("UserCache") == "bob"
    refusal = L.check_swap(sess, {"source": _swap_user()})
    assert refusal is not None and refusal.component == "UserCache"
    assert "snapshot" in refusal.message


def test_re_claiming_the_name_earns_the_exemption_back():
    """The recovery path is explicit and holder-checked: a real claim, not a
    document, makes the name yours."""
    sess = _FakeSession(Operator("bob"), sandbox=ENFORCING)
    persist._restore_leases(sess, [{"component": "UserCache", "holder": "bob",
                                    "acquired": 1.0, "expiry": 1e12}])
    assert L.check_swap(sess, {"source": _swap_user()}) is not None
    sess.leases.claim("UserCache", "bob", ttl=600)   # bob is the named holder
    assert L.check_swap(sess, {"source": _swap_user()}) is None


def test_a_document_cannot_claim_verified_either():
    """`reinstate` ignores the document's own `verified`, so a round trip cannot
    launder an unverified lease into an exemption."""
    book = LeaseBook()
    lease = book.reinstate("UserCache", "bob", 1.0, 1e12)
    assert lease is not None and lease.verified is False
    sess = _FakeSession(Operator("bob"), sandbox=ENFORCING)
    persist._restore_leases(sess, [{"component": "UserCache", "holder": "bob",
                                    "acquired": 1.0, "expiry": 1e12,
                                    "verified": True}])
    assert L.check_swap(sess, {"source": _swap_user()}) is not None


def test_a_restored_lease_still_fences_everyone_else():
    """The fence is the part that has to survive: bob's restored lease still
    stops alice, exactly like a claimed one."""
    sess = _FakeSession(Operator("alice"), sandbox=ENFORCING)
    persist._restore_leases(sess, [{"component": "UserCache", "holder": "bob",
                                    "acquired": 1.0, "expiry": 1e12}])
    refusal = L.check_swap(sess, {"source": _swap_user()})
    assert refusal is not None and refusal.heldBy == "bob"
    assert "snapshot" not in refusal.message   # the ordinary message


def test_plan_agrees_with_swap_about_a_restored_self_lease():
    """Advisory must not stay silent where enforcement refuses — a plan that
    says "clear" and a swap that refuses is the divergence that hides a bug."""
    sess = _FakeSession(Operator("bob"), sandbox=ENFORCING)
    persist._restore_leases(sess, [{"component": "UserCache", "holder": "bob",
                                    "acquired": 1.0, "expiry": 1e12}])
    warnings = L.advise(sess, ["UserCache"])
    assert [w["component"] for w in warnings] == ["UserCache"]
    assert "re-claim" in warnings[0]["message"]


def test_an_elapsed_restored_lease_still_does_not_come_back():
    """The pre-existing rehydrate rule is untouched: a wall-clock death across a
    restart is real."""
    sess = _FakeSession(Operator("bob"), sandbox=ENFORCING)
    persist._restore_leases(sess, [{"component": "UserCache", "holder": "bob",
                                    "acquired": 1.0, "expiry": 2.0}])
    assert sess.leases.holder_of("UserCache") is None
    assert L.check_swap(sess, {"source": _swap_user()}) is None


# --------------------------------------------------- end to end, over stdio


def _call(tool, arguments):
    resp = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                          "params": {"name": tool, "arguments": arguments}})
    return resp["result"]["structuredContent"]


@pytest.fixture()
def _fresh_session():
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


@needs_runtime
def test_edit_and_swap_of_the_same_bytes_are_refused_together(_fresh_session):
    server.SESSION.sandbox = ENFORCING
    server.SESSION.operator = Operator(
        "alice", (Grant(("swap", "edit", "load"), ("*",), True),))
    assert _call("revl_load", {"source": TWO})["ok"] is True
    server.SESSION.leases.claim("UserCache", "bob", ttl=600)

    refused_swap = _call("revl_swap", {"source": _swap_user()})
    assert refused_swap["ok"] is False and refused_swap["swapped"] is False
    assert refused_swap["lease"]["heldBy"] == "bob"

    refused_edit = _call("revl_edit", _edit_user())
    assert refused_edit["ok"] is False and refused_edit["swapped"] is False
    assert refused_edit["lease"]["heldBy"] == "bob"
    assert server.SESSION.draft is None
    assert server.SESSION.leases.holder_of("UserCache") == "bob"


@needs_runtime
def test_the_edit_lands_once_the_holder_releases_the_name(_fresh_session):
    server.SESSION.sandbox = ENFORCING
    server.SESSION.operator = Operator(
        "alice", (Grant(("swap", "edit", "load"), ("*",), True),))
    assert _call("revl_load", {"source": TWO})["ok"] is True
    server.SESSION.leases.claim("UserCache", "bob", ttl=600)
    assert _call("revl_edit", _edit_user())["ok"] is False

    # bob hands the live lease back; alice claims it and her edit lands
    server.SESSION.leases.release("UserCache", "bob")
    server.SESSION.leases.claim("UserCache", "alice", ttl=600)
    landed = _call("revl_edit", _edit_user())
    assert landed["ok"] is True and landed["admitted"] is True
    assert landed["swapped"] is True
