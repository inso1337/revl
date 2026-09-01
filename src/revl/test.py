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
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import types
from pathlib import Path

from ._paths import backends_root

BACKENDS = backends_root()

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


def _without_lifecycle_tests(ir: dict) -> dict:
    """*ir* with its `lifecycle test` blocks removed, keeping the plain `test`
    blocks.

    A `lifecycle test` is not a pure test unit — the wasm emitter refuses one
    by name (it drives a live composition, not a standalone Bool export). To
    lower the *component* modules a lifecycle test drives (and to still emit a
    document's ordinary `test` blocks), the wasm lifecycle driver strips the
    lifecycle section before `emit()`, then runs those tests against the live
    cordis-wasm runtime itself (see ``run_wasm`` / backends/wasm/lifecycle.py).
    """
    tests = ir.get("tests") or []
    if not any(t.get("lifecycle") for t in tests):
        return ir
    stripped = dict(ir)
    stripped["tests"] = [t for t in tests if not t.get("lifecycle")]
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
    """Emit a throwaway crate and run its ``#[test]``s under ``cargo test``.

    rust lowers `timer` steps as of item 99 (schedule/cancel on the clock
    coeffect, residue-accounted), so a timer document routes to real execution
    here like py/ts — no timer follow-on skip. Only wasm/java still refuse
    timers as a documented follow-on (see ``_timer_follow_on``).
    """
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
    as python and TypeScript. A document carrying a `lifecycle test` lowers to
    the live stc-go runtime path instead, so the throwaway module pins the
    same stc-go require the go placement runner and the conformance validator
    use (resolved from the local module cache; FR-5).

    go lowers `timer` steps as of item 99 (schedule/cancel on the clock
    coeffect, residue-accounted), so a timer document routes to real execution
    here like py/ts — no timer follow-on skip. Only wasm/java still refuse
    timers as a documented follow-on (see ``_timer_follow_on``).
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
    go_mod = "module revltest\n\ngo 1.25\n"
    if any(t.get("lifecycle") for t in (ir.get("tests") or [])):
        go_mod += ("\nrequire github.com/0xdenny218/stc-go "
                   "v0.6.1-0.20260818143352-b3d6788a428e\n")
    with tempfile.TemporaryDirectory(prefix="revl_test_go_") as tmpd:
        tmp = Path(tmpd)
        (tmp / "gen_test.go").write_text(source, encoding="utf-8")
        (tmp / "go.mod").write_text(go_mod, encoding="utf-8")
        env = {**os.environ, "GOFLAGS": "-mod=mod"}
        if "stc-go" in go_mod:
            # the module is pinned and cached by the placement runner / the
            # conformance validator; an offline resolve is the honest gate
            env["GOPROXY"] = "off"
        # item 314: run with `-vet=off`. `go test` invokes `go vet` by
        # default, whose `bools` analyzer rejects tautological boolean
        # operands (`a || a`, `a && a`, `false || false`) as "redundant or"/
        # "redundant and". revl LEGITIMATELY admits redundant boolean
        # expressions — the py reference evaluates them and every other tier
        # runs them — so a program admitted by the frontend is failed on go
        # only because a go-specific STYLE lint fires on machine-generated
        # code the compiler itself accepts. The cross-tier contract is "the
        # emitter's output runs", not "it passes go vet's style rules";
        # `-vet=off` restores that contract without distorting revl's
        # semantics to satisfy a linter (architect decision, roadmap 314).
        result = subprocess.run([go, "test", "-vet=off", "./..."], cwd=tmp,
                                capture_output=True, text=True, timeout=600,
                                env=env)
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


def _has_timers(ir: dict) -> bool:
    """True when any component body carries a `timer` step (item 57).

    Timers lower on py + ts, and on go + rust as of item 99 (schedule/cancel on
    the clock coeffect, residue-accounted); only wasm and java still refuse them
    honestly as a documented follow-on (docs/time-coeffect.md). Detecting the
    step here lets those two tiers report a clean "not yet lowerable" skip
    instead of an opaque `unsupported component step` dump from the emitter."""
    def walk(node) -> bool:
        if isinstance(node, dict):
            if node.get("step") == "timer":
                return True
            return any(walk(v) for v in node.values())
        if isinstance(node, list):
            return any(walk(v) for v in node)
        return False
    return any(walk(comp.get("body")) for comp in (ir.get("components") or []))


def _timer_follow_on(tier: str) -> tuple[str, str]:
    """The honest refusal for a tier that cannot yet lower timers (item 57).

    py + ts + go + rust lower timers (go/rust as of item 99); only wasm and java
    still reach this refusal."""
    return ("skip", f"timers (`every`/`after`, item 57) are not yet lowerable on "
                    f"the {tier} tier — they lower on py, ts, go, and rust; a "
                    f"documented follow-on (docs/time-coeffect.md)")


def _lifecycle_refusal(ir: dict, error: Exception) -> bool:
    """True when the emit refusal is the by-design lifecycle-test refusal.

    java and wasm still refuse `lifecycle test` blocks by name (documented
    follow-ups, FR-5); that refusal is a skip-with-reason, never a tier
    failure — exactly the signal `--all`'s verdict column exists for. Any
    other emit error is a real bug and stays a fail.
    """
    return (any(t.get("lifecycle") for t in (ir.get("tests") or []))
            and "lifecycle test" in str(error)
            and "not lowerable" in str(error))


def run_java(ir: dict) -> tuple[str, str]:
    """Compile the emitted cordis4j plugin against the stubs (or the real
    classes on ``REVL_CORDIS4J_CLASSES``) and run ``REVL_TESTS`` on a JVM."""
    if _has_timers(ir):
        return _timer_follow_on("java")
    javac = _java_tool("javac")
    java = _java_tool("java")
    if javac is None or java is None:
        return ("skip", "no working JDK")
    note = _fault_note(ir, "java")
    try:
        source = _emitter("java").emit(_without_fault_tests(ir))
    except Exception as error:  # noqa: BLE001 — an emit refusal is a tier failure
        if _lifecycle_refusal(ir, error):
            return ("skip", "lifecycle tests are a documented follow-up on "
                            f"this tier — {error}")
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


def _wasm_lifecycle_module() -> types.ModuleType:
    """Load backends/wasm/lifecycle.py (the revl-side classifier/spec builder)
    under a unique module name, the same recipe ``_emitter`` uses for emit.py."""
    spec = importlib.util.spec_from_file_location(
        "revl_wasm_lifecycle", BACKENDS / "wasm" / "lifecycle.py")
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_wasm_pure(ir: dict, wasmtime: str, note: str) -> tuple[str, str] | None:
    """Run the document's plain `test` blocks on the wasmtime substrate.

    Each `test` block is an exported zero-arg Bool function (`revl_test_*`,
    true = pass); a failed `assert` traps, which wasmtime reports as a nonzero
    exit — the same recipe backends/wasm/test_v3_emit.py uses. Any `lifecycle
    test` is stripped first (it is driven separately, on the live runtime); its
    presence must not take the pure tests down with an emit refusal. Returns a
    verdict, or ``None`` when the document has no pure tests to emit."""
    pure_ir = _without_lifecycle_tests(_without_fault_tests(ir))
    pure_tests = [t for t in (ir.get("tests") or []) if not t.get("lifecycle")]
    if not pure_tests:
        return None
    try:
        emit = _emitter("wasm")
        modules = emit.emit(pure_ir)
        exports = emit.test_export_names(pure_tests)
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


def _run_wasm_lifecycle(ir: dict, lifecycle: list) -> tuple[str, str]:
    """Drive the document's `lifecycle test` blocks on a live cordis-wasm
    composition (roadmap item 142).

    Mirrors the once-mode driver split (:mod:`revl.run_wasm` on the revl side,
    ``run_harness.py`` on the cordis-wasm side): here we compile + emit the
    component modules (with the lifecycle section stripped, so the emitter does
    not refuse them), decide which tests the substrate can express
    (backends/wasm/lifecycle.py), and hand a wasmtime-bearing cordis-wasm
    interpreter a spec of modules + reduced step scripts to execute
    (``lifecycle_harness.py``). A test the substrate cannot express is skipped
    *with a reason*, per test — never a false pass. A missing cordis-wasm
    runtime is a skip-with-reason for the whole lifecycle portion, exactly as
    the py tier skips a missing cordis-py."""
    from .run_wasm import (  # noqa: PLC0415 — sibling driver; reused, not edited
        _cordis_wasm_dir,
        _cordis_wasm_python,
        wasm_runtime_reason,
    )

    lifecycle_mod = _wasm_lifecycle_module()

    # Classify first — a document may carry only tests the substrate cannot
    # express (config, non-scalar boundaries, timers), in which case we skip
    # honestly without ever needing the runtime.
    runnable: list[dict] = []
    skips: list[str] = []
    for test in lifecycle:
        ok, reason = lifecycle_mod.classify(ir, test)
        if ok:
            runnable.append(test)
        else:
            skips.append(f"{test.get('name')!r}: {reason}")

    # Emit the component modules the runnable tests drive. If the components do
    # not lower (config blocks, host builtins, …) every lifecycle test skips
    # honestly with that emit reason rather than reporting a false pass.
    modules: dict[str, str] = {}
    if runnable:
        try:
            modules = _emitter("wasm").emit(
                _without_lifecycle_tests(_without_fault_tests(ir)))
        except Exception as error:  # noqa: BLE001 — components do not lower
            for test in runnable:
                skips.append(f"{test.get('name')!r}: its components do not lower on "
                             f"the wasm tier — {error}")
            runnable = []

    if not runnable:
        detail = "; ".join(skips) if skips else "no lifecycle tests"
        return ("skip", "lifecycle tests skipped on the wasm substrate — " + detail)

    reason = wasm_runtime_reason()
    if reason is not None:
        note = ""
        if skips:
            note = "; also skipped — " + "; ".join(skips)
        return ("skip", "the cordis-wasm runtime is not available to run "
                        f"{len(runnable)} lifecycle test(s): {reason}" + note)

    component_modules = {c["name"]: modules[c["name"]]
                         for c in (ir.get("components") or [])
                         if c["name"] in modules}
    spec = {
        "name": "lifecycle",
        "modules": component_modules,
        "tests": [lifecycle_mod.build_spec_test(t) for t in runnable],
    }
    harness = BACKENDS / "wasm" / "lifecycle_harness.py"
    env = dict(os.environ)
    env["CORDIS_WASM"] = str(_cordis_wasm_dir())

    passed = 0
    with tempfile.TemporaryDirectory(prefix="revl_test_wasm_lifecycle_") as tmpd:
        spec_file = Path(tmpd) / "lifecycle.spec.json"
        spec_file.write_text(json.dumps(spec), encoding="utf-8")
        result = subprocess.run(
            [_cordis_wasm_python(), str(harness), str(spec_file)],
            capture_output=True, text=True, timeout=600, env=env)
    output = (result.stdout + result.stderr).strip()
    for line in output.splitlines():
        if line.startswith("SUMMARY "):
            try:
                passed = int(line.split()[1])
            except (IndexError, ValueError):  # pragma: no cover — malformed line
                pass
        elif line.startswith(("PASS ", "FAIL ")):
            print(line)

    ran = len(runnable)
    for skip_line in skips:
        print(f"SKIP {skip_line}")
    tail = (f"; {len(skips)} lifecycle test(s) skipped (not wasm-expressible) — "
            + "; ".join(skips)) if skips else ""
    if result.returncode != 0 or passed != ran:
        return ("fail", f"{ran - passed} of {ran} lifecycle test(s) failed on the "
                        f"live cordis-wasm runtime" + tail)
    return ("pass", f"cordis-wasm: {ran} lifecycle test(s) ran (boot -> call -> "
                    f"unload -> no-residue)" + tail)


def _combine_wasm_verdicts(*verdicts: tuple[str, str] | None) -> tuple[str, str]:
    """Fold the pure- and lifecycle-portion verdicts into one tier verdict:
    any failure fails the tier; otherwise a pass if anything ran; else a skip
    (the `--all` verdict semantics — a skip inside a pass is not a regression)."""
    present = [v for v in verdicts if v is not None]
    if not present:  # pragma: no cover — run_wasm only calls with at least one
        return ("pass", "no tests to run")
    messages = "; ".join(m for _, m in present if m)
    if any(o == "fail" for o, _ in present):
        return ("fail", messages)
    if any(o == "pass" for o, _ in present):
        return ("pass", messages)
    return ("skip", messages)


def run_wasm(ir: dict) -> tuple[str, str]:
    """Run the document's `test` and `lifecycle test` blocks on the wasm tier.

    Plain `test` blocks run as exported Bool functions on wasmtime
    (``_run_wasm_pure``); `lifecycle test` blocks — new in item 142 — boot the
    emitted components on the live cordis-wasm runtime, call through provision
    keys, unload LIFO, and check R4/R1 residue (``_run_wasm_lifecycle``),
    instead of the former blanket skip. A test the substrate cannot express
    (a `config` load, a non-scalar service boundary, a timer step) skips *with
    a reason*, never a false pass.
    """
    if _has_timers(ir):
        return _timer_follow_on("wasm")
    wasmtime = shutil.which("wasmtime")
    if wasmtime is None:
        return ("skip", "wasmtime not installed (brew install wasmtime)")
    note = _fault_note(ir, "wasm")
    lifecycle = [t for t in (ir.get("tests") or []) if t.get("lifecycle")]

    pure_verdict = _run_wasm_pure(ir, wasmtime, note)
    if not lifecycle:
        # No lifecycle tests: preserve the original single-verdict behaviour
        # (including the "no tests emitted" pass for a tests-free document).
        return pure_verdict if pure_verdict is not None else (
            "pass", "no tests emitted by the backend" + note)

    lifecycle_verdict = _run_wasm_lifecycle(ir, lifecycle)
    return _combine_wasm_verdicts(pure_verdict, lifecycle_verdict)


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


_CROSS_TIER_CAP = int(os.environ.get("REVL_SWEEP_CAP", "0")) or None


def cross_tier_sweep_command(ir: dict) -> int:
    """`revl test --backend all --sweep`: the fault sweep on every tier.

    Inject the same fault at the same step on every runtime whose toolchain is
    present (py via the real activation interrogation, the compiled/hosted
    tiers via their `--once` boot -> LIFO teardown -> no-residue proof), assert
    each is residue-free at every fault point, and assert the tiers AGREE. A
    tier whose toolchain is absent — or whose `--once` runner cannot yet drive
    a *faulting* activation to a residue proof — is a loud skip with a reason,
    never a false green (docs/fault-tests.md §10).

    Heavy compiled tiers pay an emit+build per fault point, so set
    ``REVL_SWEEP_CAP=N`` to take a representative corpus (first/middle/last step
    per component); a full CI run leaves it unset and sweeps every step.
    """
    from .fault import cross_tier_sweep  # noqa: PLC0415 — lazy: pulls the tier runners

    if not (ir.get("components") or []):
        print("[sweep-all] no components to sweep")
        return 0
    failures, dossier = cross_tier_sweep(ir, cap=_CROSS_TIER_CAP)
    if failures:
        counts = dossier["counts"]
        print(f"[sweep-all] {counts['tiersLeakingResidue']} tier(s) left "
              f"residue, {counts['disagreements']} cross-tier disagreement(s)",
              file=sys.stderr)
        return 1
    if dossier["counts"]["executed"] == 0:
        # every tier loud-skipped: a skip is never a pass, but (like the rest
        # of the cross-tier suite) a toolchain-absent environment exits 0 so a
        # laptop without runtimes is not a red build.
        print("[sweep-all] skipped: no tier could execute the sweep "
              "(see the reasons above)")
    return 0


def schedule_command(ir: dict, seed=None, seeds: int = None) -> int:
    """`revl test --schedule-seed <S>` / `--schedule-seeds <N>`: deterministic
    concurrency / schedule testing (roadmap item 295, docs/design/295-schedule-
    testing.md).

    A `fault test` proves A8/R4 at one failure *point*; the fault sweep sweeps
    every point. This sweeps every *interleaving*: a seeded scheduler drives the
    composition through many orderings of its concurrent lifecycle steps and,
    for each, checks residue-free / no-deadlock / stable-final-state / correct
    teardown / no-use-after-withdrawal. Like the sweep it activates components
    for real on the py reference tier, so a missing cordis-py runtime is a *skip
    with a reason*, never a pass.
    """
    if not (ir.get("components") or []):
        print("[schedule] no components to schedule")
        return 0
    if not _cordis_available():
        print("[schedule] skipped: schedule testing activates components for "
              "real and needs the cordis-py runtime, which this interpreter "
              "does not have\n"
              "        set it up:  sh backends/python/setup.sh\n"
              "        then rerun under that interpreter:  "
              "backends/python/.venv/bin/python -m revl test --schedule-seeds 200")
        return 0

    from .schedule import run_schedules  # noqa: PLC0415 — lazy: needs cordis

    failures, _dossier = run_schedules(ir, seed=seed, seeds=seeds)
    if failures:
        print(f"[schedule] {failures} interleaving(s) violated a property",
              file=sys.stderr)
        return 1
    return 0


def mock_requires_command(ir: dict) -> int:
    """`revl test --mock-requires`: run every `lifecycle test` in mock world
    (docs/auto-mocks.md).

    Every `requires` a loaded component leaves unsatisfied is filled by a
    generated mock — item-37-typed, seeded, deterministic responses; an
    `emission` operation is recorded-not-crossed — so a consumer is
    lifecycle-tested with zero real providers and zero setup code. It runs on
    the py reference tier (the only tier that boots the runtime for a lifecycle
    test), so a missing cordis-py runtime is a *skip with a reason*, never a
    pass.
    """
    from .mocks import lifecycle_tests, run_mock_requires  # noqa: PLC0415 — lazy: needs cordis

    if not lifecycle_tests(ir):
        print("no `lifecycle test` to mock (--mock-requires runs lifecycle tests "
              "against auto-generated mock providers)")
        return 0
    if not _cordis_available():
        print("[mock-requires] skipped: booting a composition in mock world "
              "activates components for real and needs the cordis-py runtime, "
              "which this interpreter does not have\n"
              "        set it up:  sh backends/python/setup.sh\n"
              "        then rerun under that interpreter:  "
              "backends/python/.venv/bin/python -m revl test --mock-requires")
        return 0
    try:
        failures, _total = run_mock_requires(ir)
    except ModuleNotFoundError as error:  # pragma: no cover — guarded above
        print(f"[mock-requires] skipped: the cordis-py runtime is not installed "
              f"({error.name!r} missing — sh backends/python/setup.sh)")
        return 0
    return 1 if failures else 0


def test_command(ir: dict, backend: str, sweep: bool = False,
                 mock_requires: bool = False, schedule_seed=None,
                 schedule_seeds: int = None) -> int:
    """Run the document's `test` blocks on the chosen tier(s); exit code.

    With ``sweep`` set, run the exhaustive fault sweep instead (py tier only —
    the only tier that executes fault tests); ``--backend`` does not apply.

    With ``mock_requires`` set, run every `lifecycle test` in mock world — every
    unmet `requires` satisfied by a generated mock, zero real providers (py tier
    only; docs/auto-mocks.md).

    With ``schedule_seed`` / ``schedule_seeds`` set, run schedule testing — the
    seeded interleaving sweep (py tier only; roadmap item 295,
    docs/design/295-schedule-testing.md).
    """
    if schedule_seed is not None or schedule_seeds is not None:
        if backend not in ("py", "all"):
            print(f"[schedule] note: schedule testing runs on the py reference "
                  f"tier only, not `{backend}` (docs/design/295-schedule-testing.md)")
        return schedule_command(ir, seed=schedule_seed, seeds=schedule_seeds)

    if mock_requires:
        if backend not in ("py", "all"):
            print(f"[mock-requires] note: mock world runs on the py reference "
                  f"tier only, not `{backend}` (docs/auto-mocks.md)")
        return mock_requires_command(ir)

    if sweep:
        if backend == "all":
            return cross_tier_sweep_command(ir)
        if backend != "py":
            print(f"[sweep] note: the single-tier fault sweep runs on the py "
                  f"reference tier, not `{backend}` — use `--backend all "
                  f"--sweep` to sweep every runtime (docs/fault-tests.md)")
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
