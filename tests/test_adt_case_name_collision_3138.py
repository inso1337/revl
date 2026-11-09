"""A module-scope declaration may not reuse an ADT case name (issue #3138).

The checker admitted a module `fn` named like an ADT case and resolved `Hit(5)`
to the constructor, so the enclosing function's result was typed `T`. The
python emitter then wrote both spellings as module-scope bindings of one name,
`class Hit(T)` first and `def Hit(x)` after it. The `def` replaced the class,
so importing the emitted module and calling `mk()` returned `1005` although the
checker promised a `T`; the next `match` on the value raised
`TypeError: isinstance() arg 2 must be a type`. The ts tier emitted two
`export function Hit` and failed to load with `SyntaxError: Identifier 'Hit'
has already been declared`.

The frontend now refuses the ambiguous name instead of emitting a module where
one name means two things. The same rule covers every declaration that becomes
a module-scope binding: `fn`, `extern`, `component`, `service` and `type`.
A `component` named like a case is the loud version of the same shape
(`Hit = {...}` replaces the class, so `isinstance(x, Hit)` raises), and a
record `type` beside a case emits two `class Hit` definitions.

The merged-program form of the collision (a case in one module and a `fn` or
`type` of that name in another) is already refused by the `use`-edge check
(issue #1145, tests/test_module_privates.py); this file pins the single-file
form, which the merge check deliberately leaves to the frontend.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.compiler import compile_source  # noqa: E402
from revl.diagnostics import classify  # noqa: E402
from revl.errors import RevlError  # noqa: E402

# The reproducer from issue #3138, verbatim.
REPRODUCER = """type T = Hit(Int) | Miss
fn Hit(x: Int) -> Int = x + 1000
pub fn mk() -> T = Hit(5)
pub fn kind(t: T) -> Str = match (t) {
  Hit(n) => "hit",
  Miss => "miss"
}
"""


def _refusal(source: str) -> dict:
    with pytest.raises(RevlError) as caught:
        compile_source(source, "m.rvl")
    return classify(caught.value)


def test_the_issues_reproducer_is_refused():
    d = _refusal(REPRODUCER)
    assert d["message"] == "`Hit` is already declared as a case of `T` on line 1"
    assert (d["code"], d["category"], d["line"]) == ("G6", "binding", 2)


def test_a_case_declared_after_the_fn_is_refused_at_the_case():
    """The refusal is at whichever of the two comes second, as the duplicate-fn
    and fn/extern refusals are, so the diagnostic never points backwards."""
    d = _refusal("fn Hit(x: Int) -> Int = x + 1000\n"
                 "type T = Hit(Int) | Miss\n")
    assert d["message"] == "`Hit` is already declared as a function on line 1"
    assert (d["code"], d["line"]) == ("G6", 2)


@pytest.mark.parametrize("declaration", [
    "extern pure fn Hit(x: Int) -> Int = @py { return x + 1000 }",
    "component Hit { }",
    "service Hit { fn go() -> Int }",
    "type Hit = { n: Int }",
], ids=["extern", "component", "service", "record type"])
def test_every_module_scope_binding_kind_is_refused(declaration):
    """A declaration of every kind the emitters place at module scope collides
    with a case constructor of the same name: the later definition replaces the
    constructor on the python tier, and two `export function`/`class` bindings
    break the ts tier or `isinstance`."""
    d = _refusal("type T = Hit(Int) | Miss\n" + declaration + "\n")
    assert d["message"] == "`Hit` is already declared as a case of `T` on line 1"
    assert (d["code"], d["category"], d["line"]) == ("G6", "binding", 2)


def test_distinct_names_still_compile():
    """Non-vacuity: the rule is about the NAME, not about the shape. Renaming
    the function admits the same program, and the case constructor keeps its
    typed result."""
    ir = compile_source(REPRODUCER.replace("fn Hit(", "fn make_hit("), "m.rvl")
    assert {fn["name"] for fn in ir["functions"]} == {"make_hit", "mk", "kind"}
    assert {case["name"] for case in ir["types"]["T"]["cases"]} == {"Hit", "Miss"}


def test_a_transparent_alias_is_not_a_case_name():
    """`type Sku = Str` is erased before the check runs (it has no constructor
    and emits no binding), so its right-hand side is not a case name and a
    declaration called `Str` keeps compiling."""
    ir = compile_source("type Sku = Str\n"
                        "fn Str(x: Sku) -> Sku = x\n"
                        "pub fn go() -> Sku = Str(\"a\")\n", "m.rvl")
    assert [fn["name"] for fn in ir["functions"]] == ["Str", "go"]
    assert "Sku" not in ir.get("types", {})


def test_a_type_may_name_its_own_case():
    """`type Tag = Tag(Str)` is the newtype spelling, not the collision: the
    type declaration and its own case are one declaration, and the python tier
    emits `class Tag:` before `class Tag(Tag):`, which resolves the base before
    the case rebinds the name (pinned by
    tests/test_2544_provide_param_shadows_module_fn.py). A declaration other
    than that own type still collides."""
    ir = compile_source("type Tag = Tag(Str)\n"
                        'pub fn mk() -> Tag = Tag("x")\n', "m.rvl")
    assert ir["types"]["Tag"]["kind"] == "variant"
    assert [case["name"] for case in ir["types"]["Tag"]["cases"]] == ["Tag"]
    with pytest.raises(RevlError) as caught:
        compile_source("type Tag = Tag(Str)\n"
                       "fn Tag(x: Str) -> Str = x\n", "m.rvl")
    assert classify(caught.value)["message"] == (
        "`Tag` is already declared as a case of `Tag` on line 1")
