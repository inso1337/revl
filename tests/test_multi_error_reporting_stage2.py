"""Roadmap 386, Stage 2: statement-boundary recovery and the poison sentinel.

Stage 1 (test_multi_error_reporting.py) recovers at the COMPONENT boundary: one
component's whole-body abort no longer stops the rest of the compile. Stage 2
adds the finer, statement-boundary recovery INSIDE a component body, on top of
the same collected `errors` list:

  * several independent expression-level type mismatches in ONE component are all
    reported in a single pass (not just the first), and
  * a binding whose initializer is a type error, used in many later statements,
    produces EXACTLY ONE diagnostic (at the initializer). The `POISON` sentinel
    (typecheck.py) is bound in place of the failed value; because it reads as a
    silent, absorbing wildcard, every later use stays quiet — one real mismatch
    never fabricates N cascades at every later use of the poisoned binding.

The plumbing carries no diagnostics sink through infer_ast/check_ast/infer_ir:
the expression-level `RevlError` raise already reaches the statement boundary,
where the component body loop catches it, drains it into the Stage-1 `errors`
list, poisons the failed binding, and resumes at the next statement.

These tests pin the two Stage-2 exit tests from the design (§"Exit tests for
stage 2") plus the byte-identity regressions: a single mismatch is still a
one-element list, and a clean compile still returns a normal IR with no poison
residue.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_files  # noqa: E402
from revl.diagnostics import report  # noqa: E402
from revl.errors import RevlError  # noqa: E402


# A service with two differently-typed operations, so three call sites can each
# be independently ill-typed in a distinct way (Str-for-Int, Int-for-Str,
# Bool-for-Int) rather than three copies of one mistake.
_SINK = """\
service Sink {
  fn i(v: Int) -> Int
  fn s(v: Str) -> Int
}
"""


def _refuse(tmp_path, monkeypatch, text, name="prog.rvl"):
    monkeypatch.chdir(tmp_path)
    path = tmp_path / name
    path.write_text(text)
    with pytest.raises(RevlError) as excinfo:
        compile_files([str(path)])
    return excinfo.value


def test_three_expression_mismatches_in_one_component_report_three(tmp_path, monkeypatch):
    """Design exit test 1: a component with THREE independent expression-level
    type mismatches reports THREE diagnostics — not one (abort-on-first), and
    not three-plus-cascades. Each is on its own statement line, and the walk
    resumes cleanly after each refusal (statement-boundary synchronization)."""
    src = _SINK + """\
component C requires sink: Sink {
  effect sink.i("x") undo sink.i(1)
  effect sink.s(1) undo sink.s("y")
  effect sink.i(true) undo sink.i(1)
}
"""
    error = _refuse(tmp_path, monkeypatch, src)
    diags = report(error)["diagnostics"]
    assert len(diags) == 3, [d["message"] for d in diags]
    # three distinct statement locations, in compile order
    assert [d["line"] for d in diags] == [6, 7, 8], diags
    # every diagnostic is a real type mismatch (T1), none fabricated
    assert all(d["code"] == "T1" for d in diags), diags
    joined = " ".join(d["message"] for d in diags)
    assert "got `Str`" in joined and "got `Int`" in joined and "got `Bool`" in joined, joined


def test_poisoned_binding_used_five_times_reports_exactly_one(tmp_path, monkeypatch):
    """Design exit test 2 (the cascade-suppression regression): a binding whose
    initializer is a type error, USED IN FIVE later statements, produces EXACTLY
    ONE diagnostic — at the initializer, where the poison is born. Without the
    sentinel the five uses of the (unbound/mistyped) binding would fabricate five
    more diagnostics; the poison makes every later use silent."""
    src = _SINK + """\
component C requires sink: Sink {
  let h = effect sink.i("bad") undo sink.i(1)
  effect sink.i(h) undo sink.i(1)
  effect sink.i(h) undo sink.i(1)
  effect sink.i(h) undo sink.i(1)
  effect sink.i(h) undo sink.i(1)
  effect sink.i(h) undo sink.i(1)
}
"""
    error = _refuse(tmp_path, monkeypatch, src)
    diags = report(error)["diagnostics"]
    assert len(diags) == 1, [d["message"] for d in diags]
    # the single diagnostic is the initializer's mismatch, on the `let` line
    assert diags[0]["line"] == 6, diags
    assert diags[0]["code"] == "T1", diags
    assert "got `Str`" in diags[0]["message"], diags


def test_mismatches_and_a_poisoned_binding_together(tmp_path, monkeypatch):
    """The two mechanisms compose: a poisoned binding suppresses its own five
    downstream uses (one diagnostic) while two later INDEPENDENT mismatches are
    still each reported. Three diagnostics total, no cascade from the poison."""
    src = _SINK + """\
component C requires sink: Sink {
  let h = effect sink.i("bad") undo sink.i(1)
  effect sink.i(h) undo sink.i(1)
  effect sink.i(h) undo sink.i(1)
  effect sink.i(h) undo sink.i(1)
  effect sink.i(h) undo sink.i(1)
  effect sink.i(h) undo sink.i(1)
  effect sink.s(2) undo sink.s("y")
  effect sink.i(false) undo sink.i(1)
}
"""
    error = _refuse(tmp_path, monkeypatch, src)
    diags = report(error)["diagnostics"]
    assert len(diags) == 3, [d["message"] for d in diags]
    # the initializer, then the two later independent mismatches (the five uses
    # of `h` in between contribute nothing)
    assert [d["line"] for d in diags] == [6, 12, 13], diags


def test_single_expression_mismatch_still_one_element(tmp_path, monkeypatch):
    """Byte-identity floor: a single expression-level mismatch still yields a
    one-element diagnostics list, byte-identical to what an abort-on-first
    compile reported — Stage 2 must not change the single-error shape."""
    src = _SINK + """\
component C requires sink: Sink {
  effect sink.i("x") undo sink.i(1)
}
"""
    error = _refuse(tmp_path, monkeypatch, src)
    diags = report(error)["diagnostics"]
    assert len(diags) == 1, diags
    assert diags[0]["line"] == 6 and diags[0]["code"] == "T1", diags
    # the carrier's primary fields mirror the single error (Stage 1, Change 3),
    # so a legacy single-error consumer reading `str(error)` is unchanged
    assert "expects `Int`, got `Str`" in str(error)


def test_clean_component_body_compiles_no_poison_residue(tmp_path, monkeypatch):
    """A component body with no refusal compiles to a normal IR: no statement is
    recovered, so the component is NOT marked poisoned and no `POISON` type leaks
    into the emitted document."""
    src = _SINK + """\
component C requires sink: Sink {
  effect sink.i(1) undo sink.i(0)
}
component Keeper provides sink: Sink {
  provide sink { fn i(v) = v  fn s(v) = 0 }
}
"""
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "clean.rvl"
    path.write_text(src)
    ir = compile_files([str(path)])
    assert "components" in ir
    assert not any(c.get("poisoned") for c in ir["components"])
    # no synthetic sentinel spelling reaches the serialized IR
    import json
    assert "!poison" not in json.dumps(ir)
