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
| `checked_div_trunc(b)` | 1 | Int | total `div_trunc`: `Result[Int, Str]` — `Err(reason)` at a zero divisor instead of a fault (docs/arithmetic.md) | built | built |
| `checked_div_floor(b)` | 1 | Int | total `div_floor`, same Result shape | native `//` under `Ok` | built |
| `checked_div_euclid(b)` | 1 | Int | total `div_euclid`, same Result shape | built | built |
| `checked_mod(b)` | 1 | Int | total `mod`, same Result shape | `a % abs(b)` under `Ok` | built |
| `set(k, v)` | 2 | Map | **persistent** put — new map, receiver unchanged (§Map below) | `{**x, k: v}` | IIFE copy + `set` |
| `lookup(k)` | 1 | Map | value under `k` as `Opt[V]` | `x.get(k)` | `x.get(k)` |
| `has(k)` | 1 | Map | key membership | `k in x` | `x.has(k)` |
| `to_str()` | 0 | Int | decimal rendering, `-` for negatives | `str(x)` | `x.toString()` |

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

## Host objects: constructor-tracked receivers are checked

v1 host stub objects (`Pool.open(...)`, `Map.new()`) carry their own methods
(`query`, `insert`, `drop`, ...). The checker tracks host provenance — a
`let` bound to a host-constructor call infers the *family* — and checks a
method call on such a receiver against that family's surface (the dotted
names in `_HOST_ARG_SIG`): an unknown method is refused (`HOST-METHOD`, with
the real surface named), and arity/argument types are checked exactly like
the call form. The stub's *result* stays opaque — no entry claims to know
what comes back — so values flowing out of host objects remain on the audit
surface. Receivers with no pinned family (an extern's return, a host-object
result) type unknown, and **every** method call on them is refused —
stdlib-named or not (roadmap 75(b)): a value cannot lower *through* the
builtin table into a misdispatch, and an annotation (`let v: Str = ...`) is
how such a result becomes a provable receiver again. The two method
namespaces are collision-free by construction, checked at *table-edit* time:
a module-load assertion fails with the colliding name if either table is
extended with a name from the other (`remove` is the one documented overlap,
safe because dispatch is by receiver kind — the table vs
`open/close/query/execute/new/get/insert/remove/drop/run`).

## Versioning

A `builtin` IR node anywhere in a component implies `ir_version: 3`; pure
v1 documents are unaffected. The wasm tier lowers the fixed-shape builtins
(`length`, `push`, `concat`, `slice`, `charAt`, `charCodeAt`) over its
canonical-ABI string/list model and rejects the rest (`indexOf`, `split`,
`join`, `repeat`) with its usual named tier error — not yet lowerable on
that tier.

## `Map`: the persistent value type (graduated from Planned)

`Map[Str, V]` is now a *value* type — the symbol table the self-hosted
checker needs. It is specified here before any code, per the house rule:
spec, then checker, then all emitters, then tests.

### Surface

| form | arity | on | semantics |
|---|---|---|---|
| `Map.empty()` | 0 | (constructor) | the empty map, typed `Map[Str, Never]` |
| `set(k, v)` | 2 | Map | **persistent** put — returns a new map, receiver unchanged |
| `lookup(k)` | 1 | Map | `Opt[V]` — the value under `k`, or `None` |
| `has(k)` | 1 | Map | `Bool` — is `k` present? |
| `size()` | 0 | Map | `Int` — the number of key/value pairs |
| `keys()` | 0 | Map | `List[Str]` — the keys in ascending canonical `Str` order; this IS iteration |
| `remove(k)` | 1 | Map | **persistent** delete — a new map without `k`; absent `k` is a total no-op returning an equal map, never an error |

The grain follows `List`: operations are methods on the value (`m.set(k, v)`
rebinds, exactly like `out = out.push(v)`), because revl has no mutation and
no free-function namespace to pollute. The surface grows by specification,
not by accretion: build/read/member shipped first; `size`/`keys`/`remove`
graduate by the spec step below. Iteration's **order was decided before it
shipped** — see *Iteration order* below and **docs/collections.md** — because
that is the moment the
tiers would otherwise diverge for free.

### Coexistence with the host `Map.new()`

The v1 host stub object (`let store = Map.new()`, methods
`insert/remove/get/drop`) keeps its exact existing surface. The two
namespaces stay collision-free **by construction**: every new value-side
name (`empty`, `set`, `lookup`, `has`) was chosen disjoint from the host
verb set (`open/close/query/execute/new/get/insert/remove/drop/run`; `run`
is Job's). In
particular the reader is `lookup`, not `get` — `get` already means the host
stub's unchecked read, and reusing the spelling would make `x.get(k)`
uninterpretable to a human even though dispatch itself is unambiguous
(host-family receivers route to `_HOST_FAMILIES` before `_BUILTIN_SIG` ever
runs). The disjointness is pinned by a test, so extending either namespace
without keeping it fails CI. `Map.empty()` vs `Map.new()` reads exactly
like what they are: an empty persistent value vs a stateful host object.

### Semantics, pinned

- **Keys are `Str`.** Every tier hashes strings natively, and the target
  application (symbol tables) is string-keyed. `Map[Int, V]` parses (the
  type algebra admits it) but no operation accepts a non-`Str` key, so it
  cannot be populated; widening keys is a later spec change.
- **Values are one type `V`**, inferred from use: `m.set(k, v)` requires
  `v` to match the receiver's `V`.
- **Persistence:** `set` returns a fresh map; the receiver is untouched.
  `let m2 = m.set(k, v)` leaves `m` exactly as it was, on every tier.
- **Equality** is the language's one structural equality (syntax-2.0
  §3.4), specialized order-independently: two maps are equal iff they have
  the same key set and equal values under every key — `{a=1, b=2} ==
  {b=2, a=1}`. Insertion order is never observable through `==`.
- **Iteration order is sorted, not insertion.** When iteration ships,
  `keys()` (and any `for`-over-a-map) yields keys in ascending **canonical
  `Str` order** — Unicode scalar value, equivalently UTF-8 byte
  lexicographic — on every tier. Order is a pure function of the key set,
  never of construction history, which is the same line `==` already draws.
  The full decision, the three options costed per tier, and the canonical
  order live in **docs/collections.md**; the short form is the subsection
  below.
- **`Map.empty()` types as `Map[Str, Never]`.** `Never` is the bottom of
  the compatibility relation, so the empty map flows into any `Map[Str,
  V]` — the same trick the untyped empty list literal plays — and `set`
  widens it from there.
- **Bottom-typed receivers learn `V` from use.** Because `Never` is a
  wildcard, `set` on a `Map[Str, Never]` receiver would otherwise prove
  nothing about its value argument and return another `Map[Str, Never]` —
  which flows into *any* `Map[Str, X]`, planting any value under any
  declared map type. So when the receiver's `V` is bottom, `set` unifies
  `V` against its concrete value argument and returns `Map[Str, learned]`;
  the check position then sees e.g. `Map[Str, Str]` where `Map[Str, Int]`
  is expected and refuses. The same learning rule governs `push` on the
  List empty literal (`[].push("s")` is `List[Str]`, refused where
  `List[Int]` is expected) — the identical escape existed there first.
  A let bound to `Map.empty()` is an ordinary VALUE binding: its method
  calls go through the checked builtin path, never the verbatim host path.

### Iteration order (decided before iteration ships)

`Map` has no iteration today, which is exactly why the order is fixed now:
the instant `keys()` exists the tiers diverge by default — python/typescript
maps iterate in insertion order, go and rust randomize *by design*, java is
unspecified. Deciding after the fact would pin the divergence as errata.

The contract is **ascending canonical `Str` order** (Option B below). Three
options were weighed:

| option | determinism | per-tier cost |
|---|---|---|
| insertion order | deterministic | free on python/ts; go needs an ordered wrapper, rust needs `IndexMap`/std wrapper (both invasive), java swaps to `LinkedHashMap` (cheap) |
| **sorted-key order** (chosen) | deterministic | a sort at iteration on every tier, **no representation change**; ts/java need a code-point comparator for supplementary-plane keys |
| unspecified | **not** deterministic | free everywhere, but forfeits cross-tier executable equality for any program that iterates — rejected |

Sorted wins on the house's own terms: it keeps order a function of *content*
(the line `==` already draws), it *specifies* one order rather than
inheriting python/ts container behavior (the docs/arithmetic.md doctrine),
and it needs no change to the representation table below — the two costly
retrofits (go's wrapper, rust's non-std crate) are avoided. The only
precondition, orderable keys, is already met: keys are `Str`. Full costing,
the canonical order (Unicode scalar / UTF-8 byte lexicographic, with the
ts/java UTF-16 comparator note), and the per-tier implementation sketch are
in **docs/collections.md**. The order is pinned as TCK case
`c1_map_iteration_order` (requirement C1), reported *pending* until a
runtime drives real map iteration.

### Per-tier representation

| tier | representation | equality lowering |
|---|---|---|
| python | `dict` | native `==` (order-independent) |
| typescript | built-in `Map<K, V>` | `revlEq` gains a `Map` branch |
| go | `map[string]V` | `reflect.DeepEqual` via `revlEq` |
| rust | `std::collections::HashMap<String, V>` | native `PartialEq` |
| java | `java.util.HashMap<String, V>` | native `Map.equals` |

All five give structural, order-independent map equality natively; only TS
needs help, because `Object.keys(new Map())` is `[]`.

### `size`, `keys`, `remove` — the iteration/remove spec step

These three graduate together because they share one design decision set.

**Method form, not free functions.** `size()` follows the method-on-Map
precedent (`m.size()`) rather than a `size(m)` builtin: revl has no
free-function namespace to pollute, and every other Map operation is already
a method on the receiver. `size` (not `length`) because the List surface
already owns `length` for element counts and reusing the name across
dict-like and sequence-like receivers would invite the wrong intuition about
order; `size` is the JS-Map spelling and says "how many entries".

**Iteration = `keys()`.** There is deliberately no `for (k, v) over m` yet —
revl's loop story is under design. Iteration ships as `keys(): List[Str]`
in ascending canonical order (the order contract below, pinned by tests on
every hosted tier); with `lookup`, that composes into any walk of the table.
The order is **sorted canonical**, NOT insertion order: go and rust randomize
map order by design, so insertion order would force an ordered-wrapper
retrofit on both (docs/collections.md weighs the options and rejects them).
Sorted order is deterministic, tier-portable with no representation change,
and a pure function of content — the same property map equality already has.

**Removal is persistent and total.** `remove(k)` returns a NEW map without
`k`, exactly as `set` returns a new map with it — never mutation, matching
the G6 value rule. A missing key is **not** an error: the result is a map
equal to the receiver (`has(k)` was already false, so there is nothing to
report), which keeps `remove` total and composable in expression position.
A defined error would force every caller to pre-check `has`, duplicating the
lookup for no added safety. `remove` reuses a host verb name (`Map.remove`
on the v1 stub) — the ONE sanctioned overlap between the two namespaces,
safe because dispatch is by receiver kind: a constructor-tracked host
receiver checks against the family surface before the stdlib table is ever
consulted. The namespace-invariant test pins the overlap at exactly
`{"remove"}` so it cannot grow silently.

**wasm still refuses.** That tier lowers only Int/Bool/String/List over its
canonical-ABI model; a persistent map needs a richer value model than that
tier has (there are not even pairs to spell an assoc list with). `set`,
`lookup`, `has`, `size`, `keys`, `remove` and `Map.empty()` therefore fail
with the same named tier error shape as `indexOf` did — an honest refusal,
never a miscompile. It graduates when the tier grows a map (or assoc-list)
value model.

**Go's inference limit, stated honestly:** Go cannot infer a composite
literal's type from later use, so `var m = Map.empty()` compiles on every
tier *except* that the go emitter needs the empty map's type pinned by
context — a typed return position (`fn newTable() -> Map[Str, Int] { return
Map.empty() }`), a parameter, or any annotated flow. Unpinned, it refuses
at emit time with a message saying so, mirroring how an untyped empty list
literal already behaves on tiers that cannot infer it.

### `Int.to_str()`: the rendering builtin

`n.to_str()` renders an `Int` as its decimal spelling: ASCII digits, a
leading `-` for negatives, no plus sign, no separators, `0` for zero. It is
**total** — including `Int.MIN`, whose magnitude has no positive
representative; every tier must render `-9223372036854775808` exactly (the
wasm helper does it with unsigned division on the negated bit pattern, the
one trick that survives the wrap).

It is a *method*, not a free function, on purpose: revl has no free-function
namespace to pollute (the same grain that made Map's surface methods), and
an Int-only receiver family is already how the checker dispatches Int-only
builtins (`div_trunc` and friends). The name follows the type, not the
host — `to_str`, spelled after the revl type `Str`, on every tier.

## Versioning

Integer division and modulo are specified in **docs/arithmetic.md**, including
why `/` is true division and `%` keeps TypeScript's truncated remainder.
