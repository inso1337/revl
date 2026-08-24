# findings-friction — roadmap 76(a)(b)(c), the dogfood-friction harvest

Branch `agent/friction` off devwip @ 8a20a63. Implements roadmap item 76:
(a) per-emitter dispatcher conformance maps (the "did you patch both paths"
red test), (b) empty-collection pinning on the go tier, (c) environment
honesty (pinned runtime clone, fast frontend loop, repo map).

## 1. Refusal log

Every `revl compile`-shaped refusal I hit, with a verdict.

- **`fn f(xs: List[Int]) -> Int { return xs.length }` refused by the GO tier
  (fn body): "unsupported v3 expression kind 'len' in Go backend".** Verdict:
  `caught-bug` — a genuine go gap: `xs.length` is a legal, documented pure
  expression that every other tier renders, and go's v3 renderer had no `len`
  branch at all. Fixed (renders `revlStrLen`/`revlListLen` like the `length`
  builtin). Invisible to `tools/conformance.py`, which has no `.length` case.
- **`let r = { a: 1 }` in a method body refused by go: "unsupported expr kind:
  'record'"** (and the same fall-through for `field`, `match`, `adt`, `arrow`,
  `optfield`, `optcall`, `fn` in the go component renderer). Verdict:
  `caught-bug`-shaped: the go component renderer's docstring claims it handles
  "the full surface expression set" while the frontend can legally produce at
  least eight kinds it has no branch for. These are mostly genuine v1/v2 tier
  limits (a record literal needs a declared type, and declaring one routes the
  document to go's typed-core path, which carries no live component), so the
  fix is a **named tier-limit refusal** with a workaround for each — never the
  generic fall-through — plus `field`-`.length` and `fn` handling that ARE
  expressible. The conformance sweep missed all of it (its component cases
  never put these kinds in method bodies).
- **`o?.x` in a wasm fn body: "unsupported v3 expression kind 'optfield'"**
  (and `optcall`), while the tier deliberately refuses the sibling kinds with
  named errors. Verdict: `caught-bug` — an undocumented fall-through where a
  tier limit belongs. Now a named refusal mirroring rust/java ("unwrap with
  `match` or `??`"), and listed in the wasm tier's explicit absences.
- **`Map.empty()` in a wasm component body: "unknown expression kind
  'maplit'"** while the fn-body path refuses it with a named Map-value-type
  error. Verdict: `caught-bug` — the same kind, two messages on one tier; now
  one named refusal.
- **`hole` in a go document: the fn renderer fell through to "unsupported v3
  expression kind 'hole'"** while the other five tiers refuse holes at the
  document level with a named error. Verdict: `caught-bug` — go lacked the
  pre-emit `_refuse_holes` walk every other backend has. Added (mirrors
  python's walk verbatim).
- **`var m: Map[Str, Int] = Map.empty()` refused by go: "an untyped empty Map
  needs an expected Map type ... pin it via a typed fn return/parameter or any
  annotated flow".** Verdict: `friction` — the diagnostic describes a rule the
  checker doesn't have. I verified empirically that a typed *parameter* does
  NOT pin (`takes(Map.empty())` still refuses: the go call renderer does not
  thread parameter types as expected types), and that annotated `let`/`var`
  did not pin either. Fixed the preferred way: the frontend now threads the
  author's annotation onto the `maplit` node (`"expected"`), so the annotated
  form compiles on go in fn and method bodies; the hint now names the
  positions that actually pin (typed fn return, annotated let/var) and no
  longer claims the parameter.
- **`let n = 3; let s = ...` (semicolons) in a method body: "unexpected ';' —
  statements are newline-separated".** Verdict: `friction` — correct and
  consistent, but my first three probe programs were written with `;`
  separators (habit from the task prose, which uses `;` between statements in
  its examples). No fix; the diagnostic is right.
- **`match v { 3 => 1, _ => 0 }`: "expected a match pattern (case name or
  `_`), found 3".** Verdict: `friction` — match patterns are ADT case names
  and `_`, not literals; my probe syntax was wrong. The diagnostic names the
  rule; fine.
- **`revl compile file.rvl --backend go`: "unrecognized arguments: --backend
  go".** Verdict: `friction` — `revl compile` has no backend flag; emitters
  are driven by tests/tools. I had to load emit modules directly for the
  per-tier probes. Not a gap (the harness is the intended driver), but the CLI
  surface is not discoverable for this workflow.

## 2. Friction log

- `[blocker]` **The two-renderer trap is real and unfenced.** python has two
  expression dispatchers, go has two, wasm has three, and before this branch
  nothing declared which IR kinds each must handle. I reproduced the exact
  findings-records failure mode (a pure kind handled by one path and absent
  from another) in go's component renderer within an hour. The fix — declared
  `EXPR_DISPATCHERS`/`EXPR_REFUSED` tables per emit.py + a conformance test —
  is the roadmap's own prescription.
- `[slow]` **`tools/conformance.py` has blind spots the kind-level map
  closes.** It walks 50 source cases; `.length` in fn bodies, `?.` anywhere,
  `Map.empty()` in component bodies, record literals in method bodies and
  `format` templates are not among them, so four real tier gaps and three
  fall-throughs lived under "0 gaps". The new test is per-kind, so it cannot
  share the blind spot.
- `[slow]` **Emitters live under `backends/`, not `src/revl/`** — I reproduced
  the findings-records grep: my first instinct was also `src/`. Added the
  repo-map line to CONTRIBUTING.md so the next agent does not pay the minutes.
- `[nit]` **`backends/go/emit.py` docstrings overclaim.** `_expr`'s docstring
  says "the single expression renderer ... handles the full surface expression
  set" while eight kinds fall through. The conformance table now says what is
  true, and the test would catch the docstring's lie again if the code rotted
  back.
- `[nit]` **go's whole-document routing silently drops components.** A v3
  document with a component AND any top-level pure declaration routes to the
  typed-core path, which emits only the pure declarations — the component is
  silently absent from the output, and the conformance sweep reports `ok`
  because the emitter did not raise. Documented in emit.py's routing comment
  and the README's out-of-scope note, but the silent-drop shape is exactly
  this project's recurring failure class (accept-and-mean-something-else). Logged
  here as a follow-up, not fixed: the fix is a design decision (either refuse
  the document or teach the typed-core path to carry components).
- `[nit]` **`var m: Map[Str, Int] = Map.empty()` does not bump `ir_version`.**
  The annotated-var repro compiles as `ir_version: 1`, and the pin I add is an
  additive node field, so the frozen-reference invariant held (goldens stayed
  byte-identical). Fine — but worth knowing: the Map *value* type's presence
  is not what versions a document.

## 3. What revl gave you

- **The frozen-reference invariant worked exactly as advertised.** Adding the
  `"expected"` pin field to `maplit` nodes (only where an annotation exists)
  broke zero goldens and zero reference-IR tests — byte-stability held, which
  is what made the frontend-threads-the-annotation design cheap and safe.
- **Cross-tier emit probes are a fast oracle.** Running one source through all
  six emitters found go's `len` gap, the go component renderer's fall-throughs
  and wasm's three inconsistencies in minutes each — the same lesson the
  roadmap records: single-tier tests cannot see a renderer divergence.
- **The checker refused my syntax errors before any emitter ran** (the `;`
  separator, the literal match pattern) — the compile→refuse cycle is fast
  enough that bad probes cost seconds, not minutes.
- **The `EXPR_KINDS` schema as a single registration point works.** Injecting
  a hypothetical new kind into the schema turns every tier's coverage check
  red immediately — that is the "new expression kinds have a place they must
  be registered" requirement, demonstrated by a test.

## 4. Time-to-green

Compile→refuse→fix cycles: ~8 meaningful ones (go `len` ×1, go component
fall-throughs ×2 batches, wasm `?.`/maplit/record_update-in-infer ×2 batches,
go hole walk ×1, maplit pinning ×1, conformance-test context iterations ×4+).
Longest single stall: building the behavioral layer of the conformance test —
the per-tier minimal contexts (wasm's per-function engine state, go's expected
threading, java's declared-record requirement) took several iterations of
probe-and-fix (~40 min). The static fall-through detection + position rule did
the real work; the behavioral layer is the honesty check on top.

## 5. Cost ledger

- `tooling` — grepped `src/` for the emitters before remembering they live in
  `backends/` (the findings-records friction, reproduced; ~5 min). The repo-map
  line in CONTRIBUTING.md removes it.
- `tooling` — `revl compile` has no `--backend` flag; per-tier probing meant
  loading emit modules by hand and learning each tier's render-context
  constructor (`_Env`, `_V3Ctx`, `_Scope`, `_ComponentEmitter` template mode,
  `_open_function` priming) (~30 min). A documented "emit this IR through every
  tier" one-liner would have removed most of it.
- `diagnostic` — go's "pin it via a typed fn return/parameter or any annotated
  flow" claimed two positions that do not pin; I verified both empirically and
  the hint now names the real ones (~10 min, and one false claim in a finding
  file corrected).
- `docs-gap` — go's `_expr` docstring claims full-surface coverage while eight
  kinds fall through; the conformance tables now state the truth as data
  (~5 min).
- `missing-feature` — none this run; everything I hit was fixable in-tier.
- `spec-ambiguity` — none this run; the roadmap's 76(a)(b)(c) text matched the
  code closely enough to implement directly.

Single change that would have cut the most cost: the conformance test itself —
it is the fix for the tooling/diagnostic rows above, and the reason the next
"did you patch both paths" moment is a red test instead of a stall.
