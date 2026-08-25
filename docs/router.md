# The stdlib Router, runtime routing (item 161)

**Status.** The **py-tier reference is built**: the stdlib Router component
(`stdlib/router.rvl`) plus runtime routing in `src/revl/run.py` route calls
round-robin across N worker realms and fail over when a worker withdraws, with
consumers seeing exactly one provider (G2). The per-tier *emitter* realization
landed on py/ts/rust (item 167); go/java/wasm are pending a runtime liveness
primitive (item 173), see §4. This note records the design, why the routing
lives in the driver rather than the emitted body, and what the other tiers need.

This is the runtime half of the load-balancer story; the *why* (G2 vs. load
balancing, the two non-goals, the comparison table) is docs/distribution-model.md
(item 163). Read that first for the model; this note is the mechanism.

---

## 1. What item 162 left, what item 161 adds

Item 162 landed the **frontend**: `isolate <key> in realms("w1"…"wN")
strategy(...)` parses, verifies each named realm has a provider, and records a
`routes` entry on the consumer's IR / manifest entry:

```
routes: { worker: { realms: ["w1", "w2", "w3"], strategy: "round_robin" } }
```

It did **not** route. Item 161 is the runtime: read that `routes` IR, resolve
the N per-realm provider handles, and forward each call to a live worker by the
strategy, with reactive failover.

The stdlib **Router** is the component that carries the bind. It
`requires <key> in realms(...)` (item 162 syntax) and `provides <key>` in the
parent realm. Downstream, every consumer of `<key>` resolves **one** provider,
the router, so G2 holds; the fan-out across the worker realms is the router's
private detail. `stdlib/router.rvl` is the canonical shape (three `PoolWorker`s,
one `RoundRobin` router). Realms are static string literals, so a router names
its worker realms and service concretely, it is a template to copy, not a
generic import.

---

## 2. How `run.py` resolves, selects, and fails over

The routing is realized by the py-tier driver (`_Driver` in `src/revl/run.py`),
built on three cordis facts already in the runtime, no new machinery:

1. **Per-realm resolution.** A provider isolated into `realm("w1")`
   (`isolate <key> in realm("w1")` → `runtime.plug` applies
   `ctx.isolate(key, realm_label("w1"))` before `ctx.plugin`) is reachable from
   any context as
   `root.isolate(key, realm_label("w1")).reflect.get(key)`, the same
   committed-view lookup a normal `requires` compiles to, scoped to that realm.
   The driver does this once per routed realm to get the N worker handles.
2. **Strict liveness = failover for free.** cordis's `reflect.get` returns
   `None` for a provider whose fiber is not `ACTIVE`. So the `_Router` proxy
   **re-resolves the live handle on every call** rather than caching it: a
   worker that withdraws (peer-death → provider-withdrawal → the R2/R3 reactive
   coeffect) resolves to `None` and drops out of the live set; its calls go to
   the survivors; a replacement that re-provides the key re-enters on its next
   turn. This is exactly the reactive model of docs/distribution-model.md §3,
   applied at the router's grain.
3. **One provision downstream = G2.** The driver installs the proxy as the
   provider of `<key>` in the parent realm via `root.reflect.provide(key, proxy)`,
   a real cordis effect whose disposer withdraws the key on teardown. So the
   router is the **sole** provider of the key in the parent realm (G2), and it
   leaves no residue (the provide-effect is withdrawn at the router's own LIFO
   position in `_dispose_all`).

**Strategies** (closed set, `lower.KNOWN_STRATEGIES`):

* `round_robin` (the default when `strategy` is omitted), rotate across the
  realms in declaration order, skipping any not currently live;
* `least_loaded`, route to the live realm the proxy has served fewest (a local
  served-count proxy for load; a real load signal is a follow-up, §4).

### Why the driver, not the emitted body

A router `requires <key>` (routed) and `provides <key>`. Its routed requirement
has **no single-realm provider**, the workers live in the *named* realms, not
the router's parent realm, so a normally-emitted router body would resolve
`_revl_ctx.<key>` to nothing and sit `PENDING` forever, never running its
`provide`. And the emitted `req` expression resolves exactly **one** handle in
**one** realm; there is no revl surface, and no emitter support today, to obtain
*indexed per-realm handles* from a multi-realm require. So in the py reference
the driver **realizes** a router (resolve N handles → install the proxy
provision) instead of plugging its emitted fiber. The router.rvl body documents
the forwarding intent; the driver is what fans out. This is the reference
boundary, not a hack, the language deliberately ships **no** multiplicity
construct (docs/distribution-model.md §4).

The driver also now plugs every component through the realm-aware `runtime.plug`
(applying `isolate` placements), so `revl run` honors named realms, a
prerequisite the routing needs and which aligns the standalone driver with the
emitted lifecycle-test driver. For a component with no placements this is
byte-identical to a bare `ctx.plugin`.

---

## 3. What the tests prove

`tests/test_router_runtime.py`:

* **Selection logic, no runtime** (a fake registry, every interpreter):
  round-robin rotates in declaration order and defaults correctly; a withdrawn
  realm is skipped and survivors serve; a replacement re-enters; all-withdrawn
  raises `NoLiveWorker`; `least_loaded` spreads by served-count; the proxy is
  passed through raw (not mis-wrapped as a traceable).
* **End-to-end on the cordis-py driver** (`@needs_cordis`): a real
  workers + Router + Consumer composition, calls distribute round-robin, a
  withdrawn worker is skipped while survivors serve, the consumer resolves
  exactly the router proxy (G2) before and after failover, and teardown proves
  no residue. Plus: `stdlib/router.rvl` itself boots and routes.

---

## 4. Follow-ups (STOP boundaries hit, precisely)

The deliverable is the **py-tier reference**. Two extensions are deliberately
out of scope for this item and are recorded here so they are not re-derived:

* **Per-tier emitter routing (`backends/*/emit.py`).** To make the *emitted*
  router body itself route (so `revl run --backend rust` and the other tiers fan
  out without the py driver), each emitter must, for a key carrying a `routes`
  entry, emit **per-realm handle resolution** (the N handles, one per named
  realm) plus the selection + strict-liveness skip, rather than a single
  `inject`/committed-view read. Concretely, the emitter lowers a routed require
  into: (a) N realm-scoped resolutions of the key, and (b) a strategy driver
  over them that re-checks liveness per call. The py runtime shows the shape
  (`_Router` in `src/revl/run.py`). **Item 167 landed this on py/ts/rust**;
  go/java/wasm still need a runtime liveness primitive the emitter cannot
  synthesize, tracked as item 173.

* **`least_loaded` with a real load signal.** The reference uses a local
  served-count. A faithful least-loaded wants each worker's *reported* load as a
  coeffect (docs/time-coeffect.md is the model for a harness-provided signal) so
  the router routes on actual backend pressure, not calls-issued. Small,
  self-contained follow-up on top of this reference.

Nothing here needed a new primitive: spawn, named/local realms, the reactive
coeffect, and per-realm committed-view resolution were all already in the
runtime, item 161 packages them.
