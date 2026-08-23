# findings — agent/map-value-type (persistent Map value type)

Worktree: /Users/inso/revl-wt-map, branch agent/map-value-type off devwip.
Task: spec + checker + 5 emitters + tests for `Map[Str, V]` value type
(docs/stdlib-2.0.md §Map). Appended continuously per PROTOCOL.md.

## 1. Refusal log

1. Component body written as plain fn statements:
   ```revl
   component C {
     let store = effect Map.new() undo store.drop()
     fn touch(k: Str) { store.insert(k, k) }   // refused
   }
   ```
   → `<string>:3: expected a statement (`let`, `effect`, `emit`, `fail`,
   `if`, `provide`), found 'fn'` — *bodies contain only effect forms (G6)*.
   Verdict: **caught-bug**. My test source was wrong; component logic must
   live in `provide { fn ... }`. The message names the legal forms and the
   reason, which is exactly what got me to the fix in one cycle.
2. `fn f() -> Map[Str, Int] { return m.fetch("k") ?? 0 }` on a Map VALUE →
   `no builtin method `fetch``. Verdict: **caught-bug** (deliberate probe).
   There is no verbatim pass-through of unknown method names to the host,
   which is finding-6's portability sin; now pinned as a conformance
   example (`examples/rejections/v2_map_value_unknown_method.rvl`).
3. `m.set(k, "one")` into `Map[Str, Int]` → `builtin `set` argument expects
   `Int`, got `Str``. Verdict: **caught-bug** (probe); pinned as
   `examples/rejections/v2_map_set_value_mismatch.rvl`.
4. wasm tier, every map form → `... is not lowerable on this tier yet —
   the Map value type has no representation here; use a hosted backend`.
   Verdict: correct-by-design named tier error, same shape as indexOf.
5. Go, unpinned empty map (`var m = Map.empty()` with no annotating flow)
   → `an untyped empty Map needs an expected Map type on this tier ... pin
   it via a typed fn return/parameter or any annotated flow`. Verdict:
   honest refusal, matches how untyped empty lists behave where they cannot
   be inferred; message says how to fix it.

## 2. Friction log

- [nit] `ls src/revl/backends` fails — backends live in a top-level
  `backends/` dir, not under the package; the task brief's phrasing
  ("backends/wasm/emit.py") vs repo layout took one detour to reconcile.
- [slow] The spec prose (and my first invariant test) listed the host verb
  set as `open/close/query/execute/new/get/insert/remove/drop`, but
  `_HOST_ARG_SIG` also carries `Job.run`. Docs had drifted from the table;
  the exact-set assertion caught it immediately. Doc + test both fixed;
  consider generating that verb list in the docs from the table.
- [nit] `;` is not a statement separator — statements are newline-bound.
  The parser error (`expected an expression, found ';'`) doesn't hint at
  the newline rule; cost one compile cycle.
- [nit] Backend emitters are loaded per-test via importlib and each load
  defines its own `EmitError` class, so `pytest.raises(_backend("go").EmitError)`
  fails on class identity if you call `_backend("go")` twice. Existing test
  files dodge it by loading once; worth a shared helper.

## 3. What revl gave you

- The namespace-disjointness invariant, once written as an exact-set
  assertion over `_HOST_FAMILIES`, caught the stale doc verb list (missing
  `run`) in one run — that is the "docs can't lie" property paying rent.
- The checker's builtin argument messages were precise enough to paste
  verbatim into two conformance examples without rewording.
- Python-tier persistence fell out of the emitter for free: `{**m, k: v}`
  IS the persistent set; the receiver-snapshot test passed first try, no
  aliasing bugs to chase.
- The bottom-type trick reused from `[]` meant `Map.empty()` flowed into
  `Map[Str, V]` for any V with zero extra checker machinery beyond the
  constructor interception.

## 4. Time-to-green

~6 compile→refuse→fix cycles total. Longest single stall (~2 cycles): the
component grammar — writing effect-form bodies at component scope before
learning `provide {}` is the only home for fns; the refusal message named
the forms but not the nesting rule. A "component bodies contain only
provide/handle blocks" hint would have shortened it to zero. Full suite:
1294 passed / 68 skipped (baseline ~1283), all from the worktree venv.
