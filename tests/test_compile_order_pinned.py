"""The order `compile_source` puts components in is pinned, from IR to runtime.

Reversing `ir["components"]`, or the manifest's `loadOrder`, at the end of
`compile_source` used to be caught by no test in the root suite. Order is
part of the contract three ways, and this file checks each one on one
program that has a dependency (`Alpha` requires `Zeta`'s `cache`) and an
independent component declared last (`Mid`):

* the IR: `components` in declaration order; `manifest.loadOrder` providers
  first, with independents in declaration order among what is ready;
  `manifest.components` in step with `components`;
* emitted output: the py and ts emitters write the components in that order;
* the py runtime: activation follows `loadOrder`, and teardown is its reverse
  (LIFO), as the session trace records it.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402

SOURCE = '''
service Store { fn get() -> Int }
service Cache { fn hit() -> Int }
component Zeta provides cache: Cache {
  let a = effect Map.new() undo a.drop()
  provide cache { fn hit() = 1 }
}
component Alpha requires cache: Cache provides store: Store {
  let b = effect Map.new() undo b.drop()
  provide store { fn get() = 2 }
}
component Mid {
  let c = effect Map.new() undo c.drop()
}
'''

DECLARED = ["Zeta", "Alpha", "Mid"]
LOAD_ORDER = ["Zeta", "Mid", "Alpha"]

needs_cordis = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="activation order needs the cordis-py runtime (sh backends/python/setup.sh)")


def _ir() -> dict:
    return compile_source(SOURCE, "order.rvl")


def _emitter(tier: str):
    spec = importlib.util.spec_from_file_location(
        f"_order_{tier}_emit", ROOT / "backends" / tier / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ------------------------------------------------------------------- the IR

def test_components_are_in_declaration_order():
    assert [c["name"] for c in _ir()["components"]] == DECLARED


def test_the_load_order_puts_providers_first_then_declaration_order():
    assert _ir()["manifest"]["loadOrder"] == LOAD_ORDER


def test_the_manifest_lists_components_in_step_with_the_ir():
    ir = _ir()
    assert [c["name"] for c in ir["manifest"]["components"]] == \
        [c["name"] for c in ir["components"]]


# ------------------------------------------------------------ emitted output

def _positions(text: str, names, spell) -> list:
    return [text.index(spell(name)) for name in names]


def test_the_py_emitter_writes_components_in_declaration_order():
    text = _emitter("python").emit(_ir())
    at = _positions(text, DECLARED, lambda n: f"\n{n} = {{")
    assert at == sorted(at), dict(zip(DECLARED, at))


def test_the_ts_emitter_writes_components_in_declaration_order():
    text = _emitter("typescript").emit(_ir())
    at = _positions(text, DECLARED, lambda n: f"export const {n}")
    assert at == sorted(at), dict(zip(DECLARED, at))


# --------------------------------------------------------- the py runtime

@needs_cordis
def test_activation_follows_the_load_order_and_teardown_reverses_it():
    from revl.mcp.session import Session

    session = Session()
    loaded = session.load(_ir())
    try:
        assert loaded["loadOrder"] == LOAD_ORDER
        active = [e["subject"] for e in loaded["trace"]
                  if e["channel"] == "fiber" and e["detail"].endswith("-> ACTIVE")]
        assert active == LOAD_ORDER
    finally:
        unloaded = session.unload()
    disposed = [e["subject"] for e in unloaded["trace"]
                if e["channel"] == "fiber" and e["detail"].endswith("-> DISPOSED")]
    assert disposed == list(reversed(LOAD_ORDER))
