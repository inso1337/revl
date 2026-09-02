#!/usr/bin/env python3
"""Emitted-vs-hand-written codegen benchmarks for the java backend.

Each case under ``cases/`` is a triple:

* ``case.rvl``  - the revl program, compiled and emitted by
  ``backends/java/emit.py`` exactly as ``revl build --backend java`` would.
* ``Hand.java`` - the Java a competent Java developer writes by hand for the
  same semantics. It is the yardstick, not a rewrite: it keeps the emitted
  class shape and changes only the thing under audit.
* ``Drive.java`` - a ``bench.Drive`` with ``NAME``/``N``/``WARMUP``/
  ``setup()``/``emitted(int)``/``hand(int)``. ``setup()`` asserts the two
  sides agree before anything is measured, so a benchmark cannot pass by
  computing less.

Two modes:

``--static`` needs no JDK. It reports emitted source bytes and static
allocation-site counts per case, straight out of the emitter.

The default mode compiles and runs ``harness/Bench.java``, which reports
ALLOCATED BYTES PER OP on each side. It reports no timing at all; see
``harness/Bench.java`` for why, and ``--class-sizes`` for the compiled size
of each side.

Usage::

    python3 bench/codegen/java/run.py --static     # no JDK required
    python3 bench/codegen/java/run.py              # every case, needs a JDK
    python3 bench/codegen/java/run.py router       # one case
    python3 bench/codegen/java/run.py --json       # machine-readable

Exits 77 (the automake "skipped" convention) when a JDK-requiring mode is
asked for and no working JDK is present, so a CI job can wire this in without
turning red on a runner that has none.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
STUBS = ROOT / "backends" / "java" / "stubs"
NO_JDK_EXIT = 77

# Java expressions that construct at least one object every time they are
# evaluated. Counting their occurrences in an emitted method body is a static
# lower bound on the objects that method builds per call, which is the
# load-independent shape of the finding even when no JVM is available to
# confirm the byte count.
_ALLOC_SITES = (
    (r"\bnew\s+[\w.]+\s*(?:<[^;()]*>)?\s*[\({\[]", "new"),
    (r"\bjava\.util\.(?:List|Map|Set)\.(?:<[^>]*>)?of\(", "List/Map/Set.of"),
    (r"\bjava\.util\.(?:List|Map|Set)\.copyOf\(", "copyOf"),
    (r"\bString\.format\(", "String.format"),
    (r"\bjava\.util\.Objects\.equals\(", "Objects.equals (boxes both operands)"),
    (r"\bjava\.util\.stream\.Stream\.(?:concat|of)\(", "Stream"),
    (r"\bjava\.util\.Optional\.(?:of|ofNullable)\(", "Optional"),
    (r"->", "lambda / method-ref site"),
)


def _tool(name: str) -> str | None:
    """A toolchain binary that actually works.

    macOS ships ``/usr/bin/javac`` and ``/usr/bin/java`` as shims that exist on
    PATH and fail with "Unable to locate a Java Runtime" when no JDK is
    installed, so presence on PATH proves nothing. Same probe as
    ``backends/java/test_emit_java.py``.
    """
    exe = shutil.which(name)
    if exe is None:
        return None
    try:
        probe = subprocess.run([exe, "-version"], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return exe if probe.returncode == 0 else None


def _emitter():
    sys.path.insert(0, str(ROOT / "src"))
    spec = importlib.util.spec_from_file_location(
        "revl_java_emit", ROOT / "backends" / "java" / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def emit_case(case: Path) -> str:
    module = _emitter()
    from revl import compile_source  # noqa: PLC0415 - needs sys.path from _emitter

    return module.emit(compile_source((case / "case.rvl").read_text(encoding="utf-8")))


def _strip_comments(source: str) -> str:
    source = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    return re.sub(r"^\s*//.*$", "", source, flags=re.M)


def count_alloc_sites(source: str) -> dict[str, int]:
    body = _strip_comments(source)
    counts: dict[str, int] = {}
    for pattern, label in _ALLOC_SITES:
        hits = len(re.findall(pattern, body))
        if hits:
            counts[label] = counts.get(label, 0) + hits
    return counts


def static_case(case: Path) -> dict:
    emitted = emit_case(case)
    hand = (case / "Hand.java").read_text(encoding="utf-8")
    return {
        "case": case.name,
        "emitted_source_bytes": len(emitted.encode("utf-8")),
        "hand_source_bytes": len(hand.encode("utf-8")),
        "emitted_alloc_sites": count_alloc_sites(emitted),
        "hand_alloc_sites": count_alloc_sites(hand),
    }


def run_case(case: Path, javac: str, java: str, class_sizes: bool) -> dict:
    with tempfile.TemporaryDirectory(prefix=f"revl-bench-{case.name}-") as tmp:
        work = Path(tmp)
        pkg = work / "revl"
        pkg.mkdir()
        (pkg / "Components.java").write_text(emit_case(case), encoding="utf-8")

        bench_pkg = work / "bench"
        bench_pkg.mkdir()
        for name in ("Hand.java", "Drive.java"):
            shutil.copy(case / name, bench_pkg / name)
        shutil.copy(HERE / "harness" / "Bench.java", bench_pkg / "Bench.java")

        out = work / "classes"
        out.mkdir()
        sources = (
            sorted(str(p) for p in STUBS.rglob("*.java"))
            + [str(pkg / "Components.java")]
            + sorted(str(p) for p in bench_pkg.glob("*.java"))
        )
        compiled = subprocess.run(
            [javac, "--release", "21", "-nowarn", "-d", str(out), *sources],
            capture_output=True, text=True, timeout=600,
        )
        if compiled.returncode != 0:
            return {"case": case.name, "error": "javac failed", "stderr": compiled.stderr}

        sizes = {}
        if class_sizes:
            sizes = {
                "emitted_class_bytes": sum(
                    p.stat().st_size for p in (out / "revl").glob("Components*.class")),
                "hand_class_bytes": sum(
                    p.stat().st_size for p in (out / "bench").glob("Hand*.class")),
            }

        ran = subprocess.run(
            [java, "-cp", str(out), "bench.Bench"],
            capture_output=True, text=True, timeout=1800,
        )
        if ran.returncode != 0:
            return {"case": case.name, "error": "java failed", "stderr": ran.stderr}
        result = json.loads(ran.stdout.strip().splitlines()[-1])
        result.update(sizes)
        return result


def _ratio(emitted: float, hand: float) -> str:
    if emitted < 0 or hand < 0:
        return "n/a"
    if hand == 0:
        return "inf" if emitted else "1.00"
    return f"{emitted / hand:.2f}"


def _print_static(results: list[dict]) -> None:
    for result in results:
        print(f"== {result['case']}")
        print(f"   emitted source {result['emitted_source_bytes']} B, "
              f"hand {result['hand_source_bytes']} B")
        for label in sorted(set(result["emitted_alloc_sites"]) | set(result["hand_alloc_sites"])):
            emitted = result["emitted_alloc_sites"].get(label, 0)
            hand = result["hand_alloc_sites"].get(label, 0)
            print(f"   {label:<42} emitted {emitted:>3}   hand {hand:>3}")
        print()
    print(
        "Static counts over the whole emitted unit, including runtime helpers the\n"
        "program may never call. They are a shape argument, not a measurement:\n"
        "run without --static, on a machine with a JDK, for allocated bytes per op."
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("cases", nargs="*", help="case names; default is all of them")
    parser.add_argument("--static", action="store_true",
                        help="emitted size and allocation-site counts; needs no JDK")
    parser.add_argument("--class-sizes", action="store_true",
                        help="also report compiled class-file bytes per side")
    parser.add_argument("--json", action="store_true", help="emit raw JSON, one object per line")
    args = parser.parse_args()

    cases = sorted(p for p in (HERE / "cases").iterdir() if p.is_dir())
    wanted = set(args.cases)
    if wanted:
        cases = [c for c in cases if c.name in wanted]
        missing = wanted - {c.name for c in cases}
        if missing:
            print(f"no such case: {', '.join(sorted(missing))}", file=sys.stderr)
            return 2

    if args.static:
        results = [static_case(case) for case in cases]
        if args.json:
            for result in results:
                print(json.dumps(result))
        else:
            _print_static(results)
        return 0

    javac, java = _tool("javac"), _tool("java")
    if javac is None or java is None:
        print(
            "no working JDK on this machine (`java -version` / `javac -version` fail even "
            "though the macOS shims are on PATH), so nothing here was measured. Install a "
            "JDK 21 or newer and re-run, or use --static for the JDK-free counts.",
            file=sys.stderr,
        )
        return NO_JDK_EXIT

    results = [run_case(case, javac, java, args.class_sizes) for case in cases]

    if args.json:
        for result in results:
            print(json.dumps(result))
        return 1 if any("error" in r for r in results) else 0

    header = f"{'case':<16}{'alloc B/op emitted':>20}{'hand':>12}{'emitted/hand':>14}"
    print(header)
    print("-" * len(header))
    for result in results:
        if "error" in result:
            print(f"{result['case']:<16}  {result['error']}")
            print(result.get("stderr", "").rstrip())
            continue
        print(
            f"{result['case']:<16}"
            f"{result['alloc_bytes_emitted']:>20}"
            f"{result['alloc_bytes_hand']:>12}"
            f"{_ratio(result['alloc_bytes_emitted'], result['alloc_bytes_hand']):>14}"
        )
        if args.class_sizes:
            print(f"{'':<16}class bytes: emitted {result['emitted_class_bytes']}, "
                  f"hand {result['hand_class_bytes']}")
    print()
    print(
        "Allocated bytes per op is a count, not a duration: it does not move when\n"
        "another process takes the CPU. No timing is reported here by design; see\n"
        "harness/Bench.java. Time a change on a quiet machine, separately."
    )
    return 1 if any("error" in r for r in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
