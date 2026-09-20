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
| **item 519** (this) | the **surrogate** — a model role reaches no further than the component that consults it |

Attenuation is the last of the four. G4 says *a component may not exceed its
declaration*; item 66 says *a child may not exceed its parent*; item 519 says
*a model may not reach past the component it steers*.

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

## The model role in the product (item 519)

A spawn is not the only edge that can amplify. A **model is an authority
surrogate**: it picks which capability the component consulting it reaches for.
Until item 519 the product accounted for services, realms, taints and budgets
but not for the model, so a component holding `net` whose decisions run through
a role able to reach `shell` was accounted as if the role were inert. Its
*effective* ceiling is the pair's, not its own.

A `model role` (item 512, `docs/design/531-model-placement.md`) therefore
carries a declared **reachable-capability set**:

```revl
model role local on_device  reaches [model.complete]
model role cloud off_device reaches [model.complete, net.request]
```

and the rule is item 66's with a model-route edge in place of a spawn edge:

```
reach(role)  ⊆  held(component)         → admit (the role attenuates, or matches)
reach(role)  ⊄  held(component)         → REFUSE (the surrogate widens)
```

The refusal names both sets:

```
`Classifier` routes `classify` (*) through model role `local`, which reaches
`shell.exec`, but `Classifier` holds only `model.complete` — a component's
effective ceiling is the pair's, so a model may not reach past the component
that consults it (G-MODEL-PLACE)
```

**Undeclared is not empty.** A role with no `reaches` clause reaches the
unnameable `*`, which no held set covers, so it is refused. Reading silence as
"reaches nothing" would make a model nobody has described contribute nothing to
the product, and a model nobody has described is exactly the one whose reach is
unknown. It is the same choice `_spawn_emission_surface` already makes for a
service method that declares `emission` with no capability list.

The question is asked only of a component that holds a boundary which could be
a model call, because a role can only steer an action that reaches a boundary.
An admitted composition records the product per edge under
`manifest.model_reach`, including `attenuated` — what the component holds that
the role does not reach. The section is role-only: a composition that declares
no `model role` has no `model_reach` key.

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

## The admission kernel (item 544)

The product accounts for services, realms, taints and budgets, and item 519
adds the model role. Item 544 adds the one authority that has **no legal
holder**: the admission kernel.

Item 520 states the invariant the self-evolution programme rests on: a system
may evolve its behaviour, never the rules that govern its authority. Stated as
a policy that is a rule some later generation can propose a change to, and the
loop's proposal channel is exactly the mechanism for proposing changes to
rules. So it is stated here instead, as a capability nothing may hold.

`src/revl/kernel_boundary.py` enumerates the kernel once: a capability token
per member (`kernel.admission`, `kernel.attest`, `kernel.taint`,
`kernel.retention`, `kernel.gate`, `kernel.census`, `kernel.formal`), the tree
paths each stands for, and the guarantee each refusal cites. The rule is item
66's with the kernel on the left instead of a spawner, and the fold is
intersection rather than coverage:

```
held(kernel)  n  effective(C)  =  {}   ->  admit
held(kernel)  n  effective(C) !=  {}   ->  REFUSE, naming both sets
```

`effective(C)` is what the component holds, folded with what any authority
surrogate it routes through can reach, which is the `model_reach[].effective`
record item 519 writes, consumed rather than re-derived. A role that declared no
`reaches [...]` clause contributes the unnameable `*`, because an absent
declaration is not a proof of narrowness, and `*` is disjoint from nothing.

The difference from a diff check is the whole item. A diff check asks whether a
candidate touched a kernel path, which is answered after the fact against a
changed-file set and is evaded by any route that reaches the same state without
editing those files. This asks whether the component can hold the authority at
all.

**Retention is on the kernel side.** A loop permitted to update retention
policies as ordinary behaviour tuning is a loop that can extend its own
`Retained[T, P]` deadline, which is an authority change wearing the clothes of
a cache setting; `G-RETAIN` is the guarantee it relaxes. So `kernel.retention`
is a member of the set, and an untrusted author may not declare a `retention`
policy at all (`admit_profile.check_no_retention_policy`). Naming a policy the
trusted composition declares is unaffected: what an untrusted author may not
do is mint the policy that bounds its own data.

`docs/design/545-kernel-boundary-capability.md` carries the design, the
non-vacuity measurement and the residuals.

## Soundness

The check is a compile-time refusal, not codegen — an admitted program emits
identically on every tier. `reach` is a sound over-approximation (host
emissions and first-class dispatch collapse to `*`, which no `requires` key can
name, so an amplifier reaching the host cannot hide behind an unnameable
boundary), and the closure terminates at the least fixed point over the spawn
graph. Narrowing is admitted, equal is admitted, widening is refused: monotone
shrinkage down the lineage.
