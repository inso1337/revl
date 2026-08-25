# The evolve loop: propose/regenerate over the Gate (Path A, roadmap item 148)

**Status:** the fuller Path-A slice on top of item 144's Gate service. Item 144
made revl's admission gate reachable as a bridge service — one-shot: a candidate
is admitted, or refused with a why-trace. **evolve** is the loop that turns a
refusal into a next attempt: admit, on refusal hand the structured why-trace to
a `propose` step, retry to a budget. `src/revl/evolve_loop.py` is the
orchestration; `examples/evolve_loop.rvl` declares the seam in revl.

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

`evolve(candidate, proposer, budget)` (`src/revl/evolve_loop.py`):

```text
current = candidate
for n in 1 .. budget:
    verdict = Gate.admit_structured(current.sources, current.manifest)
    record attempt n (candidate, verdict)
    if verdict.ok:
        return EvolveResult(admitted=True, attempts, final_rejection=None)
    why_trace = rejection_payload(verdict.rejection)
    if n < budget:                      # regenerate only if an attempt remains
        current = proposer(current, why_trace)
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

## The propose seam

```text
propose(candidate: Candidate, why_trace: dict) -> Candidate
```

- `candidate` — `Candidate(sources={path: source}, manifest=<running IR JSON>)`.
- `why_trace` — the rejection payload below.
- returns the revised candidate to admit next. A proposer that cannot improve
  the candidate returns it unchanged; the budget still bounds the loop, so a
  stuck proposer ends in exhaustion, never an infinite loop.

The harness installs its real generator once, at startup:

```text
evolve_loop.register_proposer(my_generator)   # then evolve() needs no proposer=
```

`evolve(candidate, proposer=…)` overrides the registered default (that is how
the tests inject `scripted_proposer`). Across the service seam the same seam is
`host_propose` in `examples/evolve_loop.rvl`, whose `@py` body is
`evolve_loop.propose_bridge` (JSON-string in, JSON-string out — value-typed, so
`service Evolve` is transport-safe for the same reason `Gate.admit` is).

## The structured-rejection payload contract

This is what `propose` receives and what a generator branches on. It is
`diagnostics.classify`'s agent-facing record (already the projection of a
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
  attempts: [ Attempt(n, candidate, admitted, diagnostic, rejection), … ],
  final_rejection: <payload> | None,   # set only on give-up
)
```

`result.to_json()` is the log a harness records — every candidate offered, in
order, each with its verdict and (on refusal) the payload that drove the next
propose. `final_candidate` is the last candidate offered (the admitted one on
success).

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

## Harness coordination

This is the mechanism the automorph harness's `evolve` wires to:

1. the harness calls `evolve_loop.register_proposer(generator)` once, where
   `generator(candidate, why_trace) -> candidate` is its LLM-backed regenerator;
2. it drives `evolve_loop.evolve(candidate, budget=…)` per candidate (or
   `evolve_bridge`/the `Evolve` service across a seam), reading `EvolveResult`;
3. on refusal the harness's generator receives the payload above — it branches
   on `g_rule`, reads `subject`/`call_path` to locate the offending code, and
   uses `fix` as the rewrite hint.

The seam is deliberately narrow (one callable, value-typed at the service
boundary) so the generator and the loop evolve independently: the harness owns
*what* a revision is, this module owns *when* one is asked for and *what
refusal-shape* it is handed.

## Trade-offs and what a fuller loop still needs

1. **The manifest is fixed.** evolve admits successive candidates against one
   running world. A loop that also evolves the running composition (admit a
   candidate, then admit the *next* against the world the previous one joined)
   is a further slice — it needs the admitted manifest threaded forward as the
   next attempt's `manifest`, which `EvolveResult.final_candidate` already
   carries the material for.
2. **The proposer is trusted.** The loop re-admits whatever `propose` returns,
   so a malicious proposer cannot slip anything past the gate (every attempt is
   re-admitted), but it *can* burn the budget. The budget is the only bound on a
   bad generator today; a fuller loop might also detect non-progress (the same
   `g_rule`+`subject` recurring) and give up early.
3. **`admit_structured` is py-tier.** Like item 144's gate it runs on the py
   process; a cross-tier `Evolve` service reaches it as a bridge proxy exactly
   as `docs/gate-as-a-service.md` describes, and inherits the same
   deadline/withdrawal bound.
