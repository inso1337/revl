"""The README conformance matrix is generated, never authored (roadmap item 328).

`tools/conformance.py --write-readme` regenerates the block between the
`CONFORMANCE-MATRIX` markers in README.md from real emitter output. These tests
are the committed-artifact gate: the block in the tree must equal a fresh
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
