# revl for humans

A practical guide to writing revl 2.0.

**revl** is a language for *spatiotemporal composability*: writing components
that can be loaded, unloaded, and hot-swapped in a running system — where
"unloading leaves no residue" and "dependencies stay coherent" are
**compile-time guarantees**, not runtime discipline.

The one-line pitch: *Cordis has revertible effects as a discipline; revl makes
them a type system* — the jump C++ RAII made to become Rust's ownership. Rust's
borrow checker governs *lexical* resource scope; revl's checker governs
*dynamic component* scope.

## The mental model

A revl program is a set of **components**. A component:

- **requires** services (its inputs — what it reads),
- **provides** services (its outputs — what it publishes),
- and has an **activation body**: a sequence of *revertible effects*
  (`effect E undo U`) that runs when its requirements are satisfied.

When a component deactivates (its provider leaves, or it's hot-swapped out),
every effect it acquired is undone — in derived, LIFO order. You never write
`activate()`/`deactivate()`. You write what to do and the inverse of each thing
you did; the runtime derives the teardown.

### A worked example

```revl
service Database {
  fn query(sql: Str) -> List[Row]
  emission fn execute(sql: Str) -> Int          // crosses the system boundary
}

service Cache {
  fn get(key: Str) -> Opt[Str]
  fn put(key: Str, value: Str)
}

component UserCache requires db: Database provides cache: Cache {
  let store = effect Map.new() undo store.drop()

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

Reading it:

- `service Database { ... }` — the interface behind a coeffect key.
  `emission fn` marks an operation that *cannot be reverted* (bytes left the
  system), so it must be called with `emit` and appears on the audit surface.
- `requires db: Database provides cache: Cache` — the component's whole
  interface to the world. Nothing else is reachable; `db` is in scope, typed
  `Database`, and stays readable throughout the component's own teardown.
- `let store = effect Map.new() undo store.drop()` — acquire a map, remember
  how to drop it. On teardown, `store.drop()` runs.
- `provide cache { ... }` — publish the `cache` key. `put` mutates the store
  (effect + undo) and *emits* a log write.
- `` `...${key}` `` is the 2.0 template-string interpolation (1.x `"$key"` is
  gone — `revl fmt --migrate` rewrites old sources).

## The four strata

revl 2.0 is four strata, each with its own rules ([docs/syntax-2.0.md](syntax-2.0.md)):

| stratum | what it is | rules |
|---|---|---|
| 1. Pure expressions & functions | a TypeScript subset | totality of the subset |
| 2. Types & data | revl's own, host-neutral | structural checker |
| 3. Components & effects | revl's own (unchanged from 1.x) | G1–G8, A1–A8 |
| 4. Host blocks | verbatim host language | boundary types + G8 audit |

The governing principle: **same meaning → same syntax** (stratum 1 borrows
TypeScript verbatim so it's instantly familiar); **different meaning →
different syntax** (strata 2–4 are revl's own, so the paradigm's constructs
are visually unmistakable).

## The language

### Types (§2)

```revl
type Row = { id: Int, name: Str, active: Bool }       // record (structural)
type Pair[A, B] = { first: A, second: B }             // generic
type TokenKind = Ident | Keyword | IntLit | StrLit    // enum (no payload)
type Outcome = Ok(Row) | NotFound | Invalid(Str)      // ADT (payloads)

// built-ins: Str, Int, Float, Bool, Bytes, Unit
// containers: List[T], Map[K, V], Opt[T], Result[T, E]
// sugar: T?  ==  Opt[T]
```

- Records are structural; ADTs are nominal (defined by their case names).
- **There is no `null`.** Absence is `Opt[T]`: `Some(value)` or `None`. `T`
  flows into `Opt[T]` automatically; `Opt[T]` does **not** flow back into `T`
  — you unwrap with `match` (or `??`).

### Functions & expressions (§3)

```revl
fn count_idents(kinds: List[Str]) -> Int {
  var n = 0
  for (kind of kinds) {                       // TS for-of
    if (kind == "Ident") n += 1               // += on var
  }
  return n
}

fn describe(outcome: Outcome) -> Str {
  return match outcome {                      // exhaustiveness-checked
    Ok(row)      => row.name,
    NotFound     => "-",
    Invalid(why) => why,
  }
}
```

- `fn` is a pure function; `pub fn` exports it. `let` is single-assignment;
  `var` is local mutable (and never escapes — a lambda captures its *current
  value*, not the cell).
- Loops, `if`, arrow lambdas (`x => x + 1`), records, lists, indexing, calls,
  `==`/`===` (identical), `!=`/`!==`, `&&`/`||`, the ternary — TypeScript's
  syntax, verbatim.
- `match` is the ADT eliminator. The checker requires every case (or a `_`
  wildcard); a missing case is a compile error naming the case you forgot.
- Destructuring: `let {id, name} = row` and `let [head, ...rest] = xs`.

### Modules (§1)

```revl
use "./tokens.rvl" { Token, TokenKind, keyword_set }
use "./util/strings.rvl" as strings

pub fn lex(source: Str) -> List[Token] { ... }   // importable
fn helper() -> Int { ... }                        // module-private by default
```

Components are *never* imported — they are *composed*, over a manifest.
Import cycles between modules are a compile error.

### Components & effects (§4)

The core from [DESIGN.md](../DESIGN.md) §3, plus 2.0's two additions:

- **Block effect form** — acquisitions with several pure setup steps:
```revl
let pool = effect {
  let url = normalize(config.url)
  Pool.open(url, config.pool_size)          // last expression = the acquisition
} undo pool.close()
```
- **`fail`** — deliberate L-Raise from an activation body (reverts what's
  accumulated, lands FAILED):
```revl
if (config.replicas < 1) fail "at least one replica required"
```

### Services 2.0 (§5)

```revl
pub service Database {
  fn query(sql: Str) -> List[Row]
  async fn stats() -> Stats                    // may await host async values
  emission fn execute(sql: Str) -> Int
}
```

`commutative` on a service or a single operation declares order-independence
(the opt-in that upgrades a key from LIFO-only to reorderable recovery).

### Host blocks (§6)

FFI is the only door out of confinement, and it must classify itself:

```revl
extern pure fn sha256(data: Bytes) -> Str
  = @ts { return crypto.createHash("sha256").update(data).digest("hex") }
  = @py { import hashlib; return hashlib.sha256(data).hexdigest() }
```

`pure` / `acquire` (must declare `undo`) / `emission` (may declare
`compensate`). An unclassified `extern` does not compile.

### test & verified (§7)

```revl
verified fn add(a: Int, b: Int) -> Int { return a + b }

test "add works" {
  assert add(1, 2) == 3
}
```

`revl test` runs the test blocks; `verified` opts a function into the totality
tier (structural recursion / bounded loops only).

### The stdlib surface

A method call on a value must name a known builtin — anything else is a
compile error, never a host pass-through ([docs/stdlib-2.0.md](stdlib-2.0.md)):

| method | on | note |
|---|---|---|
| `length` / `length()` | Str, List | element count |
| `push(v)` | List | **persistent** — returns a new list (`out = out.push(v)`) |
| `slice(a, b)` | Str, List | half-open sub-range |
| `charAt(i)` / `charCodeAt(i)` | Str | 1-char string / code point |
| `indexOf(v)` | Str, List | first index, `-1` if absent |
| `concat(y)` | Str, List | joined copy |
| `split(sep)` | Str | pieces between separators (JS shape) |
| `join(sep)` | List[Str] | elements joined by `sep` |
| `repeat(n)` | Str | `n` copies concatenated |

## What won't compile

This is the point of the language — each rejection names the guarantee and
the fix ([DESIGN.md](../DESIGN.md) §4):

| # | Guarantee |
|---|---|
| G1 | Every requirement is declared; undeclared access can't be written |
| G2 | Provision disjointness in a composition |
| G3 | Dependency cycles rejected |
| G4 | Every mutation carries an inverse or an `emit` marker |
| G5 | Teardown can't register effects (by construction) |
| G6 | Code outside effect forms is pure (confinement) |
| G7 | Derived teardown is LIFO-complete |
| G8 | The boundary surface (externs, emissions) is enumerable |

…plus the lifecycle rules A1–A8 (await boundaries, no acquisition after
`provide`, `fail` semantics, and so on). The rejection suite in
[`examples/rejections/`](../examples/rejections/) is the executable spec.

## Tooling

```bash
python -m revl compile app.rvl -o out.json   # parse → check → link → IR
python -m revl audit app.rvl                 # manifest + G8 boundary surface
python -m revl test app.rvl                  # run in-file test blocks
python -m revl fmt --migrate old.rvl         # rewrite 1.x "$name" → `${name}`
python -m revl run app.rvl                   # boot on cordis-py; hold live with a REPL over provided services
python -m revl run app.rvl --watch           # recompile on edit; a rejected change keeps the run alive
python -m revl run app.rvl --plan            # print the load plan (order, config, callable keys); no runtime needed
```

```bash
python -m revl compile app.rvl --json-diagnostics  # structured rejections for CI
python -m revl run app.rvl --placement map.toml    # split across processes/languages
python -m revl mcp serve                           # the compiler as an MCP server
python -m revl mcp schema app.rvl                  # provided services -> MCP tools
python -m revl mcp import tools.json               # an MCP server -> revl source
python3 tools/conformance.py                       # every construct x every backend
```

`run` needs the cordis-py runtime (`backends/python/setup.sh`); `--backend`
selects the tier. `--placement` composes py / node / rust / java in one
lifecycle (docs/interop-bridge.md), and `mcp serve` additionally holds a
composition **in memory** so an agent can load, call, swap and prove
no-residue without touching the filesystem (docs/mcp-bridge.md).

## Backends

- **cordis-py** (Python) — the reference backend, on a hardened lifecycle
  runtime. The tier every construct is checked against first.
- **cordis** (TypeScript) — v4.
- **cordis-rs** (Rust) and **cordis4j** (Java) — both consume *and* serve
  across a process seam, reactively (peer death becomes withdrawal).
- **cordis-wasm** — the sandboxed substrate, where confinement becomes
  physical. Deliberately i32-only at the service boundary.

What each tier can and cannot express is measured, not asserted:
`tools/conformance.py` runs every construct through all five and
[docs/conformance.md](conformance.md) records the result, separating a
deliberate limit from a gap.

## Further reading

- [DESIGN.md](../DESIGN.md) — the design, the guarantees table, the tiering rationale.
- [docs/syntax-2.0.md](syntax-2.0.md) — the full-language spec.
- [docs/stdlib-2.0.md](stdlib-2.0.md) — the stdlib surface.
- [docs/v2.0-roadmap.md](v2.0-roadmap.md) — status and remaining frontier.
- [docs/conformance.md](conformance.md) — what each backend can express.
- [docs/mcp-bridge.md](mcp-bridge.md) — the agent boundary and the live session.
- [docs/interop-bridge.md](interop-bridge.md) — splitting a composition across
  languages and processes.
- [docs/backend-ir-v3.md](backend-ir-v3.md) — the contract, if you are writing
  a backend.

