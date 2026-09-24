# Design: dispatching work to a pool member, and the one-result ledger (item 524, issue #1198)

Status: landed. `src/revl/pool_dispatch.py`, with `revl pool serve`,
`revl pool ledger` and `revl run --pool private`.

Read first: [550-private-peer-pool.md](550-private-peer-pool.md) (the pool, the
join gate, the tier ladder and withdrawal),
[566-pool-execution-receipts.md](566-pool-execution-receipts.md) (what a receipt
is and what counting one means), and
[555-asymmetric-peer-identity.md](555-asymmetric-peer-identity.md) (why a peer's
identity is a key pair).

## What was missing

Item 550's "what is left" list had four open points (its points 3 and 4 were
already closed by the receipt and asymmetric-identity slices). This closes three
of them and says exactly what the fourth needs.

`peer_pool` could declare a pool and admit a peer to it. `pool_receipt` could
check what a peer signed about work it ran. Between the two there was nothing:
no way to hand an admitted member a unit of work, no channel to hand it over,
and no record that a result came back exactly once. The consequences were
visible in the product surface rather than only in the code. `Roster.outstanding`
was a shape with no populator anywhere in the tree, so a withdrawal reported an
empty `orphaned` set on every real deployment: honest, and not load-bearing.
`revl run --pool private` did not exist at all; the flag was refused by argparse.

## The three new things, and the many old ones

Only three things here are new, and that is deliberate. Item 524's stated
failure mode is "a second, weaker policy engine beside the checked one", so
every decision that already had an owner keeps it:

| the question | who answers it |
|---|---|
| may this member be sent work of this class? | `peer_pool.work_admissible`, keyed on `lawful_retry.EffectClass` |
| is this result the one the receipt is about? | `pool_receipt.verify_result` (a hash) |
| is this receipt usable? | `pool_receipt.check_receipt` (signatures, key authority, binding) |
| what becomes of a lost peer's outstanding work? | `lawful_retry.dispatch_on_loss` |
| who may admit, revoke and attest? | the charter's three key sets |

What is new is a signed **task envelope**, a **delivery ledger** with one-result
semantics, and a **channel**.

## The task envelope

A task is signed by the operator under an identity key pair, in its own domain
(`revl.pool-task/v1`, distinct from the charter, join, receipt, attestation and
withdrawal domains for the reason each of those is distinct from the others).
The peer verifies it under a public key it pinned out of band.

The asymmetry is the point. The peer holds no operator secret, so nothing on the
peer's machine is sufficient to admit anyone, including itself. A shared key
would have made the peer able to mint its own work, and a peer that can mint its
own work has no bound at all.

The envelope carries the artifact SOURCE beside its digest, and the digest is
computed from the bytes when the body is built rather than accepted as a
parameter, so an operator cannot sign a task whose declared digest is not the
digest of the artifact it carries. The peer then re-hashes anyway. That is item
118's binding rule on this path ("a receiver re-hashes the IR + artifact bytes
it will execute, never trusts the attestation's self-declared hash"), and it is
tested with a perfectly signed task whose declared digest is one the charter
admits and whose bytes are different.

Eight checks on the peer side, each naming one lowercase link, each failing
closed: `task-shape`, `task-signature`, `pool-identity`, `charter-identity`,
`peer-identity`, `artifact-digest`, `unknown-runner`, `replayed-task`. Four more
on the way back: `peer-unreachable`, `result-digest`, `receipt-refused`,
`delivered-twice`. None of them is a `revl.diagnostics` guarantee code, for the
reason `peer_pool` and `revl.deploy` give: a dispatch refusal is a decision
about a deployment, not a verdict about a program, and there is no program to
write under `examples/rejections/` for "this peer already ran that task id".

`task_id` names a directory on the peer, so it is constrained to 64 characters
of `[A-Za-z0-9._-]` in the shape check. A signature proves who sent a task; it
does not make `../../etc` a safe file name, and the peer is the side that would
pay for the difference.

## One result, impossible and visible

Item 524 asks that "delivered twice must be impossible or visible". Both halves
are built rather than one.

**Impossible**: the ledger's state machine has no edge from `delivered`,
`orphaned` or `refused` back to `dispatched`. The test asserts that over the
three transitions that can move a task rather than over one sequence.

**Visible**: a second delivery is refused AND appended to the event log carrying
both result digests, so an operator reconciling a ledger sees that it happened.
A refusal that is dropped on the floor is a refusal nobody can audit.

Deduplication is on the TASK, which is the same choice `pool_receipt` makes for
receipts and for the same reason: a peer can re-sign the same work with a new
timestamp and get a fresh digest for free, so only the task identity is stable.

A late delivery (the peer left, then a result arrived) is a SEPARATE link from a
double delivery. They are different facts about what the pool did and an
operator reconciling a ledger has to be able to tell them apart.

## The order of operations, and why it is that one

    classify -> check the tier admits the class -> WRITE THE DISPATCH DOWN
             -> send -> verify the hash -> verify the receipt -> record

The dispatch is written before the task leaves. A peer that takes work and never
answers therefore leaves an OUTSTANDING task rather than no record at all, and
outstanding is exactly what a withdrawal reads to name its `orphaned` set. That
is the difference between the ledger being a product surface and being a shape.

`peer-unreachable` is deliberately not terminal, and it is the one refusal that
is not. A peer that answered and was refused has SETTLED the task, so the entry
is terminal with its link. A peer that did not answer has settled nothing, and a
task whose fate is unknown must not be recorded as either done or dropped.

## The effect class is derived, never declared

A tier is a bound on what a member may be sent. If the operator could state a
task's class on the command line, the bound would be self-declared and the
ladder would mean nothing, so the class is computed from the composition's own
audited boundary.

The computation is the most conservative one that is defensible: **a composition
is dispatchable exactly when its audited G8 boundary is empty** - no externs, no
emissions, no capabilities, no compensations, no awaits, no capability registers
and no recovery surface. That is `EffectClass.pure`, and nothing weaker is
claimed.

The tempting shortcut is to read an extern's declared `class` and treat `pure`
as `EffectClass.pure`. That would be fail-OPEN on the one arrow that sends work
to a machine nobody trusts: `class: pure` in revl means the extern declares no
inverse, not that it has no effect, and the stdlib's own file writer is declared
`pure` while it writes to a file.

The cost is stated rather than hidden: **only boundary-free compositions are
dispatchable today**, and the `replayable` and `durable` tiers therefore admit
nothing through this dispatcher. Classifying a composition WITH a boundary is
the work that unlocks them. Until it exists, a tier with no classifier reachable
to it grants no authority by accident, which is the right direction for the
gap to fail in.

## The channel, and what it is not

Newline-delimited JSON over TCP, loopback by default, one task per connection,
with a bounded read so a far side that never sends a newline cannot make the
near side grow without limit.

A non-loopback bind is refused unless `--allow-remote` is passed, because the
channel carries no transport security. Every record on it is signed, so nothing
on the wire can be forged or altered undetected; nothing on it is secret, and
the artifact source crosses in the clear. A confidential cross-machine channel
is roadmap item 118's mTLS work (`revl deploy`, issue #79), whose pinned host
key, bundle staging and far-side runner are built; this module is deliberately
its caller rather than a second implementation of it.

**What the tests do not reach, stated rather than implied.** The two ends in
every test are two OS processes on one machine over loopback, not two machines.
What a second machine adds is the network. Nothing here proves anything about a
hostile network; it proves that every record crossing the channel is checked
before it is believed, which is the property that does not change with the
distance.

## The two runners

The peer's runner is named in the signed task and an unlisted one is refused
(`unknown-runner`) rather than defaulted, so a runner added on one side and not
the other is inadmissible everywhere.

* **`test-py`** runs the artifact's own declared tests. It needs only the revl
  frontend, so any machine with revl on it can be a peer.
* **`run-once-py`** boots the composition, tears it down LIFO and proves no
  residue. It needs a cordis-py runtime ON THE PEER, which is the peer's
  business: a peer without one answers with the runtime's own message and a
  nonzero exit code rather than pretending to have run it.

The result record is `{runner, exit_code, stdout, stderr}` and the peer's own
workspace path is replaced in every line before it is signed. A receipt is a
signed record that leaves the peer's machine, so a local absolute path in it is
both a reproducibility problem - the digest would differ per run for the same
work - and a needless disclosure about the peer.

## A flag that is ignored is not implemented

`revl run` carries thirteen flags the LOCAL runner honours: `--backend`,
`--config`, `--env`, `--policy`, `--watch`, `--record`, `--estop-latch`,
`--wal`, `--trace`, `--withdraw`, `--plan`, `--placement` and `--once`. None of
them can cross to a pool member, so `--pool private` refuses any of them by name
on `unsupported-with-pool` instead of dispatching work somewhere the flag does
not reach. What the peer does with the artifact is `--pool-runner`, which is the
peer's side of the decision and not the operator's.

The list is derived from the parser in a test rather than hand-maintained: a
flag added to `revl run` later and classified neither as local-only nor as part
of the pool surface fails that test, because being silently dropped by a pool
dispatch is exactly the failure the refusal exists to prevent.

## What this does not close

1. **Promotion has no CLI verb.** `peer_pool.promote` recounts evidence from
   `(receipt, attestation)` pairs, and the ledger now stores exactly those pairs
   for every delivered task, so the input exists. What is missing is the verb
   and, before it would mean anything, tiers above `probation` in a charter
   `pool init` can write - and a classifier that can prove a composition
   dispatchable at one of them. Building the verb first would ship a promotion
   to a tier nothing can be sent at.
2. **Liveness in `pool status`** (item 550's open point 6). A member's row now
   carries what it OWES (`outstanding=N`), which is what an operator deciding
   whether to withdraw a peer needs beside what it holds. Reachability is still
   not shown; `src/revl/liveness.py` is the machinery to read from.
3. **A multi-file artifact.** A task pins ONE artifact by hash. Two files are
   refused on `multi-file-artifact` rather than silently hashing the first; a
   bundle digest is what that needs.
4. **Concurrency.** The ledger is a JSON file with no concurrency control, the
   same limit the roster carries and for the same assumed deployment: one
   operator writing.

## Self-host

**No port, and the answer is no rather than deferred.** `selfhost/` is the
compiler - lexer, parser, checker, lower and six emitters - and its oracles
compare emitted bytes and reference diagnostics. This module emits nothing and
diagnoses nothing: it is operator tooling over signed records and a socket,
carries no host-ref extern form, no extern body handling and no compiler
behaviour at all. Checked against the self-host sources directly rather than
through the oracles, which is the same answer items 422 and 455 recorded for the
same reason.
