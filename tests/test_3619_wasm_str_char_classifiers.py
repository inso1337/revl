"""The wasm tier emits the four single-char ASCII classifiers (issue #3619).

WHAT WAS WRONG. `Str.is_digit()` / `is_alpha()` / `is_alnum()` / `is_space()`
(item 233, docs/stdlib-2.0.md) had an arm on the py/ts/rust/go/java tiers but
not on wasm, so the tier refused them by name
(`unsupported builtin method 'is_digit'`) and could not emit the self-host
sources that scan source bytes with them: `selfhost/lexer.rvl` refused on
`is_alnum`, `selfhost/parser.rvl` on `is_digit`, `selfhost/lower.rvl` on
`is_alpha`.

WHAT IS EMITTED NOW. Each classifier lowers to a call of the demand-gated
`$str_char_class` helper: the receiver's u32 byte length must be exactly 1,
then the byte is range/equality-tested for the class (`$class` 0 = digit,
1 = alpha, 2 = alnum, 3 = space). An empty or multi-byte receiver answers 0
— false, never a fault — the same per-character contract the hosted tiers
lower inline (tests/test_str_char_class.py states it). The helper is pulled
into a module only when a rendered body actually calls it, on both emit paths
(the v3 `functions` module and the component module), so every module that
never classifies a char stays byte-identical.

Checked here (emission only — the sandbox has no wasmtime, so execution of the
helper is left to the wasmtime-backed run tests in CI):
  * each method emits `(call $str_char_class)` with its class constant and the
    helper func, on the v3 fn path AND the component provide-method path;
  * the helper WAT is paren-balanced and appears exactly once;
  * the helper is demand-gated: a document that never classifies emits no
    `$str_char_class` at all;
  * `selfhost/lexer.rvl` and `selfhost/parser.rvl` now emit end-to-end through
    the wasm backend, and `selfhost/lower.rvl` gets PAST the classifier
    refusal (its remaining refusal is the Map value type, tracked in #3145 —
    asserted here so this file fails if the classifier refusal ever regresses).
"""

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from revl import RevlError, compile_files, compile_source  # noqa: E402

from _backend_import import backend_emitter  # noqa: E402

EMIT = backend_emitter("wasm")

METHODS = ("is_digit", "is_alpha", "is_alnum", "is_space")
CLASS = {"is_digit": 0, "is_alpha": 1, "is_alnum": 2, "is_space": 3}

FN = "".join(f"fn {m}(c: Str) -> Bool {{ return c.{m}() }}\n" for m in METHODS)

COMPONENT_SRC = """
service Classify {
  fn digit(c: Str) -> Bool
}

component Classifier provides classify: Classify {
  provide classify {
    fn digit(c: Str) -> Bool { return c.is_digit() }
  }
}
"""


def _wat(modules) -> str:
    if isinstance(modules, dict):
        return "\n".join(str(v) for v in modules.values())
    return str(modules)


def _strip(s: str) -> str:
    s = re.sub(r";;[^\n]*", "", s)
    return re.sub(r'"(?:[^"\\]|\\.)*"', '""', s)


# ------------------------------------------------------------- v3 fn path

@pytest.mark.parametrize("m", METHODS)
def test_v3_fn_emits_char_class_call(m):
    wat = _wat(EMIT.emit(compile_source(
        f"fn f(c: Str) -> Bool {{ return c.{m}() }}\n", "t.rvl")))
    assert "(call $str_char_class)" in wat
    assert f"(i32.const {CLASS[m]})" in wat
    assert "(func $str_char_class" in wat


def test_all_four_in_one_module_emit_one_helper():
    wat = _wat(EMIT.emit(compile_source(FN, "t.rvl")))
    assert wat.count("(call $str_char_class)") == 4
    assert wat.count("(func $str_char_class") == 1
    for cls in (0, 1, 2, 3):
        assert f"(i32.const {cls})" in wat


def test_helper_wat_is_paren_balanced():
    helper = EMIT._V3Emitter._helper_str_char_class(
        EMIT._V3Emitter.__new__(EMIT._V3Emitter))
    stripped = _strip(helper)
    assert stripped.count("(") == stripped.count(")")
    assert stripped.lstrip().startswith("(func $str_char_class")


def test_module_paren_balanced_when_classifiers_used():
    wat = _strip(_wat(EMIT.emit(compile_source(FN, "t.rvl"))))
    assert wat.count("(") == wat.count(")")


# ------------------------------------------------------- component path

def test_component_module_emits_helper_on_demand():
    modules = EMIT.emit(compile_source(COMPONENT_SRC, "t.rvl"))
    comp = modules["Classifier"]
    assert "(call $str_char_class)" in comp
    assert "(func $str_char_class" in comp
    assert "(memory" in comp  # the helper reads linear memory


# ----------------------------------------------------------- demand gate

def test_helper_absent_when_no_classifier_used():
    wat = _wat(EMIT.emit(compile_source(
        "fn g(a: Int) -> Int { return a + 1 }\n", "t.rvl")))
    assert "$str_char_class" not in wat


def test_helper_absent_for_reader_only_module():
    # a Str module that only splits/joins keeps a byte-identical preamble
    wat = _wat(EMIT.emit(compile_source(
        'fn words(s: Str) -> List[Str] { return s.split(" ") }\n'
        'fn joined(xs: List[Str]) -> Str { return xs.join(",") }\n', "t.rvl")))
    assert "$str_split" in wat and "$str_join" in wat
    assert "$str_char_class" not in wat


# ------------------------------------------------------ self-host files

def test_selfhost_lexer_and_parser_emit_end_to_end():
    for name in ("lexer.rvl", "parser.rvl"):
        ir = compile_files([str(ROOT / "selfhost" / name)])
        wat = _wat(EMIT.emit(ir))
        assert "(call $str_char_class)" in wat, name
        assert "(func $str_char_class" in wat, name
        stripped = _strip(wat)
        assert stripped.count("(") == stripped.count(")"), name


def test_selfhost_lower_gets_past_the_classifier_refusal():
    # lower.rvl's first refusal used to be `ty_word_char: unsupported builtin
    # method 'is_alpha'`; it must now be the Map value type (#3145) instead.
    with pytest.raises((RevlError, EMIT.EmitError)) as excinfo:
        EMIT.emit(compile_files([str(ROOT / "selfhost" / "lower.rvl")]))
    msg = str(excinfo.value)
    assert "unsupported builtin method" not in msg
    assert "Map" in msg
