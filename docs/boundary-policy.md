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
```

The JSON form parses to the same policy:

```json
{
  "components": [
    {"pattern": "Agent*", "allow": ["llm", "kv*"], "deny": ["sendEmail"]}
  ],
  "realms": [{"realm": "billing", "allow": ["db", "ledger"]}],
  "tenants": {"neverReachEachOther": true},
  "mcp": {"allow": ["llm", "kv*"]}
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
