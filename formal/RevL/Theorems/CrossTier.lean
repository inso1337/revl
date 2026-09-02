import RevL.Syntax

/-!
# Item 133 — the cross-tier agreement theorem

Roadmap item 133: "Formalize the conditions under which six runtimes
produce identical values for the same IR (operands annotation, Int/Float,
code-point Str unit, canonical Map order) and prove the lowering preserves
semantics across tiers."

## What is modelled

revl lowers one IR to six backends (python / typescript / go / rust /
java / wasm). The DIVERGENCES table records the three axes on which a
naive lowering makes the tiers observe *different* values for the *same*
IR:

* **Int/Float.** An unannotated numeric literal is read with each tier's
  native default (python/go/rust/java lean integer, typescript/wasm lean
  IEEE double), so the tiers disagree on its tag. revl's answer is the
  **operand annotation**: the IR carries the numeric tag explicitly.
* **code-point Str unit.** A string's observable length/segmentation
  differs between a code-point tier and a UTF-16 tier. revl mandates the
  **code-point unit** for every conformant runtime.
* **canonical Map order.** Map iteration order is tier-native unless
  pinned. revl mandates a single **canonical order** for every conformant
  runtime.

We model each backend by its *observable profile* on these three axes and
prove: once the runtime-side conditions hold (code-point strings,
canonical map order) and the IR-side condition holds (every numeric
literal annotated), every pair of the six tiers lowers the IR to the
*same* observable value.

## Scope, stated honestly

* A backend is modelled by the value it *observes*, not by its emitter
  source. That the real python/rust/... emitters realise a conformant
  profile is the differential conformance matrix's job (an empirical
  obligation), not a Lean theorem; it is out of scope here by
  construction and is NOT smuggled in as an axiom.
* Map values are modelled one level deep (entries are atoms). The three
  divergence axes of item 133 are all leaf/entry-order properties, so a
  flat map exercises every axis; nested maps are a mechanical extension
  of `entries_agree` and are noted, not proved.

The `numDefault` field of a profile is deliberately left UNconstrained by
conformance: the six tiers genuinely differ there, and
`annotation_necessary` below exhibits that disagreement. It is the
IR-side annotation condition, not a runtime condition, that neutralises
it — which is the point of the theorem.
-/

namespace RevL.CrossTier

/-! ## Observable values -/

/-- A leaf observable value. A number carries a tag (`true` = Int,
`false` = Float) and an opaque payload; a string is a list of Unicode
code points. -/
inductive Atom where
  | num : Bool → Int → Atom
  | str : List Nat → Atom
  deriving Repr, DecidableEq

/-- An observable value: a leaf, or a map (association list of
code-point-keyed entries). -/
inductive Value where
  | atom : Atom → Value
  | map : List (List Nat × Atom) → Value
  deriving Repr, DecidableEq

/-! ## The intermediate representation -/

/-- A leaf IR node. A numeric literal carries an *optional* operand
annotation: `some tag` when the operand is annotated, `none` when the
source left it implicit. -/
inductive AtomIR where
  | num : Option Bool → Int → AtomIR
  | str : List Nat → AtomIR
  deriving Repr

/-- An IR expression: a leaf, or a map literal. -/
inductive IR where
  | atom : AtomIR → IR
  | map : List (List Nat × AtomIR) → IR
  deriving Repr

/-! ## The canonical map order

The concrete order is irrelevant to agreement — the theorem needs only
that every conformant runtime uses *the same* one. We give a real
definition (lexicographic insertion sort on the code-point key) so the
model is executable, but no proof below inspects it. -/

/-- Lexicographic `≤` on code-point keys. -/
def keyLe : List Nat → List Nat → Bool
  | [], _ => true
  | _ :: _, [] => false
  | a :: as, b :: bs =>
      if a < b then true else if b < a then false else keyLe as bs

/-- Insert one entry into an already-ordered entry list. -/
def insertEntry (x : List Nat × Atom) :
    List (List Nat × Atom) → List (List Nat × Atom)
  | [] => [x]
  | y :: ys => if keyLe x.1 y.1 then x :: y :: ys else y :: insertEntry x ys

/-- The canonical Map iteration order revl mandates across every tier. -/
def canonOrder : List (List Nat × Atom) → List (List Nat × Atom)
  | [] => []
  | x :: xs => insertEntry x (canonOrder xs)

/-! ## Runtime profiles and lowering -/

/-- The observable behaviour of one backend on the three divergence axes.
`numDefault` is the tag a runtime gives an *unannotated* numeric literal;
`strNorm` is how it observes a string's code-point sequence; `mapOrder`
is its map-iteration order. -/
structure Profile where
  numDefault : Bool
  strNorm : List Nat → List Nat
  mapOrder : List (List Nat × Atom) → List (List Nat × Atom)

/-- Lower one leaf: an annotated literal keeps its tag; an unannotated one
falls back to the tier's native default; a string is observed through the
tier's string unit. -/
def evalAtom (p : Profile) : AtomIR → Atom
  | .num (some t) n => .num t n
  | .num none n => .num p.numDefault n
  | .str s => .str (p.strNorm s)

/-- Lower one map entry (key preserved, value lowered). -/
def evalEntry (p : Profile) (e : List Nat × AtomIR) : List Nat × Atom :=
  (e.1, evalAtom p e.2)

/-- Lower an IR expression to the value the tier observes. -/
def eval (p : Profile) : IR → Value
  | .atom a => .atom (evalAtom p a)
  | .map es => .map (p.mapOrder (es.map (evalEntry p)))

/-! ## The conditions of item 133 -/

/-- IR-side condition: a leaf is *well annotated* when every numeric
literal carries its operand tag. -/
def WellAnnotatedAtom : AtomIR → Prop
  | .num none _ => False
  | .num (some _) _ => True
  | .str _ => True

/-- IR-side condition on a whole expression: every numeric literal in it
(a leaf, or a map value) is operand-annotated. -/
def WellAnnotated : IR → Prop
  | .atom a => WellAnnotatedAtom a
  | .map es => ∀ e ∈ es, WellAnnotatedAtom e.2

/-- Runtime-side conditions (the two conformance conditions of item 133):
the tier observes strings by **code point** (its string normalisation is
the identity on code-point sequences), and iterates maps in the mandated
**canonical order**. The numeric default is intentionally free. -/
structure Conformant (p : Profile) : Prop where
  strCodePoint : p.strNorm = id
  mapCanonical : p.mapOrder = canonOrder

/-! ## Agreement -/

/-- Leaf agreement: two conformant tiers lower a well-annotated leaf to
the same atom. This is where the two runtime conditions and the
annotation condition each do their work — the `none` case is exactly the
one the annotation condition rules out. -/
theorem evalAtom_agree (p q : Profile) (hp : Conformant p) (hq : Conformant q)
    (a : AtomIR) (h : WellAnnotatedAtom a) : evalAtom p a = evalAtom q a := by
  cases a with
  | num o n =>
    cases o with
    | none => simp only [WellAnnotatedAtom] at h
    | some t => rfl
  | str s =>
    simp only [evalAtom, hp.strCodePoint, hq.strCodePoint]

/-- Entry-list agreement: over a well-annotated entry list, lowering per
tier is pointwise-equal, so the lists of lowered entries coincide. -/
theorem entries_agree (p q : Profile) (hp : Conformant p) (hq : Conformant q) :
    ∀ es : List (List Nat × AtomIR),
      (∀ e ∈ es, WellAnnotatedAtom e.2) →
      es.map (evalEntry p) = es.map (evalEntry q)
  | [], _ => rfl
  | e :: es, h => by
      have hhead : evalEntry p e = evalEntry q e := by
        unfold evalEntry
        rw [evalAtom_agree p q hp hq e.2 (h e (by simp))]
      have htail : es.map (evalEntry p) = es.map (evalEntry q) :=
        entries_agree p q hp hq es
          (fun x hx => h x (by simp [hx]))
      simp only [List.map_cons, hhead, htail]

/-- **Cross-tier agreement (item 133).** Any two conformant runtimes
lower a well-annotated IR expression to the *same* observable value.

The hypotheses are exactly the item-133 conditions: `hwf` is the operand
annotation condition (Int/Float), and `hp`/`hq` package the code-point
string unit and the canonical map order. Nothing else is assumed. -/
theorem cross_tier_agreement (p q : Profile)
    (hp : Conformant p) (hq : Conformant q)
    (e : IR) (hwf : WellAnnotated e) : eval p e = eval q e := by
  cases e with
  | atom a =>
    simp only [eval]
    rw [evalAtom_agree p q hp hq a hwf]
  | map es =>
    simp only [eval, hp.mapCanonical, hq.mapCanonical]
    rw [entries_agree p q hp hq es hwf]

/-! ## The six runtimes concretely -/

/-- revl's six lowering tiers. -/
inductive Tier where
  | python | typescript | go | rust | java | wasm
  deriving Repr, DecidableEq

/-- The six tiers, as a list — the "six runtimes" of item 133. -/
def allTiers : List Tier :=
  [.python, .typescript, .go, .rust, .java, .wasm]

/-- Each tier's native numeric default (illustrative of the DIVERGENCES
table: the integer-leaning tiers vs the IEEE-double-leaning tiers). The
agreement theorem does not depend on these specific values — only on the
fact that a conformant profile leaves them free, so annotation must pin
the tag. -/
def nativeNumDefault : Tier → Bool
  | .python => true
  | .go => true
  | .rust => true
  | .java => true
  | .typescript => false
  | .wasm => false

/-- The observable profile of a tier: its native numeric default, plus
the two conditions revl mandates (code-point strings, canonical map
order). -/
def profileOf (t : Tier) : Profile :=
  { numDefault := nativeNumDefault t, strNorm := id, mapOrder := canonOrder }

/-- Every tier's mandated profile is conformant by construction. -/
theorem profileOf_conformant (t : Tier) : Conformant (profileOf t) :=
  ⟨rfl, rfl⟩

/-- **Six-runtime agreement (item 133).** For any two of the six tiers and
any well-annotated IR expression, the two tiers observe the same value —
the "six runtimes produce identical values for the same IR" statement,
under the stated conditions. -/
theorem six_tier_agreement (t u : Tier) (e : IR) (hwf : WellAnnotated e) :
    eval (profileOf t) e = eval (profileOf u) e :=
  cross_tier_agreement _ _ (profileOf_conformant t) (profileOf_conformant u) e hwf

/-- The operand-annotation condition is **necessary**, not decorative:
without it two tiers with different numeric defaults disagree on an
unannotated literal. Here python (integer default) and typescript (double
default) observe different values for the bare literal `0`. This is why
`WellAnnotated` is a genuine hypothesis of the theorem above and not a
vacuous one. -/
theorem annotation_necessary :
    ∃ e : IR, eval (profileOf .python) e ≠ eval (profileOf .typescript) e := by
  refine ⟨.atom (.num none 0), ?_⟩
  decide


/-- A well-annotated map IR: one annotated numeric literal and one
string, keyed out of canonical order. -/
def annotatedIR : IR :=
  .map [([104, 105], .num (some true) 7), ([97], .str [98, 99])]

/-- **Non-vacuity** (roadmap item 418, step 8). `cross_tier_agreement`'s
three hypotheses are jointly satisfiable, and not only on a degenerate
IR: two real tiers are conformant, `annotatedIR` is well annotated, and
the shared value both observe is a re-ordered, non-empty map, so the
agreement claim is about a value the model actually computes.
`annotation_necessary` above supplies the other half, that the
annotation hypothesis cannot be dropped. -/
theorem conformance_hypotheses_are_inhabited :
    Conformant (profileOf .python) ∧ Conformant (profileOf .typescript) ∧
    WellAnnotated annotatedIR ∧
    eval (profileOf .python) annotatedIR =
      .map [([97], .str [98, 99]), ([104, 105], .num true 7)] ∧
    eval (profileOf .python) annotatedIR =
      eval (profileOf .typescript) annotatedIR := by
  have hwf : WellAnnotated annotatedIR := by
    intro e he
    simp only [List.mem_cons, List.not_mem_nil, or_false] at he
    rcases he with rfl | rfl <;> trivial
  refine ⟨profileOf_conformant _, profileOf_conformant _, hwf, rfl,
    six_tier_agreement _ _ _ hwf⟩

end RevL.CrossTier
