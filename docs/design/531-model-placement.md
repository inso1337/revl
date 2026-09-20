# 531: Model placement as a checked route condition (`route model`)

Roadmap: item 512 (issue #1186), from the 2026-09-19 external review. Slice 1
is LANDED with this note; slices 2 to 5 are designed here and not written.

This is the foundation of the eight-item model cluster (512 to 519, issues
\#1186 to \#1193). Every one of the other seven presupposes that a model call is
a placeable, checked thing: grammar-constrained decoding (513) constrains a
call, the origin ceiling (514) bounds what a call may receive, the portfolio
(515) schedules inside a boundary, the council (516) places each member, the
decision object (517) records which placement was in force, shadow routing
(518) moves an action from one placement to another, and the attenuation
product (519) multiplies a placement into the capability ceiling. Section 9
says what each of them inherits from here and what this note deliberately
leaves to it.

Reconciles with: item 161 and `docs/router.md` (the route conditions this is a
sibling of), item 249 and `docs/design/249-taint-provenance.md` (the origin
lattice the arms are keyed by), item 257 and
`docs/design/257-typed-model-boundary.md` (the typed boundary, which is what
gets placed), item 343 (a crossing's capability is its declared token), item
411 and `docs/design/411-sandbox-placement.md` (placement as a checked
dimension), `docs/guarantees.md` and `docs/rejections.md` (the code registry).

---

## 0. The decision in one paragraph

revl already routes runtime behaviour, and the route is checked rather than
advised: `isolate <key> in realms(...)` names where a dependency resolves and
the linker refuses a realm with no provider. It has no equivalent for a model
call, so the choice of model is host configuration and the checker is blind to
it. This note adds the missing sibling. A **model role** is a declared
placement: a name plus where a call to it runs. A **`route model on <action>`**
block places one action's model calls by the origin class of what the action is
given, reusing item 249's lattice instead of inventing a second vocabulary. The
block is a PERMISSION, checked at admission, and not a scheduler: which of two
admissible roles is cheaper is item 515's question, and the router may
recommend a destination while revl still decides whether the destination is
allowed. Because it is a permission and not a selection, it contributes nothing
to the IR and no emitter changes.

---

## 1. The gap

A component that needs a completion today names a service key and emits:

```revl fragment
provide out { fn classify(text: Str) -> Str = emit llm.complete(text) }
```

Which model answers is decided outside the language. The consequences are
exactly the three the issue names, and none of them is a gap in enforcement of
an existing rule; they are questions the checker cannot be ASKED:

1. It cannot see that a `confidential` input went to a cloud model. The origin
   lattice knows the value is confidential (item 249); nothing knows where the
   crossing terminates.
2. It cannot enforce "this component never leaves the device". `isolate` places
   a provider in a realm; a realm label is a name, not a residence, and no rule
   reads one as a confinement claim.
3. It cannot record which model produced a value. Item 517 needs a placement to
   record; there is none.

The typed boundary (item 257) closed the SHAPE of the answer. The selection is
what is missing, and the review's own sentence is the design constraint: the
router may recommend a destination, and revl still decides whether the
destination is allowed.

---

## 2. The surface

Two declarations, both spelled with contextual identifiers so the lexer's
`KEYWORDS` table, and the self-hosted lexer that mirrors it, need no sync. This
is the `retention` and `boot component` discipline: a program that already uses
`model` or `route` as an ordinary name keeps parsing, which matters because
`requires model: Model` is the common spelling for the key this item is about.

```revl
model role local on_device
model role cloud off_device

service Answer { fn classify(text: Str) -> Str }

component Classifier provides out: Answer {
  route model on classify {
    confidential -> local,
    * -> cloud
  }
  provide out { fn classify(text) = text }
}
```

**`model role NAME <residence>`** is a top-level declaration. `residence` is a
closed vocabulary, `on_device` or `off_device`. `on_device` means the call runs
on the device the component runs on and the prompt does not leave it.
`off_device` means it leaves. Everything else about the role (which host, which
operator, which quantisation, which jurisdiction) is a property of the provider
and is item 515's; this declaration is deliberately the smallest fact a
placement rule can be written against.

**`route model on <action> { <origin> -> <role>, ... }`** is a component-prelude
clause, the sibling of `isolate` and `intercept`: it names what an action may
reach before any dependency access, so it must precede every effect, emit,
await and provide statement. `<origin>` is an origin class from item 249's
lattice, or `*`.

Roles are declared at the program level and routed at the component level on
purpose. Item 516's council binds several roles in one aggregation and item 519
gives a role a reachable-capability set, so a role is a program-level entity
that several components name; the route is the per-component permission.

### 2.1 What `*` means, and what it does not

`*` stands for the origin classes that no other arm in the block names AND that
are not confidentiality origins. **`*` never covers `confidential` or
`secret`.**

This is the single most important reading in the note, and it is a definition
rather than a warning. Without it, `* -> cloud` is a sentence that sends a
confidential input off the device while looking like a default, which is
precisely the failure this item exists to remove. With it, a confidential value
is placed only by an arm that names `confidential`, or it is not placed at all,
and `* -> cloud` can never be the sentence that leaks one.

Slice 1 pins the reading in the checker's own terms (an arm naming a
confidentiality origin is the only way to place one, and such an arm is checked
against the role's residence). Slice 2 is what makes it bite on a VALUE rather
than on a written arm: until the flow walk lands, a confidential value reaching
an action with a `* -> cloud` route is not yet refused. That is stated here as
a limit of slice 1 rather than implied by silence, and it is why slice 2 is the
next one and not an optional extra.

---

## 3. What the checker decides

All of it lives in `src/revl/model_route.py`, beside the rules, the way
`revl.retention` holds item 472's. The parser reads the shape and validates
nothing.

| # | The decision | Refusal cites | Direction |
| - | ------------ | ------------- | --------- |
| 1 | a role's residence is in the closed vocabulary | `G-MODEL-PLACE` | closed: a typo is a refusal, never a default placement |
| 2 | a role name is declared once | `G-MODEL-PLACE` | closed: two residences for one name is not resolved by order |
| 3 | an arm's origin is in item 249's lattice, or `*` | `G-MODEL-PLACE` | closed: an unknown origin is a refusal, not an arm that never matches |
| 4 | an arm's role is declared | `G-MODEL-PLACE` | closed: an undeclared role has no residence, so the placement is unknown and unknown is refused |
| 5 | an origin is routed at most once per block | `G-MODEL-PLACE` | closed: two arms for one origin would make the placement depend on match order |
| 6 | a block names at least one role | `G-MODEL-PLACE` | closed: an empty route places nothing and reads as permissive |
| 7 | an action is routed at most once per component | `G-MODEL-PLACE` | closed: which block binds would otherwise be declaration order |
| 8 | `on <action>` names an action the component declares | `G-MODEL-PLACE` | closed: a renamed method must not leave a block that protects nothing |
| 9 | the `secret` origin reaches no role, at any residence | `G-SECRET-FLOW` | closed: an LLM prompt is already a disclosure sink for a bound secret |
| 10 | a confidentiality origin does not reach an `off_device` role | `G-MODEL-PLACE` | closed: the roadmap's own exit test |
| 11 | a `route model` block precedes every action | `G-MODEL-PLACE` | closed: the prelude rule `isolate` and `intercept` already carry |

Decision 8 is the shape this repository keeps finding on the wrong side: state
keyed to a thing that outlived the thing meant to bound it, failing open. A
route keyed to `classify` after `classify` is renamed to `classify_text`
protects nothing while still reading, to a reviewer and to an audit, as a
placement. It is refused, and the diagnostic lists the actions the component
does declare.

Decision 9 is not a new policy. `G-SECRET-FLOW` already names an LLM prompt as
a disclosure sink for a capability-bound secret, so an arm placing one
contradicts a shipped guarantee. The refusal therefore cites that guarantee and
not this item's, which is the honest attribution and keeps `revl explain` right
for the author who hits it.

### 3.1 The new code

`G-MODEL-PLACE`, registered in `revl.diagnostics.GUARANTEES` and `FIXES` and
therefore in `docs/rejections.md` through docgen:

> a model role declared `off_device` never receives a confidentiality origin,
> and an action reaches only the roles its `route model` block names

It is revl-original with no paper anchor, in the family of `G-SECRET`,
`G-SECRET-FLOW` and `G-RETAIN`, and like them it is not a `G<digit>` code, so
it does not enter the `DESIGN.md` section 4 table.

The flagship refusal, which is item 512's exit test verbatim:

```revl reject G-MODEL-PLACE
model role local on_device
model role cloud off_device

service Answer { fn classify(text: Str) -> Str }

component Classifier provides out: Answer {
  route model on classify {
    confidential -> cloud
  }
  provide out { fn classify(text) = text }
}
```

    action `classify` (Classifier) routes the `confidential` origin to model
    role `cloud`, which is declared `off_device` on line 2: a confidential
    input may not leave the device (G-MODEL-PLACE)

The diagnostic names the action, the origin and the role, which is what the
exit test asks for, and it names the residence and the line that made it a
refusal, which is what makes it fixable without reading this note.

---

## 4. Why this is a permission and not a scheduler

The roadmap draws the line and this note holds it: "the scope is placing a
call, not scheduling it: which of two admissible models is cheaper is a policy
question, and the route is a permission."

The mechanical consequence is that `route model` contributes NOTHING to the IR.
An admitted program is byte-identical to the same program with the block
deleted, which `test_an_admitted_placement_writes_no_ir` pins by comparing the
two IRs. Nothing was added to any of the six emitters, no golden moved, and the
manifest is unchanged.

This is a real property and not an accounting trick, but it has an edge the
author must not be able to fall off: a construct that is checked and not
enforced at run time can be mistaken for one that routes. Two things keep that
from being a silent wrong answer. First, the block grants no ability at all, so
there is no behaviour to be surprised by: a program that writes a route and
then calls a model reaches exactly the model it reached before, and the route
can only ever have REFUSED something. Second, the direction of every rule in
section 3 is toward refusing, so the worst outcome available from the
declaration alone is a program that does not compile. What is genuinely absent
until slice 2 is the value side, and section 2.1 states it in the one place an
author would look.

---

## 5. Non-goals

* **Selecting a model at run time.** Item 515 schedules inside the boundary
  this draws. A route that named a winner would be a scheduler with one
  strategy hard-coded.
* **A device profile.** GPU/CPU/NPU, memory, quantisation, load and unload cost
  are item 515's, and the provider publishes them rather than the program
  asserting them (item 538, upstream `revl-harness#10`). `residence` is
  deliberately the one bit a confinement rule needs.
* **Naming a model.** A role is a name bound to a member by configuration. No
  vendor, no weights hash, no endpoint appears in a revl document.
* **Deciding what "the device" is.** `on_device` is a declared claim about the
  placement, checked against the arms that name it, in exactly the sense
  `docs/design/411-sandbox-placement.md` calls the `[sandbox.needs]` gate an
  author claim. A declaration that disagrees with the machine it runs on is
  item 515's problem, and item 538 records the decision that the profile is
  published by the provider rather than asserted by configuration for that
  reason.
* **The flow half.** See slice 2. Slice 1 checks the declaration.
* **Widening anything.** The item adds refusals and removes none. No program
  that compiles today stops compiling, and no role can grant a component a
  capability it does not hold; that direction is item 519's, and until it lands
  a role grants nothing at all.

---

## 6. Where it lands

| File | Change |
| ---- | ------ |
| `src/revl/parser.py` | `ModelRoleDecl`, `ModelRouteStmt`, `ModelRouteArm`; `Program.model_roles`; the two contextual dispatch sites; `model_role_decl()` and `model_route_stmt()` |
| `src/revl/model_route.py` | NEW. The vocabulary, `roles()`, `check()`, and all eleven refusals |
| `src/revl/taint.py` | `ORIGIN_CLASSES`, the public frozen view of `_ORIGIN_CLASSES`, so the declaration surface reads the lattice that defines it |
| `src/revl/lower.py` | `_model_route.check(program)` in `_check_and_lower`; the prelude branch in `_lower_component` that carries the ordering rule and writes no IR |
| `src/revl/diagnostics.py` | `G-MODEL-PLACE` in `GUARANTEES` and `FIXES` |
| `src/revl/compiler.py` | `model_roles` rides the declaration closure across modules, the `retention` rule |
| `docs/rejections.md`, `docs/guarantees.md`, `docs/router.md` | the code row, the family row, the sibling pointer |
| `tests/test_model_placement_512.py` | NEW. 19 tests |

No emitter, no manifest, no IR, no lexer, no crate. `tools/build_gate_crate.py
--check` reports in sync, which is the authoritative answer and not an
inference from the file list.

### 6.1 Why the fixtures are inline

The test programs are written as strings in the test file rather than dropped
in `examples/rejections/` or `tests/fixtures/`. Both directories are corpus
roots for `tools/gate_reference_census.py`, and the self-host does not parse
`route model` yet (section 7), so an ADMITTING fixture in either place would
have become a `false-reject` census entry the moment it landed. `false-reject`
is currently empty and this slice keeps it empty. A fixture belongs there when
slice 3 lands, and moving it is then part of that slice's evidence.

---

## 7. The self-host question

`selfhost/*.rvl` is a second implementation that must agree with the reference,
and its oracles catch divergence but not a missing feature. So the question is
not whether the self-host implements `route model`, which it does not, but
whether it can SILENTLY ADMIT a program the reference decides.

Measured, on the gate built from `selfhost/lower.rvl` at this branch:

| program | reference | gate |
| ------- | --------- | ---- |
| the component with no placement (control) | admits | `''` (admits) |
| `route model on classify { confidential -> local, ... }` | admits | `BAD\|unexpected token at top level` |
| `route model on classify { confidential -> cloud, ... }` | refuses `G-MODEL-PLACE` | `BAD\|unexpected token at top level` |

The gate refuses both, so it never admits a program whose placement it cannot
decide. It errs in the false-reject direction, which is the direction the
census docstring names as the one the crate is allowed to err in.

The marker it refuses with is the generic top-level parse refusal rather than a
named one. That is not new debt introduced here: it is the state of EVERY
contextual top-level declaration the reference has added since the self-host's
top-level dispatch was written, and the same gate answers
`BAD|unexpected token at top level` for a shipped `retention` declaration (item
472), measured the same way on the same build. Giving model placement a named
`MODEL` marker, so the refusal says which construct this gate does not decide
yet, is the first half of slice 3, and it is the half that touches
`selfhost/parser.rvl` and therefore both crates.

Because no `.rvl` in any corpus directory uses the construct, the census is
unmoved: `--check` reports no change from the baseline, `false-reject` is still
empty and `false-admit` is still 9.

---

## 8. Slice plan

Each slice is independently landable and carries its oracle in the same PR.
`tests/test_model_placement_512.py` is the standing guard on every one.

**S1. The declaration, checked. LANDED with this note.** The surface of section
2, the eleven refusals of section 3, the `G-MODEL-PLACE` registration, no IR.
Oracle: `tests/test_model_placement_512.py`, 19 tests, measured non-vacuous
against `52fb8ef` (18 failed, 1 passed, the one being the control) and 19
passed on the branch; census `--check` unmoved; `build_gate_crate.py --check`
in sync. Fixtures inline (section 6.1).

**S2. The value side: the origin ceiling.** This is roadmap item 514 and it is
listed here because it is what makes section 2.1 bite. The taint flow walk
learns the route table `check()` already returns, and refuses a value whose
origin the arm for that origin does not place, naming the value's origin and
the role. This is the slice that makes `* -> cloud` a real confinement
statement rather than a defined one. Oracle: a flow test in the shape of
`tests/test_secret_flow.py`, over a component whose provide-method threads a
`Secret[Str]` config field into a model emission; the refusal must fire with no
arm naming `confidential` present, which is the vacuity trap for this slice.
Owner: issue \#1188.

**S3. The self-host port.** Two halves, and the first is worth landing alone: a
named `MODEL` marker in `selfhost/parser.rvl` so the gate says which construct
it declines rather than "unexpected token", then the port itself. Both touch
the crate closure, so both regenerate `crates/**` with `build_gate_crate.py`
AND `build_gate_wasm.py`, and both must run `tests/test_gate_crate_admit.py`,
because the two drift gates compare bytes only and a byte-correct regen has
failed `cargo` before. Oracle: `_classify` learns the tag, the census records
`agree-refuse/MODEL` for a rejection fixture, and the admitting fixture moves
from the test file into `examples/` (section 6.1).

**S4. The crossing side.** S1 checks which roles an action MAY reach; nothing
yet checks which it DOES. Once a model crossing carries its role (the natural
spelling is the existing `model.<role>` capability token, item 343, which
already parses), the reach of a routed action is enumerable and a crossing to
an unrouted role is refused. This is the slice that turns the permission into a
covering one, and it is deliberately after S2 because the value side is the
half a confidential input can actually be lost through.

**S5. The role in the manifest.** A role declared in the composition manifest
rather than in a source file, which is what item 515 needs to bind a role to a
provision by key and what item 538 means by "a role is declared once and bound
to a member by configuration". The source-level `model role` stays as the
one-file form. No new rule, a second place to write the same declaration.

---

## 9. What this leaves to 513 to 519

Stated so the next agent on each does not redesign the seam.

* **513, grammar-constrained decoding.** Inherits the placement, needs nothing
  from it. A role is where a call goes; a grammar is what the call may return.
  The one seam is that a role may eventually declare whether its member
  supports constrained decoding at all, which belongs to 515's profile and not
  to `residence`.
* **514, the origin ceiling.** Inherits `check()`'s return value, which is
  `{component: {action: {origin: {role, residence}}}}` for exactly this reason,
  and inherits section 2.1 as the rule it must enforce on values. 514 is S2
  above. It should NOT re-derive the route table from the AST.
* **515, the portfolio.** Schedules inside the boundary. It owns the device
  profile, the load and unload cost, the shared provision, and the question of
  which of two admissible roles to use. It must not be able to widen a
  placement: a scheduler that picks a role no arm names is the fail-open shape,
  and S4 is what makes that refusable.
* **516, the council.** Places each member by 512 and 514, so a local adversary
  may read an origin the cloud proposer may not. It needs several roles bound
  in one aggregation, which is why roles are program-level here and not
  component-level.
* **517, the signed decision object.** Records the placement in force. The
  field it needs from here is the role name and its residence, both of which
  are declared rather than observed, so a `ModelDecision` can bind them by
  value without asking a host.
* **518, shadow routing.** Moves an action from one role to another. Both roles
  must be routable by the action's own block for the shadow to be admissible at
  all, which is a property of the route table and is the natural place for that
  gate to sit.
* **519, the attenuation product.** Adds the role to the product with a
  declared reachable-capability set. It should extend `model role`, not
  introduce a second role declaration, and it is the item that makes a role
  something a component can be attenuated BY rather than merely placed on.

---

## 10. Things stated here that are not verified

* That `on_device` corresponds to any physical fact. It is a declared claim,
  checked only against the arms that name it (section 5).
* That the eleven decisions are complete. They are the ones a declaration can
  be wrong in; the value side is open until S2 and the crossing side until S4.
* Any claim about tiers other than the reference. Nothing in this slice reaches
  an emitter, so there is nothing tier-specific to be right or wrong about, but
  that is an argument and not a measurement.
