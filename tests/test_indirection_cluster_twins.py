"""The ACCEPTING twins of the indirection-cluster rejections.

`examples/rejections/` holds one refusal per shape in that cluster: an
obligation carried through an indirection — a spawn handle, an alias, a
locally-bound arrow, a first-class function value, a spawn `with { … }` config
— that the analysis judging the direct form used to lose. Every one of those
refusals has a legitimate near-twin that MUST still compile, and a refusal that
kills its twin is not a fix. That is what this file pins.

Kept as its own module rather than as more entries in `test_frontend.py`
because the pairing is the point: each test below names the rejection fixture
it is the twin of, so a later change that widens one of these rules has the
counterexample in front of it.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import RevlError, compile_source  # noqa: E402

WORKER = """
service Net { emission[net] fn send(msg: Str) -> Int }
service Task {
  emission[net] fn run(prompt: Str) -> Int
  fn status() -> Int
}
component Worker requires net: Net provides task: Task {
  provide task {
    fn run(prompt: Str) { emit net.send(prompt)
      return 1 }
    fn status() = 0
  }
}
"""


def _ok(source: str):
    ir = compile_source(source)
    assert ir is not None
    return ir


# ---------------------------------------------------------------- G4, aliasing

def test_aliased_provision_emission_compiles_when_marked():
    """Twin of `g4_unmarked_alias_emission.rvl`.

    The alias was invisible to the marker check in BOTH directions: the
    unmarked call was admitted, and this — the correctly marked one — was
    refused as "`emit` on a call to `run`, which is not declared `emission`".
    Resolving the local back to its provision fixes both ends at once, so this
    test would have failed before the refusal existed."""
    _ok(WORKER + """
service Sup { emission fn go(prompt: Str) -> Int }
component Supervisor provides sup: Sup {
  provide sup {
    fn go(prompt: Str) {
      let w = effect spawn Worker with { } undo w.dispose()
      let t = w.task
      let r = emit t.run(prompt)
      return r
    }
  }
}
""")


def test_aliased_provision_non_emission_needs_no_marker():
    """A plain operation reached through the same alias is not a crossing and
    takes no marker."""
    _ok(WORKER + """
service Sup { emission fn go(prompt: Str) -> Int }
component Supervisor provides sup: Sup {
  provide sup {
    fn go(prompt: Str) {
      let w = effect spawn Worker with { } undo w.dispose()
      let t = w.task
      let r = t.status()
      return r
    }
  }
}
""")


def test_aliased_provision_emission_reaches_the_audit_surface():
    """G8: the aliased crossing is not merely refused when unmarked — when it
    IS marked it appears on the component's boundary, which is the half a
    refusal alone would not prove. The audit used to report this component as
    "fully revertible" with an emission running inside it."""
    from revl.__main__ import _boundary

    ir = compile_source(WORKER + """
service Sup { emission fn go(prompt: Str) -> Int }
component Supervisor provides sup: Sup {
  provide sup {
    fn go(prompt: Str) {
      let w = effect spawn Worker with { } undo w.dispose()
      let t = w.task
      let r = emit t.run(prompt)
      return r
    }
  }
}
""")
    stats = _boundary(ir)["Supervisor"]
    assert any("task.run" in e for e in stats["emissions"]), stats


# ------------------------------------------------- G4, host acquisition

def test_bracketed_host_acquisition_still_compiles():
    """Twin of the three `*_host_acquire.rvl` fixtures: the bracket form, which
    is the whole point of the rule, is untouched."""
    _ok("""
service S { fn go(u: Str) -> Int }
component C provides s: S {
  config { url: Str }
  let pool = effect Pool.open(config.url, 1) undo pool.close()
  let m = effect Map.new() undo m.drop()
  provide s { fn go(u) = 1 }
}
""")


def test_host_acquisition_in_an_unreached_fn_still_compiles():
    """The reach qualifier, pinned. A `pub fn` no component body reaches is a
    library entry point whose lifecycle belongs to the foreign caller that
    invokes it (the java scenario corpus drives exactly such functions from a
    JVM harness); there is no activation, so there is no residue promise to
    break. `backends/java/scenarios/runtime_values.rvl` and
    `tests/fixtures/emit_py_corpus/hostroots.rvl` are the real users."""
    _ok("""
pub fn pool_exec(url: Str) -> Int {
  let p = Pool.open(url, 3)
  return p.execute("INSERT INTO t VALUES (1)")
}
""")


def test_host_verb_on_an_acquired_local_still_compiles():
    """Only the ACQUIRE verbs are position-bound; the release and the ordinary
    verbs on an acquired handle are not."""
    _ok("""
service S { fn go(k: Str) -> Int }
component C provides s: S {
  let store = effect Map.new() undo store.drop()
  provide s {
    fn go(k) {
      effect store.insert(k, "v")
      undo   store.remove(k)
      return 1
    }
  }
}
""")


# ------------------------------------------------------------- G5, teardown

def test_undo_through_a_handle_to_a_plain_operation_compiles():
    """Twin of `g5_undo_handle_emission.rvl`: the same handle, the same slot, a
    NON-emission operation. What is refused is the crossing, not the handle."""
    _ok(WORKER + """
component Sup requires net: Net {
  let w = effect spawn Worker with { } undo w.dispose()
  let m = effect Map.new() undo w.task.status()
}
""")


def test_undo_through_a_pure_local_arrow_compiles():
    """Twin of `g5_undo_arrow_emission.rvl`. The arrow's BODY is walked, so a
    teardown dispatching through a pure local arrow is admitted — the arm
    follows the indirection rather than refusing every indirect call."""
    _ok("""
service Cache { fn set(key: Str) }
component C provides cache: Cache {
  let store = effect Map.new() undo store.drop()
  provide cache {
    fn set(key) {
      let f = (x: Str) => x
      effect store.insert(key, "v")
      undo   store.remove(f(key))
      return
    }
  }
}
""")


def test_undo_through_a_pure_fn_compiles():
    """Twin of `g5_undo_fn_value_emission.rvl`: a fn that reaches no emission
    is not in `emitting_fns`, so neither the call nor a reference to it is
    refused."""
    _ok("""
fn norm(x: Str) -> Str { return x.concat("!") }
service Cache { fn set(key: Str) }
component C provides cache: Cache {
  let store = effect Map.new() undo store.drop()
  provide cache {
    fn set(key) {
      effect store.insert(norm(key), "v")
      undo   store.remove(norm(key))
      return
    }
  }
}
""")


# ------------------------------------------------------------ scope shadowing

def test_method_local_may_reuse_a_name_no_component_binding_holds():
    """Twin of `g6_method_local_shadows_component.rvl`: only a COLLISION with a
    component-scope binding is refused. An ordinary method-local `let`, and one
    that reuses a name another method used, both still compile."""
    _ok("""
service Cache { fn set(key: Str) -> Str  fn get(key: Str) -> Str }
component C provides cache: Cache {
  let store = effect Map.new() undo store.drop()
  provide cache {
    fn set(key) {
      let scratch = key
      return scratch
    }
    fn get(key) {
      let scratch = key
      return scratch
    }
  }
}
""")


def test_shadowing_a_component_binding_is_refused_in_both_orders():
    """The `let` BEFORE the use is refused too — the emitted binding takes the
    component local's host-safe name either way."""
    for body in ("      let store = key\n      effect Map.new() undo store.drop()\n",
                 "      effect store.insert(key, \"v\")\n"
                 "      undo   store.remove(key)\n      let store = key\n"):
        with pytest.raises(RevlError) as excinfo:
            compile_source("""
service Cache { fn set(key: Str) }
component C provides cache: Cache {
  let store = effect Map.new() undo store.drop()
  provide cache {
    fn set(key) {
""" + body + """    }
  }
}
""")
        assert "`store` is already bound in `set`" in str(excinfo.value)


# ------------------------------------------------------------------ G9, taint

SHELL = """
extern emission[fs] fn read_file(p: Str) -> Untrusted[Str] = @py { return "" }
extern emission[shell] fn run(cmd: Trusted[Str]) = @py { return }
extern emission[fs] fn note(m: Str) = @py { return }
service Ops { emission fn go(p: Str) }
"""


def test_closure_capturing_a_clean_value_still_reaches_the_sink():
    """Twin of `g9_closure_capture_launders_taint.rvl`: the join is the
    captured value's ACTUAL taint, so a closure over a trusted value is still
    admitted into the `Trusted[T]` sink."""
    _ok(SHELL + """
component A provides ops: Ops {
  provide ops {
    fn go(p) {
      let d = "safe"
      let f = (x: Str) => d
      emit run(f("z"))
    }
  }
}
""")


def test_closure_minted_by_a_fn_carries_the_capture():
    """The interprocedural half of the same arm: an arrow returned by a fn
    carries its captures out through the fn's return, so this is refused even
    though the arrow is never written at the call site."""
    with pytest.raises(RevlError) as excinfo:
        compile_source(SHELL + """
fn mk(d: Str) -> (Str) -> Str { return (x) => d }
component A provides ops: Ops {
  provide ops {
    fn go(p) {
      let d = emit read_file(p)
      let f = mk(d)
      emit run(f("z"))
    }
  }
}
""")
    assert "flows into a shell command" in str(excinfo.value)


def test_spawn_config_on_a_field_the_child_does_not_sink_compiles():
    """Twin of `g9_spawn_config_launders_taint.rvl`, and the test that keeps
    the spawn-config arm from being a blanket refusal: the untrusted value goes
    to `label`, which the child emits to a non-sink; `cmd`, the field that does
    reach the shell sink, gets a literal. Per-field, exactly as a call site is
    per-argument."""
    _ok(SHELL + """
service Kid { emission fn k() }
component Child provides kid: Kid {
  config { label: Str, cmd: Str }
  provide kid {
    fn k() {
      emit note(config.label)
      emit run(config.cmd)
    }
  }
}
component A provides ops: Ops {
  provide ops {
    fn go(p) {
      let d = emit read_file(p)
      let w = effect spawn Child with { label: d, cmd: "ls" } undo w.dispose()
      emit w.kid.k()
    }
  }
}
""")


def test_spawn_config_with_a_clean_value_compiles():
    """The same child, the same sinking field, a trusted value."""
    _ok(SHELL + """
service Kid { emission fn k() }
component Child provides kid: Kid {
  config { cmd: Str }
  provide kid { fn k() { emit run(config.cmd) } }
}
component A provides ops: Ops {
  provide ops {
    fn go(p) {
      let w = effect spawn Child with { cmd: "ls" } undo w.dispose()
      emit w.kid.k()
    }
  }
}
""")
