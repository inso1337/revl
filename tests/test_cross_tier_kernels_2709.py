"""Issue #2709, second slice: the cross-tier kernel harness.

`bench/cross_tier_kernels/harness.py` is a measurement, so the committed table
is the evidence. These tests hold the parts that must not drift: the kernel
source compiles, every tier computes the same pinned checksum, the table carries
the nine columns the issue names, and a missing toolchain is reported as
skipped instead of measured.
"""

import importlib.util
from pathlib import Path

import pytest
from _load_by_path import load_by_path

ROOT = Path(__file__).resolve().parents[1]
HERE = ROOT / "bench" / "cross_tier_kernels"


def _harness():
    return load_by_path("cross_tier_kernels_harness", HERE / "harness.py")


# The native reference's checksums at n=5. They are the agreement oracle: an
# emitted tier that returns a different value fails the harness, and this pin
# makes a kernel edit that moves the shape visible in review.
_EXPECTED = {
    "schema_validate": 45,
    "regex_scan": 30,
    "vector_rerank": 6,
    "policy_eval": 23,
    "hash_bytes": 1295295841,
    "simulate": 9624,
    "batch_transform": 14199,
    "loop_probe": 15,
}


def test_the_kernels_compile_and_keep_the_pinned_checksums(tmp_path):
    h = _harness()
    out = tmp_path / "kernels.py"
    h._emit("python", out)
    spec = importlib.util.spec_from_file_location("cross_tier_pinned", out)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    got = {name: getattr(module, name)(5) for name in _EXPECTED}
    assert got == _EXPECTED


def test_the_table_writes_the_nine_columns_the_issue_names():
    h = _harness()
    base = {
        "n": 1000,
        "value": "1",
        "artifact_bytes": 1234,
        "cold_start_ms": 12.0,
        "peak_memory_kb": 2048,
        "loop_overhead_ns": 4.0,
        "capability_overhead_ns": None,
    }
    rows = [
        dict(base, kernel="schema_validate", tier="native", latency_ns_call=10.0,
             throughput_call_s=1e8, loop_overhead_ns=0.0),
        dict(base, kernel="schema_validate", tier="py", latency_ns_call=100.0,
             throughput_call_s=1e7, capability_overhead_ns=1824.0),
        dict(base, kernel="loop_probe", tier="native", latency_ns_call=1.0,
             throughput_call_s=1e9, loop_overhead_ns=0.0),
        dict(base, kernel="loop_probe", tier="py", latency_ns_call=50.0,
             throughput_call_s=2e7),
    ]
    table = h.render({"rows": rows, "n": {}, "skipped": []})
    for column in (
        "| kernel | tier | latency ns/call | throughput calls/s | "
        "artifact bytes | cold start ms | peak memory KB | loop overhead ns | "
        "capability overhead ns | n |"
    ).split("|"):
        if column.strip():
            assert column.strip() in table, column
    assert "schema_validate" in table and "n/a" in table


def test_a_disagreeing_or_failed_row_is_reported_not_measured():
    h = _harness()
    table = h.render({"rows": [
        {"kernel": "hash_bytes", "tier": "wasm",
         "error": "disagrees with native: 1 != 2"},
    ], "n": {}, "skipped": ["rust"]})
    assert "Failed rows" in table
    assert "disagrees with native" in table
    assert "Skipped tiers (not measured): rust." in table


def test_missing_toolchains_are_named_and_dropped(monkeypatch):
    h = _harness()
    monkeypatch.setattr(h.shutil, "which", lambda name: None)
    available, missing = h._have({"native", "py", "wasm"})
    assert available == {"py"}
    assert any("node" in m for m in missing) or any("wasm" in m for m in missing)
    assert any("cargo" in m for m in missing)


def test_the_native_floor_is_not_optional():
    """An emitted row without the floor has no checksum to agree with."""
    h = _harness()
    assert h._requested("rust,wasm") == {"rust", "wasm", "native"}
    assert h._requested("native") == {"native"}
    assert h._requested(None) == set(h.TIERS)


def test_a_partial_or_failed_run_cannot_overwrite_the_result():
    h = _harness()
    ok, reason = h._reportable({"py", "native"}, [])
    assert not ok and "missing" in reason
    ok, reason = h._reportable(set(h.TIERS), [{"error": "nope"}])
    assert not ok and "failed" in reason
    ok, _reason = h._reportable(set(h.TIERS), [])
    assert ok


def test_measure_refuses_emitted_rows_without_the_native_reference():
    h = _harness()
    with pytest.raises(RuntimeError, match="native reference is required"):
        h.measure({"py": h.Tier("py", Path("/nonexistent"))}, ns={}, samples=1)
