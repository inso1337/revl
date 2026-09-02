# Item 173 — per-tier emitted-body routing (go/java/wasm)

Item 167 landed emitted-body routing on py/ts/rust. Item 173 extends it to the
remaining three tiers, each of which needed a runtime liveness primitive the
emitter cannot synthesize (a strict, single-realm, liveness-checked read with no
parent-chain fallback). Per the runtime-ownership split:

- **wasm — first-party, built + tested here.** The primitive lives in the
  `cordis-wasm` substrate (`route:<key>` host op); the emitter
  (`backends/wasm/emit.py`) routes through it. Proven on the real Python +
  wasmtime host by `backends/wasm/test_router_exec_wasm.py`. No fork here — it is
  revl's own runtime.

- **go — upstream fork + PR, built + tested here against the fork.** See
  [`stc-go/REVL-FORK.md`](stc-go/REVL-FORK.md). `stc-go/` is a writable fork of
  `github.com/0xdenny218/stc-go` adding `ServiceInRealm`/`LiveInRealm`
  (`stc-go/route.go` + `route_test.go`). The go emitter emits a router struct
  that consumes it; `backends/go/test_router_exec_go.py` emits the scenario, wires
  it to this fork via a `go.mod replace`, and runs — round-robin, failover and
  G2 all green with go1.26.5. Upstream PR pending; once released, bump the pin
  and drop the `replace`.

- **java — upstream fork + PR spec, NOT built here (no JRE).** See
  [`cordis4j/REVL-FORK.md`](cordis4j/REVL-FORK.md). No real cordis4j runtime is
  reachable and this environment has no Java Runtime, so the primitive
  (`Context.serviceInRealm`) ships as a PR spec plus a realm-aware reference
  implementation in the in-repo stub. The java emitter emits a router class that
  consumes it, verified by `backends/java/test_router_emit.py` (pure-Python
  assertions) and the byte-identity goldens; the runtime side awaits the
  upstream cordis4j release + a JRE.

Every routes-less program on all three tiers emits byte-identically — the
routing paths are gated strictly on a non-empty `routes`.
