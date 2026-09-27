# The progress term in the self-evolution reward (roadmap item 545, issue #1224)

**Status:** implemented. `tools/evolution_progress.py`,
`tests/test_evolution_progress.py`, registered as the `progress` component of
`tools/evolution_reward.py`. Companion: `docs/design/534-evolution-reward.md`
(roadmap item 536, issue #1206), which owns the conservation half. This document
owns the progress half.

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
The fix is a component that rises, read off numbers the repository already
maintains.

## 2. The decision: retention requires an improvement

The `progress` component verifies only when every counter was read on both sides,
none regressed, **and at least one strictly improved**. It is one more conjunct in
the same `all()`, so:

* the empty diff fails `progress` and is not retained;
* a candidate that improves one counter and regresses another fails `progress`;
* a candidate that improves a counter and fails any other component is not
  retained, and that component is the blocker. Progress is added to
  preservation, never traded against it.

This reverses the first slice of this document, which made `progress` a
non-regression check and put the improvement requirement one level up, as an
existential over a generation (`promote`). The product owner decided on issue
#1224 that retention itself must require an improvement. The first slice's
argument against doing so (a correct refactor becomes unretainable, and the
easiest counter becomes the target) is real, and section 6 prices it rather than
waving it away. The argument for: a reward whose per-candidate maximum is still
the empty diff still teaches the per-candidate policy "propose nothing", whatever
a generation rule says afterwards.

`promote` stays. Every candidate the scorer retains has now improved a counter by
construction, so over real scorecards it reduces to "some candidate was
retained". It still re-reads the serialised counter directions rather than
trusting `retained`, so a scorecard from an older scorer, or one assembled by
hand, cannot promote a generation on the strength of a flag. The scorecard
carries the counter ledger under `progress` for exactly that read.

## 3. The counters

Three counters, each read out of a checked-in artifact on both sides of a
change. None of them reads a candidate's prose, a commit message or a
self-report, which is item 536's first requirement. Each is a pair of SETS: the
failing members over the measured surface.

| counter | failing members | surface | at `f1443de3` |
|---|---|---|---|
| `census-allowance` | case ids in `tools/gate_reference_census_baseline.json` | `.rvl` documents in the census corpus | 0 over 588 |
| `native-chain-residual` | `tier:path` pairs in `LOWER_GAP_DOCS` (`tests/test_selfhost_compile.py`) | `tier:path` pairs in every `tests/test_selfhost_emit_<tier>.py` `CORPUS` | 11 over 261 |
| `reach-gaps` | `oracle:construct` pairs in the emitter half of `tests/fixtures/oracle_construct_reach_ledger.json` | reference dispatches in each `backends/<tier>/emit.py` | 238 over 686 |

The native-chain surface changed in this slice. The first slice summed the
`*_DOCS` tables beside `LOWER_GAP_DOCS` (149 at `f1443de3`), which are other
tests' inputs. The ratchet recomputes the residual over each tier's oracle
`CORPUS`, so that is the surface now.

The census allowance is **at its floor**. It is 0, so it cannot improve and can
witness nothing until something regresses it. Only two counters can move today.

## 4. Progress cannot be bought cheaply

A reward that padding can satisfy is a gate that cannot fire, pointed the other
way. The first slice compared counts, and a count is defeated by a substitution:
delete a failing document, add any passing one, and the value falls while the
universe holds. The rules are now on member identity, in this order:

1. **No base member may leave the surface.** Deleting, renaming or moving a
   document, a corpus entry or a dispatch arm is a regression, whatever else
   moved.
2. **No member may newly fail.** Retiring two entries while adding one is a
   regression, not a net improvement of one.
3. **An improvement is a crossing**: a base-failing member that is now passing,
   and therefore still on the surface.
4. **The crossing document must be byte-identical on both sides.** Gutting a
   failing document until it passes is deleting it in place.
5. **The instruments must hold still.** A counter measures a distance from a
   reference, with a tool, under a test harness. When any of them changed in the
   same diff that shrank the failing set, the counter reads `unreadable`: it
   cannot tell a better system from a weaker measurement. The instruments are
   `src/revl/` (the reference compiler), the pytest configuration files and
   `conftest.py`, and per counter the census tool; the reference emitters, the
   ratchet file and the tier oracle files; or the reach tool, the coverage tool
   and the reach test. Where a table lives inside an instrument file, its
   literal may change and nothing else in that file may.
6. **The scorer's own code reads both sides.** The reach surface is sized by the
   scorer's copy of `tools/selfhost_coverage.py`. The first slice loaded the
   candidate's copy into the scoring process, where candidate code could reach
   every other component's verdict.

Rule 5 costs something: an instrument change and the improvement it enables
cannot land in one candidate. Land the instrument change first; it is
`unchanged` on its own and blocks nothing in this component.

### 4.1 Each cheap move, and what refuses it

Every row is a test in `tests/test_evolution_progress.py`.

| cheap move | census | native chain | reach | refused by |
|---|---|---|---|---|
| delete the failing member | regressed | regressed | regressed | rule 1 |
| delete it and add a filler so the count holds | regressed | regressed | regressed | rule 1 (census: also rule 4) |
| rename it | regressed | regressed | regressed | rule 1 |
| repoint `CORPUS_DIR` at easier documents | n/a | regressed | n/a | rule 1 (members are resolved paths) |
| gut the failing document, then retire its entry | regressed | regressed | n/a | rule 4 |
| weaken the reference until it agrees | unreadable | unreadable | unreadable | rule 5 |
| drop a hand-written census program and its entry | unreadable | n/a | n/a | rule 5 (the corpus walk never counted it) |
| edit the ratchet, or deselect it in `pyproject.toml` | n/a | unreadable | unreadable | rule 5 |
| add a no-op corpus document | unchanged | unchanged | unchanged | surface growth is not a crossing |
| add a test that asserts nothing | unchanged | unchanged | unchanged | no counter reads tests |
| **delete a failing table entry with nothing fixed** | **improved** | **improved** | **improved** | **another component** |

The last row is the limit of this component. The tables are the artifacts, so a
table edited to a lie looks exactly like a fix. Each table is held to the tree by
a ratchet another component runs:

* census: `tools/gate_reference_census.py --check`, run by `no-new-false-admits`,
  fails on a divergence that is not baselined;
* native chain: `tests/test_selfhost_compile.py::test_the_residual_is_located_in_lower_not_in_the_emitter`,
  run by `tests`. Measured: removing `component_edges.rvl` from
  `LOWER_GAP_DOCS["rust"]` makes this counter read `11 to 10, improved`, and the
  ratchet's `[rust]` case fails in 10 seconds. `tools/affected_tests.py` selects
  that file for the change;
* reach: `tests/test_oracle_construct_reach.py::test_the_committed_ledger_matches_this_tree`,
  run by `tests`.

That is why progress is a conjunct and not the reward.

### 4.2 One cheap move that is still open

`reach-gaps` credits a construct when a corpus document reaches it.
`selfhost_coverage.corpus_documents` reads a tier's corpus with a regular
expression over the `CORPUS` block, so a document named only in a COMMENT inside
that block is counted as reaching, while the emitter oracle, which executes the
list, never runs it. A candidate could add a document that reaches an unreached
construct, name it in a comment, and delete the ledger line: the reach ledger's
own check agrees, and the construct is credited without any oracle checking the
document. Today the regular-expression set and the literal set are identical on
all six tiers. What would close it: `corpus_documents` reading `CORPUS` with
`ast.literal_eval`, as this module does. That is a change to the reach tool and
is not made here.

## 5. Which base

Progress is judged against the candidate's own base: `git merge-base HEAD
<base>`, not the named ref's tip at scoring time.

* **The trunk moved on after the fork**, carrying an improvement the candidate
  lacks. Against the tip, the candidate reads as a regression of a counter it
  never touched. Against the merge base, it is judged on its own diff.
* **The candidate merged the trunk**, carrying somebody else's improvement into
  its tree. Against its fork point it would be credited for that work. Against
  the merge base, that work is on both sides and credits nobody.

Both are tests, each with the wrong reading computed beside the right one so the
test cannot pass vacuously. The resolved sha is recorded in the ledger.

What this cannot see: a record whose `base` names an OLDER commit than the newest
trunk commit its tree contains moves the merge base back and credits the
difference. Nothing here can tell that a named commit is stale. The record's
`base` must be written by the loop, never by the candidate. The other nine components
still read `candidate.base` verbatim. Against a moving ref, their errors run
toward failing (for example, `scope` would count the trunk's changes as the
candidate's), and no other component credits anything. That is why only
`progress` resolves the merge base.

## 6. What the counters cannot see

A candidate that makes a real improvement no counter measures is now **rejected**.
This is the cost of the decision in section 2, written down so it is not
discovered later. The counters see the census allowance, the native-chain
residual and the construct-reach gaps. They do not see:

* a bug fix whose reproducer was not already a named entry in one of them;
* a performance improvement (allocation and time budgets are in no ledger);
* a new language feature, stdlib function or tier construct: new surface grows
  a universe, and growth is `unchanged`;
* a new test, a new corpus document the chain already reproduces, a new formal
  theorem, a documentation correction;
* a refactor, dead-code removal or a clearer diagnostic;
* a security fix that is not a census divergence;
* any improvement to a measuring tool (rule 5);
* a reach-gap document the native chain cannot lower yet: the document must also
  enter `LOWER_GAP_DOCS`, which is a newly failing member (rule 2), so the
  candidate regresses one counter to improve another.

Also, the census allowance is at zero (section 3), so census work cannot be
credited at all today.

The way to lower this cost is to add counters (a performance ledger, the formal
theorem ledger read as a rising set, the named-gap tables of the other oracles),
not to relax the rule.

## 7. Failure direction

Fail-closed, with no third value.

| direction | when |
|---|---|
| `improved` | a base-failing member crossed; none left; none newly failed; its document is unedited; no instrument moved |
| `unchanged` | same failing set; no base member left the surface |
| `regressed` | a base member left the surface, a member newly failed, or a crossing document was edited |
| `unreadable` | either side could not be read, the merge base does not exist, or an instrument moved in the diff that shrank the failing set |

`unreadable` is not `unchanged`. A missing artifact, a malformed one, an artifact
absent at the base, a ledger with its entries deleted and a counter that raised
are all `unreadable`, which fails the component.

## 8. Non-vacuity

Mutations applied to the rules, each run against `tests/test_evolution_progress.py`
and `tests/test_evolution_reward.py`:

| mutation | tests failed |
|---|---|
| the component does not require an improvement | 10 |
| the surface compared by count, not identity | 5 |
| the edited-document rule removed | 2 |
| the instrument rule removed | 8 |
| the base read verbatim instead of the merge base | 2 |
| rename detection left on in the changed-file read | 1 |
| table literal edits treated as instrument moves | 3 |
| a newly failing member allowed | 7 |
| `progress` removed from `COMPONENTS` | 4 |

The counters are also read against this repository itself, each failing set a
strict subset of its surface, so a counter that silently answered nothing over
nothing would still red. Every instrument pattern is required to match a tracked
file, so an instrument that guards nothing reds too.

## 9. What this does not do

* It does not close the 7 named gaps `reach-gaps` cannot size at the base: the
  reach tool's `lower_ir` and `compile` tables are built by helpers that
  hardcode the repository root. Bringing them in means a path-taking reader in
  `tools/oracle_construct_reach.py`.
* It does not close the comment-in-`CORPUS` path in section 4.2.
* It does not detect a stale `base` named in a candidate record (section 5).
* It does not add counters. Section 6 is the list of what they would be for.
