"""`Str.to_int()` (FR-9, docs/stdlib-2.0.md §Str.to_int): the parsing builtin.

The harness's tool args arrive as strings; it hand-rolled a `parse_int` in
~15 lines because the intent (`verified fn parse_int` in the docs) had no
builtin. This is it, as a method mirroring `Int.to_str()`.

Spec: total on the ASCII digits with an optional leading `-` (no `+`, no
whitespace, no signless-empty), `None` otherwise — including out of the i64
range, which is `None` like every other non-digit (consistent with the Int
bound: `Int.MIN` itself parses, `Int.MAX + 1` does not).

Checked here:
  * the checker dispatches it on the Str family (alongside the existing
    Int32 widen, whose spelling it shares) and types the result Opt[Int];
  * the python tier executes exactly: digits, negative, zero, empty,
    non-digit, partial, overflow above/below the i64 bound, Int.MIN;
  * every tier emits its documented lowering shape.
"""

import importlib.util
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import RevlError, compile_source  # noqa: E402

FN = "fn pi(s: Str) -> Opt[Int] { return s.to_int() }\n"
FN_OR = "fn pi_or(s: Str, d: Int) -> Int { return s.to_int() ?? d }\n"

MIN = -9223372036854775808
MAX = 9223372036854775807


def _err(src: str) -> str:
    with pytest.raises(RevlError) as excinfo:
        compile_source(src)
    return str(excinfo.value)


# ---------------------------------------------------------------- checker

def test_receiver_must_be_str():
    assert "has no form for a `Int` receiver" in _err(
        "fn bad(n: Int) -> Int { return n.to_int() }")
    assert "has no form for a `Bool` receiver" in _err(
        "fn bad(b: Bool) -> Bool { return b.to_int() }")


def test_arity_is_zero():
    assert "takes 0 argument(s)" in _err(
        "fn bad(s: Str) -> Opt[Int] { return s.to_int(1) }")


def test_result_is_Opt_Int():
    # an Opt[Int] unwraps with ?? and flows into match
    ir = compile_source(
        "fn unwrap(s: Str) -> Int { return s.to_int() ?? 0 }\n"
        "fn pick(s: Str) -> Int { return match s.to_int() "
        "{ Some(v) => v, None => 0 } }\n")
    assert ir["ir_version"] == 3


def test_int32_widen_still_works():
    # `to_int` is also the Int32 -> Int widening; the two forms coexist
    ir = compile_source("fn w(n: Int32) -> Int { return n.to_int() }\n")
    assert ir["functions"][0]["returns"] == "Int"


# ---------------------------------------------------------------- python

def _exec_python(src: str):
    ir = compile_source(src)
    spec = importlib.util.spec_from_file_location(
        "pyemit_str_to_int", ROOT / "backends" / "python" / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    stub = types.ModuleType("runtime")
    stub.__getattr__ = lambda name: (lambda *a, **k: None)  # PEP 562
    had = "runtime" in sys.modules
    previous = sys.modules.get("runtime")
    sys.modules["runtime"] = stub
    try:
        namespace = {}
        exec(compile(module.emit(ir), "str_to_int.py", "exec"), namespace)
    finally:
        if had:
            sys.modules["runtime"] = previous
        else:
            del sys.modules["runtime"]
    return namespace


def test_python_digits_and_zero():
    pi = _exec_python(FN)["pi"]
    assert pi("42") == 42
    assert pi("0") == 0
    assert pi("007") == 7
    assert pi(str(MAX)) == MAX


def test_python_negative():
    pi = _exec_python(FN)["pi"]
    assert pi("-7") == -7
    assert pi("-0") == 0
    assert pi(str(MIN)) == MIN


def test_python_empty_and_non_digits_are_None():
    pi = _exec_python(FN)["pi"]
    for bad in ("", "-", "abc", "12a", "3.5", "+5", " 42", "42 ", "--7",
                "0x10", "١٢"):
        assert pi(bad) is None, f"expected None for {bad!r}"


def test_python_overflow_is_None():
    # out of the i64 range -> None, exactly like a non-digit (the Int bound)
    pi = _exec_python(FN)["pi"]
    assert pi(str(MAX + 1)) is None       # 2^63
    assert pi(str(MIN - 1)) is None       # -(2^63 + 1)
    assert pi("99999999999999999999999999") is None  # 26 digits


def test_python_nullish_fallback():
    pi_or = _exec_python(FN_OR)["pi_or"]
    assert pi_or("42", 0) == 42
    assert pi_or("x", 7) == 7


# ---------------------------------------------------------------- shapes

def _emit_with(backend: str):
    sys.path.insert(0, str(ROOT / "backends" / backend))
    try:
        spec = importlib.util.spec_from_file_location(
            f"emit_str_to_int_{backend}", ROOT / "backends" / backend / "emit.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.emit(compile_source(FN))
    finally:
        sys.path.remove(str(ROOT / "backends" / backend))


def test_python_lowers_to_the_parse_lambda():
    out = str(_emit_with("python"))
    assert "isdigit" in out


def test_typescript_lowers_to_revlParseInt():
    out = str(_emit_with("typescript"))
    assert "revlParseInt(s)" in out


def test_rust_lowers_to_parse():
    out = str(_emit_with("rust"))
    assert "(s).parse::<i64>().ok()" in out


def test_java_lowers_to_revlParseInt():
    out = str(_emit_with("java"))
    assert "revlParseInt(s)" in out
    assert "Long.parseLong" in out


def test_go_lowers_to_revlParseInt():
    out = str(_emit_with("go"))
    assert "revlParseInt(s)" in out


def test_wasm_lowers_to_the_str_to_int_helper():
    out = str(_emit_with("wasm"))
    assert "$str_to_int" in out


def test_int32_widen_shape_unchanged_on_go():
    # the Int32 form of to_int still lowers to a plain int64 conversion
    sys.path.insert(0, str(ROOT / "backends" / "go"))
    try:
        spec = importlib.util.spec_from_file_location(
            "emit_str_to_int_go_widen", ROOT / "backends" / "go" / "emit.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        out = str(module.emit(
            compile_source("fn w(n: Int32) -> Int { return n.to_int() }\n")))
    finally:
        sys.path.remove(str(ROOT / "backends" / "go"))
    assert "int64(n)" in out
    assert "revlParseInt" not in out
