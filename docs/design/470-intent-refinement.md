# 470: Intent-vs-action refinement verification

Filed against GitHub issue [#822](https://github.com/inso1337/revl/issues/822)
("Intent-vs-action refinement verification"), roadmap item 470. Design plus
three slices: the semantic kernel `src/revl/intent.py` (the `Intent` record, the
`Action` record, and `refine`, with unit coverage in
`tests/test_470_intent_refinement.py`), and the source surface that finally
holds a declaration and compares a crossing against it (the `within` and
`acting` clauses, with coverage in `tests/test_470_intent_surface.py`). Section
3 names the four landed lines and section 4 names the stages that stay open.

Companion docs: [441-goal-contracts.md](441-goal-contracts.md),
[442-typed-delegation.md](442-typed-delegation.md),
[246-auto-approve.md](246-auto-approve.md), [../boundary-policy.md](../boundary-policy.md),
[../capabilities.md](../capabilities.md).

Source of record for the claims about today's code (as of the branch base):
`src/revl/cap_order.py` (`Cap`, `covers`, `is_ceiling`, `split_ceilings`,
`_REGISTRY`, `_ALIASES`), `src/revl/mcp/session.py` (`_approval_step`,
`_grant_covers`, `_grant_within`, `_live_grant_for`), `src/revl/mcp/approval.py`
(`ClassMap.build_ticket`, `_cap_covers`), `src/revl/mcp/operator.py`
(`TOOL_VERB`, `COMPOSED_TOOL_VERB`), `docs/boundary-policy.md` (the rule
grammar), `docs/capabilities.md` (G4 capability-scoped emission).

## 1. The gap, stated against the code

Item 470's exit criterion is narrow and precise: an action that exceeds its
declared intent (a wider verb, a higher amount, a different tenant, or an extra
capability) "is refused with the intent it violated". The refusal part of that
already mostly exists. The *with the intent it violated* part does not, because
the four dimensions are checked today in four places, in four vocabularies, and
not one of them is a comparison against a stated declaration:

- **The object** is `cap_order.covers`, the tree's one partial order (token
  identity plus containment in the `path` order and equality on the discrete
  kinds). It is a fine order, but nothing in it holds a *declaration* to compare
  against: every gate compares an authority against a request, both of which were
  computed at the same moment, so the refusal can name the capability reached but
  never the intent exceeded. A probe on the branch confirms the neighbouring G4
  emission check is not this: `emission[fs.write(path="/etc")]` under a declared
  `emission[fs.write(path="/tmp")]` is refused because the emission set compares
  *spellings* against the declaration, not cones under `covers`. That check is
  name-based and must not be repurposed as an object refinement without changing
  what it refuses.
- **The tenant** exists as the item-33 realm, and `docs/boundary-policy.md`
  states its rule as composition *shape* ("tenants never reach each other"), not
  as "this action must run in the realm the intent named". A realm is compared
  for reach, not for conformance to a declared realm.
- **The verb** has no home at all. The boundary policy's patterns are flat globs
  over boundary tokens (`component Agent* may reach db`, `realm billing may reach
  db, ledger`), so nothing in the tree can express "may reach `db`, but only to
  `execute` on it". The word "verb" in `src/revl/mcp/operator.py` is a different
  concept entirely: `TOOL_VERB` maps MCP tool names onto *operator management*
  actions (load / swap / approve), not onto operations performed at a boundary.
- **No hidden extra capability** is the all-or-nothing scan over a ticket's
  capability set in `_approval_step`. Its refusal names the capabilities, not the
  intent, and it is a property of the ticket rather than of a declaration.
- **The ceiling** is the sharpest case. `cap_order`'s ceiling registry
  (`calls`, `size`, `time`, with aliases `requests`, `bytes`) is deliberately
  *erased* at mint into the shipped `remainingUses` counter and *exempt* from
  crossing-coverage (`cap_order.is_ceiling`); only
  `lower._check_spawn_attenuation` compares ceiling params, and it compares two
  *declarations*, not a declaration against a moment of spend. So a crossing is
  never compared against a stated amount at the moment it fires.

The roadmap's scope note closes the obvious loophole in the other direction:
this is "refinement checking against a declared intent, not inferring an intent
the caller never stated". Nothing in this item may guess what the caller meant.

## 2. Mapping the contract onto existing surfaces

The contract is one record of what was declared, one record of what is about to
run, and one decision function. Each dimension maps onto an existing notion
rather than a new one:

- **object** is `cap_order.Cap` and the decision is `cap_order.covers`, with each
  capability the intent names as the wider side. The kernel does not reimplement
  the order: "same or related object" is exactly `covers`' job, and a second order
  would be a second source of truth. The only thing `related` widens is the SET of
  points the order is read against, and only because the intent named them. What the kernel adds is the *declaration* being held across
  time and the split refusal: `EXTRA_CAPABILITY` when the token differs (the
  confused-deputy case, a capability the intent never named) and `OBJECT` when
  the same token is reached outside the declared cone (a sibling path, another
  host, a dropped parameter, which is a *wider* request rather than a
  relaxing one).
- **ceiling** is `cap_order`'s closed ceiling registry, canonicalized through
  `ceiling_params`, which refuses a resource-kind name. A resource parameter
  smuggled into the ceiling dimension, or a ceiling parameter smuggled into
  `object`, would let one bound be compared twice by two rules with the weaker
  one winning; both records refuse that at construction.
- **tenant** is the item-33 realm, carried as a string or an explicit `None`.
- **verb** is one of the two genuinely new fields. It is a set of operation names
  permitted on the object, and it is not a spelling of anything that exists
  today.
- **scope** is the other, and it is new because of its ORDER rather than its
  subject: it is a named set compared by containment, which is the only one of the
  six clauses that neither a single-valued `Cap` valuation nor a `<=` ceiling can
  express. The issue's recipients are one such set, the coarse data classes item
  249's taint model tracks are another, and naming the scope is what keeps the
  kernel from inventing a recipient concept the tree does not have.

The typed link the issue asks for is `Intent.from_cap` and `Action.from_cap`: the
*same* capability spelling the policy, the G4 check and the approval gate already
speak (`fs.write(path="/tmp", calls=3)`) becomes the named dimensions on either
side, with the ceiling parameters peeled off by `cap_order.split_ceilings`, the one
place that split is defined.

## 3. The slices that have landed

### 3.1 Slice 1: the unwired semantic kernel

**Landed: the unwired semantic kernel.** `src/revl/intent.py` carries
`Intent`, `Action`, `Refusal`, `Violation`, `ceiling_params` and `refine`, with
`tests/test_470_intent_refinement.py` covering each dimension and each
fail-closed asymmetry. It is a leaf module: it imports `cap_order` and nothing
else. As of slice 1 nothing in the tree imported it either, and slice 3 is what
gives it its first caller. No approval gate, no lease path and no operator
profile changes, so no existing program changes its verdict.

The kernel is worth landing alone because of the one property the whole feature
turns on. The roadmap fixates on the *wider* cases (higher amount, different
tenant, extra capability), but the dangerous direction is the quiet one: a
declaration that is silent about a dimension must not read as permission for it.
The kernel therefore makes every field of both records required (no defaults), so
a half-written intent cannot silently be an unconstrained one, and it spells out
three asymmetries rather than defaulting them:

- An intent that declares no tenant (`tenant=None`) does not constrain tenancy.
  But an *action* that declares no tenant can never be shown to be in a stated
  tenant, so against a stated tenant it is refused. Unknown is not permitted.
- An intent that states no ceiling does NOT leave the spend unbounded: an action
  that states a spend against an intent that states no ceiling is spending
  outside the stated intent, so it is refused. The intent bounded no quantity, so
  it has not authorized this spend. (The all-`unstated` case, no ceiling stated
  and none spent, is the exhaustive-intent case and is allowed.)
- An action that states no amount cannot be shown to be within a stated ceiling,
  so it is refused.

`refine` checks the dimensions from the narrowest authority outward (object,
verb, ceiling, scope, tenant), so it returns the *first* stated dimension the
action exceeds and a caller that fixes one and re-runs gets the next. Each
`Refusal` names the dimension, the declared value and the requested value, and
`__str__` renders the same one-line-message plus indented-hint shape
`revl/errors.py` already uses, so a later stage wires this into a diagnostic
without inventing a second convention.

### 3.2 Slice 2: the two clauses slice 1 did not model

**Landed: the explicitly-related object and the set-valued scope.** Slice 1
modelled four of the six clauses the issue lists. The two it left out are the two
the roadmap's own exit criterion does not name, and both are now in the kernel,
so `refine` is the whole decidable check rather than most of it.

**`Intent.related`: "same or EXPLICITLY-related object".** An intent names one
primary object and, optionally, further capabilities it also authorizes. The
object dimension admits an action that any named capability `covers`, so the
decision is still a reading of the tree's one partial order, only over a declared
*set* of points in it rather than a single one. The word that carries the weight
is *explicitly*: `related` holds what the intent itself named, so relatedness is
never inferred from a shared token prefix, a shared scheme or a shared path.
Reaching a token no declared object names is still `EXTRA_CAPABILITY`, and the
refusal now renders every object the intent named, which is what makes it
readable: an operator sees the whole declared surface and the one thing outside
it. A `related` spelling may not carry a ceiling parameter, because the ceiling
is one bound on the whole intent and a per-object ceiling would be a second
comparison the ceiling dimension never performs.

Slice 1 listed a set-valued intent as a stage-2 concern for the "no hidden extra
capability" reading, and this is that stage: a declared set of objects is what an
approval ticket's capability set needs when `_approval_step`'s all-or-nothing
scan is eventually routed through `refine`.

**`Intent.scopes` / `Action.scopes`: no broader recipient or data scope.** Slice 1
left the issue's "no broader recipient scope" out on the grounds that the tree has
no recipient concept, and that reasoning was right about recipients and wrong
about the dimension. The clause is not asking for a recipient type; it is asking
for a comparison the other four dimensions cannot make. An object valuation is
single-valued (`cap_order` binds one path, one host, one table) and a ceiling is
ordered by `<=`, while a recipient list is a *set* ordered by containment. So the
kernel carries a named set-valued dimension and not a `recipient` field: an
intent states `("recipients", {"alice", "bob"})`, an action states what it
actually reaches, and refinement is `⊆`. The same rule with a different name is
what "no broader data scope" needs over the coarse data classes item 249's taint
model already tracks (`web`, `net`, `fs`, `model`, `input`, `secret`,
`confidential`), which is why the dimension is named rather than hard-coded. The
stage that makes recipients first-class supplies the name; it does not have to
add a dimension.

Its omission rules are the ceiling dimension's, read for sets: a scope the intent
states must be answered by the action, and members in a scope the intent does not
state are refused, because a scope the intent never bounded is not a free one. The
single place it is deliberately kinder is the empty member set, and only because a
set can say what a count cannot: `("recipients", ())` states that the action
reaches nobody, and reaching nobody refines every declaration. So the rule an
author reads off the three scope refusals is one sentence: state what the action
reaches, and reaching nothing is always admitted.

Two records, one validation point per dimension, both directions: scope names are
refused when they collide with a registered capability parameter (`path`, `host`,
`table`, `calls`, `size`, `time` and the budget aliases), because those facts are
already bounded by the object or the ceiling dimension and one fact bounded twice
lets the weaker comparison win. A bare `str` member set is refused for the reason
a bare `str` verb declaration is, and the action side is the sharper half: five
single-character recipients is a *narrower* request than the name the caller
meant, so an iterated string could admit a send the intent never permitted.

`related` and `scopes` are the only two fields on either record with a default,
and the default is the empty declaration. That keeps slice 1's property intact
rather than weakening it: omission must never read as permission, and here the
empty value is the narrow reading (`related=()` names one object, `scopes=()`
permits no scope at all, and an action that states members in an unstated scope is
refused).

**`Action.from_cap`.** The typed link now exists on both sides. Slice 1 could turn
a declared capability spelling into an intent; an action had to be assembled field
by field, which is exactly where the object valuation and the spend drift apart.
`Action.from_cap` runs the crossing's spelling through the same
`cap_order.split_ceilings`, so one spelling read as an intent and read as an
action is the identity refinement.

### 3.3 Slice 3: the checker-visible rule

**Landed: the source surface, and the refusal that names the intent.** Slices 1
and 2 are a decision with nothing to decide about. The part the roadmap's exit
criterion actually turns on is a DECLARATION held across time and a crossing
compared against it at the moment it fires, and that is what this slice adds:

- `within { object: …, verbs: [ … ], ceilings: { … }, tenant: …, related: [ … ],
  scopes: { … } }` trails a service operation's return type and is the intent the
  operation DECLARES. `object` and `verbs` are required, because they are the two
  dimensions with no honest default; the rest are optional and their omission is
  the narrow reading the kernel already spells out. The clause is built straight
  into an `intent.Intent` at parse, through `Intent.from_cap`, so the record's own
  construction rules are the surface's validity rules and there is no second
  vocabulary to keep in step.
- `acting { verb: …, tenant: …, scopes: { … } }` trails an `emit` step and states
  what that one crossing does. It carries only the three dimensions a capability
  spelling cannot: the OBJECT and the AMOUNT are read off the crossing's own
  spelling (`_emit_crossed_caps`, the same per-crossing resolution the approval
  obligation uses) through `Action.from_cap`. That is the typed link doing real
  work rather than decorating: one spelling read as an intent and read as an
  action is the identity refinement, so the declaration and the crossing cannot
  drift apart by being written twice.

The two meet in a provide-method body, which is the only place a declared
operation and a real crossing are both in hand: a service operation is an
interface, its providers are the bodies, and the declaration is an upper bound
every provider inherits exactly as `emission[...]` already is. `lower`'s
`_check_intent_refinement` runs `refine` over each crossed token and raises the
refusal with the declared value in the message and the `within` clause's line in
the hint, which is the roadmap's "refused with the intent it violated" for a
source program.

Both OMISSIONS are refused, in both directions, because each of them is a hole
rather than a convenience:

- An `acting` clause with no `within` to check it against would read as a check
  and perform none. Nothing here invents the missing intent (that is the
  roadmap's own scope note), so the clause is refused.
- A crossing with no `acting` clause inside a body whose operation DOES declare
  an intent is the dangerous direction, and it is the one that would have made
  the feature vacuous: a body could declare `within` and then reach anything at
  all through an unannotated `emit`. Every crossing under a declaration must
  state what it does.

A crossing whose capability set the per-crossing resolution cannot name is
refused for the same reason. That covers a bare `emission` callee (which names
no capability at all), the `*` token, and the shapes where the resolution
returns nothing, such as a provision call off a spawn handle or a crossing
through a service-typed parameter. No declared object can be SHOWN to cover any
of them, and reading the unnameable as the declared one is exactly the direction
slice 1 was built to close: an empty capability set is "nothing to compare", not
"nothing to check".

Both clauses are CONTEXTUAL, recognised only in the one slot each occupies (the
post-return-type slot of a service operation, which the `cache` clause already
uses, and the trailing slot of an `emit` step). The lexer's `KEYWORDS` set is
untouched, so a program using `within` or `acting` as an ordinary name still
compiles, and both AST fields default to `None`, so a method and an emit that
write neither clause lower to byte-identical IR. The check lives entirely in
lower, keyed off the declaration, and contributes no IR key of its own.

### 3.4 Slice 4: the declaration bounds the operation, not its `emit` steps

**Landed: the completeness half of slice 3's rule.** Slice 3 closed one hole per
crossing, at the crossing: an `emit` step under a declaration must state what it
does. The demand it could not make from there is the one the whole feature rests
on, because a declaration is only as strong as the crossings it sees, and an
`emit` step is not the only way a provide-method body reaches a boundary. Three
other spellings do, and each of them let a body declare `within { object:
fs.write(path="/tmp"), verbs: [ingest] }` and then reach `/etc` with no refusal
at all:

- a DIRECT CALL to an `emission` extern (`let n = wr(row)`). A host emission
  extern needs no `emit` marker, so the G4 scope check counts it as a crossing
  and item 470 never saw it.
- a VALUE-POSITION emission (`let r = emit svc.op(...)`, the `EmitExpr` the
  parser admits wherever a unary sits). Its marker is inside an expression, so
  no trailing clause can reach it, and it is not an `EmitStmt` at all.
- a HELPER HOP: a plain `fn` the body calls that itself reaches an emission.
  The same G4 check follows it transitively; slice 3's per-step check does not.

So the rule is restated over the whole body. Under a declaration the only
crossing spelling admitted is an `emit` step that states itself, and whatever
the body still reaches once those steps are removed is a crossing that states
nothing. `lower._check_intent_completeness` runs the residue through
`_method_emissions`, the SAME analysis the G4 provider upper bound reads, so the
check inherits that analysis's transitive reach and its vocabulary instead of
inventing a second one, and it refuses with the objects the declaration names.

The direction is the kernel's, not the cone's: a crossing INSIDE the declared
cone is refused too when it states nothing, because the finding is "this cannot
be shown to refine the declaration" and not "this object is outside it". The
same crossing written as `emit wr(row) acting { verb: ingest }` compiles, so the
rule bounds the spelling and never the boundary.

It is keyed off `decl.within` exactly as slice 3 is, and returns before it
builds anything when no intent is declared, so no existing program reaches it
and the byte-identical-IR property slice 3 pinned is unchanged.

One consequence is worth stating rather than discovering: a VALUE-RETURNING
crossing has no stating spelling today. `let r = emit svc.op(...)` is how an
emission that returns data is written, and it can carry no clause, so under a
declaration it is refused with no rewrite available other than dropping the
returned value. The slot for it is a trailing `acting` on the binding statement
rather than on the expression, and it is named in section 4 as open work.

## 4. Explicit non-goals for this note

Four stages are named and deliberately not started; each is a separate,
independently reviewable change:

0. **The stating slot for a value-returning crossing.** Slice 4 refuses
   `let r = emit svc.op(...)` under a declaration because the marker sits inside
   an expression and no clause can trail it. The honest surface is an `acting`
   clause on the BINDING STATEMENT (`let r = emit svc.op(...) acting { verb: … }`),
   which is a `let` grammar change rather than an expression one, plus the
   per-crossing check slice 3 already runs. Until it lands, an operation that
   declares an intent must discard what its crossings return.

1. **The class-(c) approval gate.** Today the gate compares the grant against the
   request with `_grant_covers` / `_grant_within`. Routing that comparison
   through `refine` would make the amount-scoped and `uses: n` approval cases
   that `246-auto-approve.md` leaves undesigned expressible against a stated
   intent. Touching the gate changes shipped, tested semantics and belongs in its
   own slice.
2. **The lease path.** A runtime crossing carrying its intent so the lease check
   is an intent refinement rather than an authority-versus-request comparison.
   `cap_order`'s ceiling erasure into `remainingUses` interacts with this and
   must be settled there, not here.

   It has no source-side half to land first, which is worth recording because it
   is the obvious thing to reach for. `let l = effect lease <cap> … undo
   l.revoke()` is lowered only from a COMPONENT ACTIVATION body
   (`lower._lower_lease_step` has exactly one caller, in the component-body
   loop), and `within { … }` declares an intent on a SERVICE OPERATION, so no
   declaration is ever in scope where a lease is acquired. Comparing a lease
   against a declared intent therefore needs either a declaration surface the
   activation body can carry or the runtime path in `mcp/session.py`, and the
   runtime path is the one this stage names.
3. **The operator profile.** A surface that declares an operator's intent so the
   `TOOL_VERB` management actions can be checked against it.

Also explicitly out of scope for this item, not merely deferred:

- **A recipient TYPE.** Slice 2 adds the set-valued dimension the issue's
  recipient clause needs, and deliberately stops there. Nothing in the tree
  produces the recipient set for a given crossing, so the kernel compares whatever
  set a caller declares and does not invent a `Recipient` notion, a resolution
  rule, or an address grammar. The stage that makes recipients first-class fills
  the scope in; it does not have to add a dimension.
- **Per-object ceilings and verbs.** A `related` object shares the intent's
  ceiling and verb set. Per-object bounds would be a second ceiling comparison and
  a second verb comparison, and the honest surface for them is a set of intents
  rather than a nested one, which is a shape only a wired stage can justify.
- **Inferring intent.** Nothing here derives an intent from a call. The caller
  states it, or there is no intent to check against. This is the roadmap's scope
  note, enforced by construction.
- **A policy grammar.** Teaching `docs/boundary-policy.md`'s rule language a verb
  or ceiling clause touches `policy.rs`, the Python policy tier and the gate
  crate; it is a larger, cross-lane change and is not attempted here.

## Relates to

- [#822](https://github.com/inso1337/revl/issues/822), roadmap item 470 (this
  note).
- [441-goal-contracts.md](441-goal-contracts.md): goal contracts are the
  *termination* analogue of this refinement contract.
- [442-typed-delegation.md](442-typed-delegation.md): typed delegation is where a
  declared authority is handed on; the intent is what it is handed on *for*.
- [246-auto-approve.md](246-auto-approve.md): the confused-deputy framing, and
  the amount-scoped and `uses: n` approvals it deliberately leaves undesigned.
- [../boundary-policy.md](../boundary-policy.md): the rule language whose flat
  globs have no verb dimension.
- [../capabilities.md](../capabilities.md): G4 capability-scoped emission, the
  name-based check that must not be repurposed as an object refinement.
- [249-taint-provenance.md](249-taint-provenance.md): the coarse data classes the
  scope dimension compares when the scope is a data scope rather than a recipient
  set.
- `src/revl/goal.py`: the pure-kernel-first precedent this module follows.
