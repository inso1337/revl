"""The pure approval distiller (roadmap item 251, Slice 1).

The distiller folds a list of item-248 ledger records to typed offers and typed
"cannot distill" verdicts, all pure data, applying NO policy. These tests pin:

  * a settled repeated BARE-token shape distills to a rule with an honest blast
    radius and the correct negative-guarantee complement over the five origins;
  * the resource-scoped shape key (§1.2): a single recorded host distills to a
    host-scoped rule; sibling `path=` cones join to their common ancestor;
  * every fail-closed / refusal path returns its FIRST-CLASS typed reason:
    resource-scope-unrecorded, taint-unknown, varying-scope, below-threshold,
    mixed-operator, had-denial.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.distill import (  # noqa: E402
    DistilledOffer, Reason, blast_radius, distill,
)
from revl.policy import AutoApproveRule, TAINT_FOLD_ORIGINS  # noqa: E402


def grant(cap, session, *, operator="op1", realm="billing",
          component="billing:invoice", **extra):
    """One `approval-granted` ledger record (the item-248 shape)."""
    return {"record": "approval-granted", "component": component,
            "session": session, "operator": operator, "realm": realm,
            "classCCapabilities": [cap], **extra}


def _settled(cap, **extra):
    """Five grants over three sessions, one operator, zero denials - a shape that
    passes every §1.3 clause."""
    return [grant(cap, f"s{i % 3}", **extra) for i in range(6)]


def _reasons(result):
    return {r.reason for r in result.refusals}


# --------------------------------------------------- the bare-token happy path

def test_settled_bare_token_shape_distills():
    result = distill(_settled("kv.get"))
    assert len(result.offers) == 1
    assert not result.refusals
    offer = result.offers[0]
    assert isinstance(offer, DistilledOffer)
    assert offer.rule.caps == ("kv.get",)
    assert offer.rule.realm == "billing"
    assert offer.operator == "op1"
    assert offer.grant_count == 6
    assert set(offer.sessions) == {"s0", "s1", "s2"}


def test_bare_token_negative_guarantee_is_all_five():
    """An untainted bare-token rule admits no origin, so it can never approve any
    of the five - the negative guarantee is the whole set."""
    offer = distill(_settled("kv.get")).offers[0]
    assert offer.blast.negative_guarantee == TAINT_FOLD_ORIGINS
    assert offer.rule.admitting == frozenset()


def test_component_glob_folds_multiple_components():
    recs = [grant("kv.get", f"s{i % 3}",
                  component=("billing:invoice" if i % 2 else "billing:refund"))
            for i in range(6)]
    offer = distill(recs).offers[0]
    assert offer.rule.component == "billing:*"


def test_recorded_taint_becomes_the_admitting_set():
    offer = distill(_settled("gateway.send", taintOrigins=["web"])).offers[0]
    assert offer.rule.admitting == frozenset({"web"})
    assert offer.blast.negative_guarantee == frozenset(
        {"net", "fs", "model", "input"})


# --------------------------------------------------- the resource-scoped key

def _host(host):
    return {"gateway.send": f'gateway.send(host="{host}")'}


def test_single_recorded_host_distills_with_host_scope():
    recs = _settled("gateway.send", resourceScopes=_host("api.stripe.com"))
    offer = distill(recs).offers[0]
    assert offer.rule.caps == ('gateway.send(host="api.stripe.com")',)
    assert offer.blast.resource_scope == 'gateway.send(host="api.stripe.com")'


def test_sibling_path_cones_join_to_the_common_ancestor():
    recs = [grant("fs.write", f"s{i % 3}",
                  resourceScopes={"fs.write": f'fs.write(path="/var/spool/{i}")'})
            for i in range(6)]
    offer = distill(recs).offers[0]
    assert offer.rule.caps == ('fs.write(path="/var/spool")',)


# --------------------------------------------------- the typed refusals (§1.3)

def test_resource_scope_unrecorded_fails_closed():
    """A capability carrying a `_REGISTRY` resource order but no recorded
    valuation cannot name its destination - Slice 1 fails closed."""
    recs = _settled("gateway.send", resourceScopes={"gateway.send": None})
    result = distill(recs)
    assert not result.offers
    assert _reasons(result) == {Reason.RESOURCE_SCOPE_UNRECORDED}


def test_taint_unknown_fails_closed():
    """A taint-relevant crossing with no recorded taint has no honest floor for
    the negative guarantee - Slice 1 fails closed."""
    recs = _settled("gateway.send", taintRelevant=True)
    result = distill(recs)
    assert not result.offers
    assert _reasons(result) == {Reason.TAINT_UNKNOWN}


def test_varying_scope_refuses_rather_than_widen():
    """Grants spanning distinct hosts have no single scope an operator could
    narrow to, so the distiller refuses rather than emit the wider host-free
    rule (the N1 fix as a trigger clause)."""
    recs = [grant("gateway.send", f"s{i % 3}",
                  resourceScopes=_host(f"h{i}.example"))
            for i in range(6)]
    result = distill(recs)
    assert not result.offers
    assert _reasons(result) == {Reason.VARYING_SCOPE}


def test_below_threshold_too_few_grants():
    result = distill([grant("kv.get", "s0"), grant("kv.get", "s1")])
    assert _reasons(result) == {Reason.BELOW_THRESHOLD}


def test_below_threshold_single_session():
    """Five grants but one session - a within-session repetition item 344 already
    handles never by itself triggers a persistent policy change."""
    recs = [grant("kv.get", "s0") for _ in range(6)]
    result = distill(recs)
    assert _reasons(result) == {Reason.BELOW_THRESHOLD}


def test_mixed_operator_refuses():
    recs = [grant("kv.get", f"s{i % 3}", operator=("a" if i < 3 else "b"))
            for i in range(6)]
    result = distill(recs)
    assert not result.offers
    assert _reasons(result) == {Reason.MIXED_OPERATOR}


def test_had_denial_refuses():
    denial = {"record": "approval-denied", "realm": "billing",
              "classCCapabilities": ["kv.get"], "taintOrigins": []}
    result = distill(_settled("kv.get") + [denial])
    assert not result.offers
    assert _reasons(result) == {Reason.HAD_DENIAL}


def test_refusal_renders_a_human_reason():
    result = distill(_settled("gateway.send", resourceScopes={"gateway.send": None}))
    text = result.refusals[0].render()
    assert "cannot distill" in text
    assert "resource-scope-unrecorded" in text


# ------------------------------------------------------ the blast-radius fold

def test_blast_radius_partitions_the_window_with_reasons():
    """The fold reports the covered count and, for each grant it would NOT have
    covered, the reason it fell out (§3.1): a taint origin excluded, a realm out
    of scope, a component outside the glob, a resource value outside the scope."""
    rule = AutoApproveRule("billing:*", ('gateway.send(host="api.stripe.com")',),
                           realm="billing")
    window = (
        _settled("gateway.send", resourceScopes=_host("api.stripe.com"))
        + [grant("gateway.send", "s9", resourceScopes=_host("evil.example")),
           grant("gateway.send", "s9", realm="ops",
                 resourceScopes=_host("api.stripe.com")),
           grant("gateway.send", "s9", taintOrigins=["web"],
                 resourceScopes=_host("api.stripe.com"))]
    )
    blast = blast_radius(rule, window)
    assert blast.covered == 6
    reasons = {nc.reason for nc in blast.not_covered}
    assert reasons == {"resource", "realm", "taint"}


def test_blast_radius_negative_guarantee_over_five_origins():
    rule = AutoApproveRule("billing:*", ("gateway.send",), realm="billing",
                           admitting=frozenset({"web"}))
    blast = blast_radius(rule, _settled("gateway.send", taintOrigins=["web"]))
    assert blast.negative_guarantee == frozenset({"net", "fs", "model", "input"})


def test_blast_fold_host_scoped_rule_excludes_other_hosts():
    """The N1 guard: a host-scoped rule never counts a send to another host as
    covered (`cap_order.covers` refuses the sideways move)."""
    rule = AutoApproveRule("billing:*", ('gateway.send(host="api.stripe.com")',),
                           realm="billing")
    same = blast_radius(rule, _settled("gateway.send",
                                       resourceScopes=_host("api.stripe.com")))
    other = blast_radius(rule, _settled("gateway.send",
                                        resourceScopes=_host("evil.example")))
    assert same.covered == 6 and not same.not_covered
    assert other.covered == 0
    assert all(nc.reason == "resource" for nc in other.not_covered)


def test_blast_fold_empty_admission_taint_floors_to_all_five():
    """The H2 enforcement floor: a taint-RELEVANT crossing whose admission taint
    set is empty is treated as ALL FIVE origins (fail-closed), so it is NOT
    waved through a `{} subset admitting` test by an untainted rule."""
    rule = AutoApproveRule("billing:*", ("gateway.send",), realm="billing")
    relevant_empty = _settled("gateway.send", taintRelevant=True, taintOrigins=[])
    blast = blast_radius(rule, relevant_empty)
    assert blast.covered == 0
    assert all(nc.reason == "taint" for nc in blast.not_covered)
    # a genuinely non-taint-relevant crossing still distills (no over-refusal).
    clean = blast_radius(rule, _settled("gateway.send"))
    assert clean.covered == 6
