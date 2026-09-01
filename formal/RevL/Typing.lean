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

end RevL.Typing
