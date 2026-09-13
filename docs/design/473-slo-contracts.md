# 473: the SLO contract, its rollout gate, and runtime conformance

Design note for roadmap item 473 (issue #825). The companion note
[473-slo-rollout-gate.md](473-slo-rollout-gate.md) records the compile-time half
and the reasoning that landed it. This note is the contract end to end: the
surface as it stands, what the rollout gate's prediction actually rests on, what
runtime conformance would need before any of it can act, and which clauses of
the contract are refusable at compile time, which are runtime only, and which
are advisory.

This note changes no code. It exists because the observed half of item 473 was
declared a left-out in two places (`src/revl/composition.py` at the item-473
block comment, and the "what is not here yet" table in
[composition-rows.md](../composition-rows.md)) with the same reason both times:
a generation receipt and an `slo` trace event, neither of which exists. Naming
the prerequisites is the work this note does.

## Where this note starts

The premises were read against the tree, not against the roadmap line. Slice 1
has landed, so this note is not introducing the surface.

| piece | state | where |
|---|---|---|
| the `slo` block | landed | `parser._slo_block`, `_slo_value`; registries `SLO_DATUMS`, `SLO_IR_KEYS`, `SLO_BACKED_BY` |
| the compile-time gate | landed | `composition._slo_ceilings`, `_check_slo_bounds`; `G4`, `category="slo"`; runs on both `resolve` and `fold` |
| the manifest carrier | landed | `RowTable.slo`, `to_ir()`'s conditional `slo` key, `RowTable.slo_contract()` |
| the runtime monitor | absent | no producer, no sink |
| the fallback divert | absent | no fallback vocabulary in the language at all |
| the safe pause | absent | the phrase occurs only in the roadmap line and the companion note |
| the SLO receipt | absent | no receipt is attached to a generation today |
| the `slo` trace event | absent | `why_runtime.SCHEMA_VERSION` is 2, three event kinds |

Two of the five datums are gated. `SLO_BACKED_BY` maps `p95_latency` to item
260's `time` ceiling and `max_pending_tasks` to its `calls` ceiling;
`success_rate`, `recovery_time` and `approval_wait` are parsed, carried into the
IR and printed, and never compared against anything.

A numbering correction, because it is easy to build on the wrong one. Roadmap
item 460 is the service lifecycle contract ([lifecycle-contract.md](../lifecycle-contract.md),
issue #723): a documented behavioural contract over `Session` verbs and fiber
states, with no surface syntax. The design note
[460-two-phase-admission-forward-recovery.md](460-two-phase-admission-forward-recovery.md)
carries a 460 that its own header calls a placeholder chosen so the file sorts
last; it is issue #476. `src/revl/lifecycle.py` is neither, and says so: it is
item 461, the six failure stages `revl run` renders. This note composes with all
three and keeps them apart.

## The surface

### The grammar

```text
composition_clause := ... | slo_block
slo_block          := 'slo' '{' slo_entry (',' slo_entry)* '}'
slo_entry          := datum ':' literal
datum              := 'p95_latency' | 'success_rate' | 'recovery_time'
                    | 'approval_wait' | 'max_pending_tasks'
```

At most one block per composition. `slo` is contextual, recognised only in the
clause-head slot beside `row`, `remote`, `host`, `seam`, `place`, `use`, `stack`
and `site`, so the word stays an ordinary name everywhere else. The datum set is
a closed registry: an unknown datum is a refusal that lists the registry, an
empty block is a refusal, and a repeated datum is a refusal. Each literal is
read by its datum's value kind, so the unit is fixed at the surface and a bare
number can never be reinterpreted downstream: a duration goes through the same
`_duration_literal` as `cache ... ttl` and `liveness` (a bare number is
seconds), a rate is written bare and read as percent, a count is a plain
integer. Domain checks run at parse: a non-positive duration or count can never
hold, and a rate outside `(0, 100]` is not a percentage.

### Where it attaches, and why not lower

On the composition, and the alternatives were considered rather than skipped.

Not on a component. A component author cannot promise a latency they do not
control, and they already have the thing they can promise: an `emission[...]`
ceiling on the method that crosses. An objective is a claim about the assembled
system, so its author is whoever assembled it.

Not on a placement. A `place` is the operator's statement about which process
and tier a row runs on, writable from the base composition and the `site` layer.
An objective that moved with placement would change meaning on a re-place, and
the rollout gate would have no stable thing to refuse against.

The composition is also the rollout unit in every existing surface: `revl plan`,
`revl apply` and `revl canary` all take one, and the generation an SLO receipt
would hang on is a composition generation (`Session._generation`, incremented on
load, swap, rollback, undo and apply). A per-row objective remains expressible
later as an attenuation of the composition contract, and because the registry is
closed and the IR key conditional, adding one is additive.

### The comparator form, considered and not taken

The item's text sketches `slo { p95_latency <= 2s, success_rate >= 99.9% }`. The
landed spelling is `datum: value`, and the comparator is deliberately absent
because it carries no information. The direction is a property of the datum, not
of the entry: a duration and a count are upper bounds, a rate is a lower bound,
and the registry fixes that once. A writable comparator would make
`p95_latency >= 2s` spellable, which is either a typo the parser must refuse or a
second meaning nothing reads, and a contract with an unread clause is worse than
no contract. The same argument removes `%` and `ms` from the rate and duration
slots: the unit is implied by the slot, the way every other duration in the
language works.

### What it type-checks to, and what it does not

Nothing. An SLO datum is not a value in the language: no expression reads one,
there is no `Slo` type, and the checker never sees the block. This is worth
saying plainly so a later reader does not look for a type rule that was never
written. The block's whole static life is the parse-time domain checks above and
the composition gate below.

### Lowering

The contract lands on `RowTable.slo` keyed by the datum's unit-bearing IR name
(`p95_latency_ms`, `success_rate_pct`, `recovery_time_ms`, `approval_wait_ms`,
`max_pending_tasks`) with its source line, and `to_ir()` emits the `slo` key only
when a contract was declared. The unit is in the key name because the value is a
contract a rollout decision reads: an unlabelled `250` cannot be told from 250
seconds once it leaves the file. The absent key means "no contract" rather than
"an empty contract", and a composition that declares no block emits the same
document byte for byte as before the feature existed.

The gate runs after every source the composition names is known and before the
row table is built, so a refused rollout never produces a table, and it runs on
both the `resolve` and the `fold` path. The second is the load-bearing one: a
`site` layer is exactly where an objective gets widened without the base author
noticing, and a gate only the base path ran would be bypassable by a stack.

## The rollout gate: what a prediction can stand on

Four classes of evidence, strongest last. The point of ranking them is that only
one is landed, and it is the weakest.

### E1: a declaration against a declaration (landed)

The composition's own `emission[...]` ceilings, harvested through `cap_order`
the same way item 260's own gate reads them, compared against the objective:
`target >= every declared ceiling of the backing parameter`. Three honest things
about it.

It is not a measurement. It is a consistency check between two statements the
document makes, and what it refuses is self-contradiction. That is still a real
refusal worth having, because an objective no provider ever agreed to serve is a
promise with no author, and it is all the evidence a compile has.

The backing declaration is itself unenforced. Item 260 is explicit that `time`
is a runtime deadline on the capability and never a compile-time claim, and no
runtime meter for it exists in this tree: the only ceiling that becomes a live
counter is `calls` at grant mint, where `split_ceilings` peels it and it becomes
the approval-grant field `remainingUses`, which is grant scoped rather than
activation scoped, and whose exhaustion falls back to an approval prompt rather
than refusing. So E1 refuses a rollout against a promise nothing currently
checks. That does not make the refusal wrong. It makes its strength exactly "the
document contradicts itself" and no more, and a release note that said more than
that would be overclaiming.

The `max_pending_tasks` backing is a unit mismatch, and this is the place to
record it rather than discover it later. Item 260 pins a `requests`/`calls`
ceiling as per-activation-per-component, not realm-aggregate, and says a
realm-aggregate budget would be a new ceiling kind. A pending-task count is an
aggregate over a queue at an instant. The landed gate therefore catches one real
contradiction (a single route promising more crossings per activation than the
composition's whole pending bound) and is silent about the aggregate the datum
actually names.

### E2: the static cardinality verdict (designable now, not landed)

`cardinality(ir)` already produces a per-capability `{bound, kind, reason}` with
`kind` in `bounded` / `bounded-symbolic` / `unbounded`, and `audit_report` and
`revl audit --json` already carry it. A composition that declares
`max_pending_tasks` while its own roll-up verdict is `unbounded` on a route it
crosses is declaring a bound the compiler has already reported it cannot state.
Refusing that reads an existing verdict and invents no analysis, which is the
bar this item should hold itself to.

The honest limit is `bounded-symbolic`: a fuel expression like
`config.max_steps` is a proof schema, not a number, so a refusal may only be
made over an instantiated value, meaning the composition's row binds the config
field to a literal. Where it does not, the verdict is neither refused nor
admitted silently; it is reported as symbolic, the way 260 reports it.

### E3: a canary's recorded world

`revl canary --slice REALM --candidate FILE --promote-to BACKEND` already runs a
candidate on a designated realm and compares the two generations' recorded
worlds step for step in the `replay.Step` vocabulary, reporting the first
divergence with the `(component, realm)` that produced it. The canary note is
explicit that this is deliberately not a threshold on a counter, and never "error
rate crossed 2%".

That stance is not in tension with an SLO, and the reason matters. What the
canary refuses to do is invent a threshold. An `slo` block is a threshold the
author wrote down, in the source, reviewable. So the honest composition is: the
canary supplies the sample, the `slo` block supplies the bound, and the verdict
names the crossing the way a divergence already does.

What a canary cannot supply is a percentile. One slice's timeline is an ordered
account of what happened, not a distribution over enough calls to place a p95. So
E3 licenses "the candidate's recorded world already contains a crossing past the
declared bound", which is a witness. A witness is a fine reason to refuse a
promote and a bad basis for a claim about a percentile.

### E4: the predecessor generation's receipts

The strongest evidence, and the one that does not exist.
`Session._record_generation` appends `{generation, snapshot, ir, origin}` to a
history bounded by `HISTORY_LIMIT` (default 64), exported as a
`revl.generation-history` document. An SLO receipt on that entry makes "the
generation this rollout replaces measurably breached this same objective" a fact
`revl plan` can read and refuse on. That is the step that turns the gate from a
self-consistency check into something worth calling a prediction, which is why
the receipt is the next slice rather than the last one.

### What the gate refuses, stated plainly

No arrangement of E1 to E4 proves that a future SLO will hold. A gate claiming
otherwise would be claiming a proof about a statistical property of a world that
has not run yet. What the gate can refuse is narrower and defensible, and it is
exactly three things:

1. a composition whose own declarations contradict its objective (E1, landed);
2. a composition declaring a bound the compiler has already reported unbounded
   on a route it crosses (E2);
3. a rollout whose evidence already contains a breach of the same objective: a
   canary witness (E3) or the predecessor generation's receipt (E4).

Everything else is admitted and monitored. "Refused because predicted to breach"
means one of those three, and the refusal says which one, with the source
position and the binding value, the way the landed E1 refusal already does.

None of this needs a new verb. E1 and E2 belong where the composition gate
already runs; E3 belongs to `revl canary --promote-to`; E4 belongs to `revl plan`
(whose `basis` field already distinguishes `admitted` / `standalone` / `parsed` /
`none`) and to `revl apply`, which already refuses on drift from the plan's
basis.

## Runtime conformance

### What can be measured today, per datum

`src/revl/metrics.py` is the existing measurement surface: pure over a recorded
`why_runtime` trace, three numbers, and its own documented degrade
(`{"unavailable": ...}`) when the input is not there. It is the right place to
grow, and it is further from the five datums than the item's text implies.

| datum | nearest existing measurement | what is missing |
|---|---|---|
| `p95_latency` | `_duration_metrics`: the MEAN of `withdraw.ts - load.ts` per component, lifecycles paired by `(component, gen)` | a mean is not a percentile, and an activation lifetime is not a per-call latency. The trace carries three event kinds (`load`, `withdraw`, `emit`) and no call-completed event, so a p95 is not a filter over existing events; it needs a new event |
| `success_rate` | `_failure_metrics`: FAILED withdraws bucketed by diagnostic code, `unclassified` for a bare crash | the denominator. Emissions (`_emission_metrics`) and lifecycles (`_lifecycles`) are denominators of something else. "99.9%" of an unnamed denominator is not a contract, so the denominator must be declared and recorded |
| `recovery_time` | nothing | no clock is read anywhere on the recovery path, and no WAL record carries a timestamp: `WriteAheadLog._write` serializes the record as given and no writer stamps a time. Needs a timestamp on two records, which is a durable-format change |
| `approval_wait` | the approval and quorum WAL families (`approval-granted`, `approval-consumed`, `quorum-open`, `quorum-satisfied`, `quorum-expired`) | those records name DEADLINES (`expiresAt`, from the policy ttl), not waits. The wait is the interval between the prompt and the consent and neither end is stamped |
| `max_pending_tasks` | nothing aggregate | no queue owner. The nearest queues are `Session._pending_admits` (queued turn admissions) and the item-439 a2a task states. Neither is "the composition's pending tasks", and picking one silently would make the datum mean whichever was picked |

So the honest summary of the observed half is not "wire up a monitor". It is:
one datum needs a new trace event, one needs a declared denominator, two need
timestamps on a durable format, and one needs an owner for the quantity it names.

### The clock problem

`ts` is a `time.monotonic()` reading, documented as meaningful only as a
difference between events of the same run. Two consequences belong in the design
rather than in a later surprise.

A receipt cannot state a wall-clock breach time from the trace. It states an
interval, a sample size, and the generation it belongs to.

A multi-process placement (item 363) and a sandboxed one (item 411) have one
monotonic clock per process, so a composition-wide percentile cannot be computed
by pooling per-process traces. A conductor-level SLO needs a shared time base,
and the tree does not have one. Until it does, a multi-process composition's
runtime SLO is per-process and the receipt must say so, for the same reason
`estop.tier_estop_status` reports which tiers honor a halt instead of reporting a
stop it did not perform.

### The window and the sample

An SLO is a statistical claim over a window, and the landed surface has no
window. `p95_latency: 250ms` with nothing else is not falsifiable in either
direction: one slow call in a year breaches it, and so does every call. So a
verdict needs two more facts per datum, and they belong in the source beside the
target rather than in a runtime flag, because a target and the window it is
measured over are one promise and splitting them lets an operator quietly change
what the author promised.

```revl sketch
composition Shop {
  slo {
    p95_latency: 250ms over 5m min 200,
    success_rate: 99.5 over 1h min 1000 of crossings
  }
  ...
}
```

`over` is the window, `min` the smallest sample that may produce a verdict, and
`of` names `success_rate`'s denominator explicitly rather than leaving it to
whichever counter was nearest. Below the sample floor the datum's verdict is
`insufficient`, never `holding` and never `breached`: a verdict on four calls is
noise with a number attached. This is the honest-verdict discipline the tree
already uses in two places, item 260's `bound: null` with an explicit
`unbounded` kind and a machine-readable reason, and `metrics.py`'s `unavailable`
degrade detected by missing data rather than by a version field.

Adding `over` / `min` / `of` adds no datum, so the closed registry stays closed
and a composition that writes none is unaffected.

### The response, and why the escalation order is a decision

The item names three responses. Each maps onto machinery that exists or onto a
precisely named gap, and the mapping is the point: a breach response should not
be new runtime.

**Divert to a fallback provider.** Nothing in the language expresses an
alternative provider. The nearest landed thing is `on_failure(withdraw|result)`
on a `remote` row, and it is a decision about what a transport fault becomes (a
fault, or an in-band `Err` when every method returns `Result[T, Str]`), not a
second address. `remote` names a single peer, and composition-rows pins that two
peers of one service are two realms, so there is no alternate-address list to
fall back to. A divert therefore needs a fallback vocabulary, and the honest
shape is a composition clause rather than a runtime retry: a second row for the
same key, admitted as a standby whose activation G2 permits only while the
primary is withdrawn. That is a language change carrying its own G2 argument,
which is why it is the last slice and not a part of the monitor.

**A safe pause.** Does not exist, and the phrase occurs nowhere but the roadmap
line. What does exist is the E-Stop's first move: once the latch is armed, no new
boundary crossing is dispatched by that process. A pause is that first move
without the second. Refuse new crossings, keep the process, keep every bracket
registered, strand nothing. Read against the E-Stop's own contract the
difference is exactly rule E7 (die where it stands) removed, and that is why a
pause has to be its own verdict rather than a gentler E-Stop: an E-Stop's
dispositions are `stranded` and `estop-ambiguous`, it deliberately releases no
handles, and a pause that produced those would be an E-Stop with a friendlier
name. A pause also extends item 460's lifecycle contract, whose state set
(`PENDING`, `LOADING`, `ACTIVE`, `FAILED`, `DISPOSED`, `UNLOADING`, plus the
`teardown_disposition` attempt values) has no paused member and whose conformance
tests would have to grow one. That is a second reason to keep the pause in its
own slice instead of smuggling it in with a monitor.

**An E-Stop.** Landed, and reusable unchanged: arm the latch through
`estop.latch_path`, where `read_latch` fails closed so an unreadable or malformed
latch still reads as halted. The tier honesty comes with it: `TIERS_WITH_ESTOP`
is py, go, rust and java, wasm is reported statically from its compile-time
teardown section, and node/ts is not honoring pending its own issue. A breach
that latches an E-Stop inherits all of that, including that a non-honoring
component is killed rather than halted and that the halt report says so per
component. It also inherits the way back, which is `revl recover --wal`, because
an E-Stop is deliberately shaped to look like a crash to the recovery path, and
`revl estop --clear` removes the latch without resuming anything.

The escalation order is divert, then pause, then halt, and the reason is the
residue each leaves: a divert leaves none, a pause leaves everything registered
and owed but recoverable, a halt strands. A breach is a quantitative signal, so
the cheapest response that could clear it should run first.

Which responses a composition authorizes has to be declared, because a breach
that silently latched an E-Stop would be an operator action taken by a compiler,
and item 443's contract has a rule for exactly that instinct (E8, no self-halt:
there is deliberately no in-language E-Stop surface). So the proposed shape is a
per-datum `on breach` clause naming one of `divert`, `pause` or `halt`, with no
default beyond recording the receipt. A composition that declares an objective
and no response gets a receipt and nothing else, and that is the right default:
the contract is then a measurement, and this item's exit does not require every
composition to authorize a halt.

### The receipt

Follow the four signed receipts the tree already has rather than invent a fifth
shape. Kind `revl.slo-receipt`, version `"1.0"`, domain tag
`b"revl.slo-receipt/v1\x00"`, MAC over `attest._canonical_bytes` with
`attest.load_key`, exactly the `erasure_receipt.py` pattern. The distinct domain
tag is not decoration: it is what keeps an SLO receipt from verifying as a deploy
receipt or an erasure receipt.

The body carries the generation it belongs to, the contract as declared (which
`RowTable.slo_contract()` already produces as datum to `{target, line}`), and per
datum a verdict from a closed set:

```text
holding | breached | insufficient | unmeasurable
```

with the observed value and the sample size for the first two, and a reason for
the last two. `unmeasurable` is the load-bearing member. Three of five datums are
unmeasurable in this tree today, and a receipt that omitted them would read as
five objectives held.

It hangs on the generation history entry. `Session._record_generation` appends
`{generation, snapshot, ir, origin}`; the receipt is a fifth key, additive, and
`history_document()` exports it, so `revl undo`'s dossier and `revl plan`'s E4
read the same bytes rather than two derivations of them.

The trace side is a fourth `event` kind beside `load`, `withdraw` and `emit`,
plus a cause kind for a withdrawal a breach caused. Item 477's `LIVENESS_EXPIRED`
is the precedent to copy in full: a root cause the chain walk stops at, carrying
the declared bound and the observed value as operator-visible accounting, and
carrying no diagnostic `code`, because a breach classifies no `RevlError` and a
fabricated code would let a breach masquerade as a fault. That is the
`SCHEMA_VERSION` 2 to 3 bump the companion note said waits for a receipt shape.

### What a receipt proves, and what it does not

It proves three things: what the contract said, what this generation measured
over the declared window, and that the bytes were not edited after signing.

It does not prove the objective held. A `holding` verdict over a five minute
window with two hundred samples is a statement about those samples, and the
receipt carries the window and the sample size so that a reader cannot mistake
one for the other.

It does not prove the measurement was complete. A non-honoring tier, a second
process with its own monotonic clock, an unstamped event, a sample under the
floor: each degrades the datum to `insufficient` or `unmeasurable`, and the
receipt names which and why.

And it is not an availability claim about anything outside the declared datum
set. The item's own scope line says the datums are the predicted or observed
values the runtime can measure, not arbitrary external service-level indicators,
and the closed registry is how that scope is enforced rather than promised.

## Honest scope: which clause is which

| datum | compile-time refusable | runtime only | advisory |
|---|---|---|---|
| `p95_latency` | yes, against every declared `time` ceiling (E1, landed) | the percentile itself: it needs a per-call event that does not exist | no |
| `success_rate` | no: nothing in the language declares a rate | yes, once a denominator is declared | no |
| `recovery_time` | no | measurable only after a recovery has finished | yes, see below |
| `approval_wait` | no: a policy ttl is a deadline, not a wait | yes, once both ends are stamped | partly, see below |
| `max_pending_tasks` | yes, two ways: against `calls` (E1, landed) and against an `unbounded` cardinality verdict (E2) | the aggregate the datum names needs a queue owner | no |

`recovery_time` is advisory by its own timing. By the moment it is known the
recovery is over, so there is nothing left to divert or pause: it can be a
receipt line and an input to the next rollout's E4, and it can never be an
in-flight trigger. Wiring it to a response would be theatre.

`approval_wait` is partly advisory for a different reason. The wait being
measured is a human's latency, and the only lever revl owns over it is to stop
asking, which is a policy change (an auto-approve rule, a distillation, a quorum
threshold) and not a decision a breach should make on its own. So it is measured
and reported and deliberately not wired to divert, pause or halt.

The plain statement, so that no downstream sentence has to reconstruct it: no
clause of this contract is a proof that a service level will be met. Two of five
datums are refusable today, and only against declarations, one of which is
itself unmetered. One datum is advisory by timing and one partly so. None of the
five is currently measured at all. "revl enforces your SLOs" would be false.
What revl can say, after the slices below, is that it refuses a rollout whose own
declarations contradict its objectives, and that it records a signed per-generation
account of what happened with every gap in that account named.

## The slice plan

**Slice 1, landed: parse, check, carry.** The block, the closed registry, the
two backed datums, the `G4` refusal on both composition paths, the conditional IR
key. No runtime monitoring, no receipt, no response. Recorded in the companion
note; restated here only so the plan reads whole.

**Slice 2: the receipt and the trace vocabulary, with no producer.** The
`revl.slo-receipt` kind, version and domain tag; the closed verdict set; the
signing and verification following `erasure_receipt.py`; the fourth event kind
and the breach cause kind in `why_runtime`, with the `SCHEMA_VERSION` bump. Pure
vocabulary plus the decision the vocabulary encodes, which is what item 477's
first slice did, and it means every later producer emits into a shape that is
already tested and every consumer of the trace already tolerates.

**Slice 3: the window, the sample floor and the denominator.** `over`, `min` and
`of` on an entry, parsed, domain-checked and lowered to the same conditional
`slo` key. Still no monitor. This slice is what makes a verdict falsifiable, so
it precedes anything that produces one.

**Slice 4: measurement, for the datums reachable without a durable-format
change.** `success_rate` over the declared denominator, and `p95_latency` over a
new per-call trace event. Extends `metrics.py`, keeps its degrade discipline, and
produces a receipt at generation end with `unmeasurable` on the three datums it
cannot reach. No response is taken.

**Slice 5: the monitor and the response ladder.** The per-datum `on breach`
clause; the pause as a new lifecycle verdict with its conformance coverage; the
halt reusing the E-Stop latch unchanged and inheriting its tier honesty. Divert
is not in this slice.

**Slice 6: the rollout gate's remaining halves.** E2 (refuse a bound the
cardinality verdict already reported unbounded) and E4 (the predecessor's receipt
as a `revl plan` input, and a `revl canary --promote-to` refusal on a witness).
Both become honest only once a receipt exists, which is why they follow slice 2
rather than leading.

**Slice 7: the fallback vocabulary and the divert.** A standby row for a key,
its G2 argument, and the divert as a response. A language change of its own size.

**Left out, with the prerequisite named.** `recovery_time` and `approval_wait`
need timestamps on WAL records, a durable-format change under `WAL_VERSION`
(reader-compatible, since `wal.read_wal` keeps unknown record kinds and keys, but
still a format decision that is not this item's to make alone).
`max_pending_tasks` as the aggregate it names needs a queue owner and a
realm-aggregate ceiling kind, which item 260 already identified as a new kind
rather than a widening. A composition-wide percentile across processes needs a
shared time base.

## Exit

`Exit:` a composition whose own `emission[...]` ceilings contradict its declared
`slo` block is refused at compile time, naming the datum, the target, the binding
ceiling and the line (slice 1, met); and a generation that runs under a declared
contract produces a verifiable `revl.slo-receipt` on its generation-history
entry carrying one closed verdict per declared datum, where a `breached` verdict
on a datum whose composition declared a response has taken that response and the
receipt names it, and every datum this tree cannot measure reads `unmeasurable`
with a reason rather than `holding`.

## Relates to

* Item 260 (emission cardinality bounds): the ceilings E1 reads and the
  `cardinality` verdict E2 would read, the `G4` code and the opt-in shape the
  gate copies, and the source of two honest limits recorded above (`time` is a
  runtime deadline and never a compile-time claim; a `calls` ceiling is
  per-activation-per-component and not realm-aggregate).
* Item 460 (the service lifecycle contract, issue #723): the state vocabulary a
  monitor and a pause must speak (serving, cancel-requested versus
  cancel-completed, owned-after-failure, recovery-resume) instead of inventing
  a health vocabulary of its own.
* Item 443 (operator E-Stop): the halt this item's worst case latches, reused
  unchanged, together with its latch semantics, its per-tier honesty and its
  rule E8 that nothing in the language halts itself.
* Items 363 and 411 (tier and sandbox placement): the reason a composition-wide
  percentile is not available today, and the population a per-process receipt
  has to enumerate.
* Item 47 and the WAL (`wal.py`, `recovery.py`): the durable record two datums
  would need a timestamp on, and the recovery path `recovery_time` would measure
  if it had a clock.
* Item 477 (liveness expiry): the precedent this note copies twice, a
  vocabulary-first slice with no producer, and a root cause that carries the
  declared bound and the observed value and no fabricated diagnostic code.
* Item 472 (erasure receipts) and item 474 (component certificates): the signed
  receipt shape, the domain-tag discipline, and the "what a receipt proves"
  section this note owes its own version of.
* `revl canary` (progressive delivery) and `revl plan` / `revl apply`: where the
  gate runs, and the reason E3 is a witness rather than a metric.
* Item 424 (`remote` rows and `on_failure`): the closest existing divert-on-
  failure decision, and the measured reason it is not a fallback provider.
