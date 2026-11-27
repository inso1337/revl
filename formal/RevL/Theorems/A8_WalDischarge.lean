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
   (`backends/python/runtime.py`). Item 418 flagged the pre-step-5
   `G7.teardown_replays_all` as false of revl for exactly this reason;
   G7 is now stated over the entry kind and the verdict
   (`RevL.Semantics.EntryKind`), and `commit_witness` below pins the same
   behaviour on a concrete trace.
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
5. **Deferred emissions are dropped or discharged**
   (`abort_drops_deferred`, `abort_run_drops_deferred`,
   `commit_discharges_deferred`, `commit_run_queue_empty`,
   `commit_run_discharges_deferred`). The abort step, and every run that
   settles on abort, drops the session's deferral queue with zero
   crossings. The commit step fires each queued entry once, FIFO, leaving
   a `flushed` record for each; every run that settles on commit empties
   the queue and appends to the crossing list the entries queued at the
   start and then those the run enqueued, in that order, with one
   `flushed` record each, in the same order, after `commit-approved`.
   The statements are inversions of the step relation, not a fold of a
   result function. This is issue #2740's
   first slice: the recovery view already dropped a deferred emission
   with no `commit-approved`; the in-process rule was the unstated half.
6. **A fire that raises is owed, and the flush goes on**
   (`commit_flush_is_discharged_or_owed`). The roll-forward window's
   `flush-residue` surface: in a commit flush where some emissions' host
   bodies raise, every other queued entry still flushes and the commit
   still completes, each raised entry is written as `flush-residue` and
   not as crossed, and a recovery run reads exactly those entries as owed
   and the rest as flushed (`deferClass`). A cut inside the flush reads
   the unflushed entries as owed, not attempted.
7. **The crash window of a witnessed mutation**
   (`window_log_is_the_log_before_the_mutation`, `crash_window_recovery`,
   `orphan_is_invisible_to_recovery`, `window_breaks_exact_residue`,
   `split_complete_is_sem`). `SplitStep` splits the witnessed step into the
   mutation and its record. A split run that is not cut inside the window
   collapses to a `SemSteps` run (`split_complete_is_sem`), and every
   `SemSteps` run is one split run (`semSteps_has_split`), so every theorem
   above stands. The step `mutate` changes the world and leaves the log
   alone, so the log at a cut inside the window is the log at the cut
   before it: recovery cannot tell that the extern ran. It reverts
   everything the log names and leaves exactly the pending referent, which
   it neither replays nor reports. With no emission in the log, that is
   where R4's exact-residue equation stops holding.

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

**The two windows the first version left open.** A *witnessed* mutation
logs its descriptor AFTER the forward extern returns `Ok`
(`backends/python/emit.py`), so a crash between the mutation and its
record leaves a mutation with no record at all. The theorems over `SemStep`
are relative to the durable log, and `SemStep.witnessed` collapses that
window into one step. `SplitStep` states it (claim 7). What recovery can
conclude from a log that lacks the record is nothing about that referent:
it is outside `rollbackReplay` and outside `reported`.
`commit_discharges_deferred` fires every queued entry, while the
reference's flush is fallible and writes `flush-residue` for an entry that
raised. `FrameStepR.commitRaising` states that flush (claim 6).

**Still not covered.** Cascading abort, escrow, a flush that raises more
than once on the same entry, a `flushed` record that itself fails to write
after a successful fire (the runtime then lists the entry as crossed and
also records `flush-residue`, where `commitRaising` excludes a raising
entry from the crossings), positional drop (an emission queued after the
last `commit-approved` of a WAL that carries several runs), an E-Stop
between the extern returning and the registration (the emitted call site
checks the halt inside the registration, so the window can also be closed by
a halt rather than a crash), and the split of an emission's own crossing and
record, which `SplitStep.emit` keeps atomic. Durability
itself is also a floor rather than a theorem: `WAFrom` orders the fence
append before the apply, and that `fsync` reaches the platter is a host
obligation.
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
abort verdict replays. This is the behaviour the pre-step-5
`G7.teardown_replays_all` got wrong (item 418): a witnessed inverse
replays on abort, never on a clean commit. `RevL.G7` now agrees, over the
three-kind stack. -/
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
flagged the pre-step-5 `G7.teardown_replays_all` as false of revl for
exactly this reason
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

/-! ## The in-process deferral queue: abort drops it, commit fires it

The recovery view already DROPS a deferred emission with no
`commit-approved` record (`dispose`'s `.dropped` case). What was missing
is the in-process statement the runtime actually performs
(`backends/python/runtime.py`): `begin_abort` drops the session's
deferral queue with zero crossings, and the commit's `_flush` fires each
queued entry FIFO. `FrameStep.abort` and `FrameStep.commit` are those two
verdicts. The theorems below are INVERSIONS of the step relation: from an
arbitrary step that settled on a verdict, what the queue and the crossing
list must be. They do not unfold a result function written to match the
conclusion.

Two facts about the model are used throughout. A settled frame takes no
step (every `FrameStep` constructor requires verdict `none`), so a run
that reaches a verdict stops there; and only the commit step appends to
the crossing list, so a run that settles on abort can only have dropped
its queue. -/

/-- A settled frame takes no step: every `FrameStep` constructor is
defined from a frame whose verdict is `none`. -/
theorem frameStep_settled_impossible {f f' : Frame} (h : FrameStep f f')
    {v : SessionVerdict} (hv : f.verdict = some v) : False := by
  cases h <;> cases hv

/-- A run from a settled frame moves nowhere: no `FrameStep` starts at a
frame whose verdict has been decided. -/
theorem frameSteps_settled_eq {f f' : Frame} (h : FrameSteps f f')
    {v : SessionVerdict} (hv : f.verdict = some v) : f' = f := by
  induction h with
  | refl => rfl
  | @step a b c hs _ _ =>
    exact False.elim (frameStep_settled_impossible hs hv)

/-- **An abort step drops the deferral queue and crosses nothing.** Any
step that settles on abort has emptied the queue, left the crossing list
exactly as the body run left it, and written the `aborted` completion
record. It cannot have been a commit, and no other step settles a
verdict. -/
theorem abort_drops_deferred {f f' : Frame} (h : FrameStep f f')
    (hv : f'.verdict = some .abort) :
    f'.queue = [] ∧
    f'.fired = f.fired ∧
    f'.config.log = f.config.log ++ [.aborted] := by
  cases h with
  | body _ => simp at hv
  | defer => simp at hv
  | commit => simp at hv
  | abort => exact ⟨rfl, rfl, rfl⟩

/-- **Every abort a run settles fires nothing.** A `FrameSteps` run that
ends on the abort verdict, from an unsettled frame, leaves the queue empty
and the crossing list exactly as the run found it. The run cannot contain
a commit: a commit settles the verdict, and nothing moves a settled
frame. -/
theorem abort_run_drops_deferred {f f' : Frame} (h : FrameSteps f f')
    (h0 : f.verdict = none) (hv : f'.verdict = some .abort) :
    f'.queue = [] ∧ f'.fired = f.fired := by
  induction h with
  | refl => simp [h0] at hv
  | step hs hrest ih =>
    cases hs with
    | body _ => exact ih rfl hv
    | defer => exact ih rfl hv
    | commit =>
      have hc := frameSteps_settled_eq hrest (v := .commit) rfl
      rw [hc] at hv
      simp at hv
    | abort =>
      have hc := frameSteps_settled_eq hrest (v := .abort) rfl
      subst hc
      exact ⟨rfl, rfl⟩

/-- **A commit step fires the deferral queue, each entry once.** Any step
that settles on commit has emptied the queue, appended every entry to the
crossing list in FIFO order, and written `commit-approved` BEFORE the
fires, one `flushed` record per entry, and the terminal marker after them.
So "moved to discharged" is durable: each queued seq is in the crossing
list and names its own `flushed` record. A fire that raised is the
roll-forward `flush-residue` window, which `FrameStepR.commitRaising` and
`commit_flush_is_discharged_or_owed` state. -/
theorem commit_discharges_deferred {f f' : Frame} (h : FrameStep f f')
    (hv : f'.verdict = some .commit) :
    f'.queue = [] ∧
    f'.fired = f.fired ++ f.queue ∧
    (∀ s ∈ f.queue, s ∈ f'.fired) ∧
    (∀ s ∈ f.queue, Rec.flushed s ∈ f'.config.log) ∧
    f'.config.log =
      f.config.log ++ [.commitApproved] ++ List.map Rec.flushed f.queue
        ++ [.activationComplete] := by
  cases h with
  | body _ => simp at hv
  | defer => simp at hv
  | abort => simp at hv
  | @commit w L q fs =>
    refine ⟨rfl, rfl, ?_, ?_, rfl⟩
    · intro s hs
      exact List.mem_append.mpr (Or.inr hs)
    · intro s hs
      have hm : Rec.flushed s ∈ List.map Rec.flushed q :=
        List.mem_map.mpr ⟨s, hs, rfl⟩
      simp [hm]

/-- **Every commit a run settles leaves the deferral queue empty.** The
run cannot have aborted first; a commit is the only step that settles on
this verdict, and no step follows a settled frame. -/
theorem commit_run_queue_empty {f f' : Frame} (h : FrameSteps f f')
    (h0 : f.verdict = none) (hv : f'.verdict = some .commit) :
    f'.queue = [] := by
  induction h with
  | refl => simp [h0] at hv
  | step hs hrest ih =>
    cases hs with
    | body _ => exact ih rfl hv
    | defer => exact ih rfl hv
    | abort =>
      have hc := frameSteps_settled_eq hrest (v := .abort) rfl
      rw [hc] at hv
      simp at hv
    | commit =>
      have hc := frameSteps_settled_eq hrest (v := .commit) rfl
      simp [hc]

/-- The seqs a log segment enqueued, in log order: one per
`deferred-emission` record, which `FrameStep.defer` writes at the enqueue
and no other step writes. -/
def enqueuedSeqs : Log → List Seq
  | [] => []
  | .deferredEmission s :: rest => s :: enqueuedSeqs rest
  | _ :: rest => enqueuedSeqs rest

theorem enqueuedSeqs_append (L M : Log) :
    enqueuedSeqs (L ++ M) = enqueuedSeqs L ++ enqueuedSeqs M := by
  induction L with
  | nil => rfl
  | cons r rest ih => cases r <;> simp [enqueuedSeqs, ih]

/-- A body step extends the log by records that enqueue nothing and are
not the commit decision point. -/
theorem semStep_log_extends {c c' : Config} (h : SemStep c c') :
    ∃ R : Log, c'.log = c.log ++ R ∧ enqueuedSeqs R = [] ∧
      Rec.commitApproved ∉ R := by
  cases h with
  | pure => exact ⟨[], by simp, rfl, by simp⟩
  | witnessed => exact ⟨_, rfl, rfl, by simp⟩
  | emit => exact ⟨_, rfl, rfl, by simp⟩

/-- **Every commit a run settles fires the whole queue, FIFO.** A
`FrameSteps` run that ends on the commit verdict, from an unsettled frame,
leaves the queue empty and appends to the crossing list the entries queued
at the start and then the entries the run enqueued, in that order. `M` is
the segment the run wrote before the decision point (it holds no
`commit-approved` record, which pins it); after it come `commit-approved`,
one `flushed` record per fired entry in the same order, and the terminal
marker. The run cannot have aborted first: an abort settles the verdict,
and nothing moves a settled frame. -/
theorem commit_run_discharges_deferred {f f' : Frame} (h : FrameSteps f f')
    (h0 : f.verdict = none) (hv : f'.verdict = some .commit) :
    ∃ M : Log, Rec.commitApproved ∉ M ∧
      f'.queue = [] ∧
      f'.fired = f.fired ++ f.queue ++ enqueuedSeqs M ∧
      (∀ s ∈ f.queue ++ enqueuedSeqs M, Rec.flushed s ∈ f'.config.log) ∧
      f'.config.log =
        f.config.log ++ M ++ [.commitApproved]
          ++ List.map Rec.flushed (f.queue ++ enqueuedSeqs M)
          ++ [.activationComplete] := by
  induction h with
  | refl => simp [h0] at hv
  | step hs hrest ih =>
    cases hs with
    | body hc =>
      obtain ⟨M, hM, hq, hf, hfl, hl⟩ := ih rfl hv
      obtain ⟨R, hR, hRe, hRc⟩ := semStep_log_extends hc
      refine ⟨R ++ M, ?_, hq, ?_, ?_, ?_⟩
      · simp [hRc, hM]
      · simp [hf, enqueuedSeqs_append, hRe]
      · simpa [enqueuedSeqs_append, hRe] using hfl
      · simp [hl, hR, enqueuedSeqs_append, hRe]
    | @defer s b w L q fs =>
      obtain ⟨M, hM, hq, hf, hfl, hl⟩ := ih rfl hv
      refine ⟨.deferredEmission s :: M, by simp [hM], hq, ?_, ?_, ?_⟩
      · simp [hf, enqueuedSeqs]
      · intro x hx
        exact hfl x (by simpa [enqueuedSeqs] using hx)
      · simp [hl, enqueuedSeqs]
    | abort =>
      have hc := frameSteps_settled_eq hrest (v := .abort) rfl
      rw [hc] at hv
      simp at hv
    | @commit w L q fs =>
      have hc := frameSteps_settled_eq hrest (v := .commit) rfl
      subst hc
      refine ⟨[], by simp, rfl, by simp [enqueuedSeqs], ?_, by simp [enqueuedSeqs]⟩
      intro s hs
      have hm : Rec.flushed s ∈ List.map Rec.flushed q :=
        List.mem_map.mpr ⟨s, by simpa [enqueuedSeqs] using hs, rfl⟩
      simp [hm]

/-- A body that mutates seq 1, then defers seq 2, then hits L-Raise's
failing step. -/
def deferredAbortBody : Body := .witnessed 1 true (.deferred 2 .fail)

/-- Its trace: the descriptor, then the enqueue record. -/
def deferredAbortLog : Log :=
  [.descriptor 1 .transactional true, .deferredEmission 2]

/-- The frame the run starts in. -/
def deferredAbortStart : Frame :=
  ⟨⟨deferredAbortBody, [], []⟩, [], [], none⟩

/-- The frame before the abort: queue `[2]`, nothing fired. -/
def deferredAbortMid : Frame :=
  ⟨⟨.fail, [1], deferredAbortLog⟩, [2], [], none⟩

/-- The frame after the abort. -/
def deferredAbortEnd : Frame :=
  ⟨⟨.fail, [1], deferredAbortLog ++ [.aborted]⟩, [], [], some .abort⟩

/-- **The abort witness.** A real `FrameSteps` run enqueues seq 2,
reaches the failing step, and settles on abort with a NON-EMPTY queue.
The queue is dropped, the crossing list stays empty, and the durable log
carries the enqueue record and the `aborted` completion record. -/
theorem deferred_abort_run :
    FrameSteps deferredAbortStart deferredAbortEnd ∧
    FrameStep deferredAbortMid deferredAbortEnd ∧
    deferredAbortMid.queue = [2] ∧
    deferredAbortEnd.queue = [] ∧
    deferredAbortEnd.fired = [] ∧
    outcome deferredAbortEnd.config.log = .rolledBack ∧
    reported deferredAbortEnd.config.log okAll = [] := by
  refine ⟨?_, FrameStep.abort, rfl, rfl, rfl, by decide, by decide⟩
  exact FrameSteps.step (FrameStep.body SemStep.witnessed)
    (FrameSteps.step FrameStep.defer
      (FrameSteps.step FrameStep.abort FrameSteps.refl))

/-- A body that defers seqs 2 and 4, in program order, then completes. -/
def deferredCommitBody : Body := .deferred 2 (.deferred 4 .done)

/-- Its trace: two enqueue records. -/
def deferredCommitLog : Log := [.deferredEmission 2, .deferredEmission 4]

/-- The frame the run starts in. -/
def deferredCommitStart : Frame :=
  ⟨⟨deferredCommitBody, [], []⟩, [], [], none⟩

/-- The frame before the commit: queue `[2, 4]`, nothing fired. -/
def deferredCommitMid : Frame :=
  ⟨⟨.done, [], deferredCommitLog⟩, [2, 4], [], none⟩

/-- The frame after the commit: FIFO fired, one `flushed` per entry. -/
def deferredCommitEnd : Frame :=
  ⟨⟨.done, [],
     deferredCommitLog ++ [.commitApproved, .flushed 2, .flushed 4,
                           .activationComplete]⟩,
   [], [2, 4], some .commit⟩

/-- **The commit witness.** A real `FrameSteps` run enqueues seqs 2 and 4
and settles on commit. Both fire in program order, the queue is empty
afterwards, and the durable log carries a `flushed` record for each entry
under the `commit-approved` decision point. Contrast `deferred_abort_run`:
the same kind of queue, the opposite verdict. -/
theorem deferred_commit_run :
    FrameSteps deferredCommitStart deferredCommitEnd ∧
    FrameStep deferredCommitMid deferredCommitEnd ∧
    deferredCommitMid.queue = [2, 4] ∧
    deferredCommitEnd.queue = [] ∧
    deferredCommitEnd.fired = [2, 4] ∧
    outcome deferredCommitEnd.config.log = .rolledForward := by
  refine ⟨?_, FrameStep.commit, rfl, rfl, rfl, by decide⟩
  exact FrameSteps.step FrameStep.defer
    (FrameSteps.step FrameStep.defer
      (FrameSteps.step FrameStep.commit FrameSteps.refl))

/-- The same run caught part-way: seq 2 is already queued, seq 4 is still
to be deferred. -/
def deferredCommitHalf : Frame :=
  ⟨⟨.deferred 4 .done, [], [.deferredEmission 2]⟩, [2], [], none⟩

/-- **The whole-run commit witness, with both halves non-empty.** From a
frame whose queue already holds seq 2, a real `FrameSteps` run enqueues
seq 4 and settles on commit. The crossings are the entry queued at the
start and then the one the run enqueued, and the log carries the
enqueue record, `commit-approved`, and one `flushed` record each in that
order: `commit_run_discharges_deferred`'s conclusion with `M` the run's
own enqueue record, so neither the queued-at-start part nor the
run-enqueued part is the empty list. -/
theorem deferred_commit_run_from_queued :
    FrameSteps deferredCommitHalf deferredCommitEnd ∧
    deferredCommitHalf.verdict = none ∧
    deferredCommitEnd.verdict = some .commit ∧
    deferredCommitHalf.queue = [2] ∧
    enqueuedSeqs [.deferredEmission 4] = [4] ∧
    Rec.commitApproved ∉ ([.deferredEmission 4] : Log) ∧
    deferredCommitEnd.fired =
      deferredCommitHalf.fired ++ deferredCommitHalf.queue
        ++ enqueuedSeqs [.deferredEmission 4] ∧
    deferredCommitEnd.config.log =
      deferredCommitHalf.config.log ++ [.deferredEmission 4]
        ++ [.commitApproved]
        ++ List.map Rec.flushed
             (deferredCommitHalf.queue ++ enqueuedSeqs [.deferredEmission 4])
        ++ [.activationComplete] := by
  refine ⟨?_, rfl, rfl, rfl, rfl, by decide, rfl, by decide⟩
  exact FrameSteps.step FrameStep.defer
    (FrameSteps.step FrameStep.commit FrameSteps.refl)

/-! ## The roll-forward window: a fire that raises (issue #2740)

`commit_discharges_deferred` fires every queued entry. The runtime's flush
(`SessionOwner._flush`) is continue-and-record: a host body that raises is
caught, written as `flush-residue`, and the remaining queue still flushes,
so the commit still completes. A recovery run then reads each emission
against the `flushed` and `flush-residue` records (`deferClass`). -/

/-- Every conclusion about a commit flush that follows from the shape of
the frame it leaves, for a raising set `bad`. -/
theorem flush_shape_props (f f' : Frame) (bad : List Seq)
    (hq : f'.queue = [])
    (hfired : f'.fired = f.fired ++ f.queue.filter (fun s => !memSeq s bad))
    (hlog : f'.config.log =
      f.config.log ++ [.commitApproved] ++ f.queue.map (flushRec bad)
        ++ [.activationComplete])
    (hnf : ∀ s ∈ f.queue, s ∉ flushedSeqs f.config.log ∧
      s ∉ flushFailedSeqs f.config.log)
    (hfork : hasForkFrozen f.config.log = false) :
    f'.queue = [] ∧
    f'.fired = f.fired ++ f.queue.filter (fun s => !memSeq s bad) ∧
    outcome f'.config.log = .rolledForward ∧
    (∀ s ∈ f.queue, memSeq s bad = false →
      s ∈ f'.fired ∧ deferClass f'.config.log s = .flushed) ∧
    (∀ s ∈ f.queue, memSeq s bad = true →
      Rec.flushResidue s ∈ f'.config.log ∧
      deferClass f'.config.log s = .owedFailed) := by
  have hfork' : Rec.forkFrozen ∉ f.config.log := hasRec_absent.mp hfork
  have hnofork : hasForkFrozen f'.config.log = false := by
    apply hasRec_absent.mpr
    intro hmem
    rw [hlog] at hmem
    simp only [List.mem_append, List.mem_map, List.mem_cons,
      List.not_mem_nil, or_false] at hmem
    rcases hmem with ((hm | hm) | ⟨t, _, ht⟩) | hm
    · exact hfork' hm
    · cases hm
    · simp only [flushRec] at ht
      split at ht <;> cases ht
    · cases hm
  refine ⟨hq, hfired, ?_, ?_, ?_⟩
  · exact (outcome_forward_iff hnofork).mpr
      (Or.inl (hasRec_iff.mpr (by rw [hlog]; simp)))
  · intro s hs hb
    refine ⟨?_, ?_⟩
    · rw [hfired]
      exact List.mem_append.mpr (Or.inr (List.mem_filter.mpr ⟨hs, by simp [hb]⟩))
    · rw [hlog, deferClass_after_flush _ _ _ hs (hnf s hs).1 (hnf s hs).2]
      simp [hb]
  · intro s hs hb
    refine ⟨?_, ?_⟩
    · rw [hlog]
      have hm : Rec.flushResidue s ∈ f.queue.map (flushRec bad) :=
        List.mem_map.mpr ⟨s, hs, by simp [flushRec, hb]⟩
      simp [hm]
    · rw [hlog, deferClass_after_flush _ _ _ hs (hnf s hs).1 (hnf s hs).2]
      simp [hb]

/-- **A commit flush discharges or owes every queued entry, and does not
stop at the first that raises.** Any step that settles on commit, in the
relation that lets a fire raise, empties the queue and leaves the log in
which each entry whose body returned reads FLUSHED to a recovery run and
each entry whose body raised reads OWED, with a `flush-residue` record.
The crossing list gains the first set and not the second. The run is read
as rolled forward, so the owed entries are the roll-forward residue.
`hnf` says the flush found no earlier outcome for the entries it flushes,
and `hfork` that the session was not a retired fork. -/
theorem commit_flush_is_discharged_or_owed {f f' : Frame} (h : FrameStepR f f')
    (hv : f'.verdict = some .commit)
    (hnf : ∀ s ∈ f.queue, s ∉ flushedSeqs f.config.log ∧
      s ∉ flushFailedSeqs f.config.log)
    (hfork : hasForkFrozen f.config.log = false) :
    ∃ bad : List Seq,
      f'.queue = [] ∧
      f'.fired = f.fired ++ f.queue.filter (fun s => !memSeq s bad) ∧
      outcome f'.config.log = .rolledForward ∧
      (∀ s ∈ f.queue, memSeq s bad = false →
        s ∈ f'.fired ∧ deferClass f'.config.log s = .flushed) ∧
      (∀ s ∈ f.queue, memSeq s bad = true →
        Rec.flushResidue s ∈ f'.config.log ∧
        deferClass f'.config.log s = .owedFailed) := by
  cases h with
  | base hs =>
    cases hs with
    | body _ => simp at hv
    | defer => simp at hv
    | abort => simp at hv
    | @commit w L q fs =>
      refine ⟨[], flush_shape_props _ _ [] rfl ?_ ?_ hnf hfork⟩
      · simp only [memSeq]
        exact congrArg (fs ++ ·) (List.filter_eq_self.mpr fun _ _ => rfl).symm
      · have hm : q.map (flushRec []) = q.map Rec.flushed :=
          List.map_congr_left fun s _ => flushRec_nil s
        simp [hm]
  | @commitRaising bad w L q fs =>
    exact ⟨bad, flush_shape_props _ _ bad rfl rfl rfl hnf hfork⟩

/-- The queue of the whole-run commit witnesses, caught before the commit
with seqs 2 and 4 queued. -/
def deferredRaisingEnd : Frame :=
  ⟨⟨.done, [],
     deferredCommitLog ++ [.commitApproved, .flushResidue 2, .flushed 4,
                           .activationComplete]⟩,
   [], [4], some .commit⟩

/-- A WAL cut inside the flush: seq 2 flushed, then the process died before
seq 4 fired. -/
def flushCrashLog : Log :=
  deferredCommitLog ++ [.commitApproved, .flushed 2]

/-- The same enqueues with no approval at all. -/
def unapprovedLog : Log := deferredCommitLog

/-- **The flush-residue witness.** A real step of the relation that lets a
fire raise settles on commit with seq 2 raising and seq 4 returning. The
crossing list gains seq 4 and not seq 2, the log carries `flush-residue`
for seq 2 and `flushed` for seq 4, and a recovery run reads exactly seq 2
as owed and seq 4 as flushed, in a rolled-forward verdict. The raise of
seq 2 did not stop seq 4 from flushing. -/
theorem flush_residue_run :
    FrameStepR deferredCommitMid deferredRaisingEnd ∧
    deferredRaisingEnd.fired = [4] ∧
    deferralView deferredRaisingEnd.config.log =
      [(2, .owedFailed), (4, .flushed)] ∧
    owedOf deferredRaisingEnd.config.log = [2] ∧
    flushedOf deferredRaisingEnd.config.log = [4] ∧
    outcome deferredRaisingEnd.config.log = .rolledForward := by
  refine ⟨?_, rfl, by decide, by decide, by decide, by decide⟩
  exact FrameStepR.commitRaising [2]

/-- **A cut inside the flush, and a log with no approval.** After one fire
a crash leaves seq 2 flushed and seq 4 owed, not attempted: the body may
not have fired, which is the honest state. The same enqueues with no
`commit-approved` read as dropped, never owed: nothing crossed. -/
theorem flush_crash_cut_witness :
    deferralView flushCrashLog = [(2, .flushed), (4, .owedUnattempted)] ∧
    owedOf flushCrashLog = [4] ∧
    outcome flushCrashLog = .rolledForward ∧
    deferralView unapprovedLog = [(2, .dropped), (4, .dropped)] ∧
    owedOf unapprovedLog = [] ∧
    outcome unapprovedLog = .rolledBack := by
  decide

/-- **Non-vacuity for the flush theorem's side conditions.** The frame the
raising step starts from has a queue whose entries the log has no outcome
for, and the log carries no retired fork. -/
theorem flush_hypotheses_are_inhabited :
    deferredCommitMid.verdict = none ∧
    (∀ s ∈ deferredCommitMid.queue,
      s ∉ flushedSeqs deferredCommitMid.config.log ∧
      s ∉ flushFailedSeqs deferredCommitMid.config.log) ∧
    hasForkFrozen deferredCommitMid.config.log = false := by
  refine ⟨rfl, ?_, by decide⟩
  intro s hs
  simp only [deferredCommitMid, List.mem_cons, List.not_mem_nil, or_false] at hs
  rcases hs with rfl | rfl <;> decide

/-! ## The crash window of a witnessed mutation (issue #2740)

`SplitStep` splits `SemStep.witnessed` into the forward extern returning
(`mutate`) and the descriptor being written (`record`). A crash is a prefix
of the run, so every configuration a `SplitSteps` run passes through is a
crash cut, and the ones with a pending mutation are the cuts inside the
window: the referent is in the world and no record names it. -/

/-- **A split run that is not cut inside the window collapses to a
`SemSteps` run.** This is the forward direction; the converse, that every
`SemSteps` run is a split run, is `semSteps_has_split`. So every theorem
over `SemSteps` holds of the split relation at the cuts outside the window,
and the window is the only thing the split adds. -/
theorem split_complete_is_sem {a b : SplitConfig} (h : SplitSteps a b)
    (ha : a.pend = none) (hb : b.pend = none) :
    SemSteps ⟨a.body, a.world, a.log⟩ ⟨b.body, b.world, b.log⟩ := by
  have := splitSteps_collapse h
  simpa [SplitConfig.proj, ha, hb] using this

/-- **A recovery run cannot tell that the extern ran.** The step `mutate`
puts the referent in the world and writes nothing: the log after it is the
log before it, and the world a crash finds gains exactly the pending
referent. Everything a recovery run reads (`outcome`, `rollbackReplay`,
`reported`) is a function of the log, so it reads the cut inside the window
exactly as it reads the cut before the mutation. -/
theorem window_log_is_the_log_before_the_mutation {a b : SplitConfig}
    (h : SplitStep a b) (ha : a.pend = none) (hb : b.pend ≠ none) :
    ∃ s i, b.pend = some (s, i) ∧ b.log = a.log ∧ b.live = s :: a.live := by
  cases h with
  | pure => exact absurd rfl hb
  | mutate => exact ⟨_, _, rfl, rfl, rfl⟩
  | record => simp at ha
  | emit => exact absurd rfl hb

/-- **What recovery leaves of a run cut at any step.** Take any
configuration a split run from an empty log reaches, with every referent
fresh and no emission. A recovery run replays the log's witnessed
inverses, and what is left of the world a crash found is the world the run
inherited, plus the pending mutation if the cut is inside the window and
nothing otherwise. The pending referent is not undone because no record
names it. `hnew` is the seq-space invariant for the pending referent. -/
theorem crash_window_recovery {b : Body} {w₀ : World} {c : SplitConfig}
    (hs : SplitSteps ⟨b, w₀, [], none⟩ c)
    (hfresh : ∀ s ∈ logSeqs c.log, s ∉ w₀)
    (hno : emittedSeqs c.log = [])
    (hnew : ∀ s i, c.pend = some (s, i) → s ∉ logSeqs c.log) :
    replay c.live (rollbackReplay c.log) =
      (match c.pend with
       | none => w₀
       | some (s, _) => s :: w₀) := by
  obtain ⟨M, hL, hM, hw⟩ := splitSteps_trace hs
  have hM' : SemLog c.log := hL ▸ hM
  have hw' : c.world = (logSeqs c.log).reverse ++ w₀ := by rw [hL]; exact hw
  have hdisj : ∀ s ∈ emittedSeqs c.log, s ∉ witnessedSeqs c.log := by
    intro s hsm; rw [hno] at hsm; cases hsm
  have hhead : (logSeqs c.log).reverse.filter
      (fun t => !(memSeq t (witnessedSeqs c.log))) = [] := by
    rw [List.filter_reverse, logSeqs_filter_gen hM' (fun _ h => h) hdisj, hno]
    rfl
  have htail : w₀.filter (fun t => !(memSeq t (witnessedSeqs c.log))) = w₀ :=
    filter_none_out fun t ht hin => hfresh t (witnessedSeqs_sub hin) ht
  rw [rollbackReplay_sem hM', replay_eq_filter]
  cases hp : c.pend with
  | none =>
    simp only [SplitConfig.live, hp]
    rw [hw', List.filter_append, hhead, htail]
    rfl
  | some p =>
    obtain ⟨s, i⟩ := p
    have hsW : memSeq s (witnessedSeqs c.log) = false :=
      memSeq_false.mpr fun hm => hnew s i hp (witnessedSeqs_sub hm)
    simp only [SplitConfig.live, hp]
    rw [List.filter_cons, hw', List.filter_append, hhead, htail]
    simp [hsW]

/-- **The pending referent is invisible to recovery.** At a cut inside the
window the referent is in the world a crash finds, and a recovery run does
not replay it and does not report it, because the replay set and the
residue surface of a trace are read off the log's records and no record
names it (`hnew`). That the referent is in the world is the definition of
the pending mutation; the content is the two exclusions, and
`window_log_is_the_log_before_the_mutation` is why the log cannot say
more. -/
theorem orphan_is_invisible_to_recovery {b : Body} {w₀ : World} {c : SplitConfig}
    (hs : SplitSteps ⟨b, w₀, [], none⟩ c) {s : Seq} {i : Bool}
    (hp : c.pend = some (s, i)) (hnew : s ∉ logSeqs c.log) :
    s ∈ c.live ∧ s ∉ rollbackReplay c.log ∧
    s ∉ reported c.log (fun _ => true) := by
  obtain ⟨M, hL, hM, _⟩ := splitSteps_trace hs
  have hM' : SemLog c.log := hL ▸ hM
  refine ⟨?_, ?_, ?_⟩
  · simp [SplitConfig.live, hp]
  · rw [rollbackReplay_sem hM']
    exact fun hm => hnew (witnessedSeqs_sub hm)
  · rw [reported_sem hM']
    exact fun hm => hnew (emittedSeqs_sub hm)

/-- **At the window, R4's exact-residue equation does not hold.** For an
abort that reports everything it can see, the world after replay is the
inherited world plus the reported residue (`residue_is_exactly_what_remains`).
At a cut inside the window of a body with no emission (`hno`), the left side
has the pending referent and the right side, which is the inherited world,
does not, because nothing reports the referent. So the exact-residue claim
is a claim about cuts outside the window, which is where `split_complete_is_sem`
puts it. -/
theorem window_breaks_exact_residue {b : Body} {w₀ : World} {c : SplitConfig}
    (hs : SplitSteps ⟨b, w₀, [], none⟩ c)
    (hfresh : ∀ s ∈ logSeqs c.log, s ∉ w₀)
    (hno : emittedSeqs c.log = [])
    {s : Seq} {i : Bool} (hp : c.pend = some (s, i))
    (hnew : s ∉ logSeqs c.log) :
    replay c.live (rollbackReplay c.log) ≠
      (reported c.log (fun _ => true)).reverse ++ w₀ := by
  have hrec := crash_window_recovery hs hfresh hno
    (fun s' i' hp' => by
      rw [hp] at hp'
      obtain ⟨rfl, _⟩ := Prod.mk.inj (Option.some.inj hp')
      exact hnew)
  obtain ⟨M, hL, hM, _⟩ := splitSteps_trace hs
  have hM' : SemLog c.log := hL ▸ hM
  rw [hp] at hrec
  simp only at hrec
  rw [hrec, reported_sem hM', hno]
  intro h
  have := congrArg List.length h
  simp at this

/-- **The `mutate` witness.** The extern of a one-step body returns: the
step is `SplitStep.mutate`, the log is the empty log before and after, and
the world a crash finds gains seq 1. -/
theorem mutate_witness :
    SplitStep ⟨.witnessed 1 true .fail, [], [], none⟩
              ⟨.fail, [], [], some (1, true)⟩ ∧
    (⟨.fail, [], [], some (1, true)⟩ : SplitConfig).log =
      (⟨.witnessed 1 true .fail, [], [], none⟩ : SplitConfig).log ∧
    (⟨.fail, [], [], some (1, true)⟩ : SplitConfig).live = [1] ∧
    (⟨.witnessed 1 true .fail, [], [], none⟩ : SplitConfig).live = [] :=
  ⟨.mutate, rfl, rfl, rfl⟩

/-- A body of two witnessed mutations that hits the failing step, as the
split relation runs it. -/
def windowBody : Body := .witnessed 1 true (.witnessed 2 true .fail)

/-- The cut right after the second extern returned: seq 1 is mutated and
recorded, seq 2 is mutated and its record is not yet written. -/
def windowCut : SplitConfig :=
  ⟨.fail, [1], [.descriptor 1 .transactional true], some (2, true)⟩

/-- The same run one step later: seq 2 is recorded too. -/
def windowDone : SplitConfig :=
  ⟨.fail, [2, 1], [.descriptor 1 .transactional true,
                   .descriptor 2 .transactional true], none⟩

/-- **The crash-window witness.** The split relation really takes this run,
and the two cuts one step apart give opposite recovery results: at the cut
inside the window the world holds seq 2 and no record names it, so recovery
replays seq 1 only, reports nothing, and leaves seq 2 in the world; one
step later the record exists and recovery restores the world exactly. -/
theorem crash_window_witness :
    SplitSteps ⟨windowBody, [], [], none⟩ windowCut ∧
    SplitSteps ⟨windowBody, [], [], none⟩ windowDone ∧
    windowCut.live = [2, 1] ∧
    logSeqs windowCut.log = [1] ∧
    rollbackReplay windowCut.log = [1] ∧
    reported windowCut.log okAll = [] ∧
    replay windowCut.live (rollbackReplay windowCut.log) = [2] ∧
    replay windowDone.live (rollbackReplay windowDone.log) = [] := by
  refine ⟨?_, ?_, rfl, rfl, by decide, by decide, by decide, by decide⟩
  · exact .step .mutate (.step .record (.step .mutate .refl))
  · exact .step .mutate (.step .record (.step .mutate (.step .record .refl)))

/-- **Non-vacuity for the window theorems' side conditions.** The cut is
reached from an empty log by the split relation, every logged referent is
fresh against the inherited world, the log carries no emission, and the
pending referent is in no record. -/
theorem window_hypotheses_are_inhabited :
    SplitSteps ⟨windowBody, [], [], none⟩ windowCut ∧
    (∀ s ∈ logSeqs windowCut.log, s ∉ ([] : World)) ∧
    emittedSeqs windowCut.log = [] ∧
    windowCut.pend = some (2, true) ∧
    (2 : Seq) ∉ logSeqs windowCut.log := by
  refine ⟨crash_window_witness.1, ?_, rfl, rfl, by decide⟩
  intro s hs
  exact List.not_mem_nil

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


/-- A fresh run: the descriptor becomes durable, then the fence, then the
undeclared inverse fires. -/
def fencedRun : Run :=
  [.append (.descriptor 7 .transactional false), .append (.replayFence 7),
   .apply 7]

/-- No inverse in that run is declared idempotent. -/
def noneIdem : Seq → Bool := fun _ => false

/-- **Non-vacuity for the remaining hypothesis families** (roadmap item
418, step 8). `revert_witness` and `commit_witness` already exhibit the
`SemSteps` and commit hypotheses; these are the ones left over.
`fence_before_apply_at_every_cut` and `at_most_once_across_crash` are
stated over `WAFrom idem [] r` with an undeclared inverse that has
observably FIRED and a unique seq space. All four conditions hold at
once here, on a run that really applies something, so neither theorem is
quantifying over runs that fire nothing. `SemLog` is inhabited by the
revert trace, and `hasForkFrozen = false` and a discharged seq are
exhibited on the concrete logs above. -/
theorem a8_hypotheses_are_inhabited :
    SemLog revertLog ∧
    hasForkFrozen cleanAbort = false ∧
    (7 : Seq) ∈ dischargedSeqs committedSession ∧
    WAFrom noneIdem [] fencedRun ∧
    noneIdem 7 = false ∧
    (7 : Seq) ∈ fired fencedRun ∧
    SeqUnique (durable fencedRun) ∧
    Rec.descriptor 7 .transactional false ∈ durable fencedRun ∧
    outcome committedTrace ≠ .rolledBack := by
  have hsu : SeqUnique (durable fencedRun) := by
    intro a ha b hb s hsa hsb
    simp only [fencedRun, durable, List.mem_cons, List.not_mem_nil,
      or_false] at ha hb
    rcases ha with rfl | rfl <;> rcases hb with rfl | rfl <;>
      simp_all [recSeq]
  refine ⟨.descriptor (.descriptor .nil), by decide, by decide, ?_, rfl,
    by decide, hsu, by decide, by decide⟩
  exact .append (.append (.apply (Or.inr (by decide)) .nil))

end RevL.A8
