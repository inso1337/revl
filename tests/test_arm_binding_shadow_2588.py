"""Issue #2588: a binder of unknown type is still a local, in lowering too.

Issue #1990 taught the checker that a local binder occupies its name even when
its type is unknown (`typecheck.bind_local`): an absent name falls through to
item 380's rule, which types a bare top-level `fn` name as a function value.
Lowering kept the old habit. A match arm whose payload type it cannot read (an
applied generic variant such as `Mb[Str]`: `_arm_payload_type` looks the
scrutinee up by its whole spelling) and an arrow parameter with no type both
DROPPED the binder from the lowering type environment. The lowering checks
that run on the arm or arrow body then typed the binder as any top-level `fn`
of the same name in the compile. In a multi-file run that includes a private
`fn` of another file the binding file never imports, because the signature
table is one table over the merged program; `fn_scopes` limits which names a
body may CALL, not what an unbound name infers to.

The visible symptom here is the `${...}` refusal: a `Jj(ch)` arm whose body is
the backtick template `<${ch}>` was refused with "cannot interpolate a
compound value (`(List[Str]) -> Str`)", and only when the other file was in
the run. Interpolation must be a backtick template (the lexer refuses
`${...}` in a plain string); the template parses to an `Interp` node, which is
what both the checker and lowering's `_mark_interp_type` read.

The issue's own shape (a `Some(ch)` arm over a known `Opt[T]`) already checks
on base: #1990 gave the checker the builtin payload and lowering always had
it. It is kept here as a guard. Every compiled program is also run through the
python emitter, so a fix that only silences a diagnostic does not pass.
"""

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from revl import compile_files, compile_source  # noqa: E402


def _py_emit(ir):
    spec = importlib.util.spec_from_file_location(
        "revl_py_emit_arm_shadow", ROOT / "backends" / "python" / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.emit(ir)


def _call(ir, name, *args):
    """Emit the module, exec it and call the fn: the value is the assertion."""
    namespace: dict = {}
    exec(compile(_py_emit(ir), f"<emitted:{name}>", "exec"), namespace)  # noqa: S102
    return namespace[name](*args)


# File A: an arm binds `ch` from an applied generic variant and reads it as a
# value. A never imports B.
_A = '''type Mb[T] = Jj(T) | Nn

pub fn show(m: Mb[Str]) -> Str {
  return match m {
    Jj(ch) => `<${ch}>`,
    Nn => "-",
  }
}

pub fn demo() -> Str {
  return show(Jj("hi")) + show(Nn)
}
'''

# File B: a PRIVATE top-level `fn ch`, of the type the issue reported.
_B = '''fn ch(units: List[Str]) -> Str {
  return "x"
}

pub fn first_unit(units: List[Str]) -> Str {
  return ch(units)
}
'''


def test_a_private_fn_of_another_file_does_not_capture_an_arm_binding():
    """RED on base: `ch` in A's arm typed as B's `(List[Str]) -> Str` and the
    compile died with "`${...}` cannot interpolate a compound value"."""
    ir = compile_files(["a.rvl", "b.rvl"], sources={"a.rvl": _A, "b.rvl": _B})
    assert _call(ir, "demo") == "<hi>-"


def test_control_file_a_alone_checks():
    """Control (green on base too): without B in the run, A compiles and runs.
    The test above differs from this one only by B's presence."""
    ir = compile_files(["a.rvl"], sources={"a.rvl": _A})
    assert _call(ir, "demo") == "<hi>-"


def test_a_same_named_fn_of_the_same_file_does_not_capture_an_arm_binding():
    """Single-file twin of the same defect. RED on base: the arm binder is
    not in lowering's type environment, so `ch` typed as the file's own
    top-level `fn ch`."""
    ir = compile_source(_A + "\n" + _B, "arm2588.rvl")
    assert _call(ir, "demo") == "<hi>-"


# The arrow program, and the top-level `fn s` that is the only difference
# between the arrow reproducer and its control.
_ARROW = ('pub fn t() -> Str {\n'
          '  let f = (s) => `<${s}>`\n'
          '  return f("x")\n'
          '}\n')
_FN_S = ('fn s(n: Int) -> Int {\n'
         '  return n\n'
         '}\n'
         '\n')


def test_an_untyped_arrow_parameter_is_not_a_same_named_fn():
    """The arrow twin, same mechanism: a bare `(s) => ...` in an inference
    position has an unknown parameter type, and lowering dropped `s` from the
    type environment. RED on base: `s` typed as `fn s`, `(Int) -> Int`."""
    ir = compile_source(_FN_S + _ARROW, "arrow2588.rvl")
    assert _call(ir, "t") == "<x>"


def test_control_the_arrow_without_a_same_named_fn_checks():
    """Control (green on base too): the identical arrow program with no
    `fn s`. The test above differs from this one only by those lines, so its
    base refusal is the fall-through and not the template."""
    ir = compile_source(_ARROW, "arrow2588.rvl")
    assert _call(ir, "t") == "<x>"


# The issue's own shape: `Some(ch)` over a known `Opt[T]`, the value passed on
# to a fn, with B's private `fn ch` in the run.
_A_ISSUE = '''fn change_of(n: Int) -> Opt[Int] {
  if (n > 0) {
    return Some(n)
  }
  return None
}

fn bound_to(n: Int, d: Int) -> Int {
  return n + d
}

pub fn carry(n: Int) -> Int {
  return match change_of(n) {
    Some(ch) => bound_to(n, ch),
    None => 0,
  }
}
'''


def test_guard_the_issue_shape_over_a_known_opt_checks_with_b_in_the_run():
    """Guard (green on base too): the reported program, reduced. A known
    `Opt[Int]` payload has been bound in both the checker and lowering since
    #1990, so this must stay green."""
    ir = compile_files(["a.rvl", "b.rvl"],
                       sources={"a.rvl": _A_ISSUE, "b.rvl": _B})
    assert _call(ir, "carry", 2) == 4
    assert _call(ir, "carry", 0) == 0
