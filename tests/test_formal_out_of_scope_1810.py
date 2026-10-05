"""The named `out-of-scope` bucket (issue #1810).

`checker_alignment`'s fall-through used to mix refusals under a rule the
model could carry with refusals that are out of scope by kind: the type
checker, and name resolution of declarations and of the lifecycle test DSL.
The second kind now files under an informational `out-of-scope` bucket by an
explicit rule (`out_of_scope`), so `out-of-fragment` holds only unbuilt work.
"""

from __future__ import annotations

import io
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))
from _load_by_path import load_by_path  # noqa: E402

#: The documents out of scope by kind.
OUT_OF_SCOPE = (
    *(f"examples/rejections/{n}.rvl" for n in (
        "t16_provide_method_missing_return", "t1_service_arg_type",
        "t30_field_read_on_any_provide_method",
        "t31_index_non_int_provide_method", "t3_config_default_type",
        "t4_field_arg_type", "t7_provide_param_annotation_mismatch",
        "t2_null_in_expression", "lifecycle_config_unknown_field",
        "lifecycle_double_load", "lifecycle_unknown_assertion",
        "lifecycle_unknown_component", "lifecycle_unknown_operation",
        "service_compat_duplicate")),
    "examples/ecosystem-consumer/candidates/leaky_tool.rvl",
    *(f"tests/fixtures/record_field_call/t1_{n}.rvl" for n in (
        "arrow_field", "declared_type", "in_place", "let", "nested_field",
        "return")),
    # issue #1900: `try` in a provide method, a T1 refusal of the type checker
    # (the other `t1_` documents of that corpus are module fns only, so they
    # have no composition to model and file as such)
    "tests/fixtures/try_expr/t1_provide_method.rvl",
)

#: Out of fragment, NOT out of scope: a guarantee-coded refusal the model
#: does not carry yet.
ARROW = "examples/rejections/a1_async_arrow_sync_type.rvl"

T1_NEWCOMER = """\
service Db { fn query(sql: Str) -> Int }
component C requires db: Db {
  let r = effect db.query(1) undo r.drop()
}
"""


@pytest.fixture(scope="module")
def harness():
    return load_by_path("formal_diff_corpus_oos_1810",
                        ROOT / "formal" / "harness" / "diff_corpus.py")


@pytest.fixture(scope="module")
def alignment(harness, tmp_path_factory):
    tmp = tmp_path_factory.mktemp("oos")
    (tmp / "harness" / "out").mkdir(parents=True)
    with redirect_stdout(io.StringIO()):
        rows, facts, census = harness.export()
    ref = harness.reference_from_tsv(rows)
    with pytest.MonkeyPatch.context() as mp, redirect_stdout(io.StringIO()):
        mp.setattr(harness, "FORMAL", tmp)
        fatal = harness.checker_alignment(facts, census["componentless"], ref,
                                          rows)
        samples = dict(harness._ALIGN_SAMPLES)
    return samples, fatal


def test_the_documents_file_under_out_of_scope(alignment):
    samples, fatal = alignment
    assert fatal == []
    assert set(samples.get("out-of-scope", [])) == set(OUT_OF_SCOPE)
    assert not set(OUT_OF_SCOPE) & set(samples.get("out-of-fragment", []))


def test_a_guarantee_coded_refusal_stays_out_of_fragment(alignment):
    samples, _fatal = alignment
    assert ARROW in samples.get("out-of-fragment", [])


def test_no_guarantee_code_is_ever_out_of_scope(harness):
    """Whatever the message, a refusal under a guarantee code is not out of
    scope: the classifier is keyed on the code first."""
    from revl.diagnostics import GUARANTEES

    messages = ["", "unknown component `X`", "duplicate service `S`",
                "`x` is already loaded", "unknown lifecycle assertion `y`"]
    for code in GUARANTEES:
        if code in harness.OUT_OF_SCOPE_CODES:
            continue
        for m in messages:
            assert not harness.out_of_scope(code, m), (code, m)


def test_an_uncoded_refusal_needs_a_matching_message(harness):
    assert harness.out_of_scope("REVL", "unknown component `Ghost`")
    assert not harness.out_of_scope("REVL", "`isolate` must precede every effect")
    assert not harness.out_of_scope("UNCODED", "unknown component `Ghost`")


def test_a_new_t1_fixture_lands_in_out_of_scope(harness, tmp_path):
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "t1_newcomer.rvl").write_text(T1_NEWCOMER, encoding="utf-8")
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(harness, "REPO", tmp_path)
        mp.setattr(harness, "CORPUS_DIRS", ("corpus",))
        with redirect_stdout(io.StringIO()):
            rows, facts, census = harness.export()
        ref = harness.reference_from_tsv(rows)
        (tmp_path / "formal" / "harness" / "out").mkdir(parents=True)
        mp.setattr(harness, "FORMAL", tmp_path / "formal")
        assert harness.checker_code("corpus/t1_newcomer.rvl")[0] == "T1"
        with redirect_stdout(io.StringIO()):
            fatal = harness.checker_alignment(facts, census["componentless"],
                                              ref, rows)
        samples = dict(harness._ALIGN_SAMPLES)
    assert fatal == []
    assert samples.get("out-of-scope") == ["corpus/t1_newcomer.rvl"]


def test_the_bucket_is_informational(harness):
    assert "out-of-scope" not in harness.FATAL_BUCKETS
    assert "out-of-scope" not in harness.OOF_RATCHET_BUCKETS
