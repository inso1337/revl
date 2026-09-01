"""The revl surface grammar, small enough to carry in a prompt — two views.

`PROSE_GRAMMAR` is the short human-readable summary `revl_grammar` (the MCP
tool) has always returned: one component, one function, and a one-line list
of the guarantees that reject code. `PROMPT_GRAMMAR` is the roadmap item 346
artifact: a dense, complete EBNF-style grammar covering every construct in
docs/syntax-2.0.md (plus fault tests, docs/fault-tests.md) — meant to be
pinned verbatim into an authoring system prompt rather than read by a human.
Both live here, not in `mcp/server.py`, so `revl grammar --prompt` (a plain
CLI command) does not have to import the MCP session machinery to print a
string.

`PROMPT_GRAMMAR` is mirrored byte-for-byte at `docs/syntax-2.0.prompt.txt`
(the file `tests/test_grammar_prompt.py` guards against drift) — the file is
the reviewable artifact, this constant is what ships in the wheel (`docs/`
is not packaged, unlike `backends/`/`stdlib/`; see pyproject.toml).
"""

from __future__ import annotations

PROSE_GRAMMAR = """\
revl 2.0 — surface summary (full spec: docs/syntax-2.0.md)

service S { fn f(a: Str) -> Int          // checked operation
            emission fn g(a: Str) -> Int // crosses the boundary
            emission[db] fn p(a: Str)    // ... only through `db`
            async fn h() -> Str }

component C requires k: S provides j: T {
  config { field: Int = 3 }
  isolate k in realm("tenant")        // optional realm placement (prelude)
  let r = effect acquire() undo r.release()
  await Job.run("work")               // iteration boundary (divert point)
  emit k.g("x") compensate k.g("undo")
  fail "reason"                       // deliberate L-Raise
  provide j { fn m(a) = pure_fn(a) }
}

type Row = { id: Int, name: Str }      // record
type Outcome = Ok(Row) | NotFound      // ADT; match is exhaustive
pub fn f(xs: List[Row]) -> Int {       // pure stratum (TS-subset exprs)
  var n = 0
  for (x of xs) { if (x.id > 0) n += 1 }
  return n
}
extern pure fn sha(d: Bytes) -> Str = @ts { ... } = @py { ... }
test "name" { assert f([]) == 0 }

Rules that reject code: mutation needs `undo` or `emit` (G4); reads must be
declared (G1); no cycles or duplicate providers (G2/G3); teardown cannot
register effects (G5); expressions are pure (G6); `null` has no type —
absence is Opt[T]; declared types are checked at every boundary.
"""

PROMPT_GRAMMAR = """\
revl 2.0 — complete grammar (pin this verbatim; full prose: docs/syntax-2.0.md)

PRINCIPLE: same meaning -> same syntax (TypeScript, verbatim). Different
meaning (effects, inverses, provisions, boundaries) -> distinct revl syntax.

--- program ---
program   := use* decl*
use       := 'use' STRING ( '{' IDENT (',' IDENT)* '}' | 'as' IDENT )
decl      := ['pub'] (typedecl | fndecl | service | component | extern
                       | test | 'fault' test)

--- types (own syntax; capitalized names; [] generics) ---
typedecl  := 'type' IDENT ['[' IDENT (',' IDENT)* ']'] '=' (record | variant)
record    := '{' IDENT ':' type (',' IDENT ':' type)* '}'
variant   := IDENT ['(' type ')'] ('|' IDENT ['(' type ')'])*
type      := 'Str'|'Int'|'Float'|'Bool'|'Bytes'|'Unit'
             | 'List[' type ']' | 'Map[' type ',' type ']' | 'Opt[' type ']'
             | 'Result[' type ',' type ']' | type '?'        -- ?  ==  Opt[T]
             | IDENT ['[' type (',' type)* ']'] | '(' type* ')' '->' type
-- no null/undefined: absence is Opt[T]. Float is IEEE754 (NaN != NaN).

--- pure expressions & functions (stratum 1: TS subset, verbatim syntax) ---
fndecl    := ['verified'] 'fn' IDENT '(' (IDENT ':' type)* ')' ['->' type] block
block     := '{' stmt* '}'
stmt      := let | var | assign(var-only) | 'if' '(' expr ')' block ['else' block]
             | 'for' '(' IDENT 'of' expr ')' block | 'while' '(' expr ')' block
             | 'return' [expr] | 'break' | 'continue' | expr
let       := 'let' pattern '=' expr                  -- single-assignment
var       := 'var' IDENT '=' expr
pattern   := IDENT | '{' IDENT (',' IDENT)* '}' | '[' IDENT (',' '...' IDENT)? ']'
expr      := literal | IDENT | expr '.' IDENT | expr '?.' IDENT | expr '??' expr
             | expr '[' expr ']' | expr '(' expr* ')' | '[' expr* (',' '...' expr)* ']'
             | '{' IDENT ':' expr (',' IDENT ':' expr)* '}'
             | IDENT '=>' expr | '(' IDENT* ')' '=>' block
             | expr ('+'|'-'|'*'|'/'|'%') expr | expr ('=='|'!='|'<'|'<='|'>'|'>=') expr
             | expr ('&&'|'||') expr | '!' expr | expr '?' expr ':' expr
             | `template ${expr} text`  | 'match' expr '{' (pattern '=>' expr ',')+ '}'
             | 'hole' ['[' type ']'] [STRING]         -- typed obligation (docs/holes.md)
-- == and === are one operator (canonicalized to == in IR); same for != / !==.
-- EXCLUDED (named in diagnostics): class, new, this, function, import, export,
-- typeof, delete, try/catch, ++/--, compound-assign on non-var, async arrows
-- in pure code, generators, switch (use match), reference/mutable capture.

--- services (interfaces; upper-bound every provider, G4) ---
service   := ['pub'] 'service' IDENT '{' methodsig* '}'
methodsig := ['emission' ['[' IDENT (',' IDENT)* ']']] ['async'] ['commutative']
             'fn' IDENT '(' (IDENT ':' type)* ')' ['->' type]
-- bare `emission` = any boundary; emission[a,b] = only through capabilities a,b.
-- async fn: provide-method body may `await` a host promise (not a divert point).

--- components (stratum 3; unchanged core + 2.0 block forms) ---
component := 'component' IDENT ['requires' bind (',' bind)*]
             ['provides' bind (',' bind)*] '{' cbody* '}'
bind      := IDENT ':' IDENT
cbody     := config_block | acquire | 'await' expr | emit_stmt | fail_stmt
             | isolate | intercept | provide | let-plain | var | assign
config_block := 'config' '{' (IDENT ':' type ['=' literal])* '}'
acquire   := 'let' IDENT '=' 'effect' (expr | '{' stmt* expr '}'
             | 'spawn' IDENT ['with' record_literal])   -- spawn: dynamic instance
             ['undo' expr]                             -- G5: undo has no stmt position
emit_stmt := 'emit' expr ['compensate' expr]            -- also legal in expr position
fail_stmt := 'fail' STRING                              -- deliberate L-Raise (A8)
isolate   := 'isolate' IDENT 'in' 'realm(' STRING ')'
intercept := 'intercept' IDENT 'with' record_literal
provide   := 'provide' IDENT '{' ('fn' IDENT '(' IDENT* ')' ('=' expr | block))* '}'
-- a plain `let x = expr` (no `effect`) is legal only inside a provide method.
-- an activation body never takes a plain binding (G6: nothing there to revert).

--- host blocks (extern; typed boundary, opaque verbatim body) ---
extern    := 'extern' ('pure'|'acquire'|'emission') 'fn' IDENT '(' (IDENT ':' type)* ')'
             ['->' type] ['undo' expr] ['compensate' expr]
             ['config' '{' (IDENT ':' type ['=' literal])* '}']
             ('=' '@'('ts'|'py'|'rs'|'go'|'java'|'wasm') '{' <verbatim host code> '}')+
-- classification is mandatory: pure (no effect), acquire (needs undo over the
-- implicit `result` binding only), emission (may carry compensate).
-- config is resolved once at plug time, bound as `_revl_config` in the body.

--- tests ---
test      := ['lifecycle'] 'test' STRING '{' (lcstmt | stmt)* '}'
lcstmt    := 'load' IDENT ['with' '{' (IDENT ':' expr)* '}']
             | 'unload' IDENT
             | ['let' IDENT '='] 'call' IDENT '.' IDENT '(' expr* ')'
             | 'assert' ('no_residue' | expr)
faulttest := 'fault' 'test' STRING 'for' IDENT ['with' '{' (IDENT ':' literal)* '}']
             '{' ('fail' 'at' ('step' INT | 'effect' (IDENT|STRING))
                  | 'assert' ('failed'|'no' 'residue'|'no' 'emissions'
                              |'inverses' 'lifo'|'siblings' 'unaffected'))* '}'
-- exactly one `fail at`, at least one `assert`, in a fault test.

--- rejections (compile-time, not runtime surprises) ---
G1 undeclared read · G2 duplicate/cyclic provider key · G3 import/dependency
cycle · G4 a provider body exceeds its service's declared emission bound ·
G5 undo/compensate outside their slot · G6 an activation body mutates or
takes a plain binding; a closure captures by reference · G7 non-LIFO/non-
total inverse · G8 boundary audit surface must enumerate every crossing ·
A1 `await` as a divert point exists only in an activation body · A2 no
acquisition after `provide` · A3 host-name collision is renamed, not
silently shadowed · A8 `fail`/an effect fault reverts accumulated undos and
lands FAILED, newest-first.
"""

__all__ = ["PROSE_GRAMMAR", "PROMPT_GRAMMAR"]
