import RevL.Lemmas.TaintLemmas

/-!
# G9Flow — the `G9`/`G-SECRET-FLOW` differential-oracle row (issue #1811, group 2)

`RevL.G9` states the secret-flow rule over an ABSTRACT walk (`Flow`) and
`RevL.Lemmas.Admits` states what each sink admits. Neither was reachable from
`formal/harness/Oracle.lean`, so no differential-oracle row decided them, and
the four rejection documents that exercise the rule
(`examples/rejections/g9_closure_capture_launders_taint.rvl`,
`g9_service_return_launders_taint.rvl`,
`g9_spawn_config_launders_taint.rvl`,
`gsecret_service_return_discloses.rvl`) were checked by the shipped checker and
then filed under the deliberately UNRATCHETED `out-of-fragment` bucket. `RevL.G9`'s
own module note says as much: "No differential-oracle row references these
definitions".

## The row, and the route it takes

Issue #1811 authorizes two routes. Route A grows the L0 bodies so the checker's
COVERAGE of the walk is itself proved (roadmap item 418, step 9). Route B — the
sanctioned fallback, and the one taken here — exports the walk the checker
REPORTS in its own refusal and decides `Admits` on it.

The exporter (`formal/harness/diff_corpus.py`, `g9_rows`) runs the real checker
and reads four facts out of the refusal it raises:

* the sink it reached — `navigate.refused.sink`, or the sink named in the
  message itself for the `G-SECRET-FLOW` refusal, which carries no `navigate`;
* the origins that arrived there — `navigate.refused.origins`, or the rule's own
  `confidential` origin for `G-SECRET-FLOW` (`_refuse_confidential` fires on a
  value whose label carries it, and `SECRET_ORIGIN`/`CONFIDENTIAL_ORIGIN` are
  disjoint, so the label is not a guess);
* the naming chain it walked — the `The tainting path is …` / `The disclosing
  path is …` line of the `hint`;
* the ONE declassification the checker itself names as the escape —
  `navigate.alternatives`' `endorse[<origin>]` ref, or the `endorse[confidential]`
  the `G-SECRET-FLOW` hint names.

`sinkOfKind` and `originOf` below are this module's own tables for the two
strings, and `g9RowB` decides `Admits` on what they yield. `g9RowB_iff` pins the
decision procedure to `RevL.Lemmas.Admits`, which is the theorem the oracle's
`#print axioms` gate checks.

## What this row is NOT: the rule on the corpus, not coverage of the walk

This is the distinction issue #1811 requires be stated, and it is the whole
reason the row is a fallback rather than the fix.

**What the row decides.** The rule, at the sink the checker discovered, on the
label the checker discovered. If the checker's own walk is what the rule is
about, then this row agrees with it by construction: the row's premises ARE the
checker's report.

**What the row does not witness.** That the checker's walk is COMPLETE. A
checker that followed no hop, or that missed a sink, or that never reached the
sink the rule names, exports a short row or no row at all — and this row cannot
tell the difference between "the rule holds here" and "the checker looked here
and found nothing". `reported_walk_reaches_the_reported_label` states the
positive half exactly and no more: every reported walk reaches the label it
reported, at ANY length, so the hop count cannot move the verdict. Nothing here
says the reported length is the real one.

**Why the row is still not vacuous.** An absent row is a FAILURE, not a pass.
`sinkOfKind` is `none` for a sink kind this module does not name, and the
exporter emits no row for such a refusal; `diff_corpus.py` files every modelled
taint refusal with no row under `missed-G9`, which is in `FATAL_BUCKETS`. So a
checker that stops reporting its discovered sink turns this row RED rather than
silently green — which is exactly the
direction route A would make unnecessary and route B cannot.

## What is reused, and what is not

Nothing in `RevL.G9` or `RevL.Lemmas.TaintLemmas` is restated: `Step`, `Flow`,
`Sink`, `Admits`, `Origin`, `Label`, `applyD` and `Declassifier` are imported
from the L1 farm. The label algebra, the sink rules and the walk live there;
this module adds only the two string tables the harness needs and the decider
the oracle prints.

The walk is imported from L1 rather than from `RevL.G9` because the import
layering (`formal/scripts/layering_gate.py`) forbids one L2 file importing
another: `RevL.G9` and this row are two L2 files over the same walk, so the
walk sits below both, exactly as `RevL.Lemmas.ClassLemmas` does for
`RevL.Theorems.G{4,5,8}_Classified*`. The definitions are the ones `RevL.G9`
is proved against, not copies.
-/

namespace RevL.G9Flow

open RevL.Lemmas

/-! ## The row's vocabulary

`navigate.refused.kind` is the checker's own human phrase, and the exporter
spells a label as comma-joined origin names. Both tables are stated HERE rather
than read from `src/revl/taint.py`: a kind or an origin name this module does
not carry yields `none`, the exporter emits no row, and the harness reports a
`missed-G9` — a fatal bucket — rather than an agreement the row cannot make.
Widening the checker therefore turns the row red, not green.
-/

/-- The sink CLASS a checker-reported sink kind belongs to. `Admits` is stated
per `Sink`, and the taint refusals distinguish two classes of crossing: the
AUTHORITY sinks (a shell command, a policy update, a UI actuation, a capability
name, and a declared `Trusted[T]` parameter — `_sink_kind_for`'s whole table)
and the DISCLOSURE sinks (a plain extern host call, an emission crossing).
`none` for a kind the row does not model. -/
def sinkOfKind : String → Option Sink
  | "a shell command" => some .authority
  | "a policy update" => some .authority
  | "a UI actuation" => some .authority
  | "a capability name" => some .authority
  | "an extern host call (a disclosure sink)" => some .disclosure
  | "an emission crossing" => some .disclosure
  | _ => none

/-- The `Sink` a class NAME denotes — the string the exporter puts in a row's
class column, and the inverse of `sinkOfKind` on the classes it carries. -/
def sinkOfClass : String → Option Sink
  | "authority" => some .authority
  | "disclosure" => some .disclosure
  | "secretReceiver" => some .secretReceiver
  | "unnameable" => some .unnameable
  | _ => none

/-- The `RevL.Lemmas.Origin` an origin name denotes. The authority origins are
the ones `_authority_dirty` is about; `secret` and `confidential` are the
section-7 confidentiality origins, kept disjoint on purpose
(`SECRET_ORIGIN`/`CONFIDENTIAL_ORIGIN`), so a `Secret[T]` value's label is
`confidential` alone and a bound provider key's is `secret` alone.

This is `RevL.Lemmas.originOfHead`, reused rather than restated: the closed
class table the origin derivation already carries. -/
def originOf (name : String) : Option Origin := originOfHead name

/-- A comma-joined origin list, read as a label. `none` when any name is one
this module does not carry, so a partially-understood label is not silently
truncated into a smaller — and more admissible — one. -/
def labelOf (names : List String) : Option Label :=
  names.mapM originOf

/-- Split a comma-joined origin list. Structural on the character list, so it
reduces definitionally and the row's own examples are `rfl`-checked. -/
def splitComma (s : String) : List String :=
  let rec go : List Char → List Char → List String
    | [], cur => [String.ofList cur.reverse]
    | c :: rest, cur =>
        if c = ',' then String.ofList cur.reverse :: go rest []
        else go rest (c :: cur)
  go s.toList []

/-- The origins column of a row, read as a label: the empty string is the empty
label, and a name this module does not carry yields `none` rather than a
truncated label. -/
def labelOfString (s : String) : Option Label :=
  if s = "" then some [] else labelOf (splitComma s)

/-! ## The deciders -/

/-- `Admits`, decided. `Clean ℓ` is literal emptiness of the label, not "no
untrusted origin in it": `authority_refuses_dirty` refuses a label carrying ANY
origin, `secret` included. -/
def admitsB (k : Sink) (ℓ : Label) : Bool :=
  match k with
  | .authority => ℓ.isEmpty
  | .disclosure => decide (Origin.secret ∉ ℓ ∧ Origin.confidential ∉ ℓ)
  | .secretReceiver => decide (Origin.secret ∉ ℓ)
  | .unnameable => ℓ.isEmpty

/-- `admitsB` decides `Admits`, sink by sink and label by label. -/
theorem admitsB_iff (k : Sink) (ℓ : Label) : admitsB k ℓ = true ↔ Admits k ℓ := by
  cases k <;>
    simp [admitsB, Admits, Clean, List.isEmpty_iff,
      List.eq_nil_iff_forall_not_mem]

/-- The row's verdict on one exported row: the sink class the checker
discovered, and the label that arrived there. `true` means the rule HOLDS at
that sink — the sink admits the label — which the oracle prints as `ok`, the
same polarity as every other row. A `false` is the rule VIOLATED at a sink the
checker itself reported, and the oracle prints `fail`; that is what explains a
refusal rather than contradicting it. -/
def g9RowB (k : Sink) (ℓ : Label) : Bool := admitsB k ℓ

/-- `g9RowB` decides `RevL.Lemmas.Admits`: the row says `ok` exactly where the
sink admits the label. -/
theorem g9RowB_iff (k : Sink) (ℓ : Label) :
    g9RowB k ℓ = true ↔ Admits k ℓ :=
  admitsB_iff k ℓ

/-! ## The walk the row is stated over

Route B exports the walk the checker reported. `stepsOf` is the abstract walk
carrying exactly that label to the sink: the label's origins enter as declared
sources (`Step.source`, the crossing that minted each), then the reported number
of naming-chain hops propagate it. `escapeOf` appends the ONE declassification
the checker's own refusal names as the escape.

`hops` is the length of the checker's naming chain minus one — the exporter
computes it from the `path` it reports, so the row's own output carries both.
-/

/-- The abstract walk the row is stated over: `ℓ`'s origins enter as declared
sources, then `hops` propagation steps carry them to the sink. Reversed before
mapping because `Step.source` PREPENDS its origin to the incoming label, so a
forward mapping would arrive with the label reversed. -/
def stepsOf (ℓ : Label) (hops : Nat) : List Step :=
  ℓ.reverse.map Step.source ++ List.replicate hops Step.propagate

/-- The walk plus the one declassification the checker's refusal names. -/
def escapeOf (ℓ : Label) (hops : Nat) (d : Declassifier) : List Step :=
  stepsOf ℓ hops ++ [Step.declassify d]

/-- A `Flow` may be split at any point: the steps before the split reach an
intermediate label, the steps after reach the final one. -/
theorem flow_append {P : Profile} {G : Grants} {ℓin : Label} {a : List Step}
    {mid : Label} {b : List Step} {out : Label}
    (ha : Flow P G ℓin a mid) (hb : Flow P G mid b out) :
    Flow P G ℓin (a ++ b) out := by
  induction ha with
  | nil => exact hb
  | source o ℓ st out' _ ih => exact Flow.source o ℓ (st ++ b) out (ih hb)
  | join m ℓ st out' _ ih => exact Flow.join m ℓ (st ++ b) out (ih hb)
  | propagate ℓ st out' _ ih => exact Flow.propagate ℓ (st ++ b) out (ih hb)
  | declassify d ℓ st out' hok _ ih =>
      exact Flow.declassify d ℓ (st ++ b) out hok (ih hb)

/-- The declared sources alone carry `ℓ` from `ℓin` to `ℓ.reverse ++ ℓin`. -/
theorem flow_sources (P : Profile) (G : Grants) (ℓ : Label) (ℓin : Label) :
    Flow P G ℓin (ℓ.map Step.source) (ℓ.reverse ++ ℓin) := by
  induction ℓ generalizing ℓin with
  | nil => exact Flow.nil ℓin
  | cons o rest ih =>
      refine Flow.source o ℓin (rest.map Step.source) ((o :: rest).reverse ++ ℓin) ?_
      simpa [List.reverse_cons, List.append_assoc] using ih (o :: ℓin)

/-- Propagation steps carry a label through unchanged, at any length. -/
theorem flow_propagate (P : Profile) (G : Grants) (ℓ : Label) (n : Nat) :
    Flow P G ℓ (List.replicate n Step.propagate) ℓ := by
  induction n with
  | zero => exact Flow.nil ℓ
  | succ n ih => exact Flow.propagate ℓ (List.replicate n Step.propagate) ℓ ih

/-- Every reported walk reaches the label it reported, at ANY reported length.
This is the formal half of the route-B distinction: the row decides the label
the checker discovered, and the hop count cannot move that verdict. It is NOT a
statement that the reported length is the real one — see the module note. -/
theorem reported_walk_reaches_the_reported_label (P : Profile) (G : Grants)
    (ℓ : Label) (hops : Nat) : Flow P G [] (stepsOf ℓ hops) ℓ := by
  refine flow_append ?_ (flow_propagate P G ℓ hops)
  simpa [List.reverse_reverse] using flow_sources P G ℓ.reverse []

/-- The walk that ends at the declassification the checker named reaches the
label that declassifier leaves behind (`applyD`), not the label it entered
with. -/
theorem escapeFlow (P : Profile) (G : Grants) (ℓ : Label) (hops : Nat)
    (d : Declassifier) (hok : DeclassOK P G d ℓ) :
    Flow P G [] (escapeOf ℓ hops d) (applyD d ℓ) := by
  unfold escapeOf
  exact flow_append (reported_walk_reaches_the_reported_label P G ℓ hops)
    (Flow.declassify d ℓ [] (applyD d ℓ) hok (Flow.nil (applyD d ℓ)))

/-! ## Non-vacuity

`g9RowB` is a decision procedure, so it is `false` somewhere and `true` somewhere
else by construction; what has to be shown is that BOTH polarities are reachable
through the rule's own vocabulary, that the escape actually moves a label, and
that the two sink classes the corpus exercises are the two classes the table
carries. All of these are computations on concrete data.
-/

/-- An authority sink refuses a label carrying an untrusted origin — the
`fs`-at-`run` shape of three of the four corpus documents. The row prints
`fail` here. -/
theorem g9RowB_refuses_an_authority_sink :
    g9RowB Sink.authority [Origin.fs] = false := by decide

/-- An authority sink admits the empty label, so the refusal above is not
universal. -/
theorem g9RowB_admits_a_clean_label : g9RowB Sink.authority [] = true := by decide

/-- An authority sink refuses a `secret`-labelled value too: `Clean` is literal
emptiness, not "no untrusted origin", which is `authority_refuses_dirty`. -/
theorem g9RowB_refuses_a_secret_at_authority :
    g9RowB Sink.authority [Origin.secret] = false := by decide

/-- A disclosure sink refuses the `confidential` label the `G-SECRET-FLOW`
document's value carries — the fourth corpus document. The row prints `fail`
here. -/
theorem g9RowB_refuses_a_disclosure_sink :
    g9RowB Sink.disclosure [Origin.confidential] = false := by decide

/-- A disclosure sink refuses a `secret`-labelled value as well: §7 refuses
BOTH confidentiality origins at a disclosure sink, so the two are not
interchangeable. -/
theorem g9RowB_refuses_a_secret_at_a_disclosure_sink :
    g9RowB Sink.disclosure [Origin.secret] = false := by decide

/-- A disclosure sink admits the empty label, so neither refusal above is
universal. -/
theorem g9RowB_admits_a_clean_value_at_a_disclosure_sink :
    g9RowB Sink.disclosure [] = true := by decide

/-- A declared `Secret[T]` receiver admits a `confidential` value (§7b): the
receiver is the declared point the disclosure rule routes to, so the refusal at
a PLAIN extern call is a fact about the SINK, not about the value. -/
theorem g9RowB_admits_a_confidential_value_at_a_secret_receiver :
    g9RowB Sink.secretReceiver [Origin.confidential] = true := by decide

/-- …but that receiver still refuses a `secret`-labelled value: a bound
provider key has no `Secret[T]` edge either (item 256 §4a.3). -/
theorem g9RowB_refuses_a_secret_at_a_secret_receiver :
    g9RowB Sink.secretReceiver [Origin.secret] = false := by decide

/-- The escape the checker names actually clears the label: `endorse[fs]` on an
`fs` label leaves `[]`, which an authority sink admits. Without this the row's
"route it through a declared point" advice would be untested. -/
theorem g9RowB_admits_after_declassification :
    g9RowB Sink.authority
      (applyD ⟨DeclassKind.endorse Origin.fs, true⟩ [Origin.fs]) = true := by decide

/-- The escape is SCOPED: `endorse[fs]` does not clear a `web` origin, so a
declassifier is not a blanket sanitizer. -/
theorem g9RowB_refuses_after_a_misscoped_declassification :
    g9RowB Sink.authority
      (applyD ⟨DeclassKind.endorse Origin.fs, true⟩ [Origin.web]) = false := by decide

/-- The two sink kinds the corpus actually refuses are both in the table, and
they map to the two classes `Admits` states rules for. -/
theorem corpus_sink_kinds_are_modelled :
    sinkOfKind "a shell command" = some Sink.authority
      ∧ sinkOfKind "an extern host call (a disclosure sink)"
          = some Sink.disclosure := by
  constructor <;> rfl

/-- The class NAME a row carries round-trips to the same `Sink`, so the
exporter's class column and this module's table cannot drift apart. -/
theorem class_names_round_trip :
    sinkOfClass "authority" = some Sink.authority
      ∧ sinkOfClass "disclosure" = some Sink.disclosure := by
  constructor <;> rfl

/-- A sink kind this module does not carry has NO class, so the exporter emits
no row and the harness files the refusal under `missed-G9` — a fatal bucket.
This is the direction that keeps the row from passing by looking away. -/
theorem unmodelled_sink_kind_has_no_class :
    sinkOfKind "a mystery sink" = none := rfl

/-- The corpus's four labels are all in the origin table. -/
theorem corpus_labels_are_modelled :
    labelOf ["fs"] = some [Origin.fs]
      ∧ labelOf ["confidential"] = some [Origin.confidential] := by
  constructor <;> rfl

/-- A label naming an origin this module does not carry is `none` rather than a
truncated — and more admissible — one. -/
theorem unmodelled_origin_has_no_label :
    labelOf ["fs", "mystery"] = none := rfl

/-- The origins COLUMN of a row, read as a label. This is what the oracle's
decider consumes, so it is the column whose spelling matters: the empty string
is the empty label, the corpus's two labels are the origins their names denote,
and an unknown name is `none` — which emits no row at all and lands the refusal
in the fatal `missed-G9` rather than in an agreement. -/
theorem labelOfString_is_the_label :
    labelOfString "" = some []
      ∧ labelOfString "fs" = some [Origin.fs]
      ∧ labelOfString "confidential" = some [Origin.confidential]
      ∧ labelOfString "fs,mystery" = none := by
  refine ⟨?_, ?_, ?_, ?_⟩ <;> decide

/-! ### The corpus shapes

The four documents this row is for, as the exporter reads them: the sink kind
the checker reports and the origins that arrived there. The admitted twins are
the shapes the rule must NOT refuse, so the row is mutation-sensitive in both
directions. -/

/-- `g9_closure_capture_launders_taint.rvl`,
`g9_service_return_launders_taint.rvl` and
`g9_spawn_config_launders_taint.rvl` all refuse an `fs`-labelled value at a
shell command (`run`, at argument 1). -/
def corpusAuthorityRows : List (Sink × Label) := [(.authority, [Origin.fs])]

/-- `gsecret_service_return_discloses.rvl` refuses a `confidential`-labelled
value at a plain extern host call (`write_file`, at argument 2). -/
def corpusDisclosureRows : List (Sink × Label) := [(.disclosure, [Origin.confidential])]

/-- The admitted twins: the same two sinks on a clean label. -/
def admittedTwins : List (Sink × Label) := [(.authority, []), (.disclosure, [])]

/-- The row's verdict on each of those shapes, computed. -/
theorem corpus_rows_decided :
    (corpusAuthorityRows.map fun r => g9RowB r.1 r.2) = [false]
      ∧ (corpusDisclosureRows.map fun r => g9RowB r.1 r.2) = [false]
      ∧ (admittedTwins.map fun r => g9RowB r.1 r.2) = [true, true] :=
  ⟨rfl, rfl, rfl⟩

/-- The escape is not decorative: the declassifier the checker names moves the
label the walk ends at OFF the one it entered with, and to a label the same
sink admits. -/
theorem escape_moves_the_label :
    applyD ⟨DeclassKind.endorse Origin.fs, true⟩ [Origin.fs] = []
      ∧ g9RowB Sink.authority [Origin.fs] = false
      ∧ g9RowB Sink.authority
          (applyD ⟨DeclassKind.endorse Origin.fs, true⟩ [Origin.fs]) = true := by
  refine ⟨?_, ?_, ?_⟩ <;> decide

/-- **Non-vacuity**: the row prints `fail` on both corpus shapes and `ok` on the
same two sinks on a clean label, so the verdict is mutation-sensitive in both
directions — a table that dropped either corpus sink kind, or that refused
everything, fails here. -/
theorem g9_not_vacuous :
    (corpusAuthorityRows.all fun r => !g9RowB r.1 r.2) = true
      ∧ (corpusDisclosureRows.all fun r => !g9RowB r.1 r.2) = true
      ∧ (admittedTwins.all fun r => g9RowB r.1 r.2) = true := by
  refine ⟨?_, ?_, ?_⟩ <;> decide

end RevL.G9Flow
