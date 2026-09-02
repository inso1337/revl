"""Phase-1 continue-and-record on the py tier (docs/design/teardown-contract.md).

The contract's Phase-1 rule, verbatim: *"A failed inverse never skips the
remaining Phase-1 inverses. Skipping strictly increases residue: every un-run
inverse downstream of a failure is state that was recoverable and is now not.
So the rule is uniform, one mechanism: catch, record into the merged residue
schema, continue."* A failed BRACKET inverse carries the contract-grade
severity `bracket-fault` — the inverse claimed G5 infallibility and lied.

Before the fix the py tier implemented continue-and-record only for PHASE 2
(`Frame._drain_phase2`) and for the session flush (`SessionOwner._flush`).
Phase 1 had no guard at all: the emitted body yields bare `lambda: <undo>`
disposers, cordis disposes them strictly sequentially, and ONE raise broke the
chain — every earlier-registered (later-disposed) inverse was starved, nothing
was recorded, and the fiber still reported a clean DISPOSED. G7 (LIFO
completeness) and R4 (no unreported residue) both broke, silently.

The guard is runtime-side (`Frame._guard`, routed from `Frame._tracked`), not
emitter-side, so it covers every emitted disposer shape — plain bracket,
result-guarded CAS inverse, witnessed `_Transactional`, `_Compensation` — and
a later emitter change cannot forget it. The emission itself is unchanged,
which the last test pins.
"""

from __future__ import annotations

import asyncio
import importlib.util
import pathlib
import re
import sys
import types

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_BACKEND = _ROOT / "backends" / "python"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from revl.compiler import compile_source  # noqa: E402

needs_cordis = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="Phase-1 teardown is proven against a live cordis-py composition — "
           "install it with `sh backends/python/setup.sh`")

pytestmark = [needs_cordis, pytest.mark.asyncio]


def _backend():
    import emit  # noqa: PLC0415 — resolved from the backend dir appended above
    import runtime  # noqa: PLC0415
    return emit, runtime


# a bracket inverse that RAISES. `undo` is declared as an ordinary pure call,
# so the extern is exactly how an author writes a fallible inverse by accident.
_BLOW = ('extern pure fn blow(x: Str) -> Int = '
         '@py { raise RuntimeError("undo exploded") }\n')

# the audit's synchronous reproducer: three brackets, the MIDDLE one's inverse
# raises. LIFO disposal is C, B, A — so a broken chain starves A, the OLDEST
# and most valuable acquisition.
_SYNC = _BLOW + """
component C {
  let a = effect Pool.open("A", 1) undo a.close()
  let b = effect Pool.open("B", 1) undo blow("x")
  let c = effect Pool.open("C", 1) undo c.close()
}
"""

# the audit's stream variant: an async body (so the guard is proven on
# `_async_tracked` too), where a raising subscription inverse starves BOTH the
# source and the pool below it.
_STREAM = _BLOW + """
component C {
  let a = effect Pool.open("A", 1) undo a.close()
  let src = effect Stream.source() undo src.close()
  let sub = subscribe src undo blow("x")
  await sub.next()
}
"""

# the same shape with every inverse sound — the happy path, which must be
# byte-for-byte unchanged in behavior.
_CLEAN = """
component C {
  let a = effect Pool.open("A", 1) undo a.close()
  let b = effect Pool.open("B", 1) undo b.close()
  let c = effect Pool.open("C", 1) undo c.close()
}
"""


def _module(emit, source: str, name: str) -> types.ModuleType:
    module = types.ModuleType(name)
    exec(compile(emit.emit(compile_source(source, f"{name}.rvl")), f"{name}.py", "exec"),
         module.__dict__)
    return module


def _ops(events: list) -> list:
    return [re.sub(r"#\d+", "", event) for event in events]


async def _flush() -> None:
    for _ in range(60):
        await asyncio.sleep(0)


class _Run:
    """One armed activation: the trace, the probe, and the frame's residue."""

    def __init__(self) -> None:
        self.events: list = []
        self.logged: list = []
        self.state = None
        self.probe = None
        self.frame = None
        self.residue: list = []


async def _drive(source: str, name: str, deliver: bool = False) -> _Run:
    emit, runtime = _backend()
    from cordis import Context  # noqa: PLC0415

    runtime.Stream.reset()
    run = _Run()
    module = _module(emit, source, name)
    runtime.set_trace(run.events.append)
    try:
        root = Context()
        # cordis logs a disposer failure rather than raising it — capture the
        # channel so a swallowed raise is visible to the assertions.
        root.logger.error = lambda *args, **kwargs: run.logged.append(args)
        run.probe = runtime.arm_fault_probe("C")
        seen = {id(f) for f in runtime._FRAME_BY_CTX.values()}
        try:
            fiber = root.plugin(module.C)
            await _flush()
            if deliver:
                runtime.Stream.last_source().emit("item")
                await _flush()
        finally:
            runtime.disarm_fault_probe()
        # hold the activation frame across the teardown: `compensation_residue`
        # is the merged residue list every tier-py entry kind records into, and
        # it predates this fix — so these assertions are meaningful against the
        # OLD runtime too (there it stays empty, which is the bug).
        run.frame = next(f for f in runtime._FRAME_BY_CTX.values()
                         if f.name == "C" and id(f) not in seen)
        await fiber.dispose()
        await _flush()
        run.state = fiber.state
        run.residue = list(run.frame.compensation_residue)
    finally:
        runtime.set_trace(None)
        runtime.Stream.reset()
    return run


# ---------------------------------------------------------------------------
# 1. the reproducer: a raising bracket inverse must not abort the rest of Phase 1
# ---------------------------------------------------------------------------


async def test_a_raising_bracket_inverse_does_not_skip_the_remaining_inverses():
    run = await _drive(_SYNC, "phase1_sync")

    # G7: every inverse was reached, newest-first, to completion. Before the
    # fix `ran` was [3, 2] and `never_ran()` was [1] — A's inverse starved.
    assert run.probe.never_ran() == [], "every registered inverse must be reached"
    assert run.probe.ran == list(reversed(run.probe.accumulated))
    assert run.probe.lifo_violation() is None

    # the host proves it: C closed (sound inverse), then B's inverse EXPLODED,
    # then A still closed. Before the fix the trace stopped at `pool.close C`.
    assert _ops(run.events) == [
        "pool.open A", "pool.open B", "pool.open C", "pool.close C", "pool.close A"]

    # R4: the failure is recorded, not silent, and it NAMES the inverse.
    [record] = run.residue
    assert record["kind"] == "bracket-fault"
    assert record["state"] == "unresolved"
    assert record["component"] == "C"
    assert record["method"] == "blow"
    assert record["attemptedFlag"] is True
    assert record["attempted"] == {"phase": 1}
    assert record["outcome"] == "failed"
    assert record["error"] == {"type": "RuntimeError", "message": "undo exploded"}

    # the verdict is no longer a clean disposal: the fiber state is cordis's
    # own (the disposal DID complete), so the honest verdict is the residue —
    # and it is non-empty. B is the one resource still out in the world.
    assert run.residue, "a teardown that lost an inverse must not read clean"
    # and the fault-test harness reads exactly that list off the probe
    assert run.probe.residue() == run.residue


async def test_the_fault_test_judge_calls_the_reproducer_residue(tmp_path):
    """The `no residue` verdict must read the recorded residue, not only the
    probe counters. Continue-and-record means the failed inverse DID run, so
    `never_ran` is empty — without the residue arm the judge would now call
    this teardown cleaner than the silent-skip it replaces."""
    from revl import fault as fault_mod  # noqa: PLC0415

    outcome = fault_mod._Outcome()
    # a fully clean snapshot on every other axis, so the residue arm is the
    # only thing that can produce a failure line
    clean = {"provisions": [], "listeners": 0, "effects": 0, "registry": 0}
    outcome.baseline = dict(clean)
    outcome.unwound = dict(clean)
    outcome.settled = dict(clean)
    outcome.residue = [{
        "kind": "bracket-fault", "state": "unresolved", "component": "C",
        "method": "blow", "seq": None, "attemptedFlag": True,
        "attempted": {"phase": 1}, "outcome": "failed",
        "error": {"type": "RuntimeError", "message": "undo exploded"}}]
    failures = fault_mod._judge({"assert": ["no-residue"], "step": 2}, outcome, None)
    assert any("bracket-fault" in line and "blow" in line for line in failures)
    assert any("bracket-fault" in note for note in fault_mod._notes(outcome))


# ---------------------------------------------------------------------------
# 2. the stream variant (async body): two resources leaked before the fix
# ---------------------------------------------------------------------------


async def test_a_raising_subscription_inverse_does_not_strand_the_source_and_pool():
    run = await _drive(_STREAM, "phase1_stream", deliver=True)

    assert run.probe.never_ran() == [], "every registered inverse must be reached"
    assert run.probe.lifo_violation() is None

    ops = _ops(run.events)
    # before the fix the subscription's raise starved BOTH inverses below it:
    # neither `stream.source close` nor `pool.close A` appeared.
    assert "stream.source close" in ops
    assert "pool.close A" in ops
    assert ops.index("stream.source close") < ops.index("pool.close A"), \
        "the source closes before the pool it was acquired after (LIFO)"

    [record] = run.residue
    assert record["kind"] == "bracket-fault"
    assert record["method"] == "blow"
    assert record["error"]["message"] == "undo exploded"


# ---------------------------------------------------------------------------
# 3. the happy path is unchanged
# ---------------------------------------------------------------------------


async def test_a_clean_teardown_is_unchanged_and_records_no_residue():
    run = await _drive(_CLEAN, "phase1_clean")

    assert _ops(run.events) == [
        "pool.open A", "pool.open B", "pool.open C",
        "pool.close C", "pool.close B", "pool.close A"]
    assert run.probe.never_ran() == []
    assert run.probe.lifo_violation() is None
    assert run.residue == [], "a clean teardown records nothing"
    assert run.logged == [], "and logs nothing"


async def test_the_guard_is_runtime_side_so_the_emission_is_unchanged():
    """The disposers stay bare `lambda: <undo>` in the emitted body — the
    guard is installed by `Frame._tracked`, so no emitted shape can miss it
    and no golden output moves."""
    emit, _runtime = _backend()
    body = emit.emit(compile_source(_SYNC, "phase1.rvl"))
    assert "yield lambda: a.close()" in body
    assert "yield lambda: blow('x')" in body
    assert "yield lambda: c.close()" in body
