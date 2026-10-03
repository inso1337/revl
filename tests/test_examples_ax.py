"""examples/ax: revl as the MCP gate inside a google/ax Workspace (issue #1464).

The Workspace in `examples/ax/ax.yaml` wires exactly one MCP server into an AX
task, `revl mcp serve`, launched through `sh -c` with its working directory and
environment set inline. These tests run that server the way an agent inside the
sandbox would: they materialise the Workspace's inlined files the way AX's
runner does (relative paths land under the workspace mount), rewrite the mount
path `/workspace/app` to a temporary directory, start the server from the
manifest's own `command` and `args`, and speak MCP to it over stdio.

What is checked here is revl's half. AX's half, whether the manifest decodes
and validates under AX's own schema code, is `examples/ax/validate.sh`, which
needs Go and network access and so does not run in this suite.

The last two tests need the pinned cordis-py runtime (they boot the
composition) and skip without it; CI's `frontend-cordis` job runs them. They
file a ticket through the gate, SIGKILL the server (what AX does to a task's
processes after the 10 s suspend grace, and what deletion does outright), then
run the manifest's `after-agent.sh`, which calls `revl recover`, and read the
verdict it leaves behind.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import queue
import re
import shlex
import signal
import subprocess
import sys
import threading
from pathlib import Path

import pytest
import yaml  # the `test` extra; a hard import so a missing parser errors, not skips

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "ax"
MOUNT = "/workspace/app"
TITLE = "disk full on build-7"
RPC_TIMEOUT = 180

needs_cordis = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="booting the gated composition needs the pinned cordis-py runtime "
           "(sh backends/python/setup.sh); CI's frontend-cordis job runs this")


# ------------------------------------------------------------------ manifest

def _documents() -> dict:
    docs = [d for d in yaml.safe_load_all((EXAMPLE / "ax.yaml").read_text()) if d]
    return {d["kind"]: d for d in docs}


def _gate_server() -> dict:
    servers = _documents()["Workspace"]["spec"]["mcp"]["servers"]
    return next(s for s in servers if s["name"] == "revl-gate")


def _agent_source() -> str:
    """The agent composition the README tells an agent to load: the first
    ```revl block in examples/ax/README.md, so the doc and the test agree."""
    text = (EXAMPLE / "README.md").read_text(encoding="utf-8")
    match = re.search(r"```revl\n(.*?)```", text, re.S)
    assert match, "examples/ax/README.md has no ```revl block"
    return match.group(1)


def test_the_pinned_ax_commit_is_the_same_everywhere():
    pin = re.search(r"^AX_COMMIT=([0-9a-f]{40})$",
                    (EXAMPLE / "validate.sh").read_text(encoding="utf-8"), re.M)
    assert pin, "validate.sh does not pin a full AX commit"
    for name in ("ax.yaml", "Dockerfile", "README.md"):
        text = (EXAMPLE / name).read_text(encoding="utf-8")
        assert pin.group(1) in text, f"{name} does not name AX commit {pin.group(1)}"
        others = set(re.findall(r"google/ax[^\n]*?\b([0-9a-f]{40})\b", text)) - {pin.group(1)}
        assert not others, f"{name} names another AX commit: {others}"


def test_the_task_binds_the_workspace_at_the_mount_every_path_assumes():
    docs = _documents()
    binding = docs["Task"]["spec"]["workspaces"]
    assert binding == [{"name": docs["Workspace"]["metadata"]["name"], "path": MOUNT}]
    assert docs["Task"]["spec"]["command"][:2] == ["sh", f"{MOUNT}/.revl/after-agent.sh"]


def test_the_gate_is_a_stdio_server_whose_flags_revl_accepts():
    """The flags in the manifest are parsed by revl's own CLI parser, so a
    renamed or removed flag fails here instead of inside someone's cluster."""
    from revl.cli.parser import build_parser

    server = _gate_server()
    assert "endpoint" not in server and server["command"] == "sh"
    assert server["args"][0] == "-c"
    words = shlex.split(server["args"][1])
    start = words.index("revl")
    assert words[start:start + 3] == ["revl", "mcp", "serve"]
    args = build_parser().parse_args(words[start + 1:])
    assert args.mcp_command == "serve"
    assert args.author_trust == "untrusted"
    assert args.provider == ["tickets.rvl"]
    assert args.grant == ["Tickets"]
    assert args.root == [MOUNT]
    # issue #1706: the example's agent approves its own tickets, which is the
    # explicit `advisory` mode now that the default refuses it
    assert args.approval_policy == "advisory"
    env = dict(w.split("=", 1) for w in words[:start] if "=" in w)
    assert env["REVL_WAL_DIR"] == f"{MOUNT}/.revl/wal"


# ------------------------------------------------------- the sandbox, locally

class _Sandbox:
    """A temporary stand-in for the task's /workspace/app, populated the way
    AX's runner populates it (internal/workspace/setup.go, writeFiles)."""

    def __init__(self, tmp_path: Path):
        self.mount = tmp_path / "app"
        for entry in _documents()["Workspace"]["spec"]["files"]:
            dest = self.mount / entry["path"]
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(entry["content"], encoding="utf-8")
        # the image puts the `revl` console script on PATH; here a shim runs
        # this interpreter's revl, so the manifest's own command lines run.
        shim_dir = tmp_path / "bin"
        shim_dir.mkdir()
        shim = shim_dir / "revl"
        shim.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} -P -m revl "$@"\n')
        shim.chmod(0o755)
        self.env = dict(os.environ, PATH=f"{shim_dir}{os.pathsep}{os.environ['PATH']}")
        self.env.pop("REVL_WAL_DIR", None)

    def local(self, text: str) -> str:
        return text.replace(MOUNT, str(self.mount))

    @property
    def revl_dir(self) -> Path:
        return self.mount / ".revl"

    def wals(self) -> list:
        return sorted((self.revl_dir / "wal").glob("*.wal"))

    def outbox(self) -> str:
        path = self.revl_dir / "outbox.log"
        return path.read_text(encoding="utf-8") if path.exists() else ""


class _Gate:
    """The revl-gate MCP server, started from the manifest's command and args."""

    def __init__(self, sandbox: _Sandbox):
        server = _gate_server()
        argv = [server["command"], *(sandbox.local(a) for a in server["args"])]
        self.proc = subprocess.Popen(
            argv, env=sandbox.env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True)
        self._lines: queue.Queue = queue.Queue()
        threading.Thread(target=self._pump, daemon=True).start()
        self._id = 0

    def _pump(self) -> None:
        for line in self.proc.stdout:
            self._lines.put(line)
        self._lines.put(None)

    def rpc(self, method: str, params: dict | None = None) -> dict:
        self._id += 1
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": self._id,
                                          "method": method, "params": params or {}}) + "\n")
        self.proc.stdin.flush()
        line = self._lines.get(timeout=RPC_TIMEOUT)
        assert line is not None, f"the gate exited: {self.proc.stderr.read()}"
        return json.loads(line)

    def tool(self, name: str, arguments: dict) -> dict:
        response = self.rpc("tools/call", {"name": name, "arguments": arguments})
        return json.loads(response["result"]["content"][0]["text"])

    def kill(self) -> None:
        """SIGKILL, no teardown: what a suspend's grace expiry or a delete does."""
        if self.proc.poll() is None:
            os.kill(self.proc.pid, signal.SIGKILL)
        self.proc.wait(timeout=30)

    def close(self) -> None:
        self.kill()
        for stream in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
            stream.close()


@pytest.fixture
def gate(tmp_path):
    sandbox = _Sandbox(tmp_path)
    server = _Gate(sandbox)
    try:
        init = server.rpc("initialize", {"protocolVersion": "2025-06-18",
                                         "capabilities": {},
                                         "clientInfo": {"name": "ax-agent", "version": "0"}})
        assert init["result"]["serverInfo"]["name"] == "revl"
        yield sandbox, server
    finally:
        server.close()


def test_the_gate_answers_over_stdio_and_admits_the_readme_agent(gate):
    _, server = gate
    names = {t["name"] for t in server.rpc("tools/list")["result"]["tools"]}
    assert {"revl_load", "revl_call", "revl_approve", "revl_commit",
            "revl_commit_confirm"} <= names
    verdict = server.tool("revl_check", {"source": _agent_source()})
    assert verdict["ok"] is True, verdict


def test_the_agent_cannot_bring_its_own_host_code(gate):
    """The untrusted-author profile: an agent that declares an extern of its
    own, instead of composing the operator's Tickets service, is refused."""
    _, server = gate
    rogue = (
        "service Leak { emission fn send(x: Str) }\n"
        "extern emission fn post(x: Str) = @py { return }\n"
        "component Rogue provides leak: Leak {\n"
        "  provide leak { fn send(x) { emit post(x) } }\n"
        "}\n")
    from revl.compiler import compile_source

    compile_source(rogue, "rogue.rvl")   # control: it compiles when the author is trusted
    verdict = server.tool("revl_check", {"source": rogue})
    assert verdict["ok"] is False
    assert "untrusted-author profile forbids new `extern`" in json.dumps(verdict["diagnostics"])


# ------------------------------------------------------ what recover reports

def _file_a_ticket(server: _Gate) -> None:
    loaded = server.tool("revl_load", {"source": _agent_source(), "record": True})
    assert loaded["ok"] is True, loaded
    call = {"key": "triage", "method": "report", "args": [TITLE]}
    first = server.tool("revl_call", call)
    # `emission` with no inverse is class (c): nothing fires without a yes.
    assert first.get("approvalRequired") is True, first
    approved = server.tool("revl_approve", {"hash": first["ticket"]["hash"]})
    assert approved["ok"] is True, approved
    assert server.tool("revl_call", call)["ok"] is True


def _after_agent(sandbox: _Sandbox) -> tuple[int, str, dict]:
    """Run the manifest's after-agent.sh with an agent that already exited.
    Returns its exit status, its stdout, and the one verdict it wrote."""
    done = subprocess.run(
        ["sh", str(sandbox.revl_dir / "after-agent.sh"), "true"],
        env=sandbox.env, capture_output=True, text=True, timeout=RPC_TIMEOUT)
    verdicts = sorted((sandbox.revl_dir / "recovery").glob("*.json"))
    assert len(verdicts) == 1, (done.stdout, done.stderr)
    return (done.returncode, done.stdout,
            json.loads(verdicts[0].read_text(encoding="utf-8")))


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@needs_cordis
def test_a_task_killed_before_commit_leaves_a_residue_verdict(gate):
    sandbox, server = gate
    _file_a_ticket(server)
    assert sandbox.outbox() == f"filed:{TITLE}\n"
    [wal] = sandbox.wals()
    server.kill()
    before = _digest(wal)

    status, stdout, verdict = _after_agent(sandbox)

    assert status == 0, "after-agent.sh must pass the agent's own exit status through"
    assert "exit 1 (0 clean, 1 residue)" in stdout
    assert verdict["verdict"] == "rolled-back"
    assert verdict["residue"]["clean"] is False
    still_out = [r for r in verdict["unreconstructible"] if r["label"] == "tickets.file"]
    assert len(still_out) == 1 and TITLE in still_out[0]["still_out"], verdict
    assert still_out[0]["reason"].startswith("an emission is a one-way crossing")
    # `revl recover` reads the log and evaluates against its own model of the
    # world. It neither calls the ticket system nor rewrites the WAL.
    assert sandbox.outbox() == f"filed:{TITLE}\n"
    assert _digest(wal) == before


@needs_cordis
def test_a_committed_session_recovers_clean(gate):
    sandbox, server = gate
    _file_a_ticket(server)
    manifest = server.tool("revl_commit", {})
    assert manifest["ok"] is True, manifest
    confirmed = server.tool("revl_commit_confirm", {"hash": manifest["manifest"]["hash"]})
    assert confirmed["ok"] is True and confirmed["committed"] is True, confirmed
    server.kill()

    _, stdout, verdict = _after_agent(sandbox)

    assert "exit 0 (0 clean, 1 residue)" in stdout
    assert verdict["verdict"] == "rolled-forward", verdict
    assert verdict["residue"]["clean"] is True, verdict
