"""Issue #2187: a stray `=` after a method signature must name the `=`.

`fn go(x) = { let k = x ... }` used to parse the `{` as a record literal, so
the body's first `let` reported as `expected ident, found 'let'` with the
reserved-keyword "pick another name" hint — advice that is false about the
language (`let` is legal in `fn` bodies) and unactionable (no rename can work
in field position). The parser now refuses the shape at the `=` with a
diagnostic that names the `=`, mirroring `_arrow_body`'s treatment of the
identical failure class for closures.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import RevlError  # noqa: E402
from revl.parser import Parser  # noqa: E402


def parse(src: str):
    return Parser(src, "<test>").parse()


def provide_method(body_src: str) -> str:
    return (
        "service S { fn go(x: Str) -> Str }\n"
        "component C provides s: S {\n"
        "  provide s {\n"
        f"    fn go(x) = {body_src}\n"
        "  }\n"
        "}\n"
    )


def provide_method_ast(src: str):
    prog = parse(src)
    comp = next(c for c in prog.components if c.name == "C")
    return next(
        s for s in comp.body if type(s).__name__ == "ProvideStmt"
    ).methods[0]


def test_stray_equals_names_the_equals_not_the_keyword():
    """The issue's repro: the diagnostic blames the `=`, not the `let`."""
    with pytest.raises(RevlError) as exc:
        parse(provide_method("{\n      let k = x\n      k\n    }"))
    assert exc.value.message == (
        "a body written with `=` takes a single expression, not statements"
    )
    assert "drop the `=`" in (exc.value.hint or "")
    # the old failure mode is gone: no rename is prescribed
    assert "reserved keyword" not in (exc.value.hint or "")
    assert "pick another name" not in (exc.value.hint or "")
    # pointed at the `=` (line 4), not at the `let` (line 5)
    assert exc.value.line == 4


@pytest.mark.parametrize("kw", ["let", "var", "effect", "return", "while", "for"])
def test_stray_equals_fires_for_any_statement_keyword(kw):
    """Whatever statement the author tries first, the `=` is named."""
    stmt = {"let": "let k = x", "var": "var k = x",
            "effect": "effect x.length()", "return": "return x",
            "while": "while true { }", "for": "for (c of x) { }"}[kw]
    with pytest.raises(RevlError) as exc:
        parse(provide_method("{\n      " + stmt + "\n    }"))
    assert "takes a single expression, not statements" in exc.value.message
    assert f"`{kw}` was read as a field name" in (exc.value.hint or "")


def test_stray_equals_top_level_fn():
    """The same `=` sugar on a top-level `fn` gets the same diagnostic."""
    with pytest.raises(RevlError) as exc:
        parse("fn f(x: Str) -> Str = {\n  let k = x\n  k\n}\n")
    assert "takes a single expression, not statements" in exc.value.message
    assert "drop the `=`" in (exc.value.hint or "")
    assert "`fn f(...) { ... }`" in (exc.value.hint or "")


def test_genuine_record_literal_after_equals_still_parses():
    """`= { k: x }` is a real record literal — untouched by the refusal."""
    src = (
        "type R = { k: Str }\n"
        "service S { fn go(x: Str) -> R }\n"
        "component C provides s: S {\n"
        "  provide s {\n"
        "    fn go(x) = { k: x }\n"
        "  }\n"
        "}\n"
    )
    assert len(provide_method_ast(src).body) == 1


def test_empty_record_after_equals_still_parses():
    parse("type R = { k: Str }\nfn f() -> R = { k: \"v\" }\n")


def test_record_update_after_equals_still_parses():
    parse("type R = { k: Str }\nfn f(r: R) -> R = { r | k = \"v\" }\n")


def test_block_body_without_equals_still_parses():
    """The correct form from the issue compiles clean."""
    method = provide_method_ast(
        "service S { fn go(x: Str) -> Str }\n"
        "component C provides s: S {\n"
        "  provide s {\n"
        "    fn go(x) {\n"
        "      let k = x\n"
        "      return k\n"
        "    }\n"
        "  }\n"
        "}\n"
    )
    assert len(method.body) == 2
