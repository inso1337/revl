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
- **cordis-py A8 async-body gap** (documented, runtime-verified): a component
  body containing an `await` compiles to an *async* generator, and cordis-py
  routes an async effect-setup failure to `_make_effect_guard` (auto-dispose)
  rather than the fiber's error slot. The accumulated inverses **do** run,
  LIFO, with no residue (A8's containment holds), but the fiber lands `ACTIVE`
  instead of `FAILED` — A8's "the component lands FAILED with the error
  recorded" is dropped for async bodies. Found independently by the fault-test
  and replay features (docs/fault-tests.md §8, docs/replay.md §7): reproduced
  with hand-built IR and no revl code in the loop, and confirmed tier-side —
  it occurs identically with recording off. Sync bodies are unaffected. The
  fault-test harness asserts the inverses ran and reports the wrong state
  rather than masking it; the replay test asserts *neutrality* (same verdict
  recording on/off), so it keeps passing once cordis-py is fixed instead of
  pinning the bug.
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
  the class by *running* the probe on each tier; python and TypeScript execute
  by default, rust and java behind `REVL_CROSS_TIER_SLOW=1`, with cheap static
  guards for all three lowerings.

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

- **Host-object results are untyped** (by design, host provenance — *narrowed*):
  the method *names, arities, and argument types* on a constructor-tracked
  receiver are now checked: `Map.new()` infers the family, and a method call on
  it is validated against the stub surface spelled in `_HOST_ARG_SIG`
  (docs/stdlib-2.0.md) — `m.putt(k)`, the typo'd method that used to emit a
  dynamic dispatch and crash at host runtime, is now a compile error
  (`HOST-METHOD`). What stays opaque is the *result*: no table entry claims to
  know what a stub returns, so a value flowing out of `store.get(k)` carries no
  type and is unchecked wherever it goes. Receivers whose provenance no
  constructor pins (an extern's return) type unknown; the lowerer refuses
  non-stdlib method *names* on them, but a stdlib-named method on such a
  receiver lowers as that builtin and is wrong at runtime — that residual
  sliver, plus host-object results, is the remaining fence. Still the
  deliberate G8 trust boundary: host objects are on the audit surface, not the
  checked surface. Stratum-1 stdlib methods on `Str` / `List` / `Bytes` are
  typed and their misuse is refused, in `fn` bodies and (as of the setup op
  sweep) in component effect blocks alike.

- **Arrow *values* have no type** (frontier, narrowed): an arrow's parameters
  are un-annotated and no arrow type is reconstructed, so the arrow itself, and
  anything obtained by calling one, infers to an unknown. Trigger: `let f = (x)
  => "s"` then `f(1)` in an `Int` position. Blast radius: the call's result
  flows anywhere unchecked, and the arrow's arity is not checked at the call
  site. Narrowed, not open: the arrow's *body* is now type-checked against the
  enclosing scope, so a captured variable misused inside a lambda (`(x) =>
  o.name` on an `Opt`, `(x) => s[0]`, a wrong-arity call) is refused exactly as
  it is outside one. Closing the rest needs arrow-parameter annotations in the
  grammar.

- **Type parameters cannot be bounded, and the implicit spelling still
  applies** (frontier, narrowed): instantiation is closed — a type parameter is
  a wildcard only inside its own fn's body and is unified against the actual
  arguments at every call site (`collect_tparams` / `unify`, `typecheck.py`),
  so `fn id(x: T) -> T` then `id("hello")` no longer satisfies an `Int`
  position. The **declaration form is closed too** as of `4725770`:
  `fn id[T](x: T) -> T` and the `extern` counterpart declare parameters by
  name, a strict superset of the implicit single-uppercase heuristic
  (docs/generics.md). What remains open:

  (a) the implicit rule is still positional-by-spelling and still on — a
  one-letter signature type is generic whether or not the author meant it, so a
  typo'd one-letter name becomes a type parameter rather than an error. The
  explicit `[T]` list lets an author say what they mean; it does not let them
  turn the heuristic off. (b) Parameters cannot be bounded, or shared across
  signatures. (c) Only `fn` and `extern` quantify — service `provide`-methods
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
