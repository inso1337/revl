import RevL.Semantics

/-!
# G7: derived teardown is LIFO-complete over the entries the verdict replays

DESIGN.md §4: G7 holds "by lowering" (paper Thm. 16). DESIGN.md §3.2:
"everything it does is undone — in derived, LIFO order — when it
deactivates", and the README: teardown is "LIFO over exactly the effects
that ran".

**Roadmap item 418, C3 and step 5.** "Exactly the effects that ran" is not
"every entry on the stack". `docs/design/teardown-contract.md` and
`backends/python/runtime.py` agree on a three-kind stack and a two-phase
walk under the activation's verdict:

- a `bracket` entry replays on clean unload and on abort alike;
- a `transactional` entry (item 243) is **discharged** on a clean commit,
  because the mutation is the deliverable, and replays only on abort, in
  Phase 1;
- a `compensation` entry (item 247) is discharged on a clean commit too,
  and on abort it is deferred out of the Phase-1 proof pass into the
  Phase-2 drain, so a best-effort failure can never interrupt proof
  recovery.

The pre-step-5 model had one entry kind and replayed all of it on every
teardown, which made `teardown_replays_all` *contradict* the reference.
The statements below are relative to the kind and the verdict, so they say
what revl does.

Five statements pin "exactly": completeness (every entry the verdict
replays is replayed), soundness (nothing else is), the LIFO equation, the
phase split (no compensation runs before the proof pass finishes), and
LIFO within a phase. Together with `RevL.Semantics.teardown_length` (one
replay per replaying entry) they pin the replay set and its order.
-/

namespace RevL.G7

open RevL.Semantics RevL.Syntax

/-! ### The replay rule, read off the contract's table -/

/-- The contract's "replays on clean unload" / "replays on abort" rows,
stated as a theorem so a change to `replaysUnder` has to face them
(docs/design/teardown-contract.md, "the three entry kinds, one stack"). -/
theorem replay_table :
    EntryKind.bracket.replaysUnder .commit = true ∧
    EntryKind.bracket.replaysUnder .abort = true ∧
    EntryKind.transactional.replaysUnder .commit = false ∧
    EntryKind.transactional.replaysUnder .abort = true ∧
    EntryKind.compensation.replaysUnder .commit = false ∧
    EntryKind.compensation.replaysUnder .abort = true := by
  decide

/-! ### Completeness and soundness -/

/-- Completeness, entry level: every entry the verdict replays is on the
replay list. -/
theorem replayed_complete : ∀ (v : Verdict) (log : List LogEntry) (w : LogEntry),
    w ∈ log → w.kind.replaysUnder v = true → w ∈ replayed v log := by
  intro v log w hw hr
  simp only [replayed, List.mem_append, phase1, phase2, List.mem_reverse,
    List.mem_filter]
  cases hp : w.kind.inPhase1 with
  | true =>
    refine Or.inl ⟨hw, ?_⟩
    rw [hr]
    rfl
  | false =>
    refine Or.inr ⟨hw, ?_⟩
    simp only [EntryKind.inPhase2, hp, hr, Bool.not_false, Bool.true_and]

/-- Completeness, G7 as stated: the inverse of every entry the verdict
replays is replayed. This is the corrected form of the pre-step-5
`teardown_replays_all`, which asserted the same conclusion with no
hypothesis and was therefore false of a committing activation carrying a
transactional entry. -/
theorem teardown_replays_all : ∀ (v : Verdict) (log : List LogEntry) (w : LogEntry),
    w ∈ log → w.kind.replaysUnder v = true → w.inverse ∈ teardown v log := by
  intro v log w hw hr
  exact List.mem_map_of_mem (replayed_complete v log w hw hr)

/-- Soundness, entry level: the replay list contains only stack entries,
and only ones this verdict replays. -/
theorem replayed_sound : ∀ (v : Verdict) (log : List LogEntry) (w : LogEntry),
    w ∈ replayed v log → w ∈ log ∧ w.kind.replaysUnder v = true := by
  intro v log w hw
  simp only [replayed, List.mem_append, phase1, phase2, List.mem_reverse,
    List.mem_filter, Bool.and_eq_true] at hw
  rcases hw with ⟨h, _, hr⟩ | ⟨h, _, hr⟩ <;> exact ⟨h, hr⟩

/-- Soundness, G7 as stated: teardown replays nothing that was not
registered, and nothing this verdict discharges. -/
theorem teardown_only_witnessed : ∀ (v : Verdict) (log : List LogEntry) (e : Expr),
    e ∈ teardown v log →
    ∃ w ∈ log, w.inverse = e ∧ w.kind.replaysUnder v = true := by
  intro v log e he
  simp only [teardown, List.mem_map] at he
  obtain ⟨w, hw, rfl⟩ := he
  obtain ⟨hmem, hr⟩ := replayed_sound v log w hw
  exact ⟨w, hmem, rfl, hr⟩

/-! ### The three kinds against the reference -/

/-- A clean commit discharges every transactional entry: the mutation is
the deliverable, so the inverse must NOT run (`runtime.py`,
`_Transactional.__call__`, the `frame._committed` branch). This is the row
the pre-step-5 G7 contradicted outright. -/
theorem commit_discharges_transactional (log : List LogEntry) :
    ∀ w ∈ replayed .commit log, w.kind ≠ .transactional := by
  intro w hw hk
  have hr := (replayed_sound .commit log w hw).2
  rw [hk] at hr
  exact absurd hr (by decide)

/-- A clean commit discharges every compensation too: the forward emission
was the deliverable, and best-effort cleanup on success would be wrong
(item 247, `_Compensation`). -/
theorem commit_discharges_compensation (log : List LogEntry) :
    ∀ w ∈ replayed .commit log, w.kind ≠ .compensation := by
  intro w hw hk
  have hr := (replayed_sound .commit log w hw).2
  rw [hk] at hr
  exact absurd hr (by decide)

/-- Taken together: a clean commit replays brackets and nothing else. -/
theorem commit_replays_only_brackets (log : List LogEntry) :
    ∀ w ∈ replayed .commit log, w.kind = .bracket := by
  intro w hw
  have h1 := commit_discharges_transactional log w hw
  have h2 := commit_discharges_compensation log w hw
  cases hk : w.kind
  · rfl
  · exact absurd hk h1
  · exact absurd hk h2

/-- An abort replays every transactional entry, the other half of the
`_Transactional` branch. -/
theorem abort_replays_every_transactional (log : List LogEntry) (w : LogEntry)
    (hw : w ∈ log) (hk : w.kind = .transactional) : w ∈ replayed .abort log :=
  replayed_complete .abort log w hw (by rw [hk]; rfl)

/-- A bracket replays under every SETTLING verdict: releasing an acquired
handle is always right when the activation actually settles.

Roadmap item 443 added the hypothesis. Pre-443 this quantified over every
verdict, because every verdict settled; the E-Stop is the verdict at which
it does not, and `estop_strands_the_bracket` below proves the E-Stop
counter-instance rather than leaving the weakening unexplained. The
settling case is untouched, and this is NOT a weakening of G7's
completeness theorem (`teardown_replays_all`), which was already stated
relative to `replaysUnder v` and needs no hypothesis at all. -/
theorem bracket_replays_under_every_verdict (v : Verdict) (log : List LogEntry)
    (w : LogEntry) (hw : w ∈ log) (hk : w.kind = .bracket)
    (hv : v.settles = true) :
    w ∈ replayed v log :=
  replayed_complete v log w hw (by
    rw [hk]; cases v <;> simp_all [Verdict.settles, EntryKind.replaysUnder])

/-! ### Order: the LIFO equation, the phase split, LIFO within a phase -/

/-- The LIFO equation, per phase: each phase replays the inverses of the
entries it selects, in reverse registration order. Position `i` of a phase
is its `(n-1-i)`-th selected entry (combine with `List.getElem_reverse`). -/
theorem teardown_eq_reversed_inverses : ∀ (v : Verdict) (log : List LogEntry),
    teardown v log =
      ((log.filter (fun e => e.kind.inPhase1 && e.kind.replaysUnder v)).map
        (·.inverse)).reverse ++
      ((log.filter (fun e => e.kind.inPhase2 && e.kind.replaysUnder v)).map
        (·.inverse)).reverse := by
  intro v log
  simp [teardown, replayed, phase1, phase2, List.map_append, List.map_reverse]

/-- The phase split, as the contract states it: "all Phase-1 inverses
complete before any compensation starts". The replay list is a
compensation-free prefix followed by an all-compensation suffix, so a
best-effort compensation failure can never leave a proof inverse un-run
(teardown-contract.md, "why two phases", reason 1). -/
theorem compensations_drain_after_the_proof_pass (v : Verdict)
    (log : List LogEntry) :
    ∃ proofPass drain, replayed v log = proofPass ++ drain ∧
      (∀ w ∈ proofPass, w.kind ≠ .compensation) ∧
      (∀ w ∈ drain, w.kind = .compensation) := by
  refine ⟨phase1 v log, phase2 v log, rfl, ?_, ?_⟩
  · intro w hw hk
    simp only [phase1, List.mem_reverse, List.mem_filter, Bool.and_eq_true] at hw
    rw [hk] at hw
    exact absurd hw.2.1 (by decide)
  · intro w hw
    simp only [phase2, List.mem_reverse, List.mem_filter, Bool.and_eq_true] at hw
    cases hk : w.kind
    · rw [hk] at hw; exact absurd hw.2.1 (by decide)
    · rw [hk] at hw; exact absurd hw.2.1 (by decide)
    · rfl

/-- LIFO within a phase: undoing the run order gives back a sub-sequence
of the registration order, so a phase neither reorders nor invents
entries. -/
theorem phase1_is_lifo (v : Verdict) (log : List LogEntry) :
    (phase1 v log).reverse.Sublist log := by
  simp only [phase1, List.reverse_reverse]
  exact List.filter_sublist

theorem phase2_is_lifo (v : Verdict) (log : List LogEntry) :
    (phase2 v log).reverse.Sublist log := by
  simp only [phase2, List.reverse_reverse]
  exact List.filter_sublist

/-! ### Item 443: the E-Stop column

`docs/design/443-estop.md`. An operator halt is a THIRD column in the
contract's table, not a second model. The theorems above are already
stated relative to `replaysUnder v`, so they cover `.halted` unchanged and
hold with an empty replay set — G7 does not become conditional. What the
halt needs on top is the honest accounting: the entries it did not replay
are not discharged either, they are OWED, and the inventory names every
one of them.

The halt can also arrive DURING a teardown that was already running under
`commit` or `abort`. That case is a CUT into that verdict's replay order:
what completed, the at-most-one inverse that was in flight and is
therefore AMBIGUOUS (item 440's tier, item 309's spent at-most-once
attempt), and what was never attempted. -/

/-- The E-Stop replays nothing. Not "replays the brackets and skips the
rest": a halt whose first act is to release two hundred handles is not a
halt (item 443, the guarantee). -/
theorem estop_replays_nothing (log : List LogEntry) :
    teardown .halted log = [] := by
  have h1 : log.filter
      (fun e => e.kind.inPhase1 && e.kind.replaysUnder .halted) = [] := by
    rw [List.filter_eq_nil_iff]
    intro w _
    cases w.kind <;> simp [EntryKind.replaysUnder, EntryKind.inPhase1]
  have h2 : log.filter
      (fun e => e.kind.inPhase2 && e.kind.replaysUnder .halted) = [] := by
    rw [List.filter_eq_nil_iff]
    intro w _
    cases w.kind <;> simp [EntryKind.replaysUnder, EntryKind.inPhase2,
      EntryKind.inPhase1]
  simp [teardown, replayed, phase1, phase2, h1, h2]

/-- The E-Stop discharges nothing either. Discharge RELEASES the inverse
and the witness; doing that at a halt would destroy exactly the state the
reconciliation path reads back. -/
theorem estop_discharges_nothing (log : List LogEntry) :
    discharged .halted log = [] := by
  rw [discharged, List.filter_eq_nil_iff]
  intro w _
  cases w.kind <;> simp [EntryKind.dischargedUnder]

/-- **The honest counterpart of R4.** R4 is "no residue"; the E-Stop is
"all residue, all of it reported". Every registered entry — every kind,
in registration order, none dropped — is on the halt's inventory. This is
what lets the runtime record what it did NOT unwind instead of pretending
a teardown ran. -/
theorem estop_strands_everything (log : List LogEntry) :
    stranded .halted log = log := by
  rw [stranded, List.filter_eq_self]
  intro w _
  cases w.kind <;> rfl

/-- The E-Stop counter-instance for `bracket_replays_under_every_verdict`:
the bracket row really does change in the third column, so the settling
hypothesis that theorem now carries is load-bearing and not decoration. -/
theorem estop_strands_the_bracket :
    EntryKind.bracket.replaysUnder .halted = false ∧
    EntryKind.bracket.strandedUnder .halted = true ∧
    Verdict.halted.settles = false := by decide

/-! #### The halt cut: what was in flight

`k` is how many inverses of the interrupted verdict's replay order had
recorded their completion when the latch was read. -/

/-- The inverses that provably ran before the halt. -/
def haltCompleted (v : Verdict) (k : Nat) (log : List LogEntry) : List LogEntry :=
  (replayed v log).take k

/-- The at-most-one inverse that was DISPATCHED and whose completion was
never recorded. It may or may not have landed, and the runtime says so
(`outcome: "unknown"`) rather than guessing — item 440's ambiguous tier,
reached deliberately instead of by accident. -/
def haltAmbiguous (v : Verdict) (k : Nat) (log : List LogEntry) : List LogEntry :=
  ((replayed v log).drop k).take 1

/-- The inverses the halt never attempted at all. -/
def haltUnattempted (v : Verdict) (k : Nat) (log : List LogEntry) : List LogEntry :=
  (replayed v log).drop (k + 1)

/-- The cut is a partition of the interrupted replay order: nothing is
counted twice and nothing is lost between the three books. -/
theorem halt_inventory_is_total (v : Verdict) (k : Nat) (log : List LogEntry) :
    haltCompleted v k log ++ haltAmbiguous v k log ++ haltUnattempted v k log
      = replayed v log := by
  simp only [haltCompleted, haltAmbiguous, haltUnattempted, List.append_assoc]
  rw [← List.drop_drop, List.take_append_drop, List.take_append_drop]

/-- **At most one thing is ambiguous.** The halt creates exactly the
ambiguity of the single crossing it interrupted, never a fog over the
whole stack — which is what makes the record actionable (open question 2
of item 443: the bracket whose own inverse was in flight IS this entry). -/
theorem halt_ambiguity_is_at_most_one (v : Verdict) (k : Nat)
    (log : List LogEntry) : (haltAmbiguous v k log).length ≤ 1 := by
  simp only [haltAmbiguous, List.length_take]
  omega

/-- **The books balance across the halt.** Completed + ambiguous +
unattempted + discharged + stranded is the whole stack, under every
verdict and at every cut. An entry cannot fall off the accounting by
being halted. -/
theorem halt_books_are_total (v : Verdict) (k : Nat) (log : List LogEntry) :
    (haltCompleted v k log).length + (haltAmbiguous v k log).length
      + (haltUnattempted v k log).length + (discharged v log).length
      + (stranded v log).length = log.length := by
  have h := congrArg List.length (halt_inventory_is_total v k log)
  simp only [List.length_append] at h
  have hb := book_lengths_add v log
  omega

/-! ### Non-vacuity: the verdict and the phase split are both load-bearing

One activation stack carrying all three kinds. The acquisition is
registered first, the witnessed mutation second, the compensation last, so
a single-phase LIFO walk would run the compensation FIRST, and a
verdict-blind walk would replay the mutation on a clean commit. Neither
happens. -/

/-- `w.db.open()`, an acquisition; its inverse releases the handle. -/
def acquireEntry : LogEntry := ⟨.bracket, .call "release_handle" []⟩

/-- `emit w.db.insert(row) undo w.db.delete(row)`, a witnessed mutation
(item 243). Registered after the acquisition. -/
def mutateEntry : LogEntry := ⟨.transactional, .call "db_delete" []⟩

/-- `emit w.mail.send(m) compensate w.mail.apologize(m)`, a compensation
for an emission that already crossed (item 247). Registered last. -/
def compensateEntry : LogEntry := ⟨.compensation, .call "send_apology" []⟩

/-- The activation's stack, in registration order. -/
def stack : List LogEntry := [acquireEntry, mutateEntry, compensateEntry]

/-- A clean commit runs the bracket's inverse and nothing else: the
witnessed mutation persists and the compensation never fires. A
verdict-blind teardown would have replayed all three. -/
theorem commit_runs_the_bracket_only :
    teardown .commit stack = [Expr.call "release_handle" []] := rfl

/-- An abort runs the proof pass LIFO, the mutation's inverse before the
handle release because the mutation was registered later, and only then
drains the compensation, even though the compensation was registered LAST
and a single-phase LIFO walk would have run it FIRST. -/
theorem abort_runs_the_proof_pass_then_the_drain :
    teardown .abort stack =
      [Expr.call "db_delete" [], Expr.call "release_handle" [],
       Expr.call "send_apology" []] := rfl

/-- Non-vacuity: the verdict is load-bearing. One stack, two verdicts,
different replays, so G7's conclusion turns on the rule and not on a
model that replays everything. -/
theorem verdict_is_load_bearing :
    teardown .commit stack ≠ teardown .abort stack := by
  intro h
  have h1 : (teardown Verdict.commit stack).length = 1 := rfl
  have h2 : (teardown Verdict.abort stack).length = 3 := rfl
  rw [h, h2] at h1
  exact absurd h1 (by decide)

/-- Non-vacuity: `commit_discharges_transactional` is not refusing over an
empty stack. The transactional entry IS registered, it IS on the log the
theorem quantifies over, and it is still absent from the commit replay and
present in the abort replay. -/
theorem commit_discharge_is_not_vacuous :
    mutateEntry ∈ stack ∧ mutateEntry ∉ replayed .commit stack ∧
    mutateEntry ∈ replayed .abort stack := by
  refine ⟨by simp [stack], ?_, ?_⟩
  · intro h
    exact commit_discharges_transactional stack mutateEntry h rfl
  · exact abort_replays_every_transactional stack mutateEntry (by simp [stack]) rfl


/-! ### Item 443 non-vacuity: the halt is load-bearing on a real stack -/

/-- One stack, three verdicts. The abort replays three inverses and owes
nothing; the E-Stop replays NONE and owes all three. So the third column
is not a relabelling of an existing one, and `estop_strands_everything` is
not a claim about an empty stack. -/
theorem estop_is_load_bearing :
    (teardown .abort stack).length = 3 ∧
    (teardown .halted stack).length = 0 ∧
    (discharged .halted stack).length = 0 ∧
    (stranded .halted stack).length = 3 ∧
    (stranded .abort stack).length = 0 := by
  refine ⟨rfl, rfl, rfl, rfl, rfl⟩

/-- A halt that arrives mid-abort, after the witnessed mutation's inverse
has run and with the handle release in flight: one completed, one
ambiguous, one never attempted. The cut is a real three-way split on a
real stack, not a definition with no instance. -/
theorem mid_abort_halt_cut_is_not_vacuous :
    haltCompleted .abort 1 stack = [mutateEntry] ∧
    (haltAmbiguous .abort 1 stack).length = 1 ∧
    (haltUnattempted .abort 1 stack).length = 1 ∧
    (haltCompleted .abort 1 stack).length + (haltAmbiguous .abort 1 stack).length
      + (haltUnattempted .abort 1 stack).length = 3 := by
  refine ⟨rfl, rfl, rfl, rfl⟩

end RevL.G7
