"""v2.0 parser: type & fn declarations, pure expressions (syntax-2.0 §2–§3)."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.parser import ExprBin, Parser  # noqa: E402


def _parse(source):
    return Parser(source, "<test>").parse()


def test_record_type_decl():
    prog = _parse("type Row = { id: Int, name: Str }")
    (decl,) = prog.type_decls
    assert decl.name == "Row"
    assert [f.name for f in decl.fields] == ["id", "name"]
    assert [f.type for f in decl.fields] == ["Int", "Str"]


def test_generic_type_decl():
    prog = _parse("type Pair[A, B] = { first: A, second: B }")
    (decl,) = prog.type_decls
    assert decl.params == ["A", "B"]
    assert [f.type for f in decl.fields] == ["A", "B"]


def test_adt_type_decl():
    prog = _parse("type Outcome = Ok(Row) | NotFound | Invalid(Str)")
    (decl,) = prog.type_decls
    assert [(c.name, c.payload) for c in decl.cases] == [
        ("Ok", "Row"), ("NotFound", None), ("Invalid", "Str"),
    ]


def test_enum_type_decl():
    prog = _parse("type TokenKind = Ident | Keyword | IntLit")
    (decl,) = prog.type_decls
    assert all(c.payload is None for c in decl.cases)


def test_fn_decl_with_body():
    prog = _parse("fn add(a: Int, b: Int) -> Int { return a + b }")
    (fn,) = prog.fn_decls
    assert fn.name == "add"
    assert [(p.name, p.type) for p in fn.params] == [("a", "Int"), ("b", "Int")]
    assert fn.returns == "Int"
    assert not fn.public


def test_pub_fn_decl_with_unbraced_if():
    prog = _parse('pub fn classify(n: Int) -> Str { if (n < 0) return "neg" return "pos" }')
    (fn,) = prog.fn_decls
    assert fn.public
    assert fn.returns == "Str"
    assert fn.body[0].__class__.__name__ == "IfStmt"
    assert len(fn.body) == 2


def test_optional_type_sugar():
    prog = _parse("fn get(k: Str) -> Str? { return null }")
    (fn,) = prog.fn_decls
    assert fn.returns == "Opt[Str]"


def test_expression_precedence():
    prog = _parse("fn f() -> Int { return 1 + 2 * 3 == 7 }")
    (fn,) = prog.fn_decls
    eq = fn.body[0].expr
    assert isinstance(eq, ExprBin) and eq.op == "=="
    assert eq.left.op == "+"
    assert eq.left.right.op == "*"
    assert eq.right.value == 7


def test_record_and_list_literals():
    prog = _parse("fn f() -> Int { let r = { a: 1 } let xs = [1, 2, 3] return xs[0] }")
    (fn,) = prog.fn_decls
    assert len(fn.body) == 3


def test_arrow_literal():
    prog = _parse("fn f() -> Int { let g = x => x + 1 return g(2) }")
    (fn,) = prog.fn_decls
    assert fn.body[0].value.__class__.__name__ == "ExprArrow"
