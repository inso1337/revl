# Where the compiler assumes a static, finite, link-time-known set of component instances

**Status:** audit only (branch `agent/static-audit`). Nothing here proposes a design; it
records what the compiler assumes and what would break if the same `component` could
be live N times with N unknown until runtime.

> **The `:NNN` line numbers below are pinned to `agent/static-audit` and have
> since rotted.** Spot-checked against `main`: `compiler.py:209-212` is now a
> docstring about vendored-copy drift, not the `duplicate component` raise;
> `run.py:222-226` is config-extern merging, not the fiber assignment;
> `parser.py:182-190` is stream-merge prose, not `class ComponentDecl`; and
> `backends/python/runtime.py:72-80` is `__all__`, not `def plug`. Read every
> citation below as a *symbol* name — those are still correct — and grep for it
> rather than jumping to the line. No corrected line number is given here on
> purpose: a pin is what rotted, and a fresh one would rot the same way. The
> audit's substance holds; only the pins moved.

---

## 0. The baseline assumption: **name is identity**

The compiler nowhere represents "an instance". It represents a *component declaration*, and
that declaration is 1:1:1 with a manifest entry and with a runtime fiber. The identity used
at every layer is the bare component **name** (a `str`), and each layer independently
enforces that names are unique.

| layer | enforcement | citation |
|---|---|---|
| module merge | one declaration per name across the whole composition | `src/revl/compiler.py:195`, `:209-212` |
| checker | one declaration per name inside a program | `src/revl/lower.py:1653-1656` |
| linker | manifest entries and every graph keyed by name | `src/revl/lower.py:3006`, `:3040`, `:3079-3081` |
| runtime driver | `dict[str, fiber]` | `src/revl/run.py:225-226` |
| emitter | one module-level plugin dict per component; duplicate names refused | `backends/python/emit.py:499-518`, `:1045-1047` |
| runtime adapter | `RESOLVED_CONFIG[name] = resolved` (process-global) | `backends/python/runtime.py:378` |

```python
# src/revl/compiler.py:209-212
            if comp.name in seen_components:
                raise RevlError(module.path, comp.line,
                                f"duplicate component `{comp.name}` (also declared in {seen_components[comp.name]})")
            seen_components[comp.name] = module.path
```

```python
# src/revl/run.py:222-226
        for name in _load_order(ir):
            comp = by_name[name]
            ...
            fiber = self.root.plugin(getattr(module, name), self.config.get(name, {}))
            self.fibers[name] = fiber
```

Everything below is a consequence of this one fact. There is no `instance` concept in the
AST (`ComponentDecl` in `src/revl/parser.py` has `name/config/requires/provides/body/line`,
plus `source` for provenance and the item-296 `require_carry` alias map; no instance
field among them), none in the IR (`docs/backend-ir.md:42-51`), and none in the manifest
(`src/revl/lower.py:3145`).

---

## 1. G2 — provision disjointness

### How it is checked today

`_link` (`src/revl/lower.py:3002-3077`) builds a flat list of manifest `entries` — one per
component *declaration*, ambient (running) entries first, then newly compiled ones
(`:3008-3032`) — and then quantifies over exactly one data structure:

```python
# src/revl/lower.py:3053-3077
    provider_of: dict[tuple[str, str], str] = {}
    for entry in entries:
        for key in entry["provides"]:
            realm = _realm(entry, key)
            ...
            if (key, realm) in provider_of:
                first = provider_of[(key, realm)]
                ...
                raise RevlError(
                    program.filename, _line(entry["name"]),
                    f"provision conflict: key `{key}`{where} is provided "
                    f"by both {provider_of[(key, realm)]} and {entry['name']} (G2)",
                    ...)
            provider_of[(key, realm)] = entry["name"]
```

So: **`(key, realm) -> component-name`, a total function over a link-time-finite list.**
`realm` comes from the entry's own `isolate` map, defaulting to the shared realm:

```python
# src/revl/lower.py:3047-3048
    def _realm(entry: dict, key: str) -> str:
        return (entry.get("isolate") or {}).get(key, SHARED_REALM)
```

`SHARED_REALM = ""` (`src/revl/lower.py:118`). The same `(key, realm) -> provider` map is
rebuilt independently in the query engine (`src/revl/query.py:112-115`) and in the planner
(`src/revl/plan.py:239-247`).

### Does it survive two live instances each providing the same key in *its own* realm?

**No — not as written, for three separate reasons.**

1. **Realm labels are static string literals, by an explicit soundness argument.** The
   parser refuses anything else:

   ```python
   # src/revl/parser.py:1112-1120
               raise self.err(
                   line,
                   "dynamic realm labels are not supported — a realm is a static string literal",
                   hint="config is unknown at link and admission time, so the linker could "
                        "neither prove nor refute a collision between config-derived realms "
                        "(G2 would be unsound); dynamic realms await instance-parametric "
                        "components (docs/design-v2-realms.md)",
               )
   ```

   `docs/design-v2-realms.md:50-57` states the same in prose and names this exact feature as
   the prerequisite for lifting it. Two live `Session` instances "each in its own realm"
   requires a realm label derived from a runtime value; today there is no such thing.

2. **Even if the label existed, the map is a one-writer-wins dict.** `provider_of` maps a
   `(key, realm)` pair to a single `str` name. N instances of `Session`, each providing
   `conn` in realm `session/<id>`, is N entries under one *declaration* — but the linker
   iterates `entries`, a list of declarations, so it would see one `Session` entry, one
   `(conn, ρ)` pair, and prove nothing about the N runtime pairs. The check would silently
   pass while being vacuous, which is worse than failing.

3. **The realm function in the checker is flat; the realm function at runtime is
   hierarchical.** `_realm(entry, key)` reads *only that entry's own* `isolate` dict. The
   runtime resolves isolation by walking the *parent context chain*:

   > "the fiber's context chain is fixed at plugin time and inject resolution reads
   > `_effective_isolate()` from the parent chain" — `docs/design-v2-realms.md:96-98`

   and `plug` applies `ctx.isolate(...)` to a *scoped* context before `ctx.plugin`:

   ```python
   # backends/python/runtime.py:72-80
   def plug(ctx, component: dict, config=None):
       scoped = ctx
       for key, realm in (component.get("isolate") or {}).items():
           scoped = scoped.isolate(key, realm_label(realm))
       return scoped.plugin(component, config)
   ```

   Today the two agree only because every component is plugged onto the *root* context
   (`src/revl/run.py:225`), so the chain is one level deep and "own isolate map" == "effective
   isolate". A spawned instance plugged onto its spawner's context breaks that coincidence
   immediately.

### Sub-question: a spawned instance providing into its **parent's** realm

There is no compile-time model of this at all — and the runtime model says it happens *by
default*. A child fiber inherits the parent's effective isolate chain (above), so a spawned
`Session` that provides `conn` without its own `isolate` lands in whatever realm its spawner
was isolated into. The linker computes `SHARED_REALM` for it (`lower.py:3048`, the `.get(key,
SHARED_REALM)` default) and will therefore:

- **miss real conflicts** — N children all defaulting into the parent's realm collide on the
  same `(key, realm)` at runtime, while the linker's table shows one shared-realm entry; and
- **miss real edges** — see G3 below, where edges are built from the same `_realm` call.

Additionally, `realm_label` is a **process-wide** string→object registry
(`backends/python/runtime.py:63-69`), deliberately so that equal strings share a realm. With
per-instance realms that registry grows without bound and is never collected — a leak keyed
by a string that no longer has a live owner.

Finally, note that the runtime-side view of provisions is not even realm-aware: the REPL
namespace and the placement planner both collapse `key -> service` and `key -> owner`
globally, dropping the realm dimension entirely (`src/revl/run.py:73-78`,
`src/revl/placement.py:344`, `:346-349`).

---

## 2. G3 — acyclicity

### Type graph or instance graph?

**The type graph — and the compiler cannot tell the difference, because for it the two are
the same graph.** Nodes are component names:

```python
# src/revl/lower.py:3079-3101
    graph: dict[str, list[str]] = {entry["name"]: [] for entry in entries}
    indegree: dict[str, int] = {entry["name"]: 0 for entry in entries}
    edge_key: dict[tuple[str, str], str] = {}
    for entry in entries:
        for key in entry["inject"]:
            provider = provider_of.get((key, _realm(entry, key)))
            if provider == entry["name"]:
                name = entry["name"]
                raise RevlError(
                    program.filename, _line(name),
                    f"component {name} requires a key it provides itself (`{key}`) (G3)",
                    ...)
            if provider is not None:
                graph[provider].append(entry["name"])
                ...
```

Then a colored DFS over that name graph (`:3103-3126`) rejects any cycle.

The self-loop check at `:3086-3097` is the sharpest statement of the assumption in the
codebase: *"component X requires a key it provides itself"* is rejected outright. On an
instance graph that is the **normal** shape of a recursive spawner — a `Session` that
requires `session` and spawns a child `Session` providing `session` to it is a self-edge in
the type graph and a perfectly acyclic parent→child chain in the instance graph. There is no
mechanism, flag, or annotation anywhere in `_link` that distinguishes the two; the word
"instance" does not appear in `src/revl/lower.py` at all (only `isinstance` and the JS
reserved word `instanceof` at `:126`).

The one thing the graph *is* sensitive to is realms — `docs/design-v2-realms.md:67-69`:
"G3/loadOrder are realm-aware: an edge exists only where the consumer's realm for a key
matches the provider's — realm separation legitimately breaks cycles." That is the closest
existing machinery to instance separation, and it inherits every limitation from §1: static
labels only, flat lookup, no parent inheritance.

Secondary: `visit` (`:3107-3126`) is **recursive on the Python stack**, one frame per graph
node. Fine for a link-time-finite manifest, not for anything sized by runtime instance count
(this is only a concern if the graph were ever built over instances — noted for completeness).

---

## 3. G8 — enumerable boundary

### How the boundary is enumerated today

`_boundary` in `src/revl/__main__.py:75-140`. It walks the **lowered IR component bodies** —
not the manifest, not the runtime — and produces one row per component *declaration*:

```python
# src/revl/__main__.py:91-93
    for comp in ir.get("components") or []:
        stats = {"emissions": set(), "compensated": 0, "awaits": 0, "capabilities": {}}
```

```python
# src/revl/__main__.py:136-140
        report[comp["name"]] = {
            "emissions": sorted(stats["emissions"]),
            "capabilities": dict(sorted(stats["capabilities"].items())),
            "compensated": stats["compensated"],
            "awaits": stats["awaits"],
```

What it counts: emission call sites (`stats["emissions"]` is a **set of labels**,
`:99-107`), the declared capability scope of each, a count of compensated emits, a count of
`await`s, and transitively reachable `extern`s (`:126-134`). The CLI then prints it *per
manifest entry*, i.e. per declaration (`src/revl/__main__.py:515-560`), keyed off
`manifest["loadOrder"]` and `manifest["components"]`.

### What breaks with a dynamic instance count

- **The unit of enumeration is wrong, not the enumeration.** `emissions` is a set of
  *syntactic* call sites; a set of call sites is already instance-independent. What the
  audit prints — "component `Session` reaches `db.write`" — remains true for N instances.
- **Every count in the row becomes meaningless.** `compensated` and `awaits` are integers
  per declaration (`__main__.py:139-140`); with N instances the honest answer is
  "`compensated` × N" for an N nobody knows. The plan renderer treats these as absolute
  facts about the composition (`src/revl/plan.py:376-378`, `:650`, and `_emission_surface`
  at `:477-505`).
- **"Gained / lost irreversible reach" stops being a set difference.** `_emission_surface`
  (`src/revl/plan.py:477-505`) computes the after-surface as *a dict keyed by component
  name*, with retained components keeping their entry and replaced ones taking the
  candidate's. Withdrawing a declaration removes its reach from the surface. With runtime
  instances, the reach of a component is present iff *at least one* instance is live — a
  predicate the manifest cannot answer, and `plan()` explicitly disclaims runtime knowledge
  already (`src/revl/plan.py:441-444`: "the *inverses* each component replays depend on what
  it acquired at runtime and are not visible in a manifest").
- **`revl audit`'s own frame is the manifest.** It opens with
  `print("composition (providers first):", " -> ".join(manifest.get("loadOrder") or []))`
  (`src/revl/__main__.py:515`) and then iterates `manifest["components"]` (`:516`). A
  composition whose population is dynamic has no `loadOrder` to print.

So G8 itself is the *most* portable of the guarantees — the boundary is a syntactic set —
but every quantitative and every compositional statement built on top of it is per-declaration
and assumes multiplicity 1.

---

## 4. G4 (emission upper bound) and G6 (purity/confinement) across a spawn boundary

### Is there any mechanism that could bound what a spawned instance emits?

**No.** There is exactly one emission-bounding mechanism in the language, and it lives on
*service declarations*, not on components:

```python
# src/revl/lower.py:2775-2802 (in _lower_provide)
        # A service declaration is an *upper bound* on its providers' effects:
        # consumers bind to the service, not to this component, and a provider
        # may be purer than declared but never less. Without this, a plain
        # declaration hides an irreversible call from every consumer — and from
        # the G8 audit, which enumerates a caller's emissions by reading the
        # declarations of the methods it calls.
        if not decl.emission:
            caused_steps: dict[str, list] = {}
            caused, _caps = _method_emissions(mbody, env, caused_steps)
            if caused:
                ...
                raise RevlError(..., f"`{svc.name}.{method.name}` is declared plain, but this "
                                     f"implementation reaches {evidence}", ..., code="G4", ...)
```

and its capability-scoped refinement at `:2803-2827`. Both are checked against **one
provide-method body** — a lexically bounded piece of syntax inside one component.

A component's *activation body* has **no declared emission bound at all**. `ComponentDecl`
(`src/revl/parser.py:182-190`) carries no emission clause; the only per-statement obligation
is that an `emit` marker sit on something actually declared `emission`
(`src/revl/lower.py:2848-2877`). So there is nothing on a component that a spawner could
widen to cover its children, and nothing for the checker to compare a child's reach against.
Consequence: a spawner declaring `emission[db]` on a provide-method could spawn a child that
emits through `net`, and no rule in the current compiler is even *phrased* over that
relationship.

### The capability names themselves assume one instance

This is the subtlest G4 finding, and it is stated in the source as a load-bearing premise:

```python
# src/revl/lower.py:2353-2357 (_method_emissions docstring)
    Returns `(evidence, capabilities)`: the human-readable call sites, and the
    *set* of boundaries they cross (docs/capabilities.md). A call through a
    required key `db` is capability `db` — the key is composition-wide (G2
    makes it unique), so it names the same boundary to every reader; host code
    is named by the `emission` extern it reaches.
```

A capability is a *required key name*, and its meaning as a boundary identity is justified
by G2's global uniqueness of that key. Once N instances resolve the same key `db` to N
different providers (per-instance realms), `db` no longer "names the same boundary to every
reader" and the capability vocabulary loses its denotation. Note this is already *slightly*
false under realms — the docstring says "G2 makes it unique" but G2 is per-`(key, realm)`
since `lower.py:3050-3052` — it just hasn't bitten because realm labels are static and few.

### G6

G6 is by construction and survives: "purity outside effects -> by construction (statement
grammar)" (`src/revl/lower.py:10`); `fn` bodies have no syntactic path to an effect
(`docs/syntax-2.0.md:102-104`); captures are by-value snapshots (`docs/syntax-2.0.md:113-114`).
A `spawn`-shaped effect form would inherit that unchanged. What does *not* survive is the
adjacent confinement fact:

- **Config is admission-time-resolved and link-time-unknown, and the compiler relies on
  that.** Component config is type-checked only against literal defaults
  (`src/revl/lower.py:2448-2456`) and resolved by a runtime `ConfigSchema`
  (`backends/python/runtime.py:385-426`). `compile_files` has no config channel at all —
  which is the stated reason dynamic realms are rejected (`docs/design-v2-realms.md:50-57`).
  `spawn Session with { id }` is precisely "config carrying a runtime value", the thing the
  current design says it cannot see.
- **`RESOLVED_CONFIG` is a process-global keyed by name** (`backends/python/runtime.py:378`),
  and `_flush_config_trace` "attributes the parked resolution" through a single-slot module
  global `_pending_config` (`:371-382`). Two concurrent activations of the same component
  interleave into the same slot.
- **A provision cannot nest.** `provide` is top-level-only in a component body
  (`src/revl/parser.py:1026-1029`, "`provide` is not allowed inside a method body"), and the
  emitter refuses a nested one outright (`backends/python/emit.py:460-461`: "nested 'provide'
  inside a method body is not lowerable"). A spawned instance that provides is structurally a
  nested provision.

---

## 5. G7 — LIFO teardown

G7 has two halves, and only one of them is instance-blind.

**Intra-component teardown is genuinely dynamic and does *not* assume link order.** The
`Frame` accumulator is created fresh per activation, and inverses are replayed newest-first
off a runtime stack:

```python
# backends/python/runtime.py:266-276
class Frame:
    """One component activation's effect accumulator.

    The emitted ``apply`` creates a fresh ``Frame`` per activation and
    installs the component body as a single ``ctx.effect`` generator.  The
    generator's final ``yield frame.drain`` places the drain at the *top* of
    the runtime's LIFO disposer stack, so inverses accumulated after
    activation (provide-method ``effect`` steps, adopted below) are undone
    first, newest first, before the activation-time inverses run — exact
    component-level LIFO on a runtime that is only per-effect LIFO.
```

Nothing in the checker orders inverses statically; G5 is by construction
(`src/revl/lower.py:8-9`) and G7's only *compile-time* obligation is that a `verified fn` be
total (`src/revl/diagnostics.py:58`, `src/revl/lower.py:560-583`).

**Inter-component teardown assumes link-determined order everywhere.** Every teardown path
in the tree is `reversed(loadOrder)`:

| site | code | citation |
|---|---|---|
| `revl run` teardown | `for name in reversed(_load_order(ir)):  # consumers before providers` | `src/revl/run.py:238` |
| fault-test harness | `for name in reversed(order):` | `src/revl/fault.py:251` (order from `loadOrder`, `:208-209`) |
| multi-process runner | `for label, fiber in reversed(fibers):` | `src/revl/_process_runner.py:154` |
| `withdrawal` query | `order = [name for name in reversed(index.load_order) if name in gone]` | `src/revl/query.py:402`, contract at `docs/queries.md:129` |
| `revl plan` prediction | `teardown = [name for name in reversed(ordered) if name in torn_down]` | `src/revl/plan.py:373` |

`loadOrder` itself is a Kahn topological sort over the name graph, computed once at link
time and frozen into the IR:

```python
# src/revl/lower.py:3128-3145
    order: list[str] = []
    ready = [e["name"] for e in entries if indegree[e["name"]] == 0]
    while ready:
        name = ready.pop(0)
        order.append(name)
        ...
    return {"components": entries, "loadOrder": order}
```

So: **the teardown *rule* is dynamic (a runtime stack), the teardown *plan* is static (a
frozen name list).** A dynamically spawned instance has no position in `loadOrder` — it is
not in `entries`, so it is not in the sort — and therefore every one of the five sites above
would simply skip it. The fault harness's LIFO judgement (`src/revl/fault.py:325-337`,
`outcome.lifo_violation`) checks accumulation order *within one armed activation* via a
process-global single-slot probe (`backends/python/runtime.py:241-253`, "Process-global and
single-slot on purpose: a fault test drives exactly one activation at a time"), which is a
harness limitation rather than a language one, but it does mean no existing test apparatus
can observe two concurrent activations of one component.

---

## 6. Inventory: everywhere instance count, ordering, or identity is baked in at link time

### 6.1 The linker (`src/revl/lower.py:3002-3145`)

| line | what is baked in |
|---|---|
| `3006` | `lines = {comp["name"]: decl.line for comp, decl in zip(components, program.components)}` — positional zip of lowered components to declarations; assumes exactly one lowered component per declaration, in order |
| `3008-3032` | `entries`: the finite list. Ambient entries reconstructed from the running manifest by name (`:3010-3019`), new ones appended (`:3021-3031`) |
| `3040` | `by_name = {entry["name"]: entry for entry in entries}` — name→entry is injective |
| `3047-3048` | `_realm(entry, key)` — flat, own-declaration-only realm lookup, shared-realm default |
| `3053-3077` | G2: `provider_of: dict[(key, realm), name]` — one provider per pair, globally |
| `3079-3101` | G3 edges over the name graph; self-provision rejected outright |
| `3103-3126` | DFS cycle detection over names |
| `3128-3145` | `loadOrder`: total order over names, frozen into the IR |

### 6.2 Admission / hot-swap (`src/revl/compiler.py:288-317`)

```python
# src/revl/compiler.py:292-303
    if manifest is not None:
        running = manifest.get("manifest", manifest)
        dropped = set(replacing) | {comp.name for comp in merged.components}
        ambient = {
            "services": manifest.get("services") or {},
            "components": [
                entry for entry in (running.get("components") or [])
                if entry.get("name") not in dropped
            ],
        }
```

**Replacement is by name.** "A compiled component whose name matches a running one implicitly
replaces it (the hot-swap case)" (`src/revl/compiler.py:181-186`). With N live instances of
one declaration, "replace the component named `Session`" is ambiguous: it names a
declaration, and the gate has no vocabulary for "which of the N". The planner mirrors the
same set arithmetic (`src/revl/plan.py:221-231`) and renders the case as "a new instance of
the component takes the provision over" (`src/revl/plan.py:611-613`) — the only place in the
tree that uses the word "instance", and it means "a replacement of the single one".

Also on this path: interface drift is checked per service name against the running manifest
(`src/revl/lower.py:1618-1626`), and admission is refused while holes remain
(`src/revl/holes.py:70-93`). Neither is instance-sensitive, but both are per-declaration
gates run once per admission.

### 6.3 The IR schema

`docs/backend-ir.md:41-51` — a `components` array of `{name, config, requires, provides,
body}`. There is:

- **no instance count, cardinality, or multiplicity field** anywhere;
- **no lifetime scope** other than "the component" — the `provide` step installs at a key and
  "the withdrawal inverse is derived by the backend" (`docs/backend-ir.md:61`);
- **`manifest.loadOrder`**, a frozen name list (`src/revl/lower.py:3145`);
- `isolate: {key: realm}` and `intercept: {key: metadata}` emitted per component only when
  non-empty (`src/revl/lower.py:2604-2609`, `docs/design-v2-realms.md:82-86`) — the realm map
  is a *property of the declaration*, so it cannot vary per instance by construction.

The IR therefore assumes one instance per component declaration in the strongest possible
sense: the declaration *is* the runtime object. The emitter makes this literal —
`backends/python/emit.py:499-518` emits `Session = { 'name': ..., 'inject': ..., 'apply': ...,
'isolate': ... }` as a module-level singleton dict, and `plug` reads `component["isolate"]`
off it at plug time (`backends/python/runtime.py:72-80`), so a per-instance realm would have
to mutate a shared dict.

### 6.4 Consumers of `loadOrder` / name identity outside the linker

| file:line | assumption |
|---|---|
| `src/revl/run.py:68-70` | `_load_order` falls back to declaration order |
| `src/revl/run.py:73-78` | `_key_to_service`: `key -> service`, realm-blind, one provider per key |
| `src/revl/run.py:222-226` | one fiber per name, stored in `self.fibers: dict[str, object]` |
| `src/revl/run.py:238-244` | teardown by `reversed(loadOrder)` |
| `src/revl/run.py:248-249` | REPL namespace `{key: self.root.get(key) for key in _key_to_service(self.ir)}` — one object per key |
| `src/revl/query.py:105-115` | `components`/`entries`/`provider_of` all name-keyed |
| `src/revl/query.py:122-127`, `:261-280` | scope ids are `f"{name}:{key}.{method}"` — a name-derived, instance-free address for every analyzable scope |
| `src/revl/plan.py:154`, `:230`, `:373` | before/after `loadOrder` and predicted teardown |
| `src/revl/placement.py:344` | `owner = {key: p ...}` — one process owns each key, realm dimension dropped |
| `src/revl/placement.py:350` | `load_order` drives per-process component lists |
| `src/revl/fault.py:208-215`, `:251` | bring-up and teardown over `loadOrder`; the armed component is named once |
| `src/revl/mcp/server.py:60-72` | wire summary is `loadOrder` + one row per manifest entry |
| `src/revl/mcp/session.py:316-329` | live state is `[{name, state}]` from `driver.fibers` |
| `backends/python/runtime.py:63-69` | `realm_label`: process-wide, never-released string→label registry |
| `backends/python/runtime.py:241-253` | fault probe: process-global, single-slot, matched by component name |
| `backends/python/runtime.py:378` | `RESOLVED_CONFIG[name]` — last activation of a name wins |
| `backends/python/emit.py:1045-1047` | emitter refuses duplicate component names |

---

## 7. HARD blockers vs MECHANICAL generalizations

The distinction used below: **MECHANICAL** = the *rule* stays sound with dynamic instances
and the code iterates over the wrong collection (or reports a per-declaration number that
should be per-instance). **HARD** = the rule as stated is *unsound or vacuous* with dynamic
instances and needs a genuinely new formulation, not a new loop.

### HARD (needs a new rule)

**H1. G2 disjointness over an unbounded provider set.** `src/revl/lower.py:3053-3077`. The
check is a link-time injectivity proof on a finite table. With N unknown instances the table
cannot be built, and the property "at most one provider per `(key, realm)`" has to become a
statement about *runtime* realm freshness — i.e. a proof that each instance's realm is
distinct — which is exactly the proof the parser says it cannot do
(`src/revl/parser.py:1112-1120`) and the design doc names as the open prerequisite
(`docs/design-v2-realms.md:50-57`). Iterating a different collection does not help; there is
no collection.

**H2. Dynamic realm labels.** `src/revl/parser.py:1107-1123`. A hard, deliberate,
documented refusal with a soundness argument attached. Any per-instance realm is a
config-derived realm. This is the single explicit blocker already written down in the tree.

**H3. G3 self-provision and cycles on the type graph.**
`src/revl/lower.py:3086-3097` (self-loop) and `:3103-3126` (DFS). "Component X requires a
key it provides itself" is *categorically* rejected; on an instance graph it is the normal
shape of recursive spawning. Distinguishing "cycle in the type graph, chain in the instance
graph" requires a new well-foundedness rule over the spawn relation — a different theorem,
not a different iteration. (Note the current linker cannot even *represent* the distinction:
`graph` is keyed by `str`.)

**H4. G4 has no spawn-boundary mechanism to generalize.** `src/revl/lower.py:2775-2827` is
the only emission upper bound, and its scope is one provide-method body checked against one
service declaration. `ComponentDecl` (`src/revl/parser.py:182-190`) carries no emission
clause, so there is nothing on a spawner to widen. A rule of the form "a spawner's bound
covers its children's" does not exist in any form today and must be invented.

**H5. Capability identity is derived from G2's global key uniqueness.**
`src/revl/lower.py:2353-2357`: *"A call through a required key `db` is capability `db` — the
key is composition-wide (G2 makes it unique), so it names the same boundary to every
reader."* If instances resolve the same key to different providers, the capability
vocabulary in `docs/capabilities.md` stops denoting a boundary and the `emission[db]` subset
check (`src/revl/lower.py:2803-2827`) compares incomparable things. This one is easy to miss
because it reads as a comment, not a check.

**H6. Realm resolution is flat in the checker and hierarchical at runtime.**
`src/revl/lower.py:3047-3048` versus `docs/design-v2-realms.md:96-98` +
`backends/python/runtime.py:72-80`. Today the two agree only because every component is
plugged onto the root context (`src/revl/run.py:225`). The moment a child is plugged onto its
spawner's context, the linker's realm for that child is wrong (it says shared; the runtime
says "whatever the parent was isolated into"), and both G2 (H1) and G3 edges silently
mis-resolve. This is a soundness gap that opens the instant spawn exists, independently of
whether per-instance realms are ever added.

**H7. Lifetime scope: a provision cannot nest, and a per-call effect outlives the call.**
`src/revl/parser.py:1026-1029` and `backends/python/emit.py:460-461` refuse a nested
`provide` outright; `backends/python/runtime.py:266-276` + `emit.py:429-440` put every effect
acquired inside a provide-method into the *component's* accumulator, so it lives until the
component tears down. There is exactly one teardown scope per component and no sub-scope.
An instance with its own lifecycle and teardown is a new scope kind — new IR, new lowering,
new rule about what may cross it. Related: A2 (`src/revl/lower.py:2515-2523`, "acquisition
after `provide`") is stated over the single activation body and would need re-derivation for
a scope that acquires *while* ACTIVE.

**H8. Hot-swap identity.** `src/revl/compiler.py:295` (`dropped = set(replacing) | {comp.name
...}`). Replacement is by declaration name. "Replace `Session`" with N live instances is
undefined — swap all, swap none, swap on next spawn are three different semantics and the
gate has no way to say which. This is a semantics question, not a loop.

### MECHANICAL (the rule generalizes; the code iterates the wrong collection)

**M1. `loadOrder` and every `reversed(loadOrder)` teardown.**
`src/revl/lower.py:3128-3145`, `src/revl/run.py:238`, `src/revl/fault.py:251`,
`src/revl/_process_runner.py:154`, `src/revl/query.py:402`, `src/revl/plan.py:373`. The
*rule* (consumers before providers; LIFO) is already dynamic at the effect level
(`backends/python/runtime.py:266-288`) and is correct for any population. These sites just
need to walk live instances instead of a frozen name list. The static list remains correct
for the statically-composed subset.

**M2. G8 emission enumeration.** `src/revl/__main__.py:75-140`. `emissions` and
`capabilities` are sets of *syntactic* labels and are already instance-independent — one row
per declaration is the right answer. Only the integer counters (`compensated`, `awaits`,
`__main__.py:139-140`) and the per-declaration presence semantics of `_emission_surface`
(`src/revl/plan.py:477-505`) need re-basing onto "reach is present iff ≥1 instance may be
live", which is a reporting change.

**M3. Every name-keyed index in the analysis and tooling layer.**
`src/revl/query.py:105-127` (`components`, `entries`, `scopes_of`, scope ids
`f"{name}:{key}.{method}"`), `src/revl/mcp/server.py:60-72`, `src/revl/mcp/session.py:316-329`,
`src/revl/run.py:225-226` (`self.fibers: dict[str, object]`). These want an instance id
alongside the declaration name. Scope ids in particular are the right *analysis* granularity
already — a scope is syntax, not an instance — and only the live-state views need the extra
key.

**M4. Runtime adapter globals.** `RESOLVED_CONFIG[name]`
(`backends/python/runtime.py:378`), the single-slot `_pending_config`
(`:371-382`), the process-global fault probe (`:241-253`), and the never-released
`_REALM_LABELS` registry (`:63-69`). All are "keyed by name, should be keyed by instance"
(or, for the probe, a harness limitation). No rule changes.

**M5. Emitter singleton shape.** `backends/python/emit.py:499-518` emits one module-level
plugin dict per component and `:1045-1047` refuses duplicate names. The plugin dict is
*already* re-pluggable — cordis `ctx.plugin(component, config)` takes it as a value — so the
emitted artifact does not itself forbid N instances; only `isolate` being baked into the
shared dict (`emit.py:512-515`, read by `runtime.py:78`) does, and that is a parameterization
change.

**M6. `key -> service` / `key -> owner` collapses.** `src/revl/run.py:73-78` and
`src/revl/placement.py:344`. Both already drop the realm dimension that `_link` computes,
which is a pre-existing bug-shaped simplification rather than a guarantee. Straightforward to
key by `(key, realm)` / `(key, instance)`.

**M7. Duplicate-name enforcement.** `src/revl/compiler.py:209-212`,
`src/revl/lower.py:1653-1656`, `backends/python/emit.py:1045-1047`. These are about
*declaration* uniqueness and stay correct; they only become confusing if instance identity is
ever spelled with the same `str`.

### One-line summary

The compiler's static-instance assumption is not spread thin — it is **concentrated in
`_link` (`src/revl/lower.py:3002-3145`), which is the only place G2 and G3 exist**, plus the
`loadOrder` list it emits. G8, G5, G6 and the intra-component half of G7 are already
instance-blind or by-construction and generalize for free. The genuinely new theory needed is
(a) per-instance provision disjointness without a link-time table, (b) a well-foundedness rule
that separates the type graph from the instance graph, (c) an emission bound that composes
across a spawn boundary, and (d) a nested lifetime scope in the IR.
