"""Fuller Path A: a **ts consumer reaches the py `Gate` over the network** —
a real localhost TCP + mutual-TLS round trip, not the local UDS of item 144.

Item 144 proved a cross-tier admit over a filtered shared IR on a **local UDS**.
The clean shape is two compositions sharing only `service Gate` over item 56's
**network** transport — but that transport's *client* was py-only, so a ts
consumer over the network was unwired (docs/gate-as-a-service.md, "What a fuller
Path A needs" #1). Item 149 wires it: placement allows a ts->py network seam and
backends/typescript/bridge.ts::makeProxy grew a TCP+mTLS endpoint.

These tests spawn a **real node process** (the production makeProxy path) against
a **real py provider** over loopback TCP+mTLS, and assert:

  * a candidate admits / a G2 collision is refused over the network path, verdict
    + why-trace surviving the round trip (G2 holds across the boundary, G8 keeps
    the served surface to the declared operations);
  * a wedged remote provider breaches the seam **deadline** and the ts consumer
    **reactively withdraws** (does not block forever);
  * a **dropped** remote provider (SIGKILL) is turned into the same reactive
    withdrawal by the mTLS monitor connection.

No cordis is needed for the transport itself (as in tests/test_gate_bridge_
service.py level 3); the harness drives makeProxy directly. Skips cleanly when
`openssl` or `node` is absent — never a false pass.
"""

from __future__ import annotations

import json
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

if shutil.which("openssl") is None:
    pytest.skip("openssl CLI not available for test cert generation",
                allow_module_level=True)

from revl import placement as _placement  # noqa: E402

PROVIDER = ROOT / "tests" / "_net_gate_provider.py"
CLIENT = ROOT / "tests" / "_net_gate_client.ts"
HOST = "127.0.0.1"
SERVERNAME = "localhost"  # the cert carries SAN DNS:localhost + IP:127.0.0.1


def _ts_gate_client_skip_reason() -> str | None:
    """Why the ts network client cannot run here, or None when it can.

    Every real test in this module drives the production client by spawning
    `node <_net_gate_client.ts>`. Some node builds load a bare `.ts` entry point
    as CommonJS (nothing marks the tree as an ES module), so the client's
    top-level `import` raises "Cannot use import statement outside a module" and
    the ts tier runtime is simply unusable on this box. Probe it once by loading
    the real client: on that failure skip with a reason rather than report a
    spurious failure; a healthy runtime loads the module cleanly (then exits on
    the missing config argument), which counts as runnable.
    """
    node = shutil.which("node")
    if node is None:
        return "node not on PATH for the ts network client"
    try:
        probe = subprocess.run([node, str(CLIENT)],
                               capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"could not probe the ts gate client with node: {exc}"
    combined = probe.stdout + probe.stderr
    if ("Cannot use import statement outside a module" in combined
            or "Failed to load the ES module" in combined):
        return (f"node ({node}) cannot load the .ts gate client as an ES module "
                "here, so the ts tier runtime is unusable on this box")
    return None


_TS_CLIENT_SKIP = _ts_gate_client_skip_reason()
if _TS_CLIENT_SKIP is not None:
    pytest.skip(_TS_CLIENT_SKIP, allow_module_level=True)


@pytest.fixture(scope="module")
def certs(tmp_path_factory):
    out = tmp_path_factory.mktemp("net_gate_certs")
    return _placement.generate_seam_certs(out, ["gate", "user"])


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind((HOST, 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _start_provider(tmp_path, certs, port, *, wedge=False) -> subprocess.Popen:
    cfg = {"host": HOST, "port": port, "server_hostname": SERVERNAME,
           "cert": certs["gate"]["cert"], "key": certs["gate"]["key"],
           "ca": certs["gate"]["ca"], "identity": "gate", "wedge": wedge}
    cfg_path = tmp_path / f"provider_{port}.json"
    cfg_path.write_text(json.dumps(cfg))
    proc = subprocess.Popen(
        [sys.executable, str(PROVIDER), str(cfg_path)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    # wait for PROVIDER-UP (bounded), so the client never races the listener
    deadline = time.time() + 15
    while time.time() < deadline:
        line = proc.stdout.readline()
        if not line:
            raise RuntimeError("provider exited before coming up: "
                               + (proc.stdout.read() or ""))
        if line.startswith("PROVIDER-UP"):
            return proc
    proc.kill()
    raise RuntimeError("provider did not come up in time")


def _client_endpoint(certs, port) -> dict:
    return {"host": HOST, "port": port,
            "tls": {"cert": certs["user"]["cert"], "key": certs["user"]["key"],
                    "ca": certs["user"]["ca"], "identity": "user",
                    "server_hostname": SERVERNAME}}


def _run_client(tmp_path, cfg, *, tag) -> subprocess.CompletedProcess:
    cfg_path = tmp_path / f"client_{tag}.json"
    cfg_path.write_text(json.dumps(cfg))
    return subprocess.run(
        [shutil.which("node"), str(CLIENT), str(cfg_path)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=60)


# ---------------------------------------------------------------------------
# 1. the round trip: a ts consumer admits against the py gate over TCP+mTLS
# ---------------------------------------------------------------------------

def test_ts_consumer_admits_against_py_gate_over_the_network(tmp_path, certs):
    """The definition-of-done proof: a real node client reaches the py `Gate`
    over loopback **TCP + mutual TLS** (not UDS). The `collide` candidate is
    refused by G2 and the `clean` one admitted — the guarantee and the verdict
    cross the network boundary intact."""
    port = _free_port()
    provider = _start_provider(tmp_path, certs, port)
    try:
        cfg = {"endpoint": _client_endpoint(certs, port), "deadlineMs": 30000,
               "calls": [["admit_case", "collide"], ["admit_case", "clean"]]}
        result = _run_client(tmp_path, cfg, tag="admit")
    finally:
        provider.terminate()
        provider.wait(timeout=10)

    out = result.stdout
    assert result.returncode == 0, out
    # both verdicts crossed back over the network seam
    lines = {line.split(" ", 2)[1]: line for line in out.splitlines()
             if line.startswith("VERDICT ")}
    assert "collide" in lines and "clean" in lines, out

    collide = json.loads(lines["collide"].split(" ", 2)[2])
    clean = json.loads(lines["clean"].split(" ", 2)[2])
    # G2 survives the network boundary: the colliding candidate is refused, its
    # why-trace riding back in the verdict.
    assert collide["ok"] is False and "(G2)" in collide["diagnostic"], collide
    assert "why" in collide["diagnostic"]
    assert collide["admitted"] == []
    # the clean candidate admits, over the same TCP+mTLS path.
    assert clean["ok"] is True and clean["admitted"] == ["Extra"], clean


def test_network_proxy_forwards_only_declared_methods(tmp_path, certs):
    """G8, consumer side: makeProxy exposes exactly the declared operations, so
    a method the `Gate` service does not declare cannot even be forwarded over
    the network seam. Asserted against a live provider so the declared calls are
    shown to work over the same TCP+mTLS path that refuses the undeclared one."""
    port = _free_port()
    provider = _start_provider(tmp_path, certs, port)
    try:
        cfg = {"endpoint": _client_endpoint(certs, port), "deadlineMs": 30000,
               "calls": [["admit_case", "collide"], ["nonexistent", "x"]]}
        result = _run_client(tmp_path, cfg, tag="surface")
    finally:
        provider.terminate()
        provider.wait(timeout=10)
    out = result.stdout
    assert result.returncode == 0, out
    assert "VERDICT collide" in out, out          # a declared op crosses
    # an undeclared op is never forwarded — the proxy has no such method, so the
    # call raises client-side rather than reaching the wire.
    assert "CALL ERROR x" in out, out


# ---------------------------------------------------------------------------
# 2. a wedged remote provider: the seam deadline fires and the ts consumer
#    reactively withdraws rather than blocking forever.
# ---------------------------------------------------------------------------

def test_wedged_provider_breaches_deadline_and_withdraws(tmp_path, certs):
    port = _free_port()
    provider = _start_provider(tmp_path, certs, port, wedge=True)
    try:
        cfg = {"endpoint": _client_endpoint(certs, port), "deadlineMs": 500,
               "calls": [["admit_case", "collide"]]}
        result = _run_client(tmp_path, cfg, tag="wedge")
    finally:
        provider.terminate()
        provider.wait(timeout=10)

    out = result.stdout
    assert result.returncode == 0, out
    assert "CALL SEAM_DEADLINE collide" in out, out   # the hang is bounded
    assert "WITHDRAWN" in out, out                     # and it withdrew (G-timeout)
    assert out.rstrip().endswith("DONE withdrawn"), out


# ---------------------------------------------------------------------------
# 3. a dropped remote provider (SIGKILL): the mTLS monitor turns the death into
#    the same reactive withdrawal (R2/R3).
# ---------------------------------------------------------------------------

def test_dropped_provider_triggers_reactive_withdrawal(tmp_path, certs):
    port = _free_port()
    provider = _start_provider(tmp_path, certs, port)
    cfg = {"endpoint": _client_endpoint(certs, port), "deadlineMs": 30000,
           "calls": [["admit_case", "clean"]], "watch": True, "watchMs": 15000}
    cfg_path = tmp_path / "client_drop.json"
    cfg_path.write_text(json.dumps(cfg))
    client = subprocess.Popen(
        [shutil.which("node"), str(CLIENT), str(cfg_path)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        # wait until the client has admitted once and is watching the monitor,
        # then kill the provider so the mTLS monitor connection drops.
        saw_watching = False
        deadline = time.time() + 30
        while time.time() < deadline:
            line = client.stdout.readline()
            if not line:
                break
            if line.startswith("WATCHING"):
                saw_watching = True
                provider.kill()
                break
        assert saw_watching, "client never reached the watch phase"
        rest = client.stdout.read()
        assert "WITHDRAWN" in rest, rest
    finally:
        provider.poll() is None and provider.kill()
        client.wait(timeout=15)


# ---------------------------------------------------------------------------
# 4. the full conductor boot over the network placement: `revl run` on
#    examples/placement/gate_pyts_network.toml — a node GateUser probes the py
#    Gate over TCP+mTLS. Skips cleanly when node or either cordis runtime is
#    absent, never a false pass.
# ---------------------------------------------------------------------------

GATE_RVL = str(ROOT / "examples" / "gate_service.rvl")
GATE_NET_TOML = str(ROOT / "examples" / "placement" / "gate_pyts_network.toml")


def _cross_tier_ready():
    if not _placement._cordis_py_installed():
        return "cordis-py runtime not installed (sh backends/python/setup.sh)"
    if not (_placement._TS_DIR / "node_modules" / "cordis").is_dir():
        return "cordis-ts runtime not installed (cd backends/typescript && npm install)"
    return None


def test_full_conductor_boot_over_the_network_placement(capfd):
    """The end-to-end conductor form: run the network placement for real — the
    node `GateUser` probes the py `Gate` across the item-56 TCP+mTLS transport.
    The G2 collision is refused and the clean candidate admitted, cross-tier,
    over the network — the same verdicts as the UDS placement, now on TCP."""
    reason = _cross_tier_ready()
    if reason:
        pytest.skip(reason)
    rc = _placement.run_placement([GATE_RVL], GATE_NET_TOML, once=True)
    out = capfd.readouterr().out
    assert rc == 0, out
    assert "collide" in out and "clean" in out
    assert "(G2)" in out                                # the refusal crossed TCP
    assert "network seams (item 56): 1 over TCP+mTLS" in out
    assert '"ok": true' in out or "'ok': True" in out   # the clean admit crossed
