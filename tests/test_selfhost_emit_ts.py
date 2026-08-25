"""The self-hosted cordis-v4 (TypeScript) EMITTER (selfhost/emit_ts.rvl, roadmap
item 190 — Path B): compiled by revl, emitted through the python backend,
executed, and cross-checked BYTE-FOR-BYTE against the reference emitter
(backends/typescript/emit.py's ``emit``) over a corpus of interchange-IR
documents.

This is the TypeScript instance of the self-host emit oracle — the exact shape of
tests/test_selfhost_emit_py.py. Two independent implementations of one lowering
(the reference backend and its revl port) are forced to agree, and the agreement
is the strongest kind an emitter can be held to: the emitted TypeScript source
must be identical to the last byte. The reference is ground truth; any divergence
is a defect in the slice.

Navigation reads the IR in PURE revl through stdlib/value.rvl's ``value_*`` (item
180). Only host FORMATTING stays ``@py``: ``json_dumps`` (the reference renders
string/number literals with ``json.dumps``), ``template_text`` (the
template-literal escaper), ``newline``, and ``py_rstrip``/``py_strip``.

Covered subset (what emits byte-identical) — the v3 FUNCTION-ONLY document:
  * module scaffold — the two generated-header comments, the
    ``import type { Context }`` line + ``import { host } from '../runtime.ts'``;
  * the conditional helper preludes (``_revl_helpers``): ``revlEq``, ``revlI64``,
    ``revlI32``, the named-integer-arithmetic block, and the code-point string
    helpers, each gated exactly as the reference gates them;
  * ``_emit_ts_functions`` -> ``_v3_stmt`` -> ``_expr`` for the base surface:
    let/assign (const/let), return, if/while/for, expr; and the 2.0 expression
    algebra — lit, var, bin (incl ``??``, structural ``==`` via revlEq, bounded
    ``+ - *``, true ``/``, truncated ``%``), un, call, field, index, len, stdlib
    builtins, maplit, sync arrow (incl captures IIFE), match (Opt Some/None and
    the tagged switch), record, list, record-update, string interpolation,
    optional field/call; plus the ``Number(...)``/``BigInt(...)`` widen markers
    and the document-wide ``$revl_match_N`` temp counter.

Deliberately OUT (deferred to a follow-on slice): components/services entirely
(the ``_component``/``_provide_impl``/``_method_body`` path, service interfaces,
context augmentation, config interfaces), v3 TYPE declarations
(``_emit_ts_types``), externs, in-file ``test``/``fault_test``/``lifecycle test``
emission, async coloring, spawn/instances, realm placements
(isolate/intercept/routes), the canonical ABI, and ``assert``/``let_pattern``
statements (implementable but not exercised by the function corpus, so deferred
to keep the slice byte-VERIFIED rather than byte-guessed).
"""

import importlib.util
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_files  # noqa: E402

CORPUS_DIR = ROOT / "tests" / "fixtures" / "emit_ts_corpus"
CORPUS = [
    "arith.rvl",       # bounded int/int32, division/modulo, comparisons, unary
    "strings.rvl",     # the stdlib string builtins and `${…}` interpolation
    "control.rvl",     # while/for/if, match (Some/None/wildcard), sync arrow
    "records.rvl",     # record literal, functional record update, list literal
    "optionals.rvl",   # optional-call chaining (opt receiver)
    "mixed.rvl",       # a cross-section of the above in three functions
]


def _load_reference_emit():
    """The reference TS emitter, loaded by path so we compare against the exact
    file this slice mirrors (not whatever `revl` re-exports)."""
    spec = importlib.util.spec_from_file_location(
        "tsemit_reference", ROOT / "backends" / "typescript" / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _exec_emitted() -> dict:
    """Compile selfhost/emit_ts.rvl, emit python (the emitter itself runs on the
    py tier), exec it. The file's component wrapper makes the emitted module
    `from runtime import …`; the pure emitter functions under test never touch
    it, so a lazy stub suffices (as in the other self-host stage tests)."""
    ir = compile_files([str(ROOT / "selfhost" / "emit_ts.rvl")])
    assert ir["ir_version"] == 3
    spec = importlib.util.spec_from_file_location(
        "tsemit_selfhost_backend", ROOT / "backends" / "python" / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    stub = types.ModuleType("runtime")
    stub.__getattr__ = lambda name: (lambda *a, **k: None)  # PEP 562
    had_runtime = "runtime" in sys.modules
    previous = sys.modules.get("runtime")
    sys.modules["runtime"] = stub
    try:
        namespace = {}
        exec(compile(module.emit(ir), "selfhost_emit_ts.py", "exec"), namespace)
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
def test_selfhosted_ts_emitter_is_byte_identical(emitted, reference, rel):
    """The self-hosted emitter's TypeScript output == the reference's,
    byte-for-byte, for every interchange-IR document in the covered subset."""
    ir = compile_files([str(CORPUS_DIR / rel)])
    want = reference.emit(ir)
    got = emitted["emit_src"](ir)
    assert got == want, (
        f"self-hosted TS emitter diverged from the reference on {rel}\n"
        f"--- lengths ref={len(want)} got={len(got)} ---"
    )


def test_selfhosted_ts_emitter_output_header_and_helpers(emitted):
    """Beyond byte-identity, pin the scaffold shape the slice is responsible for:
    the generated-header comments, the runtime import, and the gated helpers a
    bounded-int / string document pulls in."""
    ir = compile_files([str(CORPUS_DIR / "arith.rvl")])
    src = emitted["emit_src"](ir)
    assert src.startswith(
        "// Generated by revl backends/typescript/emit.py — do not edit.\n")
    assert "import { host } from '../runtime.ts'" in src
    assert "function revlI64(v: bigint): bigint {" in src
    assert "function revlI32(v: number): number {" in src
    assert "export function i64ops(a: bigint, b: bigint): bigint {" in src
    # a document with no string method must NOT carry the code-point helpers
    assert "function revlLen(" not in src
    strings = emitted["emit_src"](compile_files([str(CORPUS_DIR / "strings.rvl")]))
    assert "function revlLen(" in strings


def test_selfhosted_ts_emitter_in_file_tests_pass(emitted):
    """The .rvl file's own `test` blocks run under the python backend."""
    tests = emitted.get("REVL_TESTS")
    assert tests and len(tests) >= 4, "expected the file's test blocks in REVL_TESTS"
    for entry in tests:
        fn = entry[-1] if isinstance(entry, tuple) else entry
        fn()
