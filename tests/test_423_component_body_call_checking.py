"""Item 423: a component-body call to a declared `fn` or extern is held to the
callee's declared arity and argument types.

Item 404 handed the raising filename to `infer_ir` for a provide-method `let`;
item 420 handed it over for the site `undo` slot. Every other component
position still called `infer_ir` in its non-raising oracle mode, so a
component body was the one place in the language where a call to a DECLARED
callable was checked for nothing at all. The two shapes item 423 names
compiled clean on main:

    component C { let s = effect secret_put(42) undo secret_release(s) }
    component C { let s = effect secret_put("v", "w") undo secret_release(s) }

The fix is not a new judgment and not a new slot. `_lower_component_pure_expr`
is the ONE place a component-stratum `fn` node is ever built, and `_lower_expr`
funnels every component expression through it, so the check sits there and
every position inherits it at once: acquire expressions, site `undo` and
`compensate` slots, `emit` and `await` expressions, `fail` messages, guard
conditions, effect-block setup, and provide-method bodies.

The judgment itself is `infer_ir`'s `fn` arm, unchanged: one signature table,
one arity rule, one `unify`, and the same messages a `fn` body has always
produced.
"""

import pytest

from revl.compiler import compile_source
from revl.errors import RevlError, RevlErrors


_VAULT = (
    "type SecretHandle = Opaque\n"
    "extern acquire fn secret_put(v: Str) -> SecretHandle"
    " undo secret_release(result) = @py { return 1 }\n"
    "extern pure fn secret_release(h: SecretHandle) -> Unit = @py { return }\n"
    "extern pure fn note(tag: Str) -> Unit = @py { return }\n"
    "extern emission fn ship(tag: Str) -> Unit = @py { return }\n"
    "pub fn tag(s: Str) -> Str { return s }\n"
)


def _compile(src: str) -> dict:
    return compile_source(_VAULT + src, "t.rvl")


def _refusal(src: str) -> str:
    with pytest.raises((RevlError, RevlErrors)) as ei:
        _compile(src)
    return str(ei.value)


# -- the two shapes item 423 names -----------------------------------------

def test_acquire_argument_type_is_checked():
    msg = _refusal('component C {\n'
                   '  let s = effect secret_put(42) undo secret_release(s)\n'
                   '}\n')
    assert "argument 1 of `secret_put(...)` expects `Str`, got `Int`" in msg


def test_acquire_argument_arity_is_checked():
    msg = _refusal('component C {\n'
                   '  let s = effect secret_put("v", "w") undo secret_release(s)\n'
                   '}\n')
    assert "`secret_put` takes 1 argument(s), 2 given" in msg


def test_the_correct_acquire_still_compiles():
    ir = _compile('component C {\n'
                  '  let s = effect secret_put(tag("v")) undo secret_release(s)\n'
                  '}\n')
    assert any(c["name"] == "C" for c in ir["components"])


# -- every other component-body position, one per slot ---------------------
#
# These are not repetitions of the two above: each names a DIFFERENT lowering
# path that reached `infer_ir` in oracle mode before this. They are what makes
# the item's "general case" claim testable rather than asserted.

def test_site_undo_slot_is_checked():
    msg = _refusal('component C {\n'
                   '  let s = effect secret_put("v") undo secret_release("x")\n'
                   '}\n')
    assert "argument 1 of `secret_release(...)` expects `SecretHandle`, got `Str`" in msg


def test_unbound_effect_undo_slot_is_checked():
    msg = _refusal('component C {\n'
                   '  effect secret_put("v") undo note(42)\n'
                   '}\n')
    assert "argument 1 of `note(...)` expects `Str`, got `Int`" in msg


def test_emit_expression_is_checked():
    msg = _refusal('component C {\n  emit ship(42)\n}\n')
    assert "argument 1 of `ship(...)` expects `Str`, got `Int`" in msg


def test_emit_compensate_slot_is_checked():
    msg = _refusal('component C {\n  emit ship("a") compensate note(42)\n}\n')
    assert "argument 1 of `note(...)` expects `Str`, got `Int`" in msg


def test_fail_message_is_checked():
    msg = _refusal('component C {\n  if (true) { fail tag(42) }\n}\n')
    assert "argument 1 of `tag(...)` expects `Str`, got `Int`" in msg


def test_guard_condition_is_checked():
    msg = _refusal('component C {\n  if (tag(42) == "x") { fail "no" }\n}\n')
    assert "argument 1 of `tag(...)` expects `Str`, got `Int`" in msg


def test_effect_block_setup_is_checked():
    msg = _refusal('component C {\n'
                   '  let s = effect { let t = tag(42)  secret_put(t) }'
                   ' undo secret_release(s)\n'
                   '}\n')
    assert "argument 1 of `tag(...)` expects `Str`, got `Int`" in msg


def test_provide_method_let_is_checked():
    msg = _refusal('service S { fn go() -> Str }\n'
                   'component C provides s: S {\n'
                   '  provide s { fn go() -> Str { let x = tag(42)  return x } }\n'
                   '}\n')
    assert "argument 1 of `tag(...)` expects `Str`, got `Int`" in msg


def test_provide_method_return_is_checked():
    msg = _refusal('service S { fn go() -> Str }\n'
                   'component C provides s: S {\n'
                   '  provide s { fn go() -> Str { return tag(42) } }\n'
                   '}\n')
    assert "argument 1 of `tag(...)` expects `Str`, got `Int`" in msg


def test_provide_method_emit_is_checked():
    msg = _refusal('service S { emission fn go() -> Str }\n'
                   'component C provides s: S {\n'
                   '  provide s { fn go() -> Str { emit ship(42)  return "k" } }\n'
                   '}\n')
    assert "argument 1 of `ship(...)` expects `Str`, got `Int`" in msg


# -- checked at construction, so nesting cannot launder a call -------------

def test_a_call_nested_under_another_call_is_still_checked():
    # `infer_ir` deliberately does not descend into an opaque node's arguments,
    # so a check rooted at a step would miss this. Judging each call as it is
    # BUILT (bottom-up) reaches it.
    msg = _refusal('component C {\n'
                   '  let s = effect secret_put(tag(42).concat("x"))'
                   ' undo secret_release(s)\n'
                   '}\n')
    assert "argument 1 of `tag(...)` expects `Str`, got `Int`" in msg


# -- leniency is unchanged -------------------------------------------------

def test_an_unknown_argument_type_is_still_admitted():
    # A host-frontier value infers to None; `unify` accepts it and the call
    # stays admitted. Only a DEFINITE mismatch raises, so closing this hole
    # does not turn the component stratum into a stricter one than a `fn` body.
    ir = _compile('extern pure fn opaque_str() -> Any = @py { return "x" }\n'
                  'component C {\n'
                  '  let s = effect secret_put(opaque_str())'
                  ' undo secret_release(s)\n'
                  '}\n')
    assert any(c["name"] == "C" for c in ir["components"])


def test_a_name_that_is_not_a_declared_callable_is_not_judged_here():
    # ADT construction, `Some`/`None` and the host constructors take the arms
    # above this one and are unaffected.
    ir = _compile('component C {\n'
                  '  let m = effect Map.new() undo m.drop()\n'
                  '}\n')
    assert any(c["name"] == "C" for c in ir["components"])
