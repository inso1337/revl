"""The node consumer of a guarded seam: does `bridge.ts` genuinely SEAL?

Item 118's `CorrelationGuard` authenticates each crossing by an HMAC under the
caller's own per-process secret. `backends/typescript/bridge.ts` had no
`correlation` parameter at all, so a node consumer sent a pre-118 request and the
guard would have refused it as `malformed-envelope`. `placement.py` therefore
refused to install the guard whenever ANY local consumer was on the node tier
(`_CORRELATION_SEALING_TIERS`), which left that py provider unguarded — the
whole seam's protection removed by one consumer's missing feature.

Adding the parameter is not the proof. Sealing is byte-exact or it is worthless:
the tag is an HMAC over `revl.attest._canonical_bytes` of the envelope, so a node
sealer that sorts keys differently, spells `generation` as a float, or emits
whitespace produces a tag the py guard reads as a FORGERY. So this suite spawns a
real node process against a real py provider running the real guard, and pins:

  1. a node-sealed crossing is ADMITTED and dispatched;
  2. a second call is admitted too — a fresh idempotency key per call, not one
     envelope reused;
  3. an exact replay of a node-sealed envelope is refused `duplicate-envelope`
     (it authenticated, then failed freshness — which is only reachable if the
     tag verified);
  4. a node consumer sealing under the WRONG secret is refused
     `forged-envelope`, so (1) is a real check and not a guard that admits
     anything shaped like an envelope.

Skips cleanly when node is absent or cannot load a bare `.ts` entry point —
never a false pass.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import deploy  # noqa: E402

CLIENT = ROOT / "tests" / "_ts_correlation_client.ts"
SECRET = bytes(range(32))
IDENTITY = "node-consumer"
COMPOSITION = "composition-under-test"


def _bridge():
    """backends/python/bridge.py, imported directly (needs no cordis)."""
    name = "revl_ts_correlation_test_bridge"
    spec = importlib.util.spec_from_file_location(
        name, ROOT / "backends" / "python" / "bridge.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


bridge = _bridge()


def _node_skip_reason() -> str | None:
    node = shutil.which("node")
    if node is None:
        return "node not on PATH for the ts correlation client"
    try:
        probe = subprocess.run([node, str(CLIENT)],
                               capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"could not probe the ts correlation client with node: {exc}"
    combined = probe.stdout + probe.stderr
    if ("Cannot use import statement outside a module" in combined
            or "Failed to load the ES module" in combined):
        return (f"node ({node}) cannot load a .ts entry point as an ES module "
                "here, so the ts tier runtime is unusable on this box")
    return None


_SKIP = _node_skip_reason()
if _SKIP is not None:
    pytest.skip(_SKIP, allow_module_level=True)


class _Counter:
    """Records every dispatch, so a refused crossing is proved never to reach
    the service rather than merely to have been answered with an error."""

    def __init__(self):
        self.calls: list = []

    def get(self, name):
        self.calls.append(name)
        return f"v:{name}"


class _Ctx:
    def __init__(self, table):
        self._table = table

    def get(self, key):
        return self._table.get(key)


class _GuardedSeam:
    """A real py `bridge.serve` over UDS with a real `CorrelationGuard`, on its
    own event-loop thread."""

    def __init__(self, path: str):
        self.service = _Counter()
        self.guard = deploy.CorrelationGuard({IDENTITY: SECRET})
        self._loop = asyncio.new_event_loop()
        ready = threading.Event()

        def run():
            asyncio.set_event_loop(self._loop)
            server = self._loop.run_until_complete(bridge.serve(
                _Ctx({"cache": self.service}), {"cache": ["get"]}, path,
                correlation=self.guard))
            ready.set()
            self._loop.run_forever()
            server.close()
            self._loop.run_until_complete(server.wait_closed())
            self._loop.close()

        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()
        assert ready.wait(10)

    def stop(self):
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)


def _run_client(tmp_path, sock, *, mode="fresh", calls=("a",), secret=SECRET):
    cfg = {"socket": str(sock), "mode": mode, "calls": list(calls),
           "correlation": {"composition_id": COMPOSITION,
                           "peer_identity": IDENTITY,
                           "secret": secret.hex()}}
    cfg_path = tmp_path / f"client_{mode}_{secret[:1].hex()}.json"
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
    result = subprocess.run([shutil.which("node"), str(CLIENT), str(cfg_path)],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, timeout=120)
    assert result.returncode == 0, result.stdout
    return result.stdout


@pytest.fixture
def sockdir():
    """A short-pathed directory for Unix sockets (the macOS ``sun_path`` limit;
    same rationale as tests/test_deploy_118.py)."""
    import shutil as _shutil
    import tempfile

    directory = tempfile.mkdtemp(prefix="rvts", dir="/tmp")
    try:
        yield Path(directory)
    finally:
        _shutil.rmtree(directory, ignore_errors=True)


@pytest.fixture
def seam(sockdir):
    sock = sockdir / "s"
    seam = _GuardedSeam(str(sock))
    try:
        yield seam, sock
    finally:
        seam.stop()


def test_a_node_sealed_crossing_is_admitted_by_the_py_guard(tmp_path, seam):
    """The load-bearing one: the tag the ts bridge computes verifies under the
    py guard, so the envelope's canonical bytes agree ACROSS the two languages.
    Two calls, because a sealer that reused one envelope would fail the second
    on freshness."""
    guarded, sock = seam
    out = _run_client(tmp_path, sock, calls=("a", "b"))
    assert 'OK a "v:a"' in out, out
    assert 'OK b "v:b"' in out, out
    assert guarded.service.calls == ["a", "b"]


def test_a_replayed_node_envelope_is_refused_as_a_duplicate(tmp_path, seam):
    """An exact replay of a crossing the ts bridge itself sealed. Reaching
    `duplicate-envelope` at all means the tag verified first — the guard checks
    authenticity before freshness — so this pins the seal and the ledger in one
    round trip."""
    guarded, sock = seam
    out = _run_client(tmp_path, sock, mode="frozen", calls=("a", "b"))
    assert 'OK a "v:a"' in out, out
    assert f"ERR b correlation refused: {deploy.REJECT_DUPLICATE}" in out, out
    assert guarded.service.calls == ["a"]      # the replay never dispatched


def test_a_node_consumer_sealing_under_the_wrong_secret_is_refused(tmp_path, seam):
    """Non-vacuity for the admission above: the guard is really checking the
    tag, so a node client that seals correctly is passing a check a wrong
    secret fails."""
    guarded, sock = seam
    out = _run_client(tmp_path, sock, calls=("a",), secret=bytes(32))
    assert f"ERR a correlation refused: {deploy.REJECT_FORGED}" in out, out
    assert guarded.service.calls == []


def test_the_node_tier_is_listed_as_a_sealing_tier(tmp_path, seam):
    """The wiring this unlocks. `placement.py` installs a provider's guard only
    when EVERY local consumer runs on a tier that can seal; with the node tier
    missing from that set, one node consumer disarmed the whole provider. The
    tier is listed only because the three tests above pass — installing a guard
    in front of a caller that cannot satisfy it breaks the seam rather than
    hardening it."""
    from revl.placement import _CORRELATION_SEALING_TIERS, _canonical_backend
    assert _canonical_backend("ts") in _CORRELATION_SEALING_TIERS
