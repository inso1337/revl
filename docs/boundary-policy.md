# Boundary policy — the third leg of the gate

Everything on the G8 surface (`revl audit`) answers **what does this reach?**
For every component it enumerates the emission scopes it may cross and the
host code it touches. What the surface does *not* let you say is the inverse:
**what may anything here reach?** The boundary policy adds that — a file the
composition operator writes, evaluated against the audit graph at admission,
that states absolute authority over the boundary and **refuses admission** for
anything that exceeds it.

## The triad

Three gates, three different questions, one audit graph:

| Gate | Question | Axis |
| --- | --- | --- |
| admission (DESIGN §5) | does a redeclared interface keep running consumers valid? | **correctness** |
| `revl audit --diff` (item 21) | did a regenerated component quietly *widen* what it reaches? | **drift** |
| **boundary policy** (item 33) | does any component reach a capability it **may not**? | **authority** |

Correctness protects the components already running. Drift protects against a
regeneration sneaking a new boundary crossing past review. Policy is the
absolute floor: a statement of authority that holds no matter what the code
says it does. A boundary the code reaches but the policy forbids is refused,
with a why-trace naming the offending chain.

## How it evaluates

A policy is a set of allow/deny rules over **capabilities** — the exact tokens
the G8 audit already enumerates:

* **emission scopes** — `emission[llm] fn ask(...)` contributes capability
  `llm` to every component that emits through it (docs/capabilities.md);
* **host externs** — an `extern emission fn sendEmail(...)` a component's body
  reaches contributes capability `sendEmail`;
* **`*`** — a bare `emission` (no scope) or a first-class dispatch reaches an
  *unnameable* boundary. It never satisfies a named allow-list; only a literal
  `*` in the allow-list accepts it, because an unnameable reach can never be
  proven in-bounds.

A component's **reach** is the union of those tokens. Evaluation is pure set
operations over that reach — no new analysis, just the audit graph the other
two legs already read (`revl.audit_diff.audit_report`). A rule constrains the
reach; a token outside an allow-list, inside a deny-list, or shared across
tenants is a **violation**, and the first violation **refuses admission**.

### Which spelling a rule selects

A rule names a **token**, never a source construct, so the same crossing spelled
two ways in the source is one token to the policy. The whole table (item 247;
pinned by `tests/test_247_capability_reach_spellings.py`):

| the component crosses through | the token a rule names |
|---|---|
| `emit db.execute(...)` on `emission[...] fn execute` required as key `db` | `db` — the **requirement key**, not the service name |
| `emit cache.put(...)` on a bare `emission fn put` | `*` |
| `emit pg_write(...)` on `extern emission[db] fn pg_write` | `db` — the **declared scope** |
| `emit sendEmail(...)` on `extern emission fn sendEmail` | `sendEmail` — the extern **names itself** |
| the same extern reached through a chain of plain `fn`s | unchanged: the scope, or the extern name |
| `effect stash(...)` on `extern witnessed[fs] fn stash` | `fs` |
| a `pure`/`acquire` extern the body reaches | its own name (no scope is declarable) |
| an emitting callable handed to a dispatcher | `*` |

Two consequences worth stating outright, because nothing in the source hints at
them:

- **A scoped extern's NAME is not a token.** `emission[db] fn pg_write`
  contributes `db` and only `db`, so `component X may not reach pg_write`
  selects nothing while `component X may not reach db` refuses. That is the same
  rule item 343 applies to the approval gate (`capability db requires
  approval`), the register floor (`capability db requires register keyed`) and
  `secret K for db` — one namespace across all of them. The extern name is still
  on the audit's `host code:` line, with the token beside it, so a refusal on
  `db` is navigable back to the declaration that carries it.
- **Whether a rule selects at all depends on reach, not on declaration.** A
  floor on a capability nothing in the composition crosses refuses nothing. `revl
  policy evaluate` reports such a rule as inert rather than as passing.

## The file format

A small line DSL (`revl.policy`), or the equivalent JSON. Blank lines and
`#` comments are ignored. Patterns are globs (`fnmatch`) over capability
tokens — `kv*` matches `kv`, `kvstore`, `kv.sessions`; `*` matches anything.

```
# per component pattern — an allow-list bound to a component-name glob
component Agent*   may reach     llm, kv*
component Reporter may reach     db, metrics
component *        may not reach sendEmail        # a deny-list (refuses always)

# per realm — the same, bound to every component isolated into that realm
realm billing may reach db, ledger

# tenants never reach each other — two components in *different* realms that
# reach a common named boundary are refused; their isolation is not real
tenants never reach each other

# the MCP / agent sandbox — the profile for agent-generated code admitted
# through the MCP session: "agent output may reach [llm, kv*] and nothing else"
mcp may reach llm, kv*

# refuse a call whose class-(c) capability has an unbounded item-260 crossing
# ceiling, instead of ticketing it (issue #1755; off unless written)
approvals require bounded crossings

# mark a capability never-STANDING (issue #1982): the per-call class-(c) prompt
# stays, and one operator `yes` may never be widened into a standing grant
# (item 344) or a distilled auto-approve rule (item 251) for it
capability mail.send  may never be granted standing
capability shell.*    may never be granted standing
```

The JSON form parses to the same policy:

```json
{
  "components": [
    {"pattern": "Agent*", "allow": ["llm", "kv*"], "deny": ["sendEmail"]}
  ],
  "realms": [{"realm": "billing", "allow": ["db", "ledger"]}],
  "tenants": {"neverReachEachOther": true},
  "mcp": {"allow": ["llm", "kv*"]},
  "approvalCeilings": {"refuseUnbounded": true},
  "neverStanding": ["mail.send", "shell.*"]
}
```

### Rule semantics

* **`may reach` (allow-list).** When any allow rule *selects* a component
  (its name matches a `component` glob, or it is isolated into a named
  `realm`), that component is under a **closed allow-list**: the union of every
  allow pattern that selects it. A reach outside that union is refused. A
  component that no allow rule selects is *unconstrained* by allow-lists — only
  deny rules apply to it.
* **`may not reach` (deny-list).** A reach matching a deny pattern is refused
  regardless of any allow-list. Deny wins.
* **`tenants never reach each other`.** Partition components by realm (their
  `isolate` map). Two components in *disjoint* realms that reach a common
  named boundary are refused: one tenant's world touches a boundary the
  other's does too, so the isolation the realms promise is not real. A
  component with no realm lives in the shared realm and is not a tenant. `*`
  is excluded from this check — an unnameable reach is caught by the
  allow-lists, and it would pair every tenant with every other for no
  actionable reason.
* **`mcp` / `agent` (the sandbox).** An allow-list that applies only to
  components admitted through the MCP session (see below). Everywhere else it
  is inert.
* **`approvals require bounded crossings`.** Every class-(c) ticket carries the
  item-260 ceiling of each capability it asks about (`ceilings`, see
  [mcp-reference.md](mcp-reference.md#revl_approve)). With this line, a call
  whose capability's ceiling is `unbounded` in the crossing component is
  refused before anything is spent or ticketed, and the refusal names the
  capability and the cardinality reason. Without it the call is ticketed as
  before, with the `unbounded` ceiling on the ticket.
* **`capability <glob> may never be granted standing` (issue #1982).** The
  class-(c) prompt is a per-call floor, and a yes can be widened over a series
  in exactly two ways: a session-scoped standing grant (`revl_approve` with
  `capability` + `uses`/`ttlMs`, item 344) and an applied distilled
  auto-approve rule (item 251). Both record ONE operator yes and then cover
  every later crossing until `uses` runs out or the TTL lapses, and `ttl`
  bounds the grant's *lifetime*, not its shape — a large TTL with a large
  `uses` is the same unbounded grant. This clause is what a policy could not
  say: not *this* capability, not as a standing thing. It is a glob over the
  capability vocabulary below, and it is **not** a deny-list — the capability
  stays usable:

  * the crossing still prompts, and the single-use exact-hash
    `revl_approve(hash=…)` still answers it one call at a time;
  * `revl_approve(capability=…, uses=…/ttlMs=…)` is **refused** with a
    diagnostic naming the capability and quoting the clause — for a grant
    named directly AND for one minted from an outstanding ticket, which is why
    the refusal is enforced where the ticket's own capability spellings are
    resolved rather than in the verb dispatch;
  * `revl_distillation_offers` does not *offer* a rule whose capability set
    intersects the clause (reported in `refusals` with reason
    `never-standing`), and `revl_apply_distillation` **refuses to install**
    one — a distilled rule is standing auto-approval reached by a different
    verb, so a clause enforced only on the mint would leave it as the way
    around;
  * the refusal is fail-closed in both directions: every capability slot a
    ticket carries is checked (not only the spelling the caller passed), and
    a spelling the capability order cannot parse is compared as itself rather
    than read as unmarked;
  * it is enforced where a standing yes is *read*, not only where it is
    written, so a grant or applied rule that already exists stops covering
    the moment the clause is in force rather than outliving the policy that
    forbids it. That includes a `may auto-approve` rule written BY HAND for a
    marked capability: where the clause and a standing auto-approve meet in
    one policy, the narrower statement wins and the crossing prompts again.

  An unmarked capability mints and distills exactly as before, so a policy
  written without this clause behaves byte-identically.

### What a capability token may be

The capability vocabulary is CLOSED. A token in a reach rule is a capability
pattern: dot-separated identifier segments naming a boundary in the wiring
namespace (`mail.send`, `fs.write`), optionally widened with the glob
metacharacters `*`, `?` and a character set (`llm*`, `mail.*`, `[mn]ail.send`).
Several tokens are separated by **commas**, never spaces. The JSON form's
`allow` / `deny` / `mcp.allow` arrays and the taint-flow lists are held to the
same shape.

A token that cannot be a capability pattern is a `PolicyError` **at parse
time**, naming the token and its `file:line`, so a typo can never become a rule
that silently requires or denies nothing (issue #1984). This is the same
closed-vocabulary rule an unknown evidence facet already follows. In
particular:

* `component * may not reach mail.send shell.run` is refused: two patterns need
  a comma, and one whitespace-bearing token denies neither.
* `component Agent* may reach llm kv*` is refused for the same reason. It used
  to parse and then grant nothing: the fail-closed direction, but still a
  no-op nobody was told about.
* `except` is **not** part of the reach grammar, and `component * may not reach
  mail.send except the send kit` is refused with a diagnostic saying so. A
  reach rule carries no carve-out slot: the supported way to say "this crossing
  must be asked about" is
  `capability <glob> requires approval [ttl <D>] [require <N> of {a, b}]`
  (item 246), which refuses the reach until it is approved. To narrow a rule,
  name the components it applies to with a `component <glob>` selector instead
  of carving an exception out of a wildcard.

A token can also be capability-shaped and still wrong (`mail.sedn` where the
boundary is `mail.send`). Nothing at parse time can tell those apart, so
`evaluate` warns (`InertDenyPolicyWarning`) when a `may not reach` rule selected
a component in the audit and no reach of that component matched any of its
patterns. Such a rule is not protecting anything, and without the warning
enforcing it reads exactly like a clean pass.

## The refusal

A violation refuses admission and carries a why-trace naming the violating
chain — which component reaches what it may not, and how. For a component
reaching outside its allow-list:

```
policy violation: component `AgentLeak` may reach only [llm, kv], but it
reaches `sendEmail` through host code — admission refused (boundary policy,
item 33)
  why `AgentLeak` was rejected:
    AgentLeak -> sendEmail   (sendEmail)
    AgentLeak  policy_agents.rvl  reaches host code `sendEmail`
    sendEmail                     boundary crossed [sendEmail]
```

For a cross-tenant reach the trace names both tenants and the shared boundary
(a set, not a chain):

```
policy violation: tenants never reach each other, but `TenantAJob` (realm
tenantA) and `TenantBJob` (realm tenantB) both reach `bus` — their isolation
is not real; admission refused (boundary policy, item 33)
  why `bus` was rejected:
    TenantAJob  policy_tenants.rvl  realm tenantA, reaches `bus`
    bus                             shared across tenants [bus]
    TenantBJob  policy_tenants.rvl  realm tenantB, reaches `bus`
```

## The CLI

```
revl audit <files...> --policy revl.policy          # refuse (nonzero) on any breach
revl audit <files...> --policy revl.policy --json    # machine-readable violations
revl audit <files...> --policy revl.policy --mcp-scope '*'   # apply the mcp sandbox everywhere
```

`--policy` builds the same audit graph `--json`/`--diff` use, evaluates the
policy over it, prints the report, and returns nonzero if any component
breaches it. `--mcp-scope COMPONENT` (repeatable, or `*` for all) marks
components as MCP/agent-admitted so the policy's `mcp` sandbox applies to them
from the CLI, mirroring what the MCP session does automatically.

## The MCP / agent sandbox

The dedicated block exists because agent-generated code is the case where
"what may this reach?" most needs a machine-checked answer rather than a
review convention. The MCP session (`revl.mcp.session`) is how an agent
generates, checks, admits, and drives a composition in memory. Give the
session a sandbox policy and the `mcp` allow-list becomes an **admission
invariant**: every component the agent tries to `load` (or `swap` in) is
agent output, and its G8 reach must stay inside the sandbox. A draft that
over-reaches is refused *before any runtime is touched* — the check is set
operations over the audit graph, so nothing boots.

```python
from revl.mcp.session import Session
from revl.policy import parse_policy

session = Session()
session.sandbox = parse_policy("mcp may reach llm, kv")   # the agent floor
session.load(agent_generated_ir)   # SessionError if it reaches beyond [llm, kv]
```

"Agent output may reach `[llm, kv]` and nothing else" stops being a sentence in
a review checklist and becomes a gate the code cannot pass without satisfying.
