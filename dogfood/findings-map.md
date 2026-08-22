# findings — agent/map-value-type (persistent Map value type)

Worktree: /Users/inso/revl-wt-map, branch agent/map-value-type off devwip.
Task: spec + checker + 5 emitters + tests for `Map[Str, V]` value type
(docs/stdlib-2.0.md §Map). Appended continuously per PROTOCOL.md.

## 1. Refusal log

(compile→refuse cycles logged as they happen)

## 2. Friction log

- [nit] `ls src/revl/backends` fails — backends live in a top-level
  `backends/` dir, not under the package; the task brief's phrasing
  ("backends/wasm/emit.py") vs repo layout took one detour to reconcile.

## 3. What revl gave you

(so far: design phase — nothing executed yet)

## 4. Time-to-green

(not yet: implementation phase)
