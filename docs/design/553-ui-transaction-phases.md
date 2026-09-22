# Design: UI transaction phases, the revert split, and the confirmation gate

Design-doc id 553, assigned rather than chosen (four lanes collided on 531 by
checking `main` alone, and three on 536). Roadmap item served: 522 (issue
#1196), Slice 2 and Slice 4 of the plan in `docs/design/538-ui-transactions.md`
section 10. Slice 1 is that note and is merged; this one starts where its
section 11 stops.

Direct dependencies, all read at `3975045f` on `main`: 521 (issue #1195, the
computer-use namespace, `docs/design/532-typed-computer-use.md`), 546 (issue
#1225, PR #1256, merged, the general state-class rule), 246 (the approval
authority), 254 (the compensate-grade crossing on the erase report), 29 (the
erase report itself). Adjacent and not restated: 539 (the computer-use
substrate, upstream `inso1337/revl-harness#11`), 525 (issue #1200, PR #1275,
open, whose demo asserts this item's refusals - section 8).

Sources studied: `src/revl/ui_family.py`, `src/revl/erase_report.py`,
`src/revl/query.py` (`classify_compensation`), `src/revl/policy.py`
(`approval_admission`, `evaluate`, `render_report`), `src/revl/lower.py`
(`_approval_index`, `_approval_covers`, `_emit_crossed_caps`,
`_lower_emit_approval`), `src/revl/typecheck.py` (the `Approval` arm),
`src/revl/parser.py` (`let a = await approval[C]`),
`src/revl/layer_state.py` (item 546), `src/revl/__main__.py` (the `audit
--policy` leg), and `tests/test_approval_typed.py`.

## 0. The decision in one paragraph

Slice 1 stopped a program CLAIMING cleanliness. This slice makes the report
tell the truth about what it found, and puts the operator's confirmation gate
on the surface an operator actually reads. Three things land: the eight phases
as a COMPILE-TIME ELIGIBILITY decision plus a static plan read off the lowered
IR; a FIVE-WAY residue split that replaces one word covering three outcomes;
and `revl audit --policy` running the approval-requirement gate that until now
only `revl.mcp.session` ever called. Postcondition verification lands as a
per-step VERDICT whose strongest value is deliberately not `verified`. The
`Approval[C]` scoping blocker from 538 section 6 is NOT resolved - it is
re-measured, and section 3 says which of that section's three options this
slice took and what it does and does not buy.

## 1. The measured false word

`revl erase-report` had two words for a boundary crossing: `compensated` when
an offset is attached, `bare` otherwise, under a note reading

> a bare crossing left the system with nothing done about it; the report lists
> it precisely so it can be handled out of band.

Run against a computer-use agent (the item-525 flagship, `revl erase-report
ladder.rvl --realm billing` at `dfecba2a`):

```
[2] BOUNDARY CROSSINGS — 7 (1 compensated, 6 bare)
    [BARE]         LegacyAgent  host actuate()        <- ui.click
    [BARE]         LegacyAgent  host fetch_receipt()  <- ui.download
    [BARE]         LegacyAgent  host locate()         <- ui.find
    [BARE]         LegacyAgent  host read_pane()      <- screen.observe
    [compensated]  LegacyAgent  host type_amount()    <- ui.text
```

`read_pane` is `screen.observe`. It is a READ. Nothing was done about it
because there is nothing to do and there is nothing to handle out of band.
`actuate` is `ui.click`: no inverse EXISTS, so nothing CAN be done. One word
carried three readings and two of them were false:

1. nothing needed to be done (a read);
2. nothing could be done (no inverse exists);
3. nothing was done though an inverse exists (avoidable residue).

This is not cosmetic. A residue report that over-reports a read is the same
defect as one that under-reports an actuation, because an auditor who learns to
discount `bare` discounts it everywhere - and the crossing they then discount
is the click. The collapse runs at the other end too: `compensated` covered
both a `ui.text` inverse that RESTORES the field it typed into and a generic
offset that merely counteracts a crossing. Those are different facts and the
issue's own framing says so: a revert must report what it RESTORED and what it
only COMPENSATED, separately.

### 1.1 The five states

`src/revl/ui_transaction.py`. Disjoint, and none is a synonym for another.

| state | what it asserts | residue? |
| --- | --- | --- |
| `untouched` | the step changed no state the target owns | no |
| `restored` | an inverse exists, was declared, and puts the state back | no |
| `compensated` | an offset was attached that is not a restoration | yes |
| `uncompensated` | no inverse exists, and no run of anything changes that | yes |
| `unregistered` | an inverse exists and nobody declared one | yes |

The aggregate over a step set is the WEAKEST part, which is item 546's rule 3
applied to steps rather than layers. `unregistered` is the weakest, BELOW
`uncompensated`, and the ordering is not a preference: `uncompensated` is a
stated outcome with a stated bound, while `unregistered` is an omission, and
item 546 orders `undeclared` below `neither` by exactly that argument. Slice
1's own note reaches the same place from the other side, calling a missing
inverse "the worst of the three outcomes: not restored, not reported"
(538 section 5, R2).

### 1.2 One vocabulary, not two

Item 546 landed first and its `layer_state.OUTCOMES` is `("restored",
"compensated", "uncompensated", "untouched")`, with a note saying
`uncompensated` "is item 522's word for that fact and is taken from it rather
than renamed". Those four are these four, spelled identically, and
`tests/test_ui_transaction_phases_522.py` asserts the set equality so neither
item can grow a word the other does not have.

The fifth, `unregistered`, has no member there and should not: 546's list is
what a ROLLBACK DID, and `unregistered` is a fact about a DECLARATION known
before anything runs.

## 2. The phases, built as an eligibility decision

The item proposes observe, plan, stage, preview, confirm, commit, verify,
compensate. 538 section 3 already said which of those revl decides; this slice
makes that table executable, because a phase list nothing reads is the implied
guarantee 539's own argument warns about.

`ui_transaction.ELIGIBLE` maps a registry class to the phases a step of that
class may OCCUPY. The load-bearing rows are the ABSENCES:

- a `reversible` verb is eligible for `verify` and NOT for `commit` or
  `compensate`. That is 538 section 3.1 made checkable: a `verify` phase built
  from a fresh `screen.observe` / `ui.find` pair does not grow the residue it
  is checking;
- an `irreversible` or `unknown` verb is NOT eligible for `compensate`. That is
  the whole item in one table cell - a transaction may not place a step with no
  inverse in the phase whose name means it was undone.

`plans(ir)` then reads a composition and produces, per provide method and per
activation body, the ordered steps with each step's class, eligible phases,
residue state, confirmation state and postcondition state, plus the LIFO
compensation order and the weakest-part aggregate. `revl erase-report` prints
it as section `[4]`.

Nothing executes. The section header says "a static reading; revl executes no
phase" because that is what it is; the substrate belongs to item 539.

## 3. The confirmation gate, and the blocker re-measured

538 section 6 recorded three options. This slice takes the THIRD, and the
reason the other two are still unavailable is re-measured rather than quoted:

- `await approval[C]` in a provide method raises "`await approval[C]` is only
  allowed in a component activation body, not a provide method";
- `Approval[ui.click]` written as a parameter type raises "`Approval[...]`
  cannot be written as a type".

Both are asserted in
`test_the_provide_method_cannot_acquire_the_approval_the_gate_wants`, so the
blocker is a test rather than a claim. A computer-use loop lives in a provide
method. Option 1 (extend `Approval[C]` there) is a change to item 246's
non-persistence rule and must be argued at item 246, not smuggled in here.
Option 2 (a per-transaction ticket) is a second authority unless it is spelled
as a restriction of the first, and this slice has no place to put that
restriction yet.

### 3.1 What option 3 was missing, and what this slice adds

538 called option 3 "already works ... and is not sufficient on its own,
because a policy rule is optional and absence is silence". Measuring it found
something worse than optional.

`capability ui.click requires approval` is the operator raising `ui.click` to
`confirm-required`. `policy.approval_admission` enforces it correctly - and at
`dfecba2a` it was CALLED from exactly one place, `revl.mcp.session`. So the
gate was a SESSION gate, and the static surface an operator reads before
shipping said this, on the very same composition:

```
$ revl audit ladder.rvl --policy click.pol
boundary policy (click.pol): clean — every component's reach is within its
declared authority.
$ echo $?
0
```

A gate whose static surface says clean is a gate an operator learns not to
consult. This slice adds one line to the `audit --policy` leg of
`src/revl/__main__.py` so that surface runs the same gate the session runs.
`policy.evaluate` is untouched, so every other caller keeps its exact contract;
this is the one surface that claimed to BE the gate.

### 3.2 What that buys, and what it does not

It buys: an operator who writes the rule is told, by the tool they run, that
the composition is refused, with the acquisition named. It does not buy: a rule
the operator never wrote. Absence of a policy rule is still absence.

So the other half is a REPORT, not a refusal. `plans()` computes a
confirmation state per crossing - `confirmed-per-crossing` (an `Approval[C]`
edge on the `emit`, the only unforgeable form), `confirmed-per-session` (an
operator rule raises it), `unconfirmed` - and section `[4]` of the erase report
ENUMERATES BY NAME every step that is both `uncompensated` and `unconfirmed`.
That is what stops absence from being silence: an operator does not have to
know to ask.

revl still does not refuse the click. Making the confirmation mandatory would
not gate the feature, it would refuse it, for the reason section 3 measures,
and refusing item 521's flagship program is not a gate, it is a retraction.

### 3.3 The raise, and why `confirm-required` still has no member

Slice 1 asserted that no verb is BORN `confirm-required` and made the emptiness
a test so a later slice could not ship the label without the check. This slice
lands the raise and the emptiness STAYS TRUE, because the raise is a FUNCTION
of `(registry class, operator policy)` and not an entry in the table:
`raised_class(token, approval_required=...)`.

It is monotone by construction - it only ever strengthens the obligation - so
an operator may raise a verb and may not lower one. Slice 1 kept authors from
lowering a class; the operator is not exempt from that rule, they are simply
allowed to go the other way.

Item 546 refuses to fold `confirm-required` into a state class
(`class-not-a-state-class`), because it says who may authorise a step rather
than what the step leaves behind. This slice keeps that distinction
STRUCTURALLY, not by convention: a step carries its registry state class and
its raised authority class in separate fields, and the residue verdict is
computed from the state class alone, so a raise can never move a step's
residue. `test_a_raise_never_moves_a_steps_residue` is the assertion.

## 4. Postcondition verification

538 section 3.1 is kept: a UI actuation's return value says the actuation was
DELIVERED, not that the business effect occurred. A click can land on a control
that was re-rendered, on a disabled button that swallows it, or on the right
button in an application that then failed server-side. So the postcondition is
a fresh `screen.observe` / `ui.find` read.

`postcondition(token, followed_by_read=...)` returns one of three:

| verdict | when |
| --- | --- |
| `not-applicable` | a read; it actuates nothing, so it has no postcondition |
| `unverified` | an actuation with no read after it in the same method |
| `verified-against-untrusted-read` | an actuation with a read after it |

There is deliberately NO plain `verified`, and
`test_there_is_no_plain_verified_verdict` asserts the absence so a later slice
cannot quietly add the stronger word without arguing for it. 538 section 4 is
the argument: accessibility metadata comes from the application, and a hostile
or merely broken application is the same input to revl, so a postcondition read
through a surface the target controls cannot be stronger than that surface. A
verified postcondition raises confidence; it does not establish a fact.

The honest limit of the IMPLEMENTATION is stated in the report's own words: the
check is POSITIONAL. It says a read follows the actuation in the same method,
not that the read checks THAT actuation. Binding a postcondition to its step
needs item 521's slice 4 (`UiTarget` as a record that survives a phase
boundary); until then, naming the unverified steps is the honest half and it is
the half that is landable.

## 5. Failure direction of every decision here

| decision | fails | why that is the safe side |
| --- | --- | --- |
| the aggregate is the WEAKEST part | closed | one unclassifiable step cannot be averaged away by four clean ones (item 546 rule 3) |
| `unregistered` is below `uncompensated` | closed | an omission must not inherit the exit of a stated class |
| an unrecognised token gets NO verdict | closed | this module declines to speak about a crossing it does not understand rather than defaulting to permissive; the caller's existing tagging stands |
| `unknown` and `irreversible` both reach `uncompensated` | closed | `ui_family.NO_INVERSE`'s join, read once and not copied |
| a read is `untouched`, not `bare` | closed | over-reporting a read trains an auditor to discount the word, and the crossing they then discount is the click |
| a read is `confirmation-not-required`, not `unconfirmed` | closed | the same error at the confirmation surface: one word for "nobody authorised an actuation" and "there was no actuation" |
| there is no plain `verified` | closed | a postcondition read through a surface the target controls cannot be stronger than that surface |
| `audit --policy` runs the approval gate | closed | it was already refused at session load; this removes a disagreement between two surfaces, in the direction of the stricter one |
| a raise cannot move a residue verdict | closed | otherwise an authority class would silently become a state class, which is what item 546 refuses by name |
| the unconfirmed steps are NAMED, not refused | open, deliberately | mandating the confirmation would refuse item 521's flagship program rather than gate it (section 3) |
| the plan walk descends EVERYTHING but a registered inverse | closed | a list of statement kinds is how seven spellings of a crossing came to be dropped (section 5.1); a generic walk fails toward naming a crossing, and the two keys it skips are named with a reason |
| BOTH arms of a branch are reported | closed | the plan cannot know which arm runs. Reporting the arm a given run skips over-states the plan by one crossing; reporting neither leaves an irreversible actuation with no verdict at all |

### 5.1 The walk that dropped a crossing, and what replaced it

`method_plan` collected a step for a crossing written three ways -- `emit e`,
`let x = e`, and a bare expression statement -- and read only the TOP node of
each one's expression. Seven other spellings of the same crossing therefore
reached no plan at all:

| position | example |
| --- | --- |
| `return` | `return emit click(t)` |
| an `if`'s `then` arm | `if (c) { let x = emit click(t) }` |
| an `if`'s `else` arm | `if (c) { ... } else { emit click(t) }` |
| a `while` body | `while (c) { let x = emit click(t) }` |
| a `for` body | `for (s of xs) { let x = emit click(t) }` |
| an assignment | `n = emit click(t)` |
| nested in a larger expression | `let x = emit click(t) + 0` |

A step that is not in the plan is not reported as uncovered -- it gets no
`residue` verdict, no `confirmation` state, no `postcondition` verdict, no row
in the erase report's `[4]` section and no entry in
`unconfirmedIrreversibleSteps`, which is the enumeration issue #1293's derived
note is keyed by. The plan instead reports that the method made no such
crossing, which is the fail-open direction in the one namespace whose point is
that an irreversible actuation is never silently uncovered. Tail position is
where an actuation most naturally lands, since a provide method that returns
what it clicked has nothing left to bind.

The repair is a generic walk over the statement, in evaluation order, rather
than a longer list of statement kinds -- a list only defers the problem, since
every step kind the language grows is a new way to hide a crossing until
someone remembers to extend it. `emission_analysis._calls_in` already makes
that call for the same reason; the plan's walk differs only in keeping ORDER
and the approval edge, which a set cannot. The two keys it does not descend
are the registered inverses, `compensate` and `undo`: both are entries on the
teardown accumulator that run on unwind rather than in the forward order the
plan reports, and whether a crossing has an inverse is already carried per
step. Counting them would claim an actuation the program never makes.

`tests/test_ui_rung_reaches_every_obligation.py` measures all eleven positions
(the seven above, plus the two that always worked as controls, plus the
compensated and both-arms readings). Nine of the eleven fail on the walk this
replaces; the two controls pass on both, so a walk that reported nothing would
fail the file rather than satisfy it.

#### Why a dropped crossing read BETTER than the truth

`aggregate` is the weakest state PRESENT in the step set, which is item 546's
rule 3. A step that is absent from the fold is therefore not a gap in the
answer, it is a step that cannot drag the answer down: a program whose only
uncompensated actuation sat in one of the seven positions above aggregated to
`restored`, or to `untouched`, and its claim carried no "may not be reported as
cleanly reverted". That is the same shape as the over-reported read this module
exists to fix, in the other direction, and it is why this is a semantic change
and not a listing change.

Measured over every program in the tree that gets a plan at all, which is the
embedded programs of the six item-521/522 suites (25 distinct compositions, 20
with a plan): 12 plans move and 9 of the 12 change their aggregate, all 9 from
a state that reads clean (`untouched` or `restored`) to `uncompensated`. The
item-525 flagship demo does not move -- it has no crossing in any of the seven
positions -- and its erase report is byte-identical.

#### The one neighbouring fold, and why only this one had the hole

`revl erase-report` folds a realm's computer-use crossings twice. Section
`[1]`'s `boundaryCrossings.uiResidue`, the per-crossing `uiResidue` tag,
`residueSteps` and that section's own `compensateOrder` all read
`query.Composition`'s reachability facts, which do not know where in a body a
crossing was written. None of them ever had this hole, and none of them moves.
Section `[4]`'s plan is the one that reads a method body statement by
statement, and its residue verdict, its confirmation state, its postcondition
verdict, its `compensateOrder` and its `unconfirmedIrreversibleSteps` were all
blind together, because all five are computed from the one walk. So the same
report answered the same question about the same click two ways, and the
weaker answer was the one a reader would take as the transaction's own
verdict. `test_the_two_computer_use_folds_agree_about_a_tail_crossing` pins the
agreement as an equality so the two folds cannot drift apart again.

## 6. No new guarantee code

Nothing here registers one. The gate reuses the item-246 `approval` violation
kind, which already ships with its own message, why-trace and navigation
record. The residue split is a REPORT and refuses nothing. Slice 1's two
refusals (G4, `reversibility`) are untouched.

## 7. Self-host

Does this need a self-host port? No, and for 538 section 9's reason unchanged:
`selfhost/lower.rvl`'s `admit_src` issues no admission at all, so the gate is
already fail-closed for this construct. No `selfhost/*.rvl` file is touched and
`tools/build_gate_crate.py --check` reports in-sync, so no crate is
regenerated.

The obligation 538 recorded still stands and still is not discharged: when a
slice starts deciding ADMISSION on a reversibility class, the self-host must
either port the check or decline the program by a NAMED marker. This slice
decides admission on an OPERATOR POLICY, which is `revl audit --policy` and not
a language construct, so it does not cross that line.

## 8. Effect on the open item-525 demo (PR #1275)

The demo asserts the erase report's exact tags, and three of them change. It is
open and not merged, so the lines are named here rather than edited:

```
demo/legacy_enterprise/run_demo.py, step 9

-    _check("[BARE]         LegacyAgent  host fetch_receipt()" in report,
-           "the `ui.download` crossing reports BARE - nothing was done about it")
-    _check("[BARE]         LegacyAgent  host actuate()" in report,
-           "the `ui.click` crossing reports BARE too, because unknown is not clean")
-    _check("[compensated]  LegacyAgent  host type_amount()" in report,
-           "the `ui.text` crossing reports compensated - an offset landed")
+    _check("[UNCOMPENSATED] LegacyAgent  host fetch_receipt()" in report,
+           "the `ui.download` crossing reports UNCOMPENSATED - no inverse exists")
+    _check("[UNCOMPENSATED] LegacyAgent  host actuate()" in report,
+           "the `ui.click` crossing reports UNCOMPENSATED too, because unknown is not clean")
+    _check("[restored]     LegacyAgent  host type_amount()" in report,
+           "the `ui.text` crossing reports restored - its inverse puts the field back")
```

The demo's own thesis ("the irreversible step, and the word that may not cover
it") is the argument for this change, so the replacement lines say what that
section was already trying to say. The demo's POLICY (`component LegacyAgent
may not reach ui.download`) carries no approval rule, so the gate in section 3
does not fire on it and steps 1-8 and 10 are unaffected.

## 9. What is not verified

- **No phase executes.** There is no runtime transaction, no LIFO run, no
  scheduler. `compensateOrder` is an ORDER, not a result, and the report says
  so. 538 slice 3 is still open and still needs item 521's slice 4.
- **The postcondition check is POSITIONAL.** It reports that a read follows an
  actuation in the same method, not that the read checks that actuation.
  Section 4.
- **The check-to-use race is open**, unchanged from 538 section 4. Nothing in
  the tree resolves a UI target into a handle, so every phase boundary
  re-resolves by name.
- **`unregistered` is unreachable from a program that admits today**, because
  slice 1 refuses the declaration that omits the inverse. It is implemented and
  unit-tested, not measured on a compiled program, and it is written down so
  the ordering in section 1.1 is the one the design argues and so a recorded IR
  from before that refusal still gets a truthful word.
- **The gate still depends on the operator writing a rule.** Section 3.2. What
  landed removes the surface disagreement and names the unconfirmed steps; it
  does not manufacture an authority nobody invoked.
- **The plan does not follow a callee.** It reads one method body. A crossing
  reached through a module `fn` that body calls is not a step in its plan --
  unchanged by the walk repaired in section 5.1, which is a syntactic fix
  inside one body. `emission_analysis` owns the transitive question and the
  G4/G8 gates read it there; whether the transaction plan should read it too
  was not decided here.
- **`revl audit --policy` was the only surface changed.** Other callers of
  `policy.evaluate` were left alone deliberately, and whether any of them
  should also run the approval gate was not investigated here.
