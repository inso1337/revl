# REPORT — cordis v4 (TypeScript) backend

Backend for the frozen v0 IR contract (`docs/backend-ir.md`), targeting
`cordis@4.0.0-rc.8` (npm `latest`; matches `packages/core` upstream). All
R1–R5 tests pass; the acceptance demo passes with the required event log.
This file records what it took.

## 1. Impedance mismatches with cordis v4

### 1.1 Top-level fiber effects are disposed concurrently — the lowering had to be shaped around it (major)

`Fiber._unload` runs the fiber's top-level disposers via `Promise.all`
(each disposer wrapped in its own `async` fn): they are *started* in reverse
order, but any disposer that suspends lets later (i.e. earlier-acquired)
disposers proceed. `ctx.provide`'s withdrawal disposer is async — it awaits
`fiber.await()` on every dependent — so with a **naive per-step lowering**
(each IR step its own `ctx.effect`, or a bare generator `apply` whose yields
land directly in `fiber._disposables`), the provider's *earlier* effects are
reverted while dependents are still tearing down. Concretely: the provider's
pool closes before a dependent's undo gets to call `db.execute(...)` through
its committed view — the call throws, the error is swallowed into the fiber
logger, and R3 is silently violated.

Minimal repro: `tests/upstream.test.ts` → "upstream finding 1" (asserts the
unlock never reaches the host under the naive lowering).

By contrast, the disposers collected by **one** `fiber.effect(generator)` are
run strictly sequentially in LIFO order (each `dispose` chained with
`.then`). **Adapter workaround (clearly marked, in `emit.py`):** the whole
component body is lowered into a single `ctx.effect(function* ...)`;
`provide` steps `yield` the wrapper returned by `ctx.provide`, which cordis
then re-parents into that effect at the correct LIFO position. With this
shape, withdrawal fully completes (dependents deactivated and awaited)
*before* the next-earlier disposer runs — R1 and R3 hold, as the demo log
shows (`UserCache UNLOADING -> PENDING` strictly before `pool.close`).

This is the semantics the paper's Theorem 63 requires and is squarely in the
territory of upstream's pending lifecycle work
([cordiverse/cordis#39](https://github.com/cordiverse/cordis/pull/39)): the
runtime has an exactness-preserving sequential path and a concurrent path,
and which one you get depends on how a plugin happens to arrange its effects.
Recommend upstream either make `_unload` sequential or document the
single-effect idiom as the only conforming shape.

### 1.2 Effects can be registered during teardown; they leak permanently (G5 gap, upstream bug)

`Fiber.assertActive()` only checks `uid !== null` — i.e. "not disposed", not
"not unloading". During a *deactivation* (requirement withdrawn; the fiber
survives as PENDING), an undo that calls `ctx.effect(...)` is accepted:

1. the new effect executes and its disposer is pushed into
   `fiber._disposables` — *after* `_unload` already `clear()`ed the list;
2. the fiber ends PENDING while still holding an effect
   (`fiber.getEffects().length > 0` on an inactive fiber);
3. even a subsequent `fiber.dispose()` never runs that disposer, because the
   epoch is already `INACTIVE` and no further unload is triggered —
   **permanent residue**.

Minimal repro: `tests/upstream.test.ts` → "upstream finding 2" (asserts the
leaked disposer is never invoked, even after full disposal). On a *full
disposal* the guard does work (uid is nulled before the unload), which is
exactly the reentrancy asymmetry DESIGN.md attributes to the DS mod 6 /
PR #39 hardening.

The emitted code cannot trip this: revl gives undo bodies no syntactic
position for effect forms (G5 is by-construction).

**RESOLVED in the pinned fork.** The backend now pins revl's fork
(`inso1337/cordis@harden-assert-active`, commit `c8b94b2`, via the
codeload tarball URL in `package.json`) whose `assertActive` also refuses
`FiberState.UNLOADING` — `effect()`, `ctx.on()`, `ctx.plugin()`, `restart()`
and `update()` all inherit the guard, so registration during teardown raises
`INACTIVE_EFFECT` exactly as full disposal does. The repro test flipped from
a red-on-fix characterization test to a pin of the fixed behavior (finding 1
remains current-upstream behavior and still asserts what rc.8 does). The
upstream PR draft lives at `docs/upstream/cordis-ts-assertActive.md`; it is
not opened without coordinator confirmation.

### 1.3 Provider swap cannot be atomic

Provision disjointness is enforced at runtime (`ctx.provide` throws if the
key is held), so "swap the Database provider" is necessarily
*dispose-then-load*, with a window in which dependents are deactivated
(observable in the demo log). That is the correct paradigm semantics for v0,
but it means the language currently has no way to express the paper's §6.2
rolling update (service broker); this matches DESIGN.md open question 1 and
should stay a frontend/loader concern, not an emitter hack.

### 1.4 Smaller notes

- **`emission` is erased at runtime.** cordis has no acquisitions/emissions
  distinction; the emitter can only carry the marker as a doc comment on the
  service interface. Fine for v0 (the checker owns G4/G8), but the runtime
  will never enforce it.
- **Config defaults:** cordis validates config via standard-schema
  (`Plugin.Config`). The IR's `{name, type, default}` triples don't warrant a
  schema dependency, so the adapter applies defaults/required-checks itself
  (`host.applyConfigDefaults`). Idiomatic cordis would use a schema; revl's
  checker makes that redundant.
- **Root contexts bypass `inject`.** A fiber without a runtime (the root) may
  read any service without declaring it (`reflect.get(prop, false)`), which
  the demo/tests use to drive `ctx.cache`. Convenient for host code; also a
  reminder that in library-cordis, declared-only access is a discipline, not
  a check — precisely the row revl moves to "compile".
- **Positive match:** the committed view (`fiber.store` surviving until the
  end of `_unload`) gives R3's "req readable during own teardown" for free,
  and `ctx.provide`'s disposer awaiting dependents is exactly R5. The
  contract's bet that provision withdrawal can be runtime-derived is correct
  on this runtime.
- `ctx.set` ended up unused: IR provisions are whole-service values, so
  `ctx.provide(name, impl)` covers them; revertible `set` is finer-grained
  than the contract needs.

### 1.5 Per-tool-call witnessed effects — the H1 gate (item 318 → 324)

A witnessed `[fs]` mutation that fires from a **provide-method body** (per tool
call, after activation) is the real agent H1 gate: the mutation IS the
deliverable, so it must PERSIST on a clean session/component unload and REVERT,
residue-free, on an abort. The activation-body form (`Frame.transactional`,
Slice 2b) yields its disposer into the body generator's own cordis LIFO stack;
a method body has no such generator.

**The soundness hazard (verified on this cordis-style tier).** The obvious move
— adopt the method's witnessed entry as a sibling `ctx.effect`, so its disposer
joins the fiber's teardown — is UNSOUND here, exactly as item 318 found on py.
cordis disposes an adopted sibling effect BEFORE the body effect's final
`yield frame.drain` runs, so on a CLEAN unload that disposer observes
`committed` still false and WRONGLY REPLAYS (reverts) the deliverable. The fix
mirrors py: `Frame.transactionalMethod` does NOT return a cordis disposer. It
PARKS the entry in the frame (`deferredList`) and `Frame.drain` disposes it
itself, AFTER `committed`/`aborting` is settled — so it reads the correct
commit-vs-abort bit by construction. `tests/method_witnessed.test.ts` pins the
hazard directly (a clean unload never reverts) plus the full H1 loop.

**The abort seam.** A component that activated cleanly always reaches its final
`yield frame.drain`, so any later clean unload runs `drain` and would implicitly
COMMIT every per-call mutation. `Frame.abort()` (the seam item 245's commit/
abort UX will drive) sets `aborting` before that unload; `drain` then leaves
`committed` false, so every transactional entry — activation-body and
method-deferred alike — replays and the mutations revert. A test reaches the
live frame through `frameForCtx(fiber.ctx)` (a WeakMap keyed by the apply ctx,
the TS analog of py's `_FRAME_BY_CTX`).

**Byte-identity.** The whole apparatus is gated on `_needs_frame`, now extended
to descend into provide-method bodies but to trigger ONLY on a WITNESSED method
effect — an ordinary method-body bracket / `emit … compensate` stays the
pre-existing bare `ctx.effect(...)`. Every non-witnessed and non-method-witnessed
program emits byte-identically (verified: goldens + every committed generated
module diff clean against a fresh emit).

**Shared-doc note (not edited here):** `docs/design/243-witnessed-externs.md`
and `docs/design/teardown-contract.md` already describe the method-body
witnessed position and the park-for-drain rule generically (item 318, py
reference); this TS tier now realises that rule with no new shared-doc surface.
`selfhost/emit_ts.rvl` is not yet ported to this lowering (item 323 owns the
port; see §2 below).

## 2. What the IR contract could not express cleanly (reporting, not fixing)

1. **No types on service methods** — `params` are bare names, so emitted
   interfaces/methods are `any`-typed. The config table has a `type` field;
   services deserve the same, or the "idiomatic host source" bar caps out at
   `any`.
2. **No `await`/iteration-boundary step** — DESIGN.md §3.4 makes every
   `await` a divert point, and the generator lowering would support it for
   free (`async function*`), but the step table has no way to say it, so
   divert-at-boundary is untestable from IR.
3. **No `compensate` for emissions** — §3.5 types compensations apart from
   inverses; the `emit` step has no slot for one, so saga-style emissions
   can't reach a backend at all.
4. **`format` escaping is unspecified** — the emitter assumes every
   `$<digits>` is a placeholder; a literal `$0` in template text is
   unrepresentable.
5. **Provide-step redundancy** — method names/params are repeated in the
   `provide` step and in the service declaration. The emitter validates they
   agree (and rejects drift), but the duplication invites it.
6. **No provision replacement construct** — see §1.3.

None of these blocked acceptance; the reference IR is accepted verbatim.

## 3. LOC breakdown

Hand-written (excluding generated code and lockfile):

| file | LOC | notes |
|---|---|---|
| `emit.py` | 460 | emitter incl. contract validation + CLI (~90 comment/docstring) |
| `runtime.ts` | 218 | host builtins + config glue + R4 introspection |
| `demo.ts` | 127 | acceptance demo, self-checking |
| `tests/semantics.test.ts` | 159 | R1–R5 |
| `tests/emitter.test.ts` | 114 | golden diff + rejections |
| `tests/upstream.test.ts` | 101 | pinned upstream repros |
| `scripts/` | 49 | golden regen + fixture emission |
| **total** | **~1,250** | |

Generated: `golden/user_cache.ts` 72 LOC (checked in, diffed by tests).

## 4. Should v0 ship this backend first?

**No — ship cordis-py first, keep this backend green in CI as the second.**

- DESIGN.md §8 already stakes v0 on cordis-py because *we hardened it
  ourselves* and its paper-conformance suite doubles as revl's. This exercise
  confirmed that reasoning empirically: upstream cordis v4 (TS) has at least
  two live lifecycle gaps (§1.1, §1.2). §1.1 is fully neutralized by the
  lowering shape, but §1.2 is only unreachable *from emitted code* — any
  hand-written host plugin sharing the Context can still corrupt the
  environment revl's guarantees describe. On a hardened cordis-py, the
  metatheorems' hypotheses and the runtime actually agree.
- The compiler is Python; a Python-target backend keeps the v0 toolchain
  single-language and the debug loop shorter.
- That said, this backend earned its keep: it proved the frozen contract is
  implementable on the *reference* runtime with a ~480-line emitter, forced
  the discovery of §1.1 (which any backend on upstream JS cordis must know),
  and its upstream tests are canaries for cordiverse/cordis#39. Ship it
  second, pinned to 4.0.0-rc.8, and revisit "first" if upstream lands the
  reentrancy hardening — the TS ecosystem is where cordis users actually are.
