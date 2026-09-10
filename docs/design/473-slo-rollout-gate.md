# 473: the composition SLO contract and its rollout gate

Design note for roadmap item 473. It records what the item asks, what the tree
actually held when the item was picked up, the one slice that lands with this
note, and the half that stays design only with the reason it cannot land yet.

## What the item asks

The roadmap text (item 473, "SLO CONTRACT + ROLLOUT GATE") asks for:

> A composition-level `slo { p95_latency, success_rate, recovery_time,
> approval_wait, max_pending_tasks }` contract the runtime monitors, refusing a
> risky rollout when predicted budgets or SLOs would fail, diverting to fallback
> providers, or triggering a safe pause or e-stop before a full incident, with
> SLO receipts tied to the running generation. It extends budgets (item 260),
> lifecycle (item 460) and e-stop (item 443). The scope is the declared budget
> set and the predicted or observed values the runtime can measure, not
> arbitrary external service-level indicators. Exit: a rollout predicted to
> breach a declared SLO is refused, and a live breach triggers the declared
> fallback or pause with an SLO receipt on the generation.

Two halves, then: a PREDICTED half (refuse a rollout that would breach) and an
OBSERVED half (a live breach diverts, pauses or latches a receipt).

## What the tree held: the stale premises

Every premise was checked against the code before building on it. Four did not
survive.

1. **There was no `slo` vocabulary and no contract to extend.** No `slo`
   keyword, clause, IR key, panel or design note existed anywhere in
   `src/`, `docs/` or `examples/`. The item's "contract" had no artifact, so
   this note is not extending one; it is introducing the surface.

2. **Four of the five datum names map to no declaration in the tree.** No
   declaration produces `p95_latency`, `success_rate`, `recovery_time`,
   `approval_wait` or `max_pending_tasks`. The nearest real numbers are item
   260's per-method `emission[...]` ceilings (`time` and `calls`, checked by
   `lower._check_declared_ceilings`) and liveness's `liveness <dur>` bound.
   `approval_wait` and `max_pending_tasks` have no source at all: nothing in
   the tree declares an approval wait or a pending-task count, so no runtime
   can measure them and no compiler can predict them.

3. **The OBSERVED half has no sink.** There is no generation receipt structure
   to hang an SLO receipt on, no `slo` trace event (`why_runtime.SCHEMA_VERSION`
   is `2` and carries `LOAD`/`WITHDRAW`/`EMIT` only), and no fallback-provider
   vocabulary. `lifecycle.py` is 79 lines. The closest thing that already
   exists is `on_failure(withdraw|result)` on a `remote` row, and it is a
   decision about a transport fault, not about a budget.

4. **The item's exit test is the only half whose vocabulary exists today**, and
   it is a COMPILE-time gate: "a rollout predicted to breach a declared SLO is
   refused" is a statement about numbers the compiler already has.

So this note lands the predicted half, which is the half the tree can carry
honestly, and declares the observed half a left-out rather than inventing a
generation receipt and a trace event to have something to point at.

## The slice that lands: the declared contract and the compile-time gate

### The surface

`slo` is a clause on a composition, at the same level as `row` and `place`:

```revl
composition Shop {
  use "services.rvl"

  slo { p95_latency: 250ms, success_rate: 99.5 }

  row @checkout from "consumer.rvl" provides checkout
  remote @billing provides billing: Billing
    at host("billing.internal:8443")
}
```

Three things are fixed at the surface, deliberately:

* **The datum set is closed.** `SLO_DATUMS` in `parser.py` is the whole
  vocabulary: an unknown datum is a parse refusal naming the accepted set. An
  open set would be an unmonitorable contract, since a name nothing measures
  can never be checked.
* **The unit is the datum's.** `p95_latency`, `recovery_time` and
  `approval_wait` take a duration (`ms`, `s`, `m`, `h`, and a bare number is
  seconds, the same rule every duration in the language follows);
  `success_rate` takes a percentage and may be fractional; `max_pending_tasks`
  takes a count. The unit is not inferable from a bare number, so it is not
  negotiated: a duration datum with a count unit is refused at parse.
* **Only some datums can be backed by a declaration.** `SLO_BACKED_BY` maps
  `p95_latency -> time` and `max_pending_tasks -> calls`, the two item-260
  ceilings. The gate reads those and is silent about the other three, which
  have no declarable value to compare against. Naming that map in one place is
  the honest form: it says precisely which half of the contract is checked.

### The gate

`_check_slo_bounds` in `composition.py` is the gate, and it is a rollup of
numbers that already exist rather than a new analysis:

* it reads every `emission[...]` ceiling the composition declares
  (`_slo_ceilings`), the same way `lower._check_declared_ceilings` reads them
  for item 260's own check, through `cap_order.parse_cap` and
  `split_ceilings`. The scope is EVERY file the composition names: the row
  sources first and the `use`d files before them, and every route in them
  whether or not a row crosses it. See "The conservative scope" below;
* for each datum in `SLO_BACKED_BY` it compares the declared SLO against EVERY
  declared ceiling of that kind and refuses with `code="G4", category="slo"`
  when the SLO is STRICTER than any one of them (`target < value`). A hard
  ceiling of `time="2s"` cannot deliver a `p95_latency: 250ms` promise, so the
  rollout is refused before it starts. The predicate is therefore `target >=
  every ceiling`, which makes the BINDING ceiling the LARGEST one of that kind:
  that is the value the target has to reach, so that is the one the refusal
  names (and, when there is more than one, the message also reports how many
  were compared and which of them is tightest);
* the bound is INCLUSIVE: a target exactly at the binding ceiling resolves. The
  ceiling is a bound the provider already agreed to, so a promise to meet it is
  not a breach.

The gate runs on BOTH composition paths, `resolve` and `fold`, because a layer
is where an SLO is most likely to be widened without the base author noticing,
and a gate that only the base path ran would be bypassable by a stack.

The gate is INERT unless the program declares an `slo` clause. A composition
that writes none is byte-identical in behaviour and in the IR to what it was
before this slice, which is the property that keeps an opt-in contract from
being a breaking change and is why the refusal is a new `category` value rather
than a reused one.

#### The conservative scope

The gate reads ceilings from every file the composition names, including a file
it only `use`s, and on every route, including one that no row of this
composition crosses. That is deliberately BROADER than the set of routes this
composition actually crosses, and the breadth is the point: a declared
`emission[...]` ceiling is a promise about that backing parameter wherever it
is written, so a composition that carries a provider whose `db` method caps
`time=30s` carries a 30s crossing whether or not a row crosses `db` today.

The asymmetry is what settles it. Over-refusal costs a confusing refusal, and
the refusal is at compile time with the source position and the binding value
in it; a wrongly narrowed gate costs a rollout that violates a declared ceiling,
which is the failure this item exists to prevent. Narrowing `_slo_ceilings` to
the crossed routes would make the gate ADMIT compositions it refuses today,
which is not a documentation change and is not something an over-refusal
argument licenses. This is a decision, recorded here so that a later reader
does not "fix" it back; `_slo_ceilings` carries the same note in code, and
`tests/test_473_slo_rollout_gate.py` pins it with a composition whose second
file contributes a ceiling on a route no row crosses.

### The carrier and the panel

The contract is carried from the parser to the IR on `RowTable.slo`, and
`to_ir()` emits the `slo` key ONLY when a contract was declared, so the absent
key means "no contract" rather than "an empty contract". `RowTable.slo_contract()`
exposes it keyed by the datum's declared name with its line, for a consumer that
wants the source position. `revl show` prints an `SLO` section between the
composition identity and the rows, conditionally, so existing panels are
unchanged for programs that declare nothing.

## The exit test

`tests/test_473_slo_rollout_gate.py` pins the item's exit statement as a
refusal: a composition whose `emission[net(time="2s", requests=100)]` provider
declares `slo { p95_latency: 2000ms, max_pending_tasks: 100 }` is refused with
`G4`/`slo` naming the datum, the target, the ceiling and the line. It also pins
the inclusive boundary, the three ungated datums, the no-ceiling composition,
the fold path widening a ceiling, the IR unit-bearing keys, the absent key when
undeclared, the panel, and the JSON round trip. It also pins the conservative
SCOPE as intentional rather than incidental: a ceiling contributed by a file the
composition only `use`s, and a ceiling on a route no row crosses, both refuse,
and the refusal names the binding (largest) ceiling of that kind rather than
whichever one the file order reaches first.

## Left out, and why

* **The OBSERVED half in full**: the live monitor, the divert to a fallback
  provider, the safe pause, the e-stop trigger, and the SLO receipt tied to the
  running generation. The sink does not exist (stale premise 3). This is a
  runtime and an artifact design that deserves its own note; the compile-time
  gate above is the half that stands on its own.
* **`success_rate`, `recovery_time` and `approval_wait` are parsed, carried
  and printed, but never gated**, because nothing in the tree declares a value
  they could be compared against (stale premise 2). They are in the vocabulary
  because the contract is a closed set and a program that declares them should
  not be refused for it; they are in `SLO_BACKED_BY`'s complement because
  gating them would mean inventing the measurement first.
* **No `slo` trace event and no schema bump.** `why_runtime.SCHEMA_VERSION`
  moves when an SLO receipt has a shape, which waits on the observed half.
* **No runtime `--write` of anything**, and no change to any backend emitter,
  so this slice does not reach the self-host ledger.

## Relates to

* Item 260 (emission cardinality bounds): the ceilings this gate reads, and the
  model the gate copies (`opt-in`, `G4`, no cost when unused).
* Item 460 (lifecycle) and item 443 (E-Stop): the vocabulary a live breach
  would divert or pause into. Both names stay unreferenced here deliberately.
* Item 424: the `remote` row and `on_failure(withdraw|result)`, the closest
  existing "divert on failure" decision, and the reason the observed half is a
  design of its own rather than a reuse.
