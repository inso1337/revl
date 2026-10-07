"""String concatenation is discoverable, not guessed (issue #2150).

The measured failure: an agent whose task was string building guessed `++`,
was told "revl has no `++` increment operator — mutate a `var` with `+= 1`", a
sentence about NUMERIC increment, never learned that `+` and `.concat()` exist,
and re-emitted the same file with the same sentence for 26 turns. `++` is not
an operator in revl, and `+` and `.concat()` both already compiled: the gap was
DISCOVERABILITY, not language.

Two independent halves, either of which breaks the loop:

1. the `++` refusal is operand-aware. With a `Str` on either side it names the
   spellings that DO join strings, and the hint names the idiom that teaches
   them, so the answer is in the message that refuses. Every other operand
   shape keeps the increment wording item 384 shipped, which is correct there
   and stays pinned by `examples/rejections/foreign_increment.rvl` and
   `tests/test_frontend.py`.
2. `str-concat` and `str-format` are served idioms, so the surface the refusal
   points at has an answer.

The tests below also compile every form the refusal names: a redirect that
names a spelling which does not work would be a worse message than none.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import RevlError, compile_source, idioms  # noqa: E402
from revl.mcp.server import handle  # noqa: E402


def _refusal(source: str) -> str:
    with pytest.raises(RevlError) as excinfo:
        compile_source(source, "guess.rvl")
    return str(excinfo.value)


def _idiom(name: str) -> dict:
    response = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": "revl_idiom",
                                  "arguments": {"name": name}}})
    return response["result"]


# ---- 1. the numeric case does not regress -----------------------------------

INCREMENT = ("revl has no `++` increment operator — expressions are pure "
             "(syntax-2.0 §3.3)\n"
             "  mutate a `var` with `+= 1` (write `i += 1`), inside a "
             "`while`/`for` loop body")


def test_the_postfix_increment_refusal_is_byte_identical():
    """The shape the rejection fixture pins: nothing is known about operands,
    and the message item 384 shipped is the right one."""
    assert _refusal("fn f() -> Int {\n  var i = 0\n  i++\n  return i\n}") == \
        "guess.rvl:3: " + INCREMENT


def test_the_numeric_infix_refusal_is_byte_identical():
    """`i ++ 1` is not string building: it keeps the increment wording."""
    assert _refusal("fn f(i: Int) -> Int = i ++ 1") == "guess.rvl:1: " + INCREMENT


def test_the_decrement_refusal_is_byte_identical():
    assert "revl has no `--` decrement operator" in \
        _refusal("fn f() -> Int {\n  var i = 0\n  i--\n  return i\n}")


# ---- 2. the string case names what works -------------------------------------

def test_two_strings_are_told_the_two_spellings_that_concatenate():
    rendered = _refusal("fn f(a: Str, b: Str) -> Str = a ++ b")
    assert rendered.startswith(
        "guess.rvl:1: `++` is not an operator in revl — string concatenation "
        "is `+` (`a + b`) or `a.concat(b)`")
    assert "both operands are `Str`" in rendered
    assert "`a.concat(b)` is the method form" in rendered


def test_a_string_and_a_non_string_are_told_how_to_join_them():
    """`"total: " ++ n` is the other shape string building takes, and the
    increment wording answers it with a mutation the author never asked for."""
    rendered = _refusal('fn f(n: Int) -> Str = "total: " ++ n')
    assert "string concatenation is `+` (`a + b`) or `a.concat(b)`" in rendered
    assert "`+` joins `Str` to `Str`" in rendered
    assert "`.to_str()`" in rendered


def test_an_untyped_other_operand_is_not_named_as_a_type():
    """An operand whose type is not known yet (`nope ++ s`) must not put a
    literal `None` in the message: the redirect has to read as English."""
    rendered = _refusal("fn f(s: Str) -> Str = nope ++ s")
    assert "None" not in rendered
    assert "the other operand is not a `Str`" in rendered


@pytest.mark.parametrize("source", [
    "fn f(a: Str, b: Str) -> Str = a ++ b",
    'fn f(n: Int) -> Str = "total: " ++ n',
    "fn f(n: Int, s: Str) -> Str = n ++ s",
])
def test_the_refusal_names_the_idiom_that_answers_it(source):
    """The loop the issue measured is closed inside the refusal: the author is
    told the name of the idiom that teaches the construct, and the next call
    (`test_the_named_idiom_is_served`) resolves it."""
    assert "`revl_idiom` serves the `str-" in _refusal(source)


def test_every_form_the_refusal_names_compiles():
    """The redirect may only name spellings that work, in the position the
    author was refused in."""
    assert compile_source('fn f(a: Str, b: Str) -> Str = a + b', "ok.rvl")
    assert compile_source('fn f(a: Str, b: Str) -> Str = a.concat(b)', "ok.rvl")
    assert compile_source('fn f(n: Int) -> Str = "total: " + n.to_str()', "ok.rvl")
    assert compile_source('fn f(n: Int) -> Str = `total: ${n}`', "ok.rvl")


# ---- 3. the refusal holds in every position, not just a module `fn` -----------

@pytest.mark.parametrize("operand,expected", [
    ('"a" ++ "b"', "both operands are `Str`"),
    ("1 ++ 2", "revl has no `++` increment operator"),
])
def test_a_provide_method_body_cannot_lower_a_foreign_operator(operand, expected):
    """The position a `fn`-only guard misses. A provide-method body is lowered
    to IR and typed by a second stratum, so a `++` node the checker does not
    type is handed to the emitter as an IR `bin` with `op: "++"` — a foreign
    operator that COMPILED. Both operand shapes are refused here instead."""
    source = (
        "type Row = { id: Int, name: Str }\n"
        "service S { fn report() -> Row }\n"
        "component C provides s: S {\n"
        "  provide s {\n"
        "    fn report() = { id: 1, name: %s }\n"
        "  }\n"
        "}\n" % operand)
    rendered = _refusal(source)
    assert rendered.startswith("guess.rvl:5: "), rendered
    assert expected in rendered


# ---- 4. the surface the refusal points at answers -----------------------------

def test_the_named_idiom_is_served():
    for name in ("str-concat", "str-format"):
        assert name in idioms.names()
        result = _idiom(name)
        assert result.get("isError") is not True, result
        assert result["structuredContent"]["idiom"]["name"] == name


def test_the_concatenation_idiom_shows_both_spellings():
    idiom = _idiom("str-concat")["structuredContent"]["idiom"]
    assert idiom["exampleExpression"] == '"hi " + name'
    assert " + " in idiom["example"] and ".concat(" in idiom["example"]
    assert any("no `++` operator" in rule for rule in idiom["rules"])


def test_the_served_idioms_compile_as_served():
    for name in ("str-concat", "str-format"):
        assert compile_source(idioms.get(name)["example"], f"{name}.rvl")


def test_the_idiom_list_is_where_an_author_looking_for_concat_lands():
    """The issue measured `revl_idiom {name: "concat"}` -> "no such idiom".
    The name is still `str-concat`, but the refusal now lists the names, so the
    guess costs one call and returns the idiom."""
    response = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": "revl_idiom",
                                  "arguments": {"name": "concat"}}})
    result = response["result"]
    assert result["isError"] is True
    message = result["structuredContent"]["diagnostics"][0]["message"]
    assert "no idiom named 'concat'" in message
    assert "str-concat" in message
