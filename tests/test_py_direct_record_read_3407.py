"""Issue #3407: a record field read on the python tier skips the `isinstance`
dispatch only where the receiver is a record this document built.

The dispatch (`f['x'] if isinstance(f, dict) else getattr(f, 'x')`) exists for
a value that is not the dict a record literal emits: an ADT payload object, or
a host object handed in under a record type. The emitter now reads `f['x']`
directly when every value `f` can hold was built by a record literal in the
program (`_dict_receivers` in backends/python/emit.py). The first group pins
that shape on the case the issue measured. The second group pins, one test per
way a host value can reach a record parameter, that the dispatch is still
emitted there and that a host object still reads (or fails) exactly as it did.
"""

import importlib.util
import re
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402

_SPEC = importlib.util.spec_from_file_location(
    "emit_py_direct_read_3407", ROOT / "backends" / "python" / "emit.py")
EMIT = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(EMIT)

FLY = "type Fly = { x: Float, y: Float, k: Float }\n"
MOVE = """
fn move(f: Fly, tx: Float, dt: Float) -> Fly {
  let dx = tx - f.x
  let dy = tx - f.y
  return { x: f.x + dx * dt, y: f.y + dy * dt, k: f.k }
}
"""


def _emit(source: str) -> str:
    return EMIT.emit(compile_source(source, "direct_read_3407.rvl"))


def _load(text: str):
    module = types.ModuleType("direct_read_3407_mod")
    exec(compile(text, "<direct_read_3407>", "exec"), module.__dict__)
    return module


def _def(text: str, name: str) -> str:
    """The emitted source of one module-level `def`."""
    match = re.search(rf"^def {name}\(.*?(?=^\S)", text + "\n#", re.S | re.M)
    assert match, f"no def {name} in the emitted module"
    return match.group(0)


DISPATCH = "isinstance(f, dict)"


# ---------------------------------------------------------------- direct reads

def test_measured_case_reads_the_record_parameter_directly():
    """A non-exported fn whose only callers pass records built in revl: the
    loop in `run` starts from a record literal and assigns `move`'s result,
    which is itself a record literal, back."""
    text = _emit(FLY + MOVE + """
pub fn run(n: Int, tx: Float) -> Float {
  var f = { x: 1.0, y: 2.0, k: 3.0 }
  var i = 0
  while (i < n) {
    f = move(f, tx, 0.5)
    i += 1
  }
  return f.x
}
""")
    move = _def(text, "move")
    assert "isinstance" not in move and "getattr" not in move, move
    assert "(tx - f['x'])" in move
    assert "f['x']" in _def(text, "run")
    assert _load(text).run(3, 9.0) == 8.0


def test_let_bound_records_and_tests_count_as_built_here():
    text = _emit(FLY + MOVE + """
pub fn once(tx: Float) -> Float {
  let a = { x: 1.0, y: 2.0, k: 3.0 }
  let b = move(a, tx, 0.5)
  let c = { b | k = 0.0 }
  return move(c, tx, 0.5).k + b.y
}
test "move" {
  assert move({ x: 0.0, y: 0.0, k: 1.0 }, 2.0, 0.5).x == 1.0
}
""")
    assert "isinstance" not in _def(text, "move")
    assert "b['y']" in _def(text, "once")
    assert _load(text).once(4.0) == 3.0


def test_a_recursive_entry_from_a_literal_reads_directly():
    """The facts are a greatest fixpoint: `walk` calls itself with `move(f)`
    and returns `f`; the only entry passes a literal."""
    text = _emit(FLY + MOVE + """
fn walk(f: Fly, n: Int) -> Fly {
  if (n == 0) { return f }
  return walk(move(f, 1.0, 0.5), n - 1)
}
pub fn go() -> Float {
  return walk({ x: 0.0, y: 0.0, k: 0.0 }, 2).x
}
""")
    assert "isinstance" not in _def(text, "move")
    assert _load(text).go() == 0.75


# ------------------------------------------------- boundaries keep the dispatch

class _Host:
    """A host object carrying the record's fields as attributes: the shape
    the `getattr` arm exists to read."""
    def __init__(self, x, y, k):
        self.x, self.y, self.k = x, y, k


def _keeps_dispatch(text: str, entry: str, args):
    """`move` keeps the dispatch, a host object still reads through `getattr`,
    and an object without the field still fails with today's AttributeError."""
    assert DISPATCH in _def(text, "move"), _def(text, "move")
    module = _load(text)
    got = getattr(module, entry)(*args(_Host(1.0, 2.0, 3.0)))
    with pytest.raises(AttributeError):
        getattr(module, entry)(*args(object()))
    return got


def test_a_pub_fn_parameter_keeps_the_dispatch():
    text = _emit(FLY + MOVE.replace("fn move", "pub fn move"))
    got = _keeps_dispatch(text, "move", lambda h: (h, 5.0, 0.5))
    assert got == {"x": 3.0, "y": 3.5, "k": 3.0}


def test_a_value_a_host_passes_through_a_pub_fn_keeps_the_dispatch():
    text = _emit(FLY + MOVE + """
pub fn step(f: Fly) -> Fly { return move(f, 5.0, 0.5) }
""")
    assert _keeps_dispatch(text, "step", lambda h: (h,))["x"] == 3.0


def test_a_list_element_keeps_the_dispatch():
    text = _emit(FLY + MOVE + """
pub fn step(fs: List[Fly]) -> Float {
  var s = 0.0
  for (f of fs) { s += move(f, 5.0, 0.5).x }
  return s
}
""")
    assert _keeps_dispatch(text, "step", lambda h: ([h],)) == 3.0


def test_an_extern_return_keeps_the_dispatch():
    text = _emit(FLY + MOVE + """
extern pure fn fetch(h: Value) -> Fly = @py { return h }
pub fn step(h: Value) -> Fly {
  let f = fetch(h)
  return move(f, 5.0, 0.5)
}
""")
    assert _keeps_dispatch(text, "step", lambda h: (h,))["x"] == 3.0


def test_an_adt_payload_keeps_the_dispatch():
    text = _emit(FLY + MOVE + """
type Got = Hit(Fly) | Miss
pub fn step(g: Got) -> Float {
  return match g { Hit(p) => move(p, 5.0, 0.5).x, Miss => 0.0 }
}
""")
    module = _load(text)
    assert DISPATCH in _def(text, "move")
    assert module.step(module.Hit(_Host(1.0, 2.0, 3.0))) == 3.0


def test_a_fn_used_as_a_value_keeps_the_dispatch():
    text = _emit(FLY + MOVE + """
pub fn pick() -> (Fly, Float, Float) -> Fly { return move }
""")
    assert DISPATCH in _def(text, "move")
    assert _load(text).pick()(_Host(1.0, 2.0, 3.0), 5.0, 0.5)["x"] == 3.0


def test_a_fn_no_revl_code_calls_keeps_the_dispatch():
    """Only a host can call it (the `prop test` wrapper is one such fn)."""
    text = _emit(FLY + MOVE)
    _keeps_dispatch(text, "move", lambda h: (h, 5.0, 0.5))


def test_a_service_argument_keeps_the_dispatch():
    """A provide method's argument arrives from a caller through the runtime
    (a session call, a peer): the call site sits outside every fn body."""
    text = _emit(FLY + MOVE + """
service Mover { fn nudge(f: Fly) -> Float }
component M provides mover: Mover {
  provide mover {
    fn nudge(f) = move(f, 5.0, 0.5).x
  }
}
pub fn local() -> Float { return move({ x: 1.0, y: 2.0, k: 3.0 }, 5.0, 0.5).x }
""")
    assert DISPATCH in _def(text, "move")


def test_a_shadowing_arrow_parameter_keeps_the_dispatch():
    """`f` is a built record in `run`, but the arrow's own `f` is whatever the
    arrow is called with: the spelling is not resolved, it disqualifies."""
    text = _emit(FLY + """
pub fn run(h: Fly) -> Float {
  let f = { x: 1.0, y: 2.0, k: 3.0 }
  let g = (f: Fly) => f.x
  return g(h) + f.y
}
""")
    run = _def(text, "run")
    assert "isinstance(f, dict)" in run, run
    assert _load(text).run(_Host(1.0, 2.0, 3.0)) == 3.0


# ------------------------------------- host text emitted verbatim into the module
#
# An extern's `@py` body is host text the emitter writes into the module as it
# is, so a call made by name there reaches the module fn with whatever the host
# hands it. The IR carries that text as one opaque string, inline or spliced
# from a file (`= @py file`), so the reference count reads every word of every
# string. A `= @py ref` symbol lives in its own host module, where the module
# fn's name is not bound at all.

HOST_CALL = FLY + """
fn rd(f: Fly, t: Float) -> Float {
  let d = t - f.x
  return d * f.y
}
extern pure fn poke(h: Value) -> Float = @py { return rd(h, 1.0) }
pub fn go(h: Value) -> Float {
  return poke(h) + rd({ x: 1.0, y: 1.0, k: 1.0 }, 2.0)
}
"""


def _host_reads(text: str):
    rd = _def(text, "rd")
    assert DISPATCH in rd, rd
    module = _load(text)
    got = module.go(_Host(5.0, 1.0, 1.0))
    with pytest.raises(AttributeError):
        module.go(object())
    return got


def test_a_fn_an_inline_py_body_calls_keeps_the_dispatch():
    assert _host_reads(_emit(HOST_CALL)) == -3.0


def test_a_fn_a_spliced_py_file_calls_keeps_the_dispatch(tmp_path):
    from revl.compiler import compile_files
    (tmp_path / "poke_body.py").write_text("return rd(h, 1.0)\n", encoding="utf-8")
    app = tmp_path / "app.rvl"
    app.write_text(HOST_CALL.replace(
        "= @py { return rd(h, 1.0) }", '= @py file "poke_body.py"'), encoding="utf-8")
    assert _host_reads(EMIT.emit(compile_files([str(app)]))) == -3.0


def test_a_ref_host_module_cannot_call_a_module_fn_by_name(tmp_path, monkeypatch):
    """The ref'd symbol runs in its own module: a bare `rd` there is unbound
    on base and here alike, so it is no route to the module fn."""
    from revl.compiler import compile_files
    pkg = f"poke_ref_3407_{tmp_path.name}"
    (tmp_path / pkg).mkdir()
    (tmp_path / pkg / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / pkg / "host.py").write_text(
        "def poke(h):\n    return rd(h, 1.0)\n", encoding="utf-8")
    app = tmp_path / "app.rvl"
    app.write_text(HOST_CALL.replace(
        "= @py { return rd(h, 1.0) }", f'= @py ref poke from "{pkg}/host.py"'),
        encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    text = EMIT.emit(compile_files([str(app)]))
    try:
        with pytest.raises(NameError, match="'rd'"):
            _load(text).go(_Host(5.0, 1.0, 1.0))
    finally:
        for name in (pkg, f"{pkg}.host"):
            sys.modules.pop(name, None)


def test_a_word_of_a_string_literal_keeps_the_dispatch():
    """Conservative by construction: any string with the fn name as a word
    opens it. The values are dicts here, so only the shape changes."""
    text = _emit(FLY + MOVE + """
pub fn once() -> Str {
  let a = move({ x: 1.0, y: 2.0, k: 3.0 }, 5.0, 0.5)
  return "move(a)"
}
""")
    assert DISPATCH in _def(text, "move")
    assert _load(text).once() == "move(a)"


def test_a_test_title_containing_the_name_is_not_a_reference():
    text = _emit(FLY + MOVE + """
test "move halves the gap" {
  assert move({ x: 0.0, y: 0.0, k: 1.0 }, 2.0, 0.5).x == 1.0
}
""")
    assert "isinstance" not in _def(text, "move")


def test_a_call_folded_by_inlining_leaves_no_call_site():
    """A single-return pure fn is folded into its fn callers before the
    analysis runs, so the emitted `def` has no remaining call site and keeps the
    dispatch; the folded caller still computes the same value."""
    source = FLY + """
fn rd(f: Fly) -> Float { return f.x }
pub fn go() -> Float { return rd({ x: 1.0, y: 1.0, k: 1.0 }) }
"""
    ir = compile_source(source, "direct_read_3407.rvl")
    assert EMIT._dict_receivers(EMIT._inline_pure_fns(ir)) == {}
    text = EMIT.emit(ir)
    assert "isinstance(f, dict)" in _def(text, "rd")
    module = _load(text)
    assert module.go() == 1.0
    assert module.rd(_Host(4.0, 0.0, 0.0)) == 4.0
