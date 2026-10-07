"""G4 / inverse in the formal model (issue #2097).

Two checker rules carry the `G4` code and the `inverse` category without being
the marker rule the `G` row states: `lower._check_site_release` (issue #1859)
asks whether a bracket's `undo` is the release its acquisition OWNS — the host
family's release on the handle it bound, or an `extern acquire`'s DECLARED
inverse on that handle — and `lower._method_effect_inverse` (issue #1945) asks
whether a host write's `undo` is its table entry on the same receiver with the
same key expression.

`RevL.G4Inverse` states that rule over the arm, the acquisition, the verb the
checker's own advice demanded and the spelling that advice told the author to
write. This module pins the differential-oracle row issue #2097 added for it:
the row's decider is pinned to the rule by `RevL.G4Inverse.rowB_iff`, and
`formal/harness/diff_corpus.py` carries what the checker reports as one `INV`
row per refusal, filed under `agree-G4` or the fatal `missed-G4`. Before the
row landed, those refusals had no model fact at all and were held in the
shrink-only `out-of-fragment-inverse` bucket by name.

That row is **the rule on the corpus, not coverage of the checker's walk**:
its premises ARE the checker's report, so it cannot witness that the walk is
complete. Roadmap item 418 step 9 is deliberately unclaimed. See
`formal/RevL/Theorems/G4Inverse.lean` and `formal/STATUS.md`.

This module runs in the plain `pytest tests/` job. The Lean half is checked
here when `lake` is on PATH, and by `make formal` always.
"""

from __future__ import annotations

import io
import shutil
import sys
import types
from contextlib import redirect_stdout
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))
from _load_by_path import load_by_path  # noqa: E402
from revl.lower import _HOST_WRITE_INVERSE  # noqa: E402
from revl.typecheck import _HOST_ACQUIRE_VERBS  # noqa: E402

#: The corpus's four `G4` / `inverse` refusals the `INV` row decides — the
#: documents that held `out-of-fragment-inverse` open.
FIXTURES = (
    "examples/rejections/g4_extern_undo_not_declared.rvl",
    "examples/rejections/g4_method_write_not_inverse.rvl",
    "examples/rejections/g4_undo_not_release.rvl",
    "tests/fixtures/canary_candidate_inverse.rvl",
)

#: What the checker's own refusal discovered, per file: the arm, the
#: acquisition, the inverse verb the checker's advice demanded, the spelling
#: that advice told the author to write, and the `undo` the checker READ at
#: the bracket it refused.
DISCOVERED = {
    FIXTURES[0]: ("extern", "log_open", "log_close", "log_close(log)",
                  "log_flush()"),
    FIXTURES[1]: ("write", "Map.insert", "remove", "store.remove(k)",
                  'store.remove("not-the-key")'),
    FIXTURES[2]: ("host", "Map.new", "drop", "store.drop()", 'store.get("x")'),
    FIXTURES[3]: ("write", "Map.insert", "remove", "store.remove(k)",
                  'store.remove("some-other-key")'),
}

#: The shrink-only ledger bucket the four documents were held in by name.
BUCKET = "out-of-fragment-inverse"


@pytest.fixture(scope="module")
def harness():
    return load_by_path("formal_diff_corpus_g4inverse_2097",
                        ROOT / "formal" / "harness" / "diff_corpus.py")


@pytest.fixture(scope="module")
def rows(harness):
    with redirect_stdout(io.StringIO()):
        out, _facts, _census = harness.export()
    return out


@pytest.fixture(scope="module")
def verdicts(harness, rows):
    return harness.reference_from_tsv(rows)


def _inv_rows(rows, rel=None):
    prefix = f"INV\t{rel}\t" if rel is not None else "INV\t"
    return [r for r in rows if r.startswith(prefix)]


def _align(harness, rels, verdicts, rows, tmp_path):
    (tmp_path / "harness" / "out").mkdir(parents=True, exist_ok=True)
    with pytest.MonkeyPatch.context() as mp, redirect_stdout(io.StringIO()):
        mp.setattr(harness, "FORMAL", tmp_path)
        fatal = harness.checker_alignment({rel: {} for rel in rels}, [],
                                          verdicts, rows)
        samples = dict(harness._ALIGN_SAMPLES)
    return {rel: k for k, v in samples.items() for rel in v}, fatal


def _spelled_verb(spelling):
    """The verb of a demanded spelling, respelled here rather than read from
    the harness, so the harness's own reader is checked and not restated."""
    return spelling.split("(")[0].rsplit(".", 1)[-1]


# ------------------------------------------------------------- the premise

@pytest.mark.parametrize("rel", FIXTURES)
def test_the_checker_refuses_every_fixture_with_the_inverse_code(harness, rel):
    assert harness.checker_code(rel) == ("G4", "inverse")


# --------------------------------------------------------------- the facts

@pytest.mark.parametrize("rel", FIXTURES)
def test_the_row_carries_what_the_checker_discovered(rows, rel):
    row = _inv_rows(rows, rel)
    assert len(row) == 1
    f = row[0].split("\t")
    assert len(f) == 9
    assert (f[2], f[3]) == ("G4", "inverse")
    assert (f[4], f[5], f[6], f[7], f[8]) == DISCOVERED[rel]


@pytest.mark.parametrize("rel", FIXTURES)
def test_the_verb_column_is_the_verb_the_advice_demanded(harness, rows, rel):
    """`verb` is read off the checker's own `` write `undo …` `` advice in
    every arm, so the column means one thing. The extern arm NEEDS that: its
    sentence names the acquisition that DECLARES the inverse (`` the inverse
    `log_open` declares ``), which is not the inverse's own verb — that
    appears only in the advice, and it is what the row carries."""
    f = _inv_rows(rows, rel)[0].split("\t")
    assert f[6] == harness._g4inv_demand_verb(f[7])


def test_the_extern_arms_verb_is_the_declaration_not_the_acquisition(rows):
    f = _inv_rows(rows, FIXTURES[0])[0].split("\t")
    assert (f[5], f[6], f[7]) == ("log_open", "log_close", "log_close(log)")
    assert f[6] != f[5]
    assert _spelled_verb(f[7]) == f[6]


def test_the_three_arms_are_the_three_the_rule_states(harness, rows):
    assert harness.G4INV_ARMS == ("host", "extern", "write")
    assert {f[4] for f in (r.split("\t") for r in _inv_rows(rows))} == set(
        harness.G4INV_ARMS)


def test_the_rows_tables_are_the_checkers_own(harness):
    """The harness restates `_HOST_ACQUIRE_VERBS` and `_HOST_WRITE_INVERSE`
    rather than importing them, so widening the checker's tables moves the
    checker alone and the refusal becomes the harness's fatal `missed-G4`. That
    only works while the restatements agree with the shipped tables, which is
    what this pins."""
    assert harness.G4INV_HOST_RELEASE == _HOST_ACQUIRE_VERBS
    assert harness.G4INV_WRITE_INVERSE == {
        k: v[0] for k, v in _HOST_WRITE_INVERSE.items()}


# ---------------------------------------------- both sides, the four files

@pytest.mark.parametrize("rel", FIXTURES)
def test_the_reference_decides_every_fixture_violated(harness, rows,
                                                      verdicts, rel):
    harness.reference_from_tsv(rows)
    assert verdicts.inv[rel] == "fail"


def test_every_fixture_files_under_agree_G4(harness, verdicts, rows,
                                             tmp_path):
    buckets, fatal = _align(harness, list(FIXTURES), verdicts, rows, tmp_path)
    assert fatal == []
    assert {buckets[rel] for rel in FIXTURES} == {"agree-G4"}


def test_a_blind_row_is_filed_under_missed_G4(harness, verdicts, rows,
                                              tmp_path):
    blind = verdicts._replace(inv={k: "ok" for k in verdicts.inv})
    _buckets, fatal = _align(harness, list(FIXTURES), blind, rows, tmp_path)
    assert fatal == [f"missed-G4: {rel}" for rel in FIXTURES]
    assert "missed-G4" in harness.FATAL_BUCKETS


def test_the_bucket_the_four_documents_held_is_empty(harness):
    """The point of the row: the four documents are MODELLED now, so the
    shrink-only `out-of-fragment-inverse` ledger holds no record for any of
    them — and holds no record at all, since they were the whole bucket."""
    held = ROOT / "formal" / "out_of_fragment_ledger" / BUCKET
    recorded = {p.relative_to(held).as_posix() for p in held.rglob("*.json")}
    assert recorded == set()


# ------------------------------------------------ the coverage ratchet

def test_the_coverage_ratchet_is_satisfied(harness, rows):
    harness.reference_from_tsv(rows)
    assert harness.g4inverse_coverage() == []


def test_the_ratchet_bites_without_any_inv_row(harness, rows):
    without = [r for r in rows if not r.startswith("INV\t")]
    harness.reference_from_tsv(without)
    findings = harness.g4inverse_coverage()
    harness.reference_from_tsv(rows)
    assert len(findings) == 1 and "no INV rows" in findings[0]


@pytest.mark.parametrize("rel", FIXTURES)
def test_the_ratchet_bites_when_the_site_is_the_demanded_spelling(
        harness, rows, rel):
    """The flip the ratchet executes: a row whose `site` column is the demanded
    spelling HOLD rather than violating, so a row that returned a constant — or
    that ignored the receiver or the key — fails here."""
    original = _inv_rows(rows, rel)[0]
    f = original.split("\t")
    tampered = [r if r != original else "\t".join(f[:8] + [f[7]])
                for r in rows]
    assert tampered != rows
    harness.reference_from_tsv(tampered)
    findings = harness.g4inverse_coverage()
    harness.reference_from_tsv(rows)
    assert any("does not read the site" in x for x in findings), findings


def test_the_ratchet_bites_when_the_acquisition_leaves_the_table(harness,
                                                                 rows):
    original = _inv_rows(rows, FIXTURES[2])[0]
    f = original.split("\t")
    tampered = [r if r != original else "\t".join(f[:5] + ["Log.open"] + f[6:])
                for r in rows]
    harness.reference_from_tsv(tampered)
    findings = harness.g4inverse_coverage()
    harness.reference_from_tsv(rows)
    assert any("table does not hold" in x for x in findings), findings


def test_the_ratchet_bites_when_the_demanded_verb_leaves_the_table(harness,
                                                                   rows):
    """A checker that began demanding a verb the row's own table does not hold
    must be made to say so here rather than agreeing with itself."""
    original = _inv_rows(rows, FIXTURES[2])[0]
    f = original.split("\t")
    tampered = [r if r != original else "\t".join(f[:6] + ["close"] + f[7:])
                for r in rows]
    harness.reference_from_tsv(tampered)
    findings = harness.g4inverse_coverage()
    harness.reference_from_tsv(rows)
    assert any("model is behind the checker" in x for x in findings), findings


def test_the_three_refusal_sentences_are_the_three_the_row_reads(harness,
                                                                 rows):
    """The arm is read off the checker's sentence, so the three sentences must
    be mutually exclusive — a message two of them matched would be decided on
    whichever arm the code tried first. And a refusal in a shape the row does
    not carry emits NO row and is a finding, not a silent `fail`."""
    for rel in FIXTURES:
        message = harness.checker_message(rel)
        matched = [i for i, re_ in enumerate((harness.G4INV_HOST_RE,
                                              harness.G4INV_EXTERN_RE,
                                              harness.G4INV_WRITE_RE))
                   if re_.match(message)]
        assert len(matched) == 1, (rel, matched)
    other = ("`store.insert` declares a persistence sink whose argument 1 is "
             "`Retained[T, p]` and that policy's deadline passed (G4)")
    assert not any(re_.match(other) for re_ in (harness.G4INV_HOST_RE,
                                                harness.G4INV_EXTERN_RE,
                                                harness.G4INV_WRITE_RE))


def test_a_refusal_in_an_unreadable_shape_is_a_finding_not_a_fail(harness):
    """`g4inverse_rows` must record what it could not read rather than decide
    it, so a checker that grew a fourth sentence reds the gate instead of
    producing a plausible-looking agreement."""
    rel = FIXTURES[0]
    unreadable = types.SimpleNamespace(
        code="G4", category="inverse",
        message="the `undo` of `effect store.insert(...)` must undo it "
                "somehow (G4)")
    # The row returns before it needs the AST, but it reads it first; an empty
    # program is enough to reach the sentence guard.
    empty = types.SimpleNamespace(externs=[], components=[], fn_decls=[],
                                  tests=[])
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(harness, "checker_refusal", lambda _rel: unreadable)
        assert harness.g4inverse_rows(rel, empty) == []
    try:
        assert any("not one of the three sentences" in x
                   for x in harness.g4inverse_coverage())
    finally:
        harness._G4INV_UNREADABLE.pop(rel, None)


# ------------------------------------------------------ the Lean half

def test_the_lean_oracle_agrees_on_every_fixture(harness, rows,
                                                 tmp_path_factory):
    if shutil.which("lake") is None:
        pytest.skip("lake is not on PATH; the formal gate runs this half")
    root = tmp_path_factory.mktemp("g4inverse_oracle")
    tsv = root / "corpus.tsv"
    tsv.write_text("\n".join(rows) + "\n", encoding="utf-8")
    formal = harness.parse_verdicts(
        harness.run_oracle(tsv, root / "formal_verdicts.tsv"))
    ref = harness.reference_from_tsv(rows)
    assert formal.inv == ref.inv
    assert {rel: formal.inv[rel] for rel in FIXTURES} == {
        rel: "fail" for rel in FIXTURES}
