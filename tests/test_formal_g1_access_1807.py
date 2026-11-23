"""G1 declared access in the formal model (issue #1807).

The checker refuses a call head whose root names no declared requirement,
with code G1 ("`db` is not a declared requirement of C"), wherever the head
sits: the activation body, a provide method, inside a block, in a condition,
and as an `intercept` target.

The model states the rule per scope (issue #2744): a scope is the activation
or one provide method, and `RevL.G1Access.classify` decides whether each root
it uses is `bound` (a binding of THAT scope), resolved by the file (a module
callable, an import, a host family, a constructor), `declared` or
`undeclared`. `formal/harness/diff_corpus.py` exports the facts, `GAS` (a
scope), `GAU` (a root the scope uses, with what the file alone resolves it to,
`key` for an `intercept` target that is not a provision) and `GAL` (a name the
scope binds). The oracle prints the class of every root (`G1R`), one `G1S`
verdict per scope (`scopeAccessRowB`) and one `G1` verdict per component
(`accessRowB`, bridged by `accessRowB_iff`), the reference recomputes all
three, and a G1 refusal files under `agree-G1`, or under the fatal
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

#: The scope fixtures of issue #2744: a name one provide method binds does not
#: resolve in another, and the twins that do not depend on it.
OTHER_METHOD = "tests/formal_corpus/g1_name_bound_in_other_method.rvl"
EACH_METHOD = "tests/formal_corpus/g1_name_bound_in_each_method.rvl"
REQUIREMENT_NAME = "tests/formal_corpus/g1_requirement_name_bound_in_one_method.rvl"

#: The corpus's G1 refusals, which sat in the generic `out-of-fragment`.
G1_FIXTURES = (
    LOGGER,
    "examples/rejections/g1_unknown_upper_host.rvl",
    "examples/ecosystem-consumer-js/candidates/undeclared_tool.rvl",
    "examples/ecosystem-consumer-rs/candidates/undeclared_tool.rvl",
    INTERCEPT,
    OTHER_METHOD,
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
    # an activation binding is in view in a provide method of its component
    "activation_local_in_method": (
        "  let m = effect Map.new() undo m.drop()", "let v = m.size()", True),
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


@pytest.mark.parametrize("rel,verdict", [
    (OTHER_METHOD, "G1"), (EACH_METHOD, "accept"), (REQUIREMENT_NAME, "accept")])
def test_the_checker_verdict_on_each_scope_fixture(harness, rel, verdict):
    assert harness.checker_code(rel)[0] == verdict


@pytest.mark.parametrize("name", sorted(SHAPES))
def test_the_checker_verdict_on_each_shape(name):
    act, body, ok = SHAPES[name]
    assert _checker(_component(act, body), f"{name}.rvl") == (
        "accept" if ok else "G1")


# ------------------------------------------------------------- the facts

def test_every_scope_of_a_component_has_a_header(rows):
    """One `GAS` per scope: the activation when it has a statement, and one
    per provide method, labelled as the coverage row labels them."""
    scopes = [r.split("\t")[3] for r in rows
              if r.startswith(f"GAS\t{OTHER_METHOD}\t")]
    assert scopes == ["pair.first", "pair.second"]
    logger = [r.split("\t")[3] for r in rows
              if r.startswith(f"GAS\t{LOGGER}\t")]
    assert logger == ["activation"]


def test_the_roots_are_classified_per_scope_by_the_model(verdicts, rows):
    """`Logger` reads `db`, which it does not declare; `notes.rvl`'s
    `NotesConsole` keeps its declared roots and every other root falls in
    another class."""
    assert verdicts.g1class[(LOGGER, "Logger", "activation", "db")] == "undeclared"
    assert f"GAU\t{LOGGER}\tLogger\tactivation\tdb\tnone" in rows
    classes = {k[3]: c for k, c in verdicts.g1class.items()
               if k[:2] == ("examples/app/notes.rvl", "NotesConsole")}
    declared = {r.split("\t")[3] for r in rows
                if r.startswith("R\texamples/app/notes.rvl\tNotesConsole\t")}
    assert classes and "undeclared" not in classes.values()
    assert {root for root, c in classes.items() if c == "declared"} <= declared


def test_an_intercept_target_is_a_key_use_of_the_activation(verdicts, rows):
    assert f"GAU\t{INTERCEPT}\tWatcher\tactivation\tdb\tkey" in rows
    assert verdicts.g1class[(INTERCEPT, "Watcher", "activation", "db")] == \
        "undeclared"


def test_a_name_one_method_binds_does_not_resolve_in_another(verdicts):
    """The second slice. `first` binds `tmp`, `second` reads it: the same root
    is `bound` in one scope and `undeclared` in the other."""
    assert verdicts.g1class[(OTHER_METHOD, "Holder", "pair.first", "tmp")] == "bound"
    assert verdicts.g1class[(OTHER_METHOD, "Holder", "pair.second", "tmp")] == \
        "undeclared"
    assert verdicts.g1scope[(OTHER_METHOD, "Holder", "pair.first")] == "ok"
    assert verdicts.g1scope[(OTHER_METHOD, "Holder", "pair.second")] == "fail"
    assert verdicts.access[(OTHER_METHOD, "Holder")] == "fail"


def test_the_twins_that_do_not_depend_on_it_are_admitted(verdicts):
    for rel in (EACH_METHOD, REQUIREMENT_NAME):
        assert verdicts.access[(rel, "Holder")] == "ok", rel
        assert {x for k, x in verdicts.g1scope.items() if k[0] == rel} == {"ok"}
    assert verdicts.g1class[(EACH_METHOD, "Holder", "pair.second", "tmp")] == "bound"
    # a requirement's name bound in one method is `bound` there and `declared`
    # in the other: the scopes' bindings do not leak into each other
    assert verdicts.g1class[(REQUIREMENT_NAME, "Holder", "pair.first", "net")] == \
        "bound"
    assert verdicts.g1class[(REQUIREMENT_NAME, "Holder", "pair.second", "net")] == \
        "declared"


def test_pooling_the_bindings_would_admit_the_refused_fixture(harness, rows):
    """What the per-scope reading changes. Re-decide the refused fixture with
    every scope's bindings pooled, the component-wide reading of the first
    slice, and every scope is admitted."""
    by_scope: dict = {}
    for r in rows:
        f = r.split("\t")
        if f[1] != OTHER_METHOD:
            continue
        if f[0] == "GAL":
            by_scope.setdefault(f[3], [set(), {}])[0].add(f[4])
        elif f[0] == "GAU":
            by_scope.setdefault(f[3], [set(), {}])[1][f[4]] = f[5]
    pooled = set().union(*(b for b, _u in by_scope.values()))
    assert pooled == {"tmp"}
    for scope, (_bound, uses) in by_scope.items():
        assert all(harness._g1_classify(set(), pooled, kind, root) != "undeclared"
                   for root, kind in uses.items()), scope
    refused = [scope for scope, (bound, uses) in by_scope.items()
               if any(harness._g1_classify(set(), bound, kind, root) == "undeclared"
                      for root, kind in uses.items())]
    assert refused == ["pair.second"]


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
    assert formal.g1scope == ref.g1scope
    assert formal.g1class == ref.g1class


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

def test_the_scope_fixtures_file_under_agree(harness, verdicts, rows, tmp_path):
    (tmp_path / "harness" / "out").mkdir(parents=True)
    rels = [OTHER_METHOD, EACH_METHOD, REQUIREMENT_NAME]
    with pytest.MonkeyPatch.context() as mp, redirect_stdout(io.StringIO()):
        mp.setattr(harness, "FORMAL", tmp_path)
        fatal = harness.checker_alignment({rel: {} for rel in rels}, [],
                                          verdicts, rows)
        samples = dict(harness._ALIGN_SAMPLES)
    assert fatal == []
    assert OTHER_METHOD in samples.get("agree-G1", [])
    for rel in (EACH_METHOD, REQUIREMENT_NAME):
        assert rel in samples.get("agree-accept", []), rel


def test_the_lean_oracle_agrees_on_every_scope_and_class(
        harness, rows, verdicts, tmp_path_factory):
    """The whole corpus, not only the shapes: the model's per-scope verdict and
    the class it gave every root equal the reference's."""
    if shutil.which("lake") is None:
        pytest.skip("lake is not on PATH; the formal gate runs this half")
    root = tmp_path_factory.mktemp("g1_scope_oracle")
    tsv = root / "corpus.tsv"
    tsv.write_text("\n".join(rows) + "\n", encoding="utf-8")
    formal = harness.parse_verdicts(
        harness.run_oracle(tsv, root / "formal_verdicts.tsv"))
    assert formal.access == verdicts.access
    assert formal.g1scope == verdicts.g1scope
    assert formal.g1class == verdicts.g1class
    assert formal.g1class, "the oracle classified no root at all"

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


def _without(rows, rels):
    drop = ("GAS", "GAU", "GAL")
    return [r for r in rows
            if not (r.split("\t")[0] in drop and r.split("\t")[1] in rels)]


def test_the_ratchet_bites_without_a_refused_component(harness, rows):
    with redirect_stdout(io.StringIO()):
        harness.export()
    verdicts = harness.reference_from_tsv(rows)
    refused = {k[0] for k, x in verdicts.access.items() if x == "fail"}
    harness.reference_from_tsv(_without(rows, refused))
    findings = harness.access_coverage()
    harness.reference_from_tsv(rows)
    assert any("a component refused for an undeclared access root" in f
               for f in findings), findings


def test_the_ratchet_bites_without_the_per_scope_witness(harness, rows):
    """The one refusal a component-wide reading would admit is what shows the
    per-scope classification changes a verdict. Remove that one file and the
    ratchet says so, and only that."""
    with redirect_stdout(io.StringIO()):
        harness.export()
    harness.reference_from_tsv(_without(rows, {OTHER_METHOD}))
    findings = harness.access_coverage()
    harness.reference_from_tsv(rows)
    assert len(findings) == 1 and "bound in another scope" in findings[0], findings
