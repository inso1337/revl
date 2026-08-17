# revl interop bridge — cross-host services without shared memory

**Status:** proposal (2026-08-16) · companion to docs/syntax-2.0.md · §4
(distributability audit) implemented in `revl audit`; the transport tier (§3)
is not yet built

A translation layer between the host languages (TypeScript and Python) was
raised as a follow-up to the [2.0 syntax proposal](syntax-2.0.md): since a
Node process and a Python process cannot share memory, can revl have the
equivalent of Android's AIDL — generated proxies and stubs that make a
cross-process call feel local? And does doing so require any change to the
v2 syntax?

The answer, in one line: **full *interface* interop is buildable and largely
already designed; full *semantic* transparency is impossible in principle
for everyone, forever — and revl narrows the gap further than AIDL ever
could, without adding a single grammar rule.**

---

## 1. What a translation layer can and cannot do

AIDL does not make processes share memory. It makes *not* sharing memory feel
like a method call. The recipe is always the same, whatever the stack calls
itself — AIDL, gRPC, COM, Thrift, Cap'n Proto:

```
interface definition ──▶ generated proxy (caller) + stub (callee)
                                   │
                          serialization (Parcel / protobuf / FlatBuffers)
                                   │
                              a transport (Binder / HTTP/2 / shared pipe)
```

The physics is settled — Waldo, Wyant, Wollrath & Kendall, "A Note on
Distributed Computing" (1994) — and no translation layer ever built, AIDL
included, escapes it. Local and remote calls can be made *syntactically*
identical but never *semantically* identical. Four things leak through every
boundary:

1. **Reference semantics.** You cannot hand Python a live JS object. Every
   value that crosses is either a *copy* (aliasing and mutation stop
   propagating) or a *proxy* (every touch crosses the wire again). AIDL's
   Parcelable-vs-Binder-object is exactly this fork; there is no third option.
2. **Latency.** Local ~ns, IPC ~µs, network ~ms — three to six orders of
   magnitude. Invisible for coarse calls, fatal for chatty ones (an inner
   loop reading array elements through a proxy).
3. **Partial failure.** A local call cannot fail *in transit*; a remote one
   can, in ways no interface can express away. The paper says it in our own
   vocabulary: cross-process services must be designed against an
   *asynchronous contract*.
4. **Identity.** `a == b` across a boundary, object graphs with cycles, GC of
   remote handles — all degrade.

So: **100% interface interoperability — yes. 100% semantic transparency —
impossible in principle.** Anyone who claims otherwise is hiding one of the
four leaks.

## 2. What revl already owns

The reason IDLs feel bolted-on in ordinary languages is that code secretly
violates the discipline they need — globals, aliased mutable objects,
identity tricks. revl outlaws all of that already. Concretely, every
ingredient of the AIDL recipe *except the transport* is present in the
codebase today:

| Bridge ingredient | Where it already is in revl |
|---|---|
| Interface definition (the "IDL") | `service` declarations, DESIGN.md §3.1 — typed, host-neutral, carried in the IR (`services` block, A6) |
| Wire-friendly data | value semantics by construction: records/ADTs, `Opt[T]` not `null`, no object identity, no cycles — serializable without a marshalling spec |
| Proxy & stub generation | the emitters already generate per-host provide-implementations and committed-view access (DESIGN.md §7); a proxy is the same codegen turned one degree |
| Wiring / composition | the linker manifest (`loadOrder`, G2/G3) and the runtime-admission gate (`compile_files(manifest=running)`, DESIGN.md §4) |
| Conformance checking | the checker — a service's declared signature is enforced, not hoped for |
| Failure model | withdrawal, divert, L-Raise (R2/R3/A8). A remote peer dying is a provider withdrawal — see §3 |
| Boundary audit | `revl audit` (G8) — the home for distributability, see §4 |

The one genuinely missing piece is a **transport** — and the design doc
already blessed it: the paper's §6.2 service broker does "cross-process
invocation … mediated by an RPC mechanism that preserves the interface,
making the distribution transparent to consumers," and DESIGN.md §10 lists
the broker as the first open question. The bridge tier is the answer to that
open question, and it is a *backend + manifest + audit* matter — not a syntax
one.

## 3. The bridge tier

**Status:** implemented over a Unix socket, verified against the real runtimes:

- **py↔py** (`backends/python/bridge.py`, `demo/bridge_pypy.py`): PgDatabase and
  UserCache from one `.rvl`, split across two Python processes.
- **py↔node** (`backends/typescript/bridge.ts`, `backends/typescript/bridge_node.ts`):
  the headline seam. PgDatabase in Python, UserCache on cordis-ts in Node, one
  service contract, JSON on the wire.
- **py↔rust** (`backends/rust/bridge_client/`, `demo/bridge_pyrust.py`): a Rust
  process consuming a Python-provided service over the wire, values marshalled
  back into typed Rust. cordis-rs services are static traits
  (`Arc<dyn Database>`), so — unlike the dynamic py/node proxies — a Rust proxy
  that lets a cordis-rs *component* consume the seam has to be
  emitter-generated per service. **That codegen has since landed** (`e349487`,
  §"placement backends" below): `backends/rust/emit.py` generates the proxy,
  stub dispatcher and plugin table per composition, so Rust now both consumes
  and serves across a seam.

Both show value-copy marshalling and peer-death-as-withdrawal (R2/R3): killing
the provider deactivates the consumer reactively and replays its inverses LIFO,
no exception. The crossed method (Database.execute) is a *synchronous* emission,
which §4 flags as address-space-bound; the py↔node proxy pays that literally
(a blocking round-trip per call), whereas an `async fn` service would proxy
without blocking.

Placement wiring is implemented: `revl run app.rvl --placement p.toml` splits
the components across processes and wires each seam straight from the manifest
(`src/revl/placement.py`), the manifest-driven form of demo/bridge_pypy.py.
`--once` brings the composition up, runs per-process probes (which may cross a
seam), and tears down. Each process declares its `backend`:

- `py` (default) runs on cordis-py via `src/revl/_process_runner.py`;
- `node` runs on cordis-ts via `backends/typescript/placement_runner.ts`;
- `rust` runs on cordis-rs via `backends/rust/placement_runner` (the conductor
  `cargo build`s it);
- `java` runs on cordis4j via `backends/java/placement/PlacementRunner` (the
  conductor emits + `javac`s the module).

All four are verified against a Python provider with `--once`, the emit
crossing into the provider's pool and `cache.get` returning the value:
`examples/placement/user_cache{,_pynode,_nodepy,_pyrust,_pyjava}.toml`.

Participation levels differ, and the docs are honest about it:
- `py`/`node` are full, reactive participants: consume via a dynamic proxy,
  serve via the stub, and peer death is withdrawal (R2/R3).
- `java` runs on the real reactive cordis4j when a JDK 21 and a compiled
  cordis4j (`REVL_CORDIS4J_CLASSES`, or the cached checkout the verifier builds)
  are present: `RealPlacementRunner` consumes via a generic
  `java.lang.reflect.Proxy` (any interface, no codegen) and a monitor connection
  turns provider death into reactive withdrawal (verified: the consumer unloads,
  no exception). Without JDK 21 + cordis4j it falls back to the non-reactive
  in-repo stub (`PlacementRunner`, JDK 17), which still crosses and tears down.
- `rust` is emitter-generated (`backends/rust/emit.py`): per service a
  `<Svc>Proxy` and a stub dispatcher plus a `plugin_by_name` table, so the
  runner carries no composition-specific code, consumes and serves any seam,
  and the conductor regenerates `components.rs` from the running IR before
  `cargo build`. It is now a full reactive participant: a peer-death monitor
  disposes the proxy on provider EOF, so a dead provider deactivates the rust
  consumer reactively (`Active -> Pending`, verified), like py/node/java. Its
  proxy/stub marshal scalars, `Opt`, `List`, `Map`, records, `Result`, and user
  ADTs via serde. Records/`Map`/`List`/`Opt`/scalars are plain JSON and so
  cross-language-consistent; `Result` and ADTs use serde's externally-tagged
  form, which round-trips rust<->rust but would need the other emitters aligned
  to the same tagging to cross languages. The only unmarshalled type is the
  opaque host `Value`.

A third backend target (alongside cordis-py and cordis-wasm) whose job is to
emit, for each cross-process seam, a **proxy** on the consumer side and a
**stub** on the provider side, over a **transport-agnostic** channel. The
same generated code works over in-process calls, local IPC, or the network;
which transport a seam uses is manifest data, not source text. One
composition, one `.rvl`, proxies generated per seam:

```
requires db: Database ──▶ generated proxy ──▶ transport ──▶ generated stub ──▶ PgDatabase
     (Node process)          (committed-view            (pipe/socket)           (Python process)
                              access, unchanged)
```

Three decisions make it honest:

- **Value types cross by copy; resource types cross by handle.** revl's
  value types (records, ADTs, `Str`/`Int`/`Bool`/`Bytes`, `List`, `Map`,
  `Opt`, `Result`) copy cleanly by construction. The one thing that cannot
  copy — a capability like `Socket`, a pool, anything an `extern acquire`
  returns — is what WIT calls a *resource*: proxied by handle, with lifetime
  tied to the providing fiber. This is leak 1, and revl's type system has
  already decided which side of the fork every type is on; nothing new needs
  inventing to name it.
- **Immutable bulk data genuinely shares.** Mutable object graphs can never
  be shared across languages, but immutable value buffers can — Apache Arrow
  is exactly this (one in-memory layout, zero-copy reads from every
  language). Because revl values are immutable, `Bytes` and `List[Int]`
  crossing a same-machine bridge can be Arrow-style shared buffers rather
  than copies. Value semantics pays a second time.
- **Peer death is withdrawal.** This is where revl beats every RPC framework
  shipped. Binder needed death-recipient plumbing bolted on; gRPC needs
  keepalives and retry budgets. In revl, a remote peer dying is just a
  provider *withdrawing*: the dependent's committed view changes, it
  deactivates with ordered teardown, and reactivates when a replacement
  appears — R2/R3, already implemented and already demonstrated live
  (demo/live.py). Leak 3, the worst failure mode in distributed systems, is
  revl's best-handled event — a first-class reactive transition instead of an
  exception.

## 4. Distributability as a checked property

**Implemented.** `revl audit` emits this verdict per service in both text and
`--json` (`src/revl/distribute.py`, `tests/test_distribute.py`); the block
below is live output, not a sketch.

Leak 2 (latency) cannot be removed, but it can be *named*. The natural home
is the `revl audit` command that already reports the G8 boundary surface. Add
a per-service verdict: a service is **transport-safe** iff every operation is
`async fn` with value-typed parameters and returns, and no resource type
crosses the boundary in either direction; it is **address-space-bound** if any
operation is a bare synchronous method (chatty, latency-bound — works
remotely, but the audit flags the cost) or any signature mentions a resource
type (a handle, hence a lifetime coupled to a remote fiber).

```
$ revl audit components/*.rvl --json
{
  "manifest": { … },
  "boundary": { … },
  "distributability": {
    "Database": { "verdict": "address-space-bound",
                  "reasons": ["execute: emission (sync)", "query: not async fn"] },
    "Cache":    { "verdict": "transport-safe",
                  "reasons": ["all operations async fn", "all params/returns value-typed"] }
  }
}
```

This is something AIDL never had: the compiler telling you *which interfaces
may cross* before you try — and *at what marshalling cost*, with *what
failure contract*. The boundary disappears from the code (same source,
unchanged) and appears in the audit (explicit, checked, per seam). AIDL hides
the seam and lets you discover the leaks in production; revl hides the
ceremony and documents the leaks at compile time.

## 5. Does this change v2 syntax? — the direct answer

**No new grammar.** The bridge is a backend tier and an audit extension. It
does not grow the system-prompt grammar budget, so the hard constraint in
syntax-2.0.md §8 is untouched.

What it does to the proposal already on the table:

- **It upgrades two already-committed v2 items from "nice" to "load-bearing."**
  `async fn` in services (syntax-2.0.md §5) stops being merely TS fluency and
  becomes the *transport-safety contract* — the one thing the bridge's
  checker predicate turns on. The `extern` classification discipline
  (`pure` / `acquire` / `emission`, §6) stops being merely audit hygiene and
  becomes the *value/resource fork*: a type returned by an `extern acquire`
  is a resource type (crosses by handle); everything else is a value type
  (crosses by copy). If the acceptance benchmark (30 specs × three variants ×
  several models) puts pressure on trimming the "2.0+host-blocks" variant,
  these two must survive the cut — they are now doing bridge work, not just
  ergonomics work.
- **It reserves one §2 type-level item, uncommitted.** A marker to *declare*
  a type as a resource (`Socket`, pools) rather than a value would make the
  value/resource fork explicit in source instead of derived from `extern`
  classification. Recommendation: do **not** add it now. Derive it in the
  checker from existing syntax; if and only if the benchmark plus a bridge
  prototype show the derivation is too coarse, amend §2 — under the existing
  grammar-budget discipline. Types are stratum 2, and the budget is the law.
- **It closes DESIGN.md §10's first open question.** "Surface syntax for
  replacing a provision (rolling update / service broker, paper §6.2) —
  language construct or library pattern on top?" The bridge answers:
  *neither — a backend + manifest concern.* The broker is a `placement` map in
  the manifest (key → host/backend/transport) plus generated proxy/stub code.
  The linker, the checker, and the source language are untouched.

Net: the bridge influences v2's *priorities* (keep `async fn`, keep `extern`
classification), its *sequencing* (§6), and one *reserved* type-level item —
but it adds zero syntax of its own.

## 6. Sequencing

The bridge does not disturb the syntax-2.0 sequencing discipline (run the
acceptance benchmark with a mock checker before implementing stratum 1). It is
a **backend tier**, so it is naturally ordered after the three v2 strata land
— and its only hard dependency on them is `async fn` in services, which the
benchmark will exercise anyway. The plan:

1. *(unchanged)* benchmark with a mock checker — grammar-only validation;
2. *(unchanged)* expression parser → fn/types → match/ADTs/verified, each
   stage keeping all three backend suites green;
3. **bridge backend** as the fourth backend target, once `async fn` services
   exist: proxy/stub emitter + a transport adapter + the `revl audit`
   distributability verdict. First milestone is the smallest honest one — one
   service, one cross-process seam, `PgDatabase` in Python and `UserCache` in
   Node, the same `.rvl`, the same manifest, withdrawal semantics spanning
   the process boundary.

## 7. Open questions

- **Transport contract in the IR.** The manifest `placement` map must be
  serialized in the IR document so `revl audit` can report it — does it live
  in the manifest (next to `loadOrder`) or in a new `deploy` section? Leaning
  manifest: placement is composition data, not component data.
- **Arrow sharing vs. copy for `Bytes`/`List[Int]`.** Worth it only for
  same-machine bridges; should the emitter choose per seam, or is "always
  copy, optimise later" the right first cut? Leaning always-copy first:
  the Arrow path is an optimisation, not a semantics change, and premature
  optimisation is the one leak this document won't hide.
- **Remote handle GC.** Resource handles tie lifetime to the providing fiber
  (which already works, via withdrawal) — but a *consumer* leaking a handle
  across a long-lived composition still needs a release story. The honest
  answer is the same one RPC systems give: refcount at the boundary, audit
  the residuals. Confirm this is sufficient before promising more.
- **Partial failure *inside* a remote call.** Withdrawal covers peer death
  cleanly, but a single call that fails mid-flight over a still-alive peer is
  leak 3's residue. The `Result[T, E]` value type already carries the error;
  the question is whether the *async contract* should require it at the
  interface (a bridge service returns `Result`, not a bare value) or leave it
  optional. Leaning: audit flag, not a hard requirement — same reasoning as
  `commutative` (opt-in strictness, not a default).
