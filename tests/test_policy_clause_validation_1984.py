"""Issue #1984 — a capability token that cannot match must not be an inert rule.

`_split_caps` split a capability list on COMMAS and validated nothing, so a
`may not reach` line carrying a typo or a trailing clause became ONE token with
whitespace inside it (`['mail.send except the send kit']`). Such a token can
never match a capability, so the rule denied nothing — with no diagnostic at
parse time and none at enforce time. The harm is asymmetric: an allow-list that
matches nothing refuses everything (fail closed), while a DENY-list that matches
nothing admits everything (fail open), so a policy an operator believed was
protecting a boundary was silently a no-op.

The fix validates every capability token against the capability grammar at PARSE
time — the same closed-vocabulary rule `_parse_facet_clause` already applies to
evidence facets — and warns at enforce time when a WELL-FORMED deny pattern
matches no reach of the components it selects (`mail.sedn`, which is
capability-shaped and so cannot be caught at parse time).

These tests pin both directions:

  * every spelling that cannot be a capability glob is a `PolicyError` naming
    the offending token and the rule's location — never a silently inert rule;
  * every legitimate spelling keeps working (bare and prefixed wildcards,
    namespaced/dotted tokens, comma-separated lists, realm and mcp subjects);
  * a comma-separated deny-list still denies BOTH of its tokens.
"""

import sys
import warnings
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402
from revl.audit_diff import audit_report  # noqa: E402
from revl.policy import PolicyError, evaluate, parse_policy  # noqa: E402

# A component that reaches two dotted capabilities, so a comma-separated
# deny-list has something to deny on both sides.
AGENT = """
service Mail  { emission[mail.send]  fn send(account: Str) -> Str }
service Shell { emission[shell.run]  fn run(command: Str) -> Str }

component Agent requires mail: Mail, shell: Shell {
  emit shell.run("ls")
  emit mail.send("ops")
}
"""


@pytest.fixture(scope="module")
def audit() -> dict:
    return audit_report(compile_source(AGENT))


def _reached(audit: dict) -> set[str]:
    from revl.policy import component_reach
    return {r.token for r in component_reach(audit, "Agent")}


# ------------------------------------------------- the fail-open, one line each

# (the line as an operator writes it, the offending token it must name)
REFUSED = (
    ("component * may not reach mail.send except the send kit",
     "mail.send except the send kit"),
    ("component * may not reach mail.sedn because a typo",
     "mail.sedn because a typo"),
    ("component * may not reach mail.send shell.run",
     "mail.send shell.run"),
    ("component Agent* may reach llm kv*", "llm kv*"),
    ("web-taint may not reach mail.send shell.run", "mail.send shell.run"),
    ("component * may not reach mail.send, shell run", "shell run"),
)


@pytest.mark.parametrize("line, token", REFUSED)
def test_a_token_that_cannot_be_a_capability_is_refused_at_parse(line, token):
    """Each of these had a deny-list (or allow-list) that matched nothing.

    Before the fix all six parsed, and every deny-shaped one ADMITTED a
    composition that really crossed the boundary it named.
    """
    with pytest.raises(PolicyError) as caught:
        parse_policy(line, "p.policy")
    message = str(caught.value)
    assert token in message, message                    # names the token
    assert "capability" in message, message             # names what it is not
    assert "may [not] reach" in message, message        # names the rule form
    assert caught.value.filename == "p.policy", message
    assert caught.value.line == 1, message


def test_the_error_names_the_rule_line_it_refused():
    """A location is what turns a diagnostic into an edit: line 3 is the rule."""
    text = ("component Agent* may reach llm, kv\n"
            "# a comment between the rules\n"
            "component * may not reach mail.send except the send kit\n")
    with pytest.raises(PolicyError) as caught:
        parse_policy(text, "p.policy")
    assert caught.value.line == 3, str(caught.value)
    assert "p.policy:3" in str(caught.value), str(caught.value)


def test_an_except_clause_is_refused_and_names_the_supported_mechanism():
    """The clause an operator naturally writes is prose, not a capability.

    The reach grammar has nowhere to put a carve-out, so the rule cannot be
    honoured — the error says so and names the mechanism that can express an
    exception (`capability <glob> requires approval`, roadmap item 246).
    """
    with pytest.raises(PolicyError) as caught:
        parse_policy("component * may not reach mail.send except the send kit",
                     "p.policy")
    message = str(caught.value)
    assert "except" in message, message
    assert "mail.send except the send kit" in message, message
    assert "requires approval" in message, message


def test_the_refusal_is_not_a_bare_shape_check():
    """`mail.sedn` is capability-SHAPED, so parse cannot catch it — the
    enforce-time warning is what keeps it from being silently inert."""
    policy = parse_policy("component * may not reach mail.sedn", "p.policy")
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        violations = evaluate(policy, audit_report(compile_source(AGENT)))
    assert violations == []
    inert = [w for w in caught if "denies nothing" in str(w.message)]
    assert len(inert) == 1, [str(w.message) for w in caught]
    assert type(inert[0].message).__name__ == "InertDenyPolicyWarning"
    assert "mail.sedn" in str(inert[0].message)


# ------------------------------------------------------------ no over-refusal

POSITIVE = (
    "component * may not reach *",                       # the unbounded token
    "component * may not reach mail.send",               # dotted
    "component * may not reach shell.run",               # dotted
    "component * may not reach ops.post",                # dotted, never reached
    "component * may not reach mail.*",                  # prefixed wildcard
    "component * may not reach mail.send*",              # suffixed wildcard
    "component * may not reach kv*",
    "component * may not reach llm*",
    "component * may not reach [mn]ail.send",            # a glob character set
    "component * may not reach mail.send, shell.run",    # comma-separated list
    "component * may not reach mail.send , shell.run",   # ... with whitespace
    "realm billing may not reach fs.write",              # realm subject
    "component Agent* may reach llm, kv",                # allow-list
    "mcp may reach llm, kv*",                            # mcp sandbox subject
    "web-taint may not reach mail.send, shell.run",      # taint-flow rule
    "web-taint may not reach mail.send without approval",
    "component * may not declassify web, net",           # a separate vocabulary
)


@pytest.mark.parametrize("line", POSITIVE)
def test_every_legitimate_spelling_still_parses(line):
    assert parse_policy(line, "p.policy") is not None


def test_a_comma_separated_deny_list_still_denies_both_tokens(audit):
    """The positive control: the list that always worked keeps working."""
    assert _reached(audit) == {"mail.send", "shell.run"}
    policy = parse_policy("component * may not reach mail.send, shell.run",
                          "p.policy")
    assert policy.rules[0].patterns == ("mail.send", "shell.run")
    assert {v.token for v in evaluate(policy, audit)} == {"mail.send", "shell.run"}


def test_a_deny_that_matches_does_not_warn(audit):
    """The warning is for a rule that denies nothing — not for every deny."""
    policy = parse_policy("component * may not reach mail.send", "p.policy")
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        assert [v.token for v in evaluate(policy, audit)] == ["mail.send"]
    assert [w for w in caught if "denies nothing" in str(w.message)] == []
