"""`T { ... }` on a declared record type gets the record-building hint (#3492).

`let p = P { name: "a", n: 1 }` (the Rust/Go/Swift spelling) was refused with
"`P` is not declared in this function" plus advice to declare a variable named
`P` — the name IS declared, as a type, two lines up. The G1 refusal now
detects that the undeclared name is a declared record type and says a record
is built from a typed binding: `let p: P = { ... }`. Genuinely undeclared names
keep the plain G1 text.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402
from revl.errors import RevlError  # noqa: E402


def _refusal(source: str) -> RevlError:
    with pytest.raises(RevlError) as err:
        compile_source(source, "e.rvl")
    return err.value


def test_type_name_before_a_record_literal_gets_the_typed_binding_hint():
    e = _refusal('type P = { name: Str, n: Int }\n'
                 'test "t" { let p = P { name: "a", n: 1 } assert p.n == 1 }\n')
    assert e.code == "G1"
    assert "`P` is a type, not a value" in str(e)
    hint = e.hint or ""
    assert "let p: P = { ... }" in hint
    assert "not in front of the literal" in hint


def test_an_undeclared_name_keeps_the_plain_g1():
    e = _refusal('test "t" { let q = Nope { x: 1 } }\n')
    assert e.code == "G1"
    assert "`Nope` is not declared in this function" in str(e)
    assert "let`/`var`" in (e.hint or "")
    assert "type, not a value" not in str(e)


def test_the_working_form_still_compiles():
    compile_source('type P = { name: Str, n: Int }\n'
                   'test "t" { let p: P = { name: "a", n: 1 } assert p.n == 1 }\n',
                   "e.rvl")
