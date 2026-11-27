"""A module-level def whose name Python mangles at a class-body reference is
refused on the py tier (#3582).

Python rewrites a reference to `__h` inside a class body as `_Class__h`, while
the module-level `def __h()` keeps its spelling. A provided method is emitted
inside a class body, so its call `__h()` mangled to `_Class__h()` and raised
NameError at run time instead of reaching the function. Top-level `fn` and
`extern` names that start with `__` and do not end with `__` are now refused at
emit naming the symbol and the tier. Locals and parameters named `__x` are
unaffected: their declaration and every use sit in the same class body, so
both mangle identically.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for p in (ROOT / "src", ROOT / "tests"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from _backend_import import backend_emitter  # noqa: E402
from revl import compile_source  # noqa: E402

EMIT = backend_emitter("python")


def _emitted(source: str, name: str = "dunder_fn_3582.rvl") -> str:
    emitted = EMIT.emit(compile_source(source, name))
    if isinstance(emitted, dict):
        return "\n".join(v for v in emitted.values() if isinstance(v, str))
    return str(emitted)


def test_issue_repro_a_provided_method_calling_fn_dunder_h_is_refused():
    """The issue's repro: `fn __h` plus a provided method whose body calls it.
    Before the fix this emitted and the call raised NameError at run time."""
    source = """
fn __h() -> Int { return 1 }
service S { fn go() -> Int }
component C provides s: S {
  provide s {
    fn go() = __h()
  }
}
"""
    with pytest.raises(EMIT.EmitError) as err:
        _emitted(source)
    message = str(err.value)
    assert "function `__h`" in message
    assert "#3582" in message


def test_a_plain_top_level_fn_named_dunder_h_is_refused():
    source = "fn __h() -> Int { return 1 }\n"
    with pytest.raises(EMIT.EmitError) as err:
        _emitted(source)
    assert "function `__h`" in str(err.value)


def test_an_extern_named_dunder_h_is_refused():
    source = "extern pure fn __h() -> Int = @py { return 1 }\n"
    with pytest.raises(EMIT.EmitError) as err:
        _emitted(source)
    message = str(err.value)
    assert "extern `__h`" in message
    assert "#3582" in message


@pytest.mark.parametrize("name", ["__h", "__hidden", "__x"])
def test_every_leading_double_underscore_fn_name_is_refused(name):
    with pytest.raises(EMIT.EmitError) as err:
        _emitted(f"fn {name}() -> Int {{ return 1 }}\n")
    assert f"`{name}`" in str(err.value)


@pytest.mark.parametrize("source, needle", [
    ("fn __x__() -> Int { return 1 }\n", "def __x__():"),
    ("fn h() -> Int { return 1 }\n", "def h():"),
    ("fn __init__() -> Int { return 1 }\n", "def __init__():"),
])
def test_names_that_do_not_mangle_still_emit(source, needle):
    """Controls: dunder-ended names and ordinary names are unchanged."""
    assert needle in _emitted(source)


def test_a_local_named_dunder_x_still_works_inside_a_fn_body():
    """Locals are renamed at declaration AND use inside the same body, so a
    `__x` local is consistent and still runs."""
    source = "fn f() -> Int { let __x = 1\n  return __x + 1 }\n"
    text = _emitted(source)
    assert "def f():" in text
    ns: dict = {}
    exec(compile(text, "<dunder_fn_3582>", "exec"), ns)
    assert ns["f"]() == 2
