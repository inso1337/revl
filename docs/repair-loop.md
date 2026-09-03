# The repair loop — faults that fix themselves, within policy

**Status:** v1 implemented — `run_repair` in `src/revl/mcp/repair.py`, the
`revl_repair` MCP verb in `src/revl/mcp/server.py`, the `revl repair` CLI verb
in `src/revl/__main__.py`, tested in `tests/test_repair_loop.py`. The unattended
swap needs the cordis-py runtime; everything up to it (admission, policy,
widening) is pure frontend, so it runs and is graded everywhere.

This is the capstone (roadmap item 62). Every piece it needs already landed. The
one thing missing was the *sentence that ties them together*:

> A component faults at runtime, and the system repairs itself — inside declared
> bounds, and stops for a human exactly when it would step outside them.

`repair.py` is that orchestration and nothing else. It reimplements no
machinery; it wires it. Read that as a hard rule: a real runtime defect in any
wired piece is fixed **in that piece**, never patched around here.

## The loop, one landed piece per step

| step | what it does | landed piece | file |
|---|---|---|---|
| **fault** | the *why*: the cause chain behind the component's recorded failure | causal trace, item 27 | `why_runtime.Trace.cause_chain` (`src/revl/why_runtime.py`) |
| **slice** | localize the fault to the first recorded step where a predicate flips | bisect, item 40 | `session.bisect` (`src/revl/mcp/session.py`) |
| **eligible?** | may this component self-repair, and how far may a repair reach? | **self-repair policy** (this module) | `SelfRepairPolicy` (`src/revl/mcp/repair.py`) |
| **candidate** | a regenerated component — or one the reuse check finds already built | reuse, item 49 | `registry.resolve` (`src/revl/registry.py`) |
| **gauntlet** | admission + lifecycle no-residue, graded not thrown | gauntlet, item 31 | `gauntlet.run` (`src/revl/mcp/gauntlet.py`) |
| **policy** | nothing reaches what it may not | boundary policy, item 33 | `policy.evaluate` (`src/revl/policy.py`) |
| **widen?** | a repair that WIDENS outward reach stops for a human ack | boundary-widening, item 21 | `audit_diff.evaluate` (`src/revl/audit_diff.py`) |
| **swap** | the remediation — hot-swap the proved candidate in | hot-swap, item 23 | `session.swap` (`src/revl/mcp/session.py`) |
| **authority** | who/what authorized it | operator authority, item 55 | `mcp.operator` (`src/revl/mcp/operator.py`) |

The running composition is mutated by **exactly one** call — the final
`session.swap` — and only when every gate before it was green and no
unacknowledged widening remains. Every other outcome leaves it untouched and is
reported as a *status*, never an exception (the gauntlet's discipline).

## Unattended, inside declared bounds — the self-repair policy

Item 33's boundary policy answers *"what may anything in the composition
reach?"* — a property of the composition. The repair loop needs a different
question answered: *"which components may repair themselves unattended, and how
far may a repair go before a human must weigh in?"* That is the **self-repair
policy**, this module's own contribution. It reuses item 33's realm resolution
(`policy.component_realms`) and the same `fnmatch` glob idiom, so a realm-scoped
rule means the same thing here it does everywhere.

```
component Cache*  may self-repair      # by component-name glob
realm     edge    may self-repair      # or by realm (item 33's isolation)
self-repair may touch kv, log*         # cap: which capabilities a repair may reach
self-repair may widen                  # OPTIONAL: turn the ack-on-widen rule OFF
```

or the equivalent JSON:

```json
{"eligible": [{"component": "Cache*"}, {"realm": "edge"}],
 "mayTouch": ["kv", "log*"], "ackOnWiden": true}
```

Three bounds, three behaviours:

* **eligibility** — a component no rule selects is **ineligible**: the loop
  halts (`status: ineligible`) and hands off to a human. **Closed by default** —
  with no self-repair policy, *nothing* self-repairs. Self-repair is the
  privileged direction, so its floor is closed, not open.
* **`mayTouch`** — an absolute cap on the capabilities a repair may reach. A
  candidate that reaches outside it is **refused** (`status: rejected`, reason
  `may-touch`) — not an ack, a refusal: the bound is hard. `None` means *inherit
  the running boundary* — a new capability is then caught by the gentler
  widening gate instead. An unnameable reach `*` is in-bounds only under a
  literal `*` (mirroring `policy._allowed`).
* **`ackOnWiden`** (default true) — see below.

## The human-ack interrupt (item 21)

This is the one place the unattended loop hands control back. After the
candidate passes gauntlet and policy, the loop diffs its G8 boundary surface
against the running composition's (`audit_diff.evaluate`). If the candidate
**adds** a boundary crossing — an emission scope or a reached host extern the
running composition did not have — it **WIDENS** what the composition reaches
outside the system. That is the dangerous direction (adding authority), so with
`ackOnWiden` on, the loop **pauses** (`status: awaiting-ack`) instead of
swapping. The running composition is untouched; the added crossings are named in
`verdicts.widening.unacknowledged`.

A human resolves it by acknowledging the intended crossings (`accept` on the MCP
verb / `--accept` on the CLI — the ack token is exactly the crossing string,
e.g. `host:MemCache:now_ms`). With the widening acknowledged, the same repair
proceeds to swap. This is item 21's boundary-widening rule, pointed at the
repair loop: *a regenerated component may not quietly widen its reach.*

## The incident dossier

The point of the whole exercise. `run_repair` returns a structured report that
reconstructs **every step** — fault, why, slice, candidate, gauntlet/policy/
widening verdicts, swap, authority — from the causal trace and the loop's own
inputs, so an incident can be read back end to end after the fact with no live
runtime. Shape:

```
{
  "incident":    {component, status, unattended, swapped, note},
  "fault":       {chain, root, oracle},          # item 27 (+ the 27 oracle)
  "slice":       {bisect | unavailable},          # item 40
  "eligibility": {eligible, by, note},            # self-repair policy
  "candidate":   {origin: regenerated|reused, reuse},  # item 49
  "verdicts":    {gauntlet, policy, mayTouch, widening},  # 31 / 33 / self / 21
  "remediation": {strategy, applied, swap, canaryFollowOn},  # item 23
  "authority":   {authority, unattended, operator, why},     # item 55
  "dossier":     {steps: [ {stage, roadmapItem, reached, detail} ... ]}
}
```

The `dossier.steps` list is the incident narrative: one ordered entry per stage,
each marked `reached` or not, each pinned to its roadmap item. `authority.why`
is a `revl.why` trace (item 55's idiom): *the self-repair rule → the component it
authorized.* In unattended mode the authority **is** the self-repair policy — the
loop acts *as* the eligibility rule that named the component; a bound operator
token is recorded alongside, so "on whose authority" is answerable either way.

The self-repair policy is the loop's *own* bound, not a substitute for the
session's. A repair remediates by swapping, so unless `apply: false` makes it a
rehearsal it is gated as `swap`: the bound operator must hold the `swap` grant
over the component ([operator capabilities](operator-capabilities.md)), and an
enforced lease another operator holds on that component refuses it
([component leases](component-leases.md)). The candidate is agent-supplied
source and compiles under the session's authoring trust like any other, so a
candidate `revl_check` would refuse is graded `rejected` rather than gauntleted
and swapped.

## Remediation is pluggable — the canary follow-on hook

Remediation is a `RemediationStrategy`: the loop calls `remediate(session, ir,
origin)` and records what it returns. Today it wires **`SwapRemediation`** — the
landed hot-swap (item 23), atomic (a rejected migration rolls the whole thing
back, `session.swap`'s own contract).

The **verified canary** (item 59) is the anticipated second strategy: route a
fraction of traffic to the candidate, watch it, then promote or abort. It
implements the *same* interface, so when it lands a `CanaryRemediation` is passed
to `run_repair` as a constructor argument and **the loop does not change**. This
is deliberate: item 59 is being built in parallel and is **not** a dependency of
this loop — v1 wires the swap path only, and every dossier surfaces the
`canaryFollowOn` hook (`remediation.canaryFollowOn`) so the seam is documented
where it will attach.

## Using it

MCP (`revl_repair`):

```json
{"component": "MemCache",
 "trace": [ ...item-27 causal events... ],
 "predicate": "step >= 0",
 "candidate": {"source": "...the regenerated repair..."},
 "selfRepairPolicy": {"eligible": [{"component": "MemCache"}], "ackOnWiden": true},
 "accept": []}
```

CLI:

```
revl repair app.rvl --component MemCache \
  --trace run.jsonl \
  --candidate repaired.rvl \
  --self-repair-policy self-repair.policy \
  [--predicate "step >= 0"] [--boundary-policy boundary.policy] \
  [--accept host:MemCache:now_ms] [--plan]
```

Exit status: `0` repaired (or `--plan` clean), `2` awaiting-ack (a widening
needs a human), `1` otherwise (ineligible / rejected / no candidate).

## The exit test

`tests/test_repair_loop.py::test_injected_fault_repaired_unattended` pins the
roadmap's own definition of done: an injected fault in a demo composition is
**detected, repaired (regenerate → gauntlet → policy → swap), verified, and
swapped with ZERO human input**, and the **incident report reconstructs every
step from the causal trace alone**. Two companions pin the human-ack rule: a
widening repair `pauses` for an ack, and the *same* repair with the crossing
acknowledged proceeds to swap.
```
