"""A session keeps what its teardown check needs, not every host event of its
life (issue #3382).

The driver used to append one string per host-stub event to a list read only
at teardown, so a session whose component writes a store on each call grew by
one string a write until it was unloaded. It now keeps the handles still open,
and the teardown report names a leaked one with the same text as before.
"""

import copy
import re
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
    reason="a teardown needs a live cordis-py session; install it with "
           "`sh backends/python/setup.sh` and run under its venv",
)

# the repro of the issue: five keys, written once a call
MEMORY = """
service Mind { fn get(k: Int) -> Int  emission fn set(k: Int, v: Int) }
component Memory provides mind: Mind {
  handoff mind: Map[Int, Int]
  let m = effect Map.new() undo m.drop()
  provide mind {
    fn get(k) = m.get(k) ?? 0
    fn set(k, v) {
      let was = m.get(k)
      effect m.insert(k, v) undo match was { Some(w) => m.insert(k, w), None => m.remove(k) }
    }
  }
}
"""

# a pool opened at activation whose close is made to raise at teardown
POOL = """
service S { fn ping() -> Int }
component Db provides s: S {
  let p = effect Pool.open("db://orders", 2) undo p.close()
  provide s { fn ping() = 1 }
}
"""


def _loaded(source, name):
    from revl.mcp.session import Session

    session = Session()
    session.load(copy.deepcopy(compile_source(source, name)))
    return session


def _kept_host_events(driver) -> int:
    """How many host-event strings the driver holds: every string that names
    a host-stub verb, in the containers its attributes reach (and in the
    attributes of the revl objects among them)."""
    seen: set = set()
    count = 0
    stack = list(vars(driver).values())
    while stack:
        value = stack.pop()
        if id(value) in seen:
            continue
        seen.add(id(value))
        if isinstance(value, str):
            head = value.partition(" ")[0]
            if head.startswith(("map#", "pool#")) and "." in head:
                count += 1
        elif isinstance(value, dict):
            stack.extend(value.keys())
            stack.extend(value.values())
        elif isinstance(value, (list, tuple, set, frozenset)):
            stack.extend(value)
        elif type(value).__module__.startswith("revl.") and hasattr(value, "__dict__"):
            stack.extend(vars(value).values())
    return count


@needs_cordis
def test_host_event_memory_stays_bounded_over_many_calls():
    session = _loaded(MEMORY, "memory.rvl")
    try:
        driver = session._driver
        for n in range(50):
            session.call("mind", "set", [n % 5, n], raw=True)
        after_50 = _kept_host_events(driver)
        for n in range(500):
            session.call("mind", "set", [n % 5, n], raw=True)
        after_550 = _kept_host_events(driver)
        assert after_550 == after_50, (after_50, after_550)
        assert after_550 <= 1, after_550
        assert session.call("mind", "get", [3], raw=True)["result"] == 498
    finally:
        report = session.unload()
    assert report["checks"]["hostResources"] is True
    assert report["detail"]["unreleased"] == []


@needs_cordis
def test_a_leaked_handle_is_still_reported_after_many_calls(monkeypatch):
    import runtime

    session = _loaded(POOL, "db.rvl")
    for _ in range(200):
        assert session.call("s", "ping", [], raw=True)["result"] == 1

    def fail(self):
        raise RuntimeError("injected: the pool would not close")

    monkeypatch.setattr(runtime.Pool, "close", fail)
    report = session.unload()
    assert report["checks"]["hostResources"] is False
    assert report["noResidue"] is False
    # the text the teardown report has always used for an unpaired open
    [unreleased] = report["detail"]["unreleased"]
    assert re.fullmatch(r"pool#\d+ \(open\(\) with no close\(\)\)", unreleased), \
        unreleased
