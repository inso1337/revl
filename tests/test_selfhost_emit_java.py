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
  * slice 1 — the module scaffold (banner comments, ``package revl;``, the five
    unconditional ``import io.cordis4j.core.*;`` lines, the
    ``public final class Components { private Components() {} }`` wrapper);
    ``_emit_v3_functions`` (each fn a ``public static`` method, ``_java_v3_type``
    for scalar/``List``/``Opt``/``Map``/``Result`` types, ``_fn_name`` renaming,
    ``// (empty body)``); ``_v3_stmt`` (let/assign, return, if/while/for,
    bare-expr, assert); and the base ``_expr`` algebra (lit, name, var incl
    ``None`` -> ``Optional.empty()``, bin incl ``??``/``==``/exact-arith/``/``/
    comparisons/``&&``, un incl ``Math.negateExact``, the 2.0 call and the
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
    stays a loud ``<<DEFER-component-nonsimple>>``.

  * slice 4 (item 235) — REALM placements on the modern path: an ``isolate <key>
    in realm("..")`` opens the ``apply()`` body with ``ctx = ctx.isolate(<Svc>.class,
    "..")`` (the service resolved through ``provides`` else ``requires``); an
    ``intercept <key> with <meta>`` emits ``ctx.intercept(ServiceKey.of(<Svc>.class),
    <meta>)`` (service through ``requires``) with ``_metadata_lit`` rendering nested
    map/list/int/float/bool/string metadata byte-for-byte. Both make the component
    ``needs_modern`` and are admitted so long as the body/method shapes stay inside
    the slice-3 subset; async/await/spawn placements are NOT (see below).

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
    ``<<DEFER-host-stub:Pool>>``); ``setup`` blocks on effect steps, method-body
    ``let``/``assign``/``await``/arrow bindings, and body-level ``while``/``for``/
    ``let_pattern`` (a component outside the supported subset surfaces a loud
    ``<<DEFER-component-nonsimple>>``);
  * the stdlib surface (every ``builtin``/``len`` node and
    ``_emit_stdlib_helpers``); the built-in Result surface (``Ok``/``Err`` ctor
    and ``match``, ``_emit_result_type``, ``_emit_checked_div_helpers``);
  * functional record-update ``{r | f = e}`` (the Java reference itself RAISES on
    ``record_update`` — a structural exclusion);
  * async coloring / spawn / instances / externs / in-file ``test`` /
    lifecycle-test emission; the canonical Float->Str ``revlFtoa`` (float
    interpolation excluded); the ``_reject_fn_type`` refusal; local ``let``-bound
    arrows and their ``_inline_arrow`` beta-reduction; and ``let_pattern`` (its
    temp name ``__revl_destructure_{id(node)}`` is a host identity no port can
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
    # slice 1 (item 199) — functions-only base surface
    "arith.rvl",     # bounded int/int32, /, %, comparisons, ==/!=, unary, ??
    "bitwise.rvl",  # Int32 bitwise & | ^ << >> and unary ~ (item 366, item 391 self-host port)
    "control.rvl",   # let/var/assign, if/else, while, for, bare-expr, assert
    "calls.rvl",     # free-function calls
    "strings.rvl",   # string `+`, `${..}` interpolation, literals
    "lists.rvl",     # list literal, index, nested list types
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
