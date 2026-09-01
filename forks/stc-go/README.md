# stc-go

**English** | [简体中文](README.zh-CN.md)

[![CI](https://github.com/0xdenny218/stc-go/actions/workflows/ci.yml/badge.svg)](https://github.com/0xdenny218/stc-go/actions/workflows/ci.yml)
[![Go Reference](https://pkg.go.dev/badge/github.com/0xdenny218/stc-go.svg)](https://pkg.go.dev/github.com/0xdenny218/stc-go)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

**A Go implementation of the spatiotemporal composability paradigm** — the
programming model behind [Cordis](https://github.com/cordiverse/cordis)
(TypeScript) and [DeepSeek Harness (dsh)](https://github.com/deepseek-ai/deepseek-harness),
DeepSeek's "everything is a plugin" agent harness.

stc-go is specified directly by the paper
[*A Programming Paradigm for Spatiotemporal Composability*](https://github.com/cordiverse/paper)
(pinned at `948a07b`, 2026-08-14 draft) — it is **not a port of Cordis**. Cordis
(pinned at `8cc9e33`) serves only as a reference and a source of test scenarios.
The acceptance criteria are the paper's five metatheory theorems, implemented as
property-based tests.

## The paradigm at a glance

- **Temporal composability** — every side effect a component registers carries
  an inverse; unloading a component rewinds its effects in exact LIFO order
  (revertible effects).
- **Spatial composability** — components declare their dependencies (inject);
  the runtime reactively tracks satisfaction and loss of those dependencies,
  moving each fiber between Pending/Loading/Active/Unloading
  (reactive coeffects).
- Both dimensions unify in a single **context** type: a context is both a
  service container and an effect accumulator.

This is what lets a plugin host hot-reload a component — Go or WASM — with
provable cleanup: no leaked subscriptions, no stale services, no dangling
state, and dependent components reload automatically.

## Install

```sh
go get github.com/0xdenny218/stc-go
```

WASM component loading (optional) lives in the subpackage:

```go
import "github.com/0xdenny218/stc-go/wasm"
```

## Quick start

```go
package main

import (
	"context"
	"fmt"

	stc "github.com/0xdenny218/stc-go"
)

var greeting = stc.NewKey[string]("greeting")

func main() {
	root := stc.New()
	defer root.Close()

	// Loaded first, but stays Pending: its dependency isn't satisfied yet.
	consumer := root.Load(stc.Component{
		Name:   "consumer",
		Inject: []stc.Key{greeting},
		Apply: func(c *stc.Context) (stc.Inverse, error) {
			msg, err := stc.Service[string](c, greeting)
			if err != nil {
				return nil, err
			}
			fmt.Println("consumer saw:", msg)
			return nil, nil
		},
	})

	// Provide registers its own inverse; unloading rewinds it automatically.
	root.Load(stc.Component{
		Name:    "provider",
		Provide: []stc.Key{greeting},
		Apply: func(c *stc.Context) (stc.Inverse, error) {
			_, err := c.Provide(greeting, "hello, spatiotemporal world")
			return nil, err
		},
	})

	// Becomes Active only after greeting is provided (Theorem: Ordering).
	if err := consumer.Ready(context.Background()); err != nil {
		panic(err)
	}
}
```

## Theory mapping (paper §5.1, Table 2)

| Paper construct | Symbol | stc-go |
|---|---|---|
| context (first-class) | Γ∞ | `Context` (scoped tree, `New`/`Child`) |
| revertible effect | e ∈ 𝔈Γ | `Context.Effect(install)` |
| context read/write | get(k) / set(k,v) | `Context.Get` / `Context.Set` |
| service provision | provide | `Context.Provide` (auto-registers undo) |
| coeffect read | d (inject) | `Component.Inject` + `stc.Service[T]` |
| isolation | isolate(k, r) | `Context.Isolate(key, realm)` |
| interception | intercept(k, ν) | `Context.Intercept(key, meta)` |
| component instance | ⟨d,p,e,π,σ,τ,θ⟩ | `Fiber` (`Load` creates, `Dispose` withdraws) |
| registry | dom(Fγ) | fiber registry per tree; snapshot via `Context.Fibers()` |
| fiber state | τ | `Pending → Loading → Active → Unloading → (Pending \| Failed)`; explicit `Dispose` → `Gone` |

## Lifecycle contracts (the important bits)

- **`Close` is root-only**: it shuts the orchestrator down and rewinds the
  whole tree. For subtree-only cleanup on a non-root scope, use `Release`.
- **Same-key replacement must wait for `Gone`**: before reloading a provider of
  an already-provided key, `Dispose` the old one and wait for its `Gone` to
  return. Overlapping duplicates are rejected with `ErrDuplicateProvide`
  (fail-fast enforcement of the paper's Def. 58 well-formedness).
- **`Fiber.Context()` returns the current load cycle's context**; inertial
  reloads replace it, so observing a previous (already-rewound) cycle's
  context is a legal race outcome.
- **`Gone()` returns once the fiber leaves the registry** (Gone or Failed);
  **`Ready()`** returns on Active / Failed / Gone (nil / load error /
  `ErrDisposed` respectively).
- **`Context.Fibers()` enumerates the tree's registry**: a read-only,
  ID-ordered snapshot of every loaded-but-not-yet-withdrawn fiber (one
  registry per `New()` tree; disposed and failed fibers have already left,
  so they only vanish from later snapshots — no registration side needed).
- **`Load` is asynchronous**: it returns a handle immediately and `Apply`
  runs later on the orchestrator. If a fiber's `Apply` immediately consumes
  what sibling fibers provide (e.g. starts a serving loop), await the
  providers' `Ready` before loading it — otherwise its first actions can
  observe a not-yet-registered world (bootstrap ordering).

## Verification = the five metatheory theorems (paper §4.4)

`property_test.go` turns each theorem into a property-based test:

| Theorem | Property |
|---|---|
| T59 Preservation | registry well-formedness holds after every operation |
| T61 Recovery exactness | state after unwinding a fiber ≡ state where it never loaded |
| T63 Ordering | a fiber enters Loading only after its dependencies are ready |
| T66 Progress | quiescence within a bounded number of orchestrator steps |
| T73 Confluence | quiescent end state is independent of schedule order (`-race` + randomized schedules) |

```sh
go test -race ./...
go test -run Property -fuzz FuzzInterleaving -fuzztime 10s ./...
```

## WASM components (`stc-go/wasm`)

Module instantiation = introduction, module close = withdrawal (the paper's
§6.4 runtime-code route): dependency gating, inertia locks and exact rewinding
apply to WASM guests exactly as to Go components.

- `wasm.Runtime` wraps [wazero](https://github.com/tetratelabs/wazero)
  (interpreter config, platform-independent) plus an `stc` host module;
  `wasi_snapshot_preview1` is always instantiated, so toolchain-built
  guests work out of the box. Guests participate via exported
  `start()`/`stop()` (reactor-mode `_initialize()` is honored before
  `start`); host functions `provide/get/get_size/log` register services on
  the fiber's own context — rewind on unload is guaranteed by the core
  mechanism, guests register no cleanup themselves.
- `wasm.Load` probes (compile + trial instantiation) before loading;
  `Handle.Update` performs atomic hot-swap: probe failure leaves the old
  version intact, a start trap rolls back to the old bytes automatically.
- `Handle.Call(ctx, name, arg)` calls an exported guest function over an
  all-string ABI (the guest exports `stc_alloc`; the callee takes
  `(ptr, len)` and returns the result packed as `(ptr<<32)|len`; an optional
  `stc_free` releases both buffers). Call and Update hold the same lock: an
  in-flight call finishes untorn, Update waits, and calls placed after an
  Update land on the new version.
- Acceptance (`wasm/wasm_test.go`, `wasm/call_test.go`): the three HMR
  contracts (reload, cross-boundary dependency chain, failure rollback) +
  spec Test/WasmRollback + T61 cross-boundary unload exactness + the Call
  contracts (round-trip, buffer release, bad-build keeps serving,
  Update–Call mutual exclusion under `-race`).
- Test guests are hand-encoded WASM binaries (a tiny encoder in
  `guest_test.go`) — zero toolchain dependencies.

### Writing guests in Go (`stc-go/guest`)

Guests are plain Go compiled with [TinyGo](https://tinygo.org/) against the
guest-side SDK:

```go
//go:build wasm

package main

import "github.com/0xdenny218/stc-go/guest"

//export start
func start() {
	_ = guest.Provide("wasm-message", "hello from a guest")
}

//export stop
func stop() { guest.Log("bye") }

func main() {} // reactor mode: entry points are start/stop
```

```sh
tinygo build -target wasip1 -buildmode=c-shared -o guest.wasm .
```

For host→guest calls the SDK itself exports `stc_alloc`/`stc_free` and the
`invoke` entry point — a guest just registers a handler, no extra `//export`
boilerplate:

```go
func init() {
	guest.OnInvoke(func(args string) string { return `{"echo":` + args + `}` })
}
```

and the host invokes it with `handle.Call(ctx, "invoke", args)`.

### Hot reload (`stc-go/hmr`)

`hmr.Watch(ctx, handle, "guest.wasm")` watches the file (directory-level,
survives atomic-save renames), debounces, and runs `Handle.Update` on every
change. Failed updates keep the old version serving; results are reported
through an `OnReload` callback.

### Watch primitive (`stc-go/watch`)

`watch.Watch(ctx, path, opts)` is the minimal debounced file-watching
primitive underneath `hmr`: events on a file or directory settle for a
debounce window (default 200 ms), then `OnFire` fires once with the last
event's path and op (create/write/remove/rename). Deliberately no diffing,
no domain semantics — the consumer decides what a fire means (stat for
reload-or-gone, or rescan a directory and diff). File mode watches the
parent directory (atomic-save rename safe); directory mode watches the
directory itself. Extracted from two hand-rolled fsnotify loops in
stc-agent's skills package through the reflux review
([#6](https://github.com/0xdenny218/stc-go/issues/6)).

## Stable registries (`stc-go/registry`)

The **stable registry** pattern: one fiber provides a registry whose key
identity never changes (empty `Inject`, so it survives every cascade), and
member fibers register themselves as revertible effects (inverse =
unregister). Consumers read the current view per use, so membership churn
never reloads them — this is what lets
[stc-agent](https://github.com/0xdenny218/stc-agent) hot-swap tools
mid-conversation without the agent loop noticing.

```go
var KeyTools = stc.NewKey[*registry.Registry[Tool]]("tools")

// one stable provider fiber
root.Load(registry.Component[Tool]("toolset", KeyTools))

// each member fiber: registration is a revertible effect
stc.Component{
	Name:   "tool:" + t.Name,
	Inject: []stc.Key{KeyTools},
	Apply: func(c *stc.Context) (stc.Inverse, error) {
		ts, err := stc.Service[*registry.Registry[Tool]](c, KeyTools)
		if err != nil {
			return nil, err
		}
		return ts.Register(t.Name, t), nil
	},
}
```

`Register` returns the unregistering inverse (idempotent); re-registering a
name replaces the value, and a superseded inverse never deletes the newer
registration. `Lookup` / `List` / `Names` give the current view (`List` is
sorted by name). Extracted from two independent uses in stc-agent (tool
registry and slash-command registry) through the reflux review
([#2](https://github.com/0xdenny218/stc-go/issues/2)).

## Example application

[`examples/plugin-http`](examples/plugin-http) is a plugin HTTP server where
every feature is a fiber: routes as revertible effects (exact removal on
unload), cascading reloads on service re-provision, a TinyGo WASM guest
serving a string through a Go bridge fiber, and live hot reload on rebuild.
Runs out of the box (`go run .` in that directory, prebuilt guest committed);
see its README for a guided tour.

## Relation to Cordis and DeepSeek Harness

The spatiotemporal composability paradigm has one specification (the paper)
and several implementations:

| Project | Language | Role |
|---|---|---|
| [cordiverse/paper](https://github.com/cordiverse/paper) | — | the specification (preprint, actively revised) |
| [cordiverse/cordis](https://github.com/cordiverse/cordis) | TypeScript | reference implementation; drives the [Koishi](https://koishi.chat) plugin ecosystem |
| [deepseek-ai/deepseek-harness](https://github.com/deepseek-ai/deepseek-harness) | TypeScript | DeepSeek's agent harness (dsh), "everything is a plugin", driven by Cordis |
| **stc-go** (this repo) | Go | independent implementation; paper as spec, five theorems as acceptance |

If you are building a plugin system, an agent harness, or a hot-reload host in
Go and want the same composition guarantees that Cordis gives the TypeScript
world (dependency-gated loading, exact effect rewind, reactive reload of
dependents), stc-go is that library.

Not ported from Cordis (by design): the four event-dispatch modes
(`emit/parallel/serial/bail/waterfall`), `ctx.plugin()` with config schemas,
and the `hmr`/`loader` satellite packages are Cordis ecosystem concerns, not
paradigm core. Go replacements are idiomatic: explicit typed accessors instead
of `Proxy` + declaration merging, static component registration instead of the
Go plugin package (which cannot unload), and WASM for runtime-loaded code.

## Related projects

stc-go sits in a specific layer — **runtime dependency reactivity with provable
unload semantics** — that is currently empty in the Go ecosystem. Adjacent
projects solve neighboring layers, and compose with stc-go rather than compete:

| Layer | Projects | They solve | They don't |
|---|---|---|---|
| Startup-time DI | [uber/fx](https://github.com/uber-go/fx), [google/wire](https://github.com/google/wire), [sarulabs/di](https://github.com/sarulabs/di) | static dependency graphs, app-level lifecycle hooks | runtime (un)provision, cascade reload of dependents |
| WASM plugin loading | [Extism](https://github.com/extism/extism), [knqyf263/go-plugin](https://github.com/knqyf263/go-plugin) | sandboxed loading/calling of WASM plugins, codegen, OCI distribution | inter-plugin dependencies, exact unload semantics |
| Process plugins | [hashicorp/go-plugin](https://github.com/hashicorp/go-plugin) | crash isolation via subprocess + gRPC | in-place hot swap, dependency graph |
| Dev-time reload | [air](https://github.com/air-verse/air), [edwingeng/hotswap](https://github.com/edwingeng/hotswap) | restarting/swapping Go code while developing | state preservation, rollback, dependency tracking |

stc-go's WASM layer is built on [wazero](https://github.com/tetratelabs/wazero) —
the same runtime Extism and go-plugin use. The layers are complementary:
mechanisms like typed host↔guest calls, sandbox hardening, and OCI artifact
distribution are natural future integrations, pulled in as real consumers
demand them.

## Documented deviations from the paper / Cordis

- No `Proxy`: coeffect access goes through the explicit generic
  `stc.Service[T]` (the compile-time route the paper's §6.4 endorses).
- Concurrency model: a single RWMutex plus a central orchestrator goroutine
  serializes fiber transitions; `Apply`/inverses run outside the lock in their
  own goroutines (the paper does not prescribe a concurrency model).
- Nested child fibers do not cascade-dispose with their parent (a scoped
  narrowing, documented in the project spec).
- Duplicate provide of the same key is excluded from the confluence guarantee
  (matching the theorems' conditional statements).
- Effect accumulation happens at registration (return values of
  `Effect`/`Apply`); the paper's iterator-style continuous yield is not
  implemented — no acceptance scenario depends on it.

## License

[MIT](LICENSE)
