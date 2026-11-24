"""Issue #3582: a top-level `fn` named like `__h` was emitted as a module-level
`def __h()` on the py tier, but a call to it written inside a provided method
is emitted inside a class body, where Python rewrites the name (`_C__h`) and
the call raises NameError. The shared lowerer now refuses the spelling, so
every tier refuses identically; a true dunder (`__h__`) is not mangled and
stays admitted."""

import pytest

from revl import RevlError, compile_source


def test_a_dunder_prefix_fn_name_is_refused_and_names_the_rule():
    src = (
        "fn __h() -> Int { return 1 }\n"
        "service S { fn go() -> Int }\n"
        "component C provides s: S {\n"
        "  provide s {\n"
        "    fn go() = __h()\n"
        "  }\n"
        "}\n"
    )
    with pytest.raises(RevlError) as excinfo:
        compile_source(src, "dunder.rvl")
    msg = str(excinfo.value)
    assert "`__h`" in msg and "class body" in msg and "#3582" in msg


def test_a_true_dunder_fn_name_is_admitted():
    src = "fn __h__() -> Int { return 1 }\npub fn out() -> Int { return __h__() }\n"
    compile_source(src, "dunder_ok.rvl")


def test_a_plain_fn_name_is_admitted():
    src = "fn h() -> Int { return 1 }\npub fn out() -> Int { return h() }\n"
    compile_source(src, "plain.rvl")
