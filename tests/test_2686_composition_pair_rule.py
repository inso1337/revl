"""Issue #2686, second slice: the composition-wide pair rule.

    composition may not reach fs.read together with network.call

The per-component pair rule (`test_2686_pair_capability_rule.py`) refuses ONE
component holding both capabilities. This rule refuses the whole COMPOSITION
holding both, over the union of every component's reach. How realms count: they
do not. Two realms holding X and Y separately never trip the per-component rule
and do trip this one.

These tests pin:

  * two components in different realms holding X and Y separately are admitted
    by the per-component rule and refused by the composition rule;
  * one component holding both is refused by both, and the composition refusal
    names that component for both capabilities;
  * a composition reaching only one of them is admitted;
  * the refusal names the rule, both capabilities and the component and chain
    that reaches each;
  * a malformed token is refused at parse time with file and line, in both
    spellings, and a realm subject stays refused;
  * the rule is a policy leg the recorded-history diff reports as unmodelled.
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
    CompositionPairRule, InertPairPolicyWarning, PolicyError, evaluate,
    parse_policy, unpredicted_rules,
)

COMPOSITION_POLICY = (
    "composition may not reach fs.read together with network.call")
COMPONENT_POLICY = (
    "component * may not reach fs.read together with network.call")

# Each component holds ONE side, each in its own realm: the case the
# per-component rule must admit and the composition rule must refuse.
SPLIT_REALMS = """
service Files { emission[fs.read] fn read(path: Str) -> Str }
service Net   { emission[network.call] fn call(url: Str) -> Str }

component ReaderJob requires files: Files {
  isolate files in realm("tenantA")
  emit files.read("notes")
}
component CallerJob requires net: Net {
  isolate net in realm("tenantB")
  emit net.call("https://example.test")
}
"""

# The same split with no realms at all: realms are not what decides it.
SPLIT_PLAIN = """
service Files { emission[fs.read] fn read(path: Str) -> Str }
service Net   { emission[network.call] fn call(url: Str) -> Str }

component ReaderJob requires files: Files {
  emit files.read("notes")
}
component CallerJob requires net: Net {
  emit net.call("https://example.test")
}
"""

# One component holds both.
SINGLE = """
service Files { emission[fs.read] fn read(path: Str) -> Str }
service Net   { emission[network.call] fn call(url: Str) -> Str }

component Exfiltrate requires files: Files, net: Net {
  emit files.read("notes")
  emit net.call("https://example.test")
}
component Bystander requires files: Files {
  emit files.read("other")
}
"""

# Only one side of the pair exists anywhere.
ONE_SIDE = """
service Files { emission[fs.read] fn read(path: Str) -> Str }

component ReaderA requires files: Files {
  emit files.read("a")
}
component ReaderB requires files: Files {
  emit files.read("b")
}
"""


def _audit(source: str) -> dict:
    return audit_report(compile_source(source))


# ------------------------------------------------------------- evaluation

def test_two_realms_holding_the_pair_separately_trip_only_the_composition_rule():
    audit = _audit(SPLIT_REALMS)
    # the per-component rule admits: neither component holds both
    assert evaluate(parse_policy(COMPONENT_POLICY), audit) == []
    # the composition rule refuses: the whole emission set holds both
    violations = evaluate(parse_policy(COMPOSITION_POLICY), audit)
    assert [v.kind for v in violations] == ["composition-pair"]
    v = violations[0]
    assert v.token == "fs.read + network.call"
    assert v.component == "ReaderJob"


def test_realms_do_not_change_the_composition_verdict():
    """The same split with and without realms is refused the same way."""
    refused = {
        name: [v.kind for v in evaluate(parse_policy(COMPOSITION_POLICY),
                                        _audit(source))]
        for name, source in (("realms", SPLIT_REALMS), ("plain", SPLIT_PLAIN))}
    assert refused == {"realms": ["composition-pair"],
                       "plain": ["composition-pair"]}


def test_the_diagnostic_names_the_rule_both_capabilities_and_each_chain():
    v = evaluate(parse_policy(COMPOSITION_POLICY), _audit(SPLIT_REALMS))[0]
    assert "the composition may not reach `fs.read` together with " \
           "`network.call`" in v.message
    assert "`fs.read` through `ReaderJob`" in v.message
    assert "`network.call` through `CallerJob`" in v.message
    # both reach chains are exhibits, each from its own component
    assert v.why.shape == "set"
    assert [step.name for step in v.why.steps] == [
        "ReaderJob", "fs.read", "CallerJob", "network.call"]
    rendered = v.render()
    assert "via emission `files.read`" in rendered
    assert "via emission `net.call`" in rendered


def test_a_single_component_holding_both_is_refused_by_both_rules():
    audit = _audit(SINGLE)
    component = evaluate(parse_policy(COMPONENT_POLICY), audit)
    assert [(v.kind, v.component) for v in component] == [
        ("pair", "Exfiltrate")]
    composition = evaluate(parse_policy(COMPOSITION_POLICY), audit)
    assert [(v.kind, v.component) for v in composition] == [
        ("composition-pair", "Exfiltrate")]
    # one holder: the same component is named for both capabilities
    assert "`fs.read` through `Exfiltrate`" in composition[0].message
    assert "`network.call` through `Exfiltrate`" in composition[0].message


def test_a_composition_reaching_only_one_side_is_admitted():
    policy = parse_policy(COMPOSITION_POLICY)
    with pytest.warns(InertPairPolicyWarning):
        assert evaluate(policy, _audit(ONE_SIDE)) == []


def test_one_token_matching_both_patterns_is_not_the_pair():
    """Two DISTINCT reached tokens are required: `fs.*` and `fs.read` both match
    the only reach there is, and one capability is not the forbidden pair."""
    policy = parse_policy("composition may not reach fs.* together with fs.read")
    with pytest.warns(InertPairPolicyWarning):
        assert evaluate(policy, _audit(ONE_SIDE)) == []


THREE_WAY = """
service Files { emission[fs.read] fn read(path: Str) -> Str }
service Logs  { emission[fs.append] fn append(line: Str) }
service Net   { emission[network.call] fn call(url: Str) -> Str }

component ReaderJob requires files: Files {
  emit files.read("notes")
}
component LogJob requires logs: Logs {
  emit logs.append("x")
}
component CallerJob requires net: Net {
  emit net.call("https://example.test")
}
"""


def test_the_union_spans_three_components_and_overlapping_globs():
    """`fs.*` matches both `fs.read` and `fs.append`, held by two different
    components, and `network.call` is held by a third. The composition rule
    finds the pair across the three (the first match in component order is
    `fs.read` via ReaderJob with `network.call` via CallerJob). Overlapping
    patterns split across components still need two DISTINCT tokens."""
    policy = parse_policy(
        "composition may not reach fs.* together with network.call")
    violations = evaluate(policy, _audit(THREE_WAY))
    assert [v.kind for v in violations] == ["composition-pair"]
    v = violations[0]
    assert v.token == "fs.read + network.call"
    assert "`fs.read` through `ReaderJob`" in v.message
    assert "`network.call` through `CallerJob`" in v.message
    # overlapping patterns split across components need two DISTINCT tokens
    overlap = parse_policy(
        "composition may not reach fs.* together with fs.read")
    tokens = {x.token for x in evaluate(overlap, _audit(THREE_WAY))}
    assert len(tokens) == 1
    assert set(next(iter(tokens)).split(" + ")) == {"fs.read", "fs.append"}


def test_both_rules_in_one_policy_fire_independently():
    policy = parse_policy(COMPONENT_POLICY + "\n" + COMPOSITION_POLICY)
    assert [v.kind for v in evaluate(policy, _audit(SINGLE))] == [
        "pair", "composition-pair"]
    assert [v.kind for v in evaluate(policy, _audit(SPLIT_REALMS))] == [
        "composition-pair"]


def test_an_inert_composition_rule_warns_naming_the_components():
    policy = parse_policy(COMPOSITION_POLICY)
    with pytest.warns(InertPairPolicyWarning) as caught:
        evaluate(policy, _audit(ONE_SIDE))
    message = str(caught[0].message)
    assert "refuses nothing" in message
    assert "ReaderA" in message and "ReaderB" in message
    assert "fs.read" in message and "network.call" in message


# ---------------------------------------------------------------- parsing

def test_the_composition_rule_parses_to_the_model():
    policy = parse_policy(COMPOSITION_POLICY)
    assert policy.composition_pair_rules == (
        CompositionPairRule("fs.read", "network.call"),)
    assert policy.pair_rules == ()
    assert not policy.is_empty()
    assert policy.composition_pair_rules[0].to_dsl() == COMPOSITION_POLICY


def test_the_json_and_dsl_forms_agree():
    dsl = parse_policy(COMPOSITION_POLICY)
    js = parse_policy(json.dumps({
        "compositionPairs": [{"together": ["fs.read", "network.call"]}]}))
    assert js.composition_pair_rules == dsl.composition_pair_rules


@pytest.mark.parametrize("line, token", [
    ("composition may not reach fs read together with network.call",
     "fs read"),
    ("composition may not reach fs.read together with network call",
     "network call"),
])
def test_a_malformed_token_is_refused_at_parse_with_file_and_line(line, token):
    with pytest.raises(PolicyError) as caught:
        parse_policy(line, "p.policy")
    message = str(caught.value)
    assert token in message, message
    assert "together with" in message, message
    assert caught.value.filename == "p.policy", message
    assert caught.value.line == 1, message


def test_the_parse_error_names_the_rule_line_it_refused():
    text = ("component Agent* may reach llm, kv\n"
            "# a comment between the rules\n"
            "composition may not reach fs.read together with network call\n")
    with pytest.raises(PolicyError) as caught:
        parse_policy(text, "p.policy")
    assert caught.value.line == 3, str(caught.value)
    assert "p.policy:3" in str(caught.value), str(caught.value)


def test_the_json_malformed_token_is_refused():
    with pytest.raises(PolicyError) as caught:
        parse_policy(json.dumps({
            "compositionPairs": [{"together": ["fs read", "network.call"]}]}),
            "p.policy")
    assert "fs read" in str(caught.value)
    assert caught.value.line == 1


def test_a_composition_pair_rule_needs_exactly_two_capabilities():
    with pytest.raises(PolicyError):
        parse_policy(json.dumps({
            "compositionPairs": [{"together": ["fs.read"]}]}))
    with pytest.raises(PolicyError):
        parse_policy("composition may not reach fs.read together with")
    with pytest.raises(PolicyError):
        parse_policy("composition may not reach together with network.call")


def test_the_composition_rule_has_no_allow_form():
    with pytest.raises(PolicyError) as caught:
        parse_policy(
            "composition may reach fs.read together with network.call")
    assert "may not reach" in str(caught.value)


def test_a_composition_rule_takes_no_component_selector():
    with pytest.raises(PolicyError) as caught:
        parse_policy(
            "composition Agent* may not reach fs.read together with "
            "network.call")
    assert "composition may not reach" in str(caught.value)


def test_a_predict_refusal_names_the_composition_rule_it_cannot_evaluate():
    assert unpredicted_rules(parse_policy(COMPOSITION_POLICY)) == [
        COMPOSITION_POLICY]


# ------------------------------------------------------------------- CLI

def test_the_cli_gate_refuses_a_split_composition(tmp_path, capsys):
    source = tmp_path / "split.rvl"
    source.write_text(SPLIT_REALMS, encoding="utf-8")
    policy_file = tmp_path / "split.policy"
    policy_file.write_text(COMPOSITION_POLICY + "\n", encoding="utf-8")
    assert main(["audit", str(source), "--policy", str(policy_file)]) == 1
    out = capsys.readouterr().out
    assert "ReaderJob" in out and "CallerJob" in out
    assert "fs.read" in out and "network.call" in out


def test_the_cli_gate_admits_the_split_under_the_per_component_rule(tmp_path):
    source = tmp_path / "split.rvl"
    source.write_text(SPLIT_REALMS, encoding="utf-8")
    policy_file = tmp_path / "split.policy"
    policy_file.write_text(COMPONENT_POLICY + "\n", encoding="utf-8")
    assert main(["audit", str(source), "--policy", str(policy_file)]) == 0
