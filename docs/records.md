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
because `Never` is the bottom of the elementwise compatibility check: it flows
out of a `Never` position into any other, and nothing flows in. So
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

## 7. What an emitted record IS (python tier)

The representation of a record value is part of the tier's contract, not an
implementation detail, because things outside the emitted module produce and
consume record values: a host handing an argument across a service boundary,
the `prop test` generators and shrinkers in `src/revl/fault.py`, the item-60
auto-mocks in `src/revl/mocks.py`, and every test that execs an emitted module.

**A record value is a plain `dict`, keyed by the revl field name, spelled
exactly as the source spells it.** `{ id: 4, from: "a" }` emits
`{'id': 4, 'from': 'a'}`. There is one representation and it holds everywhere —
inside the module and out.

The emitted `class Row:` is a **shape declaration**: field names and types as
annotations, no constructor, nothing to instantiate. It is emitted because it
makes the module readable next to the revl source. It is not the value carrier
and cannot be made into one by accident, because a field's **class attribute**
is renamed for Python keyword collisions (item 165) while its **runtime key**
is the raw revl name:

```
type Q = { from: Str, class: Int }
```

```python
class Q:                 # shape: `from` renamed, because `class Q: from: str` is a SyntaxError
    from_: str
    class_: int

def mk():
    return {'from': 'a', 'class': 2}          # value: the raw revl names

def readit(q):
    return (_fv['class'] if isinstance((_fv := q), dict) else getattr(_fv, 'class'))
```

An instance of `Q` answers neither field read. The `getattr` arm of that
dispatch is for **ADT payloads**, which are real objects (`Ok(v)` has a `.value`
attribute), never for records; it also lets a record a host hands back as an
object still read on the common case.

The class carried `@dataclass` until roadmap item 436 F9, which is why building
one appeared to work. It appeared to work in tests only: `_make_record` and
three `tests/test_v2_emit.py` cases constructed the class, so both `prop test`
and the auto-mocks explored a value shape no emitted program can produce. That
is how a live defect stayed green — `let {id, name} = row` emitted `tmp.id` and
raised `AttributeError` on every record value the emitter itself produces. It
now emits the same dispatch a `row.id` read does.

Corollary for anything that builds a record value from outside: build the dict.
Do not call the class.
