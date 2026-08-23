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
- **Components**: `requires` / `provides` clauses, `config` blocks, `effect …
  undo …` forms (including `effect { setup … }` blocks and `effect spawn C with
  { … }`), `emit … compensate …`, `fail`, `if` guards, `isolate … in
  realm(…)`, `intercept … with { … }`, and `provide` methods (`fn f(x) = expr`
  or `fn f(x) { … }`).
- **Types**: records `{ f: T }`, variants `A(T) | B`, aliases `type Rows =
  List[Row]`, generics `Map[Str, Int]`, optionals `T?`, function types `(Int,
  Str) -> Bool`, and `type Id[T] = …` parameters.
- **Functions**: `fn`, `pub fn`, `verified fn`, `fn id[T](…)` type parameters,
  and the full pure-expression stratum — ternary, `||` / `??` / `&&`,
  equality/comparison/arithmetic, `!` / unary `-`, calls, `.`/`?.` member
  access, indexing, records, lists, arrows (`x => …`, `(a: Int) => …`),
  `match`, string / template (`` `…${expr}…` ``) / number / boolean / `null`
  literals, and typed `hole` placeholders.
- **Externs & host blocks**: `extern pure|acquire|emission fn … = @backend { …
  }`. The `@backend { … }` body is brace-balanced host text, consumed verbatim
  by the external scanner (`src/scanner.c`), exactly as the reference lexer does.
- **Tests**: `test "…" { … }`, `lifecycle test "…" { load / unload / call /
  swap / assert … }`, and `fault test "…" for C { fail at … assert … }`.

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

Current result: **78 / 79 files parse with zero `ERROR` nodes**, with **1 named
exemption**.

`tree-sitter test` additionally runs the structural fixtures in
[`test/corpus/`](test/corpus).

### Exemptions

An exemption is legitimate only when the **reference parser itself rejects the
file at parse time** with a construct that a context-free LR grammar cannot
express. No file is silently skipped — `check.mjs` parses and reports every one.

| File | Reason |
| --- | --- |
| `examples/rejections/t19_union_type.rvl` | `type Payload = List[Row] \| Str` — revl has **no union types**; `\|` separates the *cases* of a variant (constructor names), not type applications. The reference parser rejects this at parse time. The grammar accepts variant cases (`Name(payload)`) but not a `\|`-separated list of type applications, so it produces an `ERROR` here — matching the reference's own refusal. |

### A note on the reference's other parse-time refusals

The reference parser rejects **eleven** corpus files at parse time. Ten of them
are refused for **context-sensitive** reasons an LR grammar does not enforce —
they are syntactically well-formed, so this grammar parses them cleanly (with
zero `ERROR` nodes), which is the correct behavior for a syntax highlighter
(highlight the code even when it is semantically invalid). These ten are:

- `a1_await_in_method` — `await` outside a component body (position rule).
- `g4_missing_undo` — `effect` without `undo` (G4 pairing rule).
- `g6_impure_statement` — a bare expression statement in a component body (G6).
- `lifecycle_no_swap` — a `swap` statement (the grammar accepts it as a
  lifecycle statement; the reference forbids it because G2 makes it meaningless).
- `lifecycle_stmt_in_pure_test` — `load` in a plain (non-`lifecycle`) `test`.
- `v2_dynamic_realm` — `realm(config.x)` with a non-literal label.
- `v2_extern_unclassified` — `extern fn` with no `pure`/`acquire`/`emission`.
- `v2_fail_in_pure_fn` — `fail` inside a pure `fn` (activation-only transition).
- `v2_nullish_mixed_with_or` — `a ?? b || c` without parentheses.
- `v2_optional_chain_nonoptional` — `a?.b.c` (a plain access after `?.`).

Only `t19_union_type` names a construct that is genuinely absent from revl's
context-free syntax, which is why it is the single exemption above.
