"""Issue #844: a record literal returned for a type this compilation does not
declare is reported as an unresolved type, not as a mismatch of a `T1` the
checker never made.

The reproducer is a pair of files: `a.rvl` declares `Row`, `b.rvl` returns a
record literal for it. Compiled together the pair is clean, so the record
literal IS the declared shape. Compiled alone, `b.rvl` used to say
"this function's return expects `Row`, got `{n: Int, name: Str}`" with
`code: T1`, `category: type-mismatch` and the T1 fix ("make the types agree at
the call site, or change the declaration") - three false statements about a
program whose only problem is compilation scope.

What is pinned here:
  * the honest diagnostic (its code, category, message, expected/actual);
  * the two-file compile stays clean;
  * a record that genuinely disagrees with a *declared* type is still `T1`;
  * an undeclared nominal that is NOT met by a record literal is still `T1`:
    the opaque-nominal contract (docs/generics.md, docs/contract-errata.md) is
    untouched, so `-> Row { return 1 }` and the arrow-annotation hygiene case
    (examples/rejections/t35_...) keep their mismatches.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import RevlError, compile_files, compile_source  # noqa: E402
from revl.diagnostics import report  # noqa: E402

A_ROW = "pub type Row = { name: Str, n: Int }\n"

B_MAKE_ROW = """pub fn make_row() -> Row {
  return { name: "x", n: 1 }
}
"""

WRONG_MAKE_ROW = """pub fn make_row() -> Row {
  return { name: "x", n: "y" }
}
"""


def _diagnostic(source: str, filename: str = "b.rvl") -> dict:
    with pytest.raises(RevlError) as excinfo:
        compile_source(source, filename)
    record = report(excinfo.value)
    assert record["ok"] is False
    return record["diagnostics"][0]


def test_issue_844_reports_the_missing_declaration_not_a_mismatch():
    """The reproducer verbatim: `Row` has no declaration here, so there is
    nothing to compare the shape against."""
    d = _diagnostic(B_MAKE_ROW)
    assert d["code"] == "T-UNRESOLVED"
    assert d["category"] == "unresolved-type"
    assert d["file"] == "b.rvl"
    assert d["message"] == (
        "this function's return expects `Row`, got `{n: Int, name: Str}`; "
        "`Row` has no declaration in this compilation"
    )
    assert d["expected"] == "Row"
    assert d["actual"] == "{n: Int, name: Str}"
    assert "refused as unresolved" in d["guarantee"]
    assert "declare the type in this compilation" in d["fix"]
    # the false reading the issue is about: the record already satisfies the
    # declaration, so "the types disagree" must not be the claim a caller gets.
    assert d["code"] != "T1"
    assert d["category"] != "type-mismatch"


def test_issue_844_the_same_pair_compiles_clean_together(tmp_path):
    """`revl compile a.rvl b.rvl` is exit 0 before and after: with the
    declaration in scope the record literal checks against it."""
    (tmp_path / "a.rvl").write_text(A_ROW, encoding="utf-8")
    (tmp_path / "b.rvl").write_text(B_MAKE_ROW, encoding="utf-8")
    ir = compile_files([str(tmp_path / "b.rvl"), str(tmp_path / "a.rvl")])
    assert ir["types"]["Row"]["kind"] == "record"
    assert [fn["name"] for fn in ir["functions"]] == ["make_row"]
    assert ir["functions"][0]["returns"] == "Row"


def test_issue_844_a_wrong_record_against_a_declared_type_is_still_t1():
    """Do not weaken the checker: with `Row` declared, a field of the wrong
    type is a real mismatch, and the message says which field."""
    d = _diagnostic(A_ROW + WRONG_MAKE_ROW)
    assert d["code"] == "T1"
    assert d["category"] == "type-mismatch"
    assert d["message"] == "field `n` of `Row` expects `Int`, got `Str`"


def test_issue_844_a_non_record_value_is_still_an_ordinary_mismatch():
    """An undeclared multi-letter name is an ordinary opaque nominal that
    unifies with nothing (docs/contract-errata.md), so a scalar meeting it is
    still `T1` - the new code is for the case that cannot be checked at all."""
    d = _diagnostic("""
pub fn make_row() -> Row {
  return 1
}
""")
    assert d["code"] == "T1"
    assert d["category"] == "type-mismatch"
    assert d["message"] == "this function's return expects `Row`, got `Int`"


def test_issue_844_a_let_annotation_is_named_the_same_way():
    """The same boundary one statement up: a `let` annotated with an
    undeclared type and initialised with a record literal."""
    d = _diagnostic("""
pub fn f() -> Int {
  let r: Row = { name: "x" }
  return 1
}
""")
    assert d["code"] == "T-UNRESOLVED"
    assert d["category"] == "unresolved-type"
    assert d["message"].endswith("`Row` has no declaration in this compilation")


def test_issue_844_arrow_annotation_hygiene_is_unchanged():
    """`Q` in an arrow annotation is opaque on purpose (t35, item 75(a) rule
    G): a record literal is not a reason to reclassify it, and neither is a
    scalar. Pinned through the checked-in rejection example."""
    example = (
        ROOT / "examples" / "rejections" / "t35_arrow_annotation_not_quantified.rvl"
    )
    with pytest.raises(RevlError) as excinfo:
        compile_files([str(example)])
    d = report(excinfo.value)["diagnostics"][0]
    assert d["code"] == "T1"
    assert d["message"] == "argument 1 of `f` expects `Q`, got `Int`"
