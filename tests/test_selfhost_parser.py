"""The self-hosted expression parser (selfhost/parser.rvl, syntax-2.0 §3.2):
compiled by revl, emitted through the python backend, executed, and
cross-checked against the reference parser (src/revl/parser.py) on both
*shape* and *rejection*.

This is the second half of a differential oracle. Two independent
implementations of one grammar are forced to agree on every input, which
is the cheapest way there is to find a precedence, associativity or
lookahead bug: neither implementation is the spec, so a disagreement is
always a real defect in one of them.

Agreement is checked two ways:
  * accepted input -> identical canonical S-expressions;
  * rejected input -> both reject (revl's pure stratum has no exceptions,
    so the reference *raising* and the revl parser returning `Bad` is the
    agreement; the messages are not compared).
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


# ---------------------------------------------------------------- harness

def _exec_emitted() -> dict:
    ir = compile_files([str(ROOT / "selfhost" / "parser.rvl")])
    assert ir["ir_version"] == 3
    spec = importlib.util.spec_from_file_location(
        "pyemit_selfhost_parser", ROOT / "backends" / "python" / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    stub = types.ModuleType("runtime")
    stub.__getattr__ = lambda name: (lambda *a, **k: None)  # PEP 562
    had = "runtime" in sys.modules
    previous = sys.modules.get("runtime")
    sys.modules["runtime"] = stub
    try:
        namespace = {}
        exec(compile(module.emit(ir), "selfhost_parser.py", "exec"), namespace)
    finally:
        if had:
            sys.modules["runtime"] = previous
        else:
            del sys.modules["runtime"]
    return namespace


@pytest.fixture(scope="module")
def parse_render():
    return _exec_emitted()["parse_render"]


# ------------------------------------------------- reference -> S-expression

def _ref_parse(src: str):
    """Parse one expression, requiring it to consume the whole input."""
    parser = refparser.Parser(src, "diff.rvl")
    node = parser.pure_expr()
    if not parser.at("eof"):
        raise RevlError("diff.rvl", 1, "trailing tokens")
    return node


def _seq(nodes) -> str:
    return " ".join(_render(n) for n in nodes)


def _render(n) -> str:
    P = refparser
    if isinstance(n, P.ExprLit):
        v = n.value
        if v is None:
            return "(null)"
        if v is True:
            return "(bool true)"
        if v is False:
            return "(bool false)"
        if isinstance(v, int):
            return f"(int {v})"
        if isinstance(v, float):
            return f"(float {v})"
        return f"(str {v})"
    if isinstance(n, P.ExprVar):
        return f"(var {n.name})"
    if isinstance(n, P.ExprBin):
        return f"(bin {n.op} {_render(n.left)} {_render(n.right)})"
    if isinstance(n, P.ExprUn):
        return f"(un {n.op} {_render(n.operand)})"
    if isinstance(n, P.EmitExpr):
        return f"(emit {_render(n.expr)})"
    if isinstance(n, P.ExprCall):
        return f"(call {_render(n.callee)} {_seq(n.args)})"
    if isinstance(n, P.ExprField):
        return f"(field {_render(n.target)} {n.name})"
    if isinstance(n, P.ExprOptField):
        return f"(optfield {_render(n.target)} {n.name})"
    if isinstance(n, P.ExprOptCall):
        return f"(optcall {_render(n.target)} {n.method} {_seq(n.args)})"
    if isinstance(n, P.ExprIndex):
        return f"(index {_render(n.target)} {_render(n.index)})"
    if isinstance(n, P.ExprIf):
        return f"(if {_render(n.cond)} {_render(n.then)} {_render(n.otherwise)})"
    if isinstance(n, P.ExprRecord):
        return "(rec " + " ".join(f"(f {k} {_render(v)})" for k, v in n.fields) + ")"
    if isinstance(n, P.ExprList):
        return f"(list {_seq(n.items)})"
    if isinstance(n, P.ExprArrow):
        params = " ".join(
            f"(p {name} {ty if ty else '_'})"
            for name, ty in zip(n.params, n.param_types))
        return f"(arrow {params} {_render(n.body)})"
    if isinstance(n, P.ExprMatch):
        arms = " ".join(
            f"(arm {pat} {bind if bind else '_'} {_render(body)})"
            for pat, bind, body in n.arms)
        return f"(match {_render(n.scrutinee)} {arms})"
    if isinstance(n, P.Interp):
        return "(templ " + " ".join(
            f"(t {v})" if k == "text" else f"(e {_render(v)})"
            for k, v in n.parts) + ")"
    if isinstance(n, P.ExprHole):
        return f"(hole {n.type or '_'} {n.message or '_'})"
    raise AssertionError(f"no renderer for {type(n).__name__}")


def _reference(src: str) -> str:
    """`(bad)` when the reference rejects — the shape revl produces."""
    try:
        return _render(_ref_parse(src))
    except RevlError:
        return "(bad)"


def _agree(parse_render, src: str) -> None:
    want = _reference(src)
    got = parse_render(src)
    assert got == want, f"\n  source: {src!r}\n  reference: {want}\n  selfhost : {got}"


# ---------------------------------------------------------------- corpus

ACCEPTED = [
    # float literals (canonical decimal spellings only — see the render
    # comment in parser.rvl about exponent/trailing-zero normalization)
    "2.5", "0.5", "3.0", "2.5 + 1", "2.5 * 0.5 < 2.0",
    "1", '"hi"', "true", "false", "null", "x", "config.retries",
    "1 + 2 * 3", "1 * 2 + 3", "(1 + 2) * 3", "a - b - c", "a - (b - c)",
    "a / b % c", "-x", "!ok", "!!ok", "- - 1",
    "a < b == c > d", "a <= b != c >= d", "a === b", "a !== b",
    "a && b || c", "a || b && c", "a ?? b ?? c", "(a ?? b) || c",
    "a || (b ?? c)", "a ? b : c", "a ? b : c ? d : e", "a ? b ? c : d : e",
    "f()", "f(1)", "f(1, 2)", "f(1,)", "f(g(h(1)))",
    "a.b.c", "a.b(1).c", "xs[0]", "xs[i + 1]", "f(1)[2].g",
    "a?.b", "a?.b?.c", "a?.b(1)", "a?.b(1)?.c", "a?.b()",
    "match e { }",
    "{}", "{ a: 1 }", "{ a: 1, b: 2 }", "{ a: 1, b: { c: 2 } }",
    "[]", "[1]", "[1, 2, 3]", "[[1], [2]]", "[{ a: 1 }]",
    "x => x", "x => x + 1", "() => 1", "(a) => a", "(a, b) => a + b",
    "(a: Int) => a", "(a: Int, b) => a + b", "(a: List[Str]) => a",
    "(f: (Int) -> Bool) => f", "(a: Str?) => a",
    "emit db.write(1)", "emit f(1) + 2",
    "match e { Ok(v) => v, _ => 0, }",
    "match e { Ok(v) => v, _ => 0 }",
    "match e { None => 1, Some(x) => x, }",
    "match f(1) { A(x) => x + 1, B(y) => y * 2, _ => 0, }",
    "`plain`", "`hi ${name}`", "`n=${r.count}!`", "`${a + b}`",
    "`${f(1, 2)}`", "`a${x}b${y}c`",
    # the "|" join separator, and its escape character, inside part payloads
    "`${a || b}`", "`a|b`", "`100%`", "`%p`", "`${a || b}|${c}`",
    "`p${`inner ${x}`}q`",
    "hole", 'hole "why"', "hole[Int]", 'hole[List[Str]] "todo"',
    "a.b ?? c.d", "xs[0] ?? 1", "f(a ?? b)", "[a ?? b]", "{ k: a ?? b }",
    "(a && b) ?? c", "a ?? (b && c)",
    "x => y => x + y", "f(x => x + 1)", "[x => x, y => y]",
]

REJECTED = [
    "", "1 +", "(1", "(1))", "f(", "f(1,", "[1", "{ a: }", "{ a }",
    "a ?? b || c", "a || b ?? c", "a ?? b && c", "a && b ?? c",
    "a?.b.c", "a?.b[0]", "a?.b.c?.d",
    "a ? b", "a ? b : ", "match e { Ok => }",
    "match e {", "1 2", "a b", ".x", "=> x", "*", ")",
    "hole[", "hole[]", "`${}`", "(a: ) => a", "((a, b)) => a",
]


@pytest.mark.parametrize("src", ACCEPTED)
def test_accepted_expressions_agree(parse_render, src):
    assert _reference(src) != "(bad)", f"corpus bug: reference rejects {src!r}"
    _agree(parse_render, src)


@pytest.mark.parametrize("src", REJECTED)
def test_rejected_expressions_agree(parse_render, src):
    assert _reference(src) == "(bad)", f"corpus bug: reference accepts {src!r}"
    assert parse_render(src) == "(bad)", f"selfhost accepted {src!r}"


# ---------------------------------------------------------------- fuzz

ATOMS = [
    "1", "2.5", "x", '"s"', "true", "null", "hole", 'hole "w"', "hole[Int]",
    "hole[List[Str]]", "config.k", "`t`", "`a${x}b`", "`${x + 1}`",
    "f(1)", "a.b", "xs[0]", "{a: 1}", "[1]", "z => z", "(a: Int) => a",
    "(f: (Int) -> Bool) => f", "(a: Str?) => a", "emit g(1)",
    # payloads that collide with the lexer's part encoding
    "`${a || b}`", "`100%`", "`a|b`",
]
BINOPS = ["+", "-", "*", "/", "%", "<", ">", "<=", ">=", "==", "===",
          "!=", "!==", "&&", "||", "??"]


def _gen(rng: random.Random, depth: int) -> str:
    if depth <= 0:
        return rng.choice(ATOMS)
    roll = rng.random()

    def g():
        return _gen(rng, depth - 1)

    if roll < 0.30:
        return f"{g()} {rng.choice(BINOPS)} {g()}"
    if roll < 0.40:
        return f"({g()})"
    if roll < 0.47:
        return f"{rng.choice(['!', '-'])}{g()}"
    if roll < 0.55:
        return f"{g()} ? {g()} : {g()}"
    if roll < 0.62:
        return f"f({g()}, {g()})"
    if roll < 0.68:
        return f"[{g()}, {g()},]"
    if roll < 0.74:
        return f"{{ k: {g()}, j: {g()} }}"
    if roll < 0.79:
        return f"match e {{ Ok(v) => {g()}, _ => {g()}, }}"
    if roll < 0.84:
        return f"({g()}).b"
    if roll < 0.88:
        return "a?.b?.c"
    if roll < 0.92:
        return f"({g()})[0]"
    if roll < 0.96:
        return f"`p${{{g()}}}q`"
    return f"emit {g()}"


@pytest.mark.parametrize("seed", range(12))
def test_generated_expressions_agree(parse_render, seed):
    """Random expressions over the whole grammar. Nothing here is a fixed
    oracle — the two parsers are each other's oracle, including on the
    inputs the generator makes ill-formed by accident (the `??`/`&&` mixes
    especially), where agreeing to *reject* is the property under test.

    This is what found the template-part encoding bug: nesting a template
    inside an interpolation, or writing `${a || b}`, put the "|" join
    separator into a payload. It showed up about once in 400 expressions
    and was invisible to every hand-written case.
    """
    rng = random.Random(seed)
    for _ in range(60):
        _agree(parse_render, _gen(rng, rng.randint(1, 4)))
