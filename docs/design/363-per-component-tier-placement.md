# 363: per-component tier placement

Design note for roadmap item 363 (`docs/v2.0-roadmap.md:4020`), a revl-harness
CLI-engine provider request: let a composition declare which backend tier each
component emits to, so a hot worker compiles to rust or go while the rest of
the composition stays on py/ts. This is design-first. It changes no compiler
code; it records the measured gap, the machinery that already exists and how
far it already goes, the one architectural question the item turns on (is
cross-tier a new boundary or the existing cross-process one), three surface
options with trade-offs, a recommendation, the guarantee accounting across the
boundary, the tier-capability gate, a staged plan, and exit tests an
implementation agent can pick up. Per the item's own framing this is EXPLICIT,
author-declared placement, never automatic tier selection; the distribution
model's non-goals (`docs/distribution-model.md`, section 4) are load-bearing
here and this note does not relitigate them.

## The problem (measured)

`revl run` emits the whole composition to ONE tier per run. The CLI takes a
single `--backend py|ts|rust|java|wasm|go` (`src/revl/cli/parser.py:557-563`),
`run_command` compiles one IR and dispatches the WHOLE document to one tier
driver (`src/revl/run.py:1150-1172`: one `run_rust(ir, ...)`, one
`run_ts(ir, ...)`, and so on; py boots in-process at `run.py:1174` onward).
Every tier's emitter has the same whole-document contract:
`emit(ir) -> str` (`backends/python/emit.py:3018`, `backends/rust/emit.py:6171`,
`backends/go/emit.py:5787`; wasm returns a dict of modules,
`backends/wasm/emit.py:5380`, but still for the whole document). There is no
spelling for "this component is CPU-bound, emit IT to rust, keep the control
plane on py".

The harness wants exactly that split: its CLI-engine provider is a hot worker
(spawn, stream, parse at high call rates) wrapped in a control plane
(configuration, routing, session state) that is pure orchestration. Today the
choice is all-or-nothing per run, and the all-native choice drags every
py-convenient component onto the strictest tier.

The second measurement reframes the item, exactly as the stdlib import-thunk
reframed item 396: most of the machinery already exists, undeclared at the
component level. The placement conductor (`revl run --placement p.toml`)
already gives every PROCESS a tier: `backend = "rust" | "py" | "node" | "ts" |
"java" | "go"` per `[processes.<p>]` (`src/revl/placement.py:9-15`, accepted
set at `placement.py:72`), builds one artifact per tier used
(`placement.py:1177-1197`), and wires cross-process keys through generated
proxy/stub pairs over UDS or TCP+mTLS. A component's tier is even mutable at
runtime, per component: `revl swap <component> --to <backend>` re-hosts one
component on another tier live, behind an admission gate
(`placement.py:238-299`, the conductor-side swap at `placement.py:1312`
onward). So per-component tier placement is not a missing capability. It is a
capability that today can only be spelled by hand-authoring a process topology
in a TOML, with three real gaps behind the spelling:

1. No per-COMPONENT declaration. The author must invent process names and
   group components into them; the tier is a property of the invented process,
   not of the component (`placement.py:815-829` builds the `placed` map from
   the process tables).
2. The emitters get the WHOLE document per tier, not the placed slice. Only
   the ts tier has a narrowing (`ts_safe_ir`, `placement.py:588-625`, built
   for item 144's py-extern case); `_build_rust` (`placement.py:677-692`),
   `_build_go` (`placement.py:695-720`) and `_build_java`
   (`placement.py:649-674`) serialize the full IR. A composition whose
   py-placed control plane reaches a `@py`-only extern cannot put its hot
   worker on rust at all today: the rust build emits the whole document and
   refuses the extern it cannot spell, even though no rust-placed component
   reaches it. This is the "emit pipeline learning per-component backend
   selection" gate the roadmap names.
3. No tier-capability gate at plan time. A component placed on a tier that
   refuses one of its constructs (a declared function type on java, the wasm
   i32/value gate) surfaces as a raw build error from the tier toolchain
   (`placement.py:685-686` wraps it as "rust emit failed: ..."), not as a
   placement diagnostic naming the component and the tier.

Item 363 is those three gaps, plus the guarantee accounting that makes closing
them safe.

## Background: what already exists, with the seams named

### The distribution model and its boundary

revl already splits one composition across processes by placement
(`docs/distribution-model.md`; `docs/interop-bridge.md`). The conductor
compiles the composition ONCE (`placement.py:798`), so G2/G3 are checked over
the whole manifest before anything is split (DESIGN.md:225-232 is the
guarantee table; DESIGN.md:234-240 states the linker phase that makes G2/G3
static). It then derives, per process: which keys it provides, which it
requires from another process (`placement.py:847-849`), a proxy spec per
cross-process require (`placement.py:1086-1122`) and a serve spec whose
method list is the service declaration's, used as the stub's allowlist so the
served surface is exactly the enumerable one, G8
(`placement.py:1146-1159`; enforcement per tier described in
`docs/interop-bridge.md`, "What the stub will dispatch").

What crosses the seam is fixed and documented: values cross BY COPY in the
canonical wire encoding (scalars, `List`, records, `Map`, `Opt` untagged,
ADTs and `Result` as `{"$kind", "$value"}`;
`docs/interop-bridge.md:163-189`), verified across all placement tiers; the
one unmarshalled type is the opaque host `Value`
(`docs/interop-bridge.md:158-161`). Resource types, anything an
`extern acquire` returns, do not copy; `revl audit`'s distributability verdict
names any service whose signature mentions one
(`src/revl/distribute.py:8-21`, `:29-35`, `:49`). Every proxy carries a seam
deadline (`placement.py:74-79`, `:1084-1095`; `docs/seam-deadlines.md`), and
peer death is a provider withdrawal driving the R2/R3 reactive cascade
(`docs/interop-bridge.md:222-228`). A seam may name a machine: TCP + mutual
TLS with per-process identity (item 56, `docs/network-placement.md`), with a
per-ROLE tier rule today: the mTLS listener ships on the py runner only, the
client on py and node/ts, so a rust/go/java process may neither serve nor
consume a network seam (`placement.py:1005-1019`;
`docs/network-placement.md:121-143`).

### Live tier change per component: swap

`swap_admission` (`placement.py:238-299`) is the existing per-component
cross-tier gate, and it is the template for 363's checks. It recompiles the
candidate against the RUNNING manifest (`placement.py:265-266`), so G2/G3
admission is the same checker, tier-agnostic; and it refuses the swap when a
provided-and-consumed service is not transport-safe
(`placement.py:284-297`), because a tier swap moves the component across a
process seam. The teardown of the displaced provider is LIFO inside its own
process (`placement.py:1404`).

### Per-tier routing (item 173) and realms

Item 363 was gated on item 173, which has landed: emitted-body routing runs on
py/ts/rust (item 167, `docs/v2.0-roadmap.md:3237`) and, via 173, on wasm
first-party and go through the stc-go fork; java has the emitter and awaits an
upstream cordis4j release (`docs/v2.0-roadmap.md:3249`;
`docs/distribution-model.md:199-235`). Routing is an intra-process mechanism:
a routed require (`isolate <key> in realms(...) strategy(...)`, item 162,
`src/revl/parser.py:239-247`) resolves N per-realm provider handles inside the
component's own runtime, and a Router's `spawn`ed pool lives in the spawning
process (`docs/router.md`, sections 1 and 4). Placement already polices
realm/host consistency: a host may pin itself to a realm and a component
isolating into a foreign realm is refused on it
(`placement.py:443-449`, `:466-474`, item 119).

### The load-bearing facts

- The tier is ALREADY a per-process property, and cross-tier communication
  ALREADY rides the serialized proxy/stub seam. Nothing about "a component on
  another tier" is new at runtime.
- The compiler links ONCE over the whole composition before any split
  (`placement.py:798`); placement is packaging downstream of verification.
- The precedent for WHERE placement facts live is consistent and deliberate:
  the interop design answered "language construct or library pattern?" with
  "neither, a backend + manifest concern; the linker, the checker, and the
  source language are untouched" (`docs/interop-bridge.md:372-377`, and the
  no-new-grammar verdict at `:346-350`), and item 119 added the
  capability/realm dimension "read off the placement toml (no `.rvl` source
  change)" (`placement.py:390-399`).

## The crux: an extension of per-process distribution, not a new boundary

The design question the item poses: does a component on another tier need a
new in-process cross-tier boundary, or is it the existing per-process
distribution with a tier attached? The answer is the second, and it is worth
establishing precisely rather than by vibes.

Two tiers cannot share a heap. A rust component and a py component have
different value representations, allocators, and schedulers; any in-process
composition of the two is an FFI seam that must marshal every crossing value
anyway. So an in-process cross-tier boundary would pay the full serialization
cost of the process seam while giving up what the process seam already
provides: the canonical encoding all tiers agree on, the G8 stub allowlist,
the seam deadline, and above all the failure model, where a dead peer is a
provider withdrawal driving R2/R3 instead of a segfault taking down the
co-resident control plane. It would also be a per-PAIR mechanism (py-in-rust,
py-in-go, node-in-rust, ...) where the process seam is one mechanism for all
tiers. And it would blur the fault domain: half the point of placing the hot
worker separately is that its crash is a withdrawal, not a shared-fate abort.

So: a component placed on tier X runs in a process on tier X, and cross-tier
calls ride the SAME seam cross-process calls ride today, the generated
proxy/stub over UDS on one machine (`placement.py:1119`) or TCP+mTLS across
machines (`placement.py:1104-1117`), value-copy canonical encoding, deadline,
withdrawal. This note adds no transport, no encoding, and no runtime
machinery. Two components on the same declared tier may share one process,
exactly as a multi-component `[processes]` entry does today; two components on
different tiers are in different processes BY CONSTRUCTION. The in-process
cross-tier boundary is refused as a design, in section-4-of-the-
distribution-model style: if a future proposal wants it, the answer is this
section.

## Surface: how a component declares its tier

Three candidate spellings, per the roadmap text.

### Option (a): a source-level placement clause

```revl sketch
component HotWorker placed on rust {
  provides work: Work
  ...
}
```

The declaration travels with the component; the hot path is self-documenting
in the `.rvl`.

The costs are structural, not cosmetic:

- It breaks the portability property the conformance suite is built on: the
  same source compiles on every tier, and a refusal is a named tier limit
  (`docs/conformance.md:171`). A tier baked into source means `revl test
  --all`, the per-tier goldens, `revl run --backend py` for local dev, and
  the self-host corpus all need an override-or-ignore story for the clause,
  and every one of those stories is a place where source says one thing and
  the run does another.
- It spends grammar. The interop design's direct answer was that the broker
  is manifest data with zero new syntax (`docs/interop-bridge.md:346-350`,
  `:372-377`), and item 119 held the same line for capabilities and realm
  pins (`placement.py:390-399`). Deployment substrate in source reverses a
  decided precedent.
- It fixes deployment at authoring time. The same composition legitimately
  runs all-py in CI, py+rust on a laptop, and py+rust-across-machines in
  production; a source clause pins one of those or grows a manifest override
  anyway, at which point the manifest is the real surface and the clause is
  a default that can lie.
- `revl swap <component> --to <backend>` can move the component off its
  declared tier at runtime (`placement.py:238-299`); after a swap the source
  clause is stale by design.

### Option (b): per-component tiers in the placement manifest

Extend the existing placement manifest with a component-keyed tier table, and
let the conductor synthesize the process topology:

```toml
# hot_worker.toml: the whole per-component placement surface
default_tier = "py"

[tiers]
HotWorker = "rust"

[config.HotWorker]
threads = 4
```

Expansion rule, fully mechanical: group components by declared tier (every
component absent from `[tiers]` takes `default_tier`), synthesize one process
per distinct tier named `tier_<name>` (`tier_py`, `tier_rust`), and proceed
through the EXISTING conductor unchanged: `placed` map, seams, proxies, stub
allowlists, preflight, capability/realm checks, per-tier builds. A
`[tiers]`-form manifest and a hand-written `[processes]` form that groups the
same components onto the same tiers are the same placement by construction.
The two forms are mutually exclusive in one file (a manifest carrying both is
refused with one diagnostic naming the rule); an author who needs finer
process topology than one-process-per-tier, or addresses, TLS identities,
probes, per-process deadlines, writes `[processes]`, which remains the full
surface and gains a per-process `backend` nothing (it already has it).
A CLI spelling for the two-tier quick case can ride the same expansion with
no file: `revl run app.rvl --place HotWorker=rust` synthesizes the same map
(a later stage, sugar only).

What this buys: the author declares placement per component, in a file that is
trusted at the same level as source, reviewed and versioned
(`docs/interop-bridge.md`, "Trust model": a placement file is trusted input at
the level of the `.rvl`); dev and CI keep running the same source all-py with
no override machinery; swap stays coherent (the manifest describes boot-time
placement, swap changes runtime placement, and the conductor already owns
both); and the implementation is an expansion function in front of machinery
that ships.

What it costs, honestly: the declaration does not travel with the component
source. A reader of `HotWorker.rvl` does not see "this is the native one"
without opening the manifest. That is the same trade every placement fact
already made (which machine, which capabilities, which realm pin), and the
mitigation is the same: `revl run --plan` and the conductor's boot summary
print the placement (`placement.py:1243`), and `revl audit` can grow the
per-component tier line.

### Option (c): a tier on the realm declaration

Extend `isolate <key> in realm("w1")` with a tier. Refused. A realm label is
verification identity, the subject of G2's per-(key, realm) disjointness
(`docs/design-v2-realms.md:23`, `:37`); a tier is deployment substrate. One
component isolates different keys into different realms, so there is no
single realm slot that means "this component"; and welding the two would make
a tier change a semantic change to the manifest the linker checks, which is
exactly backwards. The existing division of labor stays: realms say what must
be disjoint, placement says where things run, and the item-119 check polices
their consistency (`placement.py:466-474`).

### Recommendation: (b)

Adopt option (b). It is the only spelling that adds the missing declaration
without spending grammar, breaking tier portability, or fighting swap, and it
lands as an expansion in front of shipped machinery. Option (a) is not built;
if the self-documentation want returns, the honest home for it is a doc
comment, not a clause the toolchain must know when to ignore. This also keeps
the item's own boundary crisp: 363 changes how placement is DECLARED and how
artifacts are SLICED; it does not change what placement means.

## The boundary, precisely

A cross-tier crossing is a cross-process crossing. Everything below is the
existing contract, restated as the conditions 363 checks at plan time; only
the last two bullets add checks that do not run today.

- The crossed key is a declared `service`; the proxy forwards and the stub
  dispatches only the declared method list (G8, `placement.py:1146-1159`;
  DESIGN.md:232).
- Every value that crosses is a value type in the canonical encoding
  (`docs/interop-bridge.md:163-189`). The encoding is tier-complete for
  scalars, `List`, `Map`, records, `Opt`, ADTs, `Result` on every placement
  tier; the opaque host `Value` does not cross
  (`docs/interop-bridge.md:158-161`).
- Deadlines and withdrawal: every cross-tier proxy carries a seam deadline
  (`placement.py:1084-1095`), and a dead or wedged peer becomes a provider
  withdrawal, R2/R3 (`docs/interop-bridge.md:222-228`,
  `docs/network-path.md` for the deadline-breach-is-withdrawal rule on
  network seams).
- NEW CHECK, resource types: a service crossing a tier boundary whose
  signature mentions a resource type (an `extern acquire` return,
  `distribute.py:29-35`) is refused at plan time, before anything spawns. A
  handle's lifetime is tied to a fiber in one process; copying it is
  meaningless and proxying it is out of scope. Today only `revl swap`
  enforces this (`placement.py:284-297`) and the initial conductor does not;
  363 closes that asymmetry for the seams it creates.
- NEW REPORT, sync crossings: a cross-tier seam whose service is
  address-space-bound for async reasons only (a sync `fn` or `emission`,
  `distribute.py:12-14`) is permitted and NAMED at plan time (the
  distributability verdict per seam, one line each). Permitted, because
  today's cross-process placements permit it and pay the blocking round-trip
  knowingly (`docs/interop-bridge.md:112-118`); named, because a hot worker
  behind a sync seam is a performance lie the author should see before
  wondering where the rust speedup went. `revl swap` keeps its stricter
  refusal (a swap re-points a LIVE consumer, which is the riskier moment);
  the asymmetry is deliberate and stated.

## Emission: from one artifact to a per-tier artifact set

### The placement slice

The fix for gap 2 is at the IR-document level, not inside the emitters. Define
one function, the placement slice: given the linked IR and the set of
components placed on a process, keep those components, every service and type
declaration, and every top-level fn and extern REACHED from a kept component;
drop the rest. Reachability reuses the deliberately over-inclusive name walk
that `ts_safe_ir` already uses (`_names_in`, `placement.py:569-585`), erring
toward keeping. Each per-tier build then hands its process's slice to the
existing whole-document `emit(ir)` (`placement.py:1177-1197` stays the build
seam; `_build_rust`/`_build_go`/`_build_java`/`_emit_ts_module` consume a
slice instead of the full IR). This answers the roadmap's "emit pipeline
learning per-component backend selection" with the cheapest sound mechanism:
selection happens once, uniformly, in the conductor; no emitter grows a
per-component mode; every emitter keeps its contract.

Properties, each an exit test:

- Additive: a single-process placement's slice is the full IR byte-identical
  (nothing is unreachable from all components together minus nothing), so
  every existing placement builds byte-identical artifacts.
- Unblocking: a `@py`-only extern reached only by py-placed components never
  reaches the rust/go/java emitters, so the hot-worker-on-rust composition
  builds. This is `ts_safe_ir`'s job done by placement declaration instead of
  by per-tier body inference; `ts_safe_ir` remains as a second, ts-specific
  filter applied after the slice (a py-only extern reached by a ts-PLACED
  component is a placement error the capability gate below reports, but the
  legacy inference keeps old placements working unchanged).
- Duplication, stated: a shared pure fn reached from both sides of a seam is
  emitted into BOTH artifacts. That is correct (pure code has no home
  process) and it is exactly where the determinism obligation below bites.

One consequence worth naming: because the slice is per PROCESS, two processes
on the SAME tier get different artifacts. Today they share one build
(`built` is keyed by backend, `placement.py:1177-1179`). The implementation
keys builds by (backend, component-set) or simply per process; the shared
build was a cache, not a semantic.

### What `revl run` produces

With a `[tiers]` manifest, `revl run app.rvl --placement hot_worker.toml`
produces what the conductor produces today, now per component declaration: one
compiled+linked IR; one artifact per process (a cordis-ts module in `_gen`, a
rust binary via the placement runner crate, a go binary, java classes, a py
spec run in-process by `_process_runner`); one spec file per process carrying
its components, activation prerequisites (`placement.py:1140`), proxies,
serve allowlist, and config; sockets or endpoints; and the boot summary
naming every process, tier, and component (`placement.py:1243`). `--once`
runs the probes, which may cross the seams, and tears down LIFO per process.
`revl run --backend X` without a placement is untouched, byte-identical, and
`--backend` combined with `--placement` becomes a loud refusal instead of
today's silent ignore (`run.py:1114-1116` returns into the placement path
before `--backend` is read); one diagnostic names the rule. Bundling a
mixed-tier placement (`revl bundle`) is out of scope here and refused with a
named gap until its own stage lands, the 396 discipline.

## Guarantee preservation across the tier boundary

### G2, G3, G4: linked once, before the split

The conductor compiles the whole composition in one `compile_files` call
(`placement.py:798`) and the placement slice happens strictly AFTER linking,
so provision disjointness, acyclicity, and inverse-or-emit are checked over
the whole manifest with tiers invisible to the checker (DESIGN.md:225-232).
A cross-tier component is admitted exactly like a cross-process one because
it IS one: dynamic admission recompiles against the running manifest with the
same checker (`swap_admission`, `placement.py:265-266`; "booting is
admission", `run.py:1128`). Nothing in 363 gives the linker a tier input, and
that is a property to test, not an accident: the same composition with any
`[tiers]` assignment links identically or refuses identically.

### G7, effects, witnessed frames: per process, exactly as today

Effects accumulate in the process of the component that performs them, and
derived teardown is LIFO-complete PER PROCESS (`placement.py:1404`; a
withdrawn provider's consumer replays its own inverses LIFO in its own
process, `docs/interop-bridge.md:112-118`). Cross-process ordering is the
reactive cascade, not a global LIFO, and 363 inherits that statement
verbatim.

Witnessed frames and compensation do not span the seam, and this note scopes
that out rather than hiding it: a witnessed frame is runtime state of one
process; what crosses a seam is a call and a value, never a witness ledger.
A consumer-side witnessed frame that fails does not and cannot unwind effects
the provider accumulated on the far side; the provider's effects are unwound
by the provider's own lifecycle (its teardown, its A8 revert, its
withdrawal-driven inverse replay). A "witnessed rollback spanning two tiers"
would be a distributed transaction; revl does not have one across processes
today and 363 does not create one. An author who needs cross-tier
compensation writes it explicitly as service operations (a compensating call
that itself crosses the seam), which the audit sees as an ordinary crossing.
This is identical to the cross-process status quo; 363 adds no weakening and
no strengthening, and the design's claim is exactly parity.

### Determinism: the item-385 class becomes load-bearing

Item 385 established the rule that a pure stdlib function must be
byte-identical across tiers, fixed `json_stringify`, and added cross-tier
byte-equality conformance tests (`docs/v2.0-roadmap.md:4102`; item 390
extended it to records on go, `:4112`). Per-component placement raises the
stakes from "cross-tier bites when you hash on two tiers" to "every shared
pure fn duplicated into two artifacts by the slice is a determinism
obligation": a control-plane py process and a hot-worker rust process that
both canonicalize a request must produce the same bytes or every
cassette/signature/ledger equality across the seam is broken. The guarantee
instrument is the 385 conformance suite, and the honest statement is that it
is a corpus, not a proof: drift outside the audited surface remains possible,
which is why the exit tests below include a cross-tier byte-agreement check
on a shared pure fn evaluated on both sides of a live seam, and why the 385
audit habit ("audit json/value/str for other cross-tier byte drift") is part
of this item's exit rather than a hope.

### Admission and identity

Runtime admission (swap, evolve) is tier-agnostic by construction (the gate
recompiles sources against the running manifest and never reads a tier,
`placement.py:265-266`); network identity remains per process via the
operator model on network seams (`placement.py:977-987`). 363 changes
neither.

## Tier-capability gating

A component placed on a tier that cannot support it must be refused at plan
time with a diagnostic naming the component, the tier, and the reason. The
refusal classes exist today, scattered across the pipeline by design (each is
a named, deliberate tier limit, `docs/conformance.md:171`):

- declared function types: refused on java and wasm, escaping positions on
  rust (`docs/function-types.md:205-212`, `:242-269`; the refusal text at
  `backends/java/emit.py:183-191`; the wasm lowerability gate at
  `backends/wasm/emit.py`, `_V3Emitter._check_type`);
- a reached extern with no `@<tier>` body (each emitter refuses at emit);
- a `config` extern off the injection tiers (`src/revl/lower.py:645`,
  refusal at `lower.py:2486-2490`) and a host-module `ref` off py/ts
  (`lower.py:2523-2540`), both already compile-time and tier-naming;
- wasm's broader envelope: `Float`, `Map` and function-typed signatures, no
  config channel, no host builtins on that tier (`docs/conformance.md`, the
  per-tier refusal table).

Mechanism: the emitters are the oracle, not a second list. At plan time,
before anything spawns, the conductor dry-runs each process's slice through
its tier's emitter (for rust/go/java/ts this is the emit half of the build it
would do anyway; for py it is one extra `emit` call). A refusal is re-wrapped
as a placement diagnostic; attribution runs the emitter again on
single-component sub-slices to name the culprit component (bounded work, done
only on the failure path). This keeps the gate exactly as strong as each
tier's real refusal set with zero drift risk, at the cost of the diagnostic's
tail being the emitter's own message. A curated mirror of the refusal classes
in the conductor was considered and rejected: it would be a second source of
truth that goes stale the day a tier limit moves, the exact failure mode the
conformance harness exists to catch.

Two placement-level refusals sit above the emitters:

- wasm: not a placement tier in this item. The conductor's backend set has no
  wasm (`placement.py:72`) because no wasm placement runner, bridge client,
  or stub exists on the substrate (`backends/wasm/` has run/lifecycle/router
  harnesses, no placement runner). `[tiers] X = "wasm"` is refused at plan
  naming the gap and redirecting to rust/go for native placement. A wasm
  placement runner is a real, separately designed follow-on (the substrate's
  natural seam is embedder-satisfied imports, a different mechanism), not a
  pending flag flip. The item's headline ("rust/go/wasm") lands as rust/go
  here, and this note says so instead of promising the third.
- network roles: a `[tiers]`-placed process that ends up on a network seam
  obeys the existing per-role rule, provider py-only, consumer py/node
  (`placement.py:1005-1019`); a hot worker on rust serves same-machine UDS
  seams today. Cross-machine native providers wait on a non-py listener,
  out of scope here, already tracked by the network-placement docs.

## Reconciliation: item 173, and the distribution model's non-goals

173 and 363 compose by layering, and the layering is already the codebase's:
routing is INSIDE a process (a routed require resolves per-realm handles in
the component's own runtime; a Router's `spawn_pool` workers live in the
Router's process, `docs/router.md`), placement is BETWEEN processes. So the
hot-path move the harness wants is: place the Router component (with its
pool) on rust via `[tiers]`, and the entire pool goes native in one process,
while every consumer still sees one provider in the parent realm (G2,
`docs/distribution-model.md`, section 2). A routed require whose worker
realms would be served from a DIFFERENT process is not a thing today
(cross-process keys arrive as proxies resolved before local activation,
`placement.py:1134-1140`) and stays out of scope; the realm/host consistency
check (`placement.py:466-474`) and the co-location advice
(`placement.py:478-500`) already push the pool and its router together.

The non-goals hold unchanged. This is declared placement, never an
auto-optimizer: no cost model chooses a tier, no profile output feeds
`[tiers]`, and the distribution model's warning stands, the naive
"compile each piece to its fastest tier" instinct trades a verified
composition for an unverified distributed system
(`docs/distribution-model.md:128-167`). 363 keeps the composition verified by
keeping the linker whole-manifest and the boundary the audited seam; what it
declines to do is decide FOR the author.

## Staged implementation plan

Each stage lands independently; a placement using none of the new surface
stays byte-identical throughout (the 342/388/396 additivity discipline).

- Stage 1 (surface). The `[tiers]` table + `default_tier` in the placement
  manifest, the expansion to synthesized one-process-per-tier `[processes]`,
  the both-forms refusal, and the `--backend`-with-`--placement` refusal.
  Everything downstream unchanged. Exit: a two-line `[tiers]` manifest runs a
  two-tier composition end to end with `--once` probes crossing the seam; a
  `[tiers]`-form and the equivalent hand-written `[processes]` form produce
  identical specs; existing placements byte-identical.
- Stage 2 (placement slice). The per-process slice function; per-process
  builds consume slices; build cache keyed per process. Exit: the
  py-only-extern composition places its hot worker on rust and boots (red
  today); a single-process placement's slice is the full IR byte-identical;
  ts placements with the legacy `ts_safe_ir` inference unchanged.
- Stage 3 (capability gate). Plan-time dry-run emit per slice, refusal
  re-wrapped naming component + tier, single-component attribution on the
  failure path; the wasm placement refusal with the named gap. Exit: a
  fn-typed component placed on java is refused at plan naming the component
  and the java reason (never a javac stderr); the same component placed on py
  in the same manifest boots; `[tiers] X = "wasm"` refuses with the redirect.
- Stage 4 (boundary checks). Plan-time per-seam distributability report; the
  resource-type cross-seam refusal at the conductor (parity with swap's
  existing rule). Exit: a service returning an `extern acquire` type across a
  declared tier boundary is refused before spawn, naming the type and the
  seam; a sync-emission seam boots and prints its address-space-bound line.
- Stage 5 (demo + guarantee proofs). The roadmap deliverable: a demo
  composition with a rust-placed hot worker and a py control plane; the
  cross-tier determinism exit (shared pure fn byte-agreement across the live
  seam); doc updates (`docs/distribution-model.md` gains the per-component
  tier section; `revl audit` prints the per-component tier when a placement
  is given). Exit: demo green under `--once`; audit line present;
  `test_doc_examples` green (this note's proposed syntax stays `sketch`
  until then).
- Named follow-ons, out of scope: the wasm placement runner (own design
  note); non-py mTLS listener and rust/go/java network clients; `revl
  bundle` for mixed-tier placements; `--place` CLI sugar.

## Exit tests

- Two-tier composition: one `[tiers]` manifest, HotWorker on rust, control
  plane on py; `revl run --placement --once` emits two artifacts, both boot,
  a probe's call crosses the seam and returns the value; teardown proves no
  residue in either process.
- Incapable tier refused: a component declaring a function-typed service
  param placed on java, and any component placed on wasm, are refused at plan
  time with diagnostics naming the component, the tier, and the reason;
  nothing is spawned and no tier toolchain error is the user-visible surface.
- Single-tier byte-identity: `revl run --backend X` for every X is
  byte-identical with 363 landed; a placement with no `[tiers]` is
  byte-identical through specs, builds, and boot output; a one-process
  placement's slice equals the full IR.
- Slice unblocking: the composition whose py control plane reaches a
  `@py`-only extern boots with a rust-placed worker (the extern never
  reaches the rust emitter); the same extern reached BY the rust-placed
  component is a stage-3 refusal naming it.
- Guarantees: a G2 collision compiles to the same refusal under any `[tiers]`
  assignment (the linker never sees tiers); killing the rust worker withdraws
  the py consumer reactively (R2/R3) and a replacement re-activates it; the
  seam-deadline breach on a wedged worker surfaces as the distinguishable
  deadline error, not a hang.
- Boundary types: a resource-returning service across the declared boundary
  is refused at plan; an ADT and a `Result` cross the rust/py seam and
  rebuild natively (the existing outcome fixtures re-run under a `[tiers]`
  manifest).
- Determinism: one pure fn reachable from both processes returns
  byte-identical output invoked from each side of the live seam (the 385
  suite extended from per-tier goldens to an in-placement assertion).
- Swap parity: `revl swap` of the placed worker to another tier still runs
  the same admission and transport-safety gate; a swap back lands
  byte-identical artifacts.
- `test_doc_examples` stays green: every proposed-syntax block in this note
  is fenced `revl sketch` (the option-a clause must not compile until, and
  unless, it ever lands); manifests are TOML blocks the gate does not
  compile.

## The honest hard part (consolidated)

The item looks like a language feature and is actually a packaging and
declaration feature over a boundary that already exists, which is its good
fortune and its discipline: every guarantee statement above is PARITY with
the cross-process status quo, not a new proof. The genuinely hard residues
are three. First, the capability oracle: no single compile-time surface
enumerates what a tier refuses, the refusals live in six emitters at emit
time by design, so the plan-time gate must either duplicate that knowledge
(and drift) or dry-run the emitters and attribute failures by re-slicing,
which is the recommended shape but makes diagnostic quality hostage to
emitter message quality, and the attribution pass is the part most likely to
need iteration. Second, determinism: the placement slice duplicates shared
pure code into every artifact that reaches it, so the 385 byte-equality
discipline stops being a stdlib nicety and becomes the correctness floor for
any value both sides compute independently, and that discipline is a
conformance corpus, not a theorem; the exit test pins one live seam, the
audit habit has to carry the rest. Third, the scope refusals must be held
against pressure: no wasm placement until the substrate grows a real
placement runner, no witnessed frame spanning the seam, no auto-tiering ever;
each has a named home in this note precisely so the next "small exception"
has something to argue with. Everything else, the surface, the slice, the
boundary checks, is an expansion function, a filter, and two plan-time
checks in front of machinery that already runs mixed-tier compositions every
day under hand-written process maps.
