"""Operator capabilities — G4 for the management plane (roadmap item 55).

The MCP session's mutating verbs (revl_swap/unload/restore/undo/edit/load/
snapshot) can rewrite a running system, and nothing there scopes the caller.
This gates each one against a declared **operator profile**: a session runs as
one operator, and a management verb is refused unless that operator's grants
permit it for the target component/realm — with a policy-style why, and the
operator identity recorded into the causal trace ("who").

Most of this is exercised without the cordis-py runtime: gating is a pure
decision over `session.ir` (which a refused verb never runs past), so the
target-computation and refusal paths need no live composition. Only the
end-to-end "an authorized swap actually lands and the trace names who" test
boots a runtime, and is skipped where it is not installed.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402
from revl.mcp import operator as op  # noqa: E402
from revl.mcp import server  # noqa: E402
from revl.mcp.operator import (  # noqa: E402
    Operator, ProfileError, decide, parse_profile,
)

needs_runtime = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="the live swap needs the cordis-py runtime — install it with "
           "`sh backends/python/setup.sh`",
)

# a two-realm composition: one tenant per realm, so a swap can target one
# tenant's component while another operator is scoped to a different tenant.
TWO_REALM = """
service Cache { fn get(k: Str) -> Opt[Str]
                fn size() -> Int }
component TenantACache provides ca: Cache {
  isolate ca in realm("tenant_a")
  let s = effect Map.new() undo s.drop()
  provide ca { fn get(k) = s.get(k)
               fn size() = 0 }
}
component TenantBCache provides cb: Cache {
  isolate cb in realm("tenant_b")
  let s = effect Map.new() undo s.drop()
  provide cb { fn get(k) = s.get(k)
               fn size() = 0 }
}
"""

PROFILE = """
# alice runs tenant_a: may swap within it, may snapshot everything,
# may never unload the whole system
operator alice may swap, plan on tenant_a*
operator alice may snapshot on *
operator alice may not unload on *

# bob is a read/backup operator: may snapshot, nothing else
operator bob may snapshot on *
"""


def _swap_a():
    return TWO_REALM.replace("provide ca { fn get(k) = s.get(k)\n               fn size() = 0 }",
                             "provide ca { fn get(k) = s.get(k)\n               fn size() = 42 }")


def _swap_b():
    return TWO_REALM.replace("provide cb { fn get(k) = s.get(k)\n               fn size() = 0 }",
                             "provide cb { fn get(k) = s.get(k)\n               fn size() = 42 }")


class _FakeSession:
    """Enough of a session for the gate: it reads only `ir` and `operator`.
    A refused verb never runs past the gate, so no driver is needed."""

    def __init__(self, ir, operator):
        self.ir = ir
        self.operator = operator


# ---------------------------------------------------------------- the profile


def test_dsl_parses_operators_and_grants():
    reg = parse_profile(PROFILE)
    assert sorted(reg.operators) == ["alice", "bob"]
    alice = reg.get("alice")
    # three grants: swap (allow), snapshot (allow), unload (deny)
    assert len(alice.grants) == 3
    assert any(g.allow and "swap" in g.verbs for g in alice.grants)
    assert any(not g.allow and "unload" in g.verbs for g in alice.grants)


def test_json_profile_is_equivalent():
    reg = parse_profile(
        '{"operators": [{"token": "alice", "grants": ['
        '{"verbs": ["swap"], "on": ["tenant_a*"]},'
        '{"verbs": ["unload"], "on": ["*"], "deny": true}]}]}')
    alice = reg.get("alice")
    labels_a = frozenset({"TenantACache", "tenant_a"})
    assert alice.allows("swap", labels_a)[0] is True
    assert alice.allows("unload", labels_a)[0] is False


def test_rollback_verb_is_an_alias_for_undo():
    reg = parse_profile("operator carol may rollback on *")
    carol = reg.get("carol")
    # both revl_rollback and revl_undo map to the same 'undo' verb key
    assert carol.allows("undo", frozenset({"*"}))[0] is True


def test_sole_operator_needs_no_token():
    reg = parse_profile("operator only may swap on *")
    assert reg.sole().token == "only"
    assert parse_profile(PROFILE).sole() is None  # two operators -> ambiguous


def test_malformed_profile_is_a_parse_error():
    with pytest.raises(ProfileError):
        parse_profile("operator alice can swap on *")  # no may/may not


# ------------------------------------------------------------- gating: swap


def test_swap_within_the_granted_realm_is_authorized():
    sess = _FakeSession(compile_source(TWO_REALM), parse_profile(PROFILE).get("alice"))
    d = decide(sess, "revl_swap", {"source": _swap_a()})
    assert d.gated and d.allowed
    assert d.subjects == ("TenantACache",)  # only the touched component


def test_swap_outside_the_granted_realm_is_refused_with_a_why():
    sess = _FakeSession(compile_source(TWO_REALM), parse_profile(PROFILE).get("alice"))
    d = decide(sess, "revl_swap", {"source": _swap_b()})
    assert d.gated and not d.allowed
    assert "TenantBCache" in d.message
    assert d.why is not None and d.why.subject == "alice"
    # the why-trace names the operator and the target it may not reach
    assert d.why.path() == ["alice", "TenantBCache"]


def test_unload_is_refused_by_a_deny_rule():
    sess = _FakeSession(compile_source(TWO_REALM), parse_profile(PROFILE).get("alice"))
    d = decide(sess, "revl_unload", {})
    assert d.gated and not d.allowed
    assert "may not" in d.message and "unload" in d.message


def test_snapshot_everything_operator_snapshots_but_cannot_mutate():
    bob = parse_profile(PROFILE).get("bob")
    sess = _FakeSession(compile_source(TWO_REALM), bob)
    assert decide(sess, "revl_snapshot", {}).allowed is True
    # bob has no swap grant at all -> every swap refused, target notwithstanding
    assert decide(sess, "revl_swap", {"source": _swap_a()}).allowed is False
    assert decide(sess, "revl_unload", {}).allowed is False


# --------------------------------------------------- cold load is ungated


def test_cold_load_is_ungated_but_swap_and_reload_stay_gated():
    """Roadmap item 300: the initial cold `revl_load` (nothing live yet,
    `session.ir is None`) is not a privileged mutation: it only boots a
    candidate to inspect/gauntlet, so it is ungated even for an operator whose
    profile carries no `load` grant. Ungating the cold load must NOT open a
    hole: with a composition live the same operator still cannot swap or reload
    without the appropriate grant."""
    alice = parse_profile(PROFILE).get("alice")  # swap/plan/snapshot, no `load`
    empty = Operator("alice")                    # no grants at all

    # cold: nothing loaded -> load proceeds ungated for either operator.
    for oper in (alice, empty):
        cold = decide(_FakeSession(None, oper), "revl_load", {"source": TWO_REALM})
        assert cold.gated is False and cold.allowed is True

    # but the gate still holds on the state-changing verbs, so no hole opened:
    live = compile_source(TWO_REALM)
    # a swap the profile does not cover (tenant_b) is still refused,
    swap_b = decide(_FakeSession(live, alice), "revl_swap", {"source": _swap_b()})
    assert swap_b.gated and not swap_b.allowed and "TenantBCache" in swap_b.message
    # an operator with no grants at all cannot swap or unload anything,
    assert decide(_FakeSession(live, empty), "revl_swap",
                  {"source": _swap_a()}).allowed is False
    assert decide(_FakeSession(live, empty), "revl_unload", {}).allowed is False
    # and a second `revl_load` against a running composition stays gated
    # (`load` never replaces/activates a live comp; the handler refuses it too).
    reload_live = decide(_FakeSession(live, empty), "revl_load", {"source": TWO_REALM})
    assert reload_live.gated is True and reload_live.allowed is False


# -------------------------------------------------- back-compat: no profile


def test_no_profile_leaves_every_verb_ungated():
    sess = _FakeSession(compile_source(TWO_REALM), None)
    for tool in ("revl_swap", "revl_unload", "revl_snapshot", "revl_load"):
        d = decide(sess, tool, {"source": _swap_a()})
        assert d.gated is False  # unchanged: root over transport


def test_read_only_verbs_are_never_gated_even_with_a_profile():
    sess = _FakeSession(compile_source(TWO_REALM), parse_profile(PROFILE).get("bob"))
    for tool in ("revl_check", "revl_audit", "revl_plan", "revl_state",
                 "revl_query_reach", "revl_grammar"):
        assert decide(sess, tool, {}).gated is False


def test_a_candidate_that_does_not_compile_defers_to_the_handler():
    # gating cannot scope what will not compile; the handler rejects it and
    # nothing mutates, so the gate steps aside (does not spuriously refuse).
    sess = _FakeSession(compile_source(TWO_REALM), parse_profile(PROFILE).get("bob"))
    d = decide(sess, "revl_swap", {"source": "service Broken {"})
    assert d.gated is False


# ----------------------------------------------- who: the authority stamp


def test_authority_stamp_records_who_and_injects_a_trace_event():
    alice = parse_profile(PROFILE).get("alice")
    decision = op.Decision(gated=True, allowed=True, operator="alice",
                           verb="swap", subjects=("TenantACache",))
    payload = {"ok": True, "trace": [{"channel": "lifecycle",
                                      "subject": "TenantACache", "detail": "up"}]}
    server._stamp_authority(payload, decision)
    # who is on the result
    assert payload["authority"] == {"operator": "alice", "verb": "swap",
                                    "subjects": ["TenantACache"], "allowed": True}
    # and rides in the causal trace itself, first, so "what changed and on
    # whose authority" is one query over the same trace
    assert payload["trace"][0]["channel"] == "operator"
    assert payload["trace"][0]["subject"] == "alice"
    assert "swap" in payload["trace"][0]["detail"]


def test_refused_payload_carries_the_why_and_leaves_the_system_untouched():
    alice = parse_profile(PROFILE).get("alice")
    sess = _FakeSession(compile_source(TWO_REALM), alice)
    d = decide(sess, "revl_swap", {"source": _swap_b()})
    payload = server._refused_by_operator(d)
    assert payload["ok"] is False
    assert payload["authorized"] is False
    assert payload["authority"]["operator"] == "alice"
    assert payload["why"]["subject"] == "alice"
    assert "untouched" in payload["note"]


# --------------------------------------------- end-to-end through the server


@pytest.fixture()
def _profiled_session():
    """Bind the real server SESSION to an operator for one test, then clear."""
    prior = server.SESSION.operator
    server.SESSION.operator = parse_profile(PROFILE).get("alice")
    yield server.SESSION
    server.SESSION.operator = prior
    if server.SESSION.loaded:
        server.SESSION.unload()


def _call(tool, arguments):
    resp = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                          "params": {"name": tool, "arguments": arguments}})
    return resp["result"]["structuredContent"]


def test_refused_swap_never_reaches_the_handler(_profiled_session):
    # no runtime needed: the gate refuses before _tool_swap is ever called,
    # so this holds even without a loaded composition.
    server.SESSION.ir = compile_source(TWO_REALM)
    try:
        payload = _call("revl_swap", {"source": _swap_b()})
        assert payload["ok"] is False
        assert payload["authorized"] is False
        assert "TenantBCache" in payload["diagnostics"][0]["message"]
    finally:
        server.SESSION.ir = None


@needs_runtime
def test_authorized_swap_lands_and_the_trace_names_who(_profiled_session):
    loaded = _call("revl_load", {"source": TWO_REALM})
    assert loaded["ok"] is True
    # alice may swap within tenant_a
    swapped = _call("revl_swap", {"source": _swap_a()})
    assert swapped["ok"] is True and swapped["swapped"] is True
    assert swapped["authority"]["operator"] == "alice"
    assert swapped["authority"]["verb"] == "swap"
    assert any(e.get("channel") == "operator" and e.get("subject") == "alice"
               for e in swapped.get("trace", []))
    # but not within tenant_b — refused, the running system untouched
    refused = _call("revl_swap", {"source": _swap_b()})
    assert refused["ok"] is False and refused["authorized"] is False
    # and unload is denied outright
    assert _call("revl_unload", {})["authorized"] is False
