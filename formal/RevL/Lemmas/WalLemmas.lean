/-!
RevL.Lemmas.WalLemmas — L1 lemma farm: the write-ahead log state machine
(roadmap items 47 / 243 / 245 / 247 / 309 / 322 / 413; the reference is
`src/revl/wal.py` + `src/revl/recovery.py`).

Core only: this farm imports nothing, so it cannot drift into L0 and no
other farm depends on it.

## What is modelled

`revl.wal` says what a durable WAL is: JSON Lines, one header, one line
per record, a terminal marker. `revl.recovery.recover` says what reading
one back means. Both are modelled here as a *state machine over a log*,
because the only thing a fresh process has after a crash IS the log.

### The record set (`Rec`)

One constructor per record kind `read_wal` classifies, minus the ones
that carry no decision content:

| reference record | constructor | what it decides |
|---|---|---|
| `discharge-descriptor` | `Rec.descriptor` | a re-issuable named inverse (`transactional`) or compensation, with item 309's `undo_idempotent` flag |
| `effect` | `Rec.effect` | the legacy boundary record: whether the referent outlives the process, and whether its inverse is reconstructible or closure-only |
| `discharge` | `Rec.discharge` | the commit-path proof for a set of seqs |
| `replay-fence` | `Rec.replayFence` | item 309 §3a's durable at-most-once fence |
| `deferred-emission` | `Rec.deferredEmission` | a class-(b) emission queued but not fired |
| `commit-approved` | `Rec.commitApproved` | the session-commit decision point (item 245) |
| `aborted` | `Rec.aborted` | the in-process abort's COMPLETION record |
| `fork-frozen` | `Rec.forkFrozen` | item 250's retired-at-k parent |
| `activation-complete` | `Rec.activationComplete` | the terminal marker |

`header` carries no recovery decision (its version gate fails closed
before any decision is reached) and is not modelled.

### The crash cut

A crash is not a separate event: it is a *prefix*. What survives is what
was durable, so a crash cut is `L.take n` over the run, and a torn
trailing line is the cut that lands one record short. `read_wal`
tolerating a torn trailing line and refusing mid-file corruption is
exactly the statement that the durable state is always a prefix — so
every theorem below quantified over `n` is quantified over every crash
point.

### The decision

`recover`'s if-chain, verbatim in `outcome`:

```python
if frozen is not None:            return _fork_retired(...)      # item 250
if wal["complete"]:               return _roll_forward(...)      # terminal marker
if approved is not None:          return _roll_forward_window(...)  # item 245 D3
return _roll_back(...)
```

### The roll-back walk

`_roll_back`'s two families, `dispose` per record:

* legacy `effect`: no durable referent ⇒ **moot** (its memory died with
  the process); reconstructible ⇒ re-issued; closure-only ⇒ **residue**
  ("never pretending a dead lambda ran");
* `discharge-descriptor`, Phase 1 (transactional), in the reference's own
  branch order — durable `discharge` ⇒ **committed, skipped, retained**
  ("a COMMITTED transaction is NOT rolled back"); undeclared-and-fenced
  with an `aborted` completion record ⇒ resolved, not re-applied;
  undeclared-and-fenced without one ⇒ **fenced residue**, outcome
  unknown, refused; otherwise re-issued, and a failed re-issue is
  **restore residue** (243 rule 6: the inverse is fallible);
* Phase 2 (compensation): discharged ⇒ skipped; otherwise re-issued
  best-effort and **always** residue, because a compensation's landing
  cannot be confirmed in a fresh process (247: compensation is never
  inversion);
* `deferred-emission` with no `commit-approved` ⇒ **dropped**, never
  fired, zero crossings, reported clean.

### The write-ahead ordering

`WAFrom` is the consume-before-fire rule of item 309 §3a: an inverse that
is not *declared* idempotent may only be applied after its fence is
durable. It is stated as a property of the RUN (a list of durable appends
and observable applies), not of the log, because that is the only place a
crash *between* the durable write and the effect can be expressed.

## Scope, stated honestly

* Durability itself is the model's floor, not its theorem: `WAFrom` says
  the fence append precedes the apply in the run, and `durable`/`fired`
  read the run off at a cut. That an `fsync` really reaches the platter
  is a host obligation, and no theorem here claims otherwise.
* The success of a re-issued inverse against the outside world is an
  oracle parameter (`ok : Seq → Bool`), matching 243 rule 6.
* `reported` models the ROLL-BACK path's residue surface. The
  roll-forward window's `flush-residue` (a deferred emission fired with
  no idempotency key) is a different surface and is not modelled; R4
  below is stated under `outcome L = .rolledBack`.
* Compensation and transactional referents are keyed by seq here; the
  reference keys off `World.key(call)`. Two descriptors sharing a
  referent are therefore two seqs in the model, which is the reference's
  own descriptor granularity.
-/

namespace RevL.Lemmas

/-! ## Seqs and computable membership -/

/-- A WAL step identity (`discharge-descriptor.seq`). -/
abbrev Seq := Nat

/-- Decidable membership in a seq list, spelled by recursion so the
concrete traces at the bottom of the L2 files close by `decide`. -/
def memSeq (s : Seq) : List Seq → Bool
  | [] => false
  | t :: rest => if t = s then true else memSeq s rest

theorem memSeq_iff {s : Seq} : ∀ {l : List Seq}, memSeq s l = true ↔ s ∈ l := by
  intro l
  induction l with
  | nil => simp [memSeq]
  | cons t rest ih =>
    simp only [memSeq, List.mem_cons]
    split
    · next h => exact ⟨fun _ => Or.inl h.symm, fun _ => rfl⟩
    · next h =>
      rw [ih]
      exact ⟨Or.inr, fun hm => hm.elim (fun he => absurd he.symm h) id⟩

theorem memSeq_false {s : Seq} {l : List Seq} : memSeq s l = false ↔ s ∉ l := by
  constructor
  · intro h hm; rw [← memSeq_iff] at hm; rw [hm] at h; exact Bool.noConfusion h
  · intro h; cases hd : memSeq s l with
    | false => rfl
    | true => exact absurd (memSeq_iff.mp hd) h

/-! ## Records -/

/-- The `entry` field of a `discharge-descriptor`: an inverse that undoes
(items 243) or a compensation that offsets (item 247). -/
inductive Entry where
  | transactional
  | compensation
  deriving DecidableEq, Repr

/-- One durable WAL record. -/
inductive Rec where
  /-- `discharge-descriptor`: a re-issuable named call. `undoIdempotent`
  is item 309's declared-idempotent flag. -/
  | descriptor (seq : Seq) (entry : Entry) (undoIdempotent : Bool)
  /-- The legacy per-step `effect` record. `boundary` is whether the
  referent outlives the process; `reconstructible` is whether the inverse
  is a named call rather than a dead closure. -/
  | effect (seq : Seq) (boundary : Bool) (reconstructible : Bool)
  /-- `discharge`: the commit-path proof for these seqs. -/
  | discharge (seqs : List Seq)
  /-- `replay-fence`: item 309 §3a's at-most-once fence. -/
  | replayFence (seq : Seq)
  /-- `deferred-emission`: queued, not yet fired. -/
  | deferredEmission (seq : Seq)
  /-- `commit-approved`: the session-commit decision point (item 245). -/
  | commitApproved
  /-- `aborted`: the in-process abort's completion record. -/
  | aborted
  /-- `fork-frozen`: a forked parent retired at k (item 250). -/
  | forkFrozen
  /-- `activation-complete`: the terminal marker. -/
  | activationComplete
  deriving DecidableEq, Repr

/-- A durable write-ahead log: the records, in the order they were
appended. -/
abbrev Log := List Rec

/-- Decidable presence of a record, by recursion for the same reason
`memSeq` is. -/
def hasRec (r : Rec) : Log → Bool
  | [] => false
  | x :: rest => if x = r then true else hasRec r rest

theorem hasRec_iff {r : Rec} : ∀ {L : Log}, hasRec r L = true ↔ r ∈ L := by
  intro L
  induction L with
  | nil => simp [hasRec]
  | cons x rest ih =>
    simp only [hasRec, List.mem_cons]
    split
    · next h => exact ⟨fun _ => Or.inl h.symm, fun _ => rfl⟩
    · next h =>
      rw [ih]
      exact ⟨Or.inr, fun hm => hm.elim (fun he => absurd he.symm h) id⟩

/-- Presence is monotone along a sublist: nothing durable is lost by
letting the run continue. -/
theorem hasRec_mono {r : Rec} {L M : Log} (hs : ∀ x ∈ L, x ∈ M) :
    hasRec r L = true → hasRec r M = true := by
  intro h
  exact hasRec_iff.mpr (hs r (hasRec_iff.mp h))

theorem hasRec_false_mono {r : Rec} {L M : Log} (hs : ∀ x ∈ L, x ∈ M) :
    hasRec r M = false → hasRec r L = false := by
  intro h
  cases hd : hasRec r L with
  | false => rfl
  | true => rw [hasRec_mono hs hd] at h; exact Bool.noConfusion h

/-! ## Reading the log -/

/-- `wal["complete"]` — the terminal marker is present. -/
def hasComplete (L : Log) : Bool := hasRec .activationComplete L

/-- The `commit-approved` record (item 245, Decision 3). -/
def hasApproved (L : Log) : Bool := hasRec .commitApproved L

/-- The `fork-frozen` record (item 250, Decision 5). -/
def hasForkFrozen (L : Log) : Bool := hasRec .forkFrozen L

/-- `abort_completed`: the in-process abort's completion record. -/
def abortCompleted (L : Log) : Bool := hasRec .aborted L

/-- Every seq named by a durable `discharge` record. -/
def dischargedSeqs : Log → List Seq
  | [] => []
  | .discharge ss :: rest => ss ++ dischargedSeqs rest
  | _ :: rest => dischargedSeqs rest

/-- Every seq named by a durable `replay-fence` record. -/
def fencedSeqs : Log → List Seq
  | [] => []
  | .replayFence s :: rest => s :: fencedSeqs rest
  | _ :: rest => fencedSeqs rest

/-! ## The decision -/

/-- What one recovery run converges to. Three constructors, not two:
item 250's frozen fork is a terminal, non-live state that is neither a
commit nor an abort, and pretending otherwise would be modelling a
machine the reference does not have. -/
inductive Outcome where
  | rolledForward
  | rolledBack
  | forkRetired
  deriving DecidableEq, Repr

/-- `recover`'s if-chain. -/
def outcome (L : Log) : Outcome :=
  if hasForkFrozen L then .forkRetired
  else if hasComplete L then .rolledForward
  else if hasApproved L then .rolledForward
  else .rolledBack

/-! ## The roll-back walk -/

/-- Why a witnessed effect is still owed after a recovery run. -/
inductive Residue where
  /-- item 309 §3a: an undeclared inverse whose single at-most-once
  attempt was already spent. Outcome unknown; refused. -/
  | fenced
  /-- A boundary inverse found only as a closure: it cannot be re-issued
  in a fresh process. -/
  | unreconstructible
  /-- 247: a compensation's landing cannot be confirmed after a crash. -/
  | compensationUnconfirmed
  /-- 243 rule 6: the re-issued inverse failed. -/
  | restoreFailed
  deriving DecidableEq, Repr

/-- What a recovery run concluded about one witnessed effect. -/
inductive Disp where
  /-- A durable `discharge` record: committed, skipped, referent
  deliberately retained. -/
  | committed
  /-- An in-process referent: its memory died with the process, so the
  inverse is a no-op. Moot, not residue. -/
  | moot
  /-- A class-(b) deferred emission with no `commit-approved`: never
  fired, zero crossings. -/
  | dropped
  /-- The inverse ran (or a completed abort proves it ran). -/
  | discharged
  /-- Still owed, and reported. -/
  | residue (r : Residue)
  deriving DecidableEq, Repr

/-- `_roll_back`'s classification of one record, in the reference's own
branch order. `ok` is the re-issue oracle (243 rule 6: the inverse is
fallible). -/
def dispose (L : Log) (ok : Seq → Bool) : Rec → Option (Seq × Disp)
  | .descriptor s .transactional idem =>
      some (s,
        if memSeq s (dischargedSeqs L) then .committed
        else if idem = false && memSeq s (fencedSeqs L) then
          (if abortCompleted L then .discharged else .residue .fenced)
        else if ok s then .discharged
        else .residue .restoreFailed)
  | .descriptor s .compensation _ =>
      some (s, if memSeq s (dischargedSeqs L) then .committed
               else .residue .compensationUnconfirmed)
  | .effect s b rc =>
      some (s, if b = false then .moot
               else if rc then .discharged
               else .residue .unreconstructible)
  | .deferredEmission s => some (s, .dropped)
  | _ => none

/-- The residue surface a roll-back reports (`residue.outstanding`). -/
def reported (L : Log) (ok : Seq → Bool) : List Seq :=
  L.filterMap fun r =>
    match dispose L ok r with
    | some (s, .residue _) => some s
    | _ => none

/-- `residue.clean`. -/
def clean (L : Log) (ok : Seq → Bool) : Bool := (reported L ok).isEmpty

/-- The seqs whose inverse this roll-back run actually RE-ISSUES against
the world. Distinct from `Disp.discharged`, which also covers the
completed-abort resolution and the moot in-process cases: nothing is
applied there. -/
def reissued (L : Log) : Rec → List Seq
  | .descriptor s .transactional idem =>
      if memSeq s (dischargedSeqs L) then []
      else if idem = false && memSeq s (fencedSeqs L) then []
      else [s]
  | .descriptor s .compensation _ =>
      if memSeq s (dischargedSeqs L) then [] else [s]
  | .effect s b rc => if b && rc then [s] else []
  | _ => []

/-- The roll-back path's replay set. -/
def rollbackReplay (L : Log) : List Seq := L.flatMap (reissued L)

/-- What a whole recovery run applies. A roll-forward and a retired fork
apply NOTHING: `_roll_forward`, `_roll_forward_window` and `_fork_retired`
all return without touching the world. -/
def replayed (L : Log) : List Seq :=
  match outcome L with
  | .rolledBack => rollbackReplay L
  | _ => []

/-! ## The declarative residue spec

`reported` is the runtime's walk. `Owed` is what "still owed" MEANS,
written independently as a relation, so that "the residue surface is
exactly the set the runtime reports" is a theorem relating two
definitions rather than a restatement of one. -/

/-- `Owed L ok r`: record `r` names a witnessed effect that a roll-back
of `L` leaves un-discharged. -/
inductive Owed (L : Log) (ok : Seq → Bool) : Rec → Prop where
  /-- Undeclared, fenced, and the abort did not complete: its single
  at-most-once attempt is spent and its outcome is unknown. -/
  | fenced : ∀ {s idem}, idem = false → s ∉ dischargedSeqs L →
      s ∈ fencedSeqs L → abortCompleted L = false →
      Owed L ok (.descriptor s .transactional idem)
  /-- Re-issued and failed (243 rule 6). -/
  | restoreFailed : ∀ {s idem}, s ∉ dischargedSeqs L →
      ¬(idem = false ∧ s ∈ fencedSeqs L) → ok s = false →
      Owed L ok (.descriptor s .transactional idem)
  /-- An owed compensation: re-attempted best-effort, never confirmed. -/
  | compensation : ∀ {s idem}, s ∉ dischargedSeqs L →
      Owed L ok (.descriptor s .compensation idem)
  /-- A boundary referent whose inverse was closure-only. -/
  | unreconstructible : ∀ {s}, Owed L ok (.effect s true false)

/-- The seq a disposable record names. -/
def recSeq : Rec → Option Seq
  | .descriptor s _ _ => some s
  | .effect s _ _ => some s
  | .deferredEmission s => some s
  | _ => none

/-- One strictly increasing seq space per session WAL (item 325,
`replay._next_seq`): a seq names at most one witnessed effect, so at most
one record disposes of it. -/
def SeqUnique (L : Log) : Prop :=
  ∀ a ∈ L, ∀ b ∈ L, ∀ s, recSeq a = some s → recSeq b = some s → a = b

theorem fencedSeqs_mem {s : Seq} : ∀ {L : Log}, Rec.replayFence s ∈ L →
    s ∈ fencedSeqs L := by
  intro L h
  induction L with
  | nil => cases h
  | cons x rest ih =>
    cases List.mem_cons.mp h with
    | inl he => subst he; exact List.mem_cons_self ..
    | inr hr =>
      cases x <;> simp only [fencedSeqs] <;> first
        | exact ih hr
        | exact List.mem_cons_of_mem _ (ih hr)

/-- Everything the roll-back walk re-issues is named by the record it
came from. -/
theorem reissued_recSeq {L : Log} {rc : Rec} {s : Seq} :
    s ∈ reissued L rc → recSeq rc = some s := by
  intro h
  cases rc with
  | descriptor t e b =>
    cases e <;>
      (simp only [reissued] at h
       repeat' split at h
       all_goals simp_all [recSeq])
  | effect t bd rc' =>
    simp only [reissued] at h
    split at h
    all_goals simp_all [recSeq]
  | discharge _ => simp [reissued] at h
  | replayFence _ => simp [reissued] at h
  | deferredEmission _ => simp [reissued] at h
  | commitApproved => simp [reissued] at h
  | aborted => simp [reissued] at h
  | forkFrozen => simp [reissued] at h
  | activationComplete => simp [reissued] at h

/-! ## The world a re-issued inverse acts on

`recovery.DictWorld` models the outside world as the set of durable
referents a crash orphaned; `apply_inverse` pops the referent the call
names, and `remaining()` is what is left. That set model is why a
DECLARED-idempotent inverse replays freely: popping twice is popping
once. -/

/-- `DictWorld.remaining()` — the durable referents still out. -/
abbrev World := List Seq

/-- `world.apply_inverse` — pop the referent this inverse names. -/
def applyInv (w : World) (s : Seq) : World := w.filter (fun t => !(s = t))

/-- Run a replay set against the world. -/
def replay (w : World) (ss : List Seq) : World := ss.foldl applyInv w

theorem filter_all_out {l : List Seq} : ∀ {w : List Seq}, (∀ t ∈ w, t ∈ l) →
    w.filter (fun t => !(memSeq t l)) = [] := by
  intro w
  induction w with
  | nil => intro _; rfl
  | cons a rest ih =>
    intro h
    have ha : memSeq a l = true := memSeq_iff.mpr (h a (List.mem_cons_self ..))
    simp only [List.filter_cons, ha, Bool.not_true, if_false]
    exact ih fun t ht => h t (List.mem_cons_of_mem _ ht)

theorem filter_none_out {l : List Seq} : ∀ {w : List Seq}, (∀ t ∈ w, t ∉ l) →
    w.filter (fun t => !(memSeq t l)) = w := by
  intro w
  induction w with
  | nil => intro _; rfl
  | cons a rest ih =>
    intro h
    have ha : memSeq a l = false := memSeq_false.mpr (h a (List.mem_cons_self ..))
    simp only [List.filter_cons, ha, Bool.not_false, if_true]
    exact congrArg _ (ih fun t ht => h t (List.mem_cons_of_mem _ ht))

theorem applyInv_sub {w : World} {s t : Seq} : t ∈ applyInv w s → t ∈ w := by
  intro h
  exact (List.mem_filter.mp h).1

theorem applyInv_gone {w : World} {s : Seq} : s ∉ applyInv w s := by
  intro h
  have := (List.mem_filter.mp h).2
  simp at this

theorem applyInv_fix {w : World} {s : Seq} (h : s ∉ w) : applyInv w s = w := by
  induction w with
  | nil => rfl
  | cons t rest ih =>
    have hne : s ≠ t := fun he => h (he ▸ List.mem_cons_self ..)
    have hr : s ∉ rest := fun hm => h (List.mem_cons_of_mem _ hm)
    simp only [applyInv, List.filter_cons]
    simp only [hne, decide_false, Bool.not_false, if_true]
    exact congrArg _ (ih hr)

theorem replay_sub {ss : List Seq} : ∀ {w : World} {t : Seq},
    t ∈ replay w ss → t ∈ w := by
  induction ss with
  | nil => intro w t h; exact h
  | cons s rest ih => intro w t h; exact applyInv_sub (ih h)

theorem replay_fix {ss : List Seq} : ∀ {w : World}, (∀ s ∈ ss, s ∉ w) →
    replay w ss = w := by
  induction ss with
  | nil => intro w _; rfl
  | cons s rest ih =>
    intro w h
    have hs : s ∉ w := h s (List.mem_cons_self ..)
    show replay (applyInv w s) rest = w
    rw [applyInv_fix hs]
    exact ih fun t ht => h t (List.mem_cons_of_mem _ ht)

theorem replay_erases {ss : List Seq} : ∀ {w : World} {s : Seq}, s ∈ ss →
    s ∉ replay w ss := by
  induction ss with
  | nil => intro _ _ h; cases h
  | cons t rest ih =>
    intro w s hs hmem
    cases List.mem_cons.mp hs with
    | inl he =>
      subst he
      exact applyInv_gone (replay_sub hmem)
    | inr hr => exact ih hr hmem

/-- Replay as a single filter: the world minus the whole replay set. -/
theorem replay_eq_filter : ∀ (ss : List Seq) (w : World),
    replay w ss = w.filter (fun t => !(memSeq t ss)) := by
  intro ss
  induction ss with
  | nil =>
    intro w
    show w = _
    induction w with
    | nil => rfl
    | cons a rest ih =>
      simp only [List.filter_cons, memSeq, Bool.not_false, if_true]
      exact congrArg _ ih
  | cons s rest ih =>
    intro w
    show replay (applyInv w s) rest = _
    rw [ih, applyInv, List.filter_filter]
    apply List.filter_congr
    intro t _
    simp only [memSeq]
    by_cases h : s = t
    · simp [h]
    · simp [h]

/-! ## The run, and the crash cut

A log alone cannot say anything about a crash *between* a durable write
and the effect it guards, because both the write and the effect must be
visible for the window to exist. A `Run` makes them visible: it
interleaves durable appends with the observable applies of inverses, and
a crash is `Run.take n` — every crash point is some `n`.

The reference's own reading of a torn record is what makes the prefix
model exact: "a torn fence line … is discarded by the reader exactly as
any torn record, so it implies the apply never ran"
(`backends/python/replay.py`). A half-written record is a record that is
not in the cut. -/

/-- A crash cut only ever grows: what was durable at an earlier crash
point is durable at a later one. -/
theorem mem_take_mono {α : Type} : ∀ {n m : Nat}, n ≤ m →
    ∀ {l : List α} {x : α}, x ∈ l.take n → x ∈ l.take m := by
  intro n m hnm l
  induction l generalizing n m with
  | nil => intro x h; simp at h
  | cons a rest ih =>
    intro x h
    cases n with
    | zero => simp at h
    | succ k =>
      cases m with
      | zero => exact absurd hnm (by omega)
      | succ j =>
        rw [List.take_succ_cons] at h ⊢
        cases List.mem_cons.mp h with
        | inl he => exact he ▸ List.mem_cons_self ..
        | inr hr => exact List.mem_cons_of_mem _ (ih (by omega) hr)

/-- A cut carries only what the run carries. -/
theorem mem_of_mem_take {α : Type} : ∀ {n : Nat} {l : List α} {x : α},
    x ∈ l.take n → x ∈ l := by
  intro n l
  induction l generalizing n with
  | nil => intro x h; simp at h
  | cons a rest ih =>
    intro x h
    cases n with
    | zero => simp at h
    | succ k =>
      rw [List.take_succ_cons] at h
      cases List.mem_cons.mp h with
      | inl he => exact he ▸ List.mem_cons_self ..
      | inr hr => exact List.mem_cons_of_mem _ (ih hr)

/-- One step of a running activation. -/
inductive RunStep where
  /-- A durable, `fsync`'d append (`replay.py`'s per-record
  `write`/`flush`/`fsync`, no batching). -/
  | append : Rec → RunStep
  /-- The observable application of an inverse against the world. -/
  | apply : Seq → RunStep
  deriving DecidableEq, Repr

/-- A run: steps in the order they happened. -/
abbrev Run := List RunStep

/-- What survives the crash: the durable log at this cut. -/
def durable : Run → Log
  | [] => []
  | .append r :: rest => r :: durable rest
  | .apply _ :: rest => durable rest

/-- What the outside world already saw at this cut. -/
def fired : Run → List Seq
  | [] => []
  | .apply s :: rest => s :: fired rest
  | .append _ :: rest => fired rest

/-- The fences a record makes durable. -/
def fencesOf : Rec → List Seq
  | .replayFence s => [s]
  | _ => []

/-- **Consume-before-fire** (item 309 §3a), as a property of the run:
an inverse that is not *declared* idempotent may be applied only after
its `replay-fence` is already durable. `F` is the fence set the run
starts from — the fences an earlier process left behind — so
`WAFrom idem [] r` is a fresh run.

The reference's statement: "the fence is fsync'd BEFORE the apply so a
crash between them leaves a fence and no double-apply". A declared
idempotent inverse needs no fence and writes none. -/
inductive WAFrom (idem : Seq → Bool) : List Seq → Run → Prop where
  | nil : ∀ {F}, WAFrom idem F []
  | append : ∀ {F rc rest}, WAFrom idem (fencesOf rc ++ F) rest →
      WAFrom idem F (.append rc :: rest)
  | apply : ∀ {F s rest}, (idem s = true ∨ s ∈ F) →
      WAFrom idem F rest → WAFrom idem F (.apply s :: rest)

theorem waFrom_take {idem : Seq → Bool} : ∀ {F : List Seq} {r : Run} (n : Nat),
    WAFrom idem F r → WAFrom idem F (r.take n) := by
  intro F r n h
  induction h generalizing n with
  | nil => cases n <;> exact .nil
  | @append F rc rest _ ih =>
    cases n with
    | zero => exact .nil
    | succ k => exact .append (ih k)
  | @apply F s rest hs _ ih =>
    cases n with
    | zero => exact .nil
    | succ k => exact .apply hs (ih k)

/-- **The fence is durable at every crash cut.** If an undeclared
inverse has observably fired by the cut, its fence is either one the run
started with or one the cut already carries. This is the whole content of
"a crash between the durable write and the effect leaves a fence and no
double-apply": the window has no interior a crash can land in and lose
the fence. -/
theorem fence_durable_of_fired {idem : Seq → Bool} : ∀ {F : List Seq} {r : Run} {s : Seq},
    WAFrom idem F r → idem s = false → s ∈ fired r →
    s ∈ F ∨ Rec.replayFence s ∈ durable r := by
  intro F r s h
  induction h with
  | nil => intro _ hf; cases hf
  | @append F rc rest _ ih =>
    intro hid hf
    have := ih hid hf
    rcases this with hF | hd
    · rcases List.mem_append.mp hF with hfen | hF'
      · right
        have : rc = Rec.replayFence s := by
          cases rc <;> simp [fencesOf] at hfen
          · exact congrArg _ hfen.symm
        exact this ▸ List.mem_cons_self ..
      · exact Or.inl hF'
    · exact Or.inr (List.mem_cons_of_mem _ hd)
  | @apply F t rest hs _ ih =>
    intro hid hf
    rcases List.mem_cons.mp hf with he | hr
    · subst he
      rcases hs with hidem | hF
      · rw [hid] at hidem; exact Bool.noConfusion hidem
      · exact Or.inl hF
    · exact ih hid hr

/-! ## A small-step semantics that ACCUMULATES the log

Everything above reasons about a log. Nothing above says where a log
comes from, and a theorem over a fabricated log says nothing about the
effects that actually ran. This section closes that: a body, a world, and
a step relation under which *taking* an effect step is what appends its
record. The log is then not an input — it is the trace of the run.

`Body.fail` is L-Raise's failing step. It has no step rule, and neither
does `Body.done`: both are stuck, which is what "terminal" means here.
The three effect-taking rules are the only way a configuration moves, so
`SemSteps` is a relation with genuine content rather than a function that
ignores its argument.

### Fidelity, stated honestly

* Five body forms against revl's roughly twenty. This is the fragment the
  WAL guarantees are about — a step either touches nothing, mutates with
  a declared inverse, or crosses the boundary one-way — and it is not a
  model of the language.
* A `witnessed` step appends its descriptor and creates its referent in
  ONE step. The reference logs the descriptor after the forward extern
  returns `Ok` (`backends/python/emit.py`), so the real runtime has a
  window between the mutation and its record that this model collapses. A
  mutation that never reached the log is outside every theorem here, and
  that is named rather than papered over.
* An `emit` step creates a referent whose inverse is NOT reconstructible:
  "an emission is a one-way crossing; it has no inverse"
  (`backends/python/replay.py`). That is why the abort cannot restore it
  and why it becomes residue.
* The semantics is sequential and single-activation: no cascading abort,
  no compensation drain, no escrow. -/

/-- A body, small enough to have a semantics and large enough to have a
failing step. -/
inductive Body where
  /-- Ran to completion. Stuck: terminal success. -/
  | done
  /-- L-Raise's failing step. Stuck: terminal failure. -/
  | fail
  /-- A step that touches nothing (paper Def. 48). -/
  | pure (rest : Body)
  /-- A mutation with a declared inverse (Def. 8's witnessed inverse),
  carrying item 309's declared-idempotent flag. -/
  | witnessed (seq : Seq) (undoIdem : Bool) (rest : Body)
  /-- A one-way boundary crossing. -/
  | emit (seq : Seq) (rest : Body)
  deriving DecidableEq, Repr

/-- What is left to run, the durable referents created so far (newest
first, as `DictWorld` holds them), and the log accumulated so far. -/
structure Config where
  body : Body
  world : World
  log : Log
  deriving DecidableEq, Repr

/-- The step relation. Taking an effect step is what appends its record:
the log is the trace, not a parameter. -/
inductive SemStep : Config → Config → Prop where
  | pure : ∀ {b w L}, SemStep ⟨.pure b, w, L⟩ ⟨b, w, L⟩
  | witnessed : ∀ {s i b w L},
      SemStep ⟨.witnessed s i b, w, L⟩
              ⟨b, s :: w, L ++ [.descriptor s .transactional i]⟩
  | emit : ∀ {s b w L},
      SemStep ⟨.emit s b, w, L⟩ ⟨b, s :: w, L ++ [.effect s true false]⟩

/-- Reflexive-transitive closure: a run. -/
inductive SemSteps : Config → Config → Prop where
  | refl : ∀ {c}, SemSteps c c
  | step : ∀ {a b c}, SemStep a b → SemSteps b c → SemSteps a c

/-! ### Reading the accumulated log -/

/-- Every referent the log records as created, in log order. -/
def logSeqs : Log → List Seq
  | [] => []
  | .descriptor s _ _ :: rest => s :: logSeqs rest
  | .effect s _ _ :: rest => s :: logSeqs rest
  | _ :: rest => logSeqs rest

/-- The witnessed mutations: the ones with a re-issuable inverse. -/
def witnessedSeqs : Log → List Seq
  | .descriptor s .transactional _ :: rest => s :: witnessedSeqs rest
  | _ :: rest => witnessedSeqs rest
  | [] => []

/-- The boundary crossings: one-way, no inverse. -/
def emittedSeqs : Log → List Seq
  | .effect s true false :: rest => s :: emittedSeqs rest
  | _ :: rest => emittedSeqs rest
  | [] => []

/-- A log the semantics can produce: transactional descriptors and
one-way boundary records, nothing else. Recovery bookkeeping records
(`discharge`, `replay-fence`, the session markers) are written by the
*runtime*, not by a body's steps, so they never appear in a trace. -/
inductive SemLog : Log → Prop where
  | nil : SemLog []
  | descriptor : ∀ {s i L}, SemLog L → SemLog (.descriptor s .transactional i :: L)
  | emitted : ∀ {s L}, SemLog L → SemLog (.effect s true false :: L)

/-- **The log is the trace.** A run ends with exactly the records its
effect steps appended, and in exactly the world those steps created on
top of the one it started in. Nothing appears in the log that no step
took, and no effect step fails to appear. -/
theorem steps_trace : ∀ {c d : Config}, SemSteps c d →
    ∃ M, d.log = c.log ++ M ∧ d.world = (logSeqs M).reverse ++ c.world ∧ SemLog M := by
  intro c d h
  induction h with
  | @refl c => exact ⟨[], (List.append_nil c.log).symm, rfl, .nil⟩
  | @step a b c hs _ ih =>
    obtain ⟨M, hL, hw, hM⟩ := ih
    cases hs with
    | pure => exact ⟨M, hL, hw, hM⟩
    | @witnessed s i b0 w L =>
      refine ⟨.descriptor s .transactional i :: M, ?_, ?_, .descriptor hM⟩
      · rw [hL]; simp
      · rw [hw]; simp [logSeqs]
    | @emit s b0 w L =>
      refine ⟨.effect s true false :: M, ?_, ?_, .emitted hM⟩
      · rw [hL]; simp
      · rw [hw]; simp [logSeqs]

/-! ### What a roll-back does to a log the semantics produced -/

theorem semLog_no_discharge {L : Log} (h : SemLog L) : dischargedSeqs L = [] := by
  induction h with
  | nil => rfl
  | descriptor _ ih => exact ih
  | emitted _ ih => exact ih

theorem semLog_no_fence {L : Log} (h : SemLog L) : fencedSeqs L = [] := by
  induction h with
  | nil => rfl
  | descriptor _ ih => exact ih
  | emitted _ ih => exact ih

/-- **The abort replays exactly the witnessed mutations.** On a trace,
the roll-back walk re-issues every witnessed inverse and nothing else —
in particular it does NOT re-issue an emission, which has no inverse to
re-issue. -/
theorem rollbackReplay_sem {L : Log} (h : SemLog L) :
    rollbackReplay L = witnessedSeqs L := by
  have hd := semLog_no_discharge h
  have hf := semLog_no_fence h
  show L.flatMap (reissued L) = _
  induction h with
  | nil => rfl
  | @descriptor s i M hM ih =>
    have hdM : dischargedSeqs M = [] := semLog_no_discharge hM
    have hfM : fencedSeqs M = [] := semLog_no_fence hM
    rw [List.flatMap_cons]
    simp only [reissued, hd, hf, memSeq, Bool.and_false, if_false,
      witnessedSeqs, List.cons_append, List.nil_append]
    exact congrArg _ (ih hdM hfM)
  | @emitted s M hM ih =>
    have hdM : dischargedSeqs M = [] := semLog_no_discharge hM
    have hfM : fencedSeqs M = [] := semLog_no_fence hM
    rw [List.flatMap_cons]
    simp only [reissued, Bool.and_false, if_false, witnessedSeqs, List.nil_append]
    exact ih hdM hfM

/-- On a log carrying no `discharge` and no `replay-fence` record — which
is every log a trace produces — a witnessed mutation whose inverse
re-issues cleanly is discharged. -/
theorem dispose_txn_clean {L : Log} {ok : Seq → Bool} {s : Seq} {i : Bool}
    (hd : dischargedSeqs L = []) (hf : fencedSeqs L = []) (hok : ok s = true) :
    dispose L ok (.descriptor s .transactional i) = some (s, .discharged) := by
  simp only [dispose, hd, hf, memSeq, Bool.and_false, hok]
  rfl

/-- A one-way crossing is always residue: it has no inverse to re-issue. -/
theorem dispose_emit {L : Log} {ok : Seq → Bool} {s : Seq} :
    dispose L ok (.effect s true false) = some (s, .residue .unreconstructible) := rfl

/-- **The residue surface of an abort is exactly the boundary
crossings.** On a trace, with every re-issued inverse succeeding, the
reported residue is precisely the emissions: a witnessed mutation is
discharged (its inverse ran) and a one-way crossing is reported, "never
pretending a dead lambda ran". -/
theorem reported_sem {L : Log} (h : SemLog L) :
    reported L (fun _ => true) = emittedSeqs L := by
  have hd := semLog_no_discharge h
  have hf := semLog_no_fence h
  show L.filterMap _ = _
  -- `dispose` closes over the whole log, so induct on a separate `M`
  -- while `L` (and its two emptiness facts) stay fixed.
  suffices hgen : ∀ {M : Log}, SemLog M →
      M.filterMap (fun r =>
        match dispose L (fun _ => true) r with
        | some (s, .residue _) => some s
        | _ => none) = emittedSeqs M from hgen h
  intro M hM
  induction hM with
  | nil => rfl
  | @descriptor s i N _ ih =>
    rw [List.filterMap_cons]
    rw [dispose_txn_clean hd hf rfl]
    simp only [emittedSeqs]
    exact ih
  | @emitted s N _ ih =>
    rw [List.filterMap_cons, dispose_emit]
    simp only [emittedSeqs]
    exact congrArg _ ih

/-- Witnessed seqs are log seqs. -/
theorem witnessedSeqs_sub {L : Log} : ∀ {s}, s ∈ witnessedSeqs L → s ∈ logSeqs L := by
  induction L with
  | nil => intro s h; cases h
  | cons x rest ih =>
    intro s h
    cases x with
    | descriptor t e i =>
      cases e with
      | transactional =>
        rcases List.mem_cons.mp h with he | hr
        · exact he ▸ List.mem_cons_self ..
        · exact List.mem_cons_of_mem _ (ih hr)
      | compensation => exact List.mem_cons_of_mem _ (ih h)
    | effect t b r => exact List.mem_cons_of_mem _ (ih h)
    | _ => exact ih h

/-- Emitted seqs are log seqs. -/
theorem emittedSeqs_sub {L : Log} : ∀ {s}, s ∈ emittedSeqs L → s ∈ logSeqs L := by
  induction L with
  | nil => intro s h; cases h
  | cons x rest ih =>
    intro s h
    cases x with
    | effect t b r =>
      cases b <;> cases r <;> first
        | exact List.mem_cons_of_mem _ (ih h)
        | (rcases List.mem_cons.mp h with he | hr
           · exact he ▸ List.mem_cons_self ..
           · exact List.mem_cons_of_mem _ (ih hr))
    | descriptor t e i => exact List.mem_cons_of_mem _ (ih h)
    | _ => exact ih h

/-- On a trace, a log seq is either witnessed or emitted: there is no
third kind of record. -/
theorem logSeqs_split {L : Log} (h : SemLog L) : ∀ {s}, s ∈ logSeqs L →
    s ∈ witnessedSeqs L ∨ s ∈ emittedSeqs L := by
  induction h with
  | nil => intro s hs; cases hs
  | @descriptor t i M _ ih =>
    intro s hs
    rcases List.mem_cons.mp hs with he | hr
    · exact Or.inl (he ▸ List.mem_cons_self ..)
    · exact (ih hr).imp (List.mem_cons_of_mem _) id
  | @emitted t M _ ih =>
    intro s hs
    rcases List.mem_cons.mp hs with he | hr
    · exact Or.inr (he ▸ List.mem_cons_self ..)
    · exact (ih hr).imp id (List.mem_cons_of_mem _)

/-- **Filtering the trace's referents by the replay set leaves exactly
the crossings.** `W` is held fixed (it is the whole log's witnessed set)
while the induction walks the trace, which is what lets the statement be
about one log rather than about each suffix. -/
theorem logSeqs_filter_gen {W : List Seq} : ∀ {M : Log}, SemLog M →
    (∀ s ∈ witnessedSeqs M, s ∈ W) → (∀ s ∈ emittedSeqs M, s ∉ W) →
    (logSeqs M).filter (fun t => !(memSeq t W)) = emittedSeqs M := by
  intro M hM
  induction hM with
  | nil => intro _ _; rfl
  | @descriptor s i N _ ih =>
    intro hw he
    have hs : s ∈ W := hw s (List.mem_cons_self ..)
    have hsW : memSeq s W = true := memSeq_iff.mpr hs
    simp only [logSeqs, List.filter_cons, hsW, Bool.not_true, if_false, emittedSeqs]
    exact ih (fun t ht => hw t (List.mem_cons_of_mem _ ht)) he
  | @emitted s N _ ih =>
    intro hw he
    have hs : s ∉ W := he s (List.mem_cons_self ..)
    have hsW : memSeq s W = false := memSeq_false.mpr hs
    simp only [logSeqs, List.filter_cons, hsW, Bool.not_false, if_true, emittedSeqs]
    exact congrArg _ (ih hw (fun t ht => he t (List.mem_cons_of_mem _ ht)))

end RevL.Lemmas
