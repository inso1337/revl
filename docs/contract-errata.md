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
| A9 | A `provide` block keyed outside the `provides` clause was rejected with a bare, uncoded `RevlError` — no guarantee, no fix hint (item 153, site-playground finding) | py | sibling of A6 (A6 bounds a declared key's *methods*; A9 bounds the *key* against the clause). Dedicated code A9 — A1–A8 are all occupied above (A4 = `format` `$$`-escaping, A7 = advisory emission flags), so A9 is the next amendment slot. Hint names both fixes: rename the provide block to a declared key, or add the key (with its service) to the `provides` clause. `diagnostics.GUARANTEES`/`FIXES` + `lower.py` raise; `examples/rejections/a9_provide_key_not_declared.rvl` |

## Upstream issues surfaced (to file / track)

- **cordis (TS) residue bug** (RESOLVED — fixed in the pinned fork,
  `inso1337/cordis@harden-assert-active` commit `c8b94b2`, PR draft at
  `docs/upstream/cordis-ts-assertActive.md`; do NOT open the upstream PR
  without the coordinator's confirmation): `assertActive` checked `uid !==
  null`, not lifecycle state, so an undo could register an effect during
  deactivation that landed after the unload snapshot and was never disposed —
  permanent residue, the G5 gap, on the tier the README calls the portability
  proof. The fork's `assertActive` now also refuses `FiberState.UNLOADING`
  (`effect()`, `ctx.on()`, `ctx.plugin()`, `restart()` and `update()` all
  inherit the guard), and the repo's TS tooling pins the fork revision in
  `backends/typescript/package.json` (codeload tarball at the fork commit)
  until the fix merges upstream (feeds cordiverse/cordis#39 review). The
  repro at `backends/typescript/tests/upstream.test.ts` ("finding 2") was a
  red-on-fix characterization test; it now pins the fixed behavior (the exact
  playbook that closed cordis-py's A8 async gap).
- **cordis-py dict-plugin `Config`** (RESOLVED — one-line fix in the pinned
  fork, `inso1337/cordis-py@harden-fiber-lifecycle` commit `1c5e6f1`, now
  pinned by `backends/python/setup.sh` per roadmap item 76(c); follow-up
  candidate to geohotstan/cordis-py#1): `registry.plugin()` read `inject`
  via `dict.get` but `Config` via `getattr`, so dict plugins couldn't carry
  a schema and emitted code validated config inside `apply` as a workaround.
  The fork now reads `Config` with the same `isinstance(dict)` branch as
  `inject`; the emitter ships the schema as `'Config'` on the plugin dict and
  drops the in-`apply` resolution (`ConfigSchema.validate` speaks cordis-py's
  `{issues, value}` protocol; the Frame still attributes the `<name>.config`
  trace and R4 `resolved_config` state; the replay harness — which calls
  emitted `apply` directly — applies the same resolution itself).
- **cordis-py ordering provenance** (documented, not a bug): the fiber's
  unload empirically starts disposals newest-first, but this is disclaimed by
  its docs; the py adapter's drain derives R1 from the *documented* contract
  and covers async undos. Do not remove the drain on the strength of the
  empirical ordering.
- **cordis-py A8 async-body gap** (RESOLVED — fixed in the pinned runtime,
  `inso1337/cordis-py@harden-fiber-lifecycle` commit `1316174`, folded into
  geohotstan/cordis-py#1): a component body containing an `await` compiled to
  an *async* generator, and cordis-py routed an async effect-setup failure to
  `_make_effect_guard` (auto-dispose) rather than the fiber's error slot — the
  accumulated inverses ran LIFO with no residue (A8 containment held) but the
  fiber landed `ACTIVE` instead of `FAILED`, dropping A8's "the component lands
  FAILED with the error recorded" for async bodies. The runtime now routes an
  async setup failure to the fiber's error slot, matching the sync path: the
  fiber lands `FAILED` with the error recorded while the inverses still run
  LIFO (containment unchanged). Sync bodies were always unaffected. The
  fault-test lock `test_an_await_body_lands_failed_like_a_sync_body` now pins
  the fixed behavior (it was a red-on-fix characterization test); the replay
  neutrality test was unaffected throughout. See docs/fault-tests.md §8.
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

  **Decision (2026-08-24): A1's contracted invariant is promoted to torn-state
  freedom.** The weaker invariant that holds on *every* tier becomes the
  wording of the A1 contract; the per-tier difference is spec, not surprise.
  "Emission after boundary never happens once diverted" is a py/wasm tier
  property, not the contract: on cordis-rs a divert during a component `await`
  defers (post-boundary steps run, then everything reverts LIFO), and that is
  compliant. No upstream fix (fork+PR: don't hold the fiber transition lock
  across the await boundary) at this time — cordis-rs stays pinned at 0.3.0,
  and the race-loop assertion pins the promoted invariant so it cannot drift.
  Revisit the stronger guarantee only if a consumer's correctness actually
  depends on divert-at-boundary on the rs tier.
- **cordis4j global-realm divergence** (documented, runtime-verified): revl's
  contract (docs/design-v2-realms.md) is that realms are keyed by label and
  **equal realm-label strings denote the same realm** — two components that
  both `isolate kv in realm("t")` share one realm, so a second provider of `kv`
  in realm `"t"` is a G2 conflict and a consumer in realm `"t"` resolves the
  provider in realm `"t"`. That holds at runtime on cordis-py, cordis (TS), and
  cordis-rs. It is **false on cordis4j (Java)** at the level revl's emitter
  targets: equal strings do **not** share, they separate.

  *Observable difference (runtime-verified on the real cordis4j jar with revl's
  own emitted code — `examples/tenants.rvl` → `emit.emit`, driven by
  `backends/java/scenarios/RunRealmDivergence.java`):*
  - Two `TenantAStorePlugin` instances, both emitting `ctx.isolate(Kv.class,
    "tenant_a")` and both providing `kv`, loaded onto one root → **BOTH LOAD**.
    The contract requires a G2 `SupplyConflictException` (one label = one
    realm). `root.find(kv)` is even `<absent>` — each provision hides in its own
    isolate child.
  - `TenantAApp` (`isolate kv in realm("tenant_a")`, requires `kv`) loaded after
    `TenantAStore` (same realm) → **`NoSuchServiceException`** ("looked up
    through the context chain: #9 → #7 (root)"): the consumer's isolate child is
    a *different* context from the provider's, so it never resolves the
    provider. The reference tiers resolve it. (Distinct realm strings —
    `tenant_a` vs `tenant_b` — separate correctly on every tier, including Java;
    only the equal-string *sharing* direction diverges.)

  *Root cause (verified against the real cordis4j sources,
  github.com/1na-ko/cordis4j, cordis4j-core):* revl's Java emitter emits
  `ctx = ctx.isolate(<Svc>.class, "<label>")` **inside each component's
  `apply()`** (`backends/java/emit.py:2127-2129`). Core `Context` exposes
  exactly one isolate overload, `<T> Context isolate(Class<T>, String)`
  (`core/Context.java:124`), and it **always mints a fresh child** —
  `new ContextImpl(this)` + `child.registry.overrideRealm(type, realm)`
  (`core/internal/ContextImpl.java:160-168`). Each `ContextImpl` owns its own
  `ServiceRegistry.store` (`core/internal/ServiceRegistry.java:41`); the realm
  label is folded into the store key only within that context
  (`provide` at `:87-92`, `get` / parent-chain walk at `:148-155`,
  `effectiveRealm` at `:57-68`). There is **no** get-or-create-by-label form on
  `Context`, so two `isolate(_, "t")` calls are two disjoint stores. The global
  "equal strings = one realm" interning exists exactly one layer up, in
  `Loader`: a `Map<String, IsolatedDomain> domains` (`core/Loader.java:67`)
  keyed by the full isolate-chain **path** and get-or-created per label
  (`core/Loader.java:341-359`, key at `:346`, reuse-or-create at `:347-351`),
  with refcounted disposal of the derived context once the realm drains
  (`:275-296`). revl's emitter never reaches `Loader`: it composes with direct
  `ctx.plugin(...)` / `ctx.inject(...)` (see
  `backends/java/scenarios/RunRealScenarios.java`,
  `backends/java/placement/RealPlacementRunner.java`), so the interning layer is
  bypassed. Net: on Java, **local** realms (per-instance, distinct) come for
  free from a bare `isolate`/`fork`, but **global** (label-shared) realms are
  effectively unimplemented in the emitted code. This is the exact inverse of
  the other hosted tiers, whose shims/emitters intern the label string directly.

  *Why revl's emitted code cannot currently close it:* label-keyed sharing on
  cordis4j lives only in `Loader.reconcileTree(List<ComponentSpec>)` over a
  `ComponentSpec.Isolate(type, realm, children)` tree (`Loader.of(Context)` at
  `core/Loader.java:81-82`, `reconcileTree` at `:123`, `ComponentSpec.Isolate`
  in `core/ComponentSpec.java`). revl does not emit that: it emits self-contained
  `Plugin`s that isolate themselves inside `apply()` and are composed by the host
  with plain `plugin`/`inject` calls. Two independent `apply()` invocations have
  nothing to intern a string against. Closing the gap would require one of:
  (i) re-architecting the Java backend to drive composition through `Loader` +
  a `ComponentSpec.Isolate` tree — replacing the `plugin`/`inject` composition
  that G7 LIFO teardown, A8 self-revert, and Theorem-63 withdrawal ordering are
  all built and runtime-verified against, and making Java's composition model
  diverge from the other four tiers; or (ii) hand-rolling interning in emitted
  glue (a static `Map` keyed by root identity + type + realm that get-or-creates
  the isolate child) — which reimplements, worse, what `Loader` already does
  (no path-keyed nesting, no refcounted disposal at `:275-296`, and it leaks
  contexts and would reuse a disposed context on reload) and injects global
  mutable state into every emitted program. Both are out of proportion, so this
  divergence is documented rather than force-fixed.

  *Recommended path:* route Java realm placement through `Loader` — lift the
  `isolate` out of `apply()` and emit a `Loader.of(root).reconcileTree(...)`
  composition whose `ComponentSpec.Isolate` nodes carry the realm labels, so
  cordis4j's own `domains` interning (`core/Loader.java:341-359`) makes equal
  strings share and handles refcounted derived-context disposal. That is a
  composition-model change spanning the whole Java backend (emitter + the
  scenario/placement harnesses) and is a coordinator-level decision, not a
  contained emitter tweak. Until it lands, the Java tier honors local realms and
  the *distinct-string* separation, but not global label sharing; the cross-tier
  gate (`tests/test_realm_conformance.py`) marks Java `xfail(strict=True)` with
  this entry as the tracked reason, and
  `backends/java/test_emit_java.py::test_global_realm_divergence_characterized`
  pins the current behavior so it cannot regress silently.

## Semantic divergences (closed, recorded so they cannot regress)

- **`==` was not structural on every tier**, though syntax-2.0 §3.4 says revl
  has one equality, that it is structural, and — in as many words — that
  because the parser canonicalizes `===`→`==` in the IR, "no backend can
  diverge". Two tiers did.

  - **typescript** lowered `==` to JS `===`, which is *identity* for objects
    and arrays. `{a: 1} == {a: 1}` and `[1, 2] == [1, 2]` were `true` on
    python and `false` here: a silent wrong answer, not a refusal, on a tier
    the README calls the portability proof. Now lowered through a `revlEq`
    helper emitted with the module (key order irrelevant, arrays and objects
    never equal, `NaN`/`-0` falling through to `===` so they behave as they do
    on the reference tier).
  - **rust** derived only `Clone` on v3 records and variants, so `==` on a
    record did not compile at all (rustc E0369). Legal revl, refused by one
    tier. `PartialEq` is now derived alongside `Clone` and `Debug`.
  - **java** was already correct — `java.util.Objects.equals` on emitted
    records, whose `equals` is structural by construction — and is the
    precedent the other two now follow. **wasm** is unaffected: its values are
    i32 and compound equality is a documented tier limit.

  How it survived: `tests/test_cross_tier.py` checks that every emitter
  *accepts* a construct, which catches a tier that refuses and cannot catch a
  tier that accepts and then means something else. This is the project's own
  recurring lesson one level up — "the emitter did not raise" never implied
  "the code is right", and "every emitter agreed on a shape" never implied
  "every tier agrees on a value". `tests/test_cross_tier_execution.py` closes
  the class by *running* the probe on each tier; python and go execute by
  default, TypeScript wherever `backends/typescript/node_modules` exists, rust
  and java behind `REVL_CROSS_TIER_SLOW=1`, with cheap static guards for all
  three lowerings. Only the `conformance` job satisfies all three conditions
  at once (item 445); everywhere else a missing tier is a skip, and a skip
  here means unmeasured, not passing.

## Arithmetic divergences (open, pinned — one root cause)

Found by executing the same source on every tier
(`tests/test_cross_tier_execution.py`), which is also where each is pinned so
it cannot drift silently. **These are not fixed.**

- ✅ **`Int / Int` is not `Int`** — *closed, and closed the other way round
  from how it first looked.* The checker typed it `Int` while python and
  TypeScript produced `3.5`, so the declared type and the runtime value
  disagreed. The fix was not to make those two truncate: §0 says `/` is
  spelled as TypeScript spells it and therefore means what TypeScript means,
  and `7 / 2` is `3.5`. They were faithful; the checker was wrong, and rust
  was the tier out of step with the syntax. `Int / Int` now types as `Float`,
  integer division has named operations (`div_trunc` / `div_floor` /
  `div_euclid`), and `mod` gives the Euclidean remainder. See
  docs/arithmetic.md.
- ✅ **`%` disagreed on negatives** — *closed.* python floored (`-7 % 3 == 2`)
  where rust, java and JS truncate (`== -1`). `%` is now the truncated
  remainder everywhere, which §0 requires (it is TypeScript's spelling) and
  which the pairing law confirms: `%` partners `div_trunc`, `mod` partners
  `div_euclid`, and both identities are asserted by execution over sixteen
  sign combinations. python builds the truncated form; every other tier had
  it natively.
- **`Int` lost precision past 2^53 on TypeScript** (closed). JS numbers are
  f64, so `9007199254740993 - 9007199254740992` was `0`. python is
  arbitrary-precision and rust is `i64`. Unlike the other two this needed
  `BigInt`, not a type annotation — `Int` now maps to `bigint` on that tier,
  which imposes the 64-bit bound and traps on overflow the way python does.
  docs/arithmetic.md records the Int/Float boundary rules the port settled.
- **Unary minus on `Int.MIN`** (closed). Negating `Int.MIN` overflows (it is
  `0 - Int.MIN`), and the tiers used to split three ways. Every tier now
  faults: python via `_revl_i64`, wasm via checked `0 - x`, rust's native `-`
  panic, and — the three that used to wrap or grow — go via `revlSub(0, x)`,
  java via `Math.negateExact`, TypeScript via `revlI64(-x)`. Asserted by
  execution across py/ts/go/java and by per-tier emit checks
  (tests/test_cross_tier_execution.py::test_negation_of_int_min_traps).
- **`Int.MIN / -1`** (closed). Integer division overflows at exactly this
  input (quotient 2^63; mod is fine, `Int.MIN % -1 == 0`). Every tier now
  handles it: rust panics, wasm's `i64.div_s` traps, TypeScript re-imposes the
  bound via `revlI64`, and the three that used to wrap or grow — python bounds
  the faulting quotient through `_revl_i64`, go through `revlDivTrunc` /
  `revlDivFloor` (panic), java through `Math.divideExact` / `Math.negateExact`.
  The checked forms (`checked_div_*`) return `Err("revl: Int overflow")` rather
  than a wrapped value, totalising the range as well as the zero divisor.
  Asserted by execution (tests/test_cross_tier_execution.py::
  test_div_int_min_traps / test_checked_div_int_min_is_err).

**The root cause is closed.** An IR `bin` node used to carry `op`, `left` and
`right` and *no type*, so no backend could distinguish `Int / Int` from
`Float / Float` — a runtime dispatch works on python, where `int` and `float`
are distinct, and cannot work on TypeScript, where both are `number`. `/` and
`%` now carry an `operands` field (`"Int"` / `"Float"`) when the checker can
determine it, and so does unary minus on an `Int` — the one unary operator
whose operand type a backend must know, since negating `Int.MIN` overflows
(see the entry above). The annotation is additive and v1 documents are
untouched, so the frozen-reference invariant holds.

`%` and the `Int` width are both settled now (docs/arithmetic.md): `%` is
the truncated remainder, and `Int` is 64-bit two's complement with trapping
overflow on every tier — including wasm, whose `Int` was widened to `i64`
with checked `$int_*` helpers. The one residual split is the *message*: a
wasm trap carries no payload, so it faults with `unreachable` where the
hosted tiers raise a labelled overflow error (see docs/arithmetic.md).

**Decision (2026-08-24): the bare-`unreachable` trap is accepted as a
documented tier limit.** A wasm trap carries no payload by design — the fault
channel is the trap itself, not a value the module can hand back — so a
designated fault-reason export before the trap would buy a labelled message
at the cost of a second, concurrent error channel (an export the module must
write *and* the host must read) for information the trap already conveys.
The labelled-error difference is diagnostic depth, not semantics: every tier
faults, and no tier continues past the fault. Revisit only if a tool wants
to distinguish overflow from a programmatic `unreachable` on the wasm tier;
until then the tier documents "faults with `unreachable`, no payload".

Not everything diverges: `<` on `Str` is lexicographic by code point on every
tier, including across the case boundary, and is asserted alongside the pins
so this section is not read as "arithmetic is broken generally".

## Arbitrary-precision `Integer` (fenced, designed, not built)

`Int32` landed complete across all six tiers (docs/arithmetic.md, "Sized
integers"): type, IR widen marker, codegen and trapping overflow, proven by
cross-tier execution. Its sibling on the roadmap — **`Integer`, arbitrary
precision** — is **not built**, and is fenced here so it never reads as clean.

**Trigger.** A program that names the type `Integer` — `fn f(x: Integer)`,
`let x: Integer = ...`. There is no `.to_integer()` conversion and no
arbitrary-precision arithmetic; the design is docs/integer-proposal.md.

**Blast radius.** `Integer` is *not* refused at a single, clear site today: it
is an unknown capitalized type name, so the checker treats it like any
undeclared nominal type. A binding whose value type is known (`let x: Integer =
5`) is rejected with a type mismatch, but a bare `fn f(x: Integer) -> Integer`
signature is **accepted** — the parameter and return infer to an unknown and
flow untyped, exactly the gradual-frontier behaviour, and no tier can lower it.
So the gap is a silent-accept at the signature boundary, contained to programs
that opt into the unbuilt type. Until it is built, do not spell `Integer`.

**Why it is not cheap-everywhere.** The cost is real and uneven, which is why
it is fenced rather than half-shipped on the two easy tiers:

- **cheap / native**: python `int` and TypeScript `bigint` are already
  arbitrary precision — `+ - *` are the host operators with the i64 bound
  *removed*, not imposed.
- **native but not `+`**: java `BigInteger` and go `math/big.Int` carry
  arbitrary precision but only through method calls (`.add`, `(&big.Int).Add`)
  and reference/pooling semantics — a different emission shape from every
  scalar op the emitters render today.
- **new dependency**: rust needs a bignum crate (`ibig`/`malachite`); the go
  and rust tiers are otherwise dependency-light, so this is a policy choice as
  much as a code one.
- **concentrated in wasm**: wasm has no bignum. It would need a bignum-in-WAT —
  tag a pointer and keep the digits inside linear memory so confinement holds
  (docs/integer-proposal.md) — which is a linear-memory arithmetic library, not
  an instruction. This is the tier the whole feature's cost concentrates in,
  and the reason `Integer` is a separate pass from `Int32`.

Shipping `Integer` on python+TypeScript alone would be a 2-of-6 feature that
reads as clean on two tiers and is absent on four — the exact failure this
section exists to prevent. It stays one fence until it can land whole (or land
with its own per-tier pins), tracked as roadmap item 12's second half.

## Typing gaps (fenced, not closed)

The checker is sound where types are known and silent where they are not (the
gradual frontier of the sound-typing milestone). The gaps below are known
soundness holes rather than mere unknowns, so each is loud here with its
trigger and blast radius, the way the cordis-rs A1 divergence is. Everything
else the checker does not type infers to an unknown and is left alone by
design.

**Closed since the last revision** (roadmap "Typing follow-ups"; rejection
files `t8`–`t17`). One of these — generic instantiation — was fenced here; the
rest were *not*, which is the failure mode this section exists to prevent: a
review found six programs the checker accepted and the strict tiers refuse,
none of them named by any fence. Each closure is verified against `javac
--release 21` and the emitted Rust.

- a declared return not produced on every path — in a `fn` and in a `provide`
  method alike (rust E0308, java "missing return statement"; python silently
  returned `None`, and python is the reference backend);
- a call with the wrong arity (rust E0061, java "cannot be applied to given
  types"), in `fn`, component and `test` bodies;
- any access *through* an `Opt` — `.field`, `[i]`, a stdlib method — plus `?.`
  on a non-optional and `?.` typed as the inner rather than `Opt[inner]` (rust
  E0609, java "cannot find symbol"). This one contradicted the README's
  headline claim outright: `return o` was refused while `o.name` escaped to an
  unknown and then flowed anywhere;
- `Str` indexing (rust E0277, java "cannot find symbol" — the emitter renders
  `.get(i)` on a `String`); the specified surface is `charAt` / `charCodeAt` /
  `slice`;
- a `match` arm naming something that is not a case of the scrutinee's ADT
  (java "cannot find symbol", rust `EmitError`);
- generic instantiation at the call site (below);
- **`type X = Y` silently meaning something other than an alias.** It parsed as
  a one-case variant whose single case was named `Y`, so `type Sku = Str` made
  the author's own alias unusable (`f("abc")` was refused for a `Sku`
  parameter) while `fn g() -> Sku { return Str }` was accepted, `Str` having
  resolved as that variant's nullary case constructor. This was the governing
  principle's own failure case — `type X = Y` is TypeScript's alias spelling
  and revl gave it a different meaning with no diagnostic — and a live trap for
  importers, since a WIT `type sku = string` transcribed naively was quietly
  wrong. Now a transparent alias; see below for where the line falls.

Arrow bodies are now checked against their enclosing scope, which is where the
first four used to hide. Do not re-fence any of these; each has an executable
rejection.

- **Host-object results are untyped** (by design, host provenance — *closed as
  of roadmap 75(b)*):
  the method *names, arities, and argument types* on a constructor-tracked
  receiver are now checked: `Map.new()` infers the family, and a method call on
  it is validated against the stub surface spelled in `_HOST_ARG_SIG`
  (docs/stdlib-2.0.md) — `m.putt(k)`, the typo'd method that used to emit a
  dynamic dispatch and crash at host runtime, is now a compile error
  (`HOST-METHOD`). What stays opaque is the *result*: no table entry claims to
  know what a stub returns, so a value flowing out of `store.get(k)` carries no
  type and is unchecked wherever it goes — **but it can no longer call a method
  on the way out**. Receivers whose provenance no constructor pins (an extern's
  return, a host-object result, a type parameter) type unknown; every method
  call on them is now refused, stdlib-named or not (`t24_opaque_receiver_builtin.rvl`;
  the stdlib-named-method sliver — `pool.remove("k")` lowering as the Map
  `remove` builtin — is closed with the same HOST-METHOD diagnostic), so a
  value cannot lower *through* the builtin table into a misdispatch. Only a
  receiver the checker can *prove* is a Str/List/Int/Int32/Bytes/Map value
  takes the table; an annotation (`let v: Str = store.get(k)`) is how a host
  result becomes provable. The fence is now exactly "host-object **results**
  are on the audit surface" — the G8 line as designed, with no accidental hole
  through the builtin table. Tooling half of the same closure: the stdlib
  method table and the host-verb surface are checked disjoint at *table-edit*
  time — a module-load assertion in `typecheck.py`/`lower.py` fails with the
  colliding name if either table is extended with a name from the other
  (`remove` is the one sanctioned overlap, safe because dispatch is by
  receiver kind; dogfood/findings-mapiter.md §2). Still the deliberate G8
  trust boundary: host objects are on the audit surface, not the checked
  surface. Stratum-1 stdlib methods on `Str` / `List` / `Bytes` are
  typed and their misuse is refused, in `fn` bodies and (as of the setup op
  sweep) in component effect blocks alike.

- **Arrow *values* have no type** — **CLOSED as of roadmap 75(a) slice 1**.
  Arrow values have a type; its unknown components are `Any`, the documented
  gradual frontier the rest of the checker already lives with. Every arrow
  types as a function type, so its arity — which is syntactic and never in
  doubt — is checked at every call through it (`examples/rejections/t33`), and
  where the author annotates (`(v: Int): Int => …`, parameters or return) or
  the body cannot depend on an un-annotated parameter, the result is known and
  is checked wherever it flows (`examples/rejections/t32`, the entry's own
  trigger). A written `Async[...]` return is refused: colour is positional and
  an arrow may not self-declare it (`t34`, docs/function-types.md rule C1).

  Two residuals, both named rather than latent: (1) stratum 3 — `infer_ir`,
  the checker over component and `provide`-method bodies, still types no arrow
  and no call through one (75(a) slice 3); (2) a result that *does* depend on
  an un-annotated parameter stays unknown, deliberately — inferring it from a
  body typed under unknown parameters yields half-solved types (`[x]` infers
  `List[Never]`, `{ a: x }` infers `{a: Any}`), and widening the rule needs a
  dependency analysis rather than the syntactic free-occurrence test.

- **Type parameters cannot be bounded, and the implicit spelling still
  applies** (frontier, narrowed): instantiation is closed — a type parameter is
  a wildcard only inside its own fn's body and is unified against the actual
  arguments at every call site (`collect_tparams` / `unify`, `typecheck.py`),
  so `fn id(x: T) -> T` then `id("hello")` no longer satisfies an `Int`
  position. The **declaration form is closed too** as of `4725770`:
  `fn id[T](x: T) -> T` and the `extern` counterpart declare parameters by
  name, a strict superset of the implicit single-uppercase heuristic
  (docs/generics.md). What remains open:

  (a) ~~the implicit rule is still positional-by-spelling and still on~~ —
  **CLOSED as of roadmap 75(c)**: a signature that carries an explicit `[T]`
  list turns the implicit heuristic OFF for that signature — declared means
  declared, and a stray one-letter name is an ordinary undeclared (opaque
  nominal) type that errors where it is used instead of silently quantifying
  (`t25_explicit_tparam_heuristic_off.rvl`; the interaction is pinned in
  docs/generics.md). (b) Parameters cannot be bounded, or shared across
  signatures — **bounds (`[T: Ord]`) stay deferred**: no consumer demands them
  yet, and the decision (not the machinery) is pinned in docs/generics.md.
  (c) Only `fn` and `extern` quantify — service `provide`-methods
  are checked through a separate path and are not in the shared signature
  table, so they take no list, and a one-letter name in a record field or an
  ADT payload is an ordinary opaque nominal type, like any other undeclared
  name (undeclared multi-letter names such as the `Row` in `-> List[Row]` are
  deliberately opaque, since service returns are host-shaped).

- **Type aliases are transparent, and the alias/variant line is drawn where
  TypeScript's own is** (closed, recorded because the rule is subtle): `type X
  = Y` is an alias exactly when `Y` names an existing type — a builtin, a
  declared record/variant/alias, a type application (`List[Row]`), or the `T?`
  sugar. It is then substituted at every declaration site and its declaration
  erased, so no alias name reaches the type table, the IR or a backend. When
  `Y` is *undeclared*, the one-case-variant reading stands: `type Status =
  Pending` is still how an opaque nominal is spelled, and a payload always
  makes a newtype (`type W = Wrap(Int)`), never an alias.

  The split is not arbitrary — it is TypeScript's, verified against `tsc
  --strict`: `type Sku = string` compiles and is transparent in both
  directions, while `type Sku = Ident` and `type K = Ident | Keyword` with
  undeclared names are hard errors (TS2304). So revl now agrees with
  TypeScript everywhere TypeScript compiles, and is free to mean its own thing
  only where TypeScript refuses — which is exactly what the governing
  principle asks for. Transparent rather than nominal for the same reason: a
  nominal alias would need construction syntax revl does not have, and would
  re-commit the sin by giving TypeScript's spelling a different meaning.

  Consequences worth knowing: an alias cannot carry type parameters (there is
  nothing to instantiate), an alias cycle is refused rather than looped on, and
  `List[Row] | Str` is refused with "revl has no union types" — `|` separates
  variant *cases*, which are constructor names, not types. The interaction with
  implicit type parameters resolves in the alias's favour and does so before
  the signature table is built: `type S = Str` makes `S` mean `Str`, while an
  undeclared one-letter name in a signature is still that fn's type parameter.

- **A `match` over an untypable scrutinee stays best-effort** (frontier,
  unchanged): case-name and exhaustiveness checks need the scrutinee's ADT.
  When it is not recoverable — a host-valued local, an arrow result — both
  checks are skipped and the Python emitter adds a runtime fallback. Trigger:
  `match store.lookup(k) { ... }` on a host object. Blast radius: a missing or
  misspelled case in that one `match` reaches the tiers. Narrowed: a bare
  nullary constructor now types as its ADT, so `match FirstTime { ... }` is
  checked; only genuinely unknown scrutinees are silent.

## G8 audit-surface gaps

The G8 boundary surface (`revl audit`, and the `host:`/`emit:` crossing tokens
`revl audit --diff` gates on) is meant to be the *enumerable* set of host
reaches — "everything that reaches the host must appear on the audit surface"
(docs/rejections.md, G8). One enumeration gap was known and fenced here; it is
now **RESOLVED** (item 24), and the record is kept for provenance.

- **G8 enumeration for first-class host reaches — RESOLVED (item 24)** (found by
  the gate threat-model program, docs/threat-model.md; the pin
  `tests/test_adversarial_gate.py::test_first_class_laundered_host_reach_is_enumerated_on_the_g8_surface`
  is now a plain passing assertion, no longer `xfail`). *The gap that was:* a
  host `extern` reached **only** through a first-class function value —
  `indirect(ship, a)` rather than `ship(a)` — did **not** appear in `revl
  audit`'s per-component `externs` list, so it produced no
  `host:<component>:<extern>` crossing token; `revl audit --diff` (the
  authority-drift gate) could not detect a widening that added such a reach.
  Throughout, the load-bearing G4 defence held — the operation was still
  correctly flagged `readOnlyHint: false` / `destructiveHint: true`, and the
  same launder in a *plain* (read-only) operation was *refused* at compile
  time — so it was an *enumeration* incompleteness, never a read-only lie.
  *How it was closed:* `_boundary` (`src/revl/__main__.py`) now folds the
  first-class reach the G4 fixed point already computes onto the audit surface.
  For each component body it collects first-class *value* references the same
  way the emission analysis does — reusing `emission_analysis._calls_in`'s
  value channel (read-only; no change to `emission_analysis.py`) — and joins
  each referenced callable's capabilities from `_emitting_capabilities` into the
  per-component host set. A laundered host extern now surfaces the identical
  `host:<component>:<extern>` crossing as a direct call, so `revl audit --diff`
  flags a regeneration that adds a first-class-laundered reach instead of
  silently accepting it. The `*` first-class-dispatch marker still appears when
  the dispatched value is genuinely unnameable.

## Contract rejection coverage (the executable spec)

`examples/rejections/` plus the REJECTIONS table in `tests/test_frontend.py` is
the checker's executable definition of sound: every guarantee the checker can
refuse has a program it must refuse, with a diagnostic that names the guarantee.
The guarantees below have no rejection file because a program cannot violate
them at compile time; they are listed so the coverage claim is complete, not
sampled.

- **G1, G2, G3, G4, G6, G8** and **A1, A2, A6, A8** are compile-time; each has
  at least one wired rejection. `g6_impure_statement.rvl` (confinement, "plain
  expressions have no effect to record (G6)") was added alongside the component
  setup op sweep.
- **G5** (teardown cannot register effects) holds by construction: `undo`
  bodies are pure expressions, so there is no syntactic slot for an effect
  during teardown. There is nothing to refuse; the residue it guards against is
  the library-side runtime concern noted above (cordis TS `assertActive`).
- **G7** (LIFO-complete derived teardown) is a runtime property of the lowering,
  verified by the runtime scenarios in `backends/rust/scenarios/` and
  `backends/java/scenarios/`, not by the checker.
- **A3, A4, A5** are lowering transforms (host-name renaming, `$$` escaping, the
  `compensate` slot), not refusals; each has a positive test in
  `tests/test_frontend.py` (`test_a3_host_colliding_names_are_renamed`,
  `test_a4_literal_dollars_are_escaped`, `test_a5_compensate_lowering`).
- **A7** is advisory; its enforcement is G4 itself (`g4_unmarked_emission.rvl`).
- **T1–T20** are the type-soundness refusals. `t8`–`t19` were added with the
  typing follow-ups above and `t20_int_literal_range.rvl` alongside the Int
  literal-range diagnostic; they are deliberately paired: each rejection file
  states the tier error it prevents, and `tests/test_typesafety.py` asserts
  both the refusal *and* the legal spellings it must not touch (a `fn`
  returning nothing needs no `return`; `if`/`else` where both arms return is
  fine; `o?.name`, `o?.name ?? d` and a `match` unwrap all stay accepted;
  `xs[0]`, `s.charAt(0)` and a `_` arm stay accepted). A soundness check with
  no false-positive test is a check nobody can safely tighten later.

## Record updates on receivers with no named type (RESOLVED — item 71)

✅ **Closed by structural record types (roadmap item 71).** `{ r | f = e }` was
field-checked only when `r`'s type was *known*: a declared record parameter, a
named binding, or a value read from a typed position. A receiver whose type the
checker could not recover — in practice, a `let`-bound **anonymous record
literal** (`let a = { h: "x" }`) — had no name to look up, so the update's field
names and value types were **not checked**, and neither were later reads through
the binding. The trigger below compiled, an Int flowing into a Str-shaped
record:

```rvl,reject
type C = { h: Str }
fn main() -> Int {
  let a = { h: "x" }
  let b = { a | h = 5 }   // now refused: update of field `h` expects `Str`, got `Int`
  assert b.h == 5
  return 0
}
```

The fix was the design the roadmap called for, not a patch. An anonymous record
literal now infers a **structural record type** — spelled `{field: Type, ...}`
in canonical (sorted) order, in the checker only (`typecheck.py`
`structural_fields` / `format_structural`). An update `{ a | f = e }` on it is
field-checked against that shape: an undeclared field, or a replacement of the
wrong type, is refused naming the guarantee (fixtures
`t26_anon_record_update_wrong_type.rvl`, `t27_anon_record_update_undeclared_field.rvl`);
reads through the binding are checked too.

At every *declared* boundary the structural type **unifies field-wise with the
nominal record it meets** — the field set must match and each field type must be
compatible, with the `List[Never]` bottom rule falling out of the elementwise
`compatible` recursion (`Never` flows out of a bottom position into any other,
and nothing flows in). This is the same shape
as item 11's `?T` widening marker: a checker-level annotation that **never
reaches the IR**. The `record` / `record_update` IR nodes carry no type, an
inferred `let` type is not emitted, and the emitted `types` table stays nominal
— so the v1/v2/v3 goldens are byte-identical and no emitter changed. See
docs/records.md ("Structural vs nominal at declared boundaries").

## TCK A5 respec: two-phase teardown (amendment, 2026-08-26)

`a5_compensate_lifo` (tck/spec.py) asserts the v0 placeholder behavior: a
compensation joins the single teardown accumulator and reverts interleaved
LIFO with the activation inverses, on every teardown. The DELETE fires before
the earlier bracket unlock, success and abort alike. The unified teardown
contract (docs/design/teardown-contract.md, from 243/247) changes this on two
axes at once, ordering AND firing condition, so A5 is respecced as two
clauses:

- **a5a, discharge on clean unload.** A clean successful unload DISCHARGES
  the compensation: it never runs, and the forward emission it would have
  offset (the insert) survives as the deliverable. Observable: no
  `migration_log` DELETE in the trace, the row is present, sibling provider
  unaffected.
- **a5b, two-phase abort.** An abort runs Phase-1 proof replay (every
  `bracket` and `transactional` inverse, LIFO, to completion) and only THEN
  Phase-2 compensations (LIFO within the class, best-effort, bounded).
  Observable: every proof inverse in the trace precedes the first
  compensation; the compensation DELETE now fires AFTER the earlier bracket
  unlock, the exact inversion of the old a5 ordering assertion.

Sequencing, because `pytest tests/` does not run the per-backend goldens
(a green root suite proves nothing about this respec): the a5a/a5b spec
change lands together with the py tier's runtime flip and an explicit sweep
of `backends/*/golden`, the TCK adapters, and the executed per-tier scenario
suites where old-a5 behavior actually lives and runs: the go scenarios under
`go test` (backends/go/scenarios), the typescript suite under `npm test`
(backends/typescript), and the java scenario runner
(backends/java/scenarios). The adapter directory holds exactly one adapter,
`tck/adapters/py_adapter.py`; the other tiers exercise the spec through the
scenario harnesses just named, so "sweep the adapters" means that one file
plus those harnesses. a5b also needs a NEW adapter fixture: the current a5
case only disposes a cleanly activated component, so there is no
abort-after-the-compensated-emit path against which a5b's ordering
observable could be asserted. Each remaining tier flips against the new
clauses, carried as a pinned `Divergence` in tck/spec.py until it does. No
backend is ever built or asserted against the old single-interleaved-LIFO
a5 once the spec change is in. The pre-flip corpus sweep for programs
relying on clean-unload compensation firing (247 open question 2) gates the
first flip.
