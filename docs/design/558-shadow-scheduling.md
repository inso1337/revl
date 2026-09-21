# 558: The shadow scheduler, the action correlation, and the recorded world

Roadmap: item 518 (issue #1192), from the 2026-09-19 external review. Slice 2
of [540-shadow-promotion.md](540-shadow-promotion.md), whose slice 1 landed as
PR #1250 (`dfecba2a`). Slice 3 is still open and section 9 says what is left
of it.

Design number: 558, assigned rather than claimed. 540 to 554 are taken on main
or on an open branch, and note 540 records what happens when three lanes read
the same number as free at the same moment.

Reconciles with: item 496 and [../verified-canary.md](../verified-canary.md)
(the replay comparison and its attribution, called rather than restated), item
517 and [536-model-decision-evidence.md](536-model-decision-evidence.md) (the
sealed records, and the closed body that is the reason the correlation has to
come from somewhere else), item 512 and
[531-model-placement.md](531-model-placement.md) (the route table), item 522
and PR #1287 (the residue vocabulary a revert reports in), item 543 (issue
#1222, the authority diff as a precondition).

---

## 0. The decision in one paragraph

Slice 1 accumulates agreement over item 517's records and turns it into a
`PROMOTE` / `REVERT` / `REFUSE` verdict. Nothing schedules the shadow, so "a
declared fraction of a model action" was a phrase in the item and not a value
anywhere; and because item 517's record body is a closed vocabulary that names
no action and no realm, nothing could say which action class a window was
evidence for. This note adds the scheduler and it adds exactly two facts to
each observation, both of which the scheduler already held when it scheduled
the shadow: the action and the realm. It also connects the comparison item 518
was always supposed to run and did not: the two RECORDED WORLDS, compared by
`revl.mcp.canary.compare_timelines`, which is item 496's own function with
item 496's own key. The verdict stays slice 1's. There is no second verdict
type, no fourth decision word, and no number in the gate that a measurement
could move.

---

## 1. The two gaps, measured

Both on `dfecba2a`, both run rather than read.

### 1.1. A window of one action's evidence promotes another action

`docs/design/540-shadow-promotion.md` section 7 states it plainly: item 517's
record carries `component` and `step_index` and does NOT carry the action, so
"a plan that names the wrong action is checked against the route table but not
against the records". Run, over a component with two routed actions:

```
plan: Classifier.classify, threshold 0.95, min_observations 4
window: 20 agreeing pairs recorded for Classifier.summarize
verdict: PROMOTE, agreement 1.0
```

This is not a defect in slice 1. The correlation is not derivable from a
sealed record whose body is closed, and `revl.model_evidence.BODY_MEMBERS`
contains neither `action` nor `realm`. It is derivable from the thing that
scheduled the shadow, which did not exist.

### 1.2. The recorded-world comparison was not connected

Item 518's own text says it "deliberately reuses item 496's construction". What
slice 1 reused was the RATIONALE. The comparison it runs is over two members of
a record: `chosen_digest` and `outcome`. Item 496 is the measurement that says
that is not enough:

> a generation that moved a write onto the read path was certified equivalent
> and promoted

Two generations can name the same completion and record different worlds, and
the direction of the miss is fail-open, because `diverged: false` is the branch
that promotes. Run, at the shadow layer, on twenty pairs whose records agree on
every member a record comparison reads and whose recorded worlds differ by one
relocated step:

| window | verdict | agreement |
| --- | --- | --- |
| records only | PROMOTE | 1.00 |
| records + the two recorded worlds | REVERT | 0.95 |

---

## 2. The scheduler

`src/revl/shadow_routing.py`. `serve(route, crossings, incumbent=, candidate=)`
walks a sequence of `(component, step_index)` crossing keys and produces a
`ShadowLedger`.

Three properties, each measured in `tests/test_shadow_routing_518.py` rather
than asserted:

1. **`candidate` is called only on the crossings the share selects.** The
   ledger's `candidate_calls` is counted at the call site, not derived from the
   share, and the test compares the calls against the selection over 200
   crossings. That is what "a declared fraction" means operationally: the
   successor is not consulted on the other crossings at all.
2. **The answer served is the incumbent's on every crossing while the route is
   not live.** Measured over a window in which the candidate's answers really
   do differ, so it is not true by the candidate being a copy. A window in
   which a not-live candidate answered refuses (`served-candidate`); that is
   not a shadow, it is a cutover that already happened.
3. **Every entry carries this route's action and realm.**

### 2.1. The share is a rational, not a float

`Share(1, 20)`, not `0.05`. A share is a statement about counting and "1 in
20" has an exact meaning a float rounds. `0/d` is the off switch and `d/d` is
the exhaustive run; both are legal and the tests use both.

Selection is `selects(route, crossing)`, a keyed SHA-256 digest of
`(salt, component, step_index)` modulo the denominator. It reads no counter,
so it is a PURE FUNCTION of the crossing: two runs over the same crossings in a
different order shadow the same set, and a replay of a recorded run shadows
what the run shadowed. Measured over 1000 crossings:

```
share 1/20:   52 / 1000
share 1/4:   250 / 1000
share 1/2:   517 / 1000
share 1/1:  1000 / 1000
```

This is a scheduling draw and not a security boundary. What it has to be is the
same draw on a replay.

### 2.2. The share is not a verdict input

The share decides which crossings are OBSERVED. It is not evidence about any of
them, and it does not reach the gate: `ShadowPlan` has no member for a share,
`Pair` has none, `Promotion` has none, and a test asserts all three plus the
absence of the word from the serialised verdict. Two shares over the same forty
crossings reach the same decision on the same stated threshold, with different
sample sizes; observing more does not buy a promotion, because agreement is a
ratio and not a count.

---

## 3. The stamp, and why it is not a second log

An `Entry` is one observation plus `(component, action, realm)`. That is the
whole correlation.

It is not a member added to a sealed record: item 517's body is closed and
`verify` refuses an unknown member (`evidence-vocabulary`), which is correct
and stays correct. It is not a parallel log joined on a second key: the
crossing key is still item 517's own `(component, step_index)`, and the test
asserts that against `model_evidence.crossing_key`. It is what the scheduler
knew at the moment it decided to shadow that crossing, written down.

`decide(route, plan, entries)` reads the stamp and refuses:

| shape | refusal |
| --- | --- |
| an observation with no stamp at all | `action-unscheduled` |
| an entry stamped for another action | `action-mismatched` |
| an entry stamped for another realm | `realm-mismatched` |
| a not-live candidate that answered | `served-candidate` |
| a route that disagrees with the plan | `route-mismatched` |
| a share that is not a rational `n/d` | `share-malformed` |
| a route missing a name, or shadowing a role with itself | `route-malformed` |

Each is reported through `shadow_promotion.refused`, so the caller gets the
same `Promotion`, with `measurements_read` false and every stage of the gate
recorded as `not reached`. A test asserts that last part on a real refusal:
there is no path on which a schedule refusal has walked a precondition.

### 3.1. What the stamp is trusted for

The scheduler is trusted to name the action it was scheduling. That is a
smaller trust than it looks, because the alternative on offer today is no
correlation at all, but it is a trust and it is stated: nothing verifies the
stamp against the composition, because `step_index` has no static producer in
the tree. Deriving it needs either a member added to item 517's body or a
derivation from the WAL's surrounding `effect` records, which is section 9.

---

## 4. The comparison, which is item 496's

An observation may carry the two RECORDED WORLDS as `replay.Timeline` objects
in `revl.mcp.canary`'s own format. When both are present,
`shadow_promotion.accumulate` compares them with `canary.compare_timelines`:
called, not reimplemented, so the key stays item 496's
`(kind, label, undo, compensate, slot)` and moves when that one moves. A test
asserts the function identity rather than the behaviour, because the behaviour
is the canary's to define.

A pair AGREES when the two agreement members match AND, when both worlds were
recorded, the worlds compare identical step for step. The two legs are not a
refinement of one another: the first says what the decision named, the second
says what the answer then did.

Three details that are load-bearing:

* **A missing comparator DIVERGES.** If `revl.mcp.canary` cannot be imported,
  a pair carrying two worlds is counted as diverged, not as agreeing. "The
  comparison did not run" and "the two worlds matched" are the two branches
  item 496 measured the cost of confusing.
* **The rendered sides carry the discriminating field.** A relocated step has
  the same `kind` and the same `label` on both sides. A report that printed
  only those two would print both sides of item 496's own finding identically,
  so `_step_name` appends the field `compare_timelines` discriminated on:
  `effect store.insert(k, v) [slot=0]` against `[slot=1]`.
* **One world is not a comparison.** A pair carrying fewer than two worlds is
  compared on its records alone, and the ledger reports `worlds_recorded` so a
  reader is never guessing which legs ran.

### 4.1. The attribution is `(component, realm)`

`Divergence` carries the crossing, the realm the scheduler stamped, the step
index within the recorded world and the field that discriminated.
`Divergence.attribution` is the `(component, realm)` pair `revl canary`
reports. The realm is on the PUBLIC side of `Pair` because it says where the
shadow was served, not what it answered.

---

## 5. Promotion is still an admission decision

The sharpest case in the test file, because it is the one a numeric gate gets
wrong:

```
19 of 20 pairs agree; threshold 0.95; accumulated agreement EXACTLY 0.95
=> REVERT, divergence-attributed
```

The ratio meets the stated threshold. A gate that compared a number against a
number promotes here. The attributed divergence reverts it instead, and the
report carries the ratio beside the attribution rather than in place of it.
That is item 496's rationale as behaviour rather than as prose: the output when
a shadow disagrees is a name.

The metric is absent from all of it. Every observation in that run carried a
perfect SLO block, `slo_supplied` is 20 and `slo_reads` is 0, and the counter
is taken from the observations rather than asserted by the module about itself.

---

## 6. What a revert reports

Unchanged from slice 1, which already implements item 546's four words, and
compatible with item 522's five residue states (PR #1287,
`docs/design/553-ui-transaction-phases.md`): `restored` and `compensated` are
two separate lists, the aggregate is the weakest part, and no sixth word is
introduced here. A revert through the scheduler goes through slice 1's
`_restoration` verbatim. `restored: route-arm`; `compensated only:
agreement-ledger via supersede-window`, `placement-history via
replay-placement-log`. The string "rolled back" appears in neither, and the
test asserts its absence in the scheduler's own renderer too.

This branch does not read PR #1287's code and does not depend on it.

---

## 7. Failure direction

Fail-closed throughout, in the direction the repository has measured before:

* absent evidence refuses; a share of `0/1` over twenty crossings produces
  `evidence-missing`, not a promotion on an empty window;
* an unstamped observation refuses rather than being admitted on the strength
  of its stamped neighbours;
* an unresolvable world comparator diverges rather than agreeing;
* a malformed route produces an EMPTY ledger carrying the refusal rather than
  raising, because the caller is a runtime seam and the failure that is needed
  there stops the shadow, not the incumbent's answer.

### 7.1. No new guarantee code

Every refusal here is a named LINK, the discipline `revl.shadow_promotion`,
`revl.model_evidence` and `revl.deploy` use. A scheduler refusing a window is
not the checker refusing a program. Nothing registers a `G-` code, so item
523's generated tier matrix is owed neither a reproducer under
`examples/rejections/` nor an `ACKNOWLEDGED` entry in
`tools/tier_guarantees.py`. A test asserts no link starts with `G-`.

---

## 8. Non-vacuity

The promote and the revert both happen on the SAME twenty pairs, and the only
thing that differs is whether the candidate is the one answering:

| case | decision | link | paired | agreement | slo supplied | slo read |
| --- | --- | --- | --- | --- | --- | --- |
| agrees on every recorded step | PROMOTE | - | 20 | 1.00 | 20 | 0 |
| live, one relocated step | REVERT | `divergence-attributed` | 20 | 0.95 | 20 | 0 |
| the same window, worlds stripped | PROMOTE | - | 20 | 1.00 | 20 | 0 |
| no accumulated evidence | REFUSE | `evidence-missing` | - | - | 0 | 0 |
| share `0/1` over 20 crossings | REFUSE | `evidence-missing` | - | - | 0 | 0 |
| `summarize`'s window, `classify` plan, unstamped | PROMOTE | - | 20 | 1.00 | 0 | 0 |
| `summarize`'s window, `classify` plan, stamped | REFUSE | `action-mismatched` | - | - | 0 | 0 |
| `summarize`'s window, `summarize` plan (control) | PROMOTE | - | 20 | 1.00 | 0 | 0 |

Rows 1 and 2 are the promote and the revert. Rows 2 and 3 are the
recorded-world leg's differential. Rows 6, 7 and 8 are the action
correlation's, and row 8 is the control that stops row 7 from being a refusal
of everything.

Oracle: `tests/test_shadow_routing_518.py`, 48 tests.
`tests/test_shadow_promotion_518.py` (83) and `tests/test_canary.py` (15) are
unchanged and green, which is the statement that the additions to
`shadow_promotion` are additive.

---

## 9. Things stated here that are not verified

1. **No tier calls `serve`.** The two producers are a seam and nothing in the
   emitted runtimes is wired to it. A real shadow is a runtime concern per
   tier and this note does not claim one was run. Every record in the test
   file is constructed, and the two integration tests against
   `revl.model_evidence`'s real sealer are what stop the shape from drifting.
2. **There is no CLI.** `revl promote --plan` is slice 3 of note 540, together
   with the adapter that hands this verdict to item 520's controller as its
   `shadow` stage record. Neither is here.
3. **The stamp is trusted, not derived.** Section 3.1. Nothing checks the
   scheduler's claim about which action a crossing belonged to against the
   composition, because `step_index` has no static producer.
4. **The recorded worlds are supplied, not built.** This note connects the
   comparison and defines where the two timelines go. It does not record them:
   building a per-crossing timeline from a live model action needs the same
   runtime seam as (1). What is measured is that when they ARE supplied the
   comparison runs, attributes, and changes the verdict.
5. **The realm is declared, not checked against a composition.**
   `revl.mcp.canary` resolves a slice realm through `placement.slice_partition`
   and refuses a realm the composition does not have. This module does not, and
   a scheduler naming a realm that does not exist is accepted. Wiring the two
   is worth doing and is not done; it belongs with (1), because that is where a
   composition is in hand.
6. **The share's statistical behaviour is not a claim.** `1/20` selected 52 of
   1000 and `1/2` selected 517 of 1000. The digest is not asserted to be
   uniform; what is asserted is that the selection is deterministic,
   order-independent, salt-sensitive, and exact at both ends.

---

## 10. Slice 3: the seam, and the four gaps section 9 left

Slice 2 landed as PR #1297. Section 9 listed four things it did not do, and
all four had one cause: nothing was wired to a running composition. Slice 3 is
that wiring. `src/revl/shadow_runtime.py`, oracle
`tests/test_shadow_runtime_518.py` (27 tests).

### 10.1. The tier, and the five that are not wired

**python only.** `backends/python/runtime.py` grew one hook,
`revl_attach_shadow(observe)`, consulted inside `validate_retry`, the single
seam every model completion in that tier crosses (item 121 section 2.1),
after the response has validated:

```python
_revl_record_model_call(started, attempt + 1, budget + 1, value)
revl_note_validated_completion(site)
_revl_serve_shadow(_revl_recorded_crossing.get(), value)
return validated
```

`revl.shadow_runtime.TierShadow` wires that hook to a
`revl.shadow_routing.Scheduler`. Three properties of the placement, each a run
rather than an assertion:

* **The hook cannot change the answer.** Its return is discarded and
  `validate_retry` returns the object `validate_response` produced. Measured
  with a successor answering a different model name on every crossing.
* **The successor's own completion does not re-enter the seam.** It crosses
  the same `validate_retry`, so the tier holds a re-entrancy register for the
  duration of the hook. Measured as a count: 21 crossings observed, not 42.
* **An observer fault stops the shadow, not the run.** The tier catches it,
  detaches, and keeps it in `revl_shadow_faults()`; `TierShadow.ledger()`
  turns a non-empty fault list into a `shadow-faulted` refusal, because the
  crossings that did accumulate are the ones before the fault and that is a
  biased sample of the ones offered.

**ts, rust, java, wasm and cordis(C) are NOT wired.** `WIRED_TIERS` and
`UNWIRED_TIERS` say so in the module, and a test asserts both. No emitter
changed on this branch, so there is nothing an emitter could have dropped: the
hook is in the runtime shim an emitted component imports, not in emitted
output. A tier is wired by growing the same hook at its own completion seam.

`revl.shadow_routing.serve` is now a loop over a new `Scheduler`, which is the
same selection, stamping and counting path taken one crossing at a time. There
is deliberately not a second implementation: a running composition does not
hold a list of crossings, and a batch form and a live form that disagreed
about which crossings were shadowed would be two answers to the item's own
question.

### 10.2. What "runs", precisely

There is no cordis activation. `import cordis` resolves in one CI job and in
no local checkout. What runs is the tier's RECORDER (`backends/python/
replay.py`) and its COMPLETION SEAM (`backends/python/runtime.py`), driven
over the composition's declared steps, which is the same nesting
`tests/test_250_model_decision_wal.py` calls "the way the recorder wires a
live run": `Timeline.record_emission` inside `validate_retry`'s `make_call`.
Every crossing key in the oracle is minted by that recorder. None is written
down by the test.

## 11. The recorded worlds, built

`world_for(ir, component)` is `canary.slice_timeline`, called. A shadow over a
live composition holds the two GENERATIONS' IRs, so it builds the two
timelines instead of being handed them. The oracle compiles three generations
of one component with `compile_source`:

| generation | how it differs | recorded world |
| --- | --- | --- |
| incumbent | (the baseline) | (the baseline) |
| sibling | an unrelated component added | identical step for step |
| relocated | every `summarize` crossing moved into `classify` | same kinds, same labels, same order, different `slot` |

The relocated one is item 496's own finding in `.rvl`: the flat step list is
unchanged and the entry point is not.

## 12. The stamp, derived

Section 3.1 said `step_index` has no static producer in the tree. It has one,
and it is item 496's walker. `canary.slice_timeline` appends each step through
`replay.Timeline._add`, which assigns `Step.index = len(self.steps)`, the
same assignment `Timeline.record_emission` makes at run time. So the static
walk's emission indices ARE the crossing keys a run of that component mints,
and each step's `detail["origin"]` names the provide method it is reached
from, which is the action.

`crossing_actions(ir, component)` is that map. It is used twice:

* **At the seam**, by `Resolution.owns`: a component with two routed actions
  offers both actions' crossings to one completion seam, and a schedule that
  stamped all of them with its own action would manufacture section 1.1's
  miscorrelation rather than prevent it. A crossing the composition attributes
  elsewhere is never observed, never stamped, never counted. This was found by
  running it: the first live drive of the oracle refused with `stamp-forged`
  on the `classify` crossing, which is the derivation catching the wiring.
* **After it**, by `check_stamps`: slice 2's `_check_entries` compares the
  stamp against the PLAN, and both sides of that comparison are the
  scheduler's own word. This compares it against the composition.

The measurement that the derivation is about a run and not only about a file
is `test_the_recorder_mints_the_indices_the_composition_declares`: the live
recorder's emission keys equal the statically derived ones, on a drive of the
composition.

What remains an assumption, stated: the activation runs each declared step
once, in source order, which is the premise `revl canary` already makes when
it calls the static walk "the recorded world". A run that departs from it
produces a crossing the composition declares at no index, and `check_stamps`
REFUSES it (`stamp-underived`) rather than guessing an action for it.

## 13. The realm, resolved

Two lines, and they are `revl canary`'s:

```python
part = slice_partition(ir, route.realm)
if not part["members"]:
    ... REALM_UNKNOWN, naming slice_realms(ir)
```

The same call `canary.select_slice` makes, reading the same empty-`members`
signal, and refusing with the same shape of message. A component that is not a
member of the designated realm refuses too (`realm-unplaced`), because the
realm is half of the `(component, realm)` attribution and a component
attributed to a realm it is not isolated into attributes to nothing.

## 14. Non-vacuity, live

Every row is a drive of the compiled composition through the tier's recorder
and completion seam. `offered` and `shadowed` are the ledger's counts and
`cand calls` is counted in the candidate producer itself, not derived from
the share.

| case | decision | link | paired | agreement | offered | shadowed | cand calls | worlds | slo sup | slo read |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| shadow, sibling generation | PROMOTE | - | 20 | 1.00 | 20 | 20 | 20 | 20 | 0 | 0 |
| live, relocated step | REVERT | `divergence-attributed` | 20 | 0.00 | 20 | 20 | 20 | 20 | 0 | 0 |
| shadow, relocated step | REFUSE | `agreement-below-threshold` | 20 | 0.00 | 20 | 20 | 20 | 20 | 0 | 0 |
| live, one differing answer | REVERT | `divergence-attributed` | 20 | 0.95 | 20 | 20 | 20 | 20 | 20 | 0 |
| share `1/4` | PROMOTE | - | 7 | 1.00 | 20 | 7 | 7 | 7 | 0 | 0 |
| share `0/1` | REFUSE | `evidence-missing` | - | - | 20 | 0 | 0 | 0 | 0 | 0 |

Rows 1 and 2 are the promote and the revert, on the same twenty crossings of
the same composition, differing only in which generation the candidate's world
is built from. Rows 1 and 3 are the recorded-world leg's differential without
the `live` rule in the way. Row 4 is section 5's sharpest case carried onto a
live run: nineteen of twenty agree, the plan states 0.95, the accumulated
agreement is EXACTLY 0.95, and the attributed divergence reverts it anyway
while every observation carried a perfect SLO block that the gate read zero
times.

The two attributions in full, as the verdict prints them:

```
Classifier in realm `tenant_a` step 2: the recorded worlds differ at replay
step 2 (emission model.complete('p0') [slot=1] -> emission
model.complete('p0') [slot=0])

Classifier in realm `tenant_a` step 5: chosen_digest '9320fca4...' ->
'7e54a2aa...'
```

The first is invisible to a record comparison: both records name the same
completion.

## 15. One thing slice 2 derives that this measures

Slice 2 derives the SERVED SIDE from the route: `live` means the candidate is
answering, so an entry on a live route records `candidate`. An offline caller
has nothing better. A seam does: this hook's return is discarded, so the
incumbent answered, and `TierShadow` passes `INCUMBENT` to `Scheduler.offer`
as a fact. `Scheduler.offer` grew a `served=` parameter for it and keeps slice
2's derivation as the default, so `serve` is unchanged.

A route served through this seam is a shadow whatever its `live` flag says.
`live` selects the gate's rule, which is that the first attributed divergence
reverts, and not who answered. A route whose successor really answers is a cutover, and
this seam does not perform one.

## 16. Still not verified, after slice 3

1. **Five tiers are unwired.** Section 10.1. The claim here is about python.
2. **No cordis activation ran.** Section 10.2. The recorder and the completion
   seam ran; the runtime that drives an emitted component did not, because
   `import cordis` does not resolve outside one CI job.
3. **There is still no CLI.** `revl promote --plan` and the adapter that hands
   this verdict to item 520's controller as its `shadow` stage record are
   both untouched.
4. **One activation, distinct crossings.** `shadow_promotion`'s
   `_precondition_evidence` refuses a window that repeats a crossing, and a
   second activation of one component mints the same keys again. So a window
   is one activation's crossings, and accumulating across activations needs an
   activation identity the crossing key does not carry. Not designed here.
5. **The static walk's premise.** Section 12. A run that does not execute each
   declared step once in source order refuses rather than mis-attributing,
   which is the right direction and is not the same as handling it.
6. **The share's statistical behaviour is still not a claim.** `1/4` selected
   7 of 20 above. What is asserted is that the selection is deterministic,
   order-independent and exact at both ends.
