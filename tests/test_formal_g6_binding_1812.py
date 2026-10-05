"""G6 binding uniqueness in the formal model (issue #1812).

The checker refuses a binding whose name is already in view, with code G6,
category `binding`: `Env.bind_local` in the activation body (against the
`requires` locals and earlier activation bindings) and `_check_rebind` in a
provide method (against its parameters, its earlier locals and the activation
bindings). Visibility is block-scoped (`_lower_scoped_block`).

The model states the rule as `RevL.G6Binding.ScopeOK` over a scope's steps,
and `formal/harness/diff_corpus.py` exports them as `BE` rows: the names in
view at the start (`seed`), then `bind`, `enter` and `leave` in source order.
The oracle prints one `BU` verdict per scope (`bindingRowB`, bridged by
`bindingRowB_iff`), the reference recomputes it, and a G6 `binding` refusal
files under `agree-G6`, or under the fatal `missed-G6` when no scope fails.

This module runs in the plain `pytest tests/` job. The Lean half is checked
here when `lake` is on PATH, and by `make formal` always.
"""

from __future__ import annotations

import io
import json
import shutil
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))
from _load_by_path import load_by_path  # noqa: E402

FIXTURE = "examples/rejections/g6_method_local_shadows_component.rvl"

_HEAD = """\
service Net { fn ping() -> Int }
service K { fn f(n: Int) -> Int }
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


#: Each shape with the checker's verdict on it: True admitted, False refused
#: under G6 `binding`. The model has to agree on every one.
SHAPES = {
    "method_let_twice": ("", "let x = 1\nlet x = 2", False),
    "method_let_shadows_param": ("", "let n = 2", False),
    "method_let_shadows_require": ("", "let net = 2", True),
    "method_let_shadows_activation": (
        "  let s = effect Map.new() undo s.drop()", "let s = 2", False),
    "sibling_if_arms": (
        "", "if (n > 0) { let x = 1 } else { let x = 2 }", True),
    "arm_then_after": ("", "if (n > 0) { let x = 1 }\nlet x = 2", True),
    "twice_in_one_arm": ("", "if (n > 0) { let x = 1\nlet x = 2 }", False),
    "two_loops_one_binder": (
        "", "for (x of [1]) { let y = x }\nfor (x of [2]) { let z = x }", True),
    "loop_binder_then_let": ("", "for (x of [1]) { let y = x }\nlet x = 3", True),
    "let_then_loop_binder": ("", "let x = 3\nfor (x of [1]) { let y = x }", False),
    "loop_body_shadows_binder": ("", "for (x of [1]) { let x = 2 }", False),
    "while_body_then_after": (
        "", "var i = 0\nwhile (i < 1) { let x = 1\ni = i + 1 }\nlet x = 2", True),
    "var_twice": ("", "var x = 1\nvar x = 2", False),
    "activation_twice": (
        "  let s = effect Map.new() undo s.drop()\n"
        "  let s = effect Map.new() undo s.drop()", "let y = 1", False),
    "activation_shadows_require": (
        "  let net = effect Map.new() undo net.drop()", "let y = 1", False),
}


@pytest.fixture(scope="module")
def harness():
    return load_by_path("formal_diff_corpus_g6_1812",
                        ROOT / "formal" / "harness" / "diff_corpus.py")


@pytest.fixture(scope="module")
def rows(harness):
    with redirect_stdout(io.StringIO()):
        out, _facts, _census = harness.export()
    return out


@pytest.fixture(scope="module")
def verdicts(harness, rows):
    return harness.reference_from_tsv(rows)


def _checker(source: str, name: str) -> tuple[str, str]:
    from revl.compiler import compile_source
    from revl.diagnostics import classify
    from revl.errors import RevlError

    try:
        compile_source(source, name)
        return ("accept", "")
    except RevlError as e:
        info = classify(e)
        return (info.get("code") or "REVL", info.get("category") or "")


@pytest.fixture(scope="module")
def shapes(harness, tmp_path_factory):
    """Every shape exported as one corpus and decided on both sides."""
    root = tmp_path_factory.mktemp("g6_shapes")
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

def test_the_checker_refuses_the_fixture_under_g6_binding(harness):
    assert harness.checker_code(FIXTURE) == ("G6", "binding")


@pytest.mark.parametrize("name", sorted(SHAPES))
def test_the_checker_verdict_on_each_shape(name):
    act, body, ok = SHAPES[name]
    want = ("accept", "") if ok else ("G6", "binding")
    assert _checker(_component(act, body), f"{name}.rvl") == want


# ------------------------------------------------------------- the facts

def test_the_fixture_s_method_scope_is_seeded_with_the_activation_binding(rows):
    steps = [r.split("\t")[4:] for r in rows
             if r.startswith(f"BE\t{FIXTURE}\tC\tcache.set\t")]
    assert steps == [["0", "seed", "store"], ["1", "seed", "key"],
                     ["2", "bind", "store"]]


def test_the_activation_scope_is_seeded_with_the_requires_locals(harness):
    from revl.parser import Parser

    act, body, _ok = SHAPES["activation_shadows_require"]
    prog = Parser(_component(act, body), "x.rvl").parse()
    out = harness.binding_rows("x.rvl", prog.components[0])
    act_rows = [r.split("\t")[3:] for r in out if "\t@act\t" in r]
    assert act_rows == [["@act", "0", "seed", "net"], ["@act", "1", "bind", "net"]]


# ------------------------------------------------- both sides, every shape

def test_the_reference_agrees_with_the_checker_on_every_shape(shapes):
    _out, ref, _formal, _root = shapes
    for name, (_act, _body, ok) in SHAPES.items():
        mine = [x for k, x in ref.bindings.items()
                if k[0] == f"corpus/{name}.rvl"]
        assert mine, name
        assert (all(x == "ok" for x in mine)) is ok, (name, mine)


def test_the_lean_oracle_agrees_on_every_shape(shapes):
    _out, ref, formal, _root = shapes
    if formal is None:
        pytest.skip("lake is not on PATH; the formal gate runs this half")
    assert formal.bindings == ref.bindings


def test_the_shapes_file_under_agree(harness, shapes, tmp_path):
    """Every shape lands in an agreeing bucket: the checker's verdict and
    the row's, compiled from the synthetic corpus."""
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
        bucket = "agree-accept" if ok else "agree-G6"
        assert f"corpus/{name}.rvl" in samples.get(bucket, []), (name, bucket)


# -------------------------------------------------- the alignment arm

def test_the_fixture_files_under_agree_g6(harness, verdicts, rows, tmp_path):
    (tmp_path / "harness" / "out").mkdir(parents=True)
    with pytest.MonkeyPatch.context() as mp, redirect_stdout(io.StringIO()):
        mp.setattr(harness, "FORMAL", tmp_path)
        fatal = harness.checker_alignment({FIXTURE: {}}, [], verdicts, rows)
        samples = dict(harness._ALIGN_SAMPLES)
    assert fatal == []
    assert samples.get("agree-G6") == [FIXTURE]


def test_a_blind_row_is_filed_under_missed_g6(harness, verdicts, rows, tmp_path):
    """With every `BU` verdict `ok`, the binding refusal has nothing to agree
    with: the fatal bucket, not an out-of-fragment one."""
    blind = verdicts._replace(bindings={k: "ok" for k in verdicts.bindings})
    (tmp_path / "harness" / "out").mkdir(parents=True)
    with pytest.MonkeyPatch.context() as mp, redirect_stdout(io.StringIO()):
        mp.setattr(harness, "FORMAL", tmp_path)
        fatal = harness.checker_alignment({FIXTURE: {}}, [], blind, rows)
    assert fatal == [f"missed-G6: {FIXTURE}"]
    assert "missed-G6" in harness.FATAL_BUCKETS


def test_the_g6_ledger_list_is_empty(harness):
    ledger = json.loads(harness.OOF_LEDGER_PATH.read_text(encoding="utf-8"))
    assert ledger["out-of-fragment-G6"] == []
    assert "out-of-fragment-G6" in harness.OOF_RATCHET_BUCKETS


# ------------------------------------------------ the coverage ratchet

def test_the_coverage_ratchet_is_satisfied(harness, rows):
    harness.reference_from_tsv(rows)
    assert harness.binding_coverage() == []


def test_the_ratchet_bites_without_the_refused_scope(harness, rows):
    without = [r for r in rows if not r.startswith(f"BE\t{FIXTURE}\t")]
    harness.reference_from_tsv(without)
    findings = harness.binding_coverage()
    harness.reference_from_tsv(rows)
    assert len(findings) == 1 and "rebinding a name in view" in findings[0]


def test_the_ratchet_bites_without_a_block(harness, rows):
    flat = [r for r in rows
            if not (r.startswith("BE\t") and r.split("\t")[5] in ("enter", "leave"))]
    harness.reference_from_tsv(flat)
    findings = harness.binding_coverage()
    harness.reference_from_tsv(rows)
    assert len(findings) == 1 and "opens a block" in findings[0]
