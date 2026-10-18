"""The type-alias sweep reaches every node kind (issues #2514 and #2515).

`_resolve_type_aliases` erases a transparent alias (`type Count = Int`) from
every written annotation before the checker runs. It used to do so through a
hand-written walker that named each statement and expression kind, and every
kind it did not name kept the alias: a prop test's parameters and body (#2514),
and a lambda annotation inside an `emit`, an `effect`, an `endorse`, a `spawn`
config or a `subscribe` stage (#2515). Issue #2509 was the same defect for a
match block arm.

The sweep now walks the program by dataclass FIELD, so a node kind it has
never heard of is still descended into; what it must still be told is which
fields of a kind are annotations (`lower._ANNOTATION_FIELDS`). Two tests here
guard the two halves:

* the reflection test reads every dataclass in `revl.parser` and fails on a
  type-carrying field that is neither in the sweep's table nor in this file's
  list of fields that hold no written annotation, so a NEW node kind with an
  annotation cannot be added without a decision here;
* the slot tests put the alias in one position each, run the sweep, and walk
  the parsed program by reflection for any string still naming the alias, so
  a slot is covered whatever kind its node turns out to be.
"""

from __future__ import annotations

import dataclasses
import inspect
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import RevlError, compile_source  # noqa: E402
from revl import fault as fault_mod  # noqa: E402
from revl import lower as _lower  # noqa: E402
from revl import parser as _parser  # noqa: E402
from revl.lower import _resolve_type_aliases  # noqa: E402
from revl.parser import parse_text  # noqa: E402

# empty where the sweep still names node kinds by hand (the tree before this
# file), so each test below fails there on its own assertion
_ANNOTATION_FIELDS = getattr(_lower, "_ANNOTATION_FIELDS", {})
_UNSWEPT_FIELDS = getattr(_lower, "_UNSWEPT_FIELDS", {})

# --- every type-carrying field of every node kind is accounted for ----------

_TYPE_FIELD = re.compile(r"(^|_)(type|types|returns|payload)$")

# Parser fields whose name says type but which hold no WRITTEN annotation.
# A field added to the parser lands here or in `_ANNOTATION_FIELDS`, never
# in neither.
NOT_WRITTEN = {
    ("ExprArrow", "param_types"): "resolved by the checker, after the sweep",
    ("ExprArrow", "returns"): "resolved by the checker, after the sweep",
    ("ExprArrow", "resolved_type"): "resolved by the checker, after the sweep",
}


def _parser_dataclasses():
    for name, cls in inspect.getmembers(_parser, inspect.isclass):
        if dataclasses.is_dataclass(cls) and cls.__module__ == _parser.__name__:
            yield name, cls


def test_every_type_carrying_field_of_every_node_kind_is_accounted_for():
    found = {(name, f.name) for name, cls in _parser_dataclasses()
             for f in dataclasses.fields(cls) if _TYPE_FIELD.search(f.name)}
    swept = {(kind.__name__, f) for kind, names in _ANNOTATION_FIELDS.items()
             for f in names}
    assert found - swept - set(NOT_WRITTEN) == set()
    assert set(NOT_WRITTEN) <= found      # no stale entry in this list
    assert swept <= found                 # no stale entry in the sweep's table


def test_the_sweep_s_tables_name_real_fields():
    for table in (_ANNOTATION_FIELDS, _UNSWEPT_FIELDS):
        for kind, names in table.items():
            assert set(names) <= {f.name for f in dataclasses.fields(kind)}, kind


# --- one slot per position; the sweep leaves no alias name anywhere --------

HEAD = '''type Count = Int
type Handle = { fd: Int }
service Mem { fn go(x: Int) -> Int }
service Vault { endorse[confidential] fn lookup(name: Str) -> Result[Str, Str] }
service Mail { emission[mail] fn go(x: Int) -> Int }
extern emission[mail] fn send_mail(n: Int) -> Int = @py { return n }
extern acquire fn open_h(n: Int) -> Handle undo release_h(result) = @py { return {"fd": n} }
extern local fn release_h(h: Handle) -> Unit = @py { return None }
component Child { config { n: Int = 0 } }
'''

ARROW = "((v: Count): Count => v)"

SLOTS = {
    "prop test parameter": 'prop test "p" (n: Count) {\n  assert n == n\n}',
    "prop test body let": 'prop test "p" (n: Int) {\n  let y: Count = n\n  assert y == n\n}',
    "prop test body arrow": f'prop test "p" (n: Int) {{\n  assert {ARROW}(n) == n\n}}',
    "emit statement": f'''component Writer provides mail: Mail {{
  provide mail {{ fn go(x) {{ emit send_mail({ARROW}(x)) return x }} }}
}}''',
    "emit expression": f'''component Writer provides mail: Mail {{
  provide mail {{ fn go(x) {{ let a = emit send_mail({ARROW}(x)) return a }} }}
}}''',
    "effect acquire": f'''component C {{
  let h = effect open_h({ARROW}(1)) undo release_h(h)
}}''',
    "endorse expression": f'''component K provides vault: Vault {{
  provide vault {{ fn lookup(name) = Err(endorse[confidential]({ARROW}(1), reason = "r")) }}
}}''',
    "spawn config": f'''component S {{
  let w = effect spawn Child with {{ n: {ARROW}(1) }} undo w.dispose()
}}''',
    "subscribe stage": '''component C {
  let src = effect Stream.source() undo src.close()
  let sub = subscribe src.map((x: Count): Count => x) undo sub.close()
}''',
    "hole annotation": 'fn helper(x: Int) -> Int = hole[Count] "a function body"',
    "test body": 'test "t" {\n  let y: Count = 1\n  assert y == 1\n}',
}

_ALIAS = re.compile(r"\bCount\b")


def _mentions(node, seen=None) -> int:
    """How many strings under `node` name the alias, by reflection."""
    seen = set() if seen is None else seen
    if isinstance(node, str):
        return len(_ALIAS.findall(node))
    if isinstance(node, (list, tuple, set, frozenset)):
        return sum(_mentions(n, seen) for n in node)
    if isinstance(node, dict):
        return sum(_mentions(n, seen) for n in node.values())
    if not dataclasses.is_dataclass(node) or id(node) in seen:
        return 0
    seen.add(id(node))
    # `type_decls` holds the alias's own declaration, which the sweep drops;
    # `FnDecl.idents` is the fn's token spellings (issue #1900), not a type
    return sum(_mentions(getattr(node, f.name), seen)
               for f in dataclasses.fields(node)
               if f.name not in ("type_decls", "idents"))


@pytest.mark.parametrize("slot", sorted(SLOTS))
def test_the_sweep_leaves_no_alias_name_in_any_slot(slot):
    program = parse_text(HEAD + SLOTS[slot] + "\n", "a.rvl")
    written = len(_ALIAS.findall(SLOTS[slot]))   # the slot's annotations
    assert written >= 1
    assert _mentions(program) == written
    _resolve_type_aliases(program, "a.rvl")
    assert _mentions(program) == 0


def test_an_extern_config_field_is_left_as_written():
    # examples/rejections/g4_extern_config_alias_opaque.rvl: an alias in an
    # extern's config block is refused as an opaque head, on both sides
    program = parse_text(
        HEAD + 'extern pure fn load(p: Str) -> Str\n  config { n: Count }\n'
        '  = @py { return p }\n', "a.rvl")
    assert _mentions(program) == 1
    _resolve_type_aliases(program, "a.rvl")
    assert _mentions(program) == 1


# --- the programs compile, and a wrong use is refused ----------------------

PROP = '''type Count = Int

prop test "p" (n: Count) {
  let y: Count = n
  assert y == n
}
'''


def test_a_prop_test_over_an_alias_compiles_and_holds_on_the_py_tier():
    ir = compile_source(PROP, "t.rvl")
    assert ir["prop_tests"][0]["params"][0]["type"] == "Int"
    failures, dossier = fault_mod.run_prop_units(ir, fault_mod.prop_units(ir))
    assert failures == 0
    assert dossier["counts"]["props"] == 1


def test_a_wrong_use_in_a_prop_test_body_is_refused_as_an_int():
    with pytest.raises(RevlError, match=r"expects `Int`, got `Str`"):
        compile_source(PROP.replace("let y: Count = n", 'let y: Count = "s"'), "t.rvl")


COMPONENT_SLOTS = [k for k in SLOTS if not k.startswith(("prop", "hole", "test"))]


@pytest.mark.parametrize("slot", sorted(COMPONENT_SLOTS))
def test_an_alias_in_a_lambda_annotation_compiles_in_every_component_slot(slot):
    compile_source(HEAD + SLOTS[slot] + "\n", "a.rvl")


def test_a_wrong_use_in_a_spawn_config_is_refused_as_an_int():
    src = HEAD + SLOTS["spawn config"].replace(f"{ARROW}(1)", f'{ARROW}("s")') + "\n"
    with pytest.raises(RevlError, match=r"expects `Int`, got `Str`"):
        compile_source(src, "a.rvl")


# A lambda in a component body is a stratum-3 arrow, which the checker does
# not resolve, so its annotations used to be held to nothing there (#2515).
# They are now held where a `fn` body holds them: an in-place application's
# arguments against the parameter annotations, and the body against the
# return annotation. Each pair is (the slot's well-typed use, a wrong one).
WRONG_USE = {
    "emit statement": (f"{ARROW}(x)", f'{ARROW}("s")'),
    "emit expression": (f"{ARROW}(x)", f'{ARROW}("s")'),
    "effect acquire": (f"{ARROW}(1)", f'{ARROW}("s")'),
    "endorse expression": (f"{ARROW}(1)", f'{ARROW}("s")'),
    "subscribe stage": ("(x: Count): Count => x", '(x: Count): Count => "s"'),
}


@pytest.mark.parametrize("slot", sorted(WRONG_USE))
def test_a_wrong_use_in_a_component_lambda_is_refused(slot):
    original, wrong = WRONG_USE[slot]
    assert original in SLOTS[slot]
    src = HEAD + SLOTS[slot].replace(original, wrong) + "\n"
    with pytest.raises(RevlError, match=r"expects `Int`, got `Str`"):
        compile_source(src, "a.rvl")


def test_an_argument_spelled_like_the_arrow_s_parameter_keeps_its_own_type():
    # the argument `x` is the method's Str parameter, not the arrow's `x`
    src = HEAD + '''service Echo { fn go(x: Str) -> Int }
component E provides echo: Echo {
  provide echo { fn go(x) = ((x: Count): Count => x)(x) }
}
'''
    with pytest.raises(RevlError, match=r"expects `Int`, got `Str`"):
        compile_source(src, "a.rvl")


def test_an_arrow_applied_in_place_takes_exactly_its_arity():
    src = HEAD + SLOTS["effect acquire"].replace(f"{ARROW}(1)", f"{ARROW}(1, 2)") + "\n"
    assert f"{ARROW}(1)" in SLOTS["effect acquire"]
    with pytest.raises(RevlError, match=r"takes 1 argument\(s\), 2 given"):
        compile_source(src, "a.rvl")


def _refusal(src: str) -> RevlError:
    with pytest.raises(RevlError) as excinfo:
        compile_source(src, "a.rvl")
    return excinfo.value


def test_the_arity_refusal_reads_as_it_does_in_a_fn_body():
    # no written return: the type the message names is the body's, as in a fn
    arrow = "((v: Int) => v)(1, 2)"
    in_fn = _refusal(f"fn d() -> Int {{ return {arrow} }}\n")
    in_component = _refusal(HEAD + SLOTS["effect acquire"].replace(f"{ARROW}(1)", arrow) + "\n")
    assert in_component.message == (
        "this call's callee is a `(Int) -> Int` and takes 1 argument(s), 2 given")
    assert (in_component.message, in_component.hint, in_component.code) == \
        (in_fn.message, in_fn.hint, in_fn.code)


# Bodies a written return used to leave unread in a component, and which the
# check must still admit: a generic call whose type variable the arrow body
# does not solve, and a built-in case.
GENERIC_MAP = "fn map_[A, B](xs: List[A], f: (A) -> B) -> List[B] { return xs }\n"
CHECKED_BODIES = {
    "generic call": "(xs: List[Int]): List[Int] => map_(xs, (y) => y)",
    "Ok case": "(v: Int): Result[Int, Str] => Ok(v)",
}


@pytest.mark.parametrize("body", sorted(CHECKED_BODIES))
def test_a_written_return_admits_a_well_typed_component_body(body):
    compile_source(GENERIC_MAP + HEAD + f'''component G provides mem: Mem {{
  provide mem {{ fn go(x) {{ let f = {CHECKED_BODIES[body]} return x }} }}
}}
''', "a.rvl")


# --- an unknown type name in a component lambda is refused (#2515) ---------

# No type `Nope` is declared, so it is an opaque nominal that no value has,
# and a use of the arrow is refused as it is in a `fn` body. Spawn config is
# not here: its value is checked by `check_ast` and was already refused.
NOPE = "((v: Nope): Nope => v)"
STORE = "service Store {\n  fn grab(k: Int) -> Int\n  fn release(k: Int) -> Int\n}\n"

UNKNOWN_NAME = {
    "emit statement": f'''component Writer provides mail: Mail {{
  provide mail {{ fn go(x) {{ emit send_mail({NOPE}(1)) return x }} }}
}}''',
    "emit expression": f'''component Writer provides mail: Mail {{
  provide mail {{ fn go(x) {{ let a = emit send_mail({NOPE}(1)) return a }} }}
}}''',
    "effect acquire": f'''component C {{
  let h = effect open_h({NOPE}(1)) undo release_h(h)
}}''',
    "effect undo": f'''component C requires store: Store {{
  let r = effect store.grab(1) undo store.release({NOPE}(1))
}}''',
    "effect setup": f'''component C requires store: Store {{
  let r = effect {{
    let k = {NOPE}(2)
    store.grab(k)
  }} undo store.release(2)
}}''',
    "endorse expression": f'''component K provides vault: Vault {{
  provide vault {{ fn lookup(name) = Err(endorse[confidential]({NOPE}("s"), reason = "r")) }}
}}''',
    "subscribe stage": '''component C {
  let src = effect Stream.source() undo src.close()
  let sub = subscribe src.map((x: Int): Nope => x) undo sub.close()
}''',
}


@pytest.mark.parametrize("slot", sorted(UNKNOWN_NAME))
def test_an_unknown_type_name_in_a_component_lambda_is_refused(slot):
    src = HEAD + STORE + UNKNOWN_NAME[slot] + "\n"
    with pytest.raises(RevlError, match=r"expects `Nope`, got `(Int|Str)`"):
        compile_source(src, "a.rvl")
