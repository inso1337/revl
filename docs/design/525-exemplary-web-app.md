# 525 — the exemplary revl web application

**Roadmap:** item 462 (drives 456–461) · **Source:** revl-harness developer feedback, 2026-09-07 · **Status:** DESIGN DRAFT

## Purpose

Not another feature list. A small, exemplary revl web application that demonstrates two things at once:

1. **Ordinary web development is boring.** Typed routes, request validation, response serialization, safe templates (or a real TS frontend behind a typed boundary), persistence, and streaming are written the obvious way, with no harness-specific routing sentinels (`""`, `"::empty::"`) and no emitter workarounds (ternary-heavy dispatch, `maybe_run`/`maybe_ship`).
2. **The complexity is reserved for the guarantees revl uniquely provides.** The app exercises exactly one differentiator end to end: a live-reconfigurable / hot-swapped effect-governed service, with visible admission, cancellation, and residue-free recovery.

The app is simultaneously the dogfood, the reference, and the public demo/console — the current ~4,800-line console is the anti-example (scripts carried in revl strings, status reconstructed from body prose); this replaces it, rebuilt on first-class primitives rather than retrofitted.

## Non-goals

- Not a framework. Where a capability belongs in a standard web library or in tooling rather than the language, the corresponding roadmap item (456–461) says so; this app is the forcing function that reveals which is which.
- Not large. One clean pass over each guarantee beats breadth. If a section needs a workaround, that workaround is a bug filed against 456–461, not something the app absorbs.
- Not a benchmark. The deliverable includes a written "where revl is already competitive vs. where friction remains" report, not a performance claim.

## Shape (illustrative, to be firmed up)

A minimal task/notes service:

- **Boring half:** a handful of typed CRUD endpoints (item 457: one endpoint declaration → routing + validation + serialization + OpenAPI + TS client), explicit authorization as a separate step, persistence to a small store, a server-sent-events / streaming endpoint, and a UI built from external assets + a TS frontend behind a typed boundary (item 459).
- **Differentiated half:** one service (e.g. the notification/stream fan-out, or a pluggable ranking/scoring component) that is **hot-swapped at runtime** through admission — a new implementation is proposed, admitted (or refused with retained ownership), swapped in, and on an injected fault reverts residue-free. The app surfaces the lifecycle state (item 460: serving / cancel-requested vs cancel-completed / owned-after-failure / recovery-resume) so the guarantee is observable, not implicit.

## Acceptance bar (item 462 exit)

1. Runs under **one** development command (item 461); an induced error names the `.rvl` source line and the failing lifecycle stage.
2. **Zero** routing sentinels and **zero** emitter workarounds in the app source — every place one would have been needed is instead a filed gap against 456–459.
3. Status and execution outcome come from the typed HTTP contract (item 456), never from interpreting output prose.
4. The hot-swap scenario demonstrates admission → cancellation → residue-free recovery against the documented lifecycle contract (item 460), with conformance coverage on the native runtime.
5. Ships with the competitiveness/friction report.

## Sequencing

The app drives 456–461; it is not gated on all of them being finished first. Build it incrementally: each time it needs a primitive, that primitive's item gets a concrete, app-shaped exit test rather than a speculative spec. The app is "done" when it needs no more workarounds, at which point 456–461 are demonstrably real.

## Open questions

- Which single differentiator best showcases admission + lifecycle without bloating the app — hot-swap of a ranking component, or a live-reconfigured stream fan-out? (Pick one.)
- Template engine vs. a TS frontend behind a typed boundary for the UI — the developer explicitly prefers retaining JS/TS tooling with revl behind a clean boundary (item 459), so the default is the TS-frontend path unless a templated server-render proves simpler for the boring half.
- Persistence tier: the smallest thing that exercises the effect/ownership story honestly (a witnessed file store vs. an embedded DB behind an effect).
