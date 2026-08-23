# Findings — agent/record-update

Two ergonomics gaps that cost the selfhost porting agents real cycles:
functional record update `{r | f = e}` and block-bodied match arms.
Spec: docs/records.md.

## 1. Refusal log

- `{ p | x = p.x + 1 }` (pre-implementation, from shadow2's log, quoted in
  the task): refused with a generic "expected an expression" — verdict:
  **gap**. The language could not express a copy-with-one-field-changed;
  agents fell back to 6+ lines of let-chaining or helper families. This is
  exactly what this branch fixes.
- Block arm `=> { let y = g(b)  y + 1 }`: now parses and typechecks but
  lowering refuses with "no backend emits them yet" — verdict: **gap,
  partially closed**. The refusal names the deferral and the workaround
  (lift into a helper fn), which is at least actionable; before this branch
  it was a syntax error with no hint.
- Self-inflicted, caught by my own tests: first cut of the python emitter
  only handled `record_update` in one of its *two* expression dispatchers
  (component renderer vs fn-body renderer). The fn-body path raised
  "unsupported expression kind 'record_update'". Verdict: **caught-bug** —
  the execution probe in tests/test_record_update.py caught it immediately;
  revl's two-renderer design makes "did you patch both paths" a real trap
  and the test-first rule saved me.
- Non-raising oracle hazard: my first checker draft raised record-type
  errors even when `infer_ast` was called without a filename (match
  exhaustiveness probing), violating the oracle's never-raises contract.
  Caught on review of `_expr_static_type`'s docstring — would have been a
  nasty intermittent crash. Verdict: **caught-bug** (by reading, not tests —
  worth a fuzz test someday).

## 2. Friction log

- [slow] Emitters live under `backends/`, not `src/revl/` — grepped
  `src/` for the TS emitter for several minutes before finding them.
- [slow] Several emitters have *two* expression dispatchers (component vs
  v3/fn paths) plus wasm has three; nothing marks which kinds must be
  handled where. A shared "kinds handled per dispatcher" conformance table
  would prevent silent divergence.
- [nit] `Parser` takes source text, not tokens, despite `lex()` being
  public; cost one probe iteration.
- [slow] No statement terminators between `let`s inside blocks means
  block-arm syntax reads `{ let y = e  tail }` — double space as separator.
  Consistent with existing fn bodies but visually ambiguous; flagged in
  docs/records.md §4.
- [nit] Full suite takes ~45s+; no `-x` fast loop documented for frontend-
  only changes (`pytest tests/test_frontend.py tests/test_doc_examples.py`
  covers most frontend work).

## 3. What revl gave you

- The bidirectional checker's split (`infer_ast` non-raising oracle vs
  raising mode) forced me to think about who consumes inference results —
  that discipline is what surfaced the oracle-contract bug above before it
  shipped.
- The doc-fence gate (test_doc_examples.py) swept up my new docs/records.md
  automatically: the `revl` example had to compile and the fragment had to
  parse. Zero extra wiring; the spec cannot rot silently.
- Additive IR + frozen reference invariant worked as advertised: ir_version
  stayed 3, all golden/reference tests passed untouched after adding a new
  node kind — the byte-stability guarantee did exactly the job it claims.

## 4. Time-to-green

Compile→refuse→fix cycles: ~6 (2 parser ambiguity iterations, 1 oracle
contract fix, 1 second-dispatcher fix, 1 duplicate-record-branch cleanup,
1 wasm message naming the wrong tier). Longest stall: discovering the dual
expression dispatchers in backends/python/emit.py (~15 min); a per-backend
map of "dispatcher → supported IR kinds" in each emit.py header would have
shortened it to zero.
