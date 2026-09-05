"""The self-hosted cordis-wasm EMITTER (selfhost/emit_wasm.rvl, roadmap item 200 —
Path B slice 1 for the WASM tier): compiled by revl, emitted through the python
backend, executed, and cross-checked BYTE-FOR-BYTE against the reference emitter
(backends/wasm/emit.py's ``emit``) over a corpus of interchange-IR documents.

This is the wasm instance of the self-host emit oracle — the exact shape of
tests/test_selfhost_emit_{py,ts,rust}.py: two independent implementations of one
lowering (the reference backend and its revl port) are forced to agree, and the
agreement is the strongest an emitter can be held to: the emitted WAT
(WebAssembly text) source must be identical to the last byte. The reference is
ground truth; any divergence is a defect in the slice.

wasm is the HARDEST tier — WAT is S-expressions, ``Int`` is i64 while every
address is i32, and even a one-line function drags in the whole ~430-line
linear-memory + checked-arithmetic helper preamble (``_helper_funcs``). The
byte-reproducible corner this slice mirrors is a FUNCTION-ONLY v3 document over
the SCALAR value ABI (Int / Int32 / Bool), which is the corner of the
reference's ``_V3Emitter.emit`` -> ``_emit_function`` -> ``_emit_stmts`` /
``_expr`` that needs NO data segments (``heap_start`` stays 0) and NONE of the
demand-driven helpers ($f64_to_str / $str_index_of / $str_split / $str_join):

Covered subset (what emits byte-identical):
  * the module scaffold — the 4-line banner, ``(module``, the exported memory,
    the ``$__hp`` bump-pointer global at 0, the constant helper preamble
    (embedded verbatim in the .rvl as a fixed second implementation of the same
    bytes), and the fn-block layout;
  * ``_emit_function`` — params as ``$p_<name>``, the result, the sorted
    ``$l_<name>`` locals with per-binding wasm widths, the always-present
    ``$__revl_tmp`` scratch, and the trailing stack-polymorphic ``unreachable``
    a diverging body needs;
  * ``_emit_stmts`` — let/var/assign (with the width tracking that types each
    local), return, if/else, while, the bare-expr ``(drop)``, and assert;
  * ``_expr`` — lit (Int ``i64.const`` / Bool ``i32.const``), var (the
    param/local slot), bin (the checked ``$int_add``/``$int_sub``/``$int_mul``
    and their ``$int32_*`` twins, ``%`` as ``i64.rem_s``, ``/`` as
    ``i64.div_s``, the i64/i32 comparisons, ``&&``/``||`` as ``i32.and``/
    ``i32.or``), un (``!`` as ``i32.eqz``, ``-`` as a checked subtract-from-zero).

Deliberately OUT (excluded from the corpus, deferred to wasm Path B slice 2+):
any value that touches linear memory (Str, List, records, tagged Opt/Result/user
variants — string literals pool into ``data`` and move ``heap_start``, and each
value threads ``$alloc``/``_str_ptr``/the nested scratch-pointer stack); Float
end to end (``/`` yields Float, refused at a scalar return/binding by name; a
``${aFloat}`` part pulls in ``$f64_to_str``); every ``builtin``/``len`` node
(``.to_int()`` widening is a ``builtin``, not a bare ``widen`` marker) and the
``for`` loop (a memory walk); components/services entirely; ``match``/``adt`` over
tagged cells; arrow values; ``??``; field/index; ``@wasm`` externs; and in-file
``test``/lifecycle-test emission.

Slice 2 (``strlit.rvl``) added the Str-literal ``data``-segment pool, and slice 3
(``listmem.rvl`` / ``recmem.rvl``) the rest of the *allocation* surface: ``List``
and record VALUES in linear memory — ``$alloc``, the ``[u32 count][slot…]`` /
declared-order-field-slot layouts, ``_slot_store`` widening, the nesting-depth
scratch pointer (``_acquire_tmp`` -> ``__revl_tmp`` / ``__revl_tmp_n1`` …, with
``_tmp_extra`` reconstructed from the max allocation depth), and the
``_type_comments`` layout block. Elements/fields are scalars or ASCII ``Str``
literals.

Slice 4 adds the READ and tagged-CONSTRUCTION surface: ``reads.rvl`` the field
(``_field_expr``) and index (``_index_expr``) reads plus ``x.length`` (the
``len`` node) via ``_slot_load``; ``variants.rvl`` the ``[u32 tag][pad][slot]``
tagged cell (``_make_tagged`` / ``_tagged_layout`` / ``_tag_of``) for the
built-in ``Opt``/``Result`` and user ``variant``s, nullary and payload cases,
nested cells, and the ``@variant`` layout comments; ``forloop.rvl`` the ``for
(x of xs)`` list walk (the ``for_ptr``/``for_cnt``/``for_idx`` cursor triple in
pre-order loop-id order); and ``builtins.rvl`` the ``builtin`` method surface
whose helpers all live in the always-emitted preamble (``length``, the
``to_int``/``to_int32`` widths, the four integer divisions, ``to_str`` /
``Str.to_int``, and ``push``/``concat``/``slice``/``charAt``/``charCodeAt``/
``startsWith``/``endsWith`` over Str and List).

Still OUT after slice 4: non-ASCII ``Str`` content (needs item 221's
``str_utf8_bytes``); READING a tagged cell — ``match`` and ``??`` (their
scrutinee/arm-bind locals and payload branches carry their own header ordering);
the demand-pulled reader helpers ``indexOf``/``split``/``join`` and Float
interpolation (``$str_index_of``/``$str_split``/``$str_join``/``$f64_to_str`` are
not in the fixed preamble); the checked-division total forms (their per-node
``cdiv_*`` Int scratch locals); the ``Map`` value type (a named refusal);
components/services; arrow values; ``@wasm`` externs; anonymous records reached
with no expected type (the ``_anon`` counter path); and in-file ``test``/
lifecycle-test emission."""

import importlib.util
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_files  # noqa: E402

CORPUS_DIR = ROOT / "tests" / "fixtures" / "emit_wasm_corpus"
CORPUS = [
    "folding.rvl",
    "calls.rvl",
    "inference.rvl",
    "string_ops.rvl",
    "arith.rvl",    # checked int/int32 +-*, % (rem_s), i64/i32 cmp, &&/||, !, unary -
    "constfold.rvl",  # item 432(g): constant `+ - *` folded to one const, the
                      # overflowing ones declined so their checked helper (and
                      # its runtime trap) stays, and the exact-Int.MIN product
                      # that DOES fit
    "bitwise.rvl",  # Int32 bitwise & | ^ << >> and unary ~ (item 366, item 391 self-host port)
    "control.rvl",  # if/else, while, let/var/assign, bare-expr drop, assert, divergence
    "strlit.rvl",   # the Str-literal memory ABI: data-segment pooling, _wat_bytes,
                    # first-encounter dedup, 4-byte stride, 8-aligned heap_start, _str_ptr
    "listmem.rvl",  # slice 3a: the List value ABI — $alloc, [u32 count][slot…]
                    # layout, _slot_store widening, the nesting-depth scratch
                    # (_acquire_tmp / __revl_tmp_n*), flat + nested + let/return
    "recmem.rvl",   # slice 3b: the record value ABI — declared-order 8-byte-slot
                    # fields, nested record/list fields on deeper scratch, the
                    # _type_comments layout block, field-set-match let inference
    "reads.rvl",    # slice 4a: field access + index READS — slot_load, declared
                    # field offsets, constant/variable list index, `x.length`
                    # (the len node), reads composed on reads
    "variants.rvl", # slice 4b: tagged-union CONSTRUCTION — the [u32 tag][pad]
                    # [slot payload] cell, Opt/Result built-ins + user variants,
                    # nullary vs payload cases, nested cells + lists of cells,
                    # the @variant layout comments
    "forloop.rvl",  # slice 4c: the `for (x of xs)` list walk — for_ptr/cnt/idx
                    # cursor locals, pre-order loop-id numbering (siblings,
                    # nested, in-if), the element slot_load at the bind's width
    "builtins.rvl", # slice 4d: the preamble-backed builtin/len surface — length,
                    # to_int/to_int32 widths, the four int divisions, to_str /
                    # Str.to_int, push/concat/slice/charAt/charCodeAt/startsWith/
                    # endsWith over Str and List
    "loopctrl.rvl", # item 379 / 391: break/continue via named labels
                    # ($revl_brk_N/$revl_top_N, inner $revl_cnt_N so `for`'s
                    # continue still runs idx++), nested-if/nested-loop targeting,
                    # and the break-aware while(true) terminates-check (C4)
]


def _load_reference_emit():
    """The reference emitter, loaded by path so we compare against the exact
    file this slice mirrors (not whatever `revl` re-exports)."""
    spec = importlib.util.spec_from_file_location(
        "wasmemit_reference", ROOT / "backends" / "wasm" / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _exec_emitted() -> dict:
    """Compile selfhost/emit_wasm.rvl, emit python, exec it. The file's component
    wrapper makes the emitted module `from runtime import …`; the pure emitter
    functions under test never touch it, so a lazy stub suffices (as in the
    other self-host stage tests)."""
    ir = compile_files([str(ROOT / "selfhost" / "emit_wasm.rvl")])
    assert ir["ir_version"] == 3
    spec = importlib.util.spec_from_file_location(
        "wasmemit_selfhost_backend", ROOT / "backends" / "python" / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    stub = types.ModuleType("runtime")
    stub.__getattr__ = lambda name: (lambda *a, **k: None)  # PEP 562
    had_runtime = "runtime" in sys.modules
    previous = sys.modules.get("runtime")
    sys.modules["runtime"] = stub
    try:
        namespace = {}
        exec(compile(module.emit(ir), "selfhost_emit_wasm.py", "exec"), namespace)
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
    """The self-hosted emitter's WAT output == the reference's, byte-for-byte,
    for every interchange-IR document in the covered subset.

    The reference emits a `{module_name: wat}` dict; a function-only document has
    the single `functions` module, which is what this slice reproduces."""
    ir = compile_files([str(CORPUS_DIR / rel)])
    want = reference.emit(ir)["functions"]
    got = emitted["emit_src"](ir)
    assert got == want, (
        f"self-hosted emitter diverged from the reference on {rel}\n"
        f"--- lengths ref={len(want)} got={len(got)} ---"
    )


def test_selfhosted_emitter_output_scaffold(emitted):
    """A byte-identical output is trivially valid WAT source; pin the scaffold
    and a representative body detail so a regression in the banner, the helper
    preamble, or the checked-arithmetic lowering surfaces here, not only in the
    byte diff."""
    ir = compile_files([str(CORPUS_DIR / "arith.rvl")])
    src = emitted["emit_src"](ir)
    assert src.startswith(
        ";; Generated by the revl cordis-wasm backend (ir_version 3)")
    assert '(memory (export "memory") 1)' in src
    assert "(global $__hp (mut i32) (i32.const 0))" in src
    assert "(func $int_add" in src          # the checked-arithmetic preamble
    assert '(func $i64ops (export "i64ops")' in src
    assert "(call $int_add)" in src         # `a + b` lowered through the helper
    assert src.endswith(")\n")


def test_selfhosted_emitter_in_file_tests_pass(emitted):
    """The .rvl file's own `test` blocks run under the python backend."""
    tests = emitted.get("REVL_TESTS")
    assert tests and len(tests) >= 3, "expected the file's test blocks in REVL_TESTS"
    for entry in tests:
        fn = entry[-1] if isinstance(entry, tuple) else entry
        fn()


def test_fn_type_param_refused(emitted, reference):
    """item 383 / 391 (self-host port): a `.map`/`.filter`/`.reduce` transform
    lowers (in the frontend) to a `list_*` free function with a function-value
    parameter, and a declared function type is not representable on this tier.
    The reference RAISES EmitError; a pure self-host emitter fn cannot `fail`, so
    it refuses within the subset with a loud `<<DEFER-fn-type>>` marker instead of
    silently emitting a mis-typed signature. Both refuse the same construct, so
    tests/fixtures/emit_*_corpus/transforms.rvl stays OUT of the byte-identity
    CORPUS above (the reference would raise there) and is checked only here."""
    ir = compile_files([str(CORPUS_DIR / "transforms.rvl")])
    with pytest.raises(reference.EmitError, match="function type"):
        reference.emit(ir)
    got = emitted["emit_src"](ir)
    assert "<<DEFER-fn-type>>" in got
