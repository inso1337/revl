---
id: P50
title: Pinning the Semantics a Six-Target Compiler Silently Inherits: String Units, Sized and Trapping Integers, Total Division, Iteration Order and Injective Mangling
theme: compiler
kind: experience
maturity: partial
status: Decisions, docs and per-tier probes are on main for strings, integers, division, map order and mangling; the cost figures are single prose measurements and there is no divergence ledger yet.
fused_with: P48, P49
---

## Pitch
When one IR feeds six host languages, every unspecified detail (what a string's length counts, what happens at integer overflow, how a map is iterated, whether two identifiers can collapse into one) is silently decided six different ways, and each time revl decided it once, wrote the choice down, and turned it into a probe that runs on every tier.

## Abstract
A compiler that emits Python, TypeScript, Go, Rust, Java and WebAssembly from one typed IR inherits, for every operation the specification leaves open, six host-language answers. We report on a series of decisions in revl that closed such gaps, in each case the same way: measure what each tier actually computes on a separating input, choose one meaning by an argument from the language's own rules, make the frontend mark the information the backends lacked, add an execution probe on every tier that can run it, and document the closure so it cannot regress. The cases are: the unit of a string (code points; `"😀".length()` was 1, 2, 4 or a rejected literal depending on the tier), `Int` as 64-bit with trapping overflow on all six tiers (a measured cost of about 9% on a dependent scalar chain, with an `Int32` type for kernels that are about 34% faster), division named by rounding rule with a Result-returning total form, canonical sorted-key map iteration, an implicit Int-to-Float coercion marked on the IR, and reserved-word mangling that was not injective (`lambda` and `lambda_` collided, so Python returned a different value than the other five tiers). We list the divergences found, which were not found by the specification but by execution, and the three ways the checks themselves were found to be switched off in CI. We are explicit about what is not closed: arbitrary-precision `Integer` is a proposal, WebAssembly refused Float at the time of this wave (it lowers Float as a native f64 now, issue #3295; its remaining value limits are `Map` and non-integer float rendering), and the cost figures come from single measurements stated in prose.

## Problem and claim
Cross-target agreement is usually argued from the emitters: if every backend accepts a construct, the construct is portable. revl's own errata say why that fails: "`tests/test_cross_tier.py` checks that every emitter *accepts* a construct, which catches a tier that refuses and cannot catch a tier that accepts and then means something else" (`docs/contract-errata.md:271-274`). The failures that mattered were silent: a value that differs, not a build that breaks.

The claim of this paper is a procedure and its history, not a theory. Cross-target divergence appears first in decisions nobody wrote down. Deciding each once, giving the backends the missing information through the IR (for example `operands` on arithmetic nodes, `"widen": "Float"` on coercion sites), and attaching a probe that executes on each tier converts a silent divergence into either agreement, a named refusal, or a failing test. The docs also give the cost of each decision where it was measured.

The scope is deliberately limited to what the tree shows. Several decisions are only partly landed, and some documentation is stale; both are noted below instead of smoothed over.

## Contributions
1. A decision write-up for five families, each with the measured pre-fix divergence table, the chosen meaning and the argument: string unit (`docs/strings.md:99-135`), integer width and overflow (`docs/arithmetic.md:213-245`), division and remainder conventions (`docs/arithmetic.md:31-82`), map iteration order (`docs/collections.md:85-129`), and identifier mangling (`docs/v2.0-roadmap.md:1584`).
2. The pattern "frontend marks, backends emit": `operands` on `bin` nodes for `/` and `%`, and `"widen": "Float"` on coercion sites, both additive with no `ir_version` bump (`docs/arithmetic.md:377-412`; operands defined at `:172` and `:355`).
3. A counterexample to cross-tier equivalence of a different kind: non-injective keyword mangling, where Python silently returned a different value than the other five tiers. The roadmap notes that an injectivity assertion failed on the pristine self-host tree; this is not reproduced in the tree.
4. Cost figures stated in the docs (single prose measurements, not reproduced in the tree): about 9% for 64-bit trapping `Int` on a scalar chain (0.98 to 1.07 ns/iter), and about 34% for an `Int32` vectorisable loop.
5. A baseline discipline for known divergences: pinned current behaviour that fails both when it worsens and when it starts passing (`tests/test_cross_tier_execution.py:740-745`, `:930-940`).
6. An honest ledger of what escaped: probes sitting behind an environment variable nothing sets, a skipped TypeScript column, a doc that called a defective tier "already correct".

## Related work
| Prior system or line of work | What it does | What revl does differently |
|---|---|---|
| WebAssembly core spec (deterministic semantics, one nondeterminism list) | Specifies one machine; implementations differ only in listed places | revl specifies a source language over six unrelated hosts, one of which is Wasm itself; it must also pin things the hosts disagree on |
| Java and JavaScript string-length pitfalls (UTF-16 units vs code points) | Known hazard; each language keeps its unit | One unit chosen for the whole language and pinned by probes on all six tiers (the rust, java and wasm probes run only under `REVL_CROSS_TIER_SLOW`), including a Wasm tier rewritten to decode UTF-8 |
| Rust overflow modes (debug panic, release wrap) | Mode-dependent behaviour | revl emits `checked_*` so the guarantee does not depend on the cargo profile (`docs/arithmetic.md:300-309`) |
| Swift and Zig checked arithmetic | Trapping integers in one language | The same guarantee across six hosts, two of which (Python, TypeScript BigInt) are arbitrary precision and must impose the bound |
| Go map randomisation vs Python 3.7 insertion order | Each host decides; programs observe it | Sorted-key order chosen so order is a function of content, matching the existing order-independent `==` (`docs/collections.md:92-96`) |
| Haxe, Kotlin Multiplatform (citation to be checked) | Multi-target languages, target-specific escape hatches | No claim here to compare in depth; their documented per-target differences are the foil |
| Boute, Euclidean division (TOPLAS 1992) (citation to be checked) | The mathematical case for Euclidean `mod` | Cited by revl's docs (`docs/arithmetic.md:76-82`); revl adds named rounding and the pairing law as a testable identity |
| Compiler differential testing (Csmith, McKeeman) (citation to be checked) | Finds divergences after the fact | This paper is about closing them by decision; differential fuzzing is the finding side |

## Evidence on main
| Claim | path:line or test | Status | Note |
|---|---|---|---|
| String unit is code points on all six tiers | `docs/strings.md:99-104`; `tests/test_cross_tier_execution.py:1927-2010` (`test_string_unit_is_code_points`); `tests/test_458_str_ordering.py` | shipped | Executed on py, ts, go by default; rust, java, wasm only under `REVL_CROSS_TIER_SLOW` |
| Pre-fix unit divergence table (py 1, ts 2, java 2, wasm 4, go and rust rejected the literal) | `docs/strings.md:37-47` | shipped (measurement in the docs) | The java column was read from the emitter, not executed ("no JDK here", `docs/strings.md:6-7`) |
| Unit choice argued from existing `split` behaviour and the syntax rule | `docs/strings.md:106-132` | shipped | |
| String ordering, `split("")`, index bounds also pinned | `tests/test_458_str_ordering.py:1-40`; `docs/contract-errata.md:659-694`; `tests/test_str_index_bounds_cross_tier.py` | shipped | A third fault (java `keys()` tie-break, described at `tests/test_458_str_ordering.py:40-46`; the errata range cited above does not mention it) was found while building the boundary fixture |
| `Int` is 64-bit two's complement and overflow traps, on all six tiers | `docs/arithmetic.md:213-245` table; `:278-310` | shipped | wasm traps with no message; "a property of the instruction set" |
| Cost of the trap about 9% | `docs/arithmetic.md:222-228` (0.98 to 1.07 ns/iter) | shipped (prose number) | Single dependent scalar chain; no harness in the tree reproduces it |
| `Int32` ships; vectorisable loop about 34% faster | `docs/arithmetic.md:414-441` | shipped (type), unreproduced (number) | `docs/int32-proposal.md:3` still says "proposed, not implemented": stale |
| Unary minus and `Int.MIN / -1` trap on every tier | `docs/arithmetic.md:262-277`; `docs/contract-errata.md:596-620`; `tests/test_int_min_edges_cross_tier.py`; `test_negation_of_int_min_traps` (`tests/test_cross_tier_execution.py:1644`), `test_div_int_min_traps` (`:1698`) | shipped | |
| TypeScript `Int` is BigInt, was f64 | `docs/contract-errata.md:596-601`; `tests/test_cross_tier_execution.py:1553` | shipped | `docs/arithmetic.md:620-623` still lists "TypeScript is still not 64-bit" un-struck under "Still open", which contradicts `:278-283`: stale bullet |
| Literal outside the range is refused by the checker | `docs/arithmetic.md:246-258`; `examples/rejections/t20_int_literal_range.rvl` | shipped | `Int.MIN` has no literal spelling |
| Division has names; `%` is truncated; `mod` Euclidean; pairing law over 16 sign combinations | `docs/arithmetic.md:31-82`; `tests/test_cross_tier_execution.py:1395-1425` (`PAIRING_LAW`, `test_division_remainder_pairing_law`) | shipped | |
| Total division forms return `Result[Int, Str]`; `Int.MIN / -1` answers `Err("revl: Int overflow")` | `docs/arithmetic.md:642-693`; `tests/test_cross_tier_execution.py:1309` (`CHECKED_DIVISION`), `:1719` | shipped | Executed on py, ts, go, wasm by default; rust, java slow |
| Integer division by zero faults on all six tiers; literal zero divisor refused | `docs/arithmetic.md:591-616`; `examples/rejections/arith_zero_divisor.rvl` | shipped | TypeScript used to return `Infinity` |
| Float is IEEE binary64; `==` not reflexive; `<` partial | `docs/arithmetic.md:178-211` | shipped | Not a divergence, a caveat the docs state |
| Int-to-Float coercion marked on the IR, explicit conversion per backend | `docs/arithmetic.md:377-412`; `tests/test_cross_tier_execution.py:687` (`test_widening_is_marked_in_the_ir`) | shipped | Before: Rust E0308, TypeScript wrong answer (`3n === 3` false) |
| Map iteration: sorted by Unicode scalar, shipped on five tiers, wasm refuses maps | `docs/collections.md:85-129`; `tests/test_cross_tier_execution.py:214` (probe entry "map iteration is canonical order", not a test function); `tests/test_458_str_ordering.py:285-290` | shipped | `docs/collections.md:151-160` and `docs/stdlib-2.0.md:271` still say the TCK case is "pending"; stale |
| TCK clause for map order exists | `tck/spec.py:271,369-380` (`c1_map_iteration_order`) | shipped | Whether any adapter currently reports it live was not checked |
| Reserved-word mangling made injective on five tiers | `docs/v2.0-roadmap.md:1584`; `backends/python/emit.py:440-470`; `backends/python/tests/test_reserved_word_idents.py:121-170`; `tests/test_cross_tier_execution.py:312-335` | shipped | The roadmap's cited line numbers (`emit.py:278-289`) are out of date; the rule now lives at `emit.py:446` |
| Injectivity ported to five self-host emitters; the roadmap notes an injectivity assertion failing on the pristine tree | `docs/v2.0-roadmap.md:1687` | shipped | The pristine-tree check is described in the roadmap; no script in the tree reproduces it |
| A residual namespaced-key collision is refused by name | `tests/test_selfhost_emit_rust.py:1379-1395` | shipped | Rust shown; same pattern in py/go/java tests |
| Known divergences are pinned, failing on drift either way | `tests/test_cross_tier_execution.py:740-745,757,930-940` | shipped | |
| `Integer` (arbitrary precision) | `docs/integer-proposal.md:3` "proposed, not implemented"; `docs/contract-errata.md:891-901` | designed | Wasm bignum "concentrated cost" |
| Probes were not running in CI | `docs/contract-errata.md:276-300` | shipped (finding) | `REVL_CROSS_TIER_SLOW` is set in `.github/workflows/ci.yml:2292` now |

## Numbers that exist
- `Int` overflow check: 0.98 to 1.07 ns/iter on a dependent scalar chain, "about 9%" (`docs/arithmetic.md:222-228`). One measurement, no n, no machine stated.
- `Int32` vectorisable loop "~34% faster than i64" (`docs/arithmetic.md:414-420`). Stated twice (also `docs/int32-proposal.md:15`), no harness or raw values.
- String probe: `"😀"` is 1 code point, 2 UTF-16 units, 4 UTF-8 bytes (`docs/strings.md:29-31`); the pre-fix per-tier values are a six-cell table (`docs/strings.md:37-47`).
- Pairing law: 8 (trunc/rem) plus 8 (euclid/mod) sign-combination assertions per tier (`tests/test_cross_tier_execution.py:1409-1418`).
- Mangling: the roadmap names five fixed emitters and five self-host emitters (`docs/v2.0-roadmap.md:1584,1687`).
- No count of how many silent divergences the decisions closed in total exists in one place. The "Semantic divergences (closed...)" section (`docs/contract-errata.md:203` onward) lists them individually.

## Paper outline
1. Introduction: the emoji-length table as the opening example; why accept-only checks pass.
2. Method: measure, choose, mark in the IR, probe, document. The `DIVERGENCES` baseline discipline.
3. Five decisions in turn: strings (unit and ordering and bounds), integers (width, trap, literal range, `Int32`, `Int.MIN` edges), division and remainder (names, pairing law, total forms), iteration order, mangling injectivity.
4. Costs: the two measurements and what they do and do not show; the Wasm tier's refusals as a cost in coverage.
5. How the checks failed: the env var nothing set, the skipped TypeScript column, the "already correct" Java claim.
6. Related work and lessons.

## Threats to validity and limits
- Costs are single measurements stated in prose. The docs themselves say the check "is much worse" on vectorisable reductions and give no figure for that case (`docs/arithmetic.md:225-228`).
- One language, one team: the decisions are argued from revl's own rule that "syntax revl shares with TypeScript means what TypeScript means" (`docs/arithmetic.md:10-12`). A reader may reject that rule; the code-point choice in particular departs from the JS prior and is justified by internal consistency with `split`.
- Wasm was an incomplete participant at the time of the wave. It refused Float values, maps, `Int / Int`, string ordering and `repeat` by name (`docs/arithmetic.md:582-590`, `docs/collections.md:131-150`, `tests/test_458_str_ordering.py:262`). Cross-tier agreement on those was vacuous for Wasm then. Since issue #3295 it lowers Float and `Int / Int` as a native f64 value (a JavaScript browser host for the coeffect seam also lives in `backends/wasm/host/`); maps, string ordering and `repeat` stay named refusals, and non-integer float rendering still traps at runtime there (`docs/strings.md:340-376`).
- Java columns are often read from emitter source; execution of rust and java sits behind `REVL_CROSS_TIER_SLOW`, which `docs/contract-errata.md:276-300` notes was unset in CI until later fixed.
- `NaN != NaN` makes IEEE `==` non-reflexive, which the docs call "a caveat on the spec's wording", and it produces a Java HashMap/ArrayList anomaly described in `docs/contract-errata.md:241-262`. The pinned semantics are therefore not an equivalence relation on Float.
- Arbitrary-precision `Integer` is design-only; Float-to-string for exponent forms on Wasm is fenced; none of this paper claims otherwise.
- The agreement theorem models only three axes (numeric default, string unit, map order) and states what is outside it; this paper does not claim it covers the full set of decisions.

## Fact-check
Claims in this draft were checked against revl main at commit 45bec431f: 28 checked, 6 corrected.
