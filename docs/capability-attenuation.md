# Capability attenuation on spawn

Instances (`docs/design-v2-instances.md`, roadmap item 10) make one component
live *N* times: `let s = effect spawn Worker with { ... } undo s.dispose()`.
The security question that comes with them is about **lineage** — what a
spawned child is allowed to reach.

Without a rule, a spawn *inherits everything*: a component with no authority of
its own could spawn a child that reaches the network, the database, the audit
log. That makes a spawning component a **capability amplifier** — it grants
authority it does not itself hold. The attenuation rule closes that hole.

> **The rule.** A spawned child's capability set must be a **checked subset**
> of its spawner's. A spawn may *narrow* — pass down less — but never *widen*:
> it may not grant a boundary the parent does not hold. Monotone shrinkage, the
> same direction §5 admits for purity.

## Where it sits

Four rules bound capability, each at a different scope:

| rule | bounds |
|------|--------|
| **G4** (`docs/capabilities.md`) | a component's **declaration** — a provider's body stays within its service's `emission[...]` |
| **item 33** (composition policy) | the **composition** — which boundaries the assembled graph may cross |
| **item 55** (operator authority) | the **operators** — who may act on the running system |
| **item 66** (this) | the **lineage** — a spawned child holds no more than its spawner |

Attenuation is the last of the four. G4 says *a component may not exceed its
declaration*; item 66 says *a child may not exceed its parent*.

## What a component holds, what a child reaches

Two sets, both drawn from the existing G4 capability machinery
(`src/revl/lower.py`).

**Held** — what a spawner may pass down. A component holds a boundary when it
has wired access to it: every key in its `requires` clause (a `requires db: DB`
is the right to reach `db`), plus every boundary its own body already crosses
(its `emit` steps and the emission methods it provides, including the
unnameable host `*`). This is the spawner's *own* authority — deliberately
**not** its transitive spawn closure, so a parent cannot launder a capability it
lacks by routing it through one child into another.

**Reached** — what an instance can actually do. A component's `emit` steps name
the boundaries its code crosses (`_collect_emit_caps`): the required key of
every emission, and `*` for a host emission or first-class dispatch that no key
can name. Closed over the spawn graph, so a child that itself spawns a
grandchild reaching `kv_c` *reaches* `kv_c` too. This is more precise than a
component's *declared* emission surface: a worker that only ever emits through
`kv_a` provably does not reach `kv_b`, whatever its service's bare `emission`
promises — the reach is bounded by the keys it wires through `requires`.

The check, at admission of every activation-body spawn:

```
reach(child)  ⊆  held(parent)          → admit (narrowing, or equal)
reach(child)  ⊄  held(parent)          → REFUSE (widening)
```

### Scope: the activation-body hole

The rule applies to **activation-body** spawns — the top-level supervision
`let s = effect spawn C ...`. A spawn nested inside a `provide` method is
already bounded by that method's `emission[...]` clause (G4 across the spawn
boundary, `_check_spawn_emission_bounds`, decision 8). The activation body has
no such clause, which is exactly the hole item 66 closes.

## Least authority, per instance — for free

The payoff is per-tenant isolation with no new syntax. A router holds two
tenant stores and spawns one worker per tenant, each scoped to its own:

```revl fragment
component Router requires kv_a: StoreA requires kv_b: StoreB {
  let a = effect spawn TenantAWorker with { } undo a.dispose()   // reaches kv_a
  let b = effect spawn TenantBWorker with { } undo b.dispose()   // reaches kv_b
}
```

Each worker's template reaches only one store. The spawn narrows: the
`tenant_a` instance is *granted* `kv_a` and **provably cannot reach `kv_b`**,
even though the `Router` that spawned it holds both. Least authority is a
property of the child's own declaration; the spawn checks it never exceeds the
parent. (`examples/tenant_attenuation.rvl`.)

## The refusal

A widening spawn is refused with the chain named — which spawner, which child,
the offending capability, and what the spawner actually holds:

```
`Supervisor` spawns `Leaker`, granting it `kv_b`, but `Supervisor` holds
only `kv_a` — a spawn may narrow a child's capabilities, never widen them
  a spawned child's capability set must be a subset of its spawner's
  (attenuation, item 66) — `Supervisor` cannot pass down `kv_b` it does not
  hold; add the matching `requires` to `Supervisor` so it holds what it
  grants, or drop the capability from `Leaker` (monotone shrinkage: narrowing
  is sound, widening is not)
```

(`examples/rejections/g4_spawn_widens_capability.rvl`.)

## The audit chain (G8)

`revl audit` shows the attenuation chain per instance — the spawner → child
narrowing, and the authority dropped on the way down:

```
capability attenuation (per instance — lineage narrows, never widens):
  Router → TenantAWorker: holds [kv_a, kv_b] ⊇ grants [kv_a]  (dropped: kv_b)
  Router → TenantBWorker: holds [kv_a, kv_b] ⊇ grants [kv_b]  (dropped: kv_a)
```

The same data rides in `revl audit --json` under `manifest.instances`, one
record per lineage edge:

```json
{ "parent": "Router", "child": "TenantAWorker",
  "holds": ["kv_a", "kv_b"], "granted": ["kv_a"], "attenuated": ["kv_b"] }
```

`granted` is the least-authority proof — the boundaries the instance may reach;
`attenuated` is what the parent held but did **not** pass down. The section is
additive and spawn-only: a composition with no spawns has no `instances` key,
so its manifest is byte-identical to before.

## Soundness

The check is a compile-time refusal, not codegen — an admitted program emits
identically on every tier. `reach` is a sound over-approximation (host
emissions and first-class dispatch collapse to `*`, which no `requires` key can
name, so an amplifier reaching the host cannot hide behind an unnameable
boundary), and the closure terminates at the least fixed point over the spawn
graph. Narrowing is admitted, equal is admitted, widening is refused: monotone
shrinkage down the lineage.
