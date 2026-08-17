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


def test_service_async_fn_parses():
    prog = _parse(
        "pub service Database {\n"
        "  fn query(sql: Str) -> List[Row]\n"
        "  async fn stats() -> Stats\n"
        "  emission fn execute(sql: Str) -> Int\n"
        "}"
    )
    (svc,) = prog.services
    assert svc.name == "Database"
    assert svc.commutative is False
    assert svc.methods["query"].async_ is False
    assert svc.methods["stats"].async_ is True
    assert svc.methods["execute"].emission is True


def test_service_commutative_parses_at_service_and_operation_level():
    prog = _parse(
        "commutative service Database {\n"
        "  commutative fn query(sql: Str) -> List[Row]\n"
        "  async fn stats() -> Stats\n"
        "}"
    )
    (svc,) = prog.services
    assert svc.commutative is True
    assert svc.methods["query"].commutative is True
    assert svc.methods["query"].async_ is False
    assert svc.methods["stats"].commutative is False
    assert svc.methods["stats"].async_ is True


def test_pub_commutative_service_parses():
    prog = _parse("pub commutative service Cache { fn get(key: Str) -> Opt[Str] }")
    (svc,) = prog.services
    assert svc.commutative is True


def test_async_provide_method_parses():
    prog = _parse(
        "service Cache { async fn get(key: Str) -> Opt[Str] }\n"
        "component C provides cache: Cache {\n"
        "  provide cache {\n"
        "    async fn get(key) {\n"
        "      await Job.run(\"lookup\")\n"
        "      return null\n"
        "    }\n"
        "  }\n"
        "}"
    )
    (_, component) = prog.services, prog.components
    (provide,) = component[0].body
    assert provide.methods[0].async_ is True
    assert provide.methods[0].body[0].__class__.__name__ == "AwaitStmt"


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


def test_while_for_and_compound_assignment_parse():
    prog = _parse(
        """
        fn count_idents(tokens: List[Token]) -> Int {
          var n = 0
          for (tok of tokens) {
            if (tok.kind == Ident) n += 1
          }
          while (n < 3) n += 1
          return n
        }
        """
    )
    (fn,) = prog.fn_decls
    assert [s.__class__.__name__ for s in fn.body] == ["LetStmt", "ForStmt", "WhileStmt", "ReturnStmt"]
    assert fn.body[0].mutable is True
    for_stmt = fn.body[1]
    assert for_stmt.bind == "tok"
    assert isinstance(for_stmt.iterable, ExprVar)
    if_stmt = for_stmt.body[0]
    assert if_stmt.cond.left.__class__.__name__ == "ExprField"
    assert if_stmt.cond.left.name == "kind"
    assert if_stmt.then[0].op == "+="
    while_stmt = fn.body[2]
    assert while_stmt.body[0].op == "+="


def test_unbraced_while_and_for_bodies_parse():
    prog = _parse(
        """
        fn skip(source: Str, start: Int) -> Int {
          var i = start
          while (i < source.length) i += 1
          for (ch of source) i += 1
          return i
        }
        """
    )
    (fn,) = prog.fn_decls
    while_stmt, for_stmt = fn.body[1:3]
    assert while_stmt.__class__.__name__ == "WhileStmt"
    assert len(while_stmt.body) == 1
    assert for_stmt.__class__.__name__ == "ForStmt"
    assert len(for_stmt.body) == 1


def test_record_and_list_destructuring_parse():
    prog = _parse(
        """
        fn f(row: Row, xs: List[Int]) -> Int {
          let {id, name} = row
          var [head, ...rest] = xs
          return head
        }
        """
    )
    (fn,) = prog.fn_decls
    let_pattern, var_pattern = fn.body[:2]
    assert let_pattern.__class__.__name__ == "LetPatternStmt"
    assert let_pattern.mutable is False
    assert let_pattern.pattern.fields == ["id", "name"]
    assert var_pattern.__class__.__name__ == "LetPatternStmt"
    assert var_pattern.mutable is True
    assert var_pattern.pattern.binds == ["head"]
    assert var_pattern.pattern.rest == "rest"
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
    # since the strata unification, undo parses in the same full
    # expression grammar as every component position
    assert stmt.undo.__class__.__name__ == "ExprCall"
    assert stmt.undo.callee.target.name == "conn"
    assert stmt.undo.callee.name == "close"


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


# ---------------------------------------------------------------------------
# syntax-2.0 §3.2: `??` nullish coalescing, `?.` optional chaining, dotted
# template interpolation `${a.b}` — all three parse and compile end to end
# after the 2.0-review fix batch.

def test_nullish_coalescing_parses_and_lowers():
    from revl.compiler import compile_source

    ir = compile_source("fn pick(x: Opt[Int]) -> Int { return x ?? 0 }")
    fn = ir["functions"][0]
    ret = fn["body"][0]["expr"]
    assert ret == {
        "kind": "bin", "op": "??",
        "left": {"kind": "var", "name": "x"},
        "right": {"kind": "lit", "value": 0},
    }


def test_optional_chaining_field_and_call_parse():
    from revl.compiler import compile_source

    ir = compile_source(
        "type Row = { id: Int, name: Str }\n"
        "fn nm(r: Opt[Row]) -> Opt[Str] { return r?.name }\n"
    )
    ret = ir["functions"][0]["body"][0]["expr"]
    assert ret["kind"] == "optfield" and ret["name"] == "name"


def test_template_dotted_chain_lexes_and_lowers():
    from revl.compiler import compile_source

    ir = compile_source(
        "type U = { name: Str }\n"
        "fn hi(u: U) -> Str { return `hi ${u.name}` }\n"
    )
    ret = ir["functions"][0]["body"][0]["expr"]
    assert ret == {"kind": "interp", "parts": [("text", "hi "), ("var", "u.name")]}


def test_python_backend_runs_nullish_and_template():
    """The two features aren't only parsed — the reference backend produces
    executable code."""
    import types

    backend_dir = ROOT / "backends" / "python"
    sys.path.insert(0, str(backend_dir))
    try:
        from revl.compiler import compile_source
        import emit  # backend module

        src = (
            "type U = { name: Str }\n"
            "fn pick(x: Opt[Int]) -> Int { return x ?? 42 }\n"
            "fn greet(u: U) -> Str { return `hi ${u.name}` }\n"
        )
        module = types.ModuleType("t")
        exec(compile(emit.emit(compile_source(src)), "<t>", "exec"), module.__dict__)
        assert module.pick(None) == 42
        assert module.pick(7) == 7  # Some(7) == 7 under T | None
        assert module.greet({"name": "world"}) == "hi world"
    finally:
        sys.path.remove(str(backend_dir))
