/-!
RevL.Lemmas.TaintLemmas — L1 lemma farm: the taint/provenance label
lattice and its declassifiers (roadmap items 249 / 256, `src/revl/taint.py`
and `src/revl/admit_profile.py`).

Core only: this farm imports nothing, so it cannot drift into L0 and no
other farm depends on it. Two L2 files are built on it:
`RevL.Theorems.G9_NoAuthorityFromUntrusted`, the G9 guarantee, and
`RevL.Theorems.G9Flow`, the differential-oracle row issue #1811 group 2
added — which is why the `Step`/`Flow` walk they share is defined here.

## What is modelled

Item 249 Slice A/B/C makes every value carry a **set of origin labels**,
ordered by inclusion, with bottom `{}` = trusted and join = set union
(`taint._join`, Decision 2). The origin classes are a CLOSED set
(`taint._ORIGIN_CLASSES`):

| origin | minted by | policy |
|---|---|---|
| `web`, `net`, `fs`, `model`, `input` | a crossing whose declared capability scope names it (`_origin_of`) | refused at a `Trusted[T]` sink; declassifiable |
| `secret` (item 256 Slice 1) | a `secret NAME for CAP` bound provider key | refused at EVERY crossing; **no declassifier at all** |
| `confidential` (item 256 Slice 3) | a `Secret[T]` value | refused at a disclosure sink; admitted at a declared `Secret[T]` receiver; declassifiable at a declared `endorse[confidential]` |

`Origin.custom` carries the reference's residual case: `_origin_of`
returns the raw capability string when the scope head is not one of the
seven classes, and that string is a real, dirty origin no closed-set rule
names.

## Declassifiers

Two ship (`taint._FlowChecker._endorse`, `_taint_of_call`):

* the scoped `endorse[<origin>](v, reason = "...")` — clears **only** its
  own declared origin (item 249 Finding 1: a cross-origin endorse
  launders nothing), and only where the enclosing declaration granted the
  slot (`TaintModel.declared_endorse`);
* the **checked parser** — a `verified fn` returning `Trusted[T]`, which
  cleans the whole label.

The ambient originless `endorse(v)` of Slice A is a *parse error*
(`parser._endorse_expr`: "the ambient `endorse(v)` is superseded"), so it
is deliberately not a constructor here: every declassification in a
parseable program is scoped and reasoned.

Two refusals ride on the same structure and are why `secret` has no
escape at all:

* `endorse[secret]` is refused UNCONDITIONALLY, before the declared-slot
  check (`taint.py`, item 256 §4a.3) — hence `o ≠ .secret` in `kindOK`;
* a `verified fn` declassifier applied to a `secret`-carrying value is
  refused rather than laundering it — hence `.secret ∉ ℓ` in `kindOK`.

`Declassifier.granted` records whether the declassifier came from the
pre-granted closure rather than from the admitted source itself. Under
the untrusted-author profile (`AdmissionProfile.untrusted_author`, which
sets `no_declassify=True`), `admit_profile.check_no_declassify` refuses
both self-minted doors on the ROOT AST — any `endorse`, and any
`verified fn` returning `Trusted[...]`.

## Sinks

`Admits` is the *four* admission rules the flow walk actually runs, kept
distinct because they are genuinely different predicates:

* `authority` — a declared `Trusted[T]` parameter, or a
  shell/exec/terminal/policy-scoped crossing under `taint_strict`
  (`_SINK_CLASS_SCOPES`). Refuses ANY dirty label (`arg_taints[i].dirty`).
* `disclosure` — an emission crossing, a plain extern host call, or a
  provide-method return. Refuses `secret` (G-SECRET) and `confidential`
  (G-SECRET-FLOW); a provenance origin is *recorded* on the audit surface
  there, not refused, so it is admitted here.
* `secretReceiver` — a position declaring `Secret[T]` (§7b). Admits
  `confidential`, and is kept DISJOINT from the authority sinks, so it
  never refuses an `Untrusted[T]` value — but it still refuses `secret`
  (the A8 / CRITICAL 1 guarantee).
* `unnameable` — a first-class callable revl cannot name. Every argument
  position is a sink, and `secret`/`confidential` are refused
  independently of that.
-/

namespace RevL.Lemmas

/-! ## Origins and labels -/

/-- A coarse origin label (`taint._ORIGIN_CLASSES`). `custom` is
`_origin_of`'s residual: a capability scope outside the closed class set
still mints a real, dirty origin, named by the capability string. -/
inductive Origin where
  | web : Origin
  | net : Origin
  | fs : Origin
  | model : Origin
  | input : Origin
  /-- item 256 Slice 1: a capability-bound provider key. -/
  | secret : Origin
  /-- item 256 Slice 3: the `Secret[T]` value qualifier. -/
  | confidential : Origin
  /-- `_origin_of`'s residual: an unclassified capability scope. -/
  | custom : String → Origin
  deriving Repr, DecidableEq

/-- A value's taint: the set of origins it carries, bottom `{}` = trusted
(`taint.Taint.origins`). Modelled as a list read up to membership, so
`join` is `++` and the `via` diagnostic chain — which carries no
security content — is dropped. -/
abbrev Label := List Origin

/-- Set-union join (`taint._join`, Decision 2): a trusted prefix never
launders an untrusted suffix. -/
def join (a b : Label) : Label := a ++ b

/-- `Clean ℓ` — bottom of the lattice, the negation of `Taint.dirty`. -/
def Clean (ℓ : Label) : Prop := ∀ o : Origin, o ∉ ℓ

/-- The inclusion order on labels. -/
def LabelLe (a b : Label) : Prop := ∀ o : Origin, o ∈ a → o ∈ b

/-- The three origins the brief calls *untrusted* — data that arrived
from outside (`web`/`net`/`input`). `fs` and `model` are provenance
origins too and are refused at an authority sink just the same; this
predicate only sharpens the non-vacuity witnesses. -/
def untrustedB : Origin → Bool
  | .web => true
  | .net => true
  | .input => true
  | _ => false

/-- `IsUntrusted o` — `o` is a web/net/input origin. -/
def IsUntrusted (o : Origin) : Prop := untrustedB o = true

/-! ## The origin derivation (`taint._origin_of`) -/

/-- The characters of a capability token up to its first `.`. -/
def headSegment : List Char → List Char
  | [] => []
  | c :: rest => if c = '.' then [] else c :: headSegment rest

/-- The scope head of a capability token: `web.fetch` -> `web`
(`str(cap).split(".", 1)[0]`). Written over characters rather than
`String.splitOn` so it reduces in the kernel — the non-vacuity witnesses
below compute real origins from real capability tokens. -/
def scopeHead (cap : String) : String := String.ofList (headSegment cap.toList)

/-- The closed origin-class table. -/
def originOfHead : String → Option Origin
  | "web" => some .web
  | "net" => some .net
  | "fs" => some .fs
  | "model" => some .model
  | "input" => some .input
  | "secret" => some .secret
  | "confidential" => some .confidential
  | _ => none

/-- The origin a single capability token mints. -/
def originOfCap (cap : String) : Origin :=
  match originOfHead (scopeHead cap) with
  | some o => o
  | none => .custom cap

/-- `_origin_of`: the origin a crossing mints, **derived from its declared
capability scope, never guessed**. The reference returns on the FIRST
capability; an unscoped crossing mints `input`. -/
def mintedBy : List String → Origin
  | [] => .input
  | c :: _ => originOfCap c

/-- The origins a declared capability context can mint — the bridge from
this algebra down to L0's `Ctx` (a list of wiring keys), the sibling of
`CapLemmas.capKeys`. -/
def contextOrigins (Γ : List String) : Label := Γ.map originOfCap

/-! ## Declassifiers -/

/-- The two shipped declassifier forms. The ambient originless
`endorse(v)` is a parse error and is deliberately absent. -/
inductive DeclassKind where
  /-- `endorse[<origin>](v, reason = "...")`, the scoped downgrade. -/
  | endorse : Origin → DeclassKind
  /-- a `verified fn` returning `Trusted[T]` — the checked parser. -/
  | parser : DeclassKind
  deriving Repr, DecidableEq

/-- A declassifier on a data-flow path. `granted` says it came from the
pre-granted closure rather than from the admitted source itself
(`admit_profile.check_no_declassify` scopes its refusal to the ROOT
programs). -/
structure Declassifier where
  kind : DeclassKind
  granted : Bool
  deriving Repr, DecidableEq

/-- The origins the enclosing declaration granted an `endorse[...]` slot
for (`TaintModel.declared_endorse`). -/
abbrev Grants := List Origin

/-- How much the admitting side trusts the AUTHOR
(`admit_profile.AdmissionProfile`). -/
structure Profile where
  /-- item 249 Slice C: forbid the admitted root source from minting its
  own declassifier. On in `untrusted_author`. -/
  noDeclassify : Bool := false
  /-- item 249 Slice D: derive sinks and sources with no annotation. -/
  taintStrict : Bool := false
  deriving Repr, DecidableEq

/-- The untrusted-author profile (`AdmissionProfile.untrusted_author`),
in the one dimension that matters here. -/
def untrustedAuthor : Profile := ⟨true, true⟩

/-- Whether a declassifier of this kind clears origin `o`. A scoped
`endorse[p]` clears exactly `p` (item 249, Finding 1: it is not a blanket
sanitizer); a checked parser clears the label. -/
def clearsB : DeclassKind → Origin → Bool
  | .endorse p, o => p == o
  | .parser, _ => true

/-- `Clears d o` — `d` downgrades origin `o`. -/
def Clears (d : Declassifier) (o : Origin) : Prop := clearsB d.kind o = true

instance (d : Declassifier) (o : Origin) : Decidable (Clears d o) :=
  inferInstanceAs (Decidable (clearsB d.kind o = true))

/-- The label a declassifier leaves behind (`_endorse`'s `residual`, and
the parser declassifier's `return CLEAN`). -/
def applyKind : DeclassKind → Label → Label
  | .endorse p, ℓ => ℓ.filter (fun q => !(q == p))
  | .parser, _ => []

/-- The label `d` leaves behind. -/
def applyD (d : Declassifier) (ℓ : Label) : Label := applyKind d.kind ℓ

/-- The kind-level admission rule. `endorse[secret]` is refused
unconditionally (item 256 §4a.3 — a bound provider key has no downgrade
edge, so the refusal sits BEFORE the declared-slot check); every other
scoped endorse needs its declared slot; a checked parser is refused on a
`secret`-carrying value rather than laundering it. -/
def kindOK (G : Grants) (ℓ : Label) : DeclassKind → Prop
  | .endorse o => o ≠ Origin.secret ∧ o ∈ G
  | .parser => Origin.secret ∉ ℓ

/-- `DeclassOK P G d ℓ` — the checker admits declassifier `d` applied to a
value labelled `ℓ`, under profile `P` and the enclosing declaration's
grants `G`. The profile clause is
`admit_profile.check_no_declassify`: under `no_declassify` a declassifier
the admitted source minted itself is refused structurally, before
lowering. -/
def DeclassOK (P : Profile) (G : Grants) (d : Declassifier) (ℓ : Label) : Prop :=
  (P.noDeclassify = true → d.granted = true) ∧ kindOK G ℓ d.kind

/-! ## Sinks -/

/-- The four admission rules the flow walk runs at a crossing. -/
inductive Sink where
  /-- a declared `Trusted[T]` parameter, or a derived
  shell/exec/terminal/policy sink under `taint_strict`. -/
  | authority : Sink
  /-- an emission crossing, a plain extern host call, or a
  provide-method return. -/
  | disclosure : Sink
  /-- a position declaring `Secret[T]` (§7b). -/
  | secretReceiver : Sink
  /-- a first-class callable revl cannot name. -/
  | unnameable : Sink
  deriving Repr, DecidableEq

/-- Whether a sink admits a value carrying label `ℓ`. -/
def Admits : Sink → Label → Prop
  | .authority, ℓ => Clean ℓ
  | .disclosure, ℓ => Origin.secret ∉ ℓ ∧ Origin.confidential ∉ ℓ
  | .secretReceiver, ℓ => Origin.secret ∉ ℓ
  | .unnameable, ℓ => Clean ℓ

/-! ## The flow walk

`Step` and `Flow` are the walk the taint rules are stated over. They live
HERE, in L1, rather than beside the G9 guarantee that first needed them:
two L2 files now state theorems over the same walk — `RevL.G9`, the
guarantee, and `RevL.G9Flow`, the differential-oracle row issue #1811
group 2 added — and the import layering (`formal/scripts/layering_gate.py`)
forbids one guarantee file importing another, so the shared model has to
sit below both. This is the same arrangement `RevL.Lemmas.ClassLemmas`
has with `RevL.Theorems.G{4,5,8}_Classified*`.

Nothing about the walk changed in the move: `Step`, `Flow` and their
constructors are the definitions `RevL.G9` was proved against. -/

/-- One step of a data-flow path. -/
inductive Step where
  /-- a crossing whose return mints origin `o` (`model.sources`). -/
  | source : Origin → Step
  /-- a join with another value's label (`_join` / `_union_children`). -/
  | join : Label → Step
  /-- an opaque or pure hop that carries the label through. -/
  | propagate : Step
  /-- the one weakening step: an admitted declassifier. -/
  | declassify : Declassifier → Step
  deriving Repr

/-- `Flow P G ℓin steps ℓout`: under profile `P` and the enclosing
declaration's grants `G`, a value entering the path labelled `ℓin` leaves
it labelled `ℓout`. A `declassify` step carries its admission side
condition, so an inadmissible declassification is not a flow at all. -/
inductive Flow (P : Profile) (G : Grants) : Label → List Step → Label → Prop where
  | nil : ∀ ℓ, Flow P G ℓ [] ℓ
  | source : ∀ (o : Origin) (ℓ : Label) (st : List Step) (out : Label),
      Flow P G (o :: ℓ) st out → Flow P G ℓ (.source o :: st) out
  | join : ∀ (m ℓ : Label) (st : List Step) (out : Label),
      Flow P G (join m ℓ) st out → Flow P G ℓ (.join m :: st) out
  | propagate : ∀ (ℓ : Label) (st : List Step) (out : Label),
      Flow P G ℓ st out → Flow P G ℓ (.propagate :: st) out
  | declassify : ∀ (d : Declassifier) (ℓ : Label) (st : List Step) (out : Label),
      DeclassOK P G d ℓ → Flow P G (applyD d ℓ) st out →
      Flow P G ℓ (.declassify d :: st) out

/-! ## Lattice facts -/

theorem clean_nil : Clean [] := by
  intro o h
  exact absurd h (by simp)

theorem labelLe_refl (a : Label) : LabelLe a a := fun _ h => h

theorem labelLe_trans {a b c : Label} : LabelLe a b → LabelLe b c → LabelLe a c :=
  fun hab hbc o h => hbc o (hab o h)

theorem mem_join_left {o : Origin} {a b : Label} : o ∈ a → o ∈ join a b := by
  intro h; exact List.mem_append.mpr (Or.inl h)

theorem mem_join_right {o : Origin} {a b : Label} : o ∈ b → o ∈ join a b := by
  intro h; exact List.mem_append.mpr (Or.inr h)

/-- The join really is a join: a joined value carries both sides' origins,
so `taint(a + b) = taint(a) ∪ taint(b)` never launders. -/
theorem join_le_left (a b : Label) : LabelLe a (join a b) := fun _ h => mem_join_left h

theorem join_le_right (a b : Label) : LabelLe b (join a b) := fun _ h => mem_join_right h

/-- An authority sink admits nothing dirty: the whole `Trusted[T]` rule. -/
theorem authority_refuses_dirty {o : Origin} {ℓ : Label} :
    o ∈ ℓ → ¬ Admits Sink.authority ℓ := by
  intro ho hadm
  exact hadm o ho

/-! ## Declassifier facts -/

/-- An origin a declassifier does not clear survives it. This is item 249
Finding 1 as a lemma: a scoped `endorse[<origin>]` is not a blanket
sanitizer, so a value carrying a second, un-endorsed origin is still
refused downstream. -/
theorem applyKind_preserves {k : DeclassKind} {o : Origin} {ℓ : Label} :
    clearsB k o = false → o ∈ ℓ → o ∈ applyKind k ℓ := by
  intro hc ho
  cases k with
  | endorse p =>
    have hne : ¬ (o = p) := by
      intro h
      subst h
      simp [clearsB] at hc
    simp only [applyKind, List.mem_filter]
    refine ⟨ho, ?_⟩
    cases hb : (o == p) with
    | false => simp
    | true => exact absurd (eq_of_beq hb) hne
  | parser => simp [clearsB] at hc

/-- Same, at the declassifier level. -/
theorem applyD_preserves {d : Declassifier} {o : Origin} {ℓ : Label} :
    ¬ Clears d o → o ∈ ℓ → o ∈ applyD d ℓ := by
  intro hc ho
  have : clearsB d.kind o = false := by
    cases hb : clearsB d.kind o with
    | false => rfl
    | true => exact absurd (show Clears d o from hb) hc
  exact applyKind_preserves this ho

/-- **A bound provider key is never declassified.** Whatever declassifier
the checker admits, a `secret` origin comes out the other side: an
`endorse[secret]` is refused outright (§4a.3) and a checked parser is
refused on a secret-carrying value rather than laundering it. This is the
lemma the whole `Secret` half of G9 rests on. -/
theorem declassOK_secret_persists {P : Profile} {G : Grants}
    {d : Declassifier} {ℓ : Label} :
    DeclassOK P G d ℓ → Origin.secret ∈ ℓ → Origin.secret ∈ applyD d ℓ := by
  intro hok hs
  have hk := hok.2
  cases hkd : d.kind with
  | endorse o =>
    rw [hkd] at hk
    have hne : o ≠ Origin.secret := hk.1
    refine applyD_preserves ?_ hs
    intro hcl
    unfold Clears at hcl
    rw [hkd] at hcl
    exact hne (eq_of_beq hcl)
  | parser =>
    rw [hkd] at hk
    exact absurd hs hk

/-- **`endorse[secret]` is refused, unconditionally** — even when the
enclosing declaration lists `secret` in its grants, and on any profile.
A bound provider key has no "I know what I am doing" downgrade edge
(item 256 §4a.3). -/
theorem endorse_secret_refused {P : Profile} {G : Grants} {g : Bool} {ℓ : Label} :
    ¬ DeclassOK P G ⟨.endorse Origin.secret, g⟩ ℓ := by
  intro h
  exact h.2.1 rfl

/-- **A declassification is never ambient.** A scoped `endorse[o]` whose
origin the enclosing declaration did not grant is refused at admission
(item 249, Slice C). -/
theorem endorse_needs_declared_slot {P : Profile} {G : Grants} {o : Origin}
    {g : Bool} {ℓ : Label} :
    o ∉ G → ¬ DeclassOK P G ⟨.endorse o, g⟩ ℓ := by
  intro hng h
  exact hng h.2.2

/-- **A self-minted declassifier does not count.** Under the
untrusted-author profile, a declassifier the admitted source declared
itself — an `endorse` in any form, or a `verified fn` returning
`Trusted[...]` — is refused structurally on the root AST
(`admit_profile.check_no_declassify`). -/
theorem selfMinted_refused {P : Profile} {G : Grants} {d : Declassifier}
    {ℓ : Label} :
    P.noDeclassify = true → d.granted = false → ¬ DeclassOK P G d ℓ := by
  intro hp hg h
  exact absurd (h.1 hp) (by rw [hg]; simp)

/-! ## The `Ctx` bridge -/

/-- Every key of a declared context contributes its derived origin. -/
theorem originOfCap_mem {Γ : List String} {k : String} :
    k ∈ Γ → originOfCap k ∈ contextOrigins Γ := by
  intro h
  exact List.mem_map.mpr ⟨k, h, rfl⟩

/-! ## A value model for the walk (issue #2738)

`Flow` relates labels, not values: the rule at the sink says which origins
may reach it, never what a run computes. This section defines a small
value model next to the walk so that a statement about two runs can be
made. It is a model defined here. Nothing in `RevL.G9Flow` or the
differential oracle reads it, and it is not tied to the checker.

A value is `Val`: the data it carries and the label the walk gives it.
The data of a run comes from numbered inputs (`Inputs`). A crossing
(`VStep.source o i`) reads input `i` and mints origin `o`; a join
(`VStep.join m i`) combines the running value with input `i`, labelled
`m`; both ADD the input to the running data, so the output really is a
function of what was read. `propagate` carries the value through, and a
declassifier leaves the data alone and rewrites the label with `applyD`.
`VStep.toStep` forgets the payload, so a list of `VStep` is also a list of
`Step`, and the label transition of `stepV` is exactly the one `Flow`
states (`execV_label_eq`).

The observer is the disclosure sink, `Admits Sink.disclosure`: a value
whose label carries `secret` or `confidential` is not observable there.
`execV_lowEq` is the two-run statement: two runs of one admitted flow
whose inputs agree wherever the observer can see them end with the same
label, and with the same data whenever the output is observable, unless a
declassifier on the path clears `confidential`. Unlike a statement about a
path that never writes the public output, the data here is COMPUTED from
the inputs, so the proof has to use the label discipline: a join with a
`secret`-labelled input taints the label of the result, and a declassifier
that is not admitted (`endorse[secret]`) is exactly what breaks it. -/

/-- A labelled value: the data it carries and the label the walk gives it. -/
structure Val where
  data : Nat
  label : Label
  deriving Repr, DecidableEq

/-- A step of a run. `source o i` is a crossing minting origin `o` whose
result is input `i`; `join m i` combines input `i`, labelled `m`, into the
running value; `propagate` and `declassify` are as in `Step`. -/
inductive VStep where
  | source : Origin → Nat → VStep
  | join : Label → Nat → VStep
  | propagate : VStep
  | declassify : Declassifier → VStep
  deriving Repr

/-- The label-level step a run step stands for. -/
def VStep.toStep : VStep → Step
  | .source o _ => .source o
  | .join m _ => .join m
  | .propagate => .propagate
  | .declassify d => .declassify d

/-- The data the numbered inputs of a run carry. -/
abbrev Inputs := Nat → Nat

/-- One step over a labelled value. The label transition is `Flow`'s. -/
def stepV (ρ : Inputs) : VStep → Val → Val
  | .source o i, v => ⟨v.data + ρ i, o :: v.label⟩
  | .join m i, v => ⟨v.data + ρ i, join m v.label⟩
  | .propagate, v => v
  | .declassify d, v => ⟨v.data, applyD d v.label⟩

/-- Run a path over a value, left to right. -/
def execV (ρ : Inputs) : List VStep → Val → Val
  | [], v => v
  | s :: st, v => execV ρ st (stepV ρ s v)

/-- Two input assignments agree along a path where the observer can see
what is read: an input whose label the disclosure sink admits carries the
same data in both. An input labelled `secret` or `confidential` may
differ. -/
def InputsAgree (ρ₁ ρ₂ : Inputs) : List VStep → Prop
  | [] => True
  | .source o i :: st =>
      (Admits Sink.disclosure [o] → ρ₁ i = ρ₂ i) ∧ InputsAgree ρ₁ ρ₂ st
  | .join m i :: st =>
      (Admits Sink.disclosure m → ρ₁ i = ρ₂ i) ∧ InputsAgree ρ₁ ρ₂ st
  | .propagate :: st => InputsAgree ρ₁ ρ₂ st
  | .declassify _ :: st => InputsAgree ρ₁ ρ₂ st

/-- A value the disclosure sink admits stays admitted when a crossing's
origin is consed on: both the origin and the rest are admitted. -/
theorem disclosure_of_cons {o : Origin} {ℓ : Label}
    (h : Admits Sink.disclosure (o :: ℓ)) :
    Admits Sink.disclosure [o] ∧ Admits Sink.disclosure ℓ := by
  obtain ⟨hs, hc⟩ := h
  refine ⟨⟨?_, ?_⟩, ⟨?_, ?_⟩⟩
  · intro hm; exact hs (List.mem_cons.mpr (Or.inl (List.mem_singleton.mp hm)))
  · intro hm; exact hc (List.mem_cons.mpr (Or.inl (List.mem_singleton.mp hm)))
  · intro hm; exact hs (List.mem_cons_of_mem _ hm)
  · intro hm; exact hc (List.mem_cons_of_mem _ hm)

/-- Likewise for a join: both sides of the joined label are admitted. -/
theorem disclosure_of_append {m ℓ : Label}
    (h : Admits Sink.disclosure (m ++ ℓ)) :
    Admits Sink.disclosure m ∧ Admits Sink.disclosure ℓ := by
  obtain ⟨hs, hc⟩ := h
  refine ⟨⟨?_, ?_⟩, ⟨?_, ?_⟩⟩
  · intro hm; exact hs (List.mem_append.mpr (Or.inl hm))
  · intro hm; exact hc (List.mem_append.mpr (Or.inl hm))
  · intro hm; exact hs (List.mem_append.mpr (Or.inr hm))
  · intro hm; exact hc (List.mem_append.mpr (Or.inr hm))

/-- An admitted declassifier that does not clear `confidential` cannot make
an unobservable value observable: `secret` is never cleared (the two
refusals in `kindOK`), and `confidential` survives by hypothesis. -/
theorem disclosure_of_applyD {P : Profile} {G : Grants} {d : Declassifier}
    {ℓ : Label} (hok : DeclassOK P G d ℓ)
    (hnc : ¬ Clears d Origin.confidential)
    (h : Admits Sink.disclosure (applyD d ℓ)) : Admits Sink.disclosure ℓ :=
  ⟨fun hs => h.1 (declassOK_secret_persists hok hs),
   fun hc => h.2 (applyD_preserves hnc hc)⟩

/-- The label of a run is the label `Flow` states for the same path. -/
theorem execV_label_eq {P : Profile} {G : Grants} {ρ : Inputs}
    (st : List VStep) :
    ∀ {ℓ out : Label} (v : Val),
      Flow P G ℓ (st.map VStep.toStep) out → v.label = ℓ →
      (execV ρ st v).label = out := by
  induction st with
  | nil =>
    intro ℓ out v hf hv
    have hf' : Flow P G ℓ [] out := hf
    cases hf'
    exact hv
  | cons s st ih =>
    intro ℓ out v hf hv
    cases s with
    | source o i =>
      have hf' : Flow P G ℓ (Step.source o :: st.map VStep.toStep) out := hf
      cases hf' with
      | source _ _ _ _ hrest =>
        exact ih ⟨v.data + ρ i, o :: v.label⟩ hrest
          (by show o :: v.label = o :: ℓ; rw [hv])
    | join m i =>
      have hf' : Flow P G ℓ (Step.join m :: st.map VStep.toStep) out := hf
      cases hf' with
      | join _ _ _ _ hrest =>
        exact ih ⟨v.data + ρ i, join m v.label⟩ hrest
          (by show join m v.label = join m ℓ; rw [hv])
    | propagate =>
      have hf' : Flow P G ℓ (Step.propagate :: st.map VStep.toStep) out := hf
      cases hf' with
      | propagate _ _ _ hrest => exact ih v hrest hv
    | declassify d =>
      have hf' : Flow P G ℓ (Step.declassify d :: st.map VStep.toStep) out := hf
      cases hf' with
      | declassify _ _ _ _ _ hrest =>
        exact ih ⟨v.data, applyD d v.label⟩ hrest
          (by show applyD d v.label = applyD d ℓ; rw [hv])

/-- **Two runs of one admitted flow, inputs agreeing where the observer
can see.** Both end with the flow's output label, and if a disclosure sink
admits that label, both end with the same data, provided no declassifier
on the path clears `confidential`. The data is computed by adding the
inputs a crossing or join reads, so the label discipline does the work: a
read of a `secret`- or `confidential`-labelled input puts that origin on
the label of everything computed from it, and a declassifier cannot take a
`secret` off. -/
theorem execV_lowEq {P : Profile} {G : Grants} {ρ₁ ρ₂ : Inputs}
    (st : List VStep) :
    ∀ {ℓ out : Label} (v₁ v₂ : Val),
      Flow P G ℓ (st.map VStep.toStep) out →
      InputsAgree ρ₁ ρ₂ st →
      (∀ d, VStep.declassify d ∈ st → ¬ Clears d Origin.confidential) →
      v₁.label = ℓ → v₂.label = ℓ →
      (Admits Sink.disclosure ℓ → v₁.data = v₂.data) →
      (execV ρ₁ st v₁).label = out ∧ (execV ρ₂ st v₂).label = out ∧
      (Admits Sink.disclosure out →
        (execV ρ₁ st v₁).data = (execV ρ₂ st v₂).data) := by
  induction st with
  | nil =>
    intro ℓ out v₁ v₂ hf _ _ h₁ h₂ hd
    have hf' : Flow P G ℓ [] out := hf
    cases hf'
    exact ⟨h₁, h₂, hd⟩
  | cons s st ih =>
    intro ℓ out v₁ v₂ hf hag hcl h₁ h₂ hd
    have hcl' : ∀ d, VStep.declassify d ∈ st → ¬ Clears d Origin.confidential :=
      fun d hd' => hcl d (List.mem_cons_of_mem _ hd')
    cases s with
    | source o i =>
      have hf' : Flow P G ℓ (Step.source o :: st.map VStep.toStep) out := hf
      cases hf' with
      | source _ _ _ _ hrest =>
        obtain ⟨hi, hag'⟩ := hag
        exact ih ⟨v₁.data + ρ₁ i, o :: v₁.label⟩ ⟨v₂.data + ρ₂ i, o :: v₂.label⟩
          hrest hag' hcl'
          (by show o :: v₁.label = o :: ℓ; rw [h₁])
          (by show o :: v₂.label = o :: ℓ; rw [h₂])
          (fun hadm => by
            obtain ⟨ho, hl⟩ := disclosure_of_cons hadm
            show v₁.data + ρ₁ i = v₂.data + ρ₂ i
            rw [hd hl, hi ho])
    | join m i =>
      have hf' : Flow P G ℓ (Step.join m :: st.map VStep.toStep) out := hf
      cases hf' with
      | join _ _ _ _ hrest =>
        obtain ⟨hi, hag'⟩ := hag
        exact ih ⟨v₁.data + ρ₁ i, join m v₁.label⟩ ⟨v₂.data + ρ₂ i, join m v₂.label⟩
          hrest hag' hcl'
          (by show join m v₁.label = join m ℓ; rw [h₁])
          (by show join m v₂.label = join m ℓ; rw [h₂])
          (fun hadm => by
            obtain ⟨hm, hl⟩ := disclosure_of_append hadm
            show v₁.data + ρ₁ i = v₂.data + ρ₂ i
            rw [hd hl, hi hm])
    | propagate =>
      have hf' : Flow P G ℓ (Step.propagate :: st.map VStep.toStep) out := hf
      cases hf' with
      | propagate _ _ _ hrest => exact ih v₁ v₂ hrest hag hcl' h₁ h₂ hd
    | declassify d =>
      have hf' : Flow P G ℓ (Step.declassify d :: st.map VStep.toStep) out := hf
      cases hf' with
      | declassify _ _ _ _ hok hrest =>
        exact ih ⟨v₁.data, applyD d v₁.label⟩ ⟨v₂.data, applyD d v₂.label⟩
          hrest hag hcl'
          (by show applyD d v₁.label = applyD d ℓ; rw [h₁])
          (by show applyD d v₂.label = applyD d ℓ; rw [h₂])
          (fun hadm => hd (disclosure_of_applyD hok
            (hcl d (by simp)) hadm))

/-- An origin no declassifier on the path clears is still on the label when
the run ends: the label half of `origin_persists_or_is_declassified`, over
the value model. -/
theorem execV_preserves_uncleared_origin (o : Origin) (ρ : Inputs) :
    ∀ (st : List VStep) (v : Val),
      (∀ d, VStep.declassify d ∈ st → ¬ Clears d o) →
      o ∈ v.label → o ∈ (execV ρ st v).label
  | [], v, _, ho => ho
  | .source o' i :: st, v, h, ho =>
      execV_preserves_uncleared_origin o ρ st (stepV ρ (.source o' i) v)
        (fun d hd => h d (List.mem_cons_of_mem _ hd))
        (List.mem_cons_of_mem o' ho)
  | .join m i :: st, v, h, ho =>
      execV_preserves_uncleared_origin o ρ st (stepV ρ (.join m i) v)
        (fun d hd => h d (List.mem_cons_of_mem _ hd))
        (mem_join_right ho)
  | .propagate :: st, v, h, ho =>
      execV_preserves_uncleared_origin o ρ st (stepV ρ .propagate v)
        (fun d hd => h d (List.mem_cons_of_mem _ hd)) ho
  | .declassify d :: st, v, h, ho =>
      execV_preserves_uncleared_origin o ρ st (stepV ρ (.declassify d) v)
        (fun d' hd' => h d' (List.mem_cons_of_mem _ hd'))
        (applyD_preserves (h d (by simp)) ho)

end RevL.Lemmas
