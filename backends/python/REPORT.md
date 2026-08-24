# revl cordis-py backend — v0 report

Backend built against cordis-py, branch `harden-fiber-lifecycle`
(inso1337/cordis-py, upstream PR geohotstan/cordis-py#1). All acceptance
criteria of `docs/backend-ir.md` are met: the reference IR is accepted
verbatim, the golden file regenerates identically, the demo shows R2/R3 in
its event log and asserts R1/R4, and the pytest suite covers R1–R5
(15 tests, all passing; one command, see README).

## 1. Impedance mismatches with cordis-py

### F1 — Component-level LIFO (R1) is not the runtime's contract; the emitter has to construct it

The IR wants one accumulator per component, recovered in exact reverse
order, *including* effects accumulated by provide-method calls while active.
cordis-py's fiber unload is `asyncio.gather` over the fiber's top-level
effects (mirroring upstream's `Promise.all`), and its README explicitly
disclaims sequential LIFO: "LIFO ordering is preserved only within a single
effect's yielded disposers."

Lowering chosen: the whole component body compiles to **one**
`ctx.effect(generator)` whose yields are the inverses in step order (the
runtime's per-effect disposer is LIFO — that part is contractual), and a
small adapter (`runtime.Frame`) adopts every effect registered later by
provide-method calls; the generator's final `yield frame.drain` puts the
drain at the top of the disposer stack, so adopted effects are undone
newest-first before the activation-time inverses.

Empirical wrinkle found while mutation-testing the adapter: the fiber's
unload pass *already* starts disposals newest-first (`DisposableList.clear()`
reverses, faithfully porting the JS runtime), so with synchronous undos the
drain finds every adopted effect already disposed (single-flight no-op) and
R1 ordering is in practice delivered by the fiber itself. We keep the drain
anyway: (a) that fiber-level ordering is exactly what the runtime's docs
disclaim — it is an artifact of task-creation order under `gather`, and an
*async* undo would let an older inverse start before a newer one finishes;
(b) the drain derives R1 from the documented per-effect contract instead.
Either mechanism alone passes today's suite; only removing both fails it.

### F2 — Dict plugins cannot carry `Config` (CLOSED in the pinned fork)

`registry.plugin()` read `inject` with `plugin.get("inject")` for dict
plugins but read `Config` with `getattr(plugin, "Config", None)`, which
never sees dict keys — so the DESIGN §7 lowering "`config {}` → a schema" on
the plugin dict could not use the runtime's `resolve_config` path, and the
emitter worked around it by resolving defaults/validation inside the emitted
`apply` (`runtime.ConfigSchema`).

**Closed** by a one-line fix in our fork (`inso1337/cordis-py@
harden-fiber-lifecycle` commit `1c5e6f1`, pinned in `setup.sh`): `Config`
is now read with the same `isinstance(dict)` branch as `inject`. The emitter
ships the schema as `'Config'` on the plugin dict and the emitted `apply`
stops resolving config — cordis-py's `resolve_config` validates/resolves
before `apply` runs, and `ConfigSchema.validate` (the cordis-py
`{issues, value}` protocol) parks the resolved value so the component's
Frame still attributes the `<name>.config` trace and R4 `resolved_config`
state. The replay harness (which calls emitted `apply` directly) applies the
same resolution itself. Follow-up candidate to geohotstan/cordis-py#1.

### F3 — Event callbacks receive a `this` argument first

`ctx.on("internal/status", cb)` invokes `cb(this_arg, fiber, old_state)` — a
JS-ism of the port. Only affects demo/test listeners, not emitted code, but
every host application integrating compiled components will trip on it once.

### F4 — Provision is provide-then-set, not atomic

The IR's `provide` step is one step; the runtime splits it into
`ctx.provide(name)` (registers with value `None`, returns the revertible
effect) and `ctx.set(name, value)`. The window where the impl exists with
value `None` is invisible in practice because `provide` defers dependent
notification until the fiber turns ACTIVE — but that invisibility is a
runtime scheduling property, not something the backend can enforce.

### F5 — The withdrawal guard's placement is what makes R3 work — and it does

The clause "a dependent can still call its required services during its own
teardown" needed zero adapter code: `req` lowers to `ctx.<name>` attribute
access, which resolves through the fiber's committed store, and the provider
awaits its dependents inside the provide disposer before its earlier
inverses run. The `test_r3_dependent_can_call_required_service_during_own_teardown`
test (an IR component whose `undo` calls `db.execute`) passes with the
dependent's unlock landing strictly before `pool.close`. This is the fork
branch's hardening paying off; v0 should stay pinned to it until PR #1 lands.

## 2. What the IR contract could not express cleanly

Reported, not fixed, per the contract's instructions:

1. **No identifier lexicon.** The IR never says what a legal name is. Any
   `bind`/param/requires key named `ctx`, `config`, `frame`, `self` (or any
   Python keyword) would capture emitted scaffolding; the emitter rejects
   these with `EmitError` instead of renaming. The contract should either
   define the lexicon or require backends to alpha-rename.
2. **`provide` position is semantically load-bearing but unconstrained.**
   R3's ordering (dependents fully deactivate before the provider's other
   inverses) falls out of the provide step sitting *last* in the accumulator,
   so its withdrawal inverse runs *first* on recovery. The reference IR does
   this, but the contract doesn't require it: a component that acquires
   another effect *after* its `provide` step would revert that acquisition
   while dependents can still call the service. The linker should either
   forbid post-provide acquisitions or the contract should pin the ordering.
3. **No `await` / iteration boundaries.** DESIGN §3.4 makes every `await` a
   divert point and the runtime supports async-generator effects natively,
   but ir_version 0 has no step or expression for asynchrony — the backend's
   divert-at-boundary capability is unexercised.
4. **`emission` is unenforceable metadata.** The backend sees `emit` steps
   (call sites) but cannot check the converse: a `call` to an
   `emission: true` method inside a plain `return` step would be emitted as
   an ordinary call with no marker. Enforcement belongs to the checker; the
   contract could say so explicitly.
5. **`format` semantics underspecified.** `$0`-substitution with `str()`
   coercion was chosen; no escaping story (the sample's SQL interpolation
   would be injectable against a real database — fine for a stub, worth a
   note in the contract).
6. **Failure semantics are silent.** Nothing in the IR says what happens if
   an `acquire` throws mid-body. The runtime gives a good answer (effects
   accumulated so far are reverted; the fiber lands FAILED), and the
   emitted single-generator lowering inherits it — but that is inherited
   behavior, not contract.

## 3. LOC breakdown

| file | LOC | notes |
|---|---|---|
| `emit.py` | 322 | emitter incl. validation/rejections |
| `runtime.py` | 276 | ~115 adapter (`Frame`, `ConfigSchema`, `fmt`), ~120 stub stdlib, rest tracing |
| `demo.py` | 122 | event-logged swap scenario + R1/R4 assertions |
| `golden/user_cache.py` | 93 | generated |
| `tests/` | 399 | conftest 78, emitter 55, semantics 266 |
| **total** | **~1210** | plus README/REPORT/setup.sh |

## 4. Recommendation: ship the cordis-py backend first

Yes — v0 should ship this backend first, as DESIGN §8 already argues:

- **Every required semantic landed on runtime primitives.** R2, R3, R5 took
  zero backend code beyond calling the right API; R4 is assertable through
  the runtime's own introspection (hook snapshot, `reflect.store`,
  `registry.size`, fiber disposables); only R1 needed an adapter, and a
  small one (~40 lines that also double as the method-effect join point the
  IR demands).
- **The hardened fork is a hard dependency.** The teardown-readability and
  reentrancy behavior R3/R5 rely on comes from `harden-fiber-lifecycle`;
  pin it until geohotstan/cordis-py#1 merges.
- **What this tier cannot prove** — confinement (any emitted closure could
  reach a Python global) and enforcement of `emission` purity — is exactly
  what DESIGN already assigns to the checker (G6, G8) and the Wasm tier.
  No surprises were found that would promote the Wasm tier to first.
