"""`revl run --placement`: launch a composition split across processes.

The placement file (TOML/JSON) assigns each component to a named process, and
each process declares its `backend`:

    [config.PgDatabase]
    url = "postgres://primary:5432/app"

    [processes.provider]
    components = ["PgDatabase"]          # backend defaults to "py"

    [processes.consumer]
    backend = "rust"                     # or "py" | "node" | "java" | "go"
    components = ["UserCache"]
    probe = ["cache.put('alice', '42')", "cache.get('alice')"]

The conductor compiles the `.rvl`, checks that the runtimes the placement asks
for are actually installed (the preflight — a missing runtime is one
diagnostic here, not one traceback per child), works out the seams from the IR
(which key each process provides, which it requires from another), assigns a
Unix socket per provider, and spawns one runner per process wired so a key
required across a process boundary is served on one side and proxied on the
other. This is the manifest-driven form of what demo/bridge_pypy.py did by
hand (docs/interop-bridge.md §5: the broker is a placement map plus per-backend
proxy/stub).

Each seam is described to *both* sides from the IR: the consumer's spec carries
the proxy's method list, the provider's carries the same list as the stub's
allowlist, so the served surface is exactly the operations the `service`
declares (G8; docs/interop-bridge.md §3 "Trust model" for what that does and
does not buy).

Backends and their runners:
- py   -> src/revl/_process_runner.py            (cordis-py; reactive)
- node -> backends/typescript/placement_runner.ts (cordis-ts; reactive)
- rust -> backends/rust/placement_runner          (cordis-rs; reactive; the
          proxy/stub/dispatch table is emitted per composition by
          backends/rust/emit.py, so it both consumes and serves)
- java -> backends/java/placement/{Real,}PlacementRunner (real cordis4j on a
          JDK 21 when present — reactive, generic reflection proxy — else the
          in-repo stub runtime, which crosses but does not withdraw)
- go   -> backends/go/placement_runner (cordis-go; reactive; the
          proxy/stub/dispatch table is emitted per composition by
          backends/go/emit.py, so it both consumes and serves)
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from .compiler import compile_files
from .errors import RevlError

KNOWN_BACKENDS = ("py", "node", "rust", "java", "go")

_BACKENDS_DIR = Path(__file__).resolve().parents[2] / "backends"
_TS_DIR = _BACKENDS_DIR / "typescript"
_RUST_RUNNER = _BACKENDS_DIR / "rust" / "placement_runner"
_JAVA_DIR = _BACKENDS_DIR / "java"
_GO_DIR = _BACKENDS_DIR / "go"
_GO_RUNNER = _GO_DIR / "placement_runner"
_PROBE_RE = re.compile(r"^\s*(\w+)\.(\w+)\((.*)\)\s*$")


def _load_placement(path: str) -> dict:
    text = Path(path).read_text(encoding="utf-8")
    if path.endswith(".json"):
        return json.loads(text)
    import tomllib  # noqa: PLC0415; stdlib, py3.11+

    return tomllib.loads(text)


def _snake(name: str) -> str:
    """PascalCase component name -> snake_case cordis-rs plugin fn name
    (matches backends/rust/emit.py: UserCache -> user_cache)."""
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def _parse_probe(expr: str) -> dict:
    """`key.method('a', 'b')` -> {"key","method","args"} for the rust runner,
    whose probes are structured rather than eval'd strings."""
    match = _PROBE_RE.match(expr)
    if not match:
        raise RuntimeError(f"cannot parse probe {expr!r} for the rust backend (use key.method('a', 'b'))")
    key, method, arg_str = match.groups()
    arg_str = arg_str.strip()
    args = [a.strip().strip("'\"") for a in arg_str.split(",")] if arg_str else []
    return {"key": key, "method": method, "args": args}


# --------------------------------------------------------------------------
# preflight: fail with a diagnostic instead of spawning children that die
# --------------------------------------------------------------------------


def _cordis_py_installed() -> bool:
    """Is cordis-py importable by the interpreter that will run py processes?

    Named (rather than inlined) so a test can force the missing case: py
    children are spawned as ``sys.executable -m revl._process_runner``, so the
    conductor's own import environment is exactly the children's."""
    try:
        return importlib.util.find_spec("cordis") is not None
    except (ImportError, ValueError):  # pragma: no cover — a broken install
        return False


def _rerun_hint(files, placement_path: str, once: bool) -> str:
    """The same command, under the backend venv — copy-pasteable verbatim."""
    venv = _BACKENDS_DIR / "python" / ".venv" / "bin" / "python"
    parts = [str(venv), "-m", "revl", "run", *[str(f) for f in files],
             "--placement", placement_path]
    if once:
        parts.append("--once")
    return " ".join(parts)


def _preflight(backends_used: set[str], files, placement_path: str, once: bool) -> str | None:
    """The runtime check `revl run` does, done once before anything is spawned.

    Without it a missing runtime surfaces as one raw traceback per child
    process (`ModuleNotFoundError` from the runner), which says nothing about
    how to fix it. Returns an error message, or None when every backend the
    placement asks for can actually start."""
    if "py" in backends_used and not _cordis_py_installed():
        backend_dir = _BACKENDS_DIR / "python"
        return ("the cordis-py runtime is not installed ('cordis' missing).\n"
                f"       py-placed processes are spawned as {sys.executable},\n"
                "       which has no cordis — each would die with ModuleNotFoundError.\n"
                f"       set it up:  sh {backend_dir / 'setup.sh'}\n"
                f"       then run it under that interpreter:  {_rerun_hint(files, placement_path, once)}")
    if "node" in backends_used:
        if shutil.which("node") is None:
            return ("node is not on PATH, but this placement puts a process on the node backend.\n"
                    "       install Node (>= 22, for --experimental-strip-types), then re-run.")
        if not (_TS_DIR / "node_modules" / "cordis").is_dir():
            return ("the cordis-ts runtime is not installed (backends/typescript/node_modules/cordis missing).\n"
                    f"       set it up:  (cd {_TS_DIR} && npm install)")
    if "java" in backends_used and shutil.which("java") is None and _find_jdk21() is None:
        return ("java is not on PATH, but this placement puts a process on the java backend.\n"
                "       install a JDK (21 for the reactive cordis4j runtime, 17 for the stub),\n"
                "       or point JAVA21_HOME at one.")
    if "rust" in backends_used and shutil.which("cargo") is None:
        return ("cargo is not on PATH, but this placement puts a process on the rust backend.\n"
                "       install a Rust toolchain (https://rustup.rs), then re-run.")
    if "go" in backends_used and shutil.which("go") is None:
        return ("go is not on PATH, but this placement puts a process on the go backend.\n"
                "       install a Go toolchain (>= 1.25, https://go.dev/dl), then re-run.")
    return None


# --------------------------------------------------------------------------
# per-backend build steps (done once, before spawning)
# --------------------------------------------------------------------------


def _emit_ts_module(ir: dict, tmp: Path) -> str:
    """Emit the cordis-ts module for node processes into backends/typescript/
    _gen/ so its `../runtime.ts` / `cordis` imports resolve."""
    gen = _TS_DIR / "_gen"
    gen.mkdir(exist_ok=True)
    ir_json = tmp / "ir.json"
    ir_json.write_text(json.dumps(ir), encoding="utf-8")
    result = subprocess.run([sys.executable, str(_TS_DIR / "emit.py"), str(ir_json)],
                            capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"TS emit failed:\n{result.stderr.strip()}")
    module = gen / f"mod_{tmp.name}.ts"
    module.write_text(result.stdout, encoding="utf-8")
    return str(module)


def _build_java(ir: dict, tmp: Path) -> str:
    """Emit revl/Components.java and compile it + the stubs + PlacementRunner
    into a classes dir; return that dir (the `java -cp` classpath)."""
    out = tmp / "java_out"
    out.mkdir()
    gen = tmp / "java_gen" / "revl"
    gen.mkdir(parents=True)
    spec = importlib.util.spec_from_file_location("revl_java_emit", _JAVA_DIR / "emit.py")
    emit_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(emit_module)
    (gen / "Components.java").write_text(emit_module.emit(ir), encoding="utf-8")

    stubs = [str(p) for p in (_JAVA_DIR / "stubs").rglob("*.java")]
    compile_runner = subprocess.run(
        ["javac", "--release", "17", "-d", str(out), *stubs, str(_JAVA_DIR / "placement" / "PlacementRunner.java")],
        capture_output=True, text=True,
    )
    if compile_runner.returncode:
        raise RuntimeError(f"javac (runner) failed:\n{compile_runner.stderr.strip()}")
    compile_components = subprocess.run(
        ["javac", "--release", "17", "-cp", str(out), "-d", str(out), str(gen / "Components.java")],
        capture_output=True, text=True,
    )
    if compile_components.returncode:
        raise RuntimeError(f"javac (components) failed:\n{compile_components.stderr.strip()}")
    return str(out)


def _build_rust(ir: dict, tmp: Path) -> str:
    """Regenerate the runner's components.rs (proxies/stub/plugin table) from the
    running IR, then cargo build. Regenerating per composition is what makes the
    rust runner general; for user_cache it reproduces the committed module."""
    ir_json = tmp / "rust_ir.json"
    ir_json.write_text(json.dumps(ir), encoding="utf-8")
    emitted = subprocess.run([sys.executable, str(_BACKENDS_DIR / "rust" / "emit.py"), str(ir_json)],
                             capture_output=True, text=True)
    if emitted.returncode:
        raise RuntimeError(f"rust emit failed:\n{emitted.stderr.strip()}")
    (_RUST_RUNNER / "src" / "components.rs").write_text(emitted.stdout, encoding="utf-8")
    build = subprocess.run(["cargo", "build", "--manifest-path", str(_RUST_RUNNER / "Cargo.toml")],
                           capture_output=True, text=True)
    if build.returncode:
        raise RuntimeError(f"cargo build (rust runner) failed:\n{build.stderr.strip()}")
    return str(_RUST_RUNNER / "target" / "debug" / "revl_placement_runner")


def _build_go(ir: dict, tmp: Path) -> str:
    """Emit the go runner's `emitted` package (the ordinary module + the interop
    bridge: proxy/stub/dispatch + runner entry points) from the running IR, then
    `go build`. Regenerating per composition is what makes the go runner general
    — cordis-go services are static Go interfaces, so generality is codegen, not
    a runtime-generic proxy (the same shape the rust runner takes)."""
    spec = importlib.util.spec_from_file_location("revl_go_emit", _GO_DIR / "emit.py")
    emit_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(emit_module)
    try:
        source = emit_module.emit_placement(ir, "emitted")
    except Exception as exc:  # noqa: BLE001 — surface emit failures as one diagnostic
        raise RuntimeError(f"go emit failed:\n{exc}") from exc
    # emit_placement concatenates gen.go and bridge_gen.go with a form-feed
    # sentinel so each file carries its own import block.
    module_src, bridge_src = source.split("\f", 1)
    emitted = _GO_RUNNER / "emitted"
    emitted.mkdir(parents=True, exist_ok=True)
    (emitted / "gen.go").write_text(module_src, encoding="utf-8")
    (emitted / "bridge_gen.go").write_text(bridge_src, encoding="utf-8")
    binary = _GO_RUNNER / "revl_placement_runner"
    build = subprocess.run(["go", "build", "-o", str(binary), "."],
                           cwd=str(_GO_RUNNER), capture_output=True, text=True)
    if build.returncode:
        raise RuntimeError(f"go build (go runner) failed:\n{build.stderr.strip()}")
    return str(binary)


def _find_jdk21() -> str | None:
    """bin dir of a JDK 21 (real cordis4j needs 21), or None."""
    home = os.environ.get("JAVA21_HOME")
    if home and (Path(home) / "bin" / "java").exists():
        return str(Path(home) / "bin")
    for candidate in ("/opt/homebrew/opt/openjdk@21/bin", "/usr/lib/jvm/temurin-21-jdk/bin"):
        if (Path(candidate) / "java").exists():
            return candidate
    java = shutil.which("java")
    if java:
        version = subprocess.run([java, "-version"], capture_output=True, text=True).stderr
        if 'version "21' in version or "version 21" in version:
            return str(Path(java).resolve().parent)
    return None


def _find_cordis4j_classes() -> str | None:
    """A compiled cordis4j-core classes dir (REVL_CORDIS4J_CLASSES or the cached
    checkout the java verifier builds), or None to fall back to the stub runtime."""
    env = os.environ.get("REVL_CORDIS4J_CLASSES")
    if env and Path(env).is_dir():
        return env
    cached = _JAVA_DIR / ".cordis4j-classes"
    return str(cached) if cached.is_dir() else None


def _build_java_real(ir: dict, tmp: Path, jdk_bin: str, cordis_classes: str) -> str:
    """Compile RealPlacementRunner + the emitted module against the real cordis4j
    (JDK 21); return the classes dir. The reactive runtime gives peer-death-as-
    withdrawal, unlike the stub path."""
    out = tmp / "java_real_out"
    out.mkdir()
    gen = tmp / "java_real_gen" / "revl"
    gen.mkdir(parents=True)
    spec = importlib.util.spec_from_file_location("revl_java_emit", _JAVA_DIR / "emit.py")
    emit_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(emit_module)
    (gen / "Components.java").write_text(emit_module.emit(ir), encoding="utf-8")
    compile_result = subprocess.run(
        [str(Path(jdk_bin) / "javac"), "--release", "21", "-cp", cordis_classes, "-d", str(out),
         str(_JAVA_DIR / "placement" / "RealPlacementRunner.java"), str(gen / "Components.java")],
        capture_output=True, text=True,
    )
    if compile_result.returncode:
        raise RuntimeError(f"javac (real cordis4j) failed:\n{compile_result.stderr.strip()}")
    return str(out)


# --------------------------------------------------------------------------
# teardown
# --------------------------------------------------------------------------


def _stop_all(children: dict) -> None:
    """children: name -> (proc, stop_mode). rust holds on stdin (close it to
    stop gracefully); py/node/java tear down on SIGTERM."""
    for proc, stop_mode in children.values():
        if proc.poll() is not None:
            continue
        try:
            if stop_mode == "stdin":
                proc.stdin.close()
            else:
                proc.terminate()
        except OSError:
            pass
    for proc, _ in children.values():
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def run_placement(files, placement_path: str, once: bool = False) -> int:
    try:
        ir = compile_files(files)
    except RevlError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    placement = _load_placement(placement_path)
    processes = placement.get("processes") or {}
    config = placement.get("config") or {}
    if not processes:
        print("error: placement has no [processes]", file=sys.stderr)
        return 1

    components = {c["name"]: c for c in ir.get("components") or []}

    placed: dict[str, str] = {}
    for pname, pconf in processes.items():
        for cname in pconf.get("components") or []:
            if cname not in components:
                print(f"error: process {pname!r} lists unknown component {cname!r}", file=sys.stderr)
                return 1
            if cname in placed:
                print(f"error: component {cname!r} is placed in both {placed[cname]!r} and {pname!r}",
                      file=sys.stderr)
                return 1
            placed[cname] = pname
    unplaced = [c for c in components if c not in placed]
    if unplaced:
        print(f"error: components not placed in any process: {', '.join(unplaced)}", file=sys.stderr)
        return 1

    # runtimes before processes: an unrunnable backend is a diagnostic here,
    # not a traceback per child a second later (an unknown backend name is
    # reported by the spec loop below, so it is not preflighted).
    backends_used = {pconf.get("backend", "py") for pconf in processes.values()} & set(KNOWN_BACKENDS)
    problem = _preflight(backends_used, files, placement_path, once)
    if problem:
        print(f"error: {problem}", file=sys.stderr)
        return 3

    def merged(cnames, which):  # union of components' provides/requires (key -> service)
        out: dict[str, str] = {}
        for cname in cnames:
            out.update(components[cname].get(which) or {})
        return out

    provides = {p: merged(pc.get("components") or [], "provides") for p, pc in processes.items()}
    requires = {p: merged(pc.get("components") or [], "requires") for p, pc in processes.items()}
    owner = {key: p for p, keys in provides.items() for key in keys}
    methods = {name: list((svc.get("methods") or {}).keys()) for name, svc in (ir.get("services") or {}).items()}
    key_service: dict[str, str] = {}
    for comp in components.values():
        key_service.update(comp.get("provides") or {})
        key_service.update(comp.get("requires") or {})
    load_order = (ir.get("manifest") or {}).get("loadOrder") or [c["name"] for c in ir["components"]]

    # 0700 by construction (mkdtemp), so the sockets under it are reachable
    # by this user only; removed again in the `finally` below.
    tmp = Path(tempfile.mkdtemp(prefix="revl_placement_"))
    sockets = {p: str(tmp / f"{p}.sock") for p in processes}

    def abort(message: str, code: int = 1) -> int:
        """Report and bail out without leaving the placement dir behind."""
        print(f"error: {message}", file=sys.stderr)
        shutil.rmtree(tmp, ignore_errors=True)
        return code

    # base specs (backend-neutral)
    specs: dict[str, dict] = {}
    backends: dict[str, str] = {}
    for pname, pconf in processes.items():
        backend = pconf.get("backend", "py")
        if backend not in KNOWN_BACKENDS:
            return abort(f"process {pname!r} has unsupported backend {backend!r} "
                         f"({', '.join(KNOWN_BACKENDS)})")
        backends[pname] = backend
        proxies: dict[str, dict] = {}
        for key, service in requires[pname].items():
            if key in provides[pname]:
                continue
            host = owner.get(key)
            if host is None:
                return abort(f"key {key!r} required by {pname!r} is provided by no process")
            proxies[key] = {"socket": sockets[host], "methods": methods.get(service, []), "service": service}
        serve_keys = [k for k in provides[pname] if any(k in requires[q] and q != pname for q in processes)]
        spec = {
            "name": pname,
            "backend": backend,
            "files": [str(f) for f in files],
            "components": [c for c in load_order if placed.get(c) == pname],
            "config": config,
            "provides": list(provides[pname]),
            "proxies": proxies,
            "probe": pconf.get("probe") or [],
        }
        if serve_keys:
            # `methods` is the stub's allowlist: the operations the *service
            # declaration* admits for each exported key, read off the IR. The
            # stub refuses anything else, so the served surface is exactly the
            # enumerable one (G8), matching what the proxy side forwards.
            spec["serve"] = {
                "socket": sockets[pname],
                "keys": serve_keys,
                "methods": {k: methods.get(provides[pname][k], []) for k in serve_keys},
            }
        specs[pname] = spec

    # per-backend build steps (once)
    cleanup: list[str] = []
    java_mode = "stub"
    java_out = java21_bin = cordis_classes = rust_bin = go_bin = None
    try:
        if any(b == "node" for b in backends.values()):
            ts_module = _emit_ts_module(ir, tmp)
            cleanup.append(ts_module)
            for pname, spec in specs.items():
                if backends[pname] == "node":
                    spec["module"] = ts_module
        if any(b == "java" for b in backends.values()):
            java21_bin = _find_jdk21()
            cordis_classes = _find_cordis4j_classes()
            if java21_bin and cordis_classes:
                java_mode = "real"
                java_out = _build_java_real(ir, tmp, java21_bin, cordis_classes)
            else:
                java_out = _build_java(ir, tmp)
        if any(b == "rust" for b in backends.values()):
            rust_bin = _build_rust(ir, tmp)
        if any(b == "go" for b in backends.values()):
            go_bin = _build_go(ir, tmp)
    except (RevlError, RuntimeError, OSError) as exc:
        return abort(str(exc))

    # per-backend spec adaptation + spawn command + stop mode
    def command_for(pname: str, spec_file: Path) -> tuple[list, dict | None, str]:
        backend = backends[pname]
        if backend == "node":
            return ["node", str(_TS_DIR / "placement_runner.ts"), str(spec_file)], None, "term"
        if backend == "rust":
            return [rust_bin, str(spec_file)], None, "stdin"
        if backend == "go":
            return [go_bin, str(spec_file)], None, "term"
        if backend == "java":
            if java_mode == "real":
                cp = f"{cordis_classes}{os.pathsep}{java_out}"
                return [str(Path(java21_bin) / "java"), "-cp", cp, "RealPlacementRunner", str(spec_file)], None, "term"
            return ["java", "-cp", java_out, "PlacementRunner", str(spec_file)], None, "term"
        return [sys.executable, "-m", "revl._process_runner", str(spec_file)], env, "term"

    import revl  # noqa: PLC0415
    src_dir = str(Path(revl.__file__).resolve().parents[1])
    # keep the inherited PYTHONPATH, but drop empty entries: an empty entry is
    # the *current directory* on the child's sys.path, which would let a stray
    # module in CWD shadow a real one.
    inherited = [p for p in os.environ.get("PYTHONPATH", "").split(os.pathsep) if p]
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([src_dir, *inherited])}

    for pname, spec in specs.items():
        backend = backends[pname]
        if backend == "rust":
            spec["components"] = [_snake(c) for c in spec["components"]]
            spec["probe"] = [_parse_probe(p) for p in spec["probe"]]
        elif backend == "go":
            # go keeps PascalCase component names (RevlLoad switches on them);
            # only probes are structured rather than eval'd strings.
            spec["probe"] = [_parse_probe(p) for p in spec["probe"]]
        elif backend == "java":
            spec["module"] = "revl.Components"
            iface_keys = set(spec["proxies"]) | set(spec.get("serve", {}).get("keys", [])) | set(spec["provides"])
            spec["ifaces"] = {k: f"revl.Components${key_service[k]}" for k in iface_keys if k in key_service}

    summary = "  ".join(f"{p}[{backends[p]}]=[{','.join(processes[p].get('components') or [])}]" for p in processes)
    print(f"placement: {summary}", flush=True)
    if any(b == "java" for b in backends.values()):
        note = ("real cordis4j (reactive)" if java_mode == "real"
                else "stub (non-reactive; set REVL_CORDIS4J_CLASSES + a JDK 21 for reactive withdrawal)")
        print(f"  java runtime: {note}", flush=True)

    children: dict[str, tuple] = {}
    for pname, spec in specs.items():
        spec_file = tmp / f"{pname}.spec.json"
        spec_file.write_text(json.dumps(spec), encoding="utf-8")
        cmd, proc_env, stop_mode = command_for(pname, spec_file)
        proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            env=proc_env, text=True,
        )
        children[pname] = (proc, stop_mode)

    up: set[str] = set()

    def pump(pname: str, proc: subprocess.Popen) -> None:
        for line in proc.stdout:
            sys.stdout.write(line)
            sys.stdout.flush()
            if line.strip() == f"[{pname}] UP":
                up.add(pname)

    threads = [threading.Thread(target=pump, args=(p, proc), daemon=True) for p, (proc, _) in children.items()]
    for thread in threads:
        thread.start()

    rc = 0
    try:
        if once:
            for _ in range(600):  # up to ~60s (java/rust builds already ran; this waits for activation)
                if len(up) == len(children):
                    break
                time.sleep(0.1)
            if len(up) != len(children):
                missing = ", ".join(p for p in children if p not in up)
                print(f"error: processes did not come up: {missing}", file=sys.stderr)
                rc = 1
            _stop_all(children)
        else:
            print("(placement up; Ctrl-C to tear down)", flush=True)
            for proc, _ in children.values():
                proc.wait()
    except KeyboardInterrupt:
        pass
    finally:
        _stop_all(children)
        for thread in threads:
            thread.join(timeout=2)
        for stale in cleanup:
            if os.path.exists(stale):
                os.unlink(stale)
        # the placement dir holds only sockets and spec files, both dead once
        # the children are; leaving it behind leaked a 0700 tmpdir per run.
        shutil.rmtree(tmp, ignore_errors=True)
    return rc
