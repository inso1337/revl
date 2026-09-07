# Item 250, Slice 3b: the LLM-aware replay modes

Status: the offline readiness half landed with this revision (`revl replay`, the
`revl.replay_modes` planner). The live executor (`revl replay branch` actually
re-running a branch) is specified here and deferred, because it needs Slice 3a's
`model-decision` record on the WAL (issue #87, PR #516) and a live component the
offline surface does not have.

This doc is a companion to `docs/design/250-session-branching.md`, which owns
Slices 1 through 3a. It resolves the substitution-semantics questions that doc
left open for Slice 3b, and states the readiness contract the two halves share.

## The four modes

The roadmap item names four replay modes, in order of how much of the record
each one needs and how much it can claim.

1. **exact.** Reproduce the run byte-for-byte without calling the model. Needs
   the model responses on the record (to feed back instead of re-calling) and
   the determinism inputs (RNG seed, clock readings). Slice 3a records neither,
   by design: the response text is never written, and no seed or clock is
   captured. So exact is not reachable from a Slice-3a WAL, and the planner
   never reports it plannable.
2. **tool-only.** Replay the recorded model responses as fixtures and re-execute
   the tool and effect calls live. Needs the recorded responses (missing, as
   above) and, at execution time, a live component. Not reachable from the
   record either, for the same reason as exact.
3. **model-substitute.** Swap the model, feed it the original prompts, and re-run
   from the fork point. The recorded decisions name which model answered at each
   crossing and what it cost, which is the substitution surface: the planner can
   say which decisions a swap would touch. It cannot execute, because the prompt
   is not on the record (only its absence is) and a live component is needed.
4. **counterfactual.** Ask what would have happened if the agent had acted
   differently. This is model-substitute plus a divergence oracle over the
   re-execution, and it is the most valuable mode. Same record gaps as
   model-substitute, plus the determinism caveat: without a recorded seed or
   clock a counterfactual is a divergent continuation, not a controlled A/B.

## What the record carries, and the requirement vocabulary

The planner judges each mode against a fixed set of requirement inputs. Only the
first is ever met by a Slice-3a WAL; the rest name exactly what is missing, so a
blocked mode reads as a stated gap rather than an unexplained refusal.

- `modelDecisions`: one durable `model-decision` record per completion crossing
  (Slice 3a). Present on any WAL a Slice-3a runtime wrote across a model
  boundary.
- `responseText`: the model's response, needed to feed a recorded completion
  back. Never written to the WAL.
- `promptDigest`: a digest of the prompt, needed to bind a substituted call to
  the original request. Absent, because its suppression gate is the compile-side
  taint certificate the driver holds (item 444); a digest without that gate is
  the confirmation oracle item 121 closes.
- `toolCalls`: the tool calls the model requested. Made inside the opaque host
  body, which revl does not see and Slice 3a does not record.
- `seedsAndClock`: the RNG seed and clock readings, needed for a bit-reproducible
  replay. Not recorded.
- `liveComponent`: a live component, workspace handle and fiber to re-execute
  against. An offline reader has none; this is the requirement the live executor
  exists to meet.

## The readiness contract (the offline half, landed)

`revl.replay_modes.plan(wal)` reads a WAL through the tier-agnostic WAL core and
returns, per mode, its requirements, which are met, which are missing, and two
verdicts:

- `plannable`: the WAL carries enough to INFORM the mode. True when model
  decisions are recorded and the only remaining gaps are inputs the live
  executor supplies (that is, the live component). Model-substitute and
  counterfactual are plannable from a model-aware WAL; exact and tool-only are
  not, because they need response text the WAL never carries.
- `executable`: always false offline. An offline reader runs nothing, exactly as
  `revl branch` and `revl compare` state of themselves.

`revl replay WAL [--mode MODE] [--json]` renders the plan and exits 0 when at
least one mode is plannable, 1 when none is (no model decision on the WAL). That
nonzero exit doubles as an honest gate on "is this WAL model-aware", which the
live executor can consult before it tries to run anything.

A WAL with no `model-decision` record cannot be told apart from a run that made
no model completion. Both read as "no decision recorded", and the plan says so
rather than guessing, the same blind spot `revl.branch.NOT_COMPARABLE` states.

## The live executor (deferred, with its requirements)

`revl replay branch` re-running a branch belongs in the run/session machinery,
not the offline reader, because it needs a live component, a workspace at the
fork-point state (the Slice-1 rewind already produces this live), and, for
model-substitute and counterfactual, a substitution seam through the host model
call. The offline planner names precisely what that executor must satisfy:

- it supplies `liveComponent`, so a plannable mode becomes runnable;
- for a substituted call whose prompt is not on the record, the executor must
  re-derive the prompt from the re-executed branch state, not from the WAL: the
  record says which model was asked and what it cost, never what it was asked;
- determinism is stated, not faked. With no recorded seed or clock, a replay is
  labelled a divergent continuation. Recording a seed and clock is a later slice
  and would flip the `seedsAndClock` requirement with no change to this planner.

The honesty line Slice 3b holds, matching 3a: the surface reports readiness from
what the record carries, refuses to run what an offline reader cannot, and names
every input the live executor would still need instead of pretending the record
already holds it.
