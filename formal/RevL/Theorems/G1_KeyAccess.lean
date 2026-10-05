/-!
# G1, declared access over a component's real heads

`RevL.Theorems.G1_DeclaredAccess` states declared-only access over L0
statements, which have no component bodies, so nothing compared it with the
checker (issue #1807). The checker refuses a call head whose root names no
declared requirement, with code G1 ("`db` is not a declared requirement of
C", `src/revl/lower.py`): in the activation body, in a provide method, inside
an `if`/`else`/`while`/`for`/guard block and in a condition, and as the target
of an `intercept`.

## The model

The exporter reads every call head the component makes, at every nesting
depth, and drops the roots the checker resolves without a requirement: a
binding in the component (a `let`, `var`, parameter, loop variable, arrow or
`match` binder), a module `fn` or `extern`, a name a `use` imports, a host
family (`Map`, `Pool`, `Job`, `Stream`), and a type or variant constructor.
What remains are the ACCESS roots (`GA` rows), and an `intercept` target that
is not a provision joins them. The rule is stated here: every access root is
a declared requirement. `accessB_iff` bridges the oracle's `G1` row.

## What this does not cover

The classification of a root as local, callable, host or constructor is the
exporter's, read off the AST component-wide rather than per statement scope,
so a name bound in one method and used undeclared in another is read as
local. The corpus measures that reading against the checker in both
directions (`missed-G1` and `formal-strict` are fatal).
-/

namespace RevL.G1Access

/-- **The rule.** Every access root names a declared requirement. -/
def AccessOK (declared roots : List String) : Prop := ∀ r ∈ roots, r ∈ declared

/-- The rule, as the decider the oracle runs. -/
def accessB (declared roots : List String) : Bool := roots.all (fun r => declared.contains r)

/-- **The `G1` verdict is the rule.** -/
theorem accessB_iff (declared roots : List String) :
    accessB declared roots = true ↔ AccessOK declared roots := by
  simp [accessB, AccessOK, List.all_eq_true]

/-- One undeclared access root refuses the component, wherever it sits. -/
theorem undeclared_refused (declared roots : List String) (r : String)
    (hr : r ∈ roots) (hn : r ∉ declared) : ¬ AccessOK declared roots :=
  fun h => hn (h r hr)

/-- Declaring more never refuses: the rule is monotone in the requirements. -/
theorem access_mono (declared declared' roots : List String)
    (hsub : ∀ r, r ∈ declared → r ∈ declared') (h : AccessOK declared roots) :
    AccessOK declared' roots :=
  fun r hr => hsub r (h r hr)

/-- Declaring the missing key admits the component it refused. -/
theorem declaring_admits (declared roots : List String) (r : String)
    (h : ∀ x ∈ roots, x ≠ r → x ∈ declared) : AccessOK (r :: declared) roots := by
  intro x hx
  by_cases hxr : x = r
  · subst hxr; exact List.mem_cons_self
  · exact List.mem_cons_of_mem _ (h x hx hxr)

/-! ### The corpus shapes -/

/-- `g1_undeclared_access.rvl`: `Logger` declares nothing and calls `db`. -/
def loggerRoots : List String := ["db"]

/-- `notes.rvl`-like: a component that calls its declared `store`. -/
def declaredRoots : List String := ["store"]

theorem fixtures_decided :
    accessB [] loggerRoots = false ∧ accessB ["db"] loggerRoots = true
      ∧ accessB ["store"] declaredRoots = true ∧ accessB ["db"] declaredRoots = false :=
  ⟨rfl, rfl, rfl, rfl⟩

/-- **Non-vacuity**: the rule refuses the undeclared access and admits it
once the key is declared. -/
theorem g1_access_not_vacuous :
    ¬ AccessOK [] loggerRoots ∧ AccessOK ["db"] loggerRoots :=
  ⟨fun h => absurd ((accessB_iff _ _).mpr h) (by decide), (accessB_iff _ _).mp rfl⟩

end RevL.G1Access
