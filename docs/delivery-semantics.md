# Delivery semantics — idempotency in the IR (roadmap item 44)

Every `emission` is irreversible: it crosses the system boundary and nothing
accumulates to undo it. That made every emission **at-most-once** by default —
retrying a delivery is only safe when re-delivering is defined to have the
same effect as delivering once, and nothing in the IR distinguished that case.
Item 44 promotes idempotency to a **checked IR property** so the runtime can
earn, per emission, the right to auto-retry a transient failure.

## The declaration

```revl
service Store {
  fn get(key: Str) -> Str
  emission idempotent fn put(key: Str, value: Str) -> Int
}
```

`idempotent` is a modifier on an `emission` — the sibling of `commutative`
(Def. 39), which set the precedent for algebraic properties as declarations.
It lowers to `"idempotent": true` on the service method in the IR, and a
document carrying it emits `ir_version: 3` (the property tier).

Two rules bound it:

- **Only an emission may claim it.** A plain `fn` never crosses the boundary,
  so there is nothing to re-deliver — the parser refuses the claim rather than
  silently dropping it:

  ```revl reject
  service Store {
    idempotent fn peek(key: Str) -> Str
  }
  ```

- **It is the author's claim, stated as such.** The compiler checks the
  *shape* (an emission declares it), not the *behaviour*. Like
  safe-by-spec at the OpenAPI boundary, this is the API author asserting
  something about their server — `f(f(x)) == f(x)` round-tripped against a
  recording (roadmap item 37's property tests) is the natural *verified
  idempotent* upgrade.

## Imported evidence

`revl import openapi` writes the claim for exactly the verbs RFC 9110 §9.2.2
defines as idempotent *among emissions* — `PUT` and `DELETE`:

```revl
service Probe {
  emission idempotent fn put_thing()
}
```

The same evidence rule as the rest of the import family: the claim is written
next to the operation it applies to, and an operation the importing engineer
weakened to plain `fn` (which has no delivery) imports without it. `POST` and
`PATCH` are not idempotent by specification and stay a bare `emission fn`.

## What the runtime may do with it

The python reference runtime (`backends/python/runtime.py`) implements the
delivery contract. A host signals "the emission did not durably land" by
raising `TransientError`; the runtime retries it **iff** the checked property
says the emission is idempotent:

- `idempotent` emission + `TransientError` → auto-retry (default budget 3);
- any other emission + `TransientError` → exactly one attempt, failure
  propagates (a second delivery could double the effect);
- any non-transient exception → never retried, idempotent or not.

That is the point of making idempotency a checked flag: transient-failure
resilience becomes a verified property of the type system instead of a
wrapper library's guess, and the retry right exists precisely where the IR
says it does — nowhere else.
