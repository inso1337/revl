"""Typed data retention: `Retained[T, P]` and the signed erasure receipt
(roadmap item 472, issue #824).

The item has two clauses and this file pins both halves of the retention one,
plus the receipt shape the retention half authorises:

  * a `Retained[T, P]` value past P's retention deadline is REFUSED at a
    persistence sink, and the refusal names the sink;
  * a declared legal hold OVERRIDES the deadline, because that is what a legal
    hold is;
  * an erasure request emits a SIGNED receipt naming each covered replica and
    each covered derivative;
  * a derivative the declaration does NOT cover is reported as `not-covered`
    and carries, inside the signed body, the statement that it is not claimed
    erased — never silence, and never a claim the policy cannot support.

The honest-scope tests at the bottom are as load-bearing as the refusals: the
receipt is an ENUMERATION plus a signature, not a proof of destruction, and the
tests that pin the envelope are what stop a later revision from letting a
document say two things about one row.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import RevlError  # noqa: E402
from revl import retention as R  # noqa: E402
from revl import erasure_receipt  # noqa: E402
from revl.compiler import compile_source  # noqa: E402
from revl.parser import Parser  # noqa: E402

KEY = b"retention-test-key-0123456789abc"
NOW = datetime(2026, 9, 13, 12, 0, 0, tzinfo=timezone.utc)

PAST = '''retention customer_pii {
  until: "2020-01-01T00:00:00Z"
  residence: "eu"
  deleters: dpo, subject
  derivatives: summary, index
}
'''
FUTURE = PAST.replace("2020-01-01", "2099-01-01")
HELD = PAST.replace('  residence: "eu"',
                    '  residence: "eu"\n  hold: "litigation-2025-07"')

# a persistence sink whose OWN parameter declares the retention policy
DECLARED_SINK = '''extern emission[db.insert] fn db_put(row: {t}) -> Int = @py {{ return 0 }}
service Ops {{ emission fn go(r: Str) -> Int }}
component Store provides ops: Ops {{
  provide ops {{
    fn go(r) {{
      let n = db_put(r)
      return n
    }}
  }}
}}
'''

# a retained SOURCE flowing into a persistence sink whose parameter carries no
# qualifier at all: the refusal follows the value, not the spelling at the sink
FLOW = '''extern pure fn load(k: Str) -> Retained[Str, customer_pii] = @py {{ return k }}
extern emission[{cap}] fn sink(row: Str) -> Int = @py {{ return 0 }}
service Ops {{ emission fn go(k: Str) -> Int }}
component Store provides ops: Ops {{
  provide ops {{
    fn go(k) {{
      let row = load(k)
      let n = sink(row)
      return n
    }}
  }}
}}
'''


def _policy(source: str = PAST, name: str = "customer_pii") -> R.RetentionPolicy:
    return R.policies(Parser(source, "p.rvl").parse())[name]


def _refuses(src: str) -> RevlError:
    with pytest.raises(RevlError) as excinfo:
        compile_source(src, "retain.rvl")
    return excinfo.value


# ===========================================================================
# 1. The exit criterion: refused at a persistence sink, BY NAME.
# ===========================================================================

def test_expired_retained_value_is_refused_at_a_persistence_sink_by_name():
    """The flow half: the value carries the policy, the sink declares durable
    storage, and the refusal names the sink and the scope."""
    err = _refuses(PAST + FLOW.format(cap="db.insert"))
    assert err.code == "G-RETAIN"
    assert "`sink`" in err.message, err.message
    assert "`db` crossing" in err.message
    assert "may not be written to durable storage" in err.message


def test_the_refusal_names_the_policy_and_the_deadline_it_missed():
    err = _refuses(PAST + FLOW.format(cap="db.insert"))
    assert "customer_pii" in err.message
    assert "2020-01-01T00:00:00Z" in err.hint


def test_a_declared_persistence_sink_parameter_is_refused_on_the_declaration():
    """The declaration half: a crossing that DECLARES it persists a retained
    value is refused off the declaration alone, with no call site needed."""
    err = _refuses(PAST + DECLARED_SINK.format(t="Retained[Str, customer_pii]"))
    assert err.code == "G-RETAIN"
    assert "`db_put` declares a persistence sink (`db`)" in err.message


def test_a_policy_whose_deadline_has_not_passed_compiles():
    compile_source(FUTURE + FLOW.format(cap="db.insert"), "retain.rvl")
    compile_source(FUTURE + DECLARED_SINK.format(t="Retained[Str, customer_pii]"),
                   "retain.rvl")


@pytest.mark.parametrize("scope", sorted(R.PERSISTENCE_SINK_SCOPES))
def test_every_declared_persistence_scope_refuses(scope):
    """Sink-ness is read off the crossing's DECLARED capability, so every scope
    in the set refuses and the set is the whole surface."""
    err = _refuses(PAST + FLOW.format(cap=scope))
    assert err.code == "G-RETAIN"
    assert f"`{scope}` crossing" in err.message


def test_a_non_persistence_sink_does_not_refuse():
    """A log is a disclosure sink, not a persistence sink. Retention is about
    durable storage; widening it to every crossing would make the refusal mean
    something the policy never said."""
    compile_source(PAST + FLOW.format(cap="log"), "retain.rvl")


# ===========================================================================
# 2. The legal hold overrides the deadline, as declared.
# ===========================================================================

def test_a_declared_legal_hold_overrides_the_deadline_at_the_sink():
    """The same expired deadline, the same sink, one added `hold:` line."""
    compile_source(HELD + FLOW.format(cap="db.insert"), "retain.rvl")
    compile_source(HELD + DECLARED_SINK.format(t="Retained[Str, customer_pii]"),
                   "retain.rvl")


def test_the_hold_is_applied_inside_the_policy_not_at_the_call_sites():
    """`expired` applies the hold itself, so no caller can consult the deadline
    while forgetting the exception."""
    held = _policy(HELD)
    assert held.held is True
    assert held.expired(NOW) is False
    assert _policy(PAST).expired(NOW) is True
    assert _policy(FUTURE).expired(NOW) is False


def test_an_empty_hold_is_refused_rather_than_read_as_no_hold():
    src = PAST.replace('  residence: "eu"', '  residence: "eu"\n  hold: ""')
    with pytest.raises(RevlError) as e:
        _policy(src)
    assert "has to NAME the hold" in e.value.hint


# ===========================================================================
# 3. The qualifier is orthogonal: the IR stays byte-identical.
# ===========================================================================

def test_the_qualifier_strips_to_its_base_type_and_the_ir_is_unchanged():
    """The whole point of joining the `Untrusted`/`Trusted`/`Secret` family: no
    backend needs to know this qualifier exists."""
    plain = compile_source(DECLARED_SINK.format(t="Str"), "retain.rvl")
    tagged = compile_source(
        FUTURE + DECLARED_SINK.format(t="Retained[Str, customer_pii]"),
        "retain.rvl")
    assert json.dumps(plain, sort_keys=True) == json.dumps(tagged, sort_keys=True)


def test_strip_qualifiers_handles_the_two_argument_head_anywhere():
    from revl.taint import strip_qualifiers, top_qualifier, retained_policy

    assert strip_qualifiers("Retained[Str, pii]") == "Str"
    assert strip_qualifiers("Result[Retained[Int, pii], Str]") == "Result[Int, Str]"
    assert strip_qualifiers("List[Retained[Str, pii]]") == "List[Str]"
    assert top_qualifier("Retained[Str, pii]") == "Retained"
    assert retained_policy("Retained[Str, pii]") == "pii"
    # arity is checked, so a user type that merely shares the name is left alone
    assert strip_qualifiers("Retained[A, B, C]") == "Retained[A, B, C]"
    assert top_qualifier("Retained[A, B, C]") is None


def test_a_retention_origin_is_not_an_authority_origin():
    """Retention is a time-and-residence dimension, orthogonal to 249's
    provenance dimension: a retained value at a `Trusted[T]` sink is not a G9
    refusal, because nothing about it is untrusted."""
    src = FUTURE + (
        'extern pure fn load(k: Str) -> Retained[Str, customer_pii] '
        '= @py { return k }\n'
        'extern emission[shell] fn run(cmd: Trusted[Str]) -> Int '
        '= @py { return 0 }\n'
        'service Ops { emission fn go(k: Str) -> Int }\n'
        'component Store provides ops: Ops {\n'
        '  provide ops {\n'
        '    fn go(k) {\n'
        '      let row = load(k)\n'
        '      let n = run(row)\n'
        '      return n\n'
        '    }\n'
        '  }\n'
        '}\n')
    compile_source(src, "retain.rvl")


def test_an_untrusted_and_retained_value_is_still_refused_at_an_authority_sink():
    """The filter never admits an authority flow: a value that is BOTH keeps its
    untrusted origin and is still refused by G9."""
    src = FUTURE + (
        'extern emission[web.fetch] fn fetch(u: Str) -> Untrusted[Str] '
        '= @py { return u }\n'
        'extern emission[shell] fn run(cmd: Trusted[Str]) -> Int '
        '= @py { return 0 }\n'
        'service Ops { emission fn go(u: Str) -> Int }\n'
        'component Store provides ops: Ops {\n'
        '  provide ops {\n'
        '    fn go(u) {\n'
        '      let row = emit fetch(u)\n'
        '      let n = run(row)\n'
        '      return n\n'
        '    }\n'
        '  }\n'
        '}\n')
    err = _refuses(src)
    assert err.code == "G9"


# ===========================================================================
# 4. The policy declaration: every rule is a refusal, not a shrug.
# ===========================================================================

def test_a_retention_qualifier_with_no_policy_is_refused():
    err = _refuses(PAST + DECLARED_SINK.format(t="Retained[Str]"))
    assert "with no retention policy" in err.message


def test_a_qualifier_naming_an_undeclared_policy_is_refused():
    err = _refuses(PAST + DECLARED_SINK.format(t="Retained[Str, nope]"))
    assert "which this program does not declare" in err.message
    assert "customer_pii" in err.hint


@pytest.mark.parametrize("field", ["until", "residence", "deleters"])
def test_a_missing_required_field_is_refused(field):
    lines = [ln for ln in PAST.splitlines(True)
             if not ln.strip().startswith(field + ":")]
    with pytest.raises(RevlError) as e:
        _policy("".join(lines))
    assert f"missing `{field}`" in e.value.message


def test_a_deadline_without_a_utc_offset_is_refused():
    with pytest.raises(RevlError) as e:
        _policy(PAST.replace('"2020-01-01T00:00:00Z"', '"2020-01-01T00:00:00"'))
    assert "no UTC offset" in e.value.message


def test_a_deadline_that_is_not_an_instant_is_refused():
    with pytest.raises(RevlError) as e:
        _policy(PAST.replace('"2020-01-01T00:00:00Z"', '"whenever"'))
    assert "not an RFC-3339 instant" in e.value.message


def test_an_unknown_derivative_class_is_refused():
    with pytest.raises(RevlError) as e:
        _policy(PAST.replace("summary, index", "summary, telepathy"))
    assert "unknown derivative class" in e.value.message
    assert "`telepathy`" in e.value.message


def test_derivatives_all_and_none_are_the_two_sentinels():
    assert _policy(PAST.replace("summary, index", "all")).derivatives == \
        R.DERIVATIVE_CLASSES
    assert _policy(PAST.replace("summary, index", "none")).derivatives == ()
    with pytest.raises(RevlError) as e:
        _policy(PAST.replace("summary, index", "all, summary"))
    assert "mixes" in e.value.message


def test_omitting_derivatives_covers_none():
    lines = [ln for ln in PAST.splitlines(True)
             if not ln.strip().startswith("derivatives:")]
    assert _policy("".join(lines)).derivatives == ()


def test_an_unknown_policy_field_is_refused_by_name():
    with pytest.raises(RevlError) as e:
        _policy(PAST.replace('  residence: "eu"',
                             '  residence: "eu"\n  forever: "yes"'))
    assert "has no field `forever`" in e.value.message


def test_a_duplicate_policy_is_refused():
    with pytest.raises(RevlError) as e:
        _policy(PAST + PAST)
    assert "duplicate `retention customer_pii`" in e.value.message


def test_the_evaluation_instant_is_overridable_for_a_reproducible_build():
    env = {R.AS_OF_ENV: "2021-06-01T00:00:00Z"}
    assert R.evaluation_instant(env) == datetime(2021, 6, 1, tzinfo=timezone.utc)
    assert not _policy(FUTURE).expired(R.evaluation_instant(env))
    assert _policy(PAST).expired(R.evaluation_instant(env))


def test_a_program_with_no_retention_declaration_reads_no_clock():
    """The whole surface is inert without a declaration, which is what keeps it
    free for every program that does not use it."""
    from revl.taint import extract_and_normalize

    model = extract_and_normalize(Parser(
        DECLARED_SINK.format(t="Str"), "p.rvl").parse())
    assert model.retention_policies == {}
    assert model.retention_as_of is None


# ===========================================================================
# 5. The erasure receipt: covered replicas AND covered derivatives, named.
# ===========================================================================

REPLICAS = (
    R.Replica("host:Store:db_put", "eu"),
    R.Replica("emit:Store:archive.put", "eu"),
    R.Replica("host:Store:backup_put", "us-east-1"),
)
DERIVATIVES = (
    R.Derivative("daily_digest", "summary", "emit:Store:sum.add"),
    R.Derivative("search_rows", "index", "host:Store:index_add"),
    R.Derivative("vector_index", "embedding", "host:Store:embed"),
)


def _receipt(policy=None, requester="dpo", replicas=REPLICAS,
             derivatives=DERIVATIVES, **kw):
    return R.make_receipt(policy or _policy(), requester, replicas,
                          derivatives, KEY, now=NOW, **kw)


def test_the_receipt_names_every_covered_replica_and_derivative():
    receipt = _receipt()
    ok, why = R.verify_receipt(receipt, KEY)
    assert ok, why
    named = {row["token"] for row in receipt["replicas"]}
    assert named == {r.token for r in REPLICAS}
    covered = {row["name"] for row in receipt["derivatives"] if row["covered"]}
    assert covered == {"daily_digest", "search_rows"}
    # each covered derivative names the crossing it was reached through
    for row in receipt["derivatives"]:
        if row["covered"]:
            assert row["token"]


def test_an_uncovered_derivative_is_reported_as_such_not_claimed_erased():
    """The exit criterion's fourth clause. `vector_index` is an embedding and
    the policy covers only summaries and indexes, so the row says so IN THE
    SIGNED BODY rather than being omitted or quietly counted as erased."""
    receipt = _receipt()
    row = next(r for r in receipt["derivatives"] if r["name"] == "vector_index")
    assert row["covered"] is False
    assert row["disposition"] == "not-covered"
    assert row["claim"] == R.NOT_CLAIMED
    assert "not claimed erased" in row["claim"]
    assert receipt["summary"]["uncoveredDerivatives"] == ["vector_index"]
    assert receipt["summary"]["coveredDerivatives"] == 2
    assert R.uncovered_derivatives(receipt) == [{
        "name": "vector_index", "derivativeKind": "embedding",
        "token": "host:Store:embed", "claim": R.NOT_CLAIMED}]


def test_a_policy_covering_no_derivative_claims_none_of_them():
    lines = [ln for ln in PAST.splitlines(True)
             if not ln.strip().startswith("derivatives:")]
    receipt = _receipt(policy=_policy("".join(lines)))
    assert all(row["covered"] is False for row in receipt["derivatives"])
    assert receipt["summary"]["uncoveredDerivatives"] == \
        sorted(d.name for d in DERIVATIVES)


def test_a_replica_outside_the_declared_residence_is_a_finding():
    receipt = _receipt()
    row = next(r for r in receipt["replicas"]
               if r["token"] == "host:Store:backup_put")
    assert row["disposition"] == "residence-mismatch"
    assert row["residence"] == "us-east-1"
    assert receipt["policy"]["residence"] == "eu"


def test_an_unauthorised_requester_is_refused_rather_than_signed():
    with pytest.raises(RevlError) as e:
        _receipt(requester="marketing")
    assert "may not request deletion" in e.value.message
    assert "`dpo`" in e.value.hint and "`subject`" in e.value.hint


def test_a_derivative_of_an_unknown_class_is_refused():
    with pytest.raises(RevlError) as e:
        _receipt(derivatives=(R.Derivative("x", "telepathy"),))
    assert "unknown class" in e.value.message


def test_a_legal_hold_withholds_every_row_rather_than_erasing_it():
    """Under a hold, nothing is reported as erasable — the hold is an
    instruction to KEEP, and a receipt that said otherwise would be signing the
    opposite of the policy in the same document."""
    receipt = _receipt(policy=_policy(HELD))
    assert {r["disposition"] for r in receipt["replicas"]} == \
        {"withheld:legal-hold"}
    held_derivatives = {r["name"]: r["disposition"]
                        for r in receipt["derivatives"]}
    assert held_derivatives["daily_digest"] == "withheld:legal-hold"
    # ...and an uncovered derivative is STILL uncovered under a hold: a hold
    # does not extend a policy's reach.
    assert held_derivatives["vector_index"] == "not-covered"
    assert R.verify_receipt(receipt, KEY)[0]


def test_the_receipt_is_deterministic_given_now():
    assert _receipt() == _receipt()


# ===========================================================================
# 6. The signature and the envelope.
# ===========================================================================

def test_a_wrong_key_is_refused_and_names_the_key_it_needs():
    ok, why = R.verify_receipt(_receipt(), b"another-key-0123456789abcdefghij")
    assert not ok
    assert "signed by key" in why


def test_an_edited_row_breaks_the_signature():
    receipt = _receipt()
    receipt["replicas"][0]["token"] = "host:Store:somewhere_else"
    ok, why = R.verify_receipt(receipt, KEY)
    assert not ok
    assert "altered after it was issued" in why


def test_a_dropped_claim_is_refused_by_the_envelope():
    """The `not claimed erased` statement cannot be detached from its row."""
    receipt = _receipt()
    row = next(r for r in receipt["derivatives"] if not r["covered"])
    del row["claim"]
    ok, why = R.verify_receipt(receipt, KEY)
    assert not ok
    assert "does not carry the `not claimed erased` statement" in why


def test_a_derivative_claimed_covered_by_a_policy_that_does_not_cover_it():
    """The load-bearing envelope check: `covered` is a reading of the policy in
    the SAME signed body, so an over-claim is refused even with an intact MAC."""
    receipt = _receipt()
    row = next(r for r in receipt["derivatives"] if r["name"] == "vector_index")
    row["covered"] = True
    row["disposition"] = "covered"
    del row["claim"]
    receipt["summary"]["coveredDerivatives"] = 3
    receipt["summary"]["uncoveredDerivatives"] = []
    receipt["summary"]["byDerivativeDisposition"] = {
        "covered": 3, "not-covered": 0, "withheld:legal-hold": 0}
    receipt["signature"] = R._mac(receipt, KEY)
    ok, why = R.verify_receipt(receipt, KEY)
    assert not ok
    assert "while the policy covers" in why


def test_a_summary_that_disagrees_with_its_own_rows_is_refused():
    receipt = _receipt()
    receipt["summary"]["replicas"] = 99
    receipt["signature"] = R._mac(receipt, KEY)
    ok, why = R.verify_receipt(receipt, KEY)
    assert not ok
    assert "not the tally of this receipt's own rows" in why


def test_a_requester_outside_the_policys_own_deleters_is_refused():
    receipt = _receipt()
    receipt["request"]["requester"] = "marketing"
    receipt["signature"] = R._mac(receipt, KEY)
    ok, why = R.verify_receipt(receipt, KEY)
    assert not ok
    assert "is not one of the policy's own deleters" in why


def test_a_mislabelled_document_is_refused_before_the_mac():
    receipt = _receipt()
    receipt["kind"] = "revl.erasure-receipt"
    ok, why = R.verify_receipt(receipt, KEY)
    assert not ok
    assert "envelope refused: kind" in why


def test_the_domain_tag_separates_this_protocol_from_the_erasure_receipt():
    """Four signed protocols, four domains: without a distinct tag a retention
    receipt would verify as an erase-report receipt under the same key."""
    assert R.RECEIPT_DOMAIN != erasure_receipt.RECEIPT_DOMAIN
    assert R.KEY_ID_DOMAIN != erasure_receipt.KEY_ID_DOMAIN
    assert R.key_id(KEY) != erasure_receipt.key_id(KEY)
    body = {k: v for k, v in _receipt().items() if k != "signature"}
    assert R._mac(body, KEY) != erasure_receipt._mac(body, KEY)


def test_the_canonical_bytes_and_the_key_rule_are_attests():
    """Called rather than restated, which is what makes a third-party verifier's
    reconstruction exact (the drift `erasure_receipt` was corrected for)."""
    from revl import attest

    receipt = _receipt(signer="Jose Muller")
    assert R.verify_receipt(receipt, KEY)[0]
    body = {k: v for k, v in receipt.items() if k != "signature"}
    assert attest._canonical_bytes(body).decode("utf-8")


def test_a_signer_with_no_utf8_spelling_refuses_rather_than_crashing():
    class Unspellable:
        def __repr__(self):
            return "<unspellable>"

    with pytest.raises(RevlError) as e:
        _receipt(signer=Unspellable())
    assert "cannot be signed" in e.value.message


def test_an_empty_key_is_refused_on_both_sides():
    with pytest.raises(RevlError):
        _receipt_key_empty()
    assert R.verify_receipt(_receipt(), b"")[0] is False


def _receipt_key_empty():
    return R.make_receipt(_policy(), "dpo", REPLICAS, DERIVATIVES, b"", now=NOW)


# ===========================================================================
# 7. Honest scope: what the artifact says about itself.
# ===========================================================================

def test_the_scope_travels_inside_the_signed_body():
    receipt = _receipt()
    assert receipt["scope"]["doesNotProve"]
    assert any("not a proof of destruction" in line
               for line in receipt["scope"]["doesNotProve"])
    assert any("outside revl's boundary" in line
               for line in receipt["scope"]["doesNotProve"])
    # removing it breaks the signature, so the disclaimer cannot be detached
    del receipt["scope"]
    assert R.verify_receipt(receipt, KEY)[0] is False


def test_the_erasure_receipt_modules_retention_disclaimer_is_now_out_of_date():
    """`erasure_receipt` was landed with a paragraph saying the retention half
    is not implemented. This file is that half, so the paragraph is updated
    there; this test is what keeps the two from drifting apart again."""
    text = (ROOT / "src" / "revl" / "erasure_receipt.py").read_text()
    assert "revl.retention" in text


# ===========================================================================
# 8. The multi-file path (the item-256 defect class).
# ===========================================================================

def test_a_policy_survives_the_multi_file_merge(tmp_path):
    """`compile_files` once dropped `program.secrets`, so the single-source path
    admitted a program the CLI path refused. Same list, same discipline."""
    from revl import compile_files

    (tmp_path / "policy.rvl").write_text(
        FUTURE + 'pub extern pure fn load(k: Str) -> '
                 'Retained[Str, customer_pii] = @py { return k }\n')
    (tmp_path / "app.rvl").write_text(
        'use "policy.rvl" { load }\n'
        'extern emission[log] fn logit(row: Str) -> Int = @py { return 0 }\n'
        'service Ops { emission fn go(k: Str) -> Int }\n'
        'component Store provides ops: Ops {\n'
        '  provide ops {\n'
        '    fn go(k) {\n'
        '      let row = load(k)\n'
        '      let n = logit(row)\n'
        '      return n\n'
        '    }\n'
        '  }\n'
        '}\n')
    compile_files([str(tmp_path / "app.rvl")])


def test_the_rejection_fixture_refuses_under_any_clock():
    """The corpus fixture's deadline is absolutely past, so it is not a test
    that starts failing on a particular date."""
    from revl import compile_files

    path = ROOT / "examples" / "rejections" / \
        "gretain_expired_at_persistence_sink.rvl"
    with pytest.raises(RevlError) as e:
        compile_files([str(path)])
    assert e.value.code == "G-RETAIN"
    assert _policy().until < datetime.now(timezone.utc) - timedelta(days=365)
