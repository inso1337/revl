# Design: the operator E-Stop on the ts (node) tier

Roadmap item 443, GitHub issue #122. Companion to `docs/design/443-estop.md`,
which is the reasoning of record for the verdict itself. This document covers
one thing: how the `node` tier honors the same latch the py reference tier does,
so a placement halt reports a node child HALTED rather than SIGKILLed.

Source: `backends/typescript/estop.ts` (the latch vocabulary and the inventory
shaping), `backends/typescript/bridge.ts` (the crossing seams and the in-flight
registry), `backends/typescript/placement_runner.ts` (the idle watcher),
`src/revl/estop.py` (`TIERS_WITH_ESTOP`), `src/revl/placement.py` (the conductor
halt and its report). Tests: `backends/typescript/tests/estop.test.ts`,
`backends/typescript/tests/estop_runner.test.ts`,
`tests/test_estop_conductor_443.py`.

## The gap this closes

Item 443 landed the halt on the py reference tier. The five non-py tiers kept
their cooperative teardown and had no E-Stop, so under a placement halt the
conductor could only SIGKILL them and report their residue UNKNOWN, per
component. That is honest, but it is a worse answer than the py tier gets: a
killed process may have dispatched a crossing microseconds before it died, and
nothing recorded it.

E-Stop is not a graceful unwind. The whole point of the verdict is that it does
NOT unwind: it stops dispatching new crossings, reports what was in flight, and
marks the ambiguous ones ambiguous (item 440). "Honoring the latch" on a tier
therefore means three concrete things, not a teardown path:

1. the crossing seams refuse a NEW crossing the instant the latch is armed;
2. the process names what was ALREADY in flight when the button was hit;
3. it dies where it stands, with no inverse replayed and no residue proof.

## What the node tier does

Slices 1 and 2 (landed earlier, `estop.test.ts`) gave node (1): both the accept
seam (`bridge.ts::serve`) and the dispatch seam (`bridge.ts::makeProxy`) consult
`estopEngaged()` and refuse once the latch is armed. The latch reader
(`estop.ts::readLatch`) is the byte-for-byte twin of `src/revl/estop.py`,
including the fail-closed rule: a malformed latch still reads as HALTED.

This slice adds (2) and (3):

- **In-flight inventory (`bridge.ts`).** The accept seam records each crossing
  while its handler runs (`beginCrossing`) and clears it on return, in a
  `finally` so a throwing handler still leaves the registry clean. A crossing
  still executing when the latch trips is AMBIGUOUS: its at-most-once attempt
  may or may not have landed. `estop.ts::estopInventory` shapes those crossings
  into the merged residue schema `src/revl/placement.py::_estop_halt_report`
  reads. This tier keeps no witnessed-inverse ledger, so `stranded` is empty and
  honestly so: the halt reports the ambiguous crossings it CAN name rather than
  inventing a book it does not keep, and the conductor never reads that empty
  list as "nothing was owed" because the ambiguous crossings are reported.

- **The idle watcher (`placement_runner.ts`).** The seams refuse lazily, at the
  next crossing, which is useless for a process parked waiting to be stopped: it
  crosses nothing and would sit through the emergency. The runner polls the
  latch and, on the button, prints its inventory on one `[name] HALTED {json}`
  line (the conductor merges it by prefix, no second channel) and calls
  `process.exit`, which runs no SIGTERM handler, so `teardown` and its `DOWN`
  never fire. This is the ts twin of the py runner's `estop_from_latch`.

- **The conductor (`src/revl/estop.py`, `placement.py`).** `node` joins
  `TIERS_WITH_ESTOP`. The conductor now hands a node child the latch in its spec
  (a sandboxed child, item 411, need not inherit the environment), gives it the
  bounded inventory window instead of an immediate kill, and reports it HALTED
  with its inventory rather than "NO E-Stop seam / residue UNKNOWN". The runner
  publishes the spec latch to the ambient variable the seams already read, so
  the accept seam, the dispatch seam and the watcher all consult one latch.

An unarmed placement is unchanged: no latch means no watcher, no seam read, and
a node child byte-identical to before.

## What remains (issue #122)

The four other non-py tiers (`rust`, `go`, `java`, `wasm`) keep their
cooperative teardown and no E-Stop. Under a placement halt they are still
SIGKILLed and reported UNKNOWN per component, which the conductor names
individually. Issue #122 closes when each of those either honors the latch with
these semantics or the roadmap records per tier why it deliberately will not.

The node dispatch seam refuses outgoing crossings but does not yet ADD them to
the in-flight registry (only the accept seam does). A consumer blocked on a
reply when the button is hit is ambiguous too; recording the dispatch side is a
small follow-up that widens the inventory without changing the verdict.

## Open questions, mapped

Item 443's three open questions are answered the same way on node as on py, and
this slice does not reopen them: an E-Stop is a third verdict column, not a
weakening of G7 (the LIFO completeness theorem is vacuous under `halted`, since
nothing replays); a bracket whose inverse is itself in flight is one of the
ambiguous crossings this inventory names; and the instance is dead afterwards
(`resumable: false`), with `revl recover --wal` the only way back.
