"""Smoke test for the cross-tier differential fuzzer (tools/fuzz_cross_tier.py,
roadmap item 292).

The fuzzer's own job is to *find* backend divergences; this test only pins that
the harness itself runs — generation, admissibility filtering, reference-value
extraction, the assertion oracle, and the report — end to end on a fixed seed,
without a crash, using the py reference tier ALONE. py needs no external
toolchain (compilation is pure Python and the py runtime carries pure `test`
blocks in-process), so this test is hermetic: it never depends on go/rust/wasm/
ts/java being installed, and never asserts a divergence a given box may or may
not be able to observe. The classifier, literal renderer, and generator
admissibility are unit-checked directly.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

import random  # noqa: E402

import fuzz_cross_tier as F  # noqa: E402
from revl import compile_source  # noqa: E402
from revl.errors import RevlError  # noqa: E402


def test_smoke_batch_completes_on_py_only():
    """A tiny fixed-seed batch on the py reference alone returns cleanly (0)
    and never raises. This is the 'the fuzzer runs' floor."""
    rc = F.main(["--seed", "7", "--count", "8", "--tiers", "py",
                 "--no-fixtures", "--quiet"])
    assert rc == 0


def test_smoke_is_deterministic_under_seed():
    """Same seed -> same generated programs (the --seed contract)."""
    def first_sources(seed, n=6):
        rng = random.Random(seed)
        out = []
        for _ in range(n):
            gen = F.Generator(random.Random(rng.random()))
            out.append(gen.program().render())
        return out
    assert first_sources(3) == first_sources(3)
    assert first_sources(3) != first_sources(4)


def test_generator_is_mostly_admissible():
    """A type-directed generator should produce admissible programs the large
    majority of the time; the few the frontend rejects are DISCARDED, never
    counted as divergences."""
    rng = random.Random(11)
    admitted = total = 0
    for _ in range(120):
        gen = F.Generator(random.Random(rng.random()))
        src = gen.program().render()
        total += 1
        try:
            compile_source(src)
            admitted += 1
        except RevlError:
            pass
    assert total == 120
    assert admitted >= 108, f"only {admitted}/120 admitted"


def test_reference_value_matches_known_program():
    """The py reference-value extraction returns the true value of probe()."""
    assert F.reference_value(
        "pub fn probe() -> Int { return 2 + 3 * 4 }") == 14
    assert F.reference_value(
        'pub fn probe() -> Str { return "he" + "llo" }') == "hello"
    assert F.reference_value(
        "pub fn probe() -> Opt[Int] { return None }") is None


def test_render_literal_round_trips_each_type():
    """A rendered literal, asserted against probe() on the py tier, passes —
    i.e. the reference is self-consistent (a mis-render would be a harness bug,
    not a divergence)."""
    cases = [
        ("Int", "pub fn probe() -> Int { return 0 - 5 }"),
        ("Float", "pub fn probe() -> Float { return 3.5 }"),
        ("Bool", "pub fn probe() -> Bool { return 3 > 2 }"),
        ("Str", 'pub fn probe() -> Str { return "abc" }'),
        (("List", "Int"), "pub fn probe() -> List[Int] { return [1, 2, 3] }"),
        (("Opt", "Int"), "pub fn probe() -> Opt[Int] { return Some(7) }"),
        (("Opt", "Int"), "pub fn probe() -> Opt[Int] { return None }"),
        (("Result", "Int", "Str"),
         'pub fn probe() -> Result[Int, Str] { return Err("no") }'),
    ]
    for ret, src in cases:
        value = F.reference_value(src)
        aug = F.assertion_source(src, ret, value)
        assert aug is not None
        outcome, _msg, _out = F._run_tier(F.REFERENCE, compile_source(aug))
        assert outcome == "pass", f"reference disagreed with its own literal: {src}"


def test_classify_separates_refusal_build_value():
    """The honesty core: an emitter refusal is a declared capability boundary
    (never a divergence); a toolchain build error is a build divergence; a
    ran-and-failed assertion is a value divergence."""
    assert F.classify("emitter refused: type 'Float' is not lowerable", "") == "refusal"
    assert F.classify(
        "go test exited 1",
        "FAIL\trevltest [build failed]\n./gen_test.go:34:9: undefined: None") == "build"
    assert F.classify(
        "1 of 1 test(s) failed",
        "FAIL cross_tier_probe: Invalid input WebAssembly code: type mismatch") == "build"
    # a genuine value divergence: it built and ran, an assertion returned false
    assert F.classify(
        "go test exited 1",
        "--- FAIL: TestProbe\n    assertion probe() == 3 was false") == "value"


def test_divergence_signature_is_stable_and_specific():
    """Distinct root causes get distinct fingerprints (so distinct fixtures),
    and line numbers / offsets are normalized away for stability."""
    go_none = F.divergence_signature(
        "go test exited 1",
        "# revltest\n./gen_test.go:34:9: undefined: None")
    go_iface = F.divergence_signature(
        "go test exited 1",
        "./gen_test.go:22:15: s (variable of struct type Adt0C0_0) is not an interface")
    assert go_none == "undefined: None"
    assert "is not an interface" in go_iface
    assert go_none != go_iface
