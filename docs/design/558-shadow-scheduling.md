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
