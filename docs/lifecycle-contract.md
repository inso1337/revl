# The revl service lifecycle contract

**Status:** implemented (2026-09-08) · roadmap item 460 (issue #723) · referenced by
item 462 (the exemplary web app, [docs/design/525-exemplary-web-app.md](design/525-exemplary-web-app.md))

Startup, readiness, cancellation, disposal, restoration and hot replacement each
took app-specific integration and careful reading of the runtime to interpret
before this document existed. This is the small, dependable contract: what it
means for a service to be **serving**, what a cancellation **requests** versus
what it **completes**, what stays **owned after a failure**, and how a restart
**resumes**. The differentiator is not that the happy path works; it is that
every unhappy branch has a named, observable state, and that a documented refusal
with retained ownership is preferred over an apparently successful replacement
whose cleanup is uncertain.

The contract extends the guarantees behind the landed liveness, admission and
recovery work:

- **#96** (item 308, ownership modes): the correct component performs the final
  inverse; a borrower cannot close an owned resource.
- **#476** (two-phase admission commit, `decided -> runtime_applied -> finalized`
  with forward recovery): an admission decision survives a crash and is finalized
  forward, never left ambiguous.
- **#622** (production silence detection wired to declared liveness ceilings): an
  owned observer measures silence for a live generation and drives expiry under
  the declared ceiling, with attribution and withdrawal.
- **#624** (restart reconciliation of declared liveness from durable world state):
  reconciliation represents stale, partial or contradictory evidence
  conservatively rather than re-adopting owners it cannot prove.

The authoritative implementations named below are
[`src/revl/run.py`](../src/revl/run.py) (the `_Driver` and the fiber-state
machine), [`src/revl/mcp/session.py`](../src/revl/mcp/session.py) (the `Session`
lifecycle verbs), and [`src/revl/recovery.py`](../src/revl/recovery.py) (the
restart path). Conformance coverage that exercises this contract end to end on
the native cordis-py runtime lives in
[`tests/test_issue_723_lifecycle_contract.py`](../tests/test_issue_723_lifecycle_contract.py).

## The fiber states

A component's activation is a fiber. Its state is a `cordis.fiber.FiberState`,
surfaced by name through `Session.state()["components"]`:

| State | Meaning |
| --- | --- |
| `PENDING` | declared, not yet activated (an unmet requirement leaves a fiber here) |
| `LOADING` | activation in progress (acquiring the resources named in `let ... effect ...`) |
| `ACTIVE` | activated cleanly, holding its resources, answering calls |
| `FAILED` | a mid-activation fault; honest and observable, not silently retried (run.py `_settle`) |
| `DISPOSED` | torn down, its inverses replayed |
| `UNLOADING` | teardown in progress |

`FAILED` and `DISPOSED` are not interchangeable. A `FAILED` fiber ran its body far
enough to fault; a `DISPOSED` fiber was taken down deliberately. `run.py`'s
`_SETTLED_DOWN = ("DISPOSED", "PENDING", "FAILED")` names the settled-terminal set
a liveness observer keys on.

## 1. Serving

A key is **serving** when, and only when, it has a live provider.

- `Session.state()["loaded"]` is `True`,
- the providing component's fiber is `ACTIVE`, and
- the key appears in `Session.state()["providedKeys"]`.

`providedKeys` is derived from ROOT itself (`driver.resolved_keys()`), not from a
fiber merely being `ACTIVE`. This is load-bearing: a fiber left `ACTIVE` by a
cancelled deferred activation, with no provision landed in ROOT, is **never**
reported as serving its key (run.py, item 372). "Loaded means loaded": a key that
reads as serving genuinely answers calls, and a cancelled or failed activation is
surfaced loudly rather than counted as a provider.

`Session.load(ir, config)` moves a composition into serving. `Session.call(key,
method, args)` is answered only while the key is serving. A hot replacement is
`Session.swap(new_ir)`: the successor is admitted against the running manifest,
its class map is built and gated **before any teardown**, then the old generation
is disposed and the successor loaded atomically. A call cannot observe a
half-swapped composition; the new class map and cache index go live in the same
step as the composition (session.py `swap`).

## 2. Cancellation: requested versus completed

Cancellation is two events, not one. Requesting a teardown and completing it are
distinct, observable transitions, and the gap between them is exactly where
ownership questions live.

**Requested.** A cancellation has been asked for and the disposal has been
invoked, but the outcome is not yet settled. On the async teardown route
(`Session.aclose` / `Session.aabort`, item 524) the settlement envelope reports
`disposal.invoked == True` before `settled` is known. `Session.abort` (item 245,
Decision 5) marks every live frame aborting and drops the deferral queue as its
first act, before any inverse replays.

**Completed.** The teardown ran to a settled verdict. A completed cancellation
that released cleanly reports:

- `releaseOwnership == True`,
- `noResidue == True` (no host resource left held, no introspection delta), and
- `Session.teardown_disposition()["attempt"] == "released"`.

`teardown_disposition()` is the read-only inspector for this transition. Its
`attempt` field is one of `none` (never armed), `in-flight` (requested, still
running), `released` (completed clean, ownership dropped), `unresolved`
(completed with something still owed), or `rearmed` (a finished-unresolved attempt
carried forward for a re-drive). A cancellation is **complete** only when the
attempt reads `released`; every other terminal reading keeps ownership with the
session (see section 3).

A refused **swap** is also a completed cancellation, of the *change* rather than
of the running service: the admission gate refuses the successor, the running
composition is untouched, and the previous generation keeps serving. Nothing is
half-applied. This is the two-phase discipline of #476 read at the session seam.

## 3. Owned after a failure

When a teardown does **not** complete cleanly, the resources it could not release
stay **owned by the session**. They are not dropped, not silently retried, and
not adopted by a detached task.

A native disposer fault at an original resource settles the attempt as
`unresolved`:

- `releaseOwnership == False`, `settled == False`, `nativeCleanupComplete == False`,
- the settlement names each resource's outcome: the faulted one as `failed`, a
  provider the LIFO order never reached as `owned` (item 628 distinguishes a
  failed resource from an unattempted one, an inventory `driver.fibers` alone
  cannot give because the failed fiber was popped before its `dispose` was
  awaited),
- `Session.loaded` stays `True`, so the composition remains inspectable, and
- `Session.teardown_disposition()["attempt"] == "unresolved"`, carrying the same
  `ownedResources` inventory.

From `unresolved` the host has exactly two supported moves, both explicit:

- **`Session.rearm_teardown()`** clears the finished-but-unresolved attempt and
  carries its evidence forward so a fresh `aclose` / `aabort` can re-attempt the
  same owned disposal. There is no automatic retry.
- **`Session.strand_teardown()`** accepts the unresolved ownership as **stranded**:
  owed, not released, held until the process exits or `revl recover` runs. It
  drops the Python composition; the native roots stay owed. This is the
  runtime's affirmation of "a documented refusal with retained ownership beats an
  apparently-successful replacement whose cleanup is uncertain."

A refused swap is the same principle at admission time: the successor's
post-activation health gate (`_assert_successor_activated`) refuses a candidate
that did not activate cleanly, `_abort_swap` rolls back to the predecessor, and
generation N keeps serving with its state intact. Ownership never transfers to a
generation that failed to take it.

## 4. Recovery: how a restart resumes

A restart reads the write-ahead log (`src/revl/recovery.py::recover`) and returns
a stated verdict with a residue proof. It does not re-enter the dead runtime; it
re-issues named boundary inverses (or resumes the persisted generation) from the
durable record alone.

The terminal `activation-complete` marker is the whole roll-forward / roll-back
decision:

- **`rolled-forward`** (marker present): the crash happened after the activation
  committed. With a `session` and `snapshot` supplied, the persisted generation is
  re-admitted through item 15's restore, the same admission gate a live restore
  runs. The report reads `resumed == True` and
  `resume["resumedForCrashRecovery"] == True`, and the fresh session is `loaded`.
  A completed activation whose WAL also carries steady-state crossings with no
  clean `run-complete` shutdown surfaces those as honest residue rather than
  hiding them behind the marker.

- **`rolled-back`** (marker absent): the process died mid-activation.
  Reconstructible boundary inverses run LIFO against the world; an inverse the
  recorder held only as a closure, or an emission that is a process crossing with
  no inverse, is reported as honest residue in `unreconstructible`, never claimed
  to have run. This is the durable form of section 3's stranded ownership: the
  resource a crash left owned is named, not laundered clean.

Forward-recovery (design 460 §5) runs in **both** branches, after the base is
restored or the inverse replay finishes, so an admission that was decided but not
finalized is carried forward to `finalized` under either verdict. It is a no-op
for a WAL that never admitted, so those reports are byte-identical.

Two refusals guard the resume rather than inventing authority:

- a class-(c) activation body does **not** re-fire unprompted on resume; a
  policy-less resume that would silently re-fire returns
  `verdict == "roll-forward-refused"`, and a class-(c) crossing re-prompts on
  resume exactly as on first boot (`roll-forward-needs-approval`).
- restart reconciliation of declared liveness (#624) represents stale, partial or
  contradictory durable evidence conservatively; it does not re-adopt owners it
  cannot prove live.

## The state sequence

The contract is the walk item 462's exemplary app makes observable:

```
                  load                 call
   (not loaded) ------> LOADING -----> ACTIVE = serving
                                         |
              swap (admission-gated)     |  aclose / aabort / abort / unload
              +--------------------------+---------------------------+
              |                          |                           |
        refused swap:              cancel-requested             cancel-requested
        change cancelled,          (disposal invoked)           (disposal invoked)
        gen N keeps serving             |                           |
        (owned, section 3)              v                           v
                                  cancel-completed            owned-after-failure
                                  (released, no residue)      (unresolved: failed +
                                         |                     owned resources named,
                                         v                     still loaded)
                                    DISPOSED                        |
                                                          strand_teardown (owed) /
                                                          rearm_teardown (re-attempt)
                                                                    |
                                                                    v
   restart --> recover(wal): activation-complete present -> rolled-forward (resumed)
                             absent                        -> rolled-back (residue named)
```

## Scope and non-promises

- `liveness 1s` is a declared ceiling with a production observer (#622); consumers
  must not read it as a universal watchdog for arbitrary blocking or shared-clock
  cases. Unsupported cases are refused, not silently promised.
- Recovery re-issues **reconstructible** boundary inverses and resumes persisted
  generations. It does not re-run author closures or invent an inverse for an
  emission that crossed a process boundary; those are residue by construction.
- The stranded and rolled-back paths are the honest outcomes, not error states to
  be suppressed. A named owed resource is the guarantee working, not failing.
