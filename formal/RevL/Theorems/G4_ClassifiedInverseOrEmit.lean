import RevL.Lemmas.ClassLemmas

/-!
# G4, restated over the classification lattice (roadmap item 418, step 4)

## What was wrong with the old statement

`RevL.Theorems.G4_InverseOrEmit.inverse_or_emit` reads

    ∀ s, Typed s → IsMutation s → HasInverse s ∨ IsEmit s

and its proof ends `| raw _ => cases ht  -- no Typed constructor admits a
raw mutation`. That is a tautology of the *encoding*: the IDENTICAL
sentence holds of a `Typed` that admits nothing at all, because the bad
case is not expressible. It says nothing about `lower.py`'s
classification lattice, which is where the real rule lives.

## What is stated here instead

G4 as a property of the CLASSIFICATION. A declaration carries a
classification drawn from `pure | acquire | witnessed | emission`; a
mutation is any classification other than `pure`; and the theorem is
that the per-declaration rules in `lower.py` force every mutation to
carry either a declared inverse or the `emission` marker:

* `acquire` — "acquire extern must declare `undo` (G4)" (`lower.py:2573`)
* `witnessed` — "witnessed extern must declare `undo` (G4)" (`:2744`)
* `emission` — IS the marker; it is forbidden an `undo` (`:2614`)
  because "emissions are one-way boundary crossings"

The `raw` shape — a mutation with neither — is now perfectly
representable (`rawWrite` below is a term of `ExternDecl`). It is
refused by `declOK`, a computation, not by the absence of a constructor.
`raw_mutation_is_representable` is the guard that pins this: it exhibits
the term the old model could not even write down.

The transitive half (`reached_crossing_carries_inverse_or_marker`) is
the one the old statement could not reach at all: a name that merely
*reaches* a boundary crossing through the fn call graph has a concrete
declaration behind it, and that declaration satisfies G4.
-/

namespace RevL.G4Classified

open RevL.Lemmas

/-! ## The three predicates, over classifications -/

/-- The declaration disturbs the host. Every classification but `pure`:
`acquire` takes a resource, `witnessed` mutates reversibly, `emission`
crosses one-way. -/
def Mutates (c : Cls) : Prop := c ≠ .pure

/-- The declaration names the inverse the teardown accumulator
registers. -/
def HasDeclaredInverse (d : ExternDecl) : Prop := d.undo.isSome = true

/-- The declaration IS the explicit, auditable boundary marker. -/
def IsEmissionMarker (d : ExternDecl) : Prop := d.cls = .emission

instance (c : Cls) : Decidable (Mutates c) := by unfold Mutates; infer_instance
instance (d : ExternDecl) : Decidable (HasDeclaredInverse d) := by
  unfold HasDeclaredInverse; infer_instance
instance (d : ExternDecl) : Decidable (IsEmissionMarker d) := by
  unfold IsEmissionMarker; infer_instance

/-! ## The restated theorem -/

/-- **G4 over the lattice.** Every declaration the checker admits that
mutates the host either declares an inverse or is the emission marker.

The content is the classification rule set, not a missing constructor:
the proof consumes `declOK` in the `acquire` and `witnessed` cases, and
those two conjuncts are exactly `lower.py:2573` and `lower.py:2744`. -/
theorem inverse_or_emit_classified (p : Prog) (d : ExternDecl)
    (_hd : d ∈ p.externs) (hok : declOK d = true) (hm : Mutates d.cls) :
    HasDeclaredInverse d ∨ IsEmissionMarker d := by
  unfold declOK at hok
  unfold Mutates at hm
  cases hc : d.cls with
  | pure => exact absurd hc hm
  | acquire => rw [hc] at hok; exact Or.inl hok
  | witnessed => rw [hc] at hok; exact Or.inl ((Bool.and_eq_true _ _).mp hok).1
  | emission => exact Or.inr hc

/-- The program-level form: every mutating declaration of an admitted
program satisfies G4. -/
theorem program_mutations_carry_inverse_or_marker (p : Prog) (fuel : Nat)
    (hadm : CheckerAdmits p fuel) :
    ∀ d ∈ p.externs, Mutates d.cls → HasDeclaredInverse d ∨ IsEmissionMarker d := by
  intro d hd hm
  exact inverse_or_emit_classified p d hd (checkerAdmits_elim hadm hd).1 hm

/-- A boundary crossing reached transitively is still a crossing: the
fn wrapper does not launder it. This is `_check_witnessed_inverse`'s
`emitting_fns` argument ("a callee that is a plain top-level `fn` is not
in `extern_class`, so it passed, yet a `fn` body may itself reach an
emission"), as a lemma. -/
theorem reached_crossing_is_classified {p : Prog} {fuel : Nat} {a n : Name}
    {d : ExternDecl} (hr : ReachesFrom p fuel a n)
    (hd : lookupExtern p n = some d) (hc : d.cls.crosses = true) :
    (reachCls p fuel a).crosses = true := by
  refine Cls.crosses_mono (reaches_le hr) ?_
  rw [declCls_extern hd]; exact hc

/-- **G4, transitively.** If a name's call reaches a boundary crossing
at all, there is a concrete declaration behind that verdict, it really
does cross, and it carries an inverse or the emission marker.

The old statement had no way to say this: it quantified over syntactic
statements, and the reach fold has no counterpart there. -/
theorem reached_crossing_carries_inverse_or_marker {p : Prog} {fuel : Nat} {a : Name}
    (hadm : CheckerAdmits p fuel) (hcr : (reachCls p fuel a).crosses = true) :
    ∃ n d, ReachesFrom p fuel a n ∧ lookupExtern p n = some d ∧
      d.cls.crosses = true ∧ (HasDeclaredInverse d ∨ IsEmissionMarker d) := by
  obtain ⟨n, hr, hn⟩ := reach_exact p fuel a
  cases h : lookupExtern p n with
  | none =>
    rw [declCls_none h] at hn
    rw [← hn] at hcr
    exact absurd hcr (by simp [Cls.crosses])
  | some d =>
    have hdc : d.cls.crosses = true := by
      rw [declCls_extern h] at hn; rw [hn]; exact hcr
    have hmem : d ∈ p.externs := lookupExtern_mem h
    have hmut : Mutates d.cls := by
      unfold Mutates; intro he; rw [he] at hdc; exact absurd hdc (by simp [Cls.crosses])
    exact ⟨n, d, hr, h, hdc,
      inverse_or_emit_classified p d hmem (checkerAdmits_elim hadm hmem).1 hmut⟩

/-! ## Non-vacuity

`CrossTier.annotation_necessary` is the model: a statement is only worth
having if there is something it admits and something it refuses, and if
the refusal is not universal. All four declarations below are terms of
the same type, so nothing is ruled out by the encoding. -/

/-- A host-local restore: touches nothing. -/
def memRestore : ExternDecl := ⟨"mem_restore", .pure, none, none, []⟩

/-- A mutation that carries its inverse — G4's first disjunct. -/
def memSet : ExternDecl := ⟨"mem_set", .acquire, some "mem_restore", none, []⟩

/-- A reversible mutation with a proof-grade inverse. -/
def rowInsert : ExternDecl := ⟨"row_insert", .witnessed, some "row_delete", none, ["db"]⟩

/-- A one-way crossing — G4's second disjunct, the auditable escape
hatch. -/
def dbInsert : ExternDecl := ⟨"db_insert", .emission, none, none, []⟩

/-- **The `raw` shape.** A mutation with NEITHER an inverse NOR the
marker. In the old model this could not be written: `Typed` has no
constructor for it. Here it is an ordinary term. -/
def rawWrite : ExternDecl := ⟨"raw_write", .acquire, none, none, []⟩

/-- A declaration that would smuggle a one-way crossing in under a
reversible marker: an `emission` claiming an `undo`. `lower.py:2614`
refuses it. -/
def fakeReversibleEmission : ExternDecl := ⟨"send_mail", .emission, some "unsend", none, []⟩

def goodProg : Prog := ⟨[memRestore, memSet, rowInsert, dbInsert], []⟩
def rawProg : Prog := ⟨[memRestore, rawWrite], []⟩
def fakeProg : Prog := ⟨[fakeReversibleEmission], []⟩

/-- **The bad case is expressible.** This is the guard the old G4 fails:
its statement is true of a relation admitting nothing, because `raw` has
no constructor. Here the shape G4 forbids is a term, and the theorem has
to work for its refusal. -/
theorem raw_mutation_is_representable :
    ∃ d : ExternDecl, Mutates d.cls ∧ ¬ HasDeclaredInverse d ∧ ¬ IsEmissionMarker d :=
  ⟨rawWrite, by decide, by decide, by decide⟩

/-- **G4 is non-vacuous.** Two shapes the classification ADMITS (one per
disjunct, plus the witnessed middle), and two it REFUSES — the `raw`
mutation and the emission pretending to be reversible. The refusal is
computed by `declOK`, so it is not universal: `goodProg` passes the very
same check that rejects the other two. -/
theorem g4_not_vacuous :
    -- admitted: a mutation carrying an inverse, at two classifications
    (declOK memSet = true ∧ Mutates memSet.cls ∧ HasDeclaredInverse memSet) ∧
    (declOK rowInsert = true ∧ Mutates rowInsert.cls ∧ HasDeclaredInverse rowInsert) ∧
    -- admitted: a mutation carrying the marker
    (declOK dbInsert = true ∧ Mutates dbInsert.cls ∧ IsEmissionMarker dbInsert) ∧
    -- refused: the raw shape, and the fake-reversible emission
    (declOK rawWrite = false ∧ declOK fakeReversibleEmission = false) ∧
    -- and the refusal is a program-level verdict, not a universal one
    CheckerAdmits goodProg 3 ∧ ¬ CheckerAdmits rawProg 3 ∧ ¬ CheckerAdmits fakeProg 3 := by
  refine ⟨⟨by decide, by decide, by decide⟩, ⟨by decide, by decide, by decide⟩,
    ⟨by decide, by decide, by decide⟩, ⟨by decide, by decide⟩, by decide, by decide, by decide⟩

/-- The transitive half, made concrete: a plain `fn` that calls a
one-way crossing is itself classified as crossing, at fuel 1 and up, and
the reach really does resolve back to `db_insert`. The old model has no
statement of this shape at all. -/
def auditLog : FnDecl := ⟨"audit_log", ["db_insert"]⟩
def wrapProg : Prog := ⟨[memRestore, dbInsert], [auditLog]⟩

theorem fn_wrapper_still_crosses :
    reachCls wrapProg 0 "audit_log" = .pure ∧
    reachCls wrapProg 1 "audit_log" = .emission ∧
    (reachCls wrapProg 1 "audit_log").crosses = true ∧
    ReachesFrom wrapProg 1 "audit_log" "db_insert" := by
  refine ⟨by decide, by decide, by decide, ?_⟩
  exact .step (by decide) .refl

end RevL.G4Classified
