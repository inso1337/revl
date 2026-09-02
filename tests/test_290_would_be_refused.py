"""Item 290 Slice 2: the resolve-side `wouldBeRefused` marker (design §5).

The marker is a COURTESY PREDICTION so an agent does not pick a top-ranked
candidate the gate then bounces. Everything here is about keeping it from
becoming something else. The two failure directions, and the tests that pin
them:

* it must never REFUSE. `resolve` must return the same candidates, in the same
  order, with and without a policy — a policy that predicts a refusal for every
  candidate withholds none of them.
* it must never read as an APPROVAL. Absence of the marker is not a clearance:
  realm-, capability- and mcp-scoped rules, the `requires register` floors and
  the `requires idempotent-teardown` floor all select on the assembled
  composition, so they are named in `policyPreview.unpredicted` instead of being
  reported as passing, and the block carries the caveat in words.

Agreement with the gate is structural, not asserted twice: `predict_refusals`
routes through `_rule_reports`, the same single comparison site `evaluate` and
`revl policy evaluate` read (§7), so the clause verdicts cannot drift. The test
below pins the agreement anyway, over a bundle the gate refuses.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import registry  # noqa: E402
from revl.policy import (  # noqa: E402
    evaluate,
    parse_policy,
    predict_refusals,
    unpredicted_rules,
)

REGISTRY_DIR = ROOT / "registry"

DATABASE_NEED = """
service Store {
  fn query(sql: Str) -> List[Row]
  emission fn execute(sql: Str) -> Int
}
"""

# Every registry entry ships a fault-sweep dossier and none ships an
# attestation, so this rule refuses all of them and the fault-sweep one refuses
# none — the two ends of the prediction, over the committed fixtures.
REFUSING = "component registry:* requires evidence [attestation valid]\n"
PASSING = ("component registry:* requires evidence [fault-sweep full] "
           "self-attested\n")


def _reg() -> registry.Registry:
    return registry.Registry.from_dir(REGISTRY_DIR)


def _write_policy(text: str) -> str:
    import tempfile
    path = Path(tempfile.mkdtemp()) / "boundary.policy"
    path.write_text(text, encoding="utf-8")
    return str(path)


def _names(result: dict) -> list[str]:
    return [c["name"] for c in result["candidates"]]


# ------------------------------------------------- it never refuses

def test_the_marker_withholds_nothing_and_reorders_nothing():
    """The load-bearing property: prediction is not filtering."""
    plain = _reg().resolve(DATABASE_NEED)
    marked = _reg().resolve(DATABASE_NEED, policy=parse_policy(REFUSING))
    # every candidate is refused by the policy ...
    assert all("wouldBeRefused" in c for c in marked["candidates"])
    # ... and every one of them is still returned, in the same order.
    assert _names(marked) == _names(plain)
    assert marked["refused"] == plain["refused"]


def test_no_policy_leaves_the_answer_untouched():
    """Byte-identity: the marker is opt-in, so an agent that passes no policy
    sees exactly today's resolve — no new keys to misread."""
    plain = _reg().resolve(DATABASE_NEED)
    assert "policyPreview" not in plain
    assert all("wouldBeRefused" not in c for c in plain["candidates"])


# ------------------------------------------------- it never approves

def test_a_clean_candidate_carries_no_clearance():
    """A candidate the predictable rules do not refuse gets NO key at all — not
    an empty list, which would read as "the gate cleared this one"."""
    marked = _reg().resolve(DATABASE_NEED, policy=parse_policy(PASSING))
    assert marked["candidates"], "fixture regressed: no candidates to mark"
    assert all("wouldBeRefused" not in c for c in marked["candidates"])
    assert marked["policyPreview"]["predicted"] == []


def test_rules_the_prediction_cannot_decide_are_named_not_dropped():
    """The three composition-scoped evidence families, the register floor and
    the teardown floor are all reported as UNPREDICTED. Reporting any of them as
    "not selected" would be the false clearance: their selection depends on the
    realm placement, the G8 reach and the audit graph's registers, none of which
    a candidate standing alone has."""
    policy = parse_policy(
        REFUSING
        + "realm tenant-a requires evidence [fault-sweep full] self-attested\n"
        + "mcp requires evidence [gauntlet admissible]\n"
        + "capability db.* requires register keyed\n"
        + "requires idempotent-teardown(strength: keyed)\n")
    unpredicted = unpredicted_rules(policy)
    assert unpredicted == [
        "realm tenant-a requires evidence [fault-sweep full] self-attested",
        "mcp requires evidence [gauntlet admissible]",
        "capability db.* requires register keyed",
        "requires idempotent-teardown(strength: keyed)",
    ]
    # the component-scoped rule is the one that IS predicted, so it is not here.
    assert not any(u.startswith("component ") for u in unpredicted)

    preview = _reg().resolve(DATABASE_NEED, policy=policy)["policyPreview"]
    assert preview["unpredicted"] == unpredicted
    # and the block says out loud what its own silence does not mean.
    assert "ABSENCE is not an admission" in preview["caveat"]


def test_a_capability_register_floor_is_never_predicted_from_a_claim():
    """The register floors read the audit graph's per-token registers. A
    registry entry carries only its publisher's `index.json` claim about its
    capabilities — the one input §5's assumption list says is not cross-checked
    — so predicting from it would be predicting from the thing the gate refuses
    to trust. It is unpredicted, and no marker mentions it."""
    policy = parse_policy("capability db.* requires register shape-proven\n")
    result = _reg().resolve(DATABASE_NEED, policy=policy)
    assert result["policyPreview"]["unpredicted"] == [
        "capability db.* requires register shape-proven"]
    assert all("wouldBeRefused" not in c for c in result["candidates"])


# ------------------------------------------------- it agrees with the gate

def test_the_prediction_matches_what_the_gate_says_about_the_same_facts():
    """One comparison site (§7): the marker's facet, threshold and recorded fact
    are the gate's own, so the dry-run and the gate cannot disagree on a fact
    both can see."""
    from revl.audit_diff import audit_report
    from revl.compiler import compile_source

    entry = next(e for e in _reg().entries if e.name == "pg_database")
    ir = compile_source(entry.source, "component.rvl")
    audit = audit_report(ir)
    name = next(iter(audit["boundary"]))

    policy = parse_policy(REFUSING)
    predicted = predict_refusals(
        policy, name, evidence_bundle=entry.evidence_bundle,
        evidence_ir=registry._normalize_ir_for_attest(ir))["wouldBeRefused"]

    violations = evaluate(
        policy, audit, evidence={name: entry.evidence_bundle},
        origins={name: "registry"},
        evidence_ir={name: registry._normalize_ir_for_attest(ir)})

    assert [p["facet"] for p in predicted] == [
        v.token for v in violations if v.kind == "evidence"]
    assert predicted and predicted[0]["fact"] == "unavailable"
    assert predicted[0]["threshold"] == "valid"


def test_the_assumption_list_states_the_one_sidedness():
    result = _reg().resolve(DATABASE_NEED, policy=parse_policy(REFUSING))
    joined = " ".join(result["assumptions"])
    assert "never refuses" in joined
    assert "absence is not an admission" in joined


# ------------------------------------------------- the agent-facing seam

def test_the_mcp_resolve_verb_threads_the_policy_through():
    """`revl_resolve` is the surface an agent actually calls, so the marker has
    to reach it — the whole point of §5 is that the agent sees the prediction
    before it picks."""
    from revl.mcp.server import _tool_resolve

    policy_file = _write_policy(REFUSING)
    result = _tool_resolve({"need": DATABASE_NEED,
                            "registry": str(REGISTRY_DIR),
                            "policy": policy_file})
    assert result["ok"] is True
    assert all("wouldBeRefused" in c for c in result["candidates"])
    assert result["policyPreview"]["predicted"] == _names(result)


def test_an_unreadable_policy_stops_the_call_rather_than_predicting_nothing():
    """The one place the marker's plumbing DOES refuse, and it is not a policy
    verdict: a policy that cannot be loaded means no prediction was made, and
    handing back an unmarked candidate list would be exactly the false clearance
    the marker exists to prevent."""
    from revl.mcp.server import _tool_resolve

    missing = _tool_resolve({"need": DATABASE_NEED,
                             "registry": str(REGISTRY_DIR),
                             "policy": str(REGISTRY_DIR / "no-such.policy")})
    assert missing["ok"] is False
    assert "candidates" not in missing

    malformed = _tool_resolve(
        {"need": DATABASE_NEED, "registry": str(REGISTRY_DIR),
         "policy": _write_policy(
             "component registry:* requires evidence [nope full]\n")})
    assert malformed["ok"] is False
    assert "candidates" not in malformed


def test_the_glob_is_matched_against_the_same_name_the_gate_uses():
    """An evidence rule selects on the COMPONENT name from the candidate's own
    compiled IR (`PgDatabase`), not the registry ENTRY name it is listed under
    (`pg_database`) — the same subject `revl policy evaluate --registry
    --candidate` resolves. A preview that matched the entry name would predict
    refusals the gate never issues, and miss the ones it does."""
    by_component = _reg().resolve(
        DATABASE_NEED,
        policy=parse_policy(
            "component registry:Pg* requires evidence [attestation valid]\n"))
    marked = [c["name"] for c in by_component["candidates"]
              if "wouldBeRefused" in c]
    assert marked == ["pg_database"]

    by_entry = _reg().resolve(
        DATABASE_NEED,
        policy=parse_policy(
            "component registry:pg_* requires evidence [attestation valid]\n"))
    assert all("wouldBeRefused" not in c for c in by_entry["candidates"])
