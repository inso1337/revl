"""Capability-scoped emissions (docs/capabilities.md).

`emission` said *that* a provider crosses the boundary; `emission[db]` says
*where*. These tests pin the four things that makes true: the syntax parses
and round-trips through the IR, the G4 upper bound is enforced over sets
rather than a flag, the set propagates through the transitive `fn` walk, and
both surfaces that report a boundary — `revl audit` and the MCP projection —
say which capabilities an operation may reach.
"""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402
from revl.__main__ import _boundary, main  # noqa: E402
from revl.errors import RevlError  # noqa: E402
from revl.lower import _emitting_capabilities  # noqa: E402
from revl.mcp.schema import tools_from_ir  # noqa: E402
from revl.parser import Parser  # noqa: E402

# a provider that emits through exactly the capability it was granted
SCOPED = """
service Database { emission fn execute(sql: Str) -> Int }
service Cache { emission[db] fn put(key: Str, value: Str) }

component PgCache requires db: Database provides cache: Cache {
  provide cache {
    fn put(key, value) { emit db.execute(`INSERT ${key} ${value}`) }
  }
}
"""


# ---------------------------------------------------------------- syntax

def test_bracket_list_parses():
    prog = Parser("service S { emission[db, bus] fn f(x: Str) }", "<test>").parse()
    method = prog.services[0].methods["f"]
    assert method.emission is True
    assert method.capabilities == ("db", "bus")


def test_bare_emission_is_unscoped():
    """The pre-capability spelling must keep meaning "any capability"."""
    prog = Parser("service S { emission fn f(x: Str) }", "<test>").parse()
    assert prog.services[0].methods["f"].emission is True
    assert prog.services[0].methods["f"].capabilities is None


def test_plain_fn_has_no_capabilities():
    prog = Parser("service S { fn f(x: Str) }", "<test>").parse()
    assert prog.services[0].methods["f"].emission is False
    assert prog.services[0].methods["f"].capabilities is None


def test_capabilities_compose_with_other_modifiers():
    prog = Parser("service S { async emission[db] fn f(x: Str) }", "<test>").parse()
    method = prog.services[0].methods["f"]
    assert (method.async_, method.emission, method.capabilities) == (True, True, ("db",))


def test_empty_capability_list_is_refused():
    """`emission[]` forbids every emission — that operation is a plain `fn`."""
    with pytest.raises(RevlError, match="names no capability"):
        Parser("service S { emission[] fn f(x: Str) }", "<test>").parse()


def test_duplicate_capability_is_refused():
    with pytest.raises(RevlError, match="duplicate capability `db`"):
        Parser("service S { emission[db, db] fn f(x: Str) }", "<test>").parse()


# ---------------------------------------------------------------- the bound

def test_provider_within_its_scope_is_accepted():
    ir = compile_source(SCOPED)
    assert ir["services"]["Cache"]["methods"]["put"]["capabilities"] == ["db"]


def test_provider_outside_its_scope_is_refused():
    source = """
    service Database { emission fn execute(sql: Str) -> Int }
    service Bus { emission fn publish(topic: Str, payload: Str) }
    service Cache { emission[db] fn put(key: Str, value: Str) }

    component LeakyCache requires db: Database, bus: Bus provides cache: Cache {
      provide cache {
        fn put(key, value) {
          emit db.execute(key)
          emit bus.publish("cache.put", key)
        }
      }
    }
    """
    with pytest.raises(RevlError) as excinfo:
        compile_source(source)
    error = excinfo.value
    # the diagnostic names the offending capability *and* the declaration
    assert "`Cache.put` is declared `emission[db]`" in str(error)
    assert "emits through `bus`" in str(error)
    assert error.code == "G4"
    assert error.category == "emission-capability"


def test_a_subset_is_purer_than_declared_and_allowed():
    """The bound is one-directional, exactly as the boolean one is."""
    source = """
    service Database { emission fn execute(sql: Str) -> Int }
    service Bus { emission fn publish(topic: Str, payload: Str) }
    service Cache { emission[db, bus] fn put(key: Str, value: Str) }

    component QuietCache requires db: Database, bus: Bus provides cache: Cache {
      provide cache {
        fn put(key, value) { emit db.execute(key) }
      }
    }
    """
    assert compile_source(source)["services"]["Cache"]["methods"]["put"][
        "capabilities"] == ["db", "bus"]


def test_a_scoped_provider_that_emits_nothing_is_allowed():
    source = """
    service Cache { emission[db] fn put(key: Str, value: Str) }

    component NullCache provides cache: Cache {
      let store = effect Map.new() undo store.drop()
      provide cache {
        fn put(key, value) {
          effect store.insert(key, value)
          undo   store.remove(key)
        }
      }
    }
    """
    assert compile_source(source)


def test_the_plain_declaration_rule_is_untouched():
    """A plain `fn` still refuses *any* emission, with its own diagnostic."""
    source = """
    service Database { emission fn execute(sql: Str) -> Int }
    service Cache { fn put(key: Str, value: Str) }

    component LyingCache requires db: Database provides cache: Cache {
      provide cache {
        fn put(key, value) { emit db.execute(key) }
      }
    }
    """
    with pytest.raises(RevlError) as excinfo:
        compile_source(source)
    assert "is declared plain" in str(excinfo.value)
    assert excinfo.value.category == "emission-propagation"


def test_a_teardown_position_emission_counts_against_the_scope():
    """Calling the method *schedules* the undo, so it is in scope too."""
    source = """
    service Database { emission fn execute(sql: Str) -> Int }
    service Bus { emission fn publish(topic: Str, payload: Str) }
    service Cache { emission[db] fn put(key: Str, value: Str) }

    component LeakyCache requires db: Database, bus: Bus provides cache: Cache {
      let store = effect Map.new() undo store.drop()
      provide cache {
        fn put(key, value) {
          emit   db.execute(key)
          effect store.insert(key, value)
          undo   bus.publish("rollback", key)
        }
      }
    }
    """
    with pytest.raises(RevlError, match="emits through `bus`"):
        compile_source(source)


# ---------------------------------------------------------------- transitivity

def test_the_fixed_point_propagates_sets_not_a_flag():
    externs = [{"name": "send", "class": "emission"},
               {"name": "ship", "class": "emission"},
               {"name": "sha", "class": "pure"}]
    fns = [
        {"name": "one", "body": [{"kind": "fn", "name": "send"}]},
        {"name": "two", "body": [{"kind": "fn", "name": "ship"}]},
        {"name": "both", "body": [{"kind": "fn", "name": "one"},
                                  {"kind": "fn", "name": "two"}]},
        {"name": "pure_ish", "body": [{"kind": "fn", "name": "sha"}]},
        # self-recursion must not stop the least fixed point converging
        {"name": "loopy", "body": [{"kind": "fn", "name": "loopy"},
                                   {"kind": "fn", "name": "one"}]},
    ]
    caps = _emitting_capabilities(fns, externs)
    assert caps["send"] == {"send"}
    assert caps["one"] == {"send"}
    assert caps["both"] == {"send", "ship"}
    assert caps["loopy"] == {"send"}
    assert "pure_ish" not in caps  # a pure extern is not a boundary
    assert "sha" not in caps


def test_an_extern_names_its_own_capability_through_a_chain_of_fns():
    source = """
    extern emission fn send(data: Str) = @python { pass }
    fn relay(data: Str) { return send(data) }
    fn blast(data: Str) { return relay(data) }

    service Wire { emission[send] fn go(m: Str) }
    component W provides wire: Wire {
      provide wire { fn go(m) { return emit blast(m) } }
    }
    """
    assert compile_source(source)


def test_a_transitively_reached_extern_outside_the_scope_is_refused():
    source = """
    extern emission fn send(data: Str) = @python { pass }
    fn blast(data: Str) { return send(data) }

    service Wire { emission[db] fn go(m: Str) }
    component W provides wire: Wire {
      provide wire { fn go(m) { return emit blast(m) } }
    }
    """
    with pytest.raises(RevlError) as excinfo:
        compile_source(source)
    # the capability is the extern (the boundary), while the evidence names
    # the function that reached it
    assert "emits through `send`" in str(excinfo.value)
    assert "`blast()`" in str(excinfo.value)


def test_the_capability_of_a_downstream_call_is_the_local_key():
    """Calling `emission[db] fn put` through key `cache` costs `cache`, not
    `db`: the declaration names *this* component's boundary."""
    source = """
    service Database { emission fn execute(sql: Str) -> Int }
    service Cache { emission[db] fn put(key: Str, value: Str) }
    service Api { emission[cache] fn save(key: Str) }

    component PgCache requires db: Database provides cache: Cache {
      provide cache { fn put(key, value) { emit db.execute(key) } }
    }
    component Front requires cache: Cache provides api: Api {
      provide api { fn save(key) { emit cache.put(key, "v") } }
    }
    """
    assert compile_source(source)

    leaked = source.replace("emission[cache] fn save", "emission[db] fn save")
    with pytest.raises(RevlError, match="emits through `cache`"):
        compile_source(leaked)


# ---------------------------------------------------------------- IR

def test_bare_emission_carries_no_capabilities_key():
    """Absence means "any" — which is what every pre-capability IR meant, so
    no reference IR or backend golden is invalidated."""
    ir = compile_source("""
    service Bus { emission fn send(m: Str) }
    component B provides bus: Bus {
      provide bus { fn send(m) { } }
    }
    """)
    assert "capabilities" not in ir["services"]["Bus"]["methods"]["send"]


def test_the_capability_set_survives_the_ir_round_trip():
    """A service redeclared against a running manifest must still agree."""
    from revl.lower import _service_equal, _service_from_ir

    ir = compile_source(SCOPED)
    rebuilt = _service_from_ir("Cache", ir["services"]["Cache"])
    assert rebuilt.methods["put"].capabilities == ("db",)

    widened = json.loads(json.dumps(ir["services"]["Cache"]))
    widened["methods"]["put"]["capabilities"] = ["db", "bus"]
    assert not _service_equal(rebuilt, _service_from_ir("Cache", widened))


# ---------------------------------------------------------------- audit (G8)

def test_the_audit_reports_the_capability_of_each_emission():
    boundary = _boundary(compile_source(SCOPED))
    front = boundary["PgCache"]
    assert front["emissions"] == ["db.execute"]
    # `Database.execute` is declared bare `emission`, so its scope is "any"
    assert front["capabilities"] == {"db.execute": ["*"]}


def test_the_audit_reports_a_declared_scope():
    source = SCOPED + """
    component Front requires cache: Cache {
      emit cache.put("k", "v")
    }
    """
    boundary = _boundary(compile_source(source))
    assert boundary["Front"]["capabilities"] == {"cache.put": ["db"]}


def test_the_audit_cli_prints_the_scope(tmp_path, capsys):
    path = tmp_path / "scoped.rvl"
    path.write_text(SCOPED + """
    component Front requires cache: Cache {
      emit cache.put("k", "v")
    }
    """)
    assert main(["audit", str(path)]) == 0
    out = capsys.readouterr().out
    assert "cache.put [db]" in out
    assert "capabilities: db" in out


def test_the_audit_cli_json_carries_the_map(tmp_path, capsys):
    path = tmp_path / "scoped.rvl"
    path.write_text(SCOPED)
    assert main(["audit", str(path), "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["boundary"]["PgCache"]["capabilities"] == {"db.execute": ["*"]}


# ---------------------------------------------------------------- MCP

def test_the_mcp_projection_carries_the_declared_scope():
    (tool,) = tools_from_ir(compile_source(SCOPED), composition="app")
    assert tool["name"] == "app.cache.put"
    assert tool["x-revl"]["capabilities"] == ["db"]
    assert tool["x-revl"]["effects"]["reachesCapabilities"] == ["db"]
    assert "outside [db]" in tool["description"]
    assert "bounded to [db]" in tool["x-revl"]["guarantee"]
    # a scoped emission is still an emission
    assert tool["annotations"]["destructiveHint"] is True
    assert tool["annotations"]["readOnlyHint"] is False


def test_an_unscoped_emission_says_so_rather_than_implying_a_scope():
    ir = compile_source("""
    service Bus { emission fn send(m: Str) }
    component B provides bus: Bus {
      provide bus { fn send(m) { } }
    }
    """)
    (tool,) = tools_from_ir(ir)
    assert tool["x-revl"]["capabilities"] == ["*"]
    assert "promises nothing about where the emission goes" in tool["description"]


def test_a_plain_operation_has_an_empty_capability_set():
    ir = compile_source("""
    service Store { fn get(k: Str) -> Str }
    component S provides store: Store {
      provide store { fn get(k) = "v" }
    }
    """)
    (tool,) = tools_from_ir(ir)
    assert tool["x-revl"]["capabilities"] == []
    assert tool["annotations"]["readOnlyHint"] is True


# --- G8 audit surface under first-class dispatch ---------------------------
# The G4 fix (agent/mcp-hint-hardening2) made a first-class reference to an
# emitting callable add the `*` capability. The G8 audit must not lose the
# concrete boundary names in the same situation: `*` says *that* the reach is
# unnameable, the concrete names say *what* it reaches.

_DISPATCH_SOURCE = """
extern emission fn ship(x: Str) -> Str = @py { print("SHIP EMITTED"); return x }
fn indirect(f: (Str) -> Str, x: Str) -> Str { return f(x) }
fn wrap(x: Str) -> Str { return indirect(ship, x) }
service S { emission fn loud(a: Str) -> Str }
component C provides s: S {
  provide s { fn loud(a) = wrap(a) }
}
"""


def test_capability_fixed_point_keeps_concrete_names_alongside_the_dispatch_star():
    ir = compile_source(_DISPATCH_SOURCE)
    caps = _emitting_capabilities(ir.get("functions") or [], ir.get("externs") or [])
    # `wrap` carries both: `*` marks that a first-class dispatch happens,
    # `ship` names the boundary the dispatched value reaches.
    assert caps["wrap"] == {"*", "ship"}
    # `indirect` alone earns NO entry: its dispatch runs through its own
    # parameter, so nothing concrete flows there — a dispatcher stays pure
    # until an emitting value is handed to it, which happens at `wrap`.
    assert "indirect" not in caps


def test_the_audit_reports_the_boundary_behind_a_first_class_dispatch():
    ir = compile_source(_DISPATCH_SOURCE)
    externs = _boundary(ir)["C"]["externs"]
    names = {e["name"] for e in externs}
    # before the fix only `*`-less name-only reaches appeared: ship vanished
    assert names == {"*", "ship"}
    star = next(e for e in externs if e["name"] == "*")
    assert star["class"] == "first-class dispatch"
    ship = next(e for e in externs if e["name"] == "ship")
    assert ship["class"] == "emission"


def test_the_audit_stays_clean_for_pure_higher_order_chains():
    ir = compile_source("""
    extern emission fn log(x: Str) -> Str = @py { print("LOG"); return x }
    extern pure fn purefn(x: Str) -> Str = @py { return x }
    fn chain2(x: Str) -> Str { return purefn(x) }
    service T { emission fn op(a: Str) -> Str }
    component D provides t: T {
      provide t { fn op(a) = log(chain2(a)) }
    }
    """)
    externs = _boundary(ir)["D"]["externs"]
    assert {e["name"] for e in externs} == {"log", "purefn"}
