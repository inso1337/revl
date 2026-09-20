# Design: the evolution controller, and an authority the loop may not evolve

Design-doc id **537**. 531 through 536 are claimed: 531 on
`agent/1186-route-model`, 532 on `agent/1195-typed-computer-use`, 533 on
`agent/1205-evolution-curriculum`, 534 on `agent/1206-evolution-reward`, 535 on
`agent/1207-held-out-scoring`, and 536 on `agent/1191-model-decision-evidence`. The number was taken against the open pull
requests rather than against `main`, because four lanes collided on 531 in one
day by each taking "next free" against the same `main`. The roadmap item of the
same number is unrelated, which is the established convention here.

Roadmap item served: **520** (issue #1194). Constrained by items 543 (issue
#1222) and 544 (issue #1223), and consuming the vocabulary of items 535 (issue
#1205), 536 (issue #1206) and 537 (issue #1207).

Neighbours it must not duplicate. Item 536 owns the reward and the retention
rule. Item 535 owns the curriculum. Item 537 owns the held-out draw. Item 334
owns `propose`/swap/rollback and the post-activation health gate. This document
owns the **lifecycle**: which stages exist, in what order, which of them nothing
can buy past, what the single verdict binds, and how the authority invariant is
enforced rather than asserted. It owns no probe and no measurement.

Sources studied, all at `b7bc229e` unless stated: `src/revl/gate.py`
(`ProposeResult`, `Gate.propose`, `_DECIDER_SERVICES`),
`docs/design/334-self-extending-runtime.md`, `src/revl/evolve_loop.py`,
`docs/evolve-loop.md`, `docs/capability-attenuation.md`,
`docs/design/473-slo-contracts.md`, `tools/gate_reference_census.py`,
`tools/affected_tests.py`, `docs/v2.0-roadmap.md` items 520 and 542 to 547, and
issues #1194, #1222, #1223. The three sibling lanes were read on their branches
with `git show`: `tools/evolution_reward.py` on `agent/1206-evolution-reward`,
`tools/heldout_scoring.py` on `agent/1207-held-out-scoring`,
`tools/evolve_curriculum.py` on `agent/1205-evolution-curriculum`. None of the
three is on `main` yet, and section 6 says exactly what this lane does and does
not depend on as a result.

## 0. The decision in one paragraph

The lifecycle is **seven stages and a barrier**, and the barrier is the design.
Four stages are preconditions (`trigger`, `propose`, `admit`, `authority`) and
three are measurements (`shadow`, `canary`, `observe`). The controller walks the
preconditions in order and **returns** on the first one that is not satisfied,
before a single measured record has been read, so there is no code path on which
a canary number can be compared against a precondition's verdict. The result is
one recorded `LifecycleVerdict` binding the proposal, one verdict per stage with
its own evidence, whether the measurements were read at all, and a terminal
decision drawn from three names (`PROMOTE`, `ROLL_BACK`, `REFUSE`) rather than
from a number. Every failure is named, and the failure direction is closed in
every direction: a missing record, an empty evidence list, an absent authority
axis, an unmeasurable diff and a raising judge are all failures, never passes.
The tool is `tools/evolution_controller.py`.

## 1. The gap, measured

Every mechanism item 520 lists exists and works. What did not exist is anything
that puts them in an order.

| piece | where it is | what it decides | what it does not decide |
|---|---|---|---|
| repair loop (item 62) | `src/revl/evolve_loop.py` | retry a refused candidate within a budget | whether a candidate should have been generated |
| cockpit (63), semver (64), history and undo (65) | the operator surface | what an operator sees and can undo | when to undo |
| `propose`/swap/rollback (item 334) | `src/revl/gate.py` | admit under the untrusted-author profile, swap, revert a FAILED or PENDING successor to gen N | anything about traffic, SLOs or authority *diffs* |
| `FORBIDDEN_GRANT` (item 334) | `src/revl/gate.py`, `_DECIDER_SERVICES` | refuse a granted set naming the decider | any other authority axis |

Two measurements of the gap, both taken rather than assumed:

* `ProposeResult` is the closest thing in the tree to a bound verdict, and its
  five terminal shapes are all about **one swap**: halted, refused pre-live,
  state undisclosed, reverted, swapped. None of them carries evidence from a
  shadow run, a canary or a reward, because `propose` never sees any. There is
  no object anywhere that holds a proposal and its measurement in the same
  record.
* `FORBIDDEN_GRANT` is the only enforcement of the authority half, and it is
  one refusal on one axis at one entry point. `gate.py` says so itself: the name
  check "inspects the granted SET only", and the structural enforcement is
  against the `host_admit` crossing on the composed IR. Nothing refuses a
  candidate whose *diff* reaches `src/revl/admission.py`, because nothing
  computes a diff.

So the missing thing is not a mechanism. It is the order, the binding and the
boundary.

## 2. The lifecycle

```
  trigger -> propose -> admit -> authority  ||  shadow -> canary -> observe  ->  decide
  --------------- preconditions -------------      ------ measured ------
```

| stage | kind | supplied by | what it answers |
|---|---|---|---|
| `trigger` | precondition | a gap record naming the artifact it was read off (item 535) | did a gap start this, or the clock |
| `propose` | precondition | the candidate record and its retry budget (item 536's `Candidate{tree, base, scope}`) | is it scoped, and can it terminate |
| `admit` | precondition | the standalone self-extension compile and the forbidden-grant rule (item 334, `Gate.propose`) | may this code exist at all |
| `authority` | precondition | the attenuation product, plus this module's fence and kernel enumeration (items 543, 544) | did any authority move |
| `shadow` | measured | a draw the candidate could not read (item 537) | does it behave on inputs it never saw |
| `canary` | measured | the SLO contracts (`docs/design/473-slo-contracts.md`) | does it behave on real traffic |
| `observe` | measured | the reward conjunction (item 536) | is the trajectory worth keeping |

The table is not prose: `SUPPLIED_BY` in the tool is exactly this mapping, it is
rendered into every verdict, and `test_every_stage_has_a_named_supplier` refuses
a stage without one. Item 520 requires each stage to cite the mechanism that
supplies it, so the citation is data rather than a paragraph.

### 2.1 Where this maps onto the two shorter spellings

Issue #1194 names five stages (propose, admit, activate, observe, decide) and
item 543 names nine (observe, diagnose, propose, compile, admission-check,
shadow-test, canary, measure, promote or roll back). They are the same
lifecycle at two resolutions, and this is the reconciliation:

* #1194's *propose* splits into `trigger` and `propose`, because "gaps trigger
  it, not a schedule" is a separate claim from "here is a candidate", and only
  the first one is checkable against an artifact.
* #1194's *activate* is 543's shadow and canary. Activation is not one moment;
  it is the two measured exposures, and `canary` is the one that puts the
  candidate in front of real traffic. The controller keys `ROLL_BACK` on a
  canary record for exactly that reason (section 4).
* #1194's *observe* is the reward read, item 536's conjunction.
* #1194's *decide* is the terminal decision, not a stage with evidence.
* 543's *diagnose* is folded into `trigger`: a gap that names its artifact is a
  diagnosis, and a gap that cannot name one is not.
* 543's *compile* and *admission-check* are one stage here, `admit`, because
  `Gate.propose` already performs them as one standalone compile under
  `AdmissionProfile.self_extension(granted)` and splitting them would invent a
  seam the tree does not have.
* 543's *capability and reachability diff*, which it lists as one stage among
  nine, is `authority`, and it is moved in front of the barrier. That is the
  whole content of item 543 and section 3 is about it.

## 3. Why the barrier is control flow and not a weighting

Item 543's argument, restated so the code can be checked against it: a canary on
one percent of traffic measures latency, refusal rate and answer quality **on
the paths that traffic happens to take**, while capability reach and taint edges
are **static** properties of the program. The rare path that widens reach is
precisely the path a one percent sample does not exhibit. A candidate with
excellent canary numbers and a marginally non-empty reachability diff must not
be promotable.

There are three ways to write that down and two of them are wrong.

1. **A weight of zero on the SLO components when the diff is non-empty.** This
   is the arithmetic spelling, and it fails the way every threshold fails: it is
   one edit away from a weight of 0.05, the edit looks like tuning, and nothing
   in the artifact says a boundary moved. Item 536 rejected a scalar reward for
   the same reason one level down, and a lifecycle that reintroduces a scalar
   above a conjunction has undone that work.
2. **A guard clause inside the promote arm.** Better, and still wrong, because
   the measured evidence has already been read and is sitting in scope. The next
   person to add a stage has a working comparison available to them, and the
   invariant survives only as long as nobody uses it.
3. **Return before the measurements are read.** This is what `decide()` does.
   The precondition walk is a separate loop that returns a complete
   `LifecycleVerdict`; the measured walk is unreachable code when a precondition
   refuses. The comparison item 543 forbids cannot be written, because the
   values to compare do not exist in that scope.

The artifact says so too. A verdict produced by the precondition arm carries
`measurements_read: false` and one `NOT_REACHED` record per measured stage, and
`test_the_verdict_json_shows_that_measurements_were_not_read` asserts both. A
reader of a refusal can see that the SLO dashboard was present and was not
consulted, which is the difference between an ordering rule and an ordering
rule somebody can audit.

### 3.1 The out-of-order finding

If a measured record was supplied anyway, the verdict carries an `OUT_OF_ORDER`
finding naming the stages. This is not a second refusal and it does not change
the decision. It is a report that a shadow or canary ran although the authority
diff refuses, which means some caller ran a stage it had no authority to run.
Treating that as reassurance is the exact shape issue #1222 compares to "a host
reading a green as permission to run code the gate never admitted".

## 4. The single verdict

`LifecycleVerdict` is the one recorded object item 520 asks for, and it binds:

* the proposal (tree, base, scope, attempt, budget, and the keys that were
  dropped by name);
* one `Verdict` per stage in lifecycle order, each with its own `reason`,
  `evidence` and refusal `code`, including the stages that were not reached;
* `measurements_read`, the barrier's own receipt;
* `findings`, the out-of-order report;
* the terminal `decision` and the `code` that produced it.

There is exactly one place in the module that constructs a decision, so there is
exactly one place a reader has to look and one place a future stage is added.

**Three decisions, no number.**

| decision | when | exit |
|---|---|---|
| `PROMOTE` | every precondition satisfied and every measurement satisfied | 0 |
| `ROLL_BACK` | a measurement failed and a `canary` record exists, so the candidate had gone live | 1 |
| `REFUSE` | a precondition is not satisfied, or a measurement failed and the candidate never went live | 2 |

`ROLL_BACK` versus `REFUSE` turns on one fact and not on severity: did the
candidate go live. `canary` is the stage that puts it in front of traffic, so a
canary record is the evidence that there is something to undo. Reporting a
rollback with no canary would claim an undo that never ran, which is the same
mislabel `Gate.propose` avoids when it refuses to report an E-Stop as
`SWAP_REVERTED`. The three exit statuses keep the distinction at the process
boundary, the way `tools/heldout_scoring.py` separates `REFUSED` from
`DIVERGENT`.

## 5. The authority invariant, enforced rather than asserted

> The system may evolve its behaviour, but it may not unilaterally evolve the
> rules that govern its authority.

### 5.1 Agreement with issue #1223, and what is left for this lane

Issue #1223 argues that stated as a policy this is a rule some later generation
can propose a change to, and that the boundary must instead be a **capability
the attenuation product refuses**. That is correct and this document does not
dispute it. A controller-level path check is a check on a diff, and a diff check
is exactly the kind of rule a sufficiently indirect change can satisfy while
moving the authority anyway. The attenuation product is the only mechanism in
the tree that accounts for services, realms, taints and budgets structurally,
and it is where the boundary belongs.

What a controller can contribute is the half issue #1223 lists first: **the
kernel set has to be enumerated in one place rather than understood.** It is
enumerated here, as `KERNEL_PATHS`, with every path drawn from #1223's own list
and every one of them asserted to exist in the tree
(`test_every_enumerated_kernel_path_exists_in_this_tree`), because an
enumeration naming a file that does not exist protects nothing and a rename can
otherwise empty it silently. When item 544 lands the attenuation-side capability,
this enumeration is the list it should consume, and the controller check becomes
the second of two entry points rather than the only one. That is the same
relationship `judge_admit` already has to `Gate.propose`'s structural
forbidden-grant rule.

Retention is on the kernel side, per #1223: `src/revl/retention.py` is in
`KERNEL_PATHS` and `retention` is one of the five `AUTHORITY_AXES`. A loop
permitted to update retention policies as ordinary behaviour tuning is a loop
that can extend its own `Retained[T, P]` deadline, and the kernel refusal cites
G-RETAIN by name (`test_the_kernel_refusal_cites_the_guarantee_it_defends`).

### 5.2 The fence: what "unilaterally" means as a check

`AUTHORITY_FENCE` lists the module and its tests. A proposal whose measured diff
reaches one of them is refused with `FENCE_TOUCHED` rather than judged, because
a proposal that edited the judge has not been judged. This is the mechanism
`tools/heldout_scoring.py` uses for `HELD_OUT_FENCE` and it is borrowed
deliberately, including the reasoning about what is **not** fenced: `src/`,
`selfhost/`, `backends/` and `crates/` are the subject of the loop, and fencing
them would forbid the work the loop exists to produce. The fence is the judge,
never the subject.

The operative word in the invariant is **unilaterally**. A fenced proposal is
routed to a human, not forbidden forever. That is the difference between a
boundary and a freeze, and it is why `FENCE_TOUCHED` is a distinct code from
`KERNEL_INTERSECTION`: the first says "not without a human", the second says
"not by this loop".

### 5.3 The diff is measured, never declared

Every check in section 5 reads the changed-file set, so the controller must not
accept the candidate's account of it. It does not:

* when the tree is a git tree, the set comes from `git diff --name-only <base>`
  plus untracked files, exactly the read `tools/evolution_reward.py` performs
  for its `scope` component, and that set wins;
* a declared set that **understates** the measured one is `DIFF_DISAGREES`;
* a tree whose diff cannot be measured is `DIFF_UNVERIFIED` and has no
  satisfiable path, so a proposal on an unreadable tree cannot promote.

`test_the_kernel_hit_is_found_even_when_the_candidate_hid_it` is the test that
matters here: a candidate declaring a clean diff while the tree edits
`src/revl/attest.py` is still refused with `KERNEL_INTERSECTION`.

### 5.4 An absent axis counts as moved

`authority_moved` treats an axis missing from the record as moved, not as empty.
An unmeasured axis that promotes is the fail-open shape, and `retention` is
exactly the axis issue #1223 says the wave currently files on the wrong side, so
a record that simply omits it must not read as clean.
`test_an_absent_axis_counts_as_moved_not_as_empty` pins it.

## 6. Vocabulary reused from the sibling lanes

The instruction was to use the sibling lanes' vocabulary rather than invent a
parallel one, and none of the three tools is on `main` yet. The resolution is to
reuse at the **data** level, which needs no import and works today, and to reuse
names at the type level so an import becomes a deletion later rather than a
translation.

| from | reused how |
|---|---|
| item 536, `Verdict{component, verified, reason, evidence}` | same dataclass, same field names, same "no third value" rule. One field is added, `code`, because a lifecycle refusal has to be nameable by a caller and a reward component does not. |
| item 536, `Candidate{tree, base, scope}` | same three fields, same `RECORD_KEYS` whitelist mechanism, same `prose_ignored` report. A candidate record written for the reward scorer is a valid proposal record here. |
| item 536, `Scorecard{retained, blockers, components, prose_ignored}` | consumed as the `observe` stage's evidence through the one adapter in `_normalise`. `retained` maps to `verified` because it is the same all-or-nothing question; no threshold is introduced in the translation. |
| item 537, `HELD_OUT_FENCE` | the fence *mechanism* and its "the fence is the judge, never the subject" reasoning, applied to a different set (`AUTHORITY_FENCE`). |
| item 537, `REFUSED` distinct from `DIVERGENT` | the same distinction at the lifecycle level, as `REFUSE` distinct from `ROLL_BACK`, with distinct exit statuses. |
| item 535, tasks that name the artifact they came from | the `trigger` stage requires a named artifact, which is what makes "event-driven, not scheduled" checkable. |
| item 334, `FORBIDDEN_GRANT` and `_DECIDER_SERVICES` | the code name verbatim and the service names as a superset, held to `gate.py`'s set by `test_the_decider_names_do_not_drift_from_the_gates`. |

What had to be added, and why each is not a duplicate of something above:

* `PRECONDITIONS` / `MEASURED` / `SUPPLIED_BY`. The stage table. No sibling has
  stages.
* `KERNEL_PATHS` and `AUTHORITY_AXES`. Issue #1223 asks for the enumeration in
  one file and there is no file to put it in yet.
* `LifecycleVerdict`. The binding object item 520 asks for. `Scorecard` is the
  same *shape* one level down (a list of verdicts plus a fold) but it folds a
  conjunction over commensurable components, while this folds an ordered walk
  with a barrier in it, and collapsing the two would lose the ordering.
* `not_reached`. A measured stage that was never read is neither verified nor
  failed-on-its-merits, and the artifact has to say which.

**The dependency this lane does not take.** It does not import any of the three
sibling modules. The `observe` adapter keys on a `retained` field, so it works
with item 536's scorecard JSON and with anything else that produces that shape,
and it needs no branch to be merged first. The cost is that the adapter is not
exercised against the real `tools/evolution_reward.py --json` output until that
lands; section 9 lists it.

## 7. The failure direction, and the named refusals

Fail-closed everywhere, with no third value, because a third value is where a
fail-open default hides. Fifteen named codes:

| code | fires when |
|---|---|
| `STAGE_SKIPPED` | no record at all for a stage |
| `EVIDENCE_MISSING` | a record that says yes and cites nothing |
| `STAGE_FAILED` | a record that reports failure |
| `NO_TRIGGER` | a trigger naming no gap or no artifact |
| `SCHEDULED_TRIGGER` | a trigger whose kind is a schedule |
| `UNBOUNDED_RETRY` | no positive `budget` or no positive `attempt` |
| `BUDGET_EXHAUSTED` | `attempt` past `budget` |
| `UNSCOPED` | the proposal declared no scope |
| `FORBIDDEN_GRANT` | the granted set names a decider service |
| `DIFF_UNVERIFIED` | the tree's diff cannot be measured |
| `DIFF_DISAGREES` | the declared diff understates the tree |
| `FENCE_TOUCHED` | the diff reaches the lifecycle's own rules |
| `KERNEL_INTERSECTION` | the diff reaches the admission kernel |
| `AUTHORITY_WIDENED` | a non-empty or absent authority axis |
| `JUDGE_RAISED` | a judge raised, or answered for the wrong stage |

`EVIDENCE_MISSING` is the one the item warns about directly. A controller that
promotes when a stage's evidence is *missing*, rather than failing, is the
fail-open shape, and this repository has measured ten separate checks that ran
on every PR and could not fail. `_record_for` has no branch that returns a pass
for an empty evidence list.

`AUTHORITY_WIDENED` and `KERNEL_INTERSECTION` are separate codes rather than one
`PRECONDITION_FAILED` because issue #1222's exit test asks for a refusal **by
name** on a non-empty diff, and a single generic code would satisfy the letter
of that while losing what a reader needs: which authority moved.

`JUDGE_RAISED` is the fail-closed arm for an exception inside a judge. A
`try`-less implementation would abort the run and leave a human to decide what
it meant, which is a fail-open default by another route.

## 8. Non-vacuity

Three demonstrations, all in `tests/test_evolution_controller.py`: 77 tests,
all passing.

1. **It refuses on missing evidence.** Parametrized over all four
   preconditions, twice: once with the record deleted (`STAGE_SKIPPED`) and once
   with the record present and its evidence list emptied (`EVIDENCE_MISSING`).
   Eight cases, eight refusals, and `measurements_read` false in each.
2. **It promotes on a complete proposal.** One proposal with every stage
   present and verified promotes, all seven stage verdicts verified, and the
   recorded verdict carries the base, the scope, the attempt and budget, the
   supplier of each stage, and non-empty evidence for every stage.
3. **A control cannot tell the two trees apart.** `control_any_green` is
   written in the test file, not in the tool, and promotes when any stage
   reports green, which is what "weigh the authority diff beside the SLO
   components" degenerates to once one component may compensate for another. It
   returns `PROMOTE` on the clean tree and on the authority-widened tree, and
   `PROMOTE` on a tree that edits `src/revl/attest.py`. The controller returns
   `PROMOTE`, `REFUSE` and `REFUSE`. The separation is therefore produced by
   the barrier and not by the widened tree being obviously broken.

Issue #1222's exit test is met directly and in its own words: a candidate with
perfect canary numbers (`p99 18ms` against a base of 41ms, zero refusals, plus
four percent on answer quality) and one widened reach is refused with
`AUTHORITY_WIDENED` before those numbers are read, and a control candidate whose
reach is unchanged promotes on **the same** measured evidence. The test asserts
that the string `18ms` does not appear anywhere in the rendered refusal.

Issue #1223's exit test is met for the enumeration half: seven kernel paths each
draw a named refusal citing the guarantee, and the disjoint control promotes.
The capability half is item 544's.

The trees are real git trees built in `tmp_path`, because the authority stage
measures the diff and refuses a declared one. A test that handed it a declared
diff would be exercising the path that cannot promote.

## 9. Slice plan

**Slice 1 (this change).** The reduction, the stage table, the barrier, the
fence, the kernel enumeration, the named refusals, the single verdict, the CLI
and its three exit statuses, and the reward-scorecard adapter for `observe`.
Oracle: `tests/test_evolution_controller.py`. Fixture: a git tree per case,
built in `tmp_path`, one edited path per case so the difference between a
promotion and a refusal is always a single edit.

**Slice 2: the adapters.** Each measured and precondition stage gets a function
that runs the real mechanism and returns its record, replacing hand-written
JSON: `Gate.propose` for `admit`, `tools/heldout_scoring.py --json` for
`shadow`, `tools/evolution_reward.py --json` for `observe`,
`tools/evolve_curriculum.py` for `trigger`. Oracle: for each adapter, a test
that the real tool's output parses into a stage record and that a failing run
produces a failing record. Blocked on the three sibling branches landing.

**Slice 3: the authority diff, computed.** Today the `authority` record's `diff`
object is supplied. Slice 3 computes it from the attenuation product across the
base and candidate trees, so the axes are measured the way the changed-file set
already is. Oracle: a candidate that adds one service reach to one component
produces a non-empty `capability` axis without anybody declaring it. This is the
slice that turns the strongest remaining declared input into a measured one, and
it should be sequenced with item 544 rather than before it.

**Slice 4: the controller is a component.** The lifecycle is a Python tool here.
Item 520's alternatives section rejects "let the harness own the loop" because
it would be outside the checked surface, which is why item 62 exists in revl at
all. The end state is a revl component whose stages are declared and whose
barrier is a checked property. Oracle: the self-host differential, the same way
every other checked surface is held.

## 10. Non-goals

* **Running anything.** No traffic, no compile, no draw, no cargo. The
  controller reduces evidence and nothing else.
* **A second admission decision.** `judge_admit` restates the name half of the
  forbidden-grant rule at a second entry point. It does not reimplement the
  structural rule on the composed IR, and section 6 says so rather than letting
  the docstring imply otherwise.
* **Scoring.** Item 536 owns the reward. Nothing here weights, thresholds or
  exports a number, and the decision is an enumeration for that reason.
* **Deciding what the kernel is.** `KERNEL_PATHS` transcribes issue #1223's
  list. Adding to it is a decision about authority and belongs in a change
  somebody reads, which is what the fence enforces.

## 11. What this design does not verify

Stated plainly, because a design that claims its own completeness is the thing
this repository keeps finding.

* **The kernel enumeration is a diff check, not a capability.** It refuses a
  candidate whose *files* reach the kernel. It cannot refuse a candidate that
  changes kernel behaviour through a path that touches no enumerated file, and
  issue #1223 is right that only the attenuation product can. Slice 3 and item
  544 close this; slice 1 does not.
* **The `authority` record's `diff` is supplied, not computed.** The fence, the
  kernel check and the disagreement check all run against a measured
  changed-file set, but the five axes are read off the record. A caller that
  hands in an empty `capability` axis for a candidate that widened reach is
  believed on that one input. This is the largest remaining declared input and
  slice 3 exists for it.
* **The `observe` adapter has never seen real scorecard output.**
  `tools/evolution_reward.py` is on an open branch, not on `main`. The adapter
  is written against that file as read on its branch and tested against a
  hand-built record of the same shape. If the shape changes before it lands, the
  adapter is wrong and nothing here will catch it.
* **`shadow` and `canary` are shapes, not integrations.** No held-out run and no
  SLO read happens. Their records are generic stage records today.
* **Termination is checked per proposal, not proved for the loop.** The
  `attempt`/`budget` pair makes one proposal halt. It is not a proof that the
  loop terminates, which would need the proposal-generation side, and item 520's
  "termination is a proof obligation" is discharged only in the weak sense that
  no single proposal can be retried forever.
* **Nothing enforces that a caller uses the controller.** A pipeline that
  promotes without ever calling `decide()` is not caught, here or anywhere. That
  is what slice 4 is for.
