"""A provided service method named after a Go reserved word, on the go tier
(issue #1512, the go half of what #1474 fixed on py).

MEASURED, NOT ASSUMED. One program per name over Go's 25 keywords and its
predeclared identifiers (69 names), each provided by one component, called
through a required service by another and called directly by a lifecycle
`call`, run end to end with `revl test --backend go`: the frontend refuses 10
(they are revl keywords too), and all 59 it admits pass on main. Across a
placement seam `func`, `map`, `chan`, `len` and `interface` also crossed
correctly with the go process as provider (py consumer).

There is no split here because the go tier never spells a service method in
lower case: `_camel` exports it (`func` -> `Func`) at the interface, the
provider, a required-service call and a lifecycle `call` alike, and an exported
name cannot be a Go keyword or shadow a predeclared identifier. The bridge's
dispatch is generated from the service table's contract names.

This file keeps it that way: every admitted name in ONE program, so one `go
test` build covers them all. The end-to-end test needs the go toolchain and
skips without it; CI's `backend-go` job provides it.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from revl.compiler import compile_source  # noqa: E402
from revl.errors import RevlError  # noqa: E402
from revl.test import RUNNERS  # noqa: E402

GO_KEYWORDS = {
    "break", "case", "chan", "const", "continue", "default", "defer", "else",
    "fallthrough", "for", "func", "go", "goto", "if", "import", "interface",
    "map", "package", "range", "return", "select", "struct", "switch", "type",
    "var",
}
GO_PREDECLARED = {
    "bool", "byte", "complex64", "complex128", "error", "float32", "float64",
    "int", "int8", "int16", "int32", "int64", "rune", "string", "uint", "uint8",
    "uint16", "uint32", "uint64", "uintptr", "true", "false", "iota", "nil",
    "append", "cap", "clear", "close", "complex", "copy", "delete", "imag",
    "len", "make", "max", "min", "new", "panic", "print", "println", "real",
    "recover", "any", "comparable",
}
GO_WORDS = sorted(GO_KEYWORDS | GO_PREDECLARED)

#: The names the revl frontend itself refuses: they never reach an emitter.
FRONTEND_REFUSED = {
    "break", "continue", "else", "false", "for", "if", "return", "true", "type",
    "var",
}

ADMITTED = [name for name in GO_WORDS if name not in FRONTEND_REFUSED]


def _provider(names) -> str:
    sigs = "\n".join(f"  fn {name}(x: Int) -> Int" for name in names)
    bodies = "\n".join(f"    fn {name}(x) {{ return x + {i} }}"
                       for i, name in enumerate(names, 1))
    return (f"service S {{\n{sigs}\n}}\n"
            f"component P provides s: S {{\n  provide s {{\n{bodies}\n  }}\n}}\n")


def _program(names) -> str:
    total = " + ".join(f"s.{name}(x)" for name in names)
    calls = "\n".join(f"  let r{i} = call s.{name}(41)\n  assert r{i} == {41 + i}"
                      for i, name in enumerate(names, 1))
    expected = sum(41 + i for i in range(1, len(names) + 1))
    return (_provider(names)
            + "service Ops { fn run(x: Int) -> Int }\n"
            + "component C requires s: S provides ops: Ops {\n"
            + f"  provide ops {{ fn run(x) {{ return {total} }} }}\n}}\n"
            + 'lifecycle test "reserved method names" {\n  load P\n  load C\n'
            + calls + "\n"
            + f"  let all = call ops.run(41)\n  assert all == {expected}\n"
            + "  unload C\n  unload P\n  assert no_residue\n}\n")


def test_the_corpus_is_every_keyword_and_predeclared_identifier():
    assert len(GO_KEYWORDS) == 25
    assert len(GO_WORDS) == 69 and len(ADMITTED) == 59


@pytest.mark.parametrize("name", sorted(FRONTEND_REFUSED))
def test_a_revl_keyword_is_refused_by_the_frontend_not_the_emitter(name):
    with pytest.raises(RevlError):
        compile_source(_provider([name]), "kw.rvl")


def test_every_admitted_name_is_provided_and_called_end_to_end():
    status, message = RUNNERS["go"](compile_source(_program(ADMITTED), "kw.rvl"))
    if status == "skip":
        pytest.skip(f"go: {message}")
    assert status == "pass", message
