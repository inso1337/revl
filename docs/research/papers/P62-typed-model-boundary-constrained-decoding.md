---
id: P62
title: "One Type, Three Consumers: A Typed Model Boundary Compiled to Schema, Decode Grammar and Retry Budget"
theme: agents
kind: full
maturity: partial
status: Derivation, seam, two dialects and the retry budget ship on the Python tier with structural tests and a small provider probe; retry reduction is not measured and the call-site generic form is not built.
fused_with: P63
---

## Pitch
One declared return type yields the JSON schema, the decoding grammar and the validator, with one shared null-admission predicate and a retry budget that multiplies a static cardinality bound, so the prompt, the decoder and the checker come from one declaration; whether they stay in step is tested by invariants, not proven.

## Abstract
Structured-output libraries and provider modes ask a model for JSON of a given shape and check the answer, but the schema sent to the model, the grammar given to the decoder and the validator that accepts the answer are usually three artifacts maintained separately. We describe a typed model boundary in revl, a language with effects at declared boundary crossings. An emission declared `validated` returns an algebraic data type; the response is validated on the revl side whatever the provider does (shipped on the python tier), a malformed response is a typed, retryable fault, and the retry budget is declared (`retry N`) and multiplies the crossing's static cardinality bound by N+1 rather than appearing as an unbounded loop. From the same type the compiler derives a JSON schema and a GBNF decoding grammar, and a second wire-schema dialect for providers whose structured output is schema-shaped; the dialect is negotiated at the seam. A provider that claims to have constrained a decode is held to the artifact it claimed, and an ignored grammar is not refused, because that would reject schema-valid values. One null-admission predicate is shared by the schema deriver and the grammar walk; a sweep over 1872 constructed types (per issue 1348) found no type that the schema gate accepts and the grammar walk refuses. On one local 35B mixture-of-experts model, three prompts per arm, an unconstrained call produced a valid response once in three, and a call with the wire schema attached produced three in three; the same run showed that the model's honoured schema reorders object members, which is why the grammar's pinned member order is not enforced on the schema dialect. We report what is not shown: retry counts, a 7B-class model, the llama.cpp parser, and the per-use generic form.

## Problem and claim
Existing structured-output tools tie the JSON schema to a library object (Pydantic, Zod) or to a provider call. They validate or constrain, and sometimes both, but the three consumers of the shape (what the prompt says, what the decoder is allowed to emit, what the checker accepts) are derived by different code from, at best, a shared source. Drift between them has two cost forms: a decoder that can emit something the checker rejects (wasted retries), and a checker looser than the decoder (silent acceptance of what the decoder was never meant to produce).

The claim of this paper is narrower than "constrained decoding works". It is that deriving all three from one declared type, with the checker authoritative and the decoder a hint that is audited when claimed, is a design intended to stay consistent under change and cheap to extend; the drift study that would measure this is not yet run. The evidence is a set of structural invariants, tested, rather than model quality numbers: the grammar accepts exactly what the validator accepts on a corpus; the two dialects accept the same values; the two null-admission predicates are extensionally equal on a swept pool; and an ignored constraint, a wrong-digest claim and a claimed-but-violated constraint each map to a distinct outcome.

A second claim concerns retries: because the budget is a static multiplier on one crossing, the cardinality analysis stays exact (ceiling N+1) without needing loop bounds. A third, deliberately modest claim, is that a cheap constrained decode turns a competence problem of a small model into a mechanical one. The design note argues this and measures one instance; it does not measure retry reduction, and the paper must say so.

## Contributions
1. A typed model boundary (python tier; the other five refuse it by name): a `validated` emission returns an ADT, the response is validated revl-side regardless of provider, and a malformed response is a typed fault that does not execute any subsequent effect and does not double-emit on retry (`docs/design/257-typed-model-boundary.md:508-620`; `backends/python/runtime.py:630-697`; `tests/test_validated_retry_257s2.py`).
2. A retry budget declared in the signature and folded as a static multiplier N+1 into the cardinality bound, with no loop-bound certification (`docs/design/257-typed-model-boundary.md:546-597`; `src/revl/cardinality.py:49-62,533`; `tests/test_validated_retry_257s2.py::test_validated_retry_two_reports_model_ceiling_three`).
3. A decoding grammar (GBNF) and a wire JSON schema derived from the same type, bound at the crossing as data (`format`, `root`, `text`, `digest`) that only the python tier consumes; the other five refuse the crossing by name (`src/revl/decode_grammar.py:354-464`; `docs/design/542-grammar-constrained-decoding.md:57-87`).
4. One null-admission predicate shared by the schema deriver and the grammar walk, held equal by a sweep, with the grammar walk kept as a drift assertion (`src/revl/type_schema.py:46`; `src/revl/decode_grammar.py:135`; `tests/test_decode_grammar_513.py:400-462`; `docs/design/542-grammar-constrained-decoding.md:159-243`).
5. A claim protocol: a provider that takes a constraint is held to the artifact it named; an ignored grammar is not refused; a wrong digest and a violated claim are distinct typed refusals that ride the retry budget (`backends/python/runtime.py:408,444-453`; `docs/design/542-grammar-constrained-decoding.md:548-624`).
6. A small measurement showing why dialects get different standards: an honoured JSON Schema reorders members, so the GBNF's pinned order cannot be demanded of it (`docs/design/542-grammar-constrained-decoding.md:737-852`; `bench/decode_grammar_probe.py`).

## Related work
| Prior system or line of work | What it does | What revl does differently |
|---|---|---|
| Outlines, Guidance, LMQL, llama.cpp GBNF (citation to be checked) | constrain decoding with regex, CFG or JSON schema | revl derives the constraint from the language's own type and keeps the validator authoritative; claim is audited |
| XGrammar and similar fast grammar engines (citation to be checked) | efficient grammar-constrained decode | not competing on engine speed; revl states a grammar and a provider performs it |
| Instructor, Pydantic AI, BAML (see the [related-work table](../related-work.md)) | library-level schema from a class, validate and retry | retry is a declared static budget folded into a language-level cardinality bound; no loop |
| OpenAI Structured Outputs, Anthropic tool use, Gemini responseSchema | provider-enforced schema | revl treats these as providers of one of two dialects and notes, per adapter, what is exact or approximated (`docs/model-providers.md:203-238`) |
| Grammar-constrained decoding results (e.g. Geng et al.) (citation to be checked) | analysis of quality effects of constraint | no quality claim here; revl reports a member-order effect for one provider |
| Typed LLM function-calling in languages (e.g. TypeChat) (citation to be checked) | TypeScript types as schema | revl adds the shared null predicate, claim protocol and cardinality integration |

## Evidence on main
| claim | path:line or test | status | note |
|---|---|---|---|
| response validated revl-side; malformed is a typed retryable fault | `backends/python/runtime.py:251,630-697`; `tests/test_validated_retry_257s2.py` (about 25 test functions) | shipped (python tier) | the header of the typed-boundary design note (`docs/design/257-typed-model-boundary.md:3`) still says DESIGN, while `docs/design/542-grammar-constrained-decoding.md:3` and `docs/model-providers.md` treat it as landed |
| later effect does not fire on a malformed value, and fires exactly once on a retried success | `tests/test_validated_retry_257s2.py:253,285,315` | shipped | |
| retry multiplies the cardinality bound | `src/revl/cardinality.py:49-62`; `tests/test_validated_retry_257s2.py:203-240` | shipped | ceiling 3 for `retry 2`, 1 without |
| `retry` without `validated`, retry 0, negative retry refused | `tests/test_validated_retry_257s2.py:390-420` | shipped | |
| five other tiers refuse a `validated` crossing by name | `src/revl/validated_boundary.py` (named in `docs/design/542-grammar-constrained-decoding.md:425-440`); `tests/test_validated_tier_refusal_1373.py` | shipped | typescript `--target temporal` is a deliberate exception; `validated_boundary.py` was not opened directly |
| grammar and schema derived from one type, bound as data | `src/revl/decode_grammar.py:354-464`; `tests/test_decode_grammar_513.py` (23 test functions) | shipped | |
| grammar accepts exactly what the validator accepts | `tests/test_decode_grammar_513.py:288-340` | shipped, on a corpus | uses a recogniser written for the test, not llama.cpp's parser |
| null-admission predicates equal; gate containment | `tests/test_decode_grammar_513.py:415-462` | shipped | pool built from `_sweep_pool` (line 400); test asserts pool > 1500 and 0 reaching the grammar walk. The 1872 and 0/1288 figures are quoted in `docs/design/542-grammar-constrained-decoding.md:194-201`; another passage of the same note says 1560; reconcile by running the sweep |
| two dialects accept the same values | `tests/test_decode_grammar_seam_513s2.py:26,254,312` | shipped | `Bytes` base64 constrained only under gbnf (documented) |
| claim protocol and `GrammarNotHonouredError` | `backends/python/runtime.py:408`; `tests/test_decode_grammar_seam_513s2.py` (33 test functions) | shipped | |
| adapters: openai-compatible json-schema and gbnf claim; anthropic and gemini read and do not claim | `docs/model-providers.md:203-238`; `tests/test_constrained_decoding_1462.py` (22 test functions) | shipped | llguidance optional, byte-level check |
| real-provider probe | `bench/decode_grammar_probe.py`; `bench/structured_output_bench.py` | script shipped; results in prose only | `bench/results/structured-output/` does not exist on main, so the raw completions the bench writes are not archived in the tree |
| slice 3, per-use generic `model.json[T]` | `docs/design/542-grammar-constrained-decoding.md:313-342,343-466` | not built | argued deferral; reopened 2026-09-29, undecided |

## Numbers that exist
- Gate containment sweep: types where the schema gate accepts and the grammar walk refuses: 0; where the schema gate refuses and the grammar walk accepts: 1288, over 1872 constructed types (`docs/design/542-grammar-constrained-decoding.md:194-201`, issue 1348). The same note elsewhere says 1560 (`:405`); the test asserts only more than 1500 (`tests/test_decode_grammar_513.py:452`). Not run here.
- Probe on a local 35B-A3B Q4 model through Ollama 0.33.3, temperature 0, flagship `AgentTurn` type, 3 prompts per arm: no constraint: valid 1/3, inside GBNF 1/3; wire schema attached: valid 3/3, inside GBNF 1/3 (`docs/design/542-grammar-constrained-decoding.md:737-800`). Two of the three schema-honoured completions had nested members transposed. Each call took 4 to 11 minutes.
- The same endpoint ignored a GBNF grammar in silence on two calls (`:747-757`).
- Cost of slice 3: 0 of 54 `.ir.json`, 0 of 29 golden files, 5 refusal fixtures only, python the only tier reading `response_grammar` (`:443-460`).
- Before issue 1373 some tiers dropped the constraint in silence; the note's history says four tiers in one sentence and five in the next (`:379-395`), so the count is not settled.
- No retry counts, no token counts, no 7B-class number.

## Paper outline
1. Introduction: three consumers of one shape and where they drift.
2. The boundary: `validated`, ADT return, typed fault, `retry N`, no double emit.
3. Derivation: schema, GBNF, wire schema; null admission; what has no grammar and why refusal sits in one place.
4. The claim protocol and dialect negotiation; why an ignored grammar is not refused.
5. Retry as a static multiplier in the cardinality analysis.
6. Evaluation: invariants and sweeps; the provider probe; retry study (to be run).
7. Related work. 8. Limits.

## Threats to validity and limits
From the docs:
- "No before-and-after retry counts are reported here, and the roadmap item should not be read as claiming any" (`docs/design/542-grammar-constrained-decoding.md:474-500`). Retry reduction is not measured.
- The probe sample is "three prompts per arm ... reported as three, not generalised"; the model is a 35B mixture-of-experts, not the 7B-class case in the motivation; the unconstrained arm failed two of three anyway (`:737-745,841-852`).
- The GBNF text has not been parsed by llama.cpp; the agreement tests use a recogniser written for the test (`:474-480`). Part of this is closed with llguidance (`:853-877`) but "it is not llama.cpp's parser".
- The honoured-check "is necessary and not sufficient" (`:474-508`).
- A pinned member order is a cost: it might make some decoders slower or lower quality; not measured.
- The grammar gate is shadowed by the schema gate; only the schema gate is author-visible. The sweep shows this by construction of the pool, not on real code.
- Python is the only tier that reads the derivation; the other five refuse by name. A cross-tier claim must be limited to "refuse by name".
- Anthropic and Gemini adapters send a schema and claim nothing; their guarantees are the provider's.
- The typed-boundary design note's own header says "DESIGN (implementation pending)", which conflicts with the other notes.
- The shipped parts are authored and tested by the same team; the comparison to Instructor and Outlines is not done.

## Fact-check
Claims in this draft were checked against revl main at commit 45bec431f: 36 checked, 8 corrected.
