/-!
# G4Witnessed — the witnessed-extern site-`undo` rule (issue #2098)

A `witnessed` extern (`extern witnessed fn put_w(k) -> store, unput`) already
declares its own inverse. Its call sites therefore do not spell one: the
teardown accumulator registers the declared inverse itself when the site's
bracket settles on the `Ok` branch. A site that spells an `undo` anyway is
declaring a second, competing inverse, and the checker refuses it, with code
G4, category `witnessed` (`src/revl/lower.py`, `_lower_effect_step`):

    witnessed extern `put_w` cannot declare a site `undo`

The guard is `wit_name in env.witnessed_externs and undo_expr is not None`: the
refusal is about the ACQUISITION's head naming a `witnessed` extern, and about
the site's `undo` slot being non-empty. Nothing else moves it — not the
inverse's spelling, not the bracket's kind, not the marker.

The two rejection documents that exercise it
(`tests/fixtures/effect_statement_rules/g4_witnessed_site_undo_activation.rvl`
and `g4_witnessed_site_undo_method.rvl`) were checked by the shipped checker and
then filed under the `out-of-fragment-witnessed` ratchet bucket, because nothing
in `formal/` decided them.

## The row, and the route it takes

Issue #1811 authorizes two routes. Route A grows the L0 bodies so the checker's
COVERAGE of the site walk is itself proved (roadmap item 418, step 9). Route B —
the sanctioned fallback, and the one taken here, as for `RevL.G9Flow`,
`RevL.GRetain` and `RevL.G4Deferred` — exports the facts the checker reads and
decides the rule on them.

The exporter (`formal/harness/diff_corpus.py`, `sw_rows`) does NOT run a refusal
and read it back: unlike the G9, G-RETAIN and DF rows, the facts this rule reads
are already in the export, in two rows the harness emits for EVERY modeled file:

* the effect site's acquisition head and its classification — the `EX` row's
  classification column, the same table `lower`'s `witnessed_externs` is built
  from (`_externs_with_capability` over `prog.externs`);
* the heads the site's `undo` spells — the `I` row's inverse column, already
  built for the marker rule and already resolved through an inverse's
  indirections (issue #1792).

So the `SW` row carries `(site index, head, classification, the undo heads)` and
`sw_rows` emits one for every effect site whose acquisition head is a
`witnessed` extern. That is a WIDER domain than the bucket: it includes the
admitted twin (`ok_witnessed_effect_in_method.rvl`, `put_w` with NO site
`undo`) and every accepted `witnessed` fixture in the corpus, which is what lets
the row be non-vacuous on the corpus itself rather than on a hand-written shape.

## What this row is NOT: the rule on the corpus, not coverage of the walk

This is the distinction issue #1811 requires be stated, and it is the whole
reason the row is a fallback rather than the fix.

**What the row decides.** The rule, at the head and classification the export
carries and on the site `undo` the export carries. If the checker's own guard is
what the rule is about, then this row agrees with it by construction: the row's
premises ARE the facts the guard read.

**What the row does not witness.** That the checker's WALK is complete — that
every site whose acquisition head is a `witnessed` extern appears in the export
at all. A site the exporter never reaches exports no `SW` row, and this row
cannot tell the difference between "the rule holds here" and "the export looked
here and found nothing". `I` rows are emitted per component statement and an
`EffectStmt`/`LetEffect` always yields one, so the reach is the exporter's, not
this module's; nothing here proves it.

**Why the row is still not vacuous.** An absent row is a FAILURE, not a pass.
`clsOfString` is `none` for a classification this module does not name, the
oracle's `swRowB` prints `fail` — never a vacuous `ok` — for such a row, and
`diff_corpus.py`'s `sw_coverage` re-derives the classification from the file's
own `EX` rows rather than trusting the `SW` row's copy. A checker that stops
refusing a witnessed site `undo` leaves the corpus's two refused files with an
`ok` `SW` row, which `sw_coverage`'s admitted-and-refused ratchet turns RED.
-/

namespace RevL.G4Witnessed

/-- How the head of an effect site's acquisition is classified. The checker's
guard reads exactly one of these — `witnessed` — and `other` covers the three
the `EX` table's vocabulary carries besides (`pure`, `acquire`, `emission`),
none of which this rule refuses. -/
inductive Cls where
  /-- `extern witnessed fn …`: the extern declares its own inverse. -/
  | witnessed
  /-- `pure`, `acquire` or `emission`: not this rule's business. -/
  | other
  deriving Repr, DecidableEq

/-- One effect site whose acquisition head is a `witnessed` extern (one `SW`
fact): the head's name, its classification, and the heads the site's `undo`
spells. -/
structure Site where
  head : String
  cls : Cls
  undo : List String
  deriving Repr, DecidableEq

/-- The rule for one site, declaratively: a site whose acquisition head is a
`witnessed` extern may not spell a site `undo`. The head's NAME is carried but
does not enter the rule — the checker's guard is on the classification, and the
name is there so the row's own output shows which extern the site reached. -/
def Legal (s : Site) : Prop := s.cls ≠ .witnessed ∨ s.undo = []

/-- The rule for one site, as the decider the oracle runs. -/
def legalB : Site → Bool
  | ⟨_, .witnessed, []⟩ => true
  | ⟨_, .witnessed, _ :: _⟩ => false
  | ⟨_, .other, _⟩ => true

theorem legalB_iff (s : Site) : legalB s = true ↔ Legal s := by
  obtain ⟨h, c, u⟩ := s
  cases c <;> cases u <;> simp [legalB, Legal]

/-- A file's verdict: every witnessed site in it is legal. -/
def SwOK (ss : List Site) : Prop := ∀ s ∈ ss, Legal s

/-- The file verdict, as the decider the oracle runs. -/
def swB (ss : List Site) : Bool := ss.all legalB

/-- **The `SW` verdict is the rule.** The oracle prints `swB`; this is what makes
the printed Bool the model's judgment. -/
theorem swB_iff (ss : List Site) : swB ss = true ↔ SwOK ss := by
  simp only [swB, List.all_eq_true, SwOK]
  exact forall_congr' fun s => imp_congr_right fun _ => legalB_iff s

/-- How the export spells a head's classification. The `EX` table's whole
vocabulary is `pure | acquire | emission | witnessed`; anything else is a
classification this module does not name, and yields `none` rather than being
folded into `other`. -/
def clsOfString : String → Option Cls
  | "witnessed" => some .witnessed
  | "pure" | "acquire" | "emission" => some .other
  | _ => none

/-- The rule at the row's COLUMNS: the classification as the export spells it,
and the heads the site's `undo` spells. An unreadable classification is `false`
— never a vacuous `ok`. -/
def legalCols (cls : String) (undo : List String) : Bool :=
  match clsOfString cls with
  | some c => legalB ⟨"", c, undo⟩
  | none => false

/-- **`legalCols` is the rule at the columns**, and an unreadable
classification is `false` rather than vacuously `true`: the model refuses a row
it cannot read instead of agreeing with it. -/
theorem legalCols_iff (cls : String) (undo : List String) :
    legalCols cls undo = true ↔
      ∃ c, clsOfString cls = some c ∧ Legal ⟨"", c, undo⟩ := by
  unfold legalCols
  cases clsOfString cls with
  | none => simp
  | some c => simp [legalB_iff]

/-- An unreadable classification is refused, whatever the site spells. -/
theorem unmodelled_classification_is_refused :
    legalCols "unknown" ["unput"] = false
      ∧ legalCols "unknown" [] = false :=
  ⟨rfl, rfl⟩

/-- **The corpus flip, at the columns the oracle reads.** The `witnessed` head
with the `unput` the two refused files spell is refused; the same head with no
site `undo` (the admitted twin's shape) is admitted; and a NON-`witnessed` head
spelling the same `unput` is admitted, which is the other disjunct of the rule.
So the verdict is read off both the classification and the `undo`, not off the
head's name. -/
theorem corpus_columns_decided :
    legalCols "witnessed" ["unput"] = false
      ∧ legalCols "witnessed" [] = true
      ∧ legalCols "emission" ["unput"] = true :=
  ⟨rfl, rfl, rfl⟩

/-- A non-`witnessed` head is admitted whatever its site spells: the guard the
checker raises is on the classification, so a `pure` extern with an `undo` is
some other rule's business (or none). -/
theorem other_head_never_refused (h : String) (u : List String) :
    Legal ⟨h, .other, u⟩ := Or.inl Cls.noConfusion

/-- A witnessed head with a site `undo` is refused, whatever the head is named
and whatever the `undo` reaches. -/
theorem witnessed_undo_refused (h : String) (u : List String) (hu : u ≠ []) :
    ¬ Legal ⟨h, .witnessed, u⟩ := by
  intro hlegal
  rcases hlegal with hcls | hu'
  · exact hcls rfl
  · exact hu hu'

/-- One illegal site refuses the file, wherever it sits in the list. -/
theorem refused_of_mem (ss : List Site) (s : Site) (hs : s ∈ ss)
    (hbad : ¬ Legal s) : ¬ SwOK ss := fun h => hbad (h s hs)

/-! ### The corpus shapes (tests/fixtures/effect_statement_rules/) -/

/-- `g4_witnessed_site_undo_activation.rvl`: `put_w` (witnessed, inverse
`unput`) called with a site `undo unput` in the activation body. The `I` row is
`I … C 0 effect put_w unput`. -/
def refusedActivation : Site := ⟨"put_w", .witnessed, ["unput"]⟩

/-- `g4_witnessed_site_undo_method.rvl`: the same site in a provide-method body.
Its `EX` and `I` rows are identical to the activation file's, so the rule must
decide both alike. -/
def refusedMethod : Site := ⟨"put_w", .witnessed, ["unput"]⟩

/-- `ok_witnessed_effect_in_method.rvl`: the ADMITTED twin. The same extern,
called with NO site `undo` — the `I` row's inverse column is empty, which is
what the teardown accumulator needs. -/
def admittedTwin : Site := ⟨"put_w", .witnessed, []⟩

/-- `verified_method_effect/ok_witnessed.rvl`: `write_batch` (witnessed,
inverse `restore_batch`) called with no site `undo`. -/
def admittedWriteBatch : Site := ⟨"write_batch", .witnessed, []⟩

theorem corpus_sites_decided :
    swB [refusedActivation] = false ∧ swB [refusedMethod] = false
      ∧ swB [admittedTwin] = true ∧ swB [admittedWriteBatch] = true :=
  ⟨rfl, rfl, rfl, rfl⟩

/-- **The corpus flip is one column.** The refused and admitted shapes have the
same head and the same classification; they differ only in the site `undo`. So
the verdict is the `undo` column's and not the extern's, and no `witnessed`
extern is refused for being witnessed. -/
theorem corpus_flip_is_the_undo_column :
    refusedMethod.head = admittedTwin.head
      ∧ refusedMethod.cls = admittedTwin.cls
      ∧ refusedMethod.undo ≠ admittedTwin.undo :=
  ⟨rfl, rfl, by decide⟩

/-- **Non-vacuity, the executed flip.** The corpus's own columns, decided: both
refused files' shapes are refused, the admitted twins are admitted, and the
refused shape is ADMITTED when either premise is mutated away — the site `undo`
emptied, and the classification moved off `witnessed`. A row that returned a
constant, or that read only one of the two, fails here.

This is the theorem `sw_coverage`'s ratchet mirrors: the harness re-decides the
same three mutations on the rows the exporter emitted for the corpus, and
`#print axioms` (via `run_gate.sh`) is what proves this one is computed rather
than assumed. -/
theorem witnessed_site_undo_not_vacuous :
    swB [refusedActivation] = false
      ∧ swB [refusedMethod] = false
      ∧ swB [admittedTwin] = true
      ∧ swB [admittedWriteBatch] = true
      ∧ swB [{ refusedMethod with undo := [] }] = true
      ∧ swB [{ refusedMethod with cls := .other }] = true :=
  ⟨rfl, rfl, rfl, rfl, rfl, rfl⟩

end RevL.G4Witnessed
