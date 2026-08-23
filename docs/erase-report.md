# `revl erase-report` — compliance as a compiler artifact

```
revl erase-report <files…> --realm <r> [--json] [--no-residue-proof]
```

A right-to-erasure request asks an operator to prove a tenant's data is gone.
Today that proof is a screenshot, a runbook, and a promise. `revl erase-report`
produces it as a *compiler artifact*: a versioned, auditor-facing document that
composes three guarantees the toolchain already establishes separately, scoped
to a single realm.

Nothing here is new machinery. Realms isolate tenants (G2), the withdrawal
query answers "does erasing this touch the other tenant" exactly, the runtime
lifecycle proves no residue on teardown (R4), and the G8 boundary surface
enumerates every crossing with its compensation status. This command is the
report generator that puts the three side by side and states, in its own
header, exactly what they do and do not establish.

## What it proves

**[1] In-process state gone — the R4 no-residue proof.**
Booting the composition on the cordis runtime and tearing it down returns the
registry, the provision store, the effect disposables and the event listeners
all to baseline (`revl.mcp.session.Session.unload`). The realm's components are
torn down as part of that proof, and the provisions they served into the realm
— the in-process state an erasure eliminates — are enumerated in the report. If
the runtime is unavailable (or `--no-residue-proof` is passed), this section is
reported as *not run* and the static sections still render.

**[2] Boundary crossings, compensated vs bare.**
Every emission call site and reached host extern the realm's components make,
read from the same G8 boundary surface `revl audit` prints
(`revl.query.Composition`). Each crossing is tagged:

* **compensated** — a `compensate` clause is attached; a second boundary
  crossing was issued to offset the first;
* **bare** — nothing was done about it.

A realm that made no irreversible crossing at all reports zero crossings
("fully revertible, G8").

**[3] Other realms provably untouched — the `survivors` set.**
Withdrawing the realm's components (`revl.query.withdrawal`, EXACT precision)
cannot orphan a consumer in another realm: G2 makes each `(key, realm)`
provision unique, so a realm's components have no cross-realm consumers, and G3
makes the withdrawal cascade terminate. The report checks that every component
outside the realm is a `survivor` — one that keeps every provision — and flags
any `breached` component (there can be none for realm-isolated provisions; the
check is there to make the guarantee visible, not to hope for it).

## What it explicitly does NOT prove

The report states this in its own header, because the gap is the whole point.

**Compensation is not inversion (paper §6.1; docs/replay.md §4.2).** A
`compensate` clause is a second boundary crossing *chosen to offset* the first —
`emit db.execute("INSERT …") compensate db.execute("DELETE …")` issues a
delete; it does not un-issue the insert. Anything downstream that already
observed the crossing — a replica, a trigger, a webhook, a human — has already
observed it. This report **enumerates** that exposure so an auditor can see
exactly what left the system and whether anything was done about it. It does
**not**, and cannot, undo it.

**External erasure is out of scope.** The state-gone proof (§1) is about
in-process runtime state only. Data that already crossed the boundary is
outside this system and outside this proof. A **bare** crossing (§2) left the
system with nothing done about it and must be handled out of band; the report
lists it precisely so it can be.

## Auditor framing

Read the three sections as one claim with an honest boundary:

> The realm's in-process state can be provably eliminated (R4), doing so cannot
> affect any other tenant (G2 / `survivors`), and here is the complete list of
> what this tenant's components ever sent outside the system — with, for each,
> whether a compensating action was issued. The list of **bare** crossings is
> the exposure that erasing in-process state does not reach.

That is a stronger and more honest artifact than "we deleted the data": it
separates what the type system can prove (in-process erasure, tenant
isolation, the exhaustive crossing list) from what no type system can
(un-observing an emission), and it never lets the second hide behind the first.

## Output

The default rendering leads with the honest-scope header, then the three
numbered sections. `--json` emits a versioned, self-describing document
(`kind: "revl.erase-report"`, `schema_version: "1.0"`) in the additive-only
spirit of the interchange format (docs/interchange-format.md) — a consumer can
gate on the MAJOR version and ignore members it does not recognise.

Exit code: `0` for a clean report; `1` for an unknown realm, an unproven
teardown (residue left), or a breached other realm. A **bare crossing does not
fail** the command — it is enumerated by design, not treated as an error.

## Related

* `revl audit` — the per-component G8 boundary surface this reads (§2).
* `revl query withdraw <c>` — the cascade and `survivors` set this reads (§3).
* `revl_unload` (MCP) / `revl run` teardown — the R4 no-residue proof (§1).
* docs/replay.md §4.2 — compensation is not inversion, in full.
