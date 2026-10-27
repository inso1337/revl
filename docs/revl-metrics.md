# `revl metrics`: capability-aware runtime metrics

*Roll a recorded lifecycle run up into the three numbers a supervisor watches —
what it emitted, what failed, and how long its components lived.*

Implementation: `src/revl/metrics.py` (the pure computation, the human render,
the file loader), `src/revl/__main__.py` (`revl metrics`),
`tests/test_metrics.py`. Roadmap item 122.

---

## 1. What it is

`revl why` explains **one** component's cause chain; the OTel export
(`python -P -m revl.otel`, the `-P` is the PYTHONSAFEPATH safety bit
closing the CWD-shadowing window issue #317 names) forwards **every**
transition as a span. `revl metrics` sits above both: it reads the same
`revl run --trace run.jsonl` JSONL trace (docs/why-runtime.md) and
aggregates the whole run into three metrics.

```
$ revl metrics run.jsonl
metrics over 7 trace event(s)

emissions by capability (total 3):
  Audit   2
  Mailer  1
  by key:
    audit.write  2
    mail.send    1

failures by G-rule (total 2):
  A8            1
  unclassified  1

avg lifecycle duration: 2.000000s over 2 lifecycle(s)
  Ledger     2.500000s (x1)
  UserCache  1.500000s (x1)
```

`--json` prints the machine-readable document instead (the same shape
`compute_metrics` returns), mirroring `revl diff --json` and `python -m
revl.otel --json`. `revl metrics` is a **read-only rollup, not a gate** — it
reports a run's shape and **always exits 0**. The only nonzero exit is a trace
that cannot be read at all (missing file, malformed JSONL).

This is possible here because the trace is a *checked* record, not a guess: the
capability a crossing is scoped to, the diagnostic code a failure classified as,
and the lifecycle boundaries are all written by the runtime from the linked
dependency graph — the same reason `revl why` can attribute a cause an APM can
only infer (docs/why-runtime.md §1).

## 2. The three metrics

### 1. Emission count by capability

Counts `emit` events — one per irreversible emission crossing an
irreversible boundary at runtime — bucketed by the **`capability`** the
emission is scoped to (the target service), with a sub-breakdown by **`key`**
(the emission label `"<key>.<method>"`).

This is the runtime counterpart to `revl audit`'s *static* boundary surface:
audit enumerates which capabilities a component *may* reach; metrics counts
which it actually *did*, in this run.

```json
"emissions": {
  "total": 3,
  "by_capability": {"Audit": 2, "Mailer": 1},
  "by_key": {"audit.write": 2, "mail.send": 1}
}
```

### 2. Failure count by G-rule

Counts `withdraw` events whose observed `transition` settled a fiber into
**`FAILED`**, bucketed by the diagnostic **`cause.code`** (`diagnostics.classify`
— e.g. `G7`, `A8`, `T1`). A FAILED withdraw whose failure carried **no**
classifiable `RevlError` (a bare crash, so no `code`) buckets as
**`"unclassified"`** — never a fabricated code.

A clean withdraw (`ACTIVE -> DISPOSED`, `ACTIVE -> PENDING`) is **not** a
failure; only the `FAILED` settle counts.

```json
"failures": {"total": 2, "by_code": {"A8": 1, "unclassified": 1}}
```

### 3. Average lifecycle duration

Per component, pairs each `load` with its matching `withdraw` and averages
`withdraw.ts − load.ts`, reporting the mean and the lifecycle count (overall and
per component).

**Pairing is by `(component, gen)`.** A component loads once and withdraws once
per generation, so the `(component, gen)` key names exactly one lifecycle — a
component that is swapped across generations has each load paired with *its own*
generation's withdraw, never a cross-generation mismatch. The first `load` and
first `withdraw` seen for a key are taken (a well-formed trace carries one of
each; taking the first is robust to a duplicate).

**Unmatched events contribute no duration.** A `load` with no `withdraw` (a
component still active when the trace ends) and a `withdraw` with no `load` (an
orphan) are each not a completed lifecycle, so neither is counted.

`ts` is a `time.monotonic()` reading, meaningful only as a *difference within
one run* — never a wall-clock time (docs/why-runtime.md §2).

```json
"lifecycleDuration": {
  "count": 2,
  "avg_seconds": 2.0,
  "by_component": {
    "Ledger":    {"count": 1, "avg_seconds": 2.5},
    "UserCache": {"count": 1, "avg_seconds": 1.5}
  }
}
```

### Residue incidents (counter, issue #2730)

Counts `residue` events: one per `revl run` teardown that ended with something
not undone, bucketed by the name of the residue check that failed (`registry`,
`provisions`, `effects`, `listeners`, `inverse residue`, `models`). A teardown
can fail several checks, so `by_check` can sum to more than `total`; `total` is
the number of incidents. A clean teardown writes no event.

The source is the run driver's residue proof at teardown (`src/revl/run.py`
`_teardown`), the same checks it prints as `RESIDUE LEFT`; it records the check
names only, never their detail text, because that text can render store
contents. A `residue` event has no `component` and no `transition` (residue is a
fact about the whole run), and an older recorder wrote none, so a trace
recorded before this counter exists reports `total: 0` without that meaning the
run was clean.

```json
"residue": {"total": 1, "by_check": {"inverse residue": 1}}
```

In the OTel export the event is an error span named `residue` with the failed
check names in `revl.residue.checks`.

Not counted here: admission rejections (the gate runs before a trace exists;
tracked in issue #2730 slice 3).

## 3. Schema v1 graceful degradation

The trace schema is v3 (docs/why-runtime.md §2); the fields these metrics need
all arrived additively — `ts` on every event, `code` on a FAILED cause, and the
`emit` event kind. A v1 trace (or a v2 trace an older recorder wrote without a
field) simply lacks the input, and each metric degrades honestly:

| metric | needs | v1 behavior |
| --- | --- | --- |
| emissions | `emit` events | a v1 trace has none → `total: 0` |
| failures | `code` on the FAILED cause | no `code` → every failure buckets `"unclassified"` |
| residue | `residue` events | a trace without them reports `total: 0` (it cannot say the run was clean) |
| duration | `ts` on the paired events | completed lifecycles present but no `ts` → `{"unavailable": "ts not present (trace schema v1)"}` |

The duration degrade is **detected by data, not by the `v` field**: the metric
is unavailable when there are lifecycles to pair but the paired events carry no
`ts` — a recorder is free to stamp some events and not others, so reading `v`
alone would be unsound. When there is genuinely nothing to measure (no completed
lifecycle at all), the metric reports `count: 0`, not `unavailable`.

### Grouping by composition (attested runs)

A run traced from an admission attestation (`revl run --trace FILE
--attestation ATTESTATION.json`, issue #2730) writes a header on the trace's
first event, and `compute_metrics` copies it into an optional `run` block:

```json
"run": {"compositionHash": "<sha256>", "attestationKeyId": "<fingerprint>",
        "rulesetDigest": "<sha256>"}
```

That is what lets two runs be compared as "the same composition" or "the same
checker" without reading their first event. The block is **absent** on an
unattested trace (never a block of nulls), and a value the attestation does not
carry (an older record with no checker identity) is absent from the block.
These are labels, not proof: a consumer that needs proof verifies the
attestation (`revl attest --verify`).

## 4. The API

`src/revl/metrics.py` is pure — it imports no runtime and reads only the
`why_runtime` trace vocabulary:

- `compute_metrics(events) -> dict` — the whole computation over a list of trace
  events (unit-testable on hand-built events).
- `render(metrics) -> str` — the human table.
- `metrics_from_file(path) -> dict` — read a JSONL trace and compute its
  metrics.

The document `compute_metrics` returns has stable top-level keys `events`,
`emissions`, `failures`, `residue`, `lifecycleDuration` — exactly what `--json` prints,
plus the optional `run` block described above, present only on an attested
trace.
