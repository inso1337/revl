# Composition queries — ask the audit, don't just read it

**Status:** implemented — `revl query {emits-to,withdraw,depends-on,reaches,drift}`,
`src/revl/query.py`, MCP tools `revl_query_*` (`src/revl/mcp/query_tools.py`).
Tests: `tests/test_query.py` over `tests/fixtures/query_mesh.rvl`. Three time
modes (§9): the same verbs and envelope answered against the static IR, the
live session (post-swap), or a recorded run — `revl_live_query`,
`revl_history_*`, `revl query {emitted-between,touched}`, tests in
`tests/test_query_modes.py`.

`revl audit` prints a composition's manifest and its G8 boundary surface. That
is the right shape for a review and the wrong shape for a refactor. An author
— or an agent — editing a live system has a **question**, not a browsing need:

| the question | subcommand | function |
|---|---|---|
| who emits to `db`? | `emits-to` | `query.emitters` |
| what breaks if I withdraw `PgDatabase`? | `withdraw` | `query.withdrawal` |
| who depends on `cache` / on `Database`? | `depends-on` | `query.dependents` |
| what does `UserCache` reach? | `reaches` | `query.reach` |
| what if `Database` loses `execute`? | `drift` | `query.drift` |

```bash
revl query emits-to  host_write  tests/fixtures/query_mesh.rvl
revl query withdraw  Journal     tests/fixtures/query_mesh.rvl --json
revl query drift     Kv          tests/fixtures/query_mesh.rvl --gains del --loses set
```

## 1. Machine first, human second

The MCP tools are the primary consumer, so the **structured result is the
product** and the CLI rendering is a courtesy view of the same object.
`--json` prints the result verbatim; so does `structuredContent` on the MCP
side. Nothing is reshaped on the way out, and no information exists only in
the rendering.

Every result carries the same envelope:

```json
{
  "ok": true,
  "query": "emitters",
  "question": "who emits to `host_write`?",
  "precision": "over-approximation",
  "precisionNote": "…what that means for this query…",
  "assumptions": ["…what would have to be false for this to be wrong…"]
}
```

A miss is `{"ok": false, "error": "unknown component: 'Ghost'", "known": [...]}`
— an agent that guessed a name gets the real ones back instead of a stack
trace. The CLI exits non-zero; the MCP result sets `isError`.

## 2. Precision — a proof or a guess?

Several of these queries are conservative over-approximations. **That is
stated in the payload, not only here**, because an agent acting on a result
will not have read this file. Two values:

- **`exact`** — read off a graph the linker already resolved. The list is
  complete *and* minimal: nothing listed is spurious, nothing missing.
- **`over-approximation`** — a *may*-analysis. Everything listed is reachable
  on some path; nothing reachable is omitted. It is a superset of what any
  single activation actually does.

`assumptions` names what the answer rests on. Three recur:

- **opaque externs** — an `extern` body is verbatim host code. The compiler
  enforces how a classification is *used*, not that it is truthful, so a
  `pure` extern whose host body writes to S3 is invisible to every query here.
  This is the one direction in which the analysis can *under*-report, and it
  is a property of the boundary, not of the query.
- **hot swap** — a call across the service seam lands in the body of whoever
  provides that key *right now*. `revl_swap` changes the answer.
- **scope** — only components in this IR are considered. Ambient components of
  a running composition that were not compiled in are not visible.

| query | precision | why |
|---|---|---|
| `emits-to` | over-approximation | may-analysis over call sites |
| `withdraw` | **exact** | G2 + G3 resolve the graph |
| `depends-on` | **exact** | `requires`/`provides` are declarations (G1) |
| `reaches` | over-approximation | may-analysis, and see §6 |
| `drift` | **exact** | providers and call sites are enumerable |

## 3. `emits-to` — who emits to X?

Every component and provide-method whose **irreversible** reach includes the
target, where the target is a provision key (`ledger`), a `key.method`
(`ledger.append`), a service name (`Ledger`) or an extern (`host_write`).
Every reading that matches something is used, and `resolved` says which — key,
service and extern names live in different namespaces and can collide.

Reach is followed two ways:

- **through the pure stratum** — `say` calls `shout` calls `host_write`. The
  fn call graph is *not* re-walked here: `lower._emitting_fns` is the
  checker's own least fixed point (the one G4 uses to force `emission` into a
  service declaration) and `__main__._extern_reachability` names which externs
  a fn reaches. Consuming both means this query cannot disagree with the gate
  that rejects code. The fn that got there is reported as `reaches.through`.
- **across the service seam** — a call on an injected key lands in the
  provider's provide-method, whose own reach is folded in. `path` is the hop
  chain (`["rep.publish", "kv.set", "ledger.append"]`) and `distance` its
  length; `direct: true` means the emission is in this scope's own body.

A component splits into **scopes**: its activation body, and one scope per
provide-method. They have different lifetimes, and an answer that cannot tell
"this component emits when it loads" from "this method emits when you call it"
is not worth much. `provide` is a top-level-only statement, so the split is a
partition, not a heuristic.

### Why the over-approximation is tight

A site is listed when a path exists, not when one is guaranteed — branch arms,
`match` arms and arrow bodies handed to builtins all count as reachable. What
it *cannot* do is miss one: G4 makes a service declaration an upper bound on
its providers, so a method not declared `emission` provably reaches none. The
traversal never has to guess whether a plain `fn` is hiding an emission — the
compiler already refused that program. This `guarantee` string ships in the
result.

Teardown-position emissions count: `compensate` and `undo` expressions are
walked, because calling the thing schedules them. `reaches.compensated` says
whether the site's emission carries a compensation.

## 4. `withdraw` — what breaks if I withdraw C?

The reactive cascade. Components that inject a provision C provides, then
*their* dependents, transitively; each with the `depth` it sits at, the
`lostKeys` that stop resolving, and the provider whose removal took them out.

- `withdrawalOrder` is LIFO — reverse `loadOrder`, restricted to the affected
  set. That is the order the runtime tears them down in: consumers first, so a
  dependent never outlives the provision it is still calling.
- `orphanedKeys` is every `(key, realm)` that stops being provided, including
  keys orphaned two levels down.
- `survivors` is the complement, so "did this touch the other tenant?" is a
  set membership test rather than a diff.

**Exact.** G2 makes each `(key, realm)` provision unique, so a lost provision
has no alternative supplier — there is no "maybe it resolves elsewhere". G3
makes the graph acyclic, so the walk terminates. Realms are honoured: in
`examples/tenants.rvl`, withdrawing `TenantAStore` cascades to `TenantAApp`
and leaves the `tenant_b` pair untouched.

> If `src/revl/plan.py` (the `revl plan` dry run) lands in the same tree, its
> cascade analysis answers a neighbouring question — reconcile the two rather
> than keeping both walkers. It was not present when this was written.

## 5. `depends-on` — who depends on service S / key k?

Per `(key, realm)`: the provider, whether it `resolved` at all, and every
consumer with `methodsCalled` — which operations it actually references and
which of those are emissions — plus its `intercept` metadata (Def. 30) for
that key. Given a service name, every key typed with it, in every realm.

**Exact.** `requires`/`provides` are declared in the component header (G1) and
resolved by the linker; the consumer set is a lookup, not an inference.
`methodsCalled` is the may-set of operations named in the bodies.

## 6. `reaches` — what does C reach?

The transitive boundary surface of one component: emissions, host code split
into emission and non-emission externs, iteration boundaries (`await`), and
compensated emissions — for its activation body *and* every provide-method,
following calls across the seam into the provider bodies they land in.
`reachedComponents` is who it touches; `providers` is who it injects from.

Two deliberate calls:

- **Provider activation bodies are not folded in.** A provider's own `effect`
  and `emit` steps are its surface, reached by *loading* it, not by calling
  it. Ask `reaches` about the provider for those.
- **`complete: false` marks the blind spot.** A key nothing in this IR
  provides is a dynamic boundary: whatever answers it at runtime has a surface
  we cannot see. `unresolvedInjections` names the keys and an extra
  `assumptions` entry says the result **under**-reports for them. This is the
  only case where a query is knowingly incomplete, and it says so in the
  payload rather than presenting a clean surface.

## 7. `drift` — what changes if S gains/loses a method?

With no `--gains`/`--loses`, the current shape: per method, whether it is an
`emission`, which providers implement it, and every call site. With them:

- **gain** — every provider of the service must grow an implementation; the
  compiler refuses a `provide` block that does not cover the declaration.
  Existing call sites are unaffected.
- **loss** — `providersMustDrop` are the providers implementing it and
  `callSites` are the calls that stop resolving. Both are rejected at the
  admission gate.

`impacted` unions the two, which is the set you feed to `revl_admit`.

**Exact** — providers are declarations and call sites are syntactic call
nodes, both enumerable. It is a complete list of what the admission gate would
flag, *not* a promise the edit is safe once those sites are fixed: a signature
change that still type-checks can still be wrong.

## 8. MCP

`revl_query_emitters`, `revl_query_withdraw`, `revl_query_dependents`,
`revl_query_reach`, `revl_query_drift` — all `readOnlyHint: true`, all
accepting `source` (inline, never written to disk), `files` or `modules`
exactly as the rest of the bridge does, so an agent can query a composition
that has never existed as a file. The tool descriptions lead with the question
in capitals, because the selection problem an agent has is "which of these
answers what I want to know".

The pairing that matters: `revl_query_withdraw` before `revl_swap`, and
`revl_query_drift` before proposing a service edit. Both are the cheap
read-only half of a change the gate would otherwise refuse.

## 9. One query surface, three time modes

The five verbs and the envelope above answer against the **static IR** — the
composition as compiled from source. But two of the envelope's own stated
`assumptions` name worlds the static IR is *not*: "a hot swap changes which
body a service call lands in" and "only components in this IR are considered".
The same verbs, answered against those other worlds, are two more modes. The
verbs do not change; the envelope's `precision`/`assumptions` already say how
the worlds differ, so a result carries a `mode` and says **which world it
describes**.

```json
{ "ok": true, "query": "withdrawal", "mode": "live", ... }
```

`mode` is one of `static` (the default, §§3–7), `live`, or `historical`.

### 9.1 Live — the same verbs, against what is loaded now

A static answer is for the composition *as linked*; after a `revl_swap` it can
be stale. The **live** mode answers the same verb against the session's actual
loaded generation — the composition as it stands after every swap applied so
far. There is no new analysis: the linked graph the static verb reads is also
what the running session holds (`session.ir` is the post-swap generation), so
`withdrawal(session.ir, C)` already answers for the live world. Live mode adds
two things to the envelope:

- the static **hot-swap caveat is spent** and replaced by the live one — this
  *is* the post-swap world, and re-querying after the next swap is how you move
  to the one after it;
- a **`live` block** carrying what only the runtime knows: `generation`, and
  the provisions **served right now**. A declared key whose provider has
  drifted to an inactive state reads as absent (`live.notServedNow` on a
  `withdraw`), where the static query — which sees the graph, not the running
  fibers — would still count it.

Live mode is **session-bound**, so it lives on the MCP surface, not the
one-shot CLI (which has no persistent session):

```
revl_live_query { "verb": "withdraw", "component": "PgDatabase" }
revl_live_query { "verb": "reaches",  "component": "UserCache" }
```

`verb` is one of the five; the verb's argument is `target` (emits-to /
depends-on), `component` (withdraw / reaches) or `service` + `gains`/`loses`
(drift). Requires `revl_load`. Use it, not `revl_query_*`, once a composition
is running.

### 9.2 Historical — the same envelope, against a recorded run

The **historical** mode answers against a *recorded* run rather than a static
IR or the live session. This is the query-side of the runtime causal traces
(item 27, `docs/why-runtime.md`): the lifecycle trace (`why_runtime`, JSONL of
load/withdraw + cause) says *when* a component lived, and the recorded effect
timeline (`backends/python/replay.py`) says *what* it did while it lived. Both
formats are **reused verbatim** — nothing here invents a parallel recording.

Two questions, both `mode: historical`, both `precision: exact` (a recorded
step is a fact, not a may-analysis or a prediction):

- **which emissions crossed between steps X and Y?** — a windowed read of the
  recorded effect timeline. An emission is a one-way boundary crossing, so each
  hit is a real crossing the runtime performed in `[from, to]`, not a reachable
  site. This is the novel one: nobody can ask it of an *un*-recorded run, and a
  *verified* effect timeline is the thing that makes the window meaningful.

  ```
  revl_history_emitted_between { "from": 3, "to": 7 }
  revl query emitted-between --timeline run.json --from 3 --to 7
  ```

- **everything this component touched during its life** — the recorded
  counterpart of `reaches`. Where `reaches` is a may-analysis over the graph,
  this is the effects and emissions the component *actually* produced on the
  run, bounded by the lifecycle trace's load and withdraw (with the cause
  behind each).

  ```
  revl_history_lifetime { "component": "Store" }
  revl query touched Store --trace run.jsonl --timeline run.json
  ```

The MCP tools read the live session's own recording (`revl_load` with `record:
true`, the same recording the replay tools read) and accept an inline
`timeline`; `revl_history_lifetime` also takes a `trace`/`traceFile` for the
lifecycle span. The CLI reads files: a replay-recording JSON (`--timeline`) and
an item-27 lifecycle JSONL (`--trace`, a `revl run --trace` file).

### 9.3 What the envelope says in each mode

| mode | world | precision | the assumption that changes |
|---|---|---|---|
| `static` | the compiled IR | per verb (§2) | "a hot swap changes the answer" **applies** |
| `live` | the session, post-swap | per verb | the swap caveat is **spent**; `live.notServedNow` reconciles served-vs-declared |
| `historical` | one recorded run | **exact** | neither may nor prediction — "what happened", scoped to what was recorded |

The point is that `precision` and `assumptions` were already the fields that
distinguished these worlds, so adding modes needed no second vocabulary: a
result says which world it answers for, and an agent reading it knows whether
it holds a proof, a may-analysis, or a log.
