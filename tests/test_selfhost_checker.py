"""The self-hosted expression type-checker slice (selfhost/checker.rvl),
compiled by revl, emitted through the python backend, executed, and
cross-checked against the reference checker (src/revl/typecheck.py's
`infer_ast`) on both *verdict* and *inferred type*.

This is the second half of a differential oracle, in the exact shape of
tests/test_selfhost_parser.py: two independent implementations of one
operator-typing algebra are forced to agree on every input, so a
disagreement is always a real defect in one of them.

Agreement is checked three ways:
  * accepted input -> identical inferred-type strings ("?" stands for the
    reference's None, the gradual frontier's unknown);
  * refused input  -> both refuse (revl's pure stratum has no exceptions,
    so the reference *raising* RevlError and the selfhost checker returning
    "refuse" is the agreement; the messages are not compared);
  * a fuzz corpus of random binop expressions over the same fixed
    environment, where the two checkers are each other's oracle including
    on the inputs the generator makes ill-typed by accident.

Slice: literal typing, + - * / % and the comparison families, over the
five-binding environment ENV below (mirrored inside checker.rvl's
base_env). Everything else the grammar allows is out of slice on both
sides of the corpus.
"""

import importlib.util
import random
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_files  # noqa: E402
import revl.parser as refparser  # noqa: E402
from revl.errors import RevlError  # noqa: E402
from revl.typecheck import infer_ast  # noqa: E402


# ---------------------------------------------------------------- harness

def _exec_emitted() -> dict:
    ir = compile_files([str(ROOT / "selfhost" / "checker.rvl")])
    assert ir["ir_version"] == 3
    spec = importlib.util.spec_from_file_location(
        "pyemit_selfhost_checker", ROOT / "backends" / "python" / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    stub = types.ModuleType("runtime")
    stub.__getattr__ = lambda name: (lambda *a, **k: None)  # PEP 562
    had = "runtime" in sys.modules
    previous = sys.modules.get("runtime")
    sys.modules["runtime"] = stub
    try:
        namespace = {}
        exec(compile(module.emit(ir), "selfhost_checker.py", "exec"), namespace)
    finally:
        if had:
            sys.modules["runtime"] = previous
        else:
            del sys.modules["runtime"]
    return namespace


@pytest.fixture(scope="module")
def infer_src():
    return _exec_emitted()["infer_expr_str"]


# ------------------------------------------------- reference inferencer

# Mirrored by base_env() in selfhost/checker.rvl. Keep the two in lockstep.
ENV = {"x": "Int", "y": "Int", "f": "Float", "s": "Str", "flag": "Bool"}


def _ref_parse(src: str):
    parser = refparser.Parser(src, "diff.rvl")
    node = parser.pure_expr()
    if not parser.at("eof"):
        raise RevlError("diff.rvl", 1, "trailing tokens")
    return node


def _ref_infer(src: str) -> str:
    """The reference verdict+type, rendered in the selfhost checker's
    vocabulary: "refuse" where infer_ast raises, "?" where it returns
    None, else the type's spelling."""
    try:
        node = _ref_parse(src)
        t = infer_ast(node, dict(ENV), {}, filename="diff.rvl")
    except RevlError:
        return "refuse"
    return t if t else "?"


def _agree(infer_src, src: str) -> None:
    want = _ref_infer(src)
    got = infer_src(src)
    assert got == want, f"{src!r}: selfhost {got!r} != reference {want!r}"


# ---------------------------------------------------------------- corpus

ACCEPTED = [
    # literals and variables
    "1", "0", "true", "false", "x", "y", "f", "s", "flag",
    "q",  # not in the environment: the gradual frontier's unknown
    # arithmetic
    "1 + 2", "x - y", "x * 2", "7 / 2", "x % 3",
    "f + 1", "1 + f", "f - f", "f * f", "f / f", "f % 2", "x / f", "x % f",
    "(1 + 2) * x", "x - y - z", "7 / 2 / 2",
    # string concatenation
    "s + s", "s + 1", "1 + s", "s + f", "s + q",
    # ordering (Str orders too)
    "x < y", "x <= f", "f > 1", "f >= x", "s < s", "s <= q",
    # equality
    "x == y", "x != y", "s == s", "flag == false", "flag != true",
    "q == x", "q != q", "x === y", "x !== y",  # === canonicalizes to ==
    # nesting across the families
    "1 + 2 < 4", "x < y == true", "(x == y) == (flag == false)",
    "1 < 2 == s < s",
]

REJECTED = [
    # Bool in arithmetic (refusal-parity: `/` on Bool is the headline case)
    "flag / flag", "flag + 1", "true * false", "x + flag", "1 % flag",
    "flag / x", "x - true",
    # Str in arithmetic (only `+` takes a Str, and only beside Str/numeric)
    "s * s", "s - s", "1 % s", "s / s", "s * 2", "2 / s",
    # Bool / Str under ordering
    "flag < 1", "x < true", "flag <= flag", "true > false",
    # equality across incompatible types
    "x == s", "s == x", "flag == x", "1 == flag", "s != 2",
    # null has no type (absence is Opt[T])
    "null", "null + 1", "1 + null", "null == null",
]


@pytest.mark.parametrize("src", ACCEPTED)
def test_accepted_expressions_agree(infer_src, src):
    assert _ref_infer(src) != "refuse", f"corpus bug: reference refuses {src!r}"
    assert _ref_infer(src) != "(bad)", f"corpus bug: reference rejects {src!r}"
    _agree(infer_src, src)


@pytest.mark.parametrize("src", REJECTED)
def test_rejected_expressions_agree(infer_src, src):
    assert _ref_infer(src) == "refuse", f"corpus bug: reference accepts {src!r}"
    assert infer_src(src) == "refuse", f"selfhost accepted {src!r}"


# ---------------------------------------------------------------- fuzz

ATOMS = ["1", "0", "7", "x", "y", "f", "s", "flag", "q", "true", "false"]
BINOPS = ["+", "-", "*", "/", "%", "<", "<=", ">", ">=", "==", "!="]


def _gen(rng: random.Random, depth: int) -> str:
    if depth <= 0:
        return rng.choice(ATOMS)
    roll = rng.random()

    def g():
        return _gen(rng, depth - 1)

    if roll < 0.75:
        return f"{g()} {rng.choice(BINOPS)} {g()}"
    return f"({g()})"


@pytest.mark.parametrize("seed", range(12))
def test_generated_expressions_agree(infer_src, seed):
    """Random binop expressions over the whole slice. Nothing here is a
    fixed oracle — the two checkers are each other's oracle, including on
    the inputs the generator makes ill-typed by accident (the Str-beside-
    arithmetic and Bool-operand mixes especially), where agreeing to
    *refuse* is the property under test."""
    rng = random.Random(seed)
    for _ in range(60):
        _agree(infer_src, _gen(rng, rng.randint(1, 4)))


