"""The signed erasure receipt (src/revl/erasure_receipt.py, roadmap item 472).

Item 472's exit criterion has two halves. The erasure half is "an erasure
request emits a signed receipt naming each in-scope replica and derivative", and
that is what this module and these tests pin:

  * every replica the erase report enumerated appears in the signed receipt,
    with the report's OWN disposition, and no replica the realm did not touch
    appears at all;
  * the row's named channel is the declared inverse (`undo` for a witnessed
    extern, `compensate` for an offset-bearing emission) or nothing, never an
    invented name and never a hardcoded recipient list;
  * the receipt verifies against the key that signed it and against the report
    it was issued over, and stops verifying when either is altered;
  * an erasure receipt is not an attestation and not a deploy receipt, one key
    notwithstanding: the MAC is domain separated.

The retention half is deliberately NOT here, and the design note
(docs/design/472-retention-erasure-receipts.md) records why: the tree has no
persistence sink the type system owns, no residence or legal-hold vocabulary
and no runtime age fact, so a `Retained[T]` deadline refusal would have to be
faked to land in this slice. Nothing in this file claims otherwise.

The `erase_receipt.rvl` fixture is one realm `vault` crossing the boundary in
each shape a row can carry (witnessed, compensate-bearing emission, bare
emission, in-process service emission) plus a second realm `other`, whose
crossing must never appear in `vault`'s receipt. The runtime no-residue proof
needs the cordis backend, so those assertions are guarded with `_has_runtime`.
"""

import hashlib
import hmac
import json
import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from revl import attest, erase_report, erasure_receipt  # noqa: E402
from revl.compiler import compile_files  # noqa: E402
from revl.errors import RevlError  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURE = os.path.join(ROOT, "tests", "fixtures", "erase_receipt.rvl")
KEY = b"a-test-key-that-is-not-a-secret"
NOW = "2026-01-01T00:00:00+00:00"
KEY_ENVS = ("REVL_ERASURE_KEY", "REVL_ERASURE_KEY_FILE")
BUCKETS = ("emissions", "externs", "witnessed", "widenings")


def _has_runtime() -> bool:
    try:
        import cordis  # noqa: F401,PLC0415
        return True
    except ModuleNotFoundError:
        return False


def _cli(*argv, extra_env=None):
    env = {k: v for k, v in os.environ.items() if k not in KEY_ENVS}
    env["PYTHONPATH"] = os.path.join(ROOT, "src")
    env.update(extra_env or {})
    return subprocess.run(
        [sys.executable, "-m", "revl", "erase-report", FIXTURE, *argv],
        capture_output=True, text=True, env=env)


def _tokens(crossings) -> set:
    return {entry["token"] for bucket in BUCKETS
            for entry in crossings.get(bucket) or []}


@pytest.fixture(scope="module")
def vault_ir():
    return compile_files([FIXTURE])


@pytest.fixture(scope="module")
def report(vault_ir):
    return erase_report.build_report(vault_ir, "vault", prove_residue=False)


@pytest.fixture(scope="module")
def receipt(report, vault_ir):
    return erasure_receipt.make_receipt(
        report, KEY, ir=vault_ir, now=NOW, signer="ops")


# ------------------------------------------------- the replicas the receipt names

def test_every_in_scope_replica_is_named_and_nothing_else_is(report, receipt):
    # the exit criterion's first half: each in-scope replica, named. The set is
    # the report's own crossing set, so the receipt cannot be more or less
    # complete than the measurement it summarises.
    crossings = report["boundaryCrossings"]
    rows = [row for bucket in BUCKETS for row in crossings[bucket] or []]
    assert {row["replica"] for row in receipt["replicas"]} == _tokens(crossings)
    assert len(receipt["replicas"]) == len(rows) == 4
    # `total` counts the IRREVERSIBLE crossings only (emissions + externs +
    # widenings); the class-(a) witnessed bucket is additive and separate, and
    # the receipt folds both into one replica list so nothing measured is left
    # out of the erasure.
    assert crossings["total"] == 3
    assert len(receipt["replicas"]) == \
        crossings["total"] + len(crossings["witnessed"])


def test_a_replica_outside_the_measured_realm_is_never_named(vault_ir, receipt):
    # `other` crosses the boundary in the same composition. A receipt for
    # `vault` that named it would be evidence about something nobody asked to
    # erase.
    other = erase_report.build_report(vault_ir, "other", prove_residue=False)
    named = {row["replica"] for row in receipt["replicas"]}
    assert not (named & _tokens(other["boundaryCrossings"]))
    assert other["boundaryCrossings"]["total"] >= 1, \
        "the fixture must cross the boundary outside `vault` too"


def test_row_carries_the_reports_own_disposition(receipt):
    # no second vocabulary: a disposition the report did not assign would be a
    # second account of the same fact.
    by_replica = {row["replica"]: row for row in receipt["replicas"]}
    assert by_replica["witnessed:VaultApp:stash"]["disposition"] == "revertible"
    assert by_replica["host:VaultApp:put"]["disposition"] == "compensated"
    assert by_replica["host:VaultApp:leak"]["disposition"] == "bare"
    assert by_replica["emit:VaultApp:index.add"]["disposition"] == "bare"


def test_named_channel_comes_from_the_declaration(receipt):
    # the second half of "names them from something real": the channel is the
    # declared inverse, read off the extern the report itself read.
    by_replica = {row["replica"]: row for row in receipt["replicas"]}
    assert by_replica["witnessed:VaultApp:stash"]["inverse"] == "unstash"
    assert by_replica["host:VaultApp:put"]["inverse"] == "restore"
    # a bare crossing names nothing, because the declaration names nothing
    assert by_replica["host:VaultApp:leak"]["inverse"] is None
    assert by_replica["emit:VaultApp:index.add"]["inverse"] is None


def test_every_named_channel_is_a_declared_channel(vault_ir, receipt):
    # an inverse this system invented would be a fabricated recipient. Every
    # channel here is read off a clause the extern itself declares: the `undo`
    # slot for a witnessed row (which the O1 double-close audit also
    # recognises) and the `compensate` slot for a compensated one.
    from revl.resources import _callee_name, closing_ops
    slots = {e["name"]: e for e in vault_ir.get("externs")}
    by_replica = {row["replica"]: row for row in receipt["replicas"]}
    declared_undo = closing_ops(vault_ir.get("externs"))
    assert declared_undo == {"unstash"}
    assert by_replica["witnessed:VaultApp:stash"]["inverse"] == \
        _callee_name(slots["stash"]["undo"]) == "unstash"
    assert by_replica["witnessed:VaultApp:stash"]["inverse"] in declared_undo
    assert by_replica["host:VaultApp:put"]["inverse"] == \
        _callee_name(slots["put"]["compensate"]) == "restore"
    # `restore` is a declared extern in the composition, but a compensate slot
    # is not an `undo` closure, so it is rightly not among the O1 inverses.
    assert "restore" in slots and "restore" not in declared_undo


def test_unresolved_offset_reports_unresolved(vault_ir):
    # a compensated crossing whose offset did not land reads `unresolved` in
    # the receipt, off the report's own third state (item 247 gap 2).
    report = erase_report.build_report(
        vault_ir, "vault", prove_residue=False,
        compensation_residue=[{"component": "VaultApp", "reason": "offset raised"}])
    receipt = erasure_receipt.make_receipt(report, KEY, ir=vault_ir, now=NOW)
    by_replica = {row["replica"]: row for row in receipt["replicas"]}
    assert by_replica["host:VaultApp:put"]["disposition"] == "unresolved"
    assert by_replica["host:VaultApp:put"]["inverse"] == "restore"
    assert receipt["summary"]["byDisposition"]["unresolved"] == 1


def test_receipt_without_the_ir_names_no_channel_rather_than_a_wrong_one(report):
    receipt = erasure_receipt.make_receipt(report, KEY, now=NOW)
    assert all(row["inverse"] is None for row in receipt["replicas"])
    assert {row["disposition"] for row in receipt["replicas"]} == \
        {"revertible", "compensated", "bare"}


def test_in_process_state_is_a_row_of_its_own(report, receipt):
    # the realm's own copy. Unproven here because the static report skips the
    # R4 teardown proof, and `unproven` is the honest reading of "not measured",
    # never of "still there".
    assert receipt["inProcess"]["replica"] == "memory://vault"
    assert receipt["inProcess"]["disposition"] == "unproven"
    assert receipt["inProcess"]["proven"] is None
    assert receipt["inProcess"]["provisionsErased"] == \
        report["inProcessStateGone"]["provisionsErased"]
    assert receipt["summary"]["replicas"] == len(receipt["replicas"]) + 1
    assert receipt["summary"]["byDisposition"]["unproven"] == 1
    # the two vocabularies describe different things and do not overlap: a
    # boundary row states what happened to a crossing, the realm row states
    # what is known about the realm's own copy.
    assert not set(erasure_receipt.DISPOSITIONS) & \
        set(erasure_receipt.IN_PROCESS_DISPOSITIONS)
    assert set(erasure_receipt.ALL_DISPOSITIONS) == \
        set(erasure_receipt.DISPOSITIONS) | \
        set(erasure_receipt.IN_PROCESS_DISPOSITIONS)


@pytest.mark.skipif(not _has_runtime(), reason="needs the cordis runtime")
def test_proven_teardown_reads_reclaimed(vault_ir):
    report = erase_report.build_report(vault_ir, "vault", prove_residue=True)
    receipt = erasure_receipt.make_receipt(report, KEY, ir=vault_ir, now=NOW)
    assert report["inProcessStateGone"]["proven"] is True
    assert receipt["inProcess"]["disposition"] == "reclaimed"


# The proof's own tri-state, as `erase_report._prove_no_residue` builds it: a
# passing R4 teardown, and a teardown that RAN and left residue behind (the case
# `revl erase-report` exits 1 on). A third reading, no proof at all, is
# `prove_residue=False` below. The two hard cases are stubbed at the report's own
# seam so they are testable without the cordis runtime.
PASSING_PROOF = {"available": True, "proven": True,
                 "checks": {"registry": True, "provisions": True,
                            "effects": True, "listeners": True},
                 "detail": {}}
FAILING_PROOF = {"available": True, "proven": False,
                 "checks": {"registry": False, "provisions": True,
                            "effects": True, "listeners": True},
                 "detail": {"registrySize": 1}}


def _report_over_proof(vault_ir, monkeypatch, proof):
    """A report whose R4 proof is `proof`, or the skipped proof when it is
    None. Goes through `build_report`, so `inProcessStateGone` is assembled by
    the code that assembles it rather than by the test."""
    if proof is None:
        return erase_report.build_report(vault_ir, "vault", prove_residue=False)
    monkeypatch.setattr(erase_report, "_prove_no_residue", lambda ir: dict(proof))
    return erase_report.build_report(vault_ir, "vault", prove_residue=True)


@pytest.mark.parametrize("proof,disposition,proven", [
    (PASSING_PROOF, "reclaimed", True),
    (None, "unproven", None),
    (FAILING_PROOF, "residue", False),
])
def test_each_reading_of_the_proof_signs_its_own_disposition(
        monkeypatch, vault_ir, proof, disposition, proven):
    # `proven: False` (the teardown ran and residue survived) and `proven: None`
    # (no proof was taken) are opposite facts about the realm. A receipt that
    # signed both as `unproven` could not tell "it is still there" from "nobody
    # looked", so each reading signs its own disposition and the evidence it
    # rests on travels inside the same signed row.
    report = _report_over_proof(vault_ir, monkeypatch, proof)
    receipt = erasure_receipt.make_receipt(report, KEY, ir=vault_ir, now=NOW)
    row = receipt["inProcess"]
    assert row["disposition"] == disposition
    assert row["proven"] is proven
    assert receipt["summary"]["byDisposition"][disposition] == 1
    assert erasure_receipt.verify_receipt(receipt, KEY) == (True, "")


def test_the_three_proof_readings_do_not_share_a_disposition(monkeypatch,
                                                             vault_ir):
    # any two readings of the proof are two different signed documents, which is
    # what "derivable from the signed body alone" has to mean: a reader cannot
    # be shown one reading and check another.
    signatures = []
    for proof in (PASSING_PROOF, None, FAILING_PROOF):
        report = _report_over_proof(vault_ir, monkeypatch, proof)
        signatures.append(erasure_receipt.make_receipt(
            report, KEY, ir=vault_ir, now=NOW)["signature"])
    assert len(set(signatures)) == 3
    assert erasure_receipt.IN_PROCESS_DISPOSITIONS == \
        ("reclaimed", "unproven", "residue")


def test_the_signed_row_carries_why_it_is_not_reclaimed(monkeypatch, vault_ir):
    # a verifier holding only the receipt must be able to see which it is
    # holding: the reason a proof was never taken, or the residue checks that
    # failed. Both live inside the signed row, so both travel with the receipt.
    skipped = erasure_receipt.make_receipt(
        _report_over_proof(vault_ir, monkeypatch, None), KEY, ir=vault_ir,
        now=NOW)["inProcess"]
    assert skipped["available"] is False
    assert skipped["reason"] == "runtime proof skipped"
    assert skipped["failedChecks"] == []

    residue = erasure_receipt.make_receipt(
        _report_over_proof(vault_ir, monkeypatch, FAILING_PROOF), KEY, ir=vault_ir,
        now=NOW)["inProcess"]
    assert residue["available"] is True
    assert residue["reason"] is None
    assert residue["failedChecks"] == ["registry"]

    text = erasure_receipt.render_receipt(erasure_receipt.make_receipt(
        _report_over_proof(vault_ir, monkeypatch, FAILING_PROOF), KEY, ir=vault_ir,
        now=NOW))
    assert "residue" in text and "registry" in text


def test_a_receipt_cannot_claim_reclaimed_over_residue_evidence(monkeypatch,
                                                               vault_ir):
    # the disposition and the proof reading it summarises are checked against
    # each other, so a body whose row says `reclaimed` while its evidence says
    # `proven: False` is refused rather than printed VALID.
    receipt = erasure_receipt.make_receipt(
        _report_over_proof(vault_ir, monkeypatch, FAILING_PROOF), KEY,
        ir=vault_ir, now=NOW)
    lying = json.loads(json.dumps(receipt))
    lying["inProcess"]["disposition"] = "reclaimed"
    ok, why = erasure_receipt.verify_receipt(lying, KEY)
    assert not ok and "inProcess.disposition" in why
    # and rewriting the WHOLE row to match the lie, evidence and tally alike, is
    # refused by the signature instead: the envelope has nothing left to object
    # to, and a self-consistent lie is still a body the key never MACed.
    rewritten = json.loads(json.dumps(receipt))
    rewritten["inProcess"].update(
        disposition="reclaimed", proven=True, available=True, reason=None,
        failedChecks=[])
    rewritten["summary"]["byDisposition"] = erasure_receipt._disposition_tally(
        rewritten["replicas"], rewritten["inProcess"])
    ok, why = erasure_receipt.verify_receipt(rewritten, KEY)
    assert not ok and "does not match the signature" in why
    assert erasure_receipt.verify_receipt(receipt, KEY) == (True, "")
    # a residue receipt cannot be re-read as "not measured" either
    shrugged = json.loads(json.dumps(receipt))
    shrugged["inProcess"]["disposition"] = "unproven"
    ok, why = erasure_receipt.verify_receipt(shrugged, KEY)
    assert not ok and "inProcess.disposition" in why


def test_a_row_cannot_read_reclaimed_without_a_standing_proof(monkeypatch,
                                                             vault_ir):
    # `reclaimed` is entailed by `proven: true` rather than chosen freely, so a
    # row that claims it over a proof that was never taken is refused, and so is
    # a row whose `proven` is not a proof reading at all.
    receipt = erasure_receipt.make_receipt(
        _report_over_proof(vault_ir, monkeypatch, None), KEY, ir=vault_ir,
        now=NOW)
    assert receipt["inProcess"]["proven"] is None
    free = json.loads(json.dumps(receipt))
    free["inProcess"]["disposition"] = "reclaimed"
    ok, why = erasure_receipt.verify_receipt(free, KEY)
    assert not ok and "inProcess.disposition" in why
    odd = json.loads(json.dumps(receipt))
    odd["inProcess"]["proven"] = "yes"
    ok, why = erasure_receipt.verify_receipt(odd, KEY)
    assert not ok and "inProcess.proven" in why


# The row's own evidence and the summary that counts it, in the only threat
# model that makes these checks load-bearing: the forger HOLDS the key, so every
# edit below is re-MACed and the refusal has to come from the envelope. A MAC
# proves who wrote a body; it does not prove the body means one thing.
def _resigned(receipt, mutate):
    body = json.loads(json.dumps(receipt))
    mutate(body)
    body.pop("signature", None)
    return {**body, "signature": erasure_receipt._mac(body, KEY)}


def _drop_the_failed_checks(body):
    del body["inProcess"]["failedChecks"]


def _blame_a_check_that_never_ran(body):
    body["inProcess"]["failedChecks"] = ["registry"]


def _claim_reclaimed_over_a_skipped_proof(body):
    body["inProcess"]["available"] = False
    body["inProcess"]["reason"] = "runtime proof skipped"


def _claim_reclaimed_over_a_failed_check(body):
    body["inProcess"]["available"] = False
    body["inProcess"]["failedChecks"] = ["registry"]


def _count_a_residue_row_as_reclaimed(body):
    body["summary"]["byDisposition"]["residue"] = 0
    body["summary"]["byDisposition"]["reclaimed"] = 1


def _count_a_row_that_is_not_there(body):
    body["summary"]["byDisposition"]["compensated"] = 3


@pytest.mark.parametrize("proof,mutate,expected", [
    pytest.param(FAILING_PROOF, _drop_the_failed_checks,
                 "inProcess.failedChecks", id="failed-checks-removed"),
    pytest.param(None, _blame_a_check_that_never_ran,
                 "inProcess.failedChecks", id="a-check-where-none-ran"),
    pytest.param(PASSING_PROOF, _claim_reclaimed_over_a_skipped_proof,
                 "reclaimed", id="reclaimed-over-a-skipped-proof"),
    pytest.param(PASSING_PROOF, _claim_reclaimed_over_a_failed_check,
                 "reclaimed", id="reclaimed-over-a-failed-check"),
    pytest.param(FAILING_PROOF, _count_a_residue_row_as_reclaimed,
                 "summary.byDisposition", id="a-residue-row-counted-reclaimed"),
    pytest.param(FAILING_PROOF, _count_a_row_that_is_not_there,
                 "summary.byDisposition", id="a-tally-of-rows-that-are-not-there"),
])
def test_the_envelope_refuses_a_row_or_tally_that_contradicts_itself(
        monkeypatch, vault_ir, proof, mutate, expected):
    # the receipt is honest before the edit and refused after it, with an intact
    # MAC both times, so what refuses it is the envelope reading the document's
    # own members against each other rather than the signature.
    receipt = erasure_receipt.make_receipt(
        _report_over_proof(vault_ir, monkeypatch, proof), KEY, ir=vault_ir,
        now=NOW)
    assert erasure_receipt.verify_receipt(receipt, KEY) == (True, "")
    forged = _resigned(receipt, mutate)
    ok, why = erasure_receipt.verify_receipt(forged, KEY)
    assert not ok, "the envelope accepted a body that contradicts its own row"
    assert "envelope refused" in why, why
    assert expected in why, why


def test_the_same_edit_without_a_re_mac_is_refused_by_the_signature(
        monkeypatch, vault_ir):
    # the other half of the same story: the envelope's checks are not a
    # substitute for the MAC, so an un-re-MACed edit is still refused, by the
    # signature, and the two refusals are distinguishable.
    receipt = erasure_receipt.make_receipt(
        _report_over_proof(vault_ir, monkeypatch, FAILING_PROOF), KEY,
        ir=vault_ir, now=NOW)
    edited = json.loads(json.dumps(receipt))
    del edited["inProcess"]["failedChecks"]
    ok, why = erasure_receipt.verify_receipt(edited, KEY)
    assert not ok and "envelope refused" in why


@pytest.mark.parametrize("proof,disposition", [
    (PASSING_PROOF, "reclaimed"),
    (None, "unproven"),
    (FAILING_PROOF, "residue"),
])
def test_every_honest_reading_still_verifies_after_the_cross_checks(
        monkeypatch, vault_ir, proof, disposition):
    # the control for the refusals above: each of the three readings the row can
    # carry, with the evidence that reading actually has, is accepted, so the
    # cross-checks refuse contradictions rather than the evidence itself. The
    # members they read are asserted here too: a later tightening that starts
    # refusing an honest row has to change this test to do it.
    receipt = erasure_receipt.make_receipt(
        _report_over_proof(vault_ir, monkeypatch, proof), KEY, ir=vault_ir,
        now=NOW)
    row = receipt["inProcess"]
    assert row["disposition"] == disposition
    assert isinstance(row["available"], bool)
    assert isinstance(row["failedChecks"], list)
    assert (row["reason"] is None) == (row["available"] is True)
    assert erasure_receipt.verify_receipt(receipt, KEY) == (True, "")
    assert erasure_receipt.verify_receipt(
        json.loads(json.dumps(receipt)), KEY) == (True, "")


# ------------------------------------------------------ the signature

def test_receipt_round_trips_against_its_key_and_its_report(report, receipt):
    assert erasure_receipt.verify_receipt(receipt, KEY) == (True, "")


def test_wrong_key_is_refused_by_fingerprint(report, receipt):
    ok, why = erasure_receipt.verify_receipt(receipt, b"another-key")
    assert not ok
    assert "signed by key" in why and receipt["key_id"] in why


def test_an_altered_replica_row_breaks_the_signature(report, receipt):
    # a WELL-FORMED edit, so the refusal comes from the signature and not from
    # the envelope: the row keeps a disposition a boundary row may carry.
    tampered = json.loads(json.dumps(receipt))
    original = tampered["replicas"][0]["disposition"]
    replacement = next(d for d in erasure_receipt.DISPOSITIONS if d != original)
    tampered["replicas"][0]["disposition"] = replacement
    # the tally is moved with the row, so the envelope has nothing to object to
    # and the refusal is the MAC's: an edit that keeps the document consistent
    # with itself is still an edit.
    tampered["summary"]["byDisposition"] = erasure_receipt._disposition_tally(
        tampered["replicas"], tampered["inProcess"])
    ok, why = erasure_receipt.verify_receipt(tampered, KEY)
    assert not ok and "does not match the signature" in why


def test_an_altered_report_stops_matching_its_receipt(report, receipt):
    # the whole reason the report hash is inside the signed body: a report
    # edited after issue is a report no receipt vouches for.
    edited = json.loads(json.dumps(report))
    edited["summary"]["bareCrossings"] = 0
    ok, why = erasure_receipt.verify_receipt(receipt, KEY, report=edited)
    assert not ok and "report binding refused" in why
    assert erasure_receipt.verify_receipt(receipt, KEY, report=report) == (True, "")


def test_the_receipt_riding_inside_its_report_still_verifies(report, receipt):
    # the embedded form is the CLI's form; the binding is over the report as it
    # stood before the receipt was attached, so it stays verifiable.
    embedded = {**report, "receipt": receipt}
    assert erasure_receipt.verify_receipt(receipt, KEY, report=embedded) == (True, "")


def test_a_relabelled_document_is_refused_by_the_envelope(receipt):
    for member, value, expected in (
            ("kind", "revl.attestation", "revl.erasure-receipt"),
            ("kind", "revl.deploy.receipt", "revl.erasure-receipt"),
            ("version", "2.0", "1.0"),
            ("sign_alg", "ed25519", "hmac-sha256"),
    ):
        mislabelled = {**receipt, member: value}
        ok, why = erasure_receipt.verify_receipt(mislabelled, KEY)
        assert not ok and "envelope refused" in why and expected in why
    bad_row = json.loads(json.dumps(receipt))
    bad_row["replicas"][0]["disposition"] = "deleted-forever"
    ok, why = erasure_receipt.verify_receipt(bad_row, KEY)
    assert not ok and "disposition" in why
    # the two vocabularies are not each other: a boundary row cannot claim an
    # in-process state, and the realm's own row cannot claim a crossing state.
    crossed = json.loads(json.dumps(receipt))
    crossed["replicas"][0]["disposition"] = "reclaimed"
    ok, why = erasure_receipt.verify_receipt(crossed, KEY)
    assert not ok and "replicas[].disposition" in why
    in_state = json.loads(json.dumps(receipt))
    in_state["inProcess"]["disposition"] = "revertible"
    ok, why = erasure_receipt.verify_receipt(in_state, KEY)
    assert not ok and "inProcess.disposition" in why
    missing_state = json.loads(json.dumps(receipt))
    del missing_state["inProcess"]
    ok, why = erasure_receipt.verify_receipt(missing_state, KEY)
    assert not ok and "inProcess" in why


def test_a_mac_from_another_protocol_does_not_verify(report, vault_ir):
    # three signed protocols, three domains. The same key material, the same
    # canonical serialization, and a MAC that must not cross over.
    body = erasure_receipt.build_body(report, vault_ir, now=NOW, key=KEY)
    forged = {**body, "signature": attest._sign(body, KEY)}
    ok, why = erasure_receipt.verify_receipt(forged, KEY)
    assert not ok and "does not match the signature" in why


def test_an_erasure_receipt_is_not_an_attestation(receipt):
    # the two protocols have different envelopes, so a receipt cannot be read
    # as an attestation: the refusal happens before any MAC is compared.
    ok, why = attest.verify_attestation(receipt, KEY)
    assert not ok and "missing required member" in why


def test_signing_is_deterministic_given_now(report, vault_ir):
    first = erasure_receipt.make_receipt(report, KEY, ir=vault_ir, now=NOW)
    second = erasure_receipt.make_receipt(report, KEY, ir=vault_ir, now=NOW)
    assert first == second
    later = erasure_receipt.make_receipt(
        report, KEY, ir=vault_ir, now="2026-01-02T00:00:00+00:00")
    assert later["signature"] != first["signature"]


def test_a_non_ascii_signer_recomputes_the_bytes_a_verifier_would(report,
                                                                  vault_ir):
    # `signer` is free text, so a receipt can carry a name that is not ASCII.
    # The canonical bytes are `attest._canonical_bytes`'s, so a verifier who
    # canonicalises the way docs/revl-attest.md documents recomputes exactly the
    # bytes the signer MACed. A second reading of the construction (escaping the
    # non-ASCII name) hashed different bytes, and every such receipt was
    # reported invalid by anyone who followed the documentation.
    signer = "Jos\u00e9 M\u00fcller"
    receipt = erasure_receipt.make_receipt(
        report, KEY, ir=vault_ir, now=NOW, signer=signer)
    assert receipt["signer"] == signer
    assert erasure_receipt.verify_receipt(receipt, KEY) == (True, "")
    body = {k: v for k, v in receipt.items() if k != "signature"}
    canonical = attest._canonical_bytes(body)
    assert erasure_receipt._mac(body, KEY) == hmac.new(
        KEY, erasure_receipt.RECEIPT_DOMAIN + canonical,
        hashlib.sha256).hexdigest()
    # the two spellings really do differ here, which is what made the divergence
    # observable to a third party, and only to a third party
    assert canonical != json.dumps(body, sort_keys=True, ensure_ascii=True,
                                   separators=(",", ":")).encode("utf-8")
    # a receipt that travelled through JSON is the same receipt
    assert erasure_receipt.verify_receipt(json.loads(json.dumps(receipt)), KEY) \
        == (True, "")


def test_an_unknown_realm_is_never_signed(vault_ir):
    bad = erase_report.build_report(vault_ir, "ghost", prove_residue=False)
    assert bad["ok"] is False
    with pytest.raises(RevlError) as err:
        erasure_receipt.make_receipt(bad, KEY)
    assert "no completed erase report to sign" in str(err.value)


def test_an_empty_key_is_refused(report):
    with pytest.raises(RevlError):
        erasure_receipt.make_receipt(report, b"")


def test_a_signer_that_has_no_utf8_spelling_is_refused_as_a_revl_error(
        report, vault_ir):
    # `signer` reaches the signature through the canonical bytes, and
    # `--receipt-signer $'ops-\xff\xfe'` arrives as lone surrogates, which have
    # no encoding. That raised attest.NotCanonicalizable, which is a ValueError
    # and NOT a RevlError, so it escaped a caller whose only answer is
    # `except RevlError` and the CLI printed a traceback with no report. The
    # refusal now happens here, as the error type this module documents, and it
    # names the flag: a body that cannot be MACed is a refusal to produce a
    # receipt, not a receipt with an unusable signature, so nothing is returned.
    assert not issubclass(attest.NotCanonicalizable, RevlError)
    undecodable = "ops-\udcff\udcfe"
    with pytest.raises(RevlError) as err:
        erasure_receipt.make_receipt(
            report, KEY, ir=vault_ir, now=NOW, signer=undecodable)
    message = str(err.value)
    assert "receipt-signer" in message, message
    assert "no conforming verifier could recompute" in message, message
    # a name that IS text is not this case: it is signed, and it verifies
    encodable = "ops-\u00e9"
    assert erasure_receipt.verify_receipt(erasure_receipt.make_receipt(
        report, KEY, ir=vault_ir, now=NOW, signer=encodable), KEY) == (True, "")


# -------------------------------------------------------------- the key rule

def test_no_key_is_an_error_and_never_a_hardcoded_default():
    with pytest.raises(RevlError) as err:
        erasure_receipt.resolve_key(None, env={})
    assert "REVL_ERASURE_KEY" in str(err.value)
    assert erasure_receipt.key_from_env({}) is False


def test_key_resolution_order_is_path_then_file_then_secret(tmp_path):
    key_file = tmp_path / "receipt.key"
    key_file.write_bytes(b"from-the-file")
    assert erasure_receipt.resolve_key(
        None, env={"REVL_ERASURE_KEY_FILE": str(key_file)}) == b"from-the-file"
    assert erasure_receipt.resolve_key(
        None, env={"REVL_ERASURE_KEY": "inline-secret"}) == b"inline-secret"
    assert erasure_receipt.resolve_key(
        str(key_file), env={"REVL_ERASURE_KEY": "inline-secret"}
    ) == b"from-the-file"
    assert erasure_receipt.key_from_env({"REVL_ERASURE_KEY": "x"}) is True


def test_a_secret_that_is_not_text_names_the_route_that_carries_bytes():
    # REVL_ERASURE_KEY is documented as the secret bytes directly, but the
    # environment hands the value over as text, so raw bytes in it arrive as
    # lone surrogates, which have no encoding. `inline.encode("utf-8")` raised
    # UnicodeEncodeError, which is a ValueError and not a RevlError, so the CLI
    # printed a traceback instead of an answer. The answer names the file route,
    # which carries any bytes.
    assert not issubclass(UnicodeEncodeError, RevlError)
    with pytest.raises(RevlError) as err:
        erasure_receipt.resolve_key(
            None, env={"REVL_ERASURE_KEY": "\udcff\udcfeabc"})
    message = str(err.value)
    assert "REVL_ERASURE_KEY" in message, message
    assert "not UTF-8" in message, message
    assert "REVL_ERASURE_KEY_FILE" in message, message
    # a secret that IS text is unchanged, UTF-8 and all, so this refuses the
    # values that cannot be a key rather than the ones that are not ASCII
    assert erasure_receipt.resolve_key(
        None, env={"REVL_ERASURE_KEY": "cl\u00e9"}) == "cl\u00e9".encode("utf-8")


def test_key_fingerprints_are_domain_separated():
    # an erasure key fingerprint and an attestation fingerprint of the same
    # bytes are different strings, so a key id can never be cross-read.
    assert erasure_receipt.key_id(KEY) != attest.key_id(KEY)


def test_a_key_file_means_the_same_thing_to_both_protocols(tmp_path, report,
                                                           vault_ir):
    # The key file rule is `attest`'s, not a second reading of it. A key written
    # with `echo` carries a trailing newline; reading it verbatim made the same
    # file two different keys, so the same receipt MACed twice and fingerprinted
    # twice depending on which protocol resolved it.
    key_file = tmp_path / "receipt.key"
    key_file.write_bytes(KEY + b"\n")
    assert erasure_receipt.load_key(str(key_file)) == attest.load_key(
        str(key_file)) == KEY
    assert erasure_receipt.resolve_key(str(key_file)) == KEY
    assert erasure_receipt.key_id(erasure_receipt.resolve_key(str(key_file))) \
        == erasure_receipt.key_id(KEY)
    receipt = erasure_receipt.make_receipt(
        report, erasure_receipt.resolve_key(str(key_file)), ir=vault_ir, now=NOW)
    assert erasure_receipt.verify_receipt(
        receipt, attest.resolve_key(str(key_file))) == (True, "")


# ---------------------------------------------------------------- the CLI

def test_cli_without_a_key_emits_the_report_it_always_did():
    proc = _cli("--realm", "vault", "--json", "--no-residue-proof")
    assert proc.returncode == 0, proc.stderr
    doc = json.loads(proc.stdout)
    assert doc["ok"] and doc["kind"] == "revl.erase-report"
    assert "receipt" not in doc


def test_cli_signs_the_erasure_request_and_the_receipt_verifies(tmp_path):
    key_file = tmp_path / "receipt.key"
    key_file.write_bytes(KEY)
    proc = _cli("--realm", "vault", "--json", "--no-residue-proof",
                "--receipt-key", str(key_file), "--receipt-signer", "ops")
    assert proc.returncode == 0, proc.stderr
    doc = json.loads(proc.stdout)
    receipt = doc["receipt"]
    assert receipt["signer"] == "ops"
    assert receipt["realm"] == "vault"
    assert receipt["report"]["hash"] == \
        erasure_receipt.report_hash(doc)
    assert erasure_receipt.verify_receipt(receipt, KEY, report=doc) == (True, "")
    assert {row["replica"] for row in receipt["replicas"]} == \
        _tokens(doc["boundaryCrossings"])
    assert receipt["signature"] in proc.stdout


def test_cli_human_output_states_scope_and_every_replica(tmp_path):
    key_file = tmp_path / "receipt.key"
    key_file.write_bytes(KEY)
    proc = _cli("--realm", "vault", "--no-residue-proof",
                "--receipt-key", str(key_file))
    assert proc.returncode == 0, proc.stderr
    text = proc.stdout
    assert "ERASURE RECEIPT: realm `vault`" in text
    assert "DOES NOT PROVE" in text
    for row in ("revertible", "compensated", "bare", "unproven"):
        assert row in text
    assert "witnessed:VaultApp:stash [fs] via unstash" in text


def test_cli_resolves_the_key_from_the_environment(tmp_path):
    key_file = tmp_path / "receipt.key"
    key_file.write_bytes(KEY)
    proc = _cli("--realm", "vault", "--json", "--no-residue-proof",
                extra_env={"REVL_ERASURE_KEY_FILE": str(key_file)})
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["receipt"]["key_id"] == \
        erasure_receipt.key_id(KEY)


def test_cli_reports_an_unreadable_receipt_key_rather_than_crashing(tmp_path):
    # every other verb answers a file it cannot read with a message on stderr
    # and a nonzero exit. This one let the error escape as a traceback out of
    # the receipt construction, which is not an answer an operator can act on.
    missing = tmp_path / "definitely-missing.key"
    proc = _cli("--realm", "vault", "--json", "--no-residue-proof",
                "--receipt-key", str(missing))
    assert "Traceback" not in proc.stderr
    assert proc.returncode == 1, proc.stderr
    assert proc.stderr.startswith("error: "), proc.stderr
    assert "definitely-missing.key" in proc.stderr
    assert proc.stdout == ""


def test_cli_refuses_an_undecodable_receipt_signer_without_a_traceback(tmp_path):
    # the signer is recorded in the signed body, so a name with no UTF-8
    # spelling cannot be signed. Asking for one is an error rather than a receipt
    # with a signature no conforming verifier could recompute, and an error
    # rather than a traceback. No report is printed either: the artifact the run
    # was asked for does not exist, which is what the missing-key test above
    # already pins for the other refusal.
    key_file = tmp_path / "receipt.key"
    key_file.write_bytes(KEY)
    proc = _cli("--realm", "vault", "--json", "--no-residue-proof",
                "--receipt-key", str(key_file),
                "--receipt-signer", "ops-\udcff\udcfe")
    assert "Traceback" not in proc.stderr, proc.stderr
    assert proc.returncode == 1, proc.stderr
    assert proc.stderr.startswith("error: "), proc.stderr
    assert "receipt-signer" in proc.stderr, proc.stderr
    assert proc.stdout == ""


def test_cli_signs_and_verifies_a_non_ascii_receipt_signer(tmp_path):
    # docs/design/472-retention-erasure-receipts.md advertises a non-ASCII
    # signer, so it has to sign and verify. A name that is encodable is not the
    # refusal above: the canonical bytes are UTF-8, so a verifier who canonicalises
    # the documented way recomputes them.
    key_file = tmp_path / "receipt.key"
    key_file.write_bytes(KEY)
    proc = _cli("--realm", "vault", "--json", "--no-residue-proof",
                "--receipt-key", str(key_file),
                "--receipt-signer", "Jos\u00e9 M\u00fcller")
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)["receipt"]
    assert receipt["signer"] == "Jos\u00e9 M\u00fcller"
    assert erasure_receipt.verify_receipt(receipt, KEY) == (True, "")


def test_cli_refuses_a_receipt_key_that_is_not_text_without_a_traceback():
    # the same class of answer, at the call site the last fix touched:
    # REVL_ERASURE_KEY is the secret bytes directly, but the environment hands
    # the value over as text, so raw bytes in it have no encoding. This printed a
    # traceback at the first revision and at the fix commit alike, and an
    # unresolved key is not a different kind of failure from a key file that
    # cannot be read.
    proc = _cli("--realm", "vault", "--json", "--no-residue-proof",
                extra_env={"REVL_ERASURE_KEY": "\udcff\udcfeabc"})
    assert "Traceback" not in proc.stderr, proc.stderr
    assert proc.returncode == 1, proc.stderr
    assert proc.stderr.startswith("error: "), proc.stderr
    assert "REVL_ERASURE_KEY" in proc.stderr, proc.stderr
    assert proc.stdout == ""


# ------------------------------------------------------------ the honest scope

def test_scope_states_what_the_receipt_cannot_reach(receipt):
    scope = receipt["scope"]
    assert scope["proves"] and scope["doesNotProve"]
    blob = " ".join(scope["doesNotProve"]).lower()
    # names its own boundary: copies made outside are not replicas it can see
    assert "copies the system cannot see" in blob
    # and does not claim the retention half of item 472 that does not exist
    assert "retention deadlines" in blob
    assert "472-retention-erasure-receipts.md" in scope["reference"]
    # the receipt is committed as a whole, so the scope travels with it
    assert "scope" in receipt


def test_render_is_readable_and_ends_with_the_signature(receipt):
    text = erasure_receipt.render_receipt(receipt)
    assert text.startswith("ERASURE RECEIPT: realm `vault`")
    assert text.rstrip().endswith(receipt["signature"])
    assert erasure_receipt.render_receipt({"kind": "nope"}).startswith("error:")
