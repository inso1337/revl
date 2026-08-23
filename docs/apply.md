# `revl apply` — a plan you can execute, with a rollback that is a theorem

**Status:** implemented — `python -m revl plan <files...> --manifest running.json
-o change.plan` writes the artifact, `python -m revl apply change.plan` executes
it, and the engine is `revl.mcp.session.Session.apply`. Tests: `tests/test_apply.py`.

`revl plan` ([docs/plan.md](plan.md)) is the dry run: it reports the delta a swap
would produce — provisions gained and withdrawn, the reactive cascade, the
teardown order, the change to the composition's irreversible reach — and then
throws the answer away. The change itself is made ad hoc.

`revl apply` closes that gap. The plan becomes an **artifact**:

```bash
revl compile app.rvl -o running.json        # what is live (bodies and all)
revl plan candidate.rvl --manifest running.json -o change.plan
revl apply change.plan
```

Ordinary plan/apply tools have no real rollback — when a change fails halfway,
you reach for a runbook. Here the rollback is **derived from the same IR the plan
was computed from**: a load's inverse is a dispose, a teardown's inverse is a
re-load of the withdrawn body, and they replay last-in-first-out. Change
management where undo is a theorem, not a procedure.

## What `apply` does

**(a) It refuses on drift.** A plan is computed against a specific running
composition. By the time you apply it, that composition may have moved — a
component swapped, a provider gone PENDING. The artifact carries a *basis*: a
fingerprint of the composition the plan assumed (which components, in what load
order, providing which keys). `apply` re-derives the live fingerprint — using the
provisions **actually served now**, so a drifted-to-PENDING provider reads as a
vanished provision — and compares. Any difference is refused with a diagnostic
naming exactly what moved:

```
the running composition has DRIFTED since this plan was computed — refusing to apply it:
  components vanished: Front, L1
  provisions vanished: api <- Front, cache <- L1
  re-run `revl plan` against the current composition to get a fresh plan.
```

Staleness is re-derived and compared, never assumed away.

**(b) It verifies reality against the prediction at every step.** The artifact's
operations are ordered — teardowns first, in the plan's derived LIFO order, then
loads in the resulting load order — and each carries the effect the plan
predicted: a component *gone* and its provisions withdrawn, or a component
*ACTIVE* and providing named keys. After `apply` performs an operation it reads
the live composition back and checks the prediction. A divergence stops the
apply:

```
loading `Front` was predicted to leave it ACTIVE, but it is PENDING
```

**(c) On any mid-plan failure it rolls the applied prefix back.** Whether a step
errored outright or its result contradicted the prediction, `apply` unwinds the
steps it already made — LIFO, by the inverses above — then proves the
composition is back exactly where it started: the runtime registry is the size it
was, and the live fingerprint equals the basis. The report says what was undone
and that nothing was left behind:

```
FAILED at `Front`: loading `Front` was predicted to leave it ACTIVE, but it is PENDING
rolled back 2 step(s) (LIFO, derived inverses):
  dispose  Front
  restore  Front
no residue: True (registry 2 -> 2)
```

The composition keeps serving its original behaviour throughout: a failed apply
is a non-event.

## The plan artifact

`change.plan` is a self-contained, versioned JSON document (`"revlPlan": 1`):

| field         | what it is |
|---------------|------------|
| `basis`       | fingerprint of the running composition the plan assumed — the drift check compares against this |
| `resulting`   | fingerprint of the composition the plan produces — the final whole-composition check |
| `operations`  | the ordered, executable steps, each with the effect it predicts |
| `runningIR`   | the pre-state's bodies, so the CLI can boot it (and so a teardown's inverse can re-load a withdrawn component) |
| `resultingIR` | the resulting composition's bodies, so an added or replaced component can be loaded |
| `plan`        | the full `revl plan` payload, for re-rendering and the summary |

Only an **admitted** plan can be serialized: `apply` executes changes, so a
candidate the gate rejects has nothing to apply, and `revl plan -o` refuses it
(the rejection is printed, no artifact is written).

An operation is one of:

- `{"op": "dispose", "name": N, "predict": {"absent": true, "withdrawnKeys": [...]}}`
  — a withdrawn or replaced component's fiber tears down (its own inverses
  replay). Its inverse is a re-load of `N` from the pre-state.
- `{"op": "load", "name": N, "predict": {"state": "ACTIVE"|"PENDING", "keys": [...]}}`
  — an added or replaced component comes up from the resulting bodies. Its
  inverse is a dispose of `N`.

Survivors that merely *rebind* to a new provider, or go *diverted* (PENDING)
because a provider left, are not stepped explicitly — the reactive runtime
produces those transitions on its own (R2/R3). The final whole-composition check
confirms they landed where the plan predicted.

## Applying against a live pre-state

The CLI is a one-shot: `revl apply change.plan` boots the plan's own `runningIR`
as the live composition, applies the plan against it, then tears it down and
reports whether the change (or its rollback) left residue. To exercise drift from
the CLI, boot a *different* current composition with `--against`:

```bash
revl apply change.plan --against whats-really-running.json
```

If that composition differs from the plan's basis, `apply` refuses before
touching anything. The same engine (`Session.apply`) backs the in-memory MCP
session ([docs/mcp-bridge.md](mcp-bridge.md)), where an agent applies a plan
against a composition it is already driving.

## Scope

The engine drives one in-process cordis-py composition; it reuses the same
per-fiber load and LIFO teardown the live session and `revl run` already use
([docs/swap.md](swap.md)). The prediction it verifies is the plan's own recorded
prediction — a local oracle. Reconciling that against an independent
prediction-vs-actuality oracle is future work; here, the plan is checked against
itself and against reality, which is what makes the rollback total.
