"""The Go tier as a live interop-bridge participant (docs/interop-bridge.md).

A Go process *consumes* a service another tier provides across a Unix-socket
seam, and *withdraws reactively* when the provider dies (R2/R3). Modelled on the
rust/java placement path: the Go runner is built per composition by
`placement._build_go` (emit + `go build`), then driven directly.

The provider here is a stdlib-only `backends/python/bridge.serve` process (a
fake `ctx.get('db')` service) rather than a full cordis-py runner, so the test
needs no cordis in the pytest interpreter — only a `go` toolchain, without which
it skips cleanly. It exercises the real wire: the Go proxy's `db.execute` RPC
crosses into the provider, and killing the provider fires the Go consumer's
reactive withdrawal.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_files  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("go") is None,
                                reason="no go toolchain on PATH")

_BRIDGE = ROOT / "backends" / "python" / "bridge.py"

# A minimal provider: serve `db` (query, execute) over the bridge with a fake
# ctx, recording every method it is asked to dispatch. Stdlib only — no cordis.
_PROVIDER_SRC = r'''
import asyncio, importlib.util, sys

bridge_path, sock, record = sys.argv[1], sys.argv[2], sys.argv[3]
spec = importlib.util.spec_from_file_location("bridge", bridge_path)
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)

class _Svc:
    def query(self, sql):
        with open(record, "a") as f: f.write("query\n")
        return []
    def execute(self, sql):
        with open(record, "a") as f: f.write("execute:" + sql + "\n")
        return 0

class _Ctx:
    def get(self, key):
        return _Svc() if key == "db" else None

async def main():
    await bridge.serve(_Ctx(), {"db": ["query", "execute"]}, sock)
    print("PROVIDER_UP", flush=True)
    await asyncio.Event().wait()

asyncio.run(main())
'''


def _placement():
    from revl import placement  # noqa: PLC0415

    return placement


def _read_until(proc: subprocess.Popen, needle: str, timeout: float) -> list[str]:
    """Collect the child's stdout lines until one contains `needle` or timeout.
    Runs a reader thread so a never-arriving line cannot block forever."""
    import threading  # noqa: PLC0415

    lines: list[str] = []
    hit = threading.Event()

    def pump() -> None:
        for line in proc.stdout:
            lines.append(line)
            if needle in line:
                hit.set()
                return

    thread = threading.Thread(target=pump, daemon=True)
    thread.start()
    hit.wait(timeout)
    return lines


def test_go_consumer_crosses_a_seam_and_withdraws_when_provider_dies(tmp_path):
    placement = _placement()
    ir = compile_files([str(ROOT / "examples" / "user_cache.rvl")])

    # build the go runner for this composition (emit + go build)
    go_bin = placement._build_go(ir, tmp_path)
    assert Path(go_bin).exists()

    # A short socket dir: AF_UNIX paths cap at ~104 bytes, well under pytest's
    # deep tmp_path. Cleaned in the finally.
    sockdir = tempfile.mkdtemp(prefix="rvlgo_")
    sock = str(Path(sockdir) / "p.sock")
    record = str(tmp_path / "dispatched.log")

    provider_py = tmp_path / "provider.py"
    provider_py.write_text(_PROVIDER_SRC, encoding="utf-8")
    provider = subprocess.Popen(
        [sys.executable, str(provider_py), str(_BRIDGE), sock, record],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )

    consumer = None
    try:
        # wait for the provider's socket to exist
        for _ in range(200):
            if os.path.exists(sock):
                break
            if provider.poll() is not None:
                out = provider.stdout.read() if provider.stdout else ""
                pytest.fail(f"provider died before serving:\n{out}")
            time.sleep(0.05)
        assert os.path.exists(sock), "provider never created its socket"

        # the Go consumer: proxy `db` from the provider, provide `cache`, probe
        consumer_spec = {
            "name": "consumer",
            "backend": "go",
            "components": ["UserCache"],
            "config": {},
            "provides": ["cache"],
            "proxies": {"db": {"socket": sock, "methods": ["query", "execute"],
                               "service": "Database"}},
            "probe": [
                {"key": "cache", "method": "put", "args": ["alice", "42"]},
                {"key": "cache", "method": "get", "args": ["alice"]},
            ],
        }
        spec_file = tmp_path / "consumer.spec.json"
        spec_file.write_text(json.dumps(consumer_spec), encoding="utf-8")

        consumer = subprocess.Popen(
            [go_bin, str(spec_file)],
            stdin=subprocess.PIPE,  # keep open: the runner stops on stdin EOF
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )

        up_lines = _read_until(consumer, "[consumer] UP", timeout=20)
        joined = "".join(up_lines)
        assert "[consumer] UP" in joined, f"consumer never came up:\n{joined}"

        # the seam crossed: cache.put's `emit db.execute(...)` reached the
        # provider, and cache.get returned the locally-stored value.
        assert '=> "42"' in joined, f"probe did not cross correctly:\n{joined}"
        dispatched = Path(record).read_text(encoding="utf-8") if os.path.exists(record) else ""
        assert "execute:" in dispatched, f"db.execute never crossed the seam:\n{dispatched}"

        # kill the provider: the monitor connection hits EOF, so the Go consumer
        # withdraws its proxy and UserCache deactivates reactively (R2/R3).
        provider.send_signal(signal.SIGKILL)
        provider.wait(timeout=5)

        # after withdrawal the runner tears down and exits; collect the rest.
        down_lines = _read_until(consumer, "[consumer] DOWN", timeout=10)
        tail = joined + "".join(down_lines)
        assert "provider died" in tail, f"peer death not detected:\n{tail}"
        # the consumer left Active — the reactive withdrawal fired.
        withdraw = [l for l in tail.splitlines() if "withdraw" in l and "UserCache" in l]
        assert withdraw, f"UserCache did not withdraw:\n{tail}"
        assert "state=active" not in withdraw[0], (
            f"UserCache stayed active after the provider died: {withdraw[0]}")
    finally:
        for proc in (consumer, provider):
            if proc is None:
                continue
            if proc.poll() is None:
                try:
                    if proc is consumer and proc.stdin:
                        proc.stdin.close()
                    proc.terminate()
                    proc.wait(timeout=5)
                except (OSError, subprocess.TimeoutExpired):
                    proc.kill()
        shutil.rmtree(sockdir, ignore_errors=True)


def test_go_v3_typed_core_records_and_adts_cross_the_seam(tmp_path):
    """The v3 typed-core bridge carries rich values (FR-8 go follow-up): a Go
    process SERVES the record/ADT service boundary of a v3 typed-core
    composition, and a python bridge client calls through it — a record
    return, an ADT-typed argument and return, and an ADT `match` inside the
    method body all round-trip on the canonical wire (records as plain JSON
    objects, ADTs as {"$kind","$value"} — docs/interop-bridge.md §3)."""
    placement = _placement()
    ir = compile_files([str(ROOT / "examples" / "v3_step_scheduler.rvl")])
    go_bin = placement._build_go(ir, tmp_path)
    assert Path(go_bin).exists()

    sockdir = tempfile.mkdtemp(prefix="rvlgo3_")
    sock = str(Path(sockdir) / "sched.sock")

    spec = {
        "name": "schedsvc",
        "backend": "go",
        "components": ["Sched"],
        "config": {},
        "provides": ["sched"],
        "proxies": {},
        "serve": {
            "socket": sock,
            "keys": ["sched"],
            "methods": {"Scheduler": ["describe", "next", "report"]},
        },
        "probe": [],
    }
    spec_file = tmp_path / "serve.spec.json"
    spec_file.write_text(json.dumps(spec), encoding="utf-8")

    svc = subprocess.Popen(
        [go_bin, str(spec_file)],
        stdin=subprocess.PIPE,  # keep open: the runner stops on stdin EOF
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    try:
        up_lines = _read_until(svc, "[schedsvc] UP", timeout=20)
        joined = "".join(up_lines)
        assert "[schedsvc] UP" in joined, f"server never came up:\n{joined}"
        assert os.path.exists(sock), "server never created its socket"

        spec2 = importlib.util.spec_from_file_location("python_bridge", _BRIDGE)
        bridge_mod = importlib.util.module_from_spec(spec2)
        spec2.loader.exec_module(bridge_mod)
        client = bridge_mod._Client(sock)
        try:
            # ADT-typed argument crosses in, the match runs, a Str comes back
            final = {"$kind": "Final", "$value": "done"}
            assert client.call("sched", "describe", [final]) == "done"
            need = {"$kind": "NeedTool",
                    "$value": {"name": "revl", "args": ["a", "b"]}}
            assert client.call("sched", "describe", [need]) == "revl"
            # the ADT round-trips back out through the encode helper
            assert client.call("sched", "next", [final]) == final
            assert client.call("sched", "next", [need]) == need
            # record service return crosses as plain JSON
            assert client.call("sched", "report", []) == {"id": 1, "name": "ada"}
        finally:
            client.close()
    finally:
        if svc.poll() is None:
            try:
                if svc.stdin:
                    svc.stdin.close()
                svc.terminate()
                svc.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                svc.kill()
