# Collections — deterministic iteration order

revl's `Map` (docs/stdlib-2.0.md §Map) is a *value* type with structural,
order-independent equality: `{a=1, b=2} == {b=2, a=1}`, and "insertion order is
never observable through `==`." It deliberately ships today with **no
iteration** — no `keys()`, no `length`, no `for`-over-a-map. That absence is
exactly why this document exists.

The moment iteration exists, the tiers disagree by default. There is no shared
"iteration order" to inherit: it is a place each host language made a different
choice, and two of them made *randomness* the choice on purpose.

| tier | native map | default iteration order |
|---|---|---|
| python | `dict` | insertion order (guaranteed since 3.7) |
| typescript | `Map` | insertion order (spec) |
| go | `map[string]V` | **randomized every run, by language spec** |
| rust | `HashMap<String,V>` | **randomized per-`HashMap` (seeded), by design** |
| java | `java.util.HashMap` | unspecified (bucket order; not insertion) |
| wasm | *refuses `Map`* | n/a — the tier has no map value model yet |

If `keys()` ships without a contract, that divergence ships with it: a revl
program that iterates a map would print one order on python, a *different order
on every run* on go and rust, and a third order on java. That is a runtime-truth
violation — the same class of bug docs/arithmetic.md was written to kill, where
"every tier used to compute whatever its host language happened to compute." So
we decide the order **before** iteration ships, per the house rule: spec, then
checker, then emitters, then tests.

## The three options, costed per tier

"Free" means the tier's existing representation already yields the order with no
code and no representation change. Costs are stated against the representation
table already pinned in docs/stdlib-2.0.md §Map (`dict` / `Map` / `map[string]V`
/ `HashMap` / `HashMap`).

### Option A — insertion order

Yield keys in the order they were first `set`. Matches python/typescript for
free; every other tier must be retrofitted to *remember* insertion, which none
of their native maps do.

| tier | cost |
|---|---|
| python | **free** — `dict` is insertion-ordered |
| typescript | **free** — `Map` is insertion-ordered |
| go | **invasive** — `map[string]V` cannot record insertion; needs an ordered wrapper (`struct{ keys []string; m map[string]V }`), threaded through every `set`, and it breaks the clean `reflect.DeepEqual` equality (a custom eq is then required) |
| rust | **invasive** — `HashMap` has no order; needs `IndexMap` (a non-std crate, against the "std-only minimal real runtime" grain) or a hand-rolled `Vec<String>` + `HashMap` |
| java | **cheap** — swap `HashMap` → `LinkedHashMap` (drop-in; `Map.equals` still holds) |
| wasm | n/a (refuses `Map`) |

Insertion order also *reintroduces* history-dependence: what you built the map
from becomes observable, which is precisely the property the equality spec
already renounced ("insertion order is never observable through `==`").

### Option B — sorted-key order  ·  **chosen**

Yield keys in ascending **canonical `Str` order** (defined below). Order is a
pure function of the *key set*, never of construction history. No tier needs a
representation change — the pinned `dict`/`Map`/`map`/`HashMap`/`HashMap` table
stays exactly as it is; each tier only sorts a key list at iteration time.

| tier | cost |
|---|---|
| python | **cheap** — `sorted(m)`; `str` sorts by code point = canonical |
| typescript | **cheap + comparator** — `[...m.keys()].sort(cmp)`; the default sort is UTF‑16 code‑unit order, so a code‑point comparator is needed to match canonical order for supplementary‑plane keys |
| go | **cheap** — `sort.Strings(keys)`; Go string `<` is UTF‑8 byte order = canonical |
| rust | **cheap** — collect keys and `.sort()`; `String` `Ord` is UTF‑8 byte order = canonical (or view via `BTreeMap`) |
| java | **cheap + comparator** — sort a key list; `String.compareTo` is UTF‑16 code‑unit order, so a code‑point comparator (compare by `codePointAt`) is needed for supplementary‑plane keys |
| wasm | n/a (refuses `Map`) |

The only precondition — *orderable keys* — is **already met**: `Map` keys are
constrained to `Str` on every tier (docs/stdlib-2.0.md §Semantics, "Keys are
`Str`"), and every tier can order strings. The one spec obligation is to name
*one* canonical order so all five agree; see below.

### Option C — "iteration order is unspecified"

Honest about the disagreement, and free on every tier. Rejected: it forfeits
cross-tier executable equality for *any* program that iterates a map, which is
the one thing the cross-tier execution suite's runtime-truth rule exists to
protect. "Unspecified" is how go and rust already behave — adopting it is
choosing the bug.

## Decision

**`Map` iteration yields keys in ascending canonical `Str` order.** `keys()`,
and any future `for`-over-a-map or `entries()`, observe that order on every tier
that supports `Map`. Values follow their keys.

Why sorted and not insertion — three reasons, in the house's own terms:

1. **It extends a line the spec already drew.** Map equality is already
   order-independent and "insertion order is never observable through `==`."
   Sorted iteration keeps order a function of *content*; insertion iteration
   reverses that decision and makes construction history observable again.
2. **It specifies rather than inherits** (docs/arithmetic.md doctrine). Sorted
   names one order reproducible everywhere; insertion order inherits
   python/typescript container behavior and forces go/rust/java to mimic it.
3. **It needs no representation change.** Sorted keeps every tier's pinned map
   type; insertion order forces the two *costly* retrofits (go's ordered
   wrapper, rust's non-std `IndexMap`) and reworks go's equality — for a
   property revl explicitly does not want to be observable.

### The canonical `Str` order, pinned

Keys sort **ascending by Unicode scalar value**: compare two keys as sequences
of Unicode scalar values (code points), lexicographically. Equivalently, this is
**UTF‑8 byte lexicographic order** — the two induce the identical total order for
well-formed UTF‑8. A `Map` has distinct keys, so ties never arise.

Native string ordering already agrees with this on three tiers and for all
BMP/ASCII keys (which is every symbol-table key the self-hosted checker
actually builds):

| tier | native string order | matches canonical? |
|---|---|---|
| python | code point (`sorted`) | yes |
| rust | UTF‑8 bytes (`String: Ord`) | yes |
| go | UTF‑8 bytes (`<`) | yes |
| typescript | UTF‑16 code unit (`.sort()`) | **only up to U+FFFF** — needs a code-point comparator for supplementary-plane keys |
| java | UTF‑16 code unit (`String.compareTo`) | **only up to U+FFFF** — needs a code-point comparator for supplementary-plane keys |

The typescript/java wrinkle is stated the way docs/arithmetic.md states the
wasm `Int.MIN` helper and the tier refusals: an honest, bounded per-tier
obligation, not a silent divergence. For supplementary-plane keys the naive
UTF‑16 sort would place a key with a surrogate lead unit differently from its
true scalar; the code-point comparator removes that. For every key the checker
builds today it is a no-op.

## What implementing iteration took (built in the same spec step)

Iteration is no longer unimplemented; `size`/`keys`/`remove` shipped to this
order (docs/stdlib-2.0.md §Map). What each tier does:

- **python** — `return sorted(m)` (or `sorted(m.items())`). No representation
  change.
- **typescript** — `[...m.keys()].sort(cp)` with a code-point comparator `cp`.
- **go** — collect keys, `sort.Strings(keys)`, iterate the slice.
- **rust** — collect `&String` keys, `.sort()`, iterate; or expose a `BTreeMap`
  view. No `IndexMap` dependency.
- **java** — collect keys into a `List`, sort with a `codePointAt` comparator,
  iterate.
- **wasm** — still refuses, with the same named tier error as `set`/`lookup`;
  graduates when the tier grows a map value model.

None of these change the pinned representation table or the order-independent
equality lowering. The decision above is the deliverable; the code lands with
the iteration spec step.

## Conformance

The order is encoded as a TCK case (docs/conformance.md, item 42 — see
`tck/spec.py`, case `c1_map_iteration_order`, requirement **C1**). It builds a
map whose keys are inserted out of order and asserts iteration yields them in
canonical `Str` order — which distinguishes sorted from insertion order and from
any randomized order. The case exists the day this decision lands; because no
runtime exercises map iteration yet, every adapter reports it **pending** (never
green for "unchecked"), and it flips to a live pass/fail the moment a runtime's
adapter drives real map iteration.
