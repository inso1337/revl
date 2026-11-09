# The stdlib Memory module (issue #2696)

**The need:** a long-lived, model-driven component keeps notes between turns.
Today that memory is whatever store a provider offers, typed as plain values, so
an item written from an untrusted source and read back later can lose the
qualifier at the store boundary, and nothing says which policy the item was held
under. The pieces that make a stored item safe already exist separately:
`Untrusted[T]`, `Retained[T, P]`, realm placement, and the refusal of a
`Secret[T]` flowing to a prompt. `stdlib/memory.rvl` is the construct that ties
them to one stored item.

It is a **library**, not a new language construct. It adds no syntax and no
checker rule: `put`, `get` and `forget` are three `kv`-scoped crossings, and the
existing taint, retention and confidentiality checks do the enforcement.

## The surface

`use "stdlib/memory.rvl" { put, get, forget, MemoryMeta }`

| fn | type | meaning |
|---|---|---|
| `put(key, value, meta)` | `[T](Str, Secret[T], MemoryMeta) -> Bool` | write one item into the `kv` resource |
| `get(key)` | `[T](Str) -> Opt[T]` | read one item, `None` when absent |
| `forget(key)` | `(Str) -> Unit` | drop one item |

A generic call infers `T` from the binding, not from an explicit type argument:
write `let back: Opt[Str] = emit get("key")`, not `get[Str](...)`.

`MemoryMeta` is a plain record:

```
pub type MemoryMeta = { origin: Str, purpose: Str, policy: Str }
```

## What the checker enforces

The value's **qualifier** is the enforced property, and it is the qualifier the
value had when it was put. The module does not strip it, re-mint a weaker one,
or ask an author to re-state it.

- **Untrusted.** An `Untrusted[Str]` item put and read back is still untrusted:
  a `Trusted[T]` sink refuses the read value with `G9`, exactly as it refused
  the value before storage. The read does not launder the origin at the store
  boundary.
- **Retention.** A `Retained[Str, P]` item past `P`'s deadline is refused at
  `put` with `G-RETAIN`, naming the `kv` crossing and the policy. The check is
  the same one every other persistence sink raises; the module does not invent a
  second retention rule. A declared legal `hold` overrides the deadline as it
  does at every other sink.
- **Confidential / not for a prompt.** A not-for-prompt item is typed
  `Secret[T]`. `put` declares its value `Secret[T]`, the receiver half of the
  rule, so the item can be stored; the read restores the `confidential` origin,
  and every receiver that does not declare `Secret[T]` refuses it with
  `G-SECRET-FLOW`: a log, an ordinary serialization, an MCP tool return, or a
  model prompt crossing such as `emission[model.complete] fn ask(p: Str)`.
  `put`'s receiver is **admission-only**: a plain value is admitted unchanged and
  is not made confidential by the declaration.

The pairing is the mechanism. `put` and `get` declare the **same** `kv` resource
scope, so the compiler's #1983 persistence-resource fixpoint treats the write and
the read as siblings of one shared resource: whatever entered it at any crossing
is reachable by any later read, in order and across bodies. That is what carries
the origin back out. Splitting the write and the read across different scopes
would make the read clean and the store would launder the origin it exists to
keep.

One conservative side effect: because `put`'s value is a declared `Secret`
parameter, the runtime audience recorder redacts the written argument in its
trace. A memory write is where a confidential item may land, so the plain item's
trace is redacted too rather than risk the other way.

## The metadata is informational

`MemoryMeta`'s three fields are **data**, not checks. The checker reads none of
them:

- `origin` is a label (`"web"`, `"input"`, `"local"`, ...), useful for the audit
  trail. It does not mint taint; the value's qualifier does.
- `purpose` is a label for the reader. It grants and removes nothing.
- `policy` is the retention policy's **name**, as a string. It is not a
  `Retained[T, P]` declaration: a plain value whose `policy` field names an
  expired policy is admitted, because the field is not the qualifier. A value
  that really is retained is refused at `put` by the qualifier it carries.

Keep the fields accurate so the trail is readable, and rely on the types for the
refusal. This is the honest split: a field a caller can write is not a check.

## What is not covered

A value **computed** from a stored item (an embedding, a summary, a conclusion)
is a derivative, and derivative handling is the separate design question tracked
by issue #2695. This module stores and returns an item unchanged; it does not
invent a second rule for derivatives.

## Tier status

The three crossings carry a py-tier reference store: an in-process map, enough
to run and test the module today. A real deployment backs the same three crossing
names with its own host store; the types, the taint and the retention enforcement
sit above the crossing and do not change with the backend. Other tiers are a
follow-up, exactly as `stdlib/net.rvl`'s py-tier crossing is.

## An example

`examples/memory_service.rvl` is a `Notebook` component: `remember` stores a
fetched page as an untrusted item, `recall` reads it back and logs it (a log is
not an authority sink, so an untrusted value is admitted there), and `drop`
forgets it. Use `endorse[web](page, reason = "...")` at a declared point when a
stored untrusted item must reach a `Trusted[T]` sink, the same declassifier every
other untrusted value uses.
