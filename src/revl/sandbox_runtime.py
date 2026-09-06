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

What is NOT in this slice
-------------------------
* The per-rung SEAM TRANSPORT. The 363 seam is a Unix socket in the placement
  directory. Crossing a container boundary with it is a bind mount, which does
  not carry a Unix socket on every host (verified non-functional in both
  directions over a Docker Desktop bind mount on macOS: the container binds the
  socket and a host connect gets ECONNREFUSED). So a container-sandboxed
  process that has a cross-boundary seam is REFUSED here rather than launched
  into a boundary its seam cannot cross. The transport variant (TCP+mTLS over
  item 56's network seam, or a shared netns) is the next sub-slice.
* The conductor-served approval-across-boundary channel.
* The `wasm-cell` and `microvm` rungs (`resolve_driver` returns None, and the
  caller refuses).
* Non-`py` backends inside a container.
"""

from __future__ import annotations

import importlib.util
import os
import secrets
import shutil
import subprocess
import sys
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
                    interactive: bool = True, workdir: str | None = None) -> list[str]:
    """The confinement flags one envelope derives, as a list, deterministically.

    Pure: no runtime is consulted, so this is the half of the driver that is
    testable everywhere and reviewable as a whole. `mounts` is the ALREADY
    resolved mount list (path, mode) — the declared `fs` grants plus the
    placement directory and, when the image does not carry it, the host
    runtime; every one of them is reported in the achieved record, so nothing
    the driver adds on the author's behalf is invisible.

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
    if env.get("net", "none") == "none":
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
        seam_keys = sorted(ctx.get("seam_keys") or [])
        if seam_keys:
            return None, (
                f"process {pname!r} is placed in a `container` sandbox but has "
                f"cross-boundary seam(s) ({', '.join(seam_keys)}): the 363 seam "
                f"is a Unix socket in the placement directory, and a Unix socket "
                f"does not cross a container bind mount portably (verified "
                f"non-functional in both directions on a Docker Desktop bind "
                f"mount). The per-rung seam transport is the next sub-slice; "
                f"until it lands, a container-sandboxed process must be "
                f"seam-free. Place {pname!r}'s component(s) with the process "
                f"they talk to, or run them unsandboxed.")

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
        declared = [(ctx["seam_dir"], "rw")] + envelope_mounts(env)
        probe, probe_err = self._probe(docker, pname, env, str(image), declared)
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
                                        pythonpath=pythonpath)
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

    def _probe(self, docker: str, pname: str, env: dict, image: str,
               mounts: list[tuple[str, str]],
               pythonpath: str | None = None) -> tuple[dict, str | None]:
        """Run the boot canary INSIDE the boundary, with the exact confinement
        flags a launch with `mounts` would get, and return its report parsed
        into fields — or a diagnostic when it could not run at all."""
        name = f"revl-canary-{pname}-{secrets.token_hex(4)}"
        env_flags = ["-e", f"PYTHONPATH={pythonpath}"] if pythonpath else []
        argv = ([docker, "run"]
                + container_flags(env, name=name, mounts=mounts, interactive=False)
                + env_flags
                + [image, "sh", "-c", _canary_script(env.get("net", "none"))])
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

        if env.get("net", "none") == "none":
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
                                  workdir=achieved.get("workdir"))
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


def resolve_driver(rung: str) -> ContainerDriver | None:
    """The runtime driver for one isolation rung, or None when the rung has no
    driver yet. The caller REFUSES on None: a declared isolation with no driver
    must never fall through to an unconfined process."""
    if rung == ContainerDriver.rung:
        return ContainerDriver()
    return None
