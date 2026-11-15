"""A long-running component keeps one undo record per key, not one per call
(issue #3304, slice 1: the python runtime).

WHAT WAS WRONG. Every call of a provide method that writes through
`effect m.insert(k, v) undo ...` adopted one more cordis effect on the
component's fiber, and nothing let go of it before the frame was disposed. A
game that calls `tick` every frame kept one record per frame for the life of
the generation: memory grew per call and a swap walked every record.

WHAT HOLDS NOW. When a bracket's forward write journals exactly one `(map,
key)` and its `undo` is an ABSOLUTE table inverse (`insert -> remove`, the
#1980 `match prev` restore) on a literal or named key, and the frame already holds such a record for that key with only
records of the same kind adopted since, the new record is released without
running. The kept, older record restores the value the key held before it,
which is the value every newer record's replay would have been overwritten
with, so the world, the `NotReversed` verdict and every other key are the
same. Anything else adopted in between (an asserted, declared or computed
inverse, a method `let-effect`, which is also how the `insert_if_absent`
CAS is written, so it is never coalesced) ends the run, and a recorded timeline (whose
`step_back` replays its own per-step list) is never coalesced.

The #2640 computed list-append inverse is `table` too but RELATIVE (it reads
the list at undo time and drops one item), so N of them are not one: it is
never coalesced, and the test below shows that coalescing it would be wrong.

Every executed test drives a live cordis-py composition; without the pinned
`cordis` fork they SKIP, and a skip is not a pass.
"""

from __future__ import annotations

import gc
import importlib.util
import sys
from pathlib import Path

import pytest

from revl.compiler import compile_source

ROOT = Path(__file__).resolve().parents[1]

needs_cordis = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="the records are cordis effects on a live composition; install "
           "the pinned fork with `sh backends/python/setup.sh`")

GAME = """service Game {
  fn tick(dt: Float, dir: Float)
  fn x() -> Float
}
component World provides game: Game {
  handoff game: Map[Str, Float]
  let s = effect Map.new() undo s.drop()
  provide game {
    fn tick(dt, dir) {
      let was = s.get("x")
      let x = match was { Some(v) => v, None => 0.0 }
      effect s.insert("x", x + dir * 60.0 * dt)
      undo match was { Some(v) => s.insert("x", v), None => s.remove("x") }
    }
    fn x() = match s.get("x") { Some(v) => v, None => 0.0 }
  }
}
"""

KEYS = """service Kv {
  fn set(k: Str, v: Str) -> Str
  fn get(k: Str) -> Str
}
component Store provides kv: Kv {
  let store = effect Map.new() undo store.drop()
  provide kv {
    fn set(k, v) {
      effect store.insert(k, v)
      undo   store.remove(k)
      return v
    }
    fn get(k) = store.get(k) ?? ""
  }
}
"""

# the #3381 repro: `undo m.remove(k)` is the inverse only while `k` was
# absent, so from the second generation on (the keys cross over through the
# handoff) every key is one `NotReversed` a generation
MIND = """service Mind {
  fn get(k: Int) -> Int
  emission fn set(k: Int, v: Int)
}
component Memory provides mind: Mind {
  handoff mind: Map[Int, Int]
  let m = effect Map.new() undo m.drop()
  provide mind {
    fn get(k) = m.get(k) ?? 0
    fn set(k, v) { effect m.insert(k, v) undo m.remove(k) }
  }
}
"""

APPEND = """fn drop_last(xs: List[Str]) -> List[Str] {
  return xs.slice(0, xs.length() - 1)
}
service Ledger {
  fn append(id: Str, msg: Str) -> Str
  fn seed(id: Str) -> Unit
}
component LedgerStore provides ledger: Ledger {
  let store = effect Map.new() undo store.drop()
  provide ledger {
    fn append(id, msg) {
      let known = store.get(id) != None
      effect store.insert(id, (store.get(id) ?? []).push(msg))
      undo known ? store.insert(id, drop_last(store.get(id) ?? [])) : store.remove(id)
      return msg
    }
    fn seed(id) = store.insert(id, ["old"])
  }
}
"""


def _session(source: str):
    from revl.mcp.session import Session  # noqa: PLC0415
    session = Session()
    session.load(compile_source(source, "c.rvl"))
    return session


def _effects() -> int:
    from cordis.fiber import FiberEffect  # noqa: PLC0415
    gc.collect()
    return sum(1 for o in gc.get_objects() if type(o) is FiberEffect)


def _messages(records) -> list:
    return [r.get("error", {}).get("message", "") for r in records or []]


@pytest.fixture
def drops(monkeypatch):
    """The data every host Map held when it was released, in order: the key
    values the teardown left after every undo ran."""
    seen: list = []
    runtime = sys.modules.get("runtime")
    if runtime is None:
        _session(KEYS).unload()      # loads the runtime module
        runtime = sys.modules["runtime"]
    original = runtime.Map.drop

    def drop(self):
        seen.append(dict(self.data))
        return original(self)

    monkeypatch.setattr(runtime.Map, "drop", drop)
    return seen


def _tick(session, n: int) -> None:
    for _ in range(n):
        session.call("game", "tick", [0.5, 1.0])


@needs_cordis
def test_a_restore_bracket_on_one_key_keeps_one_record():
    session = _session(GAME)
    _tick(session, 100)
    at_100 = _effects()
    _tick(session, 400)
    at_500 = _effects()
    assert at_500 == at_100, f"{at_100} effects after 100 calls, {at_500} after 500"
    assert session.call("game", "x", [])["result"] == 15000.0
    report = session.unload()
    assert report["noResidue"] is True


@needs_cordis
def test_distinct_keys_keep_one_record_each():
    session = _session(KEYS)
    before = _effects()
    for i in range(100):
        for k in ("a", "b", "c", "d", "e"):
            session.call("kv", "set", [k, str(i)])
    at_100 = _effects()
    for i in range(300):
        for k in ("a", "b", "c", "d", "e"):
            session.call("kv", "set", [k, str(i)])
    assert _effects() == at_100 == before + 5
    report = session.unload()
    assert report["noResidue"] is True


@needs_cordis
def test_a_swap_with_handoff_restores_each_generation_to_its_own_start(drops):
    """The kept record is the OLDEST: the old generation's map goes back to
    what it held before that generation's first write (absent in the first,
    the carried value in the second), and the carried value crosses over."""
    session = _session(GAME)
    ir = compile_source(GAME, "c.rvl")
    _tick(session, 20)
    session.swap(ir, agent_authored=False)
    assert session.call("game", "x", [])["result"] == 600.0
    _tick(session, 30)
    session.swap(ir, agent_authored=False)
    assert session.call("game", "x", [])["result"] == 1500.0
    assert drops[0] == {}
    assert drops[1] == {"x": 600.0}
    assert session._driver._compensation_residue == []
    report = session.unload()
    assert report["noResidue"] is True
    assert drops[2] == {"x": 1500.0}


@needs_cordis
def test_an_undo_to_the_earlier_generation_restores_the_key(drops):
    from revl.mcp.session import Session  # noqa: PLC0415
    ir = compile_source(GAME, "c.rvl")
    session = Session()
    session.load(ir, origin={"source": GAME})
    _tick(session, 10)
    session.swap(ir, origin={"source": GAME}, agent_authored=False)
    _tick(session, 25)
    session.undo()
    assert drops[0] == {} and drops[1] == {"x": 300.0}
    assert session._driver._compensation_residue == []
    session.unload()


@needs_cordis
def test_an_abort_restores_the_key(drops):
    session = _session(GAME)
    _tick(session, 40)
    report = session.abort()
    assert drops == [{}]
    assert _messages(report.get("compensationResidue")) == []


@needs_cordis
def test_the_3381_repro_still_reports_one_not_reversed_per_key_per_generation():
    """Two writes of every key a generation, so each key has a record that is
    coalesced; the verdict is the one an uncoalesced replay gives."""
    session = _session(MIND)
    ir = compile_source(MIND, "c.rvl")
    counts = []
    for generation in range(3):
        for k in range(5):
            session.call("mind", "set", [k, 10 * generation + k], raw=True)
            session.call("mind", "set", [k, 100 * generation + k], raw=True)
        session.swap(ir, agent_authored=False)
        counts.append(len(session._driver._compensation_residue))
    assert counts == [0, 5, 10]
    messages = _messages(session._driver._compensation_residue)
    for k in range(5):
        assert sum(f"key {k} " in m for m in messages) == 2, messages
    # the second generation's key 3 was carried as 3 and left absent
    assert any("key 3 held 3 before the first bracketed `insert` of it and "
               "absent" in m for m in messages), messages
    session.unload()


@needs_cordis
def test_a_computed_append_inverse_is_never_coalesced(drops):
    """Each #2640 inverse drops ONE item from the list it reads at undo time,
    so the oldest alone would leave the newer items behind: every record is
    kept and the list goes back to what it was."""
    session = _session(APPEND)
    session.call("ledger", "seed", ["k"])
    before = _effects()
    for msg in ("a", "b", "c", "d"):
        session.call("ledger", "append", ["k", msg])
    kept = _effects() - before
    report = session.unload()
    assert drops == [{"k": ["old"]}]
    assert report["noResidue"] is True
    assert kept == 4


def _method_source(source: str) -> str:
    spec = importlib.util.spec_from_file_location(
        "revl_3304_emit_py", ROOT / "backends" / "python" / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.emit(compile_source(source, "c.rvl"))


def test_the_emitter_marks_only_absolute_table_brackets():
    assert "_revl_frame.adopt_bracket(_revl_ctx.effect(" in _method_source(GAME)
    assert "_revl_frame.adopt_bracket(_revl_ctx.effect(" in _method_source(KEYS)
    appended = _method_source(APPEND)
    assert "_revl_frame.adopt_bracket(" not in appended
    assert "_revl_frame.adopt(_revl_ctx.effect(" in appended
