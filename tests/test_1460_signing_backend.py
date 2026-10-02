"""Signing through `cryptography` when it is installed, and refusing to sign on
a network without it (issue #1460).

Every behaviour here is checked in BOTH configurations: with `cryptography`
importable (it is a declared test dependency, so it is on every CI job), and
without it. The absence is simulated by setting every `cryptography*` entry of
`sys.modules` to ``None``, which makes any import of the package raise
`ImportError`, and by dropping the backend's cached probe. `_blocked()` restores
both by value afterwards, and a test below holds it to that.

One test runs a fresh interpreter with the import blocked before `revl` is
imported at all, so a module-level `import cryptography` anywhere on the signing
paths would be caught there and not masked by a module this process already
loaded.

What these tests do not measure: timing. `tools/ecdsa_timing_demo.py` is the
demonstration that the pure signer's time follows the nonce's bit pattern and
the backend's does not; a timing assertion on a shared CI runner would be a
flaky test, so it is not one.
"""

from __future__ import annotations

import ast
import contextlib
import dataclasses
import json
import os
import random
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from revl import _ecdsa_backend
from revl import peer_identity as pi
from revl import pool_dispatch as pd
from revl import tee_quote as tq
from revl.mcp import quorum

ROOT = Path(__file__).resolve().parent.parent

EXTRA = "revl[crypto]"
INSTALLED = "cryptography"
BLOCKED = "blocked"

KEY = tq.private_key_from_seed(tq.CURVE_P256, b"revl-1460-key")
BINDING = {"requestId": "r1", "hash": "h" * 64, "candidateHash": "c" * 64,
           "component": "Payments", "kind": "crossing", "round": 1,
           "action": "vote", "vote": "approve", "asToken": "bob"}


@contextlib.contextmanager
def _blocked():
    """`cryptography` made unimportable, then put back exactly as it was."""
    names = [name for name in sys.modules
             if name == "cryptography" or name.startswith("cryptography.")]
    saved = {name: sys.modules[name] for name in names}
    saved_probe = _ecdsa_backend._probe
    try:
        for name in names:
            sys.modules[name] = None
        sys.modules["cryptography"] = None
        _ecdsa_backend.reset()
        yield
    finally:
        for name in [n for n in sys.modules
                     if n == "cryptography" or n.startswith("cryptography.")]:
            if name not in saved:
                del sys.modules[name]
        sys.modules.update(saved)
        _ecdsa_backend._probe = saved_probe


@pytest.fixture(params=[INSTALLED, BLOCKED])
def config(request):
    """Run the test once with the backend and once without it."""
    if request.param == INSTALLED:
        saved_probe = _ecdsa_backend._probe
        _ecdsa_backend.reset()
        try:
            assert _ecdsa_backend.probe().available, _ecdsa_backend.probe()
            yield INSTALLED
        finally:
            _ecdsa_backend._probe = saved_probe
    else:
        with _blocked():
            assert not _ecdsa_backend.probe().available
            yield BLOCKED


def _refuses(call):
    with pytest.raises(tq.SigningBackendUnavailable) as caught:
        call()
    message = str(caught.value)
    assert EXTRA in message and "pip install" in message, message
    return message


# ---------------------------------------------------------------------------
# the two backends agree byte for byte
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("curve", [tq.CURVE_P256, tq.CURVE_P384],
                         ids=lambda c: c.name)
@pytest.mark.parametrize("digest", ["sha256", "sha384", "sha512"])
def test_the_two_backends_sign_byte_identically(curve, digest):
    """RFC 6979 on both sides, so the same key and message give the same
    `R || S`, and the same private key gives the same public point. This is
    what keeps every committed fixture and every pinned digest valid whichever
    backend a machine happens to have."""
    bound = dataclasses.replace(curve, digest=digest)
    draw = random.Random(0x1460 + len(digest) + curve.order_len)
    for index in range(25):
        key = draw.randrange(1, curve.n)
        message = draw.randbytes(draw.randrange(0, 200))
        assert _ecdsa_backend.sign(bound, key, message) == \
            tq.ecdsa_sign_pure(bound, key, message), (curve.name, digest, index)
        assert _ecdsa_backend.public_key(bound, key) == \
            tq._derive_public_key_pure(bound, key)


def test_ecdsa_sign_gives_the_same_bytes_in_both_configurations(config):
    expected = tq.ecdsa_sign_pure(tq.CURVE_P256, KEY, b"same bytes")
    assert tq.ecdsa_sign(tq.CURVE_P256, KEY, b"same bytes") == expected
    assert tq.signing_backend() == ("cryptography" if config == INSTALLED
                                    else "pure")


def test_with_the_extra_no_signing_reaches_the_pure_arithmetic(monkeypatch):
    """Routing, checked by making the pure path unusable: with `cryptography`
    installed, signing and deriving a public key never touch `_mul`."""
    assert tq.signing_backend() == "cryptography"

    def refuse(*_args, **_kwargs):
        raise AssertionError("the pure scalar multiplication was reached")

    monkeypatch.setattr(tq, "_mul", refuse)
    identity = pi.identity_from_seed("peer-a", b"routing")
    record = pi.sign_record(b"revl.test/v1\x00", {"n": 1}, identity,
                            network_exposed=True)
    assert record["signature"]
    assert tq.ecdsa_sign(tq.CURVE_P384, KEY, b"m", network_exposed=True)


def test_verification_stays_pure_and_accepts_either_backend(config,
                                                           monkeypatch):
    """`ecdsa_verify` never calls the backend: it is broken here and
    verification still answers, in both directions."""
    signature = tq.ecdsa_sign(tq.CURVE_P256, KEY, b"verify me")
    public = tq.derive_public_key(tq.CURVE_P256, KEY)

    def broken(*_args, **_kwargs):
        raise AssertionError("verification reached the signing backend")

    for name in ("probe", "sign", "public_key", "supports"):
        monkeypatch.setattr(_ecdsa_backend, name, broken)
    assert tq.ecdsa_verify(tq.CURVE_P256, public, b"verify me", signature)
    assert not tq.ecdsa_verify(tq.CURVE_P256, public, b"other", signature)


# ---------------------------------------------------------------------------
# the refusal
# ---------------------------------------------------------------------------


def test_a_network_exposed_signature_needs_the_extra(config):
    call = lambda: tq.ecdsa_sign(tq.CURVE_P256, KEY, b"m",  # noqa: E731
                                 network_exposed=True)
    if config == INSTALLED:
        assert call() == tq.ecdsa_sign_pure(tq.CURVE_P256, KEY, b"m")
    else:
        message = _refuses(call)
        assert "not installed" in message


def test_local_signing_keeps_the_pure_path_in_both_configurations(config):
    """Fixtures, the reference attester and records written to a file are not
    network-exposed, and they keep working without the extra."""
    assert tq.ecdsa_sign(tq.CURVE_P256, KEY, b"m") == \
        tq.ecdsa_sign_pure(tq.CURVE_P256, KEY, b"m")
    identity = pi.identity_from_seed("peer-a", b"local")
    record = pi.sign_record(b"revl.test/v1\x00", {"n": 1}, identity)
    assert pi.verify_record(b"revl.test/v1\x00", record,
                            identity.public_key)[0]


def test_a_network_exposed_record_refuses_before_touching_the_key(
        config, monkeypatch):
    """`sign_record` derives the public key before it signs, and that is a
    scalar multiplication by the private key. The refusal comes first, so the
    pure arithmetic never runs on a network-exposed key."""
    identity = pi.identity_from_seed("peer-a", b"exposed")
    if config == BLOCKED:
        def refuse(*_args, **_kwargs):
            raise AssertionError("the private key reached the pure arithmetic")

        monkeypatch.setattr(tq, "_mul", refuse)
        _refuses(lambda: pi.sign_record(b"revl.test/v1\x00", {"n": 1},
                                        identity, network_exposed=True))
    else:
        record = pi.sign_record(b"revl.test/v1\x00", {"n": 1}, identity,
                                network_exposed=True)
        assert record == pi.sign_record(b"revl.test/v1\x00", {"n": 1},
                                        identity)
        assert pi.verify_record(b"revl.test/v1\x00", record,
                                identity.public_key)[0]


def test_sign_cast_is_network_exposed_by_default(config):
    """A cast proof exists to cross the MCP transport to a session the quorum
    does not trust, so the default is to refuse without the extra. On main this
    test fails: `sign_cast` signs in pure Python whatever is installed."""
    if config == BLOCKED:
        _refuses(lambda: quorum.sign_cast(KEY, BINDING))
    else:
        proof = quorum.sign_cast(KEY, BINDING)
        public = tq.derive_public_key(tq.CURVE_P256, KEY).hex()
        assert quorum._verify_cast(public, BINDING, proof)


def test_sign_cast_for_a_local_session_keeps_the_pure_path(config):
    proof = quorum.sign_cast(KEY, BINDING, network_exposed=False)
    public = tq.derive_public_key(tq.CURVE_P256, KEY).hex()
    assert quorum._verify_cast(public, BINDING, proof)


def test_an_installed_library_that_cannot_sign_deterministically_is_absent(
        monkeypatch):
    """`cryptography` older than 44 has no RFC 6979, and randomised nonces
    would break byte compatibility. Such an install is treated as absent, and
    the refusal says why."""
    from cryptography.hazmat.primitives.asymmetric import ec

    class OldECDSA:
        def __init__(self, algorithm):  # no `deterministic_signing`
            self.algorithm = algorithm

    saved_probe = _ecdsa_backend._probe
    monkeypatch.setattr(ec, "ECDSA", OldECDSA)
    _ecdsa_backend.reset()
    try:
        assert tq.signing_backend() == "pure"
        message = _refuses(lambda: tq.ecdsa_sign(
            tq.CURVE_P256, KEY, b"m", network_exposed=True))
        assert "44" in message
    finally:
        _ecdsa_backend._probe = saved_probe


def test_a_curve_the_backend_does_not_implement_is_left_to_the_pure_path(
        config):
    """The backend maps a `Curve` by every group parameter and by its hash,
    never by its name. A curve it does not map (here: P-256's name and group
    with SHA3-256, which RFC 6979 allows and the backend does not offer) is
    signed by the pure path, and a network-exposed signer on it refuses."""
    odd_hash = dataclasses.replace(tq.CURVE_P256, digest="sha3_256")
    assert tq.signing_backend(odd_hash) == "pure"
    assert tq.ecdsa_sign(odd_hash, KEY, b"m") == \
        tq.ecdsa_sign_pure(odd_hash, KEY, b"m")
    _refuses(lambda: tq.ecdsa_sign(odd_hash, KEY, b"m", network_exposed=True))
    odd_group = dataclasses.replace(tq.CURVE_P256, b=tq.CURVE_P256.b + 1)
    assert tq.signing_backend(odd_group) == "pure"


# ---------------------------------------------------------------------------
# the network-exposed callers
# ---------------------------------------------------------------------------


class _Bound(Exception):
    """Raised by the stub socket: the code got as far as creating a socket."""


def _stub_socket(monkeypatch):
    def create(*_args, **_kwargs):
        raise _Bound()
    monkeypatch.setattr(pd.socket, "socket", create)


def _runner(tmp_path):
    return pd.PeerRunner(
        charter_record={"pool_id": "p"},
        identity=pi.identity_from_seed("worker", b"worker"),
        operator_public=pi.identity_from_seed("op", b"op").public(),
        workspace=tmp_path)


def test_a_remote_serve_refuses_before_it_binds_without_the_extra(
        config, tmp_path, monkeypatch):
    """The peer signs a receipt for every task a remote party sends, which is
    the timing oracle the issue names. Without the extra it refuses BEFORE the
    socket exists. On main this test fails: the stub socket is reached."""
    _stub_socket(monkeypatch)
    runner = _runner(tmp_path)
    if config == BLOCKED:
        with pytest.raises(pd.DispatchError, match=r"revl\[crypto\]"):
            pd.serve(runner, host="0.0.0.0", port=0, allow_remote=True,
                     once=True)
        assert runner.network_exposed is False
    else:
        with pytest.raises(_Bound):
            pd.serve(runner, host="0.0.0.0", port=0, allow_remote=True,
                     once=True)
        assert runner.network_exposed is True


def test_a_loopback_serve_keeps_the_pure_path(config, tmp_path, monkeypatch):
    _stub_socket(monkeypatch)
    runner = _runner(tmp_path)
    with pytest.raises(_Bound):
        pd.serve(runner, host="127.0.0.1", port=0, once=True)
    assert runner.network_exposed is False


def test_a_runner_built_as_network_exposed_refuses_at_construction(
        config, tmp_path):
    build = lambda: pd.PeerRunner(  # noqa: E731
        charter_record={"pool_id": "p"},
        identity=pi.identity_from_seed("worker", b"worker"),
        operator_public=pi.identity_from_seed("op", b"op").public(),
        workspace=tmp_path, network_exposed=True)
    if config == BLOCKED:
        with pytest.raises(pd.DispatchError, match=r"revl\[crypto\]"):
            build()
    else:
        assert build().network_exposed is True


def test_a_remote_dispatch_refuses_before_it_touches_the_pool(config,
                                                             tmp_path):
    """The check is the first thing `dispatch_one` does. A task written to the
    ledger and then never signed would read as outstanding work that was never
    sent, so nothing may be read or written before it."""
    pool_dir = tmp_path / "pool"
    call = lambda host: pd.dispatch_one(  # noqa: E731
        pool_dir=pool_dir, peer_id="worker", host=host, port=9,
        source=b"", runner=pd.RUNNER_TEST_PY,
        dispatch_identity=pi.identity_from_seed("op", b"op"),
        attesting_identity=pi.identity_from_seed("at", b"at"), timeout=1.0)
    if config == BLOCKED:
        with pytest.raises(pd.DispatchError, match=r"revl\[crypto\]"):
            call("192.0.2.10")
        assert not pool_dir.exists()
    else:
        # Past the check, it fails on the pool that does not exist. (Taking
        # the pool lock on the way may create the directory, so only the
        # refusal is asserted to leave nothing behind.)
        with pytest.raises(Exception) as caught:
            call("192.0.2.10")
        assert EXTRA not in str(caught.value)
    assert pd.signer_exposure("127.0.0.1", "x") is False
    assert pd.signer_exposure("localhost", "x") is False


def _parse(argv):
    from revl.cli.parser import build_parser

    return build_parser().parse_args(argv)


def test_cli_run_pool_private_to_a_remote_peer_names_the_extra(
        tmp_path, capsys):
    """`revl run --pool private --peer-addr` off loopback, end to end through
    the parser: exit 2, the extra named on stderr, nothing written."""
    keys = tmp_path / "keys"
    keys.mkdir()
    pi.write_private_identity(keys / "op.key", pi.identity_from_seed("op", b"op"))
    pi.write_private_identity(keys / "at.key", pi.identity_from_seed("at", b"at"))
    (tmp_path / "a.rvl").write_text("component A {}\n", encoding="utf-8")
    args = _parse(["run", "--pool", "private", "--pool-dir",
                   str(tmp_path / "pool"), "--peer", "worker",
                   "--peer-addr", "192.0.2.10:9",
                   "--dispatch-identity", str(keys / "op.key"),
                   "--attest-identity", str(keys / "at.key"),
                   "--pool-runner", "test-py", str(tmp_path / "a.rvl")])
    with _blocked():
        code = pd.run_pool_command(args)
    assert code == 2
    assert EXTRA in capsys.readouterr().err
    assert not (tmp_path / "pool").exists()


def test_cli_pool_serve_off_loopback_names_the_extra(tmp_path, capsys,
                                                     monkeypatch):
    _stub_socket(monkeypatch)
    worker = pi.identity_from_seed("worker", b"worker")
    pi.write_private_identity(tmp_path / "w.key", worker)
    pi.write_public_identity(tmp_path / "op.pub",
                             pi.identity_from_seed("op", b"op").public())
    (tmp_path / "charter.json").write_text(json.dumps({"pool_id": "p"}),
                                           encoding="utf-8")
    args = _parse(["pool", "serve", "--charter", str(tmp_path / "charter.json"),
                   "--identity-key", str(tmp_path / "w.key"),
                   "--operator-public", str(tmp_path / "op.pub"),
                   "--host", "0.0.0.0", "--allow-remote", "--once"])
    with _blocked():
        code = pd.serve_command(args)
    assert code == 2
    assert EXTRA in capsys.readouterr().err


# ---------------------------------------------------------------------------
# pool liveness (issue #1198): probes and heartbeats sign on the same channel
# ---------------------------------------------------------------------------

POOL_KEY = b"pool-operator-key-1460"
PROBE_OPERATOR = pi.identity_from_seed("operator", b"seed/operator/1460")
PROBE_WORKER = pi.identity_from_seed("worker", b"seed/worker/1460")


def _pool_with_one_member(tmp_path) -> tuple[Path, dict]:
    """A charter, a roster with one admitted member, and its directory: the
    least `pool_health.probe_member` reads before it signs."""
    from revl import peer_offer
    from revl import peer_pool as pp
    from revl.attest import key_id

    ceiling = ("compute()",)
    record = pp.sign_charter(pp.PoolCharter(
        pool_id="lab-1460", ceiling=ceiling,
        tiers={pp.ENTRY_TIER: pp.TierGrant(caps=ceiling)},
        admit_key_ids=(key_id(POOL_KEY),), revoke_key_ids=(key_id(POOL_KEY),),
        attest_key_ids=(), artifact_digests=("a" * 64,),
        identity_mode=pp.MODE_ASYMMETRIC), POOL_KEY)
    directory = pi.IdentityDirectory()
    directory.register(PROBE_WORKER.public())
    roster = pp.Roster(record["pool_id"], pp.canonical_digest(record))
    offer = peer_offer.PeerOffer(
        peer_id="worker", attestation=peer_offer.Attestation(trust="verified"),
        grant_ceiling=ceiling)
    join = pp.sign_join_identity(pp.JoinRequest(
        pool_id=record["pool_id"], charter_digest=pp.canonical_digest(record),
        peer_id="worker",
        offer=peer_offer.sign_offer_identity(offer, PROBE_WORKER),
        artifact_digest="a" * 64, nonce="n-0",
        issued_at=pp._iso(pp._utc_now())), PROBE_WORKER)
    verdict = pp.admit(record, join, charter_key=POOL_KEY, peer_keys={},
                       directory=directory, admitting_key_id=key_id(POOL_KEY),
                       roster=roster)
    assert verdict["verdict"] == pp.ADMIT, verdict
    pool_dir = tmp_path / "pool"
    pool_dir.mkdir()
    (pool_dir / pp.CHARTER_FILE).write_text(json.dumps(record),
                                            encoding="utf-8")
    pp.save_roster(pool_dir, roster)
    pp.save_directory(pool_dir, directory)
    return pool_dir, record


def _probe_cli(tmp_path, pool_dir, addr):
    from revl import pool_health as ph

    pi.write_private_identity(tmp_path / "op.key", PROBE_OPERATOR)
    args = _parse(["pool", "probe", "--dir", str(pool_dir), "--peer", "worker",
                   "--peer-addr", addr,
                   "--dispatch-identity", str(tmp_path / "op.key"),
                   "--timeout", "2"])
    return ph.probe_command(args)


def test_pool_probe_to_a_remote_member_names_the_extra(config, tmp_path,
                                                       capsys, monkeypatch):
    """`revl pool probe --peer-addr` off loopback: the operator's key signs a
    probe for a peer the pool does not trust. Without the extra it exits 2
    naming it, before anything is signed, sent or recorded. With the extra it
    gets as far as the connection (stubbed here to fail, so the member reads
    unreachable and the command exits 1)."""
    from revl import pool_health as ph

    connections = []

    def unreachable(address, *_args, **_kwargs):
        connections.append(address)
        raise OSError("stubbed: no network in this test")

    monkeypatch.setattr(pd.socket, "create_connection", unreachable)
    pool_dir, _record = _pool_with_one_member(tmp_path)
    code = _probe_cli(tmp_path, pool_dir, "192.0.2.10:9")
    err = capsys.readouterr().err
    if config == BLOCKED:
        assert code == 2
        assert EXTRA in err
        assert connections == []
        assert not (pool_dir / ph.HEALTH_FILE).exists()
    else:
        assert code == 1, err
        assert EXTRA not in err
        assert connections == [("192.0.2.10", 9)]


class _LockTaken(Exception):
    """Raised by the stub pool lock: the dispatch reached a locked
    transaction."""


def test_a_remote_dispatch_refuses_before_the_pool_lock_and_writes_nothing(
        config, tmp_path, monkeypatch):
    """Against a real pool with a member and a ledger on disk. Without the
    extra, a dispatch to a non-loopback peer refuses BEFORE its first locked
    transaction (issue #1198's `pool.lock`): the lock is never taken, no lock
    file appears, and every file in the pool directory, the ledger included,
    is byte-identical afterwards. With the extra, the same call does reach the
    lock, which is what makes the first half a statement about ordering and
    not about a stub that never fires."""
    from revl import pool_state

    pool_dir, record = _pool_with_one_member(tmp_path)
    from revl import peer_pool as pp

    pd.save_ledger(pool_dir, pd.DeliveryLedger(record["pool_id"],
                                               pp.canonical_digest(record)))
    before = {path.name: path.read_bytes()
              for path in sorted(pool_dir.iterdir())}
    assert pd.LEDGER_FILE in before
    assert pool_state.LOCK_FILE not in before

    taken = []

    @contextlib.contextmanager
    def lock(directory):
        taken.append(Path(directory))
        raise _LockTaken()
        yield  # pragma: no cover

    monkeypatch.setattr(pool_state, "locked", lock)
    call = lambda: pd.dispatch_one(  # noqa: E731
        pool_dir=pool_dir, peer_id="worker", host="192.0.2.10", port=9,
        source=b"pub fn f() -> Int {\n  return 1\n}\n",
        runner=pd.RUNNER_TEST_PY, dispatch_identity=PROBE_OPERATOR,
        attesting_identity=PROBE_OPERATOR, timeout=1.0)
    if config == BLOCKED:
        with pytest.raises(pd.DispatchError, match=r"revl\[crypto\]"):
            call()
        assert taken == []
    else:
        with pytest.raises(_LockTaken):
            call()
        assert taken == [pool_dir]
    after = {path.name: path.read_bytes()
             for path in sorted(pool_dir.iterdir())}
    assert after == before


def test_pool_probe_on_loopback_keeps_the_pure_path(config, tmp_path, capsys):
    import socket

    with socket.socket() as spare:
        spare.bind(("127.0.0.1", 0))
        port = spare.getsockname()[1]
    pool_dir, _record = _pool_with_one_member(tmp_path)
    code = _probe_cli(tmp_path, pool_dir, f"127.0.0.1:{port}")
    assert code == 1  # nobody listening: unreachable, not refused
    assert EXTRA not in capsys.readouterr().err


def test_a_network_exposed_runner_signs_its_heartbeat_through_the_flag(
        config, tmp_path, monkeypatch):
    """The heartbeat is the second signer on the peer's channel. A runner
    marked network-exposed hands its flag to `answer_probe`, so the heartbeat
    is refused rather than signed on the pure path, and `handle` keeps its
    never-raises contract by answering with a named refusal. `serve` already
    refuses to bind first, so this is defence in depth, checked by marking the
    flag directly."""
    from revl import peer_pool as pp
    from revl import pool_health as ph

    pool_dir, record = _pool_with_one_member(tmp_path)
    runner = pd.PeerRunner(charter_record=record, identity=PROBE_WORKER,
                           operator_public=PROBE_OPERATOR.public(),
                           workspace=tmp_path / "work")
    runner.network_exposed = True  # as `serve` would, without its check
    body = ph.build_probe(pool_id=record["pool_id"],
                          charter_digest=pp.canonical_digest(record),
                          peer_id="worker")
    signed = ph.sign_probe(body, PROBE_OPERATOR)
    if config == BLOCKED:
        answer = runner.handle(signed)  # a refusal, never an exception
        assert answer.get("ok") is False, answer
        assert answer["link"] == pd.LINK_TASK_SHAPE
        assert "SigningBackendUnavailable" in answer["reason"]
        assert EXTRA in answer["reason"]
        assert "heartbeat" not in answer
    else:
        answer = runner.handle(signed)
        assert answer.get("ok"), answer
        assert pi.verify_record(ph.HEARTBEAT_DOMAIN, answer["heartbeat"],
                                PROBE_WORKER.public_key)[0]


# ---------------------------------------------------------------------------
# the simulation itself, and the import boundary
# ---------------------------------------------------------------------------


def test_blocking_the_import_restores_sys_modules_by_value():
    """The block mutates process-global state shared by every test in the
    session, so it must hand back exactly what it took: the same module
    OBJECTS under the same names, not merely the same keys."""
    import cryptography.hazmat.primitives.asymmetric.ec  # noqa: F401

    before = dict(sys.modules)
    probe_before = _ecdsa_backend._probe
    with _blocked():
        assert sys.modules["cryptography"] is None
        with pytest.raises(ImportError):
            from cryptography.hazmat.primitives.asymmetric import ec  # noqa: F401
    after = dict(sys.modules)
    assert after.keys() == before.keys()
    assert all(after[name] is before[name] for name in before)
    assert _ecdsa_backend._probe is probe_before


def test_a_fresh_interpreter_without_cryptography_refuses_and_still_signs(
        tmp_path):
    """No `sys.modules` left over from this process: `cryptography` is blocked
    by a meta-path finder before `revl` is imported, so a module-level import
    of it on any signing path would fail here rather than be masked."""
    script = textwrap.dedent("""
        import importlib.abc, json, os, sys

        class Block(importlib.abc.MetaPathFinder):
            def find_spec(self, name, path=None, target=None):
                if name == "cryptography" or name.startswith("cryptography."):
                    raise ImportError("blocked for the test: " + name)
                return None

        sys.meta_path.insert(0, Block())
        import revl
        from revl import tee_quote, peer_identity, pool_dispatch, pool_receipt
        from revl.mcp import quorum

        key = tee_quote.private_key_from_seed(tee_quote.CURVE_P256, b"k")
        out = {"root": os.path.dirname(revl.__file__),
               "backend": tee_quote.signing_backend(),
               "local": tee_quote.ecdsa_sign(tee_quote.CURVE_P256, key,
                                             b"m").hex()}
        try:
            quorum.sign_cast(key, {"round": 1})
            out["cast"] = "signed"
        except tee_quote.SigningBackendUnavailable as error:
            out["cast"] = str(error)
        print(json.dumps(out))
    """)
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env["PYTHONPATH"] = str(ROOT / "src")
    result = subprocess.run([sys.executable, "-c", script], env=env,
                            capture_output=True, text=True, timeout=120,
                            check=False)
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout.strip().splitlines()[-1])
    # The child imported the same revl this process is testing.
    assert Path(out["root"]).resolve() == Path(tq.__file__).resolve().parent
    assert out["backend"] == "pure"
    assert out["local"] == tq.ecdsa_sign_pure(tq.CURVE_P256,
                                              tq.private_key_from_seed(
                                                  tq.CURVE_P256, b"k"),
                                              b"m").hex()
    assert EXTRA in out["cast"]


def test_only_the_backend_module_imports_cryptography():
    """revl has no required runtime dependency. The optional one is imported
    in exactly one module, lazily, so everything else imports without it and
    the differentials still have a pure implementation to compare."""
    offenders = []
    for path in sorted((ROOT / "src" / "revl").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and not node.level:
                names = [node.module or ""]
            else:
                continue
            if any(name.split(".")[0] == "cryptography" for name in names):
                offenders.append(str(path.relative_to(ROOT)))
    assert sorted(set(offenders)) == ["src/revl/_ecdsa_backend.py"], offenders
    backend = ast.parse((ROOT / "src" / "revl" / "_ecdsa_backend.py")
                        .read_text(encoding="utf-8"))
    top_level = [node for node in backend.body
                 if isinstance(node, (ast.Import, ast.ImportFrom))]
    assert not any(
        (getattr(node, "module", None) or "").startswith("cryptography")
        or any(a.name.startswith("cryptography") for a in node.names)
        for node in top_level), "the backend must import cryptography lazily"
