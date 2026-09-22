# revl command reference

Every `revl` subcommand, its flags, and what it prints. This is the complete
index, verified against `src/revl/cli/parser.py` (the subcommand tree
`main()` dispatches over) and `src/revl/__main__.py` (the dispatch). Run
`revl --help` for the same list, or `revl <command> --help` for one command's
flags.

The verb set, in the order the parser declares it:

<!-- docgen:cli-verbs begin -->
```text
compile  explain  grammar  adapt  doctor  scaffold  composition  layer
audit  goal  policy  simulate  diff  changelog  version  contract
erase-report  retention-receipt  plan  apply  undo  canary  query  fmt
quarantine  analyze  test  mcp  import  export  sourcemap  serve  run
dev  recover  estop  slo  branch  compare  replay  why  metrics  trace
profile  pool  attest  dash  repair  bundle  emit  verify  deploy
deploy-admit  truc
```
<!-- docgen:cli-verbs end -->

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
the end: `python -P -m revl.lsp` (the language server) and `python -P -m
revl.otel` (the OpenTelemetry exporter). Both are reached by running a module
directly rather than as a `revl` subcommand (`revl lsp` is not a valid choice),
with the `-P` safety bit so a composition directory cannot shadow a same-named
import.

---

## A COMPOSITION document argument

Six commands share one compile step: `compile`, `audit`, `version`, `test`,
`query` and `erase-report`. A `FILES` argument may be a MODULE or a single
COMPOSITION document, and a composition is RESOLVED rather than compiled as a
module (roadmap item 439): its rows are compiled, and the providers a `remote`
row SYNTHESIZES are in the document these commands answer from, with their
folded `net.<host>` reach.

Before that they compiled every argument as a module, and a composition
document, which declares no module-level component, compiled to nothing. Each
command then answered from the empty compilation: `compile` wrote an IR
document with no services and no components and exited 0, `version` read that
same document and derived "the interface is unchanged", `test` printed "no
tests to run", `query` answered "unknown component", and `erase-report`
answered "unknown realm".

The shapes these commands cannot resolve refuse by name with a nonzero exit,
rather than answering from an empty compilation: a composition document listed
beside modules (a composition names the rows it compiles, so the two describe
different programs), two composition documents in one invocation (a composition
document is the compiled unit), and a layer document (a layer is a delta over
the composition that stacks it, so it has no composition of its own; run the
command over the composition instead).

A composition is admitted whole, never as a layer delta, so no row is skipped,
and a non-first-party stack-layer row is compiled under its own untrusted-author
profile. Both are the over-refusing direction, and there is deliberately no
`--trust-host-code` on this path.

Each of the six takes `--root DIR`: the project root row provenance and origins
are recorded against (default: the working directory), the same root `revl
composition` takes. It is ignored for module arguments.

`revl goal` shares the same compile step and is deliberately NOT on this path.
`goal audit`'s exit code over a composition with no termination contract is
roadmap item 441/458's decision, and widening the document it is evaluated over
belongs to that item.

### The boundary-policy doors

Three more commands read the same composition document off their own compile
step rather than the shared one, and all three evaluate the item-33 boundary
policy: `policy evaluate` (the dry run of the gate), `dash --policy` (the
policy-exception queue) and `simulate policy-diff --composition` (the realms a
realm-scoped rule decides by). Each resolves a composition document, refuses
the same three shapes by name, and takes the same `--root DIR`.

Before that, `policy evaluate` reported `clean` and exited 0 over a composition
`revl audit --policy` refuses with a named violation, `dash --policy` showed an
empty pending-decision queue for the same composition, and `simulate
policy-diff` resolved no realms out of the document it was handed, leaving every
action a realm-scoped rule selects undecided.

`policy evaluate` and `simulate policy-diff` exit **2** on these refusals, not
1: on both verbs 1 already means a component would be refused, so a document
they could not read exits with their usage status instead. `dash` exits 1, the
status it already uses for an input it cannot read.

---

## Authoring and admission

### `revl compile`

Parse, check, link, and lower `FILES` to a backend IR document.

- `FILES` - one or more sources, or a single composition document (required;
  see [A COMPOSITION document argument](#a-composition-document-argument)).
- `-o`, `--output PATH` - write the IR here (default: stdout).
- `--root DIR` - with a COMPOSITION document argument, the project root row
  provenance and origins are recorded against (default: the working
  directory). Ignored for module arguments.
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

### `revl grammar`

Print the language surface, sized for a prompt. No sources; it renders the
built-in grammar.

- `--prompt` - the dense, complete, prompt-pinnable grammar (also shipped as
  `docs/syntax-2.0.prompt.txt`) instead of the short human summary.

```bash
revl grammar              # the short summary
revl grammar --prompt     # the full surface, to pin in a system prompt
```

### `revl doctor`

Diagnose each backend tier, runtime, and dependency, then smoke-test every
available tier. Each row reports `OK` / `WARN` / `MISSING` with a version and a
one-line reason; the footer counts them. Landed as roadmap item 291.

The report also prints a **toolchain resolution** block: a normalized
`component -> version` map (compiler, python, stdlib stamp, the exact cordis-py
and cordis-ts bindings, node, cargo, javac, go, wasmtime, wasm-tools) with an
absent component shown as `-`. It is the one place to pin a run: capture it on
one box and diff it against another to find the drift that broke a reproduction
(roadmap item 461). The cordis-py row names the exact binding that loads (its
on-disk origin and version when it carries one); cordis-ts reports the version
from its `package.json`.

- `--json` - machine-readable report (for an agent) instead of the table; carries
  the same `resolution` map as a top-level field, so an automation diffs one
  field rather than scraping the per-row detail strings.
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
  load order. Resolution alone compiles nothing. Admission is INCREMENTAL by
  default (item 426 S3): a composition with layers compiles its base once, then
  admits only the changed and withdrawn rows into that running manifest, so the
  cost is one compile of the layer delta, not of the whole composition.
- `--full` - with `--admit`, force WHOLE-COMPOSITION admission: every row is
  compiled, never just the delta. The verdict and resulting manifest are
  identical to the incremental default; this is the escape hatch for a
  cross-row rule a per-row admission cannot honour (426 §5.1).
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

### `revl analyze`

Derive a Petri net from the composition IR — provisions as places, activations
as transitions — and search it by bounded reachability for a dead state, a
marking from which some component can never activate. This is the REACHABILITY
question G2/G3/G7 do not answer ([analyze-liveness.md](analyze-liveness.md), item
438). Report-only: it names the deadlocked cycle, it does not refuse admission.

- `FILE ...` - composition source(s) to compile and analyze.
- `--ir DOC.json` - analyze a precompiled composition IR document instead of
  compiling sources (the analyzed unit is the IR, item 426).
- `--json` - the verdict and findings as JSON.
- `--max-states N` - bounded-BFS state cap (default 20000). A search that hits
  it reports INCONCLUSIVE, never a false "no deadlock" (item 418).
- `--max-tokens N` - per-place token cap (default 64); exceeding it flags
  possible unboundedness.

Exits nonzero only on a PROVEN deadlock (so CI can consume it); zero on a live or
bounded-inconclusive result.

```bash
revl analyze base.rvl
```

### `revl adapt`

Check whether a candidate's provided service can stand in for a required one,
and optionally synthesize the adapter that makes it so.

- `need` - the `.rvl` file declaring the required service (required).
- `candidate` - the `.rvl` file declaring the candidate's provided service
  (required).
- `--need-service NAME` / `--candidate-service NAME` - name the service on
  each side (default: the sole one in the file).
- `--adapt JSON_FILE` - the opt-in map `D` of defaults, drops, merges and
  pairings. Without it, only a structural match is accepted.
- `--emit` - also render the synthesized adapter `.rvl` source, the artifact
  you commit.
- `--name NAME` - component name for `--emit` (default: `Adapter`).
- `--provide-key KEY` - provided key for `--emit` (default: the need service
  name, lowercased).
- `--require-key KEY` - the alias the candidate is required under for
  `--emit` (default: `backing`).

The output carries `chainDepth`: the total composed depth a bridge onto this
candidate would have (1 onto ordinary code). When the candidate is itself a
committed adapter (it carries the section-4 derivation marking), the check
FLATTENS the chain and adds a `chain` array listing the proposed hop and the
committed inner hop end to end, so the review sees the composed loss - every
merge, default and drop across all hops - in one place, not just the last hop's
slice of it. An inner hop whose committed body uses a construct outside the
flagship reconstruction (a per-variant closed merge, an explicit default, a
fabricated field) is reported `opaque`, pointing at that adapter's own
attestation rather than inventing a plan it never ran.

```bash
revl adapt need.rvl candidate.rvl
revl adapt need.rvl candidate.rvl --adapt map.json --emit
```

### `revl layer check`

Resolve every row id in a layered composition and render the folded table with
each row's layer provenance, without lowering any component body. `layer` has
one subcommand, `check`.

- `file` - the `.rvl` document declaring the base composition (required).
- `--json` - the folded row table as JSON, provenance included.
- `--root DIR` - the project root that provenance and origins are recorded
  against.
- `--set @ROW.FIELD=VALUE` - the invocation overlay, the same syntax
  `revl composition` accepts.

```bash
revl layer check app.rvl
revl layer check app.rvl --json --set @db.pool=8
```

### `revl policy evaluate`

Dry-run a boundary policy over a composition and report, per rule, which
clauses pass or fail and why (a fact against a threshold). `policy` has one
subcommand, `evaluate`. See [boundary-policy.md](boundary-policy.md) for the
DSL.

- `POLICY` - the boundary policy file, DSL or JSON (required).
- `PROGRAM.rvl ...` - the source(s) to evaluate against: modules, or a single
  composition document (see [A COMPOSITION document
  argument](#a-composition-document-argument)). A composition compiled as a
  module reported `clean` and exited 0 for a composition `revl audit --policy`
  refuses.
- `--json` - machine-readable per-clause verdicts.
- `--root DIR` - with a COMPOSITION document argument, the project root row
  provenance and origins are recorded against (default: the working
  directory). Ignored for module arguments.
- `--component NAME` - narrow the report to one component.
- `--evidence DIR` - a component entry directory holding an `evidence/`
  bundle, for a bare-source component.
- `--registry DIR` with `--candidate NAME` - evaluate a published registry
  entry instead of a bare source.
- `--trusted-publisher ID` - a publisher id in the operator trust set
  (repeatable).
- `--key PATH` - an attestation verification key.
- `--mcp-scope COMPONENT` - treat COMPONENT as MCP/agent-admitted; `*` means
  every component.
- `--recompute` - run the operator's own local producers (the fault sweep, the
  inverse round-trip, and a cold gauntlet) against the component in hand and
  grade that freshly produced dossier instead of the published bundle. The
  evidence is operator-run at evaluation time, so it needs no attestation root,
  and each facet is marked `recomputed` (as opposed to `published`) in the
  report and in `--json`. A producer whose runtime is absent is skipped, never
  faked, and that facet stays as the published bundle carried it.

```bash
revl policy evaluate prod.policy app.rvl
revl policy evaluate prod.policy app.rvl --json --component Billing
revl policy evaluate local.policy app.rvl --recompute
```

### `revl simulate`

Simulate a change against a recorded run (roadmap item 468). `simulate` has
one subcommand, `policy-diff`, which reads two boundary policies and one
write-ahead log and computes the newly-allowed and newly-denied action sets the
change would have made over the crossings that run actually recorded, plus a
blast-radius summary bounded by that recorded action set.

This is not `revl audit --diff`, which re-audits a generation and fails when it
adds boundary crossings but keeps no policy on its path. It is also not
`revl policy evaluate`, which answers the policy-only question over a whole
composition, over the capabilities the composition declares rather than the
ones a run took. `simulate policy-diff` answers what the change would have done
to the actions this run took, and it refuses to answer what one recorded run
cannot say. An action the run never took is invisible here however widely the
new policy opens it; an effect record whose step carried no capability scope is
withheld rather than resolved to the label it recorded; and a realm-scoped rule
stays undecided until the compiled composition supplies the component's realms.

An action is a recorded effect record whose scope names a capability, and the
capability verdict is the gate's own: the diff calls the same
`policy.capability_verdict` the admission refuses by, over the two legs that
predicate reads, the deny-lists and the closed allow-lists. The admission
refuses a crossing on more legs than those two, and the diff reads none of the
rest: the agent-sandbox allow-list, the taint-flow tier, the approval and
declassify rules, the declaration-strength floors, the evidence bundle and the
recovery surface all decide by facts a WAL does not carry. Every leg is named in
the report and in `--json`, and a recorded pair whose surface moves on one of
them is reported undecided with the leg named, never as unchanged, so a widening
on a leg this diff cannot read is not reported clean.

No writer in this tree records the declared scope, so that definition is the
whole of the action channel: `scope.caps` reaches a record only where a timeline
step was annotated by hand, and the recorder never annotates, so on a WAL a run
wrote every effect record is unscoped and the recorded action set is empty. The
command reports those records as withheld, and exits non-zero on them, rather
than printing an empty diff as a clean change.

`--history` is the crash-recovery artefact, so a history that cannot be read
whole is exactly the shape that artefact is expected to have, and it is withheld
the same way rather than read as a run that took no actions. A torn tail (the
crash itself) and a recording that never reached its `activation-complete`
record are both reported as a finding with no record count, and both exit
non-zero; this is the reading `revl branch` already gives a torn tail
(`branch.py` reports it as a `torn-tail` finding, and the command exits `1` on
findings and residue). A file that is not a recording at all, such as a text
file or an empty one, reaches the same finding.

- `OLD` - the boundary policy in force (required).
- `NEW` - the boundary policy to simulate (required).
- `--history FILE` - the write-ahead log to read (required).
- `--composition FILE ...` - compile these composition sources and take each
  component's realms from them; without one a realm-scoped rule is undecided. A
  single COMPOSITION document is resolved rather than compiled as a module (see
  [A COMPOSITION document argument](#a-composition-document-argument)); as a
  module it named no component at all, so supplying one left the rule as
  undecided as omitting it.
- `--root DIR` - with a COMPOSITION document passed to `--composition`, the
  project root row provenance and origins are recorded against (default: the
  working directory). Ignored for module arguments.
- `--json` - machine-readable output.

Exit status follows the widening: `1` when the change newly allows a recorded
action, leaves one undecided, or withholds a record it could not name (including
every record of a history it could not read whole), and `0` when it only narrows
or changes nothing and every record was named.

```bash
revl simulate policy-diff prod.policy next.policy --history run.wal
revl simulate policy-diff prod.policy next.policy --history run.wal --json
revl simulate policy-diff loose.policy tight.policy --history run.wal --composition app.rvl
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

A `FILES` argument may be a MODULE or a single COMPOSITION document. A
composition is RESOLVED before it is audited (roadmap item 439): its rows are
compiled and the providers a `remote` row SYNTHESIZES are on the surface with
their folded `net.<host>` reach, so a composition that crosses to a peer over
the network audits as the crossings it makes. Before that the command compiled
its arguments as modules, and a composition, which declares no module-level
component, rendered an empty surface and exited 0. An empty audit surface reads
as an absence of authority, so that silence failed open.

The shapes the command cannot resolve refuse by name with a nonzero exit,
rather than rendering an empty surface: a composition document listed beside
modules (a composition names the rows it compiles, so the two describe
different surfaces), two composition documents in one invocation (a composition
document is the audited unit), and a layer document (a layer is a delta over
the composition that stacks it, so it has no boundary surface of its own; audit
the composition instead).

A composition is admitted whole, never as a layer delta, so no row is skipped
out of the surface being counted, and a non-first-party stack-layer row is
compiled under its own untrusted-author profile. Both are the over-refusing
direction. There is deliberately no `--trust-host-code` here: an audit that had
to be told to trust the code it is enumerating would be answering a different
question.

- `FILES` (required).
- `--json` - machine-readable output. This is the **supported surface for
  consumers**: a versioned, schema-published document
  ([interchange-format.md](interchange-format.md)). The default (prose) render
  is for people and is **not** a stability contract; its wording may change
  between releases. Parse `--json`, not the prose.
- `--root DIR` - with a COMPOSITION document argument, the project root row
  provenance and origins are recorded against (default: the working
  directory), the same root `revl composition` takes. Ignored for module
  arguments.
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

### `revl goal audit`

The termination-contract blind-spot report: the class-(c) capabilities a run
reaches that no criterion in its own contract observes. A pure function of a
compiled IR, with no session, policy or runtime involved. `goal` has one
subcommand, `audit` (roadmap item 441,
[441-goal-contracts.md](design/441-goal-contracts.md) §5.2,
[458-termination-language-surface.md](design/458-termination-language-surface.md) §7).

- `files ...` - the composition source(s) to audit (required).
- `--json` - machine-readable output.

```bash
revl goal audit app.rvl
revl goal audit app.rvl --json
```

### `revl replay`

Replay-mode readiness over a durable write-ahead log (roadmap item 250,
Slice 3b, [250-slice3b-replay-modes.md](design/250-slice3b-replay-modes.md)).
For each mode - `exact`, `tool-only`, `model-substitute`, `counterfactual` - it
reports whether the WAL's durable model decisions carry enough to inform that
mode and what a live executor would still need. It reads the record and runs
nothing, the same offline-reader contract as `revl branch` and `revl compare`.

- `WAL` - a write-ahead log, ideally one written by a Slice-3a runtime that
  recorded its model decisions (required).
- `--mode <mode>` - report readiness for one mode only, instead of all four.
- `--under POLICY` - counterfactual incident replay (roadmap item 467,
  [467-counterfactual-replay.md](design/467-counterfactual-replay.md)):
  recompute this policy's reach rules over the crossings the WAL recorded and
  name the first dangerous crossing. Without a candidate the question is not
  answerable from the record, because a crossing's capability token is not on
  the WAL, and the command says so and exits 1.
- `--candidate FILE...` - the candidate composition to resolve each recorded
  crossing's capability token from and to admit under `--under`. It is compiled
  statically, so no live effect fires.
- `--json` - machine-readable output.

```bash
revl replay run.wal
revl replay run.wal --mode exact --json
revl replay incident.wal --under policy/new.toml --candidate candidate.rvl
```

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
- `--root DIR` - with a COMPOSITION document argument, the project root row
  provenance and origins are recorded against (default: the working
  directory). Ignored for module arguments. See
  [A COMPOSITION document argument](#a-composition-document-argument): a
  composition compiled as a module derived "the interface is unchanged" for a
  composition whose rows had changed.
- `--json` - machine-readable derivation.

### `revl changelog`

Derive a release note from the interface, structural and authority delta
between two compositions. The semver headline comes from the same interface
diff `revl version` uses.

- `--from OLD` - the earlier composition: a compiled IR/interchange JSON
  document (`revl compile -o`, `revl audit --json`) or a `.rvl` source
  (required).
- `--to NEW` - the later composition, same accepted forms (required).
- `--format {markdown,json,plain}` - `markdown` is the stable release-note
  skeleton (default), `json` the structured document a registry or bot
  consumes, `plain` the skeleton with no markup.
- `--json` - alias for `--format json`.
- `--no-semver` - skip the semver headline (structural + authority changelog
  only); implied when an input carries no interface table.
- `--current-version X.Y.Z` - the earlier composition's declared version; with
  it, the computed next version is printed in the headline.
- `--title TEXT` - an opaque header line for the Markdown note. It never
  enters a derived line.

```bash
revl changelog --from v1.ir.json --to v2.ir.json --current-version 1.4.0
revl changelog --from old.rvl --to new.rvl --json
```

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

Every static verb's `FILES` may be a single COMPOSITION document instead of
modules, and each takes `--root DIR` (see [A COMPOSITION document argument](#a-composition-document-argument)). A composition
compiled as a module answered "unknown component" for every name its rows
define.

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

### `revl dev`

Run the exemplary web app under one parent process: Vite serves the frontend
and the Python Cordis driver boots the `.rvl` composition (roadmap item 724).
This is the local-development entry point; `revl run` is the same lifecycle
without the frontend.

- `files` - the `.rvl` app source (default: `examples/app/notes.rvl`).
- `--frontend DIR` - the Vite frontend directory (default: `frontend/`
  alongside the app source). It must exist and contain a `package.json`.
- `--host HOST` - the Vite bind host (default: `127.0.0.1`, loopback).
- `--port PORT` - the Vite port (default: `5173`). `0` is refused (exit 2)
  before anything is spawned: it asks Vite to bind an arbitrary free port, so
  the banner could not name the URL it printed.
- `--once` - boot the app, prove teardown has no residue, and exit.
- `--no-frontend` - boot only the app host, useful for diagnosing lifecycle
  failures without a frontend in the way. No Vite is spawned and no banner is
  printed, so `--port` is inert and is not validated.

```bash
revl dev                                  # examples/app/notes.rvl + its Vite frontend
revl dev myapp.rvl --frontend web --port 5180
revl dev --once                           # CI: boot, prove no residue, exit
revl dev --no-frontend                    # app host only
```

The app source is compiled and admitted before Vite is spawned, so a source
error reports with its `compile` or `admission` line and no port is bound. Vite
is started with `npm run dev`, and `npm` must be on `PATH`.

The banner names the URL the frontend is served on, so Vite is told not to
move: the child runs with `--strictPort`. A port already in use therefore exits
3 naming that port, instead of Vite quietly auto-incrementing to the next free
port behind a banner that still advertises the requested one. The same exit 3
covers a frontend whose dependencies are not installed; install them with
`npm ci` in the frontend directory, which resolves from the committed
`package-lock.json` and needs no peer-deps escape hatch.

The WebUI coeffect is a real scoped Cordis provision rather than a
process-global bridge: the development adapter records the entry the
composition registered, rejects an inline substitute or a path that escapes the
app root, and is withdrawn during normal LIFO teardown. Everything else -
compile, admission, config, boot and teardown - reuses the `revl run` driver, so
the two commands have precisely the same semantics.

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
  checks nothing. With `--placement` the CONDUCTOR watches the latch too and
  halts every process: a py child reads the latch itself, names its in-flight
  inventory and dies without unwinding; a child on any other tier is SIGKILLed,
  because that tier has no E-Stop seam. The halt report names every component
  left un-torn-down, one line each, and marks the residue it cannot enumerate
  UNKNOWN rather than omitting it.
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
cannot hang. **Any child that exits before saying `DOWN` is reported and the
run exits non-zero**, whichever signal ended it - the SIGKILL backstop, the
SIGTERM that asked it to stop, or a crash inside the unwind. Which signal it
died on says nothing about whether it finished unwinding; the `DOWN` line is
the only thing that does. Such a child leaves its entries STRANDED and its
residue UNKNOWN, the same verdict [`revl estop`](design/443-estop.md) gives a
halted session, and it is never a clean exit. Reconcile a durable run with
`revl recover --wal FILE`. Set `REVL_TEARDOWN_GRACE=<seconds>` (default 30)
when a teardown is legitimately long rather than wedged.

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

**Across a placement.** `revl run --placement P --estop-latch FILE` arms the
same latch for a composition split across processes, and `revl estop --latch
FILE` halts all of them. The two populations are different and the report says
which one each component got:

- a process on a tier that HONORS the latch (today: `py`) refuses new crossings
  at its own seams from the instant the latch appears, prints its inventory —
  the entries it stranded and the at-most-one crossing that was already
  dispatched — and dies where it stands, running no teardown;
- a process on any other tier (`node`, `rust`, `go`, `java`, `wasm`) has no
  E-Stop seam, so the only halt available for it is a SIGKILL. It may have
  dispatched a crossing microseconds before it died and nothing recorded that,
  so its residue is **UNKNOWN** and the report says so by name.

`REVL_ESTOP_HALT_WINDOW` (default 2 seconds) bounds how long a latch-honoring
child gets to name its inventory before it is killed anyway. It is not a
teardown grace and must never grow into one: by the time it starts the child is
already refusing crossings, so it buys the inventory and nothing else.
### `revl slo`

The composition **SLO contract, observed** (item 473,
[473-slo-contracts.md](design/473-slo-contracts.md)). The compile-time half of
the contract needs no verb: it runs whenever a composition resolves, and refuses
a document whose own `emission[...]` ceilings contradict the `slo { ... }` block
it declares. This verb is for the two things that happen against a run that has
already finished: measuring it, and gating the next rollout on what the
measurement found.

Three acts, selected by flag:

- **measure** (the default) - read a recorded trace against the declared
  contract, take the declared `on breach` response, and sign a receipt bound to
  the generation.
- `--verify` - check a receipt. Fail closed: a wrong envelope, a wrong key, an
  altered body, or a summary that contradicts the verdicts it summarises is
  refused, and nothing reads as "valid but unverified".
- `--gate` - REFUSE this rollout when the receipt of the generation it replaces
  measurably breached an objective the candidate still declares.

Flags:

- `--composition FILE` - the composition whose `slo { ... }` block is the
  contract. Its IR is read rather than re-parsed, so the objectives gated here
  are the ones the compile-time gate already admitted.
- `--root DIR` - the composition root its rows resolve against.
- `--trace FILE` - a recorded causal trace. The measurement reads only this: no
  metrics backend is called and no service is scraped.
- `--generation N` - the generation the measurement belongs to. It is carried
  inside the signed body, and a receipt filed under another generation is
  refused before it is filed.
- `--latch FILE` / `--wal FILE` - where a `pause` or `halt` response writes, the
  same latch `revl estop` arms and a running composition watches.
- `--key PATH` - the receipt signing key (else `REVL_SLO_KEY_FILE`, else
  `REVL_SLO_KEY`). With no key the run is measured and reported but not signed,
  and the output says so.
- `--signer TOKEN` - who issued the receipt, recorded inside the signed body.
- `--receipt FILE` - the receipt to verify, or the predecessor receipt to gate
  against.
- `--out FILE` - write the signed receipt.
- `--json` - machine-readable output.

Exit status distinguishes the two operator situations: **0** clean, **1** a
measured breach (the run missed its objectives), **2** a refused rollout (this
rollout was not allowed to start).

**What the gate refuses, and what it does not.** A datum the candidate still
declares, whose `breached` observation in the predecessor's receipt would also
breach the candidate's own target, refuses the rollout by name. A candidate that
widened the objective past the witnessed value is admitted, and the widening is
recorded: that is the author withdrawing a promise in the source, where a
reviewer sees it. An `insufficient` or `unmeasurable` datum never refuses
anything, because it is "nobody knows" rather than an observed failure, but it
is reported under `notHolding` so an admission is never mistaken for a clean
bill. A receipt
presented as evidence that does not verify is a refusal; the absence of a
receipt is not, because the first generation of any composition has no
predecessor.

**The window a verdict was taken over.** An entry may declare `over <duration>`,
`min <count>` and, on a rate, `of <denominator>`
([composition-rows.md](composition-rows.md#slo-the-rollout-contract)). A
declared window narrows the population before the verdict is taken and is
printed on the verdict and carried inside the signed body, because a target and
the window it was measured over are one promise. Two things it does not do. A
window can only be applied to a population whose records carry a timestamp, and
the run's own `model-decision` WAL records do not, so a window declared on
`p95_latency` against that population reads `insufficient` naming the record
rather than silently answering over the whole run. And an entry that declares no
window is measured over the whole of the generation's population, exactly as it
was before the qualifier existed.

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

### `revl trace`

The causal trace of a recorded run: which component caused which hop, in
order. Reads the JSONL that `revl run --trace` writes.

- `FILE` - a JSONL causal trace written by `revl run --trace` (required).
- `--json` - the machine-readable trace document instead of the human view.
- `--component NAME` - only that component's hops.
- `--model` - only model hops (the LLM view).
- `--otel` - emit the trace through the OTel SDK, delegating to `revl.otel`.

```bash
revl run app.rvl --trace run.jsonl
revl trace run.jsonl --component Billing
```

### `revl attest`

Cryptographic attestation of a verified composition (roadmap item 127): sign a
portable record that this exact composition was admitted (canonical IR hash +
verdict + guarantees + timestamp), or `--verify` one ([revl-attest.md](revl-attest.md)).

With `--certificate` the same verb signs a wider envelope instead (roadmap item
474): a component certificate that states what the composition's guarantees rest
on, read out of the formal package, beside the admitted verdict. See section 7
of [revl-attest.md](revl-attest.md) for the trust boundary.

- `target` - what to attest: a composition (`.rvl` source, compiled IR, or
  `audit --json`). With `--verify`, the attestation JSON to check instead
  (required). With `--verify-certificate`, the certificate JSON to check.
- `--verify` - verify mode: `target` is an attestation JSON; check its
  signature (and, with `--against`, that the composition still matches). Exits
  nonzero if the attestation is invalid.
- `--certificate` - sign a component certificate rather than an attestation:
  the same gate verdict, plus the per-guarantee coverage, the proof model, the
  artifacts' digests and the caveats the formal package records.
- `--verify-certificate` - verify mode for a certificate: re-derive its
  evidence from the formal package and check that the document matches it.
  Exits nonzero if the certificate is invalid. Never reads a status out of the
  document it is checking.
- `--formal DIR` - the formal package to read the evidence from, instead of the
  one found from the working directory or `REVL_FORMAL_DIR`. A directory with no
  `STATUS.md` is refused rather than silently searched past.
- `--against COMPOSITION` - with `--verify` or `--verify-certificate`: the
  composition to re-hash and check the record against. Omit to check only the
  signature over the embedded hash.
- `--key PATH` - the signing/verifying key file. Falls back to the
  `REVL_ATTEST_KEY_FILE` (a path) or `REVL_ATTEST_KEY` (the secret) environment
  variables. Never hardcoded, and never passed as a value in argv.
- `--signer NAME` - an optional signer label recorded in (and signed into) the
  record; falls back to the `REVL_ATTEST_SIGNER` env var.
- `--json` - machine-readable output.

### `revl pool`

Stand up and operate a private peer pool (roadmap item 524): several
independent operators running work for each other without trusting each other's
machines. The verbs declare a pool, admit a peer that proves its identity and
its artifact, read the roster, and withdraw a peer. See
[design/550-private-peer-pool.md](design/550-private-peer-pool.md) for the trust
progression and the failure direction of every step in it.

The pool is a signed charter plus an append-only roster, and a directory of the
peers' public keys. A peer's identity is a key, not an address, and its join
request pins the charter by digest, so a peer agrees to a set of terms rather
than to a pool name. Every refusal names one lowercase link (`artifact-digest`,
`replayed-join`, `grant-ceiling`, ...), and the verb exits nonzero on one.

A peer's identity is an asymmetric key pair by default (issue #1278): the peer
draws it with `pool keygen`, the operator pins only the public half with `pool
register`, and a join, an offer and a withdrawal are each verifiable by any
holder of that public key rather than only by the operator. A compromised
operator key therefore forges no peer's join. The original shared-key MAC
backing is still available as `--identity shared-key`, and `--identity mixed`
admits both during a migration, in which case `pool status` names the members
still on the weaker one. There is no fallback between the two: `sign_alg`
selects one verifier and its failure is a refusal, and a peer with a pinned
public key may never present a shared-key join. See
[design/555-asymmetric-peer-identity.md](design/555-asymmetric-peer-identity.md)
for the key lifecycle and what the signature binds.

- `init` - declare a pool and sign its charter.
  - `--dir DIR` - where `charter.json` and `roster.json` are written.
  - `--pool-id ID` - the pool's name. A peer pins the charter by digest, so
    renaming a pool does not let old terms be reused.
  - `--ceiling CAP` - the most authority this pool will ever delegate to any
    member at any tier. Repeatable. Every tier grant is diffed against it.
  - `--entry-caps CAP` - the grant the entry tier hands a newly admitted peer.
    Repeatable. Not covered by `--ceiling` means the pool admits nobody.
  - `--artifact DIGEST` - an artifact digest this pool admits. Repeatable.
  - `--trust-floor LEVEL` - the minimum attested trust a joining peer clears.
  - `--identity MODE` - `asymmetric` (default), `shared-key` or `mixed`. The
    mode is inside the signed charter body, so it cannot be flipped without
    re-signing.
  - `--revoke-identity PATH` - a public identity file whose fingerprint may
    also revoke. Repeatable. Give this when withdrawals should be signed with a
    key pair.
  - `--key PATH` - the operator signing key (falls back to
    `REVL_ATTEST_KEY_FILE` / `REVL_ATTEST_KEY`).
- `keygen` - the peer side: draw a key pair on this machine. The private half is
  written 0600 and never leaves it; the public half is what the operator pins.
  - `--peer-id ID`, `--out PATH` (private), `--public PATH`
- `register` - pin a peer's public key. The only way a key enters the
  directory: a join request cannot introduce the key it is checked under. Check
  the fingerprint over a second channel before pinning.
  - `--dir DIR`, `--public PATH`
- `rotate` - replace a peer's active key. The old key stays in the directory and
  keeps verifying what it signed; it authorises nothing from the rotation on.
  - `--dir DIR`, `--public PATH` (the NEW public half), `--reason TEXT`
- `revoke-key` - withdraw a key's authority. It keeps verifying, so the records
  it signed stay checkable by anyone holding the public half.
  - `--dir DIR`, `--peer-id ID`, `--key-id FP`, `--reason TEXT`
- `request` - the peer side: sign a join request against a charter it was
  given, carrying its signed peer offer and the artifact digest it will run.
  - `--charter PATH`, `--peer-id ID`, `--artifact DIGEST`, `--out PATH`
  - `--ceiling CAP` - the most authority this peer will accept. Repeatable. A
    tier grant not covered by it is refused.
  - `--trust LEVEL`, `--region NAME`, `--hardware NAME` - the facets this peer
    attests.
  - `--identity-key PATH` - this peer's private identity file from `pool
    keygen`. The join and the offer are both signed with it.
  - `--key PATH` - this peer's shared key, exchanged with the operator out of
    band. Used only when `--identity-key` is not given.
- `join` - the operator side: decide a peer's signed join request against the
  charter. Admits at the entry tier or refuses, naming the link.
  - `--dir DIR`, `--join PATH`, `--key PATH`
  - `--peer-key PATH` - needed only for a legacy shared-key join. A peer with a
    pinned public key is verified against that.
- `status` - members, tiers, what each holds, the effect class each tier
  admits, who may admit, revoke and attest, and which members are on which
  identity backing. Needs no key.
  - `--dir DIR`, `--json`
- `withdraw` - remove a peer and report, in three disjoint sets, what that
  revokes (an inverse exists), what it retains (no inverse: the work is done
  and the ledger keeps it) and what it orphans (outstanding work, handed to the
  lawful-retry dispatcher). Every key the directory pins for the peer is
  revoked, not only the one that signed its join: each confers no authority and
  each still verifies what it signed, so the ledger stays checkable.
  - `--dir DIR`, `--peer ID`, `--reason TEXT`, `--key PATH`
  - `--identity-key PATH` - sign the withdrawal receipt with an operator key
    pair, so any holder of the matching public key can check who removed whom.
    Its fingerprint must be in the charter's revoke authority.

### `revl erase-report`

Right-to-erasure evidence for one realm: in-process state gone (no-residue
proof), boundary crossings compensated-vs-bare, and other realms provably
untouched ([erase-report.md](erase-report.md)).

- `FILES` - sources, or a single composition document (required; see [A COMPOSITION document argument](#a-composition-document-argument)). A composition compiled as a module answered "unknown realm" for
  every realm its rows declare.
- `--realm R` - the realm to report erasure evidence for (required).
- `--root DIR` - with a COMPOSITION document argument, the project root row
  provenance and origins are recorded against (default: the working
  directory). Ignored for module arguments.
- `--json` - machine-readable, versioned report document.
- `--no-residue-proof` - skip the runtime teardown proof (static sections
  only; use where the cordis runtime is unavailable).

### `revl retention-receipt`

The erasure REQUEST under a declared `retention` policy, and the check on a
receipt somebody presents (roadmap item 472). The receipt is a signed
ENUMERATION of the replicas and derivatives the system knows about, not a proof
of destruction: what the signature establishes is which copies were named, under
which policy, by whom, and that the document has not been altered since.

The sources are PARSED, not compiled. Past a policy's deadline a composition
that persists a value under it is refused (`G-RETAIN`), and that refusal is why
the erasure is being requested, so this verb has to work on a composition the
checker will not admit.

- `FILES` - the sources declaring the policy (required when issuing).
- `--policy NAME` - the `retention <NAME> { ... }` the request is made under.
- `--requester WHO` - who is requesting the deletion. Must be one of the
  policy's own `deleters`, or the request is refused rather than signed.
- `--replica TOKEN[@RESIDENCE]` - one known copy; repeatable. A replica whose
  residence differs from the policy's is reported `residence-mismatch`.
- `--derivative NAME=CLASS[@RESIDENCE]` - one value made from the retained data;
  repeatable. `CLASS` is one of `summary`, `index`, `embedding`, `backup`,
  `export`, `cache`. A class the policy does not cover is reported
  `not-covered` and explicitly not claimed erased.
- `--inventory PATH` - an operator enumeration as JSON,
  `{"replicas": [...], "derivatives": [...]}`, for the rows a command line
  cannot carry. An unknown member is refused by name.
- `--receipt-key PATH` - the signing key file; falls back to
  `REVL_ERASURE_KEY_FILE` (a path) then `REVL_ERASURE_KEY` (the secret), and is
  never hardcoded.
- `--signer NAME` - the issuer identity recorded inside the signed body.
- `--issued-at INSTANT` - the RFC-3339 instant to stamp, so an issue is
  reproducible; defaults to now.
- `--verify PATH` - check a presented receipt instead of issuing one.
- `--json` - machine-readable receipt or verdict.

Exit codes, and the two directions they fail in. Issuing: a receipt that cannot
be signed is never printed, so an unresolvable key, an unauthorised requester,
an unknown derivative class, an undeclared policy name and sources declaring no
policy at all are each exit `1` with nothing on stdout. Verifying: a receipt
that IS presented and does not verify is exit `1` naming the one reason -
including when no key can be resolved, because "could not check it" is never
"valid". An ABSENT receipt is not a refusal here at all; that is the issue path.

### `revl dash`

The supervisor's cockpit (roadmap item 63): a READ-ONLY live view over a
session or a recorded run - the dependency graph (realms, seams), the causal
trace streaming, and the pending-decisions queue with evidence attached
([dash.md](dash.md)).

- `FILES` - the composition whose graph to show (required): modules, or a
  single composition document (see [A COMPOSITION document
  argument](#a-composition-document-argument)). A composition compiled as a
  module rendered an empty graph and an empty `--policy` decision queue.
- `--root DIR` - with a COMPOSITION document argument, the project root row
  provenance and origins are recorded against (default: the working
  directory). Ignored for module arguments.
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

- `FILES` - sources, or a single composition document (required; see [A COMPOSITION document argument](#a-composition-document-argument)). A composition compiled as a module collected nothing and printed
  "no tests to run".
- `--root DIR` - with a COMPOSITION document argument, the project root row
  provenance and origins are recorded against (default: the working
  directory). Ignored for module arguments.
- `--backend {py, ts, rust, java, wasm, go, all}` - tier to run the blocks on
  (default: `py`); `all` runs every tier whose toolchain is present.
- `--list` - print every test name the compilation collects (plain `test`,
  `prop test` and `fault test` units, in the order the py tier runs them) and
  execute nothing: no tier runner, no emit, no runtime. A query, so the mode
  flags below do not apply to it; with `--filter`, list only the selection.
  Listing a compilation that collects no test units exits 2.
- `--filter PATTERN` - run only the collected test units whose name *contains*
  `PATTERN`. A plain substring, not a regex. A `PATTERN` that selects nothing
  exits 2 with a message on stderr: an empty selection is never a silent green.
  The selection is applied to the IR, so every tier and every mode that reads a
  test section honours it, and `--mock-requires` (which runs `lifecycle test`
  units by name) honours it too. A tier the selection leaves with no unit it
  runs (`prop test` and `fault test` are py-tier-only) reports `skip` with that
  reason and exits 0: nothing was expected to run there, which is never printed
  as a pass. `--sweep` and `--schedule-*` sweep steps and interleavings rather
  than named units, so combining them with `--filter` exits 2 instead of
  filtering nothing.
- `-v`, `--verbose` - append a per-test duration to each one-line `PASS`/`FAIL`
  (py tier only).
- `--report {json,tap}` - emit a machine-readable per-test report (name, status,
  duration) in JSON or TAP form instead of the human per-test lines. Verdicts
  and exit codes are unchanged; py tier only.
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

## Emitting and packaging

### `revl emit`

Render one backend's source for a composition, without the IR round-trip
`revl compile` does. This is the emitter surface the conformance matrix
measures.

- `files` - one or more `.rvl` sources (required).
- `--backend {python,typescript,rust,java,go,wasm}` - the emitter to render
  (default: `typescript`).
- `--target TARGET` - a rendering of that backend's emitter, orthogonal to
  `--backend`. `temporal` renders the TypeScript emitter for the Temporal TS
  SDK; omit for the backend's native runtime.
- `-o`, `--output PATH` - output path (default: stdout).

```bash
revl emit app.rvl --backend rust -o app.rs
revl emit app.rvl --backend typescript --target temporal
```

### `revl bundle`

Emit a whole composition into a portable bundle directory: every backend's
source plus the runtime manifest, so one artifact carries every tier.

- `files` - the `.rvl` sources to bundle, the whole composition (required).
- `--out DIR` - the bundle directory to write, e.g. `app.revlbundle`
  (required).
- `--backend BACKEND` - a backend to emit into the bundle; repeatable. Omit to
  emit every backend (python, typescript, rust, java, go, wasm). An emitter
  that refuses this IR is recorded as skipped, not as a failure.
- `--topology PLACEMENT` - a placement/topology map (TOML or JSON) carried in
  the bundle as `topology.json`; omit for a single-process bundle.
- `--json` - print the bundle path and its runtime manifest as JSON.

```bash
revl bundle app.rvl --out app.revlbundle
revl bundle app.rvl --out app.revlbundle --backend rust --backend wasm
```

### `revl verify`

Check a bundle: that each tier in it is present, well-formed and agrees with
the manifest. Exits nonzero when a tier fails, so it is usable as a release
gate.

- `BUNDLE` - a bundle directory written by `revl bundle` (required).
- `--json` - machine-readable tier-by-tier report.

```bash
revl verify app.revlbundle
revl verify app.revlbundle --json    # CI: parse the per-tier verdict
```

### `revl deploy`

Deploy a compiled composition across process seams with attested admission and
coordinated rollback (roadmap item 118). Every seam is a local process seam
today; cross-machine control-plane pieces are deliberately deferred.

- `MAP` - the deploy/placement map (TOML or JSON): `[processes.<p>]` with an
  optional `[processes.<p>.deploy]` table per process.
- `--dry-run` - run the admission plan and stop before any COMMIT; reports the
  targets, refusals and boundaries without opening a boundary or activating any
  participant.
- `--json` - machine-readable verdict: the admission plan under `--dry-run` (or
  on a refusal), otherwise the coordinated deploy report.

Admission is the load-bearing half: the receiver re-hashes the IR and artifact
bytes it will actually execute and verifies item 127's signed `evidence_bindings`
chain (`source -> IR -> artifact -> policy -> evidence`) against a local trust
store, never trusting a self-declared `backend`/`artifact_hash`. A machine
boundary is refused outright, an unknown `via` is refused rather than read as
local, and a container target is refused unless it is proven seam-free.

Without `--dry-run`, an admitted map is DEPLOYED over the process boundary
(`via = local`): one participant per process is spawned and driven through the
two-phase PREPARE/COMMIT protocol. The coordinator holds only an ordered commit
ledger and, on any COMMIT failure, drives ABORT in reverse ledger order so each
participant runs its own local LIFO unwind — a participant it cannot reach is
reported `unresolved`, never rolled back. The aggregate verdict is `applied`
only when every participant applied, `aborted-clean` only when every committed
participant rolled back clean, otherwise `aborted-with-residue`. Because
admission supplies no seam set here, only a process-boundary target reaches the
COMMIT path; the cross-machine / container launch orchestration and the
host-side chain verify at admission are following slices.

```bash
revl deploy deploy.toml --dry-run     # plan admission, stop before COMMIT
revl deploy deploy.toml               # deploy over the process boundary
revl deploy deploy.toml --json        # machine-readable verdict
```

### `revl deploy-admit`

The far-side runner of the deploy orchestration channel (roadmap item 118). It
reads one JSON request per line on stdin and writes one signed verdict per line
to stdout: a PREPARE admit-request runs the attestation-chain verify, and a
COMMIT commit-request runs the load-time measurement. It is the process a
transport spawns to have the RECEIVING side do the check, against that host's
own local trust store, rather than the conductor checking in its own process.

- `--key PATH` - a file holding a raw HMAC verify key this host trusts;
  repeatable. The file is read the way `attest.load_key` reads it (one trailing
  newline stripped). The request carries no key, so with no `--key` the chain
  cannot be verified and admission refuses at the signer link.
- `--host-key PATH` - a file holding this host's own signing key, read the same
  way (one trailing newline stripped). PREPARE signs its admission verdict with
  it and COMMIT signs the load-time measurement with it; with no `--host-key` a
  COMMIT refuses rather than returning an unsigned, unattributable measurement.
- `--require-gauntlet` / `--require-conformance` - refuse a chain that binds no
  item-31 gauntlet or item-306 conformance evidence. The host is the floor: a
  request may add either requirement but never turn one off.
- `--runtime-version NAME=VER` - a runtime this host reports on its verdicts
  (e.g. `python=3.14`); repeatable.

The trust configuration is the runner's own, never anything the request carries:
a request can ASK for admission but cannot supply the trust that would grant it.
Locally the conductor spawns the `revl deploy-admit` runner as a local child
process and speaks to it over stdin/stdout (`deploy.StdioRunnerTransport`);
across a machine boundary it
is the same command behind `ssh <host>`, which is a following slice together
with the bundle staging and pinned SSH host key.

```bash
# usually spawned by a transport, not by hand; driven over stdin/stdout:
revl deploy-admit --key host.pub --host-key host.key --runtime-version python=3.14
```

## Interop: MCP, import, export, source maps, serve

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
- `--follow-redirects` - let the generated crossing follow a **same-origin**
  307 or 308. Off by default: the endpoint is part of what the file declares
  and what the composition admits, so a redirect is refused rather than
  followed. A 301, 302 or 303 stays refused even with the flag, because it
  re-issues the `POST` as a `GET` and drops the body
  ([import-a2a.md](import-a2a.md) §3a).
- `-o`, `--output PATH` - output path (default: stdout).
- `--json-diagnostics` - structured diagnostic on rejection.

Every result is `Untrusted[Str]`, the reach is derived from the endpoint's host
alone, and no inverse is ever synthesized: a remote call cannot participate in
G7 teardown. See [import-a2a.md](import-a2a.md) §2.

### `revl export`

The reverse of `revl import`. Two subcommands.

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

`revl export client FILES` - generate a typed remote CLIENT for a NON-revl
consumer, over the canonical value encoding the bridges already speak
([interop-bridge.md](interop-bridge.md); item 424 gap (c), slice C1). Pure IR
codegen: no runtime, no emission, no language change. The client's types ARE the
wire encoding (a record is a plain object, `Opt[T]` is `T | null`, a user ADT or
`Result` is the adjacently-tagged `{$kind, $value}` object), so a value
round-trips to the placement bridge by construction. It carries the gate
FRONTIER (item 338) and is bounded LOCALLY: it makes no claim about what the
callee runs (D-424c.8; there is no verified-remote badge - a mutual guarantee
between two revl peers is `revl contract export`/`check`). A method the
projection cannot express - a resource handle (an `extern acquire` return), a
`Map` with a non-`Str` key - is refused at generation, naming the method. The
generated file also ships an `httpTransport(base)` factory that drops straight
onto a `revl serve --http` face (below): `base` is the service's route prefix
`http://host:port/<composition>/<key>`, and each call POSTs its positional
arguments as a JSON array.

`--face webui` projects a different typed boundary from the same IR: the Cordis
WebUI channel of ONE component (item 457 slice S4, design note 530 Decision B;
see [frontend-assets.md](frontend-assets.md)). The reactive state is the record
type of the `data` parameter the component publishes through `webui.add_entry`,
and the RPC method set is the services the component `provides` - the surface
`revl audit` reports as G1 - so the browser's `useRpc<T>()` type and the server's
published fields are one declaration. It emits `<Component>State`,
`<Component>Rpc` and `<Component>Channel`, and no transport: Cordis WebUI owns the
WebSocket seam. A component with no webui requirement, an `add_entry` with no
`data` parameter, a `data` type that is not a declared record, or a component that
provides nothing is refused rather than projected as half a contract.

- `FILES` - `.rvl` source files (required).
- `--lang LANG` - target language for the generated client (default: `ts`; `ts`
  is the slice-C1 target).
- `--face FACE` - which typed boundary to project: `rest` (default, the remote
  client above) or `webui` (a component's browser channel).
- Exactly one of (required, mutually exclusive):
  - `--service NAME` - export a client for a single service by name (`rest`).
  - `--composition` - export a client for every service the composition
    provides (`rest`).
  - `--component NAME` - export this component's browser channel (`webui`).
- `-o`, `--output PATH` - output path (default: stdout).
- `--json-diagnostics` - structured diagnostic on rejection.

### `revl sourcemap`

Compose Source Map v3 documents. One subcommand.

`revl sourcemap compose MAP` - rewrite every mapping of MAP that points INTO a
generated file through that file's own map, so one document reaches the original
([frontend-assets.md](frontend-assets.md), item 459 F2). The case it exists for:
`stdlib/template.rvl` maps rendered text back to the template that produced it,
a bundler maps its output back to the files it read, and when a rendered file is
one of those inputs nothing composes the two, so devtools follow the bundler's
map back to the generated file and stop.

It compiles nothing and takes no `.rvl` files. The composition is defined as the
walk a consumer would do holding both documents (the last mapping on the line at
or before the column, no interpolation), mappings into other sources pass
through untouched, and a mapping the inner map does not cover becomes a 1-field
mapping rather than being kept or deleted.

A source map is a build artifact, so this reads exactly the paths on its own
command line: a `sources`, `sourceRoot`, `file` or `sourceMappingURL` entry
INSIDE a document is compared and copied as text and is never opened.

- `MAP` - the OUTER map, the bundler's, describing the artifact a browser loads
  (required).
- `--through [NAME=]MAP` - an INNER map: one generated input's own, the document
  `stdlib/template.rvl`'s `source_map` writes. `NAME` is how the OUTER map
  spells that input in its `sources`; without it the inner map's own `file` is
  used. Repeatable, applied in order (required). A `NAME` naming no source of
  the outer map is refused rather than returning the outer map unchanged, and a
  `NAME` whose path suffix matches two different sources is refused as
  ambiguous.
- `-o`, `--output PATH` - output path (default: stdout).

```bash
revl sourcemap compose dist/console.js.map \
  --through generated.ts=build/generated.ts.map \
  -o dist/console.js.map
```

Exits 1 naming the document and the reason on any refusal: a version that is not
3, an index map (`sections`), a malformed `mappings` run, an index outside its
array, or a `sourcesContent` whose length does not match `sources`.

### `revl serve`

Serve a composition's OWN provided operations (the fourth quadrant: hints
derived by the compiler), over one of two transports. Distinct from `revl mcp
serve`, which serves the compiler itself. The transport decides nothing: the
operation set, the checked emission hints and the wire shape are all the
compiler's.

- `FILES` (required).
- One transport (mutually exclusive; required):
  - `--mcp` - serve over the MCP stdio protocol.
  - `--http` - serve over HTTP. Each provided operation is
    `POST /<composition>/<key>/<op>`, with the request and response bodies in
    the canonical value encoding ([interop-bridge.md](interop-bridge.md)) - the
    server face `revl export client` pairs with (item 424 gap (c), D-424c.6). A
    request body is the operation's positional arguments as a JSON array (or an
    object `{"args": [...]}`, or empty for a no-argument call); a success replies
    `{"ok": true, "value": <encoded result>}`, the exact shape the placement
    bridge returns. `GET /` returns the manifest: the served operations, their
    compiler-derived `readOnly`/`emission` hints, and the gate FRONTIER the face
    was projected under. The face is LOCAL contract only - it makes no safety
    claim about any callee it in turn reaches - and binds loopback by default.
- `--host HOST` - `--http` bind address (default: `127.0.0.1`).
- `--port PORT` - `--http` bind port (default: `8080`).
- `--config FILE` - TOML/JSON file of `component-name = { ... }` config tables,
  supplied to each component at boot.
- `--env FILE` - TOML/JSON file of flat `name = value` environment values,
  injected into the composition's `boot` component. Its `config {}` block is the
  ENVIRONMENT CONTRACT (item 350): a `--config` table naming the boot component,
  an `--env` key the contract does not declare, a missing required field, or a
  value outside a declared `under "<prefix>"` / `in [...]` bound each refuse the
  boot before any runtime is imported. See
  [environment-binding.md](environment-binding.md).
- `--composition PREFIX` - tool/route-name prefix (MCP tools are
  `<prefix>.<key>.<op>`; HTTP routes are `/<prefix>/<key>/<op>`; default:
  `revl`).

---

## The component manager

### `revl truc`

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

`python -P -m revl.lsp` runs the human-facing language server over stdio
(`revl lsp` is not a subcommand; the parser rejects it as an invalid choice,
and the module is the only spelling). The `-P` is PYTHONSAFEPATH, the safety bit
that closes the CWD-shadowing window issue #317 names. It pushes
`textDocument/publishDiagnostics` from the checker, answers
`textDocument/hover` from the diagnostic explanations and symbol info, and
answers `textDocument/definition` from the resolver, reusing the compiler
surfaces read-only (`src/revl/lsp/`).

`python -P -m revl.otel run.jsonl` exports a `revl run --trace` lifecycle
trace to OpenTelemetry spans, events, and links (a transition is a span, its
cause an event, a causal edge a link), so a composition's causality shows up
in Grafana, Datadog, Honeycomb, or Jaeger. The OTel SDK is the optional
`revl[otel]` extra; `--json` prints the span model without it
([opentelemetry.md](opentelemetry.md)). `-P` is required: `-m` puts the
CWD at `sys.path[0]` and the otel subcommand imports `opentelemetry` from
bare name.

---

## See also

- [mcp-reference.md](mcp-reference.md) - every verb the `revl mcp serve` server
  exposes.
- [guide-humans.md](guide-humans.md) - the language and toolchain for people.
- [guide-ai-agents.md](guide-ai-agents.md) - the same, oriented for an agent
  driving the compiler over MCP.
