# Findings — service-level canonical ABI (harness milestone 35, finding #28)

Probe: the service-level slice of item 41 (`provide` methods cross the
WASI-P2 canonical boundary; `wasm/service.rvl` + extended
`tools/canonical_demo.py` in revl-harness). Verified green — the harness's
`Msg` wire record, `List[Msg]`, and `Opt` all round-trip under wasmtime's
component model. One cross-tier inconsistency surfaced along the way.

## Verified green (pinned by the harness)

```revl
type Msg = { role: Str, content: Str }
component LedgerImpl provides ledger: Ledger { … }
```

- `make("user","hello")` → `{role: "user", content: "hello"}`;
- `role-of`/`with-content` record surface round-trips;
- `transcript("assistant")` → `[{role: "assistant", content: "line 1"}, …]`
  (List[Msg] — the session wire surface);
- `maybe(9)`/`maybe(-1)` → `some(9)`/`none` (Opt);
- the canonical wrappers name each provide method (`$__prov_ledger_make`)
  and export `revl:exported/ledger#make`; wasm-tools validate clean;
  wasmtime's component model runs all of it.

The canonical boundary carries the service's *pure* methods; the emission
side of the harness stays off the interface (WIT describes shape, not
effects) — as designed.

## The finding: let/var annotations drop in the IR; wasm can't type `[]`

The first version of the demo built a session list with a loop:

```revl
fn lines(role: Str, n: Int) -> List[Msg] {
  var out: List[Msg] = []
  ...
}
```

The checker accepts the annotation, and py/ts lower it (they tolerate `[]`
and type the accumulator from the later `push`). But the IR `let`/`var`
step carries **no `type` field** — `{"step":"let","name":"out","value":
{"kind":"list","items":[]},"mutable":true}` — and the wasm emitter infers
the initializer with no expected type, so it refuses:
`EmitError: an untyped empty list literal needs an expected List type`.

**Ask (roadmap item 99 on agent/harness-m3):** carry the declared type on
the `let`/`var` IR step and use it as the expected type for the initializer
in every emitter (the other tiers could then also check the initializer
against the annotation). Repro: `wasm/service.rvl` with `var out:
List[Msg] = []` — py/ts pass, wasm REFUSED. The demo ships with a typed
element list literal.
