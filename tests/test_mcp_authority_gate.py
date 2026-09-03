"""The MCP authority gate: no verb reaches a privileged operation around it.

Two independently-found defects, one shape. A verb or a path reached an
authority-requiring operation without passing the check that guards it:

  1. `revl_swap`'s target derivation compiled the candidate WITHOUT the
     `replacing` the handler passes. A candidate that renames the component it
     replaces therefore failed to link for the gate and linked fine for the
     handler, so the derivation came back "undecidable" — and both the operator
     capability gate (item 55) and the enforced lease check (item 61) read
     undecidable as *defer*. An operator with no `swap` grant swapped; an
     operator swapped over another operator's enforced lease.
  2. `revl_gauntlet`, `revl_quarantine` and `revl_repair` compiled
     agent-supplied source through `compile_source` / `compile_files` directly,
     carrying no authoring trust — so a candidate `revl_check` / `revl_admit` /
     `revl_swap` refuse compiled here anyway, and the verbs that boot a scratch
     session then RAN it in the server process.

Both are the per-verb-check failure mode: enforcement that each author must
remember to wire is enforcement that will eventually not be wired. So the fixes
are positional, and this file holds the two guards that keep them positional:

  * `test_every_advertised_tool_is_gated_or_recorded_as_ungated` enumerates
    every advertised MCP tool and fails on one that is neither gated nor listed
    in `UNGATED` with a reason — the set is checked, not trusted;
  * `test_every_mcp_compiler_call_goes_through_the_authoring_door` reads every
    `compile_source` / `compile_files` call site under `src/revl/mcp/` out of
    the source and fails on one that neither carries `profile=` nor routes
    through `server.compile_under_authoring`.

The same enumerate-both-sides discipline as
`tests/test_swap_ref_pins.py::test_successor_spec_carries_every_boot_spec_key`,
for the same reason: the NEXT one cannot be forgotten the way these were.
"""

import ast
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402
from revl.mcp import leases as _leases  # noqa: E402
from revl.mcp import operator as _operator  # noqa: E402
from revl.mcp import server  # noqa: E402
from revl.mcp.operator import decide, parse_profile  # noqa: E402


# ---------------------------------------------------------------- harness

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

# the same composition with TenantBCache RENAMED. It provides the same key, so
# it only links against the running manifest when the compile is told that
# TenantBCache is being replaced.
RENAMED = TWO_REALM.replace("component TenantBCache", "component TenantBCache2")

# the same composition with only TenantBCache's body changed (the last
# `fn size()` in the text is its own), so a swap of it touches exactly one
# component
PATCHED = "fn size() = 1".join(TWO_REALM.rsplit("fn size() = 0", 1))


class _EnforcingPolicy:
    leases_enforced = True


class _FakeSession:
    """Enough session for the gate: what is running, who is driving, and
    whether the boundary policy enforces leases. No runtime involved."""

    def __init__(self, ir, operator, *, enforce_leases=False):
        self.ir = ir
        self.operator = operator
        self.loaded = ir is not None
        self.sandbox = _EnforcingPolicy() if enforce_leases else None


@pytest.fixture
def live():
    return compile_source(TWO_REALM)


def _call(name: str, arguments: dict) -> dict:
    response = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                              "params": {"name": name, "arguments": arguments}})
    return json.loads(response["result"]["content"][0]["text"])


def _text(payload: dict) -> str:
    return json.dumps(payload)


# ============================================================================
# 1. a swap cannot launder past the capability gate or an enforced lease
# ============================================================================

def test_a_renaming_swap_is_gated_like_any_other(live):
    """The bypass: `replacing` names TenantBCache and the candidate renames it.

    Both spellings of the same swap must reach the same verdict — an operator
    holding only `snapshot` may not swap either way."""
    bob = parse_profile("operator bob may snapshot on *").get("bob")

    plain = decide(_FakeSession(live, bob), "revl_swap", {"source": TWO_REALM})
    assert plain.gated and not plain.allowed

    renamed = decide(_FakeSession(live, bob), "revl_swap",
                     {"source": RENAMED, "replacing": ["TenantBCache"]})
    assert renamed.gated and not renamed.allowed, \
        "a renaming swap must be gated exactly as the plain one is"
    assert "TenantBCache" in renamed.message


def test_a_renaming_swap_scopes_to_what_it_actually_replaces(live):
    """Gated, and gated on the RIGHT subjects: the component that goes away and
    the one that takes its place — not the whole composition, so a correctly
    scoped grant still works."""
    alice = parse_profile(
        "operator alice may swap on TenantBCache\n"
        "operator alice may swap on TenantBCache2").get("alice")
    decision = decide(_FakeSession(live, alice), "revl_swap",
                      {"source": RENAMED, "replacing": ["TenantBCache"]})
    assert decision.gated and decision.allowed
    assert set(decision.subjects) == {"TenantBCache", "TenantBCache2"}


def test_a_renaming_swap_cannot_step_over_another_operators_lease(live):
    """`leases enforced` + a lease alice holds on TenantBCache. Both spellings
    of the swap that replaces it must be refused."""
    bob = parse_profile("operator bob may swap on *").get("bob")

    plain_session = _FakeSession(live, bob, enforce_leases=True)
    _leases._book(plain_session).claim("TenantBCache", "alice", ttl=600)
    assert _leases.check_swap(plain_session, {"source": TWO_REALM}) is not None

    renamed_session = _FakeSession(live, bob, enforce_leases=True)
    _leases._book(renamed_session).claim("TenantBCache", "alice", ttl=600)
    refusal = _leases.check_swap(
        renamed_session, {"source": RENAMED, "replacing": ["TenantBCache"]})
    assert refusal is not None, \
        "a renaming swap must not launder past an enforced lease"
    assert refusal.component == "TenantBCache"
    assert refusal.heldBy == "alice"


def test_an_undecidable_target_set_refuses_instead_of_ungating(live):
    """The general rule behind (1): the gate may not read "I cannot tell what
    this touches" as "so let it through". A candidate that will not compile has
    no derivable target set, and a subject-scoped operator is refused."""
    bob = parse_profile("operator bob may swap on tenant_a*").get("bob")
    decision = decide(_FakeSession(live, bob), "revl_swap",
                      {"source": "service Broken {"})
    assert decision.gated and not decision.allowed


def test_an_undecidable_swap_is_refused_against_every_held_lease(live):
    """The lease half of the same rule: a swap whose targets cannot be derived
    is checked against every active lease, not against none of them."""
    bob = parse_profile("operator bob may swap on *").get("bob")
    session = _FakeSession(live, bob, enforce_leases=True)
    _leases._book(session).claim("TenantBCache", "alice", ttl=600)
    assert _leases.check_swap(session, {"source": "service Broken {"}) is not None


def test_an_operator_holding_the_whole_composition_still_proceeds(live):
    """Failing closed must not become failing always: `may swap on *` covers
    the unnameable whole, so an unscoped operator still reaches the handler and
    gets its diagnostic."""
    root = parse_profile("operator root may swap on *").get("root")
    decision = decide(_FakeSession(live, root), "revl_swap",
                      {"source": "service Broken {"})
    assert decision.gated and decision.allowed


# ============================================================================
# 2. the grading/repair verbs compile under the same authoring trust
# ============================================================================

# a candidate that declares no extern of its own — so the structural
# pre-dispatch half does not fire — and reaches host code through a stdlib
# module that declares one. Only the transitive half of the untrusted-author
# profile refuses this, and that half lives in the compiler, on the verbs that
# pass a profile.
def _host_reaching_source(marker: str) -> str:
    return ('use "stdlib/shell.rvl" { sh }\n'
            'service Pwn { emission fn go() -> Str }\n'
            'component Pwned provides pwn: Pwn {\n'
            f'  emit sh("touch {marker}")\n'
            '  provide pwn { fn go() = emit sh("id") }\n'
            '}\n')

# any module at all, so the compile takes the `compile_files` path where a
# `use` resolves
_MODULES = {"dummy.rvl": "pub fn d() -> Int { return 1 }"}


@pytest.fixture(autouse=True)
def _closed_authoring(monkeypatch):
    """The default, closed authoring trust, restored afterwards."""
    from revl.mcp.session import Session

    before = server.AUTHORING
    monkeypatch.setattr(server, "SESSION", Session())
    server.set_authoring_trust(host_code=False, granted=None, providers=None,
                               roots=())
    yield
    server.AUTHORING = before


def test_revl_check_refuses_a_transitive_host_reach(tmp_path):
    """The reference refusal the other three must match."""
    payload = _call("revl_check",
                    {"source": _host_reaching_source(tmp_path / "marker"),
                     "modules": _MODULES})
    assert payload["ok"] is False
    assert "host code" in _text(payload)


@pytest.mark.parametrize("tool", ["revl_gauntlet", "revl_quarantine"])
def test_the_grading_verbs_refuse_what_the_gate_refuses(tool, tmp_path):
    """A candidate the admission gate refuses is GRADED on the refusal — never
    compiled past it, and never booted in a scratch session."""
    marker = tmp_path / "marker"
    payload = _call(tool, {"source": _host_reaching_source(marker),
                           "modules": _MODULES})
    assert payload.get("verdict") == "rejected", \
        f"{tool} admitted a candidate revl_check refuses"
    assert "host code" in _text(payload)
    assert not marker.exists(), f"{tool} executed the candidate's host code"


def test_the_repair_loop_refuses_what_the_gate_refuses(tmp_path):
    """`revl_repair` runs the gauntlet on an agent-supplied candidate and swaps
    it in. Its candidate is authored source like any other."""
    marker = tmp_path / "marker"
    payload = _call("revl_repair", {
        "component": "Pwned",
        "candidate": {"source": _host_reaching_source(marker),
                      "modules": _MODULES},
        "selfRepairPolicy": {"eligible": [{"component": "*"}],
                             "mayTouch": ["*"], "ackOnWiden": False},
        "trace": [{"channel": "fault", "subject": "Pwned", "detail": "boom"}],
        "apply": False,
    })
    assert (payload.get("incident") or {}).get("status") == "rejected"
    assert (payload.get("incident") or {}).get("swapped") is not True
    assert not marker.exists(), "revl_repair executed the candidate's host code"


# ============================================================================
# 3. the composed verbs — a swap reached through another verb's machinery
# ============================================================================

def test_revl_ship_is_gated_as_the_swap_it_performs(live):
    """`revl_ship --apply` calls the `revl_swap` handler directly, so the
    dispatch-time gate never saw a swap under it."""
    bob = parse_profile("operator bob may snapshot on *").get("bob")
    session = _FakeSession(live, bob)

    rehearsal = decide(session, "revl_ship", {"source": TWO_REALM})
    assert rehearsal.gated is False, "the read-only rehearsal is not a mutation"

    applied = decide(session, "revl_ship", {"source": TWO_REALM, "apply": True})
    assert applied.gated and not applied.allowed
    assert applied.verb == "swap"


def test_revl_repair_is_gated_as_the_swap_it_performs(live):
    """The repair loop's remediation step calls `Session.swap` itself."""
    bob = parse_profile("operator bob may snapshot on *").get("bob")
    session = _FakeSession(live, bob)
    arguments = {"component": "TenantBCache",
                 "candidate": {"source": PATCHED}}

    rehearsal = decide(session, "revl_repair", {**arguments, "apply": False})
    assert rehearsal.gated is False, "the apply:false rehearsal is not a mutation"

    applied = decide(session, "revl_repair", arguments)  # apply defaults to true
    assert applied.gated and not applied.allowed
    assert applied.verb == "swap"
    # scoped to the component the repair replaces, not the whole composition
    assert "TenantBCache" in applied.message


def test_revl_repair_respects_an_enforced_lease(live, monkeypatch):
    """Observable at the transport: `revl_repair` over a component another
    operator leases is refused with the lease payload, before the loop runs."""
    bob = parse_profile("operator bob may swap on *").get("bob")
    session = _FakeSession(live, bob, enforce_leases=True)
    _leases._book(session).claim("TenantBCache", "alice", ttl=600)
    monkeypatch.setattr(server, "SESSION", session)

    payload = _call("revl_repair", {"component": "TenantBCache",
                                    "candidate": {"source": TWO_REALM}})
    assert payload["ok"] is False
    assert payload.get("swapped") is False
    assert payload["lease"] == {"component": "TenantBCache", "heldBy": "alice",
                                "expiry": payload["lease"]["expiry"],
                                "operator": payload["lease"]["operator"]}


# ============================================================================
# 4. the guards — the set is checked, not trusted
# ============================================================================

# Every advertised tool that is deliberately NOT gated by the operator profile,
# with the reason. A tool here reaches NONE of `Session.swap` / `.load` /
# `.unload` / `.restore` / `.rollback` / `.undo` / `.estop`, directly or through
# a handler it calls. Adding a tool to this table is a decision to be defended
# in review; the point of the table is that the decision has to be written down.
UNGATED = {
    # read-only compiles and static queries over agent-supplied source
    "revl_check": "compiles a candidate; loads nothing",
    "revl_admit": "compiles against the running manifest; mutates nothing",
    "revl_plan": "reports the delta a swap would produce; swaps nothing",
    "revl_audit": "reads the boundary of a candidate",
    "revl_canary": "compares two generations' recorded timelines; swaps nothing",
    "revl_gauntlet": "grades a candidate in a throwaway session",
    "revl_quarantine": "proves a candidate in the wasm substrate",
    "revl_resolve": "registry resolution",
    "revl_scaffold": "emits source text",
    "revl_fmt": "formats source text",
    "revl_explain": "renders a diagnostic",
    "revl_grammar": "returns the grammar",
    "revl_tools": "returns the tool surface",
    "revl_state": "reports session state",
    "revl_estop_report": "reports a halt that already happened",
    "revl_distillation_offers": "proposes rules; installs none",
    "revl_query_reach": "static query",
    "revl_query_dependents": "static query",
    "revl_query_emitters": "static query",
    "revl_query_drift": "static query",
    "revl_query_withdraw": "static query",
    "revl_live_query": "reads the live composition",
    "revl_history_emitted_between": "reads the recording",
    "revl_history_lifetime": "reads the recording",
    "revl_timeline": "reads the recording",
    "revl_inspect_step": "reads one recorded step",
    # session-plane verbs that move a cursor or a workspace rather than the
    # composition. They mutate SESSION state and are ungated today: the
    # operator profile's grammar has no verb for them, and minting one is a
    # policy decision for the profile's author, not a bug fix. Recorded here so
    # the choice is visible rather than implied by absence.
    "revl_call": "invokes a provided method; composes nothing new",
    "revl_lease": "claims/releases a lease in the workspace book",
    "revl_fork": "derives a fork hash; confirms nothing",
    "revl_fork_confirm": "rewinds and branches the session workspace",
    "revl_step_back": "moves the replay cursor",
    "revl_replay_forward": "moves the replay cursor",
    "revl_replay_bisect": "binary-searches the recording for a predicate flip",
}


def test_every_advertised_tool_is_gated_or_recorded_as_ungated():
    """The completeness guard. A new verb is either in `TOOL_VERB` /
    `COMPOSED_TOOL_VERB`, or named in `UNGATED` with the reason it does not
    need to be. A verb in neither fails here rather than shipping ungated."""
    advertised = {tool["name"] for tool in server.TOOLS}
    accounted = (set(_operator.TOOL_VERB)
                 | set(_operator.COMPOSED_TOOL_VERB) | set(UNGATED))
    assert not (advertised - accounted), (
        "these MCP verbs are neither gated by the operator profile nor "
        "recorded as deliberately ungated: "
        + ", ".join(sorted(advertised - accounted))
        + ". Gate it in operator.TOOL_VERB / COMPOSED_TOOL_VERB, or add it to "
          "UNGATED here with the reason it reaches no privileged operation.")
    assert not (accounted - advertised), (
        "these names are accounted for but no longer advertised: "
        + ", ".join(sorted(accounted - advertised)))


# `src/revl/mcp/` modules allowed to reach the compiler without a profile, with
# the reason each is safe. Everything else must pass `profile=` explicitly or
# route through `server.compile_under_authoring`.
COMPILER_DOOR_EXCEPTIONS = {
    "operator.py": "target derivation only — decides WHICH components an "
                   "action touches; lowers no host body and boots nothing, and "
                   "the source it compiles has already passed the pre-dispatch "
                   "authoring gate",
    "persist.py": "the snapshot re-admission; its agent-facing caller "
                  "(`revl_restore`) runs its own profiled decision compile "
                  "first (`server._restore_authoring_refusal`), and its other "
                  "caller is the operator's own `--restore`",
    "query_tools.py": "read-only static queries — compile only, never loads, "
                      "boots or swaps",
}

_COMPILER_ENTRY_POINTS = {"compile_source", "compile_files"}


def _unprofiled_compiler_calls(path: Path) -> list[str]:
    """Every `compile_source(...)` / `compile_files(...)` call in `path` that
    passes no `profile=` keyword, as `name:lineno` strings."""
    tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else \
            func.attr if isinstance(func, ast.Attribute) else None
        if name not in _COMPILER_ENTRY_POINTS:
            continue
        if any(kw.arg == "profile" for kw in node.keywords):
            continue
        out.append(f"{name}:{node.lineno}")
    return out


def test_every_mcp_compiler_call_goes_through_the_authoring_door():
    """The door guard.

    `server.compile_under_authoring` is the one place agent-supplied source
    meets the compiler with the session's authoring trust attached. A module
    that calls `compile_source` / `compile_files` itself gets NO trust, and a
    candidate the gate refuses is then compiled — and, for the verbs that boot,
    RUN. That is how the grading verbs became an admission-gate bypass.

    So: every compiler call under `src/revl/mcp/` either passes `profile=`
    explicitly or is in a module listed above with its reason. A new one fails
    here, at the door, instead of in an advisory."""
    offenders = {}
    for path in sorted((ROOT / "src" / "revl" / "mcp").glob("*.py")):
        if path.name in COMPILER_DOOR_EXCEPTIONS:
            continue
        calls = _unprofiled_compiler_calls(path)
        if calls:
            offenders[path.name] = calls
    assert not offenders, (
        "these MCP modules reach the compiler with no authoring trust: "
        + json.dumps(offenders)
        + ". Route them through `server.compile_under_authoring`, pass "
          "`profile=AUTHORING.profile()`, or record the module in "
          "COMPILER_DOOR_EXCEPTIONS with the reason it is safe.")


def test_the_door_guard_would_catch_a_new_unprofiled_call(tmp_path):
    """Non-vacuity for the guard itself: it must actually flag a bare call."""
    sample = tmp_path / "sample.py"
    sample.write_text("compile_source(src, 'x.rvl')\n"
                      "compile_files(paths, profile=p)\n", encoding="utf-8")
    assert _unprofiled_compiler_calls(sample) == ["compile_source:1"]
