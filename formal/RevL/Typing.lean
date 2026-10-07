import RevL.Syntax

/-
RevL.Typing — L0 typing judgment (architect-owned; changes gate on the
architect).

`Typed` is the checker's admission relation. Its shape *is* the G4 claim:
there is a constructor for `effect`, one for `emit`, one for `pure` — and
no constructor for `raw`. The G4 theorem (RevL.Theorems.G4) makes that
shape explicit as a statement.
-/

namespace RevL.Typing

open RevL.Syntax

/-- The checker admits statement `s`. -/
inductive Typed : Stmt → Prop where
  | pure : ∀ e, Typed (.pure e)
  | effect : ∀ m u, Typed (.effect m u)
  | emit : ∀ m, Typed (.emit m)

/-- The statement mutates the shared environment. -/
inductive IsMutation : Stmt → Prop where
  | effect : ∀ m u, IsMutation (.effect m u)
  | emit : ∀ m, IsMutation (.emit m)
  | raw : ∀ m, IsMutation (.raw m)

/-- The statement carries an inverse (the mutation/undo pair). -/
inductive HasInverse : Stmt → Prop where
  | effect : ∀ m u, HasInverse (.effect m u)

/-- The statement is an explicit, enumerable boundary crossing (G8). -/
inductive IsEmit : Stmt → Prop where
  | emit : ∀ m, IsEmit (.emit m)

/-! ### Confinement surface (paper Def. 48; architect extension) -/

/-- The declared requirement keys a component may reach (service names).
L0 architect decision (formal/STATUS.md TODO 3): capabilities are part of
the typing context, not a separate judgment. -/
abbrev Ctx := List String

/-- Every call head appearing in an expression, in order. This is the
reach surface the audit enumerates (G8). -/
def heads : Expr → List String
  | .lit _ => []
  | .call k args => k :: args.flatMap heads

/-- Every call head appearing anywhere in a statement. -/
def stmtHeads : Stmt → List String
  | .pure e => heads e
  | .effect m u => heads m ++ heads u
  | .emit m => heads m
  | .raw m => heads m

/-- `ReachIn C e`: `e` reaches only through keys declared in `C` —
declared-only access by construction. -/
inductive ReachIn : Ctx → Expr → Prop where
  | lit : ∀ C s, ReachIn C (.lit s)
  | call : ∀ C k args, k ∈ C → (∀ a ∈ args, ReachIn C a) →
      ReachIn C (.call k args)

/-- `TypedIn C s`: the checker admits `s` under requirements `C`. Same
shape as `Typed` — still no `raw` constructor — now confinement-checked. -/
inductive TypedIn : Ctx → Stmt → Prop where
  | pure : ∀ C e, ReachIn C e → TypedIn C (.pure e)
  | effect : ∀ C m u, ReachIn C m → ReachIn C u → TypedIn C (.effect m u)
  | emit : ∀ C m, ReachIn C m → TypedIn C (.emit m)

/-! ### Component bodies (issue #2108)

The typing side of the body syntax added in `RevL.Syntax`. The structural
scopes (`Body.scopes` and friends) live in `RevL.Syntax`; what is added here
is the admission relation over a whole body, which is what the G9 coverage
obligation is stated about. -/

/-- The statement-level admission relation under a name the body-level
judgments cannot shadow.

While declaring `Item.TypedIn`, the unqualified name `TypedIn` resolves to
the declaration being defined — whose index is `Item`, not `Stmt` — so a
binder written from `TypedIn C s` silently gets the wrong type. Naming it
once here keeps every occurrence below unambiguous. -/
abbrev TypedInStmt := TypedIn

/-- `Item.TypedIn C i`: the checker admits every statement of the item under
`C` — the activation statement itself, or every statement of every method of
a provide block. The provide case quantifies over the methods *inside* the
constructor, so a body cannot be admitted by ignoring one. -/
inductive Item.TypedIn (C : Ctx) : Item → Prop where
  | step : ∀ (s : Stmt), TypedInStmt C s → Item.TypedIn C (.step s)
  | provide : ∀ (p : Provide),
      (∀ m ∈ p.methods, ∀ s ∈ m.body, TypedInStmt C s) →
      Item.TypedIn C (.provide p)

/-- `Body.TypedIn C b`: the checker admits the whole body under `C`. -/
inductive Body.TypedIn (C : Ctx) : Body → Prop where
  | mk : ∀ (b : Body), (∀ i ∈ b.items, Item.TypedIn C i) → Body.TypedIn C b

/-- The statement is the unmarked mutation G4 forbids. Spelled as a predicate
rather than compared with `=`, so the lemmas below are `cases` on the typing
judgment and need no decidability. -/
inductive IsRaw : Stmt → Prop where
  | raw : ∀ m, IsRaw (.raw m)

/-- An admitted item admits each of its statements. The bridge from the item
judgment to the statement list the coverage theorem reads: without it,
"every statement of the body" and "the body's statements" would be two
unrelated lists. -/
theorem Item.TypedIn.stmts_typed {C : Ctx} {i : Item} (h : Item.TypedIn C i)
    {st : Stmt} (hst : st ∈ i.stmts) : TypedInStmt C st := by
  cases h with
  | step s hs =>
      simp only [Item.stmts, List.mem_singleton] at hst
      subst hst
      exact hs
  | provide p hp =>
      simp only [Item.stmts, List.mem_flatMap] at hst
      obtain ⟨m, hm, hst⟩ := hst
      exact hp m hm st hst

/-- An admitted list of items admits every statement of every one of them. -/
theorem items_stmts_typed {C : Ctx} {items : List Item}
    (hall : ∀ i ∈ items, Item.TypedIn C i) {st : Stmt}
    (hst : st ∈ items.flatMap Item.stmts) : TypedInStmt C st := by
  induction items with
  | nil => simp only [List.flatMap_nil, List.not_mem_nil] at hst
  | cons i is ih =>
      simp only [List.flatMap_cons, List.mem_append] at hst
      rcases hst with h1 | h2
      · exact Item.TypedIn.stmts_typed (hall i (by simp)) h1
      · exact ih (fun j hj => hall j (by simp [hj])) h2

/-- An admitted body admits every statement of every one of its items. -/
theorem Body.TypedIn.stmts_typed {C : Ctx} {b : Body} (h : Body.TypedIn C b)
    {st : Stmt} (hst : st ∈ b.stmts) : TypedInStmt C st := by
  cases h with
  | mk hall => exact items_stmts_typed hall hst

/-- The typing judgment admits no `raw` statement, at any depth of the new
body syntax: growing L0 did not open a door G4 closes. -/
theorem TypedIn.not_raw {C : Ctx} {s : Stmt} (h : TypedInStmt C s) :
    ¬ IsRaw s := by
  intro hr
  cases h <;> cases hr

/-- The same, read through a whole component body. -/
theorem Body.TypedIn.not_raw {C : Ctx} {b : Body} (h : Body.TypedIn C b)
    {st : Stmt} (hst : st ∈ b.stmts) : ¬ IsRaw st :=
  (h.stmts_typed hst).not_raw

end RevL.Typing
