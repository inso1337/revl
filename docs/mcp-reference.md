# revl MCP verb reference

Every verb the `revl mcp serve` server advertises, its inputs, and what it
returns. This is the complete set, verified against `src/revl/mcp/server.py`
(the `TOOLS` registry and its handlers) and `src/revl/mcp/query_tools.py` (the
query verbs appended to it). The advertised list is exactly the 36 verbs below.

Start the server with `revl mcp serve` (see [commands-reference.md](commands-reference.md#revl-mcp)
for its flags). The protocol is JSON over stdio; `tools/list` returns these
verbs and `tools/call` invokes one.

## How to read this

**Source input.** Every verb that takes a candidate composition accepts the
same three-way input, and you pass exactly one form:

- `source` - inline `.rvl` text (use this for a generated component; it is
  never written to disk).
- `files` - an array of `.rvl` paths.
- `modules` - in-memory sources for `use` imports, keyed by the path the import
  names, so a multi-module candidate is checked without touching the
  filesystem.

Verbs marked "defaults to the loaded session" may be called with none of these,
in which case they act on the composition the server already holds.

**Safety annotations.** Each verb carries `readOnlyHint` and `destructiveHint`,
derived from what the handler does, not asserted loosely. Read-only verbs never
mutate the running session; destructive verbs can replace or tear down a
generation. The table names both.

**Rejections carry their fix.** A rejected candidate returns a structured
diagnostic - `code`, `guarantee`, `expected`/`actual`, and, since roadmap item
286, a `fix` field: the exact one-line rewrite that satisfies the guarantee,
beside the guarantee itself. This comes from `revl.diagnostics.classify()` and
flows through `revl_check`, `revl_gauntlet`, `revl_edit`, and every other verb
that admits, so an agent gets rule + call-chain + fix in one response, never a
second round-trip. A rejected candidate never deploys: the compile runs before
the transition, so the running system keeps serving.

## The verb set at a glance

| verb | read-only | destructive | required inputs |
|---|---|---|---|
| `revl_check` | yes | no | - (source) |
| `revl_admit` | yes | no | `manifest` |
| `revl_plan` | yes | no | - (source) |
| `revl_ship` | no | yes | - (source) |
| `revl_audit` | yes | no | - (source) |
| `revl_tools` | yes | no | - (source) |
| `revl_load` | no | no | - (source) |
| `revl_call` | no | no | `key`, `method` |
| `revl_swap` | no | yes | - (source) |
| `revl_edit` | no | yes | `edits` |
| `revl_gauntlet` | yes | no | - (source) |
| `revl_quarantine` | yes | no | - (source) |
| `revl_repair` | no | yes | `component` |
| `revl_rollback` | no | yes | - |
| `revl_undo` | no | yes | - |
| `revl_unload` | no | yes | - |
| `revl_commit` | yes | no | - |
| `revl_commit_confirm` | no | yes | `hash` |
| `revl_abort` | no | yes | - |
| `revl_state` | yes | no | - |
| `revl_lease` | no | no | `component` |
| `revl_snapshot` | yes | no | - |
| `revl_restore` | no | no | `snapshot` |
| `revl_timeline` | yes | no | - |
| `revl_inspect_step` | yes | no | `at` |
| `revl_step_back` | no | yes | `to` |
| `revl_replay_bisect` | yes | no | `assert` |
| `revl_replay_forward` | no | yes | `from` |
| `revl_grammar` | yes | no | - |
| `revl_resolve` | yes | no | `need` |
| `revl_canary` | yes | no | `realm` |
| `revl_query_emitters` | yes | no | `target` (source) |
| `revl_query_withdraw` | yes | no | `component` (source) |
| `revl_query_dependents` | yes | no | `target` (source) |
| `revl_query_reach` | yes | no | `component` (source) |
| `revl_query_drift` | yes | no | `service` (source) |
| `revl_live_query` | yes | no | `verb` |
| `revl_history_emitted_between` | yes | no | `from`, `to` |
| `revl_history_lifetime` | yes | no | `component` |

---

## Author and admit

### `revl_check`

Compile a component. Returns the composition summary, the G8 boundary, and
`holes` (open typed-hole obligations, each with file, line, expected type and
message) on success, or structured diagnostics (code, guarantee,
expected/actual, `fix` hint) on rejection. A draft with holes compiles; it is
refused at admission until every hole is filled.

- Inputs: `source` / `files` / `modules`.

### `revl_admit`

Check a candidate against a RUNNING composition (the admission gate): ambient
services are in scope, G2/G3 span both, and interface drift is refused. Use
before hot-swapping generated code into a live system.

- Inputs: `source` / `files` / `modules`; `manifest` (the compiled IR of the
  running composition, required); `replacing` (components withdrawn in this
  admission).

### `revl_plan`

Dry run for admission: what a swap WOULD do, without doing it. Reports
provisions gained and withdrawn, which running components divert or reactivate,
the LIFO teardown order, how the composition's irreversible reach (G8) changes,
and any interface drift. A rejected candidate is explained, not thrown.
Defaults to the composition the server has loaded, so it is the rehearsal for
`revl_swap`.

- Inputs: `source` / `files` / `modules`; `manifest` (omit to plan against the
  loaded session, or against nothing for a cold start); `replacing`.

### `revl_ship`

One intent, one call: fuse check → admit → plan into a single early-exit
request instead of three round-trips (the token-saving ship path,
[token-economy.md](token-economy.md)). Runs the stages in order and STOPS at
the first that fails, returning one consolidated result with a per-stage verdict
and `stoppedAt`. The running manifest defaults to the loaded composition. Pass
`apply: true` to also hot-swap the candidate in once all stages pass, collapsing
check → admit → plan → swap into this one call; without it nothing is mutated.

- Inputs: `source` / `files` / `modules`; `manifest`; `replacing`; `apply`
  (default false, the read-only rehearsal).
- Destructive when `apply: true`.

### `revl_audit`

The G8 boundary surface of a composition: which emissions each component can
perform, the capabilities each may cross (`*` = unscoped), which are
compensated, its iteration boundaries, and the host code it reaches.

- Inputs: `source` / `files` / `modules`.

### `revl_tools`

Project a composition's provided services to MCP tool definitions whose
behavioural annotations are derived from the compiler rather than asserted by an
author.

- Inputs: `source` / `files` / `modules`; `composition` (tool-name prefix).

### `revl_grammar`

The revl surface syntax and the rules that reject code - small enough to keep in
context while generating. No inputs.

---

## Drive a live session

### `revl_load`

Boot a composition IN MEMORY and hold it live. Nothing is written to disk, so a
draft component can be run and tested before it exists as a file. Returns fiber
states, provided keys, and the lifecycle trace.

- Inputs: `source` / `files` / `modules`; `config` (per-component config
  tables); `record` (record the effect accumulator so the composition can be
  stepped backwards - must be set at load, recording is installed before
  activation).

### `revl_call`

Invoke a provided service operation on the running composition - how you test a
component you just loaded. Returns the result and the trace it produced.

- Inputs: `key` (provided key, required), `method` (operation name, required),
  `args` (positional arguments).

### `revl_state`

What is loaded right now: fiber states, provided keys, whether a rollback is
available, and the trace since the last call. No inputs.

### `revl_swap`

Admit a candidate against the RUNNING composition and hot-swap it in. A rejected
candidate leaves the running system untouched; this is the acting half of
`revl_admit`. Called with NO source (`source`/`files`/`modules`), it re-admits
the source the server already holds - so an agent that edited server-side with
`revl_edit` need not re-serialize the whole file.

- Inputs: `source` / `files` / `modules` (all optional); `replacing`.

### `revl_edit`

Patch the SERVER-SIDE source of the running composition and re-admit - deltas,
not documents. Each edit is one of: `{hole, expr}` (fill the typed hole on that
source line, pairs with `revl_check`'s fill spec), `{range: [start, end],
replacement}` (replace a character span), or `{anchor, replacement}` (replace a
literal snippet, no offsets). Re-admission runs the SAME gate as `revl_swap`: a
patch that breaks a guarantee is refused with its diagnostic and the running
system is untouched. A clean patch is hot-swapped in; one that still has open
holes advances the server-side source but swaps nothing. Returns the admission
verdict / holes / diagnostic, never the whole source.

- Inputs: `edits` (array, required - each `{hole, expr}` / `{range,
  replacement}` / `{anchor, replacement, count?}`); `target` (which server-side
  buffer to edit, omit for the main inline source); `replacing`.

### `revl_unload`

Tear the composition down and report the residue checks (registry, provisions,
effects, listeners) - prove a component leaves nothing behind before you commit
it to disk. No inputs. Under the session commit protocol a plain unload is the
implicit terminal commit (witnessed mutations discharge); the explicit,
WAL-marked path is `revl_commit_confirm` / `revl_abort` below.

### The session commit protocol (`revl_commit`, `revl_commit_confirm`, `revl_abort`)

A session is one driver lifetime (from `revl_load` to a commit or abort). Its
actions split three ways by their checked extern classification, and nothing at
runtime can move an action between classes
([245-session-commit.md](design/245-session-commit.md), Decision 2):

- **(a) witnessed-revertible** — an `effect` over a `witnessed` extern (item
  243). Runs freely mid-session; its declared inverse is registered on the
  activation frame. On commit it discharges (the mutation persists); on abort it
  replays (the mutation reverts).
- **(b) deferrable** — an `emission` extern declared `deferred`
  (`extern emission deferred fn send(...)`). The call does NOT fire the host
  body; it ENQUEUES a descriptor on the session's deferral queue and returns
  Unit. On commit the queue flushes FIFO (each host body fires once); on abort
  the queue is dropped and nothing fires. Because nothing crossed, the abort is
  exact by construction. A deferred emission must return Unit, may not declare
  `compensate` or `async`, and may not appear in a teardown slot (all checked).
- **(c) immediate** — a plain `emission` extern. Fires at the call. On abort it
  has already crossed; `compensate` (item 247) may offset it.

The commit is two-step and hash-bound. `revl_commit` derives its gate target
from OWNER-held state - the deferral queue, the discharge escrow, and the live
activation-frame registry - never the runtime's per-call current frame, so a
session with three live components commits all three or none. It returns the
manifest: `summary` (the one-line prompt, e.g. "empty trash: 3 files; send: 1
email"), `deferred` (the queue), `witnessed` (the count about to discharge), and
a `hash` binding exactly that target. `revl_commit_confirm(hash)` recomputes the
hash; if the queue or the live composition drifted since enumeration (another
enqueue, a swap) the hashes differ and the confirm is REFUSED with a fresh
manifest (`ok:false`) - what fires is exactly what was approved, never a
superset. On approval the durable WAL record order is `commit-approved` (before
the first fire), then one `flushed` per fired emission, then the one `discharge`
covering every witnessed seq, then `activation-complete`. `revl_abort` marks
every live frame aborting before any teardown, drops the queue, replays the
witnessed inverses, and proves a clean world.

- `revl_commit`: no inputs; returns `{manifest}`.
- `revl_commit_confirm`: `hash` (from `revl_commit`); returns the flush and
  residue report, or `ok:false` with a fresh manifest on a stale hash.
- `revl_abort`: no inputs; returns the replayed seqs and the dropped-deferral
  count.

Both `revl_commit_confirm` and `revl_abort` (and enumeration) are gated by the
operator `commit` verb ([operator-capabilities.md](operator-capabilities.md)).

Recovery honors the approved-to-discharged window: a crash after
`commit-approved` and before `discharge` is a COMMITTED session on `revl
recover` - it replays no witnessed inverse, rolls the missing discharge forward,
and reports any approved-but-unflushed emission as owed (never auto-fired). The
absence of `commit-approved` is the abort verdict, and deferred emissions with
no approval are reported "dropped, never fired", counted clean.

Slice status: the py runtime, driver, MCP verbs, and recovery are implemented.
The refuse-at-emit tier gate for `deferred` on the rust/go/java/wasm/typescript
tiers is owed by Slice 2 (the guard and its canonical diagnostic already live in
`revl.session_commit.refuse_deferred_on_ownerless_tier`, ready to wire into each
backend's emit refusal channel).

### `revl_rollback`

Restore the generation that was running before the last swap. No inputs.

### `revl_undo`

Return to an earlier generation through the retained generation history - the
deep version of `revl_rollback`. With no `to`, undoes to generation N−1; `to`
names any still-retained generation. The undo is itself an ADMITTED, gated
change: the target's sources are re-admitted through the same compile+admission
gate a swap runs, so a target the current checker rejects is refused
(`ok:false`, with the diagnostic) and the running composition is untouched. The
dossier rides along: what unloads, what state drops, and the interim boundary
crossings that no undo can un-emit ([generation-history.md](generation-history.md)).

- Inputs: `to` (a retained generation number; omit for N−1).

### `revl_lease`

Claim, renew, or release an operator-scoped, TTL-bound LEASE on a component
NAME - the multi-agent workspace primitive. A lease is NOT a lock: the running
component keeps serving every call. It governs who may REPLACE it while you
iterate. By default a swap that would replace someone else's leased component is
WARNED at plan/swap but proceeds; under a boundary policy that declares `leases
enforced` (item 33) that swap is REFUSED at admission. Leases expire on their
TTL, so a walked-away agent never wedges the workspace
([component-leases.md](component-leases.md)).

- Inputs: `component` (required); `action` (`claim` / `renew` / `release`,
  default `claim`); `ttl` (seconds, default 300).

### `revl_snapshot`

Capture the running composition as re-admittable JSON: the SOURCES of the
currently-admitted components plus the manifest and meta. Not a runtime dump -
it is the inputs a fresh boot needs to put the same composition back through the
admission gate, so self-evolution survives a restart. Pair with `revl_restore`.
No inputs.

### `revl_restore`

Re-admit a snapshot (from `revl_snapshot`) into an empty session by REPLAYING
ADMISSION: the sources are recompiled through the same gate a live `revl_load`
runs, never rehydrated from the stored manifest. A component the current checker
rejects fails the restore loudly with its diagnostic. Requires nothing loaded.

- Inputs: `snapshot` (a `revl_snapshot` document, required).

---

## Grade and prove a candidate

### `revl_gauntlet`

Grade a candidate instead of merely admitting it: run a battery in an ISOLATED
scratch session the live composition never sees, and return a structured verdict
dossier. It separates what was PROVED (admission, derived teardown), what was
TESTED with counts (a real boot/unload no-residue lifecycle), and what remains
CLAIMED (the enumerated G8 extern boundary). A rejected or faulting candidate is
graded, not thrown. `ok` reports that a dossier was produced; the grade is in
`verdict` (`admissible` | `rejected`) ([gauntlet.md](gauntlet.md)).

- Inputs: `source` / `files` / `modules`; `config`; `replacing`.

### `revl_quarantine`

Quarantine an UNTRUSTED candidate before it may touch a hosted tier: grade it
with the gauntlet, then compile it to a STANDARD wasm component and run its
lifecycle + fault battery in wasmtime's component-model SANDBOX, where a fault
that would escape on a hosted tier is a TRAP the runtime catches. `verdict` is
`passed` (proved itself, eligible for admission), `trapped` (contained, host
untouched, not eligible), `rejected` (admission refused, never reached the
substrate), `deferred` (no Str-surface function) or `unavailable`
(wasm-tools/wasmtime absent). It also reports the policy admission decision
(`admission`): under `quarantine required`, a candidate is admissible only after
it passes - unless the session's operator holds `quarantine-bypass` authority
([quarantine-tier.md](quarantine-tier.md)).

- Inputs: `source` / `files` / `modules`; `service` (WIT interface name);
  `config`; `replacing`.

### `revl_repair`

The repair loop (roadmap item 62): a faulting component fixes itself, within
policy. Give the fault's causal trace and a regenerated `candidate` (or a `need`
for the reuse check to find an existing fix), and the loop runs unattended:
gauntlet → boundary policy → capability-widening ack → hot-swap, authorized by
the SELF-REPAIR POLICY. A candidate that would WIDEN what the composition
reaches PAUSES for a human ack (`awaiting-ack`) instead of swapping; an
ineligible component halts. Returns the INCIDENT DOSSIER reconstructed from the
causal trace ([repair-loop.md](repair-loop.md)).

- Inputs: `component` (required); `trace` (item-27 lifecycle events) or
  `traceFile` (a JSONL path); `predicate` (a bisect predicate, needs a session
  loaded with `record: true`); `candidate` (`{source|files|modules}`); `need`
  (a `service` decl, a fill spec, or a shape object); `selfRepairPolicy` (a
  dict `{eligible, mayTouch, ackOnWiden}` or DSL text; absent = closed);
  `accept` (widening ack tokens); `apply` (default true; false runs every gate
  but does not swap); `registry` (dir for the reuse check).

### `revl_canary`

Progressive delivery with a derived rollback: run a successor generation on ONE
designated slice (a realm) while the baseline serves the rest, and decide on
evidence. Returns the DIVERGENCE (a replay comparison of the two recorded
worlds, attributed to the exact component/realm that produced the first
differing step), the REVERT proof (the derived LIFO teardown with the exact
`survivors` set proving the other N-1 tenants keep every provision, plus the R4
no-residue proof), and, with `promoteTo`, the PROMOTE verdict. It DECIDES;
`revl_swap` acts. Stateless canary only ([verified-canary.md](verified-canary.md)).

- Inputs: `realm` (required); `baseline` (source) or `baselineFiles`;
  `candidate` (source) or `candidateFiles` (paths, required to also report the
  promote verdict); `provider` (when the realm serves several); `promoteTo`
  (backend tier for the promote verdict); `proveResidue` (default true).

### `revl_resolve`

Find a component to IMPORT instead of regenerating one. Give the NEED - a
`service` declaration, a hole's fill spec (verbatim from `revl_check`), or a
service shape object - and it returns ranked candidates whose provided service
is §5-compatible with the need, each carrying its SOURCE and MANIFEST inline so
the next call is `revl_admit` / `revl_swap`. Matching is admission, never text:
the same structural-compatibility gate a hot-swap runs, pointed at the index.
Pass `manifest` to upgrade the answer from "compatible somewhere" to
"admissible here". Ranking is least-authority-first
([registry.md](registry.md)).

- Inputs: `need` (required); `manifest` (the running composition's IR);
  `limit` (max candidates, default 5); `registry` (dir, default
  `$REVL_REGISTRY` or the repo's `registry/`).

---

## Walk a recorded run

These read the effect accumulator recorded by `revl_load` with `record: true`.

### `revl_timeline`

The recorded effect accumulator: every effect step in order, the inverse
registered for it, and every emission - marked as the one kind of step that has
no inverse.

- Inputs: `component` (omit for all).

### `revl_inspect_step`

What the composition looks like at step k: which provisions are active, which
inverses are still accumulated (newest first), which have already run, and the
emissions at or before k.

- Inputs: `at` (step index, required; `-1` means before every step);
  `component`.

### `revl_step_back`

Unwind the accumulator to step k by running the registered inverses from the top
down, newest first - leaving the component LIVE, not torn down. Refuses if the
range crosses an emission with no `compensate`; `force` crosses anyway and
reports what was crossed. The guarantee is "the inverses ran in order", never
"state was restored".

- Inputs: `to` (required; `-1` unwinds everything); `component`; `force`.

### `revl_replay_bisect`

git-bisect for an execution: binary-search the recorded timeline for the FIRST
step at which `assert` flips, in log2(N) evaluations instead of N. `assert` is a
predicate expression over the same inspect view (names like `activeProvisions`,
`emissionsSoFar`, `accumulated`, `step`). Read-only: it reconstructs each probed
step. Returns the found step's full record.

- Inputs: `assert` (predicate expression, required); `component`.

### `revl_replay_forward`

Re-run the tail after step k by re-invoking the service calls that produced it -
how you re-test after a fix. Activation-body steps are reported as not
replayable rather than faked.

- Inputs: `from` (replay steps after this one, required); `component`.

---

## Query the composition

The five static query verbs answer over source; two of them are proofs over the
linked graph and three are may-analyses, and each result says which via
`precision`, `precisionNote`, and `assumptions` ([queries.md](queries.md)).

### `revl_query_emitters`

WHO EMITS TO X? Every component and provide-method whose irreversible reach
includes a provision key, a `key.method`, a service or an extern - including
transitively through pure `fn` calls and across the service seam.

- Inputs: `source` / `files` / `modules`; `target` (required).

### `revl_query_withdraw`

WHAT BREAKS IF I WITHDRAW C? The reactive cascade - components that inject a
provision C provides, then their dependents, with the LIFO teardown order and
the keys that stop being provided. EXACT.

- Inputs: `source` / `files` / `modules`; `component` (required).

### `revl_query_dependents`

WHO DEPENDS ON THIS? For a provision key or service: the provider, every
consumer, which operations each consumer calls (and which are emissions), and
the realm each resolution happens in. EXACT.

- Inputs: `source` / `files` / `modules`; `target` (required).

### `revl_query_reach`

WHAT DOES C REACH? The transitive boundary surface of one component: its
emissions, host code, iteration boundaries, and everything it reaches through
injected providers. Over-approximate, and it says so - `complete: false` plus
`unresolvedInjections` marks a key nothing in this IR provides.

- Inputs: `source` / `files` / `modules`; `component` (required).

### `revl_query_drift`

WHAT CHANGES IF A SERVICE GAINS OR LOSES A METHOD? Interface drift: which
providers must implement or drop it, and which call sites stop resolving. With
no `gains`/`loses` it reports the current per-method provider and call-site map.
EXACT for the compiled composition.

- Inputs: `source` / `files` / `modules`; `service` (required); `gains`
  (methods added), `loses` (methods removed).

### `revl_live_query`

THE QUERY SURFACE, ANSWERED AGAINST THE LIVE SESSION. Runs one of the five verbs
against the composition CURRENTLY LOADED - the generation as it stands after
every `revl_swap`, not a static IR. The result is the same envelope with `mode:
live`, plus a `live` block for what only the runtime knows (which provisions are
actually SERVED right now, so a key whose provider drifted to inactive reads as
absent in `live.notServedNow`). Requires a loaded session.

- Inputs: `verb` (required, one of `emits-to` | `withdraw` | `depends-on` |
  `reaches` | `drift`); `target`, `component`, `service`, `gains`, `loses` (as
  the chosen verb needs).

### `revl_history_emitted_between`

WHICH EMISSIONS CROSSED BETWEEN STEPS X AND Y? A windowed read of a RECORDED
run's effect timeline. Each hit is a real crossing the runtime performed in
`[from, to]`, not a reachable site. EXACT for the recorded world; `mode:
historical`.

- Inputs: `from` (required), `to` (required); `component`; `timeline` (a replay
  recording JSON).

### `revl_history_lifetime`

EVERYTHING THIS COMPONENT TOUCHED DURING ITS LIFE. The recorded counterpart of
`revl_query_reach`: the effects and emissions the component ACTUALLY produced on
a recorded run, bounded by item 27's lifecycle trace. `mode: historical`, EXACT
for that run.

- Inputs: `component` (required); `trace` or `traceFile` (a `revl run --trace`
  JSONL, for the lifecycle); `timeline`.

---

## See also

- [commands-reference.md](commands-reference.md) - every `revl` CLI subcommand.
- [mcp-bridge.md](mcp-bridge.md) - the full shapes and the service ⇄ tool
  projection behind `revl mcp schema` / `revl_tools`.
- [guide-ai-agents.md](guide-ai-agents.md) - the agent loop these verbs
  compose into.
- [operator-capabilities.md](operator-capabilities.md) - scoping which
  management verbs a bound session may reach.
