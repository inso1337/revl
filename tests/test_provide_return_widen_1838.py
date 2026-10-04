"""A provide method that returns an Int as its declared Float carries the
widen marker (issue #1838).

`_mark_widen` marks an implicit Int to Float coercion so every tier can emit
the conversion (docs/arithmetic.md). A pure fn's `return` was marked; a
provide method's was not, so the go tier emitted `return n` from a `float64`
method and `go build` refused it. The same unmarked return broke rust
(`mismatched types`) and TypeScript (`bigint` is not assignable to `number`);
java and python absorb the conversion, and wasm refuses `Float` here.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

from revl.compiler import compile_source  # noqa: E402

HEAD = "service S { fn half(n: Int) -> Float }\n"
BODIES = {
    "expression body": "fn half(n) = n",
    "block body": "fn half(n) { return n }",
    "one branch": "fn half(n) { if (n > 0) { return n } return 0.5 }",
}


def _component(body: str) -> str:
    return HEAD + "component C provides s: S { provide s { " + body + " } }\n"


def _find_widen(ir: dict) -> list:
    found = []

    def walk(node):
        if isinstance(node, dict):
            if node.get("widen"):
                found.append(node)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    for comp in ir["components"]:
        walk(comp)
    return found


@pytest.mark.parametrize("body", list(BODIES.values()), ids=list(BODIES))
def test_the_int_returned_as_float_is_marked(body):
    widened = _find_widen(compile_source(_component(body), "w.rvl"))
    assert [n.get("widen") for n in widened] == ["Float"]
    assert widened[0].get("kind") in ("name", "var")


def test_a_float_or_int_return_is_not_marked():
    same = ("service S { fn f(n: Int) -> Int fn g(x: Float) -> Float }\n"
            "component C provides s: S { provide s { fn f(n) = n fn g(x) = x } }\n")
    assert _find_widen(compile_source(same, "w.rvl")) == []


def _check(tier: str, body: str) -> tuple:
    import conformance  # noqa: PLC0415
    from validate import VALIDATORS  # noqa: PLC0415

    validator = VALIDATORS[tier]
    reason = validator.unavailable()
    if reason:
        pytest.skip(f"{tier}: {reason}")
    ir = compile_source(_component(body), "w.rvl")
    art = conformance.emitter(tier).emit(
        ir, **conformance._emit_kwargs(tier, 1838))
    return validator.check([("widen", art)])["widen"]


@pytest.mark.parametrize("tier", ["go", "rust", "typescript", "java"])
@pytest.mark.parametrize("body", list(BODIES.values()), ids=list(BODIES))
def test_every_typed_tier_builds_the_widened_return(tier, body):
    status, detail = _check(tier, body)
    assert status == "ok", detail
