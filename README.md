# revl

A research language for **spatiotemporal composability**: components that can
be loaded, unloaded, and hot-swapped in a running system, where "unloading
leaves no residue" and "dependencies stay coherent" are **compile-time
guarantees**, not runtime discipline.

revl is the language-level realization of the paradigm formalized in
[*A Programming Paradigm for Spatiotemporal Composability*](https://github.com/cordiverse/paper)
and implemented as a library by [Cordis](https://github.com/cordiverse/cordis).
The one-line pitch: **Cordis has revertible effects as a discipline; revl makes
them a type system** — the same jump C++ RAII made to become Rust's ownership.

```revl
component UserCache requires db: Database provides cache: Cache {
  let store = effect Map.new() undo store.drop()

  provide cache {
    fn get(key) = store.get(key)
    fn put(key, value) {
      effect store.insert(key, value)
      undo   store.remove(key)
      emit db.execute("INSERT INTO cache_log VALUES ($key)")
    }
  }
}
```

- Undeclared access won't compile. Mutations without an inverse (or an
  explicit `emit` admission of irreversibility) won't compile. Dependency
  cycles and provision conflicts are rejected at link time. Teardown cannot
  register effects, by construction.
- Backends: [cordis-py](https://github.com/geohotstan/cordis-py),
  [cordis](https://github.com/cordiverse/cordis) (TypeScript), and the
  cordis-wasm substrate. See [DESIGN.md](DESIGN.md) for the full design,
  the checked-guarantees table, and why raw native codegen is deliberately
  a non-goal.

**Status: v1.** The pipeline runs end-to-end on **three backends**:
`python -m revl compile` takes `.rvl` sources through parse → check →
link → IR ([docs/backend-ir-v1.md](docs/backend-ir-v1.md)), and the
emitters produce runnable components for **cordis-py**, **cordis** (TS),
and the **cordis-wasm substrate** ([backends/wasm/](backends/wasm/)) —
where confinement is enforced by the sandbox and `effect/undo` compiles
to a state machine with physical partial rollback. Divert-during-`await`
semantics (A1) are verified on all three. `python -m revl audit` prints a
composition's manifest and G8 boundary surface; `compile_files(...,
manifest=running)` is the runtime-admission gate. The rejection suite in
[examples/rejections/](examples/rejections/) is the checker's executable
spec, and [demo/](demo/) is a live file-watching hot-swap loop — edit a
`.rvl`, watch the running system recompile and swap it. The
[2.0 full-language proposal](docs/syntax-2.0.md) is the next frontier.

```bash
uv venv && uv pip install -e ".[test]" && .venv/bin/pytest tests/
```
