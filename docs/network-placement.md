# Network placement — from processes to machines

Roadmap item 56. Placement (`docs/interop-bridge.md` §5, `src/revl/placement.py`)
already splits one composition across host **processes**: a service provided in
one process, required in another, wired by a generated proxy/stub over a
transport. Until now that transport was a Unix-domain socket — one host by
construction. This item lets a seam name a **machine**: point it at `host:port`
in the placement map and the same seam crosses TCP instead of a local socket.

The delta is deliberately small. Everything partial failure already paid for is
reused unchanged:

- the **async contract + reactive withdrawal** on peer death (the monitor
  connection's EOF; `bridge._Client`, `tests/test_swap.py`),
- **canonical value encoding** (records/ADTs cross by copy),
- **seam deadlines** (item 54, `docs/seam-deadlines.md`) — a wedged *remote*
  provider must not block its consumer any more than a wedged local one.

Two things are added on top, and only two: a **TCP + mutual-TLS** transport, and
**identity per process** (issued by the operator model, item 55). A network seam
without both is *refused* — that is why this item is sequenced behind 54 and 55.

## Non-goals

This is a **static map**, nothing more. Explicitly **not** in scope, and not
built:

- **service discovery** — you write the address; nothing resolves it for you;
- **orchestration** — nothing launches the remote process for you (the provider
  runs its own placement on its own machine);
- **placement scheduling** — nothing decides *where* a seam should live.

Pointing a seam at a machine is the whole feature.

## Spelling it in the placement map

A seam becomes a network seam when the **provider process** declares an
`address`. Give that process a TLS `identity`, and give every process that
consumes one of its keys an identity too. Everything else in the placement file
is unchanged, and any process **without** an address keeps the local UDS
transport — no certificate, full back-compat.

```toml
# mint self-signed *loopback test* certs for every declared identity (openssl).
# omit this and supply cert/key/ca paths under each [tls] for real deployments.
generate_test_certs = true

[processes.provider]
components = ["PgDatabase"]
[processes.provider.address]
host = "10.0.0.5"
port = 9443
rtt_ms = 0.4          # optional: the configured latency class for this seam
[processes.provider.tls]
identity = "provider" # this process's identity (an operator token, item 55)

[processes.consumer]
components = ["UserCache"]
[processes.consumer.tls]
identity = "consumer"
```

The provider serves its keys over TCP+mTLS at `10.0.0.5:9443`; the consumer
proxies to that address. A local seam in the same file (a process with no
`address`) still crosses a Unix socket exactly as before.

### Real certificates

`generate_test_certs = true` is a **loopback convenience** — it shells out to
`openssl` to mint a throwaway CA and one short-lived leaf per identity, for
tests and local dogfooding. It never touches real keys. For a real deployment,
drop it and give each network process explicit material:

```toml
[processes.provider.tls]
identity = "provider"
cert = "/etc/revl/provider.crt"
key  = "/etc/revl/provider.key"
ca   = "/etc/revl/seam-ca.crt"
server_hostname = "provider.internal"   # optional; defaults to the address host
```

## The transport

`backends/python/bridge.py` gained an `Endpoint` (a UDS path, or a TCP
`host:port` + `TlsConfig`) and dials each shape appropriately. `serve`,
`_Client`, `repoint` and `proxy_component` all take an endpoint now; a bare path
string still means "local UDS", so the existing callers are untouched.

**Mutual TLS.** Both ends present a certificate. The provider's `serve` sets
`verify_mode = CERT_REQUIRED`, so a caller that reaches the port with **no**
client certificate is refused — a network seam is "the two processes holding
CA-signed certs", not "whoever can reach the socket". The consumer verifies the
provider's certificate against the same CA, hostname-checked. Identity per
process is the certificate's subject: reused from item 55's operator model, so a
seam call is attributable to a named process, not just an address. When the
placement names an `operator_profile`, every network identity must be a declared
operator token, or the placement is refused.

**Deadlines and withdrawal, unchanged.** The deadline machinery bounds the reply
read over TCP exactly as over the UDS — a wedged remote provider raises
`SeamDeadline`, not a hang (`tests/test_network_placement.py`). The monitor
connection still turns a dropped remote peer into a reactive withdrawal. The
canonical codec is transport-agnostic: the same JSON-line request/reply crosses.

## Latency becomes a number

The distributability audit (`docs/interop-bridge.md` §4) called a chatty seam
"latency-bound" in the abstract. Once a seam points at a machine, that becomes a
real figure: `placement.seam_latency_ms(host, port)` measures the median TCP
connect round-trip, and the conductor prints it per network seam once the
provider is up —

```
  seam consumer.cache -> tcp://10.0.0.5:9443  RTT ~0.4 ms (measured)
```

falling back to the configured `rtt_ms` class when the endpoint is not yet
reachable.

## What is refused, and why

A network seam is malpractice without identity and a deadline, so the conductor
refuses to build one that lacks either, naming the missing half:

- a network process with **no `identity`** — a seam that crosses machines must
  present a per-process identity (mTLS);
- a network process whose **`seam_deadline` is null** — an unbounded round-trip
  against a wedged remote provider would block its consumer forever;
- an identity that is **not a declared operator** when an `operator_profile` is
  configured;
- a network seam placed on a **non-py backend** — the TCP+mTLS transport ships
  on the py runner in this cut; rust/go/ts/java runners read only the local
  `socket` form, so a network seam must sit on a py process.

The refusals are config-time diagnostics: nothing is spawned, and the local UDS
path is entirely unaffected by any of them.
