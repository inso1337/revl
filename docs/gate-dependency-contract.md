# Depending on revl: the security contract (`revl.gate`)

This is for a host that depends on revl's admission gate as a LIBRARY: an
MCP server, a CI system, an agent framework, a registry, rather than
shelling out to the `revl` command. Read this before writing a line of
integration code.

Three dependency forms exist, and they do not give the same guarantee.
Sections one to five below are the py wheel (`pip install revl`, then
`from revl.gate import admit, gate_version, ...`), which is the full
reference compiler and can both refuse and admit.
["The rust tier"](#the-rust-tier-the-revl-gate-crate) is the native
`revl-gate` crate. Its VERDICT type can only REFUSE. The crate does issue
admissions, but through a separate type and only inside a narrow certified
surface (issue #346); a `NoObjection` is never one of them.
["The wasm tier"](#the-wasm-tier-the-gate-in-a-browser-a-worker-a-cdn-node)
is that same crate packaged as a component for a browser, an edge worker or
`wasmtime`. It carries the crate's verdict surface and NOT its admission
surface: `issue_admission` is deliberately off the component's world, which
`crates/revl-gate-wasm/wit/gate.wit` states in the `verdict` record's own
comment, so on that tier the admitting arm really is absent. The contract is
the same asymmetric one in all three; the rust tier has a sliver of the
admitting half, and the wasm tier has none of it.

## The contract, stated once

**A refusal is authoritative and fail-closed. An admission is a
compile-time judgment scoped to `gate_version().frontier`, not a runtime
confinement. The runtime half is a separate, py-only dependency you adopt
explicitly. The gate never confines its host.**

The optional Python `revl.fs_workspace` bootstrap is a separate supported
runtime capability, versioned independently of `revl.gate`. Its process-lifetime
physical-root binding and limits are specified in
[witnessed-fs.md](witnessed-fs.md#python-process-lifetime-physical-root-binding-api-1).
It neither changes a gate admission nor confines arbitrary host code.
The same facade's separately versioned
[committed-preimage finalizer](witnessed-fs.md#trusted-committed-preimage-cleanup-api-1)
is trusted-host-only and requires authoritative commit ownership plus exclusive
sidecar-directory write access; it is not a commit receipt or an agent capability.

That sentence, not "revl as a safety kernel," is what you are allowed to
build on. "Safety kernel" describes an aspiration for the full stack (admit,
plus the py-only revertible runtime, plus operator-configured confinement),
never the guarantee a bare `admit(source)` call gives you. A host that reads
"admitted" as "safe to run" has built an unsafe system on a sentence revl
never promised.

## Clause 1: a refusal is dependable

If `admit(source).admitted` is `False`, the reference `revl` compiler would
refuse `source` too: same code, same message, verbatim. You may rely on a
refusal absolutely. A component the gate refuses is one the reference
refuses, and you must not run it. This is the strong half of the contract,
and there is no asterisk on it.

## Clause 2: an admission is compile-time, and it is scoped

`admitted = True` means: the source type-checks, has no open holes, its
effects are classified, its requires/provides resolve, and, under the
untrusted-author admission profile, it reaches only what it was granted. It
does **not** mean the admitted code is confined once it runs. An admitted
component's granted `extern` host body is arbitrary host code the gate
*surfaced*, not code it neutered. `admit != safe to run unwitnessed`.

The admission is also scoped to the gate that produced it. `gate_version()`
returns three fields:

```python
{"api": "1.0.0", "language": "2.0.0", "frontier": "reference-full:2.0.0"}
```

- **`api`**: the semver of the `revl.gate` surface itself. Branch your
  integration code on it; pin a compatible range (`api ~= 1.0`).
- **`language`**: the revl language version this gate admits against.
- **`frontier`**: the identifier of what this gate actually COVERS. On the
  py wheel it is `reference-full:<language>`, the whole reference compiler.
  A native gate (the rust crate, the wasm component) pins a narrower
  frontier, `selfhost-admit:<hash>`, a self-hosted subset of the language.

**`frontier` is not an advanced-user footnote. Treat it as part of the
verdict itself.** Two gates at different frontiers can disagree on the same
source. If you cache a verdict, transmit it to another service, or compare it
against a verdict from a different revl deployment, record `frontier`
alongside it. "revl admitted it" is not a portable fact on its own; "revl's
`reference-full:2.0.0` gate admitted it" is.

## Clause 3: the runtime half is a separate adoption

The reversible-execution guarantees (witnessed effects, session commit/abort,
the approver seam, WAL recovery, the self-extension `propose` verb) live in
`revl.gate.Gate`, a stateful, single-gate-per-process, synchronous facade.
Calling `admit`/`admit_into` gets you none of this. If you want "admitted AND
run revertibly," you adopt `Gate` explicitly and take on its walls (one live
gate per process, no async). If you only `pip install revl` and call `admit`,
you are on layer 1 alone, and you own whatever runtime discipline your
admitted code runs under.

The native crate now carries part of this layer as well, in
[`revl_gate::session`](#layer-2-revl_gatesession). It is a real in-process
witnessed runtime, not a reservation, but it has no crash recovery, no
approver callback and no accept half. `revl.gate.Gate` on py is still the only
tier that has all three, which is what "py-only" means in the boxed sentence
above.

## Clause 4: the gate does not confine you

Depending on revl does not change what your own process is allowed to do. A
library cannot jail its own host. revl's guarantees govern the code it
admits, not the process embedding it.

## The versioning obligations this implies

- **Surface skew.** `revl.gate` adds a function: `api` minor-bumps, your
  pinned range is unaffected (additive-only).
- **Language skew.** revl adds a language feature: a component might be
  re-admitted differently under the new `language`. **If you cache verdicts,
  key the cache on the full `gate_version()` triple** (`api`, `language`,
  `frontier`), so a language bump invalidates stale entries instead of you
  trusting an old admission forever.
- **Frontier skew.** The dangerous direction (a narrower-frontier gate
  admitting what the reference refuses) stays closed by revl's own
  differential release gate. But you must still know WHICH gate's verdict
  you are holding before treating it as authoritative for a different
  deployment.

`examples/ecosystem-consumer/` is a small out-of-tree project that
demonstrates all of the above end to end: it declares `revl` as a dependency
in its own `pyproject.toml`, imports only `revl.gate`, admits a batch of
agent-authored `.rvl` candidates, keys its verdict cache on the full
`gate_version()` triple, and gates its "register" decision on `admitted`
while logging `frontier` with every verdict. Read its `README.md` for the
same contract in a consumer's own words.

## The promised import surface

`revl.gate.__all__` is the entire dependency surface: `Verdict`, `Emit`,
`admit`, `admit_into`, `compile_to`, `gate_version`, `Gate`, `GateError`,
`GateRefused`, `AdmitResult`, `ProposeResult`, `Handle`, `recover`. Nothing
else under `revl.*` is promised. The wheel is not minimal. Installing revl
pulls in the whole compiler, the six backends, the stdlib, the MCP server,
and the CLI, and every module under `revl.*` is importable, but only
`revl.gate` is versioned. `tests/test_gate_compat.py` pins this list exactly
in CI, so an added or removed name is a reviewable change, never silent
drift.

**The rule, one line:** branch on `api` and `code`, gate your run/accept
decision on `admitted`, record `frontier` with every verdict you keep, log
`message` but never parse it, and treat anything outside `revl.gate.__all__`
as private and unversioned. You can import it, but a patch release may
change it under you without warning.

## The rust tier: the `revl-gate` crate

Everything above describes the py wheel. There is a second dependency form,
`crates/revl-gate`, the native gate as a rust library, and its contract is
the SAME asymmetric contract with the asymmetry taken further. Read this
before depending on it, because the difference is not a detail:

**The rust gate's verdict surface issues no admissions at all.** It is
`selfhost/lower.rvl`'s
`admit_src` compiled to rust: the composition and guarantee layer (`G1`..`G4`,
`A1`, `PRELUDE`, and parse failures as `BAD`), and **not** the reference type
layer. A type-incorrect program is not something it can refuse. So its
`Verdict` has three arms and no `Admitted`:

| arm | meaning | what you may do with it |
|---|---|---|
| `Refused { code, message }` | the reference compiler refuses this source too, same code, same message verbatim | act on it: this is Clause 1, and it is the whole reason to depend on the crate |
| `NoObjection` | "this gate found nothing it is able to refuse" | **not an admission.** Get a reference verdict before accepting or running anything |
| `OutsideFrontier { reason }` | the gate is not entitled to decide this source (a construct outside its generated frontier table, an oversized source, an abort in the native front end) | same: ask the reference |

The wire shape fails closed to match: `Verdict::to_json` reports
`"admitted": false` on **every** arm, so a consumer written against the py
tier's fixed `{admitted, code, message}` shape reads this gate as "never
admits" rather than mistaking a no-objection for an admission. The real arm
travels in an extra `"verdict"` field.

`gate_version()` carries a fourth field here, `layer`, which says in prose
what was decided (`"composition + guarantee layer … NOT the reference type
layer"`). Read it before trusting a non-refusal. And the `frontier` differs
from py's at the same `language`, `selfhost-admit:<hash>` versus
`reference-full:<language>`, which is the frontier skew of §"The versioning
obligations" made concrete: **never serve a verdict cached from one tier to a
reader of the other.**

What the verdict surface buys, then, is not a second admission gate for the
language at large. It is a local, in-process, **Python-free refusal** that
byte-agrees with the reference on the covered corpus: a cheap pre-filter in
front of an expensive authoritative check. Refusing what the reference admits
would be an inconvenience; admitting what the reference refuses is the defect
class this arc exists to prevent, and
the verdict surface has no arm that could commit it.

### The composition arm: `admit_into`

`revl_gate::admit_into(source, manifest)` asks the same VERDICT question
across a composition boundary: what does this gate say about `source` once it
is admitted INTO the running composition `manifest` describes? The manifest is
the item-186 row wire, `C/k/r` for a provision, `C<k` for a requirement,
`!halted` for the halt header, rows joined by `;`. The empty manifest is the
empty composition, so `admit_into(src, "")` is `admit(src)`.

It folds the manifest and the incoming source into one composition and decides
the legs that only exist across the boundary: a provision key the running
composition already holds and a realm route the union does not provide (`G2`),
and a dependency cycle spanning the boundary (`G3`). Refusals come out ordered
exactly as the single-source composition of `manifest ++ source` orders them.

Three things it does not do, stated here rather than discovered later. It does
not run the reference type layer any more than `admit` does, so a
type-incorrect candidate is a `NoObjection` here too. It does not RESOLVE a
candidate's `requires`; it checks them for disjointness and acyclicity, and a
`requires` the union does not provide is a no-objection. And it REFUSES, with
the fold's own `MANIFEST` code, a row of no kind at all, or a garbled row of a
kind it does know (a replacement `-C` naming no component, a handoff `C=k:T`
missing its state type), rather than skipping it, because a row this gate cannot
honour is exactly where a wave-through would hide.

Two bounds fail closed ahead of the fold, because an overflow in the native
front end aborts rather than refusing and the crate's `catch_unwind` path
cannot see it: a manifest above `MAX_SOURCE_BYTES`, and a manifest above
`MANIFEST_ROW_LIMIT` rows. Both return `OutsideFrontier`. The two bounds are
separate on purpose: the fold recurses one stack frame per row, so a byte
bound is not a bound on its stack use.

`admit_into` returns a `Verdict`, so the table above applies to it unchanged.
No input produces an admission.

### Which "issues no admissions" sentence applies to what

The crate has two surfaces answering two different questions, so every "no
admissions" claim in this document, in the crate's own docs and in the WIT
world is scoped to the TYPE it is about:

| you call | you hold | can it ever say yes |
|---|---|---|
| `admit`, `admit_into` | `Verdict` | no. Three arms, none admitting, and `Verdict::to_json` writes `"admitted": false` on every one of them |
| `issue_admission`, `issue_admission_into` | `Admission` | yes, and only inside `ADMISSION_SURFACE_ID`. `Admission::Admitted` writes `"admitted": true` |
| the wasm world's `admit`, `admit-into` | the `verdict` record | no. The crate's admission surface is not on that world at all |

The `Verdict` half of this is not a historical accident the admission surface
has since overtaken. `Verdict` is what a consumer holds when it did NOT ask for
an admission by name, and it has no arm that could carry one. That is exactly
why `issue_admission` returns a different type rather than adding a fourth arm.

### The admission surface, and how narrow it is

`revl_gate::issue_admission(source)` returns an `Admission`, not a `Verdict`,
and `Admission::Admitted` is a real admission: `"admitted": true` on the wire,
byte-identical to the py tier's admission wire for the same program. It is a
separate type on purpose. A consumer holding a `Verdict` has no arm it could
misread, and a consumer that wants a green has to ask for one by name.

Two conditions are both necessary, and the first is the structural one:
`admit(source)` must have returned `NoObjection`, so an admission is never
issued over a refusal or a frontier gap; and the source must be inside the
ADMISSION SURFACE named by `ADMISSION_SURFACE_ID`, described in one line by
`ADMITTED_LAYER`, and implemented in the crate's generated `src/admission.rs`.

That surface is deliberately tiny: interface declarations only, meaning
`service` method signatures and scalar `type` aliases over a closed scalar
vocabulary derived from the reference's own table. It holds no function body,
no expression, no literal and no generic head, which is exactly why a
no-objection over it is the whole answer rather than a partial one. Anything
else is `Admission::Withheld`, carrying the verdict verbatim, so switching from
`admit` to `issue_admission` can only ADD the admitted wire.

`issue_admission_into(source, manifest)` asks the same question against a
running composition. The empty manifest is the empty composition and reduces to
the standalone question. Against a non-empty one the candidate carries one
obligation more: nothing it declares may REDECLARE a service the running
composition already declares. That is the only interaction the reference has
between an interface-only candidate and a running manifest, and it gates a
redeclaration on the compatibility relation the type layer decides, so a
redeclaration is withheld here while a fresh interface is admitted.

The running names arrive in the item-186 wire's SERVICE BLOCK: a `!services`
header followed by one `:S,op,op` row per declared service, carrying its name
and the operations it declares, which `revl.manifest_wire` renders from a
compiled composition. The header is the load-bearing half. A wire without it
CLAIMS NOTHING about the running services, so the set is unknown rather than
empty and any declared service is withheld, exactly as before the block existed:
silence is never read as "declares nothing". The operation list is the same claim
one level down, and it is what lets the fold resolve a candidate's call through
`requires k: S` against the RUNNING declaration and refuse a call to an operation
that service does not declare; a `:S` row with no list says nothing about the
surface and decides no member. Two things the block does not buy. A
redeclaration stays withheld, because the block carries the service's operation
names and not their signatures, and the compatibility relation is decided on
those. And a wire carrying a WITHDRAWAL row (`-C`, the replacement wave) is
declined outright: the fold decides a withdrawal in full, and re-deriving which
provisions survive it on this side would be a second implementation of that
reasoning.

Two obligations for a consumer of an admission, both from the ASYMMETRIC clause:
an admission is a compile-time judgment scoped to `gate_version().frontier` and
`ADMISSION_SURFACE_ID`, not runtime confinement; and a cached admission may
never be served to a reader whose gate reports a different value for either.

### Layer 2: `revl_gate::session`

Layer 2 is not a reservation. `crates/revl-gate/src/session.rs` is a real
`Session` state machine over one live composition in one process (item 334
slices 1 and 2), and a host embedding the crate reaches it. What it carries:

- an `Externs` registry the embedder declares up front, which enforces the
  item-243 pair rules AT DECLARATION TIME. `Externs::witnessed` refuses an
  inverse that is not already registered (`UNKNOWN_INVERSE`) and one that is
  not an `ExternClass::Inverse` (`INVERSE_NOT_LOCAL`); `Externs::bind` refuses
  to put an undo slot on the call surface (`INVERSE_NOT_CALLABLE`).
- `Session::call`, which resolves an effect's class from that registry rather
  than from the caller, and RUNS the host body. Class (a) fires and is escrowed
  with its checked inverse, and is escrowed even when the body reports failure,
  because a half-done effect is exactly what an owed undo is for. Class (b) does
  not fire; it is queued for `commit`. Class (c) fires only with
  `Session::approve_irreversible` and otherwise fails closed
  (`APPROVER_REQUIRED`).
- `Session::abort`, which replays the recorded inverses LIFO against the world,
  so `AbortReport::residue_free` is a MEASUREMENT rather than a report about a
  report: an inverse whose body reports failure lands in `AbortReport::residue`.
- `Session::commit`, which discharges the deferred tails and ENUMERATES the ones
  that failed in `CommitReport::deferrals_failed` rather than swallowing them.
- `Session::propose`, whose decision half runs in the design's order: halt
  dominance first, then the FORBIDDEN-GRANT name check against
  `DECIDER_SERVICES`, then the layer-1 decision.

What it does NOT carry is the part to plan around. `propose` has no ACCEPT
half: a candidate the native gate does not refuse comes back fail-closed with
`code = "NO_ADMISSION"`, never activated and never swapped in, because layer 1
issues no admission here and this tier has no runtime to activate one. There is
no WAL and no `recover`, so a crash mid-frame strands the escrow exactly as
`Session::unload` does. The approver is a boolean on the session, not a host
callback. Those are item 334's remaining slices.

So the accurate statement is no longer "the witnessed runtime half stays
py-only". It is: the rust crate has an in-process witnessed runtime, and
`revl.gate.Gate` on py remains the only tier with crash recovery, an approver
seam and an accept half. If you need any of those three, you need the py facade.

### What is still absent from the crate

`compile_to` output. `revl_gate::compile_to` is exported so its arrival is
additive, and it returns `Err(Verdict::OutsideFrontier)` unconditionally: the
self-host emitters (`selfhost/emit_py.rvl`, `selfhost/emit_rust.rvl`) still
carry `@py`-only helper externs and do not emit to rust at all, so there is no
native emitter to call. Emit with the reference `revl compile --backend <tier>`.

Two further public modules exist and are versioned apart from the gate surface,
so do not read them as part of it: `revl_gate::symbols`, the navigation surface,
which carries its own `SYMBOLS_API_VERSION` and has no py twin; and
`revl_gate::ir`, whose `check_ir_boundary` validates an IR wire against
`KNOWN_IR_FIELDS` and `KNOWN_IR_REVISIONS`.

[`examples/ecosystem-consumer-rs/`](../examples/ecosystem-consumer-rs/) is a
standalone rust project that demonstrates the verdict surface: one dependency,
four candidates covering all three arms plus a py-admitted case, a verdict cache
keyed on the full `gate_version()` triple, and exactly two decisions, `REJECT`
on a refusal and `ESCALATE` on everything else. It never calls
`issue_admission`, and so it never invents an acceptance the gate did not give.
`tests/test_gate_consumer_example_rs.py` holds that as a test.

## The wasm tier: the gate in a browser, a worker, a CDN node

There is a third dependency form, and it is the rust tier's VERDICT contract
unchanged, because it IS the rust tier: `crates/revl-gate-wasm` packages
`crates/revl-gate` as a `revl:gate@1.0.0` WASI-P2 component (roadmap item
335). Everything the section above says about the crate's verdict surface holds
here word for word: three arms, no admission among them, `frontier` is
`selfhost-admit:<hash>`, and `layer` says what was decided. The composition arm
is on the world too, as `admit-into` and `admit-into-json`, with the same
manifest wire and the same `MANIFEST` refusal for an uncovered row.

What the world does NOT export is the crate's ADMISSION surface, and that is
deliberate rather than pending: `issue_admission` leans on the crate's
`catch_unwind` fail-closed path, and on wasm the panic strategy is `abort`, so
that path does not exist here. `compile_to` output is absent for the same
reason it is absent from the crate. `admit-artifact` is exported, and declines:
the item-289 chain's `declared caps` leg is the G8 boundary projection, which
the reference derives with a whole-IR reachability walk that has no native port,
so the export returns `outside-frontier` rather than guessing the declared set.
A `no-objection` from this world is "no verdict", never a green.

What the wasm packaging adds is reach, not authority: the same verdict where
there is no Python and no native toolchain, only a wasm engine. `gate_version()`
carries a fifth field here, `tier` (`"wasm"`), so the packaging is
distinguishable from the crate it wraps.

Two things are specific to this tier, and both matter before you embed it.

**The import section is empty, and that is a checkable fact about the
artifact.** The component imports no clock, no filesystem, no random and no
host function, so a verdict is a total, deterministic function of its
arguments, provable from the binary rather than promised in prose. Nothing in
the environment can widen a verdict, because there is no channel through
which anything could. `tests/test_gate_wasm_vector.py` reads the import list
off the built artifact and requires it empty; a build that grows an import is
a red.

**A trap is not a verdict.** On every wasm target rust's panic strategy is
`abort`, so the crate's fail-closed `catch_unwind` path does not catch and a
panic inside the native gate takes the instance down instead of returning
`outside_frontier`. A trap is loud and it is not an admission, but a host
must treat it as "no verdict was reached" and fail closed on it, never as a
non-refusal.

For JavaScript hosts, `jco transpile` turns the component into a JS module
(`tools/build_gate_js.py` drives it), and the packaging step does one thing
the transpiler cannot: it narrows the emitted `admitted: boolean` back to the
literal `false`. WIT has no singleton type, so `bool` is the strongest thing
the component's world can say, and a TypeScript consumer handed `boolean`
writes `if (v.admitted) run(x)` with the type checker's blessing, on a branch
that is not reachable. After narrowing, `v.admitted === true` is a compile
error. Branch on `kind`, never on `admitted`:

```js
const v = admit(source);
if (v.kind === "refused") return REJECT;   // authoritative, final
return ESCALATE;                           // ask the reference toolchain
```

And the sentence that carries over from the design: **a verdict is a
decision, not an enforcement.** The gate returns a verdict; the browser's
loader, the worker's dispatcher or the CDN's serving path is the code that
must refuse to instantiate, execute or serve on a refusal. A gate whose
verdict nobody consults gates nothing. For an artifact you do go on to run,
item 289 gives a second, independent enforcement for free: instantiate it
with an import object shaped by the policy, and an ungranted reach is a
missing import refused by the wasm engine itself. The gate decides, the
substrate enforces, and neither trusts the other's absence.

[`examples/ecosystem-consumer-js/`](../examples/ecosystem-consumer-js/) is a
standalone project demonstrating all of it: the same four candidates, the
same two decisions, a browser page that walks both enforcement layers, and an
edge `fetch` handler whose only answers are `403 REJECT` and `202 ESCALATE`,
with no `200` arm because there is no arm that would justify one.
`tests/test_gate_consumer_example_js.py` holds that as a test.

## What is and is not deliverable today

The py surface above ships now: `pip install revl` and
`from revl.gate import ...` works today, in-process, with no subprocess and
no wire. The rust crate ships as committed source in this repo and builds
with no Python on the machine, so a rust consumer can depend on it via a path
dependency today (see the example's `README.md`); what is NOT done is the
PUBLISH step. `revl-gate` is not on crates.io, so `cargo add revl-gate` is
the shape a consumer gets once revl's release path cuts it, not a command
that works right now. The wasm tier is in the same position one step further
along: the component builds from committed crate source and transpiles to a
JS module that runs in a browser, a worker or `wasmtime` today
(`python3 tools/build_gate_js.py --out DIR`, or `npm run build` inside
`examples/ecosystem-consumer-js/`), and what is NOT done is again the PUBLISH
step. Nothing is on npm, so `npm i` the gate is the shape a consumer gets
once revl's release path cuts it, not a command that works right now. Neither
publish changes a line of the contract above; both are packaging.

The **wasm** form itself is `crates/revl-gate-wasm`, which packages the rust gate
as a WASI-P2 component (`revl:gate@1.0.0`, item 335), generated by
`tools/build_gate_wasm.py`, drift-gated by `tests/test_gate_wasm_drift.py` and
import-gated by `tests/test_gate_wasm_vector.py`. Its world exports `admit`,
`admit-json`, `admit-into`, `admit-into-json`, `admit-artifact` and
`gate-version`, and its **import list is empty**, so the verdict is a total
deterministic function of its arguments, provable from the artifact's own
import section. It carries the same no-admission asymmetry as the rust crate's
verdict surface: `no-objection` is the non-refusing arm, not an admission, and
the crate's separate admission surface is not on this world.

## Publish status: the only remaining step

All three dependency forms are built and verified from the committed source in
CI today; what none of them has yet is a copy on a public registry. That last
act is the one thing this repository deliberately does not do for itself,
because putting a version on a registry is irreversible and is the project
owner's decision, not a merge's. So the honest state of "revl as a dependency"
is: code-complete on every tier, awaiting one owner-run publish per registry.

| tier | dependency form | built + checked from source in CI | the only step that remains |
|---|---|---|---|
| py | `pip install revl`, then `from revl.gate import ...` | wheel built and installed into a fresh venv by `release dry run`, manifest-gated by `tools/check_wheel_manifest.py`, surface-gated by `tests/test_gate_compat.py` | push a `v*` tag: `publish.yml` runs the full matrix on the tag and uploads to **PyPI** by Trusted Publishing (the one-time PyPI publisher config is noted inline in `publish.yml`) |
| rust | `cargo add revl-gate` | `crates/revl-gate` regenerated and drift-gated by `tests/test_gate_crate_drift.py`; the example depends on it by path and its verdicts are gated by `tests/test_gate_consumer_example_rs.py` | `cargo publish` the crate to **crates.io** with an owner token |
| wasm / js | `npm i` the jco-transpiled gate | `crates/revl-gate-wasm` built by `tools/build_gate_wasm.py`, transpiled by `tools/build_gate_js.py`, drift/import/vector-gated by the three `test_gate_wasm_*` suites and exercised by `tests/test_gate_consumer_example_js.py` | `npm publish` the transpiled package to **npm** with an owner token |

Nothing above is a code change. Each remaining step is an owner running a
publish against a registry with a credential this repository does not hold, and
none of them alters a line of the contract stated at the top of this document:
a refusal stays authoritative and fail-closed, an admission stays a
frontier-scoped compile-time judgment, and the registry a consumer fetches from
changes only where the bytes came from, never what a verdict means.
`tests/test_gate_dependency_publish_ready.py` pins this readiness so the "only a
publish remains" claim cannot quietly rot back into a code gap.

See also: [`docs/design/338-revl-as-dependency.md`](design/338-revl-as-dependency.md)
for the full design and its adversarial review;
[`docs/stability.md`](stability.md) for what a revl version number promises
more broadly; [`src/revl/gate.py`](../src/revl/gate.py) for the module this
document is a consumer-facing translation of;
[`crates/revl-gate/src/lib.rs`](../crates/revl-gate/src/lib.rs) and
[`crates/revl-gate/src/session.rs`](../crates/revl-gate/src/session.rs) for the
native tier's.
