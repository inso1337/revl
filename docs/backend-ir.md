# revl backend IR (v0 contract)

> **Historical contract.** This file froze the *v0* shape so the frontend
> and backends could be built in parallel. No emitter produces
> `ir_version: 0` any more — the shipping contract is `ir_version` 1/2/3
> (docs/backend-ir-v1.md for the v1 deltas, docs/design-v2-realms.md for
> 2, docs/syntax-2.0.md §9 for 3). Kept for provenance; read
> docs/backend-ir-v1.md for what emitters actually accept.

The frontend (parser → checker → linker) lowers each component to a JSON
document; a backend turns that document into a component for its host runtime.
This file freezes the v0 contract so frontend and backends can be built in
parallel. `examples/user_cache.ir.json` is the reference instance — it is the
hand-lowered form of `examples/user_cache.rvl` and MUST be accepted verbatim.

A backend consists of:

1. an **emitter** (a Python module, so the compiler stays one language):
   `emit(ir: dict) -> str` producing a host-language source file;
2. a **runtime adapter**: whatever glue the emitted file needs to register
   with the host runtime (cordis-py / cordis) such that the semantics below
   hold;
3. a **demo + tests** proving the semantics (see §Acceptance).

## Document shape

```jsonc
{
  "ir_version": 0,
  "services": {
    "<ServiceName>": {
      "methods": {
        "<method>": { "params": ["<name>", ...], "emission": false }
      }
    }
  },
  "components": [
    {
      "name": "<ComponentName>",
      "config": [ { "name": "url", "type": "Str", "default": null } ],
      "requires": { "<local>": "<ServiceName>" },   // local → service
      "provides": { "<key>": "<ServiceName>" },
      "body": [ <step>... ]
    }
  ]
}
```

## Steps

| step | fields | semantics |
|---|---|---|
| `let-effect` | `bind`, `acquire`, `undo` | evaluate `acquire`, bind result to `bind` for later steps; push `undo` (which may reference `bind`) onto the accumulator |
| `effect` | `acquire`, `undo` | as above, no binding |
| `emit` | `expr` | evaluate for its externalized effect; nothing accumulated |
| `provide` | `name`, `service`, `methods` | install the provision under key `name`; the withdrawal inverse is derived by the backend, not present in the IR |
| `return` | `expr` | only inside provide-method bodies |

`methods` items: `{ "name", "params": [...], "body": [ <step>... ] }` — a body
is steps; a trailing `return` yields the method's value. Steps inside a method
body run while the component is ACTIVE, and any `effect` steps there join the
component's accumulator (coeffect operations are effects).

## Expressions

| kind | fields | meaning |
|---|---|---|
| `lit` | `value` | literal (string/int/bool/null) |
| `name` | `id` | local binding (from `let-effect` or a method param) |
| `config` | `field` | config field access |
| `req` | `name` | a required capability (service instance) |
| `call` | `target`, `method`, `args` | method call on an expression |
| `host` | `fn`, `args` | host-runtime builtin (see Host builtins) |
| `format` | `template`, `args` | string interpolation; `$0`, `$1`… placeholders |

## Host builtins (v0 stub stdlib)

Backends implement these minimally; they exist so the demo runs without FFI:

- `Pool.open(url, size) -> pool` with `pool.close()`, `pool.query(sql)`,
  `pool.execute(sql)` — an in-memory fake recording queries.
- `Map.new() -> map` with `map.drop()`, `map.get(k)`, `map.insert(k, v)`,
  `map.remove(k)`.

## Required semantics (what the adapter must guarantee)

These come from DESIGN.md §3–4 and are the point of the exercise:

- **R1 — LIFO recovery.** Unloading a component runs accumulated undos in
  reverse order of their effects, including effects accumulated by provide-
  method calls while active.
- **R2 — reactive resolution.** A component activates only when every
  `requires` key is provided, deactivates when one is withdrawn, and
  reactivates against a replacement provider.
- **R3 — withdrawal ordering.** When a provider is withdrawn, dependents
  fully deactivate first, and a dependent can still call its required
  services during its own teardown (`undo` expressions may use `req`).
- **R4 — no-residue.** After unloading everything, the host runtime holds no
  bindings, listeners, or effects from the composition (assert via the
  runtime's introspection).
- **R5 — provision withdrawal is derived.** The emitted code never hand-rolls
  teardown for provisions; it uses the runtime's revertible provide/set.

## Acceptance

A backend is done when, against `examples/user_cache.ir.json`:

1. the emitter produces an idiomatic host source file (checked into the
   backend dir as a golden file);
2. a demo script loads PgDatabase + UserCache, exercises `cache.put/get`,
   swaps the Database provider at runtime (R2, R3 observed via an event log),
   then unloads everything (R1, R4 asserted);
3. tests cover R1–R5, runnable from the backend dir with one command
   documented in its README;
4. `REPORT.md` records: impedance mismatches with the host runtime, anything
   the IR could not express cleanly, LOC, and a recommendation for which
   backend v0 should ship first.

Do not modify this file or the sample IR; if the contract is wrong, note it in
REPORT.md — the frontend integration pass will arbitrate.
