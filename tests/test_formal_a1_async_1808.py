"""A1 async colour in the formal model (issue #1808).

The checker refuses, with code A1, a sync provide method that reaches an
async operation, an unawaited `effect` or `emit` step that reaches one, an
awaited step that reaches none, and an `undo` or `compensate` slot that
reaches one; and, uncoded, a provide method whose colour differs from its
service declaration's.

The model states the rules as `RevL.A1Async.SiteOK` over a site's kind and
heads, the file's async names and its `fn` call graph, and the signature rule
as `RevL.A1Async.SigOK`. `formal/harness/diff_corpus.py` exports `AN` (async
names), `AS` (sites) and `AG` (signatures) rows; the oracle prints `A1` and
`A1S` verdicts (`asyncRowB_iff`, `sigRowB_iff`), the reference recomputes them
with a true fixed point, and an A1 refusal files under `agree-A1`, or under
the fatal `missed-A1`.

This module runs in the plain `pytest tests/` job. The Lean half is checked
here when `lake` is on PATH, and by `make formal` always.
"""

from __future__ import annotations

import io
import shutil
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))
from _load_by_path import load_by_path  # noqa: E402

ARROW = "examples/rejections/a1_async_arrow_sync_type.rvl"
SIGNATURE = "examples/rejections/v2_async_signature_mismatch.rvl"
UNDO = "examples/rejections/a1_async_undo_suspends.rvl"

#: The A1 refusals the rows decide (the arrow-type one is not among them).
A1_FIXTURES = tuple(f"examples/rejections/{n}.rvl" for n in (
    "a1_async_compensate_suspends", "a1_async_effect_not_awaited",
    "a1_async_emit_step_not_awaited", "a1_async_extern_sync_method",
    "a1_async_op_sync_ternary", "a1_async_undo_suspends",
    "a1_await_emit_sync", "a1_effect_await_sync")) + (SIGNATURE,)

_HEAD = """\
extern emission async fn ho(u: Str) -> Str = @py { return u }
fn relay(u: Str) -> Str { return ho(u) }
service M { emission async fn complete(m: Str) -> Str  emission fn note(m: Str) -> Int }
service W { async fn heat() -> Int  fn cool() -> Int  async fn open(u: Str) -> Str }
"""

_SYNC = "fn f(x: Str) -> Str"
_ASYNC = "async fn f(x: Str) -> Str"
_PLAIN = "fn f(x) = x"


def _program(act: str, svc: str, method: str) -> str:
    return _HEAD + f"""service K {{ {svc} }}
component C requires m: M, w: W provides k: K {{
{act}
  provide k {{ {method} }}
}}
"""


#: Each shape with the checker's verdict: "accept", "A1", or "REVL" for the
#: uncoded signature refusal.
SHAPES = {
    "sync_method_reaches_op": ("", "emission " + _SYNC,
                               "fn f(x) = emit m.complete(x)", "A1"),
    "async_method_reaches_op": ("", "emission " + _ASYNC,
                                "async fn f(x) = emit m.complete(x)", "accept"),
    "sync_method_plain": ("", _SYNC, _PLAIN, "accept"),
    "sync_method_reaches_fn": ("", "emission " + _SYNC,
                               "fn f(x) = emit relay(x)", "A1"),
    "effect_unawaited": ('  let c = effect w.open("u") undo w.cool()',
                         _SYNC, _PLAIN, "A1"),
    "effect_awaited": ('  let c = effect await w.open("u") undo w.cool()',
                       _SYNC, _PLAIN, "accept"),
    "effect_await_sync": ("  let c = effect await w.cool() undo w.cool()",
                          _SYNC, _PLAIN, "A1"),
    "undo_suspends": ('  let c = effect await w.open("u") undo w.heat()',
                      _SYNC, _PLAIN, "A1"),
    "emit_unawaited": ('  emit m.complete("hi")', _SYNC, _PLAIN, "A1"),
    "emit_awaited": ('  await emit m.complete("hi")', _SYNC, _PLAIN, "accept"),
    "await_emit_sync": ('  await emit m.note("hi")', _SYNC, _PLAIN, "A1"),
    "compensate_suspends": (
        '  await emit m.complete("up") compensate m.complete("down")',
        _SYNC, _PLAIN, "A1"),
    "compensate_sync": (
        '  await emit m.complete("up") compensate m.note("down")',
        _SYNC, _PLAIN, "accept"),
    "signature_mismatch": ("", _ASYNC, _PLAIN, "REVL"),
}


@pytest.fixture(scope="module")
def harness():
    return load_by_path("formal_diff_corpus_a1_1808",
                        ROOT / "formal" / "harness" / "diff_corpus.py")


@pytest.fixture(scope="module")
def rows(harness):
    with redirect_stdout(io.StringIO()):
        out, _facts, _census = harness.export()
    return out


@pytest.fixture(scope="module")
def verdicts(harness, rows):
    return harness.reference_from_tsv(rows)


def _checker(source: str, name: str) -> str:
    from revl.compiler import compile_source
    from revl.diagnostics import classify
    from revl.errors import RevlError

    try:
        compile_source(source, name)
        return "accept"
    except RevlError as e:
        return classify(e).get("code") or "REVL"


@pytest.fixture(scope="module")
def shapes(harness, tmp_path_factory):
    root = tmp_path_factory.mktemp("a1_shapes")
    corpus = root / "corpus"
    corpus.mkdir()
    for name, (act, svc, method, _want) in SHAPES.items():
        (corpus / f"{name}.rvl").write_text(_program(act, svc, method),
                                            encoding="utf-8")
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(harness, "REPO", root)
        mp.setattr(harness, "CORPUS_DIRS", ("corpus",))
        with redirect_stdout(io.StringIO()):
            out, _facts, _census = harness.export()
    ref = harness.reference_from_tsv(out)
    formal = None
    if shutil.which("lake") is not None:
        tsv_path = root / "corpus.tsv"
        tsv_path.write_text("\n".join(out) + "\n", encoding="utf-8")
        formal = harness.parse_verdicts(
            harness.run_oracle(tsv_path, root / "formal_verdicts.tsv"))
    return out, ref, formal, root


def _admitted(ref, rel: str) -> bool:
    return all(x == "ok" for k, x in ref.async_sites.items() if k[0] == rel) \
        and all(x == "ok" for k, x in ref.async_sigs.items() if k[0] == rel)


# ------------------------------------------------------------- the premise

def test_the_checker_refuses_every_fixture(harness):
    for rel in A1_FIXTURES:
        want = "REVL" if rel == SIGNATURE else "A1"
        assert harness.checker_code(rel)[0] == want, rel
    assert harness.A1_SIGNATURE_MESSAGE in harness.checker_message(SIGNATURE)
    assert harness.A1_ARROW_MESSAGE in harness.checker_message(ARROW)


@pytest.mark.parametrize("name", sorted(SHAPES))
def test_the_checker_verdict_on_each_shape(name):
    act, svc, method, want = SHAPES[name]
    assert _checker(_program(act, svc, method), f"{name}.rvl") == want


# ------------------------------------------------------------- the facts

def test_the_undo_fixture_s_facts(rows):
    """`open_conn` reaches the async extern `ho` through the `fn` graph,
    the acquisition is awaited, and the `undo` calls the async `W.heat`."""
    assert f"AN\t{UNDO}\tho" in rows and f"AN\t{UNDO}\tW.heat" in rows
    sites = [r.split("\t")[3:] for r in rows if r.startswith(f"AS\t{UNDO}\tA\t")]
    assert ["0", "effectAwait", "open_conn"] in sites
    assert ["1", "undo", "W.heat"] in sites


# ------------------------------------------------- both sides, every shape

def test_the_reference_agrees_with_the_checker_on_every_shape(shapes):
    _out, ref, _formal, _root = shapes
    for name, (_a, _s, _m, want) in SHAPES.items():
        assert _admitted(ref, f"corpus/{name}.rvl") is (want == "accept"), name


def test_the_lean_oracle_agrees_on_every_shape(shapes):
    _out, ref, formal, _root = shapes
    if formal is None:
        pytest.skip("lake is not on PATH; the formal gate runs this half")
    assert formal.async_sites == ref.async_sites
    assert formal.async_sigs == ref.async_sigs


def test_the_shapes_file_under_agree(harness, shapes, tmp_path):
    out, ref, _formal, root = shapes
    rels = [f"corpus/{name}.rvl" for name in SHAPES]
    (tmp_path / "harness" / "out").mkdir(parents=True)
    with pytest.MonkeyPatch.context() as mp, redirect_stdout(io.StringIO()):
        mp.setattr(harness, "FORMAL", tmp_path)
        mp.setattr(harness, "REPO", root)
        fatal = harness.checker_alignment({rel: {} for rel in rels}, [], ref, out)
        samples = dict(harness._ALIGN_SAMPLES)
    assert fatal == []
    for name, (_a, _s, _m, want) in SHAPES.items():
        bucket = "agree-accept" if want == "accept" else "agree-A1"
        assert f"corpus/{name}.rvl" in samples.get(bucket, []), (name, bucket)


# -------------------------------------------------- the alignment arm

def test_every_fixture_files_under_agree_a1(harness, verdicts, rows, tmp_path):
    (tmp_path / "harness" / "out").mkdir(parents=True)
    rels = (*A1_FIXTURES, ARROW)
    with pytest.MonkeyPatch.context() as mp, redirect_stdout(io.StringIO()):
        mp.setattr(harness, "FORMAL", tmp_path)
        fatal = harness.checker_alignment({rel: {} for rel in rels}, [],
                                          verdicts, rows)
        samples = dict(harness._ALIGN_SAMPLES)
    assert fatal == []
    assert set(samples.get("agree-A1", [])) == set(A1_FIXTURES)
    assert ARROW in samples.get("out-of-fragment", [])


def test_a_blind_row_is_filed_under_missed_a1(harness, verdicts, rows, tmp_path):
    blind = verdicts._replace(
        async_sites={k: "ok" for k in verdicts.async_sites},
        async_sigs={k: "ok" for k in verdicts.async_sigs})
    (tmp_path / "harness" / "out").mkdir(parents=True)
    with pytest.MonkeyPatch.context() as mp, redirect_stdout(io.StringIO()):
        mp.setattr(harness, "FORMAL", tmp_path)
        fatal = harness.checker_alignment({UNDO: {}, SIGNATURE: {}}, [], blind,
                                          rows)
    assert sorted(fatal) == sorted([f"missed-A1: {UNDO}",
                                    f"missed-A1: {SIGNATURE}"])
    assert "missed-A1" in harness.FATAL_BUCKETS


# ------------------------------------------------ the coverage ratchet

def test_the_coverage_ratchet_is_satisfied(harness, rows):
    harness.reference_from_tsv(rows)
    assert harness.async_coverage() == []


def test_the_ratchet_bites_without_a_refused_teardown(harness, rows):
    refused = {k for k, x in harness.reference_from_tsv(rows).async_sites.items()
               if x == "fail"}
    without = [r for r in rows if not (
        r.startswith("AS\t") and r.split("\t")[4] in ("undo", "compensate")
        and tuple(r.split("\t")[1:4]) in refused)]
    harness.reference_from_tsv(without)
    findings = harness.async_coverage()
    harness.reference_from_tsv(rows)
    assert findings == ["async coverage: NO witness of a refused teardown "
                        "site — the A1 row would agree vacuously"]
