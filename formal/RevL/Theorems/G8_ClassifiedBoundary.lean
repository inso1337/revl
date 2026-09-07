import RevL.Boundary
import RevL.Lemmas.ClassLemmas

/-!
# G8, restated without the definitional escape (item 418, step 4)

## What was wrong with the old statement

`RevL.Theorems.G8_Boundary.boundary_only_declared` rests on
`RevL.Boundary.boundaryOf`, whose second line is

    | .effect _ _ => []

**by definition**. So a typed `.effect (.call "http.post" ...) undo` has
an empty boundary surface, and the soundness direction ("everything on
the surface is an emission") is true because the surface was defined to
contain only what the `.emit` marker put there. The audit is being
proved to enumerate the marker, not the crossings.

The compiler does not work that way. `emission_analysis`'s
`_emitting_capabilities` computes, for every name, the set of
capabilities its call TRANSITIVELY reaches — seeded from `emission`
externs (which contribute their own name) and `witnessed` externs
(which contribute their declared scope, "a witnessed extern crosses the
same boundary as an emission"). The audit surface is that set.

## What is stated here instead

`stmtSurface` is `stmtHeads` composed with the reach fold, **uniformly
over all four statement forms**. There is no per-constructor case, so
there is no per-constructor escape: whether a statement is on the
boundary surface is decided by what its calls reach, which is the
classification.

Both directions of the old G8 are kept:

* `surface_enumerates_reached_crossings` (completeness) — nothing a
  reachable crossing declares is dropped;
* `surface_only_declared_crossings` (soundness) — everything on the
  surface traces back to a concrete, reachable, boundary-crossing
  declaration.

Soundness now needs NO typing hypothesis. The old one needed `Typed` to
kill the `raw` case; here a `raw` leak is on the surface like anything
else (`raw_leak_is_on_the_surface`), so the audit is honest about code
the checker would refuse rather than depending on the refusal.

`effect_carrying_emission_is_on_the_surface` is the anti-tautology
guard: it exhibits the statement the old definition hides, and the
statement the old definition invents, side by side with both verdicts.
-/

namespace RevL.G8Classified

open RevL.Lemmas RevL.Syntax RevL.Typing

/-! ## The surface, computed from the classification -/

/-- The boundary surface of one statement: the capabilities its call
heads transitively reach. One clause, all four statement forms — the
classification decides, not the constructor. -/
def stmtSurface (p : Prog) (fuel : Nat) (s : Stmt) : List Name :=
  (stmtHeads s).flatMap (reachCaps p fuel)

/-- The enumerable boundary surface of a body (`revl audit`). -/
def bodySurface (p : Prog) (fuel : Nat) (body : List Stmt) : List Name :=
  body.flatMap (stmtSurface p fuel)

/-! ## Both directions -/

/-- **Completeness.** Every capability that a statement's calls reach —
through any depth of `fn` indirection — appears on the audited surface
of the body containing it. Nothing crossing is dropped. -/
theorem surface_enumerates_reached_crossings {p : Prog} {fuel : Nat}
    {body : List Stmt} {s : Stmt} (hs : s ∈ body)
    {h : Name} (hh : h ∈ stmtHeads s) {n : Name} {d : ExternDecl}
    (hr : ReachesFrom p fuel h n) (hd : lookupExtern p n = some d)
    {k : Name} (hk : k ∈ capsOfDecl d) : k ∈ bodySurface p fuel body := by
  refine List.mem_flatMap.mpr ⟨s, hs, ?_⟩
  exact List.mem_flatMap.mpr ⟨h, hh, reachCaps_complete hr hd hk⟩

/-- **Soundness.** Everything on the audited surface is a capability
some reachable, boundary-crossing declaration actually declares. There
is no typing hypothesis: the statement holds of untyped bodies too, so
the audit does not borrow its correctness from the checker. -/
theorem surface_only_declared_crossings {p : Prog} {fuel : Nat}
    {body : List Stmt} {k : Name} (hk : k ∈ bodySurface p fuel body) :
    ∃ s ∈ body, ∃ h ∈ stmtHeads s, ∃ n d, ReachesFrom p fuel h n ∧
      lookupExtern p n = some d ∧ d.cls.crosses = true ∧ k ∈ capsOfDecl d := by
  obtain ⟨s, hs, hks⟩ := List.mem_flatMap.mp hk
  obtain ⟨h, hh, hkh⟩ := List.mem_flatMap.mp hks
  obtain ⟨n, d, hr, hd, hc, hkd⟩ := reachCaps_sound p fuel h k hkh
  exact ⟨s, hs, h, hh, n, d, hr, hd, hc, hkd⟩

/-- A statement with a non-empty surface has a crossing classification
behind every head that contributed to it: the capability fold and the
classification fold agree. -/
theorem surface_implies_crossing {p : Prog} {fuel : Nat} {s : Stmt} {k : Name}
    (hk : k ∈ stmtSurface p fuel s) :
    ∃ h ∈ stmtHeads s, (reachCls p fuel h).crosses = true := by
  obtain ⟨h, hh, hkh⟩ := List.mem_flatMap.mp hk
  exact ⟨h, hh, reachCaps_crosses hkh⟩

/-! ## Non-vacuity: the definitional escape, closed

The witness program: one host-local restore, one one-way crossing, one
reversible crossing scoped to `db`, and one `fn` that wraps the one-way
crossing. -/

def memRestore : ExternDecl := ⟨"mem_restore", .pure, none, none, []⟩
def dbInsert : ExternDecl := ⟨"db_insert", .emission, none, none, []⟩
def rowInsert : ExternDecl := ⟨"row_insert", .witnessed, some "mem_restore", none, ["db"]⟩
def auditLog : FnDecl := ⟨"audit_log", ["db_insert"]⟩

def witProg : Prog := ⟨[memRestore, dbInsert, rowInsert], [auditLog]⟩

/-- The statement the review names: an `effect` whose mutation is an
emission-classified call. -/
def effectWithEmission : Stmt := .effect (.call "db_insert" []) (.call "mem_restore" [])

/-- A genuinely host-local `effect`: nothing it calls crosses. -/
def effectLocal : Stmt := .effect (.call "mem_restore" []) (.call "mem_restore" [])

/-- An `effect` carrying the reversible crossing: on the surface too,
named by its declared scope. -/
def effectWitnessed : Stmt := .effect (.call "row_insert" []) (.call "mem_restore" [])

/-- A `pure` statement whose call reaches the crossing through a `fn`. -/
def pureWrappedCrossing : Stmt := .pure (.call "audit_log" [])

/-- An `emit` marker on a call that crosses nothing. -/
def emitLocal : Stmt := .emit (.call "mem_restore" [])

/-- An `emit` marker on a real crossing: the case where marker and
classification agree. -/
def emitCrossing : Stmt := .emit (.call "db_insert" [])

/-- The surface, once the call heads are known. `RevL.Typing.heads` is
compiled by well-founded recursion, so its head list is established by
`simp` and the reach fold is then evaluated by the kernel. -/
theorem surface_of_heads {p : Prog} {fuel : Nat} {s : Stmt} {l : List Name}
    (h : stmtHeads s = l) : stmtSurface p fuel s = l.flatMap (reachCaps p fuel) := by
  unfold stmtSurface; rw [h]

theorem heads_effectWithEmission :
    stmtHeads effectWithEmission = ["db_insert", "mem_restore"] := by
  simp [effectWithEmission, stmtHeads, heads]

theorem heads_effectLocal :
    stmtHeads effectLocal = ["mem_restore", "mem_restore"] := by
  simp [effectLocal, stmtHeads, heads]

theorem heads_effectWitnessed :
    stmtHeads effectWitnessed = ["row_insert", "mem_restore"] := by
  simp [effectWitnessed, stmtHeads, heads]

theorem heads_pureWrappedCrossing :
    stmtHeads pureWrappedCrossing = ["audit_log"] := by
  simp [pureWrappedCrossing, stmtHeads, heads]

theorem heads_emitLocal : stmtHeads emitLocal = ["mem_restore"] := by
  simp [emitLocal, stmtHeads, heads]

theorem heads_emitCrossing : stmtHeads emitCrossing = ["db_insert"] := by
  simp [emitCrossing, stmtHeads, heads]

/-- **The definitional escape, closed.** Four verdicts, each paired with
the old model's verdict on the same statement:

* an `effect` carrying an emission IS on the classified surface, and is
  NOT on the old one (`boundaryOf (.effect _ _) = []`);
* a `pure` statement reaching the crossing through a `fn` is likewise on
  one and not the other;
* an `emit` marker on a host-local call is on the OLD surface and not
  the classified one — the old surface enumerates the marker, this one
  enumerates the crossing;
* a genuinely local `effect` is on neither.

The two models therefore disagree in both directions, which is what
makes this a different theorem rather than a restatement. -/
theorem effect_carrying_emission_is_on_the_surface :
    stmtSurface witProg 3 effectWithEmission = ["db_insert"] ∧
    RevL.Boundary.boundaryOf effectWithEmission = [] ∧
    stmtSurface witProg 3 pureWrappedCrossing = ["db_insert"] ∧
    RevL.Boundary.boundaryOf pureWrappedCrossing = [] ∧
    stmtSurface witProg 3 emitLocal = [] ∧
    RevL.Boundary.boundaryOf emitLocal = ["mem_restore"] ∧
    stmtSurface witProg 3 effectLocal = [] ∧
    RevL.Boundary.boundaryOf effectLocal = [] := by
  refine ⟨?_, rfl, ?_, rfl, ?_, ?_, ?_, rfl⟩
  · rw [surface_of_heads heads_effectWithEmission]; decide
  · rw [surface_of_heads heads_pureWrappedCrossing]; decide
  · rw [surface_of_heads heads_emitLocal]; decide
  · simp [emitLocal, RevL.Boundary.boundaryOf, heads]
  · rw [surface_of_heads heads_effectLocal]; decide

/-- Where the marker is honest, the two agree — so the classified
surface is a refinement of the old one, not a different subject. The
witnessed crossing is named by its declared scope (`db`), which the old
model cannot express at all. -/
theorem surface_agrees_with_an_honest_marker :
    stmtSurface witProg 3 emitCrossing = ["db_insert"] ∧
    RevL.Boundary.boundaryOf emitCrossing = ["db_insert"] ∧
    stmtSurface witProg 3 effectWitnessed = ["db"] := by
  refine ⟨?_, ?_, ?_⟩
  · rw [surface_of_heads heads_emitCrossing]; decide
  · simp [emitCrossing, RevL.Boundary.boundaryOf, heads]
  · rw [surface_of_heads heads_effectWitnessed]; decide

/-- The old soundness direction needed `Typed` to discharge the `raw`
case. This surface does not: a `raw` leak's reached crossings are
enumerated like everything else, so `surface_only_declared_crossings`
can drop the typing hypothesis the old statement carried. -/
theorem raw_leak_is_on_the_surface :
    stmtSurface witProg 3 (.raw (.call "db_insert" [])) = ["db_insert"] ∧
    RevL.Boundary.boundaryOf (.raw (.call "db_insert" [])) = ["db_insert"] := by
  refine ⟨?_, ?_⟩
  · rw [surface_of_heads (show stmtHeads (.raw (.call "db_insert" [])) = ["db_insert"] from by
      simp [stmtHeads, heads])]
    decide
  · simp [RevL.Boundary.boundaryOf, heads]

/-- **G8 is non-vacuous.** A body the audit puts on the surface, a body
it correctly leaves off, and the off-surface body is not the empty body
— so "nothing is on the surface" is not the trivial reading. -/
theorem g8_surface_is_not_universal :
    bodySurface witProg 3 [effectWithEmission, effectWitnessed, emitLocal]
      = ["db_insert", "db"] ∧
    bodySurface witProg 3 [effectLocal, emitLocal] = [] ∧
    ([effectLocal, emitLocal] : List Stmt) ≠ [] := by
  have s1 : stmtSurface witProg 3 effectWithEmission = ["db_insert"] := by
    rw [surface_of_heads heads_effectWithEmission]; decide
  have s2 : stmtSurface witProg 3 effectWitnessed = ["db"] := by
    rw [surface_of_heads heads_effectWitnessed]; decide
  have s3 : stmtSurface witProg 3 emitLocal = [] := by
    rw [surface_of_heads heads_emitLocal]; decide
  have s4 : stmtSurface witProg 3 effectLocal = [] := by
    rw [surface_of_heads heads_effectLocal]; decide
  refine ⟨?_, ?_, by simp⟩
  · simp [bodySurface, s1, s2, s3]
  · simp [bodySurface, s3, s4]

/-- Soundness, discharged concretely: the surface entry `db_insert`
really does trace back through a reachable path to the `emission`
declaration that owns it. -/
theorem witness_surface_traces_to_its_declaration :
    ReachesFrom witProg 1 "audit_log" "db_insert" ∧
    lookupExtern witProg "db_insert" = some dbInsert ∧
    dbInsert.cls.crosses = true ∧
    "db_insert" ∈ capsOfDecl dbInsert ∧
    "db_insert" ∈ bodySurface witProg 3 [pureWrappedCrossing] := by
  refine ⟨.step (by decide) .refl, by decide, by decide, by decide, ?_⟩
  have s5 : stmtSurface witProg 3 pureWrappedCrossing = ["db_insert"] := by
    rw [surface_of_heads heads_pureWrappedCrossing]; decide
  simp [bodySurface, s5]

/-! ## The oracle's S8 row is mutation-sensitive (issue 276)

The differential harness exports an `S8` boundary-surface row for every
reconstructed statement and compares the Lean `stmtSurface` against an
independent Python fold (`formal/harness/diff_corpus.py`). A row that was
empty on every statement — or non-empty on every statement — would agree
vacuously, so the row's ratchet demands both. This is the model side: the
surface turns on whether the wrapping `fn` reaches the crossing. -/

/-- The wrapping fn with the emission removed — the shipped-side perturbation
the S8 row's coverage names. -/
def auditLogClean : FnDecl := ⟨"audit_log", []⟩

def witProgClean : Prog := ⟨[memRestore, dbInsert, rowInsert], [auditLogClean]⟩

/-- **The S8 row flips under perturbation.** A `pure` statement reaching the
crossing through a `fn` has a non-empty surface exactly while the `fn` reaches
the emission; the surface goes empty the moment it does not. So a non-empty
`S8 surface=db_insert` is a claim about the reach fold, and the differential
would catch a Python fold that lost the wrapped crossing. -/
theorem g8_row_not_vacuous :
    stmtSurface witProg 3 pureWrappedCrossing = ["db_insert"] ∧
    stmtSurface witProgClean 3 pureWrappedCrossing = [] := by
  refine ⟨?_, ?_⟩
  · rw [surface_of_heads heads_pureWrappedCrossing]; decide
  · rw [surface_of_heads heads_pureWrappedCrossing]; decide

end RevL.G8Classified
