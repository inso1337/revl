# Records: functional update & block-bodied match arms

**Status:** functional record update implemented (python + typescript
emitters); block-bodied match arms specified, parsed and typechecked with all
emitters deferred. See §6. Proposed 2026-06; both gaps were first-class field
data from the selfhost porting agents (dogfood refusal logs).

## 1. Grammar

```
expr        := ... | record-update
record-literal := '{' ident ':' expr (',' ident ':' expr)* '}'
record-update  := '{' pure-expr '|' ident '=' expr (',' ident '=' expr)* '}'
```

The two `{`-forms are distinguished by a single top-level token: a bare `|`
before the matching `}` means *update*; a record literal can never contain
one (field values are bracket-balanced and `|` is not a revl binary
operator). The OCaml/Rust struct-update spelling is deliberate — it is the
form models write on autopilot.

## 2. Semantics

`{r | f = e}` evaluates `r`, then `e`, then yields a **fresh value**: a copy
of `r` in which field `f` is `e`. `r` itself is untouched — exactly the
discipline of `Map.set`. Multiple updates apply to the same base (`{r | x =
1, y = 2}` never observes `{r | x = 1}`).

## 3. Type rules

- `r` must carry a record type — **named**, or the **structural** type of an
  anonymous literal (§3.1); anything else is refused ("record update requires a
  record type").
- every named field must exist in that record's declaration (or the structural
  literal's shape).
- each replacement expression must match the declared field type.
- the result's type **is** `r`'s type — an update flows anywhere the base
  record flows without re-annotation.

```revl
type Point = { x: Int, y: Int }

fn moved(p: Point) -> Point {
    return { p | x = p.x + 1 }
}
```

### 3.1. Structural vs nominal at declared boundaries (item 71)

An **anonymous** record literal (`let a = { h: "x" }`) has no nominal name to
look up. It infers a **structural record type**, spelled `{field: Type, ...}`
in canonical (sorted) order. That shape is what makes an update on an anonymous
receiver checkable: `{ a | h = 5 }` on the `{ h: Str }` above is refused
(`update of field \`h\` expects \`Str\`, got \`Int\``), and `{ a | missing = … }`
is refused for naming a field the shape does not have. Reads through the binding
(`a.h`) are checked the same way.

The structural type lives **only in the checker**. It is not a second kind of
record you can declare — there is no surface syntax for it — and it never
reaches the IR: the `record` / `record_update` nodes carry no type, an inferred
`let` type is not emitted, and the emitted type table stays nominal. This is the
same discipline as the `?T` widening marker (item 11): a checker-level
annotation that leaves the emitted structure byte-identical.

At any **declared boundary** — an annotated `let`, a `return`, an argument — a
structural type **unifies field-wise** with the nominal record it meets: the
field sets must match and each field type must be compatible. The `List[Never]`
bottom rule applies recursively (an empty-list field flows into any `List[T]`),
because `Never` is a wildcard in the elementwise compatibility check. So
`let a = { h: "x" }` still flows into a `C = { h: Str }` position exactly as a
named `C` value would, while a shape that disagrees is refused at the boundary.

## 4. Block-bodied match arms

An arm body may be a statement block:

```
match-arm := pattern '=>' (pure-expr | block-arm)
block-arm := '{' let-stmt* pure-expr '}'
```

The block's value is its final expression. v1 subset, deliberately narrow:
only single-assignment `let` statements may precede the tail — no `var`, no
`if`/`while`/`return`, no effects. A block containing a `let` is parsed as a
block arm; a `{` whose top level starts `ident :` stays a record literal.

```revl fragment
match shape {
    _ => { let doubled = area * 2  doubled + 1 }
}
```

## 5. IR

Both forms are **additive** — `ir_version` stays 3 and the v1/v2/v3 reference
documents are byte-identical:

- `"kind": "record_update"`, keys `base`, `updates` (list of `[name, expr]`).
- block arms exist only in the parser AST; lowering refuses until §6 lands a
  representation (the leading candidate is lambda-lifting the block into a
  synthetic helper fn, which keeps the IR arm-body-as-expression invariant).

## 6. Tier status

| form | python | typescript | rust | java | go | wasm |
|---|---|---|---|---|---|---|
| record update | ✅ spread | ✅ spread | refused | refused | refused | refused |
| block arms | refused | refused | refused | refused | refused | refused |

Refusals name the tier and point here; nothing half-emits. Rust/Java need a
per-record copy constructor walk; Go needs typed struct copies; Wasm needs a
linear-memory clone routine — each is mechanical but unproven, and an
unverified emitter is worse than a loud refusal on a tier whose contract is
byte-exact output.
