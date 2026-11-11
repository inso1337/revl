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
