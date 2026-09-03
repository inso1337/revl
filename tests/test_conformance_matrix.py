"""docs/conformance.md's tables are generated, never authored (item 328, #233).

`tools/conformance.py --write-readme` regenerates two blocks in
docs/conformance.md from real emitter output: the construct x tier matrix
(`CONFORMANCE-MATRIX`) and the per-tier emit sweep (`CONFORMANCE-SWEEP`). These
tests are the committed-artifact gate: each block in the tree must equal a fresh
generation, the generation must be deterministic (so the gate cannot flap), and
the revl-native self-host column must be present. Same contract as the other
generated-artifact gates in the tree — a stale block fails the build instead of
drifting quietly.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "src"))

import conformance  # noqa: E402


def _fresh_block() -> str:
    return conformance.readme_block(conformance.run(), conformance.selfhost_column())


def test_matrix_block_is_not_stale():
    """The committed matrix block (in docs/conformance.md) must match a fresh
    generation. The README carries only a short prose summary and links here."""
    doc = (ROOT / "docs" / "conformance.md").read_text(encoding="utf-8")
    start, end = conformance.README_START, conformance.README_END
    assert start in doc and end in doc, "the matrix markers are missing from docs/conformance.md"
    committed = doc[doc.index(start):doc.index(end) + len(end)]
    assert committed == _fresh_block(), (
        "docs/conformance.md matrix is stale — run "
        "`python3 tools/conformance.py --write-readme` (or `make matrix`) and commit")


def test_generation_is_deterministic():
    """Two generations must be byte-identical, or the staleness gate would flap."""
    assert _fresh_block() == _fresh_block()


def test_no_wall_clock_leaks_into_the_gated_block():
    """A timing number in the gated block would make every re-run stale.

    Live per-tier emit timings are a `--markdown` operator readout only; the
    committed block carries none of them.
    """
    block = _fresh_block()
    assert "emit (ms)" not in block
    assert "per construct" not in block


def test_revl_self_host_column_is_present():
    """revl must appear as its own column — the matrix shows revl conforming to
    itself, not only to its six host tiers."""
    block = _fresh_block()
    assert "| revl |" in block, "the self-host column header is missing"
    assert "revl (self-host)" in block, "the self-host summary row is missing"


def test_deliberate_limit_and_real_gap_are_distinguished():
    """The three-way distinction (ok / deliberate limit / real gap) is keyed on
    how a refusal was raised: a tier's own EmitError is a deliberate limit, any
    other exception is a real gap. Today every tier is gap-free; if that ever
    stops being true the matrix says so loudly (**GAP**), feeding the v3.0 E1
    exit test."""
    report = conformance.run()
    for row in report["cases"]:
        for tier in conformance.TIERS:
            assert row["emit_kind"][tier] in ("ok", "limit", "gap")
    real_gaps = {tier: [row["case"] for row in report["cases"]
                        if row["emit_kind"][tier] == "gap"]
                 for tier in conformance.TIERS}
    real_gaps = {t: cases for t, cases in real_gaps.items() if cases}
    assert not real_gaps, (
        "a backend has a real emit gap (an unhandled construct, not a declared "
        f"tier limit): {real_gaps}. This is the E1 signal — close it or the "
        "matrix regenerates with a **GAP** cell.")


# ---------------------------------------------------------------------------
# the per-tier emit sweep (issue #233)
# ---------------------------------------------------------------------------
# The sweep summary lived beside the matrix in the same file, under a heading
# that said counts were "from the run at the commit that closed the first
# sweep" — that is, a remembered measurement with no gate. It rotted: rust read
# 3 refusals against a measured 0 and java 3 against a measured 1. The table is
# generated now and these tests are its committed-artifact gate, the same
# contract the matrix above has had since item 328.


def _fresh_sweep() -> str:
    return conformance.sweep_block(conformance.run())


def test_sweep_block_is_not_stale():
    """The committed emit-sweep block must match a fresh generation."""
    doc = (ROOT / "docs" / "conformance.md").read_text(encoding="utf-8")
    start, end = conformance.SWEEP_START, conformance.SWEEP_END
    assert start in doc and end in doc, (
        "the emit-sweep markers are missing from docs/conformance.md — the "
        "per-tier refusal table must stay generated, not be re-authored by hand")
    committed = doc[doc.index(start):doc.index(end) + len(end)]
    assert committed == _fresh_sweep(), (
        "docs/conformance.md emit sweep is stale — run "
        "`python3 tools/conformance.py --write-readme` (or `make matrix`) and commit")


def test_sweep_generation_is_deterministic():
    """Two generations must be byte-identical, or the staleness gate would flap.

    The `deliberate` column aggregates causes into a dict, so its ordering is
    the one thing here that could plausibly wobble; it is sorted explicitly.
    """
    assert _fresh_sweep() == _fresh_sweep()


def test_sweep_counts_match_the_measured_run():
    """The rendered refusal count per tier is the measured one, not a literal.

    This is the assertion the old hand-authored table could not make. It reads
    the committed markdown back out and compares each row against `run()`.
    """
    report = conformance.run()
    doc = (ROOT / "docs" / "conformance.md").read_text(encoding="utf-8")
    start, end = conformance.SWEEP_START, conformance.SWEEP_END
    block = doc[doc.index(start):doc.index(end)]

    rendered = {}
    for line in block.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) == 4 and cells[1].strip("*").isdigit():
            rendered[cells[0]] = int(cells[1])

    measured = {conformance._short(tier): len(report["gaps"].get(tier) or [])
                for tier in conformance.TIERS}
    assert rendered == measured, (
        f"the committed sweep table says {rendered}, a fresh run says {measured}")


def test_sweep_covers_every_tier():
    """Every tier gets a row. `go` had none in the hand-authored table, so the
    one tier added after that table was written was simply absent from it."""
    doc = (ROOT / "docs" / "conformance.md").read_text(encoding="utf-8")
    block = doc[doc.index(conformance.SWEEP_START):doc.index(conformance.SWEEP_END)]
    for tier in conformance.TIERS:
        assert f"| {conformance._short(tier)} | " in block, f"{tier} has no sweep row"


def test_refusal_reasons_are_all_recognised():
    """Every deliberate refusal classifies to a named cause, not to its raw
    message. `refusal_reason` falls back to the message when no rule matches,
    which is deliberately conspicuous: a new refusal class should be named here
    rather than quietly rendering an emitter sentence into the table."""
    report = conformance.run()
    unnamed = []
    for tier in conformance.TIERS:
        for item in report["gaps"].get(tier) or []:
            if not item["deliberate"]:
                continue
            reason = conformance.refusal_reason(item["message"])
            if reason == item["message"]:
                unnamed.append((tier, item["case"], item["message"]))
    assert not unnamed, (
        "a deliberate refusal has no classification rule in "
        f"tools/conformance.py `_REFUSAL_RULES`: {unnamed}")


def test_refusal_reason_reads_the_cause_out_of_the_message():
    """Type and extern refusals name their own cause, so a new refusing type or
    a new tier needs no edit to the classifier."""
    assert conformance.refusal_reason(
        "f: param 'x': type 'Float' is not lowerable — this tier supports Int"
    ) == "`Float` signature"
    assert conformance.refusal_reason(
        "C: s.f param 'x': type 'Map[Str, Int]' is not lowerable — ..."
    ) == "`Map` signature"
    assert conformance.refusal_reason(
        "extern `ship` has no @rs body — not portable to this backend"
    ) == "extern with no `@rs` body"
    assert conformance.refusal_reason(
        "C: config blocks are not lowerable — no instantiation-config channel"
    ) == "config block"


def test_sweep_block_carries_no_wall_clock():
    """Same rule as the matrix: nothing in a gated block may change per run."""
    block = _fresh_sweep()
    assert "ms" not in block
