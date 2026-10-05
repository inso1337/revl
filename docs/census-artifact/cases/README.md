# Census case records

One file per program the census runs, written by
`python3 tools/census_artifact.py --write` and checked by
`python3 tools/census_artifact.py --verify --strict`. Do not edit by hand.

Each file holds the program's case id and the bucket it landed in, as one JSON
object: `{"buckets": [...], "case": "<case id>"}`. A case id the corpus reaches
twice has two buckets, in run order.

## Where a program's file is

A case id that is a clean repo-relative path is mirrored as a path, so a pull
request's diff shows which program moved:

    tests/fixtures/value_method_call/t1_x.rvl  ->  cases/tests/fixtures/value_method_call/t1_x.rvl.json

A path is clean when every segment is non-empty, is not `.` or `..`, and uses
only `A-Z a-z 0-9 _ . + -`, and its first segment is not `_escaped`.

Every other case id (the inline programs, `admission:...`, `oracle-reject:...`)
goes under `cases/_escaped/`, with each UTF-8 byte outside `A-Z a-z 0-9 _ . -`
written as `%XX` in uppercase hex, `/` included:

    admission:comment_only  ->  cases/_escaped/admission%3Acomment_only.json

The rule is `case_path` in `tools/census_artifact.py`. It is deterministic and
injective: a clean id never starts with `_escaped`, and an escaped name keeps
every byte. The loader refuses a file whose path is not its own id's path.

## Why one file per program (issue #1768)

The records used to be one sorted file, `cases.jsonl`. Two pull requests that
added programs sorting next to each other inserted into the same gap and
conflicted. Two pull requests that add different files never conflict. Two
pull requests that both edit one program's file moved the same verdict twice,
which is a real conflict.

After a conflict, merge `origin/main` and run `python3 tools/regen_generated.py`.
