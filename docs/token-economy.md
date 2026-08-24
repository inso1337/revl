# The token economy — make revl the cheapest language for an agent to write

**Status:** metric + one compound verb implemented (roadmap item 50).
- **Metric:** `bench/tokens.py`, committed snapshot `bench/results/tokens-to-green.md`,
  pinned by `tests/test_tokens_to_green.py`.
- **Compound verb:** `revl_ship` in `src/revl/mcp/ship.py`, registered in
  `src/revl/mcp/server.py`, pinned by `tests/test_mcp_ship.py`.
- **Deferred this wave:** the terse authoring form (needs the parser — owned by
  item 53). See the last section.

The house rule for this item is **measure first, optimize second**: no
token-saving change ships until the number it moves is already recorded, so the
saving is a demonstrated delta and not a story.

---

## 1. The metric — tokens-to-green

Iterations-to-green (`bench/run.py`) counts *turns* to a compiling component; it
does not count *spend*. A two-iteration component whose attempts are twice as
long as another's costs the same in turns and twice as much in tokens.
**Tokens-to-green** (`bench/tokens.py`) is that missing number: the output
tokens a model emits over its committed attempts to get **one component
admitted**, scored with a deterministic BPE-shaped proxy (`count_tokens`) and,
where a paid run recorded exact usage, the recorded figure verbatim.

Committed snapshot (`bench/results/tokens-to-green.md`): across **136** admitted
components, **mean 149 est. output tokens-to-green** (median 130). The costliest
cells are ranked in that file so optimization has a target. Every later
optimization on this item has to move *this* number before it ships.

There are two halves to the spend, and the metric names both:

- **Generation side** — the tokens the model emits to write the component.
  That is what tokens-to-green measures directly, and it is the *small* half.
- **Protocol side** — the tokens the agent spends *driving the MCP surface* to
  ship that component. `bench/results/token-surface-audit.md` ranks these:
  re-sending source the session already holds, and walking chatty verb
  sequences that take three or four round-trips to express one intent. That is
  the large, and until now unmeasured, half — and it is what the compound verb
  below attacks.

---

## 2. One intent, one call — `revl_ship`

The audit's **finding #3** measures the ship-a-component loop. An agent that has
generated a component walks four verbs to land it:

```
revl_check  ->  revl_admit  ->  revl_plan  ->  revl_swap
```

That is four schema+result exchanges for a single intent — *ship this* — and,
because each call re-serialises the candidate (and `revl_admit` additionally
round-trips the running manifest back in), roughly **4× the source in tokens**.

`revl_ship` fuses the chain into one call:

- **Read-only rehearsal (default):** `check -> admit -> plan` in a single
  request, returning a per-stage verdict and the delta the swap would produce.
- **`apply: true`:** extends the chain with the hot-swap, so the whole
  `check -> admit -> plan -> swap` loop is one call.

### Early-exit — no wasted work

The stages run in order and **stop at the first that fails**, exactly like the
gauntlet (`docs/gauntlet.md`) that this shape follows:

| stage stops | meaning | later stages |
|---|---|---|
| `check` | the candidate does not compile, or has open typed holes | never run |
| `admit` | compiles, but is not admissible against the running composition | plan/swap never run |
| `plan`  | admissible, but the swap delta could not be produced | swap never run |
| `swap`  | (`apply` only) planned, but the swap did not apply | — |

The consolidated result carries one `stages` list, a `stoppedAt` field naming
the first failure (or `null` on full success), and the failing stage's own
payload (diagnostic / holes) merged up — so the agent learns *how far it got and
why it stopped* from a single exchange, and a candidate that does not compile is
never admitted, one that is not admissible is never planned, and nothing is
swapped unless every prior stage passed.

### Two token wins, both structural

1. **Round-trips collapse** from up to four to one. `roundTrips.saved` reports
   the difference for the stages actually run (e.g. `{fused: 1, wouldHaveBeen: 3,
   saved: 2}` for a passing rehearsal).
2. **The running manifest is not re-sent.** Like `revl_plan`, the admit and plan
   stages default their `manifest` to the composition the server already holds
   (`SESSION.ir`) when the agent does not pass one — so the agent never
   round-trips the running IR through its own context just to admit against it.
   The `against` field names what was used (`"session"` / `"manifest"`).

### Result shape (rehearsal, success)

```jsonc
{
  "ok": true,
  "shipped": false,              // true only when apply actually swapped it in
  "stoppedAt": null,             // or "check" / "admit" / "plan" / "swap"
  "against": "session",          // manifest source: session | manifest | null
  "stages": [
    {"stage": "check", "ok": true, "holes": 0},
    {"stage": "admit", "ok": true, "against": "session"},
    {"stage": "plan",  "ok": true}
  ],
  "roundTrips": {"fused": 1, "wouldHaveBeen": 3, "saved": 2},
  "summary": { "loadOrder": [...], "components": [...], "services": [...] },
  "boundary": { ... },           // the G8 surface, from the admit stage
  "plan": { ... },               // the delta, without a second revl_plan call
  "note": "checked, admitted and planned in one call — pass `apply: true` ..."
}
```

### Design notes

- The orchestration lives in `src/revl/mcp/ship.py` and is free of `server`
  imports: it receives the primitive stage handlers (`_tool_check`,
  `_tool_admit`, `_tool_plan`, `_tool_swap`) as callables. This keeps it a pure
  function a test can drive with fakes (`test_orchestration_*`), and keeps the
  additions to `server.py` a thin wiring handler plus one `TOOLS` entry — a
  single new verb, not a rewrite of the surface.
- Annotations: `revl_ship` advertises `readOnlyHint: false, destructiveHint:
  true` — it is *capable* of mutation via `apply`, even though it defaults to a
  read-only rehearsal, so an agent is never surprised by a mutation.

---

## 3. Deferred this wave — the terse authoring form

The item's third optimization is a **terse authoring form** that an agent writes
in fewer tokens and the toolchain **self-provingly expands** to the full,
checked source. It is deferred here because it needs the parser/lowering
frontend (`src/revl/parser.py`, `lower.py`), which roadmap item 53 owns this
wave. When that lands, the terse form drops in beside the two pieces above, and
tokens-to-green — already recorded — is the number that will say whether it
actually pays.
