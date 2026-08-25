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
  retried, so start order stays irrelevant — the same policy as the py client's
  `_connect_tcp`.
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

## What this does and does not settle

This wires the *network client* onto the node/ts tier and proves the seam. It
does **not** yet split the gate into two fully independent compositions: the
example still compiles one composition and hands the node side the `ts_safe_ir`
slice (`docs/gate-as-a-service.md`), because provider and consumer share one
`.rvl` here. The transport is now ready for the two-composition shape — a
consumer whose IR never contains the compiler extern, reaching a separately
deployed gate by address — which is the remaining Path-A step. rust/go/java
network *consumers* are still unwired (their runners read only the local
`socket` form); the client generalizes the same way the py and node clients did,
when a tier needs it.
