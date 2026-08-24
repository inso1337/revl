# `revl dash`: the supervisor's cockpit

*A read-only live view over a running session or a recorded run — the
dependency graph as it actually is, the causal trace streaming, and the pending
human decisions with their evidence attached.*

Implementation: `src/revl/dash.py` (the model builder and the text render),
`src/revl/__main__.py` (`revl dash`), `tests/test_dash.py`.

---

## 1. Why this exists

The system grew a row of human-in-the-loop gates. Item 21 (`audit --diff`) asks
a human to **acknowledge** a boundary widening before a regenerated component is
admitted. Item 33 (`policy`) refuses admission on a boundary-policy violation
and waits for a human to **rule** on it. Items 55, 61 and 62 add operator
actions, leases and interrupts — each one ends a sentence with *"and here a
human decides."*

But the human was handed a CLI and a wall of JSON. To see the graph they were
about to change, the causal chain behind a cascade, and the evidence behind a
pending ack, they had to run three or four separate commands and reassemble the
picture in their head.

`revl dash` **is** the surface those features assume. One frame, three panes,
read-only:

1. the **dependency graph** as it actually is — components, the realms they are
   isolated into, and the service seams between them;
2. the **causal trace** streaming — every lifecycle transition with the cause
   behind it (item 27), and optionally the recorded effect timeline;
3. the **pending-decisions queue** — the boundary widenings awaiting an ack
   (item 21) and the policy exceptions awaiting a call (item 33), each rendered
   *with its evidence inline*.

## 2. Read-only is the whole contract

The dash **observes; it never mutates the running system.** It holds no handle
that can swap, roll back, dispose, or apply. Everything it shows is sourced from
surfaces that already exist and are only ever read:

| pane | source (read-only) |
|------|--------------------|
| dependency graph, realms, seams | `query.Composition` (the linked provider graph) |
| served-vs-drifted, fiber states, generation | a session's `live_state()` (`mcp.session.Session`) |
| causal trace | `why_runtime.Trace` (item 27's lifecycle JSONL) |
| effect timeline | a replay recording (the `query`-side normaliser) |
| boundary-widening queue | `audit_diff.diff_crossings` / `evaluate` (item 21) |
| policy-exception queue | `policy.evaluate` and each violation's why-trace (item 33) |

`build_model` treats every input dict as immutable — it deep-copies the IR
before handing it to `Composition`, and writes back to none of its arguments.
The tests pin this two ways: nothing the model is handed is changed
(`test_build_model_mutates_no_input`), and a session stand-in that raises on any
attribute other than `ir` and `live_state()` is driven through a full snapshot
without tripping (`test_dashboard_from_session_is_read_only`).

Because the model is a cheap pure read, `--watch` is just a rebuild-and-reprint
loop on an interval — a refresh is a re-read, never a re-run.

## 3. Two ways to feed it

The dash works with or without a live runtime.

**A live session.** `Dashboard.from_session(session)` reads the session's
current `ir` and `live_state()` and colors the graph as it stands *now*: the
generation, each provision marked *served* or *drifted* (declared in the graph
but not served by a running fiber), and each component's fiber state. A later
`snapshot()` re-reads the live state, so the view tracks the composition as it
moves. From the CLI, hand it a serialized snapshot with `--live-state FILE`
(the `{generation, servedKeys, componentStates}` shape `live_state()` returns).

**A recorded trace.** With no runtime at all, `--trace run.jsonl` (a
`revl run --trace` lifecycle JSONL) renders the causal pane, and
`--timeline dump.json` (a `revl_timeline` replay recording) adds the
effect/emission detail behind it. This is the mode for a post-mortem: the run is
over, the process is gone, the trace remains.

The mode label follows the inputs: `live` when a live state is folded in,
`recorded` when only a trace/timeline is, `static` for a bare composition.

## 4. The decisions queue is the approval surface

The point of the third pane is that **the evidence is the decision**, not a
footnote to it. A supervisor should never have to leave the dash to see *why* a
choice is being asked of them.

* A **boundary widening** (item 21) is one row per added G8 crossing, decoded
  from its ack token to the component and the emission or host reach it names —
  the dossier diff the ack is a decision over. `--accept <token>` /
  `--accept-all` mark rows already acknowledged, so the queue separates *pending*
  from *decided*.
* A **policy exception** (item 33) is one row per violation, carrying the
  violation's own why-trace — the offending chain that names which component
  reaches what it may not, and how. `--policy FILE` supplies the policy;
  `--mcp-scope` marks components admitted through the MCP session for the
  policy's `mcp` sandbox.

## 5. Usage

```
revl dash <sources.rvl...> [options]

  --trace FILE         an item-27 lifecycle JSONL (revl run --trace)
  --timeline FILE      a replay recording JSON (a revl_timeline dump)
  --live-state FILE    a live-state snapshot {generation, servedKeys, componentStates}
  --against PREV.json  a previous `audit --json`; additions since it are the widening queue
  --accept CROSSING    mark one added crossing acknowledged (repeatable)
  --accept-all         mark every added crossing acknowledged
  --policy POLICY      a boundary policy; its violations are the exception queue
  --mcp-scope NAME     a component under the policy's mcp sandbox (repeatable; * = all)
  --watch              periodic-refresh loop (read-only; Ctrl-C to stop)
  --interval SECONDS   refresh interval for --watch (default 2.0)
  --no-color           plain output, no ANSI
  --json               print the structured model instead of the text view
```

Examples:

```
# just the graph (components, realms, seams)
revl dash service.rvl

# a post-mortem: the causal cascade behind a recorded run
revl dash service.rvl --trace run.jsonl --timeline run.timeline.json

# the live picture with both human gates in view
revl dash gen2.rvl --live-state now.json \
    --against gen1.audit.json --policy authority.policy
```

Machine first, human second (see `docs/queries.md` §1): `--json` emits the
structured model — `{ok, readOnly, mode, generation, graph, trace, decisions}` —
and the text render is the courtesy window onto it.
