# revl command reference

Every `revl` subcommand, its flags, and what it prints. This is the complete
index, verified against `src/revl/cli/parser.py` (the subcommand tree
`main()` dispatches over) and `src/revl/__main__.py` (the dispatch). Run
`revl --help` for the same list, or `revl <command> --help` for one command's
flags.

The verb set, in the order the parser declares it:

```text
compile  explain  doctor  scaffold  composition  audit  diff  version
contract  erase-report  plan  apply  undo  canary  query  fmt  quarantine
test
mcp  import  export  serve  run  recover  estop  why  metrics  profile  attest
dash  repair  truc
```

Conventions used below:

- `FILES` is one or more `.rvl` source paths unless noted.
- A flag is shown exactly as the parser accepts it. Where a flag takes a
  fixed set of values, the set is listed; those are the only accepted values.
- `--json` always means "machine-readable output for an agent or CI" and is
  noted per command because not every command has it.
- Exit code is `0` on success. Gates (`--check`, `--strict`, `--diff`,
  `contract check`, `--require-runtime`, a failed admission) exit nonzero;
  the per-command notes call out which.

Two module entry points sit outside this subcommand tree and are documented at
the end: `python -m revl.lsp` (the language server) and `python -m revl.otel`
(the OpenTelemetry exporter).

---

## Authoring and admission

### `revl compile`

Parse, check, link, and lower `FILES` to a backend IR document.

- `FILES` - one or more sources (required).
- `-o`, `--output PATH` - write the IR here (default: stdout).
- `--json-diagnostics` - on rejection, print a structured diagnostic (code,
  guarantee, expected/actual, `fix` hint) instead of the human rendering.

The IR document it writes is the input other commands read as a "compiled
composition": `revl diff`, `revl version --against`, `revl profile`,
`revl attest`, `revl plan --manifest`, and `contract check --provider`.

```bash
revl compile app.rvl -o app.ir.json
revl compile app.rvl --json-diagnostics    # CI: parse the rejection
```

### `revl explain`

What a diagnostic code guarantees and how to satisfy it. No sources; it reads
the built-in guarantee/fix table (`src/revl/diagnostics.py`).

- `CODE` - a diagnostic code, e.g. `G4` (case-insensitive), required.
- `--json` - machine-readable output.

```bash
revl explain G4
revl explain t3 --json
```

### `revl doctor`

Diagnose each backend tier, runtime, and dependency, then smoke-test every
available tier. Each row reports `OK` / `WARN` / `MISSING` with a version and a
one-line reason; the footer counts them. Landed as roadmap item 291.

- `--json` - machine-readable report (for an agent) instead of the table.
- `--no-smoke` - skip the per-tier compile+boot smoke test (report only).
- `--smoke-timeout SECONDS` - per-tier smoke-test timeout (default: 90).

```text
$ revl doctor --no-smoke
revl doctor - compiler 2.0.0

  [OK     ] compiler (revl)                   2.0.0  the running compiler
  [OK     ] python backend                    3.14.3  ...
  [MISSING] java backend (JDK)                 no working JDK found ...
  [WARN   ] cordis-py runtime                  'cordis' not importable ...

  8 OK, 3 WARN, 1 MISSING
```

A `MISSING` core tier is what makes another command's "runtime unavailable"
skip make sense; run `doctor` first when a tier behaves unexpectedly.

### `revl scaffold`

Generate a typed, holed composition skeleton from a spec, so an agent fills
holes rather than writing a whole component from a blank file (roadmap item
288, [scaffold.md](scaffold.md)). The skeleton compiles as a draft; admission
refuses it until every `hole[T]` is filled ([holes.md](holes.md)).

- `--service NAME` - the service the component provides (required).
- `--provides KEY` - the provision key (default: the service, lowercased).
- `--component NAME` - the component name (default: `<Service>Provider`).
- `--requires KEY[:Service]` - an injected dependency; repeatable. A bare
  `KEY` defaults its service to `KEY` capitalized.
- `--capabilities CAP` - a boundary the component may emit through;
  repeatable. Only a capability whose boundary is injected (via `--requires`)
  becomes an emission bound; an un-injected one stays a hole, never a silently
  widened permission.
- `--method 'name(p: T) -> R'` - a pure service method; repeatable.
- `--emits 'name(p: T) -> R'` - an emission service method, bound to the wired
  capabilities; repeatable.
- `--config name:Type` - a component config field; repeatable.
- `--resource Type` - the type of the effect-acquired resource (default:
  `<Service>Resource`).
- `--no-effect` - omit the acquire/undo effect block.
- `-o`, `--out PATH` - write the `.rvl` skeleton here (default: stdout).
- `--json` - print the skeleton, its obligations, and each hole's fill spec
  as one JSON document.

```bash
revl scaffold --service Cache \
  --requires db:Db --capabilities db \
  --method 'get(k: Str) -> Str' \
  --emits 'put(k: Str, v: Str) -> Unit'
```

Passing `--emits` for a capability whose boundary is not injected is refused
with a specific error rather than binding the emission to "any boundary"; wire
the boundary with a matching `--requires`/`--capabilities` pair, or drop
`--emits` so the method scaffolds pure.

---

## Composition analysis

### `revl composition`

Resolve a composition document's ROW TABLE: the label, claims, component,
config and requires of every row ([composition-rows.md](composition-rows.md)).
Header-only by default, so every row id resolves and the whole wiring renders
without lowering a single component body.

- `FILE` - the `.rvl` document declaring the composition (required).
- `--json` - the row table as JSON instead of the ROWS/WIRING panels.
- `--admit` - also COMPILE the rows the table names and print the resulting
  load order. Resolution alone compiles nothing.
- `--root DIR` - the project root row provenance and origins are recorded
  against (default: the working directory). A document under `trucs/<key>/`
  is scoped to the origin `<key>`; anything else to the project origin `.`.

Exits nonzero on a refusal: an unresolvable row, an assertion the component
header contradicts, two rows claiming one `(key, realm)` pair, a config field
the component does not declare or a value that does not fit its type, or a
`requires` outside the row's `granted` set.

```bash
revl composition base.rvl --admit
```

### `revl audit`

The composition manifest plus the G8 boundary surface: which emissions each
component can perform and the capabilities each crosses.

Also prints the **retention surface** (roadmap item 308 F10) when the
composition has one: every declared position at which a resource handle leaves
revl's sight — a resource-carrying parameter of a non-inverse extern, or of a
service method whose implementation may live host-side. This is report-only and
refuses nothing: the ownership checks refuse a borrow that escapes through a
revl position, but a host body that keeps the handle it was handed escapes
through a surface the declaration does not describe, so the frontier is listed
for review instead. A row is a may-retain, and an absent row is not a proof of
non-retention. Declared inverses are excluded: teardown closing a handle is the
contract working, not a hazard.

- `FILES` (required).
- `--json` - machine-readable output.
- `--diff PREV.json` - authority-drift gate: re-audit and FAIL (nonzero) if
  the new generation ADDS boundary crossings not in `PREV.json`
  ([audit-diff.md](audit-diff.md)).
- `--accept CROSSING` - acknowledge one added crossing so it no longer fails
  `--diff` (the token printed after `+`); repeatable.
- `--accept-all` - acknowledge every added crossing under `--diff`.
- `--policy POLICY` - boundary-policy gate (roadmap item 33): evaluate a
  policy file over the audit graph and REFUSE admission (nonzero) if any
  component reaches a capability it may not ([boundary-policy.md](boundary-policy.md)).
- `--mcp-scope COMPONENT` - treat `COMPONENT` as MCP/agent-admitted so the
  policy's `mcp` sandbox allow-list applies to it; repeatable, `*` = every
  component.

### `revl diff`

Semantic composition diff: the IR-level structural delta between two
compositions (components added/removed/changed, emissions gained/lost,
provide/require edges added/broken). The PR-review tool for agent-generated
compositions (roadmap item 123, [revl-diff.md](revl-diff.md)).

- `BEFORE` - the earlier composition: a compiled IR/interchange JSON
  (`revl compile -o` or `revl audit --json`) or a `.rvl` source (required).
- `AFTER` - the later composition, same accepted forms (required).
- `--json` - machine-readable delta.

```bash
revl diff before.ir.json after.rvl
```

### `revl version`

Derive the required semver bump from the interface diff against a previous
composition ([derived-versioning.md](derived-versioning.md)). The bump is a
measurement of the change, not a policy choice.

- `FILES` (required).
- `--against PREV.json` - a previous compiled composition document to diff
  against (produce one with `revl compile <sources> -o prev.json` or
  `--emit-manifest`).
- `--current-version X.Y.Z` - the previous composition's declared version;
  when given, the computed next version is printed too.
- `--emit-manifest` - print the compiled composition document (the diff input
  a later `--against` reads) and exit, instead of deriving a bump.
- `--json` - machine-readable derivation.

### `revl contract`

Federated contracts between sovereign compositions ([federation.md](federation.md)).
Two subcommands:

`revl contract export FILES`

- `FILES` (required).
- `--consumer LABEL` - a name for the consumer, echoed into the artifact and
  its verdicts (default: none).

Projects composition A's compiled IR into its consumer surface: the pinnable
contract of everything A requires from a provider.

`revl contract check`

- `--consumer A-pinned.json` - the consumer surface a provider must satisfy
  (produce it with `revl contract export <A-sources>`), required.
- `--provider B ...` - the provider's current composition: its `.rvl` sources
  (compiled here), or a single compiled manifest `.json`; required, one or
  more.
- `--json` - machine-readable verdict.

`check` FAILs (nonzero) on a §5 drift that breaks the pinned surface.

### `revl query`

Ask the composition a question ([queries.md](queries.md)). Static verbs read
source; the two historical verbs read a recorded run. Every verb takes
`--json`.

Static (over source; each takes `TARGET FILES`):

- `emits-to TARGET FILES` - who emits to a service key, `key.method`, service
  or extern?
- `withdraw COMPONENT FILES` - what breaks if this component is withdrawn (the
  reactive cascade)?
- `depends-on TARGET FILES` - who depends on a provision key or service?
- `reaches COMPONENT FILES` - the transitive boundary surface of one component.
- `drift SERVICE FILES` - which providers and call sites a service interface
  change implicates. Adds `--gains METHOD` and `--loses METHOD` (each
  repeatable) to model a method the service would gain or lose.

Historical (over a recorded run):

- `emitted-between --timeline FILE --from X --to Y [--component C]` - which
  emissions crossed between steps X and Y of a replay recording JSON (a
  `revl_timeline` dump). `--from`/`--to` are inclusive step indices.
- `touched COMPONENT [--trace FILE] [--timeline FILE]` - everything a
  component touched during its life: `--trace` is a lifecycle JSONL
  (`revl run --trace`), `--timeline` a replay recording for the
  effects/emissions.

Live mode is session-bound and has no one-shot CLI entry; use the MCP
`revl_live_query` verb instead.

---

## Deployment and lifecycle

### `revl plan`

Dry run for admission: the delta a swap would produce, without applying it
([plan.md](plan.md)).

- `FILES` (required).
- `--manifest RUNNING.json` - compiled IR document of the RUNNING composition
  (as written by `revl compile -o`); omit for a cold start.
- `--replacing NAME` - a running component withdrawn in this admission
  (renames); repeatable.
- `-o`, `--output change.plan` - serialize an EXECUTABLE plan artifact
  (basis for drift, ordered ops, resulting IR); apply it with `revl apply`
  ([apply.md](apply.md)).
- `--json` - machine-readable output.

### `revl apply`

Execute a `revl plan -o` artifact against a live composition: drift-refuse,
verify each step, roll back on failure ([apply.md](apply.md)).

- `plan` - a plan artifact written by `revl plan -o` (required).
- `--against RUNNING.json` - boot this composition as the live pre-state
  instead of the plan's own; drift is refused if it differs from the plan's
  basis.
- `--json` - machine-readable output.

### `revl undo`

Operator undo: replay a generation history and return to an earlier generation
THROUGH THE GATE ([generation-history.md](generation-history.md)). The target's
sources are re-admitted, so a now-rejected target is refused, not forced.

- `history` - a `revl.generation-history` document (the session's history
  export), required.
- `--to GEN` - a recorded generation number to return to; omit to undo to the
  immediately previous generation (N−1).
- `--json` - machine-readable output.

### `revl canary`

Progressive delivery for one slice: run a candidate on a designated realm,
compare recorded worlds (replay), and prove the revert clean so the other
tenants are untouched ([verified-canary.md](verified-canary.md)).

- `FILES` - the running (baseline) composition's `.rvl` files (required).
- `--candidate FILE` - the successor generation of the slice's provider;
  repeatable, required.
- `--slice REALM` - the designated slice, a named realm (a tenant, a sandbox);
  required.
- `--provider COMPONENT` - the slice's provider to canary (only needed when
  the realm serves several).
- `--promote-to BACKEND` - report a promote (swap the remainder) admission
  verdict for this tier.
- `--json` - machine-readable, versioned report document.
- `--no-residue-proof` - skip the runtime teardown proof (static survivors
  proof only; use where cordis is unavailable).

### `revl repair`

The repair loop (roadmap item 62): a faulting component fixes itself within
policy - regenerate/reuse → gauntlet → policy → widening-ack → hot-swap,
unattended, with an incident dossier ([repair-loop.md](repair-loop.md)).

- `FILES` - the running composition to repair (required).
- `--component NAME` - the faulting component to repair (required).
- `--trace FILE` - a JSONL causal trace (`revl run --trace`): the fault's why.
- `--candidate FILE` - the regenerated repair source(s), a whole composition
  to swap in; repeatable.
- `--self-repair-policy FILE` - which components may self-repair and which
  capabilities a repair may touch; absent = closed (nothing self-repairs).
- `--boundary-policy FILE` - an item-33 boundary policy for the reach gate.
- `--predicate EXPR` - a bisect predicate to slice the fault to a step.
- `--accept CROSSING` - acknowledge a widening crossing (item-21 ack token);
  repeatable.
- `--plan` - run every gate but do not swap (a rehearsal).
- `--no-record` - load without recording (disables the timeline slice; the
  loop still runs).
- `--json` - print the incident dossier as JSON.

### `revl quarantine`

Prove an untrusted Str-surface candidate in the wasm sandbox (roadmap item 45):
grade it with the gauntlet, then run its lifecycle + fault battery as a
standard component under wasmtime, where an escape is a trap, not an incident
([quarantine-tier.md](quarantine-tier.md)).

- `FILES` (required).
- `--json` - machine-readable report.
- `--service NAME` - WIT interface name to group the candidate's Str-surface
  functions under (default: the sole declared service, else `Candidate`).
- `--policy POLICY` - a boundary policy (item 33): with `quarantine required`,
  the admission decision reports whether the candidate is admissible.
- `--require-runtime` - fail (exit 3) instead of exiting 0 when
  wasm-tools/wasmtime are absent, so the substrate battery could not actually
  run.

---

## Running and recovery

### `revl run`

Boot a composition on a Cordis runtime and stream the lifecycle/host trace.
Holds and opens a REPL by default; `--watch`, `--once`, or `--plan` change that.

- `FILES` (required).
- `--backend {py, ts, rust, java, wasm, go}` - target runtime tier (default:
  `py`). All six boot live: `py` in-process, the rest each as a separate
  process over the bridge seam. A missing runtime is a skip with a reason and
  a nonzero exit.
- `--config FILE` - TOML/JSON file of `component-name = { ... }` config tables.
- `--env FILE` - TOML/JSON file of flat `name = value` environment values,
  injected into the composition's `boot` component. Its `config {}` block is the
  ENVIRONMENT CONTRACT (item 350): a `--config` table naming the boot component,
  an `--env` key the contract does not declare, a missing required field, or a
  value outside a declared `under "<prefix>"` / `in [...]` bound each refuse the
  boot before any runtime is imported. See
  [environment-binding.md](environment-binding.md).
- `--watch` - watch the sources and recompile on change; a rejected edit is
  refused and the run keeps going.
- `--record` - record the effect accumulator so the REPL can step backwards
  (`:timeline`, `:back k`); see [replay.md](replay.md).
- `--wal FILE` - persist the effect accumulator as a durable write-ahead log
  (implies `--record`). On restart, `revl recover --wal FILE` rolls forward or
  back with a checked verdict ([crash-recovery.md](crash-recovery.md)).
- `--estop-latch FILE` - watch FILE for an operator E-Stop, so `revl estop
  --latch FILE` from another terminal halts this run immediately
  ([443-estop.md](design/443-estop.md)). Unarmed by default; an unarmed run
  checks nothing.
- `--trace FILE` - write a causal lifecycle trace (JSONL); every transition
  carries the cause chain, queryable with `revl why` ([why-runtime.md](why-runtime.md)).
- `--withdraw COMPONENT` - one-shot: boot, withdraw this live component while
  recording the causal cascade, diff the actual cascade against the static
  `withdraw` prediction (the runtime oracle), then tear down.
- `--plan` - print the load plan (order, config, callable keys) and exit,
  without a runtime.
- `--placement FILE` - TOML/JSON placement map: split components across
  processes and wire the seams. Opens a `swap>` prompt whose
  `swap <component> --to <backend>` migrates a live component across tiers
  ([swap.md](swap.md)).
- `--once` - bring the composition up, then tear down LIFO and exit (with
  `--placement`, run probes across processes first; with a non-py backend,
  boot the tier's process, prove no residue, exit).

Under `--placement` the conductor waits for each child's own `[<name>] DOWN`
line - the runner's statement that its LIFO unwind covered every registered
entry and its no-residue proof printed - rather than for a fixed number of
seconds. A wedged child is still SIGKILLed after a backstop, so the conductor
cannot hang, but that kill is **reported and the run exits non-zero**: a child
killed mid-unwind leaves its entries STRANDED and its residue UNKNOWN, the same
verdict [`revl estop`](design/443-estop.md) gives a halted session, and it is
never a clean exit. Reconcile a durable run with `revl recover --wal FILE`. Set
`REVL_TEARDOWN_GRACE=<seconds>` (default 30) when a teardown is legitimately
long rather than wedged.

`run --record` opens the replay REPL (`:timeline`, `:back`, `:forward`,
`:inspect`, `:bisect`; see [replay.md](replay.md)).

### `revl recover`

Crash recovery: read a `revl run --wal` write-ahead log and roll forward
(resume the persisted generation) or roll back (run the boundary inverses
LIFO), ending in a checked verdict + residue proof
([crash-recovery.md](crash-recovery.md)).

- `--wal FILE` - a write-ahead log written by `revl run --wal` (required).
- `--restore SNAPSHOT.json` - on roll-forward, the item-15 snapshot to
  re-admit so recovery resumes the persisted generation.
- `--json` - machine-readable output.

### `revl estop`

The operator's **emergency halt** ([443-estop.md](design/443-estop.md)). Arms a
latch that a running composition watches, so it stops dispatching NEW boundary
crossings immediately and reports what was in flight, instead of performing the
graceful two-phase LIFO unwind every other stop performs.

This is not `revl_abort` with a shorter name. Abort is a verdict on the work
and pays for a full unwind; estop pays for a latch flip. The price is stated,
not hidden: **nothing is unwound.** No inverse runs, no compensation runs,
nothing is discharged; every registered entry is left stranded (still owed) and
every acquired handle stays held. The instance is dead afterwards — there is no
resume, and the way back is `revl recover --wal FILE`.

- `--latch FILE` - the latch file the running process watches (`revl run
  --estop-latch FILE`, or the ambient `REVL_ESTOP_LATCH`). Required unless
  `--wal` is given.
- `--wal FILE` - the running session's write-ahead log. Derives the latch as
  `FILE.estop` when `--latch` is omitted, and names the log the outstanding
  inventory is read from.
- `--reason TEXT` - why the button was hit; carried into the halt record and
  every residue record it produces.
- `--operator TOKEN` - the operator accountable for the halt. An E-Stop is an
  operator authority (item 55's `estop` verb), never something a composition or
  an agent may invoke on itself.
- `--report` - read the latch back and print what was halted, without arming
  anything or touching the world.
- `--clear` - remove the latch so a *fresh* process may boot. Not a resume: the
  halted instance stays dead and its stranded entries stay owed.
- `--json` - machine-readable output.

Exit status follows the residue, as `revl recover` does: 0 when nothing is
outstanding, 1 when a halt is engaged and entries are owed. An E-Stop is never
clean.
### `revl branch`

Session branch lineage over durable write-ahead logs (roadmap item 250): what a
WAL is, the branch tree across several WALs, and the fork partition of a
recorded tail ([design/250-session-branching.md](design/250-session-branching.md)).

Reads through the tier-agnostic WAL core, so a branch written by any tier's
runtime reads the same. It runs nothing and rewinds nothing - an offline reader
has no workspace to rewind. Exit status follows the residue: `0` when the branch
stands on a clean fork point, `1` when honest residue (a crossed emission, an
unfired crossing inverse) or an unclosed lineage edge remains.

- `--wal FILE` - a write-ahead log (required). One WAL prints its lineage:
  `standalone`, `forked-parent` (frozen at its fork point), `branch`, or
  `forked-branch`, with the provenance a branch inherited and, listed
  explicitly, the provenance it did not. Repeat `--wal` to reconstruct the
  branch tree; an edge whose other end was not supplied is reported as an
  orphan or a dangling child rather than dropped.
- `--at SEQ` - instead of the lineage, enumerate the fork partition of the tail
  above this WAL position: what a fork there would put back, what already
  crossed and cannot be undone, what would be enumerated and never fired, and
  every reason the fork would be refused. `-1` is the whole recorded tail.
  Requires a single `--wal`.
- `--json` - machine-readable output.

### `revl compare`

Compare two recorded session histories that share a fork point (roadmap item
250): what each did after diverging, and what a comparison of durable logs
cannot yet say.

Two WALs with no recorded lineage relation are not compared against an invented
common point - the comparison says so and exits nonzero.

- `LEFT.wal` / `RIGHT.wal` - the two write-ahead logs (required).
- `--json` - machine-readable output.

### `revl why`

Explain a recorded lifecycle transition: the cause chain for a component in a
`revl run --trace` JSONL trace ([why-runtime.md](why-runtime.md)).

- `component` - the component whose transition to explain (required).
- `--trace FILE` - a JSONL causal trace written by `revl run --trace`
  (required).
- `--check FILE ...` - also run the oracle: compile these source files and
  diff the static `withdraw` prediction against the recorded cascade; a
  mismatch is a defect (nonzero exit).
- `--json` - machine-readable output.

---

## Observability and evidence

### `revl metrics`

Capability-aware runtime metrics over a `revl run --trace` JSONL trace
(roadmap item 122): emission count by capability, failure count by G-rule, and
average lifecycle duration ([revl-metrics.md](revl-metrics.md)).

- `trace FILE` - a JSONL causal trace written by `revl run --trace` (required).
- `--json` - machine-readable metrics document instead of the human table.

### `revl profile`

Capability/emission profiling (roadmap item 124): diff a component's DECLARED
emission surface against what a `revl run --trace` JSONL trace actually
emitted, flagging over-declaration ([revl-profile.md](revl-profile.md)).

- `composition` - the composition whose declarations to read: a `.rvl` source,
  a compiled IR (`revl compile -o`), or an `audit --json` document (required).
- `trace FILE` - a JSONL causal trace written by `revl run --trace` (required).
- `--json` - machine-readable profile document.
- `--strict` - least-privilege gate: exit nonzero if any component
  over-declares an emission the run never exercised.

### `revl attest`

Cryptographic attestation of a verified composition (roadmap item 127): sign a
portable record that this exact composition was admitted (canonical IR hash +
verdict + guarantees + timestamp), or `--verify` one ([revl-attest.md](revl-attest.md)).

- `target` - what to attest: a composition (`.rvl` source, compiled IR, or
  `audit --json`). With `--verify`, the attestation JSON to check instead
  (required).
- `--verify` - verify mode: `target` is an attestation JSON; check its
  signature (and, with `--against`, that the composition still matches). Exits
  nonzero if the attestation is invalid.
- `--against COMPOSITION` - with `--verify`: the composition to re-hash and
  check the attestation against. Omit to check only the signature over the
  embedded hash.
- `--key PATH` - the signing/verifying key file. Falls back to the
  `REVL_ATTEST_KEY_FILE` (a path) or `REVL_ATTEST_KEY` (the secret) environment
  variables. Never hardcoded.
- `--signer NAME` - an optional signer label recorded in (and signed into) the
  attestation; falls back to the `REVL_ATTEST_SIGNER` env var.
- `--json` - machine-readable output.

### `revl erase-report`

Right-to-erasure evidence for one realm: in-process state gone (no-residue
proof), boundary crossings compensated-vs-bare, and other realms provably
untouched ([erase-report.md](erase-report.md)).

- `FILES` (required).
- `--realm R` - the realm to report erasure evidence for (required).
- `--json` - machine-readable, versioned report document.
- `--no-residue-proof` - skip the runtime teardown proof (static sections
  only; use where the cordis runtime is unavailable).

### `revl dash`

The supervisor's cockpit (roadmap item 63): a READ-ONLY live view over a
session or a recorded run - the dependency graph (realms, seams), the causal
trace streaming, and the pending-decisions queue with evidence attached
([dash.md](dash.md)).

- `FILES` - the composition whose graph to show (required).
- `--trace FILE` - a lifecycle JSONL (`revl run --trace`): streams the causal
  pane with no live runtime.
- `--timeline FILE` - a replay recording JSON (a `revl_timeline` dump) for the
  effect/emission detail behind the lifecycle.
- `--live-state FILE` - a live-state snapshot JSON (`{generation, servedKeys,
  componentStates}`) that colors the graph as it stands now.
- `--against PREV.json` - a previous `audit --json` document; the boundary
  additions since it become the widening queue.
- `--accept CROSSING` - mark one added crossing as already acknowledged;
  repeatable.
- `--accept-all` - mark every added crossing as acknowledged.
- `--policy POLICY` - a boundary policy file (item 33); its violations over the
  current audit are the policy-exception queue, each with its why-trace.
- `--mcp-scope COMPONENT` - treat `COMPONENT` as MCP/agent-admitted for the
  policy's `mcp` sandbox; repeatable, `*` = every component.
- `--watch` - periodic-refresh loop: re-read the sources and reprint on an
  interval (read-only; Ctrl-C to stop).
- `--interval SECONDS` - refresh interval for `--watch` (default: 2.0).
- `--no-color` - plain output with no ANSI color.
- `--json` - print the structured model instead of the text view.

---

## Formatting and testing

### `revl fmt`

Canonically format `.rvl` sources, gated on IR equivalence (the reformat must
lower to the same IR).

- `FILES` (required).
- `--migrate` - rewrite 1.x `$` interpolation to backtick templates instead of
  formatting.
- `--check` - do not write; exit nonzero if any file is not already canonical
  (the CI gate).
- `-o`, `--output PATH` - write the result to this path instead of in place
  (single input).

### `revl test`

Compile and run in-file `test` blocks (and `prop test` / `fault test` /
`lifecycle test`).

- `FILES` (required).
- `--backend {py, ts, rust, java, wasm, go, all}` - tier to run the blocks on
  (default: `py`); `all` runs every tier whose toolchain is present.
- `--sweep` - fault sweep: inject failure at every step of every component and
  check L-Raise / no-residue / LIFO / siblings at each (py tier). With
  `--backend all`, sweep every runtime whose toolchain is present and assert
  they agree — residue-free on every tier; a toolchain-absent or
  not-yet-capable tier loud-skips, never a false green. Set `REVL_SWEEP_CAP=N`
  to take a representative corpus on the heavy tiers (§10 of
  [fault-tests.md](fault-tests.md)).
- `--mock-requires` - run every `lifecycle test` in mock world: each unmet
  `requires` is filled by an auto-generated mock provider, so a consumer boots
  with zero real providers (py tier; [auto-mocks.md](auto-mocks.md)).

---

## Interop: MCP, import, export, serve

### `revl mcp`

The MCP bridge ([mcp-bridge.md](mcp-bridge.md)). Four subcommands:

`revl mcp serve` - run the compiler itself as an MCP server over stdio. This is
the server whose verbs are documented in [mcp-reference.md](mcp-reference.md).

- `--files [FILE ...]` - optional default composition for tools called without
  one.
- `--restore SNAPSHOT.json` - re-admit a `revl_snapshot` document into the
  session before serving (self-evolution across a restart;
  [persistence.md](persistence.md)).
- `--operator-profile PROFILE` - bound the management verbs this session may
  call (swap/unload/restore/undo/edit/load/snapshot) to an operator's declared
  grants (item 55); a DSL or JSON file. Omit for ungated (root over transport)
  ([operator-capabilities.md](operator-capabilities.md)).
- `--operator TOKEN` - which operator in the profile this session runs as (its
  session token); optional when the profile declares exactly one operator.
- `--policy POLICY` - a boundary-policy file (item 33) bound to this session:
  its `mcp` sandbox bounds admitted agent code, and `leases enforced` refuses a
  swap that would replace a component another operator leases (item 61). Omit
  for advisory-only leases.

`revl mcp schema FILES` - project provided services to MCP tool definitions
(the `revl -> MCP` direction, annotations derived from the checker).

- `FILES` (required).
- `--composition PREFIX` - tool-name prefix (default: `revl`).

`revl mcp import MANIFEST` - turn an MCP `tools/list` manifest into revl source
(the `MCP -> revl` direction; everything the manifest does not assert read-only
becomes an `emission`).

- `manifest` - JSON file: a `tools/list` result (or `{"tools": [...]}`),
  required.
- `--service NAME` - generated service name (default: `Imported`).
- `--key KEY` - provision key (default: `imported`).
- `--backend {ts, py}` - host block backend for the generated externs
  (default: `ts`).
- `-o`, `--output PATH` - output path (default: stdout).

### `revl import`

Import an external interface definition as typed revl source. Three
subcommands.

`revl import wit FILE` - a WIT world/interface ([import-wit.md](import-wit.md)).

- `file` - a `.wit` file (required).
- `--backend {wasm, ts, py, rust}` - host block backend for the generated
  extern stubs (default: `wasm`).
- `--pure NAME` - assert that `<interface>.<func>` (or `<func>`) is reversible,
  so it is emitted as a plain `fn` instead of `emission`; repeatable. WIT makes
  no such claim; this is your assertion, recorded in the output.
- `-o`, `--output PATH` - output path (default: stdout).
- `--json-diagnostics` - on rejection, print a structured diagnostic instead of
  the human rendering.

`revl import openapi FILE` - an OpenAPI 3.x document ([import-openapi.md](import-openapi.md)).

- `file` - a `.json` (or `.yaml`, if PyYAML is importable) OpenAPI 3.x document
  (required).
- `--backend {ts, py, rust}` - host block backend (default: `ts`).
- `--service NAME` - generated service name (default: from `info.title`).
- `--pure OP` - assert that an operation whose HTTP verb is not safe (a
  `POST /search`) changes nothing, so it emits as a plain `fn`; name it by
  generated name, `operationId`, or `"POST /search"`; repeatable.
- `--emission OP` - assert that a safe-by-spec operation (a `GET` that writes)
  is irreversible after all, overriding the verb; named the same way;
  repeatable.
- `-o`, `--output PATH` - output path (default: stdout).
- `--json-diagnostics` - structured diagnostic on rejection.

`revl import cordis FILE` - a Cordis (TS) plugin's inject/provide surface
([import-cordis.md](import-cordis.md)).

- `file` - a Cordis plugin `.ts` (or `.js`) file (required).
- `--backend {ts, py, rust}` - host block backend (default: `ts`).
- `--service NAME` - generated service name (default: from the provided service
  key).
- `--pure OP` - assert that a method changes nothing, so it emits as a plain
  `fn`; name it `<Service>.<method>` or `<method>`; repeatable.
- `--mark-unrecovered` - instead of refusing an operation whose signature
  cannot be recovered, emit a loud `// UNRECOVERED` marker so a partial surface
  still compiles (nothing is ever guessed).
- `-o`, `--output PATH` - output path (default: stdout).
- `--json-diagnostics` - structured diagnostic on rejection.

`revl import a2a FILE` - an A2A 1.0.0 Agent Card's skill surface
([import-a2a.md](import-a2a.md), roadmap item 439 slice 1).

- `file` - an A2A 1.0.0 Agent Card `.json` (required).
- `--backend {ts, py}` - host block backend (default: `ts`). Unlike its
  siblings the bodies are real: a JSON-RPC 2.0 `message/send` crossing.
- `--service NAME` - generated service name (default: from the card's `name`).
- `--allow-plaintext` - import a card whose `url` is plaintext `http`; refused
  without it, and the generated header records that the flag was used.
- `-o`, `--output PATH` - output path (default: stdout).
- `--json-diagnostics` - structured diagnostic on rejection.

Every result is `Untrusted[Str]`, the reach is derived from the endpoint's host
alone, and no inverse is ever synthesized: a remote call cannot participate in
G7 teardown. See [import-a2a.md](import-a2a.md) §2.

### `revl export`

The reverse of `revl import`. One subcommand today.

`revl export wit FILES` - generate the standard WIT interface for a revl
service or composition ([wit-bridge.md](wit-bridge.md)).

- `FILES` - `.rvl` source files (required).
- Exactly one of (required, mutually exclusive):
  - `--service NAME` - export a single service by name.
  - `--composition` - export every service the composition provides.
- `--package NS:NAME` - WIT package id for the generated file (default:
  `revl:exported`).
- `-o`, `--output PATH` - output path (default: stdout).
- `--json-diagnostics` - structured diagnostic on rejection.

### `revl serve`

Serve a composition's OWN provided operations as MCP tools (the fourth
quadrant: hints derived by the compiler). Distinct from `revl mcp serve`, which
serves the compiler itself.

- `FILES` (required).
- `--mcp` - serve over the MCP stdio protocol (required).
- `--config FILE` - TOML/JSON file of `component-name = { ... }` config tables,
  supplied to each component at boot.
- `--env FILE` - TOML/JSON file of flat `name = value` environment values,
  injected into the composition's `boot` component. Its `config {}` block is the
  ENVIRONMENT CONTRACT (item 350): a `--config` table naming the boot component,
  an `--env` key the contract does not declare, a missing required field, or a
  value outside a declared `under "<prefix>"` / `in [...]` bound each refuse the
  boot before any runtime is imported. See
  [environment-binding.md](environment-binding.md).
- `--composition PREFIX` - tool-name prefix (tools are `<prefix>.<key>.<op>`;
  default: `revl`).

---

## The component manager: `revl truc`

`revl truc <verb> ...` is the namespaced form of the standalone `truc <verb>`
(roadmap item 136, [truc.md](truc.md)). It is a pure passthrough: the tail
after `truc` is handed verbatim to truc's own launcher (`revl.truc:main`, the
same entry point the `truc` console script calls), so `revl truc add X` ==
`truc add X`. `argparse.REMAINDER` captures the tail untouched, flags included.

truc's dispatch, help text, and refusal logic for its state-changing verbs live
in its `.rvl` components (`src/revl/truc/components/cli.rvl`), not in Python. The
verbs it accepts today:

- `truc add <name>` - add a dependency to the workspace.
- `truc rm <name>` - remove a dependency.
- `truc assemble` - assemble the workspace into a build (`--check` is the
  dry-run that writes nothing).
- `truc ship <target>` - publish the assembled composition to a registry. A free
  name is claimed first-come; a name already published is republished as a NEW
  RELEASE, declared in `[ship] version` (roadmap item 49 phase 2,
  [registry.md §1.2](registry.md#12-releases--the-update-flow)). A published
  release is immutable, an unversioned entry cannot be replaced, and the
  declared version must satisfy the bump `revl version` computes from the
  interface diff against the release it replaces - an under-bump is refused by
  name. A version the bump check cannot read (a date, a build id) refuses unless
  `[ship] version_scheme = "opaque"`, which publishes with the check recorded as
  `cannot verify`. `[ship] publisher` must stay the same across releases of a
  name (continuity of a self-asserted label, not authentication). Each release
  freezes its bytes, manifest, record and derived changelog (item 261) under
  `components/<name>/releases/<version>/`.
- `truc reproduce <component@version>` - deterministic package reproduction
  (roadmap item 297, [truc.md](truc.md#truc-reproduce)). Rebuilds a published
  component and verifies it is bit-for-bit what was published, comparing
  recomputed hashes tier by tier: version, source, independent pin, dependency
  lock, IR, policy surface, backend version, attestation, and emitted artifact. Each
  tier reports OK, MISMATCH, or "cannot verify" (nothing was recorded for it -
  honest degradation, not a pass). `reproduce` is a verifier and changes no truc
  state, so the launcher intercepts it before the component dispatch rather than
  routing it through `cli.rvl`.
  - `component` - the component to reproduce, `name` or `name@version`
    (required; omitting it exits 2). `@version` is a pin: it is checked against
    the version the registry records for the entry (its `version` file, carried
    into the index row). A version the registry does not record refuses the
    resolution; a registry that records none at all makes it a MISMATCH on the
    `version` tier, never a silent reproduction of whatever `name` is today.
    Asking for no version leaves the `version` tier "cannot verify", so an
    unpinned run is at best *partially* reproduced.
  - `--registry PATH` - reproduce against this registry directory instead of the
    one declared in `truc.toml`.
  - `--json` - print the tier-by-tier report as JSON.
  - Exit codes: `0` reproduced (no tier diverged), `1` on any MISMATCH, `2` on a
    usage or resolution error.

`revl truc reproduce <component@version>` is the namespaced spelling of the same
verb.

Because truc boots itself through the gate on every invocation, it needs the
cordis-py runtime installed (`sh backends/python/setup.sh`); without it, any
`revl truc <verb>` reports the runtime is missing rather than running.

---

## Entry points outside the subcommand tree

Two Python module entry points are not `revl` subcommands.

`python -m revl.lsp` runs the human-facing language server over stdio: it
pushes `textDocument/publishDiagnostics` from the checker, answers
`textDocument/hover` from the diagnostic explanations and symbol info, and
answers `textDocument/definition` from the resolver, reusing the compiler
surfaces read-only (`src/revl/lsp/`).

`python -m revl.otel run.jsonl` exports a `revl run --trace` lifecycle trace to
OpenTelemetry spans, events, and links (a transition is a span, its cause an
event, a causal edge a link), so a composition's causality shows up in Grafana,
Datadog, Honeycomb, or Jaeger. The OTel SDK is the optional `revl[otel]` extra;
`--json` prints the span model without it ([opentelemetry.md](opentelemetry.md)).

---

## See also

- [mcp-reference.md](mcp-reference.md) - every verb the `revl mcp serve` server
  exposes.
- [guide-humans.md](guide-humans.md) - the language and toolchain for people.
- [guide-ai-agents.md](guide-ai-agents.md) - the same, oriented for an agent
  driving the compiler over MCP.
