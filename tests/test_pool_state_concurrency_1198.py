"""Two operator processes writing one pool directory at once (issue #1198).

`revl run --pool private`, `revl pool withdraw`, `revl pool probe` and
`revl pool join` all read pool state, change it and write it back: the roster,
the delivery ledger, the health record and the key directory. Nothing
serialised those read-modify-write cycles, and `dispatch_one` held its copy of
the roster and the ledger across the network round trip to the peer, which can
take as long as the task runs.

Each test below runs two REAL `python -m revl` processes against one pool
directory and forces the interleaving that loses an update. The forcing is a
peer the test controls: it holds its answer until the other process has done
its work (a `threading.Event`) or until both processes have sent (a
`threading.Barrier`). No sleeps, so the interleaving is the same on every run
and does not depend on how loaded the machine is.

On the tree before the fix, every one of these fails:

* a withdrawal made while a dispatch is in flight is UNDONE when the dispatch
  finishes: the withdrawn peer is back in `roster.json` and its orphaned task
  reads `delivered`;
* two dispatches to two members leave one task in `ledger.json`, not two;
* two probes of two members leave one row in `health.json`, not two.
"""

import ast
import json
import os
import socket
import subprocess
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import peer_identity as pi  # noqa: E402
from revl import peer_offer, peer_pool as pp  # noqa: E402
from revl import pool_dispatch as pd  # noqa: E402
from revl import pool_health as ph  # noqa: E402
from revl import pool_state as ps  # noqa: E402
from revl.attest import key_id  # noqa: E402
from revl.peer_offer import Attestation, PeerOffer  # noqa: E402

OP_KEY = b"pool-operator-key-1198-concurrency"
WORKER = "peer-worker"
OTHER = "peer-other"

#: Fixture identities. A seed committed to a repository is a published private
#: key, so these are used nowhere but here.
WORKER_ID = pi.identity_from_seed(WORKER, b"seed/worker/1198-concurrency")
OTHER_ID = pi.identity_from_seed(OTHER, b"seed/other/1198-concurrency")
OPERATOR_ID = pi.identity_from_seed("operator", b"seed/operator/1198-conc")
ATTESTOR_ID = pi.identity_from_seed("attestor", b"seed/attestor/1198-conc")

CEILING = ("compute()",)
IDENTITIES = {WORKER: WORKER_ID, OTHER: OTHER_ID}

PURE_SOURCE = b"""pub fn add(a: Int, b: Int) -> Int {
  return a + b
}

test "add is addition" {
  assert add(2, 3) == 5
}
"""
PURE_DIGEST = pd.artifact_digest(PURE_SOURCE)

#: How long any one wait may take before the test fails loudly. A generous
#: bound on a loaded machine, not a timing assumption: nothing waits for it to
#: elapse on the passing path.
WAIT = 240


# ---------------------------------------------------------------------------
# the pool, the key files and the CLI
# ---------------------------------------------------------------------------


def _join(record, peer_id, identity, nonce):
    offer = PeerOffer(peer_id=peer_id,
                      attestation=Attestation(trust="verified"),
                      grant_ceiling=CEILING)
    return pp.sign_join_identity(pp.JoinRequest(
        pool_id=record["pool_id"], charter_digest=pp.canonical_digest(record),
        peer_id=peer_id, offer=peer_offer.sign_offer_identity(offer, identity),
        artifact_digest=PURE_DIGEST, nonce=nonce,
        issued_at=pp._iso(pp._utc_now())), identity)


def pool_on_disk(tmp_path):
    record = pp.sign_charter(pp.PoolCharter(
        pool_id="lab-concurrency", ceiling=CEILING,
        tiers={pp.ENTRY_TIER: pp.TierGrant(caps=CEILING)},
        admit_key_ids=(key_id(OP_KEY),), revoke_key_ids=(key_id(OP_KEY),),
        attest_key_ids=(ATTESTOR_ID.key_id,),
        artifact_digests=(PURE_DIGEST,),
        identity_mode=pp.MODE_ASYMMETRIC), OP_KEY)
    directory = pi.IdentityDirectory()
    for identity in (WORKER_ID, OTHER_ID, ATTESTOR_ID):
        directory.register(identity.public())
    roster = pp.Roster(record["pool_id"], pp.canonical_digest(record))
    for n, (peer, identity) in enumerate(IDENTITIES.items()):
        verdict = pp.admit(record, _join(record, peer, identity, f"n-{n}"),
                           charter_key=OP_KEY, peer_keys={},
                           directory=directory,
                           admitting_key_id=key_id(OP_KEY), roster=roster)
        assert verdict["verdict"] == pp.ADMIT, verdict
    pool_dir = tmp_path / "pool"
    pool_dir.mkdir(parents=True)
    (pool_dir / pp.CHARTER_FILE).write_text(json.dumps(record),
                                            encoding="utf-8")
    pp.save_roster(pool_dir, roster)
    pp.save_directory(pool_dir, directory)

    keys = tmp_path / "keys"
    keys.mkdir()
    pi.write_private_identity(keys / "operator.key", OPERATOR_ID)
    pi.write_private_identity(keys / "attestor.key", ATTESTOR_ID)
    (keys / "operator.secret").write_bytes(OP_KEY)
    artifact = tmp_path / "work.rvl"
    artifact.write_bytes(PURE_SOURCE)
    return pool_dir, record, keys, artifact


def _env() -> dict:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(ROOT / "src"), env.get("PYTHONPATH", "")]).rstrip(os.pathsep)
    return env


def start(*argv: str) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-m", "revl", *argv],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, env=_env())


def finish(process: subprocess.Popen) -> tuple[int, str, str]:
    out, err = process.communicate(timeout=WAIT)
    return process.returncode, out, err


def dispatch(pool_dir, keys, artifact, peer, addr) -> subprocess.Popen:
    return start("run", "--pool", "private", "--pool-dir", str(pool_dir),
                 "--peer", peer, "--peer-addr", addr,
                 "--dispatch-identity", str(keys / "operator.key"),
                 "--attest-identity", str(keys / "attestor.key"),
                 "--pool-runner", "test-py", "--pool-timeout", str(WAIT),
                 str(artifact))


def probe(pool_dir, keys, peer, addr) -> subprocess.Popen:
    return start("pool", "probe", "--dir", str(pool_dir), "--peer", peer,
                 "--peer-addr", addr,
                 "--dispatch-identity", str(keys / "operator.key"),
                 "--timeout", str(WAIT))


def read(pool_dir, name) -> dict:
    return json.loads((Path(pool_dir) / name).read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# a peer that answers when the test says so
# ---------------------------------------------------------------------------


class HeldPeer:
    """A real listening socket for one member. It reads one frame, reports
    that it arrived, waits for `release`, then answers honestly through the
    member's own `PeerRunner`.

    The arrival is the synchronisation point: by the time a frame reaches the
    peer, the operator process that sent it has already read and written
    whatever pool state it touches before sending."""

    def __init__(self, tmp_path, record, peer_id, *, release=None,
                 barrier=None):
        self.runner = pd.PeerRunner(
            charter_record=record, identity=IDENTITIES[peer_id],
            operator_public=OPERATOR_ID.public(),
            workspace=tmp_path / f"peer-{peer_id}", timeout=WAIT)
        self.arrived = threading.Event()
        self.release = release
        self.barrier = barrier
        self.error = []
        self._server = socket.socket()
        self._server.bind(("127.0.0.1", 0))
        self._server.listen(1)
        self.addr = f"127.0.0.1:{self._server.getsockname()[1]}"
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        try:
            conn, _ = self._server.accept()
            with conn:
                frame = json.loads(pd._read_frame(conn))
                self.arrived.set()
                if self.barrier is not None:
                    self.barrier.wait(WAIT)
                if self.release is not None:
                    assert self.release.wait(WAIT), "never released"
                answer = self.runner.handle(frame)
                conn.sendall(json.dumps(answer).encode() + b"\n")
        except Exception as error:  # noqa: BLE001 - surfaced by the test
            self.error.append(error)
        finally:
            self._server.close()

    def join(self):
        self._thread.join(WAIT)
        assert not self.error, self.error


# ---------------------------------------------------------------------------
# the three races
# ---------------------------------------------------------------------------


def test_a_withdrawal_during_a_dispatch_is_not_undone(tmp_path):
    """The operator withdraws WORKER while a task to it is in flight.

    Before the fix, `dispatch_one` still held the roster and ledger it read
    before sending, and wrote them back when the answer came: the withdrawn
    peer reappeared in the roster with its caps, and the task the withdrawal
    orphaned was overwritten as delivered."""
    pool_dir, _record, keys, artifact = pool_on_disk(tmp_path)
    release = threading.Event()
    peer = HeldPeer(tmp_path, _record, WORKER, release=release)

    running = dispatch(pool_dir, keys, artifact, WORKER, peer.addr)
    assert peer.arrived.wait(WAIT), finish(running)
    task_id = next(iter(read(pool_dir, pd.LEDGER_FILE)["tasks"]))

    withdrawn = subprocess.run(
        [sys.executable, "-m", "revl", "pool", "withdraw", "--dir",
         str(pool_dir), "--peer", WORKER, "--reason", "left mid-task",
         "--key", str(keys / "operator.secret")],
        capture_output=True, text=True, env=_env(), timeout=WAIT)
    assert withdrawn.returncode == 0, (withdrawn.stdout, withdrawn.stderr)
    assert json.loads(withdrawn.stdout)["orphaned"] == [task_id]

    release.set()
    code, out, err = finish(running)
    peer.join()

    roster = read(pool_dir, pp.ROSTER_FILE)
    assert WORKER not in roster["members"], "the withdrawal was undone"
    assert WORKER in roster["revoked"]
    ledger = read(pool_dir, pd.LEDGER_FILE)
    assert ledger["tasks"][task_id]["state"] == pd.STATE_ORPHANED, \
        "the orphaned task was overwritten"
    # The late answer is refused and visible, not absorbed.
    assert code == 1, (out, err)
    assert [e["event"] for e in ledger["events"]
            if e.get("task_id") == task_id][-1] in ("refused",
                                                    "delivery-refused")


def test_two_dispatches_to_two_members_keep_both_tasks(tmp_path):
    pool_dir, record, keys, artifact = pool_on_disk(tmp_path)
    barrier = threading.Barrier(2)
    peers = {peer: HeldPeer(tmp_path, record, peer, barrier=barrier)
             for peer in (WORKER, OTHER)}
    running = [dispatch(pool_dir, keys, artifact, peer, held.addr)
               for peer, held in peers.items()]
    results = [finish(process) for process in running]
    for held in peers.values():
        held.join()
    assert [code for code, _o, _e in results] == [0, 0], results

    ledger = read(pool_dir, pd.LEDGER_FILE)
    delivered = sorted(entry["peer_id"] for entry in ledger["tasks"].values()
                       if entry["state"] == pd.STATE_DELIVERED)
    assert delivered == [OTHER, WORKER], ledger["tasks"]


def test_two_probes_of_two_members_keep_both_rows(tmp_path):
    pool_dir, record, keys, _artifact = pool_on_disk(tmp_path)
    barrier = threading.Barrier(2)
    peers = {peer: HeldPeer(tmp_path, record, peer, barrier=barrier)
             for peer in (WORKER, OTHER)}
    running = [probe(pool_dir, keys, peer, held.addr)
               for peer, held in peers.items()]
    results = [finish(process) for process in running]
    for held in peers.values():
        held.join()
    assert [code for code, _o, _e in results] == [0, 0], results

    members = read(pool_dir, ph.HEALTH_FILE)["members"]
    assert sorted(members) == [OTHER, WORKER], members
    assert {row["state"] for row in members.values()} == {ph.HEALTH_LIVE}


def test_one_join_submitted_twice_at_once_admits_once(tmp_path):
    """Design note 550's A5 says a join submitted twice CONCURRENTLY is refused
    the second time. Across processes that needs the lock: each `pool join`
    reads the spent-nonce set, and without serialisation both could read it
    before either wrote it. This one cannot be forced to fail on the old tree
    with a barrier, since `join` makes no network call to hold; it is here as
    the guard, and it passes on every interleaving only because the gate's
    read and write are one transaction."""
    pool_dir, record, keys, _artifact = pool_on_disk(tmp_path)
    third = pi.identity_from_seed("peer-third", b"seed/third/1198-conc")
    directory = pp.load_directory(pool_dir)
    directory.register(third.public())
    pp.save_directory(pool_dir, directory)
    join = tmp_path / "third.join.json"
    join.write_text(json.dumps(_join(record, "peer-third", third, "n-3")),
                    encoding="utf-8")
    argv = ("pool", "join", "--dir", str(pool_dir), "--join", str(join),
            "--key", str(keys / "operator.secret"))
    results = [finish(p) for p in [start(*argv), start(*argv)]]
    assert sorted(code for code, _o, _e in results) == [0, 1], results
    refused = next(out for code, out, _e in results if code == 1)
    assert json.loads(refused)["link"] in ("replayed-join",
                                           "duplicate-member"), refused
    assert "peer-third" in read(pool_dir, pp.ROSTER_FILE)["members"]


# ---------------------------------------------------------------------------
# the mechanism, held in place
# ---------------------------------------------------------------------------


#: Every function that writes a pool state file after reading it.
STATE_WRITERS = {"save_roster", "save_directory", "save_ledger",
                 "save_health"}


def _name(func) -> str:
    return func.attr if isinstance(func, ast.Attribute) \
        else getattr(func, "id", "")


def _unlocked_writes(node, locked=False, where="<module>"):
    if isinstance(node, ast.With) and any(
            isinstance(item.context_expr, ast.Call)
            and _name(item.context_expr.func) == "locked"
            for item in node.items):
        locked = True
    if isinstance(node, ast.Call) and _name(node.func) in STATE_WRITERS \
            and not locked:
        yield where, node.lineno, _name(node.func)
    for child in ast.iter_child_nodes(node):
        inner = child.name if isinstance(child, ast.FunctionDef) else where
        yield from _unlocked_writes(child, locked, inner)


@pytest.mark.parametrize("module", [pp, pd, ph])
def test_every_state_write_in_the_pool_modules_is_under_the_lock(module):
    """A structural check, because the races above only show the orderings
    they force. Every call that writes the roster, the key directory, the
    ledger or the health record sits lexically inside a
    `with pool_state.locked(...)` block, so a writer added later without the
    lock fails here rather than in a deployment."""
    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    calls = [c for c in ast.walk(tree) if isinstance(c, ast.Call)
             and _name(c.func) in STATE_WRITERS]
    assert calls, "the check would pass vacuously"
    assert list(_unlocked_writes(tree)) == []


def test_the_lock_is_not_reentrant_and_says_so(tmp_path):
    with ps.locked(tmp_path):
        with pytest.raises(ps.PoolStateError):
            with ps.locked(tmp_path):
                pass
    # Released on the way out, including after the refusal above.
    with ps.locked(tmp_path):
        pass


def test_a_failed_write_leaves_the_old_file_whole(tmp_path, monkeypatch):
    target = tmp_path / "roster.json"
    ps.write_json(target, {"members": {"a": 1}})
    before = target.read_bytes()

    def refuse(_src, _dst):
        raise OSError("disk full")

    monkeypatch.setattr(ps.os, "replace", refuse)
    with pytest.raises(OSError):
        ps.write_json(target, {"members": {"a": 1, "b": 2}})
    assert target.read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["roster.json"]


def test_the_bytes_are_the_ones_the_pool_always_wrote(tmp_path):
    payload = {"b": [1, 2], "a": {"z": 1, "y": "x"}}
    ps.write_json(tmp_path / "f.json", payload)
    assert (tmp_path / "f.json").read_text(encoding="utf-8") == \
        json.dumps(payload, indent=2, sort_keys=True) + "\n"
