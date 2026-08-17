# Conformance: every construct against every tier

**Run it:** `python3 tools/conformance.py [--json]` · 48 cases × 5 emitters,
no toolchain required (every emitter is pure Python).

`tests/test_cross_tier.py` holds a floor for a handful of constructs known to
be portable. This walks the *whole* surface and reports what each tier does
with each construct, so a gap is data rather than something discovered by a
user targeting a tier by hand.

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

## Known blind spot of this matrix

It checks that an emitter **does not raise** — never that its output
compiles or runs. A tier can therefore report `ok` and still emit broken
code. One live instance: the **rust backend does not capture `requires`
bindings into the provider struct**, so a provide-method calling a required
service emits Rust that references a field that does not exist. Pre-existing
and unrelated to this sweep, invisible here, and caught only because an
agent compiled the output by hand. Closing that blind spot means compiling
or executing emitted code per tier — which the wasm tier already does on
wasmtime, and the rust tier does under `cargo` when a network is available.

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
