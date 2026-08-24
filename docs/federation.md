# Federated compositions: verified contracts between sovereign systems

Roadmap item 58. Module: `src/revl/federation.py`. CLI: `revl contract`.

Placement (item 55) splits *one* composition across processes — one team, one
deploy, one `revl audit`. The org-scale question is different: **two**
compositions, owned by **two** teams, deployed **independently**, where
composition **A** consumes a service that composition **B** provides. That seam
— the boundary between two sovereign systems — is where microservice
architectures bleed today: the contract between A and B is prose in a wiki, and
drift is discovered in production when B ships a change that silently breaks A.

revl already owns both halves of the fix. Federation just points them *across*
the deployment boundary.

## The manifest is the contract; the drift check is the checker

Two pieces revl already had:

- **The manifest (item 28).** A compiled composition's IR carries a `services`
  table: the exact interface shape of every service — parameter and return
  types, and the `emission`/capability classification that says which
  operations cross the G8 boundary. `revl audit --json` already exports it as a
  versioned interchange document. It *is* the contract, expressed as data a
  tool in any language can read without running revl.

- **The §5/drift check.** Whether one interface is an admissible replacement for
  another is `admission._service_compatible` (DESIGN §5) — the same predicate
  the runtime-admission gate runs when a service is hot-swapped, and the same one
  derived versioning (item 64, `revl version`) reads off to compute a semver
  bump. It already knows that removing an op, narrowing a parameter, widening a
  return, or *gaining* an emission breaks a consumer, while adding an op,
  widening a parameter, or *dropping* an emission does not.

Federation aims the second at the first, across the boundary between A and B:

> Does provider B's **new** manifest still satisfy consumer A's **pinned**
> requirement?

That is a drift question, and revl answers it with the drift predicate it
already trusts — no new relation, no test code.

## Consumer-driven, compiler-verified

The contract is written by the **consumer**. A knows exactly which services it
requires from B and the exact shape it calls them at — that is A's *consumer
surface*. A publishes it; B's CI gates on it.

```
              revl contract export A.rvl          registered with B
   ┌────────┐ ───────────────────────────►  A-pinned.json  ─────────────┐
   │  team  │   the consumer surface: the                                │
   │   A    │   interface shapes A requires                              ▼
   └────────┘   from a provider                            ┌───────────────────────┐
                                                           │  team B's CI, before   │
   ┌────────┐   revl contract check                        │  every deploy:         │
   │  team  │   --consumer A-pinned.json  ◄────────────────│  "would this break     │
   │   B    │   --provider B.rvl                            │   any registered       │
   └────────┘   → pass, or the drift that breaks A          │   consumer?"           │
                                                           └───────────────────────┘
```

No test code is written on either side. The contract is the manifest; the check
is the compiler's own §5 predicate. A break is a compile-time verdict, not a
runtime surprise.

## The consumer surface

A service is part of A's surface when some component of A **requires** it and
**no** component of A **provides** it — i.e. it is supplied by another sovereign
composition. `federation.consumer_surface` projects A's compiled IR down to
exactly those services, copying each interface shape verbatim from the `services`
table so the pin carries the full type and emission/capability detail the drift
check needs. A service A both requires and provides internally is not external
and is omitted: the contract is only about the cross-boundary seam.

```json
{
  "schema_version": "1.0",
  "kind": "revl.contract.consumer",
  "consumer": "team-A",
  "requires": {
    "Store": {
      "methods": {
        "get":   { "params": [{"name": "key", "type": "Str"}], "returns": "Str", "emission": false },
        "write": { "params": [{"name": "key", "type": "Str"}, {"name": "value", "type": "Str"}],
                   "returns": null, "emission": true }
      }
    }
  }
}
```

Like the interchange format, `schema_version` is MAJOR.MINOR: additive changes
bump MINOR, and a consumer of the artifact gates on MAJOR and ignores unknown
members.

## The check

`federation.check(consumer_doc, provider_ir)` feeds A's pinned service to
`version.diff_services` as the **old** interface and B's current service as the
**new** one — the identical consumer regime (`providers_retained=False`) that
`revl version` runs — and reads its classification back:

- **any change graded MAJOR is a contract break.** A removed or narrowed
  operation; an operation that gained an emission A's G8 audit never accounted
  for (its unmarked call site would silently cross the boundary — G4/G8); a
  widened capability scope; or a whole service A requires that B no longer
  provides.
- **a minor change is compatible.** An added operation A never calls, a widened
  parameter, a narrowed return, a dropped emission — additive or strictly-purer
  surface that leaves A's shipped call sites valid.

The verdict is the real predicate's, not a second copy — the same probe items 49
and 64 use to bind their machinery to `_service_compatible`, pointed across the
boundary (`tests/test_federation.py::test_contract_agrees_with_the_real_drift_predicate`).

## CLI

```
# team A: export the pinnable consumer surface
revl contract export A.rvl --consumer team-A > A-pinned.json

# team B's CI, before deploying: does B still satisfy A?
revl contract check --consumer A-pinned.json --provider B.rvl
revl contract check --consumer A-pinned.json --provider B.rvl --json
```

`--provider` takes B's current composition either as its `.rvl` sources
(compiled in place) or as a single pre-compiled manifest `.json` (from `revl
compile -o` or `revl version --emit-manifest`), so it drops into a pipeline that
already has B's manifest as an artifact.

`check` exits **0** when B still satisfies A and prints `contract OK`; it exits
**1** on a break and prints every pinned requirement that drifted, with the drift
predicate's own reason — the shape of a CI gate.

```
$ revl contract check --consumer A-pinned.json --provider B-breaking.rvl
contract BROKEN — deploying the provider would break team-A: 1 pinned requirement(s) drift.

breaking (the consumer's shipped call site is invalidated):
  [break] Store.get  (removed)
      `get` is removed from service `Store`, but a running consumer may still call it
```

## Where it sits

The consumer surface is the artifact a registry (item 49) would hold: B's CI
enumerates its registered consumers and runs `contract check` for each before it
deploys. The break/no-break line is the same one item 64 turns into a semver
bump — federation is that measurement, read across an organization instead of
across a version. The module is read-only on `interchange.py`, `admission.py`,
`plan.py`, `audit_diff.py`, and `version.py`: it reuses the drift and version
machinery whole and adds only the projection and the cross-boundary framing.
