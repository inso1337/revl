"""Confidence/evidence admission policy — item 290, Slice 1.

The `kind: evidence` policy rule is a HARD PREDICATE over the item-293 evidence
facets: a conjunction of thresholds, fail-closed, refuse-only, composed
worst-wins with the shipped rule families. These tests pin the deliverable:

  * a high-evidence registry component admits under `component registry:*
    requires evidence [...]`; a low-evidence one is refused naming the failing
    threshold; a first-party bare-source component is NOT refused by a
    `registry:*` rule (origin scoping);
  * the trust-root fix: a FORGED/copied dossier riding an honest, valid
    attestation is refused (its bytes do not hash to the signed binding);
  * an unrooted threshold is a `PolicyError` unless acknowledged;
  * `mcp requires evidence [gauntlet admissible]` is satisfiable now via the
    live session gauntlet dossier;
  * `requires register declared` admits; a higher floor is a parse error;
  * `revl policy evaluate` explains pass/fail with facts vs thresholds, names
    vacuous-vs-checked admission, and reports inert selectors;
  * an evidence-free policy is byte-identical to today.
"""

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import attest  # noqa: E402
from revl import registry as reg  # noqa: E402
from revl import compile_source  # noqa: E402
from revl.audit_diff import audit_report  # noqa: E402
from revl.policy import (  # noqa: E402
    PolicyError, evaluate, explain, parse_policy, render_explain,
)

KEY = b"evidence-policy-test-key-0123456789"
NOW = datetime(2025, 1, 1, tzinfo=timezone.utc)

# a two-component composition: one component we treat as registry-resolved
# (CsvReader), one as first-party bare source (LocalMain).
SRC = """
service Store { emission[db] fn put(key: Str, value: Str) }
component CsvReader requires store: Store {
  emit store.put("a", "b")
}
component LocalMain requires store: Store {
  emit store.put("c", "d")
}
"""

# a single-component source so the audited name is predictable for attestation.
SOLO = """
service Store { emission[db] fn put(key: Str, value: Str) }
component CsvReader requires store: Store {
  emit store.put("a", "b")
}
"""


def _audit(src):
    return audit_report(compile_source(src))


def _sweep(passed, steps, unreachable=0):
    return {"kind": "revl.fault-sweep", "status": "passed",
            "counts": {"steps": steps, "passed": passed,
                       "unreachable": unreachable}}


def _caps():
    return {"kind": "revl.capabilities", "boundary": {}}


def _solo_ir():
    return reg._normalize_ir_for_attest(compile_source(SOLO, "component.rvl"))


def _bundle(*, sweep, attest_bindings=True, forged_sweep=False,
            with_attest=True):
    """Build a bundle whose attestation binds the honest dossier hashes. When
    `forged_sweep`, the bundle carries a DIFFERENT sweep dossier than the one
    the attestation signed — the forged-dossier case (exit test 5)."""
    ir = _solo_ir()
    honest_sweep = _sweep(*sweep)
    caps = _caps()
    bindings = None
    if attest_bindings:
        bindings = {"fault-sweep": reg._facet_hash(honest_sweep),
                    "capabilities": reg._facet_hash(caps)}
    att = (attest.make_attestation(ir, KEY, now=NOW, evidence_bindings=bindings)
           if with_attest else None)
    carried = honest_sweep
    if forged_sweep:
        # same 12/12 claim, different bytes — will not hash to the binding.
        carried = _sweep(*sweep)
        carried["handwritten"] = True
    return reg.EvidenceBundle(attestation=att, fault_sweep=carried,
                              capabilities=caps)


ROOTED = "component registry:* requires evidence [attestation valid, fault-sweep full]"


# ------------------------------------------ high evidence admits, low refuses

def test_high_evidence_registry_component_admits():
    audit = _audit(SOLO)
    bundle = _bundle(sweep=(12, 12))
    violations = evaluate(
        parse_policy(ROOTED), audit,
        evidence={"CsvReader": bundle}, origins={"CsvReader": "registry"},
        key=KEY, evidence_ir={"CsvReader": _solo_ir()})
    assert violations == []


def test_low_evidence_registry_component_is_refused_naming_threshold():
    audit = _audit(SOLO)
    bundle = _bundle(sweep=(8, 12))          # partial sweep
    violations = evaluate(
        parse_policy(ROOTED), audit,
        evidence={"CsvReader": bundle}, origins={"CsvReader": "registry"},
        key=KEY, evidence_ir={"CsvReader": _solo_ir()})
    assert len(violations) == 1
    v = violations[0]
    assert v.kind == "evidence"
    assert "fault-sweep" in v.message
    assert "8/12" in v.message and "full" in v.message


def test_unreachable_steps_block_full():
    audit = _audit(SOLO)
    bundle = _bundle(sweep=(12, 12, 8))      # 12/12 but 8 unreachable
    violations = evaluate(
        parse_policy(ROOTED), audit,
        evidence={"CsvReader": bundle}, origins={"CsvReader": "registry"},
        key=KEY, evidence_ir={"CsvReader": _solo_ir()})
    assert any(v.kind == "evidence" and "unreachable" in v.message
               for v in violations)


def test_missing_evidence_is_refused_fail_closed():
    audit = _audit(SOLO)
    violations = evaluate(
        parse_policy(ROOTED), audit,
        evidence={}, origins={"CsvReader": "registry"},
        key=KEY, evidence_ir={})
    # both clauses fail: attestation unavailable, fault-sweep unavailable.
    facets = {v.token for v in violations}
    assert "attestation" in facets and "fault-sweep" in facets


# ------------------------------------------------- the trust-root fix (§6.2)

def test_forged_dossier_riding_a_valid_attestation_is_refused():
    """The load-bearing security fix: an honest, signed attestation cannot vouch
    for a hand-written fault-sweep whose bytes do not hash to the signed
    binding. The `fault-sweep full` clause passes on the forged 12/12 claim, but
    the rooted `attestation valid` clause grades the attestation INVALID."""
    audit = _audit(SOLO)
    bundle = _bundle(sweep=(12, 12), forged_sweep=True)
    violations = evaluate(
        parse_policy(ROOTED), audit,
        evidence={"CsvReader": bundle}, origins={"CsvReader": "registry"},
        key=KEY, evidence_ir={"CsvReader": _solo_ir()})
    assert len(violations) == 1
    v = violations[0]
    assert v.token == "attestation"
    assert "forged or copied" in v.message or "does not hash" in v.message


def test_keyless_attestation_valid_fails_closed():
    audit = _audit(SOLO)
    bundle = _bundle(sweep=(12, 12))
    # no key: an `attestation valid` clause cannot be verified, so it fails
    # (mirrors verify-required's keyless refusal), never a silent downgrade.
    violations = evaluate(
        parse_policy(ROOTED), audit,
        evidence={"CsvReader": bundle}, origins={"CsvReader": "registry"},
        key=None, evidence_ir={"CsvReader": _solo_ir()})
    assert any(v.token == "attestation" for v in violations)


# --------------------------------------------------- origin scoping (§3.2)

def test_registry_selector_does_not_refuse_first_party_bare_source():
    """`component registry:*` constrains only registry-resolved components; a
    first-party bare-source component in the same composition is not selected
    and admits."""
    audit = _audit(SRC)
    bundle = _bundle(sweep=(12, 12))
    violations = evaluate(
        parse_policy(ROOTED), audit,
        evidence={"CsvReader": bundle},
        origins={"CsvReader": "registry", "LocalMain": "source"},
        key=KEY, evidence_ir={"CsvReader": _solo_ir()})
    # CsvReader admits (high evidence); LocalMain is not selected at all.
    assert violations == []
    result = explain(
        parse_policy(ROOTED), audit,
        evidence={"CsvReader": bundle},
        origins={"CsvReader": "registry", "LocalMain": "source"},
        key=KEY, evidence_ir={"CsvReader": _solo_ir()})
    local = next(c for c in result["components"] if c["component"] == "LocalMain")
    assert "no evidence rule selects" in local["verdict"]


def test_bare_component_selector_selects_at_any_origin():
    """A bare `component *` selects by name regardless of origin — so it WOULD
    refuse a bare-source component with no evidence (the origin selector is what
    keeps first-party code writable)."""
    audit = _audit(SRC)
    policy = parse_policy(
        "component * requires evidence [attestation valid] ")
    violations = evaluate(
        policy, audit, evidence={},
        origins={"CsvReader": "registry", "LocalMain": "source"},
        key=KEY, evidence_ir={})
    refused = {v.component for v in violations}
    assert refused == {"CsvReader", "LocalMain"}


# ----------------------------------------------- unrooted thresholds (§6.3)

def test_unrooted_threshold_is_a_policy_error():
    with pytest.raises(PolicyError) as exc:
        parse_policy("component vendored-* requires evidence [fault-sweep full]")
    assert "self-attested" in str(exc.value)


def test_unrooted_threshold_with_acknowledgment_loads_and_warns_in_body():
    policy = parse_policy(
        "component vendored-* requires evidence [fault-sweep full] self-attested")
    assert policy.evidence_rules[0].self_attested
    # policy-level acknowledgment also works.
    policy2 = parse_policy(
        "evidence-root: local\n"
        "component vendored-* requires evidence [fault-sweep full]")
    assert policy2.evidence_root_local
    # the self-attested standing shows in the evaluate body.
    audit = _audit(SOLO)
    bundle = _bundle(sweep=(12, 12))
    p = parse_policy(
        "component CsvReader requires evidence [fault-sweep full] self-attested")
    result = explain(p, audit, evidence={"CsvReader": bundle},
                     origins={"CsvReader": "registry"})
    text = render_explain(result)
    assert "self-attested" in text


def test_attestation_valid_clause_roots_a_sweep_threshold():
    # a sweep threshold beside a binding-covering `attestation valid` clause is
    # rooted, so no acknowledgment is needed (§6.2).
    policy = parse_policy(
        "component registry:Csv* requires evidence "
        "[attestation valid, fault-sweep full]")
    assert policy.evidence_rules[0].unrooted_facets() == frozenset()


# ------------------------------------------------- MCP gauntlet plumbing (§4)

def test_mcp_gauntlet_clause_satisfied_by_session_dossier():
    audit = _audit(SOLO)
    policy = parse_policy("mcp requires evidence [gauntlet admissible]")
    dossier = {"verdict": "admissible"}
    bundle = reg.EvidenceBundle(gauntlet=dossier)
    # with the session dossier plumbed in, the mcp draft admits.
    violations = evaluate(policy, audit, mcp_components={"CsvReader"},
                          evidence={"CsvReader": bundle})
    assert violations == []
    # without a gauntlet run, the draft is refused with `gauntlet unavailable`.
    refused = evaluate(policy, audit, mcp_components={"CsvReader"}, evidence={})
    assert any(v.token == "gauntlet" and "unavailable" in v.message
               for v in refused)


def test_mcp_gauntlet_threshold_needs_no_attestation_root():
    # a gauntlet threshold under `mcp` is operator-run, so it is not a PolicyError
    # even without an attestation clause or acknowledgment.
    policy = parse_policy("mcp requires evidence [gauntlet admissible]")
    assert policy.evidence_rules[0].unrooted_facets() == frozenset()


# --------------------------------------------------- register floor (slice 1)

def test_requires_register_declared_admits():
    audit = _audit(SOLO)
    policy = parse_policy("capability db requires register declared")
    assert evaluate(policy, audit) == []


def test_requires_register_higher_floor_is_a_parse_error():
    for level in ("keyed", "shape-proven", "strong"):
        with pytest.raises(PolicyError) as exc:
            parse_policy(f"capability db requires register {level}")
        assert "not yet recordable" in str(exc.value)


# ----------------------------------------------- revl policy evaluate report

def test_evaluate_report_distinguishes_vacuous_from_checked():
    audit = _audit(SRC)
    bundle = _bundle(sweep=(12, 12))
    result = explain(
        parse_policy(ROOTED), audit,
        evidence={"CsvReader": bundle},
        origins={"CsvReader": "registry", "LocalMain": "source"},
        key=KEY, evidence_ir={"CsvReader": _solo_ir()})
    csv = next(c for c in result["components"] if c["component"] == "CsvReader")
    local = next(c for c in result["components"] if c["component"] == "LocalMain")
    assert "clause" in csv["verdict"] and "hold" in csv["verdict"]
    assert "no evidence rule selects" in local["verdict"]
    # the clause table names the fact vs the threshold.
    clauses = [c for r in csv["rules"] for c in r["clauses"]]
    assert any(c["facet"] == "fault-sweep" and c["pass"] for c in clauses)


def test_inert_selector_is_reported():
    audit = _audit(SOLO)
    # `component registry:Nope*` matches no component: inert.
    policy = parse_policy(
        "component registry:Nope* requires evidence [attestation valid]")
    result = explain(policy, audit, origins={"CsvReader": "registry"}, key=KEY)
    assert result["inertSelectors"]
    assert "selects no component" in render_explain(result)


# ------------------------------------------------------- additivity (§CRITICAL)

def test_evidence_free_policy_is_byte_identical():
    audit = _audit(SRC)
    policy = parse_policy("component * may reach db")
    # evaluate with and without the new (empty) evidence inputs agree.
    base = evaluate(policy, audit)
    grown = evaluate(policy, audit, evidence={}, origins={},
                     trusted_publishers=frozenset(), key=None, evidence_ir={})
    assert [v.message for v in base] == [v.message for v in grown]
    assert base == []
