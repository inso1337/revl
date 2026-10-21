"""The formal job's proof-status summary and its accept-and-refuse rule
(issue #2745).

`formal/harness/diff_corpus.py` counts, per oracle row kind, the corpus
documents accepted and refused, and fails the gate when a kind lacks either
(G6 already did for its own row). `formal/scripts/proof_summary.py` renders the
census and the `#print axioms` blocks as markdown, and `run_gate.sh` appends it
to `$GITHUB_STEP_SUMMARY` on every exit.

Nothing here runs `lake`: the rule is tested over synthetic verdicts, the
summary over fixture log strings, and the shell wiring over a stub `lake`.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))
from _load_by_path import load_by_path  # noqa: E402

GATE = ROOT / "formal" / "scripts" / "run_gate.sh"


@pytest.fixture(scope="module")
def dc():
    return load_by_path("formal_diff_corpus_2745",
                        ROOT / "formal" / "harness" / "diff_corpus.py")


@pytest.fixture(scope="module")
def summary():
    return load_by_path("formal_proof_summary_2745",
                        ROOT / "formal" / "scripts" / "proof_summary.py")


def _ref(dc, **rows):
    empty = {f: {} for f in dc.Verdicts._fields}
    return dc.Verdicts(**{**empty, **rows})


def _full(dc, ref_rows):
    """A census in which every graded kind has rows from `ref_rows`, and the
    kinds not named carry one accepted and one refused document."""
    base = {f: {f"{f}/ok.rvl": "ok", f"{f}/bad.rvl": "fail"}
            for f, _ in dc.ROW_KINDS}
    base["files"] = {"files/ok.rvl": ("ok", "ok", "ok"),
                     "files/bad.rvl": ("ok", "fail", "ok")}
    for f, (_side, _why) in dc.ONE_SIDED.items():
        base[f] = {f"{f}/doc.rvl": "fail" if _side == "accepted" else "ok"}
    base.update(ref_rows)
    return dc.row_census(_ref(dc, **base))


def test_census_counts_documents_not_rows(dc):
    ref = _ref(dc,
               comps={("a.rvl", "C1"): "ok", ("a.rvl", "C2"): "fail",
                      ("b.rvl", "C1"): "ok", ("c.rvl", "C1"): "fail"},
               files={"a.rvl": ("ok", "ok", "ok"),
                      "b.rvl": ("fail", "ok", "ok")},
               g9={"r.rvl": "fail"})
    c = dc.row_census(ref)
    assert c["comps"]["rows"] == 4
    # a.rvl has a refused component, so it is refused, not accepted.
    assert (c["comps"]["accepted"], c["comps"]["refused"]) == (1, 2)
    assert (c["files"]["accepted"], c["files"]["refused"]) == (1, 1)
    assert (c["g9"]["accepted"], c["g9"]["refused"]) == (0, 1)


def test_a_kind_with_no_refused_document_fails(dc):
    census = _full(dc, {"comps": {("a.rvl", "C"): "ok"}})
    findings = dc.polarity_coverage(census)
    assert len(findings) == 1
    assert "G marker" in findings[0] and "no refused corpus document" in findings[0]


def test_a_kind_with_no_accepted_document_fails(dc):
    census = _full(dc, {"access": {("a.rvl", "C"): "fail"}})
    findings = dc.polarity_coverage(census)
    assert len(findings) == 1
    assert "G1 declared access" in findings[0]
    assert "no accepted corpus document" in findings[0]


def test_a_kind_with_no_rows_fails_on_both_sides(dc):
    census = _full(dc, {"councils": {}})
    findings = dc.polarity_coverage(census)
    assert len(findings) == 2
    assert all("CTV council split" in f for f in findings)


def test_a_balanced_census_is_clean(dc):
    assert dc.polarity_coverage(_full(dc, {})) == []


def test_an_exemption_is_stale_once_the_missing_side_appears(dc):
    census = _full(dc, {"g9": {"x.rvl": "fail", "y.rvl": "ok"}})
    findings = dc.polarity_coverage(census)
    assert len(findings) == 1
    assert "delete the exemption" in findings[0]


def test_an_exemption_covers_only_the_side_it_names(dc):
    # g9 is exempt for "accepted"; losing its refused side is still a finding.
    census = _full(dc, {"g9": {"y.rvl": "ok"}})
    findings = dc.polarity_coverage(census)
    assert any("no refused corpus document" in f for f in findings)


def test_the_exemptions_name_graded_kinds(dc):
    graded = {f for f, _ in dc.ROW_KINDS}
    assert set(dc.ONE_SIDED) <= graded
    assert {s for s, _ in dc.ONE_SIDED.values()} <= {"accepted", "refused"}


@pytest.fixture(scope="module")
def real_census(dc):
    import warnings
    with warnings.catch_warnings(), redirect_stdout(StringIO()):
        warnings.simplefilter("ignore")
        tsv, _facts, _census = dc.export()
        return dc.row_census(dc.reference_from_tsv(tsv))


def test_the_real_corpus_satisfies_the_rule(dc, real_census):
    """The rule is not red on the corpus the gate runs over, and the graded
    kinds are the ones the oracle compares."""
    assert dc.polarity_coverage(real_census) == []
    assert all(c["rows"] > 0 for c in real_census.values())
    assert set(real_census) | {"refused", "dispositions", "recoveries",
                               "g8surface", "g5reg"} == set(dc.Verdicts._fields)


def test_census_file_round_trips_into_the_summary(dc, summary, real_census,
                                                  tmp_path):
    out = tmp_path / "harness" / "out"
    out.mkdir(parents=True)
    dc.write_row_census(real_census, out / "row_census.json")
    text = summary.render(tmp_path, 0)
    assert "C confinement (G6)" in text
    for field in ("confinements", "g9", "gc"):
        c = real_census[field]
        ex = dc.ONE_SIDED.get(field)
        note = f"no {ex[0]} document: {ex[1]}" if ex else ""
        assert (f"| {c['label']} | {c['rows']} | {c['accepted']} | "
                f"{c['refused']} | {note} |") in text
    assert real_census["g9"]["accepted"] == 0


AXIOMS = """\
'RevL.G1.declared_only_access' does not depend on any axioms
'RevL.G6.confinement' depends on axioms: [propext]
'RevL.G6.g6_row_not_vacuous' depends on axioms: [propext, Classical.choice, Quot.sound]
'RevL.Semantics.phase_lengths_add' depends on axioms: [sorryAx]
'RevL.G6.leaky' depends on axioms: [myAxiom]
"""
ORACLE = """\
'RevLOracle.confinedB_iff' depends on axioms: [propext]
'RevLOracle.linkOKB_iff' does not depend on any axioms
"""


def test_theorem_groups_count_per_guarantee(summary):
    g = summary.theorem_groups(AXIOMS)
    assert g == {"G1": (1, 0), "G6": (3, 1), "Semantics": (1, 1)}
    assert summary.theorem_groups(ORACLE) == {"Oracle": (2, 0)}


def test_render_reports_theorems_and_marks_missing_inputs(summary, tmp_path):
    (tmp_path / ".axioms.out").write_text(AXIOMS)
    (tmp_path / ".oracle-axioms.out").write_text(ORACLE)
    text = summary.render(tmp_path, 1)
    assert "Gate: FAILED (exit 1)" in text
    assert "Theorems: 7 printed, 2 not axiom-clean." in text
    assert "| G6 | 3 | 1 |" in text and "| Oracle | 2 | 0 |" in text
    assert "Oracle rows: not produced" in text


@pytest.mark.parametrize("content", ["", "\n", "lake: error: boom\n"])
def test_an_axioms_file_with_no_theorem_is_not_produced(summary, tmp_path,
                                                        content):
    (tmp_path / ".axioms.out").write_text(content)
    text = summary.render(tmp_path, 1)
    assert "Theorems: not produced" in text
    assert "0 printed" not in text


def test_render_never_shows_a_skipped_run_as_zero(summary, tmp_path):
    text = summary.render(tmp_path, 77)
    assert "SKIPPED" in text and "NOT checked" in text
    assert "Theorems: not produced" in text
    assert "Oracle rows: not produced" in text
    assert "| 0 |" not in text


def _run_gate(tmp_path, lake_exit, with_variable=True):
    """Run the real script with a stub `lake` (or none) on a bare PATH. The
    `python3` on that PATH is a shim that writes `sentinel` whenever the
    summary script is invoked, so a call is observable either way."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    if lake_exit is not None:
        lake = bindir / "lake"
        lake.write_text(f"#!/bin/sh\nexit {lake_exit}\n")
        lake.chmod(0o755)
    sentinel = tmp_path / "sentinel"
    py = bindir / "python3"
    py.write_text(f'#!/bin/sh\ncase "$*" in *proof_summary*) '
                  f'echo called >> "{sentinel}";; esac\n'
                  f'exec "{sys.executable}" "$@"\n')
    py.chmod(0o755)
    summ = tmp_path / "summary.md"
    env = {"PATH": f"{bindir}:/usr/bin:/bin", "HOME": str(tmp_path),
           "PYTHONDONTWRITEBYTECODE": "1"}
    if with_variable:
        env["GITHUB_STEP_SUMMARY"] = str(summ)
    r = subprocess.run(["sh", str(GATE)], env=env, capture_output=True,
                       text=True, cwd=tmp_path, check=False)
    return r, summ, sentinel


@pytest.mark.skipif(shutil.which("sh") is None, reason="needs sh")
def test_gate_writes_the_summary_when_lake_fails(tmp_path):
    r, summ, sentinel = _run_gate(tmp_path, lake_exit=1)
    assert sentinel.exists()
    assert r.returncode == 1, r.stdout + r.stderr
    text = summ.read_text(encoding="utf-8")
    assert "Gate: FAILED (exit 1)" in text


@pytest.mark.skipif(shutil.which("sh") is None, reason="needs sh")
def test_gate_writes_the_summary_on_a_loud_skip_and_keeps_exit_77(tmp_path):
    r, summ, sentinel = _run_gate(tmp_path, lake_exit=None)
    assert sentinel.exists()
    assert r.returncode == 77, r.stdout + r.stderr
    assert "SKIPPED" in summ.read_text(encoding="utf-8")


@pytest.mark.skipif(shutil.which("sh") is None, reason="needs sh")
def test_gate_without_the_variable_never_calls_the_summary(tmp_path):
    r, summ, sentinel = _run_gate(tmp_path, lake_exit=None,
                                  with_variable=False)
    assert r.returncode == 77, r.stdout + r.stderr
    assert not sentinel.exists()
    assert not summ.exists()
