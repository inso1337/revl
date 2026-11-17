"""State hand-off on the TypeScript tier (item 53, docs/state-handoff.md).

The runtime half, capture, restore, rollback, is executed on real cordis in
`tests/handoff_swap.test.ts`. These toolchain-free checks pin the emitter and
the gate side of the same contract:

  * a component that declares a `handoff` carries the declaration on its
    emitted object and records its stateful acquisitions on the activation
    frame (`$revl_frame.track(...)`);
  * a composition with no `handoff` emits neither, so the pre-item-53 output is
    unchanged;
  * the SAME admission gate the py tier uses refuses a successor whose declared
    state shape cannot hold the predecessor's, the refuse-don't-drop rule, so
    a refused shape never reaches the runtime at all.

Run with:
    .venv/bin/pytest backends/typescript/test_handoff_ts.py -q
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent
ROOT = BACKEND.parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import RevlError, compile_source  # noqa: E402


def _load_ts_emit():
    spec = importlib.util.spec_from_file_location(
        "revl_ts_emit_handoff", BACKEND / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _cache(state: str, *, component: str = "Cache", version: int = 1) -> str:
    return f"""
service Store {{
  fn get(k: Str) -> Opt[Str]
  fn put(k: Str, v: Str)
  fn version() -> Int
}}
component {component} provides cache: Store {{
  handoff cache: {state}
  let m = effect Map.new() undo m.drop()
  provide cache {{
    fn get(k) = m.get(k)
    fn put(k, v) {{ effect m.insert(k, v) undo m.remove(k) }}
    fn version() = {version}
  }}
}}
"""


def test_handoff_is_emitted_on_the_component_and_tracked_on_the_frame():
    """The emitted component carries the declaration, and each declared
    stateful acquisition is tracked, in acquisition order, so a swap can
    capture the vector from the live provider before teardown."""
    m = _load_ts_emit()
    out = m.emit(compile_source(_cache("Map[Str, Str]"), "cache.rvl"))
    assert 'handoff: { key: "cache", type: "Map[Str, Str]" },' in out
    assert "$revl_frame.track(m)" in out
    # the resource is still bracketed for teardown, after it is tracked
    assert out.index("$revl_frame.track(m)") < out.index('"drop", () => m.drop()')


def test_no_handoff_emits_nothing_new():
    """Inertness: a real composition whose provider holds a Map but declares no
    hand-off emits no declaration and no tracking, so its module is unchanged
    from before item 53."""
    m = _load_ts_emit()
    ir = json.loads(
        (BACKEND / "tests" / "fixtures" / "user_cache.ir.json")
        .read_text(encoding="utf-8"))
    out = m.emit(ir)
    assert "handoff:" not in out
    assert ".track(" not in out


def test_incompatible_declared_shape_is_refused_at_admission():
    """The declared-shape gate: a successor that accepts `Map[Str, Int]` for a
    provider exporting `Map[Str, Str]` is refused when the candidate is
    admitted against the running manifest, so a refused shape never reaches a
    swap and the predecessor keeps serving."""
    running = compile_source(_cache("Map[Str, Str]"), "cache.rvl")
    with pytest.raises(RevlError, match="state hand-off on `cache` differs"):
        compile_source(_cache("Map[Str, Int]", component="CacheV2", version=2),
                       "cache_v2.rvl", manifest=running, replacing=("Cache",))
    # the compatible successor is admitted, and is what the runtime test drives
    admitted = compile_source(_cache("Map[Str, Str]", component="CacheV2", version=2),
                              "cache_v2.rvl", manifest=running, replacing=("Cache",))
    assert admitted["components"][0]["handoff"] == {
        "key": "cache", "type": "Map[Str, Str]"}
