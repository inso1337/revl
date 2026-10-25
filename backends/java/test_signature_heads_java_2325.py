"""Generic variants, `Unit`, `Result`, `Set` and `Stream` in java signatures (#2325).

WHAT WAS WRONG. These signature shapes emitted Java that javac rejects:

* a generic variant (`type Box[T] = Full(T) | Empty`) declared no type
  parameter, so `Box<java.lang.Long>` in a signature named a class that takes
  none, and a `T` payload had no `T` in scope;
* `Unit` as a type argument rendered `void` (`java.util.List<void>`), which is
  not a type argument;
* a `Result[..]` named only in a signature referenced `RevlResult`, a class
  emitted only when a document builds or matches an `Ok`/`Err`;
* a `Set[..]` or `Stream[..]` head rendered as the bare class name
  (`Set<java.lang.Long>`, `Stream<java.lang.Long>`), and no such class exists in
  the unit.

WHAT IS EMITTED NOW. A generic variant is `Box<T>` and each case
`Full<T> implements Box<T>`. `Unit` boxes to `java.lang.Void`, a signature naming
`Result[..]` pulls in `RevlResult`, `Set[T]` is `java.util.Set<T>`, and
`Stream[T]` widens to `Object` (the tier's `Stream` runtime class is emitted
only for a document that holds a stream and takes no type parameter; rust
widens the same head to `Value`).

The compile tests need a JDK and skip without one. A skip is not a pass: CI's
`backend-java` job provisions a JDK.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))
if str(ROOT / "tests") not in sys.path:
    sys.path.append(str(ROOT / "tests"))

from _load_by_path import load_by_path  # noqa: E402

javac_gate = load_by_path("javac_gate", HERE / "javac_gate.py")
java_emit = load_by_path("revl_java_emit_2325", HERE / "emit.py")
emit = java_emit.emit

from revl.compiler import compile_source  # noqa: E402

needs_jdk = pytest.mark.skipif(javac_gate.JAVAC is None, reason=javac_gate.NO_JDK)

UNIT_PROGRAM = """\
pub fn units(xs: List[Unit]) -> List[Unit] { return xs }
pub fn maybe(x: Opt[Unit]) -> Opt[Unit] { return x }
pub fn table(m: Map[Str, Unit]) -> Map[Str, Unit] { return m }
"""

RESULT_PROGRAM = """\
pub fn settle(r: Result[Unit, Str]) -> Result[Unit, Str] { return r }
pub fn results(rs: List[Result[Int, Str]]) -> List[Result[Int, Str]] { return rs }
"""

SET_PROGRAM = """\
pub fn tags(s: Set[Int]) -> Set[Int] { return s }
pub fn names(s: Set[Str]) -> Set[Str] { return s }
"""

STREAM_PROGRAM = """\
pub fn feed(s: Stream[Int]) -> Stream[Int] { return s }
"""

#: Declaration side only: no value of a generic variant can be built yet (the
#: checker refuses `Full(1)` for `Box[Int]`, issue #2402), and a `match` that
#: binds a generic payload is not covered here.
VARIANT_PROGRAM = """\
type Box[T] = Full(T) | Empty
type Either[L, R] = Left(L) | Right(R) | Neither
pub fn keep(b: Box[Int]) -> Box[Int] { return b }
pub fn eithers(es: List[Either[Str, Int]]) -> List[Either[Str, Int]] { return es }
pub fn count(bs: List[Box[Str]]) -> Int { return bs.length() }
"""

#: A generic record built in a `let` (issue comment 4). It used to be built
#: raw, `new Bx(n)`, so `final var b` was a raw `Bx` and `b.v + 1L` read an
#: `Object`. It is built with the diamond now, and javac infers `Bx<Long>`.
RECORD_LITERAL_PROGRAM = """\
type Bx[T] = { v: T }
type Pair[A, B] = { first: A, second: B }
pub fn bump(n: Int) -> Int {
  let b: Bx[Int] = { v: n }
  return b.v + 1
}
pub fn both(n: Int) -> Int {
  let p = { first: "a", second: n }
  return p.second * 2 + p.first.length()
}
pub fn many(n: Int) -> List[Bx[Int]] { return [{ v: n }, { v: n + 1 }] }
pub fn nested(n: Int) -> Int {
  let b = { v: { v: n } }
  return b.v.v + 1
}
pub fn some(n: Int) -> Int {
  let b = { v: Some(n) }
  return match b.v { Some(x) => x + 1, None => 0 }
}
pub fn listed(n: Int) -> Int {
  let b = { v: [n, 2] }
  return b.v.length()
}
"""

#: Controls: a literal whose field javac would infer looser than revl types it
#: (an empty list, `None`, `Map.empty()`, a variant case, a local bound to one,
#: a field read off a raw record) stays raw, because `Bx<List<Object>>` does
#: not convert to the declared `Bx<List<Long>>` where the raw `Bx` did. Each
#: compiles on main and must keep compiling.
LOOSE_RECORD_PROGRAM = """\
type Bx[T] = { v: T }
type Box[T] = Full(T) | Empty
type S = A | B(Int)
pub fn e() -> Bx[List[Int]] {
  let b = { v: [] }
  return b
}
pub fn o() -> Bx[Opt[Int]] {
  let b = { v: None }
  return b
}
pub fn m() -> Bx[Map[Str, Int]] {
  let b = { v: Map.empty() }
  return b
}
pub fn s() -> Bx[S] {
  let b = { v: B(1) }
  return b
}
pub fn a() -> Bx[S] {
  let b = { v: A }
  return b
}
pub fn r(n: Int) -> Bx[Result[Int, Str]] {
  let b = { v: Ok(n) }
  return b
}
pub fn g(n: Int) -> Bx[Box[Int]] {
  let b = { v: Full(n) }
  return b
}
pub fn local_list() -> Bx[List[Int]] {
  let x = []
  let b = { v: x }
  return b
}
pub fn local_none() -> Bx[Opt[Int]] {
  let x = None
  let b = { v: x }
  return b
}
pub fn reassigned() -> Bx[List[Int]] {
  var x = []
  x = [1]
  let b = { v: x }
  return b
}
pub fn raw_field() -> Bx[List[Int]] {
  let a = { v: [] }
  let b = { v: a.v }
  return b
}
pub fn raw_nested() -> Bx[Bx[List[Int]]] {
  let a = { v: [] }
  let b = { v: a }
  return b
}
"""

PROGRAMS = {
    "variant": VARIANT_PROGRAM,
    "record_literal": RECORD_LITERAL_PROGRAM,
    "loose_record": LOOSE_RECORD_PROGRAM,
    "unit": UNIT_PROGRAM,
    "result": RESULT_PROGRAM,
    "set": SET_PROGRAM,
    "stream": STREAM_PROGRAM,
}


def _java_source(src: str) -> str:
    out = emit(compile_source(src, "probe_2325.rvl"))
    return out if isinstance(out, str) else "\n".join(map(str, out.values()))


def test_generic_variant_declares_its_parameters():
    java = _java_source(VARIANT_PROGRAM)
    assert "public sealed interface Box<T> permits Box.Full, Box.Empty {" in java
    assert "final class Full<T> implements Box<T> {" in java
    assert "final class Empty<T> implements Box<T> {" in java
    assert "final class Neither<L, R> implements Either<L, R> {" in java
    assert "public static Box<java.lang.Long> keep(Box<java.lang.Long> b)" in java


def test_plain_variant_is_not_parameterised():
    java = _java_source("type S = A | B(Int)\npub fn f(s: S) -> S { return s }\n")
    assert "public sealed interface S permits S.A, S.B {" in java
    assert "final class B implements S {" in java


def test_unit_type_argument_is_void_class():
    java = _java_source(UNIT_PROGRAM)
    assert "java.util.List<java.lang.Void> units(java.util.List<java.lang.Void> xs)" in java
    assert "java.util.Optional<java.lang.Void> maybe(" in java
    assert "java.util.Map<String, java.lang.Void> table(" in java
    assert "<void>" not in java and ", void>" not in java


def test_unit_outside_a_type_argument_stays_void():
    assert java_emit._java_v3_type("Unit") == "void"
    assert java_emit._java_v3_type("Unit", boxed=True) == "java.lang.Void"


def test_result_in_a_signature_emits_revl_result():
    java = _java_source(RESULT_PROGRAM)
    assert "public sealed interface RevlResult<T, E>" in java
    assert "RevlResult<java.lang.Void, String> settle(" in java
    assert "java.util.List<RevlResult<java.lang.Long, String>> results(" in java


def test_no_result_no_revl_result():
    assert "RevlResult" not in _java_source(UNIT_PROGRAM)


def test_set_head_is_java_util_set():
    java = _java_source(SET_PROGRAM)
    assert "java.util.Set<java.lang.Long> tags(java.util.Set<java.lang.Long> s)" in java
    assert "java.util.Set<String> names(" in java


def test_stream_head_widens_to_object():
    java = _java_source(STREAM_PROGRAM)
    assert "public static Object feed(Object s)" in java
    assert "Stream<" not in java


def test_generic_record_literal_uses_the_diamond():
    java = _java_source(RECORD_LITERAL_PROGRAM)
    assert "final var b = new Bx<>(n);" in java
    assert "final var p = new Pair<>(\"a\", n);" in java
    assert "java.util.List.of(new Bx<>(n), new Bx<>(" in java
    assert "final var b = new Bx<>(new Bx<>(n));" in java
    assert "final var b = new Bx<>(java.util.Optional.of(n));" in java


def test_plain_record_literal_has_no_diamond():
    java = _java_source(
        "type P = { x: Int }\npub fn f(n: Int) -> Int {\n  let p = { x: n }\n"
        "  return p.x\n}\n")
    assert "final var p = new P(n);" in java


def test_loosely_inferred_record_literal_stays_raw():
    java = _java_source(LOOSE_RECORD_PROGRAM)
    for arg in ("java.util.List.of()", "java.util.Optional.empty()",
                "java.util.Map.of()", "new S.B(1L)", "new S.A()",
                "new RevlResult.Ok<>(n)", "new Box.Full(n)", "x", "a.v", "a"):
        assert f"final var b = new Bx({arg});" in java
    assert "new Bx<>" not in java


def test_generic_fn_call_keeps_the_record_raw():
    # Text only: a generic top-level fn does not compile on this tier yet (its
    # method declares no `<U>`), but its return type is inferred from its
    # arguments, so once it does, a diamond around it would be loose.
    java = _java_source(
        "type Bx[T] = { v: T }\nfn wrap[U](x: U) -> Opt[U] { return Some(x) }\n"
        "pub fn g(n: Int) -> Bx[Opt[Int]] {\n  let b = { v: wrap(n) }\n"
        "  return b\n}\n")
    assert "final var b = new Bx(wrap(n));" in java


@needs_jdk
@pytest.mark.parametrize("shape", sorted(PROGRAMS))
def test_signature_heads_compile_on_java(shape):
    javac_gate.compile_check(_java_source(PROGRAMS[shape]), f"{shape} signature head")
