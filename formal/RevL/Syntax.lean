/-
RevL.Syntax — L0 core syntax (architect-owned; changes gate on the architect).

A deliberately minimal calculus capturing the constructs the guarantee
theorems are about: statements that mutate, statements that pair a mutation
with an inverse, statements marked as boundary crossings, and undo bodies.

Porting notes (DESIGN.md §1, §4): this is the surface level of paper Def. 8
(witnessed inverses) and Def. 48 (confinement). Values are opaque — the
guarantees below are about *effects*, not about evaluation.
-/

namespace RevL.Syntax

/-- A pure expression. Only the shape matters here; the guarantee theorems
are about effect structure, not values. -/
inductive Expr where
  | lit : String → Expr
  | call : String → List Expr → Expr
  deriving Repr, BEq

/-- A statement in a component body (the activation). Four forms, and the
grammar is the point: `effect` *cannot be written* without its inverse
(the two expressions are one constructor), and there is no fifth form that
mutates without an inverse or a marker. -/
inductive Stmt where
  /-- A pure expression; touches nothing (paper Def. 48 confinement). -/
  | pure : Expr → Stmt
  /-- A mutation paired with its inverse — Def. 8's witnessed inverse,
  enforced by syntax rather than by discipline. -/
  | effect : Expr → Expr → Stmt
  /-- A mutation explicitly marked as crossing the system boundary
  (an auditable escape hatch, G8). -/
  | emit : Expr → Stmt
  /-- A mutation with *neither* an inverse nor a marker — the shape G4
  exists to forbid. -/
  | raw : Expr → Stmt
  deriving Repr, BEq

/-! ### Component bodies (issue #2108)

`Stmt` alone is the ACTIVATION's surface. A real component body is more than
its activation: it is the activation *plus* a `provide` block per service key,
and each provide method is a separate body with its own declared parameters.
L0 had no way to write that down, and the two bugs that motivated issue #2108
were exactly there — a walk that skipped the activation body, and a
`Secret[T]` parameter stripped inside its own receiver body. A calculus that
cannot express a provide method's typed parameters cannot state an obligation
about them, which is why the G9 coverage obligation was carried as UNPROVED,
*unstatable* rather than as a `sorry` on a false statement.

These declarations are the minimum that makes the obligation expressible, and
they are additive: every theorem stated over `Stmt` is unchanged. -/

/-- The qualifier a parameter is declared with. The four non-`plain` cases are
the ones that seed an origin in a body's own environment; `plain` is the
absence of a qualifier, not a fifth origin. -/
inductive Qual where
  | plain
  | trusted
  | untrusted
  | secret
  | retained
  deriving Repr, BEq, DecidableEq

/-- A parameter: a name and the qualifier the DECLARATION gives it. The
qualifier is part of the parameter, not of the call site — a receiver's own
body is where a stripped qualifier used to launder a value. -/
structure Param where
  name : String
  qual : Qual
  deriving Repr, BEq

/-- A `provide` method: a named body with its declared parameters. -/
structure Method where
  name : String
  params : List Param
  body : List Stmt
  deriving Repr, BEq

/-- A `provide <key> { … }` block: the methods implementing one service key. -/
structure Provide where
  key : String
  methods : List Method
  deriving Repr, BEq

/-- An item of a component body. The two constructors *are* the
activation/provide distinction: a `step` is a statement of the activation, a
`provide` is a whole provide block. A body's scopes are read off this
discrimination, so "the walk visits every statement of the body" has a
subject. -/
inductive Item where
  | step : Stmt → Item
  | provide : Provide → Item
  deriving Repr, BEq

/-- A component body: its items, in declaration order. -/
structure Body where
  items : List Item
  deriving Repr, BEq

/-! #### The body's scopes

`Body.scopes` is the single enumeration of a body's scopes — the activation
and one per provide method — carrying each scope's statements and declared
parameters TOGETHER, so the three projections cannot drift apart. The G9
coverage theorem (issue #2108) quantifies over exactly this list, which is
what makes "the checker visits every statement of a real body" a statement
about the body rather than a recount of the walk.

These are structural, not typing: a scope is where a statement lives, not
whether it is admitted. -/

/-- One scope of a component body: the activation, or one provide method. -/
structure Scope where
  /-- `activation`, or `<provide key>.<method>`. -/
  label : String
  /-- The scope's statements, in declaration order. -/
  stmts : List Stmt
  /-- The scope's declared parameters. Empty for the activation, which has
  none — the activation runs in the component's own frame. -/
  params : List Param
  deriving Repr, BEq

/-- The activation's statements: the `step` items, in order. -/
def Body.activationStmts (b : Body) : List Stmt :=
  b.items.filterMap fun i => match i with
    | .step s => some s
    | .provide _ => none

/-- The body's provide blocks, in order. -/
def Body.provides (b : Body) : List Provide :=
  b.items.filterMap fun i => match i with
    | .step _ => none
    | .provide p => some p

/-- A provide block's scopes, one per method. -/
def Provide.scopes (p : Provide) : List Scope :=
  p.methods.map fun m =>
    { label := s!"{p.key}.{m.name}", stmts := m.body, params := m.params }

/-- Every scope of a component body, in declaration order: the activation
first — and only when it has a statement, which is exactly when the walk has
a body to walk — then each provide method. -/
def Body.scopes (b : Body) : List Scope :=
  (if b.activationStmts.isEmpty then []
   else [{ label := "activation", stmts := b.activationStmts, params := [] }])
  ++ b.provides.flatMap Provide.scopes

/-- The scope labels of a body, in order — the projection the corpus rows
agree on before any count is compared. -/
def Body.scopeLabels (b : Body) : List String :=
  b.scopes.map fun s => s.label

/-- Each scope's declared parameters, by label. -/
def Body.scopeParams (b : Body) : List (String × List Param) :=
  b.scopes.map fun s => (s.label, s.params)

/-- Every statement of the item, in order: the activation statement itself,
or every method body of a provide block. -/
def Item.stmts : Item → List Stmt
  | .step s => [s]
  | .provide p => p.methods.flatMap (·.body)

/-- Every statement of the body, in order. -/
def Body.stmts (b : Body) : List Stmt :=
  b.items.flatMap Item.stmts

end RevL.Syntax
