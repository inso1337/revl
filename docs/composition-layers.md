# Composition layers

A LAYER is a patch over a composition's [row table](composition-rows.md). It
says what to change, not what the result is: four operations over rows that have
names, checked before anything compiles.

This is roadmap item 426, slice S2, and the design note is
[design/426-composition-layers.md](design/426-composition-layers.md) §2 and §3.
Slice S1 built the object a layer patches; this slice builds the patch.

The property worth stating first, because it is the reason the shape is what it
is: **no provider is ever chosen by precedence.** Two third-party layers that
fight over one key refuse, loudly, naming both. Only the operator resolves, and
only by naming both sides. A composition whose database changed because someone
reordered a list is the failure this slice exists to make impossible.

## The document

```revl
layer PgSwap for Demo {
  touches key("db"), @metrics

  replace key("db") with row @db from "pg_database.rvl" provides db
    config { url: "postgres://primary:5432/app" }

  add row @metrics from "metrics.rvl" provides metrics
}
```

A layer names the composition it patches, and the name is checked rather than
assumed. **A layer document contains ONLY layer operations** — no `component`,
no `service`, no `extern`, no top-level `fn`. Without that rule a layer is a
component-authoring surface, and the confinement profile would have to run over
the layer document itself.

The base composition names its layers, in order:

```revl
composition Demo {
  use "services.rvl"
  row @db from "db.rvl" provides db
    config { url: "sqlite://local" }

  stack "layers/pg_swap.rvl"
  stack "layers/observability.rvl"
  site  "layers/ops.rvl"
}
```

Fold it:

```bash
revl layer check base.rvl         # the folded ROWS and WIRING, with provenance
revl layer check base.rvl --json  # the folded row table
revl composition base.rvl         # the same fold; --admit compiles the result
revl composition base.rvl --set '@db.pool=32'    # the invocation overlay
```

Folding is HEADER-ONLY, exactly as resolution is: every address resolves, every
claim is checked and the whole wiring renders without lowering one component
body.

## The four levels

| Rank | Level | Who writes it | Peer semantics |
|---|---|---|---|
| 0 | the base composition | the composition's owner | not a patch level |
| 1 | `stack` layers | third parties | **peers: conflicts refuse** |
| 2 | the `site` layer | the operator. Exactly one. | resolves, by naming both sides |
| 3 | the invocation overlay (`--set`) | the command line | values only, never structure |

There is one site layer and not two, because a precedence order between "the
project's patch" and "the machine's patch" is precisely a precedence rule that
chooses a provider. Two operator-owned files merge under the same peer rules,
and a conflict between them refuses.

## The four operations

| operation | meaning |
|---|---|
| `add row @label from "..." provides key` | introduce a row |
| `remove <address>` | withdraw a row |
| `replace <address> with row ...` | swap the implementation behind a row. **The replacement must claim exactly the same set.** The label is preserved. |
| `configure <address> with { field: value }` | merge fields into a row's config |

**There is no positional operation.** No `insert before`, no priority, no
ordering. Load order is derived by Kahn over the dependency graph, not declared,
so a position operation would invent a concept the gate does not have.

**`replace` is claim-preserving**, and that is a determinism lever, not a
restriction for its own sake: a replacement claiming exactly what it replaced can
never create or destroy a provision conflict. Changing what a row claims is
expressible — as `remove` plus `add`, which is loud in the diff.

**`configure` is typed.** A field the component does not declare, a value that
does not fit the declared type, and a `configure` against a row whose component
declares no config at all are three refusals. A config typo is the most common
way a layered composition breaks in practice, and here it never reaches a run.

## Addressing: two spellings, both exact

```revl sketch
key("db")                     -- whatever row currently claims `db`
key("kv", realm: "tenant_a")  -- per-realm, so two tenants never collide
@db                           -- that exact row, in this layer's own origin
acme_pg::@db                  -- that exact row, fully qualified
.::@db                        -- a row the project's own composition declared
```

| Spelling | Survives a label rename | Survives a re-provision |
|---|---|---|
| `key("db")` | yes | no — refuses if the key moved rows |
| `@db` / `acme_pg::@db` | no — refuses, loudly | yes |

Both are useful and the choice is a choice about failure mode. A third-party
layer overriding a contract writes `key("db")`, because it wants to follow the
contract wherever it moved. A site layer naming exactly the row it means writes
the qualified label, because it wants to hear about it if that row is gone.

**An address that resolves to nothing is a REFUSAL, never a no-op.** This is the
sharpest single difference from a patch system where a vanished target silently
does nothing and the operator finds out at runtime:

```text
error: layers/obs.rvl:2: layer `Obs` addresses key("logger"), which no row
  claims in composition Demo
  row `.::@log` claims key("logging"). Address the row directly by its label if
  you mean that exact row, or repin the source that dropped the key (426 §2.4)
```

## `configure @db with { ... }`, and why not `configure @db { ... }`

`@db {` is not two tokens. The lexer turns an `@` identifier followed by a brace
into a verbatim HOST BODY (`lexer.py:422-446`), which is how `@py { ... }` works,
so `configure @db { url: "..." }` would hand the parser one opaque blob. The
alternatives were to teach the lexer a composition context or to change the
shape. The shape changed, because `with { ... }` is not a new spelling to learn:
`spawn C with { ... }` and `intercept kv with { ... }` already read that way, and
the lexer stays context-free — which also keeps the self-hosted lexer in sync for
free. Writing the brace form is a refusal that names the fix.

`::` is likewise not a token: it reaches the parser as two `:` pieces, so no
operator table changes either. The formatter renders it tight.

## Peer conflicts

| situation | outcome |
|---|---|
| two stack layers `add` rows with intersecting claims | **REFUSED** |
| two stack layers `replace` the same row | **REFUSED** |
| two stack layers `configure` the same row and field, different values | **REFUSED** |
| two stack layers `configure` the same row, disjoint fields | merged, commutative |
| two stack layers `remove` the same row | idempotent, allowed |
| one stack layer `remove`s, another `replace`s or `configure`s the same row | **REFUSED** |
| the site layer does any of the above over a stack layer | the site layer wins, no refusal |

A refusal carries LAYER PROVENANCE, which is the whole reason the fold pre-checks
at all. The linker's own message names two components, and after layering that is
useless because the operator wrote neither of them:

```text
error: layers/acme_pg.rvl:2: layer conflict on key("db"): stack layers
  `AcmePg` and `CorpSqlite` both add a row claiming key("db")
  neither layer is preferred (426 §3.4) — precedence never chooses a provider
  (decision 4). Only the operator's site layer decides, and it decides by
  naming what it means:
  resolve key("db") to .::@db_acme over .::@db_corp
  (this is G2, provision disjointness, seen at the layer level)
```

Both the message and the file and line it is reported at are derived from the
layer names sorted, never from the order the stack listed them, so permuting the
stack changes nothing at all.

## The site layer resolves

```revl
site layer Ops for Demo {
  resolve key("db") to .::@db_acme over .::@db_corp
  configure @db_acme with { pool: 32 }
}
```

`resolve` names both sides. It is a site-layer operation and writing it in a
stack layer is a refusal: refusal is only meaningful BETWEEN peers, and the
operator is not a peer of the layers — the operator is the person the refusal is
shown to. So there is exactly one level at which "I decide" is expressible, and
it is the level a human owns.

A `resolve` that decides nothing is also a refusal. An operator who wrote one
believed there was a conflict; if there is not, that belief is worth correcting.

## `touches`

An optional declaration of a layer's reach:

```revl sketch
touches key("db"), @metrics
```

It is enforced — a layer whose operations address something outside its own
`touches` is refused — so it is checkable rather than decorative. It is **not** a
security property: an author who wants to touch a row simply lists it. What it
buys is that a layer's reach can be read off its head without resolving it.

## `granted` is never in a stack layer

`granted { ... }` is the reach allowlist a confined row may compose against
([composition rows](composition-rows.md#granted)). It is writable in the base
composition and in the site layer, and **a stack layer that writes one is
refused**: a layer granting itself keys is a layer raising its own authority.

This is the third of the clause's three rules, and it is the one that needed
layers to exist before it could be enforced.

## The fold, and why the gate is never inside it

Resolution is a pure fold:

1. Start from the base row table.
2. Apply the stack layers. Peer conflicts refuse, so the result does not depend
   on the order they were listed in.
3. Apply the site layer, which may resolve a level-1 refusal.
4. Apply the invocation overlay: values only.
5. Record, per row, the ordered provenance — every `(level, layer, op)` that
   touched it.

```text
ROWS
  .::@db                   PgDatabase  (pg_database.rvl)
                             row by `<base>` (L0) -> replace by `PgSwap` (L1)
  .::@metrics              Metrics  (metrics.rvl)
                             add by `PgSwap` (L1) -> resolve by `Ops` (L2)
```

Two things follow, and both are load-bearing.

**Withdrawals are applied before admissions.** Admitting operation by operation
opens a determinism trap: `remove key("logger")` followed by `add @logger`
succeeds while the reverse order refuses on provision disjointness at an
intermediate state, so the verdict would depend on the order the operations
happened to be listed in. Folding first means the intermediate state where both
rows claim `logger` never exists, and the disjointness check runs exactly once,
over the final table.

**The fold is not on the trusted path.** `_link` still runs G2 and G3 unchanged
over the assembled composition. The fold is a pre-check whose only privilege is
to produce a better message, so a bug in it cannot admit a composition the linker
would refuse — it can only over-refuse, which is a usability bug and not a
soundness one.

Every input is an ordered list in a file. Nothing here depends on filesystem
iteration order, directory listing order, wall-clock time, or fetch completion
order.

## What is not here yet

| next | what it adds | what it waits on |
|---|---|---|
| incremental admission | admitting the resolved delta through `admit_into` with a `replacing` withdrawal set, so the cost is one compile of the patched rows | this slice only |
| confinement | non-first-party rows compiled under the untrusted-author profile, and the per-root profile split in `compile_files` | roadmap 425 F1's decision |
| the authority panel | crossing tokens re-keyed by row label, a `config:` token carrying a value digest, a fail-closed headline, a printed blind-spots block | confinement, and roadmap 428 F3 |
| distribution | a layer is a truc, the `[trucs]` origin namespace becomes real, the pin becomes mandatory | roadmap 428 F3 |

Until distribution lands, **layers are files in a directory**: `stack` and `site`
name paths, and the origin of a layer's rows is still read off where the document
lives, never written by the document.

`open` (which fields a stack layer may configure) and `reach` (the
composition-level authority bound) are still not grammar, and writing one is a
parse error rather than a silently ignored clause. They arrive with the authority
panel, which is the slice that gives them meaning.

Activation is unchanged: applying a layer that only ADDS rows is a pure
extension, and anything with a `remove` or a `replace` in it is a new generation.
That is a property of G7, not of effort.
