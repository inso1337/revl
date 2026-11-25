"""Issue #3582: a top-level `fn` named like a Python-mangled identifier is
refused instead of emitting code that breaks when called from a provided
method.

A name starting with `__` and not ending with `__` (`__h`) is accepted as a
module-level `def __h()` on the py tier, but Python rewrites the same
identifier inside a class body to `_Class__h` — so a call written in a
provided method was emitted as `return __h()` inside the class and raised
`NameError` at run time instead of reaching the function (#3135 already
refuses dunder `__x__` names for service methods, record fields and config
fields; this is the fn-name twin).

The name is now refused at the frontend in `_lower_fns`, next to the duplicate
check, with a message naming the py mangling and the issue. Dunder names
(`__h__`) and single-underscore names (`_x`) are unaffected — Python does not
mangle those inside a class body.

Checked here:
  * the issue's exact repro is refused, naming the fn and the rule;
  * `__h__` and `_x` fn names still compile and the py tier emits and runs
    them;
  * a provided-method call to a dunder-named fn still emits and runs (the
    module-level `def __h__` and the in-class call agree, since `__h__` is not
    mangled).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import RevlError  # noqa: E402
from revl.compiler import compile_source  # noqa: E402

#: the issue's exact repro: `__h` called from a provided method.
REPRO = (
    "fn __h() -> Int { return 1 }\n"
    "service S { fn go() -> Int }\n"
    "component C provides s: S {\n"
    "  provide s {\n"
    "    fn go() = __h()\n"
    "  }\n"
    "}\n"
)


def test_mangled_fn_name_is_refused_with_the_issue_named():
    with pytest.raises(RevlError) as excinfo:
        compile_source(REPRO, "m.rvl")
    err = excinfo.value
    assert "`__h`" in err.message
    assert "name-mangled" in err.message
    assert "#3582" in err.message
    assert err.line == 1


def test_dunder_and_single_underscore_fn_names_still_compile():
    compile_source("fn __h__() -> Int { return 1 }\nfn f() -> Int { return __h__() }\n",
                   "ok.rvl")
    compile_source("fn _x() -> Int { return 1 }\nfn f() -> Int { return _x() }\n",
                   "ok2.rvl")


def test_dunder_named_fn_called_from_a_provided_method_emits_unmangled_on_py():
    src = (
        "fn __h__() -> Int { return 41 }\n"
        "service S { fn go() -> Int }\n"
        "component C provides s: S {\n"
        "  provide s {\n"
        "    fn go() = __h__() + 1\n"
        "  }\n"
        "}\n"
    )
    ir = compile_source(src, "d.rvl")
    spec = importlib.util.spec_from_file_location(
        "pyemit_3582", ROOT / "backends" / "python" / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    emitted = module.emit(ir)
    # `__h__` ends with two underscores, so Python does not mangle it inside
    # the class body: the def and the in-class call keep the same spelling
    assert "def __h__():" in emitted
    # the in-class call keeps the same spelling (wrapped in the Int-bounds
    # check, so match the call itself)
    assert "__h__()" in emitted
    assert "_C__h__" not in emitted and "_C__h(" not in emitted
