"""Sandbox runtime drivers (roadmap item 411, Slice 2): the `container` rung.

Slice 1 made the sandbox surface STATIC — the manifest tables, the plan-time
capability gate, the per-process spec narrowing, the boot-summary envelope. A
sandboxed process still booted on the ordinary runner: the isolation was
DECLARED, never established. This module is the first rung of the ladder made
REAL. A process placed in a `container` sandbox now runs inside a container
whose confinement flags are derived from its declared envelope, and the
confinement is VERIFIED from inside the boundary before the process is allowed
to start.

Which rung, and why this one first
----------------------------------
The ladder is `wasm-cell` -> `container` -> `microvm` (weakest to strongest).
`container` is the cheapest rung that is *genuinely an isolation boundary*:

* `wasm-cell` is in-process. Its confinement is a generated import set (the
  cell's `no_extern` surface plus the seam imports), which is a compiler-side
  job on top of item 335's substrate, not a runtime driver — and an in-process
  cell shares the conductor's address space, so it is the weakest claim on the
  ladder even once it exists.
* `microvm` needs a hypervisor (`/dev/kvm`, firecracker/qemu). Neither this
  repository's CI runners nor a developer laptop reliably has nested
  virtualization, so its driver could be written but never EXECUTED, which is
  the shape this repo has been bitten by (item 430/445).
* `container` needs a container runtime, which developer machines and
  GitHub-hosted `ubuntu-latest` runners both have. It is a real kernel-enforced
  boundary (namespaces + cgroups + a dropped capability set), and it is what the
  411 design names for the untrusted-author payoff (`docker run --network=none
  --read-only`).

Refuse, never degrade
---------------------
A sandbox is a security boundary, so every failure direction here is REFUSAL.
There is deliberately no code path in this module or its caller that runs a
sandboxed process outside its boundary: if the runtime is missing, if the image
cannot be resolved, if the backend has no in-container form yet, if the seam
cannot cross the boundary, or if the in-sandbox canary cannot CONFIRM the
confinement actually took, the placement aborts. A composition that believes it
holds an isolation it does not hold is worse than one with no isolation at all,
because the rest of the composition then trusts a body that is not confined.

The trusted-enforcer caveat from the design still stands: revl asks the
container runtime for the confinement and then checks, from inside, what it can
observe (interfaces, mount flags, root filesystem writability). A runtime that
lies about all of that is outside what revl can detect, exactly as the OS is
today. The canary is what turns "we passed a flag" into "the boundary reports
itself established", and its evidence is printed, so the achieved rung is
auditable rather than assumed.

What T3 adds, and what is still NOT in this slice
-------------------------------------------------
* The per-rung SEAM TRANSPORT REACHABILITY (item 411 T3, this slice). The 363
  seam is a Unix socket in the placement directory, which does not cross a
  container bind mount portably (verified non-functional over a Docker Desktop
  bind mount on macOS). T3 carries item 56's TCP+mTLS across the boundary over
  a SEAM-ONLY per-process `--internal` network (`--network <net>` in place of
  `--network=none`, `container_flags`) and one conductor-owned RELAY
  (`SeamRelayManager`, `revl.seam_relay`) that is the only other thing on it —
  a blind byte forwarder holding no key. The seam-only canary
  (`seam_canary_script`, `_evaluate_seam`) confirms the transport from inside:
  every relay listener accepts (SEAM), a target the relay proved open from its
  bridge side is dropped from the sandbox (ISOLATION), and DNS is closed. The
  item-56 role rule and item-54 deadline rule stay plan-layer refusals
  (`seam_transport_descriptor`); reachability is now ESTABLISHED and confirmed,
  no longer a standing refusal. T1 (`seam_transport_descriptor`) and T2
  (`seam_dir_mounts`, the per-process cert view) still hold underneath.
* The conductor-served approval-across-boundary channel (item 411 T5): the
  conductor serves an `approval` key over the same mTLS listener shape, carried
  by one relay row per sandboxed process that can raise a class-(c) operation,
  so an approval request from inside a sandbox reaches the operator VIA the
  conductor rather than by a direct escape. The plan-layer wiring (the relay
  row, the served key, the boot-summary count) lands here; see `placement.py`
  `sandbox_approval_rows`.
* Non-`py` backends inside a container.

Since this header was first written, the `wasm-cell` rung (the in-process cell
substrate) and the `microvm` rung (the KVM guest, item 411 T6) have both landed
their drivers below. Neither downgrades: `wasm-cell` verifies the cell substrate
and refuses the unbuilt component-hosting step, and `microvm` gates on `/dev/kvm`
via `microvm_runtime_reason` — refusing with a named gap wherever the accelerator
is absent (every host in reach today), and where KVM is present booting a VM and
confirming the boundary from inside before refusing the same unbuilt hosting
step. Verifying that live microVM boot on a KVM-capable lane is the last
requirement to close item 411's microVM rung.
"""

from __future__ import annotations

import importlib.util
import os
import secrets
import shlex
import shutil
import socket
import subprocess
import sys
import threading
from pathlib import Path

from ._paths import backends_root

# One place for every container-runtime call's timeout. A hung daemon must
# surface as a refusal with a diagnostic, never as a placement that waits.
_DOCKER_TIMEOUT = 120.0
# The canary is a `sh` one-shot; it either answers immediately or the boundary
# is not usable.
_CANARY_TIMEOUT = 60.0

# The in-sandbox boot canary. Mostly POSIX `sh` reading what the KERNEL reports
# from inside the boundary rather than what the flags asked for:
#
#   ARCH=<uname -m>      the kernel's machine arch inside the boundary
#   ROUTES=<n>            IPv4 route entries in this network namespace
#   EGRESS=blocked:<e>    an ACTIVE outbound connect attempt's errno (net=none)
#   ROOTFS=ro|rw          whether the root filesystem accepts a write
#   MOUNT=<mp> <opts>     the kernel's own mount options for each mount point
#   PY=yes|no             whether the image has a python3 at all
#   RUNTIME=image|absent  whether the image already carries revl + cordis-py
#
# The egress clause is deliberately ACTIVE. A passive reading (an interface
# list) is not enough on its own: a `--network=none` namespace still shows the
# kernel's address-less tunnel stubs (`tunl0`, `ip6tnl0`) on a stock Docker
# Desktop, so "no interfaces but lo" would refuse a boundary that is in fact
# established. What actually distinguishes the two is that a confined namespace
# has NO route and a connect fails immediately; the probe target is TEST-NET-1
# (RFC 5737), which is unroutable by construction, so a boundary that turned out
# NOT to be established still reaches nothing — it times out, and a timeout is a
# refusal, because an unconfirmed boundary is refused like a broken one.
#
# `RUNTIME` decides whether the host runtime has to be mounted in (see
# `_runtime_mounts`); it is reported by the same probe so the driver costs one
# container per sandboxed process at preflight, not two.
_EGRESS_PY = (
    "import socket;s=socket.socket();s.settimeout(2)\n"
    "try:\n"
    "    s.connect(('192.0.2.1', 9));print('EGRESS=open')\n"
    "except socket.timeout:\n"
    "    print('EGRESS=timeout')\n"
    "except OSError as e:\n"
    "    print('EGRESS=blocked:%s' % (e.errno,))\n"
)

_CANARY_SH = r"""
echo "ARCH=$(uname -m)"
echo "ROUTES=$(tail -n +2 /proc/net/route | wc -l | tr -d ' ')"
if touch /.revl-canary-probe 2>/dev/null; then
  echo "ROOTFS=rw"
  rm -f /.revl-canary-probe 2>/dev/null
else
  echo "ROOTFS=ro"
fi
while read -r dev mp fstype opts rest; do
  echo "MOUNT=$mp $opts"
done < /proc/mounts
if command -v python3 >/dev/null 2>&1; then
  echo "PY=yes"
  if python3 -c "import cordis, revl" >/dev/null 2>&1; then
    echo "RUNTIME=image"
  else
    echo "RUNTIME=absent"
    echo "RUNTIME_ERR=$(python3 -c 'import cordis, revl' 2>&1 | tail -n 1)"
  fi
  __EGRESS__
else
  echo "PY=no"
  echo "RUNTIME=absent"
fi
echo "CANARY=done"
"""


def _canary_script(net: str) -> str:
    """The canary, with the active egress probe compiled in only when the
    envelope claims `net = "none"`. Under `net = "all"` the envelope confines
    nothing, so there is nothing to confirm and the sandbox must not make an
    outbound connection of its own to say so."""
    if net == "none":
        return _CANARY_SH.replace("__EGRESS__", f"python3 -c \"{_EGRESS_PY}\"")
    return _CANARY_SH.replace("__EGRESS__", 'echo "EGRESS=unclaimed"')


# --------------------------------------------------------------------------
# the seam-only canary (item 411 T3)
# --------------------------------------------------------------------------
#
# The `--network=none` canary confirms confinement by an active connect to
# TEST-NET-1 that must fail AT ONCE (ENETUNREACH). An `--internal` network DROPS
# rather than refuses (measured, docs/design/411-seam-transport.md), so that one
# clause cannot be reused. The seam-only variant replaces the single negative
# probe with a DISCRIMINATING pair plus two DNS closures, all run from inside
# the process's own network with the relay already up:
#
#   SEAM=open|closed:<e>    every relay listener this process's seams use must accept
#   ISOLATION=confirmed|LEAK a target the relay proved open from bridge must NOT open here
#   DNS=closed|OPEN:<name>  an external name and host.docker.internal must not resolve
#   ROUTES=<n>              reported, not judged (internal subnet + dropped default route)
#
# Same isolation target, two vantage points, one positive (the relay's, proven
# before launch) and one that must be negative (the sandbox's): that is what
# makes a DROP evidence of confinement rather than the absence of evidence.
_SEAM_PROBE_PY = r"""
import socket
def _c(host, port):
    # (connected, errno). Connect-success and a timeout must be distinguishable:
    # a dropped connect on an --internal network raises `TimeoutError` whose
    # `errno` is None (the network DROPS, it does not refuse), and a bare errno
    # return would then read a drop as a successful open — the exact inversion
    # that would misreport a confined sandbox as a LEAK. So success is its own
    # boolean, never inferred from errno being absent.
    s = socket.socket(); s.settimeout(3)
    try:
        s.connect((host, int(port))); return True, None
    except OSError as ex:
        return False, ex.errno
    finally:
        s.close()
SEAM = __SEAM__
ISO = __ISO__
DNS = __DNS__
ok = True
for host, port in SEAM:
    up, e = _c(host, port)
    if not up:
        print("SEAM=closed:%s(%s:%s)" % (e, host, port)); ok = False
if ok and SEAM:
    print("SEAM=open")
elif not SEAM:
    print("SEAM=none")
if ISO:
    up, e = _c(ISO[0], ISO[1])
    print("ISOLATION=LEAK" if up else "ISOLATION=confirmed")
else:
    print("ISOLATION=unclaimed")
leaked = []
for name in DNS:
    try:
        socket.getaddrinfo(name, None); leaked.append(name)
    except OSError:
        pass
print("DNS=OPEN:%s" % (",".join(leaked),) if leaked else "DNS=closed")
"""


def seam_canary_script(seam_targets, isolation_target,
                       dns_names=("example.com", "host.docker.internal")) -> str:
    """A POSIX `sh` one-shot for a SEAM-carrying sandbox: the ordinary boundary
    clauses (rootfs, mounts, arch, runtime) via `_CANARY_SH`, plus the seam-only
    network clauses via `_SEAM_PROBE_PY`. `seam_targets` is the list of
    `(host, port)` relay listeners this process's seams reach; `isolation_target`
    is the `(host, port)` the relay proved open from its bridge side, which must
    be UNreachable from inside the sandbox."""
    probe = (_SEAM_PROBE_PY
             .replace("__SEAM__", repr([[h, int(p)] for h, p in seam_targets]))
             .replace("__ISO__", repr([isolation_target[0], int(isolation_target[1])]
                                      if isolation_target else []))
             .replace("__DNS__", repr(list(dns_names))))
    # The probe is a full Python program carrying double-quoted string literals
    # (e.g. print("SEAM=closed:...")). Interpolating it into a `python3 -c "..."`
    # double-quoted word lets those inner quotes terminate the shell argument and
    # exposes the following text (parentheses, %s) as shell syntax, so the emitted
    # `sh -c` program fails to parse. Transport it as one shell-quoted argument.
    return _CANARY_SH.replace("__EGRESS__", f"python3 -c {shlex.quote(probe)}")


def _run(argv: list[str], *, timeout: float = _DOCKER_TIMEOUT) -> tuple[int, str, str]:
    """One container-runtime call. Never raises: a missing binary, a hung
    daemon and a non-zero exit all come back as a return code the caller turns
    into a refusal."""
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return 127, "", f"{argv[0]}: not found"
    except subprocess.TimeoutExpired:
        return 124, "", f"{' '.join(argv[:3])}: timed out after {timeout:g}s"
    except OSError as exc:  # pragma: no cover - a broken exec environment
        return 126, "", str(exc)
    return proc.returncode, proc.stdout, proc.stderr


def _tail(text: str, limit: int = 240) -> str:
    """The last useful line of a runtime's stderr, for a refusal diagnostic."""
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    return (lines[-1][:limit] if lines else "no output")


# --------------------------------------------------------------------------
# mixed-arch: the OCI platform string, and what `uname -m` must report under it
# --------------------------------------------------------------------------
#
# The `container` rung is an arch bridge (411 design, "Mixed-arch
# compositions"): a component placed with `platform = "linux/arm64"` runs in a
# container OF THAT ARCH — the runtime supplies emulation (qemu/binfmt, Rosetta)
# or a native node — and composes with the host-arch placement over the
# arch-agnostic seam. revl REQUESTS the platform (`--platform`) and then, in the
# same "confirm from inside, never trust the flag" discipline as the rest of the
# canary, CONFIRMS the boundary actually came up under that arch by reading
# `uname -m` from inside it. A platform the runtime silently ran as the host
# arch (emulation absent) is refused, not accepted: an unconfirmed arch is a
# boundary that did not take, like any other.
#
# The map is OCI arch -> the `uname -m` value(s) a correct kernel reports for it.
# A `platform` whose arch is not in this map is refused where it ENTERS
# (`placement._normalize_sandbox_table`), so the driver never reaches an arch it
# could not have confirmed.
_OCI_ARCH_UNAME: dict[str, tuple[str, ...]] = {
    "amd64": ("x86_64",),
    "386": ("i686", "i386"),
    "arm64": ("aarch64", "arm64"),
    "arm": ("armv7l", "armv6l", "armv8l"),
    "ppc64le": ("ppc64le",),
    "s390x": ("s390x",),
    "riscv64": ("riscv64",),
}


def accepted_uname(platform: str) -> tuple[str, ...] | None:
    """The `uname -m` values a correct kernel reports for an OCI platform string
    (`os/arch[/variant]`), or None when the string is malformed or names an arch
    this driver has no confirmation mapping for.

    None is the signal `placement` refuses on: a platform the canary could not
    verify from inside is not accepted at plan time, so a declared arch is never
    a claim the runtime is merely trusted to have honored."""
    parts = platform.split("/")
    if len(parts) not in (2, 3) or not all(parts):
        return None
    # os/arch/variant are lowercase alphanumeric tokens (linux, arm64, v7)
    if not all((seg.isalnum() and seg.islower()) or seg.isdigit() for seg in parts):
        return None
    return _OCI_ARCH_UNAME.get(parts[1])


# --------------------------------------------------------------------------
# derived confinement: envelope -> container flags
# --------------------------------------------------------------------------

def container_flags(env: dict, *, name: str, mounts: list[tuple[str, str]],
                    interactive: bool = True, workdir: str | None = None,
                    network: str | None = None) -> list[str]:
    """The confinement flags one envelope derives, as a list, deterministically.

    Pure: no runtime is consulted, so this is the half of the driver that is
    testable everywhere and reviewable as a whole. `mounts` is the ALREADY
    resolved mount list (path, mode) — the declared `fs` grants plus the
    placement directory and, when the image does not carry it, the host
    runtime; every one of them is reported in the achieved record, so nothing
    the driver adds on the author's behalf is invisible.

    `network` is the per-process seam-only network a seam-carrying sandbox joins
    (item 411 T3): an `--internal` user-defined network the conductor created,
    on which the ONLY other endpoint is the relay. When set it REPLACES
    `--network=none` (the sandbox has no host loopback of its own; its seams
    reach the relay's listeners on this network and nothing else), and under
    `net = "all"` the driver additionally `docker network connect`s the
    container to the default bridge after start so there is one transport path.
    When it is None the pre-T3 behaviour stands: `net = "none"` derives
    `--network=none`, `net = "all"` derives no network flag.

    Beyond the envelope's own `fs`/`net`, every container gets the hardening the
    411 design names as the point of the rung: a read-only root filesystem, no
    added capabilities, no privilege escalation, and the invoking user's uid so
    a write into a granted `rw` mount lands as that user rather than as root.
    """
    flags = [
        "--rm",
        "--name", name,
        "--read-only",
        # a read-only root still needs somewhere to put a temporary file, and
        # an interpreter start-up will. nosuid/nodev keep it from being a hole.
        "--tmpfs", "/tmp:rw,nosuid,nodev,size=64m",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--pids-limit", "512",
        "--label", "revl.sandbox=411",
    ]
    platform = env.get("platform")
    if platform:
        # the mixed-arch bridge: run the container OF the declared arch and let
        # the runtime supply emulation. The canary confirms the arch actually
        # took from inside (`_evaluate`), so a runtime that ignored the flag is
        # a refusal, not a silent host-arch run.
        flags += ["--platform", platform]
    if network is not None:
        # item 411 T3: the seam-only per-process network. It carries the seam
        # (to the relay) and nothing else; under net=none it is the whole of the
        # container's reachability, under net=all the driver also joins bridge.
        flags += ["--network", network]
    elif env.get("net", "none") == "none":
        flags += ["--network=none"]
    uid_gid = _uid_gid()
    if uid_gid:
        flags += ["--user", uid_gid]
    for path, mode in mounts:
        # identity-mapped: the spec file, the placement directory and every
        # granted path keep their absolute host spelling inside the boundary,
        # so a path the conductor wrote into the spec resolves to the same file
        # on the other side of it.
        flags += ["-v", f"{path}:{path}:{'rw' if mode == 'rw' else 'ro'}"]
    if workdir:
        # the same working directory as the conductor, so a relative source path
        # the placement was invoked with resolves to the same file inside.
        flags += ["--workdir", workdir]
    if interactive:
        # the runner's control channel (`repoint`) is newline-delimited JSON on
        # stdin, so stdin must stay open across the boundary.
        flags += ["-i"]
    return flags


def _uid_gid() -> str | None:
    try:
        return f"{os.getuid()}:{os.getgid()}"
    except AttributeError:  # pragma: no cover - non-POSIX host
        return None


def envelope_mounts(env: dict) -> list[tuple[str, str]]:
    """The declared `fs` grants as (path, mode) pairs. Paths are already
    canonical and absolute: a non-canonical spelling was refused where it
    entered (`placement._normalize_sandbox_table`)."""
    out: list[tuple[str, str]] = []
    for mount in env.get("fs") or []:
        parts = mount.split(":")
        out.append((parts[0], parts[1] if len(parts) >= 2 and parts[1] else "ro"))
    return out


def _runtime_mounts() -> tuple[list[tuple[str, str]], str | None]:
    """The host paths a `py` process needs to BE a revl runner, read-only, plus
    the PYTHONPATH they imply — or a diagnostic when they cannot be located.

    An image that already carries revl + cordis-py needs none of this (the
    canary reports `RUNTIME=image` and the caller drops them). When it does not,
    they are mounted READ-ONLY and listed in the achieved record: the confined
    body can read the conductor's own source, which is a real widening of what
    it can see and therefore is printed rather than assumed harmless. It buys
    the property that the sandboxed process runs the SAME vintage of revl and
    cordis as the conductor that admitted it, with no image/version skew.
    """
    revl_src = Path(__file__).resolve().parent.parent
    # the py runner imports the cordis-py bridge/runtime out of the backends
    # tree by path (`_process_runner.run` -> `backends_root() / "python"`), so
    # the runner is not complete without it.
    backend_py = backends_root() / "python"
    if not backend_py.is_dir():  # pragma: no cover - a broken checkout/wheel
        return [], (f"the cordis-py backend directory {backend_py} is missing, so "
                    "the py runner cannot be mounted into the sandbox")
    try:
        spec = importlib.util.find_spec("cordis")
    except (ImportError, ValueError):  # pragma: no cover - broken install
        spec = None
    if spec is None or not spec.origin:
        return [], ("the cordis-py runtime is not importable by this conductor, "
                    "so it cannot be mounted into the sandbox and the image "
                    "does not carry it either")
    cordis_root = Path(spec.origin).resolve().parent.parent
    return [(str(revl_src), "ro"), (str(backend_py), "ro"),
            (str(cordis_root), "ro")], None


def seam_dir_mounts(ctx: dict) -> list[tuple[str, str]]:
    """The per-process view of the placement directory a sandboxed process is
    given (item 411 T2), replacing the whole-directory read-write mount Stage 2a
    used.

    A confined process reads its OWN spec and — on a cross-boundary sandbox seam
    — its own conductor-minted leaf certificate, its own key, and the CA
    certificate, all read-only. It never sees the CA key, nor a sibling's spec
    or key: the whole-directory mount handed a hostile sandbox every process's
    private identity, which is the exact leak this narrowing closes before T3
    makes a sandboxed seam launchable at all.

    When `ctx` carries no `spec_path` (a bare-ctx unit probe that refuses before
    launch), the pre-T2 whole-directory mount is returned unchanged, so a caller
    that has not adopted the narrowed view is byte-identical.
    """
    spec_path = ctx.get("spec_path")
    if not spec_path:
        return [(ctx["seam_dir"], "rw")]
    mounts: list[tuple[str, str]] = [(spec_path, "ro")]
    tls = ctx.get("seam_tls")
    if tls:
        # leaf, key and CA cert only — never the CA key (which never leaves the
        # conductor's own mint directory, itself unmounted).
        mounts += [(tls["cert"], "ro"), (tls["key"], "ro"), (tls["ca"], "ro")]
    return mounts


def source_mounts(files, cwd: str) -> list[tuple[str, str]]:
    """The composition's own `.rvl` sources, read-only.

    A py process re-COMPILES the composition it was handed (item 337 boot
    re-admission: the running manifest it judges its seams against is one it
    builds itself, not one the conductor asserts), so its own program text has
    to be readable inside the boundary. The conductor's working directory comes
    with them because the file list it was invoked with may be relative to it,
    and the container runs with the same working directory so those paths
    resolve to the same files.
    """
    dirs = {cwd}
    for f in files:
        dirs.add(str(Path(f).resolve().parent))
    return [(d, "ro") for d in sorted(dirs)]


# --------------------------------------------------------------------------
# the seam transport descriptor (item 411 T1)
# --------------------------------------------------------------------------

# The plan-layer transport a cross-boundary sandbox seam rides: item 56's
# TCP+mTLS between the two processes, carried over a conductor-owned relay on a
# per-process network (docs/design/411-seam-transport.md, "The decision"). T1
# built the descriptor and its item-56 preconditions; T3 lands the reachability
# those preconditions gate — the seam-only per-process network, the relay, and
# the seam-only canary that confirm the seam actually crosses. So the plan-layer
# descriptor now admits (empty `unmet`) once the item-56 role and item-54
# deadline rules hold, and REACHABILITY is established at preflight by the driver
# (`ContainerDriver._establish_reachability`), not asserted here.
SEAM_TRANSPORT = "relay-mtls"


def seam_transport_descriptor(pname: str, seams: list) -> dict:
    """The `relay-mtls` transport descriptor for a sandboxed process's
    cross-boundary seams (item 411 T1/T3): `{"transport", "unmet": [...]}`,
    where `unmet` names every PLAN-LAYER precondition NOT satisfied, one human
    refusal fragment each. The item-56 role rule (a provider is py; a consumer
    is py/node, the rust/go/java runners holding only the UDS-only client) and
    the item-54 deadline rule (every in-placement participant's `seam_deadline`
    is non-null) are checked here per seam. An empty `unmet` means the plan
    layer admits and the driver goes on to ESTABLISH reachability (network +
    relay + seam-only canary, T3); a seam-free process passes an empty `seams`
    and no descriptor is built at all."""
    unmet: list[str] = []

    def add(fragment: str) -> None:
        if fragment not in unmet:
            unmet.append(fragment)

    for e in seams:
        key = e["key"]
        pb = e.get("provider_backend", "py")
        cb = e.get("consumer_backend", "py")
        # provider role: the mTLS listener is py-only (item 56). A remote
        # provider lives in another composition that already serves mTLS (py),
        # so its backend is not this placement's to check.
        if not e.get("remote") and pb != "py":
            add(f"provider {e['provider']!r} of seam {key!r} is on the {pb} "
                f"backend: the TCP+mTLS listener (item 56) is py-only, so a "
                f"sandboxed provider — and any host-side provider serving a "
                f"sandbox — must be a py process")
        # consumer role: the TCP+mTLS client ships on py and node/ts only
        # (item 149); rust/go/java read only the local `socket` (UDS-only) form.
        if cb not in ("py", "node"):
            add(f"consumer {e['consumer']!r} of seam {key!r} is on the {cb} "
                f"backend: the TCP+mTLS client ships on the py and node/ts "
                f"runners only (item 149); the {cb} runner reads only the local "
                f"`socket` (UDS-only) client, so a {cb} consumer of a sandboxed "
                f"provider is refused — put it on py or node/ts")
        # deadline rule (item 54): every participant this placement can see must
        # carry a non-null seam_deadline. The remote provider's deadline is not
        # visible here and is not this side's to enforce.
        parts = [("provider", e["provider"], e.get("provider_deadline"))]
        if e.get("remote"):
            parts = []
        parts.append(("consumer", e["consumer"], e.get("consumer_deadline")))
        for _role, part, dl in parts:
            if dl is None:
                add(f"participant {part!r} of seam {key!r} has a null "
                    f"seam_deadline: a cross-boundary round-trip needs a deadline "
                    f"(item 54); set seam_deadline on {part!r} or leave it at the "
                    f"default")

    return {"transport": SEAM_TRANSPORT, "unmet": unmet}


# --------------------------------------------------------------------------
# the seam relay + per-process networks (item 411 T3)
# --------------------------------------------------------------------------

# The candidate host bind addresses, in order (docs/design/411-seam-transport.md,
# "Host bind address"). On Docker Desktop `host.docker.internal` reaches the
# host's loopback, so `127.0.0.1` is right; on Linux the relay reaches the host
# through the bridge gateway and a loopback-bound listener is not reachable, so
# the gateway address is the one that answers. The conductor does not guess — it
# PROBES each in order and takes the first that answers; `0.0.0.0` is never a
# candidate.
_HOST_BIND_LOOPBACK = "127.0.0.1"

# The probe body a bridge container runs to test one host bind candidate: dial
# `host.docker.internal:<port>`, exchange a byte, print REACHABLE. Stdlib only
# (the runner image carries a stock `python3`); a non-zero exit or no REACHABLE
# line means this candidate did not answer from the relay's vantage.
_HOST_BIND_PROBE_PY = (
    "import socket,sys\n"
    "port=int(sys.argv[1])\n"
    "s=socket.socket()\n"
    "s.settimeout(5)\n"
    "s.connect(('host.docker.internal', port))\n"
    "s.sendall(b'revl-bind-probe')\n"
    "s.recv(32)\n"
    "print('REACHABLE')\n"
)


def seam_network_name(placement_id: str, pname: str) -> str:
    """The `--internal` per-process seam network's name (item 411 T3). One per
    sandboxed process, so a sandbox sees only its own seams' endpoints."""
    return f"revl-sb-{placement_id}-{pname}"


def relay_container_name(placement_id: str) -> str:
    """The one relay container per placement — the only thing besides the
    sandbox on each per-process network, and the only forwarder in the path."""
    return f"revl-sb-{placement_id}-relay"


class SeamRelayManager:
    """Creates the per-process seam networks and the one conductor-owned relay
    for a placement, and tears them down (item 411 T3).

    Docker-gated: every method that touches the runtime is a no-op refusal when
    no `docker` is resolved, and the object still tracks the names it would have
    created so teardown is exact. The relay is `python3 -m revl.seam_relay`
    running the derived table inside the first-party runner image; it holds no
    key and forwards ciphertext only.
    """

    def __init__(self, placement_id: str, image: str,
                 docker: str | None = None) -> None:
        self.placement_id = placement_id
        self.image = image
        self._docker = docker
        self.relay_name = relay_container_name(placement_id)
        self._networks: dict[str, str] = {}   # pname -> network name
        self._relay_started = False
        self._bind_host: str | None = None

    # -- naming / bookkeeping (plan-layer) ---------------------------------
    def network_for(self, pname: str) -> str:
        net = seam_network_name(self.placement_id, pname)
        self._networks[pname] = net
        return net

    def created_names(self) -> tuple[str, list[str]]:
        """The relay container and networks this manager is responsible for
        removing, for the teardown audit."""
        return self.relay_name, sorted(self._networks.values())

    # -- runtime lifecycle (docker-gated) ----------------------------------
    def _resolve_docker(self) -> str | None:
        if self._docker is None:
            self._docker = shutil.which("docker") or ""
        return self._docker or None

    def create_network(self, pname: str) -> tuple[str, str | None]:
        """Create the process's `--internal` network. Returns `(name, err)`."""
        net = self.network_for(pname)
        docker = self._resolve_docker()
        if docker is None:  # pragma: no cover - caller refused earlier
            return net, "no container runtime to create the seam network"
        rc, _, err = _run([docker, "network", "create", "--internal", net])
        if rc != 0:
            return net, (f"could not create the seam-only network {net!r} for "
                         f"{pname!r} ({_tail(err)})")
        return net, None

    def relay_argv(self, docker: str, table_path: str, bind_host: str) -> list[str]:
        """The exact `docker run` argv the relay is started with — a `-d` runner
        on the default bridge, the derived table mounted read-only and passed by
        path, no key of any kind. Split out so it is reviewable without a daemon."""
        return [docker, "run", "-d", "--rm", "--name", self.relay_name,
                "--label", "revl.sandbox=411",
                "-v", f"{table_path}:{table_path}:ro",
                self.image,
                "python3", "-m", "revl.seam_relay", table_path,
                "--bind-host", bind_host]

    def start_relay(self, table_path: str, bind_host: str) -> str | None:
        """Start the one relay container from the derived table (mounted at
        `table_path`), on the default bridge. Idempotent: only the first call
        starts it."""
        if self._relay_started:
            return None
        docker = self._resolve_docker()
        if docker is None:  # pragma: no cover
            return "no container runtime to start the seam relay"
        self._bind_host = bind_host
        rc, _out, err = _run(self.relay_argv(docker, table_path, bind_host))
        if rc != 0:
            return f"could not start the seam relay {self.relay_name!r} ({_tail(err)})"
        self._relay_started = True
        return None

    def connect_relay(self, net: str) -> str | None:
        """Attach the relay to one per-process network so it can forward onto
        it. The relay binds its listeners on its address on THIS network."""
        docker = self._resolve_docker()
        if docker is None:  # pragma: no cover
            return "no container runtime to connect the seam relay"
        rc, _, err = _run([docker, "network", "connect", net, self.relay_name])
        if rc != 0:
            return (f"could not attach the relay to {net!r} ({_tail(err)})")
        return None

    def _bridge_gateway(self, docker: str) -> str | None:
        """The default bridge's gateway address — the Linux candidate a
        loopback-bound host listener is NOT reachable on, so the probe must be
        what decides, never this value on its own."""
        rc, out, _ = _run([docker, "network", "inspect", "bridge",
                           "--format", "{{(index .IPAM.Config 0).Gateway}}"])
        gw = (out or "").strip()
        return gw or None

    def _host_bind_candidates(self, docker: str) -> list[str]:
        """The candidate host bind addresses in order: loopback first (right on
        Docker Desktop), then the bridge gateway (right on Linux)."""
        candidates = [_HOST_BIND_LOOPBACK]
        gw = self._bridge_gateway(docker)
        if gw and gw not in candidates:
            candidates.append(gw)
        return candidates

    def _probe_bind_candidate(self, docker: str, candidate: str,
                              *, timeout: float = 20.0) -> tuple[bool, str]:
        """The real per-candidate probe (docs/design/411-seam-transport.md,
        "Host bind address"): bind a throwaway listener on THIS candidate on the
        host, then run a short-lived container on the default bridge that dials
        `host.docker.internal:<port>` and exchanges a byte. Returns
        `(reachable, detail)`; `detail` is the reason it did not answer, for the
        refusal diagnostic. A bridge container is the faithful stand-in for the
        relay (same image, same default-bridge vantage, same `host-gateway`
        route), and it is used because the real relay is not started until the
        bind address it needs is known."""
        try:
            srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        except OSError as exc:  # pragma: no cover - a broken socket layer
            return False, f"could not open a probe socket ({exc})"
        try:
            try:
                srv.bind((candidate, 0))
            except OSError as exc:
                # a candidate the host cannot even bind (e.g. the Linux bridge
                # gateway on a macOS host, which lives inside the VM) is not a
                # host bind address; rule it out rather than treating the later
                # container failure as ambiguous.
                return False, f"not bindable on the host ({exc})"
            srv.listen(1)
            srv.settimeout(timeout)
            port = srv.getsockname()[1]
            answered = {"ok": False}

            def _accept() -> None:
                try:
                    conn, _ = srv.accept()
                    with conn:
                        conn.settimeout(timeout)
                        conn.recv(16)
                        conn.sendall(b"revl-bind-ok")
                    answered["ok"] = True
                except OSError:
                    pass

            acceptor = threading.Thread(target=_accept, daemon=True)
            acceptor.start()
            rc, out, err = _run(
                [docker, "run", "--rm", "--label", "revl.sandbox=411",
                 "--add-host", "host.docker.internal:host-gateway",
                 self.image, "python3", "-c", _HOST_BIND_PROBE_PY,
                 str(port)],
                timeout=timeout)
            acceptor.join(timeout=2.0)
            if rc == 0 and "REACHABLE" in (out or "") and answered["ok"]:
                return True, "reachable"
            reason = _tail(err) if err.strip() else _tail(out)
            return False, f"not reachable via host.docker.internal ({reason})"
        finally:
            try:
                srv.close()
            except OSError:  # pragma: no cover
                pass

    def _select_host_bind(self, candidates: list[str], probe) -> tuple[str | None, str | None]:
        """Take the first candidate `probe(candidate) -> (ok, detail)` reports
        reachable, or refuse naming every candidate and why each failed. Split
        from `probe_host_bind` so the selection order and the no-answer refusal
        are unit-testable with an injected probe (no runtime)."""
        tried: list[str] = []
        for candidate in candidates:
            ok, detail = probe(candidate)
            if ok:
                self._bind_host = candidate
                return candidate, None
            tried.append(f"{candidate} ({detail})")
        return None, (
            "no host bind address answered a relay-side connect via "
            "host.docker.internal, so a host-side seam listener could not be "
            f"reached from the sandbox network: tried {', '.join(tried)}. The "
            "conductor does not fall back to 0.0.0.0 or to net=all.")

    def probe_host_bind(self) -> tuple[str | None, str | None]:
        """The address a host-side seam listener binds so the relay can reach it
        (docs/design/411-seam-transport.md, "Host bind address"). Probed, never
        guessed: bind a throwaway listener on each candidate in order
        (`127.0.0.1`, then the bridge gateway) and have a bridge container
        connect to `host.docker.internal:<port>`; the first that answers wins.
        Returns `(address, None)` or `(None, diagnostic)` naming both candidates.

        Docker-gated: when no docker is resolved it returns the loopback so the
        plan layer has a value; with a runtime the bind+connect probe runs and
        decides (`127.0.0.1` on Docker Desktop, the bridge gateway on Linux)."""
        docker = self._resolve_docker()
        if docker is None:  # pragma: no cover - caller refused earlier
            return _HOST_BIND_LOOPBACK, None
        candidates = self._host_bind_candidates(docker)
        return self._select_host_bind(
            candidates, lambda c: self._probe_bind_candidate(docker, c))

    def relay_bridge_target(self, port: int) -> tuple[str, int] | None:
        """The relay's own bridge-side `(ip, port)` — a target the relay proves
        open from its bridge vantage, which the seam-only canary requires to be
        DROPPED from inside the sandbox (the discriminating ISOLATION clause).
        None when the relay's bridge IP cannot be read."""
        docker = self._resolve_docker()
        if docker is None or not self._relay_started:  # pragma: no cover
            return None
        rc, out, _ = _run([docker, "inspect", "-f",
                           "{{.NetworkSettings.Networks.bridge.IPAddress}}",
                           self.relay_name])
        ip = (out or "").strip()
        if rc != 0 or not ip:
            return None
        return ip, port

    def teardown(self) -> None:
        """Remove the relay and every per-process network, best-effort — the
        belt for a conductor killed mid-run (design: relay death withdraws every
        sandbox seam, so no residue must survive it)."""
        docker = self._resolve_docker()
        if docker is None:  # pragma: no cover - nothing was created
            return
        if self._relay_started:
            _run([docker, "rm", "-f", self.relay_name], timeout=30.0)
            self._relay_started = False
        for net in list(self._networks.values()):
            _run([docker, "network", "rm", net], timeout=30.0)
        self._networks.clear()


# --------------------------------------------------------------------------
# the container rung driver
# --------------------------------------------------------------------------

class ContainerDriver:
    """The `container` rung: preflight + canary + launch + teardown.

    One instance per placement run; it remembers the container names it started
    so teardown can remove them even if the runtime's own `--rm` did not fire.
    """

    rung = "container"
    name = "docker"

    def __init__(self, docker: str | None = None) -> None:
        self._docker = docker
        self._containers: dict[str, str] = {}

    # -- preflight ---------------------------------------------------------
    def preflight(self, pname: str, env: dict, ctx: dict) -> tuple[dict | None, str | None]:
        """Establish that this process CAN be confined, and prove the boundary
        from inside it. Returns `(achieved, None)` or `(None, diagnostic)`;
        a diagnostic is a refusal, and the caller must not spawn anything."""
        backend = ctx.get("backend", "py")
        if backend != "py":
            return None, (
                f"process {pname!r} is placed in a `container` sandbox on the "
                f"{backend!r} backend, but the container rung's runtime driver "
                f"covers the `py` backend only in this slice (the other tiers "
                f"need their own in-image runner form). Move the process to the "
                f"`py` tier, or take it out of the sandbox.")
        # item 411 T1/T3: a cross-boundary seam builds the relay-mtls transport
        # descriptor. The plan-layer preconditions (item-56 role, item-54
        # deadline) refuse HERE naming each unmet one; REACHABILITY is no longer
        # a plan-layer refusal — T3's relay + per-process network establish it,
        # and the seam-only canary (run in `_probe` below when `seam_network` is
        # set) confirms it from inside. A seam-free process carries no seams and
        # this block is inert (byte-identical to a seam-free boot).
        seams = ctx.get("seams") or []
        if seams:
            desc = seam_transport_descriptor(pname, seams)
            if desc["unmet"]:
                bullets = "".join(f"\n  - {u}" for u in desc["unmet"])
                return None, (
                    f"process {pname!r} is placed in a `container` sandbox with "
                    f"cross-boundary seam(s); the {desc['transport']} transport "
                    f"cannot be admitted until every precondition is met. "
                    f"Unmet:{bullets}\n"
                    f"Place {pname!r}'s component(s) with the process they talk "
                    f"to, run them unsandboxed, or fix each precondition above.")

        docker = self._resolve_docker()
        if docker is None:
            return None, (
                f"process {pname!r} declares the `container` isolation rung, but "
                f"no container runtime is on PATH (`docker`). The sandbox cannot "
                f"be established, and a declared isolation is never downgraded to "
                f"an unconfined process: install a container runtime, or remove "
                f"the `[processes.{pname}.sandbox]` isolation from the placement.")

        rc, out, err = _run([docker, "version", "--format", "{{.Server.Version}}"])
        if rc != 0:
            return None, (
                f"process {pname!r} declares the `container` isolation rung, but "
                f"the container runtime is not usable: `{Path(docker).name} version` "
                f"failed ({_tail(err) or _tail(out)}). Is the daemon running? The "
                f"sandbox cannot be established, so the placement refuses rather "
                f"than running {pname!r} unconfined.")
        server = (out or "").strip() or "unknown"

        image = env.get("image")
        image_err = self._ensure_image(docker, pname, str(image))
        if image_err:
            return None, image_err

        # PHASE 1 — the declared envelope alone: the placement directory (the
        # process reads its own spec there) plus the granted `fs` mounts. This
        # is the set the manifest is a claim ABOUT, so it is the set the canary
        # confirms first, and its RUNTIME line says whether the image can be the
        # runner on its own.
        cwd = ctx.get("cwd") or os.getcwd()
        # item 411 T3: a seam-carrying sandbox runs on its per-process seam-only
        # network with the relay up, and the canary discriminates a seam (open)
        # from egress (a dropped isolation target) instead of the net=none probe.
        seam_network = ctx.get("seam_network")
        canary = self._canary_for(env, ctx)
        # item 411 T2: the process's OWN spec and seam identity, not the whole
        # placement directory — a hostile sandbox must not read its siblings' keys.
        declared = seam_dir_mounts(ctx) + envelope_mounts(env)
        probe, probe_err = self._probe(docker, pname, env, str(image), declared,
                                       network=seam_network, canary=canary)
        if probe_err:
            return None, probe_err
        evidence, err = self._evaluate(pname, env, str(image), declared, probe)
        if err:
            return None, err

        # the host paths the driver adds on the author's behalf: the sources the
        # process re-compiles, and (when the image is not a revl runner itself)
        # the conductor's own runtime. All read-only, all reported.
        host_mounts = source_mounts(ctx.get("files") or [], cwd)
        pythonpath: str | None = None
        if probe.get("RUNTIME") != "image":
            runtime, rt_err = _runtime_mounts()
            if rt_err:
                return None, (
                    f"process {pname!r}: the image {image!r} does not carry the "
                    f"revl runner (no importable `revl`+`cordis` under its "
                    f"`python3`), and {rt_err}. Use an image that carries the "
                    f"runtime, or install cordis-py for this conductor "
                    f"(sh backends/python/setup.sh).")
            host_mounts += runtime
            pythonpath = os.pathsep.join(p for p, _ in runtime)

        # PHASE 2 — the FINAL configuration. The process does not run under the
        # set phase 1 confirmed, it runs under that set plus the host mounts, so
        # the canary is run again over exactly what will be launched: the same
        # envelope clauses re-confirmed, and the runtime made to IMPORT under the
        # image's own interpreter. The second run is what turns "the image is
        # missing a cordis dependency" into a refusal at preflight rather than a
        # child dying moments after the conductor announced the boundary.
        final = declared + host_mounts
        probe2, probe_err = self._probe(docker, pname, env, str(image), final,
                                        pythonpath=pythonpath,
                                        network=seam_network, canary=canary)
        if probe_err:
            return None, probe_err
        evidence, err = self._evaluate(pname, env, str(image), final, probe2)
        if err:
            return None, err
        if probe2.get("RUNTIME") != "image":
            return None, (
                f"process {pname!r}: the revl runner does not import inside "
                f"{image!r} even with the conductor's own runtime mounted "
                f"read-only ({probe2.get('RUNTIME_ERR') or 'no detail'}). The "
                f"runner's third-party dependencies come from the IMAGE (cordis-py "
                f"needs pyyaml and watchdog); pin an image that has them, or one "
                f"that carries revl + cordis outright. The placement refuses "
                f"rather than announcing a boundary and then dying inside it.")

        return {
            "rung": self.rung,
            "runtime": f"{Path(docker).name} server {server}",
            "enforced": True,
            "image": image,
            "platform": env.get("platform"),
            "workdir": cwd,
            "mounts": final,
            "host_mounts": host_mounts,
            "pythonpath": pythonpath,
            "seam_network": seam_network,
            "evidence": evidence,
        }, None

    def _resolve_docker(self) -> str | None:
        if self._docker is None:
            self._docker = shutil.which("docker") or ""
        return self._docker or None

    def _ensure_image(self, docker: str, pname: str, image: str) -> str | None:
        """The image is trusted input at the level of the placement file, but it
        must EXIST before anything is launched: an image resolved lazily by `run`
        turns a missing image into a dead child instead of a refusal."""
        rc, _, _ = _run([docker, "image", "inspect", image])
        if rc == 0:
            return None
        rc, _, err = _run([docker, "pull", image])
        if rc != 0:
            return (
                f"process {pname!r}: the sandbox image {image!r} is neither "
                f"present locally nor pullable ({_tail(err)}). The isolation "
                f"cannot be established, so the placement refuses; pin an image "
                f"that exists (by digest), or pull it first.")
        return None

    def _canary_for(self, env: dict, ctx: dict) -> str:
        """The canary script this process gets: the seam-only variant when it
        runs on a per-process seam network (item 411 T3), else the pre-T3
        net=none/all script. Split out so the choice is testable without a
        daemon."""
        if ctx.get("seam_network"):
            return seam_canary_script(ctx.get("seam_canary_targets") or [],
                                      ctx.get("seam_isolation_target"))
        return _canary_script(env.get("net", "none"))

    def _probe(self, docker: str, pname: str, env: dict, image: str,
               mounts: list[tuple[str, str]],
               pythonpath: str | None = None,
               network: str | None = None,
               canary: str | None = None) -> tuple[dict, str | None]:
        """Run the boot canary INSIDE the boundary, with the exact confinement
        flags a launch with `mounts` would get, and return its report parsed
        into fields — or a diagnostic when it could not run at all."""
        name = f"revl-canary-{pname}-{secrets.token_hex(4)}"
        env_flags = ["-e", f"PYTHONPATH={pythonpath}"] if pythonpath else []
        script = canary if canary is not None else _canary_script(env.get("net", "none"))
        argv = ([docker, "run"]
                + container_flags(env, name=name, mounts=mounts,
                                  interactive=False, network=network)
                + env_flags
                + [image, "sh", "-c", script])
        rc, out, err = _run(argv, timeout=_CANARY_TIMEOUT)
        if rc != 0 or "CANARY=done" not in out:
            # the canary IS the platform preflight: it runs with the same
            # `--platform` the launch would, so a host that cannot run the
            # requested arch fails HERE, at plan time, not as a dead child. Name
            # the platform as the likely cause rather than leaving a bare runtime
            # error (411 design: "one diagnostic, not a spawn failure").
            hint = ""
            if env.get("platform"):
                hint = (f" This process requests platform {env['platform']!r}; if "
                        f"this host cannot run that architecture (no qemu/binfmt "
                        f"or Rosetta emulation, and no native node), that is the "
                        f"likely cause — install the platform's emulation, or "
                        f"place the process on a host of its arch.")
            return {}, (
                f"process {pname!r}: the in-sandbox boot canary did not run in "
                f"{image!r} ({_tail(err) or _tail(out)}). The canary is a POSIX "
                f"`sh` one-shot reading /proc from inside the boundary; an image "
                f"without `sh` cannot be verified, and an unverified boundary is "
                f"refused rather than trusted." + hint)
        report: dict = {"MOUNTS": {}}
        for line in out.splitlines():
            key, _, value = line.partition("=")
            if key == "MOUNT":
                mp, _, opts = value.partition(" ")
                report["MOUNTS"][mp] = opts
            elif key:
                report[key] = value
        return report, None

    def _evaluate(self, pname: str, env: dict, image: str,
                  mounts: list[tuple[str, str]],
                  report: dict) -> tuple[list[str], str | None]:
        """Judge one canary report against the envelope it claims to enforce.

        Every clause is a refusal when it cannot be CONFIRMED, not merely when
        it is contradicted: an unconfirmed boundary and a broken one are the
        same thing to a composition that is about to trust it.
        """
        mount_opts: dict = report.get("MOUNTS") or {}
        lines: list[str] = []
        if report.get("PY") != "yes":
            return [], (
                f"process {pname!r}: the sandbox image {image!r} has no `python3`. "
                f"The container rung runs the py runner inside the boundary and "
                f"verifies the envelope with a probe that needs it, so an image "
                f"without one cannot be confirmed and is refused rather than "
                f"trusted.")

        if "SEAM" in report:
            # item 411 T3: a seam-carrying sandbox on its per-process network.
            # The discriminating pair plus the DNS closures replace the net=none
            # egress clause; any clause that cannot be CONFIRMED is a refusal.
            seam_err = self._evaluate_seam(pname, env, report, lines)
            if seam_err:
                return [], seam_err
        elif env.get("net", "none") == "none":
            routes = report.get("ROUTES", "?")
            egress = report.get("EGRESS", "unreported")
            if routes != "0":
                return [], (
                    f"process {pname!r}: the sandbox asked for net = \"none\", but "
                    f"the boot canary sees {routes} route(s) in the network "
                    f"namespace inside the boundary. The confinement did not take, "
                    f"and a sandbox that cannot confirm its own envelope is "
                    f"refused, never silently downgraded.")
            if not egress.startswith("blocked:"):
                return [], (
                    f"process {pname!r}: the sandbox asked for net = \"none\", but "
                    f"the in-sandbox egress probe reports {egress!r} rather than an "
                    f"immediate refusal. The boundary's own network confinement is "
                    f"unconfirmed, and an unconfirmed boundary is refused exactly "
                    f"like a broken one.")
            lines.append(f"net=none confirmed in-sandbox: no route in the namespace, "
                         f"an outbound connect fails at once (errno "
                         f"{egress.split(':', 1)[1]})")
        else:
            lines.append("net=all: egress permitted by the envelope (nothing confined)")

        rootfs = report.get("ROOTFS", "unknown")
        if rootfs != "ro":
            return [], (
                f"process {pname!r}: the boot canary wrote to the container's root "
                f"filesystem (ROOTFS={rootfs}), so `--read-only` did not take. The "
                f"boundary is not the one the placement asked for and is refused.")
        lines.append("root filesystem read-only, confirmed in-sandbox")

        for path, mode in mounts:
            opts = mount_opts.get(path)
            if opts is None:
                return [], (
                    f"process {pname!r}: the boot canary does not see the mount "
                    f"{path!r} inside the boundary (it is not in /proc/mounts). The "
                    f"envelope the process would run under is not the one declared, "
                    f"so the placement refuses.")
            seen = "rw" if opts.split(",")[0] == "rw" else "ro"
            if seen != mode:
                return [], (
                    f"process {pname!r}: the mount {path!r} is {seen} inside the "
                    f"boundary but the envelope declares {mode}. The envelope did "
                    f"not take, so the placement refuses rather than running under "
                    f"a mount set nobody declared.")
            lines.append(f"mount {path} {mode}, confirmed in-sandbox")

        platform = env.get("platform")
        if platform:
            want = accepted_uname(platform) or ()
            seen = report.get("ARCH", "unknown")
            if seen not in want:
                return [], (
                    f"process {pname!r}: the sandbox asked for platform "
                    f"{platform!r}, but the boot canary reports `uname -m` = "
                    f"{seen!r} inside the boundary (expected one of "
                    f"{', '.join(want) or '?'}). The container did not run under "
                    f"the requested architecture — the runtime applied no "
                    f"emulation for it — and an unconfirmed platform is refused "
                    f"like any boundary that did not take, never silently run as "
                    f"the host arch.")
            lines.append(f"platform {platform} confirmed in-sandbox (uname -m = {seen})")

        lines.append("all capabilities dropped, no-new-privileges")
        return lines, None

    def _evaluate_seam(self, pname: str, env: dict, report: dict,
                       lines: list[str]) -> str | None:
        """The item 411 T3 seam-only network clauses. `lines` is appended to on
        success; a returned string is a refusal. Under `net = "none"` all three
        clauses (SEAM open, ISOLATION confirmed, DNS closed) are enforced; under
        `net = "all"` the sandbox is on the bridge too and the posture is `all`,
        so only SEAM is confirmed (the isolation target IS reachable, and DNS is
        open, by construction — refusing on either would refuse a legitimate
        `net = "all"` manifest)."""
        seam = report.get("SEAM", "unreported")
        if seam.startswith("closed:"):
            return (
                f"process {pname!r}: a seam's relay listener did not accept a "
                f"connect from inside the sandbox ({seam}); the seam cannot cross "
                f"the boundary, and an unreachable seam is refused rather than the "
                f"process booting into a composition it cannot talk to.")
        if seam not in ("open", "none"):
            return (
                f"process {pname!r}: the in-sandbox seam probe reports {seam!r} "
                f"rather than `open`; an unconfirmed seam transport is refused "
                f"exactly like a broken one.")
        lines.append("seam transport confirmed in-sandbox: every relay listener "
                     "this process's seams use accepts (relay-mtls, item 411 T3)")

        if env.get("net", "none") == "all":
            lines.append("net=all: the sandbox is on the default bridge too; "
                         "posture is `all` and only the seam is confirmed")
            return None

        isolation = report.get("ISOLATION", "unreported")
        if isolation != "confirmed":
            return (
                f"process {pname!r}: the isolation probe reports {isolation!r}. A "
                f"target the relay proved open from its bridge side must NOT open "
                f"from inside the sandbox; that it does (or could not be judged) "
                f"means the seam-only network is not confining egress, and an "
                f"unconfirmed boundary is refused, never silently downgraded.")
        lines.append("isolation confirmed in-sandbox: a target open from the "
                     "relay's bridge vantage is dropped from the sandbox's")

        dns = report.get("DNS", "unreported")
        if dns != "closed":
            return (
                f"process {pname!r}: DNS is not closed inside the sandbox ({dns}). "
                f"An `--internal` network's resolver must forward nothing; a name "
                f"that resolves is a leak, so the placement refuses rather than "
                f"boot into a boundary whose DNS is open.")
        lines.append("DNS closed in-sandbox: no external name resolves")
        return None

    # -- launch ------------------------------------------------------------
    def wrap(self, pname: str, cmd: list, proc_env: dict | None,
             achieved: dict) -> tuple[list, dict | None]:
        """Rewrite one process's command so it runs INSIDE the boundary.

        The interpreter is the image's `python3` (the conductor's own
        `sys.executable` is a host path that does not exist on the other side);
        every other argument keeps its absolute host spelling, which resolves
        because the mounts are identity-mapped.
        """
        docker = self._resolve_docker()
        name = f"revl-sb-{pname}-{secrets.token_hex(4)}"
        self._containers[pname] = name
        inner = ["python3" if str(a) == sys.executable else str(a) for a in cmd]
        env_flags: list[str] = []
        if achieved.get("pythonpath"):
            env_flags += ["-e", f"PYTHONPATH={achieved['pythonpath']}"]
        argv = ([docker, "run"]
                + container_flags(achieved["_env"], name=name,
                                  mounts=achieved["mounts"],
                                  workdir=achieved.get("workdir"),
                                  network=achieved.get("seam_network"))
                + env_flags + [str(achieved["image"])] + inner)
        # the child's environment is the CONDUCTOR's process environment for
        # the `docker` CLI only; nothing from it crosses into the boundary
        # except the `-e` flags above, which is the point: the host env is not
        # part of the declared envelope (fs and net are).
        return argv, None

    # -- teardown ----------------------------------------------------------
    def teardown(self, pname: str | None = None) -> None:
        """Best-effort removal. `--rm` already fires on a clean exit; this is
        the belt for a conductor killed mid-run, so a placement never leaves a
        confined process behind it."""
        docker = self._resolve_docker()
        if docker is None:  # pragma: no cover - nothing was ever started
            return
        names = ([self._containers[pname]] if pname and pname in self._containers
                 else list(self._containers.values()))
        for name in names:
            _run([docker, "rm", "-f", name], timeout=30.0)
        if pname is None:
            self._containers.clear()


# --------------------------------------------------------------------------
# the wasm-cell rung driver
# --------------------------------------------------------------------------
#
# The weakest rung of the ladder, and the one that is NOT an OS boundary: the
# cell is a wasm instance living inside an ordinary py placement process, so its
# confinement is a generated import set, not a `--network=none` / `--read-only`
# envelope. The 411 design says so plainly — the `fs`/`net` keys bind nothing a
# wasm instance could use, which is why a non-default value under `wasm-cell` is
# already refused at plan time (`placement._normalize_sandbox_table`), the `*`
# opaque reach is refused (`placement._sandbox_capability_gate`), and a @py/@ts
# host body cannot enter a cell at all (the wasm emitter is the oracle). None of
# that is this driver's job; by the time a process reaches here its manifest has
# already been judged cell-eligible.
#
# What IS this driver's job, and what is not
# ------------------------------------------
# Same three responsibilities as `ContainerDriver`: establish the boundary,
# CONFIRM it from inside, and tear it down — under the same "refuse, never
# degrade" law. Two things make the shape different from the container rung:
#
#   * The substrate is out-of-conductor. The cordis-wasm runtime (item 335) and
#     its `wasmtime` bindings live in their own checkout with their own venv;
#     the conductor's interpreter does not carry `wasmtime`. So the cell is
#     established and probed through that venv's interpreter, exactly as
#     `run_wasm` boots a whole wasm composition — and the availability gate is
#     the same `run_wasm.wasm_runtime_reason()` the wasm TIER already uses, so
#     this rung introduces no new environment switch (item 445) and skips
#     wherever the wasm tier already skips.
#   * Hosting a PLACEMENT COMPONENT inside the cell — instantiating its emitted
#     wasm module with imports generated from the grant (the seam proxies plus
#     the granted host functions), so the "seam crossing" is import
#     satisfaction — is the py runner's cell mode, item 411 Stage 4. That code
#     is not built in this slice: `_process_runner` has no cell path. Until it
#     lands, this driver ESTABLISHES and VERIFIES the cell substrate (the launch
#     + health half) but REFUSES to boot a component in it, because a cell that
#     silently fell back to an ordinary in-process body would be the exact
#     silent downgrade the whole module exists to forbid — worse here than
#     elsewhere, since an in-process body shares the conductor's address space.
#     The refusal names Stage 4 as the one remaining step and points at the
#     container rung, the same way the container rung's own cross-boundary seam
#     refuses per-precondition pending T3.
#
# The in-cell health canary
# --------------------------
# The wasm analog of the container boot canary, and just as ACTIVE. It boots two
# probe modules inside a fresh cell and reads what the instantiator reports, not
# what was asked for:
#
#   IMPORTS=<n>          the empty module's import count (0 == zero ambient authority)
#   AMBIENT=blocked:<e>  a module DECLARING a host import fails to instantiate
#                        against an EMPTY import set — the cell's defining
#                        property (item 289: an ungranted reach is a missing
#                        import at instantiation, never a call-time surprise)
#   AMBIENT=linked       that module instantiated anyway -> the cell grants
#                        ambient authority -> the boundary did not take -> refuse
#   WTVERSION=<v>        the wasmtime version that established the cell
#
# The AMBIENT clause is the point: a passive "the module has no imports" reading
# proves nothing, the same way an interface list proved nothing for the
# container's net probe. What distinguishes a cell from an ordinary instance is
# that an UNGRANTED import cannot be satisfied, so the canary tries to
# instantiate one and demands the failure. A cell that links it is refused like
# any boundary that did not take.
_CELL_PROBE_PY = r"""
import sys
try:
    import wasmtime
except Exception as exc:  # noqa: BLE001
    print("WTIMPORT=absent:%s" % (type(exc).__name__,)); print("CELL=done"); sys.exit(0)
eng = wasmtime.Engine()
store = wasmtime.Store(eng)
empty = wasmtime.Module(eng, "(module)")
wasmtime.Instance(store, empty, [])
print("IMPORTS=%d" % (len(empty.imports),))
needs_host = wasmtime.Module(eng, '(module (import "revl:host" "ambient" (func)))')
try:
    wasmtime.Instance(store, needs_host, [])
    print("AMBIENT=linked")
except Exception as exc:  # noqa: BLE001 - a link/trap failure is the confinement
    print("AMBIENT=blocked:%s" % (type(exc).__name__,))
print("WTVERSION=%s" % (getattr(wasmtime, "__version__", "?"),))
print("CELL=done")
"""

# The cell canary imports nothing but wasmtime and boots two tiny modules, so it
# either answers at once or the substrate is not usable.
_CELL_TIMEOUT = 60.0


def evaluate_cell(pname: str, report: dict) -> tuple[list[str], str | None]:
    """Judge one in-cell canary report against what a cell must be, PURELY.

    Every clause is a refusal when it cannot be CONFIRMED, not merely when it is
    contradicted — an unconfirmed cell and a broken one are the same thing to a
    composition about to trust a body inside the conductor's own process. Kept
    free of any runtime call so the whole judgment is testable with synthetic
    reports wherever the substrate is absent, exactly as `ContainerDriver.
    _evaluate` is."""
    if report.get("WTIMPORT", "").startswith("absent"):
        return [], (
            f"process {pname!r}: the wasm-cell substrate interpreter could not "
            f"import `wasmtime` ({report['WTIMPORT']}). The cell is a wasm "
            f"instance under wasmtime; without the bindings the boundary cannot "
            f"be established, and an unestablished boundary is refused, never "
            f"downgraded to an ordinary in-process body.")
    if "CELL" not in report:
        return [], (
            f"process {pname!r}: the in-cell boot canary did not complete. An "
            f"unverified cell is refused rather than trusted.")
    imports = report.get("IMPORTS")
    if imports != "0":
        return [], (
            f"process {pname!r}: the empty cell module reports {imports!r} "
            f"import(s) rather than 0. A cell must grant zero ambient authority, "
            f"so a boundary that starts with any is refused.")
    ambient = report.get("AMBIENT", "unreported")
    if ambient == "linked":
        return [], (
            f"process {pname!r}: a module declaring an ungranted host import was "
            f"instantiated inside the cell anyway, so the cell satisfies ambient "
            f"authority. The cell's defining confinement — an ungranted reach is "
            f"a missing import at instantiation (item 289) — did not hold, and an "
            f"unconfirmed boundary is refused exactly like a broken one.")
    if not ambient.startswith("blocked:"):
        return [], (
            f"process {pname!r}: the in-cell ambient-authority probe reports "
            f"{ambient!r} rather than a link failure. The cell's confinement is "
            f"unconfirmed, and an unconfirmed boundary is refused.")
    version = report.get("WTVERSION", "?")
    return [
        f"empty cell instantiates with 0 imports (zero ambient authority), "
        f"confirmed in-cell under wasmtime {version}",
        f"an ungranted host import fails at instantiation "
        f"({ambient.split(':', 1)[1]}), confirmed in-cell — item 289's chain",
    ], None


class WasmCellDriver:
    """The `wasm-cell` rung: establish + in-cell canary + teardown.

    One instance per placement run. It establishes the cell substrate through
    the cordis-wasm venv's interpreter and verifies the cell's confinement from
    inside, then REFUSES to host a placement component (the py runner's cell mode
    is item 411 Stage 4, not built) rather than boot it unconfined.

    `runtime_reason` and `probe` are injectable so the whole driver is testable
    at the plan layer with the substrate absent; both default to the real
    cordis-wasm interpreter path the wasm tier uses.
    """

    rung = "wasm-cell"
    name = "wasmtime"

    def __init__(self, runtime_reason=None, probe=None) -> None:
        self._runtime_reason = runtime_reason
        self._probe = probe
        self._cells: dict[str, str] = {}

    # -- preflight ---------------------------------------------------------
    def preflight(self, pname: str, env: dict, ctx: dict) -> tuple[dict | None, str | None]:
        """Establish the cell substrate and prove its confinement from inside,
        then refuse the launch naming the one unbuilt step. Returns
        `(None, diagnostic)` in this slice on every path — a refusal — because a
        confirmed cell substrate is not yet a hosted component, and a cell that
        cannot host is refused rather than downgraded."""
        backend = ctx.get("backend", "py")
        if backend != "py":
            return None, (
                f"process {pname!r} is placed in a `wasm-cell` sandbox on the "
                f"{backend!r} backend, but a cell is hosted inside a py placement "
                f"process (the isolation is wasm; the tier is py). Move the "
                f"process to the `py` tier, or take it out of the sandbox.")
        # Defensive: the cell takes no OS envelope, and a non-default fs/net was
        # already refused where it entered. If one reached here the plan layer is
        # out of step with this driver, which is a refusal, not a silent accept.
        if env.get("fs") or env.get("net", "none") != "none":
            return None, (
                f"process {pname!r}: a `wasm-cell` grants nothing through an "
                f"fs/net OS envelope (its confinement is a generated import set), "
                f"but this process reached the driver with a non-default fs/net. "
                f"The placement refuses rather than pretend to enforce it.")

        reason = self._resolve_runtime_reason()
        if reason is not None:
            return None, (
                f"process {pname!r} declares the `wasm-cell` isolation rung, but "
                f"the wasm substrate is not available: {reason}. The cell is a "
                f"wasm instance under wasmtime, so it cannot be established here, "
                f"and a declared isolation is never downgraded to an unconfined "
                f"process — the placement refuses. Provide the cordis-wasm "
                f"runtime (the same one the wasm tier needs), use the `container` "
                f"rung, or take the process out of the sandbox.")

        report, probe_err = self._run_probe(pname)
        if probe_err:
            return None, probe_err
        evidence, err = evaluate_cell(pname, report)
        if err:
            return None, err

        # The substrate is real and confirmed. What is missing is the py runner's
        # cell mode (item 411 Stage 4): instantiating THIS component's emitted
        # wasm module with the import set generated from its grant. Refuse naming
        # it, and carry the verified-substrate evidence so the progress is
        # auditable rather than swallowed by the refusal.
        established = "".join(f"\n  - {line}" for line in evidence)
        return None, (
            f"process {pname!r}: the `wasm-cell` substrate is established and "
            f"verified in-cell:{established}\n"
            f"But hosting a placement component inside the cell needs the py "
            f"runner's cell mode — instantiating the component's emitted wasm "
            f"module with the seam-forwarding import set generated from its grant "
            f"— which is item 411 Stage 4 and not built in this slice "
            f"(`_process_runner` has no cell path yet). Until it lands, the "
            f"wasm-cell rung refuses rather than booting {pname!r} as an ordinary "
            f"in-process body that nothing confines. Use the `container` rung, "
            f"which establishes an OS boundary today, or take the process out of "
            f"the sandbox.")

    def _resolve_runtime_reason(self) -> str | None:
        if self._runtime_reason is not None:
            return self._runtime_reason()
        from .run_wasm import wasm_runtime_reason  # noqa: PLC0415 - lazy, avoids a cycle
        return wasm_runtime_reason()

    def _cordis_python(self) -> str | None:
        from .run_wasm import _cordis_wasm_python  # noqa: PLC0415
        return _cordis_wasm_python()

    def _run_probe(self, pname: str) -> tuple[dict, str | None]:
        """Boot the in-cell canary through the substrate interpreter and parse
        its report — or a diagnostic when it could not run at all."""
        if self._probe is not None:
            return self._probe(pname)
        python = self._cordis_python()
        if python is None:  # pragma: no cover - runtime_reason already refused this
            return {}, (f"process {pname!r}: no cordis-wasm interpreter to boot "
                        f"the in-cell canary.")
        rc, out, err = _run([python, "-c", _CELL_PROBE_PY], timeout=_CELL_TIMEOUT)
        if rc != 0 or "CELL=done" not in out:
            return {}, (
                f"process {pname!r}: the in-cell boot canary did not run under "
                f"the wasm substrate ({_tail(err) or _tail(out)}). An unverified "
                f"cell is refused rather than trusted.")
        report: dict = {}
        for line in out.splitlines():
            key, _, value = line.partition("=")
            if key:
                report[key] = value
        return report, None

    # -- launch ------------------------------------------------------------
    def wrap(self, pname: str, cmd: list, proc_env: dict | None,
             achieved: dict) -> tuple[list, dict | None]:  # pragma: no cover
        """Unreachable in this slice: `preflight` refuses every wasm-cell
        placement before a command is ever built, so a wrapped launch cannot be
        reached. Kept to satisfy the driver contract, and to fail LOUDLY rather
        than pass a command through unconfined if the refusal above is ever
        weakened without landing the runner's cell mode."""
        raise AssertionError(
            f"wasm-cell launch reached for {pname!r} without the py runner's "
            f"cell mode (item 411 Stage 4); preflight must have refused. Booting "
            f"the component here would run it unconfined in-process.")

    # -- teardown ----------------------------------------------------------
    def teardown(self, pname: str | None = None) -> None:
        """Best-effort. A cell is an in-instance under a short-lived probe
        interpreter with no host resource to reclaim in this slice; teardown
        drops the driver's own bookkeeping so a conductor killed mid-run leaves
        nothing dangling. When Stage 4 lands the hosting instance's dispose goes
        here, mirroring `ContainerDriver.teardown`'s belt."""
        if pname is not None:
            self._cells.pop(pname, None)
        else:
            self._cells.clear()


# --------------------------------------------------------------------------
# the microVM rung driver (item 411 T6)
# --------------------------------------------------------------------------
#
# The strongest rung of the ladder: the confined body runs under its OWN kernel
# inside a hardware-accelerated virtual machine, not merely in a namespace on the
# host kernel the way the container rung does. That is the whole security
# difference — a container escape is a shared-kernel bug, a microVM escape is a
# hypervisor bug — and it is why the design names this rung for "code you assume
# is actively escaping" and multi-tenant isolation.
#
# What this driver establishes, and what is still a named follow-on
# -----------------------------------------------------------------
# Same three responsibilities as `ContainerDriver`: establish the boundary,
# CONFIRM it from inside with a boot canary, tear it down — under the same
# "refuse, never degrade" law. Two things shape it differently:
#
#   * The accelerator is not portable. A microVM is a KVM guest, so it needs
#     `/dev/kvm`; the design (and this module's header, the 430/445 lesson) is
#     blunt that neither the CI runners nor a developer laptop reliably has it.
#     So the availability gate is `microvm_runtime_reason()`, the sibling of the
#     wasm tier's `run_wasm.wasm_runtime_reason()` and the container rung's
#     `docker version` probe: when it names a reason, the rung REFUSES with that
#     reason (the clean named gap the design says this rung stays until a
#     KVM-capable lane verifies it), never a container silently substituted and
#     never a slow full-emulation TCG guest passed off as the declared boundary.
#   * A microVM has no image registry to pull a guest from and no shared
#     filesystem by default (411 design). Its guest kernel and root image are
#     the runner's to provide (`REVL_MICROVM_KERNEL` / `REVL_MICROVM_ROOTFS`),
#     fs grants cross as virtio-9p shares, and the seam crosses the VM's virtio
#     NIC as item-56 TCP+mTLS over a HOST-side relay (T6 reuses the T3 table and
#     canary; the relay's host mode is `revl.seam_relay`).
#
# The live boundary this driver boots — a KVM microVM whose in-guest canary
# confirms the arch, the read-only root, the net posture and the 9p mounts from
# inside — is COMPLETE in code here but has never been EXECUTED, because nothing
# in reach carries `/dev/kvm`. Verifying that boot on a KVM-capable lane is the
# last requirement to close item 411's microVM rung (the CI job
# `sandbox-microvm`, gated on a self-hosted KVM runner). Hosting the placement
# COMPONENT inside the confirmed VM — the in-guest py runner over a 9p root and
# the host-mode seam relay — is the step after that, so until it lands this
# driver, like `WasmCellDriver`, VERIFIES the boundary and then REFUSES to boot
# the component in it rather than downgrade, carrying the in-VM evidence so the
# progress is auditable rather than swallowed by the refusal.

# A VM boot, even a microVM's, is slower than a container start; give the canary
# room before a hang becomes a refusal.
_MICROVM_TIMEOUT = 180.0
_KVM_DEVICE = "/dev/kvm"


def _microvm_monitor(override: str | None = None) -> tuple[str, str] | None:
    """`(kind, path)` for the VM monitor, or None when none resolves.

    `qemu-system-<host arch>` with the `microvm` machine type is the one
    concrete monitor wired here: it is CLI-driven (a reviewable argv, no
    out-of-band API socket or JSON config), it carries virtio-9p in-tree for the
    fs-grant shares, and `accel=kvm` is the same accelerator the reason gate
    proved. `REVL_MICROVM_MONITOR` overrides the path — a firecracker or
    cloud-hypervisor monitor is a legitimate follow-on and rides that override
    once its config form is wired."""
    if override:
        path = shutil.which(override) or (override if os.path.exists(override) else None)
        return ("qemu", path) if path else None
    try:
        machine = os.uname().machine
    except AttributeError:  # pragma: no cover - non-POSIX host
        return None
    qemu = {"x86_64": "qemu-system-x86_64",
            "aarch64": "qemu-system-aarch64",
            "arm64": "qemu-system-aarch64"}.get(machine)
    if not qemu:
        return None
    path = shutil.which(qemu)
    return ("qemu", path) if path else None


def microvm_runtime_reason(kvm_device: str = _KVM_DEVICE,
                           environ: dict | None = None) -> str | None:
    """None when a microVM can actually be booted here, else WHY it cannot.

    The availability gate for the strongest rung, and the direct sibling of
    `run_wasm.wasm_runtime_reason`. Three things must hold, checked in order so
    the diagnostic names ONE fix rather than a list; the first missing one is
    the reason, and any reason is a refusal (never a downgrade):

      1. `/dev/kvm` is present and this process can open it read-write. Without
         the accelerator a "microVM" is a full-emulation TCG guest, a DIFFERENT
         and weaker boundary than the one the manifest declared, so the rung
         refuses rather than boot a weaker VM under the same word. microVMs are
         Linux + `/dev/kvm` only, by nature (411 seam-transport design, T6).
      2. a VM monitor resolves (`_microvm_monitor`).
      3. the guest kernel and root image are configured and present. A microVM
         has no image registry to pull from the way the container rung does, so
         the boot assets are the runner's to provide; their absence is a refusal
         that NAMES them, exactly as a missing container image is.
    """
    env = environ if environ is not None else os.environ
    if not os.path.exists(kvm_device):
        return (f"{kvm_device} is not present, so no hardware-accelerated VM can "
                f"boot here. A microVM is a KVM guest; without the accelerator "
                f"the rung would fall back to full software emulation, which is a "
                f"weaker boundary than the one declared, so it refuses rather "
                f"than substitute it. The microVM rung is Linux + /dev/kvm only "
                f"(by nature, not omission); use the `container` rung on a host "
                f"without KVM, or take the process out of the sandbox.")
    if not os.access(kvm_device, os.R_OK | os.W_OK):
        return (f"{kvm_device} exists but is not readable+writable by this user, "
                f"so the hypervisor cannot open the accelerator. Add the user to "
                f"the `kvm` group (or grant access to the device), then re-run.")
    monitor = _microvm_monitor(env.get("REVL_MICROVM_MONITOR"))
    if monitor is None:
        return ("no VM monitor resolved: looked for a host-arch `qemu-system-*` "
                "with the `microvm` machine type on PATH, and REVL_MICROVM_MONITOR "
                "is unset. Install qemu, or set REVL_MICROVM_MONITOR to a monitor "
                "path.")
    kernel = env.get("REVL_MICROVM_KERNEL")
    rootfs = env.get("REVL_MICROVM_ROOTFS")
    unset = [n for n, v in (("REVL_MICROVM_KERNEL", kernel),
                            ("REVL_MICROVM_ROOTFS", rootfs)) if not v]
    if unset:
        return (f"the microVM boot assets are not configured ({', '.join(unset)} "
                f"unset). Unlike the container rung there is no image registry to "
                f"pull a guest from, so the guest kernel and root image are the "
                f"runner's to provide; point these at a kernel and a rootfs that "
                f"carry `sh` + `python3` (and 9p for fs grants).")
    for label, value in (("kernel", kernel), ("rootfs", rootfs)):
        if not os.path.exists(value):
            return (f"the microVM {label} configured at {value!r} does not exist.")
    return None


# The control-dir 9p tag: the driver writes the canary (and, at launch, the
# runner's control table) into a host directory shared into the guest under this
# tag, and the guest init mounts it and runs `canary.sh`, writing its report to
# the serial console the driver captures. This is the rootfs contract the
# runner-provided guest honours; it is the microVM analog of the container rung
# handing the canary as `sh -c <script>`.
_MICROVM_CTL_TAG = "revl-ctl"
_MICROVM_CTL_MNT = "/revl-ctl"


def microvm_mount_tag(index: int) -> str:
    """The 9p mount tag for the Nth fs-grant share. Deterministic and distinct
    from the control tag so the guest's mount table is unambiguous."""
    return f"revl-fs-{index}"


def microvm_vm_argv(monitor: str, *, kernel: str, rootfs: str, ctl_dir: str,
                    mounts: list[tuple[str, str]], memory_mib: int = 256,
                    net: str = "none") -> list[str]:
    """The exact `qemu-system` argv a microVM boot is started with, PURE.

    The microVM analog of `container_flags`: no monitor is invoked, so this is
    the half of the driver that is reviewable as a whole and testable without
    `/dev/kvm`. Every confinement property the rung claims is visible here —

      * `accel=kvm` (the reason gate proved the accelerator; a boot that could
        not get it fails loudly rather than emulating), a fresh guest kernel,
        and a READ-ONLY virtio root drive;
      * `net = "none"` derives NO `-netdev` at all, which is the T3 posture by
        construction (the guest has no NIC, so no route and no egress — the
        in-guest canary confirms it the same way the container rung does);
      * each fs grant crosses as its own virtio-9p share, read-only unless the
        grant is `rw`, mounted in-guest at its identity path;
      * the control dir carrying the canary is one more read-only 9p share;
      * the serial console is captured (`-serial stdio`, `-display none`) so the
        canary's report reaches the driver, and `-no-reboot` turns a guest panic
        into an exit rather than a boot loop.

    A `net = "all"` boot adds a user-mode NIC; the driver's own reason gate and
    the plan layer have already established there is no host allowlist to honour
    (the envelope is `none`/`all`)."""
    argv = [monitor, "-machine", "microvm,accel=kvm", "-cpu", "host",
            "-m", f"{memory_mib}M", "-no-reboot", "-display", "none",
            "-kernel", kernel,
            "-drive", f"file={rootfs},format=raw,if=virtio,readonly=on"]
    # the control share: canary in, report out over the serial console.
    argv += ["-fsdev",
             f"local,id=ctl,path={ctl_dir},security_model=none,readonly=on",
             "-device",
             f"virtio-9p-device,fsdev=ctl,mount_tag={_MICROVM_CTL_TAG}"]
    for i, (path, mode) in enumerate(mounts):
        ro = ",readonly=on" if mode != "rw" else ""
        argv += ["-fsdev",
                 f"local,id=fs{i},path={path},security_model=none{ro}",
                 "-device",
                 f"virtio-9p-device,fsdev=fs{i},mount_tag={microvm_mount_tag(i)}"]
    if net == "all":
        argv += ["-netdev", "user,id=net0", "-device", "virtio-net-device,netdev=net0"]
    # net == "none": no -netdev, so the guest has no NIC at all.
    # the guest init mounts the control share and runs the canary, streaming its
    # report to ttyS0, which is this stdio serial line.
    cmdline = (f"console=ttyS0 root=/dev/vda ro "
               f"revl.ctl_tag={_MICROVM_CTL_TAG} revl.ctl_mnt={_MICROVM_CTL_MNT}")
    argv += ["-append", cmdline, "-serial", "stdio"]
    return argv


def evaluate_microvm(pname: str, env: dict, mounts: list[tuple[str, str]],
                     report: dict) -> tuple[list[str], str | None]:
    """Judge one in-VM boot canary report against the envelope it claims to
    enforce, PURELY — kept free of any monitor call so the whole judgment is
    testable with synthetic reports wherever `/dev/kvm` is absent, exactly as
    `evaluate_cell` is.

    The guest runs the SAME POSIX-`sh` canary the container rung does (the
    design's "reuses the T3 canary"), so the report shape is identical and every
    clause is a refusal when it cannot be CONFIRMED, not merely when it is
    contradicted."""
    if "CANARY" not in report:
        return [], (
            f"process {pname!r}: the in-VM boot canary did not complete inside "
            f"the microVM. An unverified boundary is refused rather than trusted.")
    if report.get("PY") != "yes":
        return [], (
            f"process {pname!r}: the microVM guest image has no `python3`. The "
            f"rung runs the py runner inside the guest and verifies the envelope "
            f"with a probe that needs it, so a guest without one cannot be "
            f"confirmed and is refused rather than trusted.")
    lines: list[str] = []
    if env.get("net", "none") == "none":
        routes = report.get("ROUTES", "?")
        egress = report.get("EGRESS", "unreported")
        if routes != "0":
            return [], (
                f"process {pname!r}: the sandbox asked for net = \"none\", but the "
                f"in-VM canary sees {routes} route(s) inside the guest. The microVM "
                f"was given a NIC it should not have; the boundary is not the one "
                f"declared and is refused, never silently downgraded.")
        if not egress.startswith("blocked:"):
            return [], (
                f"process {pname!r}: the sandbox asked for net = \"none\", but the "
                f"in-VM egress probe reports {egress!r} rather than an immediate "
                f"refusal. The guest's network confinement is unconfirmed, and an "
                f"unconfirmed boundary is refused exactly like a broken one.")
        lines.append(f"net=none confirmed in-VM: no route in the guest, an outbound "
                     f"connect fails at once (errno {egress.split(':', 1)[1]})")
    else:
        lines.append("net=all: egress permitted by the envelope (nothing confined)")

    if report.get("ROOTFS", "unknown") != "ro":
        return [], (
            f"process {pname!r}: the in-VM canary wrote to the guest root "
            f"filesystem (ROOTFS={report.get('ROOTFS', 'unknown')}), so the "
            f"read-only virtio root did not take. The boundary is not the one the "
            f"placement asked for and is refused.")
    lines.append("guest root filesystem read-only, confirmed in-VM")

    mount_opts: dict = report.get("MOUNTS") or {}
    for i, (path, mode) in enumerate(mounts):
        opts = mount_opts.get(path)
        if opts is None:
            return [], (
                f"process {pname!r}: the in-VM canary does not see the 9p share "
                f"for {path!r} inside the guest (tag {microvm_mount_tag(i)}). The "
                f"envelope the process would run under is not the one declared, so "
                f"the placement refuses.")
        seen = "rw" if opts.split(",")[0] == "rw" else "ro"
        if seen != mode:
            return [], (
                f"process {pname!r}: the mount {path!r} is {seen} inside the guest "
                f"but the envelope declares {mode}. The envelope did not take, so "
                f"the placement refuses.")
        lines.append(f"mount {path} {mode}, confirmed in-VM (9p)")

    platform = env.get("platform")
    if platform:
        want = accepted_uname(platform) or ()
        seen = report.get("ARCH", "unknown")
        if seen not in want:
            return [], (
                f"process {pname!r}: the sandbox asked for platform {platform!r}, "
                f"but the in-VM canary reports `uname -m` = {seen!r} inside the "
                f"guest (expected one of {', '.join(want) or '?'}). A microVM boots "
                f"under one kernel/arch; a mismatch means the guest is not the "
                f"declared arch, and an unconfirmed platform is refused.")
        lines.append(f"platform {platform} confirmed in-VM (uname -m = {seen})")
    return lines, None


class MicroVMDriver:
    """The `microvm` rung: establish + in-VM health canary + teardown.

    One instance per placement run. It gates on `/dev/kvm` and a monitor via
    `microvm_runtime_reason` (refusing with the named gap wherever the
    accelerator is absent — the state on every host in reach today), and where
    KVM IS present it boots a microVM and CONFIRMS the boundary from inside with
    the boot canary. Hosting the placement component inside the confirmed VM (the
    in-guest py runner over a 9p root, the host-mode seam relay) is the step
    after the live boot is verified on a KVM lane, so — like `WasmCellDriver` —
    it verifies the boundary and then REFUSES to boot the component rather than
    downgrade, carrying the in-VM evidence.

    `runtime_reason`, `monitor` and `probe` are injectable so the whole driver
    is testable at the plan layer with `/dev/kvm` absent; all default to the
    real KVM path.
    """

    rung = "microvm"
    name = "qemu-microvm"

    def __init__(self, runtime_reason=None, monitor=None, probe=None) -> None:
        self._runtime_reason = runtime_reason
        self._monitor = monitor
        self._probe = probe
        self._vms: dict[str, str] = {}

    # -- preflight ---------------------------------------------------------
    def preflight(self, pname: str, env: dict, ctx: dict) -> tuple[dict | None, str | None]:
        """Establish that this process CAN be confined in a microVM and prove the
        boundary from inside it, then refuse the component launch naming the one
        step the live boot has yet to reach. Returns `(None, diagnostic)` on
        every path in this slice — a refusal — because a confirmed VM boundary is
        not yet a hosted component, and a VM that cannot host is refused rather
        than downgraded."""
        backend = ctx.get("backend", "py")
        if backend != "py":
            return None, (
                f"process {pname!r} is placed in a `microvm` sandbox on the "
                f"{backend!r} backend, but the microVM rung runs the `py` runner "
                f"inside the guest in this slice (the other tiers need their own "
                f"in-guest runner form). Move the process to the `py` tier, or "
                f"take it out of the sandbox.")
        # item 411 T1/T6: a cross-boundary seam builds the relay-mtls transport
        # descriptor; its plan-layer preconditions (item-56 role, item-54
        # deadline) refuse HERE naming each unmet one, exactly as the container
        # rung does — the design's "the item-56 per-role tier rule carries over".
        seams = ctx.get("seams") or []
        if seams:
            desc = seam_transport_descriptor(pname, seams)
            if desc["unmet"]:
                bullets = "".join(f"\n  - {u}" for u in desc["unmet"])
                return None, (
                    f"process {pname!r} is placed in a `microvm` sandbox with "
                    f"cross-boundary seam(s); the {desc['transport']} transport "
                    f"cannot be admitted until every precondition is met. "
                    f"Unmet:{bullets}\n"
                    f"Place {pname!r}'s component(s) with the process they talk "
                    f"to, run them unsandboxed, or fix each precondition above.")

        reason = self._resolve_runtime_reason()
        if reason is not None:
            return None, (
                f"process {pname!r} declares the `microvm` isolation rung, but a "
                f"microVM cannot be booted here: {reason} A declared isolation is "
                f"never downgraded to a container or to an unconfined process — "
                f"the placement refuses.")

        report, probe_err = self._run_probe(pname, env, ctx)
        if probe_err:
            return None, probe_err
        mounts = seam_dir_mounts(ctx) + envelope_mounts(env)
        evidence, err = evaluate_microvm(pname, env, mounts, report)
        if err:
            return None, err

        # The VM boundary is real and confirmed from inside. What is missing is
        # hosting THIS component in it: the in-guest py runner over the 9p root
        # and the host-mode seam relay (T6). Refuse naming it, and carry the
        # verified-boundary evidence so the progress is auditable rather than
        # swallowed by the refusal, mirroring `WasmCellDriver.preflight`.
        established = "".join(f"\n  - {line}" for line in evidence)
        return None, (
            f"process {pname!r}: the `microvm` boundary is established and verified "
            f"in-VM:{established}\n"
            f"But hosting a placement component inside the guest needs the in-guest "
            f"py runner over the 9p root and the host-mode seam relay (item 411 "
            f"T6, the step after the live boot is verified on a KVM lane), which "
            f"is not built in this slice. Until it lands, the microVM rung refuses "
            f"rather than booting {pname!r} unconfined. Use the `container` rung, "
            f"which hosts a py component today, or take the process out of the "
            f"sandbox.")

    def _resolve_runtime_reason(self) -> str | None:
        if self._runtime_reason is not None:
            return self._runtime_reason()
        return microvm_runtime_reason()

    def _resolve_monitor(self) -> str | None:
        if self._monitor is not None:
            return self._monitor
        resolved = _microvm_monitor(os.environ.get("REVL_MICROVM_MONITOR"))
        return resolved[1] if resolved else None

    def _run_probe(self, pname: str, env: dict, ctx: dict) -> tuple[dict, str | None]:
        """Boot the in-VM canary under the monitor and parse its serial report —
        or a diagnostic when it could not run at all. The canary is the same
        POSIX-`sh` one-shot the container rung uses, written into a control-dir
        9p share the guest init mounts and runs (`_MICROVM_CTL_TAG`)."""
        if self._probe is not None:
            return self._probe(pname, env, ctx)
        monitor = self._resolve_monitor()
        if monitor is None:  # pragma: no cover - runtime_reason already refused this
            return {}, (f"process {pname!r}: no VM monitor to boot the in-VM canary.")
        environ = os.environ
        ctl_dir = Path(ctx.get("seam_dir") or ".") / f"revl-microvm-{pname}"
        try:
            ctl_dir.mkdir(parents=True, exist_ok=True)
            (ctl_dir / "canary.sh").write_text(
                _canary_script(env.get("net", "none")), encoding="utf-8")
        except OSError as exc:  # pragma: no cover - a broken placement dir
            return {}, (f"process {pname!r}: could not stage the microVM canary "
                        f"({exc}).")
        mounts = seam_dir_mounts(ctx) + envelope_mounts(env)
        argv = microvm_vm_argv(
            monitor, kernel=environ["REVL_MICROVM_KERNEL"],
            rootfs=environ["REVL_MICROVM_ROOTFS"], ctl_dir=str(ctl_dir),
            mounts=mounts, net=env.get("net", "none"))
        rc, out, err = _run(argv, timeout=_MICROVM_TIMEOUT)
        if rc != 0 or "CANARY=done" not in out:
            return {}, (
                f"process {pname!r}: the in-VM boot canary did not run under the "
                f"microVM monitor ({_tail(err) or _tail(out)}). The canary is a "
                f"POSIX `sh` one-shot reading /proc from inside the guest; a guest "
                f"that cannot run it cannot be verified, and an unverified boundary "
                f"is refused rather than trusted.")
        report: dict = {"MOUNTS": {}}
        for line in out.splitlines():
            key, _, value = line.partition("=")
            if key == "MOUNT":
                mp, _, opts = value.partition(" ")
                report["MOUNTS"][mp] = opts
            elif key:
                report[key] = value
        return report, None

    # -- launch ------------------------------------------------------------
    def wrap(self, pname: str, cmd: list, proc_env: dict | None,
             achieved: dict) -> tuple[list, dict | None]:  # pragma: no cover
        """Unreachable in this slice: `preflight` refuses every microVM placement
        before a command is ever built. Kept to satisfy the driver contract and
        to fail LOUDLY rather than pass a command through unconfined if the
        refusal above is ever weakened without landing the in-guest runner."""
        raise AssertionError(
            f"microVM launch reached for {pname!r} without the in-guest py runner "
            f"(item 411 T6); preflight must have refused. Booting the component "
            f"here would run it without the confinement the rung promises.")

    # -- teardown ----------------------------------------------------------
    def teardown(self, pname: str | None = None) -> None:
        """Best-effort. The canary VM is a transient `-no-reboot` monitor child
        that exits on its own; teardown drops the driver's bookkeeping so a
        conductor killed mid-run leaves nothing tracked. When the in-guest runner
        lands, the long-lived monitor child's kill goes here, mirroring
        `ContainerDriver.teardown`'s belt."""
        if pname is not None:
            self._vms.pop(pname, None)
        else:
            self._vms.clear()


def resolve_driver(rung: str) -> ContainerDriver | WasmCellDriver | MicroVMDriver | None:
    """The runtime driver for one isolation rung, or None when the rung has no
    driver at all. The caller REFUSES on None: a declared isolation with no
    driver must never fall through to an unconfined process.

    Every rung on the ladder now resolves to a driver — `container` (the OS
    boundary), `wasm-cell` (the in-process cell substrate) and `microvm` (the
    KVM guest). None of them ever downgrades: a rung whose boundary cannot be
    established here refuses with the named gap (the microVM rung on any host
    without `/dev/kvm`, which is every host in reach today)."""
    if rung == ContainerDriver.rung:
        return ContainerDriver()
    if rung == WasmCellDriver.rung:
        return WasmCellDriver()
    if rung == MicroVMDriver.rung:
        return MicroVMDriver()
    return None
