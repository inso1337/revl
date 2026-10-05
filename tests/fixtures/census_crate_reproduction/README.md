# Recorded crate reproduction

`tools/gate_reference_census.py --engine crate` asks the real `crates/revl-gate`,
built by cargo, the same questions as the census's fast engine. It takes minutes
and needs a rust toolchain, so its result is recorded here and read by
`tools/census_artifact.py`.

There is one file per checker version: `<checker version>.json`. Each holds that
version, the tracked buckets, the false admissions and the programs the run
covered. Only the record at the current checker version can lift a claim; a
record at any other version is stale and lifts nothing.

Recording ADDS the file for the current version and deletes nothing:

```
python3 tools/regen_generated.py --only census
```

That command is also the resolution after a merge. It needs cargo.

Stale records are deleted by a change of their own:

```
python3 tools/census_artifact.py --prune-stale-reproductions
```

It refuses while there is no record at the current version, so a prune never
leaves the report with less than it had.

## Why one file per version, and why recording only adds (issue #1768)

The record used to live in one file, and its `checker_version` line changed in
every pull request that moved `selfhost/lower.rvl` or another checker source.
Two such pull requests always conflicted on that line, and resolving it cost
another crate run. Now each pull request that re-records adds its own file, and
two of them add two different files, which git merges.

Recording does not delete the old record in the same change. Two records are
nearly identical, so git would read the delete-plus-add as a RENAME, and two
pull requests renaming one file to two names conflict exactly as the single file
did. A prune on its own is a plain deletion, which merges with any re-record.

After two re-recording pull requests land, main holds records at both of their
versions and neither is at main's own, because each was taken before the other's
change. Nothing is lifted until one re-record on main. CI's `census-artifact`
job (`python3 tools/census_artifact.py --verify --strict`) names the missing
record until it is made.
