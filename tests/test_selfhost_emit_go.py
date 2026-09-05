"""The self-hosted cordis-go EMITTER (selfhost/emit_go.rvl, roadmap item 198 —
Path B slice 1 for the Go tier): compiled by revl, emitted through the python
backend, executed, and cross-checked BYTE-FOR-BYTE against the reference emitter
(backends/go/emit.py's ``emit``) over a corpus of interchange-IR documents.

This has the exact shape of tests/test_selfhost_emit_rust.py: two independent
implementations of one lowering — the reference Go backend and its revl port —
are forced to agree, and the agreement is the strongest an emitter can be held
to: the emitted Go source must be identical to the last byte. The reference is
ground truth; any divergence is a defect in the slice.

Every IR the frontend produces is ir_version 3, and a FUNCTION-ONLY document
(no components) routes through the reference's PURE typed-core path
(``emit`` -> ``_emit_v3_go`` -> ``_emit_v3_go_functions`` -> ``_go_v3_stmt`` /
``_go_v3_expr``): ordinary Go, no stc-go runtime. The covered corpus is the
corner of that path which emits byte-identical with only the module scaffold,
the conditional runtime preambles, and the free-function bodies.

Covered subset (what emits byte-identical):
  * the module scaffold — the two ``// Code generated …`` banner lines, the
    ``package emitted``, the conditional ``import ( "fmt" )`` block (only when
    interpolation is present in-slice), and the conditional runtime preambles
    the pure path prepends: ``revlDiv`` (true division), the
    ``revlAdd``/``revlSub``/``revlMul`` Int-overflow trio, and the
    ``revlAddI32``/``revlSubI32``/``revlMulI32``/``revlToI32`` Int32 block.
    Which preambles appear is a deterministic function of the IR (computed by a
    structural pass mirroring the flags the reference sets while rendering).
  * ``_emit_v3_go_functions`` — each module fn as a Go ``func`` with ``go_type``
    for scalar / ``List`` / ``Opt`` / ``Map`` / ``Result`` / function parameter
    and return types, and an empty (Unit) return rendered as no result.
  * ``_go_v3_stmt`` — let (with the int64/float64 ``var name T = …`` pin and the
    ``_ = name`` keep-alive), assign, return, if/else, while, for (the
    ``for _, x := range …`` form with its ``_ = x``), the bare-expr ``_ = …``,
    and assert (``if !(…) { t.Fatalf(…) }``).
  * ``_go_v3_expr`` — lit, name/var, bin (the trapping Int/Int32 ``+ - *``,
    ``/`` as ``revlDiv(float64(..), float64(..))``, ``%``, comparisons,
    ``&&``/``||``, native scalar ``==``/``!=``, and string ``+`` as Go ``+``),
    un (``!``, and the ``revlSub(0, x)`` Int negate), the free-function call,
    index, list literal, the sync arrow (with untyped-param recovery), the
    ``widen`` Float/Int markers, and non-float ``${..}`` interpolation via
    ``fmt.Sprintf``.

Covered typed-core (item 209, byte-identical): user ``type`` decls
(``_emit_v3_go_types`` — a record as a Go ``struct`` with EXPORTED,
``json:"<source>"``-tagged fields (item 390), a variant as a sealed interface +
case structs), record literals and field access, ADT construction (nullary +
payload), and ``match`` over user variants as a Go type-switch IIFE, plus user
type names in ``go_type``.

Deliberately OUT (excluded from the corpus, deferred to Go Path B slice 3+):
the go LIVE-COMPONENT world (v1/v2 stc-go runtime — a component routes there,
not here); functional record-update (``{r | f = e}`` — the go reference itself
RAISES on it, python/typescript-only today); the built-in Opt / Result / Map surface
(``??``, ``Some``/``None``/``Ok``/``Err``, ``Map.empty()``, optional chaining,
and the Opt/Result/Map preambles they pull in); the stdlib surface (every
``builtin``/``len`` node, the total division forms and their helpers,
``Str.to_int``); structural equality over non-scalars (``revlEq`` / the
``reflect`` import) and the canonical Float->Str ``revlFtoa`` in interpolation
(so a Float part is excluded); externs, in-file ``test`` emission, async /
lifecycle, and the astral reaches of ``_go_string`` beyond the ASCII/BMP core.
"""

import importlib.util
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_files  # noqa: E402

CORPUS_DIR = ROOT / "tests" / "fixtures" / "emit_go_corpus"
CORPUS = [
    "inference.rvl",
    "arith.rvl",     # trapping int/int32 + - *, / widening, %, comparisons, unary
    "bitwise.rvl",  # Int32 bitwise & | ^ << >> and unary ~ (item 366, item 391 self-host port)
    "control.rvl",   # var/let/assign, if/else, while, for, bare-expr, assert
    "calls.rvl",     # free-function calls + the call-return type pin on a `let`
    "strings.rvl",   # string `+` as Go `+`, `${..}` interpolation, literals
    "lists.rvl",     # list literal, index, the sync arrow, Map-typed passthru
    "records.rvl",   # user record `type`s -> structs, record literals, field access
    "variants.rvl",  # user variant `type`s -> sealed ifaces, ADT construction, match
    # item 383 / 391 (self-host port) — the `.reduce` transform desugars to the
    # `list_reduce` free call; the go tier lowers the `(A, T) -> A` function-value
    # param and the two-parameter arrow argument (reduce threads an accumulator
    # with no intermediate list, so its body needs no `.push`)
    "transforms.rvl",
    # item 421 F6 / item 429(d) — an extern whose declared return was
    # `Secret[T]`: the `revlSecretResult(revlSecret_<name>(..))` wrapper around
    # the verbatim body, and the extern surface that carries it. No other
    # document in this corpus declares a `Secret[T]` (or an extern), so without
    # this one the byte-agreement gate never reaches the redaction.
    "secrets.rvl",
]


def _load_reference_emit():
    """The reference emitter, loaded by path so we compare against the exact
    file this slice mirrors (not whatever `revl` re-exports)."""
    spec = importlib.util.spec_from_file_location(
        "goemit_reference", ROOT / "backends" / "go" / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _exec_emitted() -> dict:
    """Compile selfhost/emit_go.rvl, emit python, exec it. The file's component
    wrapper makes the emitted module `from runtime import …`; the pure emitter
    functions under test never touch it, so a lazy stub suffices (as in the
    other self-host stage tests)."""
    ir = compile_files([str(ROOT / "selfhost" / "emit_go.rvl")])
    assert ir["ir_version"] == 3
    spec = importlib.util.spec_from_file_location(
        "goemit_selfhost_backend", ROOT / "backends" / "python" / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    stub = types.ModuleType("runtime")
    stub.__getattr__ = lambda name: (lambda *a, **k: None)  # PEP 562
    had_runtime = "runtime" in sys.modules
    previous = sys.modules.get("runtime")
    sys.modules["runtime"] = stub
    try:
        namespace = {}
        exec(compile(module.emit(ir), "selfhost_emit_go.py", "exec"), namespace)
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
    """The self-hosted emitter's Go output == the reference's, byte-for-byte,
    for every interchange-IR document in the covered subset."""
    ir = compile_files([str(CORPUS_DIR / rel)])
    want = reference.emit(ir)
    got = emitted["emit_src"](ir)
    assert got == want, (
        f"self-hosted emitter diverged from the reference on {rel}\n"
        f"--- lengths ref={len(want)} got={len(got)} ---"
    )


def test_selfhosted_emitter_output_scaffold(emitted):
    """A byte-identical output is trivially valid Go source; pin the scaffold and
    a representative body detail so a regression in the header or the trapping
    arithmetic lowering surfaces here, not only in the byte diff."""
    ir = compile_files([str(CORPUS_DIR / "arith.rvl")])
    src = emitted["emit_src"](ir)
    assert src.startswith(
        "// Code generated by backends/go/emit.py — DO NOT EDIT.")
    assert "package emitted" in src
    assert 'panic("revl: Int overflow")' in src
    assert "return revlSub(revlAdd(a, b), revlMul(a, b))" in src
    assert src.endswith("}\n")


def test_selfhosted_emitter_in_file_tests_pass(emitted):
    """The .rvl file's own `test` blocks run under the python backend."""
    tests = emitted.get("REVL_TESTS")
    assert tests and len(tests) >= 3, "expected the file's test blocks in REVL_TESTS"
    for entry in tests:
        fn = entry[-1] if isinstance(entry, tuple) else entry
        fn()
