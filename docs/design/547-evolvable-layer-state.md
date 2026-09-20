# 547: the state an evolvable layer accumulates, and what a rollback of it actually did

Roadmap item 546 (issue #1225), from the 2026-09-20 architecture review of the
512-541 wave. Slice 1 is landed with this note. Slices 2 to 4 are designed here
and not written.

Design number 547 was ASSIGNED by the orchestrator, not scanned for. Four
collisions happened on 2026-09-20 because concurrent lanes each read the open
pull requests for the next free number before the others' pull requests
existed, and 531, 536 and 539 were each claimed twice that way. A scan is not a
reservation between lanes that run at the same time.

Reconciles with: item 518 and `docs/design/540-shadow-promotion.md` (issue
#1192, PR #1250), item 522 and `docs/design/538-ui-transactions.md` (issue
#1196, PR #1242), item 520 and `docs/design/537-evolution-controller.md` (the
lifecycle this gate is a precondition inside), item 243 (the witness and
inverse pair), item 249 (authority is derived from the granting side, never
from an author's qualifier), item 254 (the compensate-grade crossing the
residue report reads), item 523 (issue #1197, the generated tier matrix that
decides what a new guarantee code owes).

---

## 0. The decision in one paragraph

An evolvable layer's state class is a property of the LAYER, not of the effects
that wrote it, and it is decided in two places that compose as a ceiling and a
claim. A registry owns a per-layer ceiling, which is the strongest class the
layer may ever be claimed at, and a plan declares a class per part of each
layer at or below that ceiling. A layer's aggregate class is the weakest of its
declared parts, so a plan cannot make an accumulating layer look revertible by
declaring only its revertible half. Absence is not a class: an undeclared part
resolves to `undeclared`, which is ordered below `neither` and which no
compensation lifts. The rollback report then carries four lists rather than one
word, and the outcome vocabulary is deliberately not the class vocabulary,
because a `compensatable` part whose compensation did not run is
`uncompensated`.

---

## 1. The gap, stated exactly

The evolution lifecycle ends in "promote or roll back". Rollback is the half
revl makes trivial: a revertible effect carries its inverse, unloading a
component replays the inverses in LIFO order, removal leaves no residue.

That guarantee is about effects. It says nothing about a routing table that
drifted across two hundred generations, a peer's accumulated trust score, a
threshold tuned twenty times, or a retention index rebuilt against a policy that
has since changed. Those are accumulated statistics, and an accumulated
statistic has no inverse. Reverting the component that wrote it does not restore
the world in which it was never written, and the next generation inherits the
drift as if it were measurement.

The item's own table, which `LAYERS` in `src/revl/layer_state.py` is:

| layer | accumulates | ceiling |
| --- | --- | --- |
| policies | no | `revertible` |
| routing | yes | `compensatable` |
| components | no | `revertible` |
| workflows | yes | `compensatable` |
| memory | yes | `compensatable` |
| runtime | no | `revertible` |
| device placement | yes | `compensatable` |

**A counting slip in the item text, corrected here.** Both the issue and the
roadmap paragraph say the guarantee "covers only the two that are not the
problem". Seven layers minus the four the same sentence names as stateful is
three, and the issue's own table lists three unstateful rows: policies,
components, runtime. The table is taken. Nothing in the code depends on which
number is right, but a reader counting rows against the prose should not have to
wonder which one drifted.

---

## 2. The general rule

Two lanes answered this shape for one layer each on 2026-09-20 and they
disagree about who is allowed to say what. The disagreement is the work.

**Item 518, PR #1250, shadow promotion.** The plan declares a class for every
layer and the gate checks the declaration rather than assuming one. Its section
9.1 says so directly: the classes in its own table "are a default a plan may
disagree with", because nothing in the tree yet writes either store and what
would actually be compensatable is unproven. Its concern is a module asserting a
class it cannot know.

**Item 522, PR #1242, the computer-use surface.** The class is registry-owned
and an author may not declare that a click is reversible. It is item 249's
argument moved one surface over: a classification an author can lower is a
classification a careless author lowers, and the hostile case is not even the
interesting one. Its concern is an author raising a class.

Both concerns are real and neither subsumes the other. The rule that holds both:

1. **The registry owns a ceiling.** A layer that accumulates state may not be
   claimed `revertible` by anyone, including by a plan its own author wrote.
   This is item 522's rule, stated at layer granularity.
2. **The plan declares a class per part, at or below the ceiling.** This is item
   518's shape, and it keeps the freedom item 518 needs: a deployment that knows
   its agreement ledger really is compensatable may say so, and one that does
   not may declare `neither` and carry a compensation instead.
3. **A layer's aggregate is the weakest of its declared parts.** A layer is
   exactly as restorable as its least restorable part.

Rule 3 is where the leverage is, and it is the refusal worth reading the code
for. `route-arm` really is revertible: the arm is a declaration and re-declaring
it restores the prior world exactly. A plan that declares routing by its arm
alone is therefore locally true in every clause and globally the exact bug the
item is about, because routing then resolves to `revertible` and the rollback
report says the layer was restored while the accumulated preference goes
unmentioned. The ceiling catches it and the refusal names the layer's reason
rather than a rule number.

---

## 3. What an undeclared layer resolves to, and why

`undeclared` is its own value, ordered BELOW `neither`, and no compensation
lifts it. The only exit is to declare a class.

The fail-open shape is a class that is never stated defaulting to `revertible`,
and that value is not reachable by omission anywhere in the module. But mapping
an omission to `neither` is not good enough either, and the difference is the
part worth stating. `neither` is a class with an escape hatch: declare a
compensation, record it tested, and the promotion is admitted. If an omission
resolved to `neither`, a plan could attach a compensation to a part nobody ever
classified and promote it, and the gate would have checked a compensation
against no claim at all. `undeclared` has no escape hatch, so an omission
refuses until somebody states what the part is.

This is the shape PR #1253 resolved for held capability sets, citing
`lower._spawn_emission_surface`: an emission method with no declared capability
list maps to the unnameable `*` rather than to `set()`, so the missing answer
resolves to the value nothing covers instead of to the empty one, and a held set
that covers everything still does not cover it. `undeclared` is the same move on
the class lattice.

Two consequences follow and both are tested:

* **An omitted layer refuses rather than reading as untouched.** `untouched` is
  a declaration a plan makes, not a state the absence of a declaration implies.
  A plan must account for all seven layers.
* **`Part(name, "undeclared", a_tested_compensation)` still refuses**, with
  `layer-undeclared` and not with any compensation code, and the refusal text
  says why: a part nobody classified is not a part that is hard to restore.

---

## 4. Mapping the two lanes onto the rule

### 4.1 Item 522's five classes

| item 522 | here | note |
| --- | --- | --- |
| `reversible` | `revertible` | the step changes no state the target owns |
| `compensatable` | `compensatable` | an inverse exists and only the author can write it |
| `irreversible` | `neither` | no inverse exists |
| `unknown` | `neither` | revl cannot tell |
| `confirm-required` | no fold, refused | see below |

`layer_state.fold("ui", token)` performs it and returns the class with the
REASON, so item 522's split survives the fold. Item 522 separates `irreversible`
from `unknown` for the diagnostic and joins them for the decision; both halves
hold here, asserted as `fold` returning the same class and different reasons.

`confirm-required` is refused rather than folded. It says who may authorise the
step, not what the step leaves behind, and folding an authority class into a
state class gives the vocabulary a fourth meaning nothing reads. Item 522's own
section 7 says it is orthogonal to the three; `fold` is that sentence made
executable, with the link `class-not-a-state-class`.

**Note on `unknown` and `undeclared`, which are not the same thing.** Item 522's
`unknown` is a registry row that records that somebody looked and has no answer.
`undeclared` here is the absence of a row. A recorded ignorance is a statement
and folds to `neither`, which has an exit; an absent statement is not and does
not. That distinction is why the two are not merged despite reading alike.

### 4.2 Item 518's three parts

`SHADOW_PARTS` records the part-to-layer assignment and nothing else. The
classes stay item 518's plan's to declare.

| item 518 part | layer here | item 518's class |
| --- | --- | --- |
| `route-arm` | routing | `revertible` |
| `agreement-ledger` | routing | `compensatable` |
| `placement-history` | device placement | `neither` |

Routing therefore aggregates to `compensatable`, which sits exactly at its
ceiling, and device placement aggregates to `neither`, which is below it. Item
518's plan is admitted here unchanged, which is the check that the general rule
did not quietly redefine the special case.

`from_shadow_layers()` lifts item 518's two maps into a plan here and takes
plain mappings rather than its `ShadowPlan` class, because that module is an
open pull request and this one does not import it. It will NOT infer that the
other five layers are untouched: a model-action promotion does not write them,
and the CALLER says so. Inferring it is the omission-reads-as-untouched shape
section 3 exists to refuse, and a test holds the adapter to refusing without the
`untouched` argument.

### 4.3 Which classification was taken where they disagree

Taken from item 522: the registry owns a ceiling and an author cannot raise a
class past it. Taken from item 518: the plan declares, per part, and the gate
checks the declaration rather than substituting its own judgement.

The one place this note overrides both is the OUTCOME vocabulary. Item 518's
revert report carries `restored`, `compensated` and a third list named `neither`
after the class. Item 522 uses `uncompensated` for the same fact, and item 522's
own exit bullet is that a transaction over a step with no inverse reports
`uncompensated` rather than `no_residue`. `uncompensated` is taken, because a
class is what a part IS and an outcome is what a rollback DID, and they come
apart: a `compensatable` part whose compensation did not execute is
`uncompensated` while remaining `compensatable`. `report(plan, ran=[...])` makes
that difference observable, and `OUTCOMES` is asserted disjoint from
`STATE_CLASSES` so the two cannot be confused by a later reader.

---

## 5. What the first slice refuses

Ten named links, all lowercase, checked in a fixed order. The order is not
cosmetic: coverage is checked before any declared class is read, so a
well-declared routing block cannot distract from a memory block that is not
there.

| link | fires when |
| --- | --- |
| `plan-malformed` | a layer declared twice, a part declared twice, a layer both untouched and carrying parts |
| `layer-unknown` | a layer name outside the registry. The registry is closed |
| `layer-unaccounted` | a registry layer the plan says nothing about |
| `layer-undeclared` | a layer with no parts and not untouched, or a part with no class |
| `class-unknown` | a class token outside the three |
| `class-not-a-state-class` | `fold` handed `confirm-required` |
| `class-above-ceiling` | a layer resolving stronger than its registry ceiling |
| `state-not-restorable` | a non-revertible part naming no compensation |
| `compensation-untested` | a compensation named and not recorded tested |
| `compensation-orphaned` | a compensation parked on a revertible part, or on a part the plan does not classify |

Two of these are worth the sentence.

`class-above-ceiling` is the item's headline. It is the only refusal that fires
on a plan in which every individual clause is true.

`compensation-orphaned` is hygiene with teeth. A compensation attached to a
revertible part is a path nothing runs, and a reader auditing the plan counts it
as cover that is not there. It is also what a typo in a part name looks like
from the outside.

### 5.1 No new guarantee code

Every refusal is a named lowercase link, the discipline `revl.deploy` uses, and
none is a G-code. A promotion gate refusing a plan is not the checker refusing a
program.

This is also a measured trap rather than a preference. Item 523's generated tier
matrix requires every registered G-code to carry a reproducer under
`examples/rejections/` or an `ACKNOWLEDGED` entry in `tools/tier_guarantees.py`,
and a code registered with neither reds main on
`tests/test_tier_guarantee_matrix.py`. That happened on 2026-09-20 and PR #1247
is the repair. This change registers no code and owes neither. A test asserts
that no link in `LINKS` begins with a guarantee code and that none of the
checker's registered codes appears quoted in the module.

---

## 6. The report: four outcomes, and not one word

`Rollback` carries `restored`, `compensated`, `uncompensated` and `untouched`,
and `render()` prints one sentence per non-empty list. The phrase "rolled back"
appears in none of them, asserted by a test, because that single word covering
two different outcomes is what the item objects to. The renderer's own words:

```
Restored to the state before promo-full: policies.
Not restored, compensated: routing by supersede_window, device-placement by
redate_history. The prior world is not back; these layers were made acceptable
again by a declared path.
Not written by this promotion, so nothing to undo: components, workflows,
memory, runtime.
```

`fully_restored` is true only when `compensated` and `uncompensated` are both
empty, so a caller that wants the one-bit answer gets one that means something.

`uncompensated` is empty by construction on any plan that passed the gate with
every compensation run, since `state-not-restorable` refused the non-revertible
part with no compensation. It is populated by `report(plan, ran=[...])` when a
compensation did not execute, which is the case a partial rollback is, and it is
reported rather than dropped so a partial rollback is legible without knowing
which check ran.

---

## 7. Failure direction of every decision here

| decision | fails | why that is the safe side |
| --- | --- | --- |
| an omitted layer refuses | closed | an omission is silence, and silence reading as "untouched" is the fail-open direction |
| an undeclared part resolves to `undeclared`, below `neither` | closed | `neither` has an exit and the absence of a statement must not inherit it |
| no compensation lifts `undeclared` | closed | otherwise the gate checks a compensation against no claim |
| an accumulating layer may not be claimed `revertible` by anyone | closed | item 522 and item 249: a class an author can raise is one a careless author raises |
| the aggregate is the weakest part | closed | the alternative lets a true clause about the arm stand in for the whole layer |
| the registry is closed to unknown layer names | closed | an unlisted layer is an unmeasured layer |
| `compensatable` carries the same compensation requirement as `neither` | closed | item 518's reason: a compensation is the only thing that makes the two different, so one without it is `neither` with a nicer word on it |
| `confirm-required` is refused rather than folded | closed | an authority class silently entering a state vocabulary is a fourth meaning nothing reads |
| a compensation on a revertible part refuses | closed | a path nothing runs reads as cover |
| `untouched` is believed | open, deliberately | section 10 |

---

## 8. Non-vacuity

The measurement, in the form item 522's slice 1 used.

`effects_only_admits` in the test file writes out the rule as it stands before
this change: a promotion is admitted when every effect it performs is
revertible, and the state a promoted rule accumulates while it runs is not an
effect, so nothing in that rule reads it. Every plan in the corpus performs only
revertible effects, so it admits all of them. `src/revl/layer_state.py` does not
exist on `origin/main`, so there is no second implementation of the prior rule
to disagree with.

* **Nine plans are admitted by the prior rule and refused here**, across seven
  distinct links: routing declared only by its revertible arm, memory left out
  of the plan, a routing part with no class, an undeclared part carrying a
  tested compensation, placement history declared `neither` with no
  compensation, placement history with an untested compensation, workflows
  claimed revertible, a compensation parked on a revertible part, and a layer
  nobody registered.
* **Five controls are admitted by both**: every layer untouched, policies alone
  declared revertible, routing with both halves declared, placement history with
  a tested compensation, and the full three-outcome plan.

The controls matter more than the refusals. A gate that refused everything would
pass the first list and fail the second.

---

## 9. Self-host

Does this need a self-host port? **No, and not later either as slice 1 stands.**
`selfhost/lower.rvl` decides admission of revl PROGRAMS. Nothing here is a
program construct: there is no surface syntax, no IR entry and no checker rule,
so there is no divergence for an oracle to catch and nothing for the native gate
to agree or disagree with. `tools/build_gate_crate.py --check` reports in-sync
and no crate is regenerated.

The obligation is recorded for the slice that changes that. **If a later slice
gives a layer declaration surface syntax**, the rule item 391 and item 417
establish applies: `selfhost/lower.rvl` must either port the check or decline
the program by a named marker, never by agreeing with silence. The oracles catch
divergence, not missing features, so a check the self-host does not run leaves
every oracle green.

---

## 10. Slice plan

**Slice 1, landed with this note.** `src/revl/layer_state.py` and
`tests/test_layer_state_546.py`: the registry, the ceiling and claim rule, the
aggregate, `undeclared`, the ten refusals, the four-outcome report, the folds
for both sibling vocabularies and the adapter for item 518's maps. 59 tests with
both sibling modules present, 57 with 2 skipped on a tree that has neither.

**Slice 2: `untouched` becomes a measurement.** Today a plan claiming a layer is
untouched is believed. The check is a WAL question rather than a declaration
one: a promotion that declared `memory` untouched and wrote a retention index
should be refused by comparing the declaration against the recorded effects of
the generation. This is the largest remaining hole and section 11 says so.

**Slice 3: the compensation becomes an object rather than a name.** `tested` is
a boolean an author writes. Item 518 requires the same boolean for the same
reason, and both are an assertion. A compensation should carry the test node id
that exercised it, so that "recorded tested" is a reference into the suite
rather than a claim. Deliberately NOT tightened in slice 1, because doing so
unilaterally would refuse item 518's plans before its pull request lands.

**Slice 4: the lifecycle wiring.** Item 520's controller has a promote stage and
no producer for this precondition. The adapter that hands a `Rollback` to it,
and the CLI surface that prints `render()`, belong with that item's slice that
lands the stage.

**Slice 5: the residue report.** Item 522 slice 4 extends the residue and erase
surfaces with `uncompensated`. When it lands, a layer-level `uncompensated`
should reach the same surface, so a generation's residue is one report rather
than two.

---

## 11. What is not verified

Written down so a reader does not infer more than was measured.

- **`untouched` is a claim and nothing checks it.** A plan that declares memory
  untouched and writes a retention index is admitted. This is slice 2 and it is
  the largest gap in the item today.
- **`tested` is a boolean an author writes.** The gate checks that the plan says
  tested, not that a test exists. Slice 3, and the reason it is not in slice 1
  is compatibility with item 518 rather than a judgement that the boolean is
  enough.
- **No layer store is written anywhere in the tree yet.** Nothing accumulates a
  routing preference, an agreement ledger or a placement history today, so which
  of them is really compensatable is unproven. This is item 518's own section 9
  caveat and it applies here with more force, because this registry covers seven
  layers rather than three. The ceiling is a bound on what may be CLAIMED; it is
  not a measurement of what is true.
- **The ceilings are derived from the item's table, which is an architecture
  review's judgement.** `policies` is `revertible` because a bounded rule set is
  the whole of its state. A deployment whose policy engine keeps a tuning history
  has a policies layer that accumulates, and the registry would be wrong for it.
  Nothing in the tree can tell the difference today.
- **Both sibling vocabularies were read at their branch heads, not at main.**
  The reconciliation tests import `revl.shadow_promotion` and `revl.ui_family`
  when they are importable and skip otherwise, so they are green on this tree
  with 2 skips and were run once with both modules copied in, where all 59 pass.
  If either pull request changes its vocabulary before it merges, those two
  tests are what will say so.
- **Nothing here has run against a real promotion.** Every plan in the test file
  is constructed. The gate is a pure function and is tested as one.
