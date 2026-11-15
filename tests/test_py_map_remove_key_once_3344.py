"""Regression for issue #3344: `Map.remove(k)` evaluates `k` once per call.

The python backend rendered the key inside the comprehension's condition, so a
call key ran once per map entry. A key that is not a bare name or a number is
now passed as an argument, evaluated once; a bare key keeps the one-frame form.
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

PY = load_by_path("revl_py_emit_3344", ROOT / "backends" / "python" / "emit.py")

SOURCE = """
type Box = { m: Map[Str, Int] }
fn key(x: Int) -> Str {
  var s = x
  while (s > 1000000) { s = s - 1 }
  return `k${s}`
}
fn viaCall(b: Box, x: Int) -> Map[Str, Int] { return b.m.remove(key(x)) }
fn viaBare(b: Box, k: Str) -> Map[Str, Int] { return b.m.remove(k) }
fn viaKk(m: Map[Str, Int], kk: Str) -> Map[Str, Int] { return m.remove(kk) }
fn viaVv(m: Map[Str, Int], vv: Str) -> Map[Str, Int] { return m.remove(vv) }
fn viaLocal(m: Map[Str, Int], x: Int) -> Map[Str, Int] { return m.remove(key(x)) }
"""


def _run(src: str) -> dict:
    stub = types.ModuleType("runtime")
    stub.__getattr__ = lambda name: (lambda *a, **k: None)  # PEP 562
    previous = sys.modules.get("runtime")
    sys.modules["runtime"] = stub
    try:
        namespace: dict = {}
        exec(compile(src, "emitted_3344.py", "exec"), namespace)
        return namespace
    finally:
        if previous is None:
            del sys.modules["runtime"]
        else:
            sys.modules["runtime"] = previous


def _counting():
    src = PY.emit(compile_source(SOURCE))
    ns = _run(src)
    calls = []
    original = ns["key"]

    def key(x):
        calls.append(x)
        return original(x)

    ns["key"] = key
    return src, ns, calls


def test_a_call_key_is_evaluated_once_per_remove():
    _, ns, calls = _counting()
    m = {f"k{i}": i for i in range(3000)}
    out = ns["viaCall"]({"m": m}, 5)
    assert calls == [5]
    assert len(out) == 2999 and "k5" not in out and len(m) == 3000


def test_a_call_key_on_a_plain_receiver_is_evaluated_once():
    _, ns, calls = _counting()
    m = {f"k{i}": i for i in range(3000)}
    assert "k7" not in ns["viaLocal"](m, 7)
    assert calls == [7]


def test_removing_a_missing_key_is_total_and_still_evaluates_once():
    _, ns, calls = _counting()
    m = {"k1": 1}
    assert ns["viaCall"]({"m": m}, 99) == {"k1": 1}
    assert calls == [99]


def test_a_bare_key_keeps_the_one_frame_comprehension():
    src, ns, _ = _counting()
    assert ns["viaBare"]({"m": {"a": 1, "b": 2}}, "a") == {"b": 2}
    body = src[src.index("def viaBare("):].split("\ndef ")[0]
    assert "kk != k}" in body


def test_a_key_named_like_the_comprehension_variable_is_not_captured():
    _, ns, _ = _counting()
    assert ns["viaKk"]({"a": 1, "b": 2}, "a") == {"b": 2}
    assert ns["viaVv"]({"a": 1, "b": 2}, "a") == {"b": 2}
