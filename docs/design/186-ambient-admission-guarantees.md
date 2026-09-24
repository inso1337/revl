# 186: ambient admission, the guarantee story, and what is deferred

**Roadmap:** item 186 (Path C, folds 419c/419f) · **Issue:** #86 · **Landed:** slices 1-2 in docs/design/186-ambient-admission.md (`admit_ambient(src, manifest)`: G2 and multi-realm routes against a provisions-only wire) · **Status:** DECISION, 2026-09-08. Lower priority; one bounded slice now, the replacement wave deferred behind named preconditions.

## The decision

**Yes, a component may be admitted against the ambient running manifest, and
the self-host gate must be able to say so.** This is not new behaviour for
revl: the reference already does it on every `revl_load`, `swap` and per-turn
`admit` (`compile_files(paths, manifest=running, replacing=[...])`,
`src/revl/compiler.py`: ambient services in scope, G2/G3 spanning both, a
same-named component implicitly replacing the running one). What is missing is
parity in `selfhost/lower.rvl`, and parity matters for one reason: the native
gate (`crates/revl-gate`, the wasm edge gate, the polyglot mesh of 337) is what
a receiving tier re-admits against, and a gate that can only answer "does this
one text admit" cannot serve hot-swap at a seam. Ambient admission is what makes
337's "re-admit at the receiver against its local running manifest" a sentence
about the native gate rather than the py one.

The shape stays `admit_ambient(src: Str, manifest: Str) -> Str`. The wire grows
by tagged row kinds rather than by a third argument, so the crate signature the
gate digests does not move.

What is deferred, with preconditions: replacement (hot-swap, `replacing`), the
`handoff` state contract, and the differential oracle that constructs a running
manifest on both sides. Each is designed below so the deferred wave builds
against a decision, not a blank.

## The guarantee story under ambient admission

Let `M` be the running manifest, `X` the incoming text, `R ⊆ M` the components
being replaced (empty today).

| guarantee | under ambient admission |
|---|---|
| G1 declared access | intrinsic to `X`. The manifest contributes only which keys exist and which service each carries (`services` in the ambient), never a reach the text did not declare |
| G2 provision disjointness | over `(key, realm)` in `provisions(X) ∪ provisions(M \ R)`. Landed for `R = ∅`. A conflict is reported against the incoming component, byte-identical to the in-text wording |
| G3 acyclicity | over the union graph: `M`'s provider-to-consumer edges plus `X`'s. NOT landed: the wire carries no requirements, so a cycle that closes through `M` is not seen. Slice 3 below |
| G4, G5, G6, G8 | intrinsic to `X`; the manifest cannot add or remove an effect form, a mode, or an extern |
| G7 | unchanged for `X`; for `R`, the withdrawal runs the replaced component's own LIFO teardown, which is what makes replacement expensive and is why it is a wave |
| G9 taint | checked over `X`'s bodies with the running providers' declared return qualifiers taken from the ambient `services`; a running component's own sinks are not re-walked, because its text was admitted with its own flow and is unchanged |
| multi-realm routes (162) | a route may target a realm provided only by `M`. Landed (slice 2) |
| handoff (53) | the replacement's accepted state type must be compatible with the replaced component's exported type under the §5 relation. Landed on both sides (wave part 2) |
| the halted state (443) | admission against a halted composition is refused; the reference's session already refuses `load`/`swap` after a halt, and the self-host gate refuses a manifest whose header row says `halted` (below) |

The base invariant that every slice keeps: `admit_ambient(src, "") ==
admit_src(src)` exactly, and a manifest holding only provision rows behaves
exactly as slices 1-2 do today.

## Replacement semantics (decided now, implemented in the wave)

- `replacing` is a set of running component names carried IN the manifest
  string (row kind `-C`), so `R` is a property of the admission call, never
  something the incoming text can assert about itself.
- G2 runs against `M \ R`. A component in `X` whose name matches one in `M` is
  implicitly in `R`, mirroring `compile_files`.
- **A replacement that would leave a live consumer unmet is refused by
  default.** If `R` provided `(k, r)` and some component in `M \ R` requires
  `k` in `r`, then `X` must provide `(k, r)`, otherwise the refusal names the
  consumer and the lost key. This is item 460's rule (a documented refusal with
  retained ownership beats an apparently successful replacement) and the
  row-level rule already in docs/composition-rows.md (a provision removed
  upstream is a refusal). There is no opt-in to strand consumers in this wave;
  a composition that wants a key gone unloads the consumer first.
- `handoff`: for each `(k, r)` in `R` with an exported state type `E`, the
  replacement's `handoff k: A` must satisfy `compatible(E, A)` in the value-flow
  direction docs/state-handoff.md fixes; a missing `handoff` on the replacement
  when `R` exported one is a refusal naming the dropped state.
- Atomicity is the runtime's: the gate only answers. The reference's `swap`
  boots, admits, re-points and then tears down (docs/swap.md); the native gate
  returns the same verdict string so a receiving tier can refuse before booting.

## The wire

Rows joined by `;`. Kind by the leading character:

```
C/k/r        provision: component C provides key k in realm r ("" = shared)   (landed)
C<k          requirement: component C requires key k                           (slice 3)
C<k/r        requirement: the same, resolved in realm r                        (wave 1)
C<*k         requirement: the same, multi-realm bound (item 162)               (wave 1)
C>k/r,r      route: the realms C binds k across, in declaration order          (#1036)
-C           replacing: component C is withdrawn by this admission             (wave 1)
C=k:T        handoff: C exports state of type T at key k                       (wave 2)
!halted      header: the composition is halted; every admission refuses       (slice 3)
!services    header: the `:S` rows are the WHOLE running service set          (item 346)
:S           service: the running composition declares service S              (item 346)
:S,a,b       service: ... and its operations are exactly `a` and `b`          (457 T4b)
:S,a(k:Str)  service: ... and `a`'s declared parameters are exactly those    (issue #346)
```

Provision rows are exactly today's; a manifest of provision rows parses as
before. `parse_manifest` becomes `parse_manifest_rows` over the tagged kinds
and unknown kinds refuse by name rather than being skipped (a row that parses
and does nothing is worse than one that refuses).

The SERVICE BLOCK (issue #346) is the one kind the fold accepts and computes no
COMPOSITION edge from, and the rule above is what decides that it may: a service
declaration contributes no provision, no requirement, no graph node and no
withdrawable component, so there is nothing for G2/ROUTE/G3 or the
unmet-consumer check to fold it into at any wire. What it does decide is
RESOLUTION: the name says whether a candidate's `requires k: S` resolves at all,
and the operation list a `:S,a,b` row carries says what that requirement offers,
which is what lets the fold refuse a call to an operation the running service
does not declare (`A6`, docs/design/457 T4b). The comma is the claim - a bare
`:S` says nothing about the surface and decides no member, the same
silence-is-not-emptiness rule the header carries for the names.

An operation token may carry its own declared PARAMETER LIST behind it
(`:S,a(k:Str|n:Int)`, issue #346), which is the same claim one level further
down and is what lets a call through a requirement be TYPED against the running
declaration rather than only resolved against it: `store.bump(key)` on a running
`bump(n: Int)` is refused `T1` in the reference's own sentence. The bracket is
the claim there, so `a()` is the empty parameter list while a bare `a` says
nothing about the arguments and leaves the rule silent. The renderer WITHHOLDS a
list it cannot spell without one of the wire's own structural characters
(`Map[Str, Int]` carries the operation separator), so a token that arrives
carrying one is a garbled row and refuses the wire by name rather than being
read as a shorter parameter list. Return types, emission and async markings are
NOT on the wire: the G4 and A1 arms read those, and an arm answering from a
declaration nobody sent is the wave-through this block exists to avoid. `-C` and `C=k:T` are the
contrast: each of them CHANGES what the fold must compute, which is why one is
folded in full and the other still refuses. A malformed `:S` name is held to the
same bare-identifier rule a `-C` name is, and refuses the same way.

Its consumer is the rust crate's admission certifier
(`crates/revl-gate/src/admission.rs`), which parses the wire itself and needs the
running service NAMES to tell a fresh interface from a redeclaration of a running
one: the reference gates the second on the compatibility relation of §5
(`revl.admission._admit_service_replacement`, reached only when the declared name
is already in the ambient service table) and admits the first outright.

The `!services` HEADER carries the whole weight. Without it the wire makes no
claim about the running services, so a reader must treat the set as UNKNOWN, not
empty: reading an absent block as "the composition declares nothing" would admit
a redeclaration the reference refuses, which is the wave-through the gate exists
to prevent. The block sits between the composition rows and the withdrawal rows,
because it describes the composition and a withdrawal acts on what precedes it,
so `-C` stays last and the provision/requirement rows keep their exact positions
and order. That leaves the G3 DFS seed order (`mnames`, which a `:S` row does not
touch) provably unchanged.

Because `selfhost/lower.rvl` is a digest input of the gate crate, this slice
regenerates BOTH `tools/build_gate_crate.py` and `tools/build_gate_wasm.py`
outputs and runs `--check` on each.

## The differential oracle

Two oracles, one per regime:

- **Oracle A (no replacement, landed shape).** `admit_ambient(X, wire(M)) ==
  admit_src(M ++ X) == reference(M ++ X)`. Slice 3 extends it to G3: a cycle
  that closes through `M` refuses identically on all three legs.
- **Oracle B (replacement, the wave).** The equivalence above breaks, so both
  sides must derive the running manifest from ONE artifact. Compile `M` on the
  reference to `IR(M)`; a new projection `revl.manifest_wire(ir) -> str` in
  `src/revl/` renders `IR(M)` to the wire above; then
  `admit_ambient(X, manifest_wire(IR(M)) + ";-R...")` is compared to the first
  `TAG|message` of `compile_files([X], manifest=IR(M), replacing=R)`. The
  reference side needs a thin wrapper that returns that string; nothing else on
  the reference changes. This is the "construct a running manifest on both
  sides" the roadmap asked for, and it is byte-agreement on the verdict, in
  keeping with the self-host oracle discipline.

## Slices and preconditions

| slice | delivers | precondition |
|---|---|---|
| **3 (now)** | requirement rows on the wire; G3 over the union graph in `link_refusals(pg, seed)`; the `!halted` header; `manifest_wire(ir)` projection; oracle A extended to G3 and pinned in `tests/test_selfhost_lower.py`; gate crate and wasm gate regenerated | none beyond the regen |
| **wave, part 1** (landed) | `-C` rows, G2 against `M \ R`, the unmet-consumer refusal on both the reference and the native gate, oracle B | the reference wrapper returning the first verdict string |
| **wave, part 2** (landed) | `C=k:T` rows and the handoff compatibility check | none, as it turned out: both shapes reach the gate as declared SPELLINGS and the reference compares them with NO declared-type table, so the port is the type-STRING algebra alone |
| **wave, part 3** (landed) | 419c closure: line-ordered collecting of ambient refusals against internal ones for the multi-refusal corpus (slice 2 ordered the single-conflict case) | part 1. Part 2's hand-off verdict later took its own place in the same sink, immediately ahead of the withdrawal |

Slice 3 is small, needs no type layer, and closes the one guarantee hole that
is a soundness gap today (a cycle hidden by the manifest). The wave was deferred
until the type layer lands, and this note is the spec it landed against — the
part-2 note below records where that precondition turned out to be wrong.

### Landed since this note was written

**Slice 3 (2026-09-08 to 2026-09-11).** Requirement rows, G3 over the union
graph, and the `manifest_wire(ir)` projection (`src/revl/manifest.py`).

**The unmet-consumer refusal, reference side (2026-09-13).** The rule under
"Replacement semantics" above is now enforced by the reference gate itself, not
only predicted by `revl plan`: `compile_files(X, manifest=M, replacing=R)`
refuses when `R` provided `(k, r)`, some retained component of `M \ R` requires
`k` in `r`, and `X` does not provide `(k, r)`. The refusal names the withdrawn
provider, the lost key with its realm, and the retained consumer, and it
classifies as `(G2, admission)` like every other admission rejection.
`src/revl/admission.py` holds the check (`_admit_provision_withdrawal`),
`src/revl/compiler.py` supplies the withdrawn half of the ambient view
(`ambient["withdrawn"]`, the running entries this admission drops), and
`tests/test_withdrawn_provision_admission.py` pins both directions.

Only the transition met to unmet is refused. A requirement that was already
unmet before the admission stays admissible, because an incremental composition
legitimately admits a consumer before its provider. A routed key is left to the
link-time per-realm provider check of item 162, so one loss is never reported
twice. Re-providing the key in a different realm does not satisfy a
shared-realm consumer, which is the case a realm-blind check would have wrongly
admitted.

**Wave part 1, the native gate and oracle B (2026-09-14).** `-C` rows on the
wire, G2/ROUTE/G3 against `M \ R`, the same unmet-consumer refusal inside
`selfhost/lower.rvl`'s `admit_ambient`, and the differential that builds the
running manifest on both sides.

The wire grows three shapes, all backward compatible (a slice-3 wire renders
and parses byte for byte as before):

```
-C           replacing: component C is withdrawn by this admission
C<k/r        requirement: the same as C<k, resolved in realm r
C<*k         requirement: the same as C<k, multi-realm bound (item 162)
```

A requirement row carried no realm before, which was enough for the G3 edge it
existed for and is not enough for a per-(key, realm) unmet-consumer check: a
consumer isolated into realm `r` would have read as a shared-realm one and the
loss would have been missed. The routed marker keeps a multi-realm bind out of
the single-realm table, so a stray shared-realm provider cannot shadow a route's
own targets and the withdrawal check leaves a routed loss to the item-162 check,
exactly as the reference does. `manifest_wire(ir, replacing=R)` renders all
three, so the wire says what `compile_files(paths, manifest=IR(M), replacing=R)`
says.

`R` reaches the gate two ways, mirroring `compile_files`: the explicit `-C`
rows, and the implicit replacement a component performs by redeclaring a running
name. A `-C` row that does not name a bare component is a garbled wire and
refuses, as an unknown row kind does.

Oracle B, the exit test the roadmap asked item 186 for: the reference compiles
`M` to `IR(M)`, `manifest_wire(IR(M), replacing=R)` renders the wire, and the
first `TAG|message` of `compile_source(X, manifest=IR(M), replacing=R)` is
compared byte for byte with `admit_ambient(X, wire)`. It covers plain
replacement, an unmet consumer reached implicitly and explicitly, a
realm-separated re-provision, a realm-separated non-conflict, an already-unmet
requirement, and a withdrawal of the consumer itself. It lives in
`tests/test_selfhost_lower.py`; the gate's own in-language cases live in
`selfhost/lower.rvl` and ride into `crates/revl-gate` with the generated crate.

Wave part 2 closed the remaining row; see below.

**Wave part 2, the `C=k:T` handoff row and state compatibility (2026-09-15).**
The last surface the wave held back. The wire grows one kind:

```
C=k:T        handoff: the state shape the running component C exports at key k
```

`manifest_wire` renders it off the WHOLE IR document's `components`, whose
`handoff` field survives lowering — the same place `compiler._running_handoffs`
reads, so the gate sees exactly the ambient table the reference's own check
sees. It is a per-component COMPOSITION row: it rides with its component, after
that component's requirement and route rows and ahead of the service block, so
the provision and requirement positions are untouched and the G3 DFS seed order
(`mnames`) is unchanged. A composition that declares no `handoff` renders
byte-identically to before.

`admit_ambient` now runs the reference's own check. For each incoming component
declaring `handoff k: A`, if some running provider exports state at `k` with
shape `E`, then `A` must accept everything an `E` produces — the covariant
`compatible(expected=A, actual=E)` of §5, pointed at state — and a successor
that cannot hold the predecessor's state is refused in the reference's exact
words, classified `(G2, admission)`. A key nothing running exported starts COLD
and a successor that declares no `handoff` opts out: neither is a conflict, on
either side. The running table is NOT filtered by the withdrawn set, because the
provider being replaced is precisely the one whose exported state the
replacement must accept.

**The precondition this note named did not hold.** Part 2 was deferred behind
"the self-host type layer (457): `compatible` cannot be ported to `lower.rvl`
before the types it compares exist there". That was the wrong reading of the
dependency. Both shapes reach the gate as declared SPELLINGS — the running one
on the wire, the incoming one off the component's own annotation — and
`admission._handoff_compatible` calls `compatible(accepted, exported)` with NO
declared-type table. So the only algebra to port is the type-STRING one, which
`ty_parse`/`ty_render` already were; `ho_compatible` is the `types=None` reading
of `typecheck.compatible`, head for head. The nominal-record resolution that
would need a table is off this path entirely, and the STRUCTURAL-record branch
is unreachable: `Parser.type_` refuses a `{` at an annotation position, so
`handoff st: {a: Int}` is a parse error on the reference and never a comparison.
What genuinely needs 457 is the §5 relation on a redeclared SERVICE
(`_admit_service_replacement`), which compares declared method signatures; that
one stays open, and the gate is a no-objection on it.

Where it sits in the sink (part 3's question, asked again for the new producer):
`check_and_lower` collects `_admit_handoff_replacement` immediately BEFORE
`_admit_provision_withdrawal`, both after the component loop and ahead of the
spawn bounds, the attenuation walk and `_link`'s BOOT count. `collect_nonlink`
takes the two spliced in that order, so a hand-off drift and a stranded consumer
that land on ONE line are broken the same way by `seq` on both sides. Measured
over the ordering corpus in three line layouts (hand-off against the withdrawal,
a G4 spawn-emission bound, the BOOT count, and a component's own G4, in both
orders plus two triples): 30 admissions, all agreeing, and the splice is
load-bearing — reversing the two producers reds seven of them.

Which components it runs over (issue #1127): `_admit_handoff_replacement` is
handed `live_components`, which `check_and_lower` builds by dropping every
component whose body lowering raised. A raise there is caught, recorded, and
replaced by a `poisoned` header stub that keeps `_link`'s topology complete, so
the component is still linked but is no longer walked by any body-reading
post-pass. A component that refuses in its body therefore contributes NO
hand-off verdict on the reference: the body refusal is the whole answer.

`collect_nonlink` carries the same set. It records every component its loop
refused over and `handoff_refusals` skips those, which is the only place the
gate needs it — the withdrawal runs over the FULL list on both sides, for the
reason stated above it. Without the set the gate ranked a `G2` hand-off drift,
anchored at the COMPONENT line, ahead of an inline body refusal that `body_line`
anchors at the offending STATEMENT, so `pick_min` named the hand-off where
`check_and_lower` named the body. A whole-component aggregate verdict (the G4
emission-reach one) is anchored at the component line, ties, and was saved by
`seq`, which is why the ordering corpus above did not catch it.

The skip can only remove a verdict from a component the sink already refuses
over, so its failure direction is the same fail-CLOSED one: it narrows which of
two true refusals gets NAMED and can never turn a refusal into an admission.
`test_a_poisoned_component_contributes_no_handoff_verdict` measures the pair
against the reference, and the two controls beside it pin that the suppressed
verdict is real (repair the body and the drift is what both sides name) and that
the skip is per COMPONENT rather than per admission (a poisoned sibling does not
suppress a clean component's drift).

Failure direction: fail-CLOSED. Every refusal here is an admission that does not
happen; the running composition keeps running with its state where it is. The
non-vacuity controls pin that it does not refuse everything: an identical shape,
a WIDENED accepted shape (`Opt[Str]` accepting an exported `Str`), a numeric
widening, an elementwise container widening, a cold key and a successor
declaring no hand-off all still admit — and stripping the `C=k:T` rows from a
rendered wire reproduces the admission the gate used to give, which is what pins
which row closed it.

On the ADMISSION surface (`crates/revl-gate/src/admission.rs`) the row is READ
and counted as nothing, exactly as a route row is: its key is already on the
wire as its component's own provision row, and the fold is what compares the
shapes. Reading it is the point — a row that surface cannot parse declines the
WHOLE wire, so leaving handoff rows out would have silently withheld the
admission arm from every STATEFUL running composition. It is read AHEAD of the
route row on both surfaces, because the type field is the one place on the wire
carrying an arbitrary type spelling and a function type spells `->`. The type
itself is not validated against the wire-name rule: it is a spelling, and the
only thing that may judge it is the relation that compares it.

`tests/test_selfhost_lower.py` carries oracle B over the row (a 13-case corpus
of `(M, X, R)` triples, the ordering corpus, and the controls);
`selfhost/lower.rvl` carries the in-language twins, which ride into
`crates/revl-gate` with the generated crate.

**Route rows, and the routed realm loss (2026-09-14, issue #1036).** The wire
grows one more kind:

```
C>k/r1,r2    route: the realms the running component C binds key k across
```

The requirement row's `*` marker said a running consumer's key was ROUTED; this
row says WHERE. Without it the gate could not ask the question item 162 answers,
so a routed running consumer whose realm lost its provider was refused by the
reference and ADMITTED by the gate: the running consumer stranded with nothing
said anywhere. `admit_ambient` now runs the per-realm provider check over the
running composition's routes, in the order `_link` walks its entries (the
ambient ones first), and reports under **item 162's own tag and message**. The
withdrawal check still skips a routed key: it answers "this key became unmet",
this one answers "this realm has no provider", and a refusal carrying a name
that does not describe it teaches the reader the wrong rule.

The legs are edges as well as an existence check. The reference builds one
provider-to-consumer edge per routed realm for an ambient entry too, so a cycle
that closes through a routed running consumer is seen now where the legless wire
contributed no edge at all.

Failure direction: fail-CLOSED. Every refusal here is an admission that does not
happen; the running composition keeps running, its routed consumer still bound
to the providers it has. The scope is exactly the reference's — a realm named by
a route row with no provider in the resulting per-(key, realm) table — and the
non-vacuity controls in oracle B pin that a legitimate routed replacement (same
name, same key, same realm) and a routed key nothing withdraws both still admit.
A garbled route row refuses by name, as a garbled withdrawal row does, rather
than parsing into a route with no legs.

The row costs wire budget like any other: `MANIFEST_ROW_LIMIT` counts
`;`-separated segments, not kinds, so a wire of route rows over the bound is an
`outside_frontier` decline ahead of the fold, never an admission
(`crates/revl-gate`'s `route_rows_past_the_bound_are_declined_and_not_admitted_into`).
On the admission surface a route row is READ and counted as nothing:
`manifest_shape` validates its component, key and realm labels, and leaves the
counts to the `C<*k` requirement row, which already carries the by-key
obligation. Reading it is the point — a row that surface cannot parse declines
the WHOLE wire, so leaving route rows out would have silently withheld the
admission arm from every routed composition, which is the shape of the bug the
`C<*k` and `C<k/r` spellings had there until #1044 read them.

Where the row sits, on the combined wire: a route row is a COMPOSITION row, so
it rides with its component, after that component's requirement rows and ahead
of the item-346 service block, which describes the whole composition; the
withdrawal rows stay last, because a withdrawal acts on everything before it.
The provision and requirement rows keep their exact positions, so the G3 DFS
seed order (`mnames`) is unchanged. A rendered wire therefore reads, in full:

```
StoreA/kv/r1;StoreB/kv/r2;Router/api/;Router<*kv;Router>kv/r1,r2;!services;:Kv,get;:Api,go
```

**Wave part 3, the 419c refusal ordering (2026-09-14).** When an ambient
admission carries several true refusals, both sides now name the same one.

Item 419c's single-source half landed with the collecting sink: `admit_src`
reports the minimum by `(line, seq)`, which is the reference's
`diagnostics[0]`. The ambient half is the same question with one more producer
in the sink. The unmet-consumer WITHDRAWAL is neither an internal component
refusal nor a link refusal, and `seq` (the append order) is what breaks a LINE
TIE. `check_and_lower` collects `_admit_provision_withdrawal` immediately after
the component loop and AHEAD of `_check_spawn_emission_bounds`,
`_check_spawn_attenuation` and `_link`, whose head decides the BOOT count. The
gate appended its withdrawal verdicts after the whole non-link sink instead,
which put them behind the spawn bounds and the boot count. `collect_nonlink`
now takes the ambient verdicts as an argument and splices them at the
reference's own position; `admit_src` passes an empty list, so the
single-source path is byte-identical.

MEASURED, in the style of the item-391 passes: over 660 generated
multi-refusal ambient admissions (ten refusal families, pairs and random
triples/quadruples, each in three line layouts), 12 diverged before the splice
and 0 after, in exactly two families:

```
a withdrawal tying with a G4 spawn-emission bound
  reference  G2|this admission withdraws the running provider of `db` ...
  gate       G4|`Sup.run` is declared plain, but it spawns `Worker`, which emits through `kv2`

a withdrawal tying with the item-350 BOOT count
  reference  G2|this admission withdraws the running provider of `db` ...
  gate       BOOT|a composition declares at most one `boot` component, found B1, B2
```

A tie is not only the degenerate whole-program-on-one-line case the
single-source corpus uses: two components written on ONE source line tie as
well, which is the `glued` layout in the test.

Ambient ROUTE (item 162 over a running composition) and ambient G3 needed no
move. Both are `_link` refusals on both sides, so they already sat at the same
position relative to everything else, and both are anchored at
`_link`'s `lines.get(name, 1)` default for an entry the incoming text does not
declare, which is what `comp_line` returns for the same name.

Failure direction: unchanged, fail-CLOSED. Nothing here changes WHICH
admissions are refused; every program in the corpus is refused by both sides
before and after, and only the NAME of the reported refusal moves. The rule
kept is the one part 1 set when it declined to report a routed loss under the
withdrawal tag: a refusal must carry the tag that describes the finding, so an
admission's effect on the running composition is not reported under a spawn
bound's or a boot count's name because the two landed on one line. The
non-vacuity controls pin that the withdrawal did not move to the HEAD of the
sink, only to its own place in it: an earlier-LINE spawn bound still outranks a
later-line withdrawal, and a component's own G4 still wins a TIE with one,
because the component loop runs ahead of the withdrawal on both sides.

`tests/test_selfhost_lower.py` carries the oracle-B extension (the corpus in
three layouts, the two closed families with their bytes, and the controls);
`selfhost/lower.rvl` carries the in-language twins, which ride into
`crates/revl-gate` with the generated crate.

## Relation to the other decisions in this batch

- The `host` rows of docs/design/457-endpoint-one-definition.md are ambient
  provisions from the runtime's point of view: `Host/server/` and `Host/auth/`
  are provision rows in the wire, and a component `requires auth: Auth` admits
  against them through exactly this entry point. No new mechanism.
- A halted composition (docs/design/443-estop-tier-contract.md) is a manifest
  with the `!halted` header, so the native gate refuses admission against it
  for the same reason the py session does.

## Exit tests

1. **G3 through the manifest.** Running `A provides a requires b`; admit `B
   provides b requires a`. Refused, naming the cycle, byte-identical on
   `admit_ambient(B, "A/a/;A<b")`, `admit_src(A ++ B)` and the reference.
   Without the requirement row (today's wire) the same admission wrongly
   admits; the test pins that the row is load-bearing.
2. **Base invariant.** `admit_ambient(src, "")` and a provisions-only manifest
   are byte-identical to before slice 3 across the existing corpus.
3. **Halted header.** `admit_ambient(X, "!halted;A/a/")` refuses every `X`,
   naming the halt.
4. **Unknown row kind.** `admit_ambient(X, "?A/a/")` refuses naming the row.
5. **A routed running consumer's lost realm (#1036).** `M` runs `StoreA` (`kv`
   in `r1`), `StoreB` (`kv` in `r2`) and a `Router` routing `kv` across both;
   `X` redeclares `StoreB` without the provision. Both sides refuse
   `ROUTE|multi-realm bind of `kv` in Router names realm `r2` ...`. Stripping
   the route rows from the wire reproduces the admission the gate used to give,
   which pins which row closed it.
6. **Oracle B (the wave's exit).** For a corpus of `(M, X, R)` triples covering
   plain replacement, an unmet consumer, a realm-separated non-conflict and a
   handoff mismatch, the first verdict string agrees byte-for-byte between the
   reference wrapper and `admit_ambient` over `manifest_wire(IR(M))`. MET: the
   replacement and unmet-consumer cases landed with wave part 1, the realm
   cases with the route rows, and the handoff cases with wave part 2.
