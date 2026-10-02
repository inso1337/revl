"""An armed E-Stop stops every crossing BEFORE its host body runs (issue #1504).

WHAT WAS WRONG. With the latch armed, a direct `emit extern(..)` in a
provided operation still fired: `runtime.extern_emit` never checked the halt.
A witnessed effect was refused, but only when its inverse was registered,
after its host body had already run. An expression-position emission
(`let a = emit put(..)`) is a bare host call and checked nothing either.

WHAT HOLDS NOW. `_estop_poll` is the one read: it engages a halt an operator
armed through the latch file, and `_estop_check` raises on it. It is called
before the host body on every crossing path: the `estop_gated` decorator the
emitter puts on every `emission`, `witnessed` and `acquire` extern (so the
position of the call does not matter), `extern_emit` (before the crossing is
recorded), a witnessed inverse's replay, a compensation's run, a deferred
emission's flush, and the paths that were already gated.

Each test arms the latch after the composition is up, attempts one crossing
kind, and checks that the host body's counter did not move and that the
refusal names the crossing. They drive a live cordis-py composition; without
the pinned `cordis` fork they SKIP, and a skip is not a pass.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from revl.compiler import compile_source

_ROOT = Path(__file__).resolve().parents[1]
_BACKEND = _ROOT / "backends" / "python"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

needs_cordis = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="the crossings run against a live cordis-py composition; install "
           "the pinned fork with `sh backends/python/setup.sh`")


def _count(tag: str, ret: str = "None") -> str:
    """A host body that appends `tag` to the counter file, then returns."""
    return ("@py {\n    import os\n"
            "    with open(os.environ['REVL_E_COUNT'], 'a', encoding='utf-8') as f:\n"
            f"        f.write({tag!r} + chr(10))\n"
            f"    return {ret}\n}}")


SOURCE = f"""
type Stash = {{ path: Str, bak: Str }}
type FsError = {{ code: Str }}
extern emission fn shout(t: Str) -> Unit = {_count('shout')}
extern emission fn put(t: Str) -> Int = {_count('put', '1')}
extern pure fn unstash(w: Stash) -> Unit = {_count('unstash')}
extern witnessed[fs] fn stash(p: Str) -> Result[Stash, FsError] undo unstash(result)
  = @py {{
    import os
    with open(os.environ['REVL_E_COUNT'], 'a', encoding='utf-8') as f:
        f.write('stash' + chr(10))
    return Ok({{'path': p, 'bak': p + '.bak'}})
}}
extern pure fn offset(t: Str) -> Unit = {_count('offset')}
extern emission fn note(t: Str) -> Unit = {_count('note')}
extern emission deferred fn deliver(t: Str) = {_count('deliver')}
service Ops {{
  emission fn direct(t: Str)
  emission fn bound(t: Str) -> Int
  emission fn witnessed(p: Str)
  emission fn compensated(t: Str)
  emission fn later(t: Str)
}}
component Agent provides ops: Ops {{
  provide ops {{
    fn direct(t) {{ emit shout(t) }}
    fn bound(t) {{
      let a = emit put(t)
      return a
    }}
    fn witnessed(p) {{ effect stash(p) }}
    fn compensated(t) {{ emit note(t) compensate offset(t) }}
    fn later(t) {{ emit deliver(t) }}
  }}
}}
"""


@pytest.fixture
def world(tmp_path, monkeypatch):
    count = tmp_path / "count.log"
    latch = tmp_path / "estop.latch"
    monkeypatch.setenv("REVL_E_COUNT", str(count))
    from revl.mcp.session import Session
    session = Session()
    session.load(compile_source(SOURCE, "estop_gate_1504.rvl"))
    runtime = session._driver.runtime
    runtime.arm_estop_latch(str(latch))

    def arm():
        latch.write_text(json.dumps({"reason": "drill", "operator": "ops"}),
                         encoding="utf-8")

    def calls() -> list:
        return count.read_text(encoding="utf-8").splitlines() if count.exists() else []

    yield {"session": session, "runtime": runtime, "arm": arm, "calls": calls}
    runtime.arm_estop_latch(None)
    runtime.clear_estop()
    session._reset()


def _refused(world, method: str, args: list) -> Exception:
    with pytest.raises(Exception) as caught:
        world["session"].call("ops", method, args)
    return caught.value


@needs_cordis
def test_a_direct_extern_emit_is_refused_before_its_body(world):
    world["arm"]()
    error = _refused(world, "direct", ["x"])
    assert "E-STOP engaged" in str(error) and "`shout`" in str(error)
    assert world["calls"]() == []


@needs_cordis
def test_an_expression_position_emission_is_refused_before_its_body(world):
    world["arm"]()
    error = _refused(world, "bound", ["x"])
    assert "E-STOP engaged" in str(error) and "`put`" in str(error)
    assert world["calls"]() == []


@needs_cordis
def test_a_witnessed_effect_is_refused_before_its_body(world, tmp_path):
    world["arm"]()
    error = _refused(world, "witnessed", [str(tmp_path / "f")])
    assert "E-STOP engaged" in str(error) and "`stash`" in str(error)
    assert world["calls"]() == []   # on main the body ran: ['stash']


@needs_cordis
def test_an_owed_compensation_is_stranded_not_run(world):
    """The call crossed before the halt; the abort after it must not run the
    compensation, and must name it as stranded."""
    world["session"].call("ops", "compensated", ["x"])
    assert world["calls"]() == ["note"]
    world["arm"]()
    frame = next(f for f in world["runtime"]._live_frames() if f.name == "Agent")
    frame.abort()
    world["session"].unload()
    assert world["calls"]() == ["note"]       # `offset` never ran
    stranded = [r for r in world["runtime"].estop_residue()
                if r.get("kind") == "estop-stranded"]
    assert any(r.get("method") == "offset" for r in stranded), stranded


@needs_cordis
def test_a_witnessed_inverse_is_stranded_not_replayed(world, tmp_path):
    world["session"].call("ops", "witnessed", [str(tmp_path / "f")])
    assert world["calls"]() == ["stash"]
    world["arm"]()
    frame = next(f for f in world["runtime"]._live_frames() if f.name == "Agent")
    frame.abort()
    world["session"].unload()
    assert world["calls"]() == ["stash"]      # `unstash` never ran
    stranded = [r for r in world["runtime"].estop_residue()
                if r.get("kind") == "estop-stranded"]
    assert any(r.get("method") == "unstash" for r in stranded), stranded


@needs_cordis
def test_a_deferred_emission_is_refused_at_its_flush(world):
    """Queued before the halt; the commit's flush is refused, the host body
    never runs, and the refusal is recorded as flush residue."""
    world["session"].call("ops", "later", ["x"])
    assert world["calls"]() == []             # deferred: nothing fired yet
    world["arm"]()
    manifest = world["session"].commit()
    result = world["session"].commit_confirm(manifest["manifest"]["hash"]
                                             if "manifest" in manifest
                                             else manifest["hash"])
    assert world["calls"]() == []
    assert result["flushResidue"], result
    assert "E-STOP engaged" in result["flushResidue"][0]["message"]


def test_every_boundary_crossing_extern_class_is_gated_and_no_pure_one():
    from _backend_import import backend_emitter
    code = backend_emitter("python").emit(
        compile_source(SOURCE, "estop_gate_1504.rvl"))
    for name in ("shout", "put", "stash", "note"):
        assert f"@_revl_estop_gated({name!r})\ndef {name}(" in code
    for name in ("unstash", "offset"):
        assert f"@_revl_estop_gated({name!r})" not in code


def test_a_remote_bridge_call_is_refused_before_it_is_sent():
    """A call through a placement seam is a crossing too. `_Client.call`
    refuses under a halt before it encodes or sends anything: this client has
    no socket, so reaching the send would fail differently."""
    import bridge
    import runtime as rt
    rt.clear_estop()
    try:
        rt.estop("drill", operator="ops")
        client = object.__new__(bridge._Client)
        with pytest.raises(rt.EstopHalted, match="`db.execute`"):
            client.call("db", "execute", ["x"])
    finally:
        rt.clear_estop()


def test_a_component_free_document_stays_importable_without_the_runtime():
    """A document with no component runs no activation, so nothing the runtime
    governs can cross its externs: they are host code a test drives directly,
    and the emitted module must import without `runtime` on the path (the
    shape `tests/test_async_arrow_polymorphism_phase2.py` runs standalone)."""
    from _backend_import import backend_emitter
    code = backend_emitter("python").emit(compile_source(
        "extern emission fn shout(t: Str) -> Str = @py { return t }\n"
        'test "t" { let r = shout("x")\n assert r == "x" }\n', "free.rvl"))
    assert "estop_gated" not in code
