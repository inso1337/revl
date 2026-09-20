# Design: `Opt` nesting at a JSON boundary (issue #1263)

Design note for issue #1263, filed by the lane that closed issue #1187 (PR
#1262). It records one language-surface decision and the audit that follows
from it. The question is not how to render a schema; it is whether the two
spellings being rendered mean two things.

## The erasure

`json_schema_for` rendered `Opt[T]` as `{**inner, "nullable": true}`. That
folds the OUTER layer into a flag on the INNER schema, and a flag does not
stack. When `T` already accepted `null`, the outer layer disappeared:

```
Opt[Str]       ->  {"type": "string", "nullable": true}
Opt[Opt[Str]]  ->  {"type": "string", "nullable": true}    # the same dict
Unit           ->  {"type": "null"}
Opt[Unit]      ->  {"type": "null", "nullable": true}      # the same documents
```

Item 257's validating boundary derives its schema from the declared type and
checks the response against it, so two types that derive one schema are two
types the validator cannot tell apart. Nothing on the 257 surface reported it,
because a JSON schema carries one nullability flag and the erasure is invisible
from inside the result.

It surfaced from outside. PR #1262 derives a decoding GRAMMAR from the same
declared types. A grammar has no single flag: `Opt[Opt[Str]]` derives the
string `null` from two different productions, and the ambiguity is visible if
you walk the surface type rather than the schema. That PR walks the surface
type for exactly that reason and refuses a null-ambiguous `Opt` for its own
feature. It left `json_schema_for` alone, and filed this.

## The decision

**`Opt[Opt[T]]` is a distinct type in revl's type system, and it is not
expressible in a JSON document.** Those are two different claims and the tree
already answers both.

### It is distinct in the type system

- The checker refuses to substitute one for the other. `fn f() -> Opt[Int] {
  return Some(Some(5)) }` is `this function's return expects` `Opt[Int]`, `got`
  `Opt[Opt[Int]]`. They are not two spellings of one type.
- The language BUILDS the type without anyone asking for it. `?.` on an `Opt`
  receiver with an `Opt`-returning builtin produces it:
  `fn parsed(s: Opt[Str]) -> Opt[Opt[Int]] { return s?.to_int() }`
  (`tests/fixtures/emit_ts_corpus/optionals.rvl`,
  `tests/fixtures/emit_py_corpus/branches.rvl`). It is not an exotic spelling a
  user has to reach for deliberately.
- The rust tier emits `Option<Option<T>>` and roadmap item 492's boxing pass
  depends on the two layers being two inline layers
  (`_inline_cycle_target`, `backends/rust/emit.py`). Collapsing the type would
  make that computation wrong.
- `tests/test_selfhost_types.py` requires the self-host type parser to
  round-trip `Opt[Opt[Int]]` as its own type.

So the alternative resolution, "an optional of an optional collapses, and the
type system should say so at declaration", is not available. It would
invalidate `?.` chaining, the rust boxing computation and the self-host type
grammar, to buy a schema rendering.

### It is not expressible on the wire

- `revl import wit` already refuses `option<option<T>>`, on the recorded
  grounds that "`Opt[Opt[T]]` cannot distinguish the two absences"
  (`src/revl/import_wit.py`, `docs/import-wit.md`).
- `revl import openapi` already collapses absent-and-nullable into one `Opt[T]`
  and says so in the generated header, because "absent" and "null" are one
  value on this side of the boundary (`docs/import-openapi.md`).
- At run time the collapse is real on every tier whose `None` is a host
  absence. The python tier emits `return None` for both `Some(None)` and `None`
  at type `Opt[Opt[Int]]`; `tests/test_458_conversion_dispatch.py` says so in
  as many words and collapses the two levels with `??` rather than matching
  them.

A JSON document carries one `null`. No rendering fixes that. The `oneOf` form
the 257 design note left open (`{"oneOf": [T, {"type": "null"}]}`) does not:
`{"oneOf": [{"oneOf": [S, null]}, null]}` is a structurally distinct document
schema, but `null` matches two arms, which the derived-schema validators
already reject as `value is ambiguous, matching 2 union arms`. Distinct is not
the same as decidable, and a validator needs decidable.

### What follows

The schema is not taught a nesting it cannot carry. The BOUNDARY refuses the
spelling, which is the call `revl import wit` and `revl import openapi` already
made at their own boundaries, now made at the one boundary that had not made
it.

Concretely, in `src/revl/mcp/schema.py`:

- `admits_json_null(t)` is a positive predicate over the SURFACE type. Exactly
  two shapes derive a bare `null` document: `Unit`, and any `Opt[_]`. A variant
  does not (every case is a tagged object at a validated boundary, so a nullary
  case is `{"tag": "Nil"}`), a record is an object, a `List`/`Map` is an
  array/object.
- `fully_expressible` refuses an `Opt[T]` whose `T` admits `null`. It sits
  beside the refusals already there for the same reason: an untagged
  `Result[T, E]`, a `Map[K, V]` with a non-`Str` key, a cycle, an unknown
  nominal. Each is a type whose derivation is not EXACT, and an inexact schema
  validates nothing while reading as safe.
- `expressibility_reason` names the position and the reason, and points at the
  remedy: a named variant that gives each absence a tag.
- `json_schema_for` degrades the same shape to the existing `x-revlType` stub
  instead of rendering the inner type's schema. This is for the UNGATED
  consumer below; a validated boundary never reaches it, having been refused.

No new guarantee code. The refusals all carry `G4`/`validated`, which is where
the three gated consumers already report.

## The audit: every consumer of `json_schema_for`

Five call sites and two runtime readers. The erasure is upstream of all of
them, so none would have shown a symptom of its own.

| Consumer | Gate | Affected | Resolution |
|---|---|---|---|
| `lower.py`, `validated` emission response schema (item 257) | `fully_expressible` | yes | refused at compile time, position named |
| `lower.py`, `event` declaration item schema (item 130 §6) | `fully_expressible` | yes | refused at compile time |
| `lower.py`, routed endpoint bound-parameter schema (item 457) | `fully_expressible` | yes | refused at compile time |
| `mcp/schema.py`, `tools_from_ir` input/output schema | none | yes | renderer degrades to `x-revlType` |
| `mcp/http_face.py`, `_schema_error` | reads the derived schema | downstream | fixed upstream; the derived schema no longer claims two types are one |
| `backends/python/runtime.py`, `_json_schema_error` | reads the IR's schema | downstream | fixed upstream |
| the go, rust and java tiers' copies of the same validator | read the IR's schema | downstream | fixed upstream, no tier change |
| PR #1262's `decode_grammar.py` | walks the surface type | already guarded | its own refusal stays; this one is now upstream of it |

The MCP projection is the one that needed the renderer rather than the gate.
`tools_from_ir` has no `fully_expressible` in front of it by design (the MCP
surface is a projection, not a validated boundary, and degrading honestly is
its documented posture), so the renderer itself has to stop claiming a nested
`Opt` is its inner type.

Two things that look like consumers and are not:

- `src/revl/export_openapi.py` is a SECOND type-to-schema mapping, not a caller
  of this one. It renders `Opt[T]` as `{"oneOf": [T, {"type": "null"}]}`, so it
  does not erase the nesting, it produces an ambiguous `oneOf` instead. It is
  reached only from a routed operation, which the `lower.py` route gate now
  refuses first, so it never receives one. It is worth saying out loud that the
  mapping exists, because the 257 design note's "no second type-to-schema
  mapping" rule reads as though it does not.
- `src/revl/import_openapi.py` reads `nullable` on an INCOMING schema. It is
  the inverse direction and already collapses deliberately.

## Not a bypass

Nothing was admitted that a guarantee should have refused. The three gated
consumers each validated a response against a schema, and the schema was a
real constraint; it was a constraint belonging to a different declared type.
That is a representation gap, and this note is filed with that framing on
purpose. The change makes the gap a refusal rather than a silent substitution.

## Blast radius

`Opt[Opt[T]]` and `Opt[Unit]` were previously admitted at a validated
emission, an `event` declaration and a routed endpoint. Both spellings are now
refused there. Neither appears in `examples/`, in any backend fixture corpus,
or in any test that reaches one of those three surfaces: the full slice of the
suite matching `schema or model or boundary or type or opt or validator` is
green unchanged, and the reference census is unmoved. Nothing else about `Opt`
moves: a single-layer `Opt[T]` over a non-null-admitting `T` renders and
validates exactly as before, and the type system is untouched, so `?.`
chaining, the rust boxing pass and the self-host type grammar all keep the
nested type they depend on.

## What this does not do

It does not settle the 257 design note's open question about `nullable` versus
a strict `{"oneOf": [T, {"type": "null"}]}` rendering for a single-layer `Opt`.
That question is about strictness against a chosen validator and is
independent of this one: as shown above, the `oneOf` form does not distinguish
the nested case either, so neither answer to it changes this decision. It is
left open where it was.
