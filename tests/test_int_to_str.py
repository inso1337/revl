"""`Int.to_str()` (docs/stdlib-2.0.md §Int.to_str): the rendering builtin.

Spec: decimal ASCII digits, leading `-` for negatives, no separators, `0`
for zero — and total over the whole i64 range, `Int.MIN` included, which is
the edge every tier must get right without an `|MIN|` detour.

Checked here:
  * the checker dispatches it on the Int family and refuses misuse
    (wrong receiver family, wrong arity);
  * the python tier executes exactly, including the MIN spelling built as
    `0 - Int.MAX - 1` (`Int.MIN` has no literal spelling — see the
    range-refusal comment in typecheck.py);
  * the other five tiers emit their documented lowering shape;
  * the wasm tier runs the `$int_to_str` helper end-to-end (the
    digit-count probe in tests/test_wasm_backend.py).
"""

import importlib.util
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import RevlError, compile_source  # noqa: E402

FN = "fn label(n: Int) -> Str { return n.to_str() }\n"


def _err(src: str) -> str:
    with pytest.raises(RevlError) as excinfo:
        compile_source(src)
    return str(excinfo.value)


# ---------------------------------------------------------------- checker

def test_receiver_must_be_int():
    assert "needs a Int receiver" in _err(
        "fn bad(s: Str) -> Str { return s.to_str() }")


def test_arity_is_zero():
    assert "takes 0 argument(s)" in _err(
        "fn bad(n: Int) -> Str { return n.to_str(1) }")


def test_result_is_Str():
    # a Str result flows into string concatenation without refusal
    ir = compile_source("fn tag(n: Int) -> Str { return \"n=\".concat(n.to_str()) }\n")
    assert ir["ir_version"] == 3


# ---------------------------------------------------------------- python

def _exec_python(src: str):
    ir = compile_source(src)
    spec = importlib.util.spec_from_file_location(
        "pyemit_int_to_str", ROOT / "backends" / "python" / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    stub = types.ModuleType("runtime")
    stub.__getattr__ = lambda name: (lambda *a, **k: None)  # PEP 562
    had = "runtime" in sys.modules
    previous = sys.modules.get("runtime")
    sys.modules["runtime"] = stub
    try:
        namespace = {}
        exec(compile(module.emit(ir), "int_to_str.py", "exec"), namespace)
    finally:
        if had:
            sys.modules["runtime"] = previous
        else:
            del sys.modules["runtime"]
    return namespace


def test_python_tier_renders_exactly():
    label = _exec_python(FN)["label"]
    assert label(42) == "42"
    assert label(0) == "0"
    assert label(-7) == "-7"
    assert label(9223372036854775807) == "9223372036854775807"


def test_python_tire_renders_int_min():
    """`Int.MIN` has no literal spelling; build it at runtime. The result
    stays inside i64 at every step, so nothing faults on the way."""
    src = ("fn smallest() -> Str "
           "{ return (0 - 9223372036854775807 - 1).to_str() }\n")
    assert _exec_python(src)["smallest"]() == "-9223372036854775808"


# ---------------------------------------------------------------- shapes

def _emit_with(backend: str):
    sys.path.insert(0, str(ROOT / "backends" / backend))
    try:
        spec = importlib.util.spec_from_file_location(
            f"emit_int_to_str_{backend}", ROOT / "backends" / backend / "emit.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.emit(compile_source(FN))
    finally:
        sys.path.remove(str(ROOT / "backends" / backend))


def test_typescript_lowers_to_bigint_toString():
    out = str(_emit_with("typescript"))
    assert "n.toString()" in out


def test_go_lowers_to_fmt_verbatim_d():
    out = str(_emit_with("go"))
    assert 'fmt.Sprintf("%d", n)' in out
    # a pure module whose sole fmt use is to_str must still import fmt,
    # or `go build` fails with `undefined: fmt`
    assert '"fmt"' in out


def test_rust_lowers_to_i64_to_string():
    out = str(_emit_with("rust"))
    assert "(n).to_string()" in out


def test_java_lowers_to_string_valueOf():
    out = str(_emit_with("java"))
    assert "String.valueOf(n)" in out


def test_wasm_lowers_to_the_int_to_str_helper():
    out = str(_emit_with("wasm"))
    assert "(call $int_to_str " in out
