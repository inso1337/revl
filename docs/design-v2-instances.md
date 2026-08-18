# Instance-parametric components (design)

Status: **draft — open questions unresolved.** Nothing here is implemented.

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
