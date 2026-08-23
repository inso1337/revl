# Token-surface audit — where the protocol-side spend goes (roadmap item 50)

*Analysis, not a change.* [`tokens-to-green.md`](tokens-to-green.md) measures the
**generation side**: the ~149 output tokens a model emits to get one component
admitted. That is the small half. The large, currently-unmeasured half is the
**protocol side** — the tokens an agent spends *driving the MCP surface* to ship
that component: re-sending source it already sent, and walking chatty verb
sequences that take three or four round-trips to express one intent.

This document ranks those protocol-side findings so the metric can later
adjudicate which of item 50's three optimizations (compound MCP verbs, a terser
wire-form, `revl_edit` structured patches) actually pay. It changes no code:
`src/revl/mcp/*` is read-only for this pass. Every finding cites
`src/revl/mcp/server.py` at the sha it was audited against (`8ffb390`).

The house principle it measures against — from the item — is **"agents pass
names, not contents"**: a verb that makes the agent re-serialize source, a
running manifest, or a whole composition is spending tokens on bytes the session
already holds.

---

## Ranking

Ranked by estimated tokens-per-occurrence × how central the verb is to the
ship-a-component loop. "Est. tokens" uses the same BPE-proxy as the metric
(`bench/tokens.py`), applied to the *arguments* an agent must serialize.

| # | finding | verb(s) | kind | est. tokens/occurrence | frequency |
|--:|---|---|---|--:|---|
| 1 | swap re-sends the **whole composition source** | `revl_swap` | resend-full-source | grows with the *system*, not the change — hundreds–thousands | every hot-swap |
| 2 | admit round-trips the **running manifest** back in | `revl_admit` | resend-contents | the full IR document — often larger than the source | every pre-swap check |
| 3 | check → plan → admit → swap = **four round-trips** for one intent | `revl_check`, `revl_plan`, `revl_admit`, `revl_swap` | chatty-verb-sequence | 4× the source (re-sent each call) | every ship |
| 4 | restore replays the **entire snapshot source set** | `revl_restore` | resend-full-source (justified) | all sources | every cold restore |
| 5 | audit / tools / gauntlet each re-take `source`/`files` | `revl_audit`, `revl_tools`, `revl_gauntlet` | resend-contents | one source each | per inspection |

---

## The findings in detail

### 1 — `revl_swap` re-sends the whole composition source (top resend violation)

`_tool_swap` (server.py:139) already holds the running composition in `SESSION`.
To swap, it first admits the candidate against `SESSION.ir` — then, on
admission, **recompiles the entire composition from re-sent source**:

> server.py:156–160 — *"admitted: recompile the whole composition so the swap is
> a full generation … `_compile(arguments.get("source"), arguments.get("files"))`"*

and the rejection path spells the requirement out: *"pass the full source set to
swap"* (server.py:166–167). So the agent must re-serialize every component in the
system to change one. This is the textbook "documents, not deltas" cost the
item's `revl_edit` bullet targets: the tokens scale with the size of the
*running system*, not the size of the *edit*. It is the single highest-value
place a delta/patch verb would pay, and its price grows precisely as
self-evolving compositions get larger.

### 2 — `revl_admit` round-trips the running manifest back into the call

`revl_admit` **requires** `manifest`, described as *"the compiled IR of the
running composition"* (server.py:316–323, 486–491). The agent must therefore
carry the running IR in its own context and re-send it on every admission check.
That IR is frequently *larger* than the source it is checking. Note that
`revl_plan` already fixed exactly this: it *defaults to the loaded session* when
no manifest is passed (server.py:363–369, *"the agent does not have to
round-trip the running IR through its context"*). `revl_admit` and `revl_swap`
did not inherit that fix — so the checking half still pays the round-trip the
rehearsal half was spared. The asymmetry is the finding.

### 3 — the chatty ship-a-component sequence: check → plan → admit → swap

Four verbs express one intent ("ship this component into the running system"):

- `revl_check` — compile + holes (server.py:301).
- `revl_plan` — dry-run the delta (server.py:351); *"the rehearsal for
  revl_swap with the identical arguments"* (server.py:357–360).
- `revl_admit` — gate against the running composition (server.py:315).
- `revl_swap` — admit **again** internally, then apply (server.py:139).

Two redundancies stand out. First, `revl_swap` *repeats the admission compile*
`revl_admit` just did (server.py:145–148 mirrors `_tool_admit`'s
`_compile(..., manifest=...)`), so a cautious agent that calls admit-then-swap
pays for the same admission twice. Second, `revl_plan` is documented as taking
the *identical arguments* as the swap it rehearses — so plan-then-swap re-sends
the same source twice. A single compound verb ("admit-and-swap, returning the
plan it would have shown") collapses all four into one round-trip. Whether it
pays, and by how much, is exactly what tokens-to-green (re-run before/after on
this corpus) is there to decide.

### 4 — `revl_restore` replays the full snapshot (a *justified* resend)

`revl_restore` takes the entire snapshot `{sources, manifest, meta}` and
re-admits every source (server.py:661–668). This *is* a full-source transfer,
but it is the honest cost of replaying admission from cold on a fresh session —
there is no live state to diff against. Listed for completeness; not a target.

### 5 — `revl_audit` / `revl_tools` / `revl_gauntlet` each re-take source

Each inspection verb (`_tool_audit` 381, `_tool_tools` 394, `_tool_gauntlet`
194) takes `source`/`files` afresh via `_SOURCE_INPUT` (server.py:451–461). An
agent inspecting a component it *just checked* re-sends it. Lower value than
1–3 because inspection is off the hot ship-loop, but it is the same
name-vs-contents leak: none of these can name an already-checked draft.

---

## Cross-reference with the item-38 demand ranking

Item 38 (`bench/demand.py`) ranks **what agents retry** (refusals); this audit
ranks **what retries cost** (tokens). They compose: a cell that is both
frequently refused *and* expensive per attempt is where the protocol tax bites
hardest.

| corpus cell | demand signal (item 38) | token signal (this pass) | joint reading |
|---|---|---|---|
| `29-mesh` (v1/v2/v2host) | rank-2 syntax-form demand — the `kv` form, 9 refusals | 3 of the top-6 costliest admitted cells (237–244 tokens) | high-retry **and** high-cost — every `kv` retry re-sends a large source; a delta verb + the terser wire-form both pay here first |
| `G4/emission-propagation` | rank-1 demand — 53 refusals across the corpus | each refusal is a full re-send under check→…→swap | the single biggest retry driver × the full-resend cost = the largest aggregate protocol spend; fixing the *sequence* (finding 3) compounds with any G4 diagnostic improvement |
| `02-pg-pool/v2host`, `13-provider-consumer-pair/v2host` | multi-attempt (2–3 iters) | #1 and #2 costliest cells (457, 421 tokens) | cost is dominated by *re-sent retries*, not first-draft size — finding 1/2 (send deltas, not documents) is the lever |

**Reading:** demand says *fix G4's diagnostics and the `kv` syntax gap so agents
retry less*; this audit says *even the retries that remain should not re-send the
world*. The two roadmaps are additive — one lowers the retry count, the other
lowers the per-retry price.

---

## What the metric will adjudicate next

Each item-50 optimization maps to a finding above, and each has to move
tokens-to-green (re-run `bench/tokens.py` before/after on this same corpus) to
ship — "no trick ships on taste":

- **compound MCP verbs / one-intent-one-call** → findings 2 & 3 (kill the
  round-trips and the manifest re-send; give admit the session default plan
  already has).
- **terse authoring wire-form** (expanded through the item-35 formatter under
  the IR-identical gate) → the generation-side ~149 tokens in
  `tokens-to-green.md`.
- **`revl_edit` structured patches / deltas-not-documents** → finding 1 (swap
  sends the change, not the whole composition).

Where the winners eventually land is the "How you're measured" contract in
`docs/guide-ai-agents.md`; this audit is the map of where to dig, and
`tokens-to-green.md` is the scale that says whether the digging paid.
