# The stdlib JSON module (FR-3)

**The need:** real tool calls carry structured args (DSH tools take JSON);
revl had no JSON story, so the harness's mock wire protocol was flattened to
`TOOL_CALL name arg1 arg2` and parsed by hand. A real LLM's JSON tool-call
output is not expressible that way. This module is the first fix: a
documented stdlib surface (the FR-3 "suggested shape") built on the extern
mechanism, scoped to the value types the harness needs — Str/Int/Bool/Float/
List/Opt/records.

## The surface

`use "stdlib/json.rvl" { json_parse, json_stringify }` — the file lives in
the repo as `stdlib/json.rvl`, one `pub extern pure fn` per operation:

| fn | signature | semantics |
|---|---|---|
| `json_parse(s)` | `Str -> Any` | the JSON document in `s` as a revl value |
| `json_stringify(v)` | `Any -> Str` | a revl value as a JSON document |

The return type is **`Any`**, the wildcard of the type algebra — the honest
type for a value whose static type only the runtime knows. `Any` flows into
any typed position, which is exactly the harness pattern:

```revl
// `json_parse` comes from stdlib/json.rvl via `use` (the surface above)
pub extern pure fn json_parse(s: Str) -> Any
  = @py { import json; return json.loads(s) }

type ToolCall = { name: Str, args: List[Str] }

fn tool_name(s: Str) -> Str {
  let tc: ToolCall = json_parse(s)   // Any flows into the record position
  return tc.name
}
```

`Any` is deliberately *not* a "JSON value" ADT: the tiers already carry the
host representations (python dict/list/str/int/float/bool/None, TS
object/array/string/number/boolean/undefined), and re-wrapping them into a
revl variant on every tier is exactly the broad surface this pass chose not
to build. The type system's wildcard does the admission for free.

## Implementation: extern bodies per tier

Each operation is an `extern` whose implementation is the `@<backend>` body
(docs/syntax-2.0.md §extern; the lowerer keeps one body text per backend and
each emitter refuses an extern that has no body for its tier):

| tier | parse | stringify | status |
|---|---|---|---|
| py | `json.loads(s)` | `json.dumps(v)` | **executed by tests** (stdlib) |
| ts | `JSON.parse(s)` | `JSON.stringify(v)` | emitted by tests (builtin) |
| rs | — | — | an `Any` extern return type-erases to `cordis::Value` on this tier (a cloneable `Arc<dyn Any>`), so a parsed record cannot be read back (`tc.name` has no field on `Value`); the module ships no `@rs` body until the emitter types an `Any`-typed binding by its declared type. The tier refuses with `extern `json_parse` has no @rs body — not portable to this backend` |
| java | — | — | the tier refuses with `extern `json_parse` has no @java body — not portable to this backend`; a provider's Jackson/Gson plugs in here when one is on the `javac` classpath |
| go | — | — | same refusal; the go emitter adds imports for builtins, not for verbatim extern bodies, so `encoding/json` cannot reach the module yet |
| wasm | — | — | the substrate value model (Int/Bool/Str/List) cannot hold Float/Map/records, so a JSON value has no representation there |

The refusal message is the built-in honesty gate, the same shape the
conformance corpus's three bodyless externs already ride (docs/conformance.md):
a tier that cannot run the module says so at emit time, never silently.

## What is left (the path to the other tiers)

1. **rust**: an `Any` extern return is a `cordis::Value` (type-erased
   `Arc<dyn Any + Send + Sync>`) on this tier, and the emitter types a
   binding by the extern's return, not by its declared annotation — so the
   harness's core pattern (`let tc: ToolCall = json_parse(s); tc.name`)
   cannot compile. The fix is an emitter/type-flow feature: when a binding
   has an explicit declared type, type it by that (the checker already
   admits it), and only then add `@rs` bodies (`serde_json` is already a
   dependency of the emitted crate).
2. **go**: teach the go emitter to add `encoding/json` (and any import an
   extern body needs) to the module's import block, then add `@go` bodies.
   The parse semantics must map JSON values onto the tier's representations
   (`map[string]any`, `[]any`, `string`, `float64`, `bool`, `nil`).
3. **java**: no JDK JSON API; add `@java` bodies against a provider jar
   (gson/jackson) and extend the run/test classpath to include it.
4. **wasm**: needs a richer value model (Float, Map, records) on the
   substrate tier — the same prerequisite FR-4/FR-11 raise; documented as a
   deliberate tier refusal until then.

## Why externs, not builtins

FR-6/FR-9 (`startsWith`/`endsWith`/`to_int`) are *builtins*: the lowering
per tier is a one-liner and the surface rule ("a method call must name one
of the builtins") admits them. A JSON *parser* is not a one-liner on any
tier, and the harness's provider integration is exactly the place where the
implementation should be the host's own JSON library — that is what the
extern body mechanism is for. The two mechanisms meet the same admission
rule: a call must name something declared.
