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

Adding authority is the only dangerous direction, so **only unacknowledged
additions fail**. Giving up authority (a removal) never needs a gate. Exit code
is `0` iff there are no unacknowledged additions.

## Acknowledging an intended widening

A widening is sometimes intended. Two ack paths, chosen to be the smallest
thing that works:

- `--accept <crossing>` (repeatable) — acknowledge one addition by its token.
  The token is exactly the string printed after the `+` in the report, so the
  ack list is copy-paste from the failure output.
- `--accept-all` — acknowledge every addition in this diff.

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
`added` / `removed` / `unchanged` / `acknowledged` / `unacknowledged` /
`widened` decision object instead of the human report.

## Capturing a baseline

The previous audit is just `revl audit --json` output captured to a file:

    revl audit app.rvl --json > last-audit.json

Commit that file (or store it beside the generation artifact) and diff each new
generation against it.
