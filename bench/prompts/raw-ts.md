# raw Cordis (TypeScript) — the paradigm baseline

You are writing a **raw [Cordis](https://github.com/cordiverse/cordis) plugin**
in **TypeScript**. Cordis is the runtime revl compiles to; this task authors the
plugin **directly, by hand, with no revl**. It is the control arm of the
benchmark: the same component the other variants express in revl, written the
way anyone would write it in Cordis today.

Cordis mounts and unmounts plugins at runtime (unload, hot-swap). A correct
plugin **leaves nothing behind when it is unmounted** — everything it acquires
must be undone by its own teardown. Nothing in TypeScript enforces that: it is
on you to scope every listener, provision, effect, and sub-plugin to your own
fiber so Cordis reverts them on dispose.

## The Cordis lifecycle you must use

A plugin is a plain object with `name`, optional `inject`/`provide`, and an
`apply(ctx, config)` method (a bare `(ctx, config) => {}` function is also a
valid plugin). Everything you do goes through the `ctx` (a `Context`) you are
handed — **that `ctx` is your own fiber's scope**, and disposing the fiber runs
the disposers it collected, in LIFO order.

```ts
import type { Context } from 'cordis'
import { host } from './host.ts'

export const plugin = {
  name: 'PgDatabase',
  provide: ['db'],
  apply(ctx: Context, config: { url: string; pool_size?: number }) {
    ctx.effect(function* () {
      // acquire a resource; YIELD its disposer so teardown releases it
      const pool = host.Pool.open(config.url, config.pool_size ?? 10)
      yield () => pool.close()

      // provide a service; the yielded withdrawal is run on teardown
      yield ctx.provide('db', {
        query: (sql: string) => pool.query(sql),
        execute: (sql: string) => pool.execute(sql),
      })
    }, 'PgDatabase.body')
  },
}
```

The disciplines that keep a plugin residue-free — the exact contract Cordis'
unload relies on, and the thing this task measures:

- **Listeners** — bind with `ctx.on(event, fn)`. The `ctx` you were given
  collects the removal, so dispose unbinds it. Binding on a context that
  outlives you (`ctx.root.on(...)`, a captured parent context) leaks the
  listener forever.
- **Provisions** — publish services with `ctx.provide(name, impl)` and **yield
  its return value** from a `ctx.effect` generator (or otherwise register its
  withdrawal). A provision whose withdrawal is not tied to your fiber lingers in
  the reflect store after unmount.
- **Effects / acquired resources** — every `host.Pool`/`host.Map` handle (and
  any other acquisition) must have its release `yield`ed as a disposer inside
  `ctx.effect(function* () { ... })`. An acquisition with no matching disposer,
  or one registered on the root fiber, is never released.
- **Sub-plugins** — mount children with `ctx.plugin(child, config)` so they are
  **your** fiber's children and dispose with you. `ctx.root.plugin(...)` mounts
  a sibling of the root that survives your teardown — its registry entry, its
  provisions, and its effects all leak.

`ctx.effect(function* () { ... }, label)` runs the generator body; each `yield`
registers a disposer (a `() => void`, or the value returned by
`ctx.provide`/`ctx.on`). On dispose the disposers run in reverse order. This is
the shape revl emits, and the shape a hand-written plugin should match.

## Host resources (given — use verbatim)

Acquire host resources through the injected `host` object (imported exactly as
`import { host } from './host.ts'`). These are the same primitives the other
variants name; treat them as opaque handles:

- `host.Pool.open(url, size)` → a pool handle with `.close()`, `.query(sql)`,
  `.execute(sql)`.
- `host.Map.new()` → a map handle with `.get(k)`, `.insert(k, v)`,
  `.remove(k)`, `.drop()`.
- `host.Job.run(name)` → an awaitable cancellable job (use under `await` inside
  an `async` generator/apply when a spec calls for a boot/warmup boundary).

Host handles keep their own methods; there is no other stdlib. Acquiring a
handle without yielding its release (`.close()` / `.drop()`) is a resource leak.

## Services

The task pins each service interface in revl notation
(`fn name(param: Type) -> Type`, `emission fn ...`). Expose the **same
operations** from your Cordis `ctx.provide(name, { ... })` implementation, with
the obvious TypeScript signatures (`Str`→`string`, `Int`→`number`,
`Opt[Str]`→`string | undefined`, `List[Row]`→`Row[]`, `Unit`/no return→`void`).
An `emission fn` is an operation that crosses the system boundary (a real send,
an irreversible write); implement it as an ordinary method that performs that
call. A `fn` that returns `Opt[T]` returns `undefined` when absent.

## Multiple components / dependencies

Some specs describe more than one component, or a component that `requires`
another service. **Make your exported `plugin` self-contained**: it must mount
and fully activate on a *bare* `Context` with no external providers. If the spec
component requires a service, install a provider for that service inside your
plugin too (mount it as a child with `ctx.plugin(...)`, or provide it directly),
so the whole composition comes up when `plugin` is mounted and comes fully back
down when it is unmounted. A dependency that is only *declared* via `inject`
with no provider present will leave the fiber pending and never exercise your
lifecycle — provide what you depend on.

## Output contract

Reply with **exactly one fenced code block** (```ts) containing the complete
TypeScript module. It must:

- `import type { Context } from 'cordis'` and, if you use host resources,
  `import { host } from './host.ts'` — those two import lines verbatim, and no
  other imports.
- export the plugin the task describes as **`export const plugin = { ... }`**
  (this exact name is what gets mounted). Additional non-exported helper
  components/objects are fine.
- be plain erasable-syntax TypeScript: no `class`, no decorators, no top-level
  side effects — just the plugin definition.

No prose before or after the code block.
