"""The cross-machine deploy verb over MCP (roadmap item 476, issue #830).

The gap: the MCP surface hot-swaps a component into the LOCAL running
composition (`revl_swap` / `revl_ship` / `revl_repair`), and `revl deploy`
reconfigures ACROSS machines (`via = ssh`, item 118, #811) — but only from the
CLI. An agent driving a composition over MCP could not reach the cross-machine
leg at all, so the one operation whose whole reason for existing is that a
HUMAN should be asked before a machine boundary opens was the one an agent
could not even start.

`revl_deploy` closes that gap through the SAME machinery the CLI uses — never a
second protocol. The load-bearing claims under test:

  1. the read-only REHEARSAL (`apply` absent or false) runs the deploy-map
     admission gate and reports the plan while mutating NOTHING on either host:
     the `run` leg and the approval gate are never invoked;
  2. admission EARLY-EXITS — a map that does not admit (an ssh target with no
     pinned host key) stops at `admit` and never reaches `run`, so no boundary
     is opened and both hosts are untouched;
  3. `apply: true` is REFUSED without the configured approval (the issue's Exit
     clause): it returns the item-246 ticket two-step, and nothing fires;
  4. the leg is chosen by the admitted verdict, not by a flag — a map that
     admits a `via = ssh` target goes through `deploy_ssh_map`, an all-local map
     through `deploy_local_map` — so a map can never open a boundary it did not
     admit;
  5. the verb is GATED as the operation it performs: an operator who may not
     `deploy` cannot reach the handler, and one who may needs no second grant.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402
from revl.mcp import deploy as _mcp_deploy  # noqa: E402
from revl.mcp.operator import decide, parse_profile  # noqa: E402
from revl.mcp.server import handle  # noqa: E402


def _call(tool: str, arguments: dict) -> dict:
    response = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": tool, "arguments": arguments}})
    return response["result"]["structuredContent"]


# the back-compat shape `parse_deploy_map` documents: a placement with no
# `[deploy]` table anywhere is a perfectly good deploy map — it says "every
# process is my own child"
LOCAL_ONLY = {"processes": {"edge": {"components": ["Edge"]}}}

# a `via = ssh` target that pins nothing: the machine boundary is refused
UNPINNED_SSH = {"processes": {"db": {"deploy": {"via": "ssh",
                                                "host": "deploy@10.0.0.5",
                                                "runner": "revl"}}}}

# the other side of the same rule: an ssh target with BOTH a host and a pinned
# known_hosts file is admitted as a machine boundary
PINNED_SSH = {"processes": {"db": {"deploy": {
    "via": "ssh", "host": "deploy@10.0.0.5",
    "known_hosts": "/etc/revl/known_hosts"}}}}


# ---------------------------------------------------------------- the verb

def test_deploy_is_listed_and_not_read_only():
    listed = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    tools = {t["name"]: t for t in listed["result"]["tools"]}
    assert "revl_deploy" in tools
    # it defaults to a dry run, but it CAN mutate — so it advertises itself the
    # way revl_swap does rather than as a read-only verb
    assert tools["revl_deploy"]["annotations"]["readOnlyHint"] is False
    assert tools["revl_deploy"]["annotations"]["destructiveHint"] is True
    # the deploy map is the argument; everything host-shaped lives inside it
    assert tools["revl_deploy"]["inputSchema"]["required"] == ["placement"]


def test_a_missing_placement_is_a_usage_error_not_a_deploy():
    result = _call("revl_deploy", {})
    assert result["ok"] is False
    assert result["stoppedAt"] == "usage"
    assert result["rehearsal"] is True   # nothing was applied
    assert "placement" in result["reason"]


# ------------------------------------------- the rehearsal mutates nothing

def test_the_orchestration_never_calls_run_or_authorize_on_a_rehearsal():
    """The load-bearing silence: on a rehearsal the ADMIT stage runs and the
    `run` leg and approval gate are never reached. Proven with fakes, so it
    needs no second machine — and it is the proof that a rehearsal cannot open
    a boundary, which is the claim the verb's description makes."""
    calls = []

    def admit(_a):
        calls.append("admit")
        return {"ok": True, "targets": {"db": {"via": "ssh"}},
                "refusals": [], "boundaries": {"db": "machine"}}

    def run(_a):
        calls.append("run")
        raise AssertionError("a rehearsal must never reach the run leg")

    def authorize(_a):
        calls.append("authorize")
        raise AssertionError("a rehearsal must never reach the approval gate")

    result = _mcp_deploy.deploy({"placement": PINNED_SSH},
                                admit=admit, run=run, authorize=authorize)
    assert calls == ["admit"]
    assert result["ok"] is True and result["admitted"] is True
    assert result["applied"] is False and result["rehearsal"] is True
    assert result["stoppedAt"] is None
    assert result["targets"] == {"db": {"via": "ssh"}}


def test_a_rehearsal_reports_the_plan_over_the_wire():
    """The same claim end-to-end through the real handler: an admitting ssh map
    rehearses, mutates nothing, and names the plan."""
    result = _call("revl_deploy", {"placement": PINNED_SSH})
    assert result["ok"] is True
    assert result["admitted"] is True
    assert result["applied"] is False and result["rehearsal"] is True
    assert result["stoppedAt"] is None
    assert result["boundaries"] == {"db": "machine"}
    assert result["targets"]["db"]["host"] == "deploy@10.0.0.5"


def test_an_all_local_deploy_map_rehearses_too():
    """A map with no `[deploy]` table admits with no targets at all — the
    back-compat that makes a deploy map a placement map."""
    result = _call("revl_deploy", {"placement": LOCAL_ONLY})
    assert result["ok"] is True and result["admitted"] is True
    assert result["targets"] == {} and result["boundaries"] == {}


# --------------------------------------------- admission early-exits the chain

def test_a_refused_map_stops_at_admit_and_opens_no_boundary():
    """An ssh target with no pinned host key is refused at admission (R4:
    host-key checking is never trust-on-first-use). The chain stops there —
    the run leg is never entered, so nothing is staged and no boundary opens,
    on either host."""
    result = _call("revl_deploy", {"placement": UNPINNED_SSH, "apply": True,
                                   "bundle": "/tmp/whatever.bundle"})
    assert result["ok"] is False
    assert result["admitted"] is False
    assert result["applied"] is False
    assert result["stoppedAt"] == "admit"
    assert result["rehearsal"] is False   # an apply was asked for and refused
    assert result["refusals"] and result["refusals"][0]["rule"] == "machine-boundary"
    assert "known_hosts" in result["refusals"][0]["reason"]


def test_the_orchestration_does_not_reach_run_after_a_refused_admission():
    """The early-exit itself, with fakes: the run leg is not invoked at all."""
    calls = []

    def admit(_a):
        calls.append("admit")
        return {"ok": False, "targets": {}, "refusals": [
            {"process": "db", "rule": "machine-boundary", "reason": "no pin"}]}

    def run(_a):
        calls.append("run")
        raise AssertionError("a refused map must never reach the run leg")

    result = _mcp_deploy.deploy({"placement": UNPINNED_SSH, "apply": True,
                                 "bundle": "/tmp/x.bundle"},
                                admit=admit, run=run)
    assert calls == ["admit"]
    assert result["stoppedAt"] == "admit" and result["ok"] is False
    assert result["refusals"][0]["rule"] == "machine-boundary"


# ------------------------------------- apply is approval-gated (the Exit clause)
#
# These tests deliberately leave `SESSION.approval_policy` at its default
# (`None` = OFF, "every call proceeds") rather than setting it to `"auto"`, and
# that is the POINT: the deploy gate is self-contained, so `apply: true` is
# refused on its own authority, not because a serve-time policy was switched on
# (unlike `revl_swap`, whose gate is policy-conditional). Mutating the module
# global would also leak into every later file in the same pytest process.

def test_apply_without_approval_returns_the_ticket_and_deploys_nothing():
    """The issue's Exit clause: a cross-machine reconfiguration is an
    irreversible crossing, so `apply: true` is refused without the configured
    approval. The refusal is the item-246 ticket two-step — a STEP, carrying the
    hash a human yes needs — not an opaque internal fault, and nothing fires.
    No `approval_policy` is set: the gate is the deploy's own."""
    result = _call("revl_deploy", {"placement": PINNED_SSH, "apply": True,
                                   "bundle": "/tmp/x.bundle"})
    assert result["ok"] is False
    assert result.get("approvalRequired") is True
    assert "ticket" in result and "hash" in result["ticket"]
    # no verdict at all: the run leg never ran, so there is nothing to report
    assert "verdict" not in result and "applied" not in result


def test_the_ticket_is_keyed_by_the_deploy_identity_and_fires_once():
    """The ticket is keyed by the deploy's own identity (placement + bundle +
    backend digest), so the identical re-issue after `revl_approve` finds the
    standing approval and fires exactly once — and a DIFFERENT deploy does not
    inherit a yes meant for another one."""
    from revl.mcp.approval import ApprovalRequired
    from revl.mcp.server import _tool_deploy_authorize

    arguments = {"placement": PINNED_SSH, "bundle": "/tmp/x.bundle",
                 "backend": "python"}

    with pytest.raises(ApprovalRequired) as first:
        _tool_deploy_authorize(arguments)
    ticket_hash = first.value.ticket["hash"]

    # a different deploy identity mints a different ticket — a yes for one
    # deploy is not a yes for another
    with pytest.raises(ApprovalRequired) as other:
        _tool_deploy_authorize({**arguments, "bundle": "/tmp/other.bundle"})
    assert other.value.ticket["hash"] != ticket_hash

    approved = _call("revl_approve", {"hash": ticket_hash})
    assert approved["ok"] is True and approved["approved"] is True

    # the identical re-issue now passes (the standing approval is consumed) ...
    _tool_deploy_authorize(arguments)   # returns: no raise

    # ... and is spent: a THIRD call mints a fresh ticket rather than reusing it
    with pytest.raises(ApprovalRequired):
        _tool_deploy_authorize(arguments)


def test_apply_without_a_bundle_stops_before_the_approval_gate():
    """A cross-machine deploy needs the attested bundle the far host re-hashes.
    Applying without one stops at `run` BEFORE the approval gate — so no ticket
    is minted for a deploy that could not have proceeded anyway."""
    result = _call("revl_deploy", {"placement": PINNED_SSH, "apply": True})
    assert result["ok"] is False
    assert result["admitted"] is True
    assert result["stoppedAt"] == "run"
    assert result.get("approvalRequired") is not True
    assert "bundle" in result["reason"]


# ------------------------------------------- the leg is the admitted verdict

def test_the_leg_is_chosen_by_the_admitted_map_not_by_a_flag(monkeypatch, tmp_path):
    """The choice of leg is the ADMITTED VERDICT, exactly as the CLI's COMMIT
    path decides it: a map that admits a `via = ssh` target goes through
    `deploy_ssh_map`, an all-local map through `deploy_local_map`. A flag could
    be wrong; the verdict cannot open a boundary it did not admit."""
    import tempfile

    monkeypatch.setattr(tempfile, "mkdtemp", lambda **kw: str(tmp_path))
    seen = []

    def fake_ssh(placement, *, local_bundle, backend, state_dir):
        seen.append(("ssh", local_bundle, backend, state_dir))
        return {"verdict": "applied", "participants": ["db"]}

    def fake_local(placement, *, state_dir):
        seen.append(("local", state_dir))
        return {"verdict": "applied", "participants": ["edge"]}

    from revl.mcp.server import _deploy, _tool_deploy_run
    monkeypatch.setattr(_deploy, "deploy_ssh_map", fake_ssh)
    monkeypatch.setattr(_deploy, "deploy_local_map", fake_local)

    report = _tool_deploy_run({"placement": PINNED_SSH, "bundle": "/tmp/b.bundle",
                               "backend": "python"})
    assert seen == [("ssh", "/tmp/b.bundle", "python", str(tmp_path))]
    assert report["verdict"] == "applied"

    seen.clear()
    _tool_deploy_run({"placement": LOCAL_ONLY})
    assert seen == [("local", str(tmp_path))]


def test_an_aborted_deploy_is_never_reported_as_applied():
    """`ok` is exactly "the deploy applied". An abort — clean or with residue —
    and an unsettleable remote (`unresolved`) are all NOT applied, and each is
    reported by its own flag rather than collapsed into one failure."""
    def admit(_a):
        return {"ok": True, "targets": {"db": {"via": "ssh"}},
                "refusals": [], "boundaries": {"db": "machine"}}

    for verdict, flag in (("aborted-clean", "aborted"),
                          ("aborted-with-residue", "aborted"),
                          ("unresolved", "unresolved")):
        result = _mcp_deploy.deploy(
            {"placement": PINNED_SSH, "apply": True, "bundle": "/tmp/x.bundle"},
            admit=admit, run=lambda _a, v=verdict: {"verdict": v, "phase": "commit"})
        assert result["ok"] is False and result["applied"] is False
        assert result["verdict"] == verdict
        assert result[flag] is True, f"{verdict} must report {flag}"
        assert result["stoppedAt"] == "run"


# ------------------------------------------------------------- the authority gate

class _FakeSession:
    """Enough session for the gate: a running composition and who is driving."""

    def __init__(self, ir, operator):
        self.ir = ir
        self.operator = operator
        self.loaded = True
        self.sandbox = None


LIVE = """
service Cache { fn get(k: Str) -> Opt[Str] }
service Log { fn note(m: Str) -> Int }
component TenantACache provides ca: Cache {
  let s = effect Map.new() undo s.drop()
  provide ca { fn get(k) = s.get(k) }
}
component TenantBLog provides cb: Log {
  let t = effect Map.new() undo t.drop()
  provide cb { fn note(m) = 1 }
}
"""


def test_revl_deploy_needs_the_deploy_verb_not_merely_swap():
    """A deploy gets its OWN authority address: an operator trusted to hot-swap
    a component locally is not automatically trusted to push a composition onto
    a second host. The gate refuses without `may deploy`, and allows with it."""
    ir = compile_source(LIVE)
    arguments = {"placement": PINNED_SSH, "apply": True}

    bob = parse_profile("operator bob may swap on *").get("bob")
    refused = decide(_FakeSession(ir, bob), "revl_deploy", arguments)
    assert refused.gated and not refused.allowed, \
        "a local-swap grant must not authorize a cross-machine deploy"

    alice = parse_profile("operator alice may deploy on *").get("alice")
    allowed = decide(_FakeSession(ir, alice), "revl_deploy", arguments)
    assert allowed.gated and allowed.allowed


def test_a_deploy_scopes_to_the_whole_composition_it_reconfigures():
    """A deploy reconfigures the running composition as ONE coordinated unit, so
    the all-or-nothing rule is over every live component: a grant naming only
    one of them does not authorize pushing the whole composition across the
    boundary — and a correctly scoped grant still works."""
    ir = compile_source(LIVE)
    arguments = {"placement": PINNED_SSH, "apply": True}

    scoped = parse_profile("operator carol may deploy on TenantACache").get("carol")
    refused = decide(_FakeSession(ir, scoped), "revl_deploy", arguments)
    assert refused.gated and not refused.allowed
    # the refusal names the component the grant does not reach
    assert "TenantBLog" in refused.message

    both = parse_profile("operator dave may deploy on TenantACache\n"
                         "operator dave may deploy on TenantBLog").get("dave")
    allowed = decide(_FakeSession(ir, both), "revl_deploy", arguments)
    assert allowed.gated and allowed.allowed
    assert allowed.subjects == ("TenantACache", "TenantBLog")
