# backend runtime fork status + upstream-PR candidates (all tiers)

**Source:** read-only audit of the six backend runtimes, 2026-09-08 · **Status:** REFERENCE · **Scope:** which fork patches we carry should be upstreamed, and how far behind upstream each tier sits.

## Purpose

Two questions, answered per tier for the runtimes the backends target:

1. **Do we carry fork patches that should be UPSTREAMED as PRs?** — a patch is either *revl-specific hardening* (keep as a fork; only revl's gate relies on it) or a *general fix/primitive* upstream would want (a candidate PR).
2. **How far behind upstream is each pin, and should we sync?**

The TS tier (`inso1337/cordis` @ `c8b94b2`, `harden-assert-active`, tarball-pinned in `backends/typescript/package.json`; upstream `cordiverse/cordis`) is **covered separately** — see the dedicated cordis-TS sync deep-dive and `docs/upstream/cordis-ts-assertActive.md`. It is out of scope here beyond this pointer.

Cross-org PRs against any upstream (`cordiverse/*`, `geohotstan/*`, `dshbox/*`, `1na-ko/*`, `0xdenny218/*`) are the user's decision. This doc is the decision material; it opens no upstream PR.

## Matrix

| tier | upstream repo | fork we control? | pin (where) | patches we carry | upstreamable candidates | behind upstream | sync recommendation |
|---|---|---|---|---|---|---|---|
| **python** | `geohotstan/cordis-py` | yes — `inso1337/cordis-py` | `harden-fiber-lifecycle` @ `1c5e6f17` (`backends/python/setup.sh`, `CORDIS_PY_PIN`) | 3 (all general fixes) | **all 3, already submitted** as `geohotstan/cordis-py#1` (OPEN, MERGEABLE) | 0 behind (fork main == upstream main `a3d0c17`; pin is upstream main + 3) | none. Chase PR #1 to merge upstream, then repin to the merged commit and retire the fork branch. |
| **rust** | `dshbox/cordis-rs` (crates.io `cordis-rs`) | no | `cordis-rs = "0.3"` / `^0.3`, targets the 0.3.0 API (`backends/rust/placement_runner/Cargo.toml`, `backends/rust/emit.py` `CRATE`) | none (released dep) | none | ~3 minor lines: `^0.3` vs latest `0.6.2` (0.4, 0.5, 0.6 published) | nothing to upstream. Optional deliberate version bump (0.3 → 0.6); it is an emitter-compat project, not a correctness fix. |
| **java** | `1na-ko/cordis4j` | no runtime pin — in-repo javac stubs + reference impls | stubs validated against `1na-ko/cordis4j` @ `6c210e5` (v0.4.1) (`backends/java/stubs/README.md`) | 2 reference impls in the stub (not a checked-out fork) | **2, not yet submitted** — `serviceInRealm`, keyed `ServiceKey` | stub validation commit is 22 commits behind upstream HEAD (v0.4.1 → v0.4.2+) | submit the two PR specs (needs a JRE to build/run first); re-validate stubs against current cordis4j HEAD. |
| **go** | `0xdenny218/stc-go` | in-repo writable fork `forks/stc-go` (additive only), consumed via `go.mod replace` in tests | runtime pin `v0.6.1-0.20260818143352-b3d6788a428e` (= upstream main HEAD `b3d6788`) in `backends/go/{placement_runner,scenarios}/go.mod` | 1 (2 added files: `route.go`, `route_test.go`) | **1, not yet submitted** — `ServiceInRealm` / `LiveInRealm` | runtime pin 0 behind upstream main HEAD | submit the route.go PR upstream; once released, bump the pin and drop the `replace`. Runtime itself is current. |
| **wasm** | none — first-party `inso1337/cordis-wasm` | first-party (revl's own runtime) | unpinned loose checkout (`CORDIS_WASM` env / default dir; `src/revl/run_wasm.py`) | n/a | none (nothing to upstream) | n/a | nothing to upstream, nothing to sync. Consider recording a tested commit if reproducibility becomes a concern. |
| **ts** | `cordiverse/cordis` | yes — `inso1337/cordis` @ `c8b94b2` | tarball in `backends/typescript/package.json` | — | — | — | **covered separately** (cordis-TS deep-dive). |

## Consolidated list of proposed UPSTREAM PRs (decision material)

Ordered by readiness. None are revl-specific hardening — each is a general bug fix or an additive primitive the other tiers already carry, so each is a legitimate upstream contribution.

### 1. `geohotstan/cordis-py` — ALREADY OPEN as PR #1 (no new PR to open)

PR: <https://github.com/geohotstan/cordis-py/pull/1> — *"fix(fiber): close reentrant-disposal lifecycle gaps (DeepSeek Harness hardening)"*, head `inso1337:harden-fiber-lifecycle`, base `main`, state **OPEN**, mergeable **MERGEABLE**. Its head is exactly our pin `1c5e6f1`, so all three patches below are already in it. The decision is upstream-side: get it reviewed/merged.

| commit | title | class | rationale |
|---|---|---|---|
| `64aed9f` | fix(fiber): close reentrant-disposal lifecycle gaps | general bug fix | Ports the DeepSeek Harness fiber lifecycle hardening onto the Python fiber; closes 7 reentrant-disposal gaps (ownership-before-execution, single-flight joinable disposal, INACTIVE_EFFECT while UNLOADING, contained disposal notification, reload epoch recheck, pending-fiber effect drain). `tests/test_reentrancy.py` covers each. |
| `1316174` | fix(fiber): land the fiber FAILED when an async effect setup fails | general bug fix | An async-generator effect body whose setup fails was routed only to the auto-dispose guard: inverses ran LIFO but the fiber stayed ACTIVE and the error was dropped, unlike the synchronous path. Routes the async failure to the fiber's error slot. Regression test added. |
| `1c5e6f1` | fix(registry): read Config off dict plugins with dict.get, matching inject | general bug fix | `registry.plugin()` read `inject` via `plugin.get(...)` for dict plugins but `Config` via `getattr`, which never sees dict keys — a dict plugin could not carry a schema, so `resolve_config`'s validation path was unreachable. One-line parity fix; object-form plugins unchanged. |

### 2. `0xdenny218/stc-go` — NOT yet submitted

- **Upstream repo:** `github.com/0xdenny218/stc-go`
- **Proposed title:** `feat: ServiceInRealm — strict single-realm liveness-checked read`
- **Patch:** `forks/stc-go/route.go` + `forks/stc-go/route_test.go` (verified additive — the ONLY two files the fork adds over the pinned upstream commit; no existing file is touched).
- **Rationale:** `Context.resolve` walks the realm chain to the root, so a multi-realm router (whose bare key is provided in the parent realm for well-formedness) falls back to its own provision when a worker realm withdraws — it routes to itself instead of dropping the dead realm. `ServiceInRealm[T](c, key, r)` / `LiveInRealm(c, key, r)` read `provKey{realm, key}` directly with no parent walk, so a withdrawn realm reports `(zero, false)` and drops out. Additive, no change to existing exported API. Built + tested here (`go test -run TestServiceInRealm`, go1.26.5). No open upstream PR exists for it (upstream PR #1 was the unrelated, merged `wasm: Handle.Call`).
- **class:** general additive primitive (the go emitter consumes it, but the primitive itself is a general realm-scoped read upstream's own `TestIsolateRealm` documents the gap for).

### 3. `1na-ko/cordis4j` — NOT yet submitted (2 specs)

Both are PR *specs* with reference implementations living in `backends/java/stubs/io/cordis4j/core/`; neither has been built or run (no JRE in the build environment) and neither is an open upstream PR. Detail in `forks/cordis4j/REVL-FORK.md`.

| # | proposed title | class | rationale |
|---|---|---|---|
| 3a | `feat: Context.serviceInRealm — strict single-realm liveness-checked read` | general additive primitive | Java counterpart of the go `ServiceInRealm`: `Optional<T> serviceInRealm(Class<T>, String realm)` reads one realm's committed provider table with no parent/root fallback, so a router's emitted body fails over per realm instead of re-entering its own root provision via `ctx.get`. Additive — root-realm `get`/`provide` semantics unchanged. Reference impl in the stub `Context.java`. |
| 3b | `feat: keyed ServiceKey — route provide/get by provision key` | general additive primitive | `ServiceKey.of(Class<T>, String name)` + `name()` with `(type, name)` equality, and `Context.get(Class<T>, String name)`, storing providers keyed by `(type, name)` per realm so two providers of one service type coexist instead of the second overwriting the first. The go (`stc.Key`), rust (`ctx.provide("key",..)`), python, and wasm tiers already route by provision key; this brings cordis4j in line. Additive — an unnamed provision resolves exactly as before. Reference impls in stub `ServiceKey.java` / `Context.java`. |

## Per-tier detail

### python — `inso1337/cordis-py` fork of `geohotstan/cordis-py`

The setup script (`backends/python/setup.sh`) clones the fork's `harden-fiber-lifecycle` branch and hard-checks-out `CORDIS_PY_PIN=1c5e6f17abf538bf01012f9d72ce0cfa978d91b3` (a moving-branch-HEAD guard, roadmap 76c). The fork's `main` is byte-identical to upstream `main` (`a3d0c17`, 0/0), and the pin is `upstream/main + 3` commits. All three commits are general correctness fixes (no revl-only assertion among them) and all three are already carried by the open, mergeable upstream PR #1. So: nothing new to upstream, and the fork is not behind. When PR #1 merges, repin `CORDIS_PY_PIN` to the merged upstream commit and retire the fork branch.

### rust — crates.io `cordis-rs` (upstream `dshbox/cordis-rs`)

`backends/rust/placement_runner/Cargo.toml` declares `cordis = { package = "cordis-rs", version = "0.3" }` and `emit.py` emits `cordis-rs = "^0.3"` in every generated `Cargo.toml`; the emitter explicitly targets the 0.3.0 API (scope-label counter behaviour, `require`/`provide` shapes). There is **no inso1337 fork of cordis-rs** (`forks/` holds only `cordis4j` and `stc-go`), so there is nothing to upstream on this tier. Upstream has published 0.4.x, 0.5.x, and 0.6.x (latest `0.6.2`), so the `^0.3` constraint is ~3 minor lines behind. A bump would be a deliberate emitter-compatibility project, not a correctness sync; leave it unless a 0.4+ feature is needed.

### java — `1na-ko/cordis4j` (stubs, no runtime pin)

The java tier does not pin a runtime dependency: `backends/java/test_emit_java.py` compiles emitted sources against the in-repo javac stubs (`backends/java/stubs/io/cordis4j/core/`), and `backends/java/stubs/README.md` records that those stubs were validated against `1na-ko/cordis4j` @ `6c210e5` (Merge PR #16 / release v0.4.1). Two runtime primitives the emitter needs are delivered as *reference implementations in the stubs* plus PR specs (candidates 3a/3b above), because no JRE is reachable in this environment. Neither has an open upstream PR. The validation commit is now 22 commits behind upstream `main` (upstream has shipped v0.4.2, a dispose-race takeover fix, and semantic-drift parity work), so the stubs should be re-validated against current cordis4j HEAD, and the two PRs need a JRE to build and run before submission.

### go — `forks/stc-go` fork of `0xdenny218/stc-go`

Two independent facts:

- **Runtime pin is current.** `backends/go/placement_runner/go.mod` and `backends/go/scenarios/go.mod` require `github.com/0xdenny218/stc-go v0.6.1-0.20260818143352-b3d6788a428e`. That pseudo-version resolves to commit `b3d6788`, which is upstream `main` HEAD — 0 behind. (Upstream's newest tag is `v0.6.0`; the pin sits on a post-v0.6.0 main commit.)
- **The fork patch is additive and consumed only in tests.** `forks/stc-go` is a copy of the pinned upstream with exactly two files added — `route.go` (`ServiceInRealm`/`LiveInRealm`) and `route_test.go`; a diff against the pinned upstream commit confirms no existing file is modified. It is wired in only by a `go.mod replace` inside `backends/go/test_router_exec_go.py`, not by the shipped `go.mod`. So every non-routing go program builds against plain upstream. This is candidate #2; once it lands upstream and is released, bump the pin and drop the `replace`.

### wasm — first-party `inso1337/cordis-wasm`

Revl's own runtime — there is no upstream to PR against, so nothing to upstream and nothing to sync. `src/revl/run_wasm.py` locates it via `CORDIS_WASM` (or a default sibling checkout) and boots emitted WAT on its `Runtime` (wasmtime-backed); there is no in-tree commit/tag pin. The item-173 routing primitive (`route:<key>` host op) is built and tested here directly (`backends/wasm/test_router_exec_wasm.py`), not via a fork. If build reproducibility becomes a concern, recording a tested cordis-wasm commit would be the analogue of the other tiers' pins.

### ts — `inso1337/cordis` fork of `cordiverse/cordis`

Pinned as a tarball of `inso1337/cordis` @ `c8b94b2` (`harden-assert-active`) in `backends/typescript/package.json`. **Covered separately** by the cordis-TS sync deep-dive; see also `docs/upstream/cordis-ts-assertActive.md`. Listed here only for completeness.

## Method / provenance

- Pins read from `backends/python/setup.sh`, `backends/rust/placement_runner/Cargo.toml` + `backends/rust/emit.py`, `backends/java/stubs/README.md`, `backends/go/{placement_runner,scenarios}/go.mod`, `src/revl/run_wasm.py`, `backends/typescript/package.json`.
- python patch set: `git log a3d0c17..1c5e6f1` on the fork with `geohotstan/cordis-py` added as a remote; upstream PR state from `gh pr view 1 -R geohotstan/cordis-py`.
- go fork delta: file-level diff of `forks/stc-go` against `0xdenny218/stc-go` checked out at the pinned commit `b3d6788`; behind-count from `b3d6788..origin/main`.
- rust upstream versions: crates.io `cordis-rs` (`max_stable_version 0.6.2`) and `dshbox/cordis-rs` tags.
- java upstream state: `gh api repos/1na-ko/cordis4j/compare/6c210e5...HEAD` (ahead 22) and the cordis4j PR list (no serviceInRealm / ServiceKey PR).
