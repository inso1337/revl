"""G1 declared-access coverage in the formal model (issue #2744).

`formal/STATUS.md` carried G1 as `partial`: `RevL.G1.declared_only_access`
says an undeclared access cannot be *written*, and since issue #1807 the
`G1` row decides `RevL.G1Access.AccessOK` over each component's access roots
(`GA` rows, 21 `agree-G1` documents). Neither says the checker's name
resolution VISITS every statement of a real component body. G1 is enforced
during lowering, so the walk that resolves roots is the lowering walk.

`RevL.G1Coverage.coversAccessBodyB` states the obligation over
`RevL.Syntax.Body`: every scope of a body is opened, as many of its
statements visited as the scope declares, and every declared parameter seeded
into the scope's own environment. This module pins the differential-oracle half, the
`G1B`/`G1P`/`G1W` rows and the `G1C` verdicts that `formal/harness/diff_corpus.py`
adds.

TWO DISTINCT FATAL BUCKETS, kept apart on purpose. `missed-G1-coverage` is a
genuine coverage disagreement. `missed-G1-coverage-observation` is this
harness's OWN blind spot: the parse says a component has scopes and the
recorder captured none, so the walk was never observed rather than
observed-short. Folding the second into the first would let a moved seam read
as a coverage regression, or hide one behind the other.

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
COVERAGE_BUCKET = "missed-G1-coverage"
OBSERVATION_BUCKET = "missed-G1-coverage-observation"


@pytest.fixture(scope="module")
def harness():
    return load_by_path("formal_diff_corpus_g1coverage_2744",
                        ROOT / "formal" / "harness" / "diff_corpus.py")


@pytest.fixture(scope="module")
def rows(harness):
    with redirect_stdout(io.StringIO()):
        out, _facts, _census = harness.export()
    return out


@pytest.fixture(scope="module")
def verdicts(harness, rows):
    return harness.reference_from_tsv(rows)


def _g1_rows(rows, kind=None, rel=None):
    out = []
    for r in rows:
        f = r.split("\t")
        if f[0] not in ("G1B", "G1P", "G1W"):
            continue
        if kind is not None and f[0] != kind:
            continue
        if rel is not None and f[1] != rel:
            continue
        out.append(f)
    return out


def _align(harness, rels, verdicts, rows, tmp_path):
    """Run the real `checker_alignment` over a chosen file set and return the
    raw bucket membership, the way the G9 coverage module does: a file can
    carry a chain bucket AND a coverage bucket, because the coverage arm is a
    second `if` chain after the main one."""
    (tmp_path / "harness" / "out").mkdir(parents=True, exist_ok=True)
    with pytest.MonkeyPatch.context() as mp, redirect_stdout(io.StringIO()):
        mp.setattr(harness, "FORMAL", tmp_path)
        mp.setattr(harness, "_G1_COVERED", [])
        fatal = harness.checker_alignment({rel: {} for rel in rels}, [],
                                          verdicts, rows)
        samples = {k: list(v) for k, v in harness._ALIGN_SAMPLES.items()}
        covered = list(harness._G1_COVERED)
    return samples, fatal, covered


def _coverage_buckets(samples):
    """Only the two coverage buckets, so an assertion here cannot be
    satisfied or broken by the checker's refusal for the file."""
    return {k: sorted(samples.get(k, []))
            for k in (COVERAGE_BUCKET, OBSERVATION_BUCKET)}


# ----------------------------------------------------------- the export rows

def test_every_modelled_component_carries_a_body_and_a_walk(rows):
    """`G1B`/`G1P` are the body the parse enumerates, `G1W` the walk the
    checker performed. A component with a body and no walk is the shape the
    observation bucket exists for, not silently `ok`."""
    bodies = {(f[1], f[2]) for f in _g1_rows(rows)}
    walks = {(f[1], f[2]) for f in _g1_rows(rows, "G1W")}
    assert bodies
    assert walks


def test_the_export_carries_a_provide_scope_and_an_activation_scope(rows):
    """Both halves of the obligation must be exercised: an activation scope
    and a receiver body with declared parameters."""
    assert _g1_rows(rows, "G1B")
    assert _g1_rows(rows, "G1P")
    assert any(f[3] == "activation" for f in _g1_rows(rows, "G1W")), \
        "no component in the corpus has an activation scope"
    assert any(f[5] for f in _g1_rows(rows, "G1W")), \
        "no scope in the corpus seeds a declared parameter"


def test_a_g1w_row_names_its_own_scope_and_statement_count(rows):
    for f in _g1_rows(rows, "G1W"):
        assert len(f) == 6
        assert f[3], "a G1W row must name the scope it visited"
        assert f[4].isdigit(), f"visited-statement count is not a number: {f}"


def test_a_g1p_row_carries_declared_parameter_names(rows):
    for f in _g1_rows(rows, "G1P"):
        assert len(f) == 7
        if not f[6]:
            continue
        for member in f[6].split(","):
            assert member and "," not in member, \
                f"malformed parameter name {member!r}"


# ------------------------------------------------- the reference decider

def test_the_reference_decides_every_exported_scope(harness, rows, verdicts):
    """One `G1C` verdict per scope, out to the longer of the two lists, and
    every one of them `ok` on the corpus as it stands."""
    assert verdicts.g1cov
    assert set(verdicts.g1cov.values()) == {"ok"}
    per_file = {rel: [k for k in verdicts.g1cov if k[0] == rel]
                for rel in {k[0] for k in verdicts.g1cov}}
    for rel, per_file_rows in harness._G1_COVERAGE_ROWS.items():
        want = sum(max(len(b), len(w)) for _c, b, w in per_file_rows)
        assert len(per_file.get(rel, [])) == want, rel


def test_the_decider_is_not_a_constant(harness):
    """`coversAccessB` is a recursion over the scope list. Each of the three
    conjuncts must move the verdict on its own, otherwise a row that read
    only one of them would pass here."""
    body = [("activation", 2, []), ("k.m", 3, ["x"])]
    assert [v for _l, v in harness._g1_covers(body, list(body))] == \
        ["ok", "ok"]

    relabelled = [("activation", 2, []), ("k.other", 3, ["x"])]
    assert "fail" in [v for _l, v in harness._g1_covers(body, relabelled)]

    bumped = [("activation", 2, []), ("k.m", 4, ["x"])]
    assert "fail" in [v for _l, v in harness._g1_covers(body, bumped)]

    stripped = [("activation", 2, []), ("k.m", 3, [])]
    assert "fail" in [v for _l, v in harness._g1_covers(body, stripped)]

    seeded = [("activation", 2, []), ("k.m", 3, ["x", "y"])]
    assert "fail" in [v for _l, v in harness._g1_covers(body, seeded)]


def test_the_decider_runs_out_to_the_longer_list(harness):
    """A walk that opened FEWER scopes still yields a named `fail` for the
    missing one, and a PADDED walk is refused rather than truncated."""
    body = [("activation", 2, []), ("k.m", 3, ["x"])]
    short = harness._g1_covers(body, body[:1])
    assert len(short) == 2 and short[1] == ("k.m", "fail")
    padded = harness._g1_covers(body, body + [("activation", 1, [])])
    assert len(padded) == 3 and padded[2][1] == "fail"


def test_an_empty_walk_against_a_one_scope_body_is_already_false(harness):
    """The theorem is sharper than the harness: `coversAccessB` refuses the
    empty walk even where the alignment splits the file into the observation
    bucket because it has no walk to disagree with."""
    assert harness._g1_covers([("activation", 2, [])], []) == \
        [("activation", "fail")]


# ------------------------------------------------- the non-vacuity ratchet

def test_the_ratchet_is_satisfied(harness, rows, verdicts):
    harness.reference_from_tsv(rows)
    assert harness.g1coverage_coverage() == []


def test_the_ratchet_bites_when_the_row_reads_nothing(harness, rows):
    """A row that cannot fail must not satisfy the ratchet: replacing the
    decider with a constant leaves all four shortenings undetected."""
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(harness, "_g1_covers", lambda body, walk: [])
        findings = harness.g1coverage_coverage()
    assert len([f for f in findings if "changes no verdict" in f]) == 4, \
        findings


def test_the_ratchet_bites_when_nothing_was_observed(harness):
    """A recorder that captured no scope is an INSTRUMENT failure, and the
    ratchet names it as one instead of reporting a coverage failure."""
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(harness, "_G1_COVERAGE_CENSUS", {"active": 0})
        findings = harness.g1coverage_coverage()
    assert findings == [
        "g1 coverage: the observation ran over no ACTIVE file, the "
        "lowering recorder captured nothing, so the row is its own blind "
        "spot"]

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(harness, "_G1_COVERAGE_CENSUS", {"active": 1, "scopes": 0})
        findings = harness.g1coverage_coverage()
    assert findings == [
        "g1 coverage: the observation recorded no SCOPE at all, the "
        "`lower._note_access_visit` seam this pins did not fire, so a `fail` "
        "row would be reporting a coverage failure that is really an "
        "instrument failure"]


def test_the_ratchet_bites_without_any_row_at_all(harness):
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(harness, "_G1_COVERAGE_ROWS", {})
        findings = harness.g1coverage_coverage()
    assert findings == [
        "g1 coverage: no G1B/G1P/G1W rows at all, the row would decide "
        "nothing and agree vacuously"]


def test_an_unobserved_component_is_its_own_finding(harness):
    """A component the parse says has scopes and the recorder captured none
    for is reported as an OBSERVATION finding, worded so it cannot be read as
    a coverage failure."""
    rel = sorted(harness._G1_COVERAGE_ROWS)[0]
    try:
        harness._G1_UNOBSERVED[rel] = (
            "the parse gives App 2 scope(s) and the lowering recorder "
            "captured none")
        findings = harness.g1coverage_coverage()
    finally:
        harness._G1_UNOBSERVED.pop(rel, None)
    assert findings and findings[0].startswith("g1 coverage observation: ")
    assert findings[0].startswith(f"g1 coverage observation: {rel}: ")


# ------------------------------------------------------- the two buckets

def test_both_buckets_are_fatal(harness):
    assert COVERAGE_BUCKET in harness.FATAL_BUCKETS
    assert OBSERVATION_BUCKET in harness.FATAL_BUCKETS


def test_the_agreement_is_counted_on_its_own_axis(harness, verdicts, rows,
                                                  tmp_path):
    """Agreement is a second reading of files the chain has already bucketed,
    so it carries a count and no membership."""
    rels = sorted({k[0] for k in verdicts.g1cov})
    assert rels
    samples, fatal, covered = _align(harness, rels, verdicts, rows, tmp_path)
    assert fatal == []
    assert _coverage_buckets(samples) == {COVERAGE_BUCKET: [],
                                          OBSERVATION_BUCKET: []}
    assert sorted(covered) == rels


def test_a_shortened_walk_files_under_missed_g1_coverage(harness, verdicts,
                                                         rows, tmp_path):
    """The coverage DISAGREEMENT is a bucket, and it is fatal."""
    rels = sorted({k[0] for k in verdicts.g1cov})
    blind = verdicts._replace(
        g1cov={k: "fail" for k in verdicts.g1cov})
    samples, fatal, covered = _align(harness, rels, blind, rows, tmp_path)
    assert _coverage_buckets(samples) == {COVERAGE_BUCKET: rels,
                                          OBSERVATION_BUCKET: []}
    assert covered == []
    assert [x for x in fatal if x.startswith(COVERAGE_BUCKET)] == \
        [f"{COVERAGE_BUCKET}: {rel}" for rel in rels]


def test_an_unobserved_walk_takes_the_observation_bucket_not_coverage(
        harness, verdicts, rows, tmp_path):
    """When the recorder captured nothing the oracle also reports `fail` for
    every scope it can name, so the observation test has to come FIRST or a
    moved seam would be reported as the checker having skipped a statement."""
    rels = sorted({k[0] for k in verdicts.g1cov})
    rel = rels[0]
    rest = [x for x in rels if x != rel]
    blind = verdicts._replace(g1cov={k: "fail" for k in verdicts.g1cov})
    try:
        harness._G1_UNOBSERVED[rel] = (
            "the parse gives App 1 scope(s) and the lowering recorder "
            "captured none")
        samples, fatal, _covered = _align(harness, rels, blind, rows,
                                          tmp_path)
    finally:
        harness._G1_UNOBSERVED.pop(rel, None)
    assert _coverage_buckets(samples) == {COVERAGE_BUCKET: rest,
                                          OBSERVATION_BUCKET: [rel]}
    assert f"{OBSERVATION_BUCKET}: {rel}" in fatal


# ------------------------------------------------------ the pinned seams

def test_the_pinned_seams_are_where_the_row_says():
    """The exporter reaches into shipped internals and the row's docstring
    names them. Pin them here so a move fails loudly instead of silently
    shortening the walk."""
    from revl import lower

    assert list(inspect.signature(lower._note_access_visit).parameters) == \
        ["component", "scope"], "the visit seam has moved"
    assert list(inspect.signature(lower._note_access_scope).parameters) == \
        ["component", "scope", "params"], "the scope seam has moved"
    source = (ROOT / "src" / "revl" / "lower.py").read_text(encoding="utf-8")
    assert "_note_access_visit(comp.name, \"activation\")" in source, \
        "the activation visit is no longer recorded by the lowering loop"
    assert "for mstmt in method.body:" in source and \
        "_note_access_visit(comp.name, f\"{stmt.key}.{method.name}\")" in source, \
        "the provide-method visit is no longer recorded by the lowering loop"
    assert "env.params = env.bind_params(method.params, method.line)" in source, \
        "the parameters a provide method seeds are no longer bound there"


def _notes():
    """The one file the real-mutation test walks: a component with an
    activation scope and parameterised provide methods."""
    return "examples/app/notes.rvl"


def test_a_log_missing_the_activation_visits_is_refused_by_the_row(harness):
    """The row reads the recorder's log: silence the activation visits in the
    LOG, re-export one file, and the reference's own `G1C` verdict refuses
    the file. This patches the recorder, not the walk, so it pins how the row
    reads the log format; it does not drive a checker that really skips a
    scope (the checker still lowers every statement here). Where the seam
    sits is pinned by `test_the_pinned_seams_are_where_the_row_says`."""
    rel = _notes()
    p = harness.REPO / rel
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(harness, "_G1_COVERAGE_ROWS", {})
        mp.setattr(harness, "_G1_COVERAGE_CENSUS", {})
        mp.setattr(harness, "_G1_UNOBSERVED", {})
        real = harness.lower._note_access_visit
        mp.setattr(harness.lower, "_note_access_visit",
                   lambda component, scope: None if scope == "activation"
                   else real(component, scope))
        prog = harness.Parser(p.read_text(encoding="utf-8"), str(p)).parse()
        rows = harness.g1coverage_rows(rel, prog)
        verdicts = harness.reference_from_tsv(rows)
    fails = sorted(k for k, v in verdicts.g1cov.items() if v == "fail")
    assert fails, "a walk that never opened the activation scope was not refused"
    # The comparison is scope by scope out to the longer list, so a dropped
    # FIRST scope shifts every later one: the body's own label is what names
    # each failure, and the activation is among them.
    assert any(scope == "activation" for _rel, _comp, scope in fails), fails
    assert not harness._G1_UNOBSERVED, \
        "the activation skip was filed as an observation failure instead"


def test_a_log_missing_a_provide_scope_is_refused_by_the_row(harness):
    """The other scope kind: drop one provide method's scope open and visits
    from the recorder's LOG and the reference refuses that method's scope (the
    file still records the other scopes, so it is a coverage disagreement,
    never the observation bucket). Like the test above, this patches the
    recorder, not the walk: it pins the row's reading of the log format."""
    rel = _notes()
    p = harness.REPO / rel
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(harness, "_G1_COVERAGE_ROWS", {})
        mp.setattr(harness, "_G1_COVERAGE_CENSUS", {})
        mp.setattr(harness, "_G1_UNOBSERVED", {})
        real_open = harness.lower._note_access_scope
        real_visit = harness.lower._note_access_visit
        mp.setattr(harness.lower, "_note_access_scope",
                   lambda component, scope, params: None
                   if scope == "store.get" else real_open(component, scope,
                                                          params))
        mp.setattr(harness.lower, "_note_access_visit",
                   lambda component, scope: None
                   if scope == "store.get" else real_visit(component, scope))
        prog = harness.Parser(p.read_text(encoding="utf-8"), str(p)).parse()
        rows = harness.g1coverage_rows(rel, prog)
        verdicts = harness.reference_from_tsv(rows)
    fails = sorted(k for k, v in verdicts.g1cov.items() if v == "fail")
    assert any(scope == "store.get" for _rel, _comp, scope in fails), fails
    assert not harness._G1_UNOBSERVED, \
        "a skipped provide scope was filed as an observation failure instead"


# ----------------------------------------------------------- the Lean half

def test_the_lean_oracle_agrees_on_every_coverage_scope(
        harness, rows, verdicts, tmp_path_factory):
    if shutil.which("lake") is None:
        pytest.skip("lake is not on PATH; the formal gate runs this half")
    root = tmp_path_factory.mktemp("g1coverage_oracle")
    tsv = root / "corpus.tsv"
    tsv.write_text("\n".join(rows) + "\n", encoding="utf-8")
    text = harness.run_oracle(tsv, root / "formal_verdicts.tsv")
    formal = harness.parse_verdicts(text)
    assert formal.g1cov == verdicts.g1cov
    assert formal.g1cov, "the oracle decided no coverage scope at all"
    # the Lean half of the ratchet: `accessCoverageRowB` prints `ok` on the
    # recorded walks and `fail` under each shortening, on the real oracle
    assert "\nG1CM\t" in "\n" + text
    assert harness.g1coverage_coverage(text) == []


# ------------------------------- review follow-up: order, Lean half, limits

#: An accepted body whose activation `emit` comes AFTER its `provide` block:
#: the lowering loop opens the provide scope first.
PROVIDE_THEN_EMIT = "tests/fixtures/g1_coverage/provide_then_emit.rvl"


def _export_one(harness, rel):
    p = harness.REPO / rel
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(harness, "_G1_COVERAGE_ROWS", {})
        mp.setattr(harness, "_G1_COVERAGE_CENSUS", {})
        mp.setattr(harness, "_G1_UNOBSERVED", {})
        prog = harness.Parser(p.read_text(encoding="utf-8"), str(p)).parse()
        rows = harness.g1coverage_rows(rel, prog)
        per_file = dict(harness._G1_COVERAGE_ROWS)
    return rows, per_file


def test_the_walk_is_compared_activation_first(harness):
    """The checker's opening order is not the model's: the activation scope
    is moved to the front and the provide scopes keep the walk's order."""
    observed = {"b.m": {"stmts": 1, "params": ["y"]},
                "activation": {"stmts": 2, "params": []},
                "a.n": {"stmts": 1, "params": []}}
    assert harness._access_walk_scopes(observed) == [
        ("activation", 2, []), ("b.m", 1, ["y"]), ("a.n", 1, [])]


def test_an_activation_emit_after_a_provide_block_is_covered(harness):
    """The accepted shape the review reproduced: before the fix the walk
    opened `ops.run` before `activation` and BOTH scopes printed `fail`."""
    rows, _ = _export_one(harness, PROVIDE_THEN_EMIT)
    walk = [f[3] for f in (r.split("\t") for r in rows) if f[0] == "G1W"]
    assert walk == ["activation", "ops.run"], rows
    verdicts = harness.reference_from_tsv(rows)
    assert verdicts.g1cov == {
        (PROVIDE_THEN_EMIT, "Mgr", "activation"): "ok",
        (PROVIDE_THEN_EMIT, "Mgr", "ops.run"): "ok"}


def test_the_export_carries_each_shortening_for_the_lean_oracle(harness):
    """One `G1M` row per kind for every recorded walk, the walk in one
    column, so the Lean oracle decides the same shortenings Python does."""
    rows, per_file = _export_one(harness, PROVIDE_THEN_EMIT)
    g1m = [r.split("\t") for r in rows if r.startswith("G1M\t")]
    assert [f[1] for f in g1m] == list(harness.G1_SHORTENINGS)
    by_kind = {f[1]: f[4] for f in g1m}
    assert by_kind["as-walked"] == "activation|1|;ops.run|1|x"
    assert by_kind["drop"] == "ops.run|1|x"
    assert by_kind["strip"] == "activation|1|;ops.run|1|"
    (_comp, _body, walk), = per_file[PROVIDE_THEN_EMIT]
    assert dict(harness._g1_shortenings(walk))["bump"] == [
        ("activation", 2, []), ("ops.run", 2, ["x"])]


def _lean_text(harness, verdict_of):
    """A stand-in for the oracle's `G1CM` output over the module's rows,
    every verdict chosen by `verdict_of(kind, body, walk)`."""
    lines = []
    for rel, per_file in harness._G1_COVERAGE_ROWS.items():
        for comp, body, walk in per_file:
            if not walk:
                continue
            for kind, mutated in harness._g1_shortenings(walk):
                lines.append("\t".join(["G1CM", kind, rel, comp,
                                        "covers=" + verdict_of(kind, body,
                                                               mutated)]))
    return "\n".join(lines) + "\n"


def test_the_ratchet_reads_the_lean_verdicts(harness, rows):
    """An oracle printing the right whole-walk verdict satisfies the Lean
    half; one printing a constant does not."""
    harness.reference_from_tsv(rows)
    right = _lean_text(harness, lambda _k, body, w: "ok" if all(
        v == "ok" for _lb, v in harness._g1_covers(body, w)) else "fail")
    assert harness.g1coverage_coverage(right) == []


def test_the_ratchet_bites_when_the_lean_oracle_prints_ok(harness, rows):
    harness.reference_from_tsv(rows)
    findings = harness.g1coverage_coverage(
        _lean_text(harness, lambda *_a: "ok"))
    for kind in ("drop", "bump", "strip", "extra"):
        assert any(f"{kind!r} shortening" in f for f in findings), findings


def test_the_ratchet_bites_when_the_lean_oracle_prints_fail(harness, rows):
    harness.reference_from_tsv(rows)
    findings = harness.g1coverage_coverage(
        _lean_text(harness, lambda *_a: "fail"))
    assert any("printed `ok` on no recorded walk" in f for f in findings), \
        findings


def test_the_ratchet_bites_when_the_lean_oracle_prints_nothing(harness, rows):
    harness.reference_from_tsv(rows)
    findings = harness.g1coverage_coverage("")
    assert any("got no G1CM verdict" in f for f in findings), findings


def test_the_stated_remainder_names_the_observation_limits():
    """What the row does NOT observe is stated where a reader looks: the
    visit is recorded where the loop reaches a statement, before dispatch;
    the printed per-scope line is not the whole-walk Bool; and no theorem
    links typing to coverage."""
    status = (ROOT / "formal" / "STATUS.md").read_text(encoding="utf-8")
    g1 = next(line for line in status.splitlines()
              if line.startswith("| **G1** declared access"))
    assert "before it is dispatched" in g1
    assert "`G1CM`" in g1 and "`Walked.matches`" in g1
    assert "no theorem links `Body.TypedIn` or `AccessOK` to coverage" in g1
    source = (ROOT / "src" / "revl" / "lower.py").read_text(encoding="utf-8")
    assert "REACHED BY THE LOWERING LOOP" in source


def test_the_status_axioms_column_matches_print_axioms():
    """The G1 coverage rows' axioms column is what `#print axioms` gives."""
    if shutil.which("lake") is None:
        pytest.skip("lake is not on PATH; the formal gate runs this half")
    import re
    import subprocess
    out = subprocess.run(["lake", "env", "lean", "CheckAxioms.lean"],
                         cwd=ROOT / "formal", capture_output=True, text=True,
                         check=True).stdout
    real = {}
    for m in re.finditer(r"'(RevL\.G1Coverage\.[^']+)' (depends on axioms: "
                         r"\[(.*)\]|does not depend on any axioms)", out):
        real[m.group(1)] = (m.group(3) or "").replace(" ", "")
    assert real, "CheckAxioms.lean printed no G1Coverage theorem"
    status = (ROOT / "formal" / "STATUS.md").read_text(encoding="utf-8")
    for name, axioms in real.items():
        row = next(line for line in status.splitlines()
                   if line.startswith(f"| `{name}` |"))
        column = row.split(" | ")[3].strip()
        want = ",".join(f"`{a}`" for a in axioms.split(",")) if axioms \
            else "none"
        assert column.replace(", ", ",") == want, (name, column, axioms)
