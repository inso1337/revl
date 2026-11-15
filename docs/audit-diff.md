# `revl audit --diff` — the authority-drift gate

`revl audit` reports the **G8 boundary surface**: the enumerable set of
boundary crossings a composition makes — every emission call site and every
host extern each component reaches. `revl audit --json` emits that surface as
a machine-readable manifest.

`revl audit --diff <previous-audit.json>` turns the surface into a **gate**. It
re-audits the current sources, loads a previously captured audit, and fails
(nonzero exit) when the new generation has **added** boundary crossings that
were not present before — unless the additions are explicitly acknowledged.

## Authority vs. admission

The agent-gate story has two orthogonal axes, and this gate is deliberately
only one of them:

- **Admission** checks *correctness*: that a regenerated component still
  satisfies its running consumers — the contracts callers already depend on
  stay valid.
- **Audit-diff** checks *authority*: that a regenerated component has not
  quietly **widened what it reaches outside the system** between generations.

A regeneration can be perfectly admissible — every consumer still type-checks,
every contract still holds — and still be dangerous, because it now emits to a
new service or reaches a new piece of host code it never touched before.
Admission would wave that through; audit-diff catches it.

## What counts as a crossing

A *crossing* is one reach in the per-component G8 boundary table, identified by
a stable token:

    emit:<component>:<service.method>   an emission the component performs
    host:<component>:<extern-name>      host code the component reaches
    secret:<capability>:<name>          a secret bound to a capability (item 256)
    env:<name>:<bound>                  a host-injected environment value (item 350)

The same reach yields the same token across generations, so the whole diff is
a set difference over these tokens.

The `secret:` token is a composition-level crossing, not a per-component one: it
comes from the audit's top-level `secrets` table (roadmap item 256, the
capability-bound-secrets note §5), which carries a secret's **name and
capability only**, never its value, and no length, hash, or timing. Its drift
semantics follow the same rule as every other crossing:

- **Binding a new secret** adds a `secret:<cap>:<name>` token, a widening the
  gate flags (a new key is a new authority fact on the boundary surface).
- **Rebinding a secret to a wider capability** changes the token (the capability
  is part of it), so the new binding appears as an addition (widening) while the
  old one appears as a removal (narrowing).
- **Removing a secret binding** drops the token, a narrowing, always safe.

The `env:` token (item 350) is composition-level too, drawn from the top-level
`env` table — the `boot` component's config block, which is the environment
contract the host must inject. The token carries the field's declared BOUND, so
three distinct authority changes all land as an addition the gate flags as a
widening: a new contract field, a bound REMOVED (`env:data_dir:under=./data`
becomes `env:data_dir:*`), or a bound LOOSENED. Removing a field or tightening a
bound drops the old token: a narrowing. `*` means UNBOUNDED — the host may
inject any value of the declared type. The token names the field and the
author-written bound; a VALUE is never in the audit at all (values arrive at
`revl run --env` time and never enter the IR). See
[environment-binding.md](environment-binding.md).

## The exit-code contract

| Bucket      | Meaning                                   | Effect       |
|-------------|-------------------------------------------|--------------|
| `added`     | crossing in NEW but not PREV — a widening  | fails (exit 1) unless acknowledged |
| `removed`   | crossing in PREV but not NEW — narrowing   | always passes |
| `unchanged` | crossing in both                           | always passes |
| `inverse_weakened` | the derived `inverse` class of a crossing moved to a weaker one (defaulted to `unknown`) | fails (exit 1) unless acknowledged |
| `inverse_strengthened` | the class moved to a stronger one | always passes |
| `reach_weakened` | a stable extern's reach lost or moved its bound (`reach-weakened:<extern>`) | fails (exit 1) unless acknowledged |
| `reach_tightened` | the reach gained a bound | always passes |
| `scope_widened` | a stable emission's capability scope covers more (`scope:<component>:<label>`), e.g. `[db]` to unscoped | fails (exit 1) unless acknowledged |
| `scope_tightened` | the scope covers less | always passes |
| `backends_added` | an extern gained a host backend body (`backend:<extern>:<backend>`), e.g. a `@ts` body beside `@py` | fails (exit 1) unless acknowledged |
| `backends_removed` | an extern gave up a backend body | always passes |
| `recovery_dropped` | a recovery entry is gone and no `inverse:<name>` token carries it as a kind move (`recovery:<name>:<kind>`) | fails (exit 1) unless acknowledged |
| `recovery_weakened` | a recovery entry's idempotency register dropped, on a kept entry or across a kind move | fails (exit 1) unless acknowledged |
| `recovery_added` | a recovery entry appeared | always passes |
| `registers_weakened` | a capability's idempotency floor dropped or was removed (`register:<token>`) | fails (exit 1) unless acknowledged |
| `registers_strengthened` | a floor rose or was added | always passes |
| `cardinality_widened` | a per-capability emission ceiling rose (`cardinality:<component>:<token>`) | fails (exit 1) unless acknowledged |
| `cardinality_tightened` | the ceiling fell | always passes |

Adding authority is the only dangerous direction, so **only unacknowledged
additions, weakenings and widenings fail**. Giving up authority (a removal, a
tightened reach or scope, a strengthened inverse, a dropped backend) never
needs a gate. Exit code is `0` iff there are no unacknowledged gated tokens.

The gated set is `audit_diff.GATED_BUCKETS`, read through
`audit_diff.widening_from_audits`. The MCP update path's acknowledgement
decision reads the same function, so the gate and the update path cannot
disagree about what widened (issue #3182). A recovery whose kind moves on a
kept name (`compensation` to `inverse`, or back) is not a dropped recovery:
the direction of that move is the inverse-provenance diff's, so a weakening
is one `inverse:<name>` token and a strengthening passes. Every reader of the
recovery diff also reads that token: the gate (`inverse_weakened` is in
`GATED_BUCKETS`), `revl migrate` (`migrate.authority` lists `inverse:<name>`
under `widened`, a strengthening under `narrowed`) and the changelog (a
breaking `inverse.weakened:<name>` line, a strengthening as an added
`inverse.strengthened:<name>` line). migrate and the changelog report every
`inverse_weakened`, as the gate does, not only one that carries a kind move
(an effectful extern that becomes `pure` is breaking there too). A lost kind is
treated as a move only when the inverse diff actually emits that token and the
name still has a recovery entry. Otherwise it is still reported as dropped: a
recovery gone entirely (even when its class moved, as in `compensation` to
`irreversible`), an extern with both `undo` and `compensate` that loses one
while its class stays put, or a kind change against an older baseline without
`inverse` (the inverse diff skips that extern). Across a move the register is
still compared: a new-kind entry whose register is weaker than the lost kind's
(for example `compensation` with `keyed` to `inverse` with none) is reported
as `recovery_weakened` on the new entry, not as a gained recovery.

## Inverse provenance drift

Issue #2692 adds one derived field to the audit: every crossing carries
`inverse`, a class from the closed vocabulary `declared`, `generated`,
`compensation`, `forward`, `irreversible`, `unknown` (the meanings and the rule
that nothing is inferred from a name are in
[interchange-format.md](interchange-format.md#the-inverse-class)).

A class change is drift on the authority axis even when the crossing token is
unchanged: the component still reaches the same extern, but the way that
crossing can be undone may have weakened. The gate orders the vocabulary by
recovery strength (`declared`, `generated`, `compensation`, `forward`,
`irreversible`, `unknown`) and reports a move toward the end of that order as a
weakening. A best-effort offset ranks above forward-only recovery because it
attempts a restoration; forward-only ranks above a bare emission because it at
least carries a re-issue claim.

| change (examples) | bucket |
| --- | --- |
| `declared` → `compensation` (an exact inverse became a best-effort offset) | `inverse_weakened` |
| `compensation` → `forward` (the offset became forward-only recovery) | `inverse_weakened` |
| `compensation` → `irreversible` (the offset was dropped) | `inverse_weakened` |
| `forward` → `irreversible` (the re-issue claim was dropped) | `inverse_weakened` |
| `unknown` → anything known; `irreversible` → `forward`; `forward` → `compensation`; `compensation` → `declared` | `inverse_strengthened` (safe) |

Each weakening is reported as the token `inverse:<extern-name>` and is
acknowledged through the same `--accept`/`--accept-all` path as an added
crossing. Only externs present in BOTH audits are compared: a brand-new extern
is already an added `host:` crossing, and a removed one gave up its reach
entirely. An extern whose entry has no `inverse` on either side is not
comparable and is skipped, so a baseline captured before this field existed
produces neither a false weakening nor a false strengthening.

## Acknowledging an intended widening

A widening is sometimes intended. Two ack paths, chosen to be the smallest
thing that works:

- `--accept <crossing>` (repeatable): acknowledge one gated token (an
  addition, or a weakened or widened fact such as `backend:x:ts`).
  The token is exactly the string printed after the `+` in the report, so the
  ack list is copy-paste from the failure output.
- `--accept-all`: acknowledge every gated token in this diff (additions and
  every weakened or widened fact).

An acknowledged addition is reported (as `~`, `acknowledged`) but does not fail
the gate.

## Examples

Clean gate (boundary unchanged):

    revl audit app.rvl --diff last-audit.json
    # authority-drift: clean — the G8 boundary surface is unchanged from last-audit.json
    # exit 0

A regeneration that reaches a new emission:

    revl audit app.rvl --diff last-audit.json
    # authority-drift: 1 new boundary crossing(s) added since last-audit.json:
    #
    #   + emit:Front:cache.put
    #
    # These WIDEN what the composition reaches outside the system.
    # Acknowledge an intended widening with --accept <crossing> (repeatable) or --accept-all.
    # exit 1

Accepting that widening on purpose:

    revl audit app.rvl --diff last-audit.json --accept emit:Front:cache.put
    # exit 0

`--json` stays composable — `revl audit ... --diff PREV.json --json` prints the
`added` / `removed` / `unchanged` / `reach_weakened` / `reach_tightened` /
`inverse_weakened` / `inverse_strengthened` / `scope_widened` /
`scope_tightened` / `backends_added` / `backends_removed` /
`recovery_dropped` / `recovery_weakened` / `recovery_added` /
`registers_weakened` / `registers_strengthened` / `cardinality_widened` /
`cardinality_tightened` / `acknowledged` / `unacknowledged` / `widened`
decision object instead of the human report.

## Capturing a baseline

The previous audit is just `revl audit --json` output captured to a file:

    revl audit app.rvl --json > last-audit.json

Commit that file (or store it beside the generation artifact) and diff each new
generation against it.
