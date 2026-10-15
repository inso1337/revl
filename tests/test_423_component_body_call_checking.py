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


# -- issue #2483: an effect call's arguments meet the same checks ------------
#
# A required-service operation compared each argument's type in the oracle
# mode of `infer_ir`, which never raises, so a wrong case payload nested in the
# argument (`Box("x")`, `Box(1, 2)`) was admitted where a `let` initializer
# refuses it. A built-in `Some`/`Ok`/`Err` inferred no type in the component
# stratum, so `put_opt(Some("x"))` met `Opt[Int]` unjudged. Each refusal is
# the message and code the same expression gets in a `fn` body.
#
# `EFFECT_ARG_REFUSED` is read by tests/test_selfhost_compile.py, which records
# how the native chain answers each refused document.

_SHAPES = _VAULT + (
    "type Shape = Dot | Box(Int)\n"
    "service Sink {\n"
    "  emission fn put(s: Shape)\n"
    "  emission fn put_int(n: Int)\n"
    "  emission fn os(o: Opt[Shape])\n"
    "}\n"
    "extern acquire fn put_opt(o: Opt[Int]) -> SecretHandle"
    " undo secret_release(result) = @py { return 1 }\n"
    "extern acquire fn put_res(r: Result[Int, Str]) -> SecretHandle"
    " undo secret_release(result) = @py { return 1 }\n"
)


def _emit_put(arg: str) -> str:
    return _SHAPES + f"component C requires k: Sink {{\n  emit k.put({arg})\n}}\n"


def _acquire(call: str) -> str:
    return _SHAPES + ("component C {\n"
                      f"  let h = effect {call} undo secret_release(h)\n}}\n")


def _emit_put_os(arg: str) -> str:
    return _SHAPES + f"component C requires k: Sink {{\n  emit k.os({arg})\n}}\n"


_PAYLOAD_TYPE = "`Box(...)` payload expects `Int`, got `Str`"

# name -> (source, the refusal message)
EFFECT_ARG_REFUSED: dict[str, tuple[str, str]] = {
    "service_payload_type": (_emit_put('Box("x")'), _PAYLOAD_TYPE),
    "service_payload_extra": (
        _emit_put("Box(1, 2)"),
        "variant case `Box` takes 1 payload argument, 2 given"),
    "service_payload_missing": (
        _emit_put("Box()"),
        "variant case `Box` takes 1 payload argument, 0 given"),
    "service_argument_to_nullary": (
        _emit_put("Dot(1)"),
        "variant case `Dot` takes 0 payload arguments, 1 given"),
    "service_payload_in_provide_method": (
        _SHAPES + "service S { emission fn go() -> Str }\n"
                  "component C requires k: Sink provides s: S {\n"
                  '  provide s { fn go() -> Str { emit k.put(Box("x"))'
                  '  return "k" } }\n}\n',
        _PAYLOAD_TYPE),
    "service_some_payload_type": (_emit_put_os('Some(Box("x"))'), _PAYLOAD_TYPE),
    "service_argument_type": (
        _SHAPES + 'component C requires k: Sink {\n  emit k.put_int("x")\n}\n',
        "`k.put_int` argument `n` expects `Int`, got `Str`"),
    "acquire_some_payload_type": (
        _acquire('put_opt(Some("x"))'),
        "argument 1 of `put_opt(...)` expects `Opt[Int]`, got `Opt[Str]`"),
    "acquire_ok_payload_type": (
        _acquire('put_res(Ok("x"))'),
        "argument 1 of `put_res(...)` expects `Result[Int, Str]`, "
        "got `Result[Str, Any]`"),
    "acquire_err_payload_type": (
        _acquire("put_res(Err(1))"),
        "argument 1 of `put_res(...)` expects `Result[Int, Str]`, "
        "got `Result[Any, Int]`"),
}

# the `fn`-body `let` initializer each component refusal must agree with
_LET_TWIN: dict[str, str] = {
    "service_payload_type": 'Box("x")',
    "service_payload_extra": "Box(1, 2)",
    "service_payload_missing": "Box()",
    "service_argument_to_nullary": "Dot(1)",
    "acquire_some_payload_type": 'put_opt(Some("x"))',
    "acquire_ok_payload_type": 'put_res(Ok("x"))',
    "acquire_err_payload_type": "put_res(Err(1))",
    "service_some_payload_type": 'Some(Box("x"))',
}


@pytest.mark.parametrize("name", sorted(EFFECT_ARG_REFUSED))
def test_effect_argument_is_checked(name):
    source, message = EFFECT_ARG_REFUSED[name]
    with pytest.raises(RevlError) as refused:
        compile_source(source, "t.rvl")
    assert message in str(refused.value), str(refused.value)
    assert refused.value.code == "T1"


@pytest.mark.parametrize("name", sorted(_LET_TWIN))
def test_effect_refusal_matches_the_let_initializer(name):
    source = _SHAPES + ("fn mk() -> Int {\n"
                        f"  let v = {_LET_TWIN[name]}\n  return 1\n}}\n")
    with pytest.raises(RevlError) as refused:
        compile_source(source, "t.rvl")
    assert EFFECT_ARG_REFUSED[name][1] in str(refused.value)
    assert refused.value.code == "T1"


def test_well_typed_effect_arguments_are_still_admitted():
    ir = compile_source(
        _SHAPES + "component C requires k: Sink {\n"
                  "  let a = effect put_opt(Some(1)) undo secret_release(a)\n"
                  "  let b = effect put_opt(None) undo secret_release(b)\n"
                  "  let c = effect put_res(Ok(1)) undo secret_release(c)\n"
                  '  let d = effect put_res(Err("e")) undo secret_release(d)\n'
                  "  emit k.put(Box(1))\n"
                  "  emit k.put(Dot)\n"
                  "  emit k.put_int(1)\n"
                  "}\n", "t.rvl")
    assert any(c["name"] == "C" for c in ir["components"])


# A host builtin's arguments are inferred in raising mode too, so a payload
# nested in one meets the same check (`Pool.open(Str, Int)`).
@pytest.mark.parametrize("arg, message", [
    ('[Box("x")].length()', _PAYLOAD_TYPE),
    ("[Box(1, 2)].length()",
     "variant case `Box` takes 1 payload argument, 2 given"),
])
def test_host_builtin_argument_payload_is_checked(arg, message):
    with pytest.raises(RevlError) as refused:
        compile_source(_SHAPES + "component C {\n"
                       f'  let p = effect Pool.open("u", {arg}) undo p.close()\n'
                       "}\n", "t.rvl")
    assert message in str(refused.value)
    assert refused.value.code == "T1"


def test_well_typed_host_builtin_argument_is_still_admitted():
    compile_source(_SHAPES + "component C {\n"
                   '  let p = effect Pool.open("u", [Box(1)].length())'
                   " undo p.close()\n}\n", "t.rvl")


# Typing the built-in `Some`/`Ok`/`Err` in `infer_ir` also moves provide-method
# verdicts to the `fn` body's: a wrong-typed constructor in a provide return is
# refused, and an `Err` payload read through a `match` is no longer `Any`.

_PROVIDE = ("service Q {\n"
            "  fn go() -> Opt[Int]\n"
            "  fn gr() -> Result[Int, Str]\n"
            "  fn gs() -> Opt[Result[Int, Str]]\n"
            "  fn gi(b: Bool) -> Int\n"
            "}\n")
_GOOD_BODIES = {
    "go": "fn go() = Some(1)",
    "gr": "fn gr() = Ok(1)",
    "gs": "fn gs() = None",
    "gi": "fn gi(b) = 1",
}
_ERR_ARM = ("let r = b ? Ok(1) : Err(2)\n"
            "  return match r { Ok(v) => v, Err(e) => 0 - e }")


def _provide(method: str, body: str) -> str:
    bodies = dict(_GOOD_BODIES, **{method: body})
    return (_PROVIDE + "component C provides q: Q { provide q {\n  "
            + "\n  ".join(bodies[m] for m in sorted(bodies)) + "\n} }\n")


# name -> (provide-method source, the `fn` body twin, what both say)
# `PROVIDE_RETURN_REFUSED` is read by tests/test_selfhost_compile.py.
PROVIDE_RETURN_REFUSED: dict[str, tuple[str, str, str]] = {
    "provide_some_return": (
        _provide("go", 'fn go() = Some("x")'),
        'fn go() -> Opt[Int] { return Some("x") }\n',
        "expects `Opt[Int]`, got `Opt[Str]`"),
    "provide_ok_return": (
        _provide("gr", 'fn gr() = Ok("x")'),
        'fn gr() -> Result[Int, Str] { return Ok("x") }\n',
        "expects `Result[Int, Str]`, got `Result[Str, Any]`"),
    "provide_nested_ok_return": (
        _provide("gs", 'fn gs() = Some(Ok("x"))'),
        'fn gs() -> Opt[Result[Int, Str]] { return Some(Ok("x")) }\n',
        "expects `Opt[Result[Int, Str]]`, got `Opt[Result[Str, Any]]`"),
}


@pytest.mark.parametrize("name", sorted(PROVIDE_RETURN_REFUSED))
def test_provide_return_constructor_matches_the_fn_body(name):
    source, twin, message = PROVIDE_RETURN_REFUSED[name]
    for text in (twin, source):
        with pytest.raises(RevlError) as refused:
            compile_source(text, "t.rvl")
        assert message in str(refused.value), str(refused.value)
        assert refused.value.code == "T1"


def test_provide_err_payload_through_match_is_admitted():
    # main refused this with "operand of `-` expects `Int`, got `Any`"; the
    # `fn` body twin was always admitted
    compile_source("fn gi(b: Bool) -> Int {\n  " + _ERR_ARM + "\n}\n", "t.rvl")
    compile_source(_provide("gi", "fn gi(b) {\n  " + _ERR_ARM + "\n  }"), "t.rvl")
