#!/usr/bin/env python3
"""Codegen performance harness for the rust backend.

For each benchmark this builds ONE cargo crate containing two modules:

  * `emitted`     — `backends/rust/emit.py` applied to `programs/<name>.rvl`
  * `handwritten` — `handwritten/<name>.rs`, the rust a competent developer
                    would write BY HAND for the same semantics

and a driver that

  1. asserts the two agree on the result (a comparator that computes
     something else is not a comparator),
  2. counts HEAP ALLOCATIONS and heap BYTES for one call of each, through a
     counting global allocator, minus the cost of the shared input clone,
  3. counts wasteful constructs in the generated TEXT of each side.

Why it is shaped this way: allocation counts and generated-code shape are
properties of the generated code. They read the same on an idle laptop and on
a host running a dozen other jobs, so they are evidence. Wall clock is not:
a duration measured under unknown load says nothing, and neither does a ratio
of two durations sampled at different moments. So NO TIMING IS TAKEN unless
`--timing` is passed, and that flag is for a quiet, otherwise-idle host.

Usage
-----
    PYTHONPATH=<repo>/src python3 bench/codegen/rust/run.py            # all
    PYTHONPATH=<repo>/src python3 bench/codegen/rust/run.py str_append # one
    ... --json out.json --keep

    # ONLY on a quiet machine, and say so when quoting the numbers:
    PYTHONPATH=<repo>/src python3 bench/codegen/rust/run.py --timing --rounds 25

Requires `cargo` and a resolvable `cordis-rs` (the emitted module imports it).
When either is missing the harness says so and measures nothing, rather than
reporting analysis as if it were measurement.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import platform
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT / "src"))


def _load_emitter():
    """Load `backends/rust/emit.py` by path, as the backend's own tests do."""
    path = ROOT / "backends" / "rust" / "emit.py"
    spec = importlib.util.spec_from_file_location("rustemit_bench", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --------------------------------------------------------------- benchmarks
#
# `setup` builds the input; `args` is the argument list, one rust expression
# per parameter. Both sides are called with the SAME argument expressions, so
# the harness compares the emitter's choices and not two different APIs. The
# same argument expressions are also evaluated with no work on top, and those
# allocations are subtracted, so the reported figure is what the FUNCTION
# allocates rather than what the harness spent building its input.

BENCHES = {
    "str_append": {
        "doc": "accumulate a Str in a loop (`s = s + p`)",
        "setup": 'let input: Vec<String> = (0..3000).map(|i| format!("part-{}-", i)).collect();',
        "args": ["input.clone()"],
        "iters": 20,
    },
    "list_append": {
        "doc": "accumulate a List in a loop (`out = out.push(x)`) — item 284 control",
        "setup": "let input: i64 = 200000;",
        "args": ["input"],
        "iters": 20,
    },
    "loop_length": {
        # The emitted form is O(n^2) and the comparator O(n), so the expected
        # time ratio is about n/2. n is kept modest for that reason: the point
        # is the complexity class, and a bigger n only makes the same point
        # more slowly.
        "doc": "`while (i < s.length())` — loop-invariant codepoint count",
        "setup": 'let input: String = "x".repeat(8000);',
        "args": ["input.clone()"],
        "iters": 2,
    },
    "str_index_of": {
        "doc": "repeated `Str.indexOf` over one haystack",
        "setup": (
            'let hay: String = "lorem ipsum dolor sit amet ".repeat(400);\n'
            '    let needles: Vec<String> = (0..300)\n'
            '        .map(|i| format!("dolor{}", i % 3))\n'
            "        .collect();"
        ),
        "args": ["hay.clone()", "needles.clone()"],
        "iters": 5,
    },
    "for_over_local": {
        "doc": "`for r of rows` over a locally built, afterwards-dead List",
        "setup": "let input: i64 = 60000;",
        "args": ["input"],
        "iters": 20,
    },
    "literal_arg": {
        "doc": "string LITERALS passed to builtins whose helper takes `&str`",
        "setup": (
            'let input: Vec<String> = (0..40000)\n'
            '        .map(|i| format!("pre-{}-midfix", i))\n'
            "        .collect();"
        ),
        "args": ["input.clone()"],
        "iters": 5,
    },
    "str_eq_literal": {
        "doc": "`x == \"literal\"` comparisons",
        "setup": (
            'let input: Vec<String> = (0..80000)\n'
            '        .map(|i| if i % 3 == 0 { String::from("alpha") } else { format!("v{}", i) })\n'
            "        .collect();"
        ),
        "args": ["input.clone()"],
        "iters": 5,
    },
    "int_arith": {
        "doc": "trapping Int arithmetic — expected to be FINE (negative result)",
        "setup": "let input: i64 = 3000000;",
        "args": ["input"],
        "iters": 10,
    },
    "slice_scan": {
        "doc": "positional `Str.slice(i, i+1)` scan — the item-277/282 residual",
        "setup": 'let input: String = "abcdefghij".repeat(400);',
        "args": ["input.clone()"],
        "iters": 5,
    },
    "concat_append": {
        "doc": "self-append through `concat` (`out = out.concat(x)`) — the item-284 gap",
        "setup": (
            'let input: Vec<Vec<String>> = (0..600)\n'
            '        .map(|i| (0..4).map(|j| format!("line-{}-{}", i, j)).collect())\n'
            "        .collect();"
        ),
        "args": ["input.clone()"],
        "iters": 5,
    },
    "list_index": {
        "doc": "`xs[i]` read in a read-only position — clone per index read",
        "setup": (
            'let xs: Vec<String> = (0..50000)\n'
            '        .map(|i| if i % 5 == 0 { String::from("key") } else { format!("v{}", i) })\n'
            "        .collect();\n"
            '    let probe = String::from("key");'
        ),
        "args": ["xs.clone()", "probe.clone()"],
        "iters": 5,
    },
    "const_list": {
        "doc": "a constant List[Str] rebuilt on every call (selfhost lexer `keywords()`)",
        "setup": (
            'let input: Vec<String> = (0..20000)\n'
            '        .map(|i| if i % 4 == 0 { String::from("while") } else { format!("id{}", i) })\n'
            "        .collect();"
        ),
        "args": ["input.clone()"],
        "iters": 3,
    },
}


# ------------------------------------------------------------------- driver

_DRIVER = r"""// Generated by bench/codegen/rust/run.py — do not edit.
#![allow(unused_imports, unused_parens)]

mod emitted;
mod handwritten;

use std::alloc::{GlobalAlloc, Layout, System};
use std::hint::black_box;
use std::sync::atomic::{AtomicU64, Ordering};

// A counting global allocator. Load-robust: the count is a property of the
// generated code, not of what else this machine happens to be running.
static ALLOCS: AtomicU64 = AtomicU64::new(0);
static BYTES: AtomicU64 = AtomicU64::new(0);
static ARMED: AtomicU64 = AtomicU64::new(0);

struct Counting;

unsafe impl GlobalAlloc for Counting {
    unsafe fn alloc(&self, l: Layout) -> *mut u8 {
        if ARMED.load(Ordering::Relaxed) != 0 {
            ALLOCS.fetch_add(1, Ordering::Relaxed);
            BYTES.fetch_add(l.size() as u64, Ordering::Relaxed);
        }
        System.alloc(l)
    }
    unsafe fn dealloc(&self, p: *mut u8, l: Layout) {
        System.dealloc(p, l)
    }
    unsafe fn realloc(&self, p: *mut u8, l: Layout, new: usize) -> *mut u8 {
        if ARMED.load(Ordering::Relaxed) != 0 {
            ALLOCS.fetch_add(1, Ordering::Relaxed);
            // A realloc only newly obtains the GROWTH, so count the delta.
            BYTES.fetch_add(new.saturating_sub(l.size()) as u64, Ordering::Relaxed);
        }
        System.realloc(p, l, new)
    }
}

#[global_allocator]
static GLOBAL: Counting = Counting;

fn measure<F: FnOnce() -> i64>(f: F) -> (u64, u64, i64) {
    ALLOCS.store(0, Ordering::Relaxed);
    BYTES.store(0, Ordering::Relaxed);
    ARMED.store(1, Ordering::Relaxed);
    let v = f();
    ARMED.store(0, Ordering::Relaxed);
    (
        ALLOCS.load(Ordering::Relaxed),
        BYTES.load(Ordering::Relaxed),
        v,
    )
}

fn time_ms<F: FnMut() -> i64>(mut f: F, iters: usize) -> f64 {
    let t0 = std::time::Instant::now();
    let mut sink = 0i64;
    for _ in 0..iters {
        sink = sink.wrapping_add(f());
    }
    let ms = t0.elapsed().as_secs_f64() * 1e3;
    black_box(sink);
    ms
}

fn main() {
    // Timing is OPT-IN and off by default. On a machine running other work a
    // duration is noise wearing the costume of a measurement, and so is a
    // ratio of two durations sampled at different moments. Allocation counts
    // are a property of the generated code and do not move with load, so they
    // are what this harness reports unless a human explicitly asks for a
    // timing pass on a quiet host.
    let timing = std::env::args().any(|a| a == "--timing");

    __SETUP__

    let call_a = || emitted::__NAME__(__ARGS__);
    let call_b = || handwritten::__NAME__(__ARGS__);
    // The same argument expressions with no work on top: subtracted from both
    // sides so the reported allocations belong to the FUNCTION, not to the
    // harness building its input.
    let call_c = || {
        let c = (__ARGS__);
        black_box(&c);
        0i64
    };

    // 1) agreement — a comparator that computes something else is not one.
    let va = call_a();
    let vb = call_b();
    if va != vb {
        eprintln!("DISAGREE emitted={} handwritten={}", va, vb);
        std::process::exit(3);
    }

    // 2) allocations (order alternated so first-touch effects do not land on
    //    one side only)
    let (ca, cb, _) = measure(call_c);
    let (hb_a, hb_b, _) = measure(call_b);
    let (ea, eb, _) = measure(call_a);
    let (ca2, cb2, _) = measure(call_c);
    let base_a = ca.min(ca2);
    let base_b = cb.min(cb2);

    // 3) timing — ONLY when explicitly asked for, and never on by default.
    let iters: usize = __ITERS__;
    let rounds: usize = if timing { __ROUNDS__ } else { 0 };
    let mut ta_all: Vec<f64> = Vec::with_capacity(rounds);
    let mut tb_all: Vec<f64> = Vec::with_capacity(rounds);
    if timing {
        for _ in 0..2 {
            black_box(time_ms(call_a, iters));
            black_box(time_ms(call_b, iters));
        }
        for r in 0..rounds {
            let (ta, tb) = if r % 2 == 0 {
                let a = time_ms(call_a, iters);
                let b = time_ms(call_b, iters);
                (a, b)
            } else {
                let b = time_ms(call_b, iters);
                let a = time_ms(call_a, iters);
                (a, b)
            };
            ta_all.push(ta);
            tb_all.push(tb);
        }
    }

    let fmt = |v: &Vec<f64>| -> String {
        v.iter()
            .map(|x| format!("{:.6}", x))
            .collect::<Vec<_>>()
            .join(",")
    };
    println!(
        "RESULT {{\"name\":\"__NAME__\",\"value\":{},\"iters\":{},\
\"emitted_allocs\":{},\"emitted_bytes\":{},\
\"hand_allocs\":{},\"hand_bytes\":{},\
\"input_allocs\":{},\"input_bytes\":{},\
\"emitted_allocs_net\":{},\"hand_allocs_net\":{},\
\"emitted_bytes_net\":{},\"hand_bytes_net\":{},\
\"timing\":{},\"ta_ms\":[{}],\"tb_ms\":[{}]}}",
        va,
        iters,
        ea,
        eb,
        hb_a,
        hb_b,
        base_a,
        base_b,
        ea.saturating_sub(base_a),
        hb_a.saturating_sub(base_a),
        eb.saturating_sub(base_b),
        hb_b.saturating_sub(base_b),
        timing,
        fmt(&ta_all),
        fmt(&tb_all),
    );
}
"""


# --------------------------------------------------------------- cargo shims

_OFFLINE_RESOLVE_MARKERS = (
    "you're using offline mode",
    "without the offline flag",
    "--offline was specified",
    "registry index was not found",
    "no matching package",
    "failed to select a version",
)
_REAL_FAILURE_MARKERS = ("error[e", "could not compile", "panicked at")


def _is_offline_resolve_failure(proc) -> bool:
    blob = ((proc.stderr or "") + (proc.stdout or "")).lower()
    if any(m in blob for m in _REAL_FAILURE_MARKERS):
        return False
    return any(m in blob for m in _OFFLINE_RESOLVE_MARKERS)


def _crates_io_reachable() -> bool:
    import socket

    try:
        socket.create_connection(("index.crates.io", 443), timeout=3).close()
        return True
    except OSError:
        return False


def _cargo(subcommand: str, cwd: Path, env: dict, *extra: str):
    """`cargo <sub>` offline first, networked resolve only for a RESOLVE
    failure — never to launder a build failure into a retry. Same policy as
    backends/rust/test_emit_rust.py."""
    offline = subprocess.run(
        ["cargo", subcommand, "--offline", *extra],
        cwd=cwd,
        text=True,
        capture_output=True,
        timeout=1800,
        env=env,
    )
    if offline.returncode == 0 or not _is_offline_resolve_failure(offline):
        return offline
    if not _crates_io_reachable():
        return offline
    return subprocess.run(
        ["cargo", subcommand, *extra],
        cwd=cwd,
        text=True,
        capture_output=True,
        timeout=1800,
        env=env,
    )


# ------------------------------------------------------------------- driving


class Outcome:
    def __init__(self, name: str, doc: str):
        self.name = name
        self.doc = doc
        self.status = "unmeasured"
        self.reason = ""
        self.data: dict | None = None
        self.emitted_src = ""
        self.hand_src = ""
        self.build_ms: float | None = None


def _emit(emitter, name: str) -> str:
    from revl import compile_files

    return emitter.emit(compile_files([str(HERE / "programs" / f"{name}.rvl")]))


def _fn_body(src: str, name: str) -> str:
    """The emitted body of `pub fn <name>`, for the report."""
    m = re.search(rf"^pub fn {re.escape(name)}\(", src, re.M)
    if not m:
        return ""
    i = src.index("{", m.start())
    depth, j = 0, i
    while j < len(src):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[m.start() : j + 1]
        j += 1
    return ""


def run_one(emitter, name: str, spec: dict, workdir: Path, rounds: int,
            timing: bool) -> Outcome:
    out = Outcome(name, spec["doc"])

    try:
        emitted_src = _emit(emitter, name)
    except Exception as exc:  # noqa: BLE001
        out.reason = f"cannot emit to rust: {exc}"
        return out
    out.emitted_src = _fn_body(emitted_src, name)

    hand = HERE / "handwritten" / f"{name}.rs"
    if not hand.exists():
        out.reason = f"no hand-written comparator at {hand}"
        return out

    crate = workdir / name
    (crate / "src").mkdir(parents=True, exist_ok=True)
    (crate / "src" / "emitted.rs").write_text(emitted_src, encoding="utf-8")
    hand_src = hand.read_text(encoding="utf-8")
    out.hand_src = hand_src
    (crate / "src" / "handwritten.rs").write_text(hand_src, encoding="utf-8")
    driver = (
        _DRIVER.replace("__SETUP__", spec["setup"])
        .replace("__ARGS__", ", ".join(f"black_box({a})" for a in spec["args"]))
        .replace("__ITERS__", str(spec["iters"]))
        .replace("__ROUNDS__", str(rounds))
        .replace("__NAME__", name)
    )
    (crate / "src" / "main.rs").write_text(driver, encoding="utf-8")
    (crate / "Cargo.toml").write_text(
        emitter.cargo_toml(f"revl_codegen_bench_{name}"), encoding="utf-8"
    )

    env = dict(os.environ)
    # One shared target dir so cordis-rs / serde are compiled once for the
    # whole sweep rather than once per benchmark.
    env["CARGO_TARGET_DIR"] = str(workdir / "_target")

    t0 = time.perf_counter()
    built = _cargo("build", crate, env, "--release")
    out.build_ms = (time.perf_counter() - t0) * 1e3
    if built.returncode != 0:
        blob = (built.stderr or built.stdout or "").strip()
        out.reason = "cargo build failed:\n" + blob[-2000:]
        return out

    binary = workdir / "_target" / "release" / f"revl_codegen_bench_{name}"
    if not binary.exists():
        out.reason = f"release binary not found at {binary}"
        return out

    argv = [str(binary)] + (["--timing"] if timing else [])
    run = subprocess.run(argv, text=True, capture_output=True, timeout=1800)
    if run.returncode != 0:
        out.reason = "benchmark binary exited nonzero:\n" + (
            run.stderr or run.stdout or ""
        ).strip()[-2000:]
        return out
    m = re.search(r"^RESULT (\{.*\})$", run.stdout, re.M)
    if not m:
        out.reason = f"no RESULT line: {run.stdout.strip()[:400]}"
        return out
    out.data = json.loads(m.group(1))
    out.status = "ok"
    return out


# Load-independent shape counters. These are properties of the generated
# TEXT: they are identical on an idle machine and on one running ten agents,
# and they name the waste directly (a `.clone()` the hand-written version does
# not need, a `String::from` where a `&str` would do).
_SHAPE_PATTERNS = {
    ".clone()": r"\.clone\(\)",
    "String::from": r"String::from\(",
    "to_string()": r"\.to_string\(\)",
    "format!": r"format!\(",
    "collect()": r"\.collect\(\)",
    "chars()": r"\.chars\(\)",
}


def _shape(src: str) -> dict[str, int]:
    return {k: len(re.findall(v, src)) for k, v in _SHAPE_PATTERNS.items()}


def _report(outcomes: list[Outcome], rounds: int, timing: bool) -> None:
    print()
    print("=" * 78)
    print("emitted vs hand-written rust — allocation counts and generated-code shape")
    print("=" * 78)
    print(f"machine   : {platform.platform()}")
    print(f"toolchain : {_toolchain_line()}")
    print()
    print(
        "Timing is NOT reported by default and was not taken here. A duration\n"
        "measured on a machine running other work is not evidence, and neither\n"
        "is a ratio of two durations sampled at different moments. What follows\n"
        "is load-independent: heap allocations counted by a global allocator,\n"
        "heap bytes, and counts of wasteful constructs in the generated text.\n"
        "Run with --timing on a QUIET host to add a timing pass."
    )
    print()
    head = (
        f"{'benchmark':<16}{'allocs emit':>14}{'allocs hand':>14}{'alloc x':>10}"
        f"{'bytes emit':>16}{'bytes hand':>14}"
    )
    print(head)
    print("-" * len(head))
    for o in outcomes:
        if o.status != "ok":
            print(f"{o.name:<16}  UNMEASURED — {o.reason.splitlines()[0][:56]}")
            continue
        d = o.data
        ea, ha = d["emitted_allocs_net"], d["hand_allocs_net"]
        ax = "inf" if ha == 0 and ea else (f"{ea / ha:.2f}x" if ha else "1.00x")
        print(
            f"{o.name:<16}{ea:>14,}{ha:>14,}{ax:>10}"
            f"{d['emitted_bytes_net']:>16,}{d['hand_bytes_net']:>14,}"
        )
    print()
    print("`alloc x` above 1.00 means the EMITTED code allocates more.")
    print()

    for o in outcomes:
        if o.status != "ok":
            continue
        d = o.data
        print(f"--- {o.name}: {o.doc}")
        print(
            f"    heap allocations : emitted {d['emitted_allocs_net']:,}  "
            f"hand {d['hand_allocs_net']:,}"
        )
        print(
            f"    heap bytes       : emitted {d['emitted_bytes_net']:,}  "
            f"hand {d['hand_bytes_net']:,}"
        )
        se, sh = _shape(o.emitted_src), _shape(o.hand_src)
        diff = [
            f"{k} {se[k]}/{sh[k]}" for k in _SHAPE_PATTERNS if se[k] or sh[k]
        ]
        print(f"    shape (emit/hand): {', '.join(diff) or 'none'}")
        if timing and d.get("ta_ms"):
            ratios = sorted(a / b for a, b in zip(d["ta_ms"], d["tb_ms"]) if b > 0)
            print(
                f"    TIMING PASS      : median {statistics.median(ratios):.3f}x  "
                f"range {ratios[0]:.3f}-{ratios[-1]:.3f}x  (n={len(ratios)}) "
                "— only trust this from a quiet host"
            )
        if o.build_ms:
            print(f"    build            : {o.build_ms / 1000:.1f}s (setup, not measured)")
        print()


def _toolchain_line() -> str:
    def ver(cmd):
        try:
            return subprocess.check_output(cmd, text=True).strip().splitlines()[0]
        except Exception:  # noqa: BLE001
            return "unavailable"

    return f"{ver(['cargo', '--version'])} / {ver(['rustc', '--version'])}"


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("names", nargs="*", help="benchmarks to run (default: all)")
    ap.add_argument("--rounds", type=int, default=15)
    ap.add_argument(
        "--timing",
        action="store_true",
        help="ALSO take a wall-clock pass. Off by default and meaningless on a "
        "busy machine — only use it on a quiet host.",
    )
    ap.add_argument("--keep", action="store_true", help="keep the generated crates")
    ap.add_argument("--json", help="write the raw results to this path")
    args = ap.parse_args(argv[1:])

    if shutil.which("cargo") is None:
        print("cargo is not installed — nothing measured.", file=sys.stderr)
        return 1

    names = args.names or list(BENCHES)
    unknown = [n for n in names if n not in BENCHES]
    if unknown:
        print(f"unknown benchmark(s): {', '.join(unknown)}", file=sys.stderr)
        return 2

    emitter = _load_emitter()
    workdir = Path(tempfile.mkdtemp(prefix="revl-codegen-bench-"))
    try:
        outcomes = []
        for name in names:
            print(f"[build+run] {name} ...", flush=True)
            outcomes.append(run_one(emitter, name, BENCHES[name], workdir, args.rounds, args.timing))
        _report(outcomes, args.rounds, args.timing)
        if args.json:
            Path(args.json).write_text(
                json.dumps(
                    [
                        {
                            "name": o.name,
                            "doc": o.doc,
                            "status": o.status,
                            "reason": o.reason,
                            "data": o.data,
                            "emitted": o.emitted_src,
                        }
                        for o in outcomes
                    ],
                    indent=2,
                ),
                encoding="utf-8",
            )
            print(f"raw results -> {args.json}")
        for o in outcomes:
            if o.status != "ok":
                print(f"\n!! {o.name} unmeasured:\n{o.reason}\n", file=sys.stderr)
        return 0
    finally:
        if args.keep:
            print(f"crates kept in {workdir}")
        else:
            shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
