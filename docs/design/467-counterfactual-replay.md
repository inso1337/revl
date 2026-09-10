# Item 467: counterfactual incident replay

Status: the first slice landed with this revision (`revl replay WAL --under
POLICY [--candidate FILE...]`, the `revl.counterfactual` module). It answers the
policy half of the item end to end and states the rest as bounds. The candidate's
reach is recomputed offline; nothing in this surface boots a session, instantiates
a recorder, or touches the runtime tier.

The item's exit criterion is narrow and this slice is pinned to it exactly: a
recorded incident WAL replays under an ALTERNATE POLICY and against a CANDIDATE
component with ZERO LIVE EFFECTS, and the report names the first dangerous
crossing and whether the candidate is admitted. Its own honest scope line is the
other half of the contract: no new effect fires, and the report is only as
complete as the recorded host responses.

## What the premise turned out to be against the tree

The item names `revl recover`, `revl trace`, the WAL and the admission gate as
what it extends. Measured, that is only partly the seam that exists:

- `revl recover` (`src/revl/recovery.py`) reads the same tier-agnostic WAL core
  this slice reads, but its whole job is the roll-forward/roll-back decision, and
  roll-forward RESUME and roll-back inverse execution both fire live effects — the
  one authority a counterfactual must not hold. Reusing `recover` would mean
  borrowing an executor to answer a question that needs none.
- `revl trace` (`src/revl/cli/parser.py`, `revl trace` / `revl why` / `revl
  metrics` / `revl profile`) reads a JSONL causal trace written by `revl run
  --trace`, not the WAL. It is a different artifact with a different lifetime, and
  a trace is not the record a crash leaves behind.
- The surface that already frames this question, offline and with no executor, is
  `revl replay` (item 250, Slice 3b, `docs/design/250-slice3b-replay-modes.md`):
  it reads the WAL, runs nothing, and already names a `counterfactual` mode among
  the four replay modes. That mode asks the PLANNING question — would the record
  be enough to inform a counterfactual run — and answers `plannable: true,
  executable: false`. Item 467 is the concrete question underneath that
  planning answer, so `--under` extends `replay` instead of adding a verb beside
  it, and the two readings of the word "counterfactual" in this repository are
  complementary rather than duplicates: item 250 says whether the record could
  inform the mode, item 467 recomputes one alternate policy over it and reports
  what the new policy would have newly refused.

## The seam, and why a candidate is not optional

`revl replay WAL --under POLICY` re-grades the incident's recorded crossings under
an alternate policy. `--candidate FILE...` supplies the composition the crossings
would be made in; it is COMPILED (a compile fires nothing) and its G8 audit graph
is built with the same `_boundary` walk `revl audit` uses.

The candidate is not a convenience, it is the only place two of the three facts
the question needs can be read:

- the crossing's CAPABILITY TOKEN. The WAL's emission record carries `component`,
  `label` (`key.method`), and `boundary.detail` = `{key, method, service, args}`
  (`backends/python/replay.py`, `_wal_record`). It does not carry the scope the
  callee DECLARED, because the declared scope lives in the callee's declaration
  and the record holds no declaration. The whole token namespace of
  `docs/boundary-policy.md` is enumerable only from `boundary[component]
  .capabilities[label]`, which `src/revl/boundary.py` builds from the compiled IR.
  A policy rule selects a component by that token, so without the audit graph no
  rule can be evaluated against a crossing at all.
- the ADMISSION graph. `policy.evaluate` runs over a component's reach, which is
  the audit graph.

So `--under` with no `--candidate` is not a degraded answer, it is not an answer:
every crossing reads `not-answerable`, the admission reads the same, and the
process exits 1. The alternative — guessing the token from the label — would
report a denial that never happened and miss every one that did, because the label
(`sink.shout`) and the token (`sink_cap`) are different strings by design.

## What the report says

`revl.counterfactual.replay_under` returns one document; `render` prints it.

Every recorded crossing, in RECORDED ORDER, with one of three verdicts:
`permitted`, `refused`, or `not-in-candidate` (the candidate does not declare an
emission under that label on that component, so it was renamed or removed and the
candidate does not make it). The first dangerous crossing is the EARLIEST REFUSED
crossing in that order, which is the incident's own order: the WAL's `seq` space
is one strictly increasing sequence for the life of the log, resumed rather than
reset on reopen, so "first" means the first boundary the incident actually crossed
that the new policy would have refused.

The admission verdict is separate and asks a different question: the WHOLE policy,
every rule family, over the compiled candidate. That asymmetry is deliberate. The
per-crossing leg can only run the reach leg (a `capability` allow-list or a `deny`
deny-list violation), because a crossing's capability reach is the whole of what
the record supports; a taint surface, a register floor, an evidence bundle or a
tenant partition each read an input a WAL does not carry. But the admission
question is about the candidate, not the record, so all of it is heard there, and
a refusal with no recorded crossing behind it is rendered as exactly that sentence
rather than being dressed up as a dangerous crossing.

## Zero live effect authority, enforced by shape rather than promised

The item's central constraint is not "do not fire effects" as a discipline but as
a property of the code. Three things make it structural:

1. There is no effect seam. `src/revl/counterfactual.py` imports no runtime tier:
   its only imports are `.errors`, `.policy` and `.wal`, plus a lazy `.compiler`
   and `.audit_diff` inside the candidate path. A compile and a boundary walk
   fire nothing, so there is nothing here to fire one through. A later edit that
   reached for a live component to "replay properly" would have to add that
   import, and `tests/test_467_counterfactual_replay.py` runs a complete
   counterfactual in a fresh interpreter and asserts the cordis runtime tier never
   enters `sys.modules`. That is the assertion with teeth.
2. The WAL is read-only. The report digests the WAL before and after a run and
   pins the two to be equal, so a surface that ever wrote a record, a marker, or a
   sidecar beside the log would redden.
3. The report says so. Every run carries `candidate.liveEffects: 0` and the
   rendered line `live effects: 0 (compiled statically, nothing run)`, so a reader
   never has to infer the property from the absence of an effect.

## What this surface cannot answer

These are BOUNDS in the module, printed on every run, not footnotes. The first is
the design of the WAL; the second is a filed gap in it.

- **The recorded authority is not on the record.** The WAL carries no admit
  decision and no host response, and `src/revl/recovery.py` states of its own gate
  that the reader never reads back approval or grant records, so authority
  injection stays structurally impossible. The same absence is what this report
  runs on: it RECOMPUTES the alternate policy over the reach the record names
  rather than re-deciding the crossing the recording made. A crossing reported
  permitted may have been refused at the time, and one reported refused may have
  been covered by an approval edge the record does not hold. This is the single
  largest caveat in the surface and it cannot be engineered away from this side: a
  counterfactual is only as faithful as the record, and the record deliberately
  holds no decision.
- **A free host-extern emission leaves no record (issue #841).** A direct `emit
  announce(x)` over a host extern compiles to a bare module-level call that never
  touches the recording context, so no `emission` effect record is written for it
  (the comment at `backends/python/replay.py` lines 1328-1339 owns this; another
  agent owns the fix and this slice does not touch that path). Such a crossing
  cannot appear in this report, and the report cannot tell it apart from a
  crossing the incident never made. The mitigation is visibility, not a fix: the
  candidate's reached host externs are listed, per component, under `reached host
  externs the record cannot confirm`. The `NOTIFY` policy in the test file is the
  worked example — every recorded crossing passes, and the candidate is still
  refused because it reaches `db` through host code the record holds no crossing
  for, which the report prints as a refusal with no recorded crossing behind it.
- **Only emission records are crossings here.** Recorded crossings are the WAL's
  `kind: emission` effect records. The WAL also records boundary effects
  classified `acquire` on the durable resource cleanup path; an acquisition names
  a resource, not the policy token it crossed, so it is not reduced to a token
  here.
- **The per-crossing verdict is the reach leg only**, as above. The admission
  verdict runs every rule family.
- **The admission verdict is the policy leg of the gate**, which is the leg
  `revl.admission.admit_under_policy` forwards to. The gate's other legs —
  service-replacement correctness, and the registry and evidence checks — read a
  live composition and a registry the caller holds, not a recording, and are not
  run here. "Admitted" in this report means the boundary policy admits the
  candidate; it is not a claim that the gate would boot it.
- **A crossing is identified by `(component, label)`.** A body that emits the same
  label from two differently-scoped positions is one entry here, so a candidate
  that renames or reorders within a body is followed, while a candidate that moves
  a crossing between two scopes under the same label is not distinguishable from
  the record. Pinning the crossing to its site is a later slice; the record's
  `site` is carried but not used to disambiguate.
- **A torn WAL is stated, not hidden.** If a crash interrupted the final write,
  only the intact prefix is read and a `tornWal` bound is added: a crossing in the
  unacknowledged tail is not in the report and no verdict covers it.
- **Realm-scoped rules need a candidate.** A `realm <name> may reach ...` rule
  selects on a component's isolate map, which only a compiled manifest holds. With
  no candidate those rules select nothing, and the report adds a `realmScopedRules`
  bound saying that is a gap in its own reach rather than a finding that they are
  satisfied.

## What was left out, and why

- **The live half.** Nothing here re-executes a branch, substitutes a model, or
  re-runs a divergence. That is item 250's deferred live executor, which needs a
  live component, a workspace at the fork-point state and a substitution seam
  through the host model call; this slice is deliberately the offline half it
  would consult.
- **Writing the record.** Recording the gate's admit decision or the host response
  would make a counterfactual a re-decision rather than a recompute, and it would
  contradict the property `revl.recovery` is built to hold. Not landed, and not
  intended.
- **Pinning a crossing to its emission site**, and the multi-scope-same-label
  disambiguation above.
- **Comparing two policies to each other.** The slice answers "what would this
  policy have refused" one policy at a time; a policy-versus-policy delta over one
  recording is a natural follow-up and is not here.
- **Reading the incident's own trace.** The WAL is the record of crossing order
  and that is all this slice needs; a JSONL causal trace holds a different set of
  facts and a different question.
