"""G-MODEL-PLACE in the formal model (issue #1811).

The checker refuses, with code G-MODEL-PLACE, a `route model` arm that places
a confidentiality origin on a model role declared `off_device` (directly or
through a council member that receives it), and a component that consults a
model role whose `reaches [...]` it does not hold (item 519).

The model states the placement rule as `RevL.ModelPlace.PlaceOK` and decides
the reach with the spawn rule's proved `attenuatesB`.
`formal/harness/diff_corpus.py` exports `MP`/`MO` (placed arms, origins) and
`ME`/`MRC` (consulted roles, their reach); the oracle prints `MPV` and `MAV`
verdicts, the reference recomputes them, and a refusal files under
`agree-G-MODEL-PLACE`, or the fatal `missed-G-MODEL-PLACE`.

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

_REACH = "tests/fixtures/model_reach_crossing"

#: The corpus's G-MODEL-PLACE refusals the rows decide.
FIXTURES = (
    "examples/rejections/gmodelplace_confidential_off_device.rvl",
    "examples/rejections/gmodelplace_council_member_off_device.rvl",
    *(f"{_REACH}/model_{n}.rvl" for n in (
        "extern_block_value", "extern_no_block", "helper_no_block",
        "req_block", "req_no_block", "undeclared_no_block",
        "unscoped_emission")),
)

#: Their admitted twins.
TWINS = tuple(f"{_REACH}/ok_{n}.rvl" for n in (
    "extern_block_value", "extern_no_block", "helper_no_block", "req_block",
    "req_no_block", "scoped_emission"))

_PLACE = """\
model role local on_device
model role cloud off_device
model role edge on_device
service Answer {{ fn classify(text: Str) -> Str }}
{council}
component Classifier provides out: Answer {{
  route model on classify {{
    {arm}
  }}
  provide out {{ fn classify(text) = text }}
}}
"""

_COUNCIL = """\
model council Panel {
  proposer  -> %s,
  adversary -> edge,
  aggregate unanimous quorum declared on_tie split
}
"""

#: Placement shapes with the checker's verdict (True admitted).
SHAPES = {
    "confidential_on_device": ("", "confidential -> local", True),
    "confidential_off_device": ("", "confidential -> cloud", False),
    "open_origin_off_device": ("", "* -> cloud", True),
    "council_member_off_device": (_COUNCIL % "cloud", "confidential -> Panel", False),
    "council_all_on_device": (_COUNCIL % "local", "confidential -> Panel", True),
}


def _source(name: str) -> str:
    council, arm, _ok = SHAPES[name]
    return _PLACE.format(council=council, arm=arm)


@pytest.fixture(scope="module")
def harness():
    return load_by_path("formal_diff_corpus_mp_1811",
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
    root = tmp_path_factory.mktemp("mp_shapes")
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
        assert harness.checker_code(rel)[0] == "G-MODEL-PLACE", rel
    for rel in TWINS:
        assert harness.checker_code(rel) == ("accept", ""), rel


@pytest.mark.parametrize("name", sorted(SHAPES))
def test_the_checker_verdict_on_each_placement_shape(name):
    from revl.compiler import compile_source
    from revl.errors import RevlError

    try:
        compile_source(_source(name), f"{name}.rvl")
        admitted = True
    except RevlError as e:
        assert "may not leave" in e.message, e.message
        admitted = False
    assert admitted is SHAPES[name][-1]


# ------------------------------------------------------------- the facts

def test_a_council_arm_places_each_receiving_member(rows):
    rel = "examples/rejections/gmodelplace_council_member_off_device.rvl"
    placed = sorted(r.split("\t")[5:] for r in rows if r.startswith(f"MP\t{rel}\t"))
    assert placed == [["edge", "on_device"], ["vast", "off_device"]]


def test_a_role_without_reaches_reaches_star(rows):
    rel = f"{_REACH}/model_undeclared_no_block.rvl"
    assert f"MRC\t{rel}\ttool\t*" in rows
    assert f"ME\t{rel}\tClassifier\ttool" in rows


# -------------------------------------------- both sides, every shape

def test_the_shapes_file_under_agree(harness, shapes, tmp_path):
    out, ref, _formal, root = shapes
    buckets, fatal = _align(harness, [f"corpus/{n}.rvl" for n in SHAPES], ref,
                            out, tmp_path, repo=root)
    assert fatal == []
    for name, (_c, _a, ok) in SHAPES.items():
        want = "agree-accept" if ok else "agree-G-MODEL-PLACE"
        assert buckets[f"corpus/{name}.rvl"] == want, name


def test_the_lean_oracle_agrees_on_every_shape(shapes):
    _out, ref, formal, _root = shapes
    if formal is None:
        pytest.skip("lake is not on PATH; the formal gate runs this half")
    assert formal.places == ref.places
    assert formal.model_reach == ref.model_reach


# -------------------------------------------------- the alignment arm

def test_the_fixtures_and_twins_file_under_agree(harness, verdicts, rows, tmp_path):
    buckets, fatal = _align(harness, (*FIXTURES, *TWINS), verdicts, rows, tmp_path)
    assert fatal == []
    assert {buckets[r] for r in FIXTURES} == {"agree-G-MODEL-PLACE"}
    assert {buckets[r] for r in TWINS} == {"agree-accept"}


def test_blind_rows_are_filed_under_missed(harness, verdicts, rows, tmp_path):
    blind = verdicts._replace(places={k: "ok" for k in verdicts.places},
                              model_reach={k: "ok" for k in verdicts.model_reach})
    _buckets, fatal = _align(harness, FIXTURES, blind, rows, tmp_path)
    assert sorted(fatal) == sorted(f"missed-G-MODEL-PLACE: {r}" for r in FIXTURES)
    assert "missed-G-MODEL-PLACE" in harness.FATAL_BUCKETS


# ------------------------------------------------ the coverage ratchet

def test_the_coverage_ratchet_is_satisfied(harness, rows):
    harness.reference_from_tsv(rows)
    assert harness.model_coverage() == []


def test_the_ratchet_bites_without_a_refused_reach(harness, rows, verdicts):
    refused = {k for k, x in verdicts.model_reach.items() if x == "fail"}
    without = [r for r in rows if not (r.startswith("ME\t")
                                       and tuple(r.split("\t")[1:4]) in refused)]
    harness.reference_from_tsv(without)
    findings = harness.model_coverage()
    harness.reference_from_tsv(rows)
    assert len(findings) == 1 and "refused model reach" in findings[0]
