import RevL.Typing
import RevL.Lemmas.ReachLemmas
import RevL.Lemmas.CapLemmas

/-!
# Item 294 / 66 / 260 — capability ceilings and budgets

Roadmap item 294 (parameterized capabilities, slices 1+2 landed) makes a
capability a pair `(T, P)`: a token and a valuation. Item 66 (attenuation
on spawn) says a spawned child's authority must be **covered by** its
spawner's — narrowing is admitted, widening is refused. Item 260 adds
resource *budgets* (`calls`/`size`/`time`) to the same algebra.

`formal/STATUS.md` TODO 2 asks for this over the `Ctx` model. The
capability algebra itself lives in the L1 farm
(`RevL.Lemmas.CapLemmas`, modelling `src/revl/cap_order.py`); this file
is the L2 guarantee: what falls out along a *lineage*.

## The two checks, both modelled

`lower._check_spawn_attenuation` runs **two independent relations**, and
modelling only the first would not reproduce the reference's verdicts:

* the **resource** fold (`cap_order.covers_set` over
  `_strip_ceilings(...)`) — deliberately ceiling-blind, because a
  crossing binds no ceiling;
* the **ceiling** check (`_ceiling_attenuation_check`) — per
  `(token, parameter)`, against the max ceiling the parent declares, with
  a *dropped* child ceiling read as `+∞` and therefore refused.

`ceiling_check_not_subsumed` below exhibits a pair the resource fold
admits and the ceiling check refuses, so the two-relation structure is
not decoration.

## Scope, stated honestly

* `held` is the parent's **own** authority, never its transitive spawn
  closure (`docs/capability-attenuation.md`: a parent cannot launder a
  capability it lacks by routing it through one child into another).
  `Lineage` composes edges, so the *reach* side closes transitively while
  each edge is still checked against one parent's own held set — which is
  what `_spawn_surface_closure` computes.
* Parse-time canonicalization (absolute paths, unit suffixes, the closed
  registry) is upstream of the order and is not modelled; the order
  starts from canonical values, as `covers` does.
* The parent's *pre-spawn spend* is not subtracted from its budget — the
  reference calls that its conservative first cut, and the model matches
  it rather than claiming more.
* `Spends` models the runtime counter (`remainingUses`) that a `calls`
  ceiling erases into at mint; bytes and time are declared-only in the
  reference (no counter is shipped), so nothing here claims they are
  metered.
-/

namespace RevL.CapCeilings

open RevL.Lemmas RevL.Typing RevL.Syntax

/-! ## One spawn edge -/

/-- The resource half of the attenuation check: every capability the
child reaches is covered by one the parent holds, compared on resource
parameters alone (`covers_set(_strip_ceilings(held), _strip_ceilings(reach))`
is empty). -/
def ResourceOK (held reach : List Cap) : Prop :=
  ∀ c ∈ reach, ∃ h ∈ held, Covers (stripCeilings h) (stripCeilings c)

/-- The ceiling half (`_ceiling_attenuation_check`): wherever the parent
declares a budget for the child's token and parameter, the child must
declare one too, and no larger. A dropped child ceiling is `+∞`, hence a
widening, hence refused. -/
def CeilingOK (held reach : List Cap) : Prop :=
  ∀ c ∈ reach, ∀ k n, budgetOf held c.token k = some n →
    ∃ m, ceilingOf c k = some m ∧ m ≤ n

/-- One admitted spawn edge: the child's reach is an attenuation of the
parent's held authority under **both** relations. -/
def Attenuates (held reach : List Cap) : Prop :=
  ResourceOK held reach ∧ CeilingOK held reach

/-- A lineage: the reflexive-transitive closure of admitted spawn edges.
`Lineage H K` says a component holding `K` was reached from a root
holding `H` through spawns the checker admitted. -/
inductive Lineage (H : List Cap) : List Cap → Prop where
  | root : Lineage H H
  | spawn : ∀ {M K}, Lineage H M → Attenuates M K → Lineage H K

/-! ## The guarantees -/

/-- The order `covers` induces on capabilities is a partial order:
reflexive, transitive, and antisymmetric (up to valuation lookup — see
`RevL.Lemmas.covers_antisymm`). This is what makes "at-or-below its
ceiling" a well-defined claim rather than an ad-hoc test. -/
theorem cap_order_partial :
    (∀ c : Cap, Covers c c) ∧
    (∀ a b c : Cap, Covers a b → Covers b c → Covers a c) ∧
    (∀ a b : Cap, Covers a b → Covers b a →
      a.token = b.token ∧ ∀ k, lookupV a.params k = lookupV b.params k) :=
  ⟨covers_refl, fun _ _ _ => covers_trans, fun _ _ => covers_antisymm⟩

/-- **Attenuation is monotone downward (items 66/294).** A capability
held anywhere down a lineage is covered by one the root declared: no
composition of admitted spawns can produce authority exceeding the
declared ceiling. Spawning is not amplification. -/
theorem attenuation_monotone {H K : List Cap} :
    Lineage H K → ResourceOK H K := by
  intro hl
  induction hl with
  | root => intro c hc; exact ⟨c, hc, covers_refl _⟩
  | spawn _ hat ih =>
    intro c hc
    obtain ⟨m, hm, hmc⟩ := hat.1 c hc
    obtain ⟨h, hh, hhm⟩ := ih m hm
    exact ⟨h, hh, covers_trans hhm hmc⟩

/-- **Budgets only shrink (item 260).** If every capability the root
declares bounds parameter `k` at `n` or less, then so does every
capability anywhere down the lineage — including the ones the root never
saw. The `+∞` reading of a dropped ceiling is what makes this true: a
child cannot escape a budget by not mentioning it. -/
theorem lineage_ceiling_le {H K : List Cap} (k : String) (n : Nat) :
    Lineage H K →
    (∀ h ∈ H, ∃ m, ceilingOf h k = some m ∧ m ≤ n) →
    ∀ c ∈ K, ∃ m, ceilingOf c k = some m ∧ m ≤ n := by
  intro hl hH
  induction hl with
  | root => exact hH
  | spawn _ hat ih =>
    intro c hc
    obtain ⟨p, hp, hpc⟩ := hat.1 c hc
    have htok : p.token = c.token := hpc.1
    obtain ⟨v, hv, _⟩ := ih p hp
    obtain ⟨b, hb, _⟩ := budgetOf_ge hp htok hv
    have hbn : b ≤ n := by
      obtain ⟨q, hq, _, hqb⟩ := budgetOf_attained hb
      obtain ⟨w, hw, hwn⟩ := ih q hq
      rw [hqb] at hw
      injection hw with e
      omega
    obtain ⟨m, hm, hmb⟩ := hat.2 c hc k b hb
    exact ⟨m, hm, Nat.le_trans hmb hbn⟩

/-! ## The runtime counter -/

/-- A run against a `remainingUses`-style counter: each step spends `c`
out of what remains, and may not overdraw. -/
inductive Spends : Nat → List Nat → Prop where
  | done : ∀ n, Spends n []
  | step : ∀ n c cs, c ≤ n → Spends (n - c) cs → Spends n (c :: cs)

/-- Total spend of a run. -/
def total : List Nat → Nat
  | [] => 0
  | c :: cs => c + total cs

/-- A run admitted against a counter never spends more than the counter
started with. -/
theorem spend_within_budget {n : Nat} {cs : List Nat} :
    Spends n cs → total cs ≤ n := by
  intro h
  induction h with
  | done n => exact Nat.zero_le n
  | step n c cs hc _ ih => simp only [total]; omega

/-- **The budget guarantee, end to end.** A capability minted anywhere
down a lineage, run against the counter its own ceiling erases into,
spends no more than the ceiling the *root* declared. This is the
composition of `lineage_ceiling_le` (static: budgets shrink down the
lineage) with `spend_within_budget` (dynamic: a counter is not
overdrawn). -/
theorem budget_never_exceeds_root_ceiling {H K : List Cap} (k : String) (n : Nat)
    (hl : Lineage H K)
    (hH : ∀ h ∈ H, ∃ m, ceilingOf h k = some m ∧ m ≤ n)
    {c : Cap} (hc : c ∈ K) {m : Nat} (hm : ceilingOf c k = some m)
    {costs : List Nat} (hs : Spends m costs) : total costs ≤ n := by
  obtain ⟨m', hm', hm'n⟩ := lineage_ceiling_le k n hl hH c hc
  have hspend := spend_within_budget hs
  rw [hm] at hm'
  injection hm' with e
  omega

/-! ## Composition with the reach structure (G1/G6) -/

/-- **Ceilings compose with confinement.** Take a component whose
capability context `Γ` was reached from a root `H` through admitted
spawns, and a statement the checker admits under `Γ`'s wiring keys. Then
every key the statement reaches is one of `Γ`'s capabilities *and* that
capability is covered by one the root declared. G6 bounds the reach by
the declared context; this bounds the declared context by the root's
ceiling, and the two compose. -/
theorem confinement_within_ceiling {H Γ : List Cap} {s : Stmt} :
    Lineage H Γ → TypedIn (capKeys Γ) s →
    ∀ k ∈ stmtHeads s,
      ∃ c ∈ Γ, c.token = k ∧ ∃ h ∈ H, Covers (stripCeilings h) (stripCeilings c) := by
  intro hl ht k hk
  have hmem : k ∈ capKeys Γ := RevL.Lemmas.typedIn_confined ht k hk
  obtain ⟨c, hc, hck⟩ := List.mem_map.mp hmem
  exact ⟨c, hc, hck, attenuation_monotone hl c hc⟩

/-- **The host boundary is never manufactured.** `*` — the unnameable
reach a host emission or first-class dispatch collapses to — is covered
only by `*`, so a lineage rooted in nameable authority never reaches it.
This is the soundness note in `docs/capability-attenuation.md` ("an
amplifier reaching the host cannot hide behind an unnameable boundary")
as a theorem. -/
theorem no_star_amplification {H K : List Cap} :
    Lineage H K → (∀ h ∈ H, h.token ≠ "*") → ∀ c ∈ K, c.token ≠ "*" := by
  intro hl hns c hc hstar
  obtain ⟨h, hh, hcov⟩ := attenuation_monotone hl c hc
  exact hns h hh (by rw [show h.token = c.token from hcov.1, hstar])

/-! ## Non-vacuity

The hypotheses above are genuine, not vacuous: widening really is
excluded by the order, and the ceiling check really does catch something
the resource fold cannot. -/

/-- `fs.write(path=...)` from `examples/rejections/g4_spawn_widens_parameter.rvl`. -/
def fsWrite (p : List String) : Cap := ⟨"fs.write", [("path", .path p)]⟩

/-- The bare token, top of its own cone. -/
def fsWriteBare : Cap := ⟨"fs.write", []⟩

/-- **Parameter widening is refused** — the four clauses the reference's
own tests assert, on the model:

* narrowing inside the cone is admitted (`/tmp/job-42 ≤ /tmp`);
* a sibling outside the cone is refused — this is exactly
  `g4_spawn_widens_parameter.rvl`, where `Router` holds
  `fs.write(path="/tmp")` and `Kid` reaches `fs.write(path="/etc")`;
* the path order is component-wise, never string-prefix
  (`/tmp/jobber` is *not* inside `/tmp/job`);
* a bare token tops its cone, and dropping a parameter widens. -/
theorem parameter_widening_refused :
    Covers (fsWrite ["tmp"]) (fsWrite ["tmp", "job-42"]) ∧
    ¬ Covers (fsWrite ["tmp"]) (fsWrite ["etc"]) ∧
    ¬ Covers (fsWrite ["tmp", "job"]) (fsWrite ["tmp", "jobber"]) ∧
    Covers fsWriteBare (fsWrite ["tmp"]) ∧
    ¬ Covers (fsWrite ["tmp"]) fsWriteBare := by
  refine ⟨⟨rfl, fun k v hv => ?_⟩, ?_, ?_, ⟨rfl, fun k v hv => ?_⟩, ?_⟩
  · obtain ⟨hk, hvv⟩ := lookupV_single hv
    subst hk; subst hvv
    exact ⟨.path ["tmp", "job-42"], rfl, .path _ _ ⟨["job-42"], rfl⟩⟩
  · intro h
    obtain ⟨w, hw, hle⟩ := h.2 "path" (.path ["tmp"]) rfl
    obtain ⟨_, hww⟩ := lookupV_single hw
    subst hww
    cases hle with
    | path _ _ hp =>
      obtain ⟨r, hr⟩ := hp
      injection hr with e
      exact absurd e (by decide)
  · intro h
    obtain ⟨w, hw, hle⟩ := h.2 "path" (.path ["tmp", "job"]) rfl
    obtain ⟨_, hww⟩ := lookupV_single hw
    subst hww
    cases hle with
    | path _ _ hp =>
      obtain ⟨r, hr⟩ := hp
      injection hr with _ hr'
      injection hr' with e
      exact absurd e (by decide)
  · exact absurd hv (by rw [show (fsWriteBare.params) = [] from rfl, lookupV_nil]; simp)
  · intro h
    obtain ⟨w, hw, _⟩ := h.2 "path" (.path ["tmp"]) rfl
    rw [show (fsWriteBare.params) = [] from rfl, lookupV_nil] at hw
    exact absurd hw (by simp)

/-- `model.complete(calls=3)` and the same token with the ceiling dropped. -/
def callsCap (n : Nat) : Cap := ⟨"model.complete", [("calls", .ceiling n)]⟩

/-- The same token with no ceiling at all — `+∞` calls. -/
def callsBare : Cap := ⟨"model.complete", []⟩

/-- **The ceiling check is not subsumed by the resource fold.** A child
that drops its parent's `calls` ceiling passes the (ceiling-blind)
resource fold and is caught only by the dedicated budget check. The
reference makes the same point with
`tests/test_budget_260.py::test_it_is_the_dedicated_check_not_the_ceiling_blind_fold`;
here it is why `Attenuates` is a conjunction of two relations. -/
theorem ceiling_check_not_subsumed :
    ResourceOK [callsCap 3] [callsBare] ∧ ¬ CeilingOK [callsCap 3] [callsBare] := by
  constructor
  · intro c hc
    have hcb : c = callsBare := by simpa using hc
    subst hcb
    refine ⟨callsCap 3, by simp, rfl, fun k v hv => ?_⟩
    rw [show (stripCeilings (callsCap 3)).params = [] from rfl, lookupV_nil] at hv
    exact absurd hv (by simp)
  · intro h
    obtain ⟨m, hm, _⟩ := h callsBare (by simp) "calls" 3 rfl
    rw [show ceilingOf callsBare "calls" = none from rfl] at hm
    exact absurd hm (by simp)

end RevL.CapCeilings
