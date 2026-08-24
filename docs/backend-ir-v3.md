# Backend IR v2 / v3 — deltas over v1

`docs/backend-ir.md` (v0) and `docs/backend-ir-v1.md` are frozen historical
contracts. This file carries everything a backend author needs that arrived
after them. `ir_version` is the **maximum feature tier a document uses**, so a
v1-shaped source still emits `ir_version: 1` byte-identically.

| version | adds | backends |
|---|---|---|
| 2 | realms and interception | all six |
| 3 | types, pure functions, externs, tests, and the component-body constructs below | all six |

Ground truth is `tools/conformance.py`: it emits 49 constructs through every
backend and reports what each does. A tier that cannot express something must
raise a clear `EmitError` naming the limit — never emit wrong code, never
crash. (`docs/conformance.md` records where that contract has been broken.)

## v2 — realms and interception

A component may carry `isolate` and `intercept` placements, both resolved at
compile time and emitted as component metadata:

```jsonc
"isolate":   {"kv": "tenant_a"},        // key -> realm label
"intercept": {"db": {"limit": 1}}       // key -> static metadata object
```

Realm labels are static (a dynamic label is refused by the frontend).
Provision disjointness (G2) is checked per `(key, realm)` pair, so two
components may provide the same key in different realms. See
`docs/design-v2-realms.md`.

## v3 — document-level additions

```jsonc
"types":     {"Row": {"kind": "record",  "fields": {"id": "Int"}},
              "Outcome": {"kind": "variant",
                          "cases": [{"name": "Found", "payload": "Int"},
                                    {"name": "Missing", "payload": null}]}},
"functions": [{"name": "add", "params": [{"name": "a", "type": "Int"}],
               "returns": "Int", "public": true, "body": [<step>...],
               "async": true}],
"externs":   [{"name": "sha", "class": "pure|acquire|emission",
               "bodies": {"py": "...", "ts": "..."},
               "async": true,
               "undo": <expr>, "compensate": <expr>}],
"tests":     [{"name": "inc", "body": [<step>...]}]
```

- **`class` on an extern is load-bearing**: `pure` is trusted, `acquire`
  carries an inverse, `emission` is irreversible. A backend with no body for
  its own tag must refuse — that refusal is deliberate, not a gap.
- **`"async": true`** (additive; absent means sync) marks a suspension color
  (docs/design/async-extern.md). On an extern it is legal only for `class:
  emission`; on a `function` entry it is *derived* — the frontend colors a fn
  that transitively reaches an async extern and stamps the flag, so an emitter
  needs no reachability analysis of its own. Colored tiers (ts, py) emit an
  `async function`/`async def` and `await` every admitted call site; the
  synchronous tiers (rust, go) erase the color; wasm refuses. `ir_version`
  stays 3 — the key is purely additive.
- A **function name may need renaming** for a host (java rejects `double`);
  the emitter renames consistently at declaration and call sites.

## Steps a component or method body may contain

Beyond v1's `let-effect` / `effect` / `emit` / `provide` / `await` / `return`:

| step | where | meaning |
|---|---|---|
| `let` | method bodies, fn bodies, block-effect setup | plain value binding: `{"step": "let", "name", "value", "mutable"}` |
| `assign` | same | reassignment of a `mutable` binding |
| `if` | component bodies | guard, with `then`/`else` step lists |
| `fail` | component bodies | deliberate L-Raise (A8) |
| `return` with `"expr": null` | method bodies | a void service operation |

`let`/`assign` in a **method** body are new in this tier: a method may name an
intermediate instead of nesting everything into one expression. Backends
render them as host locals (`let`/`const`/`var`/`(local $x i32)`).

**`{"step": "return", "expr": null}` is legitimate** and must be handled. It
crashed three backends with a raw `AttributeError` before being caught — the
contract is an `EmitError` or support, never an exception.

## Expression kinds, and the dialect hazard

A component body mixes two dialects and **this is by design**: `let msg =
prefix + name` is a pure `bin` sitting beside a `req` call.

| dialect | kinds |
|---|---|
| component | `req`, `config`, `name` (`id`), `host`, `format`, `fn` (call by name) |
| 2.0 / pure | `var`, `bin`, `un`, `if`, `list`, `record`, `index`, `field`, `builtin`, `match`, `adt`, `arrow`, `interp`, `optfield`, `optcall`, `len` |

Three hazards, each of which has already cost a backend a bug:

1. **`call` exists in both dialects with different shapes** — component form
   is `target`/`method`, 2.0 form is `callee`/`args`. **Dispatch on shape,
   not on kind**: keying on kind alone silently takes the wrong branch and
   reads a missing child instead of failing.
2. **The dialects are not cleanly separated by position.** `Some(x)` and
   `None` arrive in *2.0* spelling inside component bodies. Backends
   normalize this privately today; unifying it is on the roadmap.
3. **Two renderers per backend is the root cause of most divergence.** A kind
   in neither falls through the crack. python — the tier with the fewest
   split paths — is the tier with zero gaps. The structural fix is one
   expression renderer per backend, with tier limits as explicit refusals.

## `arrow` and function types

An `arrow` node carries `params` (names) and `body`, plus `captures` in the
pure-fn dialect. When the checker recovered a signature for it, it *also*
carries `param_types` (parallel to `params`) and `returns`:

```json
{ "kind": "arrow", "params": ["v"], "param_types": ["Int"], "returns": "Int",
  "captures": [], "body": { ... } }
```

Both keys are **absent together** exactly when the arrow is still untyped
(`let g = v => v + 1`, with no expected type and no `(v: Int)` annotation).
That is the distinction a backend needs: "typed as `Any`" and "no type at
all" are not the same, and only the second justifies emitting an admission
such as TypeScript's explicit `any`. A declared function type is spelled
`(Int, Str) -> Bool` wherever a type appears — see docs/function-types.md,
which also records which tiers refuse values of function type and why.

## `match`, ADTs and `Opt`

- `Opt` is **not tagged**: `Some(x)` is `x` and `None` is the host's absent
  value, so a `match` on an `Opt` is a null check.
- `Result` and user variants **are tagged**: construction lowers to
  `{"kind": "adt", "type", "case", "args"}` and `match` dispatches on the tag.
- Arm bindings must be in scope **before** the arm body is rendered *and*
  before its type is inferred — a latent bug in two backends came from
  inferring an arm's type in a scope where its payload binding was missing.

## `??` and emissions

- `??` requires an `Opt` on the left (the frontend now enforces it). Lower it
  **lazily** — `unwrap_or_else(|| b)`, `orElseGet(() -> b)` — since the eager
  forms evaluate a fallback that may not be needed.
- `emit` may appear in **value position**, so an emission's result can be
  used. The node is the call itself; the marker is a frontend concern.
- A service operation declared plain **cannot** reach an emission in any
  provider (G4 upper bound), so a backend may trust the declaration when
  deciding whether an operation is irreversible.
