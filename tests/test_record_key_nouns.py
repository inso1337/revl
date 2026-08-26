"""item 237 — cordis-domain nouns as record-literal KEYS (and record field
names).

The cousin of item 158. Item 158 relaxed the five nouns
`realm`/`intercept`/`isolate`/`in`/`with` as CONTEXTUAL keywords in field-name,
parameter-name and `.field`-access position. But self-host dogfooding (item
234) hit three MORE component-grammar heads — `component`, `config`,
`requires` — which are refused where the grammar can only want a record FIELD
name:

    let n = { component: "W" }        # expected ident, found 'component'
    type Cfg = { config: Int }        # expected ident, found 'config'
    n.component                       # expected ident, found 'component'

A record FIELD position — a record-literal key (`{k: e}`), a functional
record-update field (`{base | k = e}`), a record-type field
(`type T = {k: τ}`), and the `.field`/`?.field` access that reads such a field
back — always has the name immediately followed by `:`/`=` (or preceded by
`.`/`?.`), so NONE of these nouns can head a clause there. Item 237 relaxes all
three additional nouns in exactly those positions, and *only* there: every
position where `component`/`config`/`requires` can genuinely LEAD a form
(a component declaration, a `config { … }` block, a component `requires`
clause, or a parameter name) still reads them as reserved keywords.

Two acceptance broadenings, same discipline as 158: previously-rejected shapes
become accepted, and nothing that already parsed changes shape.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import RevlError, compile_files  # noqa: E402
from revl.parser import Parser  # noqa: E402

# The five item-158 nouns PLUS the three item-234 dogfood nouns.
OLD_NOUNS = ("realm", "intercept", "isolate", "in", "with")
NEW_NOUNS = ("component", "config", "requires")
ALL_NOUNS = OLD_NOUNS + NEW_NOUNS


def parse(src: str):
    return Parser(src, "<test>").parse()


def _let_value(prog):
    """The RHS expression of the first `let` in the first fn body."""
    return prog.fn_decls[0].body[0].value


# -- record-literal KEY position (the item-234 shape) ------------------------

@pytest.mark.parametrize("noun", NEW_NOUNS)
def test_new_noun_as_record_literal_key(noun):
    """`{ component: … }` and friends now parse; before item 237 each raised
    `expected ident, found '<noun>'`."""
    prog = parse(f"pub fn g() -> Int {{ let c = {{ {noun}: 1 }}\n  return 0 }}")
    rec = _let_value(prog)
    assert type(rec).__name__ == "ExprRecord"
    assert [f[0] for f in rec.fields] == [noun]


@pytest.mark.parametrize("noun", ALL_NOUNS)
def test_noun_key_value_round_trips(noun):
    """The value under a noun key is the ordinary expression written, unchanged
    — the key relaxation does not disturb the value parse."""
    prog = parse(f'pub fn g() -> Int {{ let c = {{ {noun}: 42 }}\n  return 0 }}')
    rec = _let_value(prog)
    (name, value), = rec.fields
    assert name == noun
    assert type(value).__name__ == "ExprLit"
    assert value.value == 42


def test_all_new_nouns_and_old_nouns_in_one_record():
    """Every noun — the item-158 five and the item-237 three — coexists as a
    key in a single record literal, in source order, each with its own value."""
    src = (
        'pub fn g() -> Int {\n'
        '  let c = { realm: 1, intercept: 2, isolate: 3, in: 4, with: 5,'
        ' component: 6, config: 7, requires: 8 }\n'
        '  return 0\n'
        '}'
    )
    rec = _let_value(parse(src))
    assert [f[0] for f in rec.fields] == list(ALL_NOUNS)
    assert [f[1].value for f in rec.fields] == [1, 2, 3, 4, 5, 6, 7, 8]


# -- record-TYPE field position ----------------------------------------------

@pytest.mark.parametrize("noun", NEW_NOUNS)
def test_new_noun_as_type_record_field(noun):
    prog = parse(f"type T = {{ {noun}: Int }}")
    assert [f.name for f in prog.type_decls[0].fields] == [noun]


def test_all_nouns_as_type_record_fields():
    src = (
        "type Cfg = { realm: Str, intercept: Int, isolate: Bool, in: Int,"
        " with: Str, component: Str, config: Int, requires: Str }"
    )
    prog = parse(src)
    assert [f.name for f in prog.type_decls[0].fields] == list(ALL_NOUNS)


# -- record-UPDATE field position --------------------------------------------

@pytest.mark.parametrize("noun", NEW_NOUNS)
def test_new_noun_as_record_update_field(noun):
    prog = parse(
        f"pub fn g(r: T) -> Int {{ let c = {{ r | {noun} = 9 }}\n  return 0 }}"
    )
    upd = _let_value(prog)
    assert type(upd).__name__ == "ExprRecordUpdate"
    assert [f[0] for f in upd.updates] == [noun]


# -- `.field` / `?.field` access (reading a so-named field back) -------------

@pytest.mark.parametrize("noun", NEW_NOUNS)
def test_new_noun_as_field_access(noun):
    prog = parse(f"pub fn g(r: T) -> Int {{ return r.{noun} }}")
    ret = prog.fn_decls[0].body[0]
    assert type(ret.expr).__name__ == "ExprField"
    assert ret.expr.name == noun


@pytest.mark.parametrize("noun", NEW_NOUNS)
def test_new_noun_as_optional_field_access(noun):
    prog = parse(f"pub fn g(r: T) -> Int {{ return r?.{noun} }}")
    ret = prog.fn_decls[0].body[0]
    assert type(ret.expr).__name__ == "ExprOptField"
    assert ret.expr.name == noun


def test_new_noun_record_field_compiles_end_to_end(tmp_path):
    """A field named with a new noun is not just a parser trick: it survives
    lowering to IR (mirrors item 158's end-to-end guard)."""
    p = tmp_path / "rec.rvl"
    p.write_text(
        "type Cfg = { component: Str, config: Int }\n"
        "pub fn pick(name: Str) -> Str {\n"
        "  let c = { component: name, config: 0 }\n"
        "  return c.component\n"
        "}\n"
    )
    ir = compile_files([str(p)])
    assert ir["ir_version"]


# -- the keyword half of "contextual": these still LEAD forms as keywords -----

def test_component_still_heads_a_declaration():
    prog = parse("component C {\n}")
    assert prog.components and prog.components[0].name == "C"


def test_config_still_heads_a_config_block():
    prog = parse("component C {\n  config { url: Str }\n}")
    assert prog.components[0].config
    assert prog.components[0].config[0].name == "url"


def test_requires_still_heads_a_component_clause():
    prog = parse(
        'service S { fn get(k: Str) -> Str }\n'
        'component C requires s: S {\n}'
    )
    assert prog.components[0].requires
    assert prog.components[0].requires[0][:2] == ("s", "S")


def test_isolate_in_realm_still_parses_as_keyword_statement():
    """The item-158 keyword heads are untouched: relaxing the record-key
    position did not weaken the statement grammar."""
    prog = parse('component C requires kv: Store {\n  isolate kv in realm("a")\n}')
    assert type(prog.components[0].body[0]).__name__ == "IsolateStmt"


def test_intercept_with_still_parses_as_keyword_statement():
    prog = parse(
        'component C requires db: Store {\n'
        '  intercept db with { realm: "x", component: "w" }\n'
        '}'
    )
    stmt = prog.components[0].body[0]
    assert type(stmt).__name__ == "InterceptStmt"
    # and the metadata field names inside the `with { … }` clause are relaxed
    # nouns — including a new one, `component`, reading as a plain field name.
    assert "realm" in stmt.metadata and "component" in stmt.metadata


@pytest.mark.parametrize("noun", NEW_NOUNS)
def test_new_noun_still_rejected_as_param_name(noun):
    """The corollary that keeps `_name` honest: these three remain reserved in
    parameter position (where `component`/`config`/`requires` genuinely head a
    form), exactly as item 158 left them."""
    with pytest.raises(RevlError):
        parse(f"pub fn g({noun}: Int) -> Int {{ return 0 }}")


def test_reserved_keyword_still_rejected_as_record_key():
    """Guard against over-relaxing: a genuinely reserved word that is NOT one of
    the eight nouns (`type`) still cannot name a field, and still lands on the
    reserved-keyword hint."""
    with pytest.raises(RevlError) as exc:
        parse("type Row = { type: Str }")
    assert "reserved keyword" in (exc.value.hint or "")


# -- additivity: an ordinary record is byte-identical before/after ------------

def test_ordinary_record_literal_unchanged():
    """A record with plain identifier keys parses to the exact same AST it
    always did — item 237 is purely additive."""
    a = parse('pub fn g() -> Int { let c = { id: 1, name: 2 }\n  return 0 }')
    b = parse('pub fn g() -> Int { let c = { id: 1, name: 2 }\n  return 0 }')
    assert a == b
    rec = _let_value(a)
    assert [f[0] for f in rec.fields] == ["id", "name"]
