# Roadmap archive: Open, in rough priority order

Closed items moved verbatim from [docs/v2.0-roadmap.md](../v2.0-roadmap.md). The tools read this file from its "## " heading on.

## Open, in rough priority order

1. ✅ **Fix what the conformance matrix's second half found.** The blind spot
   itself is closed: `tools/validate.py` compiles or validates every tier's
   emitted output (python `compile()` + scope walk, `tsc`, `cargo check`,
   `javac`, `wasmtime compile`), `tests/test_conformance_validate.py` gates
   it against a shrink-only baseline, and a `conformance` CI job installs
   all five toolchains at once — `tools/conformance.py --check-toolchains`
   fails that job if any tier would silently report `unavailable`, since
   "nothing checked it" must never read as "clean". ✅ **The 16 baselined
   failures are also closed** — the shared `await`/method-time cause was the
   host-builtin contract (host calls untyped in the frontend, so `Job.run(1)`
   passed an `Int` where a name belongs) plus a `T`→`Opt[T]` injection never
   materialized and java generics carrying primitives. `KNOWN_FAILURES` is
   now empty; every tier's emitted code is accepted by its own compiler
   (python 51/51, typescript 51/51, rust 48/48, java 48/48, wasm 32/32).
2. ✅ **One expression renderer per backend.** typescript, rust and java each
   converged their v1/component renderer and their 2.0 `_v3_expr` into one
   function that dispatches the ambiguous `call` kind on *shape*, not kind
   (python and wasm were already single-renderer). Verified behaviour-
   preserving: java and typescript emit byte-identical output across all 51
   conformance constructs; rust changed exactly two (`(Some)(x)`→`Some(x)`
   and a string-literal unification, both improvements that pass `cargo
   check`). The rust refactor also removed a latent field-callee wrong-shape
   read. All five tiers still validate 100% under their real compilers.
3. ✅ **Lifecycle tests** (§2 below) — `lifecycle test` asserts no-residue
   over a *live* composition from inside the language, and `fault test`
   injects a failure and asserts L-Raise/no-residue at a chosen step. Both
   lower and execute on the python tier; the other four refuse loudly.
4. ✅ **Cross-tier `test` execution** (§3) — `revl test --backend …|--all`
   reusing each backend's existing runner; a tier whose toolchain is absent
   skips with a reason, never as passing.
5. ✅ **Uniform component IR dialect** — closed by 6f86b34's design and
   re-verified by execution (see the ✅ entry in the conformance-matrix
   section above): `Some(x)`/`None` lower to one shape in every position —
   fn bodies, component setup, provide methods, `test` blocks — and tagged
   construction always lowers to `adt`. Backends emit that one dialect; they
   do not normalize two.
6. ✅ **Explicit generic declaration syntax** — `fn id[T](x: T) -> T` (and
   `pub fn`, `extern`) now parse a `[T, U]` type-parameter list and feed it
   into the same `collect_tparams`/`unify` machinery the implicit form uses:
   an explicit name becomes a type parameter even when the single-uppercase
   heuristic would miss it, unifies at each call site, and its marker never
   reaches the IR (implicit and explicit emit byte-identical IR, pinned by a
   test). Shadowing a declared type (`type S = A | B` then `[S]`) is rejected
   rather than silently reopening the wildcard hole. Provide-methods stay
   annotation-free (separate check path). Docs: docs/generics.md.
7. ✅ **Test-hygiene hazards** — closed, and each one now has a gate rather
   than a convention.
   - *TypeScript*: the emitted `test` blocks are checked in, so a cold
     checkout collects what a warm one does (41 = 41). `tests/
     generated_coverage.test.ts` pins it: the generated module must be
     git-tracked, and it must contain one `it(` per `test` block the fixture
     declares, by name. Checking a file in is a convention; this fails if
     the convention lapses or the emitter quietly stops lowering tests.
   - *wasm*: CI installs a pinned wasmtime, asserts the binary runs, and
     sets `REVL_REQUIRE_WASMTIME=1` — with the flag set, a missing runtime
     is a failure instead of a skip. A junit check then fails the job if
     *any* wasm test skipped, which covers `tests/test_wasm_backend.py` too.
     Local dev without the flag still skips cleanly.
   - *rust*: the cargo gates resolve `--offline` first and fall back to the
     networked resolve only when the offline attempt failed for a
     *resolution* reason. CI (empty registry) fetches as before; an offline
     machine with a warm `~/.cargo` runs all ten gates for real — 26 passed
     in ~43s where the previous skip-if-unreachable rule ran none of them.
     `test_offline_fallback_never_launders_a_real_failure` pins the safety
     property: a compile error, a failing `#[test]` or a panic is never
     reclassified as "retry with network".
   - *(found on the way)* the TypeScript suite's nested `vitest run` sat
     inside vitest's 5s default timeout while spawning node plus six
     `emit.py` processes, so it failed as "Test timed out" under load —
     roughly one run in three, and worse with every file added. The clock is
     now 60s; the assertion is unchanged.
8. ✅ **Import codegen family** (§4) — complete: `revl mcp import`,
   `revl import wit` (39 tests) and `revl import openapi` (85 tests). All
   three default an imported operation to `emission` and weaken it only on
   explicit evidence, because a service declaration is an upper bound on its
   providers (G4) and an under-declared operation breaks every consumer's G8
   audit. OpenAPI is the first member whose document carries real evidence —
   RFC 9110 makes GET/HEAD/OPTIONS/TRACE safe — and the generated header says
   plainly that safe-by-spec is the author's claim, not a proof, and that
   idempotent (PUT/DELETE) is not reversible.
9. ✅ **Service versioning** (§5) before any component repository. **Half
   done:** the admission gate now checks *structural interface
   compatibility* (paper §6.6, docs/service-compat.md) instead of exact
   match — a hot-swap is admitted iff every running consumer's call site
   stays valid (a retained provider pins the interface via A6; a replaced
   one may add methods, widen params, narrow returns, drop an emission,
   but never make one *appear* — that would silently cross the boundary
   for an unmarked consumer, G4/G8). **Deferred:** key namespacing (needs
   surface syntax that collides with active parser work), and
   `plan._interface_drift` still previews drift with a plain `!=`.
11. ✅ **`Int` widens into a `Float` position, and the IR now says where.** The
    last open item in arithmetic — a *wrong answer* on TypeScript, not a
    refusal — is closed. The frontend now marks each coercion site on the
    node (`"widen": "Float"`, additive like `operands`, no `ir_version` bump:
    a coercion site needs a declared `Float` position, which only full v3
    sources can express, so the v1/v2/v3 reference documents stay
    byte-identical), and every backend emits the conversion outright
    (`float(3)`, `Number(3n)`, `(3i64 as f64)`, `float64(3)`,
    `((double) (3L))`). rust no longer refuses with E0308 and TypeScript no
    longer computes the wrong answer; wasm still refuses `Float` as before.

    `compatible("Float", "Int")` is true, so `1.5 + 2` and `ident(3)` (for
    `fn ident(x: Float) -> Float`) both type-check. The `bin` node carries
    `operands`, so the **arithmetic** case was always specifiable; a `call`
    argument, a `let` value and a `return` expression reached a backend as a
    bare `lit`/`var` with no declared type, and the tiers that keep `Int` and
    `Float` apart split:

    | tier | before | now |
    |---|---|---|
    | python, go, java | a host rule absorbed it | explicit conversion emitted |
    | rust | **E0308** — a loud compile error | `(3i64 as f64)` |
    | typescript | **wrong answer** — `3n === 3` is false | `Number(3n)` |

    Pinned positively in `tests/test_cross_tier_execution.py` (the marker is
    asserted present in the IR, each tier's conversion text is pinned, and
    the old `DIVERGENCES` entry is closed with the probe now *passing* on
    every tier that can run it). Written up in docs/arithmetic.md.

    Found by the TypeScript BigInt port, which is the pattern worth noting:
    it was invisible while `Int` and `Float` were both JS `number`, and three
    tiers hid it behind a host rule.

12. ✅ **Sized and unbounded integers** — `Int` is now 64-bit two's complement
    with trapping overflow on all six tiers (docs/arithmetic.md), which
    leaves two named types specified-but-unbuilt, both additive:

    - `Int32`, for density. Measured, not assumed: i32 and i64 scalar
      arithmetic are identical (0.99 vs 0.98 ns/iter here), but a
      *vectorisable* loop is ~34% faster in i32 because SIMD gets twice the
      lanes. That is the case a static `Int32` serves — and the case a
      dynamically-promoting integer could never serve, since a promotable
      array cannot be stored flat.
    - `Integer`, arbitrary precision. Viable everywhere via a tagged
      small-value representation (rust `ibig`/`malachite` store small values
      inline; wasm can tag a pointer and keep the bignum *inside* linear
      memory, so confinement holds). Cost is concentrated in wasm, which
      would need a bignum in WAT.

    Trapping costs ~9% on scalar arithmetic and much more on vectorisable
    loops, where the check defeats auto-vectorisation — which is the argument
    for `Int32` rather than for making the default unsafe.

    Landed: `Int32` is complete on all six tiers (32-bit trapping, coercions
    pinned — implicit widen to `Int`, explicit checked narrow — with 3 rejection
    fixtures). `Integer` is **fenced, not built**: documented in
    docs/contract-errata.md + docs/integer-proposal.md rather than half-shipped
    (the wasm bignum-in-WAT is the concentrated cost). That second half remains.

13. ✅ **A total division form** — `checked_div_trunc`/`checked_div_floor`/
    `checked_div_euclid` and `checked_mod` return `Result[Int, Str]` on all
    six tiers (`Ok` on success, `Err` on a zero divisor or the `Int.MIN / -1`
    overflow), so a program can handle a zero divisor without faulting and
    without extending `fail` into the pure stratum — totality, the property
    `verified` rests on, is preserved. Documented in docs/arithmetic.md
    (total forms table); a literal zero divisor on the partial forms remains
    a compile error and a computed one still faults uniformly.

14. ✅ **`revl import cordis` — a fourth import-family member.** Landed: `revl import cordis` (src/revl/import_cordis.py) scans a Cordis (TS) plugin's `inject`/`provide` surface into revl `extern service` decls, defaults ops to `emission` and weakens only on explicit `@revl:pure`/`--pure` evidence, and refuses unrecoverable signatures loudly (or `// UNRECOVERED` under `--mark-unrecovered`). docs/import-cordis.md, 23 tests. The import
    family (§4: MCP, WIT, OpenAPI) wraps *specified* interfaces. The runtime
    family revl actually compiles for now has a live plugin ecosystem worth
    wrapping: Cordis (TS) plugins — Koishi's marketplace, and since 2026-08
    DeepSeek Harness, whose plugin kernel *is* Cordis. An importer that reads
    a Cordis plugin's `inject`/`provide` surface and generates revl
    `extern service` declarations would let a revl composition consume real
    Cordis/DSH plugins as coeffects. The established rules generalize: an
    imported operation defaults to `emission` and is weakened only on
    explicit evidence (G4 upper bound, same reasoning §4 records for
    WIT/OpenAPI), and the generated header states what is claimed vs.
    proven. The honest hard part is that Cordis plugins are untyped TS —
    recovering signatures and undo semantics is the work, and where a
    signature cannot be recovered the import must say so loudly rather than
    guess (the "nothing checked it must never read as clean" rule applies to
    interfaces too).

15. ✅ **Composition persistence.** Landed: `revl_snapshot`/`revl_restore` (src/revl/mcp/persist.py) serialize the admitted sources + manifest to JSON; restore recompiles every source through `compile_source` and the gate (never rehydrates live objects), raising `RestoreError` on any component a newer checker now rejects. CLI `revl mcp serve --restore SNAPSHOT.json`; docs/persistence.md, 12 tests. The MCP session's evolved composition
    (`mcp/session.py`) is in-memory: an admitted generation does not survive
    a restart, so self-evolution (§1b) is durable only for the life of the
    process. A snapshot/restore of the admitted state — the sources of the
    currently-admitted components plus the manifest, enough to *re-admit*
    them through the same gate on boot, not a pickle of live objects — makes
    an evolved composition a persistent artifact. Restore must replay
    admission (compile + gate), never bypass it: a snapshot taken under an
    older checker must not smuggle a now-rejected component past a newer
    one. The replay accumulator (backends/python/replay.py) already records
    the effect history and is most of the machinery for the state half.

16. ✅ **Admission latency as a measured number.** Landed: `bench/admission_latency.py` measures the in-memory `compile_source` → admission-gate round-trip — median 0.165 ms (gate ~0.017 ms) on a representative component, recorded in bench/results/admission-latency.md with a guard test (correctness + 20 ms regression ceiling). If the gate runs inside an
    agent loop (per-candidate, per-generation — the §1b/lighthouse shape),
    compile-plus-admit time per component is a product property, not an
    implementation detail. `bench/` exists; add a benched figure for the
    in-memory `compile_source` → admission-gate round-trip on a
    representative component, tracked so a regression is visible. One
    number, honestly measured, is also the pitch: "admission costs X ms."

17. ✅ **Split `src/revl/lower.py`.** 192 KB and growing — lowering, the
    admission gate, service compatibility, emission analysis and
    spawn/instance support in one file, ~29% of the frontend. A mechanical
    split (`lower/` package, or `admission.py` / `service_compat.py` /
    `emission_analysis.py` peeled off) before instance-parametric phase 2
    (item 10) lands more weight on it. Behaviour-preserving by construction:
    the full frontend suite and the byte-identical v1 goldens are the gate.

18. ✅ **Residue probe for foreign Cordis plugins — the adoption wedge.**
    `revl run` already *proves* no-residue on exit for revl-authored
    compositions (registry, provisions, disposables, listeners back to
    baseline). Point the same contract at plugins revl did not author: a
    standalone harness that mounts/unmounts **any existing Cordis (TS)
    plugin** N cycles and reports what leaked. The py driver's baseline
    proof is the model, not the code — the probe is new TS-side work
    implementing the same before/after contract against the runtime DSH
    and Koishi plugins actually run on. Why it leads this batch: it
    delivers value with zero language adoption (a lifecycle linter for the
    ecosystem revl compiles for), and it *generates the evidence* items
    19–20 and the eventual paper need — "N plugins × M cycles, X% leak
    listeners" is the evaluation section, the write-up, and the demand
    creation ("the same plugin as a revl component makes this leak a
    compile error") in one artifact. Timing is part of the case: DSH's
    developer preview shipped 2026-08 and the attention window will not
    stay open.

19. ✅ **Fix the cordis-py A8 async-body gap upstream.** The largest open
    errata entry on the flagship tier: an async effect-setup failure runs
    its inverses LIFO with no residue (containment holds) but the fiber
    lands `ACTIVE` instead of `FAILED` — A8's "lands FAILED with the error
    recorded" is dropped for async bodies (docs/contract-errata.md;
    reproduced with hand-built IR, confirmed tier-side, sync bodies
    unaffected). The groundwork is done and the fix belongs in the runtime,
    not the emitter: PR against geohotstan/cordis-py (an upstream follow-up
    path is already noted in the errata), routing an async setup failure to
    the fiber's error slot instead of the auto-dispose guard. The replay
    test already asserts *neutrality* rather than pinning the bug, so it
    flips green when the runtime is fixed. Exit: the errata entry closes
    and the fault-test harness stops reporting the wrong state.

20. ✅ **Bench the paradigm, not just the syntax.** `bench/` measures which
    *revl syntax* models write best (30 specs × {v1, v2, v2host},
    compile-rate and iterations-to-green). Add the variant that measures
    whether *revl itself* earns its keep: the same 30 specs authored as raw
    Cordis TS plugins, scored on **lifecycle correctness** — with the item
    18 residue probe as the oracle — not on compile rate alone (raw TS
    always "compiles"; the question is what it leaks). The deliverable is
    the number the whole pitch rests on: agents converge in K iterations
    under guarantee-naming diagnostics, and Z% of their raw-TS attempts
    carry residue revl would have refused at compile time. Infra exists
    (run.py, the retry-on-diagnostic loop, committed corpora, free
    rescoring); the new work is the TS prompt/scoring path and the probe
    dependency.

21. ✅ **`revl audit --diff`: the authority-drift gate.** `audit --json`
    exists; add a mode that takes a previous audit and **fails when a new
    generation adds boundary crossings** (new emissions, new externs)
    without an explicit ack. This is the second axis of the agent-gate
    story and it is deliberately not admission's job: admission (§5)
    checks *correctness* — every running consumer stays valid — while
    audit-diff checks *authority* — a regenerated component cannot quietly
    widen what it reaches outside the system between generations. Small
    surface (a diff over the G8 boundary table, exit code + named
    additions), sized in days, and it completes what the lighthouse entry
    (Toward early production §3) calls the review surface.

22. ✅ **Hygiene sweep.** Small, all real: the README badge still says
    `runtimes-5` (and the repo description "five verified backends") with
    six backends in-tree; a stray untracked `.html` sits at the repo root;
    `backends/typescript/tests/generated/conformance.ts` has an
    uncommitted working-tree drift that needs a decision (regenerate or
    commit); and ~40 local branches from merged waves want pruning. None
    of it blocks anything — batched here so it stops being invisible.

23. ✅ **Verified live migration across tiers — `revl swap <component>
    --to <backend>` on a running placement.** Placement got the tiers into
    one lifecycle (§5 above: py ↔ node ↔ rust ↔ java ↔ go, symmetric,
    reactive); this makes membership *changeable* while it runs. The
    operation: boot the candidate provider in its own process on the
    target tier, run it through the admission gate against the running
    manifest (§5 structural compatibility — every consumer's call site
    stays valid), re-point the consumers' proxies from the old socket to
    the new one, then tear the old provider down LIFO and prove no
    residue. Every ingredient exists and none are wired to each other:
    hot-swap is py-tier-only (`--watch`, MCP `revl_swap`), placement is a
    static assignment at boot, and the seam's canonical wire encoding
    (`51d15ec`) means a candidate on another tier already speaks the
    protocol. The genuinely new engineering is the **handover**: today the
    per-proxy monitor thread knows only how to *dispose* on peer death —
    teach it to reconnect to a successor instead, so cutover is a
    re-point, not a withdrawal blip. In-flight calls at cutover: drain the
    old provider, honestly, as v1 — mid-stream handoff is a later
    refinement, not a blocker.

    A second verification axis comes almost free once item 18/replay
    mature: record a timeline against the old provider, re-drive the
    recorded calls against the candidate, and compare canonically-encoded
    results *before* cutover — interface-level safety from admission,
    behavior-level safety from replay, so "the shadow agrees" is a checked
    verdict rather than a dashboard. Separable; ship the swap first.

    Why this outranks its cost: it converts the six-backend investment
    from conformance discipline into the product — the backends stop
    being six ways to deploy revl and become six tiers one live system
    can move between. No other system has the category (Erlang hot
    upgrade is single-language and unverified relative to consumers;
    rolling deploys swap processes with no interface check and no residue
    proof), and it is the strangler-fig migration — the one moment an
    organization willingly adopts new glue — as a verified, *reversible*
    operation. Demo/exit test: a composition serving a live REPL on py;
    `revl swap UserCache --to rust`; the REPL answers across the cutover;
    the old provider unwinds with the no-residue proof printed; then swap
    it back.
 

    **+ upgrade simulation against history (external proposal #12, folded here).** `revl simulate-swap old.rvl new.rvl --history run.jsonl` replays recorded interactions against BOTH versions and compares returned values, capability crossings, lifecycle transitions, teardown, resource usage, error classes. Structural compat says 'it can connect'; simulation asks 'does it behave acceptably on real historical workloads'.

24. ✅ **Threat-model the gate itself; harden it against hostile input.** The
    positioning items 18–21 and the lighthouse entry build on — revl as
    the admission gate for agent-generated components — means the gate
    runs on adversarial input *by definition*. The mindset already exists
    in one place (`tests/test_mcp_hint_adversarial.py` attacks the G4
    read-only-hint proof with emissions hidden behind first-class function
    values); generalize it into a program:

    - **`docs/threat-model.md`** — what the gate defends against, and the
      explicit non-goals stated before someone "discovers" them: `extern`
      host blocks are arbitrary code by design (the gate *surfaces* them
      on the G8 audit surface; it does not sandbox them — the deployer
      reviews that boundary), and admission promises consumer validity and
      lifecycle guarantees, not that a well-typed component is benign.
    - **Fuzz the frontend.** `compile_source` runs inside the harness
      process via the MCP server, so a pathological source (deep nesting,
      adversarial unicode, gigantic literals, degenerate generics) that
      hangs or crashes the compiler is a DoS on the very system the gate
      protects. The bar, property-tested: reject or error cleanly —
      never hang, never crash.
    - **Resource bounds on admission** — source-size caps and compile
      timeouts, so an agent regeneration loop cannot starve the gate.
      Pairs with item 16: the same latency measurement supplies the
      budget.

    Exit: the threat-model doc exists and is linked from the MCP/agent
    docs, and the fuzz suite runs in CI with the same "a skip is loud"
    discipline as the rest.

25. ✅ **Placement in CI — prerequisite for item 23.** `ci.yml` runs no
    placement job: the cross-tier seams (py ↔ node ↔ rust ↔ java ↔ go)
    are exercised only where a local machine happens to have the
    toolchains, so "verified end to end" is not yet "verified on every
    commit". The conformance job already installs all five toolchains;
    add a `revl run --placement … --once` smoke across the seams to the
    same job. Sequenced **before** work on item 23 starts — otherwise
    every handover bug will be indistinguishable from a pre-existing seam
    regression nothing was guarding.

26. ✅ **`verified effect` — pay the spec's own debt.** The language's biggest
    honest concession (docs/vision.md; docs/replay.md §4.1): the checker
    proves an inverse is *present and well-shaped*, never that it is
    correct — "an `undo` that is wrong, partial, or a no-op" is explicitly
    not caught. syntax-2.0 §7 already *specifies* the answer (`verified
    effect` — the checker must find or generate the evidence that the undo
    undoes); the §7 ✅ above covers only the totality tier for pure `fn`,
    so this is specified-but-unbuilt with no item tracking it. Until now.

    The buildable middle between "undecidable in general" and "nothing":
    **inverse round-trip testing**. For a `verified effect`, the test
    runner auto-generates the property check — snapshot observable state,
    run the effect, run the inverse, assert the state fingerprint matches
    — N randomized rounds, on the real runtime. Not a proof, and the
    report must say so (the same honesty rule as OpenAPI's "safe-by-spec
    is the author's claim"): a test the author did not write, upgrading
    "trust the author's undo" to "this undo survived N round-trips".
    Scope honestly per replay §4.1: in-process observable state only;
    aliased references, external effects and clock-derived values stay
    out of reach and the generated test's header names them. Why it
    outranks everything nearby: it converts the paradigm's weakest link
    into a checked tier, it is what makes replay's step-back trustworthy
    rather than merely mechanical, it feeds item 18 (a foreign plugin's
    disposal can be round-tripped the same way) — and it is the feature
    Cordis-the-library structurally cannot have, because only a compiler
    that knows which expression is the effect and which is the inverse
    can generate the test.

27. ✅ **Runtime "why" — causal lifecycle traces, and prediction-vs-actuality
    as a new oracle.** The `withdraw` query is an *exact prediction* (G2
    uniqueness + G3 acyclicity: no "maybe it resolves elsewhere"), and
    why-traces explain compile-time rejections — but the runtime trace
    records events with no causes: when a cascade actually fires, nothing
    in the `revl run` stream says *UserCache unloaded because Database
    withdrew because the provider process died*. Two halves:

    - **Causal traces**: every lifecycle transition in the run/trace
      stream carries its cause chain, queryable post-hoc
      (`revl why <component> --trace run.jsonl`). The system explains
      itself — possible here and nowhere else because the dependency
      graph is a checked artifact, not an APM's guess.
    - **The oracle**: the static query predicts the exact cascade set and
      order; the causal trace records the actual one; diff them. Every
      real withdrawal becomes a free conformance check that the runtime
      does what the compiler computed — the differential-parser-oracle
      move (selfhost-findings.md), applied to the runtime. A disagreement
      is always a real defect in one of them.

28. ✅ **Publish the manifest + audit JSON as a versioned interchange
    format.** DESIGN §10's own open question, unanswered since it was
    written: "How much of the linker's manifest should be a stable,
    documented format? (It is the natural interchange point with agent
    harnesses that want to admit components at runtime.)" The lighthouse
    positioning makes the answer urgent: with a versioned schema and a
    documented compatibility promise, **other tools consume the gate's
    verdicts without running revl** — a harness reads what a component
    provides/requires/reaches as a standard artifact. The work is small
    (the JSON exists; schema + versioning promise + docs) and it
    compounds items 14, 18 and 23. The strategic point, stated plainly:
    whoever publishes the format for "verified component" defines the
    category.

29. ✅ **Realm erasure report — compliance as a compiler artifact.** Every
    ingredient exists separately: realms isolate tenants
    (examples/tenants.rvl, correct on every tier including the cordis4j
    fix), `withdraw`'s `survivors` set answers "did this touch the other
    tenant" exactly, the lifecycle machinery proves no-residue on unload,
    and `audit` enumerates boundary crossings with compensation status.
    Compose them into one command — `revl erase-report --realm <r>` — and
    the output is an auditor-facing artifact: in-process state provably
    gone (the no-residue proof), every boundary crossing the realm's
    components made listed as compensated vs. bare, other realms provably
    untouched (`survivors`). The report states its own honest scope in
    its header (compensation is not inversion, paper §6.1 — external
    observers already saw the data; the report *enumerates* that
    exposure, it does not undo it). Nobody generates right-to-erasure
    evidence from a type system today; this is a report generator over
    machinery that already works.

30. ✅ **Fault sweep — from "fail at a chosen step" to "fail at every
    step".** A `fault test` today injects failure at one author-chosen
    point (`fail at step 3` / `fail at effect db`) and asserts
    L-Raise/no-residue/LIFO/siblings there. The compiler already knows
    the complete step list from the IR, so the enumeration is mechanical:
    auto-generate the injection at **every step and every effect**, run
    the full assertion set at each, on every tier that can execute. A
    component with N steps gets N fault tests the author never wrote,
    and the claim upgrades from "A8 holds at the point I thought to
    check" to "**no mid-life failure point exists where this component
    leaves residue**" — an exhaustive verdict, which is what the paper's
    evaluation and the lighthouse pitch both want ("we don't sample
    failure points; we sweep them"). Nearly free: the fault-test
    machinery does the hard work; the sweep is a generator loop plus a
    report that names any step it could not reach. Horizon, recorded not
    promised: seeded clock/random coeffects and async-interleaving
    exploration are the full deterministic-simulation version of this —
    worth building only once async component bodies are common; the
    sequential sweep captures most of the value now.

31. ✅ **`revl_gauntlet` — the proving ground as one MCP verb.** Today the
    agent loop is binary: `revl_swap` admits or refuses. The gauntlet
    makes it graded: **candidate in → dossier out**. One verb that takes
    a candidate component and, in an isolated scratch session the live
    composition never sees, runs the full battery — the admission gate
    (§5), a lifecycle test (no-residue over a real boot/unload), the
    item 30 fault sweep at every step, and item 26's inverse round-trips
    — then returns a structured verdict dossier separating what was
    *proved* (admission, derived teardown), what was *tested* (sweep,
    round-trips, with counts), and what remains *claimed* (the G8 extern
    boundary, enumerated). The sentence that is the feature: admission
    proves a candidate *may* run; the gauntlet proves it *does* run
    correctly — before it touches anything. Sequencing is honest: this
    is the umbrella over items 26 and 30 and lands after them, but its
    dossier shape should be designed **now** so both build toward it —
    and that dossier is exactly the payload item 28's interchange format
    should carry.

32. ✅ **Fill specs — hole-directed generation for agents.** Typed holes
    are already 60% of an agent feature: `revl_check` returns open holes
    (`{file, line, expected, guarantee}`), `ok: true` with holes means
    *checked, not admissible*, and admission refuses drafts (docs/
    holes.md). The missing 40%: the obligation tells the agent *that* it
    owes something, not *what it has to work with*. Enrich each hole
    into a **fill spec** — expected type, the capability upper bound at
    that position (may this expression emit, and within which bound),
    the in-scope bindings with their types, and the signatures of
    reachable services. Everything the checker already knows at the
    hole's position, serialized. The structural effect on the loop:
    instead of generate-whole → refuse → regenerate, an agent scaffolds
    with holes and fills them one at a time, each fill constrained by a
    spec that makes most wrong answers unrepresentable. Measurable in
    the house's own instrument: a `bench/` variant — "scaffold with
    holes, then fill" vs whole-component generation, same 30 specs —
    is the experiment, and item 20's harness scores it.

33. ✅ **Boundary policy — the third leg of the gate.** Everything on the
    G8 surface answers "what *does* this reach?"; nothing lets the
    composition operator state "what *may* anything here reach". Add a
    policy file evaluated against the audit graph at admission:
    deny/allow rules over capabilities, per component pattern and per
    realm (including "tenants never reach each other"), with a dedicated
    block for components admitted via the MCP session — the sandbox
    profile for agent-generated code ("agent output may reach
    [llm, kv.*] and nothing else") as a machine-checked invariant
    instead of a review convention. A violation refuses admission with a
    why-trace naming the violating chain. The triad this completes:
    admission checks *correctness* (§5), audit --diff (item 21) checks
    *drift*, policy checks *absolute authority*. Buildable now: the
    audit graph exists, evaluation is set operations over it, and the
    why-trace machinery already renders evidence chains. The standing
    G8 caveat applies and the policy report must restate it: an extern's
    classification is trusted, not verified — policy bounds what
    *declared* boundaries may do; a lying `pure` extern is the errata's
    problem, not this feature's.

34. ✅ **One query surface, three time modes.** Every query today answers
    against the static IR, and two of the envelope's own stated
    assumptions point at the missing modes: "hot swap changes the
    answer" and "scope: only components in this IR". Keep the seven
    verbs and the envelope; add **live** (`--live` / session-bound MCP
    variants: the same `withdraw` prediction answered against what is
    actually loaded now, post-swap) and **historical** (against a
    recorded replay timeline: "which emissions crossed between steps 3
    and 7?", "everything this component touched during its life"). The
    envelope was accidentally built for this — `precision` and
    `assumptions` already express exactly how the modes differ, so a
    result says which world it describes. Historical is the novel mode
    (nobody queries a *verified* effect timeline; APMs query metrics
    they hope are complete) and is the query-side of item 27's causal
    traces — the two should share the trace format.

35. ✅ **A formatter that proves itself.** `revl fmt` does not format —
    `--migrate` is a required flag and the module is a mechanical
    `$`-string rewriter; there is no canonical formatter. For a language
    where agents write most code and humans review diffs, that is
    missing infrastructure: diff noise is review cost, paid on every
    generation. The house-style twist that makes it more than a chore:
    **format, compile both sides, assert byte-identical IR** — the
    formatter is admissible only when it provably changed nothing. The
    same gate retrofits onto `--migrate` (today verified by warnings
    plus eyeballs) and becomes the standing rule for every future
    syntax migration: a rewrite ships iff the IR is unchanged, which is
    what keeps fast syntax evolution cheap forever.

36. ✅ **`revl apply` — plan/apply with *derived* rollback.** `revl plan` is
    the dry run (delta, cascade, teardown order, G8 reach change, with
    `basis` grading how much is real and a guaranteed-vs-predicted
    split); the plan is then printed and thrown away, and the change
    itself happens ad hoc. Make the plan an executable artifact:
    `revl plan -o change.plan` → `revl apply change.plan`, which
    (a) refuses if the running composition drifted since the plan was
    computed — staleness re-derived and compared, not assumed away;
    (b) applies the ordered operations against the live session,
    verifying **at each step** that reality matches the plan's
    prediction (item 27's prediction-vs-actuality oracle, applied at the
    moment it matters most); and (c) on mid-plan failure, rolls back the
    already-applied steps via derived LIFO inverses. That clause is the
    point: the well-known plan/apply pattern has no real rollback —
    half-applied change is the operator's problem — whereas here
    rollback is derived from the same IR the plan was computed from.
    Change management where the rollback is a theorem, not a runbook.

37. ✅ **`prop test` — property testing with type-derived generators.**
    syntax-2.0 §7 already commits `verified effect` to "algebraic-law
    property tests… the `undo ∘ do = id` witness", so the machinery is
    implied by the spec; build it as a user-facing form first —
    `prop test "commutes" (a: Int, b: Money) { assert … }` — with
    generators derived from the types the checker fully knows (records,
    ADTs with every constructor visited, `Opt`/`List` nestings, the i64
    edge values), plus shrinking on failure. `lifecycle` set the
    precedent that a modifier changes the body's stratum without a new
    top-level form; `prop` does the same with parameters-as-generated-
    inputs. **Sequencing: design this before building item 26**, so the
    inverse round-trip becomes its first auto-generated instance rather
    than a bespoke mechanism. Bonus held cheaply: a `prop test` compiled
    to all six tiers with a shared seed is a randomized cross-tier
    conformance fuzzer nobody had to design separately.

38. ✅ **Refusal telemetry — the rejection log as the feature-request
    queue.** `bench/results/` already holds committed generation corpora;
    every refused attempt in them is demand data. A small miner over the
    corpus (and, opt-in, over the MCP session's own diagnostic stream)
    produces a ranked table of what the models that write this language
    keep reaching for and being refused — unknown stdlib methods
    (HOST-METHOD), invented syntax forms, missing types. Then the stdlib
    and syntax roadmap is ordered by measured demand, the exact move the
    Int32 entry already made ("measured, not assumed"). Half the seed
    exists: `bench/rescore.py --run all` already prints a *failure
    taxonomy* (its largest bucket is cited in guide-ai-agents.md "How
    you're measured"); this item extends taxonomy → demand ranking and
    adds the MCP session's diagnostic stream as an opt-in second source.
    No language has this feedback loop; revl is uniquely positioned
    because its primary authors are models whose failures are already
    committed as a corpus.

39. ✅ **Serve a composition as an MCP server — hints as theorems.** The
    bridge has three quadrants built: `revl mcp schema` projects a
    composition's provided operations into tool definitions (with
    `readOnlyHint`/`destructiveHint` derived from the checked `emission`
    classification), `revl mcp import` consumes foreign tools as
    services, and `revl mcp serve` serves *the compiler's* tools. The
    missing quadrant: serve the **composition's own operations** live —
    `revl serve --mcp app.rvl` boots the composition (the session
    already holds one and `revl_call` already drives it) and exposes
    every provided operation as an MCP tool over stdio. Why this is a
    category claim, not a convenience: everywhere else in the MCP
    ecosystem, `readOnlyHint` is an unverifiable author assertion — the
    known tool-poisoning trust gap. A revl-served tool is the only kind
    whose hints are compiler-derived, whose destructive operations carry
    declared inverses, and whose implementation hot-swaps behind the
    tool surface only through the admission gate. Import + serve then
    close the loop: revl becomes the typed, verified middle of the MCP
    ecosystem — consume tools as coeffects, provide tools as proofs.
    Most of the plumbing exists (schema + session + the stdio loop);
    the work is wiring provided operations onto the wire and a
    config-to-boot story for standalone serving.

40. ✅ **Replay bisect — git-bisect for execution.** The timeline records
    every step; `step_back k` restores any prefix state; what is missing
    is the search. `revl replay bisect --assert '<expr>'` binary-
    searches the timeline for the first step at which the predicate
    flips — log₂(N) restores instead of N, and the answer is a step
    index with its full record (who ran, what it touched, which realm).
    Nearly free on the machinery (the search is a loop over `step_back`;
    the predicate evaluates in the same expression scope `:inspect`
    uses), and it is the debugging demo in one line: "find the exact
    step that corrupted the cache, in seven restores, on a recording."
    Honest bound inherited from replay §4: bisect trusts inverses; under
    a wrong `undo` the restored states are the *inverse-produced* ones —
    item 26 is what makes the answer trustworthy, and the report should
    say whether the effects on the bisect path were `verified`.

41. ✅ **The Component Model bridge — slices 1-3 complete: export wit + WIT resources + WASI-P2 canonical ABI (Str/scalars/records/lists/variants, pure fns AND service provide-methods, wasmtime component-model proven). Remaining gaps documented: method-time effects, variant-aggregate-payload params.
    revl wasm component today runs on exactly one host: cordis-wasm
    (whose WAT convention is the tier's point — the coeffect spec *is*
    the import section). The WASI Preview 2 Component Model is the
    industry's standard container for what revl produces; bridge to it
    in honest slices, each independently useful:

    - **Slice 1 (nearly free): `revl export wit`** — the reverse of
      `import wit`: generate the standard WIT interface for any revl
      service/composition, pure codegen from the IR (the importer's
      type mapping, run backwards). Interop documentation before any
      binary compatibility.
    - **Slice 2: WIT `resource` support.** The importer refuses
      `resource` today, which locks out the real WASI worlds — yet revl
      already distinguishes value-vs-handle at seams (`distribute.py`:
      acquire-returned resource types cross by proxy, never by copy).
      WIT resources map onto that model nearly one-to-one.
    - **Slice 3 (horizon): canonical-ABI emission** — a revl component
      as a *standard* WASI P2 component, loadable by wasmtime,
      wasmCloud, Spin, jco. Gated on the wasm tier's own restrictions
      table (Str/records lowering — already named as hard EmitErrors,
      so the prerequisites are enumerated, not discovered).

    The claim the slices build toward: WIT describes *shape only* (the
    importer's "only interesting decision" is that WIT carries no
    effect information). A revl-authored component is the first
    standard component whose lifecycle and effects are *verified* —
    metadata the Component Model cannot express, carried alongside it.
    First-party leverage: cordis-wasm is ours, so both sides of the
    bridge co-evolve.

42. ✅ **The runtime TCK — make the seventh runtime somebody else's
    weekend.** stc-go proved the pattern: a third party built a Go
    runtime, and the hand-written scenario reference was "the executable
    oracle the emitter targets". Generalize it into a published
    compatibility kit: the R1–R5 runtime contract plus A1–A8/G7
    semantics as an executable suite any candidate runtime adapter runs,
    ending in a conformance report ("R1–R4 pass, R5 pending, A8
    async-body divergence — same as cordis-py"). The §2 exit criterion
    already wants R1–R5 documented per backend; the TCK is the same work
    made self-serve. This is how the six-backend cost curve bends: tier
    seven arrives as a PR with a green TCK report instead of a
    first-party wave — the JVM/POSIX/browser-engine ecosystem move,
    applied to Cordis runtimes. Divergences the kit finds land in the
    same pinned-divergence discipline the suite already uses: a pinned
    case that starts passing fails the kit too, so the report only
    changes deliberately.

43. ✅ **The browser playground — required, not optional.** Nobody can try
    revl without a clone and a Python install, and the pitch depends on
    *experiencing* a rejection with a why-trace. The pieces are
    unusually playground-friendly: the compiler and emitters are pure
    Python (Pyodide runs them client-side — no server, no cost to
    operate), and the TS tier's output lives in the JS world. Ship the
    compile-only version first — write revl, see the IR, the
    guarantee-naming diagnostics, the audit table, `plan` output — and
    treat in-browser execution on the TS tier as the stretch goal. The
    compounding effect is the point: **every example in every doc and
    in the item-18 write-up becomes a runnable link**, including "break
    this component and watch the gate refuse it".

44. ✅ **Delivery semantics — idempotency in the IR, verified retries.**
    `import_openapi.py` knows PUT/DELETE are idempotent by RFC 9110 and
    records it only as a header comment. The IR carries `emission`
    (irreversible) but nothing about *delivery*: every emission is
    implicitly at-most-once, because retrying a non-idempotent emission
    is unsafe and nothing distinguishes the safe case. Promote
    idempotency to a checked IR property (`emission idempotent fn
    put(…)`; imported evidence flows from OpenAPI exactly the way safe-
    by-spec already does), and the runtime earns a new right: it may
    auto-retry precisely the emissions the type system says are
    idempotent — transient-failure resilience as a verified property
    instead of a wrapper library's guess. `commutative` (Def. 39) set
    the precedent for algebraic properties as declarations; idempotency
    is its sibling, and the one distributed systems actually bleed
    over. Same evidence rule as the import family: idempotent-by-spec
    is the author's claim, stated as such — and a `verified idempotent`
    upgrade via item 37's property tests is the natural second step
    (f(f(x)) == f(x) round-tripped against a recording).

45. ✅ **The quarantine tier — candidates prove themselves in the
    sandbox.** Item 24's threat model will correctly declare "the gate
    does not sandbox host code" a non-goal; this is the architectural
    answer. The wasm tier's identity is that the paradigm is enforced
    by the sandbox — confinement is physical. So stage item 31's
    gauntlet through it: an untrusted candidate runs its lifecycle and
    fault battery on the substrate tier first, where an escape is a
    trap rather than an incident, and only then is admitted to a hosted
    tier. Honestly gated: today's tier restrictions (no `Float`, `Map`
    or function types, no host builtins, no config channel) make this
    impractical for real components — it lands after item 41's
    slice-3 prerequisites, and
    that ordering is the point: it changes what 41 is *for*. The bridge
    stops being only an ecosystem play and becomes the security
    architecture.

46. ✅ **Parallel activation — boot scales with the graph's depth, not its
    size.** Activation is strictly sequential today (`load order:
    A -> B -> C`). G3 makes the dependency graph a *checked DAG*:
    independent branches are provably independent, so activating them
    concurrently is safe by construction, with teardown remaining LIFO
    per branch and the derived order preserved within every chain. Most
    systems cannot parallelize startup because nobody knows the true
    dependencies; revl knows them with compiler certainty. The same
    argument parallelizes placement boot across processes. Not urgent
    at demo scale — it matters the day a composition has hundreds of
    components — but it is the cleanest small proof of the thesis that
    checked structure pays operational dividends, and the runtime
    contract question it raises (does R2 permit concurrent resolution?)
    is a TCK (item 42) question, so the two should be answered
    together.

47. ✅ **Crash recovery — the accumulator as a write-ahead log.** Item 15
    persists *admitted generations* (the composition's shape); nothing
    covers in-flight effect state when the process dies. Today the
    accumulated effects live only in process memory (`--record` is an
    in-memory observer for the replay REPL, not a durable trace), so
    `kill -9` mid-activation orphans external state with no record. But
    the paradigm's core data structure — accumulated effects with their
    inverses — *is* a write-ahead log; persist it as one: append each
    effect's step identity and inverse descriptor as it commits, and on
    restart the runtime reads the WAL and proves its way back — roll
    forward (activation had completed; resume serving the persisted
    generation, item 15's half) or roll back (run the persisted
    inverses LIFO, L-Raise style), ending in a stated, checked verdict.
    The honest analysis is the design: post-crash, in-*process* effects
    are moot — the memory is gone, their inverses are no-ops — so the
    WAL's real cargo is **boundary state**: emissions crossed (bare vs
    compensated, so recovery can run compensations), and
    acquire-classified externs whose referents outlive the process
    (files persist; sockets died with it — the extern classification
    already distinguishes these worlds). This forces one language-level
    question worth answering deliberately: an inverse that must be
    runnable *after* restart must be reconstructible from its
    description (step identity + component + captured args), not from a
    closure. Demo/exit test: `kill -9` mid-activation, restart, and the
    runtime prints the recovery verdict with the residue proof. This is
    the durable-execution story — the neighboring category's core value
    — falling out of machinery revl already has: the difference between
    "verified while it runs" and "verified even when it dies".

48. ✅ **Signals and queries — state the answer, build the one gap.** The
    workflow-engine pattern (external events into a running instance;
    reading state without mutating it) deserves an explicit revl answer
    so nobody mistakes its absence for a hole. The mapping mostly
    exists: a *query* is a provided pure `fn` — and revl's version is
    stronger than the pattern it mirrors, because `readOnlyHint` is
    compiler-proven (item 39), not a handler author's convention; a
    *signal* is a call on a provided operation (`revl_call`, or the
    item 39 MCP surface); provider-change events are already reactive
    coeffects (R2/R3). Two genuine gaps remain: **durable signals** — a
    queued delivery that survives crash rides item 47's WAL — and
    **external event subscription**: a component reacting to events
    that are not provider withdrawal has no first-class form today (it
    is a host extern loop). Whether that wants an `on …` reactive form
    or the position that "a bus is just a service" is a design decision
    to make once, in writing. Docs-first item: the mapping section
    costs an afternoon and preempts the most predictable objection the
    lighthouse audience will raise.

49. ✅ PHASE 2 VERSIONING LANDED 2026-09-02 (PR #240: `registry.release_facts` / `registry.publish_release` — the write path. Phase 0 published a name once [first-come, by "the directory did not exist"], so there was no second release, nothing for item 64's computed bump to be checked against and nothing for item 261's derived changelog to be derived across. An update now DECLARES its release; a published release is immutable and freezes its bytes/manifest/record/changelog under `releases/<version>/`; the declared version must satisfy the bump `version.derive` computes from the interface diff against the release it replaces [an under-bump is refused by name, an over-bump is not a contradiction]; an UNVERSIONED entry cannot be replaced [nothing to bump from — this is also what keeps every pre-existing entry as protected as it was under first-come]; a bump that cannot be computed REFUSES unless the publisher declares `version_scheme = "opaque"`, and then every release under it records `bumpCheck: "cannot verify"` permanently; and a name does not change publisher silently [continuity of a SELF-ASSERTED label, not authentication, and said so in code and docs]. The index row gains `releases`, the frozen chain [indexVersion "2"], so a consumer can derive a changelog across versions. `truc ship` reads the registry's verdict through a new `host_release_facts` extern in the same {ok, diagnostic} shape it already reads the gate's; `[ship]` gains version, version_scheme, publisher. Who may write is UNCHANGED — whoever can write the registry directory, which for a git-backed registry is the repository's own authority; what changed is that this authority may now REPLACE a name rather than only claim a free one, which is why every replacement is checked and recorded. 21 frontend-only tests in tests/test_registry_releases.py + 5 end-to-end in tests/test_truc_ship.py; docs/registry.md §1.2/§7, docs/truc.md, docs/commands-reference.md. The index-trust half of §1 was RESTORED 2026-09-02 (PR #249): `publish_release` wrote `description`/`tags` into the entry's row AFTER `build_index` had run, so a fresh regeneration could never reproduce them and `verify` called every registry with a published component stale — permanently, which made a genuinely stale index indistinguishable from the normal state and left `truc ship` unable to leave a green registry behind. The two fields moved to `components/<name>/meta.json` beside the source, the way `version` already lives there [item 428 F12], so `build_index` copies them verbatim and the WHOLE row is derived from files under `components/`; nothing is exempted from the comparison, because an exempt key is one a stale index can hide behind. Narrowing `verify` to a regenerable subset was considered and rejected for that reason. `meta.json` fails closed like the `version` file, an update that does not restate a description keeps it instead of having it dropped by the next regeneration, and a registry published by the old path is told to run `registry.migrate_meta` BY NAME rather than only "stale", because regenerating without it would delete the fields. The five entries under `registry/` never carried those keys, so they need no migration and `registry/index.json` is byte-unchanged. 7 new tests in tests/test_registry_releases.py; tests/test_truc_ship.py stopped filtering description/tags out of its row comparison — that filter WAS the defect — and now asserts `verify` empty end-to-end; docs/registry.md §1.3. STILL PHASE 2's AND STILL OPEN: a hosted index, accounts/tokens, authenticated publisher identity, key namespacing [item 9's deferred half], yanking policy, and resolving by version range — docs/registry.md §7 keeps that list) **The agent-first component registry — search is admission, trust
    is a dossier.** The §5 stance above said *not yet*, with two
    preconditions and a demand test ("a registry when there is a second
    author, not before"). Status has moved: structural interface
    compatibility landed (the admission gate), key namespacing remains
    (item 9's deferred half — still the prerequisite, exactly as
    written). And the demand test is met in a form the entry did not
    anticipate: the second author is an **agent**, and agents re-create
    what they cannot discover. What "agent-first" concretely means,
    beyond what npm/cargo/PyPI are:

    - **Search by structural need, not by name.** The query is "who
      provides a service admissible against *this* consumer's call
      sites" — and the §5 compatibility check *is* the search
      predicate. The registry runs the gate as its index; a hole's
      fill spec (item 32) is itself a well-formed query.
    - **Trust is attached evidence, not stars.** A published component
      carries its item 28 manifest, its item 31 gauntlet dossier
      (proved / tested / claimed, G8 boundary enumerated), and its
      tier matrix. Install-time re-verification is cheap because the
      gate is the install tool.
    - **The loop closes over MCP.** Before generating, the harness
      queries by required shape; after a gauntlet pass, `revl_publish`
      is one verb; updates ride service versioning (item 9), and a
      breaking reshape is *detected by the same drift machinery* that
      guards hot-swap.

    Sequencing preserved from §5: namespacing first, registry second —
    but design the query protocol now, because items 28/31/32 are all
    quietly defining its schema.

50. ✅ **The token economy — make revl the cheapest language for an agent
    to write.** The stated identity is agent-first; make the *cost*
    agent-first too, under the house rule (measured, not assumed):

    - **Metric before optimization:** extend `bench/` to record
      **tokens-to-green** alongside iterations-to-green — output tokens
      spent per admitted component is the number every trick below must
      move, and item 38's telemetry ranks where the spend goes.
    - **One intent, one call:** audit the MCP verb surface for chatty
      sequences (check → plan → admit is three round-trips of schema
      and result tokens); compound verbs with early-exit — item 31's
      gauntlet is already this shape — and batch forms where an agent
      predictably chains.
    - **A terse authoring form, self-provingly expanded:** accept an
      optional compressed wire syntax on the MCP surface and expand it
      through the item 35 formatter under the same gate — the terse
      form is admissible iff expansion is IR-identical, and what lands
      on disk is always canonical. Output tokens are the scarce
      resource; stratum-3's keyword density is the candidate.
    - **Deltas, not documents:** a `revl_edit` applying structured
      patches to a virtual source, so iteration never resends the file
      — pairs with item 32 (fill a hole by address) and with the
      session's existing principle that state lives server-side and
      agents pass *names*, not contents. Audit the current verbs for
      violations of that principle (a swap that resends full source is
      one).

    The honest rule for the whole item: no trick ships on taste — the
    bench metric decides which ones pay, and the ones that do become
    part of "How you're measured" in guide-ai-agents.md.

51. ✅ **The string wave — Str gets the treatment Int got.** The stdlib
    spec never says what a string's *unit* is, and the lowerings
    already disagree: `length()` is python `len(x)` (code points) but
    TS `x.length` (UTF-16 code units), so `"😀".length()` is **1 on one
    tier and 2 on another** — a silent wrong-answer divergence of
    exactly the Int→Float class, invisible while every test is ASCII.
    `charAt`/`charCodeAt` (`ord(x[i])` vs UTF-16 unit at unit index)
    split the same way, JS `charAt` on an astral character yields a
    lone surrogate, rust/go strings are UTF-8 bytes with no O(1)
    indexing, and `slice` inherits whichever unit the tier indexes by.
    The `split` entry proves the discipline exists for *semantics* (its
    JS shape is pinned on every backend, with each tier's workaround
    documented); the *unit* now needs the same one-answer treatment the
    arithmetic wave gave `/` and `%`: pick the unit (code points is the
    coherent choice; UTF-16 units is the JS-prior choice — decide once,
    in docs/strings.md, with the measured cost of each tier's
    conformance), lower every tier to it, and pin it with non-ASCII
    probes in the cross-tier *execution* suite — by the runtime-truth
    rule, "every emitter agreed on a shape" never implied every tier
    agrees on a value. Float rendering in template interpolation rides
    along (each host's default float→string differs at the edges: 1e21
    forms, negative zero, NaN spelling).

52. ✅ **Deterministic collections — decide iteration order *before*
    iteration ships.** `Map` deliberately has "no length, no iteration
    yet". That is the moment to decide, because the tiers disagree by
    default the instant iteration exists: python dicts and JS Maps are
    insertion-ordered, rust HashMap is deliberately randomized, go maps
    are randomized *by spec*, java HashMap is unspecified. If `keys()`
    ships without a contract, the divergence arrives with it — found
    later, pinned as errata, expensive. Decide now, in the spec, with
    the options costed: insertion order (matches py/ts for free; rust
    needs IndexMap or equivalent, go needs an ordered wrapper) vs
    sorted-key order (deterministic everywhere, costs a sort, needs
    orderable keys) vs "iteration order is unspecified" (honest but
    forfeits cross-tier executable equality for any program that
    iterates — against the house religion). Whatever is chosen becomes
    a TCK (item 42) clause the same day it lands in the spec.

53. ✅ **Verified state handoff on hot-swap — close the `code_change`
    gap.** `revl swap` (item 23, landed) drains in-flight *calls*; the
    old provider's *state* dies with it — its effect-created world (a
    cache's Map, a session store's entries) is torn down LIFO and the
    successor starts cold. For stateless providers that is correct; for
    stateful ones it silently converts "hot swap" into "restart with
    extra steps". The classic answer is the hot-upgrade callback
    (Erlang's `code_change`); revl's version can be *checked*: an
    optional `handoff` pair on a component — old side exports a value
    of a declared type, new side accepts it — where the gate verifies
    at admission that the successor's accepted handoff type is
    compatible with the predecessor's exported one (the same §5
    machinery, pointed at state instead of interface), refusing the
    swap when the shapes disagree instead of dropping data at runtime.
    No handoff declared = today's teardown semantics, stated in the
    plan/dossier so the operator sees "state: dropped (none declared)"
    vs "state: handed off (`CacheState`)". Honest limits carried in the
    entry: handoff is a value copy across generations (item 23's drain
    point), not shared memory; and a *cross-tier* handoff rides the
    canonical wire encoding, so what cannot marshal cannot hand off —
    the distributability verdict already names those types.

    **Decision record (2026-08-23, architect review of the
    instance-parametric hot-swap reference):** the built substrate —
    implicit *structural* migration of each instance's ordered
    stateful-resource vector via `__revl_state__()`, gated on
    same-length/same-type, capture-before-teardown, cohort-checked,
    rollback-on-reject — is accepted as **canonical at the runtime
    layer**; the declared `handoff` surface above compiles down to it
    rather than replacing it. What remains open in this item narrows
    to: (a) the declared surface (visibility: the plan/report must say
    "state: migrated (N resources)" / "pool: re-established, not
    carried" in the success cases too, not only on drop); (b)
    **stable-key correlation instead of positional** — not cosmetic:
    the structural gate has one named hazard, *same-type positional
    collision* (a successor that reorders two same-typed resources —
    two Maps — passes the gate and receives swapped state; silent
    wrong-state, the class this project exists to prevent), pinned by
    a characterization test until keys close it; (c) per-fiber
    resource-attribution ledger as a **blocking note on instance
    phase-2 fan-out** (the global stack is correct only while bodies
    are sync). Migrate-by-default is confirmed; respawn/reject remain
    available.
 

    **+ formal typed handoff protocol (external proposal #2, folded here).** Beyond interface-compat, a typed `handoff CacheState from old to new { export; validate; import; acknowledge }` that PROVES how state moves. Safety property to enforce: the old component is NOT withdrawn until the new one ACKNOWLEDGES the state.

54. ✅ **Seam deadlines — the missing half of partial failure.** The
    bridge handles peer *death* (the monitor turns EOF into reactive
    withdrawal) but not peer *hang*: a cross-process call on a slow or
    wedged provider blocks its consumer forever — the only timeouts in
    placement.py today are process-start and shutdown waits, not call
    deadlines. Waldo's fourth leak is exactly this, and the async
    contract (`async fn` at seams) was adopted to face it — so finish
    the thought: every seam call carries a deadline (per-op default,
    per-call override), a breach is a distinguishable fault (not a
    peer-death withdrawal, not a provider error), and the consumer's
    L-Raise unwinds residue-free like any other failure. The TCK (42)
    gets a wedged-provider clause; the fault sweep (30) gains "fail by
    hanging at step k" as an injection mode, which no author-chosen
    fault test today can express.

55. ✅ **Operator capabilities — G4 for the management plane.** The MCP
    session's verbs can rewrite a running system (`revl_swap`,
    `revl_unload`, `revl_restore`) and nothing authenticates or scopes
    the caller — anyone who reaches the transport is root over the
    composition. Before any networked or multi-operator deployment
    (and before item 39 exposes compositions as public MCP surfaces),
    the management plane needs what components already have: declared,
    checked capability bounds. A session token maps to an operator
    profile — which verbs, which components, which realms ("may swap
    within tenant_a; may not unload; may snapshot everything") — and
    policy (33) evaluates management actions the way it evaluates
    emissions. The audit story completes it: every management action
    lands in the causal trace (27) with *who*, so "what changed and on
    whose authority" is one query.

56. ✅ **Network placement — from processes to machines.** Placement
    seams are Unix-domain sockets: one host, by construction. The
    design already paid partial-failure's costs (async contract,
    reactive withdrawal, canonical encoding, and — once 54 lands —
    deadlines), so the remaining delta to cross-machine composition is
    honest and small in *concept*: a TCP+mTLS transport option per
    seam, identity per process (which 55's operator model can issue),
    and the latency-class note the distributability audit already
    prints becoming a real number per seam. Explicitly *not* promised:
    discovery, orchestration, or placement scheduling — the placement
    file stays a static map; pointing it at machines instead of
    processes is the whole feature. Sequenced behind 54 and 55 (a
    network seam without deadlines or identity would be malpractice).

57. ✅ **Time as a coeffect.** Periodic and delayed work today lives in
    host extern loops — outside every guarantee. A timer is a textbook
    revertible effect: `every 30s { … }` / `after 5m { … }` acquire a
    schedule whose inverse is cancellation, derived like any other
    teardown — unload the component and its timers provably die with
    it (no orphaned intervals, the classic leak the residue probe (18)
    hunts in foreign plugins). The body is the constrained part: it
    runs at activation-time stratum with the component's declared
    capabilities, so a timer cannot smuggle emissions past G4/G8 —
    the audit shows scheduled reach like any other. Determinism note
    for tests: under `revl test`/replay, the clock is a coeffect the
    harness provides, so timer firings are steps in the timeline, not
    wall-clock races — which is what makes the fault sweep (30) able
    to inject "fail at the third firing".

58. ✅ **Federated compositions — verified contracts between sovereign
    systems.** Placement splits *one* composition across processes;
    the org-scale question is different: two compositions, owned by
    two teams, deployed independently, one consuming a service the
    other provides. Today that seam is where every microservice
    architecture bleeds — contracts are prose, drift is discovered in
    production. revl already owns both halves of the answer: the
    manifest (28) is the exported contract, and the §5/drift machinery
    is the checker — pointed *across* deployment boundaries instead of
    within one. Concretely: composition A pins the manifest of what it
    consumes from composition B; B's CI runs A's pinned consumer
    surface through the drift check before B deploys ("would this
    deploy break any registered consumer?" — consumer-driven contract
    testing, compiler-verified, no test code written); the bridge seam
    at runtime is the same proxy/stub the placement conductor already
    generates, plus 54's deadlines and 56's transport. What is *not*
    promised: distributed transactions across sovereign compositions —
    an emission into another composition is a boundary crossing like
    any other (compensation, not inversion, §6.1), and the entry says
    so. Exit test: B's CI goes red on a reshape that would break A,
    with the why-trace naming A's call site — before deploy, across
    repos.

59. ✅ **Verified canary — progressive delivery with derived rollback.**
    (docs/verified-canary.md; `revl canary` + MCP `revl_canary`; the
    stateless slice — a canary provider serves a designated realm,
    divergence is the replay comparison attributed to `(component,
    realm)`, revert is the derived LIFO teardown reusing
    erase_report's residue + `survivors`, promote = item 23's swap for
    the remainder. Exit test pinned: 1 of N tenants diverges and
    reverts clean, the other N−1 `survivors` untouched. The **stateful**
    canary — a candidate that must inherit the baseline's world across
    the cutover — stays a follow-on gated on item 53.)
    `revl swap` (23) is an all-or-nothing cutover. The deployment story
    completes with the gradual form: run predecessor and successor
    simultaneously, split traffic, promote or revert on evidence. G2
    correctly forbids two providers of one key in one realm — which is
    exactly why realms/instances (10) are the mechanism: the canary
    provider serves a designated slice (a realm, a tenant instance, a
    percentage of consumer processes in a placement), the causal trace
    (27) and replay comparison attribute divergence to the canary
    precisely (both generations' timelines are recorded worlds, not
    metrics soup), and *revert is the derived LIFO teardown of the
    canary slice* — not a redeploy. Promote = item 23's swap for the
    remainder. Gated on 10's phases and 53 (a stateful canary that
    wins needs the handoff); the entry exists now so 10 phases 2–5
    leave room for per-slice provider selection. Exit test: canary
    serves 1 of N tenants, diverges under the replay comparison,
    reverts with residue proof — the other N−1 tenants provably
    untouched (`survivors`).

60. ✅ **Auto-mocks — every service declaration ships a free fake.** A
    consumer component cannot be developed or tested until something
    provides its requires; today that something is real. Derive it
    instead: from any `service` declaration, generate an in-memory
    provider whose responses come from the item 37 generators (typed,
    seeded, deterministic), with declared-emission operations mocked
    as recorded-not-crossed (the mock *counts* boundary crossings, it
    never makes them — the test report says what would have been
    emitted, which is itself an assertion surface). `revl test --mock-
    requires` boots any composition in mock world with zero setup
    code; a lifecycle test against mocks is stratum-3 unit testing.
    The registry (49) compounds it: a component's exit tests run
    against mocks at publish time, so a candidate needs no live
    dependencies to earn its dossier. Cheap by construction — the
    types are known, the generators exist, the provide machinery is
    the emitters' bread and butter.

61. ✅ **Component leases — the composition as a multi-agent workspace.**
    Directly motivated by observed reality: this project's own roadmap
    needed a write-contention convention the day two sessions edited
    it. Compositions get the same problem the moment two agents
    iterate on one running system: agent A resolves, regenerates and
    swaps `UserCache` while agent B is mid-gauntlet on its own
    `UserCache` candidate — B's plan is stale (36 catches it) but B's
    *work* is wasted. A lease is the cheap coordination primitive: an
    operator-scoped (55), TTL-bound claim on a component name —
    "agent B is iterating on UserCache until 14:32" — surfaced in
    `revl_state`, checked advisorily at plan time ("leased by B; your
    swap will race") and enforceably at admission per policy (33).
    Leases are advisory by default (agents coordinate), enforced where
    the operator says so (agents are *made* to coordinate), always
    visible in the causal trace (27: who held what when). Not a lock
    on the running component — it keeps serving; a lease governs who
    may *replace* it.

62. ✅ **The repair loop — faults that fix themselves, within policy.**
    The end-to-end scenario every piece has been building toward,
    named as one item so it gets demoed as one feature: a component
    faults at runtime → the runtime emits the causal why (27) and the
    fault's timeline slice (40's bisect narrows it) → the agent
    receives both over MCP, regenerates a candidate → gauntlet (31),
    policy (33), reuse check (49: maybe the fix already exists) →
    hot-swap (23) or canary (59) → the incident closes with a dossier
    diff: what changed, what was proved, who (or what) authorized it
    (55). Unattended inside declared bounds: policy says which
    components may self-repair, which capabilities a repair may touch,
    and when a human ack interrupts the loop (21's boundary-widening
    rule). This is the self-evolving harness's answer to the question
    that kills it in production — "what happens at 3am?" — and it is
    the lighthouse demo's second act: act one is *the gate refuses bad
    components*; act two is *the system repairs itself and shows its
    work*. Exit test: an injected fault in a demo composition is
    detected, repaired, verified and swapped with zero human input,
    and the incident report reconstructs every step from the causal
    trace alone.

63. ✅ **The supervisor's cockpit — the missing human surface.** The
    human-in-the-loop features all assume an interface that does not
    exist: 21's boundary-widening ack, 33's policy exceptions, 55's
    operator actions, 61's lease board, 62's interrupt point — every
    one of them says "a human decides here", and today that human has
    a CLI and JSON. `revl dash`: a read-only live view over the
    session/placement — the dependency graph as it actually is
    (realms colored, seams drawn, canary slices marked), the causal
    trace streaming, pending acks as a queue with the evidence
    attached (the why-trace and dossier diff *are* the approval
    context), the lease board. Read-only is a design decision, not a
    limitation: actions keep flowing through the audited MCP/CLI
    surface (55), so the cockpit cannot become an unaudited side
    door — it renders state, links the command. The agent-first
    inversion, stated plainly: agents get the language and humans get
    the dashboard, because in this product the *humans* are the
    integration surface.

64. ✅ **Derived semantic versioning — the version number is computed,
    never chosen.** Nobody trusts hand-picked semver; Elm proved the
    alternative (versions enforced from API diffs) and revl can go one
    deeper, because its interfaces carry *effects*: the drift
    machinery already classifies every change (addition, widening,
    narrowing, removal), so the bump is derivable — additive surface =
    minor, any breaking reshape = major — and **a capability change is
    a semver event even when the shape is compatible**: an operation
    that *gains* an emission is a major bump (consumers' G8 audits
    change meaning), one that *loses* an emission is minor
    (strictly purer, the direction §5 already admits). `revl version
    --against <previous manifest>` prints the required bump and why;
    the registry (49 phase 2) refuses a publish whose declared version
    contradicts the computed one. This is item 9's versioning half
    made mechanical — the number stops being a promise and becomes a
    measurement. Landed: `revl version --against <previous.json>`
    (src/revl/version.py, docs/derived-versioning.md, 22 tests) hands a
    single-method projection of every shared operation to the real
    admission predicate `_service_compatible` — the classification is
    reused, never reimplemented — and joins the per-operation verdicts
    (any major → major, else any minor → minor, else patch); the
    registry-refusal half landed with item 49 phase 2 (PR #240): `registry.release_facts` hands the release being replaced and the source being published to this module's `derive`, and refuses a declared version below the computed bump, naming the bump and the minimum version it permits. An over-bump is allowed; only an under-bump contradicts.

65. ✅ **Generation history and operator undo — git revert for running
    systems.** `revl_rollback` exists and is depth-1: a single
    `previous` slot, gone the moment a second change lands. Deepen it
    into what 15's persistence already makes cheap: every admitted
    change (swap, apply, repair-loop action) appends a generation
    snapshot; `revl undo` returns to generation N−1, `revl undo
    --to <gen>` to any retained one — each undo is itself an admitted,
    gated, causally-attributed change (an undo that bypassed the gate
    would be the one unverified path into a running system, so it
    doesn't get one). The plan/dossier for an undo is computed like
    any other change: what unloads, what state drops (53's "state:
    dropped" honesty applies in reverse), what boundary crossings the
    interim generations made that no undo can un-emit (§6.1, printed,
    always). The operator promise this buys, one sentence: **every
    change to a running system is one audited command away from
    undone — and the system tells you exactly what "undone" cannot
    include.**

66. ✅ **Capability attenuation on spawn — children hold less, provably.**
    Instances (10, phase 1 frozen) make one component live N times;
    phases 2–5 are in flight, which is exactly when to fix the
    security shape: a spawned child's capability set must be a
    **checked subset** of its parent's — a parent may pass down only
    what it holds, may narrow, may never widen (monotone shrinkage,
    the same direction §5 admits for purity). Per-tenant instances
    then get least-authority for free: the tenant_a instance spawned
    with `[kv_a]` *cannot* reach `kv_b` even if the parent could, and
    the audit shows the attenuation chain per instance. This is the
    capability story's last hole: G4 bounds a component's declaration,
    33's policy bounds the composition, 55 bounds the operators —
    attenuation bounds *lineage*, and without it a spawning component
    is a capability amplifier. Filed now for the same reason 59 was:
    so the in-flight instance lowering on the other tiers leaves room
    for a per-instance capability set instead of baking in
    inherit-everything.

67. ✅ **Extern-level `undo <expr>` is unchecked — soundness hole, found
    by dogfooding, with repro.** The uxprobe2 round established that an
    extern acquire's `undo` expression compiles completely unchecked:
    wrong parameter types, undeclared functions, even bare
    self-reference pass the gate — parsed and dropped. This is the one
    unchecked slot in exactly the place the paradigm promises inverses,
    and every acquire-style component uses it. The fix was scoped by
    the dogfood dispatcher (Tier 3, run_00015 — dispatched, **never
    landed**; see dogfood/RESUME.md): mirror the component-site
    effect/undo rigor at the extern site — arity, types,
    declared-callable, self-reference refusal — with corpus entries in
    examples/rejections/ per the executable-spec rule. Until it lands,
    this is an errata-grade known unsoundness and belongs in
    docs/contract-errata.md if any release cuts first.
    *Review 2026-08-23:* run_00015's fix (agent/extern-undo-check,
    bf19290) implemented syntax-2.0 §6's empty-scope/constants-only rule
    faithfully — and that rule is the stale half. It refuses the landed
    WIT resource model (`extern acquire fn r_new(...) -> R undo
    r_drop(r)` names the acquired result; five WIT import/export tests
    go red on the merge, reverted in 8e95be5). Worse, the §6 example
    `undo close(0)` is itself a non-inverse undo — the bug class item
    68 kills. **Decision:** the `undo` of an acquire extern sees exactly
    one implicit binding, `result: T` (the acquired value); parameters
    stay invisible (no teardown parameter capture); `compensate` stays
    constants-only. Rebuild the checker against that rule: WIT importer
    emits `undo r_drop(result)`, syntax-2.0 §6 + wit-bridge §3 updated,
    rejection corpus still refused, WIT suite green.
    Landed: `_check_extern_undo` (src/revl/lower.py) checks the slot
    with tenv `{result: T}` for an acquire's `undo` (empty for
    `compensate`; declared-callee, self-reference, arity/type rigor as
    scoped) — merged 05b4eee after review against the WIT
    counterexample: import_wit now generates `undo r_drop(result)` and
    declares the destructor extern; syntax-2.0 §6, import-wit,
    wit-bridge §3 rewritten to the result-binding rule; five
    g4_extern_* rejection-corpus entries. Suite 1754 green incl. WIT
    import/export, backend goldens 111, py-backend 58.

68. ✅ **Fault-path residue accounting — OPEN COUNTEREXAMPLE, do not
    merge the candidate fix.** The dogfood loop's last act before
    rate-limit death was catching its own subagent's fix red-handed:
    the run_00016 branch (fault-path residue asymmetry) reads a host
    trace that **nothing feeds on the real path**, so a genuinely
    leaky component — constructed from the original asymmetry repro —
    **still passes a fault test on the "fixed" tree**. False green
    from the verification feature is the worst bug class this product
    can have; a fault test that passes a leak is strictly worse than
    no fault test. Required: the reviewer's leaky-component
    construction becomes the regression test *first* (red on the
    candidate branch), then the fix is rebuilt against it —
    diagnosing which layer drops the accounting (revl lowering vs py
    adapter vs cordis-py runtime) exactly as the original dispatch
    specified, fence+errata if it lands in the runtime. Exit: the
    leaky repro is caught, the clean component still passes, and the
    test that proves both is in the suite.
    *Update 2026-08-23:* the gate exists — the reviewer's construction
    is committed verbatim as
    `test_a_non_inverse_undo_fails_under_an_injected_fault` (+ its
    inverse-undo positive control) on agent/fault-res (54a6f23), red
    there by design. (Since issue #1859 that `undo` no longer compiles,
    so the test now reaches the same program past the checker, by giving
    the compiled IR the non-inverse undo; the source refusal is pinned
    beside it by `test_a_non_inverse_undo_is_refused_before_an_injected_fault`.) The delta the fix run must explain: the subagent's
    own regression test faults the body with a `fail` statement and is
    caught; the probe-injected path (`fail at step 1`) is not — the
    host trace the judge reads is not fed on the injected-fault exit.
    Also on that branch: a04d17d flipped
    `test_an_await_body_reports_the_known_cordis_py_divergence` (that name is SUPERSEDED: the §8 upstream-divergence lock now sees the async fiber land FAILED, which is the half that landed, so the lock is `tests/test_fault_tests.py::test_an_await_body_lands_failed_like_a_sync_body` today and docs/fault-tests.md §8 was updated to match) —
    the rebuild must either restore the divergence or update the lock
    *and* docs/fault-tests.md §8 as a conscious behavior change, not
    leave the suite red.
    Landed: merge d8241ba (fix dac992b). Root cause was neither the py
    adapter nor cordis-py but the harness's own splice —
    `fault.py::_inject` placed the failure *instead of* step N, so
    `fail at step 1` ran nothing: the acquisition under test never
    executed, the a04d17d trace window captured zero events, and
    `assert no residue` judged an empty window (the false green was
    vacuity, not a starved trace). The splice now follows step N —
    steps 1..N run and arm their inverses, then the failure strikes at
    the boundary, the only placement a real fault can have — and the
    label/emission windows moved with it; the fault sweep (item 30)
    drives the same `_inject` and inherits the fix (the leaky repro now
    fails the sweep at both steps). Exit test proven live: the verbatim
    review counterexample FAILs naming `map#1 (new() with no drop())`
    (R1); the inverse-undo control PASSes; both are suite tests
    (54a6f23). The §8 divergence was independently closed by the
    cordis-py fiber fix (1316174) and origin's lock rewrite was
    adopted. Fault suite 72 green, full suite 1877 green.

69. ✅ **Record update and match-arm blocks — measured ergonomics.**
    Landed (PR #37): `{r | f = e}` functional update — fresh-value
    semantics consistent with `Map.set`, field rules enforced wherever
    the receiver's type is known, py/ts emitters, other four refuse at
    emit naming their tier; block-bodied match arms specified, parsed
    and typechecked with emitters deliberately deferred (lambda-lifting
    judged too big to land safely; the refusal names the workaround).
    IR additive, goldens byte-identical. The review also caught a real
    gap and fenced it (see contract-errata, "Record updates on receivers
    with no named type") — closing it is item 71. Map iteration landed
    alongside as PR #38 (item 52's implementation: canonical-order
    `keys()`, persistent `remove`, `size`, six tiers).
70. ✅ **Lexer: `${...}` capture and `@backend { ... }` bodies balance raw
    braces, blind to strings.** Found by an outside code review, verified
    by direct probe: a template interpolation containing a string with a
    `}` (`v=${m.lookup("}")}`) truncates the capture at the string's
    brace, producing fragments that parse into a baffling error far from
    the cause; the same raw-brace balancing bounds `@java { ... }`
    host bodies, so any verbatim Java/Rust/Go body containing `}"` in a
    string or comment dies early with a misleading unterminated-string
    error — and real host bodies will contain exactly that. Related,
    smaller: strings support no escapes at all (`"`, `
`) and neither
    the limitation nor the workaround is documented anywhere; and
    `${...}` bodies are re-parsed by a fresh Parser starting at line 1,
    so diagnostics inside a multi-line interpolation report wrong line
    numbers. Fix shape: make both capturers string/comment-aware
    (scan-with-quotes), document the no-escape model in the language
    notes, and thread a base line number into interpolation re-parsing.
71. ✅ **Structural record types for anonymous literals.** Fenced in
    docs/contract-errata.md with a verified trigger: `{ r | f = e }`
    where `r` is a let-bound anonymous literal escapes field checking
    entirely (wrong-answer-class, local to records built and consumed
    anonymously; every declared boundary recovers checking). The review
    attempt showed the fix is design, not patch: structural types must
    unify with nominal records at declared boundaries (field-wise, with
    the List[Never] bottom rule recursive) without leaking into the IR's
    type table or emitter identifier machinery — the same shape as
    item 11's widening marker. Exit test: flip the two pinned
    regressions in tests/test_record_update.py from accepted to refused.
72. ✅ **Hygiene from the outside review: tier-naming and doc drift.**
    Three small inconsistencies an external reviewer hit or flagged:
    placement manifests name the TypeScript tier `node` while every
    other surface says `ts` (accept `ts` as an alias at minimum);
    `run.py`'s backend gating is inconsistent — argparse rejects
    unlisted tiers before the friendlier "emits but has no run driver"
    message can fire, so go and the others take different paths to
    neighboring facts; and docs/backends-roadmap.md still says "the
    five tiers" while omitting go from its directory list, contradicting
    the README and the doc's own table.
    ✅ **Landed:** placement accepts `ts` as an alias for `node`
    (src/revl/placement.py `_canonical_backend`, pinned by
    tests/test_swap.py::test_placement_accepts_ts_as_an_alias_for_node);
    `revl run --backend go` now passes the choice gate and lands on the
    same friendly "emits but has no run driver" refusal as `ts`
    (src/revl/run.py, tests/test_run.py::
    test_run_refuses_an_emitting_tier_with_the_friendly_not_wired_message,
    which also pins the `backends/typescript/` dir hint); and
    docs/backends-roadmap.md now says six tiers and lists go's directory.

74. ✅ **Errata harvest, runtime side — the open upstream threads.** A
    sweep of docs/contract-errata.md found four runtime-layer threads
    that are open and actionable (everything else there is closed with
    locks, consciously deferred — the cordis4j global-realm fix stays a
    non-goal until Java realms are load-bearing — or already tracked,
    like `Integer` under item 12). Per the runtime-ownership policy
    (wasm first-party, py our fork, ts/rs/java fork+PR):
    (a) **cordis (TS) `assertActive` residue** — it checks `uid !==
    null`, not lifecycle state, so an undo can register an effect during
    deactivation that lands after the unload snapshot: *permanent
    residue, the G5 gap*, on the tier the README calls the portability
    proof. Repro is pinned (`backends/typescript/tests/upstream.test.ts`
    "finding 2"); fix belongs upstream (fork+PR, feeds cordiverse/
    cordis#39) with a pin until it merges — the exact playbook that
    closed cordis-py's A8 async gap.
    (b) **cordis-py dict-plugin `Config`** — `registry.plugin()` reads
    `inject` via `dict.get` but `Config` via `getattr`; a one-line fix
    in our fork (follow-up to geohotstan/cordis-py#1). Landing it
    retires the workaround our *emitted code* carries (config validation
    inside `apply`) — an emitter simplification paid for by a one-liner.
    (c) **cordis-rs A1 divergence — decide, don't just document.**
    `block_on` under the fiber transition lock defers a divert until
    activation completes, so "emission after boundary never happens once
    diverted" holds on py/wasm but not rs. Either fix upstream (fork+PR:
    don't hold the lock across the await boundary) or *promote the
    weaker invariant that does hold everywhere* — torn-state freedom —
    to the contracted A1 wording, so the per-tier difference becomes
    spec, not surprise. The errata's race-loop assertion already points
    at the second reading; make it official or make it moot.
    (d) **wasm trap payloads** — overflow faults as bare `unreachable`
    where every hosted tier raises a labelled error. Decide: accept as a
    documented tier limit (it may well be — a trap carries no payload by
    design), or spend a designated fault-reason export before the trap.
    Smallest of the four; bundle with whichever wasm slice touches the
    arithmetic helpers next.
    (c)(d) ✅ **Landed as dated decisions** (docs/contract-errata.md): A1's
    contracted invariant is promoted to torn-state freedom — the stronger
    "emission after boundary never happens once diverted" is a py/wasm tier
    property, not the contract, and the race-loop assertion pins the
    promoted invariant; the wasm bare-`unreachable` trap is accepted as a
    documented tier limit (a trap carries no payload by design; the
    labelled-error split is diagnostic depth, not semantics). (a)(b) remain
    open upstream threads as written.
    Exit: each thread is either landed (with its pin/lock) or carries a
    dated decision in the errata saying why it stays open — no entry
    left in the current "surfaced, to file / track" limbo.

    Landed (74a): TS assertActive fixed in the pinned fork
    (`inso1337/cordis@harden-assert-active` c8b94b2, tarball-pinned in
    `backends/typescript/package.json`), finding-2 repro flipped to pin the
    fixed behavior (`backends/typescript/tests/upstream.test.ts`), PR draft
    at `docs/upstream/cordis-ts-assertActive.md` (not opened without
    confirmation). Landed (74b): cordis-py dict-plugin `Config` one-liner in
    the pinned fork (`inso1337/cordis-py@harden-fiber-lifecycle` 1c5e6f1,
    `setup.sh` now pins the tested commit per 76c), emitter drops the
    in-`apply` config resolution (`backends/python/emit.py`, golden
    regenerated), replay harness resolves config itself
    (`backends/python/replay.py`). (c) and (d) remain open.

76. ✅ **Dogfood-friction harvest — what the wave's three findings files
    agree on.** findings-mapiter and findings-records were written by
    different agents on different tasks and converge on the same walls;
    that convergence is the signal (findings-faultres adds the
    environment half). In cost order:
    (a) **Per-emitter dispatcher conformance map.** Every backend
    carries two expression dispatchers (component vs fn-body renderers;
    wasm has three) and nothing marks which IR kinds each must handle —
    the records run patched one path and shipped "unsupported expression
    kind 'record_update'" on the other; both files independently ask
    for the same fix. Do it as data, not prose: a declared
    kinds-per-dispatcher table in each emit.py that a conformance test
    checks against the IR schema, so "did you patch both paths" becomes
    a red test instead of a 15-minute stall. New expression kinds get a
    place they must be registered; the wasm tier's deliberate absences
    are listed, not inferred.
    (b) **Empty-collection pinning: fix the rule or fix the hint.**
    `var m: Map[Str, Int] = Map.empty()` is refused on go while the
    diagnostic claims "any annotated flow" pins — only typed returns
    and parameters do, and the workaround (a typed-return helper fn
    per literal) is pure ceremony. Preferred: thread an annotated
    let/var's type into the literal so the annotation the author
    already wrote is the pin. Fallback: the hint names exactly which
    positions pin. Either way the diagnostic stops describing a rule
    the checker doesn't have.
    (c) **Environment honesty for fresh worktrees.**
    `backends/python/setup.sh` clones the runtime branch's moving HEAD,
    so vintage-pinned tests fail on a fresh environment through no
    fault of the change under test (findings-faultres hit exactly
    this). Pin the clone to the tested commit and update the pin
    deliberately. While in there: document the fast frontend loop
    (`pytest tests/test_frontend.py tests/test_doc_examples.py`) in the
    contributor notes — the 45s+ full suite is the wrong default for
    frontend-only iteration — and add a repo-map line pointing at
    `backends/*/emit.py`, which one agent grepped `src/` several
    minutes to find.
    Exit: the records-run failure mode is reproducible as a red
    conformance test before the fix and green after; the mapiter
    empty-map workaround compiles without the helper fn (or the
    diagnostic names the real rule); a fresh worktree's setup.sh yields
    the suite the branch was tested against.
    Landed: (a) `EXPR_KINDS`/`EXPR_KINDS_FN`/`EXPR_KINDS_COMPONENT` in
    src/revl/lower.py are the registration point for new expression kinds;
    each `backends/*/emit.py` declares `EXPR_DISPATCHERS`/`EXPR_REFUSED`
    (wasm's deliberate absences listed explicitly), checked by
    tests/test_expr_dispatcher_conformance.py (coverage + position rule +
    behavioral render + records-failure-mode pin; the sweep's blind spots —
    `.length` in fn bodies, `?.`, `Map.empty()` in component bodies, records
    in method bodies — exposed go's missing `len` branch, go's component
    renderer fall-throughs on eight kinds (now named tier limits plus
    `field`-`.length`/`fn` handling), wasm's unnamed maplit/optfield/optcall
    fall-throughs and go's missing document-level hole refusal, all fixed).
    (b) the frontend threads an annotated `let`/`var`'s type onto the empty
    literal (`"expected"` on `maplit`), so `var m: Map[Str, Int] =
    Map.empty()` emits on go without a helper fn, and the go hint names the
    positions that actually pin. (c) backends/python/setup.sh pins the
    cordis-py clone to the tested commit (the 74b fork pin, `CORDIS_PY_PIN`
    override); CONTRIBUTING.md + docs/guide-ai-agents.md document the fast
    frontend loop and the `backends/*/emit.py` repo map.
78. ✅ **Dedent multi-line extern bodies in the py and ts emitters (harness
    milestone 2, finding #13).** A multi-line `extern` body (`@py { }`)
    was copied verbatim at a fixed indent, so source indentation landed
    inside the emitted function and broke Python at boot
    (`IndentationError`) — `compile` was green, the emitted module
    wasn't. Fix is one `textwrap.dedent` per emitter; **implemented on
    `agent/fr13-extern-dedent`** (harness + frontend + golden + v2-emit
    suites green). The residual ask is a `compile --validate` flag that
    parses emitted code with the tier's real compiler by default (the
    conformance suite does this; the daily loop doesn't), so this class
    of emitter bug becomes a red compile instead of a boot-time error
    (dogfood/findings-extern-dedent.md).
79. ✅ **The `Any` type is not declared in emitted TypeScript (FR-3
    follow-up).** The stdlib JSON module (`json_parse`/`json_stringify`)
    returns `Any` on every tier, but the ts emitter has no `Any` type in
    `_TS_V3_TYPE` — emitted code contains a bare `Any` identifier that
    `tsc --noEmit` rejects (`Cannot find name 'Any'`), so the FR-3
    stdlib, as shipped, does not pass the ts tier's own typechecker.
    The harness's milestone-2 real provider (which uses `json_parse`)
    emits TS that fails `tsc`. Fix: declare a real `Any` (e.g.
    `type Any = unknown` in the emitted preamble, or map `Any` to
    `unknown`/`any` in `_TS_V3_TYPE`) and add a tsc check to the ts
    test suite so the FR-3 module is validated like the rest of the tier.
    (Harness finding #14.)
80. ✅ **`async`/`await` for ts extern bodies — HTTP is a Promise, the
    extern is sync (harness milestone 2, finding #15).** The harness's
    single G8 boundary crossing, `http_post`, has a `@ts` body using
    `fetch(...).then(...)` which returns a `Promise<string>`, but the
    extern declares `-> Str` and the ts emitter types it `string` —
    `tsc` rejects `Type 'Promise<string>' is not assignable to type
    'string'`. revl has `async fn` service operations (A1 allows `await`
    in async provide methods), but extern bodies cannot be async today,
    so a real LLM call on the ts tier has no clean spelling. Suggest:
    allow `extern async emission fn` (or an `async` classification on the
    extern) whose ts body may `await`, or document that ts HTTP externs
    must block (not possible with browser/node fetch semantics), or wire
    the ts tier's extern through the runtime's async seam. This is the
    last blocker between the harness and a real ts-tier deployment.
81. ✅ **JSON wire protocol narrowed multi-tier reach — the FR-3 tradeoff,
    now visible.** The harness's milestone-2 switch from the string
    protocol (`TOOL_CALL add 2 3`) to JSON (`{"kind":"tool",...}`) moved
    the composition from "runs on py/ts/rust/go" to "runs on py/ts":
    `json_parse` has no `@rs`/`@go` bodies (FR-3's documented scope — an
    `Any` extern return type-erases to `cordis::Value` on rust, and go's
    emitter cannot reach `encoding/json` from verbatim extern bodies),
    so rust/go refuse with the honest "no @rs body" message. Decision to
    record: structured args on all six tiers needs the JSON module on
    rust/go (type the `Any`-binding by declared type on rust; add
    `encoding/json` import wiring on go), or a per-tier wire protocol.
    The string protocol remains the full-tier fallback. (Harness
    finding #16; see also item 79 which blocks the ts half of the same
    story.)
82. ✅ **`emit <handle>.<key>.<method>()` crashes the frontend (harness
    milestone 3, finding #17).** Calling an *emission* service method on a
    spawned instance handle (`emit w.task.run(prompt)` in a supervisor
    component) raises `KeyError: 'target'` in `_is_emission_call`
    (`src/revl/lower.py`): the emission check handles only `req`-target
    call nodes, but a spawn-handle provision access lowers to an
    `instance-get` target, so the marker check crashes instead of
    validating. Non-emission calls through the handle
    (`w.task.status()`) compile fine. The harness's working spelling:
    call the worker's emission method bare (`w.task.run(prompt)`) from a
    supervisor method that is itself declared `emission` — the boundary
    is still marked, one level up. Fix: teach `_is_emission_call` (and
    the G4 message) about `instance-get` targets — walk the handle's
    provision to its service and check `emission` there. Blocked-on:
    none; it is the one crash in the subagent path.
83. ✅ **Durable host resources need a documented extern-`acquire` spelling
    (harness milestone 3).** The durable session store (real file handle
    via `extern acquire fn log_open(path) -> Int undo log_close(1)`)
    works, but two ergonomics surfaced: (a) the `undo` slot on an
    `acquire` extern takes a *call expression* (`log_close(1)`), so the
    fd being closed must be threaded through the component's own
    `effect log_open(...) undo log_close(fd)` — the extern-level undo is
    effectively documentation; (b) multi-line extern bodies must be
    column-0 until item 78's dedent lands (the harness keeps the
    workaround). Both are cheap to close; (a) wants a doc example of the
    real acquire/undo discipline with a resource that has state (the
    harness's `durable_log.rvl` is that example).
84. ✅ **`Map.keys()`/`size()` on the host `Map.new()` stub compile but
    crash at runtime on py (harness milestone 6, finding #18).** The
    session index needed `store.keys()` on a host-acquired
    `let store = effect Map.new() undo store.drop()`. The checker routes
    `keys()` to the stdlib builtin (it type-checks clean), but the py
    emitter lowers it as `sorted(store)` where `store` is a `Map`
    *object* (backends/python/runtime.py `class Map`), so
    `AttributeError: 'Map' object has no attribute 'keys'` at runtime.
    Same class as the FR-4 rust bug, inverted: the *host* Map family
    surface (new/insert/remove/get/drop) never gained the iteration
    methods, but the builtin table makes the checker accept them anyway.
    Fix: either emit `sorted(store.data)` for host-`Map` receivers on py
    (and the tier analogues), or refuse `keys`/`size` on host-Map
    receivers at the checker with a "host Map has no iteration — use the
    value `Map` (`Map.empty()` + `set`/`lookup`) or track keys yourself"
    hint. The harness worked around it by keeping a `__ids__` list inside
    the same host Map (reverted by the same effect accumulator).
85. ✅ **Multi-line string literals for self-hosting (harness milestone 6,
    FR-19 candidate).** revl strings have no escape sequences (`"a\nb"` is
    literal backslash-n), so an agent authoring a real `.rvl` component
    from inside the harness (the lighthouse shape) cannot hand the
    compiler a multi-line source without building it by concatenation.
    The admission tests had to flatten proposed components to one line —
    wrong for the actual use case. Suggest a triple-quoted
    `"""..."""` form (or documented line-continuation idiom) whose
    contents may contain newlines verbatim; six-tier lowering is trivial
    (the lexer already reads raw text between delimiters).
89. ✅ **Service-signature params carrying v3 record types collapse to
    `unknown` on the ts tier (harness milestone 6/7, finding #19).**
    The v1 service-interface renderer (`_ts_type`) does not know record
    names, so a service method taking `List[Msg]` (a record) emits
    `complete(history: unknown[])` — the interface exists (`export
    interface Msg`), but the param type collapses, and `tsc` then rejects
    every call site passing a real `Msg[]` (`Type 'unknown[]' is not
    assignable to type 'Msg[]'`). This is the ts half of the JSON
    protocol's multi-tier story (item 81): the RealModel provider
    (which takes `List[Msg]`) could not typecheck on the DSH tier.
    **Implemented on `agent/fr86-ts-record-params`**: `_ts_type` falls
    through to `_ts_v3_type` for unknown names (records are emitted as
    interfaces), plus List/Opt/Map/Result generics; golden regenerated.
    Residual: the harness's `@ts` http body still returns a `Promise`
    (item 80 — async extern bodies), the last tsc error on the real
    provider.

100. ✅ **`_tool_admit` compiles before honoring `replacing` (harness model
    switcher, finding #23).** The harness's live model route swaps the
    `model` provider (mock -> real) by shipping the full composition with
    `replacing=["MockModel"]`. The admit stage in
    `src/revl/mcp/server.py::_tool_admit` first compiles the candidate
    WITHOUT `replacing` (line 660) and only re-compiles with it (line 668)
    if that first compile succeeded. A provider swap always fails the
    first compile with G2 ("key `model` is provided by both MockModel and
    RealModel"), so the replacing path never runs and every provider swap
    is refused. Fix: honor `replacing` on the FIRST compile (pass it into
    `_compile`), keeping the second compile only as a fallback for
    non-`files` candidates. Repro: the harness's `_switch_model` (ship the
    full composition with the other model provider). **Re-verified
    2026-08-24 against main 5da6a22: still open.** The fused `revl_ship`
    path stops at the admit stage (`"stoppedAt": "admit"`, G2 diagnostics
    above); the standalone `revl_swap` tool honors `replacing` on its first
    compile (server.py line 297-300), so the fix is isolated to
    `_tool_admit`. Numbering note: main's item 94 (Async[T] erasure, ✅) is
    a DIFFERENT item — this text is authoritative for the admit-replacing
    fix.

101. ✅ **Rust emitter: reuse-after-move at a service-call argument (harness
    verification of item 93, finding #24).** Item 93 fixed three
    rust-emitter bugs and the mtier harness dropped from 7 cargo errors to
    1, but one remains: a provide method that passes a `String` by value
    into a service call and then returns it —
    `let _ = self.sessions.append(..., Msg { content: answer }); return
    answer;` — moves `answer` into `append` (E0382 use of moved value).
    The rust emitter needs `content: answer.clone()` when the argument
    variable is reused after the call (the same reuse analysis item 93
    applied to the loop's `current`, extended to service-call arguments).
    Repro: `revl run mtier/*.rvl --backend rust --once` in the harness
    repo. py and ts pass the same harness (2/2 lifecycle tests each) —
    this is rust-emitter-only, the last error between the string-protocol
    harness and the "runs on all runtimes" claim for the loop shape.

102. ✅ **Lifecycle tests cannot advance the clock, so a timer's *firing* is
    not provable in-language (harness verification of item 57, finding
    #25).** Item 57's contract has two halves: the schedule is revertible
    (unload cancels, residue-free) and the clock is a coeffect (a firing is
    a deterministic timeline step — "fires on the 3rd tick"). The harness
    proves the first half inside a `lifecycle test` (py + ts both PASS),
    but the second half is *not expressible in the language*: the lifecycle
    statement set is only `load`/`unload`/`call`/`assert`/`assert no_residue`
    (src/revl/parser.py `_LIFECYCLE_STMT_WORDS`, src/revl/lower.py
    `_lower_lifecycle_body`), and a firing happens only when the harness
    calls `Clock.advance(ms)` — which nothing in-language can do, and
    `asyncio.sleep` cannot produce (the clock never moves on its own). A
    revl author therefore cannot write "fires on the 3rd tick" as a
    lifecycle test; the harness proves it with a host driver
    (`tools/timer_demo.py` in revl-harness) that calls `Clock.advance`
    directly. Fix: add an `advance <ms>` lifecycle step — parser
    (`AdvanceStmt`), lowerer (`{"step": "advance", "ms": N}`), py emitter
    (`Clock.advance(N)`), ts emitter (`host.clockAdvance(N)`), and
    `revl test` doc — so the full item-57 contract is assertable in
    `revl test` on the reference tiers. Repro: `revl test
    src/timer_tests.rvl` in revl-harness proves cancellation but has no
    statement that could fire a tick.

103. ✅ **The harness's reader artifacts cannot cross the canonical ABI —
    `split`/`indexOf` are refused on the wasm tier (harness verification of
    item 41 slice-3, finding #26).** Slice-3 verified against the harness:
    `wasm/canonical.rvl` (the durable ledger's real pipe line format
    `log_line`, pure concat) emits, wasm-tools validates, and wasmtime's
    component model runs it (`revl:exported/ledger.log-line("s1","user",
    "hello")` → `"s1|user|hello"`, empty-string edge, exact round trips;
    milestone 31 in revl-harness). But the boundary carries only what concat
    builds. The harness's *reader* artifacts — the durable pipe **reader**
    and the agent's `add` tool (both `split`), and the web router's
    `route_request` (`indexOf`) — are refused by the wasm emitter
    (`unsupported builtin method 'split'`, `indexOf is not lowerable on this
    tier yet`), so the harness's full agent wire protocol cannot yet be a
    standard component. Fix: lower `split`/`join` and `indexOf` on the wasm
    tier (linear-memory forms exist in the reference backends; the wasm
    tier's restriction table already names them as hard EmitErrors). That
    closes the gap between "the Str boundary works" (slice-3) and "the
    harness's own toolbox/durable/web protocols are standard components".
    Repro: `tools/canonical_demo.py` in revl-harness (PASS) vs
    `add_tool` from `mtier/toolbox.rvl` (REFUSED: `split`).

98. ✅ **Async colour does not propagate through spawned-handle emissions in
    arrow bodies (harness milestone 33, finding #27).** Landed in two halves.
    Item 106 fixed the py EMITTER: `_py_yields_coroutine` now recognises an
    `instance-get` receiver, so a colored arrow tail-calling a spawned async
    worker renders as a plain coroutine lambda, and `_revl_as_async` awaits an
    awaitable result rather than returning it. The FRONTEND half was still
    open, and reproduced on main: `_req_op_is_async` read only the `req`
    `target` slot, so a spawn handle's async provision op was invisible to the
    async-reach. A sync-typed arrow delegating to a spawned async worker
    therefore compiled clean and leaked ("coroutine ... was never awaited" on
    cordis-py), and a sync provide method reaching one directly was admitted
    too — the handle twin of item 117's blind spot. `_async_service_op` now
    answers for BOTH receiver shapes (a required key, and a spawn handle's
    provision reached through the `callee` chain, as `_is_emission_call`
    already did for the G4 marker), so the arrow-colour leak refusal, the A1
    provide-method verdict and the teardown/activation admission rules all see
    it. Ported to `selfhost/lower.rvl` (`handle_emit` contributes to `aops`;
    `reach_handlecall` is the handle twin of `reach_reqcall`). The spawned
    ternary-arrow design is now expressible with an `Async[T]`-typed callback
    slot and refused with a named A1 diagnostic without one.
    `src/revl/lower.py`, `selfhost/lower.rvl`,
    `tests/test_async_spawn_handle_color_98.py`.

    The original report: the workflow
    (milestone 33) first spawned its three workers and fanned tasks out
    through a per-index arrow whose body is a ternary chain of
    `emit <handle>.<key>.<method>()` calls. The emitted Python wrapped the
    arrow with `_revl_as_async` (the *sync* classification) instead of a
    plain tail-coroutine lambda, so `await run_one(task)` yielded a coroutine
    OBJECT — "can only concatenate str (not 'coroutine') to str" at runtime,
    and "coroutine was never awaited". Root cause: the async-colour analysis
    (`src/revl/emission_analysis.py::_async_callables`, call-graph based)
    sees `req`-based emissions (`model.complete` — service methods of a
    `requires`) but not `handle`-based ones (`w1.wtask.run` — a spawned
    instance's service access); the arrow body is therefore not flagged
    async and the emitter's arrow decision (`_py_async_arrow`: tail-coroutine
    -> plain lambda; sync -> `_revl_as_async` wrap) picks the broken wrap
    (`_revl_as_async` does `return _f(*a)` inside `async def _g`, which for
    a coroutine-returning `_f` yields the coroutine object, never awaiting
    it). Fix: treat `handle.key.method()` calls like `req.key.method()`
    calls in the emission/async-colour analysis (the spawn handle's service
    type is known), so an arrow delegating to a spawned async worker is a
    tail coroutine; defensively, `_revl_as_async` should `return await
    _f(*a)`. The harness ships the workflow with `requires`-based workers
    (three keys, `w1`/`w2`/`w3` — `req` emissions colour correctly) and
    sublist dispatch instead of the ternary arrow; when this lands, the
    spawned design becomes expressible. Repro: milestone 33's first
    `workflow.rvl` (spawn + ternary arrow) — `revl test` fails with the
    coroutine leak; the shipped `requires`-based version passes 2/2 py + ts.

99. ✅ **A `let`/`var` declared type annotation is dropped in the IR, so the
    wasm tier cannot type an empty list initializer (harness milestone 35,
    finding #28).** DOES NOT REPRODUCE on main — the same harness
    finding reached main twice, and the pin landed under the number 107 in
    "Follow-ups from harness findings" (`ccffd1f1`, merged `5aac74c2`); this
    entry was closed as a duplicate on `79c714a5`, with no work of its own
    left. `_pin_empty_literal` in `src/revl/lower.py` threads the
    author's annotation onto an empty `list`/`maplit` initializer as
    `"expected": "List[Msg]"`, which is the alternative the item text itself
    offered, so the wasm emitter types it. Verified on main
    (2d2fa07f): a document whose helper accumulates into
    `var out: List[Msg] = []` and pushes records passes `revl test --backend
    wasm` (wasmtime) and `--backend py`. No further work; the item is closed
    as a duplicate. The original report: `var out: List[Msg] = []`
    type-checks (the checker sees
    the annotation) and lowers on py/ts — those emitters tolerate `[]` and
    type it from the later `push` — but the IR `let`/`var` step carries NO
    `type` field (`{"step":"let","name":"out","value":{"kind":"list",
    "items":[]},"mutable":true}`), and the wasm emitter infers the
    initializer's type with no expected type, so an empty list literal is
    refused (`an untyped empty list literal needs an expected List type`).
    The harness hit it building the service-level canonical proof
    (`wasm/service.rvl`): the `lines` helper's accumulator is un-buildable
    on wasm; the demo uses a typed element list literal instead. Fix: carry
    the declared type on the `let`/`var` IR step and use it as the expected
    type for the initializer in every emitter (also lets the other tiers
    check the initializer against the annotation). Repro: `wasm/service.rvl`
    in revl-harness with `var out: List[Msg] = []` — compiles + runs on
    py/ts, `REFUSED` on wasm.

447. ✅ **The `advance` lifecycle statement is py/ts-only; go/rust lifecycle
    emitters fail hard on it (harness milestone 36 verification of item
    102, finding #29).** CLOSED, AND RE-NUMBERED FROM 100 ON 2026-09-02: two different items were sharing the number 100 in this section, and this is the second filing of a harness finding that was worked under the number 112 in "Follow-ups from harness findings". Closed on `2605e074` (go half: `advance` lowers to the go clock coeffect) and `dec1fd4f` (rust half), so `revl test --backend go|rust` no longer refuses the step. The report that follows is the original filing, kept for its repro. Item 102 landed `advance <n><unit>` and the harness
    now asserts timer firings in-language (py 4/4, vitest ts). But
    `revl test --backend rust|go` on the same document fails with
    `emitter refused: lifecycle test "...": unknown lifecycle step
    'advance'` — a hard FAIL, not an honest "not yet on this tier" skip
    (the pattern `_timer_follow_on` / `_lifecycle_refusal` use). Item 99
    gave go/rust timers; the advance statement needs the same treatment in
    those lifecycle emitters (lower `{"step":"advance","ms":N}` to the
    tier's clock-advance), or at minimum a clean skip-with-reason. Repro:
    `revl test src/timer_tests.rvl --backend rust` in revl-harness.

448. ✅ **The go host Map is `map[string]string` only — a revl
    `Map[Str, Int]` silently mis-lowers to invalid go (harness milestone 36
    verification of item 99, finding #30).** CLOSED, AND RE-NUMBERED FROM 101 ON 2026-09-02: two different items were sharing the number 101 in this section, and this is the second filing of a harness finding that was worked under the number 113 in "Follow-ups from harness findings". Closed on `2605e074`: the go host `Map` is generic over its value type and each bind instantiates it, so `Map[Str, Int]` lowers to valid go. The report that follows is the original filing, kept for its repro. Item 99 made go lifecycle
    tests build for the first time, exposing a long-latent gap: the go
    emitter's host Map (`backends/go/emit.py` `type Map struct { m
    map[string]string }`) holds string values only, but the harness's
    counters (`Map[Str, Int]`, e.g. the timer tests' `Logger`) and
    ledgers (`Map[Str, List[Msg]]`) emit `Get`/`Insert` against it and
    `go test` fails with `cannot use _v (variable of type string) as
    int64 value`. Never surfaced before because go never built a v3
    harness document (mtier blocked by arrows) and no-lifecycle documents
    short-circuit without building. The `effect` acquisition binding
    cannot be annotated (`an acquisition binding cannot be annotated`), so
    there is no in-language workaround. Fix: lower a value-generic host
    Map on go (`Map[V]` or `map[string]any` + typed accessors), or refuse
    non-string Map values honestly at emit time — the tier-gap fence.
    Repro: `revl test src/timer_tests.rvl --backend go` in revl-harness
    (fails in `go test`); the same Logger compiles on py/ts/rust.

449. ✅ **Item 101's rust clone fix covers the `_V3Ctx` renderer, not the
    `_Env` provide-method renderer — a LOCAL reused after an emit/effect
    still moves (harness milestone 36 verification of item 101, finding
    #31).** CLOSED, AND RE-NUMBERED FROM 102 ON 2026-09-02: two different items were sharing the number 102 in this section, and this is the second filing of a harness finding that was worked under the number 114 in "Follow-ups from harness findings". Closed on `dec1fd4f`: `_method_body_lines` clones a reused non-Copy LOCAL at the emit/effect acquire, not only params, with a real cargo gate in `backends/rust/test_emit_rust.py`. The report that follows is the original filing, kept for its repro. Restoring the mtier agent's assistant re-append (the item-95
    workaround) re-surfaced E0382: `emit sessions.append(sid, Msg { role:
    ..., content: answer })` then `return answer` — `answer` is a *local*
    bound by `let`, and `_method_body_lines` clones only PARAMS into the
    acquire rename (`acquire_rename[param] = param.clone()`), never locals,
    so the record-literal field `content: answer` renders bare and moves.
    Item 101's `_by_value_arg` clones (record fields + direct service args)
    live in the `_V3Ctx`/`_render_expr` path (module fns and pure methods);
    an effectful provide method renders through `_Env`/`_method_body_lines`,
    which the fix did not touch. The author's three pinning tests pass —
    they exercise the `_V3Ctx` shapes — while the mtier re-append (the very
    shape item 101 was filed for) still fails. Fix: extend the clone to the
    `_Env` path — track let-bound locals' types in the method scope and
    clone a reused non-Copy local at the emit/effect acquire (and record
    field), mirroring `_by_value_arg`. Repro: `mtier/agent.rvl` in
    revl-harness with the re-append restored — py/ts pass, `cargo test`
    E0382s; with the workaround the mtier is green 2/2 on rust.

450. ✅ **The py tier erases async externs to blocking `def`s, so a revl
    component cannot await a host operation (harness milestone 37, finding
    #32).** CLOSED, AND RE-NUMBERED FROM 103 ON 2026-09-02: two different items were sharing the number 103 in this section, and this is the second filing of a harness finding that was worked under the number 115 in "Follow-ups from harness findings". Closed on `308641d2`: an async extern emits `async def` on py and is awaited at its call sites (`_PY_ASYNC_EXTERNS`, `backends/python/emit.py`). The report that follows is the original filing, kept for its repro. The dynamic-plugin loader is host-owned because a component-side
    `unload` cannot express `await fiber.dispose()`: `_emit_externs`
    (backends/python/emit.py) emits EVERY extern — async or not — as a
    blocking `def`, and the py await-seed deliberately excludes externs
    (emit.py:97-99: "an async extern erased to a blocking `def`"). An async
    extern whose host body wants to await (dispose a fiber, wait on a
    promise) is therefore inexpressible on py; the ts tier handles it (it
    awaits async externs). Ask: emit async externs as `async def` and await
    them at their call sites on py (matching ts; sync @py bodies keep
    working — the urllib body just returns). That would let a `PluginHost`
    component provide the DSH-shaped `plugins` service (load/list/unload)
    the harness currently wires host-side. Repro: milestone 37's
    `plugin_bridge.unload` is async host code — a revl `plugin_unload`
    extern with `await fiber.dispose()` in its @py body cannot compile on
    py (no await in a blocking def context).



104. ✅ **`revl import cordis` cannot see DSH's real plugin shapes — Service
    subclasses with a non-`Service` base name and decorated methods are
    invisible, and named record types across local imports are not
    transcribed (harness finding #33).** The harness can launch a typed
    cordis plugin end-to-end (import -> wire externs -> load -> use ->
    unload; `tools/dsh_plugin_demo.py`), but DSH's real plugins refuse at
    the importer. Three concrete gaps, each independently useful, with the
    real DSH plugins as the test corpus:

    (a) **surface recovery matches only `extends (?:Name\.)*Service`.**
    `src/revl/import_cordis.py:507`'s regex misses a Service subclass whose
    base is a single identifier ending in `Service` — `extends
    TypertRemoteService` (the DSH gateway pattern) is invisible, so
    `PluginInventoryGateway` reports "exposes no method surface this
    importer can read". Minimal repro (verified): a class extending a
    `Service` subclass with a typed method refuses. Fix: match any base
    ending in `Service`; verify `_members` skips `@Decorator(...)` lines
    (Typert methods carry `@Remote('list')`).

    (b) **named record types across local imports are not transcribed.**
    `list(): PluginInventorySnapshot` — the interface lives in
    `./types.ts` — is unrecoverable, so even with (a) the gateway's only
    method refuses. Walk the plugin's local imports and transcribe
    records/lists/unions (`entryId: string` -> `Str`, `enabled: boolean`
    -> `Bool`, `fiberPhase: union` -> ADT), following the family's "no
    guessing" rule.

    (c) **partial import**: when SOME ops recover, emit them and mark the
    rest `// UNRECOVERED` — today "every operation had an unrecoverable
    signature" voids the whole service even when a few methods could
    survive. `AsyncIterable<StreamChunk>` and `unknown` stay honest
    refusals (no revl spelling; the escape hatch stays refused).

    Immediate workaround (works today): hand-declare the service boundary
    — exactly the "trusted, not checked" contract the importer documents
    (the `dsh_plugin_demo` path: service + externs + provider).

    **CLOSED (verified 2026-09-02 on main `5f03c805`, before any change in
    this branch): landed as item 116.** (a) a base ending in `Service` is a
    service root and an in-file base chain through a non-`Service`-named
    intermediate is walked, (b) `@Decorator(...)` lines are skipped to reach
    the method, (c) records across local imports are transcribed, and a
    partial import emits the recovered ops and marks the rest
    `// UNRECOVERED`. All four re-verified by re-running the importer.
    Verification was against SYNTHETIC fixtures mirroring the shapes this
    item quotes, not the real DSH checkout, which is not present on this
    machine.


105. ✅ **`revl import cordis` item 116 landed but DSH's real gateway STILL
    refuses: imported Service-named bases and `.ts`-extension imports are
    not resolved (harness verification of item 116, finding #34).** Item
    116 recovered in-file base chains, decorated methods, and extensionless
    local imports — but the real `PluginInventoryGateway` (extends
    `TypertRemoteService` imported from `@deepseek-ai/dsh-typert-protocol`,
    and `import type { PluginInventorySnapshot } from './types.ts'` WITH the
    `.ts` extension) still reports "exposes no method surface this importer
    can read". Two confirmed gaps (both minimally repro'd):

    (a) **external Service-named base**: `_service_class`'s `chain_of` only
    walks LOCAL classes (`by_name`); a base imported from another package
    terminates the chain, so the plugin class is invisible. Fix: treat a
    base that is not local but whose name ends in `Service` (the cordis
    convention — `TypertRemoteService`, etc.) as a service root.
    Repro: `class Gateway extends TypertRemoteService { @Remote('list')
    list(): string[] }` (base imported) — "no method surface".

    (b) **`.ts`-extension imports**: `_resolve_module` appends candidate
    extensions to the raw spec, so `from './types.ts'` (real DSH style)
    probes `./types.ts.ts` and resolves nothing — while the extensionless
    `from './types'` (the fixture style) works. Fix: strip an existing
    `.ts`/`.tsx`/`.mts`/`.d.ts` extension before probing, or probe the
    bare path too. Repro: a method returning `Snapshot` imported from
    `./types.ts` — "unreachable: the nominal type ... not defined in this
    file or a local import"; the same import without the extension
    transcribes fine.

    With (a)+(b) the real DSH `PluginInventoryGateway.list() ->
    PluginInventorySnapshot` (record, in `./types.ts`) should import.
    Corpus: `packages/host/plugin-inventory/src/index.ts` in the DSH
    checkout.

    **CLOSED (verified 2026-09-02 on main `5f03c805`, before any change in
    this branch): landed as item 134.** A class extending
    `TypertRemoteService` imported from `@deepseek-ai/dsh-typert-protocol`
    recovers its decorated surface, and `import type { X } from
    './types.ts'` resolves; `_resolve_module` now strips a recognised
    extension before probing, so the NodeNext `./types.js` spelling resolves
    back to `types.ts` too. Verification was against SYNTHETIC fixtures
    mirroring the shapes this item quotes, not the real DSH checkout, which
    is not present on this machine.


106. ✅ **The rust emitter's `\u{...}` non-ASCII escape collides with
    `format!()` placeholders in assert/format strings (harness milestone
    40 verification of items 112-114, finding #35).** With item 112/113
    landed, `revl test --backend rust src/timer_tests.rvl` (the advance-
    based timer suite) failed to compile: `invalid reference to positional
    argument 2014 (no arguments were given)` at the emitted `assert!`
    lines. Root cause: `_string` (backends/rust/emit.py:382) escaped a
    non-ASCII scalar (the em dash U+2014 in a test name) as `\u{2014}`,
    and when that literal landed in a format-string position (`assert!(..,
    "...")` / `format!`), Rust's format macro parsed the `{2014}` inside
    the escape as a positional-argument reference. The printable-scalar
    half of this (an em dash, or any printable non-ASCII test name) was
    already fixed under item 135 / finding #35 (`_string` now emits a
    printable non-ASCII scalar literally, no `\u{...}`, no brace) before
    this item was re-examined; that fix does not reproduce any more (see
    below) and stays; `\u{...}` is kept for the lone-surrogate/unprintable
    case, which cannot appear literally in UTF-8 source.

    Re-verifying turned up two more instances of the SAME root shape (a
    `_string`-escaped value landed in a `format!`-family FIRST argument,
    which Rust scans for `{...}` placeholders) that item 135's fix did not
    close, both in `_emit_v3_lifecycle_tests`:
    (a) `where` (the per-test label used to build every load/unload/call/
    assert/no_residue message) was itself built via `_string(test['name'])`
    and then handed to a SECOND `_string(where + suffix)` call at each use.
    For a test name that still uses the residual `\u{XXXX}` escape (an
    unprintable scalar, e.g. DEL U+007F), the second `_string` call
    re-escapes the first call's own backslash (`\` -> `\\`), stranding the
    trailing `u{XXXX}` as literal, un-escaped text in the compiled string's
    runtime value, and format!'s parser reads `{XXXX}` right back out of
    it. Fixed by keeping `where` a raw (un-escaped) Python label, escaped
    exactly once at each downstream call site.
    (b) Independent of (a): `_string` never doubles a literal `{`/`}`, so
    ANY test name containing a literal brace (`"... {curly} ..."`, no
    non-ASCII involved at all) broke the same two `assert!` message sites
    with `cannot find value 'curly' in this scope`. `_v3_interp`/`_format`
    already double braces before calling `_string` for this exact reason;
    the lifecycle-test assert/no_residue messages did not. Fixed with a
    new `_escape_format_braces` helper (mirrors the existing `_v3_interp`
    idiom) applied at the two `assert!` message sites only, not at the
    `.expect(&str)` sites (`.expect` takes a plain, non-format `&str`, so
    doubling there would show doubled braces verbatim).

    Both are cargo-verified (`backends/rust/test_emit_rust.py`, new cases
    for the double-escape and the literal-brace shape) and the corpus
    (`tests/fixtures/emit_rust_corpus/strings.rvl`) gained a `curly()` case
    so the self-host oracle (`tests/test_selfhost_emit_rust.py`) exercises
    `escape_braces`/`render_interp` on a literal brace too. The self-host
    mirror (selfhost/emit_rust.rvl) does not implement lifecycle-test
    emission at all (documented exclusion at the top of the file), so (a)
    and (b) have no self-host counterpart to port; `string_lit`, the
    self-host `_string` mirror, is untouched by this fix and still bridges
    `backends/rust/emit.py::_string` verbatim.

107. ✅ **Item 134 landed (external `*Service` bases + `.ts`-extension
    imports) but the real DSH gateway still refuses: `Branded<'Tag'>` and
    string-literal unions are unmapped (harness verification of items
    134/135, finding #36).** `revl import cordis` on
    `packages/host/plugin-inventory/src/index.ts` (the DSH checkout) now
    recovers the class surface, then stops at the types:
    `unrecoverable signature on 'list': a generic type this importer does
    not map (known: Array, Promise, Record) — the TypeScript type was
    Branded<'PluginEntryId'>`. Two extensions, both verified against the
    DSH types (readonly fields already strip fine):

    (a) **`Branded<'Tag'>`** — DSH's idiomatic branded-string pattern
    (`import { Branded } from '@deepseek-ai/dsh-brand'`; `type EntryId =
    Branded<'EntryId'>`). A branded string IS a string — the brand is a
    phantom tag — so map it (and the alias through it) to `Str`, the way
    `Array`/`Promise`/`Record` are mapped. Repro:
    `list(): Snapshot` with `Snapshot.entries[].entryId: Branded<...>` —
    refused today; `type EntryId = string` in the same file imports fine.

    (b) **string-literal unions (with null)** — `PluginFiberPhase =
    'pending' | 'loading' | 'active' | 'failed' | 'unloading' | null`
    refuses: "a union of several concrete types is a sum type with no tag;
    revl needs a named variant, which this file does not define". For a
    literal-only union the literals ARE the tags — synthesize a named
    variant from them (the reverse of revl's variant->TS-union lowering),
    with `null` -> `Opt`. Corpus: `packages/host/plugin-inventory/src/
    index.ts` + `types.ts` in the DSH checkout; with (a)+(b) the gateway's
    `list(): PluginInventorySnapshot` (record + branded id + phase union)
    should import, and the harness can launch the real gateway.

    **CLOSED (verified 2026-09-02 on main `5f03c805`, before any change in
    this branch): landed as item 137.** `Branded<'Tag'>` maps to `Str` both
    by following a local alias body and, when `Branded` ships from another
    package, through the documented fallback; a literal-only union
    synthesizes a named variant with `null` -> `Opt`, while a union mixing a
    literal with a real type still refuses honestly. Verification was
    against SYNTHETIC fixtures mirroring the shapes this item quotes, not
    the real DSH checkout, which is not present on this machine.


108. ✅ **Item 137 landed (Branded->Str + literal-union->variant) but the
    real DSH gateway still refuses its LEADING-PIPE union style (harness
    verification of item 137, finding #37).** Verified: `Branded<'Tag'>` ->
    `Str` and `'pending' | 'active' | null` -> synthesized variant
    (`Phase = Pending | Active`, `null` -> `Opt`) both work — but the
    actual DSH `PluginFiberPhase` is formatted with a leading pipe per
    member (`| 'pending' | 'loading' | ...`), and the importer's union
    parser does not strip the leading `|`: `unrecoverable signature on
    'list': no revl spelling for this type — the TypeScript type was
    '| 'pending''`. The SAME union without the leading pipe imports fine.
    Fix: when splitting union members on `|`, drop an empty/leading
    fragment (TS allows `type X =\n  | 'a'\n  | 'b'`). Repro:
    `type Phase =\n  | 'pending'\n  | 'active'\n  | null` -> refused;
    `type Phase = 'pending' | 'active' | null` -> variant synthesized.
    ALSO verified: a MULTILINE union whose first member has no pipe
    (`type Phase =\n  'pending'\n  | 'loading'\n  | null`) silently
    collapses to the FIRST member (`phase: Str`, no variant, no Opt) —
    the type scan stops at the newline, so continuation members are lost.
    Both are the same fix area: the union member scanner should split on
    `|` over the WHOLE declaration (strip leading pipes, treat newlines
    as whitespace within a type) instead of stopping at the first
    member/line. With this, `PluginInventoryGateway.list():
    PluginInventorySnapshot` (the DSH checkout's
    `packages/host/plugin-inventory/src/`) imports end to end with the
    variant + Opt intact.

    **CLOSED (verified 2026-09-02 on main `5f03c805`, before any change in
    this branch): landed as item 138.** Both spellings work: the
    leading-pipe-per-line union and the `=`-line-then-bare-first-member
    multiline union synthesize the same variant, and the leading-pipe form
    imports byte-identically to the inline form. The full gateway combining
    EVERY hard spelling at once (external base, `.ts` import, externally
    imported `Branded`, leading-pipe union) had no regression fixture;
    `tests/fixtures/cordis/dsh_gateway.ts` + `dsh_types.ts` now lock it.
    Verification was against SYNTHETIC fixtures mirroring the shapes this
    item quotes, not the real DSH checkout, which is not present on this
    machine.


109. ✅ **The py emitter does not await an async emission nested inside a
    ternary expression in an async method body (harness finding #38).** The
    web_shell's `dispatch` is a ternary chain whose branches are emissions —
    including `emit agent.run_in(...)` (async). It works on py only by
    accident while dispatch is declared sync (the method returns the
    coroutine and the driver awaits it); declaring dispatch `async` (the
    A1-correct shape for reaching async emissions) breaks py: the emitted
    `async def` returns the ternary result WITHOUT awaiting the nested
    `emit`, so the caller gets a coroutine object (`coroutine ... was never
    awaited`, assertion fails). Minimal repro (verified):
    `async fn route(p) { return p == "go" ? emit m.complete(p) : "idle" }`
    in an async provide method — `revl test --backend py` fails; a
    statement-level `let x = emit m.complete(p)` in the same method awaits
    fine. The await-seed must cover emissions nested in ternary/expression
    positions of async method bodies, or the ternary emit must be rejected
    in async bodies. This is also the likely root of the web ts `{}`
    result (sync dispatch reaching async run_in mis-lowers on ts).

    **Closed** — this is the same defect as item 141, fixed there by
    `b90b2d19` (merge `71cca864`, 2026-08-25): the await-seed was extended to
    await an async service-op emission wherever it lands in an async body, not
    only in statement/return position, on py AND ts, suppressed inside arrow
    bodies so the item-92 async-arrow shape stays byte-identical. Re-verified
    on `92fdafcb`: this item's own repro (`async fn route(p) { return p ==
    "go" ? emit m.complete(p) : "idle" }`) now emits
    `return ((await _revl_ctx.m.complete(p)) if (p == 'go') else 'idle')` and
    `revl test --backend py` passes; ts emits the matching
    `(await ctx.m.complete(p))`. The py side shipped with no direct pin —
    `b90b2d19` touched only a ts generated golden and an assert-diagnostics
    test — so `backends/python/tests/test_async_expr_await_141.py` now pins
    it, textually and at runtime, over the ternary chain plus the four other
    expression positions an emission can occupy (template interpolation, a
    binary operand, a list element, a record field). It fails against
    `b90b2d19^` with exactly the reported symptom (`chain returned coroutine,
    not a value`). No emitter change was owed.

110. ✅ **The ts lifecycle-assert emitter names its assertion temporaries
    `l`/`r`, which collide with a user binding of the same name (TDZ)
    (harness milestone 42, finding #39).** Wiring real node:fs ts externs
    (fs/durable pass on ts now — 11/15) surfaced a ts-emitter bug: a
    lifecycle test with `let r = call tools.call("read", ...)` then
    `assert r == "hello from revl"` emits
    `{ const l = r, r = "hello from revl"; expect(revlEq(l, r), ...) }` —
    the assert literal is bound to the SAME name as the user's binding, so
    `const l = r` initializes from the block's own not-yet-initialized `r`
    (ReferenceError: Cannot access 'r' before initialization). The py
    tier is unaffected (python scoping). Same underlying harness finding
    #39 as item 143, filed separately and fixed there before this item was
    picked up: `b90b2d19` renames the temps to `$revl_l`/`$revl_r` — `$` is
    outside revl's identifier alphabet (`[A-Za-z_][A-Za-z0-9_]*`, lexer.py),
    so no revl source can ever bind that spelling, and a plain `_l`/`_r`
    prefix (this item's original suggested fix) would NOT have been safe
    since `_` does start a legal revl identifier. Verified on
    `origin/main`: `emit.py` on a `let r = ...` + `assert r == ...` fixture
    now emits `const $revl_l = r, $revl_r = "hello from revl"` — no
    collision. No code change needed for this item; `backends/typescript/emit.py`.

111. ✅ **A sync method that returns an async emission's result yields `{}`
    on ts (harness milestone 44, finding #40).** The web_shell's dispatch
    is the shape: a SYNC emission method whose ternary returns
    `emit agent.run_in(...)` (async). py works by accident (the method
    returns the coroutine and the lifecycle driver awaits it); ts FAILS:
    the emitted `route(p) { return (cond ? ctx.m.complete(p) : "idle") }`
    returns a Promise, and the generated lifecycle driver does
    `const out = root.k.route("go")` — NO await — so the assertion sees
    the Promise (`{}`). The checker allows sync-reaching-async through a
    ternary (the A1 gap, item 117) and the ts driver assumes a sync
    method's result is a plain value. Minimal repro (verified):
    `fn route(p) { return p == "go" ? emit m.complete(p) : "idle" }`
    in a sync provide method — ts `left = {}`; py passes. The harness
    restructured web_shell to the A1-correct async dispatch with hoisted
    emissions (16/16 py + ts) — but the underlying tier gaps remain: the
    checker should refuse sync-reaching-async (item 117), and/or the ts
    driver should await a sync method's promise result. Related: finding
    #38 (py doesn't await ternary-NESTED emits even in async methods).

    **Closed** — by the checker, which is the first of the two options this
    item lists. Item 117 (`7665139b`, 2026-08-25) extended the A1 async-reach
    with `_reached_async_req_ops`, which walks a provide-method body for
    req-target async service ops in ANY position rather than only in statement
    position. A sync provide method that reaches one is now refused, so this
    item's repro never reaches an emitter and no ts driver change is owed.
    Re-verified on `92fdafcb`: `fn route(p) { return p == "go" ? emit
    m.complete(p) : "idle" }` is refused with the A1 diagnostic: "Router.route
    is declared sync, but this implementation reaches async operation
    m.complete — a sync method has no in-flight window (A1)". Pinned by
    `examples/rejections/a1_async_op_sync_ternary.rvl` in the frontend suite
    and re-checked from the py tier by
    `test_sync_method_reaching_async_op_in_expression_is_refused` in
    `backends/python/tests/test_async_expr_await_141.py`. The A1-correct async
    spelling stays admitted and is awaited on both tiers (item 109/141).
