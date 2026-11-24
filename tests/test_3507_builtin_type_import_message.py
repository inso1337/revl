"""Issue #3507: asking for a builtin type (`Value`) in a `use` list was refused
as "not a public declaration" — reading as "this type does not exist". Now the
refusal says it is a builtin type and to drop it from the list. Unknown names
keep the old message.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402
from revl.errors import RevlError  # noqa: E402

STDLIB = {
    "stdlib/value.rvl": (ROOT / "stdlib" / "value.rvl").read_text(encoding="utf-8"),
    "stdlib/str.rvl": (ROOT / "stdlib" / "str.rvl").read_text(encoding="utf-8"),
    "stdlib/json.rvl": (ROOT / "stdlib" / "json.rvl").read_text(encoding="utf-8"),
}


def _refusal(src: str) -> RevlError:
    with pytest.raises(RevlError) as excinfo:
        compile_source(src, "t.rvl", modules=STDLIB)
    return excinfo.value


def test_importing_value_says_it_is_a_builtin_type():
    err = _refusal('use "stdlib/value.rvl" { Value, value_has }\n'
                   'test "t" { assert value_has(json_parse("{}"), "a") == false }\n')
    assert "`Value` is a builtin type: use it without importing it" in err.message
    assert "remove `Value` from this `use` list" in err.hint


def test_importing_any_builtin_type_name_says_the_same():
    err = _refusal('use "stdlib/str.rvl" { Str }\ntest "t" { assert true }\n')
    assert "`Str` is a builtin type" in err.message


def test_an_unknown_name_keeps_the_not_a_public_declaration_message():
    err = _refusal('use "stdlib/value.rvl" { NoSuchThing }\n'
                   'test "t" { assert true }\n')
    assert "`NoSuchThing` is not a public declaration in `stdlib/value.rvl`" \
        in err.message


def test_a_real_public_function_still_imports():
    ir = compile_source('use "stdlib/value.rvl" { value_has }\n'
                        'use "stdlib/json.rvl" { json_parse }\n'
                        'test "t" { assert value_has(json_parse("{}"), "a") == false }\n',
                        "t.rvl", modules=STDLIB)
    assert ir is not None
