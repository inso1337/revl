"""#548 review item 3 — an expression-bodied top-level fn: `fn f(x) -> T = expr`.

The component-author review (REVL-REVIEW-2026-09-06, summarised in #548) found
that the `= <expr>` fn shape works in a provide method but NOT at module top
level, so an author writing a small pure helper had to spell the full
`{ return … }` block. The wall was a bare "expected {" with no hint.

The fix (parser.fn_decl) is the SAME sugar the provide-method grammar already
carries (parser.provide desugars `fn m(..) = e` to `[ReturnStmt(e)]`), extended
to the top-level position: after the parameter list, optional `-> T` return
type and optional `cache` clause, an `=` heads a single-`return` body. The `=`
is unambiguous — a block body opens with `{`, never `=`. It desugars to the
identical AST a `{ return <expr> }` body produces, so the checker, lowering and
all six emitters see no new node: no new IR, no emitter change, no gate-crate
movement, and every previously-compiling program's emitted bytes are unchanged
(the emit-parity tests below pin this).
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "backends" / "python"))

from _backend_import import backend_emitter  # noqa: E402
from revl import RevlError, compile_source  # noqa: E402

emit = backend_emitter("python")
BACKENDS = ["python", "typescript", "go", "java", "rust", "wasm"]


def _run_py(source, fn, *args):
    ns = {}
    exec(compile(emit.emit(compile_source(source)), "emitted.py", "exec"), ns)
    return ns[fn](*args)


def test_expr_bodied_toplevel_fn_compiles():
    ir = compile_source("fn double(x: Int) -> Int = x * 2")
    assert ir["ir_version"] == 3


def test_expr_body_is_byte_identical_to_block_body():
    # the sugar desugars to `{ return <expr> }`; the emitted bytes must match on
    # every tier, which is the whole reason no golden/byte-agreement oracle moves.
    expr = compile_source("fn double(x: Int) -> Int = x * 2")
    block = compile_source("fn double(x: Int) -> Int { return x * 2 }")
    for backend in BACKENDS:
        e = backend_emitter(backend)
        assert e.emit(expr) == e.emit(block), backend


def test_expr_bodied_fn_runs_on_py():
    assert _run_py("fn double(x: Int) -> Int = x * 2", "double", 21) == 42


def test_expr_body_without_return_annotation():
    # the return type is optional exactly as in the block form.
    assert _run_py("fn id(x: Int) = x", "id", 7) == 7


def test_expr_body_may_be_a_match_expression():
    src = ("fn classify(n: Int) -> Str = "
           "match n.checked_div_trunc(2) { Ok(_) => `ok`, Err(_) => `bad` }")
    assert _run_py(src, "classify", 10) == "ok"


def test_expr_body_may_call_another_fn():
    src = ("fn slot_for(id: Int) -> Int = id.mod(16)\n"
           "fn banner(id: Int) -> Str = `slot ${slot_for(id).to_str()}`\n")
    assert _run_py(src, "banner", 19) == "slot 3"


def test_expr_body_composes_with_generics():
    assert _run_py("fn pick[T](a: T, b: T) -> T = a", "pick", 3, 9) == 3


def test_expr_body_composes_with_cache_clause():
    # the `cache` clause sits between the return type and the body; the `= expr`
    # follows it, byte-identically to the block form.
    expr = compile_source("fn sq(x: Int) -> Int cache pure = x * x")
    block = compile_source("fn sq(x: Int) -> Int cache pure { return x * x }")
    assert emit.emit(expr) == emit.emit(block)
    assert _run_py("fn sq(x: Int) -> Int cache pure = x * x", "sq", 6) == 36


def test_expr_body_composes_with_default_params():
    expr = compile_source("fn inc(x: Int, by: Int = 1) -> Int = x + by")
    block = compile_source("fn inc(x: Int, by: Int = 1) -> Int { return x + by }")
    assert emit.emit(expr) == emit.emit(block)


def test_expr_body_still_type_checks_the_expression():
    # the body is checked exactly as a returned expression: a Bool where the
    # signature promises Str is the compile error it always was.
    with pytest.raises(RevlError) as ei:
        compile_source("fn f(s: Str) -> Str = s.startsWith(` `)")
    assert "Str" in str(ei.value) and "Bool" in str(ei.value)


def test_empty_expr_body_is_refused():
    with pytest.raises(RevlError):
        compile_source("fn f(x: Int) -> Int = ")


def test_block_body_is_unaffected():
    # the pre-existing block form still parses and runs.
    assert _run_py("fn double(x: Int) -> Int { return x * 2 }", "double", 4) == 8


def test_realistic_component_draft_with_expr_bodied_helpers_compiles():
    # a component-author draft in the shape the review wrote: small pure helpers
    # as expr-bodied top-level fns, called from a provide method. This is the
    # acceptance the gap blocked — the draft now reaches the frontend clean.
    prelude = (
        "extern pure fn fs_write(p: Str, data: Str) -> Unit = @py { return }\n"
        "extern pure fn fs_del(p: Str) -> Unit = @py { return }\n"
    )
    draft = """
pub fn slot_for(id: Int) -> Int = id.mod(16)
pub fn banner(id: Int) -> Str = `node-${id.to_str()} @ slot ${slot_for(id).to_str()}`

service Ops { emission fn write(p: Str, data: Str) }
component Agent provides ops: Ops {
  provide ops {
    fn write(p, data) { effect fs_write(banner(3), data) undo fs_del(p) }
  }
}
"""
    ir = compile_source(prelude + draft, "draft.rvl")
    assert ir["ir_version"] == 3
