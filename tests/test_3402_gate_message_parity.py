"""The self-host gate spells the reference's tag and message on the three
#3402 shapes: cache-on-plain-fn, a trailing token after a shorthand body, and
a plain string carrying `${...}`.

Every REFUSAL case asserts the gate's `admit_src` wire is
`<expected tag>|<the reference's exact message>` - the message compared live
against the reference compiler's `RevlError.message`, so a wording drift on
either side fails here. The tag is the gate-vocabulary tag: G4 where the
reference sets `code="G4"`, G7 for the verified-totality message (which
`diagnostics.classify` names G7), BAD for the code-less parse/lexer-stage
refusals, where the gate's wire vocabulary is the parse stage's.

ADMISSION cases assert both trees admit, so the new messages cannot become
false refusals.
"""

import importlib.util
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402
from _compiled_ir import compiled_ir  # noqa: E402
from revl.errors import RevlError  # noqa: E402


def _emitted() -> dict:
    """`selfhost/lower.rvl` compiled and run under the python backend - the
    same harness `tests/test_selfhost_lower.py` uses."""
    ir = compiled_ir(ROOT / "selfhost" / "lower.rvl")
    spec = importlib.util.spec_from_file_location(
        "pyemit_selfhost_lower_3402", ROOT / "backends" / "python" / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    stub = types.ModuleType("runtime")
    stub.__getattr__ = lambda name: (lambda *a, **k: None)  # PEP 562
    had = "runtime" in sys.modules
    previous = sys.modules.get("runtime")
    sys.modules["runtime"] = stub
    try:
        namespace = {}
        exec(compile(module.emit(ir), "selfhost_lower.py", "exec"), namespace)
    finally:
        if had:
            sys.modules["runtime"] = previous
        else:
            del sys.modules["runtime"]
    return namespace


@pytest.fixture(scope="module")
def gate():
    return _emitted()["admit_src"]


def _ref_message(src: str) -> str:
    """The reference's refusal message; fails the test if it admits."""
    with pytest.raises(RevlError) as excinfo:
        compile_source(src, "case.rvl")
    return excinfo.value.message


# ------------------------------------------------------------- case 1: cache
# `cache capability`/`cache external` on a plain `fn` is G4; the freshness
# rules run before the class check (reference `_check_cache_freshness` then
# `_check_cache_declarations`); a recursive `verified fn` reports G7 totality
# first because `_lower_fns` runs before the cache phase.

REFUSALS = [
    ("cache-capability-plain", "G4",
     "fn f(x: Int) -> Int cache capability { return x }\n"),
    ("cache-external-plain-no-bound", "G4",
     "fn f(x: Int) -> Int cache external { return x }\n"),
    ("cache-external-plain-ttl", "G4",
     "fn f(x: Int) -> Int cache external ttl 5m { return x }\n"),
    ("cache-external-plain-inval", "G4",
     "fn f(x: Int) -> Int cache external invalidated_by db.write { return x }\n"),
    ("cache-capability-with-inval", "G4",
     "fn f(x: Int) -> Int cache capability invalidated_by db.write { return x }\n"),
    ("cache-pure-ttl", "G4",
     "fn f(x: Int) -> Int cache pure ttl 5m { return x }\n"),
    ("cache-pure-inval", "G4",
     "fn f(x: Int) -> Int cache pure invalidated_by db { return x }\n"),
    ("cache-unknown-class", "BAD",
     "fn f(x: Int) -> Int cache forever = x\n"),
    ("cache-capability-verified-recursive", "G7",
     "verified fn f(x: Int) -> Int cache capability { return f(x) }\n"),
    # ------------------------------------------- case 2: trailing token
    ("trailing-paren", "BAD", "fn f(x: Int) -> Int = x + 1 )\n"),
    ("trailing-ident", "BAD", "fn f(x: Int) -> Int = x y\n"),
    ("trailing-brace", "BAD", "fn f(x: Int) -> Int = f(x) }\n"),
    ("trailing-int", "BAD", "fn f(x: Int) -> Int = x + 1 42\n"),
    ("trailing-after-block", "BAD", "fn f(x: Int) -> Int { return x } )\n"),
    # ---------------------------------- case 3: plain string with `${...}`
    ("dollar-verified-shorthand", "BAD",
     'verified fn f(n: Int) -> Str = "${f(n)}"\n'),
    ("dollar-plain-shorthand", "BAD", 'fn f(n: Int) -> Str = "${n}"\n'),
    ("dollar-in-block-body", "BAD", 'fn f(n: Int) -> Str { return "${n}" }\n'),
]

ADMISSIONS = [
    ("cache-pure-plain", "fn f(x: Int) -> Int cache pure { return x }\n"),
    ("shorthand-plain", "fn f(x: Int) -> Int = x + 1\n"),
    ("plain-string-no-dollar", 'fn f() -> Str = "hi there"\n'),
    # a backtick in the string makes it DATA quoting template source, so the
    # reference's `_reject_dollar_interpolation` deliberately does not fire
    ("backtick-string-with-dollar", 'fn f() -> Str = "a `x` ${y}"\n'),
    # `${` without a closing brace is not a complete interpolation shape
    ("incomplete-dollar", 'fn f() -> Str = "cost: ${"\n'),
]


@pytest.mark.parametrize("name,tag,src", REFUSALS, ids=[r[0] for r in REFUSALS])
def test_gate_wire_matches_the_reference_message(gate, name, tag, src):
    msg = _ref_message(src)
    assert gate(src) == f"{tag}|{msg}", (name, gate(src), msg)


@pytest.mark.parametrize("name,src", ADMISSIONS, ids=[a[0] for a in ADMISSIONS])
def test_gate_admits_what_the_reference_admits(gate, name, src):
    compile_source(src, "case.rvl")
    assert gate(src) == "", (name, gate(src))


def test_cache_clause_still_steps_the_declaration_after_it(gate):
    """The clause reader consumes `cache ... invalidated_by a.b, c ttl 1d`,
    so the NEXT declaration is reached - a clause left unconsumed would turn
    into a false `expected a top-level declaration` on the following fn."""
    src = ("fn f(x: Int) -> Int cache external invalidated_by db.write, net ttl 1h { return x }\n"
           "fn g(y: Str) -> Str { return y }\n")
    msg = _ref_message(src)
    # the reference refuses on the FIRST fn's cache clause (plain fn), and
    # the gate must name that, not the second declaration
    assert "cache external" in msg and "not allowed on a plain" in msg
    assert gate(src) == f"G4|{msg}"
