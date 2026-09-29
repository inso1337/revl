/-!
# G4, the approval floor: a crossing of an approval-required capability carries a covering edge

Item 246, Decision 3, as the checker enforces it
(`lower._require_declared_approval`, issue #1437 / PR #1537, extended by
PR #1519). An extern that declares `requires approval` makes its capability
TOKEN approval-required: its declared scope when it has one
(`extern emission[pay] fn charge … requires approval` requires `pay`), else
its name. Every marked crossing that reaches a required token must carry a
`with e` edge whose `Approval[C]` scope covers that token. The refusal
carries code G4, category `approval`:

    crossing capability `<token>` requires approval, but this `emit`
    carries no covering `with` edge

The facts, one per concept the rule is stated over:

  * the **capability token**: the set `required` (`lower._approval_index`),
    keyed by token and not by extern name, so the requirement belongs to
    the capability. A sibling extern or a service operation that shares the
    token needs the edge too;
  * the **approval edge**: a crossing's `edge`, the scope `C` of the value
    its `with` clause threads, or `none`. The `emit` value form
    (`let r = emit charge(1)`) has no `with` clause, so its edge is `none`,
    and a compensation shares its step's edge;
  * the **refusal**: `CrossingOK` fails exactly when some token the crossing
    reaches is required and the edge does not cover it.

`Covers` is `lower._approval_covers`: the scope equals the token, or the
scope is a glob that matches it (`fnmatch.fnmatchcase`). `globMatch` models
the two wildcards the checker's scopes use, `*` (any run of characters,
dots included) and `?` (any one character). A `[...]` character class is
NOT modelled: `globMatch` reads `[` as a literal, so a scope using one
would make the Lean oracle and the Python reference (which calls the
shipped `_approval_covers`) disagree, and the differential gate would fail
on it. That is the intended failure mode for a shape the model does not
state, and no corpus program writes one.

What the exporter resolves, and the model does not restate: which tokens a
marked crossing reaches (a required service's `emission[...]` scope, a
direct extern's scope or name, a spawn handle's op scope, the tokens a
module `fn` reaches, each crossing in a `compensate` slot) and which scope
a `with` edge names. Those arrive as facts (`AX`, `AE`, `AR` rows in
`harness/Oracle.lean`); the judgment over them is `crossingB`, and
`crossingB_iff` makes it this file's `CrossingOK`.
-/

namespace RevL.G4Approval

/-- Whether `f` holds of some suffix of `s`, `s` itself and `[]` included. -/
def anySuffix (f : List Char → Bool) : List Char → Bool
  | [] => f []
  | c :: cs => f (c :: cs) || anySuffix f cs

/-- `fnmatch.fnmatchcase(token, scope)` over the `*` and `?` wildcards.
Structural on the pattern: a `*` tries every suffix of the subject, so it
matches any run, the empty one included. -/
def globMatch : List Char → List Char → Bool
  | [], s => s.isEmpty
  | '*' :: ps, s => anySuffix (fun t => globMatch ps t) s
  | '?' :: ps, s =>
      match s with
      | [] => false
      | _ :: cs => globMatch ps cs
  | p :: ps, s =>
      match s with
      | [] => false
      | c :: cs => p == c && globMatch ps cs

/-- An `Approval[scope]` covers a crossing of capability `token`
(`lower._approval_covers`): exact match, or a glob scope matching it. -/
def Covers (scope token : String) : Prop :=
  scope = token ∨ globMatch scope.toList token.toList = true

def coversB (scope token : String) : Bool :=
  scope == token || globMatch scope.toList token.toList

theorem coversB_iff (scope token : String) :
    coversB scope token = true ↔ Covers scope token := by
  simp [coversB, Covers]

/-- One marked crossing: the capability tokens it reaches, and the scope
of its `with` edge (`none` for the value form, or a step without `with`). -/
structure Crossing where
  tokens : List String
  edge : Option String

/-- The approval floor over one crossing: every token it reaches that is
approval-required is covered by its edge. -/
def CrossingOK (required : List String) (c : Crossing) : Prop :=
  ∀ t ∈ c.tokens, t ∈ required → ∃ s, c.edge = some s ∧ Covers s t

def edgeCoversB : Option String → String → Bool
  | some s, t => coversB s t
  | none, _ => false

theorem edgeCoversB_iff (e : Option String) (t : String) :
    edgeCoversB e t = true ↔ ∃ s, e = some s ∧ Covers s t := by
  cases e with
  | none => simp [edgeCoversB]
  | some s => simp [edgeCoversB, coversB_iff]

/-- The decision procedure the oracle prints. -/
def crossingB (required : List String) (c : Crossing) : Bool :=
  c.tokens.all fun t => !required.contains t || edgeCoversB c.edge t

/-- **The approval verdict is the model's judgment.** -/
theorem crossingB_iff (required : List String) (c : Crossing) :
    crossingB required c = true ↔ CrossingOK required c := by
  unfold crossingB CrossingOK
  rw [List.all_eq_true]
  constructor
  · intro h t ht hr
    have h1 := h t ht
    have hc : required.contains t = true := List.contains_iff_mem.mpr hr
    rw [hc] at h1
    exact (edgeCoversB_iff _ _).mp (by simpa using h1)
  · intro h t ht
    by_cases hr : t ∈ required
    · have hc : required.contains t = true := List.contains_iff_mem.mpr hr
      rw [hc]
      simpa using (edgeCoversB_iff _ _).mpr (h t ht hr)
    · have hc : required.contains t = false := by
        cases hx : required.contains t with
        | false => rfl
        | true => exact absurd (List.contains_iff_mem.mp hx) hr
      rw [hc]
      rfl

-- ------------------------------------------------------------ the rule

/-- The value form, and any step without `with`: a crossing with no edge
is admitted exactly when it reaches no approval-required token. This is
why `let r = emit charge(1)` is refused for an approval-required `charge`
however the program is arranged: nothing in the spelling can cover it. -/
theorem no_edge_iff_nothing_required (required ts : List String) :
    CrossingOK required ⟨ts, none⟩ ↔ ∀ t ∈ ts, t ∉ required := by
  constructor
  · intro h t ht hr
    obtain ⟨s, hs, _⟩ := h t ht hr
    cases hs
  · intro h t ht hr
    exact absurd hr (h t ht)

/-- The refusal: a crossing reaching a required token its edge does not
cover is refused, whatever else it reaches. -/
theorem uncovered_required_refused (required : List String) (c : Crossing)
    (t : String) (ht : t ∈ c.tokens) (hr : t ∈ required)
    (hnc : ∀ s, c.edge = some s → ¬ Covers s t) :
    ¬ CrossingOK required c := by
  intro h
  obtain ⟨s, hs, hcov⟩ := h t ht hr
  exact hnc s hs hcov

/-- The floor is keyed by TOKEN: a crossing that reaches no required token
needs no edge, whichever extern or operation it goes through. -/
theorem unrequired_needs_no_edge (required : List String) (c : Crossing)
    (h : ∀ t ∈ c.tokens, t ∉ required) : CrossingOK required c := by
  intro t ht hr
  exact absurd hr (h t ht)

/-- An edge whose scope covers every required token the crossing reaches
admits it. -/
theorem covering_edge_admits (required ts : List String) (s : String)
    (h : ∀ t ∈ ts, t ∈ required → Covers s t) :
    CrossingOK required ⟨ts, some s⟩ := by
  intro t ht hr
  exact ⟨s, rfl, h t ht hr⟩

/-- A `*` at the end of a glob matches any rest: the prefix glob
`prod.*` shape, stated over characters. -/
theorem globMatch_star_any (m : List Char) : globMatch ['*'] m = true := by
  simp only [globMatch]
  induction m with
  | nil => rfl
  | cons x xs ih => simp [anySuffix, ih]

-- ------------------------------------------------------- non-vacuity

/-- `examples/rejections/g4_approval_scoped_extern.rvl`: `charge` is
`emission[production.payment] … requires approval`, crossed with no edge. -/
def scopedRequired : List String := ["production.payment"]

def scopedNoEdge : Crossing := ⟨["production.payment"], none⟩

/-- The fix its header names: `let a = await approval[production.payment]`
and `emit charge(199) with a`. -/
def scopedExactEdge : Crossing := ⟨["production.payment"], some "production.payment"⟩

/-- The same crossing under a glob scope. -/
def scopedGlobEdge : Crossing := ⟨["production.payment"], some "production.*"⟩

/-- A glob that does not reach it. -/
def scopedWrongGlob : Crossing := ⟨["production.payment"], some "staging.*"⟩

/-- `examples/rejections/g4_approval_compensate_other_edge.rvl`: `notify`
requires `mail`, `charge` requires `pay`; the step's edge is `mail`. The
head is covered, the compensation is not. -/
def otherEdgeRequired : List String := ["pay", "mail"]

def otherEdgeHead : Crossing := ⟨["mail"], some "mail"⟩

def otherEdgeCompensation : Crossing := ⟨["pay"], some "mail"⟩

/-- `examples/rejections/g4_approval_compensate.rvl`'s head: `notify`
declares no approval, so `emit notify(1)` needs no edge. -/
def unrequiredHead : Crossing := ⟨["notify"], none⟩

/-- The refusals and admissions of the fixtures, decided and bridged. -/
theorem approval_not_vacuous :
    crossingB scopedRequired scopedNoEdge = false ∧
    ¬ CrossingOK scopedRequired scopedNoEdge ∧
    crossingB scopedRequired scopedExactEdge = true ∧
    CrossingOK scopedRequired scopedExactEdge ∧
    crossingB scopedRequired scopedGlobEdge = true ∧
    CrossingOK scopedRequired scopedGlobEdge ∧
    crossingB scopedRequired scopedWrongGlob = false ∧
    crossingB otherEdgeRequired otherEdgeHead = true ∧
    CrossingOK otherEdgeRequired otherEdgeHead ∧
    crossingB otherEdgeRequired otherEdgeCompensation = false ∧
    ¬ CrossingOK otherEdgeRequired otherEdgeCompensation ∧
    crossingB ["charge"] unrequiredHead = true ∧
    CrossingOK ["charge"] unrequiredHead := by
  refine ⟨by decide, fun h => absurd ((crossingB_iff _ _).mpr h) (by decide),
    by decide, (crossingB_iff _ _).mp (by decide),
    by decide, (crossingB_iff _ _).mp (by decide),
    by decide,
    by decide, (crossingB_iff _ _).mp (by decide),
    by decide, fun h => absurd ((crossingB_iff _ _).mpr h) (by decide),
    by decide, (crossingB_iff _ _).mp (by decide)⟩

/-- The oracle row is mutation-sensitive to each of its three facts alone:
the same tokens and required set with a different edge, the same crossing
with a different required set (so the `AR` rows are load-bearing, not a
restatement of the `AX` rows), and the same edge and required set with a
different token. -/
theorem approval_row_not_vacuous :
    crossingB scopedRequired scopedNoEdge ≠ crossingB scopedRequired scopedExactEdge ∧
    crossingB scopedRequired scopedNoEdge ≠ crossingB [] scopedNoEdge ∧
    crossingB otherEdgeRequired otherEdgeHead ≠
      crossingB otherEdgeRequired otherEdgeCompensation := by
  exact ⟨by decide, by decide, by decide⟩

end RevL.G4Approval
