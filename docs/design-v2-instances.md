# Instance-parametric components (design)

Status: **phase 1 implemented on the cordis-py reference tier.** The frontend
(parser, lower, typecheck), the cordis-py runtime and emitter, and an executed
test are in the tree; the frozen IR, grammar and resolved G-rules are recorded
in "Phase 1 — frozen" at the end of this document. The six open questions below
are the design record that phase 1 resolves; the resolutions (not the original
recommendations) are what shipped. Phases 2–5 (the other four tiers) and the
held IR-cleanup wave build on the frozen forms below.

## The problem

A revl `component` is one instance per composition. The linker composes components
over a static manifest and config is resolved at admission; components are never
imported, they are composed (`docs/syntax-2.0.md:43-46`).

Real applications are full of runtime-created things that have lifecycle: a
connection, a session, a request context, an open document, a game entity, a
tenant. In revl today those cannot be components at all. They live in the pure
stratum as records and ADTs, which have no `requires`/`provides`, no effects, and
no teardown.

So the dynamic structure of an application — precisely the part where residue
accumulates — sits outside G1-G8.

## What the runtime already does

Two experiments against cordis-py (the reference tier).

**1. Two instances in one realm are refused — by G2, not by an instance limit.**

The second fiber spawns and executes its body; what it hits is provision
disjointness:

```
AttributeError: service "kv" has been registered at <Store>
```

**2. Two instances in separate realms coexist.**

```
TWO LIVE INSTANCES: 2 2
dispose one, other survives: 2
both down: 4 4
```

Independent lifecycle, independent teardown, no compiler change required. The
substrate is not missing this feature. revl's frontend does not surface it.

## Local realms, not dynamic labels

`docs/v2.0-roadmap.md:651` pairs this with dynamic realms ("Dynamic realms —
needs instance-parametric components"), and `docs/design-v2-realms.md:50` rejects
dynamic labels as unsound: the linker "could neither prove nor refute a collision
between two config-derived realms."

That argument is correct — **for global realms**, which are keyed by label string.
`backends/python/runtime.py:62` interns labels into a process-wide registry
specifically so that equal strings share a realm. Collisions are the point of
that form, so an unknown label is genuinely unsound.

But cordis has a second realm form revl never exposes.
`backends/python/.cordis-py/src/cordis/loader.py:538` distinguishes:

- `GlobalRealm(label)` — identity is the label string, shared between entries
  with equal labels. This is the only form revl surfaces.
- `LocalRealm(entry)` — **identity is the entry**, unique per instance, no label.

`Context.isolate(name, label=None)` (`context.py:74`) mints a fresh
`unique_symbol(name)` when no label is given. Experiment 2 above called it twice
with the *identical* name and got two distinct, non-colliding realms.

**Therefore instance-parametric components do not require dynamic realm labels.**
Two instances in local realms cannot collide by construction; no config value need
be known at link or admission time. The soundness objection does not reach this
case. This is a materially smaller and better-founded change than the roadmap's
framing: exposing a realm form the compiler already depends on, not inventing one.

## The thesis: instantiation is an acquisition — and what it is missing

The paradigm contains the right shape. A spawned instance is an effect whose
inverse is its own teardown:

```revl sketch
let s = effect spawn Session with { id } undo s.dispose()
```

**But the inverse discipline does not come for free, as first drafted here.**
The static audit (`docs/notes/static-instance-assumptions.md`, branch
`agent/static-audit`) corrected this, and the correction is verified:

`provide` is rejected inside a method body (`src/revl/parser.py:1026-1029`), and
`Frame` is "one component activation's effect accumulator"
(`backends/python/runtime.py:266-276`). Effects created inside a provide-method
are *adopted* into the component-level frame — they live until the **component**
tears down, not until the call returns.

So `let s = effect spawn Session ...` inside a provide-method yields a session
whose lifetime is the component's, not the request's. For a request-scoped
instance that is precisely wrong: it is an unbounded leak — residue accumulation,
the exact failure this feature exists to prevent.

G7 covers dynamic structure at **component** granularity. Dynamic instances need
**sub-component teardown scopes**, and no such mechanism exists in the language or
in any runtime adapter. That is item zero, and it is new work, not a free ride.

## Prerequisite: the checker's realm function is flat, the runtime's is not

This one opens with `spawn` itself — before per-instance realms are ever added.

The checker (`src/revl/lower.py:3047-3048`):

```python
def _realm(entry: dict, key: str) -> str:
    return (entry.get("isolate") or {}).get(key, SHARED_REALM)
```

A flat lookup on the entry's own `isolate` map. The runtime instead reads
`_effective_isolate()` off the **parent chain** (`cordis/context.py:83-89`), and
`plug` isolates a scoped context *before* `ctx.plugin`
(`backends/python/runtime.py:72-80`).

These agree today only because everything is plugged onto the root context. The
moment a child is plugged onto its spawner's context, the linker says "shared
realm" while the runtime says "whatever the parent was isolated into" — and both
G2 and the G3 edge set silently mis-resolve. Silently is the operative word.

**Hierarchical realm resolution in the checker is a prerequisite for spawn**, not
a follow-up to it.

## Open questions

Recommendations are mine and are not accepted.

0. **Nested teardown scopes.** See above. Needs a sub-`Frame` in every runtime
   adapter and a scope notion in the language. Blocks everything else.

1. **G2 across live instances.** The audit classes this HARD, and is right about
   the mechanism: `provider_of: dict[(key, realm), name]`
   (`lower.py:3053-3077`) is a link-time injectivity proof over a finite table,
   and with N unknown there is no table.
   *Recommendation: forbid an instance providing into any realm but its own local
   one.* That replaces the table **for instances** with a structural argument —
   disjointness by construction — while static entries keep the table. My earlier
   "a rule to write down, not a research problem" was too glib: the rule is
   simple, but proving the two regimes compose is not.

2. **Addressing.** A local realm is private to its spawner, so an instance is
   reachable by its parent and its own children, not by arbitrary siblings.
   *Recommendation: accept this* — it is OTP's supervision-tree shape. It is a
   product decision, not a detail: "look up the session by id from anywhere"
   becomes inexpressible by construction.

3. **G3 acyclicity.** `lower.py:3086-3097` rejects self-provision categorically,
   and `graph` is `dict[str, list[str]]` — the linker **cannot represent** the
   type-graph/instance-graph distinction. A `Session` spawning a `Session` is a
   self-edge in the only graph that exists.

4. **G4 / G6 across the spawn boundary.** Larger than first stated: there is no
   mechanism to generalize. `ComponentDecl` (`parser.py:182-190`) has no emission
   clause at all, and the only bound (`lower.py:2775-2827`) scopes one
   provide-method against one service declaration. Without one, emissions escape
   their bound by being moved into a spawned child — a soundness hole.

5. **G8 enumerable boundary.** Audit calls this mechanical: the emission and
   capability sets are already syntactic and instance-independent; only the
   integer counters (`__main__.py:139-140`) need re-basing to report
   `Session x dynamic`.

6. **Hot-swap** replaces by declaration name (`compiler.py:295`). Undefined with
   N live instances.

Audit's overall split: the assumption is **concentrated, not spread** — `_link`
(`lower.py:3002-3145`) is the only place G2 and G3 exist. G5, G6, G8 and the
intra-component half of G7 are already instance-blind. `loadOrder` and its five
`reversed()` teardown sites are mechanical; LIFO is *already* dynamic at the
effect level (`runtime.py:266-288`).

## Runtime parity (verified, all five tiers)

From `docs/notes/runtime-parity-local-realms.md` (branch `agent/runtime-parity`).
Every tier was actually executed — no tier rests on source reading. The cordis-py
control reproduced both experiments above verbatim.

| tier | 2+ live instances | identity-keyed local realm | independent disposal + LIFO |
|---|---|---|---|
| cordis-py | yes | yes — `isolate(name)` mints a fresh symbol per call | yes |
| cordis (TS) | yes | yes — same implementation as py | yes |
| cordis-rs | yes | yes — `Context::isolate` → monotonic `Isolation(u64)` | yes |
| cordis4j | yes | yes — `ctx.fork()` alone isolates, no label needed | yes |
| cordis-wasm | yes | **no** — no realm concept in the runtime at all | yes |

**Local realms are available on four tiers today.** cordis-wasm is the only gap:
its provider table is a flat `dict[str, Provider]` and realms are compile-time
name mangling (`backends/wasm/emit.py:151-155`), so a runtime-determined instance
count cannot be expressed without emitting one module per instance. The fix is a
realm prefix applied at resolve, publish, and the conflict check
(`backends/wasm/runtime.py:293`, `:332`, `:131-137`).

### The inversion

cordis4j does not implement the reference isolation model. Each `ContextImpl`
owns its own `ServiceRegistry.store` with parent-chain resolution, so two forks
hold the same key independently **with no isolate call and no label**:

```
B1/same realm string -> BOTH LOADED
B1/same1.kv -> s1  same2.kv -> s2
```

where cordis-py, cordis (TS) and cordis-rs all refuse a plain child context.

So on cordis4j, `docs/design-v2-realms.md`'s "equal strings = same realm" is
**false at the level revl targets**. Global-realm interning exists one layer up in
`Loader`, which revl's Java emitter bypasses — `backends/java/emit.py:2094-2096`
emits `ctx.isolate(Svc.class, "<label>")` against core `Context` directly.

The consequence for this design is favourable and worth stating plainly: **the
realm form instance-parametric components need is the cheap one everywhere, and
free on Java. The expensive form is global realms — the one already shipped.**

## Inputs still outstanding

None blocking. The remaining gate is a decision on question 2 (addressing).

---

# Phase 1 — frozen

This section is normative for phases 2–5 (the other four tiers) and the held
IR-cleanup wave. It records what shipped on cordis-py.

## The grammar — exactly one new form

`spawn` is an expression, legal **only** as the acquisition of an effect
binding. No other production changed. `spawn` and (existing) `with` are the
only keywords involved; a spawn's `undo` is required, exactly as for any
acquisition.

```text
spawnexpr := 'spawn' IDENT [ 'with' '{' (IDENT ':' expr (',' IDENT ':' expr)*)? '}' ]
leteffect := 'let' IDENT '=' 'effect' spawnexpr 'undo' expr
```

So the surface form is:

```text
let s = effect spawn Worker with { tag: "a" } undo s.dispose()
```

The handle `s` is a host-frontier value (type `Instance[Worker]`, advisory)
whose one operation is `.dispose()` — the acquisition's inverse. A spawn must
be bound (there must be a handle to name in `undo`); a bare `effect spawn …` is
rejected. A spawn is legal in a component activation body **or** in a
provide-method body (the request-scoped case, item zero).

A complete, compiling example (this exact program is executed by
`tests/test_instances_exec.py`):

```revl
service Counter { fn value() -> Int }

service Super { async fn retire_a() -> Int }

component Worker provides counter: Counter {
  config { tag: Str }
  let m = effect Map.new() undo m.drop()
  provide counter { fn value() = 0 }
}

component Supervisor provides super: Super {
  let w1 = effect spawn Worker with { tag: "a" } undo w1.dispose()
  let w2 = effect spawn Worker with { tag: "b" } undo w2.dispose()
  provide super {
    async fn retire_a() {
      await w1.dispose()
      return 1
    }
  }
}
```

## The frozen IR — the checkpoint deliverable

Instantiation is an acquisition, so **there is no new IR step kind.** A spawn is
a `let-effect` step whose `acquire` is a `spawn` expression node. The handle is
the step's `bind`; the teardown is the step's `undo`. Instance identity and the
per-instance local realm are carried entirely by `realms` on the spawn node.

The `spawn` acquire node:

```json
{
  "kind": "spawn",
  "component": "Worker",
  "config": { "tag": { "kind": "lit", "value": "a" } },
  "realms": ["counter"],
  "line": 14
}
```

- `component` — the target component's name (a template; see below).
- `config` — field name → lowered pure-expression node, resolved in the
  spawner's scope against the target's declared `config { }` fields (unknown
  fields and missing required fields are rejected at lower time).
- `realms` — the sorted list of keys the target **provides**. At runtime each is
  isolated into a *fresh local realm* (unlabelled `isolate`, a distinct
  identity per spawn), so two instances of one component never collide on a
  provision — disjoint by construction, no config value known at link time.
- `line` — source line, for diagnostics.

The enclosing step is an unchanged `let-effect`:

```json
{
  "step": "let-effect",
  "bind": "w1",
  "acquire": { "kind": "spawn", "component": "Worker", "config": { ... }, "realms": ["counter"], "line": 14 },
  "undo": { "kind": "call", "target": { "kind": "name", "id": "w1" }, "method": "dispose", "args": [] }
}
```

**A spawn present anywhere bumps the document to `ir_version` 3**, so a consumer
predating the feature refuses the whole document rather than mis-composing a
template as a static entry.

**Manifest.** A spawn target is a *template*: a runtime instance, never a static
composition member. Templates are excluded from `manifest.components` (the
G2/G3 table) and from `manifest.loadOrder`, and are listed in a new
`manifest.templates` (present only when non-empty, so non-spawning programs are
byte-identical). Templates are still emitted in `ir.components` as plugin dicts
(a template must exist to be spawned). Every other tier reads `realms` off the
spawn node and `templates` off the manifest; nothing else is required.

## The runtime model (cordis-py reference)

`runtime.spawn(ctx, component, config, realms)`:

1. `scoped = ctx`; for each key in `realms`, `scoped = scoped.isolate(key)` with
   **no label** — a fresh local realm per spawn.
2. `fiber = scoped.plugin(component, config)` — the instance is a *child fiber
   of its spawner*, nested under the spawner's context. This is verified: the
   child's parent chain is `scoped → spawner-ctx → root`.
3. return a `SpawnHandle(fiber)`.

`SpawnHandle.dispose()` unloads the child fiber (running the instance's LIFO
teardown) and is **idempotent**. Because the instance is its own child fiber, it
is its own nested teardown scope (item zero): `s.dispose()` reclaims it *now*,
independent of the spawner. The spawner's own inverse (`yield lambda:
s.dispose()`, or the frame-adopted safety net for a method-body spawn) is a
harmless no-op once the instance is already gone — so an un-disposed instance
still cannot outlive its spawner, but a request-scoped instance is reclaimed the
moment the request ends.

**On "the core runtime change" (item zero).** The nested teardown scope is the
child fiber. cordis-py already has the primitive (a fiber unloads independently
of its parent); the change is that spawn *uses* it — it plugs a child fiber and
returns a disposable handle, rather than running the instance's effects inline
and adopting them flatly into the spawner's `Frame` (which is what would leak).
No new `Frame` subclass was needed; the sub-scope is a sub-fiber.

## The resolved G-rules (file:line)

Every change is inert for non-spawning programs (no template ⇒ no exclusion, no
version bump, no new check), and goldens stay byte-identical.

- **Grammar / parser.** `spawn` keyword (`src/revl/lexer.py`, synced to
  `selfhost/lexer.rvl`); `SpawnExpr` node and `spawn` in the acquisition
  position (`src/revl/parser.py`, `effect_form` / `spawn_expr`).
- **Lower — spawn node.** `_lower_spawn` (`src/revl/lower.py`), dispatched from
  `_lower_expr`; unbound-spawn rejection in `_lower_component`; method-body
  spawn (item zero) in `_lower_provide`'s method loop (`let-effect` step).
- **Typecheck.** `infer_ir` yields `Instance[C]` for a spawn node
  (`src/revl/typecheck.py`); `.dispose()` on the handle rides the existing
  host-frontier method-call path (`_lower_postfix`), the same one `p.close()`
  uses.
- **G2 (disjointness), decision 5.** Structural, not table-based: a template
  provides only into its own fresh local realm and is *excluded* from the
  link-time `provider_of` table (`_link`, the `templates` skip in
  `src/revl/lower.py`). An instance can never provide into its parent's realm —
  there is no surface form for it, and the runtime always isolates each provided
  key.
- **G3 (acyclicity), decision 6.** Quantified over the *instance* graph. The
  instance graph is a **tree by construction** — every spawn mints a fresh child
  — so spawn edges never form an instance cycle, and a `Session` spawning a
  `Session` is allowed. It is a self-edge on the type graph, but a template is
  not a static entry, so the categorical self-provision rejection in `_link`
  never sees it. Real dependency cycles among *statically composed* components
  are caught exactly as before.
- **G4/G6 across the spawn boundary, decision 8.**
  `_check_spawn_emission_bounds` (`src/revl/lower.py`), run after all components
  are lowered. A spawn inside a provide-method declared `plain` (or
  `emission[caps]`) is rejected when the target's emission surface is non-empty
  (or not ⊆ caps). The surface is a sound, conservative over-approximation: the
  union of the target's provided-service emission capabilities, its own `emit`
  steps, and — by fixpoint over the spawn graph — its own children's surfaces.
  A body-level spawn has no emission clause to widen, exactly as body-level
  `emit` has none, so it is unconstrained here.
- **G7 (LIFO teardown).** Unchanged in rule; already dynamic at the effect
  level. Per-instance LIFO is the child fiber's own unload; verified by the
  executed test.
- **G8 (enumerable boundary), decision 7.** The instance dimension is reported
  as dynamic: `revl audit` prints an "instance-parametric components (×
  dynamic)" section from `manifest.templates` (`src/revl/__main__.py`). The
  emission/capability *sets* are syntactic and instance-independent, so they are
  reported per template as before.
- **Hierarchical realm resolution, decision 9 — see the honest note below.**

## Where the design doc was wrong, and what phase 1 does instead

- **Item zero over-stated the runtime change.** It called a nested `Frame` "the
  core runtime change." No `Frame` change was needed on cordis-py: the nested
  teardown scope is a child fiber, a primitive the runtime already has. The
  change is that spawn plugs a child fiber instead of adopting inline. Phase 2
  tiers must each provide an equivalent *nested* scope (a sub-fiber / fork), not
  a flat adoption — that is the real portable obligation.

- **Decision 9 (hierarchical realm resolution in the checker) is a
  prerequisite that phase 1 did not need to exercise, and here is why.** The
  audit's H6 mismatch (checker flat, runtime hierarchical) bites only when a
  *static entry* is plugged onto a non-root context. Phase 1 never does that:
  spawn targets are **templates excluded from the static manifest**, not static
  entries composed under a spawner. The instance's realm resolution is
  hierarchical *at runtime* (cordis `_effective_isolate` walks the parent
  chain), and the checker deliberately does not reason about instances
  statically (decision 1 — instances are not globally addressable, hence not in
  the static table). So the flat `_realm` in `_link` stays correct for phase 1,
  and the goldens stay byte-identical. The hierarchical-checker work becomes
  real in phase 2 only if a future model lets a component be *both* statically
  composed *and* spawnable (see below); phase 1 forbids that, which is what
  dissolves the H6 gap rather than papering over it.

- **A component cannot be both statically composed and a spawn template
  (phase-1 restriction).** Being named by any `spawn C` makes C a template,
  fully excluded from static composition. This is the clean model — it is what
  makes the recursive-self-spawn / self-provision paradox disappear — but it is
  a restriction, and lifting it (a component with one static instance *and*
  runtime instances) is the natural phase-2 question. It needs exactly the
  hierarchical `_realm` decision 9 asks for, plus a provide-realm/require-realm
  split on the entry.

- **G4 surface is conservative.** The spawn-boundary bound uses the target's
  *declared* provided-service emission capabilities as an upper bound, which can
  over-constrain a spawner (it must cover capabilities the child could emit
  through even if a given run never triggers them). This is sound (never misses
  an escaping emission) and matches the existing "a service declaration is an
  upper bound on its providers" principle; a precise per-call analysis is a
  later refinement, not a soundness fix.

- **Self-spawn compiles but can diverge at runtime.** `G3` over the instance
  graph is about acyclicity, not termination: a `Session` that unconditionally
  spawns a `Session` is statically well-formed and will recurse without bound at
  runtime unless its own config gates it. That is the same status as unbounded
  recursion in any language — a runtime property, not a static one.

## Not in phase 1

Hot-swap of a component with live instances (question 6) is still undefined —
`compiler.py` replacement is by declaration name, and "replace `Session`" with N
live instances has no chosen semantics. Phase 1 neither uses nor breaks it: a
template is not in the manifest a hot-swap admits against. The other four tiers
(TS, rust, java, wasm) are phase 2+; cordis-wasm additionally needs the ~10-line
per-fiber realm-prefix runtime change recorded in
`docs/notes/runtime-parity-local-realms.md`.
