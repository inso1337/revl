"""The E-Stop is safe to engage from any thread (issue #1487).

WHAT WAS WRONG. `runtime.estop` walked `list(_LIVE_FRAMES)` and checked, then
set, the process-global halt with nothing guarding either. Measured on the
#1463 HTTP lane under concurrent frame churn: "Set changed size during
iteration" 189 times in about 200,000 halts. An E-Stop that can throw is an
E-Stop that can fail to stop. Two concurrent halts could also both see no
halt and both walk the frames.

WHAT HOLDS NOW. The halt is one critical section under `_ESTOP_LOCK`, and a
frame registers under the same lock, so the walk never sees the set change,
concurrent callers get the same halt record, and a frame built while a halt
is in progress is either walked or born halted.

These are stress tests with real threads. A pass is evidence, not proof: the
race needs the scheduler to switch threads inside the walk.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_BACKEND = _ROOT / "backends" / "python"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import runtime as rt  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_halt():
    def _reset():
        rt.clear_estop()
        rt.arm_estop_latch(None)
        rt._LIVE_FRAMES.clear()
    _reset()
    old = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)   # switch threads as often as the VM allows
    yield
    sys.setswitchinterval(old)
    _reset()


class _Ctx:
    pass


#: Each stress loop runs up to this many halt rounds, or until the wall-clock
#: budget is spent, whichever comes first, so a heavily loaded machine runs
#: fewer rounds instead of timing out. On an idle one all rounds run in a few
#: seconds; `MIN_ROUNDS` keeps a loaded run from proving nothing.
ROUNDS, BUDGET_S, MIN_ROUNDS = 300, 20.0, 20


def _rounds():
    deadline = time.monotonic() + BUDGET_S
    for n in range(1, ROUNDS + 1):
        yield n
        if n >= MIN_ROUNDS and time.monotonic() > deadline:
            return


def _churn(stop: threading.Event, errors: list, kept: list) -> None:
    """Build frames as fast as possible, keeping some alive and dropping the
    rest, so the live set grows and shrinks under the halt's walk."""
    try:
        while not stop.is_set():
            frame = rt.Frame(_Ctx(), "Churn")
            if len(kept) < 400:
                kept.append(frame)
    except BaseException as error:  # noqa: BLE001 — recorded for the assert
        errors.append(error)


def _halt_round(halters: int, errors: list) -> list:
    barrier = threading.Barrier(halters)
    results: list = []

    def halt():
        try:
            barrier.wait()
            results.append(rt.estop("stress", operator="op"))
        except BaseException as error:  # noqa: BLE001 — recorded for the assert
            errors.append(error)

    threads = [threading.Thread(target=halt) for _ in range(halters)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return results


def test_concurrent_halts_under_frame_churn_never_raise_and_halt_every_frame():
    errors: list = []
    kept: list = []
    stop = threading.Event()
    churners = [threading.Thread(target=_churn, args=(stop, errors, kept))
                for _ in range(4)]
    for thread in churners:
        thread.start()
    rounds = 0
    try:
        for rounds in _rounds():
            results = _halt_round(4, errors)
            assert not errors, errors[:3]
            # one halt, whoever got there first: every caller has its record
            assert len({r["at"] for r in results}) == 1
            # every frame alive now is halted, including any built during the
            # halt or after it
            unhalted = [f for f in rt._live_frames() if not f._halted]
            assert unhalted == []
            rt.clear_estop()
            kept.clear()
    finally:
        stop.set()
        for thread in churners:
            thread.join()
    assert not errors, errors[:3]
    assert rounds >= MIN_ROUNDS


def test_a_frame_built_after_the_halt_is_born_halted():
    rt.estop("before", operator="op")
    frame = rt.Frame(_Ctx(), "Late")
    assert frame._halted is True


def test_in_flight_bookkeeping_is_safe_under_concurrent_halts():
    """`_InFlight` appends and removes under the same lock the halt snapshots
    under, so a crossing dispatched on another thread is either in the halt's
    `inFlight` list or not, never a torn read."""
    errors: list = []
    stop = threading.Event()

    def dispatch():
        try:
            while not stop.is_set():
                with rt._InFlight(component="C", method="m", seq=None):
                    pass
        except BaseException as error:  # noqa: BLE001
            errors.append(error)

    workers = [threading.Thread(target=dispatch) for _ in range(4)]
    for thread in workers:
        thread.start()
    try:
        for _ in _rounds():
            _halt_round(3, errors)
            rt.clear_estop()
    finally:
        stop.set()
        for thread in workers:
            thread.join()
    assert not errors, errors[:3]
