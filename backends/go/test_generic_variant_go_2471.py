"""A user generic variant builds and runs on the go tier (issue #2471).

WHAT WAS WRONG. `type Mb[T] = Jj(T) | Nn` passed the checker (#2470) and then
emitted Go that does not parse: the signature kept the surface spelling
`Mb[Int]`, the type switch read `case Mb[Int]Jj:` (`expected ':', found Jj`),
and the case struct declared `Value T` with no `T` in scope.

WHAT IS EMITTED NOW. The variant is Go generics:
`type Mb[T any] interface { isMb(T) }` with one `MbJj[T any]` struct per case,
whose marker method names `T`, so an `MbJj[int64]` is an `Mb[int64]` and not an
`Mb[string]`. Every use names the instantiation (`Mb[int64]`, `MbJj[int64]{..}`,
`case MbJj[int64]:`), because Go infers no type argument from a composite
literal or from where a value flows. A construction takes its type arguments
from the flow target, else from its payload argument.

The run test executes the program's `test` block on the py tier and on go and
asserts both pass the same assertions. It needs `go` and skips without it; a
skip is not a pass, CI's `backend-go` job has go.
"""

from __future__ import annotations

import importlib.util
import shutil
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402
from revl.test import RUNNERS  # noqa: E402

_spec = importlib.util.spec_from_file_location("revl_go_emit_2471", HERE / "emit.py")
go_emit = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(go_emit)

# Construction, a match that binds the payload, functions taking and returning
# `Mb[Int]`, two instantiations (`Mb[Int]`, `Mb[Str]`) and a nested one, a
# two-parameter variant, a `List` payload, a record field, a list of variants,
# lets and structural equality, unpinned list literals (`[Nn, Jj(4)]`,
# `[None, Some(5)]`) and a parameter no case names (`Ph[T]`).
PROGRAM = """\
type Mb[T] = Jj(T) | Nn
type Ei[L, R] = Le(L) | Ri(R)
type Ph[T] = Aa | Bb
type Holder = { name: Str, slot: Mb[Str] }

fn len_of(s: Str) -> Int { return s.length() }
fn show(n: Int) -> Str { return n.to_str() }
fn count(xs: List[Int]) -> Int { return xs.length() }

fn un(m: Mb[Int]) -> Int {
  return match m {
    Jj(x) => x,
    Nn => 0,
  }
}

fn bump(m: Mb[Int]) -> Mb[Int] {
  return match m {
    Jj(x) => Jj(x + 1),
    Nn => Nn,
  }
}

fn label(m: Mb[Str]) -> Str {
  return match m {
    Jj(s) => "got " + s,
    Nn => "none",
  }
}

fn size(m: Mb[Str]) -> Mb[Int] {
  return match m {
    Jj(s) => Jj(len_of(s)),
    Nn => Nn,
  }
}

fn pick(c: Bool) -> Mb[Int] {
  let v: Mb[Int] = Nn
  let w = Jj(7)
  if (c) {
    return w
  }
  return v
}

fn deep(m: Mb[Mb[Int]]) -> Int {
  return match m {
    Jj(inner) => un(inner) * 10,
    Nn => 0 - 1,
  }
}

fn side(e: Ei[Int, Str]) -> Str {
  return match e {
    Le(n) => "left " + show(n),
    Ri(s) => "right " + s,
  }
}

fn total(m: Mb[List[Int]]) -> Int {
  return match m {
    Jj(xs) => count(xs),
    Nn => 0,
  }
}

fn reuse() -> Str {
  let m = Jj("a")
  let n: Mb[Str] = Nn
  let a = label(m)
  let b = label(m)
  let c = label(n)
  return `${a}/${b}/${c}`
}

fn held(h: Holder) -> Str {
  let s = label(h.slot)
  return `${h.name}:${s}`
}

fn firsts(ms: List[Mb[Int]]) -> Int {
  var acc = 0
  for (m of ms) {
    acc = acc + match m {
      Jj(x) => x,
      Nn => 100,
    }
  }
  return acc
}

fn wild(m: Mb[Int]) -> Int {
  return match m {
    Jj(_) => 1,
    _ => 2,
  }
}

fn flip(p: Ph[Int]) -> Ph[Int] {
  return match p {
    Aa => Bb,
    Bb => Aa,
  }
}

fn is_aa(p: Ph[Int]) -> Bool {
  return match p {
    Aa => true,
    Bb => false,
  }
}

fn opt_of(o: Opt[Int]) -> Int {
  return match o {
    Some(x) => x,
    None => 0,
  }
}

fn literal() -> Int {
  let xs = [Nn, Jj(4)]
  let os = [None, Some(5)]
  var acc = 0
  for (m of xs) {
    acc = acc + un(m)
  }
  for (o of os) {
    acc = acc + opt_of(o)
  }
  return acc
}

test "construct and match a generic variant" {
  assert un(Jj(41)) == 41
  assert un(Nn) == 0
  assert un(bump(Jj(1))) == 2
  assert un(bump(Nn)) == 0
  assert bump(Jj(4)) == Jj(5)
  assert bump(Nn) == Nn
  assert label(Jj("hi")) == "got hi"
  assert label(Nn) == "none"
  assert un(size(Jj("four"))) == 4
  assert size(Nn) == Nn
  assert un(pick(true)) == 7
  assert un(pick(false)) == 0
  assert deep(Jj(Jj(3))) == 30
  assert deep(Jj(Nn)) == 0
  assert deep(Nn) == 0 - 1
  assert side(Le(2)) == "left 2"
  assert side(Ri("r")) == "right r"
  assert total(Jj([1, 2, 3])) == 3
  assert total(Nn) == 0
}

test "generic variant values in lets, records and lists" {
  assert reuse() == "got a/got a/none"
  assert held({ name: "n", slot: Jj("s") }) == "n:got s"
  assert held({ name: "n", slot: Nn }) == "n:none"
  assert firsts([Jj(1), Nn, Jj(2)]) == 103
  assert wild(Jj(5)) == 1
  assert wild(Nn) == 2
  assert Jj("a") != Nn
  assert literal() == 9
  assert is_aa(flip(Bb))
  assert !is_aa(flip(Aa))
  assert flip(Aa) == Bb
}
"""


def _ir():
    return compile_source(PROGRAM, "generic_variant_2471.rvl")


def test_the_variant_is_emitted_as_go_generics():
    src = go_emit.emit(_ir())
    assert "type Mb[T any] interface { isMb(T) }" in src, src
    assert "type MbJj[T any] struct { Value T }" in src, src
    assert "func (MbNn[T]) isMb(T) {}" in src, src
    assert "type Ei[L any, R any] interface { isEi(L, R) }" in src, src
    assert "func un(m Mb[int64]) int64 {" in src, src
    assert "func size(m Mb[string]) Mb[int64] {" in src, src
    assert "case MbJj[int64]:" in src, src
    assert "case MbJj[Mb[int64]]:" in src, src
    assert "MbJj[int64]{Value: 41}" in src, src
    assert "Mb[Int]" not in src and "type MbJj struct" not in src, src


@pytest.mark.skipif(shutil.which("go") is None, reason="go is not installed")
def test_go_runs_the_generic_variant_program_like_py():
    ir = _ir()
    py_status, py_message = RUNNERS["py"](ir)
    assert py_status == "pass", py_message
    status, message = RUNNERS["go"](_ir())
    if status == "skip":
        pytest.skip(message)
    assert status == "pass", message
    assert "2 test(s) passed" in message and "2 test(s) passed" in py_message, (
        py_message, message)


# A service whose operations take and return `Mb[Int]` and `Mb[Str]`: the
# component tier lowers the same variant through its own construct and match.
COMPONENT = """\
type Mb[T] = Jj(T) | Nn

service S {
  fn get(n: Int) -> Mb[Int]
  fn read(m: Mb[Str]) -> Str
  fn bump(m: Mb[Int]) -> Mb[Int]
  fn many(n: Int) -> List[Mb[Int]]
}

component P provides s: S {
  provide s {
    fn get(n) {
      if (n > 0) {
        return Jj(n)
      }
      return Nn
    }
    fn read(m) {
      return match m {
        Jj(s) => s,
        Nn => "none",
      }
    }
    fn bump(m) {
      return match m {
        Jj(x) => Jj(x + 1),
        Nn => Nn,
      }
    }
    fn many(n) {
      return [Jj(n), Nn]
    }
  }
}

fn un(m: Mb[Int]) -> Int {
  return match m {
    Jj(x) => x,
    Nn => 0,
  }
}

fn sum(ms: List[Mb[Int]]) -> Int {
  var acc = 0
  for (m of ms) {
    acc = acc + un(m)
  }
  return acc
}

lifecycle test "a service passes generic variants" {
  load P
  let v = call s.get(41)
  assert un(v) == 41
  let z = call s.get(0)
  assert un(z) == 0
  let r = call s.read(Jj("hi"))
  assert r == "hi"
  let q = call s.read(Nn)
  assert q == "none"
  let b = call s.bump(Jj(1))
  assert un(b) == 2
  let xs = call s.many(5)
  assert sum(xs) == 5
  unload P
  assert no_residue
}
"""


def _component_ir():
    return compile_source(COMPONENT, "generic_variant_component_2471.rvl")


@pytest.mark.skipif(shutil.which("go") is None, reason="go is not installed")
def test_go_runs_a_service_over_the_generic_variant_like_py():
    py_status, py_message = RUNNERS["py"](_component_ir())
    if py_status == "skip":
        pytest.skip(py_message)
    assert py_status == "pass", py_message
    status, message = RUNNERS["go"](_component_ir())
    if status == "skip":
        pytest.skip(message)
    assert status == "pass", message
    assert "1 test(s) passed" in message and "1 test(s) passed" in py_message, (
        py_message, message)


def test_the_placement_bridge_refuses_a_generic_variant_by_name():
    # The bridge codecs are per declared type; a generic variant has one wire
    # shape per instantiation, so the bridge names it instead of emitting a
    # `_revlEncodeMb(v Mb)` that does not build.
    with pytest.raises(go_emit.EmitError) as err:
        go_emit.emit_placement(_component_ir())
    assert "type 'Mb[T]' is not lowerable on the go placement bridge" in str(
        err.value)


UNPINNED = """\
type Mb[T] = Jj(T) | Nn

service S {
  fn keep(n: Int) -> Int
}

component P provides s: S {
  provide s {
    fn keep(n) {
      let v = Nn
      return n
    }
  }
}
"""


def test_an_unpinned_type_argument_is_refused_by_name():
    # Nothing pins `T` for this `Nn`, and Go infers no type argument from
    # later use: the emitter names the variant instead of printing an
    # `MbNn[Any]{}` that does not build.
    with pytest.raises(go_emit.EmitError) as err:
        go_emit.emit(compile_source(UNPINNED, "unpinned_2471.rvl"))
    assert "variant case `Nn`: type 'Mb[T]' has no type argument for T" in str(
        err.value)
