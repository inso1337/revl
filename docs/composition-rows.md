# Composition rows

A composition is a set of ROWS. A row is one component placed into one
composition, and it carries a name of its own.

Before this, a composition was a flat list of file paths handed to
`compile_files`, swapped whole. That list has no identity: there is no object to
address, so nothing can be said about "the database row" except by naming the
file it happens to live in or the component it happens to contain, and both of
those are things upstream may change without changing what the row MEANS. A row
gives the thing a name that survives both.

This is roadmap item 426, slice S1. The design note is
[design/426-composition-layers.md](design/426-composition-layers.md); it decides
more than this slice builds, and the last section here says what is still to
come and what waits on what.

## The document

```revl
composition Demo {
  use "services.rvl"

  row @db from "db.rvl" provides db
    config { url: "postgres://primary:5432/app", pool_size: 8 }
  row @cache from "cache.rvl" provides cache
  row @routes from "routes.rvl" provides nothing
}
```

Resolve it:

```bash
revl composition base.rvl            # the ROWS and WIRING panels
revl composition base.rvl --json     # the row table
revl composition base.rvl --admit    # also compile the rows it names
```

Resolution is HEADER-ONLY. Every row's source is parsed and its `component`
declaration's header read; no component body is lowered. So every id resolves,
the whole wiring renders, and every check below fires without compiling
anything. `--admit` is what runs the compile, and that is where `_link` runs G2
and G3 as it always has.

## What a row carries, and which part is its identity

| field | role |
|---|---|
| `label` | IDENTITY. Declared, stable, scoped to the declaring document's origin. |
| `claims` | the CONTRACT: the `(key, realm)` pairs the row provides. Checked against the component header. |
| `component` | PROVENANCE. Never identity. |
| `config` | data, checked against the component's declared `config` types. |
| `requires` | what the row consumes, read from the header. |
| `granted` | the reach allowlist a confined row may compose against. |

The label is the identity, and the choice matters in both directions:

**A component renamed upstream is a non-event.** The row keeps its label, the
wiring projection is byte-identical, and nothing that addressed the row breaks.

**A provision ADDED upstream keeps the label too, and is reported.** A minor
version that grows a component's surface would silently rename a row whose
identity was its claim set, and everything written against the old id would fail
closed. Under a label it does not: the row is the same row, and the addition
prints as a claim the document did not assert.

**A provision REMOVED upstream is a refusal.** The document asserted `provides
db`; the component no longer provides it. The refusal names the row, the lost
key and the source that dropped it, instead of surfacing later as an unmet
requirement naming a component the operator never wrote.

```text
row `@db` asserts `provides db`, but component `PgDatabase` in `db.rvl`
provides `store`
```

## Origins: `<origin>::@<label>`

A label is scoped to the origin of the document that declares it. An origin is
either the project itself, spelled `.`, or a truc key from `truc.toml`'s
`[trucs]` table, which is also the vendor directory name and the lock row key.

So `.::@db` and `acme_pg::@db` are two different rows with the same bare label
and there is nothing to arbitrate. There is no registry of labels and no
squatting policy to write, because the namespace is the one the operator already
owns: they choose the `[trucs]` keys, and `.` is theirs and unmintable by anyone
else. The origin is read off where the document lives, so a document cannot
declare its own origin.

Two labels with the same spelling in ONE origin is a refusal, and it fires in
the parser.

## The claim assertion

`provides ...` on a row is an assertion, checked against the component's header.
A row that claims nothing writes `provides nothing`, which under a label scheme
is the ordinary case rather than a special one: a sink row has a label like
every other row.

Because the assertion is required and checked, the document cannot lie about the
wiring. This is the bug class it closes: the revl-harness composition is a
`List[Str]` file list plus a JSON config string, and nothing checks that the
component names in the config correspond to any component in the file list. Two
measured instances are on record, a composition listing 31 components while a
transitively used one was missing, and a console panel printing nine names while
the live boot served thirty-one keys. Both are compile errors here.

The assertion may be a strict subset of what the component provides. That is the
"added upstream" case, and it is reported rather than refused.

## Realms

The claim set is `(key, realm)`, not `key`. Two rows may claim the same key in
different realms, which is the sanctioned multi-provider shape: a per-tenant
store providing `kv` in `realm("tenant_a")` and another in `realm("tenant_b")`
resolve side by side. The realm is read from the `isolate` statement in the
component's source, which resolution reads out of the parse tree without
lowering the body.

Two rows claiming the same `(key, realm)` pair is a refusal naming both ROWS:

```text
key("db") is claimed by both row `.::@db` (component `PgDatabase`) and row
`.::@db2` (component `OtherDatabase`) in composition Demo
```

That is G2, provision disjointness, seen one level up. `_link` still runs G2
unchanged over the compiled result; this check exists only to name rows rather
than components, which is what an operator can act on. The resolver is not on
the trusted path, so a bug in it can only refuse something admissible, never
admit something the linker would refuse.

## Config

`config { field: value }` on a row supplies constants for the component's own
typed `config { field: T = default }` block, and it is checked:

- a field the component does not declare is a refusal listing the ones it does;
- a value that does not fit the declared type is a refusal naming the field and
  the type;
- a field with no default that the composition does not supply is a refusal.

A config typo is the most common way a layered composition breaks in practice.
Here it is a refusal before anything is compiled, not a runtime surprise.

## `granted`

`granted { ... }` names the services a CONFINED row may compose against. It is
the argument `AdmissionProfile.untrusted_author(granted)` takes, and something
has to produce it.

```revl
composition Observed {
  row @otel from "trucs/otel_kit/component.rvl" provides metrics
    granted { clock, metrics_sink }
}
```

Three rules:

- it defaults to EMPTY, never to "everything the row requires", so a row cannot
  grant itself authority by needing it;
- a row whose `requires` is not a subset of its `granted` is refused at
  resolution, naming the ungranted key;
- it is writable only by the composition's owner and the operator, never by a
  third-party layer, for the same reason no layer may raise its own trust class.

A row that writes no `granted` clause at all is unconfined: it is the project's
own code, and confining it would be wrong. Wiring the untrusted-author profile
per row is the confinement slice, and it waits on the trust decision recorded in
roadmap 425 F1. Until then the clause and its subset check are enforced and the
profile is not, which is exactly the split roadmap item 424 slice A1 states.

## What lands in the IR

`revl composition --admit` (and `revl.composition.compile_composition`) put the
row table on the IR document as `rows`, and on the manifest as `manifest.rows`.
Every path in it is relative to the project root and no absolute path appears, so
two machines resolving the same composition produce a byte-identical table.

```json
{
  "composition": "Demo",
  "origin": ".",
  "source": "base.rvl",
  "rows": [
    {
      "label": "db",
      "origin": ".",
      "qualified": ".::@db",
      "source": "db.rvl",
      "component": "PgDatabase",
      "claims": [{"key": "db"}],
      "requires": [],
      "config": {"url": "postgres://primary:5432/app", "pool_size": 8}
    }
  ]
}
```

A declared composition is compiled and READ: its rows are already in the IR. The
bootstrap that today compiles a manifest document, emits it to Python, execs it,
calls it, and parses the JSON string it returns becomes "compile, read the
manifest" ([composition-bootstrap.md](composition-bootstrap.md)).

## What is not here yet

The row table is the object everything else in item 426 needs, and it is
deliberately the whole of this slice.

| next | what it adds | what it waits on |
|---|---|---|
| the fold | layers and the four operations (`add`, `remove`, `replace`, `configure`), the four levels, peer conflicts that refuse with layer provenance, address resolution by `key(...)` or by qualified label | the row table only |
| incremental admission | admitting a resolved delta through `admit_into` with a `replacing` withdrawal set, so the cost is one compile of the patched rows | the fold |
| confinement | non-first-party rows compiled under the untrusted-author profile, and the per-root profile split in `compile_files` that makes a mixed-trust delta expressible in one call | roadmap 425 F1's decision |
| the authority panel | crossing tokens re-keyed by row label, a `config:` token carrying a value digest, a fail-closed headline, and a printed blind-spots block | confinement, and roadmap 428 F3 |
| distribution | a layer is a truc, the `[trucs]` origin namespace becomes real, the pin becomes mandatory | roadmap 428 F3 |

Two surface clauses the design defines are therefore not grammar yet, and
writing one is a parse error rather than a silently ignored clause: `open`
(which fields a third-party layer may configure) and `reach` (the
composition-level authority bound). `place` and `variant` are the same. Each
arrives with the slice that gives it meaning, because a clause that parses and
does nothing is worse than one that refuses.

Activation is unchanged and stays whole-generation for anything but a pure
addition. That is a property of G7, not of effort: a withdrawn component's fiber
must be disposed with its teardown in the correct LIFO position and every
consumer re-resolved, which the partial-link path deliberately refuses.
