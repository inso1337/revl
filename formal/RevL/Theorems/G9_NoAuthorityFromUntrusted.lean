import RevL.Typing
import RevL.Lemmas.ReachLemmas
import RevL.Lemmas.TaintLemmas

/-!
# G9 — untrusted data gains no authority

`src/revl/taint.py`, first line: "**untrusted input cannot DIRECTLY
create authority.**" A value that returns across an untrusted-origin
boundary carries that origin; a position that grants authority declares
its parameter `Trusted[T]`; the checker refuses the flow unless a
declassifier sits on the path. The refusal is tagged **G9**.

`formal/STATUS.md` TODO 3 listed the `Trusted[T]`/`Secret[T]`
non-interference half as deferred because "taint extends L0". It does
not: taint is a property of *values flowing along a path*, and L0's
`Ctx`/`ReachIn` is a property of *which keys a statement touches*. The
two meet at one bridge — the origin a crossing mints is derived from its
declared capability scope (`taint._origin_of`) — so the label algebra
lives in the L1 farm `RevL.Lemmas.TaintLemmas` and the guarantees live
here, exactly as `RevL.Theorems.CapCeilings` sits on
`RevL.Lemmas.CapLemmas`. **L0 is untouched.**

## The model

A `Flow` is one data-flow path through a body, as a list of `Step`s: the
monotone propagation the reference's transfer function performs (a
crossing minting its declared origin, a join with another value, an
opaque/pure call carrying the label through), plus the one *weakening*
step — an admitted declassifier. Everything the reference does to a label
is one of these:

| reference | step |
|---|---|
| `_taint_of_call`: `callee in model.sources` -> `Taint({origin})` | `.source` / `crossing` |
| `_join`, `_union_children`, the `emit` outbound fold | `.join` |
| an opaque call, a `let`/`assign`, a field read, a signature's `flows_to_return` | `.propagate` |
| `_endorse`, the `declassifiers` clean | `.declassify` |

The reference is a *monotone set-union* propagation biased to refusing
("the static over-approximation, biased to refusing, never clean"), so
modelling propagation as label-preserving and join as growth is the sound
reading of it: nothing here claims a step *loses* taint except the one
step that is allowed to.

## Non-vacuity, up front (roadmap item 418)

The adversarial review of 2026-09-02 found that G4/G5/G6/G8 are
tautologies over a chosen inductive — the identical statements hold of a
typing relation that admits nothing — and that only 3 of 25 theorems
carry non-vacuity evidence. This file is written against that bar, and
carries four separate guards, all proved below:

* `g9_not_vacuous` and `secret_rules_not_vacuous` exhibit **inhabited**
  flows, so nothing here is true by an empty relation;
* `authority_refusal_is_not_universal` exhibits ONE flow that an
  authority sink refuses and a disclosure sink admits, so the conclusion
  depends on the sink rule rather than refusing everything;
* `sink_rules_are_distinct` separates all four `Admits` rules pairwise,
  so this is not one predicate written four times (the review's "G1 and
  G6 are literally the same theorem" finding);
* `secret_refusal_is_load_bearing` shows the label algebra **can** clear a
  `secret` — the very same declassifier forms clear every other origin —
  so `secret_persists` holds because of `taint.py`'s two refusals, not
  because the datatype lacks a constructor for the bad case. Delete
  either refusal from `kindOK` and the theorem becomes false.

## Scope — what G9 does NOT cover

Stated plainly, because the review's finding is that STATUS.md claimed
more than the Lean proved.

* **Path coverage is NOT proved, and this is where the real bugs live.**
  `Flow` starts from a path that is *given*. That the checker *walks*
  every path a program contains is a separate obligation, and it is the
  one that actually broke: `taint._walk_component_methods` descends only
  into `provide` steps, so a component's **activation body was never
  taint-checked at all** (fixed on `fix/taint-activation-body-and-secret-
  receiver`), and a `Secret[T]` parameter was stripped inside its own
  receiver body. Nothing in this file would have caught either. The
  obligation cannot even be *stated* against the current L0: L0 has no
  component bodies, no `provide`/activation distinction, and no typed
  parameters — the exact structure both bugs live in. It is therefore
  recorded as a named open obligation in `formal/STATUS.md`, not smuggled
  in as a proved row and not `sorry`'d as a statement that does not
  typecheck.
* The interprocedural fixed point (`_Signature`, `_infer_signatures`)
  that discovers which paths exist is likewise unmodelled. As `CrossTier`
  says of its emitters: the model proves what follows once the path is
  exhibited.
* This is the STATIC half (Slice A/B/C/D). The runtime tag of Slice B
  (item 243) is not modelled; nothing here claims a runtime property.
* `Admits` classifies a sink; that the checker *labels* the right
  positions as sinks (`TaintModel.sinks`, `_sink_of`,
  `_SINK_CLASS_SCOPES`) is extraction, not theorem. The origin half of
  that labelling IS proved: `taint_surface_within_declared_context`
  composes with G6 to bound the origins a statement can mint by its
  declared context.
* No differential-oracle row references these definitions; the oracle
  carries no taint verdicts, so G9 is not covered by that gate either
  (review C4 applies here as much as elsewhere).
* The `via` diagnostic chain carries no security content and is dropped.
-/

namespace RevL.G9

open RevL.Lemmas RevL.Typing RevL.Syntax

/-! ## Flows -/

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

/-- A crossing named by its declared capability scope: the origin is
*derived* (`taint._origin_of`), never guessed. `crossing ["web.fetch"]`
mints `web`; `crossing []` — an unscoped crossing — mints `input`. -/
def crossing (caps : List String) : Step := .source (mintedBy caps)

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

/-- The declassifiers a path performs — the audit surface of the flow
(`_FlowChecker.declassify_records`). -/
def declassifiersOf : List Step → List Declassifier
  | [] => []
  | .declassify d :: rest => d :: declassifiersOf rest
  | _ :: rest => declassifiersOf rest

/-! ## The core lemma -/

/-- Along any admitted flow, every origin the value entered with either
comes out the other side or was cleared by a declassifier *on that path*.
There is no third possibility: no propagation step loses taint. -/
theorem origin_persists_or_is_declassified {P : Profile} {G : Grants}
    {ℓin : Label} {st : List Step} {ℓout : Label} (o : Origin) :
    Flow P G ℓin st ℓout → o ∈ ℓin →
    o ∈ ℓout ∨ ∃ d ∈ declassifiersOf st, Clears d o := by
  intro hf
  induction hf with
  | nil ℓ => intro ho; exact Or.inl ho
  | source o' ℓ st out _ ih =>
    intro ho
    rcases ih (List.mem_cons_of_mem o' ho) with h | ⟨d, hd, hcl⟩
    · exact Or.inl h
    · exact Or.inr ⟨d, by simpa [declassifiersOf] using hd, hcl⟩
  | join m ℓ st out _ ih =>
    intro ho
    rcases ih (mem_join_right ho) with h | ⟨d, hd, hcl⟩
    · exact Or.inl h
    · exact Or.inr ⟨d, by simpa [declassifiersOf] using hd, hcl⟩
  | propagate ℓ st out _ ih =>
    intro ho
    rcases ih ho with h | ⟨d, hd, hcl⟩
    · exact Or.inl h
    · exact Or.inr ⟨d, by simpa [declassifiersOf] using hd, hcl⟩
  | declassify d ℓ st out _ _ ih =>
    intro ho
    by_cases hcl : Clears d o
    · exact Or.inr ⟨d, by simp [declassifiersOf], hcl⟩
    · rcases ih (applyD_preserves hcl ho) with h | ⟨d', hd', hcl'⟩
      · exact Or.inl h
      · exact Or.inr ⟨d', by simp [declassifiersOf]; exact Or.inr hd', hcl'⟩

/-! ## G9 proper -/

/-- **Untrusted data gains no authority (G9).** If a value carrying origin
`o` reaches a position that confers authority — a declared `Trusted[T]`
parameter, or a shell/exec/terminal/policy-scoped crossing under
`taint_strict` — and the checker admits it there, then an *explicit
declassification of that very origin* sits on the path. There is no
implicit escape: no join, no pure call, no cross-component relay
downgrades a label. -/
theorem no_authority_from_untrusted {P : Profile} {G : Grants}
    {ℓin : Label} {st : List Step} {ℓout : Label} (o : Origin) :
    Flow P G ℓin st ℓout → o ∈ ℓin → Admits Sink.authority ℓout →
    ∃ d ∈ declassifiersOf st, Clears d o := by
  intro hf ho hadm
  rcases origin_persists_or_is_declassified o hf ho with h | h
  · exact absurd hadm (authority_refuses_dirty h)
  · exact h

/-- The contrapositive, in the shape the checker refuses in: a path with
no declassifier on it cannot deliver a tainted value to an authority
sink. This is the G9 refusal. -/
theorem untrusted_gains_no_authority {P : Profile} {G : Grants}
    {ℓin : Label} {st : List Step} {ℓout : Label} (o : Origin) :
    Flow P G ℓin st ℓout → o ∈ ℓin → declassifiersOf st = [] →
    ¬ Admits Sink.authority ℓout := by
  intro hf ho hnd hadm
  obtain ⟨d, hd, _⟩ := no_authority_from_untrusted o hf ho hadm
  rw [hnd] at hd
  exact absurd hd (by simp)

/-- **Declassification is the only escape.** Along a path that performs no
declassification the label is monotone: it only grows. Every other step
the transfer function takes — minting, joining, an opaque call, a
cross-body relay — preserves what was already there. -/
theorem declassification_is_the_only_escape {P : Profile} {G : Grants}
    {ℓin : Label} {st : List Step} {ℓout : Label} :
    Flow P G ℓin st ℓout → declassifiersOf st = [] → LabelLe ℓin ℓout := by
  intro hf hnd o ho
  rcases origin_persists_or_is_declassified o hf ho with h | ⟨d, hd, _⟩
  · exact h
  · rw [hnd] at hd; exact absurd hd (by simp)

/-! ## Secret confinement (item 256) -/

/-- **A bound provider key survives every admitted path.** `secret` is the
one origin with no declassifier at all: `endorse[secret]` is refused
before the declared-slot check, and a checked parser applied to a
secret-carrying value is refused rather than laundering it. So the label
cannot be weakened anywhere. -/
theorem secret_persists {P : Profile} {G : Grants}
    {ℓin : Label} {st : List Step} {ℓout : Label} :
    Flow P G ℓin st ℓout → Origin.secret ∈ ℓin → Origin.secret ∈ ℓout := by
  intro hf
  induction hf with
  | nil ℓ => exact id
  | source o' ℓ st out _ ih => intro hs; exact ih (List.mem_cons_of_mem o' hs)
  | join m ℓ st out _ ih => intro hs; exact ih (mem_join_right hs)
  | propagate ℓ st out _ ih => exact ih
  | declassify d ℓ st out hok _ ih =>
    intro hs; exact ih (declassOK_secret_persists hok hs)

/-- **Secret confinement.** A capability-bound secret reaches *no* sink:
not an authority sink, not a disclosure sink (an emission crossing, an
extern host call, a provide-method return), not an unnameable callable —
and not even a position that declares `Secret[T]`, which admits a
`confidential` value but never a bound key (the A8 / CRITICAL 1
disjointness). The bound key is confined to its own capability's extern
bodies by construction. -/
theorem secret_confined {P : Profile} {G : Grants}
    {ℓin : Label} {st : List Step} {ℓout : Label} (k : Sink) :
    Flow P G ℓin st ℓout → Origin.secret ∈ ℓin → ¬ Admits k ℓout := by
  intro hf hs hadm
  have hout : Origin.secret ∈ ℓout := secret_persists hf hs
  cases k with
  | authority => exact hadm Origin.secret hout
  | disclosure => exact hadm.1 hout
  | secretReceiver => exact hadm hout
  | unnameable => exact hadm Origin.secret hout

/-- **`Secret[T]` confinement (item 256 Slice 3).** A `confidential` value
— a real, projectable `Secret[T]` value the language computes with —
reaches a disclosure sink (a log, an ordinary serialization, an LLM
prompt, an MCP tool return, a capability crossing whose receiver does not
declare `Secret[T]`) only through an explicit declassification of the
`confidential` origin itself. Unlike `secret`, this one *does* have a
downgrade edge, and this theorem says it is the only one. -/
theorem confidential_needs_declassification {P : Profile} {G : Grants}
    {ℓin : Label} {st : List Step} {ℓout : Label} :
    Flow P G ℓin st ℓout → Origin.confidential ∈ ℓin →
    Admits Sink.disclosure ℓout →
    ∃ d ∈ declassifiersOf st, Clears d Origin.confidential := by
  intro hf hc hadm
  rcases origin_persists_or_is_declassified Origin.confidential hf hc with h | h
  · exact absurd h hadm.2
  · exact h

/-! ## The untrusted author (item 249 Slice C / item 329) -/

/-- Every declassifier on an admitted path comes from the pre-granted
closure when the author is untrusted. -/
theorem flow_declassifiers_granted {P : Profile} {G : Grants}
    {ℓin : Label} {st : List Step} {ℓout : Label} :
    P.noDeclassify = true → Flow P G ℓin st ℓout →
    ∀ d ∈ declassifiersOf st, d.granted = true := by
  intro hp hf
  induction hf with
  | nil ℓ => intro d hd; simp [declassifiersOf] at hd
  | source o' ℓ st out _ ih => intro d hd; exact ih d (by simpa [declassifiersOf] using hd)
  | join m ℓ st out _ ih => intro d hd; exact ih d (by simpa [declassifiersOf] using hd)
  | propagate ℓ st out _ ih => intro d hd; exact ih d (by simpa [declassifiersOf] using hd)
  | declassify d' ℓ st out hok _ ih =>
    intro d hd
    rcases List.mem_cons.mp (by simpa [declassifiersOf] using hd) with rfl | h
    · exact hok.1 hp
    · exact ih d h

/-- **A self-minted declassifier does not count.** Under the
untrusted-author profile — a model-authored turn — an untrusted origin
reaches an authority sink only through a declassifier from the
*pre-granted closure*: a granted checked parser, never one the turn
declared itself. `admit_profile.check_no_declassify` refuses both doors
(any `endorse`, and a `verified fn` returning `Trusted[...]`) on the root
AST, so the whole taint discipline cannot be opted out of by the one
author item 329 refuses to trust. -/
theorem untrusted_author_needs_granted_declassifier {G : Grants}
    {ℓin : Label} {st : List Step} {ℓout : Label} (o : Origin) :
    Flow untrustedAuthor G ℓin st ℓout → o ∈ ℓin →
    Admits Sink.authority ℓout →
    ∃ d ∈ declassifiersOf st, d.granted = true ∧ Clears d o := by
  intro hf ho hadm
  obtain ⟨d, hd, hcl⟩ := no_authority_from_untrusted o hf ho hadm
  exact ⟨d, hd, flow_declassifiers_granted rfl hf d hd, hcl⟩

/-- **A declassification is never ambient.** A scoped `endorse[o]` is
admitted only where the enclosing declaration granted the slot, and
`endorse[secret]` nowhere at all — so a path cannot manufacture its own
downgrade. -/
theorem declassifier_must_be_declared {P : Profile} {G : Grants}
    {o : Origin} {g : Bool} {ℓ : Label} :
    (o ∉ G ∨ o = Origin.secret) → ¬ DeclassOK P G ⟨.endorse o, g⟩ ℓ := by
  rintro (hng | rfl)
  · exact endorse_needs_declared_slot hng
  · exact endorse_secret_refused

/-! ## The bridge to L0's declared context (G1/G6 composition) -/

/-- **The taint surface is bounded by the declared context.** Every origin
a statement's crossings can mint is one its declared requirements already
declare: `taint._origin_of` reads the origin off the crossing's declared
capability scope, and G6 bounds the reached keys by `Γ`. Origins are
derived, never guessed, and never ambient — the same composition
`CapCeilings.confinement_within_ceiling` performs for ceilings. -/
theorem taint_surface_within_declared_context {Γ : Ctx} {s : Stmt} :
    TypedIn Γ s → ∀ k ∈ stmtHeads s, originOfCap k ∈ contextOrigins Γ := by
  intro ht k hk
  exact originOfCap_mem (RevL.Lemmas.typedIn_confined ht k hk)

/-- **Untrusted data needs a declared untrusted source.** A component
whose declared requirements name no web/net/input-scoped capability
cannot mint an untrusted origin at all: there is nowhere for the data to
come from. G6 is what makes this true — reach is bounded by the declared
context, so an undeclared source is not merely refused, it is
unwritable. -/
theorem no_untrusted_without_a_declared_source {Γ : Ctx} {s : Stmt} :
    TypedIn Γ s → (∀ c ∈ Γ, ¬ IsUntrusted (originOfCap c)) →
    ∀ k ∈ stmtHeads s, ¬ IsUntrusted (originOfCap k) := by
  intro ht hΓ k hk
  exact hΓ k (RevL.Lemmas.typedIn_confined ht k hk)

/-! ## Non-vacuity

The hypotheses above admit real programs and refuse real programs. Each
clause below tracks a named test of the reference. -/

/-- `endorse[web](page, reason = "...")` declared on the enclosing fn. -/
def endorseWeb : Declassifier := ⟨.endorse Origin.web, true⟩

/-- The same downgrade, minted by the admitted source itself. -/
def endorseWebSelfMinted : Declassifier := ⟨.endorse Origin.web, false⟩

/-- `endorse[confidential](...)`, the §7c downgrade of a `Secret[T]` value. -/
def endorseConfidential : Declassifier := ⟨.endorse Origin.confidential, true⟩

/-- **G9 is not vacuous.** Four clauses over the same shape — a `web`
crossing whose value relays through a pure helper into a `Trusted[T]`
sink:

* the crossing really does mint `web` from its *declared scope*
  (`_origin_of` on `web.fetch`), and an unscoped one mints `input`;
* with a declared `endorse[web]` on the path the flow is admitted and the
  value arrives clean — `test_taint_provenance.py::
  test_endorse_declassifies_and_flows_clean`;
* without it the same path cannot reach the sink —
  `test_tainted_value_reaching_a_sink_is_refused_with_G9`;
* and a *foreign-origin* endorse launders nothing: `endorse[web]` over an
  `fs` value is still refused —
  `test_taint_endorse_laundering.py::
  test_cross_call_endorse_does_not_launder_a_foreign_origin`. -/
theorem g9_not_vacuous (P : Profile) :
    mintedBy ["web.fetch"] = Origin.web ∧ mintedBy [] = Origin.input ∧
    (Flow P [Origin.web] [Origin.web] [.propagate, .declassify endorseWeb] [] ∧
      Admits Sink.authority ([] : Label)) ∧
    (∀ ℓout, Flow P [Origin.web] [Origin.web] [.propagate] ℓout →
      ¬ Admits Sink.authority ℓout) ∧
    (∀ ℓout, Flow P [Origin.web] [Origin.fs] [.declassify endorseWeb] ℓout →
      ¬ Admits Sink.authority ℓout) := by
  refine ⟨by decide, rfl, ⟨?_, clean_nil⟩, ?_, ?_⟩
  · refine Flow.propagate _ _ _ (Flow.declassify endorseWeb _ _ _ ?_ ?_)
    · exact ⟨fun _ => rfl, by decide, by decide⟩
    · exact Flow.nil _
  · intro ℓout hf
    exact untrusted_gains_no_authority Origin.web hf (by simp) rfl
  · intro ℓout hf hadm
    obtain ⟨d, hd, hcl⟩ := no_authority_from_untrusted Origin.fs hf (by simp) hadm
    have : d = endorseWeb := by simpa [declassifiersOf] using hd
    subst this
    exact absurd hcl (by decide)

/-- **The `Secret` rules are not vacuous either.** Three clauses:

* a `Secret[T]` value *does* have a downgrade — a declared
  `endorse[confidential]` clears it and it reaches a disclosure sink
  (`test_secret_flow.py::
  test_endorse_confidential_downgrades_and_compiles_with_a_declared_slot`);
* a capability-bound `secret` has none: no path of any length, under any
  profile and any grants, delivers it to any sink
  (`test_a8_endorse_secret_stays_refused_unconditionally`, and the whole
  §4a refusal family);
* under the untrusted-author profile the *same* declared `endorse[web]`
  flips from admitted to refused purely because the turn minted it
  itself — `admit_profile.check_no_declassify` door 2. -/
theorem secret_rules_not_vacuous :
    (Flow untrustedAuthor [Origin.confidential] [Origin.confidential]
        [.declassify endorseConfidential] [] ∧
      Admits Sink.disclosure ([] : Label)) ∧
    (∀ (P : Profile) (G : Grants) (st : List Step) (ℓout : Label) (k : Sink),
      Flow P G [Origin.secret] st ℓout → ¬ Admits k ℓout) ∧
    (∀ ℓ, DeclassOK untrustedAuthor [Origin.web] endorseWeb ℓ) ∧
    (∀ ℓ, ¬ DeclassOK untrustedAuthor [Origin.web] endorseWebSelfMinted ℓ) := by
  refine ⟨⟨?_, ?_⟩, ?_, ?_, ?_⟩
  · refine Flow.declassify endorseConfidential _ _ _ ?_ ?_
    · exact ⟨fun _ => rfl, by decide, by decide⟩
    · exact Flow.nil _
  · exact ⟨by simp, by simp⟩
  · intro P G st ℓout k hf
    exact secret_confined k hf (by simp)
  · intro ℓ
    exact ⟨fun _ => rfl, by decide, by decide⟩
  · intro ℓ
    exact selfMinted_refused rfl rfl

/-! ### Anti-tautology guards (roadmap item 418)

Three properties a shape tautology could not have. -/

/-- **The refusal is not universal.** One and the same declassifier-free
flow — a `web` value relayed through a pure hop — is *admitted* at a
disclosure sink and *refused* at an authority sink. So
`no_authority_from_untrusted` is not the degenerate "nothing ever reaches
anything": its conclusion turns on which sink rule applies, which is
exactly the `Untrusted[T]`-into-`Trusted[T]` distinction G9 is about. (In
the reference: a `web`-tainted value crossing an emission is *recorded*
on the audit surface, not refused; the same value at a `Trusted[T]`
parameter is the G9 error.) -/
theorem authority_refusal_is_not_universal (P : Profile) (G : Grants) :
    Flow P G [Origin.web] [Step.propagate] [Origin.web] ∧
    declassifiersOf [Step.propagate] = [] ∧
    Admits Sink.disclosure ([Origin.web] : Label) ∧
    ¬ Admits Sink.authority ([Origin.web] : Label) := by
  refine ⟨Flow.propagate _ _ _ (Flow.nil _), rfl, ⟨by decide, by decide⟩, ?_⟩
  intro h
  exact h Origin.web (by decide)

/-- **The four sink rules are four different rules**, separated pairwise
by a witness each. This is the guard against the review's "G1 and G6 are
literally the same theorem" pattern: an authority sink refuses a
provenance origin a disclosure sink admits; a disclosure sink refuses a
`confidential` value a declared `Secret[T]` receiver admits; and a
`Secret[T]` receiver — the most permissive of the three — still refuses a
capability-bound key, which is the A8 disjointness. -/
theorem sink_rules_are_distinct :
    (¬ Admits Sink.authority ([Origin.web] : Label) ∧
      Admits Sink.disclosure ([Origin.web] : Label)) ∧
    (¬ Admits Sink.disclosure ([Origin.confidential] : Label) ∧
      Admits Sink.secretReceiver ([Origin.confidential] : Label)) ∧
    ¬ Admits Sink.secretReceiver ([Origin.secret] : Label) ∧
    ¬ Admits Sink.unnameable ([Origin.web] : Label) := by
  refine ⟨⟨?_, ⟨by decide, by decide⟩⟩,
          ⟨?_, (by decide : Origin.secret ∉ ([Origin.confidential] : Label))⟩, ?_, ?_⟩
  · intro h; exact h Origin.web (by decide)
  · intro h; exact h.2 (by decide)
  · intro h; exact h (by decide)
  · intro h; exact h Origin.web (by decide)

/-- **The `secret` refusals are load-bearing, not structural.** The label
algebra is perfectly capable of clearing a `secret`: `applyD` on the very
same `endorse[secret]` node computes the empty label, and the very same
declassifier forms do clear every other origin. What stops a bound
provider key is `taint.py`'s two explicit refusals — `endorse[secret]`
rejected before the declared-slot check (§4a.3), and a checked parser
rejected on a secret-carrying value rather than laundering it — both of
which sit in `kindOK`. Remove either and `secret_persists` is false.
This is the guard the review asks for: the theorem is not true because
the datatype has no constructor for the bad case. -/
theorem secret_refusal_is_load_bearing (P : Profile) (G : Grants) :
    applyD ⟨.endorse Origin.secret, true⟩ [Origin.secret] = ([] : Label) ∧
    applyD ⟨.parser, true⟩ [Origin.secret] = ([] : Label) ∧
    ¬ DeclassOK P G ⟨.endorse Origin.secret, true⟩ [Origin.secret] ∧
    ¬ DeclassOK P G ⟨.parser, true⟩ [Origin.secret] ∧
    DeclassOK P [Origin.web] ⟨.endorse Origin.web, true⟩ [Origin.web] ∧
    applyD ⟨.endorse Origin.web, true⟩ [Origin.web] = ([] : Label) := by
  refine ⟨by decide, rfl, endorse_secret_refused, ?_, ?_, by decide⟩
  · intro h; exact h.2 (by simp)
  · exact ⟨fun _ => rfl, by decide, by decide⟩


/-- A manifest whose only capability scope is `fs`. -/
def trustedCtx : Ctx := ["fs.write"]

/-- The same shape with a web scope declared. -/
def untrustedCtx : Ctx := ["web.fetch"]

/-- A declared crossing through the `fs` scope. -/
def fsCrossing : Stmt := .emit (.call "fs.write" [.lit "row"])

/-- **Non-vacuity for the two bridge theorems and the profile hypothesis**
(roadmap item 418, step 8). `taint_surface_within_declared_context` and
`no_untrusted_without_a_declared_source` are stated over `TypedIn Γ s`;
here is a `Γ` and an `s` satisfying it with a NON-EMPTY reach surface, so
neither is a claim about a statement that touches nothing. The
untrusted-source side condition is satisfiable AND refutable: it holds of
`fs.write` and fails of `web.fetch`, so it is a real side condition, and
`flow_declassifiers_granted`'s `noDeclassify` hypothesis is one a shipped
profile satisfies. -/
theorem g9_context_hypotheses_are_inhabited :
    untrustedAuthor.noDeclassify = true ∧
    TypedIn trustedCtx fsCrossing ∧
    stmtHeads fsCrossing = ["fs.write"] ∧
    (∀ c ∈ trustedCtx, ¬ IsUntrusted (originOfCap c)) ∧
    ¬ (∀ c ∈ untrustedCtx, ¬ IsUntrusted (originOfCap c)) ∧
    originOfCap "fs.write" ∈ contextOrigins trustedCtx := by
  have hty : TypedIn trustedCtx fsCrossing :=
    TypedIn.emit _ _
      (ReachIn.call _ "fs.write" _ (by decide) (by
        intro a ha
        simp only [List.mem_cons, List.not_mem_nil, or_false] at ha
        rcases ha with rfl
        exact ReachIn.lit _ _))
  refine ⟨rfl, hty, by simp [fsCrossing, stmtHeads, heads], ?_, ?_, by decide⟩
  · intro c hc
    simp only [trustedCtx, List.mem_cons, List.not_mem_nil, or_false] at hc
    subst hc
    intro hu
    unfold IsUntrusted at hu
    rw [show untrustedB (originOfCap "fs.write") = false from by decide] at hu
    exact Bool.noConfusion hu
  · intro h
    have hw : IsUntrusted (originOfCap "web.fetch") := by
      unfold IsUntrusted
      decide
    exact h "web.fetch" (by simp [untrustedCtx]) hw

end RevL.G9
