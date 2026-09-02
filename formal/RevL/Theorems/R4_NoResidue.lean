import RevL.Lemmas.WalLemmas

/-!
# R4 — no residue, and the residue surface is exactly what is reported

`formal/STATUS.md` TODO 3. The claim R4 makes is not "an abort is clean";
revl never claims that, because an emission that already crossed the
boundary cannot be un-crossed. The claim is the honest one the WAL header
sentence makes:

> "closure-only boundary inverses are reported as residue, never silently
> claimed to have run" (`src/revl/wal.py`, `WAL_GUARANTEE`).

So R4 has two halves and this file proves both as one equation:

* **soundness** — everything the abort leaves behind is reported;
* **completeness** — everything reported is really still out there.

`residue_is_exactly_what_remains` states them together:

```
replay w₁ (rollbackReplay L) = (reported L ok).reverse ++ w₀
```

The world after the abort is the world before the body ran, plus exactly
the reported residue. Nothing silently retained, nothing falsely claimed.

## Why this is stated over a semantics, not over a log

Roadmap item 418 is right that a theorem over a *fabricated* log says
nothing about the effects that actually ran. Every theorem here is stated
over `RevL.Lemmas.SemSteps`: a small-step relation in which taking an
effect step is what appends its record and creates its referent. The log
is the trace of the run, `w₁` is the world the run actually built, and
`L` is not an input anyone chose.

`RevL.Lemmas` documents the fragment's limits: five body forms, a
`witnessed` step that appends its descriptor and mutates atomically
(collapsing a real window the reference has), and no cascading abort,
compensation drain or escrow.

## Scope

* Stated for the ABORT path (`outcome L = .rolledBack`, which every trace
  satisfies since no step writes `commit-approved` or the terminal
  marker). The roll-forward window's `flush-residue` is a different
  surface and is not modelled.
* `hfresh` and `hdisj` are the "one strictly increasing seq space per
  session WAL" invariant of `replay._next_seq`: a body's referents are
  new, and a seq is a mutation or a crossing, never both.
-/

namespace RevL.R4

open RevL.Lemmas

/-- Every re-issued inverse succeeds. 243 rule 6 makes the inverse
fallible, and `dispose` carries that as an oracle; the theorems here fix
it to success so they isolate the state machine from the outside world.
A failed re-issue is `Residue.restoreFailed`, already in the surface. -/
def okAll : Seq → Bool := fun _ => true

/-! ## The main equation -/

/-- **R4.** Run a body from world `w₀` until it hits L-Raise's failing
step, then let the abort replay the log it accumulated. What is left is
`w₀` — the world the body inherited — plus **exactly** the seqs the
runtime reports as residue, and nothing else.

Read left to right it is completeness: every reported seq really is still
out. Read right to left it is soundness: everything still out above `w₀`
is reported. This is what `G8.boundary_enumerates_emissions` /
`boundary_only_declared` do for the static surface, done for the dynamic
one. -/
theorem residue_is_exactly_what_remains {b : Body} {w₀ w₁ : World} {L : Log}
    (hs : SemSteps ⟨b, w₀, []⟩ ⟨.fail, w₁, L⟩)
    (hfresh : ∀ s ∈ logSeqs L, s ∉ w₀)
    (hdisj : ∀ s ∈ emittedSeqs L, s ∉ witnessedSeqs L) :
    replay w₁ (rollbackReplay L) = (reported L okAll).reverse ++ w₀ := by
  obtain ⟨M, hL, hw, hM⟩ := steps_trace hs
  simp only [List.nil_append] at hL
  subst hL
  have hw' : w₁ = (logSeqs L).reverse ++ w₀ := hw
  show replay w₁ (rollbackReplay L) = (reported L (fun _ => true)).reverse ++ w₀
  rw [rollbackReplay_sem hM, replay_eq_filter, hw', List.filter_append,
    reported_sem hM]
  congr 1
  · rw [List.filter_reverse]
    exact congrArg _ (logSeqs_filter_gen hM (fun _ h => h) hdisj)
  · refine filter_none_out fun t ht hin => ?_
    exact hfresh t (witnessedSeqs_sub hin) ht

/-- **No residue when nothing crossed.** A body whose every effect was
witnessed reverts EXACTLY: the abort restores the world it started in and
the runtime reports nothing owed. This is R4's headline — "after an abort
completes, no witnessed effect remains un-discharged" — and it is a
consequence of the equation above rather than a separate claim. -/
theorem abort_leaves_no_residue {b : Body} {w₀ w₁ : World} {L : Log}
    (hs : SemSteps ⟨b, w₀, []⟩ ⟨.fail, w₁, L⟩)
    (hfresh : ∀ s ∈ logSeqs L, s ∉ w₀)
    (hno : emittedSeqs L = []) :
    replay w₁ (rollbackReplay L) = w₀ ∧ reported L okAll = [] := by
  obtain ⟨M, hL, _, hM⟩ := steps_trace hs
  simp only [List.nil_append] at hL
  subst hL
  have hrep : reported L okAll = [] := by
    show reported L (fun _ => true) = []
    rw [reported_sem hM]; exact hno
  refine ⟨?_, hrep⟩
  have hdisj : ∀ s ∈ emittedSeqs L, s ∉ witnessedSeqs L := by
    intro s hsm; rw [hno] at hsm; cases hsm
  rw [residue_is_exactly_what_remains hs hfresh hdisj, hrep]
  rfl

/-! ## The two halves, spelled out -/

/-- Completeness: a reported seq really is still out in the world. The
report is not padding. -/
theorem residue_complete {b : Body} {w₀ w₁ : World} {L : Log} {s : Seq}
    (hs : SemSteps ⟨b, w₀, []⟩ ⟨.fail, w₁, L⟩)
    (hfresh : ∀ s ∈ logSeqs L, s ∉ w₀)
    (hdisj : ∀ s ∈ emittedSeqs L, s ∉ witnessedSeqs L) :
    s ∈ reported L okAll → s ∈ replay w₁ (rollbackReplay L) := by
  intro h
  rw [residue_is_exactly_what_remains hs hfresh hdisj]
  exact List.mem_append.mpr (Or.inl (List.mem_reverse.mpr h))

/-- Soundness: anything still out that the body itself created is
reported. Nothing is silently retained. -/
theorem residue_sound {b : Body} {w₀ w₁ : World} {L : Log} {s : Seq}
    (hs : SemSteps ⟨b, w₀, []⟩ ⟨.fail, w₁, L⟩)
    (hfresh : ∀ s ∈ logSeqs L, s ∉ w₀)
    (hdisj : ∀ s ∈ emittedSeqs L, s ∉ witnessedSeqs L) :
    s ∈ replay w₁ (rollbackReplay L) → s ∈ w₀ ∨ s ∈ reported L okAll := by
  intro h
  rw [residue_is_exactly_what_remains hs hfresh hdisj] at h
  exact (List.mem_append.mp h).symm.imp id List.mem_reverse.mp

/-! ## Non-vacuity

Item 418's bar: a claim needs a witness. Two runs the semantics genuinely
takes, differing in one step, with opposite verdicts. -/

/-- Two witnessed mutations, then the failing step. -/
def txnBody : Body := .witnessed 1 true (.witnessed 2 true .fail)

/-- The same, with the second step a one-way boundary crossing. -/
def emitBody : Body := .witnessed 1 true (.emit 2 .fail)

/-- The trace `txnBody` accumulates. -/
def txnLog : Log :=
  [.descriptor 1 .transactional true, .descriptor 2 .transactional true]

/-- The trace `emitBody` accumulates. -/
def emitLog : Log :=
  [.descriptor 1 .transactional true, .effect 2 true false]

/-- The step relation really takes these runs: the logs above are traces,
not hand-written inputs. -/
theorem txn_run : SemSteps ⟨txnBody, [], []⟩ ⟨.fail, [2, 1], txnLog⟩ :=
  .step .witnessed (.step .witnessed .refl)

theorem emit_run : SemSteps ⟨emitBody, [], []⟩ ⟨.fail, [2, 1], emitLog⟩ :=
  .step .witnessed (.step .emit .refl)

/-- **The no-crossing hypothesis is load-bearing.** The two runs differ in
exactly one step. The witnessed one reverts to the empty world and owes
nothing. The emitting one does NOT revert — referent 2 is still out — and
the runtime says so, naming exactly that seq.

So `abort_leaves_no_residue` is not decoration: drop its hypothesis and
its conclusion is false, and `residue_is_exactly_what_remains` is what
holds instead. Compare `RevL.CrossTier.annotation_necessary`. -/
theorem residue_necessary :
    -- admitted, and exact: every effect witnessed
    rollbackReplay txnLog = [1, 2]
    ∧ replay [2, 1] (rollbackReplay txnLog) = []
    ∧ reported txnLog okAll = []
    -- excluded from exactness, and honestly reported instead
    ∧ rollbackReplay emitLog = [1]
    ∧ replay [2, 1] (rollbackReplay emitLog) = [2]
    ∧ reported emitLog okAll = [2]
    ∧ replay [2, 1] (rollbackReplay emitLog) ≠ [] := by decide

/-- An emission is never re-issued: it has no inverse. The abort drops it
from the replay set and moves it to the residue surface rather than
pretending a dead closure ran. -/
theorem emission_is_not_replayed : (2 : Seq) ∉ rollbackReplay emitLog := by decide


/-- **Non-vacuity for the side conditions** (roadmap item 418, step 8).
Every R4 theorem carries `hfresh` (the trace's seqs are new to the world
it started in) and `hdisj` (nothing is both emitted and witnessed). Both
hold at the two runs above, so the theorems are not stated over an
unreachable pair of hypotheses; and the emitted set is empty on one run
and non-empty on the other, which is what makes
`abort_leaves_no_residue`'s `hno` a real dividing line rather than a
condition every trace meets. -/
theorem r4_side_conditions_are_inhabited :
    (∀ s ∈ logSeqs txnLog, s ∉ ([] : World)) ∧
    (∀ s ∈ emittedSeqs txnLog, s ∉ witnessedSeqs txnLog) ∧
    emittedSeqs txnLog = [] ∧
    (∀ s ∈ logSeqs emitLog, s ∉ ([] : World)) ∧
    (∀ s ∈ emittedSeqs emitLog, s ∉ witnessedSeqs emitLog) ∧
    emittedSeqs emitLog ≠ [] := by decide

end RevL.R4
