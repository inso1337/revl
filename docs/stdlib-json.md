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
| py | `json.loads(s)` | `json.dumps(v, separators=(",",":"), ensure_ascii=False)` | **executed by tests** (stdlib); compact + raw UTF-8 for the canonical form (item 385) |
| ts | number-preserving recursive descent | `JSON.stringify(v, replacer)` | emitted by tests; parse decodes a JSON integer literal to a JS `bigint` and a float to a `number` (item 311), stringify wraps `JSON.stringify` in a bigint replacer (item 281) — see the `Int` note below |
| rs | `Value::new(serde_json::from_str(&s))` | `serde_json::to_string(v.downcast())` | **runs** (item 140): `Any` erases to `cordis::Value` (a cloneable `Arc<dyn Any>`); the body boxes a parsed `serde_json::Value` into it and recovers it to re-encode (`serde_json` is already pinned in the emitted crate). A structured document survives `stringify∘parse`; `cargo test` runs the round-trip (backends/rust/scenarios/jsonwire.rvl → golden jsonwire.rs). Reading a parsed value into a typed binding for field access is still the erased-`Value` boundary. |
| java | — | — | the tier refuses with `extern `json_parse` has no @java body — not portable to this backend`; a provider's Jackson/Gson plugs in here when one is on the `javac` classpath |
| go | `json.Unmarshal([]byte(s), &v)` | `json.NewEncoder` with `SetEscapeHTML(false)` | **runs** (item 140): `Any` erases to Go's `any`; the @go body reaches `encoding/json` through `//revl:import` directives the emitter hoists into the module's import block. Encodes compact with `<`/`>`/`&` raw for the canonical form (item 385). `go test` runs the round-trip (backends/go/scenarios/emitted/jsonwire/). Record caveat: unexported struct fields marshal to `{}` — see the canonical-form section. |
| wasm | — | — | the substrate value model (Int/Bool/Str/List) cannot hold Float/Map/records, so a JSON value has no representation there |

The refusal message is the built-in honesty gate, the same shape the
conformance corpus's three bodyless externs already ride (docs/conformance.md):
a tier that cannot run the module says so at emit time, never silently.

## Field access on a dynamic value, and reserved-word keys (items 279, 299)

Reading a field off a *dynamic* value — `tc.function` where `tc: Any` came
straight from `json_parse`, without an intervening typed binding — is a
**py / ts capability only**. Those two tiers carry a runtime key reader (py's
`_revl_field(v, name)`, ts's `obj["key"]`), so the access reaches the raw
runtime key. The statically-typed tiers do not: `Any` erases to Go `any`,
Java `Object`, Rust `cordis::Value`, none of which has arbitrary members, so a
dynamic field access emits a *static* field selection the target compiler
rejects (`type any has no field`, `cannot find symbol`, `no field on type
Value`); wasm has no dynamic value at all and refuses `json_parse` at emit. The
supported shape on every tier is to read the value into a typed binding first
(`let tc: ToolCall = json_parse(s)`), where the field access is a real struct
field.

Item 279 fixed a **silent** form of this on the ts tier: a JSON key named by a
host reserved word (`function`) was renamed on the access (`tc.function` ->
`tc.function_`) while the runtime object kept the raw key, so the ts read was
`undefined` while py read the raw key and worked — the same admitted program
diverging silently between tiers. Item 299 audited rust/go/java/wasm for the
same class and found **none of them reproduce it**: the reserved-word sanitizer
does still fire on the access (Go/Java/Rust mangle a target-reserved key to
`<name>_`), but it is moot, because the mangled access is that same static
selection the compiler rejects — a *loud* build error, never a silent wrong
value. The silent read was unique to JS, where `obj.function` / `obj["function"]`
is a live dynamic lookup on any object. No lower/typecheck or emitter change was
needed; `tests/test_dynamic_reserved_key_cross_tier.py` locks the finding.

## `Int` fields on the ts tier: bigint, not a throw (item 281)

The ts backend maps revl `Int` to JS `bigint` (`backends/typescript/emit.py`
TYPE_MAP), for exact 64-bit two's-complement fidelity. The builtin
`JSON.stringify` throws `TypeError: Do not know how to serialize a BigInt` on
any bigint, so before item 281 `json_stringify` of *any* record carrying an
`Int` field (a token count, an id, a timestamp, an `max_tokens`) type-checked
and emitted clean, then died at runtime on the ts tier only (the py tier
serialized it fine). That is a compiles-implies-runs divergence, found in the
lighthouse workload's provider layer.

The `@ts` stringify body now passes a replacer to `JSON.stringify` that renders
each `bigint` as a **bare JSON number** carrying its exact decimal digits, so
the ts wire matches the py tier's `json.dumps` (a bare number, e.g.
`{"n":7}`, never `{"n":"7"}`, never a throw). The digits are emitted directly
from `bigint.toString()`, **not** via a `Number()` cast: a cast would silently
round any value past `Number.MAX_SAFE_INTEGER` (2^53), and revl `Int` is i64, so
values up to `9223372036854775807` are in range and must round-trip exactly.
Mechanically, the replacer parks each bigint behind a quoted sentinel
(`"@@revlBigInt:<n>@@"`) and the enclosing body then strips those quotes, which
is how the digits land unquoted; with no bigint in the value the body returns
the builtin result unchanged. Regression: `backends/typescript/tests/
fr3_json_int.test.ts` (runtime, under vitest) cross-checked against the py tier
in `tests/test_json_stdlib.py::test_ts_int_serialization_matches_py_tier`,
covering a small, a negative, and a beyond-2^53 `Int`.

That residual whitespace drift was itself a cross-tier bug, now fixed — see
the canonical-form contract below.

## Canonical form: byte-identical across tiers (item 385)

`json_stringify` is a `pure` fn, so revl's cross-tier determinism guarantee
(docs/syntax-2.0.md §3.4: "no backend can diverge") requires it to return the
**same bytes on every tier** for the same value. It did not: the py tier's
default `json.dumps(v)` inserts insignificant whitespace (`{"k": "v", "n": 1}`)
where ts/go emit the compact `{"k":"v","n":1}`. Each tier was self-consistent,
so both tiers' own tests passed; the drift only bit **cross-tier** — hashing a
record, byte-comparing a ledger, a cassette asserting an identical transcript,
a signature over emitted JSON (the harness does all four). A `pure` function
that returns different bytes per tier is exactly the guarantee violation.

The stated **canonical form**, now enforced by
`tests/test_json_cross_tier_bytes.py` (the same corpus stringified on py **and**
ts **and** go, asserted against one shared expected string — not per-tier
self-consistency):

- **Compact** — no space after `:` or `,`. ts `JSON.stringify` default; go; py
  `json.dumps(v, separators=(",", ":"))`.
- **Non-ASCII stays raw UTF-8** — py `ensure_ascii=False`; ts; go. `<`, `>`,
  `&` also stay raw: go's default `json.Marshal` HTML-escapes them, so the @go
  body encodes through a `json.NewEncoder` with `SetEscapeHTML(false)`.
- **Key order** is record-declaration / insertion order (py dict, ts object).
- **Booleans/null** spelled `true`/`false`/`null`; **ints** exact, incl. beyond
  2^53 (py int, ts bigint replacer, go int64 — see the two Int/bigint notes).
- **Floats**: values that share a decimal form across Python and JS (`1.5`,
  `-0.25`) are byte-identical. A **whole-number float** (`1.0` vs `1`), `-0.0`,
  or an extreme-exponent float can still format differently between the two
  runtimes — canonicalize integral quantities as `Int`, not `Float`.

**go record caveat** (a separate, deeper defect, not fixable in json.rvl's @go
body): a revl record lowers to a Go struct whose fields are **unexported**
(lowercase) with no `json:` tags, so `encoding/json` cannot see them and a
record stringifies to `{}` on go. go is canonical for scalars, strings and
lists; the record `{}` behavior is pinned by
`test_go_record_is_the_known_separate_defect` until the go emitter is taught to
export + tag record fields (its own roadmap item).

## Large integers on the ts tier: bigint on *parse* too (item 311)

Item 281 fixed the *stringify* direction; the *parse* direction had the mirror
gap, found by that same work. The builtin `JSON.parse` decodes every JSON number
to a JS `number` (f64), so an integer beyond 2^53 recovered *through* parse came
back rounded on the ts tier (`9007199254740993` → `...992`) while the py tier's
`json.loads` kept it exact — a silent cross-tier value divergence on the parse
side. A `JSON.parse` reviver cannot fix it: the reviver is handed the
already-rounded `number`, so the digits are already gone.

The `@ts` `json_parse` body is therefore a **number-preserving recursive-descent
parse** rather than the builtin. It decodes a JSON **integer literal** (no `.`,
no `e`/`E`) to a JS `bigint` — revl `Int`, full i64 precision — and a JSON
**float** (a literal carrying `.`, `e`, or `E`) to a JS `number` — revl `Float`
— matching the py tier's `int`/`float` split. Strings (including `\uXXXX`
escapes and surrogate pairs), arrays, objects, `true`/`false`/`null` decode as
JSON defines. A parsed `bigint` round-trips straight back to a bare JSON number
through the item-281 stringify replacer, so `json_stringify(json_parse(s))`
agrees with the py tier in both directions. Regression:
`backends/typescript/tests/fr3_json_int.test.ts` (runtime, under vitest) rounds
a large int, i64 max/min, a negative, a float, and a nested mix through
`stringify∘parse`, cross-checked against the py tier in
`tests/test_json_stdlib.py::test_ts_parse_roundtrip_matches_py_tier`.

Reading a parsed value into a typed binding for field access remains the erased
`Any` boundary (an integer JSON literal landing in a `Float`-typed field is a
`bigint` where a `number` is expected — the same dynamic-typing seam the py tier
has), unchanged by this fix; the contract item 311 pins is that a JSON integer
survives `parse` at full i64 precision, matching py.

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
positional strings rather than a structured document. Item 80 has since landed, so
the ts tier's residual blocker for a *real* provider is gone: an extern body
declared `async` emits an `async function` and may `await`
(`backends/typescript/golden/async_http.ts`).

## Why externs, not builtins

FR-6/FR-9 (`startsWith`/`endsWith`/`to_int`) are *builtins*: the lowering
per tier is a one-liner and the surface rule ("a method call must name one
of the builtins") admits them. A JSON *parser* is not a one-liner on any
tier, and the harness's provider integration is exactly the place where the
implementation should be the host's own JSON library — that is what the
extern body mechanism is for. The two mechanisms meet the same admission
rule: a call must name something declared.
