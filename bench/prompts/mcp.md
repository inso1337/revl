# MCP tool pack (TypeScript, @modelcontextprotocol/sdk)

You are writing a **tool pack for a Model Context Protocol server**, in
**TypeScript**, against `@modelcontextprotocol/sdk` 1.30.0. The host is a
long-running MCP server that loads and unloads tool packs while it keeps
serving clients. Your pack is loaded by calling its `install` function with the
host's `McpServer`, and later unloaded.

A correct pack **leaves nothing behind when it is unloaded**: every tool it
registered is gone from the server's lists, every host resource it acquired is
released, and nothing it started (a listener, a timer) is still running.
Nothing in the SDK enforces that. It is on you.

## How the host loads and unloads a pack

Loading: the host calls `install(server, config)` once.

Unloading, in this order:

1. The host calls `remove()` on **every handle** your `install` obtained from
   `server.registerTool`, `server.registerResource` or `server.registerPrompt`
   while it ran. `remove()` deletes that registration from the server and
   notifies clients. It calls nothing on your handler: a tool that acquired
   something is not told it is being removed.
2. If `install` **returned a function**, the host then calls it (and awaits it
   if it returns a promise). This is the only place your pack is told it is
   being unloaded, so it is where you release what you acquired.

A registration made after `install` has returned (from a timer, a callback, a
promise that resolves later) is not seen by the host and is never removed.

```ts
import type { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js'
import { z } from 'zod'
import { host } from './host.ts'

export function install(server: McpServer, config: { url?: string; pool_size?: number }) {
  // acquire a resource
  const pool = host.Pool.open(config.url ?? 'pg://localhost/app', config.pool_size ?? 10)

  // one tool per service operation, named <service>_<operation>
  server.registerTool('db_query', {
    description: 'Run a read query',
    inputSchema: { sql: z.string() },
  }, async ({ sql }) => ({
    content: [{ type: 'text', text: JSON.stringify(pool.query(sql)) }],
  }))

  server.registerTool('db_execute', {
    description: 'Run a statement',
    inputSchema: { sql: z.string() },
  }, async ({ sql }) => {
    pool.execute(sql)
    return { content: [{ type: 'text', text: 'ok' }] }
  })

  // the teardown the host calls after removing the tools
  return () => pool.close()
}
```

The disciplines that keep a pack residue-free, which is what this task
measures:

- **Registrations**: register every tool, resource or prompt synchronously
  inside `install` (or before a promise `install` returns resolves), so the
  host holds its handle.
- **Resources**: every `host.Pool` / `host.Map` / `host.Job` handle you acquire
  must be released (`.close()` / `.drop()` / `.cancel()`) by the function
  `install` returns.
- **Listeners**: a listener on anything that outlives the pack (`process`, a
  shared emitter) must be removed by that function.
- **Timers**: an interval or a pending timeout must be cleared by that
  function.

## Host resources (given, use verbatim)

Acquire host resources through the injected `host` object (imported exactly as
`import { host } from './host.ts'`). They are the same primitives the other
hosts in this benchmark use; treat them as opaque handles:

- `host.Pool.open(url, size)` returns a pool handle with `.close()`,
  `.query(sql)`, `.execute(sql)`.
- `host.Map.new()` returns a map handle with `.get(k)`, `.insert(k, v)`,
  `.remove(k)`, `.drop()`.
- `host.Job.run(name)` returns an awaitable cancellable job with `.cancel()`.

There is no other stdlib. Acquiring a handle without releasing it is a
resource leak.

## Services

The task pins each service interface in revl notation
(`fn name(param: Type) -> Type`, `emission fn ...`). Expose **each operation
as one tool** named `<service>_<operation>` in lower case (for `service Kv`
with `fn get`, the tool is `kv_get`). Map the parameters to a zod input shape
(`Str` to `z.string()`, `Int` to `z.number().int()`, `Bool` to `z.boolean()`,
`List[T]` to `z.array(...)`). A tool returns
`{ content: [{ type: 'text', text: <the result as text> }] }`; an operation
with no result returns the text `ok`, and an `Opt[T]` that is absent returns
an empty string. An `emission fn` is an operation that crosses the system
boundary (a real send, an irreversible write): implement it as an ordinary
tool that performs that call.

## Multiple components and dependencies

Some specs describe more than one component, or a component that requires
another service. **Make your pack self-contained**: `install` must bring the
whole composition up on a bare server with no other packs present, and the
function it returns must bring all of it back down. If one part depends on
another, create the dependency inside `install` and pass it along.

## Output contract

Reply with **exactly one fenced code block** (```ts) containing the complete
TypeScript module. It must:

- use only these imports, verbatim, and only the ones you need:
  `import type { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js'`,
  `import { z } from 'zod'`, `import { host } from './host.ts'`;
- export **`install`** as a function taking `(server, config)`;
- be plain erasable-syntax TypeScript: no `class`, no decorators, no `enum`,
  no top-level side effects beyond the definitions.

No prose before or after the code block.
