# findings-shadow2 — service-boundary checking in selfhost/checker.rvl

Branch `agent/checker-shadow2` off devwip @ 66bc616. Slice two of the
self-hosting effort: method signatures on service declarations, call-site
argument checks against those declarations, and the checkable core of G4's
upper-bound rule, ported from `src/revl/lower.py`
(`_component_req_call`, `_method_emissions`, `_emitting_capabilities`) with
the reference diagnostic text as ground truth. Oracle:
`tests/test_selfhost_checker.py` slice-two section — accepted programs agree
on accept, refusals agree on verdict **and** exact message (including three
checked-in `examples/rejections/` fixtures compiled verbatim), plus a fuzz
over random call-site argument lists.

**First-class dispatch is fenced out, explicitly:** the fixed point here is
the NAME-CALL subset of `_emitting_capabilities`. `calls_in` collects callee
positions only, never value positions, so commit 1a24197's `*` capability
(first-class fn values propagating may-emit) has no counterpart in this
port. A provider that hands an emitting callable to a dispatcher is invisible
to this slice. This is stated in the checker's scope comment too.

A second deliberate non-port, discovered *by the oracle*: the reference
checks neither arity nor argument types of plain-fn calls at provision call
sites (only required-service receivers and builtins get signature checks) —
my first draft checked fn arity there (t10-style) and would have merged a
false positive. The reference refused nothing on `scale(v, v)` inside a
provide body, so `fn_call` collects G4 evidence only.

## 1. Refusal log

Every `revl compile` rejection hit while writing slice two:

1. **`unexpected character '\'`** — I wrote `db.query(\"x\")` inside a revl
   string. Verdict: **caught-bug** (revl strings carry no escapes, by design
   and documented). Residual **friction**: a revl-side test therefore cannot
   embed a double quote or a newline in a source fixture, which is exactly
   what a parser/checker test wants to do; all multi-line fixtures moved to
   the Python oracle.
2. **`expected ident, found 'emission'` / `'requires'` / `'provides'`**
   (type declarations) — record fields may not be spelled with keywords.
   Verdict: **friction**. "Expected ident" gives no hint the word is
   reserved; three fields (`isEm`, `reqs`, `provs`) renamed across
   construction sites one error at a time.
3. **`expected an expression, found ';'`** — C-style `{ stmt; stmt }`.
   Verdict: **friction**: the lexer tokenizes `;` and the parser rejects it
   with no hint that statements are newline-separated.
4. **``var `k` cannot be used in a record literal``** (six hits: `k`, `em`,
   `caps`, `j`, `kd`, `svcs`). The rule is sound (the var cell would alias)
   and the message prescribes the fix ("copy its current value into a `let`
   first"). Verdict: **caught-bug**, but the mechanical result was six
   compile→refuse→fix rounds until I extracted constructor helpers
   (`mk_step`, `mk_ev2`, `mk_ck2`, `mk_ctx`, …) so every construction from
   mutable state goes through a value-taking function. That helper family is
   pure ceremony that functional record update (`{r | name = x}`) would
   retire.
5. **``argument 1 of `int_str(...)` expects `Int`, got `Float```** — my own
   `int_str(n / 10)`; `/` is TRUE division even on two Ints, exactly as
   slice one specifies. The checker caught its author. Verdict:
   **caught-bug**, the dream — quote: ``argument 1 of `int_str(...)`
   expects `Int`, got `Float```. Fixed with `div_trunc`.
6. **`` `u` is already declared in this function `` (also `r`, `s`)** —
   lets are function-scoped, not block-scoped; two `let u = …` in sibling
   if-branches collide. Verdict: **friction** — nothing I read said scopes
   are function-wide, and every language I port from has block scoping.
7. **Match arms cannot carry statement blocks** (`None => { return … }`
   parsed as a record literal whose first field name is `return`). Verdict:
   **gap** — arms are expressions only. Combined with refusal-as-value this
   forces awkward shapes wherever an arm wants to bail early: `has()`
   pre-check plus a second `lookup`, or default-record shims.
8. **`` `quote_join` is not declared in this function ``** — my own doing:
   an editor patch replaced the function instead of appending after it.
   Checker right, author careless. Verdict: **caught-bug**.

## 2. Friction log

- `[blocker]` No escapes in plain strings ⇒ a `.rvl` file cannot embed `"`,
  `\n`, or `${` in fixture sources. A raw-string form would unlock
  in-language parser tests.
- `[slow]` Keywords are unusable as record field names, and the domain
  vocabulary IS keywords here (`emission`, `requires`, `provides`). The
  checker's AST now reads `isEm`/`reqs`/`provs` — worse prose than the
  grammar it mirrors.
- `[slow]` Match arms are expression-only; early-bail arms need
  restructuring (see refusal 7).
- `[slow]` Function-scoped `let`s: branch-local names leak; long functions
  accumulate synthetic suffixes (`r2`, `rf`, `u2`, `s1..s4`).
- `[slow]` Var-in-record-literal rule ⇒ constructor-helper ceremony
  (`mk_*`, nine helpers and counting).
- `[slow]` Anonymous record types are rejected in type position
  (`List[{name: Str, ...}]`) — reasonable, but then the payload types of
  pub ADTs must be exported one by one; I published `ParamN`, `InitN`,
  `ArmN`, `PartN` from parser.rvl to name them downstream.
- `[slow]` Failure channels propagate silently: `params_at` returns
  `ok=false`; my caller ignored it and the real off-by-two (call it at the
  `(`, not after it) surfaced much later as "bad method signature in service
  Database", far from the cause. An unused-result warning for records with
  an `ok` field would localize such breaks instantly.
- `[nit]` `;` is lexed but meaningless. Newline-separated statements also
  mean multi-line statements are unrecoverable for any line-based layer;
  my statement splitter inherits that limitation (documented in-code).
- `[gap]` No `Int -> Str` anywhere in the stdlib. Diagnostics need counts
  (`takes 2 argument(s)`); hand-rolled `int_str` via `div_trunc` + a digit
  table.
- `[gap]` No `chr`/`fromCharCode`: control characters cannot be synthesized,
  compounding the fixture problem above.
- `[gap]` (pre-existing, slice one) the selfhost *expression* grammar has no
  float-literal case: the lexer emits `float` tokens the parser never
  consumes, so a `2.5` argument poisons its whole statement into `Bad`.
  Fuzz literals exclude floats for that reason; fixing means a new `Expr`
  variant rippling through every exhaustive match, so fenced, not fixed.
- `[nit]` PYTHONPATH juggling per command; venv outside the worktree.

## 3. What revl gave you

- **The arithmetic rule caught its own porter.** `int_str(n / 10)` was
  refused at typing with exact operand types — the same `/`-means-Float
  guarantee slice one implements caught me using it for integer digits.
- **Exhaustive matching as a porting harness.** `calls_in` and `walk_expr`
  enumerate every `Expr` variant with no catch-all; when the parser grows a
  variant (say `FloatLit`), both walks fail to compile instead of silently
  skipping the new node — the property a differential checker most needs.
- **Map finally pays.** The four symbol tables (services, fns, requirement
  keys, capability sets) are `Map[Str, V]` values, including record-valued
  maps. Persistent `set` made the least-fixed-point loop trivially safe:
  rebind and iterate, no aliasing ghosts. What is STILL missing for
  symbol-table work: **iteration** (keys/values/entries — the fixed point
  must walk the fn list and probe the map per name), **size**, and
  **remove**. None blocked this slice; all three will block a real module
  table.
- **refusal-as-value kept the port honest.** With no exceptions, agreement
  is total: every input yields a string, so the oracle compares messages
  byte-for-byte on refusals too — something an exception-based port would
  be tempted to weaken to "both raised".

## 4. Time-to-green

- Cycles to first clean compile of checker.rvl: **~20** (escape bug; three
  keyword-field renames × two sites each; `;`×4; anonymous record types;
  match-arm blocks ×2; true division; var-in-record ×6 across three passes;
  duplicate lets ×3; an accidental helper deletion).
- Longest single stall: the **var-in-record-literal** series — four
  point-fixes before conceding the pattern needed the constructor-helper
  refactor. The error does name the rule ("a `var` never escapes its
  function"), but a §3.5 doc example showing the sanctioned shape
  (copy-to-let vs helper) would have skipped three cycles.
- Second stall: **float literals** in fuzz — selfhost returned "" where the
  reference refused; tracing took a parse-level debug session to find the
  missing `float` case in parser.rvl. One assert that no non-`skip`
  statement contains `Bad` would have surfaced it in one run.
- After first clean compile: oracle bring-up took 3 more cycles (ADT
  payloads bind directly, not as records — twice; the requires/provides
  header scan swallowing the next clause). Final suite: **1412 passed,
  68 skipped** (~1370 baseline + slice-two corpus/fuzz/internal tests).

## Verdict summary

| # | snippet | verdict |
|---|---------|---------|
| 1 | `"...\"x\"..."` | friction (rule right, fixtures impossible in-language) |
| 2 | `type T = { emission: Bool }` | friction |
| 3 | `{ a = 1; b = 2 }` | friction |
| 4 | var fields in record literal | caught-bug (ceremony cost noted) |
| 5 | `int_str(n / 10)` | caught-bug — the headline |
| 6 | two `let u` in sibling branches | friction |
| 7 | `None => { return x }` | gap |
| 8 | deleted helper | caught-bug |

