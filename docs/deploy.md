# `revl deploy` — attested admission, correlated seams, coordinated rollback

Roadmap item 118, **Slice 1: single host / local multiprocess.** The design is
`docs/design/118-revl-deploy.md`; this page is what actually landed and how to
use it. The code is `src/revl/deploy.py`, the participant runner is
`src/revl/_deploy_participant.py`, and the seam changes are in
`backends/python/bridge.py`.

Slice 1 deliberately does not build the cross-machine half. There is no SSH or
container launch, no replicated WAL, and no quorum coordinator; every seam is a
local process seam over the item-56 bridge. What Slice 1 does build is the part
that is honest on today's tree and that the cross-machine half will reuse whole.

## What it guarantees, and what it does not

**It guarantees the chain.** A component admitted on a receiving process is
byte-for-byte the artifact whose source, IR, capability policy and runtime
evidence are bound by a signature that receiver independently trusts, checked
against the bytes on that receiver's own disk.

**It guarantees per-process residue-freedom.** Each participant applies its own
slice with its own LIFO rollback and its own no-residue proof, or reports what
it could not undo. That is `apply.py`'s property, per process, unchanged.

**It does not guarantee cross-process atomicity as one theorem.** The rollback
theorem `apply` proves is an in-process property. A peer's inverses live in that
peer's memory and WAL; the conductor holds neither. So the cross-process step is
a coordinated protocol over per-process-atomic units, and a peer the conductor
cannot reach is reported `unresolved`, never `rolled-back`.

**It does not defend against a lying receiver.** A receiver that returns a
signed ACCEPT and then loads different bytes is telling a signed lie. The
receipt binds the artifact hash it claims plus its runtime versions, so the lie
is detectable and non-repudiable — not impossible. Hardware remote attestation
is out of scope.

## 1. The attestation chain

`chain_bindings(bundle)` hashes each link of a `.revlbundle` — the emitted
artifact per backend, `policy.json`, `components.lock`, `gauntlet.json`, and the
per-backend conformance cert — and `make_deploy_attestation` folds them into
item 127's signed `evidence_bindings` (item 290's hook). One signature commits
`source -> IR -> artifact -> policy -> evidence`.

```python
from revl import deploy

att = deploy.make_deploy_attestation("app.revlbundle", signing_key, signer="ci")
```

### Admission re-hashes on receive

The receiver holds a `TrustStore`: its verify keys, its revocation set, its
backend, its capability ceiling and its freshness TTL.

```python
trust = deploy.TrustStore(
    keys={key_id: verify_key},
    backend="python",
    capability_ceiling=frozenset({"smtp"}),
    evidence_ttl_seconds=7 * 24 * 3600,
)
receipt = deploy.admit("staged.revlbundle", trust=trust, attestation=att,
                       host_key=host_signing_key)
```

Every value on the right of every comparison is recomputed from the staged
bytes: the IR hash with `attest.canonical_hash` over `ir/ir.json` as staged, the
artifact digest with `artifact_digest` over `emitted/<backend>/` as staged, and
each other facet from its staged file. The attestation contributes exactly one
thing, the signed left-hand side.

A sender cannot short-circuit that by describing itself. `deploy.admit` never
reads a `backend`, `artifact_hash`, `artifact_sha256`, `emitted_hash` or
`ir_hash` member of the attestation, even a signed one — those names are listed
in `deploy.SELF_DECLARED_IGNORED` so the refusal is a stated property. An
attestation that truthfully describes bytes other than the ones in hand is
refused just the same, because the signature proves authorship, not that this
disk holds the artifact that was signed.

A refusal always names one chain link:

| link | when |
| --- | --- |
| `signer-untrusted` | `key_id` not in the trust store, or revoked |
| `signature` | the HMAC over the payload does not verify |
| `source-or-ir-hash` | the staged IR does not hash to the bound composition hash |
| `backend` | the chain binds no artifact for this receiver's backend |
| `artifact-bytes` | the staged artifact does not hash to the bound digest |
| `policy` | the staged `policy.json` is not the bound facet |
| `capability-ceiling` | the policy requires a capability outside the ceiling |
| `evidence-stale` | the attestation is past the receiver's TTL, or a bound cert differs |

### The Ed25519 prerequisite is recorded, not papered over

Item 127's signature is symmetric, so a verifier that can check is a verifier
that can forge. That is fine inside one trust domain, which is exactly what
Slice 1 is. It is not fine across domains, so a `TrustStore` that declares
itself `cross_domain=True` under `hmac-sha256` is **refused outright** with the
reason naming the Ed25519 upgrade. Shipping cross-domain deploy on HMAC would
make `signer-untrusted` a fiction; refusing says so instead.

## 2. Effect correlation on every seam crossing

Every call crossing a seam can carry a `Correlation` envelope —
`{composition_id, generation, realm, effect_id, idempotency_key, parent_effect}`
plus the caller's item-55 identity. It rides the existing JSON-line request as a
`correlation` member; no new transport.

The envelope is **authenticated against the peer identity**. `deploy.seal`
stamps an HMAC under the caller's own per-process secret (the one the conductor
put in that process's spec), and `CorrelationGuard` verifies it under the secret
of the identity the envelope claims. On a network seam the mTLS peer
certificate authenticates the same identity independently, and both must agree,
so a leaked secret cannot speak from another TLS session and a valid TLS session
cannot speak under another process's name.

Duplicate detection is scoped on `(peer_identity, composition_id, generation,
idempotency_key)` — exactly that tuple. One peer's idempotency-key namespace is
not another's, so a peer can neither collide with nor replay a sibling's
crossing.

```python
guard = deploy.CorrelationGuard({"db": db_secret, "edge": edge_secret})
server = await bridge.serve(ctx, exports, endpoint, correlation=guard)

envelope = deploy.seal(deploy.Correlation(
    composition_id="app", generation=7, peer_identity="edge",
    effect_id="Cache.get", idempotency_key="k1"), edge_secret)
proxy = bridge.proxy_component("Cache", ["get"], endpoint, correlation=envelope)
```

`correlation` is optional on both halves. Absent, the wire and the dispatch are
byte-identical to the pre-118 seam. Present, a request that fails
authentication or is a replay is answered with an error and **never
dispatched** — the guard runs before `_invoke`.

## 3. The coordinated cross-process protocol

`run_deploy(participants, approval_path=...)` drives PREPARE / COMMIT / ABORT
over participants that are other processes.

**PREPARE** has no runtime effects, so a refusal there is not a rollback at all:
every participant is `never-committed`, nothing activated, nothing to undo. This
is the common failure case (a bad signature, a capability expansion, stale
evidence) and it costs zero unwinding.

**COMMIT** is where the coordination is real. Each participant applies its own
slice, in its own process, with its own WAL. The coordinator appends a row to a
**commit ledger** — who committed, in what order — and holds nothing else. It
does not hold the participants' inverses, cannot enumerate them, and never runs
one.

On a COMMIT failure the coordinator:

1. records the durable ABORT decision **first**, so a participant stranded from
   that moment on settles against a record rather than a guess;
2. sends ABORT to committed participants in **reverse ledger order** — the
   cross-process LIFO is an ordering over *messages*, and each unwind is
   performed by the participant that owns the inverses;
3. collects each participant's own settled verdict.

Per-participant outcomes are `never-committed`, `applied`,
`rolled-back-clean`, `rolled-back-with-residue`, or `unresolved(reason)`. The
aggregate is `applied` only if every participant applied, `aborted-clean` only
if every committed participant reported `rolled-back-clean`, otherwise
`aborted-with-residue` with the residue enumerated. A participant that could not
be reached is `unresolved` and is settled later by running `revl recover` on
**its** WAL — it is never reported as rolled back.

`ProcessParticipant` is the participant driven over a child process's
stdin/stdout, the same control-channel shape `_process_runner.py` already uses
for `repoint`.

## 4. All-or-nothing federation across co-located compositions

No composition may cross an irreversible effect before a durable
`federation-commit-approved` record exists. PREPARE holds every irreversible
crossing as an item-245 class-(b) deferred emission.

That is only possible when the crossing is deferrable. So
`federation_admission(plans)` **refuses** a plan that necessarily crosses a
non-deferrable irreversible effect — a reached `emission` extern with no
`deferred` modifier is class (c): it fires at the call, PREPARE has no way to
hold it, and admitting the plan would let a late partition strand that
composition with residue while its peers revert. The refusal names the extern
and the component, and says the fix: declare the extern `deferred`.

The same call also refuses the whole update when any pinned consumer surface
would break, using `federation.check` — the §5 drift predicate itself, not a
copy — so a federation update is admitted or refused as one unit.

```python
verdict = deploy.federation_admission(
    {"billing": billing_ir, "notifier": notifier_ir},
    contracts=[(app_surface, "billing")])
```

### Settling a stranded participant

`settle_stranded(wal_path, decision_path)` applies `recovery.py`'s rule
verbatim; it decides nothing on its own.

* durable `federation-commit-approved` present: the federation committed. The
  participant's WAL is given the `commit-approved` marker recovery already keys
  roll-forward off, and `recovery.recover` rolls it forward.
* absent, or an explicit abort decision: `recovery.recover` reads the WAL
  unchanged and rolls back LIFO, reporting residue exactly as today.
* unreadable: **fails closed** with `unresolved` and settles nothing. Guessing
  between roll-forward and roll-back is precisely the split-brain the durable
  record exists to prevent.

## Not in Slice 1

* cross-machine orchestration: SSH / container / microVM launch of a remote
  runner (`network-placement.md` still lists orchestration as a non-goal);
* a replicated WAL and a quorum-durable federation decision;
* a partition-safe distributed commit coordinator;
* the Ed25519 upgrade to `attest.py` — a hard prerequisite for the
  cross-trust-domain deploy, refused explicitly rather than faked;
* hardware remote attestation (TPM/TEE) of the loaded image.
