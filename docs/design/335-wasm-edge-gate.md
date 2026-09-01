# 335: edge / browser / serverless admission (the wasm gate)

Design note for roadmap item 335 (`docs/v2.0-roadmap.md:3981`), part of the
embeddable-compiler arc (items 332-338, arc header at
`docs/v2.0-roadmap.md:3935`). The ask: compile the admission gate itself to
wasm so revl admission runs where there is no Python and no native toolchain,
only a wasm runtime: a browser playground or IDE that type-checks and admits
revl with no server round-trip, an edge worker that admits agent code
sub-millisecond with no cold-start interpreter, a CDN node that refuses to
serve an un-admitted component. The item's exit test: the wasm-compiled
`admit` runs in a browser / wasmtime host and returns the reference verdict
on the corpus.

This is design-first. It changes no compiler code; it records the realistic
cut of what runs in wasm (and what is honestly deferred), the ABI a JS or
wasm host calls, the fail-closed contract when there is no reference compiler
nearby to cross-check against, the authority boundary of an edge gate, the
use cases the cut actually supports, a staged plan, and exit tests an
implementation agent can pick up.

## What 335 packages: layer 1 of the 332 gate, nothing more

Item 332 (`docs/design/332-embeddable-gate-api.md`, py implementation landed
at `src/revl/gate.py`) split the gate into two layers on the purity line:

- **Layer 1, the verdict surface**: `admit(source) -> Verdict`,
  `admit_into(source, manifest)`, `compile_to(source, tier)`,
  `gate_version()`. Pure functions of their arguments: no disk, no clock, no
  live state. Strings in, structured strings out, no host object graph on the
  boundary (`gate.py:91`, the `Verdict` shape; the two ABI rules in the 332
  design). 332 said in so many words: "335 (wasm gate) compiles layer 1 to
  wasm ... the wasm-component's export list is layer 1 verbatim."
- **Layer 2, the session surface**: the stateful `Gate` facade (load, admit,
  call, commit, abort) over `Session`, address-space-bound, py only.

335 packages **layer 1 only**. Layer 2 does not go to the edge in this item:
its machinery (the 245 owner, the WAL, the 246 approval choke point, cordis
booting a composition) is a runtime, and an edge gate is a checker, not a
runtime (the authority section below). This is not a limitation discovered
late; it is the same purity line 332 drew, and it is why the wasm gate is
coherent at all: a verdict is a pure function, and pure functions are what
wasm packages well.

Item 289 (`src/revl/least_authority.py`) is the second pillar. For a wasm
component the whole least-authority chain

    host imports  subset-of  declared caps  subset-of  policy-allowed

is statically decidable: the imports are literally the emitted module's
import section, the declared caps are the G8 boundary reach, the policy is
the item-33 allow-list (`least_authority.py:1-26`). The check itself is pure
set logic over strings (`component_breaches`, `least_authority.py:137`). So
the admission decision that matters most at the edge, "does this artifact's
authority fit my policy", is not just computable in wasm; it is computable
from data the artifact itself carries. 289 is why a gate at the edge can be
sound about authority without recompiling anything.

## Ground truth: the assets and the walls (measured, not assumed)

Four facts fix the design space. Each is checkable in the tree today.

**1. The self-host `admit` runs natively on rust, and does not compile
through revl's own wasm backend.** `selfhost/compile.rvl` exports
`admit(source)` (`:75`) driving the fully native lexer / parser / checker /
lowering-admission chain; it runs natively on rust end to end
(`tools/bench_selfhost_rust.py`, items 283/284, `docs/bench-selfhost.md`),
and 332 stage 3 mechanizes it as the committed `revl-gate` crate. But the
first-party wasm tier cannot lower it: `Map` has **no value representation**
on that tier (`docs/wasm-capabilities.md:36`, a hard `EmitError`, never a
silent narrowing), and the pipeline's checker and lowering stages use `Map`
throughout (`selfhost/checker.rvl`: 20 sites, `selfhost/lower.rvl`: 30
sites), before counting `repeat`, `Float`, and the other tier limits the
capabilities table names. Nothing in `tests/` or `tools/` compiles the
self-host front end to the wasm tier today; `selfhost/emit_wasm.rvl` is the
self-hosted wasm **emitter** (it emits WAT; `tests/test_selfhost_emit_wasm.py`
runs it on py), which is a different artifact from the front end running
**on** wasm. The item's own dependency line ("native-run coverage on the
wasm tier (frontier)") names exactly this wall.

**2. rustc is a production wasm compiler we do not have to build.** The
332-designed `revl-gate` crate is committed generated rust source that must
build with no Python on the machine. `rustc --target wasm32-wasip2` (or
wasm32-unknown-unknown plus the component adapter) turns that same crate
into a wasm module with an industrial toolchain, mature linear-memory
codegen, and the component-model tooling (`cargo component`, `jco`)
maintained by the ecosystem. The one dependency to prove: cordis-rs rides in
the crate graph for the value layer (`backends/rust/emit.py:46`; the 332
dependency accounting), and it must build on a wasm32 target. It is a value
and persistent-collections library with no mandatory OS surface in the pure
gate path, so this is expected to work, but it is the go/no-go spike, not an
assumption (slice 0 below).

**3. A standard component-model emission path exists in-tree.**
`revl export wit` (`src/revl/export_wit.py`) emits the WIT interface of a
revl service, and `backends/wasm/canonical.py` (item 41 slice 3) emits
canonical-ABI WASI P2 exports with the WIT embedded, loadable by wasmtime,
jco, Spin, wasmCloud. These govern revl-EMITTED components. The gate
component's own world (below) is hand-authored, the same way the crate's
`lib.rs` shim is hand-written over the generated pipeline; when the dogfood
horizon closes (cut C below) the gate becomes a revl component whose WIT the
existing exporter produces, and the two lanes converge.

**4. The browser gate already exists, expensively.** The playground ships
the full reference wheel into the browser over Pyodide
(`site/playground.html:102`, `site/vendor/revl-2.0.0-py3-none-any.whl`),
which is what the item calls "the first step in this direction". It proves
the demand and the purity (the whole compiler runs client-side because it is
pure functions), and it fixes the cost baseline the wasm gate must beat: a
multi-megabyte runtime plus interpreter start, fine for a playground tab,
disqualifying for an edge worker or serverless cold start. Pyodide is the
browser fallback with FULL reference coverage; it is not the edge story.

## 1. What runs in wasm: the recommended cut

Three candidate cuts, judged against the facts above.

### Cut A (recommended, the headline): the native `admit` via the rust lane

Cross-compile the 332 `revl-gate` crate (the committed, generated self-host
pipeline plus its thin shim) to a wasm component. What this buys:

- **Source-level admission at the edge.** `admit(source)` returns the native
  gate's verdict: the full lex / parse / check / lowering-admission chain,
  the one revl promise (a program the checker refuses cannot be compiled)
  enforced in the artifact itself.
- **The 332 staging carries over verbatim.** v1 exports `admit` only,
  because `admit` is the half that runs natively today; `compile_to` returns
  the unsupported control verdict until the emitter helper externs gain
  `@rs` bodies (332 stage 4), and `admit_into` (manifest-spanning admission)
  is refused as unsupported exactly as the rust crate refuses it, fail
  closed, a control verdict naming the gap.
- **The frontier is the self-host frontier, and the artifact says so.** The
  covered surface is the conformance `revl` column (`docs/conformance.md`:
  23 ok / 36 lim today), not the full reference language. `gate_version()`
  pins `frontier` (the 332 versioning surface) so a host can detect what
  this gate covers before trusting its verdicts. Out-of-surface constructs
  refuse loudly; the fail-closed rule from 332 is inherited unchanged.

What cut A cannot do: cover the full reference surface (that is the
self-host coverage frontier's pace, not this item's), or run anything (no
layer 2). The wasm build adds no reach the rust crate lacks; it adds
portability.

### Cut B (also in scope, small and load-bearing): the artifact-level verdict

A second export, `admit-artifact(ir, policy)`, computing the IR-level checks
that need no front end: the 289 least-authority chain (import caps
subset-of declared reach subset-of policy allow), the holes refusal (an
admitted draft with an open obligation may never run, `gate.py:160`
docstring), and the boundary-policy evaluation. Inputs are the compiled IR
(JSON, already the interchange format the self-host pipeline speaks) and the
policy; when the caller also passes the component's wasm import section (or
the wasm bytes to extract it from, the `wasm_import_capabilities` logic,
`least_authority.py:55`), leg 1 of the chain is verified against the
artifact itself.

Why this cut earns its place: the edge use cases mostly do not hold source.
A browser deciding whether to instantiate a fetched agent component, or a
CDN node deciding whether to serve one, holds the ARTIFACT. Recompiling
source at that point is the wrong question; "does this artifact's authority
fit my policy" is the right one, and 289 made it statically decidable from
data the artifact carries.

The trust accounting, stated honestly because an IR is caller-supplied data:

- An IR that **under-declares** caps (to sneak authority) is caught exactly
  where it matters: the wasm module's import section would exceed the
  declared set, leg 1 refuses, and leg 1 is checked against the artifact,
  not the claim.
- An IR that **over-declares** is refused by leg 2 if it exceeds policy and
  harms only itself otherwise.
- What the artifact verdict does NOT re-establish: the semantic guarantees
  (G1-G8, classification, undo-completeness) verified at compile time.
  Those were the compiling gate's judgment; an edge host that will not trust
  the compile provenance needs cut A plus the source, or artifact
  attestation (signing), which is adjacent to the distribution model and
  deferred by name, not smuggled in.

### Cut C (the dogfood horizon, deferred): the gate through revl's own wasm backend

The self-host pipeline emitted by `backends/wasm/emit.py` and served over
the in-tree canonical ABI would be the fully first-party story: revl
compiling revl to revl's first-party runtime. It is blocked today by named
tier limits, primarily `Map` (no representation; 50 use sites across
checker.rvl and lower.rvl), plus `repeat` and the builtin subset
(`docs/wasm-capabilities.md`). Each gap is a wasm-tier roadmap item on its
own merits, not a 335 deliverable; 335 must not become "grow the wasm value
model" by the back door. When the tier closes those gaps, cut C replaces the
rust lane's toolchain with our own and the gate's WIT with
`revl export wit` output; the ABI below is designed so that swap is
invisible to hosts.

### Rejected as the deliverable

- **Pyodide as the edge gate**: exists, full-coverage, and stays the
  playground's engine; runtime size and cold start disqualify it beyond the
  browser tab.
- **The ts emission as "the edge gate"**: `selfhost/emit_ts.rvl` emits the
  pipeline to typescript, and edge workers run JS natively, so a js-native
  gate falls out as a corollary someday. But it is not wasm (no single
  attested artifact, no wasmtime/serverless reach, a different supply chain
  per host), and the item is the wasm packaging. Noted, not built.

**Recommendation: one wasm component from the rust lane, exporting cut A
(`admit`) plus cut B (`admit-artifact`), staged; cut C named as the horizon.**

## 2. The ABI: a WIT world with an empty import list

The gate ships as a WASI P2 component whose world is layer 1 verbatim, in
the 332 ABI discipline (strings and flat structs on the boundary, never a
host object graph):

```wit
package revl:gate@1.0.0;

world gate {
  record verdict {
    admitted: bool,
    code: option<string>,
    message: option<string>,
  }

  /// Frontend admission over source. Frontier-scoped (self-host coverage);
  /// out-of-surface constructs refuse with a control verdict, fail closed.
  export admit: func(source: string) -> verdict;

  /// Artifact admission: the 289 least-authority chain + holes + boundary
  /// policy over a compiled IR (json) and a policy (json). `imports` is the
  /// artifact's own import-section capability list when the caller holds
  /// the wasm bytes; empty means "leg 1 not checkable here" and the verdict
  /// says which legs it judged.
  export admit-artifact: func(ir: string, policy: string,
                              imports: list<string>) -> verdict;

  /// {"api": ..., "language": ..., "frontier": ...} as json, the 332
  /// versioning surface, so a host can detect coverage and skew.
  export gate-version: func() -> string;
}
```

Decisions, each load-bearing:

- **The import list is empty.** The gate world imports nothing: no
  wasi:clocks, no wasi:filesystem, no wasi:random, no host functions. This
  is not minimalism for its own sake; it is the fail-closed mechanism
  (section 3) and the gate passing its own least-authority bar: item 289's
  chain applied reflexively, an artifact whose import section proves it can
  consult nothing but its arguments. A build that grows an import fails CI
  structurally (an exit test below), because an import is a channel through
  which a verdict could stop being a pure function.
- **`verdict` is the 332 shape.** `admitted` and `code` are API (codes
  append-only), `message` is the native gate's diagnostic verbatim at this
  version (`gate.py:91` docstring; the shared `"<TAG>|<message>"` parser,
  `Verdict.from_native`, `gate.py:115`, is the same split the shim
  performs). A wasm host and a py host see the same three fields.
- **Component-model-first, with adapters, not a bespoke core ABI.** In
  wasmtime and other P2 hosts the component loads directly. In browsers and
  node, `jco transpile` produces the JS shim (the same loader lane
  `canonical.py` targets for revl-emitted components, so hosts learn one
  loading story). A hand-maintained core-module ABI (raw pointer/length
  exports) is explicitly not designed here: it is a second ABI to keep
  honest, and every named host speaks the component model or has a
  transpiler that does.
- **JSON where structure exceeds WIT ergonomics** (`gate-version`,
  `admit-artifact` inputs): the boundary stays strings, the 332 rule, and
  the py surface already fixed those JSON shapes.

The embedding sketch a JS host sees (host-language, illustrative):

```js
import { admit, admitArtifact, gateVersion } from "./revl-gate.js"; // jco output

const v = admit(source);            // { admitted, code, message }
if (!v.admitted) refuse(v);         // the refusal is the repair signal
```

## 3. Fail-closed at the edge: sound with no reference nearby

The 332 security clause is "never admit what the reference refuses". At the
edge there is no reference compiler to cross-check against, no CI in the
loop, no operator. The wasm gate stays sound by construction plus evidence
plus versioning, in that order:

**Deterministic by construction.** The empty import world means the verdict
is a total, deterministic function of the arguments: no clock to race, no
filesystem to vary, no environment to consult, no nondeterministic host
call. Two hosts running the same gate artifact on the same input get the
same verdict, provable from the artifact's import section. This is the
property that makes a conformance vector MEAN something: agreement measured
once holds everywhere.

**The conformance vector, versioned and release-gating.** A committed corpus
of `(input, expected {admitted, code, message})` triples, generated from the
reference gate (`revl.gate.admit`, which IS `compile_source` plus
`refuse_admission`) at the same sha the wasm artifact is built from,
spanning: the admitted corpus, every refusal family the differential corpus
holds (`tests/test_selfhost_compile.py` discipline), the out-of-surface
control verdicts, and for `admit-artifact` the 289 breach fixtures
(`import>declared` and `declared>policy` legs). CI runs the wasm artifact
under wasmtime over the vector and requires byte-identical structured
verdicts. This is the 332 release-gate discipline extended one tier: the
package does not ship on a red or skipped vector, and the skip-with-a-reason
rule applies when wasmtime is absent (never a green that ran nothing).

**Fail closed at the frontier, with the asymmetry named.** The two
divergence directions are not symmetric (332 said it first): a wasm gate
that refuses what the reference admits is an inconvenience; a wasm gate that
ADMITS what the reference refuses is the defect class this arc exists to
prevent. So: out-of-surface constructs refuse with a control verdict; no
vector case may ever land where the wasm gate admits and the reference
refuses (release-blocking, not nightly); and any reference-side admission
change lands in the self-host gate in the same wave or the wasm release is
held, the same rule the crate carries.

**Versioned against the reference, skew detectable.** The edge failure mode
332 could not have (one gate, one process) and 337 will police at seams: a
STALE gate cached at an edge node keeps issuing verdicts from an old
language version. `gate-version` pins `{api, language, frontier}` into the
artifact; a host that requires currency compares before trusting, and the
artifact's own version surface is the mechanism, not an out-of-band
registry. What 335 owes 337 is exactly this: a serialized verdict and a
comparable frontier, both inherited from 332.

**Cannot be made permissive.** For a fixed input, nothing in the
environment can change the verdict: there is no config import, no feature
flag, no policy-widening channel other than the `policy` ARGUMENT itself.
And the policy argument is not a soundness hole, it is the caller's own
authority: a host that passes a wide policy to `admit-artifact` has widened
what IT will accept, which was always the host's right (the gate never
confines its host, the 332 contract). What the gate guarantees is that the
verdict faithfully reflects the inputs given, and that no input combination
makes `admit` accept a program the reference refuses.

## 4. What authority the edge gate has: a checker, not a runtime

The wasm gate ADMITS or refuses. It does not run host effects, hold
sessions, prompt approvers, or enforce anything. The boundary, stated so no
one reads more into a wasm binary than is there:

- **Decision here, enforcement at the host's instantiation point.** The
  gate returns a verdict; the browser's loader, the worker's dispatcher, or
  the CDN's serving path must be the code that refuses to
  instantiate / execute / serve on a refusal. A gate whose verdict nobody
  consults gates nothing; the JS harness (slice 4) exists to make the
  consulting pattern copyable, not optional-looking.
- **289 gives the edge host a second, substrate-level enforcement for
  free.** For an ADMITTED wasm component, the host instantiates it with an
  import object shaped by the policy: an ungranted reach is a missing
  import, refused by the wasm substrate itself, exactly the by-construction
  invariant 289 records (`least_authority.py:16`, "an ungranted reach is a
  missing import refused by the substrate itself"). The edge gate decides;
  the instantiation enforces; neither trusts the other's absence.
- **Runtime confinement is 411's lane, not this one.** The sandbox ladder
  (wasm cell / container / microVM, `docs/design/411-sandbox-placement.md`,
  the wasm-cell rung at `:715`) is where a component RUNS under an enforced
  envelope. 335 and 411 compose (an edge that both admits and then runs the
  admitted component in a wasm cell is the full story) but do not overlap:
  335 ships no runtime, 411 ships no verdict.
- **Layer 2 stays where a runtime lives.** Admit-at-edge, run-elsewhere is
  the expected shape for anything with host effects: the edge verdict
  travels with the request, and the 245/246 machinery governs the run where
  a session owner exists. The serialized `Verdict` (fixed by 332 for 337's
  seams) is what travels.

## 5. Use cases, grounded in what the cut supports

**A browser refusing to load an un-admitted agent component.** The page
fetches an agent component (wasm bytes plus its IR/manifest). Before
instantiating, it calls `admit-artifact(ir, policy, imports-of(bytes))`; a
refusal never reaches `WebAssembly.instantiate`, and an admission is
instantiated with the policy-shaped import object (the 289 double
enforcement). Cut B carries this today-shaped case end to end. If the page
holds SOURCE (a playground, an IDE plugin), cut A's `admit` gives the
type-check-and-admit loop with no server round-trip, frontier-scoped; the
Pyodide playground remains the full-coverage fallback and the two can
coexist in one page (fast path native, slow path reference).

**A serverless function gating a tool call.** An agent framework's worker
receives a proposed per-turn tool (source). It calls `admit(source)`
in-process in the wasm runtime it already runs on: no cold-start Python, no
subprocess, no network hop to a gate service (the item-144 trade-off
inverted, as 332 frames it). Admitted turns are forwarded to where a
runtime with layer 2 runs them; refused turns return the verdict as the
repair signal. The frontier caveat is real and stated: a turn using
surface beyond the self-host frontier refuses as out-of-surface, so a
harness choosing this path constrains its tool-authoring surface to the
frontier or falls back to a reference gate for the refusal-by-frontier
cases. Fail closed either way; never waved through.

**CDN-edge admission.** A registry edge or CDN node holds artifacts
(component + IR + policy of the distribution). `admit-artifact` before
serving means an artifact whose authority breaches the chain is refused at
the edge, before any consumer downloads it: "install is admission"
(`docs/distribution-model.md`) generalized one hop further to "serving is
admission". This is cut B plus the versioning surface (the node's gate
version is auditable via `gate-version`).

What none of these get from 335: running effects at the edge (411),
manifest-spanning `admit_into` (native gap, refused as unsupported),
`compile_to` at the edge (staged behind the crate's stage 4), full reference
coverage (the frontier's pace).

## 6. Staged plan

Each slice lands independently; every existing golden stays byte-identical
throughout (the additivity discipline). Slices 1+ assume 332 stage 3 (the
committed crate) has landed; slice 0 can spike against the bench lane
(`tools/bench_selfhost_rust.py` assembles the same pipeline) before it.

- **Slice 0 (the go/no-go spike).** Build the gate pipeline for
  wasm32-wasip2; run `admit` under wasmtime on a smoke corpus. The one
  known risk is cordis-rs on a wasm32 target; if it does not build, the
  recorded fallback is the value-layer-subset vendoring decision 332 left
  to item 336, pulled earlier by necessity and taken in the open (upstream
  fork+PR discipline applies: prefer fixing cordis-rs's wasm32 build
  upstream over vendoring). Output: a spike note, a yes/no, and measured
  artifact size + verdict latency.
- **Slice 1 (the WIT world + the artifact).** The hand-authored
  `revl:gate` world; the shim exporting `admit` + `gate-version`;
  `admit-artifact` present but returning the unsupported control verdict;
  the build tool (`tools/build_gate_wasm.py`, sibling of
  `build_gate_crate.py`); the artifact committed with the crate's drift
  discipline (CI rebuilds from the same sha, fails on byte difference,
  skip-with-reason when the toolchain is absent). Structural CI check: the
  artifact's import section is empty.
- **Slice 2 (the layer-1 verdict in wasm, vector-pinned).** The conformance
  vector generated from `revl.gate.admit` at the build sha; CI runs the
  wasm artifact under wasmtime over it, byte-identical structured verdicts
  required, release-gating. The out-of-surface control-verdict families are
  vector rows, not exemptions.
- **Slice 3 (`admit-artifact`).** The 289 chain + holes + policy
  evaluation in the gate artifact. Preferred engine: written in revl
  (a `selfhost/admit_artifact.rvl` over the IR json, compiled into the
  same crate via the native rust emitter), so the reference
  (`least_authority.py`) and the wasm build are two implementations of one
  differential-tested contract, the discipline every selfhost stage
  already follows; a hand-rust port inside the shim is the fallback if the
  frontier blocks the revl spelling, held to the same vector. Vector rows:
  the 289 breach fixtures, both legs, plus holes and policy refusals.
- **Slice 4 (the JS harness + the demo).** `jco transpile` packaging; a
  browser page that admits source and refuses/instantiates an artifact
  (the double-enforcement pattern shown, not just the call); a wasmtime
  serverless example; measured numbers (artifact size, cold instantiation,
  per-verdict latency) recorded the bench way. Playground integration
  (fast-path native admit beside Pyodide) optional, behind its own item.
- **Deferred and named, not implied:** cut C (the gate through the
  first-party wasm tier; gated on the tier's `Map`/builtin frontier);
  `compile_to` in wasm (follows the crate's stage 4); native `admit_into`;
  npm packaging of the jco output (item 338's ecosystem step); artifact
  attestation/signing (distribution-model adjacency).

## 7. Exit tests

- **The item's own exit.** The wasm-compiled `admit`, run under wasmtime
  and in a browser (jco), returns the reference verdict on the corpus:
  byte-identical `{admitted, code, message}` against `revl.gate.admit` for
  every vector row, at the pinned sha.
- **A refused component is refused at the edge.** In the slice-4 harness: a
  component whose source the gate refuses is never instantiated; a
  component whose imports exceed its declared caps, or whose declared caps
  exceed the policy, is refused by `admit-artifact` AND fails to
  instantiate under the policy-shaped import object (both enforcement
  layers demonstrated, independently).
- **The wasm gate cannot be made permissive.** Structurally: the artifact's
  import section is empty (CI-checked on every build; a build that grows an
  import fails). Behaviorally: the same input yields the same verdict
  across hosts and runs (wasmtime + browser in CI where available); no
  environment, flag, or host-provided function exists that alters a
  verdict; and the release-blocking direction (wasm admits, reference
  refuses) has zero vector rows, ever.
- **Fail closed at the frontier.** Every out-of-surface construct family
  refuses with a control verdict; the frontier pin in `gate-version`
  matches the conformance `revl`-column pin for the build sha.
- **Drift and honesty.** Rebuilding the artifact from the same sha is
  byte-identical (CI drift gate); the vector job skips with a stated
  reason, never a hollow green, where wasmtime or the wasm toolchain is
  absent.
- **Additivity.** The full suite, every backend golden, and
  `revl run`/`test`/`mcp` behavior are byte-identical with the wasm
  packaging present; nothing in `src/revl` changes behavior for a user who
  never loads the artifact.
- **`test_doc_examples` stays green**: every proposed-surface block in this
  note is host-language, WIT, or sketch-marked and must not compile until
  the feature lands.

## The honest hard part (consolidated)

Four costs, taken in the open. First, the edge gate's coverage is the
self-host frontier, not the reference language: 23 ok / 36 lim on the
conformance `revl` column today, so the browser-playground use case is
frontier-scoped fast-path plus Pyodide fallback until the frontier moves,
and a serverless harness must either author within the frontier or accept
refusal-by-frontier as a routed fallback, never a wave-through. Second, the
first wasm gate is built by rustc, not by revl: the fully first-party story
(cut C) waits on the wasm tier's own value-model frontier (`Map` above
all), and pretending otherwise would have made 335 a covert wasm-tier
value-model item. Third, soundness at the edge is construction plus
evidence plus versioning, not proof: the empty import world makes verdicts
deterministic, the vector makes agreement measured, the version surface
makes skew detectable, and the false-admit direction remains the defect
class a stage bug could open, which is why the vector is a release blocker.
Fourth, `admit-artifact` re-establishes authority facts, not semantic
guarantees: leg 1 is checked against the artifact itself, but G1-G8 were
the compiling gate's judgment, and an edge that will not trust compile
provenance needs the source and cut A, or attestation machinery this item
deliberately does not start.
