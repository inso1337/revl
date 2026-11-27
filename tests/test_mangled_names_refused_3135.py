#!/usr/bin/env python3
"""Test for issue #3135: a name Python would mangle in a class body is refused.

A service method, record field or config field named `__a` (two leading
underscores, not two trailing ones) was emitted unchanged on the python tier,
where a class body rewrites every such name to `_Class__a`. A call written as
`r.__a()` inside a provided method then reached `_S__a`, a different method of
the same service, and the provider's `def __a` was stored under `_R__a`, so the
contract name `__a` did not exist. The frontend now refuses the name on every
tier, the way it refuses a dunder (issue #319).
"""

import pytest

from revl import RevlError
from revl.compiler import compile_source

# The reproduction from the issue: a service declaring both `__a` and `_S__a`,
# and a consumer whose own provided class is `_S`.
ISSUE_REPRO = """
service R { fn __a() -> Int
            fn _S__a() -> Int }
service S { fn go() -> Int }
component Prov provides r: R {
  provide r {
    fn __a() = 1
    fn _S__a() = 2
  }
}
component Cons requires r: R provides s: S {
  provide s {
    fn go() = r.__a()
  }
}
"""


def _expect_reject(src: str, name: str, kind: str):
    with pytest.raises(RevlError) as excinfo:
        compile_source(src, "t.rvl")
    msg = str(excinfo.value)
    assert "issue #3135" in msg, f"refusal should cite issue #3135, got: {msg}"
    assert f"{kind} `{name}`" in msg, f"refusal should name the {kind} `{name}`, got: {msg}"
    return msg


def test_issue_reproduction_is_refused():
    _expect_reject(ISSUE_REPRO, "__a", "method")


@pytest.mark.parametrize("name", ["__a", "__approve", "___a", "__a_", "__a1"])
def test_mangled_service_method_refused(name):
    src = f"service S {{\n  fn {name}(x: Int) -> Int\n  fn normal(x: Int) -> Int\n}}\n"
    _expect_reject(src, name, "method")


def test_mangled_implemented_method_refused():
    src = """
service S { fn __a() -> Int }
component C provides s: S {
  provide s {
    fn __a() = 1
  }
}
"""
    _expect_reject(src, "__a", "method")


@pytest.mark.parametrize("name", ["__a", "__approve"])
def test_mangled_record_field_refused(name):
    src = f"""
type Rec = {{ {name}: Int, normal: Int }}
fn mk() -> Rec {{ return {{ {name}: 1, normal: 2 }} }}
"""
    _expect_reject(src, name, "field")


def test_mangled_config_field_refused():
    src = """
service S { fn go() -> Int }
component C provides s: S {
  config { __a: Int }
  provide s {
    fn go() = config.__a
  }
}
"""
    _expect_reject(src, "__a", "config field")


@pytest.mark.parametrize("name", ["_a", "_S__a", "a__", "a__b", "__", "___", "_"])
def test_names_python_leaves_alone_still_compile(name):
    """A name Python does not mangle is not refused: a single leading
    underscore, an inner double underscore, and a name that ends with two
    underscores (`__` and `___` included)."""
    src = f"""
service R {{ fn {name}() -> Int }}
type Rec = {{ {name}: Int }}
component Prov provides r: R {{
  provide r {{
    fn {name}() = 1
  }}
}}
"""
    assert compile_source(src, "t.rvl") is not None


def test_both_names_of_the_issue_pair_without_the_mangled_one_compile():
    """The confusable pair stops being expressible, the unconfusable one stays."""
    src = ISSUE_REPRO.replace("fn __a() -> Int", "fn _a() -> Int").replace(
        "fn __a() = 1", "fn _a() = 1").replace("r.__a()", "r._a()")
    assert compile_source(src, "t.rvl") is not None


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
