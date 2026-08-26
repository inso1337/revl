"""Boundary policy — the third leg of the gate (roadmap item 33).

Where admission checks *correctness* (running consumers stay valid) and
`audit --diff` checks *drift* (a regeneration did not widen), the boundary
policy checks *absolute authority*: does any admitted component reach a
capability it may not? Evaluation is pure set operations over the same G8
audit graph the other two legs read (`revl.audit_diff.audit_report`).

These tests pin the deliverable:

  * an allowed composition admits (no violation);
  * a component reaching outside its allow-list is refused, with the chain
    named in the why-trace;
  * a denied capability is refused;
  * a cross-tenant reach is refused (`tenants never reach each other`);
  * the MCP-sandbox profile refuses an over-reaching agent component;
  * the CLI `audit --policy` gate returns nonzero on a violation, zero clean;
  * the JSON and DSL policy forms agree.
"""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402
from revl.__main__ import main  # noqa: E402
from revl.audit_diff import audit_report  # noqa: E402
from revl.policy import (  # noqa: E402
    Policy, PolicyError, evaluate, load_policy, parse_policy,
)

FIXTURES = ROOT / "tests" / "fixtures"

AGENTS = """
service LLM { emission[llm] fn ask(prompt: Str) -> Str }
service KV  { emission[kv]  fn put(key: Str, value: Str) }
extern emission fn sendEmail(to: Str) = @py { pass }

component AgentSummarize requires llm: LLM, kv: KV {
  emit llm.ask("summarize this")
  emit kv.put("summary", "done")
}
component AgentLeak requires llm: LLM {
  emit llm.ask("draft an email")
  emit sendEmail("victim@example.com")
}
"""

TENANTS = """
service Bus { emission[bus] fn publish(message: Str) }
component TenantAJob requires bus: Bus {
  isolate bus in realm("tenantA")
  emit bus.publish("a-event")
}
component TenantBJob requires bus: Bus {
  isolate bus in realm("tenantB")
  emit bus.publish("b-event")
}
"""


def _audit(source: str) -> dict:
    return audit_report(compile_source(source))


# ----------------------------------------------------------- allow / deny

def test_allowed_composition_admits():
    audit = _audit(AGENTS)
    # both agents may reach the union of what either actually reaches
    policy = parse_policy("component Agent* may reach llm, kv, sendEmail")
    assert evaluate(policy, audit) == []


def test_reach_outside_allow_list_is_refused_with_chain_named():
    audit = _audit(AGENTS)
    policy = parse_policy("component Agent* may reach llm, kv")
    violations = evaluate(policy, audit)
    assert len(violations) == 1
    v = violations[0]
    assert v.kind == "capability"
    assert v.component == "AgentLeak"
    assert v.token == "sendEmail"
    # the why-trace names the violating chain: component -> boundary reached
    path = v.why.path()
    assert path == ["AgentLeak", "sendEmail"]
    assert "sendEmail" in v.render()


def test_a_named_deny_refuses_even_when_otherwise_allowed():
    audit = _audit(AGENTS)
    # everything is allowed, but sendEmail is explicitly denied
    policy = parse_policy(
        "component Agent* may reach llm, kv, sendEmail\n"
        "component Agent* may not reach sendEmail")
    violations = evaluate(policy, audit)
    assert [v.kind for v in violations] == ["deny"]
    assert violations[0].token == "sendEmail"


def test_a_summarize_only_agent_stays_within_its_allow_list():
    audit = _audit(AGENTS)
    # a rule bound to the summarizer alone; the leaker is unconstrained here
    policy = parse_policy("component AgentSummarize may reach llm, kv")
    violations = evaluate(policy, audit)
    # AgentSummarize reaches only llm+kv, so it passes; AgentLeak matches no
    # allow rule, so it is open — no violation from this policy
    assert violations == []


# --------------------------------------------------------------- unbounded

def test_an_unbounded_reach_never_satisfies_a_named_allow_list():
    # a bare `emission` (no scope) reaches `*` — an unnameable boundary
    source = """
    service Raw { emission fn go(x: Str) }
    component Wild requires raw: Raw { emit raw.go("x") }
    """
    audit = _audit(source)
    policy = parse_policy("component Wild may reach llm, kv")
    violations = evaluate(policy, audit)
    assert len(violations) == 1
    assert violations[0].token == "*"
    # but an allow-list that literally contains `*` accepts it
    assert evaluate(parse_policy("component Wild may reach *"), audit) == []


# ---------------------------------------------------------------- tenants

def test_a_cross_tenant_reach_is_refused():
    audit = _audit(TENANTS)
    policy = parse_policy("tenants never reach each other")
    violations = evaluate(policy, audit)
    assert len(violations) == 1
    v = violations[0]
    assert v.kind == "tenant"
    assert v.token == "bus"
    # the chain names both tenants and the boundary they share
    assert v.why.path() == ["TenantAJob", "bus", "TenantBJob"]


def test_isolated_tenants_with_disjoint_reach_admit():
    # give each tenant its own boundary — no shared token, no violation
    source = """
    service Bus { emission[bus] fn publish(m: Str) }
    service Log { emission[log] fn write(m: Str) }
    component TenantAJob requires bus: Bus {
      isolate bus in realm("tenantA")
      emit bus.publish("a")
    }
    component TenantBJob requires log: Log {
      isolate log in realm("tenantB")
      emit log.write("b")
    }
    """
    audit = _audit(source)
    assert evaluate(parse_policy("tenants never reach each other"), audit) == []


# --------------------------------------------------------- the MCP sandbox

def test_mcp_sandbox_refuses_an_over_reaching_agent_component():
    audit = _audit(AGENTS)
    policy = parse_policy("mcp may reach llm, kv")
    everyone = frozenset(audit["boundary"])
    violations = evaluate(policy, audit, mcp_components=everyone)
    assert len(violations) == 1
    v = violations[0]
    assert v.kind == "mcp-sandbox"
    assert v.component == "AgentLeak"
    assert v.token == "sendEmail"
    assert "agent-sandbox" in v.message


def test_mcp_sandbox_admits_an_in_profile_agent_component():
    audit = _audit(AGENTS)
    policy = parse_policy("mcp may reach llm, kv")
    # only the in-profile summarizer is admitted through the session
    violations = evaluate(policy, audit, mcp_components={"AgentSummarize"})
    assert violations == []


def test_mcp_block_is_inert_outside_the_mcp_admission_path():
    # with no component marked MCP-admitted, the sandbox constrains nobody
    audit = _audit(AGENTS)
    policy = parse_policy("mcp may reach llm, kv")
    assert evaluate(policy, audit) == []


# ----------------------------------------------------------- parse / forms

def test_json_and_dsl_policy_forms_agree():
    audit = _audit(AGENTS)
    dsl = parse_policy("component Agent* may reach llm, kv")
    js = parse_policy(json.dumps(
        {"components": [{"pattern": "Agent*", "allow": ["llm", "kv"]}]}))
    assert [v.token for v in evaluate(dsl, audit)] == \
           [v.token for v in evaluate(js, audit)]


def test_a_malformed_policy_line_is_a_clear_error():
    try:
        parse_policy("component Agent* can maybe reach llm")
    except PolicyError as exc:
        assert "unrecognised" in str(exc).lower()
    else:  # pragma: no cover
        raise AssertionError("a malformed policy line should raise PolicyError")


def test_load_policy_from_a_file():
    policy = load_policy(str(FIXTURES / "agent_sandbox.policy"))
    assert isinstance(policy, Policy)
    assert policy.mcp_allow == ("llm", "kv")


# ------------------------------------------------------------------- CLI

def test_cli_policy_gate_refuses_a_leaking_composition():
    code = main(["audit", str(FIXTURES / "policy_agents.rvl"),
                 "--policy", str(FIXTURES / "agent_sandbox.policy")])
    assert code == 1


def test_cli_policy_gate_passes_a_clean_composition(tmp_path):
    policy_file = tmp_path / "ok.policy"
    policy_file.write_text("component Agent* may reach llm, kv, sendEmail\n")
    code = main(["audit", str(FIXTURES / "policy_agents.rvl"),
                 "--policy", str(policy_file)])
    assert code == 0


def test_cli_policy_json_names_the_violating_chain(capsys):
    code = main(["audit", str(FIXTURES / "policy_agents.rvl"),
                 "--policy", str(FIXTURES / "agent_sandbox.policy"), "--json"])
    assert code == 1
    out = json.loads(capsys.readouterr().out)
    assert out["refused"] is True
    tokens = {v["token"] for v in out["violations"]}
    assert "sendEmail" in tokens
    leak = next(v for v in out["violations"] if v["token"] == "sendEmail")
    assert "AgentLeak" in leak["why"]["path"]


def test_cli_mcp_scope_star_applies_the_sandbox_everywhere():
    code = main(["audit", str(FIXTURES / "policy_agents.rvl"),
                 "--policy", str(FIXTURES / "agent_sandbox.policy"),
                 "--mcp-scope", "*"])
    assert code == 1


def test_cli_cross_tenant_reach_is_refused(tmp_path):
    policy_file = tmp_path / "tenants.policy"
    policy_file.write_text("tenants never reach each other\n")
    code = main(["audit", str(FIXTURES / "policy_tenants.rvl"),
                 "--policy", str(policy_file)])
    assert code == 1


# -------------------------------------------------- MCP session sandbox

def test_mcp_session_load_refuses_an_over_reaching_draft():
    """The sandbox profile as a machine-checked admission invariant: a session
    with a sandbox set refuses to load an over-reaching composition, before any
    runtime is touched (no cordis needed to decide it)."""
    from revl.mcp.session import Session, SessionError

    session = Session()
    session.sandbox = parse_policy("mcp may reach llm, kv")
    ir = compile_source(AGENTS)
    try:
        session.load(ir)
    except SessionError as exc:
        assert "agent-sandbox" in str(exc)
        assert "sendEmail" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("the sandbox should have refused admission")
    finally:
        # nothing loaded, since the refusal preceded the runtime
        assert not session.loaded


def test_mcp_session_without_a_sandbox_is_unaffected():
    """No sandbox set -> the enforcement is inert (the pre-item-33 behaviour)."""
    from revl.mcp.session import Session

    session = Session()
    assert session.sandbox is None
    # _enforce_sandbox is a no-op; it must not raise on any ir
    session._enforce_sandbox(compile_source(AGENTS))


# ---------------------------------------------------------------------------
# item 246, Slice 2: `capability C requires approval [ttl D]`
# ---------------------------------------------------------------------------

def test_requires_approval_dsl_and_json_agree():
    dsl = parse_policy(
        "capability production.payment requires approval\n"
        "capability prod.* requires approval ttl 10m\n")
    js = parse_policy(json.dumps({"approvals": [
        {"capability": "production.payment"},
        {"capability": "prod.*", "ttl": "10m"}]}))
    assert dsl.approval_rules == js.approval_rules
    assert dsl.requires_approval()
    # the tightest rule that covers a token is returned, with its ttl in ms
    assert dsl.approval_rule_for("production.payment").ttl_ms is None
    assert dsl.approval_rule_for("prod.payment").ttl_ms == 600_000
    assert dsl.approval_rule_for("unrelated") is None


def test_requires_approval_ttl_units_and_malformed():
    assert parse_policy("capability c requires approval ttl 30s") \
        .approval_rules[0].ttl_ms == 30_000
    assert parse_policy("capability c requires approval ttl 500ms") \
        .approval_rules[0].ttl_ms == 500
    assert parse_policy("capability c requires approval ttl 45") \
        .approval_rules[0].ttl_ms == 45_000       # a bare number is seconds
    with pytest.raises(PolicyError):
        parse_policy("capability c requires approval ttl notaduration")
    with pytest.raises(PolicyError):
        parse_policy("capability a, b requires approval")   # one glob only


def test_requires_approval_leaves_a_plain_policy_empty():
    # a policy with only allow/deny rules names no approval requirement
    assert not parse_policy("component A* may reach llm").requires_approval()
