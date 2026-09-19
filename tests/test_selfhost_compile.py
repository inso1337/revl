"""THE CAPSTONE (roadmap item 230, extending 224): the integrated, FULLY-NATIVE
``revl_compile`` — selfhost/compile.rvl composing lower.rvl + emit_py.rvl +
emit_rust.rvl into ONE co-compiled revl artifact, proving revl compiles revl to a
target tier END TO END with NO REFERENCE anywhere in the chain, BYTE-FOR-BYTE
against the reference — for the module-FUNCTION + TYPE surface.

What changed from item 224's capstone (whose two SEAMs this closes):
  * SEAM 1 (the reference IR in the middle) is GONE for the function surface:
    ``compile_to`` now calls the native IR producer ``lower_to_ir`` (item 232),
    parses it with the native ``json_parse``, and hands the ``Any`` to the native
    emitter. The reference ``compile_source`` is no longer in the pipeline.
  * SEAM 2 (the stages could not co-compile) is GONE: ``compile.rvl`` ``use``s
    lower.rvl, emit_py.rvl AND emit_rust.rvl in ONE composition. Item 228 stopped a
    ``use``d module's private decls from leaking; item 230 gave the emitter
    entrypoints distinct public names (``emit_py_src`` / ``emit_rust_src``) so the
    merge no longer sees a duplicate ``emit_src``.

    source ──▶ compile.rvl ``compile_to``      ONE native artifact, no reference:
                                               admit_src (frontend gate) →
                                               lower_to_ir (native IR, item 232) →
                                               json_parse → emit_py_src /
                                               emit_rust_src (native emitter).

The proof — ``compile_to(source, tier)`` takes ONLY the raw source string and
returns the target bytes; the reference (``compile_files`` / ``reference_emit``) is
used SOLELY to compute the EXPECTED value, never to produce the native output. So a
byte-for-byte match is a proof that the whole lex→parse→check→lower→emit chain ran
in revl.

The boundary (roadmap item 262 closes the last seam):
  * FULLY NATIVE, byte-exact, module-function + type surface — the emitter-ready
    documents item 232 proved ``lower_to_ir`` byte-exact on (py corpus), and the
    pure-function/type/variant slice of the rust corpus the rust emitter covers.
  * FULLY NATIVE, byte-exact, COMPONENT + extern surface (item 262) — items 242 +
    241 made ``lower_to_ir`` COMPLETE for the typed component/method body (effects,
    sagas, timers, config) and the verbatim ``@backend`` extern bodies, and each
    tier's native emitter is proven byte-exact on its component corpus. Their
    composition compiles a component program end to end natively, byte-for-byte
    against the reference, for BOTH tiers, with NO reference in the chain. The
    typed component BODY is no longer a reference-only remainder.

Ground truth is the reference; on the covered surface any divergence is a defect in
a stage.
"""

import importlib.util
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_files, compile_source  # noqa: E402
from revl.errors import RevlError  # noqa: E402


# ---------------------------------------------------------------- harness

def _exec_selfhost(rvl_relpath: str) -> dict:
    """Compile a self-host stage (selfhost/<file>.rvl) with revl, emit python,
    exec it, and return its module namespace. Identical in shape to the harness
    every other tests/test_selfhost_*.py uses: the stage's component wrapper
    makes the emitted module ``from runtime import …``; the pure functions under
    test never touch it, so a lazy stub suffices."""
    ir = compile_files([str(ROOT / rvl_relpath)])
    assert ir["ir_version"] == 3
    spec = importlib.util.spec_from_file_location(
        "pyemit_" + rvl_relpath.replace("/", "_"),
        ROOT / "backends" / "python" / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    stub = types.ModuleType("runtime")
    stub.__getattr__ = lambda name: (lambda *a, **k: None)  # PEP 562
    had = "runtime" in sys.modules
    previous = sys.modules.get("runtime")
    sys.modules["runtime"] = stub
    try:
        namespace = {}
        exec(compile(module.emit(ir),
                     rvl_relpath.replace("/", "_") + ".py", "exec"), namespace)
    finally:
        if had:
            sys.modules["runtime"] = previous
        else:
            del sys.modules["runtime"]
    return namespace


TIER_SUBDIR = {"py": "python", "rust": "rust", "ts": "typescript",
               "go": "go", "java": "java", "wasm": "wasm"}

# The six tiers ``compile.rvl`` dispatches, in the order the item-146 table below
# reports them.
TIERS = ("py", "ts", "go", "java", "rust", "wasm")


def _load_reference_emit(tier: str):
    """The reference emitter for a tier, loaded by path — the exact file the
    self-host emitter mirrors, and the ground truth for ``compile to <tier>``.

    The wasm reference returns a ``{module_name: wat}`` dict rather than one
    source string; the covered surface is its ``functions`` module, which is what
    ``emit_wasm_src`` produces and therefore what ``compile_to(..., "wasm")``
    returns. Project it here so every tier is compared the same way."""
    spec = importlib.util.spec_from_file_location(
        "ref_emit_" + tier, ROOT / "backends" / TIER_SUBDIR[tier] / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if tier == "wasm":
        return lambda ir, _emit=module.emit: _emit(ir)["functions"]
    return module.emit


@pytest.fixture(scope="module")
def compile_rvl() -> dict:
    """selfhost/compile.rvl — the ONE co-compiled artifact holding the whole
    native pipeline (lower + emit_py + emit_rust `use`d together). That this even
    compiles and execs is the SEAM-2-is-closed / 3-way-co-compilation proof."""
    return _exec_selfhost("selfhost/compile.rvl")


@pytest.fixture(scope="module")
def compile_to(compile_rvl):
    """The fully-native driver: source + tier -> target SOURCE (byte-exact vs the
    reference on the function surface) | "REFUSED|<TAG>|<msg>" | "UNKNOWN_TIER|<t>".
    Takes only the raw source string — no reference is in its chain."""
    return compile_rvl["compile_to"]


@pytest.fixture(scope="module")
def admit(compile_rvl):
    """The native frontend admission gate (verdict only)."""
    return compile_rvl["admit"]


@pytest.fixture(scope="module")
def reference_emit() -> dict:
    return {tier: _load_reference_emit(tier) for tier in TIER_SUBDIR}


# ---------------------------------------------------------------- corpus

# The FULLY-NATIVE, byte-exact surface — the module-function + type documents where
# lower_to_ir's native IR is complete (item 232) AND the tier's native emitter
# covers the form. Each entry is (tier, fixture-subdir, filename).
#
#   py: the ten emitter-ready documents item 232 proved lower_to_ir byte-exact on.
#   rust: the pure function / type / variant slice of the rust corpus (the rust
#         emitter's covered function surface — the component and record-inference
#         documents are the item-242 / reference-IR remainder).
PY_FUNCTION_DOCS = [
    "arith.rvl", "control.rvl", "strings.rvl", "records.rvl", "result.rvl",
    "optionals.rvl", "floats.rvl", "mixed.rvl", "hostroots.rvl", "types.rvl",
    # item 391: the two builtin families the self-host could not compile at all
    # — the ASCII classification set + `codepoint_at`, and the total division
    # forms. Both halves (native lowering AND native emission) are in the chain
    # here, so this is the statement that a program using them compiles with no
    # reference in the loop.
    "classify.rvl", "checked_div.rvl",
    # item 391: tagged ADT construction, native end to end (the case table is
    # built by `lower.rvl` and the constructor call rendered by `emit_py.rvl`).
    "adt.rvl", "cache_pure.rvl",
]
RUST_FUNCTION_DOCS = [
    "arith.rvl", "control.rvl", "lists.rvl", "strings.rvl", "variants.rvl",
    "float_pub.rvl",
]
# ts (roadmap item 146, gap 2): the function-only slice of the emit_ts corpus that
# the NATIVE chain reproduces byte-exact. The ts emitter is byte-exact on all 34
# corpus documents in tests/test_selfhost_emit_ts.py — but that oracle feeds it the
# REFERENCE IR. Driven by the NATIVE `lower_to_ir` instead, the async documents drop
# out (see the deliberately-out list below), which is exactly the difference this
# corpus is here to pin.
TS_FUNCTION_DOCS = [
    "arith.rvl", "bitwise.rvl", "control.rvl", "strings.rvl", "records.rvl",
    "optionals.rvl", "mixed.rvl", "transforms.rvl",
    "classify.rvl",  # item 391: the classification builtins + `codepoint_at`
]

NATIVE_CORPUS = (
    [("py", "emit_py_corpus", n) for n in PY_FUNCTION_DOCS]
    + [("rust", "emit_rust_corpus", n) for n in RUST_FUNCTION_DOCS]
    + [("ts", "emit_ts_corpus", n) for n in TS_FUNCTION_DOCS]
)

# item 445 / item 435 (d): the reference frontend proves UNIQUE OWNERSHIP of an
# accumulation local once (`src/revl/ownership.py`) and stamps the answer on the
# IR, and the ts tier LOWERS it — `out = out.push(f(x))` renders `out.push(f(x))`
# where the binding owns its object at that write. `selfhost/lower.rvl` streams
# tokens straight to IR JSON in one pass, so it grew a SECOND scan over the same
# body tokens (its `own_*` block) that builds the statement view the forward
# dataflow needs and stamps the flow-sensitive `unique` marker where the producer
# emits the assign. So the NATIVE IR now carries the marker, the ts tier's in-place
# lowering (item 435 (d)) fires on the native chain, and `compile_to(..., "ts")`
# on `transforms.rvl` is byte-for-byte the reference compile — the gap that stood
# strict-xfail here is CLOSED and it runs through the same projection as every
# other document.

NATIVE_CORPUS_PARAMS = [
    pytest.param(tier, subdir, name)
    for tier, subdir, name in NATIVE_CORPUS
]

# The FULLY-NATIVE, byte-exact COMPONENT + extern surface (roadmap item 262, the
# capstone of the self-hosting arc). Items 242 + 241 made lower_to_ir COMPLETE and
# emitter-ready for component/extern programs; these are the documents each tier's
# native emitter is separately proven byte-exact on (tests/test_selfhost_emit_py.py
# and tests/test_selfhost_emit_rust.py). Their intersection with the now-complete
# native IR is a fully-native component compile with NO reference in the chain.
#
#   py:   the six component/service documents plus the externs document, all in the
#         emit_py corpus (item-242 emitter-ready + emit_py's covered component surface).
#   rust: the seven component/service documents the rust native emitter covers
#         (slice 3 + slice 4: the bridge, required services, effectful methods, config).
#
# "services_body.rvl" is back IN as of item 317 (was dropped by item 247,
# docs/design/teardown-contract.md): the reference py emitter's activation-body
# `emit ... compensate ...` registers through `Frame.compensation` (a
# first-class, two-phase-abort-aware COMPENSATION entry) instead of a bare
# `yield lambda: ...` disposer. Item 317 ported the SAME change into the native
# selfhost emitter this test drives (`compile_to`), so `compile_to` and the
# reference are byte-identical on this fixture again — see
# tests/test_selfhost_emit_py.py's matching note for the full rationale.
#
# "secrets_nested.rvl" is the declared-`Secret[T]` document, in the corpus as of
# the config-field port: the native chain has to carry the marking through BOTH
# halves — `lower_to_ir` stamps a config field's `secret`, and `emit_py_src`
# renders it as `_revl_ConfigSchema([...], secret=[...])`. Byte-agreement here is
# the only statement that a composition compiled through `compile_to` hands the
# runtime the same redaction instructions `revl compile --backend py` does.
PY_COMPONENT_DOCS = [
    "services_basic.rvl", "services_body.rvl", "services_config.rvl",
    "services_methods.rvl", "services_method_effects.rvl", "services_timers.rvl",
    "externs.rvl", "secrets_nested.rvl",
]
RUST_COMPONENT_DOCS = [
    "service.rvl", "services_multi.rvl", "requires.rvl", "effect_emit.rvl",
    "effect_undo.rvl", "config.rvl", "config_effect.rvl",
]
# ts: the component/service documents the native chain reproduces byte-exact,
# including `v1_component_body.rvl` — the ir_version-1 dispatch, which the native
# `emit_ts_src` handles version-agnostically (item 240).
TS_COMPONENT_DOCS = [
    "services_methods.rvl", "services_body.rvl", "services_config.rvl",
    "services_method_block.rvl", "services_composite_provide.rvl",
    "components_mixed.rvl", "v1_component_body.rvl",
]

COMPONENT_CORPUS = (
    [("py", "emit_py_corpus", n) for n in PY_COMPONENT_DOCS]
    + [("rust", "emit_rust_corpus", n) for n in RUST_COMPONENT_DOCS]
    + [("ts", "emit_ts_corpus", n) for n in TS_COMPONENT_DOCS]
)


def _fixture_path(subdir: str, name: str) -> Path:
    return ROOT / "tests" / "fixtures" / subdir / name


# ---------------------------------------- the fully-native byte-exact proof

@pytest.mark.parametrize(
    "tier,subdir,name", NATIVE_CORPUS_PARAMS,
    ids=[f"{t}:{n}" for t, _, n in NATIVE_CORPUS])
def test_native_compile_is_byte_identical_with_no_reference(
        compile_to, reference_emit, tier, subdir, name):
    """THE HEADLINE. The whole chain, end to end, on one function document:
    ``compile_to(source, tier)`` — which runs lower_to_ir + emit_<tier>_src, both
    native, in one co-compiled revl artifact — produces the target source
    BYTE-FOR-BYTE equal to the reference compile.

    ``got`` is produced from the raw source string alone; the reference
    (``compile_files`` + ``reference_emit``) computes only the expected ``want``.
    A match therefore proves revl compiled the program to ``tier`` entirely in revl,
    with NO reference in the chain."""
    path = _fixture_path(subdir, name)
    source = path.read_text(encoding="utf-8")

    # native output — source string in, target bytes out; nothing else touched
    got = compile_to(source, tier)
    assert not got.startswith(("REFUSED|", "UNKNOWN_TIER|")), (
        f"native driver did not emit for {tier}:{name}: {got[:80]!r}")

    # expected — the reference compile (used ONLY to compute want)
    want = reference_emit[tier](compile_files([str(path)]))

    assert got == want, (
        f"native compile diverged from the reference on {tier}:{name}\n"
        f"--- lengths ref={len(want)} got={len(got)} ---")


@pytest.mark.parametrize(
    "tier,subdir,name", COMPONENT_CORPUS,
    ids=[f"{t}:{n}" for t, _, n in COMPONENT_CORPUS])
def test_native_compile_of_component_program_is_byte_identical(
        compile_to, reference_emit, tier, subdir, name):
    """THE CAPSTONE (roadmap item 262). The whole chain, end to end, on a COMPONENT
    or extern document: ``compile_to(source, tier)`` — lower_to_ir + emit_<tier>_src,
    both native, one co-compiled artifact — produces the target source BYTE-FOR-BYTE
    equal to the reference compile.

    Items 242 + 241 made the native IR producer COMPLETE for component/extern
    programs (the full typed component/method body, sagas, timers, config, and the
    verbatim ``@backend`` extern bodies); this proves that completeness carries all
    the way through the composed pipeline — the services/types/externs/functions
    sections survive json_parse and drive the native emitter for BOTH tiers, with NO
    reference anywhere in the chain. ``got`` is produced from the raw source alone;
    the reference computes only the expected ``want``."""
    path = _fixture_path(subdir, name)
    source = path.read_text(encoding="utf-8")

    got = compile_to(source, tier)
    assert not got.startswith(("REFUSED|", "UNKNOWN_TIER|")), (
        f"native driver did not emit for {tier}:{name}: {got[:80]!r}")

    want = reference_emit[tier](compile_files([str(path)]))

    assert got == want, (
        f"native component compile diverged from the reference on {tier}:{name}\n"
        f"--- lengths ref={len(want)} got={len(got)} ---")


# ------------------------------------- item 146 gap 2: the last three tiers
#
# go, java and wasm each exported a bare ``pub fn emit_src``. The merge that
# builds ``selfhost/compile.rvl`` flattens public declarations by bare name, so
# admitting a second one was a duplicate-symbol error and the three tiers could
# not co-compile with the rest of the pipeline at all. Renaming them to
# ``emit_go_src`` / ``emit_java_src`` / ``emit_wasm_src`` (plus two collisions the
# wider composition exposed: ``emit_wasm.rvl``'s ``type E`` against
# ``stdlib/render.rvl``'s ``render_seq[E]`` type PARAMETER, and a test name shared
# with ``emit_rust.rvl``) was the whole cost of admitting them, exactly as the
# ts wiring predicted. All six tiers now run the SAME fully-native chain.
#
# MEASURED on the tier's own emitter corpus — the enumerated document list
# ``tests/test_selfhost_emit_<tier>.py::CORPUS`` holds to byte agreement — driven
# by ``compile_to`` (native frontend + native IR producer + native emitter, no
# reference in the chain). Snapshot at ac10ed84:
#
#     tier   corpus   emitter vs the REFERENCE IR   the FULLY-NATIVE chain
#     py         55                   55 (100%)               42 (76.4%)
#     ts         60                   60 (100%)               44 (73.3%)
#     go         23                   23 (100%)              23 (100.0%)
#     java       59                   59 (100%)               50 (84.7%)
#     rust       37                   37 (100%)               36 (97.3%)
#     wasm       21                   21 (100%)              21 (100.0%)
#     TOTAL     255                  255 (100%)              216 (84.7%)
#
# The two columns are the whole finding, and neither is a snapshot any more:
# ``test_the_residual_is_located_in_lower_not_in_the_emitter`` below RECOMPUTES
# both, per tier, over whatever that tier's CORPUS holds on the day it runs. The
# left column is asserted for EVERY document — each self-host emitter reproduces
# the reference bytes when fed the REFERENCE IR — and the right column's residue
# is pinned document-by-document in ``LOWER_GAP_DOCS``.
#
# So every residual document is a ``selfhost/lower.rvl`` gap — the native IR
# producer — and NOT an emitter gap; the earlier table read that off a java-only
# probe, and it now holds on all six tiers. The emitter half of roadmap item 146
# is complete over the enumerated corpus; what is left of the maximal story is
# item 391's arc.
GO_DOCS = [
    "arith.rvl", "bitwise.rvl", "control.rvl", "calls.rvl", "strings.rvl",
    "lists.rvl", "records.rvl", "variants.rvl", "transforms.rvl", "secrets.rvl",
    "inference.rvl", "identifiers.rvl", "match_edges.rvl", "accumulators.rvl",
    "accumulator_hygiene.rvl", "arrow_containers.rvl", "builder_literal.rvl",
    # issue #721: `%` on Float (math.Mod), a Float literal that must not be a
    # Go CONSTANT, and the `widen` marker over an Int literal.
    "float_rem.rvl",
    # joined the go emitter corpus after the table above was first taken, and
    # measured through the native chain here so the 100% row stays pinned
    "arrow_captures.rvl",
    "../emit_java_corpus/records.rvl", "../emit_rust_corpus/perf_shapes.rvl",
    "../emit_wasm_corpus/loopctrl.rvl", "../emit_wasm_corpus/strlit.rvl",
]
JAVA_DOCS = [
    "arith.rvl", "bitwise.rvl", "control.rvl", "calls.rvl", "strings.rvl",
    "lists.rvl", "maps.rvl", "records.rvl", "adts.rvl", "optmatch.rvl",
    "match_ignored.rvl", "float_numeric.rvl",
    # item 391: the branch/value shapes `selfhost/lower.rvl` did not read — the
    # block-bodied `if` in value position, the empty template, the token-level
    # record-update as a `return` rvalue — so this document was a lower.rvl gap
    # rather than an emitter one. It compiles byte-exact through the fully
    # native chain now and moves up out of LOWER_GAP_DOCS["java"].
    "branch_shapes.rvl",
    # the component/service surface the java native emitter covers
    "service.rvl", "services_multi.rvl", "comp_config_req.rvl",
    "comp_config_provide.rvl", "comp_multi_effect.rvl", "comp_fail.rvl",
    "v1_components.rvl", "legacy_void.rvl",
    # cross-tier documents the java corpus borrows
    "../emit_go_corpus/records.rvl", "../emit_go_corpus/variants.rvl",
    "../emit_py_corpus/services_config.rvl",
    "../emit_py_corpus/services_method_effects.rvl",
    "../emit_ts_corpus/components_mixed.rvl",
    "../emit_wasm_corpus/constfold.rvl", "../emit_wasm_corpus/listmem.rvl",
    "../../../examples/ecosystem-consumer-js/candidates/double_tool.rvl",
    # item 391: a component that ACQUIRES a host root (`let m = effect Map.new()
    # undo m.drop()`) and calls its verbs. `selfhost/lower.rvl` refused the
    # acquisition — an upper-cased callable head resolved as neither a required
    # service nor a scoped name — and a refused step drops the WHOLE component
    # `body` key, so the native chain emitted a body-less component. With the
    # `host` node and its host-provenance verb dispatch lowered, these five
    # documents compile byte-exact through the native java chain and move up out
    # of LOWER_GAP_DOCS["java"].
    "comp_host_map.rvl", "comp_host_map_generic.rvl",
    # item 391: the realm-placement prelude. `isolate <key> in realm("…")` and
    # `intercept <key> with { … }` are component HEADER declarations that emit no
    # activation step; `cir_body` had no arm for either, so the body walk refused
    # at the first one and the component lost its `body`, its `isolate` and its
    # `intercept` together. Stepped over in the walk and collected into the two
    # tables the reference stamps after `body`, these compile byte-exact.
    "comp_realm_isolate.rvl", "comp_realm_intercept.rvl", "legacy_realms.rvl",
    "metadata_null.rvl",
    "../emit_ts_corpus/realm_intercept.rvl", "../erase_realms.rvl",
    "../realm_conformance/provider_a.rvl", "../../../examples/tenants.rvl",
    "../../../backends/go/scenarios/tagger.rvl",
    "../../../bench/results/baseline-deepseek-v4-pro/05-rate-limiter/v1/attempt-1.rvl",
    "../../../bench/results/baseline-deepseek-v4-pro/18-config-echo/v1/attempt-1.rvl",
]
WASM_DOCS = [
    "arith.rvl", "bitwise.rvl", "control.rvl", "calls.rvl", "builtins.rvl",
    "constfold.rvl", "folding.rvl", "forloop.rvl", "inference.rvl",
    "listmem.rvl", "loopctrl.rvl", "reads.rvl", "recmem.rvl", "residuals.rvl",
    "scratch_names.rvl", "string_ops.rvl", "strlit.rvl", "variants.rvl",
    "widening.rvl",
    # joined the wasm emitter corpus after the table above was first taken, and
    # measured through the native chain here so the 100% row stays pinned
    "shortcircuit.rvl",
]

WIRED_TIER_CORPUS = (
    [("go", "emit_go_corpus", n) for n in GO_DOCS]
    + [("java", "emit_java_corpus", n) for n in JAVA_DOCS]
    + [("wasm", "emit_wasm_corpus", n) for n in WASM_DOCS]
)


@pytest.mark.parametrize(
    "tier,subdir,name", WIRED_TIER_CORPUS,
    ids=[f"{t}:{n}" for t, _, n in WIRED_TIER_CORPUS])
def test_native_compile_on_the_tiers_wired_by_item_146(
        compile_to, reference_emit, tier, subdir, name):
    """The three tiers item 146 gap 2 admitted, held to the same standard as the
    first three: ``compile_to(source, tier)`` — native frontend, native IR
    producer, native emitter, ONE co-compiled revl artifact — produces the target
    source BYTE-FOR-BYTE equal to the reference compile. ``got`` comes from the
    raw source string alone; the reference computes only ``want``."""
    path = _fixture_path(subdir, name)
    source = path.read_text(encoding="utf-8")

    got = compile_to(source, tier)
    assert not got.startswith(("REFUSED|", "UNKNOWN_TIER|")), (
        f"native driver did not emit for {tier}:{name}: {got[:80]!r}")

    want = reference_emit[tier](compile_files([str(path)]))

    assert got == want, (
        f"native compile diverged from the reference on {tier}:{name}\n"
        f"--- lengths ref={len(want)} got={len(got)} ---")


# The residual, NAMED rather than skipped, for EVERY tier. These are the corpus
# documents the fully-native chain does NOT reproduce — and for every one of them
# the tier's native EMITTER is byte-exact when fed the reference IR, so the
# divergence is located in ``selfhost/lower.rvl``, not in the emitter. Recording
# that split is what makes this a ratchet: the day lower.rvl grows the realm/
# async/host-map/branch surface, the native chain agrees, the test below fails on
# the stale entry, and the document moves out of this list instead of quietly
# staying in it.
#
# This list was java-only when the split was first measured, which left the
# "all residuals are lower.rvl gaps" claim resting on one tier's probe. It now
# covers all six, and the test recomputes the set rather than sampling it.
LOWER_GAP_DOCS: dict[str, tuple[str, ...]] = {
    "py": (
        # service dispatch and interpolation inside a component method
        "services_match.rvl",
        "services_interp.rvl",
        # witnessed effects / secret marking on the activation path
        "witnessed.rvl",
        "witnessed_secret.rvl",
        # component branch shapes
        "branches.rvl",
        # whole-program documents combining several of the above
        "../../../backends/typescript/tests/fixtures/fr1_loop.rvl",
        "../../../examples/v3_step_scheduler.rvl",
        "../../../backends/typescript/tests/fixtures/conformance.rvl",
        "../../../backends/typescript/tests/fixtures/fr3_json_int.rvl",
        "../policy_agents.rvl",
        "../../../bench/results/rerun-deepseek-v4-pro-20260826/12-replicator/v2/attempt-1.rvl",
        "../../../examples/java_match.rvl",
        "../../../src/revl/truc/components/cli.rvl",
    ),
    "ts": (
        # composite service dispatch and component expressions
        "services_composite.rvl",
        "component_exprs.rvl",
        # async coloring (async methods / await / async arrows)
        "services_async.rvl",
        "components_await.rvl",
        "async_effects.rvl",
        "async_arrow_emission.rvl",
        # spawn / instance-get
        "spawn.rvl",
        "instance_get.rvl",
        # (realm placement metadata — isolate / intercept / routes — left this
        # list when lower.rvl grew the component-header prelude; the four ts
        # realm documents now compile byte-exact through the native chain.)
        # whole-program documents combining several of the above
        "../../../bench/results/gpt-oss-20b-oneshot/03-user-cache/v1/attempt-1.rvl",
        "../../../backends/typescript/tests/fixtures/fr3_json_int.rvl",
        "../../../examples/java_match.rvl",
        "../../../backends/typescript/tests/fixtures/async_http.rvl",
        "../../../backends/typescript/tests/fixtures/async_fn_values.rvl",
        # property/component edge shapes and the CAS runtime surface
        "property_edges.rvl",
        "component_edges.rvl",
        "cas_runtime.rvl",
    ),
    # no residual: the fully-native chain reproduces the whole go corpus.
    "go": (),
    "java": (
        # async coloring
        "comp_await.rvl",
        # whole-program documents combining several of the shapes below
        "../../../bench/results/baseline-deepseek-v4-pro/09-warmup-cache/v2/attempt-1.rvl",
        "../emit_ts_corpus/services_async.rvl",
        "../../../bench/results/baseline-deepseek-v4-pro/26-log-rotator/v2/attempt-2.rvl",
        # component string interpolation / branch shapes / map inference
        "component_format.rvl",
        "component_branches.rvl",
        "map_inference.rvl",
        # the stdlib builtin surface
        "stdlib_builtins.rvl",
        "../emit_ts_corpus/property_edges.rvl",
    ),
    "rust": (
        # component edge shapes; the host-root and realm-placement documents
        # left this list when lower.rvl grew those two surfaces.
        "component_edges.rvl",
    ),
    # no residual: the fully-native chain reproduces the whole wasm corpus.
    "wasm": (),
}


def _tier_corpus(tier: str) -> list[tuple[str, Path]]:
    """The tier's OWN enumerated emitter corpus — the document list
    ``tests/test_selfhost_emit_<tier>.py::CORPUS`` that the tier's byte-agreement
    oracle runs over. Read from that module rather than restated here, so the two
    measurements can never end up enumerating different documents."""
    name = f"test_selfhost_emit_{tier}"
    spec = importlib.util.spec_from_file_location(
        "corpus_" + name, ROOT / "tests" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return [(doc, Path(module.CORPUS_DIR) / doc) for doc in module.CORPUS]


@pytest.mark.parametrize("tier", TIERS)
def test_the_residual_is_located_in_lower_not_in_the_emitter(
        compile_rvl, compile_to, reference_emit, tier):
    """The measurement that prices what is left of roadmap item 146, recomputed.

    Over the tier's whole enumerated corpus, assert BOTH halves:

      (a) for EVERY document, the native emitter driven by the REFERENCE IR is
          byte-exact — this is the emitter half of item 146, held as a fact
          rather than as a comment;
      (b) the set of documents the FULLY-NATIVE chain does not reproduce is
          exactly ``LOWER_GAP_DOCS[tier]``.

    Together those locate every residual divergence in the native IR producer.
    Neither half alone would: an emitter that diverged everywhere would also
    "not reproduce the document", and a hand-kept gap list can silently absorb a
    new divergence. (a) is the non-vacuity of (b) — it runs on the documents that
    PASS the native chain too, so the gap list cannot be padded and the emitter
    column cannot rot unnoticed."""
    native_emit = compile_rvl[f"emit_{tier}_src"]
    diverged: list[str] = []
    for name, path in _tier_corpus(tier):
        reference_ir = compile_files([str(path)])
        want = reference_emit[tier](reference_ir)

        assert native_emit(reference_ir) == want, (
            f"{tier}:{name}: selfhost/emit_{tier}.rvl diverged on the REFERENCE "
            "IR, so this document is an EMITTER gap, not a lower.rvl gap — the "
            "emitter half of item 146 no longer holds over the enumerated corpus")

        if compile_to(path.read_text(encoding="utf-8"), tier) != want:
            diverged.append(name)

    expected = list(LOWER_GAP_DOCS[tier])
    closed = [n for n in expected if n not in diverged]
    opened = [n for n in diverged if n not in expected]
    assert diverged == expected, (
        f"the {tier} residual moved.\n"
        f"  now byte-exact through the native chain (remove from "
        f"LOWER_GAP_DOCS[{tier!r}]): {closed}\n"
        f"  newly diverging (a lower.rvl regression, or a corpus document whose "
        f"IR surface lower.rvl does not cover yet): {opened}")


def test_six_way_composition_co_compiles(compile_rvl):
    """SEAM 2 is closed for ALL SIX TIERS (roadmap item 146, gap 2): lower.rvl plus
    emit_{py,ts,go,java,rust,wasm}.rvl ``use``d together in compile.rvl compile into
    ONE artifact whose namespace exposes the driver AND every tier entrypoint.
    (If the composition failed — a duplicate public ``emit_src``, a leaked private
    ``Ctx``, a colliding test name, a type name colliding with a stdlib type
    PARAMETER — the ``compile_rvl`` fixture would have raised.)"""
    assert callable(compile_rvl["compile_to"])
    assert callable(compile_rvl["admit"])
    for tier in TIER_SUBDIR:
        assert callable(compile_rvl[f"emit_{tier}_src"]), tier


@pytest.mark.parametrize("source", [
    "pub component Hidden {}",
    "pub pub fn twice() -> Int { return 1 }",
    "pub",
])
def test_native_gate_refuses_invalid_public_declarations(admit, source):
    with pytest.raises(RevlError):
        compile_source(source)
    assert admit(source).startswith("BAD|")


@pytest.mark.parametrize("modifiers", [
    "emission idempotent", "idempotent emission",
    "async emission idempotent", "idempotent emission[db] async",
])
def test_native_gate_admits_idempotent_emissions(admit, modifiers):
    source = f"service Store {{ {modifiers} fn put(value: Int) -> Int }}"
    compile_source(source)
    assert admit(source) == ""


@pytest.mark.parametrize("modifiers", ["idempotent", "async idempotent"])
def test_native_gate_refuses_idempotency_without_emission(admit, modifiers):
    source = f"service Store {{ {modifiers} fn put(value: Int) -> Int }}"
    with pytest.raises(RevlError, match="only meaningful on an `emission` operation"):
        compile_source(source)
    assert admit(source).startswith("BAD|")


@pytest.mark.parametrize("clause", [
    "cache", "cache external", "cache capability",
    "cache pure ttl 5m", "cache pure invalidated_by db", "cache pure cache pure",
])
def test_native_gate_refuses_invalid_plain_function_cache(admit, clause):
    source = f"fn f(n: Int) -> Int {clause} {{ return n }}"
    with pytest.raises(RevlError):
        compile_source(source)
    assert admit(source).startswith("BAD|")


@pytest.mark.parametrize("body", [
    "return write(n)",
    "return helper(n)",
    "let f = write\nreturn n",
    "let f = write\nreturn f(n)",
    "return apply(write, n)",
])
def test_native_cache_pure_preserves_emission_refusal(admit, body):
    source = (
        "extern emission fn write(n: Int) -> Int = @py { return n }\n"
        "fn helper(n: Int) -> Int { return write(n) }\n"
        "fn apply(f: (Int) -> Int, n: Int) -> Int { return f(n) }\n"
        f"fn cached(n: Int) -> Int cache pure {{ {body} }}\n"
    )
    with pytest.raises(RevlError) as refused:
        compile_source(source)
    assert refused.value.code == "G4"
    assert admit(source) == "G4|the reach of fn `cached` crosses write: a crossing result is not pure"


@pytest.mark.parametrize("source", [
    "fn cached(f: (Int) -> Int) -> Int cache pure { return f(1) }",
    "fn pure_value(n: Int) -> Int { return n }\n"
    "fn cached(n: Int) -> Int cache pure { let f = pure_value\nreturn f(n) }",
    "extern emission fn write(n: Int) -> Int = @py { return n }\n"
    "fn plain(n: Int) -> Int { return write(n) }",
])
def test_native_cache_reach_preserves_non_emitting_and_uncached_functions(admit, source):
    compile_source(source)
    assert admit(source) == ""


# item 429: documents the reference admits and the native gate does NOT. A FALSE
# REFUSAL, which is a self-host frontend gap and not a corpus problem, so the
# document stays and the gap is NAMED here with the exact verdict it produces.
# Recording the verdict rather than skipping the document is what makes this a
# ratchet: the day `selfhost/lower.rvl` grows the grammar, the native gate
# returns "" and this test fails on the stale entry instead of quietly keeping a
# waiver nobody rereads. Never add a line here to make a red go away — read what
# it names first.
NATIVE_GATE_GAPS: dict[str, str] = {
    # `selfhost/lower.rvl`'s extern grammar now knows every extern classification
    # the reference does: `pure`/`acquire`/`emission` (reserved keywords) and the
    # contextual `witnessed` class (item 243), each with an optional `[caps]`
    # capability scope (item 343) and `(confined: p)` reach clause (item 373).
    # The `witnessed.rvl` false refusal (`BAD|expected fn after extern`) is CLOSED
    # — the gate admits it, and its `lower_to_ir` externs section was already
    # byte-exact (tests/test_selfhost_lower_ir.py's EXTERN_DECL_GAP is empty).
    #
    # `selfhost/lower.rvl` does not put a `subscribe` acquisition's bind into the
    # component scope it resolves call heads against (`call_head_declared` reads
    # `cx.scopeNames`), so the later `sub.next()` reads as an undeclared access
    # and draws the shared G1 diagnostic. The REFERENCE admits the document; it
    # is the self-host frontend that refuses, and it refuses the tree's existing
    # stream scenario `backends/rust/scenarios/stream.rvl` with the identical
    # verdict. So this is a pre-existing lower.rvl gap that the first stream
    # document in the emit corpus makes visible, not one this document
    # introduces, and it is the frontend lane's to close (item 391), not the
    # emitter's: the rust emit oracle holds this same document byte-exact
    # against the reference, stream runtime and all (issue 1153).
    "emit_rust_corpus/comp_stream.rvl":
        "G1|`sub` is not a declared requirement of Parked",
}


def test_native_gate_admits_the_whole_emit_surface(admit):
    """The native frontend's admission verdict covers the ENTIRE emitter surface —
    every corpus document (functions AND components, both tiers) the reference
    admits, the native gate admits (``""``). The component documents are now also
    compiled byte-exact end to end (item 262); this checks the gate itself over the
    full admitted surface, including documents outside the byte-exact emit slice.

    ``NATIVE_GATE_GAPS`` above holds the documents where that is not yet true,
    pinned to the exact refusal so closing the gap reddens this test rather than
    outliving the waiver."""
    for subdir in ("emit_py_corpus", "emit_rust_corpus", "emit_ts_corpus"):
        for path in sorted((ROOT / "tests" / "fixtures" / subdir).glob("*.rvl")):
            compile_files([str(path)])  # the reference admits it
            key = f"{subdir}/{path.name}"
            verdict = admit(path.read_text(encoding="utf-8"))
            expected = NATIVE_GATE_GAPS.get(key, "")
            if expected:
                assert verdict == expected, (
                    f"{key}: the native gate is recorded in NATIVE_GATE_GAPS as "
                    f"refusing this with {expected!r} and now says {verdict!r}. "
                    f"If the self-host grammar landed, DELETE the entry; if the "
                    f"refusal merely changed shape, read it before rewriting it.")
            else:
                assert verdict == "", f"{key}: {verdict!r}"


# The four checking positions a transparent type alias has to be erased at, plus
# the ones that must not move. Issue #1148's own fix erased at every LOWERING
# site and left the ADMISSION walk seeding its type environment from the RAW
# annotations, so the gate typed a body against the alias SPELLING while the
# reference types it against the erased type: `fn add(a: Count, b: Count)` drew
# "operand of `+` expects `Int`, got `Count`" for `a + b`, on a document the
# reference admits. Every case below is admitted by the reference and refused by
# the native gate before the fix, except the four marked GUARD, which are admitted
# on both trees and are here so the fix cannot buy the four by spilling substitution
# somewhere it does not belong.
ALIAS_ERASURE_CASES: dict[str, str] = {
    # the reference's whole-program uniform-name-substitution rule, so a
    # declaration site alone is not enough: the environment has to be seeded.
    "fn_params": "type Count = Int\n"
                 "fn add(a: Count, b: Count) -> Count { return a + b }",
    "fn_returns": "type Sku = Str\n"
                  "fn tag() -> Sku { return \"x\" }",
    "let_annotation": "type Count = Int\n"
                      "fn one() -> Count { let c: Count = 1 return c }",
    "let_annotation_widens": "type Ratio = Float\n"
                             "fn wide() -> Ratio { let r: Ratio = 1 return r }",
    "let_annotation_pins_empty": "type Ids = List[Int]\n"
                                 "fn none() -> Ids { let xs: Ids = [] return xs }",
    "extern_parameter": "type Count = Int\n"
                        "extern pure fn bump(n: Count) -> Count = @py { return n + 1 }\n"
                        "fn calls() -> Count { return bump(1) }",
    "extern_parameter_through_a_let":
        "type Count = Int\n"
        "extern pure fn bump(n: Count) -> Count = @py { return n + 1 }\n"
        "fn calls2() -> Count { let n: Count = 1 return bump(n) }",
    "operand_of_a_multiply": "type Ratio = Float\n"
                             "fn scale(r: Ratio) -> Ratio { return r * 2.0 }",
    "variant_payload":
        "type Count = Int\n"
        "type Found = Hit(Count) | Missing\n"
        "fn payload(f: Found) -> Count { return match f { Hit(n) => n, Missing => 0 } }",
    "record_field": "type Count = Int\n"
                    "type Row = { id: Count }\n"
                    "fn read(r: Row) -> Count { return r.id }",
    "alias_of_an_alias": "type Count = Int\n"
                         "type Tally = Count\n"
                         "fn chained(n: Tally) -> Tally { return n }",
    # GUARD: every alias in the same document without a checker position that
    # would have disagreed, so only a substitution that is too eager can move one.
    "handler_in_a_parameter": "type Count = Int\n"
                              "type Sku = Str\n"
                              "type Handler = (Count) -> Sku\n"
                              "fn takes(h: Handler) -> Sku { return h(1) }",
    "arrow_annotations": "type Count = Int\n"
                         "type Sku = Str\n"
                         "fn f() -> Sku { let g = (v: Count): Sku => \"x\" return g(1) }",
    "keyed_read": "type Reg = Map[Str, Int]\n"
                  "type Sku = Str\n"
                  "fn keyed(m: Reg, k: Sku) -> Int { return m[k] }",
    "nested_type_argument": "type Ids = List[Int]\n"
                            "fn first(xs: Ids) -> Int { return xs[0] }",
}


@pytest.mark.parametrize("case", sorted(ALIAS_ERASURE_CASES))
def test_native_gate_erases_a_transparent_alias_before_it_types_a_body(admit, case):
    """Issue #1148's GATE half. `selfhost/lower.rvl` erases a transparent alias
    (`_resolve_type_aliases`' counterpart) at every declaration site, and the
    reference erases BEFORE the checker runs — so the admission walk must seed its
    type environment, its `let` annotations, its return type and its signature
    rows from the ERASED spelling too. Seeded from the raw annotation instead, the
    gate refused the document's own arithmetic (`got `Count``), its own return, its
    own `let`, and its own call to an aliased extern, all four on documents the
    reference admits.

    Named separately from the corpus sweep above so a regression reports WHICH
    checking position lost the substitution, not just that some document refused.
    """
    source = ALIAS_ERASURE_CASES[case]
    compile_source(source)  # the reference admits it first
    assert admit(source) == "", f"{case}: {admit(source)!r}"


# --------------------------------------------- the refusal composes too

# Programs the reference REJECTS. The composed native driver must refuse them with
# the reference's guarantee tag — and never reach the IR producer or an emitter.
_REJECTED = [
    ("g4 plain provider reaches an emission",
     "extern emission fn audit_write(msg: Str) -> Int = @py { return 1 }\n"
     "service Cache { fn put(key: Str) }\n"
     "component C provides cache: Cache {\n"
     "  provide cache { fn put(key) { let n = audit_write(key) } }\n"
     "}\n", "G4"),
    ("g2 two components provide one key",
     "service S { fn op(x: Str) -> Str }\n"
     "component A provides k: S { provide k { fn op(x) { return x } } }\n"
     "component B provides k: S { provide k { fn op(x) { return x } } }\n", "G2"),
    ("a1 async extern reached from a sync method",
     "extern emission async fn http_post(url: Str, body: Str) -> Str = @py { return url }\n"
     "service Http { emission fn post(url: Str, body: Str) -> Str }\n"
     "component Poster provides http: Http {\n"
     "  provide http { fn post(url, body) = http_post(url, body) }\n"
     "}\n", "A1"),
]


def _reference_tag(src: str) -> str:
    """The reference's guarantee tag for a rejected program (a small classifier
    over the same evidence tests/test_selfhost_lower.py uses)."""
    try:
        compile_source(src, "diff.rvl")
        return ""  # admitted
    except RevlError as e:
        if e.code in ("G4", "A1"):
            return e.code
        if "(G2)" in e.message:
            return "G2"
        return "OTHER:" + e.message


@pytest.mark.parametrize("case", _REJECTED, ids=[n for n, _, _ in _REJECTED])
@pytest.mark.parametrize("tier", ["py", "rust", "ts"])
def test_refused_program_never_reaches_an_emitter(compile_to, tier, case):
    """A program the reference rejects is refused by the composed native driver
    with the SAME guarantee tag, for every tier — the ``REFUSED|`` verdict means
    the native IR producer and emitter are never reached (the one revl promise,
    enforced natively before any code is generated)."""
    name, src, tag = case
    assert _reference_tag(src) == tag, f"corpus bug: {name}"
    got = compile_to(src, tier)
    assert got.startswith("REFUSED|"), f"{name} ({tier}): {got[:80]!r}"
    got_tag = got.split("|")[1]
    assert got_tag == tag, f"{name} ({tier}): tag {got_tag!r} != {tag!r}"


def test_unknown_tier_is_reported(compile_to):
    """A tier outside the proven set is reported, never silently emitted as if
    the pipeline covered it. go/java/wasm used to be the witnesses here; item 146
    gap 2 wired them, so the witness is a tier revl has no backend for at all."""
    for tier in ("dotnet", "swift", "", "PY"):
        assert compile_to("fn id(x: Int) -> Int { return x }", tier) == \
            "UNKNOWN_TIER|" + tier


# -------------------------------------- the declared `Secret[T]` marking, native

# `src/revl/taint.py` is the reference pass that strips the `Secret[...]`
# qualifier off every declared type and leaves the stamps a backend redacts from
# (`externs[i].secret_return` / `secret_witness`, a `params[i].secret` on an
# extern, a module fn and a service operation, and a config `fields[i].secret`).
#
# It had NO counterpart anywhere in the self-host frontend — `grep -ci secret
# selfhost/{lexer,parser,checker,lower}.rvl` was 0 in all four — so `lower_to_ir`
# produced an IR with the qualifier still standing as a TYPE NAME and none of the
# stamps set, and `compile_to` emitted a module with the whole declared marking
# MISSING (no `_revl_secret_result` / `mark_secret` on py, no `host.secretResult`
# / `host.markSecret` / `secret: true` on ts) while `revl compile --backend
# <tier>` emitted all of it. Not an emitter gap: both native emitters were ported
# (py by item 429(d), ts by item 146 gap 2) and were byte-exact on their own
# `secrets.rvl` document when fed the REFERENCE IR — an emitter that knows how to
# redact cannot act on a marking the frontend never produced.
#
# `selfhost/lower.rvl` now carries the declaration-side marking (the qualifier
# surgery plus the four stamps), so this test is no longer a recorded gap but the
# END-TO-END statement of it: the fully-native compile of a `Secret[T]` document
# is byte-identical to the reference compile, on both wired tiers. Note the SCOPE
# — what landed is the declaration marking, not the reference's flow analysis
# (origins, `confidential`, the G9 refusals), which the self-host still does not
# have.
_SECRET_DOCS = [("py", "emit_py_corpus"), ("ts", "emit_ts_corpus")]


@pytest.mark.parametrize("tier,subdir", _SECRET_DOCS, ids=[t for t, _ in _SECRET_DOCS])
def test_native_compile_carries_the_declared_secret_marking(
        compile_to, reference_emit, tier, subdir):
    """A program declaring `Secret[T]` compiles natively to the same bytes the
    reference compile produces — markings included."""
    path = _fixture_path(subdir, "secrets.rvl")
    source = path.read_text(encoding="utf-8")
    got = compile_to(source, tier)
    assert not got.startswith(("REFUSED|", "UNKNOWN_TIER|")), got[:80]
    want = reference_emit[tier](compile_files([str(path)]))
    assert got == want


def test_compile_rvl_in_file_tests_pass(compile_rvl):
    """The composed artifact's own `test` blocks run under the python backend —
    including the driver's four (unsupported tier, native refusal, fully-native
    py/rust) and every block the three co-compiled stages contribute."""
    tests = compile_rvl.get("REVL_TESTS")
    assert tests and len(tests) >= 4, "expected the driver's test blocks in REVL_TESTS"
    for entry in tests:
        fn = entry[-1] if isinstance(entry, tuple) else entry
        fn()
