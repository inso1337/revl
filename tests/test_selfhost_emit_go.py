"""The self-hosted cordis-go EMITTER (selfhost/emit_go.rvl, roadmap item 198 —
Path B slice 1 for the Go tier): compiled by revl, emitted through the python
backend, executed, and cross-checked BYTE-FOR-BYTE against the reference emitter
(backends/go/emit.py's ``emit``) over a corpus of interchange-IR documents.

This has the exact shape of tests/test_selfhost_emit_rust.py: two independent
implementations of one lowering — the reference Go backend and its revl port —
are forced to agree, and the agreement is the strongest an emitter can be held
to: the emitted Go source must be identical to the last byte. The reference is
ground truth; any divergence is a defect in the slice.

A document with no components, or with top-level declarations beside only
incidental components, routes through the reference's PURE typed-core path
(``emit`` -> ``_emit_v3_go`` -> the types / externs / functions / tests
renderers -> ``_go_v3_stmt`` / ``_go_v3_expr``): ordinary Go, no stc-go
runtime. Since issue #106 the port covers that whole path: the module scaffold
with every runtime preamble and import the reference gates on what it renders
(structural equality, the canonical Float rendering, the Int/Int32 traps, the
integer division helpers, the Opt / Result / Map / stdlib constants), user
types, externs (``Secret[T]``, item 378 ``config``, the ``//revl:import``
hoist), every statement and expression form including the stdlib builtins, the
built-in Opt/Result values and their ``match``, optional chaining, the item 434
string builders and code-point scan, the item 445 destructive rebind, and the
in-file ``test`` blocks.

Deliberately OUT, each answered with a named ``<<UNSUPPORTED-...>>`` marker:
the go LIVE-COMPONENT world (an ir_version 1/2 document, a component with no
top-level declarations, an observable component beside declarations, a
``lifecycle test``), ``fault test`` blocks (the reference refuses them), the
item 388 colour-erased extern clone, and an extern body whose line breaks
Python ``splitlines()`` reads differently. Functional record-update is a
reference refusal.
"""

import importlib.util
import os
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

CORPUS_DIR = ROOT / "tests" / "fixtures" / "emit_go_corpus"
CORPUS = [
    "arrow_containers.rvl",
    # docs/closures.md — an arrow that READS an enclosing `var`. Every other
    # arrow in this corpus reads only its own parameters, so `captures` was
    # empty in all of them and the by-value pin was never compared.
    "arrow_captures.rvl",
    "match_edges.rvl",
    "accumulator_hygiene.rvl",
    "accumulators.rvl",
    "builder_literal.rvl",
    "../emit_rust_corpus/perf_shapes.rvl",
    "identifiers.rvl",
    "../emit_java_corpus/records.rvl",
    "../emit_wasm_corpus/loopctrl.rvl",
    "../emit_wasm_corpus/strlit.rvl",
    "inference.rvl",
    "arith.rvl",     # trapping int/int32 + - *, / widening, %, comparisons, unary
    # issue #721 — the Float half: `%` on Float is `math.Mod` (Go has no `%` on
    # float64), and a Float literal goes through `revlF` so it is not a Go
    # CONSTANT, whose exact fold has no signed zero and no infinity.
    "float_rem.rvl",
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
    # issue #106: the surface the port used to refuse or skip. A document of its
    # own for the shapes no tree document reaches together: the stdlib builtins
    # and `len`, Opt/Result construction and `match`, `??`, optional chaining,
    # the Map value type and its faulting subscript, structural `==`, the
    # canonical Float rendering, the code-point scan, the destructive rebind and
    # in-file tests.
    "stdlib_surface.rvl",
    # the item 378 extern `config` seam and the `//revl:import` hoist
    "extern_config.rvl",
    # the code-point scan rewrite and every near miss it must refuse, the
    # `.concat` string builder, and the destructive Map rebinds
    "loop_shapes.rvl",
    # the preamble gates read the IR's JSON text, field names included
    "blob_probes.rvl",
    # in-file `test` blocks, the `testing` import and the Go test-name rules
    "in_file_tests.rvl",
    # issue #106: tree documents the port refused or diverged on before, chosen
    # by `tools/selfhost_differential_survey.py --select-cover` for the
    # statements they reach beyond the documents above. The host-family
    # constructors and the predeclared-name escape (`make`); Opt/Result
    # construction recovered from the argument; the item 280 Opt gaps with
    # their in-file tests; let-inference through builtins; a `match` whose arms
    # ignore their binders; the total division forms; declared Opt/Map/List
    # shapes; optional chaining across the builtin table; and the Map methods.
    "../emit_py_corpus/hostroots.rvl",
    "../emit_py_corpus/adt_inference.rvl",
    "../../../backends/go/testdata/opt_gaps_280.rvl",
    "../emit_wasm_corpus/inference.rvl",
    "../emit_java_corpus/match_ignored.rvl",
    "../emit_py_corpus/checked_div.rvl",
    "../emit_py_corpus/declared_type_shapes.rvl",
    "../emit_py_corpus/optionals.rvl",
    "../emit_py_corpus/maps.rvl",
    # issue #1631: a built-in Ok/Err/None whose type the argument does not
    # fully say takes the missing part from where it flows (a declared return
    # or parameter, or the other operand of an `==`), and a call through a
    # function value types its payload
    "../../../backends/go/testdata/result_erased_1631.rvl",
    # issue #106: the v3 COMBINED path's first slice (`_emit_v3_combined`). A
    # top-level declaration beside components whose provide methods are single
    # `return`s or empty bodies, with or without `requires`: the pure tier, the
    # fixed host runtime, the service interfaces and keys, the component
    # constructors with their required services, the provide impls and the
    # load helpers, in one package. Keep these last: COMBINED_CORPUS below is
    # the tail of this list from the first of them.
    "comp_provide_pure.rvl",
    "../emit_rust_corpus/service.rvl",
    "../emit_rust_corpus/requires.rvl",
    "../emit_rust_corpus/services_multi.rvl",
    "../emit_ts_corpus/components_mixed.rvl",
    # ... and realm placement on the same slice: `isolate <key> in realm(..)`
    # (the `_revlRealm` interner and a child load context) and `intercept
    # <key> with { .. }` (its metadata as a Go literal)
    "../emit_rust_corpus/comp_realm_isolate.rvl",
    "../emit_java_corpus/comp_realm_intercept.rvl",
    "../emit_ts_corpus/realm_intercept.rvl",
    "../emit_ts_corpus/realm_isolate.rvl",
]

# The combined-path documents, which import stc-go and are built against it.
COMBINED_CORPUS = CORPUS[CORPUS.index("comp_provide_pure.rvl"):]


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
    got = emitted["emit_go_src"](ir)
    assert got == want, (
        f"self-hosted emitter diverged from the reference on {rel}\n"
        f"--- lengths ref={len(want)} got={len(got)} ---"
    )


def test_selfhosted_emitter_output_scaffold(emitted):
    """A byte-identical output is trivially valid Go source; pin the scaffold and
    a representative body detail so a regression in the header or the trapping
    arithmetic lowering surfaces here, not only in the byte diff."""
    ir = compile_files([str(CORPUS_DIR / "arith.rvl")])
    src = emitted["emit_go_src"](ir)
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


@pytest.mark.parametrize("rel", [
    "inference.rvl", "accumulators.rvl", "../emit_rust_corpus/perf_shapes.rvl",
    # issue #106: every preamble and import the ported surface pulls in
    "stdlib_surface.rvl", "loop_shapes.rvl", "extern_config.rvl", "in_file_tests.rvl",
])
def test_supported_corpus_compiles_as_go(emitted, reference, tmp_path, rel):
    """Agreement alone cannot catch a type error shared by both emitters."""
    go = shutil.which("go")
    if go is None:
        pytest.skip("Go compiler not installed")
    ir = compile_files([str(CORPUS_DIR / rel)])
    for name, source in (("reference", reference.emit(ir)),
                         ("selfhost", emitted["emit_go_src"](ir))):
        path = tmp_path / f"{name}.go"
        path.write_text(source)
        result = subprocess.run(
            [go, "test", str(path)], capture_output=True, text=True,
            env={**os.environ, "GO111MODULE": "off"}, timeout=60,
        )
        assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("side", ["reference", "selfhost"])
def test_match_edges_runtime(emitted, reference, tmp_path, side):
    go = shutil.which("go")
    if go is None:
        pytest.skip("Go compiler not installed")
    ir = compile_files([str(CORPUS_DIR / "match_edges.rvl")])
    source = reference.emit(ir) if side == "reference" else emitted["emit_go_src"](ir)
    module = tmp_path / "matches.go"
    module.write_text(source, encoding="utf-8")
    test = tmp_path / "matches_test.go"
    test.write_text(
        'package emitted\nimport "testing"\n'
        'func TestMatches(t *testing.T) {\n'
        '  if scalar_wildcard(1) != 42 || record_wildcard(Box{Value: 2}) != 43 || '
        'list_wildcard([]int64{3}) != 44 || constructed(9) != 9 || '
        'discarded(TreeNode{Value: 4}) != 45 || inverted(0) != -1 || '
        'escaped() != "a\\nb\\tcd\\u00e9" || '
        'astral() != "\\U0001f600" { t.Fatal("match edge result") }\n'
        '}\n',
        encoding="utf-8",
    )
    result = subprocess.run(
        [go, "test", str(module), str(test)], capture_output=True, text=True,
        env={**os.environ, "GO111MODULE": "off"}, timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_accumulator_generated_names_compile_and_run(
    emitted, reference, tmp_path,
):
    """Builder locals and their package alias must not collide with user names."""
    go = shutil.which("go")
    if go is None:
        pytest.skip("Go compiler not installed")
    ir = compile_files([str(CORPUS_DIR / "accumulator_hygiene.rvl")])
    test_source = """package emitted
import "testing"
func TestAccumulatorHygiene(t *testing.T) {
    xs := []string{"a", "b"}
    if got := reserved(xs); got != "xx" { t.Fatalf("reserved: %q", got) }
    if got := shadowed(xs); got != "!!" { t.Fatalf("shadowed: %q", got) }
    if got := appendSelf(0); got != "x" { t.Fatalf("appendSelf(0): %q", got) }
    if got := appendSelf(3); got != "xxxxxxxx" { t.Fatalf("appendSelf(3): %q", got) }
}
"""
    for name, source in (("reference", reference.emit(ir)),
                         ("selfhost", emitted["emit_go_src"](ir))):
        case = tmp_path / name
        case.mkdir()
        (case / "emitted.go").write_text(source)
        (case / "emitted_test.go").write_text(test_source)
        result = subprocess.run(
            [go, "test", "."], cwd=case, capture_output=True, text=True,
            env={**os.environ, "GO111MODULE": "off"}, timeout=60,
        )
        assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("side", ["reference", "selfhost"])
def test_arrow_container_parameter_inference_runtime(emitted, reference, tmp_path, side):
    go = shutil.which("go")
    if go is None:
        pytest.skip("Go compiler not installed")
    ir = compile_files([str(CORPUS_DIR / "arrow_containers.rvl")])
    source = reference.emit(ir) if side == "reference" else emitted["emit_go_src"](ir)
    module = tmp_path / "arrows.go"
    module.write_text(source, encoding="utf-8")
    test = tmp_path / "arrows_test.go"
    test.write_text(
        'package emitted\nimport "testing"\n'
        'func TestArrows(t *testing.T) {\n'
        '  if listArrow()[0] != 3 { t.Fatal("list arrow") }\n'
        '  if recordArrow().Value != 4 { t.Fatal("record arrow") }\n'
        '  if nestedArrow()[0][0] != 5 { t.Fatal("nested arrow") }\n'
        '  if constantArrow() != 1 { t.Fatal("constant arrow") }\n'
        '  if reverseArrow() != 3 { t.Fatal("reverse arrow") }\n'
        '  if callField() != 4 { t.Fatal("call field") }\n'
        '  callback := func(xs []int64, n int64) int64 { return xs[0] + n }\n'
        '  if nestedCallback(callback, []int64{6}) != 7 { t.Fatal("callback") }\n'
        '}\n',
        encoding="utf-8",
    )
    result = subprocess.run(
        [go, "test", str(module), str(test)], capture_output=True, text=True,
        env={**os.environ, "GO111MODULE": "off"}, timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_builder_text_literal_does_not_import_strings(emitted, reference):
    ir = compile_files([str(CORPUS_DIR / "builder_literal.rvl")])
    want, got = reference.emit(ir), emitted["emit_go_src"](ir)
    assert '"strings"' not in want
    assert '"strings"' not in got


# The families this list used to hold (the Opt/Result/Map surface, the stdlib
# builtins, structural equality, the Float rendering helper, the extern
# `//revl:import` hoist and the in-file test section) were ported under issue
# #106 and their witnesses moved into CORPUS (`stdlib_surface.rvl`,
# `extern_config.rvl`). `test_the_closed_families_now_agree` below keeps each
# old witness source as an agreement pin, so a regression in one of them names
# the family rather than only a corpus document.
@pytest.mark.parametrize("source, reference_token, port_token", [
    pytest.param("service S { fn f() -> Int }\ncomponent C provides s: S { provide s { fn f() = 1 } }",
                 "stc-go", "pure typed-core tier", id="live-component"),
    # an ir_version 1 document is a live stc-go module on the reference
    pytest.param("service S { fn f() -> Int }",
                 "stc-go", "<<UNSUPPORTED-LIVE-TIER:ir_version 1>>", id="live-tier-service"),
])
def test_deferred_families_remain_explicit(emitted, reference, tmp_path, source,
                                         reference_token, port_token):
    """These witnesses are not byte-agreement CORPUS; a port closes the reason."""
    path = tmp_path / "boundary.rvl"
    path.write_text(source)
    ir = compile_files([str(path)])
    want, got = reference.emit(ir), emitted["emit_go_src"](ir)
    assert_boundary_witness(want, got, reference_token, port_token)


@pytest.mark.parametrize("source", [
    pytest.param("fn f(x: Opt[Int]) -> Int { return x ?? 0 }", id="opt-coalesce"),
    pytest.param("fn f(x: Result[Int, Str]) -> Int { return match x { Ok(v) => v, Err(e) => 0 } }",
                 id="result-match"),
    pytest.param("fn f() -> Map[Str, Int] { return Map.empty() }", id="map"),
    pytest.param("fn f(s: Str) -> Int { return s.length() }", id="stdlib"),
    pytest.param("fn f(a: List[Int], b: List[Int]) -> Bool { return a == b }",
                 id="structural-equality"),
    pytest.param("fn f(x: Float) -> Str { return `x=${x}` }", id="float-format-helper"),
    pytest.param('extern pure fn f() -> Str = @go {\n//revl:import strings\nreturn strings.ToUpper("x")\n}',
                 id="extern-imports"),
    pytest.param('fn f() -> Bool { return true }\ntest "probe" { assert f() }',
                 id="in-file-tests"),
])
def test_the_closed_families_now_agree(emitted, reference, tmp_path, source):
    """Issue #106: each family the port once deferred is byte-identical now."""
    path = tmp_path / "closed.rvl"
    path.write_text(source)
    ir = compile_files([str(path)])
    assert emitted["emit_go_src"](ir) == reference.emit(ir)


def test_a_witness_token_both_sides_emit_is_rejected(emitted, reference, tmp_path):
    """Non-vacuity for the meta-check, planting the exact kind of token it was
    written for.

    Until item 1136 the `in-file-tests` case pinned `func f()` as its port
    token. Both sides emit it -- it is the document's own function -- so the
    case passed green while the port dropped the entire `testing` section. That
    family is ported now, so the plant moves to the one that is still deferred:
    a declaration beside an observable component, where both sides emit the
    function and only the reference emits the component. The planted token must
    be refused by name; the honest form (the reference's component, absent from
    the port) still passes. The component carries a config field because a
    provide-only one is on the combined slice the port carries now (issue #106).
    """
    path = tmp_path / "planted.rvl"
    path.write_text("fn f() -> Int { return 1 }\n"
                    "service S { fn g() -> Int }\n"
                    "component C provides s: S {\n"
                    "  config { n: Int = 1 }\n"
                    "  provide s { fn g() = 1 }\n"
                    "}\n")
    ir = compile_files([str(path)])
    want, got = reference.emit(ir), emitted["emit_go_src"](ir)
    assert "func f()" in want and "func f()" in got, (
        "the plant is only a proof while it is text BOTH sides emit"
    )
    with pytest.raises(AssertionError, match="text the REFERENCE also emits"):
        assert_boundary_witness(want, got, "stc.Component", "func f()")
    assert_boundary_witness(want, got, "stc.Component", None)


STREAM_130 = ROOT / "backends" / "go" / "testdata" / "stream_130.rvl"


def test_a_components_only_document_names_every_component(emitted, reference):
    """item 130 / issue #81: a stream document must not come back as a banner.

    `backends/go/testdata/stream_130.rvl` is six components and a service and
    nothing else, so the reference never routes it to the pure typed-core path
    this file mirrors: it answers with a whole stc-go module, 40502 bytes of it.
    This port answered with its three-line banner -- 144 bytes, no marker
    anywhere -- and a reader of those bytes could not tell a tier that refuses
    the stream surface from one whose lowering is missing. Each component is
    now named, in document order, so the port's answer states which.

    The witness token is the marker: `<<UNSUPPORTED-COMPONENT:` is text only the
    port emits (the reference emits no `<<...>>` marker at all for this
    document), so this case cannot pass for an unrelated reason -- the defect
    item 1136 found five times over.
    """
    ir = compile_files([str(STREAM_130)])
    names = [component["name"] for component in ir["components"]]
    assert names == ["Consumer", "Parked", "Fanin", "Windowed", "Iterate", "Chain"]
    want, got = reference.emit(ir), emitted["emit_go_src"](ir)
    assert [line for line in got.splitlines() if line.startswith("<<")] == [
        f"<<UNSUPPORTED-COMPONENT:{name}>>" for name in names
    ], "one marker per components entry, in document order"
    assert_boundary_witness(want, got, "stc-go", "<<UNSUPPORTED-COMPONENT:Consumer>>")


def test_an_observable_component_on_the_pure_path_is_named_by_the_port(emitted,
                                                                       reference,
                                                                       tmp_path):
    """The pure path's component drop is NOT a silent agreement on either side.

    This case used to assert the opposite: the reference's pure typed-core path
    routed PAST the components of a document that also carries top-level
    declarations, so both sides dropped the same thing and agreed byte-for-byte,
    and the port was told not to mark what the reference also omitted.

    That agreement was two implementations of a fail-open. A module `fn` beside
    a component with provide methods is the ordinary shape of real revl code —
    every revl-harness component file is exactly it — and go answered it with a
    compiling package that had no services, no component and no routes, with no
    error on either side of the fork. Issue #721 reached it by migrating the
    harness's ternary dispatch to a provide-method if-chain: the reference
    emitted 6185 bytes of stdlib preamble and two free functions, and nothing
    that could serve a request.

    The reference CARRIES it now (issue #1321): the declarations and the live
    components in one package. So there is no agreement left for the port to
    protect, and the port names what it does not carry instead of matching a
    reference output that no longer exists. The boundary that remains is the
    genuinely incidental component, below.
    """
    path = tmp_path / "observable.rvl"
    # A config field is past the combined path's ported slice (issue #106,
    # COMBINED_CORPUS), so this component is still one the port names.
    path.write_text("fn f() -> Int { return 1 }\n"
                    "service S { fn g() -> Int }\n"
                    "component C provides s: S {\n"
                    "  config { n: Int = 1 }\n"
                    "  provide s { fn g() = 1 }\n"
                    "}\n")
    ir = compile_files([str(path)])
    want, got = reference.emit(ir), emitted["emit_go_src"](ir)
    assert 'Name: "C",' in want, "the reference carries the component"
    assert "func f() int64 {" in want, "and the declaration it was routed for"
    assert_boundary_witness(want, got, "stc.Component", "<<UNSUPPORTED-COMPONENT:C>>")


def test_the_combined_slice_carries_a_provide_only_component(emitted, reference,
                                                             tmp_path):
    """The same shape inside the ported slice is carried byte-for-byte now."""
    path = tmp_path / "carried.rvl"
    path.write_text("fn f() -> Int { return 1 }\n"
                    "service S { fn g() -> Int }\n"
                    "component C provides s: S { provide s { fn g() = 1 } }\n")
    ir = compile_files([str(path)])
    want, got = reference.emit(ir), emitted["emit_go_src"](ir)
    assert 'Name: "C",' in got and "<<UNSUPPORTED" not in got
    assert got == want


@pytest.mark.parametrize("rel", COMBINED_CORPUS)
def test_the_combined_corpus_builds_against_stc_go(emitted, rel):
    """The combined module imports stc-go, so it is built the way the carried
    set is (tools/validate.py), not with the module-less `go test` above."""
    sys.path.insert(0, str(ROOT / "tools"))
    import validate  # noqa: PLC0415

    validator = validate.VALIDATORS["go"]
    reason = validator.unavailable()
    if reason:
        pytest.skip("go toolchain unavailable: %s" % reason)
    ir = compile_files([str(CORPUS_DIR / rel)])
    results = validator.check([(rel, emitted["emit_go_src"](ir))])
    status, detail = results[rel]
    assert status == validate.OK, detail


def test_an_incidental_component_on_the_pure_path_is_not_marked(emitted, reference,
                                                                tmp_path):
    """The boundary of the rule above, so it is not read as wider than it is.

    The rule is to name every `components` entry the port does not carry EXCEPT
    where naming it would break a byte agreement the reference itself produces.
    That exception survives for a component with nothing to drop: no activation
    body and no provide method, so routing past it loses nothing anyone could
    have called and both sides still agree byte-for-byte.

    The suppression needs BOTH halves: pure declarations present, and no in-file
    `test` section. A document with a test section already diverges (this slice
    defers the whole section), so there is no agreement left to protect there
    and the marker is free -- which is also what carries the `lifecycle test`
    documents, whose components the reference keeps.
    """
    path = tmp_path / "incidental.rvl"
    path.write_text("fn f() -> Int { return 1 }\n"
                    "component C { }\n")
    ir = compile_files([str(path)])
    want, got = reference.emit(ir), emitted["emit_go_src"](ir)
    assert "<<UNSUPPORTED-COMPONENT" not in got
    assert got == want


def test_the_stream_diversion_with_top_level_declarations_is_named_now(emitted,
                                                                       reference):
    """The recorded residual of the rule, closed -- kept as the pin that it is.

    This case used to assert SILENCE. `has_top_level` was the half of the
    reference's routing predicate the port could state without branching on a
    component STEP; the other half is item 130's stream diversion, which sends a
    document that holds a stream to the live stc-go path even when it carries
    top-level declarations (a typed-event program always does -- the event's
    record declaration is what puts a `types` entry in the document). So the
    port suppressed its marker for this document while the reference answered
    with a whole live module, and the divergence went unnamed.

    Mirroring the diversion itself is still out of reach: it would mean reading
    the `subscribe` / `stream-iter` discriminants, and tools/selfhost_coverage.py
    takes a port's construct table straight off those spellings -- reading one
    here would move `subscribe=<true>` and `step=stream-iter` out of the go
    tier's `unported` baseline in tests/fixtures/selfhost_blind_spots.json and
    claim a port of the stream lowering this slice does not have.

    Issue #1321 closed the residual without doing that. The suppression now also
    requires that no component be OBSERVABLE, and `component_is_observable`
    reads the LENGTH of a component's `body` rather than the discriminant of any
    step inside it. A stream component has a body, so it is named -- and the go
    tier's blind-spot baseline is untouched.
    """
    ir = compile_files([str(ROOT / "backends" / "go" / "testdata"
                            / "stream_event_130.rvl")])
    assert ir["components"] and ir["types"], (
        "the residual is the stream document that ALSO declares a type"
    )
    names = [component["name"] for component in ir["components"]]
    want, got = reference.emit(ir), emitted["emit_go_src"](ir)
    assert [line for line in got.splitlines() if line.startswith("<<")] == [
        f"<<UNSUPPORTED-COMPONENT:{name}>>" for name in names
    ], "one marker per components entry, in document order"
    assert_boundary_witness(want, got, "stc-go",
                            f"<<UNSUPPORTED-COMPONENT:{names[0]}>>")


def test_a_source_only_stream_acquisition_is_named_too(emitted, reference,
                                                      tmp_path):
    """The stream shape `stream_event_130.rvl` does not reach, held against the
    same rule.

    That fixture carries every stream discriminant at once, so the case above
    passes on the loop step alone and says nothing about a document that holds a
    stream without one. A source-only program is the one that is easiest to
    leave out and the hardest to notice missing: `Stream.source()` with nothing
    reading it carries no subscription flag and no loop step, yet it opens a
    live host listener with a `Close` inverse on the teardown stack, which is
    what makes the component non-incidental and what sends the document to the
    reference's live path.

    The port names it for a reason that does not mention streams at all: the
    acquisition is a `body` step, so `component_is_observable` is true, so the
    suppression does not fire. That is the whole point of keying the marker on
    the length of `body` rather than on a step discriminant. This case is the
    evidence that the cheaper predicate actually covers the shape the expensive
    one was proposed for, and it is the case that reds first if the suppression
    is ever widened back toward reading `subscribe` / `stream-iter`.
    """
    path = tmp_path / "source_only.rvl"
    path.write_text("type Reading = { value: Int }\n"
                    "component Source {\n"
                    "  let src = effect Stream.source() undo src.close()\n"
                    "}\n")
    ir = compile_files([str(path)])
    assert ir["types"], "the top-level declaration is what arms the suppression"
    steps = ir["components"][0]["body"]
    assert not any(step.get("subscribe") or step.get("step") == "stream-iter"
                   for step in steps), (
        "the case is only about the acquisition while the document carries "
        "neither of the other two stream discriminants"
    )
    want, got = reference.emit(ir), emitted["emit_go_src"](ir)
    assert_boundary_witness(want, got, "stc-go",
                            "<<UNSUPPORTED-COMPONENT:Source>>")


def test_a_bracket_subscription_alone_is_named_too(emitted, reference,
                                                   tmp_path):
    """The subscription without a loop, for the same reason as the case above:
    a subscription nothing reads is still a bracket with an inverse, the
    reference still diverts it off the pure typed-core path, and the port still
    names it off the length of `body`."""
    path = tmp_path / "subscribe_only.rvl"
    path.write_text("type Reading = { value: Int }\n"
                    "component Parked {\n"
                    "  let src = effect Stream.source() undo src.close()\n"
                    "  let sub = subscribe src undo sub.close()\n"
                    "}\n")
    ir = compile_files([str(path)])
    steps = ir["components"][0]["body"]
    assert any(step.get("subscribe") for step in steps)
    assert not any(step.get("step") == "stream-iter" for step in steps)
    want, got = reference.emit(ir), emitted["emit_go_src"](ir)
    assert_boundary_witness(want, got, "stc-go",
                            "<<UNSUPPORTED-COMPONENT:Parked>>")


def test_a_required_stream_coeffect_is_named_by_the_port(emitted, reference,
                                                         tmp_path):
    """item 130 6b (issue #81): the reference refuses this document by name, and
    the port must not answer it with silence.

    A requirement resolves against a SERVICE on this tier and a `Stream[T]` is
    not one, so `backends/go/emit.py` raises rather than emitting. Before issue
    #1321 the port answered the same document with its banner plus the event's
    record type and nothing else, 242 bytes against an `EmitError`, with no
    marker anywhere: a stated refusal made silent by the port, which is the one
    direction the byte-agreement oracle exists to rule out.

    `component_is_observable` closes it without reading a stream spelling. The
    component has an `on ... as` body step, so it is observable, so the
    suppression does not fire and the component is named. The go tier's
    `unported` baseline in tests/fixtures/selfhost_blind_spots.json is untouched
    and no port of the stream lowering is claimed.
    """
    path = tmp_path / "coeffect.rvl"
    path.write_text("""
event OrderCreated(key: order_id) { order_id: Str, quantity: Int }
service Ship { emission fn dispatch(id: Str) }

component Fulfiller requires feed: Stream[OrderCreated], ship: Ship {
  on OrderCreated as e { emit ship.dispatch(e.order_id) }
}
""")
    ir = compile_files([str(path)])
    assert ir["types"], (
        "the case is only interesting while the document ALSO carries a "
        "top-level declaration, which is what arms the suppression"
    )
    with pytest.raises(reference.EmitError) as excinfo:
        reference.emit(ir)
    assert "a required `Stream[T]` coeffect is not lowered" in str(excinfo.value), (
        "the reference no longer refuses the coeffect by name"
    )
    got = emitted["emit_go_src"](ir)
    assert "<<UNSUPPORTED-COMPONENT:Fulfiller>>" in got


def test_a_stream_coeffect_on_an_empty_component_stays_suppressed(emitted,
                                                                  reference,
                                                                  tmp_path):
    """The boundary on the test above, and the reason the port reads `body`
    rather than the requirement's declared type text.

    A `Stream[T]` requirement is not by itself a reason to name a component.
    With no body there is nothing for the reference to lose by routing past it,
    so the reference takes the pure typed-core path and EMITS, refusing nothing.
    A port that keyed the marker on the requirement type would answer that
    document with a marker the reference has no counterpart for, and would pay
    for it out of the byte agreements the suppression exists to buy.
    """
    path = tmp_path / "empty_coeffect.rvl"
    path.write_text("""
event OrderCreated(key: order_id) { order_id: Str, quantity: Int }
service Ship { emission fn dispatch(id: Str) }

component Fulfiller requires feed: Stream[OrderCreated], ship: Ship { }
""")
    ir = compile_files([str(path)])
    assert ir["types"] and not ir["components"][0]["body"], (
        "the case is a top-level declaration beside a component with nothing "
        "in it, which is what arms the suppression"
    )
    want, got = reference.emit(ir), emitted["emit_go_src"](ir)
    assert "<<UNSUPPORTED-COMPONENT" not in got
    assert got == want


def test_the_suppression_still_holds_for_a_service_only_requirement(
        emitted, reference, tmp_path):
    """The non-vacuity control: the same document shape with an ordinary service
    requirement and an empty body still routes to the reference's pure
    typed-core path, still drops its component on both sides, and is still
    byte-identical."""
    path = tmp_path / "service_req.rvl"
    path.write_text("""
type Order = { id: Str }
service Ship { fn dispatch(id: Str) }

component Fulfiller requires ship: Ship { }
""")
    ir = compile_files([str(path)])
    got = emitted["emit_go_src"](ir)
    assert "<<UNSUPPORTED-COMPONENT" not in got, (
        "the suppression has narrowed past an unobservable component and is "
        "now costing the byte agreements it exists to buy"
    )
    assert got == reference.emit(ir)


def test_record_update_is_a_reference_refusal(emitted, reference):
    """`tests/fixtures/emit_go_refusals/record_update.rvl`: the reference
    refuses functional record update by name, and the port names the node."""
    path = ROOT / "tests" / "fixtures" / "emit_go_refusals" / "record_update.rvl"
    ir = compile_files([str(path)])
    with pytest.raises(reference.EmitError, match="record.update"):
        reference.emit(ir)
    assert "<<UNSUPPORTED-EXPR:record_update>>" in emitted["emit_go_src"](ir)


def test_a_fault_test_the_reference_refuses_is_named_here_too(emitted, reference):
    """`tests/fixtures/emit_go_refusals/fault_test_section.rvl`: the reference
    refuses a `fault test` section by name, and the port, which has no refusal
    channel, names the section and the component instead of answering with a
    module that drops them."""
    path = ROOT / "tests" / "fixtures" / "emit_go_refusals" / "fault_test_section.rvl"
    ir = compile_files([str(path)])
    with pytest.raises(reference.EmitError, match="fault tests do not lower"):
        reference.emit(ir)
    got = emitted["emit_go_src"](ir)
    assert "<<UNSUPPORTED-FAULT-TEST:probe>>" in got
    assert "<<UNSUPPORTED-COMPONENT:P>>" in got
