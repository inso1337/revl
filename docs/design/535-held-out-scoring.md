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
   not take would score clean and be wrong. **Not answered by this slice, and
   nothing here tests it.** The answer is that the draw must be
   indistinguishable from real programs, which is a property of the grammar and
   not of the mechanism; it is slice 3.
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

`false-admit` -- the no-objection bypass -- is deliberately **out** of slice 1,
and the reason is measured rather than assumed. A prototype generator with an
eighth family that appends an alias shadowing a builtin drew
`type Str = Str`, which the reference refuses with
`type alias cycle: Str -> Str` and the gate raises no objection to:
`false-admit/TYPE`, the identical tag and family as the in-tree
`examples/rejections/t18_type_alias_cycle.rvl`, which is already one of the
three `false-admit/TYPE` entries in the census baseline. A random draw re-finds
the families the baseline already allows, so scoring `false-admit` absolutely
would red on a known gap rather than on the candidate. Slice 2 is the version
of this predicate that compares families against the baseline the way the
census caps its own buckets.

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

## 5. Slice plan

**Slice 1 -- the sealed draw (this change).** The generator, the three
enforcements, the fail-closed verdict, the liveness refusals, the closure
classification, and the mutation evidence. Oracle: `tests/test_heldout_
scoring.py`, 26 cases. Fixtures: none, by construction -- a fixture would be a
file, and the point is that there is not one. Exit: a scoring run on this tree
produces a clean verdict with non-zero liveness, and a mutated certifier
produces a divergent one.

**Slice 2 -- the bypass direction by family.** Extend the predicate from
`false-admission` to `false-admit`, compared against the census baseline's
families rather than against zero, the way `tests/test_gate_crate_admit.py`
caps each bucket by name. Oracle: a test that adds a family to the cap and
expects the refusal, mirroring the census's own non-gameability test. Blocked
on nothing.

**Slice 3 -- the draw stops looking generated.** Widen past the admission
surface into fn bodies and component declarations, and state a property the
draw has to satisfy against the in-tree corpus so that a compiler shortcut for
generated-looking programs cannot score clean. This is the attack section 2.1.4
leaves open and it is the one that needs the type layer of
`docs/design/457-selfhost-type-layer.md` to have landed far enough that the
reference and the gate are comparable on a fn body at all.

**Slice 4 -- the promotion verdict.** Wire the record into the reward of item
536 (issue #1206) as the component that no other component can supply: a field
naming which checks ran where the candidate could not read them. Item 536's
eight components all read artifacts the candidate read; this is the ninth and
it is the only one whose absence is detectable.

**Slice 5 -- rotation and custody.** Where the seed comes from, who holds it,
how often it changes, and what happens when it leaks. Slice 1 records the
rotation it implements (`per-invocation`, operator-supplied) and does not
decide custody.

**Slice 6 -- the other eight mechanisms.** Item 537's exit also asks that the
remaining mechanisms in the review's list are each either implemented or
recorded as a named, unowned demand. Slice 1 implements one of nine. The other
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

Verified by running it: the census is unchanged at 846 programs with the same
buckets; the tool refuses on each of `no-seed`, `seed-too-short`,
`seed-in-tree`, `no-diff`, each of the five fenced paths, an engine that will
not build, and both vacuity shapes; a scoring run leaves
`git status --porcelain` byte-identical; the reference admits all 101
`inside:` programs of a 200-program draw; a certifier mutated to admit
everything is caught 35 times in 200.

**Not verified, and not claimed.** That the draw is hard to game by a candidate
that reads the grammar and special-cases it -- section 2.1.4, untested, slice
3. That an operator's seed is secret -- the tool checks only that it is not in
the repository. That the fence is complete against a candidate that reaches the
scorer by a route other than editing one of its files, for example by changing
a file `src/revl/` imports that the census's reference depends on: the closure
test classifies that file as **subject**, which is correct (the candidate must
be able to change it) and is also the limit of what a fence can do. That the
`false-admit` direction is clean on a held-out draw -- it is not, measurably,
and section 3.4 says which family and why.
