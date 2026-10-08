"""`revl pool promote`: the ladder made operable (issue #1198).

`peer_pool.promote` has recounted signed `(receipt, attestation)` pairs since
item 543, and `docs/design/550-private-peer-pool.md` marked the ladder, the
ledger and the ceiling diff DONE. What was missing was a way to REACH it: no
`revl pool` verb called it, and `pool init` could declare exactly one rung, so
`promote` was dead on arrival -- unreachable from the CLI and un-targetable
programmatically. `pool status` did not even render the ladder, so an operator
could not see that the rung they wanted was never declared.

This file is about the WIRING, so most of what it asserts is that the wiring
adds nothing. The claims, and the mutation that makes each fail:

* F1a  a pair the dispatcher FLAGGED but that does not verify must not promote.
       Mutation: trust `counts_as_evidence` instead of calling `promote`.
* F1b  a pair nobody flagged but that DOES verify must promote. This is the
       anti-duplicate pin, and it is the one that fails if `evidence_pairs`
       pre-filters on the dispatcher's flag. Mutation: filter in
       `evidence_pairs` on `counts_as_evidence`.
* F1c  `--tier-evidence N`: N-1 pairs do not move the member, N do. Mutation:
       compare with `<=` instead of `<`, or take the count from argv.
* F1d  two receipts sharing a `task_id` count once, and the refusal names
       `replayed-receipt`. Mutation: drop the dedup in `count_evidence`.
* F2   a peer cannot attest its own promotion. Mutation: add the member's own
       key to `attest_key_ids`.
* F3   a rung wider than the ceiling is refused `grant-ceiling` BEFORE any
       receipt is read. Mutation: run the evidence stage first (item 543).
* F4   promotion unlocks no dispatchable authority: `classify_artifact` still
       refuses the artifact. Mutation: let the tier grant widen what the
       dispatcher will send.
* F5   the charter digest covers `tiers`, so a rung cannot be added to a pool
       without re-signing it. Mutation: exclude `tiers` from the signed body.
* F6   a promote racing a withdraw cannot resurrect a withdrawn member.
       Mutation: read the roster outside `pool_state.locked`.
* F7   the refusal shapes: an undeclared rung, the entry rung, a rung with no
       stated cost, and a `--tier-caps` naming a rung nobody declared.

And the ratchets, which are the reason this is wiring rather than a second
policy engine:

* R1  no count is reachable from argv.
* R2  the promote branch contains no evidence arithmetic and no rung name.
* R3  `evidence_pairs` calls no counting or verification function.
* R4  every field the new code reads is defined by the parser.
"""

import ast
import io
import json
import os
import subprocess
import sys
import threading
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import peer_identity as pi  # noqa: E402
from revl import peer_offer, peer_pool as pp  # noqa: E402
from revl import pool_dispatch as pd  # noqa: E402
from revl import pool_receipt as pr  # noqa: E402
from revl.attest import key_id  # noqa: E402
from revl.cli.parser import build_parser  # noqa: E402
from revl.peer_offer import Attestation, PeerOffer  # noqa: E402

OP_KEY = b"pool-operator-key-1198-promotion"

WORKER = "peer-worker"
OTHER = "peer-other"
ATTESTOR = "pool-attestor"

#: Fixture identities. A seed committed to a repository is a published private
#: key, so these are used nowhere but here.
WORKER_ID = pi.identity_from_seed(WORKER, b"seed/worker/1198-promotion")
OTHER_ID = pi.identity_from_seed(OTHER, b"seed/other/1198-promotion")
ATTESTOR_ID = pi.identity_from_seed(ATTESTOR, b"seed/attestor/1198-promotion")
#: A well-formed key pair nobody pinned. That is what makes it an intruder.
INTRUDER_ID = pi.identity_from_seed("intruder", b"seed/intruder/1198-prom")

CEILING = ('fs.read(path="/data")',)
ENTRY_CAPS = ('fs.read(path="/data/in")',)
RUNG = "replayable"
ARTIFACT = "d" * 64

PURE_SOURCE = b"""pub fn add(a: Int, b: Int) -> Int {
  return a + b
}
"""

#: A composition with an audited boundary, so `classify_artifact` refuses it.
#: F4 needs an artifact the dispatcher will NOT send at any tier. One host
#: extern is the whole difference from `PURE_SOURCE`, so a refusal of this one
#: is a refusal of the boundary and not of the program.
IMPURE_SOURCE = b"""extern pure fn shout(line: Str) -> Int = @py {
  import sys
  sys.stdout.write(line)
  return 1
}

pub fn add(a: Int, b: Int) -> Int {
  return a + b
}
"""

#: How long a subprocess may take before the test fails loudly. A generous
#: bound on a loaded machine, not a timing assumption: nothing waits for it to
#: elapse on the passing path.
WAIT = 240


# ---------------------------------------------------------------------------
# the pool on disk
# ---------------------------------------------------------------------------


class Pool:
    """A pool directory, its key files and the arguments a CLI call needs."""

    def __init__(self, root: Path, *, tiers, directory, roster, record):
        self.root = root
        self.dir = root / "pool"
        self.keys = root / "keys"
        self.record = record
        self.directory = directory
        self.roster = roster

    @property
    def operator_key(self) -> Path:
        return self.keys / "operator.secret"

    def argv(self, *rest: str) -> list[str]:
        return list(rest)

    def reload(self) -> tuple[dict, pp.Roster]:
        return pp.load_pool(self.dir)

    def ledger(self) -> pd.DeliveryLedger:
        return pd.load_ledger(self.dir)

    def save_ledger(self, ledger) -> None:
        pd.save_ledger(self.dir, ledger)

    def deliver(self, ledger, *, task_id: str, receipt_identity=WORKER_ID,
                attestor=ATTESTOR_ID, counts=False, peer_id=WORKER,
                artifact=None):
        """One task out, one signed `(receipt, attestation)` pair back.

        ``counts`` defaults to FALSE on purpose. That flag is the
        dispatcher's verdict at dispatch time, and none of these tests want
        the promotion to be able to lean on it -- F1b is the pin that it
        cannot."""
        artifact = artifact or ARTIFACT
        ledger.dispatch(task_id=task_id, peer_id=peer_id,
                        artifact_digest=artifact, runner="run",
                        effect_class="pure")
        receipt = pr.issue_receipt(pool_id=self.record["pool_id"],
                                   task_id=task_id, artifact_digest=artifact,
                                   result={"rows": 1},
                                   identity=receipt_identity)
        attestation = pr.attest_receipt(receipt, identity=attestor)
        return ledger.deliver(task_id=task_id, peer_id=peer_id,
                              result_digest="r" * 64, receipt_digest="c" * 64,
                              counts=counts, receipt=receipt,
                              attestation=attestation)


def make_charter(*, tiers=None, ceiling=CEILING, attest_key_ids=None,
                 artifact_digests=(ARTIFACT,), pool_id="lab-1198-promotion"):
    return pp.sign_charter(pp.PoolCharter(
        pool_id=pool_id, ceiling=ceiling,
        tiers=tiers or {pp.ENTRY_TIER: pp.TierGrant(caps=ENTRY_CAPS)},
        admit_key_ids=(key_id(OP_KEY),), revoke_key_ids=(key_id(OP_KEY),),
        attest_key_ids=attest_key_ids or (ATTESTOR_ID.key_id,),
        artifact_digests=tuple(artifact_digests),
        identity_mode=pp.MODE_ASYMMETRIC), OP_KEY)


def _join(record, peer_id, identity, nonce, artifact=ARTIFACT):
    offer = PeerOffer(peer_id=peer_id,
                      attestation=Attestation(trust="verified"),
                      grant_ceiling=CEILING)
    return pp.sign_join_identity(pp.JoinRequest(
        pool_id=record["pool_id"], charter_digest=pp.canonical_digest(record),
        peer_id=peer_id, offer=peer_offer.sign_offer_identity(offer, identity),
        artifact_digest=artifact, nonce=nonce,
        issued_at=pp._iso(pp._utc_now())), identity)


def pool_on_disk(tmp_path, *, tiers=None, members=(WORKER,), ceiling=CEILING,
                 attest_key_ids=None, artifact_digests=(ARTIFACT,),
                 artifact=ARTIFACT):
    """A pool with the given ladder and members, written where the CLI reads
    it. Built through `admit`, so the roster is one a real join produced
    rather than a shape a test typed."""
    record = make_charter(tiers=tiers, ceiling=ceiling,
                          attest_key_ids=attest_key_ids,
                          artifact_digests=artifact_digests)
    directory = pi.IdentityDirectory()
    for identity in (WORKER_ID, OTHER_ID, ATTESTOR_ID, INTRUDER_ID):
        directory.register(identity.public())
    roster = pp.Roster(record["pool_id"], pp.canonical_digest(record))
    for n, peer in enumerate(members):
        identity = WORKER_ID if peer == WORKER else OTHER_ID
        verdict = pp.admit(record, _join(record, peer, identity, f"n-{n}",
                                         artifact=artifact),
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
    (keys / "operator.secret").write_bytes(OP_KEY)
    return Pool(tmp_path, tiers=tiers, directory=directory, roster=roster,
                record=record)


def rung(*, evidence=2, caps=CEILING) -> dict:
    return {pp.ENTRY_TIER: pp.TierGrant(caps=ENTRY_CAPS),
            RUNG: pp.TierGrant(caps=tuple(caps), evidence_required=evidence)}


# ---------------------------------------------------------------------------
# driving the CLI
# ---------------------------------------------------------------------------


def run_cli(argv) -> tuple[int, str, str]:
    """The verb, in process, with the exit code and both streams captured."""
    args = build_parser().parse_args(list(argv))
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = pp.pool_command(args)
    return code, out.getvalue(), err.getvalue()


def promote_cli(pool, *extra, tier=RUNG, peer=WORKER) -> tuple[int, str, str]:
    return run_cli(["pool", "promote", "--dir", str(pool.dir), "--peer", peer,
                    "--tier", tier, "--key", str(pool.operator_key), *extra])


def receipt_of(printed: str) -> dict:
    """The JSON receipt the verb printed. `promote` prints the receipt and
    nothing else, so a refusal is still a parseable record rather than a
    sentence."""
    return json.loads(printed)


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


# ---------------------------------------------------------------------------
# F7 -- the refusal shapes, and that a refusal is NAMED
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("flags,names", [
    (["--tier", "hotfix", "--tier-evidence", "hotfix=1"], "unknown tier"),
    (["--tier", pp.ENTRY_TIER], "entry rung"),
    (["--tier", RUNG], "states no evidence requirement"),
    (["--tier", RUNG, "--tier-evidence", f"{RUNG}=-1"], "non-negative"),
    (["--tier", RUNG, "--tier-evidence", f"{RUNG}=x"], "not an integer"),
    (["--tier-caps", f"{RUNG}=compute()"], "which no --tier declares"),
])
def test_init_refuses_a_ladder_it_cannot_state(tmp_path, flags, names):
    """Every one of these is a pool whose ladder would be a lie: a rung that
    is not a rung, a rung with no cost, a cost that is not a count, or a grant
    attached to a rung nobody declared. Each is refused BY NAME, at the CLI,
    before a charter is written.

    The mutation that makes this load-bearing is deleting the check: with
    `--tier-evidence` optional, a rung's cost would default, and a default of
    0 makes the rung free while any other default is this tool inventing a
    threshold. Either way the operator's stated cost stops being the cost."""
    out = tmp_path / "pool"
    key = tmp_path / "k"
    key.write_bytes(OP_KEY)
    code, _, err = run_cli(["pool", "init", "--dir", str(out),
                            "--pool-id", "p", "--key", str(key), *flags])
    assert code == 2, (code, err)
    assert names in err, err
    assert not (out / pp.CHARTER_FILE).exists(), (
        "a refused ladder must not leave a charter behind")


def test_init_declares_the_ladder_and_status_renders_it(tmp_path):
    """The rung reaches the charter, and the operator can SEE the ladder.

    `pool status` rendering no ladder at all was half of the dead-on-arrival
    bug: `promote` refused `unknown-tier` for a rung the operator believed
    they had declared, with nothing on screen to say otherwise. Mutation: drop
    the `ladder` line from `render_status`."""
    pool = pool_on_disk(tmp_path, tiers=rung(evidence=3))
    status = pp.render_status(pool.record, pool.roster)
    assert "ladder" in status
    assert f"{RUNG}(evidence>=3, caps={CEILING[0]})" in status, status


def test_status_names_the_rungs_this_pool_does_NOT_offer(tmp_path):
    """A pool that declares only the entry tier offers no promotion at all.
    That is the fact the ladder line exists to make legible, and it is the
    state every pool was in before this slice. Mutation: render every rung in
    `TIER_ORDER` regardless of what the charter declares, which would show a
    ladder the pool cannot climb."""
    pool = pool_on_disk(tmp_path)
    status = pp.render_status(pool.record, pool.roster)
    ladder = [line for line in status.splitlines() if "ladder" in line][0]
    assert f"{pp.ENTRY_TIER}(entry" in ladder
    for absent in ("replayable", "durable"):
        assert absent not in ladder, ladder


def test_promote_refuses_a_rung_the_charter_does_not_declare(tmp_path):
    """F7, promote side. `promote` has always refused `unknown-tier`; the
    point here is that the CLI reaches that refusal and reports it as a
    refusal rather than a traceback."""
    pool = pool_on_disk(tmp_path, tiers=rung())
    code, printed, _ = promote_cli(pool, tier="durable")
    assert code == 1
    assert receipt_of(printed)["link"] == pp.LINK_UNKNOWN_TIER


# ---------------------------------------------------------------------------
# F1 -- the count is the one `count_evidence` produced
# ---------------------------------------------------------------------------


def test_promote_moves_a_member_when_the_pairs_verify(tmp_path):
    """The positive case, and the baseline for every refusal below."""
    pool = pool_on_disk(tmp_path, tiers=rung(evidence=2))
    ledger = pool.ledger()
    for n in (1, 2):
        pool.deliver(ledger, task_id=f"task-{n}")
    pool.save_ledger(ledger)

    code, printed, _ = promote_cli(pool)
    assert code == 0, printed
    receipt = receipt_of(printed)
    assert receipt["verdict"] == pp.PROMOTE
    assert receipt["from_tier"] == pp.ENTRY_TIER
    assert receipt["tier"] == RUNG
    assert receipt["evidence"] == 2
    assert receipt["effect_ceiling"] == pp.TIER_EFFECT_CEILING[RUNG].value
    # And it is PERSISTED, which is the whole point of a verb: a promotion
    # that only printed would be gone by the next read.
    _, roster = pool.reload()
    assert roster.members[WORKER].tier == RUNG
    assert roster.members[WORKER].evidence == 2


def test_an_unflagged_but_valid_pair_still_promotes(tmp_path):
    """F1b, the anti-duplicate pin.

    Every pair here carries `counts_as_evidence=False`, because that flag is
    the DISPATCHER's verdict at dispatch time and the judge of what counts as
    evidence is `count_evidence`, which re-verifies the signatures. If
    `evidence_pairs` filtered on the flag -- the obvious way to write it, and
    the tempting way -- this is the test that fails, and the failure direction
    is the wrong one: a peer whose deliveries were never flagged could never
    be promoted no matter how much verified work it did."""
    pool = pool_on_disk(tmp_path, tiers=rung(evidence=2))
    ledger = pool.ledger()
    for n in (1, 2):
        pool.deliver(ledger, task_id=f"task-{n}", counts=False)
    pool.save_ledger(ledger)
    assert all(not entry.get("counts_as_evidence")
               for entry in ledger.tasks.values())

    code, printed, _ = promote_cli(pool)
    assert code == 0, printed
    assert receipt_of(printed)["evidence"] == 2


def test_a_flagged_but_invalid_pair_does_not_promote(tmp_path):
    """F1a. The flag says yes; the signature says no, because it was signed by
    a key nobody pinned. `count_evidence` recounts and the member stays.

    Mutation: count the flags. `evidence_pairs` deliberately does not look at
    `counts_as_evidence`, so a `promote` that trusted it would be a second,
    weaker policy engine that believes a boolean instead of a signature."""
    pool = pool_on_disk(tmp_path, tiers=rung(evidence=2))
    ledger = pool.ledger()
    pool.deliver(ledger, task_id="task-1", counts=True)          # honest
    pool.deliver(ledger, task_id="task-2", counts=True,
                 receipt_identity=INTRUDER_ID)                   # forged
    pool.save_ledger(ledger)

    code, printed, _ = promote_cli(pool)
    assert code == 1
    receipt = receipt_of(printed)
    assert receipt["link"] == pp.LINK_PROMOTION_EVIDENCE
    assert receipt["verdict"] == pp.REFUSE
    _, roster = pool.reload()
    assert roster.members[WORKER].tier == pp.ENTRY_TIER, (
        "a member with one verified receipt must not reach a rung costing two")


def test_the_threshold_is_exclusive_of_the_count(tmp_path):
    """F1c. N-1 pairs do not move the member and N do, on one pool, with the
    only difference being the number of receipts. Mutation: compare with `<=`,
    or let the caller supply the count."""
    pool = pool_on_disk(tmp_path, tiers=rung(evidence=3))
    ledger = pool.ledger()
    for n in (1, 2):
        pool.deliver(ledger, task_id=f"task-{n}")
    pool.save_ledger(ledger)

    code, printed, _ = promote_cli(pool)
    assert code == 1
    refusal = receipt_of(printed)
    assert refusal["link"] == pp.LINK_PROMOTION_EVIDENCE
    assert "requires 3" in refusal["reason"] and "has 2" in refusal["reason"]
    assert pool.reload()[1].members[WORKER].tier == pp.ENTRY_TIER

    pool.deliver(pool.ledger(), task_id="task-3")
    ledger = pool.ledger()
    pool.deliver(ledger, task_id="task-3")
    pool.save_ledger(ledger)

    code, printed, _ = promote_cli(pool)
    assert code == 0, printed
    assert receipt_of(printed)["evidence"] == 3


def test_two_receipts_sharing_a_task_id_count_once(tmp_path):
    """F1d. A peer that re-signs the same work with a fresh timestamp and a
    fresh digest buys nothing: `count_evidence` deduplicates on `task_id`, and
    the duplicate is APPENDED as a refusal rather than dropped.

    Mutation: drop the dedup. Then one task's result, re-signed twice, would
    satisfy a rung costing two -- and the second signature is free, so every
    rung above entry would be reachable from one delivery."""
    pool = pool_on_disk(tmp_path, tiers=rung(evidence=2))
    ledger = pool.ledger()
    ledger.dispatch(task_id="task-1", peer_id=WORKER, artifact_digest=ARTIFACT,
                    runner="run", effect_class="pure")
    first = pr.issue_receipt(pool_id=pool.record["pool_id"], task_id="task-1",
                             artifact_digest=ARTIFACT, result={"rows": 1},
                             identity=WORKER_ID)
    ledger.deliver(task_id="task-1", peer_id=WORKER, result_digest="r" * 64,
                   receipt_digest="c" * 64, counts=False, receipt=first,
                   attestation=pr.attest_receipt(first, identity=ATTESTOR_ID))
    again = pr.issue_receipt(pool_id=pool.record["pool_id"], task_id="task-1",
                             artifact_digest=ARTIFACT, result={"rows": 2},
                             identity=WORKER_ID)
    # A second task id, but the SAME `task_id` inside the receipt: the ledger
    # slot differs while the work being claimed does not.
    ledger.dispatch(task_id="task-2", peer_id=WORKER, artifact_digest=ARTIFACT,
                    runner="run", effect_class="pure")
    ledger.deliver(task_id="task-2", peer_id=WORKER, result_digest="r" * 64,
                   receipt_digest="c" * 64, counts=False, receipt=again,
                   attestation=pr.attest_receipt(again, identity=ATTESTOR_ID))
    pool.save_ledger(ledger)

    code, printed, _ = promote_cli(pool)
    assert code == 1, printed
    refusal = receipt_of(printed)
    assert refusal["link"] == pp.LINK_PROMOTION_EVIDENCE
    assert refusal["has"] == 1, (
        "two receipts claiming the same task_id are one piece of work")
    assert refusal["refused"].get(pr.LINK_REPLAYED_RECEIPT) == 1, refusal
    assert "replayed-receipt" in refusal["reason"], refusal["reason"]


def test_no_argv_supplies_a_count(tmp_path):
    """R1. The fail-open this whole verb exists to close was a caller handing
    in an evidence integer. There must be no flag that carries one.

    Mutation: add `--evidence-count N` and have the branch pass it through.
    The parser assertion below is what fails, and it fails on the NAME, so a
    flag spelled `--force` or `--bypass-ceiling` is caught too."""
    args = build_parser().parse_args(
        ["pool", "promote", "--dir", "d", "--peer", "p", "--tier", RUNG])
    fields = set(vars(args))
    for forbidden in ("evidence", "count", "force", "bypass", "threshold",
                      "required", "receipts"):
        assert not [f for f in fields if forbidden in f], (
            f"`pool promote` must not accept a {forbidden!r} flag: the count "
            f"is recounted from the ledger, not supplied")
    # And the field it does read is the parser's, not a typo that happens to
    # parse. R4's idiom, scoped to this verb.
    assert args.tier == RUNG and args.peer == "p"


# ---------------------------------------------------------------------------
# F2 -- a peer cannot attest its own promotion
# ---------------------------------------------------------------------------


def test_a_peer_cannot_attest_its_own_promotion(tmp_path):
    """F2. The member signs its own receipt AND its own attestation. Every
    signature verifies; the attestor is simply not one the charter names, so
    the pair is not evidence and the member stays put.

    Mutation: add the member's key to `attest_key_ids`, or let the attestation
    stage fall back to the receipt's signer. Then a peer manufactures its own
    evidence and the ladder is a self-service counter."""
    pool = pool_on_disk(tmp_path, tiers=rung(evidence=2))
    ledger = pool.ledger()
    for n in (1, 2):
        pool.deliver(ledger, task_id=f"task-{n}", attestor=WORKER_ID)
    pool.save_ledger(ledger)

    code, printed, _ = promote_cli(pool)
    assert code == 1
    assert receipt_of(printed)["link"] == pp.LINK_PROMOTION_EVIDENCE
    assert pool.reload()[1].members[WORKER].tier == pp.ENTRY_TIER


# ---------------------------------------------------------------------------
# F3 -- the ceiling diff gates the stages that read evidence (item 543)
# ---------------------------------------------------------------------------


def test_a_rung_wider_than_the_ceiling_is_refused_before_evidence(tmp_path):
    """F3. The rung grants `fs.read(path="/data")` while the pool's ceiling is
    `fs.read(path="/data/in")`, so the pool cannot lawfully issue it.

    Two claims, and the second is the item-543 rule: the refusal names
    `grant-ceiling`, and the receipt carries NO `evidence` key at all -- not
    `evidence: 0`, absent -- because the evidence stage never ran. The
    authority diff is a property of the charter and needs no observation; the
    evidence count is an observation. Weighing them after the fact would make
    a pool read receipts it was never going to accept.

    The ledger is deliberately EMPTY, and that is what makes the order
    observable: the member has none of the receipts the rung costs, so a
    version that ran the measured stage first would refuse
    `promotion-evidence` and never reach the ceiling. Mutation: swap the two
    preconditions in `promote`. A pool with exactly enough receipts cannot
    tell the two orders apart, which is why this one has none."""
    pool = pool_on_disk(tmp_path, tiers=rung(evidence=1),
                        ceiling=ENTRY_CAPS)

    code, printed, _ = promote_cli(pool)
    assert code == 1
    receipt = receipt_of(printed)
    assert receipt["link"] == pp.LINK_GRANT_CEILING, receipt
    assert "evidence" not in receipt, (
        "the ceiling diff must run ahead of the evidence stage")
    # `init` DID declare the rung. A ladder is declarable before it is
    # issuable; what is refused is the grant, at the moment it would be made.
    assert RUNG in pp.charter_from_record(pool.record).tiers


# ---------------------------------------------------------------------------
# F4 -- promotion unlocks no dispatchable authority
# ---------------------------------------------------------------------------


def test_promotion_does_not_widen_what_the_dispatcher_will_send(tmp_path):
    """F4, the honest-limit pin. A `replayable` member holds an
    `idempotent-external` effect ceiling and a wider cap, and the dispatcher
    STILL refuses the artifact, because `classify_artifact` proves only
    boundary-free compositions and this one has a boundary.

    Mutation: let the tier grant decide what the dispatcher sends. That is the
    claim this slice must not make: the ladder is a statement about what a
    member may be ASKED to do, not a licence to cross a boundary the pool
    never audited."""
    impure = pd.artifact_digest(IMPURE_SOURCE)
    pool = pool_on_disk(tmp_path, tiers=rung(evidence=2),
                        artifact_digests=(impure,), artifact=impure)
    ledger = pool.ledger()
    for n in (1, 2):
        pool.deliver(ledger, task_id=f"task-{n}", artifact=impure)
    pool.save_ledger(ledger)

    code, printed, _ = promote_cli(pool)
    assert code == 0, printed
    receipt = receipt_of(printed)
    assert receipt["effect_ceiling"] == "idempotent-external", (
        "the promotion itself widens the member's ceiling")

    # ...and the artifact is still refused, by the dispatcher, at every tier.
    # The refusal is `effect-class-unproven` and it comes before the ledger is
    # touched, so a task nobody can lawfully send leaves no record behind.
    before = dict(pool.ledger().tasks)
    refusal = pd.dispatch_one(
        pool_dir=pool.dir, peer_id=WORKER, host="127.0.0.1", port=1,
        source=IMPURE_SOURCE, runner=pd.RUNNER_RUN_ONCE_PY,
        dispatch_identity=WORKER_ID, attesting_identity=ATTESTOR_ID)
    assert not refusal.get("ok"), refusal
    assert refusal["link"] == pd.LINK_EFFECT_CLASS_UNPROVEN, refusal
    assert "unproven here, not assumed safe" in refusal["reason"], refusal
    assert pool.ledger().tasks == before, (
        "a refused dispatch must leave no task behind")


def test_the_effect_ceiling_is_the_ladders_own_table(tmp_path):
    """The tier the verb moved the member to and the ceiling the receipt
    reports are read from `TIER_EFFECT_CEILING`, not from a table this verb
    keeps. Mutation: add a second ceiling table to the verb."""
    pool = pool_on_disk(tmp_path, tiers=rung(evidence=1))
    ledger = pool.ledger()
    pool.deliver(ledger, task_id="task-1")
    pool.save_ledger(ledger)
    code, printed, _ = promote_cli(pool)
    assert code == 0, printed
    assert receipt_of(printed)["effect_ceiling"] == (
        pp.TIER_EFFECT_CEILING[RUNG].value)


# ---------------------------------------------------------------------------
# F5 -- the charter digest covers the ladder
# ---------------------------------------------------------------------------


def test_the_charter_digest_covers_the_declared_ladder(tmp_path):
    """F5. A rung's cost is a term of the pool, so adding a rung to a signed
    charter must invalidate it. Without this, an operator (or anyone holding
    the file) could add a cheap rung and every peer's pinned digest would
    still match.

    Mutation: drop `tiers` from `PoolCharter.body`."""
    one = make_charter()
    two = make_charter(tiers=rung(evidence=1))
    assert pp.canonical_digest(one) != pp.canonical_digest(two)
    assert pp.canonical_digest(one) != pp.canonical_digest(
        make_charter(tiers=rung(evidence=2))), (
        "the COST of a rung is covered too, not only its name")
    # A tampered charter is refused rather than re-read under the new terms.
    edited = dict(two)
    edited["tiers"] = {pp.ENTRY_TIER: pp.TierGrant(caps=ENTRY_CAPS),
                       RUNG: pp.TierGrant(caps=CEILING, evidence_required=0)}
    ok, _ = pp.verify_charter(edited, OP_KEY)
    assert not ok


def test_a_rechartered_pool_starts_a_new_ledger(tmp_path):
    """The consequence of F5, and why the verb passes the ledger through
    `load_ledger` rather than reading `ledger.json` itself: evidence gathered
    under one charter is not evidence for the next one.

    Mutation: read the ledger file directly in the promote branch, which would
    carry old pairs across a re-charter."""
    pool = pool_on_disk(tmp_path, tiers=rung(evidence=1))
    ledger = pool.ledger()
    pool.deliver(ledger, task_id="task-1")
    pool.save_ledger(ledger)

    rechartered = make_charter(tiers=rung(evidence=1))
    rechartered["charter_digest"] = "0" * 64   # a different signed charter
    (pool.dir / pp.CHARTER_FILE).write_text(json.dumps(rechartered),
                                            encoding="utf-8")
    with pytest.raises(pd.DispatchError):
        pool.ledger()


# ---------------------------------------------------------------------------
# F6 -- a promote racing a withdraw cannot resurrect a member
# ---------------------------------------------------------------------------


def test_promote_cannot_resurrect_a_withdrawn_member(tmp_path):
    """F6. Two REAL `python -m revl` processes against one pool directory.

    The promote process is held at the point where it has read the roster and
    is about to write it; the withdraw process then removes the member. On the
    tree before `pool_state.locked` wrapped the verb, the promote wrote its
    copy back and the withdrawn peer was a member again -- the exact bug issue
    #1198 records for a dispatch finishing after a withdrawal.

    Mutation: take the lock off `_gate_command`, or read the roster before
    entering it."""
    pool = pool_on_disk(tmp_path, tiers=rung(evidence=1))
    ledger = pool.ledger()
    pool.deliver(ledger, task_id="task-1")
    pool.save_ledger(ledger)

    # Hold the pool lock so the promote process blocks INSIDE the verb, after
    # it would have read the roster and before it writes. No sleeps: the
    # withdraw is started only once the lock is held, and the promote is
    # released only once the withdraw has finished.
    from revl import pool_state as ps  # noqa: PLC0415

    release = threading.Event()
    holding = threading.Event()

    def hold():
        with ps.locked(pool.dir):
            holding.set()
            release.wait(timeout=WAIT)

    holder = threading.Thread(target=hold, daemon=True)
    holder.start()
    assert holding.wait(timeout=WAIT)

    promote = start("pool", "promote", "--dir", str(pool.dir), "--peer",
                    WORKER, "--tier", RUNG, "--key", str(pool.operator_key))
    withdraw = start("pool", "withdraw", "--dir", str(pool.dir), "--peer",
                     WORKER, "--key", str(pool.operator_key))

    # Let both processes reach the lock, then release it. Whichever wins, the
    # invariant is the same and is checked below.
    threading.Event().wait(0.5)
    release.set()
    finish(promote)
    finish(withdraw)

    _, roster = pool.reload()
    if WORKER in roster.revoked:
        assert WORKER not in roster.members, (
            "a withdrawn peer must not be a member, whatever else ran")
    # Whichever order the lock granted, the roster on disk is one the two
    # verbs produced in sequence rather than one of them overwriting the
    # other's decision.
    assert json.loads((pool.dir / pp.ROSTER_FILE).read_text(
        encoding="utf-8"))["members"].keys() == roster.members.keys()


# ---------------------------------------------------------------------------
# R2/R3 -- the branch is wiring, not a policy engine
# ---------------------------------------------------------------------------


def _function_source(name: str) -> str:
    source = (ROOT / "src" / "revl" / "peer_pool.py").read_text(
        encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(source, node) or ""
    raise AssertionError(f"no function {name!r} in peer_pool.py")


def _without_docstring(nodes):
    """The statements, minus any leading string literal. A docstring NAMES the
    rule it explains -- including the identifiers the rule forbids -- so a pin
    about what the code does must read the code."""
    return [n for n in nodes
            if not (isinstance(n, ast.Expr)
                    and isinstance(n.value, ast.Constant)
                    and isinstance(n.value.value, str))]


def test_the_promote_branch_holds_no_threshold_and_no_rung_name():
    """R2. The branch that reaches `promote` may LOAD, CALL, SAVE and RENDER.
    It may not decide: no integer literal to compare a count against, and no
    rung name spelled in the source. Both would be a policy engine living in
    the CLI, beside the checked one, and the checked one would stop being
    where the answer comes from.

    Mutation: `if len(pairs) >= 2:`, or `if args.tier == "replayable":`."""
    branch = _promote_branch_source()
    tree = ast.parse(branch)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, int):
            assert not isinstance(node.value, bool), (
                f"the promote branch compares a literal {node.value!r}")
            raise AssertionError(
                f"the promote branch contains the integer literal "
                f"{node.value!r}; the threshold is the charter's")
    for name in pp.TIER_ORDER:
        assert f'"{name}"' not in branch and f"'{name}'" not in branch, (
            f"the promote branch names the rung {name!r}; the rung is the "
            f"operator's argument, checked against the charter's ladder")


def _promote_branch_source() -> str:
    """The `elif verb == "promote":` branch of `_gate_command`, as CODE.

    Unparsed rather than taken as a raw source segment, for two reasons: the
    segment begins with `elif` and runs into the `else` clause that follows
    it, neither of which parses on its own; and it carries comments, which
    explain the rule rather than being it. What the pins below are about is
    the statements."""
    source = (ROOT / "src" / "revl" / "peer_pool.py").read_text(
        encoding="utf-8")
    tree = ast.parse(source)
    gate = [n for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef) and n.name == "_gate_command"]
    assert gate, "no _gate_command"
    for node in ast.walk(gate[0]):
        if not isinstance(node, ast.If):
            continue
        test = node.test
        if (isinstance(test, ast.Compare)
                and isinstance(test.left, ast.Name) and test.left.id == "verb"
                and any(isinstance(c, ast.Constant) and c.value == "promote"
                        for c in test.comparators)):
            body = _without_docstring(node.body)
            return ast.unparse(ast.Module(body=body, type_ignores=[]))
    raise AssertionError("no promote branch in _gate_command")


def test_evidence_pairs_is_a_read_accessor_not_a_judge():
    """R3. `evidence_pairs` selects records; it decides nothing. If it called
    `count_evidence`, `check_receipt`, `grant_widenings` or `promote`, the
    verdict would have two authors and the one in the CLI would be the weaker
    -- it would see less of the charter, and the pair that reached `promote`
    would already have been filtered by a copy of the rule.

    Mutation: move the `count_evidence` call into `evidence_pairs` so the verb
    can print a count without asking the judge."""
    source = (ROOT / "src" / "revl" / "pool_dispatch.py").read_text(
        encoding="utf-8")
    tree = ast.parse(source)
    target = [n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "evidence_pairs"]
    assert target, "no evidence_pairs in pool_dispatch.py"
    called = {n.func.attr if isinstance(n.func, ast.Attribute)
              else getattr(n.func, "id", "")
              for n in ast.walk(target[0]) if isinstance(n, ast.Call)}
    for forbidden in ("count_evidence", "check_receipt", "grant_widenings",
                      "promote", "verify_result"):
        assert forbidden not in called, (
            f"evidence_pairs calls {forbidden}; it is a read accessor")
    # And it does not read the dispatcher's flag either -- F1b's pin, at the
    # level of the source rather than one scenario.
    code = ast.unparse(ast.Module(body=_without_docstring(target[0].body),
                                  type_ignores=[]))
    assert "counts_as_evidence" not in code, (
        "evidence_pairs must not filter on the dispatcher's verdict")


def test_evidence_pairs_returns_only_pairs_and_only_for_one_peer(tmp_path):
    """The accessor's contract, spelled out: entries without BOTH halves are
    not pairs, and another peer's work is not this peer's evidence."""
    pool = pool_on_disk(tmp_path, members=(WORKER, OTHER), tiers=rung())
    ledger = pool.ledger()
    pool.deliver(ledger, task_id="task-1")
    # A delivery with no signed pair stored: the digest-only shape a ledger
    # written before the pairs were kept would have.
    ledger.dispatch(task_id="task-2", peer_id=WORKER, artifact_digest=ARTIFACT,
                    runner="run", effect_class="pure")
    ledger.deliver(task_id="task-2", peer_id=WORKER, result_digest="r" * 64,
                   receipt_digest="c" * 64, counts=True)
    pool.deliver(ledger, task_id="task-3", peer_id=OTHER)

    pairs = pd.evidence_pairs(ledger, peer_id=WORKER)
    assert len(pairs) == 1, pairs
    assert all(isinstance(r, dict) and isinstance(a, dict) for r, a in pairs)
    assert pairs[0][0]["task_id"] == "task-1"
    assert pd.evidence_pairs(ledger, peer_id="nobody") == []


def test_every_field_the_new_code_reads_is_defined_by_the_parser(tmp_path):
    """R4, in the idiom of
    `tests/test_pool_dispatch_524.py::test_the_run_parser_defines_every_flag_pool_dispatch_reads`.

    A field the code reads and the parser does not define is a crash waiting
    for the flag that would have set it, and `getattr(args, "tier", None)`
    would hide it as an empty ladder rather than a failure.

    Mutation: read `args.tier_evidence` under a flag the parser calls
    something else."""
    args = build_parser().parse_args(
        ["pool", "init", "--dir", "d", "--pool-id", "p"])
    for field in ("tier", "tier_evidence", "tier_caps", "entry_caps"):
        assert field in vars(args), (
            f"`_declared_tiers` reads args.{field} but the parser does not "
            f"define it")
    promote = build_parser().parse_args(
        ["pool", "promote", "--dir", "d", "--peer", "p", "--tier", RUNG])
    for field in ("dir", "peer", "tier", "key", "identity_key"):
        assert field in vars(promote), field


def test_a_pool_init_without_the_tier_flags_is_unchanged(tmp_path):
    """The additive pin. Every pool declared before this slice declared one
    rung, and those charters must still be exactly what they were: the entry
    tier, its caps, and nothing else.

    Mutation: give a rung a default, or add a rung when none was asked for."""
    args = build_parser().parse_args(
        ["pool", "init", "--dir", "d", "--pool-id", "p",
         "--entry-caps", ENTRY_CAPS[0]])
    tiers = pp._declared_tiers(args)
    assert tiers == {pp.ENTRY_TIER: pp.TierGrant(caps=ENTRY_CAPS)}, tiers


# ---------------------------------------------------------------------------
# the deliverable: one command an operator can run
# ---------------------------------------------------------------------------


def test_the_whole_flow_through_the_real_cli(tmp_path):
    """The slice definition, executed: declare a rung, admit a peer, let it
    deliver twice, promote it, and see it on the operator view.

    Everything here goes through `python -m revl` as separate processes, so
    what is being tested is the product and not the API the tests share."""
    pool_dir = tmp_path / "pool"
    keys = tmp_path / "keys"
    keys.mkdir()
    (keys / "operator.secret").write_bytes(OP_KEY)
    worker_key, worker_pub = keys / "w.key", keys / "w.pub"
    att_key, att_pub = keys / "a.key", keys / "a.pub"

    def cli(*argv):
        code, out, err = finish(start(*argv))
        return code, out, err

    assert cli("pool", "keygen", "--peer-id", WORKER, "--out", str(worker_key),
               "--public", str(worker_pub))[0] == 0
    assert cli("pool", "keygen", "--peer-id", ATTESTOR, "--out", str(att_key),
               "--public", str(att_pub))[0] == 0

    code, out, err = cli(
        "pool", "init", "--dir", str(pool_dir), "--pool-id", "demo",
        "--key", str(keys / "operator.secret"),
        "--ceiling", CEILING[0], "--entry-caps", ENTRY_CAPS[0],
        "--artifact", ARTIFACT, "--tier", RUNG,
        "--tier-evidence", f"{RUNG}=2",
        "--tier-caps", f"{RUNG}={CEILING[0]}",
        "--attest-identity", str(att_pub))
    assert code == 0, err
    assert f"ladder    {pp.ENTRY_TIER}(entry" in out and RUNG in out, out

    assert cli("pool", "register", "--dir", str(pool_dir),
               "--public", str(worker_pub))[0] == 0
    assert cli("pool", "request", "--charter", str(pool_dir / "charter.json"),
               "--peer-id", WORKER, "--artifact", ARTIFACT,
               "--ceiling", CEILING[0], "--identity-key", str(worker_key),
               "--out", str(tmp_path / "join.json"))[0] == 0
    code, out, err = cli("pool", "join", "--dir", str(pool_dir),
                         "--join", str(tmp_path / "join.json"),
                         "--key", str(keys / "operator.secret"))
    assert code == 0, err
    assert json.loads(out)["verdict"] == pp.ADMIT

    # Promote before any work: refused, and the refusal states the cost.
    code, out, err = cli("pool", "promote", "--dir", str(pool_dir),
                         "--peer", WORKER, "--tier", RUNG,
                         "--key", str(keys / "operator.secret"))
    assert code == 1
    assert json.loads(out)["link"] == pp.LINK_PROMOTION_EVIDENCE

    # Two real deliveries, through the ledger the dispatcher writes.
    ledger = pd.load_ledger(pool_dir)
    for n in (1, 2):
        receipt = pr.issue_receipt(
            pool_id="demo", task_id=f"task-{n}", artifact_digest=ARTIFACT,
            result={"rows": n}, identity=pi.load_private_identity(worker_key))
        ledger.dispatch(task_id=f"task-{n}", peer_id=WORKER,
                        artifact_digest=ARTIFACT, runner="run",
                        effect_class="pure")
        ledger.deliver(task_id=f"task-{n}", peer_id=WORKER,
                       result_digest="r" * 64, receipt_digest="c" * 64,
                       counts=False, receipt=receipt,
                       attestation=pr.attest_receipt(
                           receipt,
                           identity=pi.load_private_identity(att_key)))
    pd.save_ledger(pool_dir, ledger)

    code, out, err = cli("pool", "promote", "--dir", str(pool_dir),
                         "--peer", WORKER, "--tier", RUNG,
                         "--key", str(keys / "operator.secret"))
    assert code == 0, err
    receipt = json.loads(out)
    assert receipt["verdict"] == pp.PROMOTE and receipt["evidence"] == 2

    code, out, err = cli("pool", "status", "--dir", str(pool_dir))
    assert code == 0, err
    assert f"{WORKER}  tier={RUNG}" in out, out
    assert f"ladder    {pp.ENTRY_TIER}(entry" in out and RUNG in out, out
