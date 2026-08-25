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
block-arm := '{' fn-stmt* pure-expr '}'
```

The block's value is its final expression. The block accepts the same
statement set a normal fn/block body accepts — `let`, `var`, `while`, `if`,
`for`, assignments — so imperative logic can live where the value is
destructured (roadmap 202). `return` is not a block-arm statement (the arm
yields its final expression, not an early return from the enclosing fn), and
the block is a pure value position, so effects are still refused. A `{` after
`=>` is a record only when it is empty, opens `ident :` (a record literal), or
opens `base | …` (a record update); anything else is a block arm.

```revl fragment
match ft {
    Some(xs) => {
        var ps: List[Str] = []
        var i = 0
        while (i < xs.length) { ps = ps.push(xs[i])  i = i + 1 }
        Some(ps)
    },
    None => None
}
```

## 5. IR

Both forms are **additive** — `ir_version` stays 3 and the v1/v2/v3 reference
documents are byte-identical:

- `"kind": "record_update"`, keys `base`, `updates` (list of `[name, expr]`).
- block arms are lowered by lambda-lifting the block into a synthetic helper
  `fn` (`match_arm_<n>`): the arm's statements become the helper's body, the
  final expression its `return`, and every enclosing name the block reads a
  parameter. The arm's IR is a *call* to that helper, so the arm body stays an
  expression and no backend needs new emit support. `ir_version` stays 3 (a
  helper fn and a call are already v3 shapes). Lowering happens inside a module
  `fn` body; a block arm in another position (a `test`/component/prop-test
  body, an extern undo expression) still refuses loudly.

## 6. Tier status

| form | python | typescript | rust | java | go | wasm |
|---|---|---|---|---|---|---|
| record update | ✅ spread | ✅ spread | refused | refused | refused | refused |
| block arms | ✅ lift | ✅ lift | ✅ lift | ✅ lift | ✅ lift | ✅ lift |

Block arms lower to a helper fn + call (see §5), so every tier that emits a
`fn` call emits them — the lift is tier-agnostic and needs no per-backend work.

Record-update refusals name the tier and point here; nothing half-emits.
Rust/Java need a
per-record copy constructor walk; Go needs typed struct copies; Wasm needs a
linear-memory clone routine — each is mechanical but unproven, and an
unverified emitter is worse than a loud refusal on a tier whose contract is
byte-exact output.
