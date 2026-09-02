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
| `policy` | the staged `policy.json` is not the bound facet, or does not project the admitted IR |
| `capability-ceiling` | the staged composition reaches a capability outside the ceiling, or its surface cannot be measured |
| `evidence-stale` | the attestation is past the receiver's TTL, or a bound cert differs |

### The ceiling is measured off the IR, not off `policy.json`

`policy.json` is a **projection**: `build_bundle` derives it from the IR with
G4's boundary analysis, and the party a ceiling constrains is the same party
that writes it. So `admit` measures the capability surface itself, with
`deploy.capability_surface(ir)` over `ir/ir.json` as staged: the document whose
`composition_hash` was already re-derived here and checked against the
signature, and the document every emitter emits from.

The difference is not theoretical. When the ceiling read the projection,
deleting `policy.json`, emptying its `capabilities`, renaming that key, or
leaving bytes `json.loads` refuses each produced an empty wanted-set that passed
any ceiling, all four with a valid signature and a matching facet binding,
because each was signed over as staged. None of them can move the measured
surface: an edit to the IR that lowers it is a different composition and refuses
at `source-or-ir-hash` first.

Two consequences. A surface that cannot be measured is a `capability-ceiling`
refusal, never an empty set. And a bound `policy.json` that disagrees with what
the IR derives is refused at `policy`, so the projection stays a checked
artifact rather than a decorative one, without ever being the thing the ceiling
is measured against.

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

### 2b. What a network seam can carry instead

The guard above is four gates: shape, a **closed peer table**, an **HMAC** under
that peer's own per-process secret, and a **replay ledger**. On a local UDS seam
the transport authenticates nothing (`bridge.peer_identity` is `None` for a Unix
socket), so the HMAC is the only thing binding the claimed identity to a real
caller. That is what *sealed* means.

**A TCP+mTLS seam cannot carry it, and the reason is structural.** The secret is
minted fresh by one conductor at every `run_placement()` and delivered only
inside the process specs of its own children. A network provider may also be
dialled by an item-151 cross-composition consumer, which runs under a *different*
conductor and can never hold that secret; distributing it is Slice 2's replicated
control plane. Demanding a sealed envelope there does not harden the seam, it
refuses the legitimate caller. The freshness gate needs an envelope to dedup, so
it goes with the secret.

What mTLS *does* prove — per session, with a CA-signed key — is **which** identity
is calling. What was missing is a closed set to check that against:
`verify_mode = CERT_REQUIRED` against a shared CA answers every identity that CA
ever signed. `deploy.PeerAllowlist` is that set. It holds **names, not secrets**,
which is exactly why it works across a composition boundary, and it needs nothing
from the caller's bridge, so a py consumer, a node consumer and a stranger
composition are all judged the same way.

```python
allow = deploy.PeerAllowlist(["edge", "partner"])
server = await bridge.serve(ctx, exports, endpoint, peers=allow)
```

The verdict is taken **once per connection**, at the handshake's identity, before
a request is read; a refused peer gets one `{"peer_refused": ...}` line and the
connection closes. `correlation` and `peers` compose and neither implies the
other.

### 2c. The level a seam achieved, said out loud

Because the two planes are not the same property, they are not reported under the
same name. `deploy.SeamAdmission` records what each seam actually got, in
`bundle.verify`'s vocabulary:

| level | what holds |
| --- | --- |
| `sealed` | caller authenticated by its own per-process secret; replays refused |
| `peer-pinned` | mTLS identity checked against a declared allowlist; **not** replay-checked |
| `unverified` | **cannot verify who may call** |

The conductor prints one line per cross-process seam, plus a count of the
unverified ones:

```
  seam admission provider (tcp+mtls): peer-pinned — mTLS peer identity checked ...  peers: consumer, partner
  seam admission cache (uds): UNVERIFIED — a consumer runs on a tier whose bridge cannot seal ...
  seam admission: 1 of 2 seam(s) UNVERIFIED — ... a seam is only as closed as the peer set it can name
```

A seam that cannot prove who may call it says so. It does not stay quiet, and it
does not borrow the stronger word.

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

Reachability is read off `query.Composition`'s per-scope facts, the same surface
`revl audit` prints, so the gate can never disagree with the boundary the audit
shows. Two shapes that are easy to miss are counted:

* a provide-method whose body **is** the emit (`fn put(k, v) = emit host(v)`)
  lowers to a `return` step, so the `emit` marker does not survive lowering;
  the crossing is found by the called name instead;
* an emission mediated through a required service's capability-scoped operation
  (`emit store.put(..)` against `emission[host_put] fn put`), when the key
  resolves to no provider **in this composition**. Such a row carries
  `via: "<key>.<method>"` and is always class (c): deferral is a property of an
  extern declaration and is not spellable on a service method, so the operation
  fires at the call and this composition cannot prove the far side holds it.
  When the key does resolve locally, the provider's own scope already carries
  the crossing with its true deferrability, so the mediated row is skipped
  rather than double-counted.

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
