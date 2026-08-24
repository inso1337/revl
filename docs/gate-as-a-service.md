# The gate/compiler as a bridge service (Path A, roadmap item 144)

**Status:** first proving slice — a ts consumer admits a candidate by emitting
to a py `Gate` service over the interop bridge (item 56). A G2 collision is
refused across the seam, verdict + why-trace intact; a clean candidate admits.

## The problem

revl's admission gate is `compiler.compile_files(sources, manifest=running)`
(docs/registry.md §"install is admission"): a candidate joins a running
composition only by passing it, and G2/G3/G4 span the running manifest so a
candidate that would break the assembly is refused *with a why-trace* before it
joins. truc already reaches this gate — but **in-process**, on the py tier:
`src/revl/truc/components/gatekeeper.rvl` provides `Gate.admit_all` whose `@py`
body calls `compile_files`, and only a consumer in the *same* Python process
reads the verdict.

The harness's admission/evolve want to run this gate from a **non-py**
component — a ts candidate wants an admission verdict without rewriting the
compiler in TypeScript. That means the gate has to be reachable *across a
process seam*, as a service.

## The service shape

```revl
service Gate {
  async fn admit(sourcesJson: Str, manifestJson: Str) -> Str
  async fn admit_case(caseName: Str) -> Str
}
```

`examples/gate_service.rvl` declares it; `src/revl/gate_service.py` is the py
provider body. `admit` is the real contract:

- `sourcesJson`  — the candidate composition's files as `{path: source}` JSON,
- `manifestJson` — the running composition IR (`{"manifest":…, "services":…}`)
  as JSON, or `""` for a fresh admit,
- returns a verdict JSON `{ok, diagnostic, admitted, manifest}`. On a refusal
  `ok` is `false` and `diagnostic` carries the compiler's why-trace **verbatim**
  — the same string `revl compile` prints.

`admit_case(caseName)` is a fixture-driven convenience: a placement *probe*
admits only `key.method(literal, …)` and cannot carry a JSON blob through as an
argument, so `admit_case("collide"|"clean")` builds the running manifest and
candidate host-side and calls the same gate. Both operations delegate to the
one `compile_files` call — the gate is unchanged, only the reach differs.

### Why `async fn`, not `emission`

Only a **transport-safe** service — every operation `async fn`, every
parameter and return value-typed — may cross a process seam
(docs/interop-bridge.md §4, `src/revl/distribute.py`). truc's in-process gate is
`emission fn`, which is *address-space-bound*: it could never be a bridge
service. The compile, though, is a **pure** function of (candidate sources,
running manifest): handed the sources in memory it reads no disk and writes
none (`compile_source`: "Nothing is read from or written to the disk"). So the
extern that wraps it is `pure`, the effect tracker (G4) leaves the operation
uncoloured, and `Gate.admit` stays `async fn` — transport-safe. *That the
compile is genuinely effect-free is what makes Path A possible.*

## The placement

`examples/placement/gate_pyts.toml`:

```toml
[processes.gate]
components = ["GateProvider"]        # py tier — the @py extern calls compile_files

[processes.user]
backend = "node"                    # ts tier
components = ["GateUser"]
probe = ["gate.admit_case('collide')", "gate.admit_case('clean')"]
```

The conductor (`src/revl/placement.py::run_placement`) reads the seam from the
IR: `user` requires `gate`, `gate`'s process provides it, so it assigns a UDS,
serves the key on the py side, and installs a **bridge proxy** for `gate` on the
node side (`backends/typescript/placement_runner.ts` → `makeProxy`). The proxy
forwards exactly `service Gate`'s operations (the stub allowlist, G8), and the
seam call is bounded by the ordinary bridge deadline (item 54/56) with reactive
withdrawal on peer death — a wedged gate breaches a `SeamDeadline`, it does not
block the consumer forever. A compiler-service call is a seam call like any
other.

### The design fork, and its fix (`ts_safe_ir`)

`run_placement` compiles **one** composition and hands the *same* IR to every
process's emitter. But the py provider reaches host code through a `@py`-only
extern (`host_gate_admit` → `compile_files`, which has no TypeScript spelling),
and the ts emitter rightly refuses an extern with no `@ts` body
(`backends/typescript/emit.py::_emit_ts_externs`). Handed the whole IR, the node
module would not build — the consumer could not boot.

The consumer never needs that body: it reaches the gate through the proxy, which
speaks the *interface* over the seam. So `placement.ts_safe_ir(ir)` narrows the
IR for the ts module to the tier-emittable slice — it drops every extern with no
`@ts` body and every component (or top-level fn) whose body reaches one, keeping
services, types and ts-safe components verbatim. The py `GateProvider` is
dropped from the node module (it still runs, on its own py process; the node
process consumes it as a proxy); `GateUser` and `service Gate` remain. A
composition with no py-only extern is returned unchanged, so existing node
placements are byte-for-byte unaffected. This extends the existing proxy path
(`_process_runner.py` / `placement_runner.ts` already load own-components +
proxies) from run time back to *build* time.

## The trade-off

**Composition now depends on a running py gate service.** In-process, the gate
was a function call inside the consumer's own process — always available,
synchronous, no failure mode of its own. As a bridge service it is a *provider
that can be down, slow, or unreachable*: the consumer's admit is a seam
round-trip, subject to the seam deadline and to peer withdrawal. That is the
price of reaching the gate from another tier, and it is the same price every
bridge seam pays — the point of item 56 is that the price is *bounded and
observable* (a deadline, a withdrawal), not hidden.

## What a fuller Path A needs (flagged from this slice)

1. **Local UDS forces the shared-IR slice.** This slice runs provider and
   consumer as one composition over a local UDS, so the ts module is the
   *filtered* shared IR. The cleaner shape is **two independent compositions**
   sharing only `service Gate` — the consumer's IR then never contains the
   compiler extern and no filtering is needed. That is the item-56 *network*
   placement (a provider serving "remote consumers [that] live in other
   placements", `placement.py`), but its TCP+mTLS transport is **py-only** today
   (`placement.py`: "place network seams on py processes"), so a *ts* consumer
   over the network path is not yet wired. Deciding between "filter the shared
   IR" (this slice) and "two compositions over the network transport" is the
   next Path-A fork.
2. **Only `admit` is shipped.** evolve needs `propose`/regenerate: admit a
   candidate, and on refusal feed the why-trace back to a generator and retry.
   That loop (and where it runs — the harness, or a `Gate` operation) is the
   next slice.
3. **`ts_safe_ir` is ts-only.** The rust/go/java conductor build steps
   (`_build_rust`/`_build_go`/`_build_java`) still receive the full IR; a
   py-only-extern provider with a *rust* consumer would hit the same fork. The
   filter generalizes (drop externs lacking a body *for the target tier*), but
   this slice only needed it for ts.
