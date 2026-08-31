# 332: embeddable-gate API + packaging

Design note for roadmap item 332 (`docs/v2.0-roadmap.md:3904`), the foundation
of the embeddable-compiler arc (items 332-338, arc header at
`docs/v2.0-roadmap.md:3891`). The ask: ship the admission gate as a library
another program embeds, so an agent framework in py, ts, or rust calls
`gate.admit(...)` as a native function and gets the same verdict, and the same
downstream guarantees, that `revl compile` and `revl run` give today. This is
design-first. It changes no compiler code; it records what "the gate" actually
is (two gates exist, with different reach), the API surface designed once and
tier-agnostic, the packaging per tier, the embedding contract (which guarantees
hold inside a host process, which need the host to cooperate, and how the API
makes non-cooperation safe), the arc relationships that make 332 the foundation
333-338 build on, a staged plan, and exit tests an implementation agent can
pick up.

## The problem (measured)

A host program that wants an admission verdict today has three reaches, none of
them a library:

- The CLI. `revl compile` / `revl run` dispatch a whole document per invocation
  (`src/revl/run.py:1113`, tier dispatch at `run.py:1150-1172`, py boots
  in-process from `run.py:1174` onward). A framework embedding revl this way
  orchestrates subprocesses: process startup, argv plumbing, stdout parsing.
  The measured in-memory admission round-trip is **0.165 ms** per candidate
  (`bench/admission_latency.py`, item 16 at `docs/v2.0-roadmap.md:1056`,
  recorded in `bench/results/admission-latency.md` and on the conformance
  dashboard, `docs/conformance.md:102`); a subprocess pays interpreter startup
  per candidate, orders of magnitude above the decision it wraps.
- Internal host APIs, py only. `revl.compiler.compile_source`
  (`src/revl/compiler.py:243`) and `revl.mcp.session.Session`
  (`src/revl/mcp/session.py:152`) are importable and complete, and the harness
  and demos already reach them. But nothing DECLARES them: no public-surface
  contract, no versioning promise, internal exceptions on the boundary, and a
  process-global single-session binding (`src/revl/mcp/admit_bridge.py:32`).
  Item 330 (`docs/v2.0-roadmap.md:3887`) named exactly this wound for the
  in-language direction ("the admit-decision currently lives in un-declared
  Python") and fixed it for a running composition; the HOST-side twin, a
  declared surface an external program embeds, is this item.
- The gate as a bridge service (item 144, `docs/gate-as-a-service.md`). Real,
  shipped, and the right shape when the consumer is on another tier today. But
  it converts the gate into a provider that can be down, slow, or unreachable
  (`gate-as-a-service.md`, "The trade-off"), a seam round-trip per verdict
  where the decision itself costs 0.165 ms in-process.

Meanwhile the tier-portable gate already exists as pure functions:
`selfhost/compile.rvl` exports `admit(source) -> Str`
(`selfhost/compile.rvl:75`) and `compile_to(source, tier) -> Str`
(`selfhost/compile.rvl:96`), the fully-native lex-parse-check-lower-emit
pipeline, byte-exact against the reference on the covered surface
(`tests/test_selfhost_compile.py`) and run natively on py and rust (items
283/284 profiled and then fixed the rust runtime, `docs/v2.0-roadmap.md:3778`;
`docs/bench-selfhost.md`). But it is a test subject and a bench target, not a
consumable artifact: `tools/bench_selfhost_rust.py` assembles a THROWAWAY cargo
binary crate per bench run and deletes it. Nothing exists that a third program
can `cargo add` or `pip install` and call. Item 332 is the gap between "the
gate is pure functions" and "the gate is a dependency".

## Background: the two gates that exist today

The word "gate" names two implementations with different reach, and the design
must hold them apart because the API packages both.

### The reference gate (py, full language surface, plus the run half)

The decision procedure is `compile_source` / `compile_files`
(`src/revl/compiler.py:243`, `:316`). Handed sources in memory it reads no disk
and writes none (`compiler.py:248`: "Nothing is read from or written to the
disk"), takes `manifest=` for runtime admission (a candidate joins a running
composition only by passing G2/G3 over the union) and `profile=` for the
item-329 untrusted-author cut (`src/revl/admit_profile.py:49`, `:68`). A
refusal raises `RevlError` carrying `code` plus the why-trace. This purity is
load-bearing for embedding, exactly as it was for item 144: it is what made the
gate transport-safe as a service (`docs/gate-as-a-service.md`, "Why `async fn`,
not `emission`"; the transport-safety rule at `docs/interop-bridge.md:318`),
and it is what makes it embeddable as a function.

The RUN half lives on `Session` (`src/revl/mcp/session.py:152`): `load`
(`:305`) boots the composition in-process on cordis-py and creates the
item-245 `SessionOwner` (deferral queue, witnessed escrow, live-frame
registry); `call` (`:1625`) is the item-246 choke point where the approval
policy, class map, tickets and standing grants gate every crossing
(`session.py:224-268`); `commit` / `abort` (`:1352`, `:1393`) drive the 245
protocol; `admit` (`:1501`) is the item-330 per-turn crossing that applies the
329 profile, refuses non-additive turns, and returns an `AdmitVerdict`
(`session.py:33`) whose `AdmitHandle` (`:62`) routes the turn's calls into the
enclosing 245 frame. The in-language spelling is `stdlib/admit.rvl` (service at
`:24`, the one classified host body at `:34`) delegating through
`revl.mcp.admit_bridge` (`admit_bridge.py:45`). All of 243/244/245/246/322
(witnessed effects, session commit, approvals, six-tier WAL crash recovery;
`docs/v2.0-roadmap.md:3386`, `:3393`, `:3398`, `:3403`, `:3871`) hangs off this
object. Crucially, it is ALREADY in-process machinery: `revl run` on py execs
the emitted artifact into a synthetic module and boots it in the same
interpreter (`src/revl/run.py:571` onward), and the MCP server is a verb loop
around Session. "Embedding the gate in a host process" is not a new execution
model on py; it is giving the existing one a doorway with a name.

### The native gate (tier-portable, frontier-scoped)

`selfhost/compile.rvl` composes the native stages into one co-compiled
pipeline: `admit(source)` returns `""` on admission or `"<TAG>|<message>"` on
refusal; `compile_to(source, tier)` returns the emitted target source, or
`"REFUSED|<TAG>|<message>"` / `"UNKNOWN_TIER|<tier>"` control verdicts that
cannot collide with real output because emitted sources open with generated
headers (`compile.rvl:90-95` comments). `supported_tier` is `{py, rust}`
(`compile.rvl:68`). Its coverage is the `revl` column of the conformance
matrix (`docs/conformance.md`): `ok` rows are byte-identical to the reference
emitter, `lim` rows are the self-host frontier. Ground truth is fixed by the
pipeline's own header: "on the covered surface any divergence is a defect in a
stage", and the differential tests plus the matrix drift gate hold the two
gates in byte-agreement (item 328, `docs/v2.0-roadmap.md:3883`).

Native-run status, stated exactly because the packaging stages depend on it:

- `admit` runs natively on rust end to end (the `lower` stage,
  `tools/bench_selfhost_rust.py`, made viable by item 284's clone-elision:
  lexer 348.8 ms to 28.12 ms, `docs/v2.0-roadmap.md:3780`).
- `compile_to` does NOT yet run natively on rust: the emitter stages depend on
  `@py`-only externs (`py_repr` at `selfhost/emit_py.rvl:120`, `mangle`
  `:128`; `string_lit` at `selfhost/emit_rust.rvl:108`, `num_str` `:134`),
  and `docs/bench-selfhost.md` records `emit_py` as unmeasured on rust for
  exactly this reason. `json_parse` already has an `@rs` body
  (`stdlib/json.rvl:180`), so the value plumbing is not the blocker; the
  emitter helper externs are.
- The emitted rust targets cordis-rs (`backends/rust/emit.py:46`; `Any` erases
  to `cordis::Value`, `emit.py:627`). The pure gate functions never construct
  a `Context`, but they speak cordis-rs's value layer. The item text's "no
  cordis dependency, because the compiler is pure fns" is true of the RUNTIME
  (nothing boots, nothing plugs) and not yet true of the CRATE GRAPH; the
  packaging section takes this cost in the open.

## What "the gate" is, as an API: two layers, split on purity

The item's own text specifies `admit` / `compile_to` per tier. The arc's next
items need more: 333 wants an in-process admit loop, 334 wants admit PLUS the
witnessed runtime in one host. Designing only the pure surface would leave 333
and 334 to invent the stateful surface ad hoc, which is how the undeclared
`Session` reach happened the first time. So 332 fixes BOTH layers' contracts,
and ships each exactly as far as its machinery exists today:

- **Layer 1, the verdict surface.** Pure functions of their arguments: no
  disk, no clock, no live state. This is the layer that is portable to every
  tier (it is what item 144 could serve over a seam, what 335 compiles to
  wasm, what 336's binary embeds, what 337 calls at placement seams). 332
  ships it on py (reference-backed) and rust (native), designed once.
- **Layer 2, the session surface.** Stateful and address-space-bound: a live
  composition, an event loop, an owner, a WAL. This is the layer that makes
  "admit-and-RUN with the 245/246/330 guarantees" true inside a host process.
  332 ships it on py as a declared facade over the machinery that already
  exists, and fixes its tier-agnostic contract so 333 (rust/py in-process)
  and 334 (gate + witnessed runtime) implement THIS interface instead of a
  new one.

The split is principled, not phasing convenience: it is the same purity line
item 144 drew (`gate-as-a-service.md`: the compile is pure, so `Gate.admit`
could be a transport-safe `async fn`, while truc's in-process gate is
`emission fn` and address-space-bound). Guarantees divide along it too, which
is why the embedding contract below accounts per layer.

### Layer 1: the verdict surface

Designed once, spelled per tier. In host-language sketch form (py shown; the
rust twin is below under packaging):

```python
from revl.gate import admit, admit_into, compile_to, gate_version

v = admit(source)                      # frontend admission, no manifest
v = admit_into(source, manifest)       # admission INTO a running composition
out = compile_to(source, tier)         # verdict + emitted source on admission
info = gate_version()                  # {"api": ..., "language": ..., "frontier": ...}
```

- `admit(source) -> Verdict`. The frontend gate: a program the checker refuses
  cannot be compiled. On py this delegates to `compile_source` (full surface);
  on rust it IS the native `admit` (`selfhost/compile.rvl:75`).
- `admit_into(source, manifest) -> Verdict`. Runtime admission: G2/G3 span the
  running manifest, so a candidate that would collide with the live
  composition is refused with the collision's why-trace. On py this is
  `compile_source(..., manifest=...)`, the same call the item-144 service and
  truc's gatekeeper make. The native pipeline has no manifest parameter today,
  so on rust this is REFUSED as unsupported (fail closed, a control verdict
  naming the gap), and growing it natively is a named stage, not an implied
  one.
- `compile_to(source, tier) -> Emit`. Verdict plus target source. `Emit` is
  `{verdict, output}` so a refusal and an emission cannot be confused by
  string sniffing; the selfhost header-prefix disambiguation stays internal.
- `gate_version()`. The versioning surface, below.

Two ABI rules, both load-bearing:

1. **Strings in, structured strings out.** The public boundary carries `Str`
   and flat structs/JSON of `Str`; no `Any`, no host object graph.
   `tools/bench_selfhost_rust.py` records why: `Any` erases to
   `cordis::Value` on rust and there is no host-side constructor to feed one,
   which is exactly the kind of boundary that cannot cross a crate ABI or,
   later, a wasm-component boundary (335). The internal pipeline keeps its
   value layer; the boundary never shows it.
2. **The Verdict is structured; the message is verbatim.** The one shape
   designed tier-agnostically:

   ```json
   {"admitted": false, "code": "G2", "message": "<the reference refusal, verbatim>"}
   ```

   This is `AdmitVerdict.as_dict` (`session.py:33`) minus the run-half fields,
   and the item-144 precedent ("`diagnostic` carries the compiler's why-trace
   VERBATIM, the same string `revl compile` prints"). The native gate's
   stringly `"<TAG>|<message>"` protocol stays the internal form; each
   package's wrapper splits it at the first `|` into the structure and never
   rewrites the message, because the self-host discipline is byte-agreement on
   diagnostics (the lower/emit oracles compare messages and bytes, and a new
   diagnostic shape lands behind a new key, never by mutating an existing
   one).

And one safety rule that makes the native gate shippable as a SECURITY
decision: **fail closed at the frontier**. The two divergence directions are
not symmetric. A native gate that refuses a program the reference admits is an
inconvenience; a native gate that ADMITS a program the reference refuses is
the defect class this arc exists to prevent (an admission gate that waves
through). The packaged native gate therefore ships only with the differential
corpus green as a release gate (the `tests/test_selfhost_compile.py` +
conformance-matrix agreement), refuses out-of-surface constructs loudly (the
native front end already refuses what it cannot parse or check; nothing may
soften that into a wave-through), and any reference-side admission change
lands in the self-host gate in the same wave or the package release is held.
This is discipline plus evidence, not proof; the honest-hard-part section says
so in so many words.

### Layer 2: the session surface

The py facade, sketched:

```python
from revl.gate import Gate

gate = Gate(wal_path=..., approval_policy="auto", approver=my_prompt_fn)
gate.load(sources, config=...)          # boot the base composition (admission is load)
v = gate.admit(turn_source, granted=["fs", "http"])   # the item-330 crossing
if v.admitted:
    result = v.handle.call("turn", "run", [args])     # rides the 245 frame, gated by 246
gate.commit()                            # or gate.abort(): revert residue-free
```

This is deliberately NOT a new machine. Every operation delegates to the
Session member that already implements it (`load` `:305`, `admit` `:1501`,
`call` `:1625`, `commit` `:1352`, `abort` `:1393`, `unload` `:1323`), so
behavioral identity with `revl run` and `revl mcp serve` is definitional, and
the exit tests pin it by driving the same fixtures through both doors. What
the facade ADDS over raw Session reach:

- a declared, versioned public name (`revl.gate`), with everything else in
  `revl.mcp.*` / `revl.compiler` remaining internal and unpromised;
- constructor-time embedding knobs that today are scattered session attributes
  (`_wal_path` `:278`, `approval_policy` `:224`), plus the `approver` seam the
  embedding contract requires (below);
- boundary exceptions: the facade raises/returns `revl.gate` types only, never
  internal `SessionError`/`RevlError` classes, so the host's compatibility
  surface is the facade, not the internals;
- the single-gate refusal: a second live `Gate` in one process is refused
  loudly at construction (the honest spelling of today's process-global
  `admit_bridge._SESSION` bind at `admit_bridge.py:32` and the process-global
  session owner, `session.py:457`), never a silent misbind. Lifting the
  restriction is real owner-scoping work, out of scope here and not needed by
  the arc (337's mesh runs one gate per tier process).

The tier-agnostic contract fixed here for 333/334: the operation set
`{load, admit, call, commit, abort, unload}`, the `Verdict` and `Handle`
shapes, and the guarantee-accounting table below. The rust crate does not
implement layer 2 in this item (its runtime half is precisely item 334's
deliverable); it reserves the module so the addition is additive.

### Versioning (designed once, per the item text)

`gate_version()` returns three values a host can branch on:

- `api`: semver of the gate surface itself. Bumped by surface changes only.
- `language`: the revl language/package version the gate admits (the wheel and
  crate track the repo version, `pyproject.toml` `version = "2.0.0"`; a
  generated crate is stamped with the sha it was generated from).
- `frontier`: an identifier of the native gate's covered surface (the
  conformance `revl`-column row set / corpus pin), so an embedder, and later
  337's seam re-admission, can detect that two gates cover different surfaces
  before trusting their agreement.

Stability split, stated because hosts will get it wrong otherwise: `admitted`
and `code` are API (codes are append-only; an existing code never changes
meaning); `message` text is NOT API (why-traces improve; the verbatim rule
above is about reference/native agreement at one version, not about text
stability across versions).

## Packaging per tier

### py: the wheel already carries the gate; what ships is the contract

`pip install revl` already installs the full reference gate, the run half, all
six backends and the stdlib inside one dependency-free wheel
(`pyproject.toml`: no `[project] dependencies`; `backends/` and `stdlib/` ride
in via force-include). The site playground vendors exactly this wheel into the
browser today (`site/vendor/revl-2.0.0-py3-none-any.whl`). So the py packaging
deliverable is not bytes, it is the DECLARED surface: the `revl.gate` module
(both layers), its docs, and an import-surface test that pins the public
names.

One honest call the item text forces: the roadmap says "emit `selfhost/` as
a... Python package", and this design deliberately does NOT ship the
selfhost-emitted-to-py pipeline as the py engine. On py the native pipeline
adds no reach the reference lacks, is 2-5x slower per stage
(`docs/bench-selfhost.md`, baseline table), and covers less surface (the `lim`
frontier). Its py emission remains what it is today, the conformance
cross-check that keeps the two gates in byte-agreement. What the arc needs
from py packaging is a stable surface over the FULL gate, and that is what
this ships; the tier where "emit selfhost as the package" is literally the
deliverable is rust, where no reference exists to prefer.

### rust: `revl-gate`, the new artifact

The crate mechanizes what `tools/bench_selfhost_rust.py` already does by hand
per bench run, minus the throwaway:

1. A build tool (`tools/build_gate_crate.py`) compiles the selfhost pipeline
   (`selfhost/compile.rvl` and its `use` closure) to rust through the
   reference rust emitter and writes a library crate: the emitted module,
   plus a hand-written `lib.rs` shim exporting the layer-1 surface over it:

   ```rust
   pub struct Verdict { pub admitted: bool, pub code: Option<String>, pub message: Option<String> }
   pub fn admit(source: &str) -> Verdict
   pub fn compile_to(source: &str, tier: Tier) -> Result<String, Verdict>   // staged, see below
   pub fn gate_version() -> GateVersion
   ```

   The shim parses the internal `"<TAG>|<message>"` protocol into `Verdict`
   at the boundary, message verbatim.
2. The generated source is COMMITTED, not generated at install: the crate must
   build with no Python on the machine, or 336 (a single rust binary shipping
   the actual compiler) and 338 (`cargo add revl`) do not exist. Drift is
   killed the way the conformance matrix kills it (`docs/conformance.md`:
   regenerated in place, CI fails if the committed block differs from a fresh
   generation): CI regenerates the crate from the same sha and fails on any
   byte difference.
3. Dependency accounting. The emitted rust speaks cordis-rs's value layer
   (`backends/rust/emit.py:46`, `:627`), so the crate depends on the published
   cordis-rs crate. The gate's exported functions never construct a
   `Context`, never plug, never boot: the item text's "no cordis dependency"
   is delivered as "no cordis RUNTIME participates in a verdict", and the
   crate graph carries cordis-rs for `Value` and the persistent collections.
   Vendoring a value-layer subset to cut the dependency is recorded as a 336
   option, not done here: cordis-rs is upstream (the fork+PR discipline), and
   duplicating its value layer into this repo is a maintenance surface this
   item does not need. The trivial component wrapper the selfhost file
   carries (`selfhost/compile.rvl:110-118`) emits along with everything else
   and is simply never booted; excluding it would buy nothing but a special
   emit mode.
4. Staging inside the crate: v0 exports `admit` only, because `admit` is the
   half that runs natively today. `compile_to` joins when the emitter helper
   externs gain `@rs` bodies (`py_repr`, `mangle`, `snake`, `pascal`,
   `string_lit`, `num_str`; `selfhost/emit_py.rvl:120-143`,
   `selfhost/emit_rust.rvl:108-167`), each gated on byte-exact agreement.
   These are small pure string helpers with one honest exception: `py_repr`
   is CPython `repr` semantics, and its `@rs` body must be byte-exact only
   over the value shapes the emitter actually feeds it (strings and numbers
   in IR positions), a scoped claim the exit test pins, never "repr parity".
   Until then, `compile_to` on the crate returns the unsupported control
   verdict, fail closed.
5. The crate's release gate is the differential corpus: the roadmap exit test
   (a standalone binary depending only on the published crate agrees with
   `revl compile` verdicts, and `compile_to` output is byte-identical on the
   covered corpus) runs in CI behind the same toolchain-honesty gate the rust
   tier already uses (skip WITH A REASON when cargo/cordis-rs are absent,
   never a green that built nothing; the `tools/bench_selfhost_rust.py`
   discipline).

### npm / wasm-component: reserved, not shipped here

The item text says "npm / wasm-component later" and this design keeps it
that way. What 332 owes them is only that the fixed API is expressible there,
and it is by construction: the boundary is strings and JSON, the ts emission
of the pipeline exists (`selfhost/emit_ts.rvl`), and the packaging pattern
(generate, commit, drift-gate, thin shim) transfers. The wasm-component gate
is item 335's deliverable outright, with the playground wheel already the
first step in that direction per the roadmap item.

## The embedding contract

The question 332 must answer precisely: what does a host give up by running
revl-checked code INSIDE its process instead of under `revl run`? The answer,
guarantee class by guarantee class:

**Admission verdicts (the whole compile-time family, plus the 329 profile):
preserved identically, no host cooperation needed.** The decision is a pure
function of its arguments; the host's cwd, env, and filesystem cannot change a
verdict because the layer-1 gate consults none of them (`compiler.py:248`).
On rust the same holds over the covered surface with the fail-closed rule off
it. A host that wants disk-sourced compilation opts into `compile_files`
knowingly; the embedded default is in-memory sources, the shape agent loops
already use (`compiler.py:250-254`).

**Classification, approvals (246), witnessed effects and commit/abort
(243/244/245), WAL recovery (322): preserved in-process, because they ride the
choke points that ship WITH the library.** These were never properties of the
`revl` process boundary; `revl run` on py is in-process already. They are
properties of the crossings: every call routed through `Session.call` hits the
class map and approval machinery (`session.py:1625`, `:224-268`); every
witnessed crossing registers into the owner's frame registry and reverts on
abort; the WAL makes the spend and the residue durable. The facade's one
structural rule keeps the choke points unavoidable for admitted code: **a
verdict hands back a Handle, never the artifact.** The emitted module, the
driver, and the fibers are not exported; `AdmitHandle` (`session.py:62`)
refuses keys the turn does not provide, and its calls join the enclosing
frame (`:1568`).

**What the gate does not and cannot promise: confinement of the HOST.** A
library cannot jail its own process; a host can reach around any facade with
enough intent. The contract states this in G8's own vocabulary rather than
papering it: revl's guarantees govern the ADMITTED code, which under the 329
profile carries no host code and reaches only granted keys, and the audit
surface enumerates every crossing. The host already holds full process
authority and needs no smuggling; embedding changes its position not at all
(the CLI's "host" was a shell with the same authority). What embedding must
not do is WEAKEN the admitted code's guarantees, and by the accounting above
it does not. OS-level isolation, resource limits, and physical confinement of
extern bodies were never this layer's job on any path; they are the wasm tier
(335) and the quarantine machinery.

**Where the host must cooperate, and how the API makes refusing to cooperate
safe rather than silently wrong:**

- **Approval prompts.** Standalone, a class-(c) crossing prompts the operator
  through the MCP ticket two-step. Embedded, there is no operator channel
  unless the host provides one, so `Gate(approver=...)` takes a callback that
  receives the ticket (capability, component, reach-closure hash) and returns
  the decision. The default with a policy on and NO approver is refuse
  class-(c), fail closed, exactly the shape the ownerless tiers already use
  (245 Slice 2's refuse-at-emit): an uncooperative host gets refusals it can
  see, never silent auto-approval.
- **Commit or abort before exit.** The 245 protocol needs the host to end the
  session; a host that exits mid-session leaves the WAL as the recovery
  story, which is why `Gate(wal_path=...)` is a constructor knob and the
  package exports `recover` (`src/revl/recovery.py:164`) so the embedder can
  replay inverses at next boot. Documented obligation, mechanized fallback.
- **Event-loop ownership.** `Session` drives a private asyncio loop
  (`session.py`, `_run` via `run_until_complete`), and the layer-2 facade v1
  is therefore a SYNCHRONOUS surface that owns its loop. Most agent
  frameworks are async; calling a loop-owning facade from a coroutine is a
  real ergonomic wall, and the design names it instead of hiding it. The
  machinery is already re-entrancy-aware in the one place it had to be (a
  turn admitted while a call drives the loop is queued and wired when the
  call returns, `session.py:285`, `:1560-1567`), so a host-loop async facade
  is a bounded follow-on, staged below, not designed here.
- **One gate per process** (v1). Refused loudly on the second construction,
  per the layer-2 section.

**Additivity, the other half of the contract.** Standalone compositions are
untouched: the facade is a new doorway into existing machinery, `revl run`,
`revl test`, `revl mcp serve` and every golden stay byte-identical, and a
program that never imports `revl.gate` cannot observe that it exists.

## Relationship to the arc, 363, and the distribution model

- **333 (in-process gate for agent frameworks)** consumes this item directly:
  layer 1 gives the inline `admit` at native cost (the ~0.165 ms/candidate
  the item quotes is the reference in-memory round-trip already measured),
  layer 2 gives the py loop `admit -> call -> commit`. 333's exit (a rust/py
  harness embeds the gate, verdicts match the CLI over a batch) is a measured
  corollary of the behavior-identity and crate exit tests here; what 333 adds
  is the framework-side example and the round-trip numbers.
- **334 (self-extending runtime)** is layer 2 on a native tier: gate plus
  witnessed runtime in one process, accept-and-revert live. 332 hands it the
  contract (operation set, Verdict/Handle shapes, the accounting table) so
  the headline demo implements this interface; 332 deliberately does not
  build any of 334's runtime half.
- **335 (wasm gate)** compiles layer 1 to wasm. The packaging pattern
  (generate, commit, drift-gate) and the string ABI are what carry over; the
  wasm-component's export list is layer 1 verbatim.
- **336 (native single-binary tooling)** takes `revl-gate` as its front-end
  dependency; the committed-generated-source decision (build with no Python
  anywhere) exists for 336's sake as much as 338's.
- **337 (polyglot admission mesh)** is layer 1 called at every placement
  seam: the receiving tier re-admits before accepting. Two things fixed here
  are its prerequisites: the Verdict serializes (a seam refusal must cross
  the wire with its why-trace intact), and `gate_version().frontier` lets a
  seam detect gates with different covered surfaces before trusting their
  agreement. The version-skew problem embedding creates (N gates in a fleet
  can disagree by version, where the item-144 service had one gate by
  construction) is 337's to police at the seam; 332's obligation is the
  version surface that makes skew detectable, delivered above.
- **338 (revl as a dependency)** is "publish what 332 built, plus a real
  external consumer"; the api semver split is what lets 338 happen without
  churning every consumer.
- **363 (per-component tier placement)** creates the seams 337 re-admits at:
  its conductor already gives every process a tier and wires proxies across
  them (`docs/design/363-per-component-tier-placement.md`). The gate is the
  seam's checkpoint: 363 decides WHERE code lands, 332 makes the admission
  decision available AT the landing point on that tier, 337 makes the seam
  actually consult it.
- **The distribution model** (`docs/distribution-model.md`) is unchanged and
  load-bearing: nothing here adds infrastructure or multiplicity (its §4
  non-goals), and the registry's "install is admission" framing generalizes
  through this item to "arrival is admission" wherever a gate is embedded.
  The item-144 service remains the right shape when the consumer tier has no
  native gate yet; embedding inverts its trade-off (no live provider, no
  seam deadline, no round-trip) wherever the gate exists as a library, which
  is exactly the arc's direction of travel.

## Staged implementation plan

Each stage lands independently; every existing golden stays byte-identical
throughout (the 342/388/396 additivity discipline).

- **Stage 1 (py facade, layer 2 + layer 1).** Add `revl.gate`: `admit` /
  `admit_into` / `compile_to` / `gate_version` over `compile_source`, and
  `Gate` / `Verdict` / `Handle` over `Session`, with the constructor knobs
  (`wal_path`, `approval_policy`, `approver`), boundary exception types, and
  the single-gate refusal. No behavior change anywhere. Exit: the headline py
  embed test below; an import-surface test pins the public names; full suite
  byte-identical.
- **Stage 2 (verdict unification).** The structured Verdict over both
  engines; the `"<TAG>|"` parser as a shared helper; the code catalog
  documented append-only; `gate_version()` wired to the package version and
  the conformance-corpus pin. Exit: on the differential refusal corpus, the
  reference and native gates produce equal `{admitted, code, message}`
  through the same public shape.
- **Stage 3 (the crate, admit-only).** `tools/build_gate_crate.py`; the
  committed generated crate (location decision at implementation: a
  `crates/revl-gate/` top level mirrors how `forks/` and `backends/` sit);
  `lib.rs` shim; CI drift regeneration gate; the toolchain-honesty skip.
  Exit: the roadmap's own exit test, admit half: a standalone rust binary
  depending only on the crate returns the same verdict as `revl compile`
  across the corpus, with no Python installed.
- **Stage 4 (crate `compile_to`).** `@rs` bodies for the emitter helper
  externs, `py_repr` under its scoped byte-exactness claim; export
  `compile_to`; extend the differential gate to emitted bytes. Exit:
  `compile_to` from the standalone binary is byte-identical to
  `revl compile --backend py|rust` on the covered corpus; until this stage,
  the crate's `compile_to` refuses with the unsupported control verdict.
- **Stage 5 (embedding-contract hardening).** The approver callback path end
  to end; the fail-closed class-(c) default proven; `recover` exported and
  documented for embedders; the second-Gate refusal message. Exit: the
  approval and crash exit tests below.
- **Stage 6 (docs + arc handoff).** `docs/commands-reference.md` /
  `docs/mcp-bridge.md` cross-links; the layer-2 contract written where 333
  and 334 will implement it; this note's sketches promoted as they land.
  Deferred and named, not implied: the async host-loop facade;
  multi-gate-per-process owner scoping; native `admit_into`; npm and
  wasm-component packaging (335's lane).

## Exit tests

- **The py embed (this item's headline for the arc).** A host Python program
  that imports ONLY `revl.gate` loads a base composition, admits a per-turn
  source against a granted set, calls the admitted tool through the handle,
  and commits. Assertions: the verdict equals `revl compile`'s for the same
  source (message verbatim); a refused turn leaves the running composition
  untouched; a class-(b) crossing enumerates at commit and a class-(c)
  crossing with no approver refuses, matching the same fixture driven
  through `revl mcp serve` decision for decision; on abort, the witnessed
  crossing reverts residue-free with the same WAL trace the standalone run
  produces.
- **The crate (the roadmap's own exit).** A standalone rust binary depending
  only on the published/committed crate calls `admit` over the differential
  corpus and agrees with `revl compile` on every verdict, on a machine with
  no Python; after stage 4, `compile_to` output is byte-identical to the
  reference on the covered corpus.
- **Fail closed at the frontier.** A construct outside the native covered
  surface is REFUSED by the crate gate with a control verdict; no corpus
  extension may land a case where the native gate admits and the reference
  refuses (the release-blocking direction).
- **Verdict shape agreement.** Reference and native refusals of the same
  programs produce equal `{admitted, code, message}` through the public
  shape; codes in the catalog are append-only across a release.
- **Additivity.** The full suite, every backend golden, and `revl
  run`/`test`/`mcp` behavior are byte-identical with the facade present and
  unused; a second `Gate` construction in one process is refused with the
  documented message.
- **Packaging discipline.** The wheel's public import surface matches the
  documented names; regenerating the crate from the same sha is
  byte-identical (CI drift gate); the crate build skips with a stated reason,
  never a hollow green, where the rust toolchain is absent.
- **Embedder crash story.** A host process killed mid-session leaves a WAL
  from which the exported `recover` replays inverses to a clean tree at next
  boot (the item-322 machinery, driven through the public surface).
- **`test_doc_examples` stays green**: every proposed-surface block in this
  note is host-language or `sketch`-marked and must not compile until the
  feature lands.

## The honest hard part (consolidated)

Four costs are real and taken in the open. First, the gate cannot confine its
host: embedding is the host trusting revl's judgment about admitted code, not
revl bounding the host, and every guarantee claim above is scoped to the
admitted side of that line; a design that implied more would be selling a
sandbox this layer has never been. Second, the native gate's value is bounded
by the self-host coverage frontier, and its safety by a discipline rather
than a proof: byte-agreement on the differential corpus is evidence, the
fail-closed rule bounds the damage of the unknown, and the false-admit
direction remains the defect class a stage bug could open, which is why the
corpus gate is a release blocker and not a nightly. Third, the crate's
purity is runtime-purity, not graph-purity: cordis-rs rides along for the
value layer the emitted code speaks, `compile_to` waits on `@rs` bodies for a
handful of emitter externs of which `py_repr` carries CPython `repr`
semantics that can only ever be matched over the value shapes the emitter
actually produces, and cutting the cordis-rs dependency entirely is 336's
call to make, not a free lunch here. Fourth, the layer-2 surface v1 is
synchronous and single-gate in a world of async, multi-tenant hosts; the
loop-ownership and owner-scoping walls are named, staged, and deliberately
not solved in the foundation item, because 333 and 334, the first real
consumers, are where those walls will be measured instead of guessed.
