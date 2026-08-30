# stc-go: Spatiotemporal Composability for Go — from a PL paper to a verified library

*2026-08-17 · v0.2.0 · [github.com/0xdenny218/stc-go](https://github.com/0xdenny218/stc-go)*

Earlier this year, DeepSeek open-sourced [DeepSeek Harness (dsh)](https://github.com/deepseek-ai/deepseek-harness),
an agent harness built on one idea: **everything is a plugin**. Tools, memory,
the model client, the UI — every piece is a component that can be loaded,
unloaded, and hot-replaced at runtime without the system losing consistency.

dsh achieves this by standing on [Cordis](https://github.com/cordiverse/cordis),
the TypeScript framework that powers the Koishi plugin ecosystem. And Cordis,
in turn, is an implementation of a paper:
[*A Programming Paradigm for Spatiotemporal Composability*](https://github.com/cordiverse/paper).

**stc-go is a Go implementation of that same paradigm** — specified by the
paper, not ported from Cordis, and verified by the paper's own five metatheory
theorems turned into property-based tests.

## The paradigm in one paragraph

Plugin systems traditionally fail in two directions. **Time**: when you unload
a plugin, its side effects — subscriptions, timers, registrations — leak,
because nothing tracks how to undo them. **Space**: when a plugin's dependency
disappears and reappears (config change, hot swap), nothing reliably tells the
dependent to reload, and when it does, it reloads at the wrong time or in the
wrong order.

The paper's answer: a single **context** type that is both a service container
and an *effect accumulator*. Every effect registers with an inverse; unloading
rewinds in exact LIFO order (**revertible effects** — temporal composability).
Components declare dependencies (`inject`); a runtime tracks satisfaction and
loss, moving each component instance (a **fiber**) through
`Pending → Loading → Active → Unloading` (**reactive coeffects** — spatial
composability).

## Why Go is a different beast

Cordis leans on JavaScript: `Proxy` for magical service access, declaration
merging for typed coeffects, and a single-threaded event loop that makes whole
classes of races unrepresentable. Go has none of these:

- **No `Proxy`** → service access goes through an explicit generic accessor,
  `stc.Service[T](ctx, key)` — the compile-time route the paper's §6.4
  explicitly endorses.
- **No unloadable plugin package** → components are statically registered Go
  values; runtime-loaded code comes in as **WASM** (via
  [wazero](https://github.com/tetratelabs/wazero)), where *instantiation is
  introduction and close is withdrawal*.
- **Real concurrency** → fiber transitions are serialized by a central
  orchestrator goroutine; `Apply` and inverses run off-lock in their own
  goroutines and report back as commands. The paper doesn't prescribe a
  concurrency model; this one makes its confluence theorem testable under
  `-race`.

## Acceptance = theorems, not vibes

Most "framework rewrites" declare victory when the examples run. stc-go's
acceptance criteria are the paper's five metatheory theorems, each implemented
as a property-based test (`property_test.go`) over randomly generated operation
sequences, run under the race detector:

| Theorem | Property checked |
|---|---|
| T59 Preservation | registry well-formedness after every operation |
| T61 Recovery exactness | state after unwinding ≡ state where the fiber never loaded |
| T63 Ordering | a fiber enters Loading only after its dependencies are ready |
| T66 Progress | quiescence within bounded orchestrator steps |
| T73 Confluence | quiescent end state independent of schedule order |

On top of that: Go's native fuzzing over interleavings
(`go test -fuzz FuzzInterleaving`), and an **84-agent adversarial review**
that reproduced 33 findings — five of them real critical/major defects that
example-driven development would never have caught: a lost-wakeup in the
subscribe-then-check window, a data race on fiber contexts, orphan fibers
during shutdown, a global-close footgun, and a missing well-formedness check
for duplicate providers. All fixed, each with a regression test, and the
resulting lifecycle contracts are documented in the README.

## Seeing it work: a plugin HTTP server with WASM hot reload

[`examples/plugin-http`](https://github.com/0xdenny218/stc-go/tree/main/examples/plugin-http)
is a small server where every feature is a fiber:

- routes are registered as revertible effects — unloading a fiber removes its
  routes *exactly*;
- an admin endpoint re-provides a `banner` service, and the `hello` plugin
  that injects it reloads automatically;
- a **TinyGo-compiled WASM guest** (written against the new
  [`guest` SDK](https://github.com/0xdenny218/stc-go/tree/main/guest))
  provides a string service; a Go bridge fiber injects it and serves it as
  `/wasm`;
- the new [`hmr` package](https://github.com/0xdenny218/stc-go/tree/main/hmr)
  watches `guest.wasm`, and every rebuild atomically swaps the running guest.

A real session:

```console
$ curl localhost:8080/wasm
hello from TinyGo guest v2
$ $EDITOR guest/main.go && make wasm      # change the string, rebuild
$ curl localhost:8080/wasm
hello from TinyGo guest v3                # swapped; bridge fiber reloaded
$ printf 'corrupted!' > guest.wasm        # simulate a broken build
$ curl localhost:8080/wasm
hello from TinyGo guest v3                # old version kept serving
```

The server log narrates the machinery doing its job:

```
[wasm-bridge] loaded, message="hello from TinyGo guest v3"
[hmr] guest reloaded
[hmr] reload failed, old version kept: wasm: update probe failed: invalid magic number
```

And this demo immediately earned its keep: the first hot swap of a
TinyGo-built guest failed with `module[main] has already been instantiated` —
toolchain binaries carry a fixed module name, which collided with the live old
instance during the atomic update's probe. Fixed (unique instance names) with a
regression test. No property test had caught it, because the property guests
are hand-encoded and nameless. **Theorems verify semantics; only end-to-end
demos verify the world.**

## Status and what's next

v0.2.0 is out: paradigm core (context, fibers, effects, isolation), the five
theorem tests, WASM components with atomic rollback, the TinyGo guest SDK,
the `hmr` watcher, and the example app. The API is v0.x — not frozen.

Documented next steps: Cordis-style event dispatch modes
(`emit/parallel/serial/bail/waterfall`) as a satellite package, plugin config
schemas, and two paper-level widenings (cascading dispose of nested fibers,
iterator-style continuous yield) that are scoped narrowings today.

If you're building a plugin system, an agent harness, or a hot-reload host in
Go — the thing Cordis gives the TypeScript world is now available as a Go
library, with theorems instead of promises.

- Repo: [github.com/0xdenny218/stc-go](https://github.com/0xdenny218/stc-go)
- Docs: [pkg.go.dev/github.com/0xdenny218/stc-go](https://pkg.go.dev/github.com/0xdenny218/stc-go)
- Paper: [cordiverse/paper](https://github.com/cordiverse/paper)
- Sibling implementations: [Cordis](https://github.com/cordiverse/cordis) (TypeScript),
  [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) (agent harness)
