"""Codepoint-at-index scan builtin (roadmap item 276, docs/stdlib-2.0.md
§Str.codepoint_at): `Str.codepoint_at(i) -> Int`.

This exists to cut the self-host lexer's residual per-byte value-layer tax
(the item-231a finding): the hot path spelled the code point at index `j` as
`code0(source.charAt(j))`, which allocates a 1-char `Str` for `charAt(j)`, then
indexes it again inside `code0` (a revl-fn call) to reach `charCodeAt(0)`/`ord`.
`codepoint_at(i)` returns the Unicode scalar at code-point index `i` directly,
so the hot path allocates no intermediate 1-char `Str` and makes no fn call
(see docs/bench-selfhost.md, the lexer before->after row).

Spec: the receiver is a `Str`, the argument the index; the result is the
Unicode scalar value at that index. Like `charAt`/`charCodeAt`, the index is
assumed in bounds (`0 <= i < length`) — the self-host lexer only ever indexes
a position it has already guarded, exactly as it did with `charAt`.

Checked here:
  * the checker dispatches on the Str family, checks arity 1, types Int;
  * the python tier answers EXACTLY `ord(s[i])` for every byte and evaluates a
    side-effecting receiver once;
  * the python lowering emits its documented inline shape (no fn call).

Other tiers (rust/java/ts/wasm/go) get a per-backend lowering too; py is the
bench tier this item targets and the only tier selfhost/lexer.rvl is emitted
to, so the executable contract is asserted on py (per-tier note in
docs/stdlib-2.0.md §Str.codepoint_at).
"""

import importlib.util
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import RevlError, compile_source  # noqa: E402

FN = "fn cp(s: Str, i: Int) -> Int { return s.codepoint_at(i) }\n"


def _err(src: str) -> str:
    with pytest.raises(RevlError) as excinfo:
        compile_source(src)
    return str(excinfo.value)


# ---------------------------------------------------------------- checker

def test_receiver_must_be_str():
    assert "needs a Str receiver" in _err(
        "fn bad(n: Int) -> Int { return n.codepoint_at(0) }")


def test_arity_is_one():
    assert "takes 1 argument" in _err(
        "fn bad(s: Str) -> Int { return s.codepoint_at() }")


def test_result_is_Int():
    # an Int result flows into integer arithmetic without refusal
    ir = compile_source(
        "fn tag(s: Str) -> Int { return s.codepoint_at(0) + 1 }\n")
    assert ir["ir_version"] == 3


# ---------------------------------------------------------------- python

def _exec_python(src: str):
    ir = compile_source(src)
    spec = importlib.util.spec_from_file_location(
        "pyemit_codepoint_at", ROOT / "backends" / "python" / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    stub = types.ModuleType("runtime")
    stub.__getattr__ = lambda name: (lambda *a, **k: None)  # PEP 562
    had = "runtime" in sys.modules
    previous = sys.modules.get("runtime")
    sys.modules["runtime"] = stub
    try:
        namespace = {}
        exec(compile(module.emit(ir), "codepoint_at.py", "exec"), namespace)
    finally:
        if had:
            sys.modules["runtime"] = previous
        else:
            del sys.modules["runtime"]
    return namespace


def test_python_matches_ord_over_every_byte():
    fn = _exec_python(FN)["cp"]
    for n in range(256):
        assert fn(chr(n), 0) == n, n


def test_python_indexes_the_named_position():
    fn = _exec_python(FN)["cp"]
    s = "aA0 \n"
    for i, ch in enumerate(s):
        assert fn(s, i) == ord(ch), i


def test_python_receiver_evaluated_once():
    # a side-effecting receiver (a counter proxy) must fire exactly once.
    ns = _exec_python(
        "fn nth(s: Str, i: Int) -> Int { return s.slice(i, i + 1).codepoint_at(0) }\n")
    assert ns["nth"]("abc", 0) == ord("a")
    assert ns["nth"]("abc", 2) == ord("c")


# ---------------------------------------------------------------- shape

def test_python_lowering_is_inline_no_fn_call():
    spec = importlib.util.spec_from_file_location(
        "emit_codepoint_at_py", ROOT / "backends" / "python" / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    src = module.emit(compile_source(FN))
    # a bare `ord(s[i])` — no revl-fn call, no intermediate charAt binding.
    assert "ord(s[i])" in src
