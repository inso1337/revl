#!/usr/bin/env python3
"""Host extern call cost on the Python tier (issue #2709, first slice).

The codegen, self-host and admission benchmarks all time something else: the
emitted code against handwritten code (bench/codegen/), the compiler against
itself (docs/bench-selfhost.md), compile-plus-admit for one candidate
(bench/admission_latency.py). None of them times a call ACROSS a host
boundary, so "one extern call costs X" was a number nobody could state.

This measures the first tier only: the cordis-py reference tier. It compiles
one program with three externs, emits it, activates the component on a real
cordis Context, and calls a provide method in a loop. The same host body is
called directly beside it, so the difference is the crossing and not the
body.

What is on the path, and how each part is isolated:

* direct body          the host body as a plain Python function: the floor.
* pure extern          `twice`, emitted: a plain call, no crossing.
* latch path read      `estop_latch_path`, the environment read the gate makes.
* E-Stop gate          `_estop_check`, which the emitter wraps around every
                        boundary-crossing extern (class emission/witnessed/
                        acquire). Timed on its own and again inside the call.
* witnessed extern     `note`, a `witnessed[fs]` extern: gate plus body.
* session entry        `Frame.transactional_method`, the bookkeeping one
                        witnessed effect adds: a `_Transactional` entry
                        appended to the frame. The row INCLUDES the gate the
                        method itself checks first, so the entry alone is that
                        row minus the gate row.
* provide method       `bump`, which fires the witnessed extern and then the
                        pure extern: the full per-call crossing cost.

What could not be isolated: the CAPABILITY check. `witnessed[fs]` is a
compile-time declaration (G4), so there is no per-call runtime authorization
to switch off; what a call pays is the E-Stop gate and the session entry.
The AUDIT RECORD is not isolated either: `Frame._wal()` is None unless the
run is under `revl run --record`/`--wal`, and neither measured arm attaches a
recorder, so `record_discharge_descriptor` is not on this path. Measuring it
needs the recorder attached and is left to the next slice, with the
cross-tier harness (TypeScript, Rust, wasm) the issue asks for after this one.

Requires the cordis-py runtime the reference tier is built on:

  sh backends/python/setup.sh          # once, in a checkout you can write to

Usage:
  python3 bench/extern_call_cost.py                  # warm run, print the table
  python3 bench/extern_call_cost.py --iters 50000    # more calls per sample
  python3 bench/extern_call_cost.py --write          # refresh results/extern-call-cost.md

The program and `measure()` are importable, so a test can exercise the same
code path.
"""

from __future__ import annotations

import argparse
import asyncio
import gc
import importlib.util
import platform
import statistics
import sys
import time
import types
from pathlib import Path

BENCH = Path(__file__).resolve().parent
ROOT = BENCH.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "backends" / "python"))

RESULT = BENCH / "results" / "extern-call-cost.md"

#: One purpose-built program: a pure extern, a capability-bearing
#: (`witnessed[fs]`) extern, and a provide method that fires both, so the
#: method arm and the two direct arms share the same host bodies.
SOURCE = """\
extern pure fn twice(x: Int) -> Int = @py { return x * 2 }

extern witnessed[fs] fn note(p: Str) -> Result[Str, Str] undo unnote(result) = @py { return Ok("n") }
extern pure fn unnote(w: Str) -> Unit = @py { return }

service Ledger { emission fn bump(x: Int) -> Int }
component LedgerC provides ledger: Ledger {
  provide ledger {
    fn bump(x) {
      effect note("x")
      return twice(x)
    }
  }
}
"""

ARMS = ("direct body (pure)", "pure extern",
        "direct body (witnessed)", "latch path read", "E-Stop gate",
        "witnessed extern", "session entry (with its gate)", "provide method")


def _load_module():
    """The emitted module for SOURCE. Compiling and emitting are outside every
    timer: this measures calls, not the compiler."""
    from revl.compiler import compile_source  # noqa: PLC0415
    import emit  # noqa: PLC0415

    ir = compile_source(SOURCE, "<extern-call-cost>.rvl")
    module = types.ModuleType("extern_call_cost_emitted")
    exec(compile(emit.emit(ir), "extern_call_cost_emitted.py", "exec"),
         module.__dict__)
    return module


async def _flush() -> None:
    for _ in range(20):
        await asyncio.sleep(0)


def _body():
    """The two host bodies as plain functions: the "called directly" side of
    the comparison, one per extern. `pure` returns an Int, the witnessed body
    an `Ok` carrying a Str."""
    import runtime  # noqa: PLC0415

    return (lambda x: x * 2), (lambda p: runtime.Ok("n"))


async def _crossing(module, n: int) -> int:
    """Time `n` provide-method calls on one activation, and return the
    nanoseconds they took. Activation and disposal are outside the timer."""
    from cordis import Context  # noqa: PLC0415

    root = Context()
    fiber = root.plugin(module.LedgerC)
    await fiber
    await _flush()
    ledger = root.get("ledger")
    elapsed = _time_ns(lambda: ledger.bump(1), n)
    await fiber.dispose()
    await _flush()
    return elapsed


def _time_ns(call, n: int) -> int:
    """CPU nanoseconds for `n` calls, with cyclic garbage kept out of the
    timer: the observed call path allocates (an `Ok`, a `_Transactional`, a
    per-call lambda at the emitted site), and a collection landing inside one
    arm's loop would land in that arm and no other."""
    gc.collect()
    gc.disable()
    try:
        t0 = time.process_time_ns()
        for _ in range(n):
            call()
        return time.process_time_ns() - t0
    finally:
        gc.enable()
        gc.collect()


def measure(iters: int = 20_000, samples: int = 7) -> dict:
    """`{arm: median nanoseconds per call}`, each sample's own empty loop
    subtracted. Each sample re-runs the entry and method arms on a fresh
    frame, so the entries they accumulate are collected and dropped before the
    next sample starts."""
    if iters < 1 or samples < 1:
        raise ValueError("iters and samples must be at least 1")
    return asyncio.run(_measure(iters, samples))


async def _measure(iters: int, samples: int) -> dict:
    module = _load_module()
    pure_body, witnessed_body = _body()
    undo = lambda result: module.unnote(result)  # noqa: E731 - the emitted shape
    noop = lambda: None  # noqa: E731

    import runtime  # noqa: PLC0415

    calls = {
        "direct body (pure)": lambda: pure_body(2),
        "pure extern": lambda: module.twice(2),
        "direct body (witnessed)": lambda: witnessed_body("x"),
        "latch path read": lambda: runtime.estop_latch_path(),
        "E-Stop gate": lambda: runtime._estop_check("note"),
        "witnessed extern": lambda: module.note("x"),
    }

    per = {arm: [] for arm in ARMS}
    loop = []
    for _ in range(samples):
        # each sample subtracts its own empty loop, so a slow sample cannot
        # push one arm and not another
        baseline = _time_ns(noop, iters) / iters
        loop.append(baseline)
        for arm, call in calls.items():
            per[arm].append(_time_ns(call, iters) / iters - baseline)
        # a fresh frame per sample: the entries it accumulates are what the
        # session-entry arm measures, and they are collected before the next
        frame = runtime.Frame(None, "bench")   # no ctx: _wal() is None, no recorder
        per["session entry (with its gate)"].append(
            _time_ns(lambda: frame.transactional_method(
                undo, "n", scope={"caps": ["fs"]}), iters) / iters - baseline)
        per["provide method"].append(
            (await _crossing(module, iters)) / iters - baseline)

    out = {arm: statistics.median(per[arm]) for arm in ARMS}
    out["_loop"] = statistics.median(loop)
    out["_iters"] = iters
    out["_samples"] = samples
    return out


def render(m: dict) -> str:
    pure = m["direct body (pure)"]
    witnessed = m["direct body (witnessed)"]
    lines = [
        "| call | median CPU ns/call |",
        "|---|---:|",
    ]
    for arm in ARMS:
        lines.append(f"| {arm} | {m[arm]:.0f} |")
    lines.append("")
    lines.append(
        f"CPU time per call, empty loop subtracted ({m['_loop']:.0f} ns/call). "
        f"{m['_samples']} samples x {m['_iters']:,} calls per arm.")
    lines.append("")
    entry = m["session entry (with its gate)"]
    gate = m["E-Stop gate"]
    crossing = m["provide method"] - witnessed
    # the provide method pays the gate twice: once in the witnessed
    # wrapper (inside the witnessed-extern row) and once in the registration
    # (inside the session-entry row), which is why the entry row is added whole
    parts = (m["witnessed extern"] - witnessed) + entry + m["pure extern"]
    lines.append(
        "What the arms say: the pure extern costs "
        f"{m['pure extern'] - pure:.0f} ns over its body, so a pure call is the "
        "same call the host would make. The boundary crossing costs "
        f"{m['witnessed extern'] - witnessed:.0f} ns over the witnessed body "
        f"(the E-Stop gate alone is {gate:.0f} ns, of which the latch-path "
        f"read is {m['latch path read']:.0f} ns). The session entry row is "
        f"{entry:.0f} ns and includes the gate its own registration checks, so "
        f"the entry alone is about {entry - gate:.0f} ns. The full "
        f"provide-method crossing is {crossing:.0f} ns over the body "
        f"({crossing / max(witnessed, 1):.1f}x the body). It pays the E-Stop "
        "gate twice, once in the witnessed wrapper and once in the "
        "registration, and adds the session entry and the pure call; those "
        f"parts sum to about {parts:.0f} ns while the crossing measures "
        f"{crossing:.0f} ns, so the {crossing - parts:.0f} ns residual is the "
        "allocation at the emitted call site (two lambdas, a scope dict and a "
        "call dict per call) plus timing noise, not a part the table can "
        "name. The "
        "capability check is compile-time (G4) and has no per-call arm; the "
        "WAL discharge record (the audit record) is not on this path either: "
        "no arm attaches a recorder, so `Frame._wal()` is None and no "
        "descriptor is written.")
    return "\n".join(lines)


def _environment() -> str:
    try:
        import cordis  # noqa: PLC0415

        cordis_version = getattr(cordis, "__version__", "unknown")
    except ImportError:  # pragma: no cover - guarded by main()
        cordis_version = "missing"
    return (f"Python {platform.python_version()} on {platform.system()} "
            f"{platform.release()} ({platform.machine()}), cordis-py "
            f"{cordis_version}.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--iters", type=int, default=20_000,
                        help="calls per sample per arm (default 20000)")
    parser.add_argument("--samples", type=int, default=7,
                        help="samples per arm, median taken (default 7)")
    parser.add_argument("--write", action="store_true",
                        help=f"write {RESULT.relative_to(ROOT)}")
    args = parser.parse_args(argv)
    if args.iters < 1:
        parser.error("--iters must be at least 1")
    if args.samples < 1:
        parser.error("--samples must be at least 1")

    if importlib.util.find_spec("cordis") is None:
        print("SKIP: the Python tier needs cordis-py. Install it with "
              "`sh backends/python/setup.sh` (see backends/python/README.md).",
              file=sys.stderr)
        return 2
    import runtime  # noqa: PLC0415

    if runtime.estop_latch_path():
        print("SKIP: an E-Stop latch is armed, so every crossing raises. "
              "Clear REVL_ESTOP_LATCH (or the latch file) and re-run.",
              file=sys.stderr)
        return 2

    m = measure(iters=args.iters, samples=args.samples)
    table = render(m)
    print(table)
    if args.write:
        RESULT.parent.mkdir(parents=True, exist_ok=True)
        RESULT.write_text(
            "# Host extern call cost, Python tier (issue #2709)\n\n"
            f"Generated by `python3 bench/extern_call_cost.py --write`. "
            f"{_environment()}\n\n"
            "CPU time per call (`time.process_time_ns`), so a machine busy with\n"
            "other work moves wall time and not this table. Reproduce with:\n\n"
            "```sh\nsh backends/python/setup.sh\n"
            "python3 bench/extern_call_cost.py --write\n```\n\n"
            f"{table}\n",
            encoding="utf-8")
        print(f"\nwrote {RESULT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
