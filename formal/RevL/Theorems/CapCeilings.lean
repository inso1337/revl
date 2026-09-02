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

/-! ## Deriving `held` and `reach` from the program text (STATUS.md TODO 2a)

Everything above takes `held` and `reach` as **given** lists of
capabilities. `src/revl/lower.py` does not: it *derives* them from the
syntax, and until the same derivation exists here the theorems are
conditioned on an oracle rather than on the program text. This section
closes that: `bodyReach`, `heldCaps` and `reachIn` are functions of a
component shape, and the `derived_*` theorems below re-state the
guarantees with their `Lineage` / `TypedIn (capKeys Γ)` hypotheses
discharged from that shape.

### The three reference functions being tracked

| reference | here |
|---|---|
| `_collect_emit_caps_pairs` (emit STEPS only, `req` target ⇒ key cone, else `*`) | `stmtCaps` / `bodyReach` |
| `_held_capabilities_pairs` (own surface ∪ every `requires` key's cone) | `heldCaps` |
| `_spawn_surface_closure` (parent absorbs every descendant's reach) | `reachIn` |
| `_activation_spawn_sites` (activation-body spawns only) | `Comp.spawns` |
| `_check_spawn_attenuation` (both relations, per edge) | `SpawnsAdmitted` |

`_cap_keyed` is the bridge that makes this line up with `capKeys`: the
token of a derived capability is the **wiring key**, and the valuation
rides in from the service's declared emission. So a derived held set's
tokens are the component's declared `requires` keys — proved below as
`derived_held_tokens_are_declared_keys`, which is what turns the
`capKeys` bridge from an assumption into a lemma.

### What the L0 fragment cannot derive, stated rather than assumed

The checker collapses four receiver shapes to the unnameable `*`
(`_collect_emit_caps`'s `else` branch): a host emission, a spawn-handle
receiver (`w.task.run(...)`), an emission extern, and a call to a
transitively-emitting named function. L0's `Stmt`/`Expr` can spell none
of them — there are no externs, no named functions, and a handle binder
is not a wiring key. The model therefore does two things and hides
neither:

* `Comp.handles` carries the spawn-handle binders alongside the body, so
  a handle-reached emission is *expressible* (it type-checks under
  `ctxOf`, exactly as the real checker admits it) and derives `starCap`;
* `emitTarget = none` — an `emit` with no call head — is the fragment's
  stand-in for the extern / emitting-function shapes.

`NameableEmission` is the resulting hypothesis. It is named on every
theorem that needs it, never assumed away, and
`derivation_refuses_unnameable` shows the price of dropping it: the
derivation records `*` and refuses to fold it into a held wiring key.

Two further shapes are carried alongside the body rather than parsed out
of it, for the same reason (L0 has no constructor for either), matching
the reference's own plumbing, which reads them from the spawn registry
and the manifest rather than from statements:

* `Comp.spawns` — the activation-body spawn edges
  (`_check_spawn_attenuation` takes `spawn_reg["edges"]` as an argument);
* `Comp.requires` — the wiring key ↦ service map (the manifest).

One more precision loss is worth naming: the reference resolves an emit
to the *method* being called and takes that method's `emission[...]`
valuations, while an L0 call head is the receiver root alone. `Iface`
therefore maps a service to the valuations its emission methods declare,
and a key's cone is the union over them. That over-approximates reach
per statement, so the derived `SpawnsAdmitted` is at least as strict as
the checker — never looser.
-/

/-! ### The program shape -/

/-- A service's declared emission valuations — the right-hand side of
`_cap_keyed(key, cap_str)`. A service with no parameterized emission
declaration maps to `[]`, which `capsOfDecls` reads as the bare key. -/
abbrev Iface := String → List Valuation

/-- The capabilities one wiring key grants, given the declared emission
valuations of the service it names (`_held_capabilities_pairs`): a
service that declares none contributes the bare key `Cap(key, [])` —
today's element, byte-identical — and a parameterized one contributes
its narrower cone *instead* of the bare key. -/
def capsOfDecls (k : String) : List Valuation → List Cap
  | [] => [⟨k, []⟩]
  | p :: ps => (p :: ps).map (fun q => ⟨k, q⟩)

/-- Resolve a wiring key to the service it is wired to (the manifest's
`requires` map). -/
def lookupSvc : List (String × String) → String → Option String
  | [], _ => none
  | (k', s) :: rest, k => if k' = k then some s else lookupSvc rest k

/-- A component shape: the part of a `.rvl` component the capability
derivation reads. `requires` is the manifest's wiring map, `handles` the
spawn-handle binders, `body` the activation statements, `spawns` the
activation-body spawn edges (`_activation_spawn_sites`: a provide-method
spawn is already bounded by that method's `emission[...]` clause, so it
is not an attenuation edge). -/
structure Comp where
  name : String
  requires : List (String × String)
  handles : List String
  body : List Stmt
  spawns : List String

/-- A composition. -/
abbrev Prog := List Comp

/-- The wiring keys a component declares. -/
def reqKeys (c : Comp) : Ctx := c.requires.map (·.1)

/-- The reach surface the checker confines a component's statements
against: its wiring keys *and* its spawn-handle binders, both of which
are receivers in scope. Only the first are capability-bearing. -/
def ctxOf (c : Comp) : Ctx := reqKeys c ++ c.handles

/-- The service a wiring key names, if it is one. `none` is exactly the
reference's "not a `req` target". -/
def svcOf (c : Comp) (k : String) : Option String := lookupSvc c.requires k

/-! ### The derivation -/

/-- The receiver an emitted expression names, when it names one. -/
def emitTarget : Expr → Option String
  | .call k _ => some k
  | .lit _ => none

/-- Resolve a named receiver: a wiring key yields its cone, anything else
(a spawn handle) the unnameable `*`. -/
def svcCaps (I : Iface) (k : String) : Option String → List Cap
  | none => [starCap]
  | some sv => capsOfDecls k (I sv)

/-- Resolve an emit target: no call head at all is the fragment's
stand-in for an extern / emitting-function receiver, hence `*`. -/
def targetCaps (I : Iface) (c : Comp) : Option String → List Cap
  | none => [starCap]
  | some k => svcCaps I k (svcOf c k)

/-- The capabilities one statement crosses (`_collect_emit_caps_pairs`).
Only an `emit` step contributes: an inverse-paired mutation is not a
boundary crossing, and `raw` is not admitted at all. -/
def stmtCaps (I : Iface) (c : Comp) : Stmt → List Cap
  | .emit m => targetCaps I c (emitTarget m)
  | .pure _ => []
  | .effect _ _ => []
  | .raw _ => []

/-- A component's own emit-step surface — the `A` fact rows of the
differential export. -/
def bodyReach (I : Iface) (c : Comp) : List Cap :=
  c.body.flatMap (stmtCaps I c)

/-- What a component **holds** (`_held_capabilities_pairs`): its own
surface plus, for every wiring key it declares, that key's cone. This is
the set a spawn may pass down. -/
def heldCaps (I : Iface) (c : Comp) : List Cap :=
  bodyReach I c ++ c.requires.flatMap (fun kv => capsOfDecls kv.1 (I kv.2))

/-- Name resolution in a composition. -/
def compOf (P : Prog) (n : String) : Option Comp := P.find? (fun c => c.name == n)

/-- `c` is what `P` resolves its own name to. Name uniqueness is G2's
business (provision disjointness), not re-derived here, so this is an
explicit hypothesis where it is needed. -/
def Resolves (P : Prog) (c : Comp) : Prop := compOf P c.name = some c

/-- A named component's own surface. -/
def ownReach (I : Iface) (P : Prog) (n : String) : List Cap :=
  match compOf P n with
  | none => []
  | some c => bodyReach I c

/-- A named component's activation spawn edges. -/
def childrenOf (P : Prog) (n : String) : List String :=
  match compOf P n with
  | none => []
  | some c => c.spawns

/-- `_spawn_surface_closure` as a fuel-indexed unfolding: what a
component can emit, plus everything its spawned children can, to depth
`f`. The reference runs a `while changed` fixpoint; the fuel is explicit
here so no depth is hand-waved — every theorem below carries the fuel it
needs. -/
def reachIn (I : Iface) (P : Prog) : Nat → String → List Cap
  | 0, n => ownReach I P n
  | f + 1, n => ownReach I P n ++ (childrenOf P n).flatMap (reachIn I P f)

/-- `b` is reached from `a` by `d` activation spawns. -/
inductive Descends (P : Prog) : String → String → Nat → Prop where
  | refl : ∀ n, Descends P n n 0
  | step : ∀ {a b ch d}, Descends P a b d → ch ∈ childrenOf P b →
      Descends P a ch (d + 1)

/-- **The checker's spawn gate, read off the program text**
(`_check_spawn_attenuation`): every activation-body spawn edge in `P`
attenuates — the child's transitively closed derived reach sits inside
the spawner's derived held set, under *both* relations. This is the
hypothesis that replaces the given `Attenuates`/`Lineage` above. -/
def SpawnsAdmitted (I : Iface) (P : Prog) (f : Nat) : Prop :=
  ∀ p ∈ P, ∀ ch ∈ p.spawns, Attenuates (heldCaps I p) (reachIn I P f ch)

/-- The statement's receiver is one the fragment can resolve to a wiring
key. False exactly on the shapes L0 cannot spell (see the section header)
and on a handle-reached emission. -/
def NameableEmission (c : Comp) : Stmt → Prop
  | .emit m => ∃ k sv, emitTarget m = some k ∧ svcOf c k = some sv
  | .pure _ => True
  | .effect _ _ => True
  | .raw _ => True

/-! ### Shape lemmas -/

theorem capsOfDecls_token (k : String) (ps : List Valuation) :
    ∀ x ∈ capsOfDecls k ps, x.token = k := by
  cases ps with
  | nil =>
    intro x hx
    simp only [capsOfDecls, List.mem_singleton] at hx
    subst hx; rfl
  | cons p ps =>
    intro x hx
    simp only [capsOfDecls, List.mem_map] at hx
    obtain ⟨q, _, hq⟩ := hx
    rw [← hq]

theorem capsOfDecls_nonempty (k : String) (ps : List Valuation) :
    ∃ x, x ∈ capsOfDecls k ps := by
  cases ps with
  | nil => exact ⟨⟨k, []⟩, by simp [capsOfDecls]⟩
  | cons p ps => exact ⟨⟨k, p⟩, by simp [capsOfDecls]⟩

theorem lookupSvc_mem : ∀ {rs : List (String × String)} {k sv : String},
    lookupSvc rs k = some sv → (k, sv) ∈ rs := by
  intro rs
  induction rs with
  | nil => intro k sv h; simp [lookupSvc] at h
  | cons x rest ih =>
    obtain ⟨k', s'⟩ := x
    intro k sv h
    simp only [lookupSvc] at h
    by_cases hk : k' = k
    · rw [if_pos hk] at h
      injection h with e
      subst hk; subst e
      exact List.mem_cons_self
    · rw [if_neg hk] at h
      exact List.mem_cons_of_mem _ (ih h)

theorem lookupSvc_isSome : ∀ {rs : List (String × String)} {k : String},
    k ∈ rs.map (·.1) → ∃ sv, lookupSvc rs k = some sv := by
  intro rs
  induction rs with
  | nil => intro k h; simp at h
  | cons x rest ih =>
    obtain ⟨k', s'⟩ := x
    intro k h
    simp only [List.map_cons, List.mem_cons] at h
    by_cases hk : k' = k
    · exact ⟨s', by simp [lookupSvc, hk]⟩
    · rcases h with h | h
      · exact absurd h.symm hk
      · obtain ⟨sv, hsv⟩ := ih h
        exact ⟨sv, by simp only [lookupSvc, if_neg hk]; exact hsv⟩

/-- Context weakening for the reach judgment. -/
theorem reachIn_weaken {C D : Ctx} (h : ∀ k ∈ C, k ∈ D) :
    ∀ {e : Expr}, ReachIn C e → ReachIn D e := by
  intro e he
  induction he with
  | lit s => exact .lit _ s
  | call k args hk _ ih => exact .call _ k args (h k hk) ih

/-- Context weakening for the typing judgment. -/
theorem typedIn_weaken {C D : Ctx} {s : Stmt} (h : ∀ k ∈ C, k ∈ D) :
    TypedIn C s → TypedIn D s := by
  intro ht
  cases ht with
  | pure e he => exact .pure _ e (reachIn_weaken h he)
  | effect m u hm hu => exact .effect _ m u (reachIn_weaken h hm) (reachIn_weaken h hu)
  | emit m hm => exact .emit _ m (reachIn_weaken h hm)

/-- A nameable statement's derived capabilities are all keyed by one of
the component's declared wiring keys. -/
theorem stmtCaps_token (I : Iface) (c : Comp) {s : Stmt}
    (hn : NameableEmission c s) : ∀ x ∈ stmtCaps I c s, x.token ∈ reqKeys c := by
  cases s with
  | pure e => intro x hx; simp [stmtCaps] at hx
  | effect m u => intro x hx; simp [stmtCaps] at hx
  | raw m => intro x hx; simp [stmtCaps] at hx
  | emit m =>
    simp only [NameableEmission] at hn
    obtain ⟨k, sv, hk, hsv⟩ := hn
    intro x hx
    simp only [stmtCaps, hk, targetCaps, hsv, svcCaps] at hx
    rw [capsOfDecls_token k (I sv) x hx]
    exact List.mem_map.mpr ⟨(k, sv), lookupSvc_mem hsv, rfl⟩

/-- Every declared wiring key is a token of the derived held set: a key's
cone is never empty, so no declared authority is lost. -/
theorem reqKeys_sub_capKeys (I : Iface) (c : Comp) :
    ∀ k ∈ reqKeys c, k ∈ capKeys (heldCaps I c) := by
  intro k hk
  obtain ⟨kv, hkv, hkv1⟩ := List.mem_map.mp hk
  obtain ⟨x, hx⟩ := capsOfDecls_nonempty kv.1 (I kv.2)
  refine List.mem_map.mpr ⟨x, ?_, ?_⟩
  · exact List.mem_append_right _ (List.mem_flatMap.mpr ⟨kv, hkv, hx⟩)
  · rw [capsOfDecls_token kv.1 (I kv.2) x hx]; exact hkv1

/-- Dually, every token of the derived held set is a declared wiring key
— provided the body's emissions are ones the fragment can name. -/
theorem heldCaps_token (I : Iface) (c : Comp)
    (hnam : ∀ s ∈ c.body, NameableEmission c s) :
    ∀ h ∈ heldCaps I c, h.token ∈ reqKeys c := by
  intro h hh
  rcases List.mem_append.mp hh with hl | hr
  · obtain ⟨s, hs, hxs⟩ := List.mem_flatMap.mp hl
    exact stmtCaps_token I c (hnam s hs) h hxs
  · obtain ⟨kv, hkv, hx⟩ := List.mem_flatMap.mp hr
    rw [capsOfDecls_token kv.1 (I kv.2) h hx]
    exact List.mem_map.mpr ⟨kv, hkv, rfl⟩

/-! ### The closure -/

theorem ownReach_sub_reachIn (I : Iface) (P : Prog) (f : Nat) (n : String) :
    ∀ x ∈ ownReach I P n, x ∈ reachIn I P f n := by
  cases f with
  | zero => intro x hx; exact hx
  | succ f => intro x hx; exact List.mem_append_left _ hx

theorem reachIn_child {I : Iface} {P : Prog} {f : Nat} {n ch : String} {x : Cap}
    (hch : ch ∈ childrenOf P n) (hx : x ∈ reachIn I P f ch) :
    x ∈ reachIn I P (f + 1) n :=
  List.mem_append_right _ (List.mem_flatMap.mpr ⟨ch, hch, hx⟩)

/-- The closure hoists a descendant's reach into every ancestor's, which
is what lets a single edge check bound a whole subtree — exactly the
reference's design (`_spawn_surface_closure` runs *before* the per-edge
`covers_set`). -/
theorem descends_reach (I : Iface) (P : Prog) {a b : String} {d : Nat}
    (h : Descends P a b d) :
    ∀ (f : Nat) (x : Cap), x ∈ reachIn I P f b → x ∈ reachIn I P (f + d) a := by
  induction h with
  | refl => intro f x hx; simpa using hx
  | @step b ch d _ hch ih =>
    intro f x hx
    have h1 := reachIn_child (I := I) hch hx
    have h2 := ih (f + 1) x h1
    have he : f + 1 + d = f + (d + 1) := by omega
    rwa [he] at h2

/-! ### Agreement with what `CapCeilings` assumed

Three theorems, one per assumption the section above made about `held`
and `reach`. -/

/-- **The `capKeys` bridge, derived.** For a component whose emissions
the fragment can name, the tokens of the derived held set are *exactly*
its declared wiring keys. `confinement_within_ceiling` assumed
`TypedIn (capKeys Γ) s` for a given `Γ`; this makes `capKeys (heldCaps I c)`
and the `Ctx` the checker confines against the same set of keys, so the
assumption is discharged by weakening from `TypedIn (reqKeys c) s`. -/
theorem derived_held_tokens_are_declared_keys (I : Iface) (c : Comp)
    (hnam : ∀ s ∈ c.body, NameableEmission c s) :
    (∀ k ∈ capKeys (heldCaps I c), k ∈ reqKeys c) ∧
    (∀ k ∈ reqKeys c, k ∈ capKeys (heldCaps I c)) := by
  refine ⟨fun k hk => ?_, reqKeys_sub_capKeys I c⟩
  obtain ⟨h, hh, hht⟩ := List.mem_map.mp hk
  rw [← hht]
  exact heldCaps_token I c hnam h hh

/-- **The derived reach is the emit-step surface.** Agreement with
`_collect_emit_caps_pairs`, which traverses for `step == "emit"` nodes
only: a `pure`, `effect` or `raw` statement contributes nothing (an
inverse-paired mutation is not a boundary crossing — only the marker is),
an `emit` through a wiring key contributes exactly that key's cone, and
nothing enters `bodyReach` that no statement put there. -/
theorem derived_reach_is_emit_surface (I : Iface) (c : Comp) :
    (∀ s : Stmt, ¬ IsEmit s → stmtCaps I c s = []) ∧
    (∀ (m : Expr) (k sv : String), emitTarget m = some k → svcOf c k = some sv →
      stmtCaps I c (.emit m) = capsOfDecls k (I sv)) ∧
    (∀ x ∈ bodyReach I c, ∃ s ∈ c.body, x ∈ stmtCaps I c s) := by
  refine ⟨fun s hs => ?_, fun m k sv hk hsv => ?_, fun x hx => List.mem_flatMap.mp hx⟩
  · cases s with
    | pure e => rfl
    | effect m u => rfl
    | raw m => rfl
    | emit m => exact absurd (IsEmit.emit m) hs
  · simp only [stmtCaps, hk, targetCaps, hsv, svcCaps]

/-- **What the fragment cannot name, named.** Under the checker's own
confinement judgment a call-headed emission is always nameable — its head
is a declared wiring key unless it is a spawn-handle binder — so the
derivation resolves it to a real `(T, P)` capability. The two residues
are stated as equations, not swept away: a handle receiver and a
head-less emission both derive exactly `[starCap]`, which is the
reference's `caps.add("*")` and is covered only by `*`. -/
theorem unnameable_receiver_is_star (I : Iface) (c : Comp) (m : Expr) :
    (∀ k, emitTarget m = some k → k ∈ reqKeys c →
      NameableEmission c (.emit m)) ∧
    (emitTarget m = none → stmtCaps I c (.emit m) = [starCap]) ∧
    (∀ k, emitTarget m = some k → svcOf c k = none →
      stmtCaps I c (.emit m) = [starCap]) := by
  refine ⟨fun k hk hmem => ?_, fun h => by simp only [stmtCaps, h, targetCaps],
    fun k hk hsv => by simp only [stmtCaps, hk, targetCaps, hsv, svcCaps]⟩
  obtain ⟨sv, hsv⟩ := lookupSvc_isSome hmem
  exact ⟨k, sv, hk, hsv⟩

/-! ### The re-stated guarantees

Each of these is one of the nine theorems above with its `Lineage` (and,
for confinement, its `TypedIn (capKeys Γ)`) hypothesis replaced by
`SpawnsAdmitted` — the checker's own gate, computed from the component
shapes. -/

/-- One admitted edge gives a lineage, from the text. -/
theorem derived_lineage {I : Iface} {P : Prog} {f : Nat}
    (hA : SpawnsAdmitted I P f) {p : Comp} (hp : p ∈ P) {ch : String}
    (hch : ch ∈ p.spawns) : Lineage (heldCaps I p) (reachIn I P f ch) :=
  .spawn .root (hA p hp ch hch)

/-- **`attenuation_monotone`, discharged.** Every capability a
*descendant* of an admitted spawn reaches is covered by one the spawner
declared — with `held` and `reach` both read off the program text. The
transitive work is done by the closure, matching the reference: one edge
check against `_spawn_surface_closure`'s child set bounds the whole
subtree. -/
theorem derived_attenuation_monotone {I : Iface} {P : Prog} {f d : Nat}
    (hA : SpawnsAdmitted I P (f + d)) {p : Comp} (hp : p ∈ P) {b ch : String}
    (hstart : b ∈ p.spawns) (hdesc : Descends P b ch d) :
    ResourceOK (heldCaps I p) (reachIn I P f ch) := fun x hx =>
  attenuation_monotone (derived_lineage hA hp hstart) x
    (descends_reach I P hdesc f x hx)

/-- **`lineage_ceiling_le`, discharged.** If every capability the
spawner's derived held set carries bounds `k` at `n` or less, so does
every capability any descendant derivably reaches. -/
theorem derived_lineage_ceiling_le {I : Iface} {P : Prog} {f d : Nat}
    (k : String) (n : Nat) (hA : SpawnsAdmitted I P (f + d)) {p : Comp}
    (hp : p ∈ P) {b ch : String} (hstart : b ∈ p.spawns)
    (hdesc : Descends P b ch d)
    (hH : ∀ h ∈ heldCaps I p, ∃ m, ceilingOf h k = some m ∧ m ≤ n) :
    ∀ x ∈ reachIn I P f ch, ∃ m, ceilingOf x k = some m ∧ m ≤ n := fun x hx =>
  lineage_ceiling_le k n (derived_lineage hA hp hstart) hH x
    (descends_reach I P hdesc f x hx)

/-- **`budget_never_exceeds_root_ceiling`, discharged.** The end-to-end
budget claim, now rooted in a component shape: a capability derived
anywhere under an admitted spawn, run against the counter its own ceiling
erases into, spends no more than the ceiling the *spawner's* declared
authority carries. -/
theorem derived_budget_never_exceeds_root_ceiling {I : Iface} {P : Prog}
    {f d : Nat} (k : String) (n : Nat) (hA : SpawnsAdmitted I P (f + d))
    {p : Comp} (hp : p ∈ P) {b ch : String} (hstart : b ∈ p.spawns)
    (hdesc : Descends P b ch d)
    (hH : ∀ h ∈ heldCaps I p, ∃ m, ceilingOf h k = some m ∧ m ≤ n)
    {x : Cap} (hx : x ∈ reachIn I P f ch) {m : Nat} (hm : ceilingOf x k = some m)
    {costs : List Nat} (hs : Spends m costs) : total costs ≤ n :=
  budget_never_exceeds_root_ceiling k n (derived_lineage hA hp hstart) hH
    (descends_reach I P hdesc f x hx) hm hs

/-- **`confinement_within_ceiling`, discharged.** Take a spawned
component whose statements the checker admits under its *declared*
requirement keys. Then (a) every key a statement reaches is a token of
the component's own derived held set — the `capKeys` bridge is a lemma
now, not a hypothesis — and (b) every capability the statement derivably
crosses is covered by one the spawner holds. G6 bounds the reach by the
declared context; `SpawnsAdmitted` bounds the declared context by the
spawner's authority.

`NameableEmission` is required on (a) and is the named residue: a
handle-reached emission has a receiver in `ctxOf` that is not a wiring
key, so its head is not a held token. (b) needs no such hypothesis — an
unnameable crossing derives `*` and is refused by the gate rather than
absorbed. -/
theorem derived_confinement_within_ceiling {I : Iface} {P : Prog} {f : Nat}
    (hA : SpawnsAdmitted I P f) {p : Comp} (hp : p ∈ P) {c : Comp}
    (hres : Resolves P c) (hedge : c.name ∈ p.spawns) {s : Stmt}
    (hs : s ∈ c.body) (ht : TypedIn (reqKeys c) s) :
    ((∀ t ∈ c.body, NameableEmission c t) →
      ∀ k ∈ stmtHeads s, ∃ cap ∈ heldCaps I c, cap.token = k) ∧
    (∀ x ∈ stmtCaps I c s,
      ∃ h ∈ heldCaps I p, Covers (stripCeilings h) (stripCeilings x)) := by
  constructor
  · intro _ k hk
    have htw : TypedIn (capKeys (heldCaps I c)) s :=
      typedIn_weaken (reqKeys_sub_capKeys I c) ht
    obtain ⟨cap, hcap, htok, _⟩ :=
      confinement_within_ceiling (H := heldCaps I c) .root htw k hk
    exact ⟨cap, hcap, htok⟩
  · intro x hx
    have h1 : x ∈ bodyReach I c := List.mem_flatMap.mpr ⟨s, hs, hx⟩
    have hr : compOf P c.name = some c := hres
    have h2 : x ∈ ownReach I P c.name := by
      simp only [ownReach, hr]; exact h1
    exact (hA p hp c.name hedge).1 x (ownReach_sub_reachIn I P f c.name x h2)

/-- **`no_star_amplification`, discharged.** The side condition "the root
declares no `*`" is itself derived: a held set is `*`-free exactly when
no wiring key is spelled `*` and every emission in the body is one the
fragment can name. Drop `NameableEmission` and the conclusion is false,
which is `derivation_refuses_unnameable` below — so this is the honest
boundary of the claim, not a convenient hypothesis. -/
theorem derived_no_star_amplification {I : Iface} {P : Prog} {f d : Nat}
    (hA : SpawnsAdmitted I P (f + d)) {p : Comp} (hp : p ∈ P) {b ch : String}
    (hstart : b ∈ p.spawns) (hdesc : Descends P b ch d)
    (hnam : ∀ s ∈ p.body, NameableEmission p s)
    (hkeys : ∀ k ∈ reqKeys p, k ≠ "*") :
    ∀ x ∈ reachIn I P f ch, x.token ≠ "*" := by
  have hns : ∀ h ∈ heldCaps I p, h.token ≠ "*" := fun h hh =>
    hkeys h.token (heldCaps_token I p hnam h hh)
  exact fun x hx =>
    no_star_amplification (derived_lineage hA hp hstart) hns x
      (descends_reach I P hdesc f x hx)

/-! ### Non-vacuity of the derivation

`CrossTier.annotation_necessary` is the model here: a hypothesis is only
worth stating if dropping it breaks something. Three witnesses, all
computed from component shapes rather than from given capability lists.

The composition tracked is `examples/rejections/g4_spawn_widens_parameter.rvl`
— a router wired to a `/tmp`-scoped filesystem service spawning a child
wired to an `/etc`-scoped one. -/

/-- The witness service table: three filesystem services at three scopes,
one model service with a `calls` budget, one without. -/
def witIface (sv : String) : List Valuation :=
  if sv = "FsTmp" then [[("path", .path ["tmp"])]]
  else if sv = "FsEtc" then [[("path", .path ["etc"])]]
  else if sv = "FsJob" then [[("path", .path ["tmp", "job-42"])]]
  else if sv = "ModelCalls3" then [[("calls", .ceiling 3)]]
  else []

/-- The capability the witness derives at the wiring key `fs`. -/
def wFs (p : List String) : Cap := ⟨"fs", [("path", .path p)]⟩

/-- A supervisor holding `fs` at `/tmp`, spawning `child`, emitting
nothing of its own. -/
def wRouter (child : String) : Comp :=
  { name := "Router", requires := [("fs", "FsTmp")], handles := [],
    body := [], spawns := [child] }

/-- A child that crosses its `fs` boundary once, wired to service `sv`. -/
def wChild (nm sv : String) : Comp :=
  { name := nm, requires := [("fs", sv)], handles := [],
    body := [.emit (.call "fs" [])], spawns := [] }

/-- The widening composition: `Kid` reaches `/etc` beneath a `/tmp`
router. -/
def wProgBad : Prog := [wRouter "Kid", wChild "Kid" "FsEtc"]

/-- The narrowing composition: `Job` reaches `/tmp/job-42`, inside the
router's cone. -/
def wProgGood : Prog := [wRouter "Job", wChild "Job" "FsJob"]

theorem stripCeilings_wFs (p : List String) : stripCeilings (wFs p) = wFs p := rfl

theorem ceilingOf_wFs (p : List String) (k : String) : ceilingOf (wFs p) k = none := by
  show ceilingVal (lookupV [("path", PVal.path p)] k) = none
  simp only [lookupV]
  by_cases h : "path" = k
  · rw [if_pos h]; rfl
  · rw [if_neg h]; rfl

/-- A held set that declares no ceiling for `k` has no budget for `k`. -/
theorem budgetOf_none {held : List Cap} {t k : String}
    (h : ∀ c ∈ held, ceilingOf c k = none) : budgetOf held t k = none := by
  induction held with
  | nil => rfl
  | cons x rest ih =>
    have hrest : ∀ c ∈ rest, ceilingOf c k = none :=
      fun c hc => h c (List.mem_cons_of_mem _ hc)
    unfold budgetOf
    by_cases hx : x.token = t
    · rw [if_pos hx, h x List.mem_cons_self]
      exact ih hrest
    · rw [if_neg hx]
      exact ih hrest

/-- **The derivation is non-vacuous.** All four facts are computed from
the component shapes above, with no capability set supplied by hand:

* the router's derived held set is the `/tmp` cone, and the widening
  child's derived reach is the `/etc` one — neither is empty, and neither
  is the bare wiring key, so the valuation really did survive the
  derivation;
* the narrowing composition passes the derived gate;
* the widening one does not — which is
  `examples/rejections/g4_spawn_widens_parameter.rvl`, refused from the
  program text rather than from an assumed `Attenuates`. -/
theorem derivation_non_vacuous :
    wFs ["tmp"] ∈ heldCaps witIface (wRouter "Kid") ∧
    wFs ["etc"] ∈ reachIn witIface wProgBad 1 "Kid" ∧
    SpawnsAdmitted witIface wProgGood 1 ∧
    ¬ SpawnsAdmitted witIface wProgBad 1 := by
  refine ⟨by simp [heldCaps, bodyReach, wRouter, capsOfDecls, witIface, wFs], ?_, ?_, ?_⟩
  · apply ownReach_sub_reachIn
    show wFs ["etc"] ∈ ownReach witIface wProgBad "Kid"
    simp [ownReach, compOf, wProgBad, wRouter, wChild, bodyReach, stmtCaps,
      emitTarget, targetCaps, svcOf, lookupSvc, svcCaps, capsOfDecls, witIface, wFs]
  · intro p hp ch hch
    have hpk : p = wRouter "Job" := by
      simp only [wProgGood, List.mem_cons, List.not_mem_nil, or_false] at hp
      rcases hp with h | h
      · exact h
      · rw [h] at hch; simp [wChild] at hch
    subst hpk
    have hchj : ch = "Job" := by simpa [wRouter] using hch
    subst hchj
    have hheld : heldCaps witIface (wRouter "Job") = [wFs ["tmp"]] := by
      simp [heldCaps, bodyReach, wRouter, capsOfDecls, witIface, wFs]
    have hreach : reachIn witIface wProgGood 1 "Job" = [wFs ["tmp", "job-42"]] := by
      simp [reachIn, ownReach, childrenOf, compOf, wProgGood, wRouter, wChild,
        bodyReach, stmtCaps, emitTarget, targetCaps, svcOf, lookupSvc, svcCaps,
        capsOfDecls, witIface, wFs]
    constructor
    · rw [hheld, hreach]
      intro x hx
      have hxv : x = wFs ["tmp", "job-42"] := by simpa using hx
      subst hxv
      refine ⟨wFs ["tmp"], List.mem_cons_self, ?_⟩
      rw [stripCeilings_wFs, stripCeilings_wFs]
      refine ⟨rfl, fun k v hv => ?_⟩
      obtain ⟨hk, hvv⟩ := lookupV_single hv
      subst hk; subst hvv
      exact ⟨.path ["tmp", "job-42"], rfl, .path _ _ ⟨["job-42"], rfl⟩⟩
    · intro x _ k n hb
      rw [hheld] at hb
      rw [budgetOf_none (fun c hc => by
        have : c = wFs ["tmp"] := by simpa using hc
        subst this; exact ceilingOf_wFs _ _)] at hb
      exact absurd hb (by simp)
  · intro hA
    have hp : wRouter "Kid" ∈ wProgBad := List.mem_cons_self
    have hch : "Kid" ∈ (wRouter "Kid").spawns := by simp [wRouter]
    have hreach : wFs ["etc"] ∈ reachIn witIface wProgBad 1 "Kid" := by
      apply ownReach_sub_reachIn
      show wFs ["etc"] ∈ ownReach witIface wProgBad "Kid"
      simp [ownReach, compOf, wProgBad, wRouter, wChild, bodyReach, stmtCaps,
        emitTarget, targetCaps, svcOf, lookupSvc, svcCaps, capsOfDecls, witIface, wFs]
    obtain ⟨h, hh, hcov⟩ := (hA _ hp _ hch).1 _ hreach
    have hhv : h = wFs ["tmp"] := by
      have : heldCaps witIface (wRouter "Kid") = [wFs ["tmp"]] := by
        simp [heldCaps, bodyReach, wRouter, capsOfDecls, witIface, wFs]
      rw [this] at hh; simpa using hh
    subst hhv
    rw [stripCeilings_wFs, stripCeilings_wFs] at hcov
    obtain ⟨w, hw, hle⟩ := hcov.2 "path" (.path ["tmp"]) rfl
    obtain ⟨_, hww⟩ := lookupV_single hw
    subst hww
    cases hle with
    | path _ _ hp' =>
      obtain ⟨r, hr⟩ := hp'
      injection hr with e
      exact absurd e (by decide)

/-- A child that crosses a boundary through a **spawn handle**: `w` is a
spawn binder, in scope and admitted by the checker, but not a wiring key,
so the fragment cannot resolve it. -/
def wHandleKid : Comp :=
  { name := "HandleKid", requires := [("fs", "FsTmp")], handles := ["w"],
    body := [.emit (.call "w" [])], spawns := [] }

/-- **The derivation refuses to over-approximate.** The handle-reached
emission is a statement the checker *admits* (its receiver is in scope,
`TypedIn (ctxOf wHandleKid)` holds), and the component even holds a `fs`
capability. The derivation neither drops the crossing nor folds it into
the held key: it records the unnameable `*`, which no nameable held set
covers. This is the price of `NameableEmission`, made concrete — and it
is why `derived_no_star_amplification` carries that hypothesis instead of
pretending the fragment can see through a handle. -/
theorem derivation_refuses_unnameable :
    TypedIn (ctxOf wHandleKid) (.emit (.call "w" [])) ∧
    starCap ∈ bodyReach witIface wHandleKid ∧
    ¬ ResourceOK (heldCaps witIface (wRouter "Kid")) (bodyReach witIface wHandleKid) := by
  refine ⟨.emit _ _ (.call _ "w" [] (by simp [ctxOf, reqKeys, wHandleKid])
      (by intro a ha; simp at ha)), ?_, ?_⟩
  · simp [bodyReach, wHandleKid, stmtCaps, emitTarget, targetCaps, svcOf, lookupSvc,
      svcCaps]
  · intro hR
    have hstar : starCap ∈ bodyReach witIface wHandleKid := by
      simp [bodyReach, wHandleKid, stmtCaps, emitTarget, targetCaps, svcOf, lookupSvc,
        svcCaps]
    obtain ⟨h, hh, hcov⟩ := hR _ hstar
    have hhv : h = wFs ["tmp"] := by
      have : heldCaps witIface (wRouter "Kid") = [wFs ["tmp"]] := by
        simp [heldCaps, bodyReach, wRouter, capsOfDecls, witIface, wFs]
      rw [this] at hh; simpa using hh
    subst hhv
    rw [stripCeilings_wFs] at hcov
    have := covers_star (show Covers (wFs ["tmp"]) starCap from by
      simpa [stripCeilings, starCap] using hcov)
    exact absurd this (by decide)

/-- The budget composition: a supervisor holding `model(calls=3)` that
spawns a worker whose service declares no ceiling at all. -/
def wBudgetRouter : Comp :=
  { name := "Budget", requires := [("model", "ModelCalls3")], handles := [],
    body := [], spawns := ["Worker"] }

def wBudgetWorker : Comp :=
  { name := "Worker", requires := [("model", "ModelBare")], handles := [],
    body := [.emit (.call "model" [])], spawns := [] }

def wProgBudget : Prog := [wBudgetRouter, wBudgetWorker]

/-- **`ceiling_check_not_subsumed`, on derived sets.** The same point as
above, now with both sides read off component shapes: a child whose
service drops the parent's `calls` ceiling passes the (ceiling-blind)
resource fold and is caught only by the dedicated budget check. So the
derivation feeds *both* relations of `Attenuates`, and the conjunction is
still load-bearing after the sets stop being given. -/
theorem derived_ceiling_check_not_subsumed :
    ResourceOK (heldCaps witIface wBudgetRouter) (reachIn witIface wProgBudget 1 "Worker") ∧
    ¬ CeilingOK (heldCaps witIface wBudgetRouter) (reachIn witIface wProgBudget 1 "Worker") := by
  have hheld : heldCaps witIface wBudgetRouter
      = [⟨"model", [("calls", .ceiling 3)]⟩] := by
    simp [heldCaps, bodyReach, wBudgetRouter, capsOfDecls, witIface]
  have hreach : reachIn witIface wProgBudget 1 "Worker"
      = [⟨"model", []⟩] := by
    simp [reachIn, ownReach, childrenOf, compOf, wProgBudget, wBudgetRouter,
      wBudgetWorker, bodyReach, stmtCaps, emitTarget, targetCaps, svcOf,
      lookupSvc, svcCaps, capsOfDecls, witIface]
  constructor
  · rw [hheld, hreach]
    intro x hx
    have hxv : x = (⟨"model", []⟩ : Cap) := by simpa using hx
    subst hxv
    refine ⟨⟨"model", [("calls", .ceiling 3)]⟩, List.mem_cons_self, rfl, fun k v hv => ?_⟩
    rw [show (stripCeilings ⟨"model", [("calls", PVal.ceiling 3)]⟩).params = [] from rfl,
      lookupV_nil] at hv
    exact absurd hv (by simp)
  · intro hC
    rw [hheld, hreach] at hC
    obtain ⟨m, hm, _⟩ := hC ⟨"model", []⟩ List.mem_cons_self "calls" 3 rfl
    exact absurd hm (by simp [ceilingOf, lookupV, ceilingVal])

end RevL.CapCeilings
