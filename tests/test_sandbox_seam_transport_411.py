"""The sandbox seam transport: the host-bind probe, the relay's live
cross-network forwarding, the seam-only canary, and the approval channel
(roadmap item 411 T3/T5, docs/design/411-seam-transport.md).

Two levels, the same discipline the container rung already uses:

1. the host-bind SELECTION logic, with an injected probe and no runtime: the
   candidate order, first-answer-wins, and the no-answer refusal that names
   both candidates and never falls back to 0.0.0.0. This runs everywhere.

2. against a REAL container runtime, gated on `REVL_SANDBOX_DOCKER` (the
   `sandbox-container` CI job sets it, so these EXECUTE in CI and are not a
   suite that quietly never runs — item 445). Here the transport is built for
   real: the host-bind probe measures which address the relay reaches the host
   on; the relay forwards bytes across two `--internal` networks; the seam-only
   canary discriminates a reachable seam listener from a dropped isolation
   target from inside a sandbox; and the approval channel carries a round-trip
   from a sandboxed consumer, through the relay, to a conductor-side listener.

Every container and network this file creates carries the `revl.sandbox=411`
label and is torn down in a `finally`, so the job's "no container outlives the
job" audit stays green.
"""

import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import sandbox_runtime as _sb  # noqa: E402

_SEAM_RELAY_PY = ROOT / "src" / "revl" / "seam_relay.py"

# The same env gate the container rung reads, for the same item-445 reason: a
# filesystem `which("docker")` probe would let this whole level skip in CI with
# nothing noticing. With the variable set, a broken or absent runtime REDS.
_DOCKER_GATE = "REVL_SANDBOX_DOCKER"


def _docker_gate_unset() -> bool:
    return not os.environ.get(_DOCKER_GATE)


_needs_docker = pytest.mark.skipif(
    _docker_gate_unset(),
    reason=(f"{_DOCKER_GATE} is unset: the seam transport needs a working "
            f"container runtime to build --internal networks, a relay, and a "
            f"sandbox. The `sandbox-container` CI job sets it; locally, start "
            f"Docker and set it to 1."))

_IMAGE_TAG = "revl-sandbox-test:py312"
# Kept byte-identical to the container rung fixture's image so the two files
# share one build on the runner.
_DOCKERFILE = "FROM python:3.12-slim\nRUN pip install --no-cache-dir pyyaml watchdog\n"

_ENV_NONE = {"isolation": "container", "image": _IMAGE_TAG, "fs": [], "net": "none"}


# ==========================================================================
# 1. the host-bind SELECTION logic (no runtime)
# ==========================================================================


def _mgr(docker="") -> _sb.SeamRelayManager:
    # docker="" resolves to no runtime, so nothing here touches a daemon.
    return _sb.SeamRelayManager("plc", _IMAGE_TAG, docker=docker)


def test_the_first_candidate_that_answers_is_taken():
    mgr = _mgr()
    # loopback answers first; the gateway is never probed once one wins.
    probed: list[str] = []

    def probe(cand):
        probed.append(cand)
        return (cand == "127.0.0.1"), "reachable" if cand == "127.0.0.1" else "no"

    addr, err = mgr._select_host_bind(["127.0.0.1", "172.17.0.1"], probe)
    assert err is None
    assert addr == "127.0.0.1"
    assert mgr._bind_host == "127.0.0.1"
    assert probed == ["127.0.0.1"], "the winner short-circuits the rest"


def test_the_order_is_honoured_when_only_the_gateway_answers():
    # the Linux shape: loopback is NOT reachable through host.docker.internal,
    # the bridge gateway is. The probe decides; the loopback-first order does
    # not force the wrong answer.
    mgr = _mgr()
    probed: list[str] = []

    def probe(cand):
        probed.append(cand)
        return (cand == "172.17.0.1"), "reachable" if cand == "172.17.0.1" else "dropped"

    addr, err = mgr._select_host_bind(["127.0.0.1", "172.17.0.1"], probe)
    assert err is None and addr == "172.17.0.1"
    assert mgr._bind_host == "172.17.0.1"
    assert probed == ["127.0.0.1", "172.17.0.1"], "loopback is tried first, then the gateway"


def test_no_candidate_answering_is_a_refusal_naming_both():
    mgr = _mgr()

    def probe(cand):
        return False, f"unreachable-{cand}"

    addr, err = mgr._select_host_bind(["127.0.0.1", "172.17.0.1"], probe)
    assert addr is None
    # the refusal names EVERY candidate and its reason, and states the two
    # things the conductor pointedly does NOT do.
    assert "127.0.0.1" in err and "172.17.0.1" in err
    assert "unreachable-127.0.0.1" in err and "unreachable-172.17.0.1" in err
    assert "0.0.0.0" in err and "net=all" in err
    assert mgr._bind_host is None, "a refusal sets no bind address"


def test_no_runtime_probe_returns_the_loopback_for_the_plan_layer():
    # with genuinely no docker resolved, the plan layer still gets a value
    # (the loopback); the live measurement is CI's job.
    mgr = _sb.SeamRelayManager("plc", _IMAGE_TAG, docker="")
    addr, err = mgr.probe_host_bind()
    assert err is None and addr == "127.0.0.1"


def test_candidate_list_is_loopback_then_gateway(monkeypatch):
    # the candidate order the live probe walks: loopback first, then whatever
    # `docker network inspect bridge` reports as the gateway, de-duplicated.
    mgr = _sb.SeamRelayManager("plc", _IMAGE_TAG, docker="/usr/bin/docker")
    monkeypatch.setattr(mgr, "_bridge_gateway", lambda docker: "172.18.0.1")
    assert mgr._host_bind_candidates("/usr/bin/docker") == ["127.0.0.1", "172.18.0.1"]
    monkeypatch.setattr(mgr, "_bridge_gateway", lambda docker: "127.0.0.1")
    # a gateway equal to the loopback is not listed twice.
    assert mgr._host_bind_candidates("/usr/bin/docker") == ["127.0.0.1"]
    monkeypatch.setattr(mgr, "_bridge_gateway", lambda docker: None)
    assert mgr._host_bind_candidates("/usr/bin/docker") == ["127.0.0.1"]


# ==========================================================================
# 2. against a real container runtime (gated: REVL_SANDBOX_DOCKER)
# ==========================================================================


def _docker(*args, **kw):
    return subprocess.run(["docker", *args], capture_output=True, text=True,
                          timeout=kw.pop("timeout", 120))


@pytest.fixture(scope="module")
def runner_image():
    if _docker("image", "inspect", _IMAGE_TAG).returncode != 0:
        built = subprocess.run(
            ["docker", "build", "-t", _IMAGE_TAG, "-"], input=_DOCKERFILE,
            capture_output=True, text=True, timeout=900)
        assert built.returncode == 0, built.stderr[-2000:]
    return _IMAGE_TAG


class _Live:
    """Bookkeeping for the containers and networks one test creates, so a
    `finally` can remove every one even on an assertion failure."""

    def __init__(self) -> None:
        self.containers: list[str] = []
        self.networks: list[str] = []

    def network(self, name: str, *flags: str) -> str:
        rc = _docker("network", "create", *flags, name)
        assert rc.returncode == 0, rc.stderr
        self.networks.append(name)
        return name

    def run(self, name: str, *argv: str, detach: bool = True) -> subprocess.CompletedProcess:
        self.containers.append(name)
        mode = ("-d",) if detach else ()
        return _docker("run", *mode, "--rm", "--name", name,
                       "--label", "revl.sandbox=411", *argv)

    def connect(self, net: str, container: str) -> None:
        rc = _docker("network", "connect", net, container)
        assert rc.returncode == 0, rc.stderr

    def wait_for_log(self, container: str, needle: str, timeout: float = 20.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            logs = _docker("logs", container)
            if needle in (logs.stdout + logs.stderr):
                return True
            time.sleep(0.25)
        return False

    def ip_on(self, container: str, network: str) -> str:
        rc = _docker("inspect", "-f",
                     "{{(index .NetworkSettings.Networks \"" + network + "\").IPAddress}}",
                     container)
        return rc.stdout.strip()

    def teardown(self) -> None:
        for c in self.containers:
            _docker("rm", "-f", c, timeout=60)
        for n in self.networks:
            _docker("network", "rm", n, timeout=60)


@pytest.fixture()
def live():
    handle = _Live()
    try:
        yield handle
    finally:
        handle.teardown()


def _relay_flags(table_path: Path, *, add_host: bool) -> list[str]:
    """Mount the real, stdlib-only relay module and the table into the relay
    container. The shipped `SeamRelayManager.relay_argv` assumes a first-party
    image that already carries `revl.seam_relay`; the stock test image does not,
    so the module is mounted directly — it is the same file the manager runs."""
    flags = ["-v", f"{_SEAM_RELAY_PY}:/seam_relay.py:ro",
             "-v", f"{table_path}:/table.json:ro"]
    if add_host:
        flags += ["--add-host", "host.docker.internal:host-gateway"]
    return flags


_ECHO_PY = (
    "import socket,sys\n"
    "srv=socket.socket();srv.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)\n"
    "srv.bind(('0.0.0.0',int(sys.argv[1])));srv.listen(8)\n"
    "print('ECHO-UP',flush=True)\n"
    "while True:\n"
    "    c,_=srv.accept()\n"
    "    d=c.recv(4096)\n"
    "    c.sendall(d.upper());c.close()\n"
)


# -- the host-bind probe, live ----------------------------------------------


@_needs_docker
def test_the_host_bind_probe_measures_a_reachable_address(runner_image):
    """The probe binds a throwaway host listener on each candidate and has a
    bridge container connect back through host.docker.internal; the address it
    returns is the one that actually answered — 127.0.0.1 on Docker Desktop, the
    bridge gateway on Linux — never a guess and never 0.0.0.0."""
    mgr = _sb.SeamRelayManager("plc-probe", runner_image)
    addr, err = mgr.probe_host_bind()
    assert err is None, err
    assert addr, "a live daemon must yield a reachable host bind address"
    assert addr != "0.0.0.0"
    # it is one of the two documented candidates, and it is what the manager
    # then hands every host-side listener.
    candidates = mgr._host_bind_candidates(mgr._resolve_docker())
    assert addr in candidates, (addr, candidates)
    assert mgr._bind_host == addr
    print(f"host bind address measured live: {addr!r} of {candidates!r}")


# -- the relay forwards across two --internal networks ----------------------


@_needs_docker
def test_the_relay_forwards_bytes_across_two_internal_networks(runner_image, live, tmp_path):
    """T3 reachability, the core: a consumer on ONE `--internal` network reaches
    a provider on ANOTHER only through the conductor-owned relay. Neither sandbox
    can reach the other directly; the relay, attached to both, forwards the bytes
    blindly (here a plaintext echo — the real seam is mTLS the relay never reads).
    """
    net_a = live.network("revl-sb-plc-net-a", "--internal")
    net_b = live.network("revl-sb-plc-net-b", "--internal")

    echo = tmp_path / "echo.py"
    echo.write_text(_ECHO_PY, encoding="utf-8")
    prov = "revl-sb-plc-prov"
    rc = live.run(prov, "--network", net_a, "-v", f"{echo}:/echo.py:ro",
                  runner_image, "python3", "/echo.py", "9443")
    assert rc.returncode == 0, rc.stderr
    assert live.wait_for_log(prov, "ECHO-UP"), _docker("logs", prov).stdout

    # the relay listens on port 15001 and forwards to the provider BY NAME on
    # net_a (Docker's embedded DNS resolves the sibling at connect time).
    table = tmp_path / "table.json"
    table.write_text(json.dumps({"rows": [
        {"id": "consumer->prov", "listen": {"port": 15001},
         "target": f"{prov}:9443"}]}), encoding="utf-8")
    relay = "revl-sb-plc-relay"
    # the relay starts on the default bridge, then is attached to both networks;
    # its 0.0.0.0 listeners accept on the interfaces added afterwards.
    rc = live.run(relay, *_relay_flags(table, add_host=False),
                  runner_image, "python3", "/seam_relay.py", "/table.json",
                  "--bind-host", "0.0.0.0")
    assert rc.returncode == 0, rc.stderr
    assert live.wait_for_log(relay, "UP"), _docker("logs", relay).stdout
    live.connect(net_a, relay)
    live.connect(net_b, relay)

    # the consumer, on net_b ONLY, dials the relay by name and gets the echo
    # back — proving the two-network forward end to end.
    client = (
        "import socket,sys\n"
        "s=socket.socket();s.settimeout(10)\n"
        "s.connect((sys.argv[1], int(sys.argv[2])))\n"
        "s.sendall(b'seam-payload')\n"
        "print(s.recv(64).decode())\n")
    cli = tmp_path / "client.py"
    cli.write_text(client, encoding="utf-8")
    rc = live.run("revl-sb-plc-consumer", "--network", net_b,
                  "-v", f"{cli}:/client.py:ro", runner_image,
                  "python3", "/client.py", relay, "15001", detach=False)
    assert rc.returncode == 0, rc.stderr + rc.stdout
    assert "SEAM-PAYLOAD" in rc.stdout, rc.stdout + rc.stderr


# -- the seam-only canary discriminates from inside a sandbox ----------------


@_needs_docker
def test_the_seam_only_canary_confirms_the_boundary_from_inside(runner_image, live, tmp_path):
    """T3's seam-only canary, live. From inside an `--internal` sandbox: the
    relay listener the seam uses is REACHABLE (SEAM=open); the relay's own
    bridge-side address, which the relay proved open from the bridge, is DROPPED
    (ISOLATION=confirmed); and no external name resolves (DNS=closed). Same
    target, two vantage points, one positive and one negative — that is what
    makes a drop evidence of confinement rather than the absence of evidence."""
    net = live.network("revl-sb-plc-c-net", "--internal")

    # a relay whose single row just needs to ACCEPT a TCP connect for SEAM=open;
    # its target can be anything (the sandbox never gets past the relay's accept).
    table = tmp_path / "table.json"
    table.write_text(json.dumps({"rows": [
        {"id": "seam", "listen": {"port": 15001}, "target": "127.0.0.1:1"}]}),
        encoding="utf-8")
    relay = "revl-sb-plc-c-relay"
    rc = live.run(relay, *_relay_flags(table, add_host=False),
                  runner_image, "python3", "/seam_relay.py", "/table.json",
                  "--bind-host", "0.0.0.0")
    assert rc.returncode == 0, rc.stderr
    assert live.wait_for_log(relay, "UP"), _docker("logs", relay).stdout
    live.connect(net, relay)

    relay_bridge_ip = live.ip_on(relay, "bridge")
    assert relay_bridge_ip, "the relay must have a bridge-side address to probe"

    # the isolation target IS open from the bridge (the positive vantage) ...
    from_bridge = tmp_path / "frombridge.py"
    from_bridge.write_text(
        "import socket,sys\n"
        "s=socket.socket();s.settimeout(5)\n"
        "s.connect((sys.argv[1], int(sys.argv[2])));print('BRIDGE-OPEN')\n",
        encoding="utf-8")
    rc = live.run("revl-sb-plc-bridgeprobe", "-v", f"{from_bridge}:/p.py:ro",
                  runner_image, "python3", "/p.py", relay_bridge_ip, "15001",
                  detach=False)
    assert "BRIDGE-OPEN" in rc.stdout, rc.stdout + rc.stderr

    # ... and DROPPED from inside the sandbox (the negative vantage). The canary
    # runs the real generated script; the sandbox joins the --internal network
    # through the driver's own container_flags.
    script = _sb.seam_canary_script(
        [(relay, 15001)], (relay_bridge_ip, 15001))
    flags = _sb.container_flags(_ENV_NONE, name="revl-sb-plc-c-sandbox",
                                mounts=[], interactive=False, network=net)
    live.containers.append("revl-sb-plc-c-sandbox")
    proc = _docker("run", "--rm", "--name", "revl-sb-plc-c-sandbox", *flags,
                   runner_image, "sh", "-c", script, timeout=90)
    out = proc.stdout + proc.stderr
    assert "SEAM=open" in out, out
    assert "ISOLATION=confirmed" in out, (
        "a target the relay proved open from the bridge was reachable from "
        "inside the --internal sandbox: the network is not confining egress.\n"
        + out)
    assert "DNS=closed" in out, out


@_needs_docker
def test_a_sandbox_on_the_bridge_fails_the_isolation_clause(runner_image, live, tmp_path):
    """The negative control (design: "a sandbox launched on the default bridge
    instead of its internal network — the non-enforcing daemon stand-in — dies at
    the ISOLATION clause"). Same canary, same isolation target; on the bridge the
    target IS reachable, so the clause reports LEAK rather than confirmed. This is
    what proves the confirmed verdict above is measuring enforcement, not luck."""
    # a listener the sandbox on the bridge really can reach: a plain container on
    # the default bridge, probed by its bridge IP.
    echo = tmp_path / "echo.py"
    echo.write_text(_ECHO_PY, encoding="utf-8")
    target = "revl-sb-plc-bridgetarget"
    rc = live.run(target, "-v", f"{echo}:/echo.py:ro", runner_image,
                  "python3", "/echo.py", "15001")
    assert rc.returncode == 0, rc.stderr
    assert live.wait_for_log(target, "ECHO-UP"), _docker("logs", target).stdout
    target_ip = live.ip_on(target, "bridge")
    assert target_ip

    # net=all: the canary judges only the seam, but the raw probe output still
    # reports the ISOLATION reading, which on the bridge must be a LEAK.
    script = _sb.seam_canary_script([], (target_ip, 15001))
    flags = _sb.container_flags(
        {**_ENV_NONE, "net": "all"}, name="revl-sb-plc-bridgesandbox",
        mounts=[], interactive=False)  # no per-process network: plain bridge
    live.containers.append("revl-sb-plc-bridgesandbox")
    proc = _docker("run", "--rm", "--name", "revl-sb-plc-bridgesandbox", *flags,
                   runner_image, "sh", "-c", script, timeout=90)
    out = proc.stdout + proc.stderr
    assert "ISOLATION=LEAK" in out, (
        "the isolation target was NOT reachable even on the bridge, so the "
        "discriminating probe proves nothing.\n" + out)


# -- T5: the approval channel carries a round-trip ---------------------------


@_needs_docker
def test_the_approval_channel_round_trips_from_a_sandbox(runner_image, live, tmp_path):
    """T5, the transport half, live. A sandboxed consumer raises an approval
    request through the relay to a conductor-side listener and receives the
    verdict back. The path is exactly the design's "sandboxed consumer, host-side
    provider" direction: the consumer dials `<relay>:<port>` on its own network,
    the relay forwards to `host.docker.internal:<listener>`, and the host
    listener binds the PROBED host bind address (loopback on Docker Desktop, the
    bridge gateway on Linux) so the relay can reach it."""
    mgr = _sb.SeamRelayManager("plc-approval", runner_image)
    bind_host, err = mgr.probe_host_bind()
    assert err is None and bind_host, err

    # the conductor-side approval listener: it reads a prompt and returns a
    # verdict. Bound on the probed address so the relay reaches it through
    # host.docker.internal.
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind((bind_host, 0))
    listener.listen(4)
    listener.settimeout(30)
    approval_port = listener.getsockname()[1]
    seen: dict = {}

    def serve():
        try:
            conn, _ = listener.accept()
            with conn:
                conn.settimeout(30)
                seen["prompt"] = conn.recv(256).decode()
                conn.sendall(b"APPROVED")
        except OSError as exc:  # pragma: no cover - surfaces as a failed assert
            seen["error"] = str(exc)

    server = threading.Thread(target=serve, daemon=True)
    server.start()

    try:
        net = live.network("revl-sb-plc-a-net", "--internal")
        table = tmp_path / "table.json"
        table.write_text(json.dumps({"rows": [
            {"id": "approval:consumer", "listen": {"port": 15100},
             "target": f"host.docker.internal:{approval_port}"}]}),
            encoding="utf-8")
        relay = "revl-sb-plc-a-relay"
        rc = live.run(relay, *_relay_flags(table, add_host=True),
                      runner_image, "python3", "/seam_relay.py", "/table.json",
                      "--bind-host", "0.0.0.0")
        assert rc.returncode == 0, rc.stderr
        assert live.wait_for_log(relay, "UP"), _docker("logs", relay).stdout
        live.connect(net, relay)

        # the sandboxed consumer, on the --internal network only, raises its
        # prompt at <relay>:15100 and blocks for the verdict.
        client = (
            "import socket,sys\n"
            "s=socket.socket();s.settimeout(20)\n"
            "s.connect((sys.argv[1], int(sys.argv[2])))\n"
            "s.sendall(b'approve? delete_all')\n"
            "print(s.recv(64).decode())\n")
        cli = tmp_path / "client.py"
        cli.write_text(client, encoding="utf-8")
        rc = live.run("revl-sb-plc-a-consumer", "--network", net,
                      "-v", f"{cli}:/client.py:ro", runner_image,
                      "python3", "/client.py", relay, "15100", detach=False)
        server.join(timeout=30)
        assert rc.returncode == 0, rc.stderr + rc.stdout
        assert "APPROVED" in rc.stdout, rc.stdout + rc.stderr
        assert seen.get("prompt") == "approve? delete_all", seen
    finally:
        listener.close()


# -- the gate is applied, and to these tests (item 445 guard-on-guard) -------


def test_the_docker_gate_is_read_bare_and_carried_by_the_live_tests():
    assert _DOCKER_GATE == "REVL_SANDBOX_DOCKER"
    assert _needs_docker.mark.name == "skipif"
    assert _needs_docker.mark.args == (_docker_gate_unset(),)
    for gated in (test_the_host_bind_probe_measures_a_reachable_address,
                  test_the_relay_forwards_bytes_across_two_internal_networks,
                  test_the_seam_only_canary_confirms_the_boundary_from_inside,
                  test_a_sandbox_on_the_bridge_fails_the_isolation_clause,
                  test_the_approval_channel_round_trips_from_a_sandbox):
        assert _needs_docker.mark in gated.pytestmark, gated.__name__
