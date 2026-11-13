import RevL.Lemmas.WalLemmas

/-
RevL.Theorems.ParallelFanout: a two-member parallel fan-out group, stated over
the shipped `SemStep` (issue #2741).

`docs/design/2741-parallel-fanout-formalism.md` chooses an interleaving
extension of the sequential small-step relation. This file is that extension
and the first theorems over it.

`ParStep` runs two member bodies against ONE shared world and ONE shared log.
A step is either member taking a `SemStep` on that shared state, so the two
members observe each other through it and the log is the trace of whichever
interleaving ran. `SemStep` itself is not edited, so G7 and A8 keep their
statements.

What is stated:

* `par_run_is_a_sequential_run`: every interleaving that runs both members to
  completion ends in the world and log of a `SemSteps` run of the two bodies in
  sequence, in one order or the other. A failing member (`Body.fail`, which is
  stuck) does not stop the other member, so the order that matches puts the
  failing member last.
* `faulted_group_leaves_exactly_the_residue` and
  `faulted_witnessed_group_reverts`: the abort that follows a faulted group is
  R4's, applied to that sequential run. What the abort leaves is exactly the
  members that crossed the boundary, and a group whose members only made
  witnessed mutations is reverted exactly.
* `over_fired_witnessed_member_is_undone` and
  `over_fired_emission_is_residue`: a member the plan-order sequential run
  skips because an earlier member failed still fires in the parallel run. When
  it has a declared inverse the abort undoes it and the two worlds agree; when
  it is a one-way emission it stays, and the two worlds differ.

What is NOT stated here, and why:

* Forward idempotence and compensation (item 247). `Body` has no form for an
  emission carrying a compensation or a forward-idempotence flag, and
  `SemStep` has no Phase-2 compensation drain (`WalLemmas.lean`, "Fidelity").
  `Body.witnessed` carries `undoIdem`, but no theorem here uses it. The
  idempotence conjuncts of `_group_eligible` are therefore not modelled: stating
  them needs a new `Body` form, which would change `WalLemmas.lean` and every
  match over `Body`.
* A member that lands and then faults, a divert, nested groups and more than two
  members.
-/

namespace RevL.Fanout

open RevL.Lemmas

/-! ## The interleaving extension of `SemStep` -/

/-- A group configuration: two member bodies and the ONE world and log they
share. -/
structure GConf where
  b1 : Body
  b2 : Body
  world : World
  log : Log
  deriving DecidableEq, Repr

/-- A group step: either member takes a `SemStep` on the shared world and log.
No constructor is added to `SemStep`. -/
inductive ParStep : GConf → GConf → Prop where
  | left : ∀ {a b c : Body} {w w' : World} {L L' : Log},
      SemStep ⟨a, w, L⟩ ⟨b, w', L'⟩ → ParStep ⟨a, c, w, L⟩ ⟨b, c, w', L'⟩
  | right : ∀ {a c d : Body} {w w' : World} {L L' : Log},
      SemStep ⟨c, w, L⟩ ⟨d, w', L'⟩ → ParStep ⟨a, c, w, L⟩ ⟨a, d, w', L'⟩

/-- A group run: the reflexive-transitive closure of `ParStep`. -/
inductive ParSteps : GConf → GConf → Prop where
  | refl : ∀ {g}, ParSteps g g
  | step : ∀ {g h k}, ParStep g h → ParSteps h k → ParSteps g k

/-! ## Members -/

/-- A group member: it fails before its effect, or takes ONE effect step and
completes. (A member that lands and then faults is out of scope.) -/
inductive Single : Body → Prop where
  | fail : Single .fail
  | witnessed (s : Seq) (i : Bool) : Single (.witnessed s i .done)
  | emit (s : Seq) : Single (.emit s .done)

/-- Run `b`, then `k` in place of its normal completion. `fail` is stuck, so
`k` never runs after it. -/
def seqBody : Body → Body → Body
  | .done, k => k
  | .fail, _ => .fail
  | .pure r, k => .pure (seqBody r k)
  | .witnessed s i r, k => .witnessed s i (seqBody r k)
  | .emit s r, k => .emit s (seqBody r k)
  | .deferred s r, k => .deferred s (seqBody r k)

/-! ## Every interleaving is a sequential run -/

/-- A group whose two members are both stuck takes no step. -/
private theorem par_stuck {b1 b2 : Body} {w : World} {L : Log} {g : GConf}
    (h1 : b1 = .done ∨ b1 = .fail) (h2 : b2 = .done ∨ b2 = .fail)
    (hs : ParSteps ⟨b1, b2, w, L⟩ g) : g = ⟨b1, b2, w, L⟩ := by
  rcases hs with _ | ⟨hst, _⟩
  · rfl
  · cases hst with
    | left h => rcases h1 with rfl | rfl <;> cases h
    | right h => rcases h2 with rfl | rfl <;> cases h

/-- **Interleaving soundness.** Run two members, over one shared world and
log, to completion in any interleaving. The world and log it ends in are those
of a `SemSteps` run of the two bodies in sequence, in plan order or in the
reverse order, and that run fails exactly when a member failed. When a member
fails, the order that matches puts it last: the other member still fired. -/
theorem par_run_is_a_sequential_run {b1 b2 c1 c2 : Body} {w : World} {L : Log}
    (h1 : Single b1) (h2 : Single b2)
    (hs : ParSteps ⟨b1, b2, [], []⟩ ⟨c1, c2, w, L⟩)
    (ht1 : c1 = .done ∨ c1 = .fail) (ht2 : c2 = .done ∨ c2 = .fail) :
    ∃ t : Body,
      (SemSteps ⟨seqBody b1 b2, [], []⟩ ⟨t, w, L⟩ ∨
        SemSteps ⟨seqBody b2 b1, [], []⟩ ⟨t, w, L⟩) ∧
      (t = .fail ↔ (c1 = .fail ∨ c2 = .fail)) := by
  rcases h1 with _ | ⟨s1, i1⟩ | ⟨s1⟩ <;> rcases h2 with _ | ⟨s2, i2⟩ | ⟨s2⟩
  · -- member 1 F, member 2 F
    have hg := par_stuck (Or.inr rfl) (Or.inr rfl) hs
    cases hg
    exact ⟨.fail, Or.inl .refl, by simp⟩
  · -- member 1 F, member 2 W
    rcases hs with _ | ⟨hst, hs⟩
    · exfalso
      rcases ht2 with h | h <;> cases h
    · cases hst with
      | left h =>
        cases h
      | right h =>
        cases h
        have hg := par_stuck (Or.inr rfl) (Or.inl rfl) hs
        cases hg
        exact ⟨.fail, Or.inr (.step .witnessed .refl), by simp⟩
  · -- member 1 F, member 2 E
    rcases hs with _ | ⟨hst, hs⟩
    · exfalso
      rcases ht2 with h | h <;> cases h
    · cases hst with
      | left h =>
        cases h
      | right h =>
        cases h
        have hg := par_stuck (Or.inr rfl) (Or.inl rfl) hs
        cases hg
        exact ⟨.fail, Or.inr (.step .emit .refl), by simp⟩
  · -- member 1 W, member 2 F
    rcases hs with _ | ⟨hst, hs⟩
    · exfalso
      rcases ht1 with h | h <;> cases h
    · cases hst with
      | left h =>
        cases h
        have hg := par_stuck (Or.inl rfl) (Or.inr rfl) hs
        cases hg
        exact ⟨.fail, Or.inl (.step .witnessed .refl), by simp⟩
      | right h =>
        cases h
  · -- member 1 W, member 2 W
    rcases hs with _ | ⟨hst, hs⟩
    · exfalso
      rcases ht1 with h | h <;> cases h
    · cases hst with
      | left h =>
        cases h
        rcases hs with _ | ⟨hst, hs⟩
        · exfalso
          rcases ht2 with h | h <;> cases h
        · cases hst with
          | left h =>
            cases h
          | right h =>
            cases h
            have hg := par_stuck (Or.inl rfl) (Or.inl rfl) hs
            cases hg
            exact ⟨.done, Or.inl (.step .witnessed (.step .witnessed .refl)), by simp⟩
      | right h =>
        cases h
        rcases hs with _ | ⟨hst, hs⟩
        · exfalso
          rcases ht1 with h | h <;> cases h
        · cases hst with
          | left h =>
            cases h
            have hg := par_stuck (Or.inl rfl) (Or.inl rfl) hs
            cases hg
            exact ⟨.done, Or.inr (.step .witnessed (.step .witnessed .refl)), by simp⟩
          | right h =>
            cases h
  · -- member 1 W, member 2 E
    rcases hs with _ | ⟨hst, hs⟩
    · exfalso
      rcases ht1 with h | h <;> cases h
    · cases hst with
      | left h =>
        cases h
        rcases hs with _ | ⟨hst, hs⟩
        · exfalso
          rcases ht2 with h | h <;> cases h
        · cases hst with
          | left h =>
            cases h
          | right h =>
            cases h
            have hg := par_stuck (Or.inl rfl) (Or.inl rfl) hs
            cases hg
            exact ⟨.done, Or.inl (.step .witnessed (.step .emit .refl)), by simp⟩
      | right h =>
        cases h
        rcases hs with _ | ⟨hst, hs⟩
        · exfalso
          rcases ht1 with h | h <;> cases h
        · cases hst with
          | left h =>
            cases h
            have hg := par_stuck (Or.inl rfl) (Or.inl rfl) hs
            cases hg
            exact ⟨.done, Or.inr (.step .emit (.step .witnessed .refl)), by simp⟩
          | right h =>
            cases h
  · -- member 1 E, member 2 F
    rcases hs with _ | ⟨hst, hs⟩
    · exfalso
      rcases ht1 with h | h <;> cases h
    · cases hst with
      | left h =>
        cases h
        have hg := par_stuck (Or.inl rfl) (Or.inr rfl) hs
        cases hg
        exact ⟨.fail, Or.inl (.step .emit .refl), by simp⟩
      | right h =>
        cases h
  · -- member 1 E, member 2 W
    rcases hs with _ | ⟨hst, hs⟩
    · exfalso
      rcases ht1 with h | h <;> cases h
    · cases hst with
      | left h =>
        cases h
        rcases hs with _ | ⟨hst, hs⟩
        · exfalso
          rcases ht2 with h | h <;> cases h
        · cases hst with
          | left h =>
            cases h
          | right h =>
            cases h
            have hg := par_stuck (Or.inl rfl) (Or.inl rfl) hs
            cases hg
            exact ⟨.done, Or.inl (.step .emit (.step .witnessed .refl)), by simp⟩
      | right h =>
        cases h
        rcases hs with _ | ⟨hst, hs⟩
        · exfalso
          rcases ht1 with h | h <;> cases h
        · cases hst with
          | left h =>
            cases h
            have hg := par_stuck (Or.inl rfl) (Or.inl rfl) hs
            cases hg
            exact ⟨.done, Or.inr (.step .witnessed (.step .emit .refl)), by simp⟩
          | right h =>
            cases h
  · -- member 1 E, member 2 E
    rcases hs with _ | ⟨hst, hs⟩
    · exfalso
      rcases ht1 with h | h <;> cases h
    · cases hst with
      | left h =>
        cases h
        rcases hs with _ | ⟨hst, hs⟩
        · exfalso
          rcases ht2 with h | h <;> cases h
        · cases hst with
          | left h =>
            cases h
          | right h =>
            cases h
            have hg := par_stuck (Or.inl rfl) (Or.inl rfl) hs
            cases hg
            exact ⟨.done, Or.inl (.step .emit (.step .emit .refl)), by simp⟩
      | right h =>
        cases h
        rcases hs with _ | ⟨hst, hs⟩
        · exfalso
          rcases ht1 with h | h <;> cases h
        · cases hst with
          | left h =>
            cases h
            have hg := par_stuck (Or.inl rfl) (Or.inl rfl) hs
            cases hg
            exact ⟨.done, Or.inr (.step .emit (.step .emit .refl)), by simp⟩
          | right h =>
            cases h

/-! ## The abort after a group -/

/-- What the abort leaves of a run from the empty world: the world minus the
witnessed inverses it replays is exactly the emissions, newest first. This is
R4's equation, stated from the empty world and proved from the same farm
lemmas. A theorem file cannot import another, so it is not cited. -/
private theorem abort_world {b t : Body} {w : World} {L : Log}
    (hs : SemSteps ⟨b, [], []⟩ ⟨t, w, L⟩)
    (hdisj : ∀ s ∈ emittedSeqs L, s ∉ witnessedSeqs L) :
    replay w (rollbackReplay L) = (emittedSeqs L).reverse := by
  obtain ⟨M, hL, hw, hM⟩ := steps_trace hs
  simp only [List.nil_append] at hL
  subst hL
  have hw' : w = (logSeqs L).reverse := by simpa using hw
  rw [rollbackReplay_sem hM, replay_eq_filter, hw', List.filter_reverse,
    logSeqs_filter_gen hM (fun _ h => h) hdisj]

/-- **The abort after a group leaves exactly the emissions.** Take any
interleaving that runs both members to completion. The abort that follows
replays every witnessed inverse and nothing else, so what remains in the world
is exactly the members that crossed the boundary: they have no inverse to
replay. The argument is `par_run_is_a_sequential_run` followed by R4's equation
on the sequential run it supplies. -/
theorem group_abort_leaves_exactly_the_emissions {b1 b2 c1 c2 : Body}
    {w : World} {L : Log}
    (h1 : Single b1) (h2 : Single b2)
    (hs : ParSteps ⟨b1, b2, [], []⟩ ⟨c1, c2, w, L⟩)
    (ht1 : c1 = .done ∨ c1 = .fail) (ht2 : c2 = .done ∨ c2 = .fail)
    (hdisj : ∀ s ∈ emittedSeqs L, s ∉ witnessedSeqs L) :
    replay w (rollbackReplay L) = (emittedSeqs L).reverse := by
  obtain ⟨t, hrun, _⟩ := par_run_is_a_sequential_run h1 h2 hs ht1 ht2
  rcases hrun with hrun | hrun <;> exact abort_world hrun hdisj

/-- **A group that crossed nothing is reverted exactly.** If no member made a
one-way crossing, the abort restores the empty world the group started in, in
every interleaving. -/
theorem group_abort_reverts_when_nothing_crossed {b1 b2 c1 c2 : Body}
    {w : World} {L : Log}
    (h1 : Single b1) (h2 : Single b2)
    (hs : ParSteps ⟨b1, b2, [], []⟩ ⟨c1, c2, w, L⟩)
    (ht1 : c1 = .done ∨ c1 = .fail) (ht2 : c2 = .done ∨ c2 = .fail)
    (hno : emittedSeqs L = []) :
    replay w (rollbackReplay L) = [] := by
  have hdisj : ∀ s ∈ emittedSeqs L, s ∉ witnessedSeqs L := by
    intro s hsm; rw [hno] at hsm; cases hsm
  rw [group_abort_leaves_exactly_the_emissions h1 h2 hs ht1 ht2 hdisj, hno]
  rfl

/-! ## Non-vacuity and the reason the inverse matters -/

/-- A descriptor record, as `SemStep.witnessed` appends it. -/
def d (s : Seq) : Rec := .descriptor s .transactional true

/-- **Both interleavings are taken.** Two witnessed members reach two
different final worlds and logs, one per order, and neither is the plan-order
one only. So `par_run_is_a_sequential_run` quantifies over a real choice, and
its two disjuncts are both inhabited. -/
theorem both_interleavings_are_taken :
    Single (.witnessed 1 true .done) ∧ Single (.witnessed 2 true .done) ∧
    ParSteps ⟨.witnessed 1 true .done, .witnessed 2 true .done, [], []⟩
      ⟨.done, .done, [2, 1], [d 1, d 2]⟩ ∧
    ParSteps ⟨.witnessed 1 true .done, .witnessed 2 true .done, [], []⟩
      ⟨.done, .done, [1, 2], [d 2, d 1]⟩ ∧
    ((⟨.done, .done, [2, 1], [d 1, d 2]⟩ : GConf) ≠ ⟨.done, .done, [1, 2], [d 2, d 1]⟩) :=
  ⟨.witnessed 1 true, .witnessed 2 true,
    .step (.left .witnessed) (.step (.right .witnessed) .refl),
    .step (.right .witnessed) (.step (.left .witnessed) .refl), by decide⟩

/-- **A member the plan-order run skips fires in the parallel run, and its
inverse undoes it.** Member 1 fails. Plan order never reaches member 2, so
that run ends in the empty world. The parallel run fires member 2 anyway, so
world 2 is out before the abort. Member 2 made a witnessed mutation, so the
abort replays its inverse and the two worlds agree. -/
theorem over_fired_witnessed_member_is_undone :
    ParSteps ⟨.fail, .witnessed 2 true .done, [], []⟩
      ⟨.fail, .done, [2], [d 2]⟩ ∧
    SemSteps ⟨seqBody .fail (.witnessed 2 true .done), [], []⟩ ⟨.fail, [], []⟩ ∧
    (2 : Seq) ∈ ([2] : World) ∧
    replay [2] (rollbackReplay [d 2]) = replay [] (rollbackReplay []) := by
  refine ⟨.step (.right .witnessed) .refl, .refl, List.mem_cons_self .., ?_⟩
  decide

/-- **The same, with a one-way emission, leaves residue.** Member 2 is an
emission. It fired in the parallel run and has no inverse, so the abort cannot
undo it: world 2 stays out, while the plan-order run's world is empty. The
two worlds differ. -/
theorem over_fired_emission_is_residue :
    ParSteps ⟨.fail, .emit 2 .done, [], []⟩
      ⟨.fail, .done, [2], [.effect 2 true false false]⟩ ∧
    SemSteps ⟨seqBody .fail (.emit 2 .done), [], []⟩ ⟨.fail, [], []⟩ ∧
    replay [2] (rollbackReplay [.effect 2 true false false]) = [2] ∧
    replay [2] (rollbackReplay [.effect 2 true false false]) ≠
      replay [] (rollbackReplay []) := by
  refine ⟨.step (.right .emit) .refl, .refl, ?_, ?_⟩ <;> decide

end RevL.Fanout
