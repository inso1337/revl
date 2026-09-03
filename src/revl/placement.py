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

import _thread
import importlib.util
import ipaddress
import json
import os
import posixpath
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from ._paths import backends_root, stdlib_root
from .activation import local_prereqs
from .attest import canonical_hash
from .deploy import (ADMISSION_PEER_BOUND, ADMISSION_SEALED,
                     ADMISSION_UNVERIFIED, REPLAY_WINDOW_PEERS,
                     REPLAY_WINDOW_PER_PEER, SeamAdmission,
                     render_seam_admissions)
from .compiler import compile_files
from .distribute import distributability
from .errors import RevlError
from .estop import (HALTED_LINE, LATCH_ENV, TIERS_WITH_ESTOP, latch_path,
                    read_latch)
from .sandbox_runtime import resolve_driver as resolve_sandbox_driver

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

# Tiers whose consumer bridge can SEAL an item-118 correlation envelope. The
# python bridge and `typescript/bridge.ts` implement it (the ts side seals the
# same HMAC over the same canonical envelope bytes, proven end to end against
# the real py guard in tests/test_ts_correlation_seal.py); the go bridge and
# `PlacementRunner.java` still take no `correlation` parameter at all, so a
# consumer on those tiers cannot produce one. A provider is only guarded when
# every one of its local consumers is listed here (roadmap 421 F8). Add a tier
# here in the same change that teaches its bridge to seal, never before.
#
# Spelled `node`, not `ts`: `_canonical_backend` folds the `ts` alias to `node`
# at the manifest edge, and this set is compared against those canonical names.
_CORRELATION_SEALING_TIERS = frozenset({"py", "node"})

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
    import tomllib  # noqa: PLC0415  stdlib, py3.11+

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
# per-component tier placement (roadmap item 363): the `[tiers]` surface
# --------------------------------------------------------------------------
#
# A composition declares which backend tier each component emits to, in the
# placement manifest, with no `.rvl` source change (docs/interop-bridge.md
# "the broker is manifest data"; item 119 "read off the placement toml"):
#
#     default_tier = "py"
#     [tiers]
#     HotWorker = "rust"
#     [config.HotWorker]
#     threads = 4
#
# The conductor expands `[tiers]` into the equivalent synthesized `[processes]`
# topology — one process per distinct declared tier — and proceeds through the
# EXISTING conductor unchanged (placed map, seams, proxies, stub allowlists,
# preflight, capability/realm checks, per-tier builds). A cross-tier component
# communicates over the existing cross-process seam (generated proxy/stub over
# UDS/mTLS, canonical value-copy encoding, G8 allowlist, seam deadline,
# peer-death-as-withdrawal); two components on different tiers are in different
# processes by construction, so there is no in-process cross-tier FFI to build.


def expand_tiers(placement: dict, component_names) -> tuple[dict, str | None]:
    """Expand a `[tiers]` / `default_tier` manifest into the equivalent
    synthesized `[processes]` topology (one `tier_<backend>` process per
    distinct declared tier, grouping every component by its tier; a component
    absent from `[tiers]` takes `default_tier`, itself defaulting to ``py``).

    Returns ``(expanded_placement, None)`` on success or
    ``(placement, diagnostic)`` on a refusal. A manifest with neither
    ``[tiers]`` nor ``default_tier`` is returned **unchanged** — the classic
    hand-written `[processes]` form is byte-identical downstream, so the whole
    feature is a no-op then (the item's additivity guarantee).
    """
    tiers = placement.get("tiers")
    default_tier = placement.get("default_tier")
    # item 411: the `[tiers]`-form `[sandbox]` sugar table assigns a component
    # to its own isolation boundary. It shares the top-level `sandbox` key with
    # the `[sandbox.needs]` table (form-independent, consumed by the gate later),
    # so split the two: every non-`needs` key is a component -> sandbox-table
    # assignment. A manifest with no tier/sandbox sugar is returned unchanged.
    sandbox_tbl = placement.get("sandbox") or {}
    if not isinstance(sandbox_tbl, dict):
        return placement, ('`[sandbox]` must be a table (component sandbox '
                           'assignments and/or a `needs` sub-table)')
    sandbox_needs = sandbox_tbl.get("needs")
    sandbox_assign = {k: v for k, v in sandbox_tbl.items() if k != "needs"}
    if tiers is None and default_tier is None and not sandbox_assign:
        return placement, None  # classic [processes] form, untouched
    if placement.get("processes"):
        both = "[tiers]" if (tiers is not None or default_tier is not None) else "[sandbox]"
        if sandbox_assign and both != "[sandbox]":
            both = "[tiers]/[sandbox]"
        return placement, (
            f"a placement declares both a per-component {both} sugar table and an "
            "explicit `[processes]` topology — they are two spellings of the "
            "same placement and mutually exclusive in one file; keep the sugar "
            "table for one-process-per-tier/sandbox, or `[processes]` for a "
            "hand-written topology (addresses, TLS identities, probes, "
            "per-process deadlines, and per-process `[processes.<p>.sandbox]`)")
    if tiers is not None and not isinstance(tiers, dict):
        return placement, '`[tiers]` must be a table of `Component = "backend"`'
    tiers = tiers or {}
    default_tier = default_tier or "py"
    known_names = set(component_names)
    unknown = sorted(c for c in tiers if c not in known_names)
    if unknown:
        return placement, (
            f"[tiers] names unknown component(s): {', '.join(unknown)} "
            f"(known: {', '.join(sorted(known_names))})")
    unknown_sb = sorted(c for c in sandbox_assign if c not in known_names)
    if unknown_sb:
        return placement, (
            f"[sandbox] names unknown component(s): {', '.join(unknown_sb)} "
            f"(known: {', '.join(sorted(known_names))})")
    # validate every declared tier before synthesizing: wasm is refused with a
    # redirect (no wasm placement runner on the substrate), an unknown name is
    # named. The default_tier is validated once (it applies to every component
    # absent from [tiers]).
    for cname, backend in [("<default_tier>", default_tier),
                           *tiers.items()]:
        raw = str(backend)
        canon = _canonical_backend(raw)
        if raw == "wasm" or canon == "wasm":
            who = ("default_tier" if cname == "<default_tier>"
                   else f"component {cname!r}")
            return placement, (
                f"{who} is placed on the `wasm` tier, which is not a placement "
                "tier: no wasm placement runner, bridge client, or stub exists "
                "on the substrate (a wasm placement runner is a separately "
                "designed follow-on). Place native components on `rust` or `go`.")
        if canon not in KNOWN_BACKENDS:
            who = ("default_tier" if cname == "<default_tier>"
                   else f"component {cname!r}")
            return placement, (
                f"{who} is placed on unknown tier {raw!r} "
                f"(known: {', '.join(KNOWN_BACKENDS)})")
    # group NON-sandboxed components by their declared (or default) tier,
    # deterministically; a `[sandbox]`-listed component is split OUT into its
    # own synthesized process below (item 411). The tier a sandboxed component
    # runs on is still its `[tiers]` entry (or the default); isolation is a
    # fourth placement dimension composed WITH the tier, not a replacement.
    by_tier: dict[str, list[str]] = {}
    for cname in component_names:
        if cname in sandbox_assign:
            continue
        backend = _canonical_backend(str(tiers.get(cname, default_tier)))
        by_tier.setdefault(backend, []).append(cname)
    processes: dict[str, dict] = {}
    for backend in sorted(by_tier):
        processes[f"tier_{backend}"] = {
            "backend": backend, "components": list(by_tier[backend])}
    # each sandboxed component gets its own `sandbox_<component>` process
    # carrying the sandbox table verbatim (validated + normalized in
    # run_placement, uniformly with the `[processes.<p>.sandbox]` form).
    for cname in component_names:
        if cname not in sandbox_assign:
            continue
        backend = _canonical_backend(str(tiers.get(cname, default_tier)))
        processes[f"sandbox_{cname}"] = {
            "backend": backend, "components": [cname],
            "sandbox": sandbox_assign[cname]}
    # a top-level `probe` list drives the default-tier (control-plane) process:
    # the [tiers] form has no named process for the author to attach probes to,
    # and a cross-seam probe originates from the orchestrating control plane.
    probe = placement.get("probe")
    if probe:
        control = f"tier_{_canonical_backend(str(default_tier))}"
        if control in processes:
            processes[control]["probe"] = list(probe)
        else:
            # F5: every component is tiered off the default, so no default-tier
            # control-plane process was synthesized to carry the probe. Dropping
            # it silently would boot, probe NOTHING, and exit 0 — a seam
            # verification that is a no-op green. Refuse instead of vanishing.
            return placement, (
                f"a top-level `probe` is declared, but no process runs on the "
                f"default tier {default_tier!r}: every component is placed on "
                f"another tier, so there is no default-tier control plane to "
                f"attach the probe to (a cross-seam probe originates from the "
                f"orchestrating control plane). Keep at least one component on "
                f"the default tier, or move to an explicit `[processes]` "
                f"topology and attach `probe` to the process you mean to run it.")
    expanded = {k: v for k, v in placement.items()
                if k not in ("tiers", "default_tier", "probe", "sandbox")}
    expanded["processes"] = processes
    # keep the `[sandbox.needs]` table for the plan-time gate (it is
    # form-independent: an extern's needs do not depend on where it runs). The
    # sandbox ASSIGNMENTS have been consumed into the synthesized processes.
    if sandbox_needs is not None:
        expanded["sandbox"] = {"needs": sandbox_needs}
    return expanded, None


# --------------------------------------------------------------------------
# capability-enforced sandbox placement (roadmap item 411, Slice 1)
# --------------------------------------------------------------------------
#
# Isolation is a FOURTH per-process placement dimension over the 363 seam: a
# process may declare an isolation boundary (`wasm-cell` | `container` |
# `microvm`) and an OS capability envelope (`fs`, `net`) it confines the
# process to. Slice 1 is the STATIC surface: parse the manifest tables, refuse
# at plan time (the advisory needs gate + the fail-closed unmappable-need
# refusal + the cell opaque-residue refusal), narrow each sandboxed process's
# spec to its own config so a sibling's secret never enters the boundary, and
# print the envelope + effective reach in the boot summary and `revl audit`.
#
# Slice 1 does NOT launch a real jail: a sandboxed process still boots on the
# ordinary runner (the isolation is declared + gated but not yet ENFORCED). The
# runtime driver (launch/canary/transport/approval seam, the container/microVM/
# wasm-cell rungs) is Slice 2. Every refusal and every printed line below is
# real; only the enforcing boundary is deferred. A placement with no sandbox
# surface is byte-identical throughout (the 342/363/396 additivity discipline).

_ISOLATION_RUNGS = ("wasm-cell", "container", "microvm")
# The envelope vocabulary is fs and net, and ONLY fs and net (the design's
# surface section): the derived flags confine what the process reaches on the
# network and the filesystem; env, exec, ipc and device authority have no
# fs/net analogue and are NOT confined by item 411. A `[sandbox.needs]` entry
# naming a resource outside this vocabulary is a plan-time refusal, never a
# silently unenforced grant.
_ENVELOPE_RESOURCES = ("fs", "net")


def _normalize_sandbox_table(sb) -> tuple[dict | None, str | None]:
    """Validate one `sandbox` table (either `[processes.<p>.sandbox]` or a
    `[tiers]`-form `[sandbox]` assignment) and return `(normalized, None)` or
    `(None, diagnostic)`. Both envelope keys default to DENY: no `fs` key means
    no mount is granted, no `net` key means `net = "none"`."""
    if not isinstance(sb, dict):
        return None, "a sandbox table must be a mapping (isolation, image, fs, net)"
    iso = sb.get("isolation")
    if iso not in _ISOLATION_RUNGS:
        return None, (f"`isolation` must be one of "
                      f"{', '.join(repr(r) for r in _ISOLATION_RUNGS)} "
                      f"(got {iso!r})")
    net = sb.get("net", "none")
    if net not in ("none", "all"):
        return None, (f'`net` must be "none" or "all" (got {net!r}); a host '
                      "allowlist is a named follow-on, not a Slice-1 spelling")
    fs = sb.get("fs", [])
    if not isinstance(fs, list):
        return None, "`fs` must be a list of `path` or `path:mode` mount strings"
    fs = [str(m) for m in fs]
    for mount in fs:
        mount_err = _bad_fs_path(_parse_mount(mount)[0], f"mount {mount!r}")
        if mount_err:
            return None, mount_err
    image = sb.get("image")
    if iso in ("container", "microvm") and not image:
        return None, (f"the {iso!r} rung needs an `image` (pin by digest; the "
                      "image is trusted input at the level of the placement file)")
    # cell: the `fs`/`net` keys bind nothing a wasm instance could use (its
    # confinement is a generated import set, not an OS grant), so a NON-DEFAULT
    # value under a cell is refused as unmappable rather than accepted as
    # decoration (the isolation-ladder honesty paragraph).
    if iso == "wasm-cell" and (net != "none" or fs):
        return None, ("the `wasm-cell` rung takes no fs/net OS envelope; its "
                      "confinement is a generated import set (no_extern plus the "
                      "seam-only imports), so `fs`/`net` bind nothing here; "
                      "remove them, or use the `container` rung for an OS envelope")
    return {"isolation": iso, "image": image, "fs": fs, "net": net}, None


def _parse_need(resource: str) -> tuple[str, str | None, str | None]:
    """Parse one `[sandbox.needs]` resource string. Returns `(kind, path, mode)`:
    `("net", None, None)`, `("fs", path, mode)` (mode defaults to "ro"), or
    `("?", resource, None)` for anything outside the fs/net vocabulary (`env`,
    `exec`, `ipc`, devices); the fail-closed unmappable case."""
    parts = resource.split(":")
    if parts[0] == "net" and len(parts) == 1:
        return "net", None, None
    if parts[0] == "fs" and len(parts) >= 2:
        path = parts[1]
        mode = parts[2] if len(parts) >= 3 and parts[2] else "ro"
        return "fs", path, mode
    return "?", resource, None


def _parse_mount(mount: str) -> tuple[str, str]:
    """`/scratch:rw` -> ("/scratch", "rw"); `/data` -> ("/data", "ro")."""
    parts = mount.split(":")
    path = parts[0]
    mode = parts[1] if len(parts) >= 2 and parts[1] else "ro"
    return path, mode


def _canonical_fs_path(path: str) -> str | None:
    """The one canonical spelling of an absolute mount/need path, or `None` when
    the string is not one at all (roadmap 422 F5).

    `posixpath.normpath` folds `.` and `..` lexically and collapses repeated
    separators, except that POSIX reserves a leading exactly-double slash, which
    `normpath` preserves and which no mount ever means, so it is folded here.
    A relative path has no canonical form at this layer (relative to WHAT? the
    conductor's cwd is not a thing the envelope can bind) and comes back `None`,
    which every caller reads as fail-closed."""
    if not path or not path.startswith("/"):
        return None
    canonical = posixpath.normpath(path)
    while canonical.startswith("//"):
        canonical = canonical[1:]
    return canonical


def _bad_fs_path(path: str, what: str) -> str | None:
    """A diagnostic when `what`'s path is not already its canonical spelling,
    or None when it is (roadmap 422 F5).

    Refused rather than silently normalized, in BOTH directions, because the two
    directions are wrong in different ways and neither is what the author meant.
    A traversing NEED (`fs:/scratch/../../etc/shadow`) reads as covered by a
    `/scratch:rw` mount while the same file spelled `/etc/shadow` is refused, so
    normalizing silently would leave the author believing the gate had checked
    the spelling they wrote. A traversing MOUNT (`/scratch/..`) is worse the
    other way: it denotes `/`, so silently normalizing it would hand the runtime
    the whole filesystem from a line that reads like a scratch grant.

    The message names the canonical spelling, which is the nearest allowed
    space: an author who meant the traversal writes the destination out, and one
    who did not sees immediately that the two differ (item 274)."""
    canonical = _canonical_fs_path(path)
    if canonical is None:
        return (f"{what} names {path!r}, which is not an absolute path; an "
                f"fs mount and an fs need are both absolute host paths "
                f"(`/scratch`, `/scratch:rw`), there is no directory for a "
                f"relative one to be relative to")
    if canonical != path:
        return (f"{what} names {path!r}, which is not its canonical spelling: "
                f"it denotes {canonical!r}. Write {canonical!r}, so the mount "
                f"list and the needs table say what they grant and reach, a "
                f"`.`/`..`/`//` spelling is compared LITERALLY here, which is "
                f"how `/scratch/../../etc/shadow` read as covered by a "
                f"`/scratch:rw` mount")
    return None


def _fs_covers(mounts: list[str], path: str, mode: str) -> bool:
    """Does the envelope's mount list grant `path` at `mode`? A mount covers a
    path it equals or is a prefix of; `rw` is needed for a `rw` need, `ro`
    suffices for a `ro` need.

    Both sides are canonicalized first (roadmap 422 F5). This used to be a raw
    string prefix test, so `/scratch/../../etc/shadow` was COVERED by a
    `/scratch:rw` mount while the direct `/etc/shadow` spelling was refused -
    bounded while Slice 1 launches no jail and the needs gate is advisory, but
    this function is what Slice 2 inherits for deriving real mounts. The
    spellings are refused where they enter (`_normalize_sandbox_table` for a
    mount, `sandbox_capability_gate` for a need), and canonicalized again here
    so the coverage test is sound for a caller that reaches it another way.

    The two sides fail closed differently, on purpose. A non-canonical NEED is
    canonicalized and then tested, which can only narrow coverage (the path it
    denotes is the path that must be granted). A non-canonical MOUNT covers
    NOTHING, because canonicalizing it could only WIDEN the grant, and a
    defense-in-depth pass must never be the thing that widens one.

    Lexical, not resolved: no symlink on the target host is followed, and this
    layer cannot follow one (the planning host need not even have the paths).
    The ENVELOPE the runtime binds remains the security boundary; this gate
    stays advisory, as the item-411 design says."""
    want = _canonical_fs_path(path)
    if want is None:
        return False
    for mount in mounts:
        mpath, mmode = _parse_mount(mount)
        canonical = _canonical_fs_path(mpath)
        if canonical is None or canonical != mpath:
            continue
        prefix = canonical.rstrip("/")
        covers_path = want == canonical or want.startswith(prefix + "/")
        covers_mode = mmode == "rw" or mode != "rw"
        if covers_path and covers_mode:
            return True
    return False


def _need_covered(kind: str, path: str | None, mode: str | None, env: dict) -> bool:
    """Is a parsed need covered by the normalized envelope `env`?"""
    if kind == "net":
        return env.get("net") == "all"
    if kind == "fs":
        return _fs_covers(env.get("fs") or [], path or "", mode or "ro")
    return False  # unmappable; handled as a refusal by the caller


def _component_reach(ir: dict) -> dict[str, set]:
    """`{component: {reached extern name, ...}}`, `*` included for the opaque
    residue (a first-class-dispatched reach no name bounds). This is the same
    authoritative G8/G4 boundary walk `revl audit` uses; reused here so the
    sandbox gate partitions the exact reach the audit prints, with no second
    walk to drift (the `_boundary` surface, `__main__._boundary`)."""
    from .__main__ import _boundary  # noqa: PLC0415; lazy, avoids an import cycle
    surface = _boundary(ir)
    return {name: {e["name"] for e in (stats.get("externs") or [])}
            for name, stats in surface.items()}


def _envelope_str(env: dict) -> str:
    """`net=none fs=none` / `net=all fs=/scratch:rw`; the one-line envelope."""
    fs = env.get("fs") or []
    return f"net={env.get('net', 'none')} fs={','.join(fs) if fs else 'none'}"


def _reach_descriptor(pname: str, sandboxes: dict, backends: dict) -> str:
    """How a provider process's reach reads on a seam-served line: its envelope
    if it is sandboxed, else `full host reach` (an unsandboxed process runs with
    the conductor's ambient authority)."""
    backend = backends.get(pname, "py")
    if pname in sandboxes:
        env = sandboxes[pname]
        return f"{pname}[{backend}, {env['isolation']}: {_envelope_str(env)}]"
    return f"{pname}[{backend}, unsandboxed: full host reach]"


def process_tag(pname: str, processes: dict, backends: dict, sandboxes: dict) -> str:
    """The boot-summary tag for one process. A non-sandboxed process is
    byte-identical to the pre-411 tag (`p[backend]=[comps]`); a sandboxed one
    carries its rung + envelope (`p[py, container: net=none fs=none]=[comps]`)."""
    backend = backends.get(pname) or _canonical_backend(
        (processes.get(pname) or {}).get("backend", "py"))
    comps = ",".join((processes.get(pname) or {}).get("components") or [])
    if pname in sandboxes:
        env = sandboxes[pname]
        return f"{pname}[{backend}, {env['isolation']}: {_envelope_str(env)}]=[{comps}]"
    return f"{pname}[{backend}]=[{comps}]"


def sandbox_capability_gate(ir: dict, processes: dict, sandboxes: dict,
                            needs: dict, requires: dict, provides: dict,
                            owner: dict) -> str | None:
    """The item-411 Slice-1 plan-time gate. Returns a diagnostic for the first
    refusal, or None. Runs before anything spawns, next to the 119/363 gates.

    Two refusals, plus a cell carve-out (the design's "Capability enforcement
    and the plan-time gate"):

    * **fail-closed unmappable need**: a `[sandbox.needs]` entry (consulted for
      a reached host-rooted capability) naming a resource outside the fs/net
      envelope vocabulary (`env`, `exec`, ...) is refused naming the entry and
      the unmappable need. This is the one hard gate in Slice 1: the envelope
      cannot enforce it, so it is never silently ignored.
    * **advisory declared-need vs grant**: a reached host-rooted capability
      whose DECLARED needs are not covered by the envelope is refused naming the
      component, capability, need and missing grant. This borrows item 119's
      refusal shape and claims 119's authority for NEITHER: the needs side is an
      unverified authorial claim (G8 keeps the bodies opaque), so admission here
      is advisory and the ENVELOPE, not this gate, is the security boundary. A
      missing/understated entry defaults to "needs nothing" and is admitted.
    * **cell opaque residue**: `*` (a first-class-dispatched reach) under a
      `wasm-cell` is refused: the cell's confinement is a generated import set,
      and an opaque reach has no representable import. On the container/microVM
      rungs `*` is a REPORT (the envelope binds it regardless), not a refusal.
    """
    if not sandboxes:
        return None
    reach = _component_reach(ir)
    for pname in processes:
        if pname not in sandboxes:
            continue
        env = sandboxes[pname]
        rung = env["isolation"]
        for cname in processes[pname].get("components") or []:
            host_rooted = reach.get(cname, set())
            for cap in sorted(c for c in host_rooted if c != "*"):
                declared = needs.get(cap)
                if declared is None:
                    continue  # advisory default: "needs nothing", admitted
                for resource in declared:
                    kind, path, mode = _parse_need(str(resource))
                    if kind == "?":
                        return (
                            f"[sandbox.needs] entry {cap!r} names {str(resource)!r}, "
                            f"which the sandbox envelope cannot enforce: the envelope "
                            f"vocabulary is fs and net only (env, exec, ipc and device "
                            f"authority are not confined by item 411). Remove the "
                            f"entry, or express the need as an fs/net resource "
                            f"(`net`, `fs:/path:rw`).")
                    # roadmap 422 F5: the spelling is refused before it is
                    # compared, because the comparison is LITERAL. A traversing
                    # need read as covered by a mount that does not grant the
                    # file it denotes, which is the wrong direction for a gate.
                    if kind == "fs":
                        path_err = _bad_fs_path(
                            path or "",
                            f"[sandbox.needs] entry {cap!r} names "
                            f"{str(resource)!r}, whose fs path")
                        if path_err:
                            return path_err
                    if not _need_covered(kind, path, mode, env):
                        grant = (f'net = "{env["net"]}"' if kind == "net"
                                 else (f"fs = [{', '.join(repr(m) for m in env['fs'])}]"
                                       if env["fs"] else "fs = [] (no covering mount)"))
                        want = "net" if kind == "net" else str(resource)
                        grant_hint = 'net = "all"' if kind == "net" else "fs = [...]"
                        return (
                            f"component {cname!r} cannot run in sandbox {pname!r}: "
                            f"capability {cap!r} needs {want}, but the sandbox grants "
                            f"{grant}; grant it ([processes.{pname}.sandbox] "
                            f"{grant_hint}), "
                            f"serve it across the seam instead, or move the component "
                            f"out of the sandbox")
            if "*" in host_rooted and rung == "wasm-cell":
                return (
                    f"component {cname!r} reaches `*` (an opaque, first-class-"
                    f"dispatched host surface) but is placed in the `wasm-cell` "
                    f"sandbox {pname!r}: a cell's confinement is a generated import "
                    f"set and an opaque reach has no representable import. Use the "
                    f"`container` rung, whose runtime envelope binds an opaque body "
                    f"regardless.")
    return None


def render_sandbox_summary(processes: dict, sandboxes: dict, reach: dict,
                           needs: dict, requires: dict, provides: dict,
                           owner: dict, backends: dict,
                           achieved: dict | None = None) -> list[str]:
    """The per-sandboxed-process boot-summary / audit lines: the envelope-scope
    note, the opaque-reach report, the seam-served keys with each provider's
    reach, the claimed-vouched externs, the net=none egress note, and the
    ENFORCEMENT line. Empty for a placement with no sandbox (additivity).

    `achieved` (item 411 Slice 2) is the per-process record the runtime driver
    returns once it has established the boundary and its in-sandbox canary has
    confirmed it. Given one, the enforcement line reports the rung actually
    ACHIEVED plus the canary's evidence, so what the composition is running
    under is auditable rather than inferred from the manifest. Without one (the
    static `revl audit --placement` view, which launches nothing) it reports
    which rungs have a runtime driver at all — and therefore which placements
    `revl run` would refuse on this build."""
    lines: list[str] = []
    for pname in processes:
        if pname not in sandboxes:
            continue
        env = sandboxes[pname]
        comps = processes[pname].get("components") or []
        host_rooted: set = set()
        for cname in comps:
            host_rooted |= reach.get(cname, set())
        lines.append(f"  sandbox {pname}: envelope confines fs+net only; "
                     f"env/exec/ipc unenforced")
        if "*" in host_rooted:
            lines.append("    reach * (opaque host surface), bounded only by the envelope")
        # seam-served keys: required by this process, owned by ANOTHER process
        seam_keys = sorted(k for k in requires.get(pname, {})
                           if k not in provides.get(pname, {})
                           and owner.get(k) not in (None, pname))
        if seam_keys:
            pad = " " * len("    seam-served: ")
            for i, key in enumerate(seam_keys):
                head = "    seam-served: " if i == 0 else pad
                lines.append(f"{head}{key} -> "
                             f"{_reach_descriptor(owner[key], sandboxes, backends)}")
        vouched = sorted(c for c in host_rooted if c != "*" and c in needs)
        if vouched:
            lines.append("    vouched self-contained (claimed, unverified): "
                         + ", ".join(vouched))
        lines.append(f"    note: net={env['net']} bounds this process's own egress, "
                     f"not the reach of its seam-served providers")
        lines.extend(_enforcement_lines(pname, env, (achieved or {}).get(pname)))
    return lines


def _enforcement_lines(pname: str, env: dict, achieved: dict | None) -> list[str]:
    """The item-411 Slice-2 enforcement rows for one sandboxed process."""
    rung = env["isolation"]
    if achieved:
        lines = [f"    enforcement: rung {rung} ACHIEVED via {achieved['runtime']} "
                 f"(image {achieved.get('image')!r})"]
        for note in achieved.get("evidence") or []:
            lines.append(f"      canary: {note}")
        host = achieved.get("host_mounts") or []
        if host:
            # the mounts the DRIVER adds so the confined process can be the
            # runner at all (its own sources, and the conductor's runtime when
            # the image does not carry one). They widen what the body can read
            # beyond the declared envelope, read-only, so they are named here
            # rather than left to be discovered.
            lines.append("      note: driver-added read-only mounts, outside the "
                         "declared envelope: " + ", ".join(p for p, _ in host))
        return lines
    if resolve_sandbox_driver(rung) is None:
        return [f"    enforcement: NONE — rung {rung} has no runtime driver in this "
                f"build, so `revl run --placement` REFUSES this placement rather "
                f"than running {pname} unconfined (Slice 2 implements `container`)"]
    return [f"    enforcement: rung {rung} has a runtime driver; `revl run "
            f"--placement` establishes the boundary at boot, verifies it with an "
            f"in-sandbox canary, and refuses if it cannot"]


def sandbox_audit_view(ir: dict, placement: dict) -> tuple[list[str], str | None]:
    """`revl audit --placement`: the sandbox envelope + per-key reach + claimed-
    vouched list for a placement, computed off the same reach walk the gate and
    boot summary use. Returns `(lines, None)` or `([], diagnostic)`. Empty lines
    (no diagnostic) when the placement declares no sandbox."""
    expanded, err = expand_tiers(
        placement, [c["name"] for c in ir.get("components") or []])
    if err:
        return [], err
    processes = expanded.get("processes") or {}
    needs = (expanded.get("sandbox") or {}).get("needs") or {}
    sandboxes: dict[str, dict] = {}
    for pname, pconf in processes.items():
        sb = pconf.get("sandbox")
        if sb is None:
            continue
        normalized, sb_err = _normalize_sandbox_table(sb)
        if sb_err:
            return [], f"process {pname!r} [sandbox]: {sb_err}"
        sandboxes[pname] = normalized
    if not sandboxes:
        return [], None
    components = {c["name"]: c for c in ir.get("components") or []}

    def _merged(cnames, which):
        out: dict[str, str] = {}
        for cname in cnames:
            out.update((components.get(cname) or {}).get(which) or {})
        return out

    requires = {p: _merged(pc.get("components") or [], "requires")
                for p, pc in processes.items()}
    provides = {p: _merged(pc.get("components") or [], "provides")
                for p, pc in processes.items()}
    owner = {key: p for p, keys in provides.items() for key in keys}
    backends = {p: _canonical_backend(pc.get("backend", "py"))
                for p, pc in processes.items()}
    reach = _component_reach(ir)
    lines = ["sandbox placement (item 411): envelope confines fs+net only "
             "(env/exec/ipc/devices unenforced); the envelope, not the needs "
             "table, is the security boundary"]
    for pname in processes:
        if pname in sandboxes:
            lines.append("  " + process_tag(pname, processes, backends, sandboxes))
    lines.extend(render_sandbox_summary(
        processes, sandboxes, reach, needs, requires, provides, owner, backends))
    return lines, None


# --------------------------------------------------------------------------
# live swap: admission gate (roadmap §23, `revl swap <component> --to <backend>`)
# --------------------------------------------------------------------------


def _seam_model(candidate: dict, component: str, backend: str,
                from_backend: str | None, fallback: dict | None = None):
    """The two-process topology a seam decision is made over: `component` alone
    in its own process on `backend`, everything else in the process it is
    reached FROM, on `from_backend` (default `py`). Returns the four maps the
    plan-time gates take — ``(requires, provides, owner, backends)`` — so the
    swap gate and the boot re-admission below decide over the SAME model,
    built once.

    Every key the moving half provides and the rest requires (outbound), and
    every key the rest provides and the moving half requires (inbound), is a
    cross-process seam in this model; a key a half both provides and requires
    is served locally and the gates skip it.
    """
    cand_comps = candidate.get("components") or []
    moving = next((c for c in cand_comps
                   if c.get("name") == component), fallback) or {}
    move_provides = moving.get("provides") or {}
    move_requires = moving.get("requires") or {}
    rest_provides: dict[str, str] = {}
    rest_requires: dict[str, str] = {}
    for other in cand_comps:
        if other.get("name") == component:
            continue
        rest_provides.update(other.get("provides") or {})
        rest_requires.update(other.get("requires") or {})
    model_provides = {"__moving__": dict(move_provides), "__rest__": rest_provides}
    model_requires = {"__moving__": dict(move_requires), "__rest__": rest_requires}
    model_owner = {k: "__moving__" for k in move_provides}
    model_owner.update({k: "__rest__" for k in rest_provides})
    model_backends = {"__moving__": backend, "__rest__": from_backend or "py"}
    return model_requires, model_provides, model_owner, model_backends


def seam_readmission(files, running_ir: dict, component: str, backend: str,
                     from_backend: str | None = None):
    """Re-admit `component` AS IT ALREADY IS, on the tier it is already placed
    on, against the running manifest. Returns ``(candidate_ir, None)`` when the
    seam is admissible or ``(None, diagnostic)`` when it is refused. This is the
    question a CONSUMER asks at boot before it wires a proxy to a provider
    another process already hosts (item 337 Seam 2, `_process_runner.
    _boot_wiring_decision`).

    It is deliberately NOT `swap_admission`. A swap asks "may this component be
    MOVED to that tier", and its extra outbound refusal — a sync `fn`/`emission`
    the swap would re-point a LIVE consumer's call site across — is a
    relocation-only hazard: at a cutover an in-address-space call site becomes a
    cross-process one under a running consumer. At boot nothing moves and
    nothing is re-pointed. The provider is already on its tier, the seam already
    exists, the conductor already planned it, and the consumer's call sites were
    compiled against that seam from the start. Asking the swap question there
    refused seams the conductor deliberately sanctions — a same-tier py<->py
    value-typed seam whose service happens to be address-space-bound is
    permitted by `cross_tier_boundary_check` by construction ("the sync REPORT
    half stays cross-tier only"), and the cross-tier form is permitted-and-
    reported, never refused.

    So the boot seam asks EXACTLY what the conductor answered when it planned
    the placement: recompile against the running manifest (the structural
    G2/G3 half `swap_admission` also runs) and then run the conductor's own
    plan-time seam gate, `cross_tier_boundary_check`, over the seam's topology.
    A resource crossing and a `cache`-split crossing are refused, tier-
    agnostically; a sync crossing is permitted, exactly as at plan time. The
    report lines are dropped here: the conductor already printed them once, and
    a consumer re-deriving them would double every advisory.

    The honest limit, named rather than smuggled past: the model lumps every
    non-provider component into one "rest" process, so a key the real placement
    serves locally between two co-located components is modelled as a seam.
    That is the same approximation `swap_admission` already commits to, and it
    can only refuse MORE than the conductor did, never less.
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
                      f"composition (nothing to re-admit at this seam)")

    model_requires, model_provides, model_owner, model_backends = _seam_model(
        candidate, component, backend, from_backend, fallback=running_comp)
    problem, _report = cross_tier_boundary_check(
        candidate, model_requires, model_provides, model_owner, model_backends,
        candidate.get("services") or {})
    if problem is not None:
        return None, problem
    return candidate, None


def swap_admission(files, running_ir: dict, component: str, backend: str,
                   from_backend: str | None = None):
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
    * **the seam is crossable, in both directions.** A tier swap moves the
      component into its own process, so every seam between it and the rest of
      the composition becomes cross-process, INBOUND and OUTBOUND alike. A
      resource-type return cannot cross a process seam by copy in either
      direction, so a resource crossing on the services the component provides
      (consumed by a component staying behind) OR on the services it requires
      (from a component staying behind) is refused, through the same tier-
      agnostic predicate the plan-time gate uses (`resource_crossing_refusal`).
      The outbound side additionally refuses a re-pointed sync `fn`/`emission`:
      an existing consumer's live call site is re-pointed across the new seam and
      only a transport-safe (async, value-typed) service survives that re-point.
      All of this is decided before anything is booted, and `do_swap` re-runs the
      real plan-time gate (`cross_tier_boundary_check` + `tier_capability_gate`)
      over the post-swap topology, so the swap-vs-plan parity is actual and
      symmetric rather than prose.

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

    # Model the post-swap topology as two processes: the moving component alone
    # (it goes to its own process on `backend`, v1 scope) and everything staying
    # behind. Every seam between the two halves becomes cross-process, in BOTH
    # directions: the services the component PROVIDES that another consumes
    # (outbound) and the services it REQUIRES from a component staying behind
    # (inbound). A resource handle cannot cross a process seam by copy either
    # way, so refuse a resource crossing on EITHER side through the SAME shared
    # predicate the plan-time gate uses (`resource_crossing_refusal`), keeping
    # the swap-vs-plan parity actual and symmetric rather than a private loop
    # that only walked the provides side.
    model_requires, model_provides, model_owner, model_backends = _seam_model(
        candidate, component, backend, from_backend, fallback=running_comp)
    resource_problem = resource_crossing_refusal(
        candidate, model_requires, model_provides, model_owner, model_backends)
    if resource_problem is not None:
        return None, resource_problem

    # The provides side (outbound) additionally refuses a re-pointed SYNC/emission
    # service: an existing consumer's live call site is re-pointed across the new
    # seam, and only a transport-safe (async, value-typed) service survives that
    # re-point. The inbound side is a FRESH successor wiring (no live re-point),
    # so a sync inbound seam is permitted here exactly as the plan-time gate
    # permits (and reports) a cross-tier sync seam.
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
# capability/realm-aware host placement (roadmap item 119)
# --------------------------------------------------------------------------
#
# Item 56/149/151 gave placement a *backend* (which tier) and a *network seam*
# (which machine). This dimension is which HOST by capability and realm: a host
# (process) may declare the capabilities it offers/permits and the realm it is
# pinned to, and a component is refused on a host that lacks a capability it
# needs or that is pinned to a realm the component does not belong to. Both are
# read off the placement toml (no `.rvl` source change) and the realms already
# in the IR (`isolate`, item 10/56). The whole dimension is opt-in: a component
# that requires no capability and isolates into no named realm is never
# constrained, so a placement that declares neither validates trivially and is
# byte-identical to today (docs/capability-realm-placement.md).


def _offered_capabilities(pconf: dict) -> set[str]:
    """The capabilities a host (process) declares it offers/permits, from
    `[processes.<p>] capabilities = ["net", "db"]`. A process with no
    `capabilities` key offers none — a component that needs a capability must
    land on a host that lists it."""
    return {str(c) for c in (pconf.get("capabilities") or [])}


def _required_capabilities(placement: dict) -> dict[str, set[str]]:
    """`{component: {capability, ...}}` from the placement's `[capabilities]`
    table (a flat map, `PgDatabase = ["db"]`). This is the placement-layer
    spelling of "this component reaches host code that needs a permit"; it adds
    no `.rvl` grammar."""
    out: dict[str, set[str]] = {}
    for cname, caps in (placement.get("capabilities") or {}).items():
        out[cname] = {str(c) for c in (caps or [])}
    return out


def _component_realms(entry: dict) -> set[str]:
    """The named (non-shared) realms a manifest component entry isolates any key
    into — the realms the component *belongs to* (item 56's isolate dimension).
    A component that isolates nothing (or only into the shared realm) belongs to
    no named realm and is realm-unconstrained."""
    from .lower import SHARED_REALM  # noqa: PLC0415 — avoid a top-level cycle
    return {r for r in (entry.get("isolate") or {}).values() if r != SHARED_REALM}


def capability_realm_diagnostic(processes: dict, ir: dict,
                                required_caps: dict[str, set[str]]) -> str | None:
    """Validate capability- and realm-consistent placement; return a diagnostic
    for the first violation, or None when every component sits on a host that
    offers the capabilities it requires and whose declared realm (if any) is
    consistent with the realms the component belongs to.

    Two rules, both pure reads off the toml + IR, checked before anything spawns:

    * **capability** — a component that requires capability `c` (via the
      `[capabilities]` table) may be placed only on a host whose
      `capabilities` list includes `c`. A host that offers a strict superset is
      fine; one missing any needed capability is refused.
    * **realm** — a host may pin itself to a realm (`[processes.<p>] realm =
      "tenant_a"`). A component placed on a pinned host must belong to no realm
      other than that one: a component isolating a key into a *foreign* named
      realm is refused. A shared/unisolated component belongs to no named realm
      and rides on any host; an *unpinned* host constrains no component. This is
      the placement-time form of G2's per-(key, realm) disjointness — it keeps a
      realm-isolated component off a host that carries a different realm.
    """
    entries = {e["name"]: e for e in (ir.get("manifest") or {}).get("components") or []}
    for pname, pconf in processes.items():
        offered = _offered_capabilities(pconf)
        host_realm = pconf.get("realm")
        for cname in pconf.get("components") or []:
            missing = sorted((required_caps.get(cname) or set()) - offered)
            if missing:
                have = ", ".join(sorted(offered)) or "no capabilities"
                return (
                    f"component {cname!r} requires capability "
                    f"{', '.join(repr(m) for m in missing)} but is placed on host "
                    f"{pname!r}, which offers {have} — a component may run only on a "
                    f"host that offers every capability it needs (add it to "
                    f"[processes.{pname}] capabilities, or move {cname!r} to a host "
                    "that offers it)")
            if host_realm is not None:
                foreign = sorted(_component_realms(entries.get(cname) or {}) - {str(host_realm)})
                if foreign:
                    return (
                        f"component {cname!r} isolates a key into realm "
                        f"{foreign[0]!r} but is placed on host {pname!r}, which is "
                        f"pinned to realm {str(host_realm)!r} — a realm-isolated "
                        "component must stay on a host consistent with its realm "
                        "(G2's per-(key, realm) disjointness at placement time)")
    return None


def colocation_advice(processes: dict, placed: dict, ir: dict) -> list[str]:
    """Realm co-location opportunities: any named realm whose components are
    split across more than one host carries at least one same-realm seam that
    pinning the realm to a single host would remove (item 56's realm dimension).

    Advisory only — returns human-readable lines and changes no placement. The
    conductor prints them only when the placement sets `report_colocation =
    true`, so default runs are byte-identical. Conservative by construction: it
    never moves a component, it only names where a realm-affinity co-location
    could drop a seam."""
    entries = {e["name"]: e for e in (ir.get("manifest") or {}).get("components") or []}
    realm_hosts: dict[str, set[str]] = {}
    for cname, host in placed.items():
        for realm in _component_realms(entries.get(cname) or {}):
            realm_hosts.setdefault(realm, set()).add(host)
    lines: list[str] = []
    for realm in sorted(realm_hosts):
        hosts = realm_hosts[realm]
        if len(hosts) > 1:
            lines.append(
                f"realm {realm!r} spans hosts {', '.join(sorted(hosts))}; co-locating "
                "its components on one host removes a same-realm seam")
    return lines


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


def _names_in(node, acc: set, bound: frozenset = frozenset()) -> set:
    """Every extern/fn/reference name reachable in an IR subtree — enough to
    tell whether a component's body reaches a given extern (an extern or
    top-level-fn call is a `{"kind": "fn", "name": "<extern>"}` node; a bare
    reference an `{"kind": "name", "id": ...}` or `{"kind": "var", "name":
    ...}`).

    Kind-aware over parameter binders (item 363 hardening F6): a method or
    lambda PARAMETER shadows any same-named extern within its body, so a bare
    reference whose name is a bound parameter does NOT count as reaching that
    extern. Otherwise a component whose method merely has a parameter spelled
    like a `@py`-only extern would be dragged into a native slice and refused
    blaming the innocent component. This is sound because the lowerer has
    already resolved scope: a `{"kind": "fn", ...}` extern call never names a
    shadowed local (a call to a param lowers to a `call` over a `name`/`var`),
    so extern/fn calls are always counted; only bare `name`/`var` references are
    filtered by the parameter set. Non-parameter binders (let/for/match) are
    left over-inclusive — the safe direction, never dropping a reached extern."""
    if isinstance(node, dict):
        kind = node.get("kind")
        # a method/lambda introduces its parameters as binders for its body
        params = node.get("params")
        inner = (bound | {p for p in params if isinstance(p, str)}
                 if isinstance(params, list) else bound)
        if kind in ("name", "var"):
            ref = node.get("id") if kind == "name" else node.get("name")
            if isinstance(ref, str) and ref not in bound:
                acc.add(ref)
        else:
            for field in ("name", "id"):
                value = node.get(field)
                if isinstance(value, str) and value not in bound:
                    acc.add(value)
        for value in node.values():
            _names_in(value, acc, inner)
    elif isinstance(node, list):
        for value in node:
            _names_in(value, acc, bound)
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

    This drops exactly the un-emittable part: every extern the ts emitter
    refuses (`_ts_unemittable_externs` — no `@ts` body AND no `@ts ref`), and
    every component (or top-level fn) whose body reaches one. Services, types
    and ts-safe components are kept verbatim — a composition with no such
    extern is returned byte-identical, so existing node placements are
    unaffected. The dropped provider still runs, on its own (py) process; the
    node process consumes it as a proxy.

    The `@ts ref` half of that predicate is item 225. Classifying by `bodies`
    alone counted a `= @ts ref` extern — empty `bodies`, populated `refs` — as
    un-emittable, when it is precisely an extern the ts tier CAN spell: the
    emitter turns it into a lazy import thunk (backends/typescript/emit.py,
    item 396 option B). So this deleted, and `tier_capability_gate` refused,
    every component reaching one, which is why no node placement ever carried a
    `spec.refs` entry and the runner's deploy-contract hash check had nothing
    to verify.
    """
    externs = ir.get("externs") or []
    unemittable = _ts_unemittable_externs(ir)
    if not unemittable:
        return ir

    def reaches_unemittable(carrier) -> bool:
        return bool(_names_in(carrier, set()) & unemittable)

    out = dict(ir)
    out["externs"] = [e for e in externs if e.get("name") not in unemittable]
    out["components"] = [c for c in ir.get("components") or []
                         if not reaches_unemittable(c)]
    out["functions"] = [f for f in ir.get("functions") or []
                        if not reaches_unemittable(f)]
    return out


def placement_slice(ir: dict, kept) -> dict:
    """The slice of a linked composition a process hosts (roadmap item 363,
    the general form of `ts_safe_ir`). Keep the `kept` components, every
    service and type declaration, and every top-level fn and extern REACHED
    from a kept component (transitively through kept fns); drop the rest.

    Two load-bearing properties, each an exit test:

    * **Additive.** A process that hosts every component gets the full IR back
      byte-identical (the guard below), so a single-process placement — and
      every all-same-tier placement — builds exactly today's artifact.
    * **Unblocking.** A `@py`-only extern reached only by py-placed components
      never reaches the rust/go emitter, so a composition whose control plane
      touches a `@py` extern can still place its hot worker on rust. This is
      `ts_safe_ir`'s job done once, uniformly, by placement declaration instead
      of per-tier body inference; `ts_safe_ir` stays as a second ts-specific
      filter applied after this slice.

    Reachability reuses the deliberately over-inclusive `_names_in` name walk
    (it never drops an extern a kept component still reaches). A shared pure fn
    reached from both sides of a seam is kept in BOTH slices — correct (pure
    code has no home process), and exactly where the item-385 cross-tier
    byte-equality discipline bites.
    """
    all_names = {c.get("name") for c in ir.get("components") or []}
    kept = {k for k in kept if k in all_names}
    if kept >= all_names:
        return ir  # hosts everything -> the whole document, byte-identical
    components = [c for c in ir.get("components") or [] if c.get("name") in kept]
    functions = ir.get("functions") or []
    externs = ir.get("externs") or []
    reachable: set = set()
    for comp in components:
        _names_in(comp, reachable)
    # fixpoint: a kept fn may reach further fns/externs (a shared helper chain)
    changed = True
    while changed:
        changed = False
        for fn in functions:
            if fn.get("name") in reachable:
                before = len(reachable)
                _names_in(fn, reachable)
                changed = changed or len(reachable) != before
    out = dict(ir)
    out["components"] = components
    out["functions"] = [f for f in functions if f.get("name") in reachable]
    out["externs"] = [e for e in externs if e.get("name") in reachable]
    return out


def host_ref_pins(ir: dict, own, files) -> dict:
    """The three host-module pin keys a placement spec carries for a process
    hosting the components `own` (item 396 option B / 410).

    * `refRoot` — the user root compile tree a non-stdlib `@ts ref` resolves
      and hash-checks against;
    * `stdlibRefRoot` — the install tree a stdlib-origin ref resolves against
      (the runner self-derives this one, but the spec states it);
    * `refs` — the per-ref hash-check list the node runner walks BEFORE it
      imports the emitted module, so a host module that changed since compile
      refuses the boot instead of running host code.

    Built for the components the process actually hosts, not for the whole
    composition: a process must not hash-check the refs of an extern its slice
    never reaches (F4).  Harmless for non-node backends, which ignore the keys.

    ONE function, called by the boot path AND by `do_swap`'s successor. A swap
    re-hosts a component in a NEW process, and a spec key that carries a
    security property must survive that or the guarantee only reads "held until
    the first swap". The pins are read off the SAME running `ir` the tier
    artifact is emitted from (`ensure_backend` slices this very document), so
    they always describe the bytes the process is about to load — never a
    re-hash of whatever is on disk at swap time, which would bless a host
    module that changed since the composition was compiled.
    """
    own_externs = {e.get("name")
                   for e in placement_slice(ir, set(own)).get("externs") or []}
    return {
        "refRoot": (os.path.dirname(os.path.abspath(str(files[0])))
                    if files else ""),
        "stdlibRefRoot": str(stdlib_root().parent),
        "refs": [
            {"extern": e.get("name"), "path": r["path"],
             "sha256": r["sha256"],
             **({"root": r["root"]} if r.get("root") else {})}
            for e in ir.get("externs") or []
            if e.get("name") in own_externs
            for r in [(e.get("refs") or {}).get("ts")] if r is not None
        ],
    }


# per-backend emitter modules, imported lazily for the plan-time capability
# dry-run. Every emitter is pure-python codegen (stdlib only), so the gate
# needs no tier toolchain — a fn-typed component placed on java is refused at
# plan time with the emitter's own tier-limit message, never a javac stderr.
_EMIT_GATE_MODULES: dict[str, object] = {}
_EMIT_GATE_PATHS = {
    "py": _BACKENDS_DIR / "python" / "emit.py",
    "node": _TS_DIR / "emit.py",
    "rust": _BACKENDS_DIR / "rust" / "emit.py",
    "go": _GO_DIR / "emit.py",
    "java": _JAVA_DIR / "emit.py",
}


def _emit_gate_module(backend: str):
    if backend not in _EMIT_GATE_MODULES:
        path = _EMIT_GATE_PATHS[backend]
        spec = importlib.util.spec_from_file_location(f"revl_{backend}_emit_gate", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _EMIT_GATE_MODULES[backend] = module
    return _EMIT_GATE_MODULES[backend]


def _ts_unemittable_externs(ir: dict) -> set[str]:
    """Externs the ts emitter cannot spell — the ones `ts_safe_ir` deletes (and
    every component reaching one with them).

    THE predicate, in one place, mirroring the emitter's own two-arm decision in
    `backends/typescript/emit.py::_emit_ts_externs`:

      * `"ts" in refs and "ts" not in bodies` -> a lazy import thunk (item 396
        option B). EMITTABLE.
      * `"ts" in bodies`                      -> the verbatim body. EMITTABLE.
      * neither                               -> `EmitError`. Un-emittable.

    Item 225: this used to test `bodies` alone, so a `= @ts ref` extern (empty
    `bodies`, populated `refs`) fell in the un-emittable arm and every component
    reaching one was refused the node tier at plan time — the reason the item
    396(B) / 410 host-module pin check in `placement_runner.ts` had never once
    run with a pin to verify. Keep this function the single definition: a second
    copy of the test is exactly how the two drifted from the emitter.
    """
    return {e.get("name") for e in ir.get("externs") or []
            if "ts" not in (e.get("bodies") or {})
            and "ts" not in (e.get("refs") or {}) and e.get("name")}


def _dryrun_emit(backend: str, sliced: dict) -> None:
    """Run `backend`'s emitter over `sliced` via the SAME entry point the real
    build uses for that tier, and let its tier-limit `EmitError` propagate. The
    emitters ARE the capability oracle (docs/conformance.md): each tier limit
    lives as a named refusal at emit time, so dry-running the emit keeps the
    gate exactly as strong as the real refusal set with no second list to drift
    (the design's recommended shape).

    Two per-tier alignments the initial cut got wrong:

    * **go** — the build runs `emit_placement` (item 363's `_emit_v3_placement`
      + interop bridge), but the gate ran `emit`, which for an ir_version-3 doc
      with any top-level fn/type/extern routes to `_emit_v3_go` and DROPS the
      components. Every tier limit in the component/bridge codegen path escaped
      the gate and surfaced as a raw "go emit failed" RuntimeError AFTER the
      gate said yes — the exact failure stage-3 exists to eliminate (F2). rust,
      node and java already agree with their builds via `emit`.
    * **node** — the build (and this dry-run) narrows through `ts_safe_ir`,
      which DELETES any component reaching a ts-unemittable extern rather than
      refusing it, so a node-placed dirty component would be silently omitted
      from the artifact while the spec still lists it (a boot crash, F3). Diff
      the placed component set against `ts_safe_ir`'s output and REFUSE at plan
      time, naming the component + the extern it reaches. An extern with a
      `@ts ref` is NOT such an extern (item 225): it emits as a lazy import
      thunk, so a component reaching one is admitted here and the node runner
      hash-checks the ref against its compile-time pin before importing it."""
    module = _emit_gate_module(backend)
    if backend == "node":
        placed_comps = {c.get("name") for c in sliced.get("components") or []}
        safe = ts_safe_ir(sliced)
        kept_comps = {c.get("name") for c in safe.get("components") or []}
        dropped = placed_comps - kept_comps
        if dropped:
            unemittable = _ts_unemittable_externs(sliced)
            by_name = {c.get("name"): c for c in sliced.get("components") or []}
            details = []
            for cname in sorted(n for n in dropped if n is not None):
                reached = sorted(_names_in(by_name.get(cname) or {}, set())
                                 & unemittable)
                reach_str = ", ".join(reached) or "a py-only extern"
                details.append(f"{cname} (reaches {reach_str})")
            raise RuntimeError(
                "a node-placed component reaches a `@py`-only extern (no `@ts` "
                "body and no `@ts ref`), which the ts tier cannot emit: "
                + "; ".join(details)
                + " — a py-only provider must stay on the py tier and be reached "
                "across the seam as a bridge proxy (place it on `py`, give the "
                "extern a `@ts` body, or point it at a host module with "
                "`= @ts ref sym from \"...\"`)")
        module.emit(safe)
    elif backend == "go":
        module.emit_placement(sliced, "emitted")
    else:  # py, rust, java
        module.emit(sliced)


def tier_capability_gate(ir: dict, placed: dict, backends: dict) -> str | None:
    """Refuse, at plan time, a component placed on a tier that cannot emit it,
    naming the component, the tier, and the tier's own reason — never a raw
    toolchain error (roadmap item 363, stage 3).

    Mechanism: dry-run each tier's placement slice through its emitter before
    anything spawns. On a refusal, attribution re-emits single-component
    sub-slices to name the culprit (bounded work, only on the failure path).
    `py` is skipped — it is the reference tier and refuses no construct that
    compiled — so a placement using only `py` processes runs no gate and is
    byte-identical.
    """
    # components hosted on each compiled tier, in load order for a stable slice
    by_backend: dict[str, list[str]] = {}
    order = [c.get("name") for c in ir.get("components") or []]
    for cname in order:
        backend = backends.get(placed.get(cname))
        if backend and backend != "py":
            by_backend.setdefault(backend, []).append(cname)
    for backend, comps in by_backend.items():
        try:
            _dryrun_emit(backend, placement_slice(ir, set(comps)))
        except Exception as whole:  # noqa: BLE001 — any refusal is a plan diagnostic
            culprit, reason = None, str(whole).strip()
            for cname in comps:
                try:
                    _dryrun_emit(backend, placement_slice(ir, {cname}))
                except Exception as single:  # noqa: BLE001
                    culprit, reason = cname, str(single).strip()
                    break
            named = f"component {culprit!r}" if culprit else "a component"
            return (
                f"{named} cannot be placed on the `{backend}` tier: {reason}\n"
                f"       (place it on a tier that supports the construct, or "
                f"keep it on the default tier; the `{backend}` emitter is the "
                f"capability oracle — docs/conformance.md)")
    return None


def resource_crossing_refusal(ir: dict, requires: dict, provides: dict,
                              owner: dict, backends: dict) -> str | None:
    """Refuse a resource-type value crossing ANY cross-process seam, TIER-
    AGNOSTIC (Finding A). Returns a diagnostic naming the service, method and
    resource, or None.

    A resource type (an `extern acquire` return, or any record/variant that
    carries one transitively — `_resource_taint`) cannot cross a PROCESS
    boundary by copy: its lifetime is tied to a fiber in the providing process,
    so a copy is a dead handle detached from its undo/teardown contract. That is
    true whether the two processes run on the SAME backend (py<->py) or on
    different tiers. The refusal therefore runs on every cross-process seam the
    conductor wires, not only the cross-tier ones — the same seams the
    proxy/serve construction loop in `run_placement` builds.

    Only a CROSS-PROCESS crossing is refused. A resource shared WITHIN one
    process is fine: a key a process both requires and provides is served
    locally (`key in provides[consumer]`) and skipped here, so two components
    co-located in one process pass a handle in memory, ungated. A key with no
    local owner is a remote seam (item 151), a separate composition reached by
    address; there is no local provider to gate here.

    The refusal keys on the STRUCTURED `resources` kind from `distributability`,
    not on the substring of a human reason string — a wording change to the
    reason cannot silently disarm it (item 363 F1). The closure in
    `_resource_taint` makes it fire for a handle NESTED in a user record or a
    variant case payload, and the signature-level scan resolves a closed
    generic argument (`ConnG[Socket]`), so a renamed/wrapped handle is caught
    too.
    """
    verdicts = distributability(ir)
    for consumer, keys in requires.items():
        for key, service in keys.items():
            if key in provides.get(consumer, {}):
                continue  # served locally in-process, not a seam
            host = owner.get(key)
            if host is None:
                continue  # a remote seam (item 151) has no local owner to gate
            # host != consumer here (a locally-served key was skipped above), so
            # this key genuinely crosses a process boundary; refuse a resource
            # crossing it regardless of whether the tiers match.
            resource_hits = (verdicts.get(service) or {}).get("resources") or []
            if resource_hits:
                resource_reasons = [
                    f"{h['method']}: resource type {h['type']} crosses"
                    for h in resource_hits]
                same_tier = backends.get(host) == backends.get(consumer)
                boundary = (
                    f"same-tier process {host!r} -> {consumer!r} on "
                    f"{backends.get(consumer)}" if same_tier else
                    f"tier boundary {backends.get(consumer)} <- {backends.get(host)}")
                return (
                    f"service `{service}` (key {key!r}) crosses the {boundary} "
                    f"but is address-space-bound: {', '.join(resource_reasons)}. "
                    "An OWNED resource handle's bracket lives in the providing "
                    "process (item 308); it cannot cross a process seam by copy "
                    "(same tier or across tiers) — a copy is a dead handle "
                    "detached from its undo/teardown contract — and a witnessed "
                    "rollback across a seam is out of scope (parity with the "
                    "`revl swap` gate, docs/interop-bridge.md §3-4).")
    return None


def cache_crossing_refusal(ir: dict, requires: dict, provides: dict,
                           owner: dict) -> str | None:
    """Refuse a `cache`-declaring seam method split across a PROCESS boundary
    (roadmap item 310, §invalidated_by scope). Returns a diagnostic naming the
    service, method and split, or None.

    "Ordered by the same WAL" presumes ONE WAL: the item-310 entry store is
    per-session and single-process. A composition placed across processes has
    each child firing its own extern crossings locally, where no shared WAL
    orders an `invalidated_by` crossing against an entry held in another process
    — a per-process private cache with cross-process invalidation traffic is
    exactly the stale-read bug the freshness clause exists to prevent. Until the
    federation surface lands, a distributed placement refuses `cache` on any
    method the placement splits from its invalidating crossings, at admission,
    next to the resource-crossing refusal. The same composition placed in ONE
    process admits (this check only fires on a cross-process seam)."""
    services = ir.get("services") or {}
    for consumer, keys in requires.items():
        for key, service in keys.items():
            if key in provides.get(consumer, {}):
                continue  # served locally in-process, not a seam
            host = owner.get(key)
            if host is None:
                continue  # a remote seam (item 151) has no local owner to gate
            methods = (services.get(service) or {}).get("methods") or {}
            cached = sorted(m for m, spec in methods.items()
                            if (spec or {}).get("cache"))
            if cached:
                return (
                    f"service `{service}` (key {key!r}) declares `cache` on "
                    f"{', '.join(cached)} but is split across a process seam "
                    f"({host!r} -> {consumer!r}). The item-310 entry store is "
                    "single-process and WAL-ordered; a per-process private cache "
                    "with cross-process invalidation traffic is the stale-read "
                    "bug `invalidated_by` exists to prevent. Cross-composition "
                    "invalidation is the federation surface (unshipped) — place "
                    "the cache-declaring method in one process, or drop `cache` "
                    "(item 310, §invalidated_by scope).")
    return None


def cross_tier_boundary_check(ir: dict, requires: dict, provides: dict,
                              owner: dict, backends: dict,
                              services: dict) -> tuple[str | None, list[str]]:
    """Plan-time checks over the cross-process seams the conductor creates
    (roadmap item 363, stage 4; Finding A follow-up). Two halves with different
    tier scope:

    * **Refuse** a resource-type crossing, TIER-AGNOSTIC, via
      `resource_crossing_refusal`. A handle cannot cross a process seam by copy
      whether or not the two processes share a backend, so this half runs on
      SAME-TIER seams too (Finding A: the same-tier short-circuit below used to
      let a handle cross two py processes ungated). This is parity with the
      `revl swap` gate (`placement.py` `swap_admission`).
    * **Report** a sync (address-space-bound-for-async-reasons) crossing, CROSS-
      TIER only. A sync `fn`/`emission` behind a cross-tier seam is permitted
      (today's cross-process placements permit it and pay the blocking round-
      trip) but NAMED — a hot worker behind a sync seam is a performance lie the
      author should see before wondering where the native speedup went. A
      same-tier sync seam is unchanged from today (no report line).

    Returns ``(diagnostic_or_None, report_lines)``.
    """
    problem = resource_crossing_refusal(ir, requires, provides, owner, backends)
    if problem is not None:
        return problem, []
    problem = cache_crossing_refusal(ir, requires, provides, owner)
    if problem is not None:
        return problem, []
    verdicts = distributability(ir)
    lines: list[str] = []
    for consumer, keys in requires.items():
        for key, service in keys.items():
            if key in provides.get(consumer, {}):
                continue  # served locally, not a seam
            host = owner.get(key)
            if host is None:
                continue  # a remote seam (item 151) has no local owner
            if backends.get(host) == backends.get(consumer):
                continue  # same-tier seam: no sync report (byte-identical today)
            verdict = verdicts.get(service) or {}
            reasons = verdict.get("reasons") or []
            if verdict.get("verdict") == "address-space-bound":
                lines.append(
                    f"  seam {consumer}.{key} ({backends.get(consumer)} <- "
                    f"{backends.get(host)}): service `{service}` is "
                    f"address-space-bound ({', '.join(reasons)}) — permitted, "
                    "but each call is a blocking cross-tier round-trip")
    return None, lines


def process_graph(requires: dict, provides: dict, owner: dict,
                  remote_keys=()) -> dict[str, set[str]]:
    """The quotient of the component dependency DAG by the placement partition:
    one node per process, one edge ``consumer -> host`` per proxied key.

    This is the same relation the spec loop in `run_placement` turns into
    `spec["proxies"]`: a process proxies every key it requires and does not
    itself provide, and the proxy's target is the process that owns the key.
    A key served in-process is not an edge (no proxy, no socket), and neither
    is a key with no local owner.

    `remote_keys` is excluded deliberately: a `[remotes.<key>]` provider is a
    *separate composition* on its own placement (item 151), reached by address
    alone. Its boot order is not this composition's to reason about, and this
    graph must not pretend to see it.
    """
    edges: dict[str, set[str]] = {p: set() for p in requires}
    for consumer, keys in requires.items():
        for key in keys:
            if key in (provides.get(consumer) or {}):
                continue                      # served locally: no proxy at all
            if key in remote_keys:
                continue                      # another composition (item 151)
            host = owner.get(key)
            if host is None or host == consumer:
                continue                      # unprovided: its own refusal
            edges[consumer].add(host)
    return edges


def _find_process_cycle(edges: dict) -> list[str] | None:
    """The first cycle in `edges` as ``[n0, n1, ..., n0]``, or None.

    The same coloured DFS `_link` runs over components one level down, walked
    in sorted order so the reported cycle is deterministic for a given
    placement rather than dependent on TOML key order.
    """
    state: dict[str, int] = {}
    stack: list[str] = []
    found: list[str] | None = None

    def visit(node: str) -> bool:
        nonlocal found
        state[node] = 1
        stack.append(node)
        for succ in sorted(edges.get(node, ())):
            if state.get(succ) == 1:
                found = stack[stack.index(succ):] + [succ]
                return True
            if state.get(succ, 0) == 0 and visit(succ):
                return True
        stack.pop()
        state[node] = 2
        return False

    for node in sorted(edges):
        if state.get(node, 0) == 0 and visit(node):
            return found
    return None


def process_cycle_refusal(requires: dict, provides: dict, owner: dict,
                          placed: dict, components: dict,
                          remote_keys=(), placement_path: str = "") -> str | None:
    """Refuse a placement whose PROCESS graph has a cycle. Returns a diagnostic
    naming the cycle and the proxied key that closes each hop, or None.

    G3 proves the COMPONENT graph acyclic. Placement then quotients that graph
    by a process partition, and **a quotient of a DAG can contain a cycle**:
    four components in two disjoint chains, split crosswise, give two processes
    that each require a key the other provides while the component graph stays
    a pair of disjoint edges. G3 passes and `loadOrder` is produced.

    The boot order is what makes a process edge a *wait* edge.
    `_process_runner.run` is three steps in this order: (1) wire every proxy
    for keys provided by other processes, (2) activate this process's own
    components, (3) serve the keys other processes need. A process therefore
    connects to all of its providers *before it starts listening*, so on a
    cycle every process blocks in step 1 and none reaches step 3.
    `bridge._connect`'s docstring states the assumption this breaks — the retry
    loop "makes start order irrelevant", which is true of a DAG of processes
    and false of a cycle. The failure is bounded (100 attempts at 50 ms, then a
    `ConnectionError`) rather than eternal, so the composition dies at boot
    instead of wedging; it is still a composition that admits and cannot run.

    This **refuses** rather than warns, and the reason is that the check is
    exact: the graph is finite, wholly derived from the placement, and there is
    no approximation anywhere in it, so it has no false positives. Promoting
    this one shape while a general liveness analysis stays warn-only is the
    per-shape rule in `docs/design/438-petri-reachability.md` §9, not an
    exception to it — and §8.1 is this function.

    Two scoping decisions, recorded rather than left implicit:

    * **`remote` seams are excluded** (`process_graph`): a `[remotes.<key>]`
      provider is a separate composition on its own placement (item 151),
      reached by address; this graph cannot see it and must not pretend to.
    * **The check is on the PARTITION, not on the code.** The same components
      in a different partition are fine, so the refusal names the placement
      file and the processes, never the components' authors.
    """
    edges = process_graph(requires, provides, owner, remote_keys)
    cycle = _find_process_cycle(edges)
    if cycle is None:
        return None

    def _requirers(pname: str, key: str) -> str:
        names = sorted(c for c, home in placed.items()
                       if home == pname and key in (components.get(c, {}).get("requires") or {}))
        return ", ".join(names) or pname

    def _providers(pname: str, key: str) -> str:
        names = sorted(c for c, home in placed.items()
                       if home == pname and key in (components.get(c, {}).get("provides") or {}))
        return ", ".join(names) or pname

    hops: list[str] = []
    for consumer, host in zip(cycle, cycle[1:]):
        keys = sorted(k for k in requires.get(consumer) or {}
                      if k not in (provides.get(consumer) or {})
                      and k not in remote_keys and owner.get(k) == host)
        for key in keys[:4]:
            hops.append(f"  {consumer} proxies `{key}` from {host}  "
                        f"(required by {_requirers(consumer, key)}, "
                        f"provided by {_providers(host, key)})")
        if len(keys) > 4:
            hops.append(f"  ... and {len(keys) - 4} further key(s) on the same hop")

    where = f" in {placement_path}" if placement_path else ""
    return (
        "process cycle: " + " -> ".join(cycle) + "\n"
        + "\n".join(hops) + "\n"
        "  a process wires every proxy before it serves, so none of these ever\n"
        "  listens and each dies on its connect timeout.\n"
        f"  this is a property of the PARTITION{where}, not of the components:\n"
        "  the component graph is acyclic (G3 passed) and a quotient of a DAG\n"
        "  can still have a cycle. `[remotes]` keys are excluded — a remote\n"
        "  provider is a separate composition on its own placement (item 151).\n"
        "  fix: co-locate one of the two chains in a single process, or split\n"
        "  the partition along the component DAG instead of across it.")


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


# The hang BACKSTOP, in seconds: how long the conductor keeps waiting after a
# child has been asked to stop and has still not said `DOWN`. It is not a
# teardown budget -- the wait below is on the child's own DOWN line -- so it is
# only ever reached by a child that is genuinely wedged. Operators with a
# legitimately long unwind raise it with `REVL_TEARDOWN_GRACE=<seconds>`.
_TEARDOWN_GRACE = 30.0
# Once a child HAS said DOWN its unwind is complete and proven; all that is
# outstanding is its own exit (a flush, a socket close), which is bounded.
_TEARDOWN_EXIT_GRACE = 5.0
# How long to let the conductor's reader thread catch up after a child has
# exited, before concluding that no `DOWN` line is coming (issue 265). The
# child prints `DOWN` and exits microseconds later, but `is_down` is fed by a
# SEPARATE pump thread, so `proc.poll()` can go non-None while that line is
# still sitting unread in the pipe. Without this wait, widening the stranded
# check to "exited without DOWN" would accuse a cleanly-unwound child. It is
# paid only when `DOWN` has not been seen yet, and it is draining a pipe that
# is already at EOF, so it is generous rather than tuned.
_TEARDOWN_DOWN_READ_GRACE = 2.0


def _teardown_grace() -> float:
    raw = os.environ.get("REVL_TEARDOWN_GRACE")
    if raw:
        try:
            value = float(raw)
        except ValueError:
            return _TEARDOWN_GRACE
        if value > 0:
            return value
    return _TEARDOWN_GRACE


def _stop_all(children: dict, is_down=None, grace: float | None = None) -> list[str]:
    """Stop every child and wait for it to finish unwinding.

    children: name -> (proc, stop_mode). rust holds on stdin (close it to
    stop gracefully); py/node/java tear down on SIGTERM.

    THE WAIT IS ON THE CHILD'S OWN `DOWN` LINE (issue 239), not on a wall
    clock. `[<name>] DOWN` is the runner's own statement that its LIFO unwind
    ran over every registered entry (G7) and its no-residue proof printed (R4),
    and every tier's runner prints it (py, ts, rust, go, java). A wall-clock
    budget is a proxy for that event, and a bad one: it scales with the machine
    rather than with the teardown, which is how a consumer with a map inverse
    and a residue proof to run lost a five-second race on a slow CI runner
    while the provider next to it, with strictly less to do, finished.

    `grace` survives as a HANG BACKSTOP so a wedged child can never hang the
    conductor forever -- the kill exists for a reason. Because it is no longer
    racing an ordinary teardown it is generous, and, the half that matters,
    tripping it is REPORTED.

    WHAT IS REPORTED IS "IT EXITED WITHOUT EVER SAYING DOWN" (issue 265), not
    "we had to SIGKILL it". Which signal ended a child says nothing about
    whether its unwind completed; the `DOWN` line is the only thing that does.
    A child that dies on the SIGTERM above -- the runner losing the window
    between `UP` and its own signal handler, a hard crash inside the unwind, an
    external `kill` -- already has `proc.poll() is not None` by the time this
    looks, and the old check let it out through the same `continue` as a child
    that walked every entry and printed its residue proof. That is exactly the
    silence issue 239 exists to break, arriving through the one door 246 left
    open: G7 (LIFO teardown completeness) and R4 (no residue) both violated,
    neither reported, conductor rc 0.

    So: this returns the name of every child that exited before saying DOWN,
    however it died. Such a child is `halted` in item 443's sense -- its entries
    are stranded (registered, not run, not dropped) and its residue is UNKNOWN
    -- and the caller must say so. Neither a kill nor a silent death is a clean
    exit.
    """
    if grace is None:
        grace = _teardown_grace()

    def said_down(name: str) -> bool:
        return is_down is not None and bool(is_down(name))

    def settled_down(name: str) -> bool:
        """`said_down`, but only after the reader has had its chance.

        Called once a child has EXITED, where a bare `said_down` would race the
        conductor's pump thread: the runner prints `DOWN` and returns from
        `main` microseconds later, so the process can be reaped before the line
        it just wrote has been read. Returns immediately in the common case
        (the line is already in) and waits a bounded, generous moment
        otherwise -- the pipe is at EOF, so the reader is not waiting on the
        child for anything. NOT used on the kill path below, which has already
        spent the whole of `grace` polling `said_down` and needs no further
        benefit of the doubt.
        """
        if said_down(name):
            return True
        if is_down is None:
            return False
        limit = time.monotonic() + _TEARDOWN_DOWN_READ_GRACE
        while time.monotonic() < limit:
            time.sleep(0.02)
            if said_down(name):
                return True
        return False

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
    # ONE shared deadline for the group: the children were asked to stop
    # concurrently and unwind concurrently, so n children must not buy n * grace
    # of conductor hang.
    deadline = time.monotonic() + grace
    stranded: list[str] = []
    for name, (proc, _stop_mode) in children.items():
        exit_by: float | None = None
        while proc.poll() is None:
            now = time.monotonic()
            if exit_by is None and said_down(name):
                exit_by = now + _TEARDOWN_EXIT_GRACE
            if now >= (deadline if exit_by is None else exit_by):
                break
            time.sleep(0.02)
        if proc.poll() is not None:
            # It is gone without our having to kill it -- which says nothing
            # about whether it UNWOUND (issue 265). Only `DOWN` says that.
            if not settled_down(name):
                stranded.append(name)
            continue
        proc.kill()
        try:
            proc.wait(timeout=_TEARDOWN_EXIT_GRACE)
        except subprocess.TimeoutExpired:
            pass
        if not said_down(name):
            # Killed before it could say DOWN: what it had already unwound, and
            # what it still owes, are both unknown from here.
            stranded.append(name)
    return stranded


def _stranded_teardown_report(names: list[str]) -> str:
    """What the conductor says about a child that exited mid-teardown.

    Deliberately the E-Stop verdict's vocabulary (`_render_estop` in
    `cli/change.py`), because it is the same epistemic position: the unwind
    stopped part-way, so entries are STRANDED and residue is UNKNOWN. Nothing
    here is a new word for an old state.
    """
    listed = ", ".join(names)
    plural = "es" if len(names) > 1 else ""
    return (
        f"error: teardown HALTED -- {len(names)} process{plural} exited "
        f"before saying DOWN: {listed}\n"
        "  The unwind was cut mid-flight. The LIFO walk did not reach every\n"
        "  registered entry (G7) and no no-residue proof printed (R4), so every\n"
        "  entry those processes still held is STRANDED -- registered, not run,\n"
        "  not dropped -- and their residue is UNKNOWN. This run did NOT tear\n"
        "  down cleanly, whatever the trace above got as far as printing.\n"
        "  Reconcile a durable run with `revl recover --wal <file>`. If the\n"
        "  teardown is legitimately slow rather than wedged, give it room with\n"
        f"  REVL_TEARDOWN_GRACE=<seconds> (currently {_teardown_grace():g})."
    )


# --- the operator E-Stop, conductor half (item 443, docs/design/443-estop.md)
#
# Every stop the CONDUCTOR had was cooperative: `_stop_all` asks each child to
# unwind and waits on its own `DOWN` line, which is the child's statement that
# its LIFO walk covered every registered entry (G7) and its no-residue proof
# printed (R4). That is right for a composition fault and wrong for an operator
# emergency, where two hundred brackets are two hundred more chances for the
# runaway to cross the boundary again.
#
# The halt below is the other verdict. It runs NO inverse, waits for NO `DOWN`,
# and earns no residue proof. What it buys with that is bounded latency; what
# it owes in exchange is the accounting, which is why `_estop_halt_report` names
# every component individually rather than printing one line about the group.
_ESTOP_HALT_WINDOW = 2.0


def _estop_halt_window() -> float:
    """How long a LATCH-HONORING child gets to name its inventory before the
    conductor kills it anyway.

    This is not a teardown grace and must never grow into one. By the time it
    starts, the child's own crossing seams are already refusing (that is what
    honoring the latch means), so the window buys the INVENTORY — the list of
    stranded entries and the at-most-one ambiguous crossing — and nothing else.
    A child that misses it is killed and reported as residue UNKNOWN, which is
    strictly better than a halt that waits."""
    raw = os.environ.get("REVL_ESTOP_HALT_WINDOW")
    if raw:
        try:
            value = float(raw)
        except ValueError:
            return _ESTOP_HALT_WINDOW
        if value >= 0:
            return value
    return _ESTOP_HALT_WINDOW


def _halt_all(children: dict, backends: dict, has_inventory,
              window: float | None = None) -> dict:
    """Halt every child NOW. Returns pname -> disposition tag.

    Two populations, and the split is the honest part:

      * a child on a tier with NO E-Stop seam (`node`, `rust`, `go`, `java`,
        `wasm`) is SIGKILLed immediately, because a kill is the only halt that
        exists for it. It may have dispatched a crossing microseconds before
        it died and nothing recorded that, so its residue is UNKNOWN;
      * a child on a latch-honoring tier is already refusing new crossings at
        its own seams by the time we get here, so it is given a BOUNDED window
        to print its in-flight inventory, then killed regardless.

    No child is asked to unwind and none is waited on for `DOWN`. A `DOWN`
    line would be a teardown, and a teardown is the thing this verb exists not
    to do."""
    if window is None:
        window = _estop_halt_window()
    disposition: dict[str, str] = {}
    honoring: list[str] = []
    for name, (proc, _stop_mode) in children.items():
        if _canonical_backend(backends.get(name, "py")) in TIERS_WITH_ESTOP:
            # Including one that has ALREADY exited: a child that read the
            # latch itself halts and dies on its own, and its inventory may
            # still be in the pipe. `poll()` is not evidence of anything here.
            honoring.append(name)
            continue
        if proc.poll() is not None:
            disposition[name] = "exited"
            continue
        proc.kill()
        disposition[name] = "killed-no-seam"
    deadline = time.monotonic() + window
    while honoring and time.monotonic() < deadline:
        if all(has_inventory(n) for n in honoring):
            break
        time.sleep(0.02)
    for name in honoring:
        proc = children[name][0]
        still_running = proc.poll() is None
        if still_running:
            proc.kill()
        if not has_inventory(name):
            disposition[name] = "killed-silent"
        else:
            # A child that read the latch itself halts and then dies where it
            # stands, with no teardown; one that named its inventory but is
            # still up gets the kill. The report says which, because "it
            # stopped itself" and "we had to shoot it" are different facts.
            disposition[name] = ("halted-then-killed" if still_running
                                 else "halted-self-exit")
    for proc, _stop_mode in children.values():
        try:
            proc.wait(timeout=_TEARDOWN_EXIT_GRACE)
        except (subprocess.TimeoutExpired, OSError):
            pass
    return disposition


def _estop_halt_report(record: dict, latch: str, processes: dict,
                       backends: dict, disposition: dict,
                       inventories: dict) -> str:
    """What the operator is owed after hitting the button.

    A halt that leaves silent residue is worse than no halt, so this names
    every component left un-torn-down and every outstanding obligation,
    including the ones it CANNOT name — a tier with no E-Stop seam reports
    `UNKNOWN` here rather than being quietly omitted."""
    lines = ["", "E-STOP ENGAGED — the placement is HALTED, not torn down",
             f"  latch     {latch}",
             f"  reason    {record.get('reason') or 'operator halt'}",
             f"  operator  {record.get('operator') or 'unknown'}",
             "",
             "  Nothing was unwound. No inverse ran, no compensation ran,",
             "  nothing was discharged, and no process earned a `DOWN` line.",
             "  G7's LIFO completeness is VACUOUS under the `halted` verdict",
             "  (nothing replays) and R4's no-residue proof does NOT hold. The",
             "  counterpart claim is the inverse one: all residue, all of it",
             "  reported here.", ""]

    rows: list[tuple[str, str, str, str]] = []
    for pname in processes:
        tier = _canonical_backend(backends.get(pname, "py"))
        tag = disposition.get(pname, "killed-silent")
        inv = inventories.get(pname) or {}
        if tag == "exited":
            note = "already exited before the halt — it is not this halt's residue"
        elif tag in ("halted-self-exit", "halted-then-killed"):
            stranded = len(inv.get("stranded") or [])
            ambiguous = len(inv.get("inFlight") or [])
            ending = ("and died where it stood" if tag == "halted-self-exit"
                      else "and was then killed")
            note = (f"HALTED at its own crossing seams (no new crossing "
                    f"dispatched) {ending}; {stranded} entr"
                    f"{'y' if stranded == 1 else 'ies'} STRANDED, "
                    f"{ambiguous} crossing{'' if ambiguous == 1 else 's'} AMBIGUOUS")
        elif tag == "killed-no-seam":
            note = (f"SIGKILLed at once: the {tier} tier has NO E-Stop seam, so "
                    f"it kept dispatching crossings until it died — residue UNKNOWN")
        else:
            note = (f"SIGKILLed after {_estop_halt_window():g}s without naming an "
                    f"inventory — residue UNKNOWN")
        for cname in (processes[pname].get("components") or []) or ["(no components)"]:
            rows.append((cname, pname, tier, note))
    lines.append(f"  components left UN-TORN-DOWN ({len(rows)}):")
    width = max((len(r[0]) for r in rows), default=1)
    for cname, pname, tier, note in rows:
        lines.append(f"    {cname:<{width}}  process {pname}  tier {tier}")
        lines.append(f"    {'':<{width}}  {note}")

    residue: list[str] = []
    unknown: list[str] = []
    for pname in processes:
        tier = _canonical_backend(backends.get(pname, "py"))
        tag = disposition.get(pname, "killed-silent")
        if tag == "exited":
            continue
        if tag in ("halted-self-exit", "halted-then-killed"):
            inv = inventories.get(pname) or {}
            for entry in list(inv.get("inFlight") or []) + list(inv.get("stranded") or []):
                residue.append(
                    f"    {pname}/{entry.get('component') or '?'}  "
                    f"{entry.get('kind')}  {entry.get('method') or '-'}  "
                    f"outcome {entry.get('outcome')}"
                    + (f"  [seq {entry['seq']}]" if entry.get("seq") is not None else ""))
            if not (inv.get("inFlight") or inv.get("stranded")):
                residue.append(f"    {pname}  (nothing was registered — no residue)")
        else:
            unknown.append(
                f"    {pname}  UNKNOWN  the {tier} tier named no inventory; "
                f"whatever it held is still held and still owed")
    lines.append("")
    lines.append(f"  outstanding residue ({len(residue) + len(unknown)} lines, "
                 f"{len(unknown)} of them UNKNOWN):")
    lines.extend(residue + unknown)
    if not (residue or unknown):
        lines.append("    (none)")
    lines.extend([
        "",
        "  Every handle those processes held — descriptors, pool connections,",
        "  leases — went away with the process, not with an inverse: nothing",
        "  that had an external effect was undone. That is the trade the",
        "  button makes (docs/design/443-estop.md).",
        "",
        "  The instance is DEAD; there is no resume (item 443, open question 3).",
        "  Reconcile with: revl recover --wal <file>",
        "  Read the durable inventory with: revl estop --report --wal <file>",
    ])
    return "\n".join(lines)


def run_placement(files, placement_path: str, once: bool = False,
                  estop_latch: str | None = None) -> int:
    # item 443: the operator E-Stop. `--estop-latch FILE` (or the ambient
    # REVL_ESTOP_LATCH) arms it; UNARMED is the default, and a placement that
    # never arms one runs byte-identically to the pre-443 conductor — no
    # watcher thread, no latch read, no change to any teardown path.
    estop_latch = latch_path(estop_latch)
    try:
        ir = compile_files(files)
    except RevlError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    placement = _load_placement(placement_path)
    # item 363: a `[tiers]` / `default_tier` manifest declares a backend tier
    # per component and is expanded here into the equivalent synthesized
    # `[processes]` topology (one process per distinct tier). A classic
    # `[processes]` manifest is returned unchanged, so everything below runs
    # identically and is byte-identical for existing placements.
    placement, tier_err = expand_tiers(placement, [c["name"] for c in ir.get("components") or []])
    if tier_err:
        print(f"error: {tier_err}", file=sys.stderr)
        return 1
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
    # `async fn` operations must forward across a seam as awaitables, so a
    # chained async provide (`async fn hit(k) = cache.get(k)`) that emits
    # `await cache.get(k)` resolves the cross-seam value instead of awaiting a
    # bare str (item 331). Read the async subset off the service declaration.
    async_methods = {
        name: [m for m, spec in (svc.get("methods") or {}).items() if spec.get("async")]
        for name, svc in (ir.get("services") or {}).items()
    }
    key_service: dict[str, str] = {}
    for comp in components.values():
        key_service.update(comp.get("provides") or {})
        key_service.update(comp.get("requires") or {})
    # key -> the component (in THIS composition) that provides it. Seam 2 (item
    # 337) stamps this onto every same-composition proxy entry below, so the
    # consuming process can re-admit its provider against its own manifest at
    # boot without any new transport.
    key_component: dict[str, str] = {}
    for cname, comp in components.items():
        for key in (comp.get("provides") or {}):
            key_component[key] = cname
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

    # --- capability/realm-aware host placement (item 119): a host may declare
    # the capabilities it offers and the realm it is pinned to; a component is
    # refused on a host that lacks a capability it needs or that is pinned to a
    # foreign realm. Both come from the placement toml + the realms already in
    # the IR — no `.rvl` source change. Purely additive: a placement that
    # declares neither validates trivially and is byte-identical (below is a
    # no-op then). docs/capability-realm-placement.md.
    required_caps = _required_capabilities(placement)
    unknown_caps = sorted(c for c in required_caps if c not in components)
    if unknown_caps:
        return abort(f"[capabilities] names unknown component(s): {', '.join(unknown_caps)} "
                     f"(known: {', '.join(sorted(components))})")
    cap_realm_problem = capability_realm_diagnostic(processes, ir, required_caps)
    if cap_realm_problem:
        return abort(cap_realm_problem)

    # --- sandbox placement (item 411, Slice 1): a process may declare an
    # isolation boundary + fs/net envelope, either as `[processes.<p>.sandbox]`
    # (validated here) or via the `[tiers]`-form `[sandbox]` sugar (already
    # split into `sandbox_<component>` processes by expand_tiers, carrying the
    # raw table; normalized here uniformly). The `[sandbox.needs]` table is
    # form-independent. Purely additive: a placement with no sandbox surface
    # builds `sandboxes = {}` and every 411 step below is a no-op.
    sandboxes: dict[str, dict] = {}
    for pname, pconf in processes.items():
        sb_raw = pconf.get("sandbox")
        if sb_raw is None:
            continue
        normalized, sb_err = _normalize_sandbox_table(sb_raw)
        if sb_err:
            return abort(f"process {pname!r} [sandbox]: {sb_err}")
        sandboxes[pname] = normalized
    sandbox_needs = (placement.get("sandbox") or {}).get("needs") or {}

    if placement.get("report_colocation"):
        for advice in colocation_advice(processes, placed, ir):
            print(f"  co-location: {advice}", flush=True)

    # --- network placement (item 56): a seam whose provider process declares an
    # `address` crosses TCP + mutual TLS instead of a local UDS. Identity per
    # process comes from the operator model (item 55). Every other seam stays a
    # local UDS (default, no cert; full back-compat). docs/network-placement.md.
    addresses: dict[str, tuple[str, int, float | None]] = {}
    identities: dict[str, str] = {}
    explicit_tls: dict[str, dict] = {}
    # item 118 §1.4b: the identities a NETWORK provider declares may call it.
    # `None` means "not declared", which is a different thing from `[]`
    # ("only this placement's own consumers") — see the validation below.
    declared_peers: dict[str, list[str]] = {}
    for pname, pconf in processes.items():
        addr = pconf.get("address")
        if addr:
            if addr.get("host") is None or addr.get("port") is None:
                return abort(f"process {pname!r} `address` needs both host and port")
            addresses[pname] = (str(addr["host"]), int(addr["port"]), addr.get("rtt_ms"))
        praw = pconf.get("peers")
        if praw is not None:
            if not isinstance(praw, list) or any(not isinstance(x, str) for x in praw):
                return abort(f"process {pname!r} `peers` must be a list of identity "
                             "strings (the mTLS identities allowed to call this "
                             "network seam)")
            declared_peers[pname] = [str(x) for x in praw]
        tconf = pconf.get("tls") or {}
        if tconf.get("identity"):
            identities[pname] = str(tconf["identity"])
        if tconf.get("cert"):
            missing = [k for k in ("key", "ca", "identity") if not tconf.get(k)]
            if missing:
                return abort(f"process {pname!r} [tls] gives `cert` but not {', '.join(missing)}")
            explicit_tls[pname] = {"cert": tconf["cert"], "key": tconf["key"],
                                   "ca": tconf["ca"], "identity": tconf["identity"]}

    # --- effect-correlation guard (item 118 S1 / roadmap 421 F8): every local
    # UDS seam gets a `revl.deploy.CorrelationGuard` the way bridge.py's own
    # `peer_identity()` docstring says it should, namely "a correlation guard
    # authenticates the peer by its own per-process secret alone". Each process
    # gets a peer identity (its declared [tls] identity when it has one, the
    # same item-55 identity a network process presents, else its own process
    # name, unique within this placement) and a fresh per-process secret; the
    # conductor is the only party that ever holds every secret at once, same as
    # it is for TLS key material. Scoped to LOCAL sockets only: a network
    # provider (`[processes.*].address`) may also be reachable by an item-151
    # remote consumer this placement never enumerates, so wiring a guard there
    # from this table alone could refuse a legitimate caller it knows nothing
    # about; that stays a follow-on, not a regression this fix can introduce.
    correlation_identity = {pname: identities.get(pname, pname) for pname in processes}
    correlation_secret = {pname: secrets.token_bytes(32) for pname in processes}
    composition_id = canonical_hash(ir)

    def _network_correlation(pname: str) -> dict:
        """item 118 §1.4b: what a NETWORK consumer stamps on every crossing —
        its own item-55 identity (the same one its certificate carries) and the
        composition it is scoped to, and NO SECRET.

        There is nothing to distribute, which is the whole point: the receiving
        `TransportReplayGuard` scopes dedup by the identity the mTLS handshake
        proved and only reads `composition_id`/`generation`/`idempotency_key`
        off this envelope. A consumer in another composition, under another
        conductor, stamps the same shape from its own certificate and is
        deduplicated exactly the same way — no key it cannot hold, so no
        legitimate caller refused."""
        return {"composition_id": composition_id,
                "peer_identity": correlation_identity[pname]}
    # provider -> the local (UDS) consumers of its keys, mirroring exactly the
    # branch below that assigns `entry["socket"]` (never a network or remote
    # entry), so the two stay in lockstep by construction.
    uds_consumers: dict[str, set[str]] = {}
    for _cname in processes:
        for _key in requires.get(_cname, {}):
            if _key in provides.get(_cname, {}):
                continue
            _host = owner.get(_key)
            if _host is None or _host in addresses:
                continue
            uds_consumers.setdefault(_host, set()).add(_cname)

    # --- two-composition topology (item 151): a `[remotes.<key>]` names a seam
    # whose provider lives in a *separate* composition, on its own placement —
    # item 56's stated non-goal made reachable ("the provider runs its own
    # placement on its own machine"; docs/network-placement.md "Non-goals"). This
    # composition declares only `service <service>` (the interface) and reaches
    # the provider **by address alone**: it never names the provider component,
    # never shares its IR, and — the point of the decoupling — its own IR never
    # carries the provider's `@py` externs (e.g. the `compile_files` gate). So a
    # remote seam is a network seam with no local owner, wired straight to the
    # declared machine over the same TCP+mTLS transport (docs/network-path.md).
    services = ir.get("services") or {}
    remote_specs: dict[str, dict] = {}
    for key, rconf in (placement.get("remotes") or {}).items():
        rhost, rport, rservice = rconf.get("host"), rconf.get("port"), rconf.get("service")
        if rhost is None or rport is None:
            return abort(f"remote {key!r} needs both host and port")
        if not rservice:
            return abort(f"remote {key!r} needs a `service` (the interface it reaches)")
        if rservice not in services:
            return abort(f"remote {key!r} names service {rservice!r}, which this "
                         "composition does not declare — a consumer reaches a "
                         "remote only through a `service` it holds the interface for")
        if key in owner:
            return abort(f"remote {key!r} is also provided by local process "
                         f"{owner[key]!r}; a key is either local or remote, not both")
        if not any(key in requires[p] and key not in provides[p] for p in processes):
            return abort(f"remote {key!r} is required by no process in this placement")
        # SNI for the mTLS handshake: it must match a SAN on the remote's leaf.
        # Defaults to the host, but a loopback IP is not a legal TLS servername
        # (node refuses `servername: 127.0.0.1`), so a remote on an IP host names
        # the DNS SAN its cert carries (`server_hostname = "localhost"`).
        remote_specs[key] = {"service": rservice, "host": str(rhost),
                             "port": int(rport), "rtt_ms": rconf.get("rtt_ms"),
                             "server_hostname": str(rconf.get("server_hostname", rhost))}

    # --- the process graph must be acyclic (roadmap item 171,
    # docs/design/438-petri-reachability.md §5.2/§8.1). G3 proved the COMPONENT
    # graph acyclic; the partition above quotients that graph, and a quotient of
    # a DAG can have a cycle. `_process_runner.run` wires every proxy before it
    # serves, so a cycle of processes is a cycle of boot-time waits and nothing
    # ever listens. Checked here, once `owner` and `remote_specs` are both known
    # (a `[remotes]` key is another composition and is excluded), and before any
    # TLS material is minted or any child is spawned — a `revl plan`-time
    # refusal rather than five seconds into a boot.
    problem = process_cycle_refusal(requires, provides, owner, placed, components,
                                    remote_keys=set(remote_specs),
                                    placement_path=placement_path)
    if problem:
        return abort(problem)

    # which processes take part in a network seam (as provider or consumer)?
    network_processes: set[str] = set(addresses)  # a provider serves remotely
    # network provider -> the consumers of its keys that live in THIS placement.
    # These are the only callers a network provider can enumerate: an item-151
    # cross-composition consumer holds only an address and never appears here,
    # which is exactly why the peer set has to be DECLARED and cannot be derived.
    net_consumers: dict[str, set[str]] = {}
    for pname in processes:
        for key in requires[pname]:
            if key in provides[pname]:
                continue
            if owner.get(key) in addresses:
                network_processes.add(pname)          # this consumer crosses TCP
                network_processes.add(owner[key])     # to that provider
                net_consumers.setdefault(owner[key], set()).add(pname)
            elif key in remote_specs:
                network_processes.add(pname)          # this consumer dials a remote

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

    # --- item 118 §1.4b: the peer plane a TCP+mTLS seam CAN carry.
    #
    # `CorrelationGuard` (the UDS seam's guard) authenticates a caller by a
    # per-boot secret this conductor minted and handed to its own children. A
    # network provider may also be dialled by an item-151 cross-composition
    # consumer running under a DIFFERENT conductor, which can never hold that
    # secret, so demanding a sealed envelope over the network refuses the
    # legitimate caller rather than the stranger. What mTLS does prove — with a
    # CA-signed key, per session — is WHO is calling; what was missing is a
    # closed set to check that against, because `CERT_REQUIRED` against a shared
    # CA answers every identity that CA ever signed. `peers` is that set.
    #
    # It is DECLARED, never derived: this placement cannot enumerate the
    # consumers that live in other compositions, and a set derived from the ones
    # it can see would lock them out. The declared list is unioned with this
    # placement's own network consumers of the provider so an operator naming an
    # external peer does not have to restate the local ones (and cannot lock
    # them out by forgetting). An explicit empty list is therefore meaningful:
    # "only this composition's own consumers".
    peer_allowlist: dict[str, tuple[str, ...]] = {}
    for pname in sorted(declared_peers):
        if pname not in addresses:
            return abort(
                f"process {pname!r} declares `peers` but no `address` — a peer "
                "allowlist is the admission check on a network (TCP+mTLS) seam's "
                "mTLS peer identity; a local UDS seam is admitted by the item-118 "
                "correlation guard instead, whose peer set is derived, not declared")
        if allowed_identities is not None:
            for ident in declared_peers[pname]:
                if ident not in allowed_identities:
                    return abort(
                        f"process {pname!r} allows peer identity {ident!r}, which is "
                        f"not a declared operator in {profile_path!r} (identity per "
                        "process is issued by the operator model, item 55)")
    for pname in sorted(addresses):
        if pname not in declared_peers:
            continue
        admissible = set(declared_peers[pname])
        admissible |= {identities[q] for q in net_consumers.get(pname, set())}
        if not admissible:
            return abort(
                f"process {pname!r} declares `peers = []` and no process in this "
                "placement consumes its keys over the network, so no caller could "
                "ever be admitted — name the identities that may call it, or drop "
                "`peers` and accept the reported UNVERIFIED admission level")
        peer_allowlist[pname] = tuple(sorted(admissible))

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

    def _sni(host: str, tls: dict) -> str:
        # SNI (TLS servername) must be a DNS name: node — and RFC 6066 — refuse
        # an IP literal as a servername even when the leaf carries it as an IP
        # SAN, so a loopback address handed through raw fails the handshake
        # (item 152; the same trap the [remotes] path already dodges). An
        # explicit [tls] server_hostname wins; else an IP host names the DNS SAN
        # the minted certs always carry ("localhost"); a real DNS host is used
        # as-is.
        if tls.get("server_hostname"):
            return str(tls["server_hostname"])
        try:
            ipaddress.ip_address(host)
        except ValueError:
            return host
        return "localhost"

    def _serve_endpoint(pname: str) -> dict:
        host, port, _ = addresses[pname]
        return {"host": host, "port": port,
                "tls": {**certs[pname], "server_hostname": _sni(host, certs[pname])}}

    def _proxy_endpoint(consumer: str, host_proc: str) -> tuple[dict, float | None]:
        host, port, rtt = addresses[host_proc]
        return ({"host": host, "port": port,
                 "tls": {**certs[consumer], "server_hostname": _sni(host, certs[consumer])}}, rtt)

    # (consumer, key, host, port, configured_rtt) for the latency report below
    net_seams: list[tuple[str, str, str, int, float | None]] = []
    # item 118 §1.4b: the peer-admission level each serving process ACHIEVED,
    # reported by the conductor below. Built in the spec loop so it can only say
    # what was actually wired into a spec.
    seam_admissions: list[SeamAdmission] = []

    # base specs (backend-neutral)
    specs: dict[str, dict] = {}
    backends: dict[str, str] = {}
    # item 337 Seam 2: a proxy entry's "backend" names its PROVIDER's tier,
    # which may be a process not yet reached by the per-process loop below (it
    # builds `backends` incrementally, in `processes` insertion order, while it
    # ALSO builds that same process's proxies against every OTHER process's
    # backend). Populate `backends` for every process up front so a proxy
    # entry can always resolve its provider's tier regardless of declaration
    # order; the per-process loop's own assignment below is then just a
    # harmless re-affirmation (and still owns the backend validation/abort).
    for _pname, _pconf in processes.items():
        _backend = _canonical_backend(_pconf.get("backend", "py"))
        if _backend in KNOWN_BACKENDS:
            backends[_pname] = _backend
    for pname, pconf in processes.items():
        backend = _canonical_backend(pconf.get("backend", "py"))
        if backend == "wasm" or pconf.get("backend") == "wasm":
            return abort(f"process {pname!r} is on the `wasm` tier, which is not a "
                         "placement tier: no wasm placement runner, bridge client, "
                         "or stub exists on the substrate (a wasm placement runner "
                         "is a separately designed follow-on). Place native "
                         "components on `rust` or `go`.")
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
            if host is None and key not in remote_specs:
                return abort(f"key {key!r} required by {pname!r} is provided by no process")
            entry = {"methods": methods.get(service, []), "service": service,
                     "async_methods": async_methods.get(service, []),
                     "deadline": p_deadline}
            if key in remote_specs:
                # a remote seam (item 151): the provider is a *separate*
                # composition on its own placement, reached by address alone.
                # The consumer presents its own mTLS identity/CA (certs[pname])
                # and verifies the remote against the same CA — the shared trust
                # root two independent placements agree on out of band; there is
                # no local process to own the key. Deliberately NO
                # `component`/`backend` here: this key is not a component of
                # THIS composition's `running_ir`, so it is not Seam-2 eligible
                # (design doc 337, "Seam 3: the remote handoff" — a different,
                # deferred seam with its own trust-anchor requirement).
                rs = remote_specs[key]
                entry["endpoint"] = {"host": rs["host"], "port": rs["port"],
                                     "tls": {**certs[pname],
                                             "server_hostname": rs["server_hostname"]}}
                entry["latency_ms"] = rs["rtt_ms"]
                entry["remote"] = True
                entry["correlation"] = _network_correlation(pname)
                net_seams.append((pname, key, rs["host"], rs["port"], rs["rtt_ms"]))
            elif host in addresses:
                # a network seam: point the proxy at the machine over TCP+mTLS,
                # and record its latency class (the configured RTT; the conductor
                # also measures a real number once the provider is up, below).
                endpoint, rtt = _proxy_endpoint(pname, host)
                entry["endpoint"] = endpoint
                entry["latency_ms"] = rtt
                ehost, eport, _ = addresses[host]
                net_seams.append((pname, key, ehost, eport, rtt))
                # item 337 Seam 2: this is still the SAME composition, just
                # network-distributed, so the consumer holds its provider's
                # source (`spec["files"]`) and can re-admit it at boot.
                entry["component"] = key_component.get(key)
                entry["backend"] = backends.get(host)
                entry["correlation"] = _network_correlation(pname)
            else:
                entry["socket"] = sockets[host]
                # item 337 Seam 2: the provider's admissible identity, so the
                # consuming process can re-admit it against its own running
                # manifest before wiring the proxy (`_process_runner.py`,
                # `_boot_wiring_decision`) — the landed repoint seam
                # (`swap_admission`) moved one event earlier, no new transport.
                entry["component"] = key_component.get(key)
                entry["backend"] = backends.get(host)
                # item 118 S1 / roadmap 421 F8: what THIS consumer needs to seal
                # a correlation envelope on every call, namely its own identity
                # and secret, plus the composition it is scoped to. The
                # provider's matching secret table is built below from
                # `uds_consumers`.
                entry["correlation"] = {
                    "composition_id": composition_id,
                    "peer_identity": correlation_identity[pname],
                    "secret": correlation_secret[pname].hex(),
                }
            if p_deadlines:
                entry["deadlines"] = dict(p_deadlines)
            proxies[key] = entry
        serve_keys = [k for k in provides[pname] if any(k in requires[q] and q != pname for q in processes)]
        if pname in addresses:
            # a network provider serves its full provided surface — remote
            # consumers live in other placements and are not enumerable here.
            serve_keys = list(provides[pname])
        own = [c for c in load_order if placed.get(c) == pname]
        # F4: a process gets only the config of the components IT hosts, and only
        # the `@ts ref` hash-checks for the externs ITS slice reaches — never the
        # whole [config] table (a `[config.ControlPlane] db_url = "...secret..."`
        # must not be delivered to a tier that hosts no reader of it) nor every
        # extern's refs (a node process must not hash-check refs for externs it
        # does not host). A process hosting every component gets the full slice
        # back, so a single-process placement is byte-identical.
        own_config = {c: config[c] for c in own if c in config}
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
            # (correctly) absent here. docs/parallel-activation.md. That
            # resolution is what makes the PROCESS graph a boot-order wait
            # relation of its own, which is why `process_cycle_refusal` above
            # has already proved it acyclic (item 171).
            "depends": local_prereqs(manifest_entries, subset=own),
            "config": own_config,
            "provides": list(provides[pname]),
            "proxies": proxies,
            "probe": pconf.get("probe") or [],
            # item 396 option B / 410: the two host-ref roots the node runner
            # joins a `@ts ref` against, plus the per-ref hash-check list. 396(B)
            # set NEITHER under placement (a pre-existing gap for user refs); 410
            # fixes it for the stdlib kind (self-derived by the runner too) and in
            # passing for the user kind. Built by `host_ref_pins`, which the swap
            # path calls too so the pins survive a re-host (see `do_swap`).
            **host_ref_pins(ir, own, files),
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
                # item 118 §1.4b: the closed peer set, when the placement
                # declared one. Names only — no secret crosses a composition
                # boundary, which is what lets this hold where the correlation
                # guard cannot.
                # item 118 §1.4b: freshness, keyed on the identity the mTLS
                # handshake proved. Installed on EVERY network seam, with or
                # without an allowlist, because it holds no secret and so
                # refuses no legitimate caller: a crossing that carries no
                # idempotency key is dispatched exactly as before, and a
                # cross-composition consumer under another conductor stamps the
                # same unsealed envelope from its own certificate.
                serve_spec["replay"] = {"per_peer": REPLAY_WINDOW_PER_PEER,
                                        "max_peers": REPLAY_WINDOW_PEERS}
                window = (f"replays refused per proven identity over a bounded "
                          f"window ({REPLAY_WINDOW_PER_PEER} keyed crossings x "
                          f"{REPLAY_WINDOW_PEERS} peers); a crossing declaring no "
                          "idempotency key is not deduplicated")
                allow = peer_allowlist.get(pname)
                if allow:
                    serve_spec["peers"] = list(allow)
                    seam_admissions.append(SeamAdmission(
                        provider=pname, transport="tcp+mtls",
                        level=ADMISSION_PEER_BOUND,
                        detail="mTLS peer identity checked against the declared "
                               f"`peers` allowlist, and {window}. NOT sealed: an "
                               "off-placement peer cannot hold this boot's "
                               "correlation secret, so the envelope is "
                               "authenticated by the transport, not by an HMAC",
                        peers=allow))
                else:
                    seam_admissions.append(SeamAdmission(
                        provider=pname, transport="tcp+mtls",
                        level=ADMISSION_UNVERIFIED,
                        detail="mTLS proves WHICH identity is calling and "
                               f"{window}, but this placement declares no "
                               "`peers`, so every identity the shared CA signed "
                               f"is answered — add [processes.{pname}] "
                               "peers = [...] to close it"))
            else:
                serve_spec["socket"] = sockets[pname]
                # item 118 S1 / roadmap 421 F8: the secret table this provider
                # runs its `CorrelationGuard` on, one entry per LOCAL consumer of
                # these keys (`uds_consumers`, built above in lockstep with the
                # `entry["socket"]` branch). Never built for a network provider,
                # which may also answer an item-151 remote consumer this table
                # cannot enumerate.
                # A guard is installed ONLY when every local consumer of this
                # provider runs on a tier that actually SEALS a correlation.
                # Sealing is implemented in the python bridge alone today; the
                # ts, go and java bridges have no `correlation` parameter at
                # all, so a consumer on one of those tiers sends a pre-118
                # envelope and the guard refuses it as `malformed-envelope`.
                # Installing the guard in front of a caller that cannot satisfy
                # it does not harden the seam, it breaks it: that regressed the
                # py-java placement smoke. Same rule as the network case above,
                # namely never build a guard on an assumption that does not hold
                # for every caller it will judge.
                peers = uds_consumers.get(pname) or set()
                if peers and all(backends.get(q) in _CORRELATION_SEALING_TIERS
                                 for q in peers):
                    serve_spec["correlation"] = {
                        "composition_id": composition_id,
                        "peers": {correlation_identity[q]: correlation_secret[q].hex()
                                 for q in sorted(peers)},
                    }
                    seam_admissions.append(SeamAdmission(
                        provider=pname, transport="uds",
                        level=ADMISSION_SEALED,
                        detail="every caller authenticated by its own per-process "
                               "secret and replay-checked",
                        peers=tuple(sorted(correlation_identity[q] for q in peers))))
                elif peers:
                    unsealing = sorted({str(backends.get(q)) for q in peers}
                                       - set(_CORRELATION_SEALING_TIERS))
                    seam_admissions.append(SeamAdmission(
                        provider=pname, transport="uds",
                        level=ADMISSION_UNVERIFIED,
                        detail="a consumer runs on a tier whose bridge cannot seal "
                               f"a correlation envelope ({', '.join(unsealing)}), so "
                               "no guard is installed and any caller that reaches "
                               "the socket is answered"))
            spec["serve"] = serve_spec
        specs[pname] = spec

    import revl  # noqa: PLC0415
    src_dir = str(Path(revl.__file__).resolve().parents[1])
    # keep the inherited PYTHONPATH, but drop empty entries: an empty entry is
    # the *current directory* on the child's sys.path, which would let a stray
    # module in CWD shadow a real one.
    inherited = [p for p in os.environ.get("PYTHONPATH", "").split(os.pathsep) if p]
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([src_dir, *inherited])}
    if estop_latch:
        # The py child finds the latch here (`runtime.estop_latch_path`), so its
        # crossing seams refuse from the instant an operator arms it — the same
        # rendezvous single-process `revl run --estop-latch` uses. A child on a
        # tier with no E-Stop seam inherits the variable and ignores it, which
        # is exactly why `_halt_all` kills that population instead.
        env[LATCH_ENV] = estop_latch

    # per-backend build steps, done lazily and cached: the initial placement
    # builds every backend it uses, and a later `revl swap ... --to <backend>`
    # reuses those or builds a new target on demand (booting the candidate
    # provider in its own process on the target tier — roadmap §23 step 1).
    cleanup: list[str] = []
    built: dict[str, object] = {}

    def _backend_slice(backend: str, extra=()) -> dict:
        """The IR a tier's emitter sees: the placement slice of every component
        hosted on that backend (item 363). `extra` names a component being
        swapped onto the tier live, so its successor build contains it. A tier
        that hosts every component gets the full IR back byte-identical, so an
        existing (single-tier or all-same-tier) placement builds today's
        artifact unchanged."""
        comps = {c for c, p in placed.items() if backends.get(p) == backend}
        comps |= set(extra)
        return placement_slice(ir, comps)

    def ensure_backend(backend: str, extra=(), rebuild: bool = False) -> None:
        if backend in built and not rebuild:
            return
        sliced = _backend_slice(backend, extra)
        if backend == "node":
            module = _emit_ts_module(sliced, tmp)
            cleanup.append(module)
            built["node"] = module
        elif backend == "rust":
            built["rust"] = _build_rust(sliced, tmp)
        elif backend == "go":
            built["go"] = _build_go(sliced, tmp)
        elif backend == "java":
            java21_bin = _find_jdk21()
            cordis_classes = _find_cordis4j_classes()
            if java21_bin and cordis_classes:
                built["java"] = ("real", _build_java_real(sliced, tmp, java21_bin, cordis_classes),
                                 java21_bin, cordis_classes)
            else:
                built["java"] = ("stub", _build_java(sliced, tmp), None, None)
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

    # --- tier-capability gate (item 363, stage 3): a component placed on a
    # tier that cannot emit it is refused HERE, at plan time, with a diagnostic
    # naming the component + tier + the tier's own reason — never a raw
    # toolchain error after a partial spawn. The emitters are the oracle
    # (dry-run per slice); wasm was already refused at expansion.
    cap_problem = tier_capability_gate(ir, placed, backends)
    if cap_problem:
        return abort(cap_problem)

    # --- cross-process boundary checks (item 363, stage 4; Finding A): refuse a
    # resource-type crossing on EVERY cross-process seam, tier-agnostic (parity
    # with the swap gate; a handle cannot cross a process seam and a witnessed
    # rollback across a seam is out of scope) — including same-tier py<->py
    # seams, which the earlier cross-tier-only check let a handle cross ungated.
    # The sync (address-space-bound) REPORT half stays cross-tier only: a
    # same-tier value-typed seam is still byte-identical to today.
    boundary_problem, boundary_report = cross_tier_boundary_check(
        ir, requires, provides, owner, backends, services)
    if boundary_problem:
        return abort(boundary_problem)
    for advice in boundary_report:
        print(advice, flush=True)

    # --- sandbox placement gate (item 411, Slice 1): the fail-closed
    # unmappable-need refusal + the advisory declared-need-vs-envelope refusal +
    # the cell opaque-residue refusal, all before anything spawns. A no-op when
    # the placement declares no sandbox (`sandboxes == {}`).
    sandbox_problem = sandbox_capability_gate(
        ir, processes, sandboxes, sandbox_needs, requires, provides, owner)
    if sandbox_problem:
        return abort(sandbox_problem)

    try:
        for backend in backends.values():
            ensure_backend(backend)
    except (RevlError, RuntimeError, OSError) as exc:
        return abort(str(exc))

    for pname, spec in specs.items():
        adapt_spec(spec, backends[pname])
        if estop_latch and _canonical_backend(backends[pname]) in TIERS_WITH_ESTOP:
            # In the SPEC as well as the environment: a sandboxed process (item
            # 411) is wrapped by a driver that need not forward the conductor's
            # environment, and an emergency stop that a confined process cannot
            # see is not an emergency stop.
            spec["estopLatch"] = estop_latch

    # --- sandbox runtime driver (item 411, Slice 2): ESTABLISH each declared
    # isolation boundary before anything spawns, or refuse the placement.
    #
    # There is no third outcome here on purpose. A rung with no driver, a
    # missing container runtime, an unresolvable image, a backend with no
    # in-boundary runner form, a seam that cannot cross the boundary, or a boot
    # canary that cannot CONFIRM the confinement from inside all abort. A
    # sandbox that quietly degraded to an ordinary process would leave the rest
    # of the composition trusting a body nothing confines, which is strictly
    # worse than a placement that declared no sandbox at all.
    sandbox_drivers: dict = {}
    sandbox_achieved: dict[str, dict] = {}
    for pname in processes:
        if pname not in sandboxes:
            continue
        env = sandboxes[pname]
        driver = resolve_sandbox_driver(env["isolation"])
        if driver is None:
            return abort(
                f"process {pname!r} declares the {env['isolation']!r} isolation "
                f"rung, which has no runtime driver in this build (item 411 "
                f"Slice 2 implements `container`). The boundary cannot be "
                f"established, and a declared isolation is never downgraded to an "
                f"unconfined process — the placement refuses. Use the `container` "
                f"rung, or take the process out of the sandbox.")
        spec = specs[pname]
        ctx = {
            "backend": backends[pname],
            "seam_dir": str(tmp),
            "seam_keys": sorted(set(spec.get("proxies") or {})
                                | set((spec.get("serve") or {}).get("keys") or [])),
            "files": [str(f) for f in files],
            "cwd": os.getcwd(),
        }
        achieved, sb_err = driver.preflight(pname, env, ctx)
        if sb_err:
            for started in sandbox_drivers.values():
                started.teardown()
            return abort(f"sandbox refused (item 411): {sb_err}")
        achieved["_env"] = env
        sandbox_drivers[pname] = driver
        sandbox_achieved[pname] = achieved

    summary = "  ".join(process_tag(p, processes, backends, sandboxes) for p in processes)
    print(f"placement: {summary}", flush=True)
    if sandboxes:
        # item 411: the envelope + effective reach per sandboxed process (the
        # per-key seam-served provider reach, the opaque residue, the
        # claimed-vouched externs, and the net=none egress note), so `net=none`
        # is never readable as a total-egress claim — plus, since Slice 2, the
        # rung actually ACHIEVED and the in-sandbox canary's evidence for it.
        # Still deferred: the per-rung seam transport, the conductor-served
        # approval channel, and the wasm-cell / microVM rungs (all three of
        # which REFUSE rather than degrade above).
        print("  sandbox placement (item 411, Slice 2): isolation ESTABLISHED by "
              "a runtime driver and verified in-sandbox; a rung that cannot be "
              "established refuses the placement", flush=True)
        reach = _component_reach(ir)
        for line in render_sandbox_summary(
                processes, sandboxes, reach, sandbox_needs,
                requires, provides, owner, backends, sandbox_achieved):
            print(line, flush=True)
    if "java" in built:
        note = ("real cordis4j (reactive)" if built["java"][0] == "real"
                else "stub (non-reactive; set REVL_CORDIS4J_CLASSES + a JDK 21 for reactive withdrawal)")
        print(f"  java runtime: {note}", flush=True)
    if net_seams:
        print(f"  network seams (item 56): {len(net_seams)} over TCP+mTLS", flush=True)
    # item 118 §1.4b: the peer-admission level every cross-process seam actually
    # ACHIEVED — `sealed` where the caller is authenticated by its own secret and
    # replay-checked, `peer-pinned` where mTLS proved the identity and a declared
    # allowlist closed the set, and UNVERIFIED where neither holds. A seam that
    # cannot prove who may call it says so here rather than staying quiet, which
    # is `bundle.verify`'s rule applied to this plane.
    for line in render_seam_admissions(seam_admissions):
        print(line, flush=True)

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
    # issue 239 / 265: children that exited before they said DOWN -- by
    # SIGKILL, by the SIGTERM that asked them to stop, or by any other death.
    # Their unwind was cut mid-flight, so their residue is UNKNOWN; the
    # conductor must never report that as a clean exit. Accumulated across
    # EVERY teardown this run performs (a refused swap successor, a swapped-out
    # provider, the final teardown), because any one of them can be the one
    # that was cut.
    stranded: list[str] = []
    # item 443: pname -> the halt inventory that child printed for itself, and
    # the conductor-side halt state. `halted` is a one-shot latch: pressing the
    # button twice is not two halts.
    inventories: dict[str, dict] = {}
    halted: dict = {}
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
            elif text.startswith(f"[{pname}] {HALTED_LINE} "):
                # item 443: the child's own in-flight inventory, printed when
                # the latch tripped its seams. It is NOT a `DOWN` line and must
                # never be read as one — the child unwound nothing.
                try:
                    inventories[pname] = json.loads(
                        text.split(" ", 2)[2])
                except (ValueError, IndexError):
                    inventories[pname] = {}
            else:
                m = _re_repoint.match(text)
                if m and m.group("p") == pname:
                    repointed.add((pname, m.group("k")))

    def spawn(pname: str, backend: str, spec: dict) -> None:
        spec_file = tmp / f"{pname}.spec.json"
        spec_file.write_text(json.dumps(spec), encoding="utf-8")
        cmd, proc_env, stop_mode = command_for(backend, spec_file)
        if pname in sandbox_drivers:
            # item 411 Slice 2: the SAME runner command, rewritten to run inside
            # the boundary the driver established and the canary confirmed above.
            cmd, proc_env = sandbox_drivers[pname].wrap(
                pname, cmd, proc_env, sandbox_achieved[pname])
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
            if halted:
                # item 443: an operator halt outranks whatever this loop was
                # waiting for. Nothing is coming up after the button.
                return False
            time.sleep(0.05)
        return pred()

    # --- the operator E-Stop (item 443) --------------------------------------
    watch_stop = threading.Event()
    conductor_thread = threading.current_thread()

    def engage_estop(record: dict) -> None:
        """Halt the placement. One-shot: the button is idempotent.

        Order matters and is the whole design. (1) SAY IT, before anything
        that can take time, so the operator sees the halt at the instant it
        lands. (2) Stop every child — killing outright the tiers that have no
        E-Stop seam, and giving the ones that do a bounded window to name
        their inventory. (3) REPORT, naming every component left un-torn-down
        and every outstanding obligation, including the ones no tier could
        name. (4) Unblock the conductor so `revl run` actually returns.

        What it deliberately does NOT do: ask any child to unwind, wait for a
        `DOWN` line, run an inverse, or run `stop_all`. Every one of those is
        the graceful path this verb exists to bypass."""
        if halted:
            return
        halted["record"] = record
        reason = record.get("reason") or "operator halt"
        # The banner rides the interleaved TRACE on stdout, where it belongs in
        # the run's timeline; the verdict below goes to stderr, the same split
        # `_stranded_teardown_report` already uses.
        print(f"\n[conductor] E-STOP {reason} — halting {len(children)} "
              f"process(es) now, NO unwind", flush=True)
        disposition = _halt_all(children, backends, lambda n: n in inventories)
        halted["disposition"] = disposition
        print(_estop_halt_report(record, estop_latch, processes, backends,
                                 disposition, inventories),
              file=sys.stderr, flush=True)
        watch_stop.set()
        if conductor_thread is threading.main_thread():
            # The conductor may be parked in `input()` (the swap REPL) or in a
            # wait loop. An emergency stop that needed the main loop's
            # cooperation to be noticed would not be one, so interrupt it.
            _thread.interrupt_main()

    def estop_watch() -> None:
        while not watch_stop.wait(0.05):
            record = read_latch(estop_latch)
            if record is not None:
                engage_estop(record)
                return

    def stop_all(group: dict) -> None:
        """`_stop_all` with this conductor's DOWN tracker wired in, recording
        any child that exited before it finished unwinding."""
        for name in _stop_all(group, is_down=lambda n: n in down):
            if name not in stranded:
                stranded.append(name)

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
        # item 411: moving a component across an ISOLATION boundary changes the
        # security posture of a running system; an operator decision this item
        # declines to automate in v1. A swap naming a sandboxed component (or
        # targeting its process) is refused with the named gap; lifting it is a
        # follow-on with its own admission story.
        if old in sandboxes:
            env = sandboxes[old]
            print(f"swap refused (item 411): {component!r} runs in sandbox {old!r} "
                  f"({env['isolation']}: {_envelope_str(env)}); moving a component "
                  f"across an isolation boundary changes the running system's "
                  f"security posture and is not automated in v1 (a sandbox swap is "
                  f"a follow-on). Running composition untouched.", flush=True)
            return
        # item 56 / 151: a NETWORK provider's address is part of the placement
        # contract other machines hold, and a swap cannot unilaterally change
        # it. The successor is booted on a fresh local UDS under `tmp` and the
        # re-point loop below reaches only the consumers THIS conductor runs;
        # an item-56 network consumer in another process on another host, or an
        # item-151 `[remotes]` consumer in a wholly separate composition, is
        # never enumerated here and would keep dialling the TCP+mTLS address of
        # a provider this swap is about to tear down. Refusing is the honest
        # answer: serving the successor on the SAME endpoint needs an address
        # handover (cert + `peers` material carried across, and a story for the
        # window where both processes bind the port), which is a feature, not a
        # gap to paper over with a fresh socket. Refuse before anything builds
        # or boots, exactly like the sandbox gate above.
        if old in addresses:
            _nhost, _nport, _ = addresses[old]
            _remote_keys = ", ".join(sorted(provides[old])) or "(none)"
            print(f"swap refused (item 56): {component!r} is hosted in process "
                  f"{old!r}, which is a NETWORK provider — it declares "
                  f"[processes.{old}.address] and serves {_remote_keys} over "
                  f"TCP+mTLS on {_nhost}:{_nport}, not a local socket. That "
                  f"address is part of the placement contract consumers on "
                  f"OTHER machines hold (an item-56 network consumer, or an "
                  f"item-151 [remotes] consumer in a separate composition), and "
                  f"this conductor cannot enumerate them, let alone re-point "
                  f"them. A swap boots the successor on a fresh local socket, "
                  f"so it would silently cut every remote caller off the seam "
                  f"while reporting success. Re-tier a network provider by "
                  f"bringing the composition down and re-placing it with "
                  f"[processes.{old}] backend = {to_backend!r}, which keeps the "
                  f"address (and its TLS material) declared in one place. "
                  f"Running composition untouched.", flush=True)
            return

        housemates = [c for c, p in placed.items() if p == old]
        if housemates != [component]:
            others = ", ".join(c for c in housemates if c != component)
            print(f"swap refused (v1): {component!r} shares process {old!r} with "
                  f"{others}; v1 swaps a component that is alone in its process "
                  f"(docs/swap.md).", flush=True)
            return

        # --- admission gate: refuse without touching the running composition
        candidate, error = swap_admission(files, ir, component, to_backend,
                                          from_backend=backends.get(old))
        if error is not None:
            for i, text in enumerate(error.splitlines()):
                print(f"  {'swap refused:' if i == 0 else '             '} {text}", flush=True)
            print("  running composition untouched — the candidate never booted.", flush=True)
            return

        # --- re-gate the POST-SWAP topology exactly like the initial plan
        # (parity, not prose). The swap re-tiers `component` from `old`'s backend
        # onto `to_backend`; the same seam that admission modelled component-wise
        # is here re-checked over the real process placement, so the swapped
        # component's new tier is gated identically to a from-scratch plan:
        #   * tier_capability_gate: the target tier can actually emit it;
        #   * cross_tier_boundary_check: no resource crosses a seam the re-tier
        #     opens (either direction), matching the plan-time refusal.
        # Modelled on a copy of the placement maps (the component is alone in its
        # process in v1, so only its process's backend changes), so a refusal
        # here leaves the running composition untouched, nothing booted.
        post_backends = dict(backends)
        post_backends[old] = to_backend
        cap_after = tier_capability_gate(ir, placed, post_backends)
        if cap_after:
            print(f"  swap refused: {cap_after}", flush=True)
            print("  running composition untouched; the candidate never booted.", flush=True)
            return
        boundary_after, _ = cross_tier_boundary_check(
            ir, requires, provides, owner, post_backends, services)
        if boundary_after:
            print(f"  swap refused: {boundary_after}", flush=True)
            print("  running composition untouched; the candidate never booted.", flush=True)
            return

        try:
            # the successor hosts `component` alone on the target tier, so its
            # build must contain it even in a mixed-tier placement where the
            # tier's initial slice did not (item 363: builds are per component
            # set, not the shared whole-IR cache). rebuild=True re-emits the
            # tier binary including the swapped component.
            ensure_backend(to_backend, extra=(component,), rebuild=True)
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
        # INVARIANT for anyone adding a key to the per-process spec above: a key
        # that carries a security property must either be CARRIED onto the
        # successor here, or the swap must REFUSE. A swap is ordinary use, not
        # an edge case, so a property that only holds until the first swap is
        # not a property. Two keys have already been through this — the
        # correlation guard (carried, 421 F8) and the host-ref pins (carried,
        # below) — and both were silently absent for a while first. A key that
        # CANNOT be carried correctly makes the swap refuse instead, the way a
        # sandboxed component is refused above (item 411).
        # `tests/test_swap_ref_pins.py::test_successor_spec_carries_every_boot_spec_key`
        # is the guard: it reads both dict literals out of this file and fails
        # when a key exists in one and not the other, so the NEXT key cannot be
        # forgotten the way these were. Add the key to both, or record it in
        # that test's exception table with a reason.
        succ_spec = {
            "name": succ,
            "backend": to_backend,
            "files": [str(f) for f in files],
            "components": [component],  # the swapped component, alone (v1 scope)
            # §46: the successor's intra-process dependency edges, COMPUTED for
            # the component set it hosts rather than assumed empty. Today it is
            # `{component: []}`, because the v1 scope rule above refuses swapping
            # a component that shares its process — but that is a fact about the
            # scope rule, not about §46, and nothing tied the two together. When
            # the scope rule relaxes to a multi-component successor this line
            # already produces the right edges instead of silently serializing
            # (or mis-parallelizing) the successor's activation.
            "depends": local_prereqs(manifest_entries, subset=[component]),
            # F4: the successor hosts `component` alone, so it gets only that
            # component's config — never the whole [config] table.
            "config": {c: config[c] for c in [component] if c in config},
            "provides": list(provides[old]),
            "proxies": {k: dict(v) for k, v in (specs[old].get("proxies") or {}).items()},
            # `probe` is EMPTY BY THE SAME RULE THE BOOT PATH APPLIES, not
            # dropped: a process runs the probes its own `[processes.<name>]`
            # entry declares (`pconf.get("probe") or []`), and the successor is
            # a synthesized process (`<component>__t<n>`) that the placement
            # file does not name. A boot of a process with no placement entry
            # would produce `[]` here too. Probes are one-shot boot smoke calls
            # that invoke real service methods; re-firing the predecessor's on a
            # cutover would perform operator-authored side effects at a moment
            # no operator asked for, and the swap has its own verification
            # (admission gate, repoint acknowledgement, drain + no-residue).
            "probe": [],
            # item 396 option B / 410: the successor's host-module pins. Built
            # by the SAME `host_ref_pins` the boot path uses — this is the whole
            # point of it being one function. Two things about the CONTENT:
            #   * scoped to `[component]`, the successor's own slice, not copied
            #     from the predecessor's spec (whose list is its PROCESS's
            #     slice; the two coincide only because of the v1 scope rule);
            #   * read off the running `ir`, which is exactly the document
            #     `ensure_backend` slices to emit the successor's tier artifact
            #     — so the pins describe the bytes this process is about to
            #     load. Re-deriving them from `swap_admission`'s freshly
            #     compiled `candidate` would instead re-hash whatever is on disk
            #     at swap time and quietly bless a host module that changed
            #     since the running composition was compiled.
            # Without this the successor booted with no refs and no `refRoot`,
            # so the node runner's deploy-contract hash check (which walks
            # `spec.refs`, `placement_runner.ts`) had nothing to walk: host
            # module integrity was verified at boot and unverified from the
            # first `revl swap` on, silently, while the swap reported success.
            **host_ref_pins(ir, [component], files),
            # The successor always serves on a LOCAL socket: a network
            # provider is refused above, so `old` is a UDS provider by
            # construction here and no network-only serve key (`endpoint`, and
            # the mTLS peer allowlist that rides with it) can be silently
            # dropped on the way across.
            "serve": {
                "socket": new_sock,
                "keys": serve_keys,
                "methods": {k: methods.get(provides[old][k], []) for k in serve_keys},
            },
        }
        # roadmap 421 F8: carry the predecessor's correlation guard onto the
        # successor. Without this a swap SILENTLY DISARMS the seam, because the
        # successor's serve spec is built fresh from socket/keys/methods and a
        # guard that is never installed refuses nobody. "Guarded" would mean
        # "guarded until the first swap", and `revl swap` is ordinary use, not
        # an edge case. The peer table stays valid across a swap because a swap
        # re-points EXISTING consumers at a successor socket and never attaches
        # a new one, so who may call is unchanged; only where they call moves.
        # Carried only when the successor tier can actually RUN the guard: the
        # guard is built in `_process_runner.py`, so a swap onto a non-python
        # tier drops it, the same rule the boot path applies.
        _old_corr = old_serve.get("correlation")
        if _old_corr and to_backend in _CORRELATION_SEALING_TIERS:
            succ_spec["serve"]["correlation"] = {
                "composition_id": _old_corr["composition_id"],
                "peers": dict(_old_corr["peers"]),
            }
        adapt_spec(succ_spec, to_backend)
        print(f"swap: booting {component} on the {to_backend} tier ({succ}) ...", flush=True)
        spawn(succ, to_backend, succ_spec)
        if not _wait_for(lambda: succ in up, 60):
            print(f"swap refused: successor {succ} did not come up — tearing it "
                  f"back down; running composition untouched.", flush=True)
            sproc, smode = children.pop(succ, (None, None))
            if sproc is not None:
                stop_all({succ: (sproc, smode)})
            return

        # --- re-point every consumer of these keys onto the successor socket.
        # The command carries the successor's admissible identity (`component`,
        # `backend`) so the consumer process re-admits it against its own running
        # manifest before accepting the cutover (item 337): the seam re-runs the
        # same admission gate this conductor already passed, so a raced or
        # injected repoint can never substitute an un-admitted provider. The
        # selector is only half of it: the consumer also binds `component` to
        # `key` against its own manifest, and refuses any socket outside the
        # placement directory it was handed its own spec in — which is why
        # `new_sock` is bound HERE, under `tmp`, like every other seam socket.
        for key in serve_keys:
            for qname, qspec in specs.items():
                if qname in (old, succ):
                    continue
                if key in (qspec.get("proxies") or {}):
                    qproc = children[qname][0]
                    try:
                        qproc.stdin.write(json.dumps(
                            {"op": "repoint", "key": key, "socket": new_sock,
                             "component": component, "backend": to_backend}) + "\n")
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
        stop_all({old: (oldproc, oldmode)})
        # `stop_all` already waited on the DOWN line, so this only settles the
        # pump thread's last read. A provider that had to be killed will never
        # say it, and waiting the full ten seconds for a line that cannot come
        # is just a stall.
        if old not in stranded:
            _wait_for(lambda: old in down, 10)
        for key in serve_keys:
            owner[key] = succ
        placed[component] = succ
        provides[succ] = provides.pop(old)
        requires[succ] = requires.pop(old)
        backends[succ] = to_backend
        specs[succ] = succ_spec
        specs.pop(old, None)
        if old in stranded:
            # issue 239: the drain was cut short, so the sentence below would be
            # a lie. Say what is actually known instead.
            print(f"swap: {component} now on {to_backend} ({succ}), but the old "
                  f"provider {old} exited before it said DOWN: its unwind "
                  f"is INCOMPLETE and its residue is UNKNOWN.", flush=True)
        else:
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

    if estop_latch:
        print(f"  E-Stop (item 443): armed on {estop_latch} — "
              f"`revl estop --latch {estop_latch}` halts this placement "
              f"without unwinding it", flush=True)
        threading.Thread(target=estop_watch, name="revl-estop",
                         daemon=True).start()

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
            stop_all(children)
        elif _interactive():
            if net_seams and _wait_for(lambda: len(up) == len(children), 60):
                report_network_latency()
            swap_repl()
        else:
            # stdin is not a tty: it may be carrying a swap SCRIPT (one
            # `swap <component> --to <backend>` / `:keys` / `:q` per line),
            # which is how the live-migration demo is driven as a scripted
            # exit test (docs/swap.md "Scripted (non-interactive) swaps").
            # Wait for every process to come up first so a scripted swap can
            # re-point a live consumer, then drive the SAME swap REPL off
            # stdin. EOF — a closed pipe, `< /dev/null`, or the last line —
            # tears the placement down, the same stdin-closed contract as
            # single-process `revl run`.
            _wait_for(lambda: len(up) == len(children), 60)
            if net_seams:
                report_network_latency()
            swap_repl()
    except KeyboardInterrupt:
        pass
    finally:
        watch_stop.set()
        # After a halt every child is already dead, so `stop_all` terminates
        # nothing and strands nothing — the graceful path becomes a no-op
        # rather than being skipped, which keeps the sandbox/tmpdir cleanup
        # below on one code path.
        stop_all(children)
        # item 411 Slice 2: `--rm` already fires on a clean exit; this is the
        # belt, so a torn-down placement never leaves a confined process (or a
        # container holding its granted mounts) behind it.
        for driver in sandbox_drivers.values():
            driver.teardown()
        for thread in threads:
            thread.join(timeout=2)
        for stale in cleanup:
            if os.path.exists(stale):
                os.unlink(stale)
        # the placement dir holds only sockets and spec files, both dead once
        # the children are; leaving it behind leaked a 0700 tmpdir per run.
        shutil.rmtree(tmp, ignore_errors=True)
    if stranded:
        # issue 239: this is the half that turns a visible failure into a silent
        # one. A child killed mid-teardown used to be indistinguishable from a
        # clean exit here, so a partial teardown passed as success.
        print(_stranded_teardown_report(stranded), file=sys.stderr)
        rc = rc or 1
    if halted:
        # item 443: an E-Stop is NEVER clean. The report above already named
        # what is owed; this is only the status that carries it out of the
        # process, so a halted placement cannot pass as a finished one.
        rc = rc or 1
    return rc
