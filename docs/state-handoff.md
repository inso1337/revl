# Verified state hand-off on hot-swap (roadmap item 53 — the `code_change` gap)

`revl swap` (item 23) drains a provider's in-flight calls, re-points its
consumers, then tears the old provider down **LIFO** and boots the successor.
For a **stateless** provider that is exactly right. For a **stateful** one —
one whose effect-created world *is* its value: a cache's `Map`, a session
store's entries, a rate limiter's counters — teardown drops that world and the
successor starts **cold**. The swap silently becomes "restart with extra
steps", and the data is gone with no diagnostic.

Erlang solved this thirty years ago with `code_change`: on a hot code upgrade
the old process hands its state to the new one, and the new module decides
whether it can accept the old shape. Item 53 brings the **checked** version of
that to revl.

## The `handoff` declaration

A component may name **one** `handoff`, a prelude declaration targeting a key
it provides:

```
component Cache provides cache: Store {
  handoff cache: Map[Str, Str]           // <- the state contract
  let m = effect Map.new() undo m.drop() // <- the state itself
  provide cache {
    fn get(k)    = m.get(k)
    fn put(k, v) { effect m.insert(k, v) undo m.remove(k) }
  }
}
```

`handoff cache: Map[Str, Str]` declares the **shape** of the provider's live
state. It reads both ways, and that is the whole point:

* when this component is **replaced**, that type is the value it **exports**;
* when it is the **replacement**, that type is the shape it **accepts**.

It is a *prelude* declaration — it must precede every effect/emit/await/provide,
because it names what the state *is* before any effect creates it — and it
targets a key the component **provides** (its state crosses to whoever
re-provides that key). A component activates one frame and so holds one
resource vector; it therefore declares **at most one** hand-off, whose type
describes that whole vector (thread several pieces of state through one record
or `Map`).

The field is **additive**: a component with no `handoff` lowers byte-identically
to before, and `ir_version` stays **3**. In the IR it is
`component["handoff"] = {"key": <key>, "type": <type>}`.

## The gate: the §5 relation, pointed at state

The novelty is not the syntax — it is that the hand-off is **verified at
admission**, reusing the very compatibility machinery the interface-drift check
rests on (`typecheck.compatible`, `admission._service_compatible`,
`plan._interface_drift`; see `docs/service-compat.md`), pointed at **state**
instead of **interface**.

The predecessor **exports** a value of its declared type `E`; the successor
**accepts** it at its declared type `A`. The value flows old → new, so — exactly
the covariant value-flow the §5 relation already uses at a consumer's *return*
position — `A` must accept everything an `E` produces:

```
compatible(expected = A, actual = E)      # admission._handoff_compatible
```

When the running provider and the candidate both declare a hand-off on the same
provided key and the two shapes **disagree**, the swap is **refused at
admission** — the same all-or-nothing `RevlError`-with-why-trace every other
admission refusal carries (`why.kind == "state-handoff-drift"`), naming the key
and both shapes:

```
state hand-off on `cache` differs from the running manifest: `CacheV2` accepts
`Map[Str, Int]`, but `Cache` exports `Map[Str, Str]` — the successor cannot hold
the predecessor's state, and dropping it on the swap would be residue
```

Refusing is the sound move: silently dropping the predecessor's state on a swap
would be **residue**, which G7/the erase-report forbid.

### What is *not* a conflict

* **A successor that declares no hand-off** opts out of inheriting the state.
  A stateless successor of a stateful provider is a valid (if lossy) choice the
  author made explicit — no refusal, it just starts cold.
* **A key the successor accepts but nothing running exported** — the running
  provider was stateless, or pre-item-53. Nothing to be incompatible with; the
  successor starts cold.
* **A widened acceptor** — the predecessor exports `Str`, the successor accepts
  `Opt[Str]` — is admitted: the §5 relation already models the `T -> Opt[T]`
  injection, so the accepted type admits everything the exported type produces.

## Threading the value (the warm start)

On a **verified** swap the exported value is threaded from old to new through
`mcp/session.py`'s swap path (`Session.swap`), so the successor starts **warm**:

1. **Capture, before teardown.** While the old provider is still live and its
   world still exists, `_capture_provider_state` snapshots each hand-off
   provider's activation-frame resource vector — the same `frame._resources`
   the live-instance migration (item 10) reads, but for a *composition-level*
   provider fiber. Keyed by **provided key**, not component name, so a renamed
   successor (`Cache` → `CacheV2`) that re-provides `cache` is correlated by
   *what it provides*.
2. **Restore, after load.** `_restore_provider_state` re-seats each captured
   vector onto the successor's provider fiber through the resources'
   `__revl_restore__` protocol — **check the whole cohort, then apply**, so a
   single incompatible provider rejects with nothing half-written.
3. **Report.** The swap result carries a `handoff` block naming, per key, the
   successor component and the count of resources actually carried across.

Admission has already proved the *declared* shapes compatible; the resource-
vector check at step 2 is defence in depth for a provider whose runtime shape
diverges from its declaration. Either way, a rejected hand-off **rolls the whole
swap back** — the predecessor is reloaded and its captured state re-seated — so
the running composition is left exactly as if the swap had never been attempted.

## Files

* grammar + lowering — `src/revl/parser.py` (`HandoffStmt`), `src/revl/lower.py`
  (`_lower_component`, additive `handoff` IR field), `src/revl/lexer.py` /
  `src/revl/formatter.py` / `selfhost/lexer.rvl` (the `handoff` keyword);
* the gate — `src/revl/admission.py` (`_handoff_compatible`,
  `_admit_handoff_replacement`, `_handoff_error`), wired in
  `src/revl/lower.py::check_and_lower`; the running provider's exported shape is
  threaded into the ambient by `src/revl/compiler.py::_running_handoffs`;
* the warm-start threading — `src/revl/mcp/session.py` (`_capture_provider_state`,
  `_restore_provider_state`, and the extended `_abort_swap`);
* tests — `tests/test_state_handoff.py` (grammar, lowering, the gate; no runtime)
  and `tests/test_state_handoff_exec.py` (the warm start and the rollback, on
  cordis-py).
