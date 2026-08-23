"""Guard for the admission-latency benchmark (roadmap item 16).

`bench/admission_latency.py` measures the in-memory `compile_source` -> admission
round-trip and commits a number (`bench/results/admission-latency.md`). This test
keeps that number from silently rotting: it runs a *small* iteration count and
asserts the round-trip actually completes and returns an *admitted* result.

It deliberately does NOT assert the committed wall-clock figure — CI machines
vary by an order of magnitude, so pinning ~0.16 ms would flake. It asserts:

  1. Correctness: the candidate is admitted and the resulting composition carries
     both the candidate component and its new service (the real work happened).
  2. That it runs: a small warm loop of round-trips all succeed.
  3. A single generous order-of-magnitude *ceiling* on the median, purely to
     catch a catastrophic (tens-of-ms) regression — e.g. admission accidentally
     going super-linear over the running composition, or reintroducing disk I/O
     into the timed path. See CEILING_MS below for why it sits where it does.
"""

import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bench"))

import admission_latency as al  # noqa: E402

# Measured median on an Apple M1 Max is ~0.16 ms (see results/admission-latency.md).
# A wall-clock ceiling cannot pin that across machines, so this is set only to
# trip on a gross regression, not a modest one:
#   - local headroom ~150x over the 0.16 ms baseline;
#   - a CI runner several times slower still sits at a few ms, leaving ~10x;
#   - a regression that pushed per-admit into the tens of ms (super-linear
#     re-linking, disk I/O sneaking back in) would blow past it.
# A genuine ~10x slowdown to ~1.6 ms is caught by the *tracked* number in
# results/, which is the right place for a tight figure; CI only guards against
# the catastrophic case.
CEILING_MS = 20.0


def test_round_trip_admits_the_candidate():
    """The in-memory compile+admit round-trip returns an admitted document
    carrying the candidate component and its newly provided service."""
    running = al.build_running()
    doc = al.admit_once(running)

    # only the newly compiled component comes back...
    assert [c["name"] for c in doc["components"]] == ["CacheLayer"]
    # ...and the resulting composition manifest carries it alongside the running two
    manifest_names = {c["name"] for c in doc["manifest"]["components"]}
    assert {"Kv", "App", "CacheLayer"} <= manifest_names
    # the gate actually linked requires/provides: the new service is in scope
    assert "Cache" in doc["services"]
    assert "Store" in doc["services"]  # ambient service carried through


def test_round_trip_runs_repeatedly_under_a_generous_ceiling():
    """A small warm loop of round-trips all complete, and the median stays under
    a deliberately loose ceiling (see CEILING_MS) that only a gross regression
    would exceed."""
    running = al.build_running()
    # warm (exclude one-time import/first-call cost from the sample)
    for _ in range(20):
        al.admit_once(running)

    samples = []
    for _ in range(200):
        t0 = time.perf_counter()
        doc = al.admit_once(running)
        samples.append((time.perf_counter() - t0) * 1000.0)
        assert doc["components"][0]["name"] == "CacheLayer"  # still correct each time

    median = statistics.median(samples)
    assert median < CEILING_MS, (
        f"admission round-trip median {median:.3f} ms exceeded the {CEILING_MS} ms "
        f"regression ceiling — investigate bench/admission_latency.py "
        f"(expected ~0.16 ms on an M1 Max; this ceiling only catches a "
        f"catastrophic, order-of-magnitude regression)"
    )


def test_measure_helper_reports_both_figures():
    """The importable `measure()` returns round-trip and compile-only stats with
    a sane shape, so the CLI and results file stay in sync with the code."""
    res = al.measure(iters=50, warmup=10)
    for key in ("round_trip", "compile_only"):
        s = res[key]
        assert s["n"] == 50
        assert s["min"] <= s["median"] <= s["p90"] <= s["p99"]
        assert s["median"] > 0.0
