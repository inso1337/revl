# Operator capabilities — G4 for the management plane

Roadmap item 55. Source: `src/revl/mcp/operator.py` (the profile + the
decision), `src/revl/mcp/server.py` (the gate at verb dispatch),
`src/revl/mcp/session.py` (the token→operator binding).

## The gap

The MCP session (`docs/mcp-bridge.md`) gives an agent a typed protocol over a
running composition. Some of its verbs are read-only; others **rewrite a
running system**:

    revl_swap  revl_unload  revl_restore  revl_rollback  revl_undo
    revl_edit  revl_load    revl_snapshot  revl_estop
    revl_call  revl_lease   revl_fork      revl_fork_confirm
    revl_step_back  revl_replay_forward

Nothing in the session authenticates or scopes the caller. Anyone who reaches
the transport is **root over the composition** — it can unload the system,
swap arbitrary code in, or snapshot every source out. That is fine for a
single-agent loop on a laptop. It is not fine before a networked or
multi-operator deployment, and not fine once item 39 exposes compositions as
public MCP surfaces.

G4 already bounds what a *component* may reach. Item 33 bounds what *anything
in the composition* may reach. This is the third leg: what the **operator**
driving the session may **do**.

| Gate | Question | Axis |
| --- | --- | --- |
| G4 (per component) | what may a component reach? | component authority |
| boundary policy (item 33) | what may anything in the composition reach? | composition authority |
| **operator capabilities (item 55)** | what may the operator driving the session do? | **management authority** |
| lineage (item 66) | where did a component's authority come from? | provenance |

## The operator profile

A profile declares one or more **operators**. Each operator has a token and a
set of **grants**: allow/deny rules over management verbs, scoped to
components and realms. It is the same shape as the boundary policy — globs,
allow/deny, pure set evaluation, a why-trace on refusal — pointed at the
management plane instead of a component's reach.

### The line DSL

```
# alice runs tenant_a
operator alice may swap, plan on tenant_a*     # may swap within tenant_a
operator alice may snapshot on *               # may snapshot everything
operator alice may not unload on *             # may never tear the system down

# bob is a read/backup operator
operator bob may snapshot on *
```

Grammar (blank lines and `#` comments ignored):

    operator <token> may     <verb>[, ...] on <subject>[, ...]
    operator <token> may not <verb>[, ...] on <subject>[, ...]
    operator <token> may     <verb>[, ...]                        # on *

* **verbs** — `load`, `swap`, `edit`, `unload`, `restore`, `snapshot`, `undo`
  (`rollback` is accepted as an alias for `undo`), `commit`, `approve`, `estop`,
  `call`, `lease`, `fork`, and `replay`. `*` matches every verb.
* **subjects** — globs (`fnmatch`) matched against a target component's **name**
  *or* any **realm** it is isolated into. `tenant_a*` matches the realm
  `tenant_a` and the component `tenant_a_cache` alike; `*` matches anything.
  Omit `on <subject>` to mean `on *`.
* **allow vs deny** — `may` contributes a capability; `may not` prohibits, and
  **deny wins** over any allow (exactly as in the boundary policy). An operator
  is **closed by default**: a verb with no allow that selects the target is
  refused.

### The JSON equivalent

```json
{ "operators": [
    { "token": "alice",
      "grants": [
        {"verbs": ["swap", "plan"], "on": ["tenant_a*"]},
        {"verbs": ["snapshot"],     "on": ["*"]},
        {"verbs": ["unload"],       "on": ["*"], "deny": true} ] } ] }
```

Text that opens with `{` parses as JSON, otherwise as the DSL — both produce
the same `OperatorRegistry`.

## Binding a session to an operator

A session runs *as* one operator. The identity is set at serve time:

```
revl mcp serve --operator-profile ops.profile --operator alice
```

`--operator` is optional when the profile declares exactly one operator. Today
the stdio transport carries a single session, so one served process is one
operator. When the transport later carries a per-caller **session token** (item
39), the same registry maps each token to its operator with no change to the
gate — the token *is* the operator's name.

## Per-verb gating

The gate lives at the MCP verb dispatch (`server.handle` → `operator.decide`).
Before a management verb runs, the session's operator must be authorized for
that verb on **every component the action touches** — all-or-nothing, the way
admission is. A refusal returns the running system **untouched**, with a
policy-style why:

```json
{ "ok": false,
  "authorized": false,
  "note": "the running composition is untouched — the operator profile refused this management action",
  "authority": {"operator": "alice", "verb": "swap", "allowed": false},
  "why": {"kind": "operator-authority", "subject": "alice",
          "path": ["alice", "TenantBCache"], "...": "..."},
  "diagnostics": [{"category": "operator", "message": "operator `alice` may not `swap` `TenantBCache` — no grant in its profile permits `swap` there ..."}] }
```

### What each verb touches

The target set is computed **before** the action runs, from the session's IR:

* **swap** — the components the candidate actually *changes*: those added,
  removed, or whose IR entry differs (modulo provenance) from the running
  composition. So "may swap within tenant_a" permits changing a `tenant_a`
  component in a multi-realm composition, and refuses changing a `tenant_b`
  one. A no-op or server-side re-admit targets the whole composition.
* **load** — every component in the candidate (a cold boot instantiates all).
* **restore** — every component named in the snapshot manifest.
* **unload / edit / snapshot / undo / rollback** — the whole running
  composition (each operates on all of it).
* **estop** (item 443) — the whole running composition too, and deliberately:
  a halt that stopped one component would not be a halt. So a subject-scoped
  `may estop on tenant_a*` authorizes only while every live component is in
  `tenant_a*`; an operator who must always be able to hit the button needs
  `may estop on *`.

* **call** — the whole running composition, because the provided key is resolved
  by the live session and may dispatch through more than one component.
* **lease** — the named component; an unknown or unloaded component fails closed
  against the whole composition.
* **fork** — the named component when supplied, otherwise the whole composition;
  `revl_fork_confirm` is always the whole composition because its hash is the
  authority-bound rewind decision.
* **replay** — the named component when supplied, otherwise the whole composition
  (`revl_step_back` and `revl_replay_forward`).

`estop` is its own verb and is never folded into `unload` or `commit`. An
operator trusted to unload a composition cleanly is not automatically trusted
to strand two hundred brackets and leave every handle held — and the E-Stop is
the one verb a composition or an agent must never be able to invoke on itself,
which is what holding it here buys ([443-estop.md](design/443-estop.md)).

### Composed verbs — a swap reached through another verb

The gate is positional over the dispatch table, so a verb is gated by the name
it is called under. Two verbs perform a swap through *another* verb's
machinery, and both are mapped to `swap` rather than to verbs of their own —
an operator who may not swap a component may not ship or repair it either:

* **`revl_ship`** fuses check → admit → plan → swap and, with `apply: true`,
  calls the `revl_swap` handler directly;
* **`revl_repair`**'s remediation step calls `Session.swap` itself, so it is
  also checked against an enforced component lease (item 61) exactly as
  `revl_swap` is.

Both are conditional: each has a rehearsal mode that mutates nothing
(`revl_ship` without `apply`, `revl_repair` with `apply: false`), and a
rehearsal is not a privileged action. The rule for a new verb: **if it can
reach `Session.swap` / `.load` / `.unload` / `.restore` / `.rollback` /
`.undo` / `.estop` — through its own handler or any handler it calls — it must
be in `operator.TOOL_VERB` or `operator.COMPOSED_TOOL_VERB`.**
`tests/test_mcp_authority_gate.py` enumerates every advertised verb and fails
on one that is in neither and not recorded as deliberately ungated.

### An undecidable target set fails closed

When the target set cannot be determined without running the action — a
candidate that does not compile, or a verb with nothing loaded — the gate
scopes the action to the **unnameable whole composition**, which only a literal
`may <verb> on *` grant satisfies. A subject-scoped operator is refused; an
unscoped one proceeds and gets the handler's own diagnostic. Component leases
apply the same rule: a swap whose targets cannot be derived is checked against
*every* active lease.

"I cannot work out what this touches" is a reason to refuse, never a reason to
ungate. Deferring instead was a bypass an attacker could steer into: make the
target derivation fail and the gate stopped gating.

### How it reuses item 33's evaluation

The decision is the boundary policy's glob machinery pointed at operators:

* a target component's realms come from `policy.component_realms` — the exact
  realm resolution the policy uses (imported read-only, not reimplemented);
* subject matching is the same `fnmatch` glob semantics `policy` applies to
  capability tokens, and `*` is treated the same way — an unnameable
  whole-composition target is only ever satisfied by a literal `*` grant;
* deny-wins-over-allow and closed-by-default mirror the policy's allow/deny
  resolution;
* the refusal carries a `revl.why.WhyTrace` (`kind: operator-authority`),
  rendered like every other gate refusal.

## Who — the audit story (item 27)

Authorization without attribution is half the story. Every **authorized**
management action records the operator identity, so "what changed and on whose
authority" is one query:

* the result carries `authority: {operator, verb, subjects, allowed: true}`;
* for a verb that returns a causal trace (`docs/why-runtime` / item 27), an
  `operator` event is prepended to that trace — so the *who* rides inside the
  same causal record the *what* does:

  ```json
  {"channel": "operator", "subject": "alice", "detail": "authorized `swap` on TenantACache"}
  ```

This is carried in the MCP layer, not in `why_runtime`: the operator is a
property of the *session* driving a transition, not of the lifecycle transition
itself, so the runtime trace stays authority-agnostic and the change stays
additive.

## Backward compatible / opt-in

With **no** `--operator-profile`, `session.operator` is `None` and every verb
is ungated — today's root-over-transport, byte-for-byte unchanged. Read-only
and diagnostic verbs (`revl_check`, `revl_audit`, `revl_plan`, `revl_query_*`,
`revl_state`, `revl_grammar`, `revl_resolve`, …) are never gated, profile or
not. The profile is the **pre-networking safeguard** — opt-in for networked and
multi-operator use, so that when the management plane is exposed it already has
declared, checked capability bounds instead of implicit root.
