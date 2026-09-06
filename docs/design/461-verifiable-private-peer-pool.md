# 461 (provisional): verifiable private peer pool

**Provisional roadmap id.** This note is filed against GitHub issue
[#480](https://github.com/inso1337/revl/issues/480) ("Verifiable private peer
pool: signed peer attestation + lawful-retry dispatcher +
authority-monotonicity invariant"). The number 461 is a placeholder chosen so
the file sorts after the last provisional design note (460); the orchestrator
assigns the real item number at merge and renames this file. Every reference
to "this item" below means issue #480.

Design only: no compiler change, no `src/` change, nothing implemented.
Companion docs:
[118-revl-deploy.md](118-revl-deploy.md),
[411-seam-transport.md](411-seam-transport.md),
[442-typed-delegation.md](442-typed-delegation.md),
[245-session-commit.md](245-session-commit.md),
[460-two-phase-admission-forward-recovery.md](460-two-phase-admission-forward-recovery.md),
[../deploy.md](../deploy.md),
[../network-placement.md](../network-placement.md),
[../capability-attenuation.md](../capability-attenuation.md),
[../../formal/STATUS.md](../../formal/STATUS.md).

Source of record for the claims about today's code (line numbers as of
`ee35cea`): `src/revl/deploy.py` (`TrustStore` at :479, `admit` at :561, the
capability-ceiling check at :817, `PeerAllowlist` at :1318,
`TransportReplayGuard` at :1428, `SeamAdmission` at :1505, `Participant` at
:1580, `run_deploy` at :1616, `federation_admission` at :1915,
`settle_stranded` at :2078, `TEARDOWN_PROMISE` at :2250, `DEPLOY_KEYS` at
:2317, `admit_deploy_map` at :2399 and its `machine-boundary` refusal at
:2452),
`src/revl/attest.py` (`SIGN_ALG = "hmac-sha256"` at :121, `key_id` at :203,
`checker_identity` at :326), `src/revl/recovery.py` (`REDISPATCH_FREE` at
:90, `_replay_tier` at :93), `src/revl/query.py` (`classify_compensation` at
:969), `src/revl/__main__.py` (`_recovery_audit_view`, the `revl audit
--recovery` replay-class table), `src/revl/placement.py`
(`generate_seam_certs` at :146, the per-process peer identity mint described
under item 421 F8), `src/revl/cap_order.py` (the `calls` ceiling at :157 and
its `requests` alias at :168), `src/revl/parser.py` (the extern
classification vocabulary at :2052), and
`formal/RevL/Theorems/CapCeilings.lean` (`attenuation_monotone` at :105,
`lineage_ceiling_le` at :121, `budget_never_exceeds_root_ceiling` at :171,
`no_star_amplification` at :206).

## 0. The decisions, in one table

| # | Question | Decision | Section |
|---|---|---|---|
| D1 | What is a peer offer | A signed `peer-offer` record bound to the mTLS peer identity; trust LEVEL is decided by the conductor's trust store, never claimed in the offer | §2.1 |
| D2 | Where the constraint lives | A sixth `[deploy]` key, `require`, read only under the new `via = "peer"`; `trust` stays the path to the trust store | §2.2 |
| D3 | When it is matched | Plan time in `admit_deploy_map`, then re-verified fresh at PREPARE; a stale or mismatching offer refuses the deploy | §2.3 |
| D4 | Unit of dispatch | A placed process (one `Participant`), whose effect class is the join over every crossing its reach can make | §3.1 |
| D5 | Effect classes | The five the issue names, each mapped to a classification revl already computes; unknown maps to fenced | §3.2 |
| D6 | Retry on peer loss | A pure decision function over (class, settle verdict, attempts left, candidate peer level); replay only where the class permits, compensate only after the lost peer's own verdict is known | §3.3 |
| D7 | Retry budget | An item-260 ceiling parameter `attempts`, so it attenuates down the lineage with no new machinery | §3.4 |
| D8 | The invariant | Peer placement is a spawn edge in `CapCeilings.Lineage`; time, money and retry budget are ceiling parameters; one new theorem binds the receiver-side ceiling check to the lineage | §4 |
| D9 | Signing | HMAC-SHA256, single trust domain, `cross_domain` refusal kept; the asymmetric half of peer identity comes from mTLS, and the offer is bound to it | §6 |

## 1. The gap, stated against the code

revl can already deploy a slice of a composition into a process it does not
own, verify what that process is about to run, and coordinate a commit across
several of them. What it cannot do is choose the process.

**Attestation goes one way.** `admit` runs on the RECEIVING side: a
`TrustStore` holds the receiver's verify keys, its capability ceiling and its
freshness policy, and the receiver re-hashes the staged bytes before anything
loads. Nothing runs on the SENDING side to ask whether the receiver is a
process the conductor should hand a slice to. The placement map can name a
process (`[processes.<p>]`), pin the identity it must present on a network
seam (`[tls] identity`, checked by `PeerAllowlist`), and say which signer
trust store the far side verifies against (`[deploy] trust`, a filesystem
path). It cannot say "this slice needs a peer that runs the `wasm` tier, sits
in region `eu`, has a GPU, and is verified against my keys". There is no
record a peer could sign to make such a claim, and no matcher to check one
against a constraint.

**Retry has the classification and not the dispatcher.** Item 309 journals
every effect; `_replay_tier` decides `read` / `free` / `fenced` per journalled
call; item 440 adds the `read` register and the re-issue seam behind an
operator knob; item 245 splits emissions into witnessed (a), deferred (b) and
immediately-irreversible (c); `classify_compensation` partitions crossings
into bare / compensated / unresolved; and `revl audit --recovery` prints the
whole replay-class table. All of that decides what may be re-issued INSIDE one
process after a crash. `run_deploy` decides nothing about it across processes:
when a participant cannot be reached it is reported `unresolved(...)` and the
deploy stops. There is no code path that says "the slice that was on the peer
we lost is pure, so run it on another one", and no code path that refuses to.

**The invariant is proved and not bound.** `CapCeilings.lean` proves that an
admitted lineage never exceeds the root's declared authority
(`attenuation_monotone`), that every ceiling parameter only shrinks down the
lineage (`lineage_ceiling_le`), that the runtime counter composes with it
(`budget_never_exceeds_root_ceiling`), and that the host boundary `*` is never
manufactured (`no_star_amplification`). Its `Lineage` is built from
`Attenuates held reach` edges, and the only edges the derivation reads today
are in-process spawns. A peer placement is not an edge in that relation, so
the theorems say nothing about a peer, and the deploy-side ceiling check at
`deploy.py:817` (the receiver refuses a bundle whose wanted capabilities
exceed `TrustStore.capability_ceiling`) is a runtime check the proof does not
know about.

The three gaps are exactly the three primitives the issue adopts. Everything
else in the original proposal (discovery, marketplace, gossip, reputation,
settlement, public federation) is out of scope, and §7 says so again.

## 2. Primitive 1: signed peer offer, matched against placement

### 2.1 The record

A peer offer is a signed JSON document produced by the peer's own conductor
and presented to the delegating conductor. Its shape reuses item 127's
envelope discipline: a `kind`, a `version`, a canonical body, a `sign_alg`
that is checked and not merely recorded, a `key_id`, and a signature over the
domain-separated canonical bytes of the body.

```
{
  "kind": "peer-offer", "version": 1,
  "peer_identity": "worker-3",              # MUST equal the mTLS-proved identity
  "issued_at": "2026-09-06T10:00:00Z",
  "expires_at": "2026-09-06T10:05:00Z",
  "checker": {...},                           # attest.checker_identity(), a CLAIM until §2.4
  "capabilities": {
    "backends": ["py", "wasm"],
    "sandbox": ["container"],                 # item 411 rungs this peer can open
    "region": "eu-west",
    "hardware": ["gpu"],
    "offer": {"calls": 1000, "budget.bytes": 5000000, "attempts": 3,
              "seam_deadline": 30.0}
  },
  "sign_alg": "hmac-sha256", "hash_alg": "sha256",
  "key_id": "...", "signature": "..."
}
```

Three members are deliberately absent, and a matcher structurally refuses to
read them if present, the way `SELF_DECLARED_IGNORED` already works for
admission:

- **`trust`.** The offer never says how trusted it is. Trust level is a
  verdict the conductor's trust store produces about the offer's signer
  (§2.3). An offer that carries a `trust` member is refused as malformed, not
  ignored.
- **`artifact_hash` / `composition_hash`.** An offer is not an attestation of
  what the peer will run; that is what `admit` produces, on the peer, at
  PREPARE. Conflating the two would let a peer assert admission.
- **Any price or reputation member.** Out of scope, and an offer that carries
  one is malformed so the vocabulary does not grow by accident.

`peer_identity` is the load-bearing binding. The network seam already proves
an identity asymmetrically (the mTLS handshake, `PeerAllowlist.admit`). The
offer must name that identity, and the verifier compares the two. An offer
whose `peer_identity` differs from the handshake-proved one is refused with a
reason naming both. This is what stops a pool member who holds the shared HMAC
key from advertising capabilities on behalf of a different member (§6).

### 2.2 The constraint

`DEPLOY_KEYS` grows by one, `require`, and `KNOWN_VIA` by one, `peer`. Both
are additive: a placement with no `via = "peer"` parses exactly as today, and
the existing rule that an unrecognised `[deploy]` key refuses the map keeps
applying to the keys INSIDE `require`.

```toml
[processes.scorer.deploy]
via = "peer"
trust = "trust/pool.json"        # unchanged meaning: the signer trust store PATH
[processes.scorer.deploy.require]
trust = "verified"               # the LEVEL demanded, §2.3
backend = "wasm"
region = "eu-west"
hardware = ["gpu"]
```

`require.trust` and the top-level `trust` are different things and the doc
must keep saying so, because the issue's own wording (`trust >= verified`)
reads like the existing key. The top-level key is where the keys live; the
`require` key is what the keys must establish. A `via = "peer"` target with no
`require` table is refused ("a peer target with no requirement is a target
any peer satisfies, which is a pool, not a placement"), and `require.trust` is
mandatory inside it. A `require` table on any other `via` is refused the way
`local-with-remote-fields` is today.

The resource half of `require` is not written by the operator. It is derived:
the slice's declared ceilings (item 260, `calls` / `budget.*`), its seam
deadline (`seam_deadline`, item 54), and its `attempts` ceiling (§3.4) are the
demand, and the offer's `capabilities.offer` must cover every one of them. An
offer that omits a ceiling the slice demands reads as `+∞` FOR THE OFFER, which
is the wrong direction for a resource offer, so an omitted key is a mismatch,
not a pass. This is the one place the offer vocabulary is stricter than the
capability lattice, and the matcher says why in its refusal.

### 2.3 Verification and matching

`verify_offer(offer, *, trust: TrustStore, transport_identity, now) ->
OfferVerdict` runs on the conductor, in this order, refusing at the first
failure with a reason in the same `link` vocabulary `admit` uses:

1. envelope shape, `kind`, `version`, `sign_alg`, `hash_alg` (the
   `_validate_envelope` rule: a mislabelled algorithm is a mislabel, refused);
2. signer: `key_id` in `trust.keys`, not in `trust.revoked`;
3. signature over the canonical body;
4. identity: `peer_identity == transport_identity`;
5. freshness: `issued_at` within `evidence_ttl_seconds` and
   `clock_skew_seconds`, `expires_at` not passed, `not_before` honoured; the
   same three-way rule admission already applies, reused rather than
   re-derived;
6. vocabulary: no forbidden member (§2.1), no unknown top-level member.

The verdict carries the LEVEL the offer reached, and the levels are ordered:

| level | established by | what it means |
|---|---|---|
| `known` | mTLS identity in the placement's `PeerAllowlist` | the peer is one the placement named; nothing about what it can do |
| `verified` | steps 1 to 6 pass | a key in this conductor's trust store vouches for this identity's advertised capabilities |
| `attested` | `verified`, plus the peer's PREPARE returned an `admit` receipt whose `checker` matches the offer's and whose staged bytes the conductor can compare (118 R2's load-measured receipt) | the offer's toolchain claim is bound to what the peer actually admitted |

`require.trust` names the minimum level. A slice that carries a `Secret[T]`
seam value or a bound provider key demands `attested` whatever the operator
wrote, and the matcher raises the demand rather than the operator lowering it
(§3.2, the secret-bearing class).

`match_offer(require, demand, verdict) -> MatchVerdict` is a pure function:
level at least the demanded level; `backend` in `capabilities.backends`;
every demanded sandbox rung in `capabilities.sandbox`; `region` equal;
`hardware` a subset; every demanded ceiling covered by the offer. It returns
every failing constraint, not the first, so an operator sees the whole
distance between the pool and the placement in one run.

The `attested` level cannot be reached at plan time, because there is no
PREPARE yet. So `admit_deploy_map` matches at plan time against `verified` at
most and records `attested` as PENDING for any target that demands it; PREPARE
then either raises the level or refuses the deploy. This is the same
two-stage shape `federation_admission` already has for deferred emissions:
refuse at plan time what can be refused, hold at PREPARE what can only be
known then, and never degrade silently between the two.

### 2.4 What the checker identity in an offer is worth

`checker` in an offer is `attest.checker_identity()` as the PEER reports it:
the compiler version and ruleset digest of the frontend the peer says it
runs. Until PREPARE returns an `admit` receipt the conductor can compare, it
is a claim. The offer format carries it so that a mismatch is refused EARLY
(an offer from a peer on an older ruleset does not survive plan time), and
the `attested` level is what upgrades it from a claim to a checked fact. The
doc for the feature must say this in those words; a `verified` offer is not a
verified toolchain.

## 3. Primitive 2: the lawful-retry dispatcher

### 3.1 Unit of dispatch

The dispatcher decides about a placed PROCESS, not a call. A peer runs a
`Participant`, the coordinator's ledger records participants, and the loss the
dispatcher reacts to is a participant becoming unreachable (a seam-deadline
breach, an mTLS disconnect, a PREPARE or COMMIT that does not answer). The
class of a participant is the JOIN over every boundary crossing the slice's
reach can make, taken from the IR the same way `_recovery_surface` already
enumerates inverses, deferred emissions and compensations. The join is the
strictest class present: one fenced crossing makes the whole slice fenced.

This is coarser than a per-call dispatcher and it is chosen on purpose. A
per-call dispatcher across a process boundary would need the coordinator to
hold per-call state about a remote participant, which is exactly the state
§3 of the 118 design says the coordinator is not entitled to hold. Per-slice
classification keeps the coordinator holding a ledger and a class table and
nothing else.

### 3.2 The classes, mapped to what exists

| issue's class | revl classification it reads | on loss of the peer | which peers may run it |
|---|---|---|---|
| pure | every crossing is `pure`, or carries `register: "read"` (item 440) | replay freely on another matching peer, within `attempts` | any at or above `require.trust` |
| idempotent-external | every crossing is keyed or declared idempotent (`REDISPATCH_FREE`, item 309 `replay: free`) | bounded retry, at most `attempts`, each carrying the same `idempotency_key` in its `Correlation` | any at or above `require.trust` |
| witnessed / revertible | a `witnessed` crossing with an inverse (item 243) or a compensation (item 247), item 245 class (a) | NO re-dispatch until the lost peer's own settle verdict is known; `rolled-back-clean` permits one more attempt elsewhere, `rolled-back-with-residue` and `unresolved` do not | any at or above `require.trust` |
| deferred-irreversible | item 245 class (b) deferred emissions | never re-dispatched; the tail is held at the peer under PREPARE and only the conductor's COMMIT flushes it, so a peer lost before COMMIT drops it for free and one lost after COMMIT is settled by `settle_stranded` | the peer holds the tail; only the conductor (the trusted commit authority) fires it |
| secret-bearing | a `Secret[T]` value or a bound provider key crosses the slice's seam (G-SECRET / G-SECRET-FLOW) | as its underlying class, but the candidate set is fixed at plan time | `attested` peers only, else local; refused at plan time otherwise |
| (fenced) | anything else: an immediately-irreversible class (c) crossing with no key, an unknown classification, an `unresolved` compensation | one fenced at-most-once attempt already spent; `outcome: unknown`, human-finish | none; the dispatcher never picks a second peer |

The last row is the fail-closed default and it is the fall-through, not a
special case, the same way `_replay_tier` returns `fenced` for absent input.
The vocabulary above adds no new classification to the language: every row
reads a field the frontend already computes and `revl audit --recovery`
already prints.

The witnessed row is the one that needs the most care. A witnessed crossing
on a lost peer may have landed, may have been undone by the peer's own local
LIFO unwind, or may be neither. The conductor holds no inverse and cannot
find out by itself; it can only ask, and `settle_stranded` already says what
a stranded participant does with the durable decision record. So the
dispatcher's rule is "compensate only after the verdict, replay only after a
clean verdict", which is the cross-process form of item 309's "a second
attempt cannot be proven safe" rule. Durable handoff means: the ABORT
decision record exists before any second peer is contacted.

### 3.3 The decision function

```
decide(cls, verdict, attempts_left, candidate_level, require_level)
  -> Replay(peer) | Settle(peer) | Compensate(peer) | HumanFinish(reason)
```

is pure, total, and the only place the table above is encoded. It is what
`run_deploy` calls on participant loss once the network seam exists (§5,
slice 3), and it is what `revl audit --dispatch` prints per process at plan
time, so an operator reads the same decisions the runtime will make. The
audit view is the item-309 `--recovery` table with two more columns: the
slice's joined class and the peer levels it may be placed on.

### 3.4 The retry budget is a ceiling

`attempts` is registered in `cap_order` as a ceiling parameter, next to
`calls`. That is the whole of the retry-budget mechanism. A slice's
`attempts` is declared where its other ceilings are, attenuates on spawn the
way `calls` does, is refused at plan time when a child exceeds its parent, and
`lineage_ceiling_le` covers it with no new proof. The dispatcher decrements it
per re-dispatch and refuses at zero. The runtime counter is the same
`remainingUses`-style counter `spend_within_budget` models.

The alternative, a separate retry table in the placement map, was rejected
because it would be a second authority vocabulary with no theorem behind it.

## 4. Primitive 3: the authority-monotonicity invariant

### 4.1 Statement

For every peer-placed slice S delegated by a composition C:

- **authority**: every capability S holds is covered by one C declared
  (`attenuation_monotone` over a lineage that now includes the peer edge);
- **budget**: for every ceiling parameter k in {`calls`, `budget.*`,
  `attempts`, `seam_deadline`}, `ceiling_S(k) <= ceiling_C(k)`
  (`lineage_ceiling_le`), and S's spend against its counter never exceeds
  C's root ceiling (`budget_never_exceeds_root_ceiling`);
- **data**: S receives only what crosses the value-copy seam under the G8
  allowlist, and a `Secret[T]` crosses only to an `attested` peer
  (G-SECRET-FLOW, unchanged; the dispatcher enforces the level);
- **no host reach**: S never holds `*` unless C did (`no_star_amplification`).

"Time" and "money" in the issue's wording are ceiling parameters here:
`seam_deadline` is the time a delegated call may take and it is bounded by
the caller's remaining deadline; money is whatever `budget.*` ceiling the
composition declares for it. The invariant does not invent a currency.

### 4.2 What exists, and what is new

Three of the four bullets are proved today for in-process lineages. The new
formal work is small and stated so it stays small:

1. **`attempts` and `seam_deadline` become ceiling parameters in the model.**
   `CapCeilings` is generic over the parameter name `k`, so this is a
   registration in `cap_order` and two non-vacuity rows, not a new theorem.
2. **The peer edge is a `Lineage` edge.** A `via = "peer"` placement is an
   admitted spawn whose `held` is the delegating process's reach and whose
   `reach` is the slice's declared surface. The derivation in
   `CapCeilings` §"Deriving held and reach from the program text" is
   extended with the placement's per-process reach, the same surface
   `placement.py` already computes as a process's proxied and served keys.
3. **One new theorem, `dispatch_within_lineage`.** The receiver's own
   ceiling check (`deploy.py:817`, `TrustStore.capability_ceiling`) is
   modelled as a second `CeilingOK` predicate on the peer edge, and the
   theorem states that a slice admitted by BOTH the conductor's attenuation
   check and the receiver's ceiling check is a `Lineage` descendant of the
   conductor's root. Its content is that the two checks compose rather than
   one subsuming the other, which is the same shape as
   `ceiling_check_not_subsumed`.
4. **An oracle row.** The differential harness gets a row that runs
   `match_offer` over a corpus of offers and placements and diffs the
   shipped verdict against the model's, with a coverage ratchet that fails
   the gate unless the corpus contains both an admitted match and a refused
   widening (the `attenuation_coverage` pattern).

The issue calls this a G-theorem candidate. It is not added to
`diagnostics.GUARANTEES` in this item: adding a code there regenerates the
`guarantees-design` docgen block in `DESIGN.md`, and a guarantee row with no
proved theorem and no oracle row is exactly the kind of row
`formal/STATUS.md` exists to refuse. The candidate is recorded here under the
working name `G-AUTH-MONO`, and promoting it is a follow-up gated on slice 4
(§5) being green.

### 4.3 What the theorem does not say

The theorem is about what the conductor GRANTS. It says the conductor never
hands a peer more than it holds, and that the peer's own admission never
widens that. It does not say the peer runtime spends within its counter,
because the peer runtime is a trusted enforcer in exactly the sense item 411
uses for the container runtime: revl declares and refuses, the far side
enforces, and revl cannot verify enforcement from outside. A peer that lies
about its spend is a peer that has left the private pool's trust assumption
(§6), not a counterexample to the theorem.

## 5. Slices, with exit tests

Every slice is additive and lands behind `via = "peer"`: a placement that
does not say it is byte-identical in behaviour before and after.

**Slice 0: the dependency gate (nothing lands).** Until §7's dependencies
land, the `machine-boundary` refusal in `admit_deploy_map` stays, and
`via = "peer"` is refused with a reason naming this doc and the missing seam.
Exit test: the existing machine-boundary test keeps passing, plus one new
test asserting `via = "peer"` is refused by name and not treated as `local`.

**Slice 1: offer record, verifier, matcher (pure, no network).**
`src/revl/peer_offer.py`: `make_offer`, `verify_offer`, `match_offer`, the
`require` parsing in `parse_deploy_map`, `admit_deploy_map` matching at plan
time with `attested` recorded as pending.
Exit tests:
- a tampered body refuses on the signature link; a mislabelled `sign_alg`
  refuses on the envelope link;
- an unknown or revoked `key_id` refuses on the signer link;
- `peer_identity` different from the transport identity refuses, naming both;
- expired, post-dated beyond skew, and older-than-`not_before` offers refuse;
- an offer carrying `trust`, `artifact_hash`, or a price member is malformed;
- `require.trust = "verified"` against an offer signed by an untrusted key
  refuses; region mismatch, missing hardware, and a missing offered ceiling
  each refuse and the verdict lists all of them at once;
- `via = "peer"` with no `require` refuses; `require` under `via = "local"`
  refuses; an unknown key inside `require` refuses;
- a slice carrying a `Secret[T]` seam value with `require.trust =
  "verified"` is refused at plan time with the demand raised to `attested`
  in the reason.

**Slice 2: the class table and the decision function (pure).**
`src/revl/dispatch.py`: the per-slice join over `_recovery_surface` and the
`Secret[T]` seam walk, `decide`, `revl audit --dispatch`. `attempts`
registered in `cap_order`.
Exit tests:
- one slice per row of the §3.2 table, each asserting the decision on loss;
- an unknown classification joins to fenced and `decide` returns
  `HumanFinish`;
- a witnessed slice with verdict `unresolved` returns `Settle`, never
  `Replay`; with `rolled-back-clean` returns `Replay` once and then
  `HumanFinish` when `attempts` reaches zero;
- a deferred slice never returns `Replay` for any verdict;
- a child declaring `attempts` above its parent's is refused by the existing
  ceiling attenuation check with no new code path;
- `--dispatch` and `--recovery` agree on every row they share (the two views
  read one surface).

**Slice 3: wiring into `run_deploy` over the network seam (after §7).**
A `PeerParticipant` that stages the bundle, drives `admit` on the peer, and
returns the receipt; PREPARE raises `verified` to `attested` or refuses;
participant loss calls `decide`; the ABORT decision record is written before
any second peer is contacted.
Exit tests:
- boots through the real `run_placement` entry point with `via = "peer"`
  and asserts the offer is verified against the handshake identity, not a
  hand-built one (the item 421 F8 test shape: a hand-built verifier would
  reproduce the gap that shipped dead);
- kill the peer mid-COMMIT for a witnessed slice: the ledger shows
  `unresolved`, no second peer receives a PREPARE, and the decision record
  exists on disk;
- kill the peer mid-COMMIT for a pure slice: a second peer is dispatched,
  the ledger shows both rows, `attempts` decremented by one;
- an `attested` demand whose PREPARE receipt names a different `checker`
  than the offer refuses the deploy;
- the hostile-wire TCK (#475) gains rows for the offer envelope: truncated,
  duplicated, reordered against the PREPARE it precedes, and each refuses
  with no residue.

**Slice 4: the formal binding.**
`attempts` and `seam_deadline` as parameters in the model, the peer edge in
the derivation, `dispatch_within_lineage`, non-vacuity rows, the oracle row.
Exit tests: `make formal` green with the axioms gate and the non-vacuity gate
passing for every new name; the harness row agrees with `match_offer` over
the corpus and the coverage ratchet bites when the refused-widening case is
removed.

## 6. Posture, stated plainly

**Loopback reachability.** Every network seam revl can stand up today is
tested on loopback: `generate_seam_certs` mints certificates for
`127.0.0.1` and `localhost`, and `docs/network-placement.md` says real
deployments supply their own. The 411 T3 measurement found that a
`--network=none` container has no route to the host's loopback at all and
that Docker Desktop shares no namespace with the macOS host. A private pool
whose peers are on other machines is therefore reachable only through
whatever #107 T3 lands, and this doc does not claim otherwise. Slice 1 and 2
need no network; slice 3 does, and waits.

**Trusted parties.** A private pool is one trust domain: the operator who
runs the conductor also enrols the peers, distributes the trust store, and
owns the machines. That is the assumption under which every refusal in this
doc is a real refusal. Inside it, a peer is a trusted enforcer of the
ceilings it is granted, the same way the container runtime is a trusted
enforcer of `--network=none`. A pool that admits a peer the operator does
not control has left the assumption, and nothing here compensates for that;
the open-federation items the issue defers are where that work would go.

**HMAC, not Ed25519.** `attest.SIGN_ALG` is `hmac-sha256` and 118 R3
records why the asymmetric migration is a real item and not a flag flip. The
consequence for offers is exact: any pool member that can verify an offer
can also forge one, so `verified` means "signed by a key the pool shares",
which is a pool-membership claim, not a per-peer claim. Two things keep that
honest. First, the offer is bound to the mTLS identity, which IS asymmetric,
so a forged offer can only advertise capabilities for the forger's own
identity and can never impersonate another peer. Second, the
`cross_domain` refusal in `admit` stays: a trust store that declares the
signer a different domain is refused under HMAC, so a pool cannot be quietly
extended across a domain boundary before the migration lands. Ed25519 is a
prerequisite of the open-federation future, not of this item.

**What `verified` does not verify.** It does not verify the peer's
toolchain (that is `attested`, §2.4), does not verify the peer's spend, and
does not verify that the hardware or region members are true. A peer that
lies about having a GPU is caught, if at all, by the slice failing on it,
and the doc for the feature must not suggest the matcher checks more than a
signature and a vocabulary.

## 7. Dependencies, and the order they land in

This item cannot ship slice 3 on today's tree, and it should not pretend to
by narrowing the seam. In order:

1. **#421 F8, the network seam's peer admission, and #107 (item 411 T3), the
   seam transport across a container boundary.** Item 56's TCP+mTLS seam
   exists between processes the conductor spawns; the 411 T3 design decides
   how a seam reaches a sandboxed process at all and schedules the rungs.
   `admit_deploy_map` refuses a `machine` boundary today because there is no
   bundle staging, no remote `deploy-admit` runner, no load-measured signed
   COMMIT receipt, and no pinned host key. A peer is a machine boundary.
   Until those land, slice 3 has nothing to dispatch over. **These land
   first.**
2. **#475, the hostile-wire TCK.** The offer envelope is a new wire shape
   crossing an adversarial seam, and the issue is right that a peer pool is
   the TCK's natural consumer. Slice 3's envelope rows go into that section
   rather than into a private test, so they are property-based from the
   start. Lands before or alongside slice 3.
3. **118 R2 / R3.** The load-measured COMMIT receipt is what makes
   `attested` a checked level rather than a repeated claim; the Ed25519
   migration is NOT a dependency of this item (§6) and is named so it is
   not silently re-added.
4. **#439 (A2A binding) is not a dependency.** A peer in this pool is a
   revl composition speaking the revl seam. 442 §9 already records that a
   delegation to an external agent is a delegation to an untrusted author
   with nothing checked about it, and decides the remote half of delegation
   waits for 439. This item makes the same call: an A2A peer is out of scope
   here and, if it is ever in scope, it is in scope under 439.

Slices 1, 2 and 4 depend on none of the above and can land in any order
among themselves; slice 4 is most useful after slice 1 so the oracle row has
a shipped matcher to diff against.

## 8. Out of scope, restated

Open marketplace, gossip discovery, reputation, economic settlement, public
federation, A2A peers, a per-call dispatcher, and any new extern
classification. Each is either infrastructure rather than a language
feature, or a widening of the trust assumption in §6, and the issue's
verdict on the monolith holds: extract the three primitives, keep the
federated research agent as a north-star demo, and revisit the rest only
after these are dependable.
