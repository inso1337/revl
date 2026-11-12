"""A long chain of one operator loads and runs on the python tier (issue #3403).

`a + b + c + ...` lowers to a left-deep tree. The python emitter used to give
every operand one more bracket level, so a chain of ~100 bounded `+` was
accepted by the compiler and then refused at load by CPython's own parser
(`MemoryError: Parser stack overflowed`), and at 1000 operands the emitter's
own IR walkers raised `RecursionError` before any text was written. The
language states no bound on a chain, so neither may this tier's text.

Each case compiles a 1000-operand chain from source, emits it, loads the
module with the host's `compile` and calls the function. The chain's bracket
depth is pinned too: it must not grow with the chain.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for p in (ROOT / "src", ROOT / "tests"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from _backend_import import backend_emitter  # noqa: E402
from revl import compile_source  # noqa: E402

N = 1000

CASES = {
    # bounded Int arithmetic: every step is range-checked inline
    "int_plus": ("Int", "1", "+", N + 1),
    # plain Str concat
    "str_concat": ("Str", '"a"', "+", "a" * (N + 1)),
    # `&&` keeps its short-circuit: all true, so every operand is read
    "bool_and": ("Bool", "true", "&&", True),
}


def _bracket_depth(text: str) -> int:
    depth = deepest = 0
    for ch in text:
        if ch in "([{":
            depth += 1
            deepest = max(deepest, depth)
        elif ch in ")]}":
            depth -= 1
    return deepest


def _chain_module(ret: str, lit: str, op: str, n: int) -> tuple[str, dict]:
    src = f"fn chain() -> {ret} = " + f"{lit} {op} " * n + lit + "\n"
    text = backend_emitter("python").emit(compile_source(src, "chain_3403.rvl"))
    namespace: dict = {}
    exec(compile(text, "chain_3403.py", "exec"), namespace)
    return text, namespace


def _chain_body(text: str) -> str:
    lines = text.splitlines()
    at = next(i for i, line in enumerate(lines) if line.startswith("def chain("))
    return lines[at + 1]


@pytest.mark.parametrize("case", sorted(CASES))
def test_a_1000_operand_chain_loads_and_runs(case):
    ret, lit, op, want = CASES[case]
    text, namespace = _chain_module(ret, lit, op, N)
    assert namespace["chain"]() == want


@pytest.mark.parametrize("case", sorted(CASES))
def test_the_chain_bracket_depth_does_not_grow_with_the_chain(case):
    ret, lit, op, _ = CASES[case]
    short = _bracket_depth(_chain_body(_chain_module(ret, lit, op, 40)[0]))
    long = _bracket_depth(_chain_body(_chain_module(ret, lit, op, 200)[0]))
    assert long == short, (case, short, long)


def test_a_bounded_chain_still_traps_at_the_overflowing_step():
    """The flat Int chain checks every intermediate, as the nested one did:
    `MAX + 1 - 1 ...` overflows at the first step and must fault, though the
    whole chain's value would be back in range."""
    src = ("fn chain(a: Int) -> Int = a + 1" + " - 1" * 20 + "\n")
    text = backend_emitter("python").emit(compile_source(src, "trap_3403.rvl"))
    namespace: dict = {}
    exec(compile(text, "trap_3403.py", "exec"), namespace)
    assert namespace["chain"](5) == 5 + 1 - 20
    with pytest.raises(Exception) as fault:
        namespace["chain"](2**63 - 1)
    assert "overflow" in str(fault.value).lower()
