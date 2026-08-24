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

## The multi-tier tradeoff (decision recorded, roadmap item 81)

This module's per-tier scope is not a private stdlib detail — it decides how
wide a *composition that carries structured data* can run. The harness made
that concrete. Its milestone-2 switch from a string wire protocol
(`TOOL_CALL add 2 3`, parsed by hand) to a JSON one (`{"kind":"tool",…}`,
parsed by `json_parse`) moved the composition's reach from **"runs on
py/ts/rust/go" to "runs on py/ts"** — because the moment the wire format is
JSON, every tier in the composition must be able to parse JSON, and only py
and ts have `@py`/`@ts` bodies for it.

The narrowing is not a bug; it is the honesty gate working. `json_parse` ships
no `@rs`/`@go` body (the FR-3 scope above), so rust and go refuse at emit time
rather than shipping something broken. The exact message a user sees on the
rust tier is:

```
extern `json_parse` has no @rs body — not portable to this backend (available: py, ts)
```

and identically on go (`no @go body … (available: py, ts)`). Two independent
reasons keep those bodies off today, both structural rather than incidental:

- **rust** — an `Any` extern return type-erases to `cordis::Value` (a
  cloneable `Arc<dyn Any + Send + Sync>`), and the emitter types a binding by
  the extern's *return*, not by its declared annotation. So even with a
  `serde_json` body, the harness's core pattern
  (`let tc: ToolCall = json_parse(s); tc.name`) could not read a field back —
  the parsed value is opaque at the type level.
- **go** — the go emitter adds imports for *builtins*, not for verbatim extern
  bodies, so a `@go` body cannot reach `encoding/json`. The import machinery
  has to learn to pull in what an extern body names before any body can run.

**The decision:** structured args on all six tiers needs the JSON module to
gain `@rs`/`@go` bodies — on rust, type an `Any`-binding by its declared type
(then `serde_json`); on go, wire `encoding/json` into the module's import
block (see "What is left" above for the full per-tier path) — *or* a per-tier
wire protocol negotiated at the seam. Until then, **the string protocol
remains the full-tier fallback**: a composition that flattens its wire format
to `name arg1 arg2` runs on py/ts/rust/go/java/wasm, and the price is that the
args are positional strings rather than a structured document. A composition
that wants structured args over JSON is a py/ts composition today — and even
the ts half carries its own residual blocker for a *real* provider (item 80,
async extern bodies: an HTTP call is a `Promise`, and extern bodies cannot yet
`await`).

## Why externs, not builtins

FR-6/FR-9 (`startsWith`/`endsWith`/`to_int`) are *builtins*: the lowering
per tier is a one-liner and the surface rule ("a method call must name one
of the builtins") admits them. A JSON *parser* is not a one-liner on any
tier, and the harness's provider integration is exactly the place where the
implementation should be the host's own JSON library — that is what the
extern body mechanism is for. The two mechanisms meet the same admission
rule: a call must name something declared.
