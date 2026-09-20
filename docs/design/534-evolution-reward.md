# Design: the self-evolution reward, and when a trajectory is retained

Design-doc id 534 (531 went to another design doc that landed in the same
wave; the roadmap item of the same number is unrelated, which is the
established convention here). Roadmap item served: **536** (issue #1206).
Neighbours it must not duplicate: item 520 owns the evolution lifecycle and
the authority boundary, item 535 (issue #1205) owns the curriculum a
candidate draws tasks from, item 537 (issue #1207) owns the held-out scoring
set. This document owns the reward and the retention rule, and nothing else.

Sources studied, all at `52fb8ef3`: `tools/gate_reference_census.py` (`bucket`,
`compare`, `main`'s `--record` writer, `NEVER_BASELINED`, `TRACKED`),
`tools/gate_reference_census_baseline.json`, `tests/test_gate_reference_census.py`,
`tools/regen_goldens.py`, `tools/build_gate_crate.py`, `tools/build_gate_wasm.py`,
`tools/docgen.py`, `tools/check_roadmap_claims.py`, `tools/affected_tests.py`,
`docs/v2.0-roadmap.md` items 520/535/536/537, and issue #1206.

## 0. The decision in one paragraph

The reward is a **conjunction, not a scalar**, and retention is that same
conjunction. Eight components each answer `verified` or `failed`, every answer is
read off an artifact the repository owns, and a trajectory is retained only when
all eight say `verified`. No weight, no threshold, no partial credit, and no
number exported anywhere a threshold could later be attached to it. The scorer is
`tools/evolution_reward.py`; the candidate hands it a tree, a base ref and a
declared scope, and every other key in the candidate's record is dropped by name
and reported back under `prose_ignored`, so a reader of a scorecard can see that
the candidate's narrative was present and was not consulted.

## 1. The gap, measured

All eight components already had machinery at `52fb8ef3`. Seven of the eight name
a tool that exists and runs. One does not: **`tools/gate_verdict_parity.py`, which
item 536 and issue #1206 both cite for the conformance component, does not exist
anywhere in the tree.** `grep -rn gate_verdict_parity` over the repository returns
exactly one hit, the roadmap sentence itself. That is recorded here rather than
worked around, and section 5 says what it does to the component.

What was missing was not machinery. It was the fold and the rule: nothing ran the
eight and produced one verdict, and nothing stated when a trajectory is kept.

Measurement of the census at `52fb8ef3`, which the rest of this document quotes:
846 programs, 9 baselined divergences (`false-admit/T1` 6, `false-admit/TYPE` 3),
no `false-admission`, no mismatch bucket, no gate-fault, `--check` clean, 14
seconds on the `selfhost` engine.

## 2. How each component is read

Mechanically, from an artifact, never from prose. The rule the table obeys: a
component is `verified` only when a tool the repository owns ran to completion and
reported success.

| component | the artifact | how it is read | slice |
|---|---|---|---|
| `compiles` | the gate crate and the six-tier matrix | `cargo test --offline --lib` exit status, plus the per-tier emit suites | 2 |
| `tests` | the affected suite | `tools/affected_tests.py` selection, then that selection's pytest exit status | 2 |
| `no-new-false-admits` | the census buckets and `tools/gate_reference_census_baseline.json` | `gate_reference_census.py --check` exit status, AND a subset read of the baseline against `base` | **1** |
| `conformance` | verdict parity across tiers | blocked: the tool item 536 names does not exist | 3 |
| `artifact-stability` | every generated artifact | `tools/regen_goldens.py --all --check` exit status, which contains the crate and wasm digest checks | **1** |
| `formal` | the `formal/` ledger | the ledger's own coverage numbers, compared against `base` | 3 |
| `scope` | the changed-file set | `git diff --name-only <base>` plus untracked files, against the declared globs | **1** |
| `documentation` | the citation gate and the generated blocks | `tools/docgen.py --check` and `tools/check_roadmap_claims.py --check` exit status | **1** |

Two notes on the table.

`artifact-stability` is one invocation, not three. `tools/regen_goldens.py --all
--check` covers the six backend golden trees and both gate crates, so
`build_gate_crate.py --check` and `build_gate_wasm.py --check` are inside it
rather than beside it. It takes 15 seconds.

The word "unrelated" in the roadmap's phrasing ("byte stability of unrelated
goldens") is decided by `scope`, not by `artifact-stability`. A golden the
candidate legitimately regenerated in the same commit as its emitter change is in
sync, so it passes `artifact-stability`; whether it was allowed to move at all is
a question about the declared scope, and that is a different component. Splitting
it this way means neither component has to guess what the candidate intended.

## 3. Scalar or conjunction

**Conjunction.** Three reasons, in the order that decided it, and then what the
other choice would have permitted.

1. **The floor the repository enforces is not a magnitude.** `false-admission` is
   in `gate_reference_census.NEVER_BASELINED`: `--record` filters it out of what
   it writes, and `--check` fails on any member regardless of the baseline. Under
   any weighting with positive weight on the other seven components, a candidate
   can commit a false admission and still clear a threshold. A reward that is
   satisfiable in a way the underlying gate refuses is a defect, not a design
   choice, so the reward cannot be a weighted sum over a set that contains this
   component.
2. **The components are not commensurable.** "the census bucket sets are
   unchanged" and "every generated artifact is byte-identical" are equalities on
   sets and on bytes. There is no unit in which 0.3 of one trades against 0.7 of
   the other, so any weight vector is an invention rather than a measurement.
3. **A scalar rewards the fail-open shape.** This repository has measured six
   separate checks that ran on every PR and could not fail. A candidate on a
   machine with no cargo toolchain scores 7 of 8, which is 0.875, which is above
   every threshold anybody would pick. The conjunction answers "not retained",
   which is correct, because nobody verified that it compiles.

What the scalar would have permitted, concretely and in this repository:

* Re-recording the census baseline to absorb a new bypass costs at most one
  component and buys every other, so a weighted candidate that also adds forty
  passing tests outscores a clean no-op. Under the conjunction it is not
  retained, and section 4 closes the re-record path outright.
* A candidate that widens the emitted scope and regenerates the goldens to match
  loses `scope` and keeps `artifact-stability`, `tests` and `compiles`. Seven of
  eight again.
* A candidate whose formal ledger coverage dropped keeps everything else, and
  "no reduced formal coverage" is one of the seven promotion-bar entries item 536
  states negatively. A negative bar is a conjunct. It does not have a weight.

The retention half of item 536 already reads as a conjunction ("retained only
when every component verified"). The finding here is that the reward half has to
be the same object, because a reward and a retention rule that disagree give the
engine a gradient toward trajectories the retention rule throws away.

**No scalar is exported.** `Scorecard.as_dict()` carries a boolean, a blocker
list and per-component verdicts, and `tests/test_evolution_reward.py::
test_no_scalar_reward_is_exported` asserts that no number appears at the top
level. A count of verified components is trivially derivable by anyone who wants
it; what is refused is publishing one, because a published number gets
thresholded.

## 4. Retention, and the failure direction

**Retention rule, stated once.** A trajectory is retained if and only if every
component in `COMPONENTS` carries a `verified` verdict. It is implemented as
`all(by_name[name].verified for name in COMPONENTS)`, iterating the component
list rather than the verdict list, because `all()` over a short or empty list is
vacuously true in python and that is exactly how a scorer accidentally retains a
trajectory nobody scored. `test_a_missing_component_is_not_a_pass` holds it.

**Failure direction: fail-closed, with no third value.** There is no `unknown`
and no `skipped` verdict, because a third value is where a fail-open default
hides. Every one of these is `failed`:

* the tool is missing from the candidate's tree
* the tool exits non-zero
* the tool times out
* the subprocess cannot start, or the probe raises
* the component has no probe implemented yet
* the probe answers under a different component's name
* the candidate declared no scope, or changed no file

Each line has a test. A probe that raises fails its own component and does not
abort the run, so one broken gate cannot take the scorecard down and leave a
human to decide what the silence meant.

**The re-record path, which `--check` alone cannot close.** The census `--check`
fails in both directions, on a divergence not in the baseline and on a baseline
entry that no longer diverges, so a candidate can neither introduce a bypass nor
earn the component by deleting the work that retired one. What `--check` compares
against is the baseline FILE, so a candidate that runs `--record` first makes
`--check` pass. Item 536's own words are that the allowance "can only shrink in a
diff somebody reads". `probe_no_new_false_admits` reads that diff: every bucket's
id set in the candidate's baseline must be a subset of the same bucket at `base`,
and an added id fails the component. It also refuses a baseline that lists a
`NEVER_BASELINED` bucket at all, since `--record` filters those out and a
baseline carrying one was edited by hand.

On the real corpus a fabricated baseline id also trips `--check`, because it no
longer diverges. The case the extra read catches is the other one: an id that
`--record` legitimately wrote because it is a real new divergence. `--check` on a
freshly recorded baseline cannot distinguish that from the baseline it was told
to trust. The subset read can, because it looks at `base`.

`tests/test_evolution_reward.py` proves this against a real git repository with a
stub census tool that exits 0 throughout, so the only thing that can fail the
component is the grown allowance itself.

## 5. The conformance component, and why it fails today

Item 536 maps conformance and cross-tier divergence to
`tools/gate_verdict_parity.py`. That file does not exist. Three responses were
available and two of them are wrong:

* award the component, on the grounds that nothing failed. That is the fail-open
  shape in its purest form, a check that runs on every PR and cannot fail.
* drop the component from the list, which silently redefines the reward as seven
  components and makes the conjunction weaker than the item states.
* fail the component, name the missing tool in the reason, and assert in a test
  that the tool is still absent, so the test reds on the day it arrives and the
  probe has to be written.

The third is what is implemented.
`test_the_conformance_component_names_the_tool_that_does_not_exist` is the
tripwire. The nearest existing machinery is the census's own two-engine
agreement (`tests/test_gate_crate_admit.py::test_the_two_engines_agree`) and
`tools/conformance.py`; slice 3 decides which of those is the parity read item
536 meant, rather than guessing here.

## 6. Interfaces assumed from the sibling lanes

Neither sibling branch existed when this was written (both worktrees sat at
`52fb8ef3` with no commits), so these are assumptions, stated so they can be
corrected cheaply rather than discovered.

**From item 535, the curriculum (issue #1205).** The reward consumes nothing from
the curriculum directly. A task is what produced the tree; the scorer reads the
tree. The one coupling is the **declared scope**: the reward requires a candidate
to have declared one, and the natural producer of that declaration is the
curriculum task. The assumption is that a task carries a path-glob scope, and the
assumption is cheap to break: if the curriculum spells scope differently, only
`Candidate.scope` and `_glob_to_regex` change.

**From item 537, the held-out scoring set (issue #1207).** The reward is
indifferent to whether a tree came from a training task or a held-out one. It
scores a tree. The assumption is that the held-out set produces candidate trees
of the same shape, so `tools/evolution_reward.py --candidate <record>` is the
same entry point for both, and the held-out lane's job is to guarantee the
candidate never read the set, which is a property of the task, not of the score.

**Shared vocabulary.** `Candidate` holds `tree`, `base`, `scope`. `Verdict` holds
`component`, `verified`, `reason`, `evidence`. `Scorecard` holds `retained`,
`blockers`, `components`, `prose_ignored`. If a sibling lane needs a different
name, this document is the one that moves, because the reward is downstream of
both.

## 7. Non-goals

* **No authority.** The scorer computes a verdict. It does not merge, admit,
  activate or deploy anything, which is item 520's rule for evolution mode and is
  not relitigated here.
* **No model, no engine.** The reward does not know what produced the tree.
  Item 536 records Ornith-1.5 as the candidate this material was read against,
  and states it is a candidate and not a dependency. Nothing here names a model.
* **No new gate.** Every component reads an existing tool. Where no tool exists,
  the component fails rather than a new gate being invented inside the scorer.
* **No prose channel, ever.** Adding a component that reads the candidate's
  explanation is not a future slice. It is the thing the item exists to forbid.
* **Not a replacement for CI.** A retained trajectory is a training example, not
  a merge decision.

## 8. Slice plan

Each slice lands with its own oracle and its own fixture, in the shape
`docs/design/457-selfhost-type-layer.md` uses, so no slice depends on a later one
to be checkable.

**Slice 1 (landed).** `no-new-false-admits`, `artifact-stability`, `scope`,
`documentation`, plus the retention rule, the record whitelist and every
fail-closed path.
Oracle: `tests/test_evolution_reward.py`, 39 tests.
Fixtures: `tiny_repo`, a real git repository with a real base commit, so the two
diff-reading probes run against real git rather than a mock of a diff; and the
repository itself as a real candidate for `documentation` and
`artifact-stability`.
Non-vacuity: `test_documentation_fails_on_a_genuinely_stale_doc_inventory`
creates one top-level `docs/*.md` file, which makes `docs/DOC-STATUS.md` stale,
and asserts the `documentation` component fails on the real tree.
`test_artifact_stability_is_the_control_and_passes_either_way` is the control:
the same fault leaves every generated artifact byte-identical, so the scorer
locates the fault rather than reddening globally.

**Slice 2: `compiles` and `tests`.** The two expensive components, and the two
whose absence makes today's scorecard unretainable.
Oracle: a candidate tree with a deliberately broken emitter must fail `compiles`
and leave `documentation` verified, which is the same control shape as slice 1.
Fixture: the affected-selector output has to be read rather than assumed, because
`tools/affected_tests.py` falls back to FULL on an unmapped path, and a `tests`
component that silently ran zero tests is the fail-open shape again. The probe
must assert a non-zero collected count, not merely a zero exit status.

**Slice 3: `conformance` and `formal`.** Conformance first decides what the
parity read is, since section 5's tool does not exist. Formal reads the ledger's
coverage against `base` and fails on a reduction, which is one of item 536's
seven negative bar entries.
Oracle: a fixture whose formal coverage drops must fail `formal` alone.

**Slice 4: the negative promotion bar.** Item 536 states seven entries that green
tests alone must not carry: no new false admits, no widened capability reach at
the G8 boundary, no weakened refusal, no reduced formal coverage, no unexplained
golden change, no unbounded resource path, no hidden host fallback. Slices 1 to 3
cover the first, the fourth and the fifth. The other three need their own reads
and are not folded into an existing component, because a component that fails for
two unrelated reasons cannot be acted on.

## 9. What this design does not verify

* It does not check that a component's tool is itself correct. The reward is only
  as strong as the gates it reads, and it says which gate it read in every
  verdict's `evidence` so that dependence is visible.
* `compiles` and `tests` have no probe, so nothing here has been demonstrated
  against a candidate that fails to build. Slice 2 owns that.
* The census subset read compares the candidate's baseline against `base`. It
  cannot see a baseline that was already wrong at `base`; that is what
  `tests/test_gate_reference_census.py`'s named cap is for, and the two are
  complementary rather than redundant.
* Nothing here has been run against a trajectory produced by a model. The
  scorer's input is a tree, and every test supplies one directly.
