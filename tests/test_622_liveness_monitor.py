"""Roadmap item 477 follow-up 1 / issue #622 — WIRE the declared liveness
ceiling to PRODUCTION silence detection.

#477 landed the declared ceiling, the `LIVENESS_EXPIRED` vocabulary and the
runtime PRODUCER (`_Driver._perform_liveness_expiry`), but drove the producer
with a silence duration a TEST supplied (test_477_liveness_ceiling.py), leaving
detection explicitly out of scope. This slice is the owned observer that
measures the silence itself:

  * `_liveness_observability` classifies a declared ceiling as observable, or
    REFUSES it as a shared-clock (multi-realm route) or blocking (a cross into a
    required service) case — "define or refuse", never a universal watchdog;
  * `_LivenessMonitor` enrols the observable providers, times each against an
    injectable clock, and invokes the producer under the declared ceiling with a
    `silent_ms` IT computes — no test in the loop;
  * a healthy provider (answers before its ceiling), an unrelated owner (no
    ceiling), and a provider merely waiting on a peer are never expired;
  * `stop()` cancels and awaits the background task, so a cancelled/unloaded
    generation leaves no independent watcher behind.

The producer's own firing path (the fiber-settle boundary cordis owns) is
stubbed exactly as test_477 stubs it; the monitor's DETECTION runs for real.
"""

from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402
from revl import why_runtime as wr  # noqa: E402
from revl.run import (  # noqa: E402
    _Driver,
    _LivenessMonitor,
    _hangable_crossing_kinds,
    _liveness_observability,
)


HANGABLE = """
service Database { fn query(sql: Str) -> List[Row] }
component PgDatabase provides db: Database liveness 1s {
  config { url: Str = "postgres://localhost/app" }
  let pool = effect Pool.open(config.url, 4) undo pool.close()
  provide db { fn query(sql) = pool.query(sql) }
}
component UserCache requires db: Database provides cache: Cache {
  provide cache { fn get(k) = db.query(k) }
}
service Cache { fn get(k: Str) -> List[Row] }
"""

# a declared ceiling whose activation crosses into a REQUIRED service — a hang
# there is a wait on a peer, so the observer REFUSES it as `blocking`.
BLOCKING = """
service Up { fn tick() -> Int fn drop() -> Int }
service Down { fn go() -> Int }
component Upstream provides up: Up { provide up { fn tick() = 1 fn drop() = 0 } }
component Worker requires up: Up provides down: Down liveness 1s {
  let seed = effect up.tick() undo up.drop()
  provide down { fn go() = seed }
}
"""

# an observable ceiling on a provider that ALSO requires a peer — its own host
# crossing makes it observable, but it may not be expired while its require is
# still unresolved (its silence would be the peer's, not its own).
NEEDS_PEER = """
service Log { fn line(s: Str) -> Int }
service Store { fn put(k: Str) -> Int }
component Sink provides store: Store requires log: Log liveness 1s {
  let h = effect Pool.open("s", 1) undo h.close()
  provide store { fn put(k) = h.query(k) }
}
component Logger provides log: Log { provide log { fn line(s) = 1 } }
"""


# ---------------------------------------------------------------------------
# a fake clock and a fake driver: the fiber-settle boundary is the only stub
# ---------------------------------------------------------------------------


class _Clock:
    """A hand-advanced monotonic clock (seconds), so detection is deterministic
    and the TEST never supplies a silence duration — the monitor derives it."""

    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class _HungFiber:
    """A provider fiber that, on dispose, settles itself and its dependent as the
    cordis reactive graph would — target DISPOSED, the consumer of its key
    PENDING (mirrors test_477_liveness_ceiling._HungFiber)."""

    def __init__(self, drv, target, dependent) -> None:
        self._drv, self._target, self._dependent = drv, target, dependent
        self.state = "ACTIVE"

    async def dispose(self):
        self._drv._settled.append((self._target, "ACTIVE", "DISPOSED", None))
        self._drv._settled.append((self._dependent, "ACTIVE", "PENDING", None))


def _fake_driver(ir, *, fibers=None, resolved=()):
    drv = _Driver.__new__(_Driver)
    drv.ir = ir
    drv.generation = 1
    drv._seq = 0
    drv._events = []
    drv._observing = None
    drv._settled = []
    drv._monitor = None
    drv.FiberState = lambda s: types.SimpleNamespace(name=s)
    drv.fibers = fibers if fibers is not None else {}
    drv.resolved_keys = lambda: set(resolved)
    return drv


# ---------------------------------------------------------------------------
# the observability classifier: observe, or refuse (define, don't over-promise)
# ---------------------------------------------------------------------------


def test_observable_when_the_activation_hangs_only_on_host_boundaries():
    ir = compile_source(HANGABLE, "h.rvl")
    pg = next(c for c in ir["components"] if c["name"] == "PgDatabase")
    assert _hangable_crossing_kinds(pg["body"]) == {"host"}
    status, _ = _liveness_observability(pg)
    assert status == "observable"


def test_blocking_when_the_activation_crosses_into_a_required_service():
    ir = compile_source(BLOCKING, "b.rvl")
    worker = next(c for c in ir["components"] if c["name"] == "Worker")
    assert _hangable_crossing_kinds(worker["body"]) == {"req"}
    status, reason = _liveness_observability(worker)
    assert status == "blocking"
    assert "peer provider" in reason


def test_shared_clock_when_the_component_carries_a_multirealm_route():
    # a synthetic component with a `routes` bind — the classifier is pure, so it
    # is exercised directly without standing up a full multi-realm composition.
    routed = {"name": "Fan", "liveness_ceiling_ms": 1000,
              "routes": {"w": {"realms": ["w1", "w2"]}},
              "body": [{"step": "emit", "capability": "x", "key": "w"}]}
    status, reason = _liveness_observability(routed)
    assert status == "shared-clock"
    assert "realms this process does not drive" in reason


# ---------------------------------------------------------------------------
# enrolment: watch the observable, refuse the rest (and say why)
# ---------------------------------------------------------------------------


def test_enrols_only_the_observable_declared_ceiling_provider():
    drv = _fake_driver(compile_source(HANGABLE, "h.rvl"))
    mon = _LivenessMonitor(drv, clock=_Clock())
    # PgDatabase declares an observable ceiling; UserCache declares none.
    assert mon.watching == {"PgDatabase"}
    assert mon.refused == []


def test_refuses_a_blocking_ceiling_and_records_the_reason():
    drv = _fake_driver(compile_source(BLOCKING, "b.rvl"))
    mon = _LivenessMonitor(drv, clock=_Clock())
    assert mon.watching == set()  # nothing observable is watched
    assert [r["component"] for r in mon.refused] == ["Worker"]
    assert mon.refused[0]["status"] == "blocking"


# ---------------------------------------------------------------------------
# detection: expire a genuinely silent provider, computing the duration itself
# ---------------------------------------------------------------------------


def test_expires_a_silent_provider_without_the_test_supplying_a_duration():
    ir = compile_source(HANGABLE, "h.rvl")
    drv = _fake_driver(ir)
    drv.fibers = {
        "PgDatabase": _HungFiber(drv, "PgDatabase", "UserCache"),
        "UserCache": types.SimpleNamespace(state="ACTIVE"),
    }
    clock = _Clock()
    mon = _LivenessMonitor(drv, clock=clock)

    # inside the ceiling: the monitor computes a sub-ceiling silence, fires nothing.
    clock.advance(0.5)
    assert asyncio.run(mon.poll()) == []
    assert drv._events == []

    # past the 1000ms ceiling: the MONITOR (not the test) derives silent_ms and
    # invokes the producer. The cascade roots at the liveness expiry.
    clock.advance(0.8)  # 1.3s silent total
    assert asyncio.run(mon.poll()) == ["PgDatabase"]

    trace = wr.Trace(drv._events)
    root = trace.cause_chain("PgDatabase")
    assert root[-1].cause["kind"] == wr.LIVENESS_EXPIRED
    assert root[-1].cause["ceilingMs"] == 1000
    assert root[-1].cause["silentMs"] >= 1000      # the monitor's own measurement
    dep = trace.cause_chain("UserCache")
    assert [f.component for f in dep] == ["UserCache", "PgDatabase"]
    assert dep[-1].cause["kind"] == wr.LIVENESS_EXPIRED

    # the provider is dropped after firing — no repeat expiry on the next pass.
    assert mon.watching == set()
    assert asyncio.run(mon.poll()) == []


def test_a_provider_that_answers_before_its_ceiling_is_not_expired():
    drv = _fake_driver(compile_source(HANGABLE, "h.rvl"),
                       fibers={"PgDatabase": types.SimpleNamespace(state="ACTIVE")})
    clock = _Clock()
    mon = _LivenessMonitor(drv, clock=clock)
    # it reached ACTIVE within the window — the ACTIVE transition ends the watch.
    mon.note_transition("PgDatabase", "ACTIVE")
    assert mon.watching == set()
    clock.advance(10.0)
    assert asyncio.run(mon.poll()) == []
    assert drv._events == []


def test_progress_before_the_ceiling_resets_the_silence_clock():
    drv = _fake_driver(compile_source(HANGABLE, "h.rvl"),
                       fibers={"PgDatabase": _HungFiber(None, "PgDatabase", "x")})
    clock = _Clock()
    mon = _LivenessMonitor(drv, clock=clock)
    clock.advance(0.9)
    mon.note_transition("PgDatabase", "PENDING")  # a waypoint: a sign of life
    clock.advance(0.9)                             # 0.9s since the last move
    assert asyncio.run(mon.poll()) == []           # not silent past 1s yet


def test_an_unrelated_owner_without_a_ceiling_is_never_watched_or_expired():
    drv = _fake_driver(compile_source(HANGABLE, "h.rvl"),
                       fibers={"UserCache": types.SimpleNamespace(state="ACTIVE")})
    clock = _Clock()
    mon = _LivenessMonitor(drv, clock=clock)
    assert "UserCache" not in mon.watching
    clock.advance(100.0)
    assert asyncio.run(mon.poll()) == []


def test_a_provider_waiting_on_an_unmet_peer_is_not_charged_its_silence():
    ir = compile_source(NEEDS_PEER, "n.rvl")
    # Sink is observable (its own host crossing) but its require `log` is unmet.
    drv = _fake_driver(ir, resolved=())  # nothing resolved: the peer is not up
    drv.fibers = {"Sink": _HungFiber(drv, "Sink", "x")}
    clock = _Clock()
    mon = _LivenessMonitor(drv, clock=clock)
    assert "Sink" in mon.watching
    clock.advance(5.0)
    # silent past its ceiling, but its own requirement is unmet — the silence is
    # the peer's, so it is left enrolled and NOT expired.
    assert asyncio.run(mon.poll()) == []
    assert "Sink" in mon.watching
    assert drv._events == []

    # once the peer resolves, the same silence IS its own — now it expires.
    drv.resolved_keys = lambda: {"log"}
    assert asyncio.run(mon.poll()) == ["Sink"]


# ---------------------------------------------------------------------------
# lifecycle: the background task settles on stop, no watcher left behind
# ---------------------------------------------------------------------------


def test_start_arms_no_task_when_nothing_is_enrolled():
    drv = _fake_driver(compile_source("service S { fn p() -> Int }\n"
                                      "component P provides s: S "
                                      "{ provide s { fn p() = 1 } }", "p.rvl"))
    mon = _LivenessMonitor(drv, clock=_Clock())
    mon.start()
    assert mon._task is None  # a ceiling-free composition arms no watcher


def test_stop_cancels_and_awaits_the_task_leaving_no_watcher():
    drv = _fake_driver(compile_source(HANGABLE, "h.rvl"),
                       fibers={"PgDatabase": types.SimpleNamespace(state="ACTIVE")})

    async def scenario():
        mon = _LivenessMonitor(drv, clock=_Clock(), interval=0.001)
        mon.start()
        task = mon._task
        assert task is not None and not task.done()
        await asyncio.sleep(0.005)   # let it poll a few times
        await mon.stop()
        assert mon._task is None
        assert task.done()           # the watcher settled, not left running
        assert mon.watching == set()
        # idempotent: a second stop is safe.
        await mon.stop()

    asyncio.run(scenario())
