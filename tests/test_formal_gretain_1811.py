"""G-RETAIN in the formal model (issue #1811, group 3).

The checker refuses, with code `G-RETAIN` (`retention`), a `Retained[T, P]`
value that reaches a persistence sink after `P`'s deadline. The verdict
depends on the wall clock, and the refusal carries the instant it used.

`RevL.GRetain` states the rule over the scope, the sink and the walk the
checker REPORTS, at the instant the checker COMPARED. This module pins the
differential-oracle row issue #1811 group 3 added for it: the row's decider
is pinned to the rule by `RevL.GRetain.rowB_iff`, and
`formal/harness/diff_corpus.py` carries what the checker reports as one
`RETAIN` row per refusal, filed under `agree-G-RETAIN` or the fatal
`missed-G-RETAIN`. The harness pins `now` with the checker's own
`REVL_RETENTION_AS_OF`, so the verdict is reproducible rather than
wall-clock.

That row is **the rule on the corpus, not coverage of the checker's walk**:
its premises ARE the checker's report, so it cannot witness that the walk is
complete. See `formal/RevL/Theorems/GRetain.lean` and `formal/STATUS.md`.

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

#: The corpus's G-RETAIN refusal the `RETAIN` row decides.
FIXTURE = "examples/rejections/gretain_expired_at_persistence_sink.rvl"

#: What the checker's own refusal discovered: the refusal code, the category
#: the row states the rule in, the persistence scope, the sink, the policy,
#: the policy's deadline and the checker's instant as epoch seconds, and the
#: hop count of the naming chain it reported.
DISCOVERED = {
    "code": "G-RETAIN",
    "category": "retention",
    "scope": "db",
    "sink": "db_put",
    "policy": "customer_pii",
    "until": 1577836800,          # 2020-01-01T00:00:00Z, from the hint
    "chain": "load() -> db_put",
    "hops": 1,
}


@pytest.fixture(scope="module")
def harness():
    return load_by_path("formal_diff_corpus_gretain_1811",
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

def test_the_checker_refuses_the_fixture_with_the_retention_code(harness):
    code, category = harness.checker_code(FIXTURE)
    assert (code, category) == (DISCOVERED["code"], DISCOVERED["category"])


# --------------------------------------------------------------- the facts

def test_the_row_carries_what_the_checker_discovered(rows):
    retain = [r for r in rows if r.startswith(f"RETAIN\t{FIXTURE}\t")]
    assert len(retain) == 1
    f = retain[0].split("\t")
    assert len(f) == 11
    assert (f[2], f[3], f[4], f[5], f[6]) == (
        DISCOVERED["code"], DISCOVERED["category"], DISCOVERED["scope"],
        DISCOVERED["sink"], DISCOVERED["policy"])
    assert f[7] == str(DISCOVERED["until"])
    assert f[9] == DISCOVERED["chain"]
    assert f[10] == str(DISCOVERED["hops"])


def test_the_instant_column_is_the_instant_the_harness_pinned(harness, rows):
    """The `now` fact: the row carries the checker's own `evaluation_instant`,
    and the harness pinned it, so the verdict does not move with the clock."""
    f = next(r for r in rows
             if r.startswith(f"RETAIN\t{FIXTURE}\t")).split("\t")
    assert f[8] == str(harness._gretain_epoch(harness.RETENTION_AS_OF))
    assert int(f[8]) > DISCOVERED["until"]


# ---------------------------------------------- both sides, the one fixture

def test_the_reference_decides_the_rule_violated(harness, rows, verdicts):
    harness.reference_from_tsv(rows)
    assert verdicts.retain[FIXTURE] == "fail"


def test_the_fixture_files_under_agree_G_RETAIN(harness, verdicts, rows,
                                                tmp_path):
    buckets, fatal = _align(harness, [FIXTURE], verdicts, rows, tmp_path)
    assert fatal == []
    assert buckets[FIXTURE] == "agree-G-RETAIN"


def test_a_blind_row_is_filed_under_missed_G_RETAIN(harness, verdicts, rows,
                                                    tmp_path):
    blind = verdicts._replace(retain={k: "ok" for k in verdicts.retain})
    _buckets, fatal = _align(harness, [FIXTURE], blind, rows, tmp_path)
    assert fatal == [f"missed-G-RETAIN: {FIXTURE}"]
    assert "missed-G-RETAIN" in harness.FATAL_BUCKETS


def test_a_row_that_ignores_the_clock_is_filed_under_missed(harness, verdicts,
                                                            rows, tmp_path):
    """The `now` column is what the row reads: the same file, the same sink
    and the same policy, but a row that held — i.e. one that did not read the
    instant — is the fatal `missed-G-RETAIN`."""
    held = verdicts._replace(retain={FIXTURE: "ok"})
    _buckets, fatal = _align(harness, [FIXTURE], held, rows, tmp_path)
    assert fatal == [f"missed-G-RETAIN: {FIXTURE}"]


def test_a_declaration_level_retention_refusal_is_out_of_fragment(harness,
                                                                  rows):
    """The other `G-RETAIN` refusal carries no flow at all and is a different
    judgment: it exports no row and must NOT be read as an agreement.

    Its message is spelled in `taint._refuse_retention_declaration` and says
    "declares a persistence sink" where the flow-level one says "flows into
    the persistence sink"; the arm's flow-sentence guard is what keeps the
    two apart, and `retain_rows` emits nothing for it."""
    assert harness.GRETAIN_FLOW_RE.search(
        harness.checker_message(FIXTURE))
    declaration_level = (
        "`db_put` declares a persistence sink (`db`) whose argument 1 (`row`) "
        "is `Retained[T, customer_pii]`, and that policy's retention deadline "
        "passed at 2026-01-01T00:00:00Z — data past its retention deadline "
        "may not be written to durable storage (G-RETAIN)")
    assert not harness.GRETAIN_FLOW_RE.search(declaration_level)


def test_the_lean_oracle_agrees_on_the_fixture(harness, rows,
                                               tmp_path_factory):
    if shutil.which("lake") is None:
        pytest.skip("lake is not on PATH; the formal gate runs this half")
    root = tmp_path_factory.mktemp("gretain_oracle")
    tsv = root / "corpus.tsv"
    tsv.write_text("\n".join(rows) + "\n", encoding="utf-8")
    formal = harness.parse_verdicts(
        harness.run_oracle(tsv, root / "formal_verdicts.tsv"))
    ref = harness.reference_from_tsv(rows)
    assert formal.retain == ref.retain


# ------------------------------------------------ the coverage ratchet

def test_the_coverage_ratchet_is_satisfied(harness, rows):
    harness.reference_from_tsv(rows)
    assert harness.retain_coverage() == []


def test_the_ratchet_bites_without_any_retention_row(harness, rows):
    without = [r for r in rows if not r.startswith("RETAIN\t")]
    harness.reference_from_tsv(without)
    findings = harness.retain_coverage()
    harness.reference_from_tsv(rows)
    assert len(findings) == 1 and "no RETAIN rows" in findings[0]


def test_the_ratchet_bites_when_the_instant_is_not_the_pinned_one(harness,
                                                                  rows):
    """The `now` fact, enforced by the gate rather than asserted in prose: a
    row recorded at an instant this harness did not pin fails."""
    moved = [r.replace(f"\t{harness._gretain_epoch(harness.RETENTION_AS_OF)}\t",
                       f"\t{DISCOVERED['until'] + 1}\t")
             for r in rows if r.startswith(f"RETAIN\t{FIXTURE}\t")]
    assert moved and moved[0] != next(r for r in rows
                                      if r.startswith(f"RETAIN\t{FIXTURE}\t"))
    tampered = [r if not r.startswith(f"RETAIN\t{FIXTURE}\t") else moved[0]
                for r in rows]
    harness.reference_from_tsv(tampered)
    findings = harness.retain_coverage()
    harness.reference_from_tsv(rows)
    assert any("evaluated at" in f for f in findings), findings
