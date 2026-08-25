"""The self-hosted cordis-py EMITTER (selfhost/emit_py.rvl, roadmap items
146/174 — Path B slice 1): compiled by revl, emitted through the python backend,
executed, and cross-checked BYTE-FOR-BYTE against the reference emitter
(backends/python/emit.py's ``emit``) over a corpus of interchange-IR documents.

This is the first proof that revl can emit ITSELF. It has the exact shape of
tests/test_selfhost_{lexer,parser,checker,lower}.py: two independent
implementations of one lowering — the reference backend and its revl port — are
forced to agree. Here the agreement is the strongest kind an emitter can be held
to: the emitted Python source must be identical to the last byte. The reference
is ground truth; any divergence is a defect in the slice.

Covered subset (what emits byte-identical) — the FUNCTION-ONLY document:
  * the module scaffold (generated header, the always-emitted ``_revl_field``
    helper, empty ``SERVICES``/``COMPONENTS`` trailers);
  * the gated arithmetic preludes (i64/i32 overflow traps, IEEE ``_revl_div``);
  * ``_emit_functions`` -> ``_fn_stmt`` -> ``_expr`` for the base surface:
    let/assign, return, if/while/for, expr, assert; and the expression algebra
    lit, var, bin (incl ``??``, bounded ``+ - *``, ``/``, truncated ``%``), un,
    call, field, index, ternary-if, record, list, len, the stdlib builtins,
    maplit, sync arrow, match, record-update, string interpolation, opt
    field/call.

Deliberately OUT of this slice (excluded from the corpus, deferred to Path B
slice 2+): components/services (the ``_ComponentEmitter``), type declarations
(``_emit_types``), externs, in-file ``test``/``fault_test`` emission, the
built-in Result classes, the canonical Float->Str (``_revl_ftoa``) interpolation
helper, host roots (``Map``/``Pool``/``Job``) and the ``from runtime import``
line, async coloring, spawn/templates, and the canonical ABI. ``let_pattern``
(destructuring) is a permanent exclusion for a byte oracle: the reference names
its temporary from ``id(node)``, which a second implementation cannot reproduce.
"""

import importlib.util
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_files  # noqa: E402

CORPUS_DIR = ROOT / "tests" / "fixtures" / "emit_py_corpus"
CORPUS = [
    "arith.rvl",       # bounded int/int32, division/modulo, comparisons, unary
    "strings.rvl",     # the stdlib string builtins and `${…}` interpolation
    "control.rvl",     # while/for/if, match (Some/None/wildcard), sync arrow
    "records.rvl",     # record literal, functional record update, list literal
    "optionals.rvl",   # optional-call chaining (opt receiver)
    "mixed.rvl",       # a cross-section of the above in three functions
]


def _load_reference_emit():
    """The reference emitter, loaded by path so we compare against the exact
    file this slice mirrors (not whatever `revl` re-exports)."""
    spec = importlib.util.spec_from_file_location(
        "pyemit_reference", ROOT / "backends" / "python" / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _exec_emitted() -> dict:
    """Compile selfhost/emit_py.rvl, emit python, exec it. The file's component
    wrapper makes the emitted module `from runtime import …`; the pure emitter
    functions under test never touch it, so a lazy stub suffices (as in the
    other self-host stage tests)."""
    ir = compile_files([str(ROOT / "selfhost" / "emit_py.rvl")])
    assert ir["ir_version"] == 3
    spec = importlib.util.spec_from_file_location(
        "pyemit_selfhost_backend", ROOT / "backends" / "python" / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    stub = types.ModuleType("runtime")
    stub.__getattr__ = lambda name: (lambda *a, **k: None)  # PEP 562
    had_runtime = "runtime" in sys.modules
    previous = sys.modules.get("runtime")
    sys.modules["runtime"] = stub
    try:
        namespace = {}
        exec(compile(module.emit(ir), "selfhost_emit_py.py", "exec"), namespace)
    finally:
        if had_runtime:
            sys.modules["runtime"] = previous
        else:
            del sys.modules["runtime"]
    return namespace


@pytest.fixture(scope="module")
def emitted():
    return _exec_emitted()


@pytest.fixture(scope="module")
def reference():
    return _load_reference_emit()


@pytest.mark.parametrize("rel", CORPUS)
def test_selfhosted_emitter_is_byte_identical(emitted, reference, rel):
    """The self-hosted emitter's Python output == the reference's, byte-for-byte,
    for every interchange-IR document in the covered subset."""
    ir = compile_files([str(CORPUS_DIR / rel)])
    want = reference.emit(ir)
    got = emitted["emit_src"](ir)
    assert got == want, (
        f"self-hosted emitter diverged from the reference on {rel}\n"
        f"--- lengths ref={len(want)} got={len(got)} ---"
    )


def test_selfhosted_emitter_output_is_executable_python(emitted, reference):
    """A byte-identical output is trivially valid, but pin it: the emitted
    module for a corpus program compiles and its functions run."""
    ir = compile_files([str(CORPUS_DIR / "arith.rvl")])
    src = emitted["emit_src"](ir)
    ns: dict = {}
    exec(compile(src, "arith_emitted.py", "exec"), ns)
    assert ns["i64ops"](3, 4) == 3 + 4 - 3 * 4
    assert ns["divmod"](7, 2) == (
        7 // 2 + 7 // 2 + 7 // 2 + 7 % 2 + 7 % 2  # all same sign here
    )


def test_selfhosted_emitter_in_file_tests_pass(emitted):
    """The .rvl file's own `test` blocks run under the python backend."""
    tests = emitted.get("REVL_TESTS")
    assert tests and len(tests) >= 4, "expected the file's test blocks in REVL_TESTS"
    for entry in tests:
        fn = entry[-1] if isinstance(entry, tuple) else entry
        fn()
