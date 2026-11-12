"""A long chain of operators (issues #3400 and #3399).

One input shape, two passes that met it:

* #3400: lowering asked `infer_ast` for the type of each operand after
  lowering it, and each answer walked the operand's whole subtree, so a chain
  of n operators cost about n*n/2 inferences (3000 `+` took over a minute and
  a half). The count is pinned, not the time: a chain of n operators costs a
  number of `infer_ast` calls linear in n.
* #3399: the admission gate's hole walk (`holes.collect`) recursed once per IR
  level, and a chain is one level per operator, so a text the bare compile
  accepts raised a raw `RecursionError` when compiled against a manifest. The
  four texts of the issue now give the same answer both ways.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import RevlError, compile_source  # noqa: E402
import revl.lower as lower  # noqa: E402
import revl.typecheck as typecheck  # noqa: E402
from revl.parser import recursion_headroom  # noqa: E402


def _chain(n: int) -> str:
    return "fn odd() -> Int = " + "1 + " * n + "1\n"


def _count_infer_ast(monkeypatch, src: str) -> int:
    calls = [0]
    original = typecheck.infer_ast

    def counting(*args, **kwargs):
        calls[0] += 1
        return original(*args, **kwargs)

    # `infer_ast` recurses through the module global, and lower.py holds its
    # own imported name: patch both so every call is counted.
    monkeypatch.setattr(typecheck, "infer_ast", counting)
    monkeypatch.setattr(lower, "infer_ast", counting)
    compile_source(src, "g.rvl")
    return calls[0]


@pytest.mark.parametrize("src_of", [
    _chain,
    lambda n: "fn odd() -> Int = " + "-" * n + "1\n",
], ids=["binary", "unary"])
def test_operator_chain_costs_linear_inferences(monkeypatch, src_of):
    n = 300
    small = _count_infer_ast(monkeypatch, src_of(n))
    large = _count_infer_ast(monkeypatch, src_of(2 * n))
    # base: 300 `+` cost 91502 calls, 600 cost 363002 (the 4x of a square)
    assert small <= 10 * n, small
    assert large <= 2 * small + 20, (small, large)


def test_memo_keeps_answers_only_inside_its_scope():
    from revl.parser import ExprBin, ExprLit
    node = ExprBin(op="+", left=ExprLit(value=1, line=1),
                   right=ExprLit(value=2, line=1), line=1)
    assert not typecheck.infer_memo_active()
    with typecheck.infer_memo():
        assert typecheck.infer_memo_active()
        with typecheck.infer_memo():  # re-entrant: shares the outer memo
            assert typecheck.infer_ast(node, {}, {}) == "Int"
        assert typecheck.infer_memo_active()
    assert not typecheck.infer_memo_active()


_BASE = "fn odd() -> Int = 1\n"

# The four texts of issue #3399, in its table order. neg-400 and neg-700 are
# the table's baselines: they compiled both ways before the fix too. neg-1000
# and add-1000 raised RecursionError against a manifest.
_TEXTS_3399 = {
    "neg-400": "-" * 400 + "1",
    "neg-700": "-" * 700 + "1",
    "neg-1000": "-" * 1000 + "1",
    "add-1000": "1 + " * 1000 + "1",
}


@pytest.fixture
def default_recursion_limit():
    # the issue measured python's default limit of 1000
    previous = sys.getrecursionlimit()
    sys.setrecursionlimit(1000)
    try:
        yield
    finally:
        sys.setrecursionlimit(previous)


@pytest.mark.parametrize("name", list(_TEXTS_3399))
def test_chain_compiles_bare_and_against_a_manifest(name, default_recursion_limit):
    src = "fn odd() -> Int = " + _TEXTS_3399[name] + "\n"
    base = compile_source(_BASE, "g.rvl")
    bare = compile_source(src, "g.rvl")
    admitted = compile_source(src, "g.rvl", manifest=base)
    # comparing ~1000-deep dicts counts against the limit on python 3.11, so
    # the test's own check runs with the compiler's headroom
    with recursion_headroom():
        assert bare["functions"] == admitted["functions"]


def test_hole_at_the_bottom_of_a_deep_chain_is_still_refused(default_recursion_limit):
    base = compile_source(_BASE, "g.rvl")
    # the hole's expected type comes from `id`'s declared parameter
    src = ("fn id(n: Int) -> Int = n\n"
           "fn odd() -> Int = " + "1 + " * 1000 + 'id(hole "the last term")\n')
    with pytest.raises(RevlError) as excinfo:
        compile_source(src, "g.rvl", manifest=base)
    assert "admission refused" in str(excinfo.value)
