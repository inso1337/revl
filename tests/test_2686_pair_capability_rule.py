"""Issue #2686: a policy rule over two capabilities held TOGETHER.

The existing reach rules constrain one capability at a time (a deny-list, a
closed allow-list, a taint flow) or two components (`tenants never reach each
other`). None of them can say what an operator actually wants to refuse here:
each of `fs.read` and `network.call` is fine alone, but no single component may
hold BOTH. `component Agent* may not reach fs.read together with network.call`
is that statement.

These tests pin the deliverable:

  * a component reaching both is refused, and the diagnostic names the rule,
    the component and both capabilities with the chain that reaches each;
  * the same component reaching only one of them is admitted;
  * a rule with a malformed token is refused at parse time with file and line;
  * the JSON form parses to the same policy;
  * a well-formed pair that never occurs in one component is reported inert
    rather than read as a clean pass (the issue-#1984 precedent).
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
    InertPairPolicyWarning, PairRule, PolicyError, evaluate, parse_policy,
    unpredicted_rules,
)

# `AgentExfiltrate` holds both capabilities; the other two hold one each, so
# the same policy admits them while refusing the pair in one component.
PAIR = """
service Files { emission[fs.read] fn read(path: Str) -> Str }
service Net   { emission[network.call] fn call(url: Str) -> Str }

component AgentReader requires files: Files {
  emit files.read("notes")
}
component AgentCaller requires net: Net {
  emit net.call("https://example.test")
}
component AgentExfiltrate requires files: Files, net: Net {
  emit files.read("notes")
  emit net.call("https://example.test")
}
"""

PAIR_POLICY = "component Agent* may not reach fs.read together with network.call"


def _audit(source: str = PAIR) -> dict:
    return audit_report(compile_source(source))


# ------------------------------------------------------------- evaluation

def test_a_component_holding_both_capabilities_is_refused():
    violations = evaluate(parse_policy(PAIR_POLICY), _audit())
    assert [v.kind for v in violations] == ["pair"]
    v = violations[0]
    assert v.component == "AgentExfiltrate"
    assert v.token == "fs.read + network.call"
    # the message names the rule, the component and both capabilities
    assert "component" in v.message
    assert "`Agent*`" in v.message
    assert "`AgentExfiltrate`" in v.message
    assert "`fs.read`" in v.message
    assert "`network.call`" in v.message
    assert "together with" in v.message


def test_the_diagnostic_names_the_chain_that_reaches_each_capability():
    v = evaluate(parse_policy(PAIR_POLICY), _audit())[0]
    # both reach chains are exhibits, not a single path
    assert v.why.shape == "set"
    assert [step.name for step in v.why.steps] == [
        "AgentExfiltrate", "fs.read", "AgentExfiltrate", "network.call"]
    rendered = v.render()
    assert "via emission `files.read`" in rendered
    assert "via emission `net.call`" in rendered


def test_components_reaching_only_one_of_the_pair_are_admitted():
    violations = evaluate(parse_policy(PAIR_POLICY), _audit())
    refused = {v.component for v in violations}
    assert refused == {"AgentExfiltrate"}, refused
    # selected by the same `Agent*` rule, but each holds one side only
    assert "AgentReader" not in refused
    assert "AgentCaller" not in refused


def test_a_rule_bound_to_a_one_sided_component_refuses_nothing():
    policy = parse_policy(
        "component AgentReader may not reach fs.read together with network.call")
    with pytest.warns(InertPairPolicyWarning):
        assert evaluate(policy, _audit()) == []


def test_one_token_satisfying_both_patterns_is_one_capability_not_a_pair():
    """The pair is two DISTINCT reached tokens. `fs.*` and `fs.read` both match
    `AgentReader`'s only reach, and one capability is not the forbidden pair."""
    policy = parse_policy(
        "component AgentReader may not reach fs.* together with fs.read")
    with pytest.warns(InertPairPolicyWarning):
        assert evaluate(policy, _audit()) == []


def test_a_pair_rule_that_never_meets_in_one_component_warns():
    """The fail-open issue #1984 closed for deny-lists, for the pair form: the
    patterns match reaches somewhere, but never in one component."""
    policy = parse_policy(
        "component AgentReader may not reach fs.read together with network.call")
    with pytest.warns(InertPairPolicyWarning) as caught:
        evaluate(policy, _audit())
    message = str(caught[0].message)
    assert "refuses nothing" in message
    assert "AgentReader" in message
    assert "fs.read" in message and "network.call" in message


# ---------------------------------------------------------------- parsing

def test_the_pair_rule_parses_to_the_model():
    policy = parse_policy(PAIR_POLICY)
    assert policy.pair_rules == (PairRule("Agent*", "fs.read", "network.call"),)
    assert not policy.is_empty()
    assert policy.pair_rules[0].to_dsl() == PAIR_POLICY


def test_the_json_and_dsl_forms_agree():
    dsl = parse_policy(PAIR_POLICY)
    js = parse_policy(json.dumps({
        "pairs": [{"component": "Agent*",
                   "together": ["fs.read", "network.call"]}]}))
    assert js.pair_rules == dsl.pair_rules


@pytest.mark.parametrize("line, token", [
    ("component Agent* may not reach fs read together with network.call",
     "fs read"),
    ("component Agent* may not reach fs.read together with network call",
     "network call"),
])
def test_a_malformed_pair_token_is_refused_at_parse(line, token):
    with pytest.raises(PolicyError) as caught:
        parse_policy(line, "p.policy")
    message = str(caught.value)
    assert token in message, message
    assert "capability" in message, message
    assert "may not reach" in message, message
    assert "together with" in message, message
    assert caught.value.filename == "p.policy", message
    assert caught.value.line == 1, message


def test_the_parse_error_names_the_rule_line_it_refused():
    text = ("component Agent* may reach llm, kv\n"
            "# a comment between the rules\n"
            "component Agent* may not reach fs.read together with network call\n")
    with pytest.raises(PolicyError) as caught:
        parse_policy(text, "p.policy")
    assert caught.value.line == 3, str(caught.value)
    assert "p.policy:3" in str(caught.value), str(caught.value)


def test_the_json_malformed_token_is_refused():
    with pytest.raises(PolicyError) as caught:
        parse_policy(json.dumps({
            "pairs": [{"component": "Agent*",
                       "together": ["fs read", "network.call"]}]}), "p.policy")
    assert "fs read" in str(caught.value)
    assert caught.value.line == 1


def test_a_pair_rule_needs_exactly_two_capabilities():
    with pytest.raises(PolicyError):
        parse_policy(json.dumps({
            "pairs": [{"component": "Agent*", "together": ["fs.read"]}]}))
    with pytest.raises(PolicyError):
        parse_policy("component Agent* may not reach fs.read together with")


def test_the_composition_wide_form_is_not_part_of_this_slice():
    """A realm-scoped pair rule needs a decision on how realms count, so it is
    refused rather than silently read as per-component."""
    with pytest.raises(PolicyError) as caught:
        parse_policy(
            "realm billing may not reach fs.read together with network.call")
    assert "component <glob>" in str(caught.value)
    assert "not part of this slice" in str(caught.value)


def test_the_pair_rule_has_no_allow_form():
    with pytest.raises(PolicyError) as caught:
        parse_policy(
            "component Agent* may reach fs.read together with network.call")
    assert "may not reach" in str(caught.value)


def test_a_predict_refusal_names_the_pair_rule_it_cannot_evaluate():
    assert unpredicted_rules(parse_policy(PAIR_POLICY)) == [PAIR_POLICY]


# ------------------------------------------------------------------- CLI

def test_the_cli_gate_refuses_the_pair(tmp_path, capsys):
    source = tmp_path / "pair.rvl"
    source.write_text(PAIR, encoding="utf-8")
    policy_file = tmp_path / "pair.policy"
    policy_file.write_text(PAIR_POLICY + "\n", encoding="utf-8")
    code = main(["audit", str(source), "--policy", str(policy_file)])
    assert code == 1
    out = capsys.readouterr().out
    assert "AgentExfiltrate" in out
    assert "fs.read" in out and "network.call" in out
