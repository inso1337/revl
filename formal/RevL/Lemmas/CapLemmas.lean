/-!
RevL.Lemmas.CapLemmas — L1 lemma farm: the parameterized-capability
partial order (roadmap item 294, `src/revl/cap_order.py`).

Core only: this farm imports nothing, so it cannot drift into L0 and no
other farm depends on it.

## What is modelled

Item 294 makes a capability stop being a bare token compared by identity
and become a point in a partial order *under* its token: a pair `(T, P)`
of a token `T` and a **valuation** `P`, a finite map from parameter names
to values (`cap_order.Cap`). The reference's registry of parameter kinds
is CLOSED and assigns each parameter name exactly one value order:

| parameter | kind | value order |
|---|---|---|
| `path` | resource | component-wise prefix (`/tmp/job-42 ≤ /tmp`) |
| `host`, `table` | resource | discrete (equality only) |
| `calls`/`requests`, `size`/`bytes`, `time` | ceiling | numeric (`≤`) |

Because the registry is a function from name to value order, the *kind*
of a parameter is determined by the *shape of its value*; `PVal` below
therefore carries the three value orders as its three constructors, and
`isCeilingVal` recovers the resource/ceiling split without a second
name table. Canonicalization (absolute-path splitting, unit suffixes,
alias resolution) happens at parse in the reference and is upstream of
the order — the model starts from canonical values, as `covers` does.

## Correspondence with `cap_order.covers`

`covers(a, b)` iterates the parameters bound on the **wider** side `a`
and looks each one up in `b`'s valuation. `_make_cap` stores each
parameter name at most once (it refuses a duplicate and sorts by name),
so over canonical caps "iterate `a.params`" and "look up in `a.params`"
agree; `Covers` below is stated in the lookup form, which is the one
that is stable under the reference's own canonicalization.

The reference's `*` clauses (`a.token == "*"` covers only `"*"`; nothing
else covers `"*"`) collapse to token identity, because `_make_cap`
refuses parameters on `*`: a bare `*` has an empty valuation, so the
parameter clause is vacuous either way. `star_covers_iff` records that.
-/

namespace RevL.Lemmas

/-! ## Parameter values and their orders -/

/-- A canonical capability parameter value, one constructor per value
order in the closed registry: a path (component list), a discrete string
(`host`/`table`), or a numeric ceiling (`calls`/`size`/`time`, already
canonicalized to its base unit). -/
inductive PVal where
  | path : List String → PVal
  | discrete : String → PVal
  | ceiling : Nat → PVal
  deriving Repr, DecidableEq

/-- `PathLe narrow wide`: the path order of `_leq_path` — `wide`'s
component list is a **prefix** of `narrow`'s, component-wise and never
string-wise. `/tmp/job-42 ≤ /tmp` holds; `/tmp/jobber ≤ /tmp/job` does
not. -/
def PathLe (narrow wide : List String) : Prop := ∃ rest, narrow = wide ++ rest

/-- `PLeq narrow wide`: `_param_leq` — `narrow` is at-or-below `wide` in
its parameter's value order, i.e. `narrow` is the narrower authority.
Values of different kinds are incomparable (the registry never gives one
name two orders). -/
inductive PLeq : PVal → PVal → Prop where
  | path : ∀ n w, PathLe n w → PLeq (.path n) (.path w)
  | discrete : ∀ s, PLeq (.discrete s) (.discrete s)
  | ceiling : ∀ m n, m ≤ n → PLeq (.ceiling m) (.ceiling n)

/-! ## The pair (T, P) -/

/-- A valuation: parameter name to canonical value. -/
abbrev Valuation := List (String × PVal)

/-- Look a parameter up in a valuation. -/
def lookupV : Valuation → String → Option PVal
  | [], _ => none
  | (k', v) :: rest, k => if k' = k then some v else lookupV rest k

/-- A capability as a point in the partial order (`cap_order.Cap`): a
token and a valuation. A bare token has the empty valuation. -/
structure Cap where
  token : String
  params : Valuation
  deriving Repr

/-- `Covers a b` — "`a` covers `b`", i.e. `b ≤ a`: same token, and `b`
narrows every parameter `a` binds. A parameter bound on `b` but absent
from `a` is free on the wider side and only narrows, so a bare token tops
its cone; a parameter `a` binds and `b` drops is a widening and fails. -/
def Covers (a b : Cap) : Prop :=
  a.token = b.token ∧
  ∀ k v, lookupV a.params k = some v →
    ∃ w, lookupV b.params k = some w ∧ PLeq w v

/-- `covered_by_any`: some held capability covers `c`. -/
def CoveredBy (held : List Cap) (c : Cap) : Prop := ∃ h ∈ held, Covers h c

/-! ## Ceilings (resource / ceiling split) -/

/-- Whether a parameter value is ceiling-kind (`calls`/`size`/`time`). -/
def isCeilingVal : PVal → Bool
  | .ceiling _ => true
  | _ => false

/-- `split_ceilings`, resource projection: drop every ceiling-kind
parameter. A *crossing* binds no ceiling (one call is one call), so the
resource fold compares stripped capabilities. -/
def stripCeilings (c : Cap) : Cap :=
  ⟨c.token, c.params.filter (fun kv => !isCeilingVal kv.2)⟩

/-- The ceiling `c` declares for parameter `k`, if any. -/
def ceilingVal : Option PVal → Option Nat
  | some (.ceiling n) => some n
  | _ => none

/-- `split_ceilings`, ceiling map: the numeric bound `c` declares for
parameter `k`, or `none` when `c` binds no such ceiling (which the
dedicated budget check reads as `+∞`, i.e. wider). -/
def ceilingOf (c : Cap) (k : String) : Option Nat := ceilingVal (lookupV c.params k)

/-- The larger of `n` and an optional bound. -/
def maxWith (n : Nat) : Option Nat → Nat
  | none => n
  | some b => if n ≤ b then b else n

/-- `_ceiling_attenuation_check`'s parent budget: for a token `t` and a
ceiling parameter `k`, the **max** over every held capability under `t`
that declares `k` (the most generous declaration the parent holds), or
`none` when the parent declares no ceiling there. -/
def budgetOf : List Cap → String → String → Option Nat
  | [], _, _ => none
  | h :: rest, t, k =>
      if h.token = t then
        match ceilingOf h k with
        | some n => some (maxWith n (budgetOf rest t k))
        | none => budgetOf rest t k
      else budgetOf rest t k

/-- The wiring keys a capability context declares — the bridge from the
`(T, P)` algebra down to L0's `Ctx` (which is a list of keys). The
reference's key-to-token bridge (`_cap_keyed`) keeps the wiring key as
the token and rides the valuation alongside, so the token *is* the
`Ctx` key. -/
def capKeys (Γ : List Cap) : List String := Γ.map (·.token)

/-! ## The order is a partial order -/

theorem pathLe_refl (p : List String) : PathLe p p := sorry

theorem pathLe_trans (a b c : List String) :
    PathLe a b → PathLe b c → PathLe a c := sorry

theorem pathLe_antisymm (a b : List String) :
    PathLe a b → PathLe b a → a = b := sorry

theorem pleq_refl (v : PVal) : PLeq v v := sorry

theorem pleq_trans {a b c : PVal} : PLeq a b → PLeq b c → PLeq a c := sorry

theorem pleq_antisymm {a b : PVal} : PLeq a b → PLeq b a → a = b := sorry

theorem covers_refl (c : Cap) : Covers c c := sorry

theorem covers_trans {a b c : Cap} : Covers a b → Covers b c → Covers a c := sorry

/-- Antisymmetry, in the form the model can state: mutual coverage pins
the token and makes the two valuations agree on every lookup. (The
reference's tests assert antisymmetry over *canonical* caps only, for
exactly this reason — a valuation is a sorted, duplicate-free
association list, so lookup agreement is structural equality there.) -/
theorem covers_antisymm {a b : Cap} : Covers a b → Covers b a →
    a.token = b.token ∧ ∀ k, lookupV a.params k = lookupV b.params k := sorry

/-- `*` (the unnameable host reach) is covered only by `*`: no nameable
token can be widened into it. -/
theorem star_covers_iff (a : Cap) :
    Covers a ⟨"*", []⟩ ↔ a.token = "*" := sorry

/-! ## Set-level coverage -/

theorem coveredBy_trans {H M : List Cap} {c : Cap} :
    (∀ m ∈ M, CoveredBy H m) → CoveredBy M c → CoveredBy H c := sorry

/-! ## Ceilings -/

theorem ceilingOf_lookup {c : Cap} {k : String} {n : Nat} :
    ceilingOf c k = some n → lookupV c.params k = some (.ceiling n) := sorry

/-- A covered capability's ceiling is at-or-below the covering one's:
`covers` forces the narrow side to bind every parameter the wide side
binds, at a value below it. -/
theorem covers_ceiling_le {a b : Cap} {k : String} {n : Nat} :
    Covers a b → ceilingOf a k = some n →
    ∃ m, ceilingOf b k = some m ∧ m ≤ n := sorry

/-- The parent budget is *attained*: a `some` budget is some held
capability's own declaration, so a bound on every held declaration bounds
the budget. -/
theorem budgetOf_attained {held : List Cap} {t k : String} {b : Nat} :
    budgetOf held t k = some b →
    ∃ h ∈ held, h.token = t ∧ ceilingOf h k = some b := sorry

/-- The parent budget exists whenever some held capability under the
token declares the ceiling, and dominates that declaration. -/
theorem budgetOf_ge {held : List Cap} {t k : String} {h : Cap} {v : Nat} :
    h ∈ held → h.token = t → ceilingOf h k = some v →
    ∃ b, budgetOf held t k = some b ∧ v ≤ b := sorry

/-- Stripping ceilings keeps the token (the resource fold is
ceiling-blind but never token-blind). -/
theorem stripCeilings_token (c : Cap) : (stripCeilings c).token = c.token := rfl

end RevL.Lemmas
