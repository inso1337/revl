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

Covered subset (slice 3, item 207) — the components/services + bridge surface:
  * ``_emit_service_traits`` — each ``service`` as a ``pub trait S: Send + Sync``
    with one ``&self`` method per op (scalar/generic param + return lowering);
  * ``_emit_component`` (the SIMPLE provider path only — no isolate/intercept and
    no effectful methods) — the empty provider ``struct``, its trait ``impl`` with
    a pure ``_pure_method_statements`` body, and the ``plugin_sync`` factory whose
    closure provisions each key via the ``provide`` body step (``Inject::none()``,
    no config application);
  * ``_emit_bridge`` — the whole erasure block a lone ``service`` forces: the
    ``_revl_rpc`` Unix-socket JSON-RPC preamble, a consumer ``S`Proxy`` + a
    provider ``_revl_dispatch_s`` per service (the SCALAR marshalling —
    ``String``/``i64``/``f64``/``bool``, the scalar-``Option`` return table, and
    ``()`` — via ``_bridge_arg_ser``/``_arg_extract``/``_ret_deser``/``_ret_ser``),
    and the fixed key/service/plugin/isolate/load routing tables over the provided
    keys.

Covered subset (slice 4, item 218) — the effectful/config/req component surface
(``_emit_component_new`` + the ``_emit_component_auto`` dispatch):
  * required services — the ``Inject::new([..])`` gate, the ``ctx.require::<Box<
    dyn S>>("key")?`` bindings, and the per-req ``Arc<Box<dyn S>>`` provider-
    struct capture read off ``self`` (``requires.rvl``);
  * method-body effects — a block-body ``emit`` STEP and an ``effect``/``undo``
    pair through ``self.ctx.effect(label, move || { .. })``, the
    ``ctx: Arc<cordis::Context>`` field, ``_method_undo_clones``, the item-101/114
    let-inference (a required-service call's declared return types the local so a
    later by-value use clones), and the ``drop`` -> ``drop_`` method rename
    (``effect_emit.rvl`` / ``effect_undo.rvl``);
  * component ``config`` — the ``<Comp>Config`` ``#[derive(Clone)]`` struct +
    ``Default``, the local ``let config = <C>Config { .. ..Default::default() }``
    application, the captured-config provide struct + the up-front
    ``__revl_provide_config`` clone, ``config.<field>`` -> ``self.config.<field>
    .clone()`` in a method, and the ``_revl_load`` typed-config construction from
    the JSON placement spec (``config.rvl`` / ``config_effect.rvl``);
  * the component-dialect expression kinds ``req``/``config``/``fn`` and the
    ``target``/``method`` call form, threaded through the shared renderer via the
    on-``ctx`` capture-rename map.

Deliberately OUT (excluded from the corpus, deferred to Rust Path B slice 5+):
the remaining ``_emit_component_new`` surfaces — timers / routers (``routes``),
``await``/``spawn``, ``isolate``/``intercept`` (and the realm placements
``_revl_realm``/``_revl_isolate_ctx`` non-empty arms), host ``let-effect`` binds
(``Pool``/``Map``/``Job``) and the item-114 host-Map undo-reclone, body-level
``effect``/``timer``/``fail``/``if`` steps, and the ``host``/``format``
component-dialect expression kinds; the host-object stubs (``Map``/``Pool``/
``Job``) and every preamble helper (timer/stdlib/float/realm/spawn); and the
non-scalar bridge marshalling (Result/serde/``Vec<Value>``/opaque-``Value``
params and returns); functional record-update ``{r | f = e}`` (the
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
    # slice 3 (item 207) — the components/services + bridge surface:
    "service.rvl",       # one `service` trait + a simple-path provider component,
                         #   forcing the full `_emit_bridge` erasure block
    "services_multi.rvl",# two services, one component providing both — the bridge's
                         #   i64/bool/void marshalling and multi-provision routing
    # slice 4 (item 218) — the effectful/config/req component surface
    # (`_emit_component_new` + `_emit_component_auto` dispatch):
    "requires.rvl",      # required services on the SIMPLE path — the `Inject::new`
                         #   gate, `ctx.require` bindings, and the per-req struct
                         #   capture read off `self` in a pure method
    "effect_emit.rvl",   # a block-body `emit` STEP -> `_emit_component_new`: the
                         #   `ctx: Arc<cordis::Context>` field, the fire-and-forget
                         #   `let _ = self.store.write(..);`, and the req-call let
                         #   type inference driving the by-value clone
    "effect_undo.rvl",   # the revertible `effect`/`undo` pair — `method_undo_clones`,
                         #   the labelled `self.ctx.effect(.., move || { .. })`
                         #   registration, and the `drop` -> `drop_` method rename
    "config.rvl",        # component `config` on the SIMPLE path — `<Comp>Config`
                         #   struct + `Default` + application, the captured-config
                         #   provide struct, and the `_revl_load` typed construction
    "config_effect.rvl", # config + effectful: config capture in an effect body
                         #   (`self.config.banner.clone()`) + `__revl_provide_config`
    # item 217 — the v1 front-door: a components-ONLY document lowers to
    # `ir_version 1` (not v3), so it exercises the versioned `_module_header`
    # (version-1 banner + the SHORT `#![allow(dead_code, unused_variables)]`);
    # the covered service/comp body is byte-identical to the v3 path.
    "v1_components.rvl",
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
    got = emitted["emit_rust_src"](ir)
    assert got == want, (
        f"self-hosted emitter diverged from the reference on {rel}\n"
        f"--- lengths ref={len(want)} got={len(got)} ---"
    )


def test_selfhosted_emitter_output_scaffold(emitted):
    """A byte-identical output is trivially valid Rust source; pin the scaffold
    and a representative body detail so a regression in the header or the
    checked-arithmetic lowering surfaces here, not only in the byte diff."""
    ir = compile_files([str(CORPUS_DIR / "arith.rvl")])
    src = emitted["emit_rust_src"](ir)
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
    rec = emitted["emit_rust_src"](compile_files([str(CORPUS_DIR / "records.rvl")]))
    assert "pub struct Point {" in rec
    assert "#[derive(Clone, Debug, PartialEq, serde::Serialize, serde::Deserialize)]" in rec
    assert "Named { label: s.clone(), at: p.clone() }" in rec
    var = emitted["emit_rust_src"](compile_files([str(CORPUS_DIR / "variants.rvl")]))
    assert '#[serde(tag = "$kind", content = "$value")]' in var
    assert "pub enum Tree {" in var
    assert "return Tree::Leaf;" in var
    assert "Tree::Node(v) => v," in var
    assert "_ => unreachable!()," in var


def test_selfhosted_emitter_component_bridge_scaffold(emitted):
    """Pin the components/services surface (slice 3, item 207): a ``service``
    lowers to a ``Send + Sync`` trait, the simple-path component to an empty
    provider struct + trait impl + ``plugin_sync`` factory, and the erasure
    surface a lone service forces — the ``_revl_rpc`` preamble, a ``S`Proxy``
    consumer, a ``_revl_dispatch_s`` provider, and the routing tables — so a
    regression in any of these surfaces here, not only in the byte diff."""
    src = emitted["emit_rust_src"](compile_files([str(CORPUS_DIR / "service.rvl")]))
    assert "pub trait Greeter: Send + Sync {" in src
    assert "    fn greet(&self, who: String) -> String;" in src
    assert "struct HelloGreeting {\n}" in src
    assert "impl Greeter for HelloGreeting {" in src
    assert "pub fn hello() -> cordis::PluginHandle {" in src
    assert 'let greeting_box: Box<dyn Greeter> = Box::new(HelloGreeting {  });' in src
    # the bridge erasure block (a lone service is enough to fire it)
    assert "// ---- interop bridge (generated; docs/interop-bridge.md) ----" in src
    assert "pub struct GreeterProxy { pub socket: String, pub key: String }" in src
    assert "fn _revl_dispatch_greeter(svc: &dyn Greeter, method: &str," in src
    assert '"greeting" => Some("Greeter"),' in src
    assert '"hello" => Some(hello()),' in src
    # no deferred-feature marker leaked into a covered-subset output
    assert "<<DEFER" not in src and "<<NONE>>" not in src


def test_selfhosted_emitter_effectful_component_scaffold(emitted):
    """Pin the effectful/config/req surface (slice 4, item 218): the
    `_emit_component_new` path (effect/undo, `emit`, the `ctx: Arc<Context>`
    capture, the required-service `Inject` gate) and the `config` surface
    (`<Comp>Config` struct/Default/application + the captured provide struct +
    the `_revl_load` typed construction) — so a regression in any of these
    surfaces here, not only in the byte diff."""
    # required services on the simple path
    req = emitted["emit_rust_src"](compile_files([str(CORPUS_DIR / "requires.rvl")]))
    assert 'cordis::Inject::new(["assistant", "compiler"]),' in req
    assert 'let assistant = ctx.require::<Box<dyn Model>>("assistant")?;' in req
    assert "struct EvolverEvolve {\n    assistant: Arc<Box<dyn Model>>," in req
    assert "self.compiler.propose(self.assistant.complete(goal.clone()))" in req
    # a block-body emit -> _emit_component_new (the ctx field + fire-and-forget)
    eff = emitted["emit_rust_src"](compile_files([str(CORPUS_DIR / "effect_emit.rvl")]))
    assert "    ctx: Arc<cordis::Context>,\n}" in eff
    assert "let v = self.store.read(k.clone());" in eff
    assert "let _ = self.store.write(k.clone().clone(), v.clone());" in eff
    assert "Box::new(SvcApi { store: store.clone(), ctx: Arc::new(ctx.clone()) })" in eff
    # the revertible effect/undo pair + the drop -> drop_ rename
    und = emitted["emit_rust_src"](compile_files([str(CORPUS_DIR / "effect_undo.rvl")]))
    assert "let res_undo = self.res.clone();" in und
    assert "let id_undo = id.clone();" in und
    assert "let _ = self.res.drop_(id.clone().clone());" in und
    assert ('let _ = self.ctx.effect("Worker.run.effect.1", '
            "move || { res_undo.grab(id_undo.clone()); Ok(()) });") in und
    # component config on the simple path (struct/Default/application/_revl_load)
    cfg = emitted["emit_rust_src"](compile_files([str(CORPUS_DIR / "config.rvl")]))
    assert "#[derive(Clone)]\nstruct HelloConfig {" in cfg
    assert "impl Default for HelloConfig {" in cfg
    assert 'prefix: String::from("hi, "),' in cfg
    assert "cordis::plugin_sync::<HelloConfig, _>(" in cfg
    assert "let __revl_provide_config = config.clone();" in cfg
    assert 'let _c = config.get("Hello").cloned().unwrap_or(serde_json::Value::Null);' in cfg
    assert ('prefix: _c.get("prefix").and_then(|v| v.as_str()).map(|s| s.to_string())'
            '.unwrap_or_else(|| String::from("hi, ")),') in cfg
    # config read inside an effectful body renders self.config.<field>.clone()
    cfe = emitted["emit_rust_src"](compile_files([str(CORPUS_DIR / "config_effect.rvl")]))
    assert "let full = format!(\"{}{}\", self.config.banner.clone(), prompt);" in cfe
    assert "    config: AssistantConfig,\n    ctx: Arc<cordis::Context>,\n}" in cfe
    # no deferred-feature marker leaked into any covered-subset output
    for src in (req, eff, und, cfg, cfe):
        assert "<<DEFER" not in src and "<<NONE>>" not in src


def test_selfhosted_emitter_in_file_tests_pass(emitted):
    """The .rvl file's own `test` blocks run under the python backend."""
    tests = emitted.get("REVL_TESTS")
    assert tests and len(tests) >= 3, "expected the file's test blocks in REVL_TESTS"
    for entry in tests:
        fn = entry[-1] if isinstance(entry, tuple) else entry
        fn()
