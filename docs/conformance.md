# Conformance: every construct against every tier

**Run it:** `python3 tools/conformance.py [--json] [--validate]` · 50 cases ×
5 emitters. The default sweep needs no toolchain (every emitter is pure
Python); `--validate` additionally hands each tier's output to that tier's
real compiler.

`tests/test_cross_tier.py` holds a floor for a handful of constructs known to
be portable. This walks the *whole* surface and reports what each tier does
with each construct, so a gap is data rather than something discovered by a
user targeting a tier by hand.

## Two questions, not one

The matrix asks **did the emitter produce code?** and, under `--validate`,
**does that code hold up in the real toolchain?** They are different
questions, and for a long time only the first was asked — see "What the
second question found" below.

| tier | validator | depth |
|---|---|---|
| python | `compile()` + a `symtable` scope walk | syntax + unbound names |
| typescript | `tsc` through the compiler API | full typecheck |
| rust | `cargo check` over a single generated crate | full typecheck |
| java | `javac` against the checked-in cordis4j stubs | full typecheck |
| wasm | `wasmtime compile` | module validation (types, locals) |

A tier whose toolchain is missing reports **`unavailable` with the reason**,
never `ok`. "Nothing checked it" and "it passed" must not render as the same
cell, since conflating them is what hid the bugs below. rust needs crates.io
reachable (cordis-rs resolves from the index) and java needs a real JDK; both
run in CI.

## Emit sweep

Counts below are from the run at the commit that closed the first sweep.
**python and typescript are at zero; every remaining refusal on rust, java
and wasm is a deliberate tier limit.**

| tier | refusals | real gaps | deliberate |
|---|---|---|---|
| python | 0 | 0 | — |
| typescript | 0 | 0 | — |
| rust | 3 | 0 | 3 externs with no `@rs` body |
| java | 3 | 0 | 3 externs with no `@java` body |
| wasm | 19 | 0 | i32-only signatures (10), no config channel (2), no host builtins (3), Opt/Str at the boundary (2), externs (2) |

A refusal is only a *gap* when the tier could reasonably express the
construct. An extern with no body for that backend, or a `Str` on the
i32-only wasm tier, is the toolchain working as designed.

## What the sweep closed

Starting from 12/5/5/32 (ts/rust/java/wasm) plus two frontend gaps:

- **Frontend**: `match` is now usable in component and method bodies, not
  only in `fn` bodies; a bare `return` parses, which is the natural body of
  a void service operation. `??` is now type-checked — it requires an
  optional on the left, which three tiers could not render and python
  silently accepted.
- **TypeScript** (14 → 0): `fn` call nodes, `if`/`fail` steps, `match` arm
  bindings, `??` in component bodies, bare `return`.
- **rust / java** (7 → 3, 6 → 3): `??` implemented on both — lazily, as
  `unwrap_or_else(|| b)` on `Option<T>` and `orElseGet(() -> b)` on
  `Optional<T>`, since the eager forms would evaluate a fallback that is not
  needed; `match` in component bodies; bare `return` (which had been
  *crashing* with a raw `AttributeError` rather than refusing); rust
  `config` inside a guard; java renames a keyword-colliding `fn` instead of
  rejecting it.
- **wasm** (34 → 19): the component path now delegates every non-i32-native
  kind to the v3 value engine rather than duplicating it — `if`, `fn` calls
  (with the called functions lowered into the component's own module),
  `list`/`record`/`index`/stdlib, `match` + ADT construction, bare `return`,
  and `??` on an in-module `Opt`. Every refusal that remains now names the
  i32 boundary rather than reporting an unknown kind.

## Sharper root causes found along the way

- **`call` is ambiguous by kind across the two dialects.** The component
  form is `target`/`method`; the 2.0 form is `callee`/`args`. Dispatching on
  kind alone can silently take the wrong branch and read a missing child —
  the one place the two-renderer split fails *quietly* instead of loudly.
  Dispatch must key on shape.
- **The component IR dialect is not uniform.** `Some(x)` and `None` arrive
  in *v3* spelling inside component bodies while their neighbours use the
  component spelling. Backends currently normalize this themselves; the
  frontend should emit one dialect per position.
- **Two latent v3-renderer bugs** were invisible from `fn` bodies (which
  always supply an expected type) and only surfaced under component
  delegation: match arm payload bindings not being in scope when the arm's
  type was inferred, and an arm without a payload type not consulting the
  variant layout.

## What the second question found

The blind spot was documented here before it was closed, with a known
instance: the **rust backend did not capture `requires` bindings into the
provider struct**, so a component with no effects emitted Rust referencing a
free variable. It reported `ok` for months and was caught only because an
agent compiled the output by hand.

The first validated run found three more, all in TypeScript, all invisible to
an emit-only sweep and all live in a tier the matrix called clean:

- **Required services were never declared on `Context`.** The emitter
  augmented cordis's `Context` with a component's *provisions* only, so
  `ctx.bus.send(x)` — the emission path, on six of the fifty cases — hit a
  `Context` with no `bus`. This is the **same bug as the rust one in a
  different spelling**: both tiers rendered a required binding they had never
  brought into scope. Fixed by augmenting with requirements as well; keys
  repeated across emitted files merge, since identical interface members do.
- **The A1 iteration boundary emitted `yield null`.** cordis types a yielded
  value as `Disposable<T> = () => T`, and `null` is not one. Fixed by
  yielding a no-op disposer, which is the same semantics — a boundary has
  nothing to revert — and typechecks.
- **Arrow parameters were implicitly `any`**, which `strict` rejects. They
  are now explicitly `any`: arrows are in the checker's *enumerated*
  unchecked remainder, so the compiler has no type to emit, and an admission
  beats a guess. Inferring them is a typing-frontier item, not a codegen one.

Then a JDK was installed and the java tier was validated for the first time.
It emits 47 of 50 cases and **13 of those do not compile** — `long` where a
`String` is expected, unresolved symbols, `unexpected type` on generics, and a
provider that does not implement its own interface for record/ADT parameters.
The tier's own suite is green, because it asserts on emitted *strings* plus a
javac gate over four hand-picked examples; nothing walked the surface. They
are baselined in `tests/test_conformance_validate.py` — new breakage fails,
and a baselined case that starts passing also fails, so the list can only
shrink.

Once crates.io was reachable, all 47 rust cases went through `cargo check`
together for the first time: 44 passed, 3 did not — and all three were
failing on java too, which is what identified them as shared causes rather
than three coincidences. Chasing them found three bugs and closed five more
java cases as a side effect:

- **The `T` -> `Opt[T]` injection was never materialized.** `compatible()`
  lets a `T` stand where `Opt[T]` is declared, but nothing turned the *value*
  into an optional, so a method declared `-> Opt[Int]` returning `1` emitted
  `Option<i64> { 1 }` and `Optional<Long> { return 1L; }`. Fixed in the
  frontend (`_inject_opt` in lower.py), not per backend: the frontend is the
  single IR producer, and a backend deciding this would need type information
  the emitters do not carry. Untyped tiers never noticed — the injection is
  invisible on python and TypeScript, which is exactly why it survived.
- **Java put primitives in generic positions.** `Opt[Int]` rendered as
  `Optional<long>`, which is not a Java type. Type *arguments* are now boxed
  (`Optional<Long>`) while the same revl type stays primitive in parameter
  and return position. This one fix closed five cases: `Opt`, `List` and
  `Map` services, `??`, and `Some`/`None`.
- **The two-renderer `call` ambiguity fired for real.** The injected `Some(..)`
  is v3-shaped (`callee`/`args`) and lands in component positions, where the
  TypeScript v1 renderer read `method` off it, got Python's `None`, and
  reported it as a bad identifier. Dispatch now keys on *shape*, as this
  document already prescribed.

**What remains is one shared cause, not two bugs.** `component/await` and
`method/method-time effect` fail on rust and java for the same reason: host
builtins (`Job`, `Map`) have **no declared signatures**, so the frontend
cannot check an argument against them. `await Job.run(1)` passes an `Int`
where the java and rust host stubs take a name (`String`), and
`m.insert(x, x)` uses `Int` keys against a `String`-keyed map. python and
TypeScript accept both because their hosts are untyped.

Fixing it is a language decision, not a codegen one, and the tiers disagree
today: `docs/backend-ir-v1.md` specifies `Job.run(name)`, while the wasm tier
is i32-only and `examples/pulse.rvl` calls `Job.run(1)`. Typing the host
boundary — and rejecting a mismatch, the way service declarations already
bound their providers — would settle it, at the cost of breaking whichever
spelling loses. That is the open question, and it belongs on the G8 boundary
surface where the rest of the host contract lives.

The rust failure CI *did* catch is fixed here, and it is the same shape as
the others: revl's `+` on strings lowered to Rust's `+`, which
accepts `String + &str` and rejects both `&str + String` and `String +
String`. It now lowers to `format!`, which accepts every combination. Only
`cargo check` in CI could see it — see "what is still not checked" below.

The residue is honest rather than zero:

- python's validator reaches syntax and unbound names, not types — the tier
  emits untyped Python, so there is nothing deeper to check.
- wasm validates modules; it does not drive the component protocol. The wasm
  suite executes emitted components separately.
- rust needs crates.io reachable — specifically `index.crates.io` and
  `static.crates.io`, which are *different hosts* from `crates.io` itself. A
  network that serves the website and the `cargo search` API can still fail
  every resolve, which is exactly how this tier went unvalidated. The
  validator skips with that reason rather than reporting clean.
- Validation proves the output *compiles*, still not that it *behaves*.
  Cross-tier `test` execution (`revl test --backend`) is the next rung.

## What this says about the architecture

Every gap above is a **renderer** divergence, not a semantic one: the
frontend produces a well-formed node and some backend has no case for it.
None of them threatens a guarantee, and all of them are invisible to
single-tier testing — which is why this file exists and why the matrix
belongs in CI.

The structural fix, once the immediate gaps are closed, is to stop having two
expression renderers per backend. Each emitter should own **one** expression
function covering every IR kind, with tier limits expressed as explicit
refusals rather than missing cases. python — the tier with a single 17-kind
renderer — is the one with zero gaps, which is not a coincidence.
