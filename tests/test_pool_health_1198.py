"""Liveness for private-pool members: `revl pool probe`, the health record,
and `revl pool status --require-live` (item 524, issue #1198;
`src/revl/pool_health.py`).

What is under test, in the order it matters.

1. **THE CLI, END TO END.** A pool is stood up with `revl pool init`, a peer
   draws a key with `pool keygen`, joins with `pool request` / `pool join`,
   and runs `pool serve` as its own OS process. The operator probes it with
   `pool probe`, reads `pool status`, and uses `pool status --require-live`
   as a health check. Then the peer process is killed and the same commands
   report it unreachable. Every step is a `python -m revl` subprocess, so the
   flags under test are the flags an operator types.

   **Non-vacuity.** Before this change `pool probe` did not exist (argparse
   refused the verb), `pool status` carried no liveness at all, and
   `--require-live` was refused. The first test asserts all three against the
   parser, so it fails on the old tree for the right reason.

2. **A HEARTBEAT IS A CLAIM.** Something answering at the address is not the
   member. Each way a heartbeat can fail to be the member's fresh answer is
   driven through a hostile far side and refused on its own link, and none of
   them is recorded as live.

3. **NO AUTHORITY MOVES.** A probe writes `health.json` and nothing else: the
   charter, roster and identity directory are byte-identical before and after,
   and an AST walk asserts the module calls none of the functions that change
   authority. Withdrawal stays `pool withdraw` under the revoke authority.

4. **THE LINK SET, both directions**, as `pool_dispatch` holds its own.

What these tests do NOT reach: two machines. The peer is a second OS process on
loopback, which is what `pool_dispatch`'s tests reach too, for the same reason.
"""

import ast
import json
import os
import socket
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import peer_identity as pi  # noqa: E402
from revl import peer_offer, peer_pool as pp  # noqa: E402
from revl import pool_dispatch as pd  # noqa: E402
from revl import pool_health as ph  # noqa: E402
from revl.attest import key_id  # noqa: E402
from revl.peer_offer import Attestation, PeerOffer  # noqa: E402

OP_KEY = b"pool-operator-key-1198-health"
WORKER = "peer-worker"
OTHER = "peer-other"

#: Deterministic identities, fixtures only: a seed committed to a repository is
#: a published private key (see `identity_from_seed`).
WORKER_ID = pi.identity_from_seed(WORKER, b"seed/worker/1198-health")
OTHER_ID = pi.identity_from_seed(OTHER, b"seed/other/1198-health")
OPERATOR_ID = pi.identity_from_seed("operator", b"seed/operator/1198-health")
ATTESTOR_ID = pi.identity_from_seed("attestor", b"seed/attestor/1198-health")
INTRUDER_ID = pi.identity_from_seed("intruder", b"seed/intruder/1198-health")

CEILING = ("compute()",)

PURE_SOURCE = b"""pub fn add(a: Int, b: Int) -> Int {
  return a + b
}

test "add is addition" {
  assert add(2, 3) == 5
}
"""
PURE_DIGEST = pd.artifact_digest(PURE_SOURCE)


# ---------------------------------------------------------------------------
# the CLI harness
# ---------------------------------------------------------------------------


def _env() -> dict:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(ROOT / "src"), env.get("PYTHONPATH", "")]).rstrip(os.pathsep)
    return env


def revl(*argv: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "revl", *argv],
                          capture_output=True, text=True, env=_env(),
                          timeout=180)


def ok(*argv: str) -> subprocess.CompletedProcess:
    done = revl(*argv)
    assert done.returncode == 0, (argv, done.stdout, done.stderr)
    return done


class Pool:
    """A pool directory and the key files, made only through the CLI."""

    def __init__(self, base: Path):
        self.base = base
        self.dir = base / "pool"
        self.keys = base / "keys"
        self.keys.mkdir(parents=True)
        (self.keys / "operator.secret").write_bytes(OP_KEY)
        for name in ("operator", "attestor", "alpha", "intruder"):
            ok("pool", "keygen", "--peer-id", name,
               "--out", str(self.key(name)), "--public", str(self.pub(name)))
        ok("pool", "init", "--dir", str(self.dir), "--pool-id", "lab",
           "--ceiling", CEILING[0], "--entry-caps", CEILING[0],
           "--artifact", PURE_DIGEST, "--key", str(self.secret),
           "--attest-identity", str(self.pub("attestor")))

    @property
    def secret(self) -> Path:
        return self.keys / "operator.secret"

    def key(self, name: str) -> Path:
        return self.keys / f"{name}.key"

    def pub(self, name: str) -> Path:
        return self.keys / f"{name}.pub"

    def admit(self, name: str) -> None:
        join = self.base / f"{name}.join.json"
        ok("pool", "register", "--dir", str(self.dir),
           "--public", str(self.pub(name)))
        ok("pool", "request", "--charter", str(self.dir / pp.CHARTER_FILE),
           "--peer-id", name, "--artifact", PURE_DIGEST,
           "--ceiling", CEILING[0], "--identity-key", str(self.key(name)),
           "--out", str(join))
        ok("pool", "join", "--dir", str(self.dir), "--join", str(join),
           "--key", str(self.secret))

    def probe(self, *extra: str, identity: str = "operator"):
        return revl("pool", "probe", "--dir", str(self.dir),
                    "--dispatch-identity", str(self.key(identity)),
                    "--timeout", "5", *extra)

    def status(self, *extra: str):
        return revl("pool", "status", "--dir", str(self.dir), *extra)

    def authority_bytes(self) -> dict:
        return {name: (self.dir / name).read_bytes()
                for name in (pp.CHARTER_FILE, pp.ROSTER_FILE,
                             pp.IDENTITIES_FILE)}


class Serving:
    """`revl pool serve` as its own process, and the port it printed."""

    def __init__(self, pool: Pool, name: str):
        self.process = subprocess.Popen(
            [sys.executable, "-m", "revl", "pool", "serve",
             "--charter", str(pool.dir / pp.CHARTER_FILE),
             "--identity-key", str(pool.key(name)),
             "--operator-public", str(pool.pub("operator")), "--port", "0"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env=_env())
        line = self.process.stdout.readline()
        assert "listening on" in line, (line, self.process.stderr.read())
        self.addr = line.split("listening on ", 1)[1].split()[0]

    def stop(self) -> None:
        self.process.kill()
        self.process.wait(timeout=30)
        self.process.stdout.close()
        self.process.stderr.close()


def _row(status_text: str, peer: str) -> str:
    return next(line for line in status_text.splitlines()
                if line.strip().startswith(peer + " "))


# ---------------------------------------------------------------------------
# 1. the CLI, end to end
# ---------------------------------------------------------------------------


def test_the_verb_and_the_flag_exist_and_the_old_tree_had_neither():
    from revl.cli.parser import build_parser

    parser = build_parser()
    args = parser.parse_args(["pool", "probe", "--dir", "d", "--peer", "a",
                              "--peer-addr", "h:1", "--dispatch-identity", "k"])
    assert args.pool_command == "probe"
    assert args.peer == ["a"] and args.timeout == 10.0
    status = parser.parse_args(["pool", "status", "--dir", "d",
                                "--require-live", "60"])
    assert status.require_live == 60.0
    # Additive: a plain status is unchanged.
    assert parser.parse_args(["pool", "status", "--dir", "d"]).require_live \
        is None


def test_probe_status_and_require_live_track_a_real_peer_process(tmp_path):
    """The whole operator loop against a peer that is up, then down."""
    pool = Pool(tmp_path)
    pool.admit("alpha")
    before = pool.authority_bytes()

    fresh = pool.status()
    assert fresh.returncode == 0
    assert "health=unknown" in _row(fresh.stdout, "alpha")
    never = pool.status("--require-live", "3600")
    assert never.returncode == 1
    assert "not live within 3600s: alpha" in never.stderr

    serving = Serving(pool, "alpha")
    try:
        probed = pool.probe("--peer", "alpha", "--peer-addr", serving.addr)
        assert probed.returncode == 0, (probed.stdout, probed.stderr)
        assert probed.stdout.startswith("alpha  live")
        assert f"addr={serving.addr}" in probed.stdout

        up = pool.status("--require-live", "3600")
        assert up.returncode == 0, up.stderr
        assert "health=live@" in _row(up.stdout, "alpha")

        # The address was recorded by the verified contact, so a later probe
        # needs no --peer-addr.
        again = pool.probe()
        assert again.returncode == 0, (again.stdout, again.stderr)
    finally:
        serving.stop()

    gone = pool.probe()
    assert gone.returncode == 1
    assert gone.stdout.startswith("alpha  unreachable  link=peer-unreachable")
    down = pool.status("--require-live", "3600")
    assert down.returncode == 1
    assert "not live within 3600s: alpha" in down.stderr
    row = _row(down.stdout, "alpha")
    assert "health=unreachable" in row and "failures=1" in row
    assert "last-live=never" not in row   # it WAS live, and that is kept

    as_json = json.loads(pool.status("--json").stdout)
    member = as_json["health"]["members"]["alpha"]
    assert member["state"] == ph.HEALTH_UNREACHABLE
    assert member["last_live"]
    assert [e["state"] for e in as_json["health"]["events"]] == [
        ph.HEALTH_LIVE, ph.HEALTH_LIVE, ph.HEALTH_UNREACHABLE]

    # No authority moved: the charter, roster and key directory are the same
    # bytes after three probes as before them, and alpha is still a member.
    assert pool.authority_bytes() == before
    assert "alpha" in json.loads(
        (pool.dir / pp.ROSTER_FILE).read_text(encoding="utf-8"))["members"]


def test_a_probe_signed_by_a_key_the_peer_did_not_pin_is_not_liveness(
        tmp_path):
    """The peer refuses it, and an unsigned refusal proves nothing about who
    sent it, so the member is recorded `unverified`, never live."""
    pool = Pool(tmp_path)
    pool.admit("alpha")
    serving = Serving(pool, "alpha")
    try:
        refused = pool.probe("--peer", "alpha", "--peer-addr", serving.addr,
                             "--json", identity="intruder")
    finally:
        serving.stop()
    assert refused.returncode == 1
    outcome = json.loads(refused.stdout)["probes"][0]
    assert outcome["link"] == ph.LINK_PROBE_REFUSED
    assert outcome["peer_link"] == ph.LINK_PROBE_SIGNATURE
    assert outcome["health"]["state"] == ph.HEALTH_UNVERIFIED
    assert "health=unverified" in _row(pool.status().stdout, "alpha")


def test_the_cli_refusal_paths(tmp_path):
    pool = Pool(tmp_path)

    # An empty pool: nothing probed is not everything live.
    empty = pool.probe()
    assert empty.returncode == 1
    assert "no members to probe" in empty.stderr

    pool.admit("alpha")
    # Never contacted and no address given.
    blind = pool.probe("--peer", "alpha")
    assert blind.returncode == 1
    assert "link=no-address" in blind.stdout
    # Not a member.
    stranger = pool.probe("--peer", "nobody", "--peer-addr", "127.0.0.1:1")
    assert stranger.returncode == 1
    assert "link=not-a-member" in stranger.stdout
    # One address cannot belong to every member.
    usage = pool.probe("--peer-addr", "127.0.0.1:1")
    assert usage.returncode == 2
    assert "exactly one --peer" in usage.stderr
    # A malformed address is an operator error, not a peer finding.
    malformed = pool.probe("--peer", "alpha", "--peer-addr", "nowhere")
    assert malformed.returncode == 2
    # None of those learned anything about alpha, so nothing was recorded.
    assert not (pool.dir / ph.HEALTH_FILE).exists()


# ---------------------------------------------------------------------------
# in-process fixtures for the refusal corpus
# ---------------------------------------------------------------------------


def make_charter() -> dict:
    return pp.sign_charter(pp.PoolCharter(
        pool_id="lab-health", ceiling=CEILING,
        tiers={pp.ENTRY_TIER: pp.TierGrant(caps=CEILING)},
        admit_key_ids=(key_id(OP_KEY),), revoke_key_ids=(key_id(OP_KEY),),
        attest_key_ids=(ATTESTOR_ID.key_id,),
        artifact_digests=(PURE_DIGEST,),
        identity_mode=pp.MODE_ASYMMETRIC), OP_KEY)


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
    record = make_charter()
    directory = pi.IdentityDirectory()
    for identity in (WORKER_ID, OTHER_ID, ATTESTOR_ID):
        directory.register(identity.public())
    roster = pp.Roster(record["pool_id"], pp.canonical_digest(record))
    for n, (peer, identity) in enumerate(((WORKER, WORKER_ID),
                                          (OTHER, OTHER_ID))):
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
    return pool_dir, record, roster, directory


def runner(tmp_path, record, identity=WORKER_ID):
    return pd.PeerRunner(charter_record=record, identity=identity,
                         operator_public=OPERATOR_ID.public(),
                         workspace=tmp_path / "peer-work", timeout=60.0)


def serve_answers(answer_for, *, count=1):
    """A far side that answers each frame with `answer_for(frame)`. Used both
    for the honest runner and for a hostile one."""
    ready, bound = threading.Event(), {}

    def loop():
        with socket.socket() as server:
            server.bind(("127.0.0.1", 0))
            server.listen(4)
            bound["port"] = server.getsockname()[1]
            ready.set()
            for _ in range(count):
                conn, _ = server.accept()
                with conn:
                    frame = json.loads(pd._read_frame(conn))
                    conn.sendall(json.dumps(answer_for(frame)).encode()
                                 + b"\n")

    threading.Thread(target=loop, daemon=True).start()
    assert ready.wait(30)
    return f"127.0.0.1:{bound['port']}"


def dead_addr() -> str:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return f"127.0.0.1:{probe.getsockname()[1]}"


def probe(pool_dir, addr, peer=WORKER):
    return ph.probe_member(pool_dir=pool_dir, peer_id=peer, addr=addr,
                           dispatch_identity=OPERATOR_ID, timeout=5.0)


def signed_probe(record, *, peer=WORKER, identity=OPERATOR_ID, **overrides):
    body = ph.build_probe(pool_id=record["pool_id"],
                          charter_digest=pp.canonical_digest(record),
                          peer_id=peer)
    body.update(overrides)
    return ph.sign_probe(body, identity)


def forged_heartbeat(frame, identity, **overrides):
    body = {"kind": ph.HEARTBEAT_KIND, "version": ph.HEARTBEAT_VERSION,
            "pool_id": frame["pool_id"],
            "charter_digest": frame["charter_digest"],
            "peer_id": identity.peer_id, "nonce": frame["nonce"],
            "probe_digest": ph.probe_digest(frame),
            "answered_at": "2026-01-01T00:00:00+00:00"}
    body.update(overrides)
    return {"ok": True,
            "heartbeat": pi.sign_record(ph.HEARTBEAT_DOMAIN, body, identity)}


# ---------------------------------------------------------------------------
# 2. a heartbeat is a claim
# ---------------------------------------------------------------------------


def test_an_honest_member_is_live_and_its_key_is_recorded(tmp_path):
    pool_dir, record, _roster, _directory = pool_on_disk(tmp_path)
    addr = serve_answers(runner(tmp_path, record).handle)
    outcome = probe(pool_dir, addr)
    assert outcome["ok"], outcome
    assert outcome["key_id"] == WORKER_ID.key_id
    assert outcome["health"]["state"] == ph.HEALTH_LIVE
    assert outcome["health"]["addr"] == addr


def test_a_replayed_heartbeat_does_not_make_a_dead_member_look_alive(tmp_path):
    """The member answered once. Then it went away and something else at the
    address replays that genuine, correctly signed heartbeat. It is refused on
    `stale-heartbeat` because it answers a different probe."""
    pool_dir, record, _roster, _directory = pool_on_disk(tmp_path)
    captured = {}

    def honest_then_record(frame):
        answer = runner(tmp_path, record).handle(frame)
        captured["answer"] = answer
        return answer

    assert probe(pool_dir, serve_answers(honest_then_record))["ok"]
    replayed = probe(pool_dir, serve_answers(lambda _f: captured["answer"]))
    assert replayed["link"] == ph.LINK_STALE_HEARTBEAT, replayed
    assert replayed["health"]["state"] == ph.HEALTH_UNVERIFIED


@pytest.mark.parametrize("name, answer_for, link", [
    ("not-a-heartbeat", lambda f: {"ok": True, "heartbeat": "yes"},
     ph.LINK_HEARTBEAT_SHAPE),
    ("another-member-answers", lambda f: forged_heartbeat(f, OTHER_ID),
     ph.LINK_HEARTBEAT_IDENTITY),
    ("intruder-signs-as-the-member",
     lambda f: forged_heartbeat(f, INTRUDER_ID, peer_id=WORKER),
     ph.LINK_HEARTBEAT_SIGNATURE),
    ("member-key-other-charter",
     lambda f: forged_heartbeat(f, WORKER_ID, charter_digest="0" * 64),
     ph.LINK_CHARTER_IDENTITY),
    ("an-unsigned-refusal", lambda f: {"ok": False, "link": "task-shape",
                                       "reason": "no"},
     ph.LINK_PROBE_REFUSED),
])
def test_what_answers_at_the_address_is_not_taken_on_its_word(
        tmp_path, name, answer_for, link):
    pool_dir, _record, _roster, _directory = pool_on_disk(tmp_path)
    outcome = probe(pool_dir, serve_answers(answer_for))
    assert outcome["link"] == link, (name, outcome)
    assert outcome["health"]["state"] == ph.HEALTH_UNVERIFIED
    assert not ph.is_live(outcome["health"], within=3600)


def test_a_revoked_key_verifies_and_is_still_not_a_live_member(tmp_path):
    """A forgery and a revoked key are different findings, and both are
    refusals."""
    pool_dir, record, _roster, directory = pool_on_disk(tmp_path)
    directory.revoke(WORKER, WORKER_ID.key_id, reason="lost laptop")
    pp.save_directory(pool_dir, directory)
    outcome = probe(pool_dir, serve_answers(runner(tmp_path, record).handle))
    assert outcome["link"] == ph.LINK_HEARTBEAT_KEY, outcome


def test_the_peer_refuses_a_probe_on_the_terms_it_refuses_a_task(tmp_path):
    _pool_dir, record, _roster, _directory = pool_on_disk(tmp_path)
    peer = runner(tmp_path, record)
    corpus = {
        ph.LINK_PROBE_SHAPE: {"kind": ph.PROBE_KIND, "pool_id": 7},
        ph.LINK_PROBE_SIGNATURE: signed_probe(record, identity=INTRUDER_ID),
        ph.LINK_POOL_IDENTITY: signed_probe(record, pool_id="elsewhere"),
        ph.LINK_CHARTER_IDENTITY: signed_probe(record,
                                               charter_digest="0" * 64),
        ph.LINK_PEER_IDENTITY: signed_probe(record, peer=OTHER),
    }
    for link, frame in corpus.items():
        answer = peer.handle(frame)
        assert answer.get("link") == link, (link, answer)
        assert "heartbeat" not in answer
    # A probe runs nothing and does not spend a task id.
    assert peer.handle(signed_probe(record)).get("heartbeat")
    assert peer.seen == {}


@pytest.mark.parametrize("hostile", [None, [], "probe", 7,
                                     {"kind": ph.PROBE_KIND},
                                     {"kind": ph.PROBE_KIND, "nonce": ["x"]}])
def test_a_hostile_probe_gets_a_verdict_and_never_a_traceback(tmp_path,
                                                             hostile):
    _pool_dir, record, _roster, _directory = pool_on_disk(tmp_path)
    answer = runner(tmp_path, record).handle(hostile)
    assert answer["ok"] is False and answer["link"]


# ---------------------------------------------------------------------------
# the health record, and dispatch feeding it
# ---------------------------------------------------------------------------


def test_a_failing_run_keeps_its_start_and_the_last_live_instant():
    log = ph.HealthLog("p", "c" * 64)
    t0 = datetime(2026, 9, 1, tzinfo=timezone.utc)

    def at(minutes, state):
        return log.record(ph.Contact(
            peer_id=WORKER, state=state, source="probe",
            at=ph._iso(t0 + timedelta(minutes=minutes)), addr="h:1"))

    at(0, ph.HEALTH_LIVE)
    at(5, ph.HEALTH_UNREACHABLE)
    row = at(9, ph.HEALTH_UNVERIFIED)
    assert row["failures"] == 2
    assert row["since"] == ph._iso(t0 + timedelta(minutes=5))
    assert row["last_live"] == ph._iso(t0)
    assert "failures=2" in ph.health_note(row)
    assert not ph.is_live(row, within=10**6, now=t0 + timedelta(minutes=10))

    row = at(12, ph.HEALTH_LIVE)
    assert row["failures"] == 0 and row["since"] == ""
    assert ph.is_live(row, within=60, now=t0 + timedelta(minutes=12, seconds=30))
    assert not ph.is_live(row, within=60, now=t0 + timedelta(minutes=14))
    assert len(log.events) == 4


def test_a_health_record_from_another_charter_is_refused(tmp_path):
    pool_dir, _record, _roster, _directory = pool_on_disk(tmp_path)
    (pool_dir / ph.HEALTH_FILE).write_text(json.dumps(
        ph.HealthLog("lab-health", "f" * 64).as_dict()), encoding="utf-8")
    with pytest.raises(ph.HealthError):
        ph.load_health(pool_dir)


def test_a_dispatch_records_what_it_learned_about_the_member(tmp_path):
    """A delivered task is a contact verified under the member's key, and a
    task nobody answered is an unreachable one. Without this, `pool status`
    could say `live` right after `run --pool private` found nobody there."""
    pool_dir, record, _roster, _directory = pool_on_disk(tmp_path)
    addr = serve_answers(runner(tmp_path, record).handle)
    host, port = pd._split_addr(addr)
    done = pd.dispatch_one(
        pool_dir=pool_dir, peer_id=WORKER, host=host, port=port,
        source=PURE_SOURCE, runner=pd.RUNNER_TEST_PY,
        dispatch_identity=OPERATOR_ID, attesting_identity=ATTESTOR_ID,
        timeout=120.0)
    assert done["ok"], done
    row = ph.load_health(pool_dir).row(WORKER)
    assert row["state"] == ph.HEALTH_LIVE and row["source"] == "dispatch"
    assert row["key_id"] == WORKER_ID.key_id and row["addr"] == addr

    host, port = pd._split_addr(dead_addr())
    lost = pd.dispatch_one(
        pool_dir=pool_dir, peer_id=WORKER, host=host, port=port,
        source=PURE_SOURCE, runner=pd.RUNNER_TEST_PY,
        dispatch_identity=OPERATOR_ID, attesting_identity=ATTESTOR_ID,
        timeout=2.0)
    assert lost["link"] == pd.LINK_PEER_UNREACHABLE
    row = ph.load_health(pool_dir).row(WORKER)
    assert row["state"] == ph.HEALTH_UNREACHABLE
    # The address of the last VERIFIED contact is kept; a failed send does not
    # overwrite it with the address that failed.
    assert row["addr"] == addr
    assert ph.load_health(pool_dir).row(OTHER)["state"] == ph.HEALTH_UNKNOWN


# ---------------------------------------------------------------------------
# 3. no authority moves
# ---------------------------------------------------------------------------

#: The functions that change who holds what. A liveness module that called one
#: would be a second authority beside the charter's.
AUTHORITY_WRITERS = {"admit", "promote", "withdraw", "save_roster",
                     "save_directory", "save_ledger", "orphan", "register",
                     "rotate", "revoke"}


def test_the_liveness_module_calls_nothing_that_changes_authority():
    tree = ast.parse(Path(ph.__file__).read_text(encoding="utf-8"))
    called = {node.func.attr if isinstance(node.func, ast.Attribute)
              else getattr(node.func, "id", "")
              for node in ast.walk(tree) if isinstance(node, ast.Call)}
    assert not called & AUTHORITY_WRITERS, called & AUTHORITY_WRITERS


def test_an_unreachable_member_stays_a_member_until_the_revoke_authority_acts(
        tmp_path):
    pool_dir, record, _roster, directory = pool_on_disk(tmp_path)
    assert probe(pool_dir, dead_addr())["link"] == ph.LINK_PEER_UNREACHABLE
    _charter, roster = pp.load_pool(pool_dir)
    assert WORKER in roster.members
    # Removal is the existing gate, and it still refuses a key without revoke
    # authority, unreachable member or not.
    refused = pp.withdraw(record, WORKER, "stopped answering",
                          charter_key=OP_KEY, roster=roster,
                          directory=directory, revoking_key_id="not-a-revoker")
    assert refused["verdict"] == pp.REFUSE


# ---------------------------------------------------------------------------
# 4. the link set, both directions
# ---------------------------------------------------------------------------


def _declared_links() -> dict:
    tree = ast.parse(Path(ph.__file__).read_text(encoding="utf-8"))
    return {node.targets[0].id: node.value.value for node in tree.body
            if isinstance(node, ast.Assign)
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id.startswith("LINK_")
            and isinstance(node.value, ast.Constant)}


def test_every_refusal_names_a_declared_link_and_the_register_is_exact():
    declared = _declared_links()
    tree = ast.parse(Path(ph.__file__).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                and node.func.id == "_refusal":
            assert isinstance(node.args[0], ast.Name), ast.dump(node.args[0])
            assert node.args[0].id in declared, node.args[0].id
    assert set(ph.REFUSAL_LINKS) == set(declared.values())
    assert len(ph.REFUSAL_LINKS) == len(set(ph.REFUSAL_LINKS))
    from revl.diagnostics import GUARANTEES

    for link in ph.REFUSAL_LINKS:
        assert link == link.lower() and link not in GUARANTEES


def test_every_link_is_reached_at_runtime(tmp_path):
    pool_dir, record, _roster, directory = pool_on_disk(tmp_path)
    reached = set()
    peer = runner(tmp_path, record)
    for frame in ({"kind": ph.PROBE_KIND},
                  signed_probe(record, identity=INTRUDER_ID),
                  signed_probe(record, pool_id="elsewhere"),
                  signed_probe(record, charter_digest="0" * 64),
                  signed_probe(record, peer=OTHER)):
        reached.add(peer.handle(frame)["link"])
    reached.add(probe(pool_dir, "", peer="nobody")["link"])
    reached.add(probe(pool_dir, "")["link"])
    reached.add(probe(pool_dir, dead_addr())["link"])
    for answer_for in (
            lambda f: {"ok": False, "link": "x", "reason": "no"},
            lambda f: {"ok": True},
            lambda f: forged_heartbeat(f, OTHER_ID),
            lambda f: forged_heartbeat(f, INTRUDER_ID, peer_id=WORKER),
            lambda f: forged_heartbeat(f, WORKER_ID, nonce="old"),
            lambda f: forged_heartbeat(f, WORKER_ID, pool_id="elsewhere")):
        reached.add(probe(pool_dir, serve_answers(answer_for))["link"])
    directory.revoke(WORKER, WORKER_ID.key_id, reason="rotated out")
    pp.save_directory(pool_dir, directory)
    reached.add(probe(pool_dir, serve_answers(peer.handle))["link"])
    missing = set(ph.REFUSAL_LINKS) - reached
    assert not missing, f"declared but never reached at runtime: {missing}"
