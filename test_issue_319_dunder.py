#!/usr/bin/env python3
"""Test for issue #319: dunder / prototype-named record fields and service methods.

Dunder / prototype names (`__proto__`, `__init__`, `__getattr__`, ...) hijack host
protocols when they reach a record field or a service method: a Python dataclass
grows an `__init__`/`__getattr__` hook, and a JS/TS object literal silently drops a
`__proto__` value instead of storing it. revl refuses them at the frontend so no
backend ever emits one. The refusal is complementary to the reserved-word /
predeclared-name renaming (`_safe_name`) that legal-but-colliding identifiers get.
"""

import sys

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


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
