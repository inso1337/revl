# The progress term in the self-evolution reward (roadmap item 545, issue #1224)

**Status:** implemented. `tools/evolution_progress.py`,
`tests/test_evolution_progress.py`. Companion: `docs/design/534-evolution-reward.md`
and `tools/evolution_reward.py` (roadmap item 536, issue #1206), which own the
conservation half. This document owns the progress half and the generation rule
above it.

## 1. The defect

Roadmap item 536 enumerates eight reward components: compiles, tests pass, no new
false admits, no cross-tier divergence, byte stability of unrelated goldens,
formal status, scope discipline, documentation accuracy.

Every one of them is a preservation check. Nothing in the set rises when the
system gets better and everything in it falls when the system breaks, so the
reward they define is maximised by the empty diff. A loop trained against that
signal learns caution, not capability, and the cheapest policy that satisfies it
is to propose nothing.

The fix is not to weaken any of the eight. Each is correct and each is necessary.
The fix is that the reward has a second half and the repository already maintains
the numbers it needs.

## 2. The counters

Three counters, each read out of a checked-in artifact with `git show`, on both
sides of a change. None of them reads a candidate's prose, a commit message or a
self-report, which is item 536's first requirement.

| counter | value | universe | on `origin/main` at 9cf5e0ca |
|---|---|---|---|
| `census-allowance` | case ids in `tools/gate_reference_census_baseline.json` | `.rvl` documents in the census corpus | 9 over 557 |
| `native-chain-residual` | entries in `LOWER_GAP_DOCS` (`tests/test_selfhost_compile.py`) | that residual plus every `*_DOCS` corpus table beside it | 41 over 179 |
| `reach-gaps` | emitter entries in `tests/fixtures/oracle_construct_reach_ledger.json` | reference dispatches in `backends/<tier>/emit.py` | 238 over 686 |

Each was already monotone before this work, and already enforced by a tool rather
than by an operator's discipline:

* the census `--check` fails on a divergence that is not baselined **and** on a
  baseline entry that no longer diverges, so the allowance "can only shrink in a
  diff somebody reads";
* `tests/test_selfhost_compile.py` recomputes the residual set rather than
  sampling it, so a stale entry is a red and a document leaves the table the day
  `selfhost/lower.rvl` grows the surface it needed;
* the construct-reach ledger (issue #1203) reds both on a new unreached construct
  and on a listed entry that is no longer unreached, whose fix is to delete the
  line.

None of the three was a reward component before this change.

### 2.1 Why every counter is a pair

Each of these numbers falls when the work is done **and** falls when the measured
surface is deleted. A bare value would pay for the deletion, so the value is
always reported over a universe and a counter advances only when the value
strictly fell and the universe did not shrink.

The universe is chosen so that the only move that improves a counter is moving a
document, or a construct, across the line:

* deleting a divergent `.rvl` retires an allowance entry and drops the census
  corpus count;
* deleting a residual document drops `LOWER_GAP_DOCS` and the chain corpus by
  one each, because the universe is defined as their sum;
* deleting an unreached dispatch arm shortens the reach ledger and shortens the
  reference table it was drawn from.

The third is the one that needs the pair most. An arm nothing reaches emits
nothing, so deleting it breaks no golden and changes no test: `artifact-stability`
and `tests` both stay green. The universe is the only thing that sees it.

### 2.2 What each counter's reading does not cover

`reach-gaps` is read over the six emitter oracles only. The reach tool's
`lower_ir` and `compile` tables are built by private helpers that hardcode the
repository root, so they cannot be sized at `base` without a second checkout;
they are excluded from both halves of the counter rather than counted on one side
only. That leaves 7 of the tree's 245 named gaps outside this counter. Bringing
them in means giving those two tables a path-taking reader, which is a change to
`tools/oracle_construct_reach.py` and is deliberately not made here.

The census universe is the corpus walk, computed from `CORPUS_DIRS` and
`_SKIP_DIRS` read out of the census tool's own source by `ast`. It does not count
the tool's hand-written boundary programs or `ADMISSION_PROGRAMS`, which live in
the tool source rather than in the corpus directories. The census run reports 848
programs where this walk reports 557 for that reason. Deleting one of those
hand-written programs would lower the census's own count without lowering this
universe. It would also delete the only inputs in the tree written to sit on a
guarantee boundary, from a file whose covering test runs on every change to it.

## 3. How progress composes with a conjunction

`tools/evolution_reward.py` decided conjunction, not scalar, with a measured
argument: a candidate on a machine with no cargo scores seven of eight and clears
any threshold, so a scalar rewards the fail-open shape. It exports no top-level
number and a test asserts it.

A progress component that could be traded against a conservation component would
undo that. So progress enters at two levels, with two different quantifiers, and
at neither level is a number compared against a bar.

**At the candidate level it is a non-regression predicate.** The `progress`
component verifies exactly when every counter was read on both sides and none
regressed. That is a conservation check like the other eight, it is a ninth
conjunct in the same `all()`, and it cannot be traded, because a conjunction has
nothing to trade with. It carries the same four fields as every other verdict
(`component`, `verified`, `reason`, `evidence`), so registering it in that
module's `PROBES` table needs no adaptation.

**At the generation level it is an existential over the retained candidates.** A
generation is promoted when at least one candidate was retained (every component
verified, the ninth included) and that same candidate improved at least one
counter. A quantifier over a set, not a weighted sum over components.

The two levels are what keeps progress out of the trade. A candidate's
improvement is never summed with, subtracted from or compared against its
conservation verdicts: it is consulted only for candidates that already passed
every one of them. A candidate that improves three counters and breaks the census
is not retained, witnesses nothing, and the generation is no more promoted for
its existence than if it had never run.

### 3.1 What the alternatives would have permitted

**A scalar with weight on progress.** The census allowance on `origin/main` is
nine entries. A candidate that retires two of them and introduces one new
`false-admit` moves a progress counter down by two and fails exactly one
conservation component, so under any weighting with positive progress weight it
outscores a clean no-op. The census `--check` is a hard exit 1 on that candidate.
A reward satisfiable in a way the underlying gate is not is a defect, which is
the same argument item 536 made about the cargo case.

**Advancement as a tenth conjunct**, i.e. requiring every candidate to improve a
counter. Then a correct refactor, a documentation fix and a bug fix that closes
no counter are all unretainable, and the cheapest way to be retained is to pick
whichever counter is easiest to move rather than to do the work that matters. The
existential sits at the generation level precisely so that a generation of ten
honest non-advancing candidates and one real advance is promoted, and a
generation of eleven empty diffs is recorded as a generation that did not
advance.

## 4. Failure direction

Fail-closed, with no third value. A counter is in exactly one of four directions
and only two of them satisfy the ninth conjunct:

| direction | when |
|---|---|
| `improved` | universe did not shrink and value strictly fell |
| `unchanged` | universe did not shrink and value did not change |
| `regressed` | value rose, **or** the universe shrank, whatever the value did |
| `unreadable` | either side could not be read at all |

`unreadable` is not `unchanged`. A progress term that read "I could not measure
this" as "nothing got worse" would be the fail-open shape this repository has
already measured eleven times: a check that ran on every pull request and could
not fail. A missing artifact, a malformed one, an artifact absent at `base`, a
ledger with its entries deleted and a counter that raised are all `unreadable`,
which fails the component and can witness no promotion.

A universe that grew with an unmoved value is `unchanged`, not `improved`. Adding
corpus documents that all pass is good work, but it is not this counter moving,
and crediting it would make "add passing fixtures" the cheapest available advance.

The promotion rule is fail-closed in the same way. `retained` must be the literal
`true`; a string, a number or a truthy artefact of whoever serialised the record
is not a retention. Advancement is recomputed from the recorded counter
directions and never read off a boolean the producer wrote, because a rule that
verified a claim rather than a measurement is the failure item 536 named first.

## 5. Non-vacuity

`tests/test_evolution_progress.py` builds real git repositories in `tmp_path`
holding all three artifacts, and drives each counter through four cases: a tree
where it improved, a tree where it regressed, a tree where it is flat while
another counter moves, and a tree where the value fell only because the measured
surface was deleted. The empty-diff case is asserted directly: every counter
flat, the component verified, nothing advanced.

The rules were then mutated to check the tests see them:

| mutation | result |
|---|---|
| universe guard removed from the direction rule | 4 failed |
| `unreadable` folded into `unchanged` | 8 failed |
| `retained` coerced with `bool()` instead of `is True` | 1 failed |

The counters are also read against this repository itself, with each value
required to sit inside a strictly larger universe, so a counter that silently
answered zero over zero would satisfy every rule above and still red.

## 6. What this does not do

* It does not edit `tools/evolution_reward.py`. That module is on an open branch
  (pull request #1231) and not on `main`. Registering `progress_verdict` in its
  `PROBES` table is one line and is the step that makes the ninth conjunct part
  of retention in fact rather than in shape; the test here pins the verdict
  vocabulary so that line cannot silently stop fitting.
* It does not implement the four components item 536 left unimplemented
  (`compiles`, `tests`, `conformance`, `formal`). They still fail, which is the
  honest state.
* It does not close the 7 named gaps `reach-gaps` cannot size at `base`
  (see §2.2).
