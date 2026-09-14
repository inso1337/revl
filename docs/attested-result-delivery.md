# Attested result delivery: one admission, one delivery

Roadmap item 475 (issue #827). The item has two halves. The first is the offer
side: which peer may run the bundle, decided by `tee_admits` against remote
attestation, spelled in a placement file as `requires attested_tee`
(`docs/attested-tee-placement.md`) and rooted in real quote formats
(`docs/tee-attestation-root.md`). The second is the sentence the item ends on:
"the composition receives only signed results and receipts."

This document is that second half. It is the call path a result arrives by.

## What was missing

`src/revl/tee_attestation.py` has had `ResultReceipt`, `sign_result_receipt` and
`receipt_admits` since the first slice, and `receipt_admits` is a careful gate:
it refuses a receipt verified with the peer's own offer key, a receipt for
another peer, another bundle, another challenge or another result, a stale or
future-dated one, and a replayed one.

Nothing called it. Until this slice `receipt_admits` had no caller anywhere in
`src/`, and that left three shapes of failure, all of them fail-OPEN:

1. **No delivery gate at all.** Admitting a peer and then accepting whatever came
   back was the default, because nothing in the admission produced an obligation
   to check the result.
2. **Re-supplied correlation.** `receipt_admits` takes `peer_id`, `bundle` and
   `nonce` from its caller. The delivery was therefore only as good as the
   caller's memory of what the admission decided; a caller passing a different
   run's challenge accepts a result the placement never authorized.
3. **A per-call ledger.** `receipt_admits` refuses when no delivery ledger is
   supplied, but a caller handing it a **fresh** ledger each call satisfies that
   check while permitting unlimited replay.

## The shape

An admission now produces an `AttestedRun`, and that object is the only way a
result enters.

```python
from revl.placement import admit_run_for_process

run, reason = admit_run_for_process(
    placement, "worker", offer,
    offer_key=peer_key, enclave_key=enclave_key,
    root=hardware_root, tee_ledger=ledger)
if run is None:
    refuse(reason)          # the peer was not admitted; there is no delivery path

accepted, reason = run.accept_result(receipt, result)
if not accepted:
    refuse(reason)          # the result is not this run's; it never enters
```

`run` exists only because an attestation admitted this peer for this bundle under
this challenge, and `accept_result` takes none of those three back from the
caller: it has no `peer_id`, `bundle`, `nonce`, `enclave_key`, `peer_key` or
`replay_ledger` parameter. The run also owns its delivery ledger, so the replay
check cannot be defeated by handing it a new one.

The same path exists at three tiers, and they are one implementation:

| Tier | Entry point | Returns |
|---|---|---|
| verifier | `tee_attestation.open_attested_run` | `(AttestedRun \| None, reason)` |
| peer pool | `peer_offer.offer_admission` | `(AttestedRun \| None, reason)` |
| placement file | `placement.admit_run_for_process` | `(AttestedRun \| None, reason)` |

The admission verdict is `tee_admits`, unchanged, and a refusal carries its reason
text verbatim, so `offer_eligible` and `offer_admission` cannot disagree about
whether a peer is admitted, and neither can `admit_peer_for_process` and
`admit_run_for_process`. A slot or a process that demands **no** attested TEE is
refused a run rather than given one that checks nothing: the delivery gate means
something only because an attestation decided what the result is bound to.

## Binding the receipt key to the attestation

A receipt is signed with the enclave's key, which the peer does not hold. That
raises the question the first slice left open: what makes the key the composition
checks against the key of the enclave that was **admitted**, rather than some
other key the operator provisioned?

`EnclaveEvidence` now carries an optional `receipt_key_id`: the fingerprint of the
key the enclave will sign its results with, stated by the attester. It rides in
the evidence body, so the attester's signature covers it, and on the hardware path
the quote's `report_data` digest covers it too. It is therefore an attested fact,
not a member the peer fills in. Rewriting it breaks the signature on the
development path and breaks the quote on the hardware path.

`open_attested_run` refuses when the fingerprint the attestation names is not the
key the composition holds, and `require_bound_receipt_key=True` (the default) also
refuses evidence that names no receipt key at all, because an unnamed key is
exactly the ambiguous case. Pass `False` for the pre-binding evidence shape; every
delivery verdict then carries `RECEIPT_KEY_UNBOUND_NOTE` in its own reason, the
same discipline `DEV_ROOT_NOTE` follows on the development verifier.

The member is additive. An evidence that names no receipt key has exactly the
bytes it had before the member existed, so no committed quote fixture changes
meaning.

## Failure direction

Every gate here refuses. Nothing in this slice makes a result admissible that was
not admissible before; it makes results that were previously accepted without any
check refusable.

| Case | Verdict |
|---|---|
| the composition holds no receiving key | refused at admission, before the worker runs |
| the receiving key is the peer's own offer key | refused at admission, before the worker runs |
| the attestation names a different receipt key | refused at admission |
| the attestation names no receipt key | refused at admission, unless asked for by name |
| `receipt_key_id` present but unreadable | refused by the envelope, never read as absent |
| `receipt_key_id` rewritten on a signed record | signature mismatch |
| `receipt_key_id` rewritten on a quoted record | the root refuses the quote |
| the admission refuses for any reason | no run, so there is no delivery path at all |
| the offer is admitted but fails a later slot gate | no run |
| the slot or process demands no attested TEE | no run |
| a result with no receipt, or a receipt that is not a record | refused |
| a receipt signed by the peer, or by another enclave | refused |
| a receipt covering a different result, run, peer or bundle | refused |
| a stale or future-dated receipt | refused |
| a second result for the same run | refused as a replay |
| a **refused** delivery | does **not** consume the run's one delivery |

The last row is the one refusal that must not over-refuse: if a junk receipt
burned the run, a hostile third party could deny an honest worker its result by
presenting junk first. The same asymmetry holds one level up, and is pinned: a run
refused on the composition's own misconfiguration (no receiving key, or the peer's
own key) does not burn the peer's challenge, because the peer did nothing wrong. A
run refused on the receipt-key binding does burn it, because the proof was valid
and the challenge is spent; the composition must issue a new one.

## Tests

`tests/test_tee_result_delivery.py` (49 tests): the accept path at the top and at
the bottom as non-vacuity controls, the admission-side refusals, the receipt-key
binding in both directions and on both roots, the delivery-side refusals, the
replay in both directions, the structural assertion that `accept_result` cannot be
told which run it is, and the whole path driven through `offer_admission` and
`admit_run_for_process`. `test_a_fresh_ledger_per_call_is_the_replay_hole_the_run_closes`
asserts both halves of failure 3 above in one place: the primitive replaying
without limit under a caller-supplied fresh ledger, and the run refusing.

## Still open on item 475

* **The vendor DER chain.** Intel's PCK certificate chain and AMD's KDS VCEK
  certificate in place of the one-hop `PlatformEndorsement`.
* **An attestation root in the `lawful_retry` dispatcher.** A replay of an
  attested base slot refuses rather than choosing an attested peer. That is the
  safe direction and it is pinned by a test.

## Relates to

* `docs/attested-tee-placement.md`: the placement-file spelling of the demand.
* `docs/tee-attestation-root.md`: the root the admission chains to.
* `docs/design/475-attested-tee-placement.md`: the design note of record.
