"""G-COUNCIL-SPLIT in the formal model (issue #1811, group 1).

The checker refuses, with code G-COUNCIL-SPLIT, a model council's aggregation
written to admit when its members disagree (`on_tie allow` and the other
spellings of the same discipline), and an `on_tie` outside the vocabulary.

The model states the rule as `RevL.ModelCouncil.SplitOK` over the councils a
file DECLARES, each carrying the tie outcome the checker reads for it.
`formal/harness/diff_corpus.py` exports `CV` rows; the oracle prints `CTV`
verdicts, the reference recomputes them, and the tie refusal files under
`agree-G-COUNCIL-SPLIT`, or the fatal `missed-G-COUNCIL-SPLIT`.

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

#: The corpus's G-COUNCIL-SPLIT refusal the row decides.
FIXTURES = ("examples/rejections/gcouncilsplit_on_tie_allow.rvl",)

#: The corpus's admitted councils: the twins of that file, both spelling
#: `on_tie split`, which is also the default.
TWINS = ("examples/model_council.rvl", "examples/model_council_binding.rvl")

#: The council shapes, with the checker's verdict (True admitted). The tie
#: clause is what varies; everything else is held fixed. `on_tie_omitted` is
#: the default path (an `aggregate` clause with no `on_tie`), and
#: `on_tie_unknown` is the OTHER rule of the same code — the row under test
#: is deliberately silent on it, so it is not asserted to file under
#: `agree-G-COUNCIL-SPLIT`.
SHAPES = {
    "on_tie_allow": ("quorum declared on_tie allow", False),
    "on_tie_admit": ("quorum declared on_tie admit", False),
    "on_tie_proceed": ("quorum declared on_tie proceed", False),
    "on_tie_accept": ("quorum declared on_tie accept", False),
    "on_tie_first": ("quorum declared on_tie first", False),
    "on_tie_any": ("quorum declared on_tie any", False),
    "on_tie_split": ("quorum declared on_tie split", True),
    "on_tie_deny": ("quorum declared on_tie deny", True),
    "on_tie_omitted": ("quorum declared", True),
    "on_tie_unknown": ("quorum declared on_tie agreement", False),
}

#: The shapes the `CTV` row decides, and the bucket each must file under.
DECIDED = {
    "on_tie_allow": "agree-G-COUNCIL-SPLIT",
    "on_tie_admit": "agree-G-COUNCIL-SPLIT",
    "on_tie_proceed": "agree-G-COUNCIL-SPLIT",
    "on_tie_accept": "agree-G-COUNCIL-SPLIT",
    "on_tie_first": "agree-G-COUNCIL-SPLIT",
    "on_tie_any": "agree-G-COUNCIL-SPLIT",
    "on_tie_split": "agree-accept",
    "on_tie_deny": "agree-accept",
    "on_tie_omitted": "agree-accept",
}

_COUNCIL = """\
model role edge on_device
model role vast off_device

service Answer {{ fn classify(text: Str) -> Str }}

model council Release {{
  proposer -> vast,
  adversary -> edge,
  aggregate unanimous {tie}
}}

component Classifier provides out: Answer {{
  provide out {{ fn classify(text) = text }}
}}
"""


def _source(name: str) -> str:
    return _COUNCIL.format(tie=SHAPES[name][0])


@pytest.fixture(scope="module")
def harness():
    return load_by_path("formal_diff_corpus_cs_1811",
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
    root = tmp_path_factory.mktemp("cs_shapes")
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

def test_the_checker_refuses_every_fixture_and_admits_every_twin(harness):
    for rel in FIXTURES:
        assert harness.checker_code(rel)[0] == "G-COUNCIL-SPLIT", rel
    for rel in TWINS:
        assert harness.checker_code(rel) == ("accept", ""), rel


@pytest.mark.parametrize("name", sorted(SHAPES))
def test_the_checker_verdict_on_each_tie_shape(name):
    from revl.compiler import compile_source
    from revl.errors import RevlError

    try:
        compile_source(_source(name), f"{name}.rvl")
        admitted = True
    except RevlError as e:
        assert "model council" in e.message, e.message
        admitted = False
    assert admitted is SHAPES[name][-1]


def test_only_the_admitting_ties_are_refused_by_the_tie_rule():
    """The row's vocabulary is the checker's, and the two rules of the code
    stay apart: an admitting tie is refused BY NAME, an unknown tie by the
    vocabulary check."""
    from revl.compiler import compile_source
    from revl.errors import RevlError

    from revl import model_council

    for tie in model_council.ADMITTING_TIE_OUTCOMES:
        with pytest.raises(RevlError) as caught:
            compile_source(_COUNCIL.format(tie=f"quorum declared on_tie {tie}"),
                           "tie.rvl")
        assert "admits when the members disagree" in caught.value.message
    for tie in model_council.TIE_OUTCOMES:
        compile_source(_COUNCIL.format(tie=f"quorum declared on_tie {tie}"),
                       "tie.rvl")


# ------------------------------------------------------------- the facts

def test_the_omitted_clause_exports_the_default(rows):
    rel = "examples/model_council.rvl"
    assert f"CV\t{rel}\tRelease\tsplit" in rows


def test_the_refused_fixture_still_exports_its_tie(rows):
    """The row is exported from the RAW declaration, not the validated table:
    the table is empty for a refused file, and a row that vanished exactly
    when the checker refused would be a row that cannot disagree."""
    rel = FIXTURES[0]
    assert f"CV\t{rel}\tRelease\tallow" in rows


# -------------------------------------------- both sides, every shape

def test_the_shapes_file_under_the_right_bucket(harness, shapes, tmp_path):
    out, ref, _formal, root = shapes
    buckets, fatal = _align(harness, [f"corpus/{n}.rvl" for n in SHAPES], ref,
                            out, tmp_path, repo=root)
    assert fatal == []
    for name, want in DECIDED.items():
        assert buckets[f"corpus/{name}.rvl"] == want, name


def test_the_lean_oracle_agrees_on_every_shape(shapes):
    _out, ref, formal, _root = shapes
    if formal is None:
        pytest.skip("lake is not on PATH; the formal gate runs this half")
    assert formal.councils == ref.councils
    assert formal.councils, "no CTV row: the shape corpus is vacuous"


# -------------------------------------------------- the alignment arm

def test_the_fixtures_and_twins_file_under_agree(harness, verdicts, rows, tmp_path):
    buckets, fatal = _align(harness, (*FIXTURES, *TWINS), verdicts, rows, tmp_path)
    assert fatal == []
    assert {buckets[r] for r in FIXTURES} == {"agree-G-COUNCIL-SPLIT"}
    assert {buckets[r] for r in TWINS} == {"agree-accept"}


def test_blind_rows_are_filed_under_missed(harness, verdicts, rows, tmp_path):
    blind = verdicts._replace(
        councils={k: "ok" for k in verdicts.councils})
    _buckets, fatal = _align(harness, FIXTURES, blind, rows, tmp_path)
    assert sorted(fatal) == sorted(
        f"missed-G-COUNCIL-SPLIT: {r}" for r in FIXTURES)
    assert "missed-G-COUNCIL-SPLIT" in harness.FATAL_BUCKETS


# ------------------------------------------------ the coverage ratchet

def test_the_coverage_ratchet_is_satisfied(harness, rows):
    harness.reference_from_tsv(rows)
    assert harness.council_coverage() == []


def test_the_ratchet_bites_without_a_refused_tie(harness, rows, verdicts):
    refused = {k for k, x in verdicts.councils.items() if x == "fail"}
    without = [r for r in rows if not (r.startswith("CV\t")
                                       and tuple(r.split("\t")[1:3]) in refused)]
    harness.reference_from_tsv(without)
    findings = harness.council_coverage()
    harness.reference_from_tsv(rows)
    assert len(findings) == 1 and "refused tie policy" in findings[0]
