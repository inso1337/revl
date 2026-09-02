# 426: composition rows, layered patches, admitted extension (truc-first)

Design note for roadmap item 426, the resolution to item 424(a). Design only:
no compiler change, no `src/` change, nothing implemented. This note takes one
framing and commits to it.

**The framing.** truc is the unit of distribution and identity, so layering is
a truc concern. A bundle is a truc package. Identity lives in `truc.lock`.
`truc assemble` is where layers resolve and where re-admission happens. The
design adds no second resolution mechanism, no second identity scheme, and no
second gate. Where the answer could be "invent a composition-level construct"
or "extend truc's existing model", this note takes the second every time.

**The one-line thesis.** A composition is a table of rows; a row is one
component, addressed through the truc that shipped it and pinned by the lock;
a bundle is a truc that ships patch operations instead of a component; layers
fold deterministically to a row table with no gate involvement; and the delta
between the running row table and the resolved one is admitted in a single
`compile_files(newRows, manifest=running, replacing=withdrawn)` call, which is
the shipped hot-swap primitive, unchanged.

**What is new here, mechanically, is almost nothing.** That is the point. The
row is the component the gate already replaces by name. The identity is the
lock row truc already writes. The delta admission is the `replacing` parameter
`compile_files` already takes (`src/revl/compiler.py:379`, "a compiled
component whose name matches a running one implicitly replaces it"; `replacing`
names additional components withdrawn in the same admission). The authority
diff is `audit_diff.diff_crossings` plus `composition_diff.diff`, both shipped.
What this note contributes is the row table, the layer fold, the patch
grammar, the confinement rule that makes the authority diff honest, and the
statement of which admission rules are incrementally recomputable and why.

---

## 1. Row identity

### 1.1 What a row is

A **row** is one component in the assembled composition, together with the
truc that shipped it. Nothing else is a row. Not a file, not a service, not a
provision key, not a config entry.

A row's **canonical id** is:

    <truc>/<Component>

where `<truc>` is a key in `truc.toml`'s `[trucs]` table (which is also the
vendor directory name under `trucs/` and the lock row key), and `<Component>`
is the component's declared name. The project's own sources are the reserved
pseudo-truc `.`, so a first-party component reads `./App`. Synthesized config
providers (section 3.4) live under the reserved pseudo-truc `config`, so
`config/cfg`.

Examples: `./App`, `pg_database/PgDatabase`, `otel_logger/OtelLogger`,
`config/sampling`.

The id is a **coordinate**, not a content property. It names a slot in the
assembly. The lock row is the separate, independent proof of what currently
sits in that slot. Keeping those two apart is what makes the three survival
questions have clean answers.

### 1.2 Survival: a source edit

The id does not change; the `sourceHash` in the lock does. Identity and
integrity are different jobs and different fields. A patch written against
`pg_database/PgDatabase` still addresses the same row after upstream edits the
source, and the changed hash is what the drift check refuses if the vendored
bytes were edited without the lock being regenerated.

This is only true if every row is pinned. Today it is not: roadmap 428 F3
records that `plan_drift` (`src/revl/truc/components/planner.rvl:163`) skips
any vendored truc with a missing or blank `sourceHash`, and nothing requires a
truc named in `truc.toml` to carry a pin at all. A row with no pin has no
integrity anchor, and this design leans on the pin harder than anything in the
tree does today. Section 7 states this as the one blocking prerequisite.

### 1.3 Survival: a version bump

The id is version-free. The lock carries the version and the hashes. A patch
targeting `pg_database/PgDatabase` still resolves after `pg_database` bumps
from 1.4 to 2.0, provided the component name survived the bump. If it did not,
the address resolves to nothing.

**An address that resolves to nothing is a refusal, never a no-op.** This is
the single sharpest difference from DSH, where a patch whose target id vanished
silently does nothing and the operator learns at runtime. Here it is a
resolution-time refusal naming the address, the layer that wrote it, and what
the truc provides instead:

    truc: refused - patch from bundle `obs_kit` (stack position 1) addresses
    row `logger/StdLogger`, which does not exist in `logger` at the pinned
    version. `logger` provides key `logger` as component `Logger2`.
    Address it by key (`key: "logger"`) if you want the patch to follow a
    rename, or repin `logger` to a version that has `StdLogger`.

### 1.4 Two spellings, deliberately

A bundle author addresses a patch target in one of two ways, and the choice is
a choice about failure mode:

| Spelling | Resolves to | Survives a rename | Survives a re-provision |
|---|---|---|---|
| `row: "logger/StdLogger"` | that exact row | no (refuses, loudly) | yes |
| `key: "logger"` | whatever row currently provides key `logger` | yes | no (refuses if the key moved trucs) |

Key-addressing is what makes a patch survive a version bump. Row-addressing is
what makes it fail loudly when the thing it targeted is gone. Both are exact;
neither is a fuzzy match; the resolution record always prints which spelling
was used and what it resolved to, so a key-addressed patch that quietly
retargeted onto a different truc's component is visible rather than inferred.

Key-addressing is well defined because G2 guarantees at most one provider per
key. That is not a convenience, it is the reason the spelling can exist at all:
in a system where two components could fill one key, "the provider of `logger`"
would be ambiguous and the address would need a tiebreak rule. G2 removes the
question.

### 1.5 Rejected identities

- **Content hash as the id.** Does not survive an edit, which is the first
  requirement. A hash is the pin, never the address.
- **A declared `id` in source.** New syntax, and it makes identity a claim the
  package author writes rather than a coordinate the assembly owns. Two
  packages can then claim the same id, and the resolution needs a squatting
  policy. The `[trucs]` table key is already a per-project namespace with a
  natural owner (the person editing `truc.toml`), so it needs no policy.
- **Bare package name.** Insufficient the moment one truc ships two
  components, and it cannot address the project's own sources at all.
- **Bare component name.** Sufficient for the gate (which already replaces by
  bare name) but not for provenance: `Logger` from two different trucs is two
  different supply-chain facts, and the approval UX has to show which. The
  truc prefix is what carries that.

---

## 2. File or component: the row is a component

**Commit: a patch addresses a COMPONENT.** The file is the payload's shape, not
the address.

Five reasons, in descending order of weight.

1. **The gate's replacement primitive is already component-granular and there
   is no file-granular one.** `compile_files(paths, manifest=running,
   replacing=(...))` withdraws components by name. A file-addressed patch would
   have to be lowered to a component-addressed one before anything could admit
   it, which makes the file a lossy alias for the real address. Building the
   whole model on the alias means every refusal has to be translated back, and
   translation is where why-traces go to die.

2. **G2 is stated over provision keys, and keys belong to components.** A patch
   that replaces something has to answer "which key does the replacement fill".
   A component answers. A file containing three components does not.

3. **Every existing report is already a component-keyed table.**
   `audit_diff.crossings` keys per component. `composition_diff._components`
   keys per component and already renders sentences like "provider of key `db`
   changed from `PgDatabase` to `MysqlDatabase`". `registry._entry_index_row`
   folds `provides`/`requires` per component. The manifest's `components` list
   is a component table. Making the row a component means the row table is
   something four shipped subsystems already produce, and the authority diff of
   section 5 is a join over tables that exist rather than a new analysis.

4. **A file is a packaging fact and packaging is refactorable.** Moving a
   component from `a.rvl` to `b.rvl` changes nothing semantically. Under
   file-addressing it is a remove plus an add: the composition diff lies, every
   patch targeting it breaks, and the authority diff shows a spurious full
   turnover. Identity must not be destroyed by a refactor that changed nothing.

5. **The truc-first layout makes the distinction invisible in the common
   case.** A registry entry is one `component.rvl`
   (`registry/components/<name>/`), so a fetched truc is one file and, almost
   always, one component. File-addressing and component-addressing coincide
   wherever a third party is involved. They diverge only inside a project's
   own multi-component files, which is precisely where component-addressing is
   the answer you want.

**The three-level structure this settles.** A patch has a payload, an address,
and an effect, and they live at three different granularities:

| Level | Unit | Where it lives |
|---|---|---|
| Payload | a truc (a file plus `manifest.json` plus optional `dossier.json`) | `trucs/<name>/`, pinned in `truc.lock` |
| Address | a component (or a provision key) | the row table |
| Effect | a provision key filled, vacated, or reprovided | the gate |

DSH collapses all three into one config row, which is why its patches are
cheap to write and impossible to check. Keeping them separate is what buys the
checking.

**The concession, stated plainly.** DSH's row is a config value and revl's row
is a whole component, so DSH can express "change one number" with a one-line
patch and revl's equivalent is "replace the provider of a key". Section 3.4's
`set` operation is the answer, and it is genuinely more machinery than DSH
needs for the same effect. What it buys is that the change is typed and
admitted rather than substituted into an untyped dictionary. That trade is the
whole item.

---

## 3. Layers, operations, and resolution

### 3.1 The layers

Four layers, mirroring DSH's bundle / profile / home / CLI order, spelled in
truc terms:

| Rank | Layer | Source | Committed | Per-user |
|---|---|---|---|---|
| L0 | base | `truc.toml` `[assembly].entry` and `[trucs]` | yes | no |
| L1 | stack | each bundle named in `[stack].order`, in that order | yes | no |
| L2 | project patch | `truc.patch.json` at the project root | yes | no |
| L3 | home patch | `$TRUC_HOME/patch.json` (default `~/.truc/patch.json`) | no | yes |
| L4 | invocation | `truc assemble --replace`, `--set`, `--add`, `--remove` | no | no |

L0 is not a patch layer; it is the base row table the patch layers fold onto.

**A bundle is a truc.** It is fetched, hashed, vendored under `trucs/`, and
pinned in `truc.lock` exactly like a component truc. The only difference is
what its entry directory carries: `bundle.json` instead of, or beside,
`component.rvl`. That is the whole of "how do bundles interact with truc's
existing package identity": they do not interact with it, they *are* it. There
is no bundle registry, no bundle version scheme, no bundle hash, and no bundle
resolver, because truc already has all four and a bundle is a truc.

```toml
# truc.toml
[assembly]
name  = "myapp"
entry = ["src/main.rvl"]

[registries]
local = { path = "registry" }

[trucs]
pg_database = { registry = "local" }
std_logger  = { registry = "local" }

[stack]
order = ["obs_kit", "audit_kit"]     # L1, applied left to right
```

```json
// trucs/obs_kit/bundle.json
{
  "bundleVersion": 0,
  "name": "obs_kit",
  "touches": ["logger", "cfg.sampling"],
  "requiresTrucs": {
    "otel_logger": { "registry": "local", "sourceHash": "b782e70c..." }
  },
  "ops": [
    { "op": "add",     "truc": "otel_logger" },
    { "op": "replace", "key": "logger", "with": "otel_logger/OtelLogger" },
    { "op": "set",     "key": "cfg.sampling", "field": "rate_pct", "value": 10 }
  ]
}
```

`touches` is a declared summary of the rows and keys the ops address. It is
enforced (resolution refuses a bundle whose ops address something outside its
own `touches`), so it is checkable rather than decorative, but it is a
convenience, not a security property: an author who wants to touch a row simply
lists it. What it buys is that `[stack]` can be checked for two bundles
claiming the same target before anything is fetched, and that `truc apply` can
print a one-line preview of a bundle's reach without resolving it.

### 3.2 The operations

Four, and no more.

**`add { truc, as? }`** vendors a truc (pinning it in the lock if it is not
already pinned) and inserts each of its components as new rows. The rows carry
the vendoring key as their truc prefix. `as` renames the vendoring key, which
is how two copies of the same registry entry can coexist as two rows.

**`replace { row | key, with }`** withdraws the addressed row and inserts the
payload row in its place. `with` is a row address into a truc that is already
in `requiresTrucs` or already vendored. Withdrawal is not deletion of the
truc; the truc stays vendored and pinned, only its row leaves the table.

**`remove { row | key }`** withdraws the addressed row and inserts nothing.

**`set { key, field, value }`** overrides one field of a config row. See 3.4.

**Rejected: an `insert-at-position` or `order` operation.** Row order in the
table has no semantics: revl components are wired by provision key, not by
list position, so an ordering operation would be inventing a concept the gate
does not have in order to imitate a concept DSH's flat list does have. The row
table is printed in a deterministic order (truc name, then component name) for
diffability, and that order is never load-bearing.

### 3.3 Resolution is a pure fold, and the gate is never inside it

**Resolution never calls the gate.** L0 through L4 fold, in strictly increasing
rank order, to a final row table. Only then does one admission run, over the
delta between the running row table and the resolved one.

This is not a performance decision; it is a correctness decision, and it
closes a determinism trap that per-operation admission would open. Consider
`remove logger/StdLogger` and `add otel_logger` where both provide key
`logger`. Admitting per operation, `remove` then `add` succeeds and `add` then
`remove` fails on G2 at the intermediate state, so the answer depends on the
order in which ops happened to be listed. Folding first means no intermediate
state exists: the fold produces "key `logger` is provided by
`otel_logger/OtelLogger`", the delta's withdrawal set is `{StdLogger}`, and one
`compile_files(newRows, manifest=running, replacing=("StdLogger",))` call sees
the whole change at once. `replacing` exists for exactly this ("additional
components being withdrawn in the same admission").

The fold, precisely:

1. Start from the L0 row table: the project's `entry` components plus every
   component of every truc in `[trucs]`.
2. For each layer L1 through L4 in rank order, and within a layer for each
   source in its declared order (bundles in `[stack].order`, ops in the `ops`
   array, CLI flags left to right), apply each op to the table.
3. Applying an op resolves its address against the table **as of that moment**
   in the fold. Address resolution is the only order-sensitive step, and it is
   order-sensitive by design: that is what "layered" means.
4. Record, per row, the ordered provenance: every (layer, source, op index)
   that touched it, and which one won.

Every input to this fold is an ordered list in a file, or a lock-pinned hash.
Nothing depends on filesystem iteration order, directory listing order, index
row order, wall-clock time, or fetch completion order.

**And critically: the registry ranker is not in the fold.** A bundle names a
truc by a lock-pinned identity, never by "whatever the registry ranks best
today". `registry.resolve`'s least-authority-then-evidence ranking is an
*authoring-time* affordance that `truc add` may consult when a human is
choosing a package; it is not consulted when a lock exists. This is a design
commitment and it is load-bearing twice: it is what makes resolution
reproducible across machines and time, and it is what keeps 428 F5 (the
registry trusting publisher-written evidence, `"present"` outranking
`"unavailable"`, a fabricated attestation improving rank) off the resolution
path entirely.

### 3.4 `set`, and why config is a provider

revl has no config fields. A component's configuration is another component
providing a service. So `set` cannot be a dictionary write, and pretending
otherwise would require inventing a parallel untyped config plane, which is
exactly the thing this framing refuses.

A **config row** is a component all of whose provide-method bodies are
constant expressions. That is a syntactic property the compiler can decide.

```revl
service Sampling {
  fn rate_pct() -> Int
  fn enabled() -> Bool
  fn endpoint() -> Str
}

component SamplingConfig provides cfg: Sampling {
  provide cfg {
    fn rate_pct() = 10
    fn enabled() = true
    fn endpoint() = "https://collector.internal"
  }
}
```

`set { key: "cfg", field: "rate_pct", value: 25 }` desugars, during the fold,
to a `replace` of the row currently providing `cfg` with a **synthesized** row:
the same service, the same methods, the same constants, with `rate_pct`'s
constant swapped for `25`. Synthesis is a pure function of (the service
declaration, the current constants, the overrides), which makes it
deterministic and diffable, and the synthesized source is written into the row
table so the operator can read the exact component that will be admitted.

The derive-a-provider-from-a-service-declaration machinery already exists:
`revl test --mock-requires` (item 60, `docs/auto-mocks.md`) derives an
in-memory provider from a service declaration alone. Config synthesis is the
same derivation with literal return values in place of item-37 generated ones.

Two rules keep this honest:

- **`set` against a non-config row is a refusal**, not a best-effort patch:
  "row `./App` is not a config row (its provide methods are not constant
  expressions); use `replace`". DSH would happily overwrite a key that nothing
  reads.
- **A `set` whose value does not fit the declared return type does not
  admit.** The synthesized component fails to compile, and the refusal names
  the field and the declared type. In DSH the equivalent typo reaches runtime.

That second bullet is the sharpest small win in this design. Config typos are
the single most common way a layered composition breaks in practice, and this
turns them into a refusal before anything runs.

### 3.5 Conflicting patches from two bundles

Two cases, with different answers.

**Cross-layer shadowing** (bundle `obs_kit` at L1 replaces key `logger`, the
home patch at L3 replaces it too). Legal. The higher rank wins, deterministic
by construction. The provenance record names both, and `truc apply`'s row
panel prints the shadowing:

    logger    otel_logger/OtelLogger    <- L3 home patch
              (shadows: L1 obs_kit -> otel_logger/OtelLogger)
              (shadows: L0 base     -> std_logger/StdLogger)

**Intra-layer collision** (two bundles in `[stack]` both address key
`logger`). Also legal, and the later `[stack]` entry wins. Refusing would make
bundles un-stackable, and un-stackable bundles have no ecosystem story, which
is the item's whole objective. But three things make the win visible rather
than silent:

1. `touches` makes the collision detectable at `[stack]` edit time, before
   anything is fetched: `truc stack check` reports "obs_kit and audit_kit both
   touch key `logger`; obs_kit is listed first, so audit_kit wins".
2. The provenance record names the loser, and `truc apply` prints it.
3. The **authority diff is computed against the L0 base**, not against the
   intermediate stack state, so a loser bundle's authority never quietly
   disappears from the operator's view and a winner bundle's authority is shown
   in full.

**A collision on the same row within one bundle's own `ops` list is a
refusal.** A bundle author has no reason to write two ops for one row, and
allowing it would mean a single bundle's behaviour depends on its own internal
op order, which is a needless surface. Across bundles the ordering is the
operator's explicit `[stack].order`, which the operator chose; within one
bundle it would be the author's accident.

---

## 4. Re-admission cost: incremental, and here is why it is sound

### 4.1 The partition

revl's admission rules partition into three classes by what they read.

**(a) Body-local.** G1 confinement, G4 declared reach, G6 provide-method shape,
taint flow within a body, capability attenuation on activation-body spawn
(`_check_spawn_attenuation`). These read one component's source and its
imported closure. Nothing else.

**(b) Interface-local.** G2 provision conflict, G3 wiring and cycles, the
section-5 service-replacement compatibility check
(`admission._service_compatible`), and state-handoff compatibility
(`_admit_handoff_replacement`). These read the *declared interfaces* of every
row plus the candidate's body. They do not read another row's body.

**(c) Fold over rows.** The audit boundary surface, capability totals, the
item-33 boundary policy ceiling, distributability. These are a union or fold
over per-row facts.

### 4.2 What incremental admission costs

Class (a) is recomputed for the patched rows only.

Class (b) needs every row's interface, which the running manifest already
carries. Feeding it is exactly what `compile_files(candidate,
manifest=running)` does today, and that is the shipped hot-swap path, not a new
mechanism. `Session.swap` (`src/revl/mcp/session.py:794`) then performs the
transition on an already-admitted candidate.

Class (c) needs every row's per-component audit record.
`audit_diff.crossings` already produces exactly that table keyed by component,
so replacing one row's entry and re-folding is O(rows) in table entries, not in
compilations.

So the cost of re-admitting a patched composition is **one compile of the
patched rows, plus a fold over the row table**. On a 200-row composition where
a home patch replaces one row, one row compiles. The fold is a set union over
a few thousand string tokens.

This is not a new claim about admission. It is the modularity property revl
already relies on every time an agent hot-swaps a single component into a
running session. This note only names it and states its preconditions.

### 4.3 The preconditions, stated as invariants

Incremental admission is sound **only** under these. Each one is a real
constraint, and one of them is currently violated in-tree.

**I1. No admission rule may read a row's body across a row boundary.** Every
cross-row flow must be visible in the declared service type. Taint satisfies
this today because `Untrusted[T]` is a type qualifier and therefore appears in
service declarations, which means `_service_compatible` catches a
taint-widening interface change. If a future rule is a genuine cross-component
dataflow that is invisible in the declared types, incremental admission must be
disabled for that rule by name, and the honest cost is a full re-admission.

**I2. A distributable row must not `use` another row's source.** If row B
imports row A's file, A's body is inside B's compilation closure and replacing
A silently invalidates B's class-(a) facts. Structurally enforced by the truc
vendoring layout: `trucs/<name>/component.rvl` is self-contained, and its `use`
graph reaches only the pinned stdlib closure and its own package directory.
This is exactly the property truc's own components *lack*: the
truc-architecture note's stage-1 deviation records that `assembler.rvl` imports
`planner.rvl` and `workspace.rvl` imports `../externs.rvl`, so truc's own
components are not self-contained registry entries and are named as `entry`
rather than vendored under `trucs/`. That deviation is not a wart here, it is
the boundary: **project `entry` files are one row group**, compiled as a unit,
and a patch touching any of them recompiles the group. Fetched rows are
individually incremental. The invariant is only claimed where it holds.

**I3. A row's cached class-(a) facts are valid only under the compiler that
produced them.** The row table records the compiler identity, and any mismatch
downgrades to a full re-admission. This is not optional. It is also the same
requirement as 428 F2's "bind the checker's identity into the signed body",
which is a useful convergence: the fix that makes attestation truthful is the
fix that makes incremental admission safe to cache.

**The honest full-re-admission cost.** When I1, I2, or I3 fails, or when the
operator asks for it, `truc assemble --full` compiles every row. On a 200-row
composition that is 200 file compiles plus one link. It is seconds, not
milliseconds, and it is the price of a compiler-version bump or a rule that
does not partition. It is not a fallback anyone should be surprised by, so
`truc apply` prints which mode it used and why.

---

## 5. The approval UX: the authority diff is the headline

`truc apply <bundle>` rehearses by default. It writes nothing, admits nothing,
and swaps nothing. It resolves, admits the candidate as a rehearsal, and
prints four panels. `--apply` is a separate act.

### Panel 1: AUTHORITY (first, always, even when empty)

The set difference between the running row table's crossing fold and the
resolved one, using the tokens `docs/audit-diff.md` already defines
(`emit:<component>:<service.method>`, `host:<component>:<extern-name>`,
`secret:<capability>:<name>`). Additions are the dangerous direction; removals
always pass; the exit-code contract is `audit --diff`'s, unchanged.

    AUTHORITY  (obs_kit, 2 additions, 0 removals)
      + host:OtelLogger:host_http_post        capability: net
      + secret:net:OTEL_TOKEN                 capability: net
      = fs, registry, gate                    (unchanged)

      net: NEW. This composition did not reach the network before.

Where each line comes from: `audit_diff.crossings` per row, folded;
`audit_diff.diff_crossings` for the delta; the capability label from the
per-component boundary table `registry._capabilities_of` already reads.

**The precision limit, said out loud rather than papered over.** These tokens
are unparameterized. A bundle that widens `fs` from `/tmp` to `/etc` under an
extern the composition already reaches produces **no new token**, and this
panel must therefore print

    fs: already held. Token-level diff cannot distinguish which paths a
    crossing reaches. 3 rows reach `fs`; obs_kit/OtelLogger is new among
    them.

rather than "no widening". Parameterized spellings (`fs.write(path="/etc")`)
are item 294's job; until they land, the honest statement is "same token, new
reacher", and the panel lists the new reachers. Promising `/etc` precision
today would be the exact overclaim 428 F2 documents elsewhere in the tree, and
this design will not make it.

### Panel 2: ROWS

The provenance table from the fold: added, replaced, removed, config-set, with
the winning layer and every shadowed layer (section 3.5's rendering).

### Panel 3: WIRING

`composition_diff.diff`'s membership and wiring axes, in its existing
guarantee-sentence form:

    provider of key `logger` changed from `std_logger/StdLogger`
      to `otel_logger/OtelLogger`
    `otel_logger/OtelLogger` requires `cfg`, newly provided by `config/cfg`

The provider-changed sentence is the single most important line in a
supply-chain substitution, and it is already implemented.

### Panel 4: PROVENANCE, in two columns

For every incoming truc, split what the receiver **verified** from what the
publisher **claimed**. This split is mandatory, and it exists because 428 F5
records that `Registry.from_dir` reads authority claims off `index.json`,
returns `component.rvl` from disk, and cross-checks neither, while
`assess_evidence` grades a publisher-written file as `present` and lets it
outrank an honest `unavailable`.

    otel_logger
      VERIFIED (recomputed here)
        sourceHash    b782e70c...  matches the pin in truc.lock
        manifestHash  d7d185fd...  regenerated by this compiler, matches
        admission     PASSES this machine's gate (untrusted-author profile)
      CLAIMED (written by the publisher, not checked)
        publisher     "acme-observability"
        fault sweep   "9999/9999"
        attestation   present, unverified (no key configured)

Anything in the CLAIMED column is decoration and is labelled as such. A
fabricated dossier can only ever add lines to the lower half.

### Refusals are data

A candidate that does not admit produces the `RevlError` why-trace naming the
row and the rule, and nothing is written:

    truc: refused - the patched composition would not admit.
      G2 provision conflict: key `db` is provided by both
        `pg_database/PgDatabase` (L0 base)
        `audit_kit/ShadowDatabase` (L1 stack, audit_kit op 0)
      The running composition is untouched.

This is the no-blip discipline `Session.swap` already implements via
`_abort_swap` (a rejected swap rolls back to the previous generation, which
keeps serving).

### The apply gate

`--apply` requires either an interactive confirmation or, in CI, an ack per
added crossing using `audit --diff`'s exact `--accept <token>` tokens, so the
ack list is copy-pasteable from the failure output and reviewable in a diff.
`--accept-all` exists and is a policy choice spelled in the repo.

---

## 6. Why this beats DSH rather than imitating it

1. **A patch that breaks the composition is refused before anything runs**,
   with a navigable trace naming the row and the rule. DSH discovers at
   runtime.
2. **An address that resolves to nothing is a refusal, not a silent no-op.**
   DSH's vanished target id does nothing and says nothing.
3. **`set` is typed.** A wrong-typed config value does not admit. DSH
   substitutes it into a dictionary.
4. **The authority diff exists.** DSH has no analogue at all.
5. **Resolution consumes only lock-pinned identities**, so "it worked
   yesterday" is reproducible on another machine. DSH's stack resolves against
   whatever is installed.
6. **Third-party rows carry no host code by default** (section 8), so the
   authority diff is sound rather than advisory. DSH plugins are arbitrary code
   and no diff over them could mean anything.

---

## 7. What this design needs that is not currently true

Cited against roadmap 428, the supply-chain and attestation audit.

**BLOCKING: 428 F3, the optional pin.** `plan_drift`
(`truc/components/planner.rvl:163`) skips a vendored truc with a missing or
blank `sourceHash`, and nothing requires a truc named in `truc.toml` to carry
a pin. Executed through the real CLI, a dropped lock row let `PgDatabase`
become `ExfilDatabase` while truc printed that every component was admitted
through the gate. This design puts the entire weight of row integrity on the
pin, so a skippable pin is not a degraded mode, it is the absence of the
mechanism. Required: a truc without a pin is a refusal at resolution, not a
skipped check; and truc's own bootstrap lock (currently a bare file list with
no hashes) carries them.

**WANTED: 428 F5, registry claims read as facts.** Not on the resolution path,
because section 3.3 commits to keeping the ranker out of the fold. It is on the
Panel-4 path: `Registry.from_dir` must either recompute `sourceHash` and
`manifestHash` from the entry's bytes with the local compiler, or mark the row
unverified, or Panel 4's VERIFIED column would print publisher-written numbers
in the column that says the receiver checked them. Until then Panel 4 puts
everything registry-sourced in CLAIMED.

**WANTED, and convergent with I3: 428 F2, `G1..G9` as a constant.**
`make_attestation` checks a non-empty key and an empty `ir["holes"]`, then
signs all nine codes; an IR the compiler refuses by name for G2 signs all nine
and verifies. This design's answer is to **not depend on it**: a bundle is
re-admitted locally, always, and an attestation is never accepted in place of a
local admit. That is a deliberate stance, not an oversight. Attestation becomes
load-bearing only for a future "skip the local admit because someone else did
it" optimization, and that optimization must wait for the claim to be a
measurement (take the gate verdict as an argument, refuse to sign without one)
and for the checker identity to be bound into the signed body. The second half
of that fix is the same thing I3 needs for cached class-(a) facts.

**NOT on the critical path: 428 F1 (`sign_alg` unverified), F9 (no domain
separation between attestation and receipt MACs).** This design signs nothing.
Bundles are hashed and re-admitted. Say so explicitly rather than inheriting a
signature story it does not use.

**NOTED: 428 F6.** `truc reproduce` compares the registry against itself, so a
substituted dependency reproduces green when the registry is the adversary, and
the project lock's `sourceHash` is the only independent anchor. That is exactly
the load this design places on the pin, which is why F3 is blocking and not
merely wanted.

**Outside 428, from 425 F1:** the untrusted-author profile (item 329) exists and
is unwired at the MCP verbs. Section 8 needs it wired for truc's fetch path.
That is a different call site from 425's, so this is a new wiring, not a
dependency on 425's fix landing.

---

## 8. Adversarial review

### CRITICAL: the authority diff is computed from the attacker's own declarations

The design as drafted through section 7 has a hole that voids its central
promise.

`replace` lowers to `compile_files([payload], manifest=running,
replacing=(Old,))`. Panel 1 is computed from `audit_diff.crossings`, which is
derived from the per-component G8 boundary table, which is derived from
**declared** extern classes and **declared** capability labels. Roadmap 425 F1
states the consequence precisely: the gate rests on declarations that are never
enforced against the arbitrary Python in an `@py` body, and it records three
executed exploits, including one where the approval ticket reads
`capabilities: ['notify']` while the body reads and exfiltrates a `.env`.

Applied to this design: a third-party bundle ships a row whose `@py` body reads
`~/.aws/credentials` and posts it, with every extern declared `pure`. Section 5
Panel 1 prints **zero additions**. The operator sees "no new authority", applies
the bundle, and the design has actively made the attack easier by putting a
reassuring green panel in front of it. Every other property (the pin, the
determinism, the refusals, the typed `set`) survives untouched and is worth
nothing, because none of them constrain what a host body does.

This is worse than having no authority diff, because a diff that is
structurally blind to the most likely attack teaches operators to trust it.

### The fix

Three parts, and the first is the whole of it.

**F1. A fetched row is compiled under the untrusted-author profile, always.**
`compile_files` already takes `profile: AdmissionProfile`, and item 329's
untrusted-author profile refuses a new `extern` or host block in the admitted
source (`_enforce_source`, plus the item-330 import and reach closure so the
refusal cannot be routed around by importing). Every row whose truc prefix is
not `.` is admitted with that profile. No `@py` body, no `@ts` body, no host
block. The consequence is the one that matters: **there is no host code in a
fetched row for a declaration to lie about**, so Panel 1 is computed over
declarations that cannot lie, and the diff becomes sound rather than advisory.

A fetched row reaches the world only through externs the *project* declared,
in the project's own `entry` files, which the project's author reviewed and
which `revl audit` already enumerates.

**F2. This makes the trust boundary the truc boundary**, which is the
truc-first payoff and is not incidental. Authority enters a composition
through first-party `entry` sources and the pinned stdlib closure, and never
through a fetched row. The row, already the unit of identity, of patching, and
of incremental admission, becomes the unit of confinement too. One boundary
doing four jobs is the argument for this framing.

**F3. The escape hatch is explicit, separate, and shaped differently.** Some
legitimate bundles genuinely need a new host crossing: a database driver, a
telemetry exporter. Those cannot pass the no-extern profile, so `truc apply`
**refuses them by default**, naming the bodies:

    truc: refused - bundle `otel_kit` ships 2 host bodies.
      otel_logger/OtelLogger: extern `host_http_post` (@py, @ts)
      otel_logger/OtelLogger: extern `host_env_read`  (@py)
    A fetched component's host code is not checked against its declarations,
    so its authority cannot be diffed. Re-run with --trust-host-code to admit
    it as reviewed first-party code.

`--trust-host-code` admits it, and renders a panel with a different shape and a
different sentence. Not "these crossings were added", which implies a
measurement, but:

    UNCHECKED HOST CODE  (otel_kit, 2 bodies)
      This bundle ships host code whose behaviour nothing verifies. The
      crossings below are what it CLAIMS to do. Read the bodies.
      claims: host:OtelLogger:host_http_post   net
      claims: host:OtelLogger:host_env_read    (undeclared capability)
      2 bodies, 41 lines of @py. Printed in full below.

Default closed, the claim-versus-check distinction stated in the sentence
rather than buried in a footnote, and the bodies printed so "reviewed" is
achievable rather than notional.

### Residual, after the fix

The fix is sound at token granularity and blind at parameter granularity. A
fetched row can still call an extern the project already declared with a
different argument: `host_write("/etc/passwd")` where the project only ever
wrote `/tmp`. Panel 1 shows the new reacher (section 5's precision-limit
rendering) but cannot show the path. Closing that is item 294's parameterized
capabilities, and this design does not pretend otherwise. What it does do is
make the residual small and nameable: the reachable surface is bounded by the
project's own declared externs, and the panel names which new rows reach each
one.

### Two secondary findings, both closed above

**Determinism, closed in 3.3.** Admitting per operation makes `remove` then
`add` succeed and `add` then `remove` fail on G2 at an intermediate state, so
the verdict would depend on op order rather than on the resolved result. Closed
by making resolution a pure fold with no gate inside it and admitting the delta
once, using `replacing` for the whole withdrawal set.

**Ergonomics, closed in 3.4.** A model in which the only patch operation is
"replace a whole component" is too coarse for the change people actually make
(one number), and a model nobody uses fails at the item's actual objective.
Closed by `set` desugaring to a synthesized config provider, which keeps the
one-line ergonomics and gains type checking that DSH cannot offer.

---

## 9. The strongest argument against this framing

Stated honestly, because it is a real risk and not a rhetorical concession.

**truc is a static, on-disk, vendor-directory tool, and the thing that actually
needs patching is a live, in-memory composition.** `truc assemble` reads
`truc.toml`, verifies `trucs/`, and writes `build/assembly.json`. The harness
does none of that: it calls `ship_files()` (`src/components/composition.rvl`)
onto `Session.swap`, with a file list and a generation counter, and it has no
project directory, no `truc.toml`, and no vendor dir. Roadmap 424(a) records
that truc is "landed but unused by the harness".

By routing layering through truc, this design imposes truc's project layout on
a runtime that has none. Its answer ("the session's row table is materialized
from a lock") requires the harness to adopt truc's on-disk model before any of
this ships. A composition-first design can put row identity in the IR and in
the session, where the swap already lives, and can patch a running composition
with no project directory in existence. If the harness never adopts truc, this
design ships nothing, and the adoption risk is not hypothetical: it has been
landed and unused for the whole time it has existed.

**The rebuttal, briefly, without hedging the concession.** Identity has to be
persisted somewhere or a patch cannot survive a restart, and the lock is the
only place in the system that already persists composition identity together
with integrity hashes. A composition-first design will have to invent that
persistence, and it will end up looking like a lock. But that is an argument
about where the work lands, not about adoption, and the adoption objection
stands on its own.

**The falsifiable version of the disagreement**, for whoever synthesizes this
against the composition-first design: does a running session need a row table
that is *derived* from a persisted lock (this design), or a row table that is
*primary* in the IR with persistence bolted on (composition-first)? Both
designs need both artifacts. The question is which one is the source of truth
when they disagree, and the answer decides who owns re-admission.

---

## 10. Exit tests

1. A bundle replacing a row by key resolves, admits, and swaps; the row table
   and provenance record name the winning layer.
2. The same bundle, addressing a row id that no longer exists after a pinned
   version bump, is refused at resolution with the address, the layer, and what
   the truc provides instead. It never no-ops.
3. Two bundles in `[stack]` touching one key: the later wins, both appear in
   provenance, and the authority diff is computed against L0.
4. `set` with a wrong-typed value does not admit, and the refusal names the
   field and the declared return type.
5. `set` against a non-config row is refused before synthesis.
6. A bundle shipping an `@py` body is refused by default, naming the bodies;
   `--trust-host-code` admits it and prints the UNCHECKED HOST CODE panel.
7. A bundle whose declared-`pure` body would exfiltrate is refused by the
   untrusted-author profile, so the exploit shape of 425 F1 has no reachable
   spelling on the fetch path.
8. A vendored truc with no lock pin is refused at resolution (the 428 F3 gate).
9. Incremental admission of a one-row replacement compiles exactly one row;
   `--full` compiles all of them; both reach the same verdict and the same row
   table on a 200-row fixture.
10. A row group violating I2 (project `entry` files that cross-import)
    recompiles as a group, and the row table records that it did.
11. Resolution is byte-identical across two machines given the same
    `truc.toml`, `truc.lock`, `[stack]`, and patch files, with different
    registry index contents present on each.
