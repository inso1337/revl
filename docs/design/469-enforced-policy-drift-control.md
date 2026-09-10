# 469: Enforced policy-drift control

Design note for roadmap item 469 (issue #821). It records the spike that pins
the gap, the one slice that lands with this note, and the two follow-ups that
stay design only until they are scoped on their own.

Item 127 makes admission a hash-level identity statement: a signed attestation
binds the composition, the checker, the ruleset and the published dossiers to
their hashes. This item asks the next question, the one admission does not
answer: does that statement still hold an hour later, once the process is
running, the model has been swapped and a peer has been upgraded. Item 469 is
therefore not a new kind of check. It is the same check, moved off the admission
path and made an invariant.

## The spike: what admission, deploy and reconcile already bind

Read directly, the repo already owns most of the pieces of this control, each
taken at one moment rather than continuously.

Admission binds identity, once. `attest.canonical_hash` (`src/revl/attest.py`,
line 192) hashes the post-lowering IR bytes: the stable identity of the
composition, invariant under source formatting. `attest.make_attestation` folds
that hash, the checker identity and the ruleset digest into a signed body, and
`evidence_bindings` (item 290) folds the per-facet sha256 of each published
dossier into the same signed payload, so a forged or copied dossier is caught at
verification rather than trusted. `verify_attestation` signs over exactly the
received members, so an altered, dropped or added member breaks the signature.
The verdict is `admitted`, and it is a statement about a moment.

The bytes are re-hashed on the receiver, at one handshake.
`deploy.artifact_digest` (`src/revl/deploy.py`, line 223) walks an emitted tree
without following symlinks, refuses a tree that carries a symlink or holds no
bytes at all, and folds each relative path and its bytes with explicit length
framing in sorted order. That is exactly the hash-level re-measurement item 469
wants, and it already runs wherever a receiving tier re-hashes what it is about
to run; `src/revl/_deploy_participant.py` records the outcome in a WAL. It runs
at deploy, and never again.

Drift is already a word here, at the structural level. `apply.drift(basis, live)`
(`src/revl/apply.py`) compares a plan's basis against the live composition and
reports `componentsAppeared`/`componentsVanished`, `loadOrderChanged` and
`provisionsAppeared`/`provisionsVanished`; `admission._Drift` with
`_service_compatible` and `_admit_service_replacement` does the interface
version of the same thing when a component is replaced. Both compare NAMES and
KEYS. A rewritten artifact whose component names, load order and provided keys
are unchanged is invisible to both, which is the honest boundary of a
structural check and the reason item 469 is stated over hashes.

Reconcile already has the posture, over liveness.
`reconcile.reconcile_liveness_from_world` (`src/revl/reconcile.py`, line 110)
reconciles an activation's WAL, the E-Stop latch and a durable causal trace
against the world, and answers `clean`, `refused` or `unresolved`, defaulting to
`unresolved` when the evidence does not settle the question. It never claims a
`live` verdict it cannot prove. That split (a pure function that reads durable
evidence and decides, while the runtime acts) and that conservatism are what a
drift control has to copy, because the response it decides is a withdrawal of
authority.

`estop.read_latch` is the durable operator halt a `suspend` response maps onto,
and `estop.TIERS_WITH_ESTOP` already says which tiers can honour one.

## What the spike leaves open

Three things, and they are the whole item:

* nothing re-measures a RUNNING subject. Admission's hashes describe the world
  at admission, and `deploy.artifact_digest` describes the bytes at deploy; a
  model, tool, policy, dependency or peer that changes underneath a live
  composition is measured by nothing;
* nothing names what a live composition IS, as a set of named subjects with
  digests, in a shape a re-measurement can be compared against;
* there is no vocabulary for the answer. `apply.drift` renders a structural
  difference for an operator to read, and `reconcile` answers about liveness;
  neither can say "this subject no longer matches, and the configured response
  is suspend".

## The slice that lands with this note

`src/revl/drift.py`, the pure half of the control, tested by
`tests/test_821_drift_control.py`. Nothing in it reads a clock, a process or a
filesystem (except the WAL round trip the tests run), so the whole surface is
decidable without a runtime, which is what makes this slice independently
correct rather than half of a monitor.

The admitted manifest is `{role: {name: digest}}` over the item's five roles:
`model`, `tool`, `policy`, `dependency`, `peer`. A row is `(role, name)`: the
role says what kind of thing was admitted, the name says which one. A digest is
lowercase sha256 hex, the spelling `deploy.artifact_digest` and
`attest.canonical_hash` already produce, so a producer and the re-measurement
cannot disagree about the encoding.

`detect(admitted, live)` is the verdict over a re-measurement:

* `matched`: the live subject hashes to the admitted digest;
* `drifted`: it was re-measured and does not;
* `missing`: the reading reports an admitted subject absent;
* `unexpected`: the live composition holds a subject admission never named;
* `unverified`: the reading did not produce this subject at all, or produced
  something that is not a digest.

The verdict over the whole manifest is `drifted` if any of the first four
exists, else `unverified` if any subject is unverified, else `matched`. The
guarantee it states is deliberately narrow:

> a subject that was re-measured and does not hash to its admitted digest is
> drift, and its status is `drifted`; a subject the admitted manifest names that
> the live reading reports absent is `missing`; a live subject admission never
> named is `unexpected`; a subject the live reading did not produce is
> `unverified` and is never reported as `matched`; the response fired is the
> strongest response the configured policy earns over the drift set.

The `unverified` row is the load-bearing one. Absence of evidence is not drift:
a blind spot is reported loudly, and the verdict is never `matched` while one
exists, but it fires no response unless the control configures one for it. The
alternative (reading an unmeasured subject as drift) makes a monitor that cannot
see into a model sandbox suspend a healthy production composition, which is how
a control like this gets turned off and stays off. The same rule is why
`why_runtime.liveness_expired(ceiling_ms, silent_ms) -> bool` refuses to call
silence a fault, and it is the posture a response that withdraws authority has
to keep.

The response vocabulary is the item's, plus the floor:

    none < read-only < re-admit < suspend < rollback

and the order is the claim. `read-only` narrows what the live composition may do
without stopping it. `re-admit` refuses to renew the admitted identity until
admission runs again, so acting continues only so far as admission re-proves the
composition. `suspend` removes the authority now rather than at the next
admission. `rollback` goes furthest: it reverts the running artifact set to the
pinned one instead of only changing what the running set may do.

`normalize_policy(policy)` reads `{"onDrift": ..., "byRole": {...},
"onUnverified": ...}`, defaulting to `onDrift: "suspend"` and `onUnverified:
"none"`. A response is a DOWNGRADE: `respond(report, policy)` fires, per subject,
the response its role or the default grants for its status, and the effective
response is the strongest of those and never more. A drifted subject cannot earn
authority nobody configured, and no code path raises a response above what the
policy declared; going back up requires a fresh admission, which this module
cannot perform. A policy key this control does not read is refused rather than
ignored, because a misspelt `onDrift` would otherwise leave the fail-closed
default in place while the operator believed they had configured something else.

`resolve(report, policy, rollback_to=None)` is the same decision plus what the
world supports. `rollback` is a claim that a pinned artifact exists to go back
to, so a rollback decision with no pinned digest is REFUSED rather than quietly
downgraded: going back to a target nobody pinned is not a rollback.

`wal_record(report, decision, generation=..., at=...)` is the durable record of
the decision, written in the shape `wal.read_wal` already reads (a `record`
member names the kind, so the ordinary reader returns it beside
`model-decision` records with no reader change, and `wal.model_decisions`
ignores it, so recovery's view of a WAL does not move). It records the verdict,
the response, the decisive subject, every subject that was not matched, the
counts and the policy that was in force, so a post-mortem can ask what the
control knew and what it was configured to do. `drift_records(records)` indexes
them in recorded order, and `render(report, decision)` is the operator's view.

This slice is the decision half only. It names the monitor that produces its
input and the actuators that carry its output out, and it does neither.

## Follow-up 1: the re-measurement, and the record it writes - NOT LANDED

The item's Exit line needs an induced hash drift to be DETECTED. That needs a
producer, and it is a separate slice because it is a runtime concern:

* the admitted manifest of a running activation has to be persisted where the
  monitor can read it, and it already is, in three parts: the composition row
  from `attest.canonical_hash` on the admitted IR, the artifact rows from
  `deploy.artifact_digest` per emitted tree, and the generation from the WAL
  header (`wal.read_wal(path)["header"]["generation"]`), which is what makes two
  drift records comparable;
* the cadence, and the per-role re-measurement: for `dependency` and `policy`
  the digest of the bundled bytes, for `model` and `tool` the artifact tree the
  tier actually loaded (again `deploy.artifact_digest`, which is why this control
  reuses it rather than inventing a second tree hash), and for `peer` the peer's
  attested composition hash, which a verifiable peer pool already carries;
* an UNMEASURABLE subject has to be reported as unverified rather than omitted,
  which means the monitor's reading has to distinguish "measured and absent"
  (`None`) from "not measured" (key absent). `detect` takes that distinction as
  its input; producing it honestly is the monitor's job.

Where the record goes is open in one respect worth stating: `wal.py` is a
READ-ONLY module, and `drift.wal_record` returns a record rather than writing
one. The writer is the runtime's, next to the effects and model-decision
records, and it has to seal a torn tail before appending the way
`_deploy_participant._raw` does (`_seal_torn_tail`, item 535).

## Follow-up 2: the declaration, and the actuators - NOT LANDED

The `driftControl` policy has to be DECLARED somewhere. Stage 1 fixes the shape
it has (`onDrift`, `byRole`, `onUnverified`, and the fail-closed defaults), so
what remains is where the key lives and how it is parsed:

* it belongs to the admitted COMPOSITION, not to a `revl.policy.Policy`
  document. A `Policy` is the boundary document `admission.admit_under_policy`
  enforces (`src/revl/admission.py`, line 455) and holds no IR; what
  `revl attest` signs and `revl load` admits is the composition. Item 469 says
  "policy" in the sense of the whole declared policy of a composition, which is
  the IR plus its boundary policy, so the control is declared on the composition
  and travels with the hashes it protects;
* the source-level clause and the JSON key that fill it are open, and should
  follow the JSON convention the policy parser already uses (`policy._parse_json`
  accepts a camelCase key with a snake_case fallback). The manifest the control
  compares against should be DERIVED rather than hand-written wherever possible
  (the composition hash is not something an author can type), which is the
  design question the clause has to answer;

and the four responses have to be carried out:

* `read-only` narrows the running composition's effect authority. There is no
  single switch for this today; the honest first actuator is the approval and
  capability path a downgrade would refuse at;
* `suspend` arms the durable operator halt (`estop.latch_path`, `estop.read_latch`)
  and inherits its per-tier honesty: `estop.tier_estop_status` already reports
  which tiers honour a latch and which are `static`, and a suspension of a
  `wasm` composition has to say so rather than claim a halt it cannot deliver;
* `re-admit` re-runs the admission gate, which is what makes the response
  recoverable at all;
* `rollback` deploys the pinned artifact, and the pinned target `resolve` refuses
  to proceed without is exactly the `deploy` bundle digest an operator pinned.

A `revl drift` verb reading the last record, and a monitor loop, are the
interface on top of both follow-ups.

## Relates to

* Item 127 (`revl attest`): the admitted hashes this re-measures, taken once.
* Item 413 and the WAL: where the record lands, and the header generation that
  ties two records together.
* Item 477 (liveness expiry) and item 624 (`reconcileLivenessFromWorld`): the
  conservative-default precedent this posture copies, and the sibling
  restart-reconcile whose split from the runtime this module repeats in reverse
  (they reconcile the past, this watches the present).
* Item 443 (operator E-Stop): the durable halt the `suspend` response maps onto.
* Item 290 (evidence dossiers) and `deploy.artifact_digest`: the hash-level
  spellings reused rather than reinvented here.
* `apply.drift`: the structural basis drift this is distinguished from, and the
  reason item 469 is stated over bytes rather than names.
