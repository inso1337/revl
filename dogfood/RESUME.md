# Dogfood dispatcher — live board

**Status 2026-08-24:** wave 8 and the item-77 first-product harvest are
COMPLETE — the board is empty (delete-this-file convention). Wave-9 deferred
items are tracked in docs/v2.0-roadmap.md (75(a) arrow parameter annotations,
70 lexer brace balancing, 71 structural records, 73(d) v1 IR fate).

## Wave 8 — DONE (all merged to devwip, PRs #44/#45 → main)

| Run | Items | Outcome |
|---|---|---|
| w8-friction | 76 (a)(b)(c) | ✅ dispatcher conformance maps (`EXPR_KINDS`/`EXPR_DISPATCHERS`), empty-collection pinning, env honesty (agent/friction → ae4e9dd) |
| w8-hygiene | 72 + 73(a)(b)(c) + 74(c)(d) | ✅ ts-alias, run gating, golden snapshot policy, rs-A1 + wasm-trap decisions (agent/hygiene → fccde49) |
| w8-runtime | 74 (a)(b) | ✅ cordis TS assertActive fork pin (c8b94b2) + cordis-py Config one-liner (1c5e6f1) (agent/runtime-errata → e15ce1e) |
| w8-checker | 75 (b)(c) | ✅ stdlib-method sliver + table-disjointness guard + explicit [T] heuristic (agent/checker-frontier → 4e899f2) |

Plus this restart's broader wave: accessor fan-out (item 10), records fence
(item 71), items 44/60/64, selfhost checker slice, and the harness first-
product harvest (item 77).

## Item-77 follow-ups — DONE (PR #47 → main)

| Feedback | Fix |
|---|---|
| TS emitter doesn't bind arrow params in component scope | ✅ backends/typescript/emit.py binds arrow params in component scope; the agent loop runs on ts (`revl run --backend ts --once`, NO-RESIDUE) |
| Java host Map stub still HashMap<String,String> | ✅ generic `Map<V>` per site (ledger `Map[Str,List[Msg]]` compiles under javac --release 21) |
| Go placement v1/v2-only | ✅ v3 typed-core compositions place: records, ADTs, match cross the bridge seam |

## Deferred (wave 9)
75(a) arrow parameter annotations (spec-first) · 70 lexer brace balancing ·
71 structural records (anonymous-literal fence) · 73(d) v1 IR fate ·
FR-3 JSON on rust/java/go/wasm · FR-5 lifecycle on java/wasm ·
FR-1 component-path mutable-var capture (latent frontend gap, findings-fr1-ts)
