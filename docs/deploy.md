# `revl deploy`: attested admission, correlated seams, coordinated rollback

Roadmap item 118, **Slice 1** (single host / local multiprocess) and
**Slice 2a** (the deploy map, and the container boundary). The design is
`docs/design/118-revl-deploy.md`; this page is what actually landed and how to
use it. The code is `src/revl/deploy.py`, the participant runner is
`src/revl/_deploy_participant.py`, and the seam changes are in
`backends/python/bridge.py`.

Neither slice builds the cross-machine half. There is no replicated WAL and no
quorum coordinator, and a **machine boundary is refused** rather than
best-efforted (§5). What is built is the part that is honest on today's tree and
that the cross-machine half will reuse whole.

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
is detectable and non-repudiable. Detectable is not the same as impossible, and
hardware remote attestation is out of scope.

## 1. The attestation chain

`chain_bindings(bundle)` hashes each link of a `.revlbundle`: the emitted
artifact per backend, `policy.json`, `components.lock`, `gauntlet.json`, and the
per-backend conformance cert. `make_deploy_attestation` folds them into item
127's signed `evidence_bindings` (item 290's hook). One signature commits
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
`ir_hash` member of the attestation, even a signed one. Those names are listed
in `deploy.SELF_DECLARED_IGNORED`, so the refusal is a stated property. An
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
| `evidence-stale` | the attestation is past the receiver's TTL, a bound cert differs, or evidence the receiver requires is absent |

### Requiring evidence, not just checking what is present

A bound facet is re-hashed; an absent one is, by default, simply unchecked, so a
bundle built where a piece of evidence was unavailable still admits (`build_bundle`
degrades honestly and stages no dossier or cert). That silence is the hazard a
receiver that WANTS the evidence has no way to notice — the `unbound-means-unchecked`
shape. Two `TrustStore` flags close it, each fail-closed:

- `require_gauntlet=True` refuses a chain that binds no item-31 `gauntlet` facet.
- `require_conformance=True` refuses a chain that binds no item-306
  `conformance/<backend>` facet for the receiver's own backend — the chain's
  runtime-target link (§1). Its own signature is not re-verified: Slice 1 trusts
  the cert transitively through the bound hash, and interpreting the cert's
  per-tier verdict is deferred with the backend-name reconciliation it needs (a
  cert names its tiers `py`/`ts`, a deploy backend is `python`).

```python
trust = deploy.TrustStore(keys={key_id: verify_key}, backend="python",
                          require_gauntlet=True, require_conformance=True)
```

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

Every call crossing a seam can carry a `Correlation` envelope:
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

Duplicate detection is scoped on exactly one tuple, `(peer_identity,
composition_id, generation, idempotency_key)`. One peer's idempotency-key
namespace is not another's, so a peer can neither collide with nor replay a
sibling's crossing.

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
dispatched**: the guard runs before `_invoke`.

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
refuses the legitimate caller.

**The freshness gate is a different question, and it does not go with the
secret.** Dedup needs a key naming one crossing and an identity to scope that key
by. The HMAC exists to bind a *claimed* identity to a real caller. That is
load-bearing on UDS precisely because the transport binds nothing there. On
TCP+mTLS the handshake has already bound the identity, per session, with a
CA-signed key, so the scope is available with no shared secret at all.

mTLS *does* prove **which** identity is calling, per session, with a CA-signed
key. What was missing is a closed set to check that against:
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

### 2b-ii. Replay protection with no shared secret

`deploy.TransportReplayGuard` is the freshness gate for a network seam. It keys
dedup on `(transport identity, composition_id, generation, idempotency_key)`. The
first member is read off the **peer certificate**, never off the request body: a
replayer controls every byte it sends and controls nothing about the mTLS session
it sends them in.

```python
server = await bridge.serve(ctx, exports, endpoint,
                            peers=deploy.PeerAllowlist(["edge", "partner"]),
                            replay=deploy.TransportReplayGuard())
```

Unlike `peers` it runs **per request**, after the connection was admitted. Three
gates:

1. **a proven identity**: no transport identity means no scope to key by, so
   the request is refused (`unauthenticated-peer`) rather than keyed on the
   payload;
2. **agreement**: an envelope naming a `peer_identity` must name the one the
   handshake proved, so replaying a captured envelope under another certificate
   is `peer-identity-mismatch`;
3. **freshness**: a repeat of a seen scope is `duplicate-envelope` and is never
   dispatched.

A request with no envelope, or one declaring no `idempotency_key`, is dispatched
and **not** deduplicated: nothing declares it re-deliverable (item 309), and
refusing it would refuse exactly the cross-composition caller this plane exists
to keep serving. `placement.py` installs the guard on every network seam and
gives each network consumer an **unsealed** envelope (identity + composition, no
secret), so a consumer under another conductor stamps the same shape from its own
certificate and is deduplicated the same way.

**The ledger is bounded**, because a provider answers a peer for weeks: at most
1024 keyed crossings remembered per identity and 64 identities at once, LRU on
both dimensions (`deploy.BoundedReplayLedger`). Past that the oldest scope is
forgotten and a replay of *that* crossing would be admitted again; the ledger
counts every eviction. Refusing everything once full would be a self-service
denial of service, not fail-closed. The bound is per identity so a chatty peer
cannot age out a quiet one's history.

### 2c. The level a seam achieved, said out loud

Because the two planes are not the same property, they are not reported under the
same name. `deploy.SeamAdmission` records what each seam actually got, in
`bundle.verify`'s vocabulary:

| level | what holds |
| --- | --- |
| `sealed` | caller authenticated by its own per-process secret; replays refused |
| `peer-bound` | mTLS identity checked against a declared allowlist, and a keyed crossing replay-checked against that proven identity over a bounded window |
| `peer-pinned` | mTLS identity checked against a declared allowlist; **not** replay-checked |
| `unverified` | **cannot verify who may call** |

The conductor prints one line per cross-process seam, plus a count of the
unverified ones:

```
  seam admission provider (tcp+mtls): peer-bound — mTLS peer identity checked ... and replays refused ...  peers: consumer, partner
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
**commit ledger** recording who committed and in what order, and holds nothing
else. It does not hold the participants' inverses, cannot enumerate them, and
never runs one.

On a COMMIT failure the coordinator:

1. records the durable ABORT decision **first**, so a participant stranded from
   that moment on settles against a record rather than a guess;
2. sends ABORT to committed participants in **reverse ledger order**. The
   cross-process LIFO is an ordering over *messages*, and each unwind is
   performed by the participant that owns the inverses;
3. collects each participant's own settled verdict.

Per-participant outcomes are `never-committed`, `applied`,
`rolled-back-clean`, `rolled-back-with-residue`, or `unresolved(reason)`. The
aggregate is `applied` only if every participant applied, `aborted-clean` only
if every committed participant reported `rolled-back-clean`, otherwise
`aborted-with-residue` with the residue enumerated. A participant that could not
be reached is `unresolved` and is settled later by running `revl recover` on
**its** WAL. It is never reported as rolled back.

`ProcessParticipant` is the participant driven over a child process's
stdin/stdout, the same control-channel shape `_process_runner.py` already uses
for `repoint`.

## 4. All-or-nothing federation across co-located compositions

No composition may cross an irreversible effect before a durable
`federation-commit-approved` record exists. PREPARE holds every irreversible
crossing as an item-245 class-(b) deferred emission.

That is only possible when the crossing is deferrable. So
`federation_admission(plans)` **refuses** a plan that necessarily crosses a
non-deferrable irreversible effect. A reached `emission` extern with no
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
would break. It uses `federation.check`, the §5 drift predicate itself rather
than a copy, so a federation update is admitted or refused as one unit.

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

## 5. The deploy map, and the boundary a deploy may cross

**Slice 2a.** Slice 1 built the protocol and left the caller to construct
`Participant`s in Python. There was no way to *write a deploy down*, and so no
way to refuse one before things started being spawned.

A deploy map is an item-56 placement map with one new per-process table:

```toml
[processes.worker]
components = ["Ingest"]

[processes.worker.deploy]
via   = "container"                 # local | container | ssh
image = "python:3.12-slim"
trust = "/etc/revl/deploy-trust.d"  # the store the far side verifies the chain against
```

A placement with no `[deploy]` table anywhere is a perfectly good deploy map: it
says "every process is my own child", which is what `run_placement` does today.
That back-compat is the point: a deploy map *is* a placement map,
byte-identical when nothing crosses a boundary.

`admit_deploy_map(placement, seams=...)` admits the whole map before anything is
launched. The map is **unauthenticated operator input**: it is not inside the
attested bundle, nothing signs it, and it is the file that says which machine
runs the composition. So the admission is all-or-nothing (a map that half-admits
is a deploy that opens some boundaries and then discovers it cannot open the
rest) and every rule refuses rather than downgrades:

| rule | refused because |
| --- | --- |
| `unknown-via` | an unimplemented `via` is not "assume `local`"; the boundary the operator asked for would not be the boundary they got |
| `machine-boundary` | `via = ssh` is not opened here at all; see below |
| `container-seam` | the seam is a Unix socket and a Unix socket does not cross a container bind mount portably (measured non-functional in both directions; `sandbox_runtime` refuses the same shape) |
| `seam-set-unknown` | a container target must be *proven* seam-free; "not told" is not "none" |
| `network-provider` | the address it binds is a contract other machines already hold, so moving it across a boundary is the re-tier `revl swap` already refuses |
| `no-trust-store` | a receiver that cannot verify the chain makes this a copy, not a deploy |
| `container-without-image` | an image resolved later turns a missing image into a dead child instead of a refusal |
| `local-with-remote-fields` | the table describes a boundary this target does not cross |

### Boundaries, and what teardown is worth across each

`via` names a launch mechanism; what a deploy can *promise* is a function of the
**boundary** that mechanism crosses. G7 is LIFO-complete over the **registered**
entries of an accumulator, and that quantifier is the whole answer: each boundary
changes which set outlives the failure. `TEARDOWN_PROMISE` records one answer per
boundary, so two mechanisms crossing the same boundary cannot pick up different
guarantees.

* **`process`** (`via = local`) is Slice 1. The participant runs its own G7
  unwind over the entries it registered and reports clean or names its residue. The
  conductor never substitutes its own unwind; a participant it cannot reach is
  `unresolved`, never `rolled-back`.

* **`container`** (`via = container`) is identical to a process boundary
  *while the container is alive*: the control channel is stdio and the participant
  unwinds in there. When the container is **destroyed**, the accumulator and
  every closure-only inverse die with it, so the conductor's verdict is
  `unresolved` and never `rolled-back`. What the container boundary adds over a
  machine one is that the WAL sits on a mount the conductor *also* holds, so the
  target is still **settle-able**: `recovery.py` reads that WAL and applies its
  existing rule over the recorded entries, reporting each closure-only inverse as
  residue. Settling is a separate step from the deploy verdict, and the deploy
  never claims it happened.

* **`machine`** (`via = ssh`) promises **nothing**, which is why it is refused.
  A machine that goes away takes the accumulator, the closures *and* the WAL
  with it. There is no set on this side for G7 to be complete over and the
  conductor holds no inverse it could run, so the only honest verdict is
  `unresolved` naming the
  target for a human. A deploy that cannot promise teardown must not be the thing
  that quietly discovers it. The refusal also names the control plane that is
  absent: no bundle staging, no remote `deploy-admit` runner, no load-measured
  signed COMMIT receipt for the conductor to compare, and no pinned SSH host key
  (design R2/R4/R5). Without that pin, impersonating the target costs sitting on
  the network path rather than owning the machine.

### A participant behind a container boundary

`launch_container_participant(target, spec_path=..., state_dir=...)` returns a
`ContainerParticipant`, which is a `ProcessParticipant` and nothing more. That
is the finding: the coordinated protocol never held an inverse, so it does not
care what kind of boundary the other end is behind. It reuses item 411's
`sandbox_runtime.container_flags`, so the boundary gets the same hardening
(read-only root, `--cap-drop=ALL`, no-new-privileges, `--network=none`, the
invoking uid) and the same `revl.sandbox=411` label the leaked-container audit
watches.

`state_dir` is the one read-write mount and holds the participant's world file
and its WAL. Mounts are **identity-mapped**, so the launcher refuses a spec whose
`world`/`wal` paths are non-canonical or fall outside that directory: such a path
names a file that does not exist inside the boundary, and it would not fail until
the participant tried to write it *mid-COMMIT*, where the only verdict left is
`unresolved`. Every other way the boundary can fail to open (no runtime, an
unreachable daemon, an image that does not resolve) is likewise a refusal before
launch, never a downgrade to running the participant unconfined on the
conductor's own kernel.

## Not landed yet

* **cross-machine orchestration**: the `machine` boundary above, with all of
  bundle staging, a remote `deploy-admit` runner, a load-measured signed COMMIT
  receipt the conductor compares, and a pinned SSH host key
  (`network-placement.md` still lists orchestration as a non-goal);
* a **replicated WAL** and a quorum-durable federation decision;
* a **partition-safe distributed commit coordinator**;
* a **seam-carrying container target**, blocked on the per-rung seam transport
  (item 411's next sub-slice); until it lands, a container target must be
  seam-free;
* a **`revl deploy` CLI command**. Slice 2a lands the map and its admission as
  library surface; the command that reads a `.toml` off disk and drives
  `run_deploy` is the next step, and would be a wrapper over what is here;
* the **Ed25519 upgrade** to `attest.py`, a hard prerequisite for the
  cross-trust-domain deploy, refused explicitly rather than faked;
* **hardware remote attestation** (TPM/TEE) of the loaded image.
