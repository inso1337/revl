# Backend IR v1 — deltas over v0

> **Frozen v1 contract.** Everything added since lives in
> [docs/backend-ir-v3.md](backend-ir-v3.md) — realms, types, functions,
> externs, tests, and the component-body steps and expression kinds a
> backend must handle today.

v1 lands the amendments accepted in docs/contract-errata.md. The v0 document
(docs/backend-ir.md) stays frozen as the historical contract; this file lists
only what changes. `ir_version` is `1`. Emitters accept v1 only — the
frontend is the single producer, so there is no compatibility window.

## A1 — `await` step

```jsonc
{ "step": "await", "expr": <expr> }
```

Evaluate `expr`, await its result, discard the value. **This is an iteration
boundary** (paper §4.3.2): backends must lower it so the runtime may divert
here — everything accumulated before the boundary is revertible without
executing what follows. Only legal in component bodies (the checker rejects
it inside provide methods).

## A3 — identifier safety

The frontend renames any IR binding (`let-effect` binds, method params) that
collides with host-reserved names (Python keywords, common TS reserved words,
adapter names such as `ctx`/`config`/`frame`) by suffixing `_` until free.
Backends may assume every `name`/`bind` identifier is safe verbatim on both
hosts. Backends must not rename.

## A4 — `format` escaping

In `format.template`, `$$` denotes a literal dollar; `$<digits>` is a
placeholder. The frontend escapes literal dollars in source text; backends
must unescape `$$` **after** placeholder substitution is decided (i.e. split
on placeholders first, then replace `$$` with `$` in the text segments).

## A5 — `compensate` on `emit`

```jsonc
{ "step": "emit", "expr": <expr>, "compensate": <expr> }   // compensate optional
```

The emission executes as in v0. When `compensate` is present, backends push
it onto the component's accumulator exactly as an effect's undo (LIFO with
everything else). It is compensation, not inversion — the paper's §6.1
distinction — and the exactness guarantees (R1/R4 asserted state equality)
apply to it only up to the application's own equivalence; the demo/test
suites therefore assert that compensations *run in order*, not that state
is bit-recovered through them.

## A6 — typed services

```jsonc
"services": {
  "Database": {
    "methods": {
      "query":   { "params": [{"name": "sql", "type": "Str"}], "returns": "List[Row]", "emission": false },
      "execute": { "params": [{"name": "sql", "type": "Str"}], "returns": "Int",       "emission": true }
    }
  }
}
```

Type vocabulary is the surface one (`Str`, `Int`, `Bool`, `List[T]`,
`Opt[T]`, user names). Suggested host mappings: TS — `string`, `number`,
`boolean`, `T[]`, `T | undefined`, unknown names → `unknown`; Python —
annotations optional in v1. **Amendment to errata A6**: provide-method
entries *keep* their `params` list — those are the surface names that bind
the method body (they may differ from the service's declared names); what
emitters derive from the service is the *type* signature, not the names.

## A8 — mid-body failure semantics (contracted)

If an `acquire`, `await`, or `emit` expression raises during activation, the
backend must deliver the paper's L-Raise reading: effects accumulated so far
revert (LIFO), the component lands FAILED with the error recorded, and
sibling components are unaffected. Both v0 backends already inherit this
from their runtimes; v1 makes it a contract requirement with tests.

## Host builtins (additions)

- `Job.run(name) -> handle` — an **async** stub: resolves on a later tick and
  records `("run", name)` on a shared log; exists so `await` has something
  real to await in demos and tests.
