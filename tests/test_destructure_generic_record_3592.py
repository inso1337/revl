"""Issue #3592: a record destructuring `let` on an APPLIED generic record.

`type Box[T] = { v: T }` and `let { z } = b` with `b: Box[Int]` compiled: the
pattern's field lookup read the type table by the value's full spelling,
`Box[Int]`, which is not a declared name, so neither the field check (#3536)
nor the field types applied. The lookup now resolves the generic at its
instantiation, the way a record literal held to `Box[Int]` is, so a missing
field is refused with the same sentence a non-generic record draws and a bound
field carries its instantiated type.
"""

import pytest

from revl.compiler import compile_source
from revl.errors import RevlError

BOX = "type Box[T] = { v: T }\n"


def test_generic_destructure_naming_missing_field_is_refused():
    src = BOX + "fn f(b: Box[Int]) -> Int { let { z } = b; return 0 }\n"
    with pytest.raises(RevlError) as excinfo:
        compile_source(src)
    msg = str(excinfo.value)
    assert "record destructuring names `z`, which is not a field of `Box[Int]`" in msg
    assert "fields: `v`" in msg


def test_generic_destructure_names_the_first_missing_field():
    src = BOX + "fn f(b: Box[Int]) -> Int { let { v, z } = b; return v }\n"
    with pytest.raises(RevlError, match=r"names `z`, which is not a field of `Box\[Int\]`"):
        compile_source(src)


def test_generic_destructure_binds_the_instantiated_field_type():
    src = BOX + "fn f(b: Box[Int]) -> Str { let { v } = b; return v }\n"
    with pytest.raises(RevlError, match=r"return expects `Str`, got `Int`"):
        compile_source(src)


def test_generic_destructure_of_declared_fields_still_compiles():
    compile_source(BOX + "fn f(b: Box[Int]) -> Int { let { v } = b; return v }\n")
    compile_source("type Pair[A, B] = { a: A, b: B }\n"
                   "fn g(p: Pair[Int, Str]) -> Str { let { b, a } = p; return b }\n")


# The issue's second half: a record with NO fields drew the hint `fields: ` with
# nothing after it, under both the destructuring and the record update refusal.
# It now says that the record has no fields; a non-empty list reads as before.

def test_destructuring_an_empty_record_says_it_has_no_fields():
    src = "fn f() -> Int { let r = {}; let { zz } = r; return 0 }\n"
    with pytest.raises(RevlError) as excinfo:
        compile_source(src)
    msg = str(excinfo.value)
    assert "record destructuring names `zz`, which is not a field of `{}`" in msg
    assert "`{}` has no fields" in msg
    assert "fields: " not in msg


@pytest.mark.parametrize("base", ["r", "{}"])
def test_updating_an_empty_record_says_it_has_no_fields(base):
    src = f"fn f() -> Int {{ let r = {{}}; let c = {{ {base} | z = 1 }}; return 0 }}\n"
    with pytest.raises(RevlError) as excinfo:
        compile_source(src)
    msg = str(excinfo.value)
    assert "record update names `z`, which is not a field of `{}`" in msg
    assert "`{}` has no fields" in msg
    assert "fields: " not in msg


def test_non_empty_field_list_hint_is_unchanged():
    src = "fn f() -> Int { let r = { x: 1 }; let c = { r | z = 1 }; return 0 }\n"
    with pytest.raises(RevlError) as excinfo:
        compile_source(src)
    assert "fields: `x`" in str(excinfo.value)
