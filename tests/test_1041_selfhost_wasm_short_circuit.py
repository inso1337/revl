"""The SELF-HOSTED wasm emitter short-circuits `&&`/`||` too (issue #1041).

Item 458 (PR #1039) made `backends/wasm/emit.py` lower `a && b` to the folded
`(if (result i32) …)` branch whenever `b` can trap, allocate or read memory, so
an ordinary guard answers on wasm what it answers on the other five tiers.
`selfhost/emit_wasm.rvl` — the revl port of that same emitter, and the thing a
self-hosted compiler would actually run — kept emitting the strict `i32.and` /
`i32.or`. A wasm emitter built from the self-host source therefore still carried
the divergence the reference no longer had.

The byte-agreement oracle (tests/test_selfhost_emit_wasm.py) stayed GREEN over
that gap for one reason: no document in its corpus spelled a trapping right
operand, so both emitters agreed on the strict form everywhere the oracle could
see. That is the same blind spot that let the original defect live. The fix
therefore ships `tests/fixtures/emit_wasm_corpus/shortcircuit.rvl`, which spells
one; this module states, in assertions rather than in bytes, what that document
pins, so a regression reads as a sentence and not as a length mismatch.

NON-VACUITY CONTROL: `test_a_trap_free_right_operand_keeps_the_strict_form`
asserts that `a < b && a <= b` still emits a single `(i32.and)` and NO branch.
It proves the assertions above discriminate — the port did not simply become
"every `&&` is a branch" — and it pins the rule that earns the strict form: the
right operand provably cannot trap, allocate or read memory, which is what a
comparison of two locals is.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def selfhost():
    """`emit_wasm_src` as produced by compiling selfhost/emit_wasm.rvl — the same
    build the byte-agreement oracle exercises, reused rather than re-derived."""
    oracle = _load("selfhost_emit_wasm_oracle", ROOT / "tests" / "test_selfhost_emit_wasm.py")
    return oracle._exec_emitted()["emit_wasm_src"]


@pytest.fixture(scope="module")
def reference():
    return _load("wasmemit_reference_1041", ROOT / "backends" / "wasm" / "emit.py")


def _body(wat: str, fn: str) -> str:
    """The `(func $fn …)` block, so an assertion about one function's lowering
    cannot be satisfied by the ~430-line helper preamble every module carries."""
    start = wat.index(f'(func ${fn} (export "{fn}")')
    rest = wat.index("\n  (func ", start + 1) if "\n  (func " in wat[start + 1:] else len(wat)
    return wat[start:rest]


#: `(name, source, fn, the instruction that must NOT run when the guard fails)`
TRAPPING = [
    ("divisor", "fn f(n: Int, d: Int) -> Bool { return d != 0 && n % d == 0 }",
     "f", "i64.rem_s"),
    ("divisor-or", "fn f(n: Int, d: Int) -> Bool { return d == 0 || n % d != 0 }",
     "f", "i64.rem_s"),
    ("overflow", "fn f(ok: Bool, n: Int) -> Bool { return ok && n * n > 0 }",
     "f", "call $int_mul"),
    ("index", "fn f(xs: List[Int], i: Int) -> Bool { return i < xs.length && xs[i] > 0 }",
     "f", "i64.load"),
    ("call", "fn h(n: Int) -> Int { return n.div_trunc(2) }\n"
             "fn f(ok: Bool, n: Int) -> Bool { return ok && h(n) > 0 }", "f", "call $h"),
    ("negated", "fn f(ok: Bool, n: Int) -> Bool { return ok && !(n + n > 0) }",
     "f", "call $int_add"),
]


@pytest.mark.parametrize("name, source, fn, trapping", TRAPPING,
                         ids=[case[0] for case in TRAPPING])
def test_a_trapping_right_operand_is_lowered_to_a_branch(
        selfhost, name, source, fn, trapping):
    """The self-hosted emitter puts the right operand behind the branch, so the
    instruction that traps is reached only when the left operand let it be."""
    wat = selfhost(compile_source(source, "guard.rvl"))
    body = _body(wat, fn)
    assert "(if (result i32)" in body, body
    assert "(i32.and)" not in body and "(i32.or)" not in body, body
    # the trapping instruction is inside the branch arm, never on the straight
    # line before it
    guarded = body[body.index("(if (result i32)"):]
    assert trapping in guarded, body
    assert trapping not in body[:body.index("(if (result i32)")], body


TRAP_FREE = [
    ("locals", "fn f(a: Bool, b: Bool) -> Bool { return a && b }", "f"),
    ("comparison", "fn f(a: Int, b: Int) -> Bool { return a < b && a <= b }", "f"),
    ("negation", "fn f(a: Bool, b: Bool) -> Bool { return a && !b }", "f"),
    ("literal", "fn f(a: Bool) -> Bool { return a && true }", "f"),
    ("nested-logical", "fn f(a: Bool, b: Bool, c: Bool) -> Bool { return a && (b || c) }", "f"),
    ("int32", "fn f(a: Int32, b: Int32) -> Bool { return a < b && a >= b }", "f"),
]


@pytest.mark.parametrize("name, source, fn", TRAP_FREE, ids=[c[0] for c in TRAP_FREE])
def test_a_trap_free_right_operand_keeps_the_strict_form(selfhost, name, source, fn):
    """The non-vacuity control. Evaluating a constant, a local read, or a
    comparison of those where the language would have skipped it is
    observationally identical to not evaluating it, so the single strict
    instruction stays — and every pre-existing golden byte with it."""
    wat = selfhost(compile_source(source, "plain.rvl"))
    body = _body(wat, fn)
    assert "(if (result i32)" not in body, body
    assert "(i32.and)" in body or "(i32.or)" in body, body


@pytest.mark.parametrize("name, source, fn, trapping", TRAPPING,
                         ids=[case[0] for case in TRAPPING])
def test_the_port_agrees_with_the_reference_byte_for_byte(
        selfhost, reference, name, source, fn, trapping):
    """The oracle's own standard, applied to the shapes the corpus was missing:
    the port's decision is the reference's decision, not merely a decision."""
    ir = compile_source(source, "guard.rvl")
    assert selfhost(ir) == reference.emit(ir)["functions"]
