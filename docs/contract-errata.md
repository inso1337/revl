# Backend IR contract — v0 errata & decisions

Arbitration of the two backend REPORTs (backends/python/REPORT.md,
backends/typescript/REPORT.md) against the frozen v0 contract
(docs/backend-ir.md). The contract file stays frozen; changes land as IR v1
alongside the frontend. Both backends accepted the reference IR verbatim and
independently validated the contract's central bet: provision withdrawal is
runtime-derived (R5) with zero hand-rolled teardown on either runtime.

## Decision: ship order

**cordis-py ships first; TypeScript second.** Unanimous across both reports:
the hardened cordis-py runtime (pin: `inso1337/cordis-py@harden-fiber-lifecycle`
until geohotstan/cordis-py#1 merges) matches the metatheory's hypotheses;
upstream cordis 4.0.0-rc still carries a residue bug (below), and py keeps the
v0 toolchain single-language. The TS backend stays green in CI as the
portability proof.

## Decision: lowering strategy is normative

Both backends independently discovered that **fiber-level disposal is
concurrent on both runtimes; LIFO is only contracted within one effect's
yielded disposers**. A naive one-effect-per-step lowering silently violates
R1/R3. Therefore, *normative for all backends*: a component body lowers to
**one** effect generator; provisions yield their wrappers into it;
method-time effects join it (py: `Frame.adopt` + final drain; ts: same shape).
This goes in the compiler spec, not the runtimes.

## IR v1 amendments (accepted)

| # | Finding | Source | Amendment |
|---|---|---|---|
| A1 | No `await`/iteration-boundary step; divert-at-boundary (DESIGN §3.4) untestable from IR | both | add `{"step": "await", "expr"}` in v1 |
| A2 | `provide` step position is load-bearing but unconstrained: an acquisition *after* a provide would be reverted while dependents can still call the service | py | linker rule: within a body, no `let-effect`/`effect` step may follow a `provide` step (checker error, not runtime surprise) |
| A3 | No identifier lexicon: IR names colliding with host keywords / reserved names (`ctx`, `config`, `frame`) are rejected, not renamed | py | v1 defines a renaming scheme; frontend guarantees emitted-name safety |
| A4 | `format` lacks escaping for literal `$N` and coercion rules | both | v1: `$$` escapes; coercion = host string conversion, documented |
| A5 | `emit` has no `compensate` slot despite DESIGN §3.5 | ts | v1 adds optional `compensate` expression on `emit` steps |
| A6 | Service methods untyped (`any` in TS output); provide-method params duplicate the service declaration | ts | v1 carries param/return types; emitters derive signatures from the service, methods stop restating params |
| A7 | `emission` flags are advisory to backends (unenforceable there) | py | working as intended — enforcement is the *checker's* job (G4); noted so nobody expects backend enforcement |
| A8 | Mid-body acquire-failure semantics inherited from runtime, not contracted | py | v1 contracts the paper's L-Raise reading: accumulated effects revert, component lands FAILED, siblings unaffected |

## Upstream issues surfaced (to file / track)

- **cordis (TS) residue bug**: `assertActive` checks `uid !== null`, not
  lifecycle state, so an undo can register an effect during deactivation that
  lands after the unload snapshot and is never disposed — permanent residue.
  Pinned repro: `backends/typescript/tests/upstream.test.ts` ("finding 2").
  This is the G5 gap; feeds cordiverse/cordis#39 review.
- **cordis-py dict-plugin `Config`**: `registry.plugin()` reads `inject` via
  `dict.get` but `Config` via `getattr`, so dict plugins can't carry a schema;
  emitted code validates config inside `apply` as a workaround. One-line
  upstream fix; candidate follow-up to geohotstan/cordis-py#1.
- **cordis-py ordering provenance** (documented, not a bug): the fiber's
  unload empirically starts disposals newest-first, but this is disclaimed by
  its docs; the py adapter's drain derives R1 from the *documented* contract
  and covers async undos. Do not remove the drain on the strength of the
  empirical ordering.
- **cordis-rs A1 divergence** (documented, runtime-verified): cordis-rs 0.3.0
  drives `plugin_async` activation to completion with `block_on` *under the
  fiber transition lock* (fiber.rs), so a divert during a component `await`
  **defers until activation finishes** — post-boundary steps (including
  emissions) run, then everything reverts LIFO. cordis-py diverts *at* the
  boundary and skips the remainder; the wasm tier physically rolls back
  mid-activation. The invariant that holds on every tier — asserted under a
  concurrent-divert race loop in `backends/rust/scenarios/scenarios.rs` — is
  torn-state freedom: after disposal, every completed effect has run its
  inverse, in LIFO order. Authors relying on "emission after boundary never
  happens once diverted" get that guarantee on py/wasm, not on rs.
