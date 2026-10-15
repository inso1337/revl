"""A user generic variant builds and runs on the rust tier (issue #2471).

WHAT WAS WRONG. `type Mb[T] = Jj(T) | Nn` passed the checker (#2470) and then
emitted Rust that does not build: the enum declared no type parameter, so its
`Jj(T)` payload fell back to `Value`, and every `Mb[Int]` in a signature also
became `Value` (E0308 at each call, E0369 / E0277 on the payload arithmetic).

WHAT IS EMITTED NOW. The enum declares the variant's parameters
(`pub enum Mb<T> { Jj(T), Nn }`), a payload naming one keeps it, and an
applied `Mb[Int]` is `Mb<i64>`. Rust infers the type argument of every
construction from where the value flows, so `Mb::Jj(41i64)` and `Mb::Nn` need
no turbofish.

The run test executes the program's `test` blocks on the py tier and on rust
and asserts both pass the same assertions. It needs cargo and skips without it
(or without cordis-rs in an offline registry); a skip is not a pass, CI's
`backend-rust` job has both.
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

_spec = importlib.util.spec_from_file_location("revl_rust_emit_2471", HERE / "emit.py")
rust_emit = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rust_emit)

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


def test_the_variant_enum_declares_its_type_parameters():
    src = rust_emit.emit(_ir())
    assert "pub enum Mb<T> {\n    Jj(T),\n    Nn,\n}" in src, src
    assert "pub enum Ei<L, R> {\n    Le(L),\n    Ri(R),\n}" in src, src
    # `Ph[T]` names `T` in no case, and rustc rejects an unused parameter
    # (E0392): a hidden case carries it as PhantomData
    assert "    __RevlPhantom(std::marker::PhantomData<fn() -> (T,)>),\n}" in src, src
    assert "    slot: Mb<String>," in src, src
    assert "fn un(m: Mb<i64>) -> i64 {" in src, src
    assert "fn size(m: Mb<String>) -> Mb<i64> {" in src, src
    assert "fn deep(m: Mb<Mb<i64>>) -> i64 {" in src, src
    assert "fn total(m: Mb<Vec<i64>>) -> i64 {" in src, src
    assert "Jj(Value)" not in src and "(m: Value)" not in src, src


@pytest.mark.skipif(shutil.which("cargo") is None, reason="cargo is not installed")
def test_rust_runs_the_generic_variant_program_like_py():
    ir = _ir()
    py_status, py_message = RUNNERS["py"](ir)
    assert py_status == "pass", py_message
    status, message = RUNNERS["rust"](_ir())
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



@pytest.mark.skipif(shutil.which("cargo") is None, reason="cargo is not installed")
def test_rust_runs_a_service_over_the_generic_variant_like_py():
    py_status, py_message = RUNNERS["py"](_component_ir())
    if py_status == "skip":
        pytest.skip(py_message)
    assert py_status == "pass", py_message
    status, message = RUNNERS["rust"](_component_ir())
    if status == "skip":
        pytest.skip(message)
    assert status == "pass", message
    assert "1 test(s) passed" in message and "1 test(s) passed" in py_message, (
        py_message, message)
