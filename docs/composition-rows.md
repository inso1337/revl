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
  third-party layer, for the same reason no layer may raise its own trust class
  ([composition layers](composition-layers.md#granted-is-never-in-a-stack-layer)).

A row that writes no `granted` clause at all is unconfined: it is the project's
own code, and confining it would be wrong. Wiring the untrusted-author profile
per row is the confinement slice, and it waits on the trust decision recorded in
roadmap 425 F1. Until then the clause and its subset check are enforced and the
profile is not, which is exactly the split roadmap item 424 slice A1 states.

## `remote`: a row whose provider is synthesized

A `remote` row places a provider that runs somewhere else. It names no file,
because its provider does not exist as source until the resolver synthesizes it
from the service declaration and the peer address.

```revl
composition Shop {
  use "services.rvl"

  row @checkout from "consumer.rvl" provides checkout
  remote @billing provides billing: Billing
    at host("billing.internal:8443")
}
```

The address is the address: the synthesized crossing refuses a redirect rather
than following one off it (see [`redirect`](#redirect) below).

This is roadmap item 424 gap (c), slice C2; the design note is
[design/424-dsh-language-gaps.md](design/424-dsh-language-gaps.md) §3.2.

**The wiring is local, and that is the whole point.** `CheckoutSvc` still says
`requires billing: Billing` and does not change by one character between a local
provider and a remote one. G2, G3 and G4 are unchanged. Remoteness is an
ADMISSION fact — a reach, a capability, a failure mode — and never a wiring fact,
so bringing the provider back in-process is a one-line edit to the composition
rather than an edit to every consumer. That is the rule
[interop-bridge.md](interop-bridge.md) §3 already states for a placement seam
("manifest data, not source text"), applied to a callee that is in no manifest
at all. The `revl composition` WIRING panel prints the same line for both, and
the diff between the two compositions is empty there.

### What the row can promise, and what it refuses to

A remote provider is outside the composition's trust boundary by construction,
so the surface is written to refuse rather than to pretend.

**No inverse is synthesized, and a remote effect survives unwind.** revl's
teardown stack has three entry kinds. `bracket` needs an INFALLIBLE inverse
(G5), and an inverse that travels over a network is fallible by construction:
the peer may be unreachable, restarted or gone by teardown time.
`transactional` (item 243) needs a HOST-LOCAL inverse and a witness captured on
the `Ok` branch, and the peer's own claim that it undid something is not a
witness — it is one more assertion from the same peer. So every synthesized
operation is `emission` with no `undo` and no `compensate`: G4's other branch,
DECLARED IRREVERSIBLE.

This leaves **G7 intact rather than strained**. G7 is LIFO-complete over
REGISTERED entries, and a synthesized remote operation registers none, so there
is nothing for G7 to walk and nothing it can fail to walk. A `compensation`
(item 247, audit-grade, best-effort) is the only kind such a call could ever
carry, and it stays the composing engineer's to write by hand: only they know
which remote operation undoes which. The synthesizer will not guess.

**Withdrawal costs nothing, precisely because there is nothing to undo.**
Item 426 §5.3 files activation of `replace`/`remove` as blocked, and the reason
is teardown: withdrawing a wired row means disposing a fiber and replaying its
teardown in the correct LIFO position. A `remote` row does have a local fiber —
its synthesized provider is an ordinary component, plugged like one — but that
fiber holds no acquired resource and registers no teardown entry, so disposing
it is a pure unwiring: the provision is withdrawn, consumers re-resolve and
deactivate reactively, and the LIFO replay that blocks the general case is
vacuous. The expensive half of that residual is exactly the half a remote row
does not have.

**It re-admits nothing.** A remote row does not admit, verify or re-admit the
callee, and there is no "verified remote" badge to be had: item 337 requires the
RECEIVER to re-compile from its own independently held source, and a client is
the sender. What is bounded is local and only local — the reach, the capability
and the failure mode. If both sides are revl and both want a mutual guarantee,
that is `revl contract export` / `revl contract check`, or 337's seam.

The generated source carries all of this in its header, so the statement travels
with the artifact rather than living only here.

### Remotable means "declares an emission bound"

Every method of a remotable service must declare an emission bound. A plain `fn`
service is not remotable and the refusal names the method:

```text
service `Metrics` is not remotable: method `tick` is a plain `fn`
(remote row `@m`)
```

This is not a new rule. It is G4 read at the client: a network call IS a
boundary crossing, and a provider may be purer than it declares but never less
pure.

### `on_failure`

| clause | meaning |
|---|---|
| `on_failure(withdraw)` | the default. A transport failure is a FAULT. |
| `on_failure(result)` | the failure comes back in band as `Err`. Admitted only if every method returns `Result[T, Str]`. |

There is no third option: silently swallowing a transport failure has no
spelling. `withdraw` is the default because revl already has a settled answer
for a remote peer that goes away — peer death is provider withdrawal, the
consumer deactivates reactively, and a breached deadline withdraws too, because
a wedged remote provider is indistinguishable from a dead one
([network-path.md](network-path.md)). A second failure channel would put two
answers to one question in the language.

`on_failure(result)` has a real cost, which is why it is the opt-in and not the
default: the provider is not withdrawn, so a wedged peer stays wired and every
call keeps paying for the round trip.

**What this slice lands, and what it does not.** `on_failure` is parsed, checked
against the service's return types, carried into the IR and the manifest, and
the generated body raises a fault rather than returning a quietly-empty result.
The RUNTIME half — turning that fault into a provider withdrawal that cascades
through the reactive path — is armed today by the placement bridge's monitor
connection (`backends/python/bridge.py`, `watch(on_lost)` fired on monitor EOF),
which is a seam-client mechanism a synthesized row does not join. Wiring it is
the next step, and `tests/test_424_remote_row.py` pins the declaration so the
day it lands, something says so.

### `redirect`

| clause | meaning |
|---|---|
| `redirect(refuse)` | the default. A 3xx from the peer is a FAULT naming the rule. |
| `redirect(same_origin)` | a 307 or 308 that stays on the declared origin is followed, method and body intact, at most five hops. |

The peer address on the row is what a reader of the composition is entitled to
believe the crossing reaches, and the reach bound `net.<host>` is folded out of
it. `urllib` follows a redirect by default and re-issues a 301/302/303 `POST` as
a `GET` with the body dropped, so a peer that answers `302` could move the
crossing to another host, turn the declared emission into a read, and take every
header on the request — a credential, a `Secret[T]` — along with it. The row
would then be false about all three. So a redirect is a REFUSAL CONDITION, not a
transport detail.

`redirect(same_origin)` is the declared opt-in and is still bounded on both
axes. A 301, 302 or 303 is refused whatever the clause says, because it changes
the method; a cross-origin hop is refused because the address on the row is the
bound. The refusal stays a FAULT even under `on_failure(result)`: `on_failure`
says what happens when the DECLARED crossing fails, and a redirect is the peer
declining to be the declared endpoint at all.

The same policy, and the per-tier defaults behind it, are in
`src/revl/crossing_redirect.py`; `revl import a2a` carries it too, under
`--follow-redirects`.

### Two peers of one service are two realms

```revl
composition Tenants {
  use "services.rvl"

  remote @billing_a provides billing: Billing
    in realm("tenant_a") at host("a.billing.internal:8443")
  remote @billing_b provides billing: Billing
    in realm("tenant_b") at host("b.billing.internal:8443")
}
```

Two rows, two `(key, realm)` addresses, no collision and no new rule — the same
mechanism a per-tenant local store already uses. Two peers in the SAME realm
collide with the ordinary row-level G2 refusal naming both rows.

### The peer address

`at host("...")` takes a bare authority: `host` or `host:port`. A URL is
refused, and an address carrying userinfo is refused outright:

```text
the peer address of remote row `@a` carries userinfo
```

The reach token (`net.billing_internal`) is folded from the HOST alone, never
the port and never userinfo, so two credentials against two hosts cannot
collapse onto one token and a credential can never become part of a capability
spelling. Refusing the address is the fail-closed reading: accepting it and
quietly dropping the credential would leave a live secret written in a
composition document, which is a worse place for it than a URL.

An address is a static string literal. It is exactly the class of value roadmap
item 350 binds through a `boot` component, and when 350 lands a peer address
should arrive that way rather than through a second mechanism.

### What it projects, and what it refuses

The wire is the canonical one, not a second encoding: the request envelope is
`{"key", "method", "args"}` and the reply `{"ok", "value" | "error"}`, which is
the placement bridge's own envelope carried over HTTPS.

Two limits, both refusals rather than approximations:

- **The JSON-transparent subset only.** Scalars, and `Opt`/`List` of them. A
  record or an ADT needs the tagged half of the canonical encoding
  (`{"$kind", "$value"}`), which lives in the bridge and is not reachable from a
  generated host body. `revl export client` (slice C1, buildable today over the
  same encoding) builds that projection, and a remote row will use it rather
  than grow a second copy. A method it cannot project is refused naming the
  method and the type.
- **The `py` tier only.** An `emission` method emits a SYNCHRONOUS function on
  the TypeScript tier, and a network round trip is not synchronous, so a
  `fetch`-based body would be `await` inside a non-`async` function. The ts
  projection waits on the async crossing rather than shipping a body that does
  not typecheck.

### Values are not tainted yet

Item 424 D-424c.9 requires every value a remote provider returns to be
`Untrusted[T]`, and that is slice C3, not this one. Until it lands, a value that
crossed this boundary is indistinguishable at a call site from a local one —
which is exactly the hole D-424c.9 exists to close. The generated header says so
in the artifact itself.

### The one thing `remote` costs the lexer: nothing

`remote`, `at`, `host`, `through`, `on_failure` and `redirect` are CONTEXTUAL
keywords,
recognised only in this one position inside a `composition` block. The lexer's
`KEYWORDS` set is untouched, so the self-host lexer needs no sync and a program
using any of those words as a provision key, a require alias or a config field
keeps working. `in` and `realm` are reused verbatim from `isolate`. Promoting
`remote` to a real keyword would break those programs, force a matching edit in
the self-host lexer, and put a second copy of the reserved-word set on every
backend that re-derives it — for no gain, since the parse position is
unambiguous.

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

A `remote` row carries the admission facts the wiring deliberately does not:

```json
{
  "label": "billing",
  "qualified": ".::@billing",
  "source": ".revl/synthesized/_project/billing.remote.rvl",
  "component": "RemoteBillingProvider",
  "claims": [{"key": "billing"}],
  "requires": [],
  "remote": {
    "peer": "billing.internal:8443",
    "service": "Billing",
    "serviceSource": "services.rvl",
    "capability": "net.billing_internal",
    "onFailure": "withdraw",
    "redirect": "refuse",
    "inverse": null
  }
}
```

`source` names no file on disk. The synthesized provider is handed to the
compiler through the in-memory source map `compile_files` already takes, so
`_link` runs G2, G3 and G4 over it exactly as over a file row — which is why
the synthesizer is no more on the trusted path than the resolver is. The path
is derived from the origin and the label alone, so two machines resolving the
same composition still produce byte-identical rows.

A declared composition is compiled and READ: its rows are already in the IR. The
bootstrap that today compiles a manifest document, emits it to Python, execs it,
calls it, and parses the JSON string it returns becomes "compile, read the
manifest" ([composition-bootstrap.md](composition-bootstrap.md)).

## What is not here yet

The row table is the object everything else in item 426 needs, and it is
deliberately the whole of this slice.

The FOLD is built and is documented separately in
[composition layers](composition-layers.md): a composition names its `stack` and
`site` layers, and the four operations (`add`, `remove`, `replace`, `configure`)
patch the rows this document defines.

| next | what it adds | what it waits on |
|---|---|---|
| incremental admission | admitting a resolved delta through `admit_into` with a `replacing` withdrawal set, so the cost is one compile of the patched rows | the fold |
| confinement | non-first-party rows compiled under the untrusted-author profile, and the per-root profile split in `compile_files` that makes a mixed-trust delta expressible in one call | roadmap 425 F1's decision |
| the authority panel | crossing tokens re-keyed by row label, a `config:` token carrying a value digest, a fail-closed headline, and a printed blind-spots block | confinement, and roadmap 428 F3 |
| distribution | a layer is a truc, the `[trucs]` origin namespace becomes real, the pin becomes mandatory | roadmap 428 F3 |

Two surface clauses the design defines are still not grammar, and writing one is
a parse error rather than a silently ignored clause: `open`
(which fields a third-party layer may configure) and `reach` (the
composition-level authority bound). `place` and `variant` are the same. Each
arrives with the slice that gives it meaning, because a clause that parses and
does nothing is worse than one that refuses.

Activation is unchanged and stays whole-generation for anything but a pure
addition. That is a property of G7, not of effort: a withdrawn component's fiber
must be disposed with its teardown in the correct LIFO position and every
consumer re-resolved, which the partial-link path deliberately refuses.
