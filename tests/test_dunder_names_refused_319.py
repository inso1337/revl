#!/usr/bin/env python3
"""Test for issue #319: dunder / prototype-named record fields and service methods.

Dunder / prototype names (`__proto__`, `__init__`, `__getattr__`, ...) hijack host
protocols when they reach a record field or a service method: a Python dataclass
grows an `__init__`/`__getattr__` hook, and a JS/TS object literal silently drops a
`__proto__` value instead of storing it. revl refuses them at the frontend so no
backend ever emits one. The refusal is complementary to the reserved-word /
predeclared-name renaming (`_safe_name`) that legal-but-colliding identifiers get.
"""


import pytest

from revl import RevlError
from revl.compiler import compile_source


def _expect_reject(src: str, needle: str):
    with pytest.raises(RevlError) as excinfo:
        compile_source(src, "t.rvl")
    msg = str(excinfo.value)
    assert "issue #319" in msg, f"refusal should cite issue #319, got: {msg}"
    assert needle in msg, f"refusal should name `{needle}`, got: {msg}"
    return msg


def test_dunder_record_field_refused():
    """A dunder record field is refused (would become a py/ts protocol hook)."""
    src = """
type Weird = { __proto__: Str, normal: Str }
fn mk() -> Weird { return { __proto__: "p", normal: "n" } }
"""
    _expect_reject(src, "__proto__")


def test_dunder_record_field_getattr_refused():
    src = """
type Weird = { __getattr__: Str, normal: Str }
fn mk() -> Weird { return { __getattr__: "g", normal: "n" } }
"""
    _expect_reject(src, "__getattr__")


def test_dunder_service_method_refused():
    """A dunder service method is refused (would become a py/ts protocol hook)."""
    src = """
service S {
    fn __init__(x: Int) -> Int
    fn normal(x: Int) -> Int
}
"""
    _expect_reject(src, "__init__")


def test_dunder_service_method_getattr_refused():
    src = """
service S {
    fn __getattr__(x: Int) -> Int
    fn normal(x: Int) -> Int
}
"""
    _expect_reject(src, "__getattr__")


def test_normal_record_and_service_compile():
    """A record + service with ordinary names still compiles cleanly."""
    src = """
type Point = { x: Int, y: Int }
service S {
    fn normal(x: Int) -> Int
}
fn mk() -> Point { return { x: 1, y: 2 } }
"""
    ir = compile_source(src, "t.rvl")
    assert ir is not None
    assert "x" in ir["types"]["Point"]["fields"]
    assert "y" in ir["types"]["Point"]["fields"]


# issue #3140: an anonymous record literal (or record update) is not a declared
# record, so the declaration check never saw its field names. `{ __proto__: 1 }`
# on the ts tier is not a property but a prototype assignment, so the value
# vanished from `Object.keys`, `revlEq` and `JSON.stringify` while python kept it.


def test_dunder_record_literal_field_refused_3140():
    """The reproduction from #3140: both functions are refused at the frontend."""
    src = """
pub fn same() -> Bool {
  let a = { __proto__: 1, x: 1 }
  let b = { __proto__: 2, x: 1 }
  return a == b
}
pub fn rd() -> Int {
  let a = { __proto__: 7, x: 1 }
  return a.__proto__
}
"""
    msg = _expect_reject(src, "__proto__")
    assert "record literal" in msg


def test_dunder_record_literal_other_dunder_refused_3140():
    src = """
pub fn f() -> Int {
  let a = { __init__: 1, x: 1 }
  return a.x
}
"""
    _expect_reject(src, "__init__")


def test_dunder_record_literal_nested_and_listed_refused_3140():
    """Position does not matter: a nested literal and a list item are refused."""
    src = """
pub fn f() -> Int {
  let a = { inner: { __proto__: 1 }, x: 1 }
  return a.x
}
"""
    _expect_reject(src, "__proto__")
    src = """
pub fn g() -> Int {
  let xs = [{ __proto__: 1 }]
  return xs.len()
}
"""
    _expect_reject(src, "__proto__")


def test_dunder_record_update_field_refused_3140():
    """An update naming a dunder cannot reach a backend (the checker refuses a
    name the base record lacks, and no record can hold a dunder field)."""
    src = """
pub fn f() -> Int {
  let a = { x: 1 }
  let b = { a | __proto__ = 2 }
  return b.x
}
"""
    with pytest.raises(RevlError) as excinfo:
        compile_source(src, "t.rvl")
    assert "__proto__" in str(excinfo.value)


def test_dunder_record_literal_in_component_body_refused_3140():
    """A component body lowers through its own pure-expression path; it refuses too."""
    src = """
service S { fn go(t: Int) -> Int }
component C provides s: S {
  provide s {
    fn go(t) {
      let r = { __proto__: t, y: 1 }
      return r.y
    }
  }
}
"""
    _expect_reject(src, "__proto__")


def test_dunder_record_literal_in_param_default_refused_3140():
    """A default is lowered at each call that omits the argument, through the
    same path, so a dunder literal in a default is refused at that call."""
    src = """
pub fn f(a: Int = { __proto__: 1, x: 2 }.x) -> Int { return a }
pub fn g() -> Int { return f() }
"""
    _expect_reject(src, "__proto__")


def test_ordinary_record_literal_and_update_compile_3140():
    """A literal with ordinary (even underscore-leading) names still compiles."""
    src = """
pub fn f() -> Int {
  let a = { _x: 1, y__: 2, __z: 3 }
  let b = { a | y__ = 5 }
  return b.y__ + a.__z
}
"""
    assert compile_source(src, "t.rvl") is not None


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
