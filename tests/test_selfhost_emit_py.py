"""The self-hosted cordis-py EMITTER (selfhost/emit_py.rvl, roadmap items
146/174/185 — Path B slices 1+2): compiled by revl, emitted through the python
backend, executed, and cross-checked BYTE-FOR-BYTE against the reference emitter
(backends/python/emit.py's ``emit``) over a corpus of interchange-IR documents.

This is the first proof that revl can emit ITSELF. It has the exact shape of
tests/test_selfhost_{lexer,parser,checker,lower}.py: two independent
implementations of one lowering — the reference backend and its revl port — are
forced to agree. Here the agreement is the strongest kind an emitter can be held
to: the emitted Python source must be identical to the last byte. The reference
is ground truth; any divergence is a defect in the slice.

Slice 2 (item 185) also rewrote the IR-navigation bridge: where slice 1 read the
IR through a bespoke ``@py`` accessor set (``g``/``gs``/``alist``/``at``/…),
navigation is now PURE revl through stdlib/value.rvl's ``value_*`` (item 180) —
a refactor of HOW the IR is read, proven by the function corpus staying
byte-identical. Only host formatting stays ``@py`` (``py_repr``/``newline``/
``mangle``/``snake``/``pascal``), plus one flagged gap: value.rvl ships no
record-key enumerator, so ``record_keys`` is bridged locally (see the file).

Covered subset (what emits byte-identical):
  * the FUNCTION-ONLY document — module scaffold, gated arithmetic preludes
    (i64/i32 traps, IEEE ``_revl_div``), and ``_emit_functions`` ->
    ``_fn_stmt`` -> ``_expr`` for the base surface: let/assign, return,
    if/while/for, expr, assert; lit, var, bin (incl ``??``, bounded ``+ - *``,
    ``/``, truncated ``%``), un, call, field, index, ternary-if, record, list,
    len, stdlib builtins, maplit, sync arrow, match, record-update, string
    interpolation, opt field/call;
  * COMPONENTS/SERVICES (item 185, the ``_ComponentEmitter``) — the populated
    SERVICES table; the conditional ``from runtime import`` line; per component
    the ``_<snake>_apply`` closure + plugin dict with the ``inject`` list; the
    effect accumulator (``effect``/``let-effect``/``fail``/``if``-guard/``emit``
    with saga ``compensate``); timers (``every``/``after`` ->
    ``schedule_*``/``.cancel()``); ``provide`` classes with sync methods; and
    the component-body expression dispatcher ``cexpr`` (``req``/``name``/method
    ``call`` and the un-specialized component arithmetic);
  * the MODULE DECLARATION surface (item 192, slice 3): type declarations
    (``_emit_types`` — record ``@dataclass`` + sealed-variant classes, the
    forward-reference annotation quoting, and the ``_py_type`` surface->python
    map incl function types -> ``Callable``); the built-in Result (``Ok``/``Err``)
    classes with user-case shadowing; the canonical Float->Str (``_revl_ftoa``)
    helper gated by a float ``${…}`` interpolation; and host roots
    (``Map``/``Pool``/``Job``) in fn/test bodies pulled into the sorted
    ``from runtime import``.

Deliberately OUT (excluded from the corpus, deferred to Path B slice 4+):
externs (``_emit_externs`` — a faithful ``textwrap.dedent``/``splitlines`` of the
verbatim ``@py`` body, a host-string dependency), in-file ``test``/``fault_test``
and ``lifecycle test`` emission, component ``config`` (``ConfigSchema``), async
coloring (async methods / ``await`` bodies / the await-seed), method-body
``effect``/``let-effect``, realm placements (``isolate``/``intercept``/``routes``),
spawn/instances, and the canonical ABI. ``let_pattern`` (destructuring) is a
permanent exclusion for a byte oracle: the reference names its temporary from
``id(node)``, which a second implementation cannot reproduce.
"""

import importlib.util
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_files  # noqa: E402

CORPUS_DIR = ROOT / "tests" / "fixtures" / "emit_py_corpus"
CORPUS = [
    # function-only documents (slice 1); still byte-exact after the value_*
    # navigation rewrite (slice 2, item 185) — the refactor's own proof
    "arith.rvl",       # bounded int/int32, division/modulo, comparisons, unary
    "strings.rvl",     # the stdlib string builtins and `${…}` interpolation
    "control.rvl",     # while/for/if, match (Some/None/wildcard), sync arrow
    "records.rvl",     # record literal, functional record update, list literal
    "optionals.rvl",   # optional-call chaining (opt receiver)
    "mixed.rvl",       # a cross-section of the above in three functions
    # component/service documents (slice 2, item 185)
    "services_basic.rvl",    # SERVICES table, req/provide, effect accumulator, a provide method
    "services_timers.rvl",   # every/after timers -> schedule_* import + cancel inverses
    "services_methods.rvl",  # provide methods: params, un-specialized bin, builtin, ternary
    "services_body.rvl",     # let-effect, if-guard + fail, saga emit ... compensate
    # module-level declaration surface (slice 3, item 192)
    "types.rvl",       # `_emit_types`: record @dataclass + variant classes, forward-ref quoting, `_py_type` (incl fn types)
    "result.rvl",      # built-in Result (Ok/Err) classes, gated by a match on Ok/Err
    "floats.rvl",      # `_revl_ftoa` canonical Float->Str, gated by a float `${…}` interpolation
    "hostroots.rvl",   # host roots (Map/Pool/Job) in a fn body -> the sorted `from runtime import`
]


def _load_reference_emit():
    """The reference emitter, loaded by path so we compare against the exact
    file this slice mirrors (not whatever `revl` re-exports)."""
    spec = importlib.util.spec_from_file_location(
        "pyemit_reference", ROOT / "backends" / "python" / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _exec_emitted() -> dict:
    """Compile selfhost/emit_py.rvl, emit python, exec it. The file's component
    wrapper makes the emitted module `from runtime import …`; the pure emitter
    functions under test never touch it, so a lazy stub suffices (as in the
    other self-host stage tests)."""
    ir = compile_files([str(ROOT / "selfhost" / "emit_py.rvl")])
    assert ir["ir_version"] == 3
    spec = importlib.util.spec_from_file_location(
        "pyemit_selfhost_backend", ROOT / "backends" / "python" / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    stub = types.ModuleType("runtime")
    stub.__getattr__ = lambda name: (lambda *a, **k: None)  # PEP 562
    had_runtime = "runtime" in sys.modules
    previous = sys.modules.get("runtime")
    sys.modules["runtime"] = stub
    try:
        namespace = {}
        exec(compile(module.emit(ir), "selfhost_emit_py.py", "exec"), namespace)
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
    """The self-hosted emitter's Python output == the reference's, byte-for-byte,
    for every interchange-IR document in the covered subset."""
    ir = compile_files([str(CORPUS_DIR / rel)])
    want = reference.emit(ir)
    got = emitted["emit_src"](ir)
    assert got == want, (
        f"self-hosted emitter diverged from the reference on {rel}\n"
        f"--- lengths ref={len(want)} got={len(got)} ---"
    )


def test_selfhosted_emitter_output_is_executable_python(emitted, reference):
    """A byte-identical output is trivially valid, but pin it: the emitted
    module for a corpus program compiles and its functions run."""
    ir = compile_files([str(CORPUS_DIR / "arith.rvl")])
    src = emitted["emit_src"](ir)
    ns: dict = {}
    exec(compile(src, "arith_emitted.py", "exec"), ns)
    assert ns["i64ops"](3, 4) == 3 + 4 - 3 * 4
    assert ns["divmod"](7, 2) == (
        7 // 2 + 7 // 2 + 7 // 2 + 7 % 2 + 7 % 2  # all same sign here
    )


def test_selfhosted_emitter_lowers_components_and_services(emitted):
    """Beyond byte-identity: a component/service document actually drives the
    slice-2 path — the emitted module populates SERVICES and COMPONENTS and its
    plugin dict / apply closure exec cleanly (with the runtime stubbed)."""
    ir = compile_files([str(CORPUS_DIR / "services_basic.rvl")])
    src = emitted["emit_src"](ir)
    assert "def _backing_apply(_revl_ctx, _revl_config):" in src
    assert "yield _revl_ctx.provide('health')" in src
    stub = types.ModuleType("runtime")
    stub.__getattr__ = lambda name: (lambda *a, **k: None)  # PEP 562
    had = "runtime" in sys.modules
    prev = sys.modules.get("runtime")
    sys.modules["runtime"] = stub
    try:
        ns: dict = {}
        exec(compile(src, "services_basic_emitted.py", "exec"), ns)
    finally:
        if had:
            sys.modules["runtime"] = prev
        else:
            del sys.modules["runtime"]
    assert set(ns["SERVICES"]) == {"Store", "Health"}
    assert set(ns["COMPONENTS"]) == {"Backing", "Reader"}
    assert ns["Backing"]["inject"] == ["store"]
    assert ns["Reader"]["inject"] == ["store"]


def test_selfhosted_emitter_in_file_tests_pass(emitted):
    """The .rvl file's own `test` blocks run under the python backend."""
    tests = emitted.get("REVL_TESTS")
    assert tests and len(tests) >= 4, "expected the file's test blocks in REVL_TESTS"
    for entry in tests:
        fn = entry[-1] if isinstance(entry, tuple) else entry
        fn()
