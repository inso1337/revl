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


# ------------------------------------------------------------- issue #3592

def test_generic_record_destructure_naming_missing_field_is_refused():
    # the field lookup used the full spelling `Box[Int]`, which is not a
    # declared name, so the check was skipped entirely and this COMPILED.
    src = ("type Box[T] = { v: T }\n"
           "fn f(b: Box[Int]) -> Int { let { z } = b; return 0 }\n")
    with pytest.raises(RevlError) as excinfo:
        compile_source(src)
    msg = str(excinfo.value)
    assert "record destructuring names `z`, which is not a field of `Box[Int]`" in msg
    assert "fields: `v`" in msg


def test_generic_record_two_params_naming_missing_field_is_refused():
    src = ("type Pair[A, B] = { a: A, b: B }\n"
           "fn f(p: Pair[Int, Str]) -> Int { let { c } = p; return 0 }\n")
    with pytest.raises(RevlError, match=r"names `c`, which is not a field of "
                                         r"`Pair\[Int, Str\]`"):
        compile_source(src)


def test_generic_record_destructure_instantiates_field_type():
    # `Box[Int]` is `{v: Int}`: the bound name takes the SUBSTITUTED field
    # type, so `v + 1` type-checks and returning `v` as Str is refused.
    compile_source("type Box[T] = { v: T }\n"
                   "fn f(b: Box[Int]) -> Int { let { v } = b; return v + 1 }\n")
    with pytest.raises(RevlError, match="return expects `Str`, got `Int`"):
        compile_source("type Box[T] = { v: T }\n"
                       "fn f(b: Box[Int]) -> Str { let { v } = b; return v }\n")


def test_empty_record_hint_says_no_fields():
    # `{}` has no fields: the hint names that instead of printing an empty
    # `fields: ` list (the record-update refusal below shares the helper).
    src = "fn f() -> Int { let r = {}; let { q } = r; return 0 }\n"
    with pytest.raises(RevlError) as excinfo:
        compile_source(src)
    assert "the record declares no fields" in str(excinfo.value)
    src = "fn f() -> Int { let r = {}; let u = { r | q = 1 }; return 0 }\n"
    with pytest.raises(RevlError) as excinfo:
        compile_source(src)
    assert "record update names `q`, which is not a field of `{}`" in str(excinfo.value)
    assert "the record declares no fields" in str(excinfo.value)
