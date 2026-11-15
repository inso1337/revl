# Design: a scoring set the candidate cannot read

Design-doc id 535 (531 went to another design doc that landed in the same
wave; design-doc ids are independent of roadmap item numbers, so roadmap
item 535 is the curriculum named below and not this document's subject).
Roadmap item served: 537 (issue #1207, the held-out scoring set). Siblings
it must not contradict: 535 (issue #1205, the curriculum) and 536 (issue
#1206, the reward). `docs/design/457-selfhost-type-layer.md` is the
structure this follows.

Sources studied, all at `c136eae6`: `tools/gate_reference_census.py`
(`CORPUS_DIRS`, `ADMISSION_PROGRAMS`, `load_corpus`, `load_fuzz`,
`SelfhostEngine`, `bucket`, `NEVER_BASELINED`), `tools/build_gate_crate.py`
(the frontier and admission tables), `tests/test_selfhost_lower.py`
(`ACCEPTED_PROGRAMS`, `REJECTED_PROGRAMS`, `_classify`),
`tools/fuzz_frontend.py` (`_mutate`), `tools/affected_tests.py` (the
`tools/*.py` selector), `src/revl/lexer.py` (`KEYWORDS`), and roadmap items
535, 536, 537.

## 0. The decision in one paragraph

A repository cannot hide a file from a process that has the repository, so
"held out" cannot mean a hidden file and this design does not pretend it does.
What is held out is the **draw**: the specific finite set of programs one
scoring run is judged on. The generator, the scorer, the bucket vocabulary and
the fence are all public and meant to be read; the draw is a function of a
**seed that is proved absent from the tracked tree before a single program is
generated**, and it exists only in memory for the length of one run. Four
things are enforced rather than promised: the draw is never written to the tree
(so it cannot become readable next time); the seed is a file outside every git
repository, readable only by its owner, and its content is checked against the
tracked tree with `git grep` and refused if found there; each scored run
consumes the seed's digest in a rotation ledger and a digest already seen is
refused; and the candidate's changed-file set is required, bounded in size, and
a diff that reaches the scorer's own files is refused instead of scored. Every
unknown is a refusal, including the two vacuity unknowns: a draw on which the
gate issued no admission, and a draw carrying no near miss the reference
refuses, are refusals rather than clean verdicts.

## 1. The gap, measured

At `c136eae6`, `tools/gate_reference_census.py` scores 846 programs. Every one
of them is a file in this repository or a literal in a file in this repository:

| scoring corpus | where it lives | readable by a candidate |
|---|---|---|
| differential oracle programs | `ACCEPTED_PROGRAMS`, `REJECTED_PROGRAMS` in `tests/test_selfhost_lower.py` | yes |
| census corpus | the eight `CORPUS_DIRS` walked with `rglob` | yes |
| issued-admission programs | `ADMISSION_PROGRAMS`, 14 entries in the census tool | yes |
| gate fixtures | `tests/fixtures/` | yes |
| fuzz inputs | `tools/fuzz_frontend.py::_mutate` over the corpus, seeded by `--seed` whose default is `0` | yes |

The fuzz row is the closest thing in the tree to what this item asks for, and
it is worth being precise about why it is not it. `load_fuzz` already generates
at score time from a seed, so the mechanism is half built. But the seed
defaults to `0`, it is passed on a command line, nothing checks it is unknown
to anybody, and the census refuses to combine `--fuzz` with `--check` or
`--record` at all: fuzz cases "are a sweep, not a gate". Nothing in the tree
scores anything on a set whose identity a candidate could not compute.

Census baseline at `c136eae6`, for the record and because this design must not
move it: `false-reject` empty, `false-admit` at 9 (`false-admit/T1` 6,
`false-admit/TYPE` 3), `false-admission` empty.

## 2. What "held out" can mean, and what each option leaves open

Four candidate mechanisms. The first is chosen, the fourth is adopted as a
component, and the two in between are rejected with their reasons.

### 2.1 Generate at score time from a seed the candidate never sees (CHOSEN)

The set is not a file, so there is nothing to hide. The grammar is public; a
candidate can read it and can make itself correct over the whole space, which
is not an attack but the intended outcome and the reason this option survives
where the others do not.

What it leaves open, in descending order of seriousness:

1. **The candidate edits the generator to narrow the space.** Real, and the
   fence answers it: `HELD_OUT_FENCE` names the scorer's files and a diff that
   touches one is refused rather than scored. The fence is only worth its
   completeness, so `tests/test_heldout_scoring.py` classifies the whole import
   closure of a scoring run and reds on a file in no category.
2. **The candidate makes the generator depend on something it controls.**
   Answered by construction: `draw()` takes `(seed, count)` and reads no file.
   The static half of the closure test reads the literal `ROOT / "..."` joins
   out of the scorer and requires each one to be classified, so a newly named
   repo path fails before it is loaded.
3. **The seed leaks.** Through a CI log, through a verdict record, through an
   operator pasting it into a pull request. Not eliminated, but no longer
   silently replayable: the verdict carries `sha256` of the seed rather than the
   seed, the seed is a file outside every git repository and owner-only, and a
   digest the rotation ledger has already seen is refused. A leaked seed's draw
   is still readable and the only recovery is still to re-draw; what custody and
   rotation add is that the re-draw cannot be the same draw twice without the
   verdict saying so. This is the residual that option 2.4 partially covers and
   nothing here eliminates.
4. **The candidate special-cases the grammar's surface.** A compiler change
   that handles generated-looking programs on a shortcut that real programs do
   not take would score clean and be wrong. Slice 1 left this untested. Slice 3
   splits it into two quantities that slice 1 conflated, closes one and
   measures the other; section 3.7 is that work. In short: a shortcut is only
   harmful when the surface tells it keys on **separate** the `near:` half from
   the `inside:` half, and separation is now enforced. Mere **recognition**
   that a program is generated remains, cannot be reduced by this mechanism,
   and stays the named residual.
5. **The operator picks a seed the candidate suggested.** Not answered by
   custody. The seed file's location and permissions are checked; the choice of
   the string inside it is still the operator's, and a candidate that talked the
   operator into a particular seed has still chosen the draw. Custody is slice
   5's other half and is done; this remains open.

### 2.2 Keep the set outside the repository and have the gate fetch it (REJECTED)

Rejected on three pieces of evidence, not on taste.

**Availability.** This repository is public. GitHub does not expose repository
secrets to a workflow triggered by a pull request from a fork, so a gate that
needs a credential to fetch its corpus is unavailable on exactly the
contributions it exists to judge. A check that is unavailable on the
interesting case either fails open or blocks every outside contribution, and
the first of those is the failure mode this repository has measured most often.

**Diagnosis burns the set.** A held-out program that fails has to be shown to
somebody before it can be fixed, and the convention here is that a finding
carries a reproducer. Printing the failing program into a public pull request
makes that program permanently readable. Option 2.1 has the same leak and
recovers from it by re-drawing; a curated out-of-tree set is finite and
hand-made and does not recover.

**Nobody can run it.** This repository has now measured five separate checks
that ran on every pull request and could not fail. A check that can only run in
one privileged environment is on the same path, and it is the path this design
is trying not to take.

Not rejected forever. A curated adversarial set is the right use of an
out-of-tree location later, once there is a reason to prefer hand-written
programs to generated ones. It is a non-goal here, named in section 6.

### 2.3 A transformation of in-tree artifacts the candidate cannot invert (REJECTED)

Rejected outright. A deterministic function of readable inputs has readable
output: the candidate runs the transformation. There is no "cannot invert"
without a key, and the moment there is a key the scheme **is** option 2.1 (the
key is the seed) or option 2.2 (the key is held elsewhere). Stating it plainly
because the brief asks for it: an option whose only defence is that the
candidate would not think to apply the published transformation to the
published inputs is not a defence, and this is that option.

### 2.4 A commitment scheme, published before and revealed after (ADOPTED AS A COMPONENT)

A commitment proves the scorer did not choose the set after seeing the diff. It
does not hide the set from a candidate that has the revealed set, so it is not
a substitute for 2.1; the two answer different questions and the honest thing
is to take the half that is free.

What is taken: the verdict record carries `seed_digest` and `draw_digest`, so a
claim "this diff was scored on 200 programs" is checkable by anyone later
handed the seed, and an operator who scores twice on the same seed is visible
in the record rather than invisible.

What is **not** taken, and must not be claimed: the commitment is published by
the same operator who holds the seed, so it binds that operator's honesty and
nothing else. A commitment with a third-party timestamp is out of scope and is
recorded as a named, unowned demand in section 6.

## 3. The mechanism

`tools/heldout_scoring.py`. One entry point, `run()`, that returns a verdict
record and an exit status, and a `main()` that is a thin wrapper so the gate is
the same code whether it is called from CI or from a test.

### 3.1 What is withheld, and from whom

Withheld from the **candidate that authored the diff**: the identity of the
draw. Not withheld from a reviewer, an auditor, or the operator: anyone handed
the seed reproduces the draw byte for byte, which is what makes a finding
actionable and a verdict checkable.

Not withheld at all, deliberately: the grammar, the near-miss families, the
scorer, the bucket vocabulary, the fence, and this document.

### 3.2 The four enforcements

**The set is not a file.** `draw()` builds programs in memory. Nothing writes a
`.rvl`. `test_a_scoring_run_writes_nothing_into_the_tree` compares
`git status --porcelain` across a full run, over the whole tree rather than
over the directories somebody remembered: `tests/fixtures/emit_py_corpus/` is
globbed into every byte-agreement projection and the census walks eight
directories with `rglob`, so a generated program dropped in either becomes a
permanent readable member of every corpus. The cheap mistake is writing
somewhere nobody thought to check.

**The seed is a file outside every git repository, readable only by its owner.**
`--seed-file` / `REVL_HELDOUT_SEED_FILE` names a file; a literal seed is not
accepted at all, because a literal leaves nothing to check. The file must sit
outside the work tree being scored and outside every other git repository
(including a bare one), its mode must carry no group or other bits, and it must
be a regular file of sane size. The location probe runs from the path's
resolved target, so a symlink into a checkout is refused rather than accepted
on the spelling of the link, and only git's explicit "not a git repository"
counts as outside; a repository that cannot be inspected is a refusal, not an
accept. Its CONTENT is then checked with `git grep --fixed-strings` over the
tracked tree: a seed found there is refused (`seed-in-tree`), a seed shorter
than 16 characters is refused without being searched for (a short string matches
by accident and the refusal would be noise), and a tree that cannot be searched
is refused (`seed-check-unavailable`). This is the enforcement that turns "the
candidate cannot read it" into a check: the repository is readable, so the
requirement is that the seed file is outside it, and that requirement is
executable. The residual is stated rather than papered over: a candidate that
runs as the same OS user can read an owner-only file, and a candidate that
controls the gate host can lie about the verdict (section 6, non-goal). What the
check establishes is location and permissions, not process isolation.

The test suite eats its own cooking here. A literal seed committed in
`tests/test_heldout_scoring.py` would be refused, correctly, so the fixture seed
is the digest of a phrase rather than the phrase, written into a disposable
owner-only file outside the tree, and
`test_the_fixture_seed_is_itself_absent_from_the_tree` holds the content half.
A fixture seed is not held out from anybody -- a reader recomputes it in a line
-- which is why the verdict records `seed_source` and this document says so
rather than leaving the distinction to a comment.

**A scored seed is consumed once and the consumption is visible.** Every run
that produces a score appends the seed's digest -- never the seed -- to a
rotation ledger beside the seed file (outside every git repository) and records
the rotation count in the verdict. A digest the ledger has already seen is
refused (`seed-digest-reused`) before anything is drawn. A REFUSED run produced
no score, so it does not consume the seed and can be retried; that is the
difference between "the gate declined to answer" and "this draw has been paid
for". Rotation itself is the operator's act: writing a new seed file is what
rotates, and replay of an old one is now visible in the artifact and refused.
The ledger belongs to the seed-file path, so the model is one seed file
rewritten in place, which keeps every earlier digest of that file in its
history. A copy of the seed at a new path starts a new history, and the verdict
alone cannot distinguish that fork from a legitimate first run: `rotation_count`
1 is exactly what a real first run reports. That is a limit of a ledger the
scorer keeps, not a guarantee about an operator who deliberately forks it.

**The diff cannot reach the scorer.** `HELD_OUT_FENCE` is the `held_out` view
of the evaluator set in the table below. Those are the files that decide what is
drawn and what counts as a divergence. The candidate's changed-file set is
**required** -- a run that does not know what the candidate changed cannot
claim the candidate could not reach the scorer, so `no-diff` is a refusal --
and a diff that touches a fenced path is refused with the path named.

The changed set is also bounded, but the FENCE is checked first: a large diff
that touches a fenced path keeps the `diff-reaches-fence` refusal, which CI
treats as a warning. On a diff that does not reach the fence, more than
`MAX_DIFF_FILES` (200) changed files, or more than `MAX_DIFF_LINES` (2000) lines
added plus removed when a base supplied the line count, is refused with the
limit named (`diff-too-large:...`). The line count leaves REGENERATED paths out
(`REGENERATED_PATHS` / `REGENERATED_PREFIXES`): the census reproduction records
and the census-artifact catalogue, `crates/revl-gate/src/selfhost.rs`, and the
corpus provenance manifest. Regenerating one of those is not a candidate's edit
and can add thousands of lines by itself; `selfhost/lower.rvl` is deliberately
not on the list because it is a source. An unbounded diff is a review problem
the draw cannot answer; the dependency-closure half of the same demand is still
a named demand in section 6.

What is deliberately **not** fenced is the load-bearing half. `src/`,
`selfhost/`, `backends/`, `crates/`, `stdlib/`, `tests/` and the corpus
directories are the **subject**: a candidate improving the gate has to be able
to change them, and fencing them would forbid the work the loop exists to
produce. The fence is the scorer, never the subject. The fenced paths under
`tests/` are exempted by name rather than by forbidding the directory.

#### The evaluator set (issue #2802)

The fence above is one view of one enumeration. `EVALUATOR_PATHS` in
`src/revl/kernel_boundary.py` is the whole evaluator: the code, data and
protocol that decide a scorecard and a draw. The three refusals are views of it,
not copies:

| evaluator path | refused by | what it decides |
|---|---|---|
| `src/revl/admission.py` | kernel | the admit decider |
| `src/revl/admit_profile.py` | kernel | the profile a candidate is admitted under |
| `src/revl/attest.py` | kernel | the attestation chain |
| `src/revl/taint.py` | kernel | the authority lattice and declassification |
| `src/revl/retention.py` | kernel | the retention deadline (`G-RETAIN`) |
| `src/revl/kernel_boundary.py` | kernel, held-out | this enumeration, its views and the digest; it defines the held-out fence, so it is on that fence too |
| `crates/revl-gate` | kernel | the embedded admission gate |
| `formal` | kernel | the formal models the guarantees cite |
| `tools/gate_reference_census.py` | kernel, held-out | the census and the reference it compares |
| `tools/gate_reference_census_baseline.json` | kernel, held-out | the census allowance and the held-out bypass allowance |
| `tools/heldout_scoring.py` | held-out, authority | this scorer |
| `tests/test_heldout_scoring.py` | held-out, authority | this scorer's own tests |
| `tools/build_gate_crate.py` | held-out, authority | the gate crate the draw is scored against |
| `tests/test_selfhost_lower.py` | held-out, authority | the differential oracle programs |
| `tools/evolution_controller.py` | authority | the lifecycle that reduces stage records |
| `tests/test_evolution_controller.py` | authority | the lifecycle's own tests |
| `tools/evolution_reward.py` | authority | the component list and the retention rule |
| `tests/test_evolution_reward.py` | authority | the reward's own tests |
| `tools/evolution_progress.py` | authority | the progress counters |
| `tests/test_evolution_progress.py` | authority | the counters' own tests |
| `tools/evaluator_protocol.py` | held-out, authority | the loader that puts the enumeration in front of the tools |
| `tests/test_evaluator_protocol.py` | authority | the enumeration, the views and the digest |

Read the table as the refusal, not as a directory map. A path in the `kernel`
column is refused by the controller with `KERNEL_INTERSECTION`; a path in the
`authority` column is refused with `FENCE_TOUCHED`; a path in the `held-out`
column is refused by this tool with `diff-reaches-fence:<path>`. The census, its
baseline and this enumeration are in two columns because they decide two
questions, which is why they are one row and not two. The enumeration is on the
held-out fence because it DEFINES that fence: a diff that edits the table must
not be scored by the fence it widened. `tools/evolution_reward.py` and
`tools/evolution_progress.py` were on none of the old lists, so a diff could
edit the retention rule and no check named it.

The enumeration's identity is `protocol_digest`: `sha256:<hex>` over the
evaluator's tracked bytes, read from the scorer's own checkout. It is written
into every scorecard and every controller verdict, and the controller refuses a
stage record whose digest differs from its own. It proves identity, not
correctness: who holds the expected value, and how it rotates, is the custody
question in issue #2762.

Refused is not failed. A candidate that has to change the census tool is making
a semantic change to the scorer, and item 537's own list says a semantic change
is the case that routes to human review. `REFUSED` is that routing, which is
why it is exit status 2 and not exit status 1.

The change that introduces this tool is itself such a change, and the tool says
so. Run with `--diff-base origin/main` on the branch that adds it, the verdict
is `diff-reaches-fence:tests/test_heldout_scoring.py,tools/heldout_scoring.py`
and no score. That is the mechanism behaving correctly on the first diff it was
ever pointed at, and it is also the reason the evidence in section 4 is a
mutation of the subject rather than a score of this diff.

### 3.3 The grammar: `admission-surface/v1`

The draw alternates two halves.

**Inside** the admission surface: interface declarations and transparent scalar
aliases, which is the region carrying no term the reference type layer decides
and therefore the region `revl_gate::issue_admission` will certify. Zero to
three aliases over the six scalar spellings, one to three services, zero to
four methods each, zero to three parameters each, an optional return, an
optional leading comment.

**Near misses**, one token outside the surface, in seven families:
`duplicate_service`, `duplicate_method`, `fn_body`, `generic_type`,
`record_alias`, `unknown_type`, `component`. These generalise the fourteen
hand-written `ADMISSION_PROGRAMS` from a list a candidate memorises into a
space it has to be correct over.

Two invariants are tests rather than intentions. Every family has to be
reachable from some seed, or a family that stopped being generated would narrow
the draw silently. And the reference has to admit every program the draw labels
`inside:`, because "inside the admission surface" is a claim about the
reference and not about the generator. That second one caught a real defect
during bring-up: the word list carried `emit`, which is a keyword, and 8 of 200
programs labelled `inside:` were parse errors. The tool now refuses a draw
whose inside half the reference refuses (`draw-inside-refused-by-reference`),
and the word list is held against `revl.lexer.KEYWORDS`.

### 3.4 What is scored, and what is not

The predicate is the **zero-tolerance direction only**: `false-admission` (the
gate ISSUED an admission for a program the reference refuses) and `gate-fault`
(the gate crashed). Those are the two outcomes `tools/gate_reference_census.py`
gives no allowance at all: `false-admission` is its `NEVER_BASELINED`, and a
fault is a crash rather than a verdict.

`false-admit` -- the no-objection bypass -- was **out** of slice 1, and the
reason is measured rather than assumed. A prototype generator with an eighth
family that appends an alias shadowing a builtin drew
`type Str = Str`, which the reference refuses with
`type alias cycle: Str -> Str` and the gate raises no objection to:
`false-admit/TYPE`, the identical tag and family as the in-tree
`examples/rejections/t18_type_alias_cycle.rvl`, which is already one of the
three `false-admit/TYPE` entries in the census baseline. A random draw re-finds
the families the baseline already allows, so scoring `false-admit` absolutely
would red on a known gap rather than on the candidate.

**Slice 2 adds it, capped by family.** `bypass_allowance()` reads the
`false-admit/<tag>` tags out of `tools/gate_reference_census_baseline.json` --
`T1` and `TYPE` -- and compares rather than copying, so the allowance this tool
enforces is the one the census publishes. A tag the baseline carries is the
known gap re-found on new programs and is counted in `bypass.families` without
being a finding. A tag it does not carry is a bypass family nobody has read,
found on a program nobody could read, and it makes the verdict divergent. The
baseline moves from `SCORING_UNREACHED` onto `HELD_OUT_FENCE` as the visible
consequence of the scorer starting to read it: a candidate that widens its own
allowance by re-recording is refused.

**And the direction is not reachable on this grammar, which is the finding
slice 2 actually produced.** `bucket()` routes a no-objection against an `OUT:`
reference refusal to `no-objection-out-of-slice`, never to `false-admit`, and
every refusal the reference issues over an `admission-surface/v1` draw is
`OUT:` -- duplicate services, duplicate methods, unsatisfied provisions. On a
200-program draw: 42 near misses the reference refuses, **0 of them in-slice**.
So `false-admit` cannot fire here however broken the gate is. The predicate is
implemented and exercised through an injected in-slice reference, and the
verdict record carries `bypass.reachable` so an unreachable direction cannot be
read as a live one. Making it live needs a grammar that produces in-slice
refusals, which is slice 3's widening past the admission surface and is not
done here. Shipping the predicate with `reachable: false` in the artifact is
the honest state; shipping it silently would have been the sixth check in this
repository that runs everywhere and cannot fail.

### 3.5 The failure direction, named branch by branch

Every branch that cannot answer the question refuses. There is no path on which
this tool falls back to scoring against the in-tree corpus: a gate that
silently scores a readable set when the held-out one is unavailable is the
fail-open shape, and most serious findings in this repository have it.

| refusal | when |
|---|---|
| `no-seed` | no `--seed-file` and no `REVL_HELDOUT_SEED_FILE` |
| `seed-file-in-tree` | the seed file is inside this or another git repository |
| `seed-file-missing` | the path is not an existing file |
| `seed-file-readable-by-others` | the file's mode carries group or other bits |
| `seed-file-too-large` | larger than 4096 bytes |
| `seed-file-unreadable:...` | the file could not be stat'ed or read |
| `seed-file-unresolvable:...` | the path could not be canonicalised |
| `seed-empty` | the file holds only whitespace |
| `seed-too-short` | fewer than 16 characters |
| `seed-in-tree` | `git grep` finds it in the tracked tree |
| `seed-check-unavailable:...` | git missing, or an exit status that is neither found nor not-found |
| `seed-location-unavailable:...` | git could not be asked whether the file is inside a repository |
| `seed-digest-reused` | the rotation ledger already holds this digest |
| `rotation-ledger-corrupt` | the ledger holds a line that is not a digest |
| `rotation-ledger-unavailable:...` | the ledger could not be read, created or written |
| `protocol-digest-unavailable:...` | the evaluator's tracked bytes cannot be read, so the scorer cannot name the protocol it would score under |
| `no-diff` | neither `--diff-base` nor `--diff-paths` |
| `diff-unavailable:...` | `git diff` failed |
| `diff-too-large:files:<n>><limit>` | more changed files than `MAX_DIFF_FILES` |
| `diff-too-large:lines:<n>><limit>` | more added plus removed lines than `MAX_DIFF_LINES` |
| `diff-reaches-fence:<paths>` | the candidate changed a scorer file |
| `engine-unavailable:...` | the census, the reference or the gate would not build |
| `draw-too-small`, `draw-exhausted-identifiers`, `draw-unknown-family:...` | the generator cannot produce the set |
| `draw-reached-no-admission-arm` | the gate issued zero admissions over the draw |
| `draw-carries-no-refused-near-miss` | the draw carries nothing the guard could have got wrong |
| `draw-inside-refused-by-reference` | the draw is not the set it claims to be |

The last three are the vacuity refusals and they are the reason this is not the
sixth check in this repository that runs everywhere and cannot fail. The census
bucket `agree-admit` conflates an ISSUED admission with a bare no-objection
where the reference also admits, so an empty `false-admission` bucket alone
does not distinguish a sound gate from a guard that never ran. The liveness
counters separate them, and a clean verdict is only reachable when the arm
actually ran on something it could have got wrong.

### 3.6 The verdict record

JSON, written by `--json` and rendered to stdout either way. It carries
`verdict` (`clean` / `divergent` / `refused`), `refusal`, `grammar`, `size`,
`rotation` (the policy string), `rotation_count` (the ledger index of this
scored run, 1 for the first), `protocol_digest`, `seed_digest`, `seed_source`,
`draw_digest`, `fence`, `candidate_changed_files`, `liveness`,
`zero_tolerance` (the case ids, by bucket) and `buckets` (counts). A refusal
record carries the name and an empty score, never a partial one.

`protocol_digest` is the evaluator identity from section 3.2. It is in every
record, refusals included, so a record that names no protocol is visibly
different from one that names this protocol.

`seed_source` is `argv` or `env`, and it names where the seed FILE PATH came
from, not where the seed came from: the seed itself exists only in the
operator's file. It is in the record because the tool cannot tell whether that
file is secret and should not pretend to. It can tell, and does, that the file
is outside every git repository, owner-only, and that its content is not in the
tracked tree.

Slice 2 and slice 3 add `bypass` (the allowance, the families found, the
families beyond the allowance, and `reachable`) and `tells` (per named tell,
its recognition and its separation).

### 3.7 The surface tells, and the half of 2.1.4 that closes

The attack is that a candidate reads the public grammar and special-cases its
surface. A **tell** is that attack made executable: a predicate over a
program's bytes, computable by anyone holding `tools/heldout_scoring.py`, that
a candidate could key a shortcut on. Three are named:
`near-miss-name-literals`, `closed-vocabulary`, `fixed-comment-template`.

Two quantities come out of a tell and slice 1 did not separate them.

**Separation** is the gap between a tell's rate on the `near:` half and its
rate on the `inside:` half. `near:` versus `inside:` is the entire scoring
question, so a tell that separates hands a gate the whole verdict without
parsing anything. This is enforced: `separating_tells` runs on every
invocation, before any engine is built, and a draw carrying a tell over
`SEPARATION_BOUND` is refused with `draw-separable-by-tell:<name>`. That is the
treatment `draw-inside-refused-by-reference` already gives a draw that mislabels
itself, and the reason is the same -- a draw that answers its own question is
not the set it claims to be.

**Recognition** is how often the tell fires at all: is this program generated.
Reported, never enforced, and the design has to be plain about why. `draw()`
reads no file, which is what answers attack 2.1.2, so its vocabulary is a
literal in a public file and `closed-vocabulary` fires on every program it will
ever emit. Recognition is 1.00 and no change to this mechanism reduces it.
Driving it down needs the draw to be in-distribution with real programs, which
is a property of the grammar, needs the type layer, and is not done here.

Measured on slice 1's draw and on the one that ships, same seed, 200 programs:

| tell | separation, slice 1 | separation, now | recognition, now |
|---|---|---|---|
| `near-miss-name-literals` | 0.56 | 0.00 | 0.07 |
| `closed-vocabulary` | 0.76 | 0.00 | 1.00 |
| `fixed-comment-template` | 0.00 | 0.00 | 0.00 |

Both separating tells came from the same defect and the second one is the one
nobody would have predicted. Slice 1's `_near_miss` spelled every added name as
a literal -- `twice`, `Box`, `Row`, `Mystery`, `Edge`, `Comp`, `dup`, `probe`,
`id`, `all` -- so ten fixed words appeared in near misses and nowhere else.
That is `near-miss-name-literals` at 0.56 directly. It is also
`closed-vocabulary` at 0.76, because those literals are *not* in the published
word list, so "every identifier comes from `_WORDS`" was itself a near/inside
discriminator, firing on 76% of the inside half and 0% of the near half. A
one-line regex separated the two halves.

The fix is that every name a near miss introduces is drawn from the same
vocabulary as everything else. What stays fixed is structure and not names: a
generic application, a record body, a fn body, a component block, a duplicate
declaration. Those are what put a program outside the admission surface, so a
gate keying on them is giving the right answer for the right reason.

Recognition went **up** when separation went down, from 0.39 to 1.00, and that
is the honest trade rather than a regression: the draw became more obviously a
draw and stopped saying which half a program is in. Only the second of those
was ever the dangerous one.

The bring-up invariant moved with the vocabulary. `_RESERVED` is a frozen copy
of the lexer's keyword table, held against `revl.lexer.KEYWORDS` by a test
rather than imported, so `draw()` keeps reading no file and a candidate cannot
narrow the draw by editing `src/revl/lexer.py` -- which is SUBJECT and has to
stay editable. `_ident` skips a reserved word instead of emitting it, and a
test forces the word list down to a single keyword to prove the guard is what
stands between the draw and a parse error.

## 4. Measurements at `c136eae6` plus this change

Engine: the census fast engine (`selfhost/lower.rvl` through the python
backend, behind the crate's frontier mirror and followed by its admission
mirror). Seed: the suite's fixture seed.

| run | verdict | status | issued admissions | near misses the reference refuses | `false-admission` |
|---|---|---|---|---|---|
| real gate, 40 programs | clean | 0 | 21 | 7 | 0 |
| real gate, 200 programs | clean | 0 | 102 | 35 | 0 |
| certifier mutated to admit everything, 40 | divergent | 1 | -- | -- | 7 |
| certifier mutated to admit everything, 200 | divergent | 1 | -- | -- | 35 |
| certifier mutated to admit nothing, 40 | refused | 2 | 0 | -- | -- |
| certifier mutated to admit nothing, 200 | refused | 2 | 0 | -- | -- |

The two mutated rows are the non-vacuity evidence. A check that has never been
seen to fail is not evidence; the first mutation makes the gate's admission
certifier wrong in the direction that matters and the draw catches 35 of 200,
and the second makes it wrong in the direction that produces a silent vacuum
and the tool refuses rather than reporting clean. The control is the unmutated
row above them, clean on the same seed and the same draw.

A full run at 200 programs takes about 4 seconds wall clock, most of it
compiling `selfhost/lower.rvl` once. The test file is 26 cases in about 16
seconds.

Census, before and after this change, unchanged: 846 programs, `false-reject`
empty, `false-admit` 9, `false-admission` empty.

### 4.1 Slices 2 and 3, at `d4c36a8f` plus this change

Same engine, same fixture seed, 200 programs. The generator changed, so the
draw is not slice 1's: it carries 102 issued admissions and 50 near misses the
reference refuses, against slice 1's 102 and 35.

| run | `false-admission` | `false-admit` families | reading |
|---|---|---|---|
| real gate (CONTROL) | 0 | none | clean |
| surface shortcut, slice-1-shaped draw | 0 | none | **the attack succeeds** |
| surface shortcut, the draw that ships | 50 | none | caught |
| refusal arm removed, in-slice `T9NEW` | 0 | `T9NEW`: 50 | caught |
| refusal arm removed, in-slice `T1` | 0 | `T1`: 50 | clean, baselined family |

The middle three rows are the slice-3 evidence, and they are one experiment
rather than three numbers. The **surface shortcut** is a gate with no compiler
in it at all: it issues an admission for every program a candidate-readable
predicate calls an inside program and declines everything else at the frontier.
Pointed at a draw shaped the way slice 1's was, it scores **clean** -- 0
findings, 102 issued admissions, full liveness -- because `closed-vocabulary`
separated the two halves there and the shortcut rode that separation. Pointed
at the draw that ships, the same shortcut is caught 50 times in 200: the tell
now fires on every program, so the shortcut issues admissions for the near
misses too and every one the reference refuses is a `false-admission`. The
control above them is the real gate, clean on the same seed and the same draw.

The last two rows are the slice-2 evidence and they need an injected in-slice
reference, for the reason section 3.4 gives: the real pair produces 0 in-slice
refusals over this grammar, so the direction cannot be exercised on it. A gate
whose refusal arm is gone raises no objection to all 50, which is a new family
under a tag the baseline does not carry and a divergence, and the identical run
under `T1` -- a tag it does carry -- is clean. That is the allowance working in
both directions.

Census at `d4c36a8f` plus this change, unchanged from the baseline: 848
programs, `false-reject` empty, `false-admit` 9 (6 `T1`, 3 `TYPE`),
`false-admission` empty. A full scoring run still leaves
`git status --porcelain` byte-identical.

## 5. Slice plan

**Slice 1 -- the sealed draw (this change).** The generator, the three
enforcements, the fail-closed verdict, the liveness refusals, the closure
classification, and the mutation evidence. Oracle: `tests/test_heldout_
scoring.py`, 26 cases. Fixtures: none, by construction -- a fixture would be a
file, and the point is that there is not one. Exit: a scoring run on this tree
produces a clean verdict with non-zero liveness, and a mutated certifier
produces a divergent one.

**Slice 2 -- the bypass direction by family (DONE).** The predicate now covers
`false-admit/<tag>`, compared against the census baseline's families rather
than against zero. Oracle: a gate with its refusal arm removed, caught as a new
family, with the same gate under a baselined tag as the control. It also
produced a negative result that is worth more than the predicate: the direction
is **not reachable** on `admission-surface/v1`, because every reference refusal
over the draw is `OUT:`. Section 3.4 has the measurement and the verdict record
carries `bypass.reachable`.

**Slice 3 -- the draw stops looking generated (PARTLY DONE).** Split into the
two quantities section 3.7 names. SEPARATION is closed and enforced: the tells
that told `near:` from `inside:` measured 0.56 and 0.76 on slice 1's draw and
0.00 on the one that ships, and a draw carrying one is now refused.
RECOGNITION is not closed and is not closeable by this mechanism -- it is 1.00
and stays the residual. What remains is the original slice-3 body: widen past
the admission surface into fn bodies and component declarations, which is also
what would make slice 2's direction reachable. That needs the type layer of
`docs/design/457-selfhost-type-layer.md` to have landed far enough that the
reference and the gate are comparable on a fn body at all, and it is the
blocked remainder of item 537.

**Slice 4 -- the promotion verdict (DONE).** `tools/evolution_reward.py`
carries `held-out` as a ninth component. Item 536's eight all read artifacts
that are in the tree the candidate was handed; this one scores it on a draw
that does not exist until score time, which makes it the only component no
other can supply and the only one whose absence from a scorecard is detectable.
Fail-closed on two paths of its own: an unset `REVL_HELDOUT_SEED_FILE` is a
FAILED component and not a skipped one, and a REFUSED run is a fail with the
refusal name in the reason. The seed file path is not a candidate record key,
because a candidate that could name its own seed would choose the draw it is
judged on.

There is also a CI job now, `held-out` in `.github/workflows/ci.yml`, and its
seed is drawn IN THE JOB rather than held in a repository secret. That follows
from section 2.2 rather than working around it: this repository is public,
GitHub withholds secrets from fork pull requests, and a gate unavailable on
exactly the contributions it exists to judge is the shape that section rejects.
A seed that lives for one run and is never recorded is also harder to read than
a long-lived one, which is residual 2.1.3; what it gives up is reproducibility,
so the job prints the seed only when there is a finding to reproduce. The three
exit statuses are not collapsed: clean passes, a finding fails, and a refusal
fails except `diff-reaches-fence` and `diff-too-large`, which pass with an
annotation because a candidate changing the scorer is making a semantic change
that routes to human review, and the size bound is a review bound whose
regenerated-artifact churn is already excluded from the line count. The job is
advisory (`NOT_REQUIRED_CHECKS`) for that reason and the enforcement is in the
reward, where a conjunction admits no advisory verdict.

**Slice 5 -- rotation and custody (CUSTODY DONE, ROTATION ENFORCED).** Where the
seed comes from, who holds it, how often it changes, and what happens when it
leaks. The seed now comes only from a file: `--seed-file` /
`REVL_HELDOUT_SEED_FILE`, required to sit outside every git repository and to
carry no group or other bits, its content still refused if `git grep` finds it
in the tracked tree. Every scored run records the seed's digest and the rotation
count, and a digest the ledger has already seen refuses before a draw. Rotation
itself is still the operator's act -- writing a new seed file is what rotates --
and the CI job now writes its per-run seed to `$RUNNER_TEMP` with mode 0600
instead of exporting a literal. What remains of the slice is what custody cannot
answer: an operator who picks a seed the candidate suggested (2.1.5), a
commitment with a third-party timestamp (2.4), and same-user readability on a
shared host (2.1.3).

**Slice 6 -- the other eight mechanisms (NOT DONE).** Item 537's exit also asks
that the remaining mechanisms in the review's list are each either implemented
or recorded as a named, unowned demand. One of nine is implemented in full, and
the size half of demand 6 now has a named refusal; the dependency-closure half
and the other seven mechanisms are named in section 6 as demands, which is the
weaker half of that exit and is honest about being weaker.

## 6. Non-goals, and the demands this does not meet

**Non-goals.**

* Not a training-data-exclusion mechanism. Nothing here stops a model from
  having read this repository, and nothing here should be described as if it
  did.
* Not a defence against a candidate that controls the machine the gate runs on.
  A candidate with the process can lie about the verdict; the fence is about
  what a diff reaches, not about what a host does.
* Not a replacement for `tools/gate_reference_census.py`. The census measures
  the tree exhaustively and is the right tool for that; this measures a draw
  and is the only tool for that. Both, not either.
* Not a claim about the other eight anti-gaming mechanisms.
* Not a curated adversarial corpus. That is the right use of an out-of-tree
  location (2.2) once there is a reason to prefer hand-written programs, and it
  is not this.

**Named, unowned demands.** Each of these is a thing this design does not do
and does not have an owner for.

1. Cross-tier **execution** agreement rather than verdict agreement.
2. A reference path and a native path that are not both in-tree.
3. Formal checks in a process the candidate's diff does not configure.
4. Mutation testing of the suite (the sensitivity measurement item 535 names).
5. The second-model council of item 516.
6. A bound on a diff's **dependency closure**. Its size half is now implemented
   (`diff-too-large`); the closure is not.
7. A rule that routes a semantic change to the approval authority of item 55.
8. A commitment with a third-party timestamp rather than the operator's own.

## 7. What was verified, and what was not

Verified by running it: the census is unchanged at 848 programs with the same
buckets; the tool refuses on each of `no-seed`, `seed-too-short`,
`seed-in-tree`, a seed file inside this work tree, a seed file inside a sibling
work tree, one behind a symlink into that work tree, an oversized seed file, a
corrupt rotation ledger, a seed file other users can read, a reused seed
digest, a diff over the file limit, a diff over the line limit, a large diff
that touches the fence keeping the fence refusal, the line count skipping the
regenerated paths, `no-diff`, each fenced path, an engine that will
not build, an unreadable bypass allowance, a draw a tell separates, and both
vacuity shapes; a refused run consumes no rotation and a second scored seed
advances the count to 2; a scored verdict carries the seed digest and
`rotation_count` and the seed itself appears nowhere in the record; a scoring
run leaves `git status --porcelain` byte-identical; the reference admits every
`inside:` program of a 200-program draw; a certifier mutated to admit
everything is caught; a gate with no compiler in it that rides a surface tell
is caught 50 times in 200 on the draw that ships and **is not caught at all** on
a draw shaped the way slice 1's was; a gate whose refusal arm is gone is caught
as a new `false-admit` family under an injected in-slice reference and is clean
under a baselined one.

The wire that adds the evaluator identity was verified on a staged checkout:
one byte of `tools/evolution_progress.py` moves `protocol_digest`, a build
artifact under `formal/` does not, every `EVALUATOR_PATHS` entry exists, and
`tools/evolution_controller.py` refuses a stage record from another checkout
with `PROTOCOL_MISMATCH` rather than reading it.

Verified on the wiring: `tools/evolution_reward.py` fails the `held-out`
component on an unset seed file, on a refusal, on a divergence and on a run that
writes no record, and verifies only on a clean one; the probe leaves the
candidate tree as it found it. The CI job, run on the branch that adds it,
exits 2 with `diff-reaches-fence` naming both scorer files, which is the
annotated-and-not-blocking path; because it stops at the fence, this change's
own CI run never reaches a scored pass with a file seed, so the `$RUNNER_TEMP`
seed file and the ledger write beside it are covered by the unit tests and not
by CI. A pinned test reads the workflow and holds `diff-too-large:*` in the
same warning branch as `diff-reaches-fence:*`; the job's live behavior on a
batch push over the line bound is not exercised by this pull request.

**Not verified, and not claimed.** That the draw is indistinguishable from real
programs: it is not, `closed-vocabulary` recognition is 1.00, and section 3.7
says why this mechanism cannot change that. What slice 3 closes is SEPARATION,
which is a strictly weaker claim than section 2.1.4 asks for and is stated as
such. That the `false-admit` direction is live: it is not, 0 in-slice reference
refusals in 200, and the verdict record says so in `bypass.reachable` rather
than leaving an empty bucket to be misread. That an operator's seed is secret --
the tool checks that the file is outside every git repository and owner-only, not
that another process running as the same user cannot read it. That the line
limit is enforced when the caller uses `--diff-paths` instead of a git base:
that mode has no diff to count, so only the file-count limit applies. That the
size bound covers every regenerated artifact: the exclusion list is explicit
and pinned, and a newly generated path that is not on it counts toward the
limit until it is added. That the CI job's live behavior on a batch push over
the line bound was exercised by this pull request: its own run stops at the
fence, so the warning branch for `diff-too-large` is held by a test on the
workflow text rather than by a run. That the fence is complete against a
candidate that reaches the scorer by a route other than editing one of its
files, for example by changing a file `src/revl/` imports that the census's
reference depends on: the closure test classifies that file as **subject**,
which is correct (the candidate must be able to change it) and is also the
limit of what a fence can do. That the CI job has ever run on GitHub: it is
added by this change and its first run is this pull request's.
