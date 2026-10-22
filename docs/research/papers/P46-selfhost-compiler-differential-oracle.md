---
id: P46
title: A Compiler in Its Own Language as a Byte-Exact Differential Oracle
theme: compiler
kind: full
maturity: partial
status: A native source-to-source chain exists on main and is byte-exact on enumerated corpora (401 of 403 documents by the residual ledger); the self-host type checker is not the reference checker, there is no bootstrap fixed point, and the defect census and mutation analysis are still needed.
fused_with:
---

## Pitch
Writing the compiler a second time in the language it compiles, holding all six emitters to the reference's bytes and the front end to its accept/reject and canonical-shape results, turned the second implementation into a bug finder on its own side (the reference side yielded one reported smell and two replicated latent divergences), and produced a native chain that compiles revl source to target source with no reference in it, byte-exact on 401 of 403 corpus documents.

## Abstract
A self-hosted compiler is usually sold as a bootstrap milestone. We use one differently: as a differential oracle. revl is a language for agent compositions with a Python reference compiler. We wrote a second implementation in revl itself: lexer, parser, expression checker, a lowering pass that doubles as an admission gate, a type layer, and emitters for six targets (Python, TypeScript, Go, Java, Rust, WebAssembly text). The six emitters are each held to the reference's bytes; the front end is held to the reference's accept/reject and canonical shape. A driver composes the stages into one co-compiled artifact that goes from source text to target source with the reference absent from the chain, which separates the emitters (byte-exact on the reference's IR for every document of every tier's corpus, 403 documents by corpus lists) from the native IR producer (the only remaining gap: two documents, one ts and one rust, per a pinned ledger). A paired measurement, emitter fed the reference IR versus the whole native chain, locates every divergence in one half; an earlier snapshot shows 255 of 255 versus 216 of 255 before the producer was widened. We report defects the oracle found (a missing keyword and a template encoding ambiguity found by fuzzing, both on the self-host side). We also report a secret-redaction port missing from the self-host emitters that every green oracle missed, because no corpus document used it, and that a source-level blind-spot check found. And the limits: the oracle only covers what its corpus exercises, the self-host gate does not implement several guarantees, and the self-hosted compiler is itself compiled by the reference, so there is no bootstrap fixed point.

## Problem and claim
A compiler with one implementation has no oracle other than its own tests. Differential testing across independent implementations is cheap when they exist, but building a second compiler is expensive and rarely done. The self-hosting literature treats the second compiler as a bootstrap goal and measures speed or independence from a host language.

The claim of this paper is narrower and checkable. Treat the self-hosted compiler as the second implementation and hold the emitters to byte equality with the reference on all six emitted tiers, and the front end to the reference's accept/reject and canonical shape. Then (1) disagreements are defects in one side, with evidence from this project; (2) a paired measurement (emitter on reference IR, versus the fully native chain) tells which half is wrong, which a single end-to-end number cannot; (3) the end-to-end chain exists with no reference in it for the covered surface.

The paper does not claim that the self-hosted compiler is a complete compiler (`docs/selfhost-findings.md:1-19` says the purpose was the oracle, "not self-hosting"), that it is the checker the language relies on (`README.md:268-281` names the implementation as the live checker), or that agreement implies correctness: "a differential oracle only covers what its corpus exercises" (`docs/selfhost-findings.md:81-84`).

## Contributions
1. A self-hosted front end and six emitters in `selfhost/*.rvl` (12 files: lexer, parser, checker, lower, types, compile, emit_{py,ts,go,java,rust,wasm}), about 75.5k lines in total by `wc -l` (75,495), with the emitters held to the reference's bytes.
2. A composed driver, `compile_to_at(source, tier, path)`, co-compiled in one artifact: admit, lower to IR, parse JSON, emit. Unsupported cases return named refusals (`REFUSED|LINK|...`) rather than output that differs from the reference (`docs/selfhost-compile.md:25-64`).
3. The paired measurement: each document's emitter output on the reference IR versus through the whole native chain, asserted per document and recomputed every run, with the residual pinned in a ledger (`LOWER_GAP_DOCS`) (`tests/test_selfhost_compile.py:874-920`).
4. A catalogue of defects the oracle found, and a defect class the byte-agreement oracles missed (secret redaction missing at both ends), which a source-level blind-spot check found, and a coverage gate built in response (`docs/selfhost-findings.md:81-112`, `docs/selfhost-findings.md:1466-1530`; `tests/test_selfhost_coverage.py:1-20`).
5. A measured account of what a byte oracle for WebAssembly text costs: scalar corner first, then string data, list and product-type allocation, tagged cells (`docs/selfhost-findings.md:137-165`, `docs/selfhost-findings.md:701`, `docs/selfhost-findings.md:1289`, `docs/selfhost-findings.md:1347`).
6. A published list of what the self-host gate does not decide (G5, G7, G9, A2, A8, A9, G-RETAIN, G-SECRET-FLOW unimplemented; G1, G2, G3, G6 with divergences noted) (`docs/conformance.md:223-271`).

## Related work
| Prior system or line of work | What it does | What revl does differently |
|---|---|---|
| Thompson, Reflections on Trusting Trust (1984) (citation to be checked) | Shows a compiler can carry a self-propagating backdoor | Not a security result here. The second implementation is used for divergence detection, not trust |
| Wheeler, Diverse Double-Compiling (2005) (citation to be checked) | Uses an independent compiler to check a compiler binary corresponds to its source | revl's second compiler is checked at emitted-source level across six targets, not as a binary-to-source check; no DDC fixed point is claimed |
| McKeeman, Differential Testing for Software (1998) (citation to be checked) | Compare independent implementations on generated inputs | Same principle; here the second implementation is in the language itself, with hand-written plus generated corpora and a located-gap ledger |
| Bootstrapping in Rust, GHC, OCaml (citation to be checked) | The compiler is built by an earlier version of itself | revl's self-host is not the production compiler; the reference is Python and stays the authority |
| CakeML bootstrapping (Kumar et al., 2014; see the [related-work table](../related-work.md)) | Verified compiler that bootstraps inside a theorem prover | Not verified. revl has an adjacent Lean layer for its admission judgment (`README.md:268-281`) that is a different artifact |
| Csmith (Yang et al., 2011) (citation to be checked), EMI | Random program generation to find compiler bugs across compilers | revl uses small seeded grammar fuzzers for the parser and checker, not a program generator over the whole language |
| PyPy / RPython | Interpreter written in a restricted Python language and translated | RPython is the production path; revl's self-host is a parallel oracle |

A literature review is not done in this draft. The novelty claim rests on the "paired measurement locates the fault" and "oracle-found defect catalogue" parts, and needs checking against compiler-testing surveys.

## Evidence on main
| Claim | path:line or test | Status | Note |
|---|---|---|---|
| Twelve self-host source files | `selfhost/*.rvl` (lexer 822, parser 1339, checker 4176, lower 36635, types 859, compile 2788, emit_go 5401, emit_java 3993, emit_py 5028, emit_rust 6969, emit_ts 4900, emit_wasm 2585 lines) | shipped | `wc -l` |
| Native chain, no reference in it, byte-exact vs reference | `docs/selfhost-compile.md:313-366`; `tests/test_selfhost_compile.py:281` (`test_native_compile_is_byte_identical_with_no_reference`) | shipped | Parametrized headline over one function document per tier, not the corpus. Corpus-wide native agreement is the row below (test line 874, part b) |
| Six emitters co-compile in one artifact | `tests/test_selfhost_compile.py:1854` (`test_six_way_composition_co_compiles`) | shipped | |
| Emitter byte-exact on reference IR for all corpus documents; residual is in lower.rvl | `tests/test_selfhost_compile.py:874` (`test_the_residual_is_located_in_lower_not_in_the_emitter`) | shipped | Recomputed each run |
| Residual ledger: ts 1 doc, rust 1 doc, others none | `tests/test_selfhost_compile.py:492`; `docs/selfhost-findings.md:1770-1808` | shipped | Generated blocks in the doc |
| Corpus sizes py 106, ts 82, go 78, java 61, rust 54, wasm 22 | `tests/test_selfhost_emit_py.py`, `tests/test_selfhost_emit_ts.py`, `tests/test_selfhost_emit_go.py`, `tests/test_selfhost_emit_java.py`, `tests/test_selfhost_emit_rust.py`, `tests/test_selfhost_emit_wasm.py` (`CORPUS` lists) | shipped | Counted by parsing list literals, not by running |
| Parser and checker differential vs reference incl. fuzz | `tests/test_selfhost_parser.py`; `tests/test_selfhost_checker.py:1-30`; `docs/selfhost-findings.md:113-127` | shipped | Expression layer only. Compared as accept/reject and canonical S-expression shape, not bytes; messages are not compared |
| Conformance column: 59 of 61 constructs byte-identical through the native chain (py tier) | `docs/conformance.md:100-110`, `docs/conformance.md:148-162`; `README.md:283-286` | shipped | Only `emit_py_src` is used for this column |
| Self-host gate is not the reference gate | `docs/conformance.md:223-271`; `README.md:288-293`; `tools/gate_reference_census.py:1-25` | shipped (as a documented gap) | |
| Oracle blind-spot gates | `tests/test_selfhost_coverage.py:1-20`; `tests/test_selfhost_line_coverage.py:1-25` | shipped | Analysis belongs in P47 |
| wasm emitter widened past the scalar corner | `docs/selfhost-findings.md:701`, `docs/selfhost-findings.md:1289`, `docs/selfhost-findings.md:1347` | shipped | Non-ASCII string literals out (`docs/selfhost-findings.md:733`) |
| emit_rust.rvl emitted to rust builds and runs, matching reference | `docs/selfhost-compile.md:384-480` (open status at 403-405; later blockers closed at 409+); `tests/test_selfhost_emit_rust.py:978` (`test_the_rust_emitter_builds_as_rust`, skips without cordis-rs) | partial | compile.rvl itself cannot be emitted to rust (needs `@rs` body for `py_repr`, doc line 476) |
| Bootstrap fixed point (compiler compiles itself and output compiles itself) | none | not present | Fixture `compile_rvl` loads `selfhost/compile.rvl` through the reference (`tests/test_selfhost_compile.py:122-127`) |

The wasm emitter has been widened past the scalar corner (`docs/selfhost-findings.md:701`, `docs/selfhost-findings.md:1289`, `docs/selfhost-findings.md:1347`; the scalar-only text is the first slice, `docs/selfhost-findings.md:137-165`). "Six-tier byte-exact self-compile" needs the qualifier that two documents (one ts, one rust) still fall outside the fully-native chain (`tests/test_selfhost_compile.py:492`).

## Numbers that exist
- Corpus documents per tier: py 106, ts 82, go 78, java 61, rust 54, wasm 22 (sum 403). Source: CORPUS lists in `tests/test_selfhost_emit_*.py`, counted by parsing the files, not by running tests.
- Fully-native residual on main: 2 documents (one ts document with block-arm values, one rust document with method control flow; both named in the ledger), so 401 of 403 by the ledger. Source: `LOWER_GAP_DOCS`, `tests/test_selfhost_compile.py:492` and the generated block at `docs/selfhost-findings.md:1790-1808`. The ratio is derived here; the docs deliberately print no counts ("the counts are not stored here").
- Historical snapshot (commit 77f1d7f6, stated in the test comment): corpus 255, emitter on reference IR 255 (100%), fully-native 216 (84.7%) (`tests/test_selfhost_compile.py:345-363`). Useful as a before picture; not current.
- Earlier ts measurement: of the 32 documents then measured, 15 reproduced through the native chain and 17 did not, none an emitter defect (`docs/selfhost-findings.md:1486-1500`).
- Conformance matrix: 59 ok, 2 limit, 0 gap of 61 constructs for the self-host column (`docs/conformance.md:100-110`).
- Parser/lexer check: about 100 accepted and 27 rejected curated forms; fuzz 12 seeds x 60 expressions in CI, run at 40,000 in development with zero disagreements after the fix (`docs/selfhost-findings.md:113-127`).
- Fuzz yield: the template-encoding defect appeared at roughly 1 in 400 random expressions (`docs/selfhost-findings.md:100-102`).
- Line coverage claim in the docstring: 2868 of 8233 unexecuted reference statements sit in functions the corpus calls; the docstring separately reports 53% of statements blind versus 20% by constructs (`tests/test_selfhost_line_coverage.py:4-11`). 2868/8233 is 35%, so the 53% is a different figure from the same docstring and must not be read as a restatement of the 2868 count. Both are the test's own text, not recomputed.
- Test definitions: lexer 8, parser 3, checker 19, lower 118, types 13, compile 55 (`def test` matches, not parametrized cases).
- No timings, no compile-speed comparison, no runtime performance for the native chain. None yet. The docs mention a known O(n^2) `revl_push` clone-on-append in the rust-hosted lexer (`docs/selfhost-compile.md:379-380`).

## Paper outline
1. Introduction: second implementation as oracle, not as milestone.
2. The reference compiler and the interchange IR: what is compared (bytes at stage boundaries).
3. The self-host: stages, the language features it needed (recursive ADTs, no Map for the parser, friction table).
4. The native driver and its refusal discipline (named refusals instead of divergent output).
5. The paired measurement and the ledger that ratchets it.
6. What the oracle found; the secret-redaction miss and the coverage gates.
7. Cost of a byte oracle per target, with WebAssembly as the worst case.
8. Limits: gate not equal to the reference gate, corpus dependence, no fixed point, no performance.
9. Related work.

## Threats to validity and limits
- "A differential oracle only covers what its corpus exercises" (`docs/selfhost-findings.md:81-84`). The secret-redaction hole sat under every green oracle, and it was found by a source-level check rather than by any oracle's output (`docs/selfhost-findings.md:1466-1530`; `tests/test_selfhost_coverage.py:1-12`). Two implementations that share a misreading agree wrongly; that is P47's topic.
- Independence is partial: both implementations are written by the same team, against the same IR, with the self-host ported from the reference (the wasm preamble is "a fixed second implementation of the exact bytes", embedded verbatim, `docs/selfhost-findings.md:163-170`).
- Every fixed defect in the findings document is on the self-host side. The reference side has one reported smell, not fixed (`backends/java/emit.py:2536`, `docs/selfhost-findings.md:791`) and two latent divergences the port carries verbatim (`docs/selfhost-findings.md:1020`). The claim that disagreement finds bugs on both sides needs a census of all fixes citing an oracle disagreement, which has not been done.
- Byte equality is measured on emitted source, not on behaviour. Runtime agreement is a separate matter (the rust path adds a build-and-run test; others are not covered here).
- The self-host gate "is not yet the reference gate" and "does not implement" several checks (`README.md:288-293`).
- The 59/61 conformance number uses the py tier only (`docs/conformance.md:152-156`).
- Two native-chain residual documents remain; corpora are enumerated lists chosen by the project, not a random sample of programs.
- The self-hosted compiler is compiled by the reference, so this is not a bootstrap in the Wheeler or Thompson sense. The default recursion limit stops the lowering pass from admitting every self-host file (`docs/selfhost-compile.md:250-253`), so a stage-2 experiment is not yet possible.
- No single-commit rerun of the headline table, no mutation analysis of the oracle itself and no baseline on the effort cost of a second implementation exist yet.
- Static counts come from reading files and parsing literals; the suite was not run for this draft, so "green" is the repository's claim.

## Fact-check
Claims in this draft were checked against revl main at commit 45bec431f: about 60 checked, 9 corrected.
