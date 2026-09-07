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


# -- named instances (item 10 placement horizon) ---------------------------
# `spawn C … as "<name>"` stamps the instance with a stable author-declared
# address. The address makes no location claim yet (the instance still lands in
# the spawning process); it is the identity a later placement slice keys on, so
# the spawn shape need not bake "same process" in as its only possibility.

def test_named_spawn_carries_the_name_on_the_node():
    ir = _compile("""
component Sup {
  let w = effect spawn Worker with { tag: "x" } as "worker-a" undo w.dispose()
}
""")
    acq = next(c for c in ir["components"] if c["name"] == "Sup")["body"][0]["acquire"]
    assert acq["kind"] == "spawn"
    assert acq["name"] == "worker-a"


def test_unnamed_spawn_has_no_name_key():
    """Byte-identical: an unnamed spawn node is unchanged (no `name` key)."""
    ir = _compile("""
component Sup { let w = effect spawn Worker with { tag: "x" } undo w.dispose() }
""")
    acq = next(c for c in ir["components"] if c["name"] == "Sup")["body"][0]["acquire"]
    assert "name" not in acq


def test_distinct_named_instances_in_one_component_are_allowed():
    ir = _compile("""
component Sup {
  let a = effect spawn Worker with { tag: "x" } as "a" undo a.dispose()
  let b = effect spawn Worker with { tag: "y" } as "b" undo b.dispose()
}
""")
    body = next(c for c in ir["components"] if c["name"] == "Sup")["body"]
    assert [s["acquire"]["name"] for s in body] == ["a", "b"]


def test_same_name_in_different_components_is_allowed():
    """A name addresses within its spawner, so two supervisors may reuse it."""
    ir = _compile("""
component SupA { let w = effect spawn Worker with { tag: "x" } as "primary" undo w.dispose() }
component SupB { let w = effect spawn Worker with { tag: "y" } as "primary" undo w.dispose() }
""")
    got = {c["name"]: c["body"][0]["acquire"].get("name")
           for c in ir["components"] if c["name"] in ("SupA", "SupB")}
    assert got == {"SupA": "primary", "SupB": "primary"}


def test_duplicate_named_instance_in_one_component_is_rejected():
    with pytest.raises(RevlError, match="duplicate spawn instance name"):
        _compile("""
component Sup {
  let a = effect spawn Worker with { tag: "x" } as "dup" undo a.dispose()
  let b = effect spawn Worker with { tag: "y" } as "dup" undo b.dispose()
}
""")


def test_non_string_instance_name_is_rejected():
    with pytest.raises(RevlError, match="must be a string literal"):
        _compile("""
component Sup { let w = effect spawn Worker with { tag: "x" } as worker undo w.dispose() }
""")


def test_empty_instance_name_is_rejected():
    with pytest.raises(RevlError, match="cannot be empty"):
        _compile("""
component Sup { let w = effect spawn Worker with { tag: "x" } as "" undo w.dispose() }
""")


# -- named instances surfaced in the manifest (item 10, next slice) ---------
# The address landed on the acquire node (#454); this slice enumerates every
# named instance at the COMPOSITION layer as `manifest["named_instances"]`, so
# a later placement slice can key on an instance address, not only a template
# component. Additive and named-only: no named spawn -> no key (byte-identical).

def test_manifest_enumerates_named_instances():
    ir = _compile("""
component Sup {
  let a = effect spawn Worker with { tag: "x" } as "worker-a" undo a.dispose()
  let b = effect spawn Worker with { tag: "y" } as "worker-b" undo b.dispose()
}
""")
    ni = ir["manifest"]["named_instances"]
    assert [{k: r[k] for k in ("spawner", "name", "component")} for r in ni] == [
        {"spawner": "Sup", "name": "worker-a", "component": "Worker"},
        {"spawner": "Sup", "name": "worker-b", "component": "Worker"},
    ]
    # each record carries a source line for diagnostics, ascending in body order
    assert all(isinstance(r["line"], int) for r in ni)
    assert ni[0]["line"] < ni[1]["line"]


def test_manifest_has_no_named_instances_key_without_a_named_spawn():
    """Byte-identical: an unnamed spawn composition carries no new manifest key."""
    ir = _compile("""
component Sup { let w = effect spawn Worker with { tag: "x" } undo w.dispose() }
""")
    assert "named_instances" not in ir["manifest"]


def test_no_named_instances_key_for_a_non_spawning_composition():
    # just WORKER, statically composed, nothing spawned at all.
    ir = _compile("")
    assert "named_instances" not in ir["manifest"]
    assert "templates" not in ir["manifest"]


def test_named_instances_are_sorted_by_spawner_then_name():
    """Deterministic manifest regardless of source order / declaration order."""
    ir = _compile("""
component SupB {
  let z = effect spawn Worker with { tag: "x" } as "z" undo z.dispose()
  let a = effect spawn Worker with { tag: "y" } as "a" undo a.dispose()
}
component SupA {
  let m = effect spawn Worker with { tag: "z" } as "m" undo m.dispose()
}
""")
    addrs = [(r["spawner"], r["name"]) for r in ir["manifest"]["named_instances"]]
    assert addrs == [("SupA", "m"), ("SupB", "a"), ("SupB", "z")]


def test_same_name_across_spawners_is_two_distinct_addresses():
    """A name recurs across spawners, so (spawner, name) is the stable address."""
    ir = _compile("""
component SupA { let w = effect spawn Worker with { tag: "x" } as "primary" undo w.dispose() }
component SupB { let w = effect spawn Worker with { tag: "y" } as "primary" undo w.dispose() }
""")
    addrs = [(r["spawner"], r["name"]) for r in ir["manifest"]["named_instances"]]
    assert addrs == [("SupA", "primary"), ("SupB", "primary")]
    assert len(addrs) == len(set(addrs))


def test_named_instance_records_the_target_template_component():
    ir = _compile("""
component Sup { let w = effect spawn Worker with { tag: "x" } as "only" undo w.dispose() }
""")
    (rec,) = ir["manifest"]["named_instances"]
    assert rec["component"] == "Worker"
    assert ir["manifest"]["templates"] == ["Worker"]


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


# -- emit through a spawn handle (item 82) ----------------------------------
#
# Calling a service operation through a spawn handle (`w.task.run(...)`) reads
# the provision off the handle as an `instance-get`, not a `req` target. The
# emission check used to assume a `req` target and raised `KeyError: 'target'`
# on the handle spelling; it now walks the handle's provision to the service
# and reads the operation's emission-ness there.

HANDLE_BASE = """
service Net { emission[net] fn send(msg: Str) -> Int }
service Task {
  emission[net] fn run(prompt: Str) -> Int
  fn status() -> Int
}
component Worker requires net: Net provides task: Task {
  provide task {
    fn run(prompt: Str) { emit net.send(prompt) return 1 }
    fn status() = 0
  }
}
service Sup { {DECL} fn go(prompt: Str) -> Int }
component Supervisor provides sup: Sup {
  provide sup {
    fn go(prompt: Str) {
      let w = effect spawn Worker with { } undo w.dispose()
      {BODY}
      return 1
    }
  }
}
"""


def _handle(decl: str, body: str):
    return compile_source(
        HANDLE_BASE.replace("{DECL}", decl).replace("{BODY}", body), "t.rvl")


def test_emit_emission_through_handle_is_accepted():
    """The regression: `emit w.task.run(prompt)` from an `emission`-declared
    supervisor method compiles cleanly (used to KeyError in
    `_is_emission_call`). The boundary is marked one level up."""
    ir = _handle("emission", "emit w.task.run(prompt)")
    assert ir["manifest"]["templates"] == ["Worker"]
    go = next(c for c in ir["components"] if c["name"] == "Supervisor")
    emit = go["body"][0]["methods"][0]["body"][1]
    assert emit["step"] == "emit"
    # the emission rides the provision-read shape: a `field` callee over the
    # `instance-get`, not a `req` target
    callee = emit["expr"]["callee"]
    assert callee["kind"] == "field" and callee["name"] == "run"
    assert callee["target"]["kind"] == "instance-get"
    assert callee["target"]["key"] == "task"


def test_non_emission_call_through_handle_is_accepted():
    """A plain operation through the handle (`w.task.status()`) never needed a
    marker and still compiles — the emission check correctly returns False for
    it rather than crashing."""
    ir = _handle("emission", "let s = w.task.status()")
    assert ir["manifest"]["templates"] == ["Worker"]


def test_unmarked_emission_through_handle_is_rejected():
    """An emission reached through the handle without `emit` is refused with a
    G4 marker diagnostic — not silently lowered, and not a KeyError."""
    with pytest.raises(RevlError,
                       match=r"call to emission `w\.task\.run` must be marked `emit` \(G4\)"):
        _handle("emission", "let r = w.task.run(prompt)")


def test_emit_on_non_emission_through_handle_is_rejected():
    """The reverse guard: marking a non-emission handle call `emit` is refused
    with a readable diagnostic (`_node_desc` no longer KeyErrors on the
    provision-read shape), pointing at `task.status`."""
    with pytest.raises(RevlError, match=r"`emit` on `task\.status`"):
        _handle("emission", "emit w.task.status()")
