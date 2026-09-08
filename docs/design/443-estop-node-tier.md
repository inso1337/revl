# Design: the node (ts) tier and the operator E-Stop

Roadmap item 443, issue #122. This note is the reasoning of record for the
node tier's E-Stop work and the one decision that is not the runtime's to make.
`docs/design/443-estop.md` remains the reasoning of record for the halt itself.

## Where the node tier stands

The py reference tier, the formal layer, the MCP `revl_estop` operator
authority and the multi-process conductor all shipped (item 443, PRs #169 and
#284). The go and rust tiers then joined `src/revl/estop.py::TIERS_WITH_ESTOP`:
their placement runners read the latch, refuse a new crossing at the accept and
dispatch seams, record what was in flight, and print a `HALTED` inventory line
the conductor merges instead of reporting the child UNKNOWN.

The node (ts) tier has, on current main:

* the latch reader (`backends/typescript/estop.ts`: `latchPath`, `readLatch`,
  `estopEngaged`), byte-for-byte the rule `estop.py::read_latch` applies,
  including the fail-closed rule that a malformed latch still reads as HALTED;
* the crossing seams that refuse once the latch is armed, both the accept side
  (`bridge.ts::serve`) and the dispatch side (`bridge.ts::makeProxy`);
* the in-flight crossing registry and the halt inventory it feeds
  (`estop.ts`: `beginCrossing`/`endCrossing`/`inFlightCrossings`,
  `estopInventory`/`estopHaltLine`), a byte-compatible port of the reviewed go
  registry (`backends/go/placement_runner/estop/estop.go`), wired into both
  seams so a halt landing mid-crossing can name the ambiguous crossing.

This is the tier-independent vocabulary. It changes no cross-process contract:
everything above is gated behind an armed latch, which defaults off, so a node
placement that arms no latch is byte-identical to before.

## The decision this note asks for

`node` is deliberately NOT yet in `TIERS_WITH_ESTOP`. Adding it is the step
that makes the conductor honor the node child's halt instead of SIGKILLing it,
and it is an architect decision rather than a mechanical port, for two reasons.

1. **It inverts a stated conductor contract.** The conductor's per-tier
   reporting currently treats node as a no-seam tier on purpose:
   `tests/test_estop_conductor_443.py` asserts the latch is handed only to
   tiers that can honor it (`test_the_latch_is_handed_only_to_tiers_that_can_honor_it`)
   and that a node component is named no-seam with residue UNKNOWN
   (`test_an_estop_halts_a_live_placement_and_names_every_component`,
   `test_the_halt_kills_the_seamless_tier_and_never_stops_it_gracefully`).
   Flipping the tier is not a bug fix against those tests; it is a change to the
   contract they encode, so they change WITH the flip, in the same commit, or
   the flip is not honest.

2. **It changes the node child's lifecycle under an armed latch.** Honoring the
   latch means the runner (`placement_runner.ts`) grows an idle-process watcher
   (the go `main.go` twin): a process parked on its stop event crosses nothing,
   so the lazy per-crossing check never fires, and it would notice the button
   only when work next arrived. The watcher makes it halt on the button, print
   its `HALTED` inventory line, and die where it stands. That self-exit is a new
   node-child behavior. It only ever fires under an armed latch, but it is still
   a lifecycle change the conductor has to expect (the child exits without a
   `DOWN`, which the conductor must read as halted-then-exited, not as a crash).

Doing (1) and (2) in a runtime slice without the architect owning the contract
change would be exactly the "invent unreviewed runtime semantics" this arc has
avoided so far. Hence this note.

## The open sub-questions for that decision

1. **Does the node watcher `process.exit` on halt, matching go and py, or does
   it stay up and merely refuse?** The go/py answer is that the instance is dead
   (item 443, open question 3): it exits after printing its inventory. Node
   should match unless there is a reason a node child must outlive its halt.

2. **What does the conductor report for a node child that printed its inventory
   AND self-exited?** Once node is in `TIERS_WITH_ESTOP`, `_halt_all` gives it
   the halt window and `pump` already parses its `HALTED` line unconditionally,
   so the merge path exists. The disposition tag for "read the latch, halted
   itself, and exited on its own" is `halted-self-exit`, which the conductor
   already has for the py tier. No new tag is needed; the tests that assert
   `killed-no-seam` for node move to that tag.

3. **Is there any node-specific reason the latch cannot be honored** (an ESM or
   runtime-tier limitation like the five relative-specifier loops noted for the
   ts runtime contract)? If so it is recorded per tier in the roadmap as a
   deliberate non-honoring, which is the other way item 443 lets a tier close.

## Note on #598

Issue #598 ("e-stop: node tier honors the operator halt") was an earlier attempt
at this and was closed as stale: its branch drifted ~144 commits behind main and
could not be cleanly rebased, and the remaining node/ts work was folded back
under the #122 epic to be rebuilt on current main. So the exclusion of node from
`TIERS_WITH_ESTOP` is not blocked on an external dependency; it is this decision,
un-taken. The vocabulary above is the rebuilt-on-current-main foundation the flip
sits on.
