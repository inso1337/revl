# 571: The circuit breaker: a guard on the provision edge, three states, one failure rule

Design note for issue #2710. Design number 571, assigned rather than claimed:
570 is on main (`570-action-receipt-chain.md`) and a scan of main and the open
pull requests on 2026-11-13 found no claim on 571.

Status: the first slice the issue asks for. It fixes the surface, the hook, the
state's identity, the failure rule and the interactions, and changes no code.
The Python-tier behaviour, the docs line and the tests are slice 2, planned at
the end of this note.

## The gap

A component that keeps failing keeps being called. Four pieces exist around the
problem and none of them trips on repeated failure:

- `fault.py` injects one failure at a step and asserts the paradigm's own
  guarantee ([fault-tests.md](../fault-tests.md)). It is test-time injection,
  not a runtime policy.
- The `slo` block measures a windowed success rate over activations and, on a
  breach, diverts to a declared fallback provider, pauses or halts
  (`slo.admit_divert`, `slo.pause`, `slo.halt`,
  [473-slo-contracts.md](473-slo-contracts.md)). The divert is one-way: nothing
  probes the original provider again.
- The retry machinery meters its own loops: the validation retry of a model
  completion (`validated ... retry N`), the idempotent-emission retry
  (`runtime.retry_idempotent`) and the peer-pool replay budget
  (`lawful_retry.RETRY_BUDGET_KEY`). None of them wraps a call to a provided
  key.
- The E-Stop latch (`estop.py`, [443-estop.md](443-estop.md)) halts everything,
  by hand.

A search of the compiler, stdlib, runtime and docs for circuit breaker,
half-open, consecutive failures and failure threshold finds nothing that
applies. The missing piece is a policy over calls to one provider, and the
runtime has no hook on that path today. This note names the hook.

## The decision, in one paragraph

A composition may declare a `breaker` block addressing rows. For each addressed
row the py runtime installs a GUARD on the object the row's component registers
for its provided key, so every call to that key from every caller passes the
guard: a component that requires the key, `Session.call`, and `revl run`. The
guard is a per-row state machine with three states. CLOSED counts consecutive
failed calls and trips to OPEN when N consecutive failures span no more than the
window. OPEN refuses the call by name with a typed `BreakerOpen`, before the
provider's body runs. After the cooldown the next call is admitted as the single
HALF_OPEN probe; a probe success closes the breaker, a probe failure reopens it.
A failure is a call whose provider method raises, with a short, closed list of
exceptions that are not the provider's fault. The breaker's state belongs to the
provider's identity, so it survives a swap that leaves the provider unchanged
and resets when the provider changes. Every decision is traced, the status is
reported, and an operator can reset a row. A tier that cannot enforce the block
refuses the composition by name.

## Where the guard runs

### The hook

A call to a provided key is not an emission and it is not a `Session` verb. A
component that `requires` a key calls the provider's object directly, and
`Session.call` resolves the same object through `driver._namespace()`. `revl
run` resolves it through `run.resolve_key`. The one point all of those share is
the object the providing component registers with `_revl_ctx.set(key, impl)` in
its emitted `provide` step.

So the hook is a guard around that object, in the style `run._Router` already
establishes for an interposed provider: a proxy whose `__getattr__` forwards a
method call and refuses `_`-prefixed names, so cordis's traceable probe passes it
through unwrapped. `runtime.plug` installs it, for a component whose entry
carries a `breaker` clause, by presenting the component a context whose `set`
wraps the value registered under a guarded key. `plug` is already the one entry
every py activation passes and already hosts an E-Stop check, and a recording
context wrapper (`replay._RecordingContext`) is the precedent for a context
wrapper at that level.

A component with no clause is activated exactly as today and its emitted Python
does not change, so no backend golden moves.

### Why not a Session hook, and why not a seam row

The issue's problem is "a component that keeps failing keeps being called". The
callers that matter include components calling an injected provider, and `revl
run` has no `Session`. A guard in `Session.call` would govern only callers
outside every realm, which is the wrong half.

A `seam` row ([composition-rows.md](../composition-rows.md)) is the language's
existing interposition, and it is the wrong tool: it is a distinct-key
forwarder, so the wrapped provider has to be re-keyed in its own source
("Inserting the wrapper is a source edit to a component the seam author does not
own"), and the same-key form is the documented hole in `run.py`. A breaker that
required editing every guarded provider would not be a composition-level policy.

### The call paths, stated

| path | reaches the guard | how |
|---|---|---|
| a component calling its injected provider | yes | it holds the registered (guarded) object |
| `Session.call` and `Session.act` | yes | `driver._namespace()` returns the same object |
| `revl run` | yes | `run.resolve_key` reads the same registration |
| a routed key (`run._Router`) | not guarded | the router is not a plugged fiber; a clause on a routed key is refused at compile time |
| a call served from the session's cache | no | no provider call happens, so nothing crossed |

Slice 2 pins the first three with one test each, and that test is the exit
criterion for this section: if the guard is not seen on one of them the design
is wrong, not the test.

### How a provided key maps to a row

The block addresses a row the way `place` and `grant` do (426 section 2.3):
`@label`, or `key("k")` and `key("k", realm: "r")`. A row's claims are
`(key, realm)` pairs, and an address that resolves to a row providing more than
one key guards each of them under the one entry. An address that matches no row
is refused and the hint lists the rows and keys the composition has.

## The surface

### The grammar

```text
composition_clause := ... | 'breaker' breaker_block
breaker_block      := '{' breaker_entry (',' breaker_entry)* '}'
breaker_entry      := address ':' breaker_setting+
breaker_setting    := INT | 'over' duration | 'cooldown' duration
address            := '@' label | 'key' '(' string [',' 'realm' ':' string] ')'
```

`breaker` is contextual, read only in the clause-head slot beside `row`, `slo`,
`place` and `grant`. `over` and `cooldown` are matched by spelling, as `slo`'s
`over`, `min` and `of` are. A bare integer is N. The duration is the one every
other duration clause uses, `<n>[ms|s|m|h]` with a bare number meaning seconds
(`parser._duration_literal`, which funnels through `policy._parse_ttl`). There
is no `d` unit.

At most one block per composition and one entry per row. An entry writes at
least one setting; an entry that writes none is refused.

```revl sketch
composition storefront {
  row @db from "db.rvl" provides db
  row @cache from "cache.rvl" provides cache
  breaker {
    @db: 5 over 60s cooldown 30s,
    key("cache"): 3 over 2m
  }
}
```

The block is parsed into `CompositionDecl.breaker`, checked during composition
admission, and carried on the row table under its own conditional IR key, as
`slo` is (`RowTable.to_ir`'s conditional `slo` key in `composition.py`). A
composition with no block emits no `breaker` key, so its document is
byte-identical to today's, and slice 2 pins that.

The IR carries every addressed row with its settings RESOLVED, the defaults
written in, keyed by row label, with the units in the key names:

```json
"breaker": {"db": {"key": "db", "realm": null,
                   "threshold": 5, "windowMs": 60000, "cooldownMs": 30000}}
```

Resolved rather than left to a reader, because a tier, the operator's status and
the provider fingerprint below must all see the number actually in force, and a
later change to a default must not silently move a deployed policy.

### Where the configuration lives, and who owns it

In the composition, which is the operator's file. The block is read from the
base composition only, as `slo` is; a `site` layer cannot override it in slice
2, and that is a stated gap rather than an accident. The operator owns the
running state through the reset verb below.

### The defaults

| setting | default | why |
|---|---|---|
| the block | absent | opt in; absent means byte-identical |
| N | 5 | small enough to stop a clearly broken provider, large enough not to trip on one bad call |
| the window | 60s | a streak that took longer than a minute is not one outage |
| the cooldown | 30s | half the window: time for a restart, short enough that the way back needs no person |
| probe calls | 1 | one probe, not a ramp |
| reset rule | a probe success closes and clears the streak; a probe failure reopens and restarts the cooldown | |

### Compile-time refusals

Under `G4`, category `breaker`, beside the `slo` refusal:

- an address that matches no row, a repeated row, a second block, an entry that
  writes nothing, a non-positive N or duration, an unknown setting;
- a row the guard cannot observe, refused by name with the reason: a `remote`
  row (its failures are already the row's own `on_failure` decision: a fault
  withdraws the provider, or comes back in band), a `host` row, a `seam` row, and
  a key served by a router.

A breaker that silently did nothing would be the failure this feature exists to
remove, so none of these is accepted and ignored.

## The states and the transitions

One window rule: the breaker trips when the N most recent calls are all failures
and the oldest of those N is no more than the window older than the newest. A
success clears the streak. A streak that reaches N failures spread over more than
the window drops its oldest failure and keeps counting, so a slow trickle never
trips it. Implementation is a queue of at most N failure instants, cleared on
success.

```text
          N consecutive failures within the window
   CLOSED -----------------------------------------> OPEN
     ^                                                 |
     | probe succeeds                  cooldown elapsed |
     |                                                 v
     +------------------------------------------- HALF_OPEN
                  probe fails: back to OPEN,
                  cooldown restarts
```

| state | a call | effect on the state |
|---|---|---|
| CLOSED | dispatched | a failure joins the streak, a success clears it, N within the window trips |
| OPEN | refused by name, provider body not run | none; a refusal is neither failure nor success |
| HALF_OPEN | the first arrival is the probe, every other is refused | the probe's outcome settles the state once |

A refusal is a typed `BreakerOpen` exception in `runtime.py`, carrying the row,
the state and the milliseconds until the next probe, and identified by a marker
attribute rather than class identity, as `TransportFault` is. It is not an
uncoded `SessionError`: `Session._refuse_if_paused` raises one, which is why a
caller cannot tell a pause from anything else by code. `diagnostics.classify`
reads a compile-time `RevlError` and is not involved.

## What counts as a failure

One rule. A call fails when the provider method raises an `Exception` out of its
body. Nothing else counts, and there is no second definition anywhere in the
note.

The `slo` monitor's `success_rate of activations` counts something else: the
lifecycles that settled into FAILED (`metrics._is_failed`). A provide-method
error leaves the component ACTIVE, so that population cannot see the failures a
breaker exists to stop, and `metrics._failure_metrics` reads only FAILED
withdraws. The two measurements are different populations with no shared code
and may disagree; neither is pooled with the other. The per-call outcome this
guard records is exactly the numerator `success_rate of crossings` is missing
today, which is a reason to keep it a plain record and a candidate for that
slice, not a promise.

The cases a definition has to settle, each with its reason:

| case | counted | reason |
|---|---|---|
| the body raises an `Exception` | failure | the rule |
| the call returns, including an in-band `Err` | success | the guard watches the crossing, not the result type; a row whose failures come back in band is a `remote ... on_failure(result)` row, and those are refused rather than half-watched |
| arguments that do not bind the method's signature | not counted, raised to the caller | a caller error, decided before the body runs |
| `ApprovalRequired`, `ApprovalRefused`, a policy refusal | not counted | raised by the session's gate before the guard, so the guard never sees them; the peek below keeps them from spending anything |
| `EstopHalted`, `ReadOnlyRefused` | not counted, re-raised | an operator verdict, not the provider's fault |
| the caller cancels (`CancelledError`, `KeyboardInterrupt`) | not counted | not an `Exception` outcome and not evidence; a cancelled probe frees the probe slot with no verdict |
| the call hangs | not counted until it ends | nothing can interrupt it; see the probe lease |
| a refusal by this breaker | not counted | else it would never close, or would hide the outage |
| a retry inside the provider's own body | invisible | the guard sees one call and its final outcome |

## Exactly one probe, and a probe that never returns

Admission is decided under a lock held for the state read and write only, never
across the provider call, so two concurrent callers on a HALF_OPEN breaker admit
exactly one probe and refuse the other. The admission carries an epoch number.

A hung probe is bounded by a LEASE, not a timer: the probe slot expires one
cooldown after the probe started, measured on `time.monotonic()`. The lease is
read lazily, by the next call that arrives on any other task or thread; on
expiry that call records `probe-timeout`, reopens the breaker and restarts the
cooldown. There is no background task, so there is nothing a test has to
reproduce.

What the lease does not do: it cannot interrupt a call that never returns, and
under `Session.call` the session's own loop is blocked in `run_until_complete`,
so no second call can arrive while a call hangs. That is an existing property of
the session, not one this guard adds or removes. The lease protects the
concurrent callers of `revl run` and of components, which is where a stuck probe
would otherwise queue them all behind it.

The late result of an expired probe carries a stale epoch. It changes no state
and is traced as `late-result`, so a provider that recovers after the lease
cannot close a breaker that has already moved on.

Every comparison uses the monotonic clock. The wall-clock `ts` on a traced
decision is for readers and never decides anything.

## What the state belongs to

The state is owned by the guard registry the driver holds, not by the fiber and
not by `Session._generation`. `Session._generation` is bumped by load, swap,
rollback, undo and apply, so keying on it would close every breaker on every
operation. The trace's `gen` is a process-global reload counter and has the same
problem.

The state is keyed on the provision edge, `(row label, key, realm)`, and carries
a PROVIDER FINGERPRINT: a digest of the row's source text and configuration and
of its resolved breaker settings.

- A swap or an apply that re-admits the row with an equal fingerprint keeps the
  state. A deploy of something else does not reset a quarantine.
- A swap that changes the row's source, its configuration or the breaker
  settings resets the row to CLOSED and traces `reset` with the reason. A
  failure streak against code that was just replaced says nothing about its
  replacement, and replacing the provider is the ordinary way to fix a broken
  one.
- Unload or a fresh session starts CLOSED. The state is not durable.

A divert is a swap: it re-admits the composition with the row's `from` pointing
at the fallback (`slo.admit_divert`), which changes the fingerprint, so the row
resets and any breaker on the fallback is a new one. This note does not give the
divert a way back. Probing the original means keeping a provider loaded that the
divert has just disposed, and that is a new primitive, which is the standby-row
vocabulary of slice 7 in [473-slo-contracts.md](473-slo-contracts.md). The
issue's third missing piece stays open and is recorded under "Not decided here".

Rewind, fork and forward replay do not restore breaker state: it is not provider
state, and un-doing a provider's effects does not un-fail it.
`Session.replay_forward` re-invokes `self.call` live, so a replayed call is a
real call through the same guard on the live clock. It is counted, and it is
refused by name if the breaker is open, as an unapproved class-(c) step is. The
issue's phrase "replay sees the same decisions" is met in the weaker sense that
the trace records every decision with the numbers that produced it, so a reader
can reconstruct why a call was refused. Replay does not re-derive the decisions
from the trace, and the note does not claim it does.

## The interactions

### Where it sits in `Session.call`

The order is: halted, paused, key resolution, the cache lookups, then a
read-only PEEK at the guard, then the approval decision, the loop-axis count and
the invocation, where the guard itself decides atomically.

- E-Stop and a `slo` pause come first and win: a halted or paused session never
  consults a breaker.
- A cache hit returns before the peek, because no provider call happens.
- The peek runs before `_approval_decide_call`, because an approval decision can
  spend a standing approval or raise a ticket, and a refusal after that would
  leave the spend unmatched. An open breaker therefore spends no approval and
  adds no loop-axis count.
- The guard's own check is the authoritative one. If the breaker opens between
  the peek and the dispatch, the refusal comes after the approval decision and is
  treated like any raise after the gate; slice 2 has a test that says so.

### The E-Stop

The E-Stop overrides the breaker, as above. A breaker never arms the latch and
has no halt response: a breaker quarantines one provider, and halting a run is a
different decision. A composition that wants a halt on a breach already has
`slo { ... on breach halt }`, which is a declared and composition-authorized
halt. This is narrower than saying nothing in the language halts, which rule E8
of [443-estop-tier-contract.md](443-estop-tier-contract.md) does not say: E8 is
that no extern, stdlib binding or host body reaches the latch.

### The retry loops

No retry loop wraps a call to a provided key today. `lawful_retry.dispatch_on_loss`
has no caller in `src/revl` and concerns peer-pool loss; `validated ... retry N`
re-asks a model completion; `retry_idempotent` retries an emission inside a
provider body on `TransientError`. So the issue's "a retry consumes budget, an
open breaker does not" has nothing to attach to yet, and the note scopes it
rather than inventing a path.

What the guard does guarantee, and what is testable now: `BreakerOpen` is not a
`TransientError` and is never retried by `retry_idempotent`; a retry above the
guard is one more call that counts; a retry below it is invisible. When a retry
loop is later put around a provider call it must sit above the guard, and the
refusal is then a non-retryable fault that spends no budget by construction.

### The `slo` block and the divert

Independent and composable. `slo` measures a rate over activations and answers a
breach; a breaker counts consecutive failed calls of one provider and
quarantines it. Either, both or neither may be declared. A breach and a trip may
coincide and mean different things, and neither implies the other. The divert is
as described under "What the state belongs to".

### The fault test

A fault test injects one failure at a step and asserts the paradigm's guarantee,
once, under the test runner. A breaker is a runtime policy over real calls. A
fault test can produce failures that count toward a breaker and a breaker can
trip during one, but neither is the other. The docs line the issue asks for goes
in [fault-tests.md](../fault-tests.md) with slice 2, because it describes
behaviour that exists only once the clause is enforced.

## What is recorded, reported and controlled

### The trace

One new event kind, `breaker`, whose decisions are `trip`, `probe`, `close`,
`reopen`, `probe-timeout`, `late-result`, `reset` and `refused`. Each carries the
row, key, the state entered, the failure count, the settings that decided it and
the wall-clock `ts`. `why_runtime.SCHEMA_VERSION` moves from 3 to 4 additively. A
reader that sees no breaker event reports the row as `unrecorded`, never
`closed`.

`refused` is recorded per refusal, so an operator can see what an open breaker
turned away. Individual counted failures and successes are not traced: the
decisions carry the count that produced them.

### The status

`Session.state()` gains a `breaker` entry only when a clause is declared, as
`slo` does. Per row: `{row, key, realm, state, failures, threshold, windowMs,
cooldownMs, fingerprint, openedAgoMs, nextProbeInMs, lastDecision}`.

### Operator control and abuse

Any caller can raise N times and quarantine the row for every other caller, and
the guard cannot tell a provider fault from a caller who feeds a provider input
it raises on. The bounds are these, none stronger:

- the pre-body checks above keep caller errors that are decided before the body
  from counting;
- N consecutive failures within the window is the price, and the quarantine ends
  by itself after one cooldown and one good probe;
- every trip and every refusal is traced with its row, so abuse is visible;
- a provider whose methods should not raise on bad input should return `Err`,
  which is a success to the guard.

An operator may reset a row. `breaker_reset` is an operator verb under its own
authority, held like `estop` and never folded into `unload` or `commit`
(`operator.py`'s verb table); it returns the row to CLOSED and traces `reset`
with the operator. It is not needed to clear a trip, so no operator is required
for the way back, but one is available to override.

## Tiers

The IR clause is tier-neutral and the guard is py-only in slice 2. A tier that
does not enforce the block refuses the composition by name at admission, until
that tier enforces it. `estop.TIERS_WITH_ESTOP` is the precedent for naming the
honoring set, and the new set is `TIERS_WITH_BREAKER = frozenset({"py"})`. There
is no `unenforced` report, because there is no accepted, ignored declaration.
Other tiers follow the tier-parity rules once the Python behaviour is pinned
(slice 3).

## The tests slice 2 owes

The issue's acceptance and the review of this note give the list. Every test
fails on the base: the block does not parse, and the guard does not exist.

1. A provider that fails N calls in a row is refused by name on call N+1 and the
   refusal does not run its body.
2. After the cooldown exactly one probe is admitted; a probe success closes it
   and a probe failure reopens it and restarts the cooldown.
3. Two concurrent calls on a HALF_OPEN breaker admit exactly one probe.
4. A probe that hangs: the lease expires on the next call, the breaker reopens,
   and the late result changes nothing.
5. A stale failure outside the window does not trip, and a success clears the
   streak.
6. State across a swap: an equal fingerprint keeps it, a changed source resets
   it, and a divert resets it.
7. The guard is seen by a component calling its provider, by `Session.call` and
   by `revl run`.
8. A halted or paused session refuses before the breaker, an open breaker spends
   no approval, and a breaker that opens between the peek and the dispatch
   behaves as stated.
9. `BreakerOpen` is not retried by `retry_idempotent` and spends nothing.
10. Replayed calls go through the guard and are refused when it is open.
11. The trace events, the `state()["breaker"]` shape and the reset verb.
12. Each compile refusal, including every row kind that is refused.
13. IR byte-identity for a composition with no block.

## The slice plan

**Slice 1, this note.**

**Slice 2, the Python tier.** The grammar, `CompositionDecl.breaker`, the
conditional IR key and the refusals; the guard and its registry (a pure
`src/revl/breaker.py` for the state machine, the proxy in `runtime.py`); the
`plug` wiring; the `Session.call` peek; the trace event; the status; the reset
verb; the docs line; and the tests above. `revl grammar --write` regenerates the
derived grammars.

**Slice 3, other tiers.** Each tier either enforces the block or keeps refusing
it by name.

## Not decided here

- The way back after a divert. It needs a provider kept loaded for a probe, which
  is the standby vocabulary of slice 7 in 473, and a close would then be the
  signal to lift the divert.
- Breakers on `remote`, `host` and `seam` rows and on routed keys. Each has its
  own failure semantics and needs its own decision.
- A `site`-layer override of the block.
- A durable breaker state across restarts, and a composition-wide state across
  processes, which has no shared time base.
- A per-call `slo` population, which this guard's outcome record could feed.

## Relates to

- [473-slo-contracts.md](473-slo-contracts.md): the measure-and-respond surface,
  the conditional-key precedent and the divert.
- [fault-tests.md](../fault-tests.md): the injection-time experiment.
- [443-estop.md](443-estop.md) and
  [443-estop-tier-contract.md](443-estop-tier-contract.md): the halt that wins,
  and the tier set the breaker's set follows.
- [composition-rows.md](../composition-rows.md): the row spelling, the seam row
  this note declines, and `on_failure`.
- `src/revl/run.py`: `resolve_key` and `_Router`, the paths the guard covers.
- `src/revl/mcp/session.py`: `Session.call` and its gate order.
- `src/revl/lawful_retry.py` and `backends/python/runtime.py`'s
  `retry_idempotent`: the retry loops this guard does not wrap.
- `src/revl/why_runtime.py`: the trace vocabulary the new event extends.
