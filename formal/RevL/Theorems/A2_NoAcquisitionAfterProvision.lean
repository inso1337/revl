import RevL.Semantics

/-!
# A2: no acquisition after a provision, stated over the LIFO stack

`docs/rejections.md#a2` and `docs/contract-errata.md`: an effect
acquired after `provide` would be reverted while dependents can still call
the service. The checker refuses the shape at lowering
(`src/revl/lower.py`, `_dispatch_action`): once `provide_seen_line` is set
by the first `provide` block, a `LetEffect`, an `EffectStmt`, a `TimerStmt`
or an `every … in` stream iteration (`StreamIterStmt`) is refused with
code A2. `examples/rejections/a2_acquire_after_provide.rvl` is the fixture.

## Why this is a theorem over G7 and not a lint

Teardown is LIFO over the activation's stack (`RevL.Semantics.phase1`), and
a provision's withdrawal is an entry on that stack exactly like an
acquisition's release. So an acquisition registered AFTER a provision is
released BEFORE the provision is withdrawn, and in that window a dependent
can still reach the provide-method through the withdrawal guard and call it
against a released handle. A2 is the ordering condition under which the
window cannot exist: with every acquisition above the first `provide`, the
Phase-1 proof pass is every withdrawal, then every release.

## The model

An activation body is an ordered list of `Step`s: `acquire` (the four
statement forms the checker refuses after a provision), `provide`, and
`other` (everything else — a pure `let`, an `emit`, a `fail`, an `if`
guard, an `await`, a prelude declaration; none of them moves the
checker's flag, and a component `if` arm admits only `fail` and nested
`if`, so no acquisition can hide inside one). `a2B` is the checker's fold
verbatim: a flag set at the first `provide`, an acquisition refused while
it is set. `A2OK` is the same rule declaratively — no `provide` is ever
followed by an `acquire` — and `a2B_iff` is the bridge the oracle's `A2`
row rests on.

`stack` is the body's registrations as a `RevL.Semantics` stack: one
`bracket` entry per acquisition (its release) and one per provision (its
withdrawal), each labelled in the `inverse` slot the way the oracle's `D`
row labels a scenario's entries, so the model's own `phase1` / `teardown`
transport the label and the ordering claim is read off the model's list,
not off a parallel bookkeeping list.

## What this does not cover

The step list is the activation body as the checker walks it, not the
runtime's stack: a provide-method body registers entries of its own at
call time (the `method` seam of the G7 corpus), and those are outside this
model. Nothing here says a provision IS withdrawn as a bracket at runtime;
it says that IF the withdrawal sits on the same LIFO stack as the releases
— which is the premise of the rule — then A2 is exactly what puts every
withdrawal before every release.
-/

namespace RevL.A2

open RevL.Semantics RevL.Syntax

/-! ### The ordered body, and the checker's rule -/

/-- One statement of an activation body, as the A2 rule sees it. -/
inductive Step where
  /-- `let x = effect … undo …`, a bare `effect … undo …`, a timer
  (`every <duration> { … }`), or a stream iteration (`every x in s { … }`):
  the four forms `lower._dispatch_action` refuses once a provision has
  been seen. Each registers a release on the activation's stack. -/
  | acquire
  /-- A `provide <key> { … }` block. Sets the checker's flag; registers a
  withdrawal on the activation's stack. -/
  | provide
  /-- Any other body statement. Moves nothing. -/
  | other
  deriving Repr, DecidableEq

/-- The checker's fold, verbatim. `seen` is `provide_seen_line is not
None`: it is set by a `provide` and never cleared, and an acquisition with
it set is the refusal. -/
def a2Fold : Bool → List Step → Bool
  | _, [] => true
  | seen, .acquire :: rest => !seen && a2Fold seen rest
  | _, .provide :: rest => a2Fold true rest
  | seen, .other :: rest => a2Fold seen rest

/-- The A2 verdict of a body: the checker's fold from a clear flag. -/
def a2B (body : List Step) : Bool := a2Fold false body

/-- The rule, declaratively: no `provide` is followed, at any distance, by
an `acquire`. `List.Pairwise R l` is `R l[i] l[j]` for every `i < j`. -/
def A2OK (body : List Step) : Prop :=
  List.Pairwise (fun a b => a = .provide → b ≠ .acquire) body

/-- The fold with its flag SET is the rule plus "no acquisition at all":
the flag remembers a provision the list no longer shows. This is the
induction-friendly form; `a2B_iff` is its `seen = false` instance. -/
theorem a2Fold_iff (seen : Bool) (body : List Step) :
    a2Fold seen body = true ↔
      ((seen = true → .acquire ∉ body) ∧ A2OK body) := by
  induction body generalizing seen with
  | nil => simp [a2Fold, A2OK]
  | cons s rest ih =>
    cases s with
    | acquire =>
      cases seen with
      | true => simp [a2Fold]
      | false =>
        simp only [a2Fold, Bool.not_false, Bool.true_and]
        rw [ih false]
        simp [A2OK]
    | provide =>
      simp only [a2Fold]
      rw [ih true]
      simp only [A2OK, List.pairwise_cons, List.mem_cons, not_or, ne_eq, forall_const]
      constructor
      · rintro ⟨hna, hp⟩
        exact ⟨fun _ => ⟨by decide, hna⟩, fun a' ha' heq => hna (heq ▸ ha'), hp⟩
      · rintro ⟨_, hall, hp⟩
        exact ⟨fun hm => hall _ hm rfl, hp⟩
    | other =>
      simp only [a2Fold]
      rw [ih seen]
      simp [A2OK]

/-- **The A2 verdict is the rule.** The oracle's `A2` row prints `a2B`;
this is what makes the printed Bool the model's judgment. -/
theorem a2B_iff (body : List Step) : a2B body = true ↔ A2OK body := by
  rw [a2B, a2Fold_iff]
  simp

/-! ### The body's registrations as a `RevL.Semantics` stack -/

/-- The label a release carries in its `inverse` slot. -/
def releaseLabel : Expr := .lit "release"

/-- The label a withdrawal carries in its `inverse` slot. -/
def withdrawalLabel : Expr := .lit "withdraw"

/-- An acquisition's stack entry: a `bracket` whose inverse releases the
handle. -/
def release : LogEntry := { kind := .bracket, inverse := releaseLabel }

/-- A provision's stack entry: a `bracket` whose inverse withdraws the
provision (the withdrawal guard is armed for good). -/
def withdrawal : LogEntry := { kind := .bracket, inverse := withdrawalLabel }

/-- What one body step registers. -/
def entryOf : Step → List LogEntry
  | .acquire => [release]
  | .provide => [withdrawal]
  | .other => []

/-- The activation's stack, in registration order: the body's steps, each
replaced by what it registers. -/
def stack (body : List Step) : List LogEntry := body.flatMap entryOf

/-- How many acquisitions a body makes. -/
def acquisitions : List Step → Nat
  | [] => 0
  | .acquire :: rest => acquisitions rest + 1
  | _ :: rest => acquisitions rest

/-- How many provisions a body makes. -/
def provisions : List Step → Nat
  | [] => 0
  | .provide :: rest => provisions rest + 1
  | _ :: rest => provisions rest

/-- The two labels are different labels. -/
theorem labels_distinct : releaseLabel ≠ withdrawalLabel := by
  intro h
  simp [releaseLabel, withdrawalLabel] at h

/-- Every entry the body registers is a `bracket`: an acquisition's release
and a provision's withdrawal both replay under every settling verdict. -/
theorem stack_all_brackets (body : List Step) :
    ∀ e ∈ stack body, e.kind = .bracket := by
  intro e he
  simp only [stack, List.mem_flatMap] at he
  obtain ⟨s, _, hs⟩ := he
  cases s <;> simp_all [entryOf, release, withdrawal]

/-- Under a settling verdict the Phase-1 pass over an all-bracket stack is
the whole stack, LIFO. -/
theorem phase1_of_brackets (v : Verdict) (hv : v.settles = true)
    (log : List LogEntry) (hk : ∀ e ∈ log, e.kind = .bracket) :
    phase1 v log = log.reverse := by
  unfold phase1
  rw [List.filter_eq_self.mpr]
  intro e he
  rw [hk e he]
  cases v <;> simp_all [EntryKind.inPhase1, EntryKind.replaysUnder, Verdict.settles]

/-- A body with no acquisition registers only withdrawals. -/
theorem stack_of_no_acquire (body : List Step) (h : .acquire ∉ body) :
    stack body = List.replicate (provisions body) withdrawal
      ∧ acquisitions body = 0 := by
  induction body with
  | nil => exact ⟨rfl, rfl⟩
  | cons s rest ih =>
    have hr : Step.acquire ∉ rest := fun hm => h (List.mem_cons_of_mem s hm)
    obtain ⟨h1, h2⟩ := ih hr
    cases s with
    | acquire => exact absurd List.mem_cons_self h
    | provide =>
      refine ⟨?_, h2⟩
      show withdrawal :: stack rest = List.replicate (provisions rest + 1) withdrawal
      rw [h1]
      rfl
    | other =>
      exact ⟨h1, h2⟩

/-- **Under A2 the stack is every release, then every withdrawal.** The
rule is exactly the statement that the registration order puts all
acquisitions below the first provision. -/
theorem stack_shape (body : List Step) (h : A2OK body) :
    stack body = List.replicate (acquisitions body) release
      ++ List.replicate (provisions body) withdrawal := by
  induction body with
  | nil => rfl
  | cons s rest ih =>
    rw [A2OK, List.pairwise_cons] at h
    obtain ⟨hs, hrest⟩ := h
    cases s with
    | acquire =>
      show release :: stack rest = List.replicate (acquisitions rest + 1) release
        ++ List.replicate (provisions rest) withdrawal
      rw [ih hrest]
      rfl
    | provide =>
      have hna : Step.acquire ∉ rest := fun hm => hs _ hm rfl rfl
      obtain ⟨h1, h2⟩ := stack_of_no_acquire rest hna
      show withdrawal :: stack rest = List.replicate (acquisitions rest) release
        ++ List.replicate (provisions rest + 1) withdrawal
      rw [h1, h2]
      rfl
    | other =>
      exact ih hrest

/-! ### The content theorem: every withdrawal precedes every release -/

/-- **The Phase-1 proof pass is every withdrawal, then every release.**
Under A2 and any settling verdict, `RevL.Semantics.phase1` over the body's
stack runs the withdrawals first — all of them — and only then the
releases. So no dependent can reach a provide-method against a released
handle: by the time the first release runs, no provision is still
callable. The settling hypothesis is load-bearing: the E-Stop runs
nothing (`a2_not_vacuous` computes both). -/
theorem proof_pass_is_withdrawals_then_releases (body : List Step)
    (h : A2OK body) (v : Verdict) (hv : v.settles = true) :
    phase1 v (stack body) =
      List.replicate (provisions body) withdrawal
        ++ List.replicate (acquisitions body) release := by
  rw [phase1_of_brackets v hv _ (stack_all_brackets body), stack_shape body h,
    List.reverse_append, List.reverse_replicate, List.reverse_replicate]

/-- The ordering claim on a replay list: once a release has run, nothing
that runs after it is a withdrawal — equivalently, every withdrawal
precedes every release. -/
def WithdrawalsBeforeReleases (l : List LogEntry) : Prop :=
  List.Pairwise (fun a b => a.inverse = releaseLabel → b.inverse ≠ withdrawalLabel) l

/-- **A2, as the guarantee reads**: under the rule, every withdrawal
precedes every release in the Phase-1 pass, under every settling
verdict. -/
theorem withdrawals_precede_releases (body : List Step) (h : A2OK body)
    (v : Verdict) (hv : v.settles = true) :
    WithdrawalsBeforeReleases (phase1 v (stack body)) := by
  rw [WithdrawalsBeforeReleases, proof_pass_is_withdrawals_then_releases body h v hv]
  refine List.pairwise_append.mpr ⟨?_, ?_, ?_⟩
  · exact List.pairwise_replicate.mpr (Or.inr fun hw => absurd hw.symm labels_distinct)
  · exact List.pairwise_replicate.mpr (Or.inr fun _ => labels_distinct)
  · intro a ha b hb
    rw [(List.mem_replicate.mp ha).2, (List.mem_replicate.mp hb).2]
    exact fun hw => absurd hw.symm labels_distinct

/-- The same claim read off the model's `teardown` — the label list the
oracle's `D` row would print for this stack: the withdrawal labels, then
the release labels, and no compensation drain (the stack has none). -/
theorem teardown_labels (body : List Step) (h : A2OK body)
    (v : Verdict) (hv : v.settles = true) :
    teardown v (stack body) =
      List.replicate (provisions body) withdrawalLabel
        ++ List.replicate (acquisitions body) releaseLabel := by
  have hp2 : phase2 v (stack body) = [] := by
    unfold phase2
    rw [List.filter_eq_nil_iff.mpr, List.reverse_nil]
    intro e he
    rw [stack_all_brackets body e he]
    cases v <;> simp [EntryKind.inPhase2, EntryKind.inPhase1]
  rw [teardown, replayed, hp2, List.append_nil,
    proof_pass_is_withdrawals_then_releases body h v hv,
    List.map_append, List.map_replicate, List.map_replicate]
  rfl

/-! ### The converse: the fixture's shape opens the window -/

/-- `examples/rejections/a2_acquire_after_provide.rvl`, `BadOrder`: a
`Map.new()` acquisition, `provide cache`, then a second acquisition. -/
def fixtureBody : List Step := [.acquire, .provide, .acquire]

/-- The checker refuses the fixture, and so does the fold. -/
theorem fixture_refused : a2B fixtureBody = false := by
  rfl

/-- On the fixture's stack every settling verdict runs the second
acquisition's release FIRST — before the withdrawal — while `cache` is
still callable. This is the window the rule closes. -/
theorem fixture_release_before_withdrawal (v : Verdict) (hv : v.settles = true) :
    phase1 v (stack fixtureBody) = [release, withdrawal, release] := by
  cases v with
  | commit => rfl
  | abort => rfl
  | halted => simp [Verdict.settles] at hv

/-- The fixture's replay violates the ordering claim: a release runs, and
a withdrawal runs after it. So `withdrawals_precede_releases` is not true
of every body — its `A2OK` hypothesis is what excludes this one. -/
theorem fixture_opens_the_window :
    ¬ WithdrawalsBeforeReleases (phase1 .commit (stack fixtureBody)) := by
  rw [fixture_release_before_withdrawal .commit rfl, WithdrawalsBeforeReleases,
    List.pairwise_cons]
  intro ⟨hfirst, _⟩
  exact hfirst withdrawal (by simp) rfl rfl

/-! ### Non-vacuity -/

/-- The admitted shape: one acquisition, then one provision — the ordinary
`let store = effect Map.new() undo store.drop(); provide cache { … }` body.
The rule admits it, the fold agrees, the stack is `[release, withdrawal]`,
and the commit proof pass runs the withdrawal first. The E-Stop runs
nothing, which is why `proof_pass_is_withdrawals_then_releases` carries
its settling hypothesis. -/
def okBody : List Step := [.acquire, .provide]

theorem a2_not_vacuous :
    a2B okBody = true ∧ A2OK okBody
      ∧ stack okBody = [release, withdrawal]
      ∧ phase1 .commit (stack okBody) = [withdrawal, release]
      ∧ phase1 .abort (stack okBody) = [withdrawal, release]
      ∧ phase1 .halted (stack okBody) = []
      ∧ acquisitions okBody = 1 ∧ provisions okBody = 1 := by
  refine ⟨rfl, ?_, rfl, rfl, rfl, rfl, rfl, rfl⟩
  exact (a2B_iff okBody).mp rfl

end RevL.A2
