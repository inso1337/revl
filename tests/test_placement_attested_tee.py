"""The placement-file spelling of an attested-TEE demand (roadmap item 475,
issue #827).

#859 landed the typed requirement (`tee_attestation.TeeRequirement`) and the
fail-closed verifier (`tee_admits`), and `peer_offer.PlacementSlot.attested_tee`
gave the demand a field to live in — but the placement FILE could not spell the
demand, which is exactly what the design note deferred:

    "The placement-file key. `requires attested_tee` is not yet a grammar word in
     the placement surface of `src/revl/placement.py`."

This file pins that word, and it has to be three things at once, so each gets its
own section:

* the word PARSES and round-trips (parse -> render -> parse is stable), and a
  table that demands nothing is refused rather than ignored — an `[attest]` table
  that admits any enclave is worse than no table at all;
* a placement that does NOT spell it behaves exactly as it did before;
* a placement that DOES spell it refuses a peer that cannot attest, naming the
  reason, fail-closed — and the refusal is `tee_admits`' own, not a second one
  written here, so the two spellings of one demand cannot drift into two answers.

The evidence, the keys and the offers are `tests/test_tee_attestation.py`'s, not
a second copy: this file adds a placement surface to that suite's story.
"""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from revl import placement as plc  # noqa: E402
from revl.peer_offer import PlacementSlot, offer_eligible  # noqa: E402
from revl.tee_attestation import TeeError, TeeRequirement  # noqa: E402
from test_tee_attestation import (  # noqa: E402
    ATTESTER_KEY,
    BUNDLE,
    MEASUREMENT,
    NONCE,
    NOW,
    OTHER_BUNDLE,
    OTHER_REGION,
    PEER_ID,
    PEER_KEY,
    REGION,
    make_offer,
    make_proof,
    make_requirement,
)


# --------------------------------------------------------------- the fixtures


def attest_table(**overrides) -> dict:
    """A well-formed `[processes.worker.attest]` table, spelled the way a
    placement file spells it."""
    table = {"requires": plc.TEE_WORD_ATTESTED, "bundle": BUNDLE,
             "measurements": [MEASUREMENT], "region": REGION, "nonce": NONCE}
    table.update(overrides)
    return table


def placement_file(table=None, process: str = "worker") -> dict:
    """A placement that places one component in `process`. `table` is its
    `[processes.<process>.attest]` table, or `None` for a placement that demands
    nothing — the pre-item-475 file."""
    entry = {"components": ["Worker"], "backend": "py"}
    if table is not None:
        entry["attest"] = table
    return {"processes": {process: entry}}


def parsed(placement: dict, process: str = "worker", **kwargs) -> TeeRequirement:
    requirement, problem = plc.parse_tee_requirement(placement, process, **kwargs)
    assert problem is None, problem
    assert requirement is not None
    return requirement


def refusal(placement: dict, process: str = "worker", **kwargs) -> str:
    requirement, problem = plc.parse_tee_requirement(placement, process, **kwargs)
    assert requirement is None, "expected a refusal, got a requirement"
    assert problem, "a refused table must say why"
    return problem


def demanding_slot() -> PlacementSlot:
    slot, problem = plc.process_placement_slot(placement_file(attest_table()), "worker")
    assert problem is None, problem
    assert slot is not None
    return slot


def admit(record, table=None, **overrides):
    """The whole seam, configured for acceptance except the one fact a test is
    about."""
    fields = {"offer_key": PEER_KEY, "attester_key": ATTESTER_KEY,
              "tee_ledger": set(), "now": NOW}
    fields.update(overrides)
    table = attest_table() if table is None else table
    return plc.admit_peer_for_process(placement_file(table), "worker", record, **fields)


# ------------------------------------------- 1. the word parses and round-trips


def test_the_word_parses_into_the_landed_typed_requirement():
    """The spelling is a spelling: `requires = "attested_tee"` plus the facts it
    demands is exactly the `TeeRequirement` #859 already defines."""
    assert parsed(placement_file(attest_table())) == make_requirement()


def test_parse_render_parse_is_stable():
    requirement = parsed(placement_file(attest_table()))
    text = plc.render_tee_requirement(requirement)
    again = parsed(tomllib.loads(text))
    assert again == requirement
    assert plc.render_tee_requirement(again) == text


def test_the_rendered_table_re_parses_out_of_a_whole_placement_file():
    """Rendered into a real placement file, with the process table around it,
    because that is the file the conductor reads."""
    text = ("[processes.worker]\ncomponents = [\"Worker\"]\n\n"
            + plc.render_tee_requirement(make_requirement()))
    slot, problem = plc.process_placement_slot(tomllib.loads(text), "worker")
    assert problem is None
    assert slot is not None and slot.attested_tee == make_requirement()


def test_the_demand_lands_in_the_field_offer_eligible_already_reads():
    slot = demanding_slot()
    assert slot.attested_tee == make_requirement()
    # ...and the permitted region is deliberately NOT asserted on the slot: the
    # region is checked against the attestation, never against the peer's word.
    assert slot.regions is None


def test_the_word_is_the_one_peer_offer_already_names():
    """No new identifier: `attested_tee` is the field `peer_offer` already
    carries, so the placement surface and the gate cannot spell one demand two
    ways."""
    assert plc.TEE_WORD_ATTESTED == "attested_tee"
    assert "attested_tee" in PlacementSlot.__dataclass_fields__


def test_the_file_may_spell_the_plural_when_it_permits_several_regions():
    table = attest_table()
    del table["region"]
    table["regions"] = [REGION, OTHER_REGION]
    requirement = parsed(placement_file(table))
    assert requirement.regions == frozenset({REGION, OTHER_REGION})
    text = plc.render_tee_requirement(requirement)
    assert "regions = [" in text
    assert parsed(tomllib.loads(text)) == requirement


def test_one_region_is_rendered_in_the_singular():
    text = plc.render_tee_requirement(make_requirement())
    assert f'region = "{REGION}"' in text
    assert "regions = " not in text


def test_the_challenge_is_minted_when_the_file_names_none():
    """A demand with no challenge of the placement's own choosing is not evidence
    of anything in particular, so one is minted — and it is the placement's, not
    the peer's, so two parses do not agree."""
    table = attest_table()
    del table["nonce"]
    first = parsed(placement_file(table))
    second = parsed(placement_file(table))
    assert first.nonce and second.nonce
    assert first.nonce != second.nonce


def test_a_caller_supplied_challenge_is_the_one_the_placement_issues():
    table = attest_table()
    del table["nonce"]
    requirement = parsed(placement_file(table), nonce=NONCE)
    assert requirement.nonce == NONCE
    assert requirement == make_requirement()


# ------------------------------------------- 2. a table that demands nothing


@pytest.mark.parametrize("word", ["attested_tee_please", "attested", "tee", ""])
def test_an_unknown_requirement_word_is_refused_by_name(word):
    problem = refusal(placement_file(attest_table(requires=word)))
    assert "requires" in problem and repr(plc.TEE_WORD_ATTESTED) in problem


def test_a_table_with_no_requires_word_is_refused():
    """An `[attest]` table that names no word is refused rather than read as a
    demand that asks for nothing, which is what would make the whole surface
    vacuous."""
    table = attest_table()
    del table["requires"]
    problem = refusal(placement_file(table))
    assert "no `requires` word" in problem


def test_an_unknown_key_is_refused_by_name():
    problem = refusal(placement_file(attest_table(mesurements=[MEASUREMENT])))
    assert "'mesurements'" in problem and "'measurements'" in problem


def test_attest_must_be_a_table():
    problem = refusal(placement_file("attested_tee"))
    assert "must be a table" in problem


@pytest.mark.parametrize("bad", ["ab" * 32, [1], [None], {"a": "b"}, 7])
def test_measurements_must_be_a_list_of_strings(bad):
    assert "measurements" in refusal(placement_file(attest_table(measurements=bad)))


def test_a_demand_that_permits_any_enclave_is_refused():
    problem = refusal(placement_file(attest_table(measurements=[])))
    assert "admits any enclave" in problem


def test_a_demand_that_permits_any_region_is_refused():
    table = attest_table()
    del table["region"]
    table["regions"] = []
    problem = refusal(placement_file(table))
    assert "admits an enclave anywhere" in problem


def test_a_posture_other_than_forbidden_is_refused():
    problem = refusal(placement_file(attest_table(outbound_network="allowed")))
    assert "outbound_network" in problem and "'forbidden'" in problem


def test_a_missing_bundle_is_refused():
    table = attest_table()
    del table["bundle"]
    assert "bundle" in refusal(placement_file(table))


def test_region_and_regions_together_are_refused():
    """One fact spelled twice is ambiguous, so it is refused rather than
    resolved by a tie-break rule."""
    problem = refusal(placement_file(attest_table(regions=[REGION])))
    assert "spelled twice" in problem


def test_a_non_positive_freshness_window_is_refused():
    assert "max_age_s" in refusal(placement_file(attest_table(max_age_s=0)))


def test_a_refusal_names_the_process_it_came_from():
    """A refusal an operator reads has to say WHERE, because the placement is a
    file and the demand is one table in it."""
    assert refusal(placement_file(attest_table(measurements=[]))).startswith(
        "process 'worker'")


def test_a_malformed_table_comes_back_as_a_diagnostic_not_an_exception():
    """The typed requirement raises; the placement surface reports. A planner
    that crashed on an operator's typo would be a worse surface than one that
    says which line is wrong."""
    with pytest.raises(TeeError):
        TeeRequirement(bundle=BUNDLE, measurements=frozenset(), nonce=NONCE,
                       regions=frozenset({REGION}))
    assert refusal(placement_file(attest_table(measurements=[])))


# ------------------------------------------- 3. a placement without the key


def test_a_placement_without_the_key_declares_nothing():
    file = placement_file()
    assert plc.parse_tee_requirement(file, "worker") == (None, None)
    assert plc.process_placement_slot(file, "worker") == (None, None)
    assert plc.tee_placement_diagnostic(file) is None


def test_a_placement_without_the_key_admits_exactly_as_it_did_before():
    """The empty slot: an ordinary offer is eligible, an OFFERED PROOF is ignored
    rather than checked, and no challenge is consumed."""
    ledger: set = set()
    ok, reason = plc.admit_peer_for_process(
        placement_file(), "worker",
        make_offer(trust="verified", proof=make_proof()),
        offer_key=PEER_KEY, attester_key=ATTESTER_KEY, tee_ledger=ledger, now=NOW)
    assert ok, reason
    assert ledger == set()


def test_a_placement_without_the_key_admits_with_no_attester_configured():
    """Nothing to check the demand against because there is no demand: the
    absence of an attester root is not a refusal when no attestation is asked
    for."""
    ok, reason = plc.admit_peer_for_process(
        placement_file(), "worker", make_offer(trust="verified"),
        offer_key=PEER_KEY, attester_key=None, tee_ledger=None, now=NOW)
    assert ok, reason


def test_the_surface_reads_only_its_own_table():
    """A process carrying every other placement surface's keys is untouched by
    this reader: it looks at `attest` and at nothing else."""
    file = placement_file()
    file["processes"]["worker"].update({"seam_deadline": 5.0,
                                        "deploy": {"via": "local"}})
    assert plc.parse_tee_requirement(file, "worker") == (None, None)
    assert plc.tee_placement_diagnostic(file) is None


def test_a_process_with_no_entry_declares_nothing():
    for file in (placement_file(), {"processes": {}}, {}, {"processes": []}):
        assert plc.parse_tee_requirement(file, "worker") == (None, None)
        assert plc.parse_tee_requirement(file, "absent") == (None, None)
        assert plc.tee_placement_diagnostic(file) is None


# ------------------------------------------- 4. the key against a real peer


def test_an_attested_placement_refuses_a_peer_that_cannot_attest():
    """The load-bearing refusal: the peer asserts `trust: attested`, the
    placement demands an attested TEE, and the assertion is worth nothing."""
    ok, reason = admit(make_offer(trust="attested"))
    assert not ok
    assert reason.startswith("attestation:")
    assert "no enclave evidence" in reason


def test_an_attested_placement_admits_a_peer_that_can_attest():
    """...and the same placement admits the peer that proves it, so the refusal
    above is about the missing evidence and not about the demand."""
    ok, reason = admit(make_offer(proof=make_proof()))
    assert ok, reason


def test_the_refusal_is_the_landed_verifiers_own_verdict():
    """Byte-identical to what the gate #859 landed says for the same offer
    against the same slot, so the two spellings of one demand cannot drift into
    two answers."""
    record = make_offer(proof=make_proof(bundle=OTHER_BUNDLE))
    _, mine = admit(record)
    _, theirs = offer_eligible(record, demanding_slot(), PEER_KEY,
                               attester_key=ATTESTER_KEY, tee_ledger=set(), now=NOW)
    assert mine == theirs
    assert "attestation:" in mine and OTHER_BUNDLE in mine


def test_an_attested_placement_refuses_a_proof_for_another_region():
    ok, reason = admit(make_offer(proof=make_proof(region=OTHER_REGION)))
    assert not ok and reason.startswith("attestation:") and OTHER_REGION in reason


def test_an_attested_placement_refuses_a_proof_for_another_bundle():
    ok, reason = admit(make_offer(proof=make_proof(bundle=OTHER_BUNDLE)))
    assert not ok and "not the approved bundle" in reason


def test_the_placement_challenge_is_the_one_the_evidence_must_answer():
    """The challenge comes from the FILE, so evidence answering some other
    challenge — the peer's own, typically — is refused even when it is otherwise
    perfect."""
    ok, reason = admit(make_offer(proof=make_proof(nonce="peer-chosen-challenge")))
    assert not ok and "answers challenge" in reason


def test_a_caller_supplied_challenge_pins_the_evidence_end_to_end():
    table = attest_table()
    del table["nonce"]
    file = placement_file(table)
    ok, reason = plc.admit_peer_for_process(
        file, "worker", make_offer(proof=make_proof()),
        offer_key=PEER_KEY, attester_key=ATTESTER_KEY, tee_ledger=set(),
        now=NOW, nonce=NONCE)
    assert ok, reason


# ------------------------------------------- 5. fail-closed on the evidence


def test_an_absent_attestation_refuses_rather_than_admitting():
    ok, reason = admit(make_offer(proof=None))
    assert not ok and reason.startswith("attestation:")
    assert "no enclave evidence" in reason


@pytest.mark.parametrize("unreadable", [{}, {"key_id": "0" * 16},
                                        {"signature": "deadbeef"},
                                        {"bundle": BUNDLE}])
def test_an_unreadable_attestation_refuses_rather_than_admitting(unreadable):
    """Fail-closed on the evidence itself: garbage and half-written proofs are
    refused, never read as "no objection raised"."""
    ok, reason = admit(make_offer(proof=unreadable))
    assert not ok and reason.startswith("attestation:")


def test_a_demand_with_no_attester_root_refuses_rather_than_admits():
    ok, reason = admit(make_offer(proof=make_proof()), attester_key=None)
    assert not ok and "attestation:" in reason and "no attester key" in reason


def test_a_demand_with_no_ledger_refuses_rather_than_admits():
    ok, reason = admit(make_offer(proof=make_proof()), tee_ledger=None)
    assert not ok and "no replay ledger" in reason


def test_a_proof_the_peer_could_have_signed_itself_refuses():
    ok, reason = admit(make_offer(proof=make_proof()), attester_key=PEER_KEY)
    assert not ok and "peer's own offer key" in reason


def test_a_replayed_proof_refuses():
    ledger: set = set()
    record = make_offer(proof=make_proof())
    first, reason = admit(record, tee_ledger=ledger)
    assert first, reason
    second, reason = admit(record, tee_ledger=ledger)
    assert not second and "replay" in reason


def test_a_malformed_table_refuses_at_the_seam_too():
    file = placement_file(attest_table(measurements=[]))
    ok, reason = plc.admit_peer_for_process(
        file, "worker", make_offer(proof=make_proof()), offer_key=PEER_KEY,
        attester_key=ATTESTER_KEY, tee_ledger=set(), now=NOW)
    assert not ok and reason.startswith("placement:")
    assert "admits any enclave" in reason


# ------------------------------------------- 6. the plan-time refusal


def test_a_well_formed_demand_this_build_cannot_satisfy_is_refused():
    """A demand nothing here can produce evidence for is refused rather than
    ignored: running it would be an unattested run of an attested placement,
    which is the one outcome the demand exists to forbid."""
    problem = plc.tee_placement_diagnostic(placement_file(attest_table()))
    assert problem is not None
    assert "'worker'" in problem and plc.TEE_WORD_ATTESTED in problem
    assert "refused rather than run unattested" in problem


def test_a_malformed_table_is_reported_by_the_plan_time_verdict():
    problem = plc.tee_placement_diagnostic(placement_file(attest_table(measurements=[])))
    assert problem is not None and "admits any enclave" in problem


def _never_spawned(*args, **kwargs):
    raise AssertionError("run_placement spawned a child past the attestation refusal")


def test_the_conductor_refuses_an_attested_placement_before_anything_spawns(
        tmp_path, monkeypatch, capsys):
    """End to end through the conductor the CLI calls, so the refusal lands as a
    diagnostic on `revl run --placement` rather than as an unattested boot."""
    app = tmp_path / "worker.rvl"
    app.write_text("component Worker {\n  effect 0\n  undo 0\n}\n", encoding="utf-8")
    placement_path = tmp_path / "worker.toml"
    placement_path.write_text("[processes.worker]\ncomponents = [\"Worker\"]\n\n"
                              + plc.render_tee_requirement(make_requirement()),
                              encoding="utf-8")

    monkeypatch.setattr(plc, "_cordis_py_installed", lambda: True)
    monkeypatch.setattr(plc.subprocess, "Popen", _never_spawned)

    rc = plc.run_placement([str(app)], str(placement_path), once=True)
    assert rc == 1
    err = capsys.readouterr().err
    assert err.startswith("error: process(es) 'worker' require an attested TEE")
    assert "refused rather than run unattested" in err
