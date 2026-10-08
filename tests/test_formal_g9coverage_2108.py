"""G9 path coverage in the formal model (issue #2108).

`formal/STATUS.md` carried G9 **path coverage** as its one `UNPROVED,
unstatable` row. The two bugs that motivated the obligation were in the
checker's *walk*, not the rule: `taint._walk_component_methods` skipped
activation bodies entirely, so a component activation body was never
taint-checked. L0 had no syntax for a component body, so the obligation could
not be stated at all.

`RevL.Syntax.Body` grows that syntax and `RevL.G9Coverage.coversBodyB` states
the obligation over it: the walk visits every scope a component's own body
induces, on the **label**, the **statement count** and the **origin-carrying
parameters** — one conjunct per historical bug. This module pins the
differential-oracle half, the `GB`/`GP`/`GW`/`GC` rows that issue #2108 added
to `formal/harness/diff_corpus.py`.

The row is **coverage of the checker's walk ON THE CORPUS**, which is the half
the `TAINT` row cannot state: its premises ARE the checker's report, so it can
never witness that the walk is complete. The rule half is
`RevL.G9Flow.g9RowB` (issue #1811 groups 2 & 3) and is untouched here.

TWO DISTINCT FATAL BUCKETS, and this module is mostly about keeping them
apart. `missed-G9-coverage` is a genuine coverage disagreement. The
`missed-G9-coverage-observation` bucket is this harness's OWN blind spot: the
parse side says a component has scopes and the recorder captured none, so the
walk was never observed rather than observed-short. Folding the second into
the first would let a renamed seam read as a coverage regression, or hide one
behind the other.

This module runs in the plain `pytest tests/` job. The Lean half is checked
here when `lake` is on PATH, and by `sh formal/scripts/run_gate.sh` always.
"""

from __future__ import annotations

import inspect
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

#: The two buckets, kept apart on purpose. The first is a finding about the
#: checker; the second is a finding about THIS harness.
COVERAGE_BUCKET = "missed-G9-coverage"
OBSERVATION_BUCKET = "missed-G9-coverage-observation"

#: The bucket the agreement used to be filed under before it was moved onto
#: its own axis. It must never come back: a file's chain bucket already says
#: what the checker and the model agree the file IS.
RETIRED_BUCKET = "agree-G9-coverage"


@pytest.fixture(scope="module")
def harness():
    return load_by_path("formal_diff_corpus_g9coverage_2108",
                        ROOT / "formal" / "harness" / "diff_corpus.py")


@pytest.fixture(scope="module")
def rows(harness):
    with redirect_stdout(io.StringIO()):
        out, _facts, _census = harness.export()
    return out


@pytest.fixture(scope="module")
def verdicts(harness, rows):
    return harness.reference_from_tsv(rows)


def _gc_rows(rows, kind=None, rel=None):
    out = []
    for r in rows:
        f = r.split("\t")
        if f[0] not in ("GB", "GP", "GW"):
            continue
        if kind is not None and f[0] != kind:
            continue
        if rel is not None and f[1] != rel:
            continue
        out.append(f)
    return out


def _align(harness, rels, verdicts, rows, tmp_path):
    """Run the real `checker_alignment` over a chosen file set.

    Returns the raw bucket membership rather than a file -> bucket map: a file
    can carry a chain bucket AND a coverage bucket, because the coverage arm
    is a second `if` chain after the main one (deliberately — the agreement is
    not a bucket, but the two disagreements are findings about a file the
    chain has already bucketed)."""
    (tmp_path / "harness" / "out").mkdir(parents=True, exist_ok=True)
    with pytest.MonkeyPatch.context() as mp, redirect_stdout(io.StringIO()):
        mp.setattr(harness, "FORMAL", tmp_path)
        fatal = harness.checker_alignment({rel: {} for rel in rels}, [],
                                          verdicts, rows)
        samples = {k: list(v) for k, v in harness._ALIGN_SAMPLES.items()}
        covered = list(harness._GC_COVERED)
    return samples, fatal, covered


def _coverage_buckets(samples):
    """Only the two coverage buckets, so an assertion here cannot be
    satisfied or broken by the checker's refusal for the file."""
    return {k: sorted(samples.get(k, []))
            for k in (COVERAGE_BUCKET, OBSERVATION_BUCKET)}


# ----------------------------------------------------------- the export rows

def test_every_modelled_component_carries_a_body_and_a_walk(rows):
    """`GB` is the body the parse enumerates, `GW` the walk the checker
    performed. A `GB` with no `GW` is the shape that used to read as coverage
    violated; it is the observation bucket's business instead."""
    bodies = {(f[1], f[2]) for f in _gc_rows(rows, "GB")}
    walks = {(f[1], f[2]) for f in _gc_rows(rows, "GW")}
    assert bodies
    assert walks == bodies


def test_the_export_carries_a_provide_scope_and_an_activation_scope(rows):
    """Both halves of the historical bug must be exercised: a component whose
    activation body the walk skipped, and a receiver body with parameters."""
    assert _gc_rows(rows, "GB")
    assert _gc_rows(rows, "GP")
    assert any(f[3] == "activation" for f in _gc_rows(rows, "GW")), \
        "no component in the corpus has an activation scope"


def test_a_gw_row_names_its_own_scope_and_statement_count(rows):
    for f in _gc_rows(rows, "GW"):
        assert len(f) == 7
        assert f[3], "a GW row must name the scope it visited"
        assert f[4].isdigit(), f"visited-statement count is not a number: {f}"
        assert f[6] == f"{f[2]} {f[3]}", \
            "the provenance column must name the component and the scope"


def test_a_gp_row_carries_one_qualifier_per_parameter(rows):
    for f in _gc_rows(rows, "GP"):
        assert len(f) == 7
        if not f[6]:
            continue
        for member in f[6].split(","):
            name, sep, qual = member.partition(":")
            assert sep and name and qual, f"malformed parameter {member!r}"
            assert qual in ("plain", "trusted", "untrusted", "secret")


# ------------------------------------------------- the reference decider

def test_the_reference_decides_every_exported_scope(harness, rows, verdicts):
    """One `GC` verdict per scope, out to the longer of the two lists, and
    every one of them `ok` on the corpus as it stands."""
    assert verdicts.gc
    assert set(verdicts.gc.values()) == {"ok"}
    per_file = {rel: [k for k in verdicts.gc if k[0] == rel]
                for rel in {k[0] for k in verdicts.gc}}
    for rel, per_file_rows in harness._GC_ROWS.items():
        want = sum(max(len(b), len(w)) for _c, b, w in per_file_rows)
        assert len(per_file.get(rel, [])) == want, rel


def test_the_decider_is_not_a_constant(harness):
    """`coversB` is a recursion over the scope list, not a single comparison.
    Each of the three conjuncts must move the verdict on its own — otherwise a
    row that read only one of them would pass here."""
    body = [("activation", 2, []), ("k.m", 3, ["x:untrusted"])]
    assert [v for _l, v in harness._gc_covers(body, list(body))] == ["ok", "ok"]

    relabelled = [("activation", 2, []), ("k.other", 3, ["x:untrusted"])]
    assert "fail" in [v for _l, v in harness._gc_covers(body, relabelled)]

    bumped = [("activation", 2, []), ("k.m", 4, ["x:untrusted"])]
    assert "fail" in [v for _l, v in harness._gc_covers(body, bumped)]

    stripped = [("activation", 2, []), ("k.m", 3, [])]
    assert "fail" in [v for _l, v in harness._gc_covers(body, stripped)]


def test_the_decider_runs_out_to_the_longer_list(harness):
    """A walk that opened FEWER scopes still yields a named `fail` for the
    missing one, and a PADDED walk is refused rather than truncated."""
    body = [("activation", 2, []), ("k.m", 3, ["x:untrusted"])]
    short = harness._gc_covers(body, body[:1])
    assert len(short) == 2 and short[1] == ("k.m", "fail")
    padded = harness._gc_covers(body, body + [("activation", 1, [])])
    assert len(padded) == 3 and padded[2][1] == "fail"


def test_an_empty_walk_against_a_one_scope_body_is_already_false(harness):
    """The theorem is sharper than the harness: `coversB` refuses the empty
    walk even where the alignment splits the file into the observation bucket
    because it has no walk to disagree with."""
    assert harness._gc_covers([("activation", 2, [])], []) == \
        [("activation", "fail")]


# ------------------------------------------------- the non-vacuity ratchet

def test_the_ratchet_is_satisfied(harness, rows, verdicts):
    harness.reference_from_tsv(rows)
    assert harness.g9coverage_coverage() == []


def test_the_ratchet_bites_when_the_row_reads_nothing(harness, rows):
    """A row that cannot fail must not satisfy the ratchet. Replacing the
    decider with a constant leaves all four shortenings undetected, and the
    ratchet says so four times rather than passing."""
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(harness, "_gc_covers", lambda body, walk: [])
        findings = harness.g9coverage_coverage()
    assert len([f for f in findings if "changes no verdict" in f]) == 4, findings


def test_the_ratchet_bites_when_nothing_was_observed(harness):
    """A recorder that captured no scope is an INSTRUMENT failure, and the
    ratchet names it as one instead of reporting a coverage failure."""
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(harness, "_GC_CENSUS", {"active": 0})
        findings = harness.g9coverage_coverage()
    assert findings == [
        "g9 coverage: the observation ran over no ACTIVE file — the recorder "
        "captured nothing, so the row is its own blind spot"]

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(harness, "_GC_CENSUS", {"active": 1, "scopes": 0})
        findings = harness.g9coverage_coverage()
    assert findings == [
        "g9 coverage: the observation recorded no SCOPE at all — the "
        "`_FlowChecker` seam this pins did not fire, so a `fail` row would be "
        "reporting a coverage failure that is really an instrument failure"]


def test_the_ratchet_bites_without_any_row_at_all(harness):
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(harness, "_GC_ROWS", {})
        findings = harness.g9coverage_coverage()
    assert findings == [
        "g9 coverage: no GB/GP/GW rows at all — the row would decide nothing "
        "and agree vacuously"]


def test_an_unobserved_component_is_its_own_finding(harness):
    """The bucket the review asked for: a component the parse says has scopes
    and the recorder captured none for is reported as an OBSERVATION finding,
    worded so it cannot be read as a coverage failure."""
    rel = sorted(harness._GC_ROWS)[0]
    try:
        harness._GC_UNOBSERVED[rel] = (
            "the parse gives App 2 scope(s) and the recorder captured none")
        findings = harness.g9coverage_coverage()
    finally:
        harness._GC_UNOBSERVED.pop(rel, None)
    assert findings and findings[0].startswith("g9 coverage observation: ")
    assert findings[0].startswith(f"g9 coverage observation: {rel}: ")


# ------------------------------------------------------- the two buckets

def test_both_buckets_are_fatal_and_the_agreement_bucket_is_retired(harness):
    assert COVERAGE_BUCKET in harness.FATAL_BUCKETS
    assert OBSERVATION_BUCKET in harness.FATAL_BUCKETS
    assert RETIRED_BUCKET not in harness.FATAL_BUCKETS


def test_the_agreement_is_counted_on_its_own_axis(harness, verdicts, rows,
                                                  tmp_path):
    """Agreement is a second reading of files the chain has already bucketed,
    so it carries a count and no membership. Filing it as a bucket would give
    one file two, and would make the census's "files bucketed" total exceed
    the corpus."""
    rels = sorted({k[0] for k in verdicts.gc})
    assert rels
    samples, fatal, covered = _align(harness, rels, verdicts, rows, tmp_path)
    assert fatal == []
    assert RETIRED_BUCKET not in samples
    assert _coverage_buckets(samples) == {COVERAGE_BUCKET: [],
                                          OBSERVATION_BUCKET: []}
    assert sorted(covered) == rels


def test_a_shortened_walk_files_under_missed_g9_coverage(harness, verdicts,
                                                         rows, tmp_path):
    """The coverage DISAGREEMENT is a bucket, and it is fatal."""
    rels = sorted({k[0] for k in verdicts.gc})
    blind = verdicts._replace(gc={k: "fail" for k in verdicts.gc})
    samples, fatal, covered = _align(harness, rels, blind, rows, tmp_path)
    assert _coverage_buckets(samples) == {COVERAGE_BUCKET: rels,
                                          OBSERVATION_BUCKET: []}
    assert covered == []
    assert [x for x in fatal if x.startswith(COVERAGE_BUCKET)] == \
        [f"{COVERAGE_BUCKET}: {rel}" for rel in rels]


def test_an_unobserved_walk_takes_the_observation_bucket_not_coverage(
        harness, verdicts, rows, tmp_path):
    """The point of the second bucket. When the recorder captured nothing the
    oracle also reports `fail` for every scope it can name — the same `fail`
    the coverage bucket reads — so the observation test has to come FIRST, or
    a renamed seam would be reported as the checker having skipped a
    statement. Both are fatal and both name the file, so nothing is masked."""
    rels = sorted({k[0] for k in verdicts.gc})
    rel = rels[0]
    rest = [x for x in rels if x != rel]
    blind = verdicts._replace(gc={k: "fail" for k in verdicts.gc})
    try:
        harness._GC_UNOBSERVED[rel] = (
            "the parse gives App 1 scope(s) and the recorder captured none")
        samples, fatal, _covered = _align(harness, rels, blind, rows, tmp_path)
    finally:
        harness._GC_UNOBSERVED.pop(rel, None)
    assert _coverage_buckets(samples) == {COVERAGE_BUCKET: rest,
                                          OBSERVATION_BUCKET: [rel]}
    assert f"{OBSERVATION_BUCKET}: {rel}" in fatal


# ------------------------------------------------------ the pinned seams

def test_the_pinned_seams_are_where_the_row_says():
    """The exporter reaches into shipped internals. The review asked for these
    named, so they are pinned here rather than left to a comment: if any of
    them moves, this fails and the row is re-derived rather than silently
    adapting."""
    from revl import taint

    walk = inspect.signature(taint._walk_component_methods)
    assert "component" in walk.parameters, \
        "the walk no longer receives the component, so no body can be named"
    assert "model" in walk.parameters

    run = inspect.signature(taint._FlowChecker.run)
    assert list(run.parameters) == ["self", "body", "env"], \
        "the outermost `run` seam has moved"
    # `enforce` is an INSTANCE attribute, not a class one: the recorder reads
    # it off the instance it is wrapping. Pin the assignment, since a class
    # attribute would make every nested run look like an outer one.
    init = inspect.getsource(taint._FlowChecker.__init__)
    assert "self.enforce = enforce" in init, \
        "`enforce` is no longer set from the constructor's argument, so the " \
        "outer-run filter the recorder applies cannot be read"

    assert inspect.signature(taint._seed_param_env).parameters, \
        "_seed_param_env is the seam that decides which parameters a scope " \
        "carries; it must exist"


def test_the_endorse_label_spellings_are_the_two_the_recorder_reads():
    """The recorder reconciles the walk's label with the body's scope by
    matching these two spellings. A third shape is a finding, not a guess."""
    source = (ROOT / "src" / "revl" / "taint.py").read_text(encoding="utf-8")
    assert 'f"{component} activation"' in source, \
        "the activation spelling has moved"
    assert 'f"{component}.{mname}"' in source, \
        "the provide-method spelling has moved"


# ----------------------------------------------------------- the Lean half

def test_the_lean_oracle_agrees_on_every_coverage_scope(harness, rows,
                                                        verdicts,
                                                        tmp_path_factory):
    if shutil.which("lake") is None:
        pytest.skip("lake is not on PATH; the formal gate runs this half")
    root = tmp_path_factory.mktemp("g9coverage_oracle")
    tsv = root / "corpus.tsv"
    tsv.write_text("\n".join(rows) + "\n", encoding="utf-8")
    formal = harness.parse_verdicts(
        harness.run_oracle(tsv, root / "formal_verdicts.tsv"))
    assert formal.gc == verdicts.gc
    assert formal.gc, "the oracle decided no coverage scope at all"
