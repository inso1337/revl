# The stdlib List module (roadmap item 194)

**The need:** the reference emitters build sets — `uses`, `user_cases` — and
then reach for two set operations revl does not have: `x in set` (membership)
and `sorted(set)` (an ordered walk). revl has **neither a set type nor list
`sort` / `contains`**. So the first self-hosted py emitter (`selfhost/emit_py.rvl`,
item 192) worked around both, and neither workaround generalises:

- `x in set` became a hand-rolled `str_in(xs: List[Str], s: Str) -> Bool`;
- `sorted(uses)` became a **hardcoded canonical order** — the finite import
  vocabulary (`Frame`, `Job`, `Map`, `Pool`, `schedule_after`, `schedule_every`)
  walked as a fixed `if`-sequence in `emit_src`, with a comment explaining that
  Python's `sorted` puts the capitalised names before the lower-case
  `schedule_*`. That is correct for six known strings and useless for an
  arbitrary type graph.

`stdlib/list.rvl` is those two operations, once, typed, and portable — so a
self-hosted emitter drops the hardcoding and the private `str_in`, and `use`s
the module instead.

It is kin to `docs/stdlib-value.md` (180/188, walk an erased IR document) and
`docs/stdlib-str.md` (193): the small building blocks a Path B emitter needs
and the base stdlib does not ship.

## Pure revl — nothing to defer per tier

Unlike `stdlib/value.rvl` (which bridges the erased host value, so it is `@py`
with a per-tier follow-up), **every function here is pure revl**, built on the
base List/Str surface (`for … of`, `.push`, `.length()`, `.charCodeAt`). There
is no `@py` block, so there is nothing to defer: the module compiles and runs
identically on **every** backend (py / ts / rs / go / java / wasm) the moment
that tier emits the base List/Str builtins. The py tier is executed by
`tests/test_list_stdlib.py`; the rest inherit for free.

## The surface

`use "stdlib/list.rvl" { … }` — the file lives in the repo as `stdlib/list.rvl`,
one `pub fn` per operation.

| function | type | meaning |
| --- | --- | --- |
| `list_contains(xs, x)` | `(List[Str], Str) -> Bool` | membership; empty list → `false` (the `str_in` replacement) |
| `list_sort(xs)` | `List[Str] -> List[Str]` | ascending lexicographic by **Unicode code point**; stable; total (the `sorted(uses)` replacement) |
| `list_dedup(xs)` | `List[Str] -> List[Str]` | drop later duplicates, keep **first-occurrence** order; total (set-from-list) |
| `str_lt(a, b)` | `(Str, Str) -> Bool` | the codepoint comparator `list_sort` is built on; matches Python's `<` on `str` |

The emitter's `sorted(set(xs))` is `list_sort(list_dedup(xs))`.

```revl
// the kit, inlined so the doc block is a complete, compilable program;
// in real code this is `use "stdlib/list.rvl" { list_contains, list_sort, list_dedup }`
pub fn str_lt(a: Str, b: Str) -> Bool {
  let la = a.length()
  let lb = b.length()
  var i = 0
  while (i < la && i < lb) {
    let ca = a.charCodeAt(i)
    let cb = b.charCodeAt(i)
    if (ca < cb) { return true }
    if (ca > cb) { return false }
    i += 1
  }
  return la < lb
}
pub fn list_contains(xs: List[Str], x: Str) -> Bool {
  for (e of xs) { if (e == x) { return true } }
  return false
}
pub fn list_sort(xs: List[Str]) -> List[Str] {
  var out = []
  for (x of xs) {
    var res = []
    var placed = false
    for (y of out) {
      if (!placed && str_lt(x, y)) { res = res.push(x); placed = true }
      res = res.push(y)
    }
    if (!placed) { res = res.push(x) }
    out = res
  }
  return out
}
pub fn list_dedup(xs: List[Str]) -> List[Str] {
  var out = []
  for (x of xs) { if (!list_contains(out, x)) { out = out.push(x) } }
  return out
}

// the emitter's `sorted(set(uses))`, in pure revl
fn sorted_uses(uses: List[Str]) -> List[Str] { return list_sort(list_dedup(uses)) }
```

## Ordering — codepoint, byte-for-byte `sorted()`

`list_sort` is ascending lexicographic by **Unicode code point**, byte-for-byte
identical to Python's `sorted()` on `List[Str]`. This matters because it is what
lets the emitter's `sorted(uses)` drop its hardcoding: the two must agree on
order exactly, or the emitted `from runtime import …` line changes and the
byte-frozen v1 output breaks.

Two things make the match exact and portable:

- **`str_lt` compares by code point, not by the host's native `<`.** It walks
  `charCodeAt` position by position and, on a shared prefix, the shorter string
  is the smaller — which *is* Python's `str` comparison (`ord` per position,
  shorter-prefix-is-less). It deliberately does **not** use the host's native
  `<` on strings: `charCodeAt` is the Unicode scalar on every tier
  (`docs/strings.md`), whereas a host's native string `<` is UTF-16-code-unit
  order on some tiers (JS), which diverges for astral code points. Codepoint
  order is what `sorted()` uses.
- **The one case that proves it:** mixed case. `sorted(["b","A","a","B"])` is
  `["A","B","a","b"]` — every capital before every lower-case letter (`A` is
  U+0041 = 65, `a` is U+0061 = 97), **not** the case-insensitive
  `["a","A","b","B"]`. `tests/test_list_stdlib.py` pins this directly and then
  fuzzes 3000 random mixed-case / shared-prefix / empty / punctuation lists
  against `sorted()`.

`list_sort` is **stable** (an equal element is inserted after the equal ones
already placed) and **total** (empty and single-element lists are returned
unchanged; it never faults).

## Str, not `[T]` — and why no `Set[T]`

The functions are `Str`-specialised, as `value_*` was. The reason is the
kit's anchor, `list_sort`: it **cannot** be generic. The checker restricts
`<` / `<=` / `>` / `>=` to `Numeric | Str` (`src/revl/typecheck.py`,
`_binop_type`), so an ordering over a bare type parameter `T` is refused with
`` `<` cannot order `T` values``. `list_contains` and `list_dedup` need only
`==` and *would* compile as generic `[T]` — but shipping the whole kit `Str`-only
keeps it a coherent, single-type-surface **drop-in** for the emitters'
`str_in(List[Str], Str)` and `sorted(List[Str])`, with no mixed generality to
reason about. Generalising `contains`/`dedup` to `[T]` is a clean future
extension; a generic `sort` would need an ordered-type bound the language does
not yet express.

**A `Set[T]` type is explicitly out of scope.** A set is a language feature — a
type with its own literal, membership operator, and checker support — not a
stdlib module. This module delivers exactly the list operations that cover the
emitters' `sorted(set)` / `in set` needs (`list_sort ∘ list_dedup`,
`list_contains`). If a first-class `Set[T]` is later added, these functions
remain the `List`-side conversions to and from it (`list_dedup` is
set-from-list; `list_sort` gives a set a deterministic order for emission).

## What it obsoletes in `selfhost/emit_py.rvl` (spec for a future refactor)

This module is a **drop-in** for two hand-rolled bits in `selfhost/emit_py.rvl`
(item 192). That file is owned by another slice and is **not edited here**; this
is the spec for the refactor that adopts the kit:

1. **`fn str_in(xs: List[Str], s: Str) -> Bool`** (the "string utils" block) →
   delete it and `use { list_contains }`. Same signature, same semantics; the
   call sites (`str_in(all_names, tok)`, `!str_in(emitted, tok)`,
   `!str_in(shadow, "Ok")`, …) become `list_contains(...)`.
2. **The hardcoded `sorted(uses)` order in `emit_src`** — the fixed
   `imports.push("Frame …")` / `Job` / `Map` / `Pool` / `schedule_after` /
   `schedule_every` sequence, with the comment "walked here as a fixed sequence
   over the finite import vocabulary" → build the `uses` list unordered
   (`list_dedup` if a name can be pushed twice) and emit `list_sort(uses)`
   instead of the fixed walk. That removes the "won't generalise to an arbitrary
   type graph" limitation the comment calls out: once a fn/test body can pull an
   *arbitrary* host root or `_revl_` alias into `uses`, `list_sort` still orders
   it exactly as Python's `sorted()` would, so the emitted `from runtime import …`
   line stays byte-identical to the reference without anyone extending the
   hardcoded sequence.

The byte-exactness test in `tests/test_list_stdlib.py` is what makes that
refactor safe to land: it proves `list_sort` equals `sorted()` before any
emitter depends on the equality.
