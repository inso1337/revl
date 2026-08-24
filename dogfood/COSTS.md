# Dogfood cost ledger — measured per run

Maintained by the orchestrator from harness telemetry (token count, wall
duration, tool calls) at each run's completion; the causal story behind each
number lives in that run's `findings-<slug>.md` §5. Numbers are the run's
final reported totals — a run killed and resumed reports segments, marked ~.

| Run | Task | Tokens | Duration | Tool calls | Outcome | Cost notes |
|---|---|---|---|---|---|---|
| rebuild-00015 | item 67: extern undo `result` binding | ~112k | ~16 min active (3 segments) | ~96 | landed bf306e7 | rebase silently dropped upstream-reverted commits (cherry-pick recovery); rust golden's cargo build was the long tail |
| rebuild-00016 | item 68: fault splice off-by-one | ~199k+ (final segment; earlier segment lost to session limit) | ~5 min final segment | 31 | landed 37d2d80 | diagnosis segment killed by session limit mid-run; port over ~54-commit devwip drift cost a merge + re-audit of every step-index site |

Wave-8 runs append below.

## DSH restart wave (2026-08-24) — 14 runs, all landed

| Run | Task | Outcome | Cost notes |
|---|---|---|---|
| acc-rust-go | instance accessor go/rust | landed c3619b6 | go tier had a real resolve bug — never caught by CI (PR #41's go job failed with the same panic and was merged anyway); fixed 0c09da7 |
| acc-ts-java | accessor ts/java | landed 744266c | java exec test needs REVL_CORDIS4J_CLASSES (CI compiles cordis4j); java Map value type not in the IR — oracle re-derives it (friction, findings-javamap) |
| acc-wasm | accessor wasm | landed 8a20a63 | wasmtime-gated |
| records | item 71 fence | dbf7973 | cherry-picked from revl-work clone (branch lived in the other clone) |
| w8-friction | 76abc | ae4e9dd | lower.py merge conflict (checker-frontier inferred-type sweep vs friction maplit pin) — both additive |
| w8-hygiene | 72+73abc+74cd | fccde49 | pre-commit hook exceeds the 60s tool timeout (killed commits mid-hook); vitest leaves scratch files |
| w8-runtime | 74ab | e15ce1e | npm rewrites git deps to git+ssh — codeload tarball pin for anonymous CI |
| w8-checker | 75bc | 4e899f2 | provide-method let-sweep divergence only visible via the wasm tier |
| w44/w60/w64 | delivery/mocks/semver | ac27f23/24cc017/db55031 | all three were interrupted agents' uncommitted work — verified, finished, landed |
| shadow3 | selfhost checker slice | dcb5fb4 | one merge fix needed: annotated-let vs the 75b HOST-METHOD guard |
| fr1 | expressible iteration | 84f3f6a | spec-first per roadmap precedent; the comp-path scope id bug (`id: false`) cost a probe cycle |
| fr2..fr11 | harness feature wave | b36b495..010959d | 6 merges; run.py docstring/KNOWN_BACKENDS conflicts resolved to the merged truth (all six tiers runnable) |
| CI #44-46 | go accessor, rust 3.11, wasm records, java JDK gate, verdict counts | 0c09da7/fdb6ca4/b6074dd | go accessor was the real find: fiber-context resolve + spawn-ready wait |
| item-77 follow-ups | TS arrow params, Java Map, Go v3 placement | d42d629 | TS fix: params bind in the emitted arrow scope; go: v3 typed-core combined emit |

Biggest single cost cuts: (1) the go accessor's resolve-through-fiber-context
semantics should have been caught at review (the PR that merged it had a red
go CI job); (2) the Map value type is not in the IR — both emitters re-derive
it (carry it from the checker into the IR); (3) the pre-commit hook's 60s
claim vs the real ~100s suite — raise the budget or gate it per-suite.

| run | task | outcome | cost notes |
|---|---|---|---|
| harness-m2 | real HTTP provider + JSON protocol (milestone 2) | landed 4d6750b | extern-dedent emitter bug (item 78) — the session's longest single stall; fixed upstream |
| harness-m3 | durable sessions + subagents (milestone 3) | landed 805ce91 | spawn-emit frontend crash (item 82) — worked around by moving `emission` one level up |
| harness-m4 | fs tools + audit-diff approval gate (milestone 4) | landed cf7242b | the gate refused fs_exfil by design — zero cost, the feature working |
| harness-m5 | web GUI shell (milestone 5) | landed bd7d5c7 | no new findings; G6 ternary pattern fully automatic by now |
| harness-m6 | self-hosting admission + multi-session (milestone 6) | landed ad4403a | host-Map keys compile-but-crash (item 84); escape-sequence test source (item 85) |
| harness-m7 | self-evolution (milestone 7) | landed 1664b36 | extern/session module-boundary (docs note) |
| harness-m8 | self-hosting through the web shell (milestone 8) | landed 9444e0e | no new findings |
| harness-m9 | real-model self-evolution (milestone 9) | landed d0943eb | evolver once() had to decode the JSON wire reply — a harness design fix, not a revl gap |
| harness-m10 | multi-session web UI (milestone 10) | landed f1c9b62 | run_in(session,prompt); default-vs-named session coherence in tests |
| harness-mtier | multi-tier proof (string protocol) | landed 41efe81 | rust declared fn types (item 89) — the last loop-shape tier gap |
