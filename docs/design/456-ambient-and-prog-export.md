# 456: ambient composition and the whole-Prog export

Design note for two blocked items that turn out to be the same kind of
decision: issue #86 (roadmap item 186, Path C ambient/running-manifest
admission, with item 419c folded in) and issue #276's remaining half (formal
G5/G8 differential rows). Design only: no compiler change, no `src/` change,
nothing implemented. Companion docs:
[386-multi-error-reporting.md](386-multi-error-reporting.md),
[243-witnessed-externs.md](243-witnessed-externs.md),
[332-embeddable-gate-api.md](332-embeddable-gate-api.md),
`formal/STATUS.md` (the lattice section, item 418 step 4), and PR #468 (the
G6 row, the template every row below copies).

Sources read for this note, at `0e3eaf37`: `selfhost/lower.rvl` (`admit_src`
at 4803, the WHAT REMAINS block at 194), `src/revl/lower.py`
(`_check_and_lower` 6324-6856, `_link` 11526, `_raise_collected` 5999),
`src/revl/admission.py` (`_service_compatible`, `_admit_service_replacement`,
`_admit_handoff_replacement`), `src/revl/compiler.py` (`compile_files` 596,
the `ambient` dict at 766), `formal/harness/Oracle.lean`,
`formal/harness/diff_corpus.py`, `formal/RevL/Lemmas/ClassLemmas.lean`,
`formal/RevL/Theorems/G5_ClassifiedTeardownPure.lean`,
`formal/RevL/Theorems/G8_ClassifiedBoundary.lean`, `tools/build_gate_crate.py`.

---

## 0. The decisions, in one table

| # | Question | Decision | Section |
|---|---|---|---|
| D1 | Why are #86 and #276 the same decision? | Both are blocked on the same thing: the artifact each oracle diffs is too small. The self-host oracle diffs one program text; the formal oracle diffs per-statement `I` rows. Ambient admission needs a running manifest on both sides; G5/G8 need the whole `Prog` (externs, fns, call graph) on both sides. Neither gets there by adding corpus. | §1 |
| D2 | Order | 419c first (small, and the ambient oracle cannot order its refusals without it), then `admit_ambient` and its oracle, then the whole-Prog export. | §4 |
| D3 | 419c: change the wire? | No. `admit_src` keeps returning `"<TAG>\|<message>"` and keeps meaning "the reference's `diagnostics[0]`". The line rides an internal `Refusal` record and a collecting sink; the wire never carries it. `crates/revl-gate` and `revl-gate-wasm` regenerate with no API bump. | §4.1 |
| D4 | 419c: which line? | The line the REFERENCE puts on that refusal family, not the line the gate happens to notice it on. A per-family anchor table is the first deliverable of the slice. | §4.1 |
| D5 | Ambient entry point | `admit_ambient(src, running_src, replacing)` in `lower.rvl`: both sides build the running manifest from a second SOURCE text, so the oracle diffs manifest construction too. A JSON-manifest adapter for the shipped gate is a separate, later slice, not a prerequisite. | §3.1 |
| D6 | Ambient tag vocabulary | Two new tags: `DRIFT` (interface drift, `differs from the running manifest in a way that breaks`) and the existing `HANDOFF` tag for the state-shape refusal (`state hand-off on ... differs from the running manifest`). G2/G3/ROUTE/BOOT over the union keep their tags. | §3.1 |
| D7 | G5/G8 export | Two new fact rows, `EX` (extern declaration) and `FN` (fn declaration plus named callees), exported per file. `Oracle.lean` builds `RevL.Lemmas.Prog` from them and decides `inverseOK` / `registrations` / `stmtSurface` with new `*_iff` bridges. | §3.2 |
| D8 | Fuel | `fuel := fns.length + 1`, printed by the exporter on a `PG` row so both sides read the same number. The differential itself guards the choice (an under-approximating fuel shows up as a mismatch against the reference's fixed point). | §3.2 |
| D9 | First-class dispatch | Outside the Lean model (`*`). A fn whose body references an emitting callable as a value is exported with a `star` marker; every row touching it prints `n/a` on both sides and the coverage ratchet counts the exclusions. | §3.2 |
| D10 | Shared machinery? | Discipline yes, code mostly no. The two oracles have different sides (reference vs self-host; reference vs Lean). The one reusable artifact is the manifest projection (`M`/`R`/`C`/`K` rows already are one), which makes an optional Lean row for ambient composition (slice D) cheap. Do not force a shared exporter. | §3.3 |

---

## 1. The shared insight

Both issues stalled on "more corpus does not help", and the reason is the same
in both.

The self-host oracle (`tests/test_selfhost_lower.py`) compares `admit_src(src)`
with `compile_source(src)`. Every ambient consumer in `_check_and_lower` reads
the `ambient` dict: service replacement (`_admit_service_replacement`),
hand-off state replacement (`_admit_handoff_replacement`), and the union link
(`_link(program, components, ambient["components"])`). There is nothing in one
program text to compare those against. The oracle's unit of comparison has to
grow from a source to a (running composition, candidate source) pair.

The formal oracle (`formal/harness/diff_corpus.py` + `Oracle.lean`) compares
verdicts computed from TSV facts. PR #268 added the per-statement `I` rows and
PR #468 made them non-lossy and put G6 on them. G5 and G8 do not fit: their
load-bearing statements (`RevL.G5Classified.registrations`,
`RevL.G8Classified.stmtSurface`) take a `Prog` and a `fuel`, and `Prog` is the
extern table plus the fn call graph (`ClassLemmas.lean:233`). The `I` row
carries a statement's call heads and nothing about what those heads reach. The
unit of export has to grow from a statement to a program.

So the decision in both cases is an export-enrichment decision: what larger
artifact do both sides construct, from what input, and how is it compared. The
answers differ in content (a running manifest; an extern/fn table) but not in
shape, and PR #468 already set the discipline every new row must follow:

1. both sides compute the verdict from the same input, independently;
2. the row is shown to FAIL before it is shown to pass (a caught violation);
3. a coverage ratchet fails the gate if the corpus stops exercising both
   verdicts;
4. on the Lean side, a `*_iff` bridge proves the printed Bool is the model's
   predicate, and a `*_not_vacuous` theorem shows the predicate turns on the
   thing the row is about.

## 2. What is on main today (verified at `0e3eaf37`)

- `admit_src` is a first-refusal `Str` function. Every arm is an early
  `return tagged(TAG, msg)`; there are 42 `tagged(` sites in `lower.rvl`. Its
  phase order is: FOREIGN scan, nesting bound, parse, `check_cache_fns`,
  `check_reachable_fn_acquire`, `check_module_fns`, then per component
  (`comp.refuse`, `spawn_form_comp`, `check_component`), then `check_spawn`,
  the BOOT count, then `link_g2_g3` (G2, ROUTE, G3 edges, G3 cycle).
- The reference collects every recoverable refusal (item 386), dedups on
  `(code, filename, line, message)`, sorts by `(file rank, line)` with a stable
  sort, and reports `diagnostics[0]`. In a single-source compile the file rank
  is constant, so the order is by line, ties in pipeline order.
- Tokens carry `line` (`selfhost/lexer.rvl:16`; a template token's line is its
  END line, as in the reference lexer). Nothing in `lower.rvl` reads it onto a
  verdict today.
- `tests/test_selfhost_lower.py::test_which_refusal_wins_diverges_when_a_program_has_several`
  pins the divergence: the reference answers G2 (line 4, provision conflict),
  the gate answers G4 (a later line, earlier phase).
- `crates/revl-gate` calls `selfhost::admit_src(owned)` under `catch_unwind`
  and splits the wire at the first `|` (`tools/build_gate_crate.py:710`). Both
  `tools/build_gate_crate.py --check` and `tools/build_gate_wasm.py --check`
  digest `lower.rvl`; both must be regenerated on any change to it.
- The ambient dict (`compiler.py:770`): `services` (the running IR's services
  table), `components` (manifest entries minus `replacing` minus every
  component the candidate declares), `provision_services` (key -> service, off
  the full IR's `components[*].provides` map), `handoffs` (key ->
  `{type, component}`, off the full IR's `components[*].handoff`, dropped
  providers included on purpose).
- `_link` copies an ambient entry's `name`/`file`/`inject`/`provides`/
  `isolate`/`intercept`/`routes` (11563-11576). It does NOT copy `boot`. See
  §5.
- `lower.rvl`'s `CompD` carries `provKeys` (key, realm), `reqMap`
  (name, service), `iso`, `routes`, and a `refuse` string. `handoff` is read
  for its wiring rules only: `p_comp_body` threads a `hoff: Bool`; the key and
  the declared type are not kept.
- `lower.rvl` already has the pieces a running manifest needs from source:
  the services table with `params`/`returns`/`emission`/`capabilities`/`async`
  (`lower_to_ir`'s services section), and per-component headers.
  `selfhost/checker.rvl` has `compatible(expected, actual)`, the port of
  `typecheck.compatible` the §5 relation rests on.
- Formal: `diff_corpus.py` parses the corpus with `revl.parser` only, so a file
  the checker refuses at LOWER time still exports rows (only a parse refusal
  becomes an `X` row). `examples/rejections/g5_undo_*.rvl` all parse and all
  export `M`/`I` rows today. The corpus has 177 component files, 25 of them
  with externs (18 `emission`, 1 `witnessed`), and 94 fn declarations across
  them. Two of the three G5 fixtures are first-class-dispatch shapes
  (`app(wrap, key)`, an arrow), which the Lean model excludes (§3.2, D9).

## 3. The export-enrichment shape

### 3.1 Ambient: the running manifest, built on both sides

**The record.** What `_check_and_lower` reads off `ambient`, as a `lower.rvl`
type:

```
type RunSvc  = { name: Str, commutative: Bool, methods: List[RunM] }
type RunM    = { name: Str, params: List[Bind], returns: Str, isEm: Bool,
                 capsAny: Bool, caps: List[Str], isAsync: Bool, commutative: Bool }
type RunComp = { name: Str, inject: List[Str], provides: List[Str],
                 iso: List[ProvKey], routes: List[Route], boot: Bool }
type RunHoff = { key: Str, ty: Str, comp: Str }
type RunningM = { svcs: List[RunSvc], comps: List[RunComp],
                  provSvc: List[Bind],        // key -> service (provision_services)
                  hoffs: List[RunHoff] }       // key -> exported state shape
```

`RunM.params` needs the parameter TYPE strings (`_service_compatible` compares
them with `compatible`), so `MSig` is not enough; the IR services reader in
`lower_to_ir` already has them.

**Reference side.** `compile_source(running_src, "run.rvl")` gives the full IR
document; `compile_source(src, "diff.rvl", manifest=old, replacing=...)` runs
the ambient path. Nothing new in `src/`. The `_ref` helper in the self-host
test grows a `_ref_ambient(src, running_src, replacing)` twin that classifies
the drift and hand-off messages (D6).

**Self-host side.** `running_manifest(running_src) -> RunningM` in
`lower.rvl`, built from `parse_prog_ts` plus the existing services reader,
then

```
pub fn admit_ambient(src: Str, running_src: Str, replacing: List[Str]) -> Str
```

which lowers `running_src` to `RunningM`, drops `replacing` and every
component `src` declares (the reference's `dropped` set), and runs the
single-source phases with three insertions, in the reference's order:

1. after the parse, before `build_maps`: every candidate `service` whose name
   is in `RunningM.svcs` goes through the drift check (`_service_compatible`
   ported, the `providers_retained` regime decided by `_service_touchers` over
   `RunComp.inject`/`provides` and `provSvc`); every running service the
   candidate does not redeclare enters the gate's service map, so a candidate
   component may require or provide it without redeclaring it (today the gate
   has no such service and would misreport);
2. after the component loop, before `check_spawn`: the hand-off state check
   for each candidate component that declares a `handoff` on a key in
   `RunningM.hoffs` (`compatible(accepted, exported)`, the covariant
   direction; a cold key and an opted-out successor both admit). This needs
   `CompD` to keep the hand-off key and type (`hoff: Handoff` instead of
   `hoff: Bool`);
3. `link_g2_g3` takes the union: running components become link entries ahead
   of the candidate's (the reference's `entries` order), so a conflict fires
   on the candidate's entry and carries the candidate's declaration line.
   Routes resolve against the union's per-(key, realm) provider table.

`admit_src(src)` stays exactly `admit_ambient(src, "", [])` in behaviour; the
implementation should make it literally that once the sink from 419c exists,
so there is one phase spine, not two.

**How the oracle compares.** A new test module `tests/test_selfhost_ambient.py`
(the existing file is at 1950 lines; a new file also means the pre-commit hook
falls back to the full suite, so land it with targeted tests plus CI) with the
same `ns` fixture, holding:

- a hand-written pair corpus (`AMBIENT_ACCEPTED`, `AMBIENT_REJECTED`), each
  entry `(name, running_src, candidate_src, replacing)`, mirroring
  `tests/test_service_compat.py` and `tests/test_state_handoff.py` shapes plus
  the union-link cases (a candidate conflicting with a running provider in the
  same realm; the same key in a different realm admitting; a cycle closed
  through a running edge; a route to a realm only a running component
  provides; a second `boot`);
- three generators in the style of `test_generated_*_agree`: the drift matrix
  (add / remove / widen / narrow a parameter, narrow / widen a return, emission
  appears / drops, capabilities widen / narrow, `async` flips, `commutative`
  flips) crossed with {consumer retained, provider retained, nothing touches};
  the hand-off matrix over the `compatible` relation (identical, `T` to
  `Opt[T]`, the reverse, a changed `Map` parameter, no `handoff` on the
  successor); and the union-composition generator (a running composition of
  2-4 components, a candidate that replaces one of them by name or by
  `replacing`);
- the non-vacuity ratchet, the #468 way: each generator asserts that across
  its seeds BOTH verdict classes occur (an ambient corpus that only ever admits
  certifies nothing), and one mutation test per consumer flips a single field
  of the running source (a method's return type, a `handoff` type, a
  provider's realm) and asserts both sides flip together.

### 3.2 Prog: the extern table and the call graph, exported per file

**The rows** (`diff_corpus.py::export`, per component file, before its `M`
rows):

```
PG  <file>  fuel=<n>
EX  <file>  <name>  <pure|acquire|witnessed|emission>  <undo-callee|->  <compensate-callee|->  <caps,csv>
FN  <file>  <name>  <calls,csv>  <plain|star>
```

`EX` is `parser.ExternDecl` projected onto `RevL.Lemmas.ExternDecl`
(`name`, `cls`, `undo`, `compensate`, `caps`): the `undo`/`compensate`
callee names via `lower._callee_name`, `caps` from `ExternDecl.capabilities`.
`FN` is `RevL.Lemmas.FnDecl`: the named callees the fn body calls, collected
the way `_fn_emitting` already walks bodies in `diff_corpus.py` (calls only,
no value references). `star` marks a fn whose body references an emitting
callable as a VALUE (the `passed` set in `_emitting_capabilities`); D9 says
what happens to it. `PG` carries `fuel = len(fn_decls) + 1` (D8).

The `I` rows are unchanged. Their `heads` already spell a bare fn call as the
bare name and a service crossing as `root.method`; only heads whose root is an
`EX` or `FN` name resolve in `Prog` (`lookupExtern`/`lookupFn`), every other
head reaches `[]`, which is what the model says too (`calleesOf` on an
unknown name is `[]`).

**Lean side** (`Oracle.lean`): `parsePG`/`parseEX`/`parseFN`, a `progOf`
that builds `RevL.Lemmas.Prog` per file, and three deciders with bridges:

- `inverseOKB p fuel d := inverseOK p fuel d` is already a `Bool`; the bridge
  to state is `inverseOK_iff_registrations`: for a witnessed `d` with
  `d.undo = some u`, `inverseOK p fuel d = true ↔ registrations p fuel (.call u) = 0`
  (from `registrations_zero_iff` and `Cls.crosses_eq_not_admissible`);
- `teardownPureB p fuel b := decide (registrations p fuel b = 0)` with
  `teardownPureB_iff` (`registrations_zero_iff`, already proved), applied to
  `bodyOfHeads : List String → UndoBody` (one `.call` per exported inverse
  head, `seq`-folded), plus `bodyNames_bodyOfHeads : bodyNames (bodyOfHeads hs) = hs`
  in the style of `heads_exprOfHeads`;
- `stmtSurface p fuel (stmtOf r)` is already a computable `List Name`; the
  bridge is `mem_stmtSurface_iff : k ∈ stmtSurface p fuel s ↔ ∃ h ∈ stmtHeads s, k ∈ reachCaps p fuel h`
  (`List.mem_flatMap`), and the soundness/completeness pair
  `reachCaps_sound`/`reachCaps_complete` already says what membership means.

All three go into the oracle's `#print axioms` block and `run_gate.sh`'s
oracle-side list (propext/Quot.sound only; `registrations_zero_iff` currently
carries `Classical.choice`, which the gate tolerates, but the printed decider
should be `decide`-based so the bridge does not inherit more than it needs).

**Verdict rows** the oracle prints:

```
T5  <file>  <extern>            inverse=<ok|fail>         -- per witnessed extern, inverseOK
U5  <file>  <comp>  <index>     teardown=<ok|fail|n/a>    -- per effect statement, registrations over its inverse heads
S8  <file>  <comp>  <index>     surface=<caps,csv|n/a>    -- per statement, stmtSurface, deduped, sorted by the python side
```

**Reference side** (`reference_from_tsv`): build the fns/externs lists the
shipped fold expects (`{"name", "body"}` with a synthetic body whose calls are
the `FN` row's names, `{"name", "class", "capabilities"}` for externs) and call
`emission_analysis._emitting_capabilities` on them. This is the cap_order
rule from item 418 step 6: the python side calls the shipped algebra, it does
not restate it. `T5` is then `undo-callee not in caps` (and not a witnessed
name), `U5` is "no inverse head is in caps", `S8` is the union of `caps[head]`
over the statement's heads. Any fn marked `star` (or any name whose caps
contain `*`) makes the touching row `n/a` on both sides.

**Non-vacuity.** Three witnesses, two of which the corpus cannot supply today:

- a new admitted fixture `examples/witnessed_inverse_g5.rvl`: a `witnessed[fs]`
  extern whose `undo` names a `pure` extern, beside a fn that wraps an
  `emission` (so `S8` is non-empty somewhere and `T5` is a real `ok` over a
  non-empty reach);
- a new refused fixture `examples/rejections/g5_undo_fn_emission.rvl`: an
  `effect ... undo wrap(key)` where `wrap` calls an `emission` extern directly
  (no arrow, no value passing). It parses, so it exports rows; the checker
  refuses it at lower (G5, `_check_site_inverse_emission`); the `U5` row scores
  `fail` on both sides. This is the caught violation. (The existing three G5
  fixtures do not serve: two are `star` shapes, the third is a spawn-handle
  shape with no extern at all.)
- a refused fixture with a witnessed extern whose declared `undo` reaches an
  emission through one fn (the `sneakyProg` shape of
  `G5_ClassifiedTeardownPure.lean`), for a `T5` fail.

Plus `prog_coverage()` in `diff_corpus.py` (mirror of `confinement_coverage`):
fails the gate unless the corpus has a `T5` ok AND fail, a `U5` ok over a
non-empty inverse reach AND a fail, an `S8` non-empty surface AND an empty
one, and reports the `n/a` count. And on the Lean side, `g5_row_not_vacuous`
and `g8_row_not_vacuous` in the two `Classified` files: the shipped-side
perturbation is "the wrapping fn stops calling the emission" (registrations
goes to 0, the surface goes empty), proved on the `witProg`/`sneakyProg`
programs those files already define; registered in `CheckAxioms.lean`,
`run_gate.sh`, `nonvacuity.tsv`, and the `STATUS.md` table with the G5/G8
"Gap: no oracle row" cells rewritten.

### 3.3 Can the two exports share machinery?

Only at the projection layer, and it should not be forced.

- The ambient oracle diffs the reference against the self-host gate; its
  running manifest is a `lower.rvl` value built from source on one side and a
  `compile_source` IR on the other. The Prog oracle diffs the reference
  against Lean over TSV. Different sides, different transports.
- What IS the same object: the manifest projection of a compiled composition.
  `M`/`R`/`C`/`K` rows already are one (name, requires, provides, realms,
  template), and `RunComp` is that plus `inject`, `routes`, `boot`. One python
  helper, `manifest_projection(prog)`, can feed both the TSV exporter and the
  ambient test corpus's expected-manifest assertions, and that is the extent
  of the sharing.
- The payoff of the shared projection is slice D: an `AM` row (an ambient
  component, same fields as `M`) would let `Oracle.lean` decide
  `linkVerdict (ambient ++ new)` with the bridges it already has
  (`linkOKB_iff`), giving #86's composition half a third implementation for
  a few dozen lines. Optional; it does not gate #86 or #276.

The refusal-line work (419c) is not shareable and not formal; it is what makes
the ambient oracle's multi-refusal cases orderable, which is why it goes first.

---

## 4. The slice plan

Each slice names its files, its exit test, and the guards that must stay green.
Every slice edits `selfhost/lower.rvl` or the formal harness, so each needs an
owner holding those files exclusively (issue #86's triage note), and each
lands behind `python tools/build_gate_crate.py --check` and
`python tools/build_gate_wasm.py --check` (both, always) plus the item-429
coverage gates that re-run on any self-host change.

### Slice A: 419c, refusal ordering by line

**Files:** `selfhost/lower.rvl`; `crates/revl-gate/*` and
`crates/revl-gate-wasm/*` (regenerated, never edited); the two GENERATED
digests; `tests/test_selfhost_lower.py`; `docs/v2.0-roadmap.md` (tick 419c,
update 186's text).

**Shape.**

1. `type Refusal = { tag: Str, msg: Str, line: Int, seq: Int }` and a sink
   `List[Refusal]`. Every `tagged(TAG, msg)` site becomes
   `refuse(sink, TAG, msg, line)`; `seq` is the append index (the reference's
   stable-sort tie-break). `comp.refuse` (a `Str` today) becomes a `Refusal`
   or an empty tag.
2. Recovery granularity matches the reference's: per module fn, per component
   (the reference lowers each component in its own `try`, header-stubs a
   failed one, and still links), per post-pass (`_collect`), and `_link`
   collecting all G2/ROUTE/G3 refusals. Inside one component body the
   reference's Stage 2 statement-level recovery is NOT mirrored in this slice:
   the gate keeps first-refusal-per-component. That is enough for the
   `diagnostics[0]` agreement the exit test asks for, because a component's
   own first refusal by line is the one that competes with other components'
   and the link's.
3. `admit_src` returns the sink's minimum by `(line, seq)`, worded as before.
   `admit_all(src) -> Str` (newline-joined `"<line>|<TAG>|<msg>"`) is added
   for tests only; it is not part of the gate crate's surface.
4. The line anchor table. For every refusal family, the line the reference
   attaches, read off the raise site: a body refusal carries the statement's
   line; a module-fn twin carries the fn declaration line; the G2 conflict
   carries the SECOND provider's declaration line (`_link`'s
   `_line(entry["name"])`); the BOOT count carries the second boot
   component's line; the setup async-reach refusal carries the component's
   line; G3 cycles and spawn bounds/attenuation as `_link` and
   `_check_spawn_*` report them (read them, do not guess). An ambient entry
   has no line (`_link` defaults to 1). Put the table in the WHAT IT COVERS
   comment so the next porter does not re-derive it.

**Exit test.**

- `test_which_refusal_wins_diverges_when_a_program_has_several` goes red
  because the two now agree; delete it, and replace it with
  `test_multi_refusal_programs_agree` over a small corpus of two- and
  three-refusal programs (earlier-line link vs later-line body; two
  components each refused; a module fn plus a component; the `_oneline`
  variant of each, where every line is 1 and the reference's tie-break is
  pipeline order, which the gate's `seq` must reproduce).
- `admit_all` vs the reference's `RevlErrors` carrier, compared as sorted
  `(line, tag)` lists, on the same corpus, restricted to the recovery
  granularity in step 2 (documented in the test; the full statement-level
  list is out of scope).
- Every existing single-refusal test and fuzzer unchanged and green.
- Tick 419c; 419 does not tick until 186 closes.

### Slice B: `admit_ambient` and the running-manifest oracle

**Files:** `selfhost/lower.rvl` (`RunningM`, `running_manifest`,
`admit_ambient`, the `Handoff` field on `CompD`, the union `link_g2_g3`);
`tests/test_selfhost_ambient.py` (new); `tests/test_selfhost_lower.py`
(`_ref_ambient`, two classifier arms); the two gate crates regenerated;
`docs/v2.0-roadmap.md` (186); issue #86 closed on landing.

Four sub-steps, each landable alone behind the oracle:

- **B1, the manifest and the union link.** `running_manifest`, running
  services in scope, `link_g2_g3` over running plus candidate entries. Corpus:
  the union-composition cases. Exit: the hand-written union cases agree and
  the union generator shows both verdicts across seeds.
- **B2, service replacement.** `_service_compatible` ported (the drift matrix
  in §3.1, both regimes), `_service_touchers` over the running entries and
  `provSvc`, the `DRIFT` tag with the reference's message verbatim (`service
  \`X\` differs from the running manifest in a way that breaks \`A\`, \`B\`:
  <reason>`). Exit: the drift generator agrees on every cell, both regimes,
  both verdicts present.
- **B3, hand-off state.** `CompD.hoff` becomes `{ key, ty }`; the covariant
  `compatible(accepted, exported)` check after the component loop; the
  reference's message verbatim under the `HANDOFF` tag. Exit: the hand-off
  generator agrees; `examples/handoff_cache.rvl` against itself (same shape,
  `replacing=["Cache"]`) admits on both sides.
- **B4, the ratchet.** The per-generator both-verdicts assertion and the
  three mutation tests (§3.1). Exit: `pytest tests/test_selfhost_ambient.py`
  green, and each mutation test proves a one-field change of the running
  source flips both sides.

**Not in B:** a JSON-manifest adapter (`admit_manifest(src, ir_json)` over
`stdlib/value.rvl`) for the shipped gate. `crates/revl-gate` admits nothing by
design today ("No input produces an admission, by construction"), so there is
no consumer waiting on it; file it as a follow-on when one appears.

### Slice C: the whole-Prog export for G5/G8

**Files:** `formal/harness/diff_corpus.py` (`PG`/`EX`/`FN` rows,
`reference_from_tsv` T5/U5/S8, `prog_coverage`, three compare-loop entries,
`Verdicts` fields); `formal/harness/Oracle.lean` (parsers, `progOf`,
`bodyOfHeads`, the three deciders and bridges, the three verdict loops,
`#print axioms` entries); `formal/RevL/Theorems/G5_ClassifiedTeardownPure.lean`
and `G8_ClassifiedBoundary.lean` (`g5_row_not_vacuous`,
`g8_row_not_vacuous`); `formal/CheckAxioms.lean`;
`formal/scripts/{run_gate.sh,nonvacuity.tsv}`; `formal/STATUS.md`; the two
new fixtures under `examples/` and `examples/rejections/`; issue #276 closed
on landing.

Sub-steps:

- **C1, the export.** `PG`/`EX`/`FN` rows; `Oracle.lean` parses them into a
  `Prog` and ignores them (the #268 pattern: land the export, then the rows).
  Exit: `sh formal/scripts/run_gate.sh` exit 0, verdict counts unchanged,
  `corpus.tsv` carries the rows for all 25 extern-bearing files.
- **C2, the deciders and bridges.** `bodyOfHeads` + `bodyNames_bodyOfHeads`,
  `inverseOK_iff_registrations`, `teardownPureB_iff`, `mem_stmtSurface_iff`;
  registered in the oracle's `#print axioms` block and `run_gate.sh`. Exit:
  the oracle-side axioms gate clean (no sorryAx, no project axioms).
- **C3, the rows.** T5/U5/S8 on both sides, the `n/a` rule, the two fixtures,
  `prog_coverage`. Exit: every T5/U5/S8 verdict compared and agreeing;
  `prog_coverage` reports at least one caught violation per row kind; the
  fixture count in `STATUS.md`'s census updated.
- **C4, the theorems and the registry.** `g5_row_not_vacuous`,
  `g8_row_not_vacuous`; `CheckAxioms.lean`, `nonvacuity.tsv`, `run_gate.sh`
  (all three name the same set, `nonvacuity_gate.py` enforces it);
  `STATUS.md` G5/G8 rows lose their "Gap: no oracle row". Exit:
  `run_gate.sh` exit 0 end to end; #276's three exit bullets met.

### Slice D (optional): ambient composition in Lean

`AM` rows exported from an ambient-pair corpus (the running side's manifest
projection), `Oracle.lean` decides `linkVerdict (ambient ++ new)` per pair and
prints a `VA` row; `diff_corpus.py` computes the reference verdict by calling
`compile_source(..., manifest=...)`. Bridges already exist (`linkOKB_iff`).
Do this only after B lands, and only if the projection helper from §3.3 fell
out of B naturally.

---

## 5. Expected first divergences

Write these down now so the oracle's first red is read as a finding, not as a
port bug to paper over.

1. **A running `boot` component is invisible to the BOOT count.** `_link`
   builds ambient entries without `boot` (11563-11576) while the manifest
   entry it came from carries `boot: True` (11600), and the comment at 11611
   claims the swap case is caught. A candidate `boot` component admitted
   against a running one will pass the reference and (if the gate copies
   `boot` into `RunComp`, which it should) refuse on the gate. Fix the
   reference, add the case to `tests/test_manifest.py` or the boot suite,
   keep the gate's behaviour. Verify with the oracle before touching anything.
2. **Ambient-line ordering.** An ambient-caused refusal in the reference
   carries a real line only when the candidate's declaration is the second
   party; a candidate-vs-running conflict where the reference names the
   running entry would carry line 1. Slice A's anchor table must record the
   ambient case explicitly.
3. **Fuel.** If any corpus file has a fn chain longer than `len(fns)`, which
   cannot happen for distinct names but can look like it under module privacy
   mangling (`_apply_module_privacy`), an S8 mismatch will show the reference
   reaching further than the Lean fold. The `PG` row makes the number visible
   in the TSV.

## 6. Guards and landing rules

- Both gate-crate regeneration scripts on every `lower.rvl` change; the
  item-429 line-coverage and construct gates re-run on any self-host change
  and must not lose ground (a new `admit_ambient` path adds statements the
  single-source corpus never executes, so the ambient corpus is what keeps
  the line gate honest; land the tests in the same PR as the code).
- No `sorry`, no new axioms, `run_gate.sh` exit 0, for every formal step.
- Do not fix a pre-existing red found in passing; report it.
- Roadmap: mark 186 in progress at slice A start, tick 419c at slice A land,
  tick 186 at slice B land; #276 closes at slice C land.

## 7. Exit tests, collected

| Slice | Green means |
|---|---|
| A | `test_multi_refusal_programs_agree` (incl. one-line variants) green; the 419c pin deleted; all existing self-host oracle tests green; both crate `--check`s clean |
| B | `tests/test_selfhost_ambient.py` green: hand-written union/drift/hand-off pairs agree, three generators agree with both verdicts present, three mutation tests flip both sides; both crate `--check`s clean |
| C | `sh formal/scripts/run_gate.sh` exit 0 with T5/U5/S8 compared and agreeing, `prog_coverage` reporting caught violations for each, oracle axioms gate clean, `nonvacuity_gate.py` clean, `STATUS.md` G5/G8 rows updated |
| D | a `VA` row per ambient pair agreeing with `compile_source(..., manifest=...)` |
