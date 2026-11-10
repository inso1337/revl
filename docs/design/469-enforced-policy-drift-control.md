# 469: Enforced policy-drift control

Design note for roadmap item 469 (issue #821). It records the spike that pins
the gap, the pure decision half that landed with it, and the running half that
issue #2758 now lands for the subjects whose bytes are local: the monitor that
re-measures them, the durable record, and the two responses that narrow
authority.

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

`detect(admitted, live, changed=None)` is the verdict over a re-measurement:

* `matched`: the live subject hashes to the admitted digest;
* `drifted`: it was re-measured and does not, or the reader names it in
  `changed` (it is present but its kind or readability is no longer what
  admission measured);
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

This slice is the decision half. The running half below, landed by issue #2758,
re-measures the local subjects and carries out the two responses that narrow
authority.

## The running half, part 1: the re-measurement and the record it writes - LANDED for local subjects

`src/revl/drift_monitor.py` is the monitor, tested by
`tests/test_2758_drift_monitor.py`. It keeps the same boundary as the pure half:
the verdict is still `drift.detect`, the response is still `drift.resolve`, and
the record is still `drift.wal_record`. Nothing in it re-argues a decision.

The subjects are DECLARED once, at admission, as `{role, name, kind, path}`, and
`drift_monitor.admit` derives the manifest from the bytes rather than asking an
author to type a hash they cannot type. This slice measures the subjects whose
bytes are local:

* a `tool` is an emitted artifact *tree*, hashed by
  `deploy.artifact_digest`, the same tree digest a deploy receiver recomputes;
* a `policy` is a file, hashed by `drift.digest`.

A later pass re-reads the same declarations, builds the reading, and calls the
verdict. The reading keeps the distinction the `unverified` row depends on:

* a declared path that is gone is `None`, the measured absence `detect` reads as
  `missing` (drift, suspend by default);
* a path that exists but can no longer be measured as the kind it was admitted
  as (a symlink, an empty tree, an unreadable file, a tree replaced by a file)
  is left OUT of the digest reading and passed as `changed` with its cause, so
  the subject is `drifted` and earns `onDrift`. Admission measured it, so the
  change of kind or readability is evidence of drift, not a blind spot; the
  cheapest way to change an admitted artifact must not be the one that fires
  nothing;
* an admitted subject with no declaration at all - a hosted model that exposes no
  hash, for instance - is also left out and reported unverified with the reason,
  so the monitor says so loudly instead of passing it. `unverified` is kept for
  a subject that was never declared measurable.

`drift_monitor.run_pass` writes one `drift-detected` record for every
non-matched pass (a matched pass writes nothing, so a loop does not fill the
WAL with confirmations). The record is the `drift.wal_record` shape plus
`actuation` (what was carried out and whether it is in force) and `monitor`
(what was and was not measured), and it is appended with `wal.seal_torn_tail`
first so an interrupted write is never merged with a new record (§535).

What remains of this part: the cadence loop, persisting the admitted manifest of
a live activation where the monitor reads it (the composition row from
`attest.canonical_hash`, the artifact rows from `deploy.artifact_digest`, the
generation from the WAL header), and the per-role measurement for `model`,
`dependency` and `peer` (a peer's attested composition hash, a model that
exposes no hash staying unverified). The WAL is single-writer: a host that
shares a live run's effect WAL has to serialize the monitor with that writer,
and a dedicated drift WAL carrying the run generation is the recommended shape.

## The running half, part 2: the declaration and the actuators - LANDED for read-only and suspend

The monitor document is the declaration this slice reads:

    {"version": 1,
     "manifest": {...},          # derived by admit(), never hand-written
     "subjects": [{"role", "name", "kind", "path"}],
     "control": {"onDrift": ..., "byRole": {...}, "onUnverified": ...}}

`control` is the stage-1 shape; a key this monitor does not read is refused, and
`run_pass` also accepts an explicit control argument so a host can bind the
policy to the composition it protects. The `driftControl` clause on the admitted
COMPOSITION is still open, and so is deriving the composition row itself; this
slice fixes the runtime half it will feed.

Only the two responses that narrow authority are carried out:

* `read-only` arms a durable latch beside the WAL (`<wal>.read-only`). The py
  runtime reads it at every NEW effectful crossing seam (extern crossings
  through `estop_gated` and `extern_emit`, effect acquisition, deferred emit,
  approval crossing, deferred fire, and a remote call through the placement
  bridge), so a component may still read locally but may no longer start a
  world-changing crossing, and the refusal happens before the host body runs
  or, for a remote call, before anything is encoded or sent. The caller cannot
  see whether a peer's method writes, so under the hold every remote call
  refuses, reads included. Teardown, inverse registration and recovery paths are not
  narrowed, because a read-only hold must not strand cleanup. The runtime must
  be pointed at the exact latch path with `arm_read_only_latch` or the ambient
  `REVL_READ_ONLY_LATCH`; the actuator reports which tiers honour it, and this
  slice wires the py reference runtime only. The record distinguishes `armed`
  (the latch is on disk) from `applied` (a named tier's runtime reads it), so a
  host that has not pointed the runtime at the path is never told the narrowing
  is enforced: `applied` holds only when the runtime's latch path (its
  `arm_read_only_latch` path, else `REVL_READ_ONLY_LATCH`) is the path the
  monitor wrote.

  The rule for crossings already under way and for lifting the hold:

  - The latch is read at the check, once per crossing. A crossing whose check
    ran before the latch was written completes: the hold refuses NEW
    crossings, it does not interrupt one in flight or undo one that finished.
    The drift record says when the hold was armed, so a post-mortem can tell
    the crossings that ran before it from the ones it refused.
  - The hold is lifted by the operator, by removing the latch file. Nothing
    re-measures on the way out: the next crossing after the unlink proceeds.
    Before lifting it, the operator is the one who has to have re-admitted or
    rolled back the drifted subject; a later monitor pass that still finds
    drift arms the latch again.
* `suspend` arms the item-443 E-Stop latch beside the WAL (`<wal>.estop`, via
  `estop.latch_path` / `estop.read_latch`), so the runtime, the conductor and
  `revl estop --report` observe it with no reader change, and the per-tier
  honesty `estop.tier_estop_status` already states is inherited.

`drift_monitor.apply_response` carries those two out and REFUSES `re-admit` and
`rollback` by name: they change what runs, so a pass that cannot perform one
records it as unapplied rather than downgrading it to a response that looks
applied. A response is always a downgrade; no path here widens authority.

What remains of this part: the `re-admit` actuator (re-running the admission
gate), the `rollback` actuator (deploying the pinned target `resolve` requires),
read-only honouring on the other tiers, and the `revl drift` verb that reads the
last record. A monitor loop and a `revl drift` verb are the interface on top of
these.

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
