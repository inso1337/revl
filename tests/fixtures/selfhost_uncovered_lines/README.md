# Self-host uncovered-lines ledger

Which STATEMENTS of the mirrored emitters no corpus document executes.
Generated and gated by `tools/selfhost_line_coverage.py` (roadmap item 429),
run by `tests/test_selfhost_line_coverage.py`.

The sibling ledger `selfhost_blind_spots.json` counts DISPATCH ARMS. This one
counts statements, because an arm the corpus reaches can have almost all of its
body unexercised, and that is the larger half of the gap.

- `reference/<tier>.jsonl`: `backends/<tier>/emit.py`, run over the tier's own
  CORPUS list.
- `selfhost/<tier>.jsonl`: `selfhost/emit_<tier>.rvl`, measured through the
  python module it compiles to. Counts attribute to the `.rvl` FUNCTION exactly
  (the emitted `def` keeps the `fn` name); they do not attribute to a `.rvl`
  LINE, because `selfhost/*.rvl` carries no line provenance into its emitted
  output.

## Records

Each file holds two kinds of record, one per line, with a blank line between
records, sorted by reason id (a reason before its functions, functions by name):

```
["reason", "<reason id>", "<why these statements are unreached>"]
["function", "<reason id>", "<qualified function>", <uncovered>, <budget>]
```

The unit is (qualified function, count of unexecuted statements). Not line
NUMBERS: those churn on every edit above them, and a ledger that churns is a
ledger nobody reads. A reason id is any slug unique within its file; it never
changes when the reason's text is edited.

## The ratchet and the budget

RATCHET, both directions. A count that RISES fails: logic arrived that no corpus
document reaches. A count that FALLS also fails, asking you to record the
improvement, which is what makes the number monotone instead of merely bounded.
Regenerate the counts with `python3 tools/selfhost_line_coverage.py --write`,
but only after reading what moved: the usual right fix is a corpus document.

THE BUDGET is the last field of a function record. `--write` does not write it,
and the gate holds the function's count to it EXACTLY. So recording costs an
integer raised BY HAND in the same diff, and an improvement lowers the ceiling
for good instead of leaving headroom for the next unreached region to spend.
Target: zero. A function `--write` adds for the first time has a `null` budget,
which fails until a person writes the number.

Until issue #1768 the budget was one number per half and tier, in a `_budget`
block of a single JSON file, beside per-tier `statements` totals. Every pull
request that moved a count rewrote one of those adjacent lines, so after each
landing almost every open pull request that touched an emitter conflicted in
it. A budget per function is at least as strict (one function's rise can no
longer hide behind another's fall), the totals are computed by the report and
not stored, and two pull requests now conflict here only when both touched the
same record.

IT IS A RATCHET AND NOT AN INVENTORY. 21 of the first 88 changes to it LOWERED
the mass, the 2026-09-05 corpus triage took 2234 statements out of it across
five commits, and #1178 took 306 more out on 2026-09-20.

A count here that can only ever be RECORDED is a bug in the gate, not a fact
about the code. That was true of every refusal a tier states by name: a corpus
document is one the reference EMITS, a refusal path runs only where it does not,
so no corpus document could reach either half of one.
`tests/fixtures/emit_<tier>_refusals/` is where those documents live now, and
both halves are driven over them. See `refusal_documents()` in
`tools/selfhost_line_coverage.py` (issue #1419).

## Resolving a merge conflict

A conflict here means both sides changed the same record. After merging
`origin/main` into the branch, run

```
python3 tools/selfhost_line_coverage.py --write
```

It reads through the conflict markers (keeping the first copy of a record that
appears twice), re-measures every count and rewrites the files clean. It never
writes a budget, so `python3 tools/selfhost_line_coverage.py --check` then names
any budget the merged counts disagree with, to be set by hand.
