# revl ⇄ MCP — the agent boundary

**Status:** implemented (`revl mcp {serve,schema,import}`) · tests:
`tests/test_mcp.py`

An AI agent meets a revl system in three roles, and all three are *boundary*
phenomena — which is why none of them needed a language feature:

| the agent is… | the mechanism | where |
|---|---|---|
| a **consumer** of the composition | services projected to MCP tools | `revl mcp schema` |
| an **operator** of the composition | the compiler as an MCP server | `revl mcp serve` |
| a **dependency inside** it | an LLM is a `service` with `emission` ops | ordinary revl |

The third needs no tooling at all: `service Assistant { emission fn
complete(prompt: Str) -> Str }` makes a model a coeffect, so routing is
provider hot-swap, governance is `intercept` metadata, cost lands on the G8
audit, and a failed generation can carry `compensate`. Nondeterminism stays
*outside* the checked layer, where it cannot poison the metatheory.

## 1. `revl mcp schema` — services → tools

Every operation a composition **provides** becomes a tool definition whose
JSON Schema comes from the declared types.

```bash
revl mcp schema examples/user_cache.rvl
```

The point is the annotations. MCP's `readOnlyHint` / `destructiveHint` are
*assertions by the server author*; nothing checks them. revl derives them:

- **Declaration** — an `emission` operation is destructive by definition.
- **Implementation** — a plain-declared operation whose body reaches an
  emission (through a required service) or non-`pure` host code is
  destructive *anyway*. The walk is transitive through `fn` calls.

This second half matters, and the reference example demonstrates why:

```revl
service Cache { fn get(key: Str) -> Opt[Str]
                fn put(key: Str, value: Str) }   // declared plain
…
fn put(key, value) {
  effect store.insert(key, value) undo store.remove(key)
  emit db.execute(`INSERT INTO cache_log VALUES (${key})`)   // …but emits
}
```

`cache.put` projects as `readOnlyHint: false`, `destructiveHint: true`, with
the provenance recorded:

```jsonc
"x-revl": {
  "classification": "emission",
  "annotationsDerivedFrom": "compiler",
  "effects": {
    "declaredEmission": false,
    "reachesEmission": ["db.execute"],
    "reachesHostCode": [],
    "declarationUnderstatesBody": true
  }
}
```

`openWorldHint` is likewise derived, from extern reachability, and
`idempotentHint` from a `commutative` declaration.

> **Language finding.** `declarationUnderstatesBody` exists because revl
> checks emission *call sites* (G4) but does not propagate emission to the
> enclosing service operation — a consumer of `cache.put` cannot see from
> the declaration that calling it writes to the outside world. Inferring (or
> requiring) emission at the service boundary is the natural next step for
> the checker; the bridge compensates for now by trusting the body over the
> declaration. Tracked in docs/v2.0-roadmap.md.

## 2. `revl mcp import` — tools → revl

The reverse projection generates a `service` plus an extern-backed provider
skeleton. Trust runs the other way here, so the rule is blunt: **only an
explicit `readOnlyHint: true` avoids `emission`.** An absent annotations
block is not a read-only claim.

```bash
revl mcp import server-tools.json --service Tools --key tools --backend ts
```

```revl
service Tools {
  // Search the corpus
  fn query(q: Str, limit: Opt[Int]) -> Str
  // imported without a verifiable read-only claim
  emission fn write(path: Str, body: Str) -> Str
}
```

Generated sources compile as-is, and the imported surface appears in
`revl audit` under host code — imported trust is *visible* trust (G8).

## 3. `revl mcp serve` — the compiler as an MCP server

Newline-delimited JSON-RPC 2.0 over stdio, stdlib only. An agent gets a
typed protocol instead of filesystem access: every mutation it proposes runs
the same admission gate a human's `revl compile` does.

| tool | what it answers |
|---|---|
| `revl_check` | does this component compile? (summary + G8 boundary, or diagnostics) |
| `revl_admit` | may it enter **this running composition**? (ambient services, G2/G3 across both, interface drift) |
| `revl_audit` | what can this composition touch? |
| `revl_tools` | project its provided services to MCP tools (§1) |
| `revl_grammar` | the language surface, prompt-sized |

Rejections come back structured, so the agent reacts to a *code*, not prose:

```jsonc
{"ok": false, "diagnostics": [{
  "code": "T1", "category": "type-mismatch",
  "file": "…", "line": 3,
  "message": "`db.q` argument `sql` expects `Str`, got `Int`",
  "expected": "Str", "actual": "Int",
  "guarantee": "declared types are checked"
}]}
```

The same projection is available to humans and CI as
`revl compile --json-diagnostics`.

### Wiring it up

```jsonc
// claude_desktop_config.json / any MCP client
{"mcpServers": {"revl": {"command": "python", "args": ["-m", "revl", "mcp", "serve"]}}}
```

## Why this shape

The self-evolving-harness scenario (the paper's §1.2.2, and revl's reason to
exist) is exactly: *a component nobody reviewed enters a running system.*
The bridge makes that a protocol rather than a leap of faith — `revl_admit`
before a swap answers "may this enter?" mechanically, and the answer is the
compiler's, not the agent's own judgement. What the agent cannot do is more
important than what it can: it never touches the filesystem of a running
system, and it cannot describe its own tools as harmless when the compiler
says otherwise.
