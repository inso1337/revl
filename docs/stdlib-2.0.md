# The 2.0 stdlib surface (finding 6)

**The rule:** a method call on a value must name one of the builtins below.
Anything else is a compile error — never a verbatim pass-through to whatever
the host object happens to have. (Before this surface existed, `xs.push(v)`
type-checked, ran on TS, and crashed on Python: the "stdlib" was accidentally
the host's object model. That is the portability sin every other layer of
revl refuses.)

## The surface

| method | arity | on | semantics | py lowering | ts lowering |
|---|---|---|---|---|---|
| `length()` | 0 | Str, List | element count | `len(x)` | `x.length` |
| `push(v)` | 1 | List | **persistent** append — returns a new list | `x + [v]` | `[...x, v]` |
| `slice(a, b)` | 2 | Str, List | half-open sub-range | `x[a:b]` | `x.slice(a, b)` |
| `charAt(i)` | 1 | Str | 1-char string | `x[i]` | `x.charAt(i)` |
| `charCodeAt(i)` | 1 | Str | code point at i | `ord(x[i])` | `x.charCodeAt(i)` |
| `indexOf(v)` | 1 | Str, List | first index, `-1` if absent | inline dispatch helper | `x.indexOf(v)` |
| `concat(y)` | 1 | Str, List | joined copy | `x + y` | `x.concat(y)` |
| `split(sep)` | 1 | Str | pieces between separators; `""` → 1-char strings; trailing empties kept | inline dispatch | `x.split(sep)` |
| `join(sep)` | 1 | List[Str] | elements joined by sep | `sep.join(x)` | `x.join(sep)` |
| `repeat(n)` | 1 | Str | n copies concatenated | `x * n` | `x.repeat(n)` |
| `div_trunc(b)` | 1 | Int | integer division rounding toward zero | built | `Math.trunc(a / b)` |
| `div_floor(b)` | 1 | Int | integer division rounding toward −∞ | `a // b` | `Math.floor(a / b)` |
| `div_euclid(b)` | 1 | Int | division whose remainder is ≥ 0 | built | built |
| `mod(b)` | 1 | Int | Euclidean remainder, always in [0, \|b\|) | `a % abs(b)` | built |

- `push`/`concat` are **persistent** (value semantics) — consistent with
  capture-by-value and G6: no revl value is ever mutated in place. Rebind:
  `out = out.push(v)`.
- `.length` also works in property position (the existing `len` node).
- List element read is indexing (`xs[i]`), not a method.
- `charAt`/`charCodeAt` are Str-only by spec; the v0 checker does not yet
  type-dispatch (misuse is a host runtime error) — full typing tightens this.
- Type dispatch for `indexOf` is a Python-side inline helper because
  `str.find`/`list.index` disagree about absence; both backends return `-1`.
- `split` is pinned to the JS shape on every backend: `"a,,b".split(",")`
  has 3 elements, `"a,".split(",")` keeps the trailing empty (Java's
  regex-split drops it — its lowering passes `Pattern.quote(sep), -1`), and
  `"abc".split("")` is the 1-char strings (Python's `str.split("")` raises
  — its lowering dispatches on empty sep).
- `join` is declared on `List[Str]` (TS orientation: `xs.join(sep)`); the
  Python lowering swaps receiver and argument (`sep.join(xs)`).

## Host objects are exempt, by provenance

v1 host stub objects (`Pool.open(...)`, `Map.new()`) carry their own methods
(`query`, `insert`, `drop`, ...). The checker tracks host provenance — a
`let` bound to a host-constructor call, or a direct constructor-call
receiver — and exempts those receivers from the table. The two method
namespaces are collision-free by construction (checked when extending
either: the table vs `open/close/query/execute/new/get/insert/remove/drop`).

## Versioning

A `builtin` IR node anywhere in a component implies `ir_version: 3`; pure
v1 documents are unaffected. The wasm tier lowers the fixed-shape builtins
(`length`, `push`, `concat`, `slice`, `charAt`, `charCodeAt`) over its
canonical-ABI string/list model and rejects the rest (`indexOf`, `split`,
`join`, `repeat`) with its usual named tier error — not yet lowerable on
that tier.

## Planned (needed for self-hosting, not yet specified)

`Map` as a *value* type (persistent set/get/has) — symbol tables for the
self-hosted checker. Extend the table here first, per the house rule:
spec, then checker, then all emitters, then tests. (`split`/`join`/
`repeat` graduated to the table above for the self-hosted lexer.)


Integer division and modulo are specified in **docs/arithmetic.md**, including
why `/` is true division and `%` keeps TypeScript's truncated remainder.
