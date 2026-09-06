# revl for humans

A practical guide to writing revl 2.0.

**revl** is a language for *spatiotemporal composability*: writing components
that can be loaded, unloaded, and hot-swapped in a running system, where
"unloading leaves no residue" and "dependencies stay coherent" are
**compile-time guarantees**, not runtime discipline.

The one-line pitch: *Cordis has revertible effects as a discipline; revl makes
them a type system*, the jump C++ RAII made to become Rust's ownership. Rust's
borrow checker governs *lexical* resource scope; revl's checker governs
*dynamic component* scope.

## The mental model

A revl program is a set of **components**. A component:

- **requires** services (its inputs, what it reads),
- **provides** services (its outputs, what it publishes),
- and has an **activation body**: a sequence of *revertible effects*
  (`effect E undo U`) that runs when its requirements are satisfied.

When a component deactivates (its provider leaves, or it's hot-swapped out),
every effect it acquired is undone, in derived, LIFO order. You never write
`activate()`/`deactivate()`. You write what to do and the inverse of each thing
you did; the runtime derives the teardown.

### A worked example

```revl
service Database {
  fn query(sql: Str) -> List[Row]
  emission fn execute(sql: Str) -> Int          // crosses the system boundary
}

service Cache {
  fn get(key: Str) -> Opt[Str]
  emission fn put(key: Str, value: Str)         // its body emits, so the interface says so
}

component UserCache requires db: Database provides cache: Cache {
  let store = effect Map.new() undo store.drop()

  provide cache {
    fn get(key) = store.get(key)
    fn put(key, value) {
      effect store.insert(key, value)
      undo   store.remove(key)
      emit db.execute(`INSERT INTO cache_log VALUES (${key})`)
    }
  }
}
```

Reading it:

- `service Database { ... }`, the interface behind a coeffect key.
  `emission fn` marks an operation that *cannot be reverted* (bytes left the
  system), so it must be called with `emit` and appears on the audit surface.
- `requires db: Database provides cache: Cache`, the component's whole
  interface to the world. Nothing else is reachable; `db` is in scope, typed
  `Database`, and stays readable throughout the component's own teardown.
- `let store = effect Map.new() undo store.drop()`, acquire a map, remember
  how to drop it. On teardown, `store.drop()` runs.
- `provide cache { ... }`, publish the `cache` key. `put` mutates the store
  (effect + undo) and *emits* a log write. Because its body reaches an
  emission, the *service* declares it `emission fn`: a declaration is an upper
  bound on its providers, so a plain `fn` may never be implemented by a body
  that emits.
- `` `...${key}` `` is the 2.0 template-string interpolation (1.x `"$key"` is
  gone, `revl fmt --migrate` rewrites old sources).

## The four strata

revl 2.0 is four strata, each with its own rules ([docs/syntax-2.0.md](syntax-2.0.md)):

| stratum | what it is | rules |
|---|---|---|
| 1. Pure expressions & functions | a TypeScript subset | totality of the subset |
| 2. Types & data | revl's own, host-neutral | structural checker |
| 3. Components & effects | revl's own (unchanged from 1.x) | G1–G8, A1–A8 |
| 4. Host blocks | verbatim host language | boundary types + G8 audit |

The governing principle: **same meaning → same syntax** (stratum 1 borrows
TypeScript verbatim so it's instantly familiar); **different meaning →
different syntax** (strata 2–4 are revl's own, so the paradigm's constructs
are visually unmistakable).

## The language

### Types (§2)

```revl
type Row = { id: Int, name: Str, active: Bool }       // record (structural)
type Pair[A, B] = { first: A, second: B }             // generic
type TokenKind = Ident | Keyword | IntLit | StrLit    // enum (no payload)
type Outcome = Ok(Row) | NotFound | Invalid(Str)      // ADT (payloads)

// built-ins: Str, Int, Float, Bool, Bytes, Unit
// containers: List[T], Map[K, V], Opt[T], Result[T, E]
// sugar: T?  ==  Opt[T]
```

- Records are structural; ADTs are nominal (defined by their case names).
- **There is no `null`.** Absence is `Opt[T]`: `Some(value)` or `None`. `T`
  flows into `Opt[T]` automatically; `Opt[T]` does **not** flow back into `T`;
  you unwrap with `match` (or `??`).

### Functions & expressions (§3)

```revl
fn count_idents(kinds: List[Str]) -> Int {
  var n = 0
  for (kind of kinds) {                       // TS for-of
    if (kind == "Ident") n += 1               // += on var
  }
  return n
}

fn describe(outcome: Outcome) -> Str {
  return match outcome {                      // exhaustiveness-checked
    Ok(row)      => row.name,
    NotFound     => "-",
    Invalid(why) => why,
  }
}
```

- `fn` is a pure function; `pub fn` exports it. `let` is single-assignment;
  `var` is local mutable (and never escapes, a lambda captures its *current
  value*, not the cell).
- Loops, `if`, arrow lambdas (`x => x + 1`), records, lists, indexing, calls,
  `==`/`===` (identical), `!=`/`!==`, `&&`/`||`, the ternary, TypeScript's
  syntax, verbatim.
- `match` is the ADT eliminator. The checker requires every case (or a `_`
  wildcard); a missing case is a compile error naming the case you forgot.
- Destructuring: `let {id, name} = row` and `let [head, ...rest] = xs`.

### Modules (§1)

```revl sketch
use "./tokens.rvl" { Token, TokenKind, keyword_set }
use "./util/strings.rvl" as strings

pub fn lex(source: Str) -> List[Token] { ... }   // importable
fn helper() -> Int { ... }                        // module-private by default
```

Components are *never* imported, they are *composed*, over a manifest.
Import cycles between modules are a compile error.

### Components & effects (§4)

The core from [DESIGN.md](../DESIGN.md) §3, plus 2.0's two additions:

- **Block effect form**, acquisitions with several pure setup steps:
```revl fragment
let pool = effect {
  let url = normalize(config.url)
  Pool.open(url, config.pool_size)          // last expression = the acquisition
} undo pool.close()
```
- **`fail`**, deliberate L-Raise from an activation body (reverts what's
  accumulated, lands FAILED):
```revl fragment
if (config.replicas < 1) fail "at least one replica required"
```

### Services 2.0 (§5)

```revl
pub service Database {
  fn query(sql: Str) -> List[Row]
  async fn stats() -> Stats                    // may await host async values
  emission fn execute(sql: Str) -> Int
}
```

`commutative` on a service or a single operation declares order-independence
(the opt-in that upgrades a key from LIFO-only to reorderable recovery).

### Host blocks (§6)

FFI is the only door out of confinement, and it must classify itself:

```revl
extern pure fn sha256(data: Bytes) -> Str
  = @ts { return crypto.createHash("sha256").update(data).digest("hex") }
  = @py { import hashlib; return hashlib.sha256(data).hexdigest() }
```

`pure` / `acquire` (must declare `undo`) / `emission` (may declare
`compensate`). An unclassified `extern` does not compile.

### test & verified (§7)

```revl
verified fn add(a: Int, b: Int) -> Int { return a + b }

test "add works" {
  assert add(1, 2) == 3
}
```

`revl test` runs the test blocks; `verified` opts a function into the totality
tier (structural recursion / bounded loops only).

### The stdlib surface

A method call on a value must name a known builtin, anything else is a
compile error, never a host pass-through ([docs/stdlib-2.0.md](stdlib-2.0.md)):

| method | on | note |
|---|---|---|
| `length` / `length()` | Str, List | element count |
| `push(v)` | List | **persistent**, returns a new list (`out = out.push(v)`) |
| `slice(a, b)` | Str, List | half-open sub-range |
| `charAt(i)` / `charCodeAt(i)` | Str | 1-char string / code point |
| `indexOf(v)` | Str, List | first index, `-1` if absent |
| `concat(y)` | Str, List | joined copy |
| `split(sep)` | Str | pieces between separators (JS shape) |
| `join(sep)` | List[Str] | elements joined by `sep` |
| `repeat(n)` | Str | `n` copies concatenated |

## What won't compile

This is the point of the language, each rejection names the guarantee and
the fix ([DESIGN.md](../DESIGN.md) §4):

<!-- docgen:guarantees-humans begin -->
| # | Guarantee |
|---|---|
| G1 | declared access: a component reads only what it requires |
| G2 | provision disjointness: one provider per key (per realm) |
| G3 | acyclic dependencies: a cycle can never activate |
| G4 | every mutation carries an inverse, or admits irreversibility with `emit` |
| G5 | teardown cannot register effects |
| G6 | purity outside effect forms |
| G7 | derived LIFO teardown |
| G8 | the boundary surface is enumerable |
| G9 | untrusted data cannot create authority without a declared declassification |

...plus the lifecycle rules A1, A2, A3, A5, A6, A8 and A9 (await boundaries, no acquisition
after `provide`, `fail` semantics, and so on), the confidentiality rules
`G-SECRET` and `G-SECRET-FLOW`, and the typing rules T1, T2 and T3. The rejection
suite in [`examples/rejections/`](../examples/rejections/) is the
executable spec, and [rejections.md](rejections.md) is the full table.
<!-- docgen:guarantees-humans end -->

## Tooling

The complete subcommand index, every command wired in `src/revl/__main__.py`,
one line each with the doc that details it. The flags and worked examples follow
below; the exhaustive per-command flag reference is
[commands-reference.md](commands-reference.md), and the MCP verbs are in
[mcp-reference.md](mcp-reference.md).

| command | what it does | docs |
|---|---|---|
| `revl compile FILES` | parse → check → link → IR (`-o OUT`, `--json-diagnostics`) | [commands-reference.md](commands-reference.md#revl-compile) · [backend-ir-v1.md](backend-ir-v1.md) |
| `revl explain CODE` | what a diagnostic code guarantees and how to fix it | [commands-reference.md](commands-reference.md#revl-explain) · [why-traces.md](why-traces.md) |
| `revl grammar` | the language surface, sized for a prompt (`--prompt` prints the full pinnable grammar) | [commands-reference.md](commands-reference.md#revl-grammar) · [syntax-2.0.md](syntax-2.0.md) |
| `revl doctor` | diagnose each backend tier, runtime and dependency (OK/WARN/MISSING + version), then smoke-test every available tier (`--json`, `--no-smoke`, `--smoke-timeout`) | [commands-reference.md](commands-reference.md#revl-doctor) |
| `revl scaffold --service NAME` | generate a typed, holed composition skeleton from a spec (`--requires`/`--capabilities`/`--method`/`--emits`/`--config`, `-o`, `--json`) | [scaffold.md](scaffold.md) |
| `revl composition FILE` | resolve a composition document's row table: label, claims, component, config and requires per row, without lowering a body (`--json`, `--admit`) | [composition-rows.md](composition-rows.md) |
| `revl layer check FILE` | the same folded row table with each row's LAYER provenance, so an overlay's origin is visible (`--json`, `--set`) | [composition-layers.md](composition-layers.md) |
| `revl adapt NEED CANDIDATE` | can this candidate service stand in for the required one, and `--emit` the adapter that makes it so | [commands-reference.md](commands-reference.md#revl-adapt) |
| `revl policy evaluate POLICY FILES` | dry-run a boundary policy: per rule, which clauses pass or fail and why (fact against threshold) | [boundary-policy.md](boundary-policy.md) |
| `revl audit FILES` | manifest + G8 boundary surface (`--json`); `--diff PREV.json` is the authority-drift gate, `--accept`/`--accept-all` acknowledge added crossings | [interchange-format.md](interchange-format.md) · [audit-diff.md](audit-diff.md) |
| `revl analyze FILES` | Petri-net reachability liveness: derive a net from the composition and report any reachable deadlock, naming the stranded activation (report-only; `--ir`, `--json`) | [analyze-liveness.md](analyze-liveness.md) |
| `revl diff PREV CURR` | semantic composition diff: components added/removed/changed, emissions gained/lost, provide/require edges, the PR-review tool for agent-generated compositions | [revl-diff.md](revl-diff.md) |
| `revl version PREV CURR` | derive the required semver bump from the interface diff against a previous composition | [derived-versioning.md](derived-versioning.md) |
| `revl changelog --from OLD --to NEW` | the release note derived from the interface, structural and authority delta, with the semver headline | [derived-versioning.md](derived-versioning.md) |
| `revl contract FILES` | federated contracts between sovereign compositions: export a consumer surface, or check a provider against a pinned one | [federation.md](federation.md) |
| `revl erase-report FILES --realm R` | right-to-erasure evidence for one realm (`--json`, `--no-residue-proof`) | [erase-report.md](erase-report.md) |
| `revl plan FILES` | dry run for admission (`--manifest RUNNING.json`, `--replacing`); `-o change.plan` serializes an executable plan | [plan.md](plan.md) |
| `revl apply change.plan` | execute a plan: drift-refuse, verify each step, roll back on failure (`--against RUNNING.json`) | [apply.md](apply.md) |
| `revl undo --history H` | operator undo: replay a generation history and return to an earlier generation through the gate | [generation-history.md](generation-history.md) |
| `revl query emits-to\|withdraw\|depends-on\|reaches\|drift TARGET FILES` | ask the composition a static question (`drift` takes `--gains`/`--loses`) | [queries.md](queries.md) |
| `revl query emitted-between --timeline F --from X --to Y` | which emissions crossed between two steps of a recorded run | [queries.md](queries.md) |
| `revl query touched COMPONENT` | everything a component touched (`--trace` lifecycle JSONL, `--timeline` replay recording) | [queries.md](queries.md) |
| `revl fmt FILES` | canonical formatting (IR-equivalence gated); `--migrate` rewrites 1.x `$`, `--check` for CI | [fmt.md](fmt.md) |
| `revl test FILES` | run `test`/`prop test`/`fault test`/`lifecycle test` blocks; `--backend {py,ts,rust,java,wasm,go,all}`, `--sweep` fault sweep | [prop-test.md](prop-test.md) · [fault-tests.md](fault-tests.md) |
| `revl quarantine FILES [--service NAME] [--policy POLICY]` | grade a candidate with the gauntlet, then run its lifecycle + fault battery inside the wasm sandbox where an escape is a trap | [quarantine-tier.md](quarantine-tier.md) |
| `revl canary FILES --candidate FILE --slice REALM` | run both generations at once, successor on one realm slice; promote (`--promote-to`) or revert on evidence | [verified-canary.md](verified-canary.md) |
| `revl repair --component C [--candidate FILE] [--plan]` | the repair loop: diagnose a fault and re-admit a fix within declared policy bounds | [repair-loop.md](repair-loop.md) |
| `revl run FILES` | boot on a Cordis runtime, see the tier table below and the flag list | [replay.md](replay.md) · [crash-recovery.md](crash-recovery.md) |
| `revl recover --wal FILE` | crash recovery: roll a WAL forward/back to a checked verdict + residue proof (`--restore`, `--json`) | [crash-recovery.md](crash-recovery.md) |
| `revl estop` | the operator's emergency halt: stop dispatching crossings now, unwind nothing, and report what was left stranded | [commands-reference.md](commands-reference.md#revl-estop) |
| `revl branch --wal FILE` | session branch lineage over durable WALs: the branch tree, and the fork partition of a recorded tail (`--at SEQ`) | [commands-reference.md](commands-reference.md#revl-branch) |
| `revl compare LEFT.wal RIGHT.wal` | what two sessions did after a shared fork point, and what durable logs cannot yet say | [commands-reference.md](commands-reference.md#revl-compare) |
| `revl why COMPONENT --trace FILE` | explain a recorded lifecycle transition's cause chain; `--check FILES` runs the withdraw oracle | [why-runtime.md](why-runtime.md) |
| `revl trace FILE` | the causal trace of a recorded run, hop by hop (`--component`, `--model`, `--otel`) | [why-traces.md](why-traces.md) |
| `revl metrics --trace FILE` | capability-aware runtime metrics over a `run --trace` JSONL: emissions by capability, failures by G-rule, average lifecycle duration | [revl-metrics.md](revl-metrics.md) |
| `revl profile --trace FILE` | diff a component's declared emission surface against what a run actually emitted, flagging over-declaration | [revl-profile.md](revl-profile.md) |
| `revl attest FILES` | sign a portable record that this exact composition was admitted (IR hash + verdict + guarantees + timestamp); `--verify` checks one | [revl-attest.md](revl-attest.md) |
| `revl dash` | the supervisor's cockpit: a read-only live view over a session or recorded run, the dependency graph, causal trace, and pending-decisions queue | [dash.md](dash.md) |
| `revl serve --mcp FILES` | serve a booted composition's own provided operations as MCP tools (`--config`, `--composition`) | [mcp-bridge.md](mcp-bridge.md) |
| `revl mcp serve` | the compiler itself as an MCP server (`--files` default composition, `--restore SNAPSHOT.json`) | [mcp-bridge.md](mcp-bridge.md) |
| `revl mcp schema FILES` | project provided services to MCP tool definitions | [mcp-bridge.md](mcp-bridge.md) |
| `revl mcp import MANIFEST` | turn an MCP `tools/list` manifest into revl source | [mcp-bridge.md](mcp-bridge.md) |
| `revl import wit\|openapi\|cordis\|a2a FILE` | import an external interface definition as typed revl source | [import-wit.md](import-wit.md) · [import-openapi.md](import-openapi.md) · [import-cordis.md](import-cordis.md) · [import-a2a.md](import-a2a.md) |
| `revl export wit FILES --service N\|--composition` | generate the standard WIT interface for a revl service/composition | [wit-bridge.md](wit-bridge.md) |
| `revl emit FILES` | render one backend's source directly, no IR round-trip (`--backend`, `--target temporal`) | [backends-roadmap.md](backends-roadmap.md) |
| `revl bundle FILES --out DIR` | emit every tier plus the runtime manifest into one portable bundle (`--backend`, `--topology`) | [deploy.md](deploy.md) |
| `revl verify BUNDLE` | check a bundle tier by tier; nonzero on a failing tier, so it is usable as a release gate | [commands-reference.md](commands-reference.md#revl-verify) |
| `revl deploy MAP` | deploy a composition across process seams with attested admission (the receiver re-hashes IR+artifact and checks the signed evidence chain) and coordinated two-phase rollback; `--dry-run`, `--json` | [deploy.md](deploy.md) |
| `revl truc VERB …` | the component manager namespaced under the compiler, `add`/`rm`/`assemble`/`ship`/`reproduce`, tail passed through unchanged | [truc.md](truc.md) |

Two module entry points sit outside the `revl` subcommand tree. `python -m
revl.lsp` runs the human-facing language server over stdio, so an editor gets
inline diagnostics, hover, and go-to-definition: it pushes
`textDocument/publishDiagnostics` from the checker, answers `textDocument/hover`
from the diagnostic explanations and symbol info, and answers
`textDocument/definition` from the resolver, all read-only over the existing
compiler surfaces (`src/revl/lsp/`). `python -P -m revl.otel run.jsonl` exports a
`revl run --trace` lifecycle trace to OpenTelemetry (a lifecycle transition
becomes a span, its cause a span event, a causal edge a link), so a
composition's causality lands in Grafana, Datadog, Honeycomb or Jaeger; the OTel
SDK is the optional `revl[otel]` extra, and `--json` prints the span model
without it ([opentelemetry.md](opentelemetry.md)).

`revl run` flags: `--backend {py,ts,rust,java,wasm,go}` (all six boot live: py
in-process, the other five each as a separate process over the bridge seam),
`--once` (boot → LIFO teardown → no-residue proof → exit),
`--watch` (recompile on edit; a rejected edit keeps the run alive), `--record`
(record the accumulator for the replay REPL, `:timeline`, `:back`, `:forward`,
`:inspect`, `:bisect`, see [replay.md](replay.md)), `--wal FILE` (durable
write-ahead log; recover with `revl recover`, see
[crash-recovery.md](crash-recovery.md)), `--trace FILE` (causal lifecycle
JSONL for `revl why`), `--withdraw COMPONENT` (one-shot withdraw + oracle diff),
`--placement MAP` (split across processes/tiers; opens the `swap>` prompt for
live cross-tier migration, see [swap.md](swap.md)), `--plan` (print the load
plan, no runtime), `--config FILE`.

```bash
revl compile app.rvl -o out.json   # parse → check → link → IR
revl audit app.rvl                 # manifest + G8 boundary surface
revl test app.rvl                  # run in-file test blocks (--backend ts|rust|java|wasm|all)
revl fmt --migrate old.rvl         # rewrite 1.x "$name" → `${name}`
revl run app.rvl                   # boot on cordis-py; hold live with a REPL over provided services
revl run app.rvl --backend rust --once  # boot the composition as a cordis-rs process; LIFO teardown + no-residue proof; exit
revl run app.rvl --backend java --once  # same round-trip on a JVM (cordis4j runtime)
revl run app.rvl --backend wasm --once  # same round-trip on cordis-wasm (wasmtime); the substrate tier
revl run app.rvl --watch           # recompile on edit; a rejected change keeps the run alive
revl run app.rvl --plan            # print the load plan (order, config, callable keys); no runtime needed
revl plan cand.rvl --manifest running.json -o change.plan  # an executable plan artifact (docs/apply.md)
revl apply change.plan             # apply it: drift-refuse, verify each step, roll back on failure
```

```bash
revl compile app.rvl --json-diagnostics  # structured rejections for CI
revl run app.rvl --placement map.toml    # split across processes/languages
#   at the interactive swap> prompt:  swap <component> --to <backend>   (docs/swap.md)
revl mcp serve                           # the compiler as an MCP server
revl mcp schema app.rvl                  # provided services -> MCP tools
revl mcp import tools.json               # an MCP server -> revl source
python3 tools/conformance.py             # every construct x every backend
```

The first form (`revl <cmd>`) is the documented happy path: `backends/python/setup.sh` (or `uv pip install -e ".[test]"` at the root) installs the `revl` console script on `PATH`, and a script entry point is window-free by design (issue #317 / #336). Callers that need a venv's interpreter explicitly reach the same code with `python -P -m revl <cmd>` (PYTHONSAFEPATH, 3.11+) — the `-P` is the safety bit; without it, `-m` puts the CWD at `sys.path[0]`. `revl doctor` reports which shape it is under.

`run --backend py` needs the cordis-py runtime (`backends/python/setup.sh`);
without it the command says so and exits nonzero, and the end-to-end tests in
`tests/test_run.py` skip with that reason. `--config FILE` supplies the host
config a composition declares (a component with a missing required field refuses
the run before any runtime loads); `--backend` selects the tier, and `revl test
--all` is gated by `tests/test_cross_tier.py`.

The **rust tier is runnable** too: `--backend rust` boots the composition as a
separate **cordis-rs process** over the same driver contract py uses, only in a
different address space: the language-agnostic Unix-socket bridge seam the
cross-tier work already speaks ([interop-bridge.md](interop-bridge.md)). What is
wired and **gated live** by `tests/test_run_rust.py` (wherever a cordis-rs
toolchain resolves, else a skip with the reason, never a green run that booted
nothing) is the **`--once` round-trip**: emit rust → `cargo build` → boot every
component on a real `cordis::Context` → tear down LIFO (consumers before
providers) → prove no residue (`registry().len() == 0` and
`reflect().services().len() == 0`, the cordis-rs mirror of the py driver's
registry/reflect check) → exit. The **interactive REPL over provided rust
services is not yet wired** (it needs the driver to hold an RPC client against
the runner's stub for the session); without `--once` on a TTY the rust driver
says so and completes the same once round-trip rather than pretending to hold a
REPL.

The **ts, java, wasm and go tiers are runnable on the same contract**, each boots
the composition as its own process and runs the identical `--once` round-trip
(boot → LIFO teardown → no-residue proof → exit), behind its own runtime gate:

- **`--backend ts`** emits the cordis-ts module → boots it on a **node process**
  (node ≥ 23.6, cordis v4) over the same `placement_runner.ts` the cross-tier
  bridge drives, and asserts the live runtime matches its pre-load snapshot
  after teardown (`snapshotRuntime`/`assertNoResidue` from
  `backends/typescript/runtime.ts`). Gated live by `tests/test_run_ts.py`; no
  resolvable node/cordis-ts → skip with the reason.
- **`--backend java`** emits `revl.Components` → `javac` → boots the composition
  on a JVM running the once-runner (`backends/java/placement/RunOnce.java`) on
  the in-repo cordis4j runtime, and proves no residue the tier-neutral way,
  after teardown no provided service still resolves through `ctx.get`. Gated
  live by `tests/test_run_java.py`; a machine with no working JDK skips with the
  reason (macOS ships a `javac` shim that errors until a JDK is installed, so the
  gate checks that `javac`/`java` actually respond). The reactive real-cordis4j
  runtime (JDK 21 + `REVL_CORDIS4J_CLASSES`) with peer-death-as-withdrawal stays
  the domain of `--placement` and the java scenarios; a single-process `run
  --once` has no peer to withdraw, so the stub runtime carries its full contract.
- **`--backend wasm`** emits WAT → boots the composition on the **cordis-wasm**
  runtime (backed by **wasmtime**) via the once-harness
  (`backends/wasm/run_harness.py`), and proves no residue by asserting the live
  runtime holds nothing after teardown (`rt.fibers` empty and the coeffect table
  `rt.table` empty). Gated live by `tests/test_run_wasm.py`; no wasmtime/runtime
  → skip with the reason. The substrate tier is the strictest emitter (`config`,
  host builtins, non-Int component services, and more are hard `EmitError`;
  [backends/wasm/README.md](../backends/wasm/README.md)), so a composition that
  uses one is an emit failure here, not a boot.
- **`--backend go`** boots the composition on the **stc-go** placement runner
  (`backends/go/placement_runner`) in its single-process once form, the same
  seam the other non-py tiers use. Gated live by `tests/test_run_go.py`; no go
  toolchain with the pinned stc-go → skip with the reason.

On every non-py tier the interactive REPL over provided services is unwired for
the same reason it is on rust (it needs an RPC client held against the runner's
stub); without `--once` the driver notes the gap and completes the once
round-trip rather than pretending to hold a REPL.

Non-interactive round-trip (boot → trace → exit):

```bash
revl run examples/user_cache.rvl --config cfg.toml < /dev/null
```

Two of these are worth being precise about, because they are the project's
easiest place to overclaim:

- **`--placement`** is designed to compose py / node / rust / java in one
  lifecycle ([interop-bridge.md](interop-bridge.md)). What is *tested* is the
  static half: `tests/test_distribute.py` asserts the transport-safety
  verdict `revl audit` reports for each service. A four-language composition
  actually running is exercised by the `demo/bridge_py*.py` scripts, which no
  test and no CI job runs. Treat it as demonstrated, not gated. A live
  placement can also **migrate a component across tiers while it runs** via
  `swap <component> --to <backend>` at the interactive `swap>` prompt: boot the
  candidate, admit it against the running manifest, re-point the consumers, and
  drain + tear the old provider down with a no-residue proof
  ([docs/swap.md](swap.md)). The re-point itself (a planned cutover carrying
  the seam to a successor, distinct from peer-death withdrawal) is gated by
  `tests/test_swap.py`; the full multi-process cutover needs the cordis-py
  runtime.
- **`mcp serve`** holds a composition **in memory** so an agent can load,
  call, swap and prove no-residue without touching the filesystem
  ([mcp-bridge.md](mcp-bridge.md)). The live session, actually booting a
  composition through those tools, is `tests/test_mcp_session.py`, which needs
  the cordis-py runtime and therefore **skips in CI**.

<!-- docgen:mcp-test-count begin -->
The `mcp serve` tool surface, its annotations and its structured rejections
are gated by `tests/test_mcp.py` (47 tests).
<!-- docgen:mcp-test-count end -->

## Backends

- **cordis-py** (Python), the reference backend, on a hardened lifecycle
  runtime. The tier every construct is checked against first.
- **cordis** (TypeScript), v4, on node. `revl run --backend ts` boots it.
- **cordis-rs** (Rust) and **cordis4j** (Java). Each tier's emitted code is run
  against its real runtime
  (`backends/rust/test_emit_rust.py::test_runtime_scenarios_on_real_cordis_rs`,
  `backends/java/test_emit_java.py::test_runtime_scenarios_on_real_cordis4j`),
  and both skip without their toolchain. Consuming *and* serving across a
  process seam, with peer death becoming withdrawal, is the design
  ([interop-bridge.md](interop-bridge.md)) and is demonstrated by the
  `demo/bridge_*` scripts; it is not covered by a test.
- **cordis-wasm**, the sandboxed substrate, where confinement becomes
  physical. Deliberately i32-only at the service boundary.
- **stc-go** (Go), the newest tier. `revl run --backend go` boots the stc-go
  placement runner; it runs under `go test`, not yet in the conformance matrix.

All six boot live under `revl run` (see the tier notes above); each non-py tier
runs its `--once` round-trip behind a runtime gate that skips with a reason when
its toolchain is absent.

What each tier can and cannot express is measured, not asserted:
`tools/conformance.py` runs every construct through all six and
[docs/conformance.md](conformance.md) records the result, separating a
deliberate limit from a gap.

## Further reading

- [DESIGN.md](../DESIGN.md), the design, the guarantees table, the tiering rationale.
- [docs/syntax-2.0.md](syntax-2.0.md), the full-language spec.
- [docs/stdlib-2.0.md](stdlib-2.0.md), the stdlib surface.
- [docs/v2.0-roadmap.md](v2.0-roadmap.md), status and remaining frontier.
- [docs/conformance.md](conformance.md), what each backend can express.
- [docs/mcp-bridge.md](mcp-bridge.md), the agent boundary and the live session.
- [docs/interop-bridge.md](interop-bridge.md), splitting a composition across
  languages and processes.
- [docs/backend-ir-v3.md](backend-ir-v3.md), the contract, if you are writing
  a backend.

