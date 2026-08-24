# revl canary — progressive delivery with a derived rollback

**Status:** implemented (2026-08-24) · `revl canary <baseline> --candidate
<file> --slice <realm>` · MCP verb `revl_canary` · companion to
[swap.md](swap.md), [design-v2-realms.md](design-v2-realms.md),
[replay.md](replay.md) and [erase-report.md](erase-report.md).

`revl swap` (item 23) is an all-or-nothing cutover: the predecessor drains, the
successor takes the whole service in one admitted step. A **canary** is the
gradual form of the same operation — run both generations at once, give the
successor a *designated slice*, and decide on evidence: promote the successor to
the remainder, or revert the slice. This document is the model. The point of it
is that every mechanism a canary needs already exists in the toolchain; the
canary is orchestration over landed machinery, not new runtime.

## Why the slice is a realm, and why that keeps G2

A canary means two providers of one service key are live at once — the baseline
and the candidate. **G2 forbids exactly that within one realm** (provision
disjointness is per-`(key, realm)`; paper Def. 43). So the canary provider
cannot share a realm with the baseline: it must live in a *different* one. That
is not a limitation to work around — it is the mechanism. A realm is the unit of
a canary precisely because G2 makes a realm the boundary a second provider is
allowed to sit behind:

- a **tenant** is a realm (`realm("tenant_a")`) — canary one tenant;
- a **sandbox** or a **test cohort** is a realm — canary the cohort;
- a **percentage** of consumers is a realm — carve the slice, isolate it, canary
  it.

The canary never weakens G2 to run two providers. It uses realms/instances
(item 10) so the two providers are *legally* disjoint, each serving its own
`(key, realm)`. Slice selection is `placement.slice_partition(ir, realm)`: the
providers serving into the slice, the slice's member components, and the
remainder realms a promote would then have to swap.

## The three moves

### 1. Divergence — a replay comparison, not a metric

Both generations' activations are recorded **worlds**: ordered timelines of
effects, provisions and boundary crossings, in the same `replay.Step`
vocabulary the backwards-replay engine records ([replay.md](replay.md)). The
canary builds the slice provider's timeline for each generation and compares
them step-for-step. Divergence is the **first step whose `(kind, label)`
differs** — or a length mismatch — reported with the exact `(component, realm)`
that produced it.

This is deliberately not a threshold on a counter. "The candidate diverged" is a
statement about the recorded world, attributed to a code site, in the terms a
step-back would use — never "error rate crossed 2%". Because both timelines are
recorded worlds and not metrics soup, the divergence *names* what changed.

The comparison runs off the IR (the ordered account of what each generation
records), so it needs no runtime; where cordis-py is present, the same
comparison is available over live-recorded timelines.

### 2. Revert — the derived LIFO teardown of the slice

Revert is **not a redeploy**. It is the derived LIFO unwind of the canary
slice's accumulator (G7) — the same teardown a swap tears the old provider down
with, scoped to the realm. It reuses `erase_report.build_report(ir, realm)`
verbatim, which composes two landed proofs:

- **residue** — the runtime R4 no-residue proof (`Session.load`/`unload`): after
  the slice tears down, the registry, provisions, effect disposables and event
  listeners are back to baseline. In-process state only (see Non-goals).
- **survivors** — the EXACT set from `query.withdrawal`: every component
  *outside* the realm keeps every provision. G2 is what makes this exact — a
  realm's provisions have no consumer in another realm, so tearing the slice
  down cannot orphan a sibling. `survivors` is the proof that **the other N-1
  tenants are untouched**, and `breached` (a sibling that would lose a
  provision) is provably empty for realm-isolated provisions.

The residue leg needs cordis-py; the survivors leg is static and always runs, so
the "other tenants untouched" claim holds with or without the runtime, and the
R4 leg rides along when the runtime is installed.

### 3. Promote — item 23's swap, for the remainder

When the evidence says go, the sibling realms' providers are swapped to the
candidate generation. The canary does not cut over; it reports whether the
promote is **admissible** by running the *same* admission gate a hot-swap runs
(`placement.swap_admission`) against each remainder provider. The canary
decides; `revl swap <provider> --to <backend>` acts. (Note the swap gate
requires a transport-safe service to re-point across a process seam — an
address-space-bound service refuses here exactly as it would for a direct swap.)

## Surfaces

    revl canary baseline.rvl --candidate cand.rvl --slice tenant_a [--promote-to py]
                             [--provider NAME] [--json] [--no-residue-proof]

Exit status: 0 when the candidate is admitted and the revert is clean (every
sibling tenant untouched); 1 when the candidate is refused by the admission gate
or the revert would breach a sibling.

The MCP verb `revl_canary` takes `baseline`/`baselineFiles`,
`candidate`/`candidateFiles`, `realm`, optional `provider`, `promoteTo`,
`proveResidue`, and returns the same structured verdict: `divergence`
(attributed), `revert` (survivors + residue), and `promote` (the swap verdict).

## The exit test

A canary serves **1 of N tenants** (a designated slice), **diverges under the
replay comparison**, and **reverts with residue proof** — the other **N-1
tenants provably untouched** (`survivors`). Pinned in
`tests/test_canary.py::test_exit_one_of_n_diverges_and_reverts_clean` over a
three-tenant composition (`tests/fixtures/canary_tenants.rvl`): the candidate
for `tenant_a` records an extra acquisition the baseline never did; the
comparison attributes the divergence to `TenantAStore` in `tenant_a`; the revert
proves `{TenantBStore, TenantBApp, TenantCStore, TenantCApp}` are survivors and
`breached` is empty.

## Non-goals — and the stateful follow-on

- **Stateful canary needs item 53.** This is the **stateless** canary: the
  candidate serves the slice cold, and revert is the LIFO teardown of what the
  candidate built. A canary whose candidate must *inherit* the baseline's
  effect-created world across the cutover (a warm cache, a session store) is a
  **stateful** canary, and it needs verified state handoff (item 53,
  `code_change`), which is **not landed**. Until it is, a stateful canary would
  silently start the slice cold — the same trap a stateful swap has today. This
  is a follow-on, deliberately not built here.
- **No scheduling, discovery, or traffic ramping.** The canary decides one
  slice on recorded evidence; it does not discover services, schedule a rollout,
  or ramp a percentage over time. Carving a percentage into its own realm is how
  a percentage slice is expressed; growing it is out of scope.
- **Residue is in-process only.** The R4 proof speaks to runtime state, not to
  data a boundary crossing already emitted. Compensation is not inversion
  (paper §6.1; [replay.md](replay.md) §4.2) — anything the candidate emitted
  during the canary that a downstream observer already saw is outside this
  proof, exactly as it is for `erase-report`.
