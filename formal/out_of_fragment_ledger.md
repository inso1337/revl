# Out-of-fragment ledger

`formal/out_of_fragment_ledger/` lists the corpus files the checker refuses
under G5, G6, the G4 host release rule or a witnessed extern with a site `undo`,
and about which the formal model has NO fact. These are the
`out-of-fragment-G5`, `out-of-fragment-G6`, `out-of-fragment-inverse` and
`out-of-fragment-witnessed` checker-alignment buckets in
`formal/harness/diff_corpus.py`.

Some rules have left this ledger:

- The G4 deferred-position rule left it in issue #1742. The model states it as
  `RevL.G4Deferred`, decided as the `DF` row.
- The G4 approval floor left it in issue #1455: `RevL.G4Approval`, decided as
  the `AP` row.
- The G5 bucket emptied in issue #1792, when the exporter began resolving an
  inverse's indirections.
- The G6 bucket emptied in issue #1812, when binding uniqueness became
  `RevL.G6Binding` (the `BU` row).

Both of those buckets stay, so a new unresolvable `undo` or a new G6 purity
refusal still reds the gate.

`out-of-fragment-witnessed` (issue #1963) holds the files the checker refuses
for a witnessed extern called with a site `undo`. The model has no
witnessed-extern fact yet.

## Why it is a ratchet

Each bucket records an absence, so none of them can disagree with anything, and
none could fail the gate on its own (issue #1169). This ledger is what makes
them fire: MEMBERSHIP is checkable even when the contents are not.

- A file that joins a bucket without a record here is a gate failure.
- A record whose file is no longer in its bucket is also a gate failure, and
  the record must be DELETED.

So the ledger only shrinks, and every name left is a hole someone still owes the
model a row for.

## Format (issue #1768)

One file per record: `formal/out_of_fragment_ledger/<bucket>/<file>.json`,
holding `["bucket", "file"]` and a newline. For example
`examples/rejections/g4_undo_not_release.rvl` in `out-of-fragment-inverse` is

```
formal/out_of_fragment_ledger/out-of-fragment-inverse/examples/rejections/g4_undo_not_release.rvl.json
```

The path mirrors the name. A name with a character outside `[A-Za-z0-9_.+-]` in
a segment, an empty, `.` or `..` segment, or a first segment of `_escaped` is
filed at `<bucket>/_escaped/<name>.json` instead, with every byte outside
`[A-Za-z0-9_.-]` (including `/`) written as uppercase `%XX`. This is the census
cases' rule (`docs/census-artifact/cases/README.md`).

The ratchet refuses a malformed record, a bucket it does not hold, a record
filed at another record's path, a record whose bytes are not the canonical ones,
and any other file in the directory. No directory is the empty ledger; deleting
it while a bucket has members fails the gate.

It records NAMES only, never counts, totals or line numbers, so it is identical
under CI's python 3.11 and a 3.14 developer venv.

It used to be one JSON object of lists, then one record per line. In both, two
pull requests that each added a name in the same gap, or next to names the
other removed, conflicted. A file per record merges: two pull requests conflict
here only if both change the same name.

## Reading it

```
python3 formal/harness/diff_corpus.py --show-ledger
```

prints `bucket file` per record, sorted. It reads the directory and runs
nothing. `formal/STATUS.md` points here rather than listing the names.

## Regenerating

```
python3 formal/harness/diff_corpus.py --write-ledger
```

Read the diff afterwards: a new file is a new hole, not a formality.
`--write-status` deliberately does not touch the ledger.

After merging main into a branch that predates this layout, take main's side of
the old `formal/out_of_fragment_ledger.json` (`python3 tools/regen_generated.py`
does), and if the formal check then names entries this branch owes, run
`--write-ledger`.
