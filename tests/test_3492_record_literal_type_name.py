"""Issue #3492: a record literal written `T { ... }` with `T` a declared type
was refused as "`P` is not declared in this function" with advice to declare a
variable named `P`. Now the refusal says `P` is a declared type and points at
the typed binding: `let p: P = { ... }`. A genuinely undeclared name keeps the
G1 message.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402
from revl.errors import RevlError  # noqa: E402


def _refusal(src: str) -> RevlError:
    with pytest.raises(RevlError) as excinfo:
        compile_source(src, "e.rvl")
    return excinfo.value


def test_a_record_literal_with_its_type_name_points_at_the_typed_binding():
    err = _refusal('type P = { name: Str, n: Int }\ntest "t" {\n'
                   '  let p = P { name: "a", n: 1 }\n  assert p.n == 1\n}\n')
    assert "`P` is a declared type, not a value" in err.message
    assert "let p: P = { ... }" in err.hint
    assert err.code == "G1"


def test_a_genuinely_undeclared_name_keeps_the_g1_message():
    err = _refusal('test "t" {\n  let x = missing + 1\n  assert x == 2\n}\n')
    assert err.message == "`missing` is not declared in this function"
    assert "declare it with `let`/`var`" in err.hint


def test_the_typed_binding_form_still_compiles():
    ir = compile_source('type P = { name: Str, n: Int }\ntest "t" {\n'
                        '  let p: P = { name: "a", n: 1 }\n  assert p.n == 1\n}\n',
                        "e.rvl")
    assert ir is not None
