"""`Str.startsWith(p)` / `Str.endsWith(p)` (FR-6, docs/stdlib-2.0.md
§Str.startsWith): the prefix/suffix probes the harness's wire protocol needs.

The harness parses a prefix-tagged wire format (`FINAL `, `TOOL_CALL `) and
hit a real off-by-one (`"TOOL_CALL "` is 10 chars, sliced 9) that
`slice`-then-compare cannot catch. These two builtins make the check read
what it means.

Spec: `startsWith(p)` is true iff the receiver's first `p.length()` code
points equal `p`; `endsWith(p)` is true iff its last `p.length()` code
points do. The empty prefix/suffix is a prefix/suffix of every string.

Checked here:
  * the checker dispatches both on the Str family, checks arity and the
    argument type, and types the result Bool;
  * the python tier executes exactly (prefix/suffix true/false, empty
    prefix, unicode);
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

FN = ("fn pref(s: Str, p: Str) -> Bool { return s.startsWith(p) }\n"
      "fn suff(s: Str, p: Str) -> Bool { return s.endsWith(p) }\n")


def _err(src: str) -> str:
    with pytest.raises(RevlError) as excinfo:
        compile_source(src)
    return str(excinfo.value)


# ---------------------------------------------------------------- checker

def test_receiver_must_be_str():
    assert "needs a Str receiver" in _err(
        "fn bad(n: Int) -> Bool { return n.startsWith(\"a\") }")
    assert "needs a Str receiver" in _err(
        "fn bad(n: Int) -> Bool { return n.endsWith(\"a\") }")


def test_arity_is_one():
    assert "takes 1 argument(s)" in _err(
        "fn bad(s: Str) -> Bool { return s.startsWith() }")
    assert "takes 1 argument(s)" in _err(
        "fn bad(s: Str) -> Bool { return s.endsWith(\"a\", \"b\") }")


def test_argument_must_be_str():
    assert "argument expects `Str`, got `Int`" in _err(
        "fn bad(s: Str, n: Int) -> Bool { return s.startsWith(n) }")


def test_result_is_Bool():
    # a Bool result flows into && without refusal
    ir = compile_source(
        "fn tag(s: Str) -> Bool { return s.startsWith(\"FINAL \") "
        "&& s.endsWith(\"\\n\") }\n")
    assert ir["ir_version"] == 3


# ---------------------------------------------------------------- python

def _exec_python(src: str):
    ir = compile_source(src)
    spec = importlib.util.spec_from_file_location(
        "pyemit_str_prefix", ROOT / "backends" / "python" / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    stub = types.ModuleType("runtime")
    stub.__getattr__ = lambda name: (lambda *a, **k: None)  # PEP 562
    had = "runtime" in sys.modules
    previous = sys.modules.get("runtime")
    sys.modules["runtime"] = stub
    try:
        namespace = {}
        exec(compile(module.emit(ir), "str_prefix.py", "exec"), namespace)
    finally:
        if had:
            sys.modules["runtime"] = previous
        else:
            del sys.modules["runtime"]
    return namespace


def test_python_prefix_true_and_false():
    ns = _exec_python(FN)
    assert ns["pref"]("TOOL_CALL get_weather", "TOOL_CALL ") is True
    assert ns["pref"]("TOOL_CALL ", "TOOL_CALL ") is True
    assert ns["pref"]("FINAL x", "TOOL_CALL ") is False
    assert ns["pref"]("abc", "b") is False


def test_python_suffix_true_and_false():
    ns = _exec_python(FN)
    assert ns["suff"]("FINAL ", "FINAL ") is True
    assert ns["suff"]("FINAL x", "FINAL ") is False
    assert ns["suff"]("abc", "bc") is True
    assert ns["suff"]("abc", "b") is False


def test_python_empty_prefix_and_suffix():
    ns = _exec_python(FN)
    assert ns["pref"]("anything", "") is True
    assert ns["suff"]("anything", "") is True
    assert ns["pref"]("", "") is True
    assert ns["suff"]("", "") is True


def test_python_unicode_code_points():
    # code-point semantics: the astral char is ONE code point, so a
    # one-scalar prefix matches it exactly (and not a UTF-16 half)
    ns = _exec_python(FN)
    assert ns["pref"]("😀hi", "😀") is True
    assert ns["suff"]("hi😀", "😀") is True
    assert ns["pref"]("😀hi", "\ud83d") is False


# ---------------------------------------------------------------- shapes

def _emit_with(backend: str):
    sys.path.insert(0, str(ROOT / "backends" / backend))
    try:
        spec = importlib.util.spec_from_file_location(
            f"emit_str_prefix_{backend}", ROOT / "backends" / backend / "emit.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.emit(compile_source(FN))
    finally:
        sys.path.remove(str(ROOT / "backends" / backend))


def test_python_lowers_to_startswith_endswith():
    out = str(_emit_with("python"))
    assert "s.startswith(p)" in out and "s.endswith(p)" in out


def test_typescript_lowers_to_native_string_methods():
    out = str(_emit_with("typescript"))
    assert "s.startsWith(p)" in out and "s.endsWith(p)" in out


def test_rust_lowers_to_revl_starts_with_helpers():
    out = str(_emit_with("rust"))
    assert "revl_starts_with" in out and "revl_ends_with" in out


def test_java_lowers_to_native_string_methods():
    out = str(_emit_with("java"))
    assert "s.startsWith(p)" in out and "s.endsWith(p)" in out


def test_go_lowers_to_hasprefix_hassuffix():
    out = str(_emit_with("go"))
    assert "strings.HasPrefix(s, p)" in out
    assert "strings.HasSuffix(s, p)" in out


def test_wasm_lowers_to_the_prefix_helpers():
    out = str(_emit_with("wasm"))
    assert "$str_starts_with" in out and "$str_ends_with" in out
