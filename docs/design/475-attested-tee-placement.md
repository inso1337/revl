# 475: Attested TEE placement

Design note for roadmap item 475 (issue #827). It records what the tree already
enforces, the gap the exit criterion names, the slice that lands with this note,
and the parts that stay design only until they are scoped on their own.

The item asks for a placement that requires an attested trusted execution
environment: `requires attested_tee`, a region, and `outbound_network =
forbidden`. A worker proves by remote attestation that it runs an approved
bundle inside a permitted enclave or sandbox, and the composition receives only
signed results and receipts. A host that cannot produce a valid attestation
simply cannot satisfy the placement.

## The spike: what the tree already enforces, and where it stops

The substrate is the verifiable private peer pool of item 461
(`docs/design/461-verifiable-private-peer-pool.md`), whose code lives in
`src/revl/peer_offer.py`. Read directly, it is:

* `PeerOffer` (`peer_offer.py:187`), a peer's signed advertisement: `peer_id`,
  an `Attestation` facet (`peer_offer.py:159`) and a `grant_ceiling`.
* `sign_offer` / `verify_offer` (`peer_offer.py:253`, `:331`), an HMAC over the
  canonical body under a domain-separated prefix (`SIGN_DOMAIN`,
  `peer_offer.py:93`), so an offer signature never verifies as an `attest`
  attestation or a deploy receipt under a shared key.
* `PlacementSlot` (`peer_offer.py:364`), the requirements side: `trust_floor`,
  `regions`, `hardware`, `need`, `grant`, `budgets`. `attested_tee` is the
  requirement this item adds to it (see below).
* `offer_eligible` (`peer_offer.py:393`), the match. Its order is prove
  authenticity (`verify_offer`), then check facets, then check that the offer's
  `grant_ceiling` covers the grant the slot would hand over, computed with the
  same `cap_order.covers_set` the authority-monotonicity invariant uses.

The gap is one sentence. **Every fact `offer_eligible` reads from an offer is
ASSERTED by the peer and signed with the peer's own key.** The `Attestation`
facet carries `trust`, `region`, `hardware` and a `resource_offer`, and all of
it is bytes the peer chose. Whoever holds the offer key can claim any region,
any hardware, any trust level, and the signature will verify, because the
signature only proves the peer said so, not that it is so.

That is tolerable for a trust FLOOR, which is a policy statement about who may
be offered work. It is not tolerable for an attested placement, where the
requirement is a claim about WHAT is running and WHERE. `trust >= attested`
therefore does not mean "this peer is attested", it means "this peer wrote the
word attested", and a `regions` facet is a word too. Before this note the tree
had no term for a third-party proof, no place to put one, and no code that would
refuse an offer for lacking one. An `attested_tee` requirement was not even
expressible: `PlacementSlot.__init__` rejected the keyword outright.

## The slice that lands with this note

Two additions, both pure and offline, no grammar, no runtime and no formal-layer
change.

1. **A verifier, `src/revl/tee_attestation.py`.** It owns the vocabulary of a
   remote-attestation proof and the decision the vocabulary encodes.

   * `TeeRequirement` (`tee_attestation.py:243`) is the typed, checkable
     requirement. It names the approved `bundle` by canonical content hash, the
     set of acceptable `measurements` (one per permitted enclave build), the
     permitted `regions`, this placement's `nonce` (the freshness challenge it
     minted), `outbound_network` (only `forbidden` today) and `max_age_s`. Every
     one of those is CHECKED when the requirement is built: an empty permitted
     set, a bundle that is not a digest, a measurement that is not a digest, a
     missing challenge, a non-positive window and an unknown network posture all
     refuse at construction, so an unsatisfiable or vacuous requirement cannot
     be configured by accident. `outbound_network` is the interesting one: the
     only accepted value is `"forbidden"`, and the module refuses `"allowed"`
     because a requirement that asks for an open network is not the placement
     this item describes and silently honouring it would widen the very
     confinement the requirement exists to narrow.
   * `EnclaveEvidence` (`tee_attestation.py:316`) is the proof: `peer_id` (the
     peer it is about), `bundle` (the approved bundle's content hash),
     `measurement` (what is running inside the enclave), `region`,
     `outbound_network`, `nonce` (the requirement's challenge), and
     `issued_at` / `expires_at`. The signed record also carries a `key_id`
     naming the ATTESTER, added by `sign_evidence` rather than held by the
     dataclass. `sign_evidence` / `verify_evidence`
     (`:367`, `:430`) prove authenticity over its own domain
     (`EVIDENCE_SIGN_DOMAIN`, `:101`).
   * `tee_admits` (`tee_attestation.py:461`) is the whole decision, and it is
     where the security content lives. It refuses when the proof is absent, not
     an object, unsigned, signed by a key the caller supplied no attester key
     for, or signed by the wrong key. Then it refuses when the proof is about
     another peer, when the bundle digest is not the approved one, when the
     measurement is not in the permitted set, when the region is not permitted
     or absent, when the challenge is not this placement's, when
     `outbound_network` is not forbidden, when the proof is expired, when it is
     stamped further in the future than a clock skew allows, and when the
     challenge has already been consumed.
   * **The load-bearing refusal.** `tee_admits` refuses when
     `attester_key == peer_key`: a peer that signs its own attestation has
     proven nothing an `attestation` facet did not already prove, and admitting
     it would make the whole mechanism a longer way to write "trust me". It
     refuses on the same ground when the caller supplies no peer key at all
     (`None`, the empty key, or a key of the wrong type), because a separation
     that cannot be checked is not a separation.
   * Replay is closed by a ledger of consumed challenges, and the ledger is
     mutated ON SUCCESS ONLY, so a refused probe does not let an attacker burn
     a legitimate peer's challenge.
   * `ResultReceipt` / `sign_result_receipt` / `receipt_admits`
     (`tee_attestation.py:569`, `:607`, `:672`) are the return path: the
     composition gets a signed receipt binding the result digest, the run, the
     bundle and the peer, under its own domain, so an enclave cannot be asked to
     sign for a result it did not produce and a receipt cannot be replayed as
     evidence.

2. **The seam in the pool, `src/revl/peer_offer.py`.** `PeerOffer` gains an
   optional `tee_proof` member (`peer_offer.py:205`) that is covered by the
   offer signature exactly as every other member is, `_validate_envelope`
   (`:270`) refuses a `tee_proof` that is not an object, and `PlacementSlot`
   gains `attested_tee: Optional[TeeRequirement]` (`peer_offer.py:387`).
   `offer_eligible` (`peer_offer.py:393`) takes `attester_key`, `tee_ledger` and
   `now` as keyword-only arguments and, when `slot.attested_tee` is set, refuses
   the offer unless `tee_admits` admits its proof. The probe runs immediately
   after `verify_offer`, so junk cannot burn a peer's challenge, and it only
   ever refuses, so a slot that demands nothing pays a single `is None` and
   nothing else changes for the existing pool.

The consequence is the exit criterion. A placement whose requirements include
an attested TEE admits a peer only on a proof that is authentic, about the
approved bundle, inside a permitted enclave and region, confined to a forbidden
outbound network, fresh, unanswered before, and vouched for by a key the peer
does not hold. Assertion cannot reach the accept path. `trust = attested` on the
offer is decoration, and `tests/test_tee_attestation.py` pins that specifically:
`test_an_asserted_trust_level_does_not_fill_an_attested_slot`.

## Verification

`tests/test_tee_attestation.py` (73 tests) pins the accept path, then every
refusal above, then the integration: an asserted trust level does not fill an
attested slot; a proof for another bundle, another region, a tampered
measurement, a replayed challenge, a missing ledger, a missing attester key and
a missing peer key are each refused; and re-signing the offer around a forged
proof does not help,
because the offer signature covers `tee_proof`. The tamper cases re-sign the
tampered body, so they demonstrate that the accept path is not reachable with a
forged proof, not merely that an unmodified forgery is caught. One test leaves
the pool and drives `lawful_retry.dispatch_on_loss` with an attested base slot:
the dispatcher configures no attester, so the replay REFUSES with an
`attestation:` reason rather than falling back to an unattested peer. Fail
closed, and the reason names why.

Objections worth recording:

* The algorithm is a symmetric MAC (`SIGN_ALG`, `tee_attestation.py:96`). The
  member exists and is validated so an asymmetric upgrade is additive, and the
  rest of this note does not change with it. Real quote formats are deferred.
* The verifier is pure and reads no clock of its own: `now` is passed in, so a
  test can pin expiry, staleness and future-dating exactly.
* `key_id` is a hash of the attester's key and discloses no key material, which
  is the same discipline `attest.key_id` already follows.

## The follow-up slice: the placement-file key (landed)

This note deliberately left one thing out: `requires attested_tee` was not a
grammar word in the placement surface of `src/revl/placement.py`. The requirement
landed here as a typed field on `PlacementSlot` plus the verifier that decides
it, and the file-level spelling was left to the next slice, because it changes a
surface other work is actively editing and because the decision was worth having
settled before the spelling was chosen.

That slice has since landed. A placement file spells the demand as

```toml
[processes.worker.attest]
requires = "attested_tee"
bundle = "<64 hex>"
measurements = ["<hex digest>"]
region = "eu-west"
outbound_network = "forbidden"
```

and it parses into this note's `TeeRequirement`, lands in
`PlacementSlot.attested_tee`, and defers its verdict to `tee_admits` through
`offer_eligible`, so the placement spelling and the typed requirement cannot
disagree. `run_placement` refuses a well-formed demand outright, because this
build places a process on the planning host and has no peer transport and no
attester root: an attested placement is refused rather than run unattested.
`docs/attested-tee-placement.md` is the surface reference and
`tests/test_placement_attested_tee.py` pins it.

## Deliberately deferred

* **Real quote formats.** TDX and SEV-SNP quote parsing, an attestation root and
  a key hierarchy that terminates outside the peer's control. The seam is the
  same; only `verify_evidence` grows.
* **Quorum.** One attester is trusted here. Requiring k-of-n attesters from
  disjoint roots is item 471, in flight.
* **Drift control.** A permitted measurement set that is updated, re-attested and
  provably current, rather than a static set baked into the requirement, is item
  469, in flight.
* **Result flow over the wire.** The module can sign and check a result receipt.
  Wiring receipts into the composition's call path, and refusing an unattested
  result there, is the same shape as the offer-side gate and belongs with it.
* **An attestation root in the retry dispatcher.** `lawful_retry` has no place to
  hold an attester key today, so a replay of an attested base slot refuses
  instead of choosing an attested peer. That is the safe direction and it is
  pinned by a test, but the replay path is only useful once the dispatcher is
  configured with a root to check against, which is the same "attestation root"
  question as the quote formats above.

## Relates to

* Item 461 (verifiable private peer pool): the substrate this extends.
  `src/revl/peer_offer.py` and `docs/design/461-verifiable-private-peer-pool.md`.
* Item 411 (sandbox placement): the placement side whose requirements this
  tightens.
* Item 471 (quorum) and item 469 (drift control): the two follow-ups above,
  both in flight, both referenced rather than touched.
