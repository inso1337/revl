# The stdlib Str module (roadmap item 193)

**The need:** a self-hosted revl stage that parses type spellings and renders
source re-derives the same little string toolkit every time. `selfhost/
checker.rvl` and `selfhost/emit_py.rvl` (item 192) **both** hand-roll it —
`trim` / `trim_ws` / `lstrip` / `rstrip` / `last_index_of` / `ident_tokens` /
`split_type` / `split_types` — directly over the base `Str` surface (item 11:
`length` / `charAt` / `charCodeAt` / `slice` / `indexOf` / `startsWith` /
`concat` / `split` / `join`). The next emitter stages (`emit_ts`, `emit_rust`,
`emit_go`, `emit_java`, `emit_wasm`) would each re-derive it a third, fourth,
fifth time. This module is that kit, once, so a stage `use`s it instead.

It is the string-shaped sibling of `stdlib/value.rvl` (item 180): where
`value.rvl` factors out the *IR-navigation* bridge, `str.rvl` factors out the
*string-munging* bridge that sits right beside it in every emitter.

## Pure revl — the key difference from `value.rvl`

`stdlib/value.rvl`'s accessors are per-tier `@py` externs, with the other five
tiers deferred. **`stdlib/str.rvl` is different: every function is PURE revl.**
It adds no new primitive — it only *composes* the base `Str` methods item 11
already froze, and those lower on every tier. So the kit works on **py, ts, rs,
go, java and wasm the day it lands**, with no per-tier follow-up. It
deliberately avoids the two base methods that do not lower on wasm
(`Str.repeat`, `List.indexOf`); `contains` / `index_of` use `Str.indexOf`,
which does (`docs/stdlib-2.0.md`).

The one subtlety the purity forces: **revl plain strings carry no escapes** —
`"\n"` is the two chars backslash-n, and a literal newline is not allowed
inside `"..."` (see `selfhost/emit_py.rvl`'s `newline()` note). `dedent` needs a
real `U+000A`, so it gets one from a **backtick template holding a literal
newline byte** (`nl()`), which with no interpolation lowers as an ordinary
one-char string literal on every tier — no `@py chr(10)` extern required.

## The surface

`use "stdlib/str.rvl" { … }` — the file lives in the repo as `stdlib/str.rvl`,
one `pub fn` per entry. Every function is **total**: the empty string, a
no-match, and an all-whitespace input each return a defined value, never a
fault.

| fn | signature | semantics |
|---|---|---|
| `is_space(c)` | `Str -> Bool` | is the first code point ASCII whitespace (`space`/`\t`/`\n`/`\v`/`\f`/`\r`); `""` → `false` |
| `trim(s)` | `Str -> Str` | strip leading+trailing ASCII whitespace (Python `str.strip()` for ASCII) |
| `lstrip(s)` | `Str -> Str` | strip leading ASCII whitespace |
| `rstrip(s)` | `Str -> Str` | strip trailing ASCII whitespace |
| `contains(s, sub)` | `(Str, Str) -> Bool` | does `s` contain `sub`; `""` is contained in every string |
| `index_of(s, sub)` | `(Str, Str) -> Int` | first index of `sub`, or `-1` (named alias of `Str.indexOf`) |
| `last_index_of(s, sub)` | `(Str, Str) -> Int` | **last** index of `sub`, or `-1`; `""` → `s.length()` (Python `rfind`) |
| `ident_tokens(s)` | `Str -> List[Str]` | maximal identifier runs, i.e. `re.findall(r"[A-Za-z_]\w*", s)` (ASCII `\w`) |
| `split_top(s, sep)` | `(Str, Str) -> List[Str]` | split on single-char `sep` at bracket depth 0 (`[]` and `()`), each part trimmed; `""` → `[]` |
| `dedent(text)` | `Str -> Str` | **byte-exact** to Python `textwrap.dedent` |

`is_space` is exported because it is the reusable character predicate the strip
functions share; `is_word_ch` / `is_alpha_us` / `is_sp_tab` / `nl` /
`common_prefix` / `leading_sp_tab` / `line_is_ws_only` stay private.

## `dedent` — byte-exact to `textwrap.dedent`

`dedent` is the specific blocker this item was cut for: the deferred py-emitter
externs form (item 192, slice 4) renders a multi-line extern body, and the
reference reaches for `textwrap.dedent`. A self-hosted `emit_py` cannot call
Python's `textwrap`, so the behaviour has to exist in revl, **byte-for-byte**.

It reproduces CPython's `Lib/textwrap.py` algorithm exactly:

1. **`_whitespace_only_re = ^[ \t]+$`** (multiline) → `""`: a line that is
   entirely spaces/tabs is normalised to empty *before* margins are measured.
2. **`_leading_whitespace_re = (^[ \t]*)(?:[^ \t\n])`**: the indent of each
   *content* line (a line with a non-whitespace char). Blank lines are excluded
   from the margin entirely.
3. **margin** = the longest common prefix of those indents, compared char by
   char (a tab and a space are distinct, so a space-indented and a
   tab-indented line share the empty margin).
4. strip `margin` from the start of every line.

The whitespace class in dedent is **only `[ \t]`** — never `\n\r\v\f` — which is
why `dedent` uses the private `is_sp_tab`, not the wider `is_space`. Byte-
exactness is pinned in `tests/test_str_stdlib.py` two ways: a table of
hand-picked cases covering every branch (tab margins, whitespace-only-line
normalisation, divergent margins → empty common prefix, no-trailing-newline),
and a **400-iteration randomized fuzz** comparing `dedent(text)` against
`textwrap.dedent(text)` for random line shapes.

## What it obsoletes in the self-host stages (the refactor spec)

These files are **off-limits** to this item (owned by items 190/191 and the
frozen self-host set); the mapping below is the spec for a *future* refactor
that would delete the hand-rolled copies in favour of a `use`:

| hand-rolled helper | file (read-only) | replaced by |
|---|---|---|
| `trim(s)` (space-only) | `selfhost/emit_py.rvl` | `trim` |
| `lstrip(s)` | `selfhost/emit_py.rvl` | `lstrip` |
| `last_index_of(s, ch)` (single char) | `selfhost/emit_py.rvl` | `last_index_of` (generalised to substrings) |
| `ident_tokens(s)` | `selfhost/emit_py.rvl` | `ident_tokens` |
| `split_types(inner)` | `selfhost/emit_py.rvl` | `split_top(inner, ",")` |
| `is_alpha_us` / `is_word_ch` | `selfhost/emit_py.rvl` | private `is_alpha_us` / `is_word_ch` inside the kit |
| `trim_ws(s)` (space/tab/LF/CR) | `selfhost/checker.rvl` | `trim` |
| `is_ws_ch(c)` | `selfhost/checker.rvl` | `is_space` |
| `split_type(t)` argument split | `selfhost/checker.rvl` | `split_top(inner, ",")` |

`split_top` tracks both `[]` and `()` depth, so it is a drop-in for
`emit_py.rvl`'s `split_types` and a compatible generalisation of
`checker.rvl`'s `split_type` (which tracks `[]` only) — identical on any input
without bare/unbalanced parens, which is every type spelling those stages feed
it. The remaining `split_type` / `split_fn_type` glue (head/args destructuring,
the `->` arrow split) stays stage-local: it is parsing structure on top of the
`split_top` primitive this kit provides.
