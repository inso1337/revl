"""Guard for the in-process admission gate harness (roadmap item 333, Slice 1).

`bench/inprocess_gate_harness.py` embeds revl's admission gate the way an agent
tool-generation loop does - in-process, no `revl mcp serve`, no IPC - and proves
that the in-process verdict equals the reference admission verdict for a batch of
proposed candidates. This test keeps that proof honest in CI.

It asserts the three things the design's exit test (docs/design/333-inprocess-
gate.md) requires, plus that the harness runs:

  1. Verdict identity: every in-process `(admitted, code)` equals the reference
     ADMISSION oracle's, run as a fresh subprocess on the identical inputs. The
     batch spans both directions and both entry points and includes a hole-draft
     probe, so the identity is exercised for admits and refusals alike.
  2. A1 (CRITICAL): the oracle is the admission path, NOT `revl compile`. The
     hole draft is REFUSED in-process (code T3), while a naive `revl compile`
     oracle ACCEPTS it at exit 0 - so this test FAILS the day someone rewires the
     oracle to `revl compile` (or strips `refuse_admission` from `admit`).
  3. A2: verdicts are order-independent (fixed vs shuffled batch order in one
     process yields identical per-candidate verdicts), the property that proves
     the layer-1 gate is stateless.
  4. The harness runs end-to-end (small cost run + report render).

Like tests/test_admission_latency.py, it does NOT pin a wall-clock figure - CI
machines vary by an order of magnitude - only a generous ceiling that a
catastrophic (tens-of-ms, super-linear) regression on the representative
scenario would trip. The tracked distribution lives in
bench/results/inprocess-gate.md.
"""

import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bench"))
sys.path.insert(0, str(ROOT / "src"))

import inprocess_gate_harness as h  # noqa: E402

# Representative scenario median is ~0.2-0.3 ms locally (see results/). This is a
# gross-regression ceiling only, not a tight figure: local headroom is large, a
# slower CI runner still sits well under it, and a super-linear regression or
# disk I/O sneaking back into the timed path would blow past it.
CEILING_MS = 25.0


def test_inprocess_verdicts_match_the_admission_oracle():
    """Every in-process verdict equals the reference admission oracle's verdict
    (design exit test). The oracle is a fresh subprocess running the SAME
    admission engine on the SAME inputs, so an equal `(admitted, code)` is the
    end-to-end identity the whole item claims."""
    manifest = h.base_manifest()
    batch = h.correctness_batch()
    records = h.check_matches(batch, manifest)

    for r in records:
        assert r.match, (
            f"{r.name}: in-process verdict {r.inproc} != admission oracle "
            f"{r.oracle} - the in-process gate diverged from the reference "
            f"admission path")

    # both verdict directions are actually exercised, not just one.
    assert any(r.inproc[0] for r in records), "no candidate was admitted"
    assert any(not r.inproc[0] for r in records), "no candidate was refused"


def test_hole_draft_is_refused_in_process_and_the_naive_compile_oracle_disagrees():
    """A1 (CRITICAL). The hole-draft candidate is REFUSED in-process (T3) and by
    the admission oracle, while a naive `revl compile` oracle ACCEPTS it (exit
    0). The two DISAGREE, which is exactly why the oracle must be the admission
    path and not `revl compile`. If someone rewires the harness to oracle against
    `revl compile`, or strips `refuse_admission` from `admit`, this test fails
    and names the security regression."""
    manifest = h.base_manifest()
    holes = [c for c in h.correctness_batch() if c.is_hole]
    assert holes, "the batch must include a hole-draft probe (the A1 test)"

    for hole in holes:
        inproc = h.inprocess_verdict(hole, manifest)
        assert inproc[0] is False, (
            f"{hole.name}: the in-process gate must REFUSE a draft with an open "
            f"hole (it may never run), got admitted")
        assert inproc[1] == "T3", (
            f"{hole.name}: hole refusal must carry admission code T3, got "
            f"{inproc[1]!r}")
        assert h.oracle_verdict(hole) == inproc, (
            f"{hole.name}: the admission oracle must refuse the hole draft too")

    # the naive control: `revl compile` ACCEPTS the same draft (exit 0). This
    # MUST be true - it is the trap A1 warns about - and it MUST disagree with
    # the admission refusal above.
    hole_source = holes[0].source
    assert h.naive_compile_accepts(hole_source) is True, (
        "`revl compile` should accept a draft-with-holes at exit 0 (it does not "
        "call refuse_admission); if this changed, the A1 negative control no "
        "longer proves the oracle is the admission verb")


def test_batch_verdicts_are_order_independent():
    """A2: admitting the batch in a fixed order and in a shuffled order in the
    same process yields identical per-candidate verdicts. Any drift would expose
    a per-process cache (or a mutated manifest) making admit N depend on N-1."""
    manifest = h.base_manifest()
    batch = h.correctness_batch()
    ok, detail = h.order_independence(batch, manifest)
    assert ok, f"batch verdicts depend on ordering (statefulness bug): {detail['mismatches']}"


def test_measure_cost_reports_a_distribution_under_a_generous_ceiling():
    """The harness runs end-to-end: a small cost run returns a well-shaped
    distribution per size cell, and the representative-scenario median stays
    under a generous gross-regression ceiling (no hard wall-clock assert)."""
    cost = h.measure_cost(iters=40, warmup=10)

    assert cost["cells"], "cost measurement produced no cells"
    for cell in cost["cells"]:
        s = cell["stats"]
        assert s["n"] == 40
        assert s["min"] <= s["median"] <= s["p90"] <= s["p99"]
        assert s["median"] > 0.0

    rep = cost["representative"]
    assert rep["median"] < CEILING_MS, (
        f"representative in-process round-trip median {rep['median']:.3f} ms "
        f"exceeded the {CEILING_MS} ms regression ceiling - investigate "
        f"bench/inprocess_gate_harness.py (expected tenths of a ms; this "
        f"ceiling only catches a catastrophic, order-of-magnitude regression)")


def test_render_md_produces_a_report():
    """The report renders to a string carrying the load-bearing claims, so the
    committed results file stays in sync with the code."""
    manifest = h.base_manifest()
    batch = h.correctness_batch()
    records = h.check_matches(batch, manifest)
    order_ok, _ = h.order_independence(batch, manifest)
    naive = h.naive_compile_accepts(next(c for c in batch if c.is_hole).source)
    cost = h.measure_cost(iters=30, warmup=5)

    md = h.render_md(cost, records, order_ok, naive)
    assert "In-process admission gate" in md
    assert "not `revl compile`" in md
    assert "admitted` is not\n`safe to run unwitnessed`" in md or \
           "safe to run unwitnessed" in md
    # the gate surface version is logged for drift attribution (A4).
    assert "frontier=reference-full" in md


def test_all_medians_are_reported_as_a_distribution_not_a_single_number():
    """A5: the cost is a distribution across candidate/manifest sizes. A larger
    candidate must cost strictly more than a small one (parse/check/lower scale
    with size), so the harness cannot collapse to one reasserted headline."""
    cost = h.measure_cost(iters=60, warmup=10)
    by_size = {}
    for cell in cost["cells"]:
        by_size.setdefault(cell["candidate"], []).append(cell["stats"]["median"])
    # small/medium/large cells all present.
    assert {"small", "medium", "large"} <= set(by_size)
    small = statistics.median(by_size["small"])
    large = statistics.median(by_size["large"])
    assert large > small, (
        f"large-candidate median {large:.3f} ms should exceed small-candidate "
        f"median {small:.3f} ms - cost scales with candidate size, it is not a "
        f"single universal constant")
