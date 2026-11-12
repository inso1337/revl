import RevL.Syntax

/-!
# Cardinality — the emission crossing bound (item 260, issue #2743)

`src/revl/cardinality.py` reports, per component and capability, how many
times one activation can cross that capability's boundary, and answers
`unbounded` when it cannot prove a finite number. `formal/STATUS.md` and the
P08 paper carried that analysis with no Lean theorem behind it: the ceiling
and budget theorems in `RevL.CapCeilings` bound *authority*, not a crossing
COUNT, and the paper said so itself.

This file states the count. `Cmd` is the shape a body's crossings take:

* a sequence sums its parts;
* a branch takes the worse arm (a bound has to hold on every path);
* a bounded loop multiplies its body by the iteration ceiling it was given.

`bound` is the number the analysis reports for such a shape, and `Runs` is
one execution of it, recording how many crossings it actually made.
`runs_le_bound` proves every execution crosses at most `bound`. The last
section ties the shape to L0: a component body is a sequence of statements,
an `emit` statement is one crossing, and `bodyBound` is the reported number
over that body.

## Scope, stated honestly

* This is the COUNT half only. Which crossings the checker finds (a
  `req`-target emission call, a spawn-handle provision call, a retry
  multiplier), and when it must answer `unbounded` (a loop it cannot bound,
  recursion, a crossing behind a host extern or a first-class arrow), are the
  checker's classification and are decided on the corpus by the `CARD` row.
  The theorem does not claim that classification is complete.
* A `loop` here carries its iteration ceiling as an INPUT. That is exactly
  the case the checker certifies (`base + max_iters * per_iter` for a linear
  self-recursion); a loop whose ceiling is not statically known has no
  `Cmd` in this model and is `unbounded` in the analysis. Nothing here says
  anything about that body.
* The model counts crossings of ONE capability at a time, which is the
  granularity the report is keyed by. A statement crossing two capabilities
  is one `cross` in each of the two commands drawn for it. `Body.cmd` is the
  exception and says so: it is the body's TOTAL `emit` surface, not a
  per-capability command.
* The `Body` half counts an `emit` statement once. A crossing written some
  other way (an ordinary `req` call in a `let`, a retry loop) is the
  checker's classification and is not counted by `bodyCmd`; the `CARD` row
  carries the exported command the report actually answers, so the row does
  not depend on this conservative reading.
-/

namespace RevL.Cardinality

open RevL.Syntax

/-- The crossing shape of a body for one capability: nothing, one crossing,
a sequence, a branch, or a loop with a given iteration ceiling. -/
inductive Cmd where
  | nil : Cmd
  | cross : Cmd
  | seq : Cmd → Cmd → Cmd
  | branch : Cmd → Cmd → Cmd
  | loop : Nat → Cmd → Cmd
  deriving Repr, BEq, DecidableEq

/-- The bound the analysis reports for one capability over a crossing shape:
a sequence sums, a branch takes the worse arm, a bounded loop multiplies by
its iteration ceiling. -/
def bound : Cmd → Nat
  | .nil => 0
  | .cross => 1
  | .seq a b => bound a + bound b
  | .branch a b => Nat.max (bound a) (bound b)
  | .loop n body => n * bound body

/-- The number of crossings in a list of per-iteration crossing counts. -/
def sumList : List Nat → Nat
  | [] => 0
  | c :: cs => c + sumList cs

/-- One execution of a crossing shape, recording how many crossings it made.

`loop` carries the per-iteration counts it ran (`cs`) and the fact that it
ran at most the iteration ceiling `n`: a loop may exit early, and each
iteration may take a branch that crosses a different number of times. -/
inductive Runs : Cmd → Nat → Prop where
  /-- An empty body crosses nothing. -/
  | nil : Runs .nil 0
  /-- One crossing makes one crossing. -/
  | cross : Runs .cross 1
  /-- A sequence runs both parts; their crossings add. -/
  | seq : Runs a m → Runs b n → Runs (.seq a b) (m + n)
  /-- A branch runs one arm; its crossings are that arm's. -/
  | branchLeft : Runs a m → Runs (.branch a b) m
  /-- A branch runs one arm; its crossings are that arm's. -/
  | branchRight : Runs b n → Runs (.branch a b) n
  /-- A bounded loop runs at most `n` iterations, with the crossing count of
  each one: the total is their sum. -/
  | loop : ∀ (n : Nat) (body : Cmd) (cs : List Nat),
      cs.length ≤ n → (∀ c ∈ cs, Runs body c) → Runs (.loop n body) (sumList cs)

/-- A sum of per-iteration counts is at most the iteration count times the
per-iteration ceiling: the list half of the loop case. -/
theorem sumList_le_length_mul (cs : List Nat) (m : Nat) :
    (∀ c ∈ cs, c ≤ m) → sumList cs ≤ cs.length * m := by
  induction cs with
  | nil => intro _; simp [sumList]
  | cons c cs ih =>
    intro h
    have hc : c ≤ m := h c (by simp)
    have hcs : ∀ x ∈ cs, x ≤ m := by
      intro x hx
      exact h x (by simp [hx])
    have ih' := ih hcs
    simp only [sumList, List.length_cons]
    rw [Nat.add_mul, Nat.one_mul]
    omega

/-- **The cardinality bound is sound.** Any execution of a crossing shape
makes at most the number the analysis reports for it. This is the statement
`src/revl/cardinality.py` carries without a proof: sequence sums, a branch
is bounded by its worse arm, and a bounded loop is bounded by its ceiling
times its body. -/
theorem runs_le_bound {c : Cmd} {m : Nat} (h : Runs c m) : m ≤ bound c := by
  induction h with
  | nil => simp [bound]
  | cross => simp [bound]
  | seq _ _ ih1 ih2 =>
    simp [bound]
    omega
  | branchLeft _ ih =>
    exact Nat.le_trans ih (by simp [bound]; exact Nat.le_max_left _ _)
  | branchRight _ ih =>
    exact Nat.le_trans ih (by simp [bound]; exact Nat.le_max_right _ _)
  | loop n body cs hlen hcs ih =>
    simp [bound]
    have hs := sumList_le_length_mul cs (bound body) ih
    have hmul : cs.length * bound body ≤ n * bound body :=
      Nat.mul_le_mul_right (bound body) hlen
    omega

/-! ## The tie to L0's component body

`RevL.Syntax.Body` (issue #2108) is the component shape L0 carries. A body
is a sequence of statements; an `emit` statement is the one shape L0 writes
down as a crossing of its own, and every other statement crosses nothing in
this conservative reading. `bodyCmd` is therefore a sequence of `cross` and
`nil`, and `bodyBound` is the number the theorem bounds. -/

/-- One statement as a crossing shape: an `emit` is one crossing, everything
else is none. -/
def stmtCmd : Stmt → Cmd
  | .emit _ => .cross
  | _ => .nil

/-- The sequence of a list of crossing shapes. -/
def seqCmds : List Cmd → Cmd
  | [] => .nil
  | c :: cs => .seq c (seqCmds cs)

/-- A component body's crossing shape: its statements, in declaration order,
each `emit` a crossing. This is the body's TOTAL crossing surface (every
`emit` in the activation and in every provide method); a per-capability
command is drawn from the checker's IR by the `CARD` row, which is where the
per-token count lives. -/
def bodyCmd (b : Body) : Cmd := seqCmds (b.stmts.map stmtCmd)

/-- The bound a component body reports over its whole `emit` surface. -/
def bodyBound (b : Body) : Nat := bound (bodyCmd b)

/-- **The body's bound bounds its executions.** An execution of the sequence
a body induces crosses at most the number `bodyBound` reports. -/
theorem body_bound_bounds_runs (b : Body) {m : Nat} (h : Runs (bodyCmd b) m) :
    m ≤ bodyBound b :=
  runs_le_bound h

/-! ## Non-vacuity witnesses

The hypotheses above are inhabited, and each constructor of the count is
exercised at a number the theorem can distinguish from its neighbours. -/

/-- A sequence of two crossings: the reported number is 2. -/
def seqWitness : Cmd := .seq .cross .cross

/-- A branch of a crossing and nothing: the reported number is 1, not 2 — the
worse arm, which is what makes a branch a bound and not a sum. -/
def branchWitness : Cmd := .branch .cross .nil

/-- Three iterations of one crossing: the reported number is 3. -/
def loopWitness : Cmd := .loop 3 .cross

theorem witness_seq_runs : Runs seqWitness 2 :=
  Runs.seq Runs.cross Runs.cross

theorem witness_branch_runs : Runs branchWitness 1 :=
  Runs.branchLeft (a := .cross) (b := .nil) Runs.cross

theorem witness_loop_runs : Runs loopWitness 3 := by
  have h : Runs (.loop 3 .cross) (sumList [1, 1, 1]) := by
    refine Runs.loop 3 .cross [1, 1, 1] (by decide) ?_
    intro c hc
    have hc1 : c = 1 := by simpa using hc
    subst hc1
    exact Runs.cross
  simpa [loopWitness, sumList] using h

/-- The witnesses really land on the numbers the count predicts, and an
execution exceeding the bound is REFUSED by `runs_le_bound` — so an
execution model that crossed four times in a three-iteration loop can never
be passed off as an execution of it. -/
theorem cardinality_not_vacuous :
    bound seqWitness = 2 ∧ bound branchWitness = 1 ∧
    bound loopWitness = 3 ∧
    Runs seqWitness 2 ∧ Runs branchWitness 1 ∧ Runs loopWitness 3 ∧
    ¬ Runs (.loop 3 .cross) 4 := by
  refine ⟨by decide, by decide, by decide, witness_seq_runs,
    witness_branch_runs, witness_loop_runs, ?_⟩
  intro h
  have hb := runs_le_bound h
  simp [bound] at hb

/-- A body witness: an `emit`, then a pure statement. The body is a
non-empty sequence, not the empty grammar. -/
def witnessBody : Body :=
  ⟨[.step (.emit (.call "svc" [])), .step (.pure (.lit "x"))]⟩

theorem witness_body_runs : Runs (bodyCmd witnessBody) 1 := by
  show Runs (.seq .cross (.seq .nil .nil)) 1
  exact Runs.seq Runs.cross (Runs.seq Runs.nil Runs.nil)

end RevL.Cardinality
