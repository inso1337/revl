"""A host body cannot leave the function the emitter splices it into.

One shared scanner, five reports from the 2026-11-07 security review at
cf33394e8:

* #3130: a `}` in a `//` comment of an `@ts` body closes the function under
  JavaScript's own rules even though the revl lexer counts it as text, so the
  rest of the body would be emitted at module scope and run at load;
* #3162: the same for `@rs`, `@java` and `@go`;
* #3164: an extra `)` in an `@wasm` body closes the function under WAT's
  rules; the fields after it become module fields, including a `(start ...)`
  that runs at instantiation;
* #3131: `str.splitlines()` broke a `@py`/`@ts` body on U+0085, U+2028 and the
  C0 separators, emitting the text after the separator as a statement;
* #3166: the same split for `@rs`, `@java` and `@go`.

Every test asserts the emitted module cannot contain the escape: the tier
refuses the body through its own `EmitError`, or, for the python separator
case, keeps the text on the line the author wrote. The positive controls keep
the scanner from passing by refusing every body.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from revl.compiler import compile_source  # noqa: E402
from _backend_import import backend_emitter  # noqa: E402

_TIERS = {
    "ts": "typescript",
    "rs": "rust",
    "java": "java",
    "go": "go",
    "py": "python",
    "wasm": "wasm",
}


def _emit(tier: str, source: str) -> str | dict:
    emitter = backend_emitter(_TIERS[tier])
    return emitter.emit(compile_source(source, f"host-body-{tier}.rvl"))


def _refusal(tier: str, source: str) -> str:
    emitter = backend_emitter(_TIERS[tier])
    with pytest.raises(emitter.EmitError) as excinfo:
        emitter.emit(compile_source(source, f"host-body-{tier}.rvl"))
    return str(excinfo.value)


# --------------------------------------------------------------------- #3130
_TS_BRACE_ESCAPE = '''extern pure fn dead(x: Int) -> Int = @ts { // { {
  return x })())
  }
  globalThis.PWNED = "module-load"
  export function g2() { return revlToBigInt((() => { // } }
} = @py { return x }
pub fn ok(a: Int) -> Int = a
'''


def test_3130_ts_body_that_closes_its_function_is_refused():
    message = _refusal("ts", _TS_BRACE_ESCAPE)
    assert "extern `dead`" in message
    assert "brace-balanced" in message
    assert "JavaScript" in message


def test_3130_a_balanced_ts_body_still_emits():
    out = _emit(
        "ts",
        'extern pure fn f(s: Str) -> Bool = @ts { return /a{2}/.test(s) }\n'
        'pub fn g(a: Str) -> Bool = f(a)\n',
    )
    assert "function f(s: string): boolean {" in out
    assert "return /a{2}/.test(s)" in out


# --------------------------------------------------------------------- #3162
_BRACE_ESCAPES = {
    # `// {` hides the opening brace from the host; `return x }` closes the
    # emitted function, and the fields after it land at module scope.
    "rs": "// {\n"
          "  return x }\n"
          "  #[used] static PWNED: u8 = 1;\n"
          "  fn pad() -> i64 { return 0; // }\n",
    "java": "// {\n"
            "  return x; }\n"
            '  static { System.out.println("EVIL"); }\n'
            "  static long pad() { return 0; // }\n",
    "go": "// {\n"
          "  return x }\n"
          '  func init() { println("EVIL"); }\n'
          "  func pad() int64 { return 0 // }\n",
}


@pytest.mark.parametrize("tier", ["rs", "java", "go"])
def test_3162_brace_in_a_line_comment_is_refused(tier):
    source = (
        f"extern pure fn dead(x: Int) -> Int = @{tier} {{ "
        f"{_BRACE_ESCAPES[tier]}}}\n"
        "pub fn use_it(a: Int) -> Int = dead(a)\n"
    )
    message = _refusal(tier, source)
    assert f"@{tier} host body" in message
    assert "brace-balanced" in message


@pytest.mark.parametrize("tier", ["rs", "java", "go"])
def test_3162_a_balanced_body_still_emits(tier):
    source = (
        f"extern pure fn f(x: Int) -> Int = @{tier} {{ "
        "// a comment with no structural braces\n  return x + 1 }\n"
        "pub fn g(a: Int) -> Int = f(a)\n"
    )
    _emit(tier, source)


# --------------------------------------------------------------------- #3164
_WASM_PAREN_ESCAPE = '''extern pure fn dead(x: Int) -> Int = @wasm {
  (local.get $p_x))
  (global $ran (export "ran") (mut i32) (i32.const 0))
  (func $init (global.set $ran (i32.const 1)))
  (start $init)
  (func $pad (result i64) (i64.const 0)
}
pub fn use_it(a: Int) -> Int = dead(a)
'''


def test_3164_wasm_body_with_an_extra_close_paren_is_refused():
    message = _refusal("wasm", _WASM_PAREN_ESCAPE)
    assert "extern `dead`" in message
    assert "parenthesis-balanced" in message
    assert "WAT" in message


def test_3164_a_balanced_wasm_body_still_emits():
    out = _emit(
        "wasm",
        "extern pure fn f(x: Int) -> Int = @wasm { "
        "(i64.add (local.get $p_x) (i64.const 1)) }\n"
        "pub fn g(a: Int) -> Int = f(a)\n",
    )
    assert "(func $fn.f" in out["functions"]


# --------------------------------------------------------------------- #3131
@pytest.mark.parametrize("separator", ["\u2028", "\u0085", "\x0b"])
def test_3131_py_comment_separator_is_not_split_into_a_statement(separator):
    source = (
        "extern pure fn note(x: Int) -> Int = @py {\n"
        f'  # harmless note{separator}import os; os.environ["R4_PWNED"] = "ran"\n'
        "  return x\n"
        "}\n"
        "pub fn ok(a: Int) -> Int = note(a)\n"
    )
    out = _emit("py", source)
    assert f"# harmless note{separator}import os" in out
    assert "\n    import os" not in out


@pytest.mark.parametrize("separator", ["\u2028", "\u2029", "\u0085", "\x0b"])
def test_3131_ts_comment_separator_is_refused(separator):
    source = (
        f"extern pure fn note(x: Int) -> Int = @ts {{ "
        f'// harmless note{separator}globalThis.R4_PWNED = "ran"\n'
        "  return x }\n"
        "pub fn ok(a: Int) -> Int = note(a)\n"
    )
    message = _refusal("ts", source)
    assert "outside a string literal" in message
    assert "U+" in message


# --------------------------------------------------------------------- #3166
@pytest.mark.parametrize("tier", ["rs", "java", "go"])
@pytest.mark.parametrize("separator", ["\u2028", "\u0085"])
def test_3166_comment_separator_is_refused(tier, separator):
    source = (
        f"extern pure fn f(x: Int) -> Int = @{tier} {{ "
        f"// c{separator}os.Exit(7)\n"
        "  return x }\n"
        "pub fn ok(a: Int) -> Int = f(a)\n"
    )
    message = _refusal(tier, source)
    assert f"@{tier} host body" in message
    assert "outside a string literal" in message


def test_3166_separator_inside_a_string_literal_is_allowed():
    out = _emit(
        "go",
        'extern pure fn f(x: Int) -> Str = @go { return "a\u2028b" }\n'
        "pub fn g(a: Int) -> Str = f(a)\n",
    )
    assert "a\u2028b" in out


# ------------------------------------------- review of #3313: regex reading
# `o.return / 1` divides in JavaScript (a keyword after `.` is a property
# name), so the `}` closes the extern's function and the assignment runs at
# module load. The last line pads the count back: `/}/` after `if (1)` is a
# regex. The scanner follows both readings of an ambiguous `/`.
_TS_REGEX_READING_ESCAPE = '''extern pure fn dead(x: Int) -> Int = @ts {
  // {
  const r = o.return /1}/1/g
  globalThis.PWNED = "module-load"
  function pad() { if (1) /}/.test("")
}
pub fn use_it(a: Int) -> Int = dead(a)
'''


def test_ts_division_read_as_a_regex_is_refused():
    message = _refusal("ts", _TS_REGEX_READING_ESCAPE)
    assert "extern `dead`" in message
    assert "brace-balanced" in message
    assert "JavaScript" in message


# ----------------------------------------- review of #3313: unicode escapes
# javac decodes `\uXXXX` before it lexes (JLS 3.3), so `}` is a real
# `}`: it closes the extern's method and the static initialiser runs at
# class load.
_JAVA_UNICODE_ESCAPE = (
    "extern pure fn dead(x: Int) -> Int = @java { return x; \\u007d "
    "static \\u007b System.out.println(1); \\u007d "
    "static long pad() \\u007b return 0; }\n"
    "pub fn use_it(a: Int) -> Int = dead(a)\n"
)


def test_java_brace_spelled_as_a_unicode_escape_is_refused():
    message = _refusal("java", _JAVA_UNICODE_ESCAPE)
    assert "extern `dead`" in message
    assert "brace-balanced" in message
    assert "Java" in message


def test_java_even_backslashes_are_not_a_unicode_escape():
    out = _emit(
        "java",
        'extern pure fn f(x: Int) -> Str = @java { return "\\\\u007d"; }\n'
        "pub fn g(a: Int) -> Str = f(a)\n",
    )
    assert '"\\\\u007d"' in out
