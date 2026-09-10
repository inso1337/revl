# 470: Intent-vs-action refinement verification

Filed against GitHub issue [#822](https://github.com/inso1337/revl/issues/822)
("Intent-vs-action refinement verification"), roadmap item 470. Design plus a
first slice: the semantic kernel `src/revl/intent.py` (the `Intent` record, the
`Action` record, and `refine`), with unit coverage in
`tests/test_470_intent_refinement.py`. Nothing is wired to it yet; section 3
names the line and section 4 names the four stages that stay open.

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

- **object** is `cap_order.Cap` and the decision is `cap_order.covers`, with the
  intent as the wider side. The kernel does not reimplement the order: "same or
  related object" is exactly `covers`' job, and a second order would be a second
  source of truth. What the kernel adds is the *declaration* being held across
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
- **verb** is the one genuinely new field. It is a set of operation names
  permitted on the object, and it is not a spelling of anything that exists
  today.

The typed link the issue asks for is `Intent.from_cap`: the *same* capability
spelling the policy, the G4 check and the approval gate already speak
(`fs.write(path="/tmp", calls=3)`) becomes the four named dimensions, with the
ceiling parameters peeled off by `cap_order.split_ceilings`, the one place that
split is defined.

## 3. The slice that lands with this note

**Landed: the unwired semantic kernel.** `src/revl/intent.py` carries
`Intent`, `Action`, `Refusal`, `Violation`, `ceiling_params` and `refine`, with
`tests/test_470_intent_refinement.py` covering each dimension and each
fail-closed asymmetry. It is a leaf module: it imports `cap_order` and nothing
else in the tree imports it. No checker rule, no approval gate, no lease path and
no operator profile changes, so no existing program changes its verdict.

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

`refine` checks the dimensions in the order the intent declares them (object,
verb, ceiling, tenant), so it returns the *first* stated dimension the action
exceeds and a caller that fixes one and re-runs gets the next. Each `Refusal`
names the dimension, the declared value and the requested value, and `__str__`
renders the same one-line-message plus indented-hint shape `revl/errors.py`
already uses, so a later stage wires this into a diagnostic without inventing a
second convention.

## 4. Explicit non-goals for this note

Four stages are named and deliberately not started; each is a separate,
independently reviewable change:

1. **The checker-visible rule.** A surface where a declaration carries its
   intent and a crossing is checked against it before execution. This is the
   stage that produces the roadmap's "refused with the intent it violated" for a
   *source* program. It is not wired here.
2. **The class-(c) approval gate.** Today the gate compares the grant against the
   request with `_grant_covers` / `_grant_within`. Routing that comparison
   through `refine` would make the amount-scoped and `uses: n` approval cases
   that `246-auto-approve.md` leaves undesigned expressible against a stated
   intent. Touching the gate changes shipped, tested semantics and belongs in its
   own slice.
3. **The lease path.** A runtime crossing carrying its intent so the lease check
   is an intent refinement rather than an authority-versus-request comparison.
   `cap_order`'s ceiling erasure into `remainingUses` interacts with this and
   must be settled there, not here.
4. **The operator profile.** A surface that declares an operator's intent so the
   `TOOL_VERB` management actions can be checked against it.

Also explicitly out of scope for this item, not merely deferred:

- **Recipients.** The issue also names "no broader recipient scope". No recipient
  concept exists in the tree at all (`grep -r recipient src/` is empty; the word
  appears only in `docs/interop-bridge.md` and the roadmap), so an unused
  `recipient` field would be the speculative framework this item must not build.
  The stage that introduces recipients owns adding the dimension.
- **Multi-capability intents.** "No hidden extra capability" across a whole
  ticket's capability set is already all-or-nothing in `_approval_step`. A single
  object per intent is the honest first slice; a set-valued intent is a stage-2
  concern.
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
- `src/revl/goal.py`: the pure-kernel-first precedent this module follows.
