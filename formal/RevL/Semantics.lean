import RevL.Syntax

/-
RevL.Semantics: the L0 teardown stack and the two-phase replay
(architect-owned).

The teardown story (DESIGN.md §3.2, G7, and the contract in
docs/design/teardown-contract.md): one LIFO disposer stack per activation,
three entry kinds on it, and a teardown that walks the stack in two phases
under the activation's verdict.

Roadmap item 418, C3 and step 5. The previous model had ONE entry kind and
defined `teardown` as `log.reverse.map (·.inverse)`, so it said every
accumulated inverse is replayed on every teardown. That contradicts the
reference: `backends/python/runtime.py`'s `_Transactional.__call__` reads
the owning frame's commit bit and, on a clean COMMIT, *discharges* the
entry, so the inverse never runs and the witness is dropped, because the
mutation IS the deliverable. Only an ABORT replays it. A `_Compensation`
entry is discharged on commit too, and on abort it is deferred out of the
Phase-1 pass into a bounded Phase-2 drain. Only a `bracket` entry replays
on every teardown, because releasing an acquired handle is always right.

The kind and the verdict are therefore both in the model, and G7 is stated
relative to them (`RevL.Theorems.G7_LifoComplete`).
-/

namespace RevL.Semantics

open RevL.Syntax

/-- The three entry kinds of the one per-activation LIFO stack
(docs/design/teardown-contract.md, "the three entry kinds, one stack";
243 Slice 2a decision 2: the distinction lives in the entry, not in a
second structure). -/
inductive EntryKind where
  /-- An acquisition. Its inverse releases a handle, so it replays on
  clean unload and on abort alike. -/
  | bracket
  /-- A witnessed (transactional) mutation, item 243. The mutation is the
  deliverable: a clean commit discharges the entry, only an abort replays
  it. -/
  | transactional
  /-- A compensation for an emission that already crossed, item 247. A
  clean commit discharges it; an abort runs it in Phase 2, never inline in
  the Phase-1 proof pass. -/
  | compensation
  deriving Repr, BEq, DecidableEq

/-- The activation's verdict at teardown time: the reference's
`Frame._committed` bit, read at disposal rather than captured at
registration, because whether the activation commits depends on whether a
LATER step aborts. -/
inductive Verdict where
  | commit
  | abort
  deriving Repr, BEq, DecidableEq

/-- One entry of the teardown stack: the kind, and the inverse the
registration captured. -/
structure LogEntry where
  kind : EntryKind
  inverse : Expr
  deriving Repr

/-- Whether an entry of this kind actually runs its inverse under this
verdict. This is the "replays on clean unload" / "replays on abort" pair
of rows of the contract's table, read straight off it. -/
def EntryKind.replaysUnder : EntryKind → Verdict → Bool
  | .bracket, _ => true
  | .transactional, .commit => false
  | .transactional, .abort => true
  | .compensation, .commit => false
  | .compensation, .abort => true

/-- Discharged is the complement of replayed: registered, and then dropped
without running (the reference's `discharged = True`, inverse and witness
references released so no rollback state survives a committed
transaction). -/
def EntryKind.dischargedUnder (k : EntryKind) (v : Verdict) : Bool :=
  !k.replaysUnder v

theorem replays_or_discharges (k : EntryKind) (v : Verdict) :
    k.replaysUnder v = true ↔ k.dischargedUnder v = false := by
  cases k <;> cases v <;>
    simp [EntryKind.replaysUnder, EntryKind.dischargedUnder]

/-- Phase 1 is the proof pass: brackets and transactional entries only.
Compensations are skipped here by construction. -/
def EntryKind.inPhase1 : EntryKind → Bool
  | .bracket => true
  | .transactional => true
  | .compensation => false

/-- Phase 2 is the intent pass: compensations only. -/
def EntryKind.inPhase2 (k : EntryKind) : Bool := !k.inPhase1

/-- The Phase-1 entries this verdict actually replays, in the order they
run: LIFO over the registration order. -/
def phase1 (v : Verdict) (log : List LogEntry) : List LogEntry :=
  (log.filter (fun e => e.kind.inPhase1 && e.kind.replaysUnder v)).reverse

/-- The Phase-2 compensation drain, LIFO within itself. -/
def phase2 (v : Verdict) (log : List LogEntry) : List LogEntry :=
  (log.filter (fun e => e.kind.inPhase2 && e.kind.replaysUnder v)).reverse

/-- Every entry the teardown replays, in observable order: the whole
Phase-1 pass, then the compensation drain. -/
def replayed (v : Verdict) (log : List LogEntry) : List LogEntry :=
  phase1 v log ++ phase2 v log

/-- Every entry the teardown discharges without running, in registration
order. -/
def discharged (v : Verdict) (log : List LogEntry) : List LogEntry :=
  log.filter (fun e => e.kind.dischargedUnder v)

/-- Derived teardown: the inverses that actually run, in the order they
run. -/
def teardown (v : Verdict) (log : List LogEntry) : List Expr :=
  (replayed v log).map (·.inverse)

-- A filter split on a boolean side condition partitions the list: no
-- element is counted twice and none is dropped. Kept local to L0 so this
-- file still imports nothing outside L0 (formal/scripts/layering_gate.py).
set_option linter.unusedSimpArgs false in
private theorem filter_split_length {α : Type} (s r : α → Bool) :
    ∀ l : List α,
      (l.filter (fun a => s a && r a)).length +
        (l.filter (fun a => !s a && r a)).length = (l.filter r).length := by
  intro l
  induction l with
  | nil => rfl
  | cons a rest ih =>
    simp only [List.filter_cons]
    cases hs : s a <;> cases hr : r a <;>
      simp only [hs, hr, Bool.and_true, Bool.and_false, Bool.not_true,
        Bool.not_false, Bool.false_eq_true, if_false, if_pos,
        List.length_cons] <;> omega

/-- The two phases partition the replaying entries: every entry is in
exactly one phase, so the phase split neither drops nor duplicates. -/
theorem phase_lengths_add (v : Verdict) (log : List LogEntry) :
    (log.filter (fun e => e.kind.inPhase1 && e.kind.replaysUnder v)).length +
      (log.filter (fun e => e.kind.inPhase2 && e.kind.replaysUnder v)).length =
    (log.filter (fun e => e.kind.replaysUnder v)).length :=
  filter_split_length (fun e => e.kind.inPhase1)
    (fun e => e.kind.replaysUnder v) log

/-- LIFO-completeness, length form: teardown runs one inverse per entry
the verdict replays, nothing dropped and nothing invented. Note the
correction against the pre-step-5 model, which counted the WHOLE log here
and so over-counted every commit with a transactional entry on the
stack. -/
theorem teardown_length : ∀ (v : Verdict) (log : List LogEntry),
    (teardown v log).length =
      (log.filter (fun e => e.kind.replaysUnder v)).length := by
  intro v log
  simp only [teardown, replayed, phase1, phase2, List.length_map,
    List.length_append, List.length_reverse]
  exact phase_lengths_add v log

/-- Teardown of an empty activation is empty (no residue from nothing). -/
theorem teardown_nil (v : Verdict) : teardown v [] = [] := by
  cases v <;> rfl

end RevL.Semantics
