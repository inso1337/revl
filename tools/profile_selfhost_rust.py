#!/usr/bin/env python3
"""Profile the native (rust) self-host LEXER: where the ~17x goes (roadmap 283).

Item 266 measured the rust self-host lexer at ~349 ms vs 20.55 ms CPython
(17x SLOWER). Item 277 proved the per-function `Vec<char>` charAt fix REGRESSES
and concluded charAt is not the sole cost. This tool does not fix anything; it
attributes the run time to concrete operations, with data, so the next fix is
routed correctly.

It reuses the exact stage/corpus wiring the item-266 harness pins
(`tools/bench_selfhost_rust.py`, imported) and the SAME reference rust backend
(`backends/rust/emit.py`). Three measurements, all reproducible on this box
without sudo:

  1. BUILD-PROFILE SWEEP. The item-266 harness builds with `cargo build
     --release` against a Cargo.toml that declares NO `[profile.release]` block,
     so it inherits cargo's default release profile. This sweep re-emits the
     same lexer crate and rebuilds it under several explicit profiles (default
     release, +lto/codegen-units=1, opt-level 2, and a dev/unoptimised build for
     contrast), timing the in-process median each time, to show whether the
     profile explains any of the gap.

  2. OPERATION DECOMPOSITION. A second, INSTRUMENTED build of the same lexer:
     a counting global allocator (allocations + bytes per whole-corpus pass) and
     atomic counters injected into the hot operations (char index `chars().nth`
     with the summed index depth, `revl_slice`, `revl_length`, `revl_concat`,
     `code0`, and the full-source `.clone()` threaded down the helpers). One pass
     is deterministic, so the counts are exact, not sampled.

  3. MICRO-BENCHMARKS. Standalone rust timing each hot operation in isolation at
     corpus scale (front-walk `chars().nth().to_string()` vs a `Vec<char>` O(1)
     index, a full-source `String::clone`, and `revl_slice`), so each counted op
     can be priced and multiplied back against its frequency.

Every number here is measured on this machine with the printed toolchain; a
stage that cannot build/run is reported with its reason, never faked. Only the
lexer is profiled (it is the only self-host stage that builds to rust today; the
others fail to `cargo build`, per item 266).

Run:  python3 tools/profile_selfhost_rust.py
"""

from __future__ import annotations

import importlib.util
import platform
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

import bench_selfhost as pyb  # noqa: E402
from revl import compile_files  # noqa: E402
from revl.run_rust import rust_runtime_reason  # noqa: E402

# Reuse the item-266 harness so the stage table, corpus, and generated timing
# main are literally the same code, not a drifting copy.
import bench_selfhost_rust as rb  # noqa: E402


def _load_rustemit():
    spec = importlib.util.spec_from_file_location(
        "rustemit_reference_profile", ROOT / "backends/rust/emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


RUSTEMIT = _load_rustemit()

CPYTHON_LEXER_MS = rb.CPYTHON_SELFHOST_RUN_MS["lexer"]


def lexer_stage():
    for s in rb.stages():
        if s.name == "lexer":
            return s
    raise SystemExit("lexer stage not found")


# ------------------------------------------------------------ cargo profiles
#
# The item-266 crate declares no [profile.release], so `cargo build --release`
# uses cargo's DEFAULT release profile: opt-level 3, debug-assertions off,
# overflow-checks off, lto off, codegen-units 16, panic unwind. We name that
# baseline explicitly and then sweep the knobs that could plausibly move the
# number.
PROFILES = {
    # what the item-266 harness actually gets today (no [profile.*] at all).
    "release-default (item-266)": {"flag": "--release", "toml": ""},
    "release +overflow-checks=false (explicit)": {
        "flag": "--release",
        "toml": "[profile.release]\noverflow-checks = false\ndebug-assertions = false\n",
    },
    "release +lto=fat +codegen-units=1": {
        "flag": "--release",
        "toml": "[profile.release]\nlto = \"fat\"\ncodegen-units = 1\n",
    },
    "release opt-level=2": {
        "flag": "--release",
        "toml": "[profile.release]\nopt-level = 2\n",
    },
    "dev (unoptimised, opt-level=0)": {"flag": "", "toml": ""},
}


def _cargo_toml_with_profile(name: str, profile_toml: str) -> str:
    base = RUSTEMIT.cargo_toml(name)
    if profile_toml:
        base = base + "\n" + profile_toml
    return base


def _emit_lexer_source(stage) -> str:
    ir = compile_files([str(ROOT / stage.rvl)])
    return RUSTEMIT.emit(ir)


def _write_crate(crate: Path, main_rs: str, cargo_toml: str):
    (crate / "src").mkdir(parents=True, exist_ok=True)
    (crate / "src" / "main.rs").write_text(main_rs, encoding="utf-8")
    (crate / "Cargo.toml").write_text(cargo_toml, encoding="utf-8")


def _build_and_run(crate: Path, binname: str, build_flag: str):
    args = ["build"] + ([build_flag] if build_flag else [])
    built = rb._cargo(*([args[0], crate] + args[1:]))
    if built.returncode != 0:
        return None, ("build failed:\n" + (built.stderr or built.stdout or "").strip()[-1200:])
    sub = "release" if build_flag == "--release" else "debug"
    binary = crate / "target" / sub / binname
    if not binary.exists():
        return None, f"binary not found at {binary}"
    run = subprocess.run([str(binary)], text=True, capture_output=True, timeout=600)
    if run.returncode != 0:
        return None, ("run failed:\n" + (run.stderr or run.stdout or "").strip()[-1200:])
    return run.stdout, None


# ------------------------------------------------------------ profile sweep

def profile_sweep(stage, module_src: str, workroot: Path):
    print("=" * 74)
    print("1. BUILD-PROFILE SWEEP  (same lexer crate, varied cargo profile)")
    print("=" * 74)
    main_rs = module_src + rb._gen_main(stage)
    binname = "revl_prof_lexer"
    rows = []
    for label, spec in PROFILES.items():
        crate = workroot / ("prof_" + re.sub(r"[^a-z0-9]+", "_", label.lower()))
        toml = _cargo_toml_with_profile(binname, spec["toml"])
        _write_crate(crate, main_rs, toml)
        t0 = time.perf_counter()
        out, err = _build_and_run(crate, binname, spec["flag"])
        build_s = time.perf_counter() - t0
        if out is None:
            rows.append((label, None, build_s))
            print(f"  {label:<42} FAILED: {err.splitlines()[0]}")
            continue
        m = re.search(r"MEDIAN_MS\s+([0-9.]+)", out)
        ms = float(m.group(1)) if m else None
        rows.append((label, ms, build_s))
        vs = (CPYTHON_LEXER_MS / ms) if ms else None
        vs_s = f"{vs:.2f}x cpy" if vs else "?"
        print(f"  {label:<42} {ms:8.2f} ms   ({vs_s},  build {build_s:.1f}s)")
    print()
    return rows


# ------------------------------------------------------------ instrumentation

_PRELUDE = r"""
// ---- item-283 instrumentation (injected by tools/profile_selfhost_rust.py) ----
use std::alloc::{GlobalAlloc, Layout, System};
use std::sync::atomic::{AtomicU64, Ordering as __Ord};
static __ALLOC_N: AtomicU64 = AtomicU64::new(0);
static __ALLOC_B: AtomicU64 = AtomicU64::new(0);
static __NTH_N: AtomicU64 = AtomicU64::new(0);
static __NTH_IDX: AtomicU64 = AtomicU64::new(0);
static __SLICE_N: AtomicU64 = AtomicU64::new(0);
static __SLICE_CH: AtomicU64 = AtomicU64::new(0);
static __LEN_N: AtomicU64 = AtomicU64::new(0);
static __CONCAT_N: AtomicU64 = AtomicU64::new(0);
static __CODE0_N: AtomicU64 = AtomicU64::new(0);
static __SRCCLONE_N: AtomicU64 = AtomicU64::new(0);
static __SRCCLONE_B: AtomicU64 = AtomicU64::new(0);
static __PUSH_N: AtomicU64 = AtomicU64::new(0);
static __PUSH_ELEMS: AtomicU64 = AtomicU64::new(0);
struct __CountingAlloc;
unsafe impl GlobalAlloc for __CountingAlloc {
    unsafe fn alloc(&self, l: Layout) -> *mut u8 {
        __ALLOC_N.fetch_add(1, __Ord::Relaxed);
        __ALLOC_B.fetch_add(l.size() as u64, __Ord::Relaxed);
        System.alloc(l)
    }
    unsafe fn dealloc(&self, p: *mut u8, l: Layout) { System.dealloc(p, l) }
    unsafe fn realloc(&self, p: *mut u8, l: Layout, ns: usize) -> *mut u8 {
        __ALLOC_N.fetch_add(1, __Ord::Relaxed);
        __ALLOC_B.fetch_add(ns as u64, __Ord::Relaxed);
        System.realloc(p, l, ns)
    }
}
#[global_allocator]
static __GA: __CountingAlloc = __CountingAlloc;
trait __CntNth { fn cnt_nth(&self, i: usize) -> Option<char>; }
impl __CntNth for str {
    #[inline(never)]
    fn cnt_nth(&self, i: usize) -> Option<char> {
        __NTH_N.fetch_add(1, __Ord::Relaxed);
        __NTH_IDX.fetch_add(i as u64, __Ord::Relaxed);
        self.chars().nth(i)
    }
}
#[inline(never)]
fn __cnt_srcclone(s: &String) -> String {
    __SRCCLONE_N.fetch_add(1, __Ord::Relaxed);
    __SRCCLONE_B.fetch_add(s.len() as u64, __Ord::Relaxed);
    s.clone()
}
// -------------------------------------------------------------------------------
"""


def _instrument(module_src: str) -> str:
    src = module_src
    # char index: `.chars().nth((` -> counted extension (counts calls + index depth)
    src = src.replace(".chars().nth((", ".cnt_nth((")
    # full-source clone threaded down the helpers
    src = src.replace("source.clone()", "__cnt_srcclone(&source)")
    # revl_length
    src = src.replace(
        "fn revl_length(&self) -> i64 { self.chars().count() as i64 }",
        "fn revl_length(&self) -> i64 { __LEN_N.fetch_add(1, __Ord::Relaxed); self.chars().count() as i64 }")
    # revl_slice on String (the char-collecting one)
    src = src.replace(
        "fn revl_slice(&self, a: i64, b: i64) -> String {\n"
        "        self.chars().skip(a.max(0) as usize).take((b - a).max(0) as usize).collect()",
        "fn revl_slice(&self, a: i64, b: i64) -> String {\n"
        "        __SLICE_N.fetch_add(1, __Ord::Relaxed);\n"
        "        __SLICE_CH.fetch_add((b - a).max(0) as u64, __Ord::Relaxed);\n"
        "        self.chars().skip(a.max(0) as usize).take((b - a).max(0) as usize).collect()")
    # revl_concat on String
    src = src.replace(
        'fn revl_concat(&self, other: &String) -> String { format!("{}{}", self, other) }',
        'fn revl_concat(&self, other: &String) -> String { __CONCAT_N.fetch_add(1, __Ord::Relaxed); format!("{}{}", self, other) }')
    # code0 (ord of a 1-char string)
    src = src.replace(
        "fn code0(c: String) -> i64 {",
        "fn code0(c: String) -> i64 {\n    __CODE0_N.fetch_add(1, __Ord::Relaxed);")
    # Vec revl_push clone-on-append (the persistent-list lowering). Count calls
    # and the total elements copied (sum of self.len(), i.e. the O(tokens^2) work).
    src = src.replace(
        "fn revl_push(&self, item: T) -> Vec<T> {\n        let mut _v = self.clone(); _v.push(item); _v",
        "fn revl_push(&self, item: T) -> Vec<T> {\n"
        "        __PUSH_N.fetch_add(1, __Ord::Relaxed);\n"
        "        __PUSH_ELEMS.fetch_add(self.len() as u64, __Ord::Relaxed);\n"
        "        let mut _v = self.clone(); _v.push(item); _v")
    # The prelude must go AFTER the crate-level inner attrs/doc comments
    # (`//!` + `#![allow(...)]`), which must stay first, or rustc errors E0753.
    lines = src.splitlines(keepends=True)
    insert_at = 0
    for idx, ln in enumerate(lines):
        stripped = ln.lstrip()
        if stripped.startswith("//!") or stripped.startswith("#!") or stripped.strip() == "":
            insert_at = idx + 1
        else:
            break
    return "".join(lines[:insert_at]) + _PRELUDE + "\n" + "".join(lines[insert_at:])


def _instrumented_main(stage) -> str:
    # One deterministic whole-corpus pass; print counters. No warmup, no timing:
    # the atomics make timing meaningless, but the COUNTS are exact per pass.
    items = ",\n        ".join(rb._rust_str_literal(s) for s in stage.corpus)
    entry = stage.entry
    return f"""

fn main() {{
    let corpus: Vec<String> = vec![
        {items}
    ].into_iter().map(|s: &str| s.to_string()).collect();
    let mut sink: u64 = 0;
    for item in &corpus {{
        sink = sink.wrapping_add({entry}(item.clone()).len() as u64);
    }}
    if sink == 0xdead_beef_dead_beef {{ eprintln!("sink {{}}", sink); }}
    println!("ALLOC_N {{}}", __ALLOC_N.load(__Ord::Relaxed));
    println!("ALLOC_B {{}}", __ALLOC_B.load(__Ord::Relaxed));
    println!("NTH_N {{}}", __NTH_N.load(__Ord::Relaxed));
    println!("NTH_IDX {{}}", __NTH_IDX.load(__Ord::Relaxed));
    println!("SLICE_N {{}}", __SLICE_N.load(__Ord::Relaxed));
    println!("SLICE_CH {{}}", __SLICE_CH.load(__Ord::Relaxed));
    println!("LEN_N {{}}", __LEN_N.load(__Ord::Relaxed));
    println!("CONCAT_N {{}}", __CONCAT_N.load(__Ord::Relaxed));
    println!("CODE0_N {{}}", __CODE0_N.load(__Ord::Relaxed));
    println!("SRCCLONE_N {{}}", __SRCCLONE_N.load(__Ord::Relaxed));
    println!("SRCCLONE_B {{}}", __SRCCLONE_B.load(__Ord::Relaxed));
    println!("PUSH_N {{}}", __PUSH_N.load(__Ord::Relaxed));
    println!("PUSH_ELEMS {{}}", __PUSH_ELEMS.load(__Ord::Relaxed));
}}
"""


def decomposition(stage, module_src: str, workroot: Path):
    print("=" * 74)
    print("2. OPERATION DECOMPOSITION  (instrumented build, one exact pass)")
    print("=" * 74)
    inst = _instrument(module_src) + _instrumented_main(stage)
    crate = workroot / "instrumented"
    binname = "revl_prof_lexer"
    _write_crate(crate, inst, RUSTEMIT.cargo_toml(binname))
    out, err = _build_and_run(crate, binname, "--release")
    if out is None:
        print("  instrumented build FAILED:")
        print("   " + (err or "").replace("\n", "\n   "))
        return None
    vals = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].lstrip("-").isdigit():
            vals[parts[0]] = int(parts[1])
    corpus_chars = sum(len(s) for s in stage.corpus)
    corpus_bytes = sum(len(s.encode("utf-8")) for s in stage.corpus)
    print(f"  corpus: {len(stage.corpus)} files, {corpus_chars} chars, "
          f"{corpus_bytes} bytes  (one whole-corpus pass)")
    print()
    nth_n = vals.get("NTH_N", 0)
    nth_idx = vals.get("NTH_IDX", 0)
    code0_n = vals.get("CODE0_N", 0)
    big_charat = nth_n - code0_n           # source-indexing nth (the O(i) walks)
    avg_depth = (nth_idx / big_charat) if big_charat else 0
    print(f"  char index  chars().nth calls        : {nth_n:>12,}")
    print(f"    of which code0 1-char ord accesses  : {code0_n:>12,}")
    print(f"    of which big-string charAt (O(i))   : {big_charat:>12,}")
    print(f"    summed index depth (chars walked)   : {nth_idx:>12,}  "
          f"(avg depth {avg_depth:,.0f} per big charAt)")
    print(f"  1-char String allocs (charAt.to_string): {big_charat:>12,}")
    print(f"  revl_slice calls                       : {vals.get('SLICE_N',0):>12,}")
    print(f"    chars collected by slice             : {vals.get('SLICE_CH',0):>12,}")
    print(f"  revl_length (chars().count) calls      : {vals.get('LEN_N',0):>12,}")
    print(f"  revl_concat calls                      : {vals.get('CONCAT_N',0):>12,}")
    print(f"  code0 (ord) calls                      : {code0_n:>12,}")
    print(f"  full-source .clone() calls             : {vals.get('SRCCLONE_N',0):>12,}")
    print(f"    bytes copied by full-source clones   : {vals.get('SRCCLONE_B',0):>12,}  "
          f"({vals.get('SRCCLONE_B',0)/1e6:.1f} MB / pass)")
    push_n = vals.get("PUSH_N", 0)
    push_elems = vals.get("PUSH_ELEMS", 0)
    print(f"  Vec revl_push (clone-on-append) calls  : {push_n:>12,}")
    print(f"    ELEMENTS copied by push-clones       : {push_elems:>12,}  "
          f"(O(tokens^2); each Token clone = 2 String allocs)")
    print(f"    => String allocs from push-clones    : {2*push_elems:>12,}  "
          f"(~{100*2*push_elems/max(vals.get('ALLOC_N',1),1):.0f}% of total allocations)")
    print()
    print(f"  TOTAL heap allocations / pass          : {vals.get('ALLOC_N',0):>12,}")
    print(f"  TOTAL heap bytes / pass                : {vals.get('ALLOC_B',0):>12,}  "
          f"({vals.get('ALLOC_B',0)/1e6:.1f} MB / pass)")
    print()
    # Where do the bytes go? attribute the big buckets.
    clone_b = vals.get("SRCCLONE_B", 0)
    total_b = vals.get("ALLOC_B", 1)
    print(f"  full-source clone share of bytes       : {100*clone_b/total_b:5.1f}%")
    print()
    return vals


# ------------------------------------------------------------ micro-benchmarks

def _micro_crate_src(corpus_big: str, big_charat_calls: int, avg_depth: int,
                     slice_calls: int, avg_slice_len: int, srcclone_calls: int,
                     push_elems: int):
    lit = rb._rust_str_literal(corpus_big)
    return f"""
use std::time::Instant;
#[derive(Clone)]
struct Tok {{ kind: String, text: String, line: i64 }}

fn med(mut v: Vec<f64>) -> f64 {{ v.sort_by(|a,b| a.partial_cmp(b).unwrap()); v[v.len()/2] }}

fn main() {{
    let src: String = {lit}.to_string();
    let n = src.chars().count() as i64;
    let big_charat: u64 = {big_charat_calls};
    let avg_depth: i64 = {avg_depth};
    let slice_calls: u64 = {slice_calls};
    let avg_slice: i64 = {avg_slice_len};
    let srcclone_calls: u64 = {srcclone_calls};
    let reps = 30usize;

    // (A) front-walk charAt exactly as emitted: chars().nth(i).unwrap().to_string()
    // priced at the observed frequency and average index depth.
    let mut a = Vec::new();
    for _ in 0..reps {{
        let t = Instant::now();
        let mut sink: u64 = 0;
        for _ in 0..big_charat {{
            let i = (avg_depth as usize).min((n as usize).saturating_sub(1));
            let c = src.chars().nth(i).unwrap().to_string();
            sink = sink.wrapping_add(c.len() as u64);
        }}
        if sink == 0xdeadbeef {{ println!("x"); }}
        a.push(t.elapsed().as_secs_f64()*1e3);
    }}
    println!("MICRO_CHARAT_NTH_MS {{:.3}}", med(a));

    // (B) same access count against a Vec<char> built ONCE (the item-282 shape):
    // O(n) collect once + O(1) index each.
    let mut b = Vec::new();
    for _ in 0..reps {{
        let t = Instant::now();
        let cs: Vec<char> = src.chars().collect();
        let mut sink: u64 = 0;
        for _ in 0..big_charat {{
            let i = (avg_depth as usize).min(cs.len().saturating_sub(1));
            let c = cs[i].to_string();
            sink = sink.wrapping_add(c.len() as u64);
        }}
        if sink == 0xdeadbeef {{ println!("x"); }}
        b.push(t.elapsed().as_secs_f64()*1e3);
    }}
    println!("MICRO_CHARAT_VECIDX_MS {{:.3}}", med(b));

    // (C) full-source String::clone at the observed frequency.
    let mut c = Vec::new();
    for _ in 0..reps {{
        let t = Instant::now();
        let mut sink: u64 = 0;
        for _ in 0..srcclone_calls {{
            let cl = src.clone();
            sink = sink.wrapping_add(cl.len() as u64);
        }}
        if sink == 0xdeadbeef {{ println!("x"); }}
        c.push(t.elapsed().as_secs_f64()*1e3);
    }}
    println!("MICRO_SRCCLONE_MS {{:.3}}", med(c));

    // (D) revl_slice (chars().skip().take().collect()) at observed frequency/len.
    let mut d = Vec::new();
    for _ in 0..reps {{
        let t = Instant::now();
        let mut sink: u64 = 0;
        for _ in 0..slice_calls {{
            let s: String = src.chars().skip(0).take(avg_slice as usize).collect();
            sink = sink.wrapping_add(s.len() as u64);
        }}
        if sink == 0xdeadbeef {{ println!("x"); }}
        d.push(t.elapsed().as_secs_f64()*1e3);
    }}
    println!("MICRO_SLICE_MS {{:.3}}", med(d));

    // (E) revl_push clone-on-append: price the PUSH_ELEMS Token clones the
    // O(tokens^2) accumulator copy performs (each Token clone = 2 String allocs).
    let push_elems: u64 = {push_elems};
    let mut e = Vec::new();
    for _ in 0..reps {{
        let t = Instant::now();
        let proto = Tok {{ kind: String::from("ident"), text: String::from("identifier_name"), line: 1 }};
        let mut sink: u64 = 0;
        for _ in 0..push_elems {{
            let c = proto.clone();
            sink = sink.wrapping_add(c.text.len() as u64);
        }}
        if sink == 0xdeadbeef {{ println!("x"); }}
        e.push(t.elapsed().as_secs_f64()*1e3);
    }}
    println!("MICRO_PUSHCLONE_MS {{:.3}}", med(e));
}}
"""


def micro_bench(vals, stage, workroot: Path):
    print("=" * 74)
    print("3. MICRO-BENCHMARKS  (each hot op priced at its observed frequency)")
    print("=" * 74)
    if not vals:
        print("  skipped (decomposition did not produce counts)")
        return
    # Use the biggest corpus file (selfhost/lexer.rvl) as the representative
    # 16.8 KB source the helpers walk/clone.
    big = max(stage.corpus, key=len)
    code0_n = vals.get("CODE0_N", 0)
    big_charat = vals.get("NTH_N", 0) - code0_n
    avg_depth = int(vals.get("NTH_IDX", 0) / big_charat) if big_charat else 0
    slice_n = vals.get("SLICE_N", 0)
    avg_slice = int(vals.get("SLICE_CH", 0) / slice_n) if slice_n else 0
    srcclone_n = vals.get("SRCCLONE_N", 0)
    push_elems = vals.get("PUSH_ELEMS", 0)
    src = _micro_crate_src(big, big_charat, avg_depth, slice_n, avg_slice,
                           srcclone_n, push_elems)
    crate = workroot / "micro"
    binname = "revl_prof_micro"
    _write_crate(crate, src, RUSTEMIT.cargo_toml(binname))
    out, err = _build_and_run(crate, binname, "--release")
    if out is None:
        print("  micro build FAILED:\n   " + (err or "").replace("\n", "\n   "))
        return
    got = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 2:
            try:
                got[parts[0]] = float(parts[1])
            except ValueError:
                pass
    print(f"  priced against one whole-corpus pass, big source = "
          f"{len(big)} chars (selfhost/lexer.rvl)")
    print()
    print(f"  (A) charAt front-walk  chars().nth(i).to_string()  "
          f"x{big_charat:,} @ avg depth {avg_depth:,}")
    print(f"      -> {got.get('MICRO_CHARAT_NTH_MS', float('nan')):8.2f} ms / pass")
    print("  (B) same accesses vs a Vec<char> O(1) index (item-282 shape)")
    print(f"      -> {got.get('MICRO_CHARAT_VECIDX_MS', float('nan')):8.2f} ms / pass")
    print(f"  (C) full-source String::clone  x{srcclone_n:,}")
    print(f"      -> {got.get('MICRO_SRCCLONE_MS', float('nan')):8.2f} ms / pass")
    print(f"  (D) revl_slice  x{slice_n:,} @ avg {avg_slice} chars")
    print(f"      -> {got.get('MICRO_SLICE_MS', float('nan')):8.2f} ms / pass")
    print(f"  (E) revl_push clone-on-append: {push_elems:,} Token clones (O(tokens^2))")
    print(f"      -> {got.get('MICRO_PUSHCLONE_MS', float('nan')):8.2f} ms / pass")
    print()
    return got


# ------------------------------------------------------------ driver

def main() -> int:
    print("=" * 74)
    print("revl self-host LEXER native (rust) profile: where the ~17x goes (283)")
    print("=" * 74)
    print(f"machine  : {platform.platform()}")
    print(f"toolchain: {rb._toolchain_line()}")
    print(f"HEAD     : {pyb._git_head()}")
    print(f"cpython lexer run (item 229/266): {CPYTHON_LEXER_MS} ms")
    print()
    reason = rust_runtime_reason()
    if reason is not None:
        print(f"SKIP: cordis-rs not available: {reason}")
        return 0

    stage = lexer_stage()
    module_src = _emit_lexer_source(stage)
    workroot = Path(tempfile.mkdtemp(prefix="revl_profile_lexer_"))
    print(f"workroot : {workroot}")
    print()
    profile_sweep(stage, module_src, workroot)
    vals = decomposition(stage, module_src, workroot)
    micro_bench(vals, stage, workroot)
    print("(crates left in workroot for inspection; safe to delete)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
