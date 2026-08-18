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
- generic instantiation at the call site (below).

Arrow bodies are now checked against their enclosing scope, which is where the
first four used to hide. Do not re-fence any of these; each has an executable
rejection.

- **Host-object methods are untyped** (by design, host provenance): a value of
  host provenance (a `let` bound to `Map.new()` / `Pool.open(...)`, or a
  requirement's return) carries the host stub's methods, which sit outside the
  specified stdlib surface (docs/stdlib-2.0.md). `builtin_check` returns `None`
  for any method not in `_BUILTIN_SIG`, so a host-object method call has unknown
  type. Trigger: `store.get(key)` on a `Map` used in a typed position. Blast
  radius: the result flows anywhere with no check, and a misspelled host method
  is caught at the host runtime, not by the checker. This is the deliberate G8
  trust boundary: host objects are on the audit surface, not the checked
  surface. Stratum-1 stdlib methods on `Str` / `List` / `Bytes` are typed and
  their misuse is refused, in `fn` bodies and (as of the setup op sweep) in
  component effect blocks alike.

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

- **Type parameters are implicit, so they cannot be spelled or constrained**
  (frontier, tracked): instantiation itself is closed — a single-uppercase name
  in a `fn` signature that is not a declared type is that fn's type parameter,
  is a wildcard only inside that fn's own body, and is unified against the
  actual arguments at every call site (`collect_tparams` / `unify`,
  `typecheck.py`), so `fn id(x: T) -> T` then `id("hello")` no longer satisfies
  an `Int` position. What is *not* closed is the declaration form. There is no
  `[T]` syntax, so: (a) the rule is positional-by-spelling — a one-letter
  signature type is generic whether or not the author meant it, and a typo'd
  one-letter name silently becomes a type parameter rather than an error;
  (b) parameters cannot be bounded or shared across signatures; (c) only `fn`
  and `extern` signatures quantify — a one-letter name in a record field or an
  ADT payload is an ordinary opaque nominal type, like any other undeclared
  name (undeclared multi-letter names such as the `Row` in `-> List[Row]` are
  deliberately opaque, since service returns are host-shaped). Closing it needs
  explicit type-parameter syntax in the parser.

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
- **T1–T17** are the type-soundness refusals. `t8`–`t17` were added with the
  typing follow-ups above and are deliberately paired: each rejection file
  states the tier error it prevents, and `tests/test_typesafety.py` asserts
  both the refusal *and* the legal spellings it must not touch (a `fn`
  returning nothing needs no `return`; `if`/`else` where both arms return is
  fine; `o?.name`, `o?.name ?? d` and a `match` unwrap all stay accepted;
  `xs[0]`, `s.charAt(0)` and a `_` arm stay accepted). A soundness check with
  no false-positive test is a check nobody can safely tighten later.
