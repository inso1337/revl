/-!
# G-COUNCIL-SPLIT: what a model council may do when its members disagree

Issue #1811 (group 1). The checker refuses, with code G-COUNCIL-SPLIT
(`src/revl/model_council.py`, rule 10):

* **Tie outcome** (category `model-council`): an aggregation written to admit
  when its members disagree. `on_tie allow` — and the other spellings of the
  same discipline, `admit`, `proceed`, `accept`, `first`, `any` — PARSES, so
  the refusal can say why, and is then refused by name. A tie is the case
  where the rule names no value, and a council that answers anyway has told
  the caller that several models agreed when they did not.

## The model

`SplitOK` is stated here over the councils a file DECLARES, each carrying the
tie outcome the checker reads for it; `splitB_iff` bridges the oracle's `CTV`
row. This is a finite check over declarations with no reach, which is why it
needs no component body: the exporter reads `prog.model_councils` and nothing
else.

## What the default is, and where it is applied

`on_tie` is OPTIONAL and its default is `split`, the named outcome that
carries the members' differing answers as evidence. The exporter resolves the
default (`council_rows` in `formal/harness/diff_corpus.py`), exactly as
the `MO`/`MP` rows resolve a role's residence, so the model judges the
outcome the checker judges and not the spelling of its absence.

## What this does not cover

The other `model-council` refusals are different rules with the same code,
and this row is deliberately silent on them: `unknown tie outcome` (the
vocabulary is validated so a typo is not a silent resolution), the
aggregation vocabulary and its totality on the declared member set, the
member functions and their uniqueness, `quorum` bases other than `declared`,
and `exactly one aggregate rule`. A council declaring no aggregation emits no
row here at all, so its refusal lands in the harness's `out-of-fragment`
bucket rather than being read as an agreement this row cannot make.
-/

namespace RevL.ModelCouncil

/-- One declared council's tie policy: the outcome the checker reads for it,
after the `split` default. -/
structure CouncilDecl where
  name : String
  tie : String
  deriving Repr, DecidableEq

/-- The tie outcomes the checker refuses, verbatim
`model_council.ADMITTING_TIE_OUTCOMES`. They parse so that the refusal can
give the reason. -/
def admitting : List String :=
  ["allow", "admit", "proceed", "accept", "first", "any"]

/-- **The rule.** No declared council admits when its members disagree. -/
def SplitOK (councils : List CouncilDecl) : Prop :=
  ∀ c ∈ councils, c.tie ∉ admitting

/-- The rule, as the decider the oracle runs. -/
def splitB (councils : List CouncilDecl) : Bool :=
  councils.all (fun c => !admitting.contains c.tie)

theorem splitB_iff (councils : List CouncilDecl) :
    splitB councils = true ↔ SplitOK councils := by
  unfold splitB SplitOK
  rw [List.all_eq_true]
  refine forall_congr' fun c => imp_congr_right fun _ => ?_
  by_cases hc : c.tie ∈ admitting <;> simp [hc]

/-- A council whose tie policy admits is refused. -/
theorem admitting_refused (councils : List CouncilDecl) (c : CouncilDecl)
    (hc : c ∈ councils) (ht : c.tie ∈ admitting) : ¬ SplitOK councils :=
  fun h => h c hc ht

/-- `on_tie split` is admitted: it is the default and it names no value. -/
theorem split_admitted (councils : List CouncilDecl)
    (h : ∀ c ∈ councils, c.tie = "split") : SplitOK councils :=
  fun c hc => by rw [h c hc]; decide

/-- `on_tie deny` is admitted: a council may decide that inconclusive means
refuse, which can only ever move the outcome toward refusing. -/
theorem deny_admitted (councils : List CouncilDecl)
    (h : ∀ c ∈ councils, c.tie = "deny") : SplitOK councils :=
  fun c hc => by rw [h c hc]; decide

/-- A spelling outside both vocabularies is admitted by THIS row: it is the
`unknown tie outcome` rule that refuses it, and conflating the two would make
this row claim a judgment it does not make. -/
theorem unknown_tie_not_this_row : SplitOK [⟨"Release", "agreement"⟩] :=
  (splitB_iff _).mp (by decide)

/-! ### The corpus shapes -/

/-- `examples/rejections/gcouncilsplit_on_tie_allow.rvl`: council `Release`,
`aggregate unanimous on_tie allow`. -/
def releaseCouncil : List CouncilDecl := [⟨"Release", "allow"⟩]

/-- `examples/model_council.rvl` and `examples/model_council_binding.rvl`:
the admitted twins, `on_tie split` written down. -/
def splitCouncil : List CouncilDecl := [⟨"Release", "split"⟩]

/-- The narrowing the vocabulary permits, `on_tie deny`. -/
def denyCouncil : List CouncilDecl := [⟨"Release", "deny"⟩]

theorem fixtures_decided :
    splitB releaseCouncil = false ∧ splitB splitCouncil = true
      ∧ splitB denyCouncil = true ∧ splitB [] = true :=
  ⟨rfl, rfl, rfl, rfl⟩

/-- **Non-vacuity**: the rule refuses the admitting tie and admits `split`
(the default) and `deny`, so the printed `CTV` verdict is
mutation-sensitive in both directions. -/
theorem council_not_vacuous :
    ¬ SplitOK releaseCouncil ∧ SplitOK splitCouncil ∧ SplitOK denyCouncil :=
  ⟨fun h => absurd ((splitB_iff _).mpr h) (by decide),
   (splitB_iff _).mp (by decide), (splitB_iff _).mp (by decide)⟩

end RevL.ModelCouncil
