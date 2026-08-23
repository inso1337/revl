# Dogfood findings — agent/checker-shadow (selfhost/checker.rvl spike)

Scope: port the expression-typing slice of src/revl/typecheck.py to revl
(literal typing, binop operand/result rules for + - * / % and comparisons,
assoc-list type environment), differential-oracle against `infer_ast`
per tests/test_selfhost_parser.py's pattern.

## 1. Refusal log

1. Snippet: `assert infer_expr_str("s + \"t\"") == "Str"` inside a
   selfhost/checker.rvl `test` block.
   Diagnostic: `RevlError: selfhost/checker.rvl:195: unexpected character '\\'`
   Verdict: **friction**. The refusal is correct — revl plain strings carry
   no escapes (lexer.rvl's header says so, but the diagnostic doesn't). The
   message names the offending character yet never hints "revl strings have
   no escape sequences; concatenate or use code points". I lost minutes
   re-reading my own quoting before remembering the lexer comment I had
   read an hour earlier. A one-clause hint would close this entirely.

No other compile refusals in the entire slice: checker.rvl compiled clean
on its second attempt and first real cycle. Notably accepted without fuss:
`IntLit(_)` wildcard payload patterns, exhaustive match with a `_` catch-all
arm, plain recursion (`lookup_at`), cross-module ADT import + construction +
pattern-matching, `var`/`while` not needed anywhere (recursion sufficed).

## 2. Friction log

- [gap→workaround] `selfhost/parser.rvl` declares `type Expr` module-private,
  so the checker cannot import the AST it exists to consume. Cross-module
  ADT *reuse* requires `pub` on every reused declaration, and nothing in
  docs/syntax-2.0.md, docs/selfhost-findings.md, or the module tests says how
  `use { Type }` interacts with ADT *constructors*. I had to probe
  empirically (/tmp scratch compile): importing the pub type DOES bring its
  constructors into scope as match patterns, and payload field access works
  even when the payload record type (`BinN` etc.) stays private. That is
  excellent semantics, discovered by experiment — document it. Workaround:
  one-word change, `type Expr` -> `pub type Expr` in parser.rvl (behavior-
  preserving; all parser tests stay green).
- [slow] The no-escapes string model (see refusal 1) plus `.concat()` chains:
  building diagnostics reads like `("a".concat(b)).concat("c")`. An f-template
  is available (`` `x${op}y` ``) and works, but the interpolation-inside-
  template lexer limitation (parser.rvl's known bug) makes me avoid nesting
  them, so I fell back to concat anyway.
- [nit] No float-literal AST node exists (`Expr` has IntLit/StrLit/BoolLit/
  NullLit only), so a checker slice cannot see `1.5` as a Float literal —
  Floats enter only via the environment. This constrains the *next* checker
  slice more than it constrained the parser.
- [nit] Int-literal i64 range refusal (`_reject_int_literal_range`) is
  unportable in this slice: the AST stores literal text as Str, and parsing
  arbitrary-width digit strings needs big-int text comparison revl's Int
  can't do without hand-rolling. Deferred deliberately; corpus keeps literals
  in range. A `Str`->digits compare helper in stdlib would unblock it.
- [nit] Environment as assoc list meant hand-rolled recursive lookup
  (`lookup_at`). Fine at this size; a real port wants the Map value type,
  or at least tuple destructuring in match patterns over pairs.

## 3. What revl gave you

- The differential oracle itself is only possible because compiled revl is
  just callable Python: compile -> emit -> exec, then call `infer_expr_str`
  720+ times against the reference. No process spawn, no FFI.
- Refusal-as-value forced a cleaner API than the reference's dual-mode
  (filename=None vs filename=given): one function, three outcomes
  ("(bad)"/"refuse"/type-or-"?"), no exceptions to thread through.
- Exhaustive match over the imported ADT caught the shape of the job early:
  writing the `_ => ok_ty("")` arm made "outside the slice" an explicit,
  greppable decision instead of silent fall-through.
- The type system earned nothing dramatic this time — the slice is small and
  I ported from a ground truth — but nothing let me silently confuse
  Infer/Bind/Expr records across module boundaries either.

## 4. Time-to-green

Compile->refuse->fix cycles: **2** (one lexer refusal above; one pre-flight
edit adding `pub` to parser.rvl which was anticipated, not refused).
Longest single stall: ~10 minutes, the escaped-quote refusal, and it was a
memory problem (remembering the no-escape rule) rather than a decoding
problem. What would have shortened it: the hint text suggested in section 1,
or any doc statement of the string-escape rule outside lexer.rvl's header.
Oracle result: 88 pytest cases (44 accepted + 26 refused + 12 fuzz seeds x 60
expressions = 720 fuzz expressions) agreeing on verdict AND inferred type;
full suite 1367 passed, 68 skipped.
