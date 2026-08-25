"""The self-hosted cordis-rs EMITTER (selfhost/emit_rust.rvl, roadmap item 191 —
Path B slice 1 for the Rust tier): compiled by revl, emitted through the python
backend, executed, and cross-checked BYTE-FOR-BYTE against the reference emitter
(backends/rust/emit.py's ``emit``) over a corpus of interchange-IR documents.

This has the exact shape of tests/test_selfhost_emit_py.py: two independent
implementations of one lowering — the reference Rust backend and its revl port —
are forced to agree, and the agreement is the strongest an emitter can be held
to: the emitted Rust source must be identical to the last byte. The reference is
ground truth; any divergence is a defect in the slice.

Every IR the frontend produces is ir_version 3, so the covered corpus is the
corner of the reference's v3 path (``_emit_v3`` -> ``_emit_v3_functions`` ->
``_render_expr`` / ``_v3_stmt``) that emits byte-identical with only the module
scaffold and the free-function bodies:

Covered subset (what emits byte-identical):
  * the module scaffold — ``_module_header(3)`` (banner, ``#![allow(..)]``,
    the ``use std::sync::Arc;`` / ``use cordis::Value;`` lines);
  * the v3 typed-core (slice 2) — ``_emit_v3_types`` (a record as a
    ``PartialEq``-deriving ``pub struct``, a variant as a serde-tagged
    ``pub enum``); record literals (``Struct { .. }`` with the by-value field
    clone) and field access; ADT construction (``Enum::Case`` /
    ``Enum::Case(arg)`` and the built-in Result/Option constructors); ``match``
    over user variants (bind + nullary patterns, the ``_`` wildcard vs the
    appended ``unreachable!()``); user record/variant names in ``_rust_type``
    (``List[Point]`` -> ``Vec<Point>``); and the ``_V3Ctx`` type inference
    (``case_adt`` / ``case_payload`` / ``record_by_fields``);
  * ``_emit_v3_functions`` — each module fn as a Rust ``fn`` with ``_rust_type``
    for scalar / ``List`` / ``Opt`` / ``Map`` / ``Result`` / user-type parameter
    and return types, ``pub`` visibility, and ``todo!()`` for an empty body;
  * ``_v3_stmt`` — let/assign (with the ``var_types`` seeding that drives
    by-value clone decisions), return, if/while/for, the bare-expr ``let _ =``,
    and assert;
  * ``_render_expr`` — lit, name, var (incl the ``Some``/``None``/``Ok``/``Err``
    constructors), bin (``??``, the bounded ``checked_add/sub/mul`` for
    Int/Int32, ``/`` as widened f64 true division, ``%``, comparisons,
    ``&&``/``||``, string ``+`` via ``format!``), un, the 2.0 ``callee``/``args``
    call with ``_by_value_arg`` cloning, the ``widen`` markers, index, list,
    maplit, the sync arrow, and non-float ``${..}`` interpolation.

Deliberately OUT (excluded from the corpus, deferred to Rust Path B slice 3+):
components/services entirely (``_emit_component*``, service traits, effect/undo,
``provide``/``req``/``config``, timers, the component-dialect expression kinds) —
these are ENTANGLED with the deferred erasure surface: a lone ``service``
declaration fires ``_emit_bridge`` and any ``component`` additionally fires the
host stubs and full impl machinery, so no service/component fixture is byte-exact
without also porting the bridge; functional record-update ``{r | f = e}`` (the
Rust reference itself *raises* on ``record_update`` — a structural exclusion, not
merely un-ported); the stdlib surface (every ``builtin``/``len`` node and the
``_stdlib_helper_traits`` it pulls in); the Value/serde erasure surface
(``_emit_bridge``, ``Pool``/``Map``/``Job`` host stubs); async coloring / spawn /
instances / realms; externs and in-file ``test``/lifecycle-test emission; the
canonical Float->Str ``revl_ftoa`` (so float interpolation is excluded); the
``impl Fn(..)`` lowering of a declared function type; the non-ASCII reaches of
``_string`` beyond the ASCII core; and ``let_pattern`` (the list form names a
temporary from the output-buffer length, which a second implementation cannot
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

CORPUS_DIR = ROOT / "tests" / "fixtures" / "emit_rust_corpus"
CORPUS = [
    "arith.rvl",     # bounded int/int32, / widening, %, comparisons, unary, ??
    "control.rvl",   # let/var/assign, if/else, while, for, bare-expr, assert
    "calls.rvl",     # free-function calls + the by-value clone / Copy-scalar split
    "strings.rvl",   # string `+` via format!, `${..}` interpolation, literals
    "lists.rvl",     # list literal, index, the sync arrow bound to a `let`
    "maps.rvl",      # the empty map literal and the Map/List generic type lowering
    # slice 2 — the v3 typed-core:
    "records.rvl",   # record `type` -> `pub struct`, record literal + field clone,
                     #   field access, a record-typed field, List[Point] lowering
    "variants.rvl",  # variant `type` -> serde-tagged `pub enum`, ADT construction
                     #   (nullary + payload), `match` (bind / nullary / `_` wildcard
                     #   vs `unreachable!()`), built-in Some/Ok coexisting
]


def _load_reference_emit():
    """The reference emitter, loaded by path so we compare against the exact
    file this slice mirrors (not whatever `revl` re-exports)."""
    spec = importlib.util.spec_from_file_location(
        "rsemit_reference", ROOT / "backends" / "rust" / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _exec_emitted() -> dict:
    """Compile selfhost/emit_rust.rvl, emit python, exec it. The file's component
    wrapper makes the emitted module `from runtime import …`; the pure emitter
    functions under test never touch it, so a lazy stub suffices (as in the
    other self-host stage tests)."""
    ir = compile_files([str(ROOT / "selfhost" / "emit_rust.rvl")])
    assert ir["ir_version"] == 3
    spec = importlib.util.spec_from_file_location(
        "rsemit_selfhost_backend", ROOT / "backends" / "python" / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    stub = types.ModuleType("runtime")
    stub.__getattr__ = lambda name: (lambda *a, **k: None)  # PEP 562
    had_runtime = "runtime" in sys.modules
    previous = sys.modules.get("runtime")
    sys.modules["runtime"] = stub
    try:
        namespace = {}
        exec(compile(module.emit(ir), "selfhost_emit_rust.py", "exec"), namespace)
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
    """The self-hosted emitter's Rust output == the reference's, byte-for-byte,
    for every interchange-IR document in the covered subset."""
    ir = compile_files([str(CORPUS_DIR / rel)])
    want = reference.emit(ir)
    got = emitted["emit_src"](ir)
    assert got == want, (
        f"self-hosted emitter diverged from the reference on {rel}\n"
        f"--- lengths ref={len(want)} got={len(got)} ---"
    )


def test_selfhosted_emitter_output_scaffold(emitted):
    """A byte-identical output is trivially valid Rust source; pin the scaffold
    and a representative body detail so a regression in the header or the
    checked-arithmetic lowering surfaces here, not only in the byte diff."""
    ir = compile_files([str(CORPUS_DIR / "arith.rvl")])
    src = emitted["emit_src"](ir)
    assert src.startswith(
        "//! Generated by the revl cordis-rs backend (ir_version 3): do not edit.")
    assert "use cordis::Value;" in src
    assert '.checked_add(b).expect("revl: Int overflow")' in src
    assert src.endswith("}\n")


def test_selfhosted_emitter_typed_core_scaffold(emitted):
    """Pin the typed-core surface (slice 2): a record lowers to a
    ``PartialEq``-deriving ``pub struct``, a variant to a serde-tagged
    ``pub enum``, an ADT case to ``Enum::Case``, and a wildcard-free ``match``
    grows the ``unreachable!()`` fallthrough — so a regression in any of these
    surfaces here, not only in the byte diff."""
    rec = emitted["emit_src"](compile_files([str(CORPUS_DIR / "records.rvl")]))
    assert "pub struct Point {" in rec
    assert "#[derive(Clone, Debug, PartialEq, serde::Serialize, serde::Deserialize)]" in rec
    assert "Named { label: s.clone(), at: p.clone() }" in rec
    var = emitted["emit_src"](compile_files([str(CORPUS_DIR / "variants.rvl")]))
    assert '#[serde(tag = "$kind", content = "$value")]' in var
    assert "pub enum Tree {" in var
    assert "return Tree::Leaf;" in var
    assert "Tree::Node(v) => v," in var
    assert "_ => unreachable!()," in var


def test_selfhosted_emitter_in_file_tests_pass(emitted):
    """The .rvl file's own `test` blocks run under the python backend."""
    tests = emitted.get("REVL_TESTS")
    assert tests and len(tests) >= 3, "expected the file's test blocks in REVL_TESTS"
    for entry in tests:
        fn = entry[-1] if isinstance(entry, tuple) else entry
        fn()
