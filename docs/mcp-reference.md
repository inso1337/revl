# revl MCP verb reference

Every verb the `revl mcp serve` server advertises, its inputs, and what it
returns. This is the complete set, verified against `src/revl/mcp/server.py`
(the `TOOLS` registry and its handlers) and `src/revl/mcp/query_tools.py` (the
query verbs appended to it).

<!-- docgen:mcp-verb-count begin -->
The advertised list is exactly the 51 verbs below, one section each.
<!-- docgen:mcp-verb-count end -->

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

<!-- docgen:mcp-verbs begin -->
| verb | read-only | destructive | required inputs |
|---|---|---|---|
| `revl_check` | yes | no | - (source) |
| `revl_admit` | yes | no | `manifest` (source) |
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
| `revl_estop` | no | yes | - |
| `revl_estop_report` | yes | no | - |
| `revl_fork` | yes | no | `at` |
| `revl_fork_confirm` | no | yes | `hash` |
| `revl_approve` | no | no | - |
| `revl_revoke` | no | no | - |
| `revl_distillation_offers` | yes | no | - |
| `revl_apply_distillation` | no | no | `offerId` |
| `revl_revoke_distillation` | no | no | `rule` |
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
| `revl_scaffold` | yes | no | `service` |
| `revl_fmt` | yes | no | `source` |
| `revl_explain` | yes | no | `code` |
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
<!-- docgen:mcp-verbs end -->

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

### `revl_scaffold`

Generate a typed, holed component skeleton from a spec and return it WITH every
open hole's `fillSpec` in one call: expected type, capability bound, in-scope
bindings, reachable services (the same shape `revl_check` adds to `holes`). The
scaffold-first flow is `revl_scaffold` then fill each hole then
`revl_check`/`revl_admit`, instead of generating a whole component and
repairing structural errors after the fact ([scaffold.md](scaffold.md)).

- Inputs: `service` (required); `provides`, `component`, `requires`,
  `capabilities`, `methods`, `emits`, `config`, `resource`, `effect`,
  `filename`.

### `revl_fmt`

Canonically format inline source, or with `migrate: true` rewrite 1.x `$`
interpolation to 2.0 backtick templates. The MCP twin of `revl fmt`: text in,
text out, nothing touches disk. The rewrite is proven against the same
IR-equivalence gate the CLI runs, so `admitted: false` means the rewrite would
change what the compiler sees and `formatted` is NOT returned ([fmt.md](fmt.md)).

- Inputs: `source` (required); `filename`; `migrate`.

### `revl_explain`

What a diagnostic code means and how to fix it, the MCP twin of `revl explain`.
The other half of a structured `revl_check` / `revl_admit` rejection, which
already carries the code. An unknown code answers with the roster of known ones
rather than with nothing.

- Inputs: `code` (required; a diagnostic code such as `G4`, case-insensitive).

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
tiers is now wired: each backend's `emit()` calls
`revl.session_commit.refuse_deferred_on_ownerless_tier` and re-raises the single
canonical diagnostic through its own `EmitError`, so a CALL to a `deferred`
extern on any of those five tiers is refused at emit time rather than fired or
silently dropped. The refusal is call-site keyed: a `deferred` extern that is
declared but never called still emits cleanly. Targeting the python tier (the
lone session owner) emits the call normally.

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

## Approve, halt and fork

### `revl_approve`

Say YES to an outstanding class-(c) crossing. When the approval policy is on, a
`revl_call` (or a load/swap whose activation body emits) that reaches an
IRREVERSIBLE emission with no checked inverse does not fire: it returns
`approvalRequired` with a `ticket`. Relay that ticket to a human, then call this
with the ticket's `hash` to mint a standing, single-use, hash-bound approval;
the IDENTICAL re-issue then fires once and consumes it. A hash the server never
issued is refused, and a swap or edit that changes the call's reach closure
invalidates a standing approval. For a repeat-shaped session, pass `capability`
and/or `uses`/`ttlMs` INSTEAD of a bare hash to mint a session-scoped standing
grant, so n prompts become one. Gated by the `approve` operator verb: who may
say yes is scoped in the same profile grammar as who may commit. Class (a)
(witnessed-revertible) and class (b) (deferred) crossings never reach here.

- Inputs: `hash`; `capability`; `uses`; `ttlMs`.

### `revl_revoke`

Retire a session-scoped standing grant early, the symmetric partner of
`revl_approve`'s standing grant. Effective immediately and mid-session: the
NEXT class-(c) crossing the grant would have covered prompts again
(fail-closed). Target the SAME key the grant was minted against: `capability`
revokes every live grant for it, `requestId` revokes one. Revoking a key with no
live grant is a clean no-op (`count: 0`), never an error, so a double revoke or
a stale id is harmless. Gated by the `approve` operator verb: withdrawing
consent is the same authority as granting it.

- Inputs: `capability`; `requestId`.

### `revl_distillation_offers`

Fold this session's approval ledger to candidate distilled auto-approve rules.
Distillation notices that the same operator keeps saying yes to the same SHAPE
of crossing (resource-scoped capability, realm, taint origins) and writes down
the `AutoApproveRule` that would have said yes for them, a rule an operator
could have typed and that is checked on the same runtime path. Read-only and
PROPOSE-ONLY: it applies no policy, and it is scoped to the caller's own
attributed grants. Each offer carries its rule text, its blast radius (the past
prompts it would have covered, the destinations seen, and the taint origins it
can NEVER admit), the attributed operator, and the sessions it came from. No
inputs.

### `revl_apply_distillation`

Install a distilled offer as a live `AutoApproveRule`. Writes the rule into the
bound policy and records a `distillation-applied` WAL fact with its attribution
(who the repeated yeses came from, who reviewed it, the ledger window, the
time). The rule is bound to the component set it was reviewed against: a
component later ENTERING its glob that was not in that set suspends the rule and
re-offers, fail-closed. Gated by the `approve` operator verb.

- Inputs: `offerId` (required; from `revl_distillation_offers`).

### `revl_revoke_distillation`

Retire an applied distilled rule from the live policy, the symmetric partner of
`revl_apply_distillation`. Removes the rule (matched by its canonical DSL text),
records a `distillation-revoked` WAL fact, and the NEXT matching crossing
prompts again. Consume-before-fire already covers an in-flight crossing, so
there is no orphaned auto-approval mid-revoke. Revoking a rule with no live
match is a clean no-op (`count: 0`). Gated by the `approve` operator verb.

- Inputs: `rule` (required; the rule text or its canonical DSL).

### `revl_estop`

The operator's emergency halt. STOP DISPATCHING new boundary crossings
immediately, run NOTHING, and report what was in flight. This is NOT
`revl_abort`: abort is a verdict on the work and pays for a full two-phase LIFO
unwind, while an e-stop pays for a latch flip. The price is stated, not hidden.
Every registered entry is left STRANDED (owed, never discharged) and every
acquired handle stays held, so the report says what was NOT unwound. The
instance is dead afterwards: there is no resume, and the way back is `revl
recover --wal FILE`. Held as an operator authority (verb `estop`) precisely so a
composition or an agent cannot invoke it on itself.

- Inputs: `reason`; `operator` (defaults to the session's bound operator).

### `revl_estop_report`

Read the e-stop inventory back WITHOUT touching the world: what was in flight
and therefore AMBIGUOUS (at most one crossing, outcome unknown), and what was
stranded, meaning registered, never unwound, still owed. Read-only, and never
`clean`: an e-stop leaves residue by design. No inputs.

### `revl_fork`

Step 1 of the two-step session fork: ENUMERATE what forking at step k would
rewind and what it cannot. Walks the whole tail above k into an honest, total
partition: the host-confined witnessed effects and provisions that WILL be
rewound, the held deferred sends that WILL be dropped, the emissions that
already CROSSED the boundary and cannot be undone, the outbound-scoped inverses
that WOULD cross on rewind (enumerated, never fired), and any step the recorder
cannot restore. Returns a `hash` binding the rewound span. Nothing is rewound
yet. Refuses a fork whose tail carries an opaque step, a non-idempotent inverse,
or a committed boundary below k.

- Inputs: `at` (required; the step k, `-1` rewinds the whole tail);
  `component`.

### `revl_fork_confirm`

Step 2 of the session fork: PERFORM the fork the hash from `revl_fork` bound.
Re-derives the hash and refuses on any drift, returning a fresh report rather
than an error. On match it runs the scope-gated, non-emitting rewind to k
(host-confined inverses only), drops the parent deferral queue, FREEZES the
parent (retired at k, non-callable), snapshots the step-k state, and mints the
branch (fresh session id and WAL, no approval carry). The branch is then the
only live continuation over the shared workspace. The result carries `lineage`:
what the branch inherited (composition, generation, IR and source digests,
capability surface, WAL position) and, listed explicitly, what it did NOT
(provider versions, seeds and clock, model decisions). The same lineage is
written durably into the branch's own WAL, so `revl branch` and `revl compare`
read the branch tree back after the process is gone.

- Inputs: `hash` (required; from `revl_fork`).

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
