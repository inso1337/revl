/-!
# G6, the binding-uniqueness rule: a name is bound once in each scope it is seen

Issue #1169 left one G6 refusal outside the model: a provide-method `let` that
reuses a name the activation body already bound
(`examples/rejections/g6_method_local_shadows_component.rvl`). The checker
refuses a binding whose name is already in view, with code G6, category
`binding` (`src/revl/lower.py`):

* in the activation body, `Env.bind_local` refuses a name that a `requires`
  local or an earlier activation binding holds ("name `x` is already bound in
  C");
* in a provide method, `_check_rebind` refuses a name that a parameter, an
  earlier method local, or an activation binding holds ("`x` is already bound
  in `f`").

Visibility is block-scoped. An `if` arm, a `while` body and a `for` body see
every name bound outside them and add their own, and what they add is dropped
when the block ends (`_lower_scoped_block`), so two sibling arms may reuse a
name and a name bound inside an arm may be bound again after it. The `for`
binding is checked against the names in view and lives in the loop's own block.

## The model

A scope is a SEED, the names in view when it starts (the `requires` locals for
the activation body; the activation's bindings and the method's parameters for
a provide method), and a list of EVENTS: bind a name, enter a block, leave a
block. `ScopeOK` is the rule stated over the frames of names in view: every
bind's name is in no frame. `okB` is the decider the oracle runs and `okB_iff`
proves it is the rule, so the printed `BU` verdict is `ScopeOK`.

## What this does not cover

Which statements bind and which open a block is read off the AST by the
exporter, mirroring the checker's lowering. Pattern binders in a `match` arm
and the body of an `every` timer carry no events. The other G6 refusals,
purity outside an effect form and reassigning an immutable binding, are not
this rule.
-/

namespace RevL.G6Binding

/-- One step through a scope, in source order. -/
inductive Ev where
  /-- A binding of a name (`let`, `var`, `let w = effect spawn …`, a `for`
  binder). -/
  | bind (n : String)
  /-- The start of a block: an `if` arm, a `while` or `for` body. -/
  | enter
  /-- The end of the innermost open block. -/
  | leave
  deriving Repr, DecidableEq

/-- The names in view, innermost block first. -/
abbrev Frames := List (List String)

/-- A name is in view when some frame holds it. -/
def Visible (fs : Frames) (n : String) : Prop := ∃ f ∈ fs, n ∈ f

/-- `Visible`, as a Bool. -/
def visibleB (fs : Frames) (n : String) : Bool := fs.any (fun f => f.contains n)

theorem visibleB_iff (fs : Frames) (n : String) : visibleB fs n = true ↔ Visible fs n := by
  simp [visibleB, Visible, List.any_eq_true]

/-- Bind `n` in the innermost frame. -/
def push (n : String) : Frames → Frames
  | [] => [[n]]
  | f :: r => (n :: f) :: r

/-- Drop the innermost frame; the outermost one is never dropped, so an
unbalanced `leave` cannot empty the scope. -/
def pop : Frames → Frames
  | [] => []
  | [f] => [f]
  | _ :: f :: r => f :: r

/-- **The rule.** Every bind's name is out of view where it is bound. -/
inductive ScopeOK : Frames → List Ev → Prop where
  | nil (fs : Frames) : ScopeOK fs []
  | bind {fs : Frames} {n : String} {rest : List Ev} :
      ¬ Visible fs n → ScopeOK (push n fs) rest → ScopeOK fs (.bind n :: rest)
  | enter {fs : Frames} {rest : List Ev} :
      ScopeOK ([] :: fs) rest → ScopeOK fs (.enter :: rest)
  | leave {fs : Frames} {rest : List Ev} :
      ScopeOK (pop fs) rest → ScopeOK fs (.leave :: rest)

/-- The rule, as the decider the oracle runs. -/
def okB : Frames → List Ev → Bool
  | _, [] => true
  | fs, .bind n :: rest => !visibleB fs n && okB (push n fs) rest
  | fs, .enter :: rest => okB ([] :: fs) rest
  | fs, .leave :: rest => okB (pop fs) rest

theorem okB_iff : ∀ (evs : List Ev) (fs : Frames), okB fs evs = true ↔ ScopeOK fs evs
  | [], fs => ⟨fun _ => .nil fs, fun _ => rfl⟩
  | .bind n :: rest, fs => by
      have ih := okB_iff rest (push n fs)
      constructor
      · intro h
        simp only [okB, Bool.and_eq_true, Bool.not_eq_true'] at h
        refine .bind (fun hv => ?_) (ih.mp h.2)
        have := (visibleB_iff fs n).mpr hv
        rw [h.1] at this
        exact Bool.noConfusion this
      · intro h
        cases h with
        | bind hn hr =>
          simp only [okB, Bool.and_eq_true, Bool.not_eq_true']
          refine ⟨?_, ih.mpr hr⟩
          cases hb : visibleB fs n with
          | false => rfl
          | true => exact absurd ((visibleB_iff fs n).mp hb) hn
  | .enter :: rest, fs => by
      have ih := okB_iff rest ([] :: fs)
      constructor
      · intro h; exact .enter (ih.mp h)
      · intro h; cases h with | enter hr => exact ih.mpr hr
  | .leave :: rest, fs => by
      have ih := okB_iff rest (pop fs)
      constructor
      · intro h; exact .leave (ih.mp h)
      · intro h; cases h with | leave hr => exact ih.mpr hr

/-- A scope: the names in view when it starts, and its events. -/
def BindingOK (seed : List String) (evs : List Ev) : Prop := ScopeOK [seed] evs

/-- A scope's verdict, as the decider the oracle runs. -/
def bindingB (seed : List String) (evs : List Ev) : Bool := okB [seed] evs

/-- **The `BU` verdict is the rule.** -/
theorem bindingB_iff (seed : List String) (evs : List Ev) :
    bindingB seed evs = true ↔ BindingOK seed evs :=
  okB_iff evs [seed]

/-- A name the seed holds cannot be bound: a method `let` may not reuse an
activation binding or a parameter, and an activation binding may not reuse a
`requires` local. -/
theorem seed_rebind_refused (seed : List String) (n : String) (rest : List Ev)
    (h : n ∈ seed) : ¬ BindingOK seed (.bind n :: rest) := by
  intro hok
  cases hok with
  | bind hn _ => exact hn ⟨seed, List.mem_singleton_self seed, h⟩

/-- A name bound twice in a row is refused, in any frames. -/
theorem rebind_refused (fs : Frames) (n : String) (rest : List Ev) :
    ¬ ScopeOK fs (.bind n :: .bind n :: rest) := by
  intro hok
  cases hok with
  | bind _ h2 =>
    cases h2 with
    | bind hn _ =>
      apply hn
      cases fs with
      | nil => exact ⟨[n], by simp [push], by simp⟩
      | cons f r => exact ⟨n :: f, by simp [push], by simp⟩

/-- A block sees what is bound outside it: rebinding an outer name inside a
block is refused. -/
theorem inner_shadow_refused (fs : Frames) (n : String) (rest : List Ev) :
    ¬ ScopeOK fs (.bind n :: .enter :: .bind n :: rest) := by
  intro hok
  cases hok with
  | bind _ h2 =>
    cases h2 with
    | enter h3 =>
      cases h3 with
      | bind hn _ =>
        apply hn
        cases fs with
        | nil => exact ⟨[n], by simp [push], by simp⟩
        | cons f r => exact ⟨n :: f, by simp [push], by simp⟩

/-! ### The corpus shapes -/

/-- `g6_method_local_shadows_component.rvl`, method `set`: the activation
bound `store`, the parameter is `key`, and the body binds `store` again. -/
def shadowSeed : List String := ["store", "key"]
def shadowEvents : List Ev := [.bind "store"]

/-- Two sibling `if` arms each binding `x`, then `x` bound after them: the
block scoping the checker's `_lower_scoped_block` gives. -/
def siblingArms : List Ev :=
  [.enter, .bind "x", .leave, .enter, .bind "x", .leave, .bind "x"]

/-- A `let x` followed by `for (x of …)`: the binder meets the outer name. -/
def letThenFor : List Ev := [.bind "x", .enter, .bind "x", .leave]

theorem fixtures_decided :
    bindingB shadowSeed shadowEvents = false ∧ bindingB ["key"] shadowEvents = true
      ∧ bindingB ["n"] siblingArms = true ∧ bindingB ["n"] letThenFor = false :=
  ⟨rfl, rfl, rfl, rfl⟩

/-- **Non-vacuity**: the rule admits the same binding without the colliding
seed name, admits reuse across sibling blocks, and refuses the corpus shape. -/
theorem binding_not_vacuous :
    ¬ BindingOK shadowSeed shadowEvents ∧ BindingOK ["key"] shadowEvents
      ∧ BindingOK ["n"] siblingArms :=
  ⟨fun h => absurd ((bindingB_iff _ _).mpr h) (by decide),
   (bindingB_iff _ _).mp rfl,
   (bindingB_iff _ _).mp rfl⟩

end RevL.G6Binding
