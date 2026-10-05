"""The UI transaction unit is the provide-method call (issue #1369, item 522).

The decision on #1369: there is no `transaction` construct. The method call is
the unit (docs/design/538-ui-transactions.md §0, "a UI transaction is not a
new effect construct"), so the IR says which methods are UI transactions with
a computed, additive mark: `"unit": "ui"` on a provide method whose body
crosses a computer-use verb, a token rooted in `ui_family.ROOTS`
(`screen.observe`, `ui.find`, `ui.click`, a deeper rung).

- The key is absent on every other method, so every other program's IR is
  byte-identical and `ir_version` stays 3.
- It is computed from the same capability set the G4 provider upper bound
  reads (`_method_emissions`), so a crossing through a module `fn` counts and a
  call through a required key contributes the key, not the provider's verbs.
- selfhost/lower.rvl's `lower_to_ir` produces the same mark, byte for byte, on
  tests/fixtures/emit_py_corpus/ui_unit.rvl, the computer-use document the
  self-host corpus lacked, so the oracle can now see drift on this surface.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _load_by_path import load_by_path  # noqa: E402
from revl.compiler import compile_source  # noqa: E402

CORPUS_DIR = ROOT / "tests" / "fixtures" / "emit_py_corpus"
DOC = CORPUS_DIR / "ui_unit.rvl"


def _methods(ir: dict) -> dict:
    """{method name: its lowered entry} over every provide step."""
    out = {}
    for comp in ir["components"]:
        for step in comp.get("body") or []:
            if step.get("step") == "provide":
                for method in step["methods"]:
                    out[method["name"]] = method
    return out


def _units(ir: dict) -> dict:
    return {name: method.get("unit") for name, method in _methods(ir).items()}


def test_only_the_methods_that_cross_a_computer_use_verb_are_marked():
    ir = compile_source(DOC.read_text(encoding="utf-8"), "ui_unit.rvl")
    # `act` crosses screen.observe, ui.find and ui.click; `peek` crosses
    # screen.observe through a module fn; `count` crosses nothing
    assert _units(ir) == {"act": "ui", "peek": "ui", "count": None}
    assert "unit" not in _methods(ir)["count"]
    assert ir["ir_version"] == 3


def test_the_mark_follows_the_body_and_sits_after_it():
    ir = compile_source(DOC.read_text(encoding="utf-8"), "ui_unit.rvl")
    assert list(_methods(ir)["act"]) == ["name", "params", "body", "unit"]


def test_a_call_through_a_required_key_is_not_a_computer_use_crossing():
    # the consumer reaches the verb only through `desk`, so its method is not
    # the unit; the provider's method is
    source = DOC.read_text(encoding="utf-8") + """
service Front { emission fn go(region: Str) -> Int }
component Caller requires desk: Desk provides front: Front {
  provide front { fn go(region) = emit desk.act(region) }
}
"""
    ir = compile_source(source, "t.rvl")
    assert _units(ir) == {"act": "ui", "peek": "ui", "count": None, "go": None}


@pytest.fixture(scope="module")
def lower_to_ir():
    oracle = load_by_path("selfhost_lower_ir_oracle_1369",
                          ROOT / "tests" / "test_selfhost_lower_ir.py")
    return oracle._exec_emitted()["lower_to_ir"]


def test_the_self_host_lowering_marks_the_same_methods(lower_to_ir):
    source = DOC.read_text(encoding="utf-8")
    native = json.loads(lower_to_ir(source))
    reference = compile_source(source, "ui_unit.rvl")
    assert _units(native) == {"act": "ui", "peek": "ui", "count": None}
    assert [c["body"] for c in native["components"]] == \
        [c["body"] for c in reference["components"]]
