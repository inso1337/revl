# Explicit generic declarations

revl functions are generic in two spellings, both of which mean the same thing
to the checker and produce the same IR.

## Implicit (unchanged)

A single-uppercase name in a `fn`/`extern` signature that is not a declared
type is that function's type parameter:

```revl
fn id(x: T) -> T { return x }
fn head(xs: List[T]) -> T { return xs[0] }
```

`T` is a wildcard only inside the function's own body; at every call site it is
unified against the actual arguments. A single-uppercase name that *is* a
declared type (`type S = A | B`) is an ordinary nominal type and is checked
normally — the heuristic never wildcards a real type.

## Explicit (roadmap item 6)

An optional `[T]` / `[T, U]` list after the function name declares the type
parameters by name:

```revl sketch
fn id[T](x: T) -> T { return x }
fn map_[A, B](xs: List[A], f: (A) -> B) -> List[B] { ... }
fn first[Elem](xs: List[Elem]) -> Elem { ... }
```

The explicit form is a strict **superset** of the implicit one:

- It can name a parameter the single-uppercase heuristic would miss — a
  multi-character name like `Elem`, which the implicit rule treats as a nominal
  type. Only the explicit `[Elem]` makes it a type parameter.
- It states intent: the reader sees the parameters a function is generic over
  without scanning the signature for one-letter names.

Everything downstream is identical to the implicit form: the declared names
become that function's type parameters, are wildcards inside its body, and are
unified against the actual arguments at each call site by the same
`collect_tparams` / `unify` / `substitute` machinery in `typecheck.py`.

## An explicit list turns the implicit heuristic OFF (roadmap 75(c))

A signature that carries an explicit `[T]` list does **not** also collect
implicit type parameters. Declared means declared:

```revl reject
fn typo[T](xs: List[U]) -> T { return xs[0] }   // `U` is undeclared
fn call_it() -> Int { return typo([1, 2]) }     // refused: expects List[U], got List[Int]
```

`U` is an ordinary undeclared (opaque nominal) type, exactly like the
multi-character `Row` in `-> List[Row]`: it types its own positions
consistently but never unifies, so a use that conflicts with a real type
errors (`argument 1 of `typo(...)` expects `List[U]`, got `List[Int]``)
instead of silently becoming a second type parameter and wildcarding at every
call site. With no explicit list the heuristic is unchanged: a one-letter
undeclared name in a `fn`/`extern` signature is still that function's implicit
type parameter (the two forms can coexist in one program; each signature is
decided on its own list). This is what closes the hygiene hole the implicit
rule could not turn off: `[T]` is how an author says "these and only these
are the parameters".

**Rule G: an arrow never quantifies (roadmap 75(a)).** An arrow is an
expression, not a signature, so a type name written in one of its parameter or
return annotations is resolved, never collected. It means, in order: a type
parameter of the **enclosing** `fn`/`extern` signature (implicit or explicit),
so `fn id[T](x: T) -> T { let f = (v: T): T => v  return f(x) }` means that
`T`; else a declared type, record, variant or alias; else an ordinary opaque
nominal, which types its own positions consistently and unifies with nothing.
Never a fresh type parameter — otherwise `(x: Q): Q => x` would wildcard at
every call site and reopen exactly the hygiene hole above, this time with no
`[T]` list anywhere for an author to reach for. Locked by
`examples/rejections/t35_arrow_annotation_not_quantified.rvl`. Bounds are
deferred here too: an arrow annotation is a plain type, and there is no place
on an arrow to write one.

**Bounds stay deferred.** `[T: Ord]` is not accepted — the list remains a
plain name list, and there is no constraint machinery anywhere. No consumer
demands bounds yet; when one does, the parameter list is where they will
attach, but the checker does not build the machinery ahead of demand.

## Where the list is allowed

| Declaration form            | Takes `[T]`? |
| --------------------------- | ------------ |
| `fn` / `pub fn`             | yes          |
| `extern pure/acquire/emission fn` | yes    |
| service `provide`-methods   | no           |

`fn` and `extern` are allowed because they share one signature table
(`_signature_table` in `lower.py`), so the same machinery covers both with no
extra checker code — a generic host binding like
`extern pure fn identity[T](x: T) -> T` unifies at its call sites exactly as a
generic `fn` does.

Service methods are **not** entries in that shared table and are checked
through a separate path, so the explicit list is scoped out for them; `[` after
a method name still fails to parse as before. This keeps the feature to the one
place the unify machinery already lives.

## Shadowing: rejected, not allowed

A name in `[...]` may **not** collide with a builtin type (`Int`, `List`, …) or
a user-declared type:

```revl reject
type S = A | B
fn f[S](x: S) -> S { return x }   // error: type parameter `S` shadows a declared type
```

This is a deliberate choice over scoped shadowing. revl's implicit-generics
rule specifically closed the hole where a one-letter name silently wildcarded a
real `type S = A | B`; letting an explicit `[S]` reopen it — even scoped to one
body — would resurrect the same "is this the ADT or a type variable?"
ambiguity. Renaming the parameter is cheap and unambiguous. Duplicate names in
the list (`[T, T]`) and an empty list (`[]`) are also rejected.

(Type *aliases* — `type S = Int` — are resolved away before the signature table
is built, so they are transparent substitutions and are not "declared types"
for this check; an alias name is gone by the time shadowing is evaluated.)

## Function types unify positionally

A type parameter inside a function-type annotation unifies positionally,
element by element, because a function type normalises to an ordinary type
application (`(A) -> B` → head `->`, arguments `[A, B]`) and `unify` recurses
into arguments. In `map_[A, B](xs: List[A], f: (A) -> B) -> List[B]`, `A` is
learned from `xs` and threaded into the function-type argument; `B` is learned
from the function's result and threaded into `List[B]`. Full higher-kinded
types are out of scope.

## The marker never reaches the IR

Marking is purely the checker's internal view. `_signature_table` marks the
declared parameters (`T` → `?T`) so the rest of the checker can tell a
universally quantified `T` from a nominal type, but `_lower_fns` /
`_lower_externs` emit the author's spelling. A function's IR is **byte-identical
whether its parameter was implicit or explicit** — the explicit form is a
front-end convenience that leaves lowering and every backend untouched. This is
pinned by `test_explicit_and_implicit_emit_identical_ir` and
`test_explicit_type_parameter_marker_never_reaches_the_ir` in
`tests/test_generics_explicit.py`.
