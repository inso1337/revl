/-!
RevL.Lemmas.ClassLemmas — L1 lemma farm: revl's **effect-classification
lattice** and the emission-reach fixed point (roadmap item 418, step 4).

Core only: this farm imports nothing, so it cannot drift into L0 and no
other farm depends on it.

## Why this farm exists

The item-418 adversarial review found G4, G5 and G8 proved by *shape*:
`RevL.Typing.Typed` has no `raw` constructor, so `G4.inverse_or_emit`
holds of a relation that admits nothing; `G5.registrations` ignores its
argument, so an inverse that calls `db.insert` counts zero; and
`RevL.Boundary.boundaryOf (.effect _ _) = []` **by definition**, so an
`effect` carrying an emission has an empty boundary surface.

None of those statements mention the thing the compiler actually
computes. This farm models that thing: the four-point classification
lattice `pure | acquire | witnessed | emission`, the least fixed point
that propagates it through the call graph, and the two checks that read
it. The restated G4/G5/G8 in `RevL.Theorems.G{4,5,8}_Classified*` are
theorems about *this*, so a "bad case" is representable and the
statements have something to refuse.

## What is modelled, and where it comes from

| model | reference |
|---|---|
| `Cls` (four points) | `parser.ExternDecl.classification`, `parser.py:513` |
| `declOK` | `lower.py:2573` (acquire needs `undo`), `:2608` (pure has neither), `:2614` (emission has no `undo`), `:2735`/`:2744` (witnessed has `undo`, no `compensate`) |
| `reachCls` / `reachCaps` | `emission_analysis._emitting_capabilities`: seeded from `emission` externs (own name) and `witnessed` externs (declared scope, else own name), closed over the fn call graph |
| `inverseOK` | `lower._check_witnessed_inverse` (`:2165-2238`): a witnessed inverse's **transitive** classification must be neither `emission` nor `witnessed` |
| `Cls.crosses` | `_emitting_capabilities`' seed set: `emission` **and** `witnessed` cross the same boundary namespace |
| `Cls.inverseAdmissible` | the same check's admissible set: "only `pure`/`acquire` callees are admissible" |

## Fidelity, stated honestly

* **The fixed point is fuel-bounded.** `emission_analysis` iterates a
  monotone closure to stability over a finite name set;
  `reachCls p fuel n` is that closure unrolled `fuel` times. It is
  therefore an UNDER-approximation for `fuel` smaller than the longest
  fn chain, and `reach_mono_fuel` records exactly that: more fuel can
  only raise the classification, never lower it. Every theorem below is
  quantified over the `fuel` its hypothesis was checked at, so a refusal
  proved at one fuel is not silently claimed at another.
* **Names, not scopes.** A `Name` is an unqualified identifier; module
  qualification, shadowing and imports are the resolver's job and are
  not modelled. A program whose names collide is outside the model.
* **The call graph is given, not derived.** `FnDecl.calls` stands for
  `_calls_in` over a lowered body. That `_calls_in` finds every call is
  an empirical obligation on the lowering, not a theorem here, and it is
  NOT smuggled in as an axiom.
* **First-class dispatch is out of scope.** `_emitting_capabilities`
  adds the unnameable `*` when an emitting callable escapes as a value;
  `RevL.Theorems.CapCeilings` already models `*`, and nothing here
  claims to cover it. A body that passes a callable rather than calling
  it is outside every theorem below.
-/

namespace RevL.Lemmas

/-! ## The lattice -/

/-- An identifier. -/
abbrev Name := String

/-- revl's effect classification (`parser.ExternDecl.classification`).
Four points, totally ordered by how much of the host they can disturb:
`pure` touches nothing; `acquire` takes a resource a bracket gives back;
`witnessed` mutates with a proof-grade declared inverse; `emission`
crosses one-way. -/
inductive Cls where
  | pure
  | acquire
  | witnessed
  | emission
  deriving Repr, DecidableEq

namespace Cls

/-- The lattice position. -/
def rank : Cls → Nat
  | .pure => 0
  | .acquire => 1
  | .witnessed => 2
  | .emission => 3

/-- The order: `a` disturbs no more of the host than `b`. -/
def le (a b : Cls) : Prop := a.rank ≤ b.rank

/-- The join: the classification of a body that does both. -/
def join (a b : Cls) : Cls := if a.rank ≤ b.rank then b else a

/-- **Crosses a host boundary.** `_emitting_capabilities` seeds its fixed
point from `emission` *and* `witnessed` externs: "a witnessed extern
crosses the same boundary as an emission (item 243)". -/
def crosses : Cls → Bool
  | .pure => false
  | .acquire => false
  | .witnessed => true
  | .emission => true

/-- **Admissible in a witnessed inverse.** `_check_witnessed_inverse`:
"The declared inverse is a host-LOCAL restore, so only `pure`/`acquire`
callees are admissible." -/
def inverseAdmissible : Cls → Bool
  | .pure => true
  | .acquire => true
  | .witnessed => false
  | .emission => false

/-- The two predicates are one predicate: the inverse rule is exactly
"does not cross". -/
theorem crosses_eq_not_admissible (c : Cls) : c.crosses = !c.inverseAdmissible := by
  cases c <;> rfl

theorem le_refl (a : Cls) : a.le a := Nat.le_refl _

theorem le_trans {a b c : Cls} (h₁ : a.le b) (h₂ : b.le c) : a.le c :=
  Nat.le_trans h₁ h₂

theorem le_antisymm {a b : Cls} (h₁ : a.le b) (h₂ : b.le a) : a = b := by
  have : a.rank = b.rank := Nat.le_antisymm h₁ h₂
  cases a <;> cases b <;> simp_all [rank]

theorem pure_le (a : Cls) : Cls.pure.le a := by
  cases a <;> exact Nat.zero_le _

theorem le_join_left (a b : Cls) : a.le (a.join b) := by
  unfold join
  by_cases h : a.rank ≤ b.rank
  · rw [if_pos h]; exact h
  · rw [if_neg h]; exact Nat.le_refl _

theorem le_join_right (a b : Cls) : b.le (a.join b) := by
  unfold join
  by_cases h : a.rank ≤ b.rank
  · rw [if_pos h]; exact Nat.le_refl _
  · rw [if_neg h]; exact Nat.le_of_lt (Nat.lt_of_not_le h)

theorem join_le {a b c : Cls} (h₁ : a.le c) (h₂ : b.le c) : (a.join b).le c := by
  unfold join
  by_cases h : a.rank ≤ b.rank
  · rw [if_pos h]; exact h₂
  · rw [if_neg h]; exact h₁

/-- Crossing is upward closed: anything at least as disturbing as a
crossing is a crossing. -/
theorem crosses_mono {a b : Cls} (h : a.le b) (ha : a.crosses = true) :
    b.crosses = true := by
  cases a <;> cases b <;> simp_all [crosses, le, rank]

/-- Inverse-admissibility is downward closed: this is what lets the
`_check_witnessed_inverse` verdict, taken at the fold's join, be read
back onto every name the inverse reaches. -/
theorem admissible_anti {a b : Cls} (h : a.le b) (hb : b.inverseAdmissible = true) :
    a.inverseAdmissible = true := by
  cases a <;> cases b <;> simp_all [inverseAdmissible, le, rank]

theorem not_crosses_of_admissible {c : Cls} (h : c.inverseAdmissible = true) :
    c.crosses = false := by
  cases c <;> simp_all [crosses, inverseAdmissible]

end Cls

/-- The join over a list: the classification of a body that does all of
them. `_emitting_capabilities`' inner `reached |= caps.get(callee)`. -/
def joinList : List Cls → Cls
  | [] => .pure
  | c :: r => Cls.join c (joinList r)

theorem le_joinList {c : Cls} : ∀ {l : List Cls}, c ∈ l → c.le (joinList l) := by
  intro l
  induction l with
  | nil => intro h; cases h
  | cons x r ih =>
    intro h
    rcases List.mem_cons.mp h with he | hr
    · subst he; exact Cls.le_join_left _ _
    · exact Cls.le_trans (ih hr) (Cls.le_join_right _ _)

theorem joinList_le {b : Cls} : ∀ {l : List Cls}, (∀ c ∈ l, c.le b) → (joinList l).le b := by
  intro l
  induction l with
  | nil => intro _; exact Cls.pure_le b
  | cons x r ih =>
    intro h
    exact Cls.join_le (h x List.mem_cons_self)
      (ih (fun c hc => h c (List.mem_cons_of_mem _ hc)))

/-- The join of a list is `pure` or is attained at a member. This is what
makes the fold's verdict *traceable*: a non-pure reach always has a
concrete extern behind it (used by `reach_exact`). -/
theorem joinList_attained : ∀ (l : List Cls),
    joinList l = .pure ∨ ∃ c ∈ l, joinList l = c := by
  intro l
  induction l with
  | nil => exact Or.inl rfl
  | cons x r ih =>
    have hdef : joinList (x :: r) = Cls.join x (joinList r) := rfl
    by_cases h : x.rank ≤ (joinList r).rank
    · have hj : joinList (x :: r) = joinList r := by
        rw [hdef]; unfold Cls.join; rw [if_pos h]
      rcases ih with hp | ⟨c, hc, hcc⟩
      · exact Or.inl (by rw [hj, hp])
      · exact Or.inr ⟨c, List.mem_cons_of_mem _ hc, by rw [hj, hcc]⟩
    · have hj : joinList (x :: r) = x := by
        rw [hdef]; unfold Cls.join; rw [if_neg h]
      exact Or.inr ⟨x, List.mem_cons_self, hj⟩

/-! ## Declarations -/

/-- An `extern` declaration: its classification, its declared inverse
(`undo <name>(result)` — the callee name is what
`_check_witnessed_inverse` walks), its `compensate` slot, and the
capability scope of a `witnessed[fs]`/`emission[db]` crossing. -/
structure ExternDecl where
  name : Name
  cls : Cls
  undo : Option Name
  compensate : Option Name
  caps : List Name
  deriving Repr, DecidableEq

/-- A plain top-level `fn`: a name and the names its lowered body calls
(`emission_analysis._calls_in`). -/
structure FnDecl where
  name : Name
  calls : List Name
  deriving Repr, DecidableEq

/-- A program: the extern table and the fn call graph. -/
structure Prog where
  externs : List ExternDecl
  fns : List FnDecl
  deriving Repr, DecidableEq

def findExtern (n : Name) : List ExternDecl → Option ExternDecl
  | [] => none
  | d :: r => if d.name = n then some d else findExtern n r

def findFn (n : Name) : List FnDecl → Option FnDecl
  | [] => none
  | f :: r => if f.name = n then some f else findFn n r

/-- The extern declaring `n`, if any. -/
def lookupExtern (p : Prog) (n : Name) : Option ExternDecl := findExtern n p.externs

/-- The fn declaring `n`, if any. -/
def lookupFn (p : Prog) (n : Name) : Option FnDecl := findFn n p.fns

theorem findExtern_mem {n : Name} : ∀ {l : List ExternDecl} {d : ExternDecl},
    findExtern n l = some d → d ∈ l := by
  intro l
  induction l with
  | nil => intro d h; exact absurd h (by simp [findExtern])
  | cons x r ih =>
    intro d h
    unfold findExtern at h
    by_cases hx : x.name = n
    · rw [if_pos hx] at h
      have : x = d := by injection h
      exact this ▸ List.mem_cons_self
    · rw [if_neg hx] at h
      exact List.mem_cons_of_mem _ (ih h)

theorem lookupExtern_mem {p : Prog} {n : Name} {d : ExternDecl}
    (h : lookupExtern p n = some d) : d ∈ p.externs := findExtern_mem h

/-- The declared classification of a name. A name with no extern
declaration is a fn (or unknown): it has no classification of its own,
and `_emitting_capabilities` gives it "the union of what it calls, not
its own name". -/
def declCls (p : Prog) (n : Name) : Cls :=
  match lookupExtern p n with
  | some d => d.cls
  | none => .pure

/-- The call-graph successors the fold follows. An extern has none: "a
host capability is named by the `emission` extern itself — that extern
*is* the boundary". -/
def calleesOf (p : Prog) (n : Name) : List Name :=
  match lookupExtern p n with
  | some _ => []
  | none => match lookupFn p n with
      | some f => f.calls
      | none => []

theorem calleesOf_extern {p : Prog} {n : Name} {d : ExternDecl}
    (h : lookupExtern p n = some d) : calleesOf p n = [] := by
  unfold calleesOf; rw [h]

theorem declCls_extern {p : Prog} {n : Name} {d : ExternDecl}
    (h : lookupExtern p n = some d) : declCls p n = d.cls := by
  unfold declCls; rw [h]

theorem declCls_none {p : Prog} {n : Name}
    (h : lookupExtern p n = none) : declCls p n = .pure := by
  unfold declCls; rw [h]

/-! ## The emission-reach fixed point

`_emitting_capabilities` closes a monotone map over a finite name set.
`reachCls p fuel n` is that closure unrolled `fuel` times: an extern
stops the unrolling (it *is* the boundary), a fn joins what its callees
reach. -/

/-- The classification a name's call transitively reaches. -/
def reachCls (p : Prog) : Nat → Name → Cls
  | 0, n => declCls p n
  | fuel + 1, n =>
      match lookupExtern p n with
      | some d => d.cls
      | none => joinList ((calleesOf p n).map (reachCls p fuel))

theorem reachCls_extern {p : Prog} {n : Name} {d : ExternDecl}
    (h : lookupExtern p n = some d) : ∀ fuel, reachCls p fuel n = d.cls := by
  intro fuel
  cases fuel with
  | zero => exact declCls_extern h
  | succ f => show (match lookupExtern p n with
      | some d => d.cls
      | none => joinList _) = d.cls
              rw [h]

/-- The declared classification never exceeds the reached one. -/
theorem declCls_le_reach (p : Prog) (fuel : Nat) (n : Name) :
    (declCls p n).le (reachCls p fuel n) := by
  cases h : lookupExtern p n with
  | some d => rw [declCls_extern h, reachCls_extern h]; exact Cls.le_refl _
  | none => rw [declCls_none h]; exact Cls.pure_le _

/-- A callee's reach is bounded by its caller's, one unrolling up. This
single lemma is why a fn wrapper cannot launder a crossing. -/
theorem reachCls_callee_le {p : Prog} {fuel : Nat} {a m : Name}
    (hm : m ∈ calleesOf p a) : (reachCls p fuel m).le (reachCls p (fuel + 1) a) := by
  cases h : lookupExtern p a with
  | some d => rw [calleesOf_extern h] at hm; cases hm
  | none =>
    have : reachCls p (fuel + 1) a = joinList ((calleesOf p a).map (reachCls p fuel)) := by
      show (match lookupExtern p a with
        | some d => d.cls
        | none => joinList _) = _
      rw [h]
    rw [this]
    exact le_joinList (List.mem_map.mpr ⟨m, hm, rfl⟩)

/-- More unrolling can only raise the classification: the closure is
monotone, so a `fuel` too small under-approximates and never
over-approximates. -/
theorem reach_mono_fuel (p : Prog) : ∀ (fuel : Nat) (n : Name),
    (reachCls p fuel n).le (reachCls p (fuel + 1) n) := by
  intro fuel
  induction fuel with
  | zero => intro n; exact declCls_le_reach p 1 n
  | succ f ih =>
    intro n
    cases h : lookupExtern p n with
    | some d => rw [reachCls_extern h, reachCls_extern h]; exact Cls.le_refl _
    | none =>
      have e : ∀ g, reachCls p (g + 1) n = joinList ((calleesOf p n).map (reachCls p g)) := by
        intro g
        show (match lookupExtern p n with
          | some d => d.cls
          | none => joinList _) = _
        rw [h]
      rw [e f, e (f + 1)]
      refine joinList_le ?_
      intro c hc
      obtain ⟨m, hm, hcm⟩ := List.mem_map.mp hc
      subst hcm
      exact Cls.le_trans (ih m) (le_joinList (List.mem_map.mpr ⟨m, hm, rfl⟩))

/-! ## Transitive reachability

The relation the fold is an abstraction of. `_check_witnessed_inverse`'s
error message names a *chain* (`_emission_chain`); this is that chain. -/

/-- `ReachesFrom p fuel a n`: `n` is reachable from `a` in at most `fuel`
call-graph steps. -/
inductive ReachesFrom (p : Prog) : Nat → Name → Name → Prop where
  | refl : ∀ {fuel n}, ReachesFrom p fuel n n
  | step : ∀ {fuel a m n}, m ∈ calleesOf p a → ReachesFrom p fuel m n →
      ReachesFrom p (fuel + 1) a n

/-- **The fold is sound.** Everything reachable is bounded by the fold's
verdict, so a verdict of "inverse-admissible" is a claim about every
name on every path, not only about the immediate callee. -/
theorem reaches_le {p : Prog} : ∀ {fuel a n}, ReachesFrom p fuel a n →
    (declCls p n).le (reachCls p fuel a) := by
  intro fuel a n h
  induction h with
  | refl => exact declCls_le_reach _ _ _
  | @step f a m n hm _ ih => exact Cls.le_trans ih (reachCls_callee_le hm)

/-- **The fold is exact.** Its verdict is attained by some concrete
reachable name — so the classification is never invented, it is always
some declared extern's. This is the anti-over-approximation direction
that a merely-sound fold would not have. -/
theorem reach_exact (p : Prog) : ∀ (fuel : Nat) (a : Name),
    ∃ n, ReachesFrom p fuel a n ∧ declCls p n = reachCls p fuel a := by
  intro fuel
  induction fuel with
  | zero => intro a; exact ⟨a, .refl, rfl⟩
  | succ f ih =>
    intro a
    cases h : lookupExtern p a with
    | some d =>
      exact ⟨a, .refl, by rw [declCls_extern h, reachCls_extern h]⟩
    | none =>
      have e : reachCls p (f + 1) a = joinList ((calleesOf p a).map (reachCls p f)) := by
        show (match lookupExtern p a with
          | some d => d.cls
          | none => joinList _) = _
        rw [h]
      rcases joinList_attained ((calleesOf p a).map (reachCls p f)) with hp | ⟨c, hc, hcc⟩
      · exact ⟨a, .refl, by rw [declCls_none h, e, hp]⟩
      · obtain ⟨m, hm, hcm⟩ := List.mem_map.mp hc
        obtain ⟨n, hr, hn⟩ := ih m
        exact ⟨n, .step hm hr, by rw [hn, e, hcc, ← hcm]⟩

/-- Reachability only needs more fuel, never less. -/
theorem reaches_weaken {p : Prog} : ∀ {g a n}, ReachesFrom p g a n →
    ∀ h, ReachesFrom p (g + h) a n := by
  intro g a n hr
  induction hr with
  | refl => intro _; exact .refl
  | @step g' x m n hm _ ih =>
    intro h
    have hh : g' + 1 + h = (g' + h) + 1 := by omega
    rw [hh]
    exact .step hm (ih h)

/-- Reachability composes. `_check_witnessed_inverse`'s `_emission_chain`
is a path; this says paths concatenate. -/
theorem reaches_trans {p : Prog} : ∀ {f g a m n}, ReachesFrom p f a m →
    ReachesFrom p g m n → ReachesFrom p (f + g) a n := by
  intro f g a m n h₁
  induction h₁ with
  | @refl f x =>
    intro h₂
    rw [Nat.add_comm f g]
    exact reaches_weaken h₂ f
  | @step f' x m' m hm _ ih =>
    intro h₂
    have hh : f' + 1 + g = (f' + g) + 1 := by omega
    rw [hh]
    exact .step hm (ih h₂)

/-- **The fold is sound along a path.** A name reachable from `a` in `f`
steps has its own `g`-step reach bounded by `a`'s `(f+g)`-step reach.
This is what lets one `_check_witnessed_inverse` verdict, taken at the
declared inverse, constrain every call anywhere in the teardown. -/
theorem reach_le_trans {p : Prog} {f g : Nat} {a m : Name}
    (hr : ReachesFrom p f a m) : (reachCls p g m).le (reachCls p (f + g) a) := by
  obtain ⟨n, hn, hne⟩ := reach_exact p g m
  rw [← hne]
  exact reaches_le (reaches_trans hr hn)

/-! ## The capability surface

`_emitting_capabilities` refines the boolean into a *set*: name -> the
capabilities its call reaches. An `emission` extern contributes its own
name; a `witnessed` extern contributes its declared scope, or its own
name when unscoped. -/

/-- The capabilities one declaration contributes to the surface. -/
def capsOfDecl (d : ExternDecl) : List Name :=
  match d.cls with
  | .emission => [d.name]
  | .witnessed => if d.caps.isEmpty then [d.name] else d.caps
  | _ => []

/-- Only a boundary-crossing classification contributes anything. -/
theorem capsOfDecl_crosses {d : ExternDecl} {k : Name} (h : k ∈ capsOfDecl d) :
    d.cls.crosses = true := by
  unfold capsOfDecl at h
  cases hc : d.cls <;> rw [hc] at h <;> simp_all [Cls.crosses]

/-- The capabilities a name's call transitively reaches. -/
def reachCaps (p : Prog) : Nat → Name → List Name
  | 0, n => match lookupExtern p n with
      | some d => capsOfDecl d
      | none => []
  | fuel + 1, n => match lookupExtern p n with
      | some d => capsOfDecl d
      | none => (calleesOf p n).flatMap (reachCaps p fuel)

theorem reachCaps_extern {p : Prog} {n : Name} {d : ExternDecl}
    (h : lookupExtern p n = some d) : ∀ fuel, reachCaps p fuel n = capsOfDecl d := by
  intro fuel
  cases fuel with
  | zero => show (match lookupExtern p n with
      | some d => capsOfDecl d | none => []) = _
            rw [h]
  | succ f => show (match lookupExtern p n with
      | some d => capsOfDecl d | none => _) = _
              rw [h]

theorem reachCaps_fn {p : Prog} {n : Name} {fuel : Nat}
    (h : lookupExtern p n = none) :
    reachCaps p (fuel + 1) n = (calleesOf p n).flatMap (reachCaps p fuel) := by
  show (match lookupExtern p n with
    | some d => capsOfDecl d | none => _) = _
  rw [h]

/-- **Surface soundness.** Every capability on a name's reach surface
comes from a concrete, reachable, boundary-crossing declaration. -/
theorem reachCaps_sound (p : Prog) : ∀ (fuel : Nat) (a : Name) (k : Name),
    k ∈ reachCaps p fuel a →
    ∃ n d, ReachesFrom p fuel a n ∧ lookupExtern p n = some d ∧
      d.cls.crosses = true ∧ k ∈ capsOfDecl d := by
  intro fuel
  induction fuel with
  | zero =>
    intro a k hk
    cases h : lookupExtern p a with
    | some d =>
      rw [reachCaps_extern h] at hk
      exact ⟨a, d, .refl, h, capsOfDecl_crosses hk, hk⟩
    | none =>
      rw [show reachCaps p 0 a = [] from by
        show (match lookupExtern p a with
          | some d => capsOfDecl d | none => []) = _
        rw [h]] at hk
      cases hk
  | succ f ih =>
    intro a k hk
    cases h : lookupExtern p a with
    | some d =>
      rw [reachCaps_extern h] at hk
      exact ⟨a, d, .refl, h, capsOfDecl_crosses hk, hk⟩
    | none =>
      rw [reachCaps_fn h] at hk
      obtain ⟨m, hm, hkm⟩ := List.mem_flatMap.mp hk
      obtain ⟨n, d, hr, hd, hc, hkd⟩ := ih m k hkm
      exact ⟨n, d, .step hm hr, hd, hc, hkd⟩

/-- **Surface completeness.** Nothing a reachable crossing declares is
dropped from the surface. -/
theorem reachCaps_complete {p : Prog} : ∀ {fuel a n} {d : ExternDecl} {k : Name},
    ReachesFrom p fuel a n → lookupExtern p n = some d → k ∈ capsOfDecl d →
    k ∈ reachCaps p fuel a := by
  intro fuel a n d k hr
  induction hr with
  | @refl f x => intro hd hk; rw [reachCaps_extern hd]; exact hk
  | @step f x m n hm _ ih =>
    intro hd hk
    cases h : lookupExtern p x with
    | some d' => rw [calleesOf_extern h] at hm; cases hm
    | none =>
      rw [reachCaps_fn h]
      exact List.mem_flatMap.mpr ⟨m, hm, ih hd hk⟩

/-- A non-empty surface implies a crossing classification: the two folds
agree. -/
theorem reachCaps_crosses {p : Prog} {fuel : Nat} {a k : Name}
    (h : k ∈ reachCaps p fuel a) : (reachCls p fuel a).crosses = true := by
  obtain ⟨n, d, hr, hd, hc, _⟩ := reachCaps_sound p fuel a k h
  exact Cls.crosses_mono (by rw [← declCls_extern hd] at hc ⊢; exact reaches_le hr) hc

/-! ## The two declaration checks

`declOK` is the per-declaration classification rule set in `lower.py`;
`inverseOK` is `_check_witnessed_inverse`, read off the fold. -/

/-- The per-declaration rules, one per classification:

* `pure` — "`pure` means no observable effect, so there is nothing to
  invert or compensate" (`lower.py:2608`);
* `acquire` — "acquire extern must declare `undo` (G4)" (`:2573`);
* `emission` — "emissions are one-way boundary crossings" so no `undo`
  (`:2614`);
* `witnessed` — must declare `undo` (`:2744`, code G4) and cannot
  declare `compensate` (`:2735`, code G5). -/
def declOK (d : ExternDecl) : Bool :=
  match d.cls with
  | .pure => d.undo.isNone && d.compensate.isNone
  | .acquire => d.undo.isSome
  | .emission => d.undo.isNone
  | .witnessed => d.undo.isSome && d.compensate.isNone

/-- `_check_witnessed_inverse` (`lower.py:2165-2238`), rule 3 of
docs/design/243-witnessed-externs.md: a witnessed extern's declared
inverse must be classified non-emission AND non-witnessed — read off the
TRANSITIVE fold, which is what closes the fn-wrapper escape. -/
def inverseOK (p : Prog) (fuel : Nat) (d : ExternDecl) : Bool :=
  match d.cls with
  | .witnessed => match d.undo with
      | some u => (reachCls p fuel u).inverseAdmissible
      | none => false
  | _ => true

/-- The checker admits the program's declarations. -/
def checkerAdmits (p : Prog) (fuel : Nat) : Bool :=
  p.externs.all (fun d => declOK d && inverseOK p fuel d)

/-- Propositional form. -/
def CheckerAdmits (p : Prog) (fuel : Nat) : Prop := checkerAdmits p fuel = true

instance (p : Prog) (fuel : Nat) : Decidable (CheckerAdmits p fuel) := by
  unfold CheckerAdmits; infer_instance

theorem checkerAdmits_elim {p : Prog} {fuel : Nat} {d : ExternDecl}
    (h : CheckerAdmits p fuel) (hd : d ∈ p.externs) :
    declOK d = true ∧ inverseOK p fuel d = true := by
  have := (List.all_eq_true.mp h) d hd
  exact ⟨(Bool.and_eq_true _ _).mp this |>.1, (Bool.and_eq_true _ _).mp this |>.2⟩

end RevL.Lemmas
