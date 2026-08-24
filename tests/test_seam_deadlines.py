"""Seam deadlines — the missing half of partial failure (roadmap §54).

The bridge already turns peer *death* into a reactive withdrawal (a monitor
connection's EOF; see `tests/test_swap.py`). This suite covers peer *hang*: a
provider that is alive but wedged — slow, deadlocked — answers neither with a
value nor with EOF, so a naive blocking round-trip wedges the consumer too.

Every seam call carries a deadline (`docs/seam-deadlines.md`). When the
round-trip outlives it, `_Client.call` raises `bridge.SeamDeadline` — its own
fault kind, deliberately *neither* the `ConnectionError` a peer death raises
(so it does not withdraw the provision) *nor* the `RuntimeError` a provider-side
error marshals back. The consumer's L-Raise then unwinds exactly as for any
other seam failure (A8): the inverses accumulated so far run LIFO, no residue.

Like the swap suite, this drives the mechanism over real Unix sockets with a
minimal protocol-speaking provider — no cordis needed for the bridge mechanism.
The wedged provider simply sleeps past the deadline before replying, staying
alive (its monitor connection never EOFs) so a breach is provably a hang and
not a death.
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
    """A short-pathed directory for Unix sockets (the macOS ``sun_path`` limit;
    same rationale as tests/test_swap.py)."""
    directory = tempfile.mkdtemp(prefix="rvdl", dir="/tmp")
    try:
        yield Path(directory)
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def _bridge():
    """backends/python/bridge.py, imported directly (it needs no cordis)."""
    spec = importlib.util.spec_from_file_location(
        "revl_deadline_test_bridge", ROOT / "backends" / "python" / "bridge.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


bridge = _bridge()


# ---------------------------------------------------------------------------
# a minimal provider that speaks the bridge wire protocol and can be wedged
# ---------------------------------------------------------------------------


class _Provider:
    """Answers `{"key","method","args"}` on a real Unix socket like
    `bridge.serve`, but with a controllable per-method delay so a test can wedge
    it deterministically. Crucially the provider stays *alive* while it stalls
    (the monitor connection is never dropped), so a breach is a hang — not the
    EOF that would read as a death.

    `delays` maps a method name to seconds to sleep before replying; `delay` is
    the default for methods not in the map. A wedged reply is still a valid
    reply — it is just late — which is the whole point: the fault is the
    consumer's deadline, not anything the provider signals.
    """

    def __init__(self, sock_path: str, value="v1", delay: float = 0.0,
                 delays: dict | None = None) -> None:
        self.value = value
        self.delay = delay
        self.delays = dict(delays or {})
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
                    conn.close()
                    break
                self._conns.append(conn)
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn: socket.socket) -> None:
        stream = conn.makefile("rwb")
        try:
            for line in stream:
                req = json.loads(line)
                method = req.get("method")
                nap = self.delays.get(method, self.delay)
                if nap:
                    time.sleep(nap)
                if self._stopping:
                    break
                self.calls += 1
                value = self.value if method == "get" else None
                stream.write((json.dumps({"ok": True, "value": value}) + "\n").encode())
                stream.flush()
        except OSError:
            pass

    def stop(self) -> None:
        with self._lock:
            self._stopping = True
            conns = list(self._conns)
        try:
            self._srv.close()
        except OSError:
            pass
        for conn in conns:
            for op in (lambda c: c.shutdown(socket.SHUT_RDWR), lambda c: c.close()):
                try:
                    op(conn)
                except OSError:
                    pass


# ---------------------------------------------------------------------------
# a consumer scope that models the L-Raise contract (A8) at the bridge level
# ---------------------------------------------------------------------------


class _Residue(Exception):
    """Raised by the harness if an unwind leaves anything behind."""


class _LScope:
    """A minimal stand-in for a component's activation scope, exercising the
    exact L-Raise reading a real seam failure gets (docs/backend-ir.md A8/R4):

    * each successful step *acquires* a resource — recorded in a live ledger —
      and pushes its **inverse** onto a LIFO stack;
    * if a step raises (here: a seam call that breaches its deadline), the scope
      **unwinds**: every accumulated inverse runs newest-first, each exactly
      once, and the ledger must return to empty — *no residue*.

    The ledger and the order log are what the residue proof reads: after an
    L-Raise the ledger is empty (R4) and the inverses ran strictly LIFO.
    """

    def __init__(self) -> None:
        self.ledger: set[str] = set()      # live resources (must end empty)
        self._inverses: list = []          # LIFO stack of undo closures
        self.order: list[str] = []         # the order inverses actually ran

    def acquire(self, name: str) -> None:
        self.ledger.add(name)

        def undo() -> None:
            self.order.append(name)
            self.ledger.remove(name)       # KeyError if run twice: caught below

        self._inverses.append(undo)

    def unwind(self) -> None:
        """Run accumulated inverses LIFO, then assert no residue (R4)."""
        while self._inverses:
            self._inverses.pop()()         # newest-first
        if self.ledger:
            raise _Residue(f"residue after unwind: {sorted(self.ledger)}")


# ---------------------------------------------------------------------------
# the deadline breach: a wedged provider, a distinguishable fault
# ---------------------------------------------------------------------------


def test_wedged_provider_breaches_the_deadline_as_its_own_fault_kind(sockdir):
    sock = str(sockdir / "provider.sock")
    # the provider is alive but takes far longer than the deadline to answer
    provider = _Provider(sock, "v1", delay=2.0)
    client = bridge._Client(sock, deadline=0.15)

    lost: list[int] = []
    client.watch(lambda: lost.append(1))   # peer-death monitor (must NOT fire)

    t0 = time.monotonic()
    with pytest.raises(bridge.SeamDeadline) as caught:
        client.call("cache", "get", [])
    elapsed = time.monotonic() - t0

    exc = caught.value
    # it broke at the deadline, not after the provider's 2s nap
    assert elapsed < 1.0, elapsed
    # the fault is the distinguishable seam-deadline kind ...
    assert isinstance(exc, bridge.SeamDeadline)
    assert exc.key == "cache" and exc.method == "get" and exc.deadline == 0.15
    # ... and is NOT a peer-death withdrawal and NOT a provider error
    assert not isinstance(exc, ConnectionError), "a hang must not read as a death"
    assert not isinstance(exc, RuntimeError), "a hang must not read as a provider error"

    # a hang is not a death: the monitor must stay quiet (no withdrawal)
    time.sleep(0.2)
    assert lost == [], "a deadline breach must not fire the peer-death withdrawal"

    client.close()
    provider.stop()


def test_deadline_breach_unwinds_residue_free_like_any_seam_failure(sockdir):
    """The consumer's L-Raise: a seam call that breaches its deadline mid-
    activation reverts the effects accumulated so far, LIFO, leaving no residue
    (A8/R4) — exactly as a provider error or a peer death would."""
    sock = str(sockdir / "provider.sock")
    provider = _Provider(sock, "v1", delay=2.0)
    client = bridge._Client(sock, deadline=0.15)
    lost: list[int] = []
    client.watch(lambda: lost.append(1))

    scope = _LScope()
    fault: Exception | None = None
    try:
        # activation accumulates three effects (each pushes its inverse) ...
        scope.acquire("db-conn")
        scope.acquire("file-handle")
        scope.acquire("lock")
        # ... then a seam call to the wedged provider breaches its deadline
        client.call("cache", "get", [])
        scope.acquire("never")             # unreachable
    except Exception as exc:               # noqa: BLE001 — this IS the L-Raise
        fault = exc
        scope.unwind()                     # revert LIFO; asserts no residue

    # the fault that drove the unwind is the seam-deadline kind
    assert isinstance(fault, bridge.SeamDeadline), fault
    # the inverses ran newest-first (LIFO), each exactly once ...
    assert scope.order == ["lock", "file-handle", "db-conn"], scope.order
    # ... "never" was never acquired (the call raised before it) ...
    assert "never" not in scope.order
    # ... and the ledger is empty: no residue (R4). `unwind` already asserted
    # this, but state it plainly as the proof.
    assert scope.ledger == set(), scope.ledger
    # the breach did not masquerade as a peer death
    assert lost == [], lost

    client.close()
    provider.stop()


def test_per_call_override_tightens_and_loosens_the_deadline(sockdir):
    sock = str(sockdir / "provider.sock")
    # answers in ~0.3s: slower than a tight override, faster than a loose one
    provider = _Provider(sock, "v1", delay=0.3)

    # (a) a generous client default would pass, but a tight per-call override
    #     breaches — the override wins.
    client = bridge._Client(sock, deadline=5.0)
    with pytest.raises(bridge.SeamDeadline):
        client.call("cache", "get", [], deadline=0.05)
    client.close()

    # (b) a tight client default would breach, but a loose per-call override
    #     lets the same call through — the override wins the other way.
    client2 = bridge._Client(sock, deadline=0.05)
    assert client2.call("cache", "get", [], deadline=2.0) == "v1"
    client2.close()

    provider.stop()


def test_per_operation_default_deadline_via_the_deadlines_map(sockdir):
    """The per-op default: one operation gets a tight deadline, another does
    not, from the same client — the map keys the deadline by method name."""
    sock = str(sockdir / "provider.sock")
    # `slow` naps past its op-deadline; `get` answers immediately
    provider = _Provider(sock, "v1", delays={"slow": 2.0})
    client = bridge._Client(sock, deadline=None, deadlines={"slow": 0.1})

    # the fast op has no per-op deadline and no default -> it just answers
    assert client.call("cache", "get", []) == "v1"
    # the slow op carries the per-op deadline and breaches it
    with pytest.raises(bridge.SeamDeadline) as caught:
        client.call("cache", "slow", [])
    assert caught.value.method == "slow" and caught.value.deadline == 0.1

    client.close()
    provider.stop()


def test_fast_provider_under_the_deadline_is_unaffected(sockdir):
    """The common case: a provider that answers well within the deadline is
    untouched — no fault, no withdrawal, the value comes straight back."""
    sock = str(sockdir / "provider.sock")
    provider = _Provider(sock, "v1", delay=0.0)
    client = bridge._Client(sock, deadline=1.0)
    lost: list[int] = []
    client.watch(lambda: lost.append(1))

    for _ in range(20):
        assert client.call("cache", "get", []) == "v1"
    assert lost == [], "a healthy provider must not trip the deadline or withdraw"

    client.close()
    provider.stop()


def test_a_provider_error_is_still_distinct_from_a_deadline(sockdir):
    """Guard the three-way split: a provider that answers with an *error* reply
    (a `RuntimeError` across the seam) inside the deadline is not a deadline
    breach — the deadline only fires on silence, not on a fast failure."""
    sock = str(sockdir / "provider.sock")

    class _Erroring(_Provider):
        def _handle(self, conn):
            stream = conn.makefile("rwb")
            try:
                for line in stream:
                    json.loads(line)
                    stream.write((json.dumps(
                        {"ok": False, "error": "ValueError: boom"}) + "\n").encode())
                    stream.flush()
            except OSError:
                pass

    provider = _Erroring(sock, "v1")
    client = bridge._Client(sock, deadline=1.0)
    with pytest.raises(RuntimeError) as caught:
        client.call("cache", "get", [])
    assert not isinstance(caught.value, bridge.SeamDeadline)
    assert "boom" in str(caught.value)
    client.close()
    provider.stop()


# ---------------------------------------------------------------------------
# placement plumbing: the per-op default lands in the seam spec both sides read
# ---------------------------------------------------------------------------


from revl import placement as _placement  # noqa: E402


class _StubProc:
    """The smallest Popen stand-in the conductor's `--once` boot needs: it comes
    up, its provided keys reply to probes trivially, and it tears down clean."""

    def __init__(self, name: str, spec: dict) -> None:
        self.name = name
        self.spec = spec
        self._lines: list[str] = [f"[{name}] UP"]
        self._down = False
        self.stdin = self  # absorb any control writes
        self.returncode = 0

    # --- stdout: an iterable of lines the conductor pumps
    @property
    def stdout(self):
        return self

    def __iter__(self):
        return self

    def __next__(self):
        while True:
            if self._lines:
                return self._lines.pop(0)
            if self._down:
                raise StopIteration
            time.sleep(0.005)

    # --- stdin sink
    def write(self, _text: str) -> None:
        pass

    def flush(self):
        pass

    def close(self):
        pass

    # --- process controls
    def poll(self):
        return 0 if self._down else None

    def wait(self, timeout=None):
        self._teardown()
        return 0

    def terminate(self):
        self._teardown()

    def kill(self):
        self._teardown()

    def _teardown(self):
        if not self._down:
            self._lines.append(f"[{self.name}] residue no residue")
            self._lines.append(f"[{self.name}] DOWN")
            self._down = True


_APP = """
service Cache {
  async fn get(k: Str) -> Opt[Str]
  async fn slow(k: Str) -> Opt[Str]
}
service App { async fn run() -> Opt[Str] }

component MemCache provides cache: Cache {
  let m = effect Map.new() undo m.drop()
  provide cache {
    async fn get(k) = m.get(k)
    async fn slow(k) = m.get(k)
  }
}
component Consumer requires cache: Cache provides app: App {
  provide app { async fn run() = cache.get("k") }
}
"""

_PLACEMENT = """
[processes.provider]
components = ["MemCache"]

[processes.consumer]
components = ["Consumer"]
"""


def _run_conductor_once(tmp_path, monkeypatch, placement_text):
    procs: dict = {}

    def fake_popen(cmd, **kwargs):
        spec = json.loads(Path(cmd[-1]).read_text(encoding="utf-8"))
        proc = _StubProc(spec["name"], spec)
        procs[spec["name"]] = proc
        return proc

    monkeypatch.setattr(_placement, "_cordis_py_installed", lambda: True)
    monkeypatch.setattr(_placement.subprocess, "Popen", fake_popen)

    app = tmp_path / "app.rvl"
    app.write_text(_APP, encoding="utf-8")
    plc = tmp_path / "app.toml"
    plc.write_text(placement_text, encoding="utf-8")

    rc = _placement.run_placement([str(app)], str(plc), once=True)
    assert rc == 0, rc
    return procs


def test_placement_stamps_the_default_seam_deadline_on_each_proxy(tmp_path, monkeypatch):
    procs = _run_conductor_once(tmp_path, monkeypatch, _PLACEMENT)
    cache = procs["consumer"].spec["proxies"]["cache"]
    assert cache["deadline"] == _placement.DEFAULT_SEAM_DEADLINE
    assert "deadlines" not in cache  # no per-op overrides were configured


_PLACEMENT_OVERRIDES = """
[processes.provider]
components = ["MemCache"]

[processes.consumer]
components = ["Consumer"]
seam_deadline = 4.0

[processes.consumer.seam_deadlines]
slow = 0.5
"""


def test_placement_honours_per_process_and_per_operation_overrides(tmp_path, monkeypatch):
    procs = _run_conductor_once(tmp_path, monkeypatch, _PLACEMENT_OVERRIDES)
    cache = procs["consumer"].spec["proxies"]["cache"]
    assert cache["deadline"] == 4.0                 # per-process default
    assert cache["deadlines"] == {"slow": 0.5}      # per-operation override
