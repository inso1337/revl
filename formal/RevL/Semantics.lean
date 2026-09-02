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
LATER step aborts.

Roadmap item 443 adds the third constructor. `commit` and `abort` are the
two COOPERATIVE verdicts: each of them SETTLES the activation, because
each entry on the stack ends up either replayed or discharged. `halted` is
the operator E-Stop (`docs/design/443-estop.md`), and it settles nothing —
it runs no inverse, drops no inverse, and leaves every registered entry
OWED. That is not a defect in the halt; it is what an immediate stop
means, and the model says so rather than pretending a teardown ran. -/
inductive Verdict where
  | commit
  | abort
  /-- Operator E-Stop (item 443): stop dispatching, replay nothing,
  discharge nothing, owe everything. -/
  | halted
  deriving Repr, BEq, DecidableEq

/-- Whether this verdict SETTLES the activation: whether every entry it
leaves behind is accounted for by the replay/discharge dichotomy alone.
`commit` and `abort` do; the E-Stop deliberately does not, which is
exactly why `EntryKind.strandedUnder` below exists (item 443). -/
def Verdict.settles : Verdict → Bool
  | .commit => true
  | .abort => true
  | .halted => false

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
  | _, .halted => false          -- item 443: an E-Stop replays nothing
  | .bracket, _ => true
  | .transactional, .commit => false
  | .transactional, .abort => true
  | .compensation, .commit => false
  | .compensation, .abort => true

/-- Discharged: registered, and then dropped without running (the
reference's `discharged = True`, inverse and witness references released so
no rollback state survives a committed transaction).

Under a SETTLING verdict this is the complement of `replaysUnder`, and it
was defined as that complement before item 443. It is now spelled out per
row, because under `.halted` an entry is neither replayed NOR discharged:
discharge RELEASES the inverse and the witness, and an E-Stop must do the
opposite — keep them, because the reconciliation path
(`revl recover`) is what reads them back. -/
def EntryKind.dischargedUnder : EntryKind → Verdict → Bool
  | _, .halted => false          -- item 443: an E-Stop discharges nothing
  | .bracket, _ => false
  | .transactional, .commit => true
  | .transactional, .abort => false
  | .compensation, .commit => true
  | .compensation, .abort => false

/-- STRANDED (item 443): registered, not run, and NOT dropped — the
obligation is still owed to whoever reconciles. This is the third
disposition, and it is inhabited by exactly one verdict: the E-Stop. -/
def EntryKind.strandedUnder (k : EntryKind) (v : Verdict) : Bool :=
  !k.replaysUnder v && !k.dischargedUnder v

/-- The replay/discharge dichotomy, now with the hypothesis that made it
true all along: it is a property of a verdict that SETTLES. Item 443's
E-Stop is the verdict at which it fails, and `disposition_trichotomy`
below is what holds instead — the honest generalisation, not a weakening:
the settling case is unchanged. -/
theorem replays_or_discharges (k : EntryKind) (v : Verdict)
    (hv : v.settles = true) :
    k.replaysUnder v = true ↔ k.dischargedUnder v = false := by
  cases k <;> cases v <;>
    simp_all [Verdict.settles, EntryKind.replaysUnder, EntryKind.dischargedUnder]

/-- Every (kind, verdict) pair has EXACTLY ONE disposition. This is the
total accounting item 443 needs: an entry is replayed, or discharged, or
stranded, never two of them and never none. Nothing falls off the books,
under any verdict including the halt. -/
theorem disposition_trichotomy (k : EntryKind) (v : Verdict) :
    (k.replaysUnder v = true ∧ k.dischargedUnder v = false
       ∧ k.strandedUnder v = false)
    ∨ (k.replaysUnder v = false ∧ k.dischargedUnder v = true
       ∧ k.strandedUnder v = false)
    ∨ (k.replaysUnder v = false ∧ k.dischargedUnder v = false
       ∧ k.strandedUnder v = true) := by
  cases k <;> cases v <;> decide

/-- The E-Stop row of the table, per kind: no replay, no discharge, always
stranded. The third column of `docs/design/teardown-contract.md`'s table
(item 443). -/
theorem halted_strands_every_kind (k : EntryKind) :
    k.replaysUnder .halted = false ∧ k.dischargedUnder .halted = false
      ∧ k.strandedUnder .halted = true := by
  cases k <;> decide

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

/-- Every entry the teardown STRANDS (item 443): neither run nor dropped,
in registration order. This list is the E-Stop's in-flight inventory — the
records the halt owes the operator, and the descriptors `revl recover`
reads back. Empty under every settling verdict. -/
def stranded (v : Verdict) (log : List LogEntry) : List LogEntry :=
  log.filter (fun e => e.kind.strandedUnder v)

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

/-! ### Item 443: the books balance under every verdict

`teardown_length` counts what RAN. The E-Stop runs nothing, so on its own
that count is `0` and says nothing about the entries left behind. What
makes the halt honest is that the three dispositions PARTITION the stack:
every registered entry is on exactly one of the replay list, the discharge
list and the stranded inventory, so an entry can never be quietly dropped
by adding a verdict. -/

-- Two disjoint side conditions plus their joint complement partition a
-- list. Kept local to L0 so this file still imports nothing outside L0
-- (formal/scripts/layering_gate.py).
private theorem filter_three_length {α : Type} (p q : α → Bool)
    (hpq : ∀ a, (p a && q a) = false) :
    ∀ l : List α,
      (l.filter p).length + (l.filter q).length +
        (l.filter (fun a => !p a && !q a)).length = l.length := by
  intro l
  induction l with
  | nil => rfl
  | cons a rest ih =>
    have h := hpq a
    cases hp : p a <;> cases hq : q a <;>
      simp only [hp, hq] at h <;> simp [hp, hq] <;>
      first
        | omega
        | exact absurd h (by decide)   -- p and q both true: excluded by `hpq`

/-- The replay list has one element per entry the verdict replays: the
phase split neither drops nor duplicates. (`teardown_length` is this,
transported across the `map`.) -/
theorem replayed_length (v : Verdict) (log : List LogEntry) :
    (replayed v log).length
      = (log.filter (fun e => e.kind.replaysUnder v)).length := by
  simp only [replayed, phase1, phase2, List.length_append, List.length_reverse]
  exact phase_lengths_add v log

/-- **The books balance, under every verdict.** Replayed + discharged +
stranded is the whole stack. Under `commit`/`abort` the stranded term is
zero and this is the old two-way accounting; under `halted` the first two
are zero and the whole stack is on the inventory. Either way nothing falls
off (item 443). -/
theorem book_lengths_add (v : Verdict) (log : List LogEntry) :
    (replayed v log).length + (discharged v log).length
      + (stranded v log).length = log.length := by
  rw [replayed_length]
  exact filter_three_length (fun e => e.kind.replaysUnder v)
    (fun e => e.kind.dischargedUnder v)
    (fun e => by cases e.kind <;> cases v <;> rfl) log

end RevL.Semantics
