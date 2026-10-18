"""An expression inside a `${...}` interpolation is checked like any other.

Issue #2478: with `type Shape = Dot | Box(Int)` and `fn n(s: Shape) -> Int`,
`let a = n(Box("s"))` was refused (payload type) and `let a = n(Box(1, 2))`
(payload count, #2447), but the same calls inside a template were admitted
and the wrong program reached the backends. The checker typed a template as
`Str` without inferring its operands, both on the parser AST (`infer_ast`) and
on the lowered component-body IR (`infer_ir`, a provide method).

Each template document is paired with the let initializer that holds the same
expression, and the two must be refused with the same code and message.
"""

from __future__ import annotations

import pytest

from revl import compile_source
from revl.errors import RevlError

HEAD = ("type Shape = Dot | Box(Int)\n"
        "fn n(s: Shape) -> Int { return 1 }\n")
LABEL = "service Label { fn label() -> Str }\n"


def _fn(line: str) -> str:
    return HEAD + "fn mk() -> Str {\n  " + line + "\n  return \"x\"\n}"


def _provide(line: str) -> str:
    return (HEAD + LABEL + "component C provides t: Label { provide t {\n"
            "  fn label() {\n    " + line + "\n    return \"x\"\n  } } }")


# the wrong expressions, each refused in a let initializer today
WRONG = {
    "payload_type": 'n(Box("s"))',
    "payload_count": "n(Box(1, 2))",
    "builtin_payload_count": "Some(1, 2) ?? 3",
    "call_argument_type": 'n(Box(1)) + n("s")',
}

# template positions an expression `e` can sit in
POSITIONS = {
    "template": "`x${%s}`",
    "nested_template": "`a${`b${%s}`}`",
    "template_in_concat": '"p" + `x${%s}`',
    "template_match_arm": "`a${match Dot { Dot => %s, Box(v) => v }}`",
}

CASES = {f"{body}-{wrong}-{pos}": (body, wrong, pos)
         for body in ("fn", "provide") for wrong in WRONG for pos in POSITIONS}


def _refusal(source: str) -> tuple[str, str]:
    with pytest.raises(RevlError) as refused:
        compile_source(source, "interp.rvl")
    err = refused.value
    return err.code, str(err).splitlines()[0]


@pytest.mark.parametrize("name", sorted(CASES))
def test_template_operand_refused_like_a_let_initializer(name):
    body, wrong, pos = CASES[name]
    wrap = _fn if body == "fn" else _provide
    expr = WRONG[wrong]
    want = _refusal(wrap(f"let a = {expr}"))
    got = _refusal(wrap(f"let a = {POSITIONS[pos] % expr}"))
    assert got == want
    assert got[0] == "T1"


def test_the_issue_shapes_name_the_payload():
    assert "`Box(...)` payload expects `Int`, got `Str`" in \
        _refusal(_fn('let a = `${n(Box("s"))}`'))[1]
    assert "variant case `Box` takes 1 payload argument, 2 given" in \
        _refusal(_fn("let a = `${n(Box(1, 2))}`"))[1]


@pytest.mark.parametrize("wrong", sorted(WRONG))
def test_provide_expression_body_template_operand_is_refused(wrong):
    # an expression body is checked on its lowered `format` node
    source = (HEAD + LABEL + "component C provides t: Label { provide t {\n"
              "  fn label() = `x${" + WRONG[wrong] + "}` } }")
    want = _refusal(_provide(f"let a = {WRONG[wrong]}"))
    got = _refusal(source)
    assert got[0] == want[0] == "T1"
    assert got[1].split(": ", 1)[1] == want[1].split(": ", 1)[1]


# well-formed operands in the same positions: the walk must not move these
ADMITTED = {
    "fn_template": _fn("let a = `x${n(Box(1))}`"),
    "fn_nested": _fn("let a = `a${`b${n(Dot)}`}`"),
    "fn_some": _fn("let a = `x${Some(1) ?? 3}`"),
    "fn_match_arm": _fn("let a = `a${match Dot { Dot => 1, Box(v) => v }}`"),
    "provide_template": _provide("let a = `x${n(Box(1))}`"),
    "provide_expression_body": (
        HEAD + LABEL + "component C provides t: Label { provide t {\n"
        "  fn label() = `x${n(Box(1))}` } }"),
}


@pytest.mark.parametrize("name", sorted(ADMITTED))
def test_well_formed_template_operand_is_admitted(name):
    compile_source(ADMITTED[name], "interp.rvl")
