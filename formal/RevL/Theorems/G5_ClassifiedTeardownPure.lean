import RevL.Lemmas.ClassLemmas
import RevL.Lemmas.WalLemmas

/-!
# G5, restated as the rule the compiler actually enforces (item 418, step 4)

## What was wrong with the old statement

`RevL.Theorems.G5_TeardownPure.teardown_registers_nothing` reads
`∀ u : UndoStmt, registrations u = 0`, and its `registrations`

    def registrations : UndoStmt → Nat
      | .call _ => 0
      | .seq a b => registrations a + registrations b

**ignores its argument**: it is the constant-zero function, so
`registrations u = registrations v` holds for every pair of undo bodies,
and an inverse whose body is `db.insert(row)` counts zero registrations.
The theorem assumes its conclusion.

The real rule is `lower._check_witnessed_inverse` (`src/revl/lower.py`
`:2165-2238`): a witnessed extern's declared inverse is refused when its
**transitive** classification includes an emission — directly, or one
`fn` indirection later through `emission_analysis`'s reach fold, which
is the escape the reference explicitly closes ("a callee that is a plain
top-level `fn` is not in `extern_class`, so it passed, yet a `fn` body
may itself reach an emission").

## What is stated here instead

`registrations p fuel b` counts the calls in teardown body `b` whose
**reached classification crosses a host boundary**. It reads its
argument, and `registrations_depends_on_its_argument` proves it: the
review's anti-pattern probe `∀ u v, registrations u = registrations v`
is refuted outright.

Three statements then carry G5:

1. `inverse_reaches_no_emission` — for an admitted program, NO name
   transitively reachable from a witnessed extern's declared inverse is
   classified `emission` or `witnessed`. This is the rule, transitively.
2. `admitted_inverse_body_registers_nothing` — hence any teardown body
   built out of names the inverse reaches registers zero crossings.
3. `admitted_inverse_run_logs_nothing` — and, over step 2's operational
   semantics (`RevL.Lemmas.SemStep`/`SemSteps`), *running* such a body
   appends nothing to the WAL and creates no durable referent. G5 is
   about what a teardown DOES, and this says it against a semantics.

The `sneakyUndo` shape the review asked for is `sneakyProg` below: a
witnessed extern whose `undo` calls an `emission`. The old model counts
it clean; `sneaky_undo_is_refused` proves this model refuses it, refuses
its one-`fn`-deeper twin, and ADMITS the host-local inverse beside them,
so the refusal is not universal. `sneaky_inverse_run_emits` exhibits the
actual run: a one-way boundary record on the teardown path.
-/

namespace RevL.G5Classified

open RevL.Lemmas

/-! ## Teardown bodies and the real registration count -/

/-- A teardown body: the `undo`/`compensate` slot's expression, reduced
to the calls it makes. `nil` is the body that does nothing, `call` is one
callee, `seq` sequences two. -/
inductive UndoBody where
  | nil
  | call (n : Name)
  | seq (a b : UndoBody)
  deriving Repr, DecidableEq

/-- The callees of a teardown body, in order. -/
def bodyNames : UndoBody → List Name
  | .nil => []
  | .call n => [n]
  | .seq a b => bodyNames a ++ bodyNames b

/-- **The registration count that reads its argument.** How many of this
teardown body's calls transitively reach a host boundary crossing —
i.e. how many new effects the teardown registers.

Contrast the old `G5.registrations`, which is the constant-zero function
on its own argument. -/
def registrations (p : Prog) (fuel : Nat) (b : UndoBody) : Nat :=
  (bodyNames b).countP (fun n => (reachCls p fuel n).crosses)

theorem registrations_nil (p : Prog) (fuel : Nat) : registrations p fuel .nil = 0 := rfl

theorem registrations_seq (p : Prog) (fuel : Nat) (a b : UndoBody) :
    registrations p fuel (.seq a b) = registrations p fuel a + registrations p fuel b := by
  simp [registrations, bodyNames, List.countP_append]

/-- Zero registrations is exactly "no call crosses". -/
theorem registrations_zero_iff (p : Prog) (fuel : Nat) (b : UndoBody) :
    registrations p fuel b = 0 ↔ ∀ n ∈ bodyNames b, (reachCls p fuel n).crosses = false := by
  simp [registrations, List.countP_eq_zero]

/-! ## G5, the real rule -/

/-- **G5 over the lattice, transitively.** For a program the checker
admits, no name reachable from a witnessed extern's declared inverse is
classified as a boundary crossing — neither `emission` (a one-way
crossing on the teardown path) nor `witnessed` (infinite regress).

This is `_check_witnessed_inverse`'s rule 3, and the proof consumes it:
`checkerAdmits_elim` supplies `inverseOK`, which is the verdict taken at
the fold's join, and `Cls.admissible_anti` pushes that verdict back down
onto every name the fold folded in. -/
theorem inverse_reaches_no_emission {p : Prog} {fuel : Nat}
    (hadm : CheckerAdmits p fuel) {d : ExternDecl} (hd : d ∈ p.externs)
    (hw : d.cls = .witnessed) {u : Name} (hu : d.undo = some u)
    {n : Name} (hr : ReachesFrom p fuel u n) :
    (declCls p n).crosses = false := by
  have hio := (checkerAdmits_elim hadm hd).2
  unfold inverseOK at hio
  rw [hw, hu] at hio
  exact Cls.not_crosses_of_admissible (Cls.admissible_anti (reaches_le hr) hio)

/-- The declared inverse itself registers nothing. -/
theorem admitted_inverse_registers_nothing {p : Prog} {fuel : Nat}
    (hadm : CheckerAdmits p fuel) {d : ExternDecl} (hd : d ∈ p.externs)
    (hw : d.cls = .witnessed) {u : Name} (hu : d.undo = some u) :
    registrations p fuel (.call u) = 0 := by
  have hio := (checkerAdmits_elim hadm hd).2
  unfold inverseOK at hio
  rw [hw, hu] at hio
  refine (registrations_zero_iff p fuel (.call u)).mpr ?_
  intro n hn
  have : n = u := by simpa [bodyNames] using hn
  subst this
  exact Cls.not_crosses_of_admissible hio

/-- **The whole teardown registers nothing.** Not just the declared
inverse: any body whose calls are names the inverse reaches — the
compensating helper, the helper's helper — registers zero crossings.
This is the fn-wrapper escape closed at body level. -/
theorem admitted_inverse_body_registers_nothing {p : Prog} {f g : Nat}
    (hadm : CheckerAdmits p (f + g)) {d : ExternDecl} (hd : d ∈ p.externs)
    (hw : d.cls = .witnessed) {u : Name} (hu : d.undo = some u)
    {b : UndoBody} (hb : ∀ n ∈ bodyNames b, ReachesFrom p f u n) :
    registrations p g b = 0 := by
  have hio := (checkerAdmits_elim hadm hd).2
  unfold inverseOK at hio
  rw [hw, hu] at hio
  refine (registrations_zero_iff p g b).mpr ?_
  intro n hn
  exact Cls.not_crosses_of_admissible
    (Cls.admissible_anti (reach_le_trans (hb n hn)) hio)

/-! ## The teardown, run

Step 2's small-step semantics accumulates the WAL as the trace of the
steps taken. Compiling a teardown body against the classification lets
G5 be stated as what a run DOES rather than as what a count says. -/

/-- Compile a teardown body to an operational body: a call that
transitively crosses takes an `emit` step — the one-way record with no
reconstructible inverse — and a call that does not takes a `pure` step.
The classification decides which; nothing else does. -/
def toBody (p : Prog) (fuel : Nat) (seqOf : Name → Seq) : UndoBody → Body → Body
  | .nil, k => k
  | .call n, k => if (reachCls p fuel n).crosses then .emit (seqOf n) k else .pure k
  | .seq a b, k => toBody p fuel seqOf a (toBody p fuel seqOf b k)

/-- A body that can only take `pure` steps. -/
inductive PureOnly : Body → Prop where
  | done : PureOnly .done
  | fail : PureOnly .fail
  | pure : ∀ {b}, PureOnly b → PureOnly (.pure b)

theorem pureOnly_toBody {p : Prog} {fuel : Nat} {seqOf : Name → Seq} :
    ∀ {b : UndoBody} {k : Body}, registrations p fuel b = 0 → PureOnly k →
      PureOnly (toBody p fuel seqOf b k) := by
  intro b
  induction b with
  | nil => intro k _ hk; exact hk
  | call n =>
    intro k h hk
    have hc : (reachCls p fuel n).crosses = false := by
      have := (registrations_zero_iff p fuel (.call n)).mp h
      exact this n (by simp [bodyNames])
    show PureOnly (if (reachCls p fuel n).crosses then _ else _)
    rw [hc]
    exact .pure hk
  | seq x y ihx ihy =>
    intro k h hk
    rw [registrations_seq] at h
    have hx : registrations p fuel x = 0 := Nat.eq_zero_of_add_eq_zero_right h
    have hy : registrations p fuel y = 0 := Nat.eq_zero_of_add_eq_zero_left h
    exact ihx hx (ihy hy hk)

/-- A pure-only run moves nothing: same log, same world, still
pure-only. -/
theorem pureOnly_run : ∀ {a c : Config}, SemSteps a c → PureOnly a.body →
    c.log = a.log ∧ c.world = a.world := by
  intro a c h
  induction h with
  | refl => intro _; exact ⟨rfl, rfl⟩
  | @step x y z hs _ ih =>
    intro hp
    cases hs with
    | @pure b w L =>
      have hb : PureOnly b := by cases hp with | pure hh => exact hh
      exact ih hb
    | @witnessed s i b w L => cases hp
    | @emit s b w L => cases hp

/-- **G5 operationally.** A teardown body that registers nothing does
nothing: every run of it ends with the WAL it started with and the world
it started in. No descriptor, no one-way boundary record, no durable
referent. -/
theorem clean_inverse_run_logs_nothing {p : Prog} {fuel : Nat} {seqOf : Name → Seq}
    {b : UndoBody} (h : registrations p fuel b = 0) (w : World) (L : Log)
    {c : Config} (hr : SemSteps ⟨toBody p fuel seqOf b .done, w, L⟩ c) :
    c.log = L ∧ c.world = w :=
  pureOnly_run hr (pureOnly_toBody h .done)

/-- **G5, end to end.** The checker admitted the program, so the
teardown of any of its witnessed externs runs without touching the WAL.
This is the sentence the old model could not say: it had no semantics to
say it against, and a registration count that ignored its argument. -/
theorem admitted_inverse_run_logs_nothing {p : Prog} {f g : Nat}
    (hadm : CheckerAdmits p (f + g)) {d : ExternDecl} (hd : d ∈ p.externs)
    (hw : d.cls = .witnessed) {u : Name} (hu : d.undo = some u)
    {b : UndoBody} (hb : ∀ n ∈ bodyNames b, ReachesFrom p f u n)
    (seqOf : Name → Seq) (w : World) (L : Log) {c : Config}
    (hr : SemSteps ⟨toBody p g seqOf b .done, w, L⟩ c) :
    c.log = L ∧ c.world = w :=
  clean_inverse_run_logs_nothing
    (admitted_inverse_body_registers_nothing hadm hd hw hu hb) w L hr

/-! ## Non-vacuity: the `sneakyUndo` shape

The review's demand, literally: exhibit an inverse that calls an
emission, prove this model refuses it, and prove the refusal is not
universal. -/

/-- A host-local restore. -/
def memRestore : ExternDecl := ⟨"mem_restore", .pure, none, none, []⟩

/-- A one-way crossing. -/
def dbInsert : ExternDecl := ⟨"db_insert", .emission, none, none, []⟩

/-- A `fn` that wraps the crossing — the `330 -> 329`-transitive shape
`_check_witnessed_inverse`'s docstring names. -/
def auditLog : FnDecl := ⟨"audit_log", ["db_insert"]⟩

/-- A witnessed mutation with a proper host-local inverse. -/
def cleanInsert : ExternDecl := ⟨"row_insert", .witnessed, some "mem_restore", none, ["db"]⟩

/-- **`sneakyUndo`.** A witnessed mutation whose declared inverse calls
an emission: teardown would cross a one-way boundary. -/
def sneakyInsert : ExternDecl := ⟨"row_insert", .witnessed, some "db_insert", none, ["db"]⟩

/-- The same escape one `fn` indirection later. -/
def wrappedInsert : ExternDecl := ⟨"row_insert", .witnessed, some "audit_log", none, ["db"]⟩

def cleanProg : Prog := ⟨[memRestore, dbInsert, cleanInsert], [auditLog]⟩
def sneakyProg : Prog := ⟨[memRestore, dbInsert, sneakyInsert], [auditLog]⟩
def wrappedProg : Prog := ⟨[memRestore, dbInsert, wrappedInsert], [auditLog]⟩

def cleanUndo : UndoBody := .call "mem_restore"
def sneakyUndo : UndoBody := .call "db_insert"
def wrappedUndo : UndoBody := .call "audit_log"

/-- **`registrations` depends on its argument.** The review's probe —
`registrations u = registrations v` for all `u, v` — is the anti-pattern
that made the old G5 vacuous. Here it is refuted: the count separates a
host-local inverse from one that calls an emission, and from one that
reaches it through a `fn`. -/
theorem registrations_depends_on_its_argument :
    ¬ (∀ u v : UndoBody, registrations sneakyProg 3 u = registrations sneakyProg 3 v) := by
  intro h
  have := h sneakyUndo cleanUndo
  exact absurd this (by decide)

/-- The counts themselves, so the separation is on the record and is not
merely an inequality. -/
theorem registrations_counts :
    registrations cleanProg 3 cleanUndo = 0 ∧
    registrations sneakyProg 3 sneakyUndo = 1 ∧
    registrations wrappedProg 3 wrappedUndo = 1 ∧
    registrations sneakyProg 3 (.seq sneakyUndo cleanUndo) = 1 ∧
    registrations sneakyProg 3 .nil = 0 := by decide

/-- **The `sneakyUndo` shape is refused, and the refusal is not
universal.** The clean program passes the same `CheckerAdmits` check
that rejects the emission inverse and its `fn`-wrapped twin. The old
model counts all three at zero registrations. -/
theorem sneaky_undo_is_refused :
    CheckerAdmits cleanProg 3 ∧
    ¬ CheckerAdmits sneakyProg 3 ∧
    ¬ CheckerAdmits wrappedProg 3 ∧
    ReachesFrom wrappedProg 1 "audit_log" "db_insert" ∧
    (declCls wrappedProg "db_insert").crosses = true := by
  refine ⟨by decide, by decide, by decide, ?_, by decide⟩
  exact .step (by decide) .refl

/-- The fuel caveat, named rather than hidden. The fn-wrapped escape is
admitted by an UNDER-RUN fold and refused as soon as the fold crosses
the one `fn` edge; `RevL.Lemmas.reach_mono_fuel` is the general
statement that more fuel only ever raises a classification, so running
the closure to stability (which `_emitting_capabilities` does) is what
makes the refusal final. -/
theorem fold_must_run_to_stability :
    CheckerAdmits wrappedProg 0 ∧ ¬ CheckerAdmits wrappedProg 1 := by
  refine ⟨by decide, by decide⟩

/-! ### The refused shape, actually run

A count is a claim about a body. The semantics turns it into a claim
about a run. -/

/-- Referent numbering for the witness runs. -/
def witSeq (n : Name) : Seq := if n = "db_insert" then 7 else 0

/-- **The sneaky inverse really emits.** Compiled against the
classification, `sneakyUndo` is an `emit` step, and taking it appends the
one-way boundary record `Rec.effect 7 true false false` — the record
`RevL.Lemmas.emittedSeqs` reads back as residue and that
`backends/python/replay.py` says has no inverse. That is a real
irreversible crossing on the teardown path, which is exactly what G5
exists to forbid and what the old model scores as clean. -/
theorem sneaky_inverse_run_emits :
    toBody sneakyProg 3 witSeq sneakyUndo .done = .emit 7 .done ∧
    SemSteps ⟨Body.emit 7 .done, [], []⟩ ⟨.done, [7], [Rec.effect 7 true false false]⟩ ∧
    emittedSeqs [Rec.effect 7 true false false] = [7] := by
  refine ⟨by decide, ?_, by decide⟩
  exact .step SemStep.emit .refl

/-- **The clean inverse is silent.** The same compilation, the same
semantics, the opposite verdict: a `pure` step, and every run of it ends
with the WAL and world it began with. -/
theorem clean_inverse_run_is_silent :
    toBody cleanProg 3 witSeq cleanUndo .done = .pure .done ∧
    ∀ c : Config, SemSteps ⟨toBody cleanProg 3 witSeq cleanUndo .done, [], []⟩ c →
      c.log = [] ∧ c.world = [] := by
  refine ⟨by decide, ?_⟩
  intro c hr
  exact clean_inverse_run_logs_nothing (by decide) [] [] hr

end RevL.G5Classified
