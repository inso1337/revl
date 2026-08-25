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
| rs | `Value::new(serde_json::from_str(&s))` | `serde_json::to_string(v.downcast())` | **runs** (item 140): `Any` erases to `cordis::Value` (a cloneable `Arc<dyn Any>`); the body boxes a parsed `serde_json::Value` into it and recovers it to re-encode (`serde_json` is already pinned in the emitted crate). A structured document survives `stringify∘parse`; `cargo test` runs the round-trip (backends/rust/scenarios/jsonwire.rvl → golden jsonwire.rs). Reading a parsed value into a typed binding for field access is still the erased-`Value` boundary. |
| java | — | — | the tier refuses with `extern `json_parse` has no @java body — not portable to this backend`; a provider's Jackson/Gson plugs in here when one is on the `javac` classpath |
| go | `json.Unmarshal([]byte(s), &v)` | `json.Marshal(v)` | **runs** (item 140): `Any` erases to Go's `any`; the @go body reaches `encoding/json` through a `//revl:import encoding/json` directive the emitter hoists into the module's import block. `go test` runs the round-trip (backends/go/scenarios/emitted/jsonwire/). |
| wasm | — | — | the substrate value model (Int/Bool/Str/List) cannot hold Float/Map/records, so a JSON value has no representation there |

The refusal message is the built-in honesty gate, the same shape the
conformance corpus's three bodyless externs already ride (docs/conformance.md):
a tier that cannot run the module says so at emit time, never silently.

## Crossing to rust and go (item 140)

`json_parse`/`json_stringify` now ship `@rs` and `@go` bodies, so a
composition that carries structured data over JSON crosses to four tiers, and
the executable round-trip is pinned per tier (`cargo test` / `go test`):

- **rust** — `Any` erases to `cordis::Value`, a cloneable `Arc<dyn Any + Send
  + Sync>`. The @rs body parses with `serde_json::from_str::<serde_json::
  Value>` and boxes the result with `Value::new(..)`; `json_stringify`
  recovers it with `v.downcast::<serde_json::Value>()` and re-encodes with
  `serde_json::to_string`. `serde_json` is already pinned in the emitted
  crate's Cargo.toml, so no new dependency is introduced. The value stays
  opaque at the type level — a parsed value read into a typed binding for
  field access (`let tc: ToolCall = json_parse(s); tc.name`) is still the
  erased-`Value` boundary — but a structured document survives
  `stringify∘parse`, which is what a wire protocol needs.
- **go** — `Any` erases to Go's `any` (`interface{}`), exactly the shape
  `encoding/json` decodes into (`map[string]any`, `[]any`, `string`,
  `float64`, `bool`, `nil`). A verbatim extern body cannot spell its own
  `import`, so the @go body carries a **`//revl:import encoding/json`**
  directive on its own line; the emitter lifts the package to the module's
  import block and drops the directive from the emitted function body. This
  is the general seam — any `//revl:import <path>` in a @go body is hoisted —
  kept minimal rather than special-cased to `encoding/json`.

What is still left:

1. **java**: no JDK JSON API; add `@java` bodies against a provider jar
   (gson/jackson) and extend the run/test classpath to include it.
2. **wasm**: needs a richer value model (Float, Map, records) on the
   substrate tier — the same prerequisite FR-4/FR-11 raise; documented as a
   deliberate tier refusal until then.

## The multi-tier tradeoff (recorded item 81, closed for rust/go by item 140)

This module's per-tier scope is not a private stdlib detail — it decides how
wide a *composition that carries structured data* can run. The harness made
that concrete. Its milestone-2 switch from a string wire protocol
(`TOOL_CALL add 2 3`, parsed by hand) to a JSON one (`{"kind":"tool",…}`,
parsed by `json_parse`) moved the composition's reach from **"runs on
py/ts/rust/go" to "runs on py/ts"** — because the moment the wire format is
JSON, every tier in the composition must be able to parse JSON, and only py
and ts had `@py`/`@ts` bodies for it. Item 81 recorded that narrowing (the
honesty gate working: no body means an emit-time refusal, never a silent
mis-emit) and named the two structural blockers.

**Item 140 removed both**, so the JSON wire protocol now crosses to rust and
go as well (see "Crossing to rust and go" above):

- **rust** — the `Any` return still erases to `cordis::Value`, but that is
  enough for a wire protocol: the @rs body boxes a parsed `serde_json::Value`
  into the erased value and recovers it to re-encode, so a structured document
  survives `stringify∘parse`. (Reading a parsed value into a typed binding for
  field access — `let tc: ToolCall = json_parse(s); tc.name` — remains the
  erased-`Value` boundary, a narrower gap than "no body at all".)
- **go** — the `//revl:import` hoist lets the @go body reach `encoding/json`,
  and `Any` erases to Go's `any`, the shape `encoding/json` decodes into.

Java and wasm still refuse at emit time (no JDK JSON API on the run classpath;
no rich-enough value model on the substrate). For those two tiers **the string
protocol remains the full-tier fallback**: a composition that flattens its
wire format to `name arg1 arg2` runs on all six tiers, at the price of
positional strings rather than a structured document. The ts tier also still
carries a residual blocker for a *real* provider (item 80, async extern
bodies: an HTTP call is a `Promise`, and extern bodies cannot yet `await`).

## Why externs, not builtins

FR-6/FR-9 (`startsWith`/`endsWith`/`to_int`) are *builtins*: the lowering
per tier is a one-liner and the surface rule ("a method call must name one
of the builtins") admits them. A JSON *parser* is not a one-liner on any
tier, and the harness's provider integration is exactly the place where the
implementation should be the host's own JSON library — that is what the
extern body mechanism is for. The two mechanisms meet the same admission
rule: a call must name something declared.
