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
import shutil
import subprocess
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_files  # noqa: E402

sys.path.insert(0, str(ROOT / "tests"))

from _boundary_witness import assert_boundary_witness  # noqa: E402

CORPUS_DIR = ROOT / "tests" / "fixtures" / "emit_wasm_corpus"
CORPUS = [
    "scratch_names.rvl",
    "widening.rvl",
    "residuals.rvl",
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
    "shortcircuit.rvl",  # item 458 / 1041: `&&`/`||` short-circuit through the
                    # folded `(if (result i32) …)` branch when the right operand
                    # can trap, allocate or read memory (`%`, `/`, the checked
                    # `*`, an index, a `len`, a call, a `Str` compare), and keep
                    # the strict single `i32.and`/`i32.or` when it provably
                    # cannot (constants, local reads, and `!`/comparison/logical
                    # combinations of those)
    "externs.rvl",  # issue 1130: a DECLARED extern with no `@wasm` body. The
                    # reference answers one with a named
                    # `;; unsupported on this tier: externs … (no @wasm body)`
                    # comment — a refusal it states in the output — and the port
                    # reproduces it, so the byte oracle covers the sentence like
                    # any other emitted text
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
    got = emitted["emit_wasm_src"](ir)
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
    src = emitted["emit_wasm_src"](ir)
    assert src.startswith(
        ";; Generated by the revl cordis-wasm backend (ir_version 3)")
    assert '(memory (export "memory") 1)' in src
    assert "(global $__hp (mut i32) (i32.const 0))" in src
    assert "(func $int_add" in src          # the checked-arithmetic preamble
    assert '(func $i64ops (export "i64ops")' in src
    assert "(call $int_add)" in src         # `a + b` lowered through the helper
    assert src.endswith(")\n")


@pytest.mark.parametrize("rel", [
    "widening.rvl", "folding.rvl", "variants.rvl", "scratch_names.rvl", "residuals.rvl",
    "shortcircuit.rvl",
])
def test_supported_corpus_compiles_as_wasm(emitted, reference, tmp_path, rel):
    compiler = shutil.which("wat2wasm")
    if compiler is None:
        pytest.skip("wat2wasm compiler not installed")
    ir = compile_files([str(CORPUS_DIR / rel)])
    for name, source in (("reference", reference.emit(ir)["functions"]),
                         ("selfhost", emitted["emit_wasm_src"](ir))):
        path = tmp_path / f"{name}.wat"
        path.write_text(source)
        result = subprocess.run(
            [compiler, str(path), "-o", str(tmp_path / f"{name}.wasm")],
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0, result.stdout + result.stderr


def test_nested_scratch_names_sort_lexically(emitted, reference, tmp_path):
    source = tmp_path / "deep.rvl"
    source.write_text(
        "fn deep(__revl_tmp: Int) -> " + "List[" * 12 + "Int" + "]" * 12
        + " { return " + "[" * 12 + "__revl_tmp" + "]" * 12 + " }\n",
        encoding="utf-8",
    )
    ir = compile_files([str(source)])
    got = emitted["emit_wasm_src"](ir)
    assert got == reference.emit(ir)["functions"]
    assert got.index("(local $__revl_tmp_1_n10 ") < got.index("(local $__revl_tmp_1_n2 ")


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
    got = emitted["emit_wasm_src"](ir)
    assert "<<DEFER-fn-type>>" in got


@pytest.mark.parametrize("source, reference_token, port_token", [
    pytest.param("fn f(x: Opt[Int]) -> Int { return x ?? 0 }",
                 "i64.load", "<<DEFER-coalesce>>", id="tagged-coalesce"),
    pytest.param("fn f(x: Result[Int, Str]) -> Int { return match x { Ok(v) => v, Err(e) => 0 } }",
                 "i64.load", "<<UNSUPPORTED-EXPR:match>>", id="tagged-match"),
    pytest.param('fn f(s: Str) -> Int { return s.indexOf("x") }',
                 "(func $str_index_of", "<<UNSUPPORTED-BUILTIN:indexOf>>", id="demand-index-of"),
    pytest.param('fn f(s: Str) -> List[Str] { return s.split(",") }',
                 "(func $str_split", "<<UNSUPPORTED-BUILTIN:split>>", id="demand-split"),
    pytest.param('fn f(xs: List[Str]) -> Str { return xs.join(",") }',
                 "(func $str_join", "<<UNSUPPORTED-BUILTIN:join>>", id="demand-join"),
    pytest.param("fn f(x: Int) -> Str { return `n=${x}` }",
                 "(call $str_concat)", "<<UNSUPPORTED-EXPR:interp>>", id="interpolation"),
    pytest.param("fn f(a: Int, b: Int) -> Result[Int, Str] { return a.checked_div_trunc(b) }",
                 "cdiv_", "<<UNSUPPORTED-BUILTIN:checked_div_trunc>>", id="checked-division"),
    pytest.param("fn f(n: Int) -> Int { let add = (x: Int) => x + 1; return add(n) }",
                 "$f", "<<UNSUPPORTED-EXPR:arrow>>", id="inline-arrow"),
    # `(data` opens the segment on BOTH sides -- what diverges is its bytes:
    # the reference pools the UTF-8 encoding of `\u00e9` (2 bytes, length 2),
    # the port pools the code point as one byte. Pin the port's segment.
    pytest.param('fn f() -> Str { return "\u00e9" }',
                 "\\c3\\a9", '"\\01\\00\\00\\00\\e9"', id="utf8-string-pool"),
    # `;; Generated` is the banner both sides write. The port-only text is at
    # the CALL: an `@wasm`-bodied extern is never registered as a function by
    # the port, so `render_direct_call` refuses the call by name while the
    # reference renders both the call and `(func $f`. Ordinary calls ARE ported
    # (calls.rvl is in CORPUS above), so this marker names the extern deferral
    # and not calls in general.
    pytest.param("extern pure fn f() -> Int = @wasm { (i64.const 7) }\nfn g() -> Int { return f() }",
                 "(func $f", "<<UNSUPPORTED-CALL:f>>", id="wasm-extern"),
    # issue #1123: the port_token here used to be `$f` — the emitted function
    # itself, present in EVERY wasm emission of this document, so the witness
    # could not tell a named refusal from silence, which is what the port
    # actually produced. It now pins the marker.
    pytest.param('fn f() -> Bool { return true }\ntest "probe" { assert f() }',
                 "$revl_test_probe", "<<UNSUPPORTED-TEST:probe>>", id="in-file-tests"),
])
def test_deferred_families_remain_explicit(emitted, reference, tmp_path, source,
                                         reference_token, port_token):
    """Keep deferred witnesses separate from the byte-agreement workload."""
    path = tmp_path / "boundary.rvl"
    path.write_text(source)
    ir = compile_files([str(path)])
    want, got = reference.emit(ir)["functions"], emitted["emit_wasm_src"](ir)
    assert_boundary_witness(want, got, reference_token, port_token)


def test_a_witness_token_both_sides_emit_is_rejected(emitted, reference, tmp_path):
    """Non-vacuity for the meta-check, planting the exact token it was written for.

    Until item 1136 the `in-file-tests` case above pinned `$f` as its port
    token. Both sides emit it -- it is the document's own function -- so the case
    passed green while the port dropped the entire test section. Plant it back
    and the witness must now refuse it by name; the honest form of the same case
    (the reference's test function, absent from the port) still passes.
    """
    path = tmp_path / "planted.rvl"
    path.write_text('fn f() -> Bool { return true }\ntest "probe" { assert f() }')
    ir = compile_files([str(path)])
    want, got = reference.emit(ir)["functions"], emitted["emit_wasm_src"](ir)
    assert "$f" in want and "$f" in got, (
        "the plant is only a proof while it is text BOTH sides emit"
    )
    with pytest.raises(AssertionError, match="text the REFERENCE also emits"):
        assert_boundary_witness(want, got, "$revl_test_probe", "$f")
    assert_boundary_witness(want, got, "$revl_test_probe", None)


STREAM_130 = ROOT / "backends" / "go" / "testdata" / "stream_130.rvl"


def test_a_stream_document_the_reference_refuses_is_refused_by_name_here_too(
    emitted, reference,
):
    """item 130 §4.6 / issue #81: the port must not answer where the reference refuses.

    `backends/go/testdata/stream_130.rvl` is six stream components. This tier
    REFUSES it BY NAME -- a subscription suspends a fiber and this tier awaits
    only `Job.run(name)` -- and the refusal names the component it stopped on.
    The port answered the same document with a 376-byte module: a valid, empty
    WAT module for a program this tier does not accept.

    That is the sharpest form of the defect, because what the port dropped was
    ITSELF A REFUSAL. A wrong lowering is visible; a tier limit the reference
    states and its port does not is silence, and silence is the one direction
    this design is meant to make impossible. Each component is now named.
    """
    ir = compile_files([str(STREAM_130)])
    names = [component["name"] for component in ir["components"]]
    assert names == ["Consumer", "Parked", "Fanin", "Windowed", "Iterate", "Chain"]
    with pytest.raises(reference.EmitError) as exc:
        reference.emit(ir)
    assert "Consumer" in str(exc.value), (
        "the reference names what it refuses: this case no longer exercises it"
    )
    got = emitted["emit_wasm_src"](ir)
    assert [line.strip() for line in got.splitlines() if "<<" in line] == [
        f";; <<UNSUPPORTED-COMPONENT:{name}>>" for name in names
    ], "one marker per components entry, in document order"


def test_the_component_marker_is_text_only_the_port_emits(emitted, reference,
                                                          tmp_path):
    """The boundary witness for the marker, on a document the reference EMITS.

    `emit_wasm_src` is this port's whole answer for a document -- it is what
    `compile_to(source, "wasm")` returns -- while the reference answers with a
    `{module: wat}` map that carries a SEPARATE module per component. Here the
    reference emits both `functions` and `C`; the port emits the functions
    module and has nothing of `C`, so the marker is the only place the absence
    is stated. `<<UNSUPPORTED-COMPONENT:C>>` appears nowhere in the reference's
    answer, which is what makes this a witness rather than a coincidence
    (item 1136).
    """
    path = tmp_path / "mixed.rvl"
    path.write_text("fn f() -> Int { return 1 }\n"
                    "service S { fn g() -> Int }\n"
                    "component C provides s: S { provide s { fn g() = 1 } }\n")
    ir = compile_files([str(path)])
    modules = reference.emit(ir)
    assert set(modules) == {"functions", "C"}
    want, got = "\n".join(modules.values()), emitted["emit_wasm_src"](ir)
    assert_boundary_witness(want, got, ';; component C',
                            "<<UNSUPPORTED-COMPONENT:C>>")


@pytest.mark.parametrize("source, reason", [
    ("fn f(x: Float) -> Float { return x }", "Float"),
    ("fn f(x: Map[Str, Int]) -> Map[Str, Int] { return x }", "Map"),
])
def test_reference_abi_refusals(reference, tmp_path, source, reason):
    path = tmp_path / "abi.rvl"
    path.write_text(source)
    with pytest.raises(reference.EmitError, match=reason):
        reference.emit(compile_files([str(path)]))


@pytest.mark.parametrize("body, where", [
    ("fn g(s: Str) -> Str { return peek(s) }", "g: "),
    ("fn g(s: Str) -> Str { let x = peek(s); return x }", ""),
], ids=["call-position", "let-initializer"])
def test_reference_refuses_a_bodyless_extern_by_name(reference, tmp_path, body, where):
    """A DECLARED extern with no `@wasm` body is refused by NAME, stating the
    tiers that do carry one.

    This tier used to answer `callee 'peek' is not a lowerable function`, which
    is the sentence it also gives for a MISSPELLED callee, so a portability limit
    and a typo were indistinguishable. The other five emitters have always said
    "extern `X` has no @<tier> body - not portable to this backend (available:
    ...)"; wasm says it too (item 459 F6).

    Both entry points are driven because they refuse independently: a call in
    expression position goes through `_call_expr` (which prefixes the function it
    is in), a call inferring a `let`'s type goes through `_call_type` (which has
    no such context). Fixing only one leaves the misleading message on the other.

    This is a REFERENCE refusal, the same shape as `test_reference_abi_refusals`
    above, and it cannot be a CORPUS document: the corpus is documents the
    reference EMITS and holds byte-identical against the port, while every input
    reaching this arm raises. That is why the arm is carried in
    tests/fixtures/selfhost_uncovered_lines.json, and this test is what keeps it
    honest in the meantime."""
    path = tmp_path / "bodyless.rvl"
    path.write_text("extern pure fn peek(p: Str) -> Str = @py { return p }\n" + body)
    ir = compile_files([str(path)])
    with pytest.raises(reference.EmitError) as exc:
        reference.emit(ir)
    message = str(exc.value)
    assert message.startswith(f"{where}extern `peek` has no @wasm body"), message
    assert "not portable to this backend" in message
    assert "(available: py)" in message
    assert "not a lowerable function" not in message


def test_an_uncalled_bodyless_extern_still_emits(reference, tmp_path):
    """The boundary of the refusal above, so it is not read as wider than it is.

    rust, go and java refuse a bodyless extern at the DECLARATION loop, whether
    or not it is called. wasm refuses at the CALL, and this change did not move
    that: a document that merely declares a py-only extern and never calls it
    still emits, and names the extern in the `unsupported on this tier` comment.
    Widening wasm to the declaration-loop shape would refuse documents that
    emit today, which is a separate decision from stating the message better."""
    path = tmp_path / "uncalled.rvl"
    path.write_text("extern pure fn peek(p: Str) -> Str = @py { return p }\n"
                    "fn g(s: Str) -> Str { return s }\n")
    wat = reference.emit(compile_files([str(path)]))["functions"]
    assert "unsupported on this tier: externs peek (no @wasm body)" in wat
