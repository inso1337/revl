# `revl plan` — a dry run for admission

**Status:** implemented — `python -m revl plan <files...> [--manifest ir.json]
[--replacing NAME] [--json]`, the library entry point `revl.plan.plan()`, and
the MCP tool `revl_plan`. Tests: `tests/test_plan.py`.

Admission is binary. `compile_files(files, manifest=running_ir)` either links
the candidate against the running composition or raises, and `revl_swap` acts
on that answer. What it never told you is **what happens next** — which
running components stop, in what order their inverses replay, and whether the
composition just grew a way to touch the outside world.

A plan is that answer, computed without producing any of it.

```bash
revl compile app.rvl -o running.json     # what is live
revl plan candidate.rvl --manifest running.json
```

```
plan: ADMISSIBLE   (basis: admitted)

running:   Db, Front, Store
  load order: Db -> Store -> Front
resulting: Db, Front, Store
  load order: Db -> Store -> Front

components
  added:     —
  replaced:  Store
    Store: same interface
  withdrawn: —

provisions
  rebound:
    cache: Cache  Store (replaced)

reactive cascade (predicted — R2/R3)
  rebound    Front  (cache)
  unaffected Db

teardown order (predicted — LIFO, consumers before providers)
  Front -> Store

emission surface (G8)
  gained emissions: Store.db.execute
  totals: emission sites 0 -> 1 (0 -> 1 compensated); …
```

The exit status follows the gate: `0` when the candidate is admissible, `1`
when it is not. A plan is printed either way.

## 1. The model

Two composition manifests, diffed.

The *running* manifest comes in. The *resulting* one is what `lower._link`
builds when the candidate is admitted against it — ambient components minus
whatever this admission drops (same-name implicit replacements, plus anything
named in `--replacing`), plus the newly compiled ones. Everything the plan
reports is read off that pair:

| section | derived from |
|---|---|
| provisions gained / withdrawn / rebound | `(key, realm) -> provider` on each side |
| components added / replaced / withdrawn | entry names on each side |
| resulting load order | the linker's own Kahn ordering |
| emission surface | `revl audit`'s `_boundary` walk over both IRs |
| interface drift | the two `services` tables, compared structurally |

**Provisions are keyed by `(key, realm)`, not by key.** G2 is per-realm
(paper Def. 43), so replacing tenant A's `kv` provider says nothing about
tenant B's — and neither does the plan. A provision that appears on both
sides under a *different* provider (or the same name, when that component is
being replaced) is **rebound**, not gained-and-withdrawn: the key never
leaves the composition, but the thing behind it does.

No guarantee is re-checked here. G2 and G3 live in `lower._link`; if they
would fail, the gate raises and the plan reports the rejection. There is no
second implementation of the linker to drift out of sync.

## 2. The reactive cascade

Admission tells you the *static* composition links. The cascade is what the
runtime does to components that were already running, from the contract in
`docs/backend-ir.md` §"Required semantics":

- **R2 (reactive resolution)** — a component activates only when every
  `requires` key is provided, deactivates when one is withdrawn, and
  reactivates against a replacement provider.
- **R3 (withdrawal ordering)** — dependents fully deactivate before the
  provider does.

For each running component that survives the admission, the plan compares the
provider of every key it injects, before and after:

| before | after | verdict |
|---|---|---|
| a provider | no provider | **diverted** — deactivates, stays PENDING |
| a provider | a provider that is itself diverted | **diverted** (cascade) |
| a provider | a *different* provider, or the same one replaced | **rebound** — deactivates, then reactivates (R2) |
| no provider | a provider | **activated** — an unmet requirement is met |
| a provider | the same provider, untouched | unaffected |

This runs to a fixpoint, so it is transitive: withdraw the database and both
the cache that requires it *and* the API in front of the cache are reported,
the second one labelled as an upstream cascade rather than a direct loss.

A diverted component does **not** leave the composition. It is admitted and
linked; it simply cannot activate. Its emission surface stays on the audit
but is unreachable while it is PENDING — the plan says so in its notes.

## 3. Teardown order

The disturbed set — withdrawn ∪ replaced ∪ diverted ∪ rebound — in **reverse
running load order**. That is the same traversal `run._Driver._dispose_all`
performs (`for name in reversed(_load_order(ir))`), which is what makes
consumers tear down before their providers (R3), and it is G7's derived LIFO
at the composition scale.

Components the swap does not disturb never appear: replacing a leaf does not
restart its provider.

What the plan **cannot** tell you is *what each teardown replays*. The
inverses on a component's accumulator are whatever it acquired at runtime —
a `Pool.open` that was configured differently, an `effect` inside a `provide`
method that ran twenty times. None of that is in a manifest. The plan names
the components and their order; the contents of each teardown are the
runtime's.

## 4. The emission surface

The G8 half reuses `revl audit`'s `_boundary` walk verbatim — the same
function `revl audit` and `revl_audit` print. Retained running components keep
their existing boundary entry; new and replaced ones take the candidate's;
withdrawn ones drop off. The result is the irreversible reach the composition
**gains** and **loses**:

- `gained.emissions` / `withdrawn.emissions` — `Component.key.method` call
  sites the composition can newly reach, or no longer can
- `gained.hostCode` / `withdrawn.hostCode` — externs reachable from a
  component body, transitively through `fn`s
- totals for emission sites, how many carry a `compensate`, iteration
  boundaries (`await`) and host calls

This is the question worth asking before a swap: *does this generation gain a
way to touch the world that the last one did not have?*

If the caller passes a manifest without component bodies (`{manifest,
services}` rather than a full compiled IR), the *before* surface is unknown.
The plan reports `basis: "unavailable"` and says so rather than reporting the
whole candidate as a gain.

## 5. What a plan does and does not promise

Every field carries one of two warranties, and the payload states them
(`guaranteed` / `predicted`).

**Guaranteed — compiler-derived.**

- `admissible`. The gate is *actually run*, not modelled. If this says the
  candidate is admissible, `revl_swap` with the same arguments will admit it.
- Provisions gained, withdrawn and rebound; components added, replaced and
  withdrawn; the resulting load order; interface drift. All read off manifests
  the linker built.
- The emission surface. `revl audit`'s own walk over the same IR.

**Predicted — a model of the runtime, not an observation.**

- The reactive cascade. R2/R3 applied to the manifest graph. A component that
  the plan says will rebind can still land FAILED instead, if its body fails
  during reactivation (A8 L-Raise) — the manifest cannot see bodies failing.
- The teardown order. Correct as an ordering; silent about contents, as above.
- Anything config-dependent. A component's config table changes what it
  acquires, and a plan is computed without one.

**Never.** A plan does not touch the running composition, does not write a
file, does not admit and does not swap. `plan()` takes the running IR as an
argument and mutates nothing in it (`test_a_plan_mutates_nothing`). It is safe
to call on production state.

A plan is also not a *timing* claim. It says which components tear down and in
what order; it says nothing about how long that takes, or about in-flight
calls crossing the swap.

## 6. When the gate rejects

The interesting case. A rejection that only says "no" makes an agent guess;
the plan reports the rejection **and as much of the delta as it can still
compute**, so the author sees both what is wrong and what they were reaching
for. `basis` names how much of the report is real:

| `basis` | meaning |
|---|---|
| `admitted` | the gate accepted; every field is compiler-derived |
| `standalone` | the gate rejected, but the candidate compiles on its own — the delta is what it *would* have been; `resulting.loadOrder` is `null`, because the linker never built one |
| `parsed` | the candidate does not compile at all; component headers were recovered from the AST, so provisions and cascade are shapes, not checked facts (realms are not recovered — `isolate` is a body statement) |
| `none` | not even parseable; only the diagnostics mean anything |

Diagnostics carry a `from` field. `admission` is the gate — authoritative.
`standalone` comes from re-compiling the candidate *without* the running
composition in scope, which is how it can complain that an ambient service is
unknown. Those are labelled rather than hidden, because occasionally the
standalone compile is the one that names the real defect:

```
rejection [T1] <candidate>.rvl:4
  `db.query` argument `sql` expects `Str`, got `Int`
  guarantee: declared types are checked

also [REVL] <candidate>.rvl:2
  unknown service `Database` in `requires` of Extra
  note: seen while compiling the candidate on its own, without the running
        composition's services in scope — it may not be a real defect
```

Interface drift gets its own section. The gate raises on the first service
that disagrees with the running manifest; the plan enumerates **every**
disagreement, so a rename can be fixed in one pass rather than one compile at
a time.

## 7. The MCP tool

`revl_plan` is the rehearsal for `revl_swap`, and takes the same arguments
(`source` / `files` / `modules` / `replacing`) so the two can be called back
to back with an identical payload. In-memory only: a multi-module candidate
that has never existed as a file plans exactly like one on disk.

```
revl_load  → revl_plan  → revl_swap → revl_state
             (what would        (do it)
              this do?)
```

`manifest` is **optional** here, unlike `revl_admit`. When it is omitted and
the server holds a live composition, the plan runs against that — an agent
should not have to round-trip the running IR through its own context to ask
"what would this do?". An explicit `manifest` always wins, and the response's
`against` field says which was used (`session`, `manifest`, or a cold start).

The tool is annotated `readOnlyHint: true, destructiveHint: false`, and that
is the whole point of it existing next to `revl_swap`.

A rejected candidate is **not** a tool error: `ok` stays true (a plan *was*
produced), `admissible` goes false, and the diagnostics ride along. An agent
that treats every `isError` as a failure still gets the explanation.

## 8. Naming

`revl run --plan` is a different thing that predates this: it prints the load
plan for a single composition (order, config, callable keys) and exits without
a runtime. `revl plan` is a *delta* between a running composition and a
candidate. They were left as they are rather than renamed, since `revl run`'s
flag is scoped to a run.

## 9. Open

- **Config in the plan.** A component's config table changes what it acquires;
  `revl run --config` already reads one. Feeding the same file to `revl plan`
  would let the teardown section say more than "these components, in this
  order".
- **Placement.** `revl run --placement` splits a composition across processes.
  A swap that crosses a seam withdraws a *remote* provision (interop-bridge
  §3), which the cascade would model identically — but the plan does not read
  a placement map yet, so it cannot say which teardowns are cross-process.
- **`revl_swap` disposes the whole generation.** The MCP session recompiles
  and reloads everything on a swap, so today it tears down more than the plan's
  disturbed set. The plan reports the *minimal* set R2/R3 require, which is
  what a targeted admission would do — and what the session should converge on.
