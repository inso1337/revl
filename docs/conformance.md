# Conformance: every construct against every tier

**Run it:** `python3 tools/conformance.py [--json]` · 48 cases × 5 emitters,
no toolchain required (every emitter is pure Python).

`tests/test_cross_tier.py` holds a floor for a handful of constructs known to
be portable. This walks the *whole* surface and reports what each tier does
with each construct, so a gap is data rather than something discovered by a
user targeting a tier by hand.

Counts below are from the run at the commit that added this file. **python is
the reference tier: 0 gaps.**

| tier | refusals | of which deliberate |
|---|---|---|
| python | 0 | — |
| typescript | 12 | 2 (externs without a `@ts` body) |
| rust | 5 | 3 (externs without an `@rs` body) |
| java | 5 | 3 (externs without a `@java` body) |
| wasm | 32 | ~26 (i32-only tier, no config, no host builtins) |

A refusal is only a *gap* when the tier could reasonably express the
construct. An extern with no body for that backend, or a `Str` on the
i32-only wasm tier, is the toolchain working as designed — those are listed
as deliberate and belong in `EXPECTED_LIMITS` in the cross-tier test.

## The real gaps, worst first

### 1. `??` is unimplemented on three tiers

`x ?? 0` — a documented 2.0 operator (syntax-2.0 §3.2) — compiles on python
and, in `fn` bodies only, TypeScript. **rust, java and wasm reject it
outright**, in both `fn` and method bodies: `unsupported binary operator
'??'`. Any program using nullish coalescing is python-only today.

The lowering is well-formed (`{"kind": "bin", "op": "??"}`), so this is
purely three unimplemented operator cases.

### 2. Calling a pure `fn` from a component body fails on TypeScript and wasm

`provide s { fn f(x) = double(x) }` where `double` is a top-level `fn`
produces a `fn` call node that neither tier's component-path renderer knows
(`unsupported v3 expression kind 'fn'`). This is the single largest cluster —
9 of TypeScript's 12 refusals are this one cause, because every `fn/*` case
in the corpus calls its function from a component.

Same root cause as the divergences already fixed: two expression renderers
per backend with different kind sets. The `fn` node is simply in neither.

### 3. `match` cannot be used in a component or method body

Frontend-level, so it affects every tier equally: `unsupported expression in
component effect block`. `match` is the eliminator for ADTs and works in
`fn` bodies, but a provide-method cannot use it — which pushes any component
that consumes a `Result` or user variant into calling out to a `fn`.

### 4. A bare `return` does not parse

`fn f(x) { return }` — the natural body for a service operation declared with
no return type — is a parse error (`expected an expression`). The IR and the
java/rust emitters already model `{"step": "return", "expr": null}`, so only
the parser is missing it.

### 5. Smaller, tier-local

- **TypeScript**: `if` steps in a component body are unknown (`unknown step:
  'if'`), so `fail` guards do not lower; `??` in a *component* body reports
  `malformed v3 expression: None` (distinct from gap 1 — here the handler
  exists but reads a child it did not get).
- **java**: a `fn` whose name collides with a Java reserved word is
  *rejected* (`function name identifier collides with … 'double'`) rather
  than renamed. A3 renaming already solves this for bindings; function names
  need the same treatment.
- **rust**: `config` access inside a `fail`/`if` guard expression is unknown
  to its v3 renderer.
- **wasm**: beyond the deliberate i32 limits, its component-path renderer
  does not know `if` (ternary), `list`, `record`, or `fn` calls — the same
  two-renderer split, and the reason its count is high.

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
