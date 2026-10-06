/-!
# GRetain — the `G-RETAIN` differential-oracle row (issue #1811, group 3)

The rule: a `Retained[T, P]` value may not reach a persistence sink after `P`'s
retention deadline. The checker states it in `src/revl/retention.py`
(`RetentionPolicy.expired`) and raises it in `src/revl/taint.py`
(`_refuse_retention`), and the rejection document that exercises it
(`examples/rejections/gretain_expired_at_persistence_sink.rvl`) was checked by
the shipped checker and then filed under the deliberately UNRATCHETED
`out-of-fragment` bucket, because nothing in `formal/` decided it.

## The row, and the route it takes

Issue #1811 authorizes two routes. Route A grows the L0 bodies so the checker's
COVERAGE of the walk is itself proved (roadmap item 418, step 9). Route B — the
sanctioned fallback, and the one taken here, as for `RevL.G9Flow` — exports the
facts the checker REPORTS in its own refusal and decides the rule on them.

The exporter (`formal/harness/diff_corpus.py`, `gretain_rows`) runs the real
checker and reads five facts out of the refusal it raises:

* the sink the value reached — `db_put`, named in the message and in the
  `The retaining path is …` chain;
* the persistence scope that made it a sink — `a `db` crossing`, the checker's
  own reading of the crossing's declared capability;
* the policy that was exceeded — `customer_pii`;
* **the two instants**, which is what makes this rule awkward: the policy's
  `until` is read out of the hint (`may be kept until <instant> in residence`)
  and the evaluation instant `now` out of the message (`deadline passed at
  <instant>`). Both are the checker's, printed by the checker, from the one
  instant `src/revl/retention.py` compared against. The harness PINS that
  instant (`REVL_RETENTION_AS_OF`), so the verdict is reproducible.

## The rule takes `now` as an explicit input, and that is the whole point

A deadline is not a property of the program; it is a relation between the
program and a moment. So `holdsB` below takes `until` and `now` as explicit
arguments and nothing in this module reads a clock. The row is only reproducible
because the checker's instant is pinned and then EXPORTED — the harness neither
assumes an instant nor computes one.

`Instant` is whole seconds since the Unix epoch, and the exporter converts the
ISO-8601 instant the checker printed. `src/revl/retention.py:parse_instant`
normalizes every deadline to UTC (`astimezone(timezone.utc)`), so an integer is
a faithful encoding of the checker's own instant — and it is the encoding that
makes the row's examples `decide`-checkable, where parsing ISO text in the
kernel would not be (`String.splitOn` does not reduce; see
`RevL.Lemmas.TaintLemmas`).

## What this row is NOT: the rule on the corpus, not coverage of the walk

This is the distinction issue #1811 requires be stated, and it is the whole
reason the row is a fallback rather than the fix.

**What the row decides.** The rule, at the sink and the scope the checker
discovered, at the two instants the checker compared. If the checker's own
comparison is what the rule is about, then this row agrees with it by
construction: the row's premises ARE the checker's report.

**What the row does not witness.** That the checker's WALK is complete. A
checker that followed no hop, or that missed a sink, or that never reached the
sink the rule names, exports a short row or no row at all — and this row cannot
tell the difference between "the deadline has not passed here" and "the checker
looked here and found nothing". `reaches` states the positive half exactly and
no more: the naming chain the checker reported ends at the sink it reported, so
the row's `path` column is load-bearing rather than decorative. Nothing here
says the reported chain is the real one, and nothing here says the checker
considered every sink.

**Why the row is still not vacuous.** An absent row is a FAILURE, not a pass.
`sinkOfScope` is `none` for a persistence scope this module does not name, and
the exporter emits no row for such a refusal; `diff_corpus.py` files every
modelled retention refusal with no row under `missed-G-RETAIN`, which is in
`FATAL_BUCKETS`. So a checker that stops reporting its discovered sink, its
scope or its instants turns this row RED rather than silently green — which is
exactly the direction route A would make unnecessary and route B cannot.

**The non-vacuity flip is a `now`-crossing flip.** The manager's evidence bar
for this row asks that the verdict FLIP when an input moves, executed by the
gate. `formal/harness/diff_corpus.py`'s `gretain_coverage` re-decides the
corpus row's own four columns at three instants — one before the deadline, the
deadline itself, and the instant the checker reported — and requires the row to
admit, admit and refuse respectively. A row that ignored `now` would print the
same verdict three times and fail the gate. The `until` column is the same in
all three, so the flip is the instant's and not the deadline's.

## What is reused, and what is not

`RevL.Lemmas`' taint vocabulary is NOT reused, because a retention refusal is
not a taint-label refusal: `G-RETAIN` carries no label, and its message names a
policy, a scope and two instants where a `G9` message names a label. What IS
shared is the discipline: the checker's own human phrases are the row's input,
this module's tables are stated HERE rather than read from `src/revl/`, and a
phrase this module does not carry yields `none` and therefore no row.

This module lives in `RevL.Theorems` (L2) and imports nothing but the kernel, so
it does not collide with `RevL.G9Flow` (also L2); the import layering
(`formal/scripts/layering_gate.py`) forbids one L2 file importing another.
-/

namespace RevL.GRetain

/-! ## The rule, decided

The rule is stated over ONE row: a value that has reached a persistence sink,
under a policy with a deadline, at an instant. There is no walk to carry,
because the refusal this row reads is the flow-level one — the checker reports
the chain that reached the sink, and `reaches` below is the only thing the row
says about it.
-/

/-- The persistence scope head of a crossing, as the checker's message spells
it (`a `db` crossing`). This is `src/revl/retention.py`'s
`PERSISTENCE_SINK_SCOPES`, restated HERE rather than imported: a scope this
module does not name yields `none`, the exporter emits no row, and the harness
reports a `missed-G-RETAIN` — a fatal bucket — rather than an agreement the row
cannot make. Widening the checker's scope set therefore turns the row red, not
green.

The ten are exactly the checker's: a relational or document store, a filesystem
write, a generic durable store, a key/value store, object/blob storage, cold
storage, a search index, a durable cache, a durable queue, and a write-ahead
log. `fs` is a persistence sink here and a provenance SOURCE in the taint
module; the two dimensions are orthogonal and a filesystem crossing is honestly
both. -/
inductive PersistenceSink where
  | db | fs | store | kv | blob | archive | index | cache | queue | wal
  deriving DecidableEq, Repr

/-- The scope head a checker-reported crossing declares, or `none` for a scope
the row does not model. -/
def sinkOfScope : String → Option PersistenceSink
  | "db" => some .db
  | "fs" => some .fs
  | "store" => some .store
  | "kv" => some .kv
  | "blob" => some .blob
  | "archive" => some .archive
  | "index" => some .index
  | "cache" => some .cache
  | "queue" => some .queue
  | "wal" => some .wal
  | _ => none

/-- An instant, as whole seconds since the Unix epoch. The checker normalizes
every deadline to UTC before comparing (`src/revl/retention.py:parse_instant`),
so an integer is a faithful encoding of the instant it compared — and one the
kernel can reduce, which ISO text is not. -/
abbrev Instant := Nat

/-- The rule at one persistence sink, decided. `until` is the policy's deadline
and `now` the evaluation instant; both are the checker's.

`held` is the policy's legal hold, and it is FIRST for a reason: the checker's
`RetentionPolicy.expired` returns `False` for a held policy BEFORE it consults
the deadline ("the legal hold is applied HERE rather than at the call sites, so
no caller can consult the deadline while forgetting the exception"). So the
hold is not an alternative reading of the rule — it is a step of the rule, and
`hold_clears_the_deadline` below states it.

`now ≤ deadline` is the rule's own polarity: `expired` is the STRICT
`as_of > self.until`, so an instant exactly at the deadline is NOT expired.
That edge is `corpus_row_is_admitted_at_the_deadline`, and getting it backwards
would refuse a value on the last moment it was lawful to keep. -/
def holdsB (held : Bool) (deadline now : Instant) : Bool :=
  held || decide (now ≤ deadline)

/-- The rule as a proposition, and `holdsB` decides it. This is the bridge the
oracle's `#print axioms` gate rests on: the decider the harness prints is this
one, and it is pinned to the rule rather than to a chosen computation. -/
theorem holdsB_iff (held : Bool) (deadline now : Instant) :
    holdsB held deadline now = true ↔ held = true ∨ now ≤ deadline := by
  unfold holdsB
  cases held <;> simp

/-- A legal hold clears the deadline at EVERY instant: `held = true` makes
`holdsB` true whatever the deadline and `now` are. The checker's hint names this
as one of the two escapes ("or declare a `hold` on the policy, which overrides
the deadline"), so the row's escape is the checker's own. -/
theorem hold_clears_the_deadline (deadline now : Instant) :
    holdsB true deadline now = true := by
  simp [holdsB]

/-! ## The walk, as the row's premise

The refusal this row reads carries `The retaining path is load() -> db_put`: the
naming chain the checker walked to the sink. The row's `path` column is that
chain and the row's `sink` column is the sink it named, and `reaches` is the
claim that the two agree. It is the G-RETAIN analogue of `RevL.G9Flow`'s
`reported_walk_reaches_the_reported_label`, and it says exactly as much: the
reported chain ends at the reported sink, and nothing about whether the chain is
complete.
-/

/-- The separator `src/revl/taint.py` joins the chain's steps with
(`" -> ".join(...)`). -/
abbrev chainSep : String := " -> "

/-- Whether the naming chain the checker reported ends at the sink it named.
The chain is `" -> "`-joined, so the claim is that the chain ENDS WITH the
separator followed by the sink's name — which is stricter than "contains the
sink's name", and so refuses `load() -> xdb_put` for the sink `db_put`. The
single-step chain `db_put` is the one case with no separator, and it is admitted
by the first disjunct.

`List.isSuffixOf` and not `String.splitOn`: `splitOn` does not reduce in the
kernel (see `RevL.Lemmas.TaintLemmas`), so a `splitOn`-based `reaches` could
not be `decide`-checked and the row's own examples would not be computed. -/
def reaches (path sink : String) : Bool :=
  decide (path = sink) || (chainSep ++ sink).toList.isSuffixOf path.toList

/-- `reaches` as a proposition. -/
theorem reaches_iff (path sink : String) :
    reaches path sink = true ↔
      path = sink
        ∨ (chainSep ++ sink).toList.isSuffixOf path.toList = true := by
  unfold reaches
  rw [Bool.or_eq_true, decide_eq_true_iff]

/-! ## The row

`rowB` is the verdict the oracle prints for one exported row. `held` is fixed
to `false` because a HELD policy never produces the refusal this row reads: the
checker applies the hold before the deadline, so the refusal the exporter sees
is only ever raised at `held = false`. `hold_clears_the_deadline` states the
other branch, so the row's `false` is a derivation from the checker rather than
an assumption about it.
-/

/-- The row's verdict on one exported row: does the rule HOLD here? A scope the
row does not model has no sink and therefore no verdict at all — `false`, so the
row can never agree with a refusal it did not understand. -/
def rowB (scope sink path : String) (deadline now : Instant) : Bool :=
  match sinkOfScope scope with
  | none => false
  | some _ => reaches path sink && holdsB false deadline now

/-- `rowB` decides the rule on the exported columns. The RHS is the rule: a
modelled persistence scope, a walk that reaches the sink, and a deadline that
has NOT passed — `now ≤ deadline` at `held = false` is exactly the negation of
the checker's strict `as_of > self.until`. -/
theorem rowB_iff (scope sink path : String) (deadline now : Instant) :
    rowB scope sink path deadline now = true ↔
      (∃ k, sinkOfScope scope = some k)
        ∧ reaches path sink = true
        ∧ now ≤ deadline := by
  unfold rowB
  cases h : sinkOfScope scope with
  | none => simp
  | some k => simp [holdsB]

/-! ### The corpus shape

`examples/rejections/gretain_expired_at_persistence_sink.rvl`, as the exporter
reads it: the sink and scope the checker reports, the chain it walked, and the
two instants it compared — `until` from the policy's own declaration and `now`
from the instant the harness pinned. Every one of these is the checker's; the
module chooses none of them.

`corpusUntil` is the document's `until: "2020-01-01T00:00:00Z"`, and
`corpusNow` is `2026-01-01T00:00:00Z`, the instant the harness pins so the
refusal is reproducible. `corpusBefore` is a year before the deadline, and the
third instant the gate re-decides at is `corpusUntil` itself.
-/

/-- `2020-01-01T00:00:00Z`, the document's `until`, in whole seconds. -/
def corpusUntil : Instant := 1577836800

/-- `2026-01-01T00:00:00Z`, the instant the harness pins. -/
def corpusNow : Instant := 1767225600

/-- `2019-01-01T00:00:00Z`, a year before the deadline. -/
def corpusBefore : Instant := 1546300800

/-- The document's sink name, as the checker's message and chain spell it. -/
def corpusSink : String := "db_put"

/-- The document's persistence scope, as the checker's message spells it. -/
def corpusScope : String := "db"

/-- The naming chain the checker reported, verbatim from its refusal. -/
def corpusPath : String := "load() -> db_put"

/-- The corpus scope is in the table, so the exporter emits a row for it. -/
theorem corpus_sink_scope_is_modelled :
    sinkOfScope corpusScope = some .db := rfl

/-- The corpus chain ends at the corpus sink, so the row's walk premise holds. -/
theorem corpus_walk_reaches_the_corpus_sink :
    reaches corpusPath corpusSink = true := by decide

/-- A scope outside the checker's ten has NO sink, so the exporter emits no row
and the harness files the refusal under the fatal `missed-G-RETAIN`. This is the
direction that keeps the row from passing by looking away. -/
theorem unmodelled_sink_scope_has_no_sink :
    sinkOfScope "a shell command" = none := rfl

/-- The walk premise is not vacuous: the same chain does NOT reach a sink it did
not name, so the `path` column can move the verdict. `load() -> db_put` ends at
`db_put` and not at `fs_put`. -/
theorem corpus_walk_does_not_reach_another_sink :
    reaches corpusPath "fs_put" = false := by decide

/-- **The rule holds past the deadline**: at the instant the checker reported,
the corpus row is refused. This is the row's `fail`, and it is what the harness
compares the checker's refusal against. -/
theorem corpus_row_is_refused :
    rowB corpusScope corpusSink corpusPath corpusUntil corpusNow = false := by decide

/-- **The rule does not hold before the deadline**: the SAME row at an instant a
year before `until` is admitted. Nothing but `now` moved, so the verdict is the
instant's. -/
theorem corpus_row_is_admitted_before_the_deadline :
    rowB corpusScope corpusSink corpusPath corpusUntil corpusBefore = true := by decide

/-- **The rule does not hold AT the deadline.** The checker's `expired` is the
strict `as_of > self.until`, so the last lawful moment is still lawful. This is
the boundary the row must not get wrong, and it is the third instant
`gretain_coverage` re-decides at. -/
theorem corpus_row_is_admitted_at_the_deadline :
    rowB corpusScope corpusSink corpusPath corpusUntil corpusUntil = true := by decide

/-- The corpus row at the three instants the gate re-decides at, and at the
corpus's own two: refused at `now`, admitted a year before, admitted AT the
deadline. The `until` column is the same in all three. -/
theorem corpus_row_decided :
    rowB corpusScope corpusSink corpusPath corpusUntil corpusNow = false
      ∧ rowB corpusScope corpusSink corpusPath corpusUntil corpusBefore = true
      ∧ rowB corpusScope corpusSink corpusPath corpusUntil corpusUntil = true := by
  refine ⟨?_, ?_, ?_⟩ <;> decide

/-- The corpus scope's ten siblings all name a sink, so `sinkOfScope` is not
"`db` and nothing else" — the table is the checker's whole
`PERSISTENCE_SINK_SCOPES`. -/
theorem persistence_sink_scopes_are_modelled :
    sinkOfScope "db" = some .db
      ∧ sinkOfScope "fs" = some .fs
      ∧ sinkOfScope "store" = some .store
      ∧ sinkOfScope "kv" = some .kv
      ∧ sinkOfScope "blob" = some .blob
      ∧ sinkOfScope "archive" = some .archive
      ∧ sinkOfScope "index" = some .index
      ∧ sinkOfScope "cache" = some .cache
      ∧ sinkOfScope "queue" = some .queue
      ∧ sinkOfScope "wal" = some .wal := by
  refine ⟨?_, ?_, ?_, ?_, ?_, ?_, ?_, ?_, ?_, ?_⟩ <;> rfl

/-- **Non-vacuity, the `now`-crossing flip.** The corpus row's own columns,
decided at four instants: refused past the deadline, and admitted at the
deadline, before it, and under a declared hold. The `until` column is the same
throughout, so a row that dropped `now` — or that refused everything, or
nothing — fails here. This is the theorem `gretain_coverage`'s ratchet mirrors,
and the one the harness's three-instant re-decision executes. -/
theorem retain_not_vacuous :
    rowB corpusScope corpusSink corpusPath corpusUntil corpusNow = false
      ∧ rowB corpusScope corpusSink corpusPath corpusUntil corpusUntil = true
      ∧ rowB corpusScope corpusSink corpusPath corpusUntil corpusBefore = true
      ∧ holdsB true corpusUntil corpusNow = true
      ∧ rowB corpusScope corpusSink corpusPath corpusUntil
          (corpusUntil + 1) = false := by
  refine ⟨?_, ?_, ?_, ?_, ?_⟩ <;> decide

end RevL.GRetain
