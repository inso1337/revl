"""Static checks for instance-parametric components (no runtime needed).

The grammar (one new form), the template/exclusion model, and the G-rule
changes from docs/design-v2-instances.md, checked at compile time.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import RevlError, compile_source  # noqa: E402

WORKER = """
service Counter { fn value() -> Int }
component Worker provides counter: Counter {
  config { tag: Str }
  let m = effect Map.new() undo m.drop()
  provide counter { fn value() = 0 }
}
"""


def _compile(body: str):
    return compile_source(WORKER + body, "t.rvl")


# -- grammar & IR -----------------------------------------------------------

def test_spawn_lowers_to_the_frozen_node():
    ir = _compile("""
component Sup {
  let w = effect spawn Worker with { tag: "x" } undo w.dispose()
}
""")
    step = next(c for c in ir["components"] if c["name"] == "Sup")["body"][0]
    assert step["acquire"] == {
        "kind": "spawn", "component": "Worker",
        "config": {"tag": {"kind": "lit", "value": "x"}},
        "realms": ["counter"], "line": step["acquire"]["line"],
    }


def test_spawn_bumps_ir_version_to_v3():
    ir = _compile("""
component Sup { let w = effect spawn Worker with { tag: "x" } undo w.dispose() }
""")
    assert ir["ir_version"] == 3


def test_spawn_with_no_config_is_allowed_when_no_required_fields():
    src = """
service S { fn f() -> Int }
component Leaf provides s: S { provide s { fn f() = 0 } }
component Sup { let w = effect spawn Leaf undo w.dispose() }
"""
    ir = compile_source(src, "t.rvl")
    assert ir["manifest"].get("templates") == ["Leaf"]


# -- template exclusion (decision 5/6) --------------------------------------

def test_spawn_target_is_excluded_from_static_composition():
    ir = _compile("""
component Sup { let w = effect spawn Worker with { tag: "x" } undo w.dispose() }
""")
    m = ir["manifest"]
    assert m["loadOrder"] == ["Sup"]
    assert [e["name"] for e in m["components"]] == ["Sup"]
    assert m["templates"] == ["Worker"]


def test_non_spawning_program_has_no_templates_key():
    """Byte-identical static path: a program that never spawns is unchanged."""
    ir = compile_source(
        "service S { fn f() -> Int }\n"
        "component A provides s: S { provide s { fn f() = 0 } }\n", "t.rvl")
    assert "templates" not in ir["manifest"]


def test_recursive_self_spawn_is_allowed():
    """A Session spawning a Session is a self-edge on the type graph but a
    parent->child chain on the instance graph (decision 6)."""
    src = """
service Sess { fn ping() -> Int }
component Session provides s: Sess {
  config { depth: Int }
  let child = effect spawn Session with { depth: 0 } undo child.dispose()
  provide s { fn ping() = 0 }
}
component Root { let top = effect spawn Session with { depth: 3 } undo top.dispose() }
"""
    ir = compile_source(src, "t.rvl")
    assert ir["manifest"]["loadOrder"] == ["Root"]
    assert ir["manifest"]["templates"] == ["Session"]


# -- rejections -------------------------------------------------------------

def test_unbound_spawn_is_rejected():
    with pytest.raises(RevlError, match="must be bound to a handle"):
        _compile("""
component Sup { effect spawn Worker with { tag: "x" } undo nothing.dispose() }
""")


def test_spawn_unknown_component_is_rejected():
    with pytest.raises(RevlError, match="unknown component"):
        _compile("""
component Sup { let w = effect spawn Ghost with { } undo w.dispose() }
""")


def test_spawn_unknown_config_field_is_rejected():
    with pytest.raises(RevlError, match="not a config field"):
        _compile("""
component Sup { let w = effect spawn Worker with { nope: "x" } undo w.dispose() }
""")


def test_spawn_missing_required_config_is_rejected():
    with pytest.raises(RevlError, match="missing required config field"):
        _compile("""
component Sup { let w = effect spawn Worker with { } undo w.dispose() }
""")


# -- G4 across the spawn boundary (decision 8) ------------------------------

G4_BASE = """
service Net { emission[net] fn send(msg: Str) -> Int }
service Talker { emission[net] fn talk() -> Int }
service Gate { {GATE} fn open() -> Int }
component Leaker requires net: Net provides talker: Talker {
  provide talker { fn talk() { emit net.send("hi") return 1 } }
}
component Boundary provides gate: Gate {
  provide gate {
    fn open() {
      let s = effect spawn Leaker with { } undo s.dispose()
      return 1
    }
  }
}
"""


def test_g4_plain_method_spawning_an_emitter_is_rejected():
    with pytest.raises(RevlError, match="spawns `Leaker`, which emits through `net`"):
        compile_source(G4_BASE.replace("{GATE}", ""), "t.rvl")


def test_g4_bound_that_covers_the_child_is_accepted():
    ir = compile_source(G4_BASE.replace("{GATE}", "emission[net]"), "t.rvl")
    assert ir["manifest"]["templates"] == ["Leaker"]


def test_g4_bound_that_misses_the_childs_capability_is_rejected():
    with pytest.raises(RevlError, match="spawns `Leaker`, which emits through `net`"):
        compile_source(G4_BASE.replace("{GATE}", "emission[db]"), "t.rvl")
