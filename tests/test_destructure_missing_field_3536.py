"""Issue #3536: a record destructuring `let` naming an undeclared field.

`let { zz, x } = { x: 1 }` compiled: the pattern was only checked against a
NOMINAL record type, so a structural value (an anonymous literal) let any
name through, `__proto__` included, though a record literal or update naming
`__proto__` is refused. The pattern is now checked against the structural
shape too, and the refusal names the field and the type the way a record
update naming a missing field does.
"""

import pytest

from revl.compiler import compile_source
from revl.errors import RevlError


@pytest.mark.parametrize("field", ["zz", "__proto__"])
def test_structural_destructure_naming_missing_field_is_refused(field):
    src = f"fn f() -> Int {{ let {{ {field}, x }} = {{ x: 1 }}; return x }}\n"
    with pytest.raises(RevlError) as excinfo:
        compile_source(src)
    msg = str(excinfo.value)
    assert (f"record destructuring names `{field}`, which is not a field of "
            "`{x: Int}`") in msg
    assert "fields: `x`" in msg


def test_let_bound_structural_value_is_checked_too():
    src = "fn f() -> Int { let r = { x: 1 }; let { zz } = r; return 0 }\n"
    with pytest.raises(RevlError, match=r"names `zz`, which is not a field of `\{x: Int\}`"):
        compile_source(src)


def test_nominal_destructure_naming_missing_field_is_refused():
    src = ("type P = { x: Int }\n"
           "fn f(p: P) -> Int { let { zz, x } = p; return x }\n")
    with pytest.raises(RevlError, match=r"names `zz`, which is not a field of `P`"):
        compile_source(src)


def test_destructuring_declared_fields_still_compiles():
    compile_source("fn f() -> Int { let { x } = { x: 1, y: 2 }; return x }\n")
    compile_source("type P = { x: Int, y: Int }\n"
                   "fn g(p: P) -> Int { let { x, y } = p; return x + y }\n")
