# revl → cordis-go backend

Emits idiomatic Go targeting **[stc-go](https://github.com/0xdenny218/stc-go)**
— a Go implementation of the same spatiotemporal-composability paradigm revl
compiles for (specified by the same paper, accepted against the same five
metatheory theorems). `emit(ir) -> str` produces one Go source file per IR.

Unlike golden-text backends, this one is **executed**: emitted components run
on the real stc-go runtime under `go test` (`scenarios/emitted/`), and the
hand-written contract reference (`scenarios/reference*.go`) is the executable
oracle the emitter targets.

## Pinned target

stc-go is a moving draft dependency. This backend is built and executed against:

    github.com/0xdenny218/stc-go
    commit b3d6788a428ee4f58c7a6adf437fdc47e83883be   (one commit past tag v0.6.0)
    go.mod pin: v0.6.1-0.20260818143352-b3d6788a428e

`scenarios/go.mod` records the pin; Go 1.26.5 was used here (stc-go requires
Go ≥ 1.25).

## Mapping (DESIGN.md §7 — the backend contract)

| revl | Go (stc-go) |
|---|---|
| `service S` | `type S interface { <M>(…) … }` |
| `component C` | `func C(cfg) stc.Component` (an `Apply` closure) |
| `requires k: S` | `Inject: []stc.Key{_keyK}` + `stc.Service[S](ctx, _keyK)` |
| `provides k: S` | `type C_k struct{…}` impl + `ctx.Provide(_keyK, S(impl))` |
| `effect E undo U` | `ctx.Effect(func() stc.Inverse { E; return func() error { U; return nil } })` |
| `let x = effect …` | `var x *Host; ctx.Effect(func() stc.Inverse { x = …; return … })` |
| `isolate k in realm("R")` | load-site `ctx.Isolate(_keyK, _revlRealm("R"))` |
| `intercept k with {…}` | load-site `ctx.Intercept(_keyK, map[string]any{…})` |
| `emit m.f(…)` | a plain method call |
| `format \`…$0…\`` | `fmt.Sprintf("…%v…", …)` |
| config `f: T = d` | `type CConfig struct{ F T }` + `DefaultCConfig()` |
| `Opt[T]` (return) | `(T, bool)` |

Service keys are interned per binding name (`_keyDb = stc.NewKey[Database]("db")`)
so a provider's `provide db` and a consumer's `requires db` resolve the same
key. Host objects (`Pool`, `Map`) are a minimal, recording in-memory runtime
so scenarios can assert exact effect/undo order. The host `Map` is generic over
its value type (`type Map[V any]`, item 113): a `Map.new()` acquisition pins
`V` from the component's `insert` sites, so `Map[Str, Int]` / `Map[Str, List[_]]`
carry their declared value type (`MapNew[int]()`, `MapNew[[]string]()`) instead
of a hardcoded string. `scenarios/emitted/{counter,tagger}` prove the int and
list round-trips by RUNNING on stc-go.

### Realm placement is done at the load site (not inside `Apply`)

`isolate k in realm("R")` lowers to isolating the **load-target** context in
the emitted `Load<Name>` helper, *before* the fiber is loaded — never inside
the component body. stc-go evaluates a fiber's `Inject` gate against its
load-target context; isolating inside `Apply` runs *after* that gate has
already been evaluated on the un-isolated (root-realm) context, so a
realm-scoped consumer would hang in `Pending` forever. Providers and consumers
naming the same realm must share the same `*stc.Realm` pointer (stc-go keys
provisions by `(pointer, key)`, not by name), so `_revlRealm` interns realms by
name. This is the Go analog of the Rust backend's `_revl_realm` fix.

## Coverage

- **ir_version 1 & 2** components: effect/inverse, `let`-effect binds, config
  with defaults, provide/inject, provide-method bodies (`return` / `effect` /
  `emit` / `format`), block-level effects, `isolate`/realms, `intercept`.
- **Instance-parametric `spawn`** (ir_version 3, docs/design-v2-instances.md
  phase 1): a `spawn` acquisition lowers to a `revlSpawn<Target>` helper that
  plugs the target *template* as a **child fiber** of the spawner, each
  provided key isolated into a **fresh local realm** (a distinct `*stc.Realm`
  minted per spawn — never interned by name like a global realm — so two
  instances of one component never collide on a provision). The bound handle
  (`RevlSpawnHandle`) reclaims exactly that instance on `Dispose()`, running
  the instance's own LIFO teardown; disposal is idempotent, so the spawner's
  `undo w.dispose()` is a harmless no-op once the instance is gone and a
  safety net otherwise (an un-disposed instance cannot outlive its spawner).
  Proven on the real stc-go runtime by `scenarios/emitted/spawn/`.
- **Out of scope** (rejected with a clear `EmitError`): ir_version 3 pure
  functions/records/variants/match.

## Verify

```bash
# regenerate the checked-in emitted Go from source IR
bash backends/go/regen.sh

# compile gate + run emitted code on the real stc-go runtime
cd backends/go/scenarios && go test ./...

# emitter structural unit tests (compile-time complement)
pytest backends/go/test_emit_go.py -q
```

`scenarios/reference_test.go` proves the contract by hand against stc-go (LIFO
teardown, fail-revert, reactive provide/inject, realm isolation);
`scenarios/emitted/*/gen_exec_test.go` proves the **emitted** code exhibits the
same behavior.

## Upstream finding (stc-go, not worked around)

**Reactive teardown ordering differs from cordis-rs.** When a provider is
withdrawn, stc-go removes the provide entry and runs the *provider's own*
inverses as part of provider disposal, and only then does the orchestrator
observe the withdrawal and tear down the dependent consumer — so
`provider:down` precedes `consumer:down`. cordis-rs guarantees the opposite
(the dependent tears down first, while its dependency is still provided, so it
can use the service during its own teardown — cf. the `user_cache` example's
"db still readable during its teardown"). stc-go keeps the consumer safe only
because a resolved service is captured by Go reference and outlives its
provide-registration, not because of teardown ordering. The revl invariant
that *does* hold on stc-go is reactive deactivation with no torn state (every
`do:` has its `undo:`). Flagged as an upstream candidate (fork + PR with human
sign-off), **not** worked around in the emitter.
