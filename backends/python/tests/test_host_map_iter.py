"""Host `Map.new()` iteration surface — `keys()` and `size()`.

The value-Map builtin table promises `size()`/`keys()` (docs/stdlib-2.0.md
§Map), and the checker accepts them on a host `Map.new()` receiver too — the
py emitter lowers both as plain method calls on the runtime object
(`store.size()`, `store.keys()`). The host `Map` therefore has to carry those
methods, or the emitted component raises ``AttributeError`` at host runtime.
This suite pins both layers: the runtime methods directly, and the whole
emit -> exec -> cordis path that actually runs them.
"""

from __future__ import annotations

import pathlib
import sys

import pytest
from cordis import Context
from cordis.fiber import FiberState

import emit
import runtime as runtime_mod
from conftest import flush, load_module
from runtime import Map

ROOT = pathlib.Path(__file__).resolve().parents[3]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402


# ---------------------------------------------------------------------------
# runtime layer — the methods themselves
# ---------------------------------------------------------------------------


def test_size_counts_entries_and_keys_are_sorted():
    store = Map.new()
    store.insert("b", "2")
    store.insert("a", "1")
    store.insert("c", "3")
    assert store.size() == 3
    # keys come back in ascending canonical Str order (== python str sort),
    # matching the value-Map `keys()` builtin exactly
    assert store.keys() == ["a", "b", "c"]


def test_size_and_keys_track_mutation():
    store = Map.new()
    assert store.size() == 0 and store.keys() == []
    store.insert("k", "v")
    assert store.size() == 1 and store.keys() == ["k"]
    store.remove("k")
    assert store.size() == 0 and store.keys() == []


def test_iteration_is_read_only_and_untraced(trace):
    store = Map.new()  # records map.new
    store.insert("k", "v")  # records map.insert k
    mark = len(trace)
    store.size()
    store.keys()
    assert trace[mark:] == [], "reads must not emit host trace events"


def test_iteration_after_drop_is_refused():
    store = Map.new()
    store.drop()
    with pytest.raises(RuntimeError, match="use-after-free"):
        store.size()
    with pytest.raises(RuntimeError, match="use-after-free"):
        store.keys()


# ---------------------------------------------------------------------------
# end-to-end — emitted component runs the methods on a real cordis Context
# ---------------------------------------------------------------------------

_SOURCE = """
service KV {
  fn count() -> Int
  fn all_keys() -> List[Str]
  emission fn put(key: Str, value: Str)
}

component MemKV provides kv: KV {
  let store = effect Map.new() undo store.drop()

  provide kv {
    fn count()    = store.size()
    fn all_keys() = store.keys()
    fn put(key, value) {
      effect store.insert(key, value)
      undo   store.remove(key)
    }
  }
}
"""


async def test_emitted_host_map_iteration_runs(trace):
    module = load_module(emit.emit(compile_source(_SOURCE, "memkv.rvl")))
    root = Context()
    fiber = runtime_mod.plug(root, module.MemKV)
    await flush()
    assert fiber.state is FiberState.ACTIVE

    kv = root.get("kv")
    assert kv.count() == 0
    assert kv.all_keys() == []

    kv.put("banana", "yellow")
    kv.put("apple", "red")
    kv.put("cherry", "red")

    # the two methods that used to AttributeError now answer, with value-Map
    # semantics: a live count and canonically-sorted keys
    assert kv.count() == 3
    assert kv.all_keys() == ["apple", "banana", "cherry"]


def test_emitter_lowers_iteration_as_method_calls():
    """Guard the emit contract this fix depends on: `size()`/`keys()` on a
    host Map lower to method calls on the object, not to `len(store)` /
    `sorted(store)` (which would need `store` to be a bare dict)."""
    source = emit.emit(compile_source(_SOURCE, "memkv.rvl"))
    assert "store.size()" in source
    assert "store.keys()" in source
