# Design: arrow parameter annotations (item 75(a))

Status: design proposed. Spec only. No `src/`, `backends/`, `selfhost/` or
`formal/` change is made by this document.

Base: `origin/main` @ `c9d579e`. Every `file:line` anchor was read at that sha,
and every claim below about what the compiler does *today* was produced by
running the compiler at that sha, not by reading the roadmap. That distinction
matters here more than usual, because the roadmap's item text predates a large
landing and is now partly stale (§1).

Roadmap: docs/v2.0-roadmap.md item 75(a) (line 2333), deferred to wave 9 and
spec-first. Errata: docs/contract-errata.md "Arrow *values* have no type"
(line 432). Prior art this builds on: docs/function-types.md.

---

## 0. The one guarantee this item exists to deliver

> **Core guarantee.** Every arrow has a type. Its arity is always known, so
> every call through an arrow value is arity-checked; and where the author
> annotates, or where the arrow's result does not depend on anything unknown,
> the result type is known too and is checked wherever it flows.

The negative form is the errata's own trigger:

```text
// what the compiler accepts today, at c9d579e, and must refuse after this item
fn takes_int(n: Int) -> Int { return n }
fn demo() -> Int {
  let f = (x) => "s"
  return takes_int(f(1))     // a Str reaches an Int position, silently
}
```

Verified accepted at c9d579e (it emits a clean `ir_version: 3` document). So
is `f(1, 2, 3)` on the same one-parameter arrow.

---

## 1. What already shipped, and what is actually left

The roadmap's item text says "an arrow's params are un-annotated". That was
true when the item was written and is no longer true. Commit `f14e305`
(`feat(types): typed function values`) landed function types and, with them,
**arrow parameter annotations**. Confirmed by running the compiler:

| probe | at c9d579e |
|---|---|
| `let f = (x: Int) => "s"` then `takes_int(f(1))` | **refused**: ``argument 1 of `takes_int(...)` expects `Int`, got `Str` `` |
| `let f = (x: Int) => x + 1` then `f(1, 2)` | **refused**: ``` `f` is a `(Int) -> Int` and takes 1 argument(s), 2 given ``` |
| `let f: (Int) -> Str = (x) => "s"` then `takes_int(f(1))` | **refused** (checking position) |
| `let f = (x: Int): Int => x + 1` | **parse error**: `expected ), found ':'` |
| `let f = (x: Int) -> Int => x + 1` | **parse error**: `expected ), found ':'` |
| `let f = (x) => "s"` then `takes_int(f(1))` | **accepted** |
| `let f = (x) => "s"` then `f(1, 2, 3)` | **accepted** |
| `let f = (x: Int, y) => x + 1` (partial) | accepted, and lowers `"param_types": ["Int", null], "returns": null` |

The parser already has the annotated-parameter production
(`src/revl/parser.py:4267` `_arrow_params_ahead`, `:4271` the per-parameter
`:` branch), the checker already reconstructs a signature from it
(`src/revl/typecheck.py:1725` the `ExprArrow` branch, `:1757`
`_resolve_arrow`), and lowering already writes it into the IR
(`src/revl/lower.py:4670`). docs/function-types.md §2(b) documents it.

So the residual gap is **three specific things**, and this design is about
those and nothing else:

**(R1) There is no return-type annotation.** Both candidate spellings are hard
parse errors. An arrow whose parameters are annotated but whose body types to
nothing still has an unknown result, and an arrow with *no* parameters
(`() => host_call()`) has no annotation site at all.

**(R2) An arrow with any un-annotated parameter has no type at all.**
`src/revl/typecheck.py:1743`:

```python
if expr.params and any(a is None for a in annotations):
    return None
```

One bare parameter throws away everything the checker does know, including the
arity, which is purely syntactic and never in doubt. This is what leaves the
errata's reproducer silent, and it is the larger half of the gap by volume:
`v => v + 1` is the common spelling.

**(R3) The partial case already writes a malformed IR node.** The condition at
`src/revl/lower.py:4675` is `if any(p is not None for p in param_types) or
expr.returns:`, so a partially annotated arrow emits `param_types` containing
JSON `null` and `"returns": null`. docs/backend-ir-v3.md ("`arrow` and function
types") contracts that the two keys are **absent together** exactly when the
arrow is untyped, and `tests/test_function_types.py:189` pins the absent case,
but nothing pins the partial case. This is a live latent bug at c9d579e, found
while grounding this design, and it must be fixed by whichever slice touches
the lowering condition (which is slice 1).

---

## 2. Grammar

### 2.1 The production

```
arrow        ::= NAME '=>' pure_expr                       // unchanged
               | '(' [ params ] ')' [ ':' type ] '=>' pure_expr
params       ::= param { ',' param }
param        ::= NAME [ ':' type ]
```

The only new token sequence is `')' ':' type` before `'=>'`. Everything else
is exactly what `src/revl/parser.py` accepts today.

```revl sketch
let inc  = (v: Int): Int => v + 1        // both halves annotated
let name = (u): Str => u.name            // return only
let zero = (): Int => 0                  // no parameters, return annotated
let raw  = (v: Int) => v + 1             // parameters only (ships today)
let bare = v => v + 1                    // neither (ships today)
```

The bare single-parameter form takes no annotation of either kind:
`v: Int => e` and `v: Int => e` stay parse errors, because TypeScript refuses
them too and because the parenthesised list is where every other revl
annotation site lives.

### 2.2 Why `:` and not `->`

Roadmap §0's governing principle is *same meaning, same syntax*.
docs/function-types.md records that the principle's premise **failed** for the
function *type*: TypeScript writes `(a: number) => boolean` and requires the
parameter name, revl's function type carries no parameter names, so the two
constructs do not mean the same thing and revl kept `->`.

For the arrow *expression* the premise **holds**. TypeScript's
`(x: number): number => x` and revl's `(x: Int): Int => x` are the same
construct: a parameter list with names, an optional result type, a single
expression body. Nothing is elided, nothing is added. So §0 decides it, and the
roadmap's "the spelling should be TypeScript's, per §0" is satisfied literally
rather than by analogy.

Three supporting reasons, in case a reviewer wants to reopen `->`:

- `) -> T` is already the tail of a function *type*. Writing
  `(x: Int) -> Int => x` next to `(f: (Int) -> Int) => x` puts two readings of
  the same three tokens in the grammar at nearly the same place. A depth-aware
  scanner tells them apart (the existing `_arrow_params_ahead` already balances
  brackets), so this is a readability hazard rather than an ambiguity, but it
  is a real one.
- `:` already means "the type of this named thing" everywhere in revl:
  `let x: Int`, `fn f(p: Int)`, `{ name: Str }`, and the arrow's own parameter
  annotations. `(x: Int): Int` reads consistently with all of them.
- `:` collides with nothing. After the `)` of an arrow parameter list, no
  existing production may begin with `:`, which is why the current parser
  reports `expected ), found ':'` there.

### 2.3 Parser mechanics

`_arrow_params_ahead` (`src/revl/parser.py:4457`) decides "parameter list" vs
"parenthesised expression" by balancing to the matching `)` and requiring `=>`
next. It must be extended to also accept `)` `:` `<type>` `=>`. The type is
parsed with the existing `type_()`, which is greedy and cannot consume `=>`, so
the lookahead terminates cleanly and the existing "one type in a group is that
type, two or more is refused" rule (docs/function-types.md, "Precedence and
grouping") is untouched.

The return type is a full type, so `?` binds to it: `(x: Int): Str? => e` is an
arrow returning `Opt[Str]`, matching the table in docs/function-types.md.

### 2.4 A required AST change

`ExprArrow.param_types` (`src/revl/parser.py:809`) is dual-purpose today: the
parser writes the author's annotations into it and the checker *overwrites* it
with what it resolved. That conflation is exactly what makes the R3 lowering
condition wrong, and it would make the async rule in §5.2 unimplementable
(nothing downstream could tell an author-written return from a checker-derived
one). Slice 1 splits the field:

| field | written by | meaning |
|---|---|---|
| `written_param_types` | parser | the author's `(v: Int)` annotations, `None` where bare |
| `written_returns` | parser | the author's `): Int` annotation, `None` if absent |
| `param_types` | checker | the resolved parameter types, or `None` where still unknown |
| `returns` | checker | the resolved result type, or `None` |

Lowering reads only the checker fields. `arrow_annotations`
(`src/revl/typecheck.py:1749`) reads only the written ones.

---

## 3. Typing

### 3.1 Signature reconstruction

Let an arrow have parameters `p1..pn`, written parameter annotations `A1..An`
(each a type or absent), a written return annotation `Rw` (a type or absent),
and a checking-position expectation `E` (a function type or absent). Write `⊥`
for "not known".

**Parameters.** For each `i`:

```
Pi  =  E.param[i]   if E is known
       Ai           else if Ai is written
       ⊥            otherwise
```

When `E` is known *and* `Ai` is written, both are used: `Ai` must be
compatible with `E.param[i]` under the existing contravariant rule
(`src/revl/typecheck.py:652`), and a mismatch is the existing diagnostic
``parameter `v` of this arrow (from argument 1 of `f(...)`) expects `Float`,
got `Int` ``. The position wins for the body's environment, which is today's
behaviour and stays.

**Return.**

```
R   =  Rw           if written
       E.return     else if E is known
       infer(body)  else if the body is ⊥-parameter-independent (§3.2)
       ⊥            otherwise
```

When `Rw` is written and `E` is known, `Rw` must be compatible with
`E.return` (covariant), same diagnostic family.

**Value type.** The arrow's type is **always** the function type
`(P1, ..., Pn) -> R`, with every `⊥` rendered `Any`. `infer_ast` never returns
`None` for an `ExprArrow` again. Arity `n` is syntactic and therefore always
known, which is the whole point of the rule.

### 3.2 The ⊥-parameter-independence rule

Inferring an arrow's result from a body typed under unknown parameters is not
generally sound, and the checker at c9d579e demonstrates it. Probing
`infer_ast` on arrow bodies with the parameter popped out of the environment:

| body | inferred |
|---|---|
| `"s"` | `Str` |
| `1 + 2` | `Int` |
| `x` | `None` |
| `x + 1` | `None` |
| `x.length` | `None` |
| `[x]` | `List[Never]` |
| `{ a: x }` | `{a: Any}` |

`x + 1` is honest, but `[x]` collapses the element to `Never` and `{ a: x }`
collapses the field to `Any`. Those are *half-solved* types: they look known
and are not, and letting one become an arrow's declared result is precisely
the "accepted but not enforced" shape this design must avoid.

So the rule is syntactic and conservative:

> **infer the result from the body only when no `⊥` parameter occurs free in
> the body.**

`(x) => "s"` has no free `x`, so it types `(Any) -> Str` and the errata's
reproducer becomes an error. `(x) => x + 1` mentions `x`, so its result stays
`⊥` and the arrow types `(Any) -> Any`, which is exactly as permissive as
today. A parameter that *is* known (annotated or positional) never blocks
inference, so `(x: Int) => x + 1` keeps typing `(Int) -> Int` as it does now.

This deliberately under-approximates. `(x) => str_of(x)` with
`fn str_of(v: Int) -> Str` has a result that provably does not depend on `x`'s
type, and this rule still refuses to name it. Widening to "the body's type does
not depend on a `⊥` parameter" needs a dependency analysis, and solving `x`
from the body would be the whole-program inference docs/function-types.md §5
rules out. Named as an extension point, not built.

### 3.3 Call sites

A call whose callee's type is a function type already goes through
`call_function_value` (`src/revl/typecheck.py:1702`, `:1810`). Nothing changes
there. What changes is that arrows now *reach* it. Consequences:

- **Arity is checked, always.** Exact arity, since revl has no default,
  optional or variadic parameters. Existing message, reused verbatim.
- **Argument `i` is checked against `Pi`.** A `⊥` parameter renders `Any`,
  and `compatible` returns `True` for `Any` on either side
  (`src/revl/typecheck.py:590`), so a `⊥` parameter accepts everything. No new
  argument-position refusals arrive from this item; only annotations and
  checking positions produce them, exactly as they do today.
- **The call's type is `R`.** Which is what makes the roadmap's example an
  error at the *consumer*, not at the call.

### 3.4 What an un-annotated arrow types as, after this change

`v => v + 1` types `(Any) -> Any`. It is no longer "untyped" in the checker.
It is a function type whose components are all wildcards, so:

- calling it with the wrong number of arguments is refused (new, intended);
- calling it with any argument type is admitted (unchanged);
- its result flows anywhere (unchanged);
- using it where a non-function is expected is refused, with the existing
  ``an arrow is a function value; write the expected type as a function type``
  hint (unchanged, that path already fires on the syntactic node).

The errata's "Arrow *values* have no type" entry closes to: **arrow values
have a type; its unknown components are `Any`, and `Any` is the documented
gradual frontier that the rest of the checker already lives with.**

### 3.5 Diagnostics

Three shapes. Two already exist and are reused unchanged, which is the point:
this item mostly extends *reach*, not vocabulary.

The roadmap's own example, now an error:

```text
error: demo.rvl:4: argument 1 of `takes_int(...)` expects `Int`, got `Str`
```

Arity through an arrow value, now an error:

```text
error: demo.rvl:3: `f` is a `(Any) -> Str` and takes 1 argument(s), 2 given
  its parameters are `(Any)` - revl has no default, optional, or variadic
  parameters, so every call supplies exactly the declared arity
```

One genuinely new diagnostic, from §5.2:

```text
error: demo.rvl:2: an arrow may not declare its own async colour
  hint: an arrow is coloured by the position it flows into, e.g.
        `let g: (Int) -> Async[Str] = ...` or a parameter declared
        `(Int) -> Async[Str]`, because that declaration is what makes the
        consumer await it (docs/function-types.md, docs/design/async-function-values.md)
  code: A1  category: async-propagation
```

---

## 4. The IR, and why no emitter moves

This is the design decision that keeps the blast radius small, so it is stated
as a rule rather than left implicit.

> **The frontend gets stricter. The IR does not move.**

docs/backend-ir-v3.md contracts that `param_types` and `returns` are absent
together exactly when the arrow carries no signature, and three tiers depend on
that: TypeScript writes an explicit `any` for the absent case
(`backends/typescript/emit.py:826`), java and wasm beta-reduce called arrows
and refuse arrow *values* by name (`backends/java/emit.py:1089`,
`backends/wasm/emit.py:4072`), and go is specified to copy java
(docs/backend-go-v3.md §3). If every arrow suddenly carried a signature, those
tiers would start reading `Any` as a type they must render, and `Any` has no
java or wasm representation.

So lowering emits the two keys under a **tightened** condition:

```
emit param_types and returns  iff  every Pi is known (no ⊥ parameter)
```

`returns` may still be the string `"Any"` when the result is `⊥`, exactly as it
can today (`_resolve_arrow` maps an unrecovered body type to `"Any"`,
`src/revl/typecheck.py:1771`). Under this rule:

| arrow shape | IR today | IR after |
|---|---|---|
| all parameters known (annotation or position) | both keys | **byte-identical** |
| no annotation, no position | neither key | **neither key** (unchanged) |
| partially annotated | `["Int", null]` / `null` | **neither key** (R3 fixed) |

The only IR movement in the whole item is the third row, which is a malformed
node today. Every backend golden should be byte-identical unless the corpus
contains a partially annotated arrow, and if it does, that golden's diff is the
bug fix.

The checker's richer partial knowledge stays in the frontend. Carrying partial
signatures into the IR is a separate, demand-driven change (§7, slice 4), and
it would require a contract edit in docs/backend-ir-v3.md plus a decision per
tier. No tier has asked.

---

## 5. Interactions

### 5.1 Generics and type-parameter hygiene (75(c))

Item 75(c) closed a hole where the implicit one-letter heuristic could not be
turned off, so a typo'd one-letter type name silently quantified instead of
erroring (docs/generics.md, "An explicit list turns the implicit heuristic
OFF"). Adding a new annotation site is exactly how that hole could reopen: if
`(x: Q): Q => x` collected `Q` as a fresh type parameter, the arrow would
wildcard at every call site and the typo would be silent again, this time with
no `[T]` list anywhere for an author to reach for.

> **Rule G. An arrow is an expression, not a signature. It never quantifies.**

A type name written in an arrow's parameter or return annotation resolves, in
order:

1. a type parameter of the **enclosing** `fn`/`extern` signature, implicit or
   explicit (so `fn id[T](x: T) -> T { let f = (v: T) => v  return f(x) }`
   means the enclosing `T`, and compiles today at c9d579e);
2. a declared type, record, variant or alias;
3. an ordinary undeclared **opaque nominal** type, which types its own
   positions consistently and unifies with nothing.

Never a new type parameter. Concretely: `collect_tparams` must not be run over
an arrow's annotations, and the arrow branch must not extend the signature's
parameter set.

This is already the behaviour for parameter annotations. Probed at c9d579e:
`let f = (x: T) => x` inside a non-generic `fn`, then `f("s")`, is refused with
``argument 1 of `f` expects `T`, got `Str` ``, that is, `T` is opaque, not a
wildcard. The return annotation inherits the same rule by construction, and
`examples/rejections/t35` locks it.

Bounds stay deferred, per 75(c). An arrow annotation is a plain type, and there
is no place on an arrow to write one.

### 5.2 Async colouring (A1), and the CRITICAL

Colour lives in three places at c9d579e:

- `src/revl/lower.py:4680`, an arrow node gets `"async": true` iff
  `parse_type(expr.returns)[0] == "Async"`;
- `src/revl/lower.py:6879` `_refuse_leaky_pure_arrow`, a **sync-typed** arrow
  whose body reaches an async callable is the item-92 leak and is refused, and
  the guard is literally `node.get("kind") == "arrow" and not
  node.get("async")`, so an async-flagged arrow is *skipped*;
- `src/revl/emission_analysis.py:100`, callee collection with
  `stop_async_arrows=True` **stops descending** at an arrow carrying
  `"async": true`, so async callables inside it are invisible to the enclosing
  scan.

Today `expr.returns` is written only by the checker, from a checking position
or from the body type, and `Async[T]` is confined by well-formedness to a
function type's return. So `"async": true` holds only for an arrow that was
checked against a declared `(...) -> Async[T]` position, and that declaration
is what guarantees an awaiting consumer.

A return annotation would break that chain. See §8 for the exploit and the
rule that closes it. The rules this design adopts:

> **Rule C1. Colour is positional. An arrow may not declare its own colour.**
> A written arrow return annotation naming `Async[...]`, at the top level or as
> the return of a function type it names, is refused with the A1 diagnostic in
> §3.5.

> **Rule C2. An annotation never suppresses the leak scan.** With C1 in force,
> `node["async"]` is still set only from a checking position, so
> `_refuse_leaky_pure_arrow` and `stop_async_arrows` keep the meaning they have
> today. Lowering asserts this: `written_returns` never contributes to the
> `async` flag.

> **Rule C3. Coerced and nested arrows are unchanged (item 186).**
> `_coerce_async_args` stamps an arrow *argument* async when it lands in an
> `Async[T]` slot. If that arrow also carries a written return annotation, the
> annotation must be the **sync inner type** `T` and is checked against it; the
> coercion then applies exactly as it does now. Item 186's residual approximation
> (callee collection descending into a nested coerced arrow instead of stopping)
> is neither widened nor narrowed by this item: the stop set stays "arrows the
> checker coloured from a position", which under C1 is the same set as today.

> **Rule C4. Item 342 is untouched.** Sync/async arrow polymorphism decides
> colour at the *call site*, from the callee's declared parameter type, and
> monomorphises a sync clone (`src/revl/lower.py:6571`, extended to module `fn`
> and `test` call sites by item 387). Because an arrow cannot self-declare a
> colour, this item introduces no new colour source and 342's keying is
> unaffected. A `(x: Int): Str => ...` arrow handed to a
> `(Int) -> Async[Str]` parameter is coerced by C3, and 342 monomorphises the
> callee for the sync site exactly as before.

### 5.3 Stratum 3 (component and provide-method bodies)

docs/function-types.md §5 records that `infer_ir`, the checker used for
component and `provide`-method bodies, types no arrow and no call through one.
That is unchanged by slices 1 and 2 and is closed by slice 3. The grammar
change lands everywhere at once (there is one parser), so a return annotation
written in a component body parses and lowers from slice 1; it is simply not
*checked* there until slice 3. That asymmetry already exists for parameter
annotations, so slice 1 does not make it worse, but it must be stated rather
than discovered.

`src/revl/lower.py:6039` is the component-path arrow lowering site and carries
the same R3 condition as `:4675`. Both are fixed together in slice 1.

### 5.4 Self-host (item 391)

The standing process asks every reference-side language feature whether it
needs a self-host port. This one does. `selfhost/parser.rvl` already parses
annotated arrow parameters (`arrow_ahead` at `:598`, and the test `"arrow with
annotated params"` at `:970`), and `selfhost/lower.rvl` already carries the A1
arrow-colour rules, including a comment at `:183` naming item 186's residual.
The port is slice 2, and the byte-agreement oracles mean the diagnostics must
match the reference **exactly**, so C1's message text is fixed by slice 1 and
copied, not reworded.

---

## 6. Migration and backward compatibility

**The grammar is purely additive.** `(x: Int): Int => e` is a parse error today
(verified: `expected ), found ':'`), so no existing program contains the new
production and no existing program changes meaning. Nothing needs a
`revl fmt --migrate` pass, and no corpus file is rewritten for syntax.

**The checker is not purely additive, and that is the item.** Arrows that
typed to nothing now type to a partial function type, so some programs that
compiled will stop compiling. The new refusals are exactly two classes:

1. **A call through an arrow value with the wrong arity.** Never legal on any
   tier (rust E0061, java "cannot be applied to given types"), so every hit is
   a bug the strict tiers would have caught later.
2. **A result flowing out of a ⊥-parameter-independent body into an
   incompatible position.** The roadmap's own example. Also always a bug.

The design is deliberately shaped so nothing else can arrive:

- a `⊥` parameter renders `Any`, and `Any` is compatible in both directions, so
  no argument position gets stricter;
- the return stays `⊥` whenever the body mentions a `⊥` parameter, so no
  half-solved type (`List[Never]`, `{a: Any}`) is ever promoted to a claim.

**Migration procedure.** Sweep the corpus and the examples with the slice-1
build and fix each hit in place; there is no mechanical rewrite because there
is no spelling change. Expected hit count is small: the two classes are both
genuine defects, and the corpus is checked by six tiers.

**The tiers do not move**, by §4, except for the R3 null fix. Regenerate
backend goldens and expect a diff only where a partially annotated arrow
appears.

---

## 7. Blast radius and slice split

Stage columns are `src/revl/`. Emitter columns are the six under `backends/`.

| slice | parser | typecheck | lower | emitters | selfhost |
|---|---|---|---|---|---|
| **1. grammar + reconstruction** | yes | yes | yes | **none** | no |
| **2. self-host port** | no | no | no | none | parser, checker, lower |
| **3. stratum 3** | no | yes (`infer_ir`) | yes (component path) | **none** | follow-on |
| **4. partial signatures in the IR** (deferred) | no | no | yes | **all six** | all |

### Slice 1 (this is the implementable one)

Frontend only. No emitter file is opened.

1. `src/revl/parser.py`: extend `_arrow_params_ahead` (`:4457`) to accept
   `)` `:` `<type>` `=>`; parse the return type in the `(`-arrow branch
   (`:4267`); split `ExprArrow` (`:809`) into the four fields of §2.4.
2. `src/revl/typecheck.py`: replace the early `return None` at `:1743` with
   the reconstruction of §3.1; implement §3.2's free-occurrence test; render
   `⊥` as `Any` in `_resolve_arrow` (`:1757`, already does this for the
   return); check `Rw` against `E.return`; add the C1 refusal; make sure
   `collect_tparams` is not reached from an arrow annotation (Rule G).
3. `src/revl/lower.py`: tighten the emit condition at `:4675` **and** `:6039`
   to "every `Pi` known" (§4), which fixes R3; assert C2 at the `async` flag
   site (`:4680`).
4. Tests and rejections per §8.

Slice 1 is self-contained and independently shippable: it closes the errata
entry, closes the roadmap example, fixes R3, and leaves every tier byte-identical.

### Slice 3 note

Slice 3 is the larger of the two follow-ons because `infer_ir` is a separate
checker over IR rather than AST, and the arrow node reaching it has already
lost the author's annotations unless slice 1's split fields are threaded
through. The cheap version is to run the same reconstruction at the component
lowering site (`:6039`) where the AST is still present, and record the result
on the node, rather than to teach `infer_ir` about arrows. Decide when the
slice is picked up.

### Slice 4 is deferred, not planned

It exists in this document only so that a future tier that wants
`(Any) -> Str` in the IR knows what it is asking for: a contract edit in
docs/backend-ir-v3.md and six emitters, two of which (java, wasm) have no
representation for `Any` and would need a refusal rather than a rendering.

---

## 8. Adversarial review

### The CRITICAL: a written `Async` return annotation lets an arrow self-colour, and a self-coloured arrow escapes A1

The first draft of §2 allowed any type in the return annotation, on the
reasoning that the annotation is just another way to say what a checking
position says. That is false for exactly one type constructor.

At c9d579e, `"async": true` on an arrow node is set from `expr.returns`
(`src/revl/lower.py:4680`), and `expr.returns` is written only by the checker,
which only ever writes `Async[T]` there when the arrow was checked against a
declared `(...) -> Async[T]` position. That declaration is a **promise by the
consumer to await**. The flag is therefore not merely a label; it is a
certificate, and two separate analyses treat it as one:

- `_refuse_leaky_pure_arrow` (`src/revl/lower.py:6879`) refuses a sync-typed
  arrow that reaches an async callable, and **skips** arrows carrying the flag;
- `_calls_in(..., stop_async_arrows=True)`
  (`src/revl/emission_analysis.py:100`) **stops descending** at a flagged
  arrow, so async callables inside it are removed from the enclosing scope's
  reach set.

A written return annotation forges the certificate. With the first draft's
grammar:

```revl sketch
fn leak(x: Int) -> Str {
  let g = (v: Int): Async[Str] => suspending_op(v)   // self-coloured, no consumer
  return g(x)                                        // called synchronously
}
```

`g` carries `"async": true`, so the leak check skips it and the enclosing scan
never sees `suspending_op`. Four things follow, and they compound:

1. **A1 is bypassed for the whole enclosing scope**, not just for `g`. The
   flagged arrow is a stop node, so any suspending operation nested inside it
   disappears from the enclosing `fn`'s reach set. One annotation launders an
   arbitrary amount of async reach.
2. **The consumer does not await.** `g` is bound to a plain `let` and called in
   a sync body. Python emits an unawaited coroutine; TypeScript refuses at emit
   with "async callable called outside an async context, the frontend
   async-coloring check should have refused this (A1)". That is precisely the
   py/ts disagreement item 387 was filed to eliminate, reintroduced through a
   new grammar.
3. **Item 342 is defeated.** Sync/async monomorphisation keys on the *callee's
   declared parameter type* at the call site. A self-coloured arrow value has no
   such declaration, so no sync clone is materialised and the colour-polymorphic
   path is never entered.
4. **Item 186's residual is widened from latent to reachable.** 186 records
   that callee collection descends into a nested *coerced* arrow instead of
   stopping, and notes it is masked in practice because no fixture produces the
   shape. A self-colouring annotation is a fixture generator for that shape.

This is a soundness hole in the strict sense the task names: an annotation that
is *accepted but not enforced*, where the wrong thing that flows is a
suspension rather than a value.

### The fix

Rule C1 in §5.2, adopted into the design:

> An arrow may not declare its own async colour. A written arrow return
> annotation naming `Async[...]`, at the top level or as the return of a
> function type it names, is refused with A1.

Colour remains obtainable only from a position that also carries the obligation
to await: `let g: (Int) -> Async[Str] = ...`, a declared parameter, or a
declared `fn` return. Rule C2 makes the invariant checkable rather than
conventional (`written_returns` never contributes to the `async` flag, asserted
at the lowering site), and the §2.4 field split is what makes C2 expressible at
all. Rule C3 keeps the coerced-argument path, which is the *legitimate* way an
arrow acquires colour without a `let` annotation, working unchanged: the
annotation on a coerced arrow must be the sync inner type.

The rule costs nothing an author wants. The two spellings that reach an async
consumer both still work, and both are shorter than the refused one.

Locked by `examples/rejections/t34_arrow_self_declared_async.rvl` (§9), and by
a false-positive test that the coerced-argument and `let`-annotated spellings
still admit.

### Second finding (HIGH, not the CRITICAL): the R3 null leak

Found while grounding, not while designing: a partially annotated arrow already
emits `"param_types": ["Int", null], "returns": null` at c9d579e, violating
docs/backend-ir-v3.md's "absent together" contract. It is not the CRITICAL
because no tier currently mishandles it (TypeScript's renderer guards with
`isinstance(t, str)`), but it is a shape no emitter is contracted to accept, and
this item's slice 1 touches the exact condition that produces it. Fixed by §4's
tightened emit rule and pinned by a test asserting the keys are absent for a
partially annotated arrow.

### Third finding (MEDIUM): inference from a body with unknown parameters

The first draft inferred the arrow's result from its body whenever the body
typed to anything. Probing `infer_ast` showed `[x]` types `List[Never]` and
`{ a: x }` types `{a: Any}` with `x` unknown, so a draft that promoted those to
a declared result would have written a half-solved type into a function type
that then flows and unifies. Closed by §3.2's free-occurrence rule, which is
syntactic, decidable, and strictly conservative.

---

## 9. Exit tests

Following the t8 to t20 pattern the roadmap names: each rejection file names
the tier error it prevents, and false-positive tests keep every legal spelling
compiling.

### Rejections (`examples/rejections/`)

| file | source shape | code | tier error it prevents |
|---|---|---|---|
| `t32_arrow_value_result_flows.rvl` | `let f = (x) => "s"` then `takes_int(f(1))` | T1 | rust E0308, java incompatible types. The errata's own reproducer. |
| `t33_arrow_value_arity.rvl` | `let f = (x) => "s"` then `f(1, 2)` | T1 | rust E0061, java "cannot be applied to given types". The arrow-value twin of `t10_call_arity`. |
| `t34_arrow_self_declared_async.rvl` | `(v: Int): Async[Str] => suspending_op(v)` bound to a plain `let` and called synchronously | A1 | the py/ts split of item 387: python emits an unawaited coroutine, TypeScript refuses at emit. Locks the CRITICAL. |
| `t35_arrow_annotation_not_quantified.rvl` | `let f = (x: Q): Q => x` then `f(1)` used where `Str` is expected | T1 | the 75(c) hygiene hole, reopened through the arrow annotation: `Q` must be an opaque nominal, not a fresh wildcard. |

Each file carries the `// REJECTED - ...` header the existing `t*` files use,
naming the guarantee and the tier evidence.

### False positives (must keep compiling)

In `tests/test_function_types.py` unless noted.

**Nothing that compiles today stops compiling:**

- `v => v + 1`, bound and called with one argument;
- `(x) => x + 1` called as `f(1)` and as `f(1.5)`, both still admitted (the `⊥`
  parameter is `Any`, and the result stays `⊥` because the body mentions `x`);
- every snippet in docs/function-types.md, which the doc-examples gate already
  compiles;
- `apply_twice(v => v + 1, n)`, the checking-position path;
- an arrow in a `provide`-method body, and one in a component effect block
  (slice 3 must not regress slices 1 and 2);
- the item-342 fixture: one `fn loop(c: (X) -> Async[Y], ...)` called from an
  `async fn` and from a sync `emission fn`, both sites still admitted and the
  sync clone still monomorphised;
- `x => emit async_op(x)` coerced into an `Async[T]` slot, still admitted
  (Rule C3).

**The new spellings compile and type:**

- `let f = (x: Int): Int => x + 1` types `(Int) -> Int`;
- `let z = (): Int => 0` types `() -> Int`;
- `let n = (u): Str => "s"` types `(Any) -> Str`;
- `(x: Int): Str? => ...` types `(Int) -> Opt[Str]` (precedence);
- `(f: (Int) -> Int): Int => f(1)` parses (a function-typed parameter next to a
  return annotation, the case `_arrow_params_ahead` must balance correctly);
- inside `fn id[T](x: T) -> T`, `(v: T): T => v` means the enclosing `T`.

**IR and tier pins:**

- a partially annotated arrow's node carries **neither** `param_types` nor
  `returns` (replaces the `null` shape; extends
  `tests/test_function_types.py:189`);
- a fully typed arrow's node is unchanged;
- backend goldens byte-identical for python, typescript, rust, java, wasm and
  go, with any diff reviewed as the R3 fix;
- the six-tier execution test unchanged.

### Docs to update when slice 1 lands

- docs/function-types.md §2, add "(c) Return annotations" and Rule C1; §3,
  restate the IR condition as "every parameter known".
- docs/contract-errata.md, move "Arrow *values* have no type" from frontier to
  closed, with the residual named as stratum 3 (slice 3) and `⊥`-dependent
  results (§3.2's under-approximation).
- docs/generics.md, pin Rule G next to the 75(c) heuristic-off section.
- docs/backend-ir-v3.md, tighten the "absent together" wording to "absent
  together exactly when the arrow has no complete parameter signature".
- docs/v2.0-roadmap.md item 75(a), on landing, per the tick discipline (the
  gate green on the landed sha, and the shipped API matching the item text).
