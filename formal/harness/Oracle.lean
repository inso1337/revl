import RevL.Manifest
import RevL.Lemmas.CapLemmas
import RevL.Theorems.CapCeilings
import RevL.Theorems.G5_ClassifiedTeardownPure
import RevL.Theorems.G8_ClassifiedBoundary
import RevL.Theorems.G7_LifoComplete
import RevL.Theorems.A8_WalDischarge
import RevL.Theorems.R4_NoResidue
import RevL.Theorems.A9_ProvideKeyDeclared
import RevL.Theorems.A2_NoAcquisitionAfterProvision
import RevL.Theorems.G4_DeferredPosition
import RevL.Theorems.G4_ApprovalFloor
import RevL.Theorems.G6_BindingUnique
import RevL.Theorems.G1_KeyAccess
import RevL.Theorems.A1_AsyncColour
import RevL.Theorems.Prelude_InterceptMethod
import RevL.Theorems.ModelPlacement
import RevL.Theorems.ModelCouncil
import RevL.Theorems.G9Flow
import RevL.Theorems.GRetain
import RevL.Theorems.G4Inverse

/-!
Formal oracle — the differential harness's Lean side (formal/STATUS.md,
"differential oracle"). Reads the corpus TSV the exporter wrote and emits
verdicts.

**Every verdict this file can state in the model is `decide`d from the
proved model itself**, not from a private restatement of it (roadmap item
418, C4 / step 6):

  * `V … disjoint=` is `decide (RevL.Manifest.ProvidesDisjoint comps)`;
  * `V … closed=` is `decide (RevL.Manifest.RequiresClosed comps)`;
  * `V … link=` is `linkOKB`, and `linkOKB_iff` below PROVES
    `linkOKB l = true ↔ RevL.Manifest.LinkOK l`;
  * `W … atten=` is `attenuatesB`, and `attenuatesB_iff` PROVES
    `attenuatesB H R = true ↔ RevL.CapCeilings.Attenuates H R`, whose
    resource half is the proved `RevL.Lemmas.Covers` over
    `stripCeilings` and whose ceiling half is the proved `budgetOf`
    development (`ceilingOKB_iff` discharges the unbounded `∀ k` through
    `RevL.Lemmas.budgetOf_attained`);
  * `A9 … a9=` is `a9RowB`, which IS `RevL.A9.a9B` over the component's
    `LComponent` beside its installed provide-block keys (the `PB` rows)
    and its routed keys (the `PR` rows); `a9RowB_iff` PROVES
    `a9RowB c blocks routed = true ↔ RevL.A9.A9OK ⟨c, blocks, routed⟩`,
    both directions of the rule (issues 1167 and #1172);
  * `AP … approval=` is `approvalRowB`, which IS
    `RevL.G4Approval.crossingB` over one marked crossing's tokens (the
    `AX` rows), its `with` edge (the `AE` row) and the file's
    approval-required tokens (the `AR` rows); `approvalRowB_iff` PROVES
    it equals `RevL.G4Approval.CrossingOK` (issue #1455);
  * `BU … binding=` is `bindingRowB`, which IS `RevL.G6Binding.bindingB`
    over one scope's seed names and its bind/enter/leave events (the `BE`
    rows); `bindingRowB_iff` PROVES it equals `RevL.G6Binding.BindingOK`
    (G6 binding uniqueness, issue #1812);
  * `G1 … access=` is `accessRowB`, which IS `RevL.G1Access.accessB` over
    the component's declared requirements (its `M` row) and its access
    roots (the `GA` rows); `accessRowB_iff` PROVES it equals
    `RevL.G1Access.AccessOK` (G1 declared access, issue #1807);
  * `A1 … async=` is `asyncRowB`, which IS `RevL.A1Async.siteB` over one
    site's kind and heads, the file's async names (the `AN` rows) and its
    `fn` call graph (the `FN` rows); `asyncRowB_iff` PROVES it equals
    `RevL.A1Async.SiteOK`, and `A1S … sig=` is `RevL.A1Async.sigB`
    (`sigRowB_iff`) over one provide method's two colours (issue #1808);
  * `PL … prelude=`, `IC … intercept=` and `MS … method=` are
    `RevL.Prelude.preludeB`, `interceptB` and `methodB` over the `PS`
    steps, the `IT` targets against the `M` row, and the `MC` operations
    against the file's `B` table; `preludeRowB_iff`, `interceptRowB_iff`
    and `methodRowB_iff` PROVE each is its rule (issue #1809);
  * `MPV … place=` is `placeRowB`, which IS `RevL.ModelPlace.placeB` over a
    component's placed route arms (`MP`) and the confidentiality origins
    (`MO`), with `placeRowB_iff`; `MAV … reach=` is `modelReachB`, which IS
    `attenuatesB` over the component's held set and a consulted role's
    reach (`ME`, `MRC`), with `modelReachB_iff` (G-MODEL-PLACE, issue
    #1811).

The components are `RevL.Manifest.LComponent` values built from the `M`
rows, so `slots`/`needs` — the `(key, realm)` slot the linker's
`provider_of` table is indexed by — are the model's, not a copy. Edit an
L0 definition and these verdicts move.

### What is still a private restatement, and why

Three groups of verdicts have no counterpart in the model, and are
computed here by hand. They are listed so the gate's reach is not
overstated:

1. `g4OK` and `hostAcquireOK` (the `G` row), and `configDataOK` (the `CD`
   row). Three rules under the G4
   guarantee. (The fourth, the DEFERRED-POSITION rule, is not a private
   restatement: the `DF` row decides `RevL.G4Deferred.deferredB`, issue
   #1742.) `g4OK` is the MARKER rule; the G4 model
   (`RevL.Theorems.G4_InverseOrEmit` over `RevL.Syntax.Stmt`) is indexed
   by statement syntax, and the export carries call FACTS — receiver,
   service, method, marker context — so no model definition takes its
   shape. `hostAcquireOK` (issue 334) is the ACQUIRE rule: a host acquire
   verb is legal only as an `effect … undo …` bracket's acquisition. Its
   verb table (`hostAcquireVerbs`) is the model's copy of the checker's
   `_HOST_ACQUIRE_VERBS`, and it decides over the `HA` position facts the
   export carries. `configDataOK` (issue 1161) is the CONFIG-IS-DATA rule
   (item 378): a config value is injected as static data, so its declared
   type must be built, transitively, out of data. It judges a
   DECLARATION rather than a body, so nothing in the crossing facts the
   other two read could express it — which is why it is a row of its own
   rather than a case in `g4OK`, and why a refusal of that class was
   reported as `missed-G4` wherever a fixture for it was placed until
   this row existed.
2. `methodBoundOK` (the `P` row, a provide method against its service's
   `emission[...]` declaration). The model has no definition of that
   rule at all; `RevL.Boundary.bodyBoundary` enumerates a body's crossing
   heads (G8) but never compares them to a declared bound.
3. `closeN` (the spawn-surface transitive closure feeding the `W` row).
   `RevL.CapCeilings.reachIn` IS that closure, but it is indexed by a
   `Comp` whose `body : List Stmt` the export cannot produce — the
   exporter resolves reach in Python and ships capabilities, not terms.
   The per-edge JUDGMENT the closure feeds is the model's `Attenuates`;
   only the closure that computes its argument is local.

A fourth row, `X … refused=`, is a verdict of RECORD, not a computed one:
revl's own parser refused the file, so there is no manifest to model. It
is carried so the file is counted and its refusal code is diffed rather
than dropped (item 418 step 7).

Fact rows in (tab-separated, one fact per line):
  Z <cap> <token>                            canonical cap decomposition
  Y <cap> <param> <path|discrete|ceiling> <value>   one cap parameter
  M <file> <comp> <reqs-csv> <provs-csv> <realms-csv> <member|template>
                                             component manifest; realms is
                                             `key=realm` pairs from the
                                             component's `isolate` clauses,
                                             and a `template` is a spawn
                                             target, excluded from the
                                             static composition exactly as
                                             `lower._link` excludes it
  R <file> <comp> <local> <svc>              require binding -> service
  B <file> <svc> <meth> <plain|any|scoped>   service-method emission bound
  Q <file> <svc> <meth> <entry>              a scoped bound's declared entry
  C <file> <comp> <key> <svc>                provide key -> service, read
                                             off the `provides` CLAUSE
  PB <file> <comp> <key>                     one installed provide BLOCK
                                             (`provide key { … }`), in body
                                             order — a double install is a
                                             repeated row. This is the fact
                                             the A9 row reads; C is not it
  PR <file> <comp> <key>                     one `isolate key in realms(...)`
                                             bind: the routes-installed key
                                             (issue #1172's exemption, as
                                             data). Its realm legs are NOT
                                             folded into M: the linker
                                             resolves them per leg, which
                                             `LComponent`'s one realm per
                                             key cannot express
  K <file> <comp> <local> <cap>              require-held capability, named
                                             by the DECLARED boundary
  A <file> <comp> <cap>                      activation emit-step surface,
                                             named by the DECLARED boundary
  F <file> <comp> <key> <svc> <meth> <cap> <bound>
                                             provide-method emission reach.
                                             TWO spellings of the same
                                             crossing: `cap` is the declared
                                             BOUNDARY (what the attenuation
                                             fold compares across a component
                                             edge) and `bound` is the WIRING
                                             KEY it went through (what the
                                             provide-method bound compares
                                             against its service's
                                             `emission[...]`); for a host
                                             emission it is the capability
                                             TOKEN the call reaches, a scoped
                                             extern's scope (issue #1455).
                                             `A` and `K` feed the fold alone
                                             and carry the boundary spelling
                                             only
  S <file> <comp> <child>                    activation spawn edge
  H <file> <comp> <var> <child>              spawn handle var
  U <file> <comp> <ctx> <root> <svc> <meth>  call fact + marker context:
                                             `emit` for the head call an emit
                                             marks, `emitarg` for a call
                                             evaluated inside that head's
                                             argument list (judged as a plain
                                             position: one marker covers one
                                             crossing, revl issue 1175),
                                             `emitnested` for the head of an
                                             `emit` expression written inside
                                             that argument list (refused
                                             outright), `plain` elsewhere
  HA <file> <comp> <verb> <bracket|plain|emit|undo|fn>
                                             a host-family acquisition and the
                                             POSITION that decides its legality
                                             — `bracket` is the acquisition of
                                             an `effect … undo …` (legal); any
                                             other site acquires irreversibly
                                             (G4, category `acquire`)
  BE <file> <comp> <scope> <ord> <seed|bind|enter|leave> <name|->
                                             one step through a binding scope
                                             (`@act` for the activation body,
                                             `<key>.<method>` for a provide
                                             method): a name in view at its
                                             start, a binding, or a block
                                             boundary (G6, category `binding`)
  AN <file> <name>                           an async name: an async extern,
                                             or an async service operation
                                             spelled `<Service>.<op>` (A1)
  AS <file> <comp> <ord> <kind> <heads=csv>  one A1 site: a provide method
                                             (`syncMethod`/`asyncMethod`), an
                                             `effect`/`emit` step (`…Await`
                                             when awaited), or an `undo` /
                                             `compensate` slot, with the heads
                                             it calls (issue #1808)
  AG <file> <comp> <key.method> <declared> <impl>
                                             a provide method's colour as its
                                             service declares it and as it is
                                             written (`async`/`sync`)
  MP <file> <comp> <action> <origin> <role> <on_device|off_device>
                                             one placed role of a `route
                                             model` arm (a council arm places
                                             each member that receives it)
  MO <file> <origin>                         a confidentiality origin
  ME <file> <comp> <role>                    a model role the component
                                             consults (a block names it, or a
                                             crossing is placed on it)
  MRC <file> <role> <cap>                    one capability the role reaches
                                             (`*` when it declares none)
  PS <file> <comp> <ord> <prelude|action>    one activation-body statement
                                             as the prelude rule sees it
  IT <file> <comp> <key>                     an `intercept` target
  MC <file> <comp> <svc> <meth>              a service operation the
                                             component names: a crossing
                                             through a requirement, a handle
                                             or an alias, or a provide-block
                                             implementation (issue #1809)
  GA <file> <comp> <root>                    one ACCESS root: a call head's
                                             root that is no binding, module
                                             callable, import, host family or
                                             constructor, or an `intercept`
                                             target (G1, issue #1807)
  DR <file> <fn|test|component> <owner> <extern> <call|arrow|value>
                                             one reach of a `deferred` emission
                                             extern and where it sits: a call,
                                             a call inside an arrow, or the
                                             extern as a function value (G4,
                                             category `deferred`, issue #1742)
  CF <file> <component|extern> <owner> <field> <type>
                                             a declared config field
  CN <file> <component|extern> <owner> <field> <ord> <form> <type>
                                             one node that field's declared
                                             type reaches, in walk order, with
                                             the SHIPPED config-data tables'
                                             classification of its head:
                                             scalar / container / record /
                                             variant / struct / tparam are
                                             data; arrow / service / erased /
                                             opaque are not
  I <file> <comp> <index> <pure|effect|emit|raw> <heads-csv> <inverse-csv>
                                              reconstructed RevL.Syntax.Stmt
  T <file> <comp> <kind>                     statement class (census)
  X <file> <code>                            revl refused the file at parse
  N <file>                                   parsed, no component
  E <scen> <ord> <kind> <seam>               one teardown-stack entry (G7),
                                             in registration order; `kind` is
                                             the model's `EntryKind` and
                                             `seam` is the reference's
                                             registration site, ignored here
  J <scen> <verdict>                         the verdict that teardown ran
                                             under (commit|abort|halted)
  L <scen> run                               declares one crash-recovery
                                             scenario (A8/R4)
  L <scen> descriptor <seq> <entry> <idem>   a durable WAL record, in append
  L <scen> effect <seq> <b> <rc> <idem>      order; the constructors of
  L <scen> discharge <seqs-csv>              `RevL.Lemmas.Rec`, one row each
  L <scen> fence <seq>
  L <scen> deferred <seq>
  L <scen> marker <approved|aborted|forkfrozen|complete>
  L <scen> fails <seq>                       the re-issue ORACLE (243 rule 6):
                                             this seq's inverse fails when
                                             re-issued. Not a WAL record — a
                                             property of the world the
                                             reference drives.
  AR <file> <token>                          an approval-required capability
                                             token: an extern declares
                                             `requires approval`, and this is
                                             its scope, or its name
  AX <file> <comp> <ord> <token>             one token the component's
                                             marked crossing `ord` reaches
  AE <file> <comp> <ord> <scope>             that crossing's `with` edge; no
                                             row when it carries none
  AQ <file> <comp> <ord> <acquire|provide|other>
                                             one activation-body statement,
                                             at body index `ord`, as the A2
                                             rule sees it (issue 1166):
                                             `acquire` is a `let … = effect`,
                                             a bare `effect`, a timer or an
                                             `every … in` iteration — the four
                                             forms `lower._dispatch_action`
                                             refuses after a provision

Capabilities arrive DECOMPOSED (Z/Y), from `src/revl/cap_order.parse_cap`
— the checker's own parser. Nothing here re-reads the capability grammar.

Verdict rows out:
  V <file> <disjoint=ok|fail> <closed=ok|fail> <link=ok|fail>
  G <file> <comp> <g4=ok|fail>                     marker rule (incl. handle)
                                                   AND host-acquisition rule
  P <file> <comp> <key> <svc> <meth> <bound=ok|fail>
  W <file> <comp> <child> <atten=ok|fail>          spawn attenuation
  CD <file> <component|extern> <owner> <field> <data=ok|fail>
                                                   config-is-data rule
  X <file> <refused=CODE>                          refusal of record
  A9 <file> <comp> <a9=ok|fail>                    every installed block key
                                                   is declared AND every
                                                   declared key is installed
                                                   by a block or a route
                                                   (RevL.A9); one row per
                                                   component that declares
                                                   or installs anything
  D <scen> <replayed=csv> <discharged=csv> <stranded=csv>   G7 disposition
  O <scen> <outcome=...> <replayed=csv> <residue=csv|n/a>   A8/R4 recovery
  A2 <file> <comp> <a2=ok|fail>                    no acquisition after a
                                                   provision: `RevL.A2.a2B`
                                                   over the body's `AQ` steps
  DF <file> <deferred=ok|fail>                     G4 deferred position:
                                                   `RevL.G4Deferred.deferredB`
                                                   over the file's `DR` reaches
  AP <file> <comp> <ord> <approval=ok|fail>        the approval floor over one
                                                   marked crossing:
                                                   `RevL.G4Approval.crossingB`
  BU <file> <comp> <scope> <binding=ok|fail>       G6 binding uniqueness over
                                                   one scope:
                                                   `RevL.G6Binding.bindingB`
  G1 <file> <comp> <access=ok|fail>                G1 declared access:
                                                   `RevL.G1Access.accessB`
                                                   over the `GA` roots
  A1 <file> <comp> <ord> <async=ok|fail>           A1 async colour over one
                                                   site: `RevL.A1Async.siteB`
  A1S <file> <comp> <key.method> <sig=ok|fail>     A1, a provide method's
                                                   colour is its service's:
                                                   `RevL.A1Async.sigB`
  PL <file> <comp> <prelude=ok|fail>               prelude ordering:
                                                   `RevL.Prelude.preludeB`
  IC <file> <comp> <intercept=ok|fail>             intercept target:
                                                   `RevL.Prelude.interceptB`
  MS <file> <comp> <method=ok|fail>                method in service:
                                                   `RevL.Prelude.methodB`
  MPV <file> <comp> <place=ok|fail>                model placement:
                                                   `RevL.ModelPlace.placeB`
  MAV <file> <comp> <role> <reach=ok|fail>         model reach within the
                                                   component's held set:
                                                   `attenuatesB`

### The one row whose reference side RUNS rather than reads

`D` is not like the rows above. `V`/`G`/`P`/`W` decide a judgment about a
manifest, and both sides compute it. A teardown disposition is a property
of a RUN, so the `D` row's reference half executes
`backends/python/runtime.py` over the scenario's stack and reports what
actually happened (`diff_corpus.teardown_observation`), while this file
computes what `RevL.Semantics` says should happen. Nothing below restates
the rule: the three columns are the model's own `replayed` / `discharged`
/ `stranded`, and the only local content is reading each entry's label
back out of the `inverse` slot the corpus put it in.
-/

namespace RevLOracle

open RevL.Manifest
open RevL.Lemmas
open RevL.CapCeilings (ResourceOK CeilingOK Attenuates)
open RevL.Syntax (Expr)
open RevL.Semantics (EntryKind Verdict LogEntry teardown discharged stranded)

def splitKeys (s : String) : List String :=
  if s == "" then [] else (s.splitOn ",").filter (fun k => k != "")

/-- A verdict column: the labels, comma-joined, empty for an empty column
(`splitKeys` reads it back). -/
def csv (xs : List String) : String := String.intercalate "," xs

def union (x y : List String) : List String := (x ++ y).eraseDups

/-! ## Deciding the manifest model

`ProvidesDisjoint` and `RequiresClosed` are `def`s returning `Prop`, so
instance search will not see through them on its own; unfolding them is
all it takes, and the Bool the oracle prints is then literally
`decide` of the model's proposition. -/

instance instDecProvidesDisjoint (cs : List LComponent) :
    Decidable (ProvidesDisjoint cs) := by
  unfold ProvidesDisjoint; infer_instance

instance instDecRequiresClosed (cs : List LComponent) :
    Decidable (RequiresClosed cs) := by
  unfold RequiresClosed; infer_instance

/-- The `LinkOK` judgment as a decision procedure. Each conjunct is
`decide`d from the corresponding side condition of `LinkOK.cons`, so the
only content here is the recursion — `linkOKB_iff` proves it is faithful. -/
def linkOKB : List LComponent → Bool
  | [] => true
  | c :: cs =>
      decide (List.Nodup (slots c)) &&
      decide (∀ s ∈ slots c, s ∉ cs.flatMap slots) &&
      decide (∀ s ∈ needs c, s ∈ cs.flatMap slots) &&
      linkOKB cs

/-- **The link verdict is the model's judgment.** -/
theorem linkOKB_iff : ∀ l : List LComponent, linkOKB l = true ↔ LinkOK l := by
  intro l
  induction l with
  | nil => exact ⟨fun _ => LinkOK.nil, fun _ => rfl⟩
  | cons c cs ih =>
    constructor
    · intro h
      simp only [linkOKB, Bool.and_eq_true, decide_eq_true_eq] at h
      obtain ⟨⟨⟨h1, h2⟩, h3⟩, h4⟩ := h
      exact LinkOK.cons c cs h1 h2 h3 (ih.mp h4)
    · intro h
      cases h with
      | cons _ _ h1 h2 h3 h4 =>
        simp only [linkOKB, Bool.and_eq_true, decide_eq_true_eq]
        exact ⟨⟨⟨h1, h2⟩, h3⟩, ih.mpr h4⟩

/-- Pick a component whose consumed slots are all already provided — the
linker's Kahn step. Returns it with the rest of the queue. -/
def pick (provided : List Slot) : List LComponent →
    Option (LComponent × List LComponent)
  | [] => none
  | c :: cs =>
      if (needs c).all (fun s => provided.contains s) then some (c, cs)
      else match pick provided cs with
        | some (d, rest) => some (d, c :: rest)
        | none => none

/-- The admission search. `acc` is the composition in `LinkOK` order (its
head is admitted LAST), so each newly admitted component is prepended. -/
def kahn : Nat → List Slot → List LComponent → List LComponent →
    Option (List LComponent)
  | 0, _, acc, rem => if rem.isEmpty then some acc else none
  | fuel + 1, provided, acc, rem =>
      if rem.isEmpty then some acc
      else match pick provided rem with
        | none => none
        | some (c, rest) => kahn fuel (provided ++ slots c) (c :: acc) rest

/-- A requirement no component in THIS FILE provides is not a dangling
edge — the linker resolves it against the whole composition and simply
adds no edge (`lower._link`: `provider = provider_of.get(...)`, and a
`None` provider contributes nothing). The local composition therefore
elides those requirements before the judgment is applied. Elided, not
supplied by a fabricated provider: nothing is added to the provision
surface, so disjointness is untouched, and a component that requires a
key it provides ITSELF keeps that requirement and is refused, which is
the linker's G3 self-provision refusal. -/
def localComposition (comps : List LComponent) : List LComponent :=
  let provided := comps.flatMap slots
  comps.map fun c =>
    { c with requires := c.requires.filter (fun k => provided.contains (k, c.realm k)) }

/-- `link=ok` iff the search found an admission order AND `linkOKB`
certified it. A `fail` is a failure to find one; for this judgment the
greedy pass is complete (admitting an eligible component never blocks
another: needs only become more satisfied, and a slot collision is
order-independent), so the two coincide. -/
def linkVerdict (comps : List LComponent) : Bool :=
  let l := localComposition comps
  match kahn l.length [] [] l with
  | none => false
  | some ord => linkOKB ord

/-! ## Deciding A9

The installed block keys and the routed keys live beside the `LComponent`
(`RevL.A9.Installed`, additively — L0 carries the clause only), and the
verdict is the L2 file's own `a9B`, both directions. Nothing is restated
here; the bridge is the model's `a9B_iff` applied to the row's shape. -/

def a9RowB (c : LComponent) (blocks routed : List String) : Bool :=
  RevL.A9.a9B ⟨c, blocks, routed⟩

/-- **The A9 verdict is the model's judgment.** -/
theorem a9RowB_iff (c : LComponent) (blocks routed : List String) :
    a9RowB c blocks routed = true ↔ RevL.A9.A9OK ⟨c, blocks, routed⟩ :=
  RevL.A9.a9B_iff ⟨c, blocks, routed⟩

/-! ## Deciding the capability order

The capability rows arrive already decomposed by `cap_order.parse_cap`,
so `Cap`/`PVal` are built directly and `Covers` is `decide`d through
`coversB`. -/

/-- `PathLe` as a decision procedure: `wide` is a component-wise prefix
of `narrow`. -/
def pathPrefixB : List String → List String → Bool
  | [], _ => true
  | _ :: _, [] => false
  | w :: ws, n :: ns => (w == n) && pathPrefixB ws ns

theorem pathPrefixB_iff : ∀ (w n : List String),
    pathPrefixB w n = true ↔ PathLe n w := by
  intro w
  induction w with
  | nil => intro n; exact ⟨fun _ => ⟨n, rfl⟩, fun _ => rfl⟩
  | cons x ws ih =>
    intro n
    cases n with
    | nil =>
      constructor
      · intro h; simp [pathPrefixB] at h
      · rintro ⟨r, hr⟩; simp at hr
    | cons y ns =>
      simp only [pathPrefixB, Bool.and_eq_true, beq_iff_eq]
      constructor
      · rintro ⟨rfl, h⟩
        obtain ⟨r, hr⟩ := (ih ns).mp h
        exact ⟨r, by simp [hr]⟩
      · rintro ⟨r, hr⟩
        simp only [List.cons_append, List.cons.injEq] at hr
        exact ⟨hr.1.symm, (ih ns).mpr ⟨r, hr.2⟩⟩

/-- `PLeq` as a decision procedure. -/
def pleqB : PVal → PVal → Bool
  | .path n, .path w => pathPrefixB w n
  | .discrete a, .discrete b => a == b
  | .ceiling m, .ceiling n => decide (m ≤ n)
  | _, _ => false

theorem pleqB_iff : ∀ a b : PVal, pleqB a b = true ↔ PLeq a b := by
  intro a b
  cases a with
  | path n =>
    cases b with
    | path w =>
      exact ⟨fun h => .path _ _ ((pathPrefixB_iff w n).mp h),
             fun h => by cases h with
               | path _ _ hp => exact (pathPrefixB_iff w n).mpr hp⟩
    | discrete _ => exact ⟨fun h => absurd h (by simp [pleqB]), fun h => by cases h⟩
    | ceiling _ => exact ⟨fun h => absurd h (by simp [pleqB]), fun h => by cases h⟩
  | discrete s =>
    cases b with
    | path _ => exact ⟨fun h => absurd h (by simp [pleqB]), fun h => by cases h⟩
    | discrete t =>
      constructor
      · intro h
        have : s = t := by simpa [pleqB] using h
        subst this; exact .discrete _
      · intro h; cases h; simp [pleqB]
    | ceiling _ => exact ⟨fun h => absurd h (by simp [pleqB]), fun h => by cases h⟩
  | ceiling m =>
    cases b with
    | path _ => exact ⟨fun h => absurd h (by simp [pleqB]), fun h => by cases h⟩
    | discrete _ => exact ⟨fun h => absurd h (by simp [pleqB]), fun h => by cases h⟩
    | ceiling n =>
      constructor
      · intro h; exact .ceiling _ _ (by simpa [pleqB] using h)
      · intro h
        cases h with
        | ceiling _ _ hn => simpa [pleqB] using hn

theorem mem_keys_of_lookupV : ∀ (P : Valuation) (k : String) (v : PVal),
    lookupV P k = some v → k ∈ P.map (·.1) := by
  intro P
  induction P with
  | nil => intro k v h; simp [lookupV] at h
  | cons kv rest ih =>
    intro k v h
    simp only [lookupV] at h
    by_cases hk : kv.1 = k
    · simp [hk]
    · rw [if_neg hk] at h
      exact List.mem_cons_of_mem _ (ih k v h)

theorem lookupV_isSome_of_mem_keys : ∀ (P : Valuation) (k : String),
    k ∈ P.map (·.1) → ∃ v, lookupV P k = some v := by
  intro P
  induction P with
  | nil => intro k h; simp at h
  | cons kv rest ih =>
    intro k h
    simp only [lookupV]
    by_cases hk : kv.1 = k
    · rw [if_pos hk]; exact ⟨kv.2, rfl⟩
    · rw [if_neg hk]
      simp only [List.map_cons, List.mem_cons] at h
      rcases h with h | h
      · exact absurd h.symm hk
      · exact ih k h

/-- `Covers` as a decision procedure. It quantifies over the parameter
NAMES `a` binds, which is exactly the set on which `Covers`'s hypothesis
`lookupV a.params k = some v` can hold, so the equivalence is
unconditional (duplicate names included — both sides read `lookupV`). -/
def coversB (a b : Cap) : Bool :=
  a.token == b.token &&
  (a.params.map (·.1)).all fun k =>
    match lookupV a.params k, lookupV b.params k with
    | some v, some w => pleqB w v
    | some _, none => false
    | none, _ => true

/-- **The coverage verdict is the model's relation.** -/
theorem coversB_iff (a b : Cap) : coversB a b = true ↔ Covers a b := by
  simp only [coversB, Bool.and_eq_true, beq_iff_eq, List.all_eq_true]
  constructor
  · rintro ⟨ht, hall⟩
    refine ⟨ht, fun k v hv => ?_⟩
    have hk := mem_keys_of_lookupV a.params k v hv
    have := hall k hk
    rw [hv] at this
    cases hb : lookupV b.params k with
    | none => rw [hb] at this; exact absurd this (by simp)
    | some w =>
      rw [hb] at this
      exact ⟨w, rfl, (pleqB_iff w v).mp this⟩
  · rintro ⟨ht, hcov⟩
    refine ⟨ht, fun k hk => ?_⟩
    obtain ⟨v, hv⟩ := lookupV_isSome_of_mem_keys a.params k hk
    obtain ⟨w, hw, hwv⟩ := hcov k v hv
    rw [hv, hw]
    exact (pleqB_iff w v).mpr hwv

/-! ## Deciding the spawn-attenuation judgment

`RevL.CapCeilings.Attenuates` is the checker's spawn gate in the model:
the resource fold over ceiling-stripped capabilities, AND the ceiling
budget check. Both halves are decided here against the model's own
definitions. -/

def resourceOKB (held reach : List Cap) : Bool :=
  reach.all fun c => held.any fun h => coversB (stripCeilings h) (stripCeilings c)

theorem resourceOKB_iff (held reach : List Cap) :
    resourceOKB held reach = true ↔ ResourceOK held reach := by
  simp only [resourceOKB, List.all_eq_true, List.any_eq_true, ResourceOK]
  constructor
  · intro h c hc
    obtain ⟨x, hx, hxc⟩ := h c hc
    exact ⟨x, hx, (coversB_iff _ _).mp hxc⟩
  · intro h c hc
    obtain ⟨x, hx, hxc⟩ := h c hc
    exact ⟨x, hx, (coversB_iff _ _).mpr hxc⟩

/-- The parameter names any held capability binds. `CeilingOK` quantifies
over ALL names; `budgetOf_attained` says a `some` budget is some held
capability's own declaration, so no name outside this list can carry
one — which is what makes the unbounded quantifier decidable. -/
def heldParamKeys (held : List Cap) : List String :=
  held.flatMap (fun h => h.params.map (·.1))

def ceilingOKB (held reach : List Cap) : Bool :=
  reach.all fun c =>
    (heldParamKeys held).all fun k =>
      match budgetOf held c.token k with
      | none => true
      | some n =>
          match ceilingOf c k with
          | some m => decide (m ≤ n)
          | none => false

theorem ceilingOKB_iff (held reach : List Cap) :
    ceilingOKB held reach = true ↔ CeilingOK held reach := by
  simp only [ceilingOKB, List.all_eq_true, CeilingOK]
  constructor
  · intro h c hc k n hn
    obtain ⟨x, hx, hxt, hxc⟩ := budgetOf_attained hn
    have hk : k ∈ heldParamKeys held := by
      refine List.mem_flatMap.mpr ⟨x, hx, ?_⟩
      exact mem_keys_of_lookupV x.params k _ (ceilingOf_lookup hxc)
    have := h c hc k hk
    rw [hn] at this
    cases hm : ceilingOf c k with
    | none => rw [hm] at this; exact absurd this (by simp)
    | some m => rw [hm] at this; exact ⟨m, rfl, of_decide_eq_true this⟩
  · intro h c hc k _
    cases hn : budgetOf held c.token k with
    | none => rfl
    | some n =>
      obtain ⟨m, hm, hmn⟩ := h c hc k n hn
      rw [hm]
      exact decide_eq_true hmn

def attenuatesB (held reach : List Cap) : Bool :=
  resourceOKB held reach && ceilingOKB held reach

/-- **The spawn-attenuation verdict is the model's judgment.** -/
theorem attenuatesB_iff (held reach : List Cap) :
    attenuatesB held reach = true ↔ Attenuates held reach := by
  simp only [attenuatesB, Bool.and_eq_true, Attenuates]
  exact and_congr (resourceOKB_iff held reach) (ceilingOKB_iff held reach)

/-! ## Deciding the teardown disposition (G7)

The rows above are STATIC: they read a manifest and decide a judgment about
it. This one is not. `RevL.Semantics.replayed` / `discharged` / `stranded`
are about what a teardown DOES — which entries of the per-activation LIFO
stack run their inverse, which are dropped, and which are left owed — so
the fact rows it consumes are the shape of one activation's stack and the
verdict it tore down under, and the reference side does not recompute
them: it RUNS `backends/python/runtime.py` over that stack and reports what
actually happened (see `diff_corpus.teardown_observation`).

So the diff here is the model's predicted disposition against the
reference runtime's observed one. Nothing below restates the rule: the
three columns are the model's own three functions, and the only local
content is reading the entry's label back out of the `inverse` slot the
corpus put it in. -/

section G7Disposition

open RevL.Semantics

/-- The corpus carries each stack entry's LABEL in the model's own
`inverse` slot, as a literal. `teardown` maps `LogEntry.inverse` over the
replay list, so a label put here is transported by the model's own
function and read back unchanged — which is why the printed column is the
model's answer and not a parallel bookkeeping list. -/
def labelOf : Expr → String
  | .lit s => s
  | .call f _ => f

def parseKind : String → Option EntryKind
  | "bracket" => some .bracket
  | "transactional" => some .transactional
  | "compensation" => some .compensation
  | _ => none

def parseVerdict : String → Option Verdict
  | "commit" => some .commit
  | "abort" => some .abort
  | "halted" => some .halted
  | _ => none

/-- The entries whose inverse RUNS, in the order they run: the model's
`teardown`, labelled. Order is load-bearing — this column is where G7's
LIFO claim and the Phase-1-before-Phase-2 split are checked against the
reference. -/
def replayedLabels (v : Verdict) (log : List LogEntry) : List String :=
  (teardown v log).map labelOf

/-- The entries DROPPED without running (`RevL.Semantics.discharged`). -/
def dischargedLabels (v : Verdict) (log : List LogEntry) : List String :=
  (discharged v log).map (fun e => labelOf e.inverse)

/-- The entries left OWED (`RevL.Semantics.stranded`, item 443). Empty
under every settling verdict; the whole stack under the E-Stop. -/
def strandedLabels (v : Verdict) (log : List LogEntry) : List String :=
  (stranded v log).map (fun e => labelOf e.inverse)

private theorem mem_labels_filter (p : LogEntry → Bool) (log : List LogEntry)
    (s : String) :
    s ∈ (log.filter p).map (fun e => labelOf e.inverse)
      ↔ ∃ e ∈ log, p e = true ∧ labelOf e.inverse = s := by
  simp only [List.mem_map, List.mem_filter]
  constructor
  · rintro ⟨e, ⟨he, hp⟩, hs⟩; exact ⟨e, he, hp, hs⟩
  · rintro ⟨e, he, hp, hs⟩; exact ⟨e, ⟨he, hp⟩, hs⟩

/-- **The replayed column is the model's replay set.** Left to right it is
G7 soundness (`replayed_sound`): a label the oracle prints belongs to a
registered entry this verdict really replays. Right to left it is G7
completeness (`replayed_complete`): every such entry's label is printed.
So a runtime that drops an inverse, or runs one it should have discharged,
disagrees with this column. -/
theorem mem_replayedLabels_iff (v : Verdict) (log : List LogEntry) (s : String) :
    s ∈ replayedLabels v log
      ↔ ∃ e ∈ log, e.kind.replaysUnder v = true ∧ labelOf e.inverse = s := by
  simp only [replayedLabels, RevL.Semantics.teardown, List.map_map,
    Function.comp_def, List.mem_map]
  constructor
  · rintro ⟨e, he, hs⟩
    exact ⟨e, (RevL.G7.replayed_sound v log e he).1,
      (RevL.G7.replayed_sound v log e he).2, hs⟩
  · rintro ⟨e, he, hr, hs⟩
    exact ⟨e, RevL.G7.replayed_complete v log e he hr, hs⟩

/-- **The discharged column is the model's discharge set** — the entries
`EntryKind.dischargedUnder` drops without running. -/
theorem mem_dischargedLabels_iff (v : Verdict) (log : List LogEntry) (s : String) :
    s ∈ dischargedLabels v log
      ↔ ∃ e ∈ log, e.kind.dischargedUnder v = true ∧ labelOf e.inverse = s :=
  mem_labels_filter _ log s

/-- **The stranded column is the model's stranded inventory** (item 443) —
neither replayed nor discharged, still owed to whoever reconciles. -/
theorem mem_strandedLabels_iff (v : Verdict) (log : List LogEntry) (s : String) :
    s ∈ strandedLabels v log
      ↔ ∃ e ∈ log, e.kind.strandedUnder v = true ∧ labelOf e.inverse = s :=
  mem_labels_filter _ log s

/-- **The row accounts for the whole stack.** Replayed + discharged +
stranded is exactly the number of entries registered, under every verdict
(`RevL.Semantics.book_lengths_add`). An entry cannot fall off this row by
appearing in no column, so a `D` row that agrees column-by-column has
agreed about every entry — the vacuity a partial row would allow is closed
here rather than assumed. -/
theorem row_is_total (v : Verdict) (log : List LogEntry) :
    (replayedLabels v log).length + (dischargedLabels v log).length
      + (strandedLabels v log).length = log.length := by
  simp only [replayedLabels, dischargedLabels, strandedLabels,
    RevL.Semantics.teardown, List.length_map]
  exact RevL.Semantics.book_lengths_add v log

/-- **The printed order is the two-phase walk.** The replayed column is
the whole Phase-1 proof pass, LIFO over registration order, and only then
the Phase-2 compensation drain, LIFO within itself. This is what makes the
column's ORDER checkable: a reference that ran a compensation inline
during Phase 1, or unwound a phase FIFO, produces a different list. -/
theorem replayedLabels_phase_order (v : Verdict) (log : List LogEntry) :
    replayedLabels v log =
      ((log.filter (fun e => e.kind.inPhase1 && e.kind.replaysUnder v)).reverse.map
        (fun e => labelOf e.inverse))
      ++ ((log.filter (fun e => e.kind.inPhase2 && e.kind.replaysUnder v)).reverse.map
        (fun e => labelOf e.inverse)) := by
  simp only [replayedLabels, RevL.Semantics.teardown, RevL.Semantics.replayed,
    RevL.Semantics.phase1, RevL.Semantics.phase2, List.map_append, List.map_map,
    Function.comp_def]

end G7Disposition

/-! ## Deciding the crash-recovery disposition (A8, R4)

The `D` row above is about a teardown that RUNS. This one is about what a
FRESH PROCESS concludes from a durable log after the old one died — the
guarantee A8 states (`RevL.Theorems.A8_WalDischarge`) and the residue
surface R4 states (`RevL.Theorems.R4_NoResidue`). Until this row existed
both were proved only against the design documents: `formal/STATUS.md`
carried 17 A8 theorems and 8 R4 theorems and no oracle row, so the model
was checked for internal consistency and never against `src/revl`.

Like `D`, the reference half RUNS rather than reads: it writes the
scenario's records as a real JSON-Lines WAL and calls
`src/revl/recovery.py`'s `recover` over it, observing what was actually
applied to the `World` and what the report names as residue
(`diff_corpus.recovery_observation`). This file computes what
`RevL.Lemmas.WalLemmas` says should happen. Nothing below restates the
rule: the three columns are the model's own `outcome` / `replayed` /
`reported`.

Three things about the columns, stated so the row is not read as wider
than it is:

* `replayed` is compared as a SET (both sides sort it). The model's
  `rollbackReplay` walks the log in append order; `_roll_back` walks the
  legacy `effect` family newest-first and the descriptor families by
  descending seq. Neither order is claimed by the other, so ordering them
  would be comparing two bookkeeping conventions, not a guarantee. The
  LIFO claim that IS a guarantee is G7's, and the `D` row checks it
  ordered.
* `residue` is printed only under `outcome = rolledBack`. That is the
  model's OWN scope condition — `RevL.Lemmas.reported` models the
  roll-back path's surface, and R4 is stated under
  `outcome L = .rolledBack`. The roll-forward window's `flush-residue` is
  a different surface the model does not have, so the column says `n/a`
  there rather than agreeing about a claim neither side is making.
* the re-issue oracle `ok` (243 rule 6: the inverse is fallible) is a
  parameter of the model, so it is a fact in the corpus (`fails` rows)
  rather than fixed to `okAll` here. That is what puts
  `Disp.residue .restoreFailed` under the row at all.
-/

section Recovery

open RevL.Lemmas

def parseBoolField : String → Option Bool
  | "0" => some false
  | "1" => some true
  | _ => none

def parseEntry : String → Option Entry
  | "transactional" => some .transactional
  | "compensation" => some .compensation
  | _ => none

/-- One durable WAL record of one recovery scenario, as the model's own
`Rec`. A malformed row is `none` and the caller refuses the corpus — never
a silently dropped record, which would move the verdict. -/
def parseL (f : List String) : Option (String × Rec) :=
  match f with
  | ["L", scen, "descriptor", seq, entry, idem] => do
      let s ← seq.toNat?
      let e ← parseEntry entry
      let i ← parseBoolField idem
      some (scen, .descriptor s e i)
  | ["L", scen, "effect", seq, b, rc, idem] => do
      let s ← seq.toNat?
      let bb ← parseBoolField b
      let rr ← parseBoolField rc
      let ii ← parseBoolField idem
      some (scen, .effect s bb rr ii)
  | ["L", scen, "discharge", seqs] => do
      let ss ← (splitKeys seqs).mapM (·.toNat?)
      some (scen, .discharge ss)
  | ["L", scen, "fence", seq] => do
      let s ← seq.toNat?
      some (scen, .replayFence s)
  | ["L", scen, "deferred", seq] => do
      let s ← seq.toNat?
      some (scen, .deferredEmission s)
  | ["L", scen, "marker", "approved"] => some (scen, .commitApproved)
  | ["L", scen, "marker", "aborted"] => some (scen, .aborted)
  | ["L", scen, "marker", "forkfrozen"] => some (scen, .forkFrozen)
  | ["L", scen, "marker", "complete"] => some (scen, .activationComplete)
  | _ => none

/-- `L <scen> run` — a scenario exists, so an `O` row is owed for it. -/
def parseLRun (f : List String) : Option String :=
  match f with
  | ["L", scen, "run"] => some scen
  | _ => none

/-- `L <scen> fails <seq>` — the re-issue oracle's `false` points. -/
def parseLFail (f : List String) : Option (String × Seq) :=
  match f with
  | ["L", scen, "fails", seq] => (seq.toNat?).map fun s => (scen, s)
  | _ => none

/-- A row of the corpus that is an `L` row of SOME shape. Used to refuse a
malformed one instead of skipping it. -/
def isLRow (f : List String) : Bool :=
  match f with
  | "L" :: _ => true
  | _ => false

/-- The re-issue oracle 243 rule 6 makes fallible, built from the corpus's
`fails` rows. `RevL.R4.okAll` is the special case with no such row. -/
def okOf (failing : List Seq) : Seq → Bool := fun s => !memSeq s failing

/-- The printed name of the model's `Outcome`. -/
def outcomeName : Outcome → String
  | .rolledForward => "rolledForward"
  | .rolledBack => "rolledBack"
  | .forkRetired => "forkRetired"

/-- **The printed name IS the outcome.** Distinct outcomes print
differently, so comparing the strings compares the model's verdicts and a
reference that converged on a different one disagrees with this column. -/
theorem outcomeName_inj : ∀ a b : Outcome, outcomeName a = outcomeName b → a = b := by
  intro a b h
  cases a <;> cases b <;> simp_all [outcomeName]

/-- The seqs a whole recovery run APPLIES against the world
(`RevL.Lemmas.replayed`), as labels. -/
def replayedSeqLabels (L : Log) : List String := (replayed L).map toString

/-- The residue surface a roll-back reports (`RevL.Lemmas.reported`), as
labels. -/
def reportedSeqLabels (L : Log) (ok : Seq → Bool) : List String :=
  (reported L ok).map toString

/-- **The replayed column is the model's applied set.** A seq is applied
exactly when the run rolls back AND some record of the log re-issues it.
So a reference that applies an inverse this model skips — or skips one it
re-issues — disagrees with this column. -/
theorem mem_replayed_iff (L : Log) (s : Seq) :
    s ∈ replayed L ↔ outcome L = .rolledBack ∧ ∃ r ∈ L, s ∈ reissued L r := by
  have hr : replayed L = if outcome L = .rolledBack then rollbackReplay L else [] := by
    unfold replayed; cases outcome L <;> simp
  rw [hr]
  by_cases h : outcome L = .rolledBack
  · simp [h, rollbackReplay, List.mem_flatMap]
  · simp [h]

/-- **The residue column is the model's reported surface.** Left to right:
a seq printed here belongs to a record the roll-back walk disposed as
residue. Right to left: every such record's seq is printed. So a reference
that silently retains an owed referent, or reports one the model
discharges, disagrees with this column. -/
theorem mem_reported_iff (L : Log) (ok : Seq → Bool) (s : Seq) :
    s ∈ reported L ok ↔ ∃ r ∈ L, ∃ d : Residue, dispose L ok r = some (s, .residue d) := by
  simp only [RevL.Lemmas.reported, List.mem_filterMap]
  constructor
  · rintro ⟨r, hr, hd⟩
    refine ⟨r, hr, ?_⟩
    revert hd
    rcases hdis : dispose L ok r with _ | ⟨t, d⟩
    · simp
    · cases d <;> simp_all
  · rintro ⟨r, hr, d, hd⟩
    exact ⟨r, hr, by rw [hd]⟩

/-- The printed columns are the model's two lists, spelled. -/
theorem replayedSeqLabels_eq (L : Log) :
    replayedSeqLabels L = (replayed L).map toString := rfl

theorem reportedSeqLabels_eq (L : Log) (ok : Seq → Bool) :
    reportedSeqLabels L ok = (reported L ok).map toString := rfl

/-- **A commit applies nothing.** `RevL.A8.commit_replays_no_inverse`, as
it lands in the column: the whole replayed list is empty whenever the
outcome is not a roll-back, so a reference that re-issued an inverse after
a durable `commit-approved` or `activation-complete` disagrees. -/
theorem replayedSeqLabels_nil_of_not_rolledBack {L : Log}
    (h : outcome L ≠ .rolledBack) : replayedSeqLabels L = [] := by
  simp only [replayedSeqLabels, RevL.A8.commit_replays_no_inverse h, List.map_nil]

/-- **A committed transaction is never rolled back**
(`RevL.A8.committed_transaction_is_retained`), as it lands in the column:
a descriptor whose seq carries a durable `discharge` record re-issues
nothing, so its seq cannot reach the replayed list through that record. -/
theorem discharged_not_reissued {L : Log} {s : Seq} {e : Entry} {i : Bool}
    (h : s ∈ dischargedSeqs L) : reissued L (.descriptor s e i) = [] := by
  cases e <;> simp [reissued, memSeq_iff.mpr h]

/-- **At most once, across the crash** (item 309 §3a). An UNDECLARED
inverse whose fence is already durable is not applied again — by either
record family. This is the branch the `O` row caught missing from the
legacy `effect` family in `RevL.Lemmas.dispose`. -/
theorem fenced_not_reissued {L : Log} {s : Seq} (h : s ∈ fencedSeqs L) :
    reissued L (.descriptor s .transactional false) = []
    ∧ reissued L (.effect s true true false) = [] := by
  refine ⟨?_, ?_⟩ <;> simp [reissued, memSeq_iff.mpr h]

/-- **A declared-idempotent inverse replays freely** — the other half of
309, and the reason the `idem` field is in the corpus at all. Over a log
with no durable discharge, a declared-idempotent transactional inverse is
re-issued no matter how many fences the log carries. -/
theorem declared_idempotent_reissued {L : Log} {s : Seq}
    (h : s ∉ dischargedSeqs L) :
    reissued L (.descriptor s .transactional true) = [s] := by
  simp [reissued, memSeq_false.mpr h]

/-- **The residue column is empty exactly when the roll-back owes
nothing** — `RevL.Lemmas.clean`, the predicate `residue.clean` in the
reference's report. -/
theorem reportedSeqLabels_nil_iff_clean (L : Log) (ok : Seq → Bool) :
    reportedSeqLabels L ok = [] ↔ clean L ok = true := by
  simp [reportedSeqLabels, clean, List.isEmpty_iff]

end Recovery

-- ---------------------------------------------------------------- rows

structure MRow where
  path : String
  name : String
  requires : List String
  provides : List String
  realms : List (String × String)
  isTemplate : Bool

structure URow where
  path : String
  comp : String
  ctx : String
  root : String
  svc : String
  meth : String

structure BRow where
  path : String
  svc : String
  meth : String
  mode : String

structure QRow where
  path : String
  svc : String
  meth : String
  entry : String

structure ARow where
  path : String
  comp : String
  cap : String

/-- A provide method's reached crossing. It is the one fact row two
surfaces read, and they name a crossing in DIFFERENT namespaces, so it
carries both spellings:

* `cap` is the ATTENUATION element — the DECLARED boundary token and its
  valuation (`lower._cap_keyed`), or the namespaced SERVICE where no
  declaration tokens it (`lower._undeclared_cap`). `Attenuates` compares a
  parent's grant with a child's demand across a component boundary, and
  `covers` clause 1 is a boundary IDENTITY test, so a local wiring key —
  which two components spell however each likes — names nothing there;
* `bound` is the BOUND element — the wiring key the crossing went through,
  which is exactly how the reference names it when it measures a provide
  method against its own service's `emission[...]` declaration.

`A` and `K` feed only the attenuation fold and carry only that spelling. -/
structure FRow where
  path : String
  comp : String
  key : String
  svc : String
  meth : String
  cap : String
  bound : String

structure KRow where
  path : String
  comp : String
  bindn : String
  cap : String

structure SRow where
  path : String
  parent : String
  child : String

structure PBRow where
  path : String
  comp : String
  key : String

structure PRRow where
  path : String
  comp : String
  key : String

structure XRow where
  path : String
  code : String

/-- One host-family acquisition a component's reachable code names, with the
position that decides its legality (`HA` row). `pos` is `bracket` when the
verb is the root of an `effect`'s acquisition expression — the only legal
site — and otherwise the site the checker names (`plain` / `emit` / `undo` /
`fn`). -/
structure HARow where
  path : String
  comp : String
  verb : String
  pos : String

/-- One reach of a `deferred` emission extern (`DR` row), with the scope and
position the model's `RevL.G4Deferred` rule reads. -/
structure DRRow where
  path : String
  scope : String
  owner : String
  ext : String
  pos : String

/-- A declared config field (`CF` row). `kind` is `component` or `extern`,
the two owners `lower._check_config` is called for; `spelling` is the type
as the author wrote it and is carried for legibility only — the judgment
reads the `CN` nodes. -/
structure CFRow where
  path : String
  kind : String
  owner : String
  field : String
  spelling : String

/-- One node a config field's declared type reaches (`CN` row), classified
by the SHIPPED config-data tables (`typecheck._CONFIG_DATA_SCALARS` /
`_CONFIG_DATA_CONTAINERS` / `_CONFIG_ERASED`, the declared service names,
and the lightweight type table a nominal head resolves through).

This is the only fact row in the export that describes a declared TYPE
rather than something a body does, which is why config-is-data needed a row
of its own rather than a case in `g4OK` (issue 1161): both crossing rules
judge a call, and there is nothing about a call to read here. -/
structure CNRow where
  path : String
  kind : String
  owner : String
  field : String
  ord : String
  form : String
  spelling : String

structure IRow where
  path : String
  comp : String
  index : String
  kind : String
  heads : List String
  inverse : List String

def parseI (f : List String) : Option IRow :=
  match f with
  | ["I", path, comp, index, kind, heads, inverse] =>
      some ⟨path, comp, index, kind, splitKeys heads, splitKeys inverse⟩
  | _ => none

/-- Reconstruct an expression whose reach surface is EXACTLY the exported
head list — one `.call k []` per head, nested so the tail's heads survive.
The pre-276 version kept only the FIRST head (`h :: _ => .call h []`), which
dropped every subsequent crossing of a statement (`kv.set,status.shared`
became `kv.set` alone), so the confinement check below could not see the
heads it silently discarded. `heads_exprOfHeads` proves the reconstruction is
faithful. -/
def exprOfHeads : List String → Expr
  | [] => .lit "unit"
  | h :: rest => .call h [exprOfHeads rest]

/-- **The reconstruction is non-lossy** (issue 276, step 1): the reach
surface of `exprOfHeads hs` is exactly `hs`, so no exported head escapes the
confinement check. -/
theorem heads_exprOfHeads : ∀ hs : List String,
    RevL.Typing.heads (exprOfHeads hs) = hs := by
  intro hs
  induction hs with
  | nil => simp [exprOfHeads, RevL.Typing.heads]
  | cons h rest ih =>
    simp [exprOfHeads, RevL.Typing.heads, ih]

def stmtOf (r : IRow) : RevL.Syntax.Stmt :=
  match r.kind with
  | "effect" => .effect (exprOfHeads r.heads) (exprOfHeads r.inverse)
  | "emit" => .emit (exprOfHeads r.heads)
  | "raw" => .raw (exprOfHeads r.heads)
  | _ => .pure (exprOfHeads r.heads)

/-- The receiver ROOT of a reconstructed head: the segment before the first
`.` (`w.task.run` → `w`, a bare `db` → `db`). This is the granularity the
confinement context is expressed in — a declared require local or a
require-held binding — so a crossing is confined exactly when its root is
declared. `String.splitOn` never returns `[]`, so the fallback is dead. -/
def rootOf (s : String) : String :=
  match s.splitOn "." with
  | h :: _ => h
  | [] => s

/-- G6 confinement (issue 276): a statement is confined under declared
context `C` when every reconstructed call head it reaches is in `C`. `C` is
carried at head granularity (the component's own heads whose root is
declared), so membership decides `stmtHeads ⊆ C` directly. -/
def confinedB (C : List String) (s : RevL.Syntax.Stmt) : Bool :=
  (RevL.Typing.stmtHeads s).all (fun k => decide (k ∈ C))

/-- **The confinement verdict is the model's confinement surface.** The Bool
the oracle prints is `decide`d from `RevL.Typing.stmtHeads` — the very reach
surface `RevL.G6.confinement` quantifies over (`typedIn_confined`) — so a row
that says `confined=ok` asserts exactly `∀ k ∈ stmtHeads s, k ∈ C`, and a
reference that accepted a head outside `C` disagrees with it. -/
theorem confinedB_iff (C : List String) (s : RevL.Syntax.Stmt) :
    confinedB C s = true ↔ (∀ k ∈ RevL.Typing.stmtHeads s, k ∈ C) := by
  simp only [confinedB, List.all_eq_true, decide_eq_true_eq]

/-! ## Reconstructing `RevL.Lemmas.Prog` for G5/G8 (issue 276)

G6 above decides a per-statement predicate; G5 (teardown purity) and G8
(the boundary surface) are stated over a whole `Prog` — the extern table and
the fn call graph (`RevL.Lemmas.Prog`, `ClassLemmas.lean:233`) — because their
load-bearing statements (`RevL.G5Classified.registrations`,
`RevL.G8Classified.stmtSurface`) fold the classification reach over that graph.
The `EX`/`FN`/`PG` rows carry the graph; the parsers below rebuild it, and the
two deciders decide the model's own `stmtSurface` / `registrations`, verbatim
(`stmtSurfaceB_iff` / `registrationsB_iff`, both by `rfl` — the printed value
IS the model function applied, the whole-Prog analog of `confinedB_iff`). -/

structure PGRow where
  path : String
  fuel : Nat

structure EXRow where
  path : String
  name : String
  cls : String
  undo : String
  compensate : String
  caps : List String

structure FNRow where
  path : String
  name : String
  calls : List String
  star : Bool

def parsePG (f : List String) : Option PGRow :=
  match f with
  | ["PG", path, fuel] => some ⟨path, fuel.toNat!⟩
  | _ => none

def parseEX (f : List String) : Option EXRow :=
  match f with
  | ["EX", path, name, cls, undo, compensate, caps] =>
      some ⟨path, name, cls, undo, compensate, splitKeys caps⟩
  | _ => none

def parseFN (f : List String) : Option FNRow :=
  match f with
  | ["FN", path, name, calls, star] =>
      some ⟨path, name, splitKeys calls, star == "star"⟩
  | _ => none

/-- The `EX` row's class string as the model's four-point lattice. An
unrecognised string is `pure` — the fail-safe reading, since `pure` reaches
and crosses nothing. -/
def clsOf : String → Cls
  | "acquire" => .acquire
  | "witnessed" => .witnessed
  | "emission" => .emission
  | _ => .pure

/-- `-` (the exporter's empty-slot sentinel) is `none`. -/
def optName (s : String) : Option String := if s == "-" then none else some s

/-- Rebuild the file's `RevL.Lemmas.Prog` from its `EX`/`FN` rows. `star` is
carried separately (`starTainted`): the model has no first-class dispatch, so
a `star`-reaching statement is decided `n/a`, never folded. -/
def progOf (exs : List EXRow) (fns : List FNRow) : Prog :=
  { externs := exs.map fun e =>
      { name := e.name, cls := clsOf e.cls, undo := optName e.undo,
        compensate := optName e.compensate, caps := e.caps },
    fns := fns.map fun f => { name := f.name, calls := f.calls } }

/-- One `.call` per inverse head, `seq`-folded (the `UndoBody` whose
teardown `registrations` counts), `nil` for the empty body. The G5 analog of
`exprOfHeads`; `bodyNames_bodyOfHeads` proves it non-lossy. -/
def bodyOfHeads : List String → RevL.G5Classified.UndoBody
  | [] => .nil
  | h :: rest => .seq (.call h) (bodyOfHeads rest)

/-- **The teardown body is non-lossy**: every exported inverse head is a call
`registrations` counts, so no crossing on the teardown path is dropped. -/
theorem bodyNames_bodyOfHeads : ∀ hs : List String,
    RevL.G5Classified.bodyNames (bodyOfHeads hs) = hs := by
  intro hs
  induction hs with
  | nil => rfl
  | cons h rest ih =>
    simp [bodyOfHeads, RevL.G5Classified.bodyNames, ih]

/-- **G8 decider.** The boundary surface of a reconstructed statement is the
model's `stmtSurface` — `stmtHeads` composed with the classification reach
fold — with no per-constructor case. -/
def stmtSurfaceB (p : Prog) (fuel : Nat) (s : RevL.Syntax.Stmt) : List String :=
  RevL.G8Classified.stmtSurface p fuel s

/-- **The G8 verdict is the model's boundary surface**, verbatim: the caps
the oracle prints are `RevL.G8Classified.stmtSurface` applied, so a row is a
claim about the reach fold `surface_only_declared_crossings` /
`surface_enumerates_reached_crossings` are stated over, not a restatement. -/
theorem stmtSurfaceB_iff (p : Prog) (fuel : Nat) (s : RevL.Syntax.Stmt) :
    stmtSurfaceB p fuel s = RevL.G8Classified.stmtSurface p fuel s := rfl

/-- **G5 decider.** How many of a teardown body's calls transitively reach a
host crossing — the model's `registrations`, which reads its argument. -/
def registrationsB (p : Prog) (fuel : Nat) (b : RevL.G5Classified.UndoBody) : Nat :=
  RevL.G5Classified.registrations p fuel b

/-- **The G5 verdict is the model's registration count**, verbatim: the
number the oracle prints is `RevL.G5Classified.registrations` applied, so
`teardown=0` asserts exactly `registrations p fuel b = 0`
(`registrations_zero_iff`), the pure-teardown rule G5 enforces. -/
theorem registrationsB_iff (p : Prog) (fuel : Nat)
    (b : RevL.G5Classified.UndoBody) :
    registrationsB p fuel b = RevL.G5Classified.registrations p fuel b := rfl

/-- Names reaching a `star` (first-class dispatch) fn, closed transitively.
`fuel` bounds the closure; `fns.length` reaches its fixed point. A statement
touching one of these is decided `n/a` — the shape is outside the model. -/
def starClosure : Nat → List FNRow → List String → List String
  | 0, _, acc => acc
  | n + 1, fns, acc =>
      let acc' := ((fns.filter (fun f => f.calls.any (fun c => acc.contains c))
                   ).map (·.name) ++ acc).eraseDups
      if acc'.length ≤ acc.length then acc else starClosure n fns acc'

def starTainted (fns : List FNRow) : List String :=
  starClosure fns.length fns ((fns.filter (·.star)).map (·.name))

/-- `key=realm` pairs from a component's `isolate` clauses. -/
def parseRealms (s : String) : List (String × String) :=
  (splitKeys s).filterMap fun chunk =>
    match chunk.splitOn "=" with
    | [k, r] => some (k, r)
    | _ => none

def parseM (f : List String) : Option MRow :=
  match f with
  | ["M", path, name, reqs, provs, realms, role] =>
      some ⟨path, name, splitKeys reqs, splitKeys provs, parseRealms realms,
            role == "template"⟩
  | _ => none

def parseU (f : List String) : Option URow :=
  match f with
  | ["U", path, comp, ctx, root, svc, meth] => some ⟨path, comp, ctx, root, svc, meth⟩
  | _ => none

def parseB (f : List String) : Option BRow :=
  match f with
  | ["B", path, svc, meth, mode] => some ⟨path, svc, meth, mode⟩
  | _ => none

def parseQ (f : List String) : Option QRow :=
  match f with
  | ["Q", path, svc, meth, entry] => some ⟨path, svc, meth, entry⟩
  | _ => none

def parseA (f : List String) : Option ARow :=
  match f with
  | ["A", path, comp, cap] => some ⟨path, comp, cap⟩
  | _ => none

def parseF (f : List String) : Option FRow :=
  match f with
  | ["F", path, comp, key, svc, meth, cap, bound] =>
      some ⟨path, comp, key, svc, meth, cap, bound⟩
  | _ => none

def parseK (f : List String) : Option KRow :=
  match f with
  | ["K", path, comp, bind, cap] => some ⟨path, comp, bind, cap⟩
  | _ => none

def parseS (f : List String) : Option SRow :=
  match f with
  | ["S", path, parent, child] => some ⟨path, parent, child⟩
  | _ => none

def parsePB (f : List String) : Option PBRow :=
  match f with
  | ["PB", path, comp, key] => some ⟨path, comp, key⟩
  | _ => none

def parsePR (f : List String) : Option PRRow :=
  match f with
  | ["PR", path, comp, key] => some ⟨path, comp, key⟩
  | _ => none

def parseX (f : List String) : Option XRow :=
  match f with
  | ["X", path, code] => some ⟨path, code⟩
  | _ => none

def parseHA (f : List String) : Option HARow :=
  match f with
  | ["HA", path, comp, verb, pos] => some ⟨path, comp, verb, pos⟩
  | _ => none

def parseDR (f : List String) : Option DRRow :=
  match f with
  | ["DR", path, scope, owner, ext, pos] => some ⟨path, scope, owner, ext, pos⟩
  | _ => none

def parseCF (f : List String) : Option CFRow :=
  match f with
  | ["CF", path, kind, owner, field, spelling] =>
      some ⟨path, kind, owner, field, spelling⟩
  | _ => none

def parseCN (f : List String) : Option CNRow :=
  match f with
  | ["CN", path, kind, owner, field, ord, form, spelling] =>
      some ⟨path, kind, owner, field, ord, form, spelling⟩
  | _ => none

/-- One entry of a teardown scenario's LIFO stack, in registration order
(G7). `ord` is the corpus's label for the entry, carried through the
model's `inverse` slot; `kind` is one of the three the model names.

The row's fifth field is the registration SEAM the reference used
(`body` / `method`) and is deliberately DROPPED here: the model has one
per-activation stack and no seam distinction, so the seam is the
reference's business alone. That the two sides still agree is the claim —
`runtime.py` registers a method-body entry on `_deferred_transactional`
and unwinds it inside `drain` rather than through cordis, and the model
says the resulting disposition is the same as if it had been on the
stack. -/
structure ERow where
  scen : String
  ord : String
  kind : String

/-- The verdict one teardown scenario was torn down under (G7). -/
structure JRow where
  scen : String
  verdict : String

def parseE (f : List String) : Option ERow :=
  match f with
  | ["E", scen, ord, kind, _seam] => some ⟨scen, ord, kind⟩
  | _ => none

def parseJ (f : List String) : Option JRow :=
  match f with
  | ["J", scen, verdict] => some ⟨scen, verdict⟩
  | _ => none

/-- `Z <cap> <token>`. -/
def parseZ (f : List String) : Option (String × String) :=
  match f with
  | ["Z", cap, tok] => some (cap, tok)
  | _ => none

/-- `Y <cap> <param> <kind> <value>`, already canonical: a path value is
its `/`-joined component list, a ceiling its base-unit integer. -/
def parseY (f : List String) : Option (String × String × PVal) :=
  match f with
  | ["Y", cap, name, "path", v] =>
      some (cap, name, .path ((v.splitOn "/").filter (fun p => p != "")))
  | ["Y", cap, name, "discrete", v] => some (cap, name, .discrete v)
  | ["Y", cap, name, "ceiling", v] =>
      match v.toNat? with
      | some n => some (cap, name, .ceiling n)
      | none => none
  | _ => none

/-- The decomposition table: canonical cap string -> the model's `Cap`. -/
abbrev CapTable := List (String × Cap)

def buildCapTable (zs : List (String × String))
    (ys : List (String × String × PVal)) : CapTable :=
  zs.map fun (s, tok) =>
    (s, ⟨tok, (ys.filter (fun y => y.1 == s)).map (fun y => (y.2.1, y.2.2))⟩)

/-- A missing decomposition is a hard error, never a silently empty cap. -/
def capOf (t : CapTable) (s : String) : Option Cap :=
  match t.find? (·.1 == s) with
  | some (_, c) => some c
  | none => none

def capToken (t : CapTable) (s : String) : String :=
  match capOf t s with
  | some c => c.token
  | none => s

-- ------------------------------------------------------------ verdicts

/-- Marker rule (G4-shaped): marker presence must equal the interface's
declaration — every call to a declared emission method must be `emit`
-marked, and an `emit`-marked call to a non-emission method is refused.
Receivers include spawn handles (the exporter resolves them).

The marker covers the HEAD call alone (revl issue 1175,
`lower._emit_head_args`): the head's arguments lower in the mode the
`emit` sits in, so an `emitarg` fact is judged exactly as a `plain` one.
An emission there needs a marker of its own, and a plain method there is
admitted. A marker written inside the argument list (`emitnested`) is a
violation whatever the method declares: the marker admits one crossing,
so the inner one is hoisted into its own step first.

A HOST emission (a named call to an `emission` extern, or to a fn that
reaches one) has no service to resolve. The exporter writes its row on the
pseudo-service `@host` and only for a callee that IS an emission, so a row
there is judged as a call to an emission method (revl issues 1427, 1437).

PRIVATE RESTATEMENT (see the header): the G4 model is indexed by
statement syntax, and the export carries call facts. -/
def g4OK (ems : List (String × String)) (calls : List URow) : Bool :=
  !calls.any fun u =>
    let em := u.svc == "@host" || ems.any fun e => e.1 == u.svc && e.2 == u.meth
    u.ctx == "emitnested" || ((u.ctx == "emit") != em)

/-- The host acquisition verb table — the model's copy of the checker's
`_HOST_ACQUIRE_VERBS` (`src/revl/typecheck.py`). Each opens a host resource
whose release is a SEPARATE verb; the differential is that this list and the
checker's must agree (issue 334). -/
def hostAcquireVerbs : List String := ["Map.new", "Pool.open", "Stream.source"]

/-- Host-acquisition rule (G4-shaped, category `acquire`): a host acquire verb
is legal ONLY as the acquisition of an `effect … undo …` bracket, the one
construct that registers its release with the teardown accumulator. Every `HA`
occurrence of a table verb must therefore sit in `bracket` position; a `plain`
`let`, an `emit` expression, an `undo` slot, or a reached `fn` body acquires a
resource nothing reclaims (`lower._refuse_unbracketed_host_acquire`).

PRIVATE RESTATEMENT (see the header, beside `g4OK`): the acquisition-position
fact is carried by the export, and the verb table and rule are stated here. -/
def hostAcquireOK (has : List HARow) : Bool :=
  !has.any fun h => hostAcquireVerbs.contains h.verb && h.pos != "bracket"

/-- The forms a config field's declared type may reach. A scalar; a
container (`Opt`/`List`/`Map`/`Result`); a declared record, variant or
alias; a structural record literal; and a type parameter, whose binding is
the type argument at the use site and is walked in its own right.

An ALLOWLIST, and deliberately so — the checker's own rule is
"a head that is not *provably* data is refused", written that way because
enumerating the forbidden heads left `Any`, `Value`, `Never` and every
opaque nominal passing (item 378). Stating it as a denylist here would
reintroduce exactly that, one layer out: a classification neither side had
heard of would be admitted by default. With the allowlist, an unknown form
REFUSES, so the model can only become stricter than the checker — the safe
direction, reported as `formal-strict` — and never blind to one of its
refusals. -/
def configDataForms : List String :=
  ["scalar", "container", "record", "variant", "struct", "tparam"]

/-- Config-is-data rule (G4-shaped, category `config-data`, item 378): a
config value is injected as static data at plug/spawn/load time, so its
declared type must be built, TRANSITIVELY, out of data. An arrow field is a
live callable invoked past every authority fold — no ticket, no reach
attribution, no `emission[...]` bound to exceed — and a `service` field is a
capability handed over with no wiring at all.

The two other rules under this guarantee (`g4OK`, `hostAcquireOK`) judge
what a body does, over crossing facts. This one judges a DECLARATION, so
nothing in the crossing facts could express it and a config refusal was
invisible to the model — reported as `missed-G4`, fatal, wherever a fixture
for it was placed (issue 1161).

PRIVATE RESTATEMENT (see the header, beside `g4OK`): the type nodes arrive
decomposed and classified by the shipped tables, the way `Z`/`Y` carry a
capability, and the judgment over them is stated here. -/
def configDataOK (ns : List CNRow) : Bool :=
  ns.all fun n => configDataForms.contains n.form

/-- Provide-method bound: the reached emission tokens must be within the
declared bound (plain => none; any => free; scoped => the declared
entries).

PRIVATE RESTATEMENT (see the header): the model states no
declaration-versus-reach bound. -/
def methodBoundOK (t : CapTable)
                   (bounds : List (String × String × String × List String))
                   (svc meth : String) (caps : List String) : Bool :=
  let b := bounds.find? (fun x => x.1 == svc && x.2.1 == meth)
  let mode := match b with | some x => x.2.2.1 | none => "plain"
  let entries := match b with | some x => x.2.2.2 | none => []
  if mode == "any" then true
  else if mode == "scoped" then
    (caps.map (capToken t)).all fun tk => entries.contains tk
  else caps.isEmpty

/-- Component -> capability set, as an association list. -/
abbrev CapMap := List (String × List String)

def lookupCaps (m : CapMap) (k : String) : List String :=
  match m.find? (·.1 == k) with
  | none => []
  | some (_, cs) => cs

/-- Insert-or-union. An ABSENT key is ADDED, not dropped: a component with
no reach of its own still holds whatever its `requires` bindings grant it,
and a spawner with no emissions of its own is still a closure node. -/
def upsertCaps (m : CapMap) (k : String) (caps : List String) : CapMap :=
  if m.any (·.1 == k) then
    m.map (fun (n, cs) => if n == k then (n, union cs caps) else (n, cs))
  else m ++ [(k, caps.eraseDups)]

/-- Group `(comp, cap)` pairs by component. -/
def groupCaps (pairs : List (String × String)) : CapMap :=
  pairs.foldl (fun acc (k, c) => upsertCaps acc k [c]) []

/-- One closure step over the spawn edges: every parent absorbs its
children's caps.

PRIVATE RESTATEMENT (see the header): `RevL.CapCeilings.reachIn` is this
closure in the model, but it is indexed by a `Comp` carrying
`body : List Stmt`, which the export does not produce. Only the CLOSURE
is local; the judgment it feeds (`attenuatesB`) is the model's. -/
def oneStep (edges : List (String × String)) (closed : CapMap) : CapMap :=
  edges.foldl (fun acc (p, c) => upsertCaps acc p (lookupCaps acc c)) closed

def closeN (n : Nat) (edges : List (String × String)) (closed : CapMap) : CapMap :=
  match n with
  | 0 => closed
  | n + 1 => closeN n edges (oneStep edges closed)

/-! ## Deciding the G4 deferred-position rule (issue #1742)

A `DR` row is one reach of a `deferred` emission extern. Its scope and
position are read into the model's own `RevL.G4Deferred.Reach`, and the file
verdict is `RevL.G4Deferred.deferredB` over the file's reaches.
`deferredOKB_iff` is the bridge: the printed `DF` Bool is the declarative
`RevL.G4Deferred.DeferredOK`. A scope or position the model has no
constructor for is a HARD error in `main`, never a silently dropped reach. -/

section DeferredPosition

def parseScope : String → Option RevL.G4Deferred.Scope
  | "fn" => some .fn
  | "test" => some .test
  | "component" => some .component
  | _ => none

def parsePos : String → Option RevL.G4Deferred.Pos
  | "call" => some .call
  | "arrow" => some .arrow
  | "value" => some .value
  | _ => none

/-- The model's reach for one `DR` row, or `none` for an unknown spelling. -/
def reachOf (r : DRRow) : Option RevL.G4Deferred.Reach :=
  match parseScope r.scope, parsePos r.pos with
  | some s, some p => some ⟨s, p⟩
  | _, _ => none

/-- **Deferred-position decider**: the model's rule over the file's reaches. -/
def deferredOKB (reaches : List RevL.G4Deferred.Reach) : Bool :=
  RevL.G4Deferred.deferredB reaches

/-- **The `DF` verdict is the model's rule** (`RevL.G4Deferred.deferredB_iff`). -/
theorem deferredOKB_iff (reaches : List RevL.G4Deferred.Reach) :
    deferredOKB reaches = true ↔ RevL.G4Deferred.DeferredOK reaches :=
  RevL.G4Deferred.deferredB_iff reaches

end DeferredPosition

/-! ## Deciding A2: no acquisition after a provision (issue 1166)

The `AQ` rows are one component's activation body in order, each statement
as the rule sees it. The verdict is `RevL.A2.a2B` — the checker's own fold
(`lower._dispatch_action`: a flag set at the first `provide`, an acquisition
refused while it is set) — and `a2OKB_iff` below is the bridge to the
declarative rule `RevL.A2.A2OK`, the hypothesis of
`RevL.A2.withdrawals_precede_releases`: under it the Phase-1 proof pass of
the body's stack runs every withdrawal before every release. So an `A2 …
a2=ok` row is a component whose teardown cannot revert an acquisition while a
provision is still callable, and the reference recomputes the same fold
independently from the TSV (`diff_corpus.reference_from_tsv`). -/

section A2Ordering

/-- One activation-body statement of one component, at body index `ord`. -/
structure AQRow where
  path : String
  comp : String
  ord : Nat
  kind : String

def parseAQ (f : List String) : Option AQRow :=
  match f with
  | ["AQ", path, comp, ord, kind] => ord.toNat?.map fun n => ⟨path, comp, n, kind⟩
  | _ => none

/-- The exporter's spelling of a step, read back as the model's `Step`. -/
def parseStep : String → Option RevL.A2.Step
  | "acquire" => some .acquire
  | "provide" => some .provide
  | "other" => some .other
  | _ => none

/-- The exporter's spelling of a step (`diff_corpus._a2_step`). -/
def stepName : RevL.A2.Step → String
  | .acquire => "acquire"
  | .provide => "provide"
  | .other => "other"

/-- **The spelling round-trips**: every step the model names is read back
as itself, so no body step is silently dropped between the two sides. -/
theorem parseStep_stepName : ∀ s : RevL.A2.Step, parseStep (stepName s) = some s := by
  intro s; cases s <;> rfl

/-- Insertion by body index, so the steps are folded in body order whatever
order the rows arrived in. -/
def insertByOrd (r : AQRow) : List AQRow → List AQRow
  | [] => [r]
  | x :: xs => if r.ord ≤ x.ord then r :: x :: xs else x :: insertByOrd r xs

def sortByOrd (rs : List AQRow) : List AQRow := rs.foldr insertByOrd []

/-- **A2 decider**: the model's fold over the body's steps. -/
def a2OKB (steps : List RevL.A2.Step) : Bool := RevL.A2.a2B steps

/-- **The A2 verdict is the model's rule**: the printed Bool is `true`
exactly when no `provide` is followed by an `acquire`
(`RevL.A2.a2B_iff`). -/
theorem a2OKB_iff (steps : List RevL.A2.Step) :
    a2OKB steps = true ↔ RevL.A2.A2OK steps :=
  RevL.A2.a2B_iff steps

end A2Ordering

/-! ## Deciding the approval floor (issue #1455)

Item 246's declaration-owned floor: a marked crossing that reaches a
capability token some extern declared `requires approval` for must carry a
`with` edge whose scope covers it (`lower._require_declared_approval`). The
exporter resolves the three facts the rule is stated over, per crossing:
the tokens it reaches (`AX`), its edge (`AE`) and the file's required tokens
(`AR`). The verdict is the model's `RevL.G4Approval.crossingB`, and
`approvalRowB_iff` is the bridge to `RevL.G4Approval.CrossingOK`. -/

section ApprovalFloor

structure ARRow where
  path : String
  token : String

structure AXRow where
  path : String
  comp : String
  ord : String
  token : String

structure AERow where
  path : String
  comp : String
  ord : String
  scope : String

def parseAR (f : List String) : Option ARRow :=
  match f with
  | ["AR", path, token] => some ⟨path, token⟩
  | _ => none

def parseAX (f : List String) : Option AXRow :=
  match f with
  | ["AX", path, comp, ord, token] => some ⟨path, comp, ord, token⟩
  | _ => none

def parseAE (f : List String) : Option AERow :=
  match f with
  | ["AE", path, comp, ord, scope] => some ⟨path, comp, ord, scope⟩
  | _ => none

/-- **Approval decider**: the model's judgment over one crossing. -/
def approvalRowB (required tokens : List String) (edge : Option String) : Bool :=
  RevL.G4Approval.crossingB required ⟨tokens, edge⟩

/-- **The approval verdict is the model's rule**: the printed Bool is
`true` exactly when every approval-required token the crossing reaches is
covered by its edge. -/
theorem approvalRowB_iff (required tokens : List String) (edge : Option String) :
    approvalRowB required tokens edge = true ↔
      RevL.G4Approval.CrossingOK required ⟨tokens, edge⟩ :=
  RevL.G4Approval.crossingB_iff required ⟨tokens, edge⟩

end ApprovalFloor

/-! ## Deciding G6 binding uniqueness (issue #1812)

A `BE` row is one step through a binding scope, in source order: a `seed`
name in view when the scope starts, a `bind`, or an `enter`/`leave` block
boundary. The verdict is the model's `RevL.G6Binding.bindingB` over the seed
and the events, and `bindingRowB_iff` is the bridge to
`RevL.G6Binding.BindingOK`. An unknown step kind is a hard error in `main`. -/

section BindingUnique

structure BERow where
  path : String
  comp : String
  scope : String
  ord : Nat
  kind : String
  name : String

def parseBE (f : List String) : Option BERow :=
  match f with
  | ["BE", path, comp, scope, ord, kind, name] =>
      ord.toNat?.map fun o => ⟨path, comp, scope, o, kind, name⟩
  | _ => none

/-- The model's event for one `BE` row; `none` for a seed or an unknown kind. -/
def beEvent (r : BERow) : Option RevL.G6Binding.Ev :=
  match r.kind with
  | "bind" => some (.bind r.name)
  | "enter" => some .enter
  | "leave" => some .leave
  | _ => none

def beKnown (r : BERow) : Bool :=
  r.kind == "seed" || (beEvent r).isSome

/-- **Binding decider**: the model's judgment over one scope. -/
def bindingRowB (seed : List String) (evs : List RevL.G6Binding.Ev) : Bool :=
  RevL.G6Binding.bindingB seed evs

/-- **The `BU` verdict is the model's rule.** -/
theorem bindingRowB_iff (seed : List String) (evs : List RevL.G6Binding.Ev) :
    bindingRowB seed evs = true ↔ RevL.G6Binding.BindingOK seed evs :=
  RevL.G6Binding.bindingB_iff seed evs

end BindingUnique

/-! ## Deciding G1 declared access (issue #1807)

A `GA` row is one access root of a component: a call head's root the
checker can only resolve through a requirement. The verdict is the model's
`RevL.G1Access.accessB` over the component's declared requirements (its `M`
row) and its roots, and `accessRowB_iff` is the bridge to
`RevL.G1Access.AccessOK`. -/

section KeyAccess

structure GARow where
  path : String
  comp : String
  root : String

def parseGA (f : List String) : Option GARow :=
  match f with
  | ["GA", path, comp, root] => some ⟨path, comp, root⟩
  | _ => none

/-- **Access decider**: the model's judgment over one component. -/
def accessRowB (declared roots : List String) : Bool :=
  RevL.G1Access.accessB declared roots

/-- **The `G1` verdict is the model's rule.** -/
theorem accessRowB_iff (declared roots : List String) :
    accessRowB declared roots = true ↔ RevL.G1Access.AccessOK declared roots :=
  RevL.G1Access.accessB_iff declared roots

end KeyAccess

/-! ## Deciding A1 async colour (issue #1808)

An `AS` row is one site the A1 rules judge, with the heads it calls. The
verdict is the model's `RevL.A1Async.siteB` over the site's kind, the file's
async names (`AN`) and its `fn` call graph (the `FN` rows), with a fuel of
one step per `fn` plus one: a shortest reach path visits each `fn` at most
once. `asyncRowB_iff` is the bridge to `RevL.A1Async.SiteOK`. An `AG` row is
decided by `RevL.A1Async.sigB` (`sigRowB_iff`). An unknown site kind is a
hard error in `main`. -/

section AsyncColour

structure ANRow where
  path : String
  name : String

structure ASRow where
  path : String
  comp : String
  ord : String
  kind : String
  heads : List String

structure AGRow where
  path : String
  comp : String
  meth : String
  declared : String
  impl : String

def parseAN (f : List String) : Option ANRow :=
  match f with
  | ["AN", path, name] => some ⟨path, name⟩
  | _ => none

def parseAS (f : List String) : Option ASRow :=
  match f with
  | ["AS", path, comp, ord, kind, heads] => some ⟨path, comp, ord, kind, splitKeys heads⟩
  | _ => none

def parseAG (f : List String) : Option AGRow :=
  match f with
  | ["AG", path, comp, meth, declared, impl] => some ⟨path, comp, meth, declared, impl⟩
  | _ => none

def parseSiteKind : String → Option RevL.A1Async.Kind
  | "syncMethod" => some .syncMethod
  | "asyncMethod" => some .asyncMethod
  | "effect" => some .effect
  | "effectAwait" => some .effectAwait
  | "emit" => some .emit
  | "emitAwait" => some .emitAwait
  | "undo" => some .undo
  | "compensate" => some .compensate
  | _ => none

/-- **Async decider**: the model's judgment over one site. -/
def asyncRowB (g : RevL.A1Async.Graph) (as : List String) (fuel : Nat)
    (k : RevL.A1Async.Kind) (heads : List String) : Bool :=
  RevL.A1Async.siteB g as fuel k heads

/-- **The `A1` verdict is the model's rule.** -/
theorem asyncRowB_iff (g : RevL.A1Async.Graph) (as : List String) (fuel : Nat)
    (k : RevL.A1Async.Kind) (heads : List String) :
    asyncRowB g as fuel k heads = true ↔ RevL.A1Async.SiteOK g as fuel k heads :=
  RevL.A1Async.siteB_iff g as fuel k heads

/-- **Signature decider**: a provide method's colour against its service's. -/
def sigRowB (declared impl : Bool) : Bool := RevL.A1Async.sigB declared impl

theorem sigRowB_iff (declared impl : Bool) :
    sigRowB declared impl = true ↔ RevL.A1Async.SigOK declared impl :=
  RevL.A1Async.sigB_iff declared impl

end AsyncColour

/-! ## Deciding the three declaration rules (issue #1809)

`PS` rows are the activation body's statements in order, as preludes and
actions; `IT` rows are `intercept` targets; `MC` rows are the service
operations a component names. The verdicts are `RevL.Prelude.preludeB`,
`interceptB` and `methodB`, each with a bridge. -/

section PreludeRules

structure PSRow where
  path : String
  comp : String
  ord : Nat
  kind : String

structure ITRow where
  path : String
  comp : String
  key : String

structure MCRow where
  path : String
  comp : String
  svc : String
  meth : String

def parsePS (f : List String) : Option PSRow :=
  match f with
  | ["PS", path, comp, ord, kind] => ord.toNat?.map fun o => ⟨path, comp, o, kind⟩
  | _ => none

def parseIT (f : List String) : Option ITRow :=
  match f with
  | ["IT", path, comp, key] => some ⟨path, comp, key⟩
  | _ => none

def parseMC (f : List String) : Option MCRow :=
  match f with
  | ["MC", path, comp, svc, meth] => some ⟨path, comp, svc, meth⟩
  | _ => none

def parsePreludeStep : String → Option RevL.Prelude.Step
  | "prelude" => some .prelude
  | "action" => some .action
  | _ => none

def preludeRowB (steps : List RevL.Prelude.Step) : Bool := RevL.Prelude.preludeB steps

theorem preludeRowB_iff (steps : List RevL.Prelude.Step) :
    preludeRowB steps = true ↔ RevL.Prelude.PreludeOK steps :=
  RevL.Prelude.preludeB_iff steps

def interceptRowB (provides requires targets : List String) : Bool :=
  RevL.Prelude.interceptB provides requires targets

theorem interceptRowB_iff (provides requires targets : List String) :
    interceptRowB provides requires targets = true ↔
      RevL.Prelude.InterceptOK provides requires targets :=
  RevL.Prelude.interceptB_iff provides requires targets

def methodRowB (table calls : List RevL.Prelude.Op) : Bool := RevL.Prelude.methodB table calls

theorem methodRowB_iff (table calls : List RevL.Prelude.Op) :
    methodRowB table calls = true ↔ RevL.Prelude.MethodOK table calls :=
  RevL.Prelude.methodB_iff table calls

end PreludeRules

/-! ## Deciding G-MODEL-PLACE (issue #1811)

`MP` rows are the placed roles of a component's route arms, judged by
`RevL.ModelPlace.placeB` against the `MO` confidentiality origins. `ME` rows
are the roles a component consults and `MRC` rows each role's reach, judged
by the same proved `attenuatesB` the spawn `W` row uses, over the
component's held set. -/

section ModelPlacement

structure MPRow where
  path : String
  comp : String
  origin : String
  residence : String

structure MERow where
  path : String
  comp : String
  role : String

structure MRCRow where
  path : String
  role : String
  cap : String

def parseMP (f : List String) : Option MPRow :=
  match f with
  | ["MP", path, comp, _action, origin, _role, res] => some ⟨path, comp, origin, res⟩
  | _ => none

def parseMO (f : List String) : Option (String × String) :=
  match f with
  | ["MO", path, origin] => some (path, origin)
  | _ => none

def parseME (f : List String) : Option MERow :=
  match f with
  | ["ME", path, comp, role] => some ⟨path, comp, role⟩
  | _ => none

def parseMRC (f : List String) : Option MRCRow :=
  match f with
  | ["MRC", path, role, cap] => some ⟨path, role, cap⟩
  | _ => none

def parseResidence : String → Option RevL.ModelPlace.Residence
  | "on_device" => some .onDevice
  | "off_device" => some .offDevice
  | _ => none

def placeRowB (conf : List String) (arms : List RevL.ModelPlace.Placed) : Bool :=
  RevL.ModelPlace.placeB conf arms

theorem placeRowB_iff (conf : List String) (arms : List RevL.ModelPlace.Placed) :
    placeRowB conf arms = true ↔ RevL.ModelPlace.PlaceOK conf arms :=
  RevL.ModelPlace.placeB_iff conf arms

/-- **Model-reach decider**: the role's reach within the component's held
set, by the spawn rule's own decider. -/
def modelReachB (held reach : List Cap) : Bool := attenuatesB held reach

theorem modelReachB_iff (held reach : List Cap) :
    modelReachB held reach = true ↔ Attenuates held reach :=
  attenuatesB_iff held reach

/-- The `MPV` and `MAV` rows of one file. Kept out of `main` so its
elaboration stays within the default heartbeat budget. -/
structure ModelRows where
  mo : List (String × String)
  mp : List MPRow
  me : List MERow
  mrc : List MRCRow
  unknown : List String

/-- The model-placement rows, parsed once; `unknown` names the files with an
`MP` row of a residence the model has no constructor for. -/
def parseModelRows (fields : List (List String)) : ModelRows :=
  let mp := fields.filterMap parseMP
  { mo := fields.filterMap parseMO, mp := mp, me := fields.filterMap parseME,
    mrc := fields.filterMap parseMRC,
    unknown := (mp.filter (fun r => (parseResidence r.residence).isNone)).map (·.path) }

def modelVerdicts (p : String) (rows : ModelRows)
    (held : CapMap) (capTable : CapTable) : String := Id.run do
  let mut out := ""
  let conf := (rows.mo.filter (fun r => r.1 == p)).map (·.2)
  let pmp := rows.mp.filter (fun r => r.path == p)
  for cn in (pmp.map (·.comp)).eraseDups do
    let arms := (pmp.filter (fun r => r.comp == cn)).filterMap
      (fun r => (parseResidence r.residence).map fun res => (⟨r.origin, res⟩ : RevL.ModelPlace.Placed))
    let mpv := if placeRowB conf arms then "ok" else "fail"
    out := out ++ s!"MPV\t{p}\t{cn}\tplace={mpv}\n"
  let pmrc := rows.mrc.filter (fun r => r.path == p)
  for e in rows.me.filter (fun r => r.path == p) do
    let reachCaps := ((pmrc.filter (fun r => r.role == e.role)).map (·.cap)).filterMap
      (capOf capTable)
    let heldCaps := (lookupCaps held e.comp).filterMap (capOf capTable)
    let mav := if modelReachB heldCaps reachCaps then "ok" else "fail"
    out := out ++ s!"MAV\t{p}\t{e.comp}\t{e.role}\treach={mav}\n"
  return out

end ModelPlacement

/-! ## Deciding G-COUNCIL-SPLIT (issue #1811)

`CV` rows are the councils a file DECLARES, each carrying the tie outcome the
checker reads for it: the exporter resolves `on_tie`'s `split` default, so the
model judges the outcome and not the spelling of its absence. They are judged
by `RevL.ModelCouncil.splitB`. A file declaring no council emits no row, so a
council refusal this row does not decide (a missing aggregation, a bad
vocabulary, a member rule) files under `out-of-fragment` rather than being
read as an agreement the row cannot make. -/

section ModelCouncil

structure CVRow where
  path : String
  council : String
  tie : String

def parseCV (f : List String) : Option CVRow :=
  match f with
  | ["CV", path, council, tie] => some ⟨path, council, tie⟩
  | _ => none

def councilRowB (councils : List RevL.ModelCouncil.CouncilDecl) : Bool :=
  RevL.ModelCouncil.splitB councils

theorem councilRowB_iff (councils : List RevL.ModelCouncil.CouncilDecl) :
    councilRowB councils = true ↔ RevL.ModelCouncil.SplitOK councils :=
  RevL.ModelCouncil.splitB_iff councils

/-- The `CTV` rows of one file. Kept out of `main` so its elaboration stays
within the default heartbeat budget. -/
structure CouncilRows where
  cv : List CVRow

def parseCouncilRows (fields : List (List String)) : CouncilRows :=
  { cv := fields.filterMap parseCV }

def councilVerdicts (p : String) (rows : CouncilRows) : String := Id.run do
  let mut out := ""
  for r in rows.cv.filter (fun r => r.path == p) do
    let ctv := if councilRowB [⟨r.council, r.tie⟩] then "ok" else "fail"
    out := out ++ s!"CTV\t{p}\t{r.council}\ttie={ctv}\n"
  return out

end ModelCouncil

/-! ### The `TAINT` row — G9 / G-SECRET-FLOW on the corpus (issue #1811 group 2)

`TAINT` carries the sink the shipped checker itself DISCOVERED and the label
that arrived there, plus the naming chain and hop count it reported. The row
decides `RevL.Lemmas.Admits` at that sink on that label, through
`RevL.G9Flow`. It is **the rule on the corpus, not the coverage of the
checker's walk** (see `RevL/Theorems/G9Flow.lean`): the row's premises ARE
the checker's own refusal, so a checker that stops reporting its discovered
sink produces no row at all, and `diff_corpus.py` files that refusal under
the fatal `missed-G9` rather than reading it as an agreement. A class or an
origin name `RevL.G9Flow` does not carry yields `none` here, which is
exactly that same failure. -/

section G9Flow

/-- One `TAINT` row. `cls` and `origins` are the two columns the rule reads;
`code`, `category`, `kind`, `steps` and `hops` are carried so the row's own
output shows what the checker discovered and how far it walked. -/
structure G9Row where
  path : String
  code : String
  category : String
  cls : String
  kind : String
  origins : String
  steps : String
  hops : String

def parseG9 (f : List String) : Option G9Row :=
  match f with
  | ["TAINT", path, code, category, cls, kind, origins, steps, hops] =>
      some ⟨path, code, category, cls, kind, origins, steps, hops⟩
  | _ => none

/-- The row's verdict: the rule at the sink class the checker reported, on
the label the checker reported. `false` for a class or an origin name the
model does not carry, so an unreadable row can never print `ok`. -/
def rowB (r : G9Row) : Bool :=
  match RevL.G9Flow.sinkOfClass r.cls with
  | none => false
  | some k =>
      match RevL.G9Flow.labelOfString r.origins with
      | none => false
      | some ℓ => RevL.G9Flow.g9RowB k ℓ

/-- `rowB` is exactly the rule at a carried sink class and a carried label,
and `false` — never vacuously `true` — when either is not carried. -/
theorem rowB_iff (r : G9Row) :
    rowB r = true ↔ ∃ k ℓ, RevL.G9Flow.sinkOfClass r.cls = some k ∧
      RevL.G9Flow.labelOfString r.origins = some ℓ ∧ RevL.Lemmas.Admits k ℓ := by
  unfold rowB
  cases hk : RevL.G9Flow.sinkOfClass r.cls with
  | none => simp
  | some k =>
      cases hl : RevL.G9Flow.labelOfString r.origins with
      | none => simp
      | some ℓ => simp [RevL.G9Flow.g9RowB_iff k ℓ]

def g9RowB (rows : List G9Row) : Bool := rows.all rowB

theorem g9RowB_iff (rows : List G9Row) :
    g9RowB rows = true ↔ ∀ r ∈ rows, ∃ k ℓ,
      RevL.G9Flow.sinkOfClass r.cls = some k ∧
        RevL.G9Flow.labelOfString r.origins = some ℓ ∧
        RevL.Lemmas.Admits k ℓ := by
  unfold g9RowB
  rw [List.all_eq_true]
  exact forall_congr' fun r => imp_congr_right fun _ => rowB_iff r

/-- The `TAINT` rows of one file. Kept out of `main` so its elaboration stays
within the default heartbeat budget. -/
structure G9Rows where
  rows : List G9Row

def parseG9Rows (fields : List (List String)) : G9Rows :=
  { rows := fields.filterMap parseG9 }

def g9Verdicts (p : String) (rs : G9Rows) : String := Id.run do
  let mut out := ""
  for r in rs.rows.filter (fun r => r.path == p) do
    let v := if rowB r then "ok" else "fail"
    out := out ++ s!"TAINT\t{p}\t{r.cls}\tlabel={r.origins}\thops={r.hops}\t{v}\n"
  return out

end G9Flow

/-! ### The `RETAIN` row — G-RETAIN on the corpus (issue #1811 group 3)

`RETAIN` carries the persistence scope and the sink the shipped checker itself
DISCOVERED, the naming chain it walked to it, and the two instants it
COMPARED: the policy's deadline (`until`) and the evaluation instant (`now`),
both as whole seconds since the Unix epoch — the checker normalizes every
instant to UTC before comparing (`retention.parse_instant`), so an integer is
a faithful encoding of what it compared and one the kernel can reduce, which
ISO text is not. The row decides `RevL.GRetain.rowB` at those columns. It is
**the rule on the corpus, not the coverage of the checker's walk** (see
`RevL/Theorems/GRetain.lean`): the row's premises ARE the checker's own
refusal, so a checker that stops reporting its discovered sink produces no row
at all, and `diff_corpus.py` files that refusal under the fatal
`missed-G-RETAIN` rather than reading it as an agreement. A scope
`RevL.GRetain` does not carry, or an instant that does not parse, yields
`false` here — never a vacuous `ok`. -/

section GRetain

/-- One `RETAIN` row. `scope`, `sink`, `chain`, `deadline` and `now` are the
columns the rule reads; `code`, `category`, `policy` and `hops` are carried so
the row's own output shows what the checker discovered and how far it walked. -/
structure GRetainRow where
  path : String
  code : String
  category : String
  scope : String
  sink : String
  policy : String
  deadline : String
  now : String
  chain : String
  hops : String

def parseGRetain (f : List String) : Option GRetainRow :=
  match f with
  | ["RETAIN", path, code, category, scope, sink, policy, deadline, now,
     chain, hops] =>
      some ⟨path, code, category, scope, sink, policy, deadline, now, chain,
            hops⟩
  | _ => none

/-- The row's verdict: the rule at the scope, the sink, the chain and the two
instants the checker reported. `false` for a scope the model does not carry or
an instant that does not parse, so an unreadable row can never print `ok`. -/
def retainRowB (r : GRetainRow) : Bool :=
  match r.deadline.toNat?, r.now.toNat? with
  | some dl, some nw => RevL.GRetain.rowB r.scope r.sink r.chain dl nw
  | _, _ => false

/-- `retainRowB` is exactly the rule at the carried columns, and `false` —
never vacuously `true` — when an instant does not parse. -/
theorem retainRowB_iff (r : GRetainRow) :
    retainRowB r = true ↔ ∃ dl nw, r.deadline.toNat? = some dl ∧
      r.now.toNat? = some nw ∧
        RevL.GRetain.rowB r.scope r.sink r.chain dl nw = true := by
  unfold retainRowB
  cases hd : r.deadline.toNat? with
  | none => simp
  | some dl =>
      cases hn : r.now.toNat? with
      | none => simp
      | some nw => simp

def gretainRowB (rows : List GRetainRow) : Bool := rows.all retainRowB

theorem gretainRowB_iff (rows : List GRetainRow) :
    gretainRowB rows = true ↔ ∀ r ∈ rows, ∃ dl nw,
      r.deadline.toNat? = some dl ∧ r.now.toNat? = some nw ∧
        RevL.GRetain.rowB r.scope r.sink r.chain dl nw = true := by
  unfold gretainRowB
  rw [List.all_eq_true]
  exact forall_congr' fun r => imp_congr_right fun _ => retainRowB_iff r

/-- The `RETAIN` rows of one file. Kept out of `main` so its elaboration stays
within the default heartbeat budget. -/
structure GRetainRows where
  rows : List GRetainRow

def parseGRetainRows (fields : List (List String)) : GRetainRows :=
  { rows := fields.filterMap parseGRetain }

def gretainVerdicts (p : String) (rs : GRetainRows) : String := Id.run do
  let mut out := ""
  for r in rs.rows.filter (fun r => r.path == p) do
    let v := if retainRowB r then "ok" else "fail"
    out := out ++ s!"RETAIN\t{p}\t{r.scope}\t{r.sink}\tpolicy={r.policy}\tuntil={r.deadline}\tnow={r.now}\tchain={r.chain}\thops={r.hops}\t{v}\n"
  return out

end GRetain

/-! ### The `INV` row — the G4 inverse rule on the corpus (issue #2097)

`INV` carries the acquisition, the inverse verb the checker DEMANDED, the
spelling its own advice told the author to write, and the `undo` the checker
actually READ at the site. It decides `RevL.G4Inverse.rowB` at those columns —
the rule that a host acquisition's `undo` is its family's release
(`lower._check_host_release`, issue #1859), that an `extern acquire`'s `undo`
is the call its declaration named (`lower._check_extern_release`), and that a
host write's `undo` is its table entry on the same receiver and key
(`lower._method_effect_inverse`, issue #1945).

It is **the rule on the corpus, not the coverage of the checker's walk** (see
`RevL/Theorems/G4Inverse.lean`): the row's premises ARE the checker's own
refusal, so a checker that stopped reporting an acquisition's inverse produces
no row at all, and `diff_corpus.py` files that refusal under the fatal
`missed-G4` rather than reading it as an agreement. An arm this row does not
carry, or an acquisition its tables do not name, yields `false` here — never a
vacuous `ok`. -/

section G4Inverse

/-- One `INV` row. `arm`, `acq`, `verb`, `inv` and `site` are the columns the
rule reads; `code` and `category` are carried so the row's own output shows
which refusal it was exported from. -/
structure G4InvRow where
  path : String
  code : String
  category : String
  arm : String
  acq : String
  verb : String
  inv : String
  site : String

def parseG4Inv (f : List String) : Option G4InvRow :=
  match f with
  | ["INV", path, code, category, arm, acq, verb, inv, site] =>
      some ⟨path, code, category, arm, acq, verb, inv, site⟩
  | _ => none

/-- The row's verdict: the rule at the arm, the acquisition, the demanded verb
and spelling, and the site's `undo`. `false` for an arm the model does not
carry, so an unreadable row can never print `ok`. -/
def g4InvRowB (r : G4InvRow) : Bool :=
  match RevL.G4Inverse.kindOfString r.arm with
  | some k => RevL.G4Inverse.rowB k r.acq r.verb r.inv r.site
  | none => false

/-- `g4InvRowB` is exactly the rule at the carried columns, and `false` —
never vacuously `true` — for an arm the model does not carry. -/
theorem g4InvRowB_iff (r : G4InvRow) :
    g4InvRowB r = true ↔ ∃ k, RevL.G4Inverse.kindOfString r.arm = some k ∧
      RevL.G4Inverse.rowB k r.acq r.verb r.inv r.site = true := by
  unfold g4InvRowB
  cases hk : RevL.G4Inverse.kindOfString r.arm with
  | none => simp
  | some k => simp

def g4invRowB (rows : List G4InvRow) : Bool := rows.all g4InvRowB

theorem g4invRowB_iff (rows : List G4InvRow) :
    g4invRowB rows = true ↔ ∀ r ∈ rows, ∃ k,
      RevL.G4Inverse.kindOfString r.arm = some k ∧
        RevL.G4Inverse.rowB k r.acq r.verb r.inv r.site = true := by
  unfold g4invRowB
  rw [List.all_eq_true]
  exact forall_congr' fun r => imp_congr_right fun _ => g4InvRowB_iff r

/-- The `INV` rows of one file. Kept out of `main` so its elaboration stays
within the default heartbeat budget. -/
structure G4InvRows where
  rows : List G4InvRow

def parseG4InvRows (fields : List (List String)) : G4InvRows :=
  { rows := fields.filterMap parseG4Inv }

def g4invVerdicts (p : String) (rs : G4InvRows) : String := Id.run do
  let mut out := ""
  for r in rs.rows.filter (fun r => r.path == p) do
    let v := if g4InvRowB r then "ok" else "fail"
    out := out ++ s!"INV\t{p}\t{r.arm}\t{r.acq}\tverb={r.verb}\tinv={r.inv}\tsite={r.site}\t{v}\n"
  return out

end G4Inverse

-- ---------------------------------------------------------------- main

/-- Build the model's component from an `M` row. `realm` is the
component's own `isolate` map, defaulting to `sharedRealm` — `lower._realm`
exactly. A ROUTED requirement (`PR` row,
`isolate k in realms(...)`) is elided: the linker resolves it per leg
against `provider_of[(k, leg)]` and never through the single-realm table,
and `LComponent.realm` places a key in one realm, so spelling it here would
put the requirement in the shared realm — where a Router that also
`provides k` would read as a phantom G3 self-provision. Elided, like the
requirements `localComposition` elides, not supplied. -/
def toLComponent (r : MRow) (routed : List String) : LComponent :=
  { name := r.name, requires := r.requires.filter (fun k => !routed.contains k),
    provides := r.provides,
    realm := fun k =>
      match r.realms.find? (·.1 == k) with
      | some (_, rl) => rl
      | none => sharedRealm }

/-- BU verdicts (G6 binding uniqueness, issue #1812), one per scope: the
model's rule over the scope's seed names and its ordered events. -/
def bindingVerdicts (p : String) (berows : List BERow) : String := Id.run do
  let mut out := ""
  let pbe := berows.filter (fun r => r.path == p)
  for k in (pbe.map (fun r => (r.comp, r.scope))).eraseDups do
    let mine := (pbe.filter (fun r => r.comp == k.1 && r.scope == k.2)).mergeSort
      (fun a b => a.ord <= b.ord)
    let seed := (mine.filter (fun r => r.kind == "seed")).map (·.name)
    let evs := mine.filterMap beEvent
    let buv := if bindingRowB seed evs then "ok" else "fail"
    out := out ++ s!"BU\t{p}\t{k.1}\t{k.2}\tbinding={buv}\n"
  return out

/-- G1 verdicts (declared access, issue #1807), one per component: the
model's rule over its declared requirements and its access roots. -/
def accessVerdicts (p : String) (fm : List MRow) (garows : List GARow) : String := Id.run do
  let mut out := ""
  let pga := garows.filter (fun r => r.path == p)
  for m in fm do
    let roots := (pga.filter (fun r => r.comp == m.name)).map (·.root)
    let g1v := if accessRowB m.requires roots then "ok" else "fail"
    out := out ++ s!"G1\t{p}\t{m.name}\taccess={g1v}\n"
  return out

/-- A1 verdicts (async colour, issue #1808), one per site and one per
provide method's signature. -/
def asyncVerdicts (p : String) (fnrows : List FNRow) (anrows : List ANRow)
    (asrows : List ASRow) (agrows : List AGRow) : String := Id.run do
  let mut out := ""
  let graph : RevL.A1Async.Graph :=
    (fnrows.filter (fun r => r.path == p)).map (fun r => (r.name, r.calls))
  let anames := (anrows.filter (fun r => r.path == p)).map (·.name)
  for r in asrows.filter (fun r => r.path == p) do
    match parseSiteKind r.kind with
    | some k =>
      let a1v := if asyncRowB graph anames (graph.length + 1) k r.heads
        then "ok" else "fail"
      out := out ++ s!"A1\t{p}\t{r.comp}\t{r.ord}\tasync={a1v}\n"
    | none => pure ()
  for r in agrows.filter (fun r => r.path == p) do
    let sv := if sigRowB (r.declared == "async") (r.impl == "async")
      then "ok" else "fail"
    out := out ++ s!"A1S\t{p}\t{r.comp}\t{r.meth}\tsig={sv}\n"
  return out

/-- PL / IC / MS verdicts (issue #1809), one each per component. -/
def declVerdicts (p : String) (fm : List MRow) (brows : List BRow)
    (psrows : List PSRow) (itrows : List ITRow) (mcrows : List MCRow) : String := Id.run do
  let mut out := ""
  let table : List RevL.Prelude.Op :=
    (brows.filter (fun r => r.path == p)).map (fun r => (r.svc, r.meth))
  let pps := psrows.filter (fun r => r.path == p)
  let pit := itrows.filter (fun r => r.path == p)
  let pmc := mcrows.filter (fun r => r.path == p)
  for m in fm do
    let steps := ((pps.filter (fun r => r.comp == m.name)).mergeSort
      (fun a b => a.ord <= b.ord)).filterMap (fun r => parsePreludeStep r.kind)
    let plv := if preludeRowB steps then "ok" else "fail"
    out := out ++ s!"PL\t{p}\t{m.name}\tprelude={plv}\n"
    let targets := (pit.filter (fun r => r.comp == m.name)).map (·.key)
    let icv := if interceptRowB m.provides m.requires targets then "ok" else "fail"
    out := out ++ s!"IC\t{p}\t{m.name}\tintercept={icv}\n"
    let calls := (pmc.filter (fun r => r.comp == m.name)).map (fun r => (r.svc, r.meth))
    let msv := if methodRowB table calls then "ok" else "fail"
    out := out ++ s!"MS\t{p}\t{m.name}\tmethod={msv}\n"
  return out

def main (args : List String) : IO UInt32 := do
  match args with
  | [inPath, outPath] =>
    let text ← IO.FS.readFile inPath
    let fields := (text.splitOn "\n").filter (fun l => l != "")
      |>.map (·.splitOn "\t")
    let mrows := fields.filterMap parseM
    let urows := fields.filterMap parseU
    let brows := fields.filterMap parseB
    let qrows := fields.filterMap parseQ
    let arows := fields.filterMap parseA
    let frows := fields.filterMap parseF
    let krows := fields.filterMap parseK
    let srows := fields.filterMap parseS
    let xrows := fields.filterMap parseX
    let pbrows := fields.filterMap parsePB
    let prrows := fields.filterMap parsePR
    let harows := fields.filterMap parseHA
    let drrows := fields.filterMap parseDR
    let cfrows := fields.filterMap parseCF
    let cnrows := fields.filterMap parseCN
    let irows := fields.filterMap parseI
    let pgrows := fields.filterMap parsePG
    let exrows := fields.filterMap parseEX
    let fnrows := fields.filterMap parseFN
    let erows := fields.filterMap parseE
    let jrows := fields.filterMap parseJ
    let lrecs := fields.filterMap parseL
    let lruns := fields.filterMap parseLRun
    let lfails := fields.filterMap parseLFail
    let aqrows := fields.filterMap parseAQ
    let arrows' := fields.filterMap parseAR
    let axrows := fields.filterMap parseAX
    let berows := fields.filterMap parseBE
    let garows := fields.filterMap parseGA
    let anrows := fields.filterMap parseAN
    let asrows := fields.filterMap parseAS
    let agrows := fields.filterMap parseAG
    let psrows := fields.filterMap parsePS
    let mrows' := parseModelRows fields
    let crows := parseCouncilRows fields
    let g9rows := parseG9Rows fields
    let retainrows := parseGRetainRows fields
    let invrows := parseG4InvRows fields
    let itrows := fields.filterMap parseIT
    let mcrows := fields.filterMap parseMC
    let aerows := fields.filterMap parseAE
    -- An edge with no crossing, or two edges on one crossing, is a malformed
    -- export: refuse it rather than read one of them.
    for e in aerows do
      if !(axrows.any fun x => x.path == e.path && x.comp == e.comp && x.ord == e.ord) then
        IO.eprintln s!"oracle: AE row without a crossing: {e.path} {e.comp} {e.ord}"
        return 1
      if (aerows.filter fun x => x.path == e.path && x.comp == e.comp && x.ord == e.ord).length > 1 then
        IO.eprintln s!"oracle: two AE rows on one crossing: {e.path} {e.comp} {e.ord}"
        return 1
    let capTable := buildCapTable (fields.filterMap parseZ) (fields.filterMap parseY)
    -- A capability with no decomposition row would silently become the
    -- bare token; refuse instead.
    let allCaps := ((arows.map (·.cap)) ++ (frows.map (·.cap))
                    ++ (frows.map (·.bound)) ++ (krows.map (·.cap))).eraseDups
    let missing := allCaps.filter (fun c => (capOf capTable c).isNone)
    if !missing.isEmpty then
      IO.eprintln s!"oracle: capability rows without a Z decomposition: {missing}"
      return 1
    let paths := (mrows.map (·.path)).eraseDups
    let mut out := ""
    for x in xrows do
      out := out ++ s!"X\t{x.path}\trefused={x.code}\n"
    -- D rows (G7 teardown disposition), one per scenario. The stack is the
    -- scenario's `E` rows IN FILE ORDER, which is registration order; the
    -- label rides in the model's own `inverse` slot so `teardown` transports
    -- it. An unknown kind or verdict is a hard error, never a silently empty
    -- stack.
    for j in jrows do
      match parseVerdict j.verdict with
      | none =>
          IO.eprintln s!"oracle: unknown teardown verdict {j.verdict} for {j.scen}"
          return 1
      | some v =>
        let mine := erows.filter (fun r => r.scen == j.scen)
        let kinds := mine.map (fun r => parseKind r.kind)
        if kinds.any (·.isNone) then
          IO.eprintln s!"oracle: unknown entry kind in scenario {j.scen}"
          return 1
        let log : List LogEntry :=
          mine.filterMap fun r =>
            (parseKind r.kind).map fun k => { kind := k, inverse := .lit r.ord }
        out := out ++ s!"D\t{j.scen}\treplayed={csv (replayedLabels v log)}\t" ++
          s!"discharged={csv (dischargedLabels v log)}\t" ++
          s!"stranded={csv (strandedLabels v log)}\n"
    -- O rows (A8/R4 crash recovery), one per scenario. The records are the
    -- scenario's `L` rows IN FILE ORDER, which is the WAL's append order, read
    -- straight into the model's own `Rec`. An `L` row of no known shape is a
    -- HARD error: a silently dropped record moves the verdict.
    let lrows := fields.filter isLRow
    if lrows.length != lrecs.length + lruns.length + lfails.length then
      IO.eprintln "oracle: malformed L row in the crash-recovery corpus"
      return 1
    for scen in lruns do
      let log : Log := (lrecs.filter (·.1 == scen)).map (·.2)
      let failing : List Seq := (lfails.filter (·.1 == scen)).map (·.2)
      let ok := okOf failing
      -- `reported` models the ROLL-BACK path's residue surface and R4 is stated
      -- under `outcome L = .rolledBack`, so the column says `n/a` rather than
      -- claiming a surface the model does not have.
      let residue :=
        if outcome log = .rolledBack then csv (reportedSeqLabels log ok) else "n/a"
      out := out ++ s!"O\t{scen}\toutcome={outcomeName (outcome log)}\t" ++
        s!"replayed={csv (replayedSeqLabels log)}\tresidue={residue}\n"
    -- A `DR` row whose scope or position the model has no constructor for
    -- would drop a reach silently and move the verdict: refuse instead.
    if !mrows'.unknown.isEmpty then
      IO.eprintln s!"oracle: MP rows of unknown residence: {mrows'.unknown}"
      return 1
    let unknownPS := psrows.filter (fun r => (parsePreludeStep r.kind).isNone)
    if !unknownPS.isEmpty then
      IO.eprintln s!"oracle: PS rows of unknown step kind: {unknownPS.map (·.path)}"
      return 1
    let unknownAS := asrows.filter (fun r => (parseSiteKind r.kind).isNone)
    if !unknownAS.isEmpty then
      IO.eprintln s!"oracle: AS rows of unknown site kind: {unknownAS.map (·.path)}"
      return 1
    let unknownBE := berows.filter (fun r => !beKnown r)
    if !unknownBE.isEmpty then
      IO.eprintln s!"oracle: BE rows of unknown kind: {unknownBE.map (·.path)}"
      return 1
    let unknownDR := drrows.filter (fun r => (reachOf r).isNone)
    if !unknownDR.isEmpty then
      IO.eprintln s!"oracle: DR rows of unknown scope or position: {unknownDR.map (·.path)}"
      return 1
    for p in paths do
      let fm := mrows.filter (fun r => r.path == p)
      let ub := brows.filter (fun r => r.path == p)
      let uq := qrows.filter (fun r => r.path == p)
      let ua := arows.filter (fun r => r.path == p)
      let uf := frows.filter (fun r => r.path == p)
      let uk := krows.filter (fun r => r.path == p)
      let us := srows.filter (fun r => r.path == p)
      let uu := urows.filter (fun r => r.path == p)
      let uha := harows.filter (fun r => r.path == p)
      let upb := pbrows.filter (fun r => r.path == p)
      let upr := prrows.filter (fun r => r.path == p)
      let ems : List (String × String) :=
        (ub.filter (fun b => b.mode != "plain")).map (fun b => (b.svc, b.meth))
      let bounds : List (String × String × String × List String) :=
        ub.map fun b =>
          (b.svc, b.meth, b.mode,
           (uq.filter (fun q => q.svc == b.svc && q.meth == b.meth)).map (·.entry))
      -- The static composition: spawn TEMPLATES are runtime instances, not
      -- composition members, and `lower._link` excludes them from the G2/G3
      -- table for exactly that reason.
      let routedOf := fun (cn : String) =>
        (upr.filter (fun b => b.comp == cn)).map (·.key)
      let comps := (fm.filter (fun r => !r.isTemplate)).map
        (fun r => toLComponent r (routedOf r.name))
      let dv := if decide (ProvidesDisjoint comps) then "ok" else "fail"
      let cv := if decide (RequiresClosed comps) then "ok" else "fail"
      let lv := if linkVerdict comps then "ok" else "fail"
      out := out ++ s!"V\t{p}\tdisjoint={dv}\tclosed={cv}\tlink={lv}\n"
      let aPairs := ua.map (fun r => (r.comp, r.cap))
      let fPairs := uf.map (fun r => (r.comp, r.cap))
      let owns := groupCaps (aPairs ++ fPairs)
      -- Held = own reach + every capability the `requires` bindings grant.
      let held := uk.foldl (fun acc r => upsertCaps acc r.comp [r.cap]) owns
      let edges := (us.map (fun r => (r.parent, r.child))).eraseDups
      let closed := closeN (edges.length + 1) edges owns
      -- G verdicts (marker rule) per component
      for cn in fm.map (·.name) do
        let markerOK := g4OK ems (uu.filter (fun r => r.comp == cn))
        let acquireOK := hostAcquireOK (uha.filter (fun r => r.comp == cn))
        let gv := if markerOK && acquireOK then "ok" else "fail"
        out := out ++ s!"G\t{p}\t{cn}\tg4={gv}\n"
      -- A9 verdicts (issues 1167 / #1172): every installed provide block's
      -- key is declared in the clause, and every declared key is installed
      -- by a block or a `realms(...)` route. One row per component that
      -- declares or installs anything (a component with neither would agree
      -- vacuously); the clause comes off the M row's `LComponent`, the
      -- blocks off the PB rows, the routes off the PR rows, and the verdict
      -- is the model's `a9B` (`a9RowB_iff`). Templates included:
      -- `lower._lower_provide` runs on a spawn target's body too.
      for r in fm do
        let blocks := (upb.filter (fun b => b.comp == r.name)).map (·.key)
        let routed := routedOf r.name
        if !blocks.isEmpty || !r.provides.isEmpty then
          let av := if a9RowB (toLComponent r routed) blocks routed then "ok" else "fail"
          out := out ++ s!"A9\t{p}\t{r.name}\ta9={av}\n"
      -- CD verdicts (G4 config-is-data) per DECLARED config field, not per
      -- component: a file's externs declare config too and are held to the
      -- same bar, and naming the field is what makes a refusal legible.
      for cf in cfrows.filter (fun r => r.path == p) do
        let ns := cnrows.filter (fun r => r.path == p && r.kind == cf.kind
                                  && r.owner == cf.owner && r.field == cf.field)
        let cdv := if configDataOK ns then "ok" else "fail"
        out := out ++ s!"CD\t{p}\t{cf.kind}\t{cf.owner}\t{cf.field}\tdata={cdv}\n"
      -- P verdicts (provide-method bound) per method reach group
      let fkeys := (uf.map (fun r => (r.comp, r.key, r.svc, r.meth))).eraseDups
      for k in fkeys do
        let caps := (uf.filter (fun r => r.comp == k.1 && r.key == k.2.1
                                && r.svc == k.2.2.1 && r.meth == k.2.2.2))
                    |>.map (·.bound) |>.eraseDups
        let ok := methodBoundOK capTable bounds k.2.2.1 k.2.2.2 caps
        let pv := if ok then "ok" else "fail"
        out := out ++ s!"P\t{p}\t{k.1}\t{k.2.1}\t{k.2.2.1}\t{k.2.2.2}\tbound={pv}\n"
      -- MPV / MAV verdicts (G-MODEL-PLACE, issue #1811)
      out := out ++ modelVerdicts p mrows' held capTable
      -- CTV verdicts (G-COUNCIL-SPLIT, issue #1811)
      out := out ++ councilVerdicts p crows
      -- TAINT verdicts (G9 / G-SECRET-FLOW on the corpus, issue #1811 group 2)
      out := out ++ g9Verdicts p g9rows
      -- RETAIN verdicts (G-RETAIN on the corpus, issue #1811 group 3)
      out := out ++ gretainVerdicts p retainrows
      -- INV verdicts (the G4 inverse rule on the corpus, issue #2097)
      out := out ++ g4invVerdicts p invrows
      -- W verdicts (spawn attenuation) per edge
      for e in edges do
        let childCaps := (lookupCaps closed e.2).filterMap (capOf capTable)
        let heldCaps := (lookupCaps held e.1).filterMap (capOf capTable)
        let av := if attenuatesB heldCaps childCaps then "ok" else "fail"
        out := out ++ s!"W\t{p}\t{e.1}\t{e.2}\tatten={av}\n"
      -- C verdicts (G6 confinement, issue 276), one per reconstructed
      -- statement. The declared context is the component's require locals (M)
      -- together with the roots its require-held caps bind (K); a statement is
      -- confined iff every reconstructed head reaches a declared root. `ctx`
      -- lifts those roots to head granularity — the component's own heads whose
      -- root is declared — so `confinedB ctx (stmtOf r)` decides
      -- `∀ k ∈ stmtHeads (stmtOf r), k ∈ ctx`, faithful by `confinedB_iff`.
      let pi := irows.filter (fun r => r.path == p)
      for cn in (pi.map (·.comp)).eraseDups do
        let reqs := match fm.find? (fun r => r.name == cn) with
          | some r => r.requires
          | none => []
        let kbinds := (uk.filter (fun r => r.comp == cn)).map (·.bindn)
        let declaredRoots := (reqs ++ kbinds).eraseDups
        let cis := pi.filter (fun r => r.comp == cn)
        let compHeads := (cis.flatMap (fun r => r.heads ++ r.inverse)).eraseDups
        let ctx := compHeads.filter (fun h => declaredRoots.contains (rootOf h))
        for r in cis do
          let cv := if confinedB ctx (stmtOf r) then "ok" else "fail"
          out := out ++ s!"C\t{p}\t{cn}\t{r.index}\tconfined={cv}\n"
      -- S8 / U5 verdicts (G8 boundary surface, G5 teardown purity; issue
      -- 276), decided over the file's reconstructed `Prog`. The extern table
      -- and fn call graph come off the `EX`/`FN` rows, the fold's `fuel` off
      -- the `PG` row (`fns.length`, enough to reach the reach fixed point).
      -- A statement whose heads reach a first-class-dispatch `star` fn is
      -- `n/a`: that shape is outside the model, so neither side folds it.
      let pex := exrows.filter (fun r => r.path == p)
      let pfn := fnrows.filter (fun r => r.path == p)
      let prog := progOf pex pfn
      let fuel := match pgrows.find? (fun r => r.path == p) with
        | some r => r.fuel
        | none => pfn.length
      let tainted := starTainted pfn
      for r in pi do
        let s := stmtOf r
        -- S8: the boundary surface of every reconstructed statement.
        if (RevL.Typing.stmtHeads s).any (fun h => tainted.contains h) then
          out := out ++ s!"S8\t{p}\t{r.comp}\t{r.index}\tsurface=n/a\n"
        else
          out := out ++ s!"S8\t{p}\t{r.comp}\t{r.index}\t" ++
            s!"surface={csv (stmtSurfaceB prog fuel s).eraseDups}\n"
        -- U5: the teardown registration count of an effect's inverse body.
        if r.kind == "effect" then
          if r.inverse.any (fun h => tainted.contains h) then
            out := out ++ s!"U5\t{p}\t{r.comp}\t{r.index}\tteardown=n/a\n"
          else
            let n := registrationsB prog fuel (bodyOfHeads r.inverse)
            out := out ++ s!"U5\t{p}\t{r.comp}\t{r.index}\tteardown={n}\n"
      -- A2 verdicts (no acquisition after a provision, issue 1166), one per
      -- component, decided by the model's own fold over the body's ordered
      -- steps — the component's `AQ` rows, sorted by body index. An unknown
      -- step kind is a hard error, never a silently shorter body.
      let uaq := aqrows.filter (fun r => r.path == p)
      for cn in fm.map (·.name) do
        let mine := sortByOrd (uaq.filter (fun r => r.comp == cn))
        if mine.any (fun r => (parseStep r.kind).isNone) then
          IO.eprintln s!"oracle: unknown body step kind in {p} {cn}"
          return 1
        let steps := mine.filterMap (fun r => parseStep r.kind)
        let av := if a2OKB steps then "ok" else "fail"
        out := out ++ s!"A2\t{p}\t{cn}\ta2={av}\n"
      -- DF verdict (G4 deferred position, issue #1742), one per file: the
      -- model's rule over every reach of a `deferred` extern the file makes.
      let reaches := (drrows.filter (fun r => r.path == p)).filterMap reachOf
      let dfv := if deferredOKB reaches then "ok" else "fail"
      out := out ++ s!"DF\t{p}\tdeferred={dfv}\n"
      -- AP verdicts (the approval floor, issue #1455), one per marked
      -- crossing that reaches a token: the model's `crossingB` over its `AX`
      -- tokens, its `AE` edge and the file's `AR` required tokens.
      let required := (arrows'.filter (fun r => r.path == p)).map (·.token)
      let pax := axrows.filter (fun r => r.path == p)
      let pae := aerows.filter (fun r => r.path == p)
      for k in (pax.map (fun r => (r.comp, r.ord))).eraseDups do
        let toks := (pax.filter (fun r => r.comp == k.1 && r.ord == k.2)).map (·.token)
        let edge := (pae.find? (fun r => r.comp == k.1 && r.ord == k.2)).map (·.scope)
        let apv := if approvalRowB required toks edge then "ok" else "fail"
        out := out ++ s!"AP\t{p}\t{k.1}\t{k.2}\tapproval={apv}\n"
      -- BU, G1, A1/A1S and PL/IC/MS verdicts (issues #1812, #1807, #1808,
      -- #1809), in helpers so `main` stays within the elaboration budget.
      out := out ++ bindingVerdicts p berows
      out := out ++ accessVerdicts p fm garows
      out := out ++ asyncVerdicts p fnrows anrows asrows agrows
      out := out ++ declVerdicts p fm brows psrows itrows mcrows
    IO.FS.writeFile outPath out
    return 0
  | _ =>
    IO.eprintln "usage: Oracle.lean <corpus.tsv> <verdicts.tsv>"
    return 1

end RevLOracle

-- `lean --run` needs a root-level `main`.
def main (args : List String) : IO UInt32 :=
  RevLOracle.main args

/-! ## The axioms gate over the oracle's bridge theorems

`formal/CheckAxioms.lean` covers the `RevL` library. This file is not part
of it: `lakefile.lean`'s `lean_lib` has `roots := #[`RevL]`, and
`scripts/layering_gate.py` walks `formal/RevL/` only, so until these lines
existed the theorems that make the differential oracle *bite* — the ones
`STATUS.md` cites when it says "the Lean side `decide`s the proved model"
— were the only proofs in `formal/` outside every gate. A `sorry` here
would have left `lake env lean --run` at exit 0 and the gate green.

`scripts/run_gate.sh` elaborates this file a second time without `--run`
and feeds the block below to `scripts/axioms_gate.py`. Each of these is a
`... = true ↔ <model predicate>` bridge: the decision procedure the oracle
runs, proved equivalent to the judgment the theorems are about. -/

#print axioms RevLOracle.heads_exprOfHeads
#print axioms RevLOracle.confinedB_iff
#print axioms RevLOracle.bodyNames_bodyOfHeads
#print axioms RevLOracle.stmtSurfaceB_iff
#print axioms RevLOracle.registrationsB_iff
#print axioms RevLOracle.linkOKB_iff
#print axioms RevLOracle.pathPrefixB_iff
#print axioms RevLOracle.pleqB_iff
#print axioms RevLOracle.mem_keys_of_lookupV
#print axioms RevLOracle.lookupV_isSome_of_mem_keys
#print axioms RevLOracle.coversB_iff
#print axioms RevLOracle.resourceOKB_iff
#print axioms RevLOracle.ceilingOKB_iff
#print axioms RevLOracle.attenuatesB_iff
#print axioms RevLOracle.mem_replayedLabels_iff
#print axioms RevLOracle.mem_dischargedLabels_iff
#print axioms RevLOracle.mem_strandedLabels_iff
#print axioms RevLOracle.row_is_total
#print axioms RevLOracle.replayedLabels_phase_order
#print axioms RevLOracle.outcomeName_inj
#print axioms RevLOracle.mem_replayed_iff
#print axioms RevLOracle.mem_reported_iff
#print axioms RevLOracle.replayedSeqLabels_eq
#print axioms RevLOracle.reportedSeqLabels_eq
#print axioms RevLOracle.replayedSeqLabels_nil_of_not_rolledBack
#print axioms RevLOracle.discharged_not_reissued
#print axioms RevLOracle.fenced_not_reissued
#print axioms RevLOracle.declared_idempotent_reissued
#print axioms RevLOracle.reportedSeqLabels_nil_iff_clean
#print axioms RevLOracle.a9RowB_iff
#print axioms RevLOracle.parseStep_stepName
#print axioms RevLOracle.a2OKB_iff
#print axioms RevLOracle.deferredOKB_iff
#print axioms RevLOracle.approvalRowB_iff
#print axioms RevLOracle.bindingRowB_iff
#print axioms RevLOracle.accessRowB_iff
#print axioms RevLOracle.asyncRowB_iff
#print axioms RevLOracle.sigRowB_iff
#print axioms RevLOracle.preludeRowB_iff
#print axioms RevLOracle.interceptRowB_iff
#print axioms RevLOracle.methodRowB_iff
#print axioms RevLOracle.placeRowB_iff
#print axioms RevLOracle.modelReachB_iff
#print axioms RevLOracle.councilRowB_iff
#print axioms RevLOracle.g9RowB_iff
#print axioms RevLOracle.retainRowB_iff
#print axioms RevLOracle.gretainRowB_iff
#print axioms RevLOracle.g4InvRowB_iff
#print axioms RevLOracle.g4invRowB_iff
