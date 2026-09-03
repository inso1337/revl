"""An attestation may only say what was measured (roadmap item 127, the
truthfulness fixes).

An adversarial audit of `revl.attest` found that the record revl signs was not,
in three separate ways, a statement about anything that happened:

  * **the `G1..G9` claim was a CONSTANT.** `make_attestation` checked two things
    before signing all nine composition guarantees — that the key was non-empty
    and that `ir["holes"]` was empty — and never compiled, never called the
    checker, never read a gate verdict. `attest({})` signed the full list over
    an empty dict, and an IR the compiler refuses BY NAME for violating G2
    ("provision conflict: key `db` is provided by both A and B (G2)") carried a
    valid signature asserting G2 held, which `deploy.admit` then ACCEPTED.
  * **`sign_alg` was algorithm confusion.** `deploy.admit` gated its
    cross-domain refusal on `attestation["sign_alg"] == SIGN_ALG`, while
    `verify_attestation` never looked at `sign_alg` at all and `_sign` was
    unconditionally HMAC whatever the field said. Relabel it `ed25519` (or
    `''`, or `HMAC-SHA256`, or `None`) and the one guard keeping a symmetric
    signature honest across a trust boundary evaporated — no forgery skill
    required, because under HMAC every verifier is already a key holder.
  * **the receipt and the attestation shared a MAC construction.** Both were
    `hmac(key, canonical(body minus signature))` with no domain tag, and
    `verify_attestation` checked neither `kind` nor `verdict`, so a deploy
    ACCEPT receipt verified as a valid attestation and vice versa — and
    `revl attest --verify` printed VALID for a record whose verdict read
    `REJECTED-by-the-gauntlet` over guarantees `G10-does-not-exist`, `SOC2`,
    `FIPS-140-3`.

These tests are the audit's reproducers, kept executable. Each one PASSED (the
exploit worked) before the fix and fails the exploit now. The last suite pins
the audit's NEGATIVE results — the properties that were already sound and must
stay sound while the signed body changes shape.
"""

import copy
import hmac
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from revl import attest, deploy            # noqa: E402
from revl.compiler import compile_source   # noqa: E402
from revl.errors import RevlError          # noqa: E402

KEY = b"item-127-truthfulness-signer"
HOST_KEY = b"item-127-truthfulness-host"
NOW = "2026-09-01T00:00:00+00:00"

ADMISSIBLE = """\
service Database { emission fn execute(sql: Str) -> Int }

component A provides db: Database {
  provide db { fn execute(sql) = 1 }
}
component Front requires db: Database { }
"""

# the same composition with `db` provided TWICE — the compiler refuses it by
# name, citing G2.
G2_VIOLATING = """\
service Database { emission fn execute(sql: Str) -> Int }

component A provides db: Database {
  provide db { fn execute(sql) = 1 }
}
component B provides db: Database {
  provide db { fn execute(sql) = 2 }
}
component Front requires db: Database { }
"""


def _gate(source: str = ADMISSIBLE):
    return attest.run_gate(source=source, filename="<test>")


def _att(source: str = ADMISSIBLE, **kw) -> dict:
    verdict = _gate(source)
    return attest.make_attestation(verdict.ir, KEY, verdict=verdict, now=NOW,
                                   **kw)


def _resign(att: dict, **changes) -> dict:
    """Re-sign a modified body with the signing key — the realistic adversary,
    since under HMAC every verifier already holds the key. A test that only
    EDITED a field would prove nothing here: it would fail on the MAC and never
    reach the property under test."""
    body = {k: v for k, v in att.items() if k != attest.SIGNATURE_FIELD}
    body.update(changes)
    return {**body, attest.SIGNATURE_FIELD: attest._sign(body, KEY)}


# ---------------------------------------------------------------------------
# F2. the guarantee list is a measurement, not a constant
# ---------------------------------------------------------------------------


def test_attesting_an_empty_ir_is_refused():
    """`make_attestation({})` used to return a signed record admitting
    G1..G9 that verified True."""
    with pytest.raises(RevlError) as caught:
        attest.make_attestation({}, KEY, now=NOW)
    assert "gate verdict" in str(caught.value)


def test_attesting_an_empty_ir_under_a_real_verdict_is_still_refused():
    """And a REAL verdict for some other composition cannot be laundered onto
    it: the verdict's hash must be the hash of the IR being signed."""
    with pytest.raises(RevlError) as caught:
        attest.make_attestation({}, KEY, verdict=_gate(), now=NOW)
    assert "different composition" in str(caught.value)


def test_the_compiler_refuses_the_g2_composition_by_name():
    """The premise of the next test: this composition is not a borderline case,
    the reference compiler names G2 when it refuses it."""
    with pytest.raises(RevlError) as caught:
        compile_source(G2_VIOLATING)
    assert "provided by both" in str(caught.value)
    assert "(G2)" in str(caught.value)


def test_a_g2_violating_composition_cannot_be_attested():
    """The headline exploit: an IR the compiler refuses FOR VIOLATING G2 used to
    carry a valid signature asserting G2 holds."""
    verdict = attest.run_gate(source=G2_VIOLATING, filename="<test>")
    assert verdict.admitted is False
    assert "(G2)" in verdict.reason

    # the hand-built IR of that refused composition — what the audit signed
    refused_ir = _refused_ir()
    with pytest.raises(RevlError) as caught:
        attest.make_attestation(refused_ir, KEY, verdict=verdict, now=NOW)
    assert "did not admit" in str(caught.value)

    # and it cannot be signed by borrowing an admissible composition's verdict
    with pytest.raises(RevlError) as caught:
        attest.make_attestation(refused_ir, KEY, verdict=_gate(), now=NOW)
    assert "different composition" in str(caught.value)


def _refused_ir() -> dict:
    """The IR of the G2-violating composition, hand-built the way the audit did
    (the compiler will not produce one, which is the point)."""
    ir = copy.deepcopy(compile_source(ADMISSIBLE))
    duplicate = copy.deepcopy(
        next(c for c in ir["components"] if c["name"] == "A"))
    duplicate["name"] = "B"
    ir["components"].append(duplicate)
    ir["manifest"]["components"].append(
        {**copy.deepcopy(ir["manifest"]["components"][0]), "name": "B"})
    return ir


def test_the_guarantee_list_is_derived_from_the_shipped_ruleset():
    """Not `sorted()` over the diagnostics catalogue: the codes are the ones the
    frontend ruleset modules actually cite, and the catalogue is only the
    vocabulary they must be drawn from."""
    assert attest.discharged_guarantees() == _att()["guarantees"]
    assert set(attest.discharged_guarantees()) <= set(attest.catalogued_guarantees())
    # `lower.py` is the checker; every attested code is cited by the ruleset
    ruleset = (SRC / "revl" / "lower.py").read_text(encoding="utf-8")
    for code in attest.discharged_guarantees():
        assert f"({code})" in ruleset, f"{code} is attested but the checker never cites it"


def test_the_signed_body_names_which_checker_asserted_it():
    att = _att()
    assert att["checker"]["compiler"] == attest.compiler_version()
    assert att["checker"]["ruleset"] == attest.ruleset_digest()
    assert len(att["checker"]["ruleset"]) == 64
    # and the identity is INSIDE the signature, so it cannot be restated
    forged = dict(att)
    forged["checker"] = {"compiler": "9.9.9", "ruleset": "0" * 64}
    assert attest.verify_attestation(forged, KEY)[0] is False


def test_a_ruleset_that_stops_citing_a_rule_stops_attesting_it(monkeypatch):
    """The link that makes the list a measurement rather than a table: drop a
    rule from what the ruleset cites and it drops out of what is attested."""
    monkeypatch.setattr(attest, "_ruleset_cache",
                        ("f" * 64, ("G1", "G3", "G4")))
    assert attest.discharged_guarantees() == ["G1", "G3", "G4"]


# ---------------------------------------------------------------------------
# F1. sign_alg is validated, not merely recorded
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("label", ["ed25519", "", "HMAC-SHA256", None,
                                   "hmac-sha512"])
def test_a_relabelled_sign_alg_is_refused(label):
    """Every one of these verified True and ACCEPTed at admission before."""
    relabelled = _resign(_att(), sign_alg=label)
    ok, reason = attest.verify_attestation(relabelled, KEY)
    assert ok is False
    assert "sign_alg" in reason


def test_the_envelope_members_that_carry_meaning_are_all_checked():
    for member, value in (("kind", "revl.deploy.receipt"),
                          ("version", "1.0"),
                          ("verdict", "REJECTED-by-the-gauntlet"),
                          ("hash_alg", "md5"),
                          ("guarantees", ["G10-does-not-exist", "SOC2"]),
                          ("guarantees", ["G9", "G1"]),   # unsorted
                          ("guarantees", []),
                          ("checker", None),
                          ("checker", {"compiler": "2.0.0", "ruleset": "nope"}),
                          ("timestamp", "whenever"),
                          ("composition_hash", "not-a-hash"),
                          ("key_id", "short")):
        ok, reason = attest.verify_attestation(_resign(_att(), **{member: value}), KEY)
        assert ok is False, f"{member}={value!r} was accepted"
        assert "envelope refused" in reason


def test_a_v1_attestation_does_not_verify_as_a_v2_one():
    """The signed body changed shape and the guarantee list changed MEANING, so
    a v1 record is a different claim, not a v2 record missing a field. It is
    refused rather than read as if it had been measured."""
    ok, reason = attest.verify_attestation(_resign(_att(), version="1.0"), KEY)
    assert ok is False
    assert "version" in reason


# ---------------------------------------------------------------------------
# F9. domain separation between the receipt MAC and the attestation MAC
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def staged(tmp_path_factory):
    """A real bundle plus its deploy attestation — the item-118 chain."""
    from revl.bundle import build_bundle

    tmp = tmp_path_factory.mktemp("attest-truth")
    src = tmp / "app.rvl"
    src.write_text(ADMISSIBLE, encoding="utf-8")
    out = tmp / "app.revlbundle"
    build_bundle([str(src)], str(out), backends=("python",), env={})
    att = deploy.make_deploy_attestation(out, KEY, signer="ci")
    return out, att


def _trust(**over):
    base = {"keys": {attest.key_id(KEY): KEY}, "backend": "python"}
    base.update(over)
    return deploy.TrustStore(**base)


def test_an_admission_receipt_does_not_verify_as_an_attestation(staged):
    bundle, att = staged
    receipt = deploy.admit(bundle, trust=_trust(), attestation=att, host_key=KEY)
    assert receipt["verdict"] == deploy.ACCEPT
    ok, _reason = attest.verify_attestation(receipt, KEY)
    assert ok is False


def test_an_attestation_does_not_verify_as_an_admission_receipt(staged):
    _bundle, att = staged
    ok, _reason = deploy.verify_receipt(att, KEY)
    assert ok is False


def test_the_two_macs_are_domain_separated(staged):
    """Not merely different because the bodies differ: the SAME body under the
    same key MACs differently in the two protocols."""
    _bundle, att = staged
    body = {k: v for k, v in att.items() if k != attest.SIGNATURE_FIELD}
    assert attest._sign(body, KEY) != deploy._receipt_mac(body, KEY)
    assert attest.SIGN_DOMAIN != deploy.RECEIPT_DOMAIN


def test_a_receipt_with_a_foreign_kind_is_refused(staged):
    bundle, att = staged
    receipt = deploy.admit(bundle, trust=_trust(), attestation=att, host_key=KEY)
    lying = {k: v for k, v in receipt.items() if k != "signature"}
    lying["kind"] = attest.ATTEST_KIND
    lying["signature"] = deploy._receipt_mac(lying, KEY)
    ok, reason = deploy.verify_receipt(lying, KEY)
    assert ok is False
    assert "kind" in reason


# ---------------------------------------------------------------------------
# F1 at the admission boundary, and the receiver's own gate run
# ---------------------------------------------------------------------------


def test_a_relabelled_sign_alg_no_longer_buys_a_cross_domain_admission(staged):
    """The exploit end to end: honest `hmac-sha256` REFUSEd (signer-untrusted),
    relabelled `ed25519` ACCEPTed with a receipt. Both refuse now, and the
    refusal no longer reads the attestation's own claim about itself."""
    bundle, att = staged
    trust = _trust(cross_domain=True)
    honest = deploy.admit(bundle, trust=trust, attestation=att, host_key=HOST_KEY)
    assert honest["verdict"] == deploy.REFUSE
    assert honest["link"] == deploy.LINK_SIGNER

    relabelled = _resign(att, sign_alg="ed25519")
    receipt = deploy.admit(bundle, trust=trust, attestation=relabelled,
                           host_key=HOST_KEY)
    assert receipt["verdict"] == deploy.REFUSE
    assert receipt["link"] == deploy.LINK_SIGNER
    # and it is refused by a single-domain receiver too, on the envelope
    single = deploy.admit(bundle, trust=_trust(), attestation=relabelled)
    assert single["verdict"] == deploy.REFUSE
    assert single["link"] == deploy.LINK_SIGNATURE


def test_signing_a_bundle_reruns_the_frontend_over_its_own_source(staged, tmp_path):
    """`make_deploy_attestation` signs the STAGED IR, so it re-runs the gate
    over the STAGED SOURCE and refuses unless that run admits and reproduces the
    staged IR — a doctored `ir/ir.json` cannot be signed."""
    import shutil

    bundle, _att = staged
    copied = tmp_path / "copy.revlbundle"
    shutil.copytree(bundle, copied)
    ir_path = copied / deploy.IR_DOCUMENT
    ir = json.loads(ir_path.read_text(encoding="utf-8"))
    ir["components"][0] = {**ir["components"][0], "name": "Smuggled"}
    ir_path.write_text(json.dumps(ir), encoding="utf-8")
    with pytest.raises(RevlError) as caught:
        deploy.make_deploy_attestation(copied, KEY)
    assert "different composition" in str(caught.value)


def test_a_receiver_can_re_run_the_gate_itself(staged, tmp_path):
    """`TrustStore.recheck_source`: admission re-hashes the IR, but a hash is not
    a verdict. Opted in, the receiver's own frontend must admit the staged
    source and reproduce the staged IR."""
    import shutil

    bundle, att = staged
    assert deploy.admit(bundle, trust=_trust(recheck_source=True),
                        attestation=att)["verdict"] == deploy.ACCEPT

    copied = tmp_path / "unadmittable.revlbundle"
    shutil.copytree(bundle, copied)
    source = next((copied / deploy.SOURCE_ROOT).glob("*.rvl"))
    source.write_text(G2_VIOLATING, encoding="utf-8")
    receipt = deploy.admit(copied, trust=_trust(recheck_source=True),
                           attestation=att)
    assert receipt["verdict"] == deploy.REFUSE
    assert receipt["link"] == deploy.LINK_COMPOSITION
    assert "NOT admitted" in receipt["reason"]
    # and with the flag off, the same bundle is admitted on the signer's word:
    # the honest statement of what leaving it off costs.
    assert deploy.admit(copied, trust=_trust(),
                        attestation=att)["verdict"] == deploy.ACCEPT


# ---------------------------------------------------------------------------
# the CLI check
# ---------------------------------------------------------------------------


def _cli(*args, tmp_path):
    keyf = tmp_path / "k.key"
    keyf.write_bytes(KEY)
    proc = subprocess.run(
        [sys.executable, "-m", "revl", *args, "--key", str(keyf)],
        capture_output=True, text=True, cwd=str(ROOT),
        env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(SRC)})
    return proc


def test_cli_verify_refuses_a_bogus_verdict_and_guarantee_list(tmp_path):
    """`revl attest --verify` printed VALID, exit 0, for a record whose verdict
    read `REJECTED-by-the-gauntlet` over `G10-does-not-exist`/`SOC2`/
    `FIPS-140-3`."""
    bogus = _resign(_att(), verdict="REJECTED-by-the-gauntlet",
                    guarantees=["FIPS-140-3", "G10-does-not-exist", "SOC2"])
    path = tmp_path / "bogus.json"
    path.write_text(json.dumps(bogus, indent=2), encoding="utf-8")
    proc = _cli("attest", str(path), "--verify", tmp_path=tmp_path)
    assert proc.returncode == 1
    assert "VALID" not in proc.stdout.replace("INVALID", "")
    assert "INVALID" in proc.stdout

    # the guarantee list alone, with an honest verdict, is refused too
    bogus_codes = _resign(_att(), guarantees=["FIPS-140-3", "SOC2"])
    path.write_text(json.dumps(bogus_codes, indent=2), encoding="utf-8")
    proc = _cli("attest", str(path), "--verify", tmp_path=tmp_path)
    assert proc.returncode == 1
    assert "does not define" in proc.stdout


def test_cli_refuses_to_attest_a_precompiled_ir_document(tmp_path):
    """`revl attest <ir.json>` used to load the IR and sign the full guarantee
    list over a document nothing had checked. Signing runs the gate, and an IR
    document carries no source to run it over."""
    ir_path = tmp_path / "ir.json"
    ir_path.write_text(json.dumps(compile_source(ADMISSIBLE)), encoding="utf-8")
    proc = _cli("attest", str(ir_path), tmp_path=tmp_path)
    assert proc.returncode == 1
    assert "nothing to attest" in proc.stderr


def test_cli_refuses_to_attest_a_composition_the_frontend_refuses(tmp_path):
    bad = tmp_path / "bad.rvl"
    bad.write_text(G2_VIOLATING, encoding="utf-8")
    proc = _cli("attest", str(bad), tmp_path=tmp_path)
    assert proc.returncode == 1
    assert "(G2)" in proc.stderr


# ---------------------------------------------------------------------------
# the audit's NEGATIVE results — sound before, and they stay sound
# ---------------------------------------------------------------------------


def test_no_reachable_second_preimage_on_the_canonical_encoding():
    """Length-framed by JSON's own structure: a value cannot be split across a
    key boundary to collide with a different document."""
    seen = {}
    for doc in ({"a": "b:c"}, {"a:b": "c"}, {"a": "b", "c": ""},
                {"ab": "c"}, {"a": {"b": "c"}}, {"a": ["b", "c"]},
                {"a": "bc"}, {"a": 1}, {"a": "1"}, {"a": True}):
        blob = attest._canonical_bytes(doc)
        assert blob not in seen, f"{doc} collides with {seen.get(blob)}"
        seen[blob] = doc


def test_nfc_and_nfd_spellings_are_different_compositions():
    """No unicode normalization anywhere in the encoder, so two spellings that
    look identical hash differently rather than silently aliasing."""
    import unicodedata

    text = "café"
    nfc, nfd = unicodedata.normalize("NFC", text), unicodedata.normalize("NFD", text)
    assert nfc != nfd
    assert attest.canonical_hash({"name": nfc}) != attest.canonical_hash({"name": nfd})


def test_an_omitted_member_and_a_null_member_differ():
    assert attest.canonical_hash({"a": 1}) != attest.canonical_hash({"a": 1, "b": None})
    # `signer: null` is the shape an attestation with no human label carries;
    # dropping the member entirely is a DIFFERENT body and breaks the MAC.
    att = _att()
    assert att["signer"] is None
    dropped = {k: v for k, v in att.items() if k != "signer"}
    assert attest.verify_attestation(dropped, KEY)[0] is False


def test_injected_dropped_and_reordered_members_all_break_the_mac():
    att = _att()
    assert attest.verify_attestation(att, KEY)[0] is True
    injected = {**att, "note": "trust me"}
    assert attest.verify_attestation(injected, KEY)[0] is False
    dropped = {k: v for k, v in att.items() if k != "timestamp"}
    assert attest.verify_attestation(dropped, KEY)[0] is False
    # reordering is a NON-event: the canonical encoding sorts keys, so the same
    # members in another order are the same body and still verify.
    reordered = dict(reversed(list(att.items())))
    assert attest.verify_attestation(reordered, KEY)[0] is True


# --- the constant-time property, asserted by behaviour ---------------------
#
# This used to be `assert "compare_digest" in text` over attest.py and
# deploy.py. That certifies nothing about the comparison that AUTHENTICATES:
# the name is satisfied by an occurrence anywhere in the file — a comment, a
# docstring, or one of the other comparison sites — while the one an attacker
# times leaks. What follows drives each authenticating path and observes the
# comparison it actually performs.


class _EqTripwire(str):
    """A `str` that records every raw `==`/`!=` performed on it.

    `hmac.compare_digest` compares two ASCII `str`s byte-for-byte in C and never
    reaches `__eq__`. Python's `==` on `str` does, and `==` is the operator that
    returns on the first differing byte — the leak. So an empty `eq_calls` after
    a verification is positive evidence that the authenticating comparison did
    NOT go through the leaky operator, whatever the source text spells."""

    def __new__(cls, value: str) -> "_EqTripwire":
        obj = super().__new__(cls, value)
        obj.eq_calls = []
        return obj

    def __eq__(self, other):
        self.eq_calls.append(other)
        return str.__eq__(self, other)

    def __ne__(self, other):
        self.eq_calls.append(other)
        return str.__ne__(self, other)

    def __hash__(self):
        return str.__hash__(self)


@pytest.fixture()
def digest_comparisons(monkeypatch):
    """Every `hmac.compare_digest` performed while the test runs, as `(a, b)`.

    Patched on the `hmac` module itself, so it catches the call wherever the
    call lives: the property is that the constant-time comparison HAPPENS on the
    path that authenticates, not that a name appears in a file."""
    real = hmac.compare_digest
    seen: list = []

    def spy(a, b):
        seen.append((a, b))
        return real(a, b)

    monkeypatch.setattr(hmac, "compare_digest", spy)
    return seen


def _compared_constant_time(seen, operand) -> bool:
    """Identity, never `in`/`==` — an equality here would trip the very tripwire
    the caller is measuring."""
    return any(a is operand or b is operand for a, b in seen)


def test_the_attestation_signature_check_is_constant_time(digest_comparisons):
    """The presented signature is compared with `hmac.compare_digest`, never
    `==` — on both the rejecting and the accepting outcome."""
    att = _att()

    forged = dict(att)
    forged[attest.SIGNATURE_FIELD] = _EqTripwire("0" * 64)
    ok, reason = attest.verify_attestation(forged, KEY)
    assert ok is False and "signature mismatch" in reason
    tripwire = forged[attest.SIGNATURE_FIELD]
    assert tripwire.eq_calls == [], \
        f"the presented signature met a raw ==: {tripwire.eq_calls}"
    assert _compared_constant_time(digest_comparisons, tripwire)

    good = dict(att)
    good[attest.SIGNATURE_FIELD] = _EqTripwire(att[attest.SIGNATURE_FIELD])
    ok, _reason = attest.verify_attestation(good, KEY)
    assert ok is True
    assert good[attest.SIGNATURE_FIELD].eq_calls == []
    assert _compared_constant_time(digest_comparisons,
                                   good[attest.SIGNATURE_FIELD])


def test_the_composition_hash_check_is_constant_time(digest_comparisons):
    """The second authenticating comparison in `verify_attestation`: the
    attested composition hash against the hash of the composition PRESENTED."""
    verdict = _gate()
    att = _resign(_att(), composition_hash=_EqTripwire(
        attest.canonical_hash(verdict.ir)))
    attested = att["composition_hash"]

    ok, _reason = attest.verify_attestation(att, KEY, ir=verdict.ir)
    assert ok is True
    assert attested.eq_calls == [], \
        f"the attested composition hash met a raw ==: {attested.eq_calls}"
    assert _compared_constant_time(digest_comparisons, attested)

    ok, reason = attest.verify_attestation(att, KEY, ir={"a": 1})
    assert ok is False and "hash mismatch" in reason
    assert attested.eq_calls == []


def test_the_receipt_signature_check_is_constant_time(staged,
                                                      digest_comparisons):
    """`deploy.verify_receipt` authenticates under the host key, and the same
    property has to hold there: a receipt is what an audit attributes a lie
    with, so a timing oracle on it forges attribution."""
    bundle, att = staged
    receipt = deploy.admit(bundle, trust=_trust(), attestation=att,
                           host_key=KEY)

    honest = dict(receipt)
    honest["signature"] = _EqTripwire(receipt["signature"])
    ok, _reason = deploy.verify_receipt(honest, KEY)
    assert ok is True
    assert honest["signature"].eq_calls == []
    assert _compared_constant_time(digest_comparisons, honest["signature"])

    forged = dict(receipt)
    forged["signature"] = _EqTripwire("0" * 64)
    ok, reason = deploy.verify_receipt(forged, KEY)
    assert ok is False and "signature mismatch" in reason
    assert forged["signature"].eq_calls == []


def test_the_seam_envelope_auth_check_is_constant_time(digest_comparisons):
    """The remote one: `CorrelationGuard.admit` authenticates a peer-supplied
    envelope tag. It is the comparison an off-host attacker can actually time,
    because it is the only one they can drive at will."""
    correlation = deploy.Correlation(
        composition_id="c1", generation=1, peer_identity="peer-a",
        effect_id="e1", idempotency_key="k1")
    secret = b"seam-secret-for-peer-a"
    guard = deploy.CorrelationGuard({"peer-a": secret})

    honest = dict(deploy.seal(correlation, secret))
    honest[deploy.AUTH_FIELD] = _EqTripwire(honest[deploy.AUTH_FIELD])
    ok, _reason = guard.admit(honest)
    assert ok is True
    assert honest[deploy.AUTH_FIELD].eq_calls == []
    assert _compared_constant_time(digest_comparisons,
                                   honest[deploy.AUTH_FIELD])

    forged = dict(deploy.seal(correlation, secret))
    forged[deploy.AUTH_FIELD] = _EqTripwire("0" * 64)
    ok, reason = deploy.CorrelationGuard({"peer-a": secret}).admit(forged)
    assert ok is False and reason == deploy.REJECT_FORGED
    assert forged[deploy.AUTH_FIELD].eq_calls == []
