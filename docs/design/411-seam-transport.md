# 411: the sandbox seam transport (T3 reachability, and the T1 to T6 plan)

Design note for the T3 lane of roadmap item 411 (issue #107), a sub-design of
`docs/design/411-sandbox-placement.md` ("The boundary, precisely" and the
Stage 2 "Not yet, and why" paragraph). Sources: `src/revl/sandbox_runtime.py`
(the container rung, the blanket seam refusal at `ContainerDriver.preflight`,
the boot canary), `src/revl/placement.py` (the item-56 network-seam
preconditions, `generate_seam_certs`, `_serve_endpoint` / `_proxy_endpoint`,
the sandbox driver context built before spawn), `src/revl/_process_runner.py`
(the `serve` block: mTLS listener, `PeerAllowlist`, `TransportReplayGuard`),
`backends/python/bridge.py` (`Endpoint`, `serve`), `docs/network-placement.md`.

This note decides one thing and schedules six. The decision is how a seam
reaches across a container boundary at all, because today it cannot: a
container-sandboxed process with any cross-boundary seam is refused
(`sandbox_runtime.py`, `preflight`, the `seam_keys` branch; locked by
`test_a_cross_boundary_seam_refuses_until_the_transport_lands`). Everything
else in the 411 Stage 2 composing half (the approval channel, the structural
crossing walk, the microVM rung) waits on that one answer, which is why it
gets its own design pass. It changes no code.

## The gap, measured

The 411 design chose a private bind-mounted Unix socket for the container
rung and then measured it non-functional on Docker Desktop (macOS): a socket
bound inside the container under the mount is not connectable from the host,
and the reverse gets `ECONNREFUSED`. The two fallbacks it named each hit a
wall the design did not resolve:

- TCP+mTLS (item 56) exists end to end, but a `--network=none` namespace has
  no loopback to the host, so there is nothing for the seam to dial, and the
  obvious fix (let the sandbox reach exactly the seam endpoints) is the
  deferred net-allowlist, which the 411 design already recorded Docker does
  not natively enforce.
- A shared network namespace (`--network=container:<x>` or `host`) exists on
  Linux only. On Docker Desktop the containers run in a Linux VM with no
  namespace in common with the macOS host.

What was missing is a measurement of what Docker DOES natively enforce that
is narrower than `all` and wider than `none`. Measured on Docker Desktop
20.10.16 (macOS, arm64, `python:3.12-slim`), each probe a 3 second TCP
connect or a `getaddrinfo`:

| probe | from | result |
|---|---|---|
| connect TEST-NET-1 (192.0.2.1:9) | `--network=none` | `ENETUNREACH` (101) at once |
| connect TEST-NET-1 | `--internal` user-defined network | timeout (dropped, not refused); the route table shows a default route via the network's gateway plus the on-link subnet |
| resolve `example.com` | `--internal` network | unresolved (`EAI_AGAIN`); the embedded resolver forwards nothing |
| resolve `host.docker.internal` | `--internal` network | unresolved |
| resolve and connect a sibling container by name | `--internal` network | resolved, open |
| connect a container's bridge-side IP:port | `--internal` network | timeout |
| connect that same bridge-side IP:port | a container on `bridge` AND the internal network | open |
| connect a host listener bound to 127.0.0.1, via `host.docker.internal` | the same two-network container | open |
| connect a sibling on the internal network by name | the same two-network container | open |
| `-p 127.0.0.1:0:9443` on a `bridge` container | host | `docker port` reports the mapping; connect open |
| `-p 127.0.0.1:0:9443` on an `--internal` container | host | `docker port` reports nothing; connect refused |

Three facts fall out. An `--internal` network is a natively enforced
"nothing but this subnet" posture, enforced by the same daemon that enforces
`--network=none`, and it closes DNS with it. A container attached to BOTH an
internal network and the default bridge reaches the internal side by name,
the host's loopback by `host.docker.internal`, and can publish ports; the
sandbox on the internal side alone can do none of those. And the negative
evidence differs in kind from `--network=none`: the internal network DROPS
rather than refusing, so the current canary's "a timeout is a refusal" rule
cannot be reused as is; a seam-only canary needs a probe that discriminates
(the same target open from one vantage point and closed from the other,
which the table shows is measurable).

Linux is not measured in this note. What is relied on there is documented
Docker behaviour (`--internal` since 1.10, `--add-host
host.docker.internal:host-gateway` since 20.10, the embedded DNS on
user-defined networks), and the one Linux-specific fact that matters (which
host address the relay reaches the host on) is PROBED at preflight, never
assumed. See "Cross-platform, honestly".

## The decision

**A seam-only network per sandboxed process, and one conductor-owned relay
container that is the only thing on it.** The seam stays item 56's TCP+mTLS
end to end between the two processes; the relay is a blind byte forwarder
that holds no key and sees ciphertext. Concretely:

- The conductor creates one `--internal` user-defined network per sandboxed
  process (`revl-sb-<placement>-<pname>`). The sandboxed container is
  attached to that network and to nothing else when `net = "none"`. With
  `net = "all"` it is attached to the default bridge as well; the seam path
  is identical in both cases, so there is one transport code path.
- The conductor starts one relay container per placement
  (`revl-sb-<placement>-relay`), from the same first-party runner image the
  sandbox uses, running conductor code (`python3 -m revl.seam_relay`), on the
  default bridge, and then `docker network connect`s it to every per-process
  internal network. Its forwarding table is derived from the placement and
  printed in the boot summary.
- Each direction of a seam is one row of that table:
  - **sandboxed provider, host-side consumer.** The provider binds inside its
    namespace (`0.0.0.0:9443`; the namespace holds nothing but the relay).
    The relay publishes a conductor-chosen host port bound to `127.0.0.1`
    and forwards it to `<container-name>:9443`, resolved by Docker's
    embedded DNS at connect time. The consumer's proxy endpoint is
    `127.0.0.1:<host-port>` with SNI `localhost`, which the minted leaf's SAN
    set already carries.
  - **sandboxed consumer, host-side provider.** The provider listens on the
    HOST BIND ADDRESS (below) at a conductor-chosen port. The relay listens
    on its address on the consumer's internal network at a conductor-chosen
    port and forwards to `host.docker.internal:<port>`. The consumer's proxy
    endpoint is `<relay-name>:<port>`, SNI `localhost`.
  - **sandboxed consumer, sandboxed provider.** The relay listens on the
    consumer's network and forwards to `<provider-container>:9443` on the
    provider's network. Neither sandbox can reach the other directly; they
    are on different networks.
- The relay binds each listener on its address on ONE network, so a sandbox
  sees exactly the endpoints its own seams use and no other process's.
- **Host bind address.** The address a host-side seam listener binds so the
  relay can reach it is platform-dependent: on Docker Desktop
  `host.docker.internal` reaches the host's loopback (measured), so
  `127.0.0.1` is right; on Linux `host-gateway` is the bridge gateway
  address, and a loopback-bound listener is not reachable through it. The
  conductor does not guess. At preflight it binds a throwaway listener on
  each candidate in order (`127.0.0.1`, then the default bridge's gateway
  address from `docker network inspect bridge`) and has the relay connect
  to `host.docker.internal:<port>`; the first candidate that answers is the
  bind address for every host-side listener that serves a sandbox this run.
  No candidate answering is a refusal naming both candidates. `0.0.0.0` is
  never chosen silently.

Why this and not the two alternatives the T3 brief put on the table:

**Published port on the default bridge (no relay).** This is the sandbox
attached to `bridge`, publishing its own port and dialing
`host.docker.internal` itself. It is the smallest change and it works on both
platforms (measured), but the posture it provides is `net = "all"`: the
container has full egress, and the only thing standing between a hostile
body and exfiltration is nothing. Since a seam is what every useful component
has, this would make the 329/249 payoff (confinement that is ENFORCED)
unavailable to any sandboxed component that composes with anything. It is
kept as a fact, not a mode: under this design `net = "all"` plus a seam is
exactly this topology plus the relay, and the boot summary says `all`.

**An egress proxy or netfilter allowlist.** Host-side `iptables` on the bridge
needs root and does not exist on Docker Desktop; an L7 proxy the sandbox
must speak through would have to see plaintext to filter by host, which puts
a conductor component in the middle of the mTLS trust the seam is built on.
The relay chosen here is the degenerate case of this option (a TCP-level
table, conductor-owned, ciphertext only), and it is the natural place for
the deferred net HOST allowlist to land later (a `host:port` row is a relay
row; a hostname row needs DNS in the relay, which is the part still
deferred). Nothing in this note promises that follow-on.

**Linux-only shared netns.** `--network=container:<relay>` would put the
sandbox INSIDE the relay's namespace: no per-process isolation from the
relay's own egress, and no macOS at all. On Linux the relay could also be
skipped entirely (host listeners bound on the internal bridge's gateway
address, host consumers dialing container IPs directly), which is faster
but a second code path that runs on one platform and cannot be measured on
the developer machines this repo is dogfooded on. Not chosen for v1;
recorded as a possible Linux optimization once the relay path is landed and
tested, and only with its own measurement.

## The net posture, honestly

What `net = "none"` plus a seam PROVIDES under this design, each with its
enforcer named (the 411 trust-boundary discipline):

- **No egress except the relay's listeners on this process's network.**
  Enforced by the daemon (`--internal`), confirmed by the seam-only canary
  each boot. The relay forwards each listener to one conductor-designated
  target; the table is printed.
- **No sibling reach.** One network per sandboxed process; two sandboxes
  reach each other only through relay rows the placement's seam graph put
  there. Enforced by the daemon, confirmed by the canary.
- **The relay cannot read, forge, or impersonate.** mTLS terminates in the
  two processes; the relay holds no certificate, no key, and no CA key. It
  can drop or delay (an availability property the per-operation seam
  deadline already bounds, R2/R3), and it can only forward where its table
  says. It is conductor code in the runner image, trusted the way the
  conductor is, and a bug in it is a conductor bug, not a widening of the
  sandbox.
- **Admission is closed.** Every sandbox seam is intra-placement by
  construction (a `[remotes]` key is never a sandboxed provider), so the
  conductor DERIVES the provider's `peers` from the placement's own
  consumers, and the seam reports `peer-pinned`, never `UNVERIFIED`. The
  item-56 rule that `peers` is declared, not derived, exists for
  cross-composition callers, which this seam cannot have. T3 additionally
  installs the item-118 correlation guard on the sandbox seam (the sandboxed
  consumer is this conductor's own child and holds the per-boot secret via
  its spec), reaching `sealed` over TCP; this is expected to work because
  neither the runner's guard wiring nor the proxy's sealing is
  transport-specific, and it is an exit test rather than a claim.
- **A published port is loopback-only and cert-required.** `-p 127.0.0.1:`
  never `0.0.0.0`; `CERT_REQUIRED` refuses a caller with no CA-signed leaf
  at the handshake; the CA key lives in the conductor's `0700` placement
  directory.

What it does NOT provide, stated so no summary of the feature can quietly
overclaim:

- **Loopback is not the `0700` directory.** A UDS seam is reachable by the
  invoking user only, by filesystem permission. A host-published port is
  reachable by every local process of every user; admission is
  cryptographic (mTLS plus peers plus the correlation guard), not
  filesystem. That is item 56's status quo for loopback network placements
  and it is stated here because a sandbox seam brings it to placements that
  never asked for network placement.
- **The relay is one more trusted party in the seam path.** It cannot break
  confidentiality or integrity, but it is a process the conductor must keep
  alive and whose table the conductor must derive correctly. Relay death is
  every sandbox seam breaking at once: the monitor connection drops turn into
  reactive withdrawal through the existing machinery, and the conductor also
  names the relay as the cause.
- **The enforcer is still trusted.** `--internal` is iptables inside the
  daemon's host or VM, requested and not verified, exactly as
  `--network=none` is today. The canary detects gross non-enforcement (a
  sandbox that reaches what only the relay should reach); it does not verify
  the enforcer.
- **DNS is closed on Docker Desktop 20.10 (measured), not by design
  guarantee.** The embedded resolver on an internal network forwarded
  nothing here; it is a canary clause (an external name must not resolve),
  so a daemon version where it forwards refuses at boot rather than leaking.
- **Timing and shared-kernel side channels** are unchanged 411 residue.
- **No host allowlist.** `net` stays `"none" | "all"`; an allowlist spelling
  is still refused with the gap named. The relay is where that follow-on
  would live; this note does not start it.
- **`net = "all"` plus a seam is `all`.** The sandbox is on the default
  bridge; the relay still carries the seam so there is one code path, but
  the posture line in the boot summary says `all`, and nothing about the
  relay narrows it.

## The seam-only canary

The existing canary (`sandbox_runtime.py`, `_CANARY_SH` and `_EGRESS_PY`)
confirms `net = "none"` by an active connect to TEST-NET-1 that must fail AT
ONCE (`ENETUNREACH`), and treats a timeout as unconfirmed. On the internal
network that exact clause would refuse an established boundary, because the
internal network drops instead of refusing (measured: timeout, with a default
route present). The seam-only variant therefore replaces the single negative
probe with a discriminating pair plus two closures, all run before launch on
the process's own network with the relay already up:

- **SEAM=open:** connect to each relay listener this process's seams use;
  every one must accept (the relay answers a TCP connect before any TLS).
- **ISOLATION=confirmed:** connect to a conductor-designated target that the
  RELAY has just proven open from its bridge side (the relay's own
  bridge-side address on a port it listens on); from the sandbox it must NOT
  open. Same target, two vantage points, one of them positive, which is what
  makes a drop evidence rather than absence of evidence.
- **DNS=closed:** an external name (`example.com`) must fail to resolve, and
  `host.docker.internal` must fail to resolve.
- **ROUTES** is reported, not judged: the internal subnet plus a dropped
  default route is the expected shape, and the ISOLATION clause is what
  carries the weight.

Any clause that cannot be confirmed is a refusal, never a downgrade, the
rule the existing canary already enforces. The `--network=none` clause set
stays as is for a seam-free sandbox; nothing about a seam-free placement
changes.

## Identity, certificates, and the secrets rule (T2)

The item-56 preconditions apply to every process on a sandbox seam, restated
as sandbox-gate refusals that name the precondition (T1) rather than the
blanket "seam-free only" refusal:

- a sandboxed **provider** is py (already true: the container rung is
  py-only), and a host-side provider serving a sandbox must be py (the mTLS
  listener is py-only, `placement.py`, the `is_network_provider` branch);
- a **consumer** on either side is py or node; a rust/go/java consumer of a
  sandboxed provider is refused naming the tier and the UDS-only client;
- every participant's `seam_deadline` is non-null.

Certificate material is minted by the conductor per boot for every
sandbox-seam participant, without `generate_test_certs` and without an
operator-written `address`: `generate_seam_certs` already produces exactly
the shape needed (one leaf per identity, `serverAuth` plus `clientAuth`, the
`127.0.0.1` and `localhost` SANs, a one-day throwaway CA in the placement
directory). The difference from item 56's flag is in what it is called: these
are not "loopback test certs" the operator opted into, they are the
conductor's per-boot seam identities, minted with the same trust as the
per-boot correlation secret, and the boot summary names them that way.
Identity defaults to the process name; when the placement names an
`operator_profile`, item 55's attribution rule holds and the author must
declare `[processes.<p>.tls] identity` as a declared operator, refused
otherwise.

The 411 secrets rule becomes load-bearing here and is the reason T2 precedes
T3: today the container mounts the whole placement directory read-write
(`preflight`, `declared = [(ctx["seam_dir"], "rw")] + ...`). Once that
directory holds every process's private key, that mount hands a hostile
sandbox its siblings' identities. T2 narrows the mount to a per-process view
(the process's own spec file, its own leaf and key, and the CA certificate,
never the CA key), which is what the 411 design already required and Stage
2a deferred while the seam did not cross.

## The slice plan

Ordered; each independently verifiable; each names its files, its exit
test, and where it runs. "Plan-layer" means no container runtime is needed
to test it. "Docker-gated" means the test runs where the existing container
rung tests run (a developer machine with Docker Desktop; `ubuntu-latest` in
CI) and skips elsewhere, as `tests/test_sandbox_container_rung_411.py`
already does for its live probes.

**T1: transport selection and descriptor (plan-layer, cross-platform).**
Files: `src/revl/placement.py` (the sandbox driver `ctx` built before spawn
gains the seam ROLES: keys provided across the boundary, keys consumed, the
peer process and its backend for each, plus the item-56 role and deadline
checks carried into the sandbox gate), `src/revl/sandbox_runtime.py`
(`preflight` replaces the blanket `seam_keys` refusal with a transport
descriptor, `{"transport": "relay-mtls", "unmet": [...]}`, and refuses
naming each unmet precondition; until T3 lands the descriptor's own
reachability precondition is unmet and the refusal names T3),
`tests/test_sandbox_container_rung_411.py` (retarget
`test_a_cross_boundary_seam_refuses_until_the_transport_lands` to the named
precondition; do not delete it). Exit: a sandboxed provider with a rust
consumer refuses naming rust and the UDS-only client; with a py consumer and
a null `seam_deadline` refuses naming the deadline; with everything
satisfied and T3 absent refuses naming reachability; a seam-free sandbox is
byte-identical to today.

**T2: identity and certificate auto-promotion (plan-layer, cross-platform;
needs `openssl`).** Files: `src/revl/placement.py` (the cert block after the
item-56 preconditions: a `sandbox_seam_processes` set alongside
`network_processes`, minted per boot into `tmp/certs/<pname>/` holding leaf,
key, and CA cert only; identity default and the `operator_profile` rule),
`src/revl/sandbox_runtime.py` (the per-process mount view replacing the
whole-directory mount), `docs/network-placement.md` (one paragraph naming
conductor-minted seam identities as distinct from `generate_test_certs`).
Exit: for a placement with two sandboxed processes, each process's achieved
mount list (`achieved["mounts"]`) contains its own key and no other
process's, and never the CA key; an `operator_profile` placement with an
undeclared sandbox identity refuses naming item 55; a seam-free sandbox
mints nothing.

**T3: reachability, the relay (docker-gated; cross-platform by probe).**
Files: `src/revl/seam_relay.py` (new: an asyncio TCP forwarder driven by a
JSON table of `{listen: {network, port} | {publish: host_port}, target:
"name:port"}` rows, lazy name resolution per connection, a per-listener
connection cap, an `UP` line on stdout; it runs inside the runner image and
must import nothing outside the stdlib), `src/revl/sandbox_runtime.py`
(network create and remove, relay start, `network connect`, and teardown;
the host bind probe; the seam-only canary clauses; `container_flags` emits
`--network <per-process network>` in place of `--network=none` when the
process has seams, and the driver connects the container to `bridge` when
`net = "all"`; teardown removes relay and networks even after a killed
conductor, the existing belt), `src/revl/placement.py` (spec shapes: a
sandboxed provider's `serve.endpoint` carries `bind = "0.0.0.0"` and
`port = 9443`; a host-side provider serving a sandbox binds the probed host
bind address; consumer `proxies[key].endpoint` per direction as in the
decision; the correlation guard and derived `peers` installed on every
sandbox seam; the relay table and the posture line in the boot summary),
`backends/python/bridge.py` (`serve` binds `bind` when the endpoint carries
it, else `host`, so the dial address and the bind address can differ; the
client is unchanged), `docs/design/411-sandbox-placement.md` (the Stage 2
"Not yet, and why" paragraph updated to point here; no other line of that
document changes). Exit: three placements, one per direction, each with a
`--once` probe whose cross-boundary call returns its value; the seam reports
`sealed` (or `peer-pinned` if the guard does not carry over TCP, in which
case the exit test says so and the posture section above is corrected in
the same PR); `docker kill` on the sandbox withdraws the consumer and on
the relay withdraws every sandbox seam with the relay named; teardown
leaves no container, no network, and no published port; a sandbox launched
on the default bridge instead of its internal network (the non-enforcing
daemon stand-in) dies at the ISOLATION clause; the host bind probe picks
`127.0.0.1` on Docker Desktop and the bridge gateway on Linux CI, printed;
no candidate answering is a refusal naming both. A seam-free sandbox and
every non-sandboxed placement are byte-identical through boot output.

**T4: structural crossing-type walk (plan-layer, cross-platform).** Files:
`src/revl/placement.py` (a `sandbox_crossing_check` over every seam that
touches a sandboxed process: walk the crossing type structurally, every
record field and ADT arm, transitively, refuse on any embedded resource
type, refuse on a type it cannot resolve), `src/revl/distribute.py` (the
existing name-based `cross_tier_boundary_check` stays; the structural walk
is the sandboxed-seam rule regardless of where the 363 fix lands),
`tests/test_sandbox_placement_411.py`. Exit: an operation on a sandboxed
seam returning `Conn` where `record Conn { sock: Socket }` is refused at
plan time naming the field path, identically to the top-level resource; an
unresolvable crossing type refuses; the same operation on an unsandboxed
seam is unchanged (the 363 check's verdict, whatever it is).

**T5: the conductor-served approval channel (docker-gated for the
end-to-end test, plan-layer for the rest; cross-platform).** Files:
`src/revl/placement.py` (the conductor serves an `approval` key over the
same mTLS listener shape at the host bind address, with its own
human-scale deadline distinct from the per-operation seam deadline; one
relay row per sandboxed process that can raise class-(c) operations; the
key counted by the gate and printed in the seam-served lines),
`src/revl/_process_runner.py` and `backends/python/runtime.py` (the item-246
approval hook routes to the seam proxy when the spec carries an `approval`
endpoint instead of a tty), `docs/design/246-auto-approve.md` (cross-link).
Exit: a sandboxed component's class-(c) operation raises its prompt through
the channel and completes when approved, refuses when denied; an operator
delay longer than the seam deadline breaches nothing; the approval key
appears in the boot summary; a sandboxed process with no class-(c)
operations gets no approval row.

**T6: the microVM rung (Linux-only).** Reuses the T2 identities, the T3
table, canary, and bind probe. The VM's virtio NIC sits on a host-only tap
(no default route, the T3 posture by construction); the relay runs as a
HOST process (the same `revl.seam_relay` module, the table's listeners on
the tap address instead of a container network); the monitor is the child
process. Files: `src/revl/sandbox_runtime.py` (a `MicroVMDriver` behind
`resolve_driver("microvm")`), `src/revl/seam_relay.py` (host mode).
Exit: the T3 three-direction tests under `isolation = "microvm"`. This
slice needs `/dev/kvm`, which neither the CI runners nor a developer laptop
reliably has (the 430/445 lesson in `sandbox_runtime.py`'s header): the
exit test must run in a KVM-capable lane, and until it has, the rung stays
the clean named-gap refusal it is today, never a container substituted.

## Cross-platform, honestly

| slice | macOS (Docker Desktop) | Linux (docker engine) | notes |
|---|---|---|---|
| T1, T2, T4 | yes | yes | plan-layer; T2 needs `openssl` |
| T3 | yes, measured in this note | yes, by documented behaviour plus the bind probe; CI on `ubuntu-latest` is the measurement | Docker 20.10 or later (`host-gateway`); podman is out of scope, refused by name if `docker` is absent |
| T5 | yes | yes | end-to-end test docker-gated |
| T6 | no (no KVM) | yes, with `/dev/kvm` | Linux-only by nature, not by omission |

The one place the design would have had to assert Linux behaviour without
measuring it (which host address the relay reaches) is replaced by a
preflight probe, so the same code is honest on both platforms and prints
what it found. If the probe finds nothing, the placement refuses naming the
candidates; it does not fall back to `0.0.0.0` or to `net = "all"`.

## The honest hard part

Four residues, so the next reader does not rediscover them. First, the relay
is a conductor component in the seam path, and the argument that it widens
nothing rests on mTLS terminating in the processes; any future change that
gives the relay a certificate (an L7 allowlist, say) breaks that argument
and must re-derive the posture section. Second, the canary's negative
evidence is a drop, not an errno, and only the two-vantage probe makes it
evidence; a shortcut that keeps the TEST-NET clause and reads a timeout as
"confined" would be exactly the silent-weaker-grant hole 411 exists to
close. Third, the loopback-versus-`0700` gap is real and should not be
papered over in the boot summary: a sandbox seam is a loopback network seam
with cryptographic admission, and the summary line says so. Fourth, `net =
"all"` plus a seam is a legitimate manifest and a weak posture, and the
temptation will be to let the relay's presence read as narrowing it; the
posture line prints `all` and nothing else.
