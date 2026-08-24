# Generation history and operator undo

`revl_rollback` restores the generation that was running before the last swap.
It is **depth-1**: one `previous` slot, overwritten the moment a second change
lands. That is enough to unwind the change you just made, and nothing before it.

Item 65 deepens that single slot into what item 15's persistence already makes
cheap. A generation snapshot *is* a persisted composition state (the re-admittable
`{sources, manifest, meta}` bundle of docs/persistence.md). So the history is
just a **stack of those snapshots**, and undo is a walk back down it — with one
non-negotiable rule attached.

## The history model

Every **admitted change** appends a generation snapshot:

* a `revl_load` starts the history at generation 1;
* every `revl_swap` (and every `revl_undo`) appends the resulting generation;
* a `revl_apply` appends too — but from a plan *artifact* (an IR, not sources),
  so its entry has **no re-admittable snapshot** (see "the honest gaps" below).

Each entry is `{generation, snapshot, ir, origin}`:

* `snapshot` — the item-15 re-admittable bundle. This is what an undo *replays
  through the gate*; it is the authority, never the stored IR.
* `ir` — the compiled generation, kept so the dossier can read its G8 boundary
  surface (`revl audit`) without recompiling.

Retention is **bounded** (`HISTORY_LIMIT`, default 64). The oldest generations
age out. An `undo --to` a generation below the floor is **refused honestly** —
it names that the target has aged out — rather than silently reaching for one
that is gone.

The current generation is always `history[-1]`; the previous is `history[-2]`.

## `revl undo`

* `revl_undo` (no argument) returns to generation **N−1**.
* `revl_undo --to <gen>` returns to any **still-retained** generation.
* Additive CLI: `revl undo history.json [--to <gen>]` replays a
  `revl.generation-history` document (the session's `history_document()` export)
  into a fresh session and reverts it — the whole gated path from the command
  line.

## An undo is itself a gated change

This is the load-bearing property. An undo does **not** rehydrate a stored
runtime; it **re-admits the target generation's sources through the same
compile + admission gate a live `revl_swap` runs**. Concretely, `undo`:

1. recompiles the target snapshot's sources (`persist._recompile`) — this *is*
   the gate: parse, check, lower, then the holes gate and the runtime boot
   inside `Session.swap`;
2. if that recompile is **rejected**, the undo is **refused** — a *result*
   carrying the diagnostic, with the running composition **untouched**;
3. otherwise it swaps to the freshly compiled target IR, and the resulting
   generation is itself appended to the history.

Why insist on this? An undo that bypassed the gate would be the **one unverified
path into a running system**: a generation that was admissible yesterday, under
an older checker, could be smuggled back in past a checker that now rejects it.
So an undo gets no such path. It is a change like any other change, and it earns
its way in the same way — which is also why an undo can itself be undone (the
git-revert of a git-revert is a commit).

## The undo dossier

Because an undo is a change, its plan is computed like any change:
`plan.plan(target sources vs the running IR)`. The dossier reports:

* **what unloads** — the components the revert tears down (the reactive cascade,
  `withdrawn`);
* **what state drops** — item 53's "state: dropped" honesty, in reverse: the
  provisions the older shape no longer serves are named, not silently lost;
* **the interim boundary crossings that no undo can un-emit** — §6.1.

## What no undo can un-emit (compensation is not inversion)

Undoing the *code* of the interim generations does not un-emit what they *did*
while they were live. Every generation strictly after the target, up to and
including the current one, could reach the boundary; the union of their G8
crossing surfaces is exposure the revert cannot reverse. The dossier enumerates
it:

* `crossings` — every crossing the interim generations could make;
* `givenUp` — reaches the target generation no longer has: **authority
  relinquished going forward, yet already exercised** and possibly observed
  downstream (a replica, a trigger, a webhook, a human);
* `persisting` — reaches the target keeps.

This is the same honesty the erase-report leads with (docs/erase-report.md,
paper §6.1): a `compensate` clause is a *second* boundary crossing chosen to
offset the first — it does not un-issue it. The report **enumerates** the
exposure so an operator can see exactly what left the system; it does not, and
cannot, undo it. `givenUp` is precisely the list an operator must handle out of
band when they revert.

## Related

* docs/persistence.md — the snapshot bundle a generation entry reuses (item 15).
* docs/apply.md — the plan/execute machinery an undo's dossier is computed with.
* docs/audit-diff.md — the G8 crossing surface the un-emittable set is drawn from.
* docs/erase-report.md — compensation-is-not-inversion, in full.
* `revl_rollback` — the depth-1 predecessor this deepens; still available.
