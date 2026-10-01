"""Regression for issue #1632: `Map.remove` on a nested field receiver.

The python backend renders `m.remove(k)` as a dict comprehension whose
iterable is the receiver. A receiver that is not a bare name carries an
assignment expression (`o.ctx.sc` reads `o.ctx` through the `_fv :=` temp),
and Python refuses an assignment expression anywhere in a comprehension's
iterable, so the emitted module did not compile at all:
`SyntaxError: assignment expression cannot be used in a comprehension
iterable expression`. The receiver and the key are now evaluated as arguments,
outside the comprehension; a plain receiver keeps the one-frame form.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from _load_by_path import load_by_path  # noqa: E402
from revl.compiler import compile_source  # noqa: E402

PY = load_by_path("revl_py_emit_1632", ROOT / "backends" / "python" / "emit.py")

SOURCE = """
type Ctx = { sc: Map[Str, Str], n: Int }
type Out = { ctx: Ctx }
type Box = { m: Map[Str, Int] }
fn nested(o: Out, k: Str) -> Map[Str, Str] { return o.ctx.sc.remove(k) }
fn shallow(b: Box, k: Str) -> Map[Str, Int] { return b.m.remove(k) }
fn bare(m: Map[Str, Int], k: Str) -> Map[Str, Int] { return m.remove(k) }
"""


def _emitted() -> str:
    return PY.emit(compile_source(SOURCE))


def _run(src: str) -> dict:
    stub = types.ModuleType("runtime")
    stub.__getattr__ = lambda name: (lambda *a, **k: None)  # PEP 562
    previous = sys.modules.get("runtime")
    sys.modules["runtime"] = stub
    try:
        namespace: dict = {}
        exec(compile(src, "emitted_1632.py", "exec"), namespace)
        return namespace
    finally:
        if previous is None:
            del sys.modules["runtime"]
        else:
            sys.modules["runtime"] = previous


def test_a_nested_field_receiver_compiles_and_removes():
    ns = _run(_emitted())
    out = {"ctx": {"sc": {"a": "1", "b": "2"}, "n": 0}}
    assert ns["nested"](out, "a") == {"b": "2"}
    # persistent: the receiver is untouched
    assert out == {"ctx": {"sc": {"a": "1", "b": "2"}, "n": 0}}


def test_a_plain_receiver_keeps_the_one_frame_comprehension():
    src = _emitted()
    ns = _run(src)
    assert ns["shallow"]({"m": {"x": 1, "y": 2}}, "x") == {"y": 2}
    assert ns["bare"]({"x": 1}, "x") == {}
    bare = src[src.index("def bare("):]
    assert "return {kk: vv for kk, vv in m.items() if kk != k}" in bare
