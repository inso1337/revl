<div align="center">

<img src="assets/banner.svg" alt="revl, the agent-first programming language" width="820">

<p>
  <a href="https://github.com/inso1337/revl/actions/workflows/ci.yml"><img src="https://github.com/inso1337/revl/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <img src="https://img.shields.io/badge/runtimes-6-2dd4bf" alt="6 runtimes">
  <img src="https://img.shields.io/badge/emitted%20code-validated%20by%20real%20compilers-2dd4bf" alt="validated by real compilers">
  <img src="https://img.shields.io/badge/self--hosting-native-2dd4bf" alt="self-hosting">
  <img src="https://img.shields.io/badge/agent--native-MCP-a78bfa" alt="MCP native">
  <img src="https://img.shields.io/badge/license-MIT-blue" alt="MIT">
</p>

<p>
  <b><a href="#quickstart">Quickstart</a></b> ·
  <a href="#how-it-works">How it works</a> ·
  <a href="docs/README.md">Documentation</a> ·
  <a href="DESIGN.md">Design</a> ·
  <a href="docs/vision.md">Vision</a> ·
  <a href="docs/guide-ai-agents.md">For agents</a>
</p>

</div>

---

A research language for **spatiotemporal composability**: components that can be
loaded, unloaded, and hot-swapped in a running system, where "unloading leaves
no residue" and "dependencies stay coherent" are **compile-time guarantees**,
not runtime discipline.

revl is the language-level realization of the paradigm formalized in
[*A Programming Paradigm for Spatiotemporal Composability*](https://github.com/cordiverse/paper)
and implemented as a library by [Cordis](https://github.com/cordiverse/cordis).
The one-line pitch: **Cordis has revertible effects as a discipline; revl makes
them a type system** — the jump C++ RAII made to become Rust's ownership. What
this is *for*, and the honest scope of the claim, is [docs/vision.md](docs/vision.md).

```revl
service Database {
  emission fn execute(sql: Str) -> Int    // crosses the system boundary
}

service Cache {
  fn get(key: Str) -> Opt[Str]
  emission fn put(key: Str, value: Str)   // its body emits, so the interface says so
}

component UserCache requires db: Database provides cache: Cache {
  let store = effect Map.new() undo store.drop()   // every effect declares its inverse

  provide cache {
    fn get(key) = store.get(key)
    fn put(key, value) {
      effect store.insert(key, value)
      undo   store.remove(key)
      emit db.execute(`INSERT INTO cache_log VALUES (${key})`)
    }
  }
}
```

Undeclared access will not compile. A mutation without an inverse (or an
explicit `emit` admission of irreversibility) will not compile. Dependency
cycles and provision conflicts are rejected at link time. Teardown cannot
register effects, by construction.

## Quickstart

```bash
uv venv && uv pip install -e ".[test]" && .venv/bin/pytest tests/
```

```bash
python -m revl compile examples/user_cache.rvl   # source → checked IR → emitted component
python -m revl audit    examples/user_cache.rvl   # the G8 boundary surface
python -m revl mcp serve                          # the compiler as an agent admission gate
```

Then read the [agent guide](docs/guide-ai-agents.md), or `make demo` for a live
cross-tier hot-swap.

## How it works

<div align="center">

<img src="assets/architecture.svg" alt="one checked front-end, six hardened runtimes" width="880">

</div>

One front-end parses, checks, and links `.rvl` source into a single IR,
enforcing guarantees **G1–G8** before any code is emitted. Six emitters lower
that one IR to six runtimes, and the same compiler runs behind an MCP server, so
an AI agent proposing a component talks to the *admission gate*, not the
filesystem. Full design, the checked-guarantees table, and why raw native
codegen is a non-goal: [DESIGN.md](DESIGN.md).

- **Type-safe and null-safe.** Bidirectional checking, sound where declared.
  There is no `null`; absence is `Opt[T]`, and `T` never silently flows back
  out. The unchecked remainder (host objects, the extern boundary) is
  enumerated on the G8 audit surface, not implied.
- **Agent-native.** `revl mcp serve` exposes the compiler as an MCP server:
  `revl_check` / `revl_admit` instead of filesystem access, rejections as
  structured diagnostics, and tool safety hints (`readOnlyHint` /
  `destructiveHint`) *derived from the method body* — a tool cannot call itself
  harmless when it emits. [docs/mcp-bridge.md](docs/mcp-bridge.md).
- **Six tiers, one language.** cordis-py (reference), cordis (TypeScript), the
  cordis-wasm substrate, cordis-rs (Rust), cordis4j (Java), and cordis-go (Go).
  Components built for *different* runtimes compose in one running system across
  process boundaries. [docs/interop-bridge.md](docs/interop-bridge.md).
- **Self-hosting.** revl compiles itself: `selfhost/compile.rvl` runs a
  revl-native pipeline whose output is byte-identical to the reference compiler,
  with no reference in the chain. Two independent implementations of one
  grammar, each a check on the other. [docs/selfhost-findings.md](docs/selfhost-findings.md).

**The toolchain is the developer surface.** Because the author is increasingly
an agent, the compiler exposes far more than *compile / don't compile*:
`revl plan` (what a hot-swap would do), `revl query` (who emits to X? what
breaks if I withdraw C?), `revl why` (the derivation behind a rejection),
`revl test` with `fault`/`lifecycle` blocks, `revl swap`/`apply` for live
migration with derived rollback, and `revl recover` for crash recovery from a
write-ahead log. The complete per-command reference is
[docs/commands-reference.md](docs/commands-reference.md); the MCP verbs are
[docs/mcp-reference.md](docs/mcp-reference.md).

## Conformance

The claim that all six tiers agree is not asserted, it is **measured**:
`tools/conformance.py` emits every language construct through all six backends,
`--validate` hands each tier's output to that tier's *real* compiler (`tsc`,
`cargo check`, `javac`, `wasmtime`, a scope walk for Python), and CI gates the
result against drift. Today every host tier conforms with **zero real emit
gaps**; the only refusals are deliberate tier limits (the i32-only wasm
substrate, one Java arrow-type case). In-memory admission round-trip
(compile + gate) runs a median **0.165 ms** per candidate component.

The full construct-by-tier matrix, including the revl self-host column, lives in
**[docs/conformance.md](docs/conformance.md)** (regenerated by `make matrix`,
gated in CI).

## Documentation

The full index is **[docs/README.md](docs/README.md)**. Start here:

- **[DESIGN.md](DESIGN.md)** — the guarantees and the checked table · **[docs/vision.md](docs/vision.md)** — what this is *for*
- **[docs/syntax-2.0.md](docs/syntax-2.0.md)** — the full language reference · **[docs/stdlib-2.0.md](docs/stdlib-2.0.md)** — the specified stdlib
- **[docs/guide-ai-agents.md](docs/guide-ai-agents.md)** — the agent-facing guide · **[docs/mcp-bridge.md](docs/mcp-bridge.md)** — the compiler as an MCP server
- **[docs/conformance.md](docs/conformance.md)** — every construct against every tier · **[docs/crash-recovery.md](docs/crash-recovery.md)** — WAL roll-forward/back
- **[docs/v2.0-roadmap.md](docs/v2.0-roadmap.md)** — what is done and what is in flight · **[CONTRIBUTING.md](CONTRIBUTING.md)** — the workflow and the pre-commit contract

## Acknowledgments

revl is the language-level realization of [Cordis](https://github.com/cordiverse/cordis)
and the paradigm of its [paper](https://github.com/cordiverse/paper); it exists
because the runtime targets it lowers to do. With gratitude to the ecosystems it
stands on: [cordis-py](https://github.com/geohotstan/cordis-py),
[Cordis](https://github.com/cordiverse/cordis) (TypeScript),
[cordis-rs](https://github.com/dshbox/cordis-rs),
[cordis4j](https://github.com/1na-ko/cordis4j),
[cordis-wasm](https://github.com/inso1337/cordis-wasm), and
[stc-go](https://github.com/0xdenny218/stc-go), and to the toolchains that build
and validate every tier ([pytest](https://github.com/pytest-dev/pytest),
[TypeScript](https://github.com/microsoft/TypeScript),
[Wasmtime](https://github.com/bytecodealliance/wasmtime),
[Serde](https://github.com/serde-rs/serde), and more).

## License

[MIT](LICENSE).
