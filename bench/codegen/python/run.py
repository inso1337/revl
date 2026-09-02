#!/usr/bin/env python3
"""Codegen performance harness for the cordis-py backend.

WHAT THIS MEASURES. Not "is revl fast" but "how much of the work the emitted
program does is work the emitter chose". Every case pairs one revl function,
compiled through `backends/python/emit.py`, with the function a competent
Python developer would write by hand for the SAME semantics
(`handwritten.py`). The gap between the two is the emitter's overhead.

NO TIMINGS WERE TAKEN. The audit that produced this file ran on a machine with
a dozen other agents on it. Under that contention even an interleaved A/B ratio
is noise, because the two arms sample different load, so the audit reported no
duration-based number at all. Every default metric below is DETERMINISTIC: it
is a property of the program, identical on every run, on any host, at any load.

  ops     executed bytecode instructions (sys.settrace with f_trace_opcodes).
  calls   Python-level function calls (sys.setprofile 'call' events). Isolates
          per-evaluation helper and lambda construction and invocation.
  copies  container ELEMENTS moved, via copycount.py's AST instrumentation.
          `ops` is blind to C-level copying (`out + [x]` is one instruction
          that copies len(out) pointers); this is the metric that sees it, and
          it is what separates an O(n) accumulation from an O(n^2) one.
  bytes   tracemalloc peak. Corroborates `copies` on live-set growth.

    python bench/codegen/python/run.py            # everything deterministic
    python bench/codegen/python/run.py --case arith
    python bench/codegen/python/run.py --dump maps   # the emitted Python

THE TIMING PASS, LATER, ON A QUIET MACHINE. Three opt-in modes exist and were
deliberately NOT run. Run them only where nothing else is competing for CPU,
and treat their output as the only place in this harness where a duration
appears:

    python bench/codegen/python/run.py --time     # interleaved A/B ratio
    python bench/codegen/python/run.py --scale    # growth exponent, log2
    python bench/codegen/python/run.py --curve    # E/H at a series of sizes
"""

from __future__ import annotations

import argparse
import gc
import importlib.util
import math
import os
import statistics
import sys
import time
import tracemalloc
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
PROGRAMS = HERE / "programs"

sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "backends" / "python"))

import copycount  # noqa: E402


def _load_emitted(name: str) -> types.ModuleType:
    """Compile `programs/<name>.rvl` with revl, emit Python, exec the module."""
    from revl import compile_files  # noqa: PLC0415

    import emit  # noqa: PLC0415

    cwd = Path.cwd()
    os.chdir(ROOT)
    try:
        doc = compile_files([str(PROGRAMS / f"{name}.rvl")])
        src = emit.emit(doc)
    finally:
        os.chdir(cwd)
    mod = types.ModuleType(f"emitted_{name}")
    mod.__dict__["__source__"] = src
    mod.__dict__["CALLS"] = 0  # nullish.rvl's extern counts into this
    # `@dataclass` resolves annotations through `sys.modules[cls.__module__]`,
    # so an exec'd module has to be registered before the body runs.
    sys.modules[mod.__name__] = mod
    exec(compile(src, f"<emitted {name}.rvl>", "exec"), mod.__dict__)
    return mod


def _instrument_emitted(name: str, plain: types.ModuleType) -> types.ModuleType:
    return copycount.instrument(plain.__dict__["__source__"], f"rc_{name}")


def _instrument_handwritten() -> types.ModuleType:
    return copycount.instrument((HERE / "handwritten.py").read_text(), "rc_hw")


def _load_handwritten() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(
        "handwritten", HERE / "handwritten.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --------------------------------------------------------------------- metrics
def count_ops(fn) -> int:
    """Executed bytecode instructions. Deterministic; load-independent."""
    n = 0

    def local(frame, event, arg):
        nonlocal n
        if event == "opcode":
            n += 1
        return local

    def top(frame, event, arg):
        if event == "call":
            # Both, in this order: `f_trace_opcodes` alone is a no-op on a
            # frame that has no local trace function installed, which silently
            # under-counts every leaf frame.
            frame.f_trace = local
            frame.f_trace_opcodes = True
            return local
        return None

    sys.settrace(top)
    try:
        fn()
    finally:
        sys.settrace(None)
    return n


def count_calls(fn) -> int:
    """Python-level function calls (a lambda built and called counts one)."""
    n = 0

    def prof(frame, event, arg):
        nonlocal n
        if event == "call":
            n += 1

    sys.setprofile(prof)
    try:
        fn()
    finally:
        sys.setprofile(None)
    return n


def peak_bytes(fn) -> int:
    tracemalloc.start()
    try:
        fn()
        _cur, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    return peak


def ab_ratio(emitted, hand, repeats=7, inner=3):
    """Interleaved A/B wall clock. Returns (median ratio, min, max).

    The two runs alternate inside one process so that a load spike hits both
    arms, and only the RATIO is reported: an absolute millisecond figure from a
    loaded machine is meaningless, a ratio measured back to back is not.
    """
    ratios = []
    for _ in range(repeats):
        te = th = 0.0
        for _ in range(inner):
            t0 = time.perf_counter()
            emitted()
            t1 = time.perf_counter()
            hand()
            t2 = time.perf_counter()
            te += t1 - t0
            th += t2 - t1
        ratios.append(te / th if th else float("nan"))
    return statistics.median(ratios), min(ratios), max(ratios)


# ----------------------------------------------------------------------- cases
PROGRAM_ORDER = ("list_build", "transforms", "arith", "divmod",
                 "records", "maps", "strings", "matching")


def cases(em, hw):
    """(case, program, small workload, large workload, note) tuples.

    `small` drives the deterministic metrics (the opcode tracer is ~100x slow),
    `large` drives the A/B wall clock. Both call the identically named function
    in the emitted module and in handwritten.py.
    """
    ints = list(range(400))
    big_ints = list(range(4000))
    pts = [{"x": i, "y": i * 2} for i in range(400)]
    big_pts = [{"x": i, "y": i * 2} for i in range(4000)]
    keys = [f"k{i:04d}" for i in range(400)]
    big_keys = [f"k{i:04d}" for i in range(2000)]
    words = [f"w{(i * 7919) % 997:04d}" for i in range(200)]
    hay = "abcdefghij" * 40
    opts = [i if i % 3 else None for i in range(400)]
    big_opts = [i if i % 3 else None for i in range(4000)]

    def pair(mod, fn, *args):
        # Bound outside the measured region: the thunk is exactly one call.
        f = getattr(mod, fn)
        return lambda: f(*args)

    def both(fn, small_args, big_args, note, hand_fn=None):
        h = hand_fn or fn
        return (fn, note,
                pair(em, fn, *small_args), pair(hw, h, *small_args),
                pair(em, fn, *big_args), pair(hw, h, *big_args))

    def only(entries):
        # Lazily built: `cases()` describes every program, but resolving a name
        # against the wrong emitted module must not raise.
        return lambda: [both(*e) for e in entries]

    return {
        "list_build": only([
            ("build", (400,), (4000,), "`out = out.push(i)` in a loop"),
        ]),
        "transforms": only([
            ("doubled", (ints,), (big_ints,), "xs.map(f)"),
            ("evens", (ints,), (big_ints,), "xs.filter(p) with `%`"),
            ("total_reduce", (ints,), (big_ints,), "xs.reduce(0, f)"),
            ("pipeline", (ints,), (big_ints,), "map -> filter -> reduce"),
            ("list_sort", (words,), (words,),
             "stdlib list_sort (quadratic by design)"),
        ]),
        "arith": only([
            ("sum_squares", (2000,), (60000,),
             "bounded Int + - * in a while loop"),
        ]),
        "divmod": only([
            ("churn", (1000,), (30000,), "`%` / div_trunc / div_euclid"),
        ]),
        "records": only([
            ("total", (pts,), (big_pts,), "record field reads"),
            ("shift_all", (pts, 3), (big_pts, 3),
             "functional record update in a loop"),
        ]),
        "maps": only([
            ("fill", (keys,), (big_keys,), "Map.set in a loop"),
            ("drop_keys", ({k: 1 for k in keys}, keys[:100]),
             ({k: 1 for k in big_keys}, big_keys[:200]),
             "Map.remove in a loop"),
            ("probe", ({k: 1 for k in keys}, keys),
             ({k: 1 for k in big_keys}, big_keys),
             "m.lookup(k) ?? 0"),
        ]),
        "strings": only([
            ("scan", (hay, "hij", 500), (hay, "hij", 20000), "Str.indexOf"),
            ("split_join", (hay, 200), (hay, 5000), "Str.split"),
        ]),
        "matching": only([
            ("sum_present", (opts,), (big_opts,),
             "match Some/None in a loop"),
            ("classify", (7,), (7,), "match with a wildcard arm"),
        ]),
    }


# ------------------------------------------------------------- complexity
# The opcode counter measures INTERPRETER work, and it under-reports anything
# the interpreter hands to C in one instruction: `out + [x]` is a single
# BINARY_OP that copies `len(out)` pointers, so a quadratic push loop and a
# linear append loop have almost the same opcode count. The growth exponent
# closes that gap: time the same arm at n and 2n and take log2 of the ratio.
# It is a ratio WITHIN one arm, measured back to back, so it survives load far
# better than any absolute figure, and it is the measurement that separates
# O(n) from O(n^2).
def growth(make, n, repeats=9):
    small, big = make(n), make(2 * n)
    small()
    big()  # warm up: first-touch page faults are not the signal
    best_s = best_b = float("inf")
    # The cyclic collector is off for the measurement. Its cost is real and the
    # emitted arm pays more of it (a persistent push allocates one fresh
    # container per step), but it scales with the whole heap and would inflate
    # the exponent past the algorithmic one. Measured with it off, the exponent
    # is the algorithm's.
    gc_was_on = gc.isenabled()
    gc.disable()
    for _ in range(repeats):
        t0 = time.perf_counter()
        small()
        t1 = time.perf_counter()
        big()
        t2 = time.perf_counter()
        # MIN, not mean: on a loaded machine the minimum is the run that was
        # least preempted, and it is the only statistic that converges to the
        # true cost from above as contention rises.
        best_s = min(best_s, t1 - t0)
        best_b = min(best_b, t2 - t1)
    if gc_was_on:
        gc.enable()
    if best_s <= 0 or best_b <= 0:
        return float("nan")
    return math.log2(best_b / best_s)


def scale_probes(em, hw):
    """(label, base n, emitted factory, hand-written factory)."""
    def keys(n):
        return [f"k{i:06d}" for i in range(n)]

    def words(n):
        return [f"w{(i * 7919) % 99991:06d}" for i in range(n)]

    return [
        ("list_build.build", 2000,
         lambda n: (lambda: em["list_build"].build(n)),
         lambda n: (lambda: hw.build(n))),
        ("transforms.doubled", 2000,
         lambda n: (lambda xs=list(range(n)): em["transforms"].doubled(xs)),
         lambda n: (lambda xs=list(range(n)): hw.doubled(xs))),
        ("transforms.list_sort", 200,
         lambda n: (lambda xs=words(n): em["transforms"].list_sort(xs)),
         lambda n: (lambda xs=words(n): hw.list_sort(xs))),
        ("maps.fill", 1000,
         lambda n: (lambda ks=keys(n): em["maps"].fill(ks)),
         lambda n: (lambda ks=keys(n): hw.fill(ks))),
        ("maps.drop_keys", 500,
         lambda n: (lambda m={k: 1 for k in keys(n)}, ks=keys(n):
                    em["maps"].drop_keys(m, ks)),
         lambda n: (lambda m={k: 1 for k in keys(n)}, ks=keys(n):
                    hw.drop_keys(m, ks))),
        ("records.shift_all", 2000,
         lambda n: (lambda ps=[{"x": i, "y": i} for i in range(n)]:
                    em["records"].shift_all(ps, 1)),
         lambda n: (lambda ps=[{"x": i, "y": i} for i in range(n)]:
                    hw.shift_all(ps, 1))),
    ]


def curve(mk_e, mk_h, sizes):
    """E/H at a series of sizes. A ratio that GROWS with n is the signature of
    an emitter-introduced complexity class change, and it is readable even on a
    loaded machine because both arms are timed back to back at each size."""
    rows = []
    gc_was_on = gc.isenabled()
    gc.disable()
    for n in sizes:
        e, h = mk_e(n), mk_h(n)
        e()
        h()
        be = bh = float("inf")
        for _ in range(9):
            t0 = time.perf_counter()
            e()
            t1 = time.perf_counter()
            h()
            t2 = time.perf_counter()
            be = min(be, t1 - t0)
            bh = min(bh, t2 - t1)
        rows.append((n, be, bh))
    if gc_was_on:
        gc.enable()
    return rows


# ---------------------------------------------------------- element copies
# Deterministic, and the only metric here that sees C-level copying. Reported
# at n and 2n: a count that quadruples when n doubles is O(n^2) with an O(n)
# spelling sitting right next to it in handwritten.py.
COPY_PROBES = [
    ("list_build.build", "build", 500,
     lambda n: (n,)),
    ("transforms.doubled", "doubled", 500,
     lambda n: (list(range(n)),)),
    ("transforms.list_sort", "list_sort", 100,
     lambda n: ([f"w{(i * 7919) % 99991:06d}" for i in range(n)],)),
    ("maps.fill", "fill", 500,
     lambda n: ([f"k{i:06d}" for i in range(n)],)),
    ("maps.drop_keys", "drop_keys", 500,
     lambda n: ({f"k{i:06d}": 1 for i in range(n)},
                [f"k{i:06d}" for i in range(n)])),
    ("records.shift_all", "shift_all", 500,
     lambda n: ([{"x": i, "y": i} for i in range(n)], 1)),
]
COPY_PROGRAM = {
    "list_build.build": "list_build",
    "transforms.doubled": "transforms",
    "transforms.list_sort": "transforms",
    "maps.fill": "maps",
    "maps.drop_keys": "maps",
    "records.shift_all": "records",
}


def shape_metrics(src: str) -> dict:
    """Emitted-code shape: counts that do not move with machine load."""
    return {
        "lambda-in-expression": src.count("(lambda "),
        "_revl_i64(": src.count("_revl_i64("),
        "_revl_field(": src.count("_revl_field("),
        "persistent-push `+ [`": src.count(" + ["),
        "dict-spread `{**`": src.count("{**"),
    }


def _ratio(e, h):
    return f"{e}/{h} = {e / max(h, 1):>7.2f}x"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--time", action="store_true",
                    help="also run the interleaved A/B wall clock")
    ap.add_argument("--curve", action="store_true",
                    help="print E/H at a series of sizes (complexity gap)")
    ap.add_argument("--scale", action="store_true",
                    help="also measure the growth exponent (O(n) vs O(n^2))")
    ap.add_argument("--case", action="append", default=None,
                    help="restrict to one program (repeatable)")
    ap.add_argument("--dump", metavar="PROGRAM",
                    help="print the emitted Python for one program and exit")
    args = ap.parse_args()

    if args.dump:
        print(_load_emitted(args.dump).__dict__["__source__"])
        return 0

    hw = _load_handwritten()
    wanted = set(args.case) if args.case else None

    print("revl cordis-py CODEGEN audit -- emitted (E) vs hand-written (H)")
    print("Every default metric is DETERMINISTIC: same number on every run,")
    print("on any host, at any load. No duration is reported by default.")
    print("`ops` counts INTERPRETER instructions and is blind to C-level")
    print("copying; `copies` is the metric that sees it.")
    if args.time or args.scale or args.curve:
        print()
        print("!! A TIMING MODE IS ON. Its numbers are only meaningful on a")
        print("!! QUIET machine. Do not quote them from a loaded one.")
    print()

    loaded = {}
    rows = 0
    for program in PROGRAM_ORDER:
        if wanted and program not in wanted:
            continue
        em = loaded[program] = _load_emitted(program)
        shape = {k: v for k, v in
                 shape_metrics(em.__dict__["__source__"]).items() if v}
        print(f"=== {program}.rvl    "
              + "  ".join(f"{k} x{v}" for k, v in shape.items()))
        for name, note, e_small, h_small, e_big, h_big in \
                cases(em, hw)[program]():
            print(f"  {name}  --  {note}")
            print(f"      ops   {_ratio(count_ops(e_small), count_ops(h_small))}")
            print(f"      calls {_ratio(count_calls(e_small), count_calls(h_small))}")
            print(f"      bytes {_ratio(peak_bytes(e_small), peak_bytes(h_small))}")
            if args.time:
                med, lo, hi = ab_ratio(e_big, h_big)
                print(f"      wall  {med:.2f}x  [{lo:.2f} .. {hi:.2f}] "
                      f"(LOADED-MACHINE CAVEAT)")
            rows += 1
        print()

    # Element copies, at n and 2n. Deterministic, and the evidence for every
    # complexity finding in the audit.
    hw_rc = _instrument_handwritten()
    rc = {}
    print("=== element copies at n and 2n  "
          "(x4 when n doubles = quadratic; x2 = linear)")
    for label, fn, base, mkargs in COPY_PROBES:
        program = COPY_PROGRAM[label]
        if wanted and program not in wanted:
            continue
        if program not in loaded:
            loaded[program] = _load_emitted(program)
        if program not in rc:
            rc[program] = _instrument_emitted(program, loaded[program])
        e_fn, h_fn = getattr(rc[program], fn), getattr(hw_rc, fn)
        copycount.verify(e_fn, getattr(loaded[program], fn), *mkargs(base))
        copycount.verify(h_fn, getattr(hw, fn), *mkargs(base))
        e1 = copycount.measure(e_fn, *mkargs(base))
        e2 = copycount.measure(e_fn, *mkargs(2 * base))
        h1 = copycount.measure(h_fn, *mkargs(base))
        h2 = copycount.measure(h_fn, *mkargs(2 * base))
        print(f"  {label:<24} n={base:<6} E {e1:>9}  H {h1:>7}"
              f"   n={2 * base:<6} E {e2:>9} (x{e2 / max(e1, 1):.1f})"
              f"  H {h2:>7} (x{h2 / max(h1, 1):.1f})")
    print()

    # Duplicated evaluation. `??` renders its left operand twice, so a call on
    # the left runs twice. The extern in nullish.rvl counts its own
    # invocations, which turns the duplication into a deterministic number and
    # makes the point that this is a semantic defect before it is a cost.
    if not wanted or "nullish" in wanted:
        nu = _load_emitted("nullish")
        ks = list(range(1000))
        nu.CALLS = 0
        nu.resolve_many(ks)
        print("=== duplicated evaluation of a `??` left operand")
        print(f"  1000 x `lookup(k) ?? 0`: the extern ran {nu.CALLS} times. "
              f"A hand-written bind runs it {len(ks)}.")
        print()

    if args.curve:
        for p in ("list_build", "transforms", "maps", "records"):
            loaded.setdefault(p, _load_emitted(p))
        print("=== E/H at growing n  (a ratio that RISES is a complexity gap)")
        for label, base, mk_e, mk_h in scale_probes(loaded, hw):
            sizes = [base // 4, base // 2, base, base * 2]
            print(f"  {label}")
            for n, be, bh in curve(mk_e, mk_h, sizes):
                print(f"      n={n:<8} E/H = {be / bh:7.2f}x")
        print()

    if args.scale:
        missing = [p for p in ("list_build", "transforms", "maps", "records")
                   if p not in loaded]
        for p in missing:
            loaded[p] = _load_emitted(p)
        print("=== growth exponent  (time(2n)/time(n) as log2; "
              "1.0 = linear, 2.0 = quadratic)")
        for label, n, mk_e, mk_h in scale_probes(loaded, hw):
            ge = growth(mk_e, n)
            gh = growth(mk_h, n)
            print(f"  {label:<24} E {ge:5.2f}   H {gh:5.2f}")
        print()

    print(f"{rows} cases. Higher than 1.00x means the emitter costs more.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
