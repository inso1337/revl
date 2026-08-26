"""Fuller Path A, final step: the fully **independent two-composition** shape
(roadmap item 151).

Item 149 wired a ts consumer to a py `Gate` over the network, but still compiled
provider and consumer as ONE composition — a shared `.rvl` — so
`placement.ts_safe_ir` had to *filter* the `@py` compiler extern out of the node
module. This item finishes the decoupling: two SEPARATE compositions that share
only `service Gate`.

  * examples/placement/gate_provider.rvl — py `GateProvider` + the `@py` compiler
    externs, deployed on its own placement behind a TCP+mTLS listener;
  * examples/placement/gate_consumer.rvl — ts `GateUser` + only the `service Gate`
    interface, reaching the gate **by address alone** through a `[remotes.gate]`
    seam.

The point of the split is that the consumer's compiled IR never contained the
compiler extern in the first place — so `ts_safe_ir` has nothing to filter, and
the decoupling is real rather than masked. These tests assert exactly that, then
prove an independently compiled consumer admits a candidate against a
separately-booted py gate reaching it by address, with G2 refusal and reactive
withdrawal holding across the boundary — reusing item 149's real-node-process
harness (tests/_net_gate_client.ts / tests/_net_gate_provider.py).

Skips cleanly when `openssl` or `node` is absent — never a false pass.
"""

from __future__ import annotations

import json
import shutil
import socket
import subprocess
import sys
import time
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import placement as _placement  # noqa: E402
from revl.compiler import compile_files  # noqa: E402

PROVIDER_RVL = str(ROOT / "examples" / "placement" / "gate_provider.rvl")
CONSUMER_RVL = str(ROOT / "examples" / "placement" / "gate_consumer.rvl")
CONSUMER_TOML = ROOT / "examples" / "placement" / "gate_consumer_network.toml"
PROVIDER_HARNESS = ROOT / "tests" / "_net_gate_provider.py"
CLIENT = ROOT / "tests" / "_net_gate_client.ts"
HOST = "127.0.0.1"
SERVERNAME = "localhost"  # the cert carries SAN DNS:localhost + IP:127.0.0.1

# The compiler symbols the provider composition owns and the consumer must NOT.
COMPILER_SYMBOLS = ("host_gate_admit", "host_gate_admit_case", "compile_files")


def _ts_gate_client_skip_reason() -> str | None:
    """Why the ts gate client cannot run here, or None when it can.

    The two-composition transport tests drive the production client by spawning
    `node <_net_gate_client.ts>`. Some node builds load a bare `.ts` entry point
    as CommonJS (nothing marks the tree as an ES module), so the client's
    top-level `import` raises "Cannot use import statement outside a module" and
    the ts tier runtime is unusable on this box. Probe it once by loading the
    real client: on that failure skip with a reason rather than report a
    spurious failure; a healthy runtime loads the module cleanly (then exits on
    the missing config argument), which counts as runnable.
    """
    node = shutil.which("node")
    if node is None:
        return "node not on PATH for the ts client"
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


# ---------------------------------------------------------------------------
# 1. the decoupling itself: the consumer composition, compiled INDEPENDENTLY,
#    carries no compiler extern anywhere in its IR. No runtime needed.
# ---------------------------------------------------------------------------

def test_consumer_composition_has_no_compiler_extern():
    """The core claim: compiled on its own, the consumer's IR never contained the
    `@py` compiler extern — so the decoupling is real, not a filtered shared IR.

    Contrast the provider composition, which *does* own those externs. This is
    what makes `ts_safe_ir` a no-op for the consumer: there is nothing to filter
    (item 151 vs the shared-IR filtering item 144/149 relied on)."""
    consumer_ir = compile_files([CONSUMER_RVL])
    provider_ir = compile_files([PROVIDER_RVL])

    # the provider owns the compiler externs ...
    prov_externs = {e["name"] for e in provider_ir.get("externs") or []}
    assert "host_gate_admit" in prov_externs and "host_gate_admit_case" in prov_externs

    # ... and the consumer owns NONE of them, anywhere in its IR.
    consumer_externs = {e.get("name") for e in consumer_ir.get("externs") or []}
    assert consumer_externs == set(), consumer_externs
    blob = json.dumps(consumer_ir)
    for symbol in COMPILER_SYMBOLS:
        assert symbol not in blob, f"consumer IR unexpectedly reaches {symbol!r}"

    # both compositions independently declare the SAME shared interface — the one
    # thing they overlap on — so the seam is exactly `service Gate`.
    assert "Gate" in (consumer_ir.get("services") or {})
    assert (consumer_ir["services"]["Gate"].get("methods") or {}).keys() == \
           (provider_ir["services"]["Gate"].get("methods") or {}).keys()

    # ts_safe_ir has nothing to drop: the consumer slice is byte-identical.
    assert _placement.ts_safe_ir(consumer_ir) == consumer_ir


def test_consumer_module_emits_without_the_compiler_extern():
    """The stronger form: the ts module the conductor actually emits for the
    consumer (through emit.py, the production path) contains no compiler symbol
    — because the IR handed to the emitter never had one, not because it was
    filtered. Skips when node/emit prerequisites are absent."""
    if shutil.which("node") is None:
        pytest.skip("node not on PATH for the ts emitter check")
    consumer_ir = compile_files([CONSUMER_RVL])
    import tempfile
    with tempfile.TemporaryDirectory(prefix="revl_twocomp_emit_") as td:
        module_path = _placement._emit_ts_module(consumer_ir, Path(td))
        source = Path(module_path).read_text(encoding="utf-8")
        Path(module_path).unlink(missing_ok=True)
    for symbol in COMPILER_SYMBOLS:
        assert symbol not in source, f"emitted consumer module reaches {symbol!r}"
    assert "GateUser" in source  # it did emit the consumer component


# ---------------------------------------------------------------------------
# shared-CA cert material + a separately-booted py gate (the harness item 149
# already ships). Two independent compositions agree on ONE CA out of band.
# ---------------------------------------------------------------------------

pytestmark_net = pytest.mark.skipif(
    shutil.which("openssl") is None or _TS_CLIENT_SKIP is not None,
    reason=_TS_CLIENT_SKIP or "needs openssl (cert minting) and node (the ts client)")


@pytest.fixture(scope="module")
def certs(tmp_path_factory):
    if shutil.which("openssl") is None:
        pytest.skip("openssl CLI not available for test cert generation")
    out = tmp_path_factory.mktemp("two_comp_certs")
    return _placement.generate_seam_certs(out, ["gate", "user"])


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind((HOST, 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _start_gate(tmp_path, certs, port, *, wedge=False) -> subprocess.Popen:
    """Boot the py gate as its OWN process — a separately-deployed provider the
    consumer reaches by address alone (item 149's harness)."""
    cfg = {"host": HOST, "port": port, "server_hostname": SERVERNAME,
           "cert": certs["gate"]["cert"], "key": certs["gate"]["key"],
           "ca": certs["gate"]["ca"], "identity": "gate", "wedge": wedge}
    cfg_path = tmp_path / f"gate_{port}.json"
    cfg_path.write_text(json.dumps(cfg))
    proc = subprocess.Popen(
        [sys.executable, str(PROVIDER_HARNESS), str(cfg_path)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    deadline = time.time() + 15
    while time.time() < deadline:
        line = proc.stdout.readline()
        if not line:
            raise RuntimeError("gate exited before coming up: " + (proc.stdout.read() or ""))
        if line.startswith("PROVIDER-UP"):
            return proc
    proc.kill()
    raise RuntimeError("gate did not come up in time")


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
# 2. by address alone: an independently compiled consumer admits against a
#    separately-booted gate — G2 refusal crosses the boundary.
# ---------------------------------------------------------------------------

@pytestmark_net
def test_consumer_admits_against_separately_booted_gate_by_address(tmp_path, certs):
    """The definition-of-done proof (transport half): the consumer reaches a
    gate it shares no IR with, by address, over TCP+mTLS. `collide` is refused by
    G2 (why-trace intact), `clean` is admitted — the guarantee crosses the
    two-composition boundary."""
    port = _free_port()
    gate = _start_gate(tmp_path, certs, port)
    try:
        cfg = {"endpoint": _client_endpoint(certs, port), "deadlineMs": 30000,
               "calls": [["admit_case", "collide"], ["admit_case", "clean"]]}
        result = _run_client(tmp_path, cfg, tag="admit")
    finally:
        gate.terminate()
        gate.wait(timeout=10)

    out = result.stdout
    assert result.returncode == 0, out
    lines = {line.split(" ", 2)[1]: line for line in out.splitlines()
             if line.startswith("VERDICT ")}
    assert "collide" in lines and "clean" in lines, out
    collide = json.loads(lines["collide"].split(" ", 2)[2])
    clean = json.loads(lines["clean"].split(" ", 2)[2])
    assert collide["ok"] is False and "(G2)" in collide["diagnostic"], collide
    assert collide["admitted"] == []
    assert clean["ok"] is True and clean["admitted"] == ["Extra"], clean


@pytestmark_net
def test_wedged_gate_breaches_deadline_and_consumer_withdraws(tmp_path, certs):
    """Reactive withdrawal across the two-composition boundary: a wedged gate
    (accepts the handshake, never replies) breaches the seam deadline and the
    consumer withdraws rather than blocking forever."""
    port = _free_port()
    gate = _start_gate(tmp_path, certs, port, wedge=True)
    try:
        cfg = {"endpoint": _client_endpoint(certs, port), "deadlineMs": 500,
               "calls": [["admit_case", "collide"]]}
        result = _run_client(tmp_path, cfg, tag="wedge")
    finally:
        gate.terminate()
        gate.wait(timeout=10)
    out = result.stdout
    assert result.returncode == 0, out
    assert "CALL SEAM_DEADLINE collide" in out, out
    assert "WITHDRAWN" in out, out
    assert out.rstrip().endswith("DONE withdrawn"), out


# ---------------------------------------------------------------------------
# 3. the full conductor form: `revl run` on the CONSUMER placement, whose
#    `[remotes.gate]` reaches a separately-booted gate by address. Needs the
#    cordis runtimes; skips cleanly otherwise.
# ---------------------------------------------------------------------------

def _cross_tier_ready():
    if not _placement._cordis_py_installed():
        return "cordis-py runtime not installed (sh backends/python/setup.sh)"
    if not (_placement._TS_DIR / "node_modules" / "cordis").is_dir():
        return "cordis-ts runtime not installed (cd backends/typescript && npm install)"
    return None


def _template_consumer_toml(tmp_path, certs, port) -> str:
    """Load the committed consumer placement and fill in the ephemeral bits a
    real deployment supplies: the freshly minted shared-CA `user` cert and the
    gate's actual port. The topology (remote-by-address, ts tier) is the
    committed file's, unchanged."""
    conf = tomllib.loads(CONSUMER_TOML.read_text(encoding="utf-8"))
    conf["remotes"]["gate"]["port"] = port
    conf["remotes"]["gate"]["host"] = HOST
    tls = conf["processes"]["user"]["tls"]
    tls["cert"] = certs["user"]["cert"]
    tls["key"] = certs["user"]["key"]
    tls["ca"] = certs["user"]["ca"]
    # tomllib reads; write JSON (run_placement accepts .json placements too).
    out = tmp_path / "consumer_placement.json"
    out.write_text(json.dumps(conf), encoding="utf-8")
    return str(out)


@pytestmark_net
def test_full_conductor_boot_consumer_placement_reaches_remote_gate(tmp_path, certs, capfd):
    """End-to-end conductor form: boot the gate as its own process, then
    `run_placement` the CONSUMER composition — the node `GateUser` probes the
    gate across the `[remotes.gate]` seam by address. The G2 collision is refused
    and the clean candidate admitted, cross-tier, over TCP+mTLS — proving the
    conductor expresses two independent compositions sharing only `service Gate`."""
    reason = _cross_tier_ready()
    if reason:
        pytest.skip(reason)
    port = _free_port()
    gate = _start_gate(tmp_path, certs, port)
    try:
        consumer_placement = _template_consumer_toml(tmp_path, certs, port)
        rc = _placement.run_placement([CONSUMER_RVL], consumer_placement, once=True)
        out = capfd.readouterr().out
    finally:
        gate.terminate()
        gate.wait(timeout=10)
    assert rc == 0, out
    assert "collide" in out and "clean" in out, out
    assert "(G2)" in out, out                                   # refusal crossed TCP
    assert "network seams (item 56): 1 over TCP+mTLS" in out, out
    # the clean candidate admitted across the remote seam (its verdict rides back
    # as a JSON string, so the quotes are escaped in the probe echo).
    assert "admitted" in out and "Extra" in out, out


# ---------------------------------------------------------------------------
# 4. the gate placement is itself a valid standalone deployment: `run_placement`
#    on the provider composition boots the listener and tears down cleanly.
# ---------------------------------------------------------------------------

def _run_bad_remote(tmp_path, remotes, capfd) -> tuple[int, str]:
    """Run a py consumer placement whose `[remotes]` is malformed, and return
    (rc, stderr). A py consumer needs cordis-py to clear preflight, so these
    validation cases are guarded on it."""
    placement = {"remotes": remotes,
                 "processes": {"user": {"components": ["GateUser"],
                                        "seam_deadline": 30.0,
                                        "tls": {"identity": "user"}}}}
    path = tmp_path / "bad_remote.json"
    path.write_text(json.dumps(placement), encoding="utf-8")
    rc = _placement.run_placement([CONSUMER_RVL], str(path), once=True)
    return rc, capfd.readouterr().err


def test_remote_validation_diagnostics(tmp_path, capfd):
    """The `[remotes]` guardrails: an unknown service, and a remote no process
    requires, are refused with a single diagnostic before anything is spawned."""
    if not _placement._cordis_py_installed():
        pytest.skip("cordis-py runtime not installed (sh backends/python/setup.sh)")

    rc, err = _run_bad_remote(tmp_path, {"gate": {"service": "Nope",
                                                  "host": HOST, "port": 1}}, capfd)
    assert rc != 0 and "does not declare" in err, err

    rc, err = _run_bad_remote(tmp_path, {"ghost": {"service": "Gate",
                                                   "host": HOST, "port": 1}}, capfd)
    assert rc != 0 and "required by no process" in err, err

    rc, err = _run_bad_remote(tmp_path, {"gate": {"service": "Gate",
                                                  "host": HOST}}, capfd)
    assert rc != 0 and "needs both host and port" in err, err


def test_provider_placement_boots_standalone(tmp_path, capfd):
    """The gate half runs on its own placement (no consumer present): it comes
    up, serves its full provided surface behind the mTLS listener, and tears
    down. Needs cordis-py only; skips otherwise."""
    if not _placement._cordis_py_installed():
        pytest.skip("cordis-py runtime not installed (sh backends/python/setup.sh)")
    if shutil.which("openssl") is None:
        pytest.skip("openssl CLI not available for test cert generation")
    minted = _placement.generate_seam_certs(tmp_path / "pcerts", ["gate"])
    port = _free_port()
    conf = tomllib.loads(
        (ROOT / "examples" / "placement" / "gate_provider_network.toml").read_text())
    conf["processes"]["gate"]["address"]["port"] = port
    tls = conf["processes"]["gate"]["tls"]
    tls["cert"] = minted["gate"]["cert"]
    tls["key"] = minted["gate"]["key"]
    tls["ca"] = minted["gate"]["ca"]
    placement_path = tmp_path / "provider_placement.json"
    placement_path.write_text(json.dumps(conf), encoding="utf-8")
    rc = _placement.run_placement([PROVIDER_RVL], str(placement_path), once=True)
    out = capfd.readouterr().out
    assert rc == 0, out
    assert "network seams" not in out  # no consumer here, but it still serves
    assert "gate[py]" in out, out
