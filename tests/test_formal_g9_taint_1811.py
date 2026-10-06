"""G9 / G-SECRET-FLOW in the formal model (issue #1811, group 2).

The checker refuses, with code `G9` (`taint-flow`) or `G-SECRET-FLOW`
(`taint-secret-flow`), a value whose label reaches a sink the rule forbids:
an authority sink (a shell command, a policy update, a UI actuation, a
capability name) demands the empty label, and a disclosure sink (an extern
host call, an emission crossing) demands the label carry neither `secret`
nor `confidential`.

`RevL.G9` states the rule over an abstract walk and `RevL.Lemmas.Admits`
states what each sink admits. This module pins the differential-oracle row
issue #1811 group 2 added for them: `RevL.G9Flow.g9RowB` decides `Admits`
at the sink class and on the label the checker itself REPORTS in its
refusal, and `formal/harness/diff_corpus.py` carries what it reports as one
`TAINT` row per refusal, filed under `agree-G9` or the fatal `missed-G9`.

That row is **the rule on the corpus, not coverage of the checker's walk**:
its premises ARE the checker's report, so it cannot witness that the walk is
complete. See `formal/RevL/Theorems/G9Flow.lean` and `formal/STATUS.md`.

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

#: The corpus's G9 / G-SECRET-FLOW refusals the `TAINT` row decides.
FIXTURES = (
    "examples/rejections/g9_closure_capture_launders_taint.rvl",
    "examples/rejections/g9_service_return_launders_taint.rvl",
    "examples/rejections/g9_spawn_config_launders_taint.rvl",
    "examples/rejections/gsecret_service_return_discloses.rvl",
)

#: What the checker's own refusal discovered, per file: the refusal code, the
#: sink CLASS the row states the rule at, the label that arrived there, and
#: the hop count of the naming chain it reported.
DISCOVERED = {
    FIXTURES[0]: ("G9", "authority", "fs", 2),
    FIXTURES[1]: ("G9", "authority", "fs", 1),
    FIXTURES[2]: ("G9", "authority", "fs", 2),
    FIXTURES[3]: ("G-SECRET-FLOW", "disclosure", "confidential", 1),
}


@pytest.fixture(scope="module")
def harness():
    return load_by_path("formal_diff_corpus_g9_1811",
                        ROOT / "formal" / "harness" / "diff_corpus.py")


@pytest.fixture(scope="module")
def rows(harness):
    with redirect_stdout(io.StringIO()):
        out, _facts, _census = harness.export()
    return out


@pytest.fixture(scope="module")
def verdicts(harness, rows):
    return harness.reference_from_tsv(rows)


def _align(harness, rels, verdicts, rows, tmp_path):
    (tmp_path / "harness" / "out").mkdir(parents=True, exist_ok=True)
    with pytest.MonkeyPatch.context() as mp, redirect_stdout(io.StringIO()):
        mp.setattr(harness, "FORMAL", tmp_path)
        fatal = harness.checker_alignment({rel: {} for rel in rels}, [],
                                          verdicts, rows)
        samples = dict(harness._ALIGN_SAMPLES)
    return {rel: k for k, v in samples.items() for rel in v}, fatal


# ------------------------------------------------------------- the premise

def test_the_checker_refuses_every_fixture_with_a_taint_code(harness):
    for rel in FIXTURES:
        assert harness.checker_code(rel)[0] == DISCOVERED[rel][0], rel


# --------------------------------------------------------------- the facts

def test_the_row_carries_what_the_checker_discovered(rows):
    for rel, (code, cls, label, hops) in DISCOVERED.items():
        taint = [r for r in rows if r.startswith(f"TAINT\t{rel}\t")]
        assert len(taint) == 1, rel
        f = taint[0].split("\t")
        assert (f[2], f[4], f[6], f[8]) == (code, cls, label, str(hops)), rel


# ---------------------------------------------- both sides, every fixture

def test_the_reference_decides_the_rule_violated_on_every_fixture(
        harness, rows, verdicts):
    harness.reference_from_tsv(rows)
    for rel in FIXTURES:
        assert verdicts.g9[rel] == "fail", rel


def test_the_fixtures_file_under_agree_G9(harness, verdicts, rows, tmp_path):
    buckets, fatal = _align(harness, FIXTURES, verdicts, rows, tmp_path)
    assert fatal == []
    assert {buckets[r] for r in FIXTURES} == {"agree-G9"}


def test_a_blind_row_is_filed_under_missed_G9(harness, verdicts, rows, tmp_path):
    blind = verdicts._replace(g9={k: "ok" for k in verdicts.g9})
    _buckets, fatal = _align(harness, FIXTURES, blind, rows, tmp_path)
    assert sorted(fatal) == sorted(f"missed-G9: {r}" for r in FIXTURES)
    assert "missed-G9" in harness.FATAL_BUCKETS


def test_the_lean_oracle_agrees_on_every_fixture(harness, rows,
                                                 tmp_path_factory):
    if shutil.which("lake") is None:
        pytest.skip("lake is not on PATH; the formal gate runs this half")
    root = tmp_path_factory.mktemp("g9_oracle")
    tsv = root / "corpus.tsv"
    tsv.write_text("\n".join(rows) + "\n", encoding="utf-8")
    formal = harness.parse_verdicts(
        harness.run_oracle(tsv, root / "formal_verdicts.tsv"))
    ref = harness.reference_from_tsv(rows)
    assert formal.g9 == ref.g9


# ------------------------------------------------ the coverage ratchet

def test_the_coverage_ratchet_is_satisfied(harness, rows):
    harness.reference_from_tsv(rows)
    assert harness.g9_coverage() == []


def test_the_ratchet_bites_without_any_taint_row(harness, rows):
    without = [r for r in rows if not r.startswith("TAINT\t")]
    harness.reference_from_tsv(without)
    findings = harness.g9_coverage()
    harness.reference_from_tsv(rows)
    assert len(findings) == 1 and "no TAINT rows" in findings[0]
