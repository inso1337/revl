"""The self-hosted cordis4j EMITTER (selfhost/emit_java.rvl, roadmap item 199 —
Path B slice 1 for the Java tier): compiled by revl, emitted through the python
backend, executed, and cross-checked BYTE-FOR-BYTE against the reference emitter
(backends/java/emit.py's ``emit``) over a corpus of interchange-IR documents.

This has the exact shape of tests/test_selfhost_emit_rust.py: two independent
implementations of one lowering — the reference Java backend and its revl port —
are forced to agree on emitted Java source, down to the last byte. A divergence
requires inspecting both implementations, not assuming reference correctness.
This byte oracle alone does not prove runtime semantics: it compares the EMITTED
SOURCE STRINGS (a pure-Python comparison), so it needs no Java toolchain. A
focused activation test below separately checks null-bearing metadata on a JVM.

The frontend selects v1/v2/v3 according to document features. The corpus covers
scalar legacy providers, the v3 typed core, and the modern component subset
below. Float arithmetic/widening and Opt are supported; only Float-to-string
formatting and built-in Result runtime emission remain outside that core.

Covered subset (what emits byte-identical):
  * slice 1 — the module scaffold (banner comments, ``package revl;``, the five
    unconditional ``import io.cordis4j.core.*;`` lines, the
    ``public final class Components { private Components() {} }`` wrapper);
    ``_emit_v3_functions`` (each fn a ``public static`` method, ``_java_v3_type``
    for scalar/``List``/``Opt``/``Map``/``Result`` types, ``_fn_name`` renaming,
    ``// (empty body)``); ``_v3_stmt`` (let/assign, return, if/while/for,
    bare-expr, assert); and the base ``_expr`` algebra (lit, name, var incl
    ``None`` -> ``Optional.empty()``, bin incl ``??``/``==``/exact-arith/``/``/
    comparisons/``&&``, un incl ``revlNeg``, the 2.0 call and the
    ``fn`` call with ``Some``/``None``, the ``widen`` markers, field, index,
    ternary, list, empty ``maplit``, non-float ``${..}`` interpolation).
  * slice 2 (item 210) — the v3 TYPED-CORE: ``_emit_v3_types`` (a ``record`` type
    as a ``public static final class`` with public final fields + a canonical
    ctor; a ``variant`` type as a ``public sealed interface … permits …`` with a
    ``final class … implements`` per case, nullary or single-``value`` payload);
    the ``_V3Ctx`` type-table inference (``case_owners``, has-payload,
    ``record_by_fields``, and each variant's full case set); ADT construction
    (the ``adt`` node AND a ``call``/``var`` naming a user case, both
    ``new Owner.Case(args)``); the ``_adt_binding_type`` ``let`` (``final Owner
    o``); record LITERALS (``new Type(args)`` with the argument order taken from
    the DECLARED field order); ``match`` over user variants (the pattern
    ``switch`` with the document-wide ``__revl_case_N`` / ``__revl_ignored_N``
    temp numbering threaded as an Int counter, the ``.value`` unpack, and the
    ``_covers_variant`` omission of the ``default`` throw for a total match); and
    the built-in Opt ``Some``/``None`` match (``.map(..).orElseGet(..)``).
  * slice 3 (item 216) — the SIMPLE component/service unit: a ``service`` as a
    ``public interface`` (``_emit_service_interfaces_v3``), and the LEGACY
    ``_emit_component`` simple-provider path (the branch ``_component_needs_modern``
    leaves False) — one ``public static final class <Comp><Camel(key)> implements
    <Svc>`` per provision (empty ctor, one inline method whose body is
    ``_method_body``: a lone ``return <lit|name>``, a void op run for effect, or
    the ``UnsupportedOperationException("effectful method body")`` trap for an
    empty/multi-step body), then the ``<Comp>Plugin`` (the no-config
    ``_emit_plugin_ctors`` ctor + the A8 self-revert ``apply`` that registers each
    ``provide`` step onto a LIFO undo list).

  * slice 3 addendum (item 225) — the MODERN component path
    (``_emit_component_modern``) config/req/effectful CORNER: component ``config``
    (the parameterised + no-arg ``<Comp>Plugin`` ctors via ``_emit_plugin_ctors``,
    ``_config_default_lit`` / ``_zero_java_value``); required-service routing (the
    provider-class ``Context``/``Context.EffectScope fx``/``<Svc>`` fields + ctor,
    the ``ctx.get(<Svc>.class)`` locals, and the ``this.<req>`` capture rename);
    effectful provide-method bodies (``_method_body_lines`` ``return``/``effect``
    +``undo``/``emit`` +``compensate`` as ``fx.track(Disposables.of(() -> ..))``);
    and the ``apply`` that opens ``ctx.effect()`` and registers every provision
    under the A8 self-revert ``try/catch`` returning ``fx``. Routed to only when a
    component ``needs_modern`` AND falls in the supported subset (every body step a
    ``provide``, every method step ``return``/``effect``/``emit``); anything else
    stays a loud ``<<UNSUPPORTED-component-nonsimple>>``.

  * slice 4 (item 235) — REALM placements on the modern path: an ``isolate <key>
    in realm("..")`` opens the ``apply()`` body with ``ctx = ctx.isolate(<Svc>.class,
    "..")`` (the service resolved through ``provides`` else ``requires``); an
    ``intercept <key> with <meta>`` emits ``ctx.intercept(ServiceKey.of(<Svc>.class),
    <meta>)`` (service through ``requires``) with ``_metadata_lit`` rendering nested
    map/list/int/float/bool/string metadata byte-for-byte. Both make the component
    ``needs_modern`` and are admitted so long as the body/method shapes stay inside
    the supported subset. Activation async/await is covered by slice 5.

  * slice 5 (item 238) — the ASYNC activation body + host Map/Job stubs: a
    body-level ``let-effect`` host bind (``_emit_host_stubs`` Map runtime, the
    per-site ``_map_value_expr_type`` value inference pinning ``Map<V>``, and the
    ``Map<V>`` provider field/ctor/apply-local), body-level
    ``effect``/``emit``/``if``/``fail``/``await`` activation steps
    (``_emit_component_stmts``), the ``await``->``AsyncPlugin`` async coloring
    (``apply(..) throws Exception``, the ``_await_join`` ``.await()``, the ``Job``
    runtime) and the ``fail``->``CordisException`` throw — both widening
    ``_core_imports`` (``AsyncPlugin``/``CordisException``). Admitted when every
    body step is ``provide``/host-``let-effect``/``effect``/``emit``/``await``/
    ``fail``/``if`` and every provide-method step is ``return``/``effect``/``emit``.

Deliberately OUT (excluded from the corpus, deferred to Java Path B slice 6+):
  * spawn/instances (``_v3_spawn``/``instance-get``/``RevlSpawnHandle``), timers,
    routers, and the ``Pool`` host runtime (a ``Pool`` acquisition surfaces a loud
    ``<<UNSUPPORTED-host-stub:Pool>>``); ``setup`` blocks on effect steps, method-body
    ``let``/``assign``/``await``/arrow bindings, and body-level ``while``/``for``/
    ``let_pattern`` (a component outside the supported subset surfaces a loud
    ``<<UNSUPPORTED-component-nonsimple>>``);
  * the built-in Result surface (``Ok``/``Err`` ctor
    and ``match``, ``_emit_result_type``, ``_emit_checked_div_helpers``) and the
    ``revlDivFloor``/``revlDivEuclid``/``revlMod`` int-arith block, so the
    ``checked_*`` and ``div_floor``/``div_euclid``/``mod`` builtin methods stay
    behind loud ``<<UNSUPPORTED-checked-div:..>>`` / ``<<UNSUPPORTED-int-arith:..>>``
    markers;
  * functional record-update ``{r | f = e}`` (the Java reference itself RAISES on
    ``record_update`` — a structural exclusion);
  * externs / in-file ``test`` /
    lifecycle-test emission; the canonical Float->Str ``revlFtoa`` (float
    interpolation excluded); the ``_reject_fn_type`` refusal; local ``let``-bound
    arrows and their ``_inline_arrow`` beta-reduction; and ``let_pattern`` (its
    temp name carries a per-``_V3Ctx`` emission-order index this port does not
    thread yet).
"""

import importlib.util
import re
import subprocess
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_files  # noqa: E402

sys.path.insert(0, str(ROOT / "tests"))

from _boundary_witness import (  # noqa: E402
    assert_boundary_witness, shared_witness_token_reason)

CORPUS_DIR = ROOT / "tests" / "fixtures" / "emit_java_corpus"
CORPUS = [
    # slice 1 (item 199) — functions-only base surface
    "arith.rvl",     # bounded int/int32, /, %, comparisons, ==/!=, unary, ??
    "bitwise.rvl",  # Int32 bitwise & | ^ << >> and unary ~ (item 366, item 391 self-host port)
    "control.rvl",   # let/var/assign, if/else, while, for, bare-expr, assert
    "calls.rvl",     # free-function calls
    "strings.rvl",   # string `+`, `${..}` interpolation, literals
    "lists.rvl",     # list literal, index, nested list types
    "boxed_generics.rvl",  # Int32/Any type arguments, generic record parameters (#2303)
    "maps.rvl",      # the empty map literal and the Map/List generic type lowering
    # slice 2 (item 210) — the v3 typed-core
    "records.rvl",   # record decls, out-of-order literals, field access, nesting
    "adts.rvl",      # sealed-interface variants, `adt` ctors, `match` (exhaustive/
                     # wildcard/partial-default/nested), the `final Owner` let
    "optmatch.rvl",  # the built-in Opt Some/None match (.map/.orElseGet)
    # slice 3 (item 216) — the SIMPLE component/service unit
    "service.rvl",       # one `service` interface + a lone pure-provider component:
                         # `_emit_service_interfaces_v3`, the legacy `_emit_component`
                         # simple path (empty provider class + inline `return <lit>`),
                         # and the `<Comp>Plugin` `apply` provisioning
    "services_multi.rvl",# two services, one component providing both — multi-provision
                         # classes, `return <name>`, the empty-void-body trap now a real
                         # no-op (`public void reset() {  }`), and the scalar param/return
                         # surface (long/boolean/String/void)
    # slice 3 addendum (item 225) — the MODERN component path (config/req/effectful)
    "comp_config_req.rvl",  # config fields (`_emit_plugin_ctors` param+default ctors,
                            # `_config_default_lit`/`_zero_java_value`) + a required
                            # service (provider `Context`/`fx`/`Sink` fields, ctor, and
                            # the `ctx.get(Sink.class)` local) + effectful provide-method
                            # bodies (`effect`/`undo`/`emit` through the `this.sink`
                            # rename; the `Context.EffectScope fx` provider shape, the A8
                            # self-revert `apply` try/catch returning `fx`)
    "comp_config_provide.rvl",  # item 433 rider R3: a provide method that READS
                                # `config.<f>`. Forces the modern path (the legacy
                                # provider class has no config field, so the bare
                                # `prefix` did not compile) and pins the provider's
                                # config field/ctor-param/ctor-arg threading.
    # comp_multi_effect.rvl (a NO-config, MULTI-provision modern component: the
    # no-arg-only `<Comp>Plugin` ctor, two provider classes routed in one
    # `apply`, an `emit ... compensate` pair, and an `effect`/`undo` body in a
    # void observation method) exercises the item-243-Slice-2b RevlFrame two-
    # phase teardown loop (docs/design/teardown-contract.md): the reference
    # (backends/java/emit.py) routes `compensate` and every bracket sharing its
    # frame-bearing component through `RevlFrame.compensation`/`.bracket` instead
    # of the flat `fx.track(Disposables.of(() -> ..))`, threads `frame` through
    # each provider ctor, and returns a `RevlActivation(fx, frame)` after
    # `frame.commit()`. Item 321 ported ALL of that surface — the shared
    # `RevlFrame`/`RevlActivation` runtime, `_call_label`, `_component_needs_frame`
    # and the bracket/compensation routing — into selfhost/emit_java.rvl, so this
    # entry now emits byte-identical and is a plain string (no longer xfail). (The
    # method-body WITNESSED/`transactionalMethod` path the reference also carries
    # is not exercised by any corpus fixture — item 324 — and its per-body temp
    # name is a host identity no port can reproduce, so it stays out of the
    # byte-checked surface, like `let_pattern`.)
    "comp_multi_effect.rvl",
    # slice 4 (item 235) — REALM placements (isolate/intercept) on the modern path
    "comp_realm_isolate.rvl",  # `isolate <provided-key> in realm("..")`: the modern path
                               # now admits a realm placement (a pure-`return` provider),
                               # emitting the `ctx = ctx.isolate(<Svc>.class, "..")` header
                               # line (service resolved through `provides`)
    "comp_realm_intercept.rvl",# `isolate <provided>` + `intercept <required> with <meta>`:
                               # the `ctx.intercept(ServiceKey.of(<Svc>.class), <meta>)` line
                               # with `_metadata_lit` rendering a nested map/list/int/bool/
                               # string literal byte-for-byte (service through `requires`)
    # slice 5 (item 238) — async activation bodies + host Map/Job stubs
    "comp_await.rvl",       # an `await Job.run(..)` step between two effects: the async
                            # coloring (`AsyncPlugin` interface, `apply(..) throws Exception`,
                            # the `AsyncPlugin` import widening), the emitted `Job` runtime
                            # (`_emit_host_stubs`), and the `_await_join` `.await()`
    "comp_host_map.rvl",    # a host-Map `let-effect` bind (`Map.new()`/`Map.create()`): the
                            # `Map<V>` runtime (`_emit_host_stubs`), per-site
                            # `_map_value_expr_type` value inference (`Map<java.lang.Long>`),
                            # and the `Map<V>` provider field/ctor/apply-local declarations
    "comp_host_map_generic.rvl",  # the SAME host-Map path with a NESTED-generic value type
                            # (`Map[Str, List[Msg]]` -> `Map<java.util.List<Msg>>`): the
                            # item-77 follow-up shape item 275 tracked — the reference emits
                            # `Map<V>` per site, the old mirror left `Map` raw. Pins the
                            # generic-`Map` drift in the byte-identity oracle on every run.
    "comp_fail.rvl",        # an `if (..) { fail ".." }` activation guard: the `fail` ->
                            # `throw new CordisException(String.valueOf(..))` lowering, the
                            # `CordisException` import widening, a body-level `if`, and a
                            # config field with no default (`null`)
    # item 217 — the v1 front-door: a components-ONLY document lowers to
    # `ir_version 1` (not v3), so it forces the versioned banner (version-1
    # `// Generated by ...`). The covered SCALAR service/comp body is
    # byte-identical to the v3 path. (A v1 doc whose generics box to the SHORT
    # `Long`/`Integer` form — vs v3's `java.lang.Long` — is out of this slice's
    # byte-checked surface; the fixture keeps signatures scalar.)
    "v1_components.rvl",
    "legacy_void.rvl",
    "legacy_realms.rvl",
    "metadata_null.rvl",
    # Item 429: existing agreeing documents with measured additional coverage.
    "../../../examples/tenants.rvl",
    "../../../bench/results/baseline-deepseek-v4-pro/09-warmup-cache/v2/attempt-1.rvl",
    "../../../bench/results/baseline-deepseek-v4-pro/05-rate-limiter/v1/attempt-1.rvl",
    "../emit_ts_corpus/services_async.rvl",
    "../../../bench/results/baseline-deepseek-v4-pro/26-log-rotator/v2/attempt-2.rvl",
    "../emit_wasm_corpus/constfold.rvl",
    "../../../backends/go/scenarios/tagger.rvl",
    "../../../examples/ecosystem-consumer-js/candidates/double_tool.rvl",
    "../../../bench/results/baseline-deepseek-v4-pro/18-config-echo/v1/attempt-1.rvl",
    "../emit_go_corpus/variants.rvl",
    "../emit_go_corpus/records.rvl",
    "../emit_ts_corpus/realm_intercept.rvl",
    "../emit_py_corpus/services_config.rvl",
    "../emit_py_corpus/services_method_effects.rvl",
    "../emit_ts_corpus/components_mixed.rvl",
    "../emit_wasm_corpus/listmem.rvl",
    "../erase_realms.rvl",
    "../realm_conformance/provider_a.rvl",
    "match_ignored.rvl",
    "float_numeric.rvl",
    "component_format.rvl",
    "branch_shapes.rvl",
    "component_branches.rvl",
    "map_inference.rvl",
    # Item 429 blind-spot closure (java): the stdlib surface. Before these two
    # the java corpus spelled no `builtin` node at all, so `kind=builtin` and
    # every `method=..` arm below it was implemented on BOTH sides with nothing
    # holding them to each other.
    "stdlib_builtins.rvl",       # every covered `builtin` method, the `len` node
                                 # and the property-form `.length`, plus the
                                 # per-group helper splice that selects exactly
                                 # the `revl*` groups the emitted text calls
    "map_inference_builtins.rvl",# `_map_value_expr_type`'s `builtin` arm, one
                                 # host Map per answer so a missing arm shows up
                                 # as a `Map<String>` fallback in the emitted
                                 # field/ctor/local declarations
    # Other tiers' fixtures that the stdlib port brought into agreement. A
    # differential sweep of every `.rvl` in the tree moved 60 documents from
    # diverging to byte-identical; these carry the stdlib shapes the two
    # documents above do not spell, in bodies written for other tiers.
    "../emit_ts_corpus/property_edges.rvl",   # `config.label.length` in a provide
                                              # method: the `sized_length` field
    "../emit_py_corpus/strings.rvl",          # Str builtins through fn bodies
    "../emit_ts_corpus/strings.rvl",
    "../emit_rust_corpus/lengths.rvl",        # `len` vs `length()` side by side
    "../emit_wasm_corpus/string_ops.rvl",
    "../emit_py_corpus/annotated_lets.rvl",
    "../emit_rust_corpus/perf_index.rvl",
    "../emit_wasm_corpus/forloop.rvl",
    # issue #1972 deliberately does NOT join
    # `../emit_py_corpus/ui_transaction_unit.rvl` here. That document's fourth
    # case is a computer-use extern that DECLARES `compensate`, which the
    # reference registers at every crossing through `RevlDeclared`;
    # `selfhost/emit_java.rvl` does not port that registration (issue #1511,
    # recorded unported in `selfhost_blind_spots.json`). Its `act` method also
    # carries a `let`, and the component renderer here admits only
    # `return`/`effect`/`emit` method steps (`body_step_supported`), so the
    # component is refused with `<<UNSUPPORTED-component-nonsimple:Agent>>`. Adding
    # the document would make this oracle red, so the UI transaction unit stays
    # unported on this tier and `class=emission` stays a recorded unported gap.
    "to_char.rvl",  # Int.to_char, the primitive behind str_char (issue #2285)
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
    got = emitted["emit_java_src"](ir)
    assert got == want, (
        f"self-hosted emitter diverged from the reference on {rel}\n"
        f"--- lengths ref={len(want)} got={len(got)} ---"
    )


def test_selfhosted_emitter_output_scaffold(emitted):
    """A byte-identical output is trivially valid Java source; pin the scaffold
    and a representative body detail so a regression in the header or the
    trapping-arithmetic lowering surfaces here, not only in the byte diff."""
    ir = compile_files([str(CORPUS_DIR / "arith.rvl")])
    src = emitted["emit_java_src"](ir)
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


def test_fn_type_param_refused(emitted, reference):
    """item 383 / 391 (self-host port): a `.map`/`.filter`/`.reduce` transform
    lowers (in the frontend) to a `list_*` free function with a function-value
    parameter, and a declared function type is not representable on this tier.
    The reference RAISES EmitError; a pure self-host emitter fn cannot `fail`, so
    it refuses within the subset with a loud `<<UNSUPPORTED-fn-type>>` marker instead of
    silently emitting a mis-typed signature. Both refuse the same construct, so
    tests/fixtures/emit_*_corpus/transforms.rvl stays OUT of the byte-identity
    CORPUS above (the reference would raise there) and is checked only here."""
    ir = compile_files([str(CORPUS_DIR / "transforms.rvl")])
    with pytest.raises(reference.EmitError, match="function type"):
        reference.emit(ir)
    got = emitted["emit_java_src"](ir)
    assert "<<UNSUPPORTED-fn-type>>" in got


def test_null_intercept_metadata_activates_on_jvm(reference, tmp_path):
    """Java immutable collection factories reject null before intercept runs."""
    backend = ROOT / "backends" / "java"
    if str(backend) not in sys.path:
        sys.path.insert(0, str(backend))
    import javac_gate

    if javac_gate.JAVAC is None or javac_gate.JAVA is None:
        pytest.skip(javac_gate.NO_JDK)
    source = reference.emit(compile_files([str(CORPUS_DIR / "metadata_null.rvl")]))
    out = javac_gate.compile_unit(tmp_path, source)
    harness = tmp_path / "revl" / "Harness.java"
    harness.write_text(
        """package revl;
public final class Harness {
    public static void main(String[] args) {
        io.cordis4j.core.Context ctx = new io.cordis4j.core.Context();
        ctx.provide(io.cordis4j.core.ServiceKey.of(Components.Sink.class, "sink"),
                message -> {});
        new Components.MetadataPlugin().apply(ctx).dispose();
        System.out.println("ok");
    }
}
""",
        encoding="utf-8",
    )
    compiled = subprocess.run(
        [javac_gate.JAVAC, "--release", javac_gate.RELEASE, "-cp", str(out),
         "-d", str(out), str(harness)],
        capture_output=True, text=True, timeout=600,
    )
    assert compiled.returncode == 0, compiled.stderr
    ran = subprocess.run(
        [javac_gate.JAVA, "-cp", str(out), "revl.Harness"],
        capture_output=True, text=True, timeout=600,
    )
    assert ran.returncode == 0, ran.stderr
    assert ran.stdout == "ok\n"


@pytest.mark.parametrize(
    "rel, reference_fragment, port_fragment",
    [
        ("backends/java/scenarios/instance_accessor.rvl",
         "static final class RevlSpawnHandle", "<<UNSUPPORTED-component-nonsimple:Supervisor>>"),
        ("backends/go/scenarios/router.rvl",
         "class RevlRouter", "<<UNSUPPORTED-component-nonsimple:Router>>"),
        ("bench/results/baseline-deepseek-v4-pro/02-pg-pool/v1/attempt-1.rvl",
         "class Pool", "<<UNSUPPORTED-host-stub:Pool>>"),
        ("backends/go/testdata/result_ctor_302.rvl",
         "new RevlResult.Ok<>", "<<UNSUPPORTED-result-ctor:Ok>>"),
        ("tests/fixtures/emit_py_corpus/control.rvl",
         "revlLength(", "<<UNSUPPORTED-EXPR:arrow>>"),
        # The stdlib half of this document now agrees byte-for-byte (see
        # emit_java_corpus/stdlib_builtins.rvl); what still diverges is its
        # `Pool` host runtime.
        ("backends/java/scenarios/runtime_values.rvl",
         "class Pool", "<<UNSUPPORTED-host-stub:Pool>>"),
        ("tests/fixtures/emit_py_corpus/floats.rvl",
         "private static String revlFtoa(double x)", None),
        ("bench/codegen/java/cases/extern_config/case.rvl",
         "_revlExternConfig(", None),
        ("backends/java/scenarios/method_witnessed.rvl",
         "instanceof RevlResult.Ok<?, ?>", "<<UNSUPPORTED-EXPR:>>"),
        ("backends/go/scenarios/records.rvl", "REVL_TESTS", None),
    ],
)
@pytest.mark.xfail(
    strict=True, raises=AssertionError,
    reason="Declared Java Path B boundary; promote to CORPUS when byte parity lands",
)
def test_declared_boundary_still_diverges(
    emitted, reference, rel, reference_fragment, port_fragment,
):
    """Keep executable evidence for deferred families, outside agreeing CORPUS."""
    ir = compile_files([str(ROOT / rel)])
    want = reference.emit(ir)
    got = emitted["emit_java_src"](ir)
    if reference_fragment not in want:
        pytest.fail(f"{rel} no longer exercises its reference boundary")
    # A port fragment the reference emits too witnesses nothing (item 1136).
    # This arm reports through pytest.fail rather than an assertion because the
    # case is xfail-strict on AssertionError and would otherwise swallow it.
    reason = shared_witness_token_reason(want, port_fragment)
    if reason is not None:
        pytest.fail(f"{rel}: {reason}")
    if port_fragment is not None and port_fragment not in got:
        pytest.fail(f"{rel} no longer exercises its port boundary")
    if port_fragment is None and reference_fragment in got:
        pytest.fail(f"{rel} gained its missing block; review and promote the case")
    assert got == want


# ---------------------------------------------------------------------------
# The deferred in-file test section stays LOUD (issue #1123).
#
# The self-host emitters' safety argument is that an unported construct answers
# with a named `<<UNSUPPORTED-...>>` marker rather than with silence: a marker is
# visible in the emitted bytes and is pinned here, while a section the port
# simply skips is invisible to the byte oracle (no corpus document on any tier
# carries a test section) and is counted as mirrored by
# tools/selfhost_coverage.py.
#
# In-file `test` / `fault test` / `lifecycle test` emission is deferred out of
# every self-host slice, and all six ports used to emit NOTHING for it. Each now
# emits one named marker per test, per section.
IN_FILE_TEST_SRC = 'fn f() -> Bool { return true }\ntest "probe" { assert f() }'

LIFECYCLE_TEST_SRC = """service Ping { fn ping() -> Int }
component P provides p: Ping {
  provide p { fn ping() = 1 }
}
lifecycle test "probe" {
  load P
  assert true
}
"""

FAULT_TEST_SRC = """service Ping { fn ping() -> Int }
component P provides p: Ping {
  let scratch = effect Map.new() undo scratch.drop()
  provide p { fn ping() = 1 }
}
fault test "probe" for P {
  fail at step 1
  assert no residue
}
"""

@pytest.mark.parametrize("source, reference_token, port_token", [
    pytest.param(IN_FILE_TEST_SRC, "public static void testProbe() {",
                 "<<UNSUPPORTED-TEST:probe>>", id="in-file-tests"),
    pytest.param(LIFECYCLE_TEST_SRC, "public static void lifecycleProbe() {",
                 "<<UNSUPPORTED-TEST:probe>>", id="lifecycle-tests"),
])
def test_deferred_test_sections_are_named(emitted, reference, tmp_path, source,
                                          reference_token, port_token):
    """These witnesses are not byte-agreement CORPUS; a port closes the reason."""
    path = tmp_path / "boundary.rvl"
    path.write_text(source)
    ir = compile_files([str(path)])
    want, got = reference.emit(ir), emitted["emit_java_src"](ir)
    assert reference_token in want
    assert port_token in got
    assert got != want, "boundary is stale: move its witness into CORPUS"


def test_a_fault_test_section_is_a_reference_refusal_and_a_named_port_marker(
        emitted, reference, tmp_path):
    """`fault test` runs on the python reference tier only (docs/fault-tests.md),
    so this reference emitter refuses the whole document by name. The port has no
    refusal channel - a pure self-host emitter fn cannot `fail` - so it names the
    section with a marker instead of dropping it."""
    path = tmp_path / "fault.rvl"
    path.write_text(FAULT_TEST_SRC)
    ir = compile_files([str(path)])
    with pytest.raises(reference.EmitError, match="fault tests do not lower"):
        reference.emit(ir)
    assert "<<UNSUPPORTED-FAULT-TEST:probe>>" in emitted["emit_java_src"](ir)


# item 130 (issue #81): the stream surface this port does not carry
# ---------------------------------------------------------------------------
#
# The reference emitter lowers the whole `Stream[T]` surface on this tier; the
# Path B port does not, and `tests/fixtures/selfhost_blind_spots.json` carries
# that as a named `unported` baseline. The baseline records the GAP. What was
# never checked is that the port is LOUD about it: the ledger is satisfied by a
# port that silently emits a module with the subscription missing, which is the
# section-level silence issue #1123 found for the in-file test section and the
# worst answer item 130 admits for a stream. So the marker is pinned here,
# where it runs.


def test_the_stream_surface_is_named_not_dropped(emitted, reference):
    """This port defers the whole non-simple component, so its marker names the
    COMPONENT rather than the stream step inside it. That is still a refusal by
    name — the emitted unit says which component it did not carry — and the
    point of pinning it is that the day the component path grows far enough to
    render this one, the stream inside it must not slip through silently."""
    ir = compile_files([str(ROOT / "backends" / "go" / "testdata"
                            / "stream_event_130.rvl")])
    want = reference.emit(ir)
    got = emitted["emit_java_src"](ir)
    assert_boundary_witness(want, got, "Stream.subscribe(",
                            "<<UNSUPPORTED-component-nonsimple:")


# item 560 / issue #1914: the one key pair the injective mangling cannot split
# ---------------------------------------------------------------------------
# `tests/fixtures/emit_java_refusals/host_identifier_collision.rvl` is the only
# input that reaches the namespaced branch of the mangling and the refusal arm of
# `_check_host_keys`, so `tools/selfhost_line_coverage.py` drives both halves
# over it. Its refusal text is asserted here.
COLLISION_DOC = (ROOT / "tests" / "fixtures" / "emit_java_refusals"
                 / "host_identifier_collision.rvl")


def test_a_host_identifier_collision_is_refused_by_name(reference):
    """A namespaced key and the unqualified key that already spells its mangled
    identifier are two DISTINCT wiring keys that must not land on one host
    identifier. The reference refuses to guess which one was meant, by name."""
    ir = compile_files([str(COLLISION_DOC)])
    with pytest.raises(reference.EmitError,
                       match="both lower to the Java identifier"):
        reference.emit(ir)


# issue #2383: each reference refusal the class body cannot carry is named by
# the port, in the spelling the rust and ts ports use.
JAVA_NAMED_REFUSALS = [
    ("emit_java_refusals/deferred_emission_call.rvl",
     "<<UNSUPPORTED-DEFERRED-EMISSION:Agent.deliver>>"),
    ("emit_java_refusals/validated_emission_operation.rvl",
     "<<UNSUPPORTED-VALIDATED:Model.complete>>"),
    ("emit_java_refusals/required_stream_coeffect.rvl",
     "<<UNSUPPORTED-REQUIRED-STREAM:Fulfiller.feed>>"),
]


@pytest.mark.parametrize("rel,marker", JAVA_NAMED_REFUSALS,
                         ids=[r.split("/")[1] for r, _ in JAVA_NAMED_REFUSALS])
def test_a_java_reference_refusal_is_named_by_the_port(emitted, reference, rel, marker):
    ir = compile_files([str(ROOT / "tests" / "fixtures" / rel)])
    with pytest.raises(reference.EmitError):
        reference.emit(ir)
    assert marker in emitted["emit_java_src"](ir)


# issue #2492: an extern with no `@java` body is refused by the reference from
# inside `_emit_v3_externs`, which no byte-agreement document reaches: every
# extern in the CORPUS carries an `@java` body, for the reference does not emit
# one without it. The port emits no extern block at all, so the two halves used
# to disagree silently. The port names the extern instead, in the spelling the
# go, rust and ts ports already use for their own missing body.
JAVA_NO_BODY_REFUSALS = [
    ("emit_java_refusals/extern_no_java_body.rvl", "run_tool"),
]


@pytest.mark.parametrize("rel, extern", JAVA_NO_BODY_REFUSALS,
                         ids=[rel.split("/")[1] for rel, _ in JAVA_NO_BODY_REFUSALS])
def test_an_extern_with_no_java_body_is_a_named_refusal(emitted, reference, rel, extern):
    ir = compile_files([str(ROOT / "tests" / "fixtures" / rel)])
    with pytest.raises(reference.EmitError,
                       match=f"extern `{extern}` has no @java body"):
        reference.emit(ir)
    assert f"<<UNSUPPORTED-EXTERN-NO-JAVA-BODY:{extern}>>" in emitted["emit_java_src"](ir)


# every extern carrying a body emits no marker at all, so the marker is not a
# blanket claim the port makes about documents the reference accepts.
def test_an_extern_with_a_java_body_emits_no_refusal(emitted, reference, tmp_path):
    path = tmp_path / "with_java_body.rvl"
    path.write_text((
        (ROOT / "tests" / "fixtures" / "emit_java_refusals"
         / "extern_no_java_body.rvl").read_text(encoding="utf-8").replace(
            " = @py { return name } = @ts { return name; } = @rs { name } = @go { return name }",
            " = @py { return name } = @java { return name; }")
    ))
    ir = compile_files([str(path)])
    reference.emit(ir)
    assert "<<UNSUPPORTED-EXTERN-NO-JAVA-BODY" not in emitted["emit_java_src"](ir)


# The reference refuses a deferred emission before it looks at host keys, so a
# document with both must surface the DEFERRED marker first on the port.
NAMESPACED_AND_DEFERRED = """extern emission deferred fn deliver(sink: Str, msg: Str)
  = @py { return } = @ts { return; } = @rs { () } = @go { return } = @java { return; }
service Ops { emission fn enqueue(sink: Str, msg: Str) }
component Agent provides acme::ops: Ops {
  provide acme::ops {
    fn enqueue(sink, msg) { emit deliver(sink, msg) }
  }
}
"""


def test_a_deferred_call_is_named_before_a_namespaced_key(
        emitted, reference, tmp_path):
    path = tmp_path / "ns_and_deferred.rvl"
    path.write_text(NAMESPACED_AND_DEFERRED)
    ir = compile_files([str(path)])
    with pytest.raises(reference.EmitError, match="deferred"):
        reference.emit(ir)
    got = emitted["emit_java_src"](ir)
    markers = re.findall(r"<<UNSUPPORTED-[A-Z-]+:", got)
    assert "<<UNSUPPORTED-NAMESPACED-KEY:" in markers, markers
    assert markers[0] == "<<UNSUPPORTED-DEFERRED-EMISSION:", markers


# Issue #2458: `_check_host_keys` is the FIRST statement of the reference
# `emit`, ahead of the holes, deferred, validated and fault-test refusals, so a
# document that trips a host-key collision AND a deferred call must name the
# collision first on the port. `acme__ops` is exactly what `acme::ops` mangles
# to (`_host_key`), so the two wiring keys land on one Java identifier.
COLLIDING_KEYS_AND_DEFERRED = """extern emission deferred fn deliver(sink: Str, msg: Str)
  = @py { return } = @ts { return; } = @rs { () } = @go { return } = @java { return; }
service Ops { emission fn enqueue(sink: Str, msg: Str) }
component Agent provides acme::ops: Ops {
  provide acme::ops {
    fn enqueue(sink, msg) { emit deliver(sink, msg) }
  }
}
component Flat provides acme__ops: Ops {
  provide acme__ops {
    fn enqueue(sink, msg) { emit deliver(sink, msg) }
  }
}
"""


def test_a_host_key_collision_is_named_before_a_deferred_call(
        emitted, reference, tmp_path):
    path = tmp_path / "host_key_collision.rvl"
    path.write_text(COLLIDING_KEYS_AND_DEFERRED)
    ir = compile_files([str(path)])
    with pytest.raises(reference.EmitError, match="both lower to the"):
        reference.emit(ir)
    got = emitted["emit_java_src"](ir)
    markers = re.findall(r"<<UNSUPPORTED-[A-Z-]+:", got)
    assert "<<UNSUPPORTED-DEFERRED-EMISSION:" in markers, markers
    assert markers[0] == "<<UNSUPPORTED-HOST-KEY-COLLISION:", markers


# issue #2408: the reference refuses a deferred call and a `validated` crossing
# before it looks at the fault tests, so a document that trips both must
# surface that refusal's marker first on the port, not the fault-test marker.
JAVA_REFUSED_BEFORE_A_FAULT_TEST = [
    ("emit_java_refusals/deferred_emission_call.rvl", "deferred",
     "<<UNSUPPORTED-DEFERRED-EMISSION:"),
    ("emit_java_refusals/validated_emission_operation.rvl",
     "needs the response-validation", "<<UNSUPPORTED-VALIDATED:"),
]


@pytest.mark.parametrize("rel, refusal, first",
                         JAVA_REFUSED_BEFORE_A_FAULT_TEST,
                         ids=[r.split("/")[1] for r, _, _ in JAVA_REFUSED_BEFORE_A_FAULT_TEST])
def test_a_refusal_before_a_fault_test_keeps_the_reference_order(
        emitted, reference, tmp_path, rel, refusal, first):
    src = (ROOT / "tests" / "fixtures" / rel).read_text(encoding="utf-8")
    path = tmp_path / "with_fault_test.rvl"
    path.write_text(src + "\n" + FAULT_TEST_SRC)
    ir = compile_files([str(path)])
    with pytest.raises(reference.EmitError, match=refusal):
        reference.emit(ir)
    markers = re.findall(r"<<UNSUPPORTED-[A-Z-]+:", emitted["emit_java_src"](ir))
    assert "<<UNSUPPORTED-FAULT-TEST:" in markers, markers
    assert markers[0] == first, markers


# issue #2492: the reference emits the extern block while it builds the class
# body, which it reaches BEFORE it renders components, and it renders
# components before it refuses a required `Stream[T]` coeffect. So a document
# carrying both a bodyless extern and a required stream must name the extern
# first on the port. Read from the reference rather than assumed: emit.py
# reaches _emit_v3_externs while extending the class body, and
# _refuse_required_stream fires from component rendering.
NO_JAVA_BODY_AND_REQUIRED_STREAM = (ROOT / "tests" / "fixtures"
                                    / "emit_java_refusals"
                                    / "extern_no_java_body.rvl").read_text(encoding="utf-8") + """

event Tap(key: tap_id) { tap_id: Str }
service Sink { emission fn deliver(id: Str) }

component Relay requires feed: Stream[Tap], sink: Sink {
  on Tap as t { emit sink.deliver(t.tap_id) }
}
"""


def test_a_bodyless_extern_is_named_before_a_required_stream(
        emitted, reference, tmp_path):
    path = tmp_path / "no_body_and_stream.rvl"
    path.write_text(NO_JAVA_BODY_AND_REQUIRED_STREAM)
    ir = compile_files([str(path)])
    markers = re.findall(r"<<UNSUPPORTED-[A-Z-]+:", emitted["emit_java_src"](ir))
    assert "<<UNSUPPORTED-EXTERN-NO-JAVA-BODY:" in markers, markers
    assert "<<UNSUPPORTED-REQUIRED-STREAM:" in markers, markers
    assert (markers.index("<<UNSUPPORTED-EXTERN-NO-JAVA-BODY:")
            < markers.index("<<UNSUPPORTED-REQUIRED-STREAM:")), markers
