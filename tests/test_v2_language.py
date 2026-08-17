"""v2.0 parser: type & fn declarations, pure expressions (syntax-2.0 §2–§3)."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.parser import (  # noqa: E402
    EffectStmt,
    ExprBin,
    ExprCall,
    ExprMatch,
    ExprVar,
    FailStmt,
    IfStmt,
    LetEffect,
    LetStmt,
    Parser,
)


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


def test_match_expression_parses_arms_and_binds():
    prog = _parse(
        """
        fn describe(outcome: Outcome) -> Str {
          return match outcome {
            Ok(row) => row.name,
            NotFound => "-",
            Invalid(why) => why,
          }
        }
        """
    )
    (fn,) = prog.fn_decls
    match_expr = fn.body[0].expr
    assert isinstance(match_expr, ExprMatch)
    assert isinstance(match_expr.scrutinee, ExprVar)
    assert match_expr.scrutinee.name == "outcome"
    assert [(p, b) for p, b, _ in match_expr.arms] == [
        ("Ok", "row"),
        ("NotFound", None),
        ("Invalid", "why"),
    ]
    assert match_expr.arms[0][2].__class__.__name__ == "ExprField"


def test_match_expression_accepts_wildcard_and_trailing_comma():
    prog = _parse(
        'fn f(o: Outcome) -> Str { return match o { Ok(row) => row.name, _ => "other", } }'
    )
    (fn,) = prog.fn_decls
    match_expr = fn.body[0].expr
    assert isinstance(match_expr, ExprMatch)
    assert [(p, b) for p, b, _ in match_expr.arms] == [
        ("Ok", "row"),
        ("_", None),
    ]
def test_extern_parse_with_multiple_backends():
    prog = _parse(
        """
        extern pure fn sha256(data: Bytes) -> Str
          = @ts { return crypto.createHash("sha256").update(data).digest("hex") }
          = @py { import hashlib; return hashlib.sha256(data).hexdigest() }
        """
    )
    (ext,) = prog.externs
    assert ext.classification == "pure"
    assert ext.name == "sha256"
    assert [(p.name, p.type) for p in ext.params] == [("data", "Bytes")]
    assert ext.returns == "Str"
    assert [b.backend for b in ext.bodies] == ["ts", "py"]
    assert "crypto.createHash" in ext.bodies[0].text


def test_extern_acquire_and_emission_parse():
    prog = _parse(
        """
        extern acquire fn listen(port: Int) -> Socket undo close(socket)
          = @py { return socket.socket() }
        extern emission fn send(sock: Socket, data: Bytes) compensate log_unsent(sock, data)
          = @ts { return sock.write(data) }
        """
    )
    acquire, emission = prog.externs
    assert acquire.classification == "acquire"
    assert acquire.undo.__class__.__name__ == "ExprCall"
    assert emission.classification == "emission"
    assert emission.compensate.__class__.__name__ == "ExprCall"


def test_unclassified_extern_does_not_parse():
    import pytest

    from revl.errors import RevlError

    with pytest.raises(RevlError, match="unclassified extern"):
        _parse("extern fn f() = @py { pass }")
def test_verified_fn_decl():
    prog = _parse("verified fn parse_int(s: Str) -> Opt[Int] { return Some(1) }")
    (fn,) = prog.fn_decls
    assert fn.verified
    assert not fn.public


def test_pub_verified_fn_decl():
    prog = _parse("pub verified fn add(a: Int, b: Int) -> Int { return a + b }")
    (fn,) = prog.fn_decls
    assert fn.public
    assert fn.verified


def test_test_block_decl():
    prog = _parse(
        'test "put then get roundtrips" { '
        'let m = Map.new() '
        'assert m.insert("k", "v").get("k") == Some("v") '
        '}'
    )
    (test,) = prog.tests
    assert test.name == "put then get roundtrips"
    assert len(test.body) == 2
    assert test.body[0].__class__.__name__ == "LetStmt"
    assert test.body[1].__class__.__name__ == "AssertStmt"


def test_component_block_effect_parses_setup_and_final_acquisition():
    prog = _parse(
        """
        component Conn {
          let conn = effect {
            let url = normalize(config.url)
            Pool.open(url, config.pool_size)
          } undo conn.close()
        }
        """
    )
    (comp,) = prog.components
    stmt = comp.body[0]
    assert isinstance(stmt, LetEffect)
    assert stmt.bind == "conn"
    assert len(stmt.setup) == 1
    assert isinstance(stmt.setup[0], LetStmt)
    assert stmt.setup[0].name == "url"
    assert isinstance(stmt.acquire, ExprCall)
    assert stmt.undo.__class__.__name__ == "Postfix"
    assert stmt.undo.head == "conn"


def test_bare_component_effect_block_parses():
    prog = _parse(
        """
        component Conn {
          effect {
            let url = normalize(config.url)
            url
          } undo Pool.close(url)
        }
        """
    )
    (comp,) = prog.components
    stmt = comp.body[0]
    assert isinstance(stmt, EffectStmt)
    assert len(stmt.setup) == 1
    assert isinstance(stmt.setup[0], LetStmt)
    assert isinstance(stmt.acquire, ExprVar)
    assert stmt.acquire.name == "url"


def test_component_if_guard_with_fail_parses():
    prog = _parse(
        """
        component ReplicaSet {
          config { replicas: Int = 0 }
          if (config.replicas < 1) fail "at least one replica required"
        }
        """
    )
    (comp,) = prog.components
    stmt = comp.body[0]
    assert isinstance(stmt, IfStmt)
    assert isinstance(stmt.then[0], FailStmt)
    assert stmt.then[0].message.__class__.__name__ == "ExprLit"


def test_fail_rejected_in_pure_fn():
    import pytest

    from revl.errors import RevlError

    with pytest.raises(RevlError, match=r"`fail` is only allowed in a component activation body \(A8\)"):
        _parse('fn f() -> Int { fail "nope" return 0 }')
