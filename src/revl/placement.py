"""`revl run --placement`: launch a composition split across processes.

The placement file (TOML/JSON) assigns each component to a named process, and
each process declares its `backend`:

    [config.PgDatabase]
    url = "postgres://primary:5432/app"

    [processes.provider]
    components = ["PgDatabase"]          # backend defaults to "py"

    [processes.consumer]
    backend = "rust"                     # or "py" | "node" | "ts" | "java" | "go"
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
          `ts` is accepted as an alias for `node` (the manifest names the
          runtime the process boots on; every other surface calls the tier
          `ts`), so a placement file reads the same everywhere
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
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from ._paths import backends_root
from .activation import local_prereqs
from .compiler import compile_files
from .distribute import distributability
from .errors import RevlError

KNOWN_BACKENDS = ("py", "node", "ts", "rust", "java", "go")

# Every seam call carries a deadline (docs/seam-deadlines.md): a cross-process
# round-trip against a wedged provider must not block its consumer forever, so
# each proxied seam gets a per-operation deadline default. This is the
# composition-wide fallback (seconds); the placement file overrides it per
# process (`seam_deadline`) or per operation (`[processes.<p>.seam_deadlines]`).
DEFAULT_SEAM_DEADLINE = 30.0

# The TypeScript tier is `node` in the placement manifest (the process runs
# on the Node runtime) but `ts` on every other surface (run.py, `revl test`,
# the README, the conformance matrix). Both spellings are accepted; `ts` is
# canonicalized to `node` at the manifest edge so the rest of the conductor
# keys on one name.
_BACKEND_ALIASES = {"ts": "node"}


def _canonical_backend(name: str) -> str:
    return _BACKEND_ALIASES.get(name, name)


# --------------------------------------------------------------------------
# network placement (roadmap item 56): a seam may name a machine (host:port)
# instead of a local process. Transport is TCP + mutual TLS; identity per
# process derives from the operator model (item 55). docs/network-placement.md.
# --------------------------------------------------------------------------


def seam_latency_ms(host: str, port: int, samples: int = 5,
                    timeout: float = 1.0) -> float | None:
    """A **real** per-seam latency number for a network seam: the median TCP
    connect round-trip to ``host:port``, in milliseconds. This is the concrete
    figure that replaces the audit's abstract "chatty and latency-bound" note
    once a seam actually points at a machine. Returns None when the endpoint is
    not reachable (the provider is not up yet), so the caller can fall back to a
    configured RTT class."""
    rtts: list[float] = []
    for _ in range(max(1, samples)):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        start = time.perf_counter()
        try:
            sock.connect((host, port))
        except OSError:
            sock.close()
            continue
        rtts.append((time.perf_counter() - start) * 1000.0)
        try:
            sock.close()
        except OSError:
            pass
    if not rtts:
        return None
    rtts.sort()
    return round(rtts[len(rtts) // 2], 3)


def _san_entry(host: str) -> str:
    """A SAN clause for `host`: `IP:` for a dotted/colon literal, else `DNS:`."""
    return f"IP:{host}" if host[:1].isdigit() or ":" in host else f"DNS:{host}"


def generate_seam_certs(out_dir: Path, identities, hosts=("127.0.0.1", "localhost")) -> dict:
    """Mint a throwaway CA and one leaf cert per identity for a **loopback**
    network placement — self-signed *test* material only, never real keys
    (docs/network-placement.md). Every leaf carries the same SAN set (so any
    process verifies any other by host) and both ``serverAuth`` and
    ``clientAuth`` EKUs, because over mTLS each process is both a provider and a
    consumer. Uses the ``openssl`` CLI (no extra Python dependency).

    Returns ``{identity: {"cert","key","ca","identity"}}``.
    """
    if shutil.which("openssl") is None:
        raise RuntimeError(
            "generate_test_certs needs the openssl CLI on PATH to mint loopback "
            "test certificates; install it, or supply explicit cert/key/ca paths "
            "under each process's [tls]")
    out_dir.mkdir(parents=True, exist_ok=True)
    ca_key, ca_crt = out_dir / "seam_ca.key", out_dir / "seam_ca.crt"

    def _openssl(args: list[str]) -> None:
        result = subprocess.run(["openssl", *args], capture_output=True, text=True)
        if result.returncode:
            raise RuntimeError(f"openssl {args[0]} failed:\n{result.stderr.strip()}")

    _openssl(["req", "-x509", "-newkey", "ec",
              "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes",
              "-keyout", str(ca_key), "-out", str(ca_crt),
              "-days", "1", "-subj", "/CN=revl-seam-ca"])
    ext = out_dir / "seam_leaf.ext"
    san = ",".join(_san_entry(h) for h in hosts)
    ext.write_text(f"subjectAltName={san}\nextendedKeyUsage=serverAuth,clientAuth\n",
                   encoding="utf-8")
    material: dict[str, dict] = {}
    for identity in sorted(set(identities)):
        key = out_dir / f"seam_{identity}.key"
        csr = out_dir / f"seam_{identity}.csr"
        crt = out_dir / f"seam_{identity}.crt"
        _openssl(["req", "-newkey", "ec",
                  "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes",
                  "-keyout", str(key), "-out", str(csr), "-subj", f"/CN={identity}"])
        _openssl(["x509", "-req", "-in", str(csr), "-CA", str(ca_crt),
                  "-CAkey", str(ca_key), "-CAcreateserial", "-out", str(crt),
                  "-days", "1", "-extfile", str(ext)])
        material[identity] = {"cert": str(crt), "key": str(key),
                              "ca": str(ca_crt), "identity": identity}
    return material


def _operator_identities(profile_path: str) -> set[str]:
    """The operator tokens a profile declares — a *read-only* reuse of the item
    55 identity model (`src/revl/mcp/operator.py`). When a placement names an
    ``operator_profile``, every network process's identity must be one of these
    tokens, so a seam is attributable to a declared operator, not an ad-hoc
    string."""
    from .mcp.operator import load_profile  # noqa: PLC0415 — read-only reuse of item 55

    return set(load_profile(profile_path).operators)

_BACKENDS_DIR = backends_root()
_TS_DIR = _BACKENDS_DIR / "typescript"
_RUST_RUNNER = _BACKENDS_DIR / "rust" / "placement_runner"
_JAVA_DIR = _BACKENDS_DIR / "java"
_GO_DIR = _BACKENDS_DIR / "go"
_GO_RUNNER = _GO_DIR / "placement_runner"
_PROBE_RE = re.compile(r"^\s*(\w+)\.(\w+)\((.*)\)\s*$")


def _interactive() -> bool:
    """Whether a live placement should hold an interactive swap REPL rather
    than just waiting for Ctrl-C. A seam a test can pin without a real TTY."""
    return sys.stdin.isatty()


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
# live swap: admission gate (roadmap §23, `revl swap <component> --to <backend>`)
# --------------------------------------------------------------------------


def swap_admission(files, running_ir: dict, component: str, backend: str):
    """Admit a candidate provider for `component`, re-hosted on `backend`,
    against the *running* manifest. Returns ``(candidate_ir, None)`` when the
    swap is admissible, or ``(None, diagnostic)`` when it is refused — in which
    case the caller must leave the running composition untouched (no blip).

    Two guarantees, both read off the IR with no runtime:

    * **structural compatibility** — recompiling the candidate with
      ``manifest=running_ir, replacing=(component,)`` links it *against the
      running composition* (the same admission gate a py-tier hot-swap uses).
      Every running consumer's call site was checked against the old interface
      and is never recompiled, so a candidate that drops or narrows an
      operation a consumer still calls is refused here with a guarantee-naming
      diagnostic (G2/G3, `differs from the running manifest in a way that
      breaks <consumer>`).
    * **the seam is crossable** — a tier swap moves the component into its own
      process, so every service it *provides and another component consumes*
      must be transport-safe (async, value-typed; docs/interop-bridge.md §4).
      An address-space-bound service (a sync `fn`, an `emission`, or a resource
      return) cannot be re-pointed across a process boundary, so the swap is
      refused before anything is booted.

    A pure tier swap (same source, new backend) satisfies the first trivially;
    the check still runs, because passing it *is* the cross-tier guarantee.
    """
    try:
        candidate = compile_files(list(files), manifest=running_ir,
                                  replacing=(component,))
    except RevlError as exc:
        return None, str(exc)

    running_comp = next((c for c in running_ir.get("components") or []
                         if c.get("name") == component), None)
    if running_comp is None:
        return None, (f"{component!r} is not a component of the running "
                      f"composition (nothing to swap)")

    provided = running_comp.get("provides") or {}
    consumed_services = set()
    for other in running_ir.get("components") or []:
        if other.get("name") == component:
            continue
        for service in (other.get("requires") or {}).values():
            consumed_services.add(service)

    verdicts = distributability(candidate)
    for key, service in provided.items():
        if service not in consumed_services:
            continue
        verdict = (verdicts.get(service) or {}).get("verdict")
        if verdict and verdict != "transport-safe":
            reasons = ", ".join((verdicts.get(service) or {}).get("reasons") or [])
            return None, (
                f"cannot swap {component!r} to the {backend} tier: service "
                f"`{service}` (key {key!r}) is address-space-bound"
                + (f" ({reasons})" if reasons else "")
                + " — a tier swap re-points it across a process seam, and only "
                  "a transport-safe (async, value-typed) service crosses "
                  "cleanly (docs/interop-bridge.md §4)")

    return candidate, None


# --------------------------------------------------------------------------
# per-slice provider selection (roadmap §59, verified canary)
# --------------------------------------------------------------------------
#
# A canary is the gradual form of a swap: run predecessor and successor at once
# and split traffic by a *designated slice*. The mechanism is realms/instances
# (item 10), because G2 forbids two providers of one key in *one* realm — so a
# second (canary) provider of a key can only live in a *different* realm. That
# is exactly what makes a slice a clean unit: the canary provider serves one
# realm's keys and, by G2, cannot reach into any sibling realm. These functions
# pick the provider that serves a designated slice, and the remainder a promote
# would then swap — pure reads off the linked IR, no runtime.


def _realm_of(entry: dict, key: str) -> str:
    from .lower import SHARED_REALM  # noqa: PLC0415 — avoid a top-level cycle
    return (entry.get("isolate") or {}).get(key, SHARED_REALM)


def slice_realms(ir: dict) -> list[str]:
    """Every named (non-shared) realm the composition declares, sorted. Each is
    a candidate canary slice — a tenant, a sandbox, a test realm (Def. 28)."""
    from .lower import SHARED_REALM  # noqa: PLC0415
    entries = (ir.get("manifest") or {}).get("components") or []
    found: set[str] = set()
    for entry in entries:
        found |= {r for r in (entry.get("isolate") or {}).values()
                  if r != SHARED_REALM}
    return sorted(found)


def slice_providers(ir: dict, realm: str) -> dict[str, str]:
    """`{key: provider}` for every provision served *into* `realm` — the
    provider set that a canary designates for this slice. G2 makes each
    `(key, realm)` unique, so this map is single-valued by construction."""
    out: dict[str, str] = {}
    for entry in (ir.get("manifest") or {}).get("components") or []:
        for key in entry.get("provides") or []:
            if _realm_of(entry, key) == realm:
                out[key] = entry["name"]
    return dict(sorted(out.items()))


def slice_partition(ir: dict, realm: str) -> dict:
    """Split the composition into the designated canary slice and the remainder
    a *promote* (item 23's swap) would then have to move.

    Returns ``{"realm", "providers", "members", "remainderRealms",
    "remainderProviders"}``. ``members`` are the components isolating any key
    into the slice (in load order); ``providers`` are the ones that *provide*
    into it — the processes a canary boots. ``remainderProviders`` are the same
    keys' providers in every sibling realm: promote = swap each of those to the
    canary's generation once the evidence says go.
    """
    from .lower import SHARED_REALM  # noqa: PLC0415
    entries = {e["name"]: e for e in (ir.get("manifest") or {}).get("components") or []}
    load_order = (ir.get("manifest") or {}).get("loadOrder") or list(entries)

    def in_realm(name: str, r: str) -> bool:
        return r in {v for v in (entries[name].get("isolate") or {}).values()
                     if v != SHARED_REALM}

    members = [n for n in load_order if n in entries and in_realm(n, realm)]
    providers = slice_providers(ir, realm)

    # the same keys, served in every OTHER realm — what a promote must swap
    slice_keys = set(providers)
    remainder_realms = [r for r in slice_realms(ir) if r != realm]
    remainder_providers: dict[str, dict[str, str]] = {}
    for other in remainder_realms:
        served = {k: p for k, p in slice_providers(ir, other).items()
                  if k in slice_keys}
        if served:
            remainder_providers[other] = served

    return {
        "realm": realm,
        "providers": providers,
        "members": members,
        "remainderRealms": remainder_realms,
        "remainderProviders": remainder_providers,
    }


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


def _names_in(node, acc: set) -> set:
    """Every `name`/`id` string reachable in an IR subtree — enough to tell
    whether a component's body reaches a given extern (an extern call is a
    `{"kind": "fn", "name": "<extern>"}` node; a bare reference an
    `{"kind": "name", "id": ...}`). Over-inclusive by design: it never drops an
    extern a kept component still reaches."""
    if isinstance(node, dict):
        for field in ("name", "id"):
            value = node.get(field)
            if isinstance(value, str):
                acc.add(value)
        for value in node.values():
            _names_in(value, acc)
    elif isinstance(node, list):
        for value in node:
            _names_in(value, acc)
    return acc


def ts_safe_ir(ir: dict) -> dict:
    """The slice of a composition the TypeScript tier can emit (roadmap item
    144, Path A).

    A cross-tier placement compiles ONE composition and hands the *same* IR to
    every process's emitter; but a provider that lives on the py tier — the
    `Gate` compiler service is the motivating case — reaches host code through a
    `@py`-only extern (`compile_files`, which has no TypeScript spelling), and
    the ts emitter rightly refuses an extern with no `@ts` body
    (backends/typescript/emit.py::_emit_ts_externs). The consumer never needs
    that body: it reaches the provider through a **bridge proxy** (spec
    `proxies`, installed by backends/typescript/placement_runner.ts), which
    speaks the service interface over the seam. So the node module needs the
    *interface*, not the remote `@py` implementation.

    This drops exactly the un-emittable part: every extern with no `@ts` body,
    and every component (or top-level fn) whose body reaches one. Services,
    types and ts-safe components are kept verbatim — a composition with no
    py-only extern is returned byte-identical, so existing node placements are
    unaffected. The dropped provider still runs, on its own (py) process; the
    node process consumes it as a proxy.
    """
    externs = ir.get("externs") or []
    py_only = {e.get("name") for e in externs
               if "ts" not in (e.get("bodies") or {}) and e.get("name")}
    if not py_only:
        return ir

    def reaches_py_only(carrier) -> bool:
        return bool(_names_in(carrier, set()) & py_only)

    out = dict(ir)
    out["externs"] = [e for e in externs if e.get("name") not in py_only]
    out["components"] = [c for c in ir.get("components") or []
                         if not reaches_py_only(c)]
    out["functions"] = [f for f in ir.get("functions") or []
                        if not reaches_py_only(f)]
    return out


def _emit_ts_module(ir: dict, tmp: Path) -> str:
    """Emit the cordis-ts module for node processes into backends/typescript/
    _gen/ so its `../runtime.ts` / `cordis` imports resolve.

    The IR is first narrowed to the tier-emittable slice (`ts_safe_ir`): a
    py-only provider (e.g. the `Gate` compiler service) is dropped here and
    reached as a bridge proxy instead, so the ts emitter never sees a `@py`
    extern it cannot spell (roadmap item 144)."""
    gen = _TS_DIR / "_gen"
    gen.mkdir(exist_ok=True)
    ir_json = tmp / "ir.json"
    ir_json.write_text(json.dumps(ts_safe_ir(ir)), encoding="utf-8")
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
    # composition-wide seam-deadline default; a process may override it, and a
    # process may set per-operation deadlines (docs/seam-deadlines.md).
    default_deadline = placement.get("seam_deadline", DEFAULT_SEAM_DEADLINE)
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
    backends_used = {_canonical_backend(pconf.get("backend", "py"))
                     for pconf in processes.values()} & set(KNOWN_BACKENDS)
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
    # §46: the manifest entries carry the G3 inject/provides structure the
    # per-process activation DAG is reconstructed from (docs/parallel-activation.md).
    manifest_entries = (ir.get("manifest") or {}).get("components") or []

    # 0700 by construction (mkdtemp), so the sockets under it are reachable
    # by this user only; removed again in the `finally` below.
    tmp = Path(tempfile.mkdtemp(prefix="revl_placement_"))
    sockets = {p: str(tmp / f"{p}.sock") for p in processes}

    def abort(message: str, code: int = 1) -> int:
        """Report and bail out without leaving the placement dir behind."""
        print(f"error: {message}", file=sys.stderr)
        shutil.rmtree(tmp, ignore_errors=True)
        return code

    # --- network placement (item 56): a seam whose provider process declares an
    # `address` crosses TCP + mutual TLS instead of a local UDS. Identity per
    # process comes from the operator model (item 55). Every other seam stays a
    # local UDS (default, no cert; full back-compat). docs/network-placement.md.
    addresses: dict[str, tuple[str, int, float | None]] = {}
    identities: dict[str, str] = {}
    explicit_tls: dict[str, dict] = {}
    for pname, pconf in processes.items():
        addr = pconf.get("address")
        if addr:
            if addr.get("host") is None or addr.get("port") is None:
                return abort(f"process {pname!r} `address` needs both host and port")
            addresses[pname] = (str(addr["host"]), int(addr["port"]), addr.get("rtt_ms"))
        tconf = pconf.get("tls") or {}
        if tconf.get("identity"):
            identities[pname] = str(tconf["identity"])
        if tconf.get("cert"):
            missing = [k for k in ("key", "ca", "identity") if not tconf.get(k)]
            if missing:
                return abort(f"process {pname!r} [tls] gives `cert` but not {', '.join(missing)}")
            explicit_tls[pname] = {"cert": tconf["cert"], "key": tconf["key"],
                                   "ca": tconf["ca"], "identity": tconf["identity"]}

    # which processes take part in a network seam (as provider or consumer)?
    network_processes: set[str] = set(addresses)  # a provider serves remotely
    for pname in processes:
        for key in requires[pname]:
            if key in provides[pname]:
                continue
            if owner.get(key) in addresses:
                network_processes.add(pname)          # this consumer crosses TCP
                network_processes.add(owner[key])     # to that provider

    # identity per process (item 55): every network process must name one, and —
    # when an operator profile is configured — it must be a declared operator.
    profile_path = placement.get("operator_profile")
    allowed_identities: set[str] | None = None
    if profile_path:
        try:
            allowed_identities = _operator_identities(profile_path)
        except (RevlError, OSError, ValueError) as exc:
            return abort(f"cannot load operator_profile {profile_path!r}: {exc}")
    for pname in sorted(network_processes):
        if pname not in identities:
            return abort(
                f"process {pname!r} takes part in a network seam but declares no "
                f'identity — add [processes.{pname}.tls] identity = "..." (a '
                "network seam must present a per-process identity; item 56)")
        if allowed_identities is not None and identities[pname] not in allowed_identities:
            return abort(
                f"process {pname!r} identity {identities[pname]!r} is not a declared "
                f"operator in {profile_path!r} (identity per process is issued by "
                "the operator model, item 55)")
        if processes[pname].get("seam_deadline", default_deadline) is None:
            return abort(
                f"process {pname!r} takes part in a network seam but its "
                "seam_deadline is null — a network round-trip needs a deadline "
                "(item 54); set seam_deadline or leave it at the default")
        # The two sides of a network seam ship on different runners, so the
        # backend rule is per-role, not blanket (item 149):
        #   * the *provider* (the process that declares an `address` and serves
        #     its keys over the mTLS listener) runs `asyncio.start_server` +
        #     mutual TLS — that serve side is py-only in this cut, so a network
        #     provider must be py;
        #   * the *consumer* only dials that listener. The py runner and the
        #     node/ts runner both speak the TCP+mTLS *client* now
        #     (backends/typescript/bridge.ts::makeProxy grew a network endpoint),
        #     so a node/ts consumer is allowed over the network path. The
        #     rust/go/java runners still read only the local `socket` form, so a
        #     network consumer on those tiers is still refused.
        pbackend = _canonical_backend(processes[pname].get("backend", "py"))
        is_network_provider = pname in addresses
        if is_network_provider and pbackend != "py":
            return abort(
                f"process {pname!r} serves a network seam (it declares an "
                f"`address`) but is on the {pbackend} backend — the TCP+mTLS "
                "listener (item 56) is py-only in this cut; a network *provider* "
                "must be a py process")
        if not is_network_provider and pbackend not in ("py", "node"):
            return abort(
                f"process {pname!r} consumes a network seam but is on the "
                f"{pbackend} backend — the TCP+mTLS client ships on the py and "
                "node/ts runners (item 149); the rust/go/java runners read only "
                "the local `socket` form, so put the consumer on py or node/ts, "
                "or give it a local UDS seam")

    # certificate material for every network identity: minted loopback *test*
    # certs when `generate_test_certs`, else the explicit paths each [tls] gave.
    certs: dict[str, dict] = {}
    if network_processes:
        want = {identities[p] for p in network_processes}
        if placement.get("generate_test_certs"):
            hosts = tuple(sorted({h for h, _, _ in addresses.values()}
                                 | {"127.0.0.1", "localhost"}))
            try:
                minted = generate_seam_certs(tmp / "certs", want, hosts)
            except RuntimeError as exc:
                return abort(str(exc))
            certs = {p: minted[identities[p]] for p in network_processes}
        else:
            for p in sorted(network_processes):
                if p not in explicit_tls:
                    return abort(
                        f"process {p!r} is on a network seam but supplies no TLS "
                        f"material — give [processes.{p}.tls] cert/key/ca/identity, or "
                        "set generate_test_certs = true for loopback test certs")
                certs[p] = explicit_tls[p]

    def _serve_endpoint(pname: str) -> dict:
        host, port, _ = addresses[pname]
        return {"host": host, "port": port,
                "tls": {**certs[pname], "server_hostname": host}}

    def _proxy_endpoint(consumer: str, host_proc: str) -> tuple[dict, float | None]:
        host, port, rtt = addresses[host_proc]
        return ({"host": host, "port": port,
                 "tls": {**certs[consumer], "server_hostname": host}}, rtt)

    # (consumer, key, host, port, configured_rtt) for the latency report below
    net_seams: list[tuple[str, str, str, int, float | None]] = []

    # base specs (backend-neutral)
    specs: dict[str, dict] = {}
    backends: dict[str, str] = {}
    for pname, pconf in processes.items():
        backend = _canonical_backend(pconf.get("backend", "py"))
        if backend not in KNOWN_BACKENDS:
            return abort(f"process {pname!r} has unsupported backend {pconf.get('backend')!r} "
                         f"({', '.join(KNOWN_BACKENDS)})")
        backends[pname] = backend
        # this process's seam-deadline default + optional per-operation map. Each
        # proxy carries them so a wedged provider breaches the deadline as a
        # distinguishable SeamDeadline rather than blocking the consumer forever.
        p_deadline = pconf.get("seam_deadline", default_deadline)
        p_deadlines = {m: float(s) for m, s in (pconf.get("seam_deadlines") or {}).items()}
        proxies: dict[str, dict] = {}
        for key, service in requires[pname].items():
            if key in provides[pname]:
                continue
            host = owner.get(key)
            if host is None:
                return abort(f"key {key!r} required by {pname!r} is provided by no process")
            entry = {"methods": methods.get(service, []), "service": service,
                     "deadline": p_deadline}
            if host in addresses:
                # a network seam: point the proxy at the machine over TCP+mTLS,
                # and record its latency class (the configured RTT; the conductor
                # also measures a real number once the provider is up, below).
                endpoint, rtt = _proxy_endpoint(pname, host)
                entry["endpoint"] = endpoint
                entry["latency_ms"] = rtt
                ehost, eport, _ = addresses[host]
                net_seams.append((pname, key, ehost, eport, rtt))
            else:
                entry["socket"] = sockets[host]
            if p_deadlines:
                entry["deadlines"] = dict(p_deadlines)
            proxies[key] = entry
        serve_keys = [k for k in provides[pname] if any(k in requires[q] and q != pname for q in processes)]
        if pname in addresses:
            # a network provider serves its full provided surface — remote
            # consumers live in other placements and are not enumerable here.
            serve_keys = list(provides[pname])
        own = [c for c in load_order if placed.get(c) == pname]
        spec = {
            "name": pname,
            "backend": backend,
            "files": [str(f) for f in files],
            "components": own,
            # §46: the intra-process dependency edges, reconstructed from the
            # compiler's G3 inject/provides structure (not discovered at
            # runtime). The runner activates independent branches concurrently
            # and serializes only along these edges. Cross-process edges are
            # already resolved as proxies before local activation, so they are
            # (correctly) absent here. docs/parallel-activation.md.
            "depends": local_prereqs(manifest_entries, subset=own),
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
            serve_spec = {
                "keys": serve_keys,
                "methods": {k: methods.get(provides[pname][k], []) for k in serve_keys},
            }
            if pname in addresses:
                serve_spec["endpoint"] = _serve_endpoint(pname)
            else:
                serve_spec["socket"] = sockets[pname]
            spec["serve"] = serve_spec
        specs[pname] = spec

    import revl  # noqa: PLC0415
    src_dir = str(Path(revl.__file__).resolve().parents[1])
    # keep the inherited PYTHONPATH, but drop empty entries: an empty entry is
    # the *current directory* on the child's sys.path, which would let a stray
    # module in CWD shadow a real one.
    inherited = [p for p in os.environ.get("PYTHONPATH", "").split(os.pathsep) if p]
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([src_dir, *inherited])}

    # per-backend build steps, done lazily and cached: the initial placement
    # builds every backend it uses, and a later `revl swap ... --to <backend>`
    # reuses those or builds a new target on demand (booting the candidate
    # provider in its own process on the target tier — roadmap §23 step 1).
    cleanup: list[str] = []
    built: dict[str, object] = {}

    def ensure_backend(backend: str) -> None:
        if backend in built:
            return
        if backend == "node":
            module = _emit_ts_module(ir, tmp)
            cleanup.append(module)
            built["node"] = module
        elif backend == "rust":
            built["rust"] = _build_rust(ir, tmp)
        elif backend == "go":
            built["go"] = _build_go(ir, tmp)
        elif backend == "java":
            java21_bin = _find_jdk21()
            cordis_classes = _find_cordis4j_classes()
            if java21_bin and cordis_classes:
                built["java"] = ("real", _build_java_real(ir, tmp, java21_bin, cordis_classes),
                                 java21_bin, cordis_classes)
            else:
                built["java"] = ("stub", _build_java(ir, tmp), None, None)
        else:  # py needs no build step
            built["py"] = True

    def adapt_spec(spec: dict, backend: str) -> None:
        """Backend-specific spec shaping, applied just before the spec is
        written. Kept identical to what the initial spawn loop always did."""
        if backend == "node":
            spec["module"] = built["node"]
        elif backend == "rust":
            spec["components"] = [_snake(c) for c in spec["components"]]
            spec["probe"] = [_parse_probe(p) for p in spec["probe"]]
        elif backend == "go":
            # go keeps PascalCase component names (RevlLoad switches on them);
            # only probes are structured rather than eval'd strings.
            spec["probe"] = [_parse_probe(p) for p in spec["probe"]]
        elif backend == "java":
            spec["module"] = "revl.Components"
            iface_keys = (set(spec["proxies"]) | set(spec.get("serve", {}).get("keys", []))
                          | set(spec["provides"]))
            spec["ifaces"] = {k: f"revl.Components${key_service[k]}"
                              for k in iface_keys if k in key_service}

    def command_for(backend: str, spec_file: Path) -> tuple[list, dict | None, str]:
        if backend == "node":
            return ["node", str(_TS_DIR / "placement_runner.ts"), str(spec_file)], None, "term"
        if backend == "rust":
            return [built["rust"], str(spec_file)], None, "stdin"
        if backend == "go":
            return [built["go"], str(spec_file)], None, "term"
        if backend == "java":
            mode, out, java21_bin, cordis_classes = built["java"]
            if mode == "real":
                cp = f"{cordis_classes}{os.pathsep}{out}"
                return [str(Path(java21_bin) / "java"), "-cp", cp, "RealPlacementRunner",
                        str(spec_file)], None, "term"
            return ["java", "-cp", out, "PlacementRunner", str(spec_file)], None, "term"
        return [sys.executable, "-m", "revl._process_runner", str(spec_file)], env, "term"

    try:
        for backend in backends.values():
            ensure_backend(backend)
    except (RevlError, RuntimeError, OSError) as exc:
        return abort(str(exc))

    for pname, spec in specs.items():
        adapt_spec(spec, backends[pname])

    summary = "  ".join(f"{p}[{backends[p]}]=[{','.join(processes[p].get('components') or [])}]" for p in processes)
    print(f"placement: {summary}", flush=True)
    if "java" in built:
        note = ("real cordis4j (reactive)" if built["java"][0] == "real"
                else "stub (non-reactive; set REVL_CORDIS4J_CLASSES + a JDK 21 for reactive withdrawal)")
        print(f"  java runtime: {note}", flush=True)
    if net_seams:
        print(f"  network seams (item 56): {len(net_seams)} over TCP+mTLS", flush=True)

    def report_network_latency() -> None:
        """Print the real per-seam latency for each network seam (item 56): the
        measured TCP RTT once the provider is up, else the configured RTT class,
        else unreachable. This is the abstract 'latency-bound' audit note made a
        number now that the seam points at a machine."""
        for consumer, key, host, port, configured in net_seams:
            measured = seam_latency_ms(host, port)
            if measured is not None:
                detail = f"RTT ~{measured:g} ms (measured)"
            elif configured is not None:
                detail = f"RTT ~{float(configured):g} ms (configured)"
            else:
                detail = "RTT unknown (provider unreachable)"
            print(f"  seam {consumer}.{key} -> tcp://{host}:{port}  {detail}", flush=True)

    # conductor-side state a live swap must keep coherent, keyed by process
    children: dict[str, tuple] = {}
    up: set[str] = set()
    repointed: set[tuple[str, str]] = set()
    down: set[str] = set()
    threads: list[threading.Thread] = []
    _re_repoint = re.compile(r"^\[(?P<p>[^\]]+)\] REPOINTED (?P<k>\S+) ->")

    def pump(pname: str, proc: subprocess.Popen) -> None:
        for line in proc.stdout:
            sys.stdout.write(line)
            sys.stdout.flush()
            text = line.strip()
            if text == f"[{pname}] UP":
                up.add(pname)
            elif text == f"[{pname}] DOWN":
                down.add(pname)
            else:
                m = _re_repoint.match(text)
                if m and m.group("p") == pname:
                    repointed.add((pname, m.group("k")))

    def spawn(pname: str, backend: str, spec: dict) -> None:
        spec_file = tmp / f"{pname}.spec.json"
        spec_file.write_text(json.dumps(spec), encoding="utf-8")
        cmd, proc_env, stop_mode = command_for(backend, spec_file)
        proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            env=proc_env, text=True,
        )
        children[pname] = (proc, stop_mode)
        thread = threading.Thread(target=pump, args=(pname, proc), daemon=True)
        thread.start()
        threads.append(thread)

    def _wait_for(pred, timeout: float = 60.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if pred():
                return True
            time.sleep(0.05)
        return pred()

    swap_seq = [0]

    def do_swap(component: str, to_backend: str) -> None:
        """`revl swap <component> --to <backend>`: boot -> admit -> re-point ->
        drain + tear the old provider down, proving no residue (docs/swap.md).

        v1 scope: the swapped component must be the only one in its process
        (its process is the provider being replaced), and its provided services
        must already cross a seam. A mid-stream call is *drained* against the
        old provider (the client holds its lock across the in-flight call), not
        handed over mid-flight — that refinement is deferred (docs/swap.md)."""
        if component not in placed:
            print(f"swap refused: no component {component!r} in this placement "
                  f"(have: {', '.join(sorted(placed))})", flush=True)
            return
        to_backend = _canonical_backend(to_backend)
        if to_backend not in KNOWN_BACKENDS:
            print(f"swap refused: unknown backend {to_backend!r} "
                  f"({', '.join(KNOWN_BACKENDS)})", flush=True)
            return
        old = placed[component]
        housemates = [c for c, p in placed.items() if p == old]
        if housemates != [component]:
            others = ", ".join(c for c in housemates if c != component)
            print(f"swap refused (v1): {component!r} shares process {old!r} with "
                  f"{others}; v1 swaps a component that is alone in its process "
                  f"(docs/swap.md).", flush=True)
            return

        # --- admission gate: refuse without touching the running composition
        candidate, error = swap_admission(files, ir, component, to_backend)
        if error is not None:
            for i, text in enumerate(error.splitlines()):
                print(f"  {'swap refused:' if i == 0 else '             '} {text}", flush=True)
            print("  running composition untouched — the candidate never booted.", flush=True)
            return
        try:
            ensure_backend(to_backend)
        except (RevlError, RuntimeError, OSError) as exc:
            print(f"swap refused: could not build the {to_backend} tier:\n{exc}", flush=True)
            print("  running composition untouched.", flush=True)
            return

        # --- boot the successor provider on the target tier, on a new socket
        swap_seq[0] += 1
        succ = f"{component}__t{swap_seq[0]}"
        new_sock = str(tmp / f"{succ}.sock")
        old_serve = specs[old].get("serve") or {}
        serve_keys = old_serve.get("keys") or [k for k in provides[old]]
        succ_spec = {
            "name": succ,
            "backend": to_backend,
            "files": [str(f) for f in files],
            "components": [component],  # the swapped component, alone (v1 scope)
            "config": config,
            "provides": list(provides[old]),
            "proxies": {k: dict(v) for k, v in (specs[old].get("proxies") or {}).items()},
            "probe": [],
            "serve": {
                "socket": new_sock,
                "keys": serve_keys,
                "methods": {k: methods.get(provides[old][k], []) for k in serve_keys},
            },
        }
        adapt_spec(succ_spec, to_backend)
        print(f"swap: booting {component} on the {to_backend} tier ({succ}) ...", flush=True)
        spawn(succ, to_backend, succ_spec)
        if not _wait_for(lambda: succ in up, 60):
            print(f"swap refused: successor {succ} did not come up — tearing it "
                  f"back down; running composition untouched.", flush=True)
            sproc, smode = children.pop(succ, (None, None))
            if sproc is not None:
                _stop_all({succ: (sproc, smode)})
            return

        # --- re-point every consumer of these keys onto the successor socket
        for key in serve_keys:
            for qname, qspec in specs.items():
                if qname in (old, succ):
                    continue
                if key in (qspec.get("proxies") or {}):
                    qproc = children[qname][0]
                    try:
                        qproc.stdin.write(json.dumps({"op": "repoint", "key": key,
                                                      "socket": new_sock}) + "\n")
                        qproc.stdin.flush()
                    except OSError as exc:
                        print(f"swap: could not signal {qname}: {exc}", flush=True)
                        continue
                    if _wait_for(lambda: (qname, key) in repointed, 30):
                        qspec["proxies"][key]["socket"] = new_sock
                    else:
                        print(f"swap: {qname} did not acknowledge repoint of {key!r}", flush=True)

        # --- drain + tear the old provider down (LIFO inside its process), and
        # let it prove no residue, then adopt the successor into placement state
        oldproc, oldmode = children.pop(old)
        _stop_all({old: (oldproc, oldmode)})
        _wait_for(lambda: old in down, 10)
        for key in serve_keys:
            owner[key] = succ
        placed[component] = succ
        provides[succ] = provides.pop(old)
        requires[succ] = requires.pop(old)
        backends[succ] = to_backend
        specs[succ] = succ_spec
        specs.pop(old, None)
        print(f"swap: {component} now on {to_backend} ({succ}); the old provider "
              f"unwound with a no-residue proof above.", flush=True)

    def swap_repl() -> None:
        print("(placement up — `swap <component> --to <backend>`, `:keys`, "
              "`:q`/Ctrl-D to tear down)", flush=True)
        while True:
            try:
                line = input("swap> ").strip()
            except (EOFError, KeyboardInterrupt):
                print(flush=True)
                break
            if not line:
                continue
            if line in (":q", ":quit", ":exit"):
                break
            if line in (":keys", ":help"):
                for c, p in sorted(placed.items()):
                    print(f"  {c}  on {backends.get(p, '?')} ({p})", flush=True)
                continue
            parts = line.split()
            if parts[0] != "swap" or "--to" not in parts or len(parts) < 4:
                print("usage: swap <component> --to <backend>", flush=True)
                continue
            component = parts[1]
            to_backend = parts[parts.index("--to") + 1]
            do_swap(component, to_backend)

    for pname, spec in specs.items():
        spawn(pname, backends[pname], spec)

    rc = 0
    try:
        if once:
            _wait_for(lambda: len(up) == len(children), 60)
            if len(up) != len(children):
                missing = ", ".join(p for p in children if p not in up)
                print(f"error: processes did not come up: {missing}", file=sys.stderr)
                rc = 1
            elif net_seams:
                report_network_latency()
            _stop_all(children)
        elif _interactive():
            if net_seams and _wait_for(lambda: len(up) == len(children), 60):
                report_network_latency()
            swap_repl()
        else:
            if net_seams and _wait_for(lambda: len(up) == len(children), 60):
                report_network_latency()
            print("(placement up; Ctrl-C to tear down)", flush=True)
            for proc, _ in list(children.values()):
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
