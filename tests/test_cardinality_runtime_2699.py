"""The branching-recursion bound against a real run (issue #2699).

`revl.cardinality` certifies a finite crossing bound for a self-recursion with
several self-calls per path and a small literal fuel. The bound is a
certificate: no run may cross the boundary more times than it says. These tests
boot the composition on the py runtime, run it, and count the crossings the
provider actually served, then compare that count with the reported bound for
fuel 1, 2 and 3.

The provider counts its own calls: each `complete` inserts one key into an
effect-owned map, and `tally.count()` reads the map size after the run.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.cardinality import cardinality  # noqa: E402
from revl.compiler import compile_source  # noqa: E402

needs_runtime = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="cordis-py runtime not installed (sh backends/python/setup.sh)")

HEAD = """
service Model { emission[model] fn complete(m: List[Str]) -> Str }
service Loop { emission[model] fn run(s: Str) -> Str }
service Tally { fn count() -> Int }

component CountingModel provides model: Model, tally: Tally {
  let store = effect Map.new() undo store.drop()
  provide model { fn complete(m) {
    let k = `n-${store.size()}`
    effect store.insert(k, "x")
    undo store.remove(k)
    return "ok"
  } }
  provide tally { fn count() = store.size() }
}
"""


def _tree(base: str, calls: int) -> str:
    recur = "\n".join(
        ("  return" if i == calls - 1 else f"  let a{i} =") + " tree(step, msgs, n - 1)"
        for i in range(calls))
    return f"""
fn tree(step: (List[Str]) -> Str, msgs: List[Str], n: Int) -> Str {{
  if (n <= 0) {{ return {base} }}
  let x = step(msgs)
{recur}
}}"""


def _source(fuel: int, base: str, calls: int) -> str:
    return HEAD + _tree(base, calls) + f"""
component Agent requires model: Model provides agent: Loop {{
  provide agent {{ fn run(s) -> Str {{
    let msgs = [s]
    return tree(msgs2 => emit model.complete(msgs2), msgs, {fuel})
  }} }}
}}
"""


def _call(server, tool: str, arguments: dict) -> dict:
    return server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                          "params": {"name": tool, "arguments": arguments}}
                         )["result"]["structuredContent"]


def _real_crossings(src: str) -> int:
    from revl.mcp import server
    try:
        loaded = _call(server, "revl_load", {"source": src})
        assert loaded["ok"] is True, loaded
        ran = _call(server, "revl_call", {"key": "agent", "method": "run", "args": ["go"]})
        assert ran["ok"] is True, ran
        counted = _call(server, "revl_call", {"key": "tally", "method": "count", "args": []})
        assert counted["ok"] is True, counted
        return counted["result"]
    finally:
        if server.SESSION.loaded:
            server.SESSION.unload()


def _reported_bound(src: str) -> int:
    entry = cardinality(compile_source(src, "t.rvl"))["Agent"]["per_capability"]["model"]
    assert entry["kind"] == "bounded", entry
    return entry["bound"]


# (base arm, self-calls per path, {fuel: crossings a run makes})
# A leaf that returns a literal crosses zero times: 2^N - 1 crossings.
# A leaf that also steps crosses once: 2^(N+1) - 1, exactly the bound.
# Three self-calls with a crossing leaf: (3^(N+1) - 1) / 2, exactly the bound.
SHAPES = {
    "two_calls_silent_leaf": ('"stop"', 2, {1: 1, 2: 3, 3: 7}),
    "two_calls_crossing_leaf": ("step(msgs)", 2, {1: 3, 2: 7, 3: 15}),
    "three_calls_crossing_leaf": ("step(msgs)", 3, {1: 4, 2: 13}),
}


@needs_runtime
@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_a_run_never_crosses_more_than_the_reported_bound(shape):
    base, calls, expected = SHAPES[shape]
    for fuel, crossings in expected.items():
        src = _source(fuel, base, calls)
        bound = _reported_bound(src)
        real = _real_crossings(src)
        # the run is what the fixture says it is (a control on the counter)
        assert real == crossings, (shape, fuel, real)
        assert real <= bound, (shape, fuel, real, bound)


@needs_runtime
def test_fuel_three_two_calls_one_crossing_per_call_is_fifteen_and_tight():
    """The issue's acceptance shape, with the leaf crossing too, so every one
    of the 15 activations crosses once: the bound is reached, not exceeded."""
    src = _source(3, "step(msgs)", 2)
    assert _reported_bound(src) == 15
    assert _real_crossings(src) == 15
