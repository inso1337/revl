# Runtime parity: local realms & instance-parametric components

**Status:** reconnaissance. No compiler or backend behaviour was changed.
**Probes:** `probes/local-realms/` (one per tier, plus `run-all.sh`).
**Date:** 2026-08-18.

## The question

revl is designing *instance-parametric components*: runtime-created
instances (a session, a connection, a tenant), each with its own lifecycle
and teardown. Against cordis-py this is already known to work by giving
each instance its own **local realm** — `Context.isolate(name)` with no
`label`, which mints a fresh identity per call.

For each of the other four tiers:

- **A.** Can the runtime hold two or more simultaneous live instances of
  the same plugin/component?
- **B.** Is there an identity-keyed / *local* realm equivalent — can two
  instances be isolated from each other **without choosing a distinct
  label string** — or is isolation label-keyed only?
- **C.** Is per-instance disposal independent, and is teardown still LIFO
  within an instance?
- **D.** If there is no local-realm equivalent: what would it take, and is
  the gap in the runtime library or only in revl's emitter/shim?

## Method

Every tier is probed by driving its **runtime library directly** — no revl
compilation in the loop — with the same five-part shape:

| sub-probe | what it loads | reference expectation |
|---|---|---|
| **A** | the same component twice into the *same* context | refused (G2 provision disjointness) |
| **A2** | twice into *two plain child contexts*, no isolation | refused — a child context is not a realm |
| **B1** | twice into two isolates sharing **one label** | refused — one label is one realm |
| **B** | twice into two **unlabelled** isolates | both live, each with its own provision |
| **C** | dispose one instance | the other stays live; the disposed one reverts LIFO |

`probes/local-realms/python/probe.py` is the **control**: it reproduces the
cordis-py baseline from scratch, so any tier's answer is a comparison
against a re-verified reference rather than against a remembered one.

### Everything below marked VERIFIED actually ran

All five tiers ran on this machine, in one pass, via
`probes/local-realms/run-all.sh`. Toolchains present: Node v25.6.1,
cargo 1.85.1 (warm `~/.cargo` registry with `cordis-rs` 0.3.0),
OpenJDK 26.0.2 (Homebrew keg — *not* on the default `PATH`; the run needs
`PATH=/opt/homebrew/opt/openjdk/bin:$PATH`), Python 3.14 with `wasmtime`
47.x, and a checkout of the `cordis-wasm` prototype at
`~/Projects/cordis-wasm`. cordis-py and cordis4j were cloned from source
(`inso1337/cordis-py@harden-fiber-lifecycle`, `1na-ko/cordis4j`);
cordis4j-core was compiled with `javac --release 21`.

**Nothing in this report is UNVERIFIED.** Source citations are given so
the mechanism can be checked, but each A/B/C answer is backed by a run.

## Per-tier answers

| tier | A — two live instances? | B — local (identity-keyed) realm? | C — independent disposal + LIFO? | D — gap |
|---|---|---|---|---|
| **cordis-py** (control) | **YES** — VERIFIED | **YES** — VERIFIED: `isolate(name)` with no label mints `Symbol('kv')` per call; two calls compare distinct | **YES** — VERIFIED | n/a |
| **cordis** (TypeScript) | **YES** — VERIFIED | **YES** — VERIFIED: `isolate(name)` → `label ?? Symbol(name)`, byte-for-byte the same mechanism | **YES** — VERIFIED | **revl's shim only.** `runtime.ts:27-37` interns string→symbol process-wide, so revl can only reach the *global* form. |
| **cordis-rs** (Rust) | **YES** — VERIFIED | **YES** — VERIFIED: `Context::isolate(name)` calls `new_isolation()`, a monotonic `Isolation(u64)` counter; observed `Isolation(3)` vs `Isolation(4)` | **YES** — VERIFIED | **revl's emitter only.** `backends/rust/emit.py:1145-1149` emits `isolate_with(key, _revl_realm(label))`, a *hash of the label string* — never `isolate()`/`new_isolation()`. |
| **cordis4j** (Java) | **YES** — VERIFIED | **YES, and more cheaply than the reference** — VERIFIED: `ctx.fork()` alone gives an instance its own provision slot, no label at all. There is exactly one `isolate` overload and it takes a `String` (reflection-checked in-probe), but you never need it. | **YES** — VERIFIED | **revl's emitter only** — but see the divergence below; the Java tier's isolation model is structurally different from the reference. |
| **cordis-wasm** | **YES** — VERIFIED | **NO.** VERIFIED: the runtime has no realm/isolate concept at all (`Runtime`'s public surface contains no realm-, isolate-, scope-, or namespace-shaped member). Two instances are only possible with **different key strings baked into the module bytes** at compile time. | **YES** — VERIFIED | **the runtime library.** A local realm needs a new per-fiber key-prefix applied at link and publish time; the emitter alone cannot express it without emitting a distinct module per instance. |

### Exact runtime API corresponding to cordis-py's local realm

| tier | local-realm API | file:line |
|---|---|---|
| cordis-py (reference) | `Context.isolate(name, label=None)` → `unique_symbol(name)` when `label is None` | `backends/python/.cordis-py/src/cordis/context.py:74` |
| cordis-py (loader form) | `LocalRealm(entry)` vs `GlobalRealm(label)`, chosen by `label is True` | `.../cordis/loader.py:508`, `:518`, decision at `:536-546` |
| cordis (TS) | `isolate(name, label) { shadow[name] = label ?? Symbol(name) }` | `backends/typescript/node_modules/cordis/lib/index.js:1417-1421`; declared `isolate(name: string, label?: symbol)` at `lib/context.d.ts:27` |
| cordis-rs | `Context::new_isolation() -> Isolation` and `Context::isolate(name)` (which calls it); `isolate_with(name, label)` is the global form | `cordis-rs-0.3.0/src/context.rs:183-192` (`isolate_with` at `:201`, `Isolation` at `:16-33`) |
| cordis4j | **no identity-keyed isolate.** The only overload is `<T> Context isolate(Class<T> type, String realm)`. The *equivalent capability* is `Context fork()`, because the service store is per-context. | `cordis4j-core/.../core/Context.java:124`; `internal/ContextImpl.java:160-168`; `fork()` at `internal/ContextImpl.java:279-284`; per-context store at `internal/ServiceRegistry.java:41` |
| cordis-wasm | **none.** The coeffect table is a flat `dict[str, Provider]`; imports resolve by the raw key string off the module's import section. | `cordis-wasm/runtime.py:92` (table), `:126-138` (`plug`, provision-conflict check), `:288-293` (import resolution), `:330-338` (`_publish`) |

## The one real divergence: cordis4j scopes provisions by context, not by realm

This contradicts nothing you told me about cordis-py, but it does mean the
Java tier does **not** implement the reference model, and a design written
against "isolation is realm-symbol-keyed on a store global to the root"
will be wrong on one of five tiers.

On cordis-py / cordis / cordis-rs the reflect store is **one flat map at
the root**, keyed by `(isolate-symbol-for-name)`. A child context is not a
boundary: `A2` is refused on all three.

```
A2/two extend() children, no isolate -> AttributeError: service "kv" has been registered at <Store[s1]>   (py)
A2/two extend() children, no isolate -> Error: service "kv" has been registered at <Store[s1]>            (ts)
A2/two extend() children, no isolate -> CordisError { code: DuplicateService, ... }                       (rs)
```

On cordis4j each `ContextImpl` owns its **own** `ServiceRegistry.store`
(`internal/ServiceRegistry.java:41`) and resolution walks the parent chain
(`effectiveRealm`, `:57-68`). Consequences, both VERIFIED:

```
A2/two forks, no isolate -> BOTH LOADED
A2/fa.kv -> s1  fb.kv -> s2  root.kv -> null

B1/same realm string -> BOTH LOADED
B1/same1.kv -> s1  same2.kv -> s2
```

1. **`fork()` is already a local realm.** Two instances in two forks each
   get their own `kv` with no label anywhere. That is the capability
   instance-parametric components need, for free.
2. **Realm *labels* do not intern globally at the core-Context level.**
   Two sibling `isolate(Kv.class, "shared")` children do **not** join one
   realm — contrary to the paper's §5.2.1 global-realm convention and to
   `docs/design-v2-realms.md`'s "equal strings = same realm". The
   global-realm behaviour exists one layer up, in `Loader`, which interns
   realms by isolate-chain path (`core/Loader.java:40-60`, `:341-352`) —
   a layer revl's Java emitter does not use (it emits
   `ctx = ctx.isolate(<Svc>.class, "<label>")` directly,
   `backends/java/emit.py:2094-2096`).

So on cordis4j, revl's *global* realm semantics are the ones that are
currently unimplementable without going through `Loader`, and the *local*
realm semantics are the ones that come for free. That is the exact inverse
of every other hosted tier.

Also worth recording: cordis4j enforces G2 with `SupplyConflictException`
only *within one context* — `Two active components supply
ServiceKey[type=interface Probe$Kv, qualifier=]: fiber #1 and fiber #2`
(`internal/ServiceRegistry.java:224-236`). revl's linker still rejects a
composition that double-provides a key, so the guarantee holds; but the
runtime is not a backstop for it on this tier the way it is on the others.

## cordis-wasm: isolation is a compile-time string, not a runtime call

The wasm tier has no realm registry to parametrise. What revl calls a
realm is lowered by `backends/wasm/emit.py:151-155` into the module's own
import/export namespace (`provide:tenant_a/kv.get`), plus an advisory
`revl:isolate` custom section (`:163-179`). The probe shows both halves:

```
A/same-runtime second plug -> ValueError: provision conflict on 'kv': s1 / s2
B/two mangled-key instances -> states {'s1': 'active', 's2': 'active'}
B/coeffect table keys -> ['tenant_a/kv', 'tenant_b/kv']
```

Two instances therefore require **two distinct compiled modules**. An
instance-parametric component whose instance count is only known at
runtime cannot be expressed this way: you would have to emit (or rewrite)
a module per instance.

**What it would take.** The change is small and lives in
`cordis-wasm/runtime.py`, not in revl:

- give `Fiber` a `realm: str = ""` set by `plug(name, code, realm=...)`;
- prefix on resolve — `self.table[fiber.realm + key]` at `:293`;
- prefix on publish — `self.table[fiber.realm + key]` at `:332`;
- scope the provision-conflict check at `:131-137` to the same prefix.

That is roughly ten lines and it makes the realm a runtime value, at which
point revl's wasm emitter can stop mangling names and pass an instance id
instead. Until then this tier is the blocker.

## Bottom line

**"Each instance gets its own local realm" is implementable on four of the
five tiers today, and on the fifth only after a small runtime change.**

- **cordis-py, cordis (TS), cordis-rs**: the local realm already exists in
  the runtime library, verified live. Nothing is missing but revl's own
  plumbing — all three revl shims intern a *string* into a realm identity
  (`backends/python/runtime.py:44-78`,
  `backends/typescript/runtime.ts:27-37`,
  `backends/rust/emit.py:1145-1149`+`:1184-1195`) and so can only ever
  reach the global form. Exposing the local form is an emitter/shim
  change: stop interning, call the unlabelled `isolate(key)` /
  `new_isolation()` per instance.
- **cordis4j**: the capability exists and is *stronger* than the reference
  (a bare `fork()` isolates), but the tier's model is structurally
  different — per-context stores, no global interning below `Loader`. Any
  design text that says "equal realm strings are one realm" is false here.
  Emitter change only; no runtime change.
- **cordis-wasm**: **no** local-realm equivalent, and the gap is in the
  runtime library, not the emitter. ~10 lines in `runtime.py` to add a
  per-fiber key prefix.

Nothing found contradicts the two cordis-py facts you established; the
control probe reproduces both verbatim, including the
`service "kv" has been registered at <Store>` message.

## Reproducing

```sh
PATH=/opt/homebrew/opt/openjdk/bin:$PATH \
CORDIS_PY=<clone of inso1337/cordis-py@harden-fiber-lifecycle> \
CORDIS4J=<clone of 1na-ko/cordis4j> \
CORDIS_WASM=~/Projects/cordis-wasm \
PY=<python with pyyaml, watchdog, wasmtime> \
probes/local-realms/run-all.sh
```

Individual tiers:

```sh
PYTHONPATH=$CORDIS_PY/src $PY probes/local-realms/python/probe.py     # -> PY_PROBE_OK
cd probes/local-realms/typescript && npm install && node probe.ts     # -> TS_PROBE_OK
cd probes/local-realms/rust && cargo run --offline --bin probe        # -> RS_PROBE_OK
# java: javac cordis4j-core, then javac+java probes/local-realms/java/Probe.java
CORDIS_WASM=~/Projects/cordis-wasm $PY probes/local-realms/wasm/probe.py  # -> WASM_PROBE_OK
```
