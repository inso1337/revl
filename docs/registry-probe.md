# Search-as-admission probe — verdict (gates roadmap item 49)

**Question (from the roadmap's "Search-as-admission prototype" spike, gating
item 49's protocol):** run the §5 structural-compatibility check as a *query
predicate* over the existing `examples/` components — does "find me an
admissible provider" work with **zero new machinery**? If yes, the registry's
core is already written.

**Verdict: YES.** The registry's search primitive is the existing admission
gate, unchanged. A registry query — "which candidate providers would this
running composition admit?" — is the §5 compatibility relation
(`src/revl/admission.py`, `docs/service-compat.md`) run as a *filter* over a
candidate set. The probe adds **no compatibility logic**: it calls the real
gate through the same entry point the runtime uses and keeps the candidates
that don't raise the admission diagnostic. What a registry adds on top is a
thin, mechanical layer (an index + a candidate-set driver) plus one genuine
prerequisite that is already tracked as item 9 — spelled out at the end.

## What was proven

Probe: `tests/test_search_as_admission.py` (runnable as a script:
`PYTHONPATH=src python tests/test_search_as_admission.py`; also two asserting
tests in the suite).

Corpus: `examples/user_cache.rvl`. Its `UserCache` component is a running
**consumer** of `Database` — it calls `db.query(sql) -> List[Row]` and
`emit db.execute(sql) -> Int`. Those call sites *are* the "service
requirement" a registry search filters against. `PgDatabase` is the provider
hot-swapped out (`replacing=("PgDatabase",)`), so no provider is retained and
the gate applies its **consumer-subtype** regime.

The search primitive, in full — the only code the probe adds:

```python
def admits(running_ir, candidate_src):
    try:
        compile_source(candidate_src, "<candidate>.rvl",
                       manifest=running_ir, replacing=("PgDatabase",))
        return True            # linked -> admissible
    except RevlError:
        return False           # gate refused -> incompatible
```

`compile_source(..., manifest=running_ir)` runs `refuse_admission` ->
`_admit_service_replacement` -> `_service_compatible` end to end. This is the
**real** predicate, not a reimplementation; the probe is read-only on
`admission.py`.

### The filter partitions the candidate corpus exactly along §5

```
candidate providers, filtered by the REAL §5 admission gate:
  ADMIT   MysqlDatabase           identical interface (plain hot-swap)
  ADMIT   AuditedDatabase         ADDS fn ping() -> Bool  (compatible superset)
  ADMIT   PurePutDatabase         DROPS emission on execute (purer is safe)
  REFUSE  ReadOnlyDatabase        REMOVES execute -> UserCache still emits db.execute
  REFUSE  EmittingQueryDatabase   query becomes emission -> UserCache's call site
                                  is unmarked, would silently cross G4/G8

registry search result: ['MysqlDatabase', 'AuditedDatabase', 'PurePutDatabase']
```

Admissible and incompatible providers are distinguished, and both regimes of
the §5 relation are exercised: signature completeness (`execute` removed) and
effect-boundary drift (`query` gains an emission). The refusals also carry
*why* — the diagnostic names the running consumer (`UserCache`) and the
offending method — which is exactly the "why was this candidate rejected"
signal an agent needs. That comes for free from the same call.

## The thin layer a registry adds (sketch — not built here)

The probe proves the **predicate**. A registry is that predicate plus two
mechanical pieces and one real prerequisite:

1. **An index (candidate intake).** The probe's candidate set is a literal
   dict of sources. A registry stores published components and, given a
   requirement, produces the candidate subset worth checking. The index is a
   *pre-filter for cost*, not for correctness — the gate is still the
   authority; the index only narrows what the gate runs over (e.g. bucket by
   the service name a component provides, so a `Database` query does not
   compile-check every `Kv` provider). Nothing structural: `provides ->
   service` is already on every component's IR entry.

2. **The query-as-filter driver.** `search(running_ir, candidates)` in the
   probe — map the predicate over the index's shortlist, return the admitted
   set with each refusal's drift reason attached. `plan._interface_drift`
   already demonstrates the exact same call shape over IR manifests
   programmatically, so a batch/preview driver that never raises is a few
   lines. This is item 49's "the registry runs the gate as its index."

3. **Key namespacing — the one real gap (already item 9).** The probe works
   because there is a *single* consumer and a *single* `db: Database` key. A
   multi-author registry hits the collision the §5 stance called out
   (`docs/v2.0-roadmap.md`, "Do: key namespacing + an interface-compatibility
   check ... **before** any registry"): two independent authors both
   providing `db` collide on key identity, and there is no versioning to
   qualify them. **Both halves are now done.** The compatibility half is this
   probe; the namespacing half landed as namespace-qualified provision keys
   (`ns::key`, `src/revl/parser.py`), so two authors publish `acme::db` and
   `bcorp::db` and neither collides. See
   [namespacing.md](namespacing.md), which names this file as the dependency it
   satisfies.

## Bottom line for item 49

Search-as-admission is real with zero new machinery: the §5 gate, called as a
filter, is the registry's search core, and it already emits the rejection
reasons agents need. Item 49 does **not** need a new compatibility engine —
only (a) an index that shortlists candidates by provided service, (b) a
non-raising batch driver over the existing gate (the `plan._interface_drift`
call shape), and (c) item 9's key namespacing before a *multi-author* registry
is sound. The registry's hard part is already written and shipping in
`src/revl/admission.py`.
