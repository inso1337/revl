# Design: there are two type-to-schema mappings (issue #1272)

Filed by the lane closing issue #1263 (PR #1270) while enumerating every
consumer of `json_schema_for`. It is a correction to a claim in a design
document, plus the test that keeps the corrected claim true.

## The claim that was wrong

`docs/design/257-typed-model-boundary.md` said, in three places, that revl has
ONE type-to-schema mapping and that the design forbids inventing a second:
section 3.1 ("There is no second mapping and this design forbids inventing
one"), the non-goal in section 11 ("No second type-to-schema mapping: the
boundary runs `json_schema_for` or it is a bug"), and the open question at the
end of the same section ("change it in the one mapping ... never forking a
second rendering").

The reasoning was sound and is unchanged: two implementations of one semantics
drift unless something holds them together. The factual half was wrong. There
are two, and the second predates the claim.

## The tree

| | fragment mapping | document mapping |
|---|---|---|
| where | `src/revl/mcp/schema.py`, `json_schema_for` | `src/revl/export_openapi.py`, `_OpenApiExporter._schema_ref` / `_component_schema` |
| produces | one self-contained JSON Schema fragment | a `$ref` into an OpenAPI 3.1 document's `components/schemas` |
| consumers | the MCP projection (`tools_from_ir`), and the three gated boundaries in `lower.py` (a `validated` emission response, an `event` item, a routed bound parameter) | `revl export openapi` |
| read back by | `mcp/http_face.py`, `backends/*/runtime.py` and the tier validators, all of which read the DERIVED schema out of the IR | `revl import openapi`, which reads an incoming OpenAPI document |
| on a type it cannot render | degrades to the honest `{"x-revlType": N}` stub | raises `RevlError` |

`src/revl/import_openapi.py` reads `nullable` on an INCOMING schema. It is the
inverse direction, not a third mapping.

## The decision: keep two, and hold them together

The alternative, making `export_openapi.py` a caller of `json_schema_for` so
the original claim becomes true, was rejected on four pieces of evidence. They
are not stylistic. Each is a place where the two mappings must say different
things because their readers are different.

1. **A document names its schemas; a fragment cannot.** The OpenAPI mapping
   emits `{"$ref": "#/components/schemas/Note"}` and closes the referenced set
   transitively into one `components` block, so a type used by four operations
   is written once and a generated client gets four references to one model.
   `json_schema_for` has no `$defs` and expands structurally at every use,
   because its consumer is a single validator call against a single value.

2. **Absence and null are one thing over HTTP and two things at a validating
   boundary.** The OpenAPI mapping drops a bare-`Opt` record field from
   `required`, so the field may be absent. `json_schema_for` requires every
   field and marks it `nullable`, so the field must be present and may be
   `null`. This is not an oversight on either side: `revl import openapi`
   collapses absent-and-nullable into one `Opt[T]` on the way in and says so in
   the generated header, and the export mapping is that importer's inverse. The
   validating boundary has no absence to collapse; it checks one value that
   either arrived or did not.

3. **A document is read by a third party, so it refuses rather than
   degrades.** `json_schema_for("Nope")` is `{"x-revlType": "Nope"}`, an
   unconstrained schema that is honest about knowing nothing, which is the
   right posture for a projection. The same value inside a published OpenAPI
   document would read to an external consumer as a contract and constrain
   nothing. `_schema_ref` raises instead, and the same holds for `Bytes` and
   for an untagged `Result[T, E]`.

4. **Closedness.** The OpenAPI mapping writes `additionalProperties: false` on
   every record and every variant arm. The fragment mapping closes variant arms
   (the `const` tag needs the arms mutually exclusive) but leaves records open.

Points 1 to 3 are reasons the two renderings differ. Point 4 and the tag-key
spelling below are not reasons; they are differences with no argument behind
them, which is what an undifferentiated second mapping produces. They are
recorded as divergences rather than repaired here, because changing either one
changes a shipped document shape and belongs with the round-trip work noted at
the end.

## What holds them together

`tests/test_schema_mapping_differential_1272.py`. It renders one corpus of
types through both mappings and walks the two results in lockstep, driven by
the SURFACE type rather than by either schema, so a constructor one mapping
stopped rendering is a failure and not a silently skipped subtree. Every
position is classified as an agreement or as one of a closed, named set of
divergences.

The assertion is two-sided:

- a divergence the set does not name fails, which is one mapping drifting;
- a named divergence the corpus no longer observes fails, which is the two
  mappings converging while this note still says they do not.

Both failures land in the same place: this table and the tree get re-checked
against each other in the commit that moves either.

### The divergence table

Taken at `validated=True`, which is the mode whose variant rendering is uniform
and therefore comparable with the document mapping's only rendering.

| class | fragment | document | reason |
|---|---|---|---|
| `AGREE` | `{"type": "string"}` | identical | the scalar core, `List`, `Map[Str, V]` |
| `REF_VS_INLINE` | the record expanded in place | `{"$ref": "#/components/schemas/Note"}` | reason 1 |
| `RECORD_OPT_FIELD_NOT_REQUIRED` | every field in `required` | a bare-`Opt` field omitted | reason 2 |
| `RECORD_OPEN_VS_CLOSED` | no `additionalProperties` | `additionalProperties: false` | no argument, recorded |
| `OPT_NULLABLE_VS_ONEOF` | `{**inner, "nullable": true}` | `{"oneOf": [inner, {"type": "null"}]}` | 257's own open question, unsettled |
| `VARIANT_TAG_KEYS` | `tag` / `value` | `$kind` / `$value` | no argument, recorded |
| `UNIT_NULL_VS_EMPTY` | `{"type": "null"}` | `{}` | `Unit` is a 204 in a document, a JSON `null` at a boundary |
| `MAP_KEY_DROPPED` | `K` dropped | `K` dropped identically | an agreement, and a shared inexactness `fully_expressible` refuses upstream |

The refusal asymmetry (reason 3) is not a divergence class. The document
mapping raises where the fragment mapping renders, so there is no pair to
compare; it has its own test.

## The reachability correction

PR #1270's consumer audit said the document mapping "is reached only from a
routed operation, which the `lower.py` route gate now refuses first, so it
never receives one". That is true of the operation's BOUND INPUTS and not of
its RESPONSE.

The route lowering runs `fully_expressible` over the `bind` table (path, query
and body entries). `_route_response_ir` classifies the return type into
`plain` / `result` / `response` and never gates it. So a routed operation
returning `Opt[Opt[Str]]` compiles, and `revl export openapi` renders its
response schema as

```json
{"oneOf": [{"oneOf": [{"type": "string"}, {"type": "null"}]},
           {"type": "null"}]}
```

in which `null` matches two arms. Measured on both trees: refused on a bound
input only where PR #1270 is present, admitted through the response on both.
`tests/test_schema_mapping_differential_1272.py` pins both halves, the response
half with no skip because it does not depend on #1270.

This is a document-quality gap rather than a soundness one. The response is
produced by a handler the compiler has already typed, so nothing validates it
against this schema at run time; what is wrong is the published document. It is
recorded here because it is precisely the shape the claim of singularity hid: a
second mapping whose only guard is a gate that turns out to cover half of its
inputs.

## Also observed, and left open

An exported `Opt[T]` does not re-import. `revl import openapi` requires a
`discriminator` on every `oneOf`, and the export mapping emits its `Opt` and
its variant unions without one, so `revl export openapi` followed by `revl
import openapi` fails on any service with an `Opt` record field or an ADT. The
round-trip test in `tests/test_export_openapi_457.py` covers a subset with
neither. Rendering `Opt[T]` over an inline `T` as OpenAPI 3.1's
`{"type": ["string", "null"]}` would fix the ambiguity of the section above and
the round trip at once, which is why it is worth doing as one piece of work
rather than in passing here.

## What this does not do

It does not change either mapping, and no emitted document or derived schema
moves. It does not settle 257's open question about `nullable` versus a strict
`oneOf` for a single-layer `Opt`; the differential pins the current answer on
both sides so that settling it has to settle it in both.
