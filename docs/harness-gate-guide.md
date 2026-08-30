# Driving the 245/246 gate from a harness

A harness that runs an agent's tool calls through revl gets, for free, the
question every allowlist and every VM snapshot fails to answer: *which
irreversible actions did this session actually take, and which of them did a
human approve?* This guide is the onboarding path for wiring a harness to that
gate. It distills what `tests/test_approval_policy.py` and
`tests/test_approval_typed.py` already prove; read those two files when you
need the exact call sequences, and read
[design/245-session-commit.md](design/245-session-commit.md) and
[design/246-auto-approve.md](design/246-auto-approve.md) for why the gate is
shaped this way.

The gate is off by default. With no policy configured a session behaves byte
for byte as it does today (`test_no_policy_is_byte_identical`), so turning it
on is opt-in.

## Serve with `--approval-policy`

Enable the auto-approve policy at serve time:

    revl mcp serve --approval-policy auto

`auto` is the only mode today. Two rules the server enforces the moment it is
on:

- **Recording is required.** The gate's authority is the WAL. A `revl_load`
  without `record: true` is refused under an enabled policy
  (`test_enabled_policy_requires_recording`), because a policy whose approvals
  evaporate is worse than none.
- **Approval-required capabilities come from the boundary policy file.** Pass
  one with `--policy`. A rule targets a boundary by its capability token:

      capability charge requires approval

  For an emission extern the capability token is the extern NAME (an emission
  takes no `[caps]` scope; see
  [design/245-session-commit.md](design/245-session-commit.md) Decision 2). So
  the rule above requires approval on `extern emission fn charge(...)`. A
  component that reaches an approval-required capability with no approval edge
  is refused at admission (`test_policy_owned_requirement_refuses_at_admission`).

Self-approval is the default identity model's hole: with no operator profile
bound, every verb including `approve` is ungated, so the calling agent could
answer its own prompt. An enabled policy is only meaningful alongside an
operator profile (`--operator-profile`) that withholds `approve` from the
agent and grants it to the human.

## The three action classes

The policy has no vocabulary for naming actions. Its only input is the checked
effect class of a call, derived from the call's extern classifications. Nothing
at runtime or in the harness can move a call between classes.

| class | derivation | policy posture | what the harness sees |
|---|---|---|---|
| (a) revertible | every crossing is a `witnessed` extern with its registered inverse (243) | auto-approve silently | the call returns; no ticket, no prompt |
| (b) deferrable | every non-(a) crossing is a `deferred` emission (245) | auto-approve; enumerate at commit | the call returns; the crossing appears in the commit manifest's `summary` |
| (c) immediate | any emission crossing that is neither (a `compensate` does not change this, 247) | prompt per call | `revl_call` returns `approvalRequired` with a ticket; nothing fired |

A call's class is the worst class over every crossing its checked reach
includes: one prompt covers the whole call or none of it. The three classes
map onto three externs:

```revl
type Stash = { path: Str, bak: Str }
type FsError = { code: Str }
extern pure fn unstash(w: Stash) -> Unit = @py { return }
extern witnessed[fs] fn stash_path(p: Str) -> Result[Stash, FsError] undo unstash(result) = @py { return Ok({}) }
extern emission deferred fn deliver(sink: Str, msg: Str) = @py { return }
extern emission fn announce(sink: Str, msg: Str) = @py { return }
service Ops {
  emission fn stash(p: Str)
  emission fn enqueue(sink: Str, msg: Str)
  emission fn shout(sink: Str, msg: Str)
}
component Agent provides ops: Ops {
  provide ops {
    fn stash(p) { effect stash_path(p) }
    fn enqueue(sink, msg) { emit deliver(sink, msg) }
    fn shout(sink, msg) { emit announce(sink, msg) }
  }
}
```

Here `stash` is class (a), `enqueue` is class (b), and `shout` is class (c).
This is the fixture both test files build on.

## The MCP verbs a harness calls per tool call

A harness admits and drives a composition through the MCP verbs
([mcp-reference.md](mcp-reference.md) has every shape). The gate touches four
of them:

- `revl_check` compiles a candidate component; `revl_admit` checks it against
  the running composition. These are how a harness gets an agent's proposed
  component past the same admission gate a human's `revl compile` uses, before
  anything boots.
- `revl_load` boots the composition. Under the policy, pass `record: true`.
- `revl_call` drives a provided service method. This is where the per-call
  decision runs. On a class (a) or (b) target the call proceeds and returns its
  result. On an unapproved class (c) target it returns
  `{ok: false, approvalRequired: true, ticket: {...}}` and the host body does
  NOT run (`test_immediate_emission_prompts_then_fires_once`). The decision is
  made inside the session, not on the tool name, so a class (c) crossing
  re-fired by `revl_replay_forward` is caught the same way
  (`test_replay_forward_class_c_is_refused_like_a_fresh_call`).
- `revl_approve(hash)` mints a standing approval for an outstanding ticket. The
  `hash` must be one the server issued; an unknown hash is refused by the
  outstanding-ticket table (`test_unknown_ticket_hash_is_refused`).

The class (c) loop, per call:

1. `revl_call` returns `approvalRequired` with a `ticket`. The ticket names
   what a yes would mean (component, key, method, an args digest, the reached
   capabilities) and a `hash` over all of it.
2. The harness relays the ticket to the human. On a yes, call
   `revl_approve(ticket.hash)`.
3. The harness re-issues the identical `revl_call`. It recomputes the same
   hash, finds the standing approval, fires exactly once, and consumes it. A
   second identical call is refused with a fresh ticket: the approval is
   single-use, so what the human approved is exactly what fired, never a
   superset.

A drifted call (different args, a different method, or a component swapped
between mint and use) recomputes a different hash and is refused with a fresh
ticket (`test_hash_binding_drift_and_unknown_hash`,
`test_swap_invalidates_a_standing_approval`). The activation body is a boundary
too: a swap whose activation reach is class (c) does not boot until approved
(`test_activation_body_emission_cannot_dodge_the_prompt`).

## The two-step commit

Class (b) crossings do not fire mid-session; they enqueue and flush at the
session commit, after one prompt. That prompt is a two-step verb:

1. `revl_commit` enumerates the **commit manifest**. Its `summary` is the
   human's one-line prompt (grouped counts of what will fire, for example
   "empty trash: 3 files; send: 1 email"); its `hash` binds the gate target.
   Nothing crosses yet.
2. `revl_commit_confirm(hash)` flushes the deferral queue in FIFO order,
   discharges the witnessed escrow, and marks it durable. If the queue or the
   live composition changed since enumeration, the recomputed hash mismatches
   and the confirm is refused with a fresh manifest (a result, not an error).

`revl_abort` is the counterpart: it drops the deferral queue (nothing fired)
and replays the witnessed inverses, proving a clean world. An all-(a)/(b)
session commits with `prompts == {"commit": 1, "perCall": 0, "residue": 0}`
(`test_all_ab_session_is_fully_auto_approved`).

## The `session.state()` metrics

`revl_state` surfaces the session's approval metrics (None off-policy, so
`state()` stays byte-identical when the gate is off). Two counters carry the
headline numbers:

- `approvals` = `{silent, atCommit, prompted}`: boundary calls tallied by
  posture at decision time. `silent` is class (a), `atCommit` is class (b),
  `prompted` is class (c).
- `prompts` = `{commit, perCall, residue}`: human interactions raised.
  `commit` is 1 per committed session (0 for an aborted one), `perCall` is one
  per class (c) ticket, `residue` is one per residue-triggered prompt (for
  example a witnessed inverse that failed on abort).

Two derived numbers ride on those:

- **percent auto-approved-with-proof** =
  `(silent + atCommit) / (silent + atCommit + prompted)`, over calls that reach
  at least one crossing.
- **prompts-per-session** = the sum of the `prompts` counters. The target is
  about 1: a session whose every action is class (a) or (b) shows exactly one
  commit prompt and nothing else.

These are the numbers a harness dogfood measures before and after wiring the
gate. `tests/test_approval_policy.py` asserts both on a live cordis-py
composition end to end.
