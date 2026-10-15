"""A variant case call takes exactly as many arguments as the case carries.

Issue #2447: the reference checker counted nothing at a case call. `Box(1, 2)`
for `Box(Int)`, `Pp()` for `Pp(Int)`, `Qq(1)` for a nullary `Qq`, the same
on a user generic variant and on the built-in `Some`/`None`/`Ok`/`Err` were
all admitted, and only some backends noticed. Every case carries 0 or 1
payloads, so a call with any other count is refused, naming the case and both
counts.

`REFUSED` and `ADMITTED` are read by tests/test_selfhost_compile.py, which
records how the native chain answers each refused document.
"""

from __future__ import annotations

import pytest

from revl import compile_source
from revl.errors import RevlError

SHAPE = "type Shape = Dot | Box(Int)\n"
PAIR = "type P = Pp(Int) | Qq\n"
MB = "type Mb[T] = Jj(T) | Nn\n"
TALLY = "service Tally { fn count() -> Int\n  fn shape() -> Shape }\n"

# name -> (source, the refusal message)
REFUSED: dict[str, tuple[str, str]] = {
    "extra_payload": (
        SHAPE + "fn mk() -> Shape { return Box(1, 2) }",
        "variant case `Box` takes 1 payload argument, 2 given"),
    "extra_payload_in_let": (
        SHAPE + "fn mk() -> Int {\n  let s = Box(1, 2)\n  return 1\n}",
        "variant case `Box` takes 1 payload argument, 2 given"),
    "missing_payload": (
        PAIR + "fn mk() -> P { return Pp() }",
        "variant case `Pp` takes 1 payload argument, 0 given"),
    "argument_to_nullary": (
        PAIR + "fn mk() -> P { return Qq(1) }",
        "variant case `Qq` takes 0 payload arguments, 1 given"),
    "argument_to_nullary_as_argument": (
        PAIR + "fn un(p: P) -> Int { return 1 }\n"
               "fn mk() -> Int { return un(Qq(1)) }",
        "variant case `Qq` takes 0 payload arguments, 1 given"),
    "generic_missing_payload": (
        MB + "fn mk() -> Mb[Int] { return Jj() }",
        "variant case `Jj` takes 1 payload argument, 0 given"),
    "generic_extra_payload": (
        MB + "fn mk() -> Mb[Int] { return Jj(1, 2) }",
        "variant case `Jj` takes 1 payload argument, 2 given"),
    "generic_argument_to_nullary": (
        MB + "fn mk() -> Mb[Int] { return Nn(1) }",
        "variant case `Nn` takes 0 payload arguments, 1 given"),
    "some_extra": (
        "fn mk() -> Opt[Int] { return Some(1, 2) }",
        "variant case `Some` takes 1 payload argument, 2 given"),
    "some_missing": (
        "fn mk() -> Opt[Int] { return Some() }",
        "variant case `Some` takes 1 payload argument, 0 given"),
    "none_argument": (
        "fn mk() -> Opt[Int] { return None(1) }",
        "variant case `None` takes 0 payload arguments, 1 given"),
    "ok_extra": (
        "fn mk() -> Result[Int, Str] { return Ok(1, 2) }",
        "variant case `Ok` takes 1 payload argument, 2 given"),
    "err_missing": (
        "fn mk() -> Result[Int, Str] { return Err() }",
        "variant case `Err` takes 1 payload argument, 0 given"),
    "user_ok_missing": (
        "type Outcome = Ok(Int) | Err(Str)\n"
        "fn mk() -> Outcome { return Ok() }",
        "variant case `Ok` takes 1 payload argument, 0 given"),
    # a provide body is checked on its lowered IR, not by `infer_ast`
    "provide_expression_body": (
        SHAPE + TALLY + "component C provides t: Tally { provide t {\n"
                        "  fn count() = 1\n"
                        "  fn shape() = Box(1, 2) } }",
        "variant case `Box` takes 1 payload argument, 2 given"),
    "provide_block_body": (
        SHAPE + TALLY + "component C provides t: Tally { provide t {\n"
                        "  fn count() = 1\n"
                        "  fn shape() {\n"
                        "    let o = Some(1, 2)\n"
                        "    return Dot\n"
                        "  } } }",
        "variant case `Some` takes 1 payload argument, 2 given"),
}

# Well-formed calls around the refusals: the rule must not move these.
ADMITTED: dict[str, str] = {
    "payload": SHAPE + "fn mk() -> Shape { return Box(1) }",
    "nullary_bare": SHAPE + "fn mk() -> Shape { return Dot }",
    "some": "fn mk() -> Opt[Int] { return Some(1) }",
    "none": "fn mk() -> Opt[Int] { return None }",
    "ok": "fn mk() -> Result[Int, Str] { return Ok(1) }",
    "err": 'fn mk() -> Result[Int, Str] { return Err("e") }',
    # a local of function type named like a case is a call of the local
    "provide_body": (
        SHAPE + TALLY + "component C provides t: Tally { provide t {\n"
                        "  fn count() = 1\n"
                        "  fn shape() {\n"
                        "    let o = Some(1)\n"
                        "    return Box(1)\n"
                        "  } } }"),
    "shadowing_local": (
        SHAPE + "fn mk() -> Int {\n"
                "  let Box = (a: Int, b: Int) => a + b\n"
                "  return Box(1, 2)\n}"),
}


@pytest.mark.parametrize("name", sorted(REFUSED))
def test_wrong_payload_count_is_refused(name):
    source, message = REFUSED[name]
    with pytest.raises(RevlError) as refused:
        compile_source(source, "arity.rvl")
    assert message in str(refused.value), str(refused.value)
    assert refused.value.code == "T1"


@pytest.mark.parametrize("name", sorted(ADMITTED))
def test_right_payload_count_is_admitted(name):
    compile_source(ADMITTED[name], "arity.rvl")


def test_refusal_hint_names_the_payload():
    with pytest.raises(RevlError) as refused:
        compile_source(REFUSED["missing_payload"][0], "arity.rvl")
    assert refused.value.hint == "`Pp` carries one `Int` payload: write `Pp(value)`"
    with pytest.raises(RevlError) as refused:
        compile_source(REFUSED["argument_to_nullary"][0], "arity.rvl")
    assert refused.value.hint == "`Qq` carries no payload: write `Qq` on its own"
