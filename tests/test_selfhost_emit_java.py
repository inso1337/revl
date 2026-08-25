"""The self-hosted cordis4j EMITTER (selfhost/emit_java.rvl, roadmap item 199 —
Path B slice 1 for the Java tier): compiled by revl, emitted through the python
backend, executed, and cross-checked BYTE-FOR-BYTE against the reference emitter
(backends/java/emit.py's ``emit``) over a corpus of interchange-IR documents.

This has the exact shape of tests/test_selfhost_emit_rust.py: two independent
implementations of one lowering — the reference Java backend and its revl port —
are forced to agree, and the agreement is the strongest an emitter can be held
to: the emitted Java source must be identical to the last byte. The reference is
ground truth; any divergence is a defect in the slice. There is no JRE in play —
the check compares the EMITTED SOURCE STRINGS (a pure-Python comparison), so it
needs no Java toolchain.

Every IR the frontend produces is ir_version 3, so the covered corpus is the
corner of the reference's v3 path (``_emit_v3`` -> ``_emit_v3_functions`` ->
``_expr`` / ``_v3_stmt``) that emits byte-identical with only the module scaffold
and the free-function bodies.

Covered subset (what emits byte-identical):
  * the module scaffold — the two banner comments, ``package revl;``, the five
    unconditional ``import io.cordis4j.core.*;`` lines, and the
    ``public final class Components { private Components() {} }`` wrapper;
  * ``_emit_v3_functions`` — each module fn as a ``public static`` method with
    ``_java_v3_type`` for scalar / ``List`` / ``Opt`` / ``Map`` / ``Result``
    parameter and return types (boxed in type-argument position), ``_fn_name``
    keyword renaming, and ``// (empty body)`` for an empty body;
  * ``_v3_stmt`` — let/assign (with the ``_let_keyword`` ``final var`` / ``var``
    / bare ``java.util.List`` choice), return, if/while/for, the bare-expr
    statement, and assert;
  * ``_expr`` — lit, name, var (incl the ``None`` -> ``Optional.empty()``
    reference), bin (``??`` via ``.orElseGet``, ``==``/``!=`` via
    ``java.util.Objects.equals``, the trapping ``Math.addExact/subtractExact/
    multiplyExact`` for Int/Int32, ``/``, ``%``, comparisons, ``&&``/``||``,
    native string ``+``), un (incl ``Math.negateExact``), the 2.0
    ``callee``/``args`` call and the ``fn`` call with the ``Some``/``None``
    constructors, the ``widen`` markers, field, index, ternary-if, list, the
    empty map literal, and non-float ``${..}`` interpolation via
    ``String.valueOf``.

Deliberately OUT (excluded from the corpus, deferred to Java Path B slice 2+):
components/services entirely (``_emit_component*``, service interfaces, plugin
ctors, ``provide``/``req``/``config``, the component-dialect expression kinds,
and the ``_core_imports`` growth they drive); the v3 typed-core (user ``type``
decls, record literals / functional update, ADT construction and ``match`` over
user variants); the HOST Map/generics surface (``_emit_host_stubs``'
``HashMap<String,V>`` with per-site value-type inference — the plain ``maplit``
and ``Map[K,V]`` type lowering ARE covered); the stdlib surface (every
``builtin``/``len`` node and ``_emit_stdlib_helpers``); the built-in Result
surface (``Ok``/``Err`` and ``_emit_result_type``, plus ``_emit_checked_div_
helpers``); async coloring / spawn / instances / externs / in-file ``test`` /
lifecycle-test emission; the canonical Float->Str ``revlFtoa`` (so float
interpolation is excluded); the ``_reject_fn_type`` refusal; local ``let``-bound
arrows and their ``_inline_arrow`` beta-reduction; and ``let_pattern`` (its temp
name is ``__revl_destructure_{id(node)}``, a host-object identity no port can
reproduce).
"""

import importlib.util
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_files  # noqa: E402

CORPUS_DIR = ROOT / "tests" / "fixtures" / "emit_java_corpus"
CORPUS = [
    "arith.rvl",     # bounded int/int32, /, %, comparisons, ==/!=, unary, ??
    "control.rvl",   # let/var/assign, if/else, while, for, bare-expr, assert
    "calls.rvl",     # free-function calls
    "strings.rvl",   # string `+`, `${..}` interpolation, literals
    "lists.rvl",     # list literal, index, nested list types
    "maps.rvl",      # the empty map literal and the Map/List generic type lowering
]


def _load_reference_emit():
    """The reference emitter, loaded by path so we compare against the exact
    file this slice mirrors (not whatever `revl` re-exports)."""
    spec = importlib.util.spec_from_file_location(
        "javaemit_reference", ROOT / "backends" / "java" / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _exec_emitted() -> dict:
    """Compile selfhost/emit_java.rvl, emit python, exec it. The file's component
    wrapper makes the emitted module `from runtime import …`; the pure emitter
    functions under test never touch it, so a lazy stub suffices (as in the
    other self-host stage tests)."""
    ir = compile_files([str(ROOT / "selfhost" / "emit_java.rvl")])
    assert ir["ir_version"] == 3
    spec = importlib.util.spec_from_file_location(
        "javaemit_selfhost_backend", ROOT / "backends" / "python" / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    stub = types.ModuleType("runtime")
    stub.__getattr__ = lambda name: (lambda *a, **k: None)  # PEP 562
    had_runtime = "runtime" in sys.modules
    previous = sys.modules.get("runtime")
    sys.modules["runtime"] = stub
    try:
        namespace = {}
        exec(compile(module.emit(ir), "selfhost_emit_java.py", "exec"), namespace)
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
    """The self-hosted emitter's Java output == the reference's, byte-for-byte,
    for every interchange-IR document in the covered subset."""
    ir = compile_files([str(CORPUS_DIR / rel)])
    want = reference.emit(ir)
    got = emitted["emit_src"](ir)
    assert got == want, (
        f"self-hosted emitter diverged from the reference on {rel}\n"
        f"--- lengths ref={len(want)} got={len(got)} ---"
    )


def test_selfhosted_emitter_output_scaffold(emitted):
    """A byte-identical output is trivially valid Java source; pin the scaffold
    and a representative body detail so a regression in the header or the
    trapping-arithmetic lowering surfaces here, not only in the byte diff."""
    ir = compile_files([str(CORPUS_DIR / "arith.rvl")])
    src = emitted["emit_src"](ir)
    assert src.startswith(
        "// Generated by the revl cordis4j backend (ir_version 3) — do not edit.")
    assert "public final class Components {" in src
    assert "import io.cordis4j.core.ServiceKey;" in src
    assert "Math.addExact(a, b)" in src
    assert src.endswith("}\n")


def test_selfhosted_emitter_in_file_tests_pass(emitted):
    """The .rvl file's own `test` blocks run under the python backend."""
    tests = emitted.get("REVL_TESTS")
    assert tests and len(tests) >= 3, "expected the file's test blocks in REVL_TESTS"
    for entry in tests:
        fn = entry[-1] if isinstance(entry, tuple) else entry
        fn()
