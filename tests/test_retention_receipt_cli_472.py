"""`revl retention-receipt`: the erasure REQUEST, and the check on one
(roadmap item 472, issue #824).

`revl.retention` could sign an erasure receipt from Python and from nothing an
operator runs, so the item's second clause — "an erasure request emits a signed
receipt naming each in-scope replica and derivative" — held as a library fact
and not as a reachable one. These tests pin the verb, and three properties that
are easy to lose:

  * SIGNING DISCIPLINE. Every member of the issued body is INSIDE the MAC. The
    test removes and edits each top-level member in turn and requires the
    signature to stop verifying, so a member added to the body after the MAC is
    taken (the failure where a verifier covers a member the signer did not, and
    nothing ever verifies) cannot pass here.
  * FAILURE DIRECTION. A receipt that cannot be signed is never printed; a
    receipt that IS presented and cannot be checked is refused. Those are
    opposite directions and both are pinned, including the case where no key
    can be resolved at all: absent-key on the issue path is "no document", and
    absent-key on the verify path is "refused", never "valid".
  * THE COMPOSITION MAY BE REFUSED. Past a policy's deadline the composition
    that persists a value under it no longer compiles (`G-RETAIN`) — which is
    the whole reason the erasure is being requested. The verb works there, and
    the test proves the refusal it works through is real by asserting the
    compile actually fails.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import RevlError  # noqa: E402
from revl import erasure_receipt  # noqa: E402
from revl import retention as R  # noqa: E402
from revl.__main__ import main  # noqa: E402
from revl.compiler import compile_source  # noqa: E402

POLICY = '''retention customer_pii {
  until: "2020-01-01T00:00:00Z"
  residence: "eu"
  deleters: dpo, subject
  derivatives: summary, index
}
'''

# a persistence sink whose own parameter carries the policy: this is what makes
# the composition refusable once the deadline has passed.
SINK = '''extern emission[db.insert] fn db_put(row: Retained[Str, customer_pii]) -> Int = @py { return 0 }
'''


@pytest.fixture(autouse=True)
def _no_ambient_key(monkeypatch):
    """The key routes are explicit. An exported key in the developer's own
    environment would make the absent-key tests pass for the wrong reason."""
    monkeypatch.delenv(R.KEY_ENV, raising=False)
    monkeypatch.delenv(R.KEY_FILE_ENV, raising=False)
    monkeypatch.delenv(R.AS_OF_ENV, raising=False)


@pytest.fixture
def keyfile(tmp_path):
    path = tmp_path / "erasure.key"
    path.write_text("retention-test-key-0123456789abc\n", encoding="utf-8")
    return str(path)


@pytest.fixture
def source(tmp_path):
    path = tmp_path / "store.rvl"
    path.write_text(POLICY + SINK, encoding="utf-8")
    return str(path)


def _issue(source, keyfile, *extra):
    return main(["retention-receipt", source, "--policy", "customer_pii",
                 "--requester", "dpo", "--receipt-key", keyfile,
                 "--issued-at", "2026-09-14T00:00:00Z", "--json", *extra])


# --------------------------------------------------------- the request itself

def test_an_erasure_request_emits_a_signed_receipt_naming_each_replica_and_derivative(
        source, keyfile, capsys):
    """Clause 2 of the exit criterion, through the verb an operator runs."""
    rc = _issue(source, keyfile,
                "--replica", "host:Store:db_put@eu",
                "--replica", "backup:nightly@us",
                "--derivative", "search-index=index@eu",
                "--derivative", "vectors=embedding")
    receipt = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert receipt["kind"] == R.RECEIPT_KIND
    assert receipt[R.SIGNATURE_FIELD]
    tokens = [row["token"] for row in receipt["replicas"]]
    assert tokens == ["host:Store:db_put", "backup:nightly"]
    # the replica outside the declared residence is a FINDING, not a silent row
    assert [row["disposition"] for row in receipt["replicas"]] == [
        "enumerated", "residence-mismatch"]
    names = {row["name"]: row for row in receipt["derivatives"]}
    assert names["search-index"]["disposition"] == "covered"
    # the policy covers `summary, index` and not `embedding`: said out loud
    assert names["vectors"]["disposition"] == "not-covered"
    assert names["vectors"]["claim"] == R.NOT_CLAIMED


def test_the_issued_receipt_verifies_through_the_cli(source, keyfile, tmp_path,
                                                     capsys):
    """The non-vacuity control: the legitimate path works end to end. Every
    refusal below is only meaningful while this passes."""
    assert _issue(source, keyfile, "--replica", "host:Store:db_put@eu") == 0
    out = capsys.readouterr().out
    presented = tmp_path / "receipt.json"
    presented.write_text(out, encoding="utf-8")

    rc = main(["retention-receipt", "--verify", str(presented),
               "--receipt-key", keyfile])
    captured = capsys.readouterr()
    assert rc == 0
    assert "VALID" in captured.out
    assert captured.err == ""


def test_the_receipt_is_reproducible_given_issued_at(source, keyfile, capsys):
    """Two issues at the same instant are byte-identical, which is what makes a
    receipt reproducible from the evidence it summarises."""
    assert _issue(source, keyfile, "--replica", "r1@eu") == 0
    first = capsys.readouterr().out
    assert _issue(source, keyfile, "--replica", "r1@eu") == 0
    assert capsys.readouterr().out == first


def test_a_policy_declared_in_an_imported_module_is_found(tmp_path, keyfile,
                                                          capsys):
    """The declaration closure, not just the root: a `use`d module's policy is
    the composition's policy (the item-256 defect this mirrors)."""
    (tmp_path / "policies.rvl").write_text(
        POLICY + 'pub extern pure fn load(k: Str) -> '
                 'Retained[Str, customer_pii] = @py { return k }\n',
        encoding="utf-8")
    root = tmp_path / "root.rvl"
    root.write_text('use "policies.rvl" { load }\n' + SINK, encoding="utf-8")

    assert _issue(str(root), keyfile, "--replica", "r1@eu") == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["policy"]["name"] == "customer_pii"


# ------------------------------------------- the composition may be REFUSED

def test_a_composition_the_checker_refuses_still_yields_a_receipt(
        source, keyfile, capsys):
    """The reason this verb does not compile its input.

    The first half of the premise is asserted, not assumed: the same source is
    genuinely refused by the checker, because its deadline has passed and it
    persists a `Retained` value. The erasure request is made BECAUSE of that
    refusal, so the receipt verb has to work through it."""
    with pytest.raises(RevlError) as refusal:
        compile_source(POLICY + SINK, "store.rvl")
    assert "G-RETAIN" in str(refusal.value.code or "") or \
        "retention deadline" in str(refusal.value)

    assert _issue(source, keyfile, "--replica", "host:Store:db_put@eu") == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["policy"]["until"] == "2020-01-01T00:00:00Z"


# ------------------------------------------------------- signing discipline

def test_every_member_of_the_signed_body_is_inside_the_mac(source, keyfile,
                                                           capsys):
    """A member of the body the MAC does not cover is the bug where a verifier
    checks something the signer never signed. Drop each top-level member in
    turn, and edit each in turn: the signature must stop verifying every time.

    `key_id` is named explicitly because that is the member most easily added
    to a body AFTER the MAC is taken."""
    assert _issue(source, keyfile,
                  "--replica", "r1@eu", "--derivative", "s=summary",
                  "--signer", "dpo-office") == 0
    receipt = json.loads(capsys.readouterr().out)
    key = R.load_key(keyfile)
    assert R.verify_receipt(receipt, key) == (True, "")

    members = [m for m in receipt if m != R.SIGNATURE_FIELD]
    assert "key_id" in members and "signer" in members and "scope" in members
    for member in members:
        dropped = {k: v for k, v in receipt.items() if k != member}
        ok, reason = R.verify_receipt(dropped, key)
        assert not ok, f"dropping {member!r} left the receipt verifying"
        assert reason
        edited = dict(receipt)
        edited[member] = "tampered"
        ok, _ = R.verify_receipt(edited, key)
        assert not ok, f"editing {member!r} left the receipt verifying"


def test_a_receipt_signed_with_one_key_is_refused_under_another(
        source, keyfile, tmp_path, capsys):
    """And the refusal NAMES the key it needs, so an operator holding the wrong
    one is told which that is rather than left to guess."""
    assert _issue(source, keyfile, "--replica", "r1@eu") == 0
    presented = tmp_path / "receipt.json"
    presented.write_text(capsys.readouterr().out, encoding="utf-8")
    other = tmp_path / "other.key"
    other.write_text("a-completely-different-signing-key\n", encoding="utf-8")

    rc = main(["retention-receipt", "--verify", str(presented),
               "--receipt-key", str(other)])
    captured = capsys.readouterr()
    assert rc == 1
    assert "REFUSED" in captured.out
    assert "signed by key" in captured.err


# -------------------------------------------------------- failure direction

def test_an_absent_key_on_the_issue_path_prints_no_document(source, capsys):
    """ISSUE direction: a receipt that cannot be signed is not printed. Nothing
    reaches stdout, so no reader can mistake an unsigned enumeration for a
    signed one."""
    rc = main(["retention-receipt", source, "--policy", "customer_pii",
               "--requester", "dpo", "--json"])
    captured = capsys.readouterr()
    assert rc == 1
    assert captured.out == ""
    assert "error:" in captured.err


def test_a_presented_receipt_with_no_key_is_refused_not_accepted(
        source, keyfile, tmp_path, capsys):
    """VERIFY direction, the opposite one: a receipt somebody PRESENTED and
    this process cannot check is REFUSED. "We could not check it" is never
    "it is valid"."""
    assert _issue(source, keyfile, "--replica", "r1@eu") == 0
    presented = tmp_path / "receipt.json"
    presented.write_text(capsys.readouterr().out, encoding="utf-8")

    rc = main(["retention-receipt", "--verify", str(presented)])
    captured = capsys.readouterr()
    assert rc == 1
    assert "REFUSED" in captured.out
    assert "no key to verify against" in captured.err


def test_a_presented_receipt_altered_after_issue_is_refused(
        source, keyfile, tmp_path, capsys):
    assert _issue(source, keyfile, "--replica", "r1@eu",
                  "--derivative", "vectors=embedding") == 0
    receipt = json.loads(capsys.readouterr().out)
    # the over-claim this row shape exists to prevent
    receipt["derivatives"][0]["covered"] = True
    receipt["derivatives"][0]["disposition"] = "covered"
    presented = tmp_path / "receipt.json"
    presented.write_text(json.dumps(receipt), encoding="utf-8")

    rc = main(["retention-receipt", "--verify", str(presented),
               "--receipt-key", keyfile])
    captured = capsys.readouterr()
    assert rc == 1
    assert "marked covered=True while the policy covers" in captured.err


def test_an_erasure_report_receipt_presented_as_this_one_is_refused(
        tmp_path, keyfile, capsys):
    """Domain separation, from the outside. The two protocols MAC the same
    shape of document; presenting one as the other is a mislabel, refused at
    the envelope before any MAC is computed."""
    report = {"ok": True, "kind": "revl.erase-report", "schema_version": "1.0",
              "realm": "R", "boundaryCrossings": {}, "otherRealmsUntouched": {},
              "inProcessStateGone": {"proven": True, "noResidueProof": {},
                                     "provisionsErased": []}}
    other = erasure_receipt.make_receipt(report, R.load_key(keyfile))
    presented = tmp_path / "other.json"
    presented.write_text(json.dumps(other), encoding="utf-8")

    rc = main(["retention-receipt", "--verify", str(presented),
               "--receipt-key", keyfile])
    captured = capsys.readouterr()
    assert rc == 1
    assert "envelope refused" in captured.err


def test_an_unverifiable_file_is_refused_rather_than_crashing(tmp_path, keyfile,
                                                              capsys):
    junk = tmp_path / "not-a-receipt.json"
    junk.write_text("{not json", encoding="utf-8")
    rc = main(["retention-receipt", "--verify", str(junk),
               "--receipt-key", keyfile])
    assert rc == 1
    assert "not JSON" in capsys.readouterr().err


# ------------------------------------------------- refusals on the issue path

def test_an_unauthorised_requester_is_refused_rather_than_signed(source,
                                                                 keyfile,
                                                                 capsys):
    rc = main(["retention-receipt", source, "--policy", "customer_pii",
               "--requester", "marketing", "--receipt-key", keyfile, "--json"])
    captured = capsys.readouterr()
    assert rc == 1
    assert captured.out == ""
    assert "may not request deletion" in captured.err


def test_a_policy_the_sources_do_not_declare_is_refused_naming_the_ones_they_do(
        source, keyfile, capsys):
    rc = main(["retention-receipt", source, "--policy", "nope",
               "--requester", "dpo", "--receipt-key", keyfile])
    captured = capsys.readouterr()
    assert rc == 1
    assert "customer_pii" in captured.err


def test_sources_declaring_no_policy_are_refused_rather_than_signed_empty(
        tmp_path, keyfile, capsys):
    """A signature over an enumeration held under a policy nobody wrote would
    put this protocol's authority behind a retention guarantee the source does
    not make."""
    bare = tmp_path / "bare.rvl"
    bare.write_text("fn id(x: Int) -> Int { x }\n", encoding="utf-8")
    rc = main(["retention-receipt", str(bare), "--policy", "customer_pii",
               "--requester", "dpo", "--receipt-key", keyfile])
    captured = capsys.readouterr()
    assert rc == 1
    assert captured.out == ""
    assert "declare no `retention` policy" in captured.err


def test_a_derivative_class_outside_the_vocabulary_is_refused(source, keyfile,
                                                              capsys):
    rc = main(["retention-receipt", source, "--policy", "customer_pii",
               "--requester", "dpo", "--receipt-key", keyfile,
               "--derivative", "thing=telepathy"])
    captured = capsys.readouterr()
    assert rc == 1
    assert captured.out == ""
    assert "unknown class" in captured.err


def test_a_derivative_flag_with_no_class_names_the_vocabulary(source, keyfile,
                                                              capsys):
    rc = main(["retention-receipt", source, "--policy", "customer_pii",
               "--requester", "dpo", "--receipt-key", keyfile,
               "--derivative", "search-index"])
    captured = capsys.readouterr()
    assert rc == 1
    assert "embedding" in captured.err


# ------------------------------------------------------------- the inventory

def test_an_inventory_carries_the_rows_a_command_line_cannot(tmp_path, source,
                                                             keyfile, capsys):
    inventory = tmp_path / "inv.json"
    inventory.write_text(json.dumps({
        "replicas": [{"token": f"row-{i}", "residence": "eu"}
                     for i in range(50)],
        "derivatives": [{"name": "nightly", "kind": "backup",
                         "residence": "eu"}],
    }), encoding="utf-8")

    rc = _issue(source, keyfile, "--inventory", str(inventory))
    receipt = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert receipt["summary"]["replicas"] == 50
    # `backup` is outside this policy's `summary, index`, so it is reported as
    # not covered rather than quietly counted as erased
    assert receipt["derivatives"][0]["disposition"] == "not-covered"


def test_an_inventory_member_this_reader_would_drop_is_refused_by_name(
        tmp_path, source, keyfile, capsys):
    """A row member silently ignored here is a fact the signature does not
    cover, which is how an enumeration comes to mean less than it appears to."""
    inventory = tmp_path / "inv.json"
    inventory.write_text(json.dumps({
        "replicas": [{"token": "r1", "regoin": "eu"}]}), encoding="utf-8")
    rc = _issue(source, keyfile, "--inventory", str(inventory))
    captured = capsys.readouterr()
    assert rc == 1
    assert captured.out == ""
    assert "regoin" in captured.err


def test_an_inventory_that_is_not_a_document_is_refused(tmp_path, source,
                                                        keyfile, capsys):
    inventory = tmp_path / "inv.json"
    inventory.write_text("[]", encoding="utf-8")
    rc = _issue(source, keyfile, "--inventory", str(inventory))
    assert rc == 1
    assert "not a document" in capsys.readouterr().err


# ------------------------------------------------------- the sibling protocol

def test_the_erase_report_scope_no_longer_claims_no_type_carries_a_deadline():
    """`erasure_receipt.SCOPE` rides INSIDE its signed body, so a sentence in it
    that has stopped being true is a false statement this tree signs. It said
    no type in the tree carries a retention deadline; `Retained[T, P]` does.

    The correction keeps the honest half — this document is over a realm's
    erase report and records no deadline — and drops the claim about the tree."""
    text = " ".join(erasure_receipt.SCOPE["doesNotProve"])
    assert "No type in this tree carries one" not in text
    assert "Retained[T, P]" in text
    assert "records no deadline" in text
