"""Proof-carrying component certificates (roadmap item 474, issue #826).

Item 474's exit criterion: "`revl` emits a signed certificate whose
per-guarantee coverage matches `formal/STATUS.md` exactly, including the
partial and unproved rows and their caveats."

`revl attest` (item 127) signs the moment of admission: this composition was
admitted, and these are the guarantee codes the admission checked. It is a
statement about the frontend run and says nothing about the formal layer
standing behind those codes. A certificate is the other half of that record.
Its own envelope, kind `revl.component-certificate`, signed under its own
domain string with the same key machinery, carries the identity of the artifact
it speaks about, the proof model version, one status per catalogued guarantee,
the runtime evidence and the caveats, every one of them read out of
`formal/STATUS.md`, `formal/scripts/nonvacuity.tsv`, `formal/scripts/run_gate.sh`
and the package's toolchain pin.

These tests pin the definition of done, in the order the claim is made:

  * the coverage table repeats the status map row for row, read here by an
    INDEPENDENT parser rather than by the module's own reader, so the assertion
    is not the module agreeing with itself;
  * the boundaries travel with the coverage: a caveat for every row that is not
    proved, a caveat naming the qualification of every proved row that is
    qualified, and the G9 admission that its coverage is unproved and
    unstatable;
  * sign and verify round-trip through the `revl` console script, and the
    verifier re-derives its evidence instead of reading the statuses out of the
    document it is checking;
  * a certificate cannot claim what the artifacts do not record. Each refusal
    below is a mutation this file applies and watches fail closed: a status
    promoted after signing, a caveat dropped, an artifact digest forged, a
    guarantee the catalogue does not define, a registry row deleted so a status
    has no theorem behind it, a gap cell emptied so a weaker guarantee loses its
    reason, a dependency pin the manifest does not name, a signed member dropped
    from the record, a formal package that is not there at all.

The negative controls matter as much as the mutations: an untouched re-signed
copy and a faithful re-signed copy both still verify, so the sweep proves the
checks bite rather than that they always refuse.
"""

import copy
import hashlib
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from revl import attest as A           # noqa: E402
from revl import cert as C             # noqa: E402
from revl.__main__ import main         # noqa: E402

KEY = b"item-826-certificate-signer-key"
OTHER_KEY = b"item-826-a-different-signer"

#: A fixed timestamp so `make_certificate` is a pure function under test, the
#: same discipline the item 127 attestation tests keep.
NOW = "2026-09-09T00:00:00+00:00"

#: A stable, admissible two-component composition, the same shape the
#: attestation tests admit.
BASE = """
service Database { emission fn execute(sql: Str) -> Int }
service Cache { emission[db] fn put(key: Str, value: Str) }

component PgCache requires db: Database provides cache: Cache {
  provide cache { fn put(key, value) { emit db.execute(`INSERT ${key}`) } }
}
component Front requires cache: Cache { }
"""

#: A different admissible composition, so `--against` has something to reject.
CHANGED = """
service Database { emission fn execute(sql: Str) -> Int }
service Cache { emission[db] fn put(key: Str, value: Str) }

component PgCache requires db: Database provides cache: Cache {
  provide cache { fn put(key, value) { emit db.execute(`INSERT ${key}`) } }
}
component Front requires cache: Cache { emit cache.put("k", "v") }
"""

#: The same composition with `db` provided twice. The compiler refuses this by
#: name (G2), so there is no admitted verdict to certify.
G2_VIOLATING = """
service Database { emission fn execute(sql: Str) -> Int }

component A provides db: Database {
  provide db { fn execute(sql) = 1 }
}
component B provides db: Database {
  provide db { fn execute(sql) = 2 }
}
component Front requires db: Database { }
"""

FORMAL_FILES = (
    "STATUS.md",
    "lean-toolchain",
    "lake-manifest.json",
    "scripts/nonvacuity.tsv",
    "scripts/run_gate.sh",
)


# --------------------------------------------------------------------- helpers

def _write(tmp_path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def _formal_copy(tmp_path) -> Path:
    """A mutable copy of the formal package. The mutations below perturb a copy
    rather than the checkout, and the last assertion of each one is that the
    checkout is untouched, so a test can never leave the tree in the state it
    was checking for."""
    root = tmp_path / "formal-copy"
    for rel in FORMAL_FILES:
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / "formal" / rel, target)
    return root


def _pin_packages(formal: Path, *names: str) -> Path:
    """Name packages in the copy's `lake-manifest.json` and return it.

    The checkout's own manifest names none, so `dependencies` is `[]` on both
    sides of a comparison against it and an assertion there cannot fail. A
    manifest that really names packages is the only way to assert that the
    dependency pins are read out of the manifest and compared rather than
    signed and ignored."""
    manifest_path = formal / "lake-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["packages"] = [{"name": name, "type": "git", "rev": "0" * 40}
                            for name in names]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return manifest_path


def _cert(tmp_path, *, formal=None, key=KEY, source=BASE, source_path=None, **kw):
    """Build a certificate over `source`, the way the CLI builds one: through a
    real `attest.run_gate` verdict, never a hand-made one."""
    verdict = A.run_gate(source=source, filename="<cert-826>")
    assert verdict.admitted, verdict.reason
    if source_path is None:
        source_path = _write(tmp_path, "base.rvl", source)
    return C.make_certificate(verdict.ir, key, verdict=verdict,
                              source_path=source_path, formal=formal, now=NOW,
                              **kw)


def _resign(cert: dict, mutate, *, key=KEY) -> dict:
    """The forgery model: an attacker who HOLDS the key edits the record and
    signs it again. Every check that survives this is a check on the artifacts
    rather than a check on the bytes, which is the whole point of the evidence
    re-derivation."""
    document = copy.deepcopy(cert)
    document.pop("signature", None)
    mutate(document)
    return {**document, "signature": C._sign(document, key)}


def _map_rows() -> dict:
    """The status map rows, read here rather than through the module under
    test. "The certificate matches `formal/STATUS.md`" has to be checkable
    without asking the certificate's own reader what STATUS.md says."""
    text = (ROOT / "formal" / "STATUS.md").read_text(encoding="utf-8")
    section = text.split(C.MAP_HEADING, 1)[1].split("\n## ", 1)[0]
    rows = {}
    for line in section.splitlines():
        parts = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(parts) != 5:
            continue
        head = re.match(r"\*\*(G\d+)\*\*", parts[0])
        if head is None:
            continue
        rows[head.group(1)] = {
            "status": parts[1], "theorems": parts[2], "oracle": parts[3],
            "gap": parts[4],
        }
    return rows


def _row(cert: dict, code: str) -> dict:
    for row in cert["statuses"]:
        if row["code"] == code:
            return row
    raise AssertionError(f"the certificate carries no row for {code}")


def _console_script() -> str:
    found = shutil.which("revl")
    if found:
        return found
    beside = Path(sys.executable).parent / "revl"
    if beside.is_file():
        return str(beside)
    pytest.skip("no `revl` console script on PATH or beside the interpreter; "
                "run `python -m pip install --no-deps -e .`")


# ------------------------------------------------------- the coverage table

def test_the_coverage_table_repeats_the_status_map_row_for_row(tmp_path):
    """The exit criterion itself. Every catalogued guarantee has a row, and the
    row states the map's own status cell verbatim, so a reader comparing the
    certificate against `formal/STATUS.md` finds them identical rather than
    merely compatible."""
    cert = _cert(tmp_path)
    rows = _map_rows()
    catalogued = A.catalogued_guarantees()

    assert [row["code"] for row in cert["statuses"]] == sorted(catalogued)
    assert set(rows) == set(catalogued), (
        "the status map and the guarantee catalogue disagree about which "
        f"codes exist: {sorted(set(rows) ^ set(catalogued))}")
    for code in catalogued:
        assert _row(cert, code)["status_cell"] == rows[code]["status"], (
            f"{code}: the certificate states {_row(cert, code)['status_cell']!r}, "
            f"formal/STATUS.md states {rows[code]['status']!r}")
        assert _row(cert, code)["gap"] == rows[code]["gap"], (
            f"{code}: the certificate states the gap as "
            f"{_row(cert, code)['gap']!r}, formal/STATUS.md states "
            f"{rows[code]['gap']!r}")

def test_a_partial_row_carries_its_gap_and_a_caveat(tmp_path):
    """G1 is partial: `declared_only_access` is real, but its content is the
    shape of the model rather than a property of the checker. A certificate
    that reported the code without the gap would be the green check item 474
    exists to replace."""
    cert = _cert(tmp_path)
    row = _row(cert, "G1")
    rows = _map_rows()

    assert row["status"] == C.PARTIAL
    assert row["gap"], "G1 is not proved and the certificate carries no reason"
    assert row["status_cell"] == rows["G1"]["status"], row["status_cell"]
    assert row["gap"] == rows["G1"]["gap"], row["gap"]
    assert any(caveat.startswith("G1: ") for caveat in cert["caveats"]), cert["caveats"]


def test_the_unstatable_obligation_is_admitted_rather_than_hidden(tmp_path):
    """G9 is the one row whose obligation cannot even be stated. Its coverage is
    not merely unproved, and a certificate that flattened that to `unproved`
    would be claiming the document says less than it does."""
    cert = _cert(tmp_path)
    row = _row(cert, "G9")

    assert row["status"] == C.PARTIAL
    assert "unstatable" in row["status_cell"]
    caveats = [caveat for caveat in cert["caveats"] if caveat.startswith("G9: ")]
    assert caveats, "the certificate does not carry G9's open obligation"
    assert any("unstatable" in caveat for caveat in caveats), caveats
    assert any(requirement["kind"] == C.REQ_UNSTATABLE
               for requirement in cert["requirements"]), (
        "the unstatable obligation is carried as a caveat but not as a "
        "requirement, so nothing re-derives it")


def test_a_proved_row_that_is_qualified_keeps_its_qualification(tmp_path):
    """G4, G5, G6 and G8 are proved over the lattice while their shape-level or
    marker-level statements are weak, or, for G5, contentless. The status is
    `proved`; the qualification is what makes the row honest, so it travels
    verbatim."""
    cert = _cert(tmp_path)
    for code in ("G4", "G5", "G6", "G8"):
        row = _row(cert, code)
        assert row["status"] == C.PROVED, (code, row["status_cell"])
        assert ";" in row["status_cell"], (
            f"{code} is proved but carries no qualification: {row['status_cell']!r}")
        assert any(caveat.startswith(f"{code}: ") for caveat in cert["caveats"]), (
            f"{code}'s qualification is in its row and not among the caveats")


def test_a_contentless_theorem_is_named_rather_than_counted_as_proof(tmp_path):
    """G5's shape-level statement is true by definition, and the registry says
    so. A certificate may not count it as coverage: the finding is carried as a
    requirement of its own and as a caveat, and the theorem is named."""
    cert = _cert(tmp_path)
    row = _row(cert, "G5")

    assert row["contentless"], "the contentless finding did not survive the reading"
    assert all(name.startswith("RevL.G5") for name in row["contentless"])
    contentless = [requirement for requirement in cert["requirements"]
                   if requirement["kind"] == C.REQ_CONTENTLESS]
    assert [requirement["subject"] for requirement in contentless] == ["G5"]
    assert "contentless" in contentless[0]["detail"]
    assert any("contentless" in caveat for caveat in cert["caveats"])


def test_the_proof_model_is_the_pinned_toolchain_and_the_manifest_pins(tmp_path):
    """The certificate names the model its theorems were checked against, read
    from the package's own pin rather than from a constant in this module: the
    toolchain, the digest of the manifest and the dependency pins that manifest
    names.

    The copy below names two packages, out of order, because the checkout's own
    manifest names none: `dependencies == sorted([])` is `[] == []`, an
    assertion that passes whether the member is read from the manifest or
    hardcoded by the builder. Here it can only pass if it was read."""
    formal = _formal_copy(tmp_path)
    manifest_path = _pin_packages(formal, "zzz-pinned", "aaa-pinned")
    cert = _cert(tmp_path, formal=formal)
    pinned = (formal / "lean-toolchain").read_text(encoding="utf-8").strip()

    assert cert["kind"] == C.CERT_KIND
    assert cert["version"] == C.CERT_VERSION
    assert cert["proof_model"]["lean_toolchain"] == pinned
    assert cert["proof_model"]["manifest_digest"] == C.sha256_text(
        manifest_path.read_text(encoding="utf-8"))
    assert cert["proof_model"]["dependencies"] == ["aaa-pinned", "zzz-pinned"]
    model = [requirement for requirement in cert["requirements"]
             if requirement["kind"] == C.REQ_MODEL]
    assert "dependencies aaa-pinned, zzz-pinned" in model[0]["detail"]
    ok, reason = C.verify_certificate(cert, KEY, formal=formal)
    assert ok, reason


def test_every_requirement_points_at_an_artifact_that_exists(tmp_path):
    """A requirement is a claim about a document. The certificate is only worth
    the artifacts it names, so each requirement's `source` is a path in the tree
    and the kinds cover the recorded evidence rather than a summary of it."""
    cert = _cert(tmp_path)
    kinds = [requirement["kind"] for requirement in cert["requirements"]]

    assert set(kinds) <= set(C.REQUIREMENT_KINDS)
    for kind in (C.REQ_GUARANTEE, C.REQ_REGISTRY, C.REQ_MAP, C.REQ_GATE,
                 C.REQ_MODEL, C.REQ_ORACLE, C.REQ_INJECTION, C.REQ_SWEEP):
        assert kind in kinds, f"the certificate carries no {kind} requirement"
    subjects = [requirement["subject"] for requirement in cert["requirements"]
                if requirement["kind"] == C.REQ_GUARANTEE]
    assert subjects == sorted(A.catalogued_guarantees())
    for requirement in cert["requirements"]:
        assert (ROOT / requirement["source"]).is_file(), requirement
        assert requirement["check"], requirement


# ----------------------------------------------------- the trust boundary

def test_the_documented_trust_boundary_says_what_a_signature_proves(tmp_path):
    """A certificate is an attestation, so what it does NOT prove has to be
    written down where a reader meets it. The three statements below are the
    boundary: the key proves authorship and not honesty, the evidence is the
    artifacts rather than the document, and the secret is never passed in
    argv."""
    doc = (ROOT / "docs" / "revl-attest.md").read_text(encoding="utf-8").lower()

    assert "authorship, not honesty" in doc
    assert "re-derives" in doc or "re-derive" in doc
    assert "--key" in doc and "argv" in doc


# --------------------------------------------------------------- sign/verify

def test_verification_round_trips_with_and_without_the_composition(tmp_path):
    cert = _cert(tmp_path)
    verdict = A.run_gate(source=BASE, filename="<cert-826>")

    ok, reason = C.verify_certificate(cert, KEY)
    assert ok, reason
    ok, reason = C.verify_certificate(cert, KEY, against=verdict.ir)
    assert ok, reason
    assert "composition matches" in reason


def test_a_re_signed_copy_with_no_edits_still_verifies(tmp_path):
    """The negative control for the forgery sweep below. If a faithful copy
    failed, every refusal in that sweep would be proving nothing."""
    cert = _cert(tmp_path)
    ok, reason = C.verify_certificate(_resign(cert, lambda document: None), KEY)
    assert ok, reason


def test_a_green_verification_names_the_members_it_read_rather_than_checked(
        tmp_path):
    """The boundary, stated on the success path. `timestamp` and `signer` are
    statements about the signing EVENT, not about the tree, so they are inside
    the MAC and outside the re-derivation. A VALID that stayed silent about them
    would let its reader take it for a claim about the whole document."""
    cert = _cert(tmp_path)
    ok, reason = C.verify_certificate(cert, KEY)
    assert ok, reason
    assert "recorded rather than re-derived" in reason, reason
    for member in C.UNVERIFIABLE:
        assert member in reason, f"the reason does not name {member}: {reason}"
    assert "timestamp" in reason and "signer" in reason, reason


def test_the_certificate_is_a_second_signature_domain(tmp_path):
    """One key signs two protocols. Neither document may verify as the other,
    or a cross-protocol confusion is a forgery with no work in it."""
    cert = _cert(tmp_path)
    verdict = A.run_gate(source=BASE, filename="<cert-826>")
    attestation = A.make_attestation(verdict.ir, KEY, verdict=verdict, now=NOW)

    ok, reason = A.verify_attestation(cert, KEY)
    assert not ok, "a certificate verified as an attestation"
    ok, reason = C.verify_certificate(attestation, KEY)
    assert not ok
    assert "not a component certificate" in reason, reason


def test_certifying_without_a_measured_verdict_is_refused(tmp_path):
    """There is nothing to certify until the frontend has run. A certificate
    built from a bare IR would be a coverage claim about a composition nothing
    checked."""
    verdict = A.run_gate(source=BASE, filename="<cert-826>")
    with pytest.raises(C.CertError) as excinfo:
        C.make_certificate(verdict.ir, KEY, source_path=_write(tmp_path, "b.rvl", BASE),
                           now=NOW)
    assert "needs a gate verdict" in str(excinfo.value)


def test_certifying_without_the_source_file_is_refused(tmp_path):
    verdict = A.run_gate(source=BASE, filename="<cert-826>")
    with pytest.raises(C.CertError) as excinfo:
        C.make_certificate(verdict.ir, KEY, verdict=verdict, now=NOW)
    assert "needs the source file" in str(excinfo.value)


def test_a_verdict_for_a_different_composition_is_refused(tmp_path):
    """The verdict and the IR must be the same artifact, or a certificate would
    carry one composition's admission beside another's coverage."""
    verdict = A.run_gate(source=BASE, filename="<cert-826>")
    other = A.run_gate(source=CHANGED, filename="<cert-826>")
    assert other.admitted
    with pytest.raises(C.CertError) as excinfo:
        C.make_certificate(other.ir, KEY, verdict=verdict,
                           source_path=_write(tmp_path, "c.rvl", CHANGED), now=NOW)
    assert "different composition" in str(excinfo.value)


def test_a_refused_composition_has_no_certificate(tmp_path):
    verdict = A.run_gate(source=G2_VIOLATING, filename="<cert-826>")
    assert not verdict.admitted
    with pytest.raises(C.CertError) as excinfo:
        C.make_certificate(verdict.ir or {}, KEY, verdict=verdict,
                           source_path=_write(tmp_path, "g2.rvl", G2_VIOLATING), now=NOW)
    assert "did not admit" in str(excinfo.value)


# ------------------------------------------------------------ the mutations

def _promote_g1(document):
    for row in document["statuses"]:
        if row["code"] == "G1":
            row["status"] = C.PROVED


def _drop_g9(document):
    document["statuses"] = [row for row in document["statuses"] if row["code"] != "G9"]


def _drop_every_caveat(document):
    document["caveats"] = []


def _forge_the_status_digest(document):
    document["artifacts"]["status"]["digest"] = hashlib.sha256(b"elsewhere").hexdigest()


def _invent_a_guarantee(document):
    row = copy.deepcopy(document["statuses"][-1])
    row.update(code="G99", name="invented", status=C.PROVED,
               status_cell="full", gap="")
    document["statuses"].append(row)


def _drop_a_requirement(document):
    document["requirements"] = [
        requirement for requirement in document["requirements"]
        if not (requirement["kind"] == C.REQ_GUARANTEE
                and requirement["subject"] == "G1")]


def _drop_the_artifacts(document):
    document["artifacts"] = {}


def _invent_a_requirement_kind(document):
    document["requirements"][0]["kind"] = "revl.invented-by-the-signer"


def _truncate_the_subject_hash(document):
    document["subject"]["source_hash"] = "deadbeef"


def _break_the_timestamp(document):
    document["timestamp"] = "the day before yesterday"


def _claim_to_be_an_attestation(document):
    document["kind"] = A.ATTEST_KIND


# The prose a status is read out of, and the rows it is read from. These are the
# members the certificate asserts and the verifier now RE-DERIVES: the mutation
# keeps the signed member and rewrites it, so the only thing that can catch it is
# a comparison against the artifacts rather than a re-reading of the record.

def _rewrite_a_status_cell(document):
    """Keep `status` and rewrite the cell it was read out of. A certificate
    whose own status and status cell disagree is a certificate that says two
    things at once."""
    for row in document["statuses"]:
        if row["code"] == "G1":
            row["status_cell"] = "full"


def _rewrite_a_gap(document):
    for row in document["statuses"]:
        if row["code"] == "G1":
            row["gap"] = "no gaps of any kind"


def _empty_a_gap(document):
    for row in document["statuses"]:
        if row["code"] == "G9":
            row["gap"] = ""


def _rename_a_guarantee(document):
    for row in document["statuses"]:
        if row["code"] == "G1":
            row["name"] = "whatever the signer prefers"


def _unregister_a_theorem(document):
    for row in document["statuses"]:
        if row["code"] == "G1":
            row["registered"] = ["0" * 16]


def _drop_a_contentless_finding(document):
    for row in document["statuses"]:
        if row["contentless"]:
            row["contentless"] = []
            return
    raise AssertionError("no row carries a contentless finding to drop")


def _rewrite_a_theorems_cell(document):
    for row in document["statuses"]:
        if row["code"] == "G1":
            row["theorems_cell"] = "none, on the signer's word"


def _rewrite_an_oracle_cell(document):
    for row in document["statuses"]:
        if row["code"] == "G1":
            row["oracle_cell"] = "yes"


def _rewrite_a_requirement_check(document):
    document["requirements"][0]["check"] = "obviously fine, no gaps"


def _rewrite_a_requirement_detail(document):
    document["requirements"][0]["detail"] = "proved: full (the signer says so)"


def _forge_the_proof_model(document):
    document["proof_model"]["lean_toolchain"] = "leanprover/lean4:v9.9.9"


def _forge_the_manifest_pin(document):
    document["proof_model"]["manifest_digest"] = "0" * 64


def _forge_the_dependencies(document):
    document["proof_model"]["dependencies"] = ["evil-pkg"]


def _drop_the_commit(document):
    document.pop("as_of_commit")


def _forge_the_checker(document):
    document["checker"]["compiler"] = "something-else"


def _forge_the_ruleset(document):
    document["checker"]["ruleset"] = "0" * 64


def _forge_the_commit(document):
    document["as_of_commit"] = "0" * 40


def _forge_the_subject_filename(document):
    """Point the subject at a file that really exists, so the mismatch is the
    digest rather than a missing file."""
    document["subject"]["filename"] = str(ROOT / "formal" / "STATUS.md")


def _forge_the_subject_hash(document):
    document["subject"]["source_hash"] = "0" * 64


def _forge_the_key_fingerprint(document):
    document["key_id"] = "0123456789abcdef"


FORGERIES = [
    ("a promoted status", _promote_g1,
     "the recorded per-guarantee status changed for G1"),
    ("a dropped status row", _drop_g9, "reports no status for G9"),
    ("every caveat dropped", _drop_every_caveat,
     "caveat(s) the artifacts record are missing"),
    ("a forged artifact digest", _forge_the_status_digest,
     "formal/STATUS.md on this machine is not the revision"),
    ("a guarantee the catalogue does not define", _invent_a_guarantee,
     "which the guarantee catalogue does not define"),
    ("a requirement dropped", _drop_a_requirement,
     "new evidence the certificate does not carry"),
    ("the artifacts member dropped", _drop_the_artifacts,
     "artifacts is not one entry per formal artifact"),
    ("an invented requirement kind", _invent_a_requirement_kind,
     "is not one this verifier re-derives"),
    ("a truncated subject hash", _truncate_the_subject_hash,
     "subject.source_hash is not a sha256 digest"),
    ("a timestamp that is not an instant", _break_the_timestamp,
     "timestamp is not an ISO-8601 instant"),
    ("a rewrite of the kind", _claim_to_be_an_attestation,
     "not a component certificate"),
    ("a status cell rewritten under a kept status", _rewrite_a_status_cell,
     "records status_cell full"),
    ("a gap rewritten", _rewrite_a_gap, "records gap no gaps of any kind"),
    ("a gap emptied", _empty_a_gap, "records gap , and G9 re-derives"),
    ("a guarantee renamed", _rename_a_guarantee, "records name whatever the signer"),
    ("a registered theorem replaced", _unregister_a_theorem,
     "records registered ['0000000000000000']"),
    ("a contentless finding dropped", _drop_a_contentless_finding,
     "records contentless []"),
    ("a theorems cell rewritten", _rewrite_a_theorems_cell,
     "records theorems_cell none, on the signer's word"),
    ("an oracle cell rewritten", _rewrite_an_oracle_cell,
     "records oracle_cell yes"),
    ("a requirement's check rewritten", _rewrite_a_requirement_check,
     "(G1; check)"),
    ("a requirement's detail rewritten", _rewrite_a_requirement_detail,
     "(G1; detail)"),
    ("a proof model pin rewritten", _forge_the_proof_model,
     "signed proof model member lean_toolchain is leanprover/lean4:v9.9.9"),
    ("a manifest pin forged", _forge_the_manifest_pin,
     "signed proof model member manifest_digest is " + "0" * 64),
    # `dependencies` is not swept here as well: this build is over the checkout's
    # own manifest, whose `packages` is empty, so `[]` is the honest answer and
    # emptying the member would prove nothing. The pinned-manifest case lives in
    # `test_a_dependency_pin_the_manifest_does_not_name_is_refused`.
    ("a dependency pin invented", _forge_the_dependencies,
     "signed proof model member dependencies is ['evil-pkg']"),
    ("the commit member dropped", _drop_the_commit,
     "missing required member 'as_of_commit'"),
    ("the checker version rewritten", _forge_the_checker,
     "checker mismatch"),
    ("the ruleset digest rewritten", _forge_the_ruleset,
     "signed ruleset " + "0" * 64),
    ("a commit that is not in this history", _forge_the_commit,
     "no commit " + "0" * 40 + " in this repository's history"),
    ("the subject pointed at another file", _forge_the_subject_filename,
     "subject mismatch: the file this certificate names"),
    ("the subject's source digest forged", _forge_the_subject_hash,
     "but the certificate signs 000000000000"),
    ("a key fingerprint that is not the key", _forge_the_key_fingerprint,
     "which is not the key it is being checked with"),
]


@pytest.mark.parametrize("label,mutate,expected", FORGERIES,
                         ids=[forgery[0] for forgery in FORGERIES])
def test_a_re_signed_certificate_cannot_claim_more_than_the_artifacts_record(
        tmp_path, label, mutate, expected):
    """Each mutation is a change a key holder can make and sign. None of them
    may verify, and each refusal names what it found, so a reader learns which
    of the key, the envelope and the evidence failed."""
    cert = _cert(tmp_path)
    ok, reason = C.verify_certificate(_resign(cert, mutate), KEY)
    assert not ok, f"{label} verified"
    assert expected in reason, f"{label}: {reason}"


@pytest.mark.parametrize(
    "forged",
    [["other-pkg"], ["evil-pkg", "second-pkg"], [], ["zzz"]],
    ids=["replaced", "extended", "emptied", "invented"])
def test_a_dependency_pin_the_manifest_does_not_name_is_refused(tmp_path, forged):
    """The manifest is the only source for the dependency pins, so the signed
    member and the re-derived one have to be compared: this certificate is
    built over a manifest that really names `evil-pkg`, and every re-signed
    rewrite of the member is refused by name.

    Without that comparison all four are green — `['other-pkg']`,
    `['evil-pkg', 'second-pkg']`, `[]` and `['zzz']` alike — which is a signed
    member that is neither checked nor described as unchecked."""
    formal = _formal_copy(tmp_path)
    _pin_packages(formal, "evil-pkg")
    cert = _cert(tmp_path, formal=formal)
    assert cert["proof_model"]["dependencies"] == ["evil-pkg"]
    ok, reason = C.verify_certificate(cert, KEY, formal=formal)
    assert ok, reason  # the negative control: the sweep bites rather than always refusing

    def forge(document):
        document["proof_model"]["dependencies"] = list(forged)

    ok, reason = C.verify_certificate(_resign(cert, forge), KEY, formal=formal)
    assert not ok, f"a dependency pin of {forged!r} verified"
    assert "signed proof model member dependencies" in reason, reason
    assert f"{forged!r}" in reason, reason


def test_an_absent_commit_member_is_a_reason_and_not_a_traceback(tmp_path, capsys):
    """`as_of_commit` is optional in VALUE — a package outside a git work tree
    signs `null`, which is why the envelope reads it with `get` — but not in
    presence, and a verifier is a trust boundary: a document that dropped the
    member is a refusal with a reason, never the `KeyError` a direct index
    raises. Both signatures are covered, because only the second one gets past
    the MAC: the honest certificate, whose signature no longer matches once the
    member is gone, and the re-signed one, which reaches the evidence checks."""
    cert = _cert(tmp_path)
    dropped = copy.deepcopy(cert)
    dropped.pop("as_of_commit")

    ok, reason = C.verify_certificate(dropped, KEY)
    assert not ok and "signature mismatch" in reason, reason

    resigned = _resign(cert, _drop_the_commit)
    ok, reason = C.verify_certificate(resigned, KEY)
    assert not ok, "a certificate with no commit member verified"
    assert "missing required member 'as_of_commit'" in reason, reason

    comp = _write(tmp_path, "cli.rvl", BASE)
    keyf = _key_file(tmp_path)
    assert main(["attest", str(comp), "--certificate", "--key", keyf, "--json"]) == 0
    document = json.loads(capsys.readouterr().out)
    host_path = _write(tmp_path, "no-commit.json",
                       json.dumps(_resign(document, _drop_the_commit)))

    assert main(["attest", str(host_path), "--verify-certificate",
                 "--key", keyf, "--json"]) == 1
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["valid"] is False, payload
    assert "missing required member 'as_of_commit'" in payload["reason"], payload
    assert "Traceback" not in captured.out + captured.err, captured.err

    assert main(["attest", str(host_path), "--verify-certificate",
                 "--key", keyf]) == 1
    captured = capsys.readouterr()
    assert "INVALID" in captured.out, captured.out
    assert "missing required member 'as_of_commit'" in captured.out, captured.out
    assert "Traceback" not in captured.out + captured.err, captured.err


def test_verification_needs_a_key_and_never_reads_a_default(tmp_path):
    cert = _cert(tmp_path)
    for empty in (b"", bytearray(), None):
        ok, reason = C.verify_certificate(cert, empty)
        assert not ok and "no signing key" in reason
    ok, reason = C.verify_certificate(cert, OTHER_KEY)
    assert not ok and "signature mismatch" in reason


def test_verification_fails_closed_rather_than_raising(tmp_path):
    """A verifier is a trust boundary: a malformed document is a refusal, never
    a traceback a caller might read as "not invalid"."""
    cert = _cert(tmp_path)
    for document in (None, [], "cert", {}, {"kind": C.CERT_KIND}):
        ok, reason = C.verify_certificate(document, KEY)
        assert not ok and isinstance(reason, str) and reason, document
    stripped = dict(cert)
    stripped.pop("signature")
    ok, reason = C.verify_certificate(stripped, KEY)
    assert not ok and "no signature" in reason


@pytest.mark.parametrize("signature", ["\u00e9\u00e9", "caf\u00e9", "\u00e9" * 64])
def test_a_signature_that_is_not_ascii_is_a_reason_and_not_a_raise(
        tmp_path, signature):
    """A certificate comes from a peer, so a value that cannot be a hex MAC has
    to end as a reason. `hmac.compare_digest` raises `TypeError` on two strings
    that are not both ASCII, and a traceback out of a verifier is a failure mode
    its caller may read as something other than a refusal."""
    hostile = {**_cert(tmp_path), "signature": signature}
    ok, reason = C.verify_certificate(hostile, KEY)
    assert not ok
    assert "not ASCII" in reason, reason
    # The refusal has to be the character guard, not the MAC: an ASCII run of
    # the same length fails as a mismatch, and the two are different findings.
    assert "signature mismatch" not in reason, reason


def test_the_cli_refuses_a_non_ascii_signature_without_a_traceback(
        tmp_path, capsys):
    """The same refusal over the real command line, where a traceback would be
    printed rather than raised at the caller: exit 1, a reason on stdout, and no
    rendered certificate beside it."""
    comp = _write(tmp_path, "base.rvl", BASE)
    keyf = _key_file(tmp_path)
    assert main(["attest", str(comp), "--certificate", "--key", keyf, "--json"]) == 0
    document = json.loads(capsys.readouterr().out)
    document["signature"] = "\u00e9\u00e9"
    cert_path = _write(tmp_path, "hostile.json", json.dumps(document))

    assert main(["attest", str(cert_path), "--verify-certificate",
                 "--key", keyf]) == 1
    captured = capsys.readouterr()
    assert "not ASCII" in captured.out, captured.out
    assert "INVALID" in captured.out
    assert "Traceback" not in captured.out + captured.err, captured.err
    assert "coverage:" not in captured.out, "a report was fabricated"

    assert main(["attest", str(cert_path), "--verify-certificate",
                 "--key", keyf, "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["valid"] is False and "not ASCII" in payload["reason"]


def test_the_verifier_process_refuses_a_non_ascii_signature(tmp_path):
    """The refusal as a process sees it: the subprocess exit code and stderr,
    where a `TypeError` would surface as a traceback and a nonzero status rather
    than the deliberate 1 this command documents."""
    comp = _write(tmp_path, "base.rvl", BASE)
    keyf = _key_file(tmp_path)
    signed = subprocess.run(
        [sys.executable, "-m", "revl", "attest", str(comp), "--certificate",
         "--key", keyf, "--json"],
        capture_output=True, text=True, timeout=120,
        cwd=str(ROOT), env={"PATH": str(Path(sys.executable).parent),
                            "PYTHONPATH": str(SRC)})
    assert signed.returncode == 0, signed.stderr
    document = json.loads(signed.stdout)
    document["signature"] = "\u00e9\u00e9"
    hostile = _write(tmp_path, "hostile.json", json.dumps(document))

    refused = subprocess.run(
        [sys.executable, "-m", "revl", "attest", str(hostile),
         "--verify-certificate", "--key", keyf],
        capture_output=True, text=True, timeout=120,
        cwd=str(ROOT), env={"PATH": str(Path(sys.executable).parent),
                            "PYTHONPATH": str(SRC)})
    assert refused.returncode == 1, refused.stdout + refused.stderr
    assert "not ASCII" in refused.stdout, refused.stdout
    assert "Traceback" not in refused.stderr, refused.stderr
    assert refused.stderr == "", refused.stderr


# ------------------------------------------------ mutations on the artifacts

def test_a_status_without_a_registered_theorem_is_refused(tmp_path):
    """The mutation proof for the coverage claim: a status is only certifiable
    while the registry really registers a theorem under that guarantee's name.
    Delete G3's rows and the certificate refuses to be built at all."""
    formal = _formal_copy(tmp_path)
    registry = formal / "scripts" / "nonvacuity.tsv"
    kept = [line for line in registry.read_text(encoding="utf-8").splitlines()
            if not re.match(r"^RevL\.G3(Classified)?\.", line)]
    assert len(kept) < len(registry.read_text(encoding="utf-8").splitlines())
    registry.write_text("\n".join(kept) + "\n", encoding="utf-8")

    with pytest.raises(C.CertError) as excinfo:
        _cert(tmp_path, formal=formal)
    assert "G3 has no theorem in the non-vacuity registry" in str(excinfo.value)
    assert "asserted rather than checked" in str(excinfo.value)


def test_a_map_without_the_row_is_refused(tmp_path):
    formal = _formal_copy(tmp_path)
    status = formal / "STATUS.md"
    kept = [line for line in status.read_text(encoding="utf-8").splitlines()
            if not line.startswith("| **G7**")]
    status.write_text("\n".join(kept) + "\n", encoding="utf-8")

    with pytest.raises(C.CertError) as excinfo:
        _cert(tmp_path, formal=formal)
    assert "carries no row for G7" in str(excinfo.value)


def test_a_weaker_guarantee_without_its_gap_is_refused(tmp_path):
    """G1 is partial because of a named modelling limit. A document that kept
    the status and dropped the reason would let a certificate report a weaker
    guarantee with nothing attached, so the build refuses instead."""
    formal = _formal_copy(tmp_path)
    status = formal / "STATUS.md"
    text = status.read_text(encoding="utf-8")
    line = next(line for line in text.splitlines() if line.startswith("| **G1**"))
    cells = line.strip().strip("|").split("|")
    cells[-1] = " "
    status.write_text(text.replace(line, "|" + "|".join(cells) + "|"), encoding="utf-8")

    with pytest.raises(C.CertError) as excinfo:
        _cert(tmp_path, formal=formal)
    assert "G1 is not proved and records no gap" in str(excinfo.value)


def test_a_gate_that_stopped_stating_its_policy_is_refused(tmp_path):
    formal = _formal_copy(tmp_path)
    gate = formal / "scripts" / "run_gate.sh"
    text = gate.read_text(encoding="utf-8")
    assert "(no sorryAx" in text
    gate.write_text(text.replace("(no sorryAx", "(sorryAx allowed: "), encoding="utf-8")

    with pytest.raises(C.CertError) as excinfo:
        _cert(tmp_path, formal=formal)
    assert "states no axiom policy" in str(excinfo.value)


def test_a_certificate_cannot_be_built_without_a_formal_package(tmp_path):
    """The strongest form of the assertion: with no artifacts there is no
    certificate, rather than a certificate whose coverage is empty."""
    empty = tmp_path / "not-a-formal-package"
    empty.mkdir()
    with pytest.raises(C.CertError) as excinfo:
        C.formal_state(empty)
    assert "no " in str(excinfo.value) and "STATUS.md" in str(excinfo.value)


def test_verification_of_a_moved_artifact_names_the_artifact(tmp_path):
    """An artifact that changed after signing is the case a reader most needs
    spelled out. The verifier re-reads the documents and says which one moved
    rather than reporting a bare failure."""
    cert = _cert(tmp_path)
    formal = _formal_copy(tmp_path)
    registry = formal / "scripts" / "nonvacuity.tsv"
    registry.write_text(registry.read_text(encoding="utf-8") + "\n", encoding="utf-8")

    ok, reason = C.verify_certificate(cert, KEY, formal=formal)
    assert not ok
    assert "artifact mismatch" in reason
    assert "nonvacuity.tsv" in reason, reason


# ------------------------------------------------------------------ the CLI

def _key_file(tmp_path) -> str:
    path = tmp_path / "signer.key"
    path.write_bytes(KEY)
    return str(path)


def test_the_cli_signs_and_verifies_a_certificate(tmp_path, capsys):
    comp = _write(tmp_path, "base.rvl", BASE)
    keyf = _key_file(tmp_path)

    assert main(["attest", str(comp), "--certificate", "--key", keyf]) == 0
    out = capsys.readouterr().out
    assert "revl.component-certificate v1.0" in out
    assert "coverage:" in out and "caveats:" in out
    for code in A.catalogued_guarantees():
        assert f"    {code} " in out, f"the printed certificate omits {code}"

    path = _write(tmp_path, "cert.json", "")
    assert main(["attest", str(comp), "--certificate", "--key", keyf, "--json"]) == 0
    path.write_text(capsys.readouterr().out, encoding="utf-8")

    assert main(["attest", str(path), "--verify-certificate", "--key", keyf]) == 0
    assert "VALID" in capsys.readouterr().out
    assert main(["attest", str(path), "--verify-certificate", "--against", str(comp),
                 "--key", keyf]) == 0
    assert "composition matches" in capsys.readouterr().out


def test_the_cli_verify_certificate_is_a_check_that_exits_nonzero(tmp_path, capsys):
    comp = _write(tmp_path, "base.rvl", BASE)
    changed = _write(tmp_path, "changed.rvl", CHANGED)
    keyf = _key_file(tmp_path)
    assert main(["attest", str(comp), "--certificate", "--key", keyf, "--json"]) == 0
    cert_path = _write(tmp_path, "cert.json", capsys.readouterr().out)

    assert main(["attest", str(cert_path), "--verify-certificate",
                 "--against", str(changed), "--key", keyf]) == 1
    out = capsys.readouterr().out
    assert "INVALID" in out and "hash mismatch" in out

    wrong = tmp_path / "wrong.key"
    wrong.write_bytes(OTHER_KEY)
    assert main(["attest", str(cert_path), "--verify-certificate",
                 "--key", str(wrong)]) == 1
    assert "signature mismatch" in capsys.readouterr().out


def test_the_cli_refuses_a_certificate_prompt_that_is_only_an_attestation(
        tmp_path, capsys):
    """The dispatch guard, both ways: `--verify` is the attestation verb and
    `--certificate` the certificate one, and asking for both is an error rather
    than a silent choice."""
    comp = _write(tmp_path, "base.rvl", BASE)
    keyf = _key_file(tmp_path)
    assert main(["attest", str(comp), "--certificate", "--key", keyf, "--json"]) == 0
    cert_path = _write(tmp_path, "cert.json", capsys.readouterr().out)
    assert main(["attest", str(comp), "--key", keyf, "--json"]) == 0
    att_path = _write(tmp_path, "att.json", capsys.readouterr().out)

    assert main(["attest", str(cert_path), "--verify-certificate", "--verify",
                 "--key", keyf]) == 1
    assert "use --verify-certificate" in capsys.readouterr().err

    assert main(["attest", str(att_path), "--verify-certificate",
                 "--key", keyf]) == 1
    assert "not a component certificate" in capsys.readouterr().out

    assert main(["attest", str(cert_path), "--certificate",
                 "--verify-certificate", "--key", keyf]) == 1
    assert "give exactly one of them" in capsys.readouterr().err


def test_the_cli_reads_the_evidence_from_the_named_formal_package(tmp_path, capsys):
    """`--formal` is how a caller points at the package to certify. Pointed at
    a directory that holds no STATUS.md, the certificate is refused rather than
    built with empty coverage."""
    comp = _write(tmp_path, "base.rvl", BASE)
    keyf = _key_file(tmp_path)
    empty = tmp_path / "empty"
    empty.mkdir()

    assert main(["attest", str(comp), "--certificate", "--key", keyf,
                 "--formal", str(empty)]) == 1
    assert "STATUS.md" in capsys.readouterr().err


def test_the_cli_never_signs_without_a_key(tmp_path, capsys, monkeypatch):
    """No default key, and the secret arrives as a path, not as a value in
    argv: a certificate that could be signed by anyone who can type the command
    is not an attestation."""
    comp = _write(tmp_path, "base.rvl", BASE)
    monkeypatch.delenv(A.KEY_ENV, raising=False)
    monkeypatch.delenv(A.KEY_FILE_ENV, raising=False)
    assert main(["attest", str(comp), "--certificate"]) == 1
    assert "no signing key" in capsys.readouterr().err


def test_the_console_script_signs_and_verifies_a_certificate(tmp_path):
    """The documented happy path, end to end: the `revl` console script, over a
    real file, with the evidence read from the checkout's own formal package."""
    script = _console_script()
    comp = _write(tmp_path, "base.rvl", BASE)
    keyf = _key_file(tmp_path)
    env = {"PATH": str(Path(script).parent), "PYTHONPATH": str(SRC),
           "HOME": str(tmp_path)}
    cert_path = tmp_path / "cert.json"

    signed = subprocess.run(
        [script, "attest", str(comp), "--certificate", "--key", keyf, "--json"],
        capture_output=True, text=True, timeout=120, env=env, cwd=str(tmp_path))
    assert signed.returncode == 0, signed.stderr
    cert_path.write_text(signed.stdout, encoding="utf-8")
    document = json.loads(signed.stdout)
    assert document["kind"] == C.CERT_KIND

    verified = subprocess.run(
        [script, "attest", str(cert_path), "--verify-certificate", "--key", keyf,
         "--against", str(comp)],
        capture_output=True, text=True, timeout=120, env=env, cwd=str(tmp_path))
    assert verified.returncode == 0, verified.stdout + verified.stderr
    assert "VALID" in verified.stdout
    assert "composition matches" in verified.stdout
