# findings — agent/map-value-type (persistent Map value type)

Branch: agent/map-value-type off devwip.
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
- [slow] devwip's committed `backends/typescript/tests/generated/conformance.ts`
  was stale against its own fixture + emitter — regenerating for the Map
  fixture also repaired unrelated checked-div helper drift. That is the
  recurring "stale generated golden" bug class; a CI check like go's
  `test_checked_in_generated_is_current` would catch it for TS too.

## Review round 2 (orchestrator found two escapes; both fixed)

This is exactly what the protocol exists for: I shipped a feature whose
tests all passed, and review found two bugs by running code I never
executed end-to-end.

1. **BUG 1 — let-bound receivers lowered as verbatim field calls.**
   `let m = Map.empty()` marked `m` as `"host"` in the lowerer's scope,
   because `_is_host_valued` saw the constructor ROOT (`Map`) in
   `_HOST_CALLABLES` and never looked at the method (`empty`). Every later
   `m.set/lookup/has` then took the unchecked verbatim path — no compile
   check, runtime `_revl_field` KeyError. My own tests missed it because
   every executed flow used annotated parameters or a `var` binding, and
   the one test that used the chained form only asserted `ir_version`.
   Verdict: **caught-bug** (by review, not by me — that is the point of
   having a reviewer). Root cause was ONE predicate; fixing
   `_is_host_valued` closed the let-binding, the direct chain
   `Map.empty().set(...)`, and kept genuine `Map.new()` bindings host.
2. **BUG 2 — Never-as-wildcard soundness hole.** Independent of lowering:
   `set` on a `Map[Str, Never]` receiver checked its value argument
   against `Never`, which is compatible with everything, and returned
   `@self = Map[Str, Never]` — which flows into ANY `Map[Str, X]`. A Str
   could be planted under an Int-typed map; `lookup` would then claim
   `Opt[Int]`. Wrong-answer class, exactly what revl exists to refuse.
   Fix chosen: **(a) unify-and-carry**, not bidirectional plumbing or
   refusal. When the receiver's element is bottom (`Never` exactly — not
   Any/T-params, so generics are untouched), `builtin_check` learns the
   element type from the concrete argument and returns the rebuilt
   container (`m.set(k, "oops")` types `Map[Str, Str]`). Both required
   flows close because both end in a check position against a concrete
   expected type: expected-type flow refuses with ``let m2: Map[Str, Int]`
   expects Map[Str, Int], got Map[Str, Str]`, return-position flow with the
   return-position twin. Chosen over (b) because it needs no new plumbing
   through infer/check call sites, and over (c) because refusal would ban
   the natural build-up idiom `let m = Map.empty(); m = m.set(k, v)`;
   unification instead makes the empty literal behave like every other
   type-inference seed. Diagnostics get BETTER: they name the learned
   type, telling the programmer exactly which value poisoned the map.
3. **The List fence, honestly:** the reviewer asked whether `[]` had the
   same escape. It did — pre-existing, predating Map entirely:
   `[].push("s")` typed `List[Never]` and flowed into `List[Int]` in both
   expected-type and return positions. The same learning rule (applied to
   any `@self` builtin on a bottom-typed receiver: `push`, `concat`)
   fences it. Correct uses (`[].push(1)` → `List[Int]`) keep compiling;
   generics with T-param elements are untouched because only literal
   `Never` triggers learning.
4. Regression tests added for every repro verbatim (review round 2 section
   in tests/test_map_value_type.py): the planted-Str repro now refused at
   compile time; the let-bound flow asserted to lower to `builtin` nodes
   AND executed on the python tier; direct-chain IR asserted; both BUG 2
   flows plus their correct-value twins; the List fence; and a guard that
   the fix did not de-host genuine `Map.new()` bindings.

Count this round: 2 escapes found by review, ~4 probe cycles to root-cause
(one shared predicate + one checker rule), 6 regression tests. Lesson
logged: "compiles" is not "checked" when two namespaces share a dispatch
predicate — the field-call fallback was a silent unchecked path, and
nothing in my suite executed a let-bound receiver until review did.

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

## Post-final-run addendum

- [nit] Running two *backends'* test files in ONE pytest process collides on
  the module name `emit` (each backend dir has its own emit.py loaded via
  importlib/sys.path); the later import wins and the other backend's golden
  tests then compare against the wrong renderer (4 spurious failures, all
  disappearing when the suites run as intended, one process per backend).
  A unique module name per backend (`revl_py_emit`, `revl_rs_emit`, ...) or
  an importlib util that caches per path would make combined runs safe.
- Final counts, from this worktree's venv:
  - root `tests/`: **1310 passed / 54 skipped** (baseline ~1283)
  - wasm 32; go 17; java text-suite + javac-gated (skips locally without
    a JDK shim); rust cargo map tests executed green with cargo present;
  - typescript vitest **80/80** including 4 new Map tests on real node.

## Diagnostic-hints addendum (agent/diag-hints) — each friction row, fixed

Every item below was a friction entry in this or a sibling findings file;
each now has a hint, and a rejection-corpus fixture pinning it:

| finding (source) | was | now |
|---|---|---|
| `emission fn` in `provide` (uxprobe R1) | bare `expected fn, found 'emission'` | hint: provider methods are plain `fn` — emission-ness inherited from the service declaration (G4 upper bound) |
| keyword as record-field/ident (this file: "expected ident, found 'emission'/'requires'") | bare parser confusion | hint: `<kw>` is a reserved keyword — cannot name a field/variable/parameter/method; pick another name |
| `;` as statement separator (this file, time-to-green) | stack-shaped `expected an expression, found ';'` from deep in `_primary` | lexer refuses the character at source with the rule: statements are newline-separated |
| duplicate-let across branches (shadow/map sessions) | `` `y` is already declared in this function`` with no why | hint states the rule: lets are function-scoped, not block-scoped — sibling branches share one namespace; rename or reuse |

All four pinned as `examples/rejections/v2_*.rvl` + REJECTIONS rows
(asserting the HINT text, not just the message), so the guidance cannot
silently regress.
