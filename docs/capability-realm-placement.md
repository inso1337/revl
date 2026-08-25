# Capability/realm-aware host placement

Roadmap item 119. Placement (`src/revl/placement.py`) already chooses a
**backend** for each component (which tier: py/ts/rust/go/java/wasm — item 56)
and, via items 56/149/151, which **machine** a seam crosses to. This item adds a
third dimension: choosing which **host** a component may run on by the
**capabilities** that host offers and the **realm** the component belongs to.

The whole feature lives in the placement toml layer plus the realms already in
the IR. It adds **no `.rvl` grammar**: a component's realms are its existing
`isolate` declarations (item 10/56), and the capability it needs is declared in
the placement file, not in source.

## The model (toml shape)

A host (process) may declare two new things, both optional:

```toml
[processes.vault]
components  = ["Vault"]
capabilities = ["seal"]        # capabilities this host OFFERS / permits

[processes.tenant_a]
components = ["TenantAStore", "TenantAApp"]
realm = "tenant_a"             # this host is PINNED to a realm
```

A component's capability *requirements* are a flat placement-level table (this
is the placement-layer spelling of "this component reaches host code that needs
a permit" — it adds no source grammar):

```toml
[capabilities]
Vault = ["seal"]               # Vault may run only on a host offering "seal"
```

## The rules (validated before anything spawns)

`capability_realm_diagnostic` runs on the compiled IR + the parsed toml, in
`run_placement` right after the components-are-all-placed check and before any
network/seam wiring or process spawn. It refuses in the existing `abort(...)`
style (a clear one-line diagnostic, a non-zero exit, nothing spawned):

- **capability** — a component that requires capability `c` (via `[capabilities]`)
  may be placed only on a host whose `capabilities` list includes `c`. A host
  offering a strict superset is fine; a host missing any needed capability is
  refused. A host that declares no `capabilities` key offers **none** — so a
  component with a requirement must land on a host that explicitly lists it.
- **realm** — a host may pin itself to a realm. A component placed on a
  pinned host must belong to no *named* realm other than that one: a component
  isolating a key into a **foreign** realm is refused. A shared/unisolated
  component belongs to no named realm and rides any host; an **unpinned** host
  constrains no component. This is the placement-time face of G2's per-(key,
  realm) provision disjointness (`DESIGN.md` §4, `docs/design-v2-realms.md`): it
  keeps a realm-isolated component off a host that carries a different realm.

Example refusals:

```
error: component 'Vault' requires capability 'seal' but is placed on host
       'vault', which offers db — a component may run only on a host that offers
       every capability it needs ...
error: component 'TenantBApp' isolates a key into realm 'tenant_b' but is placed
       on host 'tenant_a', which is pinned to realm 'tenant_a' — a realm-isolated
       component must stay on a host consistent with its realm ...
```

## Additivity

The dimension is entirely opt-in. A component that requires no capability and
isolates into no named realm is never constrained, and an unpinned host with no
`capabilities` key constrains nothing — so a placement that declares **neither**
validates trivially and produces **byte-identical** specs and output to before
this item. The existing placement examples set neither and are unchanged.

## Optimization: the co-location advisory (conservative, opt-in)

There is a real optimization angle — a provider and consumer of the same key in
the same realm that are split across two hosts pay for a same-realm seam that
realm-affinity co-location would remove. This item ships that angle in its
**minimal, safe** form: an **advisory only**, behind explicit config, that
*names* the opportunity and moves nothing.

```toml
report_colocation = true
```

With the flag set, the conductor prints one line per named realm that spans more
than one host:

```
  co-location: realm 'tenant_a' spans hosts a_app, a_store; co-locating its
  components on one host removes a same-realm seam
```

It never re-places a component and it prints nothing unless the flag is set, so
default runs are byte-identical. Turning the advice into an *enforced* affinity
(refusing a split realm) or an automatic re-placement is deliberately left out
of this cut — correctness (capability/realm-consistent placement + a clear
refusal) is the priority, and the optimization stays conservative.

## Example

`examples/placement/caprealm_app.rvl` is a composition with two realms
(`tenant_a`/`tenant_b`, distinct keys so each realm has its own
provider+consumer) and a realm-neutral `Vault` that reaches a host permit.
`examples/placement/caprealm.toml` is a valid placement — Vault on the
seal-offering host, each tenant subsystem on its realm-pinned host:

```bash
revl run examples/placement/caprealm_app.rvl \
    --placement examples/placement/caprealm.toml --once
```

The refusal cases (a component on a capability-lacking host; a realm-isolation
violation) and the additivity guarantee are covered by
`tests/test_capability_realm_placement.py`.
