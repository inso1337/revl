# Design: the evolution curriculum, derived and tiered

Design-doc id 533. 531 was the next free number under `docs/design/` when this
was written and two concurrent lanes had already taken it, for issue #1206 and
issue #1207; 531 and 532 are left to them in issue order. The roadmap item of
the same number is unrelated. Roadmap item served: 535 (issue #1205, the
curriculum). Siblings it must not contradict: 536 (issue #1206, the reward)
and 537 (issue #1207, the held-out scoring set).

Sources studied, all at `c136eae6`: `tools/oracle_construct_reach.py`,
`tools/gate_reference_census.py` and `tools/gate_reference_census_baseline.json`,
`tools/selfhost_coverage.py`, `tools/affected_tests.py`,
`tools/build_gate_crate.py` (`DIGEST_INPUTS`), `src/revl/diagnostics.py`,
`src/revl/evolve_loop.py`, `tests/test_selfhost_lower.py`, `formal/` (43 files,
18 theorem files), and the 108 documents under `docs/design/`.

## 0. The decision in one paragraph

A curriculum is auditable when a task can be regenerated from the tree rather
than recalled, and when its rung is read off a measurement rather than
asserted. `tools/evolve_curriculum.py` does both: seven adapters read seven of
the review's eight sources and emit one task per gap the source exhibits, each
task carrying the repository path it was derived from, the locator inside it,
the gap in one line, the change site, and the mechanism that decides the task
is done. The rung is not attached by the adapter. It is a total function of a
triple measured off that mechanism: how many of the tree's four independently
maintained implementations of revl semantics the mechanism makes agree
(`impls`), how many top-level repository directories its corpus spans
(`breadth`), and whether it proves rather than samples (`proved`). At `c136eae6`
that yields 266 tasks and populations easy 7, medium 244, hard 9, expert 6,
each rung fed by a different source. At `b800456c5` it yields 257 tasks and
easy 7, medium 244, hard 0, expert 6: the `hard` rung is derived from recorded
gate bypasses and PR #1396 and PR #1404 closed the last of them. `--check`
reds when a source cannot be derived, when a rung is one no source reaches, or
when a task names a path that is not in the tree. A rung a source reaches and
finds nothing on is reported with its count and is not a red; §4.3 is why.

## 1. Why the tier is the hard half

The task half was never hard here. The review's eight sources all exist as
artifacts: the tracker, the `UNREACHED` lines of `tools/oracle_construct_reach.py`,
the `false-reject` and `false-admit` buckets of `tools/gate_reference_census.py`,
`docs/design/`, the `ACCEPTED_PROGRAMS` and `REJECTED_PROGRAMS` of
`tests/test_selfhost_lower.py`, `formal/`, `tools/fuzz_frontend.py`, and the
eight corpus directories the census names. Reading one of them and printing a
task is a morning's work.

Nothing in this tree assigned a difficulty tier to anything. That is the
stronger claim in the issue and it was accurate at `c136eae6`: no file, no
ledger and no tool carried a difficulty for any artifact. So the ladder could
not be populated even in principle, and the obvious way to populate it is the
wrong one. A table that says `backends/rust/emit.py: hard` is a table somebody
maintains, which is the asserted-difficulty problem the item exists to remove,
moved one file sideways. It also degrades silently: nothing reds when the table
stops describing the tree.

The tier therefore has to be a function of something the tree already measures
about itself.

## 2. The signal, and why this one

The tree maintains **four independent implementations of revl semantics**, and
that plurality is the repository's most load-bearing structural fact:

| implementation | where | maintained by |
|---|---|---|
| `reference` | `src/revl/**`, `backends/<tier>/emit.py` | the python frontend and six emitters |
| `selfhost` | `selfhost/*.rvl` (12 modules) | the self-host port, item 391 |
| `native` | `crates/revl-gate` (+ its wasm build) | generated from `DIGEST_INPUTS`, item 332 |
| `formal` | `formal/RevL/**` (18 theorem files) | the Lean model |

Difficulty in this repository is not how many lines a change is. It is how many
of those four have to arrive at the same answer, and over how much of the tree
that answer is re-decided. Both are measurable without a table:

- **`impls`** is read off the mechanism that verifies the task. A per-tier
  byte-agreement oracle (`tests/test_selfhost_emit_go.py`) makes the reference
  emitter and its `selfhost/emit_go.rvl` port agree: two. The census
  (`tools/gate_reference_census.py`) runs the reference, the self-host
  admission engine and the native crate over one corpus and buckets each
  disagreement, and its bucket names *which pair* diverged, so a
  `false-admit/*` case is two and a `false-admission` case is three.
- **`breadth`** is the number of top-level repository directories the
  mechanism's corpus spans. An emit oracle reads one fixture directory under
  `tests/fixtures/`: breadth 1, a corpus the task author can read end to end.
  The census walks `CORPUS_DIRS` = `examples`, `tests/fixtures`, `selfhost`,
  `stdlib`, `demo`, `tck`, `backends`, `dogfood`: breadth 8, 555 documents at
  `c136eae6`, which the author neither chose nor can cheaply enumerate. This
  is a categorical distinction (one directory, or the tree), not a threshold
  on a count, which is deliberate: a threshold is a constant somebody tunes.
- **`proved`** is true for `formal/` alone, because it has no corpus. A theorem
  quantifies over every program, so there is no sample to fit.

The ladder over that triple is five lines in `tools/evolve_curriculum.py::tier`,
and each line carries the review's own words for the rung it returns:

| condition | rung | the review's row |
|---|---|---|
| `proved` | expert | formal theorem extensions |
| `len(impls) >= 3` | expert | reference/native admission agreement |
| `len(impls) <= 1` | easy | formatter changes, isolated diagnostics |
| `breadth > 1` | hard | cross-tier lowering, self-host parity |
| otherwise | medium | IR fields, a single-backend change, stdlib |

No adapter may assign a rung. `tests/test_evolve_curriculum.py::
test_tier_is_a_total_function_of_the_signal` asserts that every task's rung
equals `tier(task.signal)` and that two tasks with the same triple never carry
different rungs, so a source cannot smuggle a difficulty in.

## 3. The seven adapters, and the gap each one reads

### 3.1 `construct-reach` (source: known gaps) -> medium, 244 tasks

`tools/oracle_construct_reach.py::survey()` names, per differential oracle, the
reference dispatches that no document in the oracle's corpus reaches. An
unreached dispatch is a construct two implementations have never been made to
agree about on any program, which is a task with the shape "write that program".

One task per `(oracle, construct)` for the six `emit_<tier>` oracles and
`lower_ir`. The artifact is `tools/oracle_construct_reach.py` and the locator is
`<oracle>.unreached[<construct>]`. `impls` is `(reference, selfhost)`; `breadth`
is 1, because `tools/selfhost_coverage.py::corpus_documents(tier)` returns one
directory under `tests/fixtures/`. Rung: medium.

Slice 2 reads the two oracles no emitter table covers, `compile` and
`gate_census`, through the same report and the same shrink-only ledger. They
were skipped in slice 1 because `compile` reported 5 of 5 constructs unreached
at `c136eae6`: `_ir_reach` only records `key=value` pairs whose value is a
string, so five top-level IR section names could never be reached by
construction, and that was a defect in the report rather than 5 tasks. Issue
#1203 fixed it (a section is reached when some corpus document's IR carries a
non-empty value for it, `_section_reach`), and `gate_census` now measures the
census's guarantee vocabulary against the documents its own engine refuses.
At the slice-2 commit `compile` leaves 1 construct unreached (`tests`, a section
the native lowerer does not emit) and `gate_census` leaves 6 tags (`BOOT`,
`FOLD`, `HANDOFF`, `HOST-ARITY`, `ROUTE`, `SPAWN`). Each is one task,
`reach/<oracle>/<construct>`, and each task quotes the ledger's written
disposition, so the doer does not re-derive a decision the tree already holds.

The rung is read off each oracle's own corpus, not declared for the slice. The
`compile` corpus is one directory under `tests/fixtures/`: breadth 1, medium.
The `gate_census` corpus is the census's eight directories: breadth 8, hard,
which is where `tier` puts a mechanism that makes two implementations agree
over documents the author did not choose. The design expected medium for both;
the measurement says otherwise for the second, and the ladder is a function of
the measurement. A report that stops naming either oracle is an error, not an
empty row, so `check` names the source rather than reading it as finished.

We consume `survey()` rather than the CLI text, so the shrink-only ledger that
issue #1203 is concurrently adding to the same tool does not move this adapter.

### 3.2 `census-bypass` (source: known historical bugs) -> hard, 9 tasks

`tools/gate_reference_census_baseline.json` records, per bucket, the programs
where two engines disagree. `false-admit/<tag>` is a program the reference
refuses and the self-host admission engine raises no objection to: a recorded
defect with the defective program already in the tree, which is the strongest
derived task available here. Nine at `c136eae6`, six `false-admit/T1` and three
`false-admit/TYPE`.

The artifact is the program itself (`examples/rejections/t14_optional_chain_
on_nonoptional.rvl` and its eight siblings); the locator is the baseline key.
`impls` is `(reference, selfhost)` from the bucket head, `breadth` is 8. Rung:
hard, which is the review's own placement for self-host parity.

`false-admission` is handled and yields three implementations, hence expert.
It has no members at `c136eae6` and is in the census's `NEVER_BASELINED`, so
that arm is written and unexercised. It is named here rather than omitted so a
member the day it appears carries the right rung.

### 3.3 `unexplained-refusal` (source: the tree's own programs) -> easy, 7 tasks

`src/revl/diagnostics.py` is the agent-facing projection of a refusal. Its
`explain(code)` answers `{"ok": False, "message": "no diagnostic code ..."}`
for anything outside `GUARANTEES`, and `classify` attaches neither `guarantee`
nor `fix`. So a code the reference *stamps* and `GUARANTEES` does not hold is a
refusal an agent receives and cannot look up.

"The reference stamps code C" is derived from the raise sites by AST, never
from a list kept in the generator: a `RevlError(..., code="C")` keyword on the
constructor call, or a `(C)` guarantee tag inside a string literal that is part
of a `raise` statement, which is the convention `diagnostics.py`'s own `_TAG`
reads back out of the message. Both are narrow on purpose. An earlier, looser
scan that took any `code=` keyword picked up `route=`, `session=`, `method=`
and `request=` in `src/revl/mcp/http_face.py` as refusal codes, and a scan that
took any `(C)` in any line picked up seven codes that appear only in prose. A
task derived from a docstring is the invented task this module exists to
exclude, so both were tightened until every surviving code has a real
constructor or raise site.

Seven survive: `HOST-ARITY`, `HOST-METHOD`, `L1`, `L4`, `R2`, `R4`, and
`lifecycle`. One implementation owns the answer: no port, no crate and no proof
has to be taught anything, which is `len(impls) <= 1` and the review's own
example for easy.

### 3.4 `formal-obligation` (source: formal obligations) -> expert, 6 tasks

A code the reference enforces (same derivation as 3.3), that
`diagnostics.py` calls a guarantee, and that no `.lean` file under `formal/`
so much as *names*, is a guarantee with a checker and no model.

Naming is a deliberately weak test in the direction that matters: any mention
in any `.lean` file excludes the code, so the adapter under-reports rather than
claiming a proof is missing when one exists. `G-SECRET` and `G-SECRET-FLOW` are
excluded exactly this way. `formal/RevL/Lemmas/TaintLemmas.lean` names them,
although no file under `formal/RevL/Theorems/` does, and an adapter that looked
only at `Theorems/` would have reported two false gaps.

Six survive at `c136eae6`: `A1`, `G-RETAIN`, `T-UNRESOLVED`, `T1`, `T2`, `T3`.
`proved` is true, so the rung is expert.

### 3.5 `cross-tier-probes` (source: cross-tier differential probes) -> hard

Slice 3. The lower oracle's `REJECTED_PROGRAMS` is curated to be in slice:
every row there is a program whose reference refusal `selfhost/lower.rvl`
decides with the same tag and message, so no row is a gap. The census records
the complementary set, programs the reference refuses and the port raises NO
OBJECTION to, which it files under `no-objection-out-of-slice`. Those are the
differential probes the lower oracle cannot carry yet, and teaching the port
the refusal family, then adding the `REJECTED_PROGRAMS` row, is the task.

The programs are read from the committed census record under
`docs/census-artifact/` (what `tools/census_artifact.py --verify --strict`
holds current), not from a fresh census run: a full census over the eight
directories costs more than the whole curriculum does today. Only programs
under `examples/rejections/` are taken: that directory exists to be refused, so
the reference verdict is a probe. `stdlib/` and `selfhost/` members of the same
bucket are single-source compiles that merely lack a `use`d module, and
teaching the port those refusals would teach it nothing.

At the landing commit 20 programs are in the bucket: the `a2`/`a9`, `g5`,
`g9`, `gsecret`, `lifecycle` and `service_compat` rejection families. Three
more rejection fixtures (`v2_use_cycle`, `v2_use_missing`, `v2_use_private`)
sit in the same bucket but are excluded, because the census compiles an
in-memory source and refuses them only because it was given no `modules=`. That
is a limit of the harness, not a refusal the port should learn, and the lower
oracle's own `_ref` uses the same in-memory call, so no `REJECTED_PROGRAMS` row
can carry them. The artifact is the program, the locator is the record's bucket
entry, `impls` is `(reference, selfhost)` and `breadth` is the census's eight
directories, so the census is the mechanism the task names. Rung: hard. A task
is emitted only if the reference still refuses the program, so a stale record
cannot invent one, and a missing record directory returns no signal so `check`
reds on the source instead of reading `hard` as empty.

### 3.6 `design-docs` (source: design documents) -> varies with the oracle named

Slice 4. A design document states its own acceptance, and the slice table is
where it does so most precisely: a row says which oracle decides the slice. A
row that names a test under `tests/` which is not in the tree is a stated exit
with no mechanism that verifies it, and the task is to write the oracle the
document already names.

Only a path under `tests/` in a slice table's own row counts, where a slice
table is one whose header opens with `Slice`, `Step`, `Phase` or `Stage`. A path
in prose or in a different kind of table is not a promise a slice plan makes: of
the two table rows in `docs/design/` that name a missing test file at this
commit, only one is in a slice table. `456-ambient-and-prog-export.md` slice B
names `tests/test_selfhost_ambient.py`; `backend-runtime-fork-status-2026-09.md`
names `tests/test_reentrancy.py` in a commit table about another repository, and
taking it would be inventing a task (`test_a_path_outside_a_slice_table_is_not_
a_design_task` is the negative control).

The rung varies with the oracle named, and an oracle that does not exist has no
corpus to read, so `_oracle_signal` reads it off the path by the naming the
tree already uses: a `test_selfhost_*` file compares `src/revl` with
`selfhost/*.rvl` (two implementations), a `crate` or `native` file adds the
gate crate, and any other file is one implementation checking itself. Breadth is
1, because one test file is one curated corpus. This is an approximation, stated
as one: it is a total function of the path rather than a table of documents, and
the written oracle's real corpus replaces it. Slice B of design 456 therefore
lands on medium. Every named oracle declares its signal whether or not the file
exists, so a tree with no slice gap left is an empty reachable rung and not a
source that stopped reading, and a missing `docs/design/` is an error.

### 3.7 `tracker` (source: the tracker) -> varies with the verifier an issue names

Slice 6, and the source that would need the network. The curriculum has to stay
computable in a checkout with none, so it never calls GitHub. `tools/tracker_
snapshot.py --write` is the only place that does: it reads the open issues once
and commits what a task needs to `tests/fixtures/tracker_snapshot.json`, and
everything downstream reads the file. Per issue it keeps the number, the title,
the labels, and `verifiers`, the in-tree paths under `tests/` and `tools/` the
body names, in order of first appearance. The body itself is not kept: a task
derives from the artifact a verifier names, not from prose, and a snapshot that
quoted bodies would be a second copy of the tracker that rots against it.
`--check` validates the file's shape offline and
`test_the_tracker_source_never_touches_the_network` makes `subprocess` and the
socket layer refuse for the whole derivation.

An open issue is a task when it names a verifier that is in the tree: that is
the issue saying which mechanism decides it is done, which every task here has
to name. An issue that names none, or only files that are gone, is a task nobody
can verify, and deriving one would invent the verifier. At the slice-6 commit
the snapshot holds 321 open issues and 67 of them name an in-tree verifier. The
rung is that verifier's, by the same reading slice 4 gives an oracle
(`_oracle_signal`); the issue's labels say nothing about how many
implementations must agree and are not read. That reading puts 50 of the 67 on
easy (a `tools/` script or a test with no self-host in its name) and 17 on
medium, which makes this the largest source on the easy rung; section 5.4's
point stands, the population is a measurement of the tree and nothing here makes
it balanced.

What an offline snapshot cannot know is stated, not hidden: an issue closed
after it was taken is still a task until the next `--write`. A missing snapshot
is an error, and a snapshot whose named files have all gone is an empty
reachable rung.

## 4. Non-vacuity: the tasks, and the gate

### 4.1 The tasks

A generator that emits tasks nobody can fail is worthless, so every rung's gap
is re-derived in `tests/test_evolve_curriculum.py` **by a different route than
the generator uses**:

| rung | the generator's route | the test's independent route |
|---|---|---|
| easy | an AST scan of `src/revl/**` for raise sites | calls the shipped `revl.diagnostics.explain(code)` and asserts `ok` is False with `no diagnostic code` |
| medium | `survey()["unreached"]` | re-runs `survey()` and, for each task, reads the dispatch value straight out of `backends/<tier>/emit.py` |
| hard | the census baseline JSON and the census record's out-of-slice bucket | compiles the named program with `revl.compile_files` and requires a `RevlError`, and for a probe re-reads the census record it was derived from |
| expert | an AST scan plus a scan of `formal/**.lean` | a plain word-boundary grep over `formal/**.lean`, which is what a reviewer would do |

and `test_the_non_vacuity_assertions_would_reject_an_invented_task` is the
negative control: the same three predicates are run against three gaps the tree
does **not** exhibit (`explain("G1")` answers; `formal/` names `G1`;
`examples/counter_pair.rvl` compiles), each is rejected, and none of the three
is in the curriculum.

Worked examples, one per rung, all checkable by hand at `c136eae6`:

```
[easy]   diagnostic/R4
         artifact src/revl/deploy.py (line 2471)
         gap      the reference stamps refusals with the code 'R4' and
                  src/revl/diagnostics.py's GUARANTEES table does not hold it
         verify   revl.diagnostics.explain("R4")["ok"]

[medium] reach/emit_go/case=Err
         artifact tools/oracle_construct_reach.py (emit_go.unreached['case=Err'])
         gap      no document in the emit_go corpus reaches the reference
                  dispatch case=Err in backends/go/emit.py
         verify   tests/test_selfhost_emit_go.py

[hard]   census/false-admit/T1/examples/rejections/t17_arrow_body_unchecked.rvl
         artifact examples/rejections/t17_arrow_body_unchecked.rvl
         gap      the reference refuses it with T1 and selfhost/lower.rvl
                  raises no objection
         verify   tools/gate_reference_census.py --check

[expert] formal/A1
         artifact src/revl/lower.py (line 9252)
         gap      the reference enforces A1, diagnostics.py calls it a
                  guarantee, and no .lean file under formal/ names it
         verify   formal/scripts/run_gate.sh
```

### 4.2 The gate

This repository has measured five separate checks that ran on every PR and
could not fail. `--check` is not allowed to be the sixth, so it is shown red on
the real code path in two independent directions, with a control that is green
on the same tree:

- `test_check_reds_when_a_rung_is_unreachable` restricts the curriculum to
  `unexplained-refusal` alone and asserts exactly three problems, one naming
  each of `medium`, `hard` and `expert`;
- `test_check_reds_on_a_source_that_ran_and_measured_nothing` and
  `test_check_reds_on_a_source_that_could_not_be_derived` take the two ways a
  source stops reading the tree;
- `test_check_reds_on_a_task_whose_artifact_left_the_tree` rewrites one task's
  artifact to a path that is not in the tree and asserts the gate names it,
  which is the failure mode of an invented task;
- `test_cli_check_exits_zero_and_the_restricted_run_exits_one` does both
  through the CLI, since that is how a gate is actually invoked;
- `test_check_is_green_on_the_whole_curriculum` is the control.

### 4.3 An empty rung, and what it is allowed to mean (issue #1410)

The first version of the gate red on an empty rung. That was right while a
rung described work the tree still had, and it became wrong the day the work
was finished. `hard` is derived from the census's recorded gate bypasses; PR
#1396 met item 391's exit and PR #1404 took the go carried set from 16 to 0,
the baseline went to `{}`, and the rung emptied because the bypasses were
closed. A gate that fires on the repository improving has exactly one cheap
way to be cleared, which is to re-open a closed gap, and that is the trade
issue #1407 forbids for the census baseline.

Widening the rung's definition was the other option and it is worse. The rung
is a total function of a measured signal, and moving the line so the bucket
stays full is calling something hard because the ladder wants it to be, which
is the asserted-difficulty problem this module exists to remove.

So the two halves are measured separately. Each source reports the SIGNALS it
stamps, computed from the artifacts it reads and independent of how many
instances it finds, and the tasks it found. What is gated is reachability: a
rung no source stamps is a decorative rung, which is the real content of the
review's "a tier that cannot be populated is a RED", and it still needs a new
source rather than a shorter ladder. What is reported is population, with the
source that feeds the rung named beside the count.

Three states, three outputs, and no two of them the same:

| state | output | exit |
|---|---|---|
| a source raised, or ran and measured no signal | `CURRICULUM-RED source 'x' ...` | 1 |
| no source stamps a rung | `CURRICULUM-RED tier 'x' is unreachable at HEAD` | 1 |
| a source reaches a rung and found nothing | `CURRICULUM-EMPTY tier 'x' has 0 tasks at HEAD, and is reachable: ...` | 0 |

The middle row is what a renamed artifact used to look like. An adapter whose
subject file has moved skips every branch and returns nothing, which under the
old gate printed the same line as a rung whose work was done; now a source
that measured no signal is named on its own line and the rungs it fed red as
unreachable rather than passing as finished.

`test_a_recorded_bypass_fills_the_hard_rung_and_removing_it_empties_it` is the
demonstration: two programs the reference genuinely refuses are staged into a
baseline the test owns, `hard` fills with them and `check` is green; the
entries are taken away, the rung empties, `census-bypass` still declares it
reaches `hard`, and `check` is green for the other reason. Nothing is written
into the tree and no closed gap is re-opened.

## 5. What the signal gets wrong, and how that was measured

Three separate misses, each measured rather than guessed.

### 5.1 It does not predict how large a change is, and was never meant to

Median commit size for the subjects behind each rung, over the full history
(3714 commits at `c136eae6`, `git log --no-merges --numstat`):

| rung | subject | commits | median files | median lines |
|---|---|---|---|---|
| easy | `src/revl/diagnostics.py` | 16 | 7 | 522 |
| easy | `src/revl/formatter.py` | 13 | 9 | 575 |
| medium | `backends/go/emit.py` | 104 | 8 | 561 |
| medium | `backends/java/emit.py` | 112 | 9 | 413 |
| hard | `selfhost/lower.rvl` | 87 | 9 | 622 |
| hard | `selfhost/checker.rvl` | 20 | 8 | 547 |
| expert | `formal/RevL/Theorems/` | 22 | 8 | 714 |
| expert | `crates/revl-gate/src/lib.rs` | 17 | 13 | 2218 |

The ladder is flat against diff size everywhere except the native crate, whose
2218-line median is mostly regenerated crate bytes rather than authored work.
The same is true of co-change span, the number of top-level directories a
commit touching each subject also touches: 3.25 and 3.54 for the easy subjects,
3.19 and 3.36 for medium, 3.51 and 3.25 for hard, 2.41 for `formal/Theorems/`
and 4.82 for the crate. Neither proxy separates the rungs.

So the honest statement is that the ladder was **not validated against a
historical difficulty proxy**, because this tree offers none that works. It is
a measurement of how many implementations must agree, which is a different
quantity from how much typing a change takes, and a reader should not read one
as the other.

### 5.2 `formal/` is the rung the history most contradicts

`formal/RevL/Theorems/` has the *lowest* co-change span of all eight subjects
measured (2.41 mean, against 3.2-3.5 for everything else) and an unremarkable
median size. By both historical proxies a formal change is the most contained
kind of change in this repository, and the ladder ranks it top.

The `proved` dominance rule is therefore an assumption carried over from the
review, not something measured here. The argument for it is that a proof
obligation cannot be discharged by adding a fixture, which is a statement about
what the work *is* rather than about what past commits looked like. It is the
weakest line in `tier`, and the one to revisit first if the ladder is
recalibrated.

### 5.3 The parser case: the review is right and the stated reasoning was wrong

The review puts parser additions on the easy rung. Applying the signal to
`src/revl/parser.py` gives `impls = (reference, selfhost)` via
`tests/test_selfhost_parser.py` and therefore medium, and the reasoning for
that was "a parser production here has to be taught to `selfhost/parser.rvl` as
well". History does not support it: of the 125 non-merge commits touching
`src/revl/parser.py`, 10 (8%) also touch anything under `selfhost/`, and 8 (6%)
touch `selfhost/parser.rvl` or `selfhost/lexer.rvl`. Ninety-two percent of
parser changes in this repository never touch the port.

The mechanism explains it. `tests/test_selfhost_parser.py` is a differential
oracle over a corpus: it reds only when a corpus *document* exercises the new
production, so most parser additions do not force the port at all, and item
391's backlog is where the lag accumulates. The right reading is that `impls`
should count implementations whose oracle would genuinely re-decide the task,
which is true for the construct-reach tasks (the task *is* to make a document
reach the dispatch, after which both engines are compared) and false for a
hypothetical "add a production" task.

No adapter emits parser tasks today, so no shipped task is mis-tiered by this.
It is recorded because it is the concrete case where the signal would be wrong,
and because a later slice that derives tasks from the parser must fix `impls`
before it does.

### 5.4 Smaller things, stated rather than hidden

- The medium rung is 244 of 266 tasks. The distribution is not balanced and
  nothing here makes it balanced; the population is a measurement of the
  corpus, and at `c136eae6` the corpus is mostly unreached emitter dispatches.
- `lifecycle` is a real `RevlError(code="lifecycle")` in `src/revl/lower.py`
  and is emitted as an easy task on those grounds. It is a lowercase code
  unlike its six siblings, and it may be that the right fix is to rename it
  rather than to describe it. The task says what is true (`revl explain
  lifecycle` answers `no diagnostic code`) and leaves which fix to the doer.
- `breadth` is computed on the mechanism's corpus, not on the task's change
  site. Two tasks verified by the same mechanism always carry the same breadth,
  which is intended (the mechanism is what re-decides them) but means breadth
  cannot distinguish two tasks inside one oracle.

## 6. Slice plan

Slices 1, 3, 4 and 6 are landed. Each later slice states the source it adds, the rung
Slices 1, 2 and 3 are landed. Each later slice states the source it adds, the rung
it is expected to land on, and the oracle that would catch it going vacuous.

| slice | source added | expected rung | fixture / oracle |
|---|---|---|---|
| 1 (landed) | known gaps, known historical bugs, the tree's own programs, formal obligations | medium, hard, easy, expert | `tests/test_evolve_curriculum.py`, 16 tests, gate firing proved both directions |
| 2 (landed) | the `compile` and `gate_census` oracles of `tools/oracle_construct_reach.py`, read through the same report and shrink-only ledger once issue #1203's `_ir_reach` defect was closed (section 3.1) | medium (`compile`), hard (`gate_census`) | the committed ledger `tests/fixtures/oracle_construct_reach_ledger.json`; a report that drops either oracle is an error |
| 3 (landed) | cross-tier differential probes: the `examples/rejections/` programs the census parks in `no-objection-out-of-slice`, which the lower oracle's `REJECTED_PROGRAMS` cannot carry yet | hard | the committed census record; `tests/test_selfhost_lower.py` for the oracle, and a task must name a program the reference refuses and the port does not (section 3.5) |
| 4 (landed) | design documents: a slice table row in `docs/design/*.md` whose named oracle does not exist in `tests/` | varies with the oracle named (medium for slice B of design 456) | the doc's own slice table; the gap is checkable by looking for the test file (section 3.6) |
| 5 | generated adversarial programs: `tools/fuzz_frontend.py` seeds that survive to a divergence | hard | the fuzz corpus; a seed is only a task while it still diverges |
| 6 (landed) | the tracker | varies with the verifier an issue names | an offline snapshot only, so the curriculum stays computable in a checkout with no network (section 3.7) |
| 7 | mutation testing, which the issue is right that this tree does not have | expert | see section 7 |

## 7. Mutation testing is absent and stays absent in slice 3

The issue is precise about this and the check still holds at `c136eae6`:

```
$ git grep -liE 'mutation testing|mutmut|cosmic.ray|mutate the (suite|tests)' -- tools/ tests/ | wc -l
0
```

`tools/fuzz_frontend.py` mutates programs as *inputs* (`_MUTATION_CHARS`), to
find a checker that faults on a program nobody wrote. Mutation testing mutates
the *subject* and asks whether the suite notices. They fail on different things
and only the second is a difficulty signal, because it measures whether the
suite that scores a candidate is sensitive enough to be worth scoring against.

Slices 1, 3, 4 and 6 do not add it, and do not pretend to. It is slice 7 because it is
Slices 1 to 3 do not add it, and do not pretend to. It is slice 7 because it is
the one source whose machinery does not exist, and because it interacts with
issue #1207: a mutation test a candidate can read is not a hidden mutation
test, so where the mutants live is that item's question and not this one's.

## 8. What this does not claim

- It does not claim the four rungs are equally hard, or that the ladder is
  calibrated against anything. Section 5 is the measurement, and it is
  negative on both proxies the tree offers.
- It does not claim to read all eight sources. It reads seven. The other one is
  slice 5 (fuzz seeds), with a named oracle; slice 2 is an extra oracle, not one
  of the eight.
- It does not claim to read all eight sources. It reads five. The other three
  sources are slices 4 (design documents), 5 (fuzz seeds) and 6 (the tracker),
  each with a named oracle. Slice 2 reads two more oracles of a source already
  counted, so it adds tasks and no source.
- It does not claim the derived tasks are a complete list of what is wrong with
  the tree. They are the gaps seven artifacts exhibit, no more.
- It does not touch the reward (issue #1206) or the held-out scoring set
  (issue #1207). A curriculum task names a mechanism that verifies it; whether
  that mechanism is one the candidate could read is #1207's question, and every
  mechanism named in slice 1 is in the tree and readable, which #1207 is
  entitled to change.
