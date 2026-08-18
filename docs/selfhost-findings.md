# Self-hosting, stage two: what building a parser in revl actually cost

The self-hosted lexer (syntax-2.0 §11) proved the pure stratum runs. This
is the second stage — `selfhost/parser.rvl`, the pure-expression grammar
(§3.2) — and its purpose is **not** self-hosting. A self-hosted compiler
is a bootstrap liability that taxes the iteration speed which is currently
this project's best asset. The purpose is a **differential oracle**: two
independent implementations of one grammar, forced to agree on every
input. Neither is the spec, so a disagreement is always a real defect in
one of them.

Scope is deliberately the expression layer, because that is where
precedence, associativity and lookahead bugs live. Declarations are
linear and comparatively boring.

**Stop here.** The checker is where a `Map` becomes load-bearing and where
the work stops being grammar and starts being semantics — a different and
much more expensive kind of problem.

## The headline: the parser needed *less* than a Map

The question this exercise was set to answer was "if it needs more than a
`Map` to get there, that's your answer." It did not need one at all.
Token access is indexing into `List[Token]`; an AST is a tree, not a
symbol table. `Map` becomes unavoidable at the *checker* (scopes,
environments), and that is precisely the boundary this stage stops at.

## What carried the weight

Each of these was probed against the compiler before a line was written,
and each works — compiled *and* executed on the python backend:

- **Recursive ADTs.** `type Expr = … | Bin(BinN)` where `BinN` holds
  `Expr` fields, including forward references between the two
  declarations. This is the load-bearing capability; an AST is exactly
  this shape and nothing else would have substituted.
- **`List[T]` of ADT values inside record payloads** — call arguments,
  list literals, match arms.
- **Mutually recursive functions** across the whole precedence ladder.
- **Modules**: `pub type` plus `use "./lexer.rvl" { Token, lex_src }`.
- `if`/`else`, `while`, `var`, early `return` from inside a loop.
- The existing stdlib surface, unextended.

## Friction, ranked by cost per unit

1. **Bindings are function-scoped, not block-scoped.** Two sibling `if`
   branches cannot each write `let r = …`. In the TypeScript subset revl
   advertises, `let` *is* block-scoped, so this is legal TS that revl
   rejects. It rejects loudly rather than silently diverging, so §0 holds
   — but a parser is a stack of branches doing identically-shaped work,
   which concentrates the tax exactly where it hurts. Four renames were
   needed in `p_primary` and `p_unary` alone.
2. **A `var` may not appear in a record literal** (the non-escaping rule,
   §3.5). Correct, and well-diagnosed — but a parser threads a cursor
   through record constructors constantly, so it forced about ten `let`
   copies that exist only to hand a value to a constructor.
3. **An ADT payload is exactly one *named* type.** Neither
   `Add(Expr, Expr)` nor `Add({ left: Expr, right: Expr })` parses; the
   record must be declared first. Sixteen of the auxiliary type
   declarations in `parser.rvl` exist for no other reason.
4. **A bare `match` is not a function body.** `fn f(e: E) -> Int { match e
   { … } }` is rejected ("body never returns a value"); `return match …`
   is required, and the `fn f() = expr` short form is not available at
   top level (only in provide-methods). The diagnostic is good and the
   fix is obvious, but the ML-family idiom is the one models will reach
   for first.
5. **No `Map` value type.** Known, documented as Planned, and not needed
   here — see above.
6. **`push` is persistent.** Right for a parser, which only appends. It
   will be the performance question for a symbol table, not a correctness
   one.

None of these blocked the work. All of 1–4 are ergonomics, and 1 and 3
are the two a model would trip on.

## What the oracle found

Three real defects, all in `selfhost/lexer.rvl`, all invisible to the
existing corpus test — which is the point worth internalizing: **a
differential oracle only covers what its corpus exercises.**

1. **`hole` was missing from the self-hosted keyword set.** The reference
   had it; the revl lexer did not, so `hole[Int] "todo"` lexed as an
   identifier. No corpus file uses `hole`, so the two lexers agreed on
   every input they were ever asked about. Fixed, and the *class* closed:
   the keyword sets are now compared as sets.
2. **`${…}` accepted only a bare identifier.** The reference captures the
   body as raw brace-balanced source for the parser to re-parse, so
   `${a + b}`, `${r.count}` and `${ {a: 1} }` are ordinary revl that the
   self-hosted lexer rejected outright with an `error` token. Fixed, with
   the missing forms added as a case list.
3. **The template part encoding was ambiguous.** Parts are flattened into
   one `Str` joined by `"|"` — so `${a || b}`, or a template nested
   inside an interpolation, put the separator into a payload and made the
   parts unrecoverable. Found by the fuzz at roughly one occurrence in
   400 random expressions; invisible to every hand-written case. Payloads
   are now escaped (`%%` for `%`, `%p` for `|`) with a left-to-right
   decoder, mirrored in the lexer, the parser and the test's reference
   canonicalizer.

The oracle also caught two bugs in the new parser itself within minutes
of first running — a match-arm loop that tested for its separator *after*
consuming it, and an optional-call branch that fell through into the
optional-field branch. Both were the same shape (a missing `else`), both
would have been invisible to a single implementation, and both were found
by comparison rather than by inspection.

## How it is checked

`tests/test_selfhost_parser.py` renders the reference AST to the same
canonical S-expression the revl parser produces and compares:

- ~100 curated accepted forms and ~27 rejected ones;
- a seeded generator over the whole grammar, 12 seeds × 60 expressions in
  CI, run at 40,000 during development with zero disagreements after the
  encoding fix.

Errors are values on the revl side (the pure stratum has no exceptions),
so the reference *raising* and the parser returning `Bad` is the
agreement checked on rejection. Messages are not compared — accept/reject
and shape are.

## Known limit

`p_type` implements the common type forms (named, generic application,
`?` sugar, the parenthesised group and function form). Anything outside
that is a parse failure rather than a wrong tree, so a divergence would
surface as a rejection disagreement, not a silent one.
