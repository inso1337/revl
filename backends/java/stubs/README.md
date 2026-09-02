# cordis4j API stubs (compile gate)

The `io.cordis4j.core` surface the emitter targets, with minimal working
bodies. The javac gate in `test_emit_java.py` compiles emitted sources
against these stubs on every run — no clone or network needed.

**Validated against the real API** ([1na-ko/cordis4j](https://github.com/1na-ko/cordis4j)
@ `6c210e5`): every signature the emitter uses exists there with the same
shape; `test_runtime_scenarios_on_real_cordis4j` compiles cordis4j-core
from source and drives the full A1/G7 scenarios on the real runtime (CI
does this on every push). Two real-runtime facts the validation surfaced —
both now encoded in the emitter and mirrored here:

- `Disposables.composite` disposes **in the given order** (the emitter
  reverses the inverse list to get LIFO teardown). An earlier stub version
  reversed internally, which masked a real G7 bug.
- `ctx.effect()` returns an **orphan** scope — the runtime owns only what
  `apply` returns, so a failing activation must self-revert before
  rethrowing (A8); the emitter wraps activation bodies accordingly.

Two members exist here for the emitted **lifecycle-test** driver (roadmap
item 178(b)), both mirroring the real API rather than inventing one:

- `Contexts.create()` — the real runtime's root factory. Emitted lifecycle
  tests mint their root through it instead of `new Context()`, so one
  emitted source compiles and runs against these stubs *and* against
  cordis4j-core, where `Context` is an interface.
- `Context.plugin(Plugin)` — the real load/unload pair (it is what
  `scenarios/RunRealScenarios.java` drives on the real runtime). The stub
  activates the plugin directly and hands back the `Disposable` that
  activation returned.

Known stub-vs-real differences that don't affect emitted code: real
`Context` is an interface (which is why the root goes through `Contexts`),
real `track` returns its argument, real `intercept` returns a `Disposable`.
