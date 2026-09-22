# Design: a scoring set the candidate cannot read

Design-doc id 535 (531 went to another design doc that landed in the same
wave; design-doc ids are independent of roadmap item numbers, so roadmap
item 535 is the curriculum named below and not this document's subject).
Roadmap item served: 537 (issue #1207, the held-out scoring set). Siblings
it must not contradict: 535 (issue #1205, the curriculum) and 536 (issue
#1206, the reward). `docs/design/457-selfhost-type-layer.md` is the
structure this follows.

Sources studied, all at `52fb8ef3`: `tools/gate_reference_census.py`
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
generated**, and it exists only in memory for the length of one run. Three
things are enforced rather than promised: the draw is never written to the tree
(so it cannot become readable next time), the seed is checked against the
tracked tree with `git grep` and a seed found there is refused, and the
candidate's changed-file set is required and a diff that reaches the scorer's
own files is refused instead of scored. Every unknown is a refusal, including
the two vacuity unknowns: a draw on which the gate issued no admission, and a
draw carrying no near miss the reference refuses, are refusals rather than
clean verdicts.

## 1. The gap, measured

At `52fb8ef3`, `tools/gate_reference_census.py` scores 846 programs. Every one
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

Census baseline at `52fb8ef3`, for the record and because this design must not
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
   operator pasting it into a pull request. Real and not fully answered: the
   verdict carries `sha256` of the seed rather than the seed, and rotation is a
   stated parameter, but a leaked seed's draw is readable and the only recovery
   is to re-draw. This is the residual that option 2.4 partially covers and
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
5. **The operator picks a seed the candidate suggested.** Not answered.
   Custody of the seed is slice 5.

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

### 3.2 The three enforcements

**The set is not a file.** `draw()` builds programs in memory. Nothing writes a
`.rvl`. `test_a_scoring_run_writes_nothing_into_the_tree` compares
`git status --porcelain` across a full run, over the whole tree rather than
over the directories somebody remembered: `tests/fixtures/emit_py_corpus/` is
globbed into every byte-agreement projection and the census walks eight
directories with `rglob`, so a generated program dropped in either becomes a
permanent readable member of every corpus. The cheap mistake is writing
somewhere nobody thought to check.

**The seed is proved absent from the tree.** `git grep --fixed-strings` over
the tracked tree, because what a candidate reads is what is tracked. A seed
found there is refused (`seed-in-tree`), a seed shorter than 16 characters is
refused without being searched for (a short string matches by accident and the
refusal would be noise), and a tree that cannot be searched is refused
(`seed-check-unavailable`). This is the enforcement that turns "the candidate
cannot read it" into a check: the repository is readable, so the requirement is
that the seed is not in it, and that requirement is executable.

The test suite eats its own cooking here. A literal seed committed in
`tests/test_heldout_scoring.py` would be refused, correctly, so the fixture
seed is the digest of a phrase rather than the phrase, and
`test_the_fixture_seed_is_itself_absent_from_the_tree` holds that property.
A fixture seed is not held out from anybody -- a reader recomputes it in a line
-- which is why the verdict records `seed_source` and this document says so
rather than leaving the distinction to a comment.

**The diff cannot reach the scorer.** `HELD_OUT_FENCE` is five paths:
`tools/heldout_scoring.py`, `tests/test_heldout_scoring.py`,
`tools/gate_reference_census.py`, `tools/build_gate_crate.py` and
`tests/test_selfhost_lower.py`. Those are the files that decide what is drawn
and what counts as a divergence. The candidate's changed-file set is
**required** -- a run that does not know what the candidate changed cannot
claim the candidate could not reach the scorer, so `no-diff` is a refusal --
and a diff that touches a fenced path is refused with the path named.

What is deliberately **not** fenced is the load-bearing half. `src/`,
`selfhost/`, `backends/`, `crates/`, `stdlib/`, `tests/` and the corpus
directories are the **subject**: a candidate improving the gate has to be able
to change them, and fencing them would forbid the work the loop exists to
produce. The fence is the scorer, never the subject. Two of the five fenced
paths are under `tests/`, and they are exempted by name rather than by
forbidding the directory.

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
| `no-seed` | no `--seed` and no `REVL_HELDOUT_SEED` |
| `seed-too-short` | fewer than 16 characters |
| `seed-in-tree` | `git grep` finds it in the tracked tree |
| `seed-check-unavailable:...` | git missing, or an exit status that is neither found nor not-found |
| `no-diff` | neither `--diff-base` nor `--diff-paths` |
| `diff-unavailable:...` | `git diff` failed |
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
`rotation`, `seed_digest`, `seed_source`, `draw_digest`, `fence`,
`candidate_changed_files`, `liveness`, `zero_tolerance` (the case ids, by
bucket) and `buckets` (counts). A refusal record carries the name and an empty
score, never a partial one.

`seed_source` is `argv` or `env`, and it is in the record because the tool
cannot tell whether an operator's seed is secret and should not pretend to. It
can tell, and does, that the seed is not in the repository.

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

## 4. Measurements at `52fb8ef3` plus this change

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

### 4.1 Slices 2 and 3, at `86eddc92` plus this change

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

Census at `86eddc92` plus this change, unchanged from the baseline: 848
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
Fail-closed on two paths of its own: an unset `REVL_HELDOUT_SEED` is a FAILED
component and not a skipped one, and a REFUSED run is a fail with the refusal
name in the reason. The seed is not a candidate record key, because a candidate
that could name its own seed would choose the draw it is judged on.

There is also a CI job now, `held-out` in `.github/workflows/ci.yml`, and its
seed is drawn IN THE JOB rather than held in a repository secret. That follows
from section 2.2 rather than working around it: this repository is public,
GitHub withholds secrets from fork pull requests, and a gate unavailable on
exactly the contributions it exists to judge is the shape that section rejects.
A seed that lives for one run and is never recorded is also harder to read than
a long-lived one, which is residual 2.1.3; what it gives up is reproducibility,
so the job prints the seed only when there is a finding to reproduce. The three
exit statuses are not collapsed: clean passes, a finding fails, and a refusal
fails except `diff-reaches-fence`, which passes with an annotation because a
candidate changing the scorer is making a semantic change that routes to human
review. The job is advisory (`NOT_REQUIRED_CHECKS`) for that reason and the
enforcement is in the reward, where a conjunction admits no advisory verdict.

**Slice 5 -- rotation and custody (NOT DONE).** Where the seed comes from, who
holds it, how often it changes, and what happens when it leaks. Two rotations
are now implemented rather than one -- operator-supplied per invocation, and
per-run-drawn in CI -- and neither decides custody. Still open.

**Slice 6 -- the other eight mechanisms (NOT DONE).** Item 537's exit also asks
that the remaining mechanisms in the review's list are each either implemented
or recorded as a named, unowned demand. One of nine is implemented. The other
eight are named in section 6 as demands, which is the weaker half of that exit
and is honest about being weaker.

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
6. A bound on a diff's size and its dependency closure.
7. A rule that routes a semantic change to the approval authority of item 55.
8. A commitment with a third-party timestamp rather than the operator's own.

## 7. What was verified, and what was not

Verified by running it: the census is unchanged at 848 programs with the same
buckets; the tool refuses on each of `no-seed`, `seed-too-short`,
`seed-in-tree`, `no-diff`, each of the six fenced paths, an engine that will
not build, an unreadable bypass allowance, a draw a tell separates, and both
vacuity shapes; a scoring run leaves `git status --porcelain` byte-identical;
the reference admits every `inside:` program of a 200-program draw; a certifier
mutated to admit everything is caught; a gate with no compiler in it that rides
a surface tell is caught 50 times in 200 on the draw that ships and **is not
caught at all** on a draw shaped the way slice 1's was; a gate whose refusal
arm is gone is caught as a new `false-admit` family under an injected in-slice
reference and is clean under a baselined one.

Verified on the wiring: `tools/evolution_reward.py` fails the `held-out`
component on an unset seed, on a refusal, on a divergence and on a run that
writes no record, and verifies only on a clean one; the probe leaves the
candidate tree as it found it. The CI job, run on the branch that adds it,
exits 2 with `diff-reaches-fence` naming both scorer files, which is the
annotated-and-not-blocking path.

**Not verified, and not claimed.** That the draw is indistinguishable from real
programs: it is not, `closed-vocabulary` recognition is 1.00, and section 3.7
says why this mechanism cannot change that. What slice 3 closes is SEPARATION,
which is a strictly weaker claim than section 2.1.4 asks for and is stated as
such. That the `false-admit` direction is live: it is not, 0 in-slice reference
refusals in 200, and the verdict record says so in `bypass.reachable` rather
than leaving an empty bucket to be misread. That an operator's seed is secret
-- the tool checks only that it is not in the repository. That the fence is
complete against a candidate that reaches the scorer by a route other than
editing one of its files, for example by changing a file `src/revl/` imports
that the census's reference depends on: the closure test classifies that file
as **subject**, which is correct (the candidate must be able to change it) and
is also the limit of what a fence can do. That the CI job has ever run on
GitHub: it is added by this change and its first run is this pull request's.
