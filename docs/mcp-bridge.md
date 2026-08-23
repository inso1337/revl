# revl ⇄ MCP — the agent boundary

**Status:** implemented — `revl mcp {serve,schema,import}`, a live in-memory
session (`revl_load`/`revl_call`/`revl_swap`/`revl_edit`/`revl_rollback`/
`revl_unload`/`revl_state`), and `revl serve --mcp app.rvl`, which serves *one
booted composition's own* operations as tools. Tests: `tests/test_mcp.py`
(projection and protocol), `tests/test_mcp_session.py` (in-memory compilation
and the live session), `tests/test_mcp_edit.py` (the delta-patch verb) and
`tests/test_mcp_serve.py` (the served composition;
the runtime-free wire and preflight tests run everywhere, the live-call tests
skip without the cordis-py runtime).

An AI agent meets a revl system in four roles, and all four are *boundary*
phenomena — which is why none of them needed a language feature:

| the agent is… | the mechanism | where |
|---|---|---|
| a **consumer** of the composition's schema | services projected to MCP tool *definitions* | `revl mcp schema` |
| a **caller** of a running composition | those tools served live off a booted composition | `revl serve --mcp` |
| an **operator** of the composition | the compiler as an MCP server | `revl mcp serve` |
| a **dependency inside** it | an LLM is a `service` with `emission` ops | ordinary revl |

The first two are the same projection at two lifecycles: `revl mcp schema`
emits the tool *definitions* (what a client would see); `revl serve --mcp`
boots the composition and puts those same tools on the wire, each call landing
on the live operation. Import (§2) and serve (§4) close the loop: a tool an
agent imported *from* another MCP server can be re-served *as* a revl tool,
now with a compiler-derived hint instead of the original author's assertion.

The third needs no tooling at all: `service Assistant { emission fn
complete(prompt: Str) -> Str }` makes a model a coeffect, so routing is
provider hot-swap, governance is `intercept` metadata, cost lands on the G8
audit, and a failed generation can carry `compensate`. Nondeterminism stays
*outside* the checked layer, where it cannot poison the metatheory.

The same "no language feature needed" reading answers the workflow-engine
pattern — a *query* is a provided pure `fn` served here with a compiler-proven
`readOnlyHint`, a *signal* is a `revl_call` on a provided operation, and a
provider-change event is a reactive coeffect (R2/R3). See
[docs/signals-and-queries.md](signals-and-queries.md), which also decides the
one gap (external event subscription: a bus is a service, not new grammar) and
shows how a durable signal rides item 47's crash-recovery WAL.

## 1. `revl mcp schema` — services → tools

Every operation a composition **provides** becomes a tool definition whose
JSON Schema comes from the declared types.

```bash
revl mcp schema examples/user_cache.rvl
```

The point is the annotations. MCP's `readOnlyHint` / `destructiveHint` are
*assertions by the server author*; nothing checks them. revl takes them from
the `emission` classification — which is worth something only because the
compiler holds every provider to it.

That guarantee had a gap when this bridge was first built: emission was
checked at call sites but not propagated to the enclosing service operation,
so a projection that trusted the declaration could advertise a write as
read-only. The bridge briefly compensated by walking bodies; the right fix
was in the checker, and it shipped.

The reference example originally understated itself — `Cache.put` was
declared plain while its body emitted through `db` — and the checker now
refuses that:

```
examples/user_cache.rvl:33: `Cache.put` is declared plain, but this
implementation reaches `db.execute`
  a service declaration bounds what its providers may do — mark it
  `emission fn put(...)` in service `Cache`, or move the irreversible call
  out of this method (G4)
```

**The rule: a service declaration is an upper bound on its providers'
effects.** A provider may be *purer* than declared (declared `emission`,
body doesn't emit — the consumer already assumed the worst); it may never
be less pure. That direction is the sound one because consumers bind to the
*service*, not to a component, and providers are hot-swappable: a plain
declaration must mean "no provider of this operation reaches the boundary".

It also repairs a hole in G8 itself. `revl audit` enumerates a caller's
emissions by reading the declarations of the methods it calls — so an
under-declared operation made the audit *incomplete for every consumer*,
not just misleading to an MCP client.

**What the guarantee covers — and first-class functions.** The emission
analysis is name-based: it proves a body reaches no `emission` extern
*through the calls it names*. An emission can also hide behind a function
*value* — `fn quiet(a) = indirect(ship, a)`, where `indirect` dispatches
through an arrow-typed parameter — which no call-graph edge can see, and
which before this fix compiled, advertised `readOnlyHint: true`, and
emitted at runtime. The rule now is: a first-class reference to an
emitting callable (any use of its name outside call position — passed as
an argument, returned, aliased to a binding) is treated as reaching an
unnameable boundary, propagates through the same fixed point as a real
emission, and refuses a plain declaration with the same G4 diagnostic,
naming the value flow. The cost is deliberate: a dispatcher helper that
*ever* receives an emitting function is may-emit for every caller, even
one that passes only pure functions. Higher-order code that never touches
an emitting callable — the ordinary case — still compiles read-only. The
guarantee remains static and revl-complete: it bounds what the compiled
program's own bodies can reach, not what host-provided values do.
The G8 audit (`revl audit`) shares this analysis: a boundary reached
through a first-class dispatch is reported as both the concrete extern
(`ship (emission, py)`) and a `*` entry — *that* the reach crosses an
unnameable dispatch and *what* it reaches, so an audit never reads
cleaner than the program it describes.

The projection therefore trusts the declaration, and reports what the body
reaches as provenance:

```jsonc
"x-revl": {
  "classification": "emission",
  "annotationsDerivedFrom": "compiler",
  "effects": {
    "reachesEmission": ["db.execute"],
    "reachesHostCode": [],
    "boundedByDeclaration": true
  }
}
```

`openWorldHint` is derived from extern reachability (transitively through
`fn` calls) and `idempotentHint` from a `commutative` declaration.

## 2. `revl mcp import` — tools → revl

The reverse projection generates a `service` plus an extern-backed provider
skeleton. Trust runs the other way here, so the rule is blunt: **only an
explicit `readOnlyHint: true` avoids `emission`.** An absent annotations
block is not a read-only claim.

```bash
revl mcp import server-tools.json --service Tools --key tools --backend ts
```

```revl
service Tools {
  // Search the corpus
  fn query(q: Str, limit: Opt[Int]) -> Str
  // imported without a verifiable read-only claim
  emission fn write(path: Str, body: Str) -> Str
}
```

Generated sources compile as-is, and the imported surface appears in
`revl audit` under host code — imported trust is *visible* trust (G8).

## 3. `revl mcp serve` — the compiler as an MCP server

Newline-delimited JSON-RPC 2.0 over stdio, stdlib only. An agent gets a
typed protocol instead of filesystem access: every mutation it proposes runs
the same admission gate a human's `revl compile` does.

| tool | what it answers |
|---|---|
| `revl_check` | does this component compile? (summary + G8 boundary, or diagnostics) |
| `revl_admit` | may it enter **this running composition**? (ambient services, G2/G3 across both, interface drift) |
| `revl_audit` | what can this composition touch? |
| `revl_tools` | project its provided services to MCP tools (§1) |
| `revl_grammar` | the language surface, prompt-sized |

Rejections come back structured, so the agent reacts to a *code*, not prose:

```jsonc
{"ok": false, "diagnostics": [{
  "code": "T1", "category": "type-mismatch",
  "file": "…", "line": 3,
  "message": "`db.q` argument `sql` expects `Str`, got `Int`",
  "expected": "Str", "actual": "Int",
  "guarantee": "declared types are checked"
}]}
```

The same projection is available to humans and CI as
`revl compile --json-diagnostics`.

### The live session — check, load, call, swap, prove

The server holds a composition **in memory** and drives it. Nothing a draft
component does touches the filesystem, so an agent can iterate on code that
has no file yet, and only write out what has already proven itself.

| tool | what it does |
|---|---|
| `revl_load` | boot a composition in memory; returns fiber states, provided keys, trace |
| `revl_call` | invoke a provided operation — how you *test* what you just loaded |
| `revl_swap` | admit a candidate against what is running, then hot-swap it (full source, or by name — see *Deltas, not documents*) |
| `revl_edit` | patch the **server-side** source of the running composition and re-admit — a delta, not the whole file ([below](#deltas-not-documents--revl_edit)) |
| `revl_rollback` | restore the generation before the last swap |
| `revl_unload` | tear down and report the residue checks (R4) |
| `revl_state` | what is loaded, and the trace since the last call |
| `revl_gauntlet` | grade a candidate — a verdict dossier from a battery run in an isolated scratch session ([docs/gauntlet.md](gauntlet.md)) |

The loop an agent actually runs:

```
revl_load   {source}                      -> MemCache ACTIVE, keys ["cache"]
revl_call   {key: cache, method: size}    -> 0
revl_swap   {source: <edited>}            -> admitted, swapped
revl_call   {key: cache, method: size}    -> 42
revl_swap   {source: <broken>}            -> T1 rejected, swapped: false
revl_call   {key: cache, method: size}    -> 42   ← still serving
revl_rollback                             -> back to the previous generation
revl_unload                               -> noResidue: true
```

Two properties make this more than convenience. **A rejected candidate cannot
deploy** — the compile runs before the transition, so the running composition
is untouched and keeps answering. And **`revl_unload` proves R4 from inside
the protocol**: registry, provisions, effects and listeners all back to
baseline, so an agent can demonstrate its component leaves nothing behind
*before* committing it to disk.

Multi-module candidates work too: `modules` supplies in-memory sources for
`use` imports, keyed by the path the import names.

### Deltas, not documents — `revl_edit`

The house principle of this protocol is that **state lives server-side and
agents pass names, not contents**. `revl_swap {source: <edited>}` breaks it: to
change one line it re-serializes the *entire composition*, so the cost scales
with the size of the running system, not the size of the change. The
token-surface audit ([`bench/results/token-surface-audit.md`](../bench/results/token-surface-audit.md),
finding #1) measured this as the single highest-value protocol leak — the price
grows precisely as self-evolving compositions get larger.

The session already holds the running composition's source server-side (it kept
the admission inputs so the composition can be snapshotted). `revl_edit` makes
that source *addressable and editable*: an agent sends a small structured patch
against a named buffer, and the server applies it, recompiles, and re-admits —
**without the client resending the file**.

The patch is a list of `edits`, each one of three narrow forms — two carry the
model, the third is ergonomic sugar:

| form | shape | for |
|---|---|---|
| hole fill | `{hole: <line>, expr: "<fill>"}` | fill the typed hole on that line — pairs with `revl_check`'s `fillSpec`, which reports each open hole's `line` and expected type |
| text range | `{range: [start, end], replacement: "<text>"}` | replace the half-open character span `[start, end)` — the precise, general edit |
| anchor | `{anchor: "<literal>", replacement: "<text>"}` | replace a literal snippet with no offset arithmetic — the ergonomic common case |

Re-admission is **never bypassed**: every form ends at the identical gate
`revl_swap` runs (`compile_source`/`compile_files` with `manifest=` the running
IR, then `refuse_admission`). So a patch that would violate a guarantee is
refused with its structured diagnostic and the running system is untouched —
exactly as a full-source swap of the same bytes would be. The verdict is what
comes back — admission result, open holes, or the diagnostic — **never the whole
source**.

```
revl_load  {source}                                        -> size() -> 0
revl_edit  {edits: [{anchor: "fn size() = 0",
                     replacement: "fn size() = 42"}]}       -> admitted, swapped
revl_call  {key: cache, method: size}                      -> 42
revl_edit  {edits: [{anchor: "fn size() = 42",
                     replacement: 'fn size() = "nope"'}]}   -> T1 refused, untouched
revl_call  {key: cache, method: size}                      -> 42   ← still serving
```

A patch may **iterate through holes**. An edit that scaffolds a `hole` compiles
but does not swap — a hole may never enter a running composition — so it comes
back as an obligation carrying its `fillSpec`, and it *advances the server-side
source* so the next edit builds on it. A follow-up `{hole: <line>, expr: …}`
fills that hole by the very line the fillSpec reported, and then it admits and
swaps. Deltas accumulate across the calls; nothing is resent. An edit that fails
to compile or admit advances nothing — the working buffer stays at its last good
state, so a refused patch never leaves a broken draft behind.

**Swap by name.** The same server-side source backs an additive extension to
`revl_swap`: called with *no* inline `source`/`files`/`modules`, it re-admits
the source the session already holds (what `revl_edit` left, or the running
generation itself). Full inline source is still accepted, unchanged — the
name-referenced form is purely additive.

**The measured win.** The delta is a fixed cost; the full-source swap is not.
Using the audit's own BPE proxy (`bench/tokens.py`) on the arguments an agent
serializes to change one `size()` line:

| running system | full-source `revl_swap` | `revl_edit` delta | saving |
|---|--:|--:|--:|
| one component | 97 tok | 30 tok | 67 tok (69%) |
| nine components | 559 tok | 30 tok | 529 tok (95%) |

The `revl_edit` cost is *constant* in the size of the composition — the agent
serializes only the change — while the swap cost grows with every component
admitted. That is the "documents, not deltas" tax the audit ranked #1, paid
down to a fixed price.

Where `revl_swap` is binary — admit or refuse — `revl_gauntlet` is the graded
form: candidate in, dossier out. It runs the same admission gate plus a real
boot/unload no-residue lifecycle in an **isolated scratch session** and returns
a verdict that separates what was **proved** (admission, derived teardown) from
what was **tested** with counts (the no-residue lifecycle) from what remains
**claimed** (the enumerated G8 extern boundary). Fault-sweep and
inverse-round-trip sections are present but report `pending` until roadmap
items 30 and 26 land. See [docs/gauntlet.md](gauntlet.md).

## 4. `revl serve --mcp` — a composition's own operations, served live

`revl mcp serve` (§3) serves the *compiler's* tools: an agent operates the
toolchain. This is the mirror image — boot one composition and put *its
provided operations* on the wire:

```bash
revl serve --mcp examples/user_cache.rvl
```

Every provided operation becomes a tool named `<prefix>.<key>.<op>` (the
prefix is `--composition`, default `revl`), projected by the exact same
`tools_from_ir` that `revl mcp schema` uses — so the tool a client *sees* and
the tool it *calls* are the same definition, at two lifecycles. A `tools/call`
maps the named MCP arguments back onto the declared parameter order and lands
on `Session.call` against the running composition — the same entry point
`revl_call` drives.

**The trust claim, sharpened.** Everywhere else in MCP, `readOnlyHint` is an
assertion by the tool's author, and nothing checks it — the tool-poisoning
gap. A revl-served tool is the only kind whose hint is *compiler-derived*:

- `readOnlyHint: true` appears only where the checker **refused** any
  unreverted mutation, and a service declaration is a checked upper bound on
  every provider's effects (G4), so no provider can exceed what the tool
  advertises;
- a `destructiveHint: true` tool names, in its `x-revl.effects`, the exact
  emissions and capabilities the operation crosses — a declared inverse or
  `compensate` where one exists;
- the implementation behind the tool surface can only change through the
  admission gate (a `revl_swap` on the operator server, §3), never by editing
  the file under a running server.

**Config-to-boot.** Standing a composition up standalone reuses `revl run`'s
preflight: compile → refuse open holes → load `--config` → refuse a component
missing a *required* config field (an IR `config` field with no default),
before any runtime is imported. A mis-configured boot fails loudly at
admission rather than advertising a tool whose fiber has silently settled onto
`FAILED`:

```bash
$ revl serve --mcp needs_config.rvl
error: invalid config:
  - DB is missing required config "url" (Str)
  components are declarations — supply their config with --config <file>.
```

Why `serve` is its own verb and not a third `mcp serve` mode: the compiler
server's tool set is fixed, but this boots a *specific* composition, which is a
`revl run`-shaped concern (same compile/admit/config preflight). `--mcp` names
the transport, leaving room for other serve frontends. It shares `run`'s
preflight, not `mcp serve`'s protocol.

### Import + serve close the loop

A tool imported *from* a foreign MCP server (§2) lands as `emission` unless it
carried an explicit `readOnlyHint: true`, because revl cannot vouch for
another author's assertion. Once that imported surface is implemented and its
classification *verified*, re-serving it with `revl serve --mcp` hands the
next agent a hint the compiler now stands behind — the round trip upgrades an
unverifiable claim into a checked one.

### Wiring it up

```jsonc
// claude_desktop_config.json / any MCP client
// the compiler as a server (operate the toolchain):
{"mcpServers": {"revl": {"command": "python", "args": ["-m", "revl", "mcp", "serve"]}}}
// a composition served live (call its operations):
{"mcpServers": {"user_cache": {"command": "python",
  "args": ["-m", "revl", "serve", "--mcp", "examples/user_cache.rvl"]}}}
```

## Why this shape

The self-evolving-harness scenario (the paper's §1.2.2, and revl's reason to
exist) is exactly: *a component nobody reviewed enters a running system.*
The bridge makes that a protocol rather than a leap of faith — `revl_admit`
before a swap answers "may this enter?" mechanically, and the answer is the
compiler's, not the agent's own judgement. What the agent cannot do is more
important than what it can: it never touches the filesystem of a running
system, and it cannot describe its own tools as harmless when the compiler
says otherwise.
