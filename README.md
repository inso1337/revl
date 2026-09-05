<div align="center">

<img src="assets/banner.svg" alt="revl: revertible effects as a type system" width="820">

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
  <a href="#what-makes-it-different">What makes it different</a> ·
  <a href="docs/README.md">Documentation</a> ·
  <a href="DESIGN.md">Design</a> ·
  <a href="docs/vision.md">Vision</a> ·
  <a href="docs/guide-ai-agents.md">For agents</a>
</p>

</div>

---

revl is a language for software that changes while it runs. Components
load, unload, and hot-swap inside a live system, and the properties that make
that survivable are checked at compile time: unloading leaves no residue,
dependencies stay coherent, nothing reaches state it never declared. The core
move is small and strict. Every mutation is written beside its inverse, and a
mutation with no inverse, one that crosses the system boundary, must carry an
`emit` marker at the call site. Irreversibility is legal; invisible
irreversibility is not.

The paradigm comes from [Cordis](https://github.com/cordiverse/cordis) and the
paper it implements, [*A Programming Paradigm for Spatiotemporal Composability*](https://github.com/cordiverse/paper).
The paper proves strong theorems about revertible effects, but each one rests
on hypotheses a library can only ask programmers to respect. revl moves those
hypotheses into the checker. C++ had RAII as a discipline and Rust made it a
type system; Cordis has revertible effects as a discipline and revl makes them
a language. The borrow checker governs lexical resource scope. revl's checker
governs dynamic component scope: what may enter a running system, what it may
touch while there, and what must be true when it leaves. What this project
claims, and what it deliberately does not, is [docs/vision.md](docs/vision.md).

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

Read that as a contract the compiler enforces. Drop the `undo` and it will not
compile. Call `db.execute` without the `emit` marker and it will not compile.
Reach for a service the component never required and it will not compile.
Declare `put` as a plain `fn` while its body emits and it will not compile: a
service declaration is an upper bound on what its providers may do. Teardown is
derived, LIFO over exactly the effects that ran, and an `undo` body has no way
to register new effects because the grammar gives it nowhere to put one. Link
time rejects dependency cycles and two providers of one key. The eight
guarantees, each anchored to the theorem hypothesis it discharges, are the
table in [DESIGN.md](DESIGN.md).

## Quickstart

```bash
uv venv && uv pip install -e ".[test]" && .venv/bin/pytest tests/
```

```bash
revl compile examples/user_cache.rvl   # source -> checked IR -> emitted component
revl audit    examples/user_cache.rvl   # everything that can cross the boundary
revl mcp serve                          # the compiler as an agent admission gate
make demo                               # a live hot-swap, migration and rollback
```

The first command installs the `revl` package editable; the documented happy
path then uses the `revl` console script (issue #336 closes the CWD-shadowing
window that a bare `python -m revl` (no `-P`) still has; issue #317 is the
underlying mechanism). The absolute-interpreter fallback is
`python -P -m revl` (PYTHONSAFEPATH, 3.11+) — the `-P` is the safety bit
that closes the rest of the window.

The language reference is [docs/syntax-2.0.md](docs/syntax-2.0.md); if the
component author is a model, start with the
[agent guide](docs/guide-ai-agents.md) instead.

## What makes it different

### The undo is checked, not hoped for

Undo logic is the classic write-only code path: written once, wrong quietly,
exercised at the worst possible moment. revl refuses the quiet part. Code
outside `effect` forms is pure, so the accumulator provably holds every
mutation, and the emission marker keeps the two kinds of change, revertible and
not, distinct in the types. When a component deactivates, the runtime replays
inverses in reverse order and the environment is exactly what it was before
activation. That property is what the whole language is shaped around.

### One front end, six live runtimes

<div align="center">

<img src="assets/architecture.svg" alt="one checked front-end, one IR, six runtimes, an MCP admission gate" width="880">

</div>

One front end parses, checks, and links `.rvl` into a single IR. Six emitters
lower that IR to six hardened Cordis runtimes: cordis-py (reference), cordis
(TypeScript), cordis-rs (Rust), cordis4j (Java), cordis-go (Go), and the
first-party cordis-wasm sandbox. This is not six ports of a demo. Components
built for different runtimes compose in one running system across process
boundaries, a Python component consuming a service a Rust component provides
([docs/interop-bridge.md](docs/interop-bridge.md)). And the claim that all six
tiers agree is measured, not asserted: every construct is emitted through every
backend and the output is handed to that tier's real compiler, `tsc`, `cargo
check`, `javac`, `wasmtime`. The construct-by-tier matrix lives in
[docs/conformance.md](docs/conformance.md), regenerated by `make matrix` and
gated in CI.

### The compiler is the agent's interface

If components are increasingly written by AI agents, the question that matters
is whether a generated component is safe to deploy into a system that is
already running. revl's answer is to make the compiler the admission gate.
`revl mcp serve` gives an agent `revl_check` and `revl_admit` instead of
filesystem access: drafts are held server-side, edited by delta, and nothing
lands until the same checker that guards human commits says yes. A rejected
candidate cannot deploy, and every rejection carries the guarantee it violated
plus the rewrite that fixes it. Tool safety annotations are derived from the
method body rather than asserted by an author, so a tool cannot call itself
read-only when it emits. In-memory admission runs a median 0.165 ms per
candidate, fast enough to sit inside a generation loop.
[docs/mcp-bridge.md](docs/mcp-bridge.md) has the shapes;
[docs/guide-ai-agents.md](docs/guide-ai-agents.md) has the workflow.

### Tooling that operates a running system

Because the compiler knows every effect and its inverse, it can answer
questions no ordinary toolchain can. `revl swap` migrates a live component to
another runtime tier, re-pointing every consumer across the cutover and ending
with a proof the old provider left nothing behind
([docs/swap.md](docs/swap.md)). `revl plan` shows the exact delta a hot-swap
would produce and `revl apply` executes it with a derived rollback, so a
mid-plan failure unwinds by inverses instead of by hand
([docs/plan.md](docs/plan.md), [docs/apply.md](docs/apply.md)). `revl why`
prints the derivation behind a rejection or a runtime transition
([docs/why-traces.md](docs/why-traces.md)). And since the effect accumulator
is an ordered list of actions paired with their undos, it persists as a
write-ahead log: `revl recover` walks it after a `kill -9` and reports, per
effect, what is moot, what compensates, and what must be undone
([docs/crash-recovery.md](docs/crash-recovery.md)).

### It compiles itself, and disagreement is a bug report

`selfhost/compile.rvl` is the revl compiler written in revl. Its output is
byte-identical to the reference compiler with no reference stage in the chain,
which makes self-hosting more than a stunt: two independent implementations of
one grammar run every input, and any divergence is a real defect in one of
them. The defects this differential oracle has already caught are written up in
[docs/selfhost-findings.md](docs/selfhost-findings.md).

The supporting cast is what you would expect from a checked language, done
plainly: bidirectional type checking, no `null` (absence is `Opt[T]` and never
silently unwraps), exhaustive `match`, and an extern boundary that must
classify itself as `pure`, `acquire`, or `emission` before it compiles, so
`revl audit` can enumerate everything a component could ever do to the world.

## Documentation

The full index is **[docs/README.md](docs/README.md)**. Start here:

- **[DESIGN.md](DESIGN.md)** for the guarantees and the checked table, **[docs/vision.md](docs/vision.md)** for what this is for and the honest scope of the claims
- **[docs/syntax-2.0.md](docs/syntax-2.0.md)** the language reference, **[docs/stdlib-2.0.md](docs/stdlib-2.0.md)** the stdlib surface
- **[docs/guide-ai-agents.md](docs/guide-ai-agents.md)** the agent workflow, **[docs/mcp-reference.md](docs/mcp-reference.md)** every MCP verb, **[docs/commands-reference.md](docs/commands-reference.md)** every subcommand
- **[docs/conformance.md](docs/conformance.md)** every construct against every tier, **[docs/crash-recovery.md](docs/crash-recovery.md)** the WAL and what honestly survives a crash
- **[docs/v2.0-roadmap.md](docs/v2.0-roadmap.md)** what is done and what is in flight, **[CONTRIBUTING.md](CONTRIBUTING.md)** the workflow and the pre-commit contract

## Acknowledgments

revl is the language-level realization of
[Cordis](https://github.com/cordiverse/cordis) and the paradigm of its
[paper](https://github.com/cordiverse/paper); it exists because the runtimes it
lowers to do. With gratitude to
[cordis-py](https://github.com/geohotstan/cordis-py),
[Cordis](https://github.com/cordiverse/cordis) (TypeScript),
[cordis-rs](https://github.com/dshbox/cordis-rs),
[cordis4j](https://github.com/1na-ko/cordis4j),
[cordis-wasm](https://github.com/inso1337/cordis-wasm), and
[stc-go](https://github.com/0xdenny218/stc-go), and to the toolchains that
build and validate every tier ([pytest](https://github.com/pytest-dev/pytest),
[TypeScript](https://github.com/microsoft/TypeScript),
[Wasmtime](https://github.com/bytecodealliance/wasmtime),
[Serde](https://github.com/serde-rs/serde), and more).

## License

[MIT](LICENSE).
