# The evolve loop: propose/regenerate over the Gate (Path A, roadmap item 148)

**Status:** the fuller Path-A slice on top of item 144's Gate service. Item 144
made revl's admission gate reachable as a bridge service — one-shot: a candidate
is admitted, or refused with a why-trace. **evolve** is the loop that turns a
refusal into a next attempt: admit, on refusal hand the structured why-trace to
a `propose` step, retry to a budget. The proposer is handed one bounded
`ProposerPacket`, every proposal call records its digest, and **rounds** restart
the loop from the last admitted component with the earlier proposals dropped.
`src/revl/evolve_loop.py` is the orchestration; `examples/evolve_loop.rvl`
declares the seam in revl.

## The problem

Admission (`docs/gate-as-a-service.md`) is a verdict, not a repair. A generator
that emits a candidate composition wants the loop: offer it, read *why* the gate
said no, revise, offer again — bounded, so a candidate that never converges is
given up on rather than retried forever. That loop needs three things the
one-shot gate does not provide:

1. a **machine-readable refusal** — not the human why-trace string, but the
   violated guarantee as a key a generator can branch on, plus the offending
   subject/call-path and the mapped fix;
2. a **propose seam** — where the (external) regeneration plugs in;
3. a **budget + attempt trace** — the termination rule and the history a
   give-up returns.

## Scope: `propose` is an extern, not an LLM

The actual code generation is **external** — an agent/LLM in the automorph
harness, built separately. This module owns the *orchestration and the
agent-readable payload*, not the generator. The `propose` step is a plain
callable seam the harness fills; the loop has no LLM dependency of its own. A
trivial deterministic in-repo proposer (`scripted_proposer`) stands in so the
loop is proven end to end in tests.

## Control flow

`evolve(candidate, proposer, budget, brief="")` (`src/revl/evolve_loop.py`):

```text
current = candidate
notes = []
for n in 1 .. budget:
    verdict = Gate.admit_structured(current.sources, current.manifest)
    record attempt n (candidate, verdict)
    if verdict.ok:
        return EvolveResult(admitted=True, attempts, final_rejection=None)
    why_trace = rejection_payload(verdict.rejection)
    if n < budget:                      # regenerate only if an attempt remains
        packet = ProposerPacket(brief, current, why_trace,
                                notes[-MAX_PACKET_NOTES:])
        attempt.packet_digest = packet.digest()
        current = proposer(packet)
        notes.append(refusal_note(n, why_trace))
return EvolveResult(admitted=False, attempts, final_rejection=why_trace)
```

- **budget** is the max number of *admit attempts* (not proposes). `budget >= 1`.
  With `budget=2`: attempt 1 refused → propose → attempt 2. `propose` is never
  spent after the final attempt.
- Terminates on the **first admit** (success, with the full history) or on
  **budget exhaustion** (give-up: `admitted=False`, the whole attempt history,
  and the final why-trace).
- The **running manifest is fixed** across the loop — the running world does not
  change; only the candidate's sources evolve.
- The loop reaches the gate through `gate_service.admit_structured`, so it
  inherits item 144's guarantees exactly — evolve *orchestrates* the gate, it
  does not re-implement admission.

## The proposer packet

Every proposal call receives one `ProposerPacket`, never a bare candidate or an
unbounded transcript:

```text
ProposerPacket(
  brief: str,                  # the original task text, unchanged
  candidate: Candidate,        # the parent candidate to start from
  last_refusal: dict | None,   # the refusal that ended the last attempt
  notes: tuple[str, ...],      # capped summaries of earlier refusals
)
```

| field | meaning | bound |
| --- | --- | --- |
| `brief` | the task text the caller supplied to `evolve`/`rounds` | carried unchanged, never accumulated |
| `candidate` | the parent candidate: the current attempt for a repair, or the retained component for a round seed | one candidate |
| `last_refusal` | the `rejection_payload` of the refusal that triggered the proposal, or `None` for a round seed | one payload |
| `notes` | one-line `refusal_note` summaries of earlier refusals in the same round | at most `MAX_PACKET_NOTES` (8), each at most `MAX_NOTE_CHARS` (160) |

The packet **never** holds the discarded attempt history (the full `Attempt`
list stays on `EvolveResult` for audit), earlier candidates or their sources,
the proposer's own conversation, or any authority. It is deliberately smaller
than the record so a fresh proposer cannot re-anchor on its own earlier edits.

`ProposerPacket.canonical_json()` is the canonical form (sorted keys, compact
separators, UTF-8) and `digest()` is its SHA-256. Every refused attempt that
hands a packet to `propose` records that digest as `Attempt.packet_digest`, so a
later reader can check exactly what the proposer was shown. An admitted attempt,
and a final refusal with no attempt left to spend, record `None`: nothing was
handed over. `packet.candidate_digest` is the content hash of the parent
candidate, so a round packet can be checked against the component the previous
round retained.

## The propose seam

```text
propose(packet: ProposerPacket) -> Candidate
```

- `packet`: the bounded input above: brief, parent candidate, last refusal,
  capped notes.
- returns the revised candidate to admit next. A proposer that cannot improve
  the candidate returns `packet.candidate` unchanged; the budget still bounds
  the loop, so a stuck proposer ends in exhaustion, never an infinite loop.

The harness installs its real generator once, at startup:

```text
evolve_loop.register_proposer(my_generator)   # then evolve() needs no proposer=
```

`evolve(candidate, proposer=…)` overrides the registered default (that is how
the tests inject `scripted_proposer`). Across the service seam the same seam is
`host_propose` in `examples/evolve_loop.rvl`, whose `@py` body is
`evolve_loop.propose_bridge` (JSON-string in, JSON-string out — value-typed, so
`service Evolve` is transport-safe for the same reason `Gate.admit` is). That
transport is `(candidate, why-trace)` strings, so the bridge reconstructs a
packet with an empty brief and no notes; a caller that needs the brief or the
notes uses `evolve`/`rounds` in-process.

## The structured-rejection payload contract

This is the `last_refusal` the packet carries and what a generator branches on.
It is `diagnostics.classify`'s agent-facing record (already the projection of a
`RevlError`), augmented by the gate with the mapped `fix`, then normalized by
`rejection_payload` into stable generator-facing keys:

```json
{
  "g_rule":    "G2",
  "guarantee": "provision disjointness: one provider per key (per realm)",
  "category":  "guarantee",
  "subject":   "thing",
  "component": "Dup",
  "call_path": [
    {"name": "Base", "kind": "provider", "file": ".../base.rvl", "line": 2, "detail": "provides `thing`"},
    {"name": "Dup",  "kind": "provider", "file": ".../dup.rvl",  "line": 1, "detail": "provides `thing`"}
  ],
  "fix":       "one provider per key per realm — withdraw one component, or `isolate` …",
  "file":      ".../base.rvl",
  "line":      1,
  "message":   "provision conflict: key `thing` is provided by both Base and Dup (G2)"
}
```

| key | meaning | source |
| --- | --- | --- |
| `g_rule` | violated guarantee as a machine key (`G2`, `G4`, `A1`, …) — the primary branch key | `classify.code` |
| `guarantee` | one-line human description of that guarantee | `diagnostics.GUARANTEES` |
| `subject` | the offending key/type/name (the clashing provision key for G2) | `why.subject` |
| `call_path` | the why-trace steps: the providers / cycle edges / emission chain that produced the verdict | `why.steps` |
| `component` | best-effort single offending component (the last named step) | derived |
| `fix` | the mapped one-line rewrite | `diagnostics.FIXES` |
| `file` / `line` / `message` | source location and the verbatim compiler message | `classify` |

The payload is **total**: an unclassified refusal still yields `g_rule` `"REVL"`
and an empty `call_path` rather than raising, so a generator never has to guard
against a missing shape. Because `g_rule` is the same machine key the whole
compiler uses (`docs/…`, `diagnostics.GUARANTEES`/`FIXES`), a proposer can carry
one dispatch table across every guarantee (G1–G8, A1–A8, T1–T3), not just G2.

`gate_service.admit_structured` is the gate entry point that produces this: it
is `admit`'s sibling, returning the same `{ok, diagnostic, admitted, manifest}`
verdict **plus** a `rejection` field (the `classify` record + `fix`, or `null`
on an admit). Item 144's `admit` is left byte-for-byte unchanged.

## The attempt trace

`EvolveResult` carries the whole run:

```text
EvolveResult(
  admitted: bool,
  attempts: [ Attempt(n, candidate, admitted, diagnostic, rejection,
                      packet_digest), … ],
  final_rejection: <payload> | None,   # set only on give-up
)
```

`result.to_json()` is the log a harness records — every candidate offered, in
order, each with its verdict, (on refusal) the payload that drove the next
propose, and (when a packet was handed over) `packet_digest`, the hash of what
the proposer saw. `final_candidate` is the last candidate offered (the admitted
one on success).

## Rounds: a fresh start from the retained component

`rounds(candidate, brief, proposer, budget, max_rounds)`
(`src/revl/evolve_loop.py`) wraps `evolve` in bounded fresh-context rounds:

```text
current = candidate
for r in 1 .. max_rounds:
    packet = ProposerPacket(brief, current, last_refusal=None, notes=())
    first = proposer(packet)             # fresh context for this round
    result = evolve(first, proposer, budget, brief=brief)
    record Round(n=r, packet, packet.digest(), result)
    if not result.admitted:
        return RoundsResult(admitted=False, rounds, result.final_rejection)
    current = result.final_candidate     # keep only the last admitted candidate
return RoundsResult(admitted=True, rounds)
```

Each round ends at a retained, admitted component; the next round starts from a
seed packet built from that component and the original brief, with no refusal
text and no notes from the previous round. The previous round's attempts are
not lost: they stay in `Round.result.attempts` for audit. Two rounds therefore
have different seed-packet digests whenever a round changed the component, and
the round-two packet contains no refusal from round one.

**Forgetting frees context, never authority.** A restart drops the proposer's
memory of earlier attempts; it never changes what the retained component may
do. Authority lives in the admitted component's declared capabilities and
effects, which travel with the component and are re-checked by the Gate on
every admission, so a restart can neither lose nor gain any. If the restarted
proposer repeats a dead end from an earlier round, that dead end is still in
`Round.result.attempts`: the record makes it visible even though the packet
does not carry it.

`budget` (per round) and `max_rounds` are fixed before the run starts. A round
that exhausts its budget ends the whole run with `admitted=False` and that
round's `final_rejection`; `RoundsResult.final_candidate` then names the last
component a round retained, or `None` when no round admitted.
`RoundsResult.to_json()` records each round's seed packet and digest beside its
attempt history.

## Worked proof (the tests)

`tests/test_evolve_loop.py` pins the loop against the item-144 fixtures:

- **repair on attempt 2** — a candidate that double-provides key `thing` is
  refused `G2` on attempt 1; the scripted `g2_key_bump_proposer` reads
  `subject == "thing"` and rewrites the provision to `thing_v2`; attempt 2 is
  admitted. The history has exactly two attempts and `final_rejection is None`.
- **budget exhaustion** — a no-op proposer (matches no `g_rule`) never repairs;
  with `budget=3` the loop spends all three attempts and gives up with
  `admitted=False`, the full three-attempt history, and a `final_rejection`
  still carrying `g_rule == "G2"` and the `["Base", "Dup"]` call path.
- **the packet digest**: a recording proposer sees, on its only call, exactly
  the round-one candidate and its refusal payload; `Attempt.packet_digest`
  equals the digest of that packet. The admitted attempt records `None`.
- **capped notes**: across a longer run the packet's `notes` grow one summary
  per earlier refusal and stop at `MAX_PACKET_NOTES`.
- **rounds**: two rounds produce different seed-packet digests; the round-two
  packet has no refusal text from round one, and carries the kept component's
  `candidate_digest` and the original brief.
- **bounded rounds**: a failed round ends the run and keeps the last retained
  component; `budget` and `max_rounds` are validated and fixed before the run.
- **probe ordering**: a candidate with a newly refused row, or a row with a
  coverage gap, is deferred and is never offered to the Gate; a clean row only
  orders, so a clean probe with a refusing Gate is still refused; one result
  carries every offered candidate's attempts and the deferred candidates; the
  budget is shared across the ordered list
  (`tests/test_2806_counterfactual_probe.py`).

## Probe ordering (item 2806, Slice 2)

A counterfactual probe (`revl replay WAL --under POLICY --probe --candidate F`,
item 467) gives the loop one row per candidate: how many recorded crossings that
candidate would newly refuse, the first dangerous crossing, and the bounds the
record carries. The probe is evidence only, so the loop uses it in one direction:

- `probe_order(candidates, probe)` calls `probe(candidate)` once per candidate and
  returns `{"ordered", "deferred", "rows"}`. `rows` is positional, one
  `{"candidate", "row"}` entry per candidate in the order given, so two equal
  candidates keep two rows. The identity is `probe_identity(candidate)`, the
  same hash (`counterfactual.sources_digest`) the CLI probe row carries as
  `candidate` for the same files; it covers the sources only, because the probe
  never reads the running manifest. A candidate whose row names a first
  dangerous crossing, whose `newlyRefused` is above zero, or whose row has a
  coverage gap (it regraded fewer crossings than the record holds, or the record
  holds none) is deferred; every other candidate keeps its relative order. An
  absent or not-answerable row is not adverse: silence must not become a
  deferral.
- `evolve_ordered(candidates, probe, budget=...)` is the loop that consumes it.
  Deferred candidates are not offered to the Gate; every ordered candidate goes
  through `evolve` unchanged, so the Gate is the only admission. A clean probe
  row never admits a candidate, and a clean row with a refusing Gate is still
  refused.
- `budget` is one budget of admit attempts shared across the whole ordered
  list, not a budget per candidate. Each candidate's run is capped so every
  later ordered candidate keeps at least one attempt; candidates the budget does
  not reach are listed in `unoffered`.
- It returns one `EvolveResult` for the run: every offered candidate's attempts
  concatenated in order (`Attempt.n` counts across the run), the deferred
  candidates with their probe rows (`deferred`), and `all_deferred`, which is
  true when the probe deferred every candidate. That outcome has no attempts,
  like an empty candidate list, and `all_deferred` is how a caller tells them
  apart.
- The module imports only the pure identity and coverage helpers from
  `revl.counterfactual`: the caller supplies the `probe` callable, so a live WAL
  read stays at the edge.

Slice 3 (a two-arm continue-versus-reset experiment from a fork point) waits on
the live replay executor in issue 2749 and the re-run seam in issue 2713.

## Harness coordination

This is the mechanism the automorph harness's `evolve` wires to:

1. the harness calls `evolve_loop.register_proposer(generator)` once, where
   `generator(packet) -> candidate` is its LLM-backed regenerator;
2. it drives `evolve_loop.evolve(candidate, budget=…)` per candidate, or
   `evolve_loop.rounds(candidate, brief, budget=…, max_rounds=…)` for a
   multi-round run (or `evolve_bridge`/the `Evolve` service across a seam),
   reading `EvolveResult`/`RoundsResult`;
3. on refusal the harness's generator receives the packet; it branches on
   `packet.last_refusal.g_rule`, reads `subject`/`call_path` to locate the
   offending code, and uses `fix` as the rewrite hint.

The seam is deliberately narrow (one callable taking one bounded packet,
value-typed at the service boundary) so the generator and the loop evolve
independently: the harness owns *what* a revision is, this module owns *when*
one is asked for and *what* bounded input it is handed.

## Trade-offs and what a fuller loop still needs

1. **The manifest is fixed.** evolve admits successive candidates against one
   running world. `rounds` threads the retained *candidate* forward, not the
   admitted *manifest*. A loop that also evolves the running composition (admit
   a candidate, then admit the *next* against the world the previous one joined)
   is a further slice: it needs the admitted manifest threaded forward as the
   next attempt's `manifest`, which `EvolveResult.final_candidate` already
   carries the material for.
2. **The proposer is trusted.** The loop re-admits whatever `propose` returns,
   so a malicious proposer cannot slip anything past the gate (every attempt is
   re-admitted), but it *can* burn the budget. The budget is the only bound on a
   bad generator today; a fuller loop might also detect non-progress (the same
   `g_rule`+`subject` recurring) and give up early.
3. **The notes are summaries.** A packet carries capped one-line summaries of
   earlier refusals, not their full payloads. The full history stays in the
   attempt record; a proposer that needs the detail of an old dead end has to be
   given it by the harness, deliberately, not implicitly by the loop.
4. **`admit_structured` is py-tier.** Like item 144's gate it runs on the py
   process; a cross-tier `Evolve` service reaches it as a bridge proxy exactly
   as `docs/gate-as-a-service.md` describes, and inherits the same
   deadline/withdrawal bound.
