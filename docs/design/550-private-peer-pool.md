# Design: the private peer pool as an operable product (item 524, issue #1198)

Status: slice 1 landed; the receipt slice landed on top of it
([566-pool-execution-receipts.md](566-pool-execution-receipts.md)), which is
where promotion evidence is now recounted rather than believed. The pool
declaration, the join admission gate, the tier ladder, promotion on attested
evidence and withdrawal are in
`src/revl/peer_pool.py` with `revl pool init | request | join | status |
withdraw`. Dispatching work to a member over a wire is not here and is named
under "What is left" with how to measure it.

Related and read before designing this: `docs/design/480-verifiable-private-peer-pool.md`
(the three kernels this sits on), `src/revl/peer_offer.py`,
`src/revl/peer_authority.py`, `src/revl/lawful_retry.py`, `src/revl/attest.py`,
`src/revl/model_evidence.py` (item 517's signing discipline), and
`docs/design/540-shadow-promotion.md` (item 518, the authority diff as a
structural precondition).

## What was missing, precisely

revl's admission, effect-witnessing and delivery machinery exists to make one
property true: a component nobody must trust can be admitted into a system and
its effects accounted for. The deployment that exercises that end to end is
several independent operators running work for each other without trusting each
other's machines, and issue #1198 reports that this deployment is not operable.

Three kernels already decided the hard parts. `peer_offer` signs a peer's
advertisement and matches it against one placement slot. `peer_authority` bounds
what a delegate may receive. `lawful_retry` decides replay versus compensate
when a peer is lost. What no module owned was the pool: a named thing an
operator declares, a roster a peer joins, an authority view naming who may admit
and revoke, and a ledger that says what a peer leaving does and does not undo.
Without those, the guarantee has nowhere to be demonstrated except a test.

So this item is not new theory. It is the missing noun.

## The trust progression, and the failure direction of every arrow

The issue sketches a progression: discover a peer, verify its identity and
artifact, assess it, admit it to a restricted pool, send it only pure or
idempotent work, increase its authority after successful evidence, withdraw that
authority after drift or violation. Every arrow in that progression is a place
where authority increases, so every arrow states where it fails, and the
direction is the same one throughout: a peer whose evidence is missing gets the
authority of a peer with no evidence, never the authority of the peer whose
evidence passed.

| arrow | what must hold | failure direction | link |
|---|---|---|---|
| a peer is named | the operator holds a key for it, exchanged out of band | nothing the peer said is read at all | `unknown-peer` |
| an operator admits it | the admitting key fingerprint is in the charter's `admit_key_ids` | refused before any peer-supplied member is parsed | `admitting-authority` |
| identity is a key | the join and the offer inside it both MAC under the peer's key, and the offer's own `peer_id` is the one the join claims | refused; a relayed offer is not an identity | `join-signature`, `offer-signature`, `offer-identity` |
| the peer agreed to these terms | the join pins the charter by DIGEST | refused; a charter re-signed with different terms invalidates every join against the old ones | `pool-identity` |
| the request is fresh and used once | inside the charter's window, `(peer_id, nonce)` unspent | refused; a captured join is not a second admission | `stale-join`, `replayed-join` |
| the artifact is the admitted one | the join pins a digest the charter lists | refused | `artifact-digest` |
| the peer clears the floor | attested trust at or above the charter's floor | refused | `trust-floor` |
| it enters at the bottom | a join issues the entry tier and nothing else | refused; there is no argument a peer can put in a signed join that lands it higher | `entry-tier` |
| it holds only what the pool holds | the tier grant is a narrowing of the charter ceiling AND is covered by the peer's own advertised ceiling | refused, naming the widened caps and budgets | `grant-ceiling` |
| it is sent only work its tier admits | `work_admissible` keys on `lawful_retry.EffectClass` | refused; the entry tier is pure work only | (not a join refusal) |
| authority rises on evidence | the tier's declared count of receipts, attested by a key in `attest_key_ids` | the member STAYS WHERE IT WAS; it does not inherit the tier it asked for, and it is not demoted either | `promotion-evidence` |
| authority falls on withdrawal | the revoking key is in `revoke_key_ids` | refused; the member is not removed | `admitting-authority` |

Two more things the table is worth reading for.

The admit authority is checked before the join is parsed. That is deliberate and
it is tested: a caller with no admit authority gets the same refusal for a good
join and a bad one, so the gate cannot be used as an oracle for whether some
peer would have passed.

The authority view splits three ways. The key that may admit is not
automatically the key that may revoke, and neither is automatically the key
whose signature counts as promotion evidence. A single compromised admit key
cannot walk a peer up the ladder on its own.

## The ladder, and why it reuses the effect classification

A member's tier is a bound on what it may be SENT, and the bound is expressed in
`lawful_retry.EffectClass` rather than in a taxonomy of this module's own. That
is the whole point of item 524's warning about a second, weaker policy engine.

| tier | effect ceiling | how it is reached |
|---|---|---|
| `probation` | `pure` | admission |
| `replayable` | `idempotent-external` | attested receipts, count declared by the charter |
| `durable` | `witnessed` | attested receipts, count declared by the charter |

Two effect classes are reachable from no rung at all, and that is a refusal
rather than an omission. `deferred-irreversible` is resolvable only at a trusted
commit authority and is never speculatively issued to a pool peer.
`secret-bearing` needs a trusted, local or hardware-attested host. A pool peer is
by construction a machine the operator does not trust, so climbing this ladder
never reaches either, and `work_admissible` says so in the refusal text. An
effect class nothing here has seen is refused rather than defaulted, so adding
one to `lawful_retry` and not here leaves it inadmissible everywhere, which is
the fail-closed direction.

## The authority diff is a precondition, not a stage

Item 518 (shadow promotion) settled the shape for a promotion whose safety rests
on a diff: make the diff a precondition of issuing the thing rather than a stage
the thing passes through, so there is no ordering in which the thing exists and
the diff has not run. It proved that structurally, with an AST walk over its own
source asserting the evidence is absent downstream. Increasing a peer's
authority is the same move and gets the same treatment.

`_ceiling_precondition` is the only function that computes a tier grant, and it
returns either a grant or a refusal, never both and never a grant it has not
diffed. `_issue_membership` is the only construction site of a `Membership` and
it takes the grant it is handed; it computes nothing.
`tests/test_peer_pool_admission.py` walks this module's AST and asserts three
facts: `Membership` is constructed in exactly one function, `_tier_grant` is
called from exactly one function, and every function that calls
`_issue_membership` also calls `_ceiling_precondition`. A behavioural test could
only show the orderings it happened to try, so the structure is asserted
directly.

Both entry and promotion diff against the CHARTER CEILING, never against the
tier below. A ladder that measured each rung against the previous one would let
an error in one rung raise the ceiling for every rung above it. A ladder measured
against the charter cannot, because the charter is the same fixed signed record
at every rung. This is tested: a charter that declares a tier wider than its own
ceiling admits nobody to it, and the rung below does not launder it.

## Withdrawal is three words, not one

Item 546 (evolvable layer state) settled that accumulated state has no inverse:
a revert RESTORES some layers and only COMPENSATES others, and "rolled back"
must not be one word covering both. A peer's history is exactly that shape, so
`Withdrawal` reports three disjoint sets and never a single boolean.

**`revoked`** is restored, and it really is an inverse. The caps and budgets the
peer held, it holds no longer; after a withdrawal its authority is exactly the
authority of a peer that never joined.

**`retained`** has no inverse and is not compensated either. The receipts it
delivered, the evidence it accumulated, the effects already witnessed and the
highest tier it reached are observations. Withdrawing the peer does not
un-observe the work it did. They stay in the ledger, and they do NOT re-confer
authority: a re-admitted identity re-enters at the entry tier with its evidence
count at zero, because the evidence is a record of what happened and not a
credential that survives its holder's removal. That is the one place where
"restore the previous state" would be the tempting and wrong answer, and it is
tested by name.

**`orphaned`** is neither. Work dispatched to the peer and not delivered when it
left is named and handed to `lawful_retry.dispatch_on_loss`, which is the module
that already owns replay-versus-compensate. Duplicating that decision here would
be the second, weaker policy engine item 524 exists to avoid.

The ledger is append-only for the same reason. A withdrawal is an appended
event, not a deletion of the admission, because a withdrawn peer and a peer that
never joined must not render the same: the first one ran work whose effects are
still in the world.

## The signing discipline, and what a signature here does not prove

Every record is HMAC-SHA256 over canonical JSON with a per-protocol domain
prefix, the construction `attest` and `peer_offer` already use. Item 272 exists
because three components in one wave independently hand-rolled the same crypto,
so nothing new is built here: `_canonical_bytes` and `key_id` come from
`attest`, and the only addition is three domains.

The covered set is derived from the record, not from a field list. That is item
517's discipline and it was written after a real finding in this tree: a signed
receipt carried a `key_id` appended to its body AFTER the MAC was taken over a
hand-written list of names, so the member every reader used to CHOOSE a
verification key was the one member the signature did not cover. `_mac` filters
the body instead, so a member added to a record is covered the day it is added,
and a test asserts structurally that it is a comprehension over the body rather
than a tuple of names.

Three protocols get three domains (`revl.pool-charter/v1`, `revl.pool-join/v1`,
`revl.pool-receipt/v1`), all distinct from `attest`'s, `peer_offer`'s and the
deploy receipt's. Without that, one protocol's signature would verify as
another's under a shared key. Tested both ways: a peer offer does not verify as
a join, and a charter body MAC'd under `attest`'s domain does not verify as a
charter.

**What this does not prove.** An HMAC gives AUTHENTICATION under a shared key.
It does not give non-repudiation: the operator verifying a peer's join holds the
same key that signs it and could have produced it. A private pool of operators
who exchanged keys out of band is the deployment where that is acceptable, and
it is why this is the private pool and not an open one. It also means a
compromised operator key forges joins for every peer whose key it holds.
Asymmetric peer identity, which makes a join provable to a third party and lets
an operator hold only public keys, landed on top of this slice under issue #1278
and is designed in
[design/555-asymmetric-peer-identity.md](555-asymmetric-peer-identity.md). The
HMAC path described here remains as the `shared-key` identity mode.

Nor does a signature prove good behaviour. It proves provenance. What a peer may
RECEIVE is bounded here; what a malicious peer DOES with received authority is
the sandbox and seam's job, and the #475 hostile-wire work is the consumer that
keeps that honest. `peer_offer`'s design already says this and nothing here
weakens it.

## Refusals are links, not guarantee codes

Every refusal names one of eighteen lowercase links and none of them is a
`revl.diagnostics.GUARANTEES` entry. That follows `revl.deploy`, which draws the
same line for host admission: a pool refusal is a decision about a DEPLOYMENT,
not a verdict about a PROGRAM. The register of guarantee codes is the register
of what the checker refuses source for, and every entry in it needs a reproducer
under `examples/rejections/`. There is no program to write for "this peer's join
nonce was already spent". A test asserts the links stay lowercase, stay out of
`GUARANTEES`, and that the module never mentions the register.

Two tests keep the link set honest in both directions. An AST walk asserts every
`_refusal` call names a declared `LINK_` constant and that every name in
`REFUSAL_LINKS` is passed by some call, so a link cannot be declared and left
unreachable, which would read as a gate that exists. A second test asserts the
corpus plus the named tests together produce every link at RUNTIME, because
reachable in the source is weaker than reached in a run.

## Non-vacuity

Before this change the only check a peer faced was `peer_offer.verify_offer`.
There was no pool to join, so nothing refused a peer on pool grounds.

The corpus in `tests/test_peer_pool_admission.py` is 14 join requests. Every one
carries a peer offer that verifies under a key the pool holds, so every one is
admitted by that pre-existing check. Every one is refused by this gate, on 13
distinct named links. A control that differs from the corpus only in being well
formed is admitted by both.

    admitted by the pre-existing check   14 / 14
    refused by the gate                  14 / 14
    distinct links exercised             13
    control, admitted by both             1 / 1

The corpus entries are, in order: a peer the operator holds no key for; a join
edited after signing; a join naming another pool; a join naming another
charter's digest; a stale join; a replayed nonce; a withdrawn peer; an existing
member; a relayed offer signed by a different peer's key; an offer describing a
different peer; a peer below the trust floor; an unadmitted artifact digest; a
peer arguing for a higher tier inside its own signed body; and a peer whose own
advertised ceiling does not cover the entry grant.

## Scope: what this slice is, and what it is not

This is the pool declaration plus the gate for joining it. Nothing here opens a
socket. Running work on a member, moving bytes over a wire and delivering a
result are the machine boundary of roadmap item 118, and this module is
deliberately their caller's data model rather than a second implementation of
them. (This paragraph said "the #421 network seam" until item 524's receipt
slice; that reference was wrong, and
[566-pool-execution-receipts.md](566-pool-execution-receipts.md) says what the
prerequisite actually is.)

`revl pool` is operable today on one machine or two, with keys exchanged out of
band:

    revl pool init --dir ./pool --pool-id lab \
      --ceiling 'fs.read(path="/data")' \
      --entry-caps 'fs.read(path="/data/in")' \
      --artifact <digest> --key operator.key

    # on the peer, given the charter
    revl pool request --charter ./pool/charter.json --peer-id alpha \
      --artifact <digest> --ceiling 'fs.read(path="/data")' \
      --out join.json --key alpha.key

    # back on the operator
    revl pool join --dir ./pool --join join.json --peer-key alpha.key \
      --key operator.key
    revl pool status --dir ./pool
    revl pool withdraw --dir ./pool --peer alpha --reason "attestation drift" \
      --key operator.key

`pool status` needs no key. That is deliberate: an operator inspects membership
without touching the secret that admits, so the roster is safe to put in a
dashboard or a health check.

## What is left, and how to measure each piece

Item 524's exit test is two machines joining a pool, one running a task the
other admits, the result verified by hash and receipt, and a peer leaving
mid-task leaving the ledger in a stated state. This slice lands the first and
last of those. What remains:

1. **Dispatch over the wire.** Handing an admitted member a unit of work needs
   a machine boundary. It does NOT need "the #421 network seam", which was a
   wrong reference: roadmap item 421 is a capability and codegen audit. The
   genuine prerequisite is roadmap item 118 (`revl deploy`, issue #79), whose
   cross-machine channel, pinned host key, bundle staging, far-side
   `deploy-admit` runner and signed COMMIT receipt are all built; what is
   missing is the dispatcher that turns a unit of work into a request.
   `deploy.VIA_PEER` exists to be refused by name, with reason
   `peer-pool-unavailable`. Measured by: two processes on two hosts, one `pool
   join`, one task dispatched, the result returned. Today the pool can be stood
   up and joined across two machines by copying three files.
2. **One-result delivery with a ledger.** `Roster.outstanding` is the shape a
   delivery ledger plugs into, and it is populated by the caller rather than by
   this module, because the thing that dispatches work is the thing that knows
   what is outstanding. Measured by: a delivered-twice attempt is refused or
   recorded, never silently absorbed. `tee_attestation`'s admission ledger is
   the existing consumer to extend rather than duplicate.
3. **Signed execution receipts feeding the evidence count.** DONE, under item
   524's receipt slice. `Membership.evidence` is derived from
   `evidence_digests`, `promote` recounts from `(receipt, attestation)` pairs
   and no longer takes a count or a key list from a caller, and a promotion
   cites the receipt digests it counted. See
   [566-pool-execution-receipts.md](566-pool-execution-receipts.md).
4. **Asymmetric peer identity.** DONE, under issue #1278. The operator's pool
   directory holds only public keys, and a third party given a join and a public
   key reaches the same verdict the operator did. See
   [design/555-asymmetric-peer-identity.md](555-asymmetric-peer-identity.md) for
   the key lifecycle, what the signature binds member by member, and what the
   non-repudiation claim rests on.
5. **`run --pool private`.** Running a composition against the pool is the
   product surface the item names and it depends on 1 and 2. Measured by: a
   program with a `pure` component runs on a `probation` member end to end.
6. **Liveness and health in `pool status`.** The roster shows membership, not
   reachability. `src/revl/liveness.py` is the existing machinery to read from.

## Adversarial review

**A1. A peer argues for a higher tier.** It adds a `tier` member to its join
body before signing, so the signature is valid and the envelope accepts the
extra member. Refused on `entry-tier`: a join issues the entry tier and the
requested value is only ever compared, never used. Pinned by the corpus entry
`asks-for-a-tier`.

**A2. A relayed offer.** A peer signs its own join correctly but embeds a peer
offer for its own `peer_id` that a different key signed, so the identity the
pool would grant to is vouched for by a key that is not the peer's. Refused on
`offer-signature`, because the offer is verified under the key the operator
holds for the JOINING peer, not under whatever key happens to verify it. Pinned
by `relayed-offer`.

**A3. An offer for someone else.** The offer verifies under the joining peer's
key but describes a different peer, so the grant would be issued to one identity
on another's advertised terms, including its ceiling. Refused on
`offer-identity`. Pinned by `offer-for-someone-else`.

**A4. Charter substitution.** An operator re-signs the charter with a wider
ceiling under the same `pool_id` and replays a peer's old join against it. The
join pins the charter DIGEST, not the name, so it stops verifying against the new
terms. Refused on `pool-identity`. Pinned by `another-charter`.

**A5. Join replay.** A captured join record is submitted again, or twice
concurrently. Refused on `replayed-join` from the roster's spent-nonce ledger,
and on `duplicate-member` if the nonce ledger were somehow bypassed, so there
are two independent refusals on the path to a second admission. A refused join
does NOT spend the nonce, so fixing the cause of a refusal and retrying the same
request works, which is also tested.

**A6. The gate as an oracle.** A caller with no admit authority submits joins to
learn which peers would be admitted. The admit-authority check runs before the
join is parsed and the refusal text does not depend on the join, so a good join
and a bad one are indistinguishable to that caller. Pinned by
`test_an_operator_without_admit_authority_cannot_use_the_gate_as_an_oracle`.

**A7. A widening charter.** An operator declares a tier grant wider than the
pool's own ceiling, betting the check only looks at the peer. Refused on
`grant-ceiling` at entry and at every promotion, naming the widened caps and
budgets, via `peer_authority.grant_widenings`, the same fold spawn attenuation
runs. A budget the pool does not hold at all widens by any positive amount,
fail-closed, which is `peer_authority`'s own rule and not a second copy of it.

**A8. Self-attested promotion.** A peer, or an operator holding only admit
authority, presents evidence signed with its own key. Refused on
`promotion-evidence` because the evidence key fingerprints must be a subset of
the charter's `attest_key_ids`. The member stays where it was.

**A9. Re-admission to recover a tier.** A withdrawn peer joins again expecting
its accumulated evidence to put it back where it was. It is refused outright on
`revoked-peer` while its identity is revoked, and if an operator clears the
revocation it re-enters at the entry tier with `evidence` at zero. Pinned by
`test_accumulated_evidence_does_not_survive_its_holder`.

**A10. A hostile record crashing the gate.** The wire is hostile, so a
peer-controlled record must not be able to turn a refusal into an exception. The
gate returns a receipt and never raises, every parse is guarded, and `_refusal`
carries no internal assertion for exactly this reason: an assertion on the
refusal path would fire on the hostile input the refusal exists for. Pinned by a
parametrized hostile-input test over ten malformed records.

## Honest limits

* An HMAC authenticates under a shared key. It does not prove authorship to a
  third party and a compromised operator key forges joins for every peer whose
  key it holds. That is why a pool should run in `asymmetric` identity mode;
  the `shared-key` mode kept here carries this limit unchanged.
* The gate bounds what a peer may RECEIVE. It says nothing about what a peer
  DOES with what it received; that is the sandbox and seam's problem, and
  `peer_offer`'s design says the same thing about the same boundary.
* `Membership.evidence` WAS supplied by the caller. Item 524's receipt slice
  closed that: it is now `len(evidence_digests)`, recounted by
  `pool_receipt.count_evidence` from receipts the pool verified, and there is
  no parameter through which a count can be stated.
* The roster is a JSON file with no concurrency control. Two operators admitting
  at once on a shared directory would race. A single-writer operator is the
  assumed deployment and a durable multi-writer roster is not designed here.
* `Roster.outstanding` is populated by whatever dispatches work. With no
  dispatcher wired in, a withdrawal today reports an empty `orphaned` set on a
  real deployment, which is honest but not yet load-bearing.
