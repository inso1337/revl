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
    operator <token> key     sha256:<64 hex>                      # item 471

* **verbs** — `load`, `swap`, `edit`, `unload`, `restore`, `snapshot`, `undo`
  (`rollback` is accepted as an alias for `undo`), `commit`, `approve`,
  `override`, `estop`, `deploy`, `call`, `lease`, `fork`, and `replay`. `*`
  matches every verb.
* **subjects** — globs (`fnmatch`) matched against a target component's **name**
  *or* any **realm** it is isolated into. `tenant_a*` matches the realm
  `tenant_a` and the component `tenant_a_cache` alike; `*` matches anything.
  Omit `on <subject>` to mean `on *`.
* **allow vs deny** — `may` contributes a capability; `may not` prohibits, and
  **deny wins** over any allow (exactly as in the boundary policy). An operator
  is **closed by default**: a verb with no allow that selects the target is
  refused.
* **key** — the operator's **vote credential**, used by multi-party approval
  (item 471) to bind who cast a vote. It is the SHA-256 **digest** of a secret
  you issue to that operator out of band, never the secret: the profile is a
  file that gets read, copied and diffed, and one carrying the secrets would
  hand every voter identity to anyone who can read it. A line that is not a
  64-character hex digest (with or without the `sha256:` prefix) is a parse
  error, and one token may declare at most one key. See
  [Vote credentials](#vote-credentials-multi-party-approval) below.

### The JSON equivalent

```json
{ "operators": [
    { "token": "alice",
      "key": "sha256:<64 hex>",
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

## Vote credentials (multi-party approval)

A `require N of {a, b, c}` approval rule (item 471) admits a crossing only once
N distinct approvers have each voted. A session runs as ONE operator, so the
other votes name their operator with `asToken` on `revl_approve`. Until issue
#979 that name was taken at face value, which meant one operator could satisfy
`require 2 of {...}` by asserting two of the names in turn: the count was of
distinct names, and multi-party control is about distinct principals.

A cast is now attributed to a **bound** identity or it is refused:

* naming **this session's own operator**, or naming nobody, attributes the cast
  to the identity bound at serve time. That is process configuration, not
  something the caller on the wire chooses;
* naming **anyone else** requires that operator's vote credential, passed as
  `asSecret` on the same call. The session hashes it and compares against the
  `key` the profile declares for that token;
* everything else refuses, by name: `unbound-identity` (no profile to check
  against), `unknown-operator`, `unkeyed-identity` (no `key` declared),
  `unproven-identity` (credential missing or wrong), `unnamed-credential` (a
  secret with no `asToken` beside it). Each refusal is written to the decision
  graph before it is raised.

The distinctness unit is the **principal**, which is derived rather than
asserted: the session binding is one principal and each distinct credential
digest is one principal. Two operators issued the SAME secret are therefore one
principal and supply one vote between them (`same-principal`), which a count of
names cannot see. The graph records a hash of the credential digest, never the
digest, so an audit can tell the principals apart without carrying the verifier.

Issue a credential by choosing a secret, hashing it, and putting the digest in
the profile:

```
$ printf %s "$SECRET" | shasum -a 256
operator bob key sha256:<the digest>
operator bob may approve on payments*
```

The same binding governs `revl_escalate`, `revl_revoke` (its question branch)
and `revl_override`: an override recorded against a name the caller merely typed
is an unattributable act wearing somebody else's name.

**What this proves, and what it does not.** N counted votes required N distinct
secrets. It does not prove N humans consented: a credential is a bearer token,
it can be shared, delegated or stolen, and every cast still arrives over one
session's wire, so an operator who has collected two secrets still satisfies a
two-of-M rule. Closing that needs a per-caller authenticated transport (item 39)
where each cast arrives on its own authenticated connection and is signed over
the question's binding, so a captured credential is not replayable. Treat the
quorum as binding against mistake and against a single operator's unaided
assertion, and as advisory against an operator who has collected the secrets.

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
* **deploy** (item 476, issue #830) — the whole running composition, because a
  deploy reconfigures it as ONE coordinated unit across a machine boundary: a
  subject-scoped `may deploy on tenant_a*` authorizes only while every live
  component is in `tenant_a*`. It has its own verb rather than folding into
  `swap` because a deploy crosses a machine boundary, which an operator must be
  able to authorize separately from a same-host swap: being trusted to hot-swap
  a component locally is not the same as being trusted to push the composition
  onto a second host and drive its teardown.

* **approve** (items 246 / 344 / 379 / 251 / 471) — the crossing component the
  approval names, resolved without running anything: the ticket `hash` against
  the outstanding-ticket table, a proactive grant's `capability` against the live
  class map, a distilled rule against the components its glob selects. So `may
  approve on payments` is usable while other components are live. It covers
  `revl_approve` (including a multi-party VOTE), `revl_revoke` (a standing grant
  or a pending question), `revl_escalate`, and the two distillation verbs.
* **override** (item 471) — the crossing component, exactly as `approve` is: an
  override decides ONE question about one candidate, so it must not widen to the
  whole composition. It is `revl_override`'s verb and nothing else's, and it is
  deliberately NOT folded into `approve`. `require N of {...}` says that N
  distinct named humans must answer, and an operator trusted to cast one of those
  N is not thereby trusted to stand in for all of them — folding the override into
  `approve` would hand every voter a one-operator bypass of the rule they vote
  under. So `may approve on payments` authorizes votes on payments and no
  override at all, and `may override on payments` is the separate address an
  on-call operator holds to break the glass. The admission is recorded as
  `satisfiedBy: "override"` with the count it actually had, so an audit never
  reads it as the quorum it stood in for
  ([471-quorum-approval.md](design/471-quorum-approval.md)).
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
