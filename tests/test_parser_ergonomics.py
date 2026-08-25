"""Parser ergonomics broadenings surfaced by self-host dogfooding (item 145).

Two *acceptance broadenings* — previously-rejected shapes become accepted, and
nothing that already parsed changes shape:

* item 157 — `;` is an optional statement separator/terminator. Statements may
  be separated by a newline (as always) OR by `;`; a leading, trailing, or
  repeated `;` (a lone `;`/`;;` empty statement) is a harmless no-op. A program
  written with no `;` parses to the exact same AST as before.
* item 158 — the cordis-domain nouns `realm`, `intercept`, `isolate`, `in`,
  `with` are CONTEXTUAL keywords: still reserved where the grammar wants the
  keyword (their statement/clause heads), but ordinary identifiers in field-name
  and parameter-name position, in `.field` access, and — the corollary that makes
  a so-named parameter usable — as a bare variable reference.
"""

import dataclasses
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import RevlError, compile_files  # noqa: E402
from revl.parser import Parser  # noqa: E402

NOUNS = ("realm", "intercept", "isolate", "in", "with")


def parse(src: str):
    return Parser(src, "<test>").parse()


def _strip_lines(obj):
    """Structural view of an AST with every `line` field removed, so two
    programs that differ only in physical line layout compare equal."""
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return (
            type(obj).__name__,
            {
                f.name: _strip_lines(getattr(obj, f.name))
                for f in dataclasses.fields(obj)
                if f.name != "line"
            },
        )
    if isinstance(obj, dict):
        return {k: _strip_lines(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_strip_lines(x) for x in obj]
    return obj


# --------------------------------------------------------------------------
# item 157 — `;` as an optional statement separator/terminator
# --------------------------------------------------------------------------

def test_semicolon_separator_matches_newline_ast():
    """`a; b` parses to the same AST (modulo source line) as `a` and `b` on
    separate lines — the whole point: `;` adds no node, it only separates."""
    semi = parse("fn bump(n: Int) -> Int {\n  let m = n + 1; return m\n}")
    newline = parse("fn bump(n: Int) -> Int {\n  let m = n + 1\n  return m\n}")
    assert _strip_lines(semi) == _strip_lines(newline)
    # and it really did produce two statements, not one mangled expression
    assert len(semi.fn_decls[0].body) == 2


def test_trailing_semicolon_is_a_noop():
    """A trailing `;` on the last statement is harmless — identical AST,
    including line numbers, to the same program without it."""
    without = parse("fn f() -> Int {\n  let m = 1\n  return m\n}")
    trailing = parse("fn f() -> Int {\n  let m = 1;\n  return m;\n}")
    assert without == trailing


def test_empty_statements_are_skipped():
    """A lone `;`, `;;`, and leading/interior `;` are empty statements: no-ops
    that add nothing to the body."""
    prog = parse("fn f() -> Int {\n  ;;\n  let m = 1;;\n  ;\n  return m\n}")
    body = prog.fn_decls[0].body
    assert [type(s).__name__ for s in body] == ["LetStmt", "ReturnStmt"]


def test_compact_block_with_semicolons_parses():
    """The motivating shape from the roadmap: a compact block on one line."""
    prog = parse(
        "fn f(x: Bool) -> Int {\n"
        "  if (x) { let a = 1; let b = a + 1; return b }\n"
        "  return 0\n"
        "}"
    )
    then = prog.fn_decls[0].body[0].then
    assert [type(s).__name__ for s in then] == ["LetStmt", "LetStmt", "ReturnStmt"]


def test_semicolons_across_body_kinds():
    """`;` separates statements uniformly: component body, provide method body,
    test body, and an effect setup block all accept it."""
    src = (
        'service S { fn get(k: Str) -> Str }\n'
        'component C requires s: S {\n'
        '  let cache = effect { let seed = 0; acquire_cache(seed) } undo drop(cache);\n'
        '  provide log { fn get(k) { let v = k; return v } }\n'
        '}\n'
        'test "t" { let a = 1; let b = a; assert b == 1 }\n'
    )
    prog = parse(src)
    assert prog.components and prog.tests


def test_newline_program_is_unaffected_guard():
    """Guard: a representative newline-only program (no `;` anywhere) parses to
    exactly the AST it always did. Reparsing is stable and byte-for-byte the
    same nodes — the broadening must not perturb the newline grammar."""
    src = (
        'pub fn classify(n: Int) -> Str {\n'
        '  let doubled = n + n\n'
        '  let label = "even"\n'
        '  if (doubled == n) {\n'
        '    return "zero"\n'
        '  }\n'
        '  return label\n'
        '}\n'
    )
    once = parse(src)
    twice = parse(src)
    assert once == twice
    body = once.fn_decls[0].body
    assert [type(s).__name__ for s in body] == ["LetStmt", "LetStmt", "IfStmt", "ReturnStmt"]


def test_semicolon_program_compiles_end_to_end(tmp_path):
    """A `;`-separated program lowers all the way to IR (not just parses)."""
    p = tmp_path / "semi.rvl"
    p.write_text(
        "pub fn bump(n: Int) -> Int {\n"
        "  let m = n + 1; let k = m + 1; return k\n"
        "}\n"
    )
    ir = compile_files([str(p)])
    assert ir["ir_version"]


# --------------------------------------------------------------------------
# item 158 — cordis-domain nouns as contextual keywords in name position
# --------------------------------------------------------------------------

@pytest.mark.parametrize("noun", NOUNS)
def test_noun_as_record_field_name(noun):
    prog = parse(f"type T = {{ {noun}: Str }}")
    assert [f.name for f in prog.type_decls[0].fields] == [noun]


@pytest.mark.parametrize("noun", NOUNS)
def test_noun_as_parameter_name_and_reference(noun):
    """A noun names a parameter AND is referenceable in the body — a parameter
    you cannot read would be a useless relaxation."""
    prog = parse(f"pub fn f({noun}: Str) -> Str {{ return {noun} }}")
    fn = prog.fn_decls[0]
    assert [p.name for p in fn.params] == [noun]
    ret = fn.body[0]
    assert type(ret).__name__ == "ReturnStmt"
    assert type(ret.expr).__name__ == "ExprVar"
    assert ret.expr.name == noun


@pytest.mark.parametrize("noun", NOUNS)
def test_noun_as_field_access(noun):
    prog = parse(f"pub fn g(r: Rec) -> Str {{ return r.{noun} }}")
    ret = prog.fn_decls[0].body[0]
    assert type(ret.expr).__name__ == "ExprField"
    assert ret.expr.name == noun


def test_all_nouns_together_in_one_record():
    src = "type T = { realm: Str, intercept: Int, isolate: Bool, in: Int, with: Str }"
    prog = parse(src)
    assert [f.name for f in prog.type_decls[0].fields] == list(NOUNS)


def test_noun_record_field_compiles_end_to_end(tmp_path):
    """Field named with a noun lowers to IR: the broadening is real, not just
    a parser trick the checker rejects a step later."""
    p = tmp_path / "rec.rvl"
    p.write_text(
        "type Cfg = { realm: Str, intercept: Int }\n"
        "pub fn pick(with: Str) -> Str {\n"
        "  let c = { realm: with, intercept: 0 }\n"
        "  return c.realm\n"
        "}\n"
    )
    ir = compile_files([str(p)])
    assert ir["ir_version"]


# -- the keyword half of "contextual": these still parse as keywords ---------

def test_isolate_in_still_parses_as_keyword_statement():
    prog = parse('component C requires kv: Store {\n  isolate kv in realm("tenant_a")\n}')
    stmt = prog.components[0].body[0]
    assert type(stmt).__name__ == "IsolateStmt"


def test_intercept_with_still_parses_as_keyword_statement():
    prog = parse(
        'component C requires db: Store {\n'
        '  intercept db with { realm: "x" }\n'
        '}'
    )
    stmt = prog.components[0].body[0]
    assert type(stmt).__name__ == "InterceptStmt"
    # and the field name *inside* the `with { ... }` clause is itself a relaxed
    # noun — `realm` reads as an ordinary metadata field name here.
    assert "realm" in stmt.metadata


def test_reserved_keyword_still_rejected_as_field_name():
    """Guard against over-relaxing: a genuinely reserved word (`type`, not one
    of the five nouns) still cannot name a field, and still lands on the
    naming-mistake hint."""
    with pytest.raises(RevlError) as exc:
        parse("type Row = { type: Str }")
    assert "reserved keyword" in (exc.value.hint or "")


@pytest.mark.parametrize("kw", ["fn", "let", "return", "service", "component"])
def test_other_keywords_still_rejected_as_param_name(kw):
    with pytest.raises(RevlError):
        parse(f"pub fn f({kw}: Str) -> Str {{ return \"x\" }}")
