"""Dispatching work to a pool member, and the one-result delivery ledger
(item 524, issue #1198; `src/revl/pool_dispatch.py`).

What is under test, in the order it matters.

1. **NON-VACUITY.** Before this change there was no dispatcher at all: nothing
   sent a task, so nothing refused one, and `Roster.outstanding` was populated
   by no caller in the tree. The corpus below is 15 tasks. Every one of them is
   signed by the operator key the peer pinned, so every one passes the only
   check that existed before this module (there was none), and every one is
   refused here on a named link. A control that differs from the corpus only in
   being well formed is run and its result delivered.

2. **ONE RESULT.** The delivered-twice attempt is the item's own wording:
   "impossible or visible". Both halves are tested. There is no edge in the
   state machine from `delivered` back to `dispatched`, and the refused second
   delivery is APPENDED to the event log rather than dropped.

3. **THE EXIT TEST, clause by clause.** Item 524's exit test is two machines
   joining a pool, one running a task the other admits, the result verified by
   hash and receipt, and a peer leaving mid-task leaving the ledger in a stated
   state. `tests/test_pool_receipts_524.py` holds the join and receipt clauses
   at the data-model level; this file holds the clauses that need a channel: a
   task crossing a real socket to a real subprocess, the result checked by hash
   AND receipt, and a withdrawal naming what it orphaned.

   **What these tests do NOT reach, stated rather than implied.** The two ends
   are two OS PROCESSES on one machine over loopback, not two machines. What a
   second machine would add is the network, and the network is roadmap item
   118's mTLS channel, which this module is a caller of and not a
   reimplementation of. Nothing here proves anything about a hostile network;
   it proves that every record crossing the channel is checked before it is
   believed, which is the property that does not change with the distance.

4. **THE LINK SET, both directions.** An AST walk asserts every `_refusal`
   call names a declared `LINK_` constant, and a runtime test asserts the
   corpus plus the named tests together produce EVERY link in
   `REFUSAL_LINKS`. Reachable in the source is weaker than reached in a run,
   so both are asserted.
"""

import ast
import json
import socket
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from revl import peer_identity as pi  # noqa: E402
from revl import peer_offer, peer_pool as pp, pool_receipt as pr  # noqa: E402
from revl import pool_dispatch as pd  # noqa: E402
from revl.attest import key_id  # noqa: E402
from revl.lawful_retry import EffectClass  # noqa: E402
from revl.peer_offer import Attestation, PeerOffer  # noqa: E402

OP_KEY = b"pool-operator-key-524-dispatch"

WORKER = "peer-worker"
OTHER = "peer-other"

#: Deterministic identities. `identity_from_seed`'s docstring says why this is
#: fixtures-only: the scalar is a hash of the seed, so a seed committed to a
#: repository is a published private key. These four are exactly that and are
#: used nowhere but here.
WORKER_ID = pi.identity_from_seed(WORKER, b"seed/worker/524-dispatch")
OTHER_ID = pi.identity_from_seed(OTHER, b"seed/other/524-dispatch")
OPERATOR_ID = pi.identity_from_seed("operator", b"seed/operator/524-dispatch")
ATTESTOR_ID = pi.identity_from_seed("attestor", b"seed/attestor/524-dispatch")
INTRUDER_ID = pi.identity_from_seed("intruder", b"seed/intruder/524-dispatch")

POOL_CEILING = ("compute()",)
ENTRY_CAPS = ("compute()",)

#: A boundary-free composition: no externs, no emissions, no capabilities. It
#: is what `classify_artifact` may call `pure`, and it is the only shape this
#: dispatcher sends anywhere.
PURE_SOURCE = b"""pub fn add(a: Int, b: Int) -> Int {
  return a + b
}

test "add is addition" {
  assert add(2, 3) == 5
}
"""

#: The same computation with ONE host extern. Nothing else differs, so a
#: refusal of this one is a refusal of the boundary and not of the program.
CROSSING_SOURCE = b"""extern pure fn shout(line: Str) -> Int = @py {
  import sys
  sys.stdout.write(line)
  return 1
}

pub fn add(a: Int, b: Int) -> Int {
  return a + b
}

test "add is addition" {
  assert add(2, 3) == 5
}
"""

PURE_DIGEST = pd.artifact_digest(PURE_SOURCE)
CROSSING_DIGEST = pd.artifact_digest(CROSSING_SOURCE)


# ---------------------------------------------------------------------------
# builders
# ---------------------------------------------------------------------------


def make_charter(**overrides) -> dict:
    spec = dict(
        pool_id="lab-dispatch",
        ceiling=POOL_CEILING,
        tiers={pp.ENTRY_TIER: pp.TierGrant(caps=ENTRY_CAPS)},
        admit_key_ids=(key_id(OP_KEY),),
        revoke_key_ids=(key_id(OP_KEY),),
        attest_key_ids=(ATTESTOR_ID.key_id,),
        artifact_digests=(PURE_DIGEST, CROSSING_DIGEST),
        identity_mode=pp.MODE_ASYMMETRIC)
    spec.update(overrides)
    return pp.sign_charter(pp.PoolCharter(**spec), OP_KEY)


def _join(charter_record, peer_id, identity, *, nonce, artifact):
    offer = PeerOffer(peer_id=peer_id,
                      attestation=Attestation(trust="verified"),
                      grant_ceiling=POOL_CEILING)
    body = pp.JoinRequest(
        pool_id=charter_record["pool_id"],
        charter_digest=pp.canonical_digest(charter_record),
        peer_id=peer_id,
        offer=peer_offer.sign_offer_identity(offer, identity),
        artifact_digest=artifact,
        nonce=nonce,
        issued_at=pp._iso(pp._utc_now()))
    return pp.sign_join_identity(body, identity)


def pool_on_disk(tmp_path, *, artifact=PURE_DIGEST, charter=None):
    """A pool directory two peers have joined, written the way the CLI writes
    it, so a test reads what an operator would read."""
    record = charter or make_charter()
    directory = pi.IdentityDirectory()
    for identity in (WORKER_ID, OTHER_ID, ATTESTOR_ID):
        directory.register(identity.public())
    roster = pp.Roster(record["pool_id"], pp.canonical_digest(record))
    for n, (peer, identity) in enumerate(((WORKER, WORKER_ID),
                                          (OTHER, OTHER_ID))):
        verdict = pp.admit(
            record, _join(record, peer, identity, nonce=f"n-{n}",
                          artifact=artifact),
            charter_key=OP_KEY, peer_keys={}, directory=directory,
            admitting_key_id=key_id(OP_KEY), roster=roster)
        assert verdict["verdict"] == pp.ADMIT, verdict
    pool_dir = tmp_path / "pool"
    pool_dir.mkdir(parents=True, exist_ok=True)
    (pool_dir / pp.CHARTER_FILE).write_text(json.dumps(record), encoding="utf-8")
    pp.save_roster(pool_dir, roster)
    pp.save_directory(pool_dir, directory)
    return pool_dir, record, roster, directory


def peer_runner(tmp_path, charter_record, *, identity=WORKER_ID,
                operator=None):
    return pd.PeerRunner(charter_record=charter_record, identity=identity,
                         operator_public=(operator or OPERATOR_ID).public(),
                         workspace=tmp_path / "peer-work", timeout=120.0)


def honest_task(charter_record, *, peer_id=WORKER, task_id="t-1",
                source=PURE_SOURCE, runner=pd.RUNNER_TEST_PY,
                identity=None):
    body = pd.build_task(
        pool_id=charter_record["pool_id"],
        charter_digest=pp.canonical_digest(charter_record),
        peer_id=peer_id, task_id=task_id, artifact=source, runner=runner,
        effect_class=EffectClass.PURE)
    return pd.sign_task(body, identity or OPERATOR_ID)


def serve_in_background(runner, *, count=1):
    """Serve `count` tasks on a loopback port and return (port, thread).

    A real socket, because a test that only calls `handle` proves the checks
    and not the channel, and the channel is half of what "operable" means."""
    ready = threading.Event()
    bound = {}

    def announce(host, port):
        bound["host"], bound["port"] = host, port
        ready.set()

    def loop():
        served = 0
        while served < count:
            served += pd.serve(runner, host="127.0.0.1",
                               port=bound.get("port", 0), once=True,
                               on_listen=announce)

    thread = threading.Thread(target=loop, daemon=True)
    thread.start()
    assert ready.wait(30), "the peer never bound a port"
    return bound["port"], thread


def dead_port() -> int:
    """A port nothing is listening on: bind it, learn the number, close it."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


# ---------------------------------------------------------------------------
# 1. the exit test, the clauses that need a channel
# ---------------------------------------------------------------------------


def test_a_task_crosses_a_socket_runs_on_the_peer_and_comes_back_verified(
        tmp_path):
    """Exit-test clause 2 and 3: one peer runs a task the other admitted, and
    the result is verified by hash AND by receipt.

    Both verifications are asserted separately below, because they fail for
    different reasons and a single `ok` would hide which one did the work."""
    pool_dir, record, _roster, _directory = pool_on_disk(tmp_path)
    port, _thread = serve_in_background(peer_runner(tmp_path, record))

    outcome = pd.dispatch_one(
        pool_dir=pool_dir, peer_id=WORKER, host="127.0.0.1", port=port,
        source=PURE_SOURCE, runner=pd.RUNNER_TEST_PY,
        dispatch_identity=OPERATOR_ID, attesting_identity=ATTESTOR_ID,
        timeout=120.0)

    assert outcome["ok"], outcome
    assert outcome["effect_class"] == "pure"
    assert outcome["tier"] == pp.ENTRY_TIER
    # It really ran: the peer's subprocess executed the artifact's own tests.
    assert outcome["result"]["exit_code"] == 0
    assert any("1 test(s) passed" in line
               for line in outcome["result"]["stdout"]), outcome["result"]
    # The hash half.
    ok, why = pr.verify_result(outcome["receipt"], outcome["result"])
    assert ok, why
    assert outcome["result_digest"] == pr.result_digest(outcome["result"])
    # The receipt half: signed by the peer, attested by a charter key.
    assert outcome["receipt"]["peer_id"] == WORKER
    assert outcome["receipt"]["key_id"] == WORKER_ID.key_id
    assert outcome["attestation"]["key_id"] == ATTESTOR_ID.key_id
    assert outcome["counts_as_evidence"] is True
    # And the ledger says it was delivered exactly once.
    ledger = pd.load_ledger(pool_dir)
    entry = ledger.tasks[outcome["task_id"]]
    assert entry["state"] == pd.STATE_DELIVERED
    assert ledger.outstanding() == {}


def test_a_peer_that_never_answers_leaves_the_task_outstanding(tmp_path):
    """Exit-test clause 4, first half. A task whose fate is unknown is neither
    delivered nor dropped: the dispatch is written BEFORE the send, so a peer
    that takes work and goes away leaves a record rather than nothing."""
    pool_dir, _record, _roster, _directory = pool_on_disk(tmp_path)

    outcome = pd.dispatch_one(
        pool_dir=pool_dir, peer_id=WORKER, host="127.0.0.1",
        port=dead_port(), source=PURE_SOURCE, runner=pd.RUNNER_TEST_PY,
        dispatch_identity=OPERATOR_ID, attesting_identity=ATTESTOR_ID,
        timeout=2.0)

    assert outcome["link"] == pd.LINK_PEER_UNREACHABLE, outcome
    ledger = pd.load_ledger(pool_dir)
    entry = ledger.tasks[outcome["task_id"]]
    assert entry["state"] == pd.STATE_DISPATCHED
    assert ledger.outstanding() == {WORKER: [outcome["task_id"]]}
    # The roster the withdrawal gate reads carries it too.
    _charter, roster = pp.load_pool(pool_dir)
    assert roster.outstanding == {WORKER: [outcome["task_id"]]}


def test_pool_status_shows_what_a_member_owes(tmp_path):
    """An operator deciding whether to withdraw a peer needs two facts
    together: what it holds, and what withdrawing it would orphan. The second
    was unavailable until the ledger existed to populate it."""
    pool_dir, _record, _roster, _directory = pool_on_disk(tmp_path)
    before = pp.render_status(*pp.load_pool(pool_dir),
                              pp.load_directory(pool_dir))
    assert "outstanding=" not in before

    lost = pd.dispatch_one(
        pool_dir=pool_dir, peer_id=WORKER, host="127.0.0.1",
        port=dead_port(), source=PURE_SOURCE, runner=pd.RUNNER_TEST_PY,
        dispatch_identity=OPERATOR_ID, attesting_identity=ATTESTOR_ID,
        timeout=2.0)
    assert lost["link"] == pd.LINK_PEER_UNREACHABLE, lost

    after = pp.render_status(*pp.load_pool(pool_dir),
                             pp.load_directory(pool_dir))
    worker_row = next(line for line in after.splitlines()
                      if line.strip().startswith(WORKER))
    other_row = next(line for line in after.splitlines()
                     if line.strip().startswith(OTHER))
    assert "outstanding=1" in worker_row
    # And only on the member that owes something: a peer with nothing
    # outstanding says nothing, rather than saying zero.
    assert "outstanding" not in other_row


def test_a_peer_leaving_mid_task_orphans_exactly_the_outstanding_work(
        tmp_path):
    """Exit-test clause 4, second half: "a peer leaving mid-task leaves the
    ledger in a STATED state".

    Three disjoint sets, and this asserts all three are right at once: the
    delivered task stays delivered (retained -- withdrawal does not un-observe
    work that happened), the outstanding one is named in `orphaned`, and the
    peer's caps are in `revoked`. Before this module `orphaned` was empty on
    every real deployment, because nothing populated `Roster.outstanding`."""
    pool_dir, record, _roster, directory = pool_on_disk(tmp_path)
    port, _thread = serve_in_background(peer_runner(tmp_path, record))

    done = pd.dispatch_one(
        pool_dir=pool_dir, peer_id=WORKER, host="127.0.0.1", port=port,
        source=PURE_SOURCE, runner=pd.RUNNER_TEST_PY,
        dispatch_identity=OPERATOR_ID, attesting_identity=ATTESTOR_ID)
    assert done["ok"], done
    lost = pd.dispatch_one(
        pool_dir=pool_dir, peer_id=WORKER, host="127.0.0.1",
        port=dead_port(), source=PURE_SOURCE, runner=pd.RUNNER_TEST_PY,
        dispatch_identity=OPERATOR_ID, attesting_identity=ATTESTOR_ID,
        timeout=2.0)
    assert lost["link"] == pd.LINK_PEER_UNREACHABLE, lost

    charter_record, roster = pp.load_pool(pool_dir)
    ledger = pd.load_ledger(pool_dir)
    roster.outstanding = ledger.outstanding()
    receipt = pp.withdraw(charter_record, WORKER, "left mid-task",
                          charter_key=OP_KEY, roster=roster,
                          directory=directory,
                          revoking_key_id=key_id(OP_KEY))
    assert receipt["verdict"] == pp.WITHDRAW, receipt
    assert receipt["orphaned"] == [lost["task_id"]]
    assert receipt["revoked"] == ["compute()"]

    moved = ledger.orphan(WORKER, reason="left mid-task")
    assert moved == [lost["task_id"]]
    assert ledger.tasks[lost["task_id"]]["state"] == pd.STATE_ORPHANED
    # Retained: the delivered task is still delivered, with its result digest.
    assert ledger.tasks[done["task_id"]]["state"] == pd.STATE_DELIVERED
    assert ledger.tasks[done["task_id"]]["result_digest"] \
        == done["result_digest"]
    assert ledger.outstanding() == {}


# ---------------------------------------------------------------------------
# 2. one result, and the delivered-twice attempt
# ---------------------------------------------------------------------------


def _ledger_with_one_dispatch():
    ledger = pd.DeliveryLedger("lab-dispatch", "c" * 64)
    assert ledger.dispatch(task_id="t-1", peer_id=WORKER,
                           artifact_digest=PURE_DIGEST,
                           runner=pd.RUNNER_TEST_PY,
                           effect_class="pure")["ok"]
    return ledger


def test_a_second_delivery_is_refused_and_recorded_not_absorbed():
    """Item 524's wording is "delivered twice must be impossible or visible".
    Both halves: the second delivery is refused (impossible) AND the attempt is
    appended to the event log with both result digests (visible)."""
    ledger = _ledger_with_one_dispatch()
    first = ledger.deliver(task_id="t-1", peer_id=WORKER, result_digest="a" * 64,
                           receipt_digest="b" * 64, counts=True)
    assert first["ok"]
    second = ledger.deliver(task_id="t-1", peer_id=WORKER,
                            result_digest="f" * 64, receipt_digest="e" * 64,
                            counts=True)
    assert second["link"] == pd.LINK_DELIVERED_TWICE, second
    # The first result is untouched: a second delivery does not overwrite.
    assert ledger.tasks["t-1"]["result_digest"] == "a" * 64
    assert ledger.tasks["t-1"]["state"] == pd.STATE_DELIVERED
    # And the attempt is in the log, naming both digests.
    refusals = [e for e in ledger.events if e["event"] == "delivery-refused"]
    assert len(refusals) == 1
    assert refusals[0]["link"] == pd.LINK_DELIVERED_TWICE
    assert refusals[0]["result_digest"] == "f" * 64
    assert refusals[0]["first_result_digest"] == "a" * 64


def test_the_state_machine_has_no_edge_back_to_dispatched():
    """The "impossible" half, asserted over the states rather than over one
    sequence a test happened to try: once a task leaves `dispatched` it never
    returns, under any of the three transitions that can move it."""
    for move in ("deliver", "refuse", "orphan"):
        ledger = _ledger_with_one_dispatch()
        if move == "deliver":
            ledger.deliver(task_id="t-1", peer_id=WORKER,
                           result_digest="a" * 64, receipt_digest="b" * 64,
                           counts=True)
        elif move == "refuse":
            ledger.refuse(task_id="t-1", link=pd.LINK_RESULT_DIGEST,
                          reason="mismatch")
        else:
            ledger.orphan(WORKER, reason="left")
        assert ledger.tasks["t-1"]["state"] != pd.STATE_DISPATCHED
        for _attempt in range(3):
            ledger.deliver(task_id="t-1", peer_id=WORKER,
                           result_digest="c" * 64, receipt_digest="d" * 64,
                           counts=True)
        assert ledger.tasks["t-1"]["state"] != pd.STATE_DISPATCHED


def test_a_delivery_after_a_withdrawal_is_its_own_finding():
    """A late delivery and a double delivery are different facts about what the
    pool did, so they are different links. An operator reconciling a ledger has
    to be able to tell them apart."""
    ledger = _ledger_with_one_dispatch()
    ledger.orphan(WORKER, reason="left mid-task")
    late = ledger.deliver(task_id="t-1", peer_id=WORKER,
                          result_digest="a" * 64, receipt_digest="b" * 64,
                          counts=True)
    assert late["link"] == pd.LINK_LATE_DELIVERY, late
    assert ledger.tasks["t-1"]["state"] == pd.STATE_ORPHANED


def test_a_result_for_work_nobody_sent_is_not_a_delivery():
    ledger = pd.DeliveryLedger("lab-dispatch", "c" * 64)
    answer = ledger.deliver(task_id="never-sent", peer_id=WORKER,
                            result_digest="a" * 64, receipt_digest="b" * 64,
                            counts=True)
    assert answer["link"] == pd.LINK_UNKNOWN_TASK, answer


def test_a_task_id_is_dispatched_once():
    ledger = _ledger_with_one_dispatch()
    again = ledger.dispatch(task_id="t-1", peer_id=WORKER,
                            artifact_digest=PURE_DIGEST,
                            runner=pd.RUNNER_TEST_PY, effect_class="pure")
    assert again["link"] == pd.LINK_REPLAYED_TASK, again
    assert [e["event"] for e in ledger.events] == ["dispatched",
                                                   "dispatch-refused"]


def test_the_ledger_round_trips_and_is_bound_to_its_charter(tmp_path):
    """A ledger belongs to a charter digest. A pool re-chartered under new
    terms starts a new ledger rather than inheriting one whose tasks were
    admitted under the old ones."""
    pool_dir, _record, _roster, _directory = pool_on_disk(tmp_path)
    ledger = pd.load_ledger(pool_dir)
    ledger.dispatch(task_id="t-1", peer_id=WORKER,
                    artifact_digest=PURE_DIGEST, runner=pd.RUNNER_TEST_PY,
                    effect_class="pure")
    pd.save_ledger(pool_dir, ledger)
    assert pd.load_ledger(pool_dir).as_dict() == ledger.as_dict()

    rechartered = make_charter(pool_id="lab-dispatch", trust_floor="attested")
    (pool_dir / pp.CHARTER_FILE).write_text(json.dumps(rechartered),
                                            encoding="utf-8")
    with pytest.raises(pd.DispatchError, match="starts a new ledger"):
        pd.load_ledger(pool_dir)


# ---------------------------------------------------------------------------
# 3. the peer-side corpus: every task is signed, every one is refused
# ---------------------------------------------------------------------------


def _edited_after_signing(record, **changes):
    edited = dict(record)
    edited.update(changes)
    return edited


def corpus(charter_record):
    """14 tasks. Every one is a real signed record under a key the peer pinned
    (or, for the two signature cases, under a key that is deliberately not the
    pinned one), so none of them is refused for being obviously junk."""
    other_charter = make_charter(pool_id="lab-dispatch",
                                 trust_floor="attested")
    return [
        ("not-an-object", "cheese", pd.LINK_TASK_SHAPE),
        ("wrong-kind",
         _edited_after_signing(honest_task(charter_record),
                               kind="revl.pool-join"),
         pd.LINK_TASK_SHAPE),
        ("missing-member",
         {k: v for k, v in honest_task(charter_record).items()
          if k != "runner"},
         pd.LINK_TASK_SHAPE),
        ("signed-by-an-intruder",
         honest_task(charter_record, identity=INTRUDER_ID),
         pd.LINK_TASK_SIGNATURE),
        ("edited-after-signing",
         _edited_after_signing(honest_task(charter_record), task_id="t-2"),
         pd.LINK_TASK_SIGNATURE),
        ("another-pool",
         pd.sign_task(
             dict(pd.build_task(
                 pool_id="some-other-pool",
                 charter_digest=pp.canonical_digest(charter_record),
                 peer_id=WORKER, task_id="t-3", artifact=PURE_SOURCE,
                 runner=pd.RUNNER_TEST_PY, effect_class=EffectClass.PURE)),
             OPERATOR_ID),
         pd.LINK_POOL_IDENTITY),
        ("another-charter",
         honest_task(other_charter, task_id="t-4"),
         pd.LINK_CHARTER_IDENTITY),
        ("addressed-to-another-peer",
         honest_task(charter_record, peer_id=OTHER, task_id="t-5"),
         pd.LINK_PEER_IDENTITY),
        ("bytes-do-not-match-the-declared-digest",
         pd.sign_task(
             dict(pd.build_task(
                 pool_id=charter_record["pool_id"],
                 charter_digest=pp.canonical_digest(charter_record),
                 peer_id=WORKER, task_id="t-6", artifact=PURE_SOURCE,
                 runner=pd.RUNNER_TEST_PY, effect_class=EffectClass.PURE),
                 artifact_digest="9" * 64),
             OPERATOR_ID),
         pd.LINK_ARTIFACT_DIGEST),
        ("an-artifact-the-charter-never-admitted",
         honest_task(charter_record, task_id="t-7",
                     source=PURE_SOURCE + b"\n# not the admitted bytes\n"),
         pd.LINK_ARTIFACT_DIGEST),
        ("a-runner-this-peer-does-not-implement",
         pd.sign_task(
             dict(pd.build_task(
                 pool_id=charter_record["pool_id"],
                 charter_digest=pp.canonical_digest(charter_record),
                 peer_id=WORKER, task_id="t-8", artifact=PURE_SOURCE,
                 runner=pd.RUNNER_TEST_PY, effect_class=EffectClass.PURE),
                 runner="shell"),
             OPERATOR_ID),
         pd.LINK_UNKNOWN_RUNNER),
        ("an-empty-runner",
         pd.sign_task(
             dict(pd.build_task(
                 pool_id=charter_record["pool_id"],
                 charter_digest=pp.canonical_digest(charter_record),
                 peer_id=WORKER, task_id="t-9", artifact=PURE_SOURCE,
                 runner=pd.RUNNER_TEST_PY, effect_class=EffectClass.PURE),
                 runner=""),
             OPERATOR_ID),
         pd.LINK_TASK_SHAPE),
        ("a-charter-digest-that-is-not-a-digest",
         pd.sign_task(
             dict(pd.build_task(
                 pool_id=charter_record["pool_id"],
                 charter_digest="not-a-digest",
                 peer_id=WORKER, task_id="t-10", artifact=PURE_SOURCE,
                 runner=pd.RUNNER_TEST_PY, effect_class=EffectClass.PURE)),
             OPERATOR_ID),
         pd.LINK_CHARTER_IDENTITY),
        ("signed-by-the-peer-itself",
         honest_task(charter_record, task_id="t-11", identity=WORKER_ID),
         pd.LINK_TASK_SIGNATURE),
        ("a-task-id-that-is-a-path-traversal",
         honest_task(charter_record, task_id="../../escape"),
         pd.LINK_TASK_SHAPE),
    ]


def test_every_corpus_task_is_refused_on_its_own_link(tmp_path):
    """Non-vacuity, both directions: 15 signed tasks, 15 refusals, and an
    honest control that differs only in being well formed is RUN."""
    _pool_dir, record, _roster, _directory = pool_on_disk(tmp_path)
    runner = peer_runner(tmp_path, record)
    seen = {}
    for name, task, expected in corpus(record):
        answer = runner.handle(task)
        assert not answer["ok"], f"{name} was not refused: {answer}"
        assert answer["link"] == expected, f"{name}: {answer}"
        seen[name] = answer["link"]
    assert len(seen) == 15
    assert len(set(seen.values())) == 7

    control = runner.handle(honest_task(record, task_id="control"))
    assert control["ok"], control
    assert control["result"]["exit_code"] == 0


def test_a_captured_task_cannot_be_replayed_for_a_second_receipt(tmp_path):
    """The peer side of one-result. A task is run once here too, so a task
    captured off the wire and resubmitted does not produce a second signed
    receipt for the same work."""
    _pool_dir, record, _roster, _directory = pool_on_disk(tmp_path)
    runner = peer_runner(tmp_path, record)
    task = honest_task(record, task_id="t-replay")
    first = runner.handle(task)
    assert first["ok"], first
    again = runner.handle(task)
    assert again["link"] == pd.LINK_REPLAYED_TASK, again


@pytest.mark.parametrize("hostile", [
    None, 7, [], "", {}, {"kind": pd.TASK_KIND},
    {"kind": pd.TASK_KIND, "pool_id": None},
    {"kind": pd.TASK_KIND, "artifact": 5},
    {"kind": 7}, [{"kind": pd.TASK_KIND}],
])
def test_a_hostile_record_gets_a_verdict_and_never_a_traceback(tmp_path,
                                                               hostile):
    """The wire is hostile, so a peer-controlled record must not be able to
    turn a refusal into an exception. `peer_pool`'s gate and
    `peer_offer.verify_offer` take the same position for the same reason."""
    _pool_dir, record, _roster, _directory = pool_on_disk(tmp_path)
    answer = peer_runner(tmp_path, record).handle(hostile)
    assert answer["ok"] is False
    assert answer["link"] in pd.REFUSAL_LINKS


def test_the_peer_re_hashes_what_it_runs_and_never_trusts_the_declared_digest(
        tmp_path):
    """Item 118's binding rule, on this path: "a receiver re-hashes the IR +
    artifact bytes it will execute, never trusts the attestation's
    self-declared hash". The task here is perfectly signed and its declared
    digest is one the charter admits; only the BYTES differ."""
    _pool_dir, record, _roster, _directory = pool_on_disk(tmp_path)
    body = dict(pd.build_task(
        pool_id=record["pool_id"],
        charter_digest=pp.canonical_digest(record), peer_id=WORKER,
        task_id="t-swap", artifact=CROSSING_SOURCE,
        runner=pd.RUNNER_TEST_PY, effect_class=EffectClass.PURE))
    body["artifact_digest"] = PURE_DIGEST     # an admitted digest, wrong bytes
    answer = peer_runner(tmp_path, record).handle(
        pd.sign_task(body, OPERATOR_ID))
    assert answer["link"] == pd.LINK_ARTIFACT_DIGEST, answer
    assert "hashes to" in answer["reason"]


def test_the_peer_workspace_path_is_not_in_the_signed_result(tmp_path):
    """A receipt is a signed record that leaves the peer's machine. A local
    absolute path in it is both a reproducibility problem -- the digest would
    differ per run for the same work -- and a needless disclosure."""
    _pool_dir, record, _roster, _directory = pool_on_disk(tmp_path)
    runner = peer_runner(tmp_path, record)
    answer = runner.handle(honest_task(record, task_id="t-scrub"))
    assert answer["ok"], answer
    blob = json.dumps(answer["result"])
    assert str(tmp_path) not in blob
    assert str(runner.workspace) not in blob


# ---------------------------------------------------------------------------
# 4. what may be sent at all: the effect class is derived, not declared
# ---------------------------------------------------------------------------


def _compile(source: bytes) -> dict:
    from revl.compiler import compile_files

    return compile_files(["artifact.rvl"],
                         sources={"artifact.rvl": source.decode("utf-8")})


def test_a_boundary_free_composition_is_pure_and_one_that_crosses_is_not():
    """The classifier, in both directions. The two sources differ by ONE
    extern, so the refusal is a refusal of the boundary and not of the
    program."""
    effect_class, reason, surface = classify = classify_of(PURE_SOURCE)
    assert effect_class is EffectClass.PURE, reason
    assert surface.empty
    assert classify  # the tuple shape, so a silent reshape breaks here

    effect_class, reason, surface = classify_of(CROSSING_SOURCE)
    assert effect_class is None
    assert not surface.empty
    assert surface.externs == 1
    assert "unproven here, not assumed safe" in reason


def classify_of(source: bytes):
    return pd.classify_artifact(_compile(source))


def test_a_composition_that_crosses_is_refused_before_anything_is_dispatched(
        tmp_path):
    """And the refusal happens BEFORE the ledger is written, so a task that was
    never sent leaves no entry to reconcile."""
    pool_dir, _record, _roster, _directory = pool_on_disk(
        tmp_path, artifact=CROSSING_DIGEST)
    outcome = pd.dispatch_one(
        pool_dir=pool_dir, peer_id=WORKER, host="127.0.0.1",
        port=dead_port(), source=CROSSING_SOURCE, runner=pd.RUNNER_TEST_PY,
        dispatch_identity=OPERATOR_ID, attesting_identity=ATTESTOR_ID,
        timeout=2.0)
    assert outcome["link"] == pd.LINK_EFFECT_CLASS_UNPROVEN, outcome
    assert outcome["surface"]["externs"] == 1
    assert pd.load_ledger(pool_dir).tasks == {}


def test_a_member_at_a_tier_nothing_is_known_about_is_sent_nothing(tmp_path):
    """Fail-closed on the tier as well as on the class. A roster whose member
    row names a tier this build has never heard of admits NOTHING, rather than
    falling back to the entry tier's ceiling."""
    pool_dir, _record, _roster, _directory = pool_on_disk(tmp_path)
    roster_path = pool_dir / pp.ROSTER_FILE
    payload = json.loads(roster_path.read_text(encoding="utf-8"))
    payload["members"][WORKER]["tier"] = "platinum"
    roster_path.write_text(json.dumps(payload), encoding="utf-8")

    outcome = pd.dispatch_one(
        pool_dir=pool_dir, peer_id=WORKER, host="127.0.0.1",
        port=dead_port(), source=PURE_SOURCE, runner=pd.RUNNER_TEST_PY,
        dispatch_identity=OPERATOR_ID, attesting_identity=ATTESTOR_ID,
        timeout=2.0)
    assert outcome["link"] == pd.LINK_WORK_INADMISSIBLE, outcome
    assert "platinum" in outcome["reason"]


def test_the_tier_bound_is_lawful_retrys_taxonomy_and_not_a_second_one():
    """Item 524's own warning is against "a second, weaker policy engine". The
    bound this module applies is `peer_pool.work_admissible`, which keys on
    `lawful_retry.EffectClass`; this module defines no effect vocabulary of its
    own, and that is asserted over the source rather than trusted."""
    source = Path(pd.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    classes = {node.name for node in ast.walk(tree)
               if isinstance(node, ast.ClassDef)}
    # No enum, no taxonomy: the only classes here are the ledger, the runner,
    # the counted surface and the error.
    assert classes == {"DispatchError", "BoundarySurface", "DeliveryLedger",
                       "PeerRunner"}
    assert "work_admissible" in source
    for reachable in ("pure", "idempotent-external", "witnessed"):
        assert f'"{reachable}"' not in source, (
            f"{reachable!r} is spelled here as a literal; the classes belong "
            f"to lawful_retry and must be referred to through it")


# ---------------------------------------------------------------------------
# 5. the operator side: what comes back is checked before it is believed
# ---------------------------------------------------------------------------


def test_a_result_that_does_not_hash_to_its_receipt_is_refused(tmp_path):
    """The hash half of "verified by hash and receipt", on the operator side.
    The receipt here is genuine and correctly signed by the peer; only the
    result bytes beside it were swapped, which is exactly the tamper a
    signature over the receipt cannot see."""
    pool_dir, record, roster, directory = pool_on_disk(tmp_path)
    receipt = pr.issue_receipt(pool_id=record["pool_id"], task_id="t-1",
                               artifact_digest=PURE_DIGEST,
                               result={"exit_code": 0}, identity=WORKER_ID)
    verdict = pd.verify_delivery(
        {"ok": True, "receipt": receipt, "result": {"exit_code": 1}},
        charter_record=record, member=roster.members[WORKER],
        directory=directory, attesting_identity=ATTESTOR_ID)
    assert verdict["link"] == pd.LINK_RESULT_DIGEST, verdict


def test_an_attestor_the_charter_does_not_name_makes_the_receipt_count_for_nothing(
        tmp_path):
    """The receipt half. The peer's signature is real and the result hashes
    correctly; the ATTESTOR is a key the charter never authorised, so the pair
    is refused and `pool_receipt`'s own link is carried through rather than
    flattened into a generic failure."""
    pool_dir, record, roster, directory = pool_on_disk(tmp_path)
    result = {"exit_code": 0}
    receipt = pr.issue_receipt(pool_id=record["pool_id"], task_id="t-1",
                               artifact_digest=PURE_DIGEST, result=result,
                               identity=WORKER_ID)
    verdict = pd.verify_delivery(
        {"ok": True, "receipt": receipt, "result": result},
        charter_record=record, member=roster.members[WORKER],
        directory=directory, attesting_identity=OTHER_ID)
    assert verdict["link"] == pd.LINK_RECEIPT_REFUSED, verdict
    assert verdict["receipt_link"] == pr.LINK_ATTESTATION_AUTHORITY
    assert verdict["check"]["counts"] is False


def test_a_refused_result_leaves_the_task_terminal_and_the_refusal_recorded(
        tmp_path):
    """A peer that answers and is refused has settled the task. The entry is
    terminal with its link, and it is NOT outstanding: a withdrawal must not
    orphan work whose fate is known."""
    pool_dir, record, _roster, _directory = pool_on_disk(tmp_path)
    port, _thread = serve_in_background(peer_runner(tmp_path, record))
    outcome = pd.dispatch_one(
        pool_dir=pool_dir, peer_id=WORKER, host="127.0.0.1", port=port,
        source=PURE_SOURCE, runner=pd.RUNNER_TEST_PY,
        dispatch_identity=OPERATOR_ID,
        attesting_identity=OTHER_ID)     # not a charter attest key
    assert outcome["link"] == pd.LINK_RECEIPT_REFUSED, outcome
    ledger = pd.load_ledger(pool_dir)
    assert ledger.tasks[outcome["task_id"]]["state"] == pd.STATE_REFUSED
    assert ledger.outstanding() == {}


def test_a_task_for_a_peer_that_is_not_a_member_is_refused(tmp_path):
    pool_dir, _record, _roster, _directory = pool_on_disk(tmp_path)
    outcome = pd.dispatch_one(
        pool_dir=pool_dir, peer_id="nobody", host="127.0.0.1",
        port=dead_port(), source=PURE_SOURCE, runner=pd.RUNNER_TEST_PY,
        dispatch_identity=OPERATOR_ID, attesting_identity=ATTESTOR_ID,
        timeout=2.0)
    assert outcome["link"] == pd.LINK_NOT_A_MEMBER, outcome
    assert pd.load_ledger(pool_dir).tasks == {}


def test_an_artifact_the_member_was_not_admitted_with_is_refused(tmp_path):
    """"The candidate a peer runs is the candidate that was admitted, pinned
    by hash" -- checked on the operator side too, so a task carrying other
    bytes is never signed in the first place."""
    pool_dir, _record, _roster, _directory = pool_on_disk(tmp_path)
    outcome = pd.dispatch_one(
        pool_dir=pool_dir, peer_id=WORKER, host="127.0.0.1",
        port=dead_port(), source=CROSSING_SOURCE, runner=pd.RUNNER_TEST_PY,
        dispatch_identity=OPERATOR_ID, attesting_identity=ATTESTOR_ID,
        timeout=2.0)
    assert outcome["link"] == pd.LINK_ARTIFACT_DIGEST, outcome


def test_an_unimplemented_runner_is_refused_before_a_task_is_signed(tmp_path):
    pool_dir, _record, _roster, _directory = pool_on_disk(tmp_path)
    outcome = pd.dispatch_one(
        pool_dir=pool_dir, peer_id=WORKER, host="127.0.0.1",
        port=dead_port(), source=PURE_SOURCE, runner="shell",
        dispatch_identity=OPERATOR_ID, attesting_identity=ATTESTOR_ID,
        timeout=2.0)
    assert outcome["link"] == pd.LINK_UNKNOWN_RUNNER, outcome


# ---------------------------------------------------------------------------
# 6. the channel
# ---------------------------------------------------------------------------


def test_a_non_loopback_bind_is_refused_unless_it_was_asked_for(tmp_path):
    """The channel has no transport security. Every record on it is signed, so
    nothing can be forged undetected; nothing on it is secret, and the artifact
    source crosses in the clear. That is a decision an operator makes, not a
    default."""
    _pool_dir, record, _roster, _directory = pool_on_disk(tmp_path)
    with pytest.raises(pd.DispatchError, match=pd.LINK_NON_LOOPBACK_BIND):
        pd.serve(peer_runner(tmp_path, record), host="0.0.0.0", port=0)


def test_an_unreadable_frame_is_answered_rather_than_dropped(tmp_path):
    """A peer that closes the connection on junk teaches a prober nothing and
    also teaches an honest operator nothing. It answers with a refusal."""
    _pool_dir, record, _roster, _directory = pool_on_disk(tmp_path)
    port, _thread = serve_in_background(peer_runner(tmp_path, record))
    with socket.create_connection(("127.0.0.1", port), timeout=30) as conn:
        conn.sendall(b"{not json at all\n")
        conn.shutdown(socket.SHUT_WR)
        answer = json.loads(conn.recv(65536).split(b"\n", 1)[0])
    assert answer["link"] == pd.LINK_TASK_SHAPE, answer


# ---------------------------------------------------------------------------
# 7. the link set, in both directions
# ---------------------------------------------------------------------------


def _declared_links() -> dict:
    tree = ast.parse(Path(pd.__file__).read_text(encoding="utf-8"))
    return {node.targets[0].id: node.value.value
            for node in tree.body
            if isinstance(node, ast.Assign)
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id.startswith("LINK_")
            and isinstance(node.value, ast.Constant)}


def test_every_refusal_names_a_declared_link():
    """An AST walk, not a grep: every `_refusal(...)` call's first argument is
    a `LINK_` NAME, never a string literal, so a refusal cannot invent a link
    that is not in the register."""
    declared = _declared_links()
    tree = ast.parse(Path(pd.__file__).read_text(encoding="utf-8"))
    calls = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                and node.func.id == "_refusal" and node.args:
            first = node.args[0]
            assert isinstance(first, ast.Name), ast.dump(first)
            assert first.id in declared, first.id
            calls += 1
    assert calls >= 15


def test_the_link_register_is_exactly_the_declared_links():
    declared = _declared_links()
    assert set(pd.REFUSAL_LINKS) == set(declared.values())
    assert len(pd.REFUSAL_LINKS) == len(set(pd.REFUSAL_LINKS))
    # Lowercase links, and none of them is a guarantee code: a dispatch
    # refusal is a decision about a deployment, not a verdict about a program.
    from revl.diagnostics import GUARANTEES

    for link in pd.REFUSAL_LINKS:
        assert link == link.lower()
        assert link not in GUARANTEES
    assert "GUARANTEES" not in Path(pd.__file__).read_text(encoding="utf-8")


def test_every_link_is_reached_at_runtime_by_this_file(tmp_path):
    """Reachable in the source is weaker than reached in a run. This drives
    every link in the register through real code and fails if one of them is a
    gate that exists only as a constant."""
    reached = set()
    _pool_dir, record, _roster, _directory = pool_on_disk(tmp_path)
    runner = peer_runner(tmp_path, record)
    for _name, task, _expected in corpus(record):
        reached.add(runner.handle(task)["link"])
    runner.handle(honest_task(record, task_id="t-once"))
    reached.add(runner.handle(honest_task(record,
                                          task_id="t-once"))["link"])

    ledger = _ledger_with_one_dispatch()
    reached.add(ledger.dispatch(task_id="t-1", peer_id=WORKER,
                                artifact_digest=PURE_DIGEST,
                                runner=pd.RUNNER_TEST_PY,
                                effect_class="pure")["link"])
    reached.add(ledger.deliver(task_id="absent", peer_id=WORKER,
                               result_digest="a" * 64,
                               receipt_digest="b" * 64,
                               counts=False)["link"])
    ledger.deliver(task_id="t-1", peer_id=WORKER, result_digest="a" * 64,
                   receipt_digest="b" * 64, counts=False)
    reached.add(ledger.deliver(task_id="t-1", peer_id=WORKER,
                               result_digest="c" * 64,
                               receipt_digest="d" * 64,
                               counts=False)["link"])
    late = _ledger_with_one_dispatch()
    late.orphan(WORKER, reason="left")
    reached.add(late.deliver(task_id="t-1", peer_id=WORKER,
                             result_digest="a" * 64, receipt_digest="b" * 64,
                             counts=False)["link"])

    pool_dir = _pool_dir
    for kwargs, in (({"peer_id": "nobody"},), ({"source": CROSSING_SOURCE},),
                    ({"runner": "shell"},), ({},)):
        call = {"pool_dir": pool_dir, "peer_id": WORKER, "host": "127.0.0.1",
                "port": dead_port(), "source": PURE_SOURCE,
                "runner": pd.RUNNER_TEST_PY,
                "dispatch_identity": OPERATOR_ID,
                "attesting_identity": ATTESTOR_ID, "timeout": 2.0}
        call.update(kwargs)
        reached.add(pd.dispatch_one(**call).get("link", ""))

    crossing_dir, _c, _r, _d = pool_on_disk(tmp_path / "crossing",
                                            artifact=CROSSING_DIGEST)
    reached.add(pd.dispatch_one(
        pool_dir=crossing_dir, peer_id=WORKER, host="127.0.0.1",
        port=dead_port(), source=CROSSING_SOURCE, runner=pd.RUNNER_TEST_PY,
        dispatch_identity=OPERATOR_ID, attesting_identity=ATTESTOR_ID,
        timeout=2.0)["link"])

    bad_tier, _c, _r, _d = pool_on_disk(tmp_path / "tier")
    payload = json.loads((bad_tier / pp.ROSTER_FILE).read_text(encoding="utf-8"))
    payload["members"][WORKER]["tier"] = "platinum"
    (bad_tier / pp.ROSTER_FILE).write_text(json.dumps(payload),
                                           encoding="utf-8")
    reached.add(pd.dispatch_one(
        pool_dir=bad_tier, peer_id=WORKER, host="127.0.0.1",
        port=dead_port(), source=PURE_SOURCE, runner=pd.RUNNER_TEST_PY,
        dispatch_identity=OPERATOR_ID, attesting_identity=ATTESTOR_ID,
        timeout=2.0)["link"])

    receipt = pr.issue_receipt(pool_id=record["pool_id"], task_id="t-1",
                               artifact_digest=PURE_DIGEST,
                               result={"exit_code": 0}, identity=WORKER_ID)
    reached.add(pd.verify_delivery(
        {"ok": True, "receipt": receipt, "result": {"exit_code": 1}},
        charter_record=record, member=_roster.members[WORKER],
        directory=_directory, attesting_identity=ATTESTOR_ID)["link"])
    reached.add(pd.verify_delivery(
        {"ok": True, "receipt": receipt, "result": {"exit_code": 0}},
        charter_record=record, member=_roster.members[WORKER],
        directory=_directory, attesting_identity=OTHER_ID)["link"])

    try:
        pd.serve(runner, host="0.0.0.0", port=0)
    except pd.DispatchError as error:
        if pd.LINK_NON_LOOPBACK_BIND in str(error):
            reached.add(pd.LINK_NON_LOOPBACK_BIND)

    reached.add(_run_pool_cli(tmp_path, files=["a.rvl", "b.rvl"])[1])
    reached.add(_run_pool_cli(tmp_path, files=["a.rvl"],
                              extra=["--once"])[1])

    reached.discard("")
    missing = set(pd.REFUSAL_LINKS) - reached
    assert not missing, f"declared but never reached at runtime: {missing}"


# ---------------------------------------------------------------------------
# 8. the CLI surface
# ---------------------------------------------------------------------------


def _run_pool_cli(tmp_path, *, files, extra=()):
    """Drive `revl run --pool private` through its own argument parser, so the
    flags under test are the flags the CLI actually defines."""
    import io
    from contextlib import redirect_stdout

    from revl.cli.parser import build_parser

    keys = tmp_path / "cli-keys"
    keys.mkdir(parents=True, exist_ok=True)
    pi.write_private_identity(keys / "op.key", OPERATOR_ID)
    pi.write_private_identity(keys / "attest.key", ATTESTOR_ID)
    argv = ["run", "--pool", "private", "--pool-dir", str(tmp_path / "pool"),
            "--peer", WORKER, "--peer-addr", "127.0.0.1:1",
            "--dispatch-identity", str(keys / "op.key"),
            "--attest-identity", str(keys / "attest.key"),
            "--pool-runner", "test-py"] + list(extra) + files
    args = build_parser().parse_args(argv)
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        code = pd.run_pool_command(args)
    printed = buffer.getvalue()
    link = json.loads(printed)["link"] if printed.strip().startswith("{") else ""
    return code, link


def test_run_pool_private_refuses_a_multi_file_artifact(tmp_path):
    """A pool task pins ONE artifact by hash. Two files would need a bundle
    digest, which this slice does not build, so it is refused by name rather
    than silently hashing the first one."""
    pool_on_disk(tmp_path)
    code, link = _run_pool_cli(tmp_path, files=["a.rvl", "b.rvl"])
    assert code == 1
    assert link == pd.LINK_MULTI_FILE_ARTIFACT


@pytest.mark.parametrize("flag", [
    ["--backend", "go"], ["--config", "c.toml"], ["--env", "e.toml"],
    ["--policy", "p.toml"], ["--watch"], ["--record"],
    ["--estop-latch", "latch"], ["--wal", "w.jsonl"], ["--trace", "t.jsonl"],
    ["--withdraw", "Comp"], ["--plan"], ["--placement", "p.toml"], ["--once"],
])
def test_a_local_only_run_flag_is_refused_rather_than_ignored(tmp_path, flag):
    """A flag that is accepted and ignored is not implemented. Every `revl run`
    flag the LOCAL runner honours is refused by name under `--pool private`,
    rather than the work quietly going somewhere the flag does not reach."""
    pool_on_disk(tmp_path)
    code, link = _run_pool_cli(tmp_path, files=["a.rvl"], extra=flag)
    assert code == 1
    assert link == pd.LINK_UNSUPPORTED_WITH_POOL


def test_the_local_only_flag_list_is_every_flag_the_run_parser_adds():
    """The list is checked against the parser rather than hand-maintained: a
    flag added to `revl run` later and not classified here would otherwise be
    silently ignored by a pool dispatch, which is the failure this whole check
    exists to prevent."""
    from revl.cli.parser import build_parser

    args = build_parser().parse_args(["run", "x.rvl"])
    pool_side = {"pool", "pool_dir", "peer", "peer_addr", "dispatch_identity",
                 "attest_identity", "pool_runner", "pool_timeout"}
    structural = {"command", "files"}
    classified = {field for field, _flag, _absent in pd.LOCAL_ONLY_RUN_FLAGS}
    unclassified = set(vars(args)) - pool_side - structural - classified
    assert not unclassified, (
        f"`revl run` flags neither classified as local-only nor as part of the "
        f"pool surface: {sorted(unclassified)}")
    # And every classified flag's "absent" value is the parser's own default,
    # so a default change cannot turn the check into a no-op.
    for field, flag, absent in pd.LOCAL_ONLY_RUN_FLAGS:
        assert getattr(args, field) == absent, (field, flag)


def test_the_run_parser_defines_every_flag_pool_dispatch_reads():
    """A flag the parser accepts and the code ignores is not implemented, and a
    field the code reads and the parser does not define is a crash waiting for
    an operator. Both directions, over the parser itself."""
    from revl.cli.parser import build_parser

    args = build_parser().parse_args(
        ["run", "--pool", "private", "--pool-dir", "d", "--peer", "p",
         "--peer-addr", "h:1", "--dispatch-identity", "a",
         "--attest-identity", "b", "--pool-runner", "run-once-py",
         "--pool-timeout", "9", "x.rvl"])
    assert args.pool == pd.POOL_PRIVATE
    assert args.pool_runner in pd.RUNNERS
    assert args.pool_timeout == 9.0
    for field in ("pool", "pool_dir", "peer", "peer_addr",
                  "dispatch_identity", "attest_identity", "pool_runner",
                  "pool_timeout"):
        assert hasattr(args, field), field

    # `private` is the only pool kind. A public or swarm pool needs a different
    # threat model and is ABSENT rather than unimplemented.
    with pytest.raises(SystemExit):
        build_parser().parse_args(["run", "--pool", "public", "x.rvl"])


def test_a_plain_run_is_untouched_by_the_pool_flags():
    """Additive: `revl run x.rvl` parses exactly as before and `--pool`
    defaults to None, so nothing about a local run changes."""
    from revl.cli.parser import build_parser

    args = build_parser().parse_args(["run", "x.rvl"])
    assert args.pool is None
    assert args.files == ["x.rvl"]


def test_pool_serve_and_pool_ledger_are_reachable_verbs():
    from revl.cli.parser import build_parser

    parser = build_parser()
    served = parser.parse_args(["pool", "serve", "--charter", "c.json",
                                "--identity-key", "k", "--operator-public",
                                "p", "--once"])
    assert served.pool_command == "serve"
    assert served.host == "127.0.0.1"    # loopback by default
    assert served.allow_remote is False
    read = parser.parse_args(["pool", "ledger", "--dir", "d", "--json"])
    assert read.pool_command == "ledger"


def test_pool_init_can_pin_an_asymmetric_attesting_key():
    """Without this the charter's attest authority held only the operator's
    SHARED-key fingerprint, and an execution receipt is an asymmetric record,
    so no receipt a peer signed could ever count."""
    from revl.cli.parser import build_parser

    args = build_parser().parse_args(
        ["pool", "init", "--dir", "d", "--pool-id", "lab",
         "--attest-identity", "attestor.pub"])
    assert args.attest_identity == ["attestor.pub"]


# ---------------------------------------------------------------------------
# 9. the runner that needs a runtime on the peer
# ---------------------------------------------------------------------------


def _has_cordis() -> bool:
    import importlib.util

    return importlib.util.find_spec("cordis") is not None


COMPOSITION_SOURCE = b"""service Counter {
  fn next() -> Int
}

component CounterSvc provides counter: Counter {
  provide counter {
    fn next() = 42
  }
}

component CounterUser requires counter: Counter {
}
"""

@pytest.mark.skipif(not _has_cordis(),
                    reason="the `run-once-py` runner boots a composition, "
                           "which needs a cordis-py runtime ON THE PEER; "
                           "without one this asserts nothing and is skipped "
                           "rather than weakened")
def test_the_run_once_runner_boots_the_composition_on_the_peer(tmp_path):
    """The other runner, executed rather than described. It boots the
    composition, tears it down LIFO and proves no residue, and the whole
    transcript is what the receipt pins."""
    digest = pd.artifact_digest(COMPOSITION_SOURCE)
    charter = make_charter(artifact_digests=(digest,))
    pool_dir, record, _roster, _directory = pool_on_disk(
        tmp_path, artifact=digest, charter=charter)
    port, _thread = serve_in_background(peer_runner(tmp_path, record))
    outcome = pd.dispatch_one(
        pool_dir=pool_dir, peer_id=WORKER, host="127.0.0.1", port=port,
        source=COMPOSITION_SOURCE, runner=pd.RUNNER_RUN_ONCE_PY,
        dispatch_identity=OPERATOR_ID, attesting_identity=ATTESTOR_ID)
    assert outcome["ok"], outcome
    assert outcome["result"]["runner"] == pd.RUNNER_RUN_ONCE_PY
    assert outcome["result"]["exit_code"] == 0
    joined = "\n".join(outcome["result"]["stdout"])
    assert "no residue" in joined or "residue" in joined, joined
