# `revl fmt` — a formatter that proves itself

`revl fmt <files>` produces a **canonical formatting** of revl source. Because
agents write most revl and humans review the diffs, diff noise is a cost paid
on every generation; a canonical form removes it.

What makes `fmt` more than a whitespace tool is its admissibility rule:

> Format the source, compile **both** the original and the formatted text, and
> assert their IR is **byte-identical**. A formatting is admitted only when it
> provably changed nothing the compiler can see. If the IR differs, `fmt`
> **refuses** that file (nonzero exit, names the file) rather than emit a
> formatting that changed meaning.

The same gate is retrofitted onto `revl fmt --migrate` (the §9 syntax
migration): a rewrite ships iff the resulting IR is unchanged — or, for a 1.x
input the current compiler cannot even parse, iff the rewritten output is newly
admissible. It is the standing rule for every future syntax migration.

## Usage

```
revl fmt <files>            # format in place (writes only admitted changes)
revl fmt --check <files>    # write nothing; exit nonzero if any file is not canonical
revl fmt -o out.rvl <file>  # write the result elsewhere (single input)
revl fmt --migrate <files>  # §9: rewrite 1.x "$name" interpolation to `${name}` templates
```

## The canonical style

- **Indentation** is two spaces per `{`/`(`/`[` nesting level. A line that
  begins with a closing bracket dedents to its enclosing level.
- **One statement per line stays on its line.** The formatter is
  *line-preserving*: it re-indents and normalises horizontal spacing but never
  moves a token onto a different logical line. Continuation lines (a wrapped
  `undo`, `compensate`, …) are re-indented to their block, not visually aligned
  under the effect above them.
- **Horizontal spacing** is normalised to a single space between tokens:
  - tight around `.` / `?.`, and no space before `,` `:` `;` `)` `]`;
  - no space between a call/index and its bracket — `set(1, 2)`, `Opt[Str]`,
    `emission[db]` — while a keyword keeps its space (`return (x)`, `if (c)`);
  - one space after `,` and `:`; spaces around binary operators and `->`.
- **Blank lines** collapse: at most one blank line between constructs, no
  leading or trailing blank lines, exactly one final newline.
- Comments are preserved (a trailing comment keeps two spaces before `//`; a
  standalone comment is indented at the current depth).

The formatting is a pure, deterministic function of the token stream, so it is
**idempotent**: `fmt(fmt(x)) == fmt(x)`.

## The admissibility rule

Every file `fmt` would write passes two checks. The first runs on every file,
whatever it contains; the second adds the compiler's view wherever a compilable
baseline exists.

### 1. Reference-lexer token identity (always)

`fmt` lexes the original and the formatted text with the compiler's own lexer
(`revl.lexer.lex`) and compares the resulting `(kind, value)` streams. Line
numbers are excluded — the formatter collapses blank lines, and nothing
downstream of the parser reads a line number — and everything else is compared
exactly. Two sources with the same stream parse identically and therefore
compile identically, so this is a proof, not a heuristic.

It is also fail-closed at every step: an original that does not lex, an output
that does not lex, or two streams that differ are each a **refusal**.

This check replaced one that compared the formatter's *own* trivia scanner
against itself (issue 309). That comparison could not fail: a scanner bug
corrupts both sides identically, so it passed and the corrupted rewrite
shipped. The lexer is the only authority on what a token stream is, so the
lexer is what gets asked.

### 2. IR equivalence (wherever a baseline exists)

For each file `fmt` also computes:

1. `ir_before = compile(original)`
2. `ir_after  = compile(formatted)`

and canonicalises each IR document (`json.dumps(..., sort_keys=True)`) to a
byte string. IR carries no source line numbers, so re-indentation — which
shifts every line — does not perturb it; a difference means the rewrite
reached something the compiler lowers.

When the file is on disk, both compiles go through `compile_files` with the
text supplied in memory, so `use` imports **resolve** and the formatted
candidate is compiled in place, in its own directory, without being written.
A `use`-bearing file used to skip this arm entirely (`compile_source` refuses a
`use` for want of a module directory), which is what let issue 309's corrupted
rewrites through.

| Original | Formatted | Verdict |
|----------|-----------|---------|
| any | tokens differ, or either side does not lex | **refused** — before anything else is tried |
| compiles | compiles, same IR | **admitted** — IR byte-identical (the headline proof) |
| compiles | compiles, different IR | **refused** — IR changed |
| compiles | fails to compile | **refused** — rewrite broke compilation |
| does not compile (rejection example, unresolvable import) | — | **admitted** on the lexer token identity proven above |
| fails (1.x legacy `$`) | compiles | `--migrate` admits it as **newly admissible** |

If the IR differs while the token streams are identical, `fmt` first re-checks
its own oracle by compiling the original a second time. If those two compiles
disagree with each other, the IR comparison says nothing about the rewrite; the
formatting is admitted on the token proof and a **warning** names the skipped
check. A weakened check is never silent.

### What the gate catches

Any rewrite that alters compiled meaning: a changed identifier or literal, a
regrouped expression, a dropped or reordered statement, a mis-scanned literal.
The formatter is designed never to do these, but the gate is what makes that a
*proof* per file rather than a claim — and it is exactly the check a future,
more aggressive formatting pass (or migration) must pass before it ships.

## How comments survive without touching the parser

The reference lexer discards comments and whitespace, so
`src/revl/formatter.py` carries a *trivia scanner* that adds comments, newlines
and blank lines to the lexer's token boundaries and captures strings, backtick
templates and `@host { … }` blocks as opaque verbatim spans.

It does not re-derive those boundaries. It calls the lexer's own `_lex_number`
(so `0xFF` and `1_000` are one literal each), `_lex_string` /
`_lex_triple_string` (so `'…'`, `\"` and `"""…"""` are one span each) and
`_match_brace` under the per-backend trivia tables (so a `}` inside a host
string or comment does not end the body), and it imports the lexer's operator,
symbol and keyword tables rather than copying them. A second copy of the
lexer's rules is what drifted in issue 309, and every point of drift was a
silent meaning change.

Reproducing those spans byte-for-byte, plus the fact that the only
whitespace the lexer treats as significant is the space separating two adjacent
word tokens, is what lets the formatter normalise everything else while the
gate confirms — for real, per file — that nothing changed.

### Documented limitation

The formatter does not re-flow statements across lines, and it does not reformat
the interior of backtick templates or `@host` blocks (those are verbatim). Line
re-flowing would need statement-boundary information only the parser holds;
rather than edit the off-limits parser, that is left out by design.
