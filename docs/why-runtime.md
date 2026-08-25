# Runtime "why": causal lifecycle traces, and the withdrawal oracle

*Every lifecycle transition a run makes, carrying the cause chain behind it —
and a free conformance check that the runtime did what the compiler computed.*

Implementation: `src/revl/why_runtime.py` (the vocabulary, the chain walk, the
oracle), `src/revl/run.py` (`--trace` and `--withdraw`, the recorder),
`src/revl/__main__.py` (`revl why`), `tests/test_why_runtime.py`.

---

## 1. Why this exists here and nowhere else

`revl run` already *streams* a lifecycle trace — `PgDatabase` goes
`PENDING -> LOADING -> ACTIVE`, `UserCache` follows, and on teardown they
unwind LIFO. But that stream records *events with no causes*. When a cascade
fires, nothing in it says **why**: that `UserCache` deactivated *because*
`PgDatabase` withdrew *because* the operator withdrew it.

An APM bolted onto a running system can only guess at that "because" — it
reconstructs causality from timing and hopes the correlation is real. revl
does not have to guess. The dependency graph is a *checked artifact*: G2 makes
each `(key, realm)` provision unique, so a lost provision has exactly one
supplier and no alternative; G3 makes the provider graph acyclic, so a cascade
terminates and its order is determined. The cause of every transition is
therefore *derivable*, not inferred — and this is the only layer where that is
true.

This is the runtime companion to the compile-time why-traces in
`src/revl/why.py`. Those explain a **rejection** — a search the compiler ran
and threw away (G4's fixed point, G3's cycle, G2's provider table). This
explains a **transition** — a move the runtime actually made. Same idea,
opposite end of the pipeline.

## 2. The causal trace

`revl run --trace run.jsonl` writes a JSONL trace alongside the streamed one.
Each line is one settled lifecycle transition, and it carries the cause behind
it:

```json
{"v":2,"seq":0,"gen":1,"event":"load","component":"PgDatabase",
 "transition":"PENDING -> ACTIVE","cause":{"kind":"boot"},"ts":1234.5}
{"v":2,"seq":1,"gen":1,"event":"load","component":"UserCache",
 "transition":"PENDING -> ACTIVE",
 "cause":{"kind":"requirements","providers":[{"component":"PgDatabase","key":"db"}]},"ts":1234.6}
{"v":2,"seq":2,"gen":1,"event":"withdraw","component":"UserCache",
 "transition":"ACTIVE -> PENDING",
 "cause":{"kind":"provider-withdrawn","component":"PgDatabase","key":"db"},"ts":1235.0}
{"v":2,"seq":3,"gen":1,"event":"withdraw","component":"PgDatabase",
 "transition":"ACTIVE -> DISPOSED",
 "cause":{"kind":"trigger","detail":"withdrawn by operator (revl run --withdraw PgDatabase)"},"ts":1235.1}
```

Three `event` kinds (`load`, `withdraw`, and the v2 `emit`) and four `cause`
kinds:

| cause kind | on | means |
| --- | --- | --- |
| `boot` | load | root: the composition booted; this component had no resolved injection |
| `requirements` | load | came up because its listed providers were already up (load order is providers-first) |
| `trigger` | withdraw | root: an external cause withdrew it (`detail` says which) |
| `provider-withdrawn` | withdraw | went down because the provider of the named injected `key` withdrew |

### Schema v2 (additive; a v1 event still parses and behaves identically)

`SCHEMA_VERSION` is **2**. Every field v1 defined is unchanged; v2 only *adds*.
A reader treats each new field as optional — a v1 event (no `ts`, no `code`, no
`emit` kind) is still a valid, fully-handled record, and the cause-chain walk,
`revl why`, the oracle and the OTel export all behave identically on it.

* **`ts`** (on `load`/`withdraw`/`emit`) — a monotonic-clock reading in
  fractional seconds (`time.monotonic()`), stamped when the transition is
  recorded. It is meaningful **only for durations within one run** (the
  difference between two events), never as a wall-clock time. Absent on a v1
  event → a consumer treats duration as unavailable, not zero. (This is what
  unblocks `revl metrics`, roadmap item 122.)

* **`code`** (on a `trigger` or `provider-withdrawn` cause) — the failure's
  diagnostic code (`diagnostics.classify`, e.g. `G7`, `A8`, `T1`) when the
  transition settled into **`FAILED`**. The causal *edge* is unchanged; `code`
  is extra detail on *how* it went down. A failure that carries no classifiable
  `RevlError` (a bare crash) **omits** `code` — never a fabricated one — and the
  consumer buckets it as unclassified:

  ```json
  {"v":2,"seq":4,"gen":1,"event":"withdraw","component":"Ledger",
   "transition":"ACTIVE -> FAILED",
   "cause":{"kind":"trigger","detail":"withdrawn by operator (...)","code":"A8"},"ts":1236.0}
  ```

* **`emit`** (a third `event` kind) — recorded when an emission crosses an
  irreversible boundary at runtime (the driver's `emissionsCrossed` site, a
  backwards `:back` step that steps over an uncompensated emission). One `emit`
  event per crossing. It carries **no `transition`** (nothing settled to a new
  fiber state — a one-way boundary was crossed) and instead names the
  `capability` it is scoped to (the target service) and the `key` — the emission
  label `"<key>.<method>"`:

  ```json
  {"v":2,"seq":5,"gen":1,"event":"emit","component":"Ledger",
   "capability":"Audit","key":"audit.write",
   "cause":{"kind":"trigger","detail":"crossed by step-back to 2 (an emission has no inverse)"},"ts":1236.2}
  ```

  A reader that does not model emissions (the withdrawal oracle, a v1-era
  consumer) simply ignores an `emit` event — it is neither a `load` nor a
  `withdraw`, so the causal-cascade walk skips over it and `revl why`'s walk is
  unaffected. The OTel export maps it to a plain span (unset status) carrying
  the capability/key as attributes.

Only **settled** transitions are recorded — a fiber coming to rest in `ACTIVE`,
`DISPOSED`, `PENDING`, or `FAILED`. An in-flight `LOADING`/`UNLOADING` waypoint
collapses into the single move the trace shows (`ACTIVE -> PENDING`), so the
record reads as cause-and-effect rather than as a state-machine dump.

The `transition` is what **actually happened** to the fiber, observed from the
runtime; the `cause` is read from the **linked provider graph**. That split is
what makes the oracle (§4) possible: the two are independent, so they can
disagree.

### `revl why <component> --trace run.jsonl`

Walks the cause chain for a component's transition to its root and prints it,
mirroring `why.render`'s shape:

```
$ revl why UserCache --trace run.jsonl
why UserCache was withdrawn:
  UserCache    ACTIVE -> PENDING    because injects `db`, provided by PgDatabase, which withdrew
  -> PgDatabase  ACTIVE -> DISPOSED  because withdrawn by operator (...)   (root cause)
```

The walk follows `provider-withdrawn` edges up a withdrawal cascade, or
`requirements` edges up a load, until it reaches a `boot`/`trigger` root. It is
robust to a truncated trace (a missing link is reported, not crashed) and — G3
forbids the cycle, but defensively — stops on a repeat. `--json` emits the
chain as data.

## 3. The withdrawal: `revl run --withdraw <component>`

To *record* a real withdrawal cascade, `revl run --withdraw C` is a one-shot:
boot the composition, withdraw `C` while it is live, record the cascade the
runtime actually produces, run the oracle (§4), and tear down.

The cascade is **observed, not computed**. The driver snapshots every fiber's
state, disposes `C`'s fiber, and lets the reactive graph settle. Under Cordis,
disposing `PgDatabase` *reactively* deactivates `UserCache` — it loses its
`db` provision and falls back to `PENDING`, on its own, with no orchestration
from revl. What comes down, and the order it settles in, is the runtime's
answer. The driver only *labels* each settled transition with its cause (read
from the provider graph) and writes it to the trace.

That independence is the point: if the runtime tore down the wrong set, or in
the wrong order, or failed to propagate at all, the recorded trace would say
so — and the oracle would catch it.

## 4. The oracle: prediction vs. actuality

The static `withdraw` query (`revl query withdraw C`, `query.withdrawal`) is an
**exact** prediction. Given G2 + G3 it names the precise set of components that
lose a provision when `C` is withdrawn, and the LIFO order the runtime will
tear them down in. Not a may-analysis — a proof.

The recorded trace holds the **actual** set and order. Diffing them turns every
real withdrawal into a free conformance check that the runtime did what the
compiler computed — the differential-oracle move (`docs/selfhost-findings.md`)
applied to the runtime instead of to a second parser.

`revl run --withdraw` runs the oracle automatically and prints its verdict:

```
oracle: does the runtime's withdrawal of PgDatabase match the compiler's prediction?
  predicted teardown (LIFO): UserCache -> PgDatabase
  actual   teardown (LIFO): UserCache -> PgDatabase
  CONFORMS — the runtime did exactly what the compiler computed (set and order).
```

Or run it post hoc over any recorded trace with `revl why C --trace run.jsonl
--check src.rvl` (compiles the sources for the prediction, diffs against the
trace, exits nonzero on a defect).

`conforms` is true only when the actual broken **set** and the teardown
**order** both match. Every mismatch is a `Defect` of one of three kinds:

| defect | means |
| --- | --- |
| `missing-teardown` | the prediction proves a component loses a provision, but the trace shows it did not go down — the runtime failed to propagate the withdrawal, or the prediction over-counts |
| `unexpected-teardown` | a component the prediction proves *survives* was torn down anyway — a component the compiler proved safe was killed |
| `order-mismatch` | the same set went down, but not in the LIFO order G3 makes exact |

A disagreement is **never swallowed** and never treated as noise. Because
neither side is allowed to be wrong on its own terms — the prediction is a
proof over a checked graph, the trace is a record of what a real runtime did —
a divergence is always a real defect in one of the two. That is the whole
force of a differential oracle: it needs no third oracle to arbitrate, because
agreement is the only outcome consistent with both being correct.

## 5. What it does not promise

* **The oracle checks the withdrawal cascade, not the whole run.** It answers
  one question — did tearing down `C` break exactly what the compiler said, in
  the order it said. It does not observe application state, effect inversion
  (that is backwards replay, `docs/replay.md`), or host-side correctness.
* **A conforming run is not a proof of the runtime.** It is a proof for *this*
  withdrawal on *this* composition — a differential oracle only covers what its
  input exercises (`docs/selfhost-findings.md`). A cascade never triggered is
  never checked.
* **`--withdraw` is a one-shot, not a live command.** It boots, withdraws once,
  and tears down. It is a conformance harness, not the interactive REPL.
* **The recorded `cause` is a graph fact, the `transition` is an observation.**
  The oracle's value comes entirely from keeping those two independent; a run
  that computed the "actual" cascade from the same graph as the prediction
  would agree trivially and check nothing.

## 6. Status

Landed for the `py` tier, where `revl run` has a runtime. The trace vocabulary
and the oracle are pure (`why_runtime.py` imports no runtime), so `revl why`
and `--check` read a trace and diff a prediction on any interpreter; only the
recording end (`revl run --withdraw`) needs cordis.
