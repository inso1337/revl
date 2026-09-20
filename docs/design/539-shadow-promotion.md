# 539: Shadow routing, accumulated agreement, and a promotion no measurement can buy

Roadmap: item 518 (issue #1192), from the 2026-09-19 external review. Slice 1
is LANDED with this note; slices 2 and 3 are designed here and not written.

Design number: 539. 531 to 538 were claimed on 2026-09-19 and 2026-09-20 (531
merged as PR #1220, 532 as #1228 and #1242, 533 as #1232, 534 as #1231, 535 as
#1230, 536 as #1239, 537 as #1241, 538 as #1242), so this note takes the next
free number after a scan of the open pull requests rather than of main alone.

Reconciles with: item 512 and `docs/design/531-model-placement.md` (the role
table this reads, and section 9 of that note, which assigns this gate its
position), item 517 and `docs/design/536-model-decision-evidence.md` (the
records agreement is accumulated over), item 520 and
`docs/design/537-evolution-controller.md` (the lifecycle this is one stage
inside, and the precondition barrier this reuses), item 496 and
`docs/verified-canary.md` (the replay comparison this does not reinvent), item
543 (issue #1222, the authority diff as a precondition), item 546 (issue
#1225, restore against compensate), item 250 Slice 3a and
`revl.wal.model_decisions` (the crossing key).

---

## 0. The decision in one paragraph

`revl canary` already compares two recorded worlds and attributes the first
differing step to an exact `(component, realm)`. Nothing accumulates that
evidence per action class or turns the accumulation into a promotion. This
note adds the accumulator and the predicate, and nothing else: no scheduler,
no live shadow, no traffic. A promotion is judged in two phases separated by a
barrier. Before the barrier are properties of the DECLARATION and of whether
the evidence is evidence: the action's own `route model` block must name both
roles, every record must verify, the policy and both host profiles must be
pinned, the authority diff must be measured on every axis and empty on all of
them, and every layer of accumulated state must be revertible or carry a
tested compensation. After the barrier is the accumulated agreement. The
barrier is control flow, and it is also a data barrier: the metric evidence is
not carried across it, and the agreement tally does not exist until it is
crossed. A promotion cannot be bought with a latency number because there is
no expression in the gate in which a latency number and an authority diff both
appear.

---

## 1. The gap, stated exactly

Item 518's own words: "`revl canary` already serves one realm on a successor,
compares the two recorded worlds, attributes the first differing step to an
exact (component, realm), and derives the revert with the survivors proved;
what is missing is the scheduling half."

The scheduling half is three separate things and they are not equally ready:

| piece | state | this note |
|---|---|---|
| serve a fraction of an action in shadow | needs a runtime seam per tier | slice 2 |
| accumulate agreement per action class | needs only records and a reducer | **slice 1, landed** |
| turn accumulated agreement into a verdict | needs only the accumulation | **slice 1, landed** |

Slice 1 is the first CHECKABLE slice because it needs nothing that is not
already a value. `revl.model_evidence` (item 517) produces the records, item
512's `model_route.check()` produces the route table, and the verdict is a
pure function of the two plus a declaration. Every part of it is testable
without a model, a host or a network.

---

## 2. What agreement is accumulated over

One shadow observation is a PAIR of item 517 records for one crossing: the
incumbent's decision and the candidate's.

The pairing key is `(component, step_index)`. That is item 517's own
`crossing_key`, which is item 250 Slice 3a's `revl.wal.model_decisions` key,
which is the completion's own `effect` record identity. No second correlation
is invented and no parallel log is written. `tests/test_shadow_promotion_518.py`
asserts agreement with `model_evidence.crossing_key` whenever that module is
importable, so the two cannot drift.

Two records AGREE when both of these match:

* `chosen_digest`, which is `candidates[chosen]` for a validated decision and
  absent for any other outcome. This is the digest of the completion the
  decision took.
* `outcome`, item 517's three-value vocabulary (`validated`, `exhausted`,
  `refused`).

Three things are deliberately NOT compared, and the reason is the same for all
three: they are how the answer was reached, not what it was.

* `model_digest`. A successor is by definition different weights. Comparing it
  would make every promotion diverge on every pair.
* `fallback_depth`. An answer that came from the second rung of the ladder is
  the same answer.
* `sampling`. A successor with a different native temperature that lands on
  the same completion agreed.

`residence` is not compared either, for the same reason and because the two
roles differ in it by construction whenever the promotion is the interesting
kind.

### 2.1. A suppressed prompt binding is not agreement

Item 517's `prompt_binding` is a MODE, not a digest, because the shipped
runtime's `revl_prompt_digest` refuses to hash a confidential or secret
prompt. A record whose binding is `suppressed` is a legitimate record of a
real decision.

It is also an unusable comparison. Nothing in a pair of suppressed records
witnesses that the two worlds were asked the same question, and agreement
between answers to two different questions is not agreement. So a suppressed
binding on either side REFUSES the window (`binding-suppressed`) rather than
being counted, and two bindings that differ in mode or in value refuse as
`binding-incomparable`.

This is the single most likely place for this gate to have been fail-open.
Counting a suppressed pair as agreement would mean that the more
confidentiality a workload carried, the more easily its models got promoted.

---

## 3. The barrier

Issue #1222 (item 543) states the rule this note is bound by: a small sample
measures latency, refusal rate and answer quality on the paths that traffic
happens to take, while capability reach and taint edges are static properties
of the program. The rare path that widens reach is precisely the path a small
sample does not exhibit. **No amount of SLO evidence may buy a non-empty
authority diff.**

PR #1241 implements that one level up as control flow: `decide()` returns on
the first unsatisfied precondition before any measured record is read. This
note reuses that shape and adds two things it can add because it works over
values rather than over stage records.

### 3.1. Control flow

```
plan -> route -> admit -> evidence -> policy -> authority -> state
---------------- preconditions --------------------------------
|| BARRIER
accumulate -> divergence / sample / threshold
---------------- measured -------------------------------------
```

`route` comes before `admit` because it needs no evidence at all: a promotion
to a role the action's block does not name is refused without a record being
read. `decide` returns inside each walk, so on a refusal no `Tally` object was
ever constructed. The verdict's `measurements_read` is DERIVED from whether a
tally exists rather than set by hand, and the stages the walk did not reach
are written into the artifact as `not reached`.

`tests/test_shadow_promotion_518.py` asserts the shape on the module's own
syntax tree: there is exactly one call to `accumulate` in `decide`, it is after
the precondition loop, each loop contains a `return`, and the route walk
precedes the single call to `admit`.

### 3.2. The metric does not cross

An `Observation` may carry an `slo` block. `admit()` does not copy it into the
`Pair` objects everything downstream works on, and `Pair` has no member for
it. So a metric is not merely unconsulted downstream of the barrier; it is not
there. The syntax-tree test asserts that `Pair` has no `slo` field and that
`admit` contains no `.slo` access.

`Observation.slo` is a property that increments a counter, and the verdict
reports `slo_reads` as a sum over the observations the caller supplied. That
number is a MEASUREMENT of this module's behaviour rather than a claim it
makes about itself, and it is zero on the promote path, the refuse path and
the revert path. If a later change starts reading a metric, the number in the
verdict moves and the tests fail.

### 3.3. A precondition may read the question, never the answer

`Pair` exposes the crossing, the two roles, the policy digest, the two
placement digests and the prompt binding. The members that say what the model
SAID are private and are read by `accumulate` alone. A precondition is handed
a tuple of `Pair` and cannot compute agreement from it. The syntax-tree test
asserts that no precondition function names an agreement member or reads a
private answer field.

### 3.4. Two codes, not one

`authority-widened` and `diff-unmeasured` are separate refusals. An axis that
widened and an axis nobody measured are different failures, and a single
`precondition-failed` would satisfy the letter of issue #1222's exit while
losing what a reader needs. An axis absent from the diff is refused rather
than read as empty; reading it as empty is how a gate passes a widening
nobody looked for.

The axes are `budget`, `capabilities`, `origins`, `retention` and `residence`.
The first four are issue #1222's and PR #1241's. `residence` is this item's
own: a promotion from an `on_device` role to an `off_device` one moves the
prompt off the device, which is an authority change whatever the agreement
says, and item 512 already refuses that statically for a confidential origin.

---

## 4. Two rules, on two sides of the promotion

The item asks for both "promote on a stated threshold" and "revert on the
first attributed divergence". Those are different rules and a gate that
applied both at once would make the threshold meaningless. They are separated
by whether the candidate is answering:

* **Before promotion** (`live` false) the candidate answers nothing and a
  divergence costs only agreement. `PROMOTE` requires the window to reach
  `min_observations` AND the ratio to reach `threshold`.
* **After promotion** (`live` true) the candidate's answer is the one in use.
  The FIRST attributed divergence is a `REVERT`, whatever the ratio is.

The test file runs the identical twenty-pair window through both and gets
`PROMOTE` and `REVERT`.

A divergence carries the crossing and the member, so the attribution is to an
exact `(component, step_index)` and an exact member of the record. That is
what item 496's rationale is about and what a threshold on a counter cannot
produce: the output when a shadow disagrees is a name, not a number.

---

## 5. Restore is not compensate

Issue #1225 (item 546) says revertible effects give no inverse for accumulated
state, and names routing as the layer with "accumulated preference, no
inverse". A promotion that has been live has accumulated exactly that. The
answer this note gives, plainly:

**A revert of a model-action promotion RESTORES one layer and only
COMPENSATES the other two.**

| layer | class | why |
|---|---|---|
| `route-arm` | revertible | the arm is a declaration; re-declaring it restores the prior world exactly |
| `agreement-ledger` | compensatable | the window was observed and cannot be unobserved; the compensation marks it superseded so a later generation does not read it as fresh measurement |
| `placement-history` | neither, by default | issue #1225's own row; the accumulated preference has no inverse and needs an explicitly declared path |

The plan DECLARES a class for every layer, and the gate checks the
declaration rather than assuming it. Issue #1225's second exit bullet is
implemented literally: a layer that is not `revertible` may not be promoted
without a compensation that is declared AND recorded as tested. The `tested`
half is included because an untested compensation is a claim that the revert
will work, which is the thing a test exists to support.

The third bullet is implemented in the verdict: a `REVERT` carries
`restoration` with `restored` and `compensated` as two separate lists, plus a
`neither` list that is empty by construction on any plan that reached a
promotion. `render()` prints them as separate sentences and the string "rolled
back" appears nowhere, which the test asserts. One word covering two outcomes
is exactly what issue #1225 asks to stop.

---

## 6. Failure direction

Fail-closed, with no third value and no default anywhere:

| shape | refusal |
|---|---|
| no observations | `evidence-missing` |
| no verifier resolvable | `evidence-unverifiable` |
| a verifier but no key | `evidence-unverifiable` |
| a verifier that raises | `evidence-unverified` |
| a record that does not verify | `evidence-unverified` |
| one crossing counted twice | `crossing-mismatched` |
| a suppressed or mismatched binding | `binding-suppressed`, `binding-incomparable` |
| no policy digest, or a changed one | `policy-drift` |
| no placement digest, or a changed one | `placement-unpinned` |
| an unmeasured authority axis | `diff-unmeasured` |
| a non-empty authority axis | `authority-widened` |
| an undeclared layer | `layer-undeclared` |
| a non-revertible layer with no compensation | `state-not-restorable` |
| an untested compensation | `compensation-untested` |

The direction that matters most: `revl.model_evidence` arrives with item 517
and is not on main yet, so the verifier is resolved lazily. When it cannot be
resolved, every record is unverifiable and the gate refuses. A gate that
promoted because its verifier was missing would be the fail-open shape this
repository has measured eleven times, and the test for it runs on a tree that
really does not have the module.

### 6.1. No new guarantee code

Every refusal here is a named LINK, the discipline `revl.deploy` and
`revl.model_evidence` use, and none of them is a G-code. A verifier refusing a
record is not the checker refusing a program. Item 523's generated tier matrix
requires every registered G-code to carry a reproducer under
`examples/rejections/` or an `ACKNOWLEDGED` entry in `tools/tier_guarantees.py`;
this change registers none and owes neither. The routing refusals that ARE
checker refusals already exist and already carry `G-MODEL-PLACE`
(`revl.model_route`, item 512). A test asserts that no link in this module
starts with `G-`.

---

## 7. What is declared rather than derived, and why

Two things, both stated so the next agent does not assume otherwise.

**The action name.** Item 517's record carries `component` and `step_index`
and does NOT carry the action. Agreement accumulates per ACTION class, so the
action half of the key is declared by the plan and the step half comes from
the records. Closing that gap is either a member added to item 517's body or a
derivation from the WAL's surrounding `effect` records, and both are larger
than this slice. Until one of them lands, a plan that names the wrong action
is checked against the route table but not against the records, so the gate
proves the promotion is ROUTABLE without proving the window is the action's.
This is the one correlation slice 1 does not derive.

**The authority diff.** It is supplied as a measured value, not computed here.
Computing it means the capability reach product (item 519) and the taint edge
walk, which are other items. What this gate owns is that the diff must be
present on every axis, that an absent axis refuses, and that a non-empty one
cannot be outweighed. PR #1241 makes the same split for the same reason.

---

## 8. Slice plan

**Slice 1, landed with this note.** `src/revl/shadow_promotion.py` and
`tests/test_shadow_promotion_518.py`. The plan, the observation, the
admission, the five preconditions, the accumulator, the three-shape verdict,
the restoration report and the renderer. 79 tests on a tree without item 517
and 83 with it.

**Slice 2, the scheduler.** Serve a declared fraction of an action in shadow:
the incumbent's answer is used, both worlds are recorded, and each pair is
appended to the window. This needs a runtime seam at the model boundary per
tier and is where the `revl canary` machinery is actually reused, since the
recorded worlds are `replay.Step` timelines. It also needs the action
correlation of section 7.

**Slice 3, the CLI and the lifecycle wiring.** `revl promote --plan` reporting
a verdict, and the adapter that hands this verdict to item 520's controller as
its `shadow` stage record. The controller already has the stage; what it does
not have is a producer.

---

## 9. Things stated here that are not verified

Listed rather than implied, in the discipline of
`docs/design/531-model-placement.md` section 10.

1. **The `agreement-ledger` and `placement-history` classes are this note's
   judgement, not a measurement.** Nothing in the tree yet writes either
   store, so what would actually be compensatable is unproven. The gate checks
   the declaration; the classes in the table in section 5 are a default a plan
   may disagree with.
2. **Item 517 is an open pull request (#1239), not main.** The integration
   tests were run against `origin/agent/1191-model-decision-evidence` by
   copying the module into the worktree, and they pass. If that PR's body
   members change before it merges, the record factory in the test file
   changes with it, and the four integration tests are what will say so.
3. **The `residence` axis is asserted to be an authority axis and is not
   checked against item 519's attenuation product**, which does not exist yet.
   The argument is item 512's: an `off_device` role's prompt leaves the
   device. Whether the product agrees that this is a capability widening is
   item 519's to settle.
4. **Nothing here has been run against a real shadow.** Every record in the
   test file is constructed. The gate is a pure function and is tested as one;
   what it would do on a real window depends on slice 2, which is not built.
5. **The threshold is a number the plan states and this note does not defend
   any particular value.** What it defends is that the threshold governs a
   ratio of COMPARISONS and that no metric enters the ratio.
