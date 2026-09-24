# The gate/reference census

GENERATED FILE. Every number below comes from a run of
`tools/census_artifact.py`, which is the only thing that writes it. Do not
edit it by hand: `python3 tools/census_artifact.py --check` reports the
edit as drift. Regenerate with `python3 tools/census_artifact.py --write`.

## What is being measured

revl has two independently written implementations of its own semantics.
`src/revl/*.py` is the reference compiler. `selfhost/*.rvl`, compiled into
`crates/revl-gate`, is the self-host gate. This census runs both over the
same corpus and classifies every disagreement. Agreement means the same
TAG and the same MESSAGE, not merely the same verdict.

Distinct programs: **888**. Programs run: **894**.
Checker version: `GATE-CENSUS-1+378bb31fb76b`.
Engine: `selfhost`.
Run: `census-selfhost-71f2f8f1e6f9`.

The two numbers differ because 6 case ids reach
the corpus twice, from two entries that spell the same
name. They are run twice and counted twice, in `n` and in every
bucket below. The number to quote is the distinct one. The repeats,
named so the gap is checkable rather than asserted:

- `oracle-reject:a ternary with three undeclared arms names the condition`
- `oracle-reject:an index whose target and subscript are both undeclared names the target`
- `oracle-reject:an undeclared name left of an unmarked emission is still G1`
- `oracle-reject:an unmarked emission left of an undeclared name is still G4`
- `oracle-reject:both operands of a binary op are undeclared: the LEFT one is named`
- `oracle-reject:two unmarked emissions in one expression name the first (G4)`

Neither identity is a commit or a clock. `run` is a sha256 over the corpus
this run read; the checker version is a sha256 over the files that decide
what the census does. Every file name inside a digest is relative to the
checkout root, so both are recomputable from any clone, at any path.

## The claim, and why it is not the corpus size

The interesting property is not that the two agree over 888 programs.
It is that the bucket that matters **cannot be written**.

`false-admission` is an issued admission for a program the reference
refuses: the gate telling a host the reference said yes when it said no.
That bucket is in the census's `NEVER_BASELINED` tuple (`false-admission`),
and that has two consequences a re-record cannot undo:

1. `--record` drops it on the way into the baseline, so no run can produce
   a baseline that grants one tolerance.
2. `--check` fails on any member regardless of what the baseline says,
   including a baseline hand-edited to list it.

Every other benchmark is cooked by adjusting what counts as acceptable.
Here the adjustment is not available in the direction that matters. The
rest of this file is a table; that sentence is the artifact.

### The mechanism, driven rather than asserted

This tool does not take the paragraph above on trust. It hands both halves
of the mechanism a synthetic member and records what they did.

| probe | what was driven | outcome |
|---|---|---|
| record | `record_payload` over a census whose only finding is `census-artifact-probe:synthetic-false-admission` | dropped, not written |
| check | `compare` against a baseline that lists that member | refused |
| baseline | committed baseline scanned for a never-baselined key | none present |

The refusal, verbatim:

```
FALSE ADMISSION (issued admission the reference refuses) false-admission: census-artifact-probe:synthetic-false-admission
```

Mechanism holds this run: **yes**.

### Observed, and why it is not a vacuum

`false-admission` members this run: **0**.

An empty bucket proves nothing if the gate never admits anything, which
is what the state looked like before the admission arm opened. So the arm
is measured for non-vacuity too: the gate ISSUED **6**
admissions over this corpus, and the reference admits every one of them.

## The standing false-admit allowance

`false-admit` is the other direction: the reference refuses under a
guarantee the gate claims to decide, and the gate raises no objection. It
is a bypass, it is baselined, and it is published as it stands, with
every residual named.

Two numbers, not one. The first is what this run MEASURED. The second
is what `tools/gate_reference_census_baseline.json`
RECORDS. Publishing only the first would let a non-empty baseline read
as an empty allowance, because a baselined
member that stopped diverging leaves the measured list empty while the
committed file still grants it tolerance. So both are here, and so is
every name on which they differ.

Measured this run: **0**. Recorded in the baseline: **0**. Capped by name in `tests/test_gate_reference_census.py`.

No member on either side: the run measured none and the committed
baseline records none. The allowance is empty, not merely unreported.

The two agree.

A count would let this list churn unread. Names have to be edited, and
`--check` fails in BOTH directions: on a new member, and on a baselined
member that no longer diverges. The second direction is the one that
matters for honesty, because it is what stops a fix from quietly widening
the allowance instead of closing it.

## Corpus provenance

A benchmark whose corpus was written by the thing it grades is worth
nothing. `tools/corpus_provenance.py` names the generation that authored
every scoring document, from `tests/fixtures/corpus_provenance.json`. A
document with no entry resolves to UNDECLARED and counts as
loop-authored at every threshold, because the other default would make
dropping an unknown file into a globbed corpus RAISE the measured
independence.

Read the two columns below as two different questions, because they are:

- **loop-authored** is: produced by a generation of the self-improvement
  loop at or after generation 1. That loop has never run in this
  repository, which is the whole of why the column reads as it does,
  and it is the column the floors gate.
- **human-authored** is: declared in the manifest as typed by a person.
  Nothing here claims this corpus was written by people, and issue
  #1397 records the measurement that says it was not: of the 850
  generation-zero entries, 153 arrived on a branch named `agent/*` and
  132 more on a commit carrying an AI co-author trailer, both of which
  are lower bounds, and the repository's root commit carries one too.

| corpus | loop-authored | human-authored | total | independent of the loop | floor | undeclared | verdict |
|---|---|---|---|---|---|---|---|
| `census` | 0 | 0 | 894 | 100.0% | 80% | 0 | ok |
| `emit_go_corpus` | 0 | 0 | 19 | 100.0% | 75% | 0 | ok |
| `emit_java_corpus` | 0 | 0 | 34 | 100.0% | 75% | 0 | ok |
| `emit_py_corpus` | 0 | 0 | 66 | 100.0% | 75% | 0 | ok |
| `emit_rust_corpus` | 0 | 0 | 41 | 100.0% | 75% | 0 | ok |
| `emit_ts_corpus` | 0 | 0 | 42 | 100.0% | 75% | 0 | ok |
| `emit_wasm_corpus` | 0 | 0 | 22 | 100.0% | 75% | 0 | ok |
| `selfhost_oracle` | 0 | 0 | 328 | 100.0% | 90% | 0 | ok |

## Full bucket table

| bucket | count |
|---|---|
| `agree-admit` | 480 |
| `agree-refuse/G4` | 77 |
| `agree-refuse/T1` | 71 |
| `agree-refuse/G1` | 57 |
| `no-objection-out-of-slice` | 54 |
| `agree-refuse/A1` | 40 |
| `agree-refuse/G6` | 21 |
| `refuse-out-of-slice/FOREIGN` | 21 |
| `agree-refuse/TYPE` | 17 |
| `refuse-out-of-slice/BAD` | 10 |
| `agree-refuse/PRELUDE` | 7 |
| `agree-refuse/ROUTE` | 5 |
| `agree-refuse/A6` | 4 |
| `agree-refuse/G2` | 4 |
| `agree-refuse/HOST-METHOD` | 4 |
| `agree-refuse/SPAWN` | 3 |
| `frontier-declined` | 3 |
| `refuse-out-of-slice/G1` | 3 |
| `agree-refuse/G3` | 2 |
| `agree-refuse/G8` | 2 |
| `agree-refuse/HANDOFF` | 2 |
| `agree-refuse/MODEL` | 2 |
| `agree-refuse/T2` | 2 |
| `agree-refuse/BOOT` | 1 |
| `agree-refuse/COUNCIL` | 1 |
| `refuse-out-of-slice/G4` | 1 |

## Reproduction

The fast engine is a python mirror of the native gate's guards, so it
could in principle be wrong in the same direction as the thing it
mirrors. The reproduction asks the REAL crate, built by cargo, and is
recorded in `tests/fixtures/census_crate_reproduction.json` because it needs a rust toolchain
and minutes rather than seconds.

- programs: **894**
- tracked buckets agree: **yes**
- crate `false-admission` members: **0**
- recorded at checker version: `GATE-CENSUS-1+378bb31fb76b`
- current for this run: **yes**

What it does not establish: The crate is BUILT from selfhost/lower.rvl by tools/build_gate_crate.py, so it is not a second specification: it is the same rvl source through a different emitter, toolchain and runtime. What this reproduction rules out is the fast engine's python mirror of the native guards being wrong. The independence that carries the census is the OTHER axis, src/revl against selfhost/, and it is in the measurement rather than in this reproduction.

## Running it yourself

No account, no key, no hosted service. From a checkout:

```
python3 -m venv .venv && .venv/bin/pip install -e .
.venv/bin/python tools/gate_reference_census.py --check
.venv/bin/python tools/corpus_provenance.py --check
.venv/bin/python tools/census_artifact.py --check
```

Those three need nothing but python and take seconds. The crate engine,
`tools/gate_reference_census.py --engine crate --check`, additionally
needs cargo, builds the shipped gate, and is the slow half.

### Checking the numbers in this file rather than trusting them

`--check` re-runs the census and compares every number here against it,
so a passing `--check` in your own clone is the whole verification: the
table is yours, not ours. It exits 1 and names each field that moved.
Nothing in the digests depends on where you cloned to, so the values
below are the values you should get.

| number here | what recomputes it |
|---|---|
| distinct programs, 888 | distinct case ids from `load_corpus` in `tools/gate_reference_census.py` |
| programs run, 894 | the length of the same list, repeats included |
| run `census-selfhost-71f2f8f1e6f9` | sha256 over every `(case id, source)` the run read, ids repo-relative |
| checker version `GATE-CENSUS-1+378bb31fb76b` | sha256 over the 5 files in `census.checker_sources`, each listed there with its own sha256 |
| `src/revl@sha256:63ca4121d9ff` | sha256 over `src/revl/**/*.py` |
| every bucket count | `tools/gate_reference_census.py --json out.json` |
| the false-admit allowance | `tools/gate_reference_census_baseline.json`, which is in the tree |
| the provenance columns | `tools/corpus_provenance.py` over `tests/fixtures/corpus_provenance.json` |

The corpus is the repository. There is no download, no server and no
hosted copy to go stale against this one: the programs the census runs
are the `.rvl` files in the directories named above plus the inline
program lists in `tests/test_selfhost_lower.py` and
`tools/gate_reference_census.py`, and `--json` writes out the per-case
classification if you want to audit an individual verdict.

## What this does not establish

- This report says nothing about any implementation other than revl's own two. It is not a comparison and carries no comparative claim.
- The gate covers one layer of a larger language. A `no-objection-out-of-slice` result is the reference refusing for a reason the gate does not decide, and is by design.
- Generation zero in the provenance manifest is a declaration about the tree as it stood, not a measurement. What holds from there on is that an arriving document must name its generation, and that an undeclared one counts as loop-authored.
- Generation zero does not mean a person typed it. Issue #1397 measured the generation-zero set against the commits that introduced it: 153 of 850 entries arrived on a branch named agent/*, 132 more on a commit carrying an AI co-author trailer, 415 on commits pushed to the trunk with no branch to read, and the repository's root commit carries such a trailer itself. Both signals are lower bounds. The honest reading is that this corpus is model-written and pre-loop, and the floors gate the second word, not the first.
- A mislabelled provenance entry defeats the provenance measurement exactly as re-recording the baseline would defeat the census. Neither is detected by a tool; both are edits in a diff somebody reads.
- The corpus holds 6 case ids that appear twice, so `n` (894) counts 6 programs twice and `n_distinct` (888) is the honest size. The repeats are named in the report. They are not deduplicated here: dropping one would move bucket counts and the recorded baseline, which is a change to the census rather than to the way it is reported.

## Schema

`docs/census-artifact.json` is the machine copy. It is an `EVAL-REPORT-1`
document under the frozen eval-honesty protocol
(`docs/design/478-eval-honesty-protocol.md`) and
`python3 tools/check_eval_report.py docs/census-artifact.json` passes on
it, which is a statement about over-claiming and not about correctness:
every public claim in it names the rung its own evidence reaches. The
census results live in its `census` section under `GATE-CENSUS-1`, which
that checker does not read.
