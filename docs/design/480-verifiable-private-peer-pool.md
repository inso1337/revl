# Design: verifiable private peer pool (#480)

Status: all three primitives landed as language-level kernels.

* Primitive 3 (authority-monotonicity invariant): `src/revl/peer_authority.py`
  + `tests/test_peer_authority_monotonicity.py`.
* Primitive 1 (signed peer offer / attestation + placement matching):
  `src/revl/peer_offer.py` + `tests/test_peer_offer.py`.
* Primitive 2 (lawful-retry dispatcher): `src/revl/lawful_retry.py` +
  `tests/test_lawful_retry.py`.

What remains is NOT a language primitive: wiring these kernels to a LIVE dispatch
(constructing the chain and candidate offers from real placement + the network
transport) needs #421 (F8 network seam) and #107 (seam transport), and the
hostile-wire TCK #475 is the adversarial consumer that keeps the still-hostile
wire honest. The primitives are proven in isolation here — the signing/matching
math and the dispatch decision table — so the transport work that consumes them
rides on a verified kernel. All three are Python-only: they touch no lexer,
typecheck, selfhost, or rust emit, so no gate-crate regen.

## Scope: three primitives, not a P2P monolith

The design intake (Proposal A) was a full peer-to-peer execution pool:
transport, discovery, a marketplace, gossip, reputation, economic settlement, a
"verifiable federated research agent" on top. That is rejected as a work item.
The transport/discovery/marketplace/gossip/reputation/settlement layers are
infrastructure, not language features, and open federation is out of scope for
now (the same deliberate limit the session-WAL scope already draws). The
federated research agent stays a NORTH-STAR demo, not a milestone.

What #480 extracts are the three primitives that are genuinely new and
genuinely revl-shaped, scoped to a PRIVATE pool riding the substrate revl
already has (content-addressed bundles, deploy PREPARE/COMMIT participants,
lease gates):

1. **Signed peer capability + attestation advertisement**, matched against
   placement constraints (`trust >= verified`, region, hardware, resource
   offer). revl has trust in deploy maps but not signed peer offers or
   attestation matching. NEW.
2. **Lawful-retry dispatcher** keyed on effect class. revl already HAS the
   classification (extern tiers + compensation classes); the new part is the
   dispatcher that consults it to decide replay-vs-compensate on peer loss.
3. **Authority-monotonicity invariant** (formal, G-theorem candidate): a peer
   may receive no more authority, data, time, money, or retry budget than the
   delegating composition possesses.

Explicitly out of scope until the above is dependable: open marketplace, gossip
discovery, reputation, economic settlement, public federation.

## The one thing to get right

All three primitives are the same statement seen from three sides: **a grant
handed to a peer is answerable to the authority of whoever handed it over.** The
attestation is what a peer must PROVE to be eligible for a grant; the dispatcher
is what may be RE-HANDED to a peer (or compensated) when a peer is lost; the
monotonicity invariant is the STATIC bound both are measured against. Get the
invariant right first and it becomes the spine the other two attach to, which is
why it is Slice 1 and the rest is design-ahead.

---

## Primitive 3 first: authority-monotonicity (Slice 1, landed)

### Statement

Model a distributed execution as a **delegation/retry chain**: an ordered
sequence of holders. Element 0 is the delegating composition's own authority.
Each later element is a peer that received a delegated grant, or a retry that
reissued an earlier attempt. The invariant:

> For every edge `(delegator -> delegate)` in the chain, the delegate's grant is
> covered by the delegator's: the delegate receives no capability, no wider
> resource cone, no larger time or data ceiling, no more money, and no larger
> retry budget than the delegator holds.

Equivalently: authority is **monotone non-increasing** along every chain. It may
narrow (hand down less), never widen (grant a boundary the delegator does not
hold). Because the capability order `covers` is transitive, edge-by-edge
coverage makes every grant in the chain covered by the root, so the composition
can never be made to reach, spend, or wait for more than it declared, no matter
how many peers the work crosses.

### Why it reuses `cap_order`, and adds no algebra

revl already owns this order. `cap_order.covers(a, b)` is "same token, and `b`
narrows every parameter `a` binds"; `cap_order.covers_set(held, reach)` returns
exactly the reach elements no held element covers, that is, the widenings. The
spawn-attenuation fold (`lower._check_spawn_attenuation`, item 66/294) already
applies that relation to ONE boundary: a spawned child's capabilities must be
covered by its spawner's. A delegation/retry chain is the general case of the
same idea, an ordered sequence of holders each covered by the one before. So the
invariant is a thin walk over `covers_set`, holder by holder. No new capability
algebra is introduced; one definition of `covers` is the representation mandate
of docs/design/294-capability-partial-order.md, and this obeys it.

### The two dimensions, and why budgets sit beside caps

#480 names five monotone quantities: authority, data, time, money, retry budget.
Three are already capability parameters `cap_order` orders:

| #480 quantity | how it is spelled | order used |
|---|---|---|
| authority / reach | token + `path=`/`host=`/`table=` cone | `covers` clause 1 + resource params |
| data | `size=` / `bytes=` byte ceiling | `covers` ceiling `<=` |
| time | `time=` duration ceiling | `covers` ceiling `<=` |

So a grant written as capability strings has authority, data, and time
monotonicity decided by `covers` for free, ceilings included: a wider ceiling
downstream is a widening exactly as `_param_leq` already says.

The remaining two, **money** and **retry budget**, are NOT in `cap_order`'s
closed registry, and Slice 1 deliberately does not add them there. Adding a
registry row changes the capability grammar and the emitted-IR/digest surface
(and would pull the gate crates with it). This invariant does not need that.
Money and retry budget are plain non-negative scalar allowances, so they ride a
separate `budgets` map on each grant and obey the same monotone rule:
non-increasing along the chain, fail-closed. This keeps Slice 1 self-contained:
it touches no lexer, no typecheck, no selfhost, no rust emit, so no gate-crate
regen.

### Fail-closed rules (each is a test)

* A budget present downstream but ABSENT upstream widens: you cannot hand down a
  budget you were never shown to hold (held reported as `None`). This mirrors
  `cap_order`'s own "a dropped parameter widens".
* A granted budget strictly greater than the held budget widens. A grant of 0
  never widens (it hands down nothing).
* A negative budget is refused outright (a budget is a non-negative allowance).
* An unparseable or unregistered capability spelling raises `cap_order.CapError`
  rather than being skipped; the boolean `chain_is_monotone` does not swallow it
  into a `True`.
* A retry reissue is an ordinary later hop and is held to the SAME rule as a
  fresh delegation, so a dispatcher cannot launder a wider grant through a
  "retry".

### Surface (landed)

`src/revl/peer_authority.py`:

* `Grant(holder, caps, budgets)` - one holder and the authority it holds.
* `grant_widenings(delegator, delegate) -> (caps, budgets)` - the structured diff
  of what the delegate receives beyond the delegator; both empty iff monotone.
* `check_hop(index, delegator, delegate)` - refuse one edge; raises
  `AuthorityWidening` naming the offending caps/budgets, else returns None.
* `check_delegation_chain(chain)` - walk the chain edge by edge; raise at the
  first widening. Empty/single chains are vacuously monotone.
* `chain_is_monotone(chain) -> bool` - boolean form for a dispatcher that wants
  to SKIP a widening peer rather than raise (fail-closed on malformed input).
* `AuthorityWidening` - carries `index`, `holder`, `delegator`, `caps`,
  `budgets` so a caller/audit view reads the offending hop without parsing prose.

---

## Primitive 1: signed peer capability + attestation advertisement (landed)

Landed as `src/revl/peer_offer.py`. `PeerOffer`/`Attestation`/`ResourceOffer`
model the record; `sign_offer`/`verify_offer` are the hmac round-trip (borrowing
`attest._canonical_bytes` + `attest.key_id`, with the distinct
`revl.peer-offer/v1` MAC domain); `PlacementSlot` + `offer_eligible` are the
three-gate match (signature, attested facets, ceiling-covers-grant). The section
below is the design it implements.


### What a peer advertises

A peer offer is a signed record binding a peer identity to what it is willing to
run and what it can prove about itself:

```
PeerOffer {
  peer_id,                     # a stable public identity
  attestation {                # what the peer proves, not merely claims
    trust: verified | attested | local,
    region, hardware,          # placement facets matched against constraints
    resource_offer {           # cpu, memory, time it will lend
      cores, memory_bytes, max_time
    }
  },
  grant_ceiling: [Cap...],     # the MOST authority the peer will ever accept
  signature                    # over the canonical bytes of the whole record
}
```

The signature reuses `attest.py` wholesale: `attest.canonical_hash` over a
sort-keyed, separator-stable serialization, and the `hmac` keyed-signature
model, with a non-secret `key_id` fingerprint so a verifier can confirm WHICH
key signed without seeing it. `attest` already signs "this exact composition
passed the gate"; a PeerOffer signs "this exact peer offers this exact
attestation and grant ceiling". Same primitive, different payload. No new crypto
dependency (`attest` is deliberately stdlib-only).

### Matching against placement constraints

A composition's placement already expresses `trust >= verified`, region,
hardware, and resource needs (`placement.py`, item 55/56). Attestation matching
is: an offer is ELIGIBLE for a placement slot iff (a) its signature verifies,
(b) its attested facets satisfy the slot's constraints (`trust` at or above the
required floor, region/hardware in the allowed set, resource offer at or above
the need), and (c) its `grant_ceiling` COVERS the grant the slot would hand it,
under the very `cap_order.covers_set` Slice 1 uses. Point (c) is the seam
between Primitive 1 and Primitive 3: the dispatcher never offers a peer a grant
the peer's own advertised ceiling does not cover, and the monotonicity invariant
guarantees that ceiling is itself covered by the delegating composition. A
peer's offer is trusted input at the level of the placement file, no more: the
signature proves provenance, not good behavior, and the wire is still hostile
(the #475 TCK is the adversarial-wire consumer).

Depends on #421 (network seam: without it there is no remote peer to dispatch
to) and #107 (seam transport).

---

## Primitive 2: lawful-retry dispatcher (landed)

Landed as `src/revl/lawful_retry.py`. `EffectClass` is the classification it
consults (with `classify_extern` mapping the real extern facet names —
`secret`/`deferred`/`witnessed`/`idempotent` — to a class, tightest-first);
`dispatch_on_loss` returns a `Decision` per the table below, bracketed by
`peer_authority.check_delegation_chain` on the replay side and
`peer_offer.offer_eligible` on the peer-selection side. Transport (actually
handing the work over) is #421's job; this is the lawful DECISION. The section
below is the design it implements.


### The classification already exists

revl already classifies every effect by how it may be re-run. The dispatcher
does not invent a taxonomy; it CONSULTS the one in extern tiers and compensation
classes (docs/design/247-compensate.md, 243-witnessed-externs.md) to decide, on
peer loss, between REPLAY (re-issue the work to another peer) and COMPENSATE
(run the recorded inverse/offset). Keyed on effect class:

| effect class | on peer loss |
|---|---|
| pure | freely replayable to any eligible peer |
| idempotent-external | bounded retry (retry budget from Primitive 3) |
| witnessed / revertible | durable handoff + recovery coordination (WAL, `recovery.py`) |
| deferred-irreversible | only at a trusted commit authority; never speculatively re-issued |
| secret-bearing | trusted/local/attested peers only (never a bare `verified` offer) |

### Where monotonicity binds it

Every REPLAY is a new hop in the delegation/retry chain, so it passes through
`check_delegation_chain`: a retry may narrow the lost attempt's grant, never
widen it, and the retry budget itself is one of the monotone quantities, so an
unbounded retry storm is a static impossibility (the budget runs out down the
chain). Every COMPENSATE runs an inverse already recorded in the accumulator/WAL
(`recovery.py`'s roll-back path), which by construction touches only boundaries
the original crossing held, so it needs no new authority. The dispatcher is thus
BRACKETED by Primitive 3 on the replay side and by the existing witnessed/
compensate machinery on the compensate side. It adds coordination, not new
authority.

Depends on #421 and on Primitive 3 (landed).

---

## Adversarial review (second pass)

Each attack is against Slice 1 as landed, or against the design seam it rests on.

**A1. Launder a wide grant through a "retry".** A dispatcher re-issues lost work
to a fresh peer with MORE authority than the original attempt (say, a wider
`path=` cone to "recover faster"). Defense: a retry is an ordinary later hop in
the chain; `check_hop` holds it to the identical `covers`-based rule. Pinned by
`test_retry_reissue_held_to_the_same_rule`.

**A2. Re-acquire dropped authority mid-chain.** root holds `db.write`, agent
narrows it away, a downstream peer asks for `db.write` back, arguing it is
"within the root's authority". Defense: the chain is checked EDGE BY EDGE against
the immediate delegator, not against the root. agent cannot hand down what it no
longer holds. Pinned by
`test_grant_covered_by_root_but_not_immediate_delegator_still_refused`.

**A3. Widen by dropping a parameter.** A peer asks for bare `fs.read` under a
delegator holding only `fs.read(path="/data")`, betting the check compares
tokens and ignores the missing `path=`. Defense: `covers` clause 2 treats a
dropped resource parameter as strictly wider (a bare token tops its cone), so
`covers_set` reports it. Pinned by `test_dropping_a_resource_parameter_widens`.

**A4. Smuggle a budget the delegator never declared.** A peer requests
`money=100` where the delegator's grant names no money budget at all, betting an
absent key reads as "unbounded". Defense: fail-closed, an absent budget is unheld
(0), so any positive grant of it widens. Pinned by
`test_money_budget_absent_upstream_widens_fail_closed`. The same reasoning
refuses a negative budget outright (`test_negative_budget_refused`).

**A5. Time/data widening hidden in a ceiling.** A peer asks for
`size="10MB"` under a delegator holding `size="1MB"`, betting the check only
looks at the token and cone, not the byte ceiling. Defense: `covers` orders
ceilings with `<=`, so a larger ceiling is a widening. Pinned by
`test_larger_data_ceiling_widens` and `test_larger_time_ceiling_widens`.

**A6. Malformed spelling read as vacuously safe.** A grant carries an
unregistered parameter (`fs.read(bogus=1)`), betting the parser skips it and the
hop reads as monotone. Defense: `parsed_caps` raises `cap_order.CapError`;
`chain_is_monotone` propagates it rather than returning `True`. Pinned by
`test_malformed_capability_spelling_raises_not_admits`.

**A7. Forged attestation (design, Primitive 1).** A peer advertises
`trust: verified` it did not earn. Defense: the offer is signed; the signature
verifies provenance against a known `key_id`, and matching happens only on a
verified signature. What the signature does NOT prove is good runtime behavior,
so the wire stays hostile and the #475 TCK is the consumer that keeps it honest.
This is called out, not hand-waved: the invariant bounds what a peer may
RECEIVE, not what a malicious peer may DO with received authority, which is the
sandbox/seam's job.

**A8. Ceiling vs resource confusion in the diff.** Could a ceiling parameter
surface as a spurious capability widening (or vice versa)? Defense: this module
does not split ceilings itself; it defers entirely to `cap_order.covers`, which
already handles resource and ceiling parameters in one relation, and to the
separate `budgets` map for the two non-registry scalars. There is no second copy
of the split to drift, which is the whole reason to reuse `cap_order`.

### Honest limits (what Slice 1 does NOT claim)

* It bounds RECEIPT of authority, not USE. A peer that receives a narrow grant
  and then misbehaves within it is the sandbox/seam's problem, not this
  invariant's.
* Money and retry budget are checked as opaque scalars, not as a currency or a
  real rate limiter. The invariant proves "no more than the delegator held"; it
  does not meter spending, which is a runtime concern for the dispatcher.
* The chain is an explicit data structure here. Wiring it to a REAL dispatch
  (constructing the chain from live placement + peer offers) needs #421 and is
  Primitive 2's job. Slice 1 is the kernel both other primitives measure against,
  proven in isolation so the measurement is trustworthy before anything depends
  on it.
