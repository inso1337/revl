# findings-shadow3 — module-table typing in selfhost/checker.rvl

Branch `agent/checker-shadow3` off devwip @ 37e0f18 (the slice was already
uncommitted in the worktree; this agent finished it). Slice three of the
self-hosting effort: type-declaration parsing (`type P = { ... }` records,
`type S = A | B(Int)` variants, transparent aliases via
`type X = Y` / `type Rows = List[Row]` / `type MaybeRow = Row?` /
`type Handler = (Int) -> Str`), the type table (`build_types`) and the ADT
case table (`build_cases`), then expression inference over that table —
nullary case constructors as values, payload checking at case calls,
Some/Ok/Err parametric results, ExprField against records (opt-escape
refusal, `.length` on sized heads), all ported from `src/revl/lower.py`
(`_resolve_type_aliases`, `_validate_declared_types`, `_lower_type_decls`,
`_case_table`) and `src/revl/typecheck.py` (`infer_ast`'s ExprVar /
ExprField / ExprCall arms). Entry point: `infer_prog_expr(progSrc, exprSrc)`.
Oracle: `tests/test_selfhost_checker.py` slice-three section — accepted
program+expression pairs agree on inferred type, rejected agree on refuse,
plus a fuzz over a fixed pool of type-table programs.

The worktree arrived broken: `selfhost/checker.rvl` did not compile
(`'r_tbl' is already declared in this function`), so every selfhost-checker
test errored at collection. The slice was otherwise complete in intent;
this agent fixed the compile errors, then brought the new differential
surface to full agreement with the reference (which caught several real
divergences below), then extended the oracle.

## 1. Refusal log

1. **`` `r_tbl` is already declared in this function ``** (also `r_fs`,
   `r_cs`, `r_pl`) — the slice's `build_types` copied the mutable `tbl`
   into a `let` in every early-return branch of one function; lets are
   function-scoped, so sibling branches collide. Verdict: **friction** (the
   duplicate-let rule again; the hint "rename or reuse the existing binding"
   is exactly right but the fix is mechanical and repetitive). I first
   deleted the copies entirely (`return { tbl: tbl, ... }`) and hit:
2. **``var `tbl` cannot be used in a record literal — a `var` never escapes
   its function``** — the copies were load-bearing, not ceremony: the record
   literal needs the *value*, and a `var` cannot supply it. Verdict:
   **caught-bug**, the rule is sound; but the two refusals together mean the
   sanctioned shape is "one uniquely-named `let` copy per branch", which is
   pure boilerplate. Functional record update (`{r | name = x}`) or a
   copy-helper would retire it.
3. **`` `k` is already declared in this function ``** — the record-branch
   and variant-branch loops both used `var k`; sibling branches share the
   namespace. Verdict: **friction**, same rule, renamed the second to `k2`.
4. **`` `r_al` is already declared in this function ``** — my own alias-cycle
   check used the same copy name twice. Verdict: **caught-bug**, author
   careless, checker right.
5. **`expected = in type Row`** (selfhost parser, after fixing the grammar) —
   the slice's record grammar was `type P { ... }` (no `=`), but
   `parser.py`'s `type_decl` REQUIRES `type P = { ... }`. Verdict:
   **caught-bug by the oracle** — `type Row = { name: Str }` failed to parse
   on the selfhost side while the reference accepted it; the corpus caught
   it immediately. Fixed `p_type_decl` to require `=` before both record
   bodies and case lists, matching the reference.
6. **Reference refusals the selfhost did not make** (table-build surface,
   all caught by the differential oracle):
   - `type A = B; type B = A` (alias cycle) — reference refuses via
     `_resolve_type_aliases` stack detection; selfhost erased both aliases
     and silently accepted. Added an expansion pass over every alias target
     (depth-bounded, mirroring the reference's stack).
   - `type Bad = Opt` / `type Bad = List` / `type Bad = Map[Str]` /
     `type Bad = Result` (malformed alias target — a bare builtin generic)
     and `type R = { a: Opt }` (malformed field type) — reference refuses
     via `check_type_wellformed` / `_validate_declared_types`; selfhost
     accepted. Ported `ty_wellformed` (generic-head arity, recursive).
   Verdict: **caught-bug by the oracle** — the differential is working as
   designed; each of these was invisible to a self-only test.
7. **`type Row { name: Str }`** (no `=`) — after fixing the grammar both
   sides agree on refusal, but with different parse messages; the corpus
   keeps parse errors out (same policy as slices one and two).

## 2. Friction log

- `[blocker]` Duplicate-let + var-in-record-literal together: the sanctioned
  "copy a mutable value into a `let`" pattern collides with function-scoped
  `let`s whenever the copy must happen in more than one branch of one
  function. Every early-return table error needed a uniquely-named copy
  (`dup_ty_tbl`, `dup_fld_tbl`, `cyc_fld_tbl`, `rec_fs`, `dup_cs_tbl`,
  `cyc_cs_tbl`, `cs_pl`, `var_cs`, `fin_tbl`, ...) — ten names for one
  idea. A `let`-per-branch namespace or a `copy()` builtin would collapse
  this.
- `[slow]` The reference's `_case_table` iterates the type table in
  declaration order; the selfhost Map's `keys()` is sorted (the python emit
  renders `keys()` as `sorted(...)`). The slice claims order-independence
  because ambiguous names are *dropped* either way — true for the corpus
  shapes, and I verified the Some/None and Ok/Err shadowing corners agree.
  Not a bug, but the two implementations' orderings genuinely differ and the
  comment's "identical to the reference's" needs that caveat.
- `[slow]` `split_type` did not trim type-argument spellings: `Result[Int,
  Any]` split to args `["Int", " Any"]`, so `compatible("Int","Any")` was
  False against the reference's `.strip()`ed `_split_top_level`. One
  leading space silently flipped verdicts on `Ok(1) == Err(1)`. The oracle
  caught it; a canonical-type helper shared by both sides would have made
  it structurally impossible.
- `[slow]` The reference `compatible` handles `&&`/`||`/`??` and `!`
  (unary) and OptCall/OptField/Index/match arms; the selfhost `infer_t`
  binop surface is deliberately smaller (slice-one scope), so expressions
  like `Some(1) && true` diverge (reference: refuse; selfhost: `?`). Fenced
  out of the corpus and documented, same as the float-literal grammar gap
  from slice one — but it is a real latent divergence on the new Opt/Result
  heads the slice itself introduces.
- `[slow]` Alias-cycle detection is depth-bounded (d > 25 ⇒ "cycle") where
  the reference uses an explicit stack; a 26-deep *legitimate* alias chain
  would be a false refusal on the selfhost side. Corpus stays shallow;
  worth a note for slice 4 (generics) which will create longer chains.
- `[nit]` `Starts-with/trim` helpers: no `trim`/`strip` builtin in the
  emitted surface, so `is_fn_type` hand-rolls whitespace skipping. Minor.
- `[nit]` The `.rvl` fixture problem persists: an in-language test cannot
  embed `"` or newlines, so all multi-line type-decl fixtures live in the
  Python oracle (as in slice two).

## 3. What revl gave you

- **The differential oracle earned its keep immediately.** Three of the
  five fixes above (`= `-grammar, alias-cycle refusal, wellformedness
  refusal) were invisible to any self-only test; the reference-vs-selfhost
  comparison surfaced each in one run, with the exact divergent input.
  Porting a compiler phase without this harness would have shipped three
  silent accept-where-reference-refuses defects.
- **The var-in-record-literal rule forced the right shape.** Writing
  `return { tbl: tbl, ... }` with a mutable `tbl` was refused with a rule
  I actually agree with (the var cell would alias); the compiler's
  insistence on a value copy is what makes the returned table safe to
  carry downstream. The friction is purely in the *naming* of the copies.
- **Refusal-as-value kept agreement total.** `build_types` returns
  `{ tbl, err }` instead of raising; `infer_prog_expr` folds every refusal
  into one string, so the oracle compares byte-for-byte on accepted inputs
  and verdict-for-verdict on refusals — no exception-shaped holes.

## 4. Time-to-green

- Compile cycles before first clean `checker.rvl`: **6** (r_tbl×7 → var-in-
  record → k/k2 → r_al). All four were the same two rules (function-scoped
  lets, var-in-record-literal) applied at scale; the hints were accurate.
- Oracle bring-up cycles: **3** probe runs (first pass 43/69 agreed; then
  grammar `=`, None→Opt[Any], compatible port; second pass 67/69; then
  split_type trim + same-line decls; 69/69; final corpus hygiene sweep).
  Longest single stall: tracing `Ok(1) == Err(1)` — selfhost refused where
  the reference said `Bool`; root cause was the untrimmed `" Any"` arg in
  `split_type`, an invisible one-space defect that a `repr`-style dump of
  the split result exposed in seconds once I printed it.
- After green: full suite **1988 passed, 139 skipped** in ~100s.

## 5. Cost ledger

| item | cost |
|---|---|
| compile→refuse→fix cycles (checker.rvl) | 6 |
| differential bring-up probe runs | 3 |
| corpus hygiene sweep (moved 3 accepted→rejected) | 1 |
| full-suite runs | 1 background, 101s |
| net new code | +705 lines checker.rvl, +274 test lines |
