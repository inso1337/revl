# `revl diff` — the semantic composition diff

**Status:** implemented · roadmap item 123

`revl diff <before> <after>` reports the **IR-level structural delta** between
two generations of a composition — not a textual one. It is the PR-review tool
for agent-generated compositions: instead of "line 14 changed", it says what
*changed about the composition as a system* —

```
component `UserCache` gained emission `bus`
provider of key `db` changed from `PgDatabase` to `MysqlDatabase`
`Auth` now requires `session`
```

An agent regenerates a component; a human (or another agent) needs to see, at a
glance, what that regeneration did to the guarantees of the whole composition.
`revl diff` answers that in the vocabulary of the IR — components, emissions,
and the provide/require dependency graph — so the reviewer reasons about
behaviour, never about text.

## Descriptive, not a gate

`revl diff` is deliberately distinct from
[`revl audit --diff`](audit-diff.md), the authority-drift **gate**. The gate
answers one yes/no question — *did this regeneration widen what the composition
reaches outside the system?* — and fails (nonzero) on an unacknowledged
widening. `revl diff` is **descriptive**: it always exits `0` and prints the
*whole* structural delta across three axes, for a reviewer to read. It reuses
the gate's crossing relation (`src/revl/audit_diff.py`) for its authority axis
rather than reinventing it.

## The three axes

A composition is three things at once, and `revl diff` reports a delta on each:

1. **Membership** — which *components* the composition contains. Reported as
   `added` / `removed` / `changed` (a component present in both generations
   whose emissions, provides, or requires moved).

2. **Authority** — what each component *reaches outside the system*: the
   emissions it performs and the host code it touches. This axis is exactly
   `audit_diff.diff_crossings` over `audit_diff.crossings` — the same
   per-component crossing tokens the authority gate uses
   (`emit:<component>:<label>`, `host:<component>:<name>`). A gained emission
   carries its declared capability scope, so the guarantee reads
   ``component `UserCache` gained emission `put` [bus]``.

3. **Wiring** — the composition's dependency graph: which key each component
   *provides* (and with which providing component/service) and which keys it
   *requires*. Reported as:
   - **provider changes** — ``provider of key `db` changed from `PgDatabase`
     to `MysqlDatabase` `` (a swap of the concrete providing component, or of
     the service interface it satisfies);
   - **added / removed require edges** — ``` `Auth` now requires `session` ```;
   - **broken dependencies** — a require edge whose key has *no provider* in
     the new composition, and was satisfiable before: the dependency the change
     quietly severed.

## Inputs

Each side is one path, in either accepted form:

- a **compiled IR / interchange JSON document** — the output of
  `revl compile <sources> -o composition.json` (or `revl audit --json`); or
- a **`.rvl` source file**, compiled on the spot.

The form is detected from the file content, so the two sides may even mix forms
(a captured IR document before, live source after). A compiled IR document
carries the full key→service wiring; a bare interchange/manifest document
carries only the keys, so it still diffs membership and edges, just without
provider-service identities.

```
revl diff before.json after.json          # two captured generations
revl diff before.rvl  after.rvl           # two sources
revl diff before.json after.rvl           # a captured baseline vs. live source
```

## Output

Default output is a human-readable report: a `+ / - / ~` component list
followed by the guarantee sentences. `--json` emits the machine-readable delta
an agent can consume:

```json
{
  "components": { "added": [...], "removed": [...], "changed": [...] },
  "providers":  { "added": [...], "removed": [...],
                  "changed": [ { "key": "db", "from": "PgDatabase",
                                 "to": "MysqlDatabase",
                                 "from_service": "Database",
                                 "to_service": "Database" } ] },
  "requires":   { "added": [ { "component": "Auth", "key": "session" } ],
                  "removed": [...], "broken": [...] },
  "crossings":  { "added": ["emit:UserCache:put"], "removed": [...] },
  "guarantees": [ "component `UserCache` gained emission `put` [bus]", ... ],
  "changed": true
}
```

`changed` is `false` and every bucket empty exactly when the two compositions
are structurally identical — an identical pair diffs to empty.

## Where it lives

- `src/revl/composition_diff.py` — the diff itself (`facts`, `diff`, `render`,
  `load_composition`), built on `src/revl/audit_diff.py`.
- `revl diff` in `src/revl/__main__.py` — the CLI verb.
- `tests/test_composition_diff.py` — the pinning tests.
