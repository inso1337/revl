# Realm labels: one provision slot per (realm, key)

**Status:** reconnaissance + tier survey. No compiler or backend behaviour was
changed by this note; the python and TypeScript defects it measures were
already fixed on `main` by #1549 (`c6de6cdd1`, "Realm labels are per (realm,
key)"), and the one tier that is still defective is filed below as a described
follow-up rather than patched here.
**Issue:** #1543 (reported from #1540).
**Date:** 2026-10-06.

## The defect as reported

`isolate db in realm("wa")` plus `isolate api in realm("wa")` left the
composition FAILED, the provision store holding a single entry `<realm wa>`,
and a resolve of `api` returning the `Db` object. The report attributed this
to the python runtime: `runtime.realm_label` mapped a realm string to one
label object, and cordis-py keys its provision store by that label alone.

## What the measurement actually shows

The second half of that attribution is **not** true of cordis-py. cordis-py
already mints **one label per key inside a realm**: `loader.Realm.access`
returns `unique_symbol(f"{key}{suffix}")`, and `reflect.provide` writes
`self_.ctx.root.__dict__["_isolate"].setdefault(name, unique_symbol(name))`,
one entry per provision key. On both the pre-fix and post-fix trees the root
context's isolation map holds two distinct symbols:

```
== root ctx isolation map (key -> label):
     'db' -> Symbol('db') (id 0x106919930)
     'api' -> Symbol('api') (id 0x10691a470)
```

The collapse happened one level up, in **revl's own shim**: `realm_label(name)`
interned one `_RealmLabel` per realm *string* and handed the same object to
every key in that realm, which is what made cordis-py's store — correctly keyed
by label — see one slot.

So the fix belongs in revl's shim (where #1549 put it), not upstream in
cordis-py. No fork change and no pin bump is warranted: the vendored fork
`backends/python/.cordis-py` at `CORDIS_PY_PIN`
(`1c5e6f17ab538bf01012f9d72ce0cfa978d91b3`) is already per-key correct, and so
is cordis-ts. Bumping the pin for this defect would have changed nothing.

## BEFORE / AFTER

Driver: `/private/tmp/repro_1543.py`, which compiles the two-keys-one-realm
program, loads it through `revl.run`, and dumps the live driver's
`root._isolate` map, `root.reflect.store`, `resolved_keys()`, and what each
key actually resolves to. BEFORE runs against the pre-fix commit
`cd8876212` (`c6de6cdd1^`); AFTER runs against `main` `f2b1f1103`.

BEFORE (`runtime.realm_label` takes one argument — realm only):

```
== realm_label('wa') is realm_label('wa'): True -> <realm wa>
== fibers: {'Db': 'ACTIVE', 'App': 'FAILED', 'Front': 'ACTIVE'}
     App FAILED: AttributeError: service "api" has been registered at <Db>
== resolved_keys(): ['api', 'db']
== provision store (label -> provider): 1 entry(ies)
     <realm wa> -> Impl <cordis.reflect.Impl object at 0x1069ef080>
== what resolving each key in realm 'wa' actually reaches:
     realm 'wa' key 'db': label <realm wa> -> _Db
     realm 'wa' key 'api': label <realm wa> -> _Db
```

AFTER:

```
== runtime.realm_label signature: (name: 'str', key: 'str') -> '_RealmLabel'
== realm_label('wa','db') is realm_label('wa','api'): False
== fibers: {'Db': 'ACTIVE', 'App': 'ACTIVE', 'Front': 'ACTIVE'}
== resolved_keys(): ['api', 'db']
== resolve_key('db') -> _Db   has .ping: True  has .up: False
== resolve_key('api') -> _Api  has .ping: False has .up: True
== call('db','ping') -> {'result': 'db', 'trace': []}
== call('api','up') -> {'result': 'api:db', 'trace': []}
== provision store (label -> provider): 2 entry(ies)
     <realm wa/db> -> Impl ...
     <realm wa/api> -> Impl ...
```

Two observations worth keeping:

- **`resolved_keys()` agreed with the truth in both worlds** — it listed
  `['api', 'db']` before and after. On the pre-fix tree that made it *wrong*
  (nothing resolved `api`; the store had no `api` slot), which is the
  "loaded means loaded" report the issue flags. It is correct now, but the
  agreement is a consequence of the store fix, not of a change to
  `resolved_keys()` itself.
- **The CLI does not surface the confusion.** `python -m revl run … --backend
  py --once` exits 0 on the pre-fix tree and merely prints
  `fiber | App | UNLOADING -> FAILED`; `:keys` still lists `api: Api -> up`.
  The store dump is what exposes the authority confusion, so a CLI-only check
  is not sufficient evidence for this class of defect.

## Per-tier survey of the realm label registries

Every tier was measured; "registry" means the structure the tier keys a
provision by.

| tier | how a provision is keyed | #1543 shape? | evidence |
|---|---|---|---|
| python | `_REALM_LABELS[(name, key)]` in `backends/python/runtime.py`; cordis-py's `_isolate` is one symbol per key | fixed by #1549 | BEFORE/AFTER transcripts above; 3 py tests |
| typescript | `realmLabels: Map<string, symbol>` keyed by `JSON.stringify([name, key])` in `backends/typescript/runtime.ts` | fixed by #1549 | real `node` run of the emitted program: all three fibers ACTIVE, `ping == "db"`, `up == "api:db"` |
| rust | `_revl_realm(label)` mints one `cordis::Isolation` per realm **string** (`REVL_REALM_TAG \| index`, `backends/rust/emit.py:5281`) | **still defective** — see below | strict xfail: `api` collides with `db` (`DuplicateService`) |
| go | `provKey{realm, key}` (`forks/stc-go/key.go`) | clean by construction | guard test passes |
| java | `ServiceRegistry`'s store key is `ServiceKey.of(key.type(), realm)`; the realm override is `Map<Class<?>, String>` (`cordis4j-core/.../internal/ServiceRegistry.java:42,72,91-92,153-154`), and the emitted `ctx.isolate(Database.class, "wa")` / `ctx.isolate(Api.class, "wa")` binds it per service **class** | clean by construction — the key dimension is carried by the class | `cordis4j-core` compiled from `1na-ko/cordis4j@82072f4`; `tests/test_realm_conformance.py::test_cordis4j_realm_conformance` now runs for real and xfails on cordis4j's *other*, already-characterized divergence (equal realm strings share instead of refusing) |
| wasm | there is no store: `_scoped_key` composes the realm into the capability address (`wa/db`, `wa/api`) | clean by construction | emitted `provide:wa/db.ping`, `coeffect:wa/db`, `provide:wa/api.up`; `test_cordis_wasm_realm_conformance` passes against the real cordis-wasm substrate |

Two of these tiers deserve a sentence more than a table cell.

**Java is a different defect, not this one.** cordis4j's realm override is
per *type*, so `Database.class` and `Api.class` in realm `wa` are two distinct
store keys and can never collide — the #1543 shape is structurally absent.
What cordis4j does get wrong is the *opposite* direction: two providers of the
same type in the same realm string both load instead of conflicting (the (H)
"equal strings share" expectation). That is already documented in
`docs/notes/runtime-parity-local-realms.md`, pinned by the conformance xfail
above, and tracked separately; it is not a regression of #1543 and is not
touched here.

**Wasm has no registry to confuse.** Because the realm is part of the
capability address, `Front` — which consumes both keys in one realm — imports
two different coeffects. The guard test added alongside this note asserts
exactly that, so the survey's claim is executable rather than prose.

## Follow-up: the rust tier is still defective

`backends/rust/emit.py:5281` `_collect_realm_labels` collects distinct realm
**strings**, and the emitted helper is

```rust
pub fn _revl_realm(label: &str) -> cordis::Isolation   // REVL_REALM_TAG | index
```

so every key in realm `wa` receives the *same* `Isolation`, and cordis-rs
(which keys `implementations` by `Isolation` alone) sees `api` collide with
`db` (`DuplicateService`). This is precisely the #1543 defect, one tier over,
and it is **not** fixed here: the fix changes the emitted helper in
`backends/rust/emit.py` **and** `selfhost/emit_rust.rvl`, which is inside the
`crates/revl-gate` digest, so it needs a sequenced `crates/revl-gate`
regeneration rather than a drive-by edit. It is pinned by a strict xfail in
`tests/test_realm_key_labels_1543.py` so it cannot silently pass.

`docs/design-v2-realms.md:105-120` already records the same finding.

## Non-goal

The emitted in-language test harness `_revl_call` in `backends/python/emit.py`
still resolves with `root.get(key)` and therefore misses isolated keys. That is
emitted code, so changing it moves goldens and must stay byte-identical to
`selfhost/emit_py.rvl`; it is a separate, golden-moving change and is
deliberately out of scope for #1543.

## Residual uncertainty

- The rust gap is *described and pinned*, not fixed; closing it requires the
  gate-crate regeneration above.
- The java and wasm rows are measured against a locally compiled
  `cordis4j-core` and the local `cordis-wasm` prototype respectively. Neither
  is part of CI's default path (`REVL_CORDIS4J_CLASSES` unset and the
  cordis-wasm venv absent both skip), so those rows are reproducible but not
  CI-enforced.
- The two-keys-one-realm program cannot be emitted for wasm with `Str` returns
  (a `Str` crossing a component boundary is issue #1601), so the wasm guard
  uses `Int` returns. The realm composition under test is identical; only the
  payload type differs.
