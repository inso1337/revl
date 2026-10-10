# Roadmap archive: Follow-ups from harness findings (items 84/85)

Closed items moved verbatim from [docs/v2.0-roadmap.md](../v2.0-roadmap.md). The tools read this file from its "## " heading on.

## Follow-ups from harness findings (items 84/85)

86. ✅ **Complete host `Map` iteration (`keys()`/`size()`) on ts/rust/java.** Item 84 fixed the py host `Map` runtime; the same gap exists on ts (`MapHandle`), rust (`struct Map<V>`), and java (`class Map<V>`) — their host `Map` carries only `new/insert/remove/get/drop`, so `store.keys()`/`store.size()` type-check clean (host-receiver provenance isn't tracked in provide bodies) but have no runtime backing and crash at run. wasm is safe (rejects `Map.new` at compile time). Mirror 84: add `keys()` (sorted canonical Str order) and `size()` to each tier's host `Map` runtime, pinned by a compile→emit→run test per tier. Do NOT add `values()`/`items()` — not part of the promised Map surface (docs/stdlib-2.0.md).

87. ✅ **Teach `selfhost/lexer.rvl` about triple-quoted strings (`"""`).** Item 85 added `"""..."""` to `src/revl/lexer.py`; the self-hosted lexer (`selfhost/lexer.rvl`, cross-checked token-for-token by `test_selfhost_lexer.py`) does not yet know the form. It passes today only because no corpus `.rvl` file uses `"""` — the moment self-hosting authors a triple-quoted source (the whole point of 85), the cross-check diverges. Add the same `"""` handling to `selfhost/lexer.rvl`.

88. ✅ **Complete host `Map` iteration (`keys()`/`size()`) on the go tier.** Items 84/86 fixed py/ts/rust/java; the go host `Map` (`type Map struct` in `backends/go/emit.py`) still carries only New/Insert/Remove/Get/Drop — same latent gap (`store.keys()`/`store.size()` type-check clean but have no runtime backing). Mirror 84/86: add `Keys()` (sorted canonical code-point order) and `Size()` to the go host Map runtime, pinned by a compile→run test. wasm remains n/a (rejects `Map.new` at compile time). This closes host-Map iteration across all six tiers.
91. ✅ **Lower declared function types on the rust tier (harness multi-tier
    proof, finding #20).** The harness's agent loop is a top-level fn over
    effectful callback arrows (`agent_loop(msgs, complete:
    (List[Msg]) -> Str, call_tool: (Str, Str) -> Str, max_steps)`). On
    py and ts this lowers and runs (FR-1's pattern); on rust the emitter
    refuses the whole document: "a declared function type is not
    lowerable on the Rust tier" (docs/function-types.md §5 — rust wants
    `impl Fn(..)` for a param, `Box<dyn Fn(..)>` for an escaping value,
    and revl carries no position distinction). So the string-protocol
    harness (no JSON, no externs — the "runs on all runtimes" proof)
    boots on py/ts but not rust, solely because of this declaration.
    Arrows bound to a local `let` and called in the same function still
    lower (rustc infers the closure type), so the fix is position-aware:
    thread the *call-site* knowledge (param vs local vs field) into the
    rust emitter's fn-type lowering, choosing `impl Fn`/`Box<dyn Fn>` per
    position, or beta-reduce single-use arrow params at the call site as
    the java tier already does. The harness keeps the mtier variant
    (`mtier/`) as the executable repro.
90. ✅ **Item-80 phase 2: let module fns carry the async color (harness
    verification of slice 1+2+3).** Slices 1-3 landed on
    `agent/async-extern-slice1` and were verified against the harness:
    the parser accepts `extern emission async fn`, the A1 coloring refuses
    a sync method reaching an async extern (with the witness chain), and
    the ts emitter produces `async function http_post(...): Promise<string>`
    with awaited call sites that pass `tsc --noEmit` — the harness's
    `Promise<string>` vs `string` blocker is gone. The remaining gap is
    the design's phase-2 rule: a *module fn* body reaching an async extern
    is refused ("cannot carry the async color yet", test_module_fn_...
    refused_phase2). This blocks the harness's agent loop, which funnels
    `model.complete` (async) through the top-level `agent_loop` callback
    arrows — the direct path (`async fn run` calling `emit model.complete`
    inline) compiles and typechecks, but the loop shape needs module fns
    to propagate the color (a fixpoint, as the design §3 table reserves).
    When it lands, the harness's `agent_loop` works unchanged.
92. ✅ **Async function values: a callback of sync function type cannot
    carry the async color (harness verification of item 90, finding
    #21).** Item 90's transitive coloring works for *direct* module-fn
    calls, but the harness's loop passes the async call through a
    callback arrow: `agent_loop(msgs, complete: (List[Msg]) -> Str, ...)`
    with `msgs => emit model.complete(msgs)` at the call site. The
    emitted loop stays sync (`resp = complete(current)` — no await, both
    py and ts), and the arrow returns a coroutine/Promise that leaks to
    `decode_reply`/`json_parse` at runtime ("the JSON object must be
    str..., not coroutine"). Item 90's roadmap text said "the harness's
    `agent_loop` works unchanged" — it does not: the function type
    `(List[Msg]) -> Str` has no async variant, so the fixpoint cannot
    color the callback, and the call site's arrow (which reaches an async
    op) is a sync-typed value. The 90 claim is falsified by execution.
    Fix directions: (a) an async-aware function type
    (`(…) -> Async[Str]` or a `Promise`-style return), so a callback
    whose body reaches an async extern is typed async and the loop awaits
    it; (b) checker-inferred arrow coloring — when an arrow's body
    reaches an async extern, mark the arrow async and require await at
    its use sites (the fixpoint extended to first-class values); or (c)
    the loop declares `complete: (List[Msg]) -> Async[Str]` explicitly.
    Executable repro: the harness's async-migration branch
    (`src/components/agent.rvl`, `agent_loop`), where the mock+loop
    lifecycle tests fail with the coroutine leak while the direct path
    passes.
93. ✅ **Emitted rust for the loop harness has three compile bugs (harness
    multi-tier proof, finding #22).** Item 91's fn-type lowering works
    (the `impl Fn` params emit), but `revl run --backend rust --once` on
    the string-protocol harness fails `cargo build` with 7 errors, three
    distinct emitter bugs:
    (a) **`config` is not in scope in provide-method bodies on rust** —
    `self.sessions.load(config.session_id.clone())` emits `config` with no
    binding (E0425); the rust emitter does not thread the component config
    into provide-method scope the way py/ts do. Minimal shape:
    `component C { config { name: Str } provide s { fn get() =
    config.name } }`.
    (b) **persistent `push` rebinding in a loop moves the value** — the
    loop's `current = current.revl_push(...)` emits `revl_push` taking
    `self` by value, so the RHS consumes `current` before the assignment
    rebinds (E0382 use of moved value). Persistent `push`/`concat` need a
    borrow (`&self`) or a clone in the rebind position.
    (c) **non-Copy ADT and `impl Fn` params used twice** — `is_tool(dec)`
    then `exec_reply(dec, ...)` consume `dec` (non-Copy `ModelReply`), and
    `call_tool: impl Fn(...)` is moved into a call inside the loop; both
    need clones or reference-taking calls.
    Repro: `revl run mtier/*.rvl --backend rust --once` in the harness
    repo (mtier/ is the string-protocol multi-tier proof). Fixes are
    local to backends/rust/emit.py.

94. ✅ **Async function-value erasure leaks on the go and rust tiers (follow-up to item 92).** Item 92 landed async fn values on py/ts; go/rust ERASE the color but render the function-type return wrong: **go** emits `complete func(string) Async[Str]` (invalid Go — `go build` rejects it), **rust** renders the erased async return as `Value` instead of the concrete type (`impl Fn(String) -> Value`). Both are one-line emitter fixes: strip/erase the `Async[T]` wrapper to `T` in each tier fn-type rendering. Conformance is currently green only because the corpus does not exercise an async-typed callback on go/rust; pin a test that does.

95. ✅ **Str and record lowering on the wasm tier — ALREADY DELIVERED by the v3 linear-memory wave (9f4b134/dda2535/07132aa).** Filed off stale roadmap prose; verify-first found the wasm tier already lowers Str/records/lists/variants over a linear-memory ABI (ptr+len strings, offset-slot records), executing on wasmtime (backends/wasm 33 passed). Still refused (documented): Float, Map, fn-values, split/join, config blocks. The real remaining wasm-bridge work is item 41 slice-3 (map this custom layout onto the standard WASI-P2 canonical ABI: cabi_realloc, WIT-typed exports, string lift/lower) — no longer gated on "wasm cannot represent Str/records".

96. ✅ **wasm `_V3Emitter` emits invalid wasm for a function whose whole body is a diverging `if`/`else` statement** (missing trailing `unreachable`) — found during 41-s3 aggregates; affects the NORMAL wasm tier, not just the canonical path. `backends/wasm/emit.py`. Bounded fix.

97. ✅ **`export_wit` emits referenced `record`/`variant` types at the WIT package top level** (invalid WIT — must be inside an `interface`); found during 41-s3 aggregates, worked around locally with a relocate helper. `src/revl/export_wit.py`. Bounded fix.

98. ✅ (fixed via item 150) **Cross-suite pytest isolation: tests/ + backends/wasm/ share golden/temp artifacts.** Running both in ONE pytest process yields ~11 backends/wasm/test_canonical_abi.py failures (reproduces on clean main; each suite passes alone). Not a CI risk today (separate CI jobs), but a real isolation smell (shared temp/golden state). Low priority. Found during items 96/97.

99. ✅ **Timers on go/rust/wasm (follow-on to item 57).** Item 57 landed every/after timers as revertible effects on py+ts; go/rust/wasm refuse honestly. Implement the schedule/cancel + clock-coeffect contract (docs/time-coeffect.md) on go and rust (Clock + TimerHandle mirroring py/ts, unload cancels residue-free). wasm timer emit remains a further follow-on (behind 41 slice-3 service-level).

104. ✅ **`Str.length` (PROPERTY form) on a multibyte string literal returns the UTF-8 byte count, not code points — REAL BUG (earlier not-a-bug call was wrong).** `fn M() -> Int { return "café".length }` const-folds to `(i64.extend_i32_u (i32.load (i32.const 0)))` = byte-len prefix (5), executes to 5 on wasmtime (should be 4). The `.length()` METHOD form is correct (routes through `$str_cp_length`) — my earlier verify tested the method form and missed the property fast-path. Fix the property `.length` fast-path (route through `$str_cp_length` or fold the compile-time code-point count); audit `charAt`/`charCodeAt`/`slice`/`indexOf` property fast-paths for the same byte-vs-code-point fold; reconcile docs/strings.md (line ~52 vs ~263 contradiction). `backends/wasm/emit.py`.

105. ✅ **Remove the dead `_relocate_types_into_interface` workaround in backends/wasm/canonical.py.** Item 97 (5cdc5d9) made `export_wit` emit named types INSIDE the interface (valid WIT) — confirmed on main. The canonical component builder's local relocate transform (canonical.py:685, used ~880) is now redundant; remove it and update `test_aggregate_wit_is_valid_types_inside_interface` to assert export_wit output directly. Keep only if removal breaks the canonical build (report why).

106. ✅ **Re-filed from stranded branch `agent/harness-m3` (never merged; its item numbers 98/99 were later reused on main): async colour does not propagate through spawned-handle emissions in arrow bodies (harness finding #27).** The async-colour analysis (`src/revl/emission_analysis.py::_async_callables`, call-graph based) sees `req`-based emissions (`model.complete` on a `requires`) but not handle-based ones (`w1.wtask.run` on a spawned instance), so an arrow whose body is `emit <handle>.<key>.<method>()` is not flagged async and the py emitter wraps it with `_revl_as_async` (the sync classification) — `await run_one(task)` then yields a coroutine OBJECT ("can only concatenate str (not 'coroutine') to str", "coroutine was never awaited"). Fix: treat `handle.key.method()` like `req.key.method()` in the colour analysis (the spawn handle's service type is known); defensively, `_revl_as_async` should `return await _f(*a)` when `_f` returns a coroutine. Full write-up in branch commits 9369d34/e5476d8; also cherry-pick `dogfood/findings-timers-tiers.md` from e5476d8, which never landed.

107. ✅ **Re-filed from `agent/harness-m3` (finding #28) — REPRODUCED on main 2026-08-24: a `let`/`var` declared type annotation is dropped in the IR, so the wasm tier cannot type an empty list initializer.** `var out: List[Int] = []` type-checks (the checker sees the annotation) and lowers on py/ts, but `revl test --backend wasm` fails with `emitter refused: an untyped empty list literal needs an expected List type`. The annotation exists at check time and is discarded before emission; carry the declared type on the let/var IR node (or pin the empty-literal type from it, as `_pin_empty_literal` does for other positions) so the wasm emitter sees the expected List type. Original analysis in branch commit 017b098. `src/revl/lower.py`, `backends/wasm/emit.py`.

108. ✅ **Packaging: the published wheel only supports compile/audit/mcp — every backend-touching command breaks when installed from PyPI.** The wheel packs `src/revl` alone (pyproject `packages = ["src/revl"]`), but `revl run` (all tiers), `revl test` on non-py tiers, placement, and fault tests resolve emitters and runtimes via `Path(__file__).resolve().parents[2] / "backends"` (src/revl/run.py:740, fault.py:44, run_go.py:63, run_java.py:64, run_wasm.py:57, _process_runner.py:94) — a path that does not exist under site-packages. Also no `[project.scripts]`: `pip install revl` yields no `revl` command while README and most docs write bare `revl compile …` (only `python -m revl` works). Fine from a checkout; broken as a package. Ship the emitters inside the wheel (package data or a `revl.backends` subpackage) with a checkout-first fallback, and add a console script. GATES the PyPI release (the publish workflow exists and would ship this today).

109. ✅ **Stale runnable-backend prose + dead refusal branch.** All six tiers are in `RUNNABLE_BACKENDS` (run_ts/run_go wired), but: `--backend` help still says "ts and go emit but have no run driver yet" (src/revl/__main__.py:1681); README line ~223 still says `--backend {py,rust,java,wasm}` with "ts refused"; and since `KNOWN_BACKENDS == RUNNABLE_BACKENDS` the refusal branch at src/revl/run.py:699–705 and the "(not runnable yet)" suffix at run.py:151 are unreachable — delete them or keep RUNNABLE as the single source the help text renders from. Also: this file's header still says "**Branch:** `v2.0`" though the work lands on main.

110. ✅ LANDED 2026-08-31 (a: .srv.log gone; b: dangling worktrees pruned; c: wheel-freshness gate now in ci.yml + pre_merge.sh via check_site_wheel.py; d: ruff lint job in ci.yml enforced, whole-repo ruff clean) **Repo hygiene sweep (from the 2026-08-24 deep review).** (a) `.srv.log` — a localhost HTTP access log — is committed at the repo root; delete and gitignore. (b) 49 of 51 local branches are fully merged — prune; ~24 worktrees live in /private/tmp and dangle after a reboot (`git worktree prune`). (c) The committed playground wheel (site/vendor) is one src commit stale (built at 237d2ca; src/revl changed in 9451818) and nothing checks it — add a CI freshness gate that rebuilds via `site/build.py` and diffs, or regenerate on src change. (d) 202 `noqa` comments carry ruff codes but no ruff config or CI job exists — pin ruff (F-level currently finds ~35 trivia: 9 unused imports, 6 unused locals e.g. lower.py:4878 `lines`, go/emit.py:1638–1639 `sig_arg`/`call_arg`, 20 placeholder-less f-strings, and a string-annotation `"Env"` undefined at emission_analysis.py:315) and add the job so the existing noqa discipline is enforced.

111. ✅ **Refactor the largest compiler units (low priority, no behaviour change).** `main()` is 889 lines (src/revl/__main__.py:1230) — split into per-subcommand handlers; `_lower_provide` (395 lines), `_lower_component_pure_expr` (351), `_lower_pure_expr` (298), `check_and_lower` (296) in the 4.9k-line src/revl/lower.py factor into per-statement/expression-kind helpers; wasm `_V3Emitter` is a single 2,568-line class (backends/wasm/emit.py). Large expr dispatchers are conventional for compilers — the bar is "each helper testable alone", not a line count. Golden/conformance suites are the safety net; land as mechanical moves, one unit per PR.

112. ✅ **`advance` lifecycle statement fails hard on go/rust lifecycle emitters** (re-filed from agent/harness-m3 finding #29; verification of item 102). Item 102 landed `advance <n><unit>` on py/ts; `revl test --backend rust|go` on the same doc fails `emitter refused: unknown lifecycle step 'advance'` — a HARD FAIL, not an honest `_timer_follow_on`/`_lifecycle_refusal` skip. Item 99 gave go/rust timers, so `advance` should lower on go/rust (drive their Clock) or at minimum skip honestly like timers do. `backends/go/emit.py`, `backends/rust/emit.py` (lifecycle emitters).

113. ✅ **go host `Map` is `map[string]string` only — `Map[Str, Int]` silently mis-lowers to invalid go** (re-filed from agent/harness-m3 finding #30; verification of item 99). `backends/go/emit.py`'s host Map is `type Map struct { m map[string]string }` — string values only. The harness's counters (`Map[Str, Int]`) and ledgers (`Map[Str, List[Msg]]`) emit `Get`/`Insert` against it and `go test` fails `cannot use _v (string) as int64`. Never surfaced until item 99 made go v3 docs build. Carry the actual value type in the go host Map lowering. `backends/go/emit.py`.

114. ✅ **item 101's rust clone fix missed the `_Env` provide-method renderer — a local reused after an emit still moves** (re-filed from agent/harness-m3 finding #31; verification of item 101). `_method_body_lines` clones only PARAMS into the acquire rename (`acquire_rename[param] = param.clone()`), never LOCALS, so `emit sessions.append(sid, Msg { content: answer })` then `return answer` (where `answer` is a `let`-bound local) renders `content: answer` bare and moves (E0382). Item 101 fixed `_V3Ctx` (record fields + direct service args) but not the `_Env` provide-method path. Extend the clone to reused locals in the `_Env` renderer. `backends/rust/emit.py`.

115. ✅ **py erases async externs to blocking `def`s, so a component cannot `await` a host operation** (re-filed from agent/harness-m3 finding #32). `_emit_externs` (`backends/python/emit.py`) emits EVERY extern as a blocking `def`, and the py await-seed excludes externs (emit.py:97-99). An async extern whose host body wants to `await` (dispose a fiber, wait on a promise) is inexpressible on py; ts handles it. Emit async externs as `async def` and let the await-seed include them on py. Collides with items 106/106-family on `backends/python/emit.py` — sequence after. `backends/python/emit.py`.

116. ✅ **`revl import cordis` cannot see DSH's real plugin shapes** (re-filed from agent/harness-m3 finding #33). Three gaps, DSH's real plugins as the corpus: (a) surface recovery matches only `extends (?:Name\.)*Service` — misses Service subclasses with a non-`Service` base name; (b) decorated methods are invisible; (c) named record types across local imports are not transcribed. The cordis importer. Each independently useful.

117. ✅ **A1/coloring is blind to async ops in EXPRESSION positions — a sync context reaching an async op is neither refused nor colored (subsumes harness finding #40; found in item 106).** The A1/async-coloring analysis (`lower.py::_req_op_is_async`/`_arrow_reaches_async`, `emission_analysis.py`) only sees async ops in statement position, not nested in a ternary or an arrow body. Two manifestations: (a) an UNCOLORED arrow whose body reaches an async op (item 106); (b) finding #40 — a SYNC provide method whose ternary returns an async emission (`fn route(p) { return p=="go" ? emit m.complete(p) : "idle" }`) is admitted, and ts emits a sync `route` returning a Promise that the lifecycle driver never awaits -> the assertion sees `{}` (py passes by accident). Per A1 a sync method has NO in-flight window, so the CHECKER should REFUSE sync-reaching-async in any expression position (ternary/arrow), forcing `async` (which item 141 then awaits correctly). Complements item 141 (async-side await). Cam worked around it in web_shell (A1-correct async dispatch, 16/16 py+ts) — this is the soundness fix, not a blocker. Frontend: `lower.py`/`emission_analysis.py`/`typecheck.py`. Queue AFTER item 141.
