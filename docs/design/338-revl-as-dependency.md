# 338: revl as a dependency for other agent infrastructure (the ecosystem step)

Design note for roadmap item 338 (`docs/v2.0-roadmap.md:4322`), the ecosystem
step of the embeddable-compiler arc (items 332-338, foundation
`docs/design/332-embeddable-gate-api.md`). The ask: publish the gate/runtime as a
LIBRARY other agent infrastructure pulls in as a dependency, so an MCP server, a
CI system, or an agent framework `cargo add revl` / `pip install revl` / `npm i
@revl/gate` and calls the admission gate directly, rather than orchestrating the
`revl` CLI as a subprocess. revl as the admission gate for agent-generated code
that other tools build on. The exit is a real EXTERNAL consumer example: a
third-party tool that imports the package and admits code.

This is design-first. It builds on 332 (the embeddable gate API + packaging
contract) and 333 (the in-process gate an agent framework embeds). It records:
the per-tier package surface (what a dependency actually pulls in), the stable
public API and what "stable" means, versioning/compat keyed on `gate_version()`,
the SECURITY CONTRACT a host embeds against (the load-bearing part, and where the
adversarial review finds the CRITICAL), an honest split of what exists today (py)
versus what is gated on the rust/wasm packaging frontier, a real-consumer example
shape, and exit tests an implementation agent can pick up.

## Where this sits in the arc: 338 publishes, it does not build

332 designed and (on py) built the two-layer gate: layer 1 the pure verdict
surface (`admit`/`admit_into`/`compile_to`/`gate_version`), layer 2 the stateful
`Gate` session facade over `Session`. 333 embedded layer 1/2 in an agent loop and
proved the in-process verdict equals the CLI's, measured. 334 added the `propose`
verb for self-extension. 338 is the DISTRIBUTION and CONTRACT step: it takes what
332 packages and 333 proved and makes it something a stranger's build system
depends on, with a public surface stable enough to pin, a versioning story that
lets revl evolve without churning consumers, and a security contract precise
enough that a downstream host does not build an unsafe system on a
misunderstanding of what "admitted" means. 338 adds no new engine and no new
verb; it is packaging, a declared surface, a contract, and one real consumer.

The dependency chain is stated plainly because 338's honesty turns on it:

- 332 py: LANDED (`src/revl/gate.py`, the `revl.gate` module in the wheel).
- 332 rust crate / npm / wasm: DEFERRED (332 "Deferred: rust revl-gate crate,
  npm/wasm"), and item 336 confirmed the rust half of that frontier is not built.
- So 338's "cargo add revl" exit is FRONTIER-GATED; 338's "pip install revl and
  import revl.gate" exit is deliverable today. The slice split below is drawn on
  exactly this line and does not pretend otherwise.

## 1. The per-tier package surface: what a dependency pulls in

### py: the wheel already IS the dependency; what 338 ships is the fence

`pip install revl` installs one dependency-free wheel (`pyproject.toml`: no
`[project] dependencies`; `requires-python >= 3.11`; `backends/` and `stdlib/`
force-included, `[tool.hatch.build.targets.wheel.force-include]`). That wheel
already carries the full reference gate, the run half, all six backends, the
stdlib, the MCP server, and the CLI. So an external consumer can depend on revl
and `from revl.gate import admit, admit_into, compile_to, gate_version, Gate`
TODAY; 333's harness (`bench/inprocess_gate_harness.py`) is a first-party proof
that the embed works.

The honest consequence, and 338's real py deliverable: the wheel is NOT a minimal
gate library. A consumer that depends on revl pulls in the entire
compiler+runtime+CLI surface, and every module under `revl.*` is IMPORTABLE even
though only `revl.gate` is PROMISED. `revl.gate.__all__` (`src/revl/gate.py:50`)
names the promised surface (`Verdict`, `Emit`, `admit`, `admit_into`,
`compile_to`, `gate_version`, `Gate`, `GateError`, `GateRefused`, `AdmitResult`,
`ProposeResult`, `Handle`, `recover`); everything in `revl.compiler`,
`revl.mcp.*`, `revl.run` is INTERNAL and unpromised, the exact undeclared reach
332 named as the wound it healed for the host side. 338's py deliverables are
therefore the FENCE and its proof, not new bytes:

- a documented public-surface contract: `revl.gate` is the dependency surface;
  nothing else is promised, and a consumer reaching past it is on its own across
  a patch bump;
- an import-surface test that pins `revl.gate.__all__` so the promised names
  cannot silently drift (this is 332's stage-1 import-surface test, elevated to
  the public compat gate 338 owns);
- packaging metadata that says so (a `Development Status` and an API-stability
  note in the project metadata / docs), so a consumer knows what it is pinning.

### rust: `revl-gate` crate (frontier-gated, 332 stage 3)

`cargo add revl-gate` is 338's headline ecosystem example, and it is gated on 332
shipping the crate. From 332's rust packaging section: `tools/build_gate_crate.py`
compiles `selfhost/compile.rvl` and its `use` closure to a COMMITTED rust library
crate plus a hand-written `lib.rs` shim exporting layer 1 (`admit`,
`gate_version`; `compile_to` staged behind `@rs` emitter externs; `admit_into`
refused as unsupported, fail closed). The crate must build with no Python on the
machine, and its release gate is the differential corpus. 338 adds nothing to the
crate's construction; it adds the PUBLISHING act (a versioned artifact on the
registry the ecosystem consumes) and the external-consumer example. Until 332
stage 3 lands, this half of 338 is designed and blocked, stated as such.

The crate's surface is a subset of py's by construction: layer 1 only (layer 2,
the stateful `Gate`, is address-space + event-loop bound and py-only per 332), and
`admit` on the SELF-HOST frontier, not the full reference language. A rust
consumer therefore embeds a gate whose covered surface is `gate_version().frontier
= selfhost:<corpus>`, not `reference-full:<language>`. This asymmetry is not a
packaging detail; it is the crux of the security contract and the adversarial
CRITICAL below.

### npm / wasm-component: reserved, via 335

`npm i` of the jco-transpiled wasm gate (335) is the browser/edge/serverless
dependency form. 338 owes it only that the published API is expressible there,
which 332/335 already guarantee by construction (the boundary is strings and JSON,
the WIT world is layer 1 verbatim, `docs/design/335`). Shipping it is 335's lane
plus a publish step; 338 names it and defers it.

## 2. The stable public API, and what "stable" means

The dependency surface is `revl.gate` (py) / the `revl-gate` crate's exported
functions (rust) / the `revl:gate` WIT world (wasm). Its stability is split
exactly as 332 fixed it, and 338's job is to PUBLISH that split as a promise a
consumer can pin against:

- **`gate_version().api`** (`GATE_API_VERSION = "1.0.0"`, `src/revl/gate.py`):
  the semver of the gate SURFACE. Bumped by surface changes only, independent of
  the language/package version. A consumer pins a compatible `api` range.
- **`Verdict.admitted` and `Verdict.code`**: API. Codes are append-only; an
  existing code never changes meaning. A consumer may branch on `code`
  programmatically (e.g. treat `G2`/`G3` link failures as "repair the interface",
  `G8` as "reject outright").
- **`Verdict.message`**: NOT API. It is the reference compiler's diagnostic
  verbatim at a given version (`gate.py:92` `Verdict`); it improves across
  versions. A consumer LOGS it (a human-usable repair signal) but does not parse
  it.
- **`gate_version().language`**: the revl language/package version the gate
  admits. It moves with the repo version (`pyproject.toml version = "2.0.0"`).
- **`gate_version().frontier`**: the identifier of the gate's COVERED surface
  (`reference-full:<language>` on py, `selfhost:<corpus>` on a native gate). This
  is the field that makes a heterogeneous fleet's verdicts comparable, and 338
  makes it a first-class part of the contract, not an advanced-user footnote.

The stability rule a consumer embeds against, stated once: **branch on
`api`+`code`, gate on `frontier`, log `message`, treat everything not in
`gate.__all__` as private.** The compat test (338 py deliverable) enforces the
first and last halves; `gate_version()` exposes the middle.

## 3. Versioning and compatibility

Three skews a dependency consumer will actually hit, and how the contract handles
each:

- **Surface skew.** revl adds a gate function. `api` minor-bumps; consumers on the
  old minor are unaffected (additive). A consumer pins `api ~= 1.0`.
- **Language skew.** revl adds a language feature, so a component admitted under
  `language = 2.3` might be re-admitted differently under `2.5`. A consumer that
  CACHES verdicts (a CI system storing "admitted" results, a registry) must key
  the cache on `gate_version()` (all three fields), so a language bump invalidates
  stale admissions rather than trusting them forever. 338 states this as a
  consumer obligation and the example demonstrates it.
- **Frontier skew (the dangerous one).** Two consumers admit the SAME source and
  get DIFFERENT verdicts because one embeds the py reference-full gate and the
  other the rust self-host-frontier crate. "revl admitted it" is therefore NOT a
  single fact across a polyglot fleet unless `frontier` is part of the claim. This
  is the seam-skew problem 337 polices at placement seams; 338's contract makes it
  a consumer's explicit responsibility: an admission is scoped to the `frontier`
  that produced it, and a consumer comparing verdicts across tiers MUST compare
  `frontier` first.

The compat gate: `api` and the `__all__` fence are CI-pinned (an import-surface
test); `code` is checked append-only across a release (the 332 catalog
discipline); `language`/`frontier` are data a consumer branches on, never silently
changed under it.

## 4. The security contract a host embeds against (the load-bearing part)

This is what a downstream tool actually needs 338 to state precisely, and it is
where the adversarial review finds the CRITICAL. The contract has four clauses.

**Clause 1: a REFUSAL is authoritative and fail-closed.** If the embedded gate
returns `admitted = false`, the reference would refuse too (the arc's
never-admit-what-reference-refuses clause, held by the 332/335 differential
release gate). A host may rely on a refusal absolutely: a component the gate
refuses is one the reference refuses, and the host must not run it. This is the
strong half of the contract and it is genuinely dependable.

**Clause 2: an ADMISSION is a COMPILE-TIME judgment, scoped to
`gate_version().frontier`, NOT a runtime confinement.** `admitted = true` means:
the source type-checks, has no open holes, its effects are classified, its
requires/provides link (G2/G3), it declares no smuggled host code beyond what the
profile allows (G8), and under the untrusted-author profile it reaches only
granted services. It does NOT mean the admitted code is confined as it RUNS: an
admitted component's granted `extern` host body is arbitrary host code the gate
SURFACED (G8), not neutered (`src/revl/gate.py:36`, "the gate cannot and does not
claim to confine its host; its guarantees govern the ADMITTED code"; 333 section
4, admit != safe to run unwitnessed). And the admission is scoped to the frontier
that produced it: a native-gate admission covers only the self-host surface.

**Clause 3: the RUNTIME half is a separate, named, py-only dependency.** The
reversible-execution guarantees (witnessed effects, session commit/abort,
approver seam, WAL recovery: items 243/244/245/246/322, the `propose` self-
extension loop of 334) live in layer 2, the `Gate` facade, which is py-only,
single-gate-per-process (`_ACTIVE_GATE`, `gate.py:412`), single-threaded, and
synchronous (it owns its event loop). A host that wants "admitted AND run
revertibly" adopts layer 2 explicitly and inherits those walls; a host that
`cargo add`s the crate or embeds the wasm gate gets ONLY layer 1 (a verdict), and
must run admitted code under its own runtime discipline (or 411 confinement).

**Clause 4: the gate does not confine ITS host.** A library cannot jail its own
process; the host holds full process authority with or without revl, and
embedding changes its position not at all (332's embedding contract). revl's
guarantees govern the ADMITTED code, and embedding must not WEAKEN them, which by
332's accounting it does not.

The one-line contract a host embeds against: **trust a refusal absolutely; treat
an admission as a frontier-scoped compile-time judgment, not a sandbox; adopt
layer 2 (py) if you need revertible execution; the gate never confines you.**

## 5. Exists today (py) vs the rust/wasm frontier (honest split)

**Deliverable today (py), the real slice-1 exit:**

- An EXTERNAL consumer (a small standalone project that is NOT part of this repo,
  depending on the published wheel) imports `revl.gate` and admits a batch of
  agent-authored components in-process, getting verdicts equal to the CLI's (333's
  proof, now from a genuinely external package rather than an in-tree bench).
- The public-surface fence: the `revl.gate.__all__` compat test, the documented
  "branch on api+code, gate on frontier, log message, rest is private" contract,
  and the packaging-metadata stability note.
- The security contract (section 4) written where a consumer will read it
  (consumer-facing docs, not only design notes), with 333's admit != run boundary
  stated in consumer words.
- The versioning obligations (cache-key on `gate_version()`; compare `frontier`
  across tiers) demonstrated in the example.

**Frontier-gated (rust/wasm), designed and blocked:**

- `cargo add revl-gate` as a real ecosystem example: gated on 332 stage 3 (the
  committed crate + `tools/build_gate_crate.py`). The crate offers layer-1 `admit`
  on the self-host frontier; NOT `admit_into` (no native manifest pipeline,
  `docs/design/333` dependency table), NOT layer 2 (py-only). So a rust CONSUMER
  can admit STANDALONE candidates today-once-the-crate-lands, but the realistic
  agent-loop shape (admit-against-a-running-composition) waits on native
  `admit_into`, a named self-host stage. 338 records this as a real capability gap,
  not a packaging nicety.
- `npm`/wasm publish: gated on 335 (cordis-rs on wasm32, the `Map` value model),
  then a publish step.

The split is unambiguous: 338's py ecosystem exit is startable now; its polyglot
ecosystem exit is the arc's frontier and is named, not faked.

## 6. The real external consumer example

The exit test is "a third-party tool imports the package and admits code", so the
example must be genuinely external in shape. The recommended py example is a
minimal MCP-server-shaped or CI-check-shaped consumer that:

1. declares `revl` as a dependency (a `pyproject.toml` depending on the published
   wheel, built and installed into a FRESH environment, so the example proves the
   PACKAGED surface, not in-tree imports);
2. imports ONLY `revl.gate`;
3. admits a batch of candidate components (a CI gate over a directory of
   agent-authored `.rvl` files, or an MCP tool that admits a proposed tool source),
   logging `gate_version()` and, per candidate, `{admitted, code}` and the verbatim
   `message` on refusal;
4. keys any verdict cache on `gate_version()` (the language-skew obligation);
5. gates its "run/serve/accept" decision on `admitted`, and NEVER runs a refused
   candidate.

This is 333's harness re-cast as an out-of-tree consumer, which is precisely the
338 delta over 333: 333 proved the embed works from inside the repo; 338 proves it
works as a DEPENDENCY of a stranger's project, against the packaged, versioned,
fenced surface.

## 7. Adversarial self-review

The single most serious flaw, and its fix, first.

### CRITICAL: publishing revl as "the safety kernel" invites a security contract the package cannot honor

**The flaw.** The item's own framing is "revl as the safety kernel for
agent-generated code that other tools build on". Published verbatim as a security
contract, that sentence is an overclaim, and a dependency consumer will build an
unsafe system on it in two distinct ways:

1. **Admit-means-safe-to-run.** A downstream tool `cargo add revl-gate` (or `npm
   i` the wasm gate), calls `admit(source)`, gets `admitted = true`, and RUNS the
   component believing revl made it safe. But the crate/wasm gate ships ONLY layer
   1, a compile-time verdict; it has no witnessed runtime, no approver seam, no
   commit/abort, no WAL, none of the reversible-execution machinery, and it does
   NOT sandbox a granted `extern`'s host body (G8 surfaces host code, it does not
   neuter it). The tool has shipped a host that runs agent code revl only
   type-checked. This is 333's admit != run boundary, but 338's ecosystem framing
   makes it far more dangerous: a stranger reading "safety kernel" on a package
   page has none of 333's design-note context.

2. **Admit-is-one-fact-across-the-fleet.** The py gate's `frontier` is
   `reference-full:<language>`; the rust crate's is `selfhost:<corpus>`. Two
   consumers admitting the SAME source can reach DIFFERENT verdicts, so "revl
   admitted it" is not a single fact unless `frontier` is part of the claim. A
   CI system that admits with the py gate and a runtime that re-admits with the
   rust crate can disagree; a consumer that treats a py admission as authoritative
   for a rust deployment has a false sense of coverage. (The SAFE direction, a
   frontier gate refusing what reference admits, is only an inconvenience; the
   contract's Clause 1 and the differential release gate keep the DANGEROUS
   direction, a native gate admitting what reference refuses, closed. But the
   consumer must still know WHICH gate's verdict it holds.)

**Why it is the CRITICAL.** 338 is the item whose entire deliverable is a
CONTRACT a stranger builds on. A vulnerability in a contract is worse than one in
code: it ships to every consumer, it cannot be patched by fixing revl (the
consumer already built the unsafe assumption in), and it is exactly the assumption
the marketing sentence invites. The other risks below are real but bounded; this
one is unbounded because it propagates into systems revl never sees.

**Resolution (mandatory, and it is section 4).** The published security contract
states the guarantee ASYMMETRICALLY and makes the scoping machine-visible, never
as "safety kernel" unqualified:

- a REFUSAL is authoritative and fail-closed (Clause 1): the dependable half;
- an ADMISSION is a COMPILE-TIME judgment scoped to `gate_version().frontier`, NOT
  a runtime confinement (Clause 2), and the verdict does not travel as a
  tier-portable fact without its `frontier`;
- the RUNTIME half is a SEPARATE, named, py-only dependency (Clause 3, layer 2 /
  334), so "run revertibly" is an explicit adoption, never implied by an admit;
- the gate never confines its host (Clause 4).

Concretely enforced, not just prose: the package documentation leads with the
asymmetric contract, not the "safety kernel" line; `gate_version().frontier` is
promoted to a first-class contract field a consumer MUST record with any admission
it caches or transmits; and the external-consumer example (section 6) DEMONSTRATES
gating "run/accept" on `admitted` while logging `frontier`, so the copyable
reference embodies the contract rather than the overclaim. "Safety kernel" is
retained only as an aspiration for the FULL stack (admit + layer-2 revertible run
+ 411 confinement), never as the guarantee of the bare `admit` a `cargo add`
delivers.

### A2: a consumer pins `api` but breaks on a patch bump by reaching past the fence

**Attack.** A py consumer, needing something `revl.gate` does not expose, imports
`revl.compiler` or `revl.mcp.session` directly (both importable). A revl patch
release (no `api` bump) changes that internal, and the consumer breaks despite
having "pinned the API".

**Assessment / mitigation.** This is the undeclared-reach wound 332 named,
resurfacing on the consumer side, and a py wheel cannot physically hide its
modules. The mitigation is contract + test + docs, not enforcement: the
`__all__`-pinning compat test makes the PROMISED surface stable and CI-guarded; the
documented contract states plainly that anything outside `revl.gate` is private and
unversioned; and where a real consumer need is discovered outside the fence (333
found none for the admit loop, but a future consumer might), the fix is to widen
`revl.gate` deliberately with an `api` bump, not to let consumers keep reaching
past it. 338 cannot prevent a determined reach-around (a host that owns its
imports can import anything), and says so; it makes the SUPPORTED surface explicit
and stable so a consumer that stays inside it is safe across patch bumps.

### A3: the single-gate-per-process invariant blocks a realistic multi-tenant consumer

**Attack.** The realistic ecosystem consumer is a multi-tenant service (a CI system
admitting many repos concurrently, an agent framework with concurrent sessions).
Layer 2's `Gate` refuses a second construction in one process (`_ACTIVE_GATE`,
`gate.py:412`; the loud second-Gate refusal), and it is synchronous/single-thread,
so such a consumer cannot use layer 2 as a dependency today.

**Assessment / mitigation.** True, and it is an honest slice boundary, not a hole.
Layer 1 (`admit`/`admit_into`) is STATELESS, takes no `Gate`, touches no
`_ACTIVE_GATE`, and is safe to call concurrently for per-candidate verdicts, which
is the shape most ecosystem consumers (CI gate, registry-serving admission, MCP
admit verb) actually need. The multi-tenant STATEFUL embed (many live sessions in
one process) is bounded by 332's named-and-deferred owner-scoping and async
host-loop walls, and 338 does not solve them; it states that the dependency's
layer-1 surface serves the common ecosystem case fully, and the stateful
multi-tenant case waits on the same follow-ons 332/334 already track. The example
(section 6) uses layer 1 for exactly this reason.

### A4: a stale cached admission trusted across a language bump

**Attack.** A registry or CI system caches "admitted" verdicts and keeps trusting
them after revl's `language` version moves, so a component admitted under old rules
is served/run under new ones without re-admission.

**Assessment / mitigation.** In-model and handled by the versioning contract: a
cached verdict is valid only for the `gate_version()` that produced it, so a
consumer MUST key its cache on all three fields (`api`, `language`, `frontier`) and
re-admit on any change. 338 states this as a consumer obligation (section 3) and
the example demonstrates the cache key, so the correct pattern is copyable rather
than a footnote a consumer discovers after shipping a stale-trust bug.

## 8. Exit tests

- **The item's own exit (py, slice 1).** A third-party project (out-of-tree,
  depending on the published wheel in a fresh environment) imports `revl.gate`,
  admits a batch of candidates in-process, and its verdicts equal the CLI
  admission oracle's per candidate (333's match discipline, from an external
  package). It gates "accept/run" on `admitted` and never runs a refusal.
- **The public-surface fence.** `revl.gate.__all__` matches the documented
  promised names (CI-pinned); a consumer that stays within it is unaffected by a
  simulated patch-level change to an internal module.
- **The security contract is demonstrated, not just stated.** The example logs
  `gate_version()`, records `frontier` with any cached/transmitted verdict, and its
  docs lead with the asymmetric contract (refusal authoritative; admission
  compile-time + frontier-scoped + not confinement; runtime half separate/py-only);
  a review checks the example never treats a bare admission as "safe to run".
- **Versioning obligations.** The verdict cache is keyed on `gate_version()`; a
  simulated `language`/`frontier` change invalidates stale admissions rather than
  trusting them.
- **Frontier-gated exits, named and blocked.** `cargo add revl-gate` admitting a
  standalone corpus with verdicts differentially agreeing with the reference
  (fail closed) is the rust exit, gated on 332 stage 3; the wasm/npm exit is gated
  on 335. Neither is claimed startable before its frontier.
- **Additivity.** The full suite and every backend golden are byte-identical with
  the fence test and example present; a program that never imports `revl.gate`
  cannot observe 338 exists.
- **`test_doc_examples` stays green**: every revl-ish block here is `sketch`- or
  plain-fenced and must not compile until the feature lands.

## The honest hard part (consolidated)

Four costs, taken in the open. First, 338's whole deliverable is a CONTRACT a
stranger builds on, and the item's own "safety kernel" framing is an overclaim the
adversarial review exists to correct: the dependable guarantee is that a REFUSAL is
authoritative and fail-closed, while an ADMISSION is a frontier-scoped compile-time
judgment, not a runtime sandbox and not a tier-portable fact, and the runtime half
is a separate py-only adoption; a package page that led with "safety kernel"
unqualified would ship a vulnerability into systems revl never sees. Second, the
polyglot ecosystem exit (`cargo add`, `npm i`) is frontier-gated on 332's deferred
crate and 335's wasm work, and even once the crate lands it offers layer-1 standalone
`admit` on the self-host frontier, NOT native `admit_into` and NOT layer 2, so the
realistic agent-loop shape and the reversible-run half stay py-only for now.
Third, the py dependency surface is fenced by contract and test, not by the runtime:
a wheel cannot hide its modules, so a consumer reaching past `revl.gate` is on its
own across patch bumps, and the honest promise is "stay inside `revl.gate` and you
are safe to pin", not "we prevent all reach-around". Fourth, the stateful embed is
single-gate, single-thread, synchronous by inheritance from 332, so a multi-tenant
consumer gets the stateless layer-1 verdict surface fully and the stateful
multi-session embed only within 332's named-and-deferred walls; 338 publishes what
exists cleanly and names the frontier rather than blurring the line.
