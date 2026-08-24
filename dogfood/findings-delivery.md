# Dogfood findings — delivery semantics (roadmap item 44)

Idempotency promoted to a checked IR property: `emission idempotent fn
put(…)`, OpenAPI evidence for PUT/DELETE (RFC 9110 §9.2.2), and the python
runtime's auto-retry right, bounded by the checked flag. Worktree
`/private/tmp/wave44-delivery`, branch `wave44-delivery`, base `devwip`
(8a20a63).

Inherited an interrupted agent's uncommitted work (11 files, ~179 insertions):
lexer/parser/lower/import_openapi/formatter + five emitters + python runtime
retry machinery, with **no tests**, a stale test broken by its own header
rewrite, and the selfhost keyword-parity gate red. Completed: tests, selfhost
mirror, conformance case, design doc, doc refresh.

## 1. Refusal log

#### R1 — doc-examples gate refused my design-doc snippet
Snippet refused (docs/delivery-semantics.md, first draft):
````markdown
```revl
  emission idempotent fn put_thing()
```
````
Gate diagnostic (abridged):
```
docs/delivery-semantics.md:48: this block is fenced as a complete revl
program, and it does not compile.
```
Verdict: **friction**. The refusal is correct — the block is a fragment, not
a program. But the fix was not to re-fence it `fragment`: the gate tries
fragments in five scaffolds (top level, component body, provide method,
function body, expression position), and a **method declaration with
modifiers parses in none of them** — modifiers are only legal inside a
`service`, which no scaffold provides. A fragment that is genuinely
"a service-method declaration" has no scaffold home; I had to present it as a
complete `service Probe { … }` program instead. The gate's HOWTO does not say
a modifier-bearing method decl is unrepresentable as a fragment.

#### R2 (deliberate-rejection experiment) — `idempotent` on a plain `fn`
Snippet refused (pinned in tests/test_delivery_semantics.py):
```revl
service Store {
  idempotent fn peek(key: Str) -> Str
}
```
Diagnostic verbatim:
```
<string>:1: `idempotent` describes how an emission is delivered, so it is only
meaningful on an `emission` operation
  write `emission idempotent fn ...`; a plain `fn` is not delivered, so there
  is nothing to re-deliver
```
Verdict: **caught-bug** (feature's own check, exercised deliberately). The
refusal is right and the hint names the fix — this is the "refuse rather than
silently drop the claim" behaviour the feature is supposed to have, and it
works first try.

## 2. Friction log

- `[slow]` Full-suite run 1 went red on `test_selfhosted_keyword_set_matches_reference`:
  adding `idempotent` to `src/revl/lexer.py` requires mirroring it in
  `selfhost/lexer.rvl`'s `keywords()`. The uncommitted diff touched neither,
  so the feature shipped with a red suite and no hint in the diff that a
  selfhost mirror exists. The failing test named the missing set exactly
  (`Extra items in the right set: 'idempotent'`), so the fix was one line —
  but the *existence* of the selfhost lexer was discovered by reading the
  test, not by the diff.
- `[nit]` The generated OpenAPI per-operation claim backtick-quotes the word
  (`// \`idempotent\` by RFC 9110 §9.2.2: …`), so my first assertion
  `"idempotent by RFC 9110 §9.2.2" in source` failed by one backtick.
- `[nit]` The generated header documentation legitimately contains the phrase
  "emission idempotent fn", so a blunt `"idempotent fn" not in source`
  assertion is wrong for POST/PATCH and a pure-weakened PUT — tests must key
  on the declaration line or the per-operation comment, not the bare phrase.
- `[slow]` Reading the doc-examples gate to understand the scaffold
  limitation (R1) took longer than the fix; the gate's HOWTO is thorough but
  silent on "modifier-bearing method decls have no fragment scaffold".
- `[nit]` Uncommitted work had zero tests and one stale assertion
  (`test_idempotent_is_not_reversible` asserted the old header text the diff
  itself rewrote). Determining completeness required a full diff read plus
  sanity runs; a half-committed feature with its own tests would have been
  self-documenting.
- `[nit]` `backends/wasm/emit.py` silently ignores the `idempotent` flag
  (consistent with how it ignores `commutative`); the conformance matrix
  reports the case `ok`. A tier that has no delivery (compile-only) is
  arguably fine to ignore it, but the "silent drop" vs "documented ignore"
  line is not stated anywhere.

## 3. What revl gave you

- **The selfhosting differential gate caught the missed keyword.** Exactly the
  `hole` precedent: a corpus-only differential oracle misses keywords no
  corpus file uses; `test_selfhosted_keyword_set_matches_reference` compares
  the *sets*, and it flagged `idempotent` the moment the reference lexer
  gained it. My (inherited) oversight, caught by the type system's own
  dogfood — one full-suite run, zero manual inspection needed.
- **The doc-examples gate refused my non-compiling snippet in seconds** —
  the gate exists because doc snippets rotted silently; it caught mine before
  the commit.
- **The conformance matrix confirmed the new construct on all six tiers
  immediately** (`service/idempotent emission op  ok ok ok ok ok ok`) —
  cross-tier portability is a first-class check here, not a manual audit.
- **`ir_version` tiering did the bookkeeping.** Adding the property to
  `uses_v3` in lower.py made any document carrying it emit `ir_version: 3`
  automatically; no emitter had to be told.
- **The parser refusal named the fix** (R2): message + hint, no decoding.
- **`compile_source` round-trip of generated OpenAPI source** is the import
  family's second pillar; reusing it let me verify the delivery claim is a
  *checked IR property* after import, not just a comment.

## 4. Time-to-green

Compile→refuse→fix cycles: **4**.
1. full suite → selfhost keyword parity → 1-line fix in `selfhost/lexer.rvl`;
2. my test assertion (backtick) → 1 fix;
3. my test assertion (header phrase) → 1 fix;
4. doc gate fragment → 1 restructure.

Longest single stall: the full-suite run itself (~2.5 min) plus reading the
doc-examples scaffold logic (~10 min). What would have shortened it: the
uncommitted work landing with its own tests and the selfhost mirror — both
red flags would then have been visible in the first targeted test run instead
of a 1951-test full sweep.

## 5. Cost ledger

Rough account of this session's spend (deepseek-v4-flash, single agent):

| item | count |
|---|---|
| tool calls (read/grep/glob/edit/write/bash) | ~45 |
| bash invocations | 11 |
| targeted pytest runs | 5 (fast, <15s) |
| full-suite pytest runs | 3 (one red on selfhost parity, two green) |
| conformance matrix runs | 3 |
| tokens (approx) | ~60k in, ~9k out |

No API retries or rate-limit stalls. The two full-suite runs were the
dominant wall-clock cost; everything else was minutes.

## Scope decisions (recorded per instructions)

- **py tier is the only tier that executes the retry right.** The other five
  emitters render the flag as a comment/metadata — the type system's promise
  is in the IR everywhere, the runtime consumption exists where a runtime
  executes. The reference implementation is `backends/python/runtime.py`
  (`TransientError` + `retry_idempotent`), documented as such.
- **No `verified idempotent` upgrade.** The roadmap names it (item 37's
  property tests, `f(f(x)) == f(x)` against a recording) as the natural
  second step; this item delivers the checked claim, stated as a claim.
- **Method-level only.** `idempotent` is a per-emission modifier, matching
  the roadmap's `emission idempotent fn put(…)` shape; no service-level or
  extern-level form.
- **Created `docs/delivery-semantics.md`** because four source files already
  referenced it in comments — a referenced-but-missing doc is a debt.
- **Added a conformance case** (`idempotent emission op`) and bumped the
  documented construct count 48→49 in docs/backend-ir-v3.md.
- **Test placement:** a dedicated `tests/test_delivery_semantics.py` for the
  item's four pins (IR mark, OpenAPI evidence, refusal, py-tier emission) plus
  runtime retry semantics; the stale OpenAPI test fixed in place.
