"""Issue #2709, first slice: the Python tier's per-call crossing cost.

`bench/extern_call_cost.py` is a measurement, so the number itself is the
evidence (results/extern-call-cost.md). This test holds the code path it
measures: the program compiles and emits, all eight arms produce a number, and
the ordering the table claims holds. It needs the cordis-py runtime the
reference tier runs on and skips without it.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BENCH = ROOT / "bench"
sys.path.insert(0, str(BENCH))
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "backends" / "python"))

needs_runtime = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="the reference tier boots on cordis-py: sh backends/python/setup.sh")


@needs_runtime
def test_the_arms_are_measured_and_ordered_as_the_table_says():
    import extern_call_cost as bench

    m = bench.measure(iters=500, samples=3)
    # the expensive arms are well clear of zero; the two body calls are only a
    # few ns over the empty loop, so they are not asserted positive
    for arm in ("latch path read", "E-Stop gate", "witnessed extern",
                "session entry (with its gate)", "provide method"):
        assert m[arm] > 0, arm
    assert m["pure extern"] >= 0 and m["direct body (witnessed)"] >= 0, m
    # the pure extern is the body itself: no crossing
    assert abs(m["pure extern"] - m["direct body (pure)"]) < \
        m["session entry (with its gate)"], m
    # a capability crossing costs more than its body, and the session entry
    # (plus the Ok branch) costs more again
    assert m["witnessed extern"] > m["direct body (witnessed)"], m
    assert m["provide method"] > m["witnessed extern"], m


@needs_runtime
def test_the_table_names_every_arm_and_says_what_could_not_be_isolated():
    import extern_call_cost as bench

    m = bench.measure(iters=200, samples=2)
    table = bench.render(m)
    for arm in bench.ARMS:
        assert arm in table
    assert "WAL discharge record" in table
    assert "not on this path" in table
