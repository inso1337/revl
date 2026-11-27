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
What remains are the ACCESS roots, and an `intercept` target that is not a
provision joins them. The rule is stated here: every access root is a declared
requirement. `accessB_iff` is the first slice's decider over a component's
access roots; the oracle's `G1` row is now the per-scope `componentAccessB`
below.

## Per scope (issue #2744, second slice)

The first reading above classified a root as local, callable, import, host
family or constructor in the exporter and COMPONENT-WIDE, so a name bound in
one method and used undeclared in another was read as local and the model
never saw it. The classification now belongs to the model and is per scope:
the exporter ships, for each scope (the activation, or one provide method),
the roots the scope uses with what the FILE alone resolves each to
(`FileKind`), and the names that scope itself binds; `classify` decides which
`RootClass` each root falls in, and `ScopeAccessOK` is the rule that no root of
a scope is `undeclared`. `scopeAccess_iff_roots_declared` bridges the new rule
to `AccessOK` above (it restates the per-scope rule in the first slice's terms,
and does not tie the roots to the exporter's), and
`other_scope_local_does_not_resolve` is the witness the component-wide reading
could not give.

## What this does not cover

Binding is read per SCOPE, not per statement: a name bound anywhere in a scope
counts as bound for every use in that scope, whatever the form that bound it:
a `let` inside a nested `if`/`while`/`for` block, an arrow's parameter beyond
its arrow, a `match` arm's binder beyond its arm. The checker refuses a bare
use of an arrow parameter or of an arm binder outside its arrow or arm, and
the exporter reads it as bound. An activation binding made before a `provide`
block counts for that block's methods, and a name used before its binding is
read as bound. For a call head the checker resolves a requirement or a host
family BEFORE the local scope, so `classify`'s `bound` is the checker's reading
in value position only; the verdict is the same either way. The corpus measures
the reading against the checker in both directions (`missed-G1` and
`formal-strict` are fatal).
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


/-! ### Per scope (issue #2744)

What the FILE alone resolves a root to, which no scope can change: a module
`fn` or `extern` (`callable`), a name a `use` imports (`imported`), a host
family (`host`) and a type or variant constructor (`ctor`). `none` is a root
the file says nothing about, and `key` is a root that is only ever a
requirement: an `intercept` target, which no binding shadows. -/

/-- What the file alone resolves a root to. -/
inductive FileKind where
  | none
  | callable
  | imported
  | host
  | ctor
  | key
  deriving Repr, DecidableEq

/-- The class a root falls in, in one scope of one component. `bound` is a
binding of THAT scope. -/
inductive RootClass where
  | bound
  | callable
  | imported
  | host
  | ctor
  | declared
  | undeclared
  deriving Repr, DecidableEq

/-- **The per-scope classification.** A binding of the scope shadows what the
file resolves, as the checker's name resolution does for a name read in value
position (a call head resolves a requirement or a host family first, with the
same verdict); a root nothing else resolves is a declared requirement or is
undeclared. An `intercept` target (`key`) is never shadowed. -/
def classify (declared locals : List String) (fk : FileKind) (r : String) : RootClass :=
  match fk with
  | .key => if r ∈ declared then .declared else .undeclared
  | .none =>
      if r ∈ locals then .bound
      else if r ∈ declared then .declared else .undeclared
  | .callable => if r ∈ locals then .bound else .callable
  | .imported => if r ∈ locals then .bound else .imported
  | .host => if r ∈ locals then .bound else .host
  | .ctor => if r ∈ locals then .bound else .ctor

/-- A root a scope uses, with what the file alone resolves it to. -/
abbrev Use := String × FileKind

/-- **The per-scope rule.** No root a scope uses is undeclared. -/
def ScopeAccessOK (declared locals : List String) (uses : List Use) : Prop :=
  ∀ u ∈ uses, classify declared locals u.2 u.1 ≠ .undeclared

/-- The rule, as the decider the oracle runs. -/
def scopeAccessB (declared locals : List String) (uses : List Use) : Bool :=
  uses.all fun u => decide (classify declared locals u.2 u.1 ≠ .undeclared)

/-- **The per-scope verdict is the rule.** -/
theorem scopeAccessB_iff (declared locals : List String) (uses : List Use) :
    scopeAccessB declared locals uses = true ↔ ScopeAccessOK declared locals uses := by
  simp [scopeAccessB, ScopeAccessOK, List.all_eq_true]

/-- **A component is admitted when every scope is.** Each scope carries the
names it binds and the roots it uses. -/
def ComponentAccessOK (declared : List String) (scopes : List (List String × List Use)) :
    Prop :=
  ∀ s ∈ scopes, ScopeAccessOK declared s.1 s.2

/-- The component rule, as the decider the oracle runs. -/
def componentAccessB (declared : List String) (scopes : List (List String × List Use)) : Bool :=
  scopes.all fun s => scopeAccessB declared s.1 s.2

theorem componentAccessB_iff (declared : List String)
    (scopes : List (List String × List Use)) :
    componentAccessB declared scopes = true ↔ ComponentAccessOK declared scopes := by
  simp only [componentAccessB, ComponentAccessOK, List.all_eq_true]
  constructor
  · intro h s hs
    exact (scopeAccessB_iff _ _ _).mp (h s hs)
  · intro h s hs
    exact (scopeAccessB_iff _ _ _).mpr (h s hs)

/-- The roots of a scope that only a requirement can resolve: the ones its own
bindings and the file do not. -/
def accessRoots (declared locals : List String) (uses : List Use) : List String :=
  (uses.filter fun u =>
      decide (classify declared locals u.2 u.1 = .declared
        ∨ classify declared locals u.2 u.1 = .undeclared)).map (·.1)

/-- A class that is neither `declared` nor `undeclared` is not a requirement
root, and the other two split on membership of the declared requirements. -/
theorem classify_ok_iff (declared locals : List String) (fk : FileKind) (r : String) :
    classify declared locals fk r ≠ .undeclared ↔
      (classify declared locals fk r = .declared
        ∨ classify declared locals fk r = .undeclared → r ∈ declared) := by
  cases fk <;> unfold classify <;>
    by_cases h1 : r ∈ locals <;> by_cases h2 : r ∈ declared <;> simp [h1, h2]

/-- **The bridge to the first slice.** The per-scope rule admits a scope exactly
when `AccessOK` admits the roots that scope can only resolve through a
requirement. `accessRoots` is itself defined through `classify`, so this
restates the per-scope rule in the first slice's terms and does not tie the
roots to anything the exporter reads. -/
theorem scopeAccess_iff_roots_declared (declared locals : List String) (uses : List Use) :
    ScopeAccessOK declared locals uses ↔
      AccessOK declared (accessRoots declared locals uses) := by
  unfold ScopeAccessOK AccessOK accessRoots
  constructor
  · intro h r hr
    simp only [List.mem_map, List.mem_filter, decide_eq_true_eq] at hr
    obtain ⟨u, ⟨hu, hc⟩, rfl⟩ := hr
    exact (classify_ok_iff declared locals u.2 u.1).mp (h u hu) hc
  · intro h u hu
    rw [classify_ok_iff]
    intro hc
    exact h u.1 (List.mem_map.mpr ⟨u, List.mem_filter.mpr ⟨hu, by simpa using hc⟩, rfl⟩)

/-- One undeclared root refuses its scope. -/
theorem undeclared_root_refuses_scope (declared locals : List String) (uses : List Use)
    (u : Use) (hu : u ∈ uses) (hc : classify declared locals u.2 u.1 = .undeclared) :
    ¬ ScopeAccessOK declared locals uses :=
  fun h => h u hu hc

/-- Declaring more never refuses: the per-scope rule is monotone in the
requirements. -/
theorem scope_access_mono (declared declared' locals : List String) (uses : List Use)
    (hsub : ∀ r, r ∈ declared → r ∈ declared') (h : ScopeAccessOK declared locals uses) :
    ScopeAccessOK declared' locals uses := by
  intro u hu
  have hu' := h u hu
  have key : ∀ (fk : FileKind) (r : String),
      classify declared locals fk r ≠ .undeclared → classify declared' locals fk r ≠ .undeclared := by
    intro fk r hne
    cases fk <;> unfold classify at hne ⊢ <;>
      by_cases h1 : r ∈ locals <;> by_cases h2 : r ∈ declared <;>
      by_cases h3 : r ∈ declared' <;> simp [h1, h2, h3] at hne ⊢ <;>
      exact h3 (hsub r h2)
  exact key u.2 u.1 hu'

/-! ### The corpus shapes -/

/-- The scopes of a component whose first method binds `tmp` and whose second
uses it: the shape a component-wide classification read as local. -/
def methodA : List String × List Use := (["tmp"], [("tmp", .none)])
def methodB : List String × List Use := ([], [("tmp", .none)])

/-- The same two scopes with their bindings pooled, the component-wide reading. -/
def pooled : List String × List Use := (["tmp"], [("tmp", .none), ("tmp", .none)])

/-- **A name bound in one scope does not resolve in another.** The component
of two methods is refused, and the component-wide pooled reading of the same
two scopes is admitted, which is the difference the second slice makes. -/
theorem other_scope_local_does_not_resolve :
    ¬ ComponentAccessOK [] [methodA, methodB]
      ∧ ComponentAccessOK [] [pooled]
      ∧ ComponentAccessOK [] [methodA] := by
  refine ⟨?_, ?_, ?_⟩ <;> rw [← componentAccessB_iff] <;> decide

/-- The component rule over its scopes is decided in each scope's own binding:
the same use is `bound` where the scope binds it, and `undeclared` where it
does not. -/
theorem same_use_two_classes :
    classify [] ["tmp"] .none "tmp" = .bound
      ∧ classify [] [] .none "tmp" = .undeclared := by decide

/-- What the file resolves, a scope binding shadows, and an `intercept` target
is never shadowed. -/
theorem file_kinds_resolve_without_a_requirement :
    classify [] [] .callable "f" = .callable
      ∧ classify [] [] .imported "g" = .imported
      ∧ classify [] [] .host "Map" = .host
      ∧ classify [] [] .ctor "Some" = .ctor
      ∧ classify [] ["f"] .callable "f" = .bound
      ∧ classify [] ["db"] .key "db" = .undeclared
      ∧ classify ["db"] ["db"] .key "db" = .declared := by decide

/-- **Non-vacuity**: a scope is refused for an undeclared root, admitted for a
declared one, and the verdict moves with the scope's own bindings and with
the declared requirements. -/
theorem g1_scope_access_not_vacuous :
    ¬ ScopeAccessOK [] [] [("db", .none)]
      ∧ ScopeAccessOK ["db"] [] [("db", .none)]
      ∧ ScopeAccessOK [] ["db"] [("db", .none)]
      ∧ ScopeAccessOK [] [] [("f", .callable), ("Map", .host), ("Some", .ctor)]
      ∧ ¬ ScopeAccessOK [] [] [("db", .key)] := by
  refine ⟨?_, ?_, ?_, ?_, ?_⟩ <;> first
    | (rw [← scopeAccessB_iff]; decide)
    | (intro h; have := (scopeAccessB_iff _ _ _).mpr h; revert this; decide)

end RevL.G1Access
