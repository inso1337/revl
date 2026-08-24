# findings — FR-5 lifecycle (agent/fr5-lifecycle)

Roadmap item 77(f), first half: lifecycle tests on the non-py tiers +
a pass/skip:reason/fail verdict column for `revl test --all`. The task
source was `~/Projects/revl-harness/FEATURE-REQUESTS.md` FR-5.

## 1. Refusal log

Every `revl compile` rejection I hit — there were almost none, because the
lifecycle language work was already done (lowerer + py driver exist); this
run was emitter-and-runner work in Python, Rust, TS and Go.

- **`lifecycle test` refused by the rust emitter** —
  "lifecycle test 'cache reverts cleanly' is not lowerable on the cordis-rs
  tier: it drives a live composition (load/call/unload) and asserts R4
  residue-freedom through the host runtime's introspection, which only the
  reference tier implements — run it with `revl test --backend py`".
  Verdict: **`caught-bug`-adjacent / `gap`** — the refusal was the honest
  fence the project's "silently-dropped construct" rule demands; it just
  described a gap that FR-5 closes. Removed by lowering the construct.
- **go: "unsupported v3 statement step 'load'"** — a lifecycle document on
  the go tier hit the pure-v3 path (the routing sends components+tests
  documents there), which had never seen a lifecycle step. Verdict:
  `friction` — the message names the step but not the routing; the fix is
  the routing change, not a new statement kind.
- **rust `#[test]` fns racing on the R1 counter** — after adding the
  process-wide live-resource counter to the emitted host stubs, `cargo
  test` flaked: `test revl_lifecycle_a_reloaded_cache_starts_empty` ran
  first (parallel threads), and `cache reverts cleanly` failed its
  no-residue check because the *other* test's Pool/Map acquisitions were
  visible in the shared static. Verdict: `caught-bug` (my own) — fixed by
  making the counter thread-local (`thread_local!`); a clean test now
  observes only its own acquisitions. This is the kind of cross-test
  contamination a py-only runner never sees, because py runs tests
  in-process sequentially.
- **ts: `assert stale == None` failed with `left = null, right =
  undefined`** — the host `MapHandle.get` returned `null` for a missing key
  while the tier's Opt is bare `value | undefined`. `revlEq(null,
  undefined)` is false, so a *clean* reload test failed. Verdict:
  `caught-bug` — the emitter's own comment says "Map.get answers undefined
  when absent: exactly the Opt None case" (stdlib path); the host path
  disagreed. Fixed `MapHandle.get` to return `undefined`. The interop
  bridge canonicalizes both to JSON null on the wire, so the seam was
  masking it. **This is the tier gap FR-6's FR-7 predicts: the emitted
  component was type-unsound (`get(): any`) and only the lifecycle test
  crossed the null/undefined boundary.**
- **go: v3-mode components using Pool did not compile** —
  `cannot use cfg.PoolSize (variable of type int64) as int value in
  argument to PoolOpen` — the host runtime's Pool used `int` while v3-mode
  component Int is `int64`. Verdict: `caught-bug` (pre-existing, surfaced
  by the lifecycle path — the pure-v3 routing had hidden it). Fixed by
  making the host runtime's Int width mode-aware.

## 2. Friction log

- `[slow]` **go emit errors as Python `%`-format errors.** My first two
  go-emitter bugs were `%`-format-string crashes (`not enough arguments
  for format string`) — emitted Go text contains literal `%v`/`%d` that
  must be doubled inside Python `%`-format strings. No test catches this
  until the emitter runs; a `pytest` unit that emits the lifecycle example
  would have caught all of them in one shot.
- `[slow]` **`.expect("...")` messages must be Rust string literals.** The
  first rust emission produced `expect(lifecycle test "cache reverts
  cleanly": ...)` — an expression, not a string. Same class as above:
  emitter string-building is the fiddly part.
- `[slow]` **go `Fatalf(msg, err)` without a format verb is a vet error.**
  Go 1.26's `go test` vet rejects `Fatalf("msg", err)` — the message must
  be `Fatalf("%s: %v", msg, err)`. A compile-only validator would miss it
  (vet runs under `go test`).
- `[nit]` **gofmt expands long single-line function bodies.** My emitted
  `revlHostLive` single-liner got reformatted by `regen.sh`'s gofmt pass,
  breaking the whitespace-normalized byte-identity check (semicolons
  aren't whitespace). Emitting multi-line from the start fixed it.
- `[slow]` **`cargo test` parallelism is invisible from the py mindset.**
  The R1 counter race (above) took a debug-instrument run to find; the
  fix (thread-local) is one line but the diagnosis cost a full crate
  build cycle.
- `[nit]` **go stc-go `Get` vs `Service` divergence.** `root.Get(key)`
  walks the context's own values; `stc.Service[T](root, key)` resolves
  through the orchestrator's provision table. The runner uses `Service`;
  my first prototype used `Get` and got nil. Nothing documents which one a
  caller outside a component should use.

## 3. What revl gave you

- **The checker's G2 provision-disjointness made the lifecycle IR
  trustworthy.** I never had to validate that a `call` step's provider was
  loaded — the lowerer already rejects a call through an unloaded key at
  compile time. The emitters could assume the script is sound and focus on
  the driver.
- **The refusal-by-name convention paid off twice.** The rust/ts
  lifecycle refusals (and the go routing hole) were loud, searchable, and
  testable — `test_other_tiers_refuse_by_name` pinned them, so removing
  the refusal on rust/ts was a test-visible event, and the remaining
  java/wasm refusals became skip reasons with the refusal's own text.
- **The `run --once` no-residue shape was directly reusable.** The rust
  driver's `registry().len() == 0 && reflect().services().len() == 0`
  became the tier-neutral assertion everywhere (ts via `snapshotRuntime`,
  go via `len(root.Fibers()) == 0`) — I did not have to invent a residue
  check per tier, only translate the same R4 clause to each runtime.
- **The py reference driver set the test-shape bar.** The three negative
  fixtures (leaky undo, left-loaded, wrong assertion) in
  test_lifecycle_exec.py told me exactly what each tier's driver must
  also fail on — an assertion that only passes is not an assertion.

## 4. Time-to-green

Roughly 14 compile→refuse→fix cycles across the four tiers. Longest single
stall: the go emitter `%`-format and `Fatalf` vet class (~4 cycles), and
the rust thread-local race (1 debug-instrumented build). A pytest unit that
emits the lifecycle example per tier and greps the output would have
caught the string-literal classes in one run.

## 5. Cost ledger

- `tooling` ×3 — `%`-format escapes, `.expect()` string literals,
  `Fatalf` vet: emitted-text assembly is where the friction lives; a
  "emit the example and eyeball it" test would have been cheaper than the
  subprocess runs that surfaced each (the cargo/vitest/go runs re-build
  the world per attempt).
- `tooling` — the rust thread-local race: needed an instrumented crate
  build to see the values; a `--nocapture` habit would have shortened it.
- `docs-gap` — go `Get` vs `Service` resolution: read the stc-go source
  to learn the provision read a lifecycle test must use.
- `spec-ambiguity` — ts host `Map` null vs `undefined`: the emitter's
  own comment contradicted the host object; the tier's Opt definition
  (`value | undefined`) is the arbiter, but it took a failing test to
  notice.
- `missing-feature` — none: every tier gap was a bug or a routing hole,
  not an absent language feature.

The single change that would have cut the most cost: a
`pytest` unit that emits `examples/lifecycle_cache.rvl` and
`examples/lifecycle_leak.rvl` on every tier and greps for the driver
markers — it would have caught the string-literal and vet classes before
any subprocess run.

## What is left (honest, per FR-5's gate)

- **java (cordis4j)** and **wasm** still refuse lifecycle tests by name.
  `--all` reports the refusal as `skip:` with the reason; lowering them is
  a documented follow-up (wasm has no live runtime at all; java needs a
  lifecycle driver against the real cordis4j, where the java tests already
  drive emitted components on the JVM — test_emit_java.py's runtime
  scenarios are the starting point).
- **go's lifecycle Opt asserts are shape-limited**: `bind == Some(v)` /
  `bind == None` lower explicitly; an Opt binding used in any other shape
  refuses with a message (the tier carries Opt in return position only).
- **ts host Map semantics**: the null→undefined fix aligns the host with
  the tier's Opt representation; no follow-up.
- **R1 host-resource pairing** is now asserted on py/ts/rust/go (each over
  its own host vocabulary); the rust and go counters are thread/package
  isolated so parallel tests do not contaminate each other.
