# 525 exemplary web app — slice 3 (the differentiated half): findings

**Roadmap:** item 462 · **Issue:** #725 · **Slice:** 3 of N (the differentiated,
hot-swap half) · **Status:** FINDINGS, 2026-09-08

Slice 1 built the boring CRUD half (`docs/design/525-webapp-slice1-gaps.md`).
This slice adds 525's second claim: the complexity is reserved for the guarantee
revl uniquely provides. `examples/app/notes.rvl` grows ONE hot-swappable,
effect-governed service — a pluggable ranking/scoring component — and
`tests/test_app_hotswap_725.py` proves the full lifecycle walk on the real
cordis-py runtime through `Session.swap`, the same admission/swap machinery an
agent drives over the MCP bridge. Nothing about admission, swap or recovery was
reinvented; the app uses `compile_source(manifest=, replacing=)` + `Session.swap`
+ `Session.aclose`/`strand_teardown` + `revl.recovery.recover` directly.

## The differentiator, made concrete

`Ranker` records engagement signals (a note is "bumped" when viewed) into
effect-created state and SCORES a note from those signals under a strategy. The
scoring strategy is the hot-swap axis:

- a `recency` build (each signal weighs 1) is proposed, admitted and swapped for a
  `weighted` build (each weighs 3) **without losing the recorded signals** — the
  state crosses the swap because `TrendingRanker` declares `handoff ranking:
  Map[Str, Str]` (item 53), and `strategy()`/`score()` prove the new code is live;
- a successor whose declared `handoff` shape cannot hold the running one, or whose
  activation is not healthy, is **refused with the running build left serving** —
  a documented refusal with retained ownership, never a silent state drop.

## The observable lifecycle states (acceptance bar 4)

The differentiated half is deliberately NOT wired behind the CRUD HTTP surface (the
boring half owns the typed HTTP contract, acceptance bar 3). Its lifecycle is
observed through the service lifecycle contract's OWN surface
(`docs/lifecycle-contract.md`), which is where item 460 makes admission /
cancellation / recovery observable. Every named state is asserted on the runtime:

| Contract state | Where it is observed | Evidence |
| --- | --- | --- |
| serving | `Session.state()` | fiber `ACTIVE`, `providedKeys == ["ranking"]`, calls answered |
| admitted swap | `Session.swap` return | `handoff` report `migrated: True`; new `strategy()`/`score()` live, signals survived |
| refused swap (retained ownership) | admission `RevlError` + swap `SessionError` | hand-off drift refused pre-teardown; unhealthy successor rolled back; gen N keeps serving |
| cancel-requested vs completed | `aclose()` + `teardown_disposition()` | `disposal.invoked`, then `released`, `noResidue == True` |
| owned-after-failure | `aclose()` + `strand_teardown()` | native fault settles `unresolved`, resource named owed, `stranded == True` |
| recovery-resume | `recover(wal)` | `activation-complete` present -> `rolled-forward`, `resumed == True` |
| residue-free roll-back | `recover(wal)` | `activation-complete` absent -> `unreconstructible == []`, `residue.clean == True` |

## 525 discipline: zero sentinels, zero emitter workarounds

The differentiated source needed **no** routing sentinel (`""`/`"::empty::"`) and
**no** emitter workaround (`maybe_run`/`maybe_ship`) — the whole-file scan in
`tests/test_app_notes_725.py::test_zero_sentinels_and_no_emitter_workarounds`
covers the added lines. No new gap is filed against 456-461 for this slice; the
admission, hand-off and recovery primitives were expressible directly.

### Not gaps (recorded so a later slice does not refile them)

- **The host-verb frontier types arguments, not results** (item 397 / 401): the
  engagement log is a raw host `Map`, so `log.size()` is opaque and
  `log.size().to_str()` is refused (G8). Routed idiomatically through a template
  literal (`` `sig-${log.size()}` ``) for the row key — the same shape
  `examples/live_counter.rvl` uses — no sentinel, no workaround. This is the
  documented host boundary already noted in the slice-1 gaps doc, not a new gap.
- **A `lifecycle test` cannot drive a swap.** Swap is a `Session`/MCP verb, not a
  lifecycle-test statement, so the in-`.rvl` lifecycle test proves only serving +
  residue-free teardown; the hot-swap legs live in the Python conformance test
  (`tests/test_app_hotswap_725.py`), exactly as
  `tests/test_state_handoff_exec.py` and `tests/test_issue_723_lifecycle_contract.py`
  drive their swaps. This is the documented boundary between the two harnesses,
  not a limitation of the app.

## What the next slices add

- The TS frontend behind a typed boundary (item 459) and the one-command dev
  runner (item 461), the remaining 525 acceptance bars (1, 5: the competitiveness
  /friction report).
- Slice 2's authorization (`stdlib/auth.rvl`, opaque `Principal`) once it lands.
