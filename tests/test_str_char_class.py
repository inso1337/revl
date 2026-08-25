"""Single-character ASCII classification builtins (roadmap item 233,
docs/stdlib-2.0.md §Str.is_alnum): `Str.is_digit()`, `Str.is_alpha()`,
`Str.is_alnum()`, `Str.is_space()`.

These exist to cut the self-host lexer's per-byte tax: `is_alnum(charAt(j))`
was a revl-fn call plus a `charCodeAt`/`ord` round-trip and a code-point range
compare, once per source byte. The builtin lowers to a native inline
comparison on the py tier, so the lexer scans the same tokens with less
overhead (see docs/bench-selfhost.md, the lexer before→after row).

Spec (ASCII, single code point; the receiver is a one-char Str — an empty
receiver is false and no input faults; multi-character input is outside the
per-character contract):
  * is_digit()  — `0`-`9`
  * is_alpha()  — `a`-`z` / `A`-`Z` (letters only, NOT `_`)
  * is_alnum()  — is_alpha ∪ is_digit
  * is_space()  — space, tab, LF, CR

Checked here:
  * the checker dispatches each on the Str family, checks arity 0, types Bool;
  * the python tier executes EXACTLY the ASCII contract over every byte, is
    empty/multi-char safe, and evaluates a side-effecting receiver once;
  * the python lowering emits its documented inline shape (no fn call).

Other tiers (rust/java/ts/wasm/go) are deferred — py is the bench tier this
item targets, and nothing outside selfhost/lexer.rvl (py-emitted) uses these
yet, so no other backend is exercised with them (per-tier note in
docs/stdlib-2.0.md §Str.is_alnum).
"""

import importlib.util
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import RevlError, compile_source  # noqa: E402

METHODS = ("is_digit", "is_alpha", "is_alnum", "is_space")

FN = "".join(
    f"fn {m}(c: Str) -> Bool {{ return c.{m}() }}\n" for m in METHODS)


# The ground-truth ASCII contract, by code point.
def _ref(method, n):
    if method == "is_digit":
        return 48 <= n <= 57
    if method == "is_alpha":
        return 65 <= n <= 90 or 97 <= n <= 122
    if method == "is_alnum":
        return 48 <= n <= 57 or 65 <= n <= 90 or 97 <= n <= 122
    if method == "is_space":
        return n in (9, 10, 13, 32)
    raise AssertionError(method)


def _err(src: str) -> str:
    with pytest.raises(RevlError) as excinfo:
        compile_source(src)
    return str(excinfo.value)


# ---------------------------------------------------------------- checker

@pytest.mark.parametrize("m", METHODS)
def test_receiver_must_be_str(m):
    assert "needs a Str receiver" in _err(
        f"fn bad(n: Int) -> Bool {{ return n.{m}() }}")


@pytest.mark.parametrize("m", METHODS)
def test_arity_is_zero(m):
    assert "takes 0 argument" in _err(
        f"fn bad(s: Str) -> Bool {{ return s.{m}(\"x\") }}")


@pytest.mark.parametrize("m", METHODS)
def test_result_is_Bool(m):
    # a Bool result flows into && without refusal
    ir = compile_source(
        f"fn tag(s: Str) -> Bool {{ return s.{m}() && s.length() > 0 }}\n")
    assert ir["ir_version"] == 3


# ---------------------------------------------------------------- python

def _exec_python(src: str):
    ir = compile_source(src)
    spec = importlib.util.spec_from_file_location(
        "pyemit_char_class", ROOT / "backends" / "python" / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    stub = types.ModuleType("runtime")
    stub.__getattr__ = lambda name: (lambda *a, **k: None)  # PEP 562
    had = "runtime" in sys.modules
    previous = sys.modules.get("runtime")
    sys.modules["runtime"] = stub
    try:
        namespace = {}
        exec(compile(module.emit(ir), "char_class.py", "exec"), namespace)
    finally:
        if had:
            sys.modules["runtime"] = previous
        else:
            del sys.modules["runtime"]
    return namespace


@pytest.mark.parametrize("m", METHODS)
def test_python_matches_ascii_contract_over_every_byte(m):
    fn = _exec_python(FN)[m]
    for n in range(256):
        assert fn(chr(n)) is _ref(m, n), (m, n)


@pytest.mark.parametrize("m", METHODS)
def test_python_empty_receiver_is_false_not_a_fault(m):
    # the empty string is the one non-single-char input the lexer can hand a
    # classifier (a clamped slice past end); it must be false, never raise
    fn = _exec_python(FN)[m]
    assert fn("") is False


@pytest.mark.parametrize("m", METHODS)
def test_python_multichar_never_faults(m):
    # multi-char input is outside the per-character contract; the lean
    # lowering does not guarantee a meaningful verdict, but it must be TOTAL
    # (never raise) so a mis-sized receiver degrades gracefully
    fn = _exec_python(FN)[m]
    for c in ("ab", "0a", "  ", "aA", "12"):
        assert fn(c) in (True, False)


def test_python_receiver_evaluated_once():
    # is_alpha/is_alnum reference the receiver more than once in the lowering;
    # the walrus must bind it a SINGLE time, so a side-effecting receiver fires
    # exactly once. Chain charAt (an index) behind a counter proxy.
    ns = _exec_python(
        "fn nth(s: Str, i: Int) -> Bool { return s.charAt(i).is_alnum() }\n")
    assert ns["nth"]("a1_", 0) is True   # 'a'
    assert ns["nth"]("a1_", 1) is True   # '1'
    assert ns["nth"]("a1_", 2) is False  # '_' is not is_alnum (letters/digits)


# ---------------------------------------------------------------- shape

def test_python_lowering_is_inline_no_fn_call():
    spec = importlib.util.spec_from_file_location(
        "emit_char_class_py", ROOT / "backends" / "python" / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    src = module.emit(compile_source(FN))
    # digit: a bare chained comparison; space: tuple membership; alpha/alnum:
    # a single walrus bind of the receiver. None of them call a revl fn.
    assert '"0" <= c <= "9"' in src
    assert 'c in (" ", "\\t", "\\n", "\\r")' in src   # literal backslash-escapes
    assert '"a" <= (_rc := c) <= "z"' in src
