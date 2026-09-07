# tree-sitter-revl

A [tree-sitter](https://tree-sitter.github.io/) grammar for **revl**
(syntax-2.0). One grammar file yields syntax highlighting in every tree-sitter
host — GitHub, Neovim, Zed, Helix — plus a fast incremental parse tree for any
tooling that wants revl's structure.

The grammar mirrors the reference parser (`src/revl/parser.py`) and lexer
(`src/revl/lexer.py`). It is **kept honest by parsing the same corpus** the
reference parser and `selfhost/parser.rvl` already agree on: the grammar ships
only if it parses every `examples/*.rvl` and `selfhost/*.rvl` file with zero
`ERROR` nodes (see [Conformance gate](#conformance-gate)).

## What it covers

- **Modules**: `use "path" { a, b }` / `use "path" as ns`.
- **Services**: `service`, method signatures, and the `emission[caps]` /
  `async` / `commutative` modifiers that are revl's defining surface.
- **Components**: `boot component`, `requires` / `provides` clauses, `config`
  blocks (with the `under "…"` / `in […]` admission bounds a boot component
  declares), the `handoff key: T` state-hand-off prelude, `effect … undo …`
  forms (including `effect { setup … }` blocks, `effect await …`, and `effect
  spawn C with { … }`), `emit … compensate …`, `fail`, `if` guards, `isolate …
  in realm(…)`, `intercept … with { … }`, timers (`every 30s { emit … }` /
  `after 5m { emit … }`), and `provide` methods (`fn f(x) = expr` or `fn f(x) {
  … }`).
- **Types**: records `{ f: T }`, variants `A(T) | B`, aliases `type Rows =
  List[Row]`, generics `Map[Str, Int]`, optionals `T?`, function types `(Int,
  Str) -> Bool`, and `type Id[T] = …` parameters.
- **Functions**: `fn`, `pub fn`, `verified fn`, `fn id[T](…)` type parameters,
  and the full pure-expression stratum — ternary, `||` / `??` / `&&`, the
  bitwise `|` / `^` / `&` / `~` and shift `<<` / `>>` operators (at the
  reference's C/TypeScript precedence), equality/comparison/arithmetic, `!` /
  unary `-`, calls, `.`/`?.` member access, indexing, records, functional
  record update `{ base | f = e }`, lists, arrows (`x => …`, `(a: Int) => …`,
  `(a: Int): Ret => …`, block-bodied `(a) => { … }`), `match`, string (`"…"`,
  `'…'`, `"""…"""` with `\"`/`\'`/`\\` escapes) / template (`` `…${expr}…` ``) /
  number (with `0x`/`0b`/`0o` radices and `_` separators) / boolean / `null`
  literals, and typed `hole` placeholders. `;` is accepted as an optional
  statement separator.
- **Externs & host blocks**: `extern pure|acquire|emission[caps]? async? fn … =
  @backend { … }`. The `@backend { … }` body is brace-balanced host text,
  consumed verbatim by the external scanner (`src/scanner.c`), exactly as the
  reference lexer does.
- **Tests**: `test "…" { … }`, `lifecycle test "…" { load / unload / call /
  advance / assert … }`, `prop test "…" (a: T, …) { assert … }`, and `fault
  test "…" for C { fail at … assert … }`.

Highlighting lives in [`queries/highlights.scm`](queries/highlights.scm):
keywords (with a distinct `@keyword.effect` group for the effect/emission
vocabulary), types, constructors, functions and methods, parameters and record
fields, strings and templates, comments, host-block bodies, numbers, booleans,
and operators.

## Build & install

The tree-sitter CLI is a **local** dev dependency (never global):

```sh
npm install            # installs tree-sitter-cli into ./node_modules
npx tree-sitter generate   # regenerates src/parser.c from grammar.js
```

`src/parser.c`, `src/scanner.c`, `src/grammar.json`, and `src/node-types.json`
are committed so hosts can build the parser without regenerating.

To use it in an editor, point the host at this directory. For example, Neovim
(`nvim-treesitter`):

```lua
local parser_config = require('nvim-treesitter.parsers').get_parser_configs()
parser_config.revl = {
  install_info = { url = '/path/to/tree-sitter-revl', files = { 'src/parser.c', 'src/scanner.c' } },
  filetype = 'rvl',
}
```

Copy `queries/highlights.scm` into the host's query path for revl.

## Conformance gate

The corpus honesty check is the conformance gate against the reference parser.
It parses every `examples/*.rvl`, `examples/rejections/*.rvl`, and
`selfhost/*.rvl` file in the parent revl checkout and **fails if any
non-exempt file produces an `ERROR` (or `MISSING`) node**:

```sh
node check.mjs          # or: npm run check
```

Current result: **173 / 187 files parse with zero `ERROR` nodes**, with **14
named exemptions** — the gate is **GREEN** (zero unexpected `ERROR`s). Every
file the **reference parser accepts** parses clean here; every exemption is a
file the reference itself rejects **at parse time** with a construct that is
genuinely absent from revl's context-free syntax.

`tree-sitter test` additionally runs the structural fixtures in
[`test/corpus/`](test/corpus).

### Exemptions

An exemption is legitimate only when the **reference parser itself rejects the
file at parse time** with a construct that is genuinely absent from revl's
context-free syntax. No file is silently skipped — `check.mjs` parses and
reports every one.

| File | Reason |
| --- | --- |
| `examples/rejections/t19_union_type.rvl` | `type Payload = List[Row] \| Str` — revl has **no union types**; `\|` separates the *cases* of a variant (constructor names), not type applications. The grammar accepts variant cases (`Name(payload)`) but not a `\|`-separated list of type applications, so it produces an `ERROR` here — matching the reference's own refusal. |
| `examples/rejections/v2_provide_emission_fn.rvl` | a provide-method carries **no purity modifier** — it is a plain `fn`, emission-ness is inherited from the service (G4). The reference rejects `emission fn` inside `provide` at parse time, so `provide_method` (plain `fn`) errors here too. |
| `examples/rejections/lifecycle_no_swap.rvl` | revl has **no `swap` statement**; a hot-swap is driven by the harness re-admitting an edited source against the running system, not by an in-language statement. The reference rejects `swap A -> B` at parse time, so the grammar carries no `swap` node and the bare word is an `ERROR`. |
| `examples/rejections/foreign_def.rvl` | `def` — revl functions are `fn`. |
| `examples/rejections/foreign_lambda.rvl` | `lambda` — revl closures are `=>` arrows. |
| `examples/rejections/foreign_elif.rvl` | `elif` — revl uses `else if`. |
| `examples/rejections/foreign_for_in.rvl` | `for x in xs` — revl iterates with `for (x of xs)`. |
| `examples/rejections/foreign_cstyle_for.rvl` | C-style `for (init; cond; step)` — not a revl loop form. |
| `examples/rejections/foreign_increment.rvl` | `++` — revl has no increment operator. |
| `examples/rejections/foreign_kwargs.rvl` | `f(name=v)` — revl has no keyword arguments. |
| `examples/rejections/foreign_python_ternary.rvl` | `a if c else b` — revl's ternary is `c ? a : b`. |
| `examples/rejections/foreign_slice.rvl` | `xs[a:b]` — revl has no slice syntax. |
| `examples/rejections/foreign_string_dict.rvl` | `{"k": v}` — revl records use identifier keys. |
| `examples/rejections/foreign_tuple.rvl` | `(a, b)` — revl has no tuples. |

The eleven `foreign_*` files are constructs from other languages that the
reference parser refuses **by name at parse time** (item 384,
`_reject_foreign_keyword` and the shape guards). They are genuinely-foreign
*syntax*, not context-sensitive semantic rejections, so an LR grammar has no
rule to match them and its `ERROR` mirrors the reference's own parse-time
refusal.

### A note on the reference's other parse-time refusals

The reference parser rejects several other corpus files at parse time for
**context-sensitive** reasons an LR grammar does not enforce — they are
syntactically well-formed, so this grammar parses them cleanly (with zero
`ERROR` nodes), which is the correct behavior for a syntax highlighter
(highlight the code even when it is semantically invalid). These include:

- `a1_await_in_method` — `await` outside a component body (position rule).
- `a1_effect_await_block` — `effect await { … }` with a statement body (an
  async-colour rule; `effect await <expr>` is accepted).
- `g4_missing_undo` — `effect` without `undo` (G4 pairing rule).
- `g6_impure_statement` — a bare expression statement in a component body (G6).
- `lifecycle_stmt_in_pure_test` — `load` in a plain (non-`lifecycle`) `test`.
- `v2_dynamic_realm` — `realm(config.x)` with a non-literal label.
- `v2_extern_unclassified` — `extern fn` with no `pure`/`acquire`/`emission`.
- `v2_fail_in_pure_fn` — `fail` inside a pure `fn` (activation-only transition).
- `v2_nullish_mixed_with_or` — `a ?? b || c` without parentheses.
- `v2_optional_chain_nonoptional` — `a?.b.c` (a plain access after `?.`).
