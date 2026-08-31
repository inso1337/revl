"""Navigable refusals - the machine-facing map beside a policy deny (item 274).

Slice 1: the mechanism plus the two already-wired families (the taint sink,
249/G9, and the boundary policy, 33), with the two review findings baked in:

  * CRITICAL - under the untrusted-author profile every policy-family refusal
    collapses to ONE non-discriminating verdict, so an author cannot read the
    family/reason/proof back to reconstruct the operator's policy topology.
  * HIGH - a `clears-this-gate` proof marker never sits on a runtime-mutable
    (lease/ledger) predicate; such a predicate is `candidate` (TOCTOU).

The tests are the spec made executable (design §7).
"""

import pytest

from revl import RevlError
from revl import navigate as nav
from revl.admit_profile import AdmissionProfile
from revl.compiler import compile_source
from revl.diagnostics import classify, report
from revl.errors import RevlErrors
from revl.policy import evaluate, parse_policy
from revl.taint import TaintModel, _FlowChecker


# --------------------------------------------------------------- fixtures

_PRELUDE = (
    "extern emission[web] fn fetch(url: Str) -> Untrusted[Str] = @py { return \"\" }\n"
    "extern emission[shell] fn run(cmd: Trusted[Str]) = @py { return }\n"
)


def _agent(op: str, body: str, header: str = "") -> str:
    return (
        _PRELUDE
        + f"service Ops {{ {op} }}\n"
        + "component Agent provides ops: Ops {\n"
        + header
        + "  provide ops {\n"
        + f"    fn go(url) {{\n{body}\n    }}\n"
        + "  }\n"
        + "}\n"
    )


def _audit(name: str, token: str, *, file: str = "a.rvl") -> dict:
    """A minimal audit graph: one component reaching one named capability."""
    return {
        "boundary": {name: {"capabilities": {"send": [token]}}},
        "manifest": {"components": [{"name": name, "file": file}]},
    }


# ------------------------------------------ taint sink (249/G9): trusted view

def test_taint_sink_trusted_view_carries_navigate_with_endorse_form():
    """A web-origin value into a shell sink, no declassifier: the refusal
    carries a `navigate` naming the endorse form as the author path."""
    src = _agent(
        "emission fn go(url: Str)",
        "      let page = emit fetch(url)\n"
        "      emit run(page)")
    with pytest.raises(RevlError) as excinfo:
        compile_source(src, "sink.rvl")
    record = classify(excinfo.value)
    assert record["code"] == "G9"
    navrec = record["navigate"]
    assert navrec["family"] == "taint-sink"
    assert navrec["blocked"] is False
    actions = " ".join(a["action"] for a in navrec["alternatives"])
    assert "endorse[web]" in actions
    # the endorse slot is not granted here, so it is a candidate, not a promise.
    endorse_alt = [a for a in navrec["alternatives"] if "endorse" in a["action"]][0]
    assert endorse_alt["proof"] == nav.PROOF_CANDIDATE
    assert endorse_alt["enacts"] == nav.ENACTS_AUTHOR


def test_taint_sink_names_in_scope_declassifier_as_clears_this_gate():
    """A declassifier in scope and returning `Trusted[T]` clears THIS gate by
    construction - an immutable-at-refusal fact, so `clears-this-gate`."""
    model = TaintModel()
    model.declassifiers.add("wash")
    checker = _FlowChecker(model, "f.rvl", 1, endorse_allowed=frozenset())
    navrec = checker._sink_navigate("run", "a shell command", ["web"])
    washes = [a for a in navrec["alternatives"] if a.get("ref") == "wash"]
    assert washes and washes[0]["proof"] == nav.PROOF_CLEARS
    assert washes[0]["enacts"] == nav.ENACTS_AUTHOR


def test_taint_sink_granted_endorse_slot_clears_the_gate():
    """When the enclosing declaration already grants `endorse[web]`, the endorse
    form clears the gate; otherwise it is a candidate."""
    model = TaintModel()
    granted = _FlowChecker(model, "f.rvl", 1, endorse_allowed=frozenset({"web"}))
    rec = granted._sink_navigate("run", "a shell command", ["web"])
    endorse_alt = [a for a in rec["alternatives"] if a["ref"] == "endorse[web]"][0]
    assert endorse_alt["proof"] == nav.PROOF_CLEARS


# --------------------------------------------- boundary policy (33): trusted

def test_boundary_capability_trusted_view_enumerates_alternatives():
    policy = parse_policy("component Agent may reach kv\n")
    violations = evaluate(policy, _audit("Agent", "net"))
    assert violations
    navrec = violations[0].navigate
    assert navrec["family"] == "policy-capability"
    assert navrec["blocked"] is False
    by_enact = {a["enacts"] for a in navrec["alternatives"]}
    assert by_enact == {nav.ENACTS_AUTHOR, nav.ENACTS_OPERATOR}
    # dropping the reach removes the token from a static reach set -> clears.
    drop = [a for a in navrec["alternatives"] if "drop the reach" in a["action"]][0]
    assert drop["proof"] == nav.PROOF_CLEARS
    # the policy edit is operator-enacted and therefore always a candidate.
    edit = [a for a in navrec["alternatives"]
            if a["enacts"] == nav.ENACTS_OPERATOR][0]
    assert edit["proof"] == nav.PROOF_CANDIDATE


def test_boundary_deny_trusted_view():
    policy = parse_policy("component Agent may not reach net\n")
    violations = evaluate(policy, _audit("Agent", "net"))
    assert violations and violations[0].navigate["family"] == "policy-deny"
    alts = violations[0].navigate["alternatives"]
    assert any(a["enacts"] == nav.ENACTS_AUTHOR and a["proof"] == nav.PROOF_CLEARS
               for a in alts)
    assert any(a["enacts"] == nav.ENACTS_OPERATOR for a in alts)


# ----------------------------- CRITICAL: untrusted-author indistinguishability

def _untrusted():
    return AdmissionProfile.untrusted_author(["kv"])


def test_untrusted_author_matrix_is_mutually_indistinguishable():
    """A matrix of granted-service operations tripping different families under
    the untrusted profile yields BYTE-IDENTICAL navigate records: no family,
    reason, or proof discriminates which gate fired."""
    prof = _untrusted()
    records = []
    # boundary capability
    records.append(evaluate(parse_policy("component Agent may reach kv\n"),
                            _audit("Agent", "net"), profile=prof)[0].navigate)
    # boundary deny
    records.append(evaluate(parse_policy("component Agent may not reach net\n"),
                            _audit("Agent", "net"), profile=prof)[0].navigate)
    # a different component/token, same profile
    records.append(evaluate(parse_policy("component Bot may reach kv\n"),
                            _audit("Bot", "shell"), profile=prof)[0].navigate)
    # the taint-sink family, collapsed under the same profile
    tc = _FlowChecker(TaintModel(), "f.rvl", 1, untrusted=True)
    records.append(tc._sink_navigate("run", "a shell command", ["web"]))
    first = records[0]
    for r in records[1:]:
        assert r == first, "families must be indistinguishable under untrusted"
    assert first["family"] == nav.UNTRUSTED_FAMILY
    assert first["blocked"] is True
    assert first["alternatives"] == []
    assert "proof" not in repr(first) or first["alternatives"] == []


def test_redacted_operator_only_is_byte_identical_to_genuine_blocked():
    """A refusal whose only real alternatives were operator-enacted (now
    redacted) and a genuinely-blocked refusal are byte-identical under the
    untrusted profile - no dropped-then-empty vs genuinely-blocked one-bit
    tell."""
    prof = _untrusted()
    redacted_operator_only = evaluate(
        parse_policy("component Agent may not reach net\n"),
        _audit("Agent", "net"), profile=prof)[0].navigate
    genuine_blocked = nav.blocked_record(
        family="taint-sink", reason="anything", profile=prof)
    assert redacted_operator_only == genuine_blocked


def test_trusted_view_is_richer_than_untrusted_filter_does_work():
    """The filter is doing work, not the family forgetting to enumerate: the
    same refusal has a fuller enumeration on the trusted view."""
    audit = _audit("Agent", "net")
    trusted = evaluate(parse_policy("component Agent may reach kv\n"),
                       audit)[0].navigate
    untrusted = evaluate(parse_policy("component Agent may reach kv\n"),
                         audit, profile=_untrusted())[0].navigate
    assert len(trusted["alternatives"]) > len(untrusted["alternatives"])
    assert trusted["family"] != untrusted["family"]


# ----------------------------------- HIGH: clears-this-gate soundness sweep

def test_clears_this_gate_never_on_a_mutable_operand():
    """A predicate over a runtime-mutable operand (a lease/ceiling counter, a
    grant-ledger membership) is TOCTOU and must be `candidate`, never
    `clears-this-gate`, no matter what the caller requested."""
    live = nav.alternative(enacts=nav.ENACTS_AUTHOR, action="retry within the "
                           "remaining lease", clears=True, mutable_operand=True)
    assert live["proof"] == nav.PROOF_CANDIDATE
    assert live["live"] is True


def test_operator_and_runtime_approval_are_never_clears():
    """The self-mint invariant: an operator/runtime-approval alternative is
    never author-enactable and therefore never `clears-this-gate`."""
    for enacts in (nav.ENACTS_OPERATOR, nav.ENACTS_RUNTIME_APPROVAL):
        alt = nav.alternative(enacts=enacts, action="x", clears=True)
        assert alt["proof"] == nav.PROOF_CANDIDATE


def test_soundness_sweep_over_wired_families_finds_no_live_clears():
    """Over the wired families' trusted records, no `clears-this-gate` marker
    sits on a value flagged live (lease/ledger derived)."""
    recs = [
        evaluate(parse_policy("component Agent may reach kv\n"),
                 _audit("Agent", "net"))[0].navigate,
        evaluate(parse_policy("component Agent may not reach net\n"),
                 _audit("Agent", "net"))[0].navigate,
        _FlowChecker(TaintModel(), "f.rvl", 1)._sink_navigate(
            "run", "a shell command", ["web"]),
    ]
    for rec in recs:
        for alt in rec["alternatives"]:
            if alt.get("proof") == nav.PROOF_CLEARS:
                assert not alt.get("live"), "clears-this-gate on a live operand"


# ---------------------------------------------------- LOW: order stability

def test_alternatives_are_order_stable_and_gap_free():
    """Deterministic order (author, then operator); redaction removes items
    wholesale, so an untrusted list is empty rather than gapped."""
    navrec = evaluate(parse_policy("component Agent may reach kv\n"),
                      _audit("Agent", "net"))[0].navigate
    enacts_order = [nav.ENACTS_AUTHOR, nav.ENACTS_OPERATOR,
                    nav.ENACTS_RUNTIME_APPROVAL]
    seen = [enacts_order.index(a["enacts"]) for a in navrec["alternatives"]]
    assert seen == sorted(seen)
    # under untrusted the list is empty (wholesale drop), never a gap.
    untrusted = evaluate(parse_policy("component Agent may reach kv\n"),
                         _audit("Agent", "net"), profile=_untrusted())[0].navigate
    assert untrusted["alternatives"] == []


# ------------------------------------------------- byte-compat (MEDIUM-2)

def test_navigate_is_an_additive_json_key_only():
    """An error with no navigate serializes with no `navigate` key; one with it
    carries it. It is never rendered into the text path."""
    plain = RevlError("f.rvl", 1, "some message")
    assert "navigate" not in classify(plain)
    withnav = RevlError("f.rvl", 1, "m", navigate={"family": "taint-sink"})
    assert classify(withnav)["navigate"] == {"family": "taint-sink"}


def test_first_line_and_census_are_byte_identical_with_navigate():
    """navigate does not move the first line or the multi-error census render."""
    src = _agent(
        "emission fn go(url: Str)",
        "      let page = emit fetch(url)\n"
        "      emit run(page)")
    with pytest.raises(RevlError) as excinfo:
        compile_source(src, "sink.rvl")
    err = excinfo.value
    assert err.navigate is not None  # the refusal DID gain a navigate
    first_line = str(err).splitlines()[0]
    assert first_line.endswith("untrusted input cannot directly create "
                               "authority (G9)")
    # a lone RevlErrors carrier renders byte-identically to the one error.
    assert str(RevlErrors([err])) == str(err)
    # a two-error census still ends with the census line, unchanged by navigate.
    other = RevlError("b.rvl", 2, "second refusal")
    census = str(RevlErrors([err, other]))
    assert census.splitlines()[-1] == "2 refusals across 2 files"


def test_report_maps_navigate_over_the_multi_error_list():
    e1 = RevlError("a.rvl", 1, "one", navigate={"family": "policy-deny"})
    e2 = RevlError("b.rvl", 2, "two")
    doc = report(RevlErrors([e1, e2]))
    diags = doc["diagnostics"]
    assert diags[0]["navigate"] == {"family": "policy-deny"}
    assert "navigate" not in diags[1]
