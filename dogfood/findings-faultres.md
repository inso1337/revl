# findings-faultres — fault-path `assert no residue` now reads the host trace

Branch `agent/fault-res` off devwip @ e0fc683. Fixes the uxprobe2 asymmetry:
a component whose setup acquires a Map stub with a NON-INVERSE undo
(`undo scratch.insert("leak", "1")`) passed a fault test while the identical
component failed a plain lifecycle test.

## The diagnosis (who records map#1, who reads it)

There are **two independent residue detectors** in the tree, and they did not
read the same evidence:

1. **Lifecycle `assert no_residue`** compiles into the py tier's harness
   (`backends/python/emit.py`, `_revl_no_residue`). It checks two halves:
   - **R4**: four cordis introspections (`registry.size`, provisions,
     effect-stack depth, event-hook counts) against a baseline; and
   - **R1 host-resource accounting**: the harness installs
     `set_trace(events.append)` at test start and pairs acquire/release verbs
     over the captured host trace (`_REVL_ACQUIRE = {"new": "drop",
     "open": "close"}`). The cordis-py runtime records every stub operation
     as `"map#1.new"`, `"map#1.insert leak"`, `"map#1.drop"` — so an
     acquisition whose release never came is visible as an unpaired `new`.

2. **Fault-test `assert no residue`** is judged revl-side in
   `src/revl/fault.py::_judge`, from `_snapshot()` — which reads exactly the
   four R4 introspections, at baseline / unwound / settled. It never touched
   the trace.

**The exact split: revl-side, in the fault harness.** Not the py adapter, not
cordis-py: the runtime already records everything needed (the lifecycle proof
consumes the same stream). With a non-inverse undo, the undo *runs* —
`scratch.insert("leak", "1")` executes happily — so disposables drain, the
registry returns to baseline, and all four counters match at every phase.
The leak lives only as an unpaired `new` in the host trace, which nothing on
the fault path read. Reproduced before fixing:

- lifecycle: `FAIL unload leaves nothing: ... residue — host resources never
  released: map#1 (new() with no drop()) (R1)`
- fault test: `PASS mid-activation failure reverts its acquisition [...]`

## The fix (right layer, revl-side)

`src/revl/fault.py`:

- `_drive` captures the host trace around the target's activation→settle
  window (`runtime_mod.add_trace(events.append)`), opened **after** the
  baseline snapshot so resources siblings acquired and hold are not charged
  to this test;
- `_unreleased_host_resources(events)` applies the same acquire/release
  pairing as the lifecycle harness (verbs mirrored in `_ACQUIRE_VERBS`; the
  two tables sit on opposite sides of the emit boundary, so sync is enforced
  by tests on both sides rather than by import);
- `_judge`'s `no residue` branch adds: `residue in the host: map#1 (new()
  with no drop()) — acquired during the activation and never released by its
  inverses (R1); the runtime counters all returned to baseline, which is
  exactly why the trace is read`.

No errata entry needed — this was fixable entirely within revl's own harness.
`docs/fault-tests.md`'s assertion table gains check (e), and
`examples/uxprobe2_fault.rvl`'s header comment no longer advertises the old
weaker behavior.

## Regression coverage

`tests/test_fault_tests.py`:

- judging layer (no runtime needed): pairing unit tests (drop closes, insert
  does not, pool open/close, non-host events ignored) + fabricated-outcome
  `_judge` tests naming the resource and staying quiet when paired;
- execution layer (`@needs_cordis`): the uxprobe2 repro verbatim must FAIL
  with `residue in the host` / `new() with no drop()` / `(R1)`, plus a
  positive control proving the real inverse keeps passing.

One testing lesson worth keeping: the first version asserted `map#1`
literally and failed only in the whole-file run — the stub serial is global
in the runtime, so the number depends on how many Maps earlier tests made.
Assert the shape (`map#\d+`), never the serial.

## Environment notes other agents will want

- `sh backends/python/setup.sh` works and creates a worktree-local
  `backends/python/.venv` with cordis-py editable; run suites with
  `backends/python/.venv/bin/python -m pytest`. Without it, all cordis-gated
  execution tests SKIP silently — the leak would have been invisible.
- Two failures under that venv are **pre-existing on unmodified baseline**
  (verified by stashing): `test_an_await_body_reports_the_known_cordis_py_divergence`
  and `test_replay_tools_say_recording_must_be_switched_on_at_load`. Both pin
  behaviors of a specific cordis-py vintage; setup.sh clones the branch HEAD,
  and upstream moved. Upstream-drift, not this change.
- Follow-up noted, out of scope: `_process_runner.py`'s placement teardown
  prints a residue verdict from the same three counters (it logs the host
  trace but does not pair it). For a live placement, resources held by ACTIVE
  components are legitimate, so the R1 pairing there needs per-component
  scoping before it means anything.

## Suite

Main venv (execution tests skip): **1525 passed, 79 skipped**, zero failures,
against this devwip's own stashed baseline of **1519 passed, 77 skipped** —
the delta is exactly the six always-run judging/pairing tests plus the two
execution tests that correctly skip without cordis. Under the cordis venv:
1570 passed vs 1562 stashed baseline, with the same two upstream-drift
failures on both sides.
