"""Functional record update `{r | f = e}` (docs/records.md §1–3, §6) and
block-bodied match arms (§4): parse, typecheck, refusal, and execution on the
implemented tiers.

Checked here:
  * record update parses; a literal with `|` is never misread and an update
    whose base contains `|`-free braces nests correctly;
  * the checker enforces all three type rules of §3 and refuses violations;
  * the python tier executes exactly — fresh value, original untouched;
  * the typescript tier emits the documented spread shape;
  * rust/go/wasm/java refuse at emit time naming their tier;
  * block arms parse and typecheck but every compile ends in the §6 refusal.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import RevlError, compile_source  # noqa: E402

POINT = "type Point = { x: Int, y: Int }\n"
MOVE = "fn moved(p: Point) -> Point { return { p | x = p.x + 1 } }\n"


def _err(src: str) -> str:
    with pytest.raises(RevlError) as excinfo:
        compile_source(src)
    return str(excinfo.value)


# ---------------------------------------------------------------- parser

def test_parses_to_record_update():
    from revl.parser import ExprRecordUpdate, Parser
    prog = Parser(POINT + MOVE, "t").parse()
    ret = prog.fn_decls[0].body[0]
    node = ret.expr
    assert isinstance(node, ExprRecordUpdate)
    assert node.updates == [("x", node.updates[0][1])]


def test_record_literal_still_parses():
    ir = compile_source(POINT + "fn mk() -> Point { return { x: 1, y: 2 } }\n")
    fn = ir["functions"][0]["body"][0]["expr"]
    assert fn["kind"] == "record"


def test_update_value_may_be_nested_update():
    # nested `{...}` inside an update must not confuse the `|` lookahead
    ir = compile_source(
        POINT + "fn wrap(p: Point) -> Point { return { p | x = { p | y = 9 }.x } }\n")
    expr = ir["functions"][0]["body"][0]["expr"]
    assert expr["kind"] == "record_update"


# ---------------------------------------------------------------- checker

def test_result_type_is_base_type():
    # flows into a declared Point return without re-annotation
    compile_source(POINT + MOVE)


def test_base_must_be_record():
    msg = _err("fn bad(n: Int) -> Int { return { n | x = 1 } }")
    assert "record update requires a record type" in msg


def test_field_must_exist():
    msg = _err(POINT + "fn bad(p: Point) -> Point { return { p | z = 1 } }")
    assert "`z`, which is not a field of `Point`" in msg


def test_replacement_must_match_field_type():
    msg = _err(POINT + "fn bad(p: Point) -> Point { return { p | x = \"s\" } }")
    assert "update of field `x`" in msg or "field `x`" in msg


# ---------------------------------------------------------------- python exec

def _exec_python(src: str):
    ir = compile_source(src)
    spec = importlib.util.spec_from_file_location(
        "pyemit_record_update", ROOT / "backends" / "python" / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    namespace: dict = {}
    exec(compile(module.emit(ir), "record_update.py", "exec"), namespace)
    return namespace


def test_python_fresh_value_original_untouched():
    moved = _exec_python(POINT + MOVE)["moved"]
    original = {"x": 1, "y": 2}
    result = moved(original)
    assert result == {"x": 2, "y": 2}
    assert original == {"x": 1, "y": 2}


def test_python_multiple_updates_same_base():
    src = (POINT
           + "fn both(p: Point) -> Point { return { p | x = 7, y = 8 } }\n")
    assert _exec_python(src)["both"]({"x": 1, "y": 2}) == {"x": 7, "y": 8}


# ---------------------------------------------------------------- typescript

def test_typescript_emits_spread():
    sys.path.insert(0, str(ROOT / "backends" / "typescript"))
    try:
        spec = importlib.util.spec_from_file_location(
            "tsemit_record_update", ROOT / "backends" / "typescript" / "emit.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        out = module.emit(compile_source(POINT + MOVE))
    finally:
        sys.path.remove(str(ROOT / "backends" / "typescript"))
    assert "{ ...p, x:" in out


# ---------------------------------------------------------------- refusals

@pytest.mark.parametrize("backend,tier", [
    ("rust", "rust"), ("java", "java"), ("go", "go"), ("wasm", "wasm"),
])
def test_deferred_tiers_refuse_naming_the_tier(backend, tier):
    sys.path.insert(0, str(ROOT / "backends" / backend))
    try:
        spec = importlib.util.spec_from_file_location(
            f"emit_ru_{backend}", ROOT / "backends" / backend / "emit.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with pytest.raises(ValueError) as excinfo:
            module.emit(compile_source(POINT + MOVE))
    finally:
        sys.path.remove(str(ROOT / "backends" / backend))
    message = str(excinfo.value)
    assert tier in message
    assert "python" in message and "typescript" in message


# ------------------------------------------------- block arms (§4, deferred)

BLOCK_ARM = ("fn f(area: Int) -> Int "
             "{ return match area { _ => { let doubled = area * 2  doubled + 1 } } }\n")


def test_block_arm_parses():
    from revl.parser import ExprBlockArm, Parser
    prog = Parser(BLOCK_ARM, "t").parse()
    arm = prog.fn_decls[0].body[0].expr.arms[0][2]
    assert isinstance(arm, ExprBlockArm)
    assert [s.name for s in arm.stmts] == ["doubled"]


def test_block_arm_typechecks_but_lowering_refuses():
    # parse+typecheck pass; the §6 refusal comes from lowering, naming deferral
    msg = _err(BLOCK_ARM)
    assert "no backend emits them yet" in msg


def test_block_arm_var_refused_at_parse():
    with pytest.raises(RevlError) as excinfo:
        compile_source("fn f(a: Int) -> Int "
                       "{ return match a { _ => { var d = 2  d } } }\n")
    assert "`var`" in str(excinfo.value)



# --- the errata fence: updates on anonymously-typed receivers are unchecked.
# Pinned so the fence in docs/contract-errata.md cannot rot silently, and so
# whoever closes the structural-records design item flips these to refusals.


def test_update_on_anonymous_let_receiver_is_unchecked_for_now():
    src = '''type C = { h: Str }
fn main() -> Int {
  let a = { h: "x" }
  let b = { a | h = 5 }
  assert b.h == 5
  return 0
}
'''
    compile_source(src)  # accepted TODAY; refusal is the design item's exit test


def test_update_through_a_declared_boundary_is_checked():
    src = '''type C = { h: Str }
fn bump(c: C) -> C {
  return { c | h = 5 }
}
'''
    with pytest.raises(RevlError) as excinfo:
        compile_source(src)
    assert "expects `Str`, got `Int`" in str(excinfo.value)
