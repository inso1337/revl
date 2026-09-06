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

import asyncio
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

    def __init__(self, name: str, cmd: list | None = None, spec_path: str | None = None,
                 spec: dict | None = None):
        self.name = name
        self.cmd = cmd or []
        self.spec_path = spec_path
        self.spec = spec or {}
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
        proc = _ScriptedProc(spec["name"], cmd=list(cmd), spec_path=cmd[-1],
                             spec=spec)
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


# ---------------------------------------------------------------------------
# item 56 / 151: a swap of a component hosted in a NETWORK provider is refused.
# The successor is booted on a fresh local UDS and the re-point loop reaches
# only the consumers this conductor runs, so a swap of a TCP+mTLS provider would
# leave every off-machine consumer (an item-56 network consumer in another
# process, an item-151 `[remotes]` consumer in a separate composition) dialling
# a torn-down address — the network seam silently ceasing to exist, with no
# refusal and no report line. The address is part of the placement contract
# other machines hold; a swap cannot unilaterally change it.
# ---------------------------------------------------------------------------


def _free_port() -> int:
    """An ephemeral port, bound and released. This suite must never pin a fixed
    port (the machine is shared); nothing ever binds this one anyway — the
    children are stubs — it only has to be a plausible number in the manifest."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


_NET_PLACEMENT = """
[processes.provider]
components = ["MemCache"]
[processes.provider.address]
host = "127.0.0.1"
port = {port}
[processes.provider.tls]
identity = "provider"
cert = "{d}/provider.crt"
key = "{d}/provider.key"
ca = "{d}/ca.crt"

[processes.consumer]
components = ["Consumer"]
[processes.consumer.tls]
identity = "consumer"
cert = "{d}/consumer.crt"
key = "{d}/consumer.key"
ca = "{d}/ca.crt"
"""


def test_conductor_refuses_to_swap_a_component_in_a_network_provider(
        tmp_path, monkeypatch, capsys):
    """`swap MemCache --to py` where MemCache is hosted in a process that
    declares an `address`: refused, naming the process, that it is a network
    provider, the endpoint remote consumers hold, and what to do instead. No
    successor boots and no consumer is re-pointed.

    TLS material is declared explicitly (paths that need not exist — the child
    processes are stubs) rather than minted, so this test shells out to nothing.
    """
    port = _free_port()
    app = _write(tmp_path, "net_app.rvl", _SWAPPABLE_APP)
    plc = _write(tmp_path, "net_app.toml",
                 _NET_PLACEMENT.format(port=port, d=tmp_path))
    procs = _wire_conductor(tmp_path, monkeypatch, ["swap MemCache --to py", ":q"])

    rc = _placement.run_placement([app], plc, once=False)
    assert rc == 0

    # the seam really is a network seam (guards against a vacuous pass: if the
    # manifest stopped producing a TCP+mTLS provider this test would otherwise
    # still "prove" the refusal)
    serve = procs["provider"].spec["serve"]
    assert "socket" not in serve and serve["endpoint"]["port"] == port

    # nothing booted beyond the two original processes ...
    assert not [n for n in procs if "__t" in n], list(procs)
    assert set(procs) == {"provider", "consumer"}
    # ... and the consumer was never re-pointed: the running seam is untouched
    written = [json.loads(w) for w in procs["consumer"].stdin.written if w.strip()]
    assert not [c for c in written if c.get("op") == "repoint"], written

    out = capsys.readouterr().out
    assert "swap refused" in out
    # the refusal names the process, what it is, and the address others hold
    assert "'provider'" in out and "NETWORK provider" in out
    assert f"127.0.0.1:{port}" in out
    assert "[processes.provider.address]" in out
    # ... and it is actionable: it says what to do instead
    assert "[processes.provider] backend =" in out
    assert "Running composition untouched." in out


def test_a_swap_in_a_purely_local_placement_is_not_refused_as_network(
        tmp_path, monkeypatch, capsys):
    """The network refusal keys on the PROVIDER's process declaring an address,
    not on the placement containing one anywhere: the same composition with no
    `address` swaps as before (guards against over-refusal)."""
    app = _write(tmp_path, "local_app.rvl", _SWAPPABLE_APP)
    plc = _write(tmp_path, "local_app.toml", _SWAPPABLE_PLACEMENT)
    procs = _wire_conductor(tmp_path, monkeypatch, ["swap MemCache --to py", ":q"])

    rc = _placement.run_placement([app], plc, once=False)
    assert rc == 0
    assert [n for n in procs if n.startswith("MemCache__t")], list(procs)
    assert "NETWORK provider" not in capsys.readouterr().out


def test_placement_accepts_ts_as_an_alias_for_node(tmp_path, monkeypatch):
    """The manifest names the TypeScript tier `node` while every other surface
    says `ts`; the conductor must accept both spellings and boot the node
    runner for either (roadmap item 72)."""
    app = _write(tmp_path, "app.rvl", _SWAPPABLE_APP)
    plc = _write(tmp_path, "app_ts.toml", _SWAPPABLE_PLACEMENT.replace(
        "components = [\"Consumer\"]", "backend = \"ts\"\ncomponents = [\"Consumer\"]", 1))
    procs = _wire_conductor(tmp_path, monkeypatch, [":q"])

    # the ts alias canonicalizes to the node tier, so the preflight needs a
    # node runtime to be "installed": stub the cordis-ts install dir and node
    # on PATH (the preflight check is not what this test is about). The TS
    # emitter subprocess is stubbed too, so no real emit runs.
    fake_ts = tmp_path / "typescript"
    (fake_ts / "node_modules" / "cordis").mkdir(parents=True)
    monkeypatch.setattr(_placement, "_TS_DIR", fake_ts)
    monkeypatch.setattr(_placement, "_emit_ts_module",
                        lambda ir, tmp: str(fake_ts / "_gen" / "mod.ts"))
    original_which = _placement.shutil.which

    def fake_which(cmd, **kw):
        if cmd == "node":
            return "/usr/bin/node"
        return original_which(cmd, **kw)

    monkeypatch.setattr(_placement.shutil, "which", fake_which)

    rc = _placement.run_placement([app], plc, once=False)
    assert rc == 0

    # the consumer process was spawned through the *node* runner (the ts
    # alias canonicalized, so the conductor never saw a backend it could
    # not build or spawn)
    consumer_cmd = procs["consumer"].cmd
    assert "placement_runner.ts" in " ".join(consumer_cmd)
    # the alias is normalized in the spec the child receives too
    assert procs["consumer"].spec["backend"] == "node"



# ---------------------------------------------------------------------------
# item 414: swap admission gates BOTH seam directions + re-gates after re-tier.
# The provides-only loop left an inbound seam ungated: a service the moving
# component REQUIRES from a component staying behind becomes cross-process when
# the component re-tiers, and a resource crossing it must be refused. And
# do_swap re-runs the plan-time gate over the post-swap topology.
# ---------------------------------------------------------------------------

_INBOUND_RES_APP = """
type Sock = { fd: Int }
extern pure fn close_sock(h: Int) = @py { return None }
extern acquire fn open_sock() -> Sock undo close_sock(0) = @py { return {"fd": 1} }
service Pool { async fn lease() -> Sock }
service App { async fn run() -> Str }

component Backend provides pool: Pool {
  provide pool { async fn lease() = open_sock() }
}
component Worker requires pool: Pool provides app: App {
  provide app { async fn run() = "ok" }
}
"""

# same shape but the required service is value-typed, so no resource crosses and
# the identical swap must ADMIT (guards against over-refusal).
_INBOUND_VAL_APP = _INBOUND_RES_APP.replace(
    "async fn lease() -> Sock", "async fn lease() -> Str"
).replace("async fn lease() = open_sock()", 'async fn lease() = "s"')

_INBOUND_PLACEMENT = """
[processes.backend]
components = ["Backend"]

[processes.worker]
components = ["Worker"]
"""


def test_admission_refuses_an_inbound_resource_seam(tmp_path):
    """Worker REQUIRES `pool: Pool` (resource-carrying) from Backend, which
    stays behind. Moving Worker to a new tier makes that INBOUND seam cross-
    process; a resource handle cannot cross it, so the swap is refused naming
    the required service and the resource, the direction the old provides-only
    loop never looked at."""
    src = _write(tmp_path, "app.rvl", _INBOUND_RES_APP)
    running = compile_files([src])
    candidate, error = swap_admission([src], running, "Worker", "go",
                                      from_backend="py")
    assert candidate is None
    assert error is not None
    assert "Pool" in error and "pool" in error and "Sock" in error


def test_admission_admits_a_swap_that_opens_no_new_resource_seam(tmp_path):
    """The same inbound-seam shape with a value-typed required service opens no
    resource crossing in either direction and stays within tier caps, so the
    swap still admits; the both-directions check does not over-refuse."""
    src = _write(tmp_path, "app.rvl", _INBOUND_VAL_APP)
    running = compile_files([src])
    candidate, error = swap_admission([src], running, "Worker", "go",
                                      from_backend="py")
    assert error is None, error
    assert candidate is not None


# NOTE on why the inbound-resource case is a swap_admission UNIT test, not a
# full do_swap integration test: through do_swap a `--to` swap re-uses the SAME
# source, so a pure tier swap never changes a component's requires/provides. Any
# inbound resource seam therefore already exists at PLAN time, where the
# tier-agnostic `resource_crossing_refusal` (the seam-hardening fix, Finding A)
# refuses it before the swap REPL is ever reached. The both-directions check in
# swap_admission is the standalone gate (and defense in depth for a
# candidate-differs caller); the materially NEW do_swap re-gate is the
# tier-capability gate over the post-swap tier, exercised below.


_RETIER_PYONLY_APP = """
extern pure fn py_only(x: Str) -> Str = @py { return x }
service Pool { async fn lease() -> Str }
service App { async fn run() -> Str }

component Backend provides pool: Pool {
  provide pool { async fn lease() = "s" }
}
component Worker requires pool: Pool provides app: App {
  provide app { async fn run() = py_only("x") }
}
"""


def test_conductor_refuses_a_swap_whose_new_tier_cannot_emit_the_component(
        tmp_path, monkeypatch, capsys):
    """Worker admits (no new resource seam, value-typed both ways) but its body
    reaches a `@py`-only extern the `node` tier cannot emit. do_swap re-runs the
    plan-time tier-capability gate over the POST-SWAP topology, so the re-tier is
    refused before any build; the parity the docstring now names, made real."""
    app = _write(tmp_path, "retier.rvl", _RETIER_PYONLY_APP)
    plc = _write(tmp_path, "retier.toml", _INBOUND_PLACEMENT)
    procs = _wire_conductor(tmp_path, monkeypatch, ["swap Worker --to node", ":q"])

    rc = _placement.run_placement([app], plc, once=False)
    assert rc == 0

    assert not [n for n in procs if "__t" in n], list(procs)
    assert set(procs) == {"backend", "worker"}
    out = capsys.readouterr().out
    assert "swap refused" in out and "Worker" in out and "node" in out


# ---------------------------------------------------------------------------
# item 337: repoint admission at the SEAM. The conductor already gates a `revl
# swap` (swap_admission above), but the raw `repoint` control command reaching a
# running process on stdin carried a socket and no source, so an injected or
# raced repoint could substitute an UN-ADMITTED provider at a placement seam.
# The process now re-admits the named successor against its OWN running manifest
# before accepting the cutover, and refuses fail-closed otherwise. These tests
# drive the handler/admission seam directly (the real control loop is heavy) and
# prove the refusal BLOCKS the substitution, not merely that admission complains.
# ---------------------------------------------------------------------------

from revl import _process_runner as _pr  # noqa: E402


class _StubClient:
    """Enough of a proxy `_Client` for the seam test: records repoint calls and
    tracks the target it currently serves, so a refused repoint is observable as
    'still pointing at the original target'."""

    def __init__(self, target: str):
        self.target = target
        self.repoints: list[str] = []

    def repoint(self, sock: str) -> None:
        self.repoints.append(sock)
        self.target = sock


_BOUND_CACHE_RVL = _CACHE_RVL.replace(
    "  async fn get(k: Str) -> Opt[Str]\n", "  fn get(k: Str) -> Str\n", 1
).replace("    async fn get(k) = m.get(k)\n", "    fn get(k) = k\n", 1)


_REMOTE_ONLY_RVL = """
service Cache {
  async fn get(k: Str) -> Opt[Str]
  async fn put(k: Str, v: Str)
}
service ApiSvc { async fn hit() -> Opt[Str] }

component Api requires cache: Cache provides api: ApiSvc {
  provide api { async fn hit() = cache.get("k") }
}
"""


def _seam_dir(tmp_path, spec=None, name="consumer"):
    """A realistic placement directory and the seam anchor a process booted out
    of it holds (item 337).

    The conductor makes one 0700 `mkdtemp` directory per placement, writes each
    process's spec into it as `<name>.spec.json`, and binds EVERY seam socket in
    it as `<name>.sock` — including a swap successor's (`placement.py`
    1751-1754, 2316, 2427). A process therefore learns the directory from its
    own argv, not from the spec JSON, which is what makes it an address anchor
    the receiver holds independently of anything the wire says.
    """
    directory = tmp_path / "revl_placement"
    directory.mkdir(exist_ok=True)
    spec_file = directory / f"{name}.spec.json"
    spec_file.write_text(json.dumps(spec or {}), encoding="utf-8")
    return directory, _pr._seam_anchor(spec or {}, str(spec_file))


def test_repoint_refused_at_seam_when_successor_fails_admission(tmp_path):
    """A repoint whose named successor FAILS admission against the running
    manifest is refused AT THE SEAM: the proxy is never re-pointed, so it keeps
    serving its original target (no blip). This is the closed gap: without the
    re-admission the socket would have been accepted outright."""
    src = _write(tmp_path, "bound.rvl", _BOUND_CACHE_RVL)
    running = compile_files([src])
    plc, anchor = _seam_dir(tmp_path)
    stub = _StubClient(target=str(plc / "orig.sock"))
    # MemCache's `cache` service is sync (address-space-bound) — swapping it to
    # the rust tier cannot cross the seam, so admission refuses it. Everything
    # else about the command is honest (MemCache really does provide `cache`,
    # and the successor socket really is in this process's placement dir), so
    # the refusal can only come from admission itself.
    cmd = {"op": "repoint", "key": "cache", "socket": str(plc / "successor.sock"),
           "component": "MemCache", "backend": "rust"}

    ok, reason = _pr._repoint_decision([src], running, cmd, anchor)
    assert ok is False
    assert reason and ("address-space-bound" in reason or "transport" in reason)

    # and the refusal actually BLOCKS the substitution at the seam:
    accepted = _pr._apply_repoint(cmd, {"cache": stub}, [src], running, anchor=anchor)
    assert accepted is False
    assert stub.repoints == []                        # never re-pointed
    assert stub.target == str(plc / "orig.sock")      # still the original provider


def test_repoint_fail_closed_when_no_admissible_reference(tmp_path):
    """A legacy socket-only repoint (no component/backend) cannot be admitted,
    so it is refused fail-closed — never silently accepted."""
    src = _write(tmp_path, "app.rvl", _CACHE_RVL)
    running = compile_files([src])
    plc, anchor = _seam_dir(tmp_path)
    stub = _StubClient(target="orig.sock")
    cmd = {"op": "repoint", "key": "cache", "socket": str(plc / "attacker.sock")}

    ok, reason = _pr._repoint_decision([src], running, cmd, anchor)
    assert ok is False
    assert reason and "fail-closed" in reason

    accepted = _pr._apply_repoint(cmd, {"cache": stub}, [src], running, anchor=anchor)
    assert accepted is False
    assert stub.repoints == []
    assert stub.target == "orig.sock"


def test_legit_repoint_still_admitted_and_repoints(tmp_path):
    """A faithful tier swap (successor passes admission against the running
    manifest, is named as the real provider of the key, and serves on a socket
    in this process's own placement directory) is admitted and the proxy
    re-points onto the successor socket."""
    src = _write(tmp_path, "app.rvl", _CACHE_RVL)
    running = compile_files([src])
    plc, anchor = _seam_dir(tmp_path)
    successor = str(plc / "cache-succ.sock")
    stub = _StubClient(target=str(plc / "orig.sock"))
    cmd = {"op": "repoint", "key": "cache", "socket": successor,
           "component": "MemCache", "backend": "rust"}

    ok, reason = _pr._repoint_decision([src], running, cmd, anchor)
    assert ok is True and reason is None

    accepted = _pr._apply_repoint(cmd, {"cache": stub}, [src], running, anchor=anchor)
    assert accepted is True
    assert stub.repoints == [successor]
    assert stub.target == successor


# ---------------------------------------------------------------------------
# item 337, the unjudged-address hole (adversarial audit, HIGH). The seams above
# admitted a SELECTOR and then applied an address nothing had judged: nothing
# bound `component` to the `key` whose proxy moved, and neither the socket nor
# the endpoint was ever an input to the decision. So any admissible
# (component, backend) pair in the composition was a pass token for re-pointing
# ANY key at ANY address. Mesh property 1 ("the gate inputs are
# receiver-controlled, not wire-controlled", docs/design/337-...md:94) requires
# both the binding and the address to be receiver-derived, so both are tested
# here as refusals that BLOCK the effect, not as diagnostics.
# ---------------------------------------------------------------------------


def test_repoint_with_an_unrelated_component_as_pass_token_is_refused(tmp_path):
    """`Api` is a perfectly admissible component of this composition — and it
    does not provide `cache`. Using it as the selector for a repoint of the
    `cache` proxy is refused by the receiver's OWN manifest, which names
    MemCache as that key's provider. Admission of some other component is not
    an admission token for this key."""
    src = _write(tmp_path, "app.rvl", _CACHE_RVL)
    running = compile_files([src])
    plc, anchor = _seam_dir(tmp_path)
    stub = _StubClient(target=str(plc / "orig.sock"))
    # the pass token really does pass admission on its own — that is the point:
    assert swap_admission([src], running, "Api", "py")[1] is None
    cmd = {"op": "repoint", "key": "cache", "socket": str(plc / "attacker.sock"),
           "component": "Api", "backend": "py"}

    ok, reason = _pr._repoint_decision([src], running, cmd, anchor)
    assert ok is False
    assert reason and "does not provide key 'cache'" in reason

    accepted = _pr._apply_repoint(cmd, {"cache": stub}, [src], running, anchor=anchor)
    assert accepted is False
    assert stub.repoints == []
    assert stub.target == str(plc / "orig.sock")


def test_repoint_to_an_unsanctioned_address_is_refused(tmp_path):
    """The selector is entirely honest — MemCache really is `cache`'s provider
    and really is admissible on the rust tier — but the socket is not one this
    receiver sanctions: it is outside the placement directory the conductor
    handed this process its own spec in, so no seam of this composition was
    ever bound there. The address is the whole effect of a repoint, so it is
    judged, and the cutover is blocked."""
    src = _write(tmp_path, "app.rvl", _CACHE_RVL)
    running = compile_files([src])
    plc, anchor = _seam_dir(tmp_path)
    stub = _StubClient(target=str(plc / "orig.sock"))
    cmd = {"op": "repoint", "key": "cache", "socket": "/tmp/attacker-controlled.sock",
           "component": "MemCache", "backend": "rust"}

    ok, reason = _pr._repoint_decision([src], running, cmd, anchor)
    assert ok is False
    assert reason and "placement directory" in reason

    accepted = _pr._apply_repoint(cmd, {"cache": stub}, [src], running, anchor=anchor)
    assert accepted is False
    assert stub.repoints == []
    assert stub.target == str(plc / "orig.sock")


def test_repoint_fail_closed_when_the_receiver_has_no_address_anchor(tmp_path):
    """A process that cannot locate its own placement directory cannot sanction
    any socket, and indeterminate is a REFUSAL: it does not fall back to
    trusting the wire's address."""
    src = _write(tmp_path, "app.rvl", _CACHE_RVL)
    running = compile_files([src])
    stub = _StubClient(target="orig.sock")
    cmd = {"op": "repoint", "key": "cache", "socket": "successor.sock",
           "component": "MemCache", "backend": "rust"}

    ok, reason = _pr._repoint_decision([src], running, cmd, _pr._seam_anchor({}, None))
    assert ok is False
    assert reason and "no placement directory" in reason

    accepted = _pr._apply_repoint(cmd, {"cache": stub}, [src], running)
    assert accepted is False
    assert stub.repoints == []


# ---------------------------------------------------------------------------
# item 337 Seam 2: boot-time bridge re-admission. Same seam as above, moved one
# event earlier: before a consumer process wires its INITIAL proxy to a
# provider served by another process, it re-admits that provider against its
# own running manifest (`_boot_wiring_decision` / `_apply_boot_wiring`), with
# the same key-binding and address sanction around it that the repoint seam
# runs. placement.py stamps the provider's admissible identity (`component`,
# `backend`) onto every same-composition proxy entry so the selector is
# already in the spec the consumer already trusts — no new transport.
#
# The BINDING and the ADDRESS are the same questions at both seams. The
# ADMISSION question is not, and that difference is load-bearing (see
# tests/test_seam2_same_tier_readmission.py): a repoint MOVES a provider under
# a live consumer and so asks `swap_admission`, while a boot seam re-admits a
# provider that is already where it is and so asks
# `placement.seam_readmission`, the conductor's own plan-time seam gate.
# ---------------------------------------------------------------------------


def _run(coro):
    return asyncio.run(coro)


# A cache whose service hands back an OWNED resource handle. A resource cannot
# cross a process seam by copy in EITHER direction, tier-agnostically, so this
# is a seam the conductor itself refuses at plan time
# (`resource_crossing_refusal`) — which is exactly why a consumer re-admitting
# the same provider at boot refuses it too.
_HANDLE_CACHE_RVL = """
type Sock = { fd: Int }
extern pure fn close_sock(h: Int) = @py { return None }
extern acquire fn open_sock() -> Sock undo close_sock(0) = @py { return {"fd": 1} }

service Cache {
  async fn get(k: Str) -> Sock
  async fn put(k: Str, v: Str)
}
service ApiSvc { async fn hit() -> Sock }

component MemCache provides cache: Cache {
  provide cache {
    async fn get(k) = open_sock()
    async fn put(k, v) = v
  }
}

component Api requires cache: Cache provides api: ApiSvc {
  provide api { async fn hit() = cache.get("k") }
}
"""


def test_boot_wiring_refused_at_seam_when_provider_fails_admission(tmp_path):
    """A boot-time proxy whose named provider FAILS admission against the
    running manifest is refused AT THE SEAM: `wire` is never invoked, so the
    proxy is never wired at all — not merely that admission complains.

    The refusal used here is one the CONDUCTOR also makes: `Cache.get` returns
    an owned `Sock` handle, and a resource cannot cross a process seam by copy,
    same tier or across tiers. (A merely SYNC service is a different case: the
    conductor permits that seam, so the boot seam must too —
    tests/test_seam2_same_tier_readmission.py.)"""
    src = _write(tmp_path, "handle.rvl", _HANDLE_CACHE_RVL)
    running = compile_files([src])
    plc, anchor = _seam_dir(tmp_path)
    # The socket is a real one INSIDE this receiver's own placement directory,
    # so the address sanction passes and the refusal under test is the
    # ADMISSION one: `Cache.get` hands back an owned `Sock`, which cannot cross
    # a process seam by copy on any tier.
    info = {"socket": str(plc / "provider.sock"), "methods": ["get"], "service": "Cache",
            "component": "MemCache", "backend": "rust"}
    wired = []

    async def wire():
        wired.append(info["socket"])

    ok, reason = _pr._boot_wiring_decision([src], running, "cache", info, anchor)
    assert ok is False
    assert reason and "address-space-bound" in reason and "Sock" in reason

    accepted = _run(_pr._apply_boot_wiring("cache", info, [src], running, wire,
                                           anchor=anchor))
    assert accepted is False
    assert wired == []   # the wire callback was never invoked -- never wired


def test_boot_wiring_fail_closed_when_no_admissible_reference(tmp_path):
    """A proxy entry carrying no component/backend (a stripped or malformed
    selector) cannot be admitted, so boot wiring refuses it fail-closed --
    never silently wired -- mirroring the repoint seam's legacy-command case."""
    src = _write(tmp_path, "app.rvl", _CACHE_RVL)
    running = compile_files([src])
    plc, anchor = _seam_dir(tmp_path)
    info = {"socket": str(plc / "provider.sock"), "methods": ["get"], "service": "Cache"}
    wired = []

    async def wire():
        wired.append(info["socket"])

    ok, reason = _pr._boot_wiring_decision([src], running, "cache", info, anchor)
    assert ok is False
    assert reason and "fail-closed" in reason

    accepted = _run(_pr._apply_boot_wiring("cache", info, [src], running, wire,
                                           anchor=anchor))
    assert accepted is False
    assert wired == []


def test_legit_boot_wiring_still_admitted_and_wires(tmp_path):
    """A faithful boot-time proxy (provider passes admission against the
    running manifest, is the key's real provider, and serves on a socket in
    this process's own placement directory) is admitted and actually wired."""
    src = _write(tmp_path, "app.rvl", _CACHE_RVL)
    running = compile_files([src])
    plc, anchor = _seam_dir(tmp_path)
    info = {"socket": str(plc / "provider.sock"), "methods": ["get", "put"],
            "service": "Cache", "component": "MemCache", "backend": "rust"}
    wired = []

    async def wire():
        wired.append(info["socket"])

    ok, reason = _pr._boot_wiring_decision([src], running, "cache", info, anchor)
    assert ok is True and reason is None

    accepted = _run(_pr._apply_boot_wiring("cache", info, [src], running, wire,
                                           anchor=anchor))
    assert accepted is True
    assert wired == [str(plc / "provider.sock")]


def test_boot_wiring_with_the_address_swapped_under_an_honest_selector_is_refused(tmp_path):
    """The injected wiring this seam exists to catch: the entry keeps the
    selector placement.py stamped (`MemCache`/`rust`, which admits) and only the
    ADDRESS is swapped. Judging the selector while wiring the address judged the
    wrong thing; the address is now judged against the receiver's own placement
    directory, and the proxy is never wired."""
    src = _write(tmp_path, "app.rvl", _CACHE_RVL)
    running = compile_files([src])
    _plc, anchor = _seam_dir(tmp_path)
    info = {"socket": "/tmp/attacker.sock", "methods": ["get", "put"],
            "service": "Cache", "component": "MemCache", "backend": "rust"}
    wired = []

    async def wire():
        wired.append(info["socket"])

    ok, reason = _pr._boot_wiring_decision([src], running, "cache", info, anchor)
    assert ok is False
    assert reason and "placement directory" in reason

    accepted = _run(_pr._apply_boot_wiring("cache", info, [src], running, wire,
                                           anchor=anchor))
    assert accepted is False
    assert wired == []


def test_boot_wiring_with_an_unrelated_component_as_pass_token_is_refused(tmp_path):
    """`Api` admits on its own, and provides `api`, not `cache`. Naming it in
    the `cache` proxy entry is refused by the consumer's own manifest before
    admission is even consulted: a selector for another component is not an
    admission token for this key."""
    src = _write(tmp_path, "app.rvl", _CACHE_RVL)
    running = compile_files([src])
    plc, anchor = _seam_dir(tmp_path)
    assert swap_admission([src], running, "Api", "py")[1] is None
    info = {"socket": str(plc / "provider.sock"), "methods": ["get", "put"],
            "service": "Cache", "component": "Api", "backend": "py"}
    wired = []

    async def wire():
        wired.append(info["socket"])

    ok, reason = _pr._boot_wiring_decision([src], running, "cache", info, anchor)
    assert ok is False
    assert reason and "does not provide key 'cache'" in reason

    accepted = _run(_pr._apply_boot_wiring("cache", info, [src], running, wire,
                                           anchor=anchor))
    assert accepted is False
    assert wired == []


def test_remote_flag_does_not_skip_seam2_on_a_same_composition_key(tmp_path):
    """`remote` is wire state, so it cannot decide whether the gate runs. This
    key IS provided by a component of the consumer's own running manifest
    (MemCache provides `cache`), so it is a same-composition seam and is gated
    however the entry labels itself: one flag must not turn an unjudged
    attacker endpoint into an admitted wiring.

    This replaces the landed `test_remote_boot_proxy_is_not_seam2_gated`, whose
    premise was wrong: it asserted the ungated wiring using a key
    (`cache`) that this very composition provides, which is precisely the
    exploit. The genuine Seam 3 case it meant to protect is the test below.
    """
    src = _write(tmp_path, "app.rvl", _CACHE_RVL)
    running = compile_files([src])
    _plc, anchor = _seam_dir(tmp_path)
    info = {"endpoint": {"host": "attacker.example", "port": 9999},
            "methods": ["get"], "service": "Cache", "remote": True}
    wired = []

    async def wire():
        wired.append(info["endpoint"])

    accepted = _run(_pr._apply_boot_wiring("cache", info, [src], running, wire,
                                           anchor=anchor))
    assert accepted is False
    assert wired == []

    ok, reason = _pr._boot_wiring_decision([src], running, "cache", info, anchor)
    assert ok is False
    # no selector at all on this entry, so it fails closed at the first gate
    assert reason and "fail-closed" in reason


def test_genuine_cross_composition_remote_is_out_of_seam2_scope(tmp_path):
    """A genuine remote seam (item 151) is one whose key NO component of this
    composition provides — the receiver decides that from its own manifest, not
    from the entry's flag. Such a key is Seam 3, a different and deferred seam,
    so it wires ungated; gating it would refuse every legitimate remote proxy,
    since its provider is not a member of this manifest at all."""
    src = _write(tmp_path, "remote_only.rvl", _REMOTE_ONLY_RVL)
    running = compile_files([src])
    _plc, anchor = _seam_dir(tmp_path)
    assert _pr._providers_of(running, "cache") == set()   # provided by nobody here
    info = {"endpoint": {"host": "10.0.0.9", "port": 4000}, "methods": ["get"],
            "service": "Cache", "remote": True}
    wired = []

    async def wire():
        wired.append("remote")

    accepted = _run(_pr._apply_boot_wiring("cache", info, [src], running, wire,
                                           anchor=anchor))
    assert accepted is True
    assert wired == ["remote"]


def test_network_seam_is_sanctioned_by_the_receivers_own_mtls_identity(tmp_path):
    """A same-composition NETWORK seam cannot be sanctioned by address — the
    machines live in the placement manifest, which the receiver does not hold —
    so it is sanctioned by the one trust anchor the receiver was itself issued:
    placement stamps the same `certs[pname]` material on this process's serve
    endpoint and on every network proxy it holds. An endpoint anchored on that
    material is wired; the same endpoint with foreign material is refused."""
    src = _write(tmp_path, "app.rvl", _CACHE_RVL)
    running = compile_files([src])
    mine = {"cert": str(tmp_path / "me.crt"), "key": str(tmp_path / "me.key"),
            "ca": str(tmp_path / "seam_ca.crt"), "identity": "consumer"}
    theirs = {**mine, "ca": str(tmp_path / "other_ca.crt")}
    spec = {"serve": {"endpoint": {"host": "10.0.0.4", "port": 4100, "tls": mine}}}
    _plc, anchor = _seam_dir(tmp_path, spec)

    def entry(tls):
        return {"endpoint": {"host": "10.0.0.9", "port": 4000,
                             "tls": {**tls, "server_hostname": "localhost"}},
                "methods": ["get", "put"], "service": "Cache",
                "component": "MemCache", "backend": "py"}

    ok, reason = _pr._boot_wiring_decision([src], running, "cache", entry(mine), anchor)
    assert ok is True and reason is None

    ok, reason = _pr._boot_wiring_decision([src], running, "cache", entry(theirs), anchor)
    assert ok is False
    assert reason and "own mTLS identity" in reason

    stripped = entry(mine)
    stripped["endpoint"].pop("tls")
    ok, reason = _pr._boot_wiring_decision([src], running, "cache", stripped, anchor)
    assert ok is False
    assert reason and "no complete mTLS material" in reason


# ---------------------------------------------------------------------------
# item 337 seam identity: the seam carries the gate SURFACE that admitted the
# crossing (`gate_version()`), and the receiver compares it against its own
# before it trusts a re-admission (docs/design/337-...md, "What identity a seam
# carries"). A crossing whose declared gate `api` differs from the receiver's
# is refused fail-closed, with the frontier named so the skew is attributable;
# a matching or absent surface changes no verdict. On a homogeneous py fleet
# both ends share one install, so this is a strict no-op — which is why the
# refusal below has to construct the skew by hand.
# ---------------------------------------------------------------------------


def _receiver_gate_version() -> dict:
    from revl import gate  # noqa: PLC0415
    return gate.gate_version()


def test_boot_wiring_matching_gate_surface_is_admitted_and_absent_is_tolerated(tmp_path):
    """Verdict-invariance: a proxy that declares the receiver's OWN gate surface
    is admitted exactly as one that declares none (the homogeneous-fleet case,
    and every entry written before the field existed). The selector, binding,
    address and re-admission checks decide, and the surface check waves both
    through."""
    src = _write(tmp_path, "app.rvl", _CACHE_RVL)
    running = compile_files([src])
    plc, anchor = _seam_dir(tmp_path)
    base = {"socket": str(plc / "provider.sock"), "methods": ["get", "put"],
            "service": "Cache", "component": "MemCache", "backend": "rust"}

    absent = dict(base)
    ok, reason = _pr._boot_wiring_decision([src], running, "cache", absent, anchor)
    assert ok is True and reason is None

    matching = {**base, "gate_version": _receiver_gate_version()}
    ok, reason = _pr._boot_wiring_decision([src], running, "cache", matching, anchor)
    assert ok is True and reason is None


def test_boot_wiring_refused_when_the_declared_gate_surface_is_incompatible(tmp_path):
    """A provider admitted by a gate whose `api` this receiver does not share is
    refused BEFORE binding/address/admission: two ends that do not cover the same
    surface cannot trust each other's agreement. The refusal names both frontiers
    (attributable skew), and it blocks the wiring rather than logging it."""
    src = _write(tmp_path, "app.rvl", _CACHE_RVL)
    running = compile_files([src])
    plc, anchor = _seam_dir(tmp_path)
    mine = _receiver_gate_version()
    skewed = {"api": mine["api"] + "-other", "language": mine["language"],
              "frontier": "some-other-tier:frontier"}
    info = {"socket": str(plc / "provider.sock"), "methods": ["get", "put"],
            "service": "Cache", "component": "MemCache", "backend": "rust",
            "gate_version": skewed}

    ok, reason = _pr._boot_wiring_decision([src], running, "cache", info, anchor)
    assert ok is False
    assert reason and "gate-version skew" in reason
    assert "some-other-tier:frontier" in reason and mine["frontier"] in reason

    wired = []

    async def wire():
        wired.append(info["socket"])

    accepted = _run(_pr._apply_boot_wiring("cache", info, [src], running, wire,
                                           anchor=anchor))
    assert accepted is False
    assert wired == []


def test_boot_wiring_refused_when_the_declared_gate_surface_is_unreadable(tmp_path):
    """A declared-but-malformed surface is a broken declaration, not an absence,
    and fails closed."""
    src = _write(tmp_path, "app.rvl", _CACHE_RVL)
    running = compile_files([src])
    plc, anchor = _seam_dir(tmp_path)
    info = {"socket": str(plc / "provider.sock"), "methods": ["get", "put"],
            "service": "Cache", "component": "MemCache", "backend": "rust",
            "gate_version": "not-a-surface"}

    ok, reason = _pr._boot_wiring_decision([src], running, "cache", info, anchor)
    assert ok is False
    assert reason and "cannot read" in reason and "gate-version skew" in reason


def test_repoint_refused_when_the_declared_gate_surface_is_incompatible(tmp_path):
    """The same discipline on the repoint seam: a successor admitted by a foreign
    gate surface is refused and the proxy keeps serving its current target (no
    blip), while the receiver's own surface (or none) re-points as before."""
    src = _write(tmp_path, "app.rvl", _CACHE_RVL)
    running = compile_files([src])
    plc, anchor = _seam_dir(tmp_path)
    successor = str(plc / "cache-succ.sock")
    mine = _receiver_gate_version()

    skewed = {"op": "repoint", "key": "cache", "socket": successor,
              "component": "MemCache", "backend": "rust",
              "gate_version": {"api": mine["api"] + "-other",
                               "frontier": "some-other-tier:frontier"}}
    ok, reason = _pr._repoint_decision([src], running, skewed, anchor)
    assert ok is False
    assert reason and "gate-version skew" in reason

    stub = _StubClient(target=str(plc / "orig.sock"))
    accepted = _pr._apply_repoint(skewed, {"cache": stub}, [src], running, anchor=anchor)
    assert accepted is False
    assert stub.repoints == [] and stub.target == str(plc / "orig.sock")

    # the matching-surface command still re-points, so the guard is not a blanket
    # refusal of every gate_version-carrying repoint
    faithful = {**skewed, "gate_version": mine}
    ok, reason = _pr._repoint_decision([src], running, faithful, anchor)
    assert ok is True and reason is None
