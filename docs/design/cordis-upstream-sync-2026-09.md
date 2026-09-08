# Assessment: syncing the cordis TypeScript runtime to upstream rc.9

Status: reference / decision material (2026-09). No compiler change, no
`src/` change, no dependency-pin change is made by this note. It records the
two deltas (revl's fork hardening and upstream since the fork diverged),
classifies each upstream change's impact on revl, lists the concrete
conflict risks at file/function level, and recommends a sync plan for the
orchestrator to schedule. Changing the pin, rebasing the fork, or opening an
upstream PR are follow-up items, not part of this note.

Companion docs:
[../../backends/typescript/REPORT.md](../../backends/typescript/REPORT.md)
(the cordis-v4 lifecycle findings ledger),
[../../backends/typescript/tests/upstream.test.ts](../../backends/typescript/tests/upstream.test.ts)
(the characterization tests that pin the vendored behavior),
[../upstream/cordis-ts-assertActive.md](../upstream/cordis-ts-assertActive.md)
(the roadmap 74(a) upstream PR draft, not yet opened),
[teardown-contract.md](teardown-contract.md),
[../threat-model.md](../threat-model.md) (G5/G7/A8 rows),
[../backend-ir-v1.md](../backend-ir-v1.md) (A8 §).

Lifecycle and dev-workflow consumers that this assessment feeds: roadmap
items 460 / issue #723 (runtime lifecycle surface), 461 / #724 (dev
workflow), and 462 (the exemplary app). Those items are the reason the
upstream hot-reload work is worth tracking; see §4(a).

Source of record for the claims about the runtime below:

- The pin: `backends/typescript/package.json:14` pins
  `cordis` = `https://github.com/inso1337/cordis/archive/c8b94b2c4a921c85ae1ee292514bc8396c98d114.tar.gz`.
- The fork: `inso1337/cordis`, default branch `harden-assert-active`, HEAD
  `f45630e`, three commits deep on a re-rooted history (`c8b94b2` is a
  parentless root snapshot).
- Upstream: `cordiverse/cordis`, `main` at `caab04e`, tag `v4.0.0-rc.9` at
  `ed8a775` (2026-08-30).
- The characterization tests: `backends/typescript/tests/upstream.test.ts`
  (findings 1 and 2).

## 1. What revl pins today, and the shape of the fork

The fork is not a normal source fork of `cordiverse/cordis`. It is a
flattened, self-contained, single-package snapshot of upstream's
`packages/core` (the `cordis` core runtime only), republished with a
re-rooted git history so it installs from a codeload tarball. Its tree is
`{ LICENSE, README.md, bin.js, package.json, src/, lib/ }` where `src/` is
the nine core modules (`context, events, fiber, index, logger, reflect,
registry, service, utils`) and `lib/` is their compiled mirror. revl runs
the compiled mirror: `package.json` sets `"main": "lib/index.js"`. The
upstream `hmr`, `loader`, `include`, `group`, and `timer` packages are **not
vendored** at all. This is decisive for the impact analysis in §4: every
upstream change outside `packages/core` lands in code revl does not ship.

The fork's core corresponds to upstream `packages/core` at the rc.8 era
(around `8cc9e33`, 2026-08-13; `package.json` version is `4.0.0-rc.8`). Six
of the nine core modules are byte-identical to that upstream tree; only
`fiber.ts`, `events.ts`, and `registry.ts` differ, and they differ only by
the hardening described in §2.

**A discrepancy the orchestrator should note first.** The pin is the tarball
of `c8b94b2`, which is the fork's *root* commit, not its HEAD. The fork
carries two further commits (`e9c2841`, `f45630e`) that revl does **not**
consume. At `c8b94b2` the hardening is folded directly into `assertActive`:

```ts
// c8b94b2 (PINNED) src/fiber.ts:224  and lib/index.js:794
assertActive() {
  if (this.uid !== null && this.state !== FiberState.UNLOADING) return
  throw new CordisError('INACTIVE_EFFECT')
}
```

so the UNLOADING refusal applies to **every** `assertActive()` caller:
`effect()` (`fiber.ts:278`), `ctx.on()` (`events.ts:150`), `ctx.plugin()`
(`registry.ts:197`), **and** `update()` / `restart()` (`fiber.ts:470/478`).
`REPORT.md` §1.2 documents exactly this ("`effect()`, `ctx.on()`,
`ctx.plugin()`, `restart()` and `update()` all inherit the guard").

The fork's own HEAD (`f45630e`) later judged the `update()` / `restart()`
half to be wrong and split it out (`e9c2841`, "scope the UNLOADING refusal to
registration, not update()/restart()"): `assertActive` is reverted to the
disposed-only check, a new `assertRegistrable()` carries the UNLOADING check,
and only the three registration call sites route through it, so `update()` /
`restart()` keep working through an inertial reload's UNLOADING pass. **revl
runs the earlier, broader variant** because the pin lags the fork's own
default branch by two commits. This is a live, pre-existing drift independent
of the upstream question, and §7 recommends closing it regardless of what is
decided about rc.9.

## 2. revl's hardening delta (fork over upstream rc.8 core)

Three commits on the fork, of which only the first two carry semantic source
(the third recompiles the mirror):

| commit | scope | what it hardens |
| --- | --- | --- |
| `c8b94b2` | root snapshot | folds the `FiberState.UNLOADING` case into `assertActive()`, so effect/listener/plugin **registration during teardown** is refused with `INACTIVE_EFFECT`. Base is rc.8-era `packages/core`. |
| `e9c2841` | `fiber.ts`, `events.ts`, `registry.ts` | splits the check into a dedicated `assertRegistrable()`; reverts `assertActive()` to disposed-only; routes `effect()`, `ctx.on()`, `ctx.plugin()` through `assertRegistrable()`; leaves `update()` / `restart()` on `assertActive()`. |
| `f45630e` | `lib/index.js` | compiled mirror of `e9c2841` (the shipped artifact). |

The net semantic the fork intends (at HEAD) versus upstream:

- `assertActive()`: throws only when the fiber is disposed (`uid === null`).
  Unchanged from upstream.
- `assertRegistrable()` (new): throws when disposed **or** when
  `state === FiberState.UNLOADING`. This is the addition.
- Registration paths route through `assertRegistrable()`: `Fiber.effect()`,
  `EventsService.on()`, `Registry.plugin()`.

### Why revl needs it, mapped to the guarantees

- **G5 (teardown cannot register effects).** `threat-model.md` states G5 is
  enforced "by construction (grammar): an `effect`/`emit` in an `undo` is a
  parse error; there is no slot to attack." The cordis hardening is the
  **runtime backstop** for G5 on the TS tier: the language makes finding 2
  unrepresentable in source, and the hardened runtime refuses it even if a
  hand-written or non-revl plugin tried it. `upstream.test.ts` finding 2
  ("effects registered during teardown are refused (G5 gap, fixed in the
  pinned fork)") is the pin of this behavior. Without the fork, upstream's
  `assertActive()` (uid-only) would accept an `ctx.effect(...)` from an undo
  running while the fiber is merely `UNLOADING` (deactivated to `PENDING`,
  not disposed), pushing a disposer **after** `_unload` already `clear()`ed
  the list, so it never runs. Permanent residue. See `REPORT.md` §1.2.
- **A8 (mid-body failure reverts and contains).** The revert path runs undo
  bodies during teardown; the guard ensures the revert cannot itself register
  fresh effects that outlive the FAILED landing. The hardening is thus a
  containment aid for A8, not its mechanism (A8 is `lower` + the L-Raise
  runtime seam).
- **G7 (LIFO teardown).** Adjacent, not directly implemented by this patch;
  the guard keeps the disposer set from growing during the unload snapshot,
  which is a precondition for the LIFO drain to be complete rather than
  chasing a moving list.

## 3. Upstream delta since the fork diverged

Upstream `8cc9e33..main` since the fork's base. Split by whether it touches
`packages/core` (the only package revl vendors):

**Core (vendored surface):**

| commit | PR | subject |
| --- | --- | --- |
| `10194de` | #98 | fix(core): do not re-enter lifecycle from a failed outcome |
| `988df36` | #68 | widen optional properties for exactOptionalPropertyTypes (types only) |
| `1c1a10e` | #51 | fix(core): dispatch symbol and prototype-named events |
| `4cfd19a` | - | fix: resolve def-site service injection |
| `5b195b3` | #44 | fix(core): guard waterfall continuations |
| `2ceea23` | #109 | fix(core): do not leave fiber failures unhandled |
| `b3df558` | - | chore: update README (noise) |
| `ed8a775` | - | chore: bump versions (rc.9 tag; noise) |

**Non-core (NOT vendored: hmr / loader / include packages):**

| commit | PR | subject |
| --- | --- | --- |
| `caab04e` | #128 | feat(hmr): support hmr.watch() |
| `c594d1a` | #121 | fix(include): reconcile file and runtime edits through a journal |
| `c2835d8` | #123 | fix(loader): resolve bare specifiers from the project |
| `303cfd2` | - | feat(hmr): keep config reloading available without loader internals |
| `b280b6c` | #111 | feat(hmr): three-stage partial reload with per-fiber drain |
| `1b7d0f2` | - | fix(loader): await nested reconciliation during config update |
| `2df12b5` | - | fix(loader): detect internal API by runtime shape |
| `0027892` | #103 | fix(hmr): nested entry trees hmr and rollback |
| `e09e752`, `4c8e6be`, `b912d39`, `cb77029` | #58/#122/#64/#53 | test/ci/timer (not core, or non-shipping) |

## 4. Per-change impact classification

Categories: **(a)** a capability revl wants; **(b)** a fix that changes
emitted-code behavior or bears on a guarantee / pinned divergence; **(c)**
conflict risk with the fork's hardening.

### (a) Capabilities revl wants (all in non-vendored packages)

The hot-reload line, #111 (three-stage partial reload with per-fiber drain),
#103 (nested entry trees + rollback), #128 (`hmr.watch()`), and the loader /
include fixes (#121, #123) are the upstream substrate for the lifecycle and
dev-workflow items 460 / #723, 461 / #724, and the exemplary app 462. **None
of them is in `packages/core`, so none is vendored today.** Consuming them is
not a "sync the pin" action at all; it is a decision to vendor additional
cordis packages (or to depend on upstream `hmr` / `loader` directly). That is
a larger scope than this note and belongs to 460 / 461 planning. The one
core-level interaction to watch: #111's per-fiber drain changes the order and
grouping of disposer execution during a partial reload, which is the same
`_unload` machinery revl's finding 1 characterizes (top-level effects
disposed concurrently via `Promise.all`). If 460 ever vendors `hmr`, the
finding-1 characterization in `upstream.test.ts` must be re-read against the
per-fiber-drain behavior.

### (b) Fixes bearing on emitted behavior or a guarantee

- **#98 (`10194de`): do not re-enter lifecycle from a failed outcome.**
  Adds `if (this._error) return` at the top of `Fiber._setEpoch()`
  (`fiber.ts:~399`). A failed fiber stops re-entering its lifecycle on an
  epoch change; it recovers only through `update()`, which clears `_error`.
  **The fork does not have this** (its base predates #98). This bears
  directly on **A8**: it sharpens "the component lands FAILED and stays
  there" rather than being reanimated by an unrelated epoch bump. This is the
  single most relevant core fix revl is currently missing.
- **#109 (`2ceea23`): do not leave fiber failures unhandled.** Makes
  `Fiber.update()` return an `Awaitable<void>` and attaches a
  `task.catch(() => {})` so a dropped result cannot become an unhandled
  rejection while `await update()` still observes the failure; widens the
  `internal/update` event type to `Awaitable`. Bears on **A8** failure
  reporting and on any revl code path that drives `update()`. Behavior-visible
  for a host that awaits `update()`.
- **#44 (`5b195b3`): guard waterfall continuations.** Rewrites
  `EventsService.waterfall()` so each continuation gets its own `next()` and
  a double-`next()` throws. `waterfall` is the dispatch under
  `internal/update`, `internal/get`, `internal/set`. Hardens the
  reflect / update dispatch revl's runtime seam rides on; no emitted-code
  dependence, but it is a correctness fix in a path revl exercises.
- **#51 (`1c1a10e`): dispatch symbol and prototype-named events.** Refactors
  `EventsService` registration to key `_hooks` by event name (symbol-safe,
  `Object.create(null)` to avoid prototype names), moving the
  `_hooks[name] ??= []` allocation into `register()`. Type-level and
  dispatch-safety fix; matters because it touches the exact `on()` /
  `register()` path the fork hardened (see §5).
- **#4cfd19a: resolve def-site service injection.** Reworks the
  shadow / traceable machinery in `reflect.ts` and `utils.ts` to separate the
  def site (governs service resolution) from the use site (governs intercept,
  isolate, effects). revl vendors both files unchanged from rc.8, so this is
  a clean correctness improvement revl is missing; it bears on service
  resolution semantics the emitted code relies on.
- **#68 (`988df36`): widen optional properties.** Types only; no runtime
  effect. Neutral.

### (c) Conflict risk with the fork hardening

See §5 for the file/function detail. Summary: the only genuine textual
overlap is #51 versus the fork's `events.ts` `on()` edit. #98 and #109 touch
`fiber.ts` but in different functions from the hardening.

## 5. Conflict-risk list (file / function level)

Rebasing the fork hardening onto rc.9 means re-applying: (1) the new
`assertRegistrable()` method in `fiber.ts`, and (2) three call-site swaps
`assertActive()` -> `assertRegistrable()` in `Fiber.effect()`,
`EventsService.on()`, and `Registry.plugin()`. Against upstream `main` the
target lines are `fiber.ts:224` (`assertActive`), `fiber.ts:278` (`effect`),
`events.ts:160` (`on`), `registry.ts:197` (`plugin`); `update()` / `restart()`
sit at `fiber.ts:472/480` and must stay on `assertActive()`.

| overlap | files / functions | risk | note |
| --- | --- | --- | --- |
| #51 vs fork `on()` swap | `events.ts` `EventsService.on()` / `register()` | **medium** | #51 rewrote the same `on()` body (removed `const hooks = this._hooks[name] ||= []`, changed the `register(...)` call). The fork's edit there is the single line `assertActive()` -> `assertRegistrable()`, a few lines above #51's edits. A 3-way merge will likely apply cleanly or produce a one-line conflict; resolve by keeping #51's body and swapping only the guard call. |
| #44 vs fork | `events.ts` `waterfall()` vs `on()` | **low** | different functions in the same file; no line overlap. |
| #98 vs fork | `fiber.ts` `_setEpoch()` vs `assertRegistrable()` / `effect()` | **low** | different functions; the fork lacks #98 entirely, so adopting it is an addition, not a conflict. |
| #109 vs fork | `fiber.ts` `update()` vs `assertRegistrable()` / `effect()` | **low-medium** | #109 edits `update()`, which the fork deliberately leaves on `assertActive()`. Compatible in intent; textual hunks are disjoint. Re-confirm after rebase that `update()` still calls `assertActive()` (not `assertRegistrable()`), or an inertial reload's UNLOADING pass would be refused. |
| #4cfd19a vs fork | `reflect.ts`, `utils.ts` | **none** | fork vendors these unchanged; clean adoption. |
| #51 type additions | `events.ts` `Events` interface | **none** | additive. |

No conflict touches `context.ts`, `logger.ts`, `service.ts`, `index.ts`.

## 6. Does anything upstream retire a hardening patch or resolve a pinned divergence?

**No.** Upstream `main` (rc.9) still guards all three registration sites with
`assertActive()` (`events.ts:160`, `fiber.ts:278`, `registry.ts:197`), still
defines `assertActive()` as the disposed-only check (`fiber.ts:224`), and has
**no** `assertRegistrable` and **no** `UNLOADING`-registration guard anywhere
in `packages/core`. The finding-2 gap the fork closes is therefore **still
open upstream**, so:

- The hardening patch **cannot be retired**; it must be carried forward on any
  sync.
- The pinned divergence is **not resolved** by rc.9. It remains a revl-only
  patch. (revl's own upstream PR draft at
  `docs/upstream/cordis-ts-assertActive.md` is the intended route to close
  the divergence upstream, and it is still unopened. Note that the draft
  proposes the *folded-into-`assertActive`* form, matching the `c8b94b2` pin,
  and is itself stale relative to the fork's scoped `assertRegistrable` HEAD;
  if that PR is ever opened it should propose the scoped form.)

Conversely, rc.9 carries core fixes revl **lacks and wants** (#98, #109, #44,
#4cfd19a, #51). So the value of a sync is in adopting those, not in shedding
revl patches.

## 7. Recommended sync plan and risk

Two independent decisions, in order of readiness.

### 7.1 First, close the intra-fork drift (low risk, do regardless of rc.9)

The pin (`c8b94b2`) runs the broad guard that also refuses `update()` /
`restart()` during UNLOADING; the fork's own HEAD (`f45630e`) already scoped
that out for a reason (inertial reload). Move the pin to `f45630e` (or rebuild
the fork so HEAD == the intended snapshot). This is a two-commit fast-forward
within the same fork, no upstream involved. It also aligns the code with the
scoped `assertRegistrable` design that `REPORT.md` and the upstream draft
describe. Out of scope for this note to perform (pin change), filed as the
recommendation.

### 7.2 Then, rebase the hardening onto rc.9 core (recommended over cherry-pick)

Prefer **re-rooting the fork on upstream `packages/core` at `v4.0.0-rc.9`**
and re-applying the two source commits (`e9c2841`'s scoped form is the one to
keep; `c8b94b2`'s broad form is superseded), then recompiling `lib/`. Rebase
beats cherry-pick here because the fork's history is a synthetic re-root, so
there is no shared base to cherry-pick *from*; the honest operation is
"take rc.9 core, apply the hardening, recompile, repackage the tarball."
Concretely the hardening reduces to: add `assertRegistrable()` after
`assertActive()`; swap the guard at `effect()`, `on()`, `plugin()`; leave
`update()` / `restart()` on `assertActive()`. Adopting rc.9 also brings #98,
#109, #44, #51, #4cfd19a for free.

Do **not** vendor `hmr` / `loader` as part of this sync; that is items
460 / 461 scope (§4a).

### 7.3 What to re-validate (name the suites)

After bumping the pin, run, in order:

1. **`backends/typescript` vitest** (the whole suite,
   `backends/typescript/vitest.config.ts`). Load-bearing cases:
   `tests/upstream.test.ts` (finding 1 must still assert current-upstream
   concurrent disposal; finding 2 must still show registration-during-UNLOADING
   refused), `tests/frame_teardown.test.ts`, `tests/witnessed_teardown.test.ts`,
   `tests/phase1_bracket_fault.test.ts`, `tests/method_compensate.test.ts`,
   `tests/method_witnessed.test.ts`, `tests/router.test.ts`,
   `tests/instances.test.ts`, `tests/v2_realms*.test.ts`. The teardown /
   bracket / compensate suites are where a changed `_unload` ordering (#98,
   #109, or a future #111 vendor) would surface.
2. **`tests/test_selfhost_emit_ts.py`** and the TS emitter Python suites under
   `backends/typescript/` (`test_runtime_contract.py`, `test_async_extern_ts.py`,
   `test_stream_ts.py`, `test_fr3_ts_emit.py`) plus `tests/test_import_cordis.py`
   and `tests/test_cordisc_crosscheck.py` (these exercise the emit path against
   the vendored runtime).
3. **Conformance:** `tests/test_conformance_execution.py`,
   `tests/test_conformance_matrix.py`, `tests/test_conformance_cert.py`,
   `tests/test_realm_conformance.py` (the TS tier must stay byte / behavior
   equal to the other tiers).
4. **Adversarial:** `tests/test_adversarial_gate.py`,
   `backends/typescript/tests/json_proto_pollution.test.ts`,
   `seam_error_*_leak.test.ts`, `host_trace_secret.test.ts` (the
   `Object.create(null)` change in #51 touches the same prototype-safety
   surface these guard).

Any change to `_unload` ordering or `update()` return type (#98 / #109) that
reddens finding 1 or a teardown suite is a signal, not a flake:
`upstream.test.ts`'s header already says finding 1 is expected to fail loudly
when upstream fixes the concurrent-disposal gap.

### 7.4 Risk

- **§7.1 (pin to fork HEAD): low.** Two commits inside the same fork; the
  scoped guard is strictly more permissive on `update()` / `restart()` and no
  revl-emitted code depends on the broad form (G5 makes finding 2
  unrepresentable in source). The finding-2 test pins the registration
  refusal, which the scoped form still satisfies. The one thing to confirm is
  that the finding-2 test comment (which describes the folded `assertActive`
  form) is updated to the scoped form; the assertions themselves stay green.
- **§7.2 (rebase onto rc.9): low-to-medium.** The hardening re-application is
  mechanical (one new method, three one-line swaps). The single merge-attention
  point is #51's `on()` refactor (§5). The medium component is behavioral, not
  textual: #98 and #109 change failure / re-entry and `update()` awaiting, so
  the teardown and conformance suites in §7.3 are the real gate. Recompiling
  `lib/` and repackaging the tarball is the operational step most likely to be
  fumbled (the fork ships compiled JS as `main`); verify `lib/index.js` shows
  `assertRegistrable` at the three sites and `assertActive` still at
  `update()` / `restart()` before pinning.
