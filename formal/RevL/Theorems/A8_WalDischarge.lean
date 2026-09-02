import RevL.Lemmas.WalLemmas

/-!
# A8 — WAL commit/abort discharge across a crash cut

`formal/STATUS.md` TODO 3 calls this "a runtime state-machine
refinement": the guarantee is not about the checker, it is about what a
*fresh process* can conclude from a durable log after the old one died.
The reference is `src/revl/wal.py` (what a durable WAL is) and
`src/revl/recovery.py` (`recover`, what reading one back means). The
algebra and the small-step semantics live in the L1 farm
`RevL.Lemmas.WalLemmas`; this file is the L2 guarantee.

## The four claims

1. **L-Raise reverts** (`revert_on_failure`). A body that hits the
   failing step has the world it built rolled back to what it inherited.
   Stated over the step relation, so the log is the run's trace.
2. **A commit replays nothing** (`commit_replays_no_inverse`,
   `committed_transaction_is_retained`). A witnessed/transactional
   inverse must NOT replay on a clean commit — only an abort replays it
   (`backends/python/runtime.py`). Item 418 flags
   `G7.teardown_replays_all` as false of revl for exactly this reason;
   `commit_witness` below pins the correct behaviour on a concrete trace.
3. **The decision point** (`commit_record_is_the_decision`,
   `approved_decides_the_crash_window`, `crash_cut_converges`). Present
   is roll-forward, absent is roll-back; and once a crash point has
   decided, no later crash point of the same run moves it.
4. **At most once** (`fence_before_apply_at_every_cut`,
   `at_most_once_across_crash`). An undeclared inverse's fence is durable
   before its apply, so however the crash cuts the window, the next
   recovery run refuses it rather than applying it twice.
   `declared_idempotent_replay_free` is the other half: over the declared
   subset replay is free, because `DictWorld.apply_inverse` pops a
   referent and popping twice is popping once.

## What each theorem is worth, honestly

Item 418's finding is that a statement can be true and empty. So:

* `revert_on_failure`, `fence_before_apply_at_every_cut`,
  `at_most_once_across_crash`, `declared_idempotent_replay_free` and
  `crash_cut_converges` carry real content: each is an induction whose
  conclusion fails if a rule of the model is changed, and each has a
  witness below.
* `commit_record_is_the_decision` and `approved_decides_the_crash_window`
  are **specification-agreement** statements: they say `outcome` is the
  reference's if-chain and nothing more. Useful as a pin against drift,
  not deep.
* `outcome_trichotomy` is **definitional**: `Outcome` has three
  constructors. It is registered because "never a mixed state" is a claim
  people make about this system and it should be visible what that claim
  is worth — the content is in `crash_cut_converges` and
  `committed_transaction_is_retained`, not here.

## What "never a mixed state" does and does not say

It says the **verdict** is single. It deliberately does NOT say the
per-seq dispositions are uniform, because in the reference they are not,
by design: `_roll_back` returns `dischargedSkipped` (committed, retained)
beside `transactionalRolledBack` in one verdict.
`mixed_disposition_admitted` exhibits such a log, so the theorem set is
checked against the machine revl has rather than a tidier one.

## Crash cuts this model covers, and the one it cannot

A crash is a prefix — of the durable log, or of the run. The reference's
own reading of a torn line is what makes that exact: a torn trailing
record is discarded, and a torn fence line "implies the apply never ran"
(`backends/python/replay.py`). Covered:

* a crash **between the durable write and the effect** —
  `fence_before_apply_at_every_cut`, quantified over every cut `n`;
* a crash **during teardown** — the abort-then-crash trace
  (`abortThenCrash`), fence durable, no `aborted` completion record;
* a crash **inside the approved-to-discharged window** —
  `approved_decides_the_crash_window`.

**Not covered, and not claimed.** A *witnessed* mutation logs its
descriptor AFTER the forward extern returns `Ok`
(`backends/python/emit.py`), so a crash between the mutation and its
record leaves a mutation with no record at all. Every theorem here is
relative to the durable log; a mutation that never reached the log is
outside all of them. The `SemStep.witnessed` rule collapses that window
into one step, so this model cannot express it. Durability itself is also
a floor rather than a theorem: `WAFrom` orders the fence append before
the apply, and that `fsync` reaches the platter is a host obligation.
-/

namespace RevL.A8

open RevL.Lemmas

/-! ## L-Raise: the abort reverts what the body built -/

/-- **A8 — revert on failure.** A body that runs from world `w₀` to
L-Raise's failing step, taking only witnessed mutations, has its world
restored exactly by replaying the log it accumulated. The log is the
trace: `SemStep.witnessed` is what appends the descriptor, so this is a
statement about the effects that actually ran. -/
theorem revert_on_failure {b : Body} {w₀ w₁ : World} {L : Log}
    (hs : SemSteps ⟨b, w₀, []⟩ ⟨.fail, w₁, L⟩)
    (hfresh : ∀ s ∈ logSeqs L, s ∉ w₀)
    (hno : emittedSeqs L = []) :
    replay w₁ (rollbackReplay L) = w₀ := by
  obtain ⟨M, hL, hw, hM⟩ := steps_trace hs
  simp only [List.nil_append] at hL
  subst hL
  have hw' : w₁ = (logSeqs L).reverse ++ w₀ := hw
  rw [rollbackReplay_sem hM, replay_eq_filter, hw', List.filter_append]
  have hdisj : ∀ s ∈ emittedSeqs L, s ∉ witnessedSeqs L := by
    intro s hsm; rw [hno] at hsm; cases hsm
  have hhead : (logSeqs L).reverse.filter (fun t => !(memSeq t (witnessedSeqs L))) = [] := by
    rw [List.filter_reverse, logSeqs_filter_gen hM (fun _ h => h) hdisj, hno]
    rfl
  have htail : w₀.filter (fun t => !(memSeq t (witnessedSeqs L))) = w₀ :=
    filter_none_out fun t ht hin => hfresh t (witnessedSeqs_sub hin) ht
  rw [hhead, htail]
  rfl

/-- Every trace reads back as an abort until the runtime says otherwise:
no body step writes `commit-approved` or the terminal marker, so a run
cut short by a crash rolls back. -/
theorem trace_reads_back_as_abort {L : Log} (h : SemLog L) :
    outcome L = .rolledBack := by
  have key : ∀ {M : Log}, SemLog M →
      hasForkFrozen M = false ∧ hasComplete M = false ∧ hasApproved M = false := by
    intro M hM
    induction hM with
    | nil => exact ⟨rfl, rfl, rfl⟩
    | descriptor _ ih =>
      simpa [hasForkFrozen, hasComplete, hasApproved, hasRec] using ih
    | emitted _ ih =>
      simpa [hasForkFrozen, hasComplete, hasApproved, hasRec] using ih
  obtain ⟨hfork, hcomp, happ⟩ := key h
  simp [outcome, hfork, hcomp, happ]

/-! ## A commit replays nothing -/

/-- **A committed transaction is never rolled back.** The reference calls
this "THE central safety claim": a durable `discharge` record retains its
referent, and the roll-back walk re-issues nothing for it. -/
theorem committed_transaction_is_retained {L : Log} {s : Seq} {e : Entry} {b : Bool} :
    s ∈ dischargedSeqs L → reissued L (.descriptor s e b) = [] := by
  intro h
  have hm : memSeq s (dischargedSeqs L) = true := memSeq_iff.mpr h
  cases e <;> simp only [reissued, hm, if_true]

/-- **A commit rolls nothing back.** `_roll_forward`, the item 245 window
path and `_fork_retired` all return without touching the world: only the
abort verdict replays. This is the behaviour `G7.teardown_replays_all`
gets wrong (item 418): a witnessed inverse replays on abort, never on a
clean commit. -/
theorem commit_replays_no_inverse {L : Log} (h : outcome L ≠ .rolledBack) :
    replayed L = [] := by
  cases hv : outcome L with
  | rolledForward => simp [replayed, hv]
  | forkRetired => simp [replayed, hv]
  | rolledBack => exact absurd hv h

/-! ## The decision point -/

/-- **Never a mixed verdict.** Definitional: `Outcome` has exactly three
constructors and `outcome` returns one of them. Registered so the claim's
weight is visible — the content is in `crash_cut_converges` and
`committed_transaction_is_retained`. -/
theorem outcome_trichotomy (L : Log) :
    (outcome L = .rolledForward ∨ outcome L = .rolledBack ∨ outcome L = .forkRetired)
    ∧ ¬(outcome L = .rolledForward ∧ outcome L = .rolledBack)
    ∧ ¬(outcome L = .rolledForward ∧ outcome L = .forkRetired)
    ∧ ¬(outcome L = .rolledBack ∧ outcome L = .forkRetired) := by
  refine ⟨?_, ?_, ?_, ?_⟩
  · cases h : outcome L with
    | rolledForward => exact Or.inl rfl
    | rolledBack => exact Or.inr (Or.inl rfl)
    | forkRetired => exact Or.inr (Or.inr rfl)
  all_goals (rintro ⟨h1, h2⟩; rw [h1] at h2; exact Outcome.noConfusion h2)

/-- `outcome` in iff form, so the crash-cut arguments below are about
record presence rather than about an if-chain. -/
theorem outcome_forward_iff {L : Log} (hf : hasForkFrozen L = false) :
    outcome L = .rolledForward ↔ (hasComplete L = true ∨ hasApproved L = true) := by
  cases hc : hasComplete L <;> cases ha : hasApproved L <;> simp [outcome, hf, hc, ha]

theorem outcome_back_iff {L : Log} (hf : hasForkFrozen L = false) :
    outcome L = .rolledBack ↔ (hasComplete L = false ∧ hasApproved L = false) := by
  cases hc : hasComplete L <;> cases ha : hasApproved L <;> simp [outcome, hf, hc, ha]

theorem hasRec_absent {r : Rec} {L : Log} : hasRec r L = false ↔ r ∉ L := by
  cases h : hasRec r L
  · exact ⟨fun _ hm => by rw [hasRec_iff.mpr hm] at h; exact Bool.noConfusion h,
           fun _ => rfl⟩
  · exact ⟨fun hc => Bool.noConfusion hc, fun hn => absurd (hasRec_iff.mp h) hn⟩

/-- **The decision converges.** Once a crash point rolls forward, every
later crash point of the same run rolls forward too: the decision records
are only ever appended, and the durable state at successive crash points
is a growing prefix. A committed session cannot be un-committed by
letting the process live a little longer.

The `hasForkFrozen` hypothesis is not decoration — a `fork-frozen` record
appearing later WOULD move the verdict, because `recover` tests it first
(item 250, Decision 5). -/
theorem crash_cut_converges {L : Log} (hf : hasForkFrozen L = false) :
    ∀ {n m : Nat}, n ≤ m →
      outcome (L.take n) = .rolledForward → outcome (L.take m) = .rolledForward := by
  intro n m hnm h
  have hfn : hasForkFrozen (L.take n) = false :=
    hasRec_false_mono (fun _ hx => mem_of_mem_take hx) hf
  have hfm : hasForkFrozen (L.take m) = false :=
    hasRec_false_mono (fun _ hx => mem_of_mem_take hx) hf
  rw [outcome_forward_iff hfn] at h
  rw [outcome_forward_iff hfm]
  exact h.imp (hasRec_mono fun _ hx => mem_take_mono hnm hx)
              (hasRec_mono fun _ hx => mem_take_mono hnm hx)

/-- **The commit record is the decision.** Specification agreement: with
no frozen fork, the verdict is roll-forward exactly when the terminal
marker or the session's `commit-approved` record is durable, and
roll-back exactly when neither is. -/
theorem commit_record_is_the_decision {L : Log} (hf : hasForkFrozen L = false) :
    (outcome L = .rolledForward ↔
      (Rec.activationComplete ∈ L ∨ Rec.commitApproved ∈ L))
    ∧ (outcome L = .rolledBack ↔
      (Rec.activationComplete ∉ L ∧ Rec.commitApproved ∉ L)) := by
  refine ⟨?_, ?_⟩
  · rw [outcome_forward_iff hf]
    exact or_congr hasRec_iff hasRec_iff
  · rw [outcome_back_iff hf]
    exact and_congr hasRec_absent hasRec_absent

/-- **The crash window (item 245, Decision 3).** The interesting cut is
the one where the terminal marker never made it. There `commit-approved`
alone decides: present, the session COMMITTED and recovery replays no
inverse; absent, it aborted and the descriptors roll back. "The durable
approval, not the discharge record, is the commit proof." -/
theorem approved_decides_the_crash_window {L : Log}
    (hf : hasForkFrozen L = false) (hc : Rec.activationComplete ∉ L) :
    (Rec.commitApproved ∈ L → outcome L = .rolledForward)
    ∧ (Rec.commitApproved ∉ L → outcome L = .rolledBack) := by
  have hd := commit_record_is_the_decision hf
  exact ⟨fun h => hd.1.mpr (Or.inr h), fun h => hd.2.mpr ⟨hc, h⟩⟩

/-! ## At most once, across the crash cut -/

/-- **The fence is durable before the effect, at every crash point.**
This is the window the brief names — a crash BETWEEN the durable write
and the effect. It has no interior a crash can land in and lose the
fence: whatever `n` the crash cuts at, if an undeclared inverse has
observably fired then its `replay-fence` is already in the durable log.
The reference's own reading of a torn line is what makes the prefix model
exact: a torn fence line "implies the apply never ran". -/
theorem fence_before_apply_at_every_cut {idem : Seq → Bool} {r : Run} {s : Seq}
    (n : Nat) (hwa : WAFrom idem [] r) (hid : idem s = false)
    (hfired : s ∈ fired (r.take n)) :
    Rec.replayFence s ∈ durable (r.take n) := by
  rcases fence_durable_of_fired (waFrom_take n hwa) hid hfired with h | h
  · cases h
  · exact h

/-- **An inverse is never applied twice with observable effect.** Take
any crash point at which an undeclared inverse has already fired. Its
fence is durable there, so the recovery run reading that cut re-issues
nothing for it — reported as `fenced-residue` with outcome unknown, or
resolved by a durable `aborted` record, but in neither case applied
again. With `fence_before_apply_at_every_cut` this is item 309 §3a's
at-most-once, across abort-then-crash and any number of recovery runs. -/
theorem at_most_once_across_crash {idem : Seq → Bool} {r : Run} {s : Seq}
    (n : Nat) (hwa : WAFrom idem [] r) (hid : idem s = false)
    (hfired : s ∈ fired (r.take n))
    (hu : SeqUnique (durable (r.take n)))
    (hd : Rec.descriptor s .transactional false ∈ durable (r.take n)) :
    s ∉ rollbackReplay (durable (r.take n)) := by
  intro hmem
  obtain ⟨rc, hrc, hs⟩ := List.mem_flatMap.mp hmem
  have hseq : recSeq rc = some s := reissued_recSeq hs
  have hrc' : rc = Rec.descriptor s .transactional false :=
    hu rc hrc _ hd s hseq rfl
  subst hrc'
  have hfen : memSeq s (fencedSeqs (durable (r.take n))) = true :=
    memSeq_iff.mpr (fencedSeqs_mem (fence_before_apply_at_every_cut n hwa hid hfired))
  simp only [reissued, hfen] at hs
  split at hs
  · cases hs
  · simp at hs

/-- **A declared-idempotent inverse replays freely.** `revl recover` is
itself idempotent over the declared subset (item 309 §3a): running the
same replay set against the world a second time changes nothing, because
`DictWorld.apply_inverse` pops the referent the call names and popping
twice is popping once. -/
theorem declared_idempotent_replay_free (w : World) (ss : List Seq) :
    replay (replay w ss) ss = replay w ss :=
  replay_fix fun _ hs => replay_erases hs

/-- A world in which an inverse is *not* idempotent: every application
counts (an append, a charge, a delta-shaped restore). -/
def applyCount (n : Nat) (_ : Seq) : Nat := n + 1

/-- **The idempotence declaration is load-bearing.** Applying an
undeclared inverse twice is observable in general, so at-most-once is not
decoration: it is the only thing between a crash in the fence/apply
window and a double charge. `DictWorld`'s set model is why the *declared*
subset gets free replay and the undeclared subset does not. -/
theorem double_apply_observable :
    applyCount (applyCount 0 7) 7 ≠ applyCount 0 7 := by decide

/-! ## Non-vacuity

Item 418's bar: concrete traces the hypotheses admit, and concrete traces
they exclude. Compare `RevL.CrossTier.annotation_necessary`. -/

/-- Every re-issued inverse succeeds (243 rule 6's oracle fixed to
success, so the witnesses isolate the state machine). -/
def okAll : Seq → Bool := fun _ => true

/-- A clean abort: the descriptor for seq 7 is durable, nothing else. -/
def cleanAbort : Log := [.descriptor 7 .transactional false]

/-- Item 309 §3a's headline — abort-then-crash. Phase 1 fenced seq 7 and
the process died before the `aborted` completion record. -/
def abortThenCrash : Log :=
  [.descriptor 7 .transactional false, .replayFence 7]

/-- The same abort, completed: the `aborted` record proves Phase 1 ran
this fenced inverse's apply to completion. -/
def abortCompletedLog : Log :=
  [.descriptor 7 .transactional false, .replayFence 7, .aborted]

/-- The same activation, committed inside the item 245 window: a durable
`discharge` and `commit-approved`, terminal marker still missing. -/
def committedSession : Log :=
  [.descriptor 7 .transactional false, .discharge [7], .commitApproved]

/-- **The crash-cut witness.** Four logs at the same seq, differing only
in which records became durable:

* a clean abort re-issues the inverse and reports nothing owed;
* the abort-then-crash cut re-issues **nothing** — the fence excludes the
  second apply — and reports seq 7 as residue rather than claiming it ran;
* the completed abort re-issues nothing **and** reports nothing, because
  the `aborted` record makes the outcome known;
* the committed session rolls nothing back at all.

Lines two and three are the point: the hypotheses genuinely exclude a
double apply, and they distinguish "outcome unknown" from "clean" instead
of collapsing both into silence. -/
theorem crash_cut_witness :
    outcome cleanAbort = .rolledBack
    ∧ rollbackReplay cleanAbort = [7]
    ∧ reported cleanAbort okAll = []
    ∧ outcome abortThenCrash = .rolledBack
    ∧ rollbackReplay abortThenCrash = []
    ∧ reported abortThenCrash okAll = [7]
    ∧ outcome abortCompletedLog = .rolledBack
    ∧ rollbackReplay abortCompletedLog = []
    ∧ reported abortCompletedLog okAll = []
    ∧ outcome committedSession = .rolledForward
    ∧ replayed committedSession = [] := by decide

/-- Two witnessed mutations, run to a clean commit rather than to the
failing step. -/
def committedTrace : Log :=
  [.descriptor 1 .transactional true, .descriptor 2 .transactional true,
   .discharge [1, 2], .commitApproved, .activationComplete]

/-- **A witnessed inverse does not replay on a clean commit.** Item 418
flags `G7.teardown_replays_all` as false of revl for exactly this reason
(`backends/python/runtime.py`): the transactional inverse is discharged
by the commit, not replayed by it. The abort path replays; the commit
path does not. Here that is a computation on a concrete trace, and the
`discharge` record is the distinction item 418's step 5 asks the log to
carry. -/
theorem commit_witness :
    outcome committedTrace = .rolledForward
    ∧ replayed committedTrace = []
    ∧ rollbackReplay committedTrace = [] := by decide

/-- One seq committed and retained, another rolled back, in one abort
verdict. -/
def mixedAbort : Log :=
  [.descriptor 1 .transactional false, .descriptor 2 .transactional false,
   .discharge [1]]

/-- **Per-seq heterogeneity is admitted, and reported clean.** The
reference builds exactly this report — `dischargedSkipped` beside
`transactionalRolledBack` — so `outcome_trichotomy`'s "never mixed" is a
claim about the verdict and must not be read as a claim about the
dispositions. This witness pins that reading: the model admits the mixed
log, replays only the undischarged seq, and owes nothing. -/
theorem mixed_disposition_admitted :
    outcome mixedAbort = .rolledBack
    ∧ rollbackReplay mixedAbort = [2]
    ∧ reported mixedAbort okAll = [] := by decide

/-- A body of two witnessed mutations that hits the failing step. -/
def revertBody : Body := .witnessed 1 true (.witnessed 2 true .fail)

/-- The trace `revertBody` accumulates. -/
def revertLog : Log :=
  [.descriptor 1 .transactional true, .descriptor 2 .transactional true]

/-- **The revert witness.** The step relation genuinely takes this run —
the log is its trace, not a hand-written input — the body is stuck at
`fail`, and replaying that trace puts the world back exactly where the
body found it. `Body.done` and `Body.fail` have no step rule, so this is
a relation with content rather than a function ignoring its argument. -/
theorem revert_witness : SemSteps ⟨revertBody, [], []⟩ ⟨.fail, [2, 1], revertLog⟩ :=
  .step .witnessed (.step .witnessed .refl)

theorem revert_witness_restores :
    replay [2, 1] (rollbackReplay revertLog) = [] := by decide

end RevL.A8
