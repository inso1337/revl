# findings-drivers — run-driver parity (FR-8/10) and the wasm capability matrix (FR-11)

Branch `agent/fr8-go-drivers` off devwip @ b460a63. Three small items from the
harness harvest (docs/v2.0-roadmap.md item 77(e), FEATURE-REQUESTS.md FR-8 /
FR-10 / FR-11):

- **FR-10** — `revl run --backend java` compiled `--release 17` while the
  emitter lowers `match` to Java 21 pattern `switch` expressions; every
  match-bearing composition failed javac.
- **FR-8** — `revl run` accepted py/ts/rust/java/wasm but not `go`, though go
  is a first-class test tier with a working emitter.
- **FR-11** — the wasm substrate's string builtins and service boundary widths
  were undocumented; the harness's `split`/`join` protocol was refused with
  "unsupported builtin method 'split'" and the "runs on all six" claim had no
  precise meaning for the substrate.

## 1. Refusal log

Every `revl compile` rejection I hit (or the frontend refused for me):

1. **`javac (components) failed: error: patterns in switch statements are not
   supported in -source 17`** (from `revl run --backend java` on a match
   composition). Verdict: **caught-bug** — the run driver was behind the test
   runner; the docs and `revl test --backend java` already said 21. The
   checker/emitter were right; the driver was wrong. Fixed to `--release 21`
   (FR-10).
2. **`javac: error: 'void' type not allowed here` — `final var q = (void)
   __revl_ignored_1.value();`** (the same java run, after the release fix).
   Verdict: **caught-bug** (second-order) — my FR-10 regression test exposed a
   real lowering gap: match arms in *component/method bodies* carried no
   `payload_type` (the pure-fn path wrote it, the method-body path did not), so
   the java emitter cast the bound value to `void`. Fixed in `lower.py` to write
   arm payload types in method bodies exactly like the pure-fn path. This is the
   dream refusal: a regression written for one bug caught a second, real one.
3. **`f: unsupported builtin method 'split'`** / **`'join'`** / **`'repeat'`**,
   **`indexOf is not lowerable on this tier yet`**, **`type 'Float' is not
   lowerable — this tier supports Int/Bool/Str/Bytes/List/record/variant/
   Opt/Result values`** (wasm emit probes). Verdict: **friction** — the refusals
   are correct (the substrate tier is deliberately the strictest) but they were
   undocumented as a matrix; I had to read `emit.py` to learn the exact
   supported set. That is precisely FR-11; the matrix is now docs/
   wasm-capabilities.md.
4. **`config blocks are not lowerable — the cordis-wasm runtime has no
   instantiation-config channel yet`** (wasm probe). Verdict: **friction** —
   correct and named, fine once the matrix exists.
5. **vitest: upstream finding 2 — an undo that registers a new effect is
   accepted while UNLOADING** (pre-commit hook, not a revl refusal but a suite
   failure). Verdict: **false-positive** in context — the test pinned the *old*
   upstream cordis behavior (leaked === true) while the shared node_modules had
   already been moved to the hardened `inso1337/cordis@harden-assert-active`
   fork (assertActive now refuses UNLOADING). My base b460a63 predates upstream
   `03d6c84`, which flipped the test. Not my change's fault; synced the pin +
   test to the repo's current state.

## 2. Friction log

- `[slow]` **The pre-commit hook runs the full `pytest tests/ -q` suite
  (~105 s) plus vitest plus the conformance matrix on every commit.** Three
  commits × three minutes each; my first `bash` timeout (60 s) killed two
  hook runs mid-flight and misled me about whether the commit landed. The hook
  is the repo's gate and I don't propose weakening it, but a commit-side note
  ("this runs the full suite; give it minutes, not a minute") would have saved
  a stalled cycle and one history-rewrite.
- `[slow]` **The shared node_modules is a symlink to the main checkout, and the
  main checkout advanced mid-session** (cordis fork pinned at 02:20 while I
  was working). My branch's vitest gate flipped red with zero tree changes on
  my side; diagnosing took reading the cordis fork's `assertActive` source and
  the main checkout's git log. `env`-class drift, fixed by syncing package.json
  + lockfile + the flipped test.
- `[nit]` `git commit -m` with a backtick in the message silently ran command
  substitution (`\`once\`` became empty) and mangled the commit message; the
  amend then landed on the wrong commit (the tree above HEAD). Pure shell
  discipline, but the second failure (amend-amending-the-wrong-commit) cost a
  `reset --soft` + two recommits.
- `[nit]` The go runner log pads the subject to 16 chars; `out.index("swap  |
  Halver")` prefix-matches `"swap  | HalverUser"` — a test assertion bug I hit
  and fixed by matching the padded form. Nothing in the toolchain; a
  self-inflicted nit.
- `[nit]` `backends/wasm/README.md`'s restriction table was stale: it claimed
  "non-Int component services" and "variant values + `match` in v3 fns" are not
  lowerable, but the v3 value-model wave already widened the service boundary
  and lowered tagged unions (both compile today). Had to probe the emitter to
  be sure the README was wrong and not my probe. Now fixed and cross-referenced.

## 3. What revl gave you

- The **type system caught the `payload_type` gap the moment javac did**: the
  IR arm carried the binding's type on the pure-fn path, and the method-body
  path's omission became visible the instant a real match crossed it. The fix
  was twelve lines mirroring code that already existed — the language's own
  structure told me where the asymmetry was.
- The **fmt gate admitted `examples/java_match.rvl` on the first try**
  (`ir_equivalent` returned admitted) — a new example with `match` in a method
  body dropped into the corpus without tripping the canonical-form gate.
- The **go once round-trip booted clean first try**: emit → `go build` →
  `[run] UP` → LIFO teardown → `0 live plugin(s)` / `0 service(s) still
  provided` → `NO-RESIDUE` → `DOWN` → exit 0, with the gitignored generated
  package leaving the checkout byte-clean. The stc-go runtime really is
  dependency-free: no network, warm module cache, ~2 s build.
- **Honest refusals beat silent degradation** on the wasm tier: every probe I
  threw at it (split, join, repeat, indexOf, Float service, Map service, config)
  came back as a named `EmitError` — that is what makes the capability matrix a
  documentation task rather than a compatibility audit.

## 4. Time-to-green

Compile→refuse→fix cycles: 3 (java release 17; java `void` cast; the Halver
test prefix collision). The longest single debugging stall was the vitest
failure — not a revl compile at all, but ~20 minutes reading cordis-ts internals
and the main checkout's history to establish that the shared node_modules had
drifted. The two `javac` failures were each < 2 minutes once the diagnostic
named the file and line. The wasm matrix cost zero fix cycles (all probes
refused as documented). Go driver: zero refusal cycles (first build green).

## 5. Cost ledger

- `env` — vitest flipped red because the main checkout's node_modules moved to
  the hardened cordis fork mid-session while my base branch pinned rc.8.
  ~20 min of diagnosis; the fix (sync pin + test) is upstream's own `03d6c84`.
  The single change that would have cut the most cost: **a fresh-worktree
  checklist noting the shared node_modules/venv are symlinks into a live main
  checkout** — any branch based more than a few commits behind devwip should
  diff `backends/typescript/package.json` + lockfile against main before
  trusting the vitest gate.
- `tooling` — the 60 s default bash timeout killing the pre-commit hook's full
  suite twice, plus the subsequent history rewrite. ~15 min. A "this hook runs
  the full suite" hint in the hook's own output (it does say so, but only after
  the fact, and the tool timeout hides it) would have avoided the rewrite.
- `diagnostic` — `unsupported builtin method 'split'` did not say which tier or
  where the supported set lives; the FR-11 doc closes exactly this. ~10 min of
  reading emit.py that the matrix now makes unnecessary.
- `docs-gap` — the stale wasm README rows ("non-Int component services",
  "match not lowerable") contradicted the current emitter; had to probe to
  disbelieve the docs. ~10 min.
- `spec-ambiguity` — none; the three FRs matched the code's intent (go tier
  works, java driver behind, wasm strictest).
- `self-inflicted` — the commit-message backtick + wrong-commit amend, ~10 min.

## What landed

- FR-10: `run_java.py` compiles `--release 21` behind a `JAVAC_RELEASE`
  constant; `examples/java_match.rvl` + a JDK-gated boot test and two
  runtime-independent gate/emit tests; the method-body match `payload_type`
  lowering fix in `lower.py`.
- FR-8: `src/revl/run_go.py` (gate + once driver), `go` wired into `run.py`,
  once-mode no-residue in `backends/go/placement_runner/main.go` (with the
  generated `RevlStillProvided` bridge read), `tests/test_run_go.py`.
- FR-11: `docs/wasm-capabilities.md` (value model, builtin matrix, two service
  boundaries, refusal table, writing-to-the-subset guidance), cross-referenced
  from README.md, backends/wasm/README.md (stale rows corrected) and
  docs/stdlib-2.0.md.
- Branch hygiene: synced the cordis pin + upstream finding-2 test to the
  hardened fork (the repo's own `03d6c84`).
