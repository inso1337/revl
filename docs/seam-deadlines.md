# Seam deadlines — the missing half of partial failure

Roadmap item 54. The interop bridge already handles peer **death**: a dedicated
monitor connection sees EOF when a provider process goes away, and the proxy
turns that into a reactive **withdrawal** — the provision is retracted and every
dependent deactivates with ordered teardown (R2/R3; `docs/interop-bridge.md`,
`tests/test_swap.py`). What it did not handle is peer **hang**.

A provider can be alive but wedged — deadlocked, GC-stalled, stuck on its own
slow dependency, or simply too slow. It answers a cross-process call with
neither a value nor an EOF. A naive blocking round-trip over the Unix socket
would then wait on it **forever**, and the consumer that called it wedges too.
The only timeouts the conductor had were process **start** and **shutdown**
waits (`_wait_for` in `src/revl/placement.py`), never a bound on an individual
CALL. This is the other half of partial failure, and the `async fn`-at-seams
contract was adopted for exactly it: an async operation can be *awaited with a
deadline*.

## The deadline model

**Every seam call carries a deadline.** A call is a request/reply round-trip on
the socket; the deadline bounds how long the consumer will wait for the reply.
There are three ways a deadline is set, in precedence order (highest first):

1. **Per-call override** — `_Client.call(key, method, args, deadline=<seconds>)`.
   One call, one bound. Wins over everything.
2. **Per-operation default** — `_Client(..., deadlines={method: seconds})`, a map
   keyed by method name. The default for calls to *that* operation.
3. **Client-wide default** — `_Client(..., deadline=<seconds>)`. The fallback for
   any operation without its own entry.

If none is set the deadline is `None` — an unbounded, blocking round-trip, the
legacy shape. A swap/repoint client (`tests/test_swap.py`) keeps `None` unless a
caller opts in, so the handover mechanics are unchanged. Placement, by contrast,
*always* stamps a finite default (below), because a placed composition is
exactly where an unbounded cross-process wait is unacceptable.

Resolution lives in one place, `_Client.deadline_for(method, override)`:

```
override if override is not None else deadlines.get(method, client_default)
```

Enforcement is in `_Client.call`: after writing the request it bounds the
blocking read with the resolved deadline (a socket read timeout for the duration
of the round-trip, restored afterward). A wedged provider sends nothing, so the
read times out at the deadline instead of blocking; a healthy provider's reply
arrives first and the timeout never fires.

## The distinguishable fault

A breach raises **`bridge.SeamDeadline`** — its **own** fault kind. This is the
crux of the item: a hang must be separable from the two failures the seam
already knows.

| what happened | fault raised in the consumer | reactive consequence |
|---|---|---|
| provider process died | `ConnectionError` (from EOF) | monitor fires `on_lost` → **withdrawal** (R2/R3) |
| provider returned an error | `RuntimeError` (marshalled `{"ok": false}`) | ordinary call failure |
| **provider hung past the deadline** | **`SeamDeadline`** | ordinary call failure — **no** withdrawal |

`SeamDeadline` subclasses `TimeoutError` (so generic timeout handling still
recognises it) but is **neither** a `ConnectionError` **nor** a `RuntimeError`,
so a consumer — or a test — tells a hang apart from a death and from a remote
error by fault kind alone. It carries the seam it broke on: `.key`, `.method`,
and the `.deadline` (seconds) that was breached.

Critically, a breach does **not** withdraw the provision. A hang is not a death:
the provider may recover, the monitor connection is still open, and `on_lost`
must stay quiet. `tests/test_seam_deadlines.py` asserts the monitor never fires
across a breach.

## Residue-free unwind

A `SeamDeadline` propagates into the calling fiber and drives the consumer's
**L-Raise** exactly like any other seam failure (A8; `docs/backend-ir.md`): the
effects accumulated so far **revert LIFO**, the component lands FAILED with the
fault recorded, siblings are unaffected, and after teardown the host holds no
residue (R4). Nothing about the deadline path is special here — that is the
point. A deadline breach is *just another failure*, and the accumulated-inverse
machinery drains it the same way it drains an `acquire`/`await`/`emit` that
raised.

`tests/test_seam_deadlines.py::test_deadline_breach_unwinds_residue_free_like_any_seam_failure`
proves it over real sockets: a scope acquires three resources (each pushing its
inverse), a call to a wedged provider breaches its deadline mid-activation, and
the L-Raise runs the inverses newest-first (`lock`, `file-handle`, `db-conn`),
each exactly once, leaving the live-resource ledger empty — no residue.

## Placement configuration

`src/revl/placement.py` stamps a deadline onto every proxied seam it wires. The
composition-wide default is `DEFAULT_SEAM_DEADLINE` (30s); the placement file
overrides it:

```toml
# composition-wide default (seconds)
seam_deadline = 10.0

[processes.consumer]
components = ["Consumer"]
seam_deadline = 4.0            # per-process default, overrides the top-level one

[processes.consumer.seam_deadlines]
slow = 0.5                     # per-operation default, overrides the per-process one
```

Each proxy entry in the process spec then carries `"deadline"` (the resolved
per-op default) and, when configured, a `"deadlines"` map — the same values both
sides read for the seam. The successor spec built during a `revl swap` copies
these forward, so a re-pointed proxy keeps its deadline across a cutover.

## Follow-ups (NOTED — not built in this pass)

These are left for their owning items and were deliberately **not** touched
here:

- **The py process runner wire.** `src/revl/_process_runner.py` constructs the
  proxy with `bridge.proxy_component(key, info["methods"], info["socket"],
  module)`. To make the placement-configured deadline take effect end-to-end in
  the cordis-py path, the runner passes `info.get("deadline")` and
  `info.get("deadlines")` through (`proxy_component` already accepts them). The
  bridge mechanism and the spec are complete; this is the one remaining hop, and
  the same forwarding is the pattern for the rust/go/java/node runners.
- **The TCK (item 42)** gains a *wedged-provider* clause: a conformance provider
  that stalls past the deadline, asserting every backend surfaces its own
  distinguishable seam-deadline fault (not a death, not a provider error) and
  unwinds residue-free.
- **The fault sweep (item 30)** gains a *"hang at step k"* injection mode
  alongside its existing crash/error injections, so the sweep exercises the
  deadline path at every seam step. (`src/revl/fault.py` untouched here.)
