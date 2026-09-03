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

`revl fmt --migrate` (the §9 syntax migration) runs a DIFFERENT gate, and the
difference is the point. The canonical formatter must leave the IR
byte-identical. Migration is a deliberate semantic upgrade: since 2.0 makes a
bare `$` a literal (item 203), rewriting `"$name"` to `` `${name}` ``
legitimately changes the IR, so an IR delta is admitted with the reason
``migration rewrite (IR intentionally changed: legacy `$` string → template)``
(`formatter.ir_equivalent`, called with `token_preserving=False`). What still
bites migration is
compilation: a rewrite whose output no longer compiles is refused. The migrate
scanner only ever rewrites `$`-bearing strings and copies everything else
through verbatim, which is what makes an IR delta safe to admit here.

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

## The IR-equivalence admissibility rule

For each file `fmt` computes:

1. `ir_before = compile(original)`
2. `ir_after  = compile(formatted)`

and canonicalises each IR document (`json.dumps(..., sort_keys=True)`) to a
byte string. The file is **admitted** only when those byte strings are equal.
IR carries no source line numbers, so re-indentation — which shifts every
line — does not perturb it; a difference means the rewrite actually changed
what the compiler lowers.

If a file does not compile on its own, the gate degrades soundly rather than
guessing:

| Original | Formatted | Verdict |
|----------|-----------|---------|
| compiles | compiles, same IR | **admitted** — IR byte-identical (the headline proof) |
| compiles | compiles, different IR | **refused** for a canonical format (the IR changed). **Admitted** under `--migrate`, whose whole job is that rewrite |
| compiles | fails to compile | **refused** — rewrite broke compilation |
| fails (imports / rejection example) | — | formatter falls back to proving the **token stream** is byte-identical (the invariant a whitespace-only rewrite actually guarantees); refused if tokens differ |
| fails (1.x legacy `$`) | compiles | `--migrate` admits it as **newly admissible** |

### What the gate catches

Any rewrite that alters compiled meaning: a changed identifier or literal, a
regrouped expression, a dropped or reordered statement. The formatter is
designed never to do these, but the gate is what makes that a *proof* per file
rather than a claim — and it is exactly the check a future, more aggressive
formatting pass (or migration) must pass before it ships.

## How comments survive without touching the parser

The reference lexer discards comments and whitespace, and both it and the
selfhost parser/lexer are off-limits to this tool. The formatter therefore does
**not** reuse them for rendering: `src/revl/formatter.py` carries a small,
self-contained *trivia scanner* that mirrors the lexer's token boundaries but
additionally keeps comments, newlines and blank lines, and captures strings,
backtick templates and `@host { … }` blocks as opaque verbatim spans.

Reproducing those spans byte-for-byte, plus the fact that the only
whitespace the lexer treats as significant is the space separating two adjacent
word tokens, is what lets the formatter normalise everything else while the IR
gate confirms — for real — that nothing changed.

### Documented limitation

The formatter does not re-flow statements across lines, and it does not reformat
the interior of backtick templates or `@host` blocks (those are verbatim). Line
re-flowing would need statement-boundary information only the parser holds;
rather than edit the off-limits parser, that is left out by design.
