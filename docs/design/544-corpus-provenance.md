# 544. Corpus provenance: measuring the independence the oracle spends

Roadmap item 542, issue #1221.

## The property being protected

`src/revl/*.py` and `selfhost/*.rvl` are two implementations held to
byte-agreement. Item 146 is worth its cost because the two were written
independently: agreement between two implementations is evidence in proportion
to how independent they are, and agreement between an implementation and a
restatement of itself is evidence of nothing.

Three items already guard the corpus against a candidate that cheats. Item 535
(#1205) derives tasks from repository artifacts instead of letting the loop
invent them. Item 536 (#1206) computes the reward from artifacts instead of the
candidate's prose. Item 537 (#1207) withholds a scoring set the candidate
cannot read.

None of them addresses the compounding one. Every generation writes
model-authored code into the tree, and that tree is at the same time the next
generation's training corpus, its differential oracle and its scoring set.
Independence is not a property the loop preserves. It is a stock the loop
spends.

## Why the spend is invisible today

Two mechanisms make it so, and both are good properties otherwise.

`tests/fixtures/emit_*_corpus/` is globbed, so a document dropped anywhere
under it enters every byte-agreement projection with no list to edit.
`CORPUS_DIRS` in `tools/gate_reference_census.py` is walked with `rglob` over
eight directories, which is how the census reaches 848 programs without anyone
maintaining a list. Both mean a generated document is indistinguishable from a
hand-written one at the point where it is counted as evidence.

The failure is not that a model writes a corpus document. It is that the census
could not report what fraction of the evidence behind "the gate agrees with the
reference" is evidence the engine authored about itself.

## Where provenance lives

In one sidecar, `tests/fixtures/corpus_provenance.json`, which lists document
names under the generation that authored them. Four alternatives were
considered and each gets something wrong.

**Git history, reading the introducing commit's author.** Attractive, and wrong
here twice over.

The first weakness is the one that is easy to state: this repository
squash-merges. A document added on a branch has an introducing commit that is
the squash, dated and authored at merge time, and a later rebase, squash or
reformat rewrites it again, so `git log --diff-filter=A` does not answer a
stable question. A provenance field that changes when somebody rebases is not a
provenance field.

The second weakness is fatal and specific to this tree: the signal was
deliberately erased. Every commit in this history is authored under a human
identity by policy. Measured on `origin/main` at `d9816ee5`, the commits that
added a `.rvl` document under `tests/fixtures/emit_py_corpus/` carry exactly
two author identities, both human, both the repository owner:

```
$ git log --diff-filter=A --format='%an' -- 'tests/fixtures/emit_py_corpus/*.rvl' \
    | sort | uniq -c
  28 <the repository owner>
   1 <the repository owner, second spelling>
```

A field that returns the same value for every input is not a measurement. Git
history cannot separate the two populations here even in principle, and the
reason it cannot is a policy nobody intends to change.

**A comment header in each document.** It changes the bytes of documents whose
whole job is byte-agreement, so every emit golden on six tiers is rewritten to
record a fact about authorship. The claim that this is harmless rests on six
emitters stripping comments identically, which is an assumption and not a
checked one, and checking it is a larger job than the one being done. The
header also rots in the direction that matters: a document copied from a
generation-zero document inherits its header, and nothing cross-checks a
header against anything. It is a ~850-file diff whose result is self-reported.

**A directory convention**, `.../gen1/foo.rvl`. Provenance would live in the
path, and the path is the identity every baseline and ledger in this tree keys
on: `tools/gate_reference_census_baseline.json`,
`tests/fixtures/oracle_construct_reach_ledger.json`, `LOWER_GAP_DOCS` and
`NATIVE_GATE_GAPS` in `tests/test_selfhost_compile.py`. Correcting a
provenance claim would then rewrite records that have nothing to do with
provenance, and the correction would be indistinguishable in the diff from a
corpus change. It also cannot express a hand-written document that a later
generation substantially rewrote.

**Nothing, and ask the candidate.** The reward becomes a function of the
candidate's prose about itself, which is what item 536 removed everywhere else.

The sidecar's own weakness, stated rather than hidden: nothing in the
filesystem forces an entry to exist, so it can go stale. That is what
`tools/corpus_provenance.py --check` is for, and it is why the failure
direction below is the load-bearing part of the design rather than a detail.

## The failure direction: undeclared is not hand-written

A provenance field that defaults to "hand-written" when unknown is the
fail-open shape and it makes the whole measurement a lie. Under that default,
dropping an undeclared document into a globbed corpus would raise the measured
independence, and the one action the mechanism exists to notice would be the
action that improves the number.

So an undeclared document resolves to `UNDECLARED`, which:

- counts as model-authored at every threshold, including thresholds above any
  generation the tree has reached;
- is a named error of its own under `--check`, not merely a term in a fraction.

Adding a corpus document therefore costs one line in one file, and forgetting
it makes the tree look worse rather than better.

The resolution is deliberately **not** "the current generation".
`current_generation` is 0 today, so resolving an unknown to it would be
indistinguishable from resolving it to hand-written, and the fail-closed
property would silently not hold until the loop had run once, the worst
possible time for it to start holding.

One more vacuity arm, one tier up: an **empty** scoring corpus crosses every
floor above zero. A corpus that vanished is not a corpus that passed, and `0 of
0` is the reading under which a ratchet over fractions goes quiet.

## Generation zero is a declaration, not a measurement

The generation-zero set records the tree as it stood before this mechanism
existed. No mechanism proves those 848 documents were hand-written; the claim
is asserted by the operator who recorded them, once, in a diff. Saying
otherwise would be the same self-report this item exists to replace.

What the mechanism holds from here on is different and checkable: every
document that arrives must name its generation, the set can only change in a
diff somebody reads, and the fraction is computed from the names and the tree
rather than claimed.

## Names only

The manifest records document names and nothing else. No counts, no fractions,
no line numbers. This is PR #1203's discipline on its shrink-only ledger, for
the same reason: a derived number in a recorded artifact rots against the thing
it was derived from, and a count written under one python is a diff under
another. Verified the same way #1203 verified it, by writing the manifest under
a real python 3.11 (3.11.16) and comparing bytes with the file written under
the dev python 3.14:

```
py3.11 rewrite sha: 01960cbfc97062a148409361e7196a051c4dd5b90fdd8702096877874ffdf5f5
committed     sha: 01960cbfc97062a148409361e7196a051c4dd5b90fdd8702096877874ffdf5f5
```

`tests/test_corpus_provenance.py::test_rewriting_the_manifest_reproduces_it_byte_for_byte`
holds the idempotence from here on.

## The scoring corpora and their floors

Eight named corpora, all enumerated by `tools/corpus_provenance.py`:

| corpus | what it is | programs | floor |
|---|---|---|---|
| `census` | every input `gate_reference_census.py --check` runs | 848 | 80% |
| `selfhost_oracle` | the programs written to sit on a guarantee edge | 291 | 90% |
| `emit_py_corpus` | the globbed python byte-agreement projection | 65 | 75% |
| `emit_ts_corpus` | typescript | 42 | 75% |
| `emit_rust_corpus` | rust | 41 | 75% |
| `emit_java_corpus` | java | 34 | 75% |
| `emit_wasm_corpus` | wasm | 22 | 75% |
| `emit_go_corpus` | go | 19 | 75% |

The floor is the minimum percent of a corpus that must remain independent of
generation 1 and up. **The values are an operator decision** and live in the
manifest, not in code. The reasoning behind the ones recorded: the oracle's
boundary programs carry the most evidential weight per document and are held
tightest at 90%; the per-tier emit corpora are the ones a loop grows fastest,
since a generation that lands a lowering lands a document that reaches it, so
they get the most headroom at 75%; the aggregate census sits between them.

The floor is evaluated **per corpus**, never in aggregate. A tier whose corpus
has gone model-authored must red even when the total across all eight is
comfortably above the floor, because the tier is what the byte-agreement claim
is made about.

The comparison is integer-only (`(total - model) * 100 < floor * total`). A
threshold compared through a float is a threshold whose edge case depends on
the platform's rounding.

`census` and `selfhost_oracle` deliberately overlap the per-tier corpora. A
document can be in three of these at once, and that is correct: each corpus is
a claim somebody makes, and each claim gets its own floor.

## What the census reports

Every `tools/gate_reference_census.py` run now prints a second table: per
bucket, how many of that bucket's programs were model-authored at or after a
named generation (`--since-generation`, default 1), with an `ALL` line and an
undeclared count.

The table is **reporting only**. It adds no bucket, moves no verdict, and
neither `--check` nor `--record` reads it, so a provenance edit can never alter
the census's verdict about the gate. That is held structurally, not by comment:
`test_the_census_verdict_path_never_reads_the_manifest` parses the census and
asserts the manifest is reachable from exactly one function, `provenance_report`,
which only `main` calls. It is also confirmed by bytes: `--record` after this
change rewrites `tools/gate_reference_census_baseline.json` identically.

The gate is a separate tool, `tools/corpus_provenance.py --check`, running in
the `frontend` CI job beside `oracle_construct_reach.py --check` and
`selfhost_coverage.py --check`.

## Non-vacuity

This repository has measured eleven checks that ran on every PR and could not
fail. The demonstration that this one fires, on the real tree at
`tests/fixtures/emit_py_corpus/` (floor 75%, 66 documents with the probe):

| arm | corpus state | `emit_py_corpus` | `emit_go_corpus` (control) | exit |
|---|---|---|---|---|
| 0 | HEAD | 0/65, 100.0%, ok | 0/19, 100.0%, ok | 0 |
| 1 | one document dropped in, undeclared | 1/66, 98.4%, ok **(1 undeclared)** | 0/19, 100.0%, ok | **1** |
| 2 | that document declared at generation 1 | 1/66, 98.4%, ok | 0/19, 100.0%, ok | 0 |
| 3 | 16 of 66 at generation 1 | 16/66, 75.7%, ok | 0/19, 100.0%, ok | 0 |
| 4 | 17 of 66 at generation 1 | 17/66, 74.2%, **BELOW FLOOR** | 0/19, 100.0%, ok | **1** |

Arms 3 and 4 differ by one document's declared generation and nothing else, and
they straddle the floor exactly. The control corpus is byte-identical in all
five arms and its verdict does not move, so the difference belongs to the
document whose generation changed rather than to the harness.

Arm 1 is the arm that matters most in practice. It is the live guard: the
declaration requirement fires on the next PR that adds a corpus document
without a manifest line, on real inputs, with no constructed tree. The floor
itself is a ratchet with declared headroom and will not fire until the loop has
run, which is why the demonstration above exists rather than a claim that it
would.

`tests/test_corpus_provenance.py` holds all five arms plus the fail-closed
resolution, the stale-entry direction, the missing-floor direction, the
empty-corpus direction and the integer comparison: 20 tests.

## What this does not do

- It does not detect a mislabelled document. An operator or a candidate that
  writes `generation 0` beside a generated document defeats the measurement,
  and no in-tree mechanism can catch that, for the same reason the census's own
  baseline can be re-recorded: the defence is that the edit is a line in a diff
  somebody reads. Stated plainly rather than papered over.
- It does not set the floor. The value is an operator decision; the tool holds
  whatever is written in the manifest.
- It does not cover non-`.rvl` scoring artifacts (goldens, IR fixtures, formal
  obligations). Those are downstream of the documents enumerated here, but they
  are a separate population and are not measured.
