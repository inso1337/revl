# 571: The circuit breaker: three states, one failure definition, and a way back

Design note for issue #2710. Design number 571, assigned rather than claimed:
570 is claimed by PR #3136 on an open branch, and main's highest merged design
number is 569. A scan of the open pull requests on 2026-11-09 found no claim on
571.

Status: this is the first slice the issue asks for. It settles the surface, the
defaults and the interactions and changes no code. The issue's remaining
acceptance items (the Python-tier open, close and half-open behaviour, and the
tests) are slice 2, planned at the end of this note.

## The gap

A component that keeps failing keeps being called. Four pieces exist around the
problem and none of them trips on repeated failure:

- `fault.py` injects one failure at a step and asserts the paradigm's own
  guarantee (FAILED, inverses newest first, siblings unaffected,
  [fault-tests.md](../fault-tests.md)). It is a test-time injection, not a
  runtime policy.
- The `slo` block measures a windowed success rate and, on a breach, diverts to
  a declared fallback provider or pauses (`slo.admit_divert`, `slo.pause`,
  [473-slo-contracts.md](473-slo-contracts.md)). The divert is one-way: nothing
  probes the original provider again.
- Retries are metered by the monotone retry budget
  (`lawful_retry.RETRY_BUDGET_KEY`), so a retry storm is impossible; the budget
  says how many replays remain, not whether the provider is worth replaying to.
- The E-Stop latch (`estop.py`, [443-estop.md](443-estop.md)) halts everything,
  by hand, and is deliberately not reachable from the language (rule E8 of
  [443-estop-tier-contract.md](443-estop-tier-contract.md)).

A search of the compiler, stdlib, runtime and docs for circuit breaker,
half-open, consecutive failures and failure threshold finds nothing that
applies. This note supplies the missing policy, not a new runtime primitive:
every mechanism the policy needs already exists.

## The decision, in one paragraph

A composition may declare a `breaker` block naming one or more of its rows. A
named row gets a per-row, per-live-generation state machine with three states.
CLOSED counts consecutive failed CALLS to that row within a window and trips to
OPEN when the count reaches N. OPEN refuses a call to that row by name, without
crossing a boundary and without spending a retry budget. After a cooldown, the
next call moves the breaker to HALF_OPEN and is admitted as the single probe; a
probe success closes the breaker and resets the count, a probe failure reopens
it and restarts the cooldown. A failure is a call that raises, classified with
the same diagnostic vocabulary the runtime already uses for a failed lifecycle,
so the breaker and the declared `success_rate` cannot disagree about what a
failure is; they count different populations and are never pooled. Every trip,
probe admission and close is a recorded decision, so a reader and a replay see
the same decisions. The breaker is not a latch: no operator action is needed to
clear it, and it never arms an E-Stop.

## The surface

### The grammar

```text
composition_clause := ... | breaker_block
breaker_block      := 'breaker' '{' breaker_entry (',' breaker_entry)* '}'
breaker_entry      := row ':' [ INT ] breaker_qualifier*
breaker_qualifier  := 'over' duration | 'cooldown' duration
```

`breaker` is contextual, recognized only in the clause-head slot beside `row`,
`slo`, `place`, `grant` and the others, so the word stays an ordinary name
everywhere else. `over` and `cooldown` are matched by spelling as plain
identifiers, the same discipline `slo`'s `over`, `min` and `of` keep. The
duration goes through the same `_duration_literal` as `slo`, `cache ... ttl`
and `liveness` (`ms`, `s`, `m`, `h`, `d`; a bare number is seconds), so the
unit is fixed at the surface and cannot be re-read downstream.

At most one block per composition. An unknown row is a refusal that lists the
rows the composition actually declares. A repeated row in the block is a
refusal. An entry must write at least one of the threshold, `over` or
`cooldown`; an entry that writes none is refused rather than silently
defaulted, so `breaker { db: }` is a typo with an error, not a policy.

```revl sketch
composition storefront {
  row db from "db.rvl"
  row cache from "cache.rvl"
  breaker {
    db: 5 over 60s cooldown 30s
    cache: 3 over 2m
  }
}
```

### Where it attaches, and why not lower

On the composition, keyed by row, for the three reasons `slo` attaches there.

Not on the component. A breaker is a statement about the ASSEMBLED system: how
many failures of this provider the composition is willing to absorb before it
stops calling it. A component author cannot promise the provider will be
reachable, and a component does not own the retry budget, the E-Stop latch or
the trace the decision is recorded in; the composition does.

Not on the service method. A method-level breaker would key on a capability
rather than a provider, so two rows providing the same service would share a
counter that one provider's failures did not earn, and a method that never
crosses would carry a policy that never fires. The row is the composition's
name for one component plus one provider, which is exactly the unit a breaker
governs.

Not on the row declaration itself. The row is the provider binding; the breaker
is a policy over failures of that binding. Keeping it a composition-level block
means a `site` layer or a `use` can change the provider without silently
changing the policy, and it follows the `slo` precedent: the block is parsed
into `CompositionDecl.breaker`, domain-checked during composition admission,
carried on the row table under its own conditional IR key, and byte-identical
to the pre-feature document when the block is absent.

The IR carries the entry with units in the key names:

```json
"breaker": {"db": {"threshold": 5, "overMs": 60000, "cooldownMs": 30000}}
```

Only the entries that declared an override appear; a default is applied by the
reader, the same way `slo`'s absent `over` is the whole population. A
composition with no block emits no `breaker` key.

### The defaults

| setting | default when omitted | why |
|---|---|---|
| the block | absent | opt-in; a composition that did not declare it emits the pre-feature document byte for byte |
| N, the threshold | 5 | the number the issue leaves undefined; small enough to stop a clearly broken provider, large enough not to trip on one retry |
| the window | 60s | a failure older than a minute does not belong to the same streak; a minute spans a typical backoff-and-redeliver cycle |
| the cooldown | 30s | half a window: long enough for a provider to recover from a restart, short enough that the way back is not a manual act |
| probe calls | exactly 1 | one probe, not a ramp; the probe's result is the whole evidence |
| reset rule | probe success closes and resets the count; probe failure reopens and restarts the cooldown | stated below |

The defaults are the reader's constants, named here so the admission and the
runtime cannot drift: an omitted value is not written into the IR, it is read
from this table, and a reader can always tell which number will be used.

### Compile-time refusals

The block is checked during composition admission, in the same style as the
`slo` bounds refusal (`composition._check_slo_bounds`, code `G4`, category
`slo`), under its own category `breaker`:

- an unknown row, with the rows the composition declares listed in the hint;
- a repeated row;
- a non-positive threshold or a non-positive duration (a threshold of zero
  would trip before the first call and a zero cooldown would probe on every
  call, so neither is a policy);
- a malformed or duplicated qualifier.

Nothing is checked at compile time about the provider's health, for the same
reason `slo` cannot predict a latency: the contract is over observed outcomes.

## The states and the transitions

```text
              N failures in the window
   CLOSED -----------------------------------> OPEN
     ^                                          |
     |  probe success            cooldown elapses|
     |  (reset count)                            v
     +-------------------------------------- HALF_OPEN
                  probe failure reopens:
                  OPEN, cooldown restarts
```

| state | a call | consequence |
|---|---|---|
| CLOSED | dispatched | a FAILED settle increments the streak; a success resets it; at N the breaker trips |
| OPEN | refused by name | no crossing, no retry budget spent, not counted as a failure or a success |
| HALF_OPEN | one probe admitted, all other calls refused | probe success closes; probe failure reopens and restarts the cooldown |

A refusal in OPEN names the row and the state, and says when the next probe is
due. It is a typed runtime refusal in the shape of `Session._refuse_if_paused`,
not a generic exception, so a caller can tell "the provider is quarantined" from
"the call failed".

### The failure definition

The unit a breaker governs is a CALL, because a call is what the caller makes
and what the breaker refuses. A failure is a call to the named row's provided
operation that raises after the crossing seam; a success is a call that returns.
The population is calls, per row, per live generation.

The CLASSIFICATION is not new. The runtime has one way to say what a failure
was: the observed `FAILED` settle `metrics._is_failed` reads, and the `RevlError`
code `metrics._failure_metrics` buckets, with a bare crash left unclassified. A
provide-method error is not a lifecycle fault today: it propagates to the
caller and the component stays ACTIVE (see
[fault-tests.md](../fault-tests.md), section 12). So the per-call outcome is the
one record the breaker needs that the runtime does not already write. Slice 2
writes it, and the record carries the same `diagnostics.classify` code the
lifecycle failure carries. That is the one definition the issue asks for: one
classification, two populations. The breaker's streak is over calls; the
declared `success_rate of activations` divides over lifecycles; the two are
never pooled and a verdict always names which population it measured.

Consequences, stated because they are choices:

- A component that fails to load is not a failed call. It never served one. The
  load failure is a different event with its own cause, and folding it in would
  count a population the caller never reached. Slice 2 may extend the counter to
  a load failure under the same classification, but that is a widening with its
  own test, not a silent equivalence.
- A call that returns an in-band `Err` is a success to the breaker: the breaker
  watches the crossing, not the application's result type. A `remote` row
  declared `on_failure(result)` turns a transport fault into an in-band `Err`,
  so that row does not trip the breaker. Counting it would need the breaker to
  know the row's result type, which is a widening slice 2 does not take.
- A call refused by an open breaker is not a failure and not a success. If it
  were a failure the breaker would never close; if it were a success it would
  hide the outage.
- A success anywhere in the streak resets the count to zero. "Consecutive" is
  literal.

### The window and "consecutive"

A failure joins the current streak only when the previous counted failure is no
older than the window; otherwise the streak restarts at one. This makes the
window the answer to "how stale may the previous failure be and still count",
which is the only reading under which both `N` and `over` have independent
work. A success resets the streak regardless of the clock.

### Exactly one probe

HALF_OPEN admits exactly one call: the admission flag is set atomically with
the state transition, and every call that arrives while the probe is in flight
is refused with the same typed refusal as OPEN. The probe result settles the
state exactly once.

A probe that never returns is the one case the state machine cannot resolve on
its own. The design refuses to add a background timer, which a replay could not
reproduce: instead a probe older than the window is PRESUMED failed at the next
call, the breaker reopens, and the cooldown restarts. The presumption is
recorded as `probe-timeout`, with both timestamps, so it is visible rather than
a silent clock read. This keeps the breaker self-clearing without a daemon and
without a latch.

## The interactions

### Retry budget

The breaker and the retry budget answer different questions. The budget says how
many lawful replays remain; the breaker says whether the provider is worth
replaying to. The rule the issue asks for:

- a retry inside a CLOSED breaker consumes retry budget exactly as any replay
  does, through `lawful_retry._retry_grant`'s decrement of `RETRY_BUDGET_KEY`;
  the breaker neither reads nor holds a budget of its own;
- an OPEN breaker refuses the call BEFORE a replay is constructed, so no
  `_retry_grant` is built and nothing is decremented. A refusal is not a
  replay, so it cannot spend a replay's budget;
- a HALF_OPEN probe is a normal call: if it is itself a replay, it pays the
  budget; if it is a first attempt, it does not.

The test that pins this is named in "The tests this design owes"; it fails today
because neither the clause nor the refusal exists.

### E-Stop

The E-Stop overrides the breaker, and the order is fixed:
`_refuse_if_halted` runs first, then the declared `slo` pause, then the
breaker. A halted session never consults a breaker; the halt is the verdict and
its own message stands. An open breaker never arms the latch: rule E8 of
[443-estop-tier-contract.md](443-estop-tier-contract.md) says nothing in the
language halts itself, and a breaker that latched an E-Stop on five failed calls
would be exactly that self-halt. Clearing the breaker is never an operator act:
the cooldown and the probe are the way back, which is the difference between a
quarantine and a latch.

### The slo block, and the divert

The two surfaces are independent and compose in one direction:

- `slo` measures a rate over a population and responds to a breach; a breaker
  counts consecutive failures of one provider and responds to that provider's
  own record. A composition may declare both, neither or one.
- A breach and a trip may coincide, and they mean different things: the breach
  says the service-level objective is not holding; the trip says this provider
  is not worth calling. Neither implies the other.
- The way back after a divert, which is the issue's third missing piece: the
  breaker records the provider SOURCE it was bound to, and a half-open probe is
  dispatched to that source, not to whatever a divert last put in the row's
  `from`. A probe success closes the breaker and is the evidence that the
  original is reachable again; lifting the divert (re-admitting the row with the
  original provider) is the divert machinery's own re-admission, not a second
  breaker decision. That is the only shape in which "a way back" is not a second
  divert: the breaker supplies one probe and a close verdict, and the existing
  divert path performs the re-admission, so the two features do not each grow a
  private router.
- Slice 2 proves the no-divert case. Making the diverted row's probe reach the
  original source, and treating a close as the un-divert signal, is the
  integration with the 473 slice 7 standby vocabulary. This note fixes the part
  the two must agree on: the breaker's source is the one captured at bind time,
  never the diverted one.

### The fault test

A fault test is a source-level experiment: it injects one failure at a named
step and asserts the paradigm's guarantee (FAILED, no residue, LIFO inverses,
siblings unaffected), and it runs once under the test runner. A breaker is a
runtime policy: it counts real failures over a window and refuses real calls. A
fault test can produce the failures that trip a breaker, and a breaker can trip
during a fault test, but neither is the other. In particular a breaker does not
assert residue, and a fault test does not clear a breaker. The docs line the
issue asks for belongs with slice 2: it goes in
[fault-tests.md](../fault-tests.md), beside the fault test's own definition,
because it describes behaviour that only exists once the clause is enforced.

### Replay and the trace

Every DECISION is recorded: `trip`, `probe`, `close`, `reopen` and
`probe-timeout`, each with the row, the generation, the state entered, the count
at that moment, the threshold, window and cooldown that decided it, and the
timestamp. The trace gains one event kind, `breaker`, and `why_runtime`'s
`SCHEMA_VERSION` moves from 3 to 4 additively, the same way v2 and v3 added
without changing a v1 field.

What is NOT recorded, stated so the claim is not read wider than it is: every
counted failure and every success in the streak are not individually traced.
The decisions carry the count that produced them, so a replay sees the same
decisions, which is what the issue asks for; it does not re-derive the streak
from individual calls, because those calls are not what the trace owes. A
reader that sees no breaker event reports the breaker as `unrecorded`, never as
`closed`: an absent record is not a healthy provider.

Replay reads the recorded timestamps, not its own clock. A refusal that falls
between events is derived from the recorded state and the recorded opening
time. That is the honest scope: the decisions the breaker took are recorded; a
call that was never made is not a decision the trace can owe.

## What is refused and what is advisory

Refused, at compile time: the block's own domain (rows, repeats, positive
values, one block, known qualifiers).

Refused, at runtime: a call to an OPEN or probe-busy HALF_OPEN row, by name.

Advisory, and stated so nobody builds on it: the threshold, window and cooldown
are a policy the author declares, not a measurement of the provider's health.
The breaker cannot prove a provider is healthy after one probe; it proves only
that one call succeeded. A probe success closes the breaker even if the provider
is flapping, and the next N failures trip it again. That is the circuit
breaker's contract and this note does not pretend to a stronger one.

## The tests this design owes (slice 2)

The issue's acceptance names the trip, the probe (close and reopen) and the
retry-budget cases. The four below make each one a named test, and each fails on
the base because the clause does not exist:

1. **Trip and refusal.** A provider that fails N calls in a row is refused by
   name on call N+1, and the refusal does not cross a boundary.
2. **One probe, then close.** After the cooldown exactly one probe is admitted;
   a success closes the breaker and the next call is dispatched.
3. **One probe, then reopen.** A probe failure reopens the breaker, restarts the
   cooldown, and no second probe is admitted before it elapses.
4. **Open does not spend the retry budget.** A call refused by an OPEN breaker
   leaves `lawful_retry.retry_budget` unchanged, while the same failure under a
   CLOSED breaker spends it exactly as a lawful replay does.

The tests classify the failing calls with the same `metrics._failure_metrics`
codes the runtime already buckets, so the breaker cannot invent a failure the
metrics would call something else.

## The slice plan

**Slice 1, this note.** The surface, the states, the defaults, the interactions
and the test contract. No code.

**Slice 2, the Python tier.** `breaker` in the grammar, parser, `CompositionDecl`
and the conditional IR key; the state machine in a pure `src/revl/breaker.py`
(the `slo.py` shape: a pure module the runtime and the tests can both drive);
the wiring in `Session.call` after `_refuse_if_halted` and `_refuse_if_paused`;
the `breaker` trace event; the read-only status the session reports; and the
four tests above. `revl grammar --write` regenerates the derived grammars.

**Slice 3, other tiers.** The IR clause is tier-neutral, so each tier reads the
same `breaker` key. The tier-parity rule applies: a tier that does not enforce
the clause says so in its status the way `estop.TIERS_WITH_ESTOP` splits
honoring from static from unknown, and a composition placed on a non-honoring
tier reports the breaker as `unenforced` rather than silently calling it
enforced.

**Later, with 473 slice 7.** The diverted-row probe described in "The slo block,
and the divert".

## Honest limits

- The counter is per live generation and per process. A restart starts CLOSED,
  which is the pre-trip state, not a restored one. The recorded events are for
  replay and audit, not for restoring a quarantine across a restart. A durable
  breaker state is a state-format decision with its own note, and it is not
  needed for the issue's acceptance.
- The breaker counts failed calls to the row, not failed lifecycles and not the
  component's activation outcome. The two populations are deliberately kept
  apart: a call error leaves the component ACTIVE today, so counting a lifecycle
  would miss the very failures the breaker exists to stop. Slice 2 adds the
  per-call record under the shared classification, with a test that proves a
  failure is counted once and not once per population.
- The probe presumption is a clock read, and the recorded timestamps make it
  reproducible; it is not a provider health check.
- A composition-wide breaker (one state across processes) is not available for
  the same reason a composition-wide percentile is not: there is no shared time
  base. This note keys on the row and leaves the realm aggregate open.

## Relates to

- [473-slo-contracts.md](473-slo-contracts.md): the measure-and-respond surface
  this note composes with, the `success_rate of activations` failure definition
  it imports, the pause it defers to, and the one-way divert it gives a way back.
- [fault-tests.md](../fault-tests.md): the injection-time experiment this note
  distinguishes from a runtime policy.
- [443-estop.md](443-estop.md) and
  [443-estop-tier-contract.md](443-estop-tier-contract.md): the halt that
  overrides the breaker, and rule E8 that stops a breaker from halting anything.
- `src/revl/lawful_retry.py`: the monotone retry budget the open state must not
  spend.
- `src/revl/metrics.py` and `src/revl/slo.py`: `_is_failed`,
  `_failure_metrics` and `_activation_observations`, the shared classification
  and the lifecycle population the breaker does not pool with its own.
- `src/revl/why_runtime.py`: the trace vocabulary and `SCHEMA_VERSION` the
  `breaker` event extends.
- [460-two-phase-admission-forward-recovery.md](460-two-phase-admission-forward-recovery.md)
  and [lifecycle-contract.md](../lifecycle-contract.md): the lifecycle states a
  breaker reports over, and the reason the breaker is a dispatch decision rather
  than a new fiber state.
