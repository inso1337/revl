#!/usr/bin/env python3
"""Admission-latency benchmark (roadmap item 16).

revl's admission gate runs *inside* an agent loop: for every candidate
component the agent drafts, `compile_source(..., manifest=running)` compiles
the source and admits it against the running composition in one in-memory
round-trip. That per-candidate cost is therefore a product property — "admission
costs X ms" is part of the pitch — not an implementation detail. This script
measures it and pins a number a regression would move.

What is timed is the *in-memory* round-trip only: parse + check/lower the
candidate + link it against the running manifest + refuse_admission. There is
NO disk I/O in the timed section (sources are strings; `compile_source` reads
and writes nothing), no network and no toolchain. The running composition is
compiled once up front, outside the timer — an agent holds it in memory across
generations, so re-deriving it per candidate would not reflect the real loop.

Usage:
  python3 bench/admission_latency.py                 # warm run, print figures
  python3 bench/admission_latency.py --iters 5000    # more samples
  python3 bench/admission_latency.py --write          # refresh results/*.md

The scenario (RUNNING / CANDIDATE below) and `measure()` are imported by
tests/test_admission_latency.py, so the guard test exercises the same code.
"""

from __future__ import annotations

import argparse
import platform
import statistics
import sys
import time
from pathlib import Path

BENCH = Path(__file__).resolve().parent
ROOT = BENCH.parent
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402

# --- the representative scenario -------------------------------------------
#
# A running composition an agent would be evolving: `Store` is a host-backed
# key/value interface provided by `Kv`, and `App` already consumes it. This is
# a real graph — a provider, a consumer, an effect with a matching undo — not a
# trivial empty component, so admitting against it exercises the gate for real:
# ambient services in scope without redeclaration, G2 (every `provides`/`requires`
# key resolves), and G3 (the dependency graph across running + candidate stays a
# DAG). It mirrors the acceptance-benchmark specs (e.g. 13-provider-consumer-pair)
# without needing a model to generate one.
RUNNING = """
service Store {
  fn get(key: Str) -> Str
  fn bump(n: Int) -> Int
  emission fn put(key: Str, value: Str)
}
service AppSvc { fn ping() -> Str }

component Kv provides store: Store {
  let m = effect Map.new() undo m.drop()
  provide store {
    fn get(key) = key
    fn bump(n) = n
    fn put(key, value) = value
  }
}
component App requires store: Store provides app: AppSvc {
  provide app { fn ping() = store.get("boot") }
}
"""

# The candidate the agent drafts and asks the gate to admit: a NEW component
# that *requires* the running `Store` (resolved against the ambient provider Kv,
# never redeclared) and *provides* a new `Cache` interface. Admitting it makes
# the gate resolve a requires against the running composition and check the new
# provides — the requires/provides + G-check path the roadmap item is about.
CANDIDATE = """
service Cache { fn lookup(key: Str) -> Str }
component CacheLayer requires store: Store provides cache: Cache {
  provide cache { fn lookup(key) = store.get(key) }
}
"""

# A standalone-valid twin of the candidate (Store inlined) that type-checks with
# NO manifest gate. Compiling it isolates the parse+check/lower "compile" cost so
# the gate's share of the round-trip can be shown. It carries one extra service
# declaration, so it slightly *over*-states compile and thus *under*-states the
# gate delta — the split is a guide, not a second headline figure.
CANDIDATE_STANDALONE = """
service Store {
  fn get(key: Str) -> Str
  fn bump(n: Int) -> Int
  emission fn put(key: Str, value: Str)
}
service Cache { fn lookup(key: Str) -> Str }
component CacheLayer requires store: Store provides cache: Cache {
  provide cache { fn lookup(key) = store.get(key) }
}
"""

WARMUP = 100


def build_running() -> dict:
    """Compile the running composition once (an agent keeps this in memory)."""
    return compile_source(RUNNING, "base.rvl")


def admit_once(running: dict) -> dict:
    """One in-memory compile+admit round-trip. Returns the admitted document."""
    return compile_source(CANDIDATE, "cand.rvl", manifest=running)


def _time_ms(fn, iters: int) -> list[float]:
    samples = []
    for _ in range(iters):
        t0 = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - t0) * 1000.0)
    samples.sort()
    return samples


def _stats(samples: list[float]) -> dict:
    n = len(samples)
    return {
        "n": n,
        "median": statistics.median(samples),
        "p90": samples[min(n - 1, int(n * 0.90))],
        "p99": samples[min(n - 1, int(n * 0.99))],
        "min": samples[0],
        "mean": statistics.fmean(samples),
    }


def measure(iters: int = 2000, warmup: int = WARMUP) -> dict:
    """Warm the caches (excluding first-call import cost), then time the
    round-trip and the compile-only baseline. Returns a dict of stats."""
    running = build_running()

    # Warm: first calls pay one-time import/JIT-ish costs we do not want in the
    # distribution. The warm-up result is also a correctness smoke check.
    warm = admit_once(running)
    assert [c["name"] for c in warm["components"]] == ["CacheLayer"], warm
    for _ in range(warmup):
        admit_once(running)
        compile_source(CANDIDATE_STANDALONE, "cand.rvl")

    round_trip = _time_ms(lambda: admit_once(running), iters)
    compile_only = _time_ms(
        lambda: compile_source(CANDIDATE_STANDALONE, "cand.rvl"), iters
    )
    return {
        "round_trip": _stats(round_trip),
        "compile_only": _stats(compile_only),
    }


def _machine() -> str:
    cpu = platform.processor() or platform.machine()
    if platform.system() == "Darwin":
        try:
            import subprocess
            brand = subprocess.run(
                ["sysctl", "-n", "machdep.cpu.brand_string"],
                capture_output=True, text=True, timeout=5,
            ).stdout.strip()
            if brand:
                cpu = brand
        except Exception:
            pass
    return (f"{platform.system()} {platform.release()} · {cpu} · "
            f"Python {platform.python_version()}")


def render_md(res: dict) -> str:
    rt, co = res["round_trip"], res["compile_only"]
    gate = rt["median"] - co["median"]
    return f"""# Admission latency

One honestly-measured number for revl's in-memory admission round-trip
(roadmap item 16). The gate runs per candidate component, per generation,
inside an agent loop, so this is the per-candidate cost of `compile_source(...,
manifest=running)`: parse + check/lower the candidate + link it against the
running composition + `refuse_admission`. No disk I/O, network, or toolchain is
in the timed section.

## Result

| measurement | median | p90 | p99 | min | samples |
|---|---|---|---|---|---|
| **compile + admit (round-trip)** | **{rt['median']:.3f} ms** | {rt['p90']:.3f} ms | {rt['p99']:.3f} ms | {rt['min']:.3f} ms | {rt['n']} |
| compile only (no gate, baseline) | {co['median']:.3f} ms | {co['p90']:.3f} ms | {co['p99']:.3f} ms | {co['min']:.3f} ms | {co['n']} |

**Admission costs ≈ {rt['median']:.2f} ms** (median) per candidate component on
the machine below. The gate itself — linking the candidate against the running
composition and refusing holes — adds ≈ {gate:.3f} ms over compiling the
component alone (the "compile only" row is a standalone-valid twin with `Store`
inlined; it carries one extra service decl, so it slightly over-states compile
and under-states the gate share — treat the split as a guide, the round-trip as
the headline).

## Methodology

- **Machine:** {_machine()}
- **Scenario:** running composition = `Store` provider `Kv` + consumer `App`;
  candidate = `CacheLayer` that *requires* the running `Store` and *provides* a
  new `Cache`. Requires/provides + G2/G3 across running and candidate — the real
  gate, not an empty component. Defined in `bench/admission_latency.py`.
- **Warm:** the running composition is compiled once outside the timer (an agent
  holds it across generations); {WARMUP} warm-up round-trips precede timing so
  one-time import cost is excluded.
- **Distribution:** {rt['n']} timed iterations; median/p90/p99 reported, not a
  single lucky sample.
- **Determinism:** pure `compile_source` on in-memory strings — no disk I/O in
  the timed section, no network, no toolchain.

## Re-run

```
python3 bench/admission_latency.py            # print the figures
python3 bench/admission_latency.py --write    # rewrite this file
```

A guard test, `tests/test_admission_latency.py`, runs a small iteration count in
CI and asserts the round-trip completes and returns an admitted result (it does
*not* assert a hard wall-clock number — machines vary — only a generous
order-of-magnitude ceiling that a ~10x regression would trip).
"""


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--iters", type=int, default=2000,
                    help="timed iterations per measurement (default 2000)")
    ap.add_argument("--warmup", type=int, default=WARMUP,
                    help=f"warm-up iterations (default {WARMUP})")
    ap.add_argument("--write", action="store_true",
                    help="rewrite bench/results/admission-latency.md")
    args = ap.parse_args()

    res = measure(iters=args.iters, warmup=args.warmup)
    rt, co = res["round_trip"], res["compile_only"]
    print(_machine())
    print(f"compile + admit : median {rt['median']:.3f} ms  "
          f"p90 {rt['p90']:.3f} ms  p99 {rt['p99']:.3f} ms  "
          f"min {rt['min']:.3f} ms  (n={rt['n']})")
    print(f"compile only    : median {co['median']:.3f} ms  "
          f"p90 {co['p90']:.3f} ms  (n={co['n']})")
    print(f"gate share      : ~{rt['median'] - co['median']:.3f} ms (median delta)")

    if args.write:
        out = BENCH / "results" / "admission-latency.md"
        out.write_text(render_md(res))
        print(f"\nwrote {out}")


if __name__ == "__main__":
    main()
