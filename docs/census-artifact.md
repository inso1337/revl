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
