# Design: awaitable Session teardown with explicit owned-resource settlement

Roadmap item 524 (GitHub issue #524). Source: `src/revl/mcp/session.py`
(`Session.aclose`, `_teardown_offloaded`, `_settlement`, and the synchronous
`unload`/`abort`/`commit_confirm` it mirrors), `src/revl/run.py`
(`_Driver._dispose_all`, the loop-bound owned disposal), `backends/python/
runtime.py` (`SessionOwner.finalize_commit`/`finalize_abort`, the commit-state
owner). Consumer: revl-harness, running a Session inside an async host.

This is a first tractable slice plus the contract it fits into. It does not
change the Cordis pin, and it introduces no automatic abort, retry or recovery.

## The gap

A `Session` owns its own event loop (`self._loop`) and drives every lifecycle
verb through `_run`, i.e. `self._loop.run_until_complete(coro)`. That is correct
for a synchronous caller. It is unusable from an async host: asyncio refuses to
run a second loop on a thread that already has one running

    RuntimeError: Cannot run the event loop while another loop is running

so a host inside its own loop cannot call `unload`/`abort`/`commit_confirm`. The
harness therefore conserves the Session (retains root/pools) rather than risk a
half-torn-down composition or a premature "shutdown complete" it cannot back up.

What the host needs is not "make unload faster" but a supported awaitable route
that (a) runs from the owning loop without nesting, and (b) reports a settlement
precise enough to distinguish a requested verdict from a completed native
cleanup from an unresolved failure from permission to release ownership.

## The API

`async def Session.aclose() -> dict`.

Behaviour preserves the documented default: like `unload`, a plain close is the
IMPLICIT terminal commit (witnessed mutations discharge, one consolidated
discharge record), unless a frame was marked aborting in-process, in which case
the inverses replay and no discharge record is written. The explicit
`commit`/`abort` verbs remain the audited, WAL-marked paths.

Mechanism: `aclose` offloads the loop-bound disposal to a worker thread via
`loop.run_in_executor(None, drive)`, where `drive` calls
`self._loop.run_until_complete(driver._dispose_all(self.ir))`. The host loop is
never blocked on a nested `run_until_complete`; the session's own loop is driven
on the worker thread, which is legal because that loop is otherwise only ever
driven transiently and synchronously. The verdict finalisation
(`finalize_commit`/`finalize_abort`) and the R4 residue report are pure Python
and run back on the host loop after the disposal returns.

## The settlement

The result keeps the four states the host must tell apart distinct, and never
collapses a caught cleanup error into a verified success:

| field | meaning |
|---|---|
| `requestedVerdict` | `commit` or `abort` — the intent, before any effect |
| `disposal` | `{invoked, returned, failed, cancelled}` — the owned disposers' fate: invoked, ran to return, raised, or a disposer itself cancelled |
| `nativeCleanupComplete` | the native `_dispose_all` returned |
| `settled` | PHYSICAL settlement: cleanup returned AND the R4 checks pass |
| `releaseOwnership` | the host may drop pool/root reservations — false on any unresolved residue, so ambiguity retains |
| `unresolved` | what is still owed (compensation residue, failed checks, or the failure reason + live components) |

On a returned cleanup the result is a superset of `unload`'s report
(`unloaded`, `noResidue`, `checks`, `detail`, `trace`, `compensationResidue`).
On a failed/cancelled cleanup the verdict is NOT finalised, the Session is NOT
reset, and `releaseOwnership` is false — the composition stays loaded for the
host to inspect, strand, or reconcile deliberately.

## One owned teardown, never duplicated

The first `aclose` retains its in-flight attempt on `self._teardown_future` and
awaits it under `asyncio.shield`. A duplicate caller, or a caller arriving after
an earlier awaiter was cancelled, joins that same future rather than launching a
competing teardown or a second pool release. Shielding means cancelling a waiter
never cancels the physical cleanup. The future is set synchronously before the
first `await`, so two callers on one loop cannot both start a teardown. It is
kept resolved after settling (a later read is idempotent) and cleared by `load`
when a fresh composition boots.

## The ownership prerequisite (pre-effect guard)

`aclose` refuses BEFORE any terminal effect when the ownership contract is
unmet, stating the missing contract so the host keeps its resources:

- nothing loaded / frozen (`_require`), or E-STOPPED (`_refuse_if_halted` —
  the estop stranding path stays with the synchronous `unload`);
- the session's runtime loop IS the caller's own running loop. That shared-loop
  shape cannot be offloaded (it would double-drive one loop) nor disposed inline
  (it would nest a loop); it is refused up front rather than discovered after
  teardown begins.

Scope is this Session's own driver and loop only. Closing one Session neither
stops nor freezes an unrelated one — there is no global clock freeze and no
global-idle requirement; an independent root keeps serving.

## Acceptance scenarios

Covered by this slice (tests in `tests/test_session_awaitable_teardown.py`):

1. Close from a running host loop with no nested `run_until_complete` failure.
2. Owned child cleanup awaits real async work (the `undo` inverse) before the
   settlement is reported — the offloaded `run_until_complete` blocks the worker
   until `_dispose_all` returns.
3. Disposal that fails/cancels/logs a fault exposes unresolved/failure state, not
   verified success; the Session is retained.
4. A cancelled waiter + a re-issued close join the one retained attempt; the
   owned disposal fires exactly once, no duplicate pool release.
5. Two independent Sessions: closing A neither stops B nor touches B's loop.
6. Unsupported (shared-loop) ownership is rejected before inverse/terminal
   effects, with enough status for the host to preserve ownership.
7. Existing synchronous lifecycle verdict behaviour is unchanged — the async
   settlement envelope is `aclose`-only; `unload` stays byte-compatible.

## Deferred slices (explicitly out of this PR)

- A structured ownership registry distinguishing original provision/disposal
  identity from arbitrary detached Tasks, so the guard can name a specific
  unsupported raw externally-held resource or shared Clock/timer domain rather
  than gating on loop identity alone (scenario 6's broader form). This slice
  gates the loop-ownership case, which is the one the harness actually hits.
- A retry-after-failure route (this slice keeps a failed attempt readable and
  requires an explicit re-arm to re-attempt; the acceptance scenarios need
  join-not-duplicate, not automatic re-attempt).
- An awaitable `abort`/`commit_confirm` twin, if a host needs the audited WAL
  paths asynchronously. `aclose` covers the implicit-commit default the harness
  refused; the audited paths can follow the same offload seam.
- Coordination with #498's witness inspection/verdict surface: review
  confirmation is not physical settlement, and this settlement deliberately
  reports `settled` from the disposal return + R4 checks, not from a verdict
  marker.
