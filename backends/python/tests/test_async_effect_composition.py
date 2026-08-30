"""Item 131 runtime proof (py): explicit async/await EFFECT composition.

These are the design's exit tests 1-4 executed against cordis-py. Unlike the
rest of this suite they compile from `.rvl` SOURCE (revl is importable beside
the backend), so the whole pipeline is proven end to end: the frontend admits
the `effect await` / `await emit` spellings, the emitter renders the
`async def` body generator with the awaited acquisition/emission, and the
runtime tears the accumulated effects down LIFO ACROSS the suspension with no
residue — the novelty item 131 claims (design §4), pinned rather than asserted.

The suspension is a req-target `async fn` service op whose provide method parks
on `Job.run` (the runtime's controllable async host op), so the test can dispose
the consumer while an acquisition is in flight and observe the inertia + LIFO
teardown the two-phase abort contract promises.
"""

from __future__ import annotations

import asyncio
import pathlib
import re
import sys
import types

import pytest

from cordis import Context
from cordis.fiber import FiberState

import emit
import runtime as runtime_mod

# revl (the frontend) lives beside this backend; import it so the proof runs the
# real compile -> emit -> run pipeline rather than a hand-built IR.
_SRC = pathlib.Path(__file__).resolve().parents[3] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from revl import RevlError, compile_source  # noqa: E402


def _module(src: str, name: str) -> types.ModuleType:
    code = emit.emit(compile_source(src))
    module = types.ModuleType(name)
    exec(compile(code, f"{name}.py", "exec"), module.__dict__)
    return module


def _ops(events: list[str]) -> list[str]:
    return [re.sub(r"#\d+", "", event) for event in events]


# A Database provider backed by the Pool host, plus one async method whose body
# parks on `Job.run` before it records — the controllable suspension source.
_PROVIDER = """
service Database {
  fn query(sql: Str) -> List[Row]
  emission fn execute(sql: Str) -> Int
  async fn slow_open(sql: Str) -> List[Row]
  emission async fn record(sql: Str) -> Int
}
component PgDatabase provides db: Database {
  config { url: Str = "postgres://localhost/app" }
  let pool = effect Pool.open(config.url, 4) undo pool.close()
  provide db {
    fn query(sql)   = pool.query(sql)
    fn execute(sql) = pool.execute(sql)
    async fn slow_open(sql) { await Job.run("B")  return pool.query(sql) }
    async fn record(sql)    { await Job.run("R")  return pool.execute(sql) }
  }
}
"""


@pytest.fixture
def trace():
    events: list[str] = []
    runtime_mod.set_trace(events.append)
    yield events
    runtime_mod.set_trace(None)


async def _flush() -> None:
    for _ in range(40):
        await asyncio.sleep(0)


# ---------------------------------------------------------------------------
# Exit test 1 — async acquisition roundtrip: close AFTER open, no residue
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_async_acquisition_roundtrip(trace):
    src = _PROVIDER + """
component Consumer requires db: Database {
  let conn = effect await db.slow_open("OPEN") undo db.query("CLOSE")
}
"""
    module = _module(src, "aec_roundtrip")
    root = Context()
    root.plugin(module.PgDatabase)
    consumer = root.plugin(module.Consumer)
    await _flush()
    assert consumer.state is FiberState.ACTIVE
    assert "pool.query OPEN" in _ops(trace), "the awaited acquisition must land"

    consumer.dispose()
    await _flush()
    ops = [e for e in _ops(trace) if e in ("pool.query OPEN", "pool.query CLOSE")]
    # R1: the inverse runs, and only after the acquisition (open before close)
    assert ops == ["pool.query OPEN", "pool.query CLOSE"]
    assert consumer.state is FiberState.DISPOSED


# ---------------------------------------------------------------------------
# Exit test 2 — abort DURING an in-flight acquisition (the novelty pinned)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_abort_during_inflight_acquisition_is_lifo(trace):
    """A (sync) then `effect await B` where B parks in flight; dispose while B
    is in flight. Assert: B lands (inertia), then teardown runs B's inverse
    BEFORE A's (LIFO across the suspension), and no residue remains."""
    src = _PROVIDER + """
component Consumer requires db: Database {
  let la = effect db.query("ACQ A") undo db.query("UNDO A")
  let lb = effect await db.slow_open("ACQ B") undo db.query("UNDO B")
}
"""
    module = _module(src, "aec_abort")
    root = Context()
    root.plugin(module.PgDatabase)
    consumer = root.plugin(module.Consumer)

    # advance until B's acquisition is in flight: A is acquired, B's Job.run has
    # started but not completed
    for _ in range(60):
        await asyncio.sleep(0)
        seen = _ops(trace)
        if "job.run B start" in seen and "job.run B done" not in seen:
            assert "pool.query ACQ A" in seen
            assert "pool.query ACQ B" not in seen, "B must not have landed yet"
            break
    else:
        raise AssertionError("B's acquisition never reached its in-flight window")

    consumer.dispose()
    await _flush()

    order = [e for e in _ops(trace) if e.startswith("pool.query")]
    # inertia: B lands despite the withdrawal; LIFO: UNDO B precedes UNDO A;
    # no residue: every acquired effect's inverse ran, none left behind
    assert order == [
        "pool.query ACQ A",
        "pool.query ACQ B",
        "pool.query UNDO B",
        "pool.query UNDO A",
    ]
    assert consumer.state is FiberState.DISPOSED


# ---------------------------------------------------------------------------
# Exit test 3 — a failed async acquisition leaves no residue
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_failed_async_acquisition_leaves_no_residue(trace):
    """B's awaited acquisition raises: the activation fails (A8), A's inverse
    replays, B contributed no entry (an acquisition that never returned never
    acquired). Residue empty; no orphaned B inverse in the trace."""
    # `bad_open` is an async-colored fn (it reaches the async emission extern
    # `ho_fail`, whose host body raises), so the awaited acquisition faults.
    src = _PROVIDER + """
extern emission async fn ho_fail(u: Str) -> Str = @py { raise RuntimeError("acq B failed") }
fn bad_open(u: Str) -> Str { return ho_fail(u) }
component Consumer requires db: Database {
  let la = effect db.query("ACQ A") undo db.query("UNDO A")
  let lb = effect await bad_open("x") undo db.query("UNDO B")
}
"""
    module = _module(src, "aec_failed")
    root = Context()
    root.plugin(module.PgDatabase)
    consumer = root.plugin(module.Consumer)
    await _flush()

    events = _ops(trace)
    assert consumer.state is FiberState.FAILED, "the awaited acquisition must fault"
    assert "pool.query ACQ A" in events, "the sync prefix was acquired"
    # A's inverse replays (prefix reverts LIFO); B registered nothing
    assert "pool.query UNDO A" in events, "the acquired prefix must revert"
    assert "pool.query UNDO B" not in events, \
        "a failed acquisition contributes no inverse (no residue)"


# ---------------------------------------------------------------------------
# Exit test 4 — awaited emission with compensation (a5a / a5b under await)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_awaited_emission_compensation_discharges_on_clean_unload(trace):
    """`await emit … compensate …`: on a clean unload the compensation is
    DISCHARGED (never runs) and the forward emission stands (a5a); the awaited
    variant must not change the two-phase loop."""
    # `record` is an async emission op (awaited); the compensation is a SYNC op
    # (`execute`) — teardown never suspends (rule 3), so the compensation stays
    # a synchronous call while the forward emission awaits.
    src = _PROVIDER + """
component Emitter requires db: Database {
  let lock = effect db.query("LOCK") undo db.query("UNLOCK")
  await emit db.record("INSERT") compensate db.execute("DELETE")
}
"""
    module = _module(src, "aec_await_emit")
    root = Context()
    root.plugin(module.PgDatabase)
    emitter = root.plugin(module.Emitter)
    await _flush()
    assert emitter.state is FiberState.ACTIVE
    assert "pool.execute INSERT" in _ops(trace), "the awaited emission must fire"

    emitter.dispose()
    await _flush()
    events = _ops(trace)
    assert "pool.execute DELETE" not in events, \
        "a5a: the compensation is discharged on a clean unload, never run"
    assert "pool.query UNLOCK" in events, "the bracket inverse still replays"
    assert emitter.state is FiberState.DISPOSED
