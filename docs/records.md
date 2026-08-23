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

- `r` must carry a **named record type**; anything else is refused
  ("record update requires a record type").
- every named field must exist in that record's declaration.
- each replacement expression must match the declared field type.
- the result's type **is** `r`'s type — an update flows anywhere the base
  record flows without re-annotation.

```revl
type Point = { x: Int, y: Int }

fn moved(p: Point) -> Point {
    return { p | x = p.x + 1 }
}
```

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
