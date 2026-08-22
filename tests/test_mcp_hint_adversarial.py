"""Adversarial probes at the MCP hint guarantee (G4 emission propagation).

The load-bearing claim: a generated tool description cannot lie about side
effects — `readOnlyHint: true` means the compiler has *proved* the body
reaches no irreversible host effect. These tests attack the proof with
first-class functions: an emission hidden behind a function *value* passed
to a dispatcher is invisible to a name-only call analysis, yet it fires at
runtime (before the fix, `s.quiet('hello')` printed "SHIP EMITTED" on the
python backend while its tool advertised `readOnlyHint: true`).

The fix treats a first-class reference to an emitting callable — any bare
use of its name outside call position — as reaching an unnameable boundary
(capability `*`), which propagates through the same fixed point as a real
emission. A dispatcher that only ever receives pure functions stays clean:
legitimate higher-order code keeps compiling read-only.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import RevlError, compile_source  # noqa: E402
from revl.mcp.schema import tools_from_ir  # noqa: E402


def _tools(source: str) -> dict:
    return {t["name"]: t for t in tools_from_ir(compile_source(source))}


# The exact shape the prior audit died proving: `ship` reaches the runtime
# only through a first-class value handed to `indirect`, whose body is
# `return f(x)` — a dispatch through an arrow-typed parameter no name-based
# walk could resolve.
FIRST_CLASS = """
extern emission fn ship(x: Str) -> Str = @py { print("SHIP EMITTED"); return x }
fn indirect(f: (Str) -> Str, x: Str) -> Str { return f(x) }
service S {
  fn quiet(a: Str) -> Str
  emission fn loud(a: Str) -> Str
}
component C provides s: S {
  provide s {
    fn quiet(a) = indirect(ship, a)
    fn loud(a) = ship(a)
  }
}
"""


def test_an_emission_smuggled_through_a_first_class_value_is_refused():
    """`quiet` never names `ship` in call position — the value does. The
    declaration check must refuse it exactly like the direct call."""
    with pytest.raises(RevlError) as excinfo:
        compile_source(FIRST_CLASS)
    message = str(excinfo.value)
    assert "`S.quiet` is declared plain" in message
    # the diagnostic names the first-class flow, not just a callee name
    assert "passed as a function value" in message
    assert excinfo.value.category == "emission-propagation"
    assert "first-class dispatch" in (excinfo.value.hint or "")


def test_the_direct_call_is_still_refused():
    """The case that always worked: pin it so the fix cannot regress it."""
    with pytest.raises(RevlError) as excinfo:
        compile_source("""
extern emission fn ship(x: Str) -> Str = @py { print("SHIP EMITTED"); return x }
service S { fn quiet(a: Str) -> Str }
component C provides s: S {
  provide s { fn quiet(a) = ship(a) }
}
""")
    assert "declared plain, but this implementation reaches `ship()`" \
        in str(excinfo.value)


def test_a_dispatcher_that_only_receives_pure_fns_stays_read_only():
    """The conservative bound must not break honest higher-order code: the
    same `indirect` helper is safe when nothing emitting ever flows through
    it, and the projection may keep advertising read-only."""
    source = """
fn indirect(f: (Str) -> Str, x: Str) -> Str { return f(x) }
fn shout(x: Str) -> Str { return x + "!" }
service S {
  fn quiet(a: Str) -> Str
  emission fn loud(a: Str) -> Str
}
component C provides s: S {
  provide s {
    fn quiet(a) = indirect(shout, a)
    fn loud(a) = indirect(shout, a)
  }
}
"""
    tools = _tools(source)
    quiet = tools["revl.s.quiet"]
    assert quiet["annotations"]["readOnlyHint"] is True
    assert quiet["x-revl"]["effects"]["reachesEmission"] == []


def test_an_emitting_value_returned_from_a_helper_is_refused():
    """Taint through a return: `getship()` never emits itself, it just hands
    `ship` onward — the dispatcher it feeds is still out of bounds."""
    with pytest.raises(RevlError) as excinfo:
        compile_source("""
extern emission fn ship(x: Str) -> Str = @py { print("SHIP EMITTED"); return x }
fn dispatch(f: (Str) -> Str, x: Str) -> Str { return f(x) }
fn getship() -> (Str) -> Str { return ship }
service S { fn quiet(a: Str) -> Str }
component C provides s: S {
  provide s { fn quiet(a) = dispatch(getship(), a) }
}
""")
    assert "`S.quiet` is declared plain" in str(excinfo.value)
    assert "`getship()`" in str(excinfo.value)


def test_aliasing_an_emission_to_a_local_is_refused():
    """No dispatcher needed: bind the extern to a local, call through the
    binding. The value reference alone must trip the gate."""
    with pytest.raises(RevlError) as excinfo:
        compile_source("""
extern emission fn ship(x: Str) -> Str = @py { print("SHIP EMITTED"); return x }
service S { fn quiet(a: Str) -> Str }
component C provides s: S {
  provide s {
    fn quiet(a) {
      let g = ship
      return g(a)
    }
  }
}
""")
    assert "passed as a function value" in str(excinfo.value)


def test_a_three_layer_transitive_chain_is_refused():
    """Pin the pre-existing propagation: method -> a -> b -> c -> ship."""
    with pytest.raises(RevlError) as excinfo:
        compile_source("""
extern emission fn ship(x: Str) -> Str = @py { print("SHIP EMITTED"); return x }
fn c(x: Str) -> Str { return ship(x) }
fn b(x: Str) -> Str { return c(x) }
fn top(x: Str) -> Str { return b(x) }
service S { fn quiet(a: Str) -> Str }
component C provides s: S {
  provide s { fn quiet(a) = top(a) }
}
""")
    # the message names the nearest culprit; the why-trace walks the whole
    # chain down to the emission extern
    message = str(excinfo.value)
    assert "declared plain, but this implementation reaches `top()`" in message
    assert "ship" in message and "emission" in message


def test_a_required_emission_edge_bounds_its_providers():
    """The require edge is a capability too: a plain provided method may not
    cross the required service's emission boundary, named or not."""
    with pytest.raises(RevlError) as excinfo:
        compile_source("""
service Database { emission fn execute(sql: Str) -> Int }
service Cache { fn put(key: Str, value: Str) }
component Lying requires db: Database provides cache: Cache {
  provide cache {
    fn put(key, value) { emit db.execute(key) }
  }
}
""")
    assert "declared plain, but this implementation reaches `db.execute`" \
        in str(excinfo.value)


def test_a_provider_purer_than_its_declaration_compiles():
    """The sound direction: `emission[db]` is an upper bound, so a provider
    that crosses less (here: nothing) is admitted — and the projection still
    speaks from the declaration, not the body."""
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
    tools = _tools(source)
    put = tools["revl.cache.put"]
    # the declaration is what the tool advertises — the purer body does not
    # upgrade the annotation, it merely passes the check
    assert put["annotations"]["readOnlyHint"] is False
