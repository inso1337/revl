/-!
# Three declaration rules over a component's body and clauses

Issue #1809: three refusals in the generic out-of-fragment bucket, each a
small rule over facts the export already reaches.

* **Prelude ordering.** `isolate`, `intercept`, `handoff`, a `realms(...)`
  route and a model route derive the resolution context before any
  dependency access, so each must precede every action in the activation
  body (`src/revl/lower.py`: "`isolate` must precede every effect, emit,
  await, and provide statement"). `RevL.Prelude.PreludeOK`.
* **Intercept target.** `intercept` is the component-declared metadata of a
  dependency, so it applies to a required key, never to a provision ("
  `intercept` applies to required keys only — `kv` is a provision").
  `RevL.Prelude.InterceptOK`.
* **Method in service.** A crossing `k.m` names an operation the service of
  `k` declares, and a provide block implements only declared operations (A6,
  "`db.execute` is not a method of service Database").
  `RevL.Prelude.MethodOK`.

Each is a Bool decider with a bridge the oracle prints (`PO`, `IN`, `MS`).
-/

namespace RevL.Prelude

/-! ## Prelude ordering -/

/-- An activation-body statement as the prelude rule sees it. -/
inductive Step where
  /-- `isolate`, `intercept`, `handoff`, a `realms(...)` route, a model route. -/
  | prelude
  /-- Anything else: an effect, an emit, an await, a provide, … -/
  | action
  deriving Repr, DecidableEq

/-- **The rule.** Past the leading preludes, no step is a prelude. -/
def PreludeOK (steps : List Step) : Prop :=
  ∀ s ∈ steps.dropWhile (fun s => s == .prelude), s ≠ .prelude

/-- The rule, as the decider the oracle runs. -/
def preludeB (steps : List Step) : Bool :=
  (steps.dropWhile (fun s => s == .prelude)).all (fun s => s != .prelude)

theorem preludeB_iff (steps : List Step) : preludeB steps = true ↔ PreludeOK steps := by
  simp [preludeB, PreludeOK, List.all_eq_true]

/-- A prelude anywhere after an action is refused. -/
theorem prelude_after_action_refused (rest : List Step) (h : Step.prelude ∈ rest) :
    ¬ PreludeOK (.action :: rest) := by
  intro hok
  have hd : (Step.action :: rest).dropWhile (fun s => s == .prelude) = .action :: rest :=
    rfl
  rw [PreludeOK, hd] at hok
  exact hok .prelude (List.mem_cons_of_mem _ h) rfl

/-- Leading preludes are admitted, before any actions. -/
theorem preludes_first_admitted (n : Nat) (acts : List Step)
    (h : ∀ s ∈ acts, s = .action) :
    PreludeOK (List.replicate n .prelude ++ acts) := by
  induction n with
  | zero =>
    intro s hs hp
    simp only [List.replicate, List.nil_append] at hs
    have hsub := List.dropWhile_sublist (p := fun s => s == Step.prelude) (l := acts)
    rw [h s (hsub.subset hs)] at hp
    exact Step.noConfusion hp
  | succ k ih =>
    intro s hs
    simp only [List.replicate_succ, List.cons_append, List.dropWhile] at hs
    exact ih s (by simpa using hs)

/-! ## Intercept target -/

/-- **The rule.** No intercept targets a provision that is not also required. -/
def InterceptOK (provides requires targets : List String) : Prop :=
  ∀ t ∈ targets, t ∈ provides → t ∈ requires

/-- The rule, as the decider the oracle runs. -/
def interceptB (provides requires targets : List String) : Bool :=
  targets.all (fun t => !provides.contains t || requires.contains t)

theorem interceptB_iff (provides requires targets : List String) :
    interceptB provides requires targets = true ↔ InterceptOK provides requires targets := by
  unfold interceptB InterceptOK
  rw [List.all_eq_true]
  refine forall_congr' fun t => imp_congr_right fun _ => ?_
  by_cases hp : t ∈ provides <;> by_cases hr : t ∈ requires <;> simp [hp, hr]

/-- An intercept of a pure provision is refused. -/
theorem intercept_provision_refused (provides requires targets : List String) (t : String)
    (ht : t ∈ targets) (hp : t ∈ provides) (hr : t ∉ requires) :
    ¬ InterceptOK provides requires targets :=
  fun h => hr (h t ht hp)

/-! ## Method in service -/

/-- A crossing or an implementation, as (service, operation). -/
abbrev Op := String × String

/-- **The rule.** Every operation named is one its service declares. -/
def MethodOK (table calls : List Op) : Prop := ∀ c ∈ calls, c ∈ table

/-- The rule, as the decider the oracle runs. -/
def methodB (table calls : List Op) : Bool := calls.all (fun c => table.contains c)

theorem methodB_iff (table calls : List Op) : methodB table calls = true ↔ MethodOK table calls := by
  simp [methodB, MethodOK, List.all_eq_true]

/-- One undeclared operation refuses the component. -/
theorem undeclared_method_refused (table calls : List Op) (c : Op)
    (hc : c ∈ calls) (hn : c ∉ table) : ¬ MethodOK table calls :=
  fun h => hn (h c hc)

/-! ### The corpus shapes -/

theorem fixtures_decided :
    -- `v2_isolate_after_effect.rvl`: an effect, then an isolate
    preludeB [.action, .prelude] = false ∧ preludeB [.prelude, .action] = true
    -- `v2_intercept_on_provision.rvl`: `Store` provides `kv` and intercepts it
      ∧ interceptB ["kv"] [] ["kv"] = false ∧ interceptB [] ["kv"] ["kv"] = true
    -- `a6_method_not_in_service.rvl`: `Database` declares `query` only
      ∧ methodB [("Database", "query")] [("Database", "execute"), ("Database", "query")] = false
      ∧ methodB [("Database", "query")] [("Database", "query")] = true :=
  ⟨rfl, rfl, rfl, rfl, rfl, rfl⟩

/-- **Non-vacuity**: each rule refuses its corpus shape and admits the twin. -/
theorem prelude_rules_not_vacuous :
    ¬ PreludeOK [.action, .prelude] ∧ PreludeOK [.prelude, .action]
      ∧ ¬ InterceptOK ["kv"] [] ["kv"] ∧ InterceptOK [] ["kv"] ["kv"]
      ∧ ¬ MethodOK [("Database", "query")] [("Database", "execute")]
      ∧ MethodOK [("Database", "query")] [("Database", "query")] :=
  ⟨fun h => absurd ((preludeB_iff _).mpr h) (by decide),
   (preludeB_iff _).mp rfl,
   fun h => absurd ((interceptB_iff _ _ _).mpr h) (by decide),
   (interceptB_iff _ _ _).mp rfl,
   fun h => absurd ((methodB_iff _ _).mpr h) (by decide),
   (methodB_iff _ _).mp rfl⟩

end RevL.Prelude
