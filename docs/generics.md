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
