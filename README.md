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
- v0 compiles to [cordis-py](https://github.com/geohotstan/cordis-py); the
  v1 backend target is the Wasm component model. See [DESIGN.md](DESIGN.md)
  for the full design, the checked-guarantees table, and why native codegen
  is deliberately not the first target.

**Status:** the v0 pipeline works end-to-end. `python -m revl compile`
takes `.rvl` sources through parse → check → link → IR; both backends
(cordis-py and cordis-ts, built in parallel against the frozen IR contract
in [docs/backend-ir.md](docs/backend-ir.md)) emit runnable components that
pass their R1–R5 semantics suites. The rejection suite in
[examples/rejections/](examples/rejections/) is the checker's executable
spec — one program per guarantee, each failing with the promised message.
Decisions and IR v1 amendments live in
[docs/contract-errata.md](docs/contract-errata.md).

```bash
uv venv && uv pip install -e ".[test]" && .venv/bin/pytest tests/
```
