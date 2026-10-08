# The gate/reference census artifact

`tools/census_artifact.py` runs `tools/gate_reference_census.py`, which compares
the reference compiler (`src/revl/*.py`) with the self-host gate
(`selfhost/*.rvl` behind `crates/revl-gate`) over the same corpus, and builds a
publishable `EVAL-REPORT-1` report from the run. The argument for the artifact
is in `docs/design/560-census-artifact.md`. This page says what the repository
commits, how to get the report, and how to resolve a conflict.

This page carries no number on purpose. Every count lives in the report, and the
report is rendered from a run.

## What is committed

The repository commits the records of the last run, in `docs/census-artifact/`:

| file | holds |
|---|---|
| `cases/<case id>.json` | one file per program run: its case id and its buckets. The path mirrors the case id (`tests/fixtures/value_method_call/t1_x.rvl` is at `cases/tests/fixtures/value_method_call/t1_x.rvl.json`); an id that is not a clean path is escaped under `cases/_escaped/`, and `cases/README.md` states the rule |
| `pins.jsonl` | `[group, file]` for every file a verdict or the report depends on, one per line, sorted |
| `facts.json` | the engine, the admissions the gate issued, the reference faults, and the driven `NEVER_BASELINED` probe, one record per line |

Records are separated by a blank line, and nothing in these files is derived
from anything else in them or from the checkout. The sha256 of every program and
of every pinned file, the run id, the compiler digest, the checker version, `n`,
the bucket table, the claims and the whole markdown report are computed from the
records and the checkout when the report is rendered, so a published copy still
pins every input by digest.

That is what lets two pull requests merge. Before issue #1768 the repository
committed the rendered report itself, and every pull request that added a
program or touched a reference module rewrote the same aggregate lines, so each
landing left most open pull requests in conflict. Now a pull request that edits
a pinned module or a program without moving a verdict leaves the records alone,
and two pull requests conflict in `docs/census-artifact/` only when both moved
the verdict of the same program or changed which files the census run reads.
Each program has its own file, so two pull requests that add programs, even
next to each other, add different files.

## Getting the report

```
python3 tools/census_artifact.py                  # markdown, from a fresh run
python3 tools/census_artifact.py --json           # EVAL-REPORT-1 JSON, fresh run
python3 tools/census_artifact.py --from-records   # markdown, from the committed records
```

`--from-records` runs no census and takes seconds. It reads the digests from the
checkout, so it describes the run only where the records are current, which CI
holds on main; on a branch that moved an input, run `--write` first. It refuses
to render records that pin a file or carry a program the checkout does not have.
A copy published outside the repository is the `--json` output, and a reader
checks it with `--verify path/to/census-artifact.json`.

## The published edition

A reader outside this repository should not have to clone anything, so the
census is also published as a static edition at
<https://inso1337.github.io/revl/census-artifact/>. It is built by the `deploy`
job in `.github/workflows/pages.yml`, from the records above and the sha being
deployed, and it is not committed: it is a rendering, and a second copy in git
would be one more thing to keep in step. The step runs
`tools/census_artifact.py --verify --strict` before it writes, so a page whose
records do not reproduce is not deployed.

`tools/publish_census_artifact.py` renders it into `site/census-artifact/`. The
edition states `n`, the checker version and the standing allowance with each
residual named, and it carries the corpus provenance fraction beside them by
`tools/corpus_provenance.py`. It adds no claim this repository does not already
make: it is a copy of the records, so a reader checks it rather than believing
it. Download the `census-artifact.json` the edition serves and run

```
/tmp/revl-venv/bin/python tools/census_artifact.py --verify census-artifact.json
```

Three commands ask three different questions, and only the third is the verdict:

| command | the question it answers |
|---|---|
| `tools/census_artifact.py --moved-inputs` | given a diff, does it touch an input this census hashes? Exit 0 means yes. CI asks this to decide whether to run the slow check at all |
| `tools/census_artifact.py --check` | have the committed records drifted from what today's tree produces? Cheap, and it answers only that: a missing per-checker-version crate reproduction is not drift, and `--check` cannot see one |
| `tools/census_artifact.py --verify --strict` | the verdict: the records, the pins, the crate reproduction at this checker version, and the strict flags, all of them, on this tree |

That venv is outside the checkout on purpose. The census measures its inputs
through an audit hook on every file the run opens, so a `.venv` inside a clone
puts pytest's own modules into the pin set and `--check` then fails on
`pins.jsonl` over files that are the reader's environment and not the census.
Anywhere outside the clone works, and the edition's own page says the same.

## Keeping it current

CI's `census-artifact` job runs `python3 tools/census_artifact.py --verify
--strict` whenever a pull request moves an input of the artifact. It re-runs the
census and fails unless every committed verdict reproduces, no program is
missing, the run read exactly the pinned files, and each record file is byte for
byte what the run writes. The fix is the same in every case:

```
python3 tools/census_artifact.py --write
```

## Resolving a merge conflict

A conflict in `docs/census-artifact/` means both sides moved the same program's
verdict, added programs at the same place, or changed the set of files the run
reads. Do not edit the records by hand. After merging `origin/main` into
the branch, regenerate them, which overwrites the conflicted files with what the
merged tree produces:

```
python3 tools/census_artifact.py --write
```
