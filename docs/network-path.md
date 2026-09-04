# A ts consumer over the network path (fuller Path A)

Roadmap item 149. Path A (item 144, `docs/gate-as-a-service.md`) proved a
cross-tier admit — a ts `GateUser` reaching a py `Gate` service — but over a
**local Unix-domain socket** with a *filtered shared IR*. The clean, general
shape flagged there is **two compositions sharing only `service Gate`** over item
56's **network** transport (TCP + mutual TLS, cross-machine). That transport
existed but its *client* was py-only: the node/ts bridge could speak the local
UDS but not the network path, so a ts consumer over the network was unwired.

This item wires it. A network seam between a **ts consumer** and a **py
provider** is now allowed; the node bridge client dials TCP + mTLS, reusing item
56's certificate/mTLS setup, the seam deadline (item 54), and reactive
withdrawal on peer death unchanged.

## What changed

Two edits, plus a proof.

### 1. Placement allows a ts→py network seam

`src/revl/placement.py` refused *any* non-py process that took part in a network
seam ("place network seams on py processes"). That rule was too broad: the two
sides of a network seam ship on different runners. The check is now per-role:

- the **provider** — the process that declares an `address` and serves its keys
  over the mTLS *listener* (`asyncio.start_server` + `ssl`) — is still **py**;
  that serve side is py-only in this cut;
- the **consumer** — which only *dials* that listener — may be **py or
  node/ts**, because the TCP+mTLS *client* now ships on both runners. A
  **rust/go/java** consumer is still refused (those runners read only the local
  `socket` form).

Nothing else in the placement map changes: the conductor already wrote a network
`endpoint` (host/port + the consumer's mTLS material) and a `deadline` into every
consumer's proxy spec; only the backend guard stood in the way of a node
consumer receiving one.

### 2. The node bridge client speaks TCP + mTLS

`backends/typescript/bridge.ts` mirrored `backends/python/bridge.py`'s proxy half
but only over a Unix socket. It gained the network endpoint shape:

- **`SeamTarget`** — a UDS path string (legacy/local, unchanged) *or* a
  `{ host, port, tls }` object. `makeProxy(key, methods, target, deadlineMs)`
  dials whichever it is; `placement_runner.ts` passes `info.endpoint ?? info.socket`.
- The one-shot RPC client and the monitor connection use **`tls.connect`** for a
  network target — presenting this process's cert/key, verifying the provider's
  against the same CA, hostname-checked (SNI = `server_hostname`). A TLS
  handshake failure is terminal; a bare TCP refusal (provider still coming up) is
  retried, so start order stays irrelevant for an acyclic process graph, the same
  policy as the py client's `_connect_tcp`. A cycle over *processes* would exhaust
  the retries on both sides instead; `placement.process_cycle_refusal` refuses
  such a placement at plan time, before any child is spawned (item 171; background
  in [design/438-petri-reachability.md](design/438-petri-reachability.md) §5.2).
- The seam **deadline** is enforced in the one-shot child: a wedged provider
  trips an in-child timer and the call surfaces a distinguishable
  **`SeamDeadlineError`** (the node mirror of py's `SeamDeadline`), rather than
  blocking the consumer forever.
- **Reactive withdrawal** is preserved two ways: a **dropped** provider EOFs the
  mTLS *monitor* connection, firing `onPeerLost` (the runner disposes the proxy
  fiber — R2/R3); and on a **network** seam a breached **deadline** also
  withdraws — a wedged remote provider is, to a consumer, indistinguishable from
  a dead one, so the consumer stops depending on it rather than re-attempting
  against a wedged machine. (A local UDS seam keeps its death-only withdrawal.)

The mTLS material, the 30s default seam deadline, and the withdrawal semantics
are **reused, not reinvented** — the same certs the py `serve` demands
(`CERT_REQUIRED`, so an anonymous caller is refused at the handshake), the same
`DEFAULT_SEAM_DEADLINE`, the same monitor-EOF-is-withdrawal contract.

## The guarantees across the network boundary

- **G2 (one provider per key)** holds across the seam: a candidate colliding on a
  provided key is refused *by the compiler on the py provider*, its `(G2)`
  why-trace riding back in the verdict — the ts consumer never sees a second
  provider admitted. The compile is a pure function of (candidate sources,
  running manifest), which is what lets the gate be a transport-safe service at
  all (`docs/gate-as-a-service.md`).
- **G8 (the boundary is the declared surface)**: `makeProxy` forwards only the
  declared method names, and the py stub refuses any key or method outside the
  `service Gate` allowlist — so the seam is exactly the enumerable interface,
  network or not.
- **Deadline + reactive withdrawal** on a dropped or slow connection, as above.

## Proof

`tests/test_network_gate_path.py` spawns a **real node process** (the production
`makeProxy` path, `tests/_net_gate_client.ts`) against a **real py provider**
(`tests/_net_gate_provider.py`) over **loopback TCP + mutual TLS** — a real
localhost round trip, not a UDS:

1. the ts consumer admits the `clean` candidate and the `collide` one is refused
   by **G2**, verdict + why-trace surviving the network round trip;
2. G8: an undeclared method never forwards;
3. a **wedged** provider (accepts the handshake, never replies) breaches the seam
   **deadline** and the ts consumer **reactively withdraws**;
4. a **dropped** provider (SIGKILL) is turned into the same withdrawal by the
   mTLS monitor connection.

Certificates are minted with `placement.generate_seam_certs` (the same openssl
loopback test certs item 56 uses); the transport needs no cordis, so the proof
runs anywhere `openssl` and `node` are present.

`examples/placement/gate_pyts_network.toml` is the runnable conductor form — the
same `gate_pyts.toml` shape, but the py `GateProvider` declares an `address` and
the node `GateUser` reaches it over TCP+mTLS. The full cross-tier boot
(`test_full_conductor_boot_over_the_network_placement`) runs it end to end when
both cordis runtimes are installed, and skips cleanly otherwise.

## The two-composition shape (item 151) — the decoupling made real

Item 149 above wired the *network client* onto the node/ts tier and proved the
seam, but it still compiled provider and consumer as **one** composition — a
shared `.rvl` — and handed the node side the `ts_safe_ir` slice
(`docs/gate-as-a-service.md`): the `@py` compiler extern was *filtered* out of
the node module. Item 151 finishes Path A by splitting that one composition into
**two fully independent** ones that share only `service Gate`.

### Two compositions, compiled independently

- **The gate composition** (`examples/placement/gate_provider.rvl`): `service
  Gate`, the `@py` `host_gate_admit*` / `compile_files` externs, and
  `GateProvider`. It is deployed on its own placement
  (`examples/placement/gate_provider_network.toml`), booting the gate behind the
  TCP+mTLS listener at an `address`. No consumer lives here.
- **The consumer composition** (`examples/placement/gate_consumer.rvl`): `service
  Gate` (the interface) and the ts `GateUser`, and **nothing else** — no
  externs at all. Compiled on its own, its IR never contained the compiler
  extern, so `ts_safe_ir` has *nothing to filter* (it returns the consumer IR
  byte-identical). The decoupling is real, not masked: the two IRs never
  overlapped on the compiler extern in the first place.

### Reaching the gate by address alone — `[remotes]`

The consumer placement (`gate_consumer_network.toml`) declares the seam whose
provider lives in the *other* composition through a **`[remotes.<key>]`** block —
item 56's stated non-goal ("the provider runs its own placement on its own
machine"; `docs/network-placement.md`, "Non-goals") made reachable:

```toml
[remotes.gate]
service = "Gate"          # the interface this composition holds
host = "127.0.0.1"
port = 39471              # the gate placement's address
server_hostname = "localhost"   # SNI: a SAN on the gate's leaf (a raw IP is not
                                # a legal TLS servername)

[processes.user]
backend = "node"
components = ["GateUser"]
[processes.user.tls]
identity = "user"
cert = "certs/seam_user.crt"     # a **shared** CA both placements agree on out of
key  = "certs/seam_user.key"     # band — `generate_test_certs` is a single-
ca   = "certs/seam_ca.crt"       # placement convenience and cannot span the split
```

`placement.py` treats a `[remotes]` key as a network seam with **no local
owner**: it is required by a local process but provided by a machine, not a
process in this placement. The consumer's proxy is pointed straight at the
declared address over the same TCP+mTLS client (item 149), presenting the
consumer's own mTLS identity and verifying the gate against the shared CA. A
`[remotes]` naming a service this composition does not declare, a key no process
requires, or a key that is *also* provided locally, is refused with one
diagnostic before anything spawns.

### Proof

`tests/test_two_composition_gate.py`:

1. **the decoupling itself** (no runtime): the consumer composition, compiled
   independently, has an empty `externs` and its IR mentions none of
   `host_gate_admit` / `compile_files` anywhere; the provider composition owns
   exactly those externs; both independently declare the same `Gate` interface;
   and `ts_safe_ir` is a no-op on the consumer. The ts module the conductor
   actually emits for the consumer contains no compiler symbol either — because
   the IR handed to the emitter never had one.
2. **by address alone** (reuses 149's real-node-process harness): an
   independently compiled consumer admits the `clean` candidate against a
   **separately-booted** py gate and the `collide` one is refused by **G2**,
   verdict + why-trace crossing the two-composition boundary; a **wedged** gate
   breaches the seam deadline and the consumer **reactively withdraws**.
3. **the full conductor form**: `revl run` on the *consumer* placement, whose
   `[remotes.gate]` reaches a separately-booted gate by address — the node
   `GateUser` probes `gate.admit_case(...)` across the seam, G2 refusal and clean
   admit both crossing TCP+mTLS. The gate placement is itself a valid standalone
   deployment (`test_provider_placement_boots_standalone`).

This is the remaining Path-A step, now settled: a consumer whose IR never
contains the compiler extern, reaching a separately deployed gate by address.
rust/go/java network *consumers* are still unwired (their runners read only the
local `socket` form); the client generalizes the same way the py and node
clients did, when a tier needs it.
