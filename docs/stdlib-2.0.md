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
| `codepoint_at(i)` | 1 | Str | code point at i, returned directly (item 276) | `ord(x[i])` | `x.charCodeAt(i)` |
| `indexOf(v)` | 1 | Str, List | first index, `-1` if absent | inline dispatch helper | `x.indexOf(v)` |
| `concat(y)` | 1 | Str, List | joined copy | `x + y` | `x.concat(y)` |
| `split(sep)` | 1 | Str | pieces between separators; `""` → 1-char strings; trailing empties kept | inline dispatch | `x.split(sep)` |
| `join(sep)` | 1 | List[Str] | elements joined by sep | `sep.join(x)` | `x.join(sep)` |
| `repeat(n)` | 1 | Str | n copies concatenated | `x * n` | `x.repeat(n)` |
| `startsWith(p)` | 1 | Str | prefix probe: `p` is the receiver's first `p.length()` code points (FR-6) | `x.startswith(p)` | `x.startsWith(p)` |
| `endsWith(p)` | 1 | Str | suffix probe: `p` is the receiver's last `p.length()` code points (FR-6) | `x.endswith(p)` | `x.endsWith(p)` |
| `is_digit()` | 0 | Str | single-char ASCII digit `0`-`9` (item 233) | `"0" <= x <= "9"` | chained compare in an IIFE |
| `is_alpha()` | 0 | Str | single-char ASCII letter `a`-`z`/`A`-`Z` (NOT `_`) | chained range compare | chained range compare |
| `is_alnum()` | 0 | Str | single-char ASCII letter or digit | chained range compare | chained range compare |
| `is_space()` | 0 | Str | single-char ASCII blank: space, tab, LF, CR | `x in (" ", "\t", "\n", "\r")` | equality set |
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
| `to_int()` | 0 | Str | ASCII-digit parse → `Opt[Int]` (FR-9); ALSO the Int32→Int widen (§ below) | lambda | `revlParseInt(x)` |

- `push`/`concat` are **persistent** (value semantics) — consistent with
  capture-by-value and G6: no revl value is ever mutated in place. Rebind:
  `out = out.push(v)`.
- `.length` also works in property position (the existing `len` node).
- List element read is indexing (`xs[i]`), not a method.
- `charAt`/`charCodeAt` are Str-only, and the checker enforces it: a non-Str
  receiver is refused with ``builtin `charAt` needs a Str receiver, got
  `List[Int]` `` (`src/revl/typecheck.py`).
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
canonical-ABI string/list model, and now also the reader trio — `split`
(`$str_split` → `List[Str]`), `join` (`$str_join`), and `Str.indexOf`
(`$str_index_of`, code-point index out) — closing the harness's reader gap.
It still rejects `repeat` and `List.indexOf` (the per-element comparison) with
its usual named tier error — not yet lowerable on that tier. The FR-6/FR-9
additions (`startsWith`, `endsWith`, `to_int`)
DO lower on wasm: byte-comparison helpers are exact for UTF-8 prefixes, and
the `$str_to_int` helper parses straight to the tier's Opt cell. The full
wasm tier-capability matrix (values, builtins, service boundary shapes, and
each hard refusal) is **docs/wasm-capabilities.md**.

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
`insert/insert_if_absent/remove/get/drop`) keeps its exact existing surface.
`insert_if_absent(k, v) -> Bool` (item 397) is the atomic compare-and-set: it
inserts and returns `true` only when the key was absent, otherwise it leaves
the existing value untouched and returns `false`, and its result is the first
host-verb result the frontend types (a `Bool`, so a claim can branch on it).
It is spelled as a bound acquisition, `let fresh = effect
store.insert_if_absent(k, v) undo store.remove(k)`, and the undo is
result-guarded (a `false` CAS registers no inverse). Per tier the test and the
insert are one atomic step: a lock spanning both on go/rust, ConcurrentHashMap
`putIfAbsent` on java, run-to-completion of a synchronous op on py/ts. See
docs/design/397-insert-if-absent.md. The two
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
- **Bottom-typed receivers learn `V` from use.** A bottom `V` is not a
  constraint the call has to satisfy, so `set` on a `Map[Str, Never]`
  receiver would otherwise prove nothing about its value argument and
  return another `Map[Str, Never]` —
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

### `Str.startsWith(p)` / `Str.endsWith(p)`: the prefix/suffix probes (FR-6)

`x.startsWith(p)` is true iff the first `p.length()` code points of `x`
equal `p`; `x.endsWith(p)` is true iff its last `p.length()` code points do.
The empty string is a prefix and a suffix of every string (so
`x.startsWith("")` is always true), and a string is a prefix of itself.
The probes are **Str-only** — declared on the Str family exactly like
`charAt`/`split` — and both arguments count in code points
(docs/strings.md), never UTF-16 units.

They exist because protocol parsing is the harness's daily bread: the wire
format is prefix-tagged (`FINAL `, `TOOL_CALL `), and the harness hit a real
off-by-one (`"TOOL_CALL "` is 10 chars, sliced 9) that
`resp.slice(0, 9) == "TOOL_CALL "` cannot catch — the checker sees two
strings compare equal-or-not, never the fence's *shape*. `startsWith` names
the intent.

Lowering is native on every tier (`str.startswith`, `String.prototype.
startsWith`, `str::starts_with`, `String.startsWith`, `strings.HasPrefix`,
and a byte-comparison WAT helper on wasm — a code-point prefix of a valid
UTF-8 string is exactly a byte prefix). All six tiers carry it.

### `Str.is_digit()` / `is_alpha()` / `is_alnum()` / `is_space()`: single-char classification (item 233)

Four **argument-less Str probes** that classify a single ASCII code point and
return `Bool`:

- `is_digit()` — the receiver is one ASCII digit `0`–`9`.
- `is_alpha()` — one ASCII letter `a`–`z` or `A`–`Z`. **Letters only — `_` is
  not a letter** (the lexer's "identifiers may contain `_`" rule stays explicit
  at the call site, not baked into the classifier).
- `is_alnum()` — the union of `is_alpha` and `is_digit`.
- `is_space()` — one of space, tab, LF, CR.

The receiver is a **one-character** string. The empty string classifies as
`false` and nothing faults; multi-character input is outside the per-character
contract (the classifiers answer about a single code point) but stays total —
it never raises. All four are **Str-only**, declared on the Str family like
`charAt`.

They exist to cut the **self-host lexer's per-byte cost**. The lexer scans
identifiers and numbers one source byte at a time; each byte previously paid a
revl-fn call (`is_alnum(c)`) plus a `charCodeAt`/`ord` round-trip and a
code-point range compare. Lowered native — a chained comparison (`"0" <= x <=
"9"`) or tuple membership — the classification collapses to an inline test with
no call and no intermediate `ord`. See `docs/bench-selfhost.md` for the lexer
before→after.

**Tier status.** **py, typescript and rust** lower all four native (`char::is_ascii_*`
on rust, a chained range compare or equality set on ts). **go, java and wasm**
still refuse them, each with its own message (`unknown v3 builtin method
'is_digit'` on go, `unknown builtin method 'is_digit'` on java, `unsupported
builtin method 'is_digit'` on wasm). A tier that later adopts them lowers to its
own ASCII test.

### `Str.codepoint_at(i)`: codepoint-at-index scan (item 276)

`codepoint_at(i)` returns the Unicode scalar value at code-point index `i` as an
`Int`, **directly** — no intermediate 1-char `Str`. It is the codepoint-domain
partner of `charAt` (which returns the 1-char string), and semantically it
matches `charCodeAt(i)`; the point of the separate name is the **self-host
lexer's hot path**, which previously spelled the code point at `j` as
`code0(source.charAt(j))` — a `charAt` that allocates a 1-char `Str`, then a
revl-fn call that indexes it a second time to reach `charCodeAt(0)`. Reading
`source.codepoint_at(j)` drops the fn call and the second index. Like
`charAt`/`charCodeAt`, the index is assumed **in bounds** (`0 <= i < length()`);
the lexer only reads a position it has already guarded with `j < n`. For a
position that may be past the end, `slice`-then-guard is still the total form
(the lexer keeps its `code0` helper, which returns `-1` on an empty clamped
slice, for exactly those probes).

**Lowering per tier.** py `ord(x[i])`; ts/go/java via the same astral-aware
`charCodeAt` helper (a lone JS surrogate would otherwise leak through
`String.charCodeAt`); rust `x.chars().nth(i).unwrap() as u32 as i64`; wasm the
UTF-8-decoding `$str_cp_char_code_at` helper. On the **py tier** the win is
small — CPython caches 1-char Latin-1 strings, so the `charAt` "allocation" was
already near-free and only the fn call is reclaimed — but on the **native tiers**
`charAt`'s lowering allocates a heap `String` (`…to_string()` on rust) per byte,
which `codepoint_at` avoids entirely. That is the residual-lexer perf lever the
item-231a finding pointed at. See `docs/bench-selfhost.md` for the before→after.

### `Str.to_int()`: the parsing builtin (FR-9)

`s.to_int()` parses `s` as an `Int` and answers `Opt[Int]`: `Some(n)` on the
ASCII digits with an optional leading `-` (`"42"`, `"-7"`, `"007"`), `None`
for **everything else** — empty, `"-"`, `"+"`-prefixed, whitespace, partial
digit runs (`"12a"`), non-ASCII digits, and **out-of-i64-range magnitudes**
(`"9223372036854775808"` is `None`, exactly like a non-digit; `Int.MIN`
itself — `"-9223372036854775808"` — parses, whose magnitude has no positive
representative, the same edge `Int.to_str` renders). It mirrors
`Int.to_str()`: the same name family, the reverse direction, the same total
domain (every `Int` renders, every digit string parses).

It is a *method*, not a free function, on purpose (the `Int.to_str()` grain),
and it is the first builtin whose **spelling is shared by two receiver
families**: `to_int` is also the Int32→Int widening (docs/arithmetic.md,
"Sized integers"). The checker dispatches by receiver head — `Int32.to_int`
widens, `Str.to_int` parses — and a receiver that is neither (`Bool.to_int`,
`Int.to_int`) is refused listing both families. The IR carries the receiver's
static type on the builtin node (`recv`), because the backends must dispatch
the same way and no tier can infer it from the method name alone.

The Opt result composes exactly like every other Opt: `s.to_int() ?? 0`,
`match s.to_int() { Some(v) => v, None => 0 }`. Every tier's lowering is a
one-liner over its native parse (`int()` under an ASCII/range gate on py,
a regex-guarded `BigInt` on ts, `str::parse::<i64>().ok()` on rust,
`Long.parseLong` under an ASCII gate on java, a hand-rolled unsigned
accumulator on go and wasm — the wasm helper is the one that needs care,
since `Int.MIN`'s magnitude is 2^63 and every larger magnitude must be
`None`).

## Crypto primitives — `stdlib/crypto.rvl` (item 272)

Three independent components in the lighthouse workload hand-rolled the **same**
crypto inside `@py`/`@ts` extern bodies within one wave — a constant-time
compare (a token gate), an HMAC-SHA256 encrypt-then-MAC (a settings store), and
two HMAC webhook-signature schemes (inbound verification). Three witnesses, all
security-load-bearing, none sharing code. `stdlib/crypto.rvl` is that kit,
**once** — a classified PRIMITIVE set (the item-244 pattern), deliberately not a
framework. It ships the four irreducible operations those sites re-derived and
stops there; a call site composes them (encrypt-then-MAC is `hmac_sha256` over
the ciphertext plus a `ct_equal` on verify) exactly as before, now over one
audited implementation instead of three.

`use "stdlib/crypto.rvl" { sha256, hmac_sha256, ct_equal, random_token }` — the
file lives in the repo as `stdlib/crypto.rvl`.

| fn | signature | class | semantics |
|---|---|---|---|
| `sha256(data)` | `Str -> Str` | `pure` | lower-case hex SHA-256 of the UTF-8 bytes of `data` (64 chars) |
| `hmac_sha256(key, data)` | `(Str, Str) -> Str` | `pure` | hex HMAC-SHA256 (RFC 2104) with `key` over `data`, both UTF-8 |
| `ct_equal(a, b)` | `(Str, Str) -> Bool` | `pure` | constant-time equality — full-width, no early exit |
| `random_token(n)` | `Int -> Str` | **`emission`** | `n` cryptographic random bytes as `2n` hex chars |

**py + ts are the exit bar** (item 272). `sha256`/`hmac_sha256`/`ct_equal` ship
a self-contained pure-JS SHA-256 on the ts tier (no host import, exactly as
`stdlib/json.rvl` ships a pure-JS parser); `random_token` draws from
`globalThis.crypto.getRandomValues` (a synchronous WHATWG global). Both tiers
hash the identical UTF-8 encoding, so the hex digests agree across py and ts.
The rust/go/java/wasm bodies are a documented follow-up.

### Why `random_token` is `emission`, not `pure`

The other three are `pure`: a hash, a MAC, and a compare are total functions of
their inputs with no observable effect and no entropy. An **entropy draw is
not** — two calls with the same argument return different values, the defining
non-property of a pure function. Its classification is a design decision with
the same rigor item 244 spent on its clonefile-preimage choice:

- **Not `acquire`-with-a-trivial-undo.** `acquire` models a resource you hold
  and must release; its `undo` replays on *clean unload and abort* alike. A
  token draw holds nothing — no handle, no lease. The only `undo` one could
  write is a no-op, and a no-op undo is a lie in the shape of a contract: it
  tells the teardown accumulator the effect was cleanly reverted when nothing
  was, and pays for a disposer on every draw.
- **Not `witnessed`.** 244 earned `witnessed` for an fs write because a preimage
  *snapshot* makes the inverse real. There is no preimage of an unpredictable
  draw, and no inverse can un-observe a minted secret — so `witnessed` is
  unearnable here.
- **`emission` is the honest reading.** A draw reads external entropy (the host
  CSPRNG), advancing non-recoverable state and producing a fresh secret: a
  non-revertible boundary crossing. It is also the correct *policy* reading —
  token minting is security-load-bearing and should be visible to the item-33
  policy gate / audit (`caps(extern emission fn e) = { e }`), which a `pure` or
  fake-`acquire` draw would hide.

A `fn`, `test`, or provider that draws a token therefore carries the
`random_token` capability outward, honestly.

### Examples

`sha256` — a content-addressing fingerprint (the `@py` body is inlined so the
block is a complete, compilable program; real code writes the `use` above):

```revl
// stdlib/crypto.rvl ships this as `pub extern pure fn sha256`; the @py body is
// inlined so this doc block is a complete, compilable program. In real code you
// write `use "stdlib/crypto.rvl" { sha256 }` instead of the extern declaration.
pub extern pure fn sha256(data: Str) -> Str = @py {
    import hashlib
    return hashlib.sha256(data.encode("utf-8")).hexdigest()
}

// content-addressing: a stable fingerprint of a blob
pub fn fingerprint(blob: Str) -> Str { return sha256(blob) }
```

`hmac_sha256` — the webhook-signature site, once:

```revl
pub extern pure fn hmac_sha256(key: Str, data: Str) -> Str = @py {
    import hashlib, hmac
    return hmac.new(key.encode("utf-8"), data.encode("utf-8"), hashlib.sha256).hexdigest()
}

// the webhook-signature site, once: sign a request body with the shared secret
pub fn webhook_signature(secret: Str, body: Str) -> Str {
  return hmac_sha256(secret, body)
}
```

`ct_equal` — verify an inbound webhook without a timing side channel (never
`==` on a secret-derived value):

```revl
pub extern pure fn ct_equal(a: Str, b: Str) -> Bool = @py {
    import hmac
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))
}
pub extern pure fn hmac_sha256(key: Str, data: Str) -> Str = @py {
    import hashlib, hmac
    return hmac.new(key.encode("utf-8"), data.encode("utf-8"), hashlib.sha256).hexdigest()
}

// verify an inbound webhook without a timing side channel: recompute and
// constant-time compare, never `==` on a secret-derived value
pub fn webhook_ok(secret: Str, body: Str, sig: Str) -> Bool {
  return ct_equal(hmac_sha256(secret, body), sig)
}
```

`random_token` — mint a session id; the `emission` classification is visible in
the declaration:

```revl
// `random_token` is `emission`, not `pure`: an entropy draw reads the host
// CSPRNG and cannot be taken back, so a `fn` that mints a token carries the
// `random_token` capability outward (visible to the policy gate / audit).
pub extern emission fn random_token(n: Int) -> Str = @py {
    import secrets
    return secrets.token_hex(n)
}

// mint a 32-hex-char session id (16 random bytes)
pub fn new_session_id() -> Str { return random_token(16) }
```

## Versioning

Integer division and modulo are specified in **docs/arithmetic.md**, including
why `/` is true division and `%` keeps TypeScript's truncated remainder.
