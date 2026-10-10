# Roadmap archive: Done (dependency order as built)

Closed items moved verbatim from [docs/v2.0-roadmap.md](../v2.0-roadmap.md). The tools read this file from its "## " heading on.

## Done (dependency order as built)

1. ✅ **Lexer** — multi-char operators, 2.0 keywords, template literals.
2. ✅ **Types & data (§2)** — records, generics `[]`, enums, ADTs, built-ins,
   `T?` sugar.
3. ✅ **Functions & expressions (§3.1–§3.5)** — `fn`/`pub fn`, TS-subset
   expression layer, local mutation (`var`/`while`/`for-of`, non-escaping),
   `==`/`===` equivalence, §3.3 exclusion diagnostics.
4. ✅ **`match` (§3.3)** — exhaustiveness-checked ADT eliminator.
5. ✅ **Modules (§1)** — `use`/`pub`, module-private default, import-cycle check.
6. ✅ **Services 2.0 (§5)** — `async fn`, `commutative`.
7. ✅ **Components 2.0 (§4)** — block-effect form, `fail` (L-Raise).
8. ✅ **Host blocks (§6)** — classified `extern … = @ts {} = @py {}`, G8 audit
   surfaces externs/host code transitively.
9. ✅ **`verified` & `test` (§7)** — `revl test` runner, totality tier.
10. ✅ **Migration (§9)** — template literals; legacy `$` forms rejected with a
    `revl fmt --migrate` hint.
11. ✅ **Stdlib surface** — docs/stdlib-2.0.md (`length`/`push`/`slice`/
    `charAt`/`charCodeAt`/`indexOf`/`concat`; persistent semantics; unknown
    method = compile error). Turing-completeness demonstrated by execution.
12. ✅ **Sound typing & null safety** (`76d2961`) — bidirectional checking in
    `src/revl/typecheck.py`: sound where declared, silent where unknown.
    Service/fn call arguments and returns, provide-method signatures from the
    service (A6), `var` type stability, `Bool` conditions, record fields, ADT
    payloads. `null` evicted from expressions — absence is `Opt[T]`
    (`T → Opt[T]` injection allowed; `Opt[T] ↛ T` with an unwrap hint).
    Documented gradual frontier: host-valued objects, single-letter type
    params, the trusted extern boundary (G8).

13. ✅ **ADT construction & tagged Result** (post-review) — `Opt` stays
    host-null/value (`Some(x)`=x, `None`=null; `match` on it is a null-check),
    while `Result` and user variants are **tagged**: construction lowers to an
    `adt` IR node and `match` discriminates by tag. User cases may shadow the
    built-in `Ok`/`Err` (the `type Outcome = Ok(Row) | …` idiom). Coverage:
    cordis-py + cordis (TS) full (executed); cordis-rs full via native
    `enum`/`Option`/`Result`; cordis4j full — user variants (sealed
    interfaces), `Opt` (`Optional`), and built-in `Result` via a generic
    sealed `RevlResult<T,E>` (verified on JDK 21); **wasm** now lowers
    variants/`Opt`/`Result` to a `[i32 tag][i32 payload]` cell in linear
    memory with tag-dispatch `match` (verified on wasmtime). All **five**
    tiers construct and match tagged values, each verified by executing
    emitted code.

14. ✅ **MCP bridge & agent-facing diagnostics** (docs/mcp-bridge.md) —
    `revl mcp serve` (compiler as an MCP server over stdio: `revl_check`,
    `revl_admit`, `revl_audit`, `revl_tools`, `revl_grammar`), `revl mcp
    schema` (services → tools with annotations **derived** from declaration
    *and* implementation, transitively through `fn` calls), `revl mcp import`
    (tools → revl; only an explicit `readOnlyHint: true` avoids `emission`,
    and generated sources compile), plus structured diagnostics
    (`src/revl/diagnostics.py`, `revl compile --json-diagnostics`): stable
    code, guarantee text, expected/actual, hint. 32 tests.

15. ✅ **syntax-2.0 acceptance benchmark** (§10) — two full 30×3 runs with
    DeepSeek V4 Pro, one against the typing-enforced checker and one against
    the pre-typing checker as a control (`bench/results/`, summarized in the
    README). Headline: **sound typing costs models nothing** — typed
    first-pass is equal-or-better within run-to-run variance. The gap the
    runs did expose was one grammar friction (models write the full
    provide-method signature, which the component grammar rejected), and
    that is fixed: optional parameter and return-type annotations are now
    accepted and checked against the service (A6).

16. ✅ **Arithmetic is specified rather than inherited**
    (**docs/arithmetic.md**). Every tier used to compute whatever its host
    computed, and because an IR `bin` node carried no operand type, no backend
    was in a position to do otherwise. Found by *executing* one source on
    every tier rather than checking that each emitter accepts it — the
    portability floor catches a tier that refuses, never one that accepts and
    then means something else.

    - **`/` is true division and yields `Float`**, even on two `Int`s. §0
      decides it: `/` is spelled as TypeScript spells it, and `7 / 2` is 3.5
      there. The checker had typed it `Int` while two tiers produced 3.5 — a
      soundness break, and rust was the tier out of step with the syntax.
    - **`%` is the truncated remainder** and pairs with `div_trunc`; `mod` is
      Euclidean and pairs with `div_euclid`. Both identities
      (`(a div b) * b + (a rem b) == a`) are published as a **law** and
      executed over sixteen sign combinations on every tier. `div_floor`
      deliberately has no remainder partner.
    - **`Int` is 64-bit two's complement and overflow traps.** Never wraps,
      never silently widens; every hosted tier raises `revl: Int overflow`.
      Two tiers needed ports to hold it: wasm moved i32→i64 (values move,
      linear-memory addresses stay i32, slots widened 4→8 bytes) and
      TypeScript moved `number`→`bigint`.
    - **`Float` is IEEE 754 binary64**, with literals (`1.5`, `1e10`,
      `1.5e-3`). Two consequences are recorded rather than glossed: `==` on
      Float is not reflexive (`NaN != NaN`, caveated in syntax-2.0 §3.4), and
      `<` is a partial order.
    - **Division by zero**: a literal zero divisor is a compile error
      (`examples/rejections/arith_zero_divisor.rvl`); a computed one faults
      uniformly. `/` by zero stays IEEE ±infinity, because it is Float.
    - **Structural `==`** was fixed on the way: TypeScript lowered it to JS
      `===` (identity for objects — `{a: 1} == {a: 1}` was *false*), rust
      lacked `PartialEq`, and Go could not compare slices at all.

    `tests/test_cross_tier_execution.py` executes the whole specification on
    every tier each run. What remains is items 11–13 under "Open".
