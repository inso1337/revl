# 294: parameterized, resource-bounded, and lease-bound capabilities

Design note for roadmap item 294 (`docs/v2.0-roadmap.md:3850`): a capability
that carries resource parameters (`fs.write(path="/tmp/job-42")`), a numeric
bound (a max call count, a size bound), or a lease (authority that expires and
is auto-revoked when its binding ends: a component generation, a task, a
realm, a human approval, a duration, a call count). This is design-first. It
changes no compiler code; it records what already exists and how far it
already goes, the one move the item turns on (a parameter is a point in a
partial order UNDER the existing token, so the item-66 subset check extends
rather than being replaced), the surface recommendation and its trade-off
against the 363/411 manifest precedent, the extended partial order stated
precisely, an honest enforcement ledger (checked vs enforced vs declared), a
reconciliation of every lease binding against the shipped 344/379/246
machinery, a staged plan, and exit tests an implementation agent can pick up.

The roadmap item's own correction is load-bearing and this note obeys it: the
capability ALGEBRA already exists. Item 66 enforces `reach(child) subset-of
held(parent)` on every activation-body spawn (`_check_spawn_attenuation`,
`src/revl/lower.py:8081`; `docs/capability-attenuation.md`;
`examples/tenant_attenuation.rvl`). Item 294 extends that subset relation to
bounded tokens. It does not introduce a second authority mechanism, and this
note refuses one at every fork where it would be the easy answer.

## The problem (measured)

A capability today is an unstructured string token, compared by identity.
Three measurements locate the gap:

1. **Authority per token is all-or-nothing.** The token grammar
   (`_capability_list`, `src/revl/parser.py:1534`; `_capability_token`,
   `parser.py:1574`) yields a bare or dotted name: a wiring key (`db`), a
   declared extern token (`gateway.send`, item 343), a realm-dotted policy
   token (`production.payment`), or the unnameable `*`. Holding `fs.write`
   is holding ALL of `fs.write`: every path, every size, forever. The
   attenuation check can drop a token from a child (the
   `tenant_attenuation` example drops `kv_b`), but it cannot pass down
   LESS of one token. The roadmap's motivating spell,
   `filesystem.read(path="/data/incoming")`, has no spelling.

2. **"How much" has no spelling at all.** Nothing in the declared surface
   bounds call counts or sizes. The one shipped how-much bound lives
   operator-side: a standing grant's `remainingUses` counter
   (`mint_standing_grant` / `_consume_grant`, `src/revl/mcp/session.py:2020`
   and `:2003`), invisible to the source program and to the attenuation
   algebra.

3. **Temporary authority exists but only as an operator artifact.** The 344
   standing grant is already a lease in everything but name: it carries an
   expiry (`expiresAt`, checked at the crossing, not at mint: invariant 3,
   `_live_grant_for`, `session.py:1944`), a uses counter spent durably
   before the fire (`_consume_grant`, Decision 3), a generation binding
   (`candidateHash`: a swap recomputes the hash and the stale grant fails
   liveness with no bookkeeping), a session binding, boundedness by
   construction (an unbounded grant is refused, `session.py:2118`), and
   early revocation (`revoke_standing_grant`, item 379,
   `session.py:2156`). What is missing is not the lease runtime. It is the
   connection: the program surface cannot declare that an authority is
   lease-shaped, the attenuation algebra cannot compare a shorter lease to
   a longer one, and the lifecycle bindings (task, realm) have no wiring
   into the ledger that already knows how to expire and revoke.

The item's payoff sentence is the agent-security one: temporary authority is
safer than permanent for agents. The 248 harness measurement already cashed
half of it (the shell-escape session went from 3 prompts to 1 via a TTL/uses
grant). 294 makes the other half declarable and checkable.

## Background: what already exists, with the seams named

### The token and its four scopes

A capability token appears in four places, all sharing one grammar and one
namespace: the `emission[caps]` / `witnessed[caps]` scope on service methods
and externs (`parser.py:1350-1361`, item 343: the declared token, not the
extern name, is the capability), the `Approval[C]` / `await approval[C]`
token (`parser.py:1574`, which refuses `*`: an unnameable reach can never be
approved into), the item-33 policy grammar, and the grant/revoke verbs
keyed by capability (`session.py:1932`). Any parameterization must land in
this shared grammar once, or the namespaces drift.

### The subset algebra (item 66) and its friends

Four rules bound capability at four scopes (`docs/capability-attenuation.md`):
G4 bounds a declaration, item 33 the composition, item 55 the operators,
item 66 the lineage. The lineage check is the one 294 parameterizes:
`held(parent)` is the requires keys plus the parent's own emission surface
(`_held_capabilities`, `lower.py:8054`), `reach(child)` is the child's
surface closed over its spawn subtree, and admission is set inclusion with
`*` as the sound over-approximation for host reach (`lower.py:8103-8132`).
One seam in that code is load-bearing for 294 and must be named now: the
elements the fold compares are WIRING KEYS, not declared tokens.
`_collect_emit_caps` records the requires-target NAME for an emit step
(else `*`), and `_held_capabilities` is requires keys plus emission caps.
The `tenant_attenuation` example works because key and token coincide
there. A parameterized token breaks the coincidence (the key is `fs`,
the declared token is `fs.write(path=...)`), so the extension cannot
just swap the comparison operator; it needs the key-to-token bridge
specified in the algebra section, or the flagship spawn check silently
compares bare keys and never sees a valuation.
The check is compile-time refusal, not codegen: an admitted program emits
identically on every tier. 294 must preserve that property in its first
slice or the "all six backends" cost in the roadmap item lands immediately
instead of when the lease runtime needs it.

### The grant ledger is already the lease runtime (344/379)

Enumerated once, because the reconciliation section leans on every field.
A standing grant (`session.py:2127-2140`) is
`{capability, candidateHash, component, session, grantedAt, expiresAt,
remainingUses, consumed, revoked}` with:

- coverage by the ONE predicate `_grant_covers` (`session.py:1932`),
  deliberately identity-on-token so mint and revoke can never disagree
  (the 245/246-F1 over-coverage hole closed on that single line);
- liveness as a separate axis (`_live_grant_for`): component, candidate
  hash (generation), session, expiry-at-the-crossing, uses remaining;
- consume-before-fire durability (WAL ordering, `_consume_grant`);
- boundedness by construction (uses, ttl, or a policy
  `requires approval ttl` rule, else refused);
- early retirement (`revoke_standing_grant`, item 379), which retires by
  the identical coverage predicate.

A 294 lease that re-implemented any of this would be the parallel mechanism
this note refuses. The design below makes every lease binding either a
liveness axis this ledger already has or a new revocation TRIGGER wired to
the revoke verb it already has.

### The approval gate (245/246)

Class-(c) crossings prompt per call with a hash-bound single-use ticket;
`await approval[C]` mints a typed, TTL-carrying `Approval[C]`;
consume-before-fire holds on py and ownerless tiers refuse at emit. A lease
"bound to a human approval" must BE this ticket, not resemble it.

### The declared-reach precedent (item 373) and the enforcement owner (411)

`extern emission(confined: workspace) fn ...` declares which PARAMETER
carries a crossing's confinement region (`parser.py:1513`, checked against
the parameter list in `lower.py:2400` onward). It is an honest trust-me
claim: the audit surface prints it, `audit --diff` flags a weakening, and
nothing enforces that the host body honors it. Item 411
(`docs/design/411-sandbox-placement.md`) is where declared resource bounds
become enforced ones: the sandbox needs table maps a capability to an OS
resource (an `fs:path` mount, a net rule), a trusted runtime enforces it,
and the plan-time gate refuses an envelope that does not cover the declared
needs. 294's path parameter slots into exactly that bridge, and this note
scopes the enforcement half to 411 explicitly rather than quietly claiming
it.

### The surface precedent (363/411): manifest over grammar, and its limit

Both placement items chose manifest data over source grammar, and 411
rejected a source-level sandbox spelling outright
(`docs/design/411-sandbox-placement.md`, "The rejected source-level
spelling") on the argument that isolation is a deployment trust decision
made by the operator about code they may not have authored, so the author
is the wrong party to control it. That argument cuts the OTHER way for a
resource parameter: `path="/data/incoming"` is part of what the program
MEANS. The author of the ingest component is the only party who knows it
reads the incoming directory and not the whole volume. The precedent is
therefore split, not overridden, in the recommendation below.

## The crux: one order, three layers

The whole item reduces to one move: a capability stops being a point
(a token) and becomes a point in a partial order UNDER its token, a cone of
narrowings. Everything else is placing each kind of bound in the layer whose
party actually knows it:

| layer | party | knows | 294 gives it |
|-------|-------|-------|--------------|
| source | author | the resource the code touches (path, table, host), the shape of its need (a count per activation) | parameterized tokens in the existing capability grammar |
| gate / ledger | operator | how long consent lasts, how many uses, when to revoke | the shipped 344/379/246 lease runtime, unchanged in kind, extended in coverage and triggers |
| manifest / sandbox | deployer | what is physically enforced | the 411 needs table consuming the declared parameters (scoped to 411) |

The invariant that ties the layers together: every layer may only NARROW
what the layer above declared. The author's parameter narrows the token,
the operator's lease narrows the author's declaration in time and count,
the sandbox narrows the process to the declared envelope. No layer can
widen, because each check is the same subset relation in the same order.

## Surface: how a bound is spelled (question 1)

Three candidate surfaces were on the table.

### (a) Parameterized tokens in the source grammar (recommended for resource parameters)

Extend the shared token grammar with an optional parenthesized argument
list:

```revl sketch
service Store {
  emission[fs.write(path="/data/incoming")] fn ingest(row: Str) -> Int
}

extern witnessed[fs.rm(path="/tmp/scratch")] fn clean(p: Str) -> Unit ...

component JobWorker requires fs: Store {
  // reach: fs.write(path="/data/incoming"), by declaration above
}
```

Grammar: after a dotted token, `(` ident `=` literal (`,` ident `=`
literal)* `)`. Values are STATIC literals (string or int); a parameter list
on `*` is refused (the unnameable reach cannot be bounded by name, the same
rule `_capability_token` already applies to approving `*`). The list lands
once, in `_capability_list` and `_capability_token`, so the method scope,
the extern scope, `Approval[C]`, the policy grammar, and the grant verbs
all speak it (the shared-namespace requirement above).

For: the resource is author knowledge (the 363/411 counter-argument cuts
this way); the roadmap item literally spells it this way; the bracket is
already revl's parameterization bracket and the token is already dotted,
so the grammar spend is one production; the declaration lands on the audit
surface where `audit --diff` makes a widening reviewable (the 373 payoff,
now with comparable values instead of prose).

Against: grammar spend against the manifest precedent; a static literal
cannot express a per-instance resource (`/tmp/job-42` where 42 is the
job); risk of reading a declared path as an enforced one (the honesty
ledger below is the answer, plus the audit line naming enforcement
status).

The per-instance gap has a revl-shaped answer that needs no new dynamic
machinery: a parameter value may also name a config key
(`fs.write(path=config.job_root)`). A symbolic value is opaque to the
static order (comparable only to itself, so it can be passed down but
never compared against a literal) EXCEPT at a spawn site whose `with { }`
block binds that key to a string literal, where the checker substitutes
and the per-instance attenuation chain shows the resolved path. That is
the roadmap's own `/tmp/job-42` example, done per instance the way
`tenant_attenuation` does per-tenant stores. Fully dynamic values remain
what item 373's `confined:` is for: declared, audited, not compared.

Two symbol rules complete this, both fail-closed. First, ONLY a literal
`with { }` binding substitutes: a spawn that binds the key to anything
non-literal leaves the parameter symbolic, and a symbolic child value
is incomparable to a literal parent bound, hence refused (never
admitted on the symbol's name alone). Second, a symbol that is
operator- or plug-bound rather than spawn-bound (`config.job_root` as a
root component's config, or a 378 plug-time value) never meets a source
literal, so the 411 plan gate would be comparing a SYMBOL against a
concrete mount. The manifest closes that seam: the deployment manifest
may bind the symbol (`config.job_root := "/srv/ingest"`), the plan gate
substitutes the manifest binding and compares the resolved literal
against the envelope, and an unbound symbol at plan time refuses the
plan. The manifest binding is deployer data in the deployer's file,
which is the crux table's own placement rule.

### (b) Manifest data (rejected as the primary surface, retained as the enforcement surface)

Spelling bounds only in placement/sandbox TOML would keep the grammar
closed, but it puts author knowledge in the operator's file, cannot feed
the item-66 spawn check (which runs at compile admission, before any
manifest exists), and would make the audit surface dumber than the
deployment. The manifest keeps its 411 role: it is where a declared
parameter is MAPPED to an enforced grant (a mount, a net rule), and the
plan-time gate compares the two. Declaration in source, enforcement
mapping in manifest, one order across both.

### (c) Operator-minted grants only (rejected as the primary surface, retained as the lease surface)

The lease HALF of 294 stays operator-side and grant-shaped, because
consent duration is operator knowledge and the runtime is shipped. What
source gains is not a second lease mechanism but a declaration that an
authority is lease-shaped, and lifecycle triggers (task, realm,
generation) that the ledger consumes; the reconciliation section
specifies each. Numeric bounds that are part of the program's meaning
(a per-activation call ceiling) may be declared in source as parameters
(`model.complete(calls=3)`) and are checked like any parameter; the
runtime counter that enforces a per-session count is the grant's, not a
new one.

**Recommendation.** Split by axis, matching the crux table: resource
parameters and static numeric bounds in the SOURCE token grammar (option
a, with the config-key substitution rule for per-instance values); lease
lifetimes and revocation operator-side on the SHIPPED grant ledger (option
c as it already exists, extended with new triggers, not a new mechanism);
the manifest as the 411 enforcement bridge only (option b in its existing
role). This is the 363/411 precedent applied at finer grain: each datum in
the hands of the party that knows it.

## The extended algebra (question 2)

### The order on one token's cone

A capability becomes a pair `(T, P)`: a token `T` (exactly today's dotted
token) and a valuation `P`, a finite partial map from parameter names to
values. A bare token is `(T, {})`. Define `(T, P) <= (T', P')` iff:

1. `T = T'` (tokens remain discrete: no order between distinct tokens,
   exactly as today; parameterization never bridges tokens), and
2. for every parameter `k` bound in `P'` (the wider side), `P` also binds
   `k` and `P[k] <=_k P'[k]` in that parameter's value order.

Parameters bound in `P` but absent from `P'` are free in the wider side
and only narrow. Consequences, each load-bearing:

- **A bare token is the top of its cone.** `(T, {})` is above every
  `(T, P)`: today's `fs.write` means all of `fs.write`, unchanged.
- **Adding a parameter only ever narrows.** `(T, P + {k: v}) <= (T, P)`
  by construction, since `k` is unbound in `P`. This is the security
  invariant in one line (question 6).
- **Dropping a parameter widens and is refused.** A child declaring bare
  `fs.write` under a parent holding `fs.write(path="/tmp")` fails clause
  2 (`path` bound in the parent, unbound in the child).
- **`*` stays strictly top and outside the scheme.** `*` takes no
  parameters (parse refusal) and no `(T, P)` ever covers `*`. The
  attenuation over-approximation is untouched.

### Per-parameter value orders

Each parameter name carries ONE value order, fixed by a small registry
(a pure table in the checker; adding a parameter kind is adding a row,
with its order and its canonicalization):

- **`path` (containment).** `a <=_path b` iff the canonicalized COMPONENT
  list of `b` is a prefix of `a`'s. Canonicalization is lexical only and
  fully specified: split on `/`, drop a single trailing slash (`/tmp/`
  canonicalizes to `/tmp`, one spelling per cone), refuse `..` and `.`
  and empty components, refuse a non-absolute path, and refuse `"/"`
  itself (a valid path value has at least one component; a root-wide
  "narrowing" narrows nothing, and everything is already spelled by the
  bare token). All refusals at parse time, each with an exit test.
  Explicitly component-wise, never string-prefix:
  `/tmp/job-42 <= /tmp` holds; `/tmp/jobber <= /tmp/job` does NOT (the
  classic prefix bug, an exit test below). Symlinks, case folding, and
  Unicode normalization are runtime facts about a real filesystem; the
  static order does not claim them (the enforcement ledger and 411 own
  that).
- **exact-match parameters (`table`, `host`).** Discrete: `a <=_k b` iff
  `a = b`. `db.read(table="orders") <= db.read` holds (clause on absent
  parameters), `table="orders" <= table="users"` does not. A richer host
  order (subdomain containment) is a registry row for later, not designed
  here.
- **The registry is CLOSED.** A parameter name not in the registry is
  refused at parse, never defaulted to the discrete order. A discrete
  default would make security invariant 4 false as written, and it
  opens a typo hazard: `pth="/data"` would parse, narrow nothing, and
  partition the token's cone so that revoke-by-cone misses grants
  spelled with the typo. Adding a parameter kind is adding a registry
  row, and the parse refusal names the registered names.
- **numeric ceilings (`calls`, `bytes`, and friends spelled `k<=N` or
  `k=N` meaning "at most N").** `N <=_k M` iff `N <= M` as integers: a
  smaller ceiling is narrower. `calls=10 <= calls=100`. Ceiling-kind
  parameters carry a second rule the resource kinds do not, because a
  CROSSING binds no `calls` (one call is one call; there is nothing on
  the crossing to compare), so under the plain coverage clause a grant
  carrying `calls=3` would cover no crossing ever and the feature would
  ship broken. The rule: ceiling parameters participate in
  declaration-to-declaration comparisons (spawn, G4) and in the
  mint-vs-declaration bound, are ERASED from the grant's valuation at
  mint and translated into the grant's `remainingUses` counter (the
  shipped enforcement), and are EXEMPT from crossing-coverage
  comparison. Each registry row is marked resource-kind (compared at
  crossings) or ceiling-kind (erased at mint); the kind is part of the
  row.
- **lease instants and durations (operator-side; listed for the one
  order's completeness).** A grant with earlier `expiresAt` is below one
  with later; a bound `expiresAt` is below an absent one; `remainingUses`
  orders as a numeric ceiling. This is how "a shorter lease subset-of a
  longer one" is the SAME clause-2 comparison, evaluated in the ledger
  instead of the checker.
- **symbolic values (`path=config.job_root`).** Comparable only to the
  identical symbol, or to any value after spawn-site literal substitution
  (the surface section; plan-time, a manifest symbol binding substitutes
  the same way). Incomparable otherwise, and incomparable means
  REFUSED, never admitted (fail closed).

Top of the whole order remains `*`; top of each cone is the bare token;
bottom is absence (the empty capability set: revl already refuses
`emission[]` at parse, `parser.py:1560`, because forbidding every crossing
is spelled by dropping the modifier, and 294 keeps that rule; no
per-token unsatisfiable valuation is representable, deny is non-mention).
Reflexivity, antisymmetry, and transitivity hold per parameter order and
lift through the product; the set-level check below preserves them.

### The set-level check (what item 66's code actually computes)

Attenuation compares SETS. Today: `reach(child) subset-of held(parent)`,
element identity. Extended: coverage,

```
admit  iff  for every c in reach(child), there exists h in held(parent)
            with c <= h
```

Set inclusion is the special case where every valuation is empty, so every
program admitted today is admitted unchanged (compatibility section). Two
parent entries with the same token and different valuations cover the
union of their cones; no meet/join needs computing, the check is per-child
element an existential scan, and `held` stays a small set, so the cost is
the same order as today's.

### The key-to-token bridge (what the fold must actually compare)

The coverage formula above is vacuous unless the fold's ELEMENTS are the
parameterized tokens, and today they are not (the seam named in the
background). `_check_spawn_attenuation` compares wiring keys: for an
emit step, `_collect_emit_caps` contributes the requires-target name
(`fs`), and `_held_capabilities` is requires keys plus emission caps.
With bare tokens, key and declared token coincide often enough for the
check to be real; with `fs.write(path="/data/incoming")` the key is
`fs`, the declared token never reaches the fold, the child's reach
silently degrades to bare `fs`, and the parent's `fs` covers it. The
"statically checked" cell for parameter subset would then be false
while reading true: the 414 fold-misses-a-crossing bug class, sitting
in the flagship check of this design.

The fix is a BRIDGE, specified here so slice 1 cannot skip it. At each
emit step the fold resolves key -> requires-target service -> the
method being called -> that method's `emission[...]` token WITH its
valuation, and contributes that `(T, P)` to `reach`. `held` is built by
the same resolution over the parent's requires surface and its own
emissions, so BOTH sides of `covers` are declared valuations, never
wiring keys. The bridge is one resolution function applied to both
sides; a key with no resolvable declared token contributes `*` (today's
over-approximation, unchanged). Because the same resolution transforms
both sides, key-and-token-coincident programs keep their admission
verdicts, and the parameter-free byte-identity exit test doubles as the
regression guard for that.

Slice 1 must carry a 414-reach-completeness-style test proving the
valuation survives the bridge INTO the fold: a program whose only
narrowing violation lives in the valuation (parent holds
`fs.write(path="/tmp")`, child's method declares
`fs.write(path="/etc")`) must be refused. If that test passes with the
bridge stubbed out, the fold is comparing keys again.

The same coverage relation, evaluated in the other shipped comparisons,
completes the algebra with no second definition: G4 (a body's crossing
`(T, P_use)` must be covered by the declaration's `(T, P_decl)`), the
item-33 policy and item-55 operator checks when they meet parameterized
tokens, the 411 plan gate (declared parameter vs envelope grant), and the
grant ledger (next section). One relation, one implementation (a small
pure module, `cap_order`, imported by lower, the gate, and placement),
every consumer.

### The representation mandate (one `(T, P)` everywhere)

One relation is not enough if the value flowing through the system is a
string that only `cap_order` knows how to read. A capability with a
valuation is mandated to travel as a structured `(T, P)` pair (or,
equivalently, one canonical spelling parsed at a single point inside
`cap_order` and nowhere else) across EVERY 414 fold boundary: the
ClassMap, `policy.component_reach`, the untrusted-author sweep, the
seam check, the taint fold, and the attenuation fold. Two concrete
corruptions this rule prevents, both known bug shapes:

- **String-distinct, cone-equal.** If `fs.write(path="/tmp")` is a
  distinct string to `Approval[C]` matching or the item-33 policy
  tables, a typed approval for bare `fs.write` fails to cover a
  parameterized crossing in its own cone, and every policy rule written
  against the bare token silently stops matching. Approval and policy
  matching go through `covers` on the structured pair, so a bare-token
  approval covers its whole cone (the top-of-cone rule doing its job).
- **Fold-side string surgery.** Any fold that splits a capability on
  `.` (the realm-dotting convention) would mangle
  `production.pay(host="x")` if the valuation rides in the string. With
  the structured pair, realm dotting operates on `T` and never sees
  `P`.

Each in-scope fold carries a 414-cell test asserting the valuation is
SEEN on the far side of the fold (a narrowing that exists only in `P`
must change that fold's verdict), the same discipline as the
reach-completeness test on the spawn bridge.

### Grant coverage under parameters

`_grant_covers` is deliberately identity-on-token (`session.py:1942`) and
the F1 hole closed on that line. The extension must not reopen it:

```
_grant_covers(grant, crossing)  iff  crossing <= grant     (in the one order)
```

Same token required (clause 1), crossing's valuation within the grant's
(clause 2). Properties preserved: a grant for `A` still never covers `B`
(distinct tokens stay discrete); a grant minted for
`fs.write(path="/tmp")` covers the `fs.write(path="/tmp/job-42")`
crossing and does NOT cover bare `fs.write` (that would be widening); a
bare-token grant covers its whole cone, which is exactly today's
semantics, so every existing mint/revoke pair behaves identically.
Mint-side, `mint_standing_grant` gains the parameterized spelling for its
`capability` argument and refuses a mint WIDER than what the ticket or
class map declares (a grant may only widen prompting convenience, never
authority: the existing `capability not on this ticket's reach` refusal,
`session.py:2082`, generalizes to a coverage check). Revoke-side, item
379 retires by the same predicate, so revoking `fs.write(path="/tmp")`
retires every grant at or below it and no other.

Three more lookups must become cone-aware, or mint-narrow (the headline
improvement over 344) is refused dead on arrival.
`crossings_for_capability` (mint-side), the F5b revoke ambiguity guard,
and the operator.py capability resolution are string-keyed today: they
look a capability up by exact spelling in the class map. A narrow mint
spelling `fs.write(path="/tmp")` finds no class-map entry (the map
declares `fs.write(path="/data/incoming")` or bare `fs.write`) and
resolves to nothing, so the very grant this design exists to enable is
refused. The rule: each of these lookups matches a declared crossing
capability that COVERS the requested grant (the requested valuation at
or below a declared one), via the same `cap_order.covers`, never string
equality. The F5b ambiguity guard restates over cones: a spelling
covered by MORE than one declared crossing capability with distinct
cones is ambiguous and refused with both candidates named, exactly as
the string-keyed guard refuses an ambiguous suffix today.

## Runtime enforcement: the honest ledger (question 3)

What each bound actually gets, stated in 411's registers (enforced /
declared / advisory), because a parameter that READS like confinement but
is only a declaration is exactly the dishonesty this design must not ship:

| bound | statically checked (admission) | runtime-enforced | merely declared |
|-------|-------------------------------|------------------|-----------------|
| token subset (today's 66) | yes, spawn + G4 | n/a | |
| parameter subset (path containment, table/host match) | at G4 and the plan gate: yes. At spawn: yes ONLY through the key-to-token bridge; without the bridge the fold compares wiring keys and this cell is false | no, by revl alone | the VALUE's relation to what the body actually does |
| ceiling parameter (`calls=N` as declared) | yes: declaration-to-declaration (spawn, G4) and mint-vs-declaration; exempt from crossing coverage | yes: erased at mint into the grant's `remainingUses` | |
| call-count lease | mint-time shape | yes: `remainingUses`, consume-before-fire WAL (`_consume_grant`) | |
| duration lease | mint-time shape | yes: `expiresAt` checked at the crossing (`_live_grant_for`, invariant 3) | |
| generation lease | | yes: `candidateHash` liveness (a swap invalidates) | |
| session binding | | yes: `session` liveness | |
| approval binding | | yes: 245/246 ticket / typed `Approval[C]`, consume-before-fire | |
| task lease | | yes via teardown, ttl-backstopped on crash (reconciliation below): revoke rides the verified LIFO disposer chain; a SIGKILL that skips teardown leaves the grant to lapse at its mandatory ttl | |
| realm lease | | partial: generation liveness of the realm's composition | the realm boundary itself, absent item 33 runtime policy |
| `path=` against the actual bytes the host body touches | | ONLY under 411 (an `fs:path` mount covering the declared value), or a first-party stdlib extern body that self-checks (the 244/245 workspace-root guard precedent in `fs.rvl` bodies) | otherwise: declared + audited, exactly item 373's register |
| `host=` / `table=` against actual egress / actual SQL | | ONLY under 411 (net rule) / a self-checking first-party body | otherwise declared + audited |

Three honest sentences the docs and the audit surface must carry:

1. **The subset relation is always checked.** Whatever a parameter's
   enforcement status, a child, a body, a grant, or a mint that claims
   MORE than its parent declared is refused at admission. That is real
   security value on its own (a reviewable, machine-checked narrowing
   chain), but it bounds declarations, not host behavior; and at spawn
   it is only true through the key-to-token bridge, which is why the
   bridge is specified in the algebra section and carries its own
   completeness test.
2. **Leases are enforced where the gate runs.** The counter and clock
   live in the session gate (`mcp/session.py`); a class-(c) crossing
   cannot fire past an exhausted or expired grant, on py with WAL
   durability, and ownerless tiers follow the 246 Slice-2 discipline
   (refuse at emit until a tier-side gate exists). An UNGATED run (no
   approval policy loaded) has no leases, and the audit surface must say
   so rather than print a lease that nothing spends.
3. **A resource parameter is enforced only by an enforcer.** A path
   value is a G8-opaque host detail; the body opens whatever it opens.
   revl DECLARES and REQUESTS `path="/tmp/job-42"` and, under 411,
   refuses a sandbox whose mounts do not cover it; the container runtime
   is the trusted enforcer, exactly as 411 states for its whole
   envelope. Without 411 (or a self-checking first-party body), the
   parameter is item-373-class: on the audit surface, weakening flagged
   by `audit --diff`, honored by review. `revl audit` prints the status
   per parameterized capability (`enforced: mount` / `enforced: body` /
   `declared`), so no reader mistakes a declaration for a jail.

## The six lease bindings, reconciled (question 4)

Each binding, against the shipped machinery; "reuse" means the mechanism
exists and 294 adds only spelling or a trigger.

| binding | mechanism | verdict |
|---------|-----------|---------|
| duration | grant `expiresAt`, clock at the crossing (`_live_grant_for` invariant 3); TTL from mint or policy `requires approval ttl` | REUSE, nothing new |
| call count | grant `remainingUses`, consume-before-fire (`_consume_grant` + WAL) | REUSE, nothing new |
| human approval | the 245/246 ticket (single-use, hash-bound) and typed `Approval[C]` with TTL; a standing lease minted FROM a ticket (`mint_standing_grant(ticket_hash=...)`) | REUSE, nothing new: the approval IS the lease |
| component generation | grant `candidateHash` + per-generation class map: a swap/rollback recomputes the hash, the stale grant fails liveness with no bookkeeping (`session.py:1959`) | REUSE; today it is an implicit consequence, 294 names it as the DEFAULT lease binding and documents it |
| task | NEW TRIGGER on shipped parts: a lease acquired in a task scope is a resource, so its revocation rides the verified LIFO teardown that is revl's own differentiator: `let l = effect lease fs.write(path=...) ttl 10m undo l.revoke()` lowers to a TICKET-MEDIATED acquisition (next subsection: acquisition is class-(c)-gated, never a free mint) whose disposer calls the 379 revoke verb. Task ends (or unwinds), teardown fires, grant retired. The runtime half (ticket, mint, revoke, liveness) is shipped; the new code is the source form, its lowering, and the acquisition gate | REUSE runtime + NEW spelling/lowering, acquisition gated |
| realm | COMPOSITION of shipped parts: a realm-dotted token (343) leased under the realm's generation; retiring or swapping the realm's composition changes the candidate hashes under it, which lapses the leases (the generation row). An explicit `revoke_standing_grant(capability="production.*")`-style prefix revoke is NOT designed here (it would widen the revoke predicate); revoking a realm is revoking its listed grants, enumerable from the ledger. That enumerate-then-revoke walk is not atomic: a crossing can spend a use between the enumeration and that grant's retirement, so realm revocation is eventual over the walk, not a fence (noted honestly, not fixed here; the generation lapse IS a fence when the composition itself changes) | REUSE + one documented pattern; no new predicate |

Early revocation for all six is item 379, unchanged: retire by the same
coverage predicate that finds. Nothing in the table mints a second ledger,
a second clock, a second counter, or a second revoke path.

### Lease acquisition is gated (the self-mint hole, closed)

The task-lease lowering above has a consent bypass hiding in it, and
the rule that closes it is load-bearing, not an implementation detail.
If `effect lease fs.write(path=x) ttl 10m undo l.revoke()` lowered to a
plain ungated `mint_standing_grant` call, any program could
self-convert prompt-per-call into prompt-never for the ttl: the
crossing that would have raised a 245 ticket instead finds a live grant
the program minted for itself. That is a consent bypass built from this
design's own parts. `mint_standing_grant` is an OPERATOR verb, gated by
the `approve` authority; the source form must not sidestep that gate.

The rule: lease ACQUISITION is itself a class-(c)-gated operation. The
`effect lease` acquisition raises a ticket naming the capability cone
and the ttl; the human or the loaded policy approves ONCE; the grant is
minted FROM that ticket (`mint_standing_grant(ticket_hash=...)`, the
shipped 246 path). The consent economics are exactly the item-248
measurement: 3 prompts collapse to 1, never to 0. The one prompt moves
from the first crossing to the acquisition; it does not disappear. An
ungated run (no operator policy loaded, no gate to raise the ticket to)
REFUSES `effect lease` at the acquisition rather than silently minting:
an unenforceable lease is not a lease, and honest sentence 2 above
already commits the design to refusing that pretense.

The disposer needs one scoped exemption, and only one: `l.revoke()`
retires the grant minted from the acquiring scope's own ticket (matched
by requestId), which is the always-safe direction (revoking your OWN
authority only narrows). The exemption does not extend to any other
grant; a disposer naming a grant it did not mint goes through the
ordinary 379 gate.

The crash caveat, stated because the honesty table must not overclaim:
the disposer does not run on SIGKILL. The grant then survives until its
ttl, which mandatory boundedness guarantees exists (an unbounded lease
is refused at mint, the shipped `session.py:2118` rule). Session
binding bounds the residue further: a WAL replay into a new session
fails liveness invariant 5, so the orphaned grant cannot be spent from
a successor session. Bounded residue, not zero residue; the ledger's
task-lease row reads "yes via teardown, ttl-backstopped on crash" for
exactly this reason.

## Compatibility

- **No parameters, no change.** The grammar extension is opt-in; every
  existing token parses to `(T, {})`; coverage degenerates to identity;
  `_grant_covers` on bare tokens is bit-for-bit today's predicate; the
  key-to-token bridge transforms both sides of the spawn check with the
  same resolution, so key-and-token-coincident programs keep their
  verdicts. Admission decisions, emitted code, manifests, and audit
  output for parameter-free programs are byte-identical (an exit test).
- **The checker change is admission-only in slice 1.** No backend emits
  differently for a parameterized declaration (the parameter is checked,
  audited, and, under 411, planned against; it does not codegen). The
  roadmap's "all six backends must handle bounded subsumption" cost is
  therefore deferred to the tier-side gate slice, where it belongs, and
  the wasm least-authority policy (item 289) consumes the same one
  `cap_order` relation when it lands.
- **Audit surface is additive.** The attenuation chain
  (`manifest.instances`) gains parameter detail on edges that have it;
  spawn-free or parameter-free compositions keep byte-identical
  manifests (the item-66 discipline, `docs/capability-attenuation.md`).

## Security: the no-widening argument (question 6)

The cap-algebra review confirmed three invariants; each survives, and the
argument is short enough to state completely:

1. **Monotone shrinkage down the lineage.** The extended check admits
   `reach(child)` covered by `held(parent)` in a partial order where
   adding a parameter is provably downward (clause 2: a parameter bound
   only on the narrow side is free on the wide side) and dropping one is
   provably upward (hence refused). A parameter can therefore never
   grant more; the worst a wrong parameter can do is refuse a program
   that should be admitted, which is the fail direction revl prefers.
2. **The `*` row.** `*` takes no parameters, tops the order, and is
   covered by nothing parameterized; approval of `*` stays refused. The
   over-approximation that keeps an amplifier from hiding behind an
   unnameable boundary is untouched.
3. **Grant coverage cannot re-widen.** `_grant_covers` extends
   downward-only within one token: the F1 property (a grant for A never
   covers B) holds because clause 1 is token identity; the new
   possibility, a narrow grant declining to cover a wide crossing, is
   fail-closed (the crossing prompts, exactly as an uncovered one does
   today). Mint is bounded by the ticket/class-map declaration through
   the same relation, so an operator cannot mint above the program's
   own declaration.
4. **Incomparable means refused.** Unregistered parameter names never
   reach a comparison at all (the registry is closed; refusal at
   parse). Symbolic values against literals and malformed paths refuse
   at the comparison with the pair named, never an admit. No default-
   allow row exists anywhere in the design.
5. **The order itself is the attack surface, so it stays tiny.** The
   registry has three orders (component-prefix, discrete, integer) plus
   symbol identity; the component-prefix canonicalizer is the one
   subtle function and carries the `/tmp/jobber` exit test. Everything
   else is equality and integer comparison.
6. **A program cannot mint its own consent.** `effect lease`
   acquisition is class-(c)-gated and ticket-mediated (the lease
   subsection): the path from source to a standing grant always passes
   the same gate a bare crossing would, once instead of per call, and
   an ungated run refuses the form. No source spelling reaches
   `mint_standing_grant` without a ticket.

One deliberate weakening-shaped surface exists and is named: a grant
minted for a WIDE valuation (bare `fs.write`) auto-approves narrow
crossings that a human might have believed were individually gated. That
is item 344's existing semantics, not new to 294; 294 improves it (the
operator can now mint narrow), and the mint-time output prints the cone
being granted.

## Staged plan (question 5)

Each slice lands green alone; later slices need earlier ones.

**Slice 1: grammar + the order + the checks (frontend only).**
Parser: parameter lists on dotted tokens in `_capability_list` /
`_capability_token` (literals + `config.` symbols; refusals: params on
`*`, duplicate keys, unregistered parameter names, malformed paths
including `"/"` and `.` / `..` components; trailing slash
canonicalized away; `emission[]` unchanged).
New pure module `src/revl/cap_order.py`: the structured `(T, P)` pair,
the CLOSED registry (path/discrete/integer/symbol, each row marked
resource-kind or ceiling-kind), `covers(a, b)`, `covers_set`, and the
single parse point for the canonical spelling (the representation
mandate).
Lower: the key-to-token bridge first (emit step -> requires-target
method -> its `emission[...]` valuation, one resolution applied to BOTH
reach and held), then `_check_spawn_attenuation`, G4 bounds, and the
emission fold compare via `covers`; the bridge's reach-completeness
test and the per-fold 414-cell tests land in this slice; refusal
messages name token, parameter, both values, and the direction ("a
parameter only narrows; `fs.write` without `path` is wider than
`fs.write(path=\"/tmp\")`").
Audit: chain and capability table print valuations; `audit --diff` flags
a parameter widening as it flags a new crossing.
Byte-identity: parameter-free corpus unchanged (goldens).

**Slice 2: per-instance substitution.** Spawn-site `with { }` literal
substitution into `config.`-valued parameters; per-instance attenuation
chain shows resolved values; unresolved symbols stay symbol-compared.

**Slice 3: the lease surface on the shipped ledger (gate side).**
`mint_standing_grant` / `revoke_standing_grant` accept parameterized
capability spellings; `_grant_covers` becomes `cap_order.covers`;
`crossings_for_capability`, the F5b revoke guard, and operator.py
resolution become cone-aware; ceiling parameters erase into
`remainingUses` at mint; mint refuses above-declaration mints; the
`effect lease ... undo l.revoke()` source form lowers to the
TICKET-MEDIATED acquisition (class-(c) gate, mint from `ticket_hash`,
refusal when ungated) plus the disposer-revoke with its own-requestId
exemption (the task binding); docs name the generation binding as the
default lease. WAL records carry the valuation. Harness-visible:
`revl_approve` gains the narrow spelling.

**Slice 4: the 411 bridge (with, not in, 411's implementation).** The
needs table maps `path=` to an `fs:path` mount and `host=` to a net
rule; the plan gate refuses an envelope not covering a declared
parameter; audit prints per-parameter enforcement status
(`enforced: mount` / `enforced: body` / `declared`). First-party
`fs.rvl` bodies honor `path=` in-body (workspace-root-guard style).

**Slice 5: tier-side lease gate + item 289 consumption.** Ownerless
tiers move from refuse-at-emit to a real gate where wanted; the wasm
policy check reuses `cap_order`. Out of 294's critical path.

## Exit tests

Grammar and order:
- `emission[fs.write(path="/data/incoming")]` parses; `*`-with-params,
  duplicate keys, relative path, `..` and `.` components, `path="/"`,
  and an unregistered parameter name (`pth="/data"`) all refuse with
  the documented messages.
- `path="/tmp/"` canonicalizes to `/tmp`: the two spellings are
  cone-equal (one grant covers crossings spelled either way).
- Order unit tests: `/tmp/job-42 <= /tmp`; `/tmp/jobber not<= /tmp/job`
  (component-wise, the string-prefix bug); `calls=10 <= calls=100`;
  `table="orders" not<= table="users"`; bare token tops its cone;
  dropped parameter refused; distinct tokens incomparable; symbol
  identity only.

Attenuation and G4:
- Bridge reach-completeness: a program whose only violation lives in
  the valuation (parent holds `fs.write(path="/tmp")`, child's method
  declares `fs.write(path="/etc")`) is refused, proving the valuation
  crossed the key-to-token bridge into the fold instead of degrading
  to a bare `fs` key match.
- One 414-cell test per in-scope fold (ClassMap,
  `policy.component_reach`, untrusted-author sweep, seam, taint,
  attenuation): a narrowing that exists only in `P` changes that
  fold's verdict; realm dotting on `production.pay(host="x")` leaves
  the valuation intact.
- `examples/rejections/g4_spawn_widens_parameter.rvl`: parent holds
  `fs.write(path="/tmp")`, child declares bare `fs.write`; refused,
  message names the parameter and the direction.
- Positive twin: child `fs.write(path="/tmp/job-42")` admitted; audit
  chain shows the narrowing and the dropped breadth.
- Per-instance: two spawns binding `config.job_root` to two literals;
  chain shows two resolved paths; a third spawn binding a path outside
  the parent's bound refused.
- Byte-identity: full existing corpus (parameter-free) admits and emits
  identically; `tenant_attenuation` manifest unchanged.

Gate and lease:
- Grant for `fs.write(path="/tmp")` auto-approves a
  `path="/tmp/job-42"` crossing, does not cover bare `fs.write` or
  `db.read`; exhausted uses and lapsed TTL prompt again (existing tests
  extended, not replaced).
- Mint wider than the ticket's declared valuation refused.
- Cone-aware mint: `mint_standing_grant` for `fs.write(path="/tmp")`
  resolves against a class map declaring bare `fs.write` (coverage,
  not string equality); a mint covering NO declared crossing is
  refused, and one ambiguously covered by two declared cones is
  refused with both candidates named (F5b over cones).
- Ceiling erasure: a grant minted from `model.complete(calls=3)`
  carries `remainingUses = 3` and no `calls` in its valuation; the
  third crossing fires, the fourth prompts.
- Approval cone: a typed `Approval[fs.write]` covers a
  `fs.write(path="/tmp/job-42")` crossing (the representation
  mandate's string-distinct cone-equal case).
- Revoke by parameterized capability retires exactly the cone.
- Task lease: in a GATED run, `effect lease` raises ONE ticket, the
  grant mints from that ticket, the task body crosses under it
  promptless (the 248 shape: 3 prompts to 1, never 0), teardown
  revokes, a post-teardown crossing prompts; unwind path (failure
  mid-task) also revokes (LIFO teardown test shape).
- Ungated refusal: `effect lease` in a run with no operator policy
  refuses at acquisition, it does not silently mint.
- Disposer exemption: `l.revoke()` retires the disposer's own grant
  (matched by requestId) without a prompt; a disposer naming any other
  grant goes through the 379 gate.
- Crash residue: a lease grant orphaned by a killed session is
  unspendable from a successor session (WAL replay fails liveness
  invariant 5) and lapses at its ttl.
- Generation lease: swap invalidates the lease (existing candidateHash
  test, renamed as the documented binding).

Honesty surface:
- `revl audit` on a parameterized, unsandboxed program prints
  `declared` for `path=`; the same program under a 411 plan with a
  covering mount prints `enforced: mount`; a plan whose mounts do not
  cover the declaration is refused at plan time (411-side test, written
  with 411).

## Scoped out (and to whom)

- **Actual resource enforcement** of `path=` / `host=` / `table=`
  against host-body behavior: item 411 (mounts, net rules, the trusted
  enforcer, the plan gate), plus first-party body self-checks. 294 owns
  the declaration, the order, and the honesty labeling only.
- **Wasm host-import subsetting**: item 289 consumes `cap_order`.
- **Byte/time budgets with runtime metering**
  (`budget.bytes=10MB, budget.time=2s`, the external-proposal fold at
  `docs/v2.0-roadmap.md:3696`): the ORDER rows are designed here
  (numeric ceilings) so the spelling will slot in, but metering is a
  runtime revl does not have and is not claimed; that fold stays with
  the cost-ceilings item.
- **Richer host orders** (subdomain containment, table wildcard): a
  registry row each, added when a consumer exists.
- **Cross-realm prefix revocation**: refused above to keep the revoke
  predicate identical to the coverage predicate.

## Open questions

1. **Where does the `k<=N` spelling settle?** The SEMANTICS are settled
   above and not open: ceiling-kind, compared declaration-to-declaration
   and mint-vs-declaration, erased into `remainingUses` at mint, exempt
   from crossing coverage. Only the surface spelling is open: `calls=3`
   reading as "at most 3" is compact but reads as equality; `calls<=3`
   is honest but spends an operator in the token grammar. Slice 1
   should pick one and refuse the other, not accept both.
2. **Does a parameterized capability appear in `Approval[C]` in slice 1
   or slice 3?** The parse is shared, so it comes for free; the typed-
   approval TTL semantics under a valuation need a sentence in the 246
   docs either way.
3. **Task-lease granularity.** The design binds the lease to the
   acquiring SCOPE via teardown; whether a spawned child's crossings may
   spend the parent's task lease (lineage-shared lease) or each task
   mints its own is deliberately left to the slice-3 implementer with a
   default of NOT shared (narrower, fail-closed, relaxable later).
