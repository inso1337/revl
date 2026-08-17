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
*assertions by the server author*; nothing checks them. revl takes them from
the `emission` classification — which is worth something only because the
compiler holds every provider to it.

That guarantee had a gap when this bridge was first built: emission was
checked at call sites but not propagated to the enclosing service operation,
so a projection that trusted the declaration could advertise a write as
read-only. The bridge briefly compensated by walking bodies; the right fix
was in the checker, and it shipped.

The reference example originally understated itself — `Cache.put` was
declared plain while its body emitted through `db` — and the checker now
refuses that:

```
examples/user_cache.rvl:33: `Cache.put` is declared plain, but this
implementation reaches `db.execute`
  a service declaration bounds what its providers may do — mark it
  `emission fn put(...)` in service `Cache`, or move the irreversible call
  out of this method (G4)
```

**The rule: a service declaration is an upper bound on its providers'
effects.** A provider may be *purer* than declared (declared `emission`,
body doesn't emit — the consumer already assumed the worst); it may never
be less pure. That direction is the sound one because consumers bind to the
*service*, not to a component, and providers are hot-swappable: a plain
declaration must mean "no provider of this operation reaches the boundary".

It also repairs a hole in G8 itself. `revl audit` enumerates a caller's
emissions by reading the declarations of the methods it calls — so an
under-declared operation made the audit *incomplete for every consumer*,
not just misleading to an MCP client.

The projection therefore trusts the declaration, and reports what the body
reaches as provenance:

```jsonc
"x-revl": {
  "classification": "emission",
  "annotationsDerivedFrom": "compiler",
  "effects": {
    "reachesEmission": ["db.execute"],
    "reachesHostCode": [],
    "boundedByDeclaration": true
  }
}
```

`openWorldHint` is derived from extern reachability (transitively through
`fn` calls) and `idempotentHint` from a `commutative` declaration.

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
