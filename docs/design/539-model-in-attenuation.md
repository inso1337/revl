# 539: The model role in the capability attenuation product

Roadmap: item 519 (issue #1193), from the 2026-09-19 external review. Slice 1
is LANDED with this note; slices 2 to 4 are designed here and not written.

Design-doc number: 539. Numbers 531 to 538 were already taken on 2026-09-20,
531 on main and the rest in open pull requests (532 in #1228 and #1242, 533 in
#1232, 534 in #1231, 535 in #1230, 536 in #1239, 537 in #1241, 538 in #1242),
which is checked against `gh pr list` diffs and not only against main, because
four lanes collided on 531 and three on 536 the same day.

Reconciles with: item 66 and `docs/capability-attenuation.md` (the product this
extends), item 512 and `docs/design/531-model-placement.md` (the `model role`
declaration this extends, per its section 9), item 294 and `revl.cap_order`
(the `(T, P)` fold both sides are compared by), item 343 (a crossing's
capability is its declared token), item 514 and issue #1188 (the origin
ceiling, the value-side sibling), issue #1223 and item 544 (the kernel
boundary, which wants this as a refusal rather than a policy).

---

## 0. The decision in one paragraph

The attenuation product accounts for services, realms, taints and budgets. A
model is an **authority surrogate**: it picks which capability the component
consulting it reaches for. It was not in the product, so a component holding
`net` whose decisions run through a model able to reach `shell` was accounted
as if the model were inert. This note puts the role in the product. A
`model role` gains an optional `reaches [...]` clause naming the capabilities a
call to it can itself reach; a component that routes an action through a role
has an **effective ceiling** of what it holds together with what the role
reaches; and a role reaching past its component is refused at admission with
both sets named. It is the item-66 rule with a model-route edge in place of a
spawn edge, folded by the same `cap_order.covers`.

---

## 1. The gap, and which way it failed

Item 512 landed the placement: *where* a model call runs. It says in its own
non-goals that "no role can grant a component a capability it does not hold;
that direction is item 519's, and until it lands a role grants nothing at all".
So until this note, the checker's view of an agent-shaped component was:

    effective(Classifier)  =  held(Classifier)

with the role contributing nothing. That is the wrong accounting in one
specific direction, and the direction is the whole item: a model reaches
**further** than the component that consults it, not less. A component holding
`net` that routes its decisions through a role able to reach `shell` is a pair
whose reach is `net` and `shell`, and the product was reading `net`.

The failure shape this removes is therefore the same one item 512 named and the
same one the repository keeps finding: **a surrogate accounted as inert**. An
attenuation product that treats a model role it knows nothing about as reaching
NOTHING fails open, because the component that has said nothing about the model
it consults is precisely the one whose effective ceiling is unknown.

---

## 2. The surface

```revl
model role local on_device  reaches [model.complete]
model role cloud off_device reaches [model.complete, net.request]
model role tool  on_device   reaches [*]
model role inert on_device   reaches []
model role quiet on_device
```

`reaches [...]` is an optional clause on the existing `model role`
declaration, per section 9 of `docs/design/531-model-placement.md`: "519 should
EXTEND `model role` with a reachable-capability set, not add a second role
declaration." It is spelled with the `emission [...]` / `witnessed [...]`
bracket so a reader meets one capability-list grammar, and every token funnels
through `Parser._capability_params`, the one canonical point that validates a
parameter list against the closed registry, so
`reaches [fs.write(path="/data")]` is validated and canonicalised exactly as an
emission token is. `reaches` is a contextual identifier read only in this slot,
so the lexer's `KEYWORDS` table and the self-hosted lexer that mirrors it need
no sync.

Four states, and the fourth is the one that matters:

| written | meaning |
| ------- | ------- |
| `reaches [a, b]` | the role reaches exactly `a` and `b` |
| `reaches [*]` | declared unbounded: the role reaches the unnameable host boundary |
| `reaches []` | declared empty: the role reaches nothing |
| clause omitted | **undeclared**, which is NOT empty. See section 3 |

### 2.1 Undeclared is not empty

`model_route.UNDECLARED_REACH` is `("*",)`. A role with no clause reaches the
unnameable `*`, the one token no `requires` key can name and that
`cap_order.covers` therefore covers with nothing, so such a role is refused
against any component that does not itself hold `*`.

This is the single most important reading in the note, and it is the fail-open
question stated as a definition rather than left to a default. Reading silence
as "reaches nothing" would make an unknown model inert in the product, and an
unknown model is what the item exists for. It is also not a new invention: it
is the choice `_spawn_emission_surface` already makes for a service method that
declares `emission` with no capability list, where `None` becomes `*` and not
`set()`.

The cost is stated plainly: a role declared before this note and routed by a
component that reaches a model boundary now needs a `reaches [...]` clause. No
program on the tree pays it (section 6), and a program that does pay it is
being asked to write down the one fact the product needs.

---

## 3. The product

For a component C whose `route model` block names roles R:

```
effective(C)  =  held(C)  u  reach(R)      for every R that C routes to
effective(C)  ⊆  held(C)                   -> admit
effective(C)  ⊄  held(C)                   -> REFUSE, naming both sets
```

`held(C)` is item 66's own held set, unchanged: every `requires` key resolved
through the key-to-token bridge to the DECLARED emission token of the service
it wires, plus the component's own crossings, with ceilings stripped exactly as
the spawn fold strips them (item 260 section 3.3). `reach(R)` is the role's
declared tokens, resolved through the same `cap_order.parse_cap` every other
capability string takes, so a parameterised reach is really compared: a role
reaching `fs.write` under a component holding `fs.write(path="/tmp")` is
refused, because a dropped parameter widens.

The admitted case is where the roadmap's "attenuates to the intersection"
clause lives. An admitted pair's record carries `holds`, `reaches` and
`attenuated` — what the component holds that the role does NOT reach — which is
the model-role sibling of the `dropped` column in the item-66 audit chain.

### 3.1 When the product is asked

The product is computed for a component that both declares a `route model`
block AND holds a boundary that could be a model call. `_consults_a_model`
decides the second, and it is an over-approximation in the refusing direction:
a held boundary counts unless its declared token PROVES it is some other
boundary. Three shapes count — a token whose head is `model`, the unnameable
`*`, and a `key:` wiring element, which is a boundary whose declaration names
no token at all and so rules nothing out.

A component whose held boundaries are all declared non-model tokens consults no
model. That is not an exemption and not a policy escape: a role steers a
component by choosing among the boundaries the component can reach, so a
component that reaches none has no ceiling for a role to widen. It is also what
keeps the slice free of a migration: item 512's own fixtures declare a route
over an action that crosses nothing.

The gate reads the HELD set and not the component's own `emit` steps, because a
provider body crosses through a `requires` key and it is the key's SERVICE that
declares the `model.*` token; the body only names the key. Reading the body
alone measured the empty set and the first draft of this check was silently
inert because of it.

### 3.2 The refusal

`G-MODEL-PLACE`, reused rather than invented. Item 512 registered it in
`revl.diagnostics.GUARANTEES` and `FIXES`; this note extends the guarantee
sentence with the reach clause. No new code is registered, so item 523's
generated tier matrix needs no new `ACKNOWLEDGED` entry and no new
`examples/rejections/` reproducer, and the fixtures stay inline for the reason
section 6.1 of `docs/design/531-model-placement.md` gives.

A declared widening:

    `Classifier` routes `classify` (*) through model role `local`, which
    reaches `shell.exec`, but `Classifier` holds only `model.complete` - a
    component's effective ceiling is the pair's, so a model may not reach past
    the component that consults it (G-MODEL-PLACE)

An undeclared reach, which says what the author actually wrote rather than
claiming they wrote `reaches [*]`:

    `Classifier` routes `classify` (*) through model role `local`, which
    reaches an unnameable host boundary, but `Classifier` holds only
    `model.complete` - ... (G-MODEL-PLACE)
      model role `local` (line 2) declares no reach, so its reach is the
      unnameable `*`. ... declare it - `model role local on_device
      reaches [...]` - naming the capabilities a call to it can reach; an
      undeclared reach is not an empty one, because a model that steers a
      component is exactly the one whose reach must be written down

Both name the component, the action, the origin arm, the role, the offending
capability and the component's whole held set, which is the exit test's "a
widening is refused naming both sets".

### 3.3 The record, for issue #1223

An admitted composition that declares a role gets a `model_reach` section in
its manifest, one entry per (component, action, origin) the block places:

```json
{ "component": "Classifier", "action": "classify", "origin": "*",
  "role": "local", "residence": "on_device",
  "holds": ["model.complete"], "reaches": ["model.complete"],
  "effective": ["model.complete"], "attenuated": [], "reach_declared": true }
```

Issue #1223 (item 544) argues the kernel boundary must be a capability the
attenuation product REFUSES rather than a policy the controller applies, and
PR #1241 agrees that the attenuation-side capability is the real enforcement.
What #1223 can consume from this slice is exactly three things, and nothing
more is promised:

1. **A refusal it can extend.** A kernel-owned boundary appearing in a role's
   `reaches [...]` under a component that does not hold it is already refused
   here, by name, before any controller runs. #1223's rule is the same fold
   with the kernel's own held set on the left, not a second mechanism.
2. **`model_reach[].effective`**, the per-edge statement of what a
   model-steered component's ceiling actually is. A controller that must decide
   whether a candidate can reach a kernel boundary reads this rather than
   re-deriving reach from the AST.
3. **`reach_declared`**, which distinguishes a role whose reach is written down
   from one that was admitted only because it consults no model. A controller
   must not read an absent entry as a proof of narrowness.

The section is additive and role-only: a composition that declares no
`model role` has no `model_reach` key, so its manifest is byte-identical.

---

## 4. Which way every decision fails

| # | The decision | Direction |
| - | ------------ | --------- |
| 1 | a role with no `reaches` clause reaches `*` | closed: an unknown surrogate is not an inert one |
| 2 | a held boundary counts as a possible model call unless its token proves otherwise | closed: `*` and an untokened `key:` wiring both count |
| 3 | a role's reach is compared with its valuation, not as a bare token | closed: a dropped parameter widens |
| 4 | every role the block NAMES is folded in, not the one a crossing reaches | closed: the crossing side is slice 2; naming all of them over-approximates toward refusing |
| 5 | a malformed stored token degrades to `*` | closed: `*` as a reach element is covered by nothing |
| 6 | ceilings are stripped before the coverage fold | neither: budget attenuation is item 260's separate check, and folding a ceiling here would make one crossing spuriously "cover" another |

Decision 4 is the honest limit of slice 1. A component routing three origins to
three roles is checked against all three reaches, even though one call reaches
one role. That refuses a program whose widest role is never actually consulted
for the origin that would consult it. It errs toward refusing, and slice 2 is
what makes it precise.

---

## 5. Non-goals

* **Deciding what a model can really do.** `reaches [...]` is a declared claim
  about a role, checked against the components that route to it, in exactly the
  sense `residence` is a declared claim in item 512 and `[sandbox.needs]` is an
  author claim in `docs/design/411-sandbox-placement.md`. Nothing here inspects
  a member, a tool list or an endpoint.
* **Selecting a model.** Item 515 schedules inside this boundary. A scheduler
  that picked a role whose reach no component holds would be the fail-open
  shape, and this check is what makes that refusable.
* **The value side.** Which origin a value carries into a model call is item
  514's origin ceiling. This note bounds the CAPABILITY the pair reaches; 514
  bounds the DATA the call receives. They are independent and neither subsumes
  the other.
* **Run-time enforcement.** The check is a compile-time refusal. An admitted
  program emits identically on every tier; nothing reaches an emitter.
* **Naming a model.** Unchanged from item 512: no vendor, no weights hash, no
  endpoint appears in a revl document.

---

## 6. Where it lands

| File | Change |
| ---- | ------ |
| `src/revl/parser.py` | `ModelRoleDecl.reach`; the optional `reaches [...]` clause in `model_role_decl()`; `_model_role_reach()` |
| `src/revl/model_route.py` | `UNDECLARED_REACH`; `Role.reach`, `Role.reach_declared`, `Role.reach_tokens`; `line` on an arm record |
| `src/revl/lower.py` | `_model_reach_caps`, `_consults_a_model`, `_check_model_attenuation`; the call site beside `_check_spawn_attenuation`; `manifest["model_reach"]` |
| `src/revl/diagnostics.py` | the `G-MODEL-PLACE` guarantee sentence gains the reach clause |
| `docs/capability-attenuation.md`, `docs/guarantees.md`, `docs/rejections.md` | the model factor, the guarantee row, the refusal section |
| `tests/test_model_attenuation_519.py` | NEW |

No emitter, no IR node, no manifest key for a program that declares no role, no
lexer table. `tools/build_gate_crate.py --check` is the authoritative answer on
the crate and is reported in section 8.

### 6.1 Why no program on the tree pays a migration

Every `model role` declaration that exists today is in
`tests/test_model_placement_512.py` or in a doc fence, and every component that
routes in them provides a service with no emission and requires nothing, so
`held` is empty and `_consults_a_model` is false. Measured rather than argued:
the item-512 suite is unchanged by this slice (section 8).

---

## 7. The self-host question

`selfhost/*.rvl` does not parse `route model` or `model role` at all; section 7
of `docs/design/531-model-placement.md` measured the gate answering
`BAD|unexpected token at top level` for every program that declares one, which
is the false-reject direction the census docstring allows. `reaches [...]` is
an extension of a declaration the gate already declines, so it cannot change
that answer, and no `.rvl` in any corpus directory uses the construct. The
census is therefore unmoved, which is measured in section 8 and not inferred.

The self-host port of the whole construct is item 512's slice 3.

---

## 8. Slice plan and evidence

**S1. The reach set and the product. LANDED with this note.** Section 2's
surface, section 3's fold, one refusal citing `G-MODEL-PLACE`, the manifest
record. Oracle: `tests/test_model_attenuation_519.py`.

**S2. The crossing side.** Fold the role a CROSSING actually reaches rather
than every role the block names, which is item 512's slice 4 (a crossing
carries its `model.<role>` token) applied here. This removes decision 4's
over-approximation and is what makes a three-role block precise.

**S3. The role in the spawn product.** A spawner whose CHILD routes through a
role reaching past the spawner is the lineage form of the same question. It
needs the model reach folded into `_spawn_surface_closure`, and it is separate
because the closure is the one place a mistake amplifies across a whole graph.

**S4. The kernel boundary.** Issue #1223's rule, expressed as this fold with a
kernel-owned held set. Section 3.3 says what it consumes.

---

## 9. Things stated here that are not verified

* That a role's `reaches [...]` corresponds to anything a real model member can
  or cannot do. It is a declared claim, checked against the components that
  route to it, and nothing measures a member (section 5).
* That `_consults_a_model` is complete. It is a deliberate
  over-approximation, and a component that reaches a model through a boundary
  whose declared token is neither `model.*`, `*`, nor untokened would not be
  gated in. No such shape is known; it was not exhaustively searched for.
* Any claim about the six emitter tiers. Nothing in this slice reaches an
  emitter, so there is nothing tier-specific to be right or wrong about, but
  that is an argument and not a measurement.
* That decision 4's over-approximation never refuses a program someone wants to
  write. It plainly can, and slice 2 is the fix; no corpus program exercises it
  today.
