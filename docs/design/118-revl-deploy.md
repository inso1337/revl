# `revl deploy` - composition as an attested distributed deployment (item 118)

## Revision (adversarial review 2026-09-01)

A second, independent adversarial review found a new top-severity defect and
four supporting ones. All five are folded in below. **This section is
authoritative and supersedes the sections that follow wherever they conflict**
(chiefly the old S1.3/S2.4/S4.2/S5-A2 and the original Slice 1 in S6). The
original text is kept for provenance, but the design of record is here.

The single reframe that drives the rest: **Slice 1 is now one SINGLE trust
domain - one host you reach with your own credentials - and the cross-trust-
domain deploy is a separate, clearly-labeled future item that needs a new
attestation primitive revl does not have.** The whole-composition signature that
is landed today and per-host confidentiality are incompatible without that
primitive; Slice 1 sidesteps the incompatibility by not requiring confidentiality
from the host at all.

### R1. The attested unit is the WHOLE composition; admission was specified PER-HOST SLICE (NEW, top severity)

The landed attestation signs the whole composition, not a slice. In
`bundle.py`, `composition_hash = attest.canonical_hash(norm_ir)` and
`make_attestation(norm_ir, key, ...)` (lines ~418/421) sign over the WHOLE
normalized IR; `_emit_files(backend, norm_ir)` (called at ~398/655) emits each
backend over the WHOLE IR; and `evidence_bindings` are whole-composition facet
hashes (item 290). There is **no per-slice attestation primitive** anywhere in
item 127 or item 305.

But the original S1.3 / Slice 1 had the host receive a *sliced* bundle and run
`revl verify` locally on it. That cannot work: a host recomputing
`canonical_hash` over a subset IR will never equal the bound whole-composition
hash, so **every honest deploy would refuse itself.** The three ways out are all
bad:

- Re-sign per slice at the conductor. Then the conductor holds the signing key,
  which is exactly the trust inversion S1.2/S2.4 forbid (the operator gets to
  assert "trust me, it is admitted"), and under any asymmetric scheme it needs
  the PRIVATE key.
- Stage the whole source+IR to every host so the whole-composition hash checks.
  Then every partially-trusted host sees the entire composition, contradicting
  the different-trust-domain motivation that S2.4 leans on.
- Invent a per-slice primitive. That is real new scope, not reuse.

**Resolution - pin the unit.** Slice 1 takes the honest minimal path: a **single
trust domain**. One host, reached with the operator's own credentials, receives
the **whole attested bundle**, stages it, and verifies the whole-composition
signature and chain as-is. "Slice" in Slice 1 means only the **activation set**
(which components this host activates), never a cryptographic subset of the
attested bytes. Because it is one trust domain, staging the whole composition to
the host discloses nothing the operator was hiding from it, so the
confidentiality objection does not arise.

The **cross-trust-domain deploy** (a partially-trusted host that must NOT see the
whole composition) is scoped as a distinct FUTURE item. It requires a new
**segmented attestation primitive**: a Merkle root over per-component IR leaves
and per-component artifact leaves, signed once, so a host can be handed only its
own leaf plus the inclusion proof and verify membership without seeing the other
components. That is a genuine EXTENSION of items 127/305, called out as new
scope, not a reuse of what is landed. Stated plainly: **today's whole-composition
signature and per-host confidentiality cannot both hold without that primitive.**

### R2. The receipt must bind a load-time MEASUREMENT the conductor COMPARES, not echo PREPARE's verified hash (HIGH)

The original A2 receipt bound "the artifact hash the host claims," which is a
TOCTOU hole: PREPARE verifies bytes, then COMMIT loads bytes, and nothing forced
the loaded bytes to equal the verified ones, nor forced the conductor to check.
For "detectable" to hold even against an honest-but-buggy host, two things are
now required:

1. The COMMIT receipt binds `sha256` of the **exact artifact bytes `deploy-admit`
   hands to the runtime, measured immediately before hand-off at COMMIT-load
   time** - a fresh measurement, never an echo of the hash PREPARE verified.
2. The **conductor COMPARES** that receipt hash against the signed
   `artifact/<backend>` binding and **REFUSES on mismatch.** S2.3's refusal was
   only during PREPARE's verify; the comparison must also gate COMMIT.

The guarantee wording SPLITS accordingly:

- **honest-but-buggy host -> DETECTABLE**, via the load-time measurement plus the
  conductor comparison. A host that loads the wrong bytes by accident is caught.
- **malicious host -> ATTRIBUTABLE / non-repudiable only.** The host signed a
  statement about what it loaded; a lie is attributable to its key, but not
  detectable without hardware (TPM/TEE), which is out of scope.

The residual TOCTOU between the measurement and the runtime's own `exec` of those
bytes is unclosable without hardware and is named, not papered over.

### R3. Ed25519 is a real blocker AND under-scoped; it is dropped from Slice 1 (HIGH)

Item 127 is symmetric: `attest.py:67` `SIGN_ALG = "hmac-sha256"`, `_sign` uses
`hmac.new`, and `verify_attestation` needs the same secret. Calling this a "flip
the alg member" change is wrong. The real migration surface, re-titled the
**asymmetric-signing migration (item 127 v2)**:

- **(a)** a NEW third-party dependency. The stdlib has no Ed25519, so this breaks
  `attest.py`'s explicit "deliberately dependency-free / stdlib-only" invariant
  (lines ~13-28); it needs `cryptography` or PyNaCl.
- **(b)** `key_id(key)` (`attest.py:107`) fingerprints the SECRET; under
  asymmetric signing it must fingerprint the PUBLIC key on both sides. That is a
  semantic change the S2.4 trust-store lookup depends on.
- **(c)** it ripples through ~6 call sites: `bundle.py` (~418/421 sign,
  ~718/723 verify), `registry.py` (~245 verify, ~645 sign), `cli/observe.py`
  (~246 verify, ~265 sign), `truc/reproduce.py` (~375 verify), and
  `mcp/server.py` (~1017 `resolve_key`).
- **(d)** `tools/conformance_cert.py:81` has a PARALLEL HMAC signer (its own
  `_sign` at ~318/325). If a host verifies the cert's own signature, item 306
  needs the same upgrade; if the host trusts the cert only transitively via the
  bound `conformance/<backend>` hash, the doc must SAY SO and forbid independent
  cert-signature trust. Slice 1 takes the transitive path (see R-slice below).

Because Slice 1 is now a single trust domain (R1), **Ed25519 is dropped from
Slice 1.** Slice 1 ships on HMAC with "signer untrusted" documented as NOT-yet-
enforced (honest for a single domain: the operator who signs is the operator who
deploys). The asymmetric migration becomes a **prerequisite of the cross-trust-
domain future item**, not of Slice 1.

### R4. The SSH orchestration target needs a pinned host key; the deploy map is outside the attested chain (MEDIUM/HIGH)

Without a pinned SSH host key, a network MITM impersonates the target host, runs
a fake `deploy-admit` that echoes the expected hash (the R2 hole if R2 is not
enforced), and serves backdoored bytes - collapsing the "own the host" bar down
to "sit on the network path." Separately, the `[deploy]` table (`host` / `runner`
/ `trust`) in `--map` is **unauthenticated operator input**: only the build-time
`topology.json` is inside the bundle, the deploy map is not.

**Resolution.** Require an SSH host-key pin (`[deploy].host_key`, or a pinned
`known_hosts`); forbid `StrictHostKeyChecking=no`. State plainly that without the
pin the A2 blast-radius argument does not hold. The load-measured receipt (R2)
plus a pinned receipt key (R5) are what backstop a MITM'd channel; the host-key
pin is what stops the impersonation in the first place.

### R5. The COMMIT-receipt signing key needs specified provenance (MEDIUM)

The conductor must verify the receipt (R2), but the original doc never said where
it gets the host's verify key. **Resolution.** The receipt key is bound to the
host's **item-55 mTLS identity** (`bridge.py` `TlsConfig.identity` / `certfile`),
or to an explicitly-pinned per-host verify key in the conductor's map.
Receipt-signature verification is a **HARD conductor-side check, not advisory**:
an unverifiable or wrong-key receipt fails the deploy.

### Validated (kept): PREPARE is effect-free for Slice 1, with two named caveats

The review confirmed PREPARE has no runtime effects for Slice 1: `revl verify`
recompiles and re-emits via the pure in-repo emitters (`bundle._emit_files`), no
bundle code runs, no extern fires, and no native toolchain is shelled. Two
caveats are named explicitly, not hidden:

1. PREPARE spawns the `deploy-admit` process on the host. That is itself an
   effect, and it is the FIRST place attacker-controlled code could run.
2. PREPARE writes the staged bundle to the host (reversible by deleting it).

The effect-free claim breaks only for an impure or tool-invoking emitter, or for
the deferred `via = container` path.

### Re-sliced plan (supersedes S6 Slice 1)

**Slice 1 - one single-trust-domain remote host, over pinned SSH.**
- Exactly ONE remote host, in the operator's OWN trust domain, reached over SSH
  with a **pinned host key** (R4).
- The **whole** attested bundle is staged to that host (R1). "Slice" = activation
  set only.
- The host runs `revl verify` plus the whole-composition chain verify **on the
  whole bundle**, on **HMAC** (R3), against the whole-composition
  `composition_hash` and `evidence_bindings`.
- The host does a LOCAL `apply` of its activation set, writing its own WAL.
- The host returns a **load-measured** COMMIT receipt (R2); the **conductor
  compares** the measured hash to the signed `artifact/<backend>` binding and
  REFUSES on mismatch.
- The **receipt key is the host's mTLS identity** (R5); receipt verification is a
  hard conductor-side gate.
- **Rollback** is that one host's local `apply`/`recover` LIFO - the exact
  in-process theorem, no distributed orchestration.

**Deferred (named boundaries):**
- **Cross-trust-domain deploy**: needs the segmented per-slice attestation
  primitive (R1) AND the asymmetric-signing migration / item 127 v2 (R3).
- **Multi-host orchestration** and the two-phase cross-host commit/abort with the
  `unresolved` residue verdict (original S3).
- **Container / microVM targets** (`via = container|microvm`).
- **Transactional multi-composition federation.**
- **Hardware remote attestation** (TPM/TEE) - the only thing that upgrades a
  malicious host from attributable to detectable (R2).


### Unreconciled with the Addendum's slice math

This pass puts a pinned-SSH control plane inside Slice 1; the Addendum at the
foot of this document (from the `design/118-revl-deploy-review` pass) records
that remote orchestration and the SSH/container launch are Slice 2+ and that
Slice 1 rides a static item-56 TCP+mTLS endpoint. Both passes are recorded as
they were written. Which control plane Slice 1 owns is open, and R4's host-key
pin is a requirement of whichever one it turns out to be.

---

**Status: design, not implemented.** This document specifies `revl deploy`:
`apply` (docs/apply.md, `src/revl/apply.py`) extended past a single process to
remote hosts, each remote component reached over the item-56 network bridge
(`backends/python/bridge.py`, docs/network-placement.md), plus an **attested
supply-chain** so a host cryptographically links the artifact it is asked to
run back to its source, guarantees, capabilities, and runtime target before it
admits it (the folded external proposal #7). It reuses the landed machinery
whole: the reproducible bundle (item 305, `src/revl/bundle.py`), the signed
attestation with evidence bindings (item 127, `src/revl/attest.py`), the
conformance certificate (item 306, `tools/conformance_cert.py`), the WAL and
crash recovery (items 47/322/413, `src/revl/wal.py`, `src/revl/recovery.py`),
and the TCP+mTLS seam with per-process identity (items 54/55/56). Nothing here
touches the frontend or the checker.

The headline finding from the adversarial self-review (S5, C1) is stated up
front because it reshapes the whole design: **the rollback theorem `apply`
proves - unwind LIFO, then prove no residue - is an in-process property, and it
does NOT cross a seam.** A remote host's inverses live in that host's process
memory and its durable WAL, unreachable from the conducting process
(`recovery.py`: a boundary inverse found only as a closure is reported as
*residue*, never claimed to have run; `placement.py` line ~2186 already records
that "a witnessed rollback across a seam is out of scope"). So `revl deploy`
cannot honestly promise the item's headline "LIFO rollback on partial failure"
as one distributed theorem. What it CAN prove decomposes into two honest parts:
each host's LOCAL apply is residue-free-or-reported exactly as today, and the
CROSS-host step is a two-phase orchestration that moves every fallible check
into a side-effect-free PREPARE phase, so the only rollback that ever spans
hosts is a best-effort abort over per-host-atomic commits, with any host it
cannot reach reported as UNRESOLVED, never as "rolled back." Slice 1 is scoped
to exactly the case where this distinction collapses to a proven property: a
single remote host, whose rollback IS its own local `apply`/`recover`.

---

## 0. What is landed, and what item 118 actually adds

The temptation is to read item 118 as "build distributed apply." Almost all of
it exists. Grounding the design in what is already real keeps the new surface
small and honest.

Already landed and reused unchanged:

- **`apply` / `.plan` artifact** (`apply.py`): drift-refusing, per-step
  prediction-checked, LIFO-rollback-with-residue-proof application of a plan to
  a live composition. The pure half is `apply.py`; the effectful half (driving
  fibers, the LIFO unwind) is `mcp.session.Session.apply`. **Single process.**
- **network bridge** (item 56, `bridge.py` + docs/network-placement.md): a seam
  can name a machine (`host:port`), crossing TCP+mTLS instead of a UDS. Both
  ends present a CA-signed certificate; identity per process is the operator
  token (item 55); a wedged remote raises `SeamDeadline` (item 54); peer death
  is a reactive withdrawal. A network seam without identity AND a deadline is
  refused.
- **reproducible bundle** (item 305, `bundle.py`): `app.revlbundle/` carries
  source, canonical IR, `components.lock`, `emitted/<backend>/...`,
  `policy.json`, `attestation.json`, `gauntlet.json`, `topology.json`,
  `runtime-manifest.json`. `revl verify` recompiles and compares tier by tier
  with OK / MISMATCH / cannot-verify.
- **attestation** (item 127, `attest.py`): `canonical_hash(ir)` is the stable
  identity; `make_attestation` signs `{composition_hash, guarantees, verdict,
  timestamp, signer, key_id, evidence_bindings?}` with HMAC-SHA256;
  `verify_attestation` reports signature-mismatch vs hash-mismatch distinctly.
  The `evidence_bindings` member (item 290) already folds per-facet sha256
  hashes INTO the signed payload - **the exact hook the deploy chain extends.**
- **conformance certificate** (item 306, `conformance_cert.py`): a signed record
  binding `source_hash` + `ir_hash` + which tiers ran + per-tier pass +
  `semantic_differences` + `runtime_versions`, signed with the same `attest`
  primitives.
- **WAL + recovery** (items 47/322/413): tier-agnostic JSON-Lines WAL, the
  `activation-complete` marker is the roll-forward/roll-back decision;
  `recover` re-issues boundary inverses newest-first and reports closure-only
  inverses as residue.
- **placement conductor** (`placement.py` `run_placement`): compiles, gates
  (tier-capability, cross-process resource-crossing, sandbox/realm/capability),
  spawns one runner per process, wires seams, prints measured seam latency,
  supports live `swap`. Isolation rungs `wasm-cell | container | microvm` with
  an image already exist.

The one thing that is genuinely NOT built - and it is called out as a non-goal
in docs/network-placement.md - is **orchestration**: "nothing launches the
remote process for you (the provider runs its own placement on its own
machine)." The conductor's `command_for` (line ~2158) only ever spawns a LOCAL
subprocess (`subprocess.Popen`, `sys.executable -m revl._process_runner`, or the
node/rust/go/java runners). Item 118 is therefore, precisely:

1. **remote launch** - a `[deploy]` target per process (`ssh` / `container` /
   `local`) that stages a bundle onto a machine and starts its runner there;
2. **an attestation chain** that binds source -> IR -> backend-artifact ->
   capability policy -> evidence -> signature, verified HOST-SIDE before a
   component is admitted;
3. **a signed admission receipt** the host returns, and a **two-phase**
   commit/abort the conductor runs over those receipts.

Everything else on the path is a reuse. This is why the STRATEGIC NOTE in the
roadmap is right that 118's remote transport is the unlock for the whole
productionization cluster: the transport, mTLS, and per-host WAL already exist;
118 is the thin orchestration and cryptographic-binding layer on top.

---

## 1. The `revl deploy` surface

### 1.1 The command

```
revl deploy app.revlbundle --map deploy.toml [--key PATH] [--dry-run] [--once]
```

`deploy` takes a **reproducible bundle** (item 305), not raw `.rvl`, on purpose:
the bundle is the unit whose bytes are attested, and deploying source would mean
the conductor recompiles and the thing that ran on the host was never the thing
that was signed. `deploy app.rvl` is sugar that runs `revl bundle` first and
deploys the result, but the artifact of record is always the bundle.

`--dry-run` runs the entire PREPARE phase (build/stage/attestation-verify on
every target) and stops before any COMMIT, printing the admission verdict each
host would return. It is the deploy analogue of `revl plan`: it makes the whole
fallible half observable with zero effects.

### 1.2 The deploy map

The deploy map is the item-56 placement map (docs/network-placement.md format -
`[processes.*]`, `.address`, `.tls`) with one new per-process table, `[deploy]`,
that says HOW to launch that process's runner. A process with no `[deploy]`
table stays local, spawned exactly as `run_placement` spawns it today, so a
mixed local+remote deploy is the default and a fully-local deploy map is
byte-identical to a placement file.

```toml
# deploy.toml  (sketch - the [deploy] table is the only thing new vs item 56)

[processes.db]
components = ["PgDatabase"]
[processes.db.address]            # item 56: this process serves a network seam
host = "10.0.0.5"
port = 9443
[processes.db.tls]                # item 56: per-process mTLS identity (item 55)
identity = "db"
cert = "/etc/revl/db.crt"
key  = "/etc/revl/db.key"
ca   = "/etc/revl/seam-ca.crt"
[processes.db.deploy]             # item 118: how to LAUNCH the runner remotely
via   = "ssh"                     # ssh | container | local
host  = "deploy@10.0.0.5"         # ssh destination (orchestration channel)
runner = "revl"                   # the runner already installed on the far host
trust  = "/etc/revl/deploy-trust.d" # the host's signer trust store (see S2.4)

[processes.edge]
components = ["UserCache"]        # a local consumer of db's key, unchanged
[processes.edge.tls]
identity = "edge"
```

Two channels are deliberately distinct and must not be conflated (this is
attack S5-A4): the **orchestration channel** (`[deploy].via = ssh`, used once,
to stage the bundle and start the runner) and the **data-plane seam**
(`[processes.db.address]` + `[processes.db.tls]`, used for every runtime call,
per item 56). SSH authenticates the operator to the machine; mTLS authenticates
the two processes to each other; neither of those authenticates the *artifact* -
that is the attestation's job (S2), checked independently on the host.

### 1.3 The per-host handshake

For each remote process the conductor runs a fixed handshake. It is a
two-phase commit (S3) whose PREPARE half has no runtime effects:

```
PREPARE (no effects, fully reversible by doing nothing):
  1. conductor slices the bundle to this process's components + backend, and
     builds/loads the deploy attestation for that slice (S2).
  2. stage: copy the sliced bundle to the target
       via=ssh       -> scp/rsync over the orchestration channel
       via=container -> add it as an image layer / bind-mount
       via=local     -> a path handoff (no copy)
  3. start the runner on the target in ADMIT-ONLY mode:
       revl deploy-admit <staged-bundle> --map <slice> --trust <store>
  4. the host runner verifies, LOCALLY (S2.4), the whole attestation chain and
     runs `revl verify` on the staged bundle. It loads NOTHING yet.
  5. the host returns a signed admission verdict: ACCEPT (+ the artifact hash it
     will load, its runtime_versions, a fresh nonce) or REFUSE (+ the failing
     chain link). No component is active on any host at the end of PREPARE.

COMMIT (per-host-local effects, each host residue-free by its own apply):
  6. only if EVERY target returned ACCEPT: the conductor tells each host to run
     its LOCAL apply of its slice (apply.py machinery, its own durable WAL).
  7. each host reports applied (+ a signed COMMIT receipt over what it loaded) or
     a local apply failure (which it has ALREADY rolled back locally, LIFO).
  8. the item-56 network seams come up between the now-running processes; the
     conductor prints measured seam latency (network-placement.md).

ABORT (if any REFUSE in PREPARE, or any COMMIT failure): S3.
```

The load-only-after-verify ordering is the same discipline
`run_placement`'s config preflight already uses ("fail with a diagnostic
instead of spawning children that die"): every check that can refuse a deploy
runs before any effect is registered.

### 1.4 How the bridge (item 56) connects the tiers

Once every host has committed, the running processes are wired by exactly the
item-56 bridge, with no new transport code. A key provided on `db` and required
on `edge` crosses `tcp://10.0.0.5:9443` under mTLS; the canonical value codec
carries the request/reply; `SeamDeadline` bounds a wedged remote; the monitor
connection turns a dropped host into a reactive withdrawal that deactivates
dependents with ordered teardown (R2/R3). The one seam-visible addition item 118
needs is **distributed effect correlation** (folded external #6): every call
crossing the seam carries a common identity `{composition_id, generation, realm,
effect_id, idempotency_key, parent_effect}` so a remote provider can correlate a
chain for recovery, OTel (items 120/121), duplicate detection, and incident
audit. This is a header on the existing JSON-line request, not a new transport;
it is what lets a post-deploy audit stitch one host's WAL to another's.

### 1.5 WAL and recovery across the seam

There is no distributed WAL and item 118 does not invent one - that is the
honest core of S3. Each host keeps its OWN durable WAL under
`wal.default_wal_dir()` (owner-only, reboot-surviving per item 413), written by
its own `apply`. Recovery is therefore per-host and unchanged: if a host
`kill -9`s mid-COMMIT, `revl recover` on THAT host reads THAT host's WAL and
rolls its slice back LIFO, reporting any closure-only boundary inverse as
residue, exactly as `recovery.py` does today. The conductor does not (cannot)
recover a remote host's in-process inverses; it can only ask a reachable host to
run its own recovery, and record - via the returned receipt and the correlation
identity (1.4) - what each host claims its WAL settled to. Cross-host
consistency after a crash is thus an *auditable* property (every host's WAL +
receipt is a signed, correlated record), not a *proven-atomic* one.

---

## 2. The attestation chain

### 2.1 What the chain binds, and why each link is needed

The roadmap names the chain: `source -> IR hash -> backend-artifact hash ->
capability policy -> gauntlet evidence -> signature -> deployment -> runtime
trace`. Today `attest.py` binds only `source/IR -> composition_hash` (the IR is
post-lowering, so one signed hash covers both). The gap item 118 closes is that
**the thing a host runs is the emitted backend artifact, and the attestation
does not currently bind it.** A nondeterministic or compromised emitter could
produce artifact bytes that never appear in the signed payload; a host checking
only `composition_hash` would admit them. So the deploy attestation extends the
signed body, through the `evidence_bindings` member `attest.py` already carries,
to bind every link:

```
deploy attestation body (signed, HMAC or Ed25519 - see S2.4):
  composition_hash        # attest.canonical_hash(IR)              [source+IR link]
  guarantees              # the G-codes the signer's gate verdict discharged
  checker                 # {compiler, ruleset}: WHICH frontend reached it
  evidence_bindings:      # per-facet sha256, folded into the signature (item 290)
    artifact/<backend>    # sha256 of emitted/<backend>/... bytes    [artifact link]
    policy                # sha256 of policy.json (item 305)         [capability link]
    lock                  # sha256 of components.lock                [surface link]
    gauntlet              # sha256 of gauntlet.json (item 31)        [evidence link]
    conformance/<backend> # sha256 of the item-306 cert for <backend>[runtime-target link]
  verdict, timestamp, signer, key_id
```

`guarantees` and `checker` are not self-descriptions: `attest.make_attestation`
refuses to sign without a `GateVerdict` from a real frontend run whose own
composition hash equals the one being signed, so `make_deploy_attestation`
re-runs the gate over the bundle's staged `source/` and a bundle whose staged
IR is not what its source compiles to cannot be signed at all.

Every one of these hashes is already computed by `bundle.py` when it writes the
bundle (`components.lock`, `policy.json`, `emitted/<backend>`, `gauntlet.json`)
and by `conformance_cert.py`. The deploy attestation does not re-derive any of
them; it collects them and folds them into ONE signature, so the whole chain is
committed by a single verification. This is the "cryptographically-linked
identity" the roadmap asks for: source, guarantees, capabilities, and runtime
target are all bound to one signature.

### 2.2 The rejection rules, mapped to the chain

The roadmap lists the six things a host must reject. Each maps to a bound hash
or a policy comparison, so the host's refusal is a mechanical check with a named
failing link, never a judgement call:

| host refuses when...        | how it is detected                                            |
|-----------------------------|---------------------------------------------------------------|
| source or IR hash changed   | recomputed `composition_hash` != bound (attest hash-mismatch) |
| backend differs             | no `artifact/<host-backend>` binding, or its hash != re-emit  |
| capability set expanded     | the surface derived from `ir/ir.json` reaches outside the ceiling |
| policy incompatible         | bound `policy` hash mismatch, or realm/operator gate fails    |
| evidence stale              | `gauntlet`/`conformance` timestamp older than the host's TTL  |
| signer untrusted            | `key_id` / verify-key not in the host's `[deploy].trust` store|

### 2.3 Tie to bundle (305) and conformance cert (306)

The host-side admission is literally `revl verify app.revlbundle` (item 305)
plus the chain check. `verify` already recompiles the source, re-emits each
backend byte-for-byte, and reports OK/MISMATCH/cannot-verify per tier. Item 118
adds only that (a) MISMATCH on the host's own backend is a hard REFUSE (not a
report), and (b) the recomputed per-tier hashes are checked against the SIGNED
`evidence_bindings`, so a bundle whose `verify` passes but whose signed chain
does not bind those exact bytes is still refused. The conformance cert (306) is
the `runtime-target` link: it is the portable evidence that this backend's
artifact means the same thing the reference tier does, so binding its hash is
what lets the host trust "this artifact, on my runtime" without re-running the
six-tier suite.

### 2.4 Host-side verification and the trust store

Verification runs on the HOST, in the `deploy-admit` runner, against a local
trust store (`[deploy].trust`) - a directory of trusted verify-keys/`key_id`s.
This placement is the whole security value: the deploying operator does not get
to assert "trust me, it is admitted"; the host independently recomputes the
chain and decides. **This forces the asymmetric-signature question.** Item 127's
HMAC-SHA256 is symmetric: a verifier must hold the signing secret, so any host
that can verify an attestation can also FORGE one for arbitrary bytes. That is
acceptable for a CI-signs-to-its-own-registry model; it is **not** acceptable
for deploy, where the host is often a different trust domain than the signer.
`attest.py` reserved the `alg` member for exactly this migration. Deploy
therefore REQUIRES the Ed25519 upgrade (or an equivalent asymmetric scheme):
the signer holds a private key, the host's trust store holds only public verify
keys, and `signer untrusted` becomes a real cross-domain check. Shipping deploy
on HMAC would make the "signer untrusted" rejection rule a fiction (S5-C1's
sibling; see S5-A1). This is a hard prerequisite, recorded in Slice 1.

**The refusal reads this build, not the record.** Until that upgrade lands,
`admit` REFUSES a `cross_domain` trust store outright. It used to gate that
refusal on `attestation["sign_alg"] == attest.SIGN_ALG`, which made a member the
SENDER writes the decider of the trust-domain question, while
`verify_attestation` never checked `sign_alg` at all and MAC-ed with the
symmetric key regardless. Relabelling the field `ed25519` therefore bought a
cross-domain ACCEPT with a receipt, and so did `''`, `'HMAC-SHA256'` and `null`.
Two changes close it: `verify_attestation` refuses any `sign_alg` but
`hmac-sha256` (the algorithm this build can actually verify is a property of
this build), and the cross-domain refusal reads nothing off the attestation.

**A signature is not a check (`TrustStore.recheck_source`).** `admit` re-hashes
`ir/ir.json` but never re-runs the gate, so with the flag off the receiver still
takes the SIGNER's word that a checker ever admitted the composition. What makes
that word worth something is upstream: an attestation cannot be issued without
an admitted `GateVerdict` over this exact `composition_hash`, and it names the
`checker` that produced it. What it leaves open is a signer whose frontend was
older, patched or lying, which is a property of the signer, not of the bytes.
`recheck_source=True` closes that: the receiver runs its OWN frontend over the
bundle's staged `source/` and refuses unless that run admits and reproduces the
staged IR. It is opt-in because it costs a compile on the receiving path and
requires the bundle's source, which a source-stripped bundle would not carry.

---

## 3. Failure and rollback across hosts

### 3.1 Why "distributed LIFO rollback" is not honestly available

`apply`'s rollback is a theorem about ONE process: it holds the ordered inverses
in memory, unwinds them last-in-first-out on any step failure or prediction
mismatch, and then re-derives the composition fingerprint to PROVE no residue
(`apply.verify_final`). None of that survives a process boundary. A remote host's
inverses are in the remote host's memory and WAL; the conductor has neither. If
host B has committed effects and host C then fails, the conductor cannot run B's
inverses - it can only ask B to run them. If B is unreachable (the partition
that motivates the whole distributed setting), the conductor cannot even ask.
Claiming a single distributed LIFO-rollback theorem here would be dishonest, and
`placement.py` already says so in a comment. The design's job is to make the
window where this matters as small as possible and to report the residue
honestly when it happens.

### 3.2 Two phases shrink the rollback window to near-zero

The handshake (1.3) is two-phase precisely so that the fallible work has no
effects to undo. PREPARE - build, stage, verify the chain, `revl verify` -
touches no runtime state on any host (staging a file is reversible by deleting
it, and nothing is loaded). A host that REFUSES in PREPARE has done nothing to
roll back; the conductor deletes the staged bundles (best-effort) and the deploy
fails clean with the failing chain link named, having activated no component
anywhere. This is the common failure case - a bad signature, a capability
expansion, stale evidence - and it costs zero rollback. This is the
transactional-federation shape (folded external #5) specialized to one plan:
commit the distributed change ONLY IF every participant admits, and because
admission is side-effect-free, a rejection is not a rollback at all.

### 3.3 A COMMIT-phase failure: per-host-atomic, best-effort cross-host

Only a COMMIT failure needs an actual cross-host abort, and COMMIT is where the
honest decomposition lives:

- Each host's COMMIT is its own local `apply`. If it fails mid-way, that host
  has ALREADY rolled its slice back LIFO and proven its own no-residue before it
  reports failure - a property `apply.py` gives for free, per host.
- The conductor, on any host's COMMIT failure, issues ABORT to every host that
  reported `applied`. Each such host runs its OWN local LIFO teardown (again a
  proven per-host property) back to its prior generation.
- A host the conductor cannot reach to abort is reported as **UNRESOLVED
  residue**: named, with its last signed receipt and correlation identity, so a
  human or a later `revl recover` on that host settles it. It is NEVER reported
  as "rolled back."

The aggregate verdict is therefore honest and structured, mirroring
`bundle.verify`'s OK/MISMATCH/cannot-verify vocabulary: each host is
`rolled-back-clean`, `never-committed`, or `unresolved(reason)`. The deploy
"succeeded" only if every host is `applied`; it "aborted clean" only if every
committed host is `rolled-back-clean`; otherwise it is `aborted-with-residue`
and the residue is enumerated.

### 3.4 Recovery reuse (item 245 / 47 / 322)

Nothing in 3.3 is new rollback code. A host's local abort is the item-245
session-commit/inverse path its `apply` already runs; a host's post-crash
settlement is `revl recover` reading that host's WAL. Item 118 adds only the
conductor-side collection of receipts and the honest aggregate verdict. The
correlation identity (1.4) is what lets `recover` on two different hosts be
reconciled after the fact into one picture of the deploy.

---

## 4. The G-invariant and the honest boundary

### 4.1 What `revl deploy` guarantees (the shape + the chain)

**G-deploy (proposed).** *A component admitted on a remote host is byte-for-byte
the artifact whose source, IR, capability policy, and per-backend runtime
evidence are bound by a signature the host independently trusts; and each host's
local application of its slice preserves every composition guarantee G1..G9 and
the A-rule lifecycle (residue-free-or-reported) exactly as an in-process
`apply`.*

Concretely, deploy preserves:

- **the shape** - the deployed composition's components, load order, provisions,
  and enumerable boundary surface (G8) are the attested ones; drift is refused
  (`apply` basis check) per host.
- **the chain** - source, IR, artifact bytes, capability policy, and runtime
  evidence are linked by one signature the host verifies against its own trust
  store before admitting (S2).
- **per-host residue-freedom** - each host's slice applies with LIFO rollback
  and a no-residue proof, or reports what it could not undo (A-rules, unchanged).
- **capability containment** - a host refuses an artifact whose policy expands
  the capability set beyond the host's ceiling (S2.2), so deployment cannot
  silently widen authority (G1/G9 across the boundary).

### 4.2 What it does NOT guarantee (the honest boundary)

- **the remote host's actual runtime.** revl emits an artifact; the host runs
  it on cordis-ts/py/rust/etc. revl does not execute on the host and cannot
  attest what the host's runtime, OS, or hardware actually did. The conformance
  cert (306) bounds "this backend means the same as the reference" as tested
  evidence, not a live proof. See S5-A2.
- **a compromised or lying host.** A host that returns a signed ACCEPT receipt
  but loads different bytes is telling a signed lie. Deploy makes this
  DETECTABLE and NON-REPUDIABLE (the receipt binds the artifact hash the host
  claims to have loaded, and its runtime_versions), but it cannot PREVENT a
  fully-compromised host from lying. The trust boundary is the host; deploy
  narrows and records it, it does not erase it. See S5-C1.
- **network partitions.** The conductor cannot distinguish a slow host from a
  dead one beyond `SeamDeadline`; a partition during ABORT yields
  `unresolved` residue (3.3), reported, not resolved.
- **cross-host atomicity as a single theorem.** Only PREPARE is globally
  all-or-nothing (it has no effects); COMMIT is per-host-atomic plus best-effort
  cross-host abort (S3).
- **that the artifact is safe to run**, only that it is the attested one with
  the declared guarantees. Deploy is an integrity-and-provenance mechanism, not
  a runtime sandbox (the sandbox rungs in `placement.py` are a separate,
  composable concern).

---

## 5. Adversarial self-review

Five attacks. The CRITICAL is C1; it is the one that reshaped the front matter.

### A1. The signer that is also a verifier (CRITICAL-adjacent, forces S2.4)

**Attack.** Item 127's signature is HMAC-SHA256, symmetric. A remote host must
verify the attestation, so it must hold the key, so it can mint an attestation
binding ANY bytes as "admitted" with any guarantees. The "signer untrusted"
rejection rule (S2.2) is then vacuous: every host that can check is a host that
can forge, and a single compromised host's leaked key forges deploys for the
whole fleet.

**Verdict / fix.** Real, and it makes one of the six named rejection rules a
fiction if ignored. `attest.py` reserved `alg` for the asymmetric upgrade;
deploy REQUIRES it (Ed25519): signer holds the private key, hosts hold only
public verify keys in their trust store. Recorded as a hard prerequisite in
Slice 1 (S6). Not a runtime bug in the design, but a shipping-blocker if the
HMAC scheme is carried over unchanged.

### A2. The host that lies about admission (CRITICAL - C1)

**Attack.** A compromised host runs the `deploy-admit` handshake, returns a
signed ACCEPT receipt naming the correct artifact hash, then loads a DIFFERENT
artifact (a backdoored binary) at COMMIT. Every conductor-side check passes; the
seam comes up; the fleet believes it is running the attested composition. The
whole chain (S2) proves what the host was ASKED to run, not what it ran.

**Verdict.** This is the CRITICAL, and it is a genuine boundary, not a fixable
bug: the host is the trust domain that executes, and no protocol run BY that
host can prove it did not lie to itself. The honest design consequences, all
adopted:

1. The G-invariant (S4) explicitly does NOT cover the host's actual runtime;
   the boundary is named, not papered over.
2. Deploy makes the lie DETECTABLE and NON-REPUDIABLE, not impossible: the
   COMMIT receipt is signed by the HOST's own key and binds the artifact hash it
   claims + its runtime_versions + the correlation nonce, so a later audit (or a
   second independent attestor) has a signed record to catch an honest-but-buggy
   host and to attribute a malicious one.
3. It bounds the blast radius by construction: the mTLS identity means a lying
   host can only serve ITS slice's keys, and the capability ceiling (S2.2) means
   it cannot widen authority even while lying about bytes.
4. Genuine defense against a malicious host needs hardware the host cannot forge
   (TPM/TEE remote attestation of the loaded image) - explicitly out of scope
   and named as such, so the doc never implies deploy defends against it.

This is why the front matter leads with "revl guarantees the shape and the
chain, not the remote runtime." Stating it up front is the fix.

### A3. The partial multi-host deploy leaving inconsistent state

**Attack.** Three hosts. A and B COMMIT; C fails at COMMIT. The composition is
now half-live: A and B serve keys, C serves nothing, dependents of C's keys are
stranded. Worse, the conductor sends ABORT and A acks but B partitions - B keeps
running a superseded generation.

**Verdict / fix.** This is the item's headline risk and the reason for S3's
whole shape. Handled, honestly: PREPARE being effect-free means the ONLY way to
reach this state is a COMMIT-phase failure (rare - admission already passed).
When it happens, A and B each roll their own slice back LIFO (proven per-host);
an unreachable B is reported as `unresolved(partition)` with its last receipt,
NOT as rolled back, and a `revl recover` on B settles it from B's own WAL. The
design's contribution is not "this never happens" (it can) but "the aggregate
verdict names exactly which hosts are clean and which are residue, and never
claims an atomicity it did not achieve." Slice 1 sidesteps this entirely by
allowing exactly one remote host (S6).

### A4. The bridge that trusts the wire

**Attack.** The seam is mTLS, so the operator concludes the transport already
guarantees integrity and skips the host-side attestation check, trusting "the
peer holds a CA cert, therefore the artifact is fine." Or: the bundle is staged
over the SSH orchestration channel and the operator treats reaching the host as
proof the bytes are the attested ones.

**Verdict / fix.** A category error the design must actively prevent. mTLS
authenticates the two PROCESSES to each other (item 56); SSH authenticates the
OPERATOR to the MACHINE; neither says anything about the ARTIFACT. The three are
kept structurally separate (1.2), and the host recomputes the attestation chain
LOCALLY over the staged bytes (S2.4) regardless of how they arrived - a bundle
that arrives over a perfect mTLS channel but whose bytes do not match the signed
`artifact/<backend>` binding is still refused. The host trusts the signature,
never the channel.

### A5. Stale but validly-signed evidence

**Attack.** An attestation signed months ago, over a bundle whose gauntlet
evidence predates a since-discovered miscompile or a revoked signer key, is
replayed to a host. The signature is authentic; the chain binds; the host admits
a composition whose evidence is no longer trustworthy.

**Verdict / fix.** Real, and the reason `evidence stale` and `signer untrusted`
are two of the six rejection rules. The host's trust store carries a freshness
TTL and a revocation list; the bound `gauntlet`/`conformance` timestamps are
checked against the TTL (S2.2), and a revoked `key_id`/verify-key is refused
even with a valid signature. A signature proves authenticity, never
current-validity; the host owns the freshness policy, not the artifact.

---

## 6. Sliced plan

### Slice 1 - single remote host, over the item-56 bridge, with chain verify

The smallest landable, honest cut. A deploy map with **exactly one** process
carrying a `[deploy].via = ssh` (or `local`) target; every other component stays
local and is spawned by the unchanged `run_placement`. The remote host receives
the sliced bundle over SSH, runs `revl verify` + the attestation-chain verify
locally against its trust store, does a LOCAL `apply` of its single slice
(writing its own WAL), and returns a signed admission + commit receipt.

Why this is the right Slice 1:

- **Rollback is a proven property, not an orchestration.** With one remote host,
  the "distributed rollback" is that host's own local `apply`/`recover` LIFO -
  the exact theorem `apply.py`/`recovery.py` already prove. The whole S3 honesty
  problem (partial multi-host state) simply does not arise: either the one host
  admits and commits, or it refuses/rolls-back-locally, atomically, at that host.
- **Almost pure reuse.** It reuses 305 (`bundle`/`verify`), 306 (cert), 127
  (`attest` + `evidence_bindings`), 56 (network seam), 54/55 (deadline/identity),
  and `apply`/`recover` unchanged. The genuinely new code is small: (a) the
  deploy-map `[deploy]` table + an SSH launch of `deploy-admit`; (b) host-side
  chain verification extending `attest` with the artifact/policy/evidence
  bindings; (c) the signed admission/commit receipt.
- **It proves the chain end to end** - the item's real novelty - on a real
  machine boundary, which is what the STRATEGIC NOTE says unlocks the
  productionization cluster.

**Hard prerequisite inside Slice 1:** the Ed25519 (asymmetric) upgrade to
`attest.py` (S2.4 / S5-A1). Deploy cannot ship on the symmetric HMAC scheme
without making "signer untrusted" a fiction. This is the one non-reuse dependency
that must land with (or before) Slice 1.

### Deferred (named, so the boundary is explicit)

- **Multi-host orchestration + the two-phase commit/abort** (S3): the full
  cross-host PREPARE/COMMIT with the `unresolved` residue verdict. Needs > 1
  remote host; deferred because Slice 1's single host makes rollback a proven
  local property and multi-host makes it a best-effort orchestration - a
  different and larger honesty surface.
- **Container / microVM deploy targets** (`[deploy].via = container|microvm`):
  reuses the isolation rungs already in `placement.py`, but image build/push is
  a separate transport from SSH; deferred behind the SSH cut.
- **Transactional multi-composition federation** (folded external #5): the
  all-or-nothing update across several compositions. It is S3's shape
  generalized past one plan; deferred with multi-host.
- **Distributed effect correlation as a first-class audit surface** (folded
  external #6): the correlation header (1.4) is defined here as what makes
  cross-host recovery auditable, but the OTel/incident tooling on top (items
  120/121) is downstream.
- **Hardware remote attestation** (TPM/TEE) of the loaded image (S5-A2): the
  only thing that would defend against a malicious host, explicitly out of
  scope; deploy's guarantee stops at detectable/non-repudiable.

### Relationship to item 253 (the cheap alternative)

The roadmap notes item 253 (Temporal emission target) gives
durable/distributed/compensating execution on someone else's cluster with NO
transport to build. If distribution pressure arrives before Slice 1's remote
transport is affordable, 253 is the answer and 118 stays deferred. Slice 1 is
worth building only when the attested-supply-chain identity (S2) - deploying
only artifacts whose source, guarantees, capabilities, and runtime target are
cryptographically linked - is itself the goal, which is a provenance property
253 does not provide.

## Addendum: review-pass sharpenings

A second independent design pass (branch design/118-revl-deploy-review) re-derived
this item and contributed three sharpenings, folded here so this doc is the single
authoritative design.

1. **The TCP+mTLS transport already exists, which reshapes the slice math.** Item 56
   landed a TCP+mTLS bridge in backends/python/bridge.py (serve / proxy_component /
   _Client over UDS or a static TCP+mTLS Endpoint with a TlsConfig). Slice 1 therefore
   does not need to build a data-plane transport at all: it reuses a STATIC Endpoint.
   What Slice 2+ still owns is the CONTROL plane (remote orchestration / SSH-container
   launch, which network-placement.md makes an explicit non-goal today), the replicated
   WAL across nodes (none exists anywhere in the tree), and a partition-safe distributed
   commit coordinator. The heavy part is replication + quorum, not the socket.

2. **A second CRITICAL: federation split-brain under partition.** Distinct from this
   doc's primary CRITICAL (the LIFO rollback theorem apply proves is an IN-PROCESS
   property and does not cross a seam, so a remote host's inverses live in that host's
   process and cross-seam rollback needs a coordinated protocol, not a lift of apply.py's
   theorem). The federation additional CRITICAL: if a composition crosses an irreversible
   emission during its local commit BEFORE a durable federation-wide decision exists, a
   late peer failure or partition strands it (it carries residue and disagrees with
   reverted peers about the committed generation), falsifying the whole-federation-reverts
   guarantee. Fix: no composition may cross an irreversible effect until one durable
   federation-commit-approved record exists (quorum-durable in Slice 2+); PREPARE holds
   every irreversible crossing as an item-245 class-(b) deferred emission; a stranded
   participant applies recovery.py's existing rule verbatim (record present -> roll
   forward, absent -> roll back) and fails closed on a guess, so split-brain is
   impossible; and a change that necessarily crosses a non-deferrable irreversible effect
   is REFUSED admission into a federation plan rather than silently degrading atomicity.

3. **Two binding rules for the attestation + correlation planes.** (a) A receiving host
   must RE-HASH the IR and the artifact bytes it will actually execute and never trust
   the attestation's self-declared backend or artifact_hash; the signed chain is checked
   against the bytes in hand, not the bytes the sender claims. (b) Correlation envelopes
   are AUTHENTICATED against the mTLS peer identity, and duplicate detection is scoped on
   (peer_identity, composition_id, generation, idempotency_key), so a correlation
   identity cannot be forged or replayed by a peer other than the one the mTLS session
   authenticates.
