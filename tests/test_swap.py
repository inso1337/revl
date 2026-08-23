"""Verified live migration across tiers — the handover (roadmap §23).

`revl swap <component> --to <backend>` boots a candidate provider on the target
tier, admits it against the running manifest, **re-points** the consumers'
proxies from the old socket to the new one, then drains and tears the old
provider down and proves no residue (docs/swap.md).

The genuinely new engineering is the *re-point*: the per-proxy monitor thread
already knew how to *withdraw* on peer death (R2/R3); a planned cutover teaches
it to reconnect to a **successor** instead, so the seam does not blink. That
mechanism lives entirely in `backends/python/bridge.py::_Client` and needs no
cordis — so this suite drives it over real Unix sockets with a minimal
protocol-speaking provider, and holds a live consumer calling *across* the
cutover to prove the "REPL keeps answering" contract. What is proven here is
the socket-level handover (re-point + drain-v1 + no spurious withdrawal + swap
back) and the admission gate's refusal. The full multi-process
`revl run --placement` interactive swap wires the same `_Client.repoint` behind
the conductor's control channel; its end-to-end boot needs the cordis-py
runtime and is exercised where that runtime is present.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


@pytest.fixture
def sockdir():
    """A short-pathed directory for Unix sockets. pytest's `tmp_path` lives
    under a deep `/private/var/folders/...` prefix that blows the ~104-char
    `AF_UNIX` `sun_path` limit on macOS, so sockets get their own short dir."""
    directory = tempfile.mkdtemp(prefix="rvsw", dir="/tmp")
    try:
        yield Path(directory)
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def _bridge():
    """backends/python/bridge.py, imported directly (it needs no cordis)."""
    spec = importlib.util.spec_from_file_location(
        "revl_swap_test_bridge", ROOT / "backends" / "python" / "bridge.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


bridge = _bridge()


# ---------------------------------------------------------------------------
# a minimal provider that speaks the bridge wire protocol on a Unix socket
# ---------------------------------------------------------------------------


class _Provider:
    """Answers `{"key","method","args"}` with a version tag, on a real Unix
    socket, exactly as `bridge.serve` does — but synchronous, so a test has
    deterministic control over when connections drop (peer death). A monitor
    connection never sends, so its handler blocks reading until `stop()` shuts
    the socket down, which is the EOF the client's watcher keys on."""

    def __init__(self, sock_path: str, version: str) -> None:
        self.version = version
        self.calls = 0
        self._stopping = False
        self._conns: list[socket.socket] = []
        self._lock = threading.Lock()
        self._srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._srv.bind(sock_path)
        self._srv.listen(16)
        self._thread = threading.Thread(target=self._accept, daemon=True)
        self._thread.start()

    def _accept(self) -> None:
        while not self._stopping:
            try:
                conn, _ = self._srv.accept()
            except OSError:
                break
            with self._lock:
                if self._stopping:
                    conn.close()  # accepted during shutdown: close it here so
                    break         # its peer sees EOF and no conn is orphaned
                self._conns.append(conn)
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn: socket.socket) -> None:
        stream = conn.makefile("rwb")
        try:
            for line in stream:
                req = json.loads(line)
                self.calls += 1
                value = self.version if req.get("method") == "get" else None
                stream.write((json.dumps({"ok": True, "value": value}) + "\n").encode())
                stream.flush()
        except OSError:
            pass

    def stop(self) -> None:
        with self._lock:
            self._stopping = True
            conns = list(self._conns)  # snapshot: a conn accepted after this is
            #                            closed by _accept's own stopping-check
        try:
            self._srv.close()
        except OSError:
            pass
        for conn in conns:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                conn.close()
            except OSError:
                pass


class _Caller(threading.Thread):
    """A stand-in for the live REPL: calls `get` in a tight loop and records
    every answer (and any error), until stopped. It never pauses for the swap —
    that is the point: the cutover must be invisible to it."""

    def __init__(self, client) -> None:
        super().__init__(daemon=True)
        self._client = client
        self._stopping = False
        self.results: list[str] = []
        self.errors: list[str] = []

    def run(self) -> None:
        while not self._stopping:
            try:
                self.results.append(self._client.call("cache", "get", []))
            except Exception as exc:  # noqa: BLE001 — record, do not crash
                self.errors.append(f"{type(exc).__name__}: {exc}")
            time.sleep(0.001)

    def stop(self) -> None:
        self._stopping = True
        self.join(timeout=5)


# ---------------------------------------------------------------------------
# the re-point: a planned cutover carries the seam to a successor
# ---------------------------------------------------------------------------


def test_repoint_keeps_answering_across_the_cutover(sockdir):
    s1, s2, s3 = (str(sockdir / f"p{n}.sock") for n in (1, 2, 3))
    v1 = _Provider(s1, "v1")
    client = bridge._Client(s1)
    lost: list[int] = []
    client.watch(lambda: lost.append(1))

    caller = _Caller(client)
    caller.start()
    # let it settle onto v1
    while not caller.results:
        time.sleep(0.005)
    assert caller.results[-1] == "v1"

    # boot the successor and re-point (planned cutover)
    v2 = _Provider(s2, "v2")
    client.repoint(s2)
    # after repoint returns, every call goes to the successor
    assert client.call("cache", "get", []) == "v2"

    # drain + tear the old provider down — this must NOT read as a death
    v1.stop()
    time.sleep(0.2)
    assert lost == [], "planned cutover must not fire the peer-death withdrawal"

    # swap back onto a fresh generation (distinct tag so we can see the cutover)
    v3 = _Provider(s3, "v3")
    client.repoint(s3)
    v2.stop()
    time.sleep(0.2)
    assert client.call("cache", "get", []) == "v3"
    assert lost == [], "the swap-back is also planned — still no withdrawal"

    caller.stop()
    client.close()
    v3.stop()

    # the live caller never saw a broken seam, and saw every generation
    assert caller.errors == [], caller.errors
    assert {"v1", "v2", "v3"} <= set(caller.results)
    # and the cutovers are monotonic: v1 before v2 before v3, never backwards
    firsts = [caller.results.index(v) for v in ("v1", "v2", "v3")]
    assert firsts == sorted(firsts), caller.results
    assert "v1" not in caller.results[firsts[1]:]  # v1 gone once v2 is live
    assert "v2" not in caller.results[firsts[2]:]  # v2 gone once v3 is live


def test_unplanned_peer_death_still_withdraws(sockdir):
    """The re-point must not regress the existing dispose-on-death path: with
    no successor, a provider that dies fires `on_lost` (R2/R3 withdrawal)."""
    sock = str(sockdir / "provider.sock")
    provider = _Provider(sock, "v1")
    client = bridge._Client(sock)
    fired = threading.Event()
    client.watch(fired.set)

    assert client.call("cache", "get", []) == "v1"
    provider.stop()  # unplanned death — no repoint preceded it
    assert fired.wait(5), "an unplanned peer death must still withdraw"
    client.close()


def test_repoint_that_cannot_reach_the_successor_leaves_the_client_live(sockdir, monkeypatch):
    """Admission's dry side of the guarantee: if the successor cannot be dialled,
    `repoint` raises before touching the live connections, so the running seam
    is untouched and the swap can be refused with nothing re-pointed."""
    sock = str(sockdir / "provider.sock")
    provider = _Provider(sock, "v1")
    client = bridge._Client(sock)
    assert client.call("cache", "get", []) == "v1"

    def _refuse(_path, *a, **k):
        raise ConnectionError("successor did not come up")

    monkeypatch.setattr(bridge, "_connect", _refuse)
    with pytest.raises(ConnectionError):
        client.repoint(str(sockdir / "successor.sock"))

    # the live client still talks to the original provider — no blip
    monkeypatch.undo()
    assert client.call("cache", "get", []) == "v1"
    client.close()
    provider.stop()


# ---------------------------------------------------------------------------
# the admission gate: a candidate that breaks a consumer call site is refused,
# and the running composition is untouched
# ---------------------------------------------------------------------------


from revl.placement import swap_admission  # noqa: E402
from revl import compile_files  # noqa: E402


_CACHE_RVL = """
service Cache {
  async fn get(k: Str) -> Opt[Str]
  async fn put(k: Str, v: Str)
}
service ApiSvc { async fn hit() -> Opt[Str] }

component MemCache provides cache: Cache {
  let m = effect Map.new() undo m.drop()
  provide cache {
    async fn get(k) = m.get(k)
    async fn put(k, v) = v
  }
}

component Api requires cache: Cache provides api: ApiSvc {
  provide api { async fn hit() = cache.get("k") }
}
"""


def _write(tmp_path, name, source):
    path = tmp_path / name
    path.write_text(source, encoding="utf-8")
    return str(path)


def test_admission_admits_a_faithful_tier_swap(tmp_path):
    """Re-hosting a component with the same declared surface admits: every
    consumer call site stays valid across the seam."""
    src = _write(tmp_path, "app.rvl", _CACHE_RVL)
    running = compile_files([src])
    candidate, error = swap_admission([src], running, "MemCache", "rust")
    assert error is None, error
    assert candidate is not None


def test_admission_refuses_a_candidate_that_breaks_a_consumer(tmp_path):
    """A candidate whose service drops the operation a running consumer calls is
    refused with a guarantee-naming diagnostic; nothing is re-pointed."""
    src = _write(tmp_path, "app.rvl", _CACHE_RVL)
    running = compile_files([src])
    # the candidate redeclares Cache without `get` — Api's call site dangles
    broken = _CACHE_RVL.replace('  async fn get(k: Str) -> Opt[Str]\n', '', 1)
    broken = broken.replace('    async fn get(k) = m.get(k)\n', '', 1)
    cand_src = _write(tmp_path, "candidate.rvl", broken)
    candidate, error = swap_admission([cand_src], running, "MemCache", "rust")
    assert candidate is None
    assert error is not None
    # the diagnostic names the guarantee, not just "it broke"
    assert "get" in error


def test_admission_refuses_a_service_that_cannot_cross_a_tier(tmp_path):
    """A tier swap moves the component to another process; a service that is
    address-space-bound (a sync method / emission / resource return) cannot
    cross that seam, so the swap is refused before anything is booted."""
    bound = """
service Cache {
  fn get(k: Str) -> Str
}

service ApiSvc { fn hit() -> Str }

component MemCache provides cache: Cache {
  provide cache {
    fn get(k) = k
  }
}

component Api requires cache: Cache provides api: ApiSvc {
  provide api { fn hit() = cache.get("k") }
}
"""
    src = _write(tmp_path, "bound.rvl", bound)
    running = compile_files([src])
    candidate, error = swap_admission([src], running, "MemCache", "rust")
    assert candidate is None
    assert error is not None
    assert "address-space-bound" in error or "transport" in error


# ---------------------------------------------------------------------------
# the conductor: `revl run --placement` driving a live swap (boot -> admit ->
# re-point -> drain + tear down). Real orchestration, scripted child processes,
# so it runs without the cordis-py runtime. The end-to-end version against real
# cordis children is the multi-process form of the socket handover above.
# ---------------------------------------------------------------------------


import queue  # noqa: E402
import builtins  # noqa: E402
from revl import placement as _placement  # noqa: E402


class _Stdout:
    """A blocking, line-iterable stream fed over time — enough of a `Popen`
    stdout for the conductor's pump thread."""

    def __init__(self):
        self._q: queue.Queue = queue.Queue()

    def push(self, line: str) -> None:
        self._q.put(line if line.endswith("\n") else line + "\n")

    def close(self) -> None:
        self._q.put(None)

    def __iter__(self):
        return self

    def __next__(self):
        item = self._q.get()
        if item is None:
            raise StopIteration
        return item


class _Stdin:
    """A control channel that answers a `repoint` command the way the real
    process runner does — by emitting a `REPOINTED` acknowledgement."""

    def __init__(self, proc):
        self._proc = proc
        self.written: list[str] = []

    def write(self, text: str) -> None:
        self.written.append(text)
        for line in text.splitlines():
            try:
                cmd = json.loads(line)
            except json.JSONDecodeError:
                continue
            if cmd.get("op") == "repoint":
                self._proc.stdout.push(
                    f"[{self._proc.name}] REPOINTED {cmd['key']} -> {cmd['socket']}")

    def flush(self):
        pass

    def close(self):
        pass


class _ScriptedProc:
    """A stand-in for a placement child: comes up immediately, acknowledges
    repoints, and on teardown emits the per-process no-residue proof the real
    runner now prints before `DOWN`."""

    def __init__(self, name: str):
        self.name = name
        self.stdout = _Stdout()
        self.stdin = _Stdin(self)
        self._alive = True
        self.stdout.push(f"[{name}] UP")

    def poll(self):
        return None if self._alive else 0

    def wait(self, timeout=None):
        self._alive = False
        return 0

    def terminate(self):
        if self._alive:
            self._alive = False
            self.stdout.push(f"[{self.name}] residue no residue | "
                             f"registry=0 provisions=[] disposables=0/0")
            self.stdout.push(f"[{self.name}] DOWN")
            self.stdout.close()

    def kill(self):
        self.terminate()


_SWAPPABLE_APP = """
service Cache {
  async fn get(k: Str) -> Opt[Str]
  async fn put(k: Str, v: Str)
}
service App { async fn run() -> Opt[Str] }

component MemCache provides cache: Cache {
  let m = effect Map.new() undo m.drop()
  provide cache {
    async fn get(k) = m.get(k)
    async fn put(k, v) = v
  }
}
component Consumer requires cache: Cache provides app: App {
  provide app { async fn run() = cache.get("k") }
}
"""

_SWAPPABLE_PLACEMENT = """
[processes.provider]
components = ["MemCache"]

[processes.consumer]
components = ["Consumer"]
"""


def _wire_conductor(tmp_path, monkeypatch, inputs):
    """Set the conductor up to run interactively with scripted children and a
    scripted swap-REPL input. Returns the {name: _ScriptedProc} registry."""
    procs: dict = {}

    def fake_popen(cmd, **kwargs):
        spec = json.loads(Path(cmd[-1]).read_text(encoding="utf-8"))
        proc = _ScriptedProc(spec["name"])
        procs[spec["name"]] = proc
        return proc

    monkeypatch.setattr(_placement, "_cordis_py_installed", lambda: True)
    monkeypatch.setattr(_placement.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(_placement, "_interactive", lambda: True)

    feed = iter(inputs)

    def fake_input(prompt=""):
        try:
            return next(feed)
        except StopIteration:
            raise EOFError from None

    monkeypatch.setattr(builtins, "input", fake_input)
    return procs


def test_conductor_swaps_a_live_component_and_unwinds_the_old_provider(tmp_path, monkeypatch):
    app = _write(tmp_path, "app.rvl", _SWAPPABLE_APP)
    plc = _write(tmp_path, "app.toml", _SWAPPABLE_PLACEMENT)
    procs = _wire_conductor(tmp_path, monkeypatch, ["swap MemCache --to py", ":q"])

    rc = _placement.run_placement([app], plc, once=False)
    assert rc == 0

    # a successor was booted on the target tier ...
    successors = [n for n in procs if n.startswith("MemCache__t")]
    assert successors, f"no successor booted (procs: {list(procs)})"
    succ = successors[0]

    # ... the consumer was re-pointed onto the successor's new socket ...
    consumer = procs["consumer"]
    repoints = [json.loads(w) for w in consumer.stdin.written if w.strip()]
    assert any(c.get("op") == "repoint" and c.get("key") == "cache"
               and succ in c.get("socket", "") for c in repoints), repoints

    # ... and the old provider was torn down (drain + LIFO teardown).
    assert procs["provider"].poll() == 0


def test_conductor_refuses_an_inadmissible_swap_and_boots_no_successor(tmp_path, monkeypatch):
    """A component whose service cannot cross a tier is refused at admission;
    no candidate is ever booted and the running composition is untouched."""
    bound_app = _SWAPPABLE_APP.replace(
        "async fn get(k: Str) -> Opt[Str]", "fn get(k: Str) -> Opt[Str]", 1
    ).replace("async fn get(k) = m.get(k)", "fn get(k) = m.get(k)", 1)
    app = _write(tmp_path, "bound_app.rvl", bound_app)
    plc = _write(tmp_path, "bound_app.toml", _SWAPPABLE_PLACEMENT)
    procs = _wire_conductor(tmp_path, monkeypatch, ["swap MemCache --to rust", ":q"])

    rc = _placement.run_placement([app], plc, once=False)
    assert rc == 0

    # nothing was booted beyond the two original processes — no successor
    assert not [n for n in procs if "__t" in n], list(procs)
    # the original provider is still the one serving (never torn down by a swap)
    assert set(procs) == {"provider", "consumer"}

