"""Default parameters for `fn` (roadmap item 187).

A parameter may declare a default value (`fn f(a: Int, b: Int = 0)`), and a
call may omit trailing defaulted arguments. Defaults are resolved at the CALL
SITE during lowering: the omitted trailing arguments are filled with each
parameter's default *expression*, so every emitter sees a fully-supplied
argument list and needs no per-tier default machinery (verified cross-tier
below). Existing programs — no defaults anywhere — emit byte-identically; that
invariant is guarded by the golden suites (tests/test_goldens.py and the
per-backend emit suites), which a `= <expr>`-free program leaves untouched.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "backends" / "python"))

from _backend_import import backend_emitter  # noqa: E402
from revl import RevlError, compile_source  # noqa: E402

py = backend_emitter("python")
ts = backend_emitter("typescript")


def _run(source):
    ns: dict = {}
    exec(compile(py.emit(compile_source(source)), "emitted.py", "exec"), ns)
    return ns


# ---------------------------------------------------------------------------
# Behavior: a default is used when omitted, overridden when supplied.
# ---------------------------------------------------------------------------

def test_default_used_when_omitted_and_overridden_when_supplied():
    ns = _run(
        """
        fn add(a: Int, b: Int = 10) -> Int {
          return a + b
        }
        fn omitted() -> Int { return add(5) }
        fn supplied() -> Int { return add(5, 1) }
        """
    )
    assert ns["omitted"]() == 15   # default 10 filled
    assert ns["supplied"]() == 6   # explicit 1 overrides


def test_all_parameters_defaulted_call_with_none():
    ns = _run(
        """
        fn greet(n: Int = 1, m: Int = 2) -> Int { return n * 10 + m }
        fn none() -> Int { return greet() }
        fn one() -> Int { return greet(9) }
        fn both() -> Int { return greet(9, 8) }
        """
    )
    assert ns["none"]() == 12
    assert ns["one"]() == 92
    assert ns["both"]() == 98


def test_default_is_an_expression_not_just_a_literal():
    # A default may be any pure expression, evaluated at the call site.
    ns = _run(
        """
        fn f(a: Int, b: Int = 3 * 4 + 1) -> Int { return a + b }
        fn g() -> Int { return f(0) }
        """
    )
    assert ns["g"]() == 13


# ---------------------------------------------------------------------------
# Cross-tier: the omitted argument is filled with the default in BOTH the
# python and typescript emitters, with no per-tier default handling — the
# emitted call site simply carries the default expression as a written arg.
# ---------------------------------------------------------------------------

def test_omitted_default_emits_as_written_argument_cross_tier():
    source = """
        fn add(a: Int, b: Int = 0) -> Int { return a + b }
        fn demo() -> Int { return add(5) + add(5, 10) }
        """
    ir = compile_source(source)
    py_out = py.emit(ir)
    ts_out = ts.emit(ir)
    # python: the default `0` is written into the omitted slot.
    assert "add(5, 0)" in py_out
    assert "add(5, 10)" in py_out
    # typescript: same call-site fill (bigint literals).
    assert "add(5n, 0n)" in ts_out
    assert "add(5n, 10n)" in ts_out


def test_default_used_from_a_component_body():
    # The component provide-method call path fills defaults too (not only the
    # pure-fn path), so a service handler can omit a trailing defaulted arg.
    source = """
        service Doubler {
          fn compute(n: Int) -> Int
        }
        component Impl provides d: Doubler {
          provide d {
            fn compute(n) = scale(n)
          }
        }
        fn scale(x: Int, factor: Int = 2) -> Int { return x * factor }
        """
    py_out = py.emit(compile_source(source))
    assert "scale(n, 2)" in py_out


# ---------------------------------------------------------------------------
# Byte-identity: a program with no defaults compiles to the exact same IR as
# before item 187 (the golden suites prove the emitted text; here we pin that
# the signature table's new keys never perturb a default-free call site).
# ---------------------------------------------------------------------------

def test_default_free_program_unaffected():
    source = """
        fn add(a: Int, b: Int) -> Int { return a + b }
        fn demo() -> Int { return add(1, 2) }
        """
    py_out = py.emit(compile_source(source))
    assert "add(1, 2)" in py_out


# ---------------------------------------------------------------------------
# Rejections.
# ---------------------------------------------------------------------------

def test_reject_required_after_defaulted():
    with pytest.raises(RevlError) as e:
        compile_source(
            """
            fn f(a: Int = 0, b: Int) -> Int { return a + b }
            fn g() -> Int { return f(1, 2) }
            """
        )
    assert "no default but follows a defaulted parameter" in str(e.value)


def test_reject_effectful_default():
    with pytest.raises(RevlError) as e:
        compile_source(
            """
            extern emission fn record(m: Str) -> Int = @python { }
            fn f(a: Int, b: Int = record("boot")) -> Int { return a + b }
            fn g() -> Int { return f(1) }
            """
        )
    msg = str(e.value)
    assert "record" in msg and "effectful" in msg


def test_reject_default_type_mismatch():
    with pytest.raises(RevlError) as e:
        compile_source(
            """
            fn f(a: Int, b: Int = "x") -> Int { return a }
            fn g() -> Int { return f(1) }
            """
        )
    assert "default for parameter `b`" in str(e.value)


def test_reject_too_few_required_arguments():
    with pytest.raises(RevlError) as e:
        compile_source(
            """
            fn f(a: Int, b: Int, c: Int = 0) -> Int { return a + b + c }
            fn g() -> Int { return f(1) }
            """
        )
    # required=2, so 1 argument is below the accepted 2..3 window.
    assert "takes 2 to 3 argument(s), 1 given" in str(e.value)


def test_reject_too_many_arguments():
    with pytest.raises(RevlError) as e:
        compile_source(
            """
            fn f(a: Int, b: Int = 0) -> Int { return a + b }
            fn g() -> Int { return f(1, 2, 3) }
            """
        )
    assert "takes 1 to 2 argument(s), 3 given" in str(e.value)
