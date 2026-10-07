# revl formal backbone — proof status

Rules of the layering. Roadmap item 418 recorded that this line used to
read "enforced by imports, not by hope" while nothing checked it;
`scripts/layering_gate.py` checks it now, and runs as the first step of
`scripts/run_gate.sh`:

- **L0** (`RevL.Syntax`, `RevL.Typing`, `RevL.Semantics`,
  `RevL.Manifest`, `RevL.Boundary` — the last two say so in their own
  headers) is architect-owned and frozen. Worker sessions do not edit it;
  a needed L0 change blocks on the architect.
- **L1** (`RevL.Lemmas.*`) is the lemma farm. Farm files import L0 only
  (ideally core only) and never each other.
- **L2** (`RevL.Theorems.G*`) is one file per guarantee, one session per
  file. Worker files import L0/L1, **never each other**. Theorem names go
  into `CheckAxioms.lean` the moment they are stated.
- A theorem counts as *proved* only if the axioms gate passes for it:
  no `sorryAx`, no project-defined axiom. Lean's three standard
  foundation axioms (`propext`, `Classical.choice`, `Quot.sound`) are
  whitelisted; anything else fails `make formal`. (`#print axioms` on a
  `sorry`'d declaration reports `sorryAx`, so an unfinished proof can
  never pass.)
- A theorem also needs a row in `scripts/nonvacuity.tsv` naming the
  concrete evidence that its hypotheses can all hold at once (roadmap item
  418, step 8). The axioms gate cannot tell a load-bearing theorem from a
  vacuous one: `#print axioms` is just as clean on a theorem whose
  hypotheses are unsatisfiable. `scripts/nonvacuity_gate.py` fails on a
  registered theorem with no row, on a witness that is not itself
  registered, on a self-witnessing row, and on a registered theorem this
  file does not name — so a proof and the record of it cannot drift
  apart. Two rows are marked `contentless` rather than witnessed,
  because they are true by definition
  rather than by any property of their subject; the gate prints both on
  every run so they cannot be quietly counted as proof.

## What the layer covers, and what it does not

The honest map, per guarantee code (`src/revl/diagnostics.py`'s
`GUARANTEES`). The table below the map lists every proved theorem; this is
the part a reader wants first, because a guarantee with no row here is a
guarantee this layer buys nothing for. "Oracle" is whether the
differential harness (`harness/diff_corpus.py`) compares the model's
verdict against the shipped checker on the corpus — a proved theorem with
no oracle row is checked against the *paper*, not against `src/revl`.

| Code | Formal status | Theorems | Oracle | The gap, and what kind of gap it is |
|---|---|---|---|---|
| **G1** declared access | partial | 2 + 6 | **yes** (one G1 row per component; 21 agree-G1) | `declared_only_access` is real and witnessed, but its content is the shape of `Typed`/`ReachIn`: it says an undeclared access cannot be *written*, not that the checker *visits* every statement of a real component body. **Modelling limit** — L0 grew component bodies in issue #2108, so the shape is expressible at last, but this theorem still does not prove the visiting half: what does is `RevL.G9Coverage.coversBodyB`, stated over a body's own scopes and decided on the corpus by the `GC` row, and that is G9's row rather than this one. Since issue #1807 the `G1` row decides `RevL.G1Access.AccessOK` over each component's ACCESS roots (`GA`): every call head's root and every name read in value position, at every nesting depth (after and inside `if`/`else`/`while`/`for`/guard blocks, in conditions), less the roots the checker resolves without a requirement, plus `intercept` targets. So the visiting half is measured against the checker on the corpus, through the exporter's walk, while the classification of a root as local, callable, import, host family or constructor is the exporter's and component-wide, not per scope |
| **G2** provision disjointness | full | 4 | **yes** (V rows, 2 agree-G2) | stated over `(key, realm)` slots from the incremental `LinkOK`, and the oracle bites: change `Manifest.needs` to ignore the realm and four corpus files mismatch |
| **G3** acyclic dependencies | full | 9 | **yes** (V rows, 1 agree-G3) | the layering certificate is *derived* from `LinkOK`, so nothing is assumed. No known gap |
| **G4** inverse-or-emit | full over the lattice; the shape-level statement is weak and marked | 2 + 7 + 10 | **yes** (182 G rows, 25 P rows, 6 agree-G4; one AP row per marked crossing for the approval floor, whose 36 refusals file under agree-G4) | `G4.inverse_or_emit` is shape-level and superseded. For the lattice form: the reach fold's **fuel bound** is real and named (`fold_must_run_to_stability`), `FnDecl.calls` stands in for `_calls_in` (an empirical obligation on the lowering), first-class dispatch is `*`, and `inverseOK` reads `undo` only where the reference walks `compensate` too. **Unbuilt work**, not modelling limits. The approval floor (item 246, issue #1455) is `RevL.G4Approval.CrossingOK` over three exported facts: the approval-required capability TOKENS (`AR`, keyed by token as `lower._approval_index` keys them), the tokens one marked crossing reaches (`AX`, as `lower._approval_crossed_caps` resolves them, a `compensate` slot's crossings included) and its `with` edge (`AE`, none for the value form); `crossingB_iff` bridges the printed verdict, and `approval_coverage` fails the gate unless the corpus carries a covered crossing, one refused under another edge, one refused with no edge and an unrequired one admitted. Not modelled there: a `[...]` class in a glob scope, an edge the exporter cannot name (read as none, fail-closed), and a required token declared in a `use`d module |
| **G5** teardown registers nothing | full over the lattice; the shape-level statement is **contentless** and registered as a finding | 2 + 15 | **yes** (one U5 row per effect statement; the gate prints the count) | `G5.teardown_registers_nothing` is true by definition (`registrations` is constant zero) and says so in the registry. `G5Classified` carries the real count, including two operational runs. The oracle now reconstructs the file's `RevL.Lemmas.Prog` from the `EX`/`FN`/`PG` rows and decides `registrations` over each effect's inverse body (`Oracle.registrationsB`, `registrationsB_iff`); the reference recomputes the same reach fold independently from the TSV, and the two agree on every one of them. `prog_coverage` fails the gate unless the corpus carries a clean teardown (count 0) AND a caught crossing (`examples/rejections/g5_undo_fn_emission.rvl`, an `undo` reaching an emission through a `fn`), and `g5_row_not_vacuous` proves the count flips to 0 when the wrapping fn stops calling the emission. An inverse's indirections are resolved by the exporter as the checker resolves them (issue #1792): a dispatched `let`-bound arrow, an emitting fn passed as a value, and a service operation read off a spawn handle (declared in the `Prog` as an `emission` boundary `<Service>.<op>`) count as heads, so all twelve G5 fixtures are `agree-G5`. First-class dispatch (`star`) is `n/a` on both sides, outside the model as in G4 |
| **G6** purity outside effect forms, binding uniqueness | full at head granularity; the shape-level statement is the content of `TypedIn`/`ReachIn` | 3 + 8 | **yes** (one C row per reconstructed statement, one BU row per binding scope; the gate prints the count) | the row reconstructs each lowered statement from its exported heads (`Oracle.exprOfHeads`, proved non-lossy by `heads_exprOfHeads`) and decides `∀ k ∈ stmtHeads s, k ∈ C` with `confinedB` (`confinedB_iff`), against a declared context of the component's require locals (M) plus its require-held binding roots (K). The reference computes the same head-roots membership independently from the TSV, and the two agree on every one of them. A leak is a `fail` on both sides, so the row bites without an admitted violation to point at (the checker refuses those at parse); `confinement_coverage` fails the gate unless the corpus carries both a confined statement over a non-empty reach and a caught violation (281 today), and `g6_row_not_vacuous` proves the verdict flips when a leaking head is accepted. Still not under the row: the derived form (reach computed from program text) lives only in `CapCeilings.derived_confinement_within_ceiling`, and host builtins and let-bound locals count as reach, so a component using them is a faithful `fail` rather than a claim it is unsafe. Binding uniqueness (issue #1812) is `RevL.G6Binding.BindingOK` over each scope's `BE` steps, decided by the `BU` row (`bindingB_iff`), and the corpus's one G6 binding refusal is `agree-G6` |
| **G7** derived LIFO teardown | full for *which* entries run, in *what order*, under *which verdict* — including the E-Stop | 32 + 7 | **yes** (267 D rows) | the row RUNS `backends/python/runtime.py` over an enumerated scenario corpus and diffs the reference's observed disposition against the model's predicted one, with a coverage ratchet (`teardown_coverage`) that fails the gate if the corpus stops distinguishing LIFO from FIFO, Phase 2 from Phase 1, or the three dispositions from one another. Still deliberately not modelled, and so not under the row: Phase-1 continue-and-record and its residue severities, the Phase-2 budget, escrow under a pending session verdict (item 245), cascading abort. This model says which entries run, **not what happens when one of them fails**. The cordis LIFO unwind of the activation-body stack is supplied by the harness, not observed — only `drain`'s own `reversed` loop (item 369) is revl's own ordering code. **Modelling limit, scoped on purpose** |
| **G8** boundary enumerable | full over the lattice; the marker-level statement is weak and marked | 3 + 9 | **yes** (one S8 row per reconstructed statement; the gate prints the count) | `G8.boundary_only_declared` rests on `boundaryOf (.effect _ _) = []` **by definition**. The lattice form drops the typing hypothesis entirely. The oracle now decides `RevL.G8Classified.stmtSurface` over each reconstructed statement's heads against the file's `Prog` (`Oracle.stmtSurfaceB`, `stmtSurfaceB_iff`); the reference recomputes the same reach caps independently from the `EX`/`FN` rows, and the two agree on every one of them. `prog_coverage` fails the gate unless the corpus carries both a non-empty and an empty surface, and `g8_row_not_vacuous` proves the surface goes empty when the wrapping fn stops reaching the crossing. First-class dispatch (`star`) is `n/a` on both sides |
| **G9** no authority from untrusted | rule proved; **coverage of the walk stated and proved** over an L0 component body since issue #2108; the rule is decided on the corpus, on the sink the checker REPORTS | 18 + 16 | **yes** (one TAINT row per checker-reported taint refusal, and one COVERAGE row per component the checker's walk actually visited — 15 files over 21 components today; the gate prints both counts; 4 agree-G9) | `Flow` starts from a path that is *given*. That the checker WALKS every path is where the real bugs were (`_walk_component_methods` skipped activation bodies entirely). **The modelling limit is closed** (issue #2108): L0 grew component bodies with the provide/activation distinction and typed parameters, so the obligation is a theorem rather than prose — `RevL.G9Coverage.coversBodyB` decides the walk against the body's own scopes on the **label**, the **statement count** and the **origin-carrying parameters**, one conjunct per bug that actually occurred, and `coversB_refuses_a_shortened_walk` is the witness that stops elaborating if the walk skips a statement. This is no longer the one **UNPROVED** row for coverage. Still not proved, and named rather than smuggled: the *interprocedural fixed point* (`_Signature`, `_infer_signatures`) that discovers **which** paths exist — the theorem is stated over the scopes a component's own body induces, so a path the walk never learns of is outside it. Since issue #1811 group 2 the oracle's `TAINT` row decides the rule at the sink and on the label the checker itself reports, via `RevL.G9Flow.g9RowB` — route B of the issue, and explicitly **the rule on the corpus, not the coverage of the walk**; the `GC` row below is what carries the coverage half |
| **G-SECRET / G-SECRET-FLOW** | partial, inside the G9 development | (within the 18) | **partial** (the same TAINT row, plus the G9 COVERAGE row; 1 agree-G9) | `secret_persists`, `secret_confined` and `confidential_needs_declassification` prove the *rule*. The coverage gap those three inherited is closed the same way G9's is (issue #2108): the walk-coverage theorem's **parameter** conjunct is exactly the `Secret[T]`-strip bug, so `RevL.G9Coverage.coversB_refuses_a_walk_that_strips_a_secret_param` is stated over the receiver body's own scope. The disclosure rule is now decided on the corpus through the same TAINT row, and **that is the rule on the corpus, not coverage of the checker's walk** — the same caveat G9 carries. The `G-SECRET-FLOW` refusal carries no `navigate`, so the exporter reads its sink out of the refusal message and its origin out of a table spelled in the harness, not out of the hint's prose |
| **G-MODEL-PLACE** model placement and model reach | partial: the placement and reach rules | 6 | **yes** (one MPV row per routed component, one MAV row per consulted role; 9 agree-G-MODEL-PLACE) | since issue #1811 `RevL.ModelPlace.PlaceOK` decides that a confidentiality origin is placed on the device only, through a council member that receives it too (`placeB_iff`), and the model-reach rule of item 519 (a consulted role's `reaches [...]` within what the component holds) is decided with the spawn rule's proved `attenuatesB`. Which roles a component consults, and whether it consults a model at all, are the exporter's, read as `lower._model_reach_edges` / `_consults_a_model` read them. Not modelled: the route-block shape rules and the value-level origin ceiling (item 514) |
| **G-COUNCIL-SPLIT** tie policy | partial: the tie-outcome rule | 7 | **yes** (one CTV row per declared council; 1 agree-G-COUNCIL-SPLIT) | since issue #1811 `RevL.ModelCouncil.SplitOK` decides that no declared council admits when its members disagree, over the tie outcome the checker reads for each council — the declared `on_tie`, or the `split` default an omitted clause resolves to (`splitB_iff`). This is a finite check over declarations with no reach, so it needs no component body; the exporter reads `prog.model_councils` and nothing else. The other `model-council` refusals are different rules with the same code and this row is deliberately silent on them: the unknown tie outcome, the aggregation vocabulary and its totality on the declared member set, the member functions and their uniqueness, `quorum` bases other than `declared`, and "exactly one aggregate rule". A council declaring no aggregation emits no `CV` row at all, so its refusal lands in `out-of-fragment` rather than being read as an agreement this row cannot make |
| **G-RETAIN** retained data at a persistence sink | partial: the deadline rule | 9 | **yes** (one RETAIN row per checker-reported retention refusal; 1 agree-G-RETAIN) | since issue #1811 group 3 `RevL.GRetain.retainRowB` decides that a `Retained[T, P]` value does not reach a persistence sink after `P`'s deadline, over the scope, the sink and the walk the checker itself REPORTS and at the instant the checker itself COMPARED — the harness pins that instant with `REVL_RETENTION_AS_OF`, so the verdict is reproducible rather than wall-clock. The rule is the checker's own strict `as_of > until` with its three escapes (erase and keep the receipt, extend `until`, declare a `hold`); the `hold` override is proved as `hold_clears_the_deadline`. **This row is the rule on the corpus, not the coverage of the checker's walk** — the same caveat G9 carries. The `G-RETAIN` refusal carries no `navigate`, so the exporter reads its sink, scope, policy, `until`, `now` and chain out of the refusal message and hint, not out of structured fields. The declaration-level `G-RETAIN` refusal (`taint._refuse_retention_declaration`) needs no flow at all and is a different judgment; it emits no row and falls through to `out-of-fragment` |
| **A1** iteration boundaries only during activation | partial: the async-colour rules | 10 | **yes** (one A1 row per site, one A1S row per provide method; 9 agree-A1) | the iteration boundary itself is not modelled (L0 has no `await`). Since issue #1808 the async-colour rules are: `RevL.A1Async` decides, per site, that a sync provide method, an unawaited `effect`/`emit` step and an `undo`/`compensate` slot reach nothing async and that an awaited step does, over the file's async names (async externs, async service operations) and its `fn` call graph within a fuel bound (`reachB_iff`, `siteB_iff`), and that a provide method's colour is its service's (`sigB_iff`). Not modelled: an arrow's type has no colour (`a1_async_arrow_sync_type.rvl` stays out of fragment), colour polymorphism through a callback parameter, and a stream `next` as a suspension |
| **A2** no acquisition after a provision | full over the ordered activation body | 14 | **yes** (317 A2 rows, 1 agree-A2) | the body is a step list (`acquire` / `provide` / `other` — the checker's four refused forms, the `provide` block, and everything else) and `RevL.A2.a2B` is `lower._dispatch_action`'s fold verbatim, bridged to the declarative rule by `a2B_iff`. The content is over G7's stack: with a `bracket` per release and per withdrawal, `proof_pass_is_withdrawals_then_releases` proves that under A2 `RevL.Semantics.phase1` runs every withdrawal before every release under every settling verdict, and `fixture_opens_the_window` proves the fixture's shape runs a release first. The oracle folds the same rule over the exported `AQ` body steps on both sides; `a2_coverage` fails the gate unless the corpus carries an admitted body with both a provision and an acquisition and the refused shape. **Not modelled**: entries a provide-method body registers at call time (the G7 corpus's `method` seam), and whether the runtime withdraws a provision as a bracket at all — the theorem takes the LIFO premise the rule rests on and shows A2 is exactly the ordering condition under it |
| **A3** host-safe identifiers | **none** | 0 | no | lexical, checked by extraction rather than by a theorem shape. **Out of scope by kind** |
| **A5** compensation accompanies an emission | partial: the registry rule, at every declared computer-use token | 26 | **yes** (one A5 row per declared computer-use capability of every extern, and one per checker-reported A5 refusal; 2 agree-G4) | since issue #2114 `RevL.A5.legalToken` decides A5 — a declaration whose token's reversibility class is `compensatable` must fill its `compensate` slot, and one whose class has NO INVERSE must not. `compensate` is an optional slot (DESIGN.md §3.5), so the guarantee is vacuous until the registry says WHEN it is required; `revl.ui_family.REVERSIBILITY` says exactly that, and the row carries the DECLARED TOKEN so both sides read the class off it through their own copy of the registry (`_a5_class` / `legalToken`), which is what makes their agreement a check rather than a restatement. **The code that carries it is G4 with category `reversibility`, NOT an A5 code — there is none.** `parser.py` calls `ui_family.teardown_refusal` at the extern declaration (the one point where the capability scope, the extern name and the `compensate` clause are all in hand) and raises `code="G4", category="reversibility"`; `reversibility` is the only category in the tree with that one raiser, and the three pre-existing `REFUSED-AT-PARSE` G4 documents report category `guarantee`, so the row's second `checker_alignment` pass discriminates cleanly. Both halves are proved: `RevL.A5.compensatable_without_compensate_refused`, `RevL.A5.no_inverse_may_not_declare`, and `RevL.A5.reversible_untouched` (neither half reaches a verb that already has its inverse). `RevL.A5.registersB_iff`, `RevL.A5.claimsNothingB_iff` and `RevL.A5.legalB_iff` decide each half and their conjunction at one declaration; `RevL.A5.a5B_iff` lifts the rule to a whole file; `RevL.A5.legalToken_iff` decides it at the token and `RevL.A5.legalCols_iff` at the two STRING columns the row carries (the declared token and the `compensate` column), so the class is read from the declared token rather than from a column the exporter computed; `RevL.A5.not_legal_of_legalB_false` is what lets a `fail` explain a refusal instead of being a harness error; `RevL.A5.rung_is_not_an_escape_hatch`, `RevL.A5.valuation_does_not_move_the_class` and `RevL.A5.outside_the_family_is_out_of_the_row` fix the three boundaries of reading the class off a token (a rung is its verb's class, an item-294 valuation does not move it, and an unclassified token is out of the row). `RevL.A5.fixtures_decided` decides the corpus's own declarations at five polarities and `RevL.A5.a5_not_vacuous` / `RevL.A5.witness_bites` show the verdict flips when NOTHING but the `compensate` column moves, in both directions, and does NOT flip for the reversible pair. `a5_coverage` fails the gate unless the corpus carries an admitted `compensatable` declaration WITH its `compensate`, an admitted class-with-no-inverse declaration, and at least one violating row whose file the checker reports as `G4`/`reversibility` (`agree-G4`; `missed-G4` is FATAL). The row is assembled in two places because a parse refusal has no `file_facts`: admitted rows from the export, violating rows from the refusal's own sentence, cross-checked against the shipped registry (a disagreement is recorded in `_A5_UNREADABLE` and fails the gate rather than being silently skipped). Refusing examples: `examples/rejections/a5_compensatable_without_compensate.rvl`, `examples/rejections/a5_no_inverse_declares_compensate.rvl`. `docs/rejections.md`'s former "no refusing example CAN exist" argument is superseded: it held only while `compensate` was read as unconditional, and the registry is what makes it conditional. **Not modelled**: `ui_family`'s scope/session caveats (a token the registry classifies but whose crossing the checker does not reach), and the `witnessed`/`irreversible` classes' own separate rules, which are G4's and not this row's |
| **A6** provide-methods match the service signature | partial: the call-site half | 4 | **yes** for the call-site half (one MS row per component; 1 agree-A6) | since issue #1809 `RevL.Prelude.MethodOK` decides that every operation a component names (a crossing or a provide-block implementation) is declared by its service. The signature match itself (arity, parameter and return types) is not modelled: the oracle's P row is a *capability bound* check, and `methodBoundOK` is a private restatement. **Unbuilt work** |
| **A8** mid-body failure reverts and contains | full over the WAL model | 18 | **yes** (1620 O rows) | the row WRITES each scenario's records as a real JSON-Lines WAL and runs `src/revl/recovery.py` over it, diffing recover's own verdict, the set it actually applied to the `World`, and its reported residue against the model's `outcome` / `replayed` / `reported`. It found the legacy-`effect` family's item-309 fence branch missing from `RevL.Lemmas.dispose` (see below). Crash cuts covered: fence-to-apply, abort-then-crash, the approved-to-discharged window. **Not covered and not claimed**: a crash between a witnessed mutation and its record (the reference logs the descriptor *after* the forward extern returns), the roll-forward `flush-residue` surface, cascading abort, escrow. Durability is a floor, not a theorem |
| **A9** provide key declared in `provides`, both directions | full over the installed blocks and routes | 16 | **yes** (one A9 row per component that declares or installs anything; the gate prints the count) | the installed `provide k { … }` block keys and the `isolate k in realms(...)` routed keys are modelled beside the L0 `LComponent` (`RevL.A9.Installed`, no L0 edit); `A9OK` is `BlocksDeclared` (every block key is in the clause, issue 1167) AND `DeclaredInstalled` (every clause key has a block or a route, issue #1172 / PR #1184). `installed_block_is_slot` is the bridge to G2/G3: under A9 every block answers a `(key, realm)` slot of the universe `LinkOK` reasons over; `undeclared_block_is_no_slot` and `declared_uninstalled_refused` are the two fixtures' shapes, `unrouted_needs_a_block` the converse as stated for an ordinary provider, `routed_installs_without_block` the exemption (`stdlib/router.rvl`'s `RoundRobin`). The oracle exports the blocks as `PB` facts and the routes as `PR` facts (the `C` row reads the clause, not the body) and decides `a9B` per component that declares or installs anything; `a9_coverage` fails the gate unless the corpus carries an admitted provider, both refused shapes (`examples/rejections/a9_provide_key_not_declared.rvl`, `examples/rejections/a9_provides_without_block.rvl`, both `agree-A9`; `missed-A9` is FATAL) and the routed shape admitted (`tests/formal_corpus/a9_routes_installs_key.rvl`). The double install ("provision `k` is installed twice", uncoded) is `NoDoubleInstall`, proved distinct from A9 and not under the row: no corpus file installs twice and the refusal carries no code. **Not modelled**: the checker's skip of the converse for a body that recovered past a refused statement (item 386), which can only land in `formal-found-other`; and the route's realm legs, elided from the V row (see the fidelity limits) |
| **T1/T2/T3** typing, `Opt[T]`, holes | **none** | 0 | no | the type checker is outside the guarantee backbone. **Out of scope by kind** |
| **R4** no residue | full for the **abort path** | 9 | **yes** (the residue column of the 1620 O rows) | the column is the model's `reported`, diffed against `recover`'s `residue.outstanding`; it is printed only under `outcome = rolledBack`, which is R4's own scope condition. Stated over the abort; the roll-forward window's `flush-residue` surface is still not modelled and the column says `n/a` there rather than agreeing about a claim neither side makes. **Unbuilt work** |
| items 66/294/260 capability ceilings | full, with the held/reach sets **derived** from component shapes | 23 | **yes** (8 W rows) | the ceiling half is now EXERCISED: `examples/budget_attenuation.rvl` (50 ≤ 100, admitted) and `examples/rejections/g4_spawn_widens_budget.rvl` (1000 > 100, refused by the ceiling half alone — strip the ceilings and the resource fold finds nothing uncovered), with `attenuation_coverage` failing the gate if the corpus stops containing both. Before those two files the row agreed over 6 edges with `ceilingOKB` never entered. Also unmodelled: parse-time canonicalization and `cap_order.disjoint`'s D2 same-token clause |
| item 133 cross-tier agreement | full, under two named hypotheses | 4 | no | that the real emitters realise a `Conformant` profile is the differential conformance matrix's empirical obligation; map values are one level deep |

Three summary readings of that map:

- **The oracle reaches G2, G3, G4, G7, A2, A8, R4, A9 and the capability order.**
  G5, G6, G8 and G9 are still proved against the design documents and the
  reference source read by hand. That remains the largest gap in the
  layer, and it is a gap in *coverage of the gate*, not in the proofs. The
  four that are left are left for a reason and the reason is the same one:
  each is indexed by `RevL.Syntax.Stmt` (or, for G9, by a `Flow` over a
  path), and the export carries FACTS — call sites, capabilities,
  manifests — not statement terms, so there is no shape for a corpus row
  to take. Closing them is an export change, not an oracle change.
  Two rows now *execute* rather than read, because their subject is a run
  and not a text: G7 drives `backends/python/runtime.py` over an
  enumerated teardown corpus, and A8/R4 drive `src/revl/recovery.py` over
  an enumerated WAL corpus.
- **Two guarantee codes have no theorem at all** (A3, T1-T3).
  A5 left this list in issue #2114. It was the one worth naming twice:
  G7 proves how a `compensation` entry is disposed without anything
  proving one has to exist, and `compensate` is an *optional* slot, so
  the guarantee was vacuous until the registry (`ui_family.
  REVERSIBILITY`) said when it becomes required. The row now states the
  rule over the registry, at every declared computer-use token, and
  credits it to the code that carries it — G4 with category
  `reversibility`, not an A5 code. A9 left this list in issue 1167, A2 in
  issue 1166, A1's async-colour rules in issue #1808 and A6's call-site
  half in issue #1809: each rule is now a theorem and a differential row.
- **No row is UNPROVED by construction any more.** G9 path coverage was
  the last one, and it said so in the table rather than being absent:
  `UNPROVED, unstatable`, because L0 had no component bodies and the
  obligation could not be written down at all. Issue #2108 grew L0 —
  `RevL.Syntax.Body` with its `provide`/activation distinction and typed
  parameters — and stated the obligation as `RevL.G9Coverage.coversBodyB`,
  decided against the walk the harness *observes* by the oracle's `GC`
  row, so the differential oracle can disagree with it. The residue it
  does **not** reach — the interprocedural fixed point that discovers
  *which* paths exist — is named in the G9 section rather than left for a
  reader to infer.

## Theorem status

| Theorem | Guarantee (DESIGN.md §4) | Status | Axioms | Notes |
|---|---|---|---|---|
| `RevL.G1.declared_only_access` | G1 — declared access (component level) | **proved** | `propext, Quot.sound` | undeclared access cannot be written |
| `RevL.G2.linkOK_provision_disjoint` | G2 — provision disjointness (Def. 43) | **proved** | `propext, Quot.sound` | from the incremental `LinkOK` judgment; the unit is the `(key, realm)` slot |
| `RevL.G2.linkOK_requires_closed` | G2/G1 — requirement closure | **proved** | `propext` | every consumed slot provided in-composition |
| `RevL.G2.realm_separation_admitted` | G2 — non-vacuity | **proved** | `propext, Quot.sound` | `examples/tenants.rvl`: one key, two realms, links |
| `RevL.G2.same_realm_conflict_refused` | G2 — non-vacuity | **proved** | `propext, Quot.sound` | drop the realms and the same pair cannot link |
| `RevL.G3.depPath_rank_lt` | G3 — cycles rejected (§6.5) | **proved** | none | ranks strictly decrease along dep paths |
| `RevL.G3.no_dependency_cycles` | G3 | **proved** | none | a layering certificate excludes cycles |
| `RevL.G3.linkOK_layeredBy_rankOf` | G3 — the layering construction | **proved** | `propext, Quot.sound` | the admission order is a layering: `rankOf` |
| `RevL.G3.linkOK_layered` | G3 — the bridge | **proved** | `propext, Quot.sound` | `LinkOK comps → ∃ rank, LayeredBy comps rank` |
| `RevL.G3.linkOK_no_cycles` | G3 — as the linker states it | **proved** | `propext, Quot.sound` | admitted composition ⇒ no cycle, nothing assumed |
| `RevL.G3.self_provision_refused` | G3 — non-vacuity | **proved** | `propext` | `Ouroboros` (requires a key it provides) cannot link |
| `RevL.G3.mutual_cycle_refused` | G3 — non-vacuity | **proved** | `propext` | `g3_dependency_cycle.rvl` refused in both orderings |
| `RevL.G3.layering_exists_for_admitted` | G3 — non-vacuity | **proved** | `propext, Quot.sound` | the certificate is reachable, not just refutable |
| `RevL.G4.inverse_or_emit` | G4 — inverse-or-emit (Def. 8) | **proved (shape-level)** | none | *weaker*: content is the shape of `Typed`, which has no `raw` constructor, so the same sentence holds of a relation admitting nothing. **Superseded by `RevL.G4Classified.inverse_or_emit_classified`** (item 418 step 4); kept as the syntactic statement |
| `RevL.G5.teardown_registers_nothing` | G5, teardown registers nothing | **proved, and CONTENTLESS** | none | `registrations` ignores its argument (constant zero), so a `sneakyUndo` calling `db.insert` counts 0 and the conclusion holds by definition rather than by any property of undo bodies. `RevL.G5.registrations_ignores_its_argument` proves the review's own probe, so the emptiness is on the record. **Superseded by `RevL.G5Classified.inverse_reaches_no_emission`** (item 418 step 4); kept as the grammar-level statement |
| `RevL.G6.confinement` | G6 — confinement (Def. 48) | **proved** | `propext, Quot.sound` | content is the shape of `TypedIn`/`ReachIn` |
| `RevL.Semantics.replays_or_discharges` | G7 (item 418 step 5) | **proved** | `propext` | replayed and discharged are complements, per kind and per **settling** verdict. Item 443 added the `v.settles = true` hypothesis; `RevL.Semantics.disposition_trichotomy` is the total replacement and `RevL.G7.settles_iff_strands_nothing` proves the hypothesis is exactly "this verdict strands nothing" |
| `RevL.Semantics.phase_lengths_add` | G7 (item 418 step 5) | **proved** | `propext, Quot.sound` | the two phases partition the replaying entries |
| `RevL.Semantics.teardown_length` | G7, length form | **proved** | `propext, Quot.sound` | one replay per entry *the verdict replays*. The pre-step-5 row said "one replay per witnessed effect", which over-counted every commit carrying a transactional entry |
| `RevL.G7.replay_table` | G7 (item 418 step 5) | **proved** | none | the teardown contract's replay rows, computed per kind and verdict |
| `RevL.G7.replayed_complete` | G7, completeness | **proved** | `propext` | every entry the verdict replays is on the replay list |
| `RevL.G7.teardown_replays_all` | G7 (LIFO-completeness, Thm. 16) | **proved** | `propext, Quot.sound` | the same at the inverse level. **Corrected in item 418 step 5**: the pre-step-5 statement had no hypothesis and was therefore false of a committing activation carrying a transactional entry |
| `RevL.G7.replayed_sound` | G7, soundness | **proved** | `propext` | the replay list holds only registered entries this verdict replays |
| `RevL.G7.teardown_only_witnessed` | G7 | **proved** | `propext, Quot.sound` | the same at the inverse level, now also excluding what the verdict discharges |
| `RevL.G7.commit_discharges_transactional` | G7 vs `runtime.py` | **proved** | `propext` | a clean commit never replays a witnessed inverse. This is the row the pre-step-5 G7 contradicted |
| `RevL.G7.commit_discharges_compensation` | G7 vs `runtime.py` (item 247) | **proved** | `propext` | a clean commit never fires a compensation |
| `RevL.G7.commit_replays_only_brackets` | G7 | **proved** | `propext` | a clean commit replays brackets and nothing else |
| `RevL.G7.abort_replays_every_transactional` | G7 vs `runtime.py` | **proved** | `propext` | the other half of the `_Transactional` branch |
| `RevL.G7.bracket_replays_under_every_settling_verdict` | G7 | **proved** | `propext` | releasing an acquired handle is always right *when the activation settles*. Item 443 added the hypothesis and this audit renamed the theorem to say so; `RevL.G7.bracket_replays_exactly_when_settling` is the hypothesis-free equation behind it and `RevL.G7.bracket_is_replayed_or_stranded` the total form |
| `RevL.G7.teardown_eq_reversed_inverses` | G7 | **proved** | `propext` | the LIFO equation, per phase; positions via `List.getElem_reverse` |
| `RevL.G7.compensations_drain_after_the_proof_pass` | G7, the phase split | **proved** | `propext` | the replay is a compensation-free prefix then an all-compensation suffix |
| `RevL.G7.phase1_is_lifo` | G7, order within a phase | **proved** | `propext` | undoing the run order gives back a sub-sequence of the registration order |
| `RevL.G7.phase2_is_lifo` | G7, order within a phase | **proved** | `propext` | the same for the drain |
| `RevL.G7.commit_runs_the_bracket_only` | G7, non-vacuity | **proved** | none | one stack, all three kinds: the commit replay is exactly the bracket inverse |
| `RevL.G7.abort_runs_the_proof_pass_then_the_drain` | G7, non-vacuity | **proved** | none | the same stack on abort: proof pass LIFO, then the compensation that was registered LAST |
| `RevL.G7.verdict_is_load_bearing` | G7, non-vacuity | **proved** | none | the two replays of one stack differ |
| `RevL.G7.commit_discharge_is_not_vacuous` | G7, non-vacuity | **proved** | `propext` | the discharged entry is on the stack the theorem quantifies over |
| `RevL.Semantics.disposition_trichotomy` | G7 / item 443 — total accounting | **proved** | none | every (kind, verdict) pair has EXACTLY one disposition: replayed, discharged or stranded. The hypothesis-free replacement for `replays_or_discharges` |
| `RevL.Semantics.halted_strands_every_kind` | G7 / item 443 — the E-Stop column | **proved** | none | under `.halted` every kind is neither replayed nor discharged, and stranded |
| `RevL.Semantics.replayed_length` | G7 / item 443 | **proved** | `propext, Quot.sound` | one replay-list entry per entry the verdict replays (`teardown_length` before the `map`) |
| `RevL.Semantics.book_lengths_add` | G7 / item 443 — the books balance | **proved** | `propext, Quot.sound` | replayed + discharged + stranded is the whole stack, under **every** verdict. All three terms are witnessed non-zero |
| `RevL.G7.estop_replays_nothing` | G7 / item 443 — the guarantee | **proved** | `propext, Classical.choice, Quot.sound` | a halt runs no inverse at all, not "the brackets and none of the rest" |
| `RevL.G7.estop_discharges_nothing` | G7 / item 443 | **proved** | `propext, Classical.choice, Quot.sound` | discharge releases the inverse and the witness; a halt must keep both for `revl recover` |
| `RevL.G7.estop_strands_everything` | G7 / item 443 — the counterpart of R4 | **proved** | `propext, Quot.sound` | R4 is "no residue"; the E-Stop is "all residue, all of it on the inventory" |
| `RevL.G7.estop_strands_the_bracket` | G7 / item 443 — non-vacuity | **proved** | none | the bracket row really does change in the third column, so the settling hypothesis is load-bearing |
| `RevL.G7.halt_inventory_is_total` | G7 / item 443 — the halt cut | **proved** | `propext` | completed ++ ambiguous ++ unattempted is the interrupted replay order exactly |
| `RevL.G7.halt_ambiguity_is_at_most_one` | G7 / item 443 — item 440's tier | **proved** | `propext, Quot.sound` | a halt makes at most ONE inverse ambiguous, never a fog over the stack |
| `RevL.G7.halt_books_are_total` | G7 / item 443 | **proved** | `propext, Quot.sound` | the five books balance at every cut and under every verdict |
| `RevL.G7.estop_is_load_bearing` | G7 / item 443 — non-vacuity | **proved** | none | one three-kind stack: abort replays 3 and strands 0, the halt replays 0 and strands 3 |
| `RevL.G7.mid_abort_halt_cut_is_not_vacuous` | G7 / item 443 — non-vacuity | **proved** | none | a halt one inverse into an abort: one completed, one ambiguous, one never attempted |
| `RevL.G7.settles_iff_not_halted` | G7 / item 443 — the hypothesis, audited | **proved** | `propext` | `v.settles = true` excludes **exactly** `.halted` and no other verdict |
| `RevL.G7.settles_iff_strands_nothing` | G7 / item 443 — the hypothesis, audited | **proved** | `propext` | a verdict settles precisely when it strands no kind, so `settles` is the property the dichotomy needs and not a side condition picked to rescue a proof |
| `RevL.G7.settling_strands_nothing` | G7 / item 443 — the hypothesis, audited | **proved** | `propext, Classical.choice, Quot.sound` | a settling verdict owes nothing: the inventory is empty for every stack. With `estop_strands_everything` this is the whole of `stranded` |
| `RevL.G7.bracket_replays_exactly_when_settling` | G7 / item 443 — strengthening | **proved** | none | *stronger than the corollary that carries the hypothesis*: the bracket row **equals** `settles`, with no hypothesis at all |
| `RevL.G7.bracket_is_replayed_or_stranded` | G7 / item 443 — the total form | **proved** | `propext` | over every verdict, no hypothesis: a registered bracket is replayed, or the verdict is the E-Stop and the bracket is on the inventory |
| `RevL.G8.boundary_enumerates_emissions` | G8 — boundary enumerable (§6.1) | **proved (shape-level)** | `propext, Quot.sound` | *weaker*: completeness over the syntactic `emit` marker. **Superseded by `RevL.G8Classified.surface_enumerates_reached_crossings`** (item 418 step 4); kept as the marker-level statement |
| `RevL.G8.boundary_only_declared` | G8 | **proved (shape-level)** | `propext, Quot.sound` | *weaker*: rests on `boundaryOf (.effect _ _) = []` **by definition**, so a typed `.effect` carrying an emission has an empty surface. **Superseded by `RevL.G8Classified.surface_only_declared_crossings`** (item 418 step 4); kept as the marker-level statement |
| `RevL.CapCeilings.cap_order_partial` | item 294 — the `(T,P)` capability order | **proved** | `propext, Quot.sound` | `covers` is reflexive, transitive, antisymmetric |
| `RevL.CapCeilings.attenuation_monotone` | items 66/294 — attenuation is downward | **proved** | `propext` | a lineage never exceeds the root's declared authority |
| `RevL.CapCeilings.lineage_ceiling_le` | item 260 — budgets only shrink | **proved** | `propext, Quot.sound` | a dropped child ceiling reads as `+∞`, so it cannot escape |
| `RevL.CapCeilings.spend_within_budget` | item 260 — the runtime counter | **proved** | `propext, Quot.sound` | `remainingUses` is never overdrawn |
| `RevL.CapCeilings.budget_never_exceeds_root_ceiling` | item 260 — end to end | **proved** | `propext, Quot.sound` | static shrink composed with the dynamic counter |
| `RevL.CapCeilings.confinement_within_ceiling` | item 294 + G6 | **proved** | `propext, Quot.sound` | reach is bounded by `Γ`, `Γ` by the root ceiling |
| `RevL.CapCeilings.no_star_amplification` | item 66 — the host boundary | **proved** | `propext` | `*` is covered only by `*`, so it is never manufactured |
| `RevL.CapCeilings.parameter_widening_refused` | item 294 — non-vacuity | **proved** | `propext` | tracks `examples/rejections/g4_spawn_widens_parameter.rvl` |
| `RevL.CapCeilings.ceiling_check_not_subsumed` | item 260 — non-vacuity | **proved** | `propext` | the resource fold is ceiling-blind; the budget check is not |
| `RevL.CapCeilings.derived_held_tokens_are_declared_keys` | TODO 2(a) — the `capKeys` bridge, split by namespace | **proved** | `propext, Quot.sound` | the BOUND column's tokens are exactly the declared wiring keys; every element of the CAPABILITY column is a declared capability, or that key in the reserved `key:` namespace — never a bare key |
| `RevL.CapCeilings.derived_reach_is_emit_surface` | TODO 2(a) — `_collect_emit_caps_pairs` | **proved** | `propext, Quot.sound` | only `emit` contributes; an `emit` contributes the cone the service behind its key declares |
| `RevL.CapCeilings.unnameable_receiver_is_star` | TODO 2(a) — the named residue | **proved** | `propext` | handle / head-less receivers derive exactly `[*]`, in both columns |
| `RevL.CapCeilings.derived_lineage` | TODO 2(a) — text to `Lineage` | **proved** | `propext` | an admitted activation spawn edge is a lineage edge |
| `RevL.CapCeilings.derived_attenuation_monotone` | TODO 2(a) — items 66/294 | **proved** | `propext, Quot.sound` | `attenuation_monotone` over derived sets; the closure carries the subtree |
| `RevL.CapCeilings.derived_lineage_ceiling_le` | TODO 2(a) — item 260 | **proved** | `propext, Quot.sound` | `lineage_ceiling_le` with its `Lineage` hypothesis discharged |
| `RevL.CapCeilings.derived_budget_never_exceeds_root_ceiling` | TODO 2(a) — item 260 | **proved** | `propext, Quot.sound` | the end-to-end budget claim, rooted in a component shape |
| `RevL.CapCeilings.derived_confinement_within_ceiling` | TODO 2(a) + G6 | **proved** | `propext, Quot.sound` | `TypedIn (capKeys Γ)` discharged from `TypedIn (reqKeys c)`; clause (a) reads the key namespace (`heldBounds`), clause (b) the declared one (`heldCaps`) |
| `RevL.CapCeilings.derived_no_star_amplification` | TODO 2(a) — item 66 | **proved** | `propext, Classical.choice, Quot.sound` | the `*`-free side condition is itself derived, now from the declared cones; a reserved `svc:` element is never `*` |
| `RevL.CapCeilings.derivation_non_vacuous` | TODO 2(a) — non-vacuity | **proved** | `propext, Classical.choice, Quot.sound` | derived sets carry valuations; `g4_spawn_widens_parameter` refused from the text |
| `RevL.CapCeilings.same_key_different_boundary_refused` | TODO 2(a) — the namespace split | **proved** | `propext, Classical.choice, Quot.sound` | one `requires` key over two different declared boundaries: the bound column derives the same list on both sides and attenuates, the capability column refuses the edge |
| `RevL.CapCeilings.same_key_undeclared_boundary_refused` | item 561 — the split where nothing is declared | **proved** | `propext, Classical.choice, Quot.sound` | the same pair with NEITHER service declaring a token, which is the spelling most of the corpus uses: the element is the service rather than the consumer's key, and the capability column refuses the edge the bound column attenuates |
| `RevL.CapCeilings.derivation_refuses_unnameable` | TODO 2(a) — non-vacuity | **proved** | `propext, Classical.choice, Quot.sound` | a handle emission derives `*` and is not folded into a held element |
| `RevL.CapCeilings.derived_ceiling_check_not_subsumed` | TODO 2(a) — non-vacuity | **proved** | `propext, Classical.choice, Quot.sound` | both relations still load-bearing once the sets are derived |
| `RevL.G9.origin_persists_or_is_declassified` | G9 (item 249) — the core lemma | **proved** | `propext` | an origin survives a flow, or a declassifier on that path cleared it |
| `RevL.G9.no_authority_from_untrusted` | G9 — untrusted data gains no authority | **proved** | `propext` | a `Trusted[T]` sink admits a tainted value only after an explicit declassification of that origin |
| `RevL.G9.untrusted_gains_no_authority` | G9 — the refusal | **proved** | `propext` | a declassifier-free path cannot reach an authority sink |
| `RevL.G9.declassification_is_the_only_escape` | G9 (item 249, Decision 2) | **proved** | `propext` | the label is monotone along every non-declassifying step |
| `RevL.G9.secret_persists` | item 256 §4a.3 | **proved** | `propext` | a bound provider key has no declassifier, so it survives every admitted path |
| `RevL.G9.secret_confined` | item 256 — secret confinement | **proved** | `propext` | a bound key reaches no sink, `Secret[T]` receivers included (the A8 disjointness) |
| `RevL.G9.confidential_needs_declassification` | item 256 Slice 3 §7 | **proved** | `propext` | a `Secret[T]` value reaches a disclosure sink only through a declared `endorse[confidential]` |
| `RevL.G9.flow_declassifiers_granted` | item 249 Slice C | **proved** | `propext` | under `no_declassify` every declassifier on an admitted path is pre-granted |
| `RevL.G9.untrusted_author_needs_granted_declassifier` | items 249/329 | **proved** | `propext` | a self-minted declassifier does not count for a model-authored turn |
| `RevL.G9.declassifier_must_be_declared` | item 249 Slice C | **proved** | none | an undeclared `endorse[o]`, and `endorse[secret]` at all, are refused |
| `RevL.G9.taint_surface_within_declared_context` | G9 + G6 | **proved** | `propext, Classical.choice, Quot.sound` | origins are derived from the declared capability scope; reach bounds them |
| `RevL.G9.no_untrusted_without_a_declared_source` | G9 + G6 | **proved** | `propext, Classical.choice, Quot.sound` | with no untrusted-scoped requirement, untrusted data is unwritable |
| `RevL.G9.g9_not_vacuous` | G9 — non-vacuity | **proved** | `propext, Classical.choice, Quot.sound` | admits the endorsed path, refuses the bare one and the foreign-origin endorse |
| `RevL.G9.secret_rules_not_vacuous` | item 256 — non-vacuity | **proved** | `propext` | `confidential` downgrades, `secret` never does, self-minted flips to refused |
| `RevL.G9.authority_refusal_is_not_universal` | G9 — anti-tautology (item 418) | **proved** | `propext` | one flow, admitted at a disclosure sink and refused at an authority sink |
| `RevL.G9.sink_rules_are_distinct` | G9 — anti-tautology (item 418) | **proved** | `propext` | the four `Admits` rules separated pairwise; not one predicate four times |
| `RevL.G9.secret_refusal_is_load_bearing` | item 256 — anti-tautology (418) | **proved** | `propext` | the algebra CAN clear a `secret`; `taint.py`'s two refusals are what stop it |
| `RevL.G9Coverage.coversB_iff` | G9 — path coverage (issue #2108) | **proved** | `propext` | `coversB` is exactly the three projections being equal out to the LONGER list, so a walk that drops, shortens, re-seeds or pads a scope flips the Bool |
| `RevL.G9Coverage.coversBodyB_iff` | G9 — path coverage (issue #2108) | **proved** | `propext` | `coversBodyB_iff` is `coversB_iff` at the body's own scopes: the printed `coverage` verdict is this Bool, so the oracle cannot print `ok` for a walk the theorem refuses |
| `RevL.G9Coverage.coversB_self` | G9 — path coverage (issue #2108) | **proved** | `propext` | a scope list covers itself — the base case every refusal witness is measured against, so those are not satisfied by a `coversB` that always returned `false` |
| `RevL.G9Coverage.walk_covers` | G9 — path coverage (issue #2108) | **proved** | `propext` | **every** body is covered by the walk it induces — the positive direction. Together with the five refusals below this pins `coversBodyB` to be neither constant-`true` nor constant-`false` |
| `RevL.G9Coverage.reachIn_callSvc` | G9 — path coverage (issue #2108) | **proved** | `propext` | the witness body's activation step reaches `svc`, a name the witness context holds: the body is inside L0's `ReachIn`, not a shape L0 cannot admit |
| `RevL.G9Coverage.scope_origins_exclude_plain_and_trusted` | G9 — path coverage (issue #2108) | **proved** | none | `Scope.origins` carries exactly the parameters whose qualifier SEEDS an origin — `Secret` and `Untrusted`, never a plain or `Trusted` one. The strip-the-`Secret[T]` bug, stated as the filter that decides it |
| `RevL.G9Coverage.witnessBody_scopes` | G9 — path coverage (issue #2108) | **proved** | `propext` | the witness body's scope list, spelled out: an activation scope with its statement count and no parameters, then the provide scopes with their counts and their `Secret`/`Untrusted` parameters in declaration order |
| `RevL.G9Coverage.witnessBody_is_admitted` | G9 — path coverage (issue #2108) | **proved** | `propext` | the witness body typechecks in the witness context, so `Body.TypedIn` is **inhabited** by a body with an activation scope and a provide block: the L0 growth is not an empty grammar |
| `RevL.G9Coverage.walkOf_is_the_walk` | G9 — path coverage (issue #2108) | **proved** | `propext` | `walkOf` on the witness body, unfolded: four scopes, `activation` then `<key>.<method>`, with the counts and the seeded parameters the body declares |
| `RevL.G9Coverage.coversB_admits_the_full_walk` | G9 — path coverage (issue #2108) — non-vacuity | **proved** | `propext` | the walk the witness body induces is ADMITTED. The positive half of the non-vacuity witness |
| `RevL.G9Coverage.coversB_refuses_a_walk_that_drops_the_activation_scope` | G9 — path coverage (issue #2108) — non-vacuity | **proved** | `propext` | delete the activation scope from the walk and the verdict flips. **The historical `_walk_component_methods` bug — a component activation body never taint-checked — as a theorem** |
| `RevL.G9Coverage.coversB_refuses_a_shortened_walk` | G9 — path coverage (issue #2108) — non-vacuity | **proved** | `propext` | visit one of the activation body's two statements instead of both and the verdict flips: the statement-count conjunct is load-bearing, and **this is the witness that bites when the walk is shortened** |
| `RevL.G9Coverage.coversB_refuses_a_walk_that_strips_a_secret_param` | G9 — path coverage (issue #2108) — non-vacuity | **proved** | `propext` | a walk that seeds nothing for the witness body's `Secret` parameter is refused — the `Secret[T]`-stripped-inside-its-own-receiver bug, as a theorem |
| `RevL.G9Coverage.coversB_refuses_a_walk_that_seeds_a_trusted_param` | G9 — path coverage (issue #2108) — non-vacuity | **proved** | `propext` | a walk that seeds a `Trusted` parameter is refused: the row is tight in the other direction, and a `Qual.seedsOrigin` widened to `Trusted` stops this elaborating |
| `RevL.G9Coverage.coversB_refuses_a_scope_the_body_does_not_have` | G9 — path coverage (issue #2108) — non-vacuity | **proved** | `propext` | a walk carrying one scope the body does not have is refused rather than truncated away: the label conjunct cannot be met by a prefix |
| `RevL.G9Coverage.g9Coverage_not_vacuous` | G9 — path coverage (issue #2108) — non-vacuity | **proved** | `propext` | **the witness**: the body's own walk admitted AND four distinct shortenings of it refused — drop the activation scope, shorten a count, strip a `Secret` parameter, add a scope. A `coversBodyB` that returned a constant fails all five |
| `RevL.A8.revert_on_failure` | TODO 3 / A8 — L-Raise reverts | **proved** | `propext` | over the small-step semantics: the abort restores the world the body inherited |
| `RevL.A8.trace_reads_back_as_abort` | TODO 3 / A8 | **proved** | `propext` | no body step writes a session marker, so a crashed run rolls back |
| `RevL.A8.committed_transaction_is_retained` | TODO 3 / A8 — the central safety claim | **proved** | `propext` | a durable `discharge` record is never rolled back |
| `RevL.A8.commit_replays_no_inverse` | TODO 3 / A8 | **proved** | `propext` | only the abort verdict replays; a commit touches nothing |
| `RevL.A8.outcome_trichotomy` | TODO 3 / A8 — "never mixed" | **proved** | none | *definitional*: `Outcome` has three constructors; the content is in the two rows below |
| `RevL.A8.crash_cut_converges` | TODO 3 / A8 — convergence | **proved** | `propext, Quot.sound` | a decided crash point stays decided at every later cut |
| `RevL.A8.commit_record_is_the_decision` | TODO 3 / A8 — decision point | **proved** | `propext` | *specification agreement* with `recover`'s if-chain |
| `RevL.A8.approved_decides_the_crash_window` | TODO 3 / A8 — item 245 D3 | **proved** | `propext` | terminal marker missing: `commit-approved` alone decides |
| `RevL.A8.fence_before_apply_at_every_cut` | TODO 3 / A8 — item 309 §3a | **proved** | `propext` | crash BETWEEN the durable write and the effect: the window has no interior |
| `RevL.A8.at_most_once_across_crash` | TODO 3 / A8 — item 309 §3a | **proved** | `propext, Quot.sound` | an undeclared inverse is never applied twice, across abort-then-crash |
| `RevL.A8.declared_idempotent_replay_free` | TODO 3 / A8 — item 309 §3a | **proved** | `propext` | `recover` is idempotent over the declared subset (`DictWorld` pops) |
| `RevL.A8.double_apply_observable` | TODO 3 / A8 — non-vacuity | **proved** | none | a non-idempotent inverse's second apply IS observable |
| `RevL.A8.crash_cut_witness` | TODO 3 / A8 — non-vacuity | **proved** | `propext` | four cuts at one seq: clean abort, abort-then-crash, completed abort, committed |
| `RevL.A8.commit_witness` | TODO 3 / A8 — non-vacuity | **proved** | `propext` | a witnessed inverse does NOT replay on clean commit (cf. item 418 on G7) |
| `RevL.A8.mixed_disposition_admitted` | TODO 3 / A8 — non-vacuity | **proved** | `propext` | committed-and-retained beside rolled-back in one verdict, by design |
| `RevL.A8.revert_witness` | TODO 3 / A8 — non-vacuity | **proved** | none | the step relation genuinely takes the run; the log is its trace |
| `RevL.A8.revert_witness_restores` | TODO 3 / A8 — non-vacuity | **proved** | `propext` | replaying that trace empties the world again |
| `RevL.R4.residue_is_exactly_what_remains` | TODO 3 / R4 — soundness + completeness | **proved** | `propext` | after the abort the world is `w₀` plus EXACTLY the reported residue |
| `RevL.R4.abort_leaves_no_residue` | TODO 3 / R4 — headline | **proved** | `propext` | no boundary crossing ⇒ exact revert and an empty residue surface |
| `RevL.R4.residue_complete` | TODO 3 / R4 | **proved** | `propext` | every reported seq really is still out (the report is not padding) |
| `RevL.R4.residue_sound` | TODO 3 / R4 | **proved** | `propext` | nothing the body left behind goes unreported |
| `RevL.R4.txn_run` | TODO 3 / R4 — non-vacuity | **proved** | none | the semantics takes the witnessed-only run |
| `RevL.R4.emit_run` | TODO 3 / R4 — non-vacuity | **proved** | none | and the run that crosses the boundary |
| `RevL.R4.residue_necessary` | TODO 3 / R4 — non-vacuity | **proved** | `propext` | one step apart: one reverts exactly, one leaves seq 2 out and says so |
| `RevL.R4.emission_is_not_replayed` | TODO 3 / R4 — non-vacuity | **proved** | `propext` | an emission has no inverse; it moves to the residue surface |

| `RevL.Lemmas.reach_mono_fuel` | item 418 step 4 — the reach fold | **proved** | `propext, Quot.sound` | more fuel only raises a classification: the fold under-approximates, never over |
| `RevL.Lemmas.reaches_le` | item 418 step 4 — fold soundness | **proved** | `propext, Quot.sound` | every transitively reachable name is bounded by the fold's verdict |
| `RevL.Lemmas.reach_exact` | item 418 step 4 — fold exactness | **proved** | `propext, Quot.sound` | the verdict is attained at a real declaration; nothing is invented |
| `RevL.Lemmas.reach_le_trans` | item 418 step 4 — paths compose | **proved** | `propext, Quot.sound` | one verdict at the declared inverse constrains every call beneath it |
| `RevL.Lemmas.reachCaps_sound` | item 418 step 4 — capability surface | **proved** | `propext, Quot.sound` | every surface entry traces to a reachable crossing declaration |
| `RevL.Lemmas.reachCaps_complete` | item 418 step 4 — capability surface | **proved** | `propext, Quot.sound` | nothing a reachable crossing declares is dropped |
| `RevL.G4Classified.inverse_or_emit_classified` | G4 over the lattice (item 418 step 4) | **proved** | `propext` | the rule is `declOK`, i.e. `lower.py:2573`/`:2744`/`:2614` — not a missing constructor |
| `RevL.G4Classified.program_mutations_carry_inverse_or_marker` | G4 — program level | **proved** | `propext, Quot.sound` | every mutating declaration of an admitted program |
| `RevL.G4Classified.reached_crossing_is_classified` | G4 — the fn wrapper | **proved** | `propext, Quot.sound` | a crossing reached through a `fn` is still classified as crossing |
| `RevL.G4Classified.reached_crossing_carries_inverse_or_marker` | G4 — transitively | **proved** | `propext, Quot.sound` | a reached crossing has a concrete declaration behind it that satisfies G4 |
| `RevL.G4Classified.raw_mutation_is_representable` | G4 — anti-tautology guard | **proved** | none | the `raw` shape IS a term here; the old G4 could not write it |
| `RevL.G4Classified.g4_not_vacuous` | G4 — non-vacuity | **proved** | `propext` | three shapes admitted, two refused (`raw_write`, an emission claiming `undo`); refusal not universal |
| `RevL.G4Classified.fn_wrapper_still_crosses` | G4 — non-vacuity | **proved** | `propext` | `audit_log` is pure at fuel 0 and `emission` at fuel 1 |
| `RevL.G5Classified.registrations_seq` | G5 — the real count | **proved** | `propext` | the count is additive over sequencing |
| `RevL.G5Classified.registrations_zero_iff` | G5 — the real count | **proved** | `propext, Classical.choice, Quot.sound` | zero registrations iff no call crosses |
| `RevL.G5Classified.inverse_reaches_no_emission` | G5 as `_check_witnessed_inverse` | **proved** | `propext, Quot.sound` | NO name transitively reachable from an admitted inverse is `emission` or `witnessed` |
| `RevL.G5Classified.admitted_inverse_registers_nothing` | G5 | **proved** | `propext, Classical.choice, Quot.sound` | the declared inverse itself registers zero crossings |
| `RevL.G5Classified.admitted_inverse_body_registers_nothing` | G5 — whole teardown | **proved** | `propext, Classical.choice, Quot.sound` | every call anywhere beneath the inverse, not only the immediate callee |
| `RevL.G5Classified.pureOnly_run` | G5 — over the step-2 semantics | **proved** | none | a pure-only run moves neither log nor world |
| `RevL.G5Classified.clean_inverse_run_logs_nothing` | G5 — operational | **proved** | `propext, Classical.choice, Quot.sound` | a teardown that registers nothing appends nothing to the WAL |
| `RevL.G5Classified.admitted_inverse_run_logs_nothing` | G5 — end to end | **proved** | `propext, Classical.choice, Quot.sound` | admitted program ⇒ its teardown runs without touching the WAL |
| `RevL.G5Classified.registrations_depends_on_its_argument` | G5 — anti-tautology guard | **proved** | none | refutes the review's probe `∀ u v, registrations u = registrations v` |
| `RevL.G5Classified.registrations_counts` | G5 — non-vacuity | **proved** | none | 0 for the host-local inverse, 1 for the emission inverse and its `fn`-wrapped twin |
| `RevL.G5Classified.sneaky_undo_is_refused` | G5 — non-vacuity (`sneakyUndo`) | **proved** | `propext` | clean program admitted, emission inverse and `fn`-wrapped inverse refused |
| `RevL.G5Classified.fold_must_run_to_stability` | G5 — the fuel caveat, named | **proved** | `propext` | the wrapped escape is admitted at fuel 0 and refused at fuel 1 |
| `RevL.G5Classified.sneaky_inverse_run_emits` | G5 — non-vacuity, operational | **proved** | `propext` | the refused inverse really takes an `emit` step and logs a one-way record |
| `RevL.G5Classified.clean_inverse_run_is_silent` | G5 — non-vacuity, operational | **proved** | `propext, Classical.choice, Quot.sound` | the admitted inverse takes a `pure` step and every run of it is silent |
| `RevL.G5Classified.g5_row_not_vacuous` | G5, oracle U5 row non-vacuity (issue 276) | **proved** | none | the fn-wrapped teardown registers 1, and 0 once the wrapping fn stops calling the emission, so the differential U5 row's count is mutation-sensitive |
| `RevL.G8Classified.surface_enumerates_reached_crossings` | G8 completeness over the lattice | **proved** | `propext, Quot.sound` | nothing a reachable crossing declares is dropped from the audit |
| `RevL.G8Classified.surface_only_declared_crossings` | G8 soundness over the lattice | **proved** | `propext, Quot.sound` | everything on the surface traces to a reachable crossing declaration — **no typing hypothesis** |
| `RevL.G8Classified.surface_implies_crossing` | G8 — the two folds agree | **proved** | `propext, Quot.sound` | a non-empty surface has a crossing classification behind it |
| `RevL.G8Classified.effect_carrying_emission_is_on_the_surface` | G8 — anti-tautology guard | **proved** | `propext, Quot.sound` | the two models disagree in BOTH directions on concrete statements |
| `RevL.G8Classified.surface_agrees_with_an_honest_marker` | G8 — non-vacuity | **proved** | `propext, Quot.sound` | where the `emit` marker is honest the two agree; `witnessed[db]` names its scope |
| `RevL.G8Classified.raw_leak_is_on_the_surface` | G8 — the dropped hypothesis | **proved** | `propext, Quot.sound` | a `raw` leak is enumerated without needing `Typed` to exclude it |
| `RevL.G8Classified.g8_surface_is_not_universal` | G8 — non-vacuity | **proved** | `propext, Quot.sound` | a body on the surface, a non-empty body off it |
| `RevL.G8Classified.witness_surface_traces_to_its_declaration` | G8 — non-vacuity | **proved** | `propext, Quot.sound` | the entry `db_insert` traces through a `fn` to the `emission` that owns it |
| `RevL.G8Classified.g8_row_not_vacuous` | G8, oracle S8 row non-vacuity (issue 276) | **proved** | `propext, Quot.sound` | the fn-wrapped pure statement has surface `db_insert`, and empty once the wrapping fn stops calling the emission, so the differential S8 row is mutation-sensitive |

| `RevL.G1.g1_not_vacuous` | G1, non-vacuity (item 418 step 8) | **proved** | `propext, Classical.choice, Quot.sound` | a component admitted under its own `requires`, and the same body neither admitted nor confined under a smaller manifest |
| `RevL.G3.g3_not_vacuous` | G3, non-vacuity (step 8) | **proved** | `propext, Quot.sound` | a link, a layering, a dependency path with a strict rank drop, and a genuine self-path on the cyclic composition |
| `RevL.G4.g4_shape_not_vacuous` | G4, non-vacuity (step 8) | **proved** | none | both conclusion branches inhabited, and the `raw` branch shown to close because `Typed` lacks a constructor |
| `RevL.G5.registrations_ignores_its_argument` | G5, the FINDING (step 8) | **proved** | none | the review's constant-function probe, proved, plus an undo body that calls an emission and still scores zero |
| `RevL.G6.g6_not_vacuous` | G6, non-vacuity (step 8) | **proved** | `propext, Classical.choice, Quot.sound` | an admitted statement with a non-empty reach surface, and a refused one |
| `RevL.G6.g6_row_not_vacuous` | G6, oracle C row non-vacuity (issue 276) | **proved** | `propext, Classical.choice, Quot.sound` | the confinement check refuses `leakStmt`'s undeclared head and accepts it once the context is extended, so the differential C row's verdict is mutation-sensitive |
| `RevL.G8.g8_marker_level_not_vacuous` | G8, non-vacuity (step 8) | **proved** | `propext, Quot.sound` | a typed body with a non-empty surface, beside the `effect` whose surface is empty by definition |
| `RevL.CrossTier.cross_tier_agreement` | item 133 — cross-tier agreement | **proved** | `propext` | any two `Conformant` profiles lower a `WellAnnotated` IR to the same `Value` |
| `RevL.CrossTier.six_tier_agreement` | item 133 — the six backends | **proved** | `propext` | the corollary over the six-element `Tier` |
| `RevL.CrossTier.annotation_necessary` | item 133 — anti-tautology | **proved** | none | python and typescript disagree on a bare literal, so the annotation hypothesis is load-bearing |
| `RevL.CrossTier.conformance_hypotheses_are_inhabited` | item 133, non-vacuity (step 8) | **proved** | `propext` | two conformant tiers and a well-annotated map IR agreeing on a re-ordered non-empty value |
| `RevL.CapCeilings.capceilings_hypotheses_are_inhabited` | items 294/66/260, non-vacuity (step 8) | **proved** | `propext, Classical.choice, Quot.sound` | a `Lineage` built by `Lineage.spawn` from a real program spawn edge, plus `Descends`, `Resolves`, `NameableEmission`, a counter run and a refused overdraw |
| `RevL.CapCeilings.ceiling_lineage_is_inhabited` | item 260, non-vacuity (step 8) | **proved** | `propext, Quot.sound` | a spawn edge narrowing a `calls` ceiling from 3 to 2, so the lineage and the ceiling side condition hold together |
| `RevL.G9.g9_context_hypotheses_are_inhabited` | G9 + G6, non-vacuity (step 8) | **proved** | `propext, Classical.choice, Quot.sound` | a `TypedIn` crossing with a non-empty head list, an untrusted-free scope and an untrusted one |
| `RevL.R4.r4_side_conditions_are_inhabited` | R4, non-vacuity (step 8) | **proved** | `propext, Quot.sound` | freshness and disjointness hold at both traces, with the emitted set empty on one and not the other |
| `RevL.A8.a8_hypotheses_are_inhabited` | A8, non-vacuity (step 8) | **proved** | `propext, Quot.sound` | `SemLog`, a fork-free log, a discharged seq, and a `WAFrom` run that really fires an undeclared inverse under a unique seq space |
| `RevL.A9.a9B_iff` | A9 — the oracle's decision (issues 1167 / #1172) | **proved** | `propext, Quot.sound` | `a9B i = true ↔ A9OK i`, both directions: the `A9` row prints the model's judgment |
| `RevL.A9.noDoubleInstallB_iff` | A9 — the uncoded sibling ("installed twice") | **proved** | `propext` | `noDoubleInstallB i = true ↔ NoDoubleInstall i` |
| `RevL.A9.installed_block_is_slot` | A9 — the bridge to G2/G3 | **proved** | `propext, Quot.sound` | under `A9OK` every installed block's `(key, realm key)` is a `slots` member of its component |
| `RevL.A9.undeclared_block_is_no_slot` | A9 — the refused shape | **proved** | `propext, Classical.choice, Quot.sound` | without `BlocksDeclared` (direction 1) some installed block is a slot of the component in NO realm |
| `RevL.A9.installed_block_uniquely_provided` | A9 + `LinkOK` | **proved** | `propext, Quot.sound` | in an admitted composition an installed block's slot is provided by its own component and by no deeper one |
| `RevL.A9.installed_slots_nodup` | A9 — no double install | **proved** | `propext, Quot.sound` | one block per slot: `NoDoubleInstall` makes the installed slots pairwise distinct |
| `RevL.A9.a9_not_vacuous` | A9, non-vacuity (direction 1) | **proved** | `propext, Quot.sound` | the first fixture refused; the renamed twin admitted and linking; the hint's second fix admitted once the orphan `skin1` is dropped, and refused by the converse when taken literally |
| `RevL.A9.a9_rules_are_distinct` | A9, anti-tautology | **proved** | `propext, Quot.sound` | `A9OK` and `NoDoubleInstall` separated in both directions |
| `RevL.A9.a9_row_not_vacuous` | A9, oracle row non-vacuity | **proved** | none | three same-manifest pairs with different verdicts (renamed block, added block, dropped route): the row reads the `PB` and `PR` facts, not the `C` fact |
| `RevL.A9.blocksDeclaredB_iff` | A9 — direction 1's decider (issue #1172) | **proved** | `propext` | `blocksDeclaredB i = true ↔ BlocksDeclared i` |
| `RevL.A9.declaredInstalledB_iff` | A9 — direction 2's decider (issue #1172) | **proved** | `propext` | `declaredInstalledB i = true ↔ DeclaredInstalled i` |
| `RevL.A9.declared_uninstalled_refused` | A9 — the converse (issue #1172, PR #1184) | **proved** | `propext` | a declared key that neither a block nor a route installs refuses the component |
| `RevL.A9.unrouted_needs_a_block` | A9 — the converse as stated for an ordinary provider | **proved** | `propext` | with no route, `A9OK` says exactly that every declared key has a block |
| `RevL.A9.routed_installs_without_block` | A9 — the `realms(...)` exemption | **proved** | `propext` | a component whose every declared key is routed satisfies A9 with no block (`RoundRobin`) |
| `RevL.A9.a9_converse_not_vacuous` | A9, non-vacuity (direction 2) | **proved** | none | the second fixture refused; `RoundRobin` admitted with no block; a backend with a block admitted |
| `RevL.A9.a9_directions_are_distinct` | A9, anti-tautology | **proved** | none | the two directions separated in both directions |

| `RevL.A2.a2Fold_iff` | A2 — the checker's fold (issue 1166) | **proved** | `propext, Quot.sound` | `lower._dispatch_action`'s fold with its flag SET is "no acquisition at all" plus the rule; the induction-friendly form behind the bridge |
| `RevL.A2.a2B_iff` | A2 — the bridge | **proved** | `propext, Quot.sound` | `a2B body = true ↔ A2OK body`: the printed `A2` verdict is exactly "no `provide` is followed by an `acquire`" |
| `RevL.A2.labels_distinct` | A2 — the two labels | **proved** | `propext, Classical.choice, Quot.sound` | a release and a withdrawal carry different labels in the `inverse` slot |
| `RevL.A2.stack_all_brackets` | A2 — the stack | **proved** | `propext, Quot.sound` | every entry the body registers is a `bracket`: releases and withdrawals replay under every settling verdict |
| `RevL.A2.phase1_of_brackets` | A2 over G7 | **proved** | `propext, Quot.sound` | under a settling verdict the Phase-1 pass over an all-bracket stack is the whole stack, LIFO |
| `RevL.A2.stack_of_no_acquire` | A2 — the stack | **proved** | `propext` | a body with no acquisition registers only withdrawals |
| `RevL.A2.stack_shape` | A2 — the rule as a stack shape | **proved** | `propext` | under `A2OK` the stack is every release, then every withdrawal, with the body's own counts |
| `RevL.A2.proof_pass_is_withdrawals_then_releases` | A2 — the content theorem | **proved** | `propext, Quot.sound` | under `A2OK` and any settling verdict `phase1` runs every withdrawal, then every release. The settling hypothesis is load-bearing: the E-Stop runs nothing |
| `RevL.A2.withdrawals_precede_releases` | A2 — as the guarantee reads | **proved** | `propext, Classical.choice, Quot.sound` | `List.Pairwise`: once a release has run, nothing that runs after it is a withdrawal, so no provide-method is callable against a released handle |
| `RevL.A2.teardown_labels` | A2 — the D-row reading | **proved** | `propext, Classical.choice, Quot.sound` | the model's `teardown` of the body's stack is the withdrawal labels then the release labels; no compensation drain |
| `RevL.A2.fixture_refused` | A2 — non-vacuity | **proved** | none | `a2_acquire_after_provide.rvl`'s shape `[acquire, provide, acquire]` is refused by the fold |
| `RevL.A2.fixture_release_before_withdrawal` | A2 — the converse, computed | **proved** | `propext` | on the fixture's stack every settling verdict runs a release BEFORE the withdrawal: the window `docs/rejections.md#a2` describes |
| `RevL.A2.fixture_opens_the_window` | A2 — the converse | **proved** | `propext` | the fixture's pass violates the ordering claim, so the `A2OK` hypothesis excludes a real body rather than nothing |
| `RevL.A2.a2_not_vacuous` | A2 — non-vacuity | **proved** | `propext, Quot.sound` | `[acquire, provide]`: admitted by fold and rule, stack `[release, withdrawal]`, commit and abort passes `[withdrawal, release]`, halted pass empty |
| `RevL.G4Deferred.legalB_iff` | G4 deferred position — one reach (issue #1742) | **proved** | `propext` | `legalB r = true ↔ Legal r`: a reach of a `deferred` extern is legal exactly when it is a call (bare or in an arrow) in a component |
| `RevL.G4Deferred.deferredB_iff` | G4 deferred position — the bridge | **proved** | `propext, Quot.sound` | `deferredB rs = true ↔ DeferredOK rs`: the printed `DF` verdict is exactly "every reach is legal" |
| `RevL.G4Deferred.value_never_legal` | G4 deferred position — values | **proved** | none | the extern as a function value is refused in every scope, a component included |
| `RevL.G4Deferred.body_reach_refused` | G4 deferred position — `fn`/`test` bodies | **proved** | none | any reach in a `fn` or `test` body is refused, inside an arrow or not: neither has a session commit |
| `RevL.G4Deferred.refused_of_mem` | G4 deferred position — the file rule | **proved** | none | one illegal reach refuses the file wherever it sits |
| `RevL.G4Deferred.fixtures_decided` | G4 deferred position — the corpus shapes | **proved** | `propext` | `ok_emit_step` admitted; a call in a `fn` body, a call in an arrow there and a component value refused |
| `RevL.G4Deferred.deferred_not_vacuous` | G4 deferred position — non-vacuity | **proved** | `propext, Quot.sound` | the rule admits the component call and refuses the `fn`-body call and the component value |
| `RevL.G4Approval.coversB_iff` | G4 approval floor — coverage (issue #1455) | **proved** | `propext, Classical.choice, Quot.sound` | `coversB scope token = true ↔ Covers scope token`: exact match, or the glob scope matches (`lower._approval_covers`) |
| `RevL.G4Approval.edgeCoversB_iff` | G4 approval floor — the edge | **proved** | `propext, Classical.choice, Quot.sound` | no edge covers nothing; an edge covers exactly what its scope covers |
| `RevL.G4Approval.crossingB_iff` | G4 approval floor — the bridge | **proved** | `propext, Classical.choice, Quot.sound` | `crossingB required c = true ↔ CrossingOK required c`: the printed `AP` verdict is exactly the rule |
| `RevL.G4Approval.no_edge_iff_nothing_required` | G4 approval floor — the value form | **proved** | `propext, Classical.choice, Quot.sound` | a crossing with no edge is admitted exactly when it reaches no approval-required token, so `let r = emit charge(1)` is refused for an approval-required `charge` however it is written |
| `RevL.G4Approval.uncovered_required_refused` | G4 approval floor — the refusal | **proved** | `propext, Classical.choice, Quot.sound` | a required token the edge does not cover refuses the crossing, whatever else it reaches |
| `RevL.G4Approval.unrequired_needs_no_edge` | G4 approval floor — keyed by token | **proved** | `propext, Classical.choice, Quot.sound` | a crossing that reaches no required token needs no edge, whichever extern or operation it goes through |
| `RevL.G4Approval.covering_edge_admits` | G4 approval floor — admission | **proved** | `propext, Classical.choice, Quot.sound` | an edge covering every required token the crossing reaches admits it |
| `RevL.G4Approval.globMatch_star_any` | G4 approval floor — the prefix glob | **proved** | `propext` | a trailing `*` matches any rest, the shape of `approval["production.*"]` |
| `RevL.G4Approval.approval_not_vacuous` | G4 approval floor — non-vacuity | **proved** | `propext, Classical.choice, Quot.sound` | `g4_approval_scoped_extern.rvl` refused, admitted under the exact and the glob edge, refused under `staging.*`; `g4_approval_compensate_other_edge.rvl`'s head admitted and compensation refused; an unrequired head admitted with no edge |
| `RevL.G4Approval.approval_row_not_vacuous` | G4 approval floor — the row | **proved** | `propext, Classical.choice, Quot.sound` | the verdict moves with the edge alone, the required set alone and the token alone, so each of `AE`, `AR` and `AX` is load-bearing |
| `RevL.G6Binding.okB_iff` | G6 binding uniqueness — the frame walk (issue #1812) | **proved** | `propext, Quot.sound` | `okB fs evs = true ↔ ScopeOK fs evs`: the decider over the frames of names in view is the inductive rule |
| `RevL.G6Binding.bindingB_iff` | G6 binding uniqueness — the bridge | **proved** | `propext, Quot.sound` | `bindingB seed evs = true ↔ BindingOK seed evs`: the printed `BU` verdict is exactly the rule |
| `RevL.G6Binding.visibleB_iff` | G6 binding uniqueness — in view | **proved** | `propext, Quot.sound` | a name is in view exactly when some open frame holds it |
| `RevL.G6Binding.seed_rebind_refused` | G6 binding uniqueness — the seed | **proved** | none | a name in view when the scope starts cannot be bound: a method `let` reusing an activation binding or a parameter, an activation binding reusing a `requires` local |
| `RevL.G6Binding.rebind_refused` | G6 binding uniqueness — twice in a row | **proved** | `propext` | a name bound twice in a row is refused, in any frames |
| `RevL.G6Binding.inner_shadow_refused` | G6 binding uniqueness — blocks see outward | **proved** | `propext` | rebinding an outer name inside a block is refused |
| `RevL.G6Binding.fixtures_decided` | G6 binding uniqueness — the corpus shapes | **proved** | none | `g6_method_local_shadows_component.rvl`'s method refused and its unseeded twin admitted; sibling arms reusing a name admitted; a `let` then a `for` binder of the same name refused |
| `RevL.G6Binding.binding_not_vacuous` | G6 binding uniqueness — non-vacuity | **proved** | `propext, Quot.sound` | the rule refuses the corpus shape and admits the twin and the sibling arms, so it is neither always true nor always false |
| `RevL.G1Access.accessB_iff` | G1 declared access — the bridge (issue #1807) | **proved** | `propext, Quot.sound` | `accessB declared roots = true ↔ AccessOK declared roots`: the printed `G1` verdict is exactly the rule |
| `RevL.G1Access.undeclared_refused` | G1 declared access — the refusal | **proved** | none | one access root that is not a declared requirement refuses the component |
| `RevL.G1Access.access_mono` | G1 declared access — monotone | **proved** | none | declaring more requirements never refuses |
| `RevL.G1Access.declaring_admits` | G1 declared access — the fix | **proved** | none | declaring the missing key admits what it refused |
| `RevL.G1Access.fixtures_decided` | G1 declared access — the corpus shapes | **proved** | none | `g1_undeclared_access.rvl`'s `Logger` refused, and admitted with `db` declared |
| `RevL.G1Access.g1_access_not_vacuous` | G1 declared access — non-vacuity | **proved** | `propext, Quot.sound` | the rule refuses the undeclared access and admits the declared one |
| `RevL.A1Async.reachB_iff` | A1 async colour — the reach (issue #1808) | **proved** | `propext, Quot.sound` | `reachB g as k n = true ↔ ReachesAsync g as k n`: a name is async, or calls through the `fn` graph one that reaches async with one step less |
| `RevL.A1Async.reach_mono` | A1 async colour — fuel | **proved** | none | more fuel never loses a reach |
| `RevL.A1Async.reaches_iff` | A1 async colour — a site's heads | **proved** | `propext, Quot.sound` | a site reaches async exactly when one of its heads does |
| `RevL.A1Async.siteB_iff` | A1 async colour — the bridge | **proved** | `propext, Quot.sound` | `siteB … = true ↔ SiteOK …`: the printed `A1` verdict is exactly the per-site rule |
| `RevL.A1Async.teardown_suspension_refused` | A1 async colour — teardown | **proved** | `propext` | an `undo` or `compensate` slot that reaches async is refused |
| `RevL.A1Async.await_without_async_refused` | A1 async colour — decoration | **proved** | `propext` | an awaited step that reaches nothing async is refused |
| `RevL.A1Async.await_pairing_exact` | A1 async colour — exact pairing | **proved** | `propext` | the same heads are admitted under exactly one of a step and its awaited form |
| `RevL.A1Async.sigB_iff` | A1 async colour — the signature | **proved** | `propext` | the printed `A1S` verdict is exactly "the implementation's colour is the declaration's" |
| `RevL.A1Async.fixtures_decided` | A1 async colour — the corpus shapes | **proved** | `propext` | the undo, effect, sync-method, emit and signature shapes of the A1 fixtures, decided both ways |
| `RevL.A1Async.a1_not_vacuous` | A1 async colour — non-vacuity | **proved** | `propext, Quot.sound` | the reach goes through a `fn`, the awaited acquisition is admitted and its unawaited twin refused, a suspending `undo` refused and a synchronous one admitted |
| `RevL.Prelude.preludeB_iff` | prelude ordering — the bridge (issue #1809) | **proved** | `propext, Quot.sound` | the printed `PL` verdict is exactly "no prelude after the first action" |
| `RevL.Prelude.prelude_after_action_refused` | prelude ordering — the refusal | **proved** | none | an `isolate`/`intercept`/`handoff`/route after an action is refused |
| `RevL.Prelude.preludes_first_admitted` | prelude ordering — admission | **proved** | `propext, Quot.sound` | any number of leading preludes before actions is admitted |
| `RevL.Prelude.interceptB_iff` | intercept target — the bridge | **proved** | `propext, Quot.sound` | the printed `IC` verdict is exactly "no intercept of a provision that is not required" |
| `RevL.Prelude.intercept_provision_refused` | intercept target — the refusal | **proved** | none | an intercept of a pure provision is refused |
| `RevL.Prelude.methodB_iff` | A6 method in service — the bridge | **proved** | `propext, Quot.sound` | the printed `MS` verdict is exactly "every named operation is declared by its service" |
| `RevL.Prelude.undeclared_method_refused` | A6 method in service — the refusal | **proved** | none | one undeclared operation refuses the component |
| `RevL.Prelude.fixtures_decided` | the three declaration rules — the corpus shapes | **proved** | none | `v2_isolate_after_effect`, `v2_intercept_on_provision` and `a6_method_not_in_service` refused, their twins admitted |
| `RevL.Prelude.prelude_rules_not_vacuous` | the three declaration rules — non-vacuity | **proved** | `propext, Quot.sound` | each rule refuses its corpus shape and admits the twin |
| `RevL.ModelPlace.placeB_iff` | G-MODEL-PLACE placement — the bridge (issue #1811) | **proved** | `propext, Quot.sound` | the printed `MPV` verdict is exactly "every confidentiality origin is placed on the device" |
| `RevL.ModelPlace.off_device_refused` | G-MODEL-PLACE placement — the refusal | **proved** | none | a confidentiality origin placed off the device refuses the component |
| `RevL.ModelPlace.open_origin_anywhere` | G-MODEL-PLACE placement — open origins | **proved** | `propext` | an origin that is not a confidentiality origin may be placed anywhere |
| `RevL.ModelPlace.on_device_admitted` | G-MODEL-PLACE placement — admission | **proved** | none | placing every arm on the device is always admitted |
| `RevL.ModelPlace.fixtures_decided` | G-MODEL-PLACE placement — the corpus shapes | **proved** | none | `gmodelplace_confidential_off_device` and the council-member fixture refused, their on-device and open-origin twins admitted |
| `RevL.ModelPlace.placement_not_vacuous` | G-MODEL-PLACE placement — non-vacuity | **proved** | `propext, Quot.sound` | the rule refuses the off-device placement and admits the on-device one |
| `RevL.ModelCouncil.splitB_iff` | G-COUNCIL-SPLIT tie policy — the bridge (issue #1811) | **proved** | `propext, Quot.sound` | the printed `CTV` verdict is exactly "no declared council admits when its members disagree" |
| `RevL.ModelCouncil.admitting_refused` | G-COUNCIL-SPLIT tie policy — the refusal | **proved** | none | a council whose `on_tie` is an admitting outcome refuses the file |
| `RevL.ModelCouncil.split_admitted` | G-COUNCIL-SPLIT tie policy — the default | **proved** | `propext, Quot.sound` | `on_tie split`, the default an omitted clause resolves to, is admitted |
| `RevL.ModelCouncil.deny_admitted` | G-COUNCIL-SPLIT tie policy — the narrowing | **proved** | `propext, Quot.sound` | `on_tie deny` is admitted, so the rule narrows the tie policy without banning it |
| `RevL.ModelCouncil.unknown_tie_not_this_row` | G-COUNCIL-SPLIT tie policy — what it does not decide | **proved** | `propext, Quot.sound` | a spelling outside both vocabularies is admitted here; the unknown-tie rule refuses it, and conflating the two would make this row claim a judgment it does not make |
| `RevL.ModelCouncil.fixtures_decided` | G-COUNCIL-SPLIT tie policy — the corpus shapes | **proved** | none | `gcouncilsplit_on_tie_allow` refused, the `on_tie split` and `on_tie deny` shapes admitted |
| `RevL.ModelCouncil.council_not_vacuous` | G-COUNCIL-SPLIT tie policy — non-vacuity | **proved** | `propext, Quot.sound` | the rule refuses the admitting tie and admits `split` and `deny`, so the printed verdict is mutation-sensitive in both directions |
| `RevL.G9Flow.admitsB_iff` | G9 / G-SECRET-FLOW on the corpus — the bridge (issue #1811 group 2) | **proved** | `propext, Classical.choice, Quot.sound` | `admitsB` decides `RevL.Lemmas.Admits` at all four sink classes, so the printed `TAINT` verdict is exactly the rule |
| `RevL.G9Flow.g9RowB_iff` | G9 / G-SECRET-FLOW on the corpus — the row's verdict | **proved** | `propext, Classical.choice, Quot.sound` | the row prints `ok` precisely where `Admits` holds and `fail` precisely where it is violated, so a `fail` is the rule VIOLATED at a sink the checker itself reported — the refusal explained, not contradicted |
| `RevL.G9Flow.reported_walk_reaches_the_reported_label` | G9 / G-SECRET-FLOW on the corpus — what route B is | **proved** | `propext` | every reported walk reaches the label it reported, at ANY reported length: the hop column cannot move the verdict. **This is not a claim that the reported length is the real one** |
| `RevL.G9Flow.escapeFlow` | G9 / G-SECRET-FLOW on the corpus — the escape | **proved** | `propext` | the declassifier the checker names can be appended to the reported walk and lands on `applyD` of the entry label, which is what the row's "route it through a declared point" advice claims |
| `RevL.G9Flow.labelOfString_is_the_label` | G9 / G-SECRET-FLOW on the corpus — the origins column | **proved** | `propext` | the empty string is the empty label, the corpus labels are the origins their names denote, and an unknown origin name is `none` — which emits no row and lands the refusal in the fatal `missed-G9` |
| `RevL.G9Flow.corpus_rows_decided` | G9 / G-SECRET-FLOW on the corpus — the corpus shapes | **proved** | `propext` | the `fs`-at-authority and `confidential`-at-disclosure shapes refused, the same two sinks on a clean label admitted |
| `RevL.G9Flow.escape_moves_the_label` | G9 / G-SECRET-FLOW on the corpus — the escape is not decorative | **proved** | `propext` | `endorse[fs]` on an `fs` label leaves the empty label, which the authority sink admits where it refused the original |
| `RevL.G9Flow.g9_not_vacuous` | G9 / G-SECRET-FLOW on the corpus — non-vacuity | **proved** | `propext` | both corpus sink kinds refused and both clean twins admitted, so the `TAINT` row is mutation-sensitive in both directions |
| `RevL.GRetain.holdsB_iff` | G-RETAIN on the corpus — the rule (issue #1811 group 3) | **proved** | `propext` | `holdsB` is `now ≤ deadline`, or anything at all under a declared hold: the checker's strict `as_of > until` with the `hold` override, and the boundary is the deadline itself |
| `RevL.GRetain.rowB_iff` | G-RETAIN on the corpus — the row's verdict | **proved** | `propext, Classical.choice, Quot.sound` | the row prints `ok` precisely where the rule holds and `fail` precisely where it is violated, so a `fail` is the rule VIOLATED at a persistence sink the checker itself reported, at the instant the checker itself compared |
| `RevL.GRetain.hold_clears_the_deadline` | G-RETAIN on the corpus — the escape | **proved** | `propext` | a declared hold holds at every instant, which is the third escape the checker's own hint names; the same row without it is refused, so the escape is not the default |
| `RevL.GRetain.reaches_iff` | G-RETAIN on the corpus — the walk premise | **proved** | `propext, Classical.choice, Quot.sound` | `reaches` decides the row's `chain` column: the reported chain ends at the reported sink and not at another, so the column moves the verdict rather than decorating it |
| `RevL.GRetain.corpus_sink_scope_is_modelled` | G-RETAIN on the corpus — the corpus scope | **proved** | none | the corpus scope `db` is in the row's table, so the exporter emits a row for the corpus refusal |
| `RevL.GRetain.unmodelled_sink_scope_has_no_sink` | G-RETAIN on the corpus — the other direction | **proved** | none | a scope outside the checker's ten yields none, so no row is emitted and the refusal lands in the fatal `missed-G-RETAIN` rather than in an agreement |
| `RevL.GRetain.corpus_walk_does_not_reach_another_sink` | G-RETAIN on the corpus — the corpus chain | **proved** | `propext, Classical.choice, Quot.sound` | the corpus chain `load() -> db_put` does not reach `fs_put`, so the walk premise is mutation-sensitive |
| `RevL.GRetain.corpus_row_decided` | G-RETAIN on the corpus — the corpus shapes | **proved** | `propext, Classical.choice, Quot.sound` | the corpus row refused at the checker's instant and admitted AT the deadline and a year before it, with the `until` column the same in all three |
| `RevL.GRetain.retain_not_vacuous` | G-RETAIN on the corpus — non-vacuity | **proved** | `propext, Classical.choice, Quot.sound` | the corpus row's own columns decided at four instants — refused past the deadline, admitted at it, admitted before it, admitted under a hold — so the `RETAIN` row is mutation-sensitive in every direction and a row that ignored `now` fails here |
| `RevL.G4Inverse.rowB_iff` | G4 inverse on the corpus — the row's verdict (issue #2097) | **proved** | `propext` | the row prints `ok` precisely where the rule holds and `fail` precisely where it is violated, so a `fail` is the rule VIOLATED at a site the checker itself reported, at an acquisition and a demanded spelling the checker itself named |
| `RevL.G4Inverse.requiredInverse_none_off_the_tables` | G4 inverse on the corpus — the two tables | **proved** | `propext` | the lookup is `none` exactly off the host and write tables, and never on the extern arm (whose requirement is the acquisition's own declaration), so an acquisition outside the tables yields no verdict at all |
| `RevL.G4Inverse.extern_arm_is_the_declared_inverse` | G4 inverse on the corpus — the extern arm has no table | **proved** | none | `requiredInverse .extern` is the verb the checker named, at every acquisition and verb: that arm's content is the site comparison, which is the honest reading of `lower._check_extern_release` (`inv` comes from `env.extern_inverse`) |
| `RevL.G4Inverse.extern_arm_never_falls_off_a_table` | G4 inverse on the corpus — the extern arm cannot be skipped | **proved** | `propext` | `requiredInverse .extern` is never `none`, at an acquisition that IS off both other tables, so the extern arm cannot be silently dropped the way a table miss drops a row |
| `RevL.G4Inverse.host_release_table` | G4 inverse on the corpus — the host table | **proved** | none | the checker's `_HOST_ACQUIRE_VERBS`, all three entries: `Map.new` → `drop`, `Pool.open` → `close`, `Stream.source` → `close` |
| `RevL.G4Inverse.write_inverse_table` | G4 inverse on the corpus — the write table | **proved** | none | the checker's `_HOST_WRITE_INVERSE`, all three entries: `Map.insert` → `remove`, `Map.insert_if_absent` → `remove`, `Map.remove` → `insert` |
| `RevL.G4Inverse.kindOfString_is_the_kind` | G4 inverse on the corpus — the arm column | **proved** | none | the exporter's three arm names are the three the row carries, so no arm can silently miss and leave the row undecided |
| `RevL.G4Inverse.unmodelled_acquisition_has_no_inverse` | G4 inverse on the corpus — the other direction | **proved** | none | `Log.open` is off the host release table and `Map.get` off the write inverse table, so a checker that began refusing under a family or a write outside these tables yields no row and the refusal lands in the fatal `missed-G4` rather than in an agreement |
| `RevL.G4Inverse.corpus_rows_refused` | G4 inverse on the corpus — the corpus shapes | **proved** | none | the four documents' own columns refused: the extern declaration's inverse against `log_flush()`, the host family's release against `store.get("x")`, and `Map.insert`'s inverse against two wrong-key sites |
| `RevL.G4Inverse.corpus_rows_flip_at_the_demanded_spelling` | G4 inverse on the corpus — the corpus flip | **proved** | none | each of the four rows is admitted at the spelling the checker demanded with nothing but the `site` column moved, so the four refusals are a verdict about the site rather than a constant |
| `RevL.G4Inverse.corpus_host_row_flips_when_the_demand_leaves_the_table` | G4 inverse on the corpus — the host arm reads its table | **proved** | none | the same site spelling against a demand that has left `hostRelease` is refused, so the row is not merely comparing the checker's advice with itself |
| `RevL.G4Inverse.corpus_write_row_flips_when_the_receiver_moves` | G4 inverse on the corpus — the write arm reads its receiver | **proved** | none | the demanded verb and key on a SIBLING handle is still refused, so issue #1859's "on THAT handle" is carried by the spelling rather than assumed |
| `RevL.G4Inverse.corpus_row_verbs_are_the_tables` | G4 inverse on the corpus — the verb compared against | **proved** | none | the requirement is the table's (or the declaration's) at all three arms on the corpus acquisitions, so the row's premise is jointly satisfiable at each arm |
| `RevL.G4Inverse.g4_inverse_not_vacuous` | G4 inverse on the corpus — non-vacuity | **proved** | none | the four corpus shapes refused at the reported site and admitted at the demanded spelling, plus a demand off `hostRelease` and a sibling receiver: the `INV` row is mutation-sensitive to the site, its receiver, its key and the tables |
| `RevL.G4Witnessed.legalB_iff` | G4 witnessed site `undo` on the corpus — the rule (issue #2098) | **proved** | none | `legalB` is "not a witnessed head, or no site `undo`": the checker's own guard (`wit_name in env.witnessed_externs and undo_expr is not None`), decided at both polarities and at the boundary |
| `RevL.G4Witnessed.swB_iff` | G4 witnessed site `undo` on the corpus — the row's verdict | **proved** | none | the row prints `ok` precisely where every witnessed site in the file satisfies the rule and `fail` precisely where one violates it, so a `fail` is the rule VIOLATED at a site the export carries |
| `RevL.G4Witnessed.legalCols_iff` | G4 witnessed site `undo` on the corpus — the columns | **proved** | none | `legalCols` decides the rule at the row's own columns, and a classification the model does not name is `false` rather than vacuously `true`, so a row the model cannot read is refused rather than agreed with |
| `RevL.G4Witnessed.unmodelled_classification_is_refused` | G4 witnessed site `undo` on the corpus — the fail-safe | **proved** | none | an unreadable classification is refused both with and without a site `undo`, so the fail-safe cannot be mistaken for the admitted twin's `ok` |
| `RevL.G4Witnessed.other_head_never_refused` | G4 witnessed site `undo` on the corpus — the other direction | **proved** | none | a non-`witnessed` head is admitted whatever its site spells, so the corpus refusal is scoped to the classification and not to the mere presence of an `undo` |
| `RevL.G4Witnessed.witnessed_undo_refused` | G4 witnessed site `undo` on the corpus — the refusal | **proved** | none | a `witnessed` head with any non-empty site `undo` is refused, whatever the head is named; the admitted twin shows the refusal is not universal over `witnessed` sites |
| `RevL.G4Witnessed.refused_of_mem` | G4 witnessed site `undo` on the corpus — the per-file verdict | **proved** | none | one illegal site refuses the whole file wherever it sits in the list, which is what makes the per-file `SW` verdict sensitive to a single site rather than to the file's shape |
| `RevL.G4Witnessed.corpus_columns_decided` | G4 witnessed site `undo` on the corpus — the corpus columns | **proved** | none | the corpus columns: `witnessed`+`unput` refused, `witnessed`+no-`undo` admitted, `emission`+`unput` admitted |
| `RevL.G4Witnessed.corpus_sites_decided` | G4 witnessed site `undo` on the corpus — the corpus shapes | **proved** | none | the corpus file shapes: both refused files' sites `false`, the admitted twin and the verified witnessed method `true` |
| `RevL.G4Witnessed.corpus_flip_is_the_undo_column` | G4 witnessed site `undo` on the corpus — the flip | **proved** | none | the refused and admitted corpus shapes share head and classification and differ only in the `undo` column, so no `witnessed` extern is refused for being witnessed |
| `RevL.G4Witnessed.witnessed_site_undo_not_vacuous` | G4 witnessed site `undo` on the corpus — non-vacuity | **proved** | none | the corpus's own columns decided at four polarities and the refused shape ADMITTED under each of the two mutations (`undo` emptied, classification moved off `witnessed`), so the `SW` row is mutation-sensitive in every direction |
(`propext` / `Quot.sound` are Lean's standard foundation axioms; the gate
whitelists exactly those three.)

### G2/G3 restated over `(key, realm)` slots (roadmap item 418, step 1)

`RevL.Manifest` used to model G2 as `Nodup (flatMap provides)` over bare
keys and let a component satisfy its own requirement. Both were wrong
against the compiler:

- revl's G2 is per `(key, realm)` — `diagnostics.GUARANTEES["G2"]` reads
  "one provider per key (per realm)" and the linker's `provider_of` table
  is keyed on the pair, with the realm read from the component's
  `isolate` clauses. The model refused `examples/tenants.rvl`, which the
  compiler accepts and whose own header states the real rule. `LComponent`
  now carries a `realm : String → String` field (defaulting to
  `sharedRealm`, so a realm-free component literal is unchanged), and
  `slots`/`needs`/`DependsOn`/`DepPath`/`LayeredBy`/`ProvidesDisjoint`/
  `RequiresClosed`/`LinkOK` are all stated over slots. Lifting the
  *dependency* relation too is forced, not cosmetic: with two realms of
  one key, no key-indexed rank function can be a layering.
- `LinkOK` now requires each component's consumed slots to be provided
  **strictly deeper** in the list, not in `c :: comps`. That is the
  linker's "component N requires a key it provides itself (`k`) (G3)"
  refusal, and transitively its cycle refusal: `LinkOK comps` says
  `comps` is a valid reverse-`loadOrder` presentation, and a program
  links iff *some* ordering derives it.

The point of the second change is `RevL.G3.linkOK_layered`, the bridge
`LinkOK comps → ∃ rank, LayeredBy comps rank`. Before it,
`no_dependency_cycles`' layering hypothesis was not establishable from
anything the model admitted, so "cycles rejected" had no proof path;
`RevL.G3.linkOK_no_cycles` now states G3 with no layering assumed.

### G7 restated over the three-kind teardown stack (roadmap item 418, step 5)

`RevL.Semantics` used to carry one entry kind and define

    teardown log = log.reverse.map (·.inverse)

so `G7.teardown_replays_all` said, with no hypothesis, that every
accumulated inverse is replayed on every teardown. Item 418's C3 recorded
that as *false of revl*, and it was:
`backends/python/runtime.py`'s `_Transactional.__call__` reads the owning
frame's commit bit and, on a clean COMMIT, **discharges** the entry. The
inverse never runs and the witness is dropped, because the mutation is the
deliverable. Only an ABORT replays it.

L0 now carries what `docs/design/teardown-contract.md` specifies, the
table under "the three entry kinds, one stack" and the two-phase algorithm
under "the teardown algorithm":

| | `bracket` | `transactional` (243) | `compensation` (247) |
|---|---|---|---|
| clean commit | replays | discharged | discharged |
| abort | replays, Phase 1 | replays, Phase 1 | runs in Phase 2 |

`EntryKind`, `Verdict`, `EntryKind.replaysUnder`, `phase1`, `phase2`,
`replayed`, `discharged` and `teardown` are the model; `G7.replay_table`
pins the six cells so a change to the rule has to face them. The
guarantees are then stated relative to the kind and the verdict:
completeness and soundness over the entries the verdict replays
(`replayed_complete` / `replayed_sound`, and their inverse-level forms
`teardown_replays_all` / `teardown_only_witnessed`), the per-kind rows
against the runtime (`commit_discharges_transactional`,
`commit_discharges_compensation`, `commit_replays_only_brackets`,
`abort_replays_every_transactional`,
`bracket_replays_under_every_settling_verdict`), the LIFO equation per
phase, the
phase split (`compensations_drain_after_the_proof_pass`, the contract's
"all Phase-1 inverses complete before any compensation starts"), and LIFO
within a phase.

The witness is one activation stack carrying all three kinds, with the
acquisition registered first, the witnessed mutation second and the
compensation LAST. A verdict-blind teardown would replay the mutation on a
clean commit; a single-phase LIFO walk would fire the compensation first.
Neither happens: the commit replay is exactly `[release_handle]` and the
abort replay is exactly `[db_delete, release_handle, send_apology]`.

**Deliberately not modelled**, so the reach of these theorems is not
overstated: Phase-1 continue-and-record and its two residue severities
(`bracket-fault` / `restore-residue`), the Phase-2 budget and its
`compensation-residue` skips, deferred method-registered entries
(`_deferred_transactional`, item 318), escrow under a pending session
verdict (item 245), and cascading abort across activations. This model
says which entries run and in what order, not what happens when one of
them fails. The failure and crash side lives in
`RevL.Theorems.A8_WalDischarge` and `RevL.Theorems.R4_NoResidue`, over the
WAL rather than over the stack.

### G7 grows a third verdict: the operator E-Stop (roadmap item 443)

`docs/design/443-estop.md`. `Verdict` gains `halted`, and it is not a
relabelling of `abort`: it replays nothing, discharges nothing, and leaves
every registered entry **owed**. That is a third disposition, STRANDED —
registered, not run, not dropped — beside replayed and discharged.

| | `bracket` | `transactional` | `compensation` |
|---|---|---|---|
| clean commit | replays | discharged | discharged |
| abort | replays, Phase 1 | replays, Phase 1 | runs in Phase 2 |
| **halted (E-Stop)** | **stranded** | **stranded** | **stranded** |

The accounting is what makes the third column honest rather than a hole:
`disposition_trichotomy` proves every (kind, verdict) pair has EXACTLY one
disposition, and `book_lengths_add` proves replayed + discharged +
stranded is the whole stack, under every verdict. An entry cannot fall off
the books by being halted. The halt can also arrive *during* a teardown
that was already running, and that is a cut into the interrupted replay
order: `halt_inventory_is_total` partitions it into completed / ambiguous
/ unattempted, and `halt_ambiguity_is_at_most_one` bounds the ambiguity at
the single crossing that was in flight (item 440's tier, reached
deliberately rather than by accident).

**Two theorems gained a hypothesis, and this is the audit of it.** Adding
a hypothesis to rescue a proof is the standard way a formal layer stops
meaning what its name says, so the added `v.settles = true` on
`Semantics.replays_or_discharges` and on the theorem now called
`G7.bracket_replays_under_every_settling_verdict` is itself pinned by
theorems rather than by this paragraph:

- `settles_iff_not_halted` — the hypothesis excludes **exactly** the
  E-Stop and no other verdict, so the gap it leaves is a single case.
- `settles_iff_strands_nothing` — and it excludes it for the right
  reason: a verdict settles precisely when it strands no kind. `settles`
  is the property the replay/discharge dichotomy needs, spelled as a
  predicate, not a side condition chosen to make a proof go through.
- `settling_strands_nothing` — under a settling verdict the inventory is
  empty for **every** stack, which with `estop_strands_everything` is the
  whole of `stranded`: empty under `commit` and `abort`, the whole stack
  under `halted`.
- `bracket_replays_exactly_when_settling` — the bracket row is not merely
  *true* under the settling verdicts, it **equals** `settles`, with no
  hypothesis at all. So the restricted corollary is weaker than something
  proved, not weaker than something claimed, and its hypothesis is the
  exact condition its conclusion needs.
- `bracket_is_replayed_or_stranded` — the total form over every verdict,
  hypothesis-free: a registered bracket is replayed, or the verdict is the
  E-Stop and the bracket is on the inventory.

Neither restriction weakened G7 itself. `teardown_replays_all` and
`teardown_only_witnessed` — completeness and soundness, the two statements
G7 *is* — were already relative to `replaysUnder v` and carry no verdict
hypothesis at all; they hold under `.halted` unchanged, with an empty
replay set.

**The E-Stop is the counterpart of R4, not a hole in it.** R4 is "no
residue"; the halt is "all residue, all of it reported"
(`estop_strands_everything`). Deliberately NOT modelled here: what the
operator does with the inventory, the reconciliation path itself (`revl
recover` reading the descriptors back), and whether the latch is observed
promptly. This model says what a halt owes, not how the debt is settled.

## Item 133 — cross-tier agreement (`RevL.Theorems.CrossTier`)

`RevL.CrossTier` models each of the six backends by its *observable
profile* on the three DIVERGENCES axes — the numeric tag an unannotated
literal defaults to, the string unit, and the map iteration order — and
lowers a small value IR (`Atom`/`Value`, numeric literals carrying an
optional operand annotation) through a profile with `eval`.

The theorem states exactly the item-133 conditions and proves they
suffice: `cross_tier_agreement` shows any two `Conformant` profiles (code-
point string unit + canonical map order) lower a `WellAnnotated` IR (every
numeric literal operand-annotated) to the *same* `Value`;
`six_tier_agreement` is the corollary over the six-element `Tier`. The
numeric default is deliberately left free per tier, and
`annotation_necessary` exhibits python vs typescript disagreeing on a bare
literal — so the annotation hypothesis is load-bearing, not vacuous. No
`sorry`, no project axioms.

Deliberately out of scope (documented, not smuggled as axioms): that the
real emitters realise a conformant profile is the differential conformance
matrix's empirical obligation, not a Lean theorem; and map values are
modelled one level deep (nested maps are a mechanical extension of
`entries_agree`).

## G9 — untrusted data gains no authority (`RevL.Theorems.G9_NoAuthorityFromUntrusted`)

`src/revl/taint.py`, first line: "untrusted input cannot DIRECTLY create
authority." The model: a `Label` is a set of `Origin`s (the closed class
set `taint._ORIGIN_CLASSES`, plus `Origin.custom` for `_origin_of`'s
unclassified-scope residual), ordered by inclusion with bottom `{}` =
trusted and join = union. A `Flow` is one data-flow path as a list of
`Step`s: a crossing minting its *derived* origin, a join, an opaque hop,
and the one weakening step — an admitted `Declassifier`. Every label
operation the reference performs is one of those four.

Two declassifiers ship and both are modelled: the scoped
`endorse[<origin>](v, reason = "...")`, which clears **only** its own
declared origin (item 249, Finding 1) and only where the enclosing
declaration granted the slot; and the checked parser (a `verified fn`
returning `Trusted[T]`). The ambient originless `endorse(v)` is a *parse
error* in the reference (`parser._endorse_expr`), so it is deliberately
not a constructor — every declassification in a parseable program is
scoped and reasoned. `endorse[secret]` is refused before the
declared-slot check and a checked parser is refused on a
`secret`-carrying value, which together are why `secret_persists` holds
with no side conditions: a capability-bound provider key has no
declassifier anywhere in the language.

Four sink rules, kept distinct because they are genuinely different
predicates: `authority` (a `Trusted[T]` parameter, or a
shell/exec/terminal/policy scope under `taint_strict`) refuses any dirty
label; `disclosure` (an emission crossing, a plain extern call, a
provide-method return) refuses `secret` and `confidential`;
`secretReceiver` (a declared `Secret[T]` position) admits `confidential`
but still refuses `secret` — the A8 / CRITICAL 1 disjointness;
`unnameable` refuses everything.

Non-vacuity, per the `CrossTier.annotation_necessary` convention:
`g9_not_vacuous` computes `mintedBy ["web.fetch"] = web` from the real
scope string, admits the path carrying a declared `endorse[web]`, refuses
the same path without it, and refuses it again when the endorse names a
*foreign* origin. `secret_rules_not_vacuous` admits an
`endorse[confidential]` downgrade, refuses `secret` at every sink over
every path, and flips the same declared `endorse[web]` from admitted to
refused purely because the untrusted author minted it itself.

### Non-vacuity, against item 418's bar

Item 418's adversarial review found G4/G5/G6/G8 to be tautologies over a
chosen inductive (the identical statements hold of a typing relation
admitting nothing) and only 3 of 25 theorems to carry non-vacuity
evidence. Step 8 closed that count for the whole layer, in
`scripts/nonvacuity.tsv`; G9 was already ahead of it, carrying four
guards, each a registered theorem:
`g9_not_vacuous` and `secret_rules_not_vacuous` exhibit **inhabited**
flows; `authority_refusal_is_not_universal` exhibits ONE declassifier-free
flow that a disclosure sink admits and an authority sink refuses, so the
conclusion turns on the sink rule rather than refusing everything;
`sink_rules_are_distinct` separates all four `Admits` rules pairwise (the
antidote to "G1 and G6 are literally the same theorem"); and
`secret_refusal_is_load_bearing` shows the algebra **can** clear a
`secret` — the same declassifier forms clear every other origin — so
`secret_persists` holds because of `taint.py`'s two explicit refusals,
not because the datatype lacks a constructor. Delete either refusal from
`kindOK` and the theorem becomes false.

### G9 path coverage — closed by issue #2108, and the residue it leaves

**Path coverage was the obligation, and it is where the real bugs were.**
`Flow` starts from a path that is *given*. That the checker *walks* every
path a program contains is a separate obligation, and it is the one that
actually broke: `taint._walk_component_methods` descended only into
`provide` steps, so a component's **activation body was never
taint-checked at all**, and a `Secret[T]` parameter was stripped inside
its own receiver body (both fixed on
`fix/taint-activation-body-and-secret-receiver`). Until issue #2108
nothing in this file would have caught either — and the reason was
structural rather than a gap in effort: L0 had no component bodies, no
`provide`/activation distinction and no typed parameters, so the
obligation could not be **stated**, let alone proved. It was carried as
the `UNPROVED, unstatable` row rather than as a `sorry` on a statement
that does not typecheck.

**Issue #2108 removed the structural obstacle rather than restating the
row.** L0 grew the syntax to express a body — `RevL.Syntax.Body` holding
`Item`s, `Item.provide` holding `Provide` blocks, `Method` holding typed
`Param`s, and `Body.scopes` projecting the three things the walk must
agree on (label, statement count, seeded parameters) **together**, so the
projections cannot drift apart — and `RevL.Typing` admits it. On top of
that, `RevL.Theorems.G9Coverage` states the coverage obligation:
`RevL.G9Coverage.coversBodyB` says the walk the body induces covers the
body's own scopes, and `RevL.G9Coverage.coversB_iff` unfolds it into the
three projections.

Each conjunct is one of the bugs that actually occurred, which is the test
of whether a coverage theorem is worth having:

* **the label** —
  `RevL.G9Coverage.coversB_refuses_a_scope_the_body_does_not_have`: a
  walk cannot satisfy the row by being a prefix of the body, so a scope
  the walk never enters is visible;
* **the statement count** —
  `RevL.G9Coverage.coversB_refuses_a_shortened_walk` and
  `RevL.G9Coverage.coversB_refuses_a_walk_that_drops_the_activation_scope`:
  entering a scope but not visiting every statement in it flips the
  verdict, which is precisely the activation-body bug;
* **the origin-carrying parameters** —
  `RevL.G9Coverage.coversB_refuses_a_walk_that_strips_a_secret_param` and
  `RevL.G9Coverage.coversB_refuses_a_walk_that_seeds_a_trusted_param`: the
  `Secret[T]`-strip bug, and its converse, so the filter
  (`RevL.Syntax.Scope.origins`, i.e. `RevL.Syntax.Qual.seedsOrigin`) is
  tight in both directions rather than merely non-empty.

`RevL.G9Coverage.g9Coverage_not_vacuous` is the witness the issue asks
for: the body's own walk is **admitted** and four distinct shortenings of
it are **refused**, so the row is mutation-sensitive in every direction it
names, and a `coversBodyB` that returned a constant fails five theorems.
The witness body is a real one —
`RevL.G9Coverage.witnessBody_is_admitted` proves it typechecks — so the L0
growth is not an empty grammar.

**What the theorem does not reach, named rather than smuggled.** The
statement is over the scopes a component's **own body** induces. It is not
a claim about the interprocedural fixed point (`_Signature`,
`_infer_signatures`) that discovers *which* paths exist in the first
place: a path the walk never learns of is outside the theorem's reach, and
so is a component whose body the exporter never reconstructs. The oracle
side is what keeps that honest — `RevLOracle.gcRowB` decides
`RevL.G9Coverage.coversB` against the walk the harness **observes** on the
corpus (the `GB`/`GP`/`GW` rows), so the formal side and the shipped
checker can disagree, and do so loudly. `diff_corpus.py` files a
disagreement under the fatal `missed-G9-coverage`, and — separately, so
that an instrument failure can never be misread as a coverage regression —
a run whose recorder observed **no** scope for a component the parse side
says has one files under the distinct fatal
`missed-G9-coverage-observation`. Both buckets were shown reachable and
disjoint by adversarial probes. Silencing the recorder puts all 15 files in
the observation bucket. Dropping only the activation scope touches 5 files,
and they split: 1 (`examples/app/notes.rvl`, whose three components each
keep a provide scope) files under the genuine coverage `fail`, while the
other 4 file under observation — those are single-component fixtures whose
*only* scope is the activation, so shortening the walk leaves the recorder
with no scope at all for a component the parse side says has one, which is
indistinguishable from its own blindness. That is the conservative reading
by design: both buckets are fatal, so the run fails either way, but the
harness only claims a coverage *disagreement* where it has a walk to
disagree with. The theorem itself is sharper than the harness — `coversB`
compares lengths, so an empty walk against a one-scope body is already
`false` in `RevL.G9Coverage`.

**A naming trap, corrected here.** The roadmap marker and this file used
to disagree about who owns this work: `docs/v2.0-roadmap.md` marked item
418 as `LANDED` with "all nine exit steps", while step 9's text ("after an
operational semantics exists") is this work's **precondition**, not this
work — and this row stayed `UNPROVED`. Issue #2108 resolved that by
correcting the marker and naming the residue, rather than by stretching
the theorem to fit the marker. The theorem was not widened: the
interprocedural fixed point is still outside it, and still named.

Also out of scope, documented rather than smuggled as axioms: the runtime
tag (Slice B, item 243) — nothing here claims a runtime property; the
model proving what follows once a path is exhibited; and that the checker
labels the right positions as sinks, which is extraction, not theorem. The
origin half of that labelling **is** proved:
`taint_surface_within_declared_context` composes with G6 to show every
origin a statement can mint is one its declared context already declares.

### What the differential-oracle `TAINT` row does and does not add

Issue #1811 group 2 bound this development to the differential oracle — as
**route B**, the sanctioned fallback, and the distinction is the point.
`RevL.G9Flow` adds no rule: `Flow`, `Admits`, `Sink` and the label algebra
are reused, and `RevL.G9Flow.g9RowB_iff` pins the oracle's decider to
`Admits`. What changed is that the four rejection documents the rule exists
for (`g9_closure_capture_launders_taint.rvl`,
`g9_service_return_launders_taint.rvl`,
`g9_spawn_config_launders_taint.rvl`,
`gsecret_service_return_discloses.rvl`) now land in `agree-G9` instead of
the deliberately unratcheted `out-of-fragment`, and a checker that stops
reporting its discovered sink turns the row **red** (`missed-G9`, fatal)
rather than green.

It is still **the rule on the corpus, not the coverage of the walk**: the
row's premises are the checker's own refusal, so it cannot distinguish "the
rule holds at the sink the checker reached" from "the checker never reached
the sink". `RevL.G9Flow.reported_walk_reaches_the_reported_label` states
exactly the positive half — the reported label is reachable from the
reported step count, at every count — and no more. The coverage half is a
**different row** (the `GC` row below, issue #2108) resting on a different
theorem (`RevL.G9Coverage.coversBodyB`); this `TAINT` row is unchanged by
that and still substitutes for nothing.

### What the differential-oracle `GC` row does and does not add

The `GC` row is the coverage half, and it is the row issue #2108 asks for:
one row per **component the checker's walk actually visited**, decided by
`RevLOracle.gcRowB` — `RevL.G9Coverage.coversB` — against the walk the
harness **observes**. It is not a second reading of the refusal: the
exporter wraps `revl.taint._walk_component_methods` and records, for each
scope the checker's own `_FlowChecker` is handed, the label it was given,
the number of statements it was handed and the origins it seeded. Those
three columns are the `GW` rows; the parse side derives the body's own
scopes from the source (the `GB`/`GP` rows). The verdict is `ok` exactly
when the two agree on all three, which is what makes the row able to
disagree with the theorem rather than merely echo it.

The exporter **pins private seams** and hard-fails rather than adapting if
any of them moves — a silently-adapted exporter would report agreement it
never measured:

* `revl.taint._walk_component_methods` as the entry point. The checker's
  taint walk is *not* observable at `revl.taint.check_taint` (the lowering
  binds `check_taint` into its own namespace at import), and a recorder on
  `_FlowChecker` alone would also capture `_infer_scope_env`'s internal
  checkers — 548 spurious scopes on today's corpus. Wrapping the walk
  itself is what makes "the walk" the unit of observation;
* `revl.taint._FlowChecker.run`'s `(body, env)` call signature and its
  `enforce` attribute. Only a call with `enforce` true is a walk step, and
  only the **outermost** call per label counts — `run` recurses into nested
  bodies under the *same* `endorse_label`, so the recorder tracks depth and
  records at depth zero;
* the `endorse_label` **spelling**: `"<C> activation"` for the activation
  body and `"<C>.<method>"` for each provide method. The parse side names
  the same scopes `<provide key>.<method>`, so the exporter translates **by
  name** rather than by position; an unexpected label is a hard failure;
* `revl.taint._seed_param_env`'s seeding rule and **declaration order**:
  only the parameter indices the model declares an origin for are seeded,
  and `Scope.origins` reads them in the order they were declared. The
  `GP` rows carry the *effective* seeding qualifiers for this reason — a
  parameter declared plain can still be seeded `untrusted` when the route
  that binds it says so, and the row has to say what the checker did, not
  what the declaration looks like.

**Four observation states, and two fatal buckets.** A file is `active`
(the walk ran and the recorder saw scopes), `inactive` (the walk ran and
the component genuinely has no scopes), `refused` (the checker refused the
file before any walk) or — the case that must never be confused with a
coverage regression — **unobserved**: the parse side says a component has
scopes and the recorder captured none. A genuine disagreement files under
the fatal `missed-G9-coverage`; an unobserved walk files under the distinct
fatal `missed-G9-coverage-observation`, and `checker_alignment` tests the
observation bucket **first**, so a blind spot can never be filed as a
coverage failure. The two were shown reachable and disjoint by adversarial
probes: silencing the recorder puts all 15 files in the observation bucket,
while dropping only the activation scope touches 5 files and splits them —
1 (`examples/app/notes.rvl`) under a genuine coverage `fail`, the other 4
under observation, because those four are single-component fixtures whose
only scope is the activation, so the shortened walk leaves the recorder no
scope at all to compare. On today's corpus: 15 files active over 21
components, 7 with an activation scope and 17 with a provide scope, 0
disagreements.

What this row does **not** claim: that the checker's walk is *complete*
across components. It decides coverage for the components whose bodies the
exporter reconstructs from the parse — a component it never reconstructs
is outside the row, exactly as it is outside
`RevL.G9Coverage.coversBodyB`, and the interprocedural fixed point that
decides *which* paths exist is outside both.

## G-RETAIN — retained data at a persistence sink (`RevL.Theorems.GRetain`)

`src/revl/taint.py` `_refuse_retention`: "data past its retention deadline
may not be written to durable storage". The model is deliberately small,
because the rule is: `PersistenceSink` is the checker's own
`retention.PERSISTENCE_SINK_SCOPES` — the ten scopes a write can be durable
through (`db`, `fs`, `object_store`, …) — mapped from the scope **name** the
refusal prints (`sinkOfScope`), and `holdsB` is the checker's own predicate
`not policy.expired(instant)`, i.e. `now ≤ deadline`, with a declared `hold`
overriding the deadline entirely (the third escape the checker's hint names,
alongside erasing with a receipt and extending `until`).

`Instant` is `Nat` — epoch seconds, which is what the refusal prints and what
the harness can compare exactly. The checker's comparison is **strict**
(`as_of > until`), so the deadline instant itself still holds; `holdsB` is
written with `≤` and `holdsB_iff` proves the correspondence rather than
leaving it to the reader.

The walk premise is `reaches`: the `chain` column the checker reported must
actually end at the `sink` column it reported. `reaches_iff` proves that,
`corpus_walk_reaches_the_corpus_sink` exhibits it on the corpus chain
`load() -> db_put`, and `corpus_walk_does_not_reach_another_sink` shows the
premise is not vacuous by failing it for a sink the chain never reaches.

Non-vacuity is carried by `retain_not_vacuous`, which decides the corpus
row's own columns at four instants — refused past the deadline, admitted AT
it, admitted a year before it, admitted under a `hold` — so the row is
mutation-sensitive in every direction and a `rowB` that dropped its `now`
argument would not elaborate. `corpus_row_decided` records the three of those
that share one `until`, which is what makes the flip attributable to the
clock and not to a different policy.

### What the differential-oracle `RETAIN` row does and does not add

Issue #1811 group 3 bound this development to the differential oracle the
same way group 2 bound G9: the `RETAIN` row decides the rule at the scope,
the sink and the walk the checker **reported**, and at the instant the
checker **compared**. `RevL.GRetain` adds no rule the checker does not
already state — `sinkOfScope` is its scope table, `holdsB` is its
`expired` negated, `reaches` is its own `The retaining path is …` line —
and `retainRowB_iff` pins the oracle's decider to it, so a `fail` is the
rule VIOLATED at a sink the checker itself reported, with the refusal
explained rather than contradicted.

It is still **the rule on the corpus, not the coverage of the walk**, and
the `now` fact is where the two would be confused. Pinning
`REVL_RETENTION_AS_OF` makes the row *reproducible*; it does not make the
checker's walk complete, and it says nothing about whether the checker
visits every path that could carry a retained value to a durable sink. The
open obligation G9 carries is inherited unchanged: the row cannot
distinguish "the rule holds at the sink the checker reached" from "the
checker never reached the sink". That coverage is **still open for this
row**: the `GC` row added by issue #2108 decides the *taint* walk's
coverage, not the retention walk's, so nothing here substitutes for it.

One further limit, stated rather than hidden: the exporter reads the row's
columns out of the refusal's **prose**, because the `G-RETAIN` refusal
passes no `navigate` (unlike the three `G9` / `taint-flow` documents, whose
sink and origins arrive as structured fields). The sink, the scope, the
policy name, `until`, `now` and the chain are all read from the message and
the hint. The hint is advice and is not scraped for anything the row
decides; the `now` column is cross-checked against the harness's own pin by
`retain_coverage`, so a message the exporter mis-parses fails the gate
rather than producing a plausible-looking row.


## G4 inverse — the bracket's `undo` is the inverse its acquisition owns (`RevL.Theorems.G4Inverse`, issue #2097)

Two checker rules carry the `G4` code and the `inverse` category without
being the marker rule `G` states:

* `lower._check_site_release` (issue #1859) — a bracket's `undo` must be
  the release its acquisition owns: a host family's release applied to the
  handle the bracket bound (`_HOST_ACQUIRE_VERBS`: `Map.new` → `drop`,
  `Pool.open` → `close`, `Stream.source` → `close`), or, for an
  `extern acquire fn`, the inverse the extern **declares** applied to that
  handle.
* `lower._method_effect_inverse` (issue #1945) — a host write's `undo` must
  be its table entry on the same receiver with the same key expression
  (`_HOST_WRITE_INVERSE`: `Map.insert` → `remove`,
  `Map.insert_if_absent` → `remove`, `Map.remove` → `insert`).

Until this row landed, the formal model had no fact for either, so every
such refusal landed in the `out-of-fragment-inverse` bucket: a
deliberately-named absence of fact, ratcheted shrink-only, and **not** a
claim that the model covered the rule. Four documents held the bucket open
(`examples/rejections/g4_extern_undo_not_declared.rvl`,
`examples/rejections/g4_undo_not_release.rvl`,
`examples/rejections/g4_method_write_not_inverse.rvl`,
`tests/fixtures/canary_candidate_inverse.rvl`).

### The model

`InvKind` is the arm: `host`, `extern`, `write`. `requiredInverse` is the
rule's one lookup — `hostRelease` for the host arm, `writeInverse` for the
write arm, and **`some verb` for the extern arm**, because that arm's
requirement comes from `env.extern_inverse`, the extern's own declaration,
not from a table the model could hold. `rowB` is then the whole rule: the
acquisition's own inverse is the verb the checker demanded, **and** the
site's `undo` is the spelling the checker demanded. Spelling equality is
what carries the receiver and the key, which is why `rowB` needs no
separate handle or key argument — and
`corpus_write_row_flips_when_the_receiver_moves` shows that a row
comparing only verbs and keys would return `true` where this one returns
`false`.

`hostRelease` and `writeInverse` are **restated** from
`src/revl/typecheck.py`'s `_HOST_ACQUIRE_VERBS` and `src/revl/lower.py`'s
`_HOST_WRITE_INVERSE` rather than imported: the formal layer is a separate
library and cannot import Python, so the tables are transcribed, and
`host_release_table` / `write_inverse_table` pin all three entries of each.
A checker that gained a family or a write would produce an acquisition the
table does not hold — `unmodelled_acquisition_has_no_inverse` — and that
emits **no row**, so the refusal lands in the fatal `missed-G4` rather than
in an agreement. The direction that keeps this row from passing by looking
away is the `none` arm of `requiredInverse`, and
`requiredInverse_none_off_the_tables` states exactly where `none` can
occur.

### What the differential-oracle `INV` row does and does not add

The oracle's `INV` row decides `rowB` at the **arm, acquisition, demanded
verb and demanded spelling the checker's own refusal names**, all four read
out of the refusal's prose by the exporter (`formal/harness/diff_corpus.py`).
`g4InvRowB_iff` pins the oracle's decider to `rowB`, so a `fail` is the
rule VIOLATED at a site the checker itself reported, with the refusal
explained rather than contradicted. The harness recomputes the verdict
independently (`_g4inverse_holds`, harness-spelled) and compares, so
changing the model alone moves the model and the reference's `fail` becomes
the harness's `missed-G4` — which is fatal.

`g4inverse_coverage` executes the non-vacuity claim rather than asserting
it, and its findings are gate failures. For every exported row it requires
that the refusal be one of the three sentences the row carries, that the
acquisition be one the row's tables hold, that the demanded verb be the one
that table holds — and that the verdict **flip** when the `site` column is
replaced by the spelling the checker's own advice demanded. Refused at the
site the checker reported, admitted at the demanded spelling: a row that
returned a constant, or that ignored the receiver or the key, fails the
ratchet.

### What this row does NOT claim

**It is the rule ON THE CORPUS — the site the checker DISCOVERED — and
NOT coverage of the checker's walk.** The four documents are all
refusals, so the row's non-vacuity is a mutation-sensitivity ratchet, not
an admitted/refused pair: it cannot distinguish "the rule holds at the
bracket the checker reached" from "the checker never reached the bracket".
Route A — growing L0 so the checker's coverage is itself proved — is
**not** item 418 step 9 (that step's "after an operational semantics
exists" is route A's *precondition*). It landed for the **taint** walk in
issue #2108, and it is deliberately **unclaimed** for this row's bracket
walk here, in the module docstring, in the row's own printed coverage
line, and in this section. Nothing here substitutes for it.

Two further limits, stated rather than hidden. The exporter recognises only
the three canonical refusal sentences; a `G4`/`inverse` refusal in another
shape emits no row and is recorded as a `g4inverse coverage` finding, so a
checker that grew a fourth shape reds the gate rather than producing a
plausible-looking row. And the demanded spelling is read out of the
checker's `` write `undo …` `` advice, which for `Map.remove` is
`store.insert(k, <the value it held>)` — not a call any real site can
literally carry, so that entry's flip is a **substitution** of the
`site` column rather than a spelling any document could exhibit. The row
states the rule for that entry (`write_inverse_table`) and the substitution
still shows the verdict reads the `site` column, but no corpus document
exercises it, and this section does not claim one does.


## The effect-classification lattice — G4/G5/G8 re-proved (item 418, step 4)

`RevL.Lemmas.ClassLemmas` (L1, core only) models what `src/revl` actually
computes, and `RevL.Theorems.G{4,5,8}_Classified*` restate G4, G5 and G8
over it. The old three theorems are kept, marked **proved (shape-level)**
in the table above with the specific weakness spelled out in each Notes
cell. They are not deleted and they are not wrong: they are statements
about the syntax, and the review's finding was that the syntax was doing
all the work.

### The model

`Cls` is the four-point lattice `pure | acquire | witnessed | emission`
(`parser.ExternDecl.classification`, `parser.py:513`), ordered by how much
of the host a declaration can disturb. Two predicates cut it, and both are
read straight off the reference:

- `Cls.crosses` = `witnessed` or `emission` — the seed set of
  `emission_analysis._emitting_capabilities` ("a witnessed extern crosses
  the same boundary as an emission", item 243);
- `Cls.inverseAdmissible` = `pure` or `acquire` — the admissible set of
  `lower._check_witnessed_inverse` ("the declared inverse is a host-LOCAL
  restore, so only `pure`/`acquire` callees are admissible").
  `crosses_eq_not_admissible` proves the two are one predicate.

`reachCls` / `reachCaps` are `_emitting_capabilities`' least fixed point
over the fn call graph: an extern stops the fold (it *is* the boundary),
a `fn` joins what its callees reach. `declOK` is the per-declaration rule
set (`lower.py:2573`, `:2608`, `:2614`, `:2735`, `:2744`) and `inverseOK`
is `_check_witnessed_inverse` read off the fold.

The fold is proved **sound** (`reaches_le`), **exact** (`reach_exact` —
its verdict is attained at a concrete declaration, so nothing is
invented), **monotone in fuel** (`reach_mono_fuel`) and **compositional
along a path** (`reach_le_trans`). Those four are what let a single
verdict taken at a declared inverse constrain every call beneath it.

### What changed for each guarantee

- **G4.** The old proof ends `| raw _ => cases ht`. Here the `raw` shape
  is an ordinary term (`rawWrite : ExternDecl`, an `acquire` with no
  `undo`), and `raw_mutation_is_representable` pins that. The refusal is a
  computation (`declOK`), and `g4_not_vacuous` shows three shapes admitted
  beside two refused, so it is not universal.
- **G5.** The old `registrations` is the constant-zero function, so an
  inverse calling `db.insert` scores clean. The new one counts calls whose
  *reached* classification crosses, and
  `registrations_depends_on_its_argument` refutes the review's probe
  outright. `sneaky_undo_is_refused` exhibits the `sneakyUndo` shape (a
  witnessed extern whose `undo` calls an `emission`), refuses it, refuses
  its one-`fn`-deeper twin, and admits the host-local inverse beside them.
  `sneaky_inverse_run_emits` then RUNS the refused inverse under step 2's
  semantics and shows the one-way boundary record land in the WAL, while
  `clean_inverse_run_is_silent` shows the admitted one take a `pure` step.
- **G8.** `boundaryOf` decides the surface per constructor, which is the
  definitional escape. `stmtSurface` is `stmtHeads` composed with the
  reach fold, uniformly over all four statement forms, so there is no
  per-constructor case to escape through. Both directions are kept, and
  soundness now carries **no typing hypothesis**: a `raw` leak is
  enumerated like anything else (`raw_leak_is_on_the_surface`) rather than
  discharged by `Typed` lacking a constructor.
  `effect_carrying_emission_is_on_the_surface` shows the two models
  disagreeing in both directions — an `effect` carrying an emission is on
  the new surface and not the old one; an `emit` marker on a host-local
  call is on the old surface and not the new one.

### What this does NOT close

- **The fuel bound is real and is named, not hidden.**
  `reachCls p fuel n` unrolls the closure `fuel` times, so it
  under-approximates when `fuel` is smaller than the longest `fn` chain.
  `fold_must_run_to_stability` exhibits exactly that: the `fn`-wrapped
  emission inverse is admitted at fuel 0 and refused at fuel 1. That
  `_emitting_capabilities` iterates to stability is a property of the
  reference implementation, not a theorem here.
- **The call graph is given, not derived.** `FnDecl.calls` stands for
  `_calls_in` over a lowered body. That `_calls_in` finds every call is an
  empirical obligation on the lowering; it is documented, not smuggled in
  as an axiom.
- **First-class dispatch is out of scope.** `_emitting_capabilities` adds
  the unnameable `*` when an emitting callable escapes as a value.
  `RevL.Theorems.CapCeilings` models `*`; nothing in this farm claims to.
- **`compensate` is not modelled as a separate slot's walk.**
  `_check_witnessed_inverse` walks `undo` and `compensate` alike; the
  model carries the `compensate` field and `declOK`'s witnessed rule
  refuses it, but `inverseOK` reads only the `undo` callee.
- No differential-oracle row references these definitions, so item 418's
  C4 applies here as it does to G9: the lattice model is not covered by
  that gate.

## Differential oracle (the proved model against the shipped checker)

`harness/diff_corpus.py` + `harness/Oracle.lean`: parse every corpus
`.rvl` with revl's real parser, export one TSV row per *fact*, then run
`harness/Oracle.lean` over the same TSV and diff its verdicts against a
Python reference. A mismatch fails `make formal`.

**What this checks (roadmap item 418, C4 / step 6 — was FALSE before,
now wired).** Until step 6 the oracle was not connected to anything
proved: `Oracle.lean` imported `RevL.Manifest` and used no definition
from it (deleting the import compiled clean), every verdict came from
private unproved Lean, and `diff_corpus.reference_from_tsv` was a third
re-implementation rather than a call into `src/revl/cap_order.py`. Step 1
of item 418 demonstrated the consequence by accident: `ProvidesDisjoint`
and `LinkOK` were restated over `(key, realm)` slots — a change to what
the model *decides* — and the oracle's output was bit-identical, 343 of
343 agreeing before and after. The G2/`LinkOK` correction later in the
item did it a second time, by construction.

Both sides are now real:

- the **Lean** side `decide`s the proved model. `V … disjoint=` is
  `decide (RevL.Manifest.ProvidesDisjoint comps)`; `V … closed=` is
  `decide (RevL.Manifest.RequiresClosed comps)`; `V … link=` is
  `linkOKB`, with `RevLOracle.linkOKB_iff` proving
  `linkOKB l = true ↔ RevL.Manifest.LinkOK l`; `W … atten=` is
  `attenuatesB`, with `attenuatesB_iff` proving
  `attenuatesB H R = true ↔ RevL.CapCeilings.Attenuates H R` — resource
  half through the proved `RevL.Lemmas.Covers` over `stripCeilings`
  (`coversB_iff`), ceiling half through the proved `budgetOf`
  development, whose unbounded `∀ k` is discharged with
  `RevL.Lemmas.budgetOf_attained`. The components are
  `RevL.Manifest.LComponent` values, so `slots`/`needs` are the model's.
- the **Python** side calls the shipped checker. Capabilities are parsed
  and ordered by `src/revl/cap_order.py` (`parse_cap`, `covers_set`,
  `split_ceilings`) — the one place the `(T, P)` algebra is implemented.
  The harness carries no capability grammar and no `covers` of its own.

The gate therefore bites, and this is the acceptance test for it: change
`RevL.Manifest.needs` to ignore the realm and `lake build` still succeeds
(27/27 targets, every theorem still proved) and the axioms gate is still
clean, but `harness/diff_corpus.py` exits 1 with four mismatches
(`examples/placement/caprealm_app.rvl`, `examples/tenants.rvl`,
`tests/fixtures/canary_tenants.rvl`, `tests/fixtures/erase_realms.rvl`).
The same edit against the pre-step-6 harness produced a bit-identical
verdict file and exit 0.

Facts exported: component manifests (M — with the component's `isolate`
realm map and a `member`/`template` role), require-binding resolutions
(R), provide-key resolutions (C — off the `provides` clause), the
installed provide blocks (PB — off the `provide k { … }` bodies, one row
per block; issue 1167) and the routed keys (PR — off the `isolate k in
realms(...)` binds; issue #1172), per-statement classifications (T), call
facts with marker context (U), service-method emission bounds (B) and a
scoped bound's declared entries (Q), the **reachability** facts that let
the model see past the marker — the capabilities a component's `requires`
bindings grant it (K), its activation emit-step surface (A), the emission
capabilities a provide method's body crosses (F), the activation-body
spawn edges (S), and the spawn handles (H) through which
`w.task.run(...)` resolves to the child's provision — the canonical
capability decompositions (Z/Y, straight out of `cap_order.parse_cap`),
parse refusals (X) and componentless files (N), and the ordered
activation-body steps as the A2 rule sees them (AQ, issue 1166).

Two families of facts are of a different kind and are listed apart for
that reason, because neither is extracted from any `.rvl` text:

- **teardown scenarios (E/J)**, for G7. A teardown disposition is a
  property of a RUN, not of a manifest, so the fact is the shape of one
  activation's LIFO stack (E — one row per entry, in registration order,
  carrying its model `EntryKind` and the reference's registration seam)
  and the verdict it unwound under (J).
- **crash-recovery scenarios (L)**, for A8 and R4. A recovery verdict is
  a property of a durable LOG, so the facts are the records of one WAL —
  the constructors of `RevL.Lemmas.Rec`, one row each, in append order —
  plus the re-issue oracle 243 rule 6 makes fallible (`L … fails <seq>`,
  a property of the world rather than a record) and a `L … run` row
  declaring the scenario.

Two extraction facts on the `M` row are what make the corrected G2/G3
rules observable:

- the **realm map**, from the component's `isolate <key> in realm(<r>)`
  clauses. `lower._realm` reads the same table, and the linker's
  `provider_of` is keyed on `(key, realm)`. Without it the exporter could
  not feed the model's slot. (`isolate <key> in realms(...)` — the
  multi-realm ROUTE, item 162 — is a different construct and is not
  folded in; no corpus file uses one.)
- the **template flag**. A spawn target is a runtime instance, not a
  static composition member: `lower._link` excludes it from the G2/G3
  table and from `loadOrder` because each instance gets a fresh local
  realm. Without it two per-tenant worker templates read as one G2
  provision conflict, which is not what revl decides.

Verdicts:

- **V rows (per file, G2/G3)**: provision disjointness over `(key,
  realm)` slots, requirement closure, and linkability. `link=ok` means an
  admission order was found AND `linkOKB` certified it, over the LOCAL
  composition — requirements no in-file component provides are elided,
  because `lower._link` resolves those against the whole composition and
  adds no edge. Elided, not supplied by a fabricated provider: the
  provision surface is untouched, so a component that requires a key it
  provides *itself* keeps that requirement and is refused, which is the
  linker's G3 self-provision refusal, and a cycle is refused because no
  admission order exists.
- **G rows (per component, G4-shaped)**: marker presence must equal the
  interface's declaration — a plain call to a declared emission method,
  or an `emit`'d call to a non-emission method, is refused. Receivers
  include spawn handles, not just `requires` bindings.
- **P rows (per provide method, G4)**: a service declaration is an upper
  bound on its providers. The method's reached capabilities must sit
  inside its declared bound — `plain` admits none, bare `emission` admits
  any, `emission[...]` admits exactly the declared entries.
- **W rows (per activation spawn edge, G4/item 66/294)**:
  `RevL.CapCeilings.Attenuates` — the child's transitively closed reach
  covered by the spawner's held capabilities on ceiling-stripped
  capabilities, plus the ceiling budget check.
- **X rows (per refused file)**: a verdict of RECORD, not a computed one.
  revl's parser refused the file, so there is no manifest to model; the
  row carries the refusal code so the file is counted and diffed rather
  than dropped. Its agreement is tautological (both sides read the same
  parser) and is reported separately from the computed verdicts for
  exactly that reason. If a parser change ever makes one of these files
  parse, it flows into the full model and the buckets move loudly.
- **D rows (per teardown scenario, G7)**: the three columns of
  `RevL.Semantics` — `replayed` (ordered: this is the LIFO claim),
  `discharged` and `stranded`. Unlike every row above, the reference side
  does not recompute a judgment: it RUNS
  `backends/python/runtime.py` over the scenario's stack and reports what
  actually happened, reading each entry's fate off the reference's own
  state (`_Transactional.discharged`, `runtime.estop_residue()`) and the
  replay order off the inverses as they run. So the diff is the model's
  *predicted* disposition against the reference's *observed* one.
- **O rows (per crash-recovery scenario, A8 + R4)**: the model's
  `RevL.Lemmas.outcome` (which of the three verdicts `recover`'s if-chain
  converges to), `replayed` (the seqs a whole recovery run APPLIES) and
  `reported` (the residue surface of a roll-back). Reference side: write
  the records as a real WAL, call `revl.recovery.recover` over it, and
  read the verdict, the applied set and `residue.outstanding` off what it
  did. `replayed` is compared as a SET — the model walks the log in
  append order and `_roll_back` walks each record family newest-first, and
  neither order is a claim the other makes; the ordered LIFO claim is
  G7's and the D row checks it ordered. `reported` is compared only under
  `outcome = rolledBack`, the model's own scope.
- **C rows (per lowered statement, G6, issue 276)**: `Oracle.confinedB` over
  `RevL.Typing.stmtHeads` of the statement reconstructed from its exported
  heads (`exprOfHeads`, non-lossy by `heads_exprOfHeads`), against the
  component's declared context (its require locals from M plus its
  require-held binding roots from K). `confinedB_iff` proves the printed Bool
  is exactly `∀ k ∈ stmtHeads s, k ∈ C`, the surface `RevL.G6.confinement`
  quantifies over. The reference computes the same head-roots membership
  independently from the TSV. Every exported statement is from an admitted
  component, so a leak never appears as an admitted violation (the checker
  refuses those at parse, hence the G6 fixtures carry no I rows); the row is
  kept honest instead by `confinement_coverage`, which fails the gate unless
  the corpus carries both a confined statement over a non-empty reach and a
  caught violation, and by `g6_row_not_vacuous`, which proves the verdict
  flips when a leaking head is accepted. Host builtins and let-bound locals
  count as reach, so a component that uses them is a faithful `fail`, not a
  claim it is unsafe.
- **A9 rows (per declaring or installing component, issues 1167 / #1172)**:
  `RevLOracle.a9RowB`, which IS `RevL.A9.a9B` over the component's
  `LComponent` (from M) beside its installed block keys (from PB) and its
  routed keys (from PR); `a9RowB_iff` proves the printed Bool is exactly
  `RevL.A9.A9OK`, both directions. The reference recomputes both
  memberships independently: every PB key in the M row's clause, and every
  clause key in PB or PR. One row per component that declares or installs
  anything — a component with neither would agree vacuously and gets none,
  and a component that declares and installs nothing (the converse
  fixture's `S`) is under the row although it has no PB row at all. Both
  fixtures are a `fail` on both sides and file under `agree-A9`;
  `a9_coverage` fails the gate unless the corpus carries an admitted
  provider, both refused shapes and the routed shape admitted, and
  `RevL.A9.a9_row_not_vacuous` proves the verdict moves with the block and
  with the route alone (same manifest, different body fact, different
  verdict). Blinding the printed verdict on a fixture yields one `a9`
  mismatch and a FATAL `missed-A9`; blinding `a9RowB` itself breaks the
  `a9RowB_iff` proof. A decider blind to direction 2 is exactly what PR
  #1184 found on CI: the converse fixture in `missed-A9`.
- **A2 rows (per component, A2, issue 1166)**: `Oracle.a2OKB` —
  `RevL.A2.a2B`, the checker's own fold — over the component's `AQ` body
  steps in body order, with `a2OKB_iff` proving the printed Bool is exactly
  `RevL.A2.A2OK` (no `provide` followed by an `acquire`), the hypothesis
  of `RevL.A2.withdrawals_precede_releases`. The reference folds
  `lower._dispatch_action`'s rule over the same rows independently. A
  refusal is a `fail` on both sides, so the row is kept honest by
  `a2_coverage` (an admitted body with both shapes AND the refused fixture
  must be in the corpus) and by `RevL.A2.a2_not_vacuous` /
  `RevL.A2.fixture_refused`, which prove the verdict flips between the two
  shapes. The alignment arm is `agree-A2` / `missed-A2`, the latter fatal;
  blinding the printed verdict was seen to produce one mismatch and
  `missed-A2 1 FATAL` on the fixture before the row was trusted, while
  blinding `a2OKB` itself never reached the row — `a2OKB_iff` stopped
  elaborating, the same layer that catches D-row model drift.
- **AP rows (per marked crossing, the G4 approval floor, issue #1455)**:
  `Oracle.approvalRowB` — `RevL.G4Approval.crossingB` — over the tokens
  the crossing reaches (`AX`), its `with` edge (`AE`, absent for the value
  form) and the file's approval-required tokens (`AR`), with
  `approvalRowB_iff` proving the printed Bool is exactly
  `RevL.G4Approval.CrossingOK`. The reference recomputes it from the same
  rows with the shipped `lower._approval_covers`. The exporter resolves
  the tokens the way `lower._approval_crossed_caps` does, and a SCOPED
  host emission carries its scope (`extern emission[pay] fn charge` is
  `pay`), both here and in the F row's bound column, where it used to read
  the extern's name (or `*` through a `fn`). A crossing through a
  provision receiver is resolved off the same alias table the marker
  rule's `G` row reads (`_route_values`): a spawn handle, a `let` alias, a
  field or element read off one, a receiver written in place (an `if`, a
  `match`, a record or list literal), a provide method's service-typed
  parameter, and an arrow's service-typed parameter at an application
  that binds a provision into it, and a provision held through a block
  `match` arm. Before this row the approval fixtures (34 then, 36 with
  issue #1699's block-arm pair) sat in a ratcheted
  `out-of-fragment-approval` bucket; they file under `agree-G4` now and
  the bucket is gone. Blinding the printed
  verdict (`ok` for every crossing) was seen to produce 34 `approval`
  mismatches and `missed-G4 34 FATAL`; making the model's
  missing edge cover everything stops `approval_not_vacuous` from
  elaborating. `approval_coverage` fails the gate unless the corpus
  carries a covered crossing, one refused under another edge, one refused
  with no edge and an unrequired one admitted.
- **BU rows (per binding scope, G6 binding uniqueness, issue #1812)**:
  `Oracle.bindingRowB` — `RevL.G6Binding.bindingB` — over one scope's
  `BE` steps: the names in view when it starts (`seed`), then its binds
  and its block boundaries (`enter`/`leave`), in source order. The
  activation body is one scope, seeded with the `requires` locals
  (`Env.bind_local`); each provide method is another, seeded with the
  activation bindings made before its `provide` block and its own
  parameters (`_check_rebind`, which does not consult `requires`). Blocks
  are a method's `if` arms and `while`/`for` bodies
  (`_lower_scoped_block`), a `for` binder living in its loop's block, and
  in the activation body an effect's `setup` block and a stream
  iteration's body. `bindingRowB_iff` proves the printed Bool is
  `RevL.G6Binding.BindingOK`, and the reference recomputes the same frame
  walk from the rows. A G6 `binding` refusal files under `agree-G6` when a
  scope fails and under the fatal `missed-G6` when none does; a failing
  scope in a file the checker accepts is `formal-strict`. Blinding the
  printed verdict (`ok` for every scope) was seen to produce 1 `binding`
  mismatch and `missed-G6 1 FATAL`; checking only the innermost frame
  stops `okB_iff` and `fixtures_decided` from elaborating.
  `binding_coverage` fails the gate unless the corpus carries a refused
  scope and an admitted one that binds a name and opens a block. Not
  modelled: pattern binders in a `match` arm, the body of an `every`
  timer, and the other G6 refusals (purity outside an effect form,
  reassigning an immutable binding).
- **G1 rows (per component, G1 declared access, issue #1807)**:
  `Oracle.accessRowB` — `RevL.G1Access.accessB` — over the component's
  declared requirements (its `M` row) and its ACCESS roots (`GA`). An
  access root is the root of a call head the component makes, or of a
  name it reads in value position (`config` excepted), at every nesting
  depth, including a block `match` arm, that is not a binding in the component (a `let`, `var`,
  parameter, loop variable, arrow or `match` binder), a module `fn` or
  `extern`, a name a `use` imports, a host family (`lower._HOST_CALLABLES`)
  or a type or variant constructor; an `intercept` target that is not a
  provision joins them (one on a provision is a different refusal, issue
  #1809). The requirements are not dropped by the exporter, so the model
  is what checks each root against them. `accessRowB_iff` proves the
  printed Bool is `RevL.G1Access.AccessOK`, and the reference recomputes
  the subset test. A G1 refusal files under `agree-G1` when the row fails
  and under the fatal `missed-G1` when it does not; a failing row in an
  accepted file is `formal-strict`. The 21 G1 refusals, 16 of them the
  block-nesting fixtures, left the generic `out-of-fragment` bucket; the
  three `match_block_arms` G1 fixtures that main added since (issue #1699)
  agree through the value-position names.
  Blinding the printed verdict was seen to produce 21 `access`
  mismatches and `missed-G1 21 FATAL`; making the decider admit every
  root stops `accessB_iff` and `g1_access_not_vacuous` from elaborating.
  `access_coverage` fails the gate unless the corpus carries a refused
  component, an admitted one with a declared access root, and an admitted
  one whose dropped roots include a local, a module callable and a host
  family.
- **A1 / A1S rows (per site and per provide method, A1 async colour,
  issue #1808)**: `Oracle.asyncRowB` — `RevL.A1Async.siteB` — over one
  site's kind and heads (`AS`), the file's async names (`AN`: async
  externs and async service operations spelled `<Service>.<op>`) and its
  `fn` call graph (the `FN` rows), with a fuel of one step per `fn` plus
  one. A site is a provide method (sync or async as its service declares
  it), an activation `effect` or `emit` step (awaited or not), or an
  `undo` / `compensate` slot; its heads are the calls it makes, a service
  operation resolved through a requirement, a spawn handle or an alias.
  `asyncRowB_iff` proves the printed Bool is `RevL.A1Async.SiteOK`, and
  the reference recomputes the reach as a true fixed point, so an
  under-fuelled oracle would show as a mismatch. `A1S` (`sigRowB_iff`)
  compares a provide method's written colour with its service's. An A1
  refusal files under `agree-A1` when a row fails and under the fatal
  `missed-A1` when none does; the uncoded signature refusal is matched by
  its message. The arrow-type refusal is not this rule and stays in
  `out-of-fragment`. Blinding both printed verdicts was seen to produce 9
  mismatches (8 `async_site`, 1 `async_sig`) and `missed-A1 9 FATAL`;
  cutting the reach's call step stops `reachB_iff` from elaborating.
  `async_coverage` fails the gate unless every rule (awaited, unawaited,
  sync method, teardown) is both admitted and refused somewhere in the
  corpus.
- **PL / IC / MS rows (per component, three declaration rules, issue
  #1809)**: `RevL.Prelude.preludeB` over the activation body's statements
  in order as preludes (`isolate`, `intercept`, `handoff`, a `realms(...)`
  route, a model route) and actions (`PS`); `interceptB` over the
  `intercept` targets (`IT`) against the `M` row's provides and requires;
  and `methodB` over the service operations the component names (`MC`: a
  crossing through a requirement, a spawn handle or an alias, and every
  provide-block implementation) against the file's `B` table. Each has a
  proved bridge (`preludeRowB_iff`, `interceptRowB_iff`,
  `methodRowB_iff`), and the reference recomputes all three. The two
  prelude and intercept refusals are uncoded and matched by message,
  filing under `agree-prelude` / `agree-intercept` or the fatal
  `missed-prelude` / `missed-intercept`; the A6 call-site refusal files
  under `agree-A6` or the fatal `missed-A6` (A6's arity and signature
  halves are not this row). Blinding the three printed verdicts was seen
  to produce 3 mismatches and one fatal `missed-*` each.
  `prelude_coverage` fails the gate unless each rule is admitted and
  refused somewhere in the corpus.
- **MPV / MAV rows (G-MODEL-PLACE, issue #1811)**: `Oracle.placeRowB`
  — `RevL.ModelPlace.placeB` — over a component's placed route arms
  (`MP`: one row per candidate role, and for a council arm one per member
  that receives the origin) and the confidentiality origins (`MO`, read
  off `model_route.CONFIDENTIALITY_ORIGINS`); and `Oracle.modelReachB` —
  the spawn rule's proved `attenuatesB` — over the component's held set
  (the same `A`/`F`/`K` set the `W` row reads) and each consulted role's
  reach (`ME` edges, `MRC` caps, `*` for a role that declares none). An
  edge is a role a `route model` block names (every candidate, every
  council member) or a role a `model.<role>` crossing is placed on, for a
  component that consults a model at all (`lower._consults_a_model`). The
  reference recomputes both, the reach with the same attenuation halves as
  the `W` row. A G-MODEL-PLACE refusal in category `model-placement` (the
  off-device message) or `capability-attenuation` files under
  `agree-G-MODEL-PLACE` or the fatal `missed-G-MODEL-PLACE`. Blinding both
  printed verdicts was seen to produce 9 mismatches (2 `model_place`, 7
  `model_reach`) and `missed-G-MODEL-PLACE 9 FATAL`; a `placeB` that
  ignores the residence stops `placeB_iff` from elaborating.
  `model_coverage` fails the gate unless the corpus carries an admitted
  and a refused confidential placement and an admitted and a refused model
  reach.
- **CTV row (G-COUNCIL-SPLIT, issue #1811)**: `Oracle.councilRowB`
  — `RevL.ModelCouncil.splitB` — over the councils a file DECLARES, each
  carrying the tie outcome the checker reads for it (`CV`: the declared
  `on_tie`, or `split` where the `aggregate` clause omits it, resolved by
  the exporter exactly as the checker's rule 10 resolves it). This is a
  finite check over declarations with no reach: the row reads
  `prog.model_councils` and nothing else, so it needs no component body. It
  is exported from the RAW declarations and not from the validated table,
  because that table is empty for a refused file and exporting it would
  emit no row for exactly the file the row exists to catch. The reference
  recomputes the verdict from the exported column against a vocabulary
  spelled out in the harness rather than read off
  `model_council.ADMITTING_TIE_OUTCOMES`, so widening the checker's list
  moves the checker alone. A G-COUNCIL-SPLIT refusal in category
  `model-council` carrying the tie message files under
  `agree-G-COUNCIL-SPLIT` or the fatal `missed-G-COUNCIL-SPLIT`. Blinding
  the printed verdict was seen to produce `missed-G-COUNCIL-SPLIT 1 FATAL`
  (the `gcouncilsplit_on_tie_allow` fixture); a `splitB` that ignores the
  tie outcome stops `splitB_iff` from elaborating. `council_coverage`
  fails the gate unless the corpus carries both an admitted and a refused
  tie policy. Not under this row, and refused by other rules of the same
  code: the unknown tie outcome, the aggregation vocabulary and its
  totality on the declared member set, the member functions and their
  uniqueness, `quorum` bases other than `declared`, and "exactly one
  aggregate rule"; a council declaring no aggregation emits no `CV` row at
  all, so its refusal lands in `out-of-fragment`.

- **TAINT row (G9 / G-SECRET-FLOW, issue #1811 group 2)**: `Oracle.g9RowB`
  — `RevL.G9Flow.g9RowB` — over the sink the checker itself REPORTS and the
  label that arrived there. **This row is the rule on the corpus, NOT the
  coverage of the checker's walk**, and that distinction is the whole of its
  scope: the row's premises ARE the checker's own refusal, so it cannot tell
  "the rule holds here" from "the checker looked here and found nothing".
  Route A of the issue — growing the L0 bodies so the checker's *coverage* is
  itself proved — landed in issue #2108 (`RevL.G9Coverage.coversBodyB`), so
  `RevL.G9`'s coverage row is no longer UNPROVED; but **this** row still does
  not carry it, and the `GC` row is what does.

  What the exporter reads out of the refusal, and from where:

  * the **sink** — `navigate.refused.sink` for the three `G9` /
    `taint-flow` documents (`run`, at argument 1); the `G-SECRET-FLOW`
    refusal (`taint-secret-flow`) passes no `navigate` at all, so its sink
    (`write_file`, at argument 2) is read out of the refusal **message**;
  * the **origins** — `navigate.refused.origins` (`fs`) for the `G9`
    documents; for `G-SECRET-FLOW` the rule's own `confidential` origin,
    from a table spelled in the harness and **not** scraped from the hint,
    whose `endorse[confidential]` occurrences are prose advice. The two
    confidentiality origins are disjoint (`SECRET_ORIGIN` /
    `CONFIDENTIAL_ORIGIN`), so the label is not a guess;
  * the **sink kind**, hence the sink class — the `kind` the refusal names
    (`a shell command` / `an extern host call (a disclosure sink)`), read
    through `RevL.G9Flow.sinkOfKind`;
  * the **naming chain** — the `The tainting path is …` / `The disclosing
    path is …` line of the `hint`, split into steps; the row's `hops`
    column is its length minus one, so the reported length is visible in
    the row's own output;
  * the **escape** — `navigate.alternatives`' `endorse[<origin>]` ref, or
    the `endorse[confidential]` the `G-SECRET-FLOW` hint names.

  A kind or an origin name the harness table does not carry yields no row at
  all, so the refusal lands in the **fatal** `missed-G9` rather than being
  read as an agreement the row cannot make: widening the checker turns the
  row red, not green. A `G9` refusal in category `taint-flow`, or a
  `G-SECRET-FLOW` refusal in category `taint-secret-flow`, files under
  `agree-G9` or `missed-G9`; the `G9` refusal in category
  `taint-declassify` is a DIFFERENT rule and falls through to
  `out-of-fragment`, as does the `G-SECRET` (`taint-secret`) refusal.
  `g9_coverage` fails the gate unless the corpus carries both an
  authority-sink refusal and a disclosure-sink refusal, and an admitted
  twin of one of them. Blinding the printed verdict was seen to produce
  `missed-G9 4 FATAL`.

- **RETAIN row (G-RETAIN, issue #1811 group 3)**: `Oracle.retainRowB` —
  `RevL.GRetain.retainRowB` — over the persistence scope, the sink and the
  walk the checker itself REPORTS, at the instant the checker itself
  COMPARED. **This row is the rule on the corpus, NOT the coverage of the
  checker's walk**, exactly as the `TAINT` row is: its premises ARE the
  checker's own refusal, so it cannot tell "the rule holds at this sink" from
  "the checker never reached the sink". The coverage of the *retention* walk
  is still unproved and out of this row's reach; the `GC` row that issue
  #2108 adds covers the **taint** walk, not this one.

  **The `now` fact, and how the harness pins it.** The rule's verdict depends
  on the wall clock, and the refusal carries the instant it used. The
  harness therefore sets `REVL_RETENTION_AS_OF` (the checker's own
  `retention.AS_OF_ENV`) to a fixed instant **once, at module import**, before
  any `compile_files` call, so every refusal the run assembles — and every
  re-decision the exporter makes — is taken at the same instant. The pin was
  measured verdict-neutral for the whole corpus: a full oracle run with the
  pin and one without differ in **zero bytes** of output, so the only verdict
  it moves is the retention file's own. `retain_coverage` asserts the row's
  `now` column equals that pinned instant, so a run whose checker evaluated
  at some other instant fails the gate instead of silently recording a row
  about a different clock.

  What the exporter reads out of the refusal, and from where — the
  `G-RETAIN` refusal passes **no `navigate`** at all, so every column but the
  code and the category comes from text:

  * the **sink** and the **scope** — the refusal message's
    ``flows into the persistence sink `<sink>` (a `<scope>` crossing)``;
  * the **policy** — the message's ``a `Retained[T, <policy>]` value``;
  * the **`now`** — the message's ``the deadline passed at <ISO>``, converted
    to epoch seconds; this is the checker's own `evaluation_instant()`;
  * the **`until`** — the **hint**'s ``may be kept until <ISO>``, the
    policy's own deadline, converted the same way;
  * the **chain** — the message's ``The retaining path is <chain>``, split
    on `" -> "`; the row's `hops` column is its length minus one, so the
    reported length is visible in the row's own output.

  A scope the harness table does not carry yields no row at all, so the
  refusal lands in the **fatal** `missed-G-RETAIN` rather than being read as
  an agreement the row cannot make: widening the checker's
  `PERSISTENCE_SINK_SCOPES` turns the row red, not green.
  `persistence_sink_scopes_are_modelled` fixes that table's two directions.
  A `G-RETAIN` refusal in category `retention` carrying the flow sentence
  files under `agree-G-RETAIN` or `missed-G-RETAIN`; the declaration-level
  `G-RETAIN` refusal, which carries no sink, no policy and no chain, is a
  different judgment and falls through to `out-of-fragment`.
  `retain_coverage` fails the gate unless the corpus row's own deadline is
  bracketed — the same row `fail`s one second past the deadline, holds AT it
  and holds a year before it — and `retain_not_vacuous` proves exactly that,
  so a row that ignored `now` would not elaborate.

### The G7 row, and what it is evidence of

The G7 row exists because the audit's central reading was that a
guarantee with 32 theorems and no oracle row is in a different epistemic
position from one with 9 theorems and a row, and the proved-row count
cannot tell them apart. G7 was the largest such body.

**It is the first row whose reference side executes rather than reads.**
`teardown_observation` builds a real `runtime.Frame`, registers entries
through the reference's own five seams (a bare guarded `lambda` for a
bracket, `transactional` / `compensation` for the activation body,
`transactional_method` / `compensation_method` for a provide method), and
drives the teardown the way the emitted body drives it: `drain` is
yielded last so it is disposed first, the activation-body disposers
unwind newest-first, and `begin` is yielded first so it is disposed last
and runs the Phase-2 drain. Nothing about the outcome is computed by the
harness.

**The corpus is enumerated, not chosen.** Every registration sequence up
to depth 3 over the five seams, constrained to body-before-method because
a provide method runs after its component activated and a stack that
interleaves them is a run that cannot happen, crossed with all three
verdicts: 267 scenarios. An oracle row over cases someone picked is an
oracle row over the cases they thought of.

**What the row does NOT check, said plainly.** The LIFO unwind of the
activation-body disposer stack is cordis's, and the harness stands in for
cordis there — so for body-seam entries the row checks the *dispositions*
(which revl owns, in `_Transactional.__call__`, `_Compensation.__call__`,
`_guard` and `drain`) and not the unwind order. The one ordering the row
checks against revl's own code is `drain`'s `reversed(deferred)` loop
over the method-registered entries (item 369). Phase-1
continue-and-record, the Phase-2 budget, escrow and cascading abort are
outside the model and so outside the row.

**Non-vacuity is enforced, not assumed** (`teardown_coverage`). The audit
found the capability-ceiling half of the `W` row agreeing *vacuously* —
no corpus file declared an integer parameter, so the `ceilingOKB` branch
the theorems are about was never entered (closed since; see the `W` row's
own ratchet, `attenuation_coverage`) — and recorded that this is the same
defect class as roadmap item 429's self-host byte-agreement being green
over a corpus that never reached the logic. So this row ships with
a ratchet that fails the gate unless the corpus still contains a witness
for each of: a replay of length ≥ 2 whose order is not registration order
(LIFO is distinguishable from FIFO), a compensation that ran strictly
after a Phase-1 inverse (the two phases are distinguishable from one
interleaved pass), a non-empty discharge column, a non-empty stranded
column, and a replay set that is a proper subset of its stack. Every
clause is a property some plausible defect would remove.

### The A8/R4 row, and the divergence it found

A8 carried 18 theorems and R4 carried 9, and neither had an oracle row:
both were checked against `docs/` and against `src/revl/recovery.py` read
by hand. The `O` row closes that, and it is the second row whose reference
side executes.

**What the corpus is.** A recovery verdict is a property of a durable
log, so the scenario is a log: every multiset of up to two content
records over nine shapes — an undeclared transactional inverse, a
declared-idempotent one, one whose re-issue FAILS, a compensation, an
in-process effect, a reconstructible boundary effect declared and
undeclared, a closure-only one, and a deferred emission — crossed with a
durable `discharge` set (none / one / all), the at-most-once fences (none
/ all), and the trailing decision record (none / `aborted` /
`activation-complete` / `commit-approved` / `fork-frozen`). 1620
scenarios, enumerated rather than picked, for the same reason G7's are.

**What the reference does.** `recovery_observation` writes the records as
a real JSON-Lines WAL — the schema `revl.wal.read_wal` reads — and calls
`revl.recovery.recover` over it with a `DictWorld` that watches what is
actually applied. That distinction is load-bearing exactly once: a fenced
inverse resolved by a durable `aborted` record lands on
`transactionalRolledBack` and is NOT applied, so reading the applied set
off the report rather than off the world would report an apply that never
happened.

**The re-issue oracle is a fact, not an assumption.** 243 rule 6 makes
the inverse fallible and the model carries that as a parameter
(`ok : Seq → Bool`); the corpus ships `fails` rows and the world raises
on them, which is what puts `Disp.residue .restoreFailed` under the row at
all. Fixing it to `okAll` would have made one whole residue kind
unreachable.

**The divergence it found.** `RevL.Lemmas.dispose` and `reissued` modelled
the legacy `effect` family as "reconstructible ⇒ re-issued", which is what
`_roll_back` did before item 309 §3a was extended to it. The shipped code
refuses an UNDECLARED reconstructible boundary inverse whose fence is
already durable and reports fenced-residue instead
(`test_idempotent_inverse_309.test_undeclared_boundary_inverse_applies_once_then_fenced`
pins it). The model was therefore *weaker than the implementation* in the
dangerous direction: it claimed a clean re-issue and no residue where revl
refuses and reports one. 60 of the 1620 scenarios disagreed. Fixed here by
giving `Rec.effect` the `undoIdempotent` flag the reference reads and
adding the fence branch to both `dispose` and `reissued`; `SemLog` only
ever produces `.effect s true false false`, so no semantic lemma and no L2
theorem moved.

**Non-vacuity is enforced** (`recovery_coverage`), on the reference's own
observations rather than on the model's predictions: all three outcomes
and both roll-forward routes, a run that applied an inverse and a
roll-back that applied none, a committed seq retained and a fenced one
refused, a declared-idempotent inverse applied freely over a durable
fence, a fenced inverse resolved by an `aborted` record, every residue
kind the model can produce, and a clean abort over a non-empty log — R4's
headline, and the one shape a model that reported everything would fail.

Each column was also watched to FAIL before being relied on. Perturbing
the shipped side one invariant at a time — `_replay_tier` stops fencing
(80 mismatches), a committed seq is rolled back (198), a completed
activation rolls back (324), a closure-only inverse is dropped from the
residue (120), a re-issued compensation is reported clean (56), a failed
re-issue is reported clean (52), the `aborted` record stops deciding the
fenced branch (11) — reddens the row every time, and on the column the
invariant belongs to.

**The row was seen to fail before it was trusted.** Three independent
injections, each reverted:

| Injection | Caught by | Result |
|---|---|---|
| `drain` replays method-registered transactional entries FIFO (delete item 369's `reversed`) | the D row | exit 1, **8 mismatches**, e.g. `g7/abort/TT: reference=('e0','e1') formal=('e1','e0')` |
| `_Compensation.__call__` discharges on abort instead of deferring to Phase 2 | the D row | exit 1, **64 mismatches**, the compensation correctly reported as having moved from the replayed column to the discharged column |
| `sorry` in `RevLOracle.row_is_total` | the axioms gate over `harness/Oracle.lean` | exit 1, `depends on sorryAx — unfinished proof` |

Two further injections were caught *earlier* than the row, which is worth
recording because it says where each layer bites. Making the printed
replay column ignore the verdict does not produce a mismatch — it fails
to **elaborate**, because `mem_replayedLabels_iff` is what ties the
column to `RevL.G7.replayed_sound` / `replayed_complete`. Making the
model run compensations inline in Phase 1
(`EntryKind.inPhase1 .compensation := true`) does not produce a mismatch
either — it breaks `lake build`, because the G7 theorem file pins the
phase split. So model-side drift is caught by the theorem layer and
implementation-side drift by the row, and the two are complementary
rather than redundant.

**Bridge theorems** (all inside the axioms gate, `run_gate.sh` step 5):
`mem_replayedLabels_iff` proves the replayed column is exactly the
model's replay set (soundness left-to-right, completeness
right-to-left); `mem_dischargedLabels_iff` and `mem_strandedLabels_iff`
do the same for the other two columns; `row_is_total` proves the three
columns account for the whole stack via
`RevL.Semantics.book_lengths_add`, so an entry cannot fall off the row by
appearing in no column; and `replayedLabels_phase_order` pins the printed
order as the whole Phase-1 pass, LIFO, then the Phase-2 drain, LIFO,
which is what makes the column's *order* checkable.

**What is still a private restatement in `Oracle.lean`**, listed so the
gate's reach is not overstated: `g4OK` (the G row — the G4 model is
indexed by `RevL.Syntax.Stmt` and the export carries call facts, not
statement terms), `methodBoundOK` (the P row — the model states no
declaration-versus-reach bound at all; `RevL.Boundary.bodyBoundary`
enumerates crossing heads but never compares them to a declaration), and
`closeN` (the spawn-surface transitive closure feeding W —
`RevL.CapCeilings.reachIn` *is* that closure, but it is indexed by a
`Comp` carrying `body : List Stmt`, which the export cannot produce;
only the closure is local, the judgment it feeds is the model's).

### Census

Nothing is dropped and nothing is counted without being named. The block
below is GENERATED by the gate that measures it
(`python3 formal/harness/diff_corpus.py --write-status`), and `make formal`
fails when the checked-in text is not what the run produced. It used to be
prose: at issue #1169 this section claimed 654 verdicts over 305 files while
the gate printed 4526 over 216, and claimed 0 formal-strict while the gate
printed 1. Nothing compared the two, in either direction. Since issue #1768
the block holds no count that moves with the corpus, so it no longer
conflicts between pull requests that add `.rvl` files; the gate prints the
counts.

<!-- BEGIN GENERATED alignment: regenerate with `python3 formal/harness/diff_corpus.py --write-status` -->

The census totals (files, components, statements, verdicts compared and
agreeing) and the file count of every informational bucket are printed
by every gate run, `make formal` and `python3
formal/harness/diff_corpus.py`, and are not stored here: a count that
moves with the corpus made every pull request that added a `.rvl`
rewrite this block (issue #1768). The block changes only when a named
file joins or leaves a bucket below.

Checker alignment over the modeled files. Every bucket recording a
DISAGREEMENT fails the gate, in both directions: `missed-*` is the model
weaker than the checker, `formal-strict` and `formal-found-other` are
the model stricter than the language that ships. `out-of-fragment*`
means the model has no fact about the rule the checker refused under,
not that it disagrees.

An absence cannot disagree, so the two buckets aimed at a row the model
does carry are `ratcheted` instead: `out-of-fragment-G5` and
`out-of-fragment-G6` and `out-of-fragment-inverse` and
`out-of-fragment-witnessed` are held to the names in
`formal/out_of_fragment_ledger/`, which shrinks only. A file that JOINS
one fails the gate, and a record no longer in its bucket fails it until
it is deleted. So a new `undo` shape the `Prog` cannot resolve, or a new
G6 purity fixture, cannot arrive while the model stays silent about it.
`agree-*` and the generic `out-of-fragment` stay informational; that one
collects every refusal under a rule the model states no row about, so it
is the list of unbuilt work. `out-of-scope` is informational too and is
not a hole: a type-checker refusal (T1, T2, T3) or name resolution of
declarations and of the lifecycle test DSL, routed by an explicit rule
(`out_of_scope`), so it grows with corpus work that never touched this
layer. The G9 coverage axis (issue #2108) is not a bucket either: it is
a SECOND reading of a file the chain has already bucketed — whether the
checker's walk of its bodies is complete — so the gate prints its
agreeing count on its own line, and its two disagreements are the FATAL
`missed-G9-coverage` and `missed-G9-coverage-observation` rows below.
Recording the agreement as a bucket would give one file two of them,
which is what the one-bucket-per-file maps the fatal list, the ratchets
and the census total are built on.

| bucket | files | gate |
| --- | --- | --- |
| `agree-A1` | printed by the gate | informational |
| `agree-A2` | printed by the gate | informational |
| `agree-A6` | printed by the gate | informational |
| `agree-A9` | printed by the gate | informational |
| `agree-G-COUNCIL-SPLIT` | printed by the gate | informational |
| `agree-G-MODEL-PLACE` | printed by the gate | informational |
| `agree-G-RETAIN` | printed by the gate | informational |
| `agree-G1` | printed by the gate | informational |
| `agree-G2` | printed by the gate | informational |
| `agree-G3` | printed by the gate | informational |
| `agree-G4` | printed by the gate | informational |
| `agree-G5` | printed by the gate | informational |
| `agree-G6` | printed by the gate | informational |
| `agree-G9` | printed by the gate | informational |
| `agree-accept` | printed by the gate | informational |
| `agree-intercept` | printed by the gate | informational |
| `agree-prelude` | printed by the gate | informational |
| `formal-found-other` | 0 | **FATAL** |
| `formal-strict` | 0 | **FATAL** |
| `missed-A1` | 0 | **FATAL** |
| `missed-A2` | 0 | **FATAL** |
| `missed-A6` | 0 | **FATAL** |
| `missed-A9` | 0 | **FATAL** |
| `missed-G-COUNCIL-SPLIT` | 0 | **FATAL** |
| `missed-G-MODEL-PLACE` | 0 | **FATAL** |
| `missed-G-RETAIN` | 0 | **FATAL** |
| `missed-G1` | 0 | **FATAL** |
| `missed-G2` | 0 | **FATAL** |
| `missed-G4` | 0 | **FATAL** |
| `missed-G5` | 0 | **FATAL** |
| `missed-G6` | 0 | **FATAL** |
| `missed-G9` | 0 | **FATAL** |
| `missed-G9-coverage` | 0 | **FATAL** |
| `missed-G9-coverage-observation` | 0 | **FATAL** |
| `missed-intercept` | 0 | **FATAL** |
| `missed-prelude` | 0 | **FATAL** |
| `out-of-fragment` | printed by the gate | informational |
| `out-of-fragment-G5` | in the ledger | ratcheted |
| `out-of-fragment-G6` | in the ledger | ratcheted |
| `out-of-fragment-inverse` | in the ledger | ratcheted |
| `out-of-fragment-witnessed` | in the ledger | ratcheted |
| `out-of-scope` | printed by the gate | informational |

The members of the ratcheted buckets are not listed here. Each is one
file under `formal/out_of_fragment_ledger/<bucket>/`, which the gate
holds equal to its bucket in both directions; `python3
formal/harness/diff_corpus.py --show-ledger` prints them, sorted,
without running the corpus. A list here put two pull requests that each
added a name next to the other's in conflict (issue #1768).

`agree-G5` says which row saw the crossing: the `U5` registration count,
or the `G` row refusing the component through the marker rule.

- `U5`: `examples/rejections/g5_undo_arrow_emission.rvl`
- `U5`: `examples/rejections/g5_undo_fn_emission.rvl`
- `U5`: `examples/rejections/g5_undo_fn_value_emission.rvl`
- `G`: `examples/rejections/g5_undo_handle_emission.rvl`
- `U5`: `examples/rejections/g5_undo_handle_ref_arg.rvl`
- `U5`: `examples/rejections/g5_undo_handle_ref_let.rvl`
- `U5`: `examples/rejections/g5_undo_method_ref_alias.rvl`
- `U5`: `examples/rejections/g5_undo_method_ref_if_arm.rvl`
- `U5`: `examples/rejections/g5_undo_method_ref_let.rvl`
- `U5`: `examples/rejections/g5_undo_method_ref_list.rvl`
- `U5`: `examples/rejections/g5_undo_method_ref_match_arm.rvl`
- `U5`: `examples/rejections/g5_undo_method_ref_record.rvl`

<!-- END GENERATED alignment -->

(The corpus grows; the shape of the census does not. The step-6 numbers
were 296 / 182 / 403 and 371 verdicts; the 387 before the G7 row; 654 as
of `chore/formal-review`; 4526 before the A9 and A2 rows; 4761 with A9
alone; 4794 with the A9 converse and 5071 with the A2 rows, each measured
on its own branch before the two landed on one corpus.)

- The 28 parse refusals are LISTED by name and code, not counted. The
  previous "(28 parse-error skips, loud)" parenthesis hid
  `examples/rejections/g4_missing_undo.rvl` — literally the shape G4
  forbids, refused with code G4 — and both G6 fixtures
  (`g6_closure_mutates_capture.rvl`, `g6_impure_statement.rvl`, both
  G6), along with a G2, a G8 and an A8 fixture.
- The 138 componentless files were previously in no bucket at all. They
  are now reported by checker code, with every non-`accept` one named:
  that surfaces `g1_template_undeclared.rvl` (a G1 the model never sees)
  and five `g4_extern_*` fixtures whose refusals are extern-declaration
  shapes, not composition shapes. The 83 accepted ones are backend emit
  corpora with no composition in them. Full list in
  `harness/out/no_manifest.txt`.

Checker alignment: each modeled file is compiled with the real checker
(`compile_files`, the door the CLI takes, so a `use` resolves against the
file's own directory) and its refusal code compared against the formal
verdicts. The gate prints the bucket counts on every run, and the
generated block above names every file in a bucket that fails or
ratchets; what the buckets MEAN is here.

Every bucket recording a disagreement is a **gate failure**, in both
directions. `missed-G4`, `missed-G2`, `missed-G5`, `missed-A9` and
`missed-A2` are the checker refusing where the model sees nothing, which
is the model weaker than what revl enforces (item 418 step 7; issues 1167, 1166,
#1169). `formal-strict` (the model refusing a file the checker ACCEPTS)
and `formal-found-other` (the model refusing a file the checker refuses
for an unrelated reason) are the opposite direction, and were
informational until issue #1169. That is exactly how three files sat in
them unread: agreement failed loudly, strictness did not, so the two
buckets could not fire. A model stricter than the checker is a model of a
different language, and every theorem proved over it is proved about that
other language, so both now fail the gate too.

A genuine fragment gap does not land in either. It lands in
`out-of-fragment` (the checker refused under a rule the model states
nothing about) or, where the rule IS modelled but the facts stop short, in
a named `out-of-fragment-G5` / `out-of-fragment-G6`. A refusal that is out
of scope BY KIND lands in neither: since issue #1810 a type-checker
refusal (T1, T2, T3) and an uncoded name-resolution refusal (the
lifecycle test DSL's names, a declaration naming an unknown or duplicate
service) file under the informational `out-of-scope` bucket, by the
explicit rule `out_of_scope` in `formal/harness/diff_corpus.py`, never by
a list of files. A guarantee-coded refusal never does, so
`out-of-fragment` is the list of unbuilt work:

- **G5**: the U5 row is stated over the file's `Prog` — the extern table
  plus the fn call graph, so it counts a teardown crossing only where the
  `undo` reaches it through a declared fn or extern. Since issue #1792 the
  exporter resolves an inverse's indirections the way
  `lower._walk_inverse_emissions` does (`inverse_reach_heads`) and adds
  what they reach to the inverse heads: a called `let`-bound arrow
  contributes its body's heads, a reference to an emitting fn passed as a
  value contributes that fn, and a read of an `emission` service operation
  off a spawn handle (directly, through a `let`, a second `let`, an
  `if`/`match` arm, a list element or a record field) contributes the
  operation, which the `Prog` declares as an `emission` boundary named
  `<Service>.<op>`. A call to a provision operation written in the slot
  (`undo w.task.run(...)`) is left to the `G` row, which refuses it as an
  unmarked emission. What still leaves the `Prog` at the first hop is a
  call through a function-typed parameter (`undo f(key)` with `f` a
  method parameter), which the checker refuses as an indirection it
  cannot bound; no corpus file has that shape today, so the
  `out-of-fragment-G5` list is empty and any file joining it reds the
  gate. Every G5 fixture is `agree-G5`, and the bucket says by which row.
  `missed-G5` is reserved for an `undo` the `Prog` CAN resolve whose
  crossing the fold still counts as zero, which is the model genuinely
  going blind.
- **G6**: revl's G6 is "purity outside effect forms" and the
  duplicate-binding refusal. The model's `C` row is the issue-276
  confinement surface, a different judgment about a different thing: it
  `fail`s on a hundred-odd corpus files the checker ACCEPTS, because a
  host root like `Map.new` is not a declared require. An `agree-G6` keyed
  on a `C` fail would therefore be an agreement that cannot fail, which is
  the informational-bucket problem one level down. The duplicate-binding
  refusal is decided by its own row since issue #1812 (`BU`, above), so a
  G6 `binding` refusal is `agree-G6` or the fatal `missed-G6`. A G6
  purity refusal is still reported as out of fragment; no corpus file
  outside the parse refusals has one today, so the `out-of-fragment-G6`
  list is empty and a file joining it reds the gate.

The A9 row moved
`examples/rejections/a9_provide_key_not_declared.rvl` from out-of-fragment
to agree-A9 and nothing else; its converse added
`examples/rejections/a9_provides_without_block.rvl` to agree-A9 (it was
`missed-A9` FATAL under the one-direction row, which is how PR #1184 found
the gap) and `tests/formal_corpus/a9_routes_installs_key.rvl` to
agree-accept. The A2 row moved
`examples/rejections/a2_acquire_after_provide.rvl` from out-of-fragment
(54 before either) to agree-A2. The three files that used to sit in the
two informational buckets were cleared by issue #1169: `notes.rvl` and
`interpose_observe.rvl` by the `compile_files` door, the `emitarg` marker
context and the direct-extern bound (PR #1174), and
`g5_undo_handle_emission.rvl` by the G5 arm.

Movements from the pre-step-6 buckets (52 agree-accept, 2 agree-G2, 6
agree-G4, 36 formal-strict, 13 formal-found-other, 21 out-of-fragment),
each explained:

- **36 formal-strict → agree-accept**, emptying the bucket. 32 of them
  were `V(ok, fail)` from reading the closure column as a checker-visible
  refusal; it is not one. `compile_source` type-checks and links ONE
  file, and `lower._link` reports nothing for a requirement with no
  in-file provider — that key is resolved against the rest of the
  composition at link time. Requirement closure is therefore reported in
  the V row and excluded from the alignment comparison, which uses
  `disjoint` and `link` (both genuinely checker-visible). The other 4 are
  the realm/template fix: `examples/tenants.rvl`,
  `tests/fixtures/canary_tenants.rvl` and `tests/fixtures/erase_realms.rvl`
  were `disjoint=fail` under the bare-key rule and are admitted by the
  slot rule, and `examples/tenant_attenuation.rvl` was `disjoint=fail`
  because its two spawn *templates* both provide `worker` — templates are
  not composition members.
- **13 formal-found-other → out-of-fragment**, emptying the bucket. Every
  one was a closure-only failure on a file the checker refuses for an
  unrelated reason (7 A1, 1 A6, 2 T1, 1 G1, 2 REVL). With closure out of
  the comparison the model is clean on them, and the refusal is honestly
  out of the modeled fragment rather than a phantom "the model found
  something else".
- **1 out-of-fragment → agree-G3**: `examples/rejections/g3_dependency_cycle.rvl`.
  This is the new bite, and it was not predicted by the step-6 brief. The
  harness had no G3 verdict at all before; the `link` column derives no
  admission order for the cycle, and the checker's G3 refusal now has a
  model verdict to agree with. `G3` was added to the alignment's
  guarantee-code arm for it.
- **28 parse refusals and 138 componentless files** enter the census for
  the first time. Verdicts compared rose 343 → 371 purely from the 28
  refusal rows; the 138 componentless files get no verdict (a vacuous
  `ok` over an empty composition would be inflation) and are reported by
  name instead. (The corpus was 292 files with 134 componentless when
  step 6 started; four more componentless fixtures landed on main during
  the work, and no other number moved with them.)
- Every G, P and W verdict is **unchanged**, 182 + 25 + 6 of them,
  bit-for-bit. Routing the coverage relation through the proved `Covers`
  and adding the ceiling half was expected to move nothing: the corpus
  carries no integer-valued capability parameter, so `stripCeilings` is
  the identity and `CeilingOK` is vacuous on it. The ceiling half is
  still wired on both sides, so the first corpus file to declare one is
  compared rather than ignored.

Known fidelity limits of the shaped model, deliberately not papered over:

- An emission reached through a spawn handle, an emission extern, or a
  transitively-emitting named function contributes the unnameable `*`
  capability to the ATTENUATION fold rather than a resolved boundary. That
  mirrors the checker's own `*` (`_emit_step_caps_pairs`), but it is
  coarse: `*` is covered only by `*`. The provide-method BOUND column
  reads the capability tokens the call reaches instead, as the checker's
  `_emitting_capabilities` names them (issue #1455): a scoped extern is
  its scope, an unscoped one its name, a `fn` the union of what it
  reaches. A spawn handle stays `*` in both.
- The attenuation fold reads a child's `emit` STEPS and value forms alike
  (the `A`/`F` rows), while the checker's `_collect_emit_caps_pairs` reads
  `emit` steps only. So a spawned child whose provide method writes
  `let r = emit charge(n)` is refused by the model and admitted by the
  checker, which lands in `formal-strict`; no corpus file has that shape
  today. The model is the fail-closed side of that difference.
- The approval floor (`AP` row) reads an edge the exporter can name: a
  `let a = await approval[C]`, an `Approval[C]`-typed parameter or
  annotated `let`, and a `let` alias of one. Any other edge reads as none,
  the fail-closed direction. Glob scopes are modelled for `*` and `?`
  only, and the required tokens come from the file's own externs.
- Capability **ceilings** are modeled on both sides now (the model's
  `CeilingOK`, the checker's `split_ceilings`), but the corpus exercises
  neither: no file declares an integer-valued capability parameter, so
  the two agree on it vacuously.
- A **routed** key (`isolate k in realms(...)`, item 162) is modelled in
  the shared realm for the V row: `_isolate_map` ignores `RouteStmt`, the
  routed REQUIREMENT is elided from the V-row manifest on both sides
  (`Oracle.toLComponent`, `reference_from_tsv`) because the linker resolves
  it per leg and `LComponent.realm` places a key in one realm, and the
  route reaches the model only as the A9 installation fact (`PR`). Item
  162's "every routed realm needs a provider" check is not under the row.
- The `closed` column of the V row is `RevL.Manifest.RequiresClosed` over
  the file's own components. It is a real model predicate and a real
  question about a composition, but a single `.rvl` is not necessarily a
  composition, so it is reported and not aligned. 51 files carry
  `closed=fail` for that reason and none of them is a finding.

## TODO (in dependency order)

1. ~~**Close the modeled G4 gaps**~~ — **done**. The export now carries a
   reachability model for provide-method and spawn bodies (B/Q/C/K/A/F/S/H
   facts) and the oracle grew the P and W verdicts over it; the
   `missed-G4` bucket is empty and all five files are modeled by the rule
   their rejection comment names. No checker change: `src/revl/` is
   untouched. Residue, tracked above under *fidelity limits*: unnameable
   `*` for handle/extern-reached emissions, and ceilings deferred to 2.
2. **Capability ceilings/budgets** (2.0 features): parameterized
   capabilities over the `Ctx` model. **Mostly done.** The `(T,P)`
   algebra of `src/revl/cap_order.py` is modelled in the L1 farm
   `RevL.Lemmas.CapLemmas` (token + valuation, the component-wise path
   order, discrete resource values, numeric ceilings, the
   resource/ceiling split), and `RevL.Theorems.CapCeilings` proves the
   nine rows above: the order is a partial order, attenuation is monotone
   downward along a lineage of admitted spawns, budgets only shrink, the
   runtime counter is not overdrawn, and the whole thing composes with
   G6's confinement through the key-to-token bridge (`capKeys`).
   **What is left**, two halves, of which (a) has landed:
   (a) ~~*theorem side*~~ — **done**. `held` and `reach` are now
   FUNCTIONS of a component shape, not given lists:
   `stmtCaps`/`bodyReach` track `_collect_emit_caps_pairs` (emit steps
   only; a `req` receiver resolves through `_cap_keyed` to the
   capabilities the service behind the key DECLARES, anything else to
   `*`), `heldCaps` tracks `_held_capabilities_pairs`, `reachIn` tracks
   `_spawn_surface_closure` as a fuel-indexed unfolding, and
   `SpawnsAdmitted` tracks `_check_spawn_attenuation` over
   activation-body spawns only (`_activation_spawn_sites`).

   A crossing has TWO names here, as it does in the harness (issue 1132)
   and in `lower.py`: the declared boundary the `emission[...]` clause
   names, and the local wiring key it was reached through. Both are
   derived, by one traversal under two `Namer`s — `capsOfDecls` for the
   capability column (`heldCaps`/`bodyReach`, what `SpawnsAdmitted` and
   every `Lineage` theorem fold over) and `boundsOfDecls` for the key
   column (`heldBounds`/`bodyBounds`, what `capKeys` and G6 confinement
   read). `Iface` now carries a service's whole emission declaration, one
   `Decl` per declared capability and one `none` per emission method that
   names no capability list, which is `_held_capabilities_pairs` arm for
   arm. An entry that declares nothing falls back to the SERVICE in the
   reserved `svc:` namespace (`lower._UNDECLARED_NS`), so neither a key
   nor a service spelling is ever a bare fold element in the boundary
   namespace. The `capKeys` bridge stops being an
   assumption: `derived_held_tokens_are_declared_keys` proves the bound
   column's tokens are exactly the component's declared `requires` keys,
   and that no element of the capability column is a bare key.
   `same_key_different_boundary_refused` is why the split is not
   cosmetic: a parent wired `kv: KvA` spawning a child wired `kv: KvB`
   derives the SAME bound list on both sides — the fold over it
   attenuates and admits the edge — while the capability column refuses
   it, which is what the reference does under G4.
   `same_key_undeclared_boundary_refused` (item 561) is the same pair
   where NEITHER service declares a token: the capability column falls
   back to the SERVICE rather than to the consumer's key, which is what
   a bare `emission` names
   (`tests/formal_corpus/g4_spawn_widens_capability_same_key.rvl`).
   Running the fold in the key namespace, which this section did before
   issue 1142, made the derived layer a theory about a different language
   than the one that ships.

   Five of the nine theorems are re-stated with their `Lineage` (and
   for confinement, `TypedIn (capKeys Γ)`) hypotheses discharged from the
   program text — `derived_attenuation_monotone`,
   `derived_lineage_ceiling_le`,
   `derived_budget_never_exceeds_root_ceiling`,
   `derived_confinement_within_ceiling`,
   `derived_no_star_amplification`. The other four
   (`cap_order_partial`, `spend_within_budget`,
   `parameter_widening_refused`, `ceiling_check_not_subsumed`) never had a
   held/reached hypothesis to discharge; the last of them gains a derived
   twin anyway (`derived_ceiling_check_not_subsumed`).

   Named residue, stated rather than assumed away. L0 has no constructor
   for a spawn handle, an emission extern, a named function, a spawn
   site, or a manifest, so `Comp` carries `handles`, `spawns` and
   `requires` alongside the `body` (the reference reads the last two from
   the spawn registry and the manifest too), and an `emit` with no call
   head is the fragment's stand-in for the extern / emitting-function
   receivers. Every emission through one of those derives the unnameable
   `*`, exactly as the checker's `else caps.add("*")` does;
   `NameableEmission` is the hypothesis this forces, carried explicitly on
   `derived_no_star_amplification` and on half of
   `derived_confinement_within_ceiling`, and
   `derivation_refuses_unnameable` is the concrete price of dropping it.
   `derived_no_star_amplification` no longer needs a side condition on
   the wiring keys themselves: a service with nothing declared on it is
   folded in the reserved namespace, which `undeclCap_token_ne_star`
   shows is never `*`; what it needs instead is that no service behind a
   declared key declares `*`.
   One precision loss: an L0 call head is the receiver ROOT, so a key's
   cone unions over the service's emission methods where the reference
   picks the method being called — the derived gate is therefore at least
   as strict as the checker, never looser. Still not modelled: parse-time
   canonicalization (upstream of the order), and `cap_order.disjoint`'s
   deferred (D2) same-token clause.
   (b) ~~*oracle side*~~ — **done** (item 418 step 6). The W verdict is
   now `RevL.CapCeilings.Attenuates`: the ceiling-blind coverage fold
   over `stripCeilings` plus the separate budget attenuation, on the Lean
   side through `attenuatesB_iff` and on the Python side through
   `cap_order.split_ceilings`/`covers_set`. The corpus still has no
   integer-valued capability parameter, so the two agree vacuously — but
   the comparison exists, so the first one to appear is checked rather
   than ignored.
3. **L3**: `Trusted[T]`/`Secret[T]` non-interference. **Done**, see *G9*
   below. (The WAL half is item 4; these two used to be one entry numbered
   3 twice, with the taint half called done in one and deferred in the
   other.) The taint half turned out **not** to need an
   L0 change: taint is a property of a value flowing along a path, L0's
   `Ctx`/`ReachIn` is a property of which keys a statement touches, and
   the two meet at one bridge (`taint._origin_of`: the origin a crossing
   mints is read off its declared capability scope). So the label algebra
   went into the new L1 farm `RevL.Lemmas.TaintLemmas` and the guarantees
   into the new L2 file `RevL.Theorems.G9_NoAuthorityFromUntrusted`,
   exactly as items 294/66/260 went into `CapLemmas` + `CapCeilings`.
   **L0 was untouched by this work and no pre-existing theorem's axiom set
   moved.** (Item 418 step 5 later did change L0, in `RevL.Semantics`; that
   is scoped to G7 and is described above.) What is proved is the flow
   *rule*; what remains open is
   the *coverage* of the checker's walk, which needs the L0 growth item
   418's ordered exit schedules ahead of it — see the G9 section for the
   named obligation and why it is not statable today.
4. **WAL commit/abort discharge. Done**, as R4 and A8 in the table above.

   This needed an operational semantics, which roadmap item 418 correctly
   said L0 did not have, so it was built additively in the L1 farm
   `RevL.Lemmas.WalLemmas` rather than by editing L0: **L0 was untouched by
   this work**. The farm carries (a) the record set, decision function
   and roll-back walk of `src/revl/wal.py` + `src/revl/recovery.py`, (b)
   a `Run`/`RunStep` model in which a crash is a *prefix*, and (c) a
   five-form small-step relation `SemStep`/`SemSteps` in which taking an
   effect step is what **appends its record and creates its referent** —
   so R4/A8 are stated over the effects that actually ran, not over a
   fabricated log. `Body.done` and `Body.fail` are stuck, and `fail` is
   L-Raise's failing step (418 step 3's prerequisite; the log also
   distinguishes a discharged transactional entry from a replayed one,
   which is 418 step 5's distinction).

   **Crash cuts covered:** between the durable write and the effect
   (`fence_before_apply_at_every_cut`, quantified over every cut);
   during teardown (abort-then-crash, fence durable, no `aborted`
   record); inside the approved-to-discharged window
   (`approved_decides_the_crash_window`).

   **Crash cut NOT covered, and not claimed:** a *witnessed* mutation
   logs its descriptor AFTER the forward extern returns `Ok`
   (`backends/python/emit.py`), so a crash between the mutation and its
   record leaves a mutation with no record at all. Every theorem here is
   relative to the durable log, and `SemStep.witnessed` collapses that
   window into one step. Durability is a floor, not a theorem: `WAFrom`
   orders the fence append before the apply; that `fsync` reaches the
   platter is a host obligation.

   **Also not modelled:** the roll-forward window's `flush-residue`
   surface (R4 is stated for the abort path), cascading abort,
   compensation drain and escrow, and item 250's frozen fork beyond
   being a third `Outcome` the commit/abort dichotomy is hypothesised
   away from.

   **Honest weighting of the rows**, per item 418's finding that a
   statement can be true and empty: `revert_on_failure`,
   `residue_is_exactly_what_remains`, `fence_before_apply_at_every_cut`,
   `at_most_once_across_crash`, `declared_idempotent_replay_free` and
   `crash_cut_converges` carry real content and each has a witness;
   `commit_record_is_the_decision` and
   `approved_decides_the_crash_window` are *specification agreement*
   with `recover`'s if-chain; `outcome_trichotomy` is *definitional* and
   is registered only so the weight of the "never a mixed state" claim
   is visible. "Never mixed" is about the VERDICT: per-seq dispositions
   are heterogeneous by design, and `mixed_disposition_admitted` pins
   that reading.

## Non-vacuity per theorem, and the layering gate (item 418, step 8)

Two things the axioms gate cannot see, now checked by
`scripts/run_gate.sh` before it ever calls `lake`.

### `scripts/nonvacuity.tsv` + `scripts/nonvacuity_gate.py`

`#print axioms` is exactly as clean on a theorem whose hypotheses cannot
all hold as on a load-bearing one. Item 418's review found G4/G5/G6/G8 to
be tautologies over a chosen inductive and counted "only 3 of 25 theorems
carry non-vacuity evidence". So every theorem registered in
`CheckAxioms.lean` now has a row in `scripts/nonvacuity.tsv` naming the
evidence, in one of four kinds:

- **instance** (188 rows): the hypotheses are jointly satisfiable, and the
  named witness theorems exhibit a concrete instance satisfying them. The
  sixteen `RevL.G9Coverage.*` rows added by issue #2108 are of this kind or
  the next.
- **necessity** (29 rows): the theorem refuses, so joint satisfiability is
  precisely what it denies. The witnesses show each hypothesis satisfiable
  on its own and the refusal not universal. `G3.linkOK_no_cycles` and
  `R4.abort_leaves_no_residue` are the shape.
- **concrete** (123 rows): the theorem is itself a computation on concrete
  data, so it has no hypotheses to satisfy. The gate accepts this label
  **only** when some other row cites the theorem as its witness, so it
  cannot be used to opt out.
- **contentless** (2 rows): true by definition rather than by any property
  of the subject. This is a finding, not a pass, and the gate prints both
  rows on every run.

The gate also fails on a row whose witness is not itself a registered
theorem (so witnesses are axiom-checked like everything else), on a
self-witnessing row, on a stale row, on `CheckAxioms.lean` and
`run_gate.sh` disagreeing about which theorems are registered, and — since
`chore/formal-review` — on a registered theorem this file does not name.
That last check was added because the record had already drifted: item
443's thirteen E-Stop theorems and three `CrossTier` theorems were
registered, witnessed and axiom-checked while the table above listed none
of them, and two rows went on describing the pre-443 unrestricted
statements of theorems that had since gained a `v.settles = true`
hypothesis. A proved theorem nobody can find and a record that overstates
what is proved are two halves of the same failure. That last
check is the one item 418's MEDIUM list wanted:
`Semantics.teardown_length` was once listed as proved in this file and
registered in neither.

**The two contentless rows, stated plainly** because they contradict how
the surrounding prose used to read:

- `RevL.G5.teardown_registers_nothing`. `registrations` is the constant
  zero function, so the theorem is true of every undo body including one
  whose only statement calls an emission.
  `RevL.G5.registrations_ignores_its_argument` proves the review's own
  probe, `forall u v, registrations u = registrations v`. The theorem is
  not *vacuous* (it has no hypotheses to be unsatisfiable) but it is
  *empty*: its conclusion follows from the definition and from nothing
  about undo bodies. The load-bearing G5 is `RevL.G5Classified`.
- `RevL.A8.outcome_trichotomy`. `Outcome` has three constructors, so this
  is definitional. It was already marked so in the table; the registry
  makes the marking machine-checked rather than editorial.

One further gap, found by the `chore/formal-review` audit and now closed:
`RevL.Semantics.discharged` appeared in three registered statements, and in
every one of them it was asserted **empty** (`discharged .halted log = []`,
`(discharged .halted stack).length = 0`) or summed inside a length
identity. So `book_lengths_add`'s three-way partition and
`halt_books_are_total`'s five-way one held with their `discharged` term
never exhibited non-zero, while the registry notes on `book_lengths_add`
and `estop_discharges_nothing` claimed a witness that no registered
theorem supplied. `G7.commit_discharge_is_not_vacuous` now computes
`discharged .commit stack` as a length-2 list containing the witnessed
mutation, and both rows cite it. The theorems did not change; what they
are worth did.

Nothing else in the registered set turned out to be vacuous. Writing the
witnesses did surface one real gap, now closed: the derived capability
lineage over the corpus (`wProgGood`) declares **no ceiling anywhere**, so
`lineage_ceiling_le` and `budget_never_exceeds_root_ceiling` had no
instance in which their `Lineage` hypothesis and their ceiling side
condition held together. `RevL.CapCeilings.ceiling_lineage_is_inhabited`
supplies one, a spawn edge narrowing `calls` from 3 to 2. Until it existed
those two theorems were conditioned on a pair of hypotheses this layer had
never exhibited jointly.

### `scripts/layering_gate.py`

This file's opening line used to call the L0/L1/L2 layering "enforced by
imports, not by hope", and item 418 recorded that nothing enforced it. The
script parses every `import` under `formal/RevL/` and fails on an L0 file
importing outside L0, an L1 farm file importing anything but L0 or
importing another farm file, and an L2 file importing another L2 file. It
also fails when an L1 or L2 module is missing from `RevL.lean`, because a
module outside the root import is a module outside the build and therefore
outside `CheckAxioms.lean`. The tree passes today: 5 L0, 7 L1, 18 L2
modules, no upward or sideways import.

### The oracle's own bridge theorems (`chore/formal-review`)

`harness/Oracle.lean` is outside `lakefile.lean`'s `lean_lib` root
(`roots := #[`RevL]`) and outside the directory the layering gate walks, so
`CheckAxioms.lean` could not reach it. Its nine `..B_iff` bridges are
exactly what this file cites when it says "the Lean side `decide`s the
proved model" — `linkOKB_iff`, `coversB_iff`, `attenuatesB_iff` and the
rest — and they were the only proofs under `formal/` outside every gate. A
`sorry` in one of them would have left `lake env lean --run` at exit 0 and
`make formal` green. The file now carries its own `#print axioms` block,
and `scripts/run_gate.sh` elaborates it a second time (without `--run`) and
feeds that block to the same `scripts/axioms_gate.py`. All nine are clean
on the standard three.

## Conventions for worker sessions

- State the theorem first with `sorry`, register it in `CheckAxioms.lean`
  and in this file as *stuck/TODO*, then fill it. The gate stays honest
  because `sorryAx` fails the build — a red build on a stated theorem is
  the system working, not a regression.
- Porting map: DESIGN.md §4 row → paper object → `src/revl/lower.py`
  (checker side) → the `tests/` file that currently witnesses the
  guarantee by execution. The test is the *example suite* for the formal
  model, not a substitute for it.
