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

Deliberately OUT (excluded from the corpus, deferred to Java Path B slice 4+):
  * the MODERN component path (``_emit_component_modern``) — isolate/intercept,
    effectful method bodies, and ``if``/``fail``/``await``/``return``/``setup``
    body steps (the ``Context.EffectScope fx`` provider shape); component
    ``config`` (the parameterised ``<Comp>Plugin`` ctor + ``_config_default_lit``);
    required-service routing (``req``/the ``ctx.get`` locals + provider fields);
    ``let-effect``/``effect``/``emit`` teardown steps; async/spawn/realms; and the
    ``await``->AsyncPlugin / ``fail``->CordisException import widening. A component
    outside the simple predicate surfaces a loud ``<<DEFER-component-nonsimple>>``;
  * the HOST Map/generics surface (``_emit_host_stubs``' ``HashMap<String,V>`` with
    ``_map_value_expr_type`` per-site inference — component territory; the plain
    ``maplit`` and ``Map[K,V]`` type lowering ARE covered);
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
                         # classes, `return <name>`, the empty-void-body throw trap,
                         # and the scalar param/return surface (long/boolean/String/void)
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
