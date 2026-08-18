# revl v2 — isolation realms & coeffect interception

**Status:** implemented (merged into `v2.0`) — cordis-py, cordis (TS), and
the wasm substrate; runtime-verified on cordis-py and cordis v4.

The paper's §3.2.3 mechanisms, deferred from v1 (DESIGN.md non-goals):
**realms** let the same coeffect key resolve to different providers for
different components (Σiso, Def. 28-29 — multi-tenancy, testing,
sandboxes); **interception** attaches metadata to dependency access
without touching provider or consumer (Σinter, Def. 30-31 — access
control, quotas, §6.3).

## Surface syntax

Two prelude statements, following syntax-2.0's reserved shapes:

```revl
service Kv {
  fn set(key: Str, value: Str) -> Int
}

component TenantAApp requires kv: Kv {
  isolate kv in realm("tenant_a")
  intercept kv with { quota: 5, paths: ["a", "b"] }

  effect kv.set("who", "alice") undo kv.set("who", "")
}
```

- **Prelude rule**: `isolate`/`intercept` must precede every `effect`,
  `emit`, `await`, and `provide` statement. Rationale: they *derive the
  resolution context* (the paper's derived realization, Def. 27/29 — no
  inverse, discarded with the context); isolation decides which provider
  satisfies a requirement and therefore when the component activates, so
  it is interface-adjacent and must be settled before any dependency
  access. Enforced like A2 enforces provide-position.
- `isolate <key> in realm("<label>")` — `<key>` is any declared key
  (requires local or provides key; ρ governs both get and set, Def. 29).
  Undeclared key → G1-shaped error. Duplicate isolate of one key →
  error (in-source reassignment is a bug even though Def. 29 permits
  runtime reassignment).
- `intercept <key> with { <field>: <literal|list-of-literals>, ... }` —
  **requires keys only**: this is the component-declared metadata `d(k)`
  of Def. 30, whose domain is the dependency set. The context-carried
  half (`ι`) belongs to the harness (right-biased merge, per the paper);
  a provider has nothing to intercept. Metadata is static literals.

## Realm semantics

- A realm label is a **static string literal**. Equal strings = same
  realm — the paper §5.2.1 loader's "global realm shared by every entry
  naming that string". An unisolated key resolves in the default shared
  realm (Def. 28's `ρ(k) = k` convention).
- **Dynamic labels (`realm(config.tenant)`) are rejected** — parse-time
  tier error. Soundness, not laziness: config is unknown at link *and
  admission* time (`compile_files` has no config channel), so the linker
  could neither prove nor refute a collision between two config-derived
  realms; declaring them fresh would let equal tenant values collide at
  runtime — a provision conflict the linker declared impossible (G2
  broken); declaring them conflicting kills multi-tenancy. Prerequisite
  for lifting this: instance-parametric components with admission-time
  config resolution. Rejection example: `v2_dynamic_realm.rvl`.

## Guarantees under realms

- **G2 becomes per-(key, realm)**: two providers of `db` in *different*
  realms is the feature; the same realm is the conflict, and the error
  names the realm (Def. 43's discussion: "disjointness within a realm").
  `examples/tenants.rvl` must compile; `v2_same_realm_conflict.rvl`
  must not — the non-conflict is as load-bearing as the conflict.
- **G3/loadOrder are realm-aware**: an edge exists only where the
  consumer's realm for a key matches the provider's — realm separation
  legitimately breaks cycles.
- **Manifest**: entries gain `isolate` (key→realm) and `intercept`
  (key→metadata) fields *only when non-empty*; the provider map is
  realm-qualified. `revl audit` renders isolated keys as `key@realm`
  and prints intercept metadata — a multi-tenant composition is exactly
  where an orchestrator needs the manifest legible.
- The **admission gate** inherits everything: ambient manifest entries
  carry their realm fields, and the same linker runs.

## IR versioning

`ir_version: 2` is emitted **only when a compiled component uses v2
constructs**; otherwise documents stay at 1. Every frozen v1 reference
and golden stays byte-identical. Backends that predate v2 reject by
version (syntax-2.0 §9's prescribed mechanism). IR extensions, per
component and only-when-non-empty: `"isolate": {key: realm}`,
`"intercept": {key: metadata}`.

## Backend lowering

**cordis-py (implemented, runtime-verified).** Verified against the
`harden-fiber-lifecycle` sources:

- *Isolation cannot happen inside `apply`*: the fiber's context chain is
  fixed at plugin time and inject resolution reads
  `_effective_isolate()` from the parent chain. So the emitted plugin
  dict carries `"isolate": {"kv": "tenant_a"}`, and the runtime adapter
  gains `plug(ctx, component, config)` which applies
  `ctx.isolate(key, realm_label(name))` per entry *before*
  `ctx.plugin(...)`. `realm_label` is a process-wide string→label-object
  registry — cordis compares labels by identity, so same-string sharing
  must go through one object, never string interning.
- *Interception* lowers onto the inject-dict mechanism: the emitted
  `inject` becomes `{"kv": <metadata>, "other": None}` (dict form) when
  any key is intercepted, else stays a list (golden stability). The
  fiber copies non-nullish configs into its context's `_intercept`
  chain; Service-style providers consume via `__cordis_resolve_config__`
  (root→leaf, right-biased) — zero provider/consumer changes.
- Runtime acceptance: two providers of one key in two realms, two
  consumers isolated likewise — both activate, each observes *its own*
  provider; the consumer fiber's `_effective_intercept()[key]` equals
  the declared record.

**cordis (TS): implemented.** The TS backend lowers `isolate`/`intercept`
onto cordis v4's identical APIs (`backends/typescript/emit.py`, runtime
`plug`/`realmLabel`), verified against the real runtime in
`backends/typescript/tests/v2_realms.test.ts` (two providers of one key in
two realms; each consumer observes its own provider; realm-local
withdrawal).

**cordis-rs: implemented, runtime-verified.** Same shape as py/ts.
cordis-rs fixes a fiber's isolation scope at plug time — its reactive
`Inject` gate is evaluated against the context the plugin is registered on,
before the plugin closure runs, and both the gate and provisions key off the
fiber's captured `meta.isolates`. So isolation *cannot* be applied inside the
plugin body: the emitter lowers `"isolate": {key: realm}` into
`_revl_isolate_ctx(ctx, name)` and isolates the registration context
(`ctx.isolate_with(key, _revl_realm(realm))`) *before* `ctx.plugin(...)`,
the direct analogue of the py/ts `plug()` helper. `_revl_realm` is a
deterministic compile-time label registry (equal strings → one `Isolation`,
tagged into a reserved high region disjoint from cordis's scope counter).
Runtime acceptance in `backends/rust/scenarios/`: an isolated
`requires kv in realm("t")` consumer stays `Pending` until a separately
plugged isolated provider supplies `kv` in the same realm, then reactively
activates (and deactivates on withdrawal).

**wasm: implemented as realm-qualified namespaces.** The substrate has no
realm registry, so `isolate` becomes the import/export namespace
(`{realm}/{key}`) and `intercept` metadata rides in `revl:isolate` /
`revl:intercept` custom sections (host-enforced if present). The wasm tier
rejects only `ir_version: 3` (typed core) by version.
