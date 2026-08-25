"""item 196: `if`/`else` in EXPRESSION position — a block-bodied conditional
whose value is the taken branch's final expression.

`if (c) { a } else { b }` is the block-bodied twin of the ternary `c ? a : b`.
It is a pure front-end addition: the parser lowers it to the SAME `ExprIf` node
the ternary produces, so the type checker (branch-agreement) and every backend
emitter render it with zero new support. These tests pin that equivalence
(byte-identical emit across all six backends), the rejection rules (an
expression `if` needs an `else`, branches must agree in type), nesting, and that
statement-position `if` (no `else`, side-effecting body) keeps its semantics.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "backends" / "python"))

from _backend_import import backend_emitter  # noqa: E402
from revl import RevlError, compile_source  # noqa: E402

emit = backend_emitter("python")

# Every backend already renders the ternary's `ExprIf`; expression-if reuses it,
# so the emit output must match the ternary byte-for-byte on all of them.
BACKENDS = ["python", "typescript", "go", "java", "rust", "wasm"]


def _run_py(source, fn, *args):
    ns = {}
    exec(compile(emit.emit(compile_source(source)), "emitted.py", "exec"), ns)
    return ns[fn](*args)


def test_expression_if_binds_a_value():
    # the motivating shape: a value-producing conditional binds directly,
    # instead of `var x = default; if (c) { x = .. }`.
    src = """
    fn classify(n: Int) -> Str {
      let kw = if (n > 0) { "pos" } else { "nonpos" }
      return kw
    }
    """
    assert _run_py(src, "classify", 3) == "pos"
    assert _run_py(src, "classify", -1) == "nonpos"


def test_expression_if_in_return_position():
    src = 'fn classify(n: Int) -> Str { return if (n > 0) { "pos" } else { "nonpos" } }'
    assert _run_py(src, "classify", 5) == "pos"
    assert _run_py(src, "classify", 0) == "nonpos"


def test_nested_expression_if():
    src = """
    fn size(n: Int) -> Str {
      return if (n > 0) {
        if (n > 10) { "big" } else { "small" }
      } else {
        "neg"
      }
    }
    """
    assert _run_py(src, "size", 50) == "big"
    assert _run_py(src, "size", 3) == "small"
    assert _run_py(src, "size", -2) == "neg"


def test_missing_else_rejected_in_expression_position():
    with pytest.raises(RevlError, match=r"used as an expression needs an `else`"):
        compile_source('fn f(n: Int) -> Str { return if (n > 0) { "pos" } }')


def test_branches_must_agree_in_type():
    # the checker enforces this on `ExprIf` exactly as it does for the ternary.
    with pytest.raises(RevlError, match="branches disagree"):
        compile_source(
            'fn f(n: Int) -> Int { let k = if (n > 0) { 1 } else { "x" } return k }'
        )


def test_statement_if_without_else_is_unchanged():
    # statement-position `if` (side-effecting body, no `else`) still parses and
    # runs as before — expression-if only ADDS a form.
    src = "fn f(n: Int) -> Int { var x = 0 if (n > 0) { x = 1 } return x }"
    assert _run_py(src, "f", 4) == 1
    assert _run_py(src, "f", -4) == 0


def _pair(ternary_body, if_body):
    tern = f"fn c(n: Int) -> Str {{ {ternary_body} }}"
    ife = f"fn c(n: Int) -> Str {{ {if_body} }}"
    return compile_source(tern), compile_source(ife)


def test_expression_if_lowers_to_the_same_ir_as_the_ternary():
    ir_t, ir_i = _pair(
        'let k = n > 0 ? "p" : "q" return k',
        'let k = if (n > 0) { "p" } else { "q" } return k',
    )
    assert ir_t == ir_i


@pytest.mark.parametrize("backend", BACKENDS)
def test_expression_if_emits_byte_identical_to_ternary(backend):
    be = backend_emitter(backend)
    ir_t, ir_i = _pair(
        'return n > 0 ? "p" : "q"',
        'return if (n > 0) { "p" } else { "q" }',
    )
    assert be.emit(ir_t) == be.emit(ir_i)


@pytest.mark.parametrize("backend", BACKENDS)
def test_nested_expression_if_emits_byte_identical_to_ternary(backend):
    be = backend_emitter(backend)
    ir_t, ir_i = _pair(
        'return n > 0 ? (n > 10 ? "big" : "small") : "neg"',
        'return if (n > 0) { if (n > 10) { "big" } else { "small" } } else { "neg" }',
    )
    assert be.emit(ir_t) == be.emit(ir_i)
