"""`revl run --placement`: launch a composition split across processes.

The placement file (TOML/JSON) assigns each component to a named process, and
each process declares its `backend`:

    [config.PgDatabase]
    url = "postgres://primary:5432/app"

    [processes.provider]
    components = ["PgDatabase"]          # backend defaults to "py"

    [processes.consumer]
    backend = "rust"                     # or "py" | "node" | "java"
    components = ["UserCache"]
    probe = ["cache.put('alice', '42')", "cache.get('alice')"]

The conductor compiles the `.rvl`, works out the seams from the IR (which key
each process provides, which it requires from another), assigns a Unix socket
per provider, and spawns one runner per process wired so a key required across
a process boundary is served on one side and proxied on the other. This is the
manifest-driven form of what demo/bridge_pypy.py did by hand (docs/interop-
bridge.md §5: the broker is a placement map plus per-backend proxy/stub).

Backends and their runners:
- py   -> src/revl/_process_runner.py            (cordis-py; reactive)
- node -> backends/typescript/placement_runner.ts (cordis-ts; reactive)
- rust -> backends/rust/placement_runner          (cordis-rs; consumer-only
          first cut, user_cache composition; hand-written Database proxy)
- java -> backends/java/placement/PlacementRunner  (cordis4j stub; generic
          reflection proxy; crossing verified, reactive withdrawal is a follow-up)
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from .compiler import compile_files
from .errors import RevlError

KNOWN_BACKENDS = ("py", "node", "rust", "java")

_BACKENDS_DIR = Path(__file__).resolve().parents[2] / "backends"
_TS_DIR = _BACKENDS_DIR / "typescript"
_RUST_RUNNER = _BACKENDS_DIR / "rust" / "placement_runner"
_JAVA_DIR = _BACKENDS_DIR / "java"
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


def _cargo_build_rust() -> str:
    result = subprocess.run(
        ["cargo", "build", "--manifest-path", str(_RUST_RUNNER / "Cargo.toml")],
        capture_output=True, text=True,
    )
    if result.returncode:
        raise RuntimeError(f"cargo build (rust runner) failed:\n{result.stderr.strip()}")
    return str(_RUST_RUNNER / "target" / "debug" / "revl_placement_runner")


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

    tmp = Path(tempfile.mkdtemp(prefix="revl_placement_"))
    sockets = {p: str(tmp / f"{p}.sock") for p in processes}

    # base specs (backend-neutral)
    specs: dict[str, dict] = {}
    backends: dict[str, str] = {}
    for pname, pconf in processes.items():
        backend = pconf.get("backend", "py")
        if backend not in KNOWN_BACKENDS:
            print(f"error: process {pname!r} has unsupported backend {backend!r} "
                  f"({', '.join(KNOWN_BACKENDS)})", file=sys.stderr)
            return 1
        backends[pname] = backend
        proxies: dict[str, dict] = {}
        for key, service in requires[pname].items():
            if key in provides[pname]:
                continue
            host = owner.get(key)
            if host is None:
                print(f"error: key {key!r} required by {pname!r} is provided by no process", file=sys.stderr)
                return 1
            proxies[key] = {"socket": sockets[host], "methods": methods.get(service, []), "service": service}
        serve_keys = [k for k in provides[pname] if any(k in requires[q] and q != pname for q in processes)]
        if backend == "rust" and serve_keys:
            print(f"error: process {pname!r} (rust) must provide {serve_keys} across a seam, but the rust "
                  f"runner has no stub yet; place that provider on py/node/java", file=sys.stderr)
            return 1
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
            spec["serve"] = {"socket": sockets[pname], "keys": serve_keys}
        specs[pname] = spec

    # per-backend build steps (once)
    cleanup: list[str] = []
    try:
        if any(b == "node" for b in backends.values()):
            ts_module = _emit_ts_module(ir, tmp)
            cleanup.append(ts_module)
            for pname, spec in specs.items():
                if backends[pname] == "node":
                    spec["module"] = ts_module
        java_out = _build_java(ir, tmp) if any(b == "java" for b in backends.values()) else None
        rust_bin = _cargo_build_rust() if any(b == "rust" for b in backends.values()) else None
    except (RevlError, RuntimeError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    # per-backend spec adaptation + spawn command + stop mode
    def command_for(pname: str, spec_file: Path) -> tuple[list, dict | None, str]:
        backend = backends[pname]
        if backend == "node":
            return ["node", str(_TS_DIR / "placement_runner.ts"), str(spec_file)], None, "term"
        if backend == "rust":
            return [rust_bin, str(spec_file)], None, "stdin"
        if backend == "java":
            return ["java", "-cp", java_out, "PlacementRunner", str(spec_file)], None, "term"
        return [sys.executable, "-m", "revl._process_runner", str(spec_file)], env, "term"

    import revl  # noqa: PLC0415
    src_dir = str(Path(revl.__file__).resolve().parents[1])
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([src_dir, os.environ.get("PYTHONPATH", "")])}

    for pname, spec in specs.items():
        backend = backends[pname]
        if backend == "rust":
            spec["components"] = [_snake(c) for c in spec["components"]]
            spec["probe"] = [_parse_probe(p) for p in spec["probe"]]
        elif backend == "java":
            spec["module"] = "revl.Components"
            iface_keys = set(spec["proxies"]) | set(spec.get("serve", {}).get("keys", [])) | set(spec["provides"])
            spec["ifaces"] = {k: f"revl.Components${key_service[k]}" for k in iface_keys if k in key_service}

    summary = "  ".join(f"{p}[{backends[p]}]=[{','.join(processes[p].get('components') or [])}]" for p in processes)
    print(f"placement: {summary}", flush=True)

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
    return rc
