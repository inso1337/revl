"""G1 declared access in the formal model (issue #1807).

The checker refuses a call head whose root names no declared requirement,
with code G1 ("`db` is not a declared requirement of C"), wherever the head
sits: the activation body, a provide method, inside a block, in a condition,
and as an `intercept` target.

The model states the rule as `RevL.G1Access.AccessOK`: every ACCESS root of a
component is one of its requires. `formal/harness/diff_corpus.py` exports the
access roots as `GA` rows: the roots of the component's call heads, less the
ones the checker resolves without a requirement (a binding in the component,
a module callable, an import, a host family, a constructor), plus `intercept`
targets that are not provisions. The oracle prints one `G1` verdict per
component (`accessRowB`, bridged by `accessRowB_iff`), the reference
recomputes it, and a G1 refusal files under `agree-G1`, or under the fatal
`missed-G1` when the row admits it.

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

LOGGER = "examples/rejections/g1_undeclared_access.rvl"
INTERCEPT = "examples/rejections/v2_intercept_undeclared.rvl"

#: The corpus's G1 refusals, which sat in the generic `out-of-fragment`.
G1_FIXTURES = (
    LOGGER,
    "examples/rejections/g1_unknown_upper_host.rvl",
    "examples/ecosystem-consumer-js/candidates/undeclared_tool.rvl",
    "examples/ecosystem-consumer-rs/candidates/undeclared_tool.rvl",
    INTERCEPT,
    *(f"tests/fixtures/gate_block_nesting/g1_after_{s}.rvl" for s in (
        "else", "else_if", "for", "guard_else", "guard_provide",
        "guard_setup", "if", "nested_guard", "oneline_guard", "while")),
    *(f"tests/fixtures/gate_block_nesting/g1_in_{s}.rvl" for s in (
        "braceless_else", "if_condition", "oneline_else", "oneline_if",
        "oneline_while", "while_condition")),
)

_HEAD = """\
service Net { fn ping() -> Int }
service K { fn f(n: Int) -> Int }
fn helper(x: Int) -> Int { return x }
"""


def _component(act: str, body: str) -> str:
    lines = "\n".join(f"      {x}" for x in body.split("\n"))
    return _HEAD + f"""component C requires net: Net provides k: K {{
{act}
  provide k {{
    fn f(n) {{
{lines}
      return 1
    }}
  }}
}}
"""


#: Each shape with the checker's verdict: True admitted, False refused G1.
SHAPES = {
    "activation_undeclared": (
        "  let r = effect db.query() undo r.drop()", "let y = 1", False),
    "method_undeclared": ("", "let v = db.get(n)", False),
    "method_declared": ("", "let v = net.ping()", True),
    "module_fn": ("", "let v = helper(n)", True),
    "constructor": ("", "let o = Some(n)", True),
    "if_condition_undeclared": ("", "if (db.get(n) == 1) { return 1 }", False),
    "else_arm_undeclared": (
        "", "if (n > 0) { let a = 1 } else { let b = db.get(n) }", False),
    "while_body_undeclared": (
        "", "var i = 0\nwhile (i < 1) { let v = db.get(n)\ni = i + 1 }", False),
    "intercept_undeclared": ("  intercept db with { quota: 5 }", "let y = 1", False),
    "intercept_declared": ("  intercept net with { quota: 5 }", "let y = 1", True),
    "upper_host_undeclared": ("", "let v = StreamB.source()", False),
    "host_family": ("  let m = effect Map.new() undo m.drop()", "let y = 1", True),
    "local_arrow": ("", "let g = (x: Int) => helper(x)\nlet v = g(n)", True),
    "loop_local": ("", "for (x of [1]) { let v = helper(x) }", True),
}


@pytest.fixture(scope="module")
def harness():
    return load_by_path("formal_diff_corpus_g1_1807",
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
    root = tmp_path_factory.mktemp("g1_shapes")
    corpus = root / "corpus"
    corpus.mkdir()
    for name, (act, body, _ok) in SHAPES.items():
        (corpus / f"{name}.rvl").write_text(_component(act, body),
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


# ------------------------------------------------------------- the premise

def test_the_checker_refuses_every_fixture_under_g1(harness):
    for rel in G1_FIXTURES:
        assert harness.checker_code(rel)[0] == "G1", rel


@pytest.mark.parametrize("name", sorted(SHAPES))
def test_the_checker_verdict_on_each_shape(name):
    act, body, ok = SHAPES[name]
    assert _checker(_component(act, body), f"{name}.rvl") == (
        "accept" if ok else "G1")


# ------------------------------------------------------------- the facts

def test_the_access_roots_keep_the_requirements_and_drop_the_rest(rows):
    """`Logger` calls `db`, which it does not declare; `notes.rvl`'s
    `NotesConsole` keeps its declared roots and drops everything else."""
    assert [r for r in rows if r.startswith(f"GA\t{LOGGER}\t")] == [
        f"GA\t{LOGGER}\tLogger\tdb"]
    console = {r.split("\t")[3] for r in rows
               if r.startswith("GA\texamples/app/notes.rvl\tNotesConsole\t")}
    declared = {r.split("\t")[3] for r in rows
                if r.startswith("R\texamples/app/notes.rvl\tNotesConsole\t")}
    assert console and console <= declared


def test_an_intercept_target_is_an_access_root(rows):
    assert f"GA\t{INTERCEPT}\tWatcher\tdb" in rows


# ------------------------------------------------- both sides, every shape

def test_the_reference_agrees_with_the_checker_on_every_shape(shapes):
    _out, ref, _formal, _root = shapes
    for name, (_act, _body, ok) in SHAPES.items():
        mine = [x for k, x in ref.access.items()
                if k[0] == f"corpus/{name}.rvl"]
        assert mine, name
        assert all(x == "ok" for x in mine) is ok, (name, mine)


def test_the_lean_oracle_agrees_on_every_shape(shapes):
    _out, ref, formal, _root = shapes
    if formal is None:
        pytest.skip("lake is not on PATH; the formal gate runs this half")
    assert formal.access == ref.access


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
    for name, (_act, _body, ok) in SHAPES.items():
        bucket = "agree-accept" if ok else "agree-G1"
        assert f"corpus/{name}.rvl" in samples.get(bucket, []), (name, bucket)


# -------------------------------------------------- the alignment arm

def test_every_fixture_files_under_agree_g1(harness, verdicts, rows, tmp_path):
    (tmp_path / "harness" / "out").mkdir(parents=True)
    with pytest.MonkeyPatch.context() as mp, redirect_stdout(io.StringIO()):
        mp.setattr(harness, "FORMAL", tmp_path)
        fatal = harness.checker_alignment({rel: {} for rel in G1_FIXTURES}, [],
                                          verdicts, rows)
        samples = dict(harness._ALIGN_SAMPLES)
    assert fatal == []
    assert set(samples.get("agree-G1", [])) == set(G1_FIXTURES)


def test_a_blind_row_is_filed_under_missed_g1(harness, verdicts, rows, tmp_path):
    blind = verdicts._replace(access={k: "ok" for k in verdicts.access})
    (tmp_path / "harness" / "out").mkdir(parents=True)
    with pytest.MonkeyPatch.context() as mp, redirect_stdout(io.StringIO()):
        mp.setattr(harness, "FORMAL", tmp_path)
        fatal = harness.checker_alignment({LOGGER: {}}, [], blind, rows)
    assert fatal == [f"missed-G1: {LOGGER}"]
    assert "missed-G1" in harness.FATAL_BUCKETS


# ------------------------------------------------ the coverage ratchet

def test_the_coverage_ratchet_is_satisfied(harness, rows):
    with redirect_stdout(io.StringIO()):
        harness.export()
    harness.reference_from_tsv(rows)
    assert harness.access_coverage() == []


def test_the_ratchet_bites_without_a_refused_component(harness, rows):
    with redirect_stdout(io.StringIO()):
        harness.export()
    refused = {r.split("\t")[1] for r in rows if r.startswith("GA\t")
               and r.split("\t")[1] in G1_FIXTURES}
    without = [r for r in rows if not (r.startswith("GA\t")
                                       and r.split("\t")[1] in refused)]
    harness.reference_from_tsv(without)
    findings = harness.access_coverage()
    harness.reference_from_tsv(rows)
    assert len(findings) == 1 and "undeclared access root" in findings[0]
