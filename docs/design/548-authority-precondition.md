# The authority diff is a precondition, not a stage

Roadmap item 543, issue #1222.

## What was already there

Two promotion paths in this tree implement the barrier the item asks for, and
both implement it well.

`tools/evolution_controller.py` (item 520, PR #1241, merged) walks
`trigger -> propose -> admit -> authority || shadow -> canary -> observe`.
`decide()` returns on the first unsatisfied precondition before any measured
record is read, so the verdict carries `measurements_read: false` and a
`NOT_REACHED` record per measured stage. The barrier is control flow, not a
weighting: the comparison the item forbids cannot be written because the
values are not in scope.

`src/revl/shadow_promotion.py` (item 518, PR #1250, open) walks
`plan -> route -> admit -> evidence -> policy -> authority -> state || accumulate`
with three independent proofs, two of them asserted by an AST walk over the
module's own source: no `Tally` is constructed left of the barrier, `admit()`
does not carry the SLO block into `Pair` at all, and the question and the
answer are private to `accumulate`.

Neither of those was the thing this change adds, and neither needed fixing.

## What was missing

**The rule was not general.** Two independent implementations that agree are
not a rule. Nothing forced a third promotion path to have the barrier, and
nothing told either implementation when the other changed.

**Nothing held the two together.** They name the same five axes differently:

| concept | `evolution_controller.py` | `shadow_promotion.py` |
| --- | --- | --- |
| capability reach | `capability` | `capabilities` |
| taint edges | `taint` | `origins` |
| resource budget | `budget` | `budget` |
| realm residence | `realm` | `residence` |
| retention deadline | `retention` | `retention` |

Two names in common out of five. An authority diff produced for one is
unreadable by the other. The direction that fails in is closed rather than
open, because each reads the other's three axes as unmeasured and refuses, so
nothing was broken. It is still not composition.

**A promotion path in the tree had no barrier at all.** `src/revl/mcp/canary.py`
(item 59) is a landed promotion path. `run_canary` recommended `promote`
whenever the two generations' recorded worlds matched:

    if divergence["diverged"]:  recommendation = "revert"
    else:                       recommendation = "promote"

That comparison walks the slice provider's own effects, emissions, awaits and
provisions. It is an account of what the slice exhibits, which is precisely
the kind of evidence the item says cannot buy an authority widening, because
capability reach and taint edges are static properties of the program and the
rare path that widens them is the one the slice does not take.

The admission gate that did run (`compile_under_authoring` with
`replacing=(provider,)`, and `placement.swap_admission` for the remainder)
answers a different question: structural compatibility with the running
composition and whether the new seams are crossable. It does not compare the
candidate's authority reach against the incumbent's.

Measured on `main` at `23455a00`, with a candidate whose `fn get` expression
body calls a pure extern:

| candidate | timeline | `audit --diff` | old recommendation |
| --- | --- | --- | --- |
| `canary_candidate_same.rvl` | identical | clean | `promote` |
| `canary_candidate_wider_reach.rvl` | identical | `+ host:TenantAStore:host_fmt` | `promote` |

An expression body produces no recorded step, so a pure extern reached from
one is invisible to the comparison and visible to the boundary surface. The
two candidates are behaviourally indistinguishable and one of them widens
capability reach.

## What this change is

### One statement of the barrier

`src/revl/promotion_barrier.py` holds the rule once.

`AUTHORITY_AXES` is the canonical axis set and `AXIS_ALIASES` maps every
spelling in the tree onto it, so the two vocabularies compose.

`authority_moved(diff)` is the shared fail-closed reading. An axis that is
absent has MOVED. It has not been measured, and reading an unmeasured axis as
an empty one is the fail-open shape and is the whole bug. A promotion that
proceeds because nobody looked is indistinguishable, from the outside, from a
promotion that proceeded because somebody looked and found nothing, and
keeping those two apart is what the barrier is for.

`PromotionPath` is what a promotion path declares about its own stage order,
and `check_path` refuses one whose authority stage is not strictly before
every measured stage. It refuses the item's own wrong shape: handed the
nine-stage lifecycle issue #1222 quotes, with the capability and reachability
diff listed "as one stage among those nine", it returns
`authority-after-measurement` and names the measured stages that were read
first. Handed the same nine stages with the authority diff moved in front of
the measured three, it admits them.

`check_axes` refuses a path that accounts for neither covering nor failing to
cover an axis. Measuring four and calling it five is the same fail-open shape
one level up.

### Something that forces a third path to have it

`REGISTRY` names every promotion path in the tree, and `discover(root)` sweeps
`src/revl` and `tools` for a module that renders a promotion verdict and is
not in it. The detector is syntactic and narrow: a module renders a promotion
when it contains a string constant whose value is exactly `promote`, in any
case. Over `origin/main` at 9649f21c it selects six modules: the four in
`REGISTRY` and the two in `SWEEP_EXEMPT`.

It errs wide, and that is the direction to err in. A module whose verdict
confers no authority is selected and has to be argued out by name. A module
that decided promotions without ever spelling the word would be missed; that
limit is stated rather than hidden, because the sweep is a ratchet against the
ordinary case of a path being added, not a proof that none can escape.

`SWEEP_EXEMPT` is a table of literal paths, each with the argument for why the
module is not a promotion path, and never a pattern, because a pattern is how
a real promotion path ends up exempt by accident. Two entries:

* `src/revl/promotion_barrier.py` itself, because the rule contains the
  comparison that looks for the token.
* `tools/evolution_progress.py`, whose `promote` renders the self-evolution
  loop's generation verdict over candidates that have already been scored. It
  deploys nothing, grants nothing, and no other module consumes its verdict.
  The candidate admission that does confer authority is
  `tools/evolution_controller.py`, whose `authority` precondition gates entry
  to the `observe` stage those same scorecards are the evidence for, and that
  module is registered.

A test asserts every exempt path still exists and would still be selected by
the detector, so an exemption cannot outlive the module it was written for,
and a second test asserts that the registry and the exemptions together are
exactly the set the detector selects.

### Something that holds the two implementations together

The registry entries are BOUND to the modules. `tools/evolution_controller.py`'s
own `PRECONDITIONS` and `MEASURED` tuples are read off the module by an AST
walk (`tools/` is not a package, and an AST read cannot execute anything) and
compared with what the registry declares. Moving `authority` after `shadow`
there reds `tests/test_promotion_barrier_543.py`.

`src/revl/peer_pool.py` is bound the same way: the registry says its `ceiling`
stage comes before its `evidence` stage, and the test reads `promote`'s body
and refuses a tree where `_ceiling_precondition` runs after `member.evidence`.

`src/revl/shadow_promotion.py` is not bound by comparison at all, because the
entry is DERIVED from the module. Issue #1338 is what settled that: the first
version of this registry hand-copied the module's stage tuples, the copy and
the module were each correct alone, and the two disagreed the moment both were
on `main`. `stages` is now the module's own `STAGES` and `covers` is its own
`AUTHORITY_AXES`, imported. What the entry still claims, and what can still
fail, is that the module walks a stage named `authority` and that every axis
spelling it uses is one the alias table knows.

### The barrier, on the path that had none

`run_canary` now runs `judge_authority` before the timelines are built:

    slice -> admit -> authority || divergence -> revert

The refusal returns before `divergence` exists, so a widened candidate's
comparison is never computed rather than being computed and outweighed. The
report carries no `divergence` key at all and says `divergenceRead: false`,
and the rendered refusal says the comparison was not read rather than leaving
a reader to infer it from an absence.

The diff is `src/revl/audit_diff.py`'s, the tree's existing authority-drift
gate over the G8 boundary surface, reused the way `promote_admission` already
reuses `placement.swap_admission`:

| axis | source |
| --- | --- |
| `capability` | a new `host:` crossing, a widened per-emission capability scope, or a host body appearing on a backend it was not on |
| `taint` | a new `emit:` crossing, a new boundary edge out |
| `realm` | a reach bound that loosened or moved |
| `budget` | a per-capability emission ceiling that widened, including a bounded count becoming `unbounded` |
| `retention` | a new declared position where a resource handle leaves revl's sight |

`audit_diff` reports the retention surface and compares no two of them, so the
retention row is an additions-only read of `resources.retention_surface` in
the direction every other bucket takes. All five axes are measured.

`judge_authority` reads the CANONICAL axis set rather than the keys the diff
happens to carry, so an axis that stopped being produced is read as moved. A
gate that walked only its own keys could never notice that it had stopped
measuring one.

## Failure direction

Fail-closed, stated once and applied everywhere.

* an absent axis has moved;
* an axis that is not a sequence of what widened has moved;
* a diff that is not a mapping moves every axis;
* a path that declares no authority stage is refused, not defaulted;
* a path that names an axis the module does not know is refused, not dropped;
* a generation whose audit cannot be built is refused, not compared.

## Non-vacuity

A test that passes is not evidence until it has been seen to fail.

Against a clone of `23455a00` carrying the new test file, the new fixture and
`src/revl/promotion_barrier.py` but NOT the canary change: **4 failed, 22
passed, 1 skipped**. The four are the canary legs.

Against the same tree with two mutants applied, `check_path` returning `None`
unconditionally and `authority_moved` reading an absent axis as empty: **10
failed, 16 passed, 1 skipped**. Every rule test is killed by one of them.

`test_the_two_candidates_are_behaviourally_indistinguishable` passes on every
tree above, and it is the control that matters. Both candidates' timelines are
identical to the baseline's step for step, so the only thing separating the
two verdicts is the authority diff, and a tree without the barrier cannot
separate them at all. `test_the_control_with_unchanged_reach_promotes_on_the_same_evidence`
is the other half: the refusal must not be satisfiable by refusing everything.

## No new guarantee code

The refusals here are lowercase links, the discipline `revl.deploy`,
`revl.model_evidence` and `revl.shadow-promotion` use. A rule refusing a
promotion path's shape is not the checker refusing a program, and registering
a G-code would pull in item 523's generated tier matrix, which wants a
reproducer under `examples/rejections/` or an `ACKNOWLEDGED` entry in
`tools/tier_guarantees.py` for every code.

## What this does not do

It does not rewrite either landed implementation. Both are correct and both
keep their own vocabulary; the alias table is what makes them readable to each
other, and the binding tests are what hold them to the rule.

It does not prove that no promotion path can escape the sweep. The detector is
a ratchet over the ordinary case, and its limit is written down above.

It does not change `audit_diff.evaluate`, whose `widened` verdict is the
`revl audit --diff` exit-code contract. The canary composes the buckets it
needs at its own call site instead.
