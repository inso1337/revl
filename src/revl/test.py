"""`revl test` across tiers — roadmap item 4 / §3.

One source's `test` blocks, proven on every backend whose toolchain is
present. ``py`` keeps the original in-process runner; every other tier reuses
that backend's existing execution recipe (the same subprocess shapes the
per-tier suites use). A tier whose toolchain is absent is *skipped with a
reason* — a skipped tier is never reported as passing, and ``--all`` is the
portability assertion: it fails the run if any *available* tier fails.

``--all`` reports one verdict line per tier — ``pass`` / ``skip: reason`` /
``fail`` — and a ``summary: N pass, M skipped, K failed`` line, so a by-design
refusal (a lifecycle test on a tier that does not lower it yet, a missing
toolchain) reads as a skip, never as a regression (FR-5).
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BACKENDS = ROOT / "backends"

_EMITTERS: dict[str, types.ModuleType] = {}


def _emitter(backend: str) -> types.ModuleType:
    """Load a backend emitter under a unique module name (a bare ``import
    emit`` would collide across backends when more than one is loaded)."""
    if backend not in _EMITTERS:
        spec = importlib.util.spec_from_file_location(
            f"revl_{backend}_emit", BACKENDS / backend / "emit.py")
        assert spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _EMITTERS[backend] = module
    return _EMITTERS[backend]


def _fault(ir: dict, module=None) -> list:
    """The document's `fault test` units (docs/fault-tests.md)."""
    from .fault import fault_units  # noqa: PLC0415 — no cordis needed to *read* them

    return fault_units(ir, module)


def _without_fault_tests(ir: dict) -> dict:
    """*ir* minus its fault-test section, for a tier that cannot run them.

    The emitters refuse a document carrying `fault_tests` outright — that is
    the hard guarantee against a silent mis-emit.  A tier runner strips the
    section first and says so, so a document's ordinary `test` blocks still
    get their cross-tier proof instead of being taken down with it.
    """
    if not (ir.get("fault_tests") or []):
        return ir
    stripped = dict(ir)
    stripped.pop("fault_tests", None)
    return stripped


def _fault_note(ir: dict, tier: str) -> str:
    units = ir.get("fault_tests") or []
    note = ""
    if units:
        print(f"[{tier}] note: {len(units)} fault test(s) not run on this tier — "
              f"`fault test` runs on the py reference tier only (docs/fault-tests.md)")
        note += f"; {len(units)} fault test(s) skipped (py tier only)"
    from .fault import roundtrip_units  # noqa: PLC0415 — no cordis needed to count them

    rt = roundtrip_units(ir)
    if rt:
        n = sum(len(u["verified"]) for u in rt)
        print(f"[{tier}] note: {n} verified-effect round-trip(s) not run on this tier — "
              f"inverse round-trip testing runs on the py reference tier only "
              f"(docs/verified-effect.md)")
        note += f"; {n} verified-effect round-trip(s) skipped (py tier only)"
    from .fault import prop_units  # noqa: PLC0415 — no cordis needed to count them

    props = prop_units(ir)
    if props:
        print(f"[{tier}] note: {len(props)} prop test(s) not run on this tier — "
              f"property testing runs on the py reference tier only "
              f"(docs/prop-test.md)")
        note += f"; {len(props)} prop test(s) skipped (py tier only)"
    return note


def _cordis_available() -> bool:
    """Can THIS interpreter import the cordis-py runtime? (Seam for tests —
    monkeypatch to simulate the missing-runtime environment.)"""
    return importlib.util.find_spec("cordis") is not None


_PY_RUNTIME_REMEDY = (
    "preflight: this document's py-tier tests drive the cordis-py runtime "
    "(a `lifecycle test`), which this interpreter does not have\n"
    "       set it up:  sh backends/python/setup.sh\n"
    "       then rerun under that interpreter:  "
    "backends/python/.venv/bin/python -m revl test")


def run_py(ir: dict) -> tuple[str, str]:
    """Exec the cordis-py output in-process (the original runner)."""
    emit = _emitter("python")
    backend_dir = str(BACKENDS / "python")
    if backend_dir not in sys.path:
        sys.path.insert(0, backend_dir)
    from .fault import prop_units, roundtrip_units  # noqa: PLC0415 — no cordis to find them

    prop_entries = prop_units(ir)
    roundtrip_entries = roundtrip_units(ir)
    # A `prop test`'s body is pure — the emitter lowers and runs it standalone
    # (src/revl/fault.py) — so a document that is *only* prop tests has no base
    # module to emit; skip the base emit rather than trip the "no content" guard.
    base_emittable = any(ir.get(section) for section in
                         ("components", "types", "functions", "externs", "tests"))
    module = None
    entries: list = []
    if base_emittable:
        source = emit.emit(ir)
        # Preflight before any test runs. The py emitter imports cordis LAZILY,
        # inside each `lifecycle test` body (not at module scope — a document may
        # mix pure and lifecycle blocks), so exec succeeds on an interpreter
        # without the runtime and the absence used to surface per-test as
        # `FAIL <name>: ModuleNotFoundError ... 0 of N passed` — a stack-shaped
        # verdict for an environment problem whose remedy is one command
        # (findings-uxprobe.md, longest stall).
        if "from cordis import" in source and not _cordis_available():
            # An absent runtime is an environment skip, never a fail: the other
            # tiers treat a missing toolchain the same way, and `--all` must
            # not read as a regression because this interpreter lacks cordis-py
            # (FR-5; the module docstring's "toolchain absent -> skip" contract).
            return ("skip", _PY_RUNTIME_REMEDY)
        module = types.ModuleType("revl_test_module")
        # Register before exec: the emitter renders record types as @dataclass,
        # and dataclasses._process_class resolves each field via
        # sys.modules[cls.__module__] — an unregistered module raises
        # AttributeError on any file that declares a record type (CPython 3.12+).
        sys.modules[module.__name__] = module
        try:
            exec(compile(source, "<revl-test>", "exec"), module.__dict__)
        finally:
            sys.modules.pop(module.__name__, None)
        entries = getattr(module, "REVL_TESTS", None) or []
    fault_entries = _fault(ir, module)
    if not entries and not fault_entries and not roundtrip_entries and not prop_entries:
        return ("pass", "no tests emitted by the backend")

    failures = 0
    for name, test_fn in entries:
        try:
            test_fn()
        except AssertionError as error:
            failures += 1
            message = str(error).strip() or "assertion failed"
            print(f"FAIL {name}: {message}")
        except Exception as error:  # noqa: BLE001 — the runner reports every failure
            failures += 1
            print(f"FAIL {name}: {type(error).__name__}: {error}")
        else:
            print(f"PASS {name}")

    summary = []
    if entries:
        summary.append(f"{len(entries) - failures} of {len(entries)} test(s) passed"
                       if failures else f"{len(entries)} test(s) passed")
    if fault_entries:
        from .fault import run_fault_units  # noqa: PLC0415 — lazy: needs cordis

        try:
            fault_failures, fault_total = run_fault_units(ir, fault_entries)
        except ModuleNotFoundError as error:
            # a fault test drives a real activation, so it needs the runtime
            # the plain `test` blocks do not; missing it is a skip, never a pass
            reason = (f"{len(fault_entries)} fault test(s) skipped "
                      f"(the cordis-py runtime is not installed: {error.name!r} missing — "
                      f"sh backends/python/setup.sh)")
            if not entries:
                return ("skip", reason)
            summary.append(reason)
        else:
            failures += fault_failures
            summary.append(
                f"{fault_total - fault_failures} of {fault_total} fault test(s) passed")

    if roundtrip_entries:
        from .fault import run_roundtrip_units  # noqa: PLC0415 — lazy: needs cordis

        try:
            rt_failures, rt_dossier = run_roundtrip_units(ir, roundtrip_entries)
        except ModuleNotFoundError as error:
            # a round-trip drives a real activation+teardown, so it needs the
            # runtime the plain `test` blocks do not; missing it is a skip
            reason = (f"{len(roundtrip_entries)} verified-effect round-trip(s) skipped "
                      f"(the cordis-py runtime is not installed: {error.name!r} missing — "
                      f"sh backends/python/setup.sh)")
            if not entries and not fault_entries:
                return ("skip", reason)
            summary.append(reason)
        else:
            failures += rt_failures
            rt_total = rt_dossier["counts"]["effects"]
            summary.append(
                f"{rt_total - rt_failures} of {rt_total} verified-effect round-trip(s) held")

    if prop_entries:
        from .fault import run_prop_units  # noqa: PLC0415 — needs only the emitter

        prop_failures, prop_dossier = run_prop_units(ir, prop_entries)
        failures += prop_failures
        prop_total = prop_dossier["counts"]["props"]
        summary.append(
            f"{prop_total - prop_failures} of {prop_total} prop test(s) held")

    if failures:
        return ("fail", "; ".join(summary) or f"{failures} test(s) failed")
    return ("pass", "; ".join(summary))


def run_ts(ir: dict) -> tuple[str, str]:
    """Emit the v3 test blocks and run them under the backend's vitest."""
    vitest = BACKENDS / "typescript" / "node_modules" / ".bin" / "vitest"
    if not vitest.exists():
        return ("skip", "vitest not installed (`cd backends/typescript && npm ci`)")
    note = _fault_note(ir, "ts")
    try:
        source = _emitter("typescript").emit(_without_fault_tests(ir),
                                             runtime_import="../../runtime.ts")
    except Exception as error:  # noqa: BLE001 — an emit refusal is a tier failure
        return ("fail", f"emitter refused: {error}")
    generated = BACKENDS / "typescript" / "tests" / "generated"
    generated.mkdir(parents=True, exist_ok=True)
    path = generated / f"revl_test_{os.getpid()}.test.ts"
    try:
        path.write_text(source, encoding="utf-8")
        result = subprocess.run(
            [str(vitest), "run", str(path)],
            cwd=BACKENDS / "typescript",
            capture_output=True, text=True, timeout=180,
            env={**os.environ, "CI": "1"},
        )
    finally:
        path.unlink(missing_ok=True)
    output = (result.stdout + result.stderr).strip()
    if output:
        print(output)
    if result.returncode != 0:
        return ("fail", f"vitest exited {result.returncode}")
    return ("pass", "vitest: all emitted tests passed" + note)


_CRATES_IO: bool | None = None


def _crates_io_reachable() -> bool:
    """Whether cordis-rs can be resolved. Cached: probed once per run.

    cordis-rs is a crates.io dependency (see backends/rust/emit.cargo_toml);
    an offline resolve fails with "no matching package", so probe once and
    skip with a reason instead of failing after minutes of retries.
    """
    global _CRATES_IO
    if _CRATES_IO is None:
        try:
            socket.create_connection(("index.crates.io", 443), timeout=3).close()
            _CRATES_IO = True
        except OSError:
            _CRATES_IO = False
    return _CRATES_IO


def run_rust(ir: dict) -> tuple[str, str]:
    """Emit a throwaway crate and run its ``#[test]``s under ``cargo test``."""
    if shutil.which("cargo") is None:
        return ("skip", "cargo not installed")
    if not _crates_io_reachable():
        return ("skip", "crates.io unreachable (cordis-rs is resolved from the index)")
    note = _fault_note(ir, "rust")
    try:
        emit = _emitter("rust")
        source = emit.emit(_without_fault_tests(ir))
        cargo_toml = emit.cargo_toml("revl_test")
    except Exception as error:  # noqa: BLE001 — an emit refusal is a tier failure
        return ("fail", f"emitter refused: {error}")
    with tempfile.TemporaryDirectory(prefix="revl_test_rust_") as tmpd:
        tmp = Path(tmpd)
        (tmp / "src").mkdir()
        (tmp / "src" / "lib.rs").write_text(source, encoding="utf-8")
        (tmp / "Cargo.toml").write_text(cargo_toml, encoding="utf-8")
        result = subprocess.run(["cargo", "test"], cwd=tmp,
                                capture_output=True, text=True, timeout=600)
        output = (result.stdout + result.stderr).strip()
    if output:
        print(output)
    if result.returncode != 0:
        return ("fail", f"cargo test exited {result.returncode}")
    return ("pass", "cargo test: all emitted tests passed" + note)


def run_go(ir: dict) -> tuple[str, str]:
    """Emit a throwaway module and run its ``Test*`` funcs under ``go test``.

    The v3 tier is ordinary Go with no dependencies, so unlike rust this needs
    no network — a bare `go.mod` is enough, and the tier is as cheap to execute
    as python and TypeScript.
    """
    go = shutil.which("go")
    if go is None:
        return ("skip", "go not installed")
    note = _fault_note(ir, "go")
    try:
        emit = _emitter("go")
        source = emit.emit(_without_fault_tests(ir))
        if isinstance(source, dict):  # some emitters return a file map
            source = next(iter(source.values()))
    except Exception as error:  # noqa: BLE001 — an emit refusal is a tier failure
        return ("fail", f"emitter refused: {error}")
    with tempfile.TemporaryDirectory(prefix="revl_test_go_") as tmpd:
        tmp = Path(tmpd)
        (tmp / "gen_test.go").write_text(source, encoding="utf-8")
        (tmp / "go.mod").write_text("module revltest\n\ngo 1.25\n", encoding="utf-8")
        result = subprocess.run([go, "test", "./..."], cwd=tmp,
                                capture_output=True, text=True, timeout=600,
                                env={**os.environ, "GOFLAGS": "-mod=mod"})
        output = (result.stdout + result.stderr).strip()
    if output:
        print(output)
    if result.returncode != 0:
        return ("fail", f"go test exited {result.returncode}")
    return ("pass", "go test: all emitted tests passed" + note)


def _java_tool(name: str) -> str | None:
    """A toolchain binary that actually works (macOS ships a `javac` shim
    that errors when no JDK is installed)."""
    exe = shutil.which(name)
    if exe is None:
        return None
    try:
        probe = subprocess.run([exe, "-version"], capture_output=True,
                               text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return exe if probe.returncode == 0 else None


def run_java(ir: dict) -> tuple[str, str]:
    """Compile the emitted cordis4j plugin against the stubs (or the real
    classes on ``REVL_CORDIS4J_CLASSES``) and run ``REVL_TESTS`` on a JVM."""
    javac = _java_tool("javac")
    java = _java_tool("java")
    if javac is None or java is None:
        return ("skip", "no working JDK")
    note = _fault_note(ir, "java")
    try:
        source = _emitter("java").emit(_without_fault_tests(ir))
    except Exception as error:  # noqa: BLE001 — an emit refusal is a tier failure
        return ("fail", f"emitter refused: {error}")
    with tempfile.TemporaryDirectory(prefix="revl_test_java_") as tmpd:
        tmp = Path(tmpd)
        pkg = tmp / "revl"
        pkg.mkdir()
        (pkg / "Components.java").write_text(source, encoding="utf-8")
        out = tmp / "out"
        out.mkdir()

        real_classes = os.environ.get("REVL_CORDIS4J_CLASSES")
        if real_classes:
            compile_components = subprocess.run(
                [javac, "--release", "21", "-cp", str(out) + os.pathsep + real_classes,
                 "-d", str(out), str(pkg / "Components.java")],
                capture_output=True, text=True, timeout=600)
        else:
            stubs = [str(s) for s in sorted((BACKENDS / "java" / "stubs").rglob("*.java"))]
            compile_components = subprocess.run(
                [javac, "--release", "21", "-d", str(out)] + stubs
                + [str(pkg / "Components.java")],
                capture_output=True, text=True, timeout=600)
        if compile_components.returncode != 0:
            return ("fail", f"javac failed: {compile_components.stderr.strip()}")

        runner = tmp / "RunRevlTests.java"
        runner.write_text(
            "public class RunRevlTests {\n"
            "    public static void main(String[] args) {\n"
            "        revl.Components.REVL_TESTS.forEach(Runnable::run);\n"
            "        System.out.println(\"REVL_TESTS_OK\");\n"
            "    }\n"
            "}\n",
            encoding="utf-8")
        compile_runner = subprocess.run(
            [javac, "--release", "21", "-cp", str(out), "-d", str(out), str(runner)],
            capture_output=True, text=True, timeout=600)
        if compile_runner.returncode != 0:
            return ("fail", f"javac runner failed: {compile_runner.stderr.strip()}")
        run = subprocess.run([java, "-cp", str(out), "RunRevlTests"],
                             capture_output=True, text=True, timeout=600)
        run_output = run.stdout

    if run.returncode != 0:
        return ("fail", run.stderr.strip() or f"JVM exited {run.returncode}")
    if "REVL_TESTS_OK" not in run_output:
        return ("fail", "REVL_TESTS did not complete")
    return ("pass", "JVM: all REVL_TESTS ran" + note)


def run_wasm(ir: dict) -> tuple[str, str]:
    """Emit the module with each `test` block as an exported zero-arg Bool
    function (`revl_test_*`, true = pass) and invoke every export on the real
    ``wasmtime`` substrate — the same recipe backends/wasm/test_v3_emit.py
    uses. A failed `assert` traps, which wasmtime reports as a nonzero exit.
    """
    wasmtime = shutil.which("wasmtime")
    if wasmtime is None:
        return ("skip", "wasmtime not installed (brew install wasmtime)")
    note = _fault_note(ir, "wasm")
    try:
        emit = _emitter("wasm")
        modules = emit.emit(_without_fault_tests(ir))
        exports = emit.test_export_names(ir.get("tests") or [])
    except Exception as error:  # noqa: BLE001 — an emit refusal is a tier failure
        return ("fail", f"emitter refused: {error}")
    if not exports:
        return ("pass", "no tests emitted by the backend" + note)
    source = modules.get("functions")
    if source is None:
        return ("fail", "emitter produced no functions module for the tests")

    failures = 0
    with tempfile.TemporaryDirectory(prefix="revl_test_wasm_") as tmpd:
        wat = Path(tmpd) / "mod.wat"
        wat.write_text(source, encoding="utf-8")
        for tname, export in exports:
            run = subprocess.run(
                [wasmtime, "--invoke", export, str(wat)],
                capture_output=True, text=True, timeout=600)
            if run.returncode != 0:
                failures += 1
                detail = (run.stderr.strip().splitlines() or [""])[-1]
                print(f"FAIL {tname}: {detail or f'wasmtime exited {run.returncode}'}")
            else:
                print(f"PASS {tname}")

    if failures:
        return ("fail", f"{failures} of {len(exports)} test(s) failed" + note)
    return ("pass", f"wasmtime: {len(exports)} test(s) passed" + note)


RUNNERS: dict[str, callable] = {
    "py": run_py,
    "ts": run_ts,
    "rust": run_rust,
    "java": run_java,
    "wasm": run_wasm,
    "go": run_go,
}

_TAG = {"pass": "pass", "skip": "skip", "fail": "fail"}


def sweep_command(ir: dict) -> int:
    """`revl test --sweep`: the exhaustive fault sweep (docs/fault-tests.md).

    A `fault test` proves A8/R4 at one author-chosen point; the sweep injects
    failure at *every* top-level step of *every* component and runs the full
    assertion set at each. It runs on the py reference tier — the only tier
    that executes fault tests — so, like a single fault test, a missing
    cordis-py runtime is a *skip with a reason*, never a pass.
    """
    if not _cordis_available():
        print("[sweep] skipped: the fault sweep activates components for real "
              "and needs the cordis-py runtime, which this interpreter does "
              "not have\n"
              "        set it up:  sh backends/python/setup.sh\n"
              "        then rerun under that interpreter:  "
              "backends/python/.venv/bin/python -m revl test --sweep")
        return 0

    from .fault import run_sweep  # noqa: PLC0415 — lazy: needs cordis

    if not (ir.get("components") or []):
        print("[sweep] no components to sweep")
        return 0
    failures, _dossier = run_sweep(ir)
    if failures:
        print(f"[sweep] {failures} step(s) left residue or broke containment",
              file=sys.stderr)
        return 1
    return 0


def test_command(ir: dict, backend: str, sweep: bool = False) -> int:
    """Run the document's `test` blocks on the chosen tier(s); exit code.

    With ``sweep`` set, run the exhaustive fault sweep instead (py tier only —
    the only tier that executes fault tests); ``--backend`` does not apply.
    """
    if sweep:
        if backend not in ("py", "all"):
            print(f"[sweep] note: the fault sweep runs on the py reference "
                  f"tier only, not `{backend}` (docs/fault-tests.md)")
        return sweep_command(ir)

    from .fault import prop_units, roundtrip_units  # noqa: PLC0415 — no cordis to find them

    if (not (ir.get("tests") or []) and not (ir.get("fault_tests") or [])
            and not roundtrip_units(ir) and not prop_units(ir)):
        print("no tests to run")
        return 0

    if backend == "all":
        verdicts = {"pass": 0, "skip": 0, "fail": 0}
        for name, runner in RUNNERS.items():
            outcome, message = runner(ir)
            verdicts[outcome] += 1
            print(f"[{name}] {_TAG[outcome]}: {message}")
        summary = (f"summary: {verdicts['pass']} pass, "
                   f"{verdicts['skip']} skipped, {verdicts['fail']} failed")
        if verdicts["fail"]:
            print(summary, file=sys.stderr)
            print(f"{verdicts['fail']} tier(s) failed", file=sys.stderr)
            return 1
        print(summary)
        print("all tiers passed")
        return 0

    outcome, message = RUNNERS[backend](ir)
    if outcome == "pass":
        print(f"[{backend}] pass: {message}")
        return 0
    print(f"[{backend}] {_TAG[outcome]}: {message}", file=sys.stderr)
    return 1
