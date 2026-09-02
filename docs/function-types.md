# Function types

**Status:** implemented (2026-08-18). Supersedes the "arrows are unchecked"
entry in `src/revl/typecheck.py`'s enumerated frontier.

Before this, an arrow had no type. `infer_ast` returned `None` for
`ExprArrow`, and the consequences were visible in three places at once: the
TypeScript backend emitted every arrow parameter as an explicit `any` (an
admission, not an inference); the Java backend could not name a target type
and lowered arrows by beta-reduction at the call site, refusing any case where
substitution would change meaning; and higher-order composition — middleware,
combinators, retry wrappers — was not expressible at all, because there was no
way to *declare* a parameter that takes a function.

revl now has a function type. Arrows can be checked, stored, passed and
returned.

---

## 1. The type

```revl sketch
(Int, Str) -> Bool        // takes an Int and a Str, returns a Bool
(Int) -> Int              // one parameter
() -> Int                 // no parameters
(Int) -> (Str) -> Bool    // returns a function (arrows associate to the right)
```

A function type is usable **anywhere a type is written**:

```revl
type Step    = (Int) -> Int                        // alias
type Handler = { name: Str, run: (Int) -> Str }    // record field
type Hook    = Wrap((Int) -> Int) | None_          // ADT payload

fn compose(f: Step, g: Step) -> Step {             // parameter and return
  return v => g(f(v))
}

service Middleware { fn wrap(next: (Int) -> Int) -> (Int) -> Int }

fn run(n: Int) -> Int {
  let inc: (Int) -> Int = v => v + 1               // let annotation
  return inc(n)
}
```

`let name: Type = value` and `var name: Type = value` are new — a `let` had no
annotation slot before, and it is the only way to give an arrow a type without
passing it somewhere.

### Why this spelling

syntax-2.0 §0's governing principle is *same meaning → same syntax*, and §2
keeps type syntax revl's own ("capitalized names, `[]` generics"). Both point
the same way here:

- **TypeScript's spelling is not adopted.** TS writes `(a: number) => boolean`
  and *requires* the parameter name. revl's function type carries no parameter
  names, so the two constructs do not mean the same thing and §0's premise
  fails. Reusing TS's syntax for a construct that rejects TS's own examples is
  exactly the uncanny valley §9 warns about.
- **`->` is already revl's return arrow**, in `fn`, `extern` and service
  method signatures. `(Int, Str) -> Bool` therefore reads as literally the
  signature of `fn f(a: Int, b: Str) -> Bool` with the names elided — the
  strongest available mnemonic, and it needs no new token (`->` already lexes
  as `arrow`).
- **`Fn[Int, Str, Bool]` was rejected** even though §2's `[]`-generic shape
  would suggest it: it hides which argument is the return type (`Fn[Int,
  Bool]` reads as two parameters until you learn the convention), and it would
  put a bare `Fn` into the user's type namespace.

Internally the checker normalises a function type to the head `FN_HEAD`
(spelled `"->"`, characters no identifier may contain) with arguments
`[param..., return]`, so the whole existing type algebra — `unify`,
`substitute`, `compatible`, implicit-type-parameter marking — operates on it
without a special case beyond variance. `format_type` puts the surface
spelling back. **There is exactly one spelling**: what the author wrote is
what the IR carries and what a diagnostic prints.

### Precedence and grouping

The return type is parsed as a full type, so `?` after it binds to the return:

| written | means |
|---|---|
| `(Int) -> Str?` | a function returning `Opt[Str]` |
| `((Int) -> Str)?` | `Opt[(Int) -> Str]` — an *optional function* |
| `(Int) -> (Str) -> Bool` | a function returning a function |
| `((Int) -> Str) -> Bool` | a function *taking* a function |

A parenthesised group holding one type is that type; a group holding two or
more with no `->` after it is rejected — revl has no tuples.

---

## 2. Checking

An arrow is checked, not inferred globally. Two things can give it types:

**(a) Checking position.** Wherever the expected type is known and is a
function type, the arrow's parameters take their types from it and its body is
checked against the expected return:

```revl
fn apply_twice(g: (Int) -> Int, x: Int) -> Int { return g(g(x)) }

fn demo(n: Int) -> Int {
  return apply_twice(v => v + 1, n)   // `v` is Int; body checked against Int
}
```

Checking positions are: a `fn`/`extern` argument, a `return` against a
declared return type, a `let`/`var` with an annotation, a reassignment of an
annotated `var`, a record-literal field against a declared record type, and a
list element against `List[T]`.

**(b) Parameter annotations.** `(v: Int) => v + 1` types itself, so an arrow
in a position with no expectation still leaves the frontier:

```revl
fn demo(n: Int) -> Int {
  let g = (v: Int) => v + 1     // g : (Int) -> Int
  return g(n)
}
```

**(c) Return annotations.** `(v: Int): Int => v + 1` names the result. The
spelling is TypeScript's, because for the arrow *expression* the "same meaning,
same syntax" premise holds where it failed for the function type: TS's
`(x: number): number => x` and revl's `(x: Int): Int => x` are the same
construct, a named parameter list with an optional result type and a single
expression body. `->` is **not** accepted in this position — `) -> T` is
already the tail of a function type, and two readings of the same three tokens
at nearly the same place is a readability hazard the grammar does not need.

```revl
fn demo(n: Int) -> Int {
  let inc = (v: Int): Int => v + 1    // both halves annotated
  let zero = (): Int => 0             // no parameters, so no other site exists
  return inc(n) + zero()
}
```

The return type is a full type, so `?` binds to it: `(v: Int): Str? => e`
returns `Opt[Str]`. The bare single-parameter form takes no annotation of
either kind — `v: Int => e` is a parse error, as it is in TypeScript.

> **Rule C1. Colour is positional. An arrow may not declare its own.**
> An arrow return annotation naming `Async[...]`, at the top level or as the
> return of a function type it names, is refused with `A1`. An arrow's
> `"async"` flag is not a label but a certificate that some *declaration*
> promised to await it — the item-92 leak check skips a flagged arrow and
> callee collection stops descending at one — so a self-declared colour would
> forge it and launder every async callable nested inside the arrow out of the
> enclosing scope's reach set. Colour stays obtainable from a declared
> parameter `(Int) -> Async[Str]`, or from the coercion that stamps an arrow
> argument landing in an `Async[T]` slot; an annotation on such a coerced
> arrow names the **sync inner type**.

> **Rule G. An arrow is an expression, not a signature. It never quantifies.**
> A type name in an arrow annotation resolves to a type parameter of the
> *enclosing* `fn`/`extern`, else to a declared type, else to an ordinary
> opaque nominal that unifies with nothing — never to a fresh type parameter.
> Otherwise a one-letter typo would silently wildcard, and unlike a `fn` there
> is no `[T]` list on an arrow to turn the implicit heuristic off with
> (docs/generics.md).

**What an arrow with none of the three types as.** `v => v + 1` types
`(Any) -> Any`. Every arrow has a type: its arity is syntactic and therefore
always known, so every call through an arrow value is arity-checked, and the
unknown components are the `Any` wildcard the rest of the checker already
lives with. The result is inferred from the body only when **no unknown
parameter occurs free in it** — `(x) => "s"` is `(Any) -> Str`, while
`(x) => x + 1` keeps an unknown result, because a body typed under unknown
parameters produces half-solved types (`[x]` infers `List[Never]`,
`{ a: x }` infers `{a: Any}`) that look known and are not.

### Calls through a function value

A call whose callee has a function type is checked like any other call: arity
first, then arguments, and it has the declared return type.

```revl reject
fn d() -> Int {
  let g: (Int) -> Int = v => v
  return g(1, 2)   // `g` is a `(Int) -> Int` and takes 1 argument(s), 2 given
}
```

A local of function type shadows a same-named top-level `fn` or ADT case at
the call site.

### Variance

Parameters are **contravariant**, results **covariant**, and arity is part of
the type:

```revl sketch
fn f(g: (Int) -> Float) -> Float { return g(1) }

f((v: Float) => v)   // ok — a function accepting Float accepts an Int too
f((v: Int) => v)     // ok — Int result widens to Float
```

but a `(Float) -> X` position will not accept an `(Int) -> X`: the position
may pass a `Float`, and the callee would receive one where it declared `Int`.
The elementwise rule the rest of the algebra uses would have made parameters
covariant, which is the classic unsound direction.

### Diagnostics

Every mismatch is a `T1`/`type-mismatch` diagnostic naming both sides:

```
the body of this arrow (from argument 1 of `f(...)`) expects `Int`, got `Str`
parameter `v` of this arrow (from argument 1 of `f(...)`) expects `Float`, got `Int`
argument 1 of `f(...)` expects `(Int, Int) -> Int` — 2 parameter(s), but this arrow declares 1
`let g: Int` expects `Int`, got an arrow
  hint: an arrow is a function value; write the expected type as a function
        type, e.g. `(Int) -> Str` (docs/function-types.md)
```

---

## 3. IR

An arrow node carries its signature when the checker recovered one:

```json
{ "kind": "arrow",
  "params": ["v"],
  "param_types": ["Int"],
  "returns": "Int",
  "captures": [],
  "body": { ... } }
```

`param_types` and `returns` are **absent together** exactly when the arrow has
no complete parameter signature — that is, they are written iff **every**
parameter type is known. A backend can therefore distinguish "typed as `Any`"
from "no type at all", which is the distinction the TypeScript emitter needs.
This is a breaking IR change; there are no external consumers.

The checker's *partial* knowledge deliberately stays in the frontend: a
half-annotated `(x: Int, y) => …` carries neither key rather than a
`["Int", null]` the contract never admitted. `returns` may still be the string
`"Any"` when the result is unknown but every parameter is not.

Nothing else in the IR changed shape. A `let` step does *not* carry its
annotation: the annotation's whole job is to be the checking position, and it
is already reflected in the arrow node and in the enclosing declared types.

---

## 4. Tier support

| tier | values of function type | how |
|---|---|---|
| **python** | ✅ implemented | arrows are `lambda`s; a declared function type renders as `Callable[[…], …]` in record annotations |
| **typescript** | ✅ implemented | real parameter types on arrows; `(Int, Str) -> Bool` renders as `((a0: number, a1: string) => boolean)` |
| **rust** | ◐ parameter/return positions | a `fn`/`extern` parameter or return lowers to `impl Fn(..)`; an *escaping* position (field, ADT payload, container element, service signature) is still refused; local arrows still lower |
| **java** | ⛔ refused, explicitly | a *declared* function type is refused; local arrows still beta-reduce as before |
| **wasm** | ⛔ refused, explicitly | same; local arrows still inline as before |

### rust: position-aware lowering (item 91)

rust used to refuse **every** declared function type. It no longer does. The
key observation is that the choice of Rust lowering is not a guess once the
*position* is known — it is exactly the choice a Rust programmer makes by hand:

- a **`fn`/`extern` parameter or return** lowers to **`impl Fn(P, ...) -> R`**.
  rustc monomorphises it, so `fn agent_loop(complete: (Str) -> Str, ...)`
  becomes `fn agent_loop(complete: impl Fn(String) -> String, ...)` — the
  callback is called directly (`complete(prompt)`), no boxing, no lifetime
  annotation. A function *returned* (`fn adder(n: Int) -> (Int) -> Int`) is
  `-> impl Fn(i64) -> i64`, and the `move |v| ...` arrow the body already
  emits is returned as-is.
- an **escaping position** — a struct field, an ADT payload, a `List`/`Opt`/
  `Map` element, or a service-method signature (whose trait must stay
  object-safe for `Box<dyn Service>`) — wants `Box<dyn Fn(..)>` *constructed at
  the point the arrow is created*. revl's type still does not carry enough
  position to do that boxing at the construction site, so these remain refused
  **by name** rather than erased to the opaque `Value` fallback.

The emitter threads the position (`param` / `return` / the default escaping
`value`) through `_rust_type`; a function type nested inside a container is
always the escaping position, so `List[(Int) -> Int]` is refused even as a
parameter. Locals were never affected — an arrow bound to a `let` and called
in the same body lowers because rustc infers its closure type.

### What "refused" means, precisely

java and wasm refuse **a function type written in a declaration** — a
`fn`/`extern` parameter or return, a service method signature, a record field,
an ADT payload, a config field; rust now refuses only the *escaping* subset
above. None of the tiers refuse arrows: an arrow bound to a local `let` and
called in the same body lowers on all six tiers, exactly as it did before this
change. Nothing that compiled on those tiers before compiles differently now.

The refusals are deliberate limits with reasons, not silent gaps:

- **rust** (escaping positions only) — a struct field, a `Vec`/`Opt`/`Map`
  element, an ADT payload, or a service-method signature wants
  `Box<dyn Fn(..)>` with an explicit lifetime, boxed where the arrow is
  created. revl's type carries the declaration position but not the arrow's
  creation site, so the emitter cannot insert that `Box::new(..)` without
  guessing. Without the refusal, `_rust_type` would erase the function type to
  its opaque `Value` fallback and emit code that compiles and means something
  else. Parameter and return positions do not have this problem and now lower
  (see above).
- **java** — a Java lambda needs a *nominal* target type, and the JDK's
  functional interfaces are neither generic over arity nor usable with
  primitives without boxing: `(Int) -> Int` is `IntUnaryOperator`, `(Int, Str)
  -> Bool` has no JDK interface at all, and arity 3+ needs a generated
  interface per shape. Generating those is a coherent design; guessing one is
  not. Without the refusal, `_java_v3_type` would erase to `Object`.
- **wasm** — the emitted module is wasm MVP: no closures, no function
  references, no table of typed funcrefs. A function value has no
  representation at all, so the tier's existing i32/value gate is extended to
  say so by name rather than reporting a confusing downstream error.

Each refusal names the type, the tier, the reason, and this document.

### MCP projection

`revl.mcp.schema` projects a function type to `{"x-revlType": "(Int) -> Str"}`
— its existing honest degradation for a type JSON cannot represent. A service
operation with a function-typed parameter is therefore exposed as a tool with
an unconstrained argument, which is accurate: nothing on the wire can carry a
function.

---

## 5. Deliberately out of scope

These are named limits, not oversights. None of them is half-implemented.

- **Closures over mutable state.** The capture rule is unchanged: an arrow
  snapshots a `var` **by value** at the point the arrow is created
  (syntax-2.0 §3.5), and a `var` never escapes its function. A function value
  returned from a `fn` therefore cannot observe later mutation of a `var` it
  captured. Making that work would require a shared mutable environment, which
  is precisely what §3.5 rules out to keep `fn` bodies pure in meaning.
  Capturing an immutable `let` or a parameter works and is used by
  `fn adder(n: Int) -> (Int) -> Int { return v => v + n }`.
- **Generics over function types.** A `fn`'s implicit type parameters (a
  single-uppercase name, see `typecheck.py`) unify against function types
  positionally, so `fn twice(g: (T) -> T, x: T) -> T` binds `T` — but there is
  no way to abstract over a function type's *arity* or to write a bound. `fn
  map_(xs: List[A], f: (A) -> B) -> List[B]` is not expressible, because `B`
  would have to be solved from the arrow's body and the checker does no such
  inference.
- **Function values in component bodies (stratum 3).** The syntax is accepted
  everywhere, but the IR-level checker (`infer_ir`, used for component and
  `provide`-method bodies) types no arrow and no call through one. A function
  type in a service signature is checked *at the boundary* — the provider's
  parameter annotation must match the service's — and then goes unchecked
  inside the method body. Stratum 1 (`fn` and `test` bodies) is where function
  types are checked.
- **Calling a function through a field directly.** `h.run(1)` is a builtin
  method call to the lowerer, and there is no builtin `run`; bind it first
  (`let r: (Int) -> Str = h.run  return r(1)`). The existing diagnostic
  already says so.
- **Function references to named `fn`s.** `compose(inc, dbl)` requires `inc`
  and `dbl` to be values; a top-level `fn` name is not one. Wrap it:
  `let inc: Step = v => inc_impl(v)`.
