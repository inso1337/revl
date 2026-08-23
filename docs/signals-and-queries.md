# Signals and queries — the workflow-engine pattern, answered

*The absence of a signal keyword is a design choice, not a hole. This states
where the workflow-engine pattern already lands in revl, decides the one place
it does not, and shows how a durable signal rides the crash-recovery WAL.*

Grounded in: [docs/mcp-bridge.md](mcp-bridge.md) (`revl_call`, `revl serve
--mcp`, `revl mcp schema`), [docs/queries.md](queries.md) (the read-only query
surface), [docs/backend-ir.md](backend-ir.md) §"Required semantics" and
[docs/plan.md](plan.md) §2 (R2/R3, the reactive cascade), and
[docs/crash-recovery.md](crash-recovery.md) with `backends/python/replay.py`
(item 47's write-ahead log).

---

## 1. The pattern, and why it deserves an explicit answer

A workflow engine gives a *running instance* three affordances: you push an
**external event** into it, you **query** its state without changing that
state, and it **reacts** when the world around it shifts. Nothing in revl is
called "signal", "query", or "event", so a reader steeped in that vocabulary
can mistake the missing keywords for missing capability. They are not. Each of
the three is an ordinary revl construct that the compiler already holds to a
stronger guarantee than the pattern it mirrors. The table is the whole claim;
the sections defend each row against what actually exists in the tree.

| workflow-engine affordance | revl construct | mechanism | where |
|---|---|---|---|
| **query** — read state, no mutation | a provided **pure `fn`** | `readOnlyHint` is *compiler-proven* (G4), not a handler-author convention (item 39) | `revl mcp schema`, `revl serve --mcp` |
| **signal** — push an external event into a running instance | a **call on a provided operation** | `revl_call` on the live session, or the served tool surface | [mcp-bridge.md](mcp-bridge.md) §3–§4 |
| **provider-change event** — react to a dependency shift | a **reactive coeffect** | R2/R3: activate/deactivate/rebind on provision change, ordered teardown | [backend-ir.md](backend-ir.md), [plan.md](plan.md) |

Two genuine gaps remain, addressed after the mapping: **durable signals** (§5)
and **external event subscription** for events that are *not* provider
withdrawal (§4). The second is a design decision, made here in writing.

---

## 2. A query is a provided pure `fn` — and it is the stronger one

Reading a running composition's state without mutating it is what the
read-only half of the MCP bridge already does. Every operation a composition
**provides** projects to an MCP tool via `tools_from_ir`, and an operation that
is *not* declared `emission` projects with `readOnlyHint: true`
([mcp-bridge.md](mcp-bridge.md) §1). Calling it — through `revl_call` on the
live session, or as a served tool off `revl serve --mcp` — returns state and
touches nothing. That *is* a query.

The composition-level query surface ([queries.md](queries.md): `emits-to`,
`withdraw`, `depends-on`, `reaches`, `drift`, all `readOnlyHint: true`) is the
same idea aimed at the *shape* of the system rather than its data — "who emits
to `db`", "what breaks if I withdraw `PgDatabase`". Both are reads that carry,
in their payload, whether the answer is a proof or a may-analysis.

**Why revl's query is stronger than the workflow-engine version.** In MCP at
large, `readOnlyHint` is *an assertion by the tool's author*, and nothing
checks it — the tool-poisoning gap. A workflow engine's "query handler" is the
same: a convention that the author wrote a handler that only reads. revl is the
only place the hint is **compiler-derived** ([mcp-bridge.md](mcp-bridge.md)
§4). It appears exactly where the checker *refused* any unreverted mutation,
and — because a service declaration is a checked upper bound on every
provider's effects (G4, item 39) — no hot-swapped provider can later exceed
what the tool advertised. The guarantee even survives first-class functions: a
name used outside call position that reaches an emitting callable is treated as
reaching an unnameable boundary and refuses a plain declaration
([mcp-bridge.md](mcp-bridge.md) §1). A "query" here is not a promise that the
author was careful; it is a property the compiler enforced.

So: **query = a provided pure `fn`, projected with a compiler-proven
`readOnlyHint`.** Nothing to build.

---

## 3. A signal is a call on a provided operation

Pushing an external event into a running instance is a `tools/call` that lands
on a live provided operation. `revl_call` invokes a provided operation against
the in-memory session ([mcp-bridge.md](mcp-bridge.md) §3, "the live session");
`revl serve --mcp` puts every provided operation on the wire as
`<prefix>.<key>.<op>`, maps the named MCP arguments back onto the declared
parameter order, and lands on `Session.call` against the running composition
(§4). An external actor — a human, another service, an agent — calling
`cache.put("k","v")` on a booted composition *is* delivering a signal to a
running instance.

The classification carries through, in the direction that matters. A signal
that changes state is a call on an `emission fn`, so it projects with
`destructiveHint: true` and names, in `x-revl.effects`, the exact emissions and
capabilities it crosses ([mcp-bridge.md](mcp-bridge.md) §4). A signal that only
reads is the §2 query. The workflow-engine distinction between a "signal" (may
mutate) and a "query" (must not) is, in revl, the `emission` classification —
already the load-bearing line everywhere else in the language.

So: **signal = a `tools/call` on a provided operation** (`revl_call`, or a tool
served by `revl serve --mcp`). Nothing to build.

---

## 4. Provider-change events are reactive coeffects — and the one gap

There are two kinds of "the world around a component changed", and revl answers
them very differently.

**Provider change is already first-class.** When a required provision is
withdrawn, replaced, or (re)appears, the reactive runtime settles the
composition on its own — this is R2/R3 from [backend-ir.md](backend-ir.md)
§"Required semantics":

- **R2 (reactive resolution)** — a component activates only when every
  `requires` key is provided, deactivates when one is withdrawn, and
  reactivates against a replacement provider.
- **R3 (withdrawal ordering)** — dependents fully deactivate before the
  provider does, and a dependent may still call its required services during
  its own teardown.

A component "reacting to an event" where the event is *a dependency changing*
needs no keyword: it declares the `requires`, and R2/R3 deliver the
activate/deactivate/rebind transitions. [plan.md](plan.md) §2 enumerates the
verdicts (diverted, rebound, activated) the cascade produces, and the query
surface can ask ahead of time what a withdrawal would do
([queries.md](queries.md) §4). This is the reactive-coeffect reading:
provider-change is a coeffect the runtime supplies, not an event a body
subscribes to.

**The gap: events that are *not* provider withdrawal.** A component reacting to
a message on a bus, a file-watch notification, a timer, a webhook — an event
with no corresponding provision in the composition graph — has **no
first-class form today**. In practice it is a host `extern` loop: an extern
that blocks on a queue and calls back into a provided operation. That works,
but it lives outside the effect/coeffect system the rest of the language is
checked against.

### 4.1 The decision: a bus is a service, not new grammar

Two shapes were on the table. State the choice so the absence reads as a
decision.

**Option A — an `on …` reactive form.** New grammar: a component declares
`on someEvent(payload) { … }`, and the runtime routes matching events into the
body. It reads naturally and names the intent directly.

**Option B — a bus is just a service.** No new grammar. A component that wants
to react *requires* a bus service and *provides* the handler operation the bus
calls; the event source is a component that *provides* the bus and, on each
event, calls the subscriber's operation. Subscription is `requires`/`provides`
wiring; delivery is an ordinary call on a provided operation — i.e. a §3 signal
whose caller happens to be an in-composition bus rather than an outside actor.

**Recommendation: Option B — a bus is a service.** The reasons are structural,
not stylistic:

1. **Zero new syntax, zero new metatheory.** Option B is expressible *today*
   with `service`, `requires`, `provides`, and a call. Every guarantee already
   holds over it unchanged: the handler's effects are bounded by its
   declaration (G4), the bus and subscriber are hot-swappable across the seam,
   `revl audit`/`revl query` see the wiring because it *is* the dependency
   graph, and the reactive cascade (R2/R3) governs a bus that comes and goes
   exactly as it governs any other provider. An `on` form would need every one
   of those defined afresh for a second kind of edge.

2. **It collapses into the mapping already made.** A delivered event is a §3
   signal — a call on a provided operation — and the only new thing is *who
   calls it*. Making the bus a service means "external event subscription" is
   not a fourth concept; it is signals (§3) plus dependency wiring (§4), both
   of which already exist and are already checked.

3. **The event source's effects stay visible.** A bus component that reaches a
   socket or a queue is an `emission`/extern like any other, so its boundary
   lands on the G8 audit. An `on` form risks hiding the subscription edge from
   the same analyses that make the language auditable, because the edge would
   not be a `requires`.

4. **It matches how revl already answered the other three affordances.** Query,
   signal, and provider-change each turned out to be an existing construct held
   to a stronger guarantee — *not* a new feature. A bus-is-a-service keeps
   external subscription in that same register. Reaching for new grammar here
   would be the one place the pattern broke its own method.

The honest cost of Option B: the boundary event *loop* — the extern that blocks
waiting for the next message — is still host code at the very edge, exactly as a
socket read is. revl does not try to bring the blocking wait inside the checked
layer; it brings the *delivery* inside (a checked call on a provided op) and
leaves the wait where nondeterminism belongs — outside, where it cannot poison
the metatheory ([mcp-bridge.md](mcp-bridge.md), "Why this shape"). That is the
same seam revl draws for an LLM: a `service` with `emission` ops, model call
checked, token generation left outside. An `on` form would pretend the wait is
inside; a bus-as-service is honest that only the delivery is.

**Therefore there is no `on …` reactive form, and there should not be one.**
External event subscription is: require a bus, provide a handler, let the bus
call it. If a future concrete need shows a bus-as-service genuinely cannot
express something (ordering guarantees across many buses, say), that is the
moment to revisit — with a demand test, not a speculative keyword.

---

## 5. Durable signals ride item 47's WAL

A **durable signal** is a queued delivery that must survive a crash: the event
was accepted, the process died before the handler finished, and on restart the
delivery must still happen (or be provably accounted for). This does *not* need
a new durability mechanism. A signal is a call on a provided operation (§3), and
item 47's write-ahead log ([crash-recovery.md](crash-recovery.md),
`backends/python/replay.py`) already records exactly that call as a
reconstructible description.

**The mechanism the WAL already has.** Every committed effect the WAL appends
carries an `origin`. For an effect produced by a provided-service call, that
origin is `{"phase": "call", "key": K, "method": M, "args": [...]}` — the
concrete, re-issuable identity of the signal that caused it
([crash-recovery.md](crash-recovery.md) §4, the effect-record shape; and
`replay.py` `forward_plan`, which re-emits precisely a `phase: "call"` unit as
`{kind, key, method, args}`). This is the same reconstructible-from-description
property item 47 turns on ([crash-recovery.md](crash-recovery.md) §3): a call
by name with captured arguments survives the process, where a closure does not.
A signal is a named call with captured arguments *by construction* — it is the
reconstructible case, never the closure case.

**What a durable-signal mechanism would take** (a design note; not built here —
building it would touch `run.py`, which item §2 owns):

1. **Accept-ahead-of-handle.** When an external event is accepted, append a WAL
   record for the intended delivery *before* running the handler — the
   write-ahead discipline `WriteAheadLog` already enforces (`flush`+`fsync` per
   record, so an acknowledged signal is on disk before it is allowed to
   matter). The record is a `phase: "call"` origin: `key.method(args)` of the
   provided operation the signal targets. Because it is a named call with
   captured args, it is reconstructible — the reconstructible path
   `record_boundary`/the call-origin path already produce, not the closure
   path.

2. **The marker decides the outcome.** The delivery's completion is a committed
   effect under the `activation-complete` marker; its presence-or-absence is
   already the entire roll-forward/roll-back decision
   ([crash-recovery.md](crash-recovery.md) §4–§5). A signal accepted and
   completed before the crash rolls forward with the generation. A signal
   accepted but *not* completed is an in-flight `phase: "call"` unit in the log
   with no completion — exactly what roll-back or a re-delivery reads.

3. **Re-delivery is `forward_plan`, not a new engine.** On restart, the queued
   signal's re-delivery is the call the WAL describes, re-issued the way
   `forward_plan` already re-issues a `phase: "call"` unit — with `replay.py`'s
   own caveat intact: re-issuing runs the invocation again; it does not restore
   the state the first attempt produced. Whether re-delivery is safe is the
   handler's idempotence, i.e. the application's own equivalence — the same
   honesty [crash-recovery.md](crash-recovery.md) §6 and [replay.md](replay.md)
   already state about running an inverse. An emission the interrupted handler
   *did* commit is a one-way crossing the WAL names but does not undo; a durable
   signal that must not double-emit needs an `emission`'s `compensate` (A5) or a
   reconstructible inverse, precisely as any other boundary state does.

So a durable signal is not a new feature: it is a §3 signal whose accepting call
is written to item 47's WAL as a `phase: "call"` record, recovered as any other
reconstructible boundary effect. The WAL "carries a queued delivery" because a
queued delivery *is* a named call with captured arguments — the one thing the
WAL was built to make survive a process.

---

## 6. Summary — three mappings, one decision, one design note

- **Query** = a provided pure `fn`, projected with a **compiler-proven**
  `readOnlyHint` (item 39, G4) — stronger than the workflow-engine convention it
  mirrors. *Exists.*
- **Signal** = a `tools/call` on a provided operation (`revl_call` /
  `revl serve --mcp`), with `emission` drawing the mutate/read line.
  *Exists.*
- **Provider-change event** = a reactive coeffect (R2/R3): activate,
  deactivate, rebind on provision change, with ordered teardown. *Exists.*
- **External event subscription** (events that are not provider withdrawal):
  **decision — a bus is a service**, not an `on …` reactive form. Require a bus,
  provide a handler, let the bus call it. Zero new syntax; every existing
  guarantee holds; the blocking wait stays honestly outside the checked layer.
- **Durable signal** = a §3 signal whose accepting call is a `phase: "call"`
  record on item 47's WAL, recovered as any reconstructible boundary effect.
  *Designed here; a build touches `run.py`, out of scope for this note.*

The point of stating all of this at once: the workflow-engine pattern's
absence from revl's vocabulary is a mapping, a decision, and a design note — not
a hole.
