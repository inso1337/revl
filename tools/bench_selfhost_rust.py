#!/usr/bin/env python3
"""Native-tier (rust) run of the self-host stages — the item-229 "after" number
(roadmap 266).

`tools/bench_selfhost.py` (item 229) pins only the CPython py-tier overhead: it
compiles each self-host stage to Python via backends/python/emit.py, execs it,
and times it against the hand-written reference. That doc explicitly defers the
meaningful comparison: the same stages emitted to a fast NATIVE tier (rust) vs
CPython. This tool builds that number.

Same stages, same corpus as the py-tier baseline (imported from
`bench_selfhost` so "same" is literal, not a copy that can drift). For each
stage:

  1. Compile selfhost/<stage>.rvl to rust through the reference rust backend
     (backends/rust/emit.py), assemble a runnable cargo binary crate whose
     `main` drives the stage's pure entry point over the corpus, and
     `cargo build --release` it — ONCE, in setup (the one-time build cost is
     reported separately as a note, exactly as the py-tier reports the one-time
     revl->py compile cost).
  2. Run the binary once. The binary itself does the warmup + repeated-pass +
     median timing IN PROCESS with `std::time::Instant` (the same methodology
     `bench_selfhost.time_pass` uses on the py side: warmup passes discarded,
     median of many whole-corpus passes), and prints the median run time. So
     only the RUN is timed, never process startup or the one-time build.
  3. Report the native run time, and rust-vs-CPython (how many times faster the
     native tier is than the CPython py-tier self-host run from the item-229
     baseline).

Honesty gate (mirrors tests/test_run_rust.py's `needs_cordis_rs`): this needs a
resolvable cordis-rs toolchain and a rust backend that can actually emit the
stage. When a stage cannot be measured the tool says exactly why (cargo/cordis
absent, the emitter refusing the stage, or a build/run failure) and leaves the
number unmeasured. It never fabricates a factor.

Run:  python3 tools/bench_selfhost_rust.py
"""

from __future__ import annotations

import importlib.util
import platform
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

# Reuse the EXACT corpus + stage list the py-tier baseline pins, so "same stages,
# same corpus" is literal. Importing the module runs only its top-level defs
# (main() is __main__-guarded), so this has no side effect on the py-tier output.
import bench_selfhost as pyb  # noqa: E402

from revl import compile_files  # noqa: E402
from revl.run_rust import rust_runtime_reason  # noqa: E402


def _load_module(relpath: str, name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relpath)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# The reference rust emitter, loaded by path (as the backend's own tests do), so
# we exercise the file under comparison rather than a re-export.
_RUSTEMIT = _load_module("backends/rust/emit.py", "rustemit_reference_bench")


# --------------------------------------------------------------- CPython baseline
#
# The py-tier self-host RUN time per stage, from the item-229 committed baseline
# (docs/bench-selfhost.md). rust-vs-CPython = this / rust-run. Kept as data, not
# re-timed here, so this tool never re-runs (and cannot perturb) the py tier.
# lexer uses the item-233 "after" number, the current selfhost/lexer.rvl.
CPYTHON_SELFHOST_RUN_MS = {
    "lexer": 20.55,
    "parser": 3.74,
    "checker": 1.41,
    "lower": 2.57,
    "emit_py": 4.93,
}


# ------------------------------------------------------------------- stage table
#
# Each stage's pure entry point (the same function the py-tier bench calls) and
# its corpus. `kind` is how the entry consumes the corpus:
#   "str_in"  — entry(source: Str) over a list of source strings; we sink the
#               length of the (Str or List) result so the call is not elided.
#   "ir_in"   — entry(ir: Any); the input is an IR document, not a string. `Any`
#               erases to cordis::Value on the rust tier and there is no rust-side
#               constructor to feed a real IR, so this stage is not driveable from
#               a generated rust main today (recorded, not faked).

class Stage:
    def __init__(self, name, rvl, entry, kind, corpus, warmup, repeats):
        self.name = name
        self.rvl = rvl
        self.entry = entry
        self.kind = kind
        self.corpus = corpus
        self.warmup = warmup
        self.repeats = repeats


def _lexer_corpus():
    return [(ROOT / rel).read_text(encoding="utf-8") for rel in pyb.LEXER_FILES]


def stages():
    return [
        Stage("lexer", "selfhost/lexer.rvl", "lex_src", "str_in",
              _lexer_corpus(), warmup=3, repeats=15),
        Stage("parser", "selfhost/parser.rvl", "parse_render", "str_in",
              list(pyb.PARSER_EXPRS), warmup=5, repeats=25),
        Stage("checker", "selfhost/checker.rvl", "infer_expr_str", "str_in",
              list(pyb.CHECKER_EXPRS), warmup=5, repeats=25),
        Stage("lower", "selfhost/lower.rvl", "admit_src", "str_in",
              list(pyb.LOWER_PROGRAMS), warmup=3, repeats=20),
        Stage("emit_py", "selfhost/emit_py.rvl", "emit_py_src", "ir_in",
              None, warmup=5, repeats=25),
    ]


# ---------------------------------------------------------------- rust codegen
#
# A generated `fn main()` appended to the emitted stage module. It embeds the
# corpus as string literals, warms up, then times `repeats` whole-corpus passes
# with Instant and prints the median in milliseconds as `MEDIAN_MS <value>`. The
# result of each call is folded into a sink the compiler cannot discard, so the
# stage's work is not optimized away under --release.

def _rust_str_literal(s: str) -> str:
    # A rust raw string with enough `#` fences to clear any run of `"#` in `s`.
    hashes = 0
    while True:
        fence = "#" * hashes
        if f'"{fence}' not in s and f'{fence}"' not in s:
            break
        hashes += 1
    fence = "#" * hashes
    return f'r{fence}"{s}"{fence}'


def _sink_expr(entry: str) -> str:
    # `lex_src` returns Vec<Token>; the string stages return String. Both expose
    # `.len()`, so folding the length into a wrapping xor sink works for either
    # and forces the call to run.
    return f"sink = sink.wrapping_add({entry}(item.clone()).len() as u64);"


def _gen_main(stage: Stage) -> str:
    items = ",\n        ".join(_rust_str_literal(s) for s in stage.corpus)
    return f"""

fn main() {{
    let corpus: Vec<String> = vec![
        {items}
    ].into_iter().map(|s: &str| s.to_string()).collect();
    let warmup = {stage.warmup};
    let repeats = {stage.repeats};
    let mut sink: u64 = 0;
    for _ in 0..warmup {{
        for item in &corpus {{
            {_sink_expr(stage.entry)}
        }}
    }}
    let mut samples: Vec<f64> = Vec::with_capacity(repeats);
    for _ in 0..repeats {{
        let t0 = std::time::Instant::now();
        for item in &corpus {{
            {_sink_expr(stage.entry)}
        }}
        samples.push(t0.elapsed().as_secs_f64() * 1e3);
    }}
    samples.sort_by(|a, b| a.partial_cmp(b).unwrap());
    let median = samples[samples.len() / 2];
    // keep the sink observable so nothing above is dead code
    if sink == 0xdead_beef_dead_beef {{ eprintln!("sink {{}}", sink); }}
    println!("MEDIAN_MS {{:.6}}", median);
}}
"""


# --------------------------------------------------------------- cargo helpers

_OFFLINE_RESOLVE_MARKERS = (
    "you're using offline mode", "without the offline flag",
    "--offline was specified", "registry index was not found",
    "no matching package", "failed to select a version",
)
_REAL_FAILURE_MARKERS = (
    "error[e", "could not compile", "panicked at", "test result: failed",
)


def _crates_io_reachable() -> bool:
    import socket
    try:
        socket.create_connection(("index.crates.io", 443), timeout=3).close()
        return True
    except OSError:
        return False


def _is_offline_resolve_failure(proc) -> bool:
    blob = ((proc.stderr or "") + (proc.stdout or "")).lower()
    if any(m in blob for m in _REAL_FAILURE_MARKERS):
        return False
    return any(m in blob for m in _OFFLINE_RESOLVE_MARKERS)


def _cargo(subcommand: str, cwd: Path, *extra: str):
    """`cargo <subcommand>` — offline first, networked resolve only when the
    offline attempt failed to *resolve* a crate (never to launder a build
    failure into a retry). Same policy as backends/rust/test_emit_rust.py."""
    offline = subprocess.run(
        ["cargo", subcommand, "--offline", *extra], cwd=cwd, text=True,
        capture_output=True, timeout=1200)
    if offline.returncode == 0 or not _is_offline_resolve_failure(offline):
        return offline
    if not _crates_io_reachable():
        return offline
    return subprocess.run(
        ["cargo", subcommand, *extra], cwd=cwd, text=True,
        capture_output=True, timeout=1200)


class StageResult:
    def __init__(self, stage: Stage):
        self.name = stage.name
        self.status = "unmeasured"   # "ok" | "unmeasured"
        self.reason = ""
        self.build_ms = None
        self.run_ms = None


def _build_stage(stage: Stage, workdir: Path) -> StageResult:
    """Emit -> assemble a cargo bin crate -> build -> run -> parse median.
    Returns a StageResult; every failure is recorded with its reason, never
    raised past this function (a stage that cannot be measured must not abort
    the others)."""
    res = StageResult(stage)

    if stage.kind == "ir_in":
        res.reason = ("entry takes an IR document (`Any`), which erases to "
                      "cordis::Value on the rust tier; no rust-side IR "
                      "constructor exists to feed it from a generated main")
        # even so, surface whether the module would emit at all
        try:
            _RUSTEMIT.emit(compile_files([str(ROOT / stage.rvl)]))
        except Exception as exc:  # noqa: BLE001
            res.reason = f"cannot emit to rust: {exc}"
        return res

    # 1) emit the stage module
    try:
        ir = compile_files([str(ROOT / stage.rvl)])
        module_src = _RUSTEMIT.emit(ir)
    except Exception as exc:  # noqa: BLE001  (EmitError and friends)
        res.reason = f"cannot emit to rust: {exc}"
        return res

    # 2) assemble the crate: emitted module + a generated timing main
    crate = workdir / stage.name
    (crate / "src").mkdir(parents=True, exist_ok=True)
    (crate / "src" / "main.rs").write_text(
        module_src + _gen_main(stage), encoding="utf-8")
    (crate / "Cargo.toml").write_text(
        _RUSTEMIT.cargo_toml(f"revl_bench_{stage.name}"), encoding="utf-8")

    # 3) build once (setup only); time the one-time cost as a note
    t0 = time.perf_counter()
    built = _cargo("build", crate, "--release")
    res.build_ms = (time.perf_counter() - t0) * 1e3
    if built.returncode != 0:
        res.reason = ("cargo build failed:\n"
                      + (built.stderr or built.stdout or "").strip()[-1500:])
        return res

    binary = crate / "target" / "release" / f"revl_bench_{stage.name}"
    if not binary.exists():
        res.reason = f"release binary not found at {binary}"
        return res

    # 4) run once; the binary does warmup + median internally (only the run is
    #    timed, in process, with Instant)
    run = subprocess.run([str(binary)], text=True, capture_output=True,
                         timeout=600)
    if run.returncode != 0:
        res.reason = ("stage binary exited nonzero:\n"
                      + (run.stderr or run.stdout or "").strip()[-1500:])
        return res
    m = re.search(r"MEDIAN_MS\s+([0-9.]+)", run.stdout)
    if not m:
        res.reason = f"no MEDIAN_MS in stage output: {run.stdout.strip()[:400]}"
        return res
    res.run_ms = float(m.group(1))
    res.status = "ok"
    return res


def _toolchain_line() -> str:
    def ver(cmd):
        try:
            return subprocess.check_output(cmd, text=True).strip().splitlines()[0]
        except Exception:  # noqa: BLE001
            return "unavailable"
    return f"{ver(['cargo', '--version'])} / {ver(['rustc', '--version'])}"


def main() -> int:
    print("=" * 74)
    print("revl self-host compiler — native tier (rust) run vs the CPython baseline")
    print("=" * 74)
    print(f"machine  : {platform.platform()}")
    print(f"toolchain: {_toolchain_line()}")
    print(f"HEAD     : {pyb._git_head()}")
    print()

    reason = rust_runtime_reason()
    if reason is not None:
        print("SKIP: the cordis-rs runtime is not available here, so no native")
        print("      number can be measured (never faked). Reason:")
        print(f"      {reason}")
        print()
        print("This is the same gate tests/test_run_rust.py's `needs_cordis_rs`")
        print("skips on. Re-run where cargo + cordis-rs resolve.")
        return 0

    print("Each stage: emit to rust -> assemble a cargo bin -> build ONCE (setup)")
    print("-> run once, the binary timing warmup+median passes in process. Only")
    print("the RUN is timed; the one-time build cost is a note.")
    print()

    workdir = Path(tempfile.mkdtemp(prefix="revl_bench_rust_"))
    results = []
    try:
        for stage in stages():
            print(f"  {stage.name:<10} building + running ...", flush=True)
            results.append(_build_stage(stage, workdir))
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    print()
    print("=" * 74)
    hdr = (f"{'stage':<10}{'cpython ms':>12}{'rust ms':>10}"
           f"{'rust vs cpy':>13}  status")
    print(hdr)
    print("-" * 74)
    any_ok = False
    for r in results:
        cpy = CPYTHON_SELFHOST_RUN_MS.get(r.name)
        cpy_s = f"{cpy:>12.2f}" if cpy is not None else f"{'-':>12}"
        if r.status == "ok":
            any_ok = True
            factor = (cpy / r.run_ms) if (cpy and r.run_ms) else None
            fac_s = f"{factor:>11.1f}x" if factor else f"{'-':>12}"
            print(f"{r.name:<10}{cpy_s}{r.run_ms:>10.3f}{fac_s}  ok")
        else:
            print(f"{r.name:<10}{cpy_s}{'-':>10}{'-':>13}  unmeasured")
    print("=" * 74)
    print()
    print("Per-stage status detail:")
    for r in results:
        if r.status == "ok":
            note = f"run {r.run_ms:.3f} ms"
            if r.build_ms:
                note += f"  (one-time cargo build {r.build_ms / 1e3:.1f}s)"
            print(f"  {r.name:<10} ok — {note}")
        else:
            print(f"  {r.name:<10} unable to measure in this environment:")
            for line in r.reason.splitlines():
                print(f"             {line}")
    print()
    if not any_ok:
        print("No stage could be measured natively. rust-vs-CPython is therefore")
        print("UNMEASURED here — the reference rust backend cannot yet emit the")
        print("self-host stages (see the reasons above). This is an honest skip,")
        print("not a number.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
