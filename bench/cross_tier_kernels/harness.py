#!/usr/bin/env python3
"""Cross-tier cost of kernel-shaped work (issue #2709, second slice).

The first slice (`bench/extern_call_cost.py`) answered what one host extern
call costs on the Python tier. This answers the second question: what does
KERNEL-SHAPED work cost on each emitted tier, against the same work written by
hand in native Rust as the floor?

Seven kernel shapes, each a pure function of a repetition count `n`, built
from the input the shape needs and returning one checksum:

  schema_validate  scan a fixed JSON document, tracking strings and depth
  regex_scan       glob-style scan of a fixed text
  vector_rerank    dot products over eight pinned vectors, argmax
  policy_eval      rules over a request, allowed count
  hash_bytes       djb2 over one pinned string
  simulate         a fixed-point numeric loop, bounded state
  batch_transform  filter and transform a pinned list

The same `kernels.rvl` is emitted to four tiers:

  py     the cordis-py reference tier, plain functions on CPython
  ts     the cordis/TypeScript tier, run by node's type stripping
  rust   the cordis-rs tier, one release crate with the emitted module
  wasm   the cordis-wasm tier, the pure function module run by wasmtime

and the handwritten native Rust reference in `native/` is the floor.

Every number is either process CPU time or a property of the artifact:

  latency_ns_call     process CPU ns per kernel call, from the driver's own
                      clock, so process startup is outside it
  throughput_kcall_s  calls per second implied by the latency
  artifact_bytes      the deployed artifact: emitted module source (py, ts),
                      the release binary (rust), the .wasm module (wasm), the
                      release binary (native)
  cold_start_ms       tier-level: minimum wall time of three warm n=0 runs,
                      so process start plus module load
  peak_memory_kb      maximum resident set of the n>0 run (`/usr/bin/time`)
  loop_overhead_ns    tier-level: this tier's `loop_probe` per call minus the
                      native reference's `loop_probe` per call. It is loop and
                      list-index cost, NOT a call or tier boundary; call
                      overhead is not isolated by this harness
  capability_overhead tier-level: the Python tier's witnessed crossing minus
                      its body, cited from `bench/results/extern-call-cost.md`;
                      the other emitted tiers are pure-kernel calls and do not
                      isolate it

The harness gates every tier's checksum against the native reference BEFORE it
reports a latency. A tier that disagrees is reported as a failure, not as a
number.

Usage:
  python3 bench/cross_tier_kernels/harness.py            # run, print, verify
  python3 bench/cross_tier_kernels/harness.py --write     # refresh the result
  python3 bench/cross_tier_kernels/harness.py --only rust,wasm --keep

Missing toolchains are reported as skipped, once each, and the table says so.
Nothing is measured from a missing tier. `--only <tiers>` always includes the
native reference, and a run with any emitted tier refuses to report rows
without it. `--write` is refused on a partial or failed run.
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
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
BENCH = ROOT / "bench"
RESULT = BENCH / "results" / "cross-tier-kernels.md"
KERNELS_RVL = HERE / "kernels.rvl"

sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(HERE))

import drivers  # noqa: E402

KERNELS = (
    "schema_validate",
    "regex_scan",
    "vector_rerank",
    "policy_eval",
    "hash_bytes",
    "simulate",
    "batch_transform",
)
CONTROL = "loop_probe"
ALL_KERNELS = KERNELS + (CONTROL,)

TIERS = ("native", "py", "ts", "rust", "wasm")
#: Tiers whose drivers report their own process CPU time. The wasm tier is a
#: fresh wasmtime process per invocation, so the parent measures that one.
SELF_TIMED = {"native", "py", "ts", "rust"}

#: One timed call per tier should be long enough to clear the CPU clock's
#: resolution. The wasm tier is a fresh wasmtime process per invocation, so its
#: process CPU delta also contains startup: it needs a longer call before the
#: startup subtraction is below noise.
CALIBRATE_NS = 10_000_000
CALIBRATE_NS_WASM = 100_000_000
CALIBRATE_MAX = 50_000_000
SAMPLES = 3


@dataclass
class Sample:
    value: str
    cpu_ns: float
    wall_s: float
    rss_kb: int | None = None


@dataclass
class Tier:
    name: str
    artifact: Path
    artifact_bytes: int = 0
    reasons: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------
# emitting and building
# --------------------------------------------------------------------------

def _env() -> dict[str, str]:
    env = dict(os.environ)
    parts = [str(ROOT / "src")]
    if env.get("PYTHONPATH"):
        parts.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(parts)
    return env


def _emit(backend: str, out: Path) -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "revl", "emit", "--backend", backend,
         str(KERNELS_RVL)],
        capture_output=True, text=True, env=_env(), timeout=300,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"cannot emit to {backend}: {(proc.stderr or proc.stdout).strip()[:1200]}")
    text = proc.stdout
    if backend == "wasm":
        # The CLI writes section headers as `// === name ===`; the core WAT
        # starts after them.
        text = "\n".join(l for l in text.splitlines() if not l.startswith("// === "))
    out.write_text(text, encoding="utf-8")


def _load_rust_emitter():
    path = ROOT / "backends" / "rust" / "emit.py"
    spec = importlib.util.spec_from_file_location("cross_tier_rust_emit", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _cargo(subcommand: str, cwd: Path, env: dict[str, str], *extra: str):
    offline = subprocess.run(
        ["cargo", subcommand, "--offline", *extra], cwd=cwd, text=True,
        capture_output=True, timeout=1800, env=env,
    )
    if offline.returncode == 0:
        return offline
    blob = offline.stderr or ""
    network_only = any(m in blob for m in (
        "failed to resolve", "no matching package", "not found in registry",
        "network", "locked"))
    if not network_only:
        return offline
    return subprocess.run(
        ["cargo", subcommand, *extra], cwd=cwd, text=True,
        capture_output=True, timeout=1800, env=env,
    )


def _build(work: Path, only: set[str]) -> dict[str, Tier]:
    tiers: dict[str, Tier] = {}
    work.mkdir(parents=True, exist_ok=True)

    if "py" in only:
        d = work / "py"
        d.mkdir(parents=True, exist_ok=True)
        _emit("python", d / "kernels.py")
        (d / "drive.py").write_text(drivers.PY_DRIVER, encoding="utf-8")
        tiers["py"] = Tier("py", d / "kernels.py")

    if "ts" in only:
        d = work / "ts"
        (d / "emitted").mkdir(parents=True, exist_ok=True)
        _emit("typescript", d / "emitted" / "kernels.ts")
        (d / "runtime.ts").write_text(
            f"export * from '{ROOT / 'backends' / 'typescript' / 'runtime.ts'}'\n",
            encoding="utf-8")
        (d / "drive.mjs").write_text(drivers.TS_DRIVER, encoding="utf-8")
        tiers["ts"] = Tier("ts", d / "emitted" / "kernels.ts")

    target = work / "target"
    env = dict(os.environ)
    env["CARGO_TARGET_DIR"] = str(target)

    if "rust" in only:
        d = work / "rust"
        (d / "src").mkdir(parents=True, exist_ok=True)
        _emit("rust", d / "src" / "emitted.rs")
        (d / "src" / "main.rs").write_text(drivers.RUST_MAIN, encoding="utf-8")
        emitter = _load_rust_emitter()
        (d / "Cargo.toml").write_text(
            emitter.cargo_toml("cross_tier_kernels_emitted"), encoding="utf-8")
        built = _cargo("build", d, env, "--release")
        binary = target / "release" / "cross_tier_kernels_emitted"
        if built.returncode != 0 or not binary.exists():
            raise RuntimeError(f"cargo build failed for the rust tier:\n"
                               f"{(built.stderr or built.stdout).strip()[-2000:]}")
        tiers["rust"] = Tier("rust", binary)

    if "native" in only:
        d = work / "native"
        shutil.copytree(HERE / "native", d, dirs_exist_ok=True)
        built = _cargo("build", d, env, "--release")
        binary = target / "release" / "cross_tier_kernels_native"
        if built.returncode != 0 or not binary.exists():
            raise RuntimeError(f"cargo build failed for the native reference:\n"
                               f"{(built.stderr or built.stdout).strip()[-2000:]}")
        tiers["native"] = Tier("native", binary)

    if "wasm" in only:
        d = work / "wasm"
        d.mkdir(parents=True, exist_ok=True)
        wat = d / "kernels.wat"
        _emit("wasm", wat)
        wasm = d / "kernels.wasm"
        parsed = subprocess.run(
            ["wasm-tools", "parse", str(wat), "-o", str(wasm)],
            capture_output=True, text=True, timeout=300)
        if parsed.returncode != 0:
            raise RuntimeError(f"wasm-tools parse failed: {parsed.stderr.strip()[:1200]}")
        tiers["wasm"] = Tier("wasm", wasm)

    for tier in tiers.values():
        tier.artifact_bytes = tier.artifact.stat().st_size
    return tiers


# --------------------------------------------------------------------------
# running
# --------------------------------------------------------------------------

def _cmd(tier: str, tiers: dict[str, Tier], kernel: str, n: int) -> list[str]:
    t = tiers[tier]
    if tier == "py":
        return [sys.executable, str(t.artifact.parent / "drive.py"),
                str(t.artifact), kernel, str(n)]
    if tier == "ts":
        return ["node", str(t.artifact.parents[1] / "drive.mjs"), kernel, str(n)]
    if tier in ("rust", "native"):
        return [str(t.artifact), kernel, str(n)]
    if tier == "wasm":
        return ["wasmtime", "run", "--invoke", kernel, str(t.artifact), str(n)]
    raise AssertionError(tier)


def _child_cpu() -> float:
    import resource
    r = resource.getrusage(resource.RUSAGE_CHILDREN)
    return r.ru_utime + r.ru_stime


def _value_of(tier: str, stdout: str) -> tuple[str, float]:
    if tier in SELF_TIMED:
        data = json.loads(stdout.strip().splitlines()[-1])
        return str(data["value"]), float(data["cpu_ns"])
    # wasm: `wasmtime run --invoke k f.wasm n` prints the value on stdout.
    return stdout.strip().splitlines()[-1], -1.0


def _invoke(tier: str, tiers: dict[str, Tier], kernel: str, n: int) -> Sample:
    cmd = _cmd(tier, tiers, kernel, n)
    before = _child_cpu() if tier not in SELF_TIMED else 0.0
    t0 = time.perf_counter()
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    wall = time.perf_counter() - t0
    if proc.returncode != 0:
        raise RuntimeError(
            f"{tier} {kernel} n={n} exited {proc.returncode}:\n"
            f"{(proc.stderr or proc.stdout).strip()[-1200:]}")
    value, cpu_ns = _value_of(tier, proc.stdout)
    if tier not in SELF_TIMED:
        cpu_ns = max(0.0, (_child_cpu() - before) * 1e9)
    return Sample(value=value, cpu_ns=cpu_ns, wall_s=wall)


_TIME_RSS = re.compile(r"(maximum resident set size|Maximum resident set size)")
_TIME_KB = re.compile(r"Maximum resident set size \(kbytes\):\s*(\d+)")


def _peak_rss_kb(tier: str, tiers: dict[str, Tier], kernel: str, n: int) -> int | None:
    cmd = _cmd(tier, tiers, kernel, n)
    time_bin = "/usr/bin/time"
    if not Path(time_bin).exists():
        return None
    flag = "-l" if platform.system() == "Darwin" else "-v"
    proc = subprocess.run([time_bin, flag, *cmd], capture_output=True, text=True,
                          timeout=1800)
    if proc.returncode != 0:
        return None
    err = proc.stderr
    m = _TIME_KB.search(err)
    if m:
        return int(m.group(1))
    for line in err.splitlines():
        if _TIME_RSS.search(line):
            parts = line.split()
            for part in parts:
                if part.isdigit():
                    value = int(part)
                    # macOS reports bytes, GNU reports kbytes (matched above).
                    return value // 1024 if platform.system() == "Darwin" else value
    return None


def _calibrate(tiers: dict[str, Tier]) -> dict[tuple[str, str], int]:
    """Pick `n` per (tier, kernel) so one timed call clears the CPU clock.

    Tiers are calibrated independently because their per-call costs differ by
    two orders of magnitude and the wasm tier must dominate its process startup.
    The checksum is still checked against the native reference at the SAME `n`,
    so the per-call figures compare even when the row's `n` differs.
    """
    chosen: dict[tuple[str, str], int] = {}
    for tier, _built in tiers.items():
        target = CALIBRATE_NS_WASM if tier == "wasm" else CALIBRATE_NS
        for kernel in ALL_KERNELS:
            n = 1000
            while n <= CALIBRATE_MAX:
                sample = _invoke(tier, tiers, kernel, n)
                if sample.cpu_ns >= target:
                    break
                n *= 2
            chosen[(tier, kernel)] = n
    return chosen


def measure(tiers: dict[str, Tier], ns: dict | None = None,
            samples: int = SAMPLES) -> dict:
    if "native" not in tiers:
        raise RuntimeError(
            "the native reference is required for every emitted row: the "
            "agreement gate cannot run without it")
    ns = ns or _calibrate(tiers)

    # Cold start and the wasm startup subtraction are properties of the tier
    # runtime, not of one kernel. Measure each once, as the minimum of three
    # warm n=0 runs: the minimum is the closest this host gets to an idle
    # start and the wall clock is the only number here that load can move.
    cold_start_ms: dict[str, float] = {}
    startup_cpu_ns: dict[str, float] = {}
    for tier in tiers:
        _invoke(tier, tiers, CONTROL, 0)
        zeros = [_invoke(tier, tiers, CONTROL, 0) for _ in range(3)]
        cold_start_ms[tier] = min(z.wall_s for z in zeros) * 1e3
        startup_cpu_ns[tier] = statistics.median(z.cpu_ns for z in zeros)

    reference: dict[tuple[str, int], str] = {}
    rows = []
    skipped: list[str] = []
    for kernel in ALL_KERNELS:
        for tier in TIERS:
            if tier not in tiers:
                skipped.append(tier)
                continue
            n = ns[(tier, kernel)]
            try:
                runs = [_invoke(tier, tiers, kernel, n) for _ in range(samples)]
            except Exception as exc:  # noqa: BLE001
                rows.append({"kernel": kernel, "tier": tier, "error": str(exc)})
                continue
            values = {run.value for run in runs}
            if len(values) != 1:
                rows.append({"kernel": kernel, "tier": tier,
                             "error": f"nondeterministic checksum: {sorted(values)}"})
                continue
            value = runs[0].value
            if tier != "native":
                key = (kernel, n)
                if key not in reference:
                    reference[key] = _invoke("native", tiers, kernel, n).value
                if value != reference[key]:
                    rows.append({"kernel": kernel, "tier": tier,
                                 "error": f"disagrees with native: {value} != {reference[key]}"})
                    continue
            cpus = [run.cpu_ns for run in runs]
            if tier not in SELF_TIMED:
                # One wasmtime process per invocation: the parent CPU delta
                # includes startup and instantiation, measured above.
                cpus = [max(0.0, cpu - startup_cpu_ns[tier]) for cpu in cpus]
            cpu = statistics.median(cpus)
            if cpu <= 0:
                rows.append({"kernel": kernel, "tier": tier,
                             "error": "CPU delta is below the clock resolution"})
                continue
            rss = _peak_rss_kb(tier, tiers, kernel, n)
            rows.append({
                "kernel": kernel,
                "tier": tier,
                "n": n,
                "value": value,
                "latency_ns_call": cpu / n,
                "throughput_call_s": 1e9 / (cpu / n),
                "artifact_bytes": tiers[tier].artifact_bytes,
                "cold_start_ms": cold_start_ms[tier],
                "peak_memory_kb": rss,
            })
    return {"rows": rows, "n": ns, "skipped": skipped, "samples": samples}


def _null_latency(rows: list[dict], tier: str) -> float | None:
    for row in rows:
        if row.get("kernel") == CONTROL and row.get("tier") == tier \
                and "latency_ns_call" in row:
            return row["latency_ns_call"]
    return None


def _capability_overhead_ns() -> float | None:
    """The Python tier's witnessed crossing over its body.

    Recomputed when cordis-py is importable; otherwise the committed
    `results/extern-call-cost.md` row is the cited source, not a fresh number.
    """
    try:
        sys.path.insert(0, str(BENCH))
        import extern_call_cost  # noqa: PLC0415
        m = extern_call_cost.measure(iters=300, samples=2)
        return m["witnessed extern"] - m["direct body (witnessed)"]
    except Exception:  # noqa: BLE001
        pass
    path = BENCH / "results" / "extern-call-cost.md"
    if not path.exists():
        return None
    values: dict[str, float] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        m = re.match(r"\|\s*([^|]+?)\s*\|\s*([\d,]+)\s*\|", line)
        if m:
            values[m.group(1)] = float(m.group(2).replace(",", ""))
    if "witnessed extern" in values and "direct body (witnessed)" in values:
        return values["witnessed extern"] - values["direct body (witnessed)"]
    return None


def _decorate(rows: list[dict]) -> None:
    native = _null_latency(rows, "native")
    capability = _capability_overhead_ns()
    for row in rows:
        if "latency_ns_call" not in row:
            continue
        tier = row["tier"]
        null = _null_latency(rows, tier)
        if tier == "native":
            row["loop_overhead_ns"] = 0.0
        elif null is not None and native is not None:
            # The native reference is the floor, but it is a reference, not a
            # hard lower bound: an emitted control that lands below it by
            # machine noise is reported at the floor rather than as a negative
            # overhead.
            row["loop_overhead_ns"] = max(0.0, null - native)
        else:
            row["loop_overhead_ns"] = None
        if tier == "py":
            row["capability_overhead_ns"] = capability
        elif tier == "native":
            row["capability_overhead_ns"] = 0.0
        else:
            row["capability_overhead_ns"] = None


# --------------------------------------------------------------------------
# reporting
# --------------------------------------------------------------------------

def _toolchain() -> list[str]:
    lines = [f"python {platform.python_version()} on {platform.platform()}"]
    for name, cmd in (
        ("node", ["node", "--version"]),
        ("rustc", ["rustc", "--version"]),
        ("wasmtime", ["wasmtime", "--version"]),
        ("wasm-tools", ["wasm-tools", "--version"]),
    ):
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if out.returncode == 0:
                first = out.stdout.strip().splitlines()[0]
                lines.append(first if first.startswith(name) else f"{name} {first}")
        except (OSError, subprocess.SubprocessError):
            next
    return lines


def _fmt(value, digits: int = 1) -> str:
    if value is None:
        return "n/a"
    return f"{value:,.{digits}f}"


def render(result: dict) -> str:
    rows = [r for r in result["rows"] if "latency_ns_call" in r]
    errors = [r for r in result["rows"] if "error" in r]
    lines = [
        "# Cross-tier cost of kernel-shaped work (issue #2709)",
        "",
        "From `python3 bench/cross_tier_kernels/harness.py`; the committed copy",
        "is refreshed with `--write`.",
        "",
        "Toolchain: " + "; ".join(_toolchain()) + ".",
        "",
        "Seven kernel shapes from `kernels.rvl` on each emitted tier, against the",
        "handwritten native Rust reference in `native/` as the floor. Every tier's",
        "checksum is compared with the native reference before a latency is",
        "reported; a disagreement fails the row rather than becoming a number.",
        "",
        "Latency is process CPU time per call, so a machine running other work",
        "moves wall time and not this column. `n` is calibrated per tier and",
        "kernel, given in the last column: the checksum is still compared with",
        "the native reference at the same `n`, so the per-call figures compare.",
        "",
        f"Calibration: one timed call takes about {CALIBRATE_NS // 1_000_000} ms on the",
        f"self-timed tiers and {CALIBRATE_NS_WASM // 1_000_000} ms on wasm (one wasmtime",
        f"process per invocation); {result.get('samples', SAMPLES)} samples,",
        "median. Cold start is the",
        "minimum of three warm n=0 runs per tier; peak memory comes from",
        "`/usr/bin/time` around the timed run.",
        "",
        "The host was not idle when this ran. Latency is CPU time and so is mostly",
        "load-independent, but CPU frequency still moves it: treat one row's",
        "absolute nanoseconds as a snapshot, read the tier ratios, and rerun on an",
        "idle host before acting on a single row.",
        "",
        "| kernel | tier | latency ns/call | throughput calls/s | artifact bytes | cold start ms | peak memory KB | loop overhead ns | capability overhead ns | n |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    order = {tier: i for i, tier in enumerate(TIERS)}
    for row in sorted(rows, key=lambda r: (ALL_KERNELS.index(r["kernel"]),
                                           order.get(r["tier"], 99))):
        kernel = row["kernel"] + (" (control)" if row["kernel"] == CONTROL else "")
        lines.append(
            f"| {kernel} | {row['tier']} | {_fmt(row['latency_ns_call'])} | "
            f"{_fmt(row['throughput_call_s'], 0)} | "
            f"{row['artifact_bytes']:,} | {_fmt(row['cold_start_ms'])} | "
            f"{_fmt(row['peak_memory_kb'], 0)} | "
            f"{_fmt(row['loop_overhead_ns'])} | "
            f"{_fmt(row['capability_overhead_ns'])} | {row['n']:,} |")
    if errors:
        lines += ["", "## Failed rows", ""]
        for row in errors:
            lines.append(f"- {row['kernel']} on {row['tier']}: {row['error']}")
    if result["skipped"]:
        lines += ["", "Skipped tiers (not measured): "
                  + ", ".join(sorted(set(result["skipped"]))) + "."]
    lines += [
        "",
        "## Reading the table",
        "",
        "`artifact bytes` is the deployed artifact, which is not the same kind of",
        "object across tiers: emitted module source for py and ts, the release",
        "binary (driver included) for rust and native, and the .wasm module for",
        "wasm. Compare it within a tier's row pair, not across languages.",
        "",
        "`loop overhead ns` is tier-level: the tier's control (`loop_probe`) per",
        "call minus the native control's per call, floored at 0. It is the emitted",
        "loop and list-index cost over the native loop, not a claim about one",
        "kernel.",
        "",
        "Call overhead across a host, tier or capability boundary is not isolated",
        "by this harness. The control crosses no boundary; `bench/extern_call_cost.py`",
        "measures the Python tier's extern crossing, and a witnessed call path on",
        "the other tiers would need its own runtime harness.",
        "",
        "`capability overhead ns` is tier-level too. The Python tier's number is",
        "the witnessed extern crossing minus its host body, from",
        "[results/extern-call-cost.md](extern-call-cost.md); a pure kernel call",
        "does not pay it. The other emitted tiers run pure kernels here and do not",
        "isolate a witnessed call path, so their cell says n/a rather than",
        "borrowing the Python number.",
        "",
        "The decision rule these numbers serve, from [bench/README.md](../README.md):",
        "native work is only justified when the gain survives call overhead. Under",
        "20% over the emitted path is not worth a tier boundary; 5x to 10x is;",
        "anything between needs the call overhead subtracted before it counts.",
        "",
        "The ratio is the emitted path against the floor, not a verdict that a",
        "native kernel is the right repair. Where the gap is a shape the emitter",
        "writes badly, the repair belongs in the emitter: the Rust tier lowers",
        "`Str.charAt` to `chars().nth(i)`, so the string-scan kernels are quadratic",
        "there and their ratio is an emitter cost, not a property of native code.",
        "The codegen benches under `bench/codegen/` are where that belongs.",
        "",
        "## Findings",
        "",
    ]
    lines += _findings(rows)
    return "\n".join(lines) + "\n"


def _findings(rows: list[dict]) -> list[str]:
    by = {(r["kernel"], r["tier"]): r for r in rows}
    out: list[str] = []
    if not any(t == "native" for _k, t in by):
        return ["The native reference is not among the measured tiers, so there is no floor to compare."]
    ratios = {}
    for kernel in KERNELS:
        base = by.get((kernel, "native"))
        if not base:
            continue
        ratios[kernel] = {
            tier: by[(kernel, tier)]["latency_ns_call"] / base["latency_ns_call"]
            for tier in TIERS
            if (kernel, tier) in by and base["latency_ns_call"] > 0
        }
    if ratios:
        for tier in TIERS:
            if tier == "native":
                continue
            values = [r[tier] for r in ratios.values() if tier in r]
            if values:
                out.append(f"- {tier}: median {statistics.median(values):.1f}x the "
                           f"native floor across the seven kernels "
                           f"(range {min(values):.1f}x to {max(values):.1f}x).")
    null = {t: by[(CONTROL, t)]["latency_ns_call"] for t in TIERS
            if (CONTROL, t) in by and "latency_ns_call" in by[(CONTROL, t)]}
    if null:
        out.append("- control `loop_probe` per call: " + ", ".join(
            f"{tier} {_fmt(value)} ns" for tier, value in null.items()) + ".")
    return out


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def _requested(only: str | None) -> set[str]:
    if not only:
        return set(TIERS)
    wanted = {part.strip() for part in only.split(",") if part.strip()}
    unknown = wanted - set(TIERS)
    if unknown:
        raise SystemExit(f"unknown tier(s): {', '.join(sorted(unknown))}")
    if wanted - {"native"}:
        # The native reference is the floor and the agreement oracle. It is not
        # optional when an emitted tier is measured: a row without a checksum to
        # agree with is not a measurement.
        wanted.add("native")
    return wanted


def _reportable(tiers: dict[str, Tier], failed: list[dict]) -> tuple[bool, str]:
    """Whether a run may overwrite the committed result.

    A partial run or any failed row keeps the previous result: `--write` must
    not replace a five-tier committed table with a subset or with a row the
    agreement gate refused.
    """
    missing = set(TIERS) - set(tiers)
    if missing:
        return False, "not every tier was measured; missing " + \
            ", ".join(sorted(missing))
    if failed:
        return False, f"{len(failed)} row(s) failed, so the table is not a measurement"
    return True, ""


def _have(requested: set[str]) -> tuple[set[str], list[str]]:
    missing = []
    if "ts" in requested and not shutil.which("node"):
        missing.append("node (ts tier)")
    if "rust" in requested and not shutil.which("cargo"):
        missing.append("cargo (rust tier)")
    if "wasm" in requested and (not shutil.which("wasm-tools")
                                or not shutil.which("wasmtime")):
        missing.append("wasm-tools and wasmtime (wasm tier)")
    if "native" in requested and not shutil.which("cargo"):
        missing.append("cargo (native reference)")
    dropped = set()
    for item in missing:
        if "ts" in item:
            dropped.add("ts")
        if "rust" in item:
            dropped.add("rust")
        if "wasm" in item:
            dropped.add("wasm")
        if "native" in item:
            dropped.add("native")
    return requested - dropped, missing


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--write", action="store_true",
                    help="refresh bench/results/cross-tier-kernels.md")
    ap.add_argument("--only", help="comma-separated tiers: "
                                   + ",".join(TIERS))
    ap.add_argument("--samples", type=int, default=SAMPLES)
    ap.add_argument("--keep", metavar="DIR",
                    help="build and keep the work tree at DIR")
    args = ap.parse_args(argv)

    requested = _requested(args.only)
    available, missing = _have(requested)
    for item in missing:
        print(f"SKIP {item}: not on PATH, its rows are not measured", file=sys.stderr)
    if not available:
        print("nothing to measure: no requested tier has a toolchain", file=sys.stderr)
        return 2
    if available - {"native"} and "native" not in available:
        print("refusing to report emitted rows without the native reference: "
              "the agreement gate cannot run (cargo is required)", file=sys.stderr)
        return 2

    cleanup = args.keep is None
    work = Path(args.keep) if args.keep else Path(tempfile.mkdtemp(prefix="revl-cross-tier-"))
    try:
        work.mkdir(parents=True, exist_ok=True)
        try:
            tiers = _build(work, available)
        except Exception as exc:  # noqa: BLE001
            print(f"cannot build the tiers: {exc}", file=sys.stderr)
            return 2
        ns = _calibrate(tiers)
        result = measure(tiers, ns=ns, samples=args.samples)
        result["skipped"] = sorted(set(result["skipped"]) | set(missing))
        result["samples"] = args.samples
        _decorate(result["rows"])
        table = render(result)
        print(table)
        failed = [r for r in result["rows"] if "error" in r]
        if args.write:
            ok, reason = _reportable(tiers, failed)
            if not ok:
                print(f"refusing --write: {reason}", file=sys.stderr)
                return 1
            RESULT.write_text(table, encoding="utf-8")
            print(f"wrote {RESULT}", file=sys.stderr)
        return 1 if failed else 0
    finally:
        if cleanup:
            shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
