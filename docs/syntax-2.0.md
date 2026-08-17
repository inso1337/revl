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

```revl
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
pub fn score(items: List[Row]) -> Int {
  let active = items.filter(r => r.active)     // TS arrow lambdas, verbatim
  return active.length
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

```revl
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
formatter canonicalizes to `==`.

### 3.5 Local mutation: `var`, `while`, `for`

Self-hosting a parser without loops or a mutable position index is
masochism, and models write loops fluently. revl 2.0 admits **function-local
mutation** — unobservable from outside, so functions remain pure in meaning:

```revl
fn count_idents(tokens: List[Token]) -> Int {
  var n = 0                                   // var: local, mutable
  for (tok of tokens) {                       // TS for-of, verbatim
    if (tok.kind == Ident) n += 1
  }
  return n
}

fn skip_ws(source: Str, start: Int) -> Int {
  var i = start
  while (i < source.length && source[i] == " ") i += 1
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

```revl
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

```revl
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

## 8. Grammar deltas (summary)

```
program     := use* decl*
use         := 'use' STRING ('{' IDENT (',' IDENT)* '}' | 'as' IDENT)
decl        := ['pub'] (typedecl | fndecl | service | component | extern | test)
typedecl    := 'type' IDENT generics? '=' (record | variant ('|' variant)*)
fndecl      := ['verified'] 'fn' IDENT '(' tparams? ')' ['->' type] block
component   := (unchanged from 1.x) + blockeffect + fail
extern      := 'extern' class 'fn' sig ['undo' expr] ['compensate' expr] hostbody+
hostbody    := '=' '@' IDENT '{' <verbatim host text, brace-balanced> '}'
expr        := TS-expression-subset  (see §3.2/§3.3)  + 'match' + block-expr
stmt        := let | var | assign(var-only) | if | for-of | while | return | expr-stmt(calls)
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

```revl
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
