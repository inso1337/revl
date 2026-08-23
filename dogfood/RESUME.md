# Dogfood dispatcher — live board (wave 8)

**Status 2026-08-24:** loop restarted with the cost-ledger protocol
(PROTOCOL.md §5 + COSTS.md). Delete this file when the board is empty.

## Board

| Run | Items | Task | Status |
|---|---|---|---|
| w8-friction | 76 (a)(b)(c) | dispatcher conformance maps, empty-collection pinning, environment honesty | dispatched |
| w8-hygiene | 72 + 73(a)(b)(c) + 74(c)(d) decisions | tier-naming/doc drift; golden policy applied (conformance.md text, rust `_string` collapse + regen, `_uses_builtin_result` re-judged, goldens into default test entry); errata decision lines for rs-A1 (torn-state freedom promoted to contract) and wasm traps (accepted tier limit) | dispatched |
| w8-runtime | 74 (a)(b) | cordis (TS) `assertActive` G5 residue fix in fork + pin (upstream PR text drafted, NOT opened — user confirms); cordis-py dict-plugin `Config` one-liner + retire the emitted workaround | dispatched |
| w8-checker | 75 (b)(c) | stdlib-named-method sliver refused on unpinned receivers + table-disjointness guard; explicit `[T]` disables the one-letter heuristic | dispatched |

Deferred to wave 9: 75(a) arrow parameter annotations (spec-first — architect
writes the spec first), 70 lexer brace balancing, 71 structural records,
73(d) v1 IR fate (decided after w8-hygiene reports v1's remaining consumers).

## Standing protocol

- Findings + cost ledger per PROTOCOL.md; orchestrator fills COSTS.md from
  telemetry at each run's completion.
- Review = diff + live repro + suite on a **merge preview**; then merge,
  push, flip ✅ with `Landed:` citations.
- No AI attribution trailers. Agents push `agent/*` branches only.
