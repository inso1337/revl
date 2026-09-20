"""The model decision as a signed evidence object (roadmap item 517, issue
#1191; `docs/design/536-model-decision-evidence.md`).

The exit clause this file is written against: a recorded run replays a model
decision from the artifact alone, and an edited field fails its digest.

What is checked here, and why each one is not vacuous:

  * **Every covered member, tampered, one at a time.** The mutation set is
    GENERATED from a sealed record's own keys rather than written out by hand,
    so a member added to the body later is covered without anyone remembering
    to cover it. That is the anti-regression for the exact bug this module
    cites: a signed receipt in this tree once had `key_id` appended to its body
    AFTER the MAC was taken over a hand-written field list, so the one member
    every reader used to choose a key was the one member nothing covered. The
    hand-written-list version of this test would have passed on that receipt.
  * **An added member and a removed member**, not only an edited one. The
    `key_id` bug was an ADDITION, and a MAC taken over a field list is blind to
    exactly that.
  * **A narrow body signed with a valid key.** `verify` is not allowed to be a
    MAC check with decoration: a record sealed by another implementation that
    holds the same key and omitted members is the case the verifier exists for,
    and it must refuse.
  * **The disclosure rules**, which restate `revl_prompt_digest`'s fail-closed
    gate (item 121 §4) at the evidence layer.
  * **Two controls that hold on BOTH trees**: `revl.attest` round-trips
    unchanged, and its `key_id` fingerprint is unchanged. Nothing here weakens
    the attestation path it borrows canonicalization from.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import attest, model_evidence as me, wal  # noqa: E402
from revl.taint import _ORIGIN_CLASSES  # noqa: E402

KEY = b"an evidence signing secret"
OTHER_KEY = b"a different signing secret"

D = me.digest


def _members(**overrides):
    """A well-formed, non-disclosing decision. Overrides replace members."""
    body = dict(
        component="Classifier",
        step_index=7,
        role="local",
        residence="on_device",
        model_digest=D("nanojev-0.4-weights"),
        placement_digest=D("device profile: m4, 4-bit, 2.1GB"),
        prompt_binding={"mode": "content-addressed",
                        "value": D("classify this ticket"), "reason": None},
        origins=["input", "web"],
        candidates=[D("candidate a"), D("candidate b"), D("candidate c")],
        chosen=1,
        outcome="validated",
        sampling={"temperature": 0.2, "top_p": 0.95, "top_k": 40,
                  "seed": 20260919, "max_tokens": 512, "stop_digest": None},
        policy_digest=D("route model on classify { confidential -> local }"),
        fallback_depth=0,
    )
    body.update(overrides)
    return body


def sealed(**overrides):
    return me.seal(KEY, **_members(**overrides))


# ---------------------------------------------------------------------------
# the well-formed record
# ---------------------------------------------------------------------------

def test_a_well_formed_record_verifies():
    verdict = me.verify(sealed(), KEY)
    assert verdict.ok, verdict.reason
    assert verdict.link == ""
    assert bool(verdict) is True


def test_the_record_carries_every_member_the_item_names():
    record = sealed()
    # the issue's own list: model hash, prompt hash, candidate set, sampling
    # parameters, input origins, the policy in force, the fallback depth.
    for member in ("model_digest", "prompt_binding", "candidates", "sampling",
                   "origins", "policy_digest", "fallback_depth"):
        assert member in record
    # plus design 531 §9's placement: role name and residence, by value.
    assert record["role"] == "local"
    assert record["residence"] == "on_device"
    assert set(record) == set(me.BODY_MEMBERS) | {me.SIGNATURE_FIELD}


def test_sealing_is_deterministic_for_one_body():
    body = _members()
    first = me.seal(KEY, recorded_at="2026-09-19T00:00:00+00:00", **body)
    second = me.seal(KEY, recorded_at="2026-09-19T00:00:00+00:00", **body)
    assert first == second


# ---------------------------------------------------------------------------
# tamper rejection — GENERATED from the record's own keys
# ---------------------------------------------------------------------------

def _a_different_value(member, value):
    """Some value of the same broad shape that is not `value`."""
    if isinstance(value, bool):
        return not value
    if isinstance(value, int):
        return value + 1
    if isinstance(value, float):
        return value + 0.5
    if isinstance(value, str):
        return (D(value + "!") if me._HEX64.match(value)
                else value + " (edited)")
    if isinstance(value, list):
        return value[1:] if value else [D("injected")]
    if isinstance(value, dict):
        edited = dict(value)
        key = sorted(edited)[0]
        edited[key] = _a_different_value(key, edited[key])
        return edited
    if value is None:
        return D("no longer absent")
    raise AssertionError(f"no mutation written for {member}={value!r}")


@pytest.mark.parametrize("member", sorted(me.BODY_MEMBERS))
def test_every_covered_member_edited_fails_the_signature(member):
    """The roadmap's exit clause, once per member.

    The parametrisation is over `BODY_MEMBERS`, so this test grows with the
    body. It is the shape that would have caught the `key_id`-after-the-MAC
    receipt, and `key_id` is one of the members it runs on.
    """
    record = sealed()
    assert me.verify(record, KEY).ok
    tampered = dict(record)
    tampered[member] = _a_different_value(member, record[member])
    assert tampered != record, f"{member} was not actually mutated"
    verdict = me.verify(tampered, KEY)
    assert not verdict.ok
    assert verdict.link == me.EVIDENCE_SIGNATURE, (
        f"editing {member!r} was not caught by the MAC: {verdict}")


@pytest.mark.parametrize("member", sorted(me.BODY_MEMBERS))
def test_every_covered_member_removed_fails_the_signature(member):
    record = sealed()
    tampered = {k: v for k, v in record.items() if k != member}
    verdict = me.verify(tampered, KEY)
    assert not verdict.ok
    assert verdict.link == me.EVIDENCE_SIGNATURE


def test_an_added_member_fails_the_signature():
    """The `key_id` bug was an ADDITION to the body, and a MAC over a written
    field list is blind to an addition by construction."""
    record = sealed()
    tampered = dict(record)
    tampered["key_id_v2"] = me.key_id(OTHER_KEY)
    verdict = me.verify(tampered, KEY)
    assert not verdict.ok
    assert verdict.link == me.EVIDENCE_SIGNATURE


def test_the_signed_set_is_read_off_the_record_not_a_field_list():
    """The structural property, asserted directly: the members `_sign` covers
    are exactly the record's own members minus `signature`."""
    record = sealed()
    covered = {k: v for k, v in record.items() if k != me.SIGNATURE_FIELD}
    assert me._sign(record, KEY) == me._sign(covered, KEY)
    assert set(covered) == set(me.BODY_MEMBERS)
    assert me._sign(record, KEY) == record[me.SIGNATURE_FIELD]


def test_key_id_is_inside_the_mac():
    """Stated on its own because it is the member the cited bug left outside."""
    record = sealed()
    assert "key_id" in record
    relabelled = dict(record)
    relabelled["key_id"] = me.key_id(OTHER_KEY)
    verdict = me.verify(relabelled, KEY)
    assert not verdict.ok
    # The MAC fires first, so the record is refused as tampered rather than as
    # naming the wrong signer. Both are refusals; the MAC is the stronger one.
    assert verdict.link == me.EVIDENCE_SIGNATURE


def test_a_record_naming_another_key_is_refused_even_with_a_valid_mac():
    """The `cert.affirm_key_id` reading. Sign a body that names the WRONG key,
    with the right key: the MAC is genuinely valid and the record is still
    about a different signer."""
    body = dict(sealed())
    body.pop(me.SIGNATURE_FIELD)
    body["key_id"] = me.key_id(OTHER_KEY)
    body[me.SIGNATURE_FIELD] = me._sign(body, KEY)
    assert me._sign(body, KEY) == body[me.SIGNATURE_FIELD]  # the MAC is right
    verdict = me.verify(body, KEY)
    assert not verdict.ok
    assert verdict.link == me.EVIDENCE_SIGNER


def test_another_key_does_not_verify():
    verdict = me.verify(sealed(), OTHER_KEY)
    assert not verdict.ok
    assert verdict.link == me.EVIDENCE_SIGNATURE


def test_a_record_with_no_signature_is_refused():
    record = {k: v for k, v in sealed().items() if k != me.SIGNATURE_FIELD}
    verdict = me.verify(record, KEY)
    assert not verdict.ok
    assert verdict.link == me.EVIDENCE_INCOMPLETE


@pytest.mark.parametrize("value", [None, "", "not hex", "ab" * 31, 17])
def test_a_signature_that_is_not_a_tag_is_refused(value):
    record = dict(sealed())
    record[me.SIGNATURE_FIELD] = value
    verdict = me.verify(record, KEY)
    assert not verdict.ok
    assert verdict.link == me.EVIDENCE_SIGNATURE


@pytest.mark.parametrize("value", [None, 3, "a record", ["a", "record"]])
def test_a_non_record_is_refused_rather_than_crashing(value):
    verdict = me.verify(value, KEY)
    assert not verdict.ok
    assert verdict.link == me.EVIDENCE_INCOMPLETE


def test_a_lone_surrogate_is_a_refusal_not_a_crash():
    """`verify`'s contract is a verdict, so a peer-supplied string with no
    canonical byte spelling must refuse rather than escape as an encoding
    error (the reading `attest.NotCanonicalizable` already ships)."""
    record = dict(sealed())
    record["component"] = "\ud800"
    verdict = me.verify(record, KEY)
    assert not verdict.ok
    assert verdict.link in (me.EVIDENCE_VOCABULARY, me.EVIDENCE_SIGNATURE)


# ---------------------------------------------------------------------------
# the verifier is not a MAC check with decoration
# ---------------------------------------------------------------------------

def _resign(body):
    """Sign an arbitrary body with the real key — the other-implementation
    case. This is what a provider (item 538) holding the same key can produce,
    and it is the reason `verify` checks the body after the MAC."""
    body = dict(body)
    body.pop(me.SIGNATURE_FIELD, None)
    body["key_id"] = me.key_id(KEY)
    body[me.SIGNATURE_FIELD] = me._sign(body, KEY)
    return body


@pytest.mark.parametrize("member", sorted(me.BODY_MEMBERS))
def test_a_validly_signed_record_missing_a_member_is_refused(member):
    """A verifier that passes when a field is absent is the fail-open shape.
    Every member, removed and RE-SIGNED, so the MAC cannot be what refuses."""
    narrow = {k: v for k, v in sealed().items() if k != member}
    record = _resign(narrow)
    if member == "key_id":
        record.pop("key_id")
        record[me.SIGNATURE_FIELD] = me._sign(record, KEY)
    verdict = me.verify(record, KEY)
    assert not verdict.ok, f"a record with no {member!r} was accepted"
    assert verdict.link in (me.EVIDENCE_INCOMPLETE, me.EVIDENCE_SIGNER)


def test_a_validly_signed_record_with_an_extra_member_is_refused():
    record = _resign(dict(sealed(), reasoning_trace="the raw chain of thought"))
    verdict = me.verify(record, KEY)
    assert not verdict.ok
    assert verdict.link == me.EVIDENCE_VOCABULARY


@pytest.mark.parametrize("member,value", [
    ("kind", "revl.attestation"),
    ("version", "2.0"),
    ("sign_alg", "ed25519"),
    ("sign_alg", ""),
    ("hash_alg", "sha512"),
    ("key_id", "not-a-fingerprint"),
    ("recorded_at", None),
])
def test_a_validly_signed_record_with_a_wrong_envelope_is_refused(member, value):
    """The algorithm-confusion downgrade item 428 F1 measured on `revl attest`:
    a record whose `sign_alg` claims something `_sign` never produced must be
    REFUSED, not quietly MAC-verified under the algorithm it really used."""
    record = _resign(dict(sealed(), **{member: value}))
    if member == "key_id":
        record["key_id"] = value
        record[me.SIGNATURE_FIELD] = me._sign(record, KEY)
    verdict = me.verify(record, KEY)
    assert not verdict.ok
    assert verdict.link in (me.EVIDENCE_ENVELOPE, me.EVIDENCE_SIGNER)


# ---------------------------------------------------------------------------
# the closed vocabularies
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value", ["on-device", "onDevice", "edge", "", None,
                                   "on_device "])
def test_a_residence_outside_the_vocabulary_is_refused(value):
    record = _resign(dict(sealed(), residence=value))
    verdict = me.verify(record, KEY)
    assert not verdict.ok
    assert verdict.link == me.EVIDENCE_VOCABULARY


def test_the_residence_vocabulary_is_item_512s():
    assert set(me.RESIDENCES) == {"on_device", "off_device"}


def test_the_residence_vocabulary_agrees_with_model_route_once_it_lands():
    """Item 512 (`src/revl/model_route.py`) is the authority on the residence
    words. Until it lands there is nothing to compare against, so this asserts
    agreement WHEN the module is importable and skips with a reason otherwise.

    The conditional part is only the cross-module agreement. The check on a
    record is unconditional: an unknown residence is refused above whether or
    not `model_route` exists."""
    route = pytest.importorskip(
        "revl.model_route",
        reason="item 512 has not landed in this tree; the residence check on a "
               "record is unconditional either way")
    names = [n for n in dir(route) if "RESIDENCE" in n.upper()]
    assert names, "revl.model_route defines no residence vocabulary to compare"
    for name in names:
        value = getattr(route, name)
        if isinstance(value, (set, frozenset, tuple, list)):
            assert set(value) == set(me.RESIDENCES), (
                f"model_route.{name} disagrees with model_evidence.RESIDENCES")


def test_the_origin_lattice_is_taints_own():
    """Not a second copy of item 249's vocabulary (the item 272 lesson)."""
    assert set(me.ORIGIN_CLASSES) == set(_ORIGIN_CLASSES)


@pytest.mark.parametrize("origins", [
    ["prompt"], ["input", "unknown"], ["Input"], "input",
    ["web", "input"],           # not sorted: two records would not compare
    ["input", "input"],         # a repeat
])
def test_origins_outside_the_lattice_or_out_of_order_are_refused(origins):
    record = _resign(dict(sealed(), origins=origins))
    verdict = me.verify(record, KEY)
    assert not verdict.ok
    assert verdict.link == me.EVIDENCE_VOCABULARY


@pytest.mark.parametrize("outcome", ["ok", "success", "", None, "VALIDATED"])
def test_an_outcome_outside_the_vocabulary_is_refused(outcome):
    record = _resign(dict(sealed(), outcome=outcome))
    verdict = me.verify(record, KEY)
    assert not verdict.ok
    assert verdict.link == me.EVIDENCE_VOCABULARY


@pytest.mark.parametrize("depth", [-1, "0", None, True, 1.5])
def test_a_fallback_depth_that_is_not_a_ladder_depth_is_refused(depth):
    record = _resign(dict(sealed(), fallback_depth=depth))
    verdict = me.verify(record, KEY)
    assert not verdict.ok
    assert verdict.link == me.EVIDENCE_VOCABULARY


def test_a_non_zero_fallback_depth_is_a_distinct_record():
    """The member exists so a clean first hit and an answer three rungs down
    the ladder are different claims rather than the same silence."""
    first = sealed(fallback_depth=0)
    third = sealed(fallback_depth=3,
                   recorded_at=first["recorded_at"])
    assert me.verify(first, KEY).ok and me.verify(third, KEY).ok
    assert first[me.SIGNATURE_FIELD] != third[me.SIGNATURE_FIELD]


# ---------------------------------------------------------------------------
# the candidate set and the choice, as one claim
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("candidates,chosen,outcome", [
    ([], 0, "validated"),                       # a choice among nothing
    ([D("a")], 1, "validated"),                 # index past the end
    ([D("a")], -1, "validated"),
    ([D("a"), D("b")], None, "validated"),      # validated but took nothing
    ([D("a"), D("b")], True, "validated"),      # a bool is not an index
    ([D("a"), D("a")], 0, "validated"),         # a repeated digest
    ([D("a")], 0, "exhausted"),                 # exhausted but took one
    ([D("a")], 0, "refused"),
    (["not a digest"], 0, "validated"),
    ([D("a")], "0", "validated"),
])
def test_an_incoherent_candidate_claim_is_refused(candidates, chosen, outcome):
    record = _resign(dict(sealed(), candidates=candidates, chosen=chosen,
                          outcome=outcome))
    verdict = me.verify(record, KEY)
    assert not verdict.ok
    assert verdict.link in (me.EVIDENCE_CANDIDATES, me.EVIDENCE_VOCABULARY)


@pytest.mark.parametrize("outcome", ["exhausted", "refused"])
def test_a_ladder_refusal_records_no_choice(outcome):
    """Item 538: an unavailable member is a refusal the ladder consumes, not a
    lower-quality completion. The record has a shape for that."""
    record = sealed(candidates=[], chosen=None, outcome=outcome,
                    fallback_depth=2)
    assert me.verify(record, KEY).ok


def test_one_candidate_and_five_candidates_are_different_claims():
    one = sealed(candidates=[D("a")], chosen=0)
    five = sealed(candidates=[D(c) for c in "abcde"], chosen=0,
                  recorded_at=one["recorded_at"])
    assert me.verify(one, KEY).ok and me.verify(five, KEY).ok
    assert one[me.SIGNATURE_FIELD] != five[me.SIGNATURE_FIELD]


# ---------------------------------------------------------------------------
# sampling: the closed re-run instruction
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("member", sorted(me.SAMPLING_MEMBERS))
def test_a_sampling_member_that_is_absent_is_refused(member):
    """The fail-closed half: the KEY is mandatory. `None` is the way a record
    says the request did not set a knob, and a missing key is not that."""
    sampling = {k: v for k, v in _members()["sampling"].items() if k != member}
    record = _resign(dict(sealed(), sampling=sampling))
    verdict = me.verify(record, KEY)
    assert not verdict.ok
    assert verdict.link == me.EVIDENCE_INCOMPLETE


@pytest.mark.parametrize("member", sorted(me.SAMPLING_MEMBERS))
def test_a_sampling_member_may_be_none_and_the_record_still_verifies(member):
    """The other half: a run that did not set a seed is a real run. It is
    recordable, and `reproducible` is what says what you can do with it."""
    sampling = dict(_members()["sampling"], **{member: None})
    record = sealed(sampling=sampling)
    assert me.verify(record, KEY).ok


@pytest.mark.parametrize("sampling", [
    {"temperature": -0.1}, {"temperature": "hot"}, {"top_p": -1},
    {"seed": -1}, {"seed": 1.5}, {"max_tokens": -4}, {"top_k": True},
    {"stop_digest": "STOP"},
])
def test_a_malformed_sampling_value_is_refused(sampling):
    record = _resign(dict(sealed(),
                          sampling=dict(_members()["sampling"], **sampling)))
    verdict = me.verify(record, KEY)
    assert not verdict.ok
    assert verdict.link == me.EVIDENCE_VOCABULARY


def test_an_unknown_sampling_member_is_refused():
    record = _resign(dict(sealed(),
                          sampling=dict(_members()["sampling"],
                                        repetition_penalty=1.1)))
    verdict = me.verify(record, KEY)
    assert not verdict.ok
    assert verdict.link == me.EVIDENCE_VOCABULARY


# ---------------------------------------------------------------------------
# the prompt binding, and the disclosure gate it inherits
# ---------------------------------------------------------------------------

def test_a_suppressed_binding_names_which_arm_of_the_gate_fired():
    record = sealed(origins=["confidential", "input"],
                    prompt_binding={"mode": "suppressed", "value": None,
                                    "reason": "confidential-origin"})
    assert me.verify(record, KEY).ok
    assert me.reproducible(record)[0] is False
    assert "prompt_binding" in me.reproducible(record)[1]


@pytest.mark.parametrize("origin", sorted(me.DISCLOSURE_ORIGINS))
def test_a_content_addressed_binding_over_a_disclosing_input_is_refused(origin):
    """`revl_prompt_digest` returns None for exactly these origins (item 121
    §4): an unsalted digest over a confidential prompt is a confirmation oracle
    that outlives the run. A record carrying one was written around the
    runtime, and this is the evidence layer refusing it anyway."""
    record = _resign(dict(sealed(), origins=sorted({origin, "input"}),
                          prompt_binding={"mode": "content-addressed",
                                          "value": D("the secret prompt"),
                                          "reason": None}))
    verdict = me.verify(record, KEY)
    assert not verdict.ok
    assert verdict.link == me.EVIDENCE_DISCLOSURE


@pytest.mark.parametrize("origin", sorted(me.DISCLOSURE_ORIGINS))
def test_a_salted_binding_over_a_disclosing_input_is_refused(origin):
    record = _resign(dict(
        sealed(), origins=sorted({origin, "input"}),
        prompt_binding={"mode": "salted-within-run",
                        "value": "hmac-sha256:" + D("salted"), "reason": None}))
    verdict = me.verify(record, KEY)
    assert not verdict.ok
    assert verdict.link == me.EVIDENCE_DISCLOSURE


def test_the_salted_binding_shape_is_the_runtimes_own():
    """`revl_prompt_digest` returns `{"salted": "hmac-sha256:<hex>"}`. The
    record accepts that spelling and nothing else, so a value from some other
    construction cannot be filed as the runtime's."""
    ok = sealed(prompt_binding={"mode": "salted-within-run",
                                "value": "hmac-sha256:" + D("x"),
                                "reason": None})
    assert me.verify(ok, KEY).ok
    for value in ("sha256:" + D("x"), D("x"), "hmac-sha256:nothex",
                  "hmac-sha256:", None):
        bad = _resign(dict(sealed(),
                           prompt_binding={"mode": "salted-within-run",
                                           "value": value, "reason": None}))
        verdict = me.verify(bad, KEY)
        assert not verdict.ok, value
        assert verdict.link == me.EVIDENCE_VOCABULARY


@pytest.mark.parametrize("binding", [
    {"mode": "hashed", "value": None, "reason": None},
    {"mode": "suppressed", "value": D("x"), "reason": "secret-origin"},
    {"mode": "suppressed", "value": None, "reason": "because"},
    {"mode": "suppressed", "value": None, "reason": None},
    {"mode": "content-addressed", "value": D("x"), "reason": "secret-origin"},
    {"mode": "content-addressed", "value": "short", "reason": None},
    {"mode": "content-addressed", "value": D("x")},
    {"mode": "content-addressed", "value": D("x"), "reason": None, "text": "hi"},
    "a digest",
])
def test_a_malformed_prompt_binding_is_refused(binding):
    record = _resign(dict(sealed(), prompt_binding=binding))
    verdict = me.verify(record, KEY)
    assert not verdict.ok
    assert verdict.link == me.EVIDENCE_VOCABULARY


# ---------------------------------------------------------------------------
# retention: an explicit decision that carries the taint
# ---------------------------------------------------------------------------

def test_retaining_content_without_saying_so_is_refused():
    with pytest.raises(me.EvidenceRefused) as caught:
        me.seal(KEY, retained={"prompt": "raw text", "completion": None,
                               "origins": ["web"]},
                **_members())
    assert caught.value.link == me.EVIDENCE_DISCLOSURE


def test_claiming_a_retention_decision_with_nothing_retained_is_refused():
    with pytest.raises(me.EvidenceRefused) as caught:
        me.seal(KEY, retain_content=True, **_members())
    assert caught.value.link == me.EVIDENCE_DISCLOSURE


def test_retention_is_legal_when_it_is_explicit_and_the_taint_fits():
    record = me.seal(KEY, retain_content=True,
                     retained={"prompt": "classify this ticket",
                               "completion": "billing",
                               "origins": ["web"]},
                     **_members())
    assert me.verify(record, KEY).ok
    assert record["retained"]["origins"] == ["web"]


def test_retained_bytes_cannot_claim_an_origin_the_decision_does_not_carry():
    """The record's own taint must not be narrower than the content it holds."""
    record = _resign(dict(sealed(),
                          retained={"prompt": "raw", "completion": None,
                                    "origins": ["fs"]}))
    verdict = me.verify(record, KEY)
    assert not verdict.ok
    assert verdict.link == me.EVIDENCE_DISCLOSURE


@pytest.mark.parametrize("origin", sorted(me.DISCLOSURE_ORIGINS))
def test_retention_over_a_disclosing_input_is_refused(origin):
    """The issue's rule: hashes are the binding. Writing confidential bytes
    into an artifact that travels is refused however explicit the caller was."""
    with pytest.raises(me.EvidenceRefused) as caught:
        me.seal(KEY, retain_content=True,
                **_members(origins=sorted({origin, "input"}),
                           prompt_binding={"mode": "suppressed", "value": None,
                                           "reason": f"{origin}-origin"},
                           retained={"prompt": "the confidential ticket",
                                     "completion": None,
                                     "origins": [origin]}))
    assert caught.value.link == me.EVIDENCE_DISCLOSURE


@pytest.mark.parametrize("retained", [
    {"prompt": None, "completion": None, "origins": ["web"]},
    {"prompt": "raw", "completion": 7, "origins": ["web"]},
    {"prompt": "raw", "completion": None, "origins": []},
    {"prompt": "raw", "completion": None, "origins": ["web", "input"]},
    {"prompt": "raw", "origins": ["web"]},
    {"prompt": "raw", "completion": None, "origins": "web"},
    "raw text",
])
def test_a_malformed_retention_block_is_refused(retained):
    record = _resign(dict(sealed(), retained=retained))
    verdict = me.verify(record, KEY)
    assert not verdict.ok
    assert verdict.link in (me.EVIDENCE_VOCABULARY, me.EVIDENCE_DISCLOSURE)


def test_the_default_record_retains_nothing():
    assert sealed()["retained"] is None


# ---------------------------------------------------------------------------
# seal cannot mint what verify refuses
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("overrides", [
    {"residence": "edge"},
    {"origins": ["prompt"]},
    {"outcome": "ok"},
    {"chosen": 9},
    {"model_digest": "not a digest"},
    {"fallback_depth": -1},
    {"sampling": {"temperature": 0.2}},
    {"prompt_binding": {"mode": "hashed", "value": None, "reason": None}},
])
def test_seal_refuses_a_body_verify_would_refuse(overrides):
    """One check function, used by both sides. A sealer that can mint a record
    its own verifier rejects is a gate that only fires on other people."""
    with pytest.raises(me.EvidenceRefused):
        me.seal(KEY, **_members(**overrides))


# ---------------------------------------------------------------------------
# where it slots in
# ---------------------------------------------------------------------------

def test_the_crossing_key_is_the_wal_index_key():
    """`revl.wal.model_decisions` indexes `model-decision` records by
    `(component, stepIndex)`. The evidence object joins on the same pair and
    invents no second correlation."""
    record = sealed(component="Classifier", step_index=7)
    wal_record = {"record": wal.RECORD_MODEL_DECISION,
                  "component": "Classifier", "stepIndex": 7,
                  "outcome": "validated", "llm": {}}
    index = wal.model_decisions([wal_record])
    assert me.crossing_key(record) in index
    assert index[me.crossing_key(record)] is wal_record


def test_the_outcome_vocabulary_covers_the_wal_records_own():
    """The WAL writer's `outcome` is `validated` or `exhausted` (item 257).
    Both are in this vocabulary, so an evidence object can carry the WAL
    record's own outcome without a translation table."""
    assert {"validated", "exhausted"} <= set(me.OUTCOMES)


def test_reproducible_reports_what_a_re_run_would_need():
    complete = sealed()
    assert me.reproducible(complete) == (True, [])

    no_seed = sealed(sampling=dict(_members()["sampling"], seed=None))
    ok, missing = me.reproducible(no_seed)
    assert not ok and missing == ["sampling.seed"]
    assert me.verify(no_seed, KEY).ok, "a seedless run is still recordable"

    no_profile = sealed(placement_digest=None)
    ok, missing = me.reproducible(no_profile)
    assert not ok and "placement_digest" in missing
    assert me.verify(no_profile, KEY).ok


def test_the_placement_digest_is_opaque_here():
    """Item 538 owns what the digest is computed OVER. This module binds it and
    does not interpret it, so any 64-hex value is a valid placement digest and
    nothing reads a quantisation out of it."""
    for value in (D("m4 / 4-bit / 2.1GB"), D("h100 / bf16"), D("")):
        assert me.verify(sealed(placement_digest=value), KEY).ok
    record = _resign(dict(sealed(), placement_digest="4-bit"))
    assert me.verify(record, KEY).link == me.EVIDENCE_VOCABULARY


# ---------------------------------------------------------------------------
# domain separation from the other signed records in the tree
# ---------------------------------------------------------------------------

def test_the_sign_domain_is_not_the_attestation_domain():
    assert me.SIGN_DOMAIN != attest.SIGN_DOMAIN


def test_an_evidence_record_does_not_verify_as_an_attestation():
    record = sealed()
    ok, _reason = attest.verify_attestation(record, KEY)
    assert not ok


def test_an_attestation_body_does_not_verify_as_evidence():
    """Both MAC canonical JSON with the same construction under the same key.
    Without a per-protocol domain tag, one would verify as the other."""
    body = {k: v for k, v in sealed().items() if k != me.SIGNATURE_FIELD}
    forged = dict(body)
    forged[me.SIGNATURE_FIELD] = attest._sign(body, KEY)
    verdict = me.verify(forged, KEY)
    assert not verdict.ok
    assert verdict.link == me.EVIDENCE_SIGNATURE


def test_the_key_fingerprints_are_domain_separated():
    """One secret fingerprints differently for the two protocols, so a
    fingerprint cannot be lifted from one record type onto the other as if it
    named the same role."""
    assert me.key_id(KEY) != attest.key_id(KEY)
    assert me.key_id(KEY) != me.key_id(OTHER_KEY)
    assert me.key_id(KEY) == me.key_id(KEY)


# ---------------------------------------------------------------------------
# controls — these hold identically on a tree without this module's changes
# ---------------------------------------------------------------------------

def test_control_the_attestation_key_fingerprint_is_unchanged():
    """A pinned constant from `revl.attest`, recomputed here. Nothing in this
    work touches the attestation path it borrows canonicalization from, and a
    change to `attest.key_id` would move this value on both trees."""
    import hashlib
    expected = hashlib.sha256(b"revl-attest-keyid\x00" + KEY).hexdigest()[:16]
    assert attest.key_id(KEY) == expected


def test_control_the_canonicalization_is_attests_own():
    """The bytes this module MACs are produced by `attest._canonical_bytes`,
    not by a second serializer. A drift in either would move this."""
    assert attest._canonical_bytes({"b": 1, "a": [2, None]}) == \
        b'{"a":[2,null],"b":1}'


# ---------------------------------------------------------------------------
# the digest is the shipped primitive, not a fourth hand-rolled one
# ---------------------------------------------------------------------------

def test_the_digest_agrees_with_the_stdlib_crypto_primitive(tmp_path):
    """Item 272 exists because three components in one wave independently
    hand-rolled the same crypto. `stdlib/crypto.rvl` is that kit, classified.

    A Python module cannot `use` an `.rvl` module, so the agreement is pinned
    instead of imported: this compiles a consumer of the classified externs and
    EXECUTES it on the py tier, asserting `sha256` and `hmac_sha256` over the
    exact strings this module digests produce the exact values this module
    produces. If either side ever changed construction, this reds.
    """
    import shutil
    from revl import compile_files
    from revl.test import run_py

    prompt = "classify this ticket"
    model = "nanojev-0.4-weights"
    mac_key = "an evidence signing secret"
    body = "revl.model-decision/v1"

    consumer = f'''\
use "stdlib/crypto.rvl" {{ sha256, hmac_sha256 }}

test "model_evidence.digest agrees with the classified sha256" {{
  assert sha256("{prompt}") == "{D(prompt)}"
  assert sha256("{model}") == "{D(model)}"
  assert sha256("") == "{D("")}"
}}

test "the MAC construction agrees with the classified hmac_sha256" {{
  assert hmac_sha256("{mac_key}", "{body}")
    == "{__import__("hmac").new(mac_key.encode("utf-8"), body.encode("utf-8"),
                                __import__("hashlib").sha256).hexdigest()}"
}}
'''
    (tmp_path / "stdlib").mkdir()
    shutil.copy(ROOT / "stdlib" / "crypto.rvl", tmp_path / "stdlib" / "crypto.rvl")
    main = tmp_path / "main.rvl"
    main.write_text(consumer, encoding="utf-8")
    verdict, detail = run_py(compile_files([str(main)]))
    assert verdict == "pass", detail
