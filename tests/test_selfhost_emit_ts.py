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

Slice 2 (item 204) extends the covered subset to the component-dialect tail of
``_emit_v3``, byte-identical: service interfaces (scalar signatures), the
``_context_augmentation`` committed-view block, config interfaces (required +
defaulted fields), and each component's plugin object — ``_component`` (inject/
provide wiring, ``apply``/``applyConfigDefaults``, the ``ctx.effect(function* …)``
body), ``_component_step`` (let-effect/effect/emit+compensate/provide/if/fail),
and ``_provide_impl``/``_method_body`` (let/assign/return/effect/emit), reusing
the single ``_expr`` (the component kinds ``req``/``config``/``name`` and the
component-shaped ``call``).

Slice 4 (item 219) adds ASYNC COLORING across the component tail, byte-identical:
async service operations (``Promise<T>`` signatures), ``async`` provide methods,
the item-141 await-seed on a req-keyed async-op call (direct + nested-in-ternary),
and the async-generator activation body (``ctx.effect(async function* …)``) with
the ``await`` step's iteration boundary.

Slice 5 (item 226) adds the MODULE-FN async path, byte-identical: async externs
(``export async function …: Promise<T>`` carrying the verbatim ``@ts`` body) and
phase-2 async-colored module ``fn``s (same signature form), with a ``var``-callee
call naming an async callable (``async_names``) or an async-value parameter
(``async_locals``, the item-92 ``(…) -> Async[T]`` slot) awaited; the async
``match`` shape (both the Opt IIFE and the tagged switch, arm-arrows and inner
calls awaited); and the async ARROW (``async (…) => …``). The ``ACx`` async-state
of slice 4 now threads to the module-fn level (and through ``v3_stmt``).

Slice 6 (item 234) adds SPAWN/INSTANCES + REALM PLACEMENTS, byte-identical: the
``spawn`` expr kind (``spawn(ctx, Worker, {config}, [realms])`` with the
``_uses_spawn`` runtime-import gate adding ``spawn``), the ``instance-get`` expr
kind (``w.counter`` -> ``w.get("counter")``), the ``isolate`` placement
(``isolate: {…},`` after the apply method), and the ``intercept`` metadata (the
dict-form ``inject: {key: metadata},`` replacing the array form). All four
fixtures are on the v3 emit path (the isolate/intercept ones carry a trivial
top-level 2.0 ``fn`` to keep them off the deferred v1/v2 path).

Slice 7 (item 240) pins the v1/v2 DISPATCH path (``emit()`` -> ``_emit_v1``),
byte-identical. A component-only document with no v3 feature (no declared
type/fn/extern/test, no ``if``/``fail``/setup-effect body step, no
builtin/adt/spawn/async-op) lowers to ir_version 1; a component carrying ONLY a
realm placement (``isolate``) or intercept metadata lowers to ir_version 2 —
item 234 kept its realm fixtures off this path by adding a trivial top-level 2.0
``fn`` (forcing v3), and this slice removes that crutch. The self-hosted
``emit_ts_src`` is version-agnostic: it assembles the same shape ``_emit_v1`` does
for a component-only doc (the header, ``_revl_helpers``, service interfaces, the
``_context_augmentation`` block, and each ``_component`` plugin object), so its
output is byte-identical to the reference's v1/v2 emission with NO emitter change
— the item-234 concern ("compiles to irv2, which the port doesn't mirror") is
closed by verification. ``components_await.rvl`` (already v1 in the corpus) covers
the async-generator body on this path; the three new fixtures add the config /
saga / provide-method v1 body and the isolate/intercept-only v2 dispatch.

Deliberately OUT (deferred to a follow-on slice): the component-dialect async
call surface — an async callable reached through the ``fn`` expr kind or from an
async PROVIDE method (its ``async_names`` are threaded empty, byte-safe because no
covered component body names one); the component-body ``timer`` step, VARIANT
type declarations (so an async ``match`` is exercised over Opt/built-in
``Result`` only, never a declared variant), in-file
``test``/``fault_test``/``lifecycle test`` emission, ROUTED requires
(item 167 — a routed require, ``requires <k> in realms(...) strategy(...)``, also
lowers to ir_version 2 but additionally needs the ``_TS_ROUTER_SRC`` runtime
literal, the ``realmLabel`` runtime import, the ``inject_keys = requires − routes``
gate, the per-key ``revlRouter`` proxy, and the routed-``req`` read; the router
literal embeds backtick templates and ``${…}`` the revl lexer reserves, so it is
kept out to stay byte-VERIFIED — a routed v2 doc must NOT enter this corpus until
that lands), the canonical ABI, and ``assert``/``let_pattern`` statements — all
kept out so the slice stays byte-VERIFIED rather than byte-guessed.
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

# item 243 Slice 2b (docs/design/teardown-contract.md): the reference emitter's
# `emit ... compensate ...` lowering registers a Phase-2 compensation entry
# through `Frame` (two-phase abort) instead of a bare `yield () => <compensate>`.
# `selfhost/emit_ts.rvl` was ported to that lowering (item 323, py's item 317
# analog), so the three saga fixtures below byte-match the reference like every
# other fixture — no `xfail` remains.
CORPUS = [
    "arith.rvl",       # bounded int/int32, division/modulo, comparisons, unary
    "bitwise.rvl",  # Int32 bitwise & | ^ << >> and unary ~ (item 366, item 391 self-host port)
    "strings.rvl",     # the stdlib string builtins and `${…}` interpolation
    "control.rvl",     # while/for/if, match (Some/None/wildcard), sync arrow
    "records.rvl",     # record literal, functional record update, list literal
    "optionals.rvl",   # optional-call chaining (opt receiver)
    "mixed.rvl",       # a cross-section of the above in three functions
    # slice 2 (item 204) — components/services, byte-exact:
    "services_methods.rvl",       # provide methods (params, ternary, builtin), context aug
    "services_body.rvl",          # let-effect (bound), if/fail guard, emit/compensate saga
                                   # (compensation -> Frame two-phase teardown, item 323)
    "services_config.rvl",        # config interface (required + defaulted), applyConfigDefaults
    "services_method_block.rvl",  # block-form provide method: let/return, req-as-ctx in method
    "components_mixed.rvl",       # a pure fn alongside a provider (independent match counters)
    # slice 3 (item 208) — composite signatures + the host/format/fn/adt kinds:
    "services_composite.rvl",         # List/Opt/Map/Result/fn-type + declared-record `List[Msg]`
    "services_composite_provide.rvl", # composite provide-method params (Row[], Map) via `_ts_type`
    "component_exprs.rvl",            # host (`host.Job.run`), format (`` `…${}` ``), fn, adt
                                       # (compensation -> Frame two-phase teardown, item 323)
    # slice 4 (item 219) — async coloring across the component tail, byte-exact:
    "services_async.rvl",       # async op `Promise<T>` sigs, `async` methods, the item-141
                                # await-seed (direct `fetch` + nested ternary arm `pick`)
    "components_await.rvl",     # activation-body `await` -> `async function*` + iteration boundary
    # slice 5 (item 226) — the MODULE-FN async path, byte-exact:
    "async_module_extern.rvl",  # async extern (`export async function …: Promise<T>` + verbatim
                                # @ts body) + a colored fn awaiting it (`var`-callee, let + return)
    "async_module_local.rvl",   # item-92 async-value local: a `(Str) -> Async[Str]` param, awaited
    "async_module_match.rvl",   # async `match` (Opt form): async IIFE + awaited binding arm-arrow
    "async_module_switch.rvl",  # async `match` (tagged switch over built-in `Result`): case-bind await
    "async_module_arrow.rvl",   # async ARROW (`async (y) => …`) + awaited call to a colored fn
    # item 435(b): an arrow whose body IS the un-awaited emission Promise, so the
    # `async` is dropped: `((msgs: any) => (ctx.model.complete(msgs)))`. No other
    # fixture puts an emission call in an arrow body, so without this one the
    # 435(b) port would be invisible to the oracle (item 429's trap).
    "async_arrow_emission.rvl",
    # slice 6 (item 234) — spawn/instances + realm placements, byte-exact:
    "spawn.rvl",            # a supervisor spawning a Worker template: the `spawn` expr node
                            # (`spawn(ctx, Worker, {cfg}, [realms])`) + the `_uses_spawn` import gate
    "instance_get.rvl",     # reading a provision off a handle: `w.counter` -> `w.get("counter")`
    "realm_isolate.rvl",    # `isolate db in realm("primary")` -> `isolate: {…},` after apply
    "realm_intercept.rvl",  # `intercept db with {…}` -> the dict-form `inject: {"db": {…}},`
    # slice 7 (item 240) — the v1/v2 DISPATCH path (`emit()` -> `_emit_v1`), byte-exact:
    "v1_component_body.rvl",  # a component-only doc with no v3 feature lowers to ir_version 1
                              # (config, effect/undo, emit/compensate saga, provide method + ternary)
                              # (compensation -> Frame two-phase teardown, item 323)
    "v2_isolate_only.rvl",    # isolate ONLY (no trivial v3 `fn`) -> ir_version 2 (closes item 234's flag)
    "v2_intercept_only.rvl",  # intercept ONLY (no trivial v3 `fn`) -> ir_version 2, dict-form inject
    # item 383 / 391 (self-host port) — `.map`/`.filter`/`.reduce` desugar to the
    # `list_*` free calls; the ts tier lowers the function-value params + arrows
    "transforms.rvl",
    # item 421 F6 (self-host port per item 429(d)) — the three sites a declared
    # `Secret[T]` survives to: the `host.secretResult` extern wrapper (origin),
    # `host.markSecret` at the head of a receiver provide method, and the
    # `, secret: true` config-field stamp. NO other fixture declares a `Secret[T]`
    # anywhere, so before this one the whole redaction surface was invisible to
    # the oracle and `selfhost/emit_ts.rvl` emitted none of it while the suite was
    # green — item 429's trap, and a live security divergence.
    "secrets.rvl",
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
    got = emitted["emit_ts_src"](ir)
    assert got == want, (
        f"self-hosted TS emitter diverged from the reference on {rel}\n"
        f"--- lengths ref={len(want)} got={len(got)} ---"
    )


def test_selfhosted_ts_emitter_output_header_and_helpers(emitted):
    """Beyond byte-identity, pin the scaffold shape the slice is responsible for:
    the generated-header comments, the runtime import, and the gated helpers a
    bounded-int / string document pulls in."""
    ir = compile_files([str(CORPUS_DIR / "arith.rvl")])
    src = emitted["emit_ts_src"](ir)
    assert src.startswith(
        "// Generated by revl backends/typescript/emit.py — do not edit.\n")
    assert "import { host } from '../runtime.ts'" in src
    assert "function revlI64(v: bigint): bigint {" in src
    assert "function revlI32(v: number): number {" in src
    assert "export function i64ops(a: bigint, b: bigint): bigint {" in src
    # a document with no string method must NOT carry the code-point helpers
    assert "function revlLen(" not in src
    strings = emitted["emit_ts_src"](compile_files([str(CORPUS_DIR / "strings.rvl")]))
    assert "function revlLen(" in strings


def test_selfhosted_ts_emitter_in_file_tests_pass(emitted):
    """The .rvl file's own `test` blocks run under the python backend."""
    tests = emitted.get("REVL_TESTS")
    assert tests and len(tests) >= 4, "expected the file's test blocks in REVL_TESTS"
    for entry in tests:
        fn = entry[-1] if isinstance(entry, tuple) else entry
        fn()
