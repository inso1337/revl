"""A `Session.call` turns the loop only while the call left work to run
(issue #3379).

Every call ended with twenty turns of the event loop, whatever the method did.
A call that started nothing now ends after one, and a call whose method spawns
work that takes many turns still sees all of it settle before it returns.
"""

import copy
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
_BACKEND = ROOT / "backends" / "python"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from revl.compiler import compile_source  # noqa: E402

needs_cordis = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="a call needs a live cordis-py session; install it with "
           "`sh backends/python/setup.sh` and run under its venv",
)

ECHO = """
service Echo { fn same(n: Int) -> Int }
component Plain provides echo: Echo {
  provide echo { fn same(n) = n }
}
"""

# `hire` spawns a Worker whose activation goes on after the spawn returns:
# two jobs of several turns each, then a map and a timer. Only the turns after
# the method returned can finish it.
SPAWN = """
service Job { fn run() -> Int }
service Log { emission fn write(s: Str) }
service Boss { emission async fn hire() -> Int }
component Sink provides log: Log {
  provide log { fn write(s) { } }
}
component Worker requires log: Log provides job: Job {
  let m = effect Map.new() undo m.drop()
  await Job.run("first")
  await Job.run("second")
  let n = effect Map.new() undo n.drop()
  after 5s { emit log.write("late") }
  provide job { fn run() = 0 }
}
component Manager provides boss: Boss {
  provide boss {
    async fn hire() {
      let w = effect spawn Worker with { } undo w.dispose()
      return 1
    }
  }
}
"""


def _session(source, name):
    from revl.mcp.session import Session

    session = Session()
    session.load(copy.deepcopy(compile_source(source, name)))
    return session


def _count_turns(session):
    """Count the passes of the session's loop (`_run_once`) from here on."""
    loop = session._loop
    turns = [0]
    run_once = loop._run_once

    def counted():
        turns[0] += 1
        run_once()

    loop._run_once = counted
    return turns


@needs_cordis
def test_a_call_that_starts_nothing_ends_in_a_few_turns():
    session = _session(ECHO, "echo.rvl")
    try:
        session.call("echo", "same", [0], raw=True)   # warm
        turns = _count_turns(session)
        for n in range(10):
            assert session.call("echo", "same", [n], raw=True)["result"] == n
        # one turn runs the call, one settles it, one stops the loop: twenty
        # settling turns made it 22 a call
        assert turns[0] <= 10 * 4, turns[0]
    finally:
        session.unload()


@needs_cordis
def test_work_a_call_spawns_still_settles_before_it_returns():
    import runtime

    session = _session(SPAWN, "spawn.rvl")
    try:
        timers = runtime.Clock.pending()
        out = session.call("boss", "hire", [])
        assert out["result"] == 1
        trace = [f"{e['subject']} {e['detail']}".rstrip() for e in out["trace"]]
        assert "job.run first done" in trace, trace
        assert "job.run second done" in trace, trace
        assert sum(1 for line in trace if line.endswith(".new")) == 2, trace
        assert runtime.Clock.pending() == timers + 1, "the Worker armed its timer"
    finally:
        session.unload()


# ---------------------------------------------------------------------------
# What the loop is still waiting on when no callback is ready: a job handed to
# a worker thread (`asyncio.to_thread`, what an async model method uses) and a
# real loop timer due within the next couple of milliseconds. The twenty turns
# let both finish; the early stop must not cut them off.
# ---------------------------------------------------------------------------

MODEL = """
model role local on_device reaches []
service Model { emission[model.local] async fn say(a: Str, b: Int) -> Str }
service Loop { emission fn run(p: Str) -> Int }
component Agent requires model: Model provides agent: Loop {
  provide agent { fn run(p) { return 1 } }
}
"""


def _fake_model_host(tmp_path):
    """An async model host whose adapter answers on its worker thread without
    leaving the process."""
    from revl.compiler import compile_files
    from revl.parser import Parser
    from revl.providers import build_hosts, parse_config, placement_of_program
    from revl.providers.completion import Completion

    path = tmp_path / "model.rvl"
    path.write_text(MODEL)
    ir = compile_files([str(path)])
    config = parse_config({"roles": {"local": {
        "provider": "openai-compatible", "base_url": "http://127.0.0.1:9/v1",
        "model": "m"}}})
    placement = placement_of_program(Parser(MODEL, "model.rvl").parse())
    host = build_hosts(ir, placement, config)["model"]
    for _, adapter in host._revl_routes.values():
        adapter.complete = lambda request: Completion(text="from the fake")
    return host


@needs_cordis
def test_an_async_model_call_a_spawn_awaits_still_settles(tmp_path):
    """A task the call left behind awaits an async model method, so its next
    step waits on a worker thread with nothing in the loop's ready queue.
    The flush keeps turning until the thread is done and the task finished,
    as the twenty turns did. Counted over 20 runs: a thread can be slower
    than twenty turns on a loaded machine, under either flush."""
    import asyncio

    host = _fake_model_host(tmp_path)
    session = _session(ECHO, "echo.rvl")
    driver = session._driver
    try:
        async def spawn_and_flush():
            answers: list = []

            async def activation():
                answers.append(await host.say("x", 2))

            asyncio.ensure_future(activation())
            await driver._flush()
            return answers

        settled = sum(session._run(spawn_and_flush()) == ["from the fake"]
                      for _ in range(20))
        assert settled >= 15, f"{settled}/20 settled before the flush returned"
    finally:
        session.unload()


def test_a_thread_job_in_flight_keeps_the_loop_runnable():
    import asyncio
    import threading

    from revl.run import _loop_has_runnable, _track_executor_work

    async def scenario():
        loop = asyncio.get_running_loop()
        _track_executor_work(loop)
        release = threading.Event()
        job = asyncio.ensure_future(asyncio.to_thread(release.wait, 10))
        await asyncio.sleep(0)               # the job is submitted
        while_running = _loop_has_runnable(loop)
        release.set()
        await job
        await asyncio.sleep(0)               # its done callbacks have run
        return while_running, _loop_has_runnable(loop)

    assert asyncio.run(scenario()) == (True, False)


def test_a_timer_due_within_the_horizon_keeps_the_loop_runnable():
    import asyncio

    from revl.run import _loop_has_runnable, _track_executor_work

    async def scenario():
        loop = asyncio.get_running_loop()
        _track_executor_work(loop)         # as `_load` and `_flush` do
        soon = loop.call_later(0.001, lambda: None)
        near = _loop_has_runnable(loop)
        soon.cancel()
        late = loop.call_later(60, lambda: None)
        far = _loop_has_runnable(loop)
        late.cancel()
        return near, far

    assert asyncio.run(scenario()) == (True, False)


def test_the_flush_waits_for_a_near_timer_as_the_twenty_turns_did():
    """A 1 ms loop timer either fires during the flush, or the flush takes
    all twenty turns waiting for it: it never stops early with the timer
    pending."""
    import asyncio

    from revl.run import _Driver

    driver = object.__new__(_Driver)

    async def scenario():
        loop = asyncio.get_running_loop()
        fired: list = []
        turns = [0]
        run_once = loop._run_once

        def counted():
            turns[0] += 1
            run_once()

        loop._run_once = counted
        loop.call_later(0.001, fired.append, 1)
        await driver._flush()
        del loop._run_once
        return bool(fired), turns[0]

    for _ in range(20):
        fired, turns = asyncio.run(scenario())
        assert fired or turns >= 20, (fired, turns)


def test_the_flush_turns_to_the_cap_while_a_thread_job_is_held():
    """Deterministic: the thread job waits on an Event that is only released
    after the flush returns, so the job is in flight for every turn. The
    flush must take all twenty turns, not stop at the first empty ready
    queue."""
    import asyncio
    import threading

    from revl.run import _FLUSH_TURNS, _Driver

    driver = object.__new__(_Driver)

    async def scenario():
        loop = asyncio.get_running_loop()
        release = threading.Event()
        await driver._flush()                # installs the executor count
        job = asyncio.ensure_future(asyncio.to_thread(release.wait, 10))
        turns = [0]
        run_once = loop._run_once

        def counted():
            turns[0] += 1
            run_once()

        loop._run_once = counted
        try:
            await driver._flush()
        finally:
            del loop._run_once
            release.set()
        await job
        return turns[0]

    assert asyncio.run(scenario()) >= _FLUSH_TURNS
