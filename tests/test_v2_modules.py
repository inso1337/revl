"""v2.0 modules & visibility (syntax-2.0 §1)."""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import RevlError, compile_files  # noqa: E402
from revl.parser import Parser  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures"


def _parse(source):
    return Parser(source, "<test>").parse()


def test_use_and_pub_parse():
    prog = _parse(
        """
        use "./tokens.rvl" { Token, TokenKind }
        use "./util/strings.rvl" as strings
        pub type Row = { id: Int }
        pub fn lex(source: Str) -> Int { return 1 }
        fn helper() -> Int { return 2 }
        """
    )
    assert [u.path for u in prog.uses] == ["./tokens.rvl", "./util/strings.rvl"]
    assert prog.uses[0].names == ["Token", "TokenKind"]
    assert prog.uses[1].alias == "strings"
    assert prog.type_decls[0].public
    assert prog.fn_decls[0].public
    assert not prog.fn_decls[1].public


def test_named_use_resolves_public_declarations():
    ir = compile_files([str(FIXTURES / "v2_math_main.rvl")])
    assert ir["ir_version"] == 3
    assert [fn["name"] for fn in ir["functions"]] == ["twice", "add", "helper"]
    assert ir["types"]["Row"]["fields"]["id"] == "Int"
    assert ir["components"][0]["name"] == "UsesMath"


def test_private_helpers_are_emitted_but_not_callable_cross_module(tmp_path):
    (tmp_path / "lib.rvl").write_text(
        "pub fn add(a: Int, b: Int) -> Int { return a + b }\n"
        "fn helper() -> Int { return 1 }\n"
    )
    (tmp_path / "main.rvl").write_text(
        'use "./lib.rvl" { add }\n'
        "pub fn twice(x: Int) -> Int { return add(x, helper()) }\n"
    )
    with pytest.raises(RevlError, match="`helper` is not declared"):
        compile_files([str(tmp_path / "main.rvl")])


def test_alias_import_call(tmp_path):
    (tmp_path / "lib.rvl").write_text(
        "pub fn add(a: Int, b: Int) -> Int { return a + b }\n"
    )
    (tmp_path / "main.rvl").write_text(
        'use "./lib.rvl" as math\n'
        "pub fn twice(x: Int) -> Int { return math.add(x, x) }\n"
    )
    ir = compile_files([str(tmp_path / "main.rvl")])
    twice = next(fn for fn in ir["functions"] if fn["name"] == "twice")
    assert twice["body"][0]["expr"]["callee"]["name"] == "add"


def test_import_cycle_is_a_module_error(tmp_path):
    (tmp_path / "a.rvl").write_text(
        'use "./b.rvl" { b }\n'
        "pub fn a() -> Int { return b() }\n"
    )
    (tmp_path / "b.rvl").write_text(
        'use "./a.rvl" { a }\n'
        "pub fn b() -> Int { return a() }\n"
    )
    with pytest.raises(RevlError, match="import cycle:"):
        compile_files([str(tmp_path / "a.rvl")])
