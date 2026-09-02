# The revl distribution model, load balancing is spawn + realms + router

**Status.** Framing/design record. The *pattern* described here is buildable
today from primitives that already ship (spawn, realms, reactive coeffects,
peer-death, TCP+mTLS), and the *stdlib components* that package it (the `Router`
and the multi-realm `require` syntax) have **shipped** (roadmap items 161 and
162). Emitted-body routing runs on py/ts/rust (item 167) and, via item 173, on
wasm (first-party) and go (through a stc-go fork); java has the emitter and
awaits an upstream cordis4j release. This document exists so that no
future contributor, reaching for the obvious-looking shortcut, quietly breaks
the guarantee that makes revl worth using.

> **The one sentence.** Load balancing and G2 (one key, one provider, one
> realm) are in *direct tension*, and the resolution is **spawn + realms +
> router**, N workers each in their own realm behind one router that provides
> a single key, **not** N providers of one key in one realm.

---

## 1. The tension

G2 is provision disjointness (DESIGN.md §4, guarantee table; Def. 43): within
a composition, a key has **at most one provider**. This is not a stylistic
rule, it is what makes a composition *verifiable*. Because `db` resolves to
exactly one provider, the compiler can reason, statically, about which provider
every consumer of `db` gets: the link phase builds a whole-composition manifest
and G2/G3 are checked over it (DESIGN.md: "the language has a linker phase
precisely so G2/G3 are static even though loading is dynamic"). Every downstream
guarantee, G3 acyclicity, G4 inverse-or-emit, G7 LIFO teardown, is stated
against *the* provider a consumer resolves to. Ambiguate that resolution and the
guarantees lose their subject.

Now the naive load-balancing instinct: "I want three database workers sharing
the traffic, so I'll register three providers of `db`." That is precisely two
(or three) providers of one key in one realm, the exact shape G2 refuses. And
it *should* refuse it: with three providers of `db`, "which provider does this
consumer get?" no longer has an answer the compiler can give, and the manifest
stops being a thing that type-checks as a whole. You would have traded a
verified composition for an unverified distributed system and called it a
feature.

So the constraint is real, and it bites exactly where load balancing wants to
live. The rest of this document is why the constraint **is** the feature.

---

## 2. The resolution: spawn + realms + router

G2 is not global, it is **per-(key, realm)**. Two providers of `db` in
*different* realms is not a conflict; it is the multi-tenancy feature
(docs/design-v2-realms.md, "Guarantees under realms": *"two providers of `db` in
different realms is the feature; the same realm is the conflict, and the error
names the realm"*). `examples/tenants.rvl` compiles precisely because its two
providers of `kv` sit in `realm("tenant_a")` and `realm("tenant_b")`;
`v2_same_realm_conflict.rvl` must not compile. The non-conflict is as
load-bearing as the conflict.

Load balancing is that fact applied on purpose:

- **N workers, N realms.** Each worker is `spawn`ed into its **own local
  realm**. G2 holds *per realm*, one provider of `db` in each, so there is no
  conflict, by construction (docs/design-v2-instances.md: a spawned instance's
  realm is `LocalRealm(entry)`, "identity is the entry, unique per instance";
  "two instances in local realms cannot collide by construction"). Or, for
  explicit sharding, N **named** realms (`realm("w1")…realm("wN")`).
- **One router.** A `Router` component `requires db in realm("w1")…realm("wN")`
  (the multi-realm require, item 162) and **`provides db`** in the *parent*
  realm.
- **One provider, downstream.** Every consumer of `db` in the parent realm sees
  exactly one provider, the router. **G2 holds.** The consumer cannot even tell
  it is talking to a pool; the fan-out is the router's private implementation
  detail. The router picks a worker per call (round-robin, least-loaded, its
  choice) and forwards.

### Architecture

```
                    parent realm
                 ┌───────────────────┐
   consumers ───►│   provides db     │        one provider of db  (G2 holds)
                 │      Router        │
                 └─────────┬─────────┘
        requires db in realm("w1") … realm("wN")
          ┌───────────┬───────────┬───────────┐
          ▼           ▼           ▼           ▼
      realm w1    realm w2    realm w3   …  realm wN
     ┌────────┐  ┌────────┐  ┌────────┐   ┌────────┐
     │ Worker │  │ Worker │  │ Worker │   │ Worker │   one provider of db
     │provides│  │provides│  │provides│   │provides│   PER realm (G2 holds
     │  db    │  │  db    │  │  db    │   │  db    │   per-(key,realm))
     └────────┘  └────────┘  └────────┘   └────────┘
      spawned     spawned     spawned       spawned
      (possibly on different hosts / tiers, via TCP+mTLS)
```

Nothing here relaxes G2. It is invoked once per boundary: **N times inside** (one
provider per worker realm) and **once outside** (the router is the sole provider
in the parent realm). The pool is real; the ambiguity is not.

---

## 3. Every primitive already exists

The pattern invents no new runtime machinery. Each piece below is verified
against the repo, including the two stdlib pieces (items 161/162) that have
since shipped.

| Piece | What it gives the pattern | Status | Where |
|---|---|---|---|
| **`spawn` (instances)** | N live copies of one worker component; 4 spawn properties, distinct local realms, independent LIFO teardown, request-scoped anti-leak, supervision-tree addressing | **Built, all 6 tiers** (cordis-py, ts, rust, go, java, wasm) | roadmap item 10 (v2.0-roadmap.md); docs/design-v2-instances.md |
| **Local realms (per-spawn isolation)** | each worker's `db` lives in its own realm → G2 holds per worker with zero labels | **Built** (identity-keyed `LocalRealm`; wasm via the B3 realm-prefix change) | docs/design-v2-instances.md |
| **Named realms (sharding)** | explicit `realm("w1")…` when you want addressable shards, not anonymous instances | **Built** (static string labels; dynamic labels rejected by design) | docs/design-v2-realms.md; `examples/tenants.rvl` |
| **Reactive coeffects (failover)** | R2: a component activates only when every `requires` key is provided, **deactivates when one is withdrawn**, reactivates against a replacement. A dead worker's coeffect suspends *its* route; survivors keep serving; a replacement re-activates it | **Built** (R2/R3, demonstrated live) | docs/backend-ir.md §R2/R3; docs/interop-bridge.md |
| **Peer-death monitor** | a dead cross-process/cross-machine worker becomes a **provider withdrawal**, the mechanism that *drives* the R2/R3 failover above | **Built** | docs/interop-bridge.md; docs/network-placement.md |
| **TCP + mTLS** | workers on other machines / other tiers; both ends present a certificate | **Built**, with a scope note: the mTLS **listener** currently ships on the **py** runner; **consumers** dial from rust/go/java | docs/network-path.md; docs/network-placement.md |
| **Capability attenuation on spawn** | a spawned worker's capability set is a *checked subset* of the parent's, a pool worker cannot hold more authority than the router handed it | **Built** | roadmap item 66 (v2.0-roadmap.md) |
| **Router lifecycle verification** | the router is a component; its own acquire/route/dispose is checked by **G7** (LIFO-complete derived teardown) and **A8** (mid-body acquire-failure reverts accumulated effects, lands FAILED, siblings unaffected) | **Built** (the guarantees, and the `Router` that uses them, item 161) | DESIGN.md §4 (G7); docs/backend-ir-v1.md §A8 |
| **stdlib `Router` / `RoundRobin`** | the packaged component: `spawn_pool(n, factory)` with `undo dispose_pool`, provides the key by routing | **Built (item 161)**; emitted-body routing on py/ts/rust (item 167) and wasm + go (item 173); java emitter routes, runtime pending upstream cordis4j | v2.0-roadmap.md item 161 |
| **Multi-realm `require` syntax** | `requires db in realms("w1","w2","w3") strategy(round_robin)`, bind all N realms at once | **Built (item 162)** (frontend + IR; the Router consumes it) | v2.0-roadmap.md item 162 |

The honest summary: the *model* is fully expressible from shipped primitives,
and the *ergonomics* (a one-line `requires db via RoundRobin[Database](3, PgDatabase)`)
shipped as items 161/162. None of it needed new runtime semantics: item 162
notes "the runtime multi-bind is the reactive coeffect model already in place."

---

## 4. The two non-goals (the crux)

These are the shortcuts that look reasonable and must never be taken. They are
recorded here so a future proposal to add either can be answered with "see
distribution-model.md §4," not re-litigated.

### Non-goal A, NO language-level multiplicity

There will be **no** `provide db with multiplicity(N)` construct, no language
feature that lets N providers answer to one key in one realm.

The moment two providers of `db` coexist in one realm, "which provider does this
consumer resolve to?" has no static answer, the composition manifest stops
type-checking as a whole, and every guarantee stated against *the* provider of a
key loses its referent. A `multiplicity(N)` keyword would not be a convenient
load-balancing sugar; it would be the single edit that converts a **verified
composition language into an unverified distributed system**. The router pattern
is not one option among several, it is the required shape, precisely because it
keeps the one-provider invariant while still fanning out.

(Roadmap item 161 states this as an explicit NON-GOAL of the stdlib work: *"a
language-level `provide db with multiplicity(3)` … breaks G2 and destroys
verification; the router pattern is the required shape."*)

### Non-goal B, revl ships NO built-in load-balancing infrastructure

There is **no** bundled Envoy, Nginx, HAProxy, or service-mesh sidecar, and
there never will be. Load balancing in revl is not an appliance you configure;
it is a **component you compose**:

- the **router** is an ordinary revl component (the stdlib `Router`, item 161);
- the **algorithm**, round-robin, least-loaded, weighted, is the router's
  *implementation detail*, not a language surface;
- the **transport** to remote workers is **TCP + mTLS** (item 56's network
  seam), not a proprietary data plane;
- **placement** decides *which host* a worker lands on, the spawn/placement
  layer, not the router, owns topology.

revl's contribution is not another load balancer. It is that *this* load
balancer's own lifecycle is compile-time verified.

---

## 5. Comparison, why this shape and not the others

| Approach | G2 | Router / LB lifecycle | Failover | Net |
|---|---|---|---|---|
| **Naive: N providers of one key in one realm** | **Broken**, provider resolution is ambiguous; the manifest no longer type-checks as a whole | n/a | undefined | **Unverifiable.** Trades the whole point of the language for a fan-out. |
| **spawn + realms + router** (this doc) | **Holds**, one provider per worker realm (per-(key,realm)); one provider in the parent realm (the router) | **Verified**, the router is a component; its acquire/route/dispose is checked by **G7 / A8** | **Reactive**, peer-death → withdrawal → R2/R3 suspends the dead route, survivors continue, replacement re-activates | **Verified pool.** The load balancer provably cannot orphan a worker. |
| **External LB** (Envoy/Nginx in front) | **Holds**, revl never sees the fan-out | **Outside verification**, the LB is not a revl component; its lifecycle is opaque to the gate | whatever the external LB does | **Works, but the LB is a blind spot.** Exactly the seam revl exists to close. |

The middle row is the only one where both columns are green: G2 intact **and**
the balancer itself inside the verified boundary.

---

## 6. The distribution model, one table

Each distributed-systems concern maps to a revl primitive, no concern needs a
new subsystem.

| Distributed concern | revl primitive | Status |
|---|---|---|
| **Instances / horizontal copies** | `spawn` (all 6 tiers) | Built |
| **Sharding** | named realms (`realm("shard_k")`) | Built |
| **Load balancing** | router + spawned pool (one provider per worker realm; router provides the key in the parent realm) | Built (stdlib `Router`, item 161; per-tier emitted routing py/ts/rust/wasm/go via item 173; java emitter routes, runtime pending upstream) |
| **Failover** | reactive coeffects (R2/R3), a withdrawn worker's route suspends, survivors continue | Built |
| **Remote / cross-machine** | TCP + mTLS network seam (listener on py; consumers on rust/go/java) | Built (with the listener-tier scope note) |
| **Circuit-breaking** | the quarantine tier, a suspect candidate proves itself in the wasm sandbox before it is admitted | Built (v1: `revl_quarantine`, swap admission hook) |
| **Registry / multi-resolution** | `revl_resolve`, find an admission-compatible provider instead of hand-wiring | Built (`revl_resolve` MCP verb + CLI) |

### Emitted-body routing: per-tier status (item 173)

Multi-realm routing works as a normal emitted component on **five of the six
tiers**, and the sixth (java) has the emitter but awaits an upstream runtime
release. Item 161 realized routing in the py driver; item 167 landed the
emitted-body form on **py, ts, and rust**; item 173 added the runtime liveness
primitive each remaining tier needed:

- **wasm — routes first-party.** wasm is revl's own runtime, so the primitive
  was built directly into the substrate (`cordis-wasm`'s `route:<key>` host op:
  a strict, single-realm, liveness-checked read plus a `live` probe, no
  parent-chain fallback). The emitter lowers a routed require into a selector +
  strict dispatch that consume it. Round-robin, failover, re-entry and G2 are
  proven by running on the real Python + wasmtime host
  (`backends/wasm/test_router_exec_wasm.py`).
- **go — routes via an emitted router struct + upstream fork.** stc-go's plain
  resolve walks the realm chain to the root (where the Router provides the key,
  G2), so a withdrawn worker's read fell back to the Router instead of dropping
  out. The fix is `ServiceInRealm` — a strict single-realm liveness-checked read
  with no parent fallback — added in a fork of stc-go (`forks/stc-go`, PR spec
  in its `REVL-FORK.md`). The emitter lowers a routed require into a
  `revlRouter<Comp><Key>` struct that re-resolves live per-realm handles every
  call. Built and passing at runtime against the fork with go1.26.5
  (`backends/go/test_router_exec_go.py`); the upstream PR is pending, after which
  the pin drops the local `replace`.
- **java — emitter routes; runtime primitive pending upstream.** The cordis4j
  emitter lowers a routed require into a `RevlRouter<Comp><Key>` class that
  consumes `ctx.serviceInRealm(...)` (the same strict, no-fallback read). The
  primitive is delivered as a cordis4j fork PR spec plus a realm-aware reference
  implementation in the in-repo stub (`forks/cordis4j/REVL-FORK.md`). It could
  not be built or run here — this environment has no Java Runtime — so java is
  the one tier whose routing awaits the upstream cordis4j release + a JRE.

Every routes-less program on all three touched tiers emits byte-identically
(the routing paths are gated strictly on a non-empty `routes`), verified by the
tier golden oracles. The routing model itself is unchanged; see `docs/router.md`
for the shape.

---

## 7. The payoff

Every service mesh gives you a load balancer whose *own* correctness you take on
faith: does the sidecar leak connections, does it drain workers in order, does a
half-started backend get traffic? You find out in production.

revl's router is a component. Its acquire/route/dispose lifecycle is checked by
the same gate that checks everything else, **G7** proves its teardown is
LIFO-complete over every effect it accumulated, **A8** proves that a worker that
fails mid-acquire reverts cleanly and lands FAILED without taking its siblings
down, and capability attenuation proves each worker holds no more authority than
the router handed it. The failover is not bolted on; it is the reactive coeffect
model the runtime already runs.

**A load balancer whose own lifecycle is compile-time verified, something no
service mesh offers.** That is the whole reason the G2 constraint was worth
keeping: honored, not escaped, it hands you a verified pool for free.
