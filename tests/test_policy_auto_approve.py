"""The distilled `AutoApproveRule` policy form (roadmap item 251, Slice 1).

Slice 1 is PARSE-ONLY: the rule is added to `Policy` and round-trips through both
the DSL and JSON forms, `admitting secret-taint` is refused, and no evaluation is
wired (the runtime consume path is Slice 2). These tests pin exactly that.
"""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.policy import (  # noqa: E402
    AutoApproveRule, Policy, PolicyError, TAINT_FOLD_ORIGINS, parse_policy,
)


def _only_rule(text: str) -> AutoApproveRule:
    policy = parse_policy(text)
    assert len(policy.auto_approve_rules) == 1, policy.auto_approve_rules
    return policy.auto_approve_rules[0]


# --------------------------------------------------------------- round-trips

MINIMAL = ("component billing:* may auto-approve "
           "gateway.send(host=\"api.stripe.com\") in realm billing")

FULL = ("component a* may auto-approve fs.write(path=\"/var/spool\"), kv.get "
        "in realm ops admitting net-taint, web-taint ttl 30s uses 5")


@pytest.mark.parametrize("text", [
    MINIMAL,
    FULL,
    "component svc may auto-approve kv.get",
    "component b:* may auto-approve db.write(table=\"ledger\") admitting fs-taint",
    "component c may auto-approve kv.get uses 3",
    "component d may auto-approve kv.get ttl 1h",
])
def test_dsl_round_trips(text):
    """A hand-written rule parses, re-renders, and re-parses to the SAME rule -
    the round-trip Slice 1 guarantees (parse -> serialize -> parse)."""
    rule = _only_rule(text)
    again = _only_rule(rule.to_dsl())
    assert again == rule, (rule, again)


@pytest.mark.parametrize("text", [MINIMAL, FULL,
                                  "component svc may auto-approve kv.get"])
def test_json_round_trips(text):
    """The rule renders to a JSON entry that re-parses to the same rule, and the
    DSL and JSON forms agree."""
    rule = _only_rule(text)
    doc = json.dumps({"autoApprovals": [rule.to_json()]})
    from_json = parse_policy(doc).auto_approve_rules[0]
    assert from_json == rule, (rule, from_json)


def test_canonical_capability_spelling():
    """Two spellings of one resource cone canonicalize equal, so a hand-written
    rule and the distiller's projection of the same cone compare equal."""
    a = _only_rule('component x may auto-approve fs.write(path="/tmp/job")')
    b = _only_rule('component x may auto-approve fs.write( path = "/tmp/job" )')
    assert a == b


def test_fields_parsed():
    rule = _only_rule(FULL)
    assert rule.component == "a*"
    assert rule.caps == ('fs.write(path="/var/spool")', "kv.get")
    assert rule.realm == "ops"
    assert rule.admitting == frozenset({"net", "web"})
    assert rule.ttl_ms == 30_000
    assert rule.uses == 5


# ------------------------------------------------ secret is never admit-able

@pytest.mark.parametrize("text", [
    "component a may auto-approve kv.get admitting secret-taint",
    "component a may auto-approve kv.get admitting web-taint, secret-taint",
])
def test_dsl_rejects_admitting_secret(text):
    """`secret` is enforced by G-SECRET at the crossing, structurally never an
    admitted origin - the parser refuses it rather than silently accept an origin
    the gate excludes (design §2.1, §3.2)."""
    with pytest.raises(PolicyError, match="secret"):
        parse_policy(text)


def test_json_rejects_admitting_secret():
    doc = json.dumps({"autoApprovals": [
        {"component": "a", "capabilities": ["kv.get"], "admitting": ["secret"]}]})
    with pytest.raises(PolicyError, match="secret"):
        parse_policy(doc)


def test_secret_not_in_the_five_origins():
    assert "secret" not in TAINT_FOLD_ORIGINS
    assert TAINT_FOLD_ORIGINS == frozenset({"web", "net", "fs", "model", "input"})


def test_negative_guarantee_is_the_complement():
    rule = _only_rule("component a may auto-approve kv.get admitting web-taint")
    assert rule.negative_guarantee() == frozenset({"net", "fs", "model", "input"})
    bare = _only_rule("component a may auto-approve kv.get")
    assert bare.negative_guarantee() == TAINT_FOLD_ORIGINS


# --------------------------------------------------------------- parse errors

@pytest.mark.parametrize("text,match", [
    ("component a may auto-approve", "at least one capability"),
    ("may auto-approve kv.get", "one component glob"),
    ("component a may auto-approve kv.get admitting bogus-taint", "admitting"),
    ("component a may auto-approve kv.get uses many", "positive integer"),
    ("component a may auto-approve kv.get(nope=\"x\")", "malformed"),
])
def test_malformed_rules_are_policy_errors(text, match):
    with pytest.raises(PolicyError, match=match):
        parse_policy(text)


# ------------------------------------------------------- additive / no regress

def test_empty_policy_has_no_auto_approve_rules():
    assert Policy().auto_approve_rules == ()
    assert Policy().is_empty()


def test_auto_approve_rule_makes_policy_non_empty():
    assert not parse_policy(MINIMAL).is_empty()


def test_existing_policy_forms_unaffected():
    """A policy with no auto-approve line parses byte-identically (the field
    defaults empty)."""
    policy = parse_policy("component Agent* may reach llm, kv*\n"
                          "tenants never reach each other")
    assert policy.auto_approve_rules == ()
    assert policy.tenants_isolated
