# revl 2.0 — full-language syntax proposal

**Status:** implemented on branch `v2.0` (2026-08-17) — see
docs/v2.0-roadmap.md for per-item status; the §10 acceptance benchmark is
running post-hoc and its findings may still revise stratum 1. Originally a
proposal (2026-08-16), successor to DESIGN.md §3.

revl 1.x is a component-composition language with a deliberately tiny pure
expression layer. revl 2.0 grows it into a full-powered language — able to
express general computation, its own compiler, and everything an AI author
needs — without weakening a single guarantee.

## 0. The governing principle

> **Same meaning → same syntax. Different meaning → different syntax.**

Where a construct means *exactly* what it means in TypeScript, revl 2.0 uses
TypeScript's syntax verbatim, harvesting every model's training prior. Where
the paradigm's semantics begin — effects, inverses, provisions, boundaries —
the syntax stays distinctly revl, as a deliberate semantic speed bump. The
constructs models write on autopilot are the ones where autopilot is correct.

Corollaries:

- TS constructs whose semantics revl does **not** share are *excluded and
  named in diagnostics* (`hits++` → "mutation; revl expressions are pure —
  mutate through an `effect` on a service").
- No construct may exist in both languages with silently different meaning.
  (One negotiated exception: `==`, see §3.4.)

The language has four strata, each with its own rules:

| stratum | syntax allegiance | checked by |
|---|---|---|
| 1. Pure expressions & functions | TypeScript subset | totality of the subset |
| 2. Types & data | revl's own (concise, host-neutral) | structural checker |
| 3. Components & effects | revl's own (unchanged from 1.x) | G1–G8, A1–A8 |
| 4. Host blocks | verbatim host language | boundary types + G8 audit |

---

## 1. Modules and visibility

A file is a module. `use` imports **pure** declarations only — types,
functions, services. Components are never imported: they are *composed*, by
the linker, over a manifest. This keeps the two composition mechanisms from
blurring: `use` is compile-time and pure; the manifest is runtime and stateful.

```revl sketch
use "./tokens.rvl" { Token, TokenKind, keyword_set }
use "./util/strings.rvl" as strings

pub fn lex(source: Str) -> List[Token] { ... }   // pub: importable
fn helper() -> Int { ... }                        // default: module-private
```

- `pub` marks exported declarations; services are `pub` by default (they are
  interfaces), components are never exported (composed, not imported).
- Import cycles between modules are a compile error (distinct from G3, which
  governs runtime component graphs).

## 2. Types and data

Type syntax stays revl's own — capitalized names, `[]` generics. Rationale:
type-level TS is structural and enormous; revl types are nominal-ish and
host-neutral, so the "same meaning" premise fails and distinct syntax is
correct. `[]` generics also avoid the classic `<` ambiguity in expressions.

```revl
type Row = { id: Int, name: Str, active: Bool }        // record
type Pair[A, B] = { first: A, second: B }              // generic
type TokenKind = Ident | Keyword | IntLit | StrLit      // enum (no payload)
type Outcome = Ok(Row) | NotFound | Invalid(Str)        // ADT (payloads)

// built-ins: Str, Int, Float, Bool, Bytes, Unit
// List[T], Map[K, V], Opt[T], Result[T, E]
// sugar: T?  ==  Opt[T]
```

- Records are structural; ADTs are nominal. `match` (§3.3) is the eliminator
  for ADTs and is exhaustiveness-checked.
- No `null`/`undefined` in the type system — absence is `Opt[T]`. In
  expressions, TS's `??` and `?.` operate on `Opt` (§3.2): the familiar
  syntax, the honest type.

## 3. Pure expressions and functions (the TypeScript-subset stratum)

### 3.1 Functions

```revl
type Row = { id: Int, name: Str, active: Bool }        // as §2

pub fn score(items: List[Row]) -> Int {
  let is_active = r => r.active               // TS arrow lambdas, verbatim
  var n = 0
  for (r of items) { if (is_active(r)) n += 1 }
  return n
}

fn classify(n: Int) -> Str {
  if (n < 0) return "negative"                  // TS if/return, verbatim
  return n === 0 ? "zero" : "positive"          // TS ternary, verbatim
}
```

`fn` bodies are **pure from the outside**: no context, no effects, no host
access, no observable mutation. G6 keeps its by-construction character —
there is no syntactic path from a `fn` body to an effect.

### 3.2 The admitted TS subset (semantics coincide — syntax verbatim)

- literals: numbers, strings, booleans, `[a, b]`, `{ id: 1, name: "x" }`
- template strings: `` `hello ${user.name}` `` — **replaces 1.x `$name`
  interpolation** (breaking change; 1.x sources migrate mechanically)
- operators: `+ - * / %`, comparisons, `&& || !`, ternary `c ? a : b`
- `?.` optional chaining and `??` nullish coalescing — typed against `Opt[T]`
- arrow functions `x => expr`, `(a, b) => { ... }`; captures are by-value
  snapshots (no shared mutable environment — see §3.5)
- method calls, indexing `xs[i]`, slicing via methods, spread `[...xs, y]`,
  destructuring `let { id, name } = row`, `let [head, ...rest] = xs`

### 3.3 Where TS semantics diverge, revl syntax diverges

- **`match`, not `switch`.** TS `switch` has fallthrough and no binding;
  revl's eliminator is different in meaning, so it looks different:

```revl fragment
match lookup(key) {
  Ok(row)      => row.name,
  NotFound     => "-",
  Invalid(why) => `bad: ${why}`,
}
```

- **`let` is single-assignment.** Reassignment of a `let` is an error with a
  fix-hint pointing at `var` (§3.5) or restructuring.
- **Excluded outright**, each with a model-facing diagnostic naming the
  idiom: `class`, `new`, `this`, `function`, `import`, `export`, `typeof`,
  `delete`, `try/catch` (failure is `Result` in pure code; L-Raise at the
  effect layer), `++`/`--`, compound assignment on non-`var`, `async`
  arrows in pure code, generators.

### 3.4 Equality — the one negotiated lookalike

revl has a single equality: structural value equality, no coercion. Both
spellings `==` and `===` are accepted and mean that one thing (`!=`/`!==`
likewise). Rationale: models emit both reflexively; making one of them an
error would burn feedback cycles on a distinction revl doesn't have. The
compiler canonicalizes to `==` in the IR (the parser folds `===`→`==`,
`!==`→`!=`), so no backend can diverge; a source-level formatter pass that
also rewrites the spelling is future work (`revl fmt` today only does
`--migrate`).

### 3.5 Local mutation: `var`, `while`, `for`

Self-hosting a parser without loops or a mutable position index is
masochism, and models write loops fluently. revl 2.0 admits **function-local
mutation** — unobservable from outside, so functions remain pure in meaning:

```revl
type TokenKind = Ident | Keyword | IntLit | StrLit
type Token = { kind: TokenKind, text: Str }

fn count_idents(tokens: List[Token]) -> Int {
  var n = 0                                   // var: local, mutable
  for (tok of tokens) {                       // TS for-of, verbatim
    if (tok.kind == Ident) n += 1
  }
  return n
}

fn skip_ws(source: Str, start: Int) -> Int {
  var i = start
  while (i < source.length && source.charAt(i) == " ") i += 1
  return i
}
```

Rules that keep this honest:

- `var` never escapes: lambdas capture its *current value*, not the cell;
  a `var` cannot appear in a record, be returned by reference, or outlive
  its scope. Purity's boundary is the function, exactly like Koka's local
  state or Rust's non-`mut`-escaping locals.
- `for (x of xs)` and `while` — TS syntax verbatim; both total-checked only
  in `verified` contexts (§7), unrestricted elsewhere.

## 4. Components and effects (unchanged core, new block forms)

Stratum 3 is revl 1.x, deliberately untouched — these keywords are the
semantic speed bumps and *must not* look like anything in the corpus:

```revl
service Cache {
  fn get(key: Str) -> Opt[Str]
  emission fn put(key: Str, value: Str)
}
service Database { emission fn execute(sql: Str) -> Int }

component UserCache requires db: Database provides cache: Cache {
  config { ttl: Int = 300 }

  let store = effect Map.new() undo store.drop()

  await Job.run("warmup")                       // divert boundary (A1)

  provide cache {
    fn get(key) = store.get(key)
    fn put(key, value) {
      effect store.insert(key, value)
      undo   store.remove(key)
      emit db.execute(`INSERT INTO cache_log VALUES (${key})`)
           compensate db.execute(`DELETE FROM cache_log WHERE k = ${key}`)
    }
  }
}
```

2.0 additions to this stratum:

- **Block effect form**, for acquisitions that take several pure steps:

```revl fragment
let conn = effect {
  let url = normalize(config.url)
  Pool.open(url, config.pool_size)              // last expression = value
} undo conn.close()
```

  The block body is stratum-1 pure code plus the final acquisition; `undo`
  is unchanged. G5 still holds by construction — `undo` has no statement
  position.

- **`fail` statement** for deliberate L-Raise from an activation body:
  `if (config.replicas < 1) fail "at least one replica required"` —
  lowering to the A8 semantics (revert accumulated, land FAILED).

- **Realms and interception (paper §3.2.3) are implemented** (merged in the
  v2.0 branch; see docs/design-v2-realms.md): `isolate db in realm("tenant")`
  · `intercept db with { paths: [...] }` — with **static** string labels only;
  the dynamic `realm(config.tenant)` form remains reserved (instance-parametric
  components).

## 4b. Rules the implementation added

These are not in the original proposal — each was forced by building
something real, and each is now enforced. They belong with §4 because they
are all about how a component's boundary is declared.

### 4b.1 A service declaration is an upper bound on its providers (G4)

A service operation declared plain may not reach an emission in *any*
provider's body — directly, through an `emission` extern, or through a
function that transitively reaches one:

```
`Cache.put` is declared plain, but this implementation reaches `db.execute`
  a service declaration bounds what its providers may do — mark it
  `emission fn put(...)` in service `Cache`, or move the irreversible call
  out of this method (G4)
```

The bound is one-directional: a provider may be **purer** than declared
(declared `emission`, body doesn't emit — the consumer already assumed the
worst), never less pure. That direction is the sound one because consumers
bind to the *service*, not to a component, and providers hot-swap
underneath them.

It also repairs `revl audit`: G8 enumerates a caller's emissions by reading
the declarations of the operations it calls, so an under-declared operation
made every consumer's audit incomplete — not merely misleading.

### 4b.2 `emit` is also an expression

`emit` began as a statement, which discarded its value — fatal for the
canonical emission that *returns* data (an LLM completion, an HTTP GET).
It is now also a prefix in value position, so the marker stays at the call
site (G4's actual point) while the value flows:

```revl fragment
provide evolve {
  fn once(goal) = emit compiler.propose(emit assistant.complete(goal))
}
```

Legal on a service emission or on an `emission` extern (both are boundary
crossings); `emit` on anything else is refused. The statement form still
takes an optional `compensate`.

### 4b.3 Provide-method bodies take plain bindings

`let x = <expr>` names an intermediate value in a method body; `let x =
effect … undo …` still means the acquisition (the parser branches on the
`effect` keyword). `var` plus assignment is available where a method
accumulates, and A3 host-name renaming applies:

```revl fragment
provide greet {
  fn hello(name) {
    let prefix = "hi, "
    return prefix + name
  }
}
```

A component **activation** body still refuses a plain binding: it records
effects, and a plain value there has nothing to revert (G6). The diagnostic
points at the effect form.

### 4b.4 `match` works in component and method bodies

The ADT eliminator is no longer confined to `fn` bodies, so a component
consuming a `Result` or user variant does not have to call out to a
function. Same node, same exhaustiveness check.

### 4b.5 A bare `return`

`fn f(x) { return }` is the natural body of a service operation declared
with no return type. A bare `return` in a *typed* operation is refused with
its own message.

### 4b.6 `??` requires an optional on the left

`a ?? b` supplies a fallback when `a` is absent, so a non-optional left
operand makes the fallback dead code. It is now a type error — three
backends could not render it (rust would emit `unwrap_or_else` on an `i64`,
java `orElseGet` on a `long`), and python silently accepted it.

## 5. Services 2.0

```revl
pub service Database {
  fn query(sql: Str) -> List[Row]
  async fn stats() -> Stats                    // async operation
  emission fn execute(sql: Str) -> Int
}
```

- `async fn` operations: provide-method bodies implementing them may
  `await` *host* async values. This `await` is **not** a divert boundary —
  boundaries exist only in activation bodies. Same keyword, and the meaning
  is TS's own (suspend on a promise), so the principle holds; the *boundary*
  reading of `await` is exclusive to component bodies and the checker
  enforces the separation (a body `await` in a method is still rejected, A1).
- `commutative` on a service (or a single operation) declares Def. 39
  order-independence, discharged under §7 — the opt-in that upgrades a key
  from LIFO-only to reorderable recovery.

## 6. Host blocks — full power, honestly labeled

The escape hatch is an `extern` with per-backend verbatim bodies. Inside is
the host language, unchecked, opaque; the boundary is typed; the whole
construct sits on the G8 audit surface.

```revl
extern pure fn sha256(data: Bytes) -> Str
  = @ts { return crypto.createHash("sha256").update(data).digest("hex") }
  = @py { import hashlib; return hashlib.sha256(data).hexdigest() }

extern acquire fn listen(port: Int) -> Socket undo close(socket)
  = @ts { ... } = @py { ... }

extern emission fn send(sock: Socket, data: Bytes)
  compensate log_unsent(sock, data)
  = @ts { ... }
```

- Multiple `@backend` bodies make an extern portable; a component compiles
  on exactly the backends whose bodies (or builtin mappings) exist, and
  `revl audit` reports the per-backend surface.
- Classification is mandatory and semantic: `pure` (no observable effect —
  trusted, audited), `acquire` (must carry `undo`), `emission` (may carry
  `compensate`). An unclassified extern does not parse.
- Inside a host block the model writes *real* TypeScript or Python — the
  place full fluency is wanted is exactly the place checking was never
  promised.

## 7. `verified` and `test` — the self-checking loop

```revl sketch
verified fn parse_int(s: Str) -> Opt[Int] { ... }   // totality-checked:
                                                     // structural recursion /
                                                     // bounded loops only

test "put then get roundtrips" {
  let m = Map.new()                                  // test-scoped world
  assert m.insert("k", "v").get("k") == Some("v")
}
```

- `test` blocks compile to the backend's native test idiom and run in
  `revl test`; for an AI author this closes the generate→check→run loop
  inside one file.
- `verified` is the opt-in strictness tier (from DESIGN §6): totality for
  functions, algebraic-law property tests for effect inverses
  (`verified effect` requires the checker to find or generate the
  `undo ∘ do = id` witness over the declared value model).

### 7.1 `lifecycle test` — the paradigm's own guarantee, asserted in-language

A plain `test` block is stratum-1: pure statements over pure functions. That
leaves the one property revl exists to provide — that composition is
*revertible* — expressible only in each backend's own host test suite. A
`lifecycle test` closes that: it is a script over a **live composition**.

```revl
lifecycle test "cache reverts cleanly" {
  load PgDatabase with { url: "postgres://primary:5432/app" }
  load UserCache
  call cache.put("k", "v")
  let hit = call cache.get("k")
  assert hit == Some("v")
  unload UserCache
  unload PgDatabase
  assert no_residue
}
```

**`lifecycle` is a modifier, not a new top-level form.** The precedent is
`verified fn`: an adjective that changes what a body may contain without
changing what the declaration *is*. A lifecycle test is still a named,
runnable unit that `revl test` executes and reports; only its body's stratum
differs (3, components and effects — not 1, pure expressions). The grammar
delta is therefore one token in one position, which is what the §8
prompt-size requirement demands.

`lifecycle` is a **contextual** keyword: it is recognized only immediately
before `test` at top level and remains an ordinary identifier everywhere
else. So are `load`, `unload` and `call` inside a lifecycle body, and
`no_residue` after `assert`. Nothing is added to the lexer's keyword set — no
existing program can stop compiling because of this feature, and the
self-hosted lexer's token stream (§11) is unchanged.

#### Grammar

```
decl     := ... | ['lifecycle'] test
test     := 'test' STRING '{' (lcstmt | stmt)* '}'
lcstmt   := 'load' IDENT ['with' '{' (IDENT ':' expr)* '}']   // instantiate
          | 'unload' IDENT                                     // revert
          | ['let' IDENT '='] 'call' IDENT '.' IDENT '(' expr* ')'
          | 'assert' 'no_residue'
          | 'assert' expr
```

Six statements, no new expression forms. `stmt` (the pure statement set) is
*not* admitted in a lifecycle body and `lcstmt` is not admitted in a pure
one — each is refused by name, not by a parse error:

```
`load` is only allowed in a `lifecycle test` body
  a plain `test` block is pure (syntax-2.0 §7); write `lifecycle test "name"
  { ... }` to drive a composition (§7.1)
```

#### Semantics, statement by statement

A lifecycle body is *linear*, so the checker tracks exactly which components
are live and which keys are provided at every point. Every rule below is a
compile error, not a runtime surprise.

- **`load C with { field: expr, … }`** — instantiate `C` and settle the
  composition. The `with` clause is checked against `C`'s `config` block:
  unknown field, duplicate field, missing required field, and type mismatch
  are all rejected. `with { }` may be omitted when `C` has no required
  config. Loading is *not* activation: a component whose `requires` are unmet
  stays PENDING (R2), and only becomes ACTIVE when a provider arrives —
  `load UserCache` before `load PgDatabase` is legal and does nothing until
  the database lands.
- **`unload C`** — dispose the instance, running its accumulated inverses
  newest-first (R1) and withdrawing its provisions (R5). Unloading a
  component that is not loaded is a compile error.
- **`call key.op(args)`** / **`let x = call key.op(args)`** — invoke an
  operation through a *provision key*, not through a component: consumers in
  revl bind to services, and so does the test. The key must be provided by a
  component that is loaded at that point, `op` must be an operation of that
  key's service, and arity and argument types are checked against the service
  declaration. The binding form names the result and is single-assignment;
  its type is the operation's declared return type, so `assert` over it is
  type-checked like any other expression. An `async fn` operation is awaited
  by the driver — the test does not spell `await`, because the boundary
  reading of `await` belongs to activation bodies only (§5).
  **No `emit` marker is required to call an emission operation.** G4's rule
  (§4b.1) is that *a service declaration is an upper bound on its providers*;
  a lifecycle test is not a provider, it is the top of the composition — the
  same position `demo.py` occupies — so there is no declaration for it to
  exceed and nothing for a marker to bound.
- **`assert no_residue`** — see below.
- **`assert <expr>`** — an ordinary Bool expression over the test's `let`
  bindings, checked exactly as in a `fn` body. A bare word that is not a
  binding is reported as an unknown *lifecycle assertion*, since that is what
  it almost always is.

#### What `no_residue` means

It is R4 from docs/backend-ir.md §Required semantics — *"after unloading
everything, the host runtime holds no bindings, listeners, or effects from
the composition (assert via the runtime's introspection)"* — taken verbatim
from the suite that defines residue-freedom for the reference tier,
`backends/python/tests/test_semantics.py::test_r4_no_residue_after_unloading_
everything`. The emitted assertion snapshots the same four quantities that
test does, at the top of the lifecycle test, and compares at the assertion
point:

| quantity | cordis-py introspection |
|---|---|
| listeners | `root.events._hooks` (non-empty counts) |
| effects | `root.fiber._disposables.length` |
| provisions | `root.reflect.store` |
| plugin runtimes | `root.registry.size` |

**And a second half, because R4 alone cannot fail.** R5 is the reason: the
emitted module hand-rolls no teardown at all — every inverse goes through the
runtime's revertible `provide`/`set` and the effect protocol — so for *any*
component the compiler accepts, the runtime's own introspection always
returns to baseline. An assertion that cannot fail is not an assertion. The
falsifiable half of residue-freedom is R1's: *unloading runs the accumulated
undos, including those accumulated by provide-method calls while active.*
So `no_residue` also requires that every host resource acquired during the
test was released by the end — no `Pool.open` without a `close`, no `Map.new`
without a `drop` — read off the host-builtin trace (`runtime.set_trace`), the
same signal the R1 suite asserts ordering on.

Together the two halves catch the two ways a composition leaks:

- *the test left something loaded* — R4 fires (`provisions: [] -> ['db']`);
- *an `undo` is not the inverse of its acquisition* — R1 fires
  (`pool#1 (open() with no close())`).

The second is the interesting one, and it is why this form earns its place:
G4 requires an acquisition to *carry* an `undo`, but nothing in the type
system knows whether that undo undoes anything — `verified effect` (§7)
would, and is not implemented. `examples/lifecycle_leak.rvl` is a component
that passes every static check and leaks a connection pool on every unload;
the assertion catches it.

The resource half is necessarily tier-specific: it is stated over the
reference tier's host-builtin vocabulary (`Pool`/`Map` in
docs/backend-ir.md §Host builtins). Another tier implementing lifecycle tests
states it over its own, against the same R1/R4 clauses.

#### There is no `swap`

The roadmap sketch for this form included `swap C -> C2`. It cannot exist:
G2 forbids two components in one document from providing the same key, so a
replacement *provider* for a key is not expressible in the document a
lifecycle test lives in — and if the two are in different realms (§4) they
are not replacements for one consumer either. What the demo actually swaps is
a replacement *instance*: `unload C` then `load C with { … }`, which this
statement set already spells, and whose R2 reactivation is exactly what the
test then asserts on (`examples/lifecycle_cache.rvl`, "a reloaded cache
starts empty"). `swap` is refused *by name* rather than as a parse error, so
the sketch that motivated this form fails with its own explanation.

#### Portability

`lifecycle test` lowers on the **reference tier only** (cordis-py). The other
four emitters refuse it by name:

```
lifecycle test 'cache reverts cleanly' is not lowerable on the cordis-rs tier:
  it drives a live composition (load/call/unload) and asserts R4
  residue-freedom through the host runtime's introspection, which only the
  reference tier implements — run it with `revl test --backend py`
```

This is a boundary, not a gap: the driver needs the host runtime's
introspection, and each tier's is different. A refusal by name is deliberate
— a construct silently dropped by one renderer and honored by another is this
project's recurring bug class. In the IR a lifecycle test is an ordinary
`ir_version: 3` entry in `tests` carrying `"lifecycle": true`, so a pure
`test` document is byte-identical to what it was before this feature.

## 8. Grammar deltas (summary)

```
program     := use* decl*
use         := 'use' STRING ('{' IDENT (',' IDENT)* '}' | 'as' IDENT)
decl        := ['pub'] (typedecl | fndecl | service | component | extern)
             | ['lifecycle'] test                              (§7.1)
typedecl    := 'type' IDENT generics? '=' (record | variant ('|' variant)*)
fndecl      := ['verified'] 'fn' IDENT '(' tparams? ')' ['->' type] block
component   := (unchanged from 1.x) + blockeffect + fail
extern      := 'extern' class 'fn' sig ['undo' expr] ['compensate' expr] hostbody+
hostbody    := '=' '@' IDENT '{' <verbatim host text, brace-balanced> '}'
expr        := TS-expression-subset  (see §3.2/§3.3)  + 'match' + block-expr
stmt        := let | var | assign(var-only) | if | for-of | while | return | expr-stmt(calls)
lcstmt      := load | unload | call | 'assert' ('no_residue' | expr)   (§7.1)
```

The full grammar remains small enough to include, in its entirety, in a
model's system prompt — that property is a requirement, not an accident,
and grammar growth that would break it needs this document amended first.

## 9. Migration from 1.x

Mechanical, one release:

- `"$name"` interpolation → `` `${name}` `` templates (`$$` escape retired);
  `revl fmt --migrate` rewrites sources.
- 1.x expression-form `effect E undo U` unchanged; everything else additive.
- IR v3 carries: `fn` declarations (lowered bodies), `match`, ADT layouts,
  extern host bodies keyed by backend, `test` units. Backends that predate
  v3 reject it by version, as v1 established. (`ir_version: 2` is already
  taken by realms/interception — see docs/design-v2-realms.md.)

## 10. Why this serves the AI-author goal (and how we'll know)

- Stratum 1 makes the model's *strongest* priors simply correct; stratum 3
  keeps the paradigm's constructs visually novel so the model attends to
  the spec; §3.3's exclusion diagnostics convert wrong priors into one-shot
  corrections; §6 gives full fluency where checking was never claimed；§7
  closes the loop in-file.
- Synthetic-corpus bootstrap: the checker's totality lets us mass-generate
  compile-verified 2.0 code for fine-tuning — the corpus gap is a decaying
  liability, not a fixed one.
- **Acceptance experiment** (before implementation hardens): 30 component
  specs × {1.x syntax, 2.0 syntax, 2.0+host-blocks} × several models;
  measure first-pass compile rate and iterations-to-green. The syntax ships
  only if the benchmark agrees it should.

## 11. Self-hosting shape (the point of all this)

With strata 1–2, the compiler becomes expressible as what it structurally
already is — a pipeline of components with pure interiors:

```revl sketch
component Lexer provides tokens: TokenStream {
  provide tokens { fn lex(source) = lex_impl(source) }   // lex_impl: pub fn
}

component Parser requires tokens: TokenStream provides ast: AstService { ... }
component Checker requires ast: AstService provides typed: TypedAst { ... }
component Emitter requires typed: TypedAst, target: Backend { ... }
```

A compiler whose checker pass can be hot-swapped in a running process, with
rollback if the replacement fails its own admission check — the paradigm
eating its own dogfood is the 2.0 exit criterion.
