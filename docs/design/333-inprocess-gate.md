# 333: in-process admission gate for agent frameworks (rust/py)

## The claim, in one sentence

An agent tool-generation loop written in py (and, when 332's rust crate lands,
in rust) imports the revl admission gate as a native function and admits every
component it proposes IN ITS OWN PROCESS, before that component can run, at a
per-candidate cost measured in tenths of a millisecond, with no `revl mcp serve`
subprocess, no IPC, and no wire between the loop and the compiler. The verdict
the loop gets in-process is the SAME verdict the reference `revl` admission path
gives; that identity is the whole product claim and the whole security claim,
and this design pins how it is guaranteed and how it is measured.

This item is almost entirely a PACKAGING-AND-PROOF item, not a new-engine item.
The in-process gate already exists as shipping code (item 332, `src/revl/gate.py`
on py). Item 16 already measured the in-memory round-trip at a median 0.165 ms.
What 333 owes is: (1) a precise statement of the API surface an agent framework
embeds and the exact match guarantee it relies on, (2) a runnable example
harness in each tier that admits/rejects a BATCH of proposed components in-process
and proves each verdict equals the CLI's, and (3) an honest cost measurement over
realistic candidate and manifest sizes rather than a single reasserted headline.

## Where this sits in the arc

- **332 (◑ partial)** ships the embeddable gate API and its packaging contract.
  py is landed; the rust crate, npm/wasm, and async multi-gate are deferred. 333
  EMBEDS what 332 exposes. This is a hard dependency and the section "The 332
  dependency" states exactly what is available versus blocked, per tier.
- **334** is the next step: a running host that embeds BOTH this gate AND the
  witnessed-effect runtime, hot-swaps an admitted component AT RUNTIME, and
  reverts residue-free on abort. 333 stops at the COMPILE-TIME admission decision;
  it does not run, sandbox, or revert the admitted code. The section "Honest
  boundary" draws that line precisely, because conflating the two is the most
  tempting overclaim in the whole arc.
- **50 (token economy)** and **docs/mcp-bridge.md** are the reason the in-process
  path matters: the alternative is the MCP bridge, where every candidate costs a
  JSON-RPC round-trip of schema and result tokens plus a process hop. In-process
  admission is the zero-token, zero-hop floor that 50's "one intent, one call"
  audit is chasing. 333 is that floor made concrete for an embedder.

## Background: the two verdict paths that exist today, and which one is "the CLI"

There are three reference surfaces that make an admission decision, and getting
the oracle right depends on distinguishing them. This matters because the item
text says the harness must match "the CLI verdicts", and a naive reading picks
the WRONG CLI verb.

1. **`revl compile <file>`** (`src/revl/__main__.py`, the `compile` command)
   calls `compile_files(files)` with NO manifest. It COMPILES and returns the IR.
   Crucially it does NOT call `refuse_admission`: a draft with an open hole
   compiles fine (holes are printed to stderr as a draft warning, exit 0). So
   `revl compile` is a CHECK verb, not an ADMIT verb.

2. **`revl run`** (`src/revl/run.py:1122`, `:1194`) compiles AND calls
   `refuse_admission(ir)` before it lets anything reach a live generation. This is
   the ADMISSION verdict: a draft-with-holes is REFUSED here.

3. **The MCP `revl_admit` verb** (`src/revl/mcp/server.py`, delegating to
   `Session.admit`, `src/revl/mcp/session.py:1738`) admits a per-turn source
   INTO a running composition under the item-329 untrusted-author profile:
   `compile_source(source, manifest=self.ir, profile=untrusted_author(granted))`.
   This is the admit-against-a-running-composition verdict, with an allowlist and
   the additive-only rule (a turn may not swap a running component).

The in-process gate's layer-1 `admit(source)` is `compile_source(source)` FOLLOWED
BY `refuse_admission(document)` (`src/revl/gate.py:180-181`). So `admit` is the
admission verdict of path (2)/(3), NOT the compile check of path (1). The oracle
the harness compares against must therefore be an ADMISSION subprocess, not
`revl compile`. This is stated up front because it is also the sharpest thing the
adversarial review finds an implementer getting wrong (see CRITICAL below).

## 1. The in-process API surface

### py: the surface an agent framework imports

The framework does `from revl import gate` (or `from revl.gate import ...`) and
uses one of two shapes depending on whether it is checking a candidate in
isolation or admitting it into a live composition.

**Shape A, the stateless per-candidate verdict (layer 1).** The pure verdict
surface already shipped in 332:

```revl sketch
# py call shapes, not revl source
verdict = gate.admit(source)                     # -> Verdict{admitted, code, message}
verdict = gate.admit_into(source, running_ir)    # admit against a running manifest
emit    = gate.compile_to(source, "rust")        # -> Emit{verdict, output}
info    = gate.gate_version()                     # -> {api, language, frontier}
```

`admit(source)` is disk-pure and clock-pure: strings in, a structured `Verdict`
out, no host object graph on the boundary. `admit_into(source, manifest)` is the
against-a-running-composition verdict, keyed by the previously-compiled IR the
agent holds in memory across generations. This is the exact call the item-16
bench times, and the shape a tool-generation loop uses when it just wants a
yes/no plus a repair message per candidate and does not need to keep the
candidate live.

**Shape B, the stateful session (layer 2).** When the loop wants to actually
LOAD a base composition and admit per-turn tools into it (the item-330 crossing,
untrusted-author profile, granted allowlist, additive-only), it drives a `Gate`:

```revl sketch
# py call shapes, not revl source
g = gate.Gate(approval_policy="auto", approver=ask_human)
g.load(base_source)
result = g.admit(tool_source, granted=["store"])   # -> AdmitResult{admitted, handle, ...}
if result.admitted:
    result.handle.call("cache", "lookup", ["k"])    # gated by the 246 approver seam
g.commit()   # or g.abort() to revert residue-free
```

333 adds NO new py entry point. Shape A and Shape B are both already exported by
`revl.gate` (`__all__` in `src/revl/gate.py:50`). 333's py deliverable is the
example harness that USES them as an agent framework would, the batch-vs-CLI
match proof, and the cost measurement. Stating this plainly keeps 333 from
accidentally re-designing 332.

### rust: the self-host front end embedded

The rust in-process gate is the `revl-gate` crate that 332 DESIGNED but has not
yet shipped. Its layer-1 surface (from 332's rust packaging section):

```rust
// sketch (from docs/design/332, rust: revl-gate) - fragment
pub struct Verdict { pub admitted: bool, pub code: Option<String>, pub message: Option<String> }
pub fn admit(source: &str) -> Verdict;
pub fn gate_version() -> GateVersion;
// compile_to and admit_into are STAGED / unsupported on the crate today (see below)
```

`admit` on rust is the native self-host pipeline `selfhost/compile.rvl`
(lexer -> parser -> checker -> lower-admit), cross-compiled to rust through the
reference rust emitter and committed as a library crate. It runs natively on rust
end to end today (332 background: the `lower` stage was made viable by item 284's
clone-elision). The crate's shim parses the internal `"<TAG>|<message>"` protocol
into the same `{admitted, code, message}` shape py uses (`Verdict.from_native`,
`src/revl/gate.py:114`), so a rust embedder gets the identical structured verdict.

The rust embed is DEFERRED in this design's Slice 1, because the crate it embeds
does not exist yet. See the 332 dependency section for the precise blocker.

### The match guarantee (the load-bearing invariant)

The in-process verdict MUST equal the reference admission verdict, per candidate,
per tier. The guarantee has two halves that are proved differently:

- **Same-engine identity (py).** `revl.gate.admit` does not reimplement anything;
  it CALLS `compile_source` and `refuse_admission`, the same functions `revl run`
  loads through and the same `Session.admit` (`revl_admit`) admits through. There
  is one engine and several doorways onto it. Identity here is definitional, and
  `tests/test_gate_surface.py` already pins it against the in-process reference
  functions (including the hole-draft case: `compile_source` alone ACCEPTS a
  draft-with-holes, `admit`/`admit_into`/`revl run` all REFUSE it, verbatim). 333
  extends this from "matches the in-process reference function" to "matches the
  reference CLI SUBPROCESS on a batch", which is a stronger, end-to-end check
  because it also catches divergence introduced by process-global state (see the
  adversarial review).

- **Differential agreement (rust, when it lands).** rust `admit` is a SEPARATE
  engine (the self-host front end), so identity is not definitional; it is a
  DIFFERENTIAL claim over a corpus, and it must fail closed: a rust gate that
  REFUSES what py admits is an inconvenience, but a rust gate that ADMITS what py
  REFUSES is a security hole. 332's crate release gate already fixes this rule
  ("fail closed at the frontier"); 333's rust harness inherits it and adds nothing
  weaker.

The `Verdict` message is the reference compiler's diagnostic VERBATIM (`admitted`
and `code` are the stable API; `message` text is not promised stable across
versions). So a harness that logs the refusal message logs exactly what a human
running the CLI would see, which is what makes the refusal a usable repair signal.

## 2. The 332 dependency: exactly what is available versus blocked

333 embeds 332. 332 is `◑` (partial). Stated per tier and per entry point:

| entry point            | py (embed target for Slice 1) | rust (deferred)                                   |
|------------------------|-------------------------------|---------------------------------------------------|
| `admit(source)`        | LANDED, `gate.py:160`         | DESIGNED, crate unbuilt; runs natively today      |
| `admit_into(src, mfst)`| LANDED, `gate.py:187`         | NO native manifest pipeline; refused fail-closed  |
| `compile_to(src, tier)`| LANDED, `gate.py:203`         | BLOCKED on `@rs` emitter-helper externs           |
| `Gate` session facade  | LANDED, `gate.py:371`         | out of scope for the crate (layer 2 is py-bound)  |
| `gate_version()`       | LANDED, `gate.py:260`         | DESIGNED with the crate                           |

**What 332 must expose for an in-process embed, and what it does expose (py):**
a stable, importable, side-effect-free entry point that returns a structured
verdict. 332 delivers exactly this on py: `revl.gate.admit`/`admit_into` are pure
functions with a declared `__all__`, a versioned surface (`gate_version().api ==
"1.0.0"`), and a structured `Verdict` whose `{admitted, code}` are API. Nothing
is missing on py for Slice 1. This is why Slice 1 is py-only and unblocked.

**What is missing/blocked for the rust embed (the named blocker):** the
`revl-gate` crate itself. 332 designed it but explicitly deferred building it
("Deferred: rust revl-gate crate"). Concretely, three things do not exist yet and
gate the rust half of 333:

1. `tools/build_gate_crate.py`, the build tool that compiles `selfhost/compile.rvl`
   and its `use` closure to a committed rust library crate plus a `lib.rs` shim.
   Today only `tools/bench_selfhost_rust.py` does this by hand, per bench run, as
   a THROWAWAY crate it deletes. Nothing produces a consumable crate.
2. Native `admit_into` (against a running manifest) on rust. The native pipeline
   has NO manifest parameter (`selfhost/compile.rvl`), so 332 specifies that on
   rust `admit_into` returns the unsupported control verdict, fail closed. An
   agent loop's realistic shape is admit-against-a-running-composition, so a rust
   embed that only offers standalone `admit` cannot yet run the same loop the py
   embed runs. This is a real capability gap, not a packaging detail.
3. `compile_to` on rust, blocked on the emitter-helper externs (`py_repr`,
   `mangle`, `snake`, `pascal`, `string_lit`, `num_str`) gaining byte-exact `@rs`
   bodies. 333 does not need `compile_to` (admission is a verdict, not an
   emission), so this blocker is noted but not on 333's critical path.

**Decision:** 333's rust embed is gated on 332 shipping the `revl-gate` crate v0
(the `admit`-only cut). Until that lands, Slice 1 delivers the py embed in full
and the design records the rust embed as "ready to build the day the crate
exists", with the harness written tier-portably so the rust harness is the same
code shape against the crate's `admit`. Do not build a bespoke rust admission
path for 333; that would duplicate the crate 332 owns.

## 3. The example harness + cost measurement

### What the harness is

A standalone program, in the agent-framework role, that:

1. holds a compiled base composition in memory (compiled once, outside the loop,
   as a real agent does across generations);
2. receives a BATCH of proposed components (a mix that must admit and must
   reject, so both verdict directions are exercised);
3. for each candidate, calls the in-process gate (`admit_into` against the held
   manifest for the running-composition case, `admit` for the standalone case)
   and records `{admitted, code}`;
4. for each candidate, independently runs the reference CLI as a SUBPROCESS on the
   same source and records its verdict;
5. asserts the in-process verdict equals the CLI verdict for every candidate;
6. times step (3) across the batch and reports the distribution.

The py harness lives at `bench/inprocess_gate_harness.py` (harness + batch +
match check + timing), reusing `bench/admission_latency.py`'s scenario and
`measure()` shape so the number is comparable to the item-16 headline. The rust
harness (deferred) is the same program against the `revl-gate` crate.

### The CLI oracle (the precise part)

The oracle subprocess must be the ADMISSION verb, not `revl compile`. Two honest
options, pick per candidate kind:

- **Standalone candidate** (`admit`): the oracle is a subprocess that performs the
  IDENTICAL two-call sequence `compile_source(src)` then `refuse_admission(doc)`,
  reachable today via `python -m revl run --check`-class admission or a tiny
  documented `python -c` shim over `revl.gate.admit`. `revl compile` is NOT valid
  here: it skips `refuse_admission`, so it would report a draft-with-holes as
  admitted while the in-process `admit` correctly refuses it, a FALSE mismatch
  that is really the oracle being wrong.
- **Against-a-running-composition candidate** (`admit_into`): the oracle is the
  reference admitting the candidate against the SAME manifest, e.g. the base and
  candidate handed together (`revl compile base.rvl cand.rvl` co-roots, or a
  manifest-fed admission), because a candidate that `requires` a running `Store`
  REFUSES standalone but ADMITS against the manifest. Comparing an `admit_into`
  verdict to a manifest-less CLI compile is comparing two different questions.

The design's rule: the oracle and the in-process call must ask the IDENTICAL
question (same source, same manifest, same profile). The harness constructs the
oracle invocation from the same inputs it hands the in-process call, so they
cannot silently diverge on which question is being asked.

### The cost measurement (honest, not reasserted)

Item 16 measured `admit_into`-shape (`compile_source(candidate, manifest=running)`)
at median **0.165 ms**, p90 0.179 ms, p99 0.659 ms over 4000 iterations on an
Apple M1 Max / py3.14, with the gate's own share (link + `refuse_admission`) about
**0.015 ms** over compiling the candidate alone. That number is real and it is the
per-candidate floor an agent loop pays in-process.

333 must NOT simply reprint 0.165 ms as if it were universal. The number is a
function of two sizes the item-16 scenario fixed small:

- **candidate size** (CacheLayer: 3 declarations). Parse+check+lower dominate the
  round-trip (~0.15 ms of the 0.165 ms), and they scale with candidate source
  size, so a large model-authored component costs proportionally more.
- **running-manifest size** (2 components). The G2/G3 link walks running +
  candidate, so a large held composition raises the gate's share.

333's harness therefore reports the round-trip across a BATCH spanning
small/medium/large candidates against small/large manifests, as a distribution
(median + p90 + p99 + n), and states the headline as "median tenths of a
millisecond on the representative scenario; scales with candidate and manifest
size" rather than a single universal constant. A guard test keeps the
representative scenario near the item-16 number with a generous
order-of-magnitude ceiling (machines vary; the item-16 guard already does exactly
this and 333 reuses that discipline, never a hard wall-clock assert).

The contrast that makes the item matter belongs in the report: the in-process
round-trip is tenths of a millisecond and zero tokens; the MCP-bridge alternative
(docs/mcp-bridge.md) adds a process hop plus a JSON-RPC request/response whose
schema and result are output tokens the agent pays for every candidate (item 50).
The in-process gate removes both the hop and the tokens. State this as a
structural difference (hop + tokens removed), and measure the MCP round-trip
head-to-head only if a bridge is already stood up; do not invent a bridge just to
produce a losing comparison number.

## 4. Honest boundary: this gate admits at COMPILE time; it does not confine the runtime

The single most important line to keep straight, and the one `gate.py` itself
draws (`src/revl/gate.py:36`): "The gate cannot and does not claim to confine its
host; its guarantees govern the ADMITTED code."

- **What 333 guarantees.** A component that the in-process gate REFUSES never runs
  in the embedder's process, because the embedder gates on the verdict before it
  loads or calls the component. A component the gate ADMITS carries the compile-
  time guarantees the reference gives: it type-checks, it has no open holes, its
  effects are classified, its requires/provides resolve against the running
  composition (G2/G3), it declares no smuggled host code (G8), and under the
  untrusted-author profile it reaches only granted services.
- **What 333 does NOT guarantee.** It does not sandbox the admitted code AS IT
  RUNS. An admitted emission still fires when called; the compile-time gate does
  not, by itself, make a wrong-but-well-typed tool call revertible, nor does it
  contain a runtime fault, nor does it isolate the admitted code's memory or its
  host-block externs. An `extern` body is arbitrary host code: the gate SURFACES
  it (G8), it does not neuter it.
- **Who owns the runtime half.** That is item 334 (embed gate + witnessed-effect
  runtime, run the admitted tool under revertible effects, roll back residue-free
  on abort) built on 243/245/322. The layer-2 `Gate` facade already threads the
  245/246 machinery (commit/abort, the approver seam) so that a SESSION admitted
  through it can be reverted; but the bare layer-1 `admit`/`admit_into` verdict an
  agent loop uses for cheap per-candidate screening is a decision, not a running
  sandbox. 333 delivers the decision at 0.x ms; 334 delivers the reversible run.

An embedder who reads "in-process gate" as "in-process sandbox" will ship an
unsafe host. The doc, the harness comments, and the report must all say: admitted
!= safe to run unwitnessed. This boundary is restated in the adversarial review
because it is also an attack surface (an overclaim is a vulnerability).

## 5. Adversarial self-review

Five attacks. The CRITICAL is A1.

### A1 (CRITICAL): "matching the CLI verdict" against the WRONG CLI verb makes the exit test a lie

The item text says the harness admits/rejects "matching the CLI verdicts". The
obvious implementation reaches for `revl compile`, because that is the verb whose
name says "compile this source". But `revl compile` does NOT call
`refuse_admission` (`__main__.py` compile path; it prints holes as a draft
warning and exits 0), while the in-process `admit` DOES (`gate.py:180-181`). So a
candidate that is a draft with an open hole: `revl compile` reports it admitted
(exit 0), `revl.gate.admit` refuses it. A harness that oracles against `revl
compile` would EITHER (a) declare a spurious mismatch on every hole draft and look
broken, OR, worse, (b) "fix" the mismatch by weakening `admit` to skip
`refuse_admission`, which reintroduces exactly the false-admit that
`tests/test_gate_surface.py::test_admit_is_not_admitted_by_compile_source_alone`
was written to close. Direction (b) is a security regression: the gate would then
ADMIT a component the reference refuses to run, which is the one thing the whole
arc forbids ("never admit what the reference refuses").

The symmetric half of A1: even with the right verb, an `admit_into` (against a
running manifest) call compared to a manifest-LESS CLI invocation asks two
different questions; a candidate that `requires` a running service refuses
standalone and admits against the manifest, so the harness would report a
mismatch that is really an oracle mis-parameterization.

**Resolution (mandatory in the design).** The oracle is the ADMISSION path, and
the oracle invocation is constructed from the IDENTICAL inputs (source, manifest,
profile) as the in-process call, per candidate kind, as specified in section 3.
The harness never uses `revl compile` as the oracle for `admit`. The match test
must include at least one hole-draft candidate specifically to prove the oracle is
the admission verb and not the compile verb (if it passes with a hole draft, the
oracle is correct; if a naive implementer wired `revl compile`, that candidate
fails loudly and names the bug). This turns A1 from a silent trap into a test the
exit criteria enforce.

### A2: the in-process verdict drifting from the CLI verdict via process-global state

The two verdicts are produced in DIFFERENT processes (in-process loop vs oracle
subprocess), and the in-process one runs after N previous admits in the SAME
process. If any admit mutates process-global state that a later admit reads, the
Nth in-process verdict can drift from the fresh-process CLI verdict even though
both call the "same" engine. Candidate state: `gate._ACTIVE_GATE` (the
process-global single-gate slot), any module-level caches in `compile_source`'s
lexer/parser/stdlib-loading path, and the `exec_module` load of backend emitters
in `compile_to`.

**Assessment.** Layer-1 `admit`/`admit_into` are documented disk-pure and
clock-pure and take no `Gate`; they do not touch `_ACTIVE_GATE`. The real risk is
a hidden parse/stdlib cache that makes admit N depend on admit N-1. This is not a
theoretical worry to wave away: it is exactly why 333's match check runs over a
BATCH in one process and compares each to a FRESH subprocess, rather than checking
one candidate once. If any per-process cache introduces order-dependence, the
batch-vs-fresh comparison exposes it as a mismatch at some N. **Mitigation:** the
harness admits the batch in a fixed order AND in a shuffled order and asserts the
verdicts are identical across orderings (order-independence is the property that
proves statelessness); any drift is a bug filed against the offending cache, never
worked around in the harness.

### A3: the py embed importing a mutable global that a malicious candidate perturbs

A hostile candidate SOURCE is admitted by an embedder who trusts the gate. Could
that source, during admission, reach into and mutate a module-global the gate or a
later admit depends on, poisoning subsequent verdicts?

**Assessment.** No, and the reason is structural: admission is parse + check +
lower + `refuse_admission`. It NEVER EXECUTES the candidate's code. There is no
`eval`, no import of the candidate as a module, no running of its bodies at admit
time. A candidate's `extern` host-block is arbitrary host code, but it is
SURFACED (G8) and only RUNS if the admitted component is later loaded and called,
which is the runtime half 333 explicitly does not do (section 4). So the attack
"a malicious component perturbs a mutable global during admission" has no admission-
time execution to ride on. **The residual real risk is A2's** (first-party caches,
not attacker code) and A4's (the embedder itself). The design states the invariant
plainly: admit executes no candidate code, therefore a candidate cannot mutate
process state at admit time; the moment it could (a load/call) is 334's boundary,
gated by the witnessed runtime.

### A4: an embedder mutating compiler state between admits

Distinct from A3: not the candidate but the HOST monkeypatches or reconfigures
revl internals between admits (reaches past `revl.gate` into `revl.compiler`,
swaps a stdlib path, flips a global flag), so the gate's later verdict is no longer
the reference's.

**Assessment.** This is out of the threat model and must be said so, honestly. The
gate cannot defend against a host that edits the compiler it imported; that host
could equally reimplement `admit` to `return Verdict(True)`. 332's embedding
contract is "the gate's guarantees hold WHEN the host uses the published surface";
333 inherits that. **Mitigation that is in scope:** `gate_version()` returns
`{api, language, frontier}` so a paranoid embedder (and 337's seam re-admission)
can assert it is talking to the surface and language version it expects before
trusting agreement; the design recommends the harness log `gate_version()` in its
report so a drift caused by a version skew is attributable rather than mysterious.
The design does NOT claim to defend against a host that tampers below the surface.

### A5: the 0.165 ms cost claim not holding under real candidate sizes

The headline was measured on a 3-declaration candidate against a 2-component
manifest. A real model-authored tool component and a real running composition are
larger, and if the round-trip scales super-linearly the in-process pitch weakens.

**Assessment.** Parse+check+lower dominate (~0.15 of 0.165 ms) and are roughly
linear in candidate source size; the G2/G3 link is roughly linear in
running+candidate declaration count. There is no known super-linear phase, but the
honest move is to MEASURE it rather than assert linearity. **Mitigation:** section
3's batch spans candidate and manifest sizes and reports the distribution; the
report states the headline as "tenths of a millisecond on the representative
scenario, scaling with candidate and manifest size", and the guard test pins only
the representative scenario with an order-of-magnitude ceiling. If the batch ever
shows a large candidate blowing past a small multiple of the headline, that is a
finding for the report and possibly a bench/perf item, not something the harness
hides by only ever measuring the small case.

## 6. Sliced plan

### Slice 1 (this item's deliverable): the py in-process embed, proven and measured

Unblocked today; embeds only what 332 already ships on py.

1. `bench/inprocess_gate_harness.py`: the example agent-framework harness. Holds a
   compiled base composition; admits a curated batch of candidates in-process via
   `revl.gate.admit` (standalone) and `revl.gate.admit_into` (against the held
   manifest); the batch includes must-admit, must-reject (G2/G3/G8/allowlist), and
   at least one hole-draft candidate (the A1 oracle-correctness probe). Reuses
   `bench/admission_latency.py`'s scenario and `measure()`.
2. The CLI-match check: per candidate, run the reference ADMISSION oracle as a
   subprocess constructed from the identical inputs (section 3), and assert the
   in-process verdict `{admitted, code}` equals the oracle's. Include the
   fixed-order vs shuffled-order equality check (A2).
3. The cost measurement: round-trip distribution across the size-spanning batch,
   rendered to `bench/results/inprocess-gate.md`, headline stated as a scaling
   claim, not a universal constant, alongside the structural in-process-vs-bridge
   contrast (hop + tokens removed).
4. A guard test `tests/test_inprocess_gate.py`: runs a small batch in CI, asserts
   every in-process verdict matches its oracle (correctness, including the hole
   draft), asserts order-independence, and applies an order-of-magnitude latency
   ceiling only (no hard wall-clock). Mirrors `tests/test_admission_latency.py`.
5. Docs: a short "embedding the gate in an agent loop" section pointing at the
   harness as the onboarding example, cross-linked from docs/mcp-bridge.md and the
   token-economy doc, and stating the section-4 boundary in consumer-facing words.

Exit for Slice 1: `python bench/inprocess_gate_harness.py` admits/rejects the
batch in-process, every verdict matches the CLI admission oracle (hole draft
included), and the round-trip distribution is recorded. This satisfies the item's
exit test on the py tier.

### Slice 2 (deferred, gated on 332): the rust in-process embed

Gated on 332 shipping the `revl-gate` crate v0 (the `admit`-only native cut) plus
`tools/build_gate_crate.py` and the committed crate. When those exist:

1. Port the harness shape to a standalone rust binary depending ONLY on the
   published `revl-gate` crate (the 332 exit-test binary is the starting point).
2. Batch of standalone candidates (rust `admit` has no manifest today, A-gap #2 in
   the 332 dependency section); the against-a-running-composition batch waits on
   native `admit_into`, tracked as a 332/self-host frontier item, not smuggled into
   333.
3. Differential match: rust `admit` verdict agrees with the py/reference verdict on
   the shared corpus, FAIL CLOSED (rust may refuse what py admits; rust must never
   admit what py refuses), inheriting 332's crate release gate rule.
4. Cost measurement on the rust tier via the crate, reported alongside the py
   number.

Do not start Slice 2 until the crate lands; do not build a bespoke rust admission
path inside 333.

## Exit tests

- **Slice 1 (py, this item):** an example py agent harness embeds the gate, admits
  and rejects a batch of proposed components in-process, EVERY verdict matches the
  reference CLI admission oracle (including a hole-draft candidate that proves the
  oracle is the admission verb, not `revl compile`), the batch verdicts are
  order-independent, and the in-process round-trip cost is measured as a
  distribution over realistic sizes and recorded. Guard test in CI.
- **Slice 2 (rust, deferred):** the same harness as a standalone rust binary
  against the published `revl-gate` crate, verdicts differentially agreeing with
  the reference fail-closed, cost measured. Gated on 332's rust crate.

## The honest hard part (consolidated)

The engine and the number already exist; 333's real work is refusing two
overclaims. First, "matches the CLI" is only true against the ADMISSION verb, and
wiring it to `revl compile` either looks broken or gets "fixed" into a security
regression (A1, CRITICAL). Second, "in-process gate" is a COMPILE-TIME decision,
not a runtime sandbox; the reversible-run half is 334, and an embedder who
conflates the two ships an unsafe host (section 4). Get those two right and 333 is
a packaging-and-proof item: one py harness, one honest measurement, and a rust
slice that waits, without apology, for the crate 332 owns.
