"""Three declaration rules in the formal model (issue #1809).

* Prelude ordering: `isolate`, `intercept`, `handoff` and the routes precede
  every action in the activation body (an uncoded checker refusal).
* Intercept target: an `intercept` names a required key, not a provision
  (uncoded).
* Method in service: every operation a component names is declared by its
  service (A6, the call-site half).

The model states them as `RevL.Prelude.PreludeOK`, `InterceptOK` and
`MethodOK`; `formal/harness/diff_corpus.py` exports `PS`, `IT` and `MC` rows,
the oracle prints `PL`, `IC` and `MS` verdicts, the reference recomputes
them, and each refusal files under its `agree-*` bucket or a fatal
`missed-*` one.

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

FIXTURES = {
    "examples/rejections/v2_isolate_after_effect.rvl": "agree-prelude",
    "examples/rejections/v2_intercept_on_provision.rvl": "agree-intercept",
    "examples/rejections/a6_method_not_in_service.rvl": "agree-A6",
}

_HEAD = """\
service Kv { fn get(k: Str) -> Opt[Str]  fn set(k: Str, v: Str) }
service K { fn f(n: Int) -> Int }
"""


def _program(body: str, provide: str = "provide k { fn f(n) = n }",
             extra: str = "") -> str:
    return _HEAD + extra + f"""component C requires kv: Kv provides k: K {{
{body}
  {provide}
}}
"""


#: Each shape with the checker's verdict: "accept", or the bucket a refusal
#: files under.
SHAPES = {
    "isolate_first": ('  isolate kv in realm("a")\n'
                      '  effect kv.set("b", "1") undo kv.set("b", "")', "accept"),
    "isolate_after_effect": ('  effect kv.set("b", "1") undo kv.set("b", "")\n'
                             '  isolate kv in realm("a")', "agree-prelude"),
    "intercept_after_effect": ('  effect kv.set("b", "1") undo kv.set("b", "")\n'
                               "  intercept kv with { quota: 5 }", "agree-prelude"),
    "intercept_requirement": ("  intercept kv with { quota: 5 }", "accept"),
    "declared_operation": ('  let v = effect kv.get("x") undo kv.set("x", "")',
                           "accept"),
    "undeclared_operation": ('  let v = effect kv.drop("x") undo kv.set("x", "")',
                             "agree-A6"),
    "undeclared_implementation": (
        "", "provide k { fn f(n) = n\n    fn g(n) = n }", "agree-A6"),
}


def _source(name: str) -> str:
    body, *rest = SHAPES[name][:-1]
    return _program(body, *rest) if rest else _program(body)


@pytest.fixture(scope="module")
def harness():
    return load_by_path("formal_diff_corpus_decl_1809",
                        ROOT / "formal" / "harness" / "diff_corpus.py")


@pytest.fixture(scope="module")
def rows(harness):
    with redirect_stdout(io.StringIO()):
        out, _facts, _census = harness.export()
    return out


@pytest.fixture(scope="module")
def verdicts(harness, rows):
    return harness.reference_from_tsv(rows)


@pytest.fixture(scope="module")
def shapes(harness, tmp_path_factory):
    root = tmp_path_factory.mktemp("decl_shapes")
    corpus = root / "corpus"
    corpus.mkdir()
    for name in SHAPES:
        (corpus / f"{name}.rvl").write_text(_source(name), encoding="utf-8")
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


def _align(harness, rels, verdicts, rows, tmp_path, repo=None):
    (tmp_path / "harness" / "out").mkdir(parents=True, exist_ok=True)
    with pytest.MonkeyPatch.context() as mp, redirect_stdout(io.StringIO()):
        mp.setattr(harness, "FORMAL", tmp_path)
        if repo is not None:
            mp.setattr(harness, "REPO", repo)
        fatal = harness.checker_alignment({rel: {} for rel in rels}, [],
                                          verdicts, rows)
        samples = dict(harness._ALIGN_SAMPLES)
    return {rel: k for k, v in samples.items() for rel in v}, fatal


# ------------------------------------------------------------- the premise

def test_the_checker_refuses_each_fixture_with_the_message_the_arm_reads(harness):
    msgs = {rel: harness.checker_message(rel) for rel in FIXTURES}
    assert harness.PRELUDE_MESSAGE in msgs["examples/rejections/v2_isolate_after_effect.rvl"]
    assert harness.INTERCEPT_MESSAGE in msgs["examples/rejections/v2_intercept_on_provision.rvl"]
    assert harness.METHOD_MESSAGE in msgs["examples/rejections/a6_method_not_in_service.rvl"]


@pytest.mark.parametrize("name", sorted(SHAPES))
def test_the_checker_verdict_on_each_shape(name):
    from revl.compiler import compile_source
    from revl.errors import RevlError

    want = SHAPES[name][-1]
    try:
        compile_source(_source(name), f"{name}.rvl")
        got = "accept"
    except RevlError as e:
        got = e.message
    if want == "accept":
        assert got == "accept"
    else:
        assert got != "accept"


# ------------------------------------------------------------- the facts

def test_the_facts_of_the_three_fixtures(rows):
    iso = "examples/rejections/v2_isolate_after_effect.rvl"
    assert [r.split("\t")[3:] for r in rows if r.startswith(f"PS\t{iso}\t")] == [
        ["0", "action"], ["1", "prelude"]]
    icp = "examples/rejections/v2_intercept_on_provision.rvl"
    assert f"IT\t{icp}\tStore\tkv" in rows
    a6 = "examples/rejections/a6_method_not_in_service.rvl"
    assert f"MC\t{a6}\tMigrator\tDatabase\texecute" in rows


# ------------------------------------------------- both sides, every shape

def test_the_shapes_file_under_agree(harness, shapes, tmp_path):
    out, ref, _formal, root = shapes
    buckets, fatal = _align(harness, [f"corpus/{n}.rvl" for n in SHAPES], ref,
                            out, tmp_path, repo=root)
    assert fatal == []
    for name, spec in SHAPES.items():
        want = "agree-accept" if spec[-1] == "accept" else spec[-1]
        assert buckets[f"corpus/{name}.rvl"] == want, name


def test_the_lean_oracle_agrees_on_every_shape(shapes):
    _out, ref, formal, _root = shapes
    if formal is None:
        pytest.skip("lake is not on PATH; the formal gate runs this half")
    assert formal.preludes == ref.preludes
    assert formal.intercepts == ref.intercepts
    assert formal.methods == ref.methods


# -------------------------------------------------- the alignment arm

def test_each_fixture_files_under_its_agree_bucket(harness, verdicts, rows, tmp_path):
    buckets, fatal = _align(harness, list(FIXTURES), verdicts, rows, tmp_path)
    assert fatal == []
    assert buckets == FIXTURES


def test_blind_rows_are_filed_under_their_missed_buckets(harness, verdicts, rows,
                                                         tmp_path):
    blind = verdicts._replace(
        preludes={k: "ok" for k in verdicts.preludes},
        intercepts={k: "ok" for k in verdicts.intercepts},
        methods={k: "ok" for k in verdicts.methods})
    _buckets, fatal = _align(harness, list(FIXTURES), blind, rows, tmp_path)
    assert sorted(fatal) == sorted(
        f"{b.replace('agree-', 'missed-')}: {rel}" for rel, b in FIXTURES.items())
    for b in FIXTURES.values():
        assert b.replace("agree-", "missed-") in harness.FATAL_BUCKETS


# ------------------------------------------------ the coverage ratchet

def test_the_coverage_ratchet_is_satisfied(harness, rows):
    ref = harness.reference_from_tsv(rows)
    assert harness.prelude_coverage(ref) == []


def test_the_ratchet_bites_without_a_refused_prelude(harness, rows):
    iso = "examples/rejections/v2_isolate_after_effect.rvl"
    without = [r for r in rows if not r.startswith(f"PS\t{iso}\t")]
    ref = harness.reference_from_tsv(without)
    findings = harness.prelude_coverage(ref)
    harness.reference_from_tsv(rows)
    assert len(findings) == 1 and "prelude after an action" in findings[0]
