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
import json
import os
import re
import shutil
import subprocess
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_files, compile_source  # noqa: E402
from revl.errors import RevlError  # noqa: E402
from _load_by_path import load_by_path  # noqa: E402


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
def lower_to_ir(compile_rvl):
    """The native IR PRODUCER, from the same co-compiled artifact — for the pins
    that must read the produced IR itself rather than the bytes a tier emitted
    from it. `compile_to` runs it internally; this exposes it directly."""
    return compile_rvl["lower_to_ir"]


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
# reference in the chain). Snapshot at 77f1d7f6:
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
    # issue #106: the stdlib / Opt / Result / Map surface and the extern
    # `config` / `//revl:import` shapes, byte-exact through the native chain
    "stdlib_surface.rvl", "extern_config.rvl", "loop_shapes.rvl", "blob_probes.rvl",
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
        # (component-method service dispatch and `${…}` interpolation, the
        # witnessed activation/method acquisitions and their `Secret[T]`
        # marking, and the effect block's pure setup all left this list when
        # lower.rvl grew the `match`/`format`/`fn`/witnessed-`effect` surface.)
        #
        # (issue #721: provide-method control flow (`if`/`while`/`for`/
        # `break`/`continue`) left this list when `cir_method_stmts` in
        # selfhost/lower.rvl grew those arms — the native IR producer now
        # lowers a method-body control-flow step to the same `if`/`while`/`for`
        # shape the reference does. The same slice taught the producer a
        # chained builtin call written in place (`x.concat(a).concat(b)`): it
        # nests the receiver's node as the builtin's `target` instead of
        # refusing and dropping the whole component.)
        # item 391: a component-body `Stream.source()` acquisition (a `host`
        # node). The EMITTER half is byte-exact on the reference IR; the native
        # IR producer used to drop the whole `let src = effect Stream.source()
        # undo src.close()` step, so the component emitted an empty body and the
        # module lost its `Stream` import. The `Map`/`Pool`/`Job` host calls in
        # services_host.rvl went through the native chain byte-exact already.
        # (issue #2087: it left this list with the stream slice below — the
        # native producer now reads the `subscribe` bracket, the combinator
        # chain, the fan-in and the qualifier tail.)
        # (issue #2086: `../emit_py_placement.rvl` left this list — the
        # placement shapes `selfhost/emit_py.rvl` used to drop. The native IR
        # producer used to drop three things the reference IR carries: the
        # `routes` entry of a routed require (so no router, no `realm_label`,
        # and the key back in the inject gate), every operation declared with a
        # `commutative` or `idempotent` modifier (the Ledger service came out
        # empty), and the `undo_captures` pin on a method-body effect. The
        # producer now reads all three, plus the `let-effect` step's key order
        # (`bind` after `acquire`/`undo`), so the document is byte-exact through
        # the fully-native chain.)
        # item 391: another EMITTER-exact document the native IR producer used
        # to reproduce wrongly — the reference frontend escapes a parameter
        # named `len_` to `len__` (and every read of it) and leaves a call of
        # the user's `fn len` as `len`. The native IR escaped the reads but not
        # the parameter, and escaped the call, so the native chain emitted
        # `def ladder(len__, sorted__)` over a body that read `len___` and
        # called `len__`.
        # (issue #2090: it left this list. The native env answered
        # `tenv_get(env, n) != ""` — "is this name bound at all" — where the
        # reference asks `n in scope` — "is this name a LOCAL". The module
        # path's env holds the case table and every module `fn`'s own name as
        # well as the body's locals, so `fn len`'s own name counted as bound
        # and its CALL site was mangled to `len_` while the reference left it
        # verbatim. `env_mut_put` now also writes a `local ` marker and
        # `params_env` writes one per parameter, and the `var` arm reads that
        # marker instead, so a local is mangled at its declaration and at every
        # read while a callable reference stays verbatim. The declaration/read
        # half of this entry already agreed before the change; only the call
        # diverged.)
        # (issue #2089: stdlib/fs.rvl left this list — the native IR producer
        # used to drop the extern's host `refs`, so the module lost its
        # `import inspect` / `_REVL_REFS` header. `lower_to_ir_refs` now reads
        # the driver's per-ref host probe off a wire and stamps the `refs` key
        # in the reference's own position and key order, so the document
        # reproduces byte-for-byte through the fully-native chain on both the
        # py and ts tiers.)
        # (issue #2088: the six in-file test documents left this list when
        # `selfhost/lower.rvl` grew the `tests`/`fault_tests` producers —
        # `test_secs_walk` over `lir_plain_test`/`lir_lifecycle_test`/
        # `lir_fault_test`. selfhost/emit_py.rvl already emitted all three
        # byte-exact from the reference IR; only the producer was missing the
        # section, so the native chain used to emit each of these documents
        # without its REVL_TESTS / lifecycle harness / REVL_FAULT_TESTS trailer
        # and without the imports that trailer pulls in. They now reproduce
        # byte-for-byte through the fully-native chain.)
        #
        # item 391: the await-seed slice. Both were emitter-exact from the
        # reference IR. async_timer.rvl's `tests` section was byte-exact too
        # (issue #2088 above), so what withheld it was the OTHER half of this
        # slice: its timer bodies `emit counter.tick()` through a required key
        # and `Counter.tick` is an `emission async fn`, but the native IR
        # producer stamped no `"async": true` on that timer step (item 170's
        # `_timer_body_reaches_async` colouring), so the emitter omitted the
        # in-flight window and its teardown. That is a different family from
        # #2088's — the same one that withheld emit_py_async_shapes.rvl, which
        # reaches the frontend's sync instance of a fn with an async-typed
        # parameter (`drive_revl_sync`) and the async-coloured timer flag,
        # neither of which the native IR producer emitted, so the native chain
        # also lost the `extern_emit` and `asyncio` imports.
        # (issue #2092: both left this list when `selfhost/lower.rvl` grew the
        # timer-step async stamp — `timer_reaches_async`, the `_timer_body_
        # reaches_async` port that ORs a directly-reached async required op
        # against the arrow-pruned call set — and the sync monomorph family —
        # `mono_plan`, `_monomorphize_free_fn_calls` then `_synthesize_sync_
        # monomorphs`: a sync caller's call to a colour-polymorphic `fn` is
        # redirected to a synthesized `_revl_sync` clone whose async-typed
        # parameters are stripped back to plain fn types, the clone being
        # appended to `functions` last. Both documents now reproduce
        # byte-for-byte through the fully-native chain.)
        # item 391: the stream slice. All five were emitter-exact from the
        # reference IR; what the native IR producer used to drop was the body's
        # stream steps (the `Stream.source()` acquisition, as in
        # services_host_stream.rvl, the `Pool.open` one beside it, and the
        # `subscribe` that reads the source), so the native chain emitted a sync
        # body with no `Stream` import. stream_event_130.rvl also came out as
        # `ir_version 1` where the reference IR of a typed-event handler carries
        # 3.
        # (issue #2087: all seven py entries of the stream slice — the five
        # documents named just above, services_host_stream.rvl and
        # emit_py_stream_builtin_bind.rvl below — left this list when
        # `selfhost/lower.rvl` grew the stream statement forms: the `subscribe`
        # bracket (its `subscribe: true`/`policy` step keys, the combinator
        # `stages` and the `buffer`/`drain` qualifier tail), the `every <x> in
        # <sub>` iteration, and the `on <Event> as <x> [in <sub>]` handler with
        # its contract object and its desugared `sub_<requirement>` bracket. The
        # native producer also had to admit a bare read of a required `Stream[T]`
        # (a `req` node) and stop reading `on` as an ordinary ident, and the
        # document version now carries the same `stream-iter` trigger the
        # reference's `uses_timers` does.)
        # issue #1646 follow-up: emitter-exact, and closed with the slice above.
        # (issue #2085: the validated slice left this list when
        # `selfhost/lower.rvl` grew the validated operation producer. The
        # producer now reads the `validated` and `retry` modifiers off the
        # method header and, for a `validated` operation, derives the response
        # contract from the declared return exactly as the reference does:
        # `_validated_response_ir`'s stripped return type becomes a JSON Schema
        # (`type_schema.json_schema_for`), and that schema becomes the decoding
        # grammar (`decode_grammar.decode_grammar_for`) — so the service carries
        # its grammar registry and validate seam and the call renders through it.
        # `../emit_ts_refusals/validated_emission_operation.rvl` and
        # `../emit_py_validated_shapes.rvl` now reproduce byte-for-byte through
        # the fully-native chain.)
        # (issue #2131: the component branch shapes left this list when
        # `selfhost/lower.rvl` grew the six producers they were waiting on.
        # Five were component-position gaps in the pure-expression walk:
        # `cir_expr` gained `RecUpd` (`record_update`), `OptField` (`optfield`)
        # and `OptCall` (`optcall`), `cir_hostacq` gained `Map.empty()`'s
        # `maplit`, and `cir_field` gained the `"opt": true` mark — each spelled
        # identically by the reference in both dialects, so the fn path already
        # had them and only the component path dropped them. The sixth is the
        # statement-block match arm: `selfhost/parser.rvl` reads an arm body as
        # an expression, so it had no node for `Some(n) => { let doubled =
        # n * 2 doubled + 1 }`; `ArmN` now carries `blk`/`blkTail`/`isBlk`
        # (read by `p_blk_arm`, with `body` left `null` so the arm walkers that
        # cannot see the bindings never read one) and `cir_match` routes the
        # block to `cir_block_arm`, which emits the reference's inline
        # `_lower_component_block_arm` `do` node. `branches.rvl` is byte-exact
        # (6542) through the fully-native chain.)
        # whole-program documents combining several of the above.
        # (`../../../examples/v3_step_scheduler.rvl` left this list with the
        # spawn/instance surface: it is the one py document whose components
        # both spawn a child and read a provision back off the handle, and the
        # component dialect's record literal and bare-name `fn` call closed the
        # rest of it. `../../../backends/typescript/tests/fixtures/fr1_loop.rvl`
        # left it when the component dialect grew its `Arrow` arm, issue #1844:
        # its provide method passes an arrow to `run_loop`.)
        # (issue #2181: `../../../backends/typescript/tests/fixtures/fr3_json_int.rvl`
        # left this list, the last py entry. It declares no externs of its own
        # and imports them (`use "stdlib/json.rvl" { json_parse,
        # json_stringify }`); the reference's `compile_files` merges that
        # module's WHOLE extern section into the document's IR, `json_try_parse`
        # included. `selfhost/compile.rvl`'s driver linked only the imported
        # `services` rows. `ul_link_externs` now appends the extern section of
        # every module in the document's pure closure, in the reference's
        # order, and the document is byte-exact on py and ts.)
    ),
    "ts": (
        # (issue #2317: `block_arm_records.rvl` left this list. A block match
        # arm in a module `fn` is now lambda-lifted into its `match_arm_N`
        # helper by `selfhost/lower.rvl` (`lir_lift_stmt`), and a structural
        # record type nested in a type argument splits at its own top level.)
        # (`services_composite.rvl` left this list when lower.rvl grew the
        # component dialect's list literal, and `component_exprs.rvl` with its
        # `format` template, bare-name `fn` call and tagged `adt` construction,
        # which closed documents on the py and java tiers as well.)
        # (async coloring left this list entirely. `components_await.rvl` went
        # with the `await` activation STEP — the colouring itself is a
        # whole-program property the producer already stamped, and what was
        # missing was the step, so the body walk refused at the first `await`
        # and the component lost its body with it. `services_async.rvl`,
        # `async_effects.rvl`, `async_arrow_emission.rvl` and the two
        # backends/typescript async fixtures went with the three remaining
        # pieces: the `async fn` provide method, the `"async": true` stamp an
        # `effect await`/`await emit` step carries, and the item-92 coercion
        # that colours an arrow passed into an `(…) -> Async[T]` parameter.)
        # (spawn / instance-get left this list when `selfhost/lower.rvl` grew
        # the instance surface: the `spawn <C> with { … }` acquire node, the
        # handle's own verb tail, and the `<handle>.<key>` provision read that
        # lowers to `instance-get`. `spawn.rvl` and `instance_get.rvl` now
        # compile byte-exact through the fully-native chain.)
        # (realm placement metadata — isolate / intercept / routes — left this
        # list when lower.rvl grew the component-header prelude; the four ts
        # realm documents now compile byte-exact through the native chain. See
        # the issue #2086 note on `../../../stdlib/router.rvl` below for the
        # `routes` half, which the prelude did not cover.)
        # whole-program documents combining several of the above.
        # (`../../../examples/java_match.rvl` left this list with issue #1845:
        # its component's `Err(_)` arm binds `__`, the reference's `_safe_name`
        # spelling of the soft keyword `_`.)
        # (issue #2181: `fr3_json_int.rvl` left this list with the py entry
        # above; one driver fix closes both tiers.)
        # (issue #2092: `component_edges.rvl` left this list with the py entry
        # above. Its ONE async provide method was withheld by a producer that
        # had the `await` step for an ACTIVATION body but not for a provide
        # METHOD body, so the first suspension inside the method returned "not
        # reproduced" and took the whole method body — and with it the
        # component's `body` — with it. The one native `selfhost/lower.rvl`
        # producer fix closes the residual for BOTH tiers.)
        # (`property_edges.rvl` left this list when the `.length` PROPERTY form
        # on a sized receiver started carrying `sized_length`, and
        # `cas_runtime.rvl` when the per-invocation `let … = effect … undo …`
        # bracket, the `assign` step and `undo_captures` landed in a provide
        # method body.)
        # (issue #721: provide-method control flow left this list with the py
        # entry above — the one native `selfhost/lower.rvl` producer fix closes
        # the residual for BOTH tiers, because selfhost/emit_ts.rvl already
        # reproduced the reference bytes from the reference IR.)
        # issue #106: the families selfhost/emit_ts.rvl learned in one slice.
        # The emitter half of each is byte-exact on the REFERENCE IR (asserted
        # above for every document); the native chain is not, because
        # selfhost/lower.rvl does not produce the IR these documents need.
        # (issue #2086: `../../../stdlib/router.rvl` — a routed require's header
        # — left this list with the py entry above. The native producer now
        # emits the component's `routes` entry, so the router, the `realm_label`
        # and the inject-gate key all come back.)
        # (issue #2089: stdlib/fs.rvl left this list with the py entry above —
        # an extern's host `refs` used to drop, taking the thunks and their
        # imports with them. The one native `selfhost/lower.rvl` producer fix
        # closes the residual for BOTH tiers.)
        # (issue #2088: the in-file `tests` section, plain and lifecycle, landed
        # in `selfhost/lower.rvl`, so `lifecycle_cache.rvl`, the item 280
        # Opt-gap document and the scenario `advance.rvl` left this list.)
        # timers inside a lifecycle-tested document,
        # (issue #2092: `../../../examples/async_timer.rvl` left this list with
        # the py entry above — the native producer now stamps the timer step
        # `"async": true` when its body reaches an async callable
        # (`timer_reaches_async`), which is the one `selfhost/lower.rvl` producer
        # fix both tiers needed.)
        # (issue #2087: the stream surface (`subscribe`, `merge`, `every ... in`,
        # `on ... as`) closed with the py entries above — `stream_130.rvl` and
        # `comp_stream.rvl` left this list too, the one native `selfhost/lower.rvl`
        # producer fix covering both tiers because selfhost/emit_ts.rvl already
        # reproduced the reference bytes from the reference IR.)
        # (issue #2131: `../emit_py_corpus/branches.rvl` left this list with the
        # py entry above — one `selfhost/lower.rvl` producer fix covers both
        # tiers, since `selfhost/emit_ts.rvl` already reproduced the reference
        # bytes from the reference IR. The six component-position producers are
        # named in the py list above.)
        # and the two documents written for this slice, which combine the above.
        # (issue #2086: `ref_externs.rvl` left this list — a declaration whose
        # ONLY body arms are HOST REFS (`= @ts ref sym from "path"`) used to fail
        # the producer's `bcount == 0` exit gate, which dropped the whole
        # `externs` section. The reference's `_lower_externs` has no body-count
        # gate at all and the PARSER already refuses a declaration with no arm of
        # either kind, so the gate now also accepts a ref-only declaration.)
        # (issue #2131: `routed_timers.rvl` left this list with the entry above.
        # Its `Tally.total()` is the statement-block match arm, so the same
        # block-arm producer that closed the py document closed this one.)
        # (issue #2096: `extern_compensate.rvl` left this list with the
        # `async_timer.rvl` entry above. Re-measured against `main`: the
        # extern's declared `compensate` slot was ALREADY lowered (`ir_extern`'s
        # `compensate` clause, issue #1592) and the native ts bytes matched the
        # reference everywhere except the `Pulse` timer step — the `emit
        # ledger.tick()` firing had no `"async": true`, so the emitter omitted
        # the in-flight window and its teardown. The timer stamp closed it, so
        # the entry this issue named is byte-exact for a cause other than the
        # one the issue's body named.)
        # Issue #1954 (item 256) / issue #2012: the capability-bound secret
        # seam. This entry is CLOSED — `selfhost/lower.rvl` now reads
        # `secret NAME for CAP` at the top level, lowers it to the reference's
        # `secrets` rows (`secrets_bind`), stamps the bound names onto every
        # emission extern that serves the capability, and refuses the four
        # malformed shapes with the reference's own messages, so
        # `../../noncensus_corpus/emit_ts_bound_secret.rvl` is byte-exact
        # through the fully-native chain and no longer belongs in this tuple.
        #
        # It is the one document in this tuple that the native GATE used to
        # refuse rather than merely mis-lower, which is why it sits outside every
        # corpus directory the census walks (`tests/noncensus_corpus/`): inside
        # one it would have had to enter the census baseline as a standing
        # `false-reject` waiver. The gate now refuses it with the reference's
        # message, so that waiver was never needed.

        # (issue #2088: the `abort` lifecycle STEP document and the issue #2009
        # py #1945 part 2 host-map write journal left this list when the
        # `tests`/`fault_tests` producers landed in `selfhost/lower.rvl`. Both
        # were never emitter gaps — selfhost/emit_ts.rvl fed the REFERENCE IR
        # reproduced each byte-exactly, `abort` arm / `frameForCtx` import and
        # `journalBegin()`/`guard()` pair included; what the fully-native chain
        # could not do was REACH that IR, because each document's whole payload
        # was its `tests` section and the producer carried none. host_map_journal
        # is the only corpus document that reaches `journalBegin()`/`guard()` —
        # its `provide`-method bracket is the one shape where no activation-body
        # bracket ever created the `Frame`.)
    ),
    "go": (
        # (issue #2088: in-file `test` blocks. selfhost/emit_go.rvl already
        # reproduced the reference bytes from the reference IR; the producer was
        # the missing half, so the fully-native chain used to emit the functions
        # without the test functions or the `testing` import. With the
        # `tests`/`fault_tests` producers in `selfhost/lower.rvl` all four
        # documents below left this list — `in_file_tests.rvl`, the item 280
        # Opt-gap document, the issue #1631 erased-Result document, and the
        # combined path's `comp_provide_pure.rvl`.)
        # no residual: the fully-native chain reproduces the whole go corpus.
    ),
    "java": (
        # (async coloring left this list entirely. `comp_await.rvl` and the two
        # bench documents went with the `await` activation step;
        # `component_format.rvl` and `component_branches.rvl` with the
        # component `format` node, the bare-name `fn` call and the `index`
        # node; `../emit_ts_corpus/services_async.rvl` with the `async fn`
        # provide method.)
        # (`map_inference.rvl` left this list when lower.rvl grew the component
        # dialect's record literal, which was the last shape its activation body
        # spelled that the walk could not read; the stdlib builtin surface —
        # `stdlib_builtins.rvl` and the borrowed
        # `../emit_ts_corpus/property_edges.rvl` — left it with the
        # `sized_length` property form.)
        # no residual: the fully-native chain reproduces the whole java corpus.
    ),
    "rust": (
        # (`component_edges.rvl` left this list with issue #1818: its provide
        # methods return `Some(...)`, which dropped the whole component body.
        # The host-root and realm-placement documents left it earlier, when
        # lower.rvl grew those two surfaces.)
        # issue 1153's two component documents. Neither was an EMITTER residual:
        # the rust byte oracle in tests/test_selfhost_emit_rust.py holds both
        # byte-exact when it is handed the REFERENCE IR (host runtime and all),
        # so what was left in each was the native IR producer.
        # `comp_await_job.rvl` is now CLOSED: its activation body awaits a host
        # `Job`, the body walk had no `await` step to carry that, and a refused
        # step drops the WHOLE component `body` — so the native chain emitted
        # 7,067 bytes against the reference's 11,094, the module with no
        # `Job`/`JobHandle` runtime in it. `comp_stream.rvl` was no longer
        # refused before any rust is built either: the item-130 stream statement
        # forms landed in `selfhost/lower.rvl` (#1139) and the native gate now
        # ADMITS it, so its residual was measured in bytes by the same comparison
        # as every document above. It was `selfhost/lower.rvl`'s, and it was
        # NAMED rather than skipped, so the day lower.rvl grew that surface this
        # list shrank instead of quietly keeping a waiver nobody rereads — which
        # is what happened: issue #2087's stream statement forms closed it.
        # (`bridge_types.rvl`, item 391's bridge marshalling document, left with
        # issue #1818: `fn weight(name) = None` dropped the component body.)
        # (issue #2088: in-file `test` blocks left this list when the
        # `tests`/`fault_tests` producers landed in `selfhost/lower.rvl`.
        # selfhost/emit_rust.rvl already reproduced the reference bytes from the
        # reference IR; the producer was the missing half, so the native chain
        # used to emit the functions and drop every `#[test] fn`. The other
        # rust documents in this tuple are unrelated gaps and stay.)
        # (issue #2095: the by-value reuse document left this list when the
        # native producer learned to type an arrow at all (`arrow_fn_ty`, the
        # port of `infer_ast`'s `ExprArrow` arm). The `let`-bound `bump` used to
        # reach the environment with no type, so the call to it read back "" and
        # `i = bump(i) + 1` was written without the reference's
        # `"operands": "Int"` annotation. The other rust documents in this tuple
        # are unrelated gaps and stay.)
        # item 391: the provide-method control-flow document. Emitter-exact from
        # the reference IR; the native IR producer drops the whole component
        # `body` of a component whose provide method holds a control-flow step,
        # because `cir_method_stmts` in selfhost/lower.rvl has no
        # `if`/`while`/`for` arm.
        "method_control_flow.rvl",
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

        if compile_rvl["compile_to_at"](path.read_text(encoding="utf-8"), tier, str(path)) != want:
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


_PURE_CLOSURE_MODULES = {
    # Declared z before a, so a path-sorted closure and a declaration-order one
    # disagree; `Store` is a service-only import, so `s_hidden` must not travel.
    "doc.rvl": (
        'use "z_mod.rvl" { z_one }\n'
        'use "a_mod.rvl" { a_one }\n'
        'use "s_mod.rvl" { Store }\n\n'
        "extern pure fn d_own(x: Int) -> Int = @py { return x } = @ts { return x }\n\n"
        "fn total(x: Int) -> Int { return d_own(a_one(z_one(x))) }\n"),
    # a_mod brings a private extern (the whole section travels) and a pure
    # dependency of its own (the closure is transitive).
    "a_mod.rvl": (
        'use "t_mod.rvl" { t_one }\n\n'
        "pub extern pure fn a_one(x: Int) -> Int = @py { return x + 1 } = @ts { return x + 1n }\n\n"
        "extern pure fn a_priv(x: Int) -> Int = @py { return x } = @ts { return x }\n"),
    "z_mod.rvl": (
        "pub extern pure fn z_one(x: Int) -> Int = @py { return x * 2 } = @ts { return x * 2n }\n"),
    "t_mod.rvl": (
        "pub extern pure fn t_one(x: Int) -> Int = @py { return x - 1 } = @ts { return x - 1n }\n"),
    "s_mod.rvl": (
        "pub extern pure fn s_hidden(x: Int) -> Int = @py { return x } = @ts { return x }\n\n"
        "service Store {\n  fn get(key: Str) -> Str\n}\n"),
}


@pytest.mark.parametrize("tier", ("py", "ts"))
def test_native_driver_links_the_pure_closure_externs(
        compile_rvl, reference_emit, tmp_path, tier):
    """Issue #2181: a `use`d module's extern section reaches the document's IR
    the way `compile_files` merges it: the document's own externs first, then
    every module in its pure closure, breadth first and path-sorted, each
    module's WHOLE section (a private extern too), and nothing from a module
    imported only for a service."""
    for name, text in _PURE_CLOSURE_MODULES.items():
        (tmp_path / name).write_text(text, encoding="utf-8")
    doc = tmp_path / "doc.rvl"
    reference_ir = compile_files([str(doc)])
    assert [e["name"] for e in reference_ir["externs"]] == [
        "d_own", "a_one", "a_priv", "z_one", "t_one"]
    want = reference_emit[tier](reference_ir)
    got = compile_rvl["compile_to_at"](doc.read_text(encoding="utf-8"), tier, str(doc))
    assert got == want


# Issue #2317: block match arms in module fns. The issue's reproducer; two
# block arms in one match; an arm nested in another arm's block, which the
# reference numbers FIRST (its lowering completes first); and a user fn that
# already holds `match_arm_0`, which the helper names step over.
_BLOCK_ARM_FNS = {
    "reproducer.rvl": (
        "fn f(r: Result[Int, Str]) -> Int {\n"
        "  return match r { Ok(v) => { var t = v\n"
        "    t }, Err(e) => 0 }\n"
        "}\n"),
    "numbering.rvl": (
        'fn match_arm_0(x: Int) -> Int { return x }\n'
        '\n'
        'fn two(r: Result[Int, Str], k: Int) -> Int {\n'
        '  return match r {\n'
        '    Ok(v) => {\n'
        '      let a = v + k\n'
        '      a\n'
        '    },\n'
        '    Err(e) => {\n'
        '      let n = e.length()\n'
        '      n + k\n'
        '    },\n'
        '  }\n'
        '}\n'
        '\n'
        'fn nest(r: Result[Int, Str], o: Result[Int, Str]) -> Int {\n'
        '  let w = 3\n'
        '  let out = match r {\n'
        '    Ok(v) => {\n'
        '      let inner = match o {\n'
        '        Ok(u) => {\n'
        '          let s = u + v\n'
        '          s * w\n'
        '        },\n'
        '        Err(e) => 0,\n'
        '      }\n'
        '      inner + 1\n'
        '    },\n'
        '    Err(e) => match_arm_0(2),\n'
        '  }\n'
        '  return out\n'
        '}\n'
        '\n'
        'test "two" {\n'
        '  assert two(Ok(1), 2) == 3\n'
        '  assert nest(Ok(1), Ok(2)) == 10\n'
        '}\n'
        ),
}


@pytest.mark.parametrize("tier", ("py", "ts"))
@pytest.mark.parametrize("name", sorted(_BLOCK_ARM_FNS))
def test_block_arm_in_a_module_fn_lifts_like_the_reference(
        compile_rvl, reference_emit, tmp_path, tier, name):
    """Issue #2317: the native chain lifts each block arm of a module `fn` into
    the reference's `match_arm_N` helper, with the same captures, numbering and
    placement, rather than lowering the whole `match` to `bad`."""
    doc = tmp_path / name
    doc.write_text(_BLOCK_ARM_FNS[name], encoding="utf-8")
    reference_ir = compile_files([str(doc)])
    assert any(f["name"].startswith("match_arm_") for f in reference_ir["functions"])
    want = reference_emit[tier](reference_ir)
    got = compile_rvl["compile_to_at"](doc.read_text(encoding="utf-8"), tier, str(doc))
    assert got == want


# Issue #2317: block arms the lift does not read the way the reference does.
# Each must come out byte-exact or LOUD (a refusal or an UNSUPPORTED node),
# never as a plausible program with a missing capture or a wrong helper.
_BLOCK_ARM_LOUD = {
    # an arm under an enclosing match's EXPRESSION arm reads that arm's `v`
    "under_expr_arm.rvl": (
        "fn f(r: Result[Int, Str], o: Result[Int, Str]) -> Int {\n"
        "  return match r { Ok(v) => match o { Ok(u) => { let s = u + v\n"
        "    s }, Err(e) => 0 }, Err(e) => 1 }\n"
        "}\n"),
    # `k` is read only inside a template
    "template_capture.rvl": (
        "fn f(r: Result[Int, Str], k: Str) -> Int {\n"
        "  return match r { Ok(v) => { let s = `a ${k}`\n"
        "    s.length() + v }, Err(e) => 0 }\n"
        "}\n"),
    # an arrow in the arm: the reference refuses `list_map` (G1)
    "arrow_in_arm.rvl": (
        "fn f(r: Result[Int, Str], xs: List[Int]) -> Int {\n"
        "  let x = 5\n"
        "  return match r { Ok(v) => { let ys = xs.map((x: Int) => x + v)\n"
        "    ys.length() }, Err(e) => x }\n"
        "}\n"),
    # a block arm in another arm's tail, which the reference numbers first
    "arm_in_tail.rvl": (
        "fn f(r: Result[Int, Str], o: Result[Int, Str]) -> Int {\n"
        "  return match r { Ok(v) => { let a = v\n"
        "    match o { Ok(u) => { let s = u + a\n"
        "      s }, Err(e) => 0 } }, Err(e) => 1 }\n"
        "}\n"),
}


@pytest.mark.parametrize("tier", ("py", "ts", "rust"))
@pytest.mark.parametrize("name", sorted(_BLOCK_ARM_LOUD))
def test_block_arm_the_lift_cannot_read_stays_loud(
        compile_rvl, reference_emit, tmp_path, tier, name):
    """Issue #2317: the self-host rule for the lift. A block arm whose captures
    the token scan cannot read exactly is not lifted; the native chain then
    agrees with the reference or says loudly that it does not."""
    doc = tmp_path / name
    doc.write_text(_BLOCK_ARM_LOUD[name], encoding="utf-8")
    try:
        want = reference_emit[tier](compile_files([str(doc)]))
    except Exception:  # the reference refuses the document
        want = None
    got = compile_rvl["compile_to_at"](doc.read_text(encoding="utf-8"), tier, str(doc))
    loud = got.startswith("REFUSED|") or "<<UNSUPPORTED-" in got
    assert got == want or loud, got


_STORE_COMPONENT = (
    "service Front { fn go() -> Str }\n\n"
    "component App requires store: Store provides front: Front {\n"
    "  provide front {\n"
    "    fn go() = store.put(%s)\n"
    "  }\n"
    "}\n")


_UNLINKABLE = {
    # The reference renames both private `h` apart (`h__m0`, `h__m1`); emitting
    # two `h` would let the document's `f` call a.rvl's.
    "private_extern_collision": ("collides", {
        "doc.rvl": (
            'use "a.rvl" { g }\n\n'
            "extern pure fn h(x: Int) -> Int = @py { return x } = @ts { return x }\n\n"
            "fn f(x: Int) -> Int { return h(g(x)) }\n"),
        "a.rvl": (
            "pub extern pure fn g(x: Int) -> Int = @py { return x } = @ts { return x }\n\n"
            "extern pure fn h(x: Int) -> Int = @py { return x + 1 } = @ts { return x + 1n }\n"),
    }),
    # Two dependencies with a private `h` each: renamed apart by the reference.
    "dependency_extern_collision": ("collides", {
        "doc.rvl": (
            'use "a.rvl" { g }\n'
            'use "b.rvl" { k }\n\n'
            "fn f(x: Int) -> Int { return g(k(x)) }\n"),
        "a.rvl": (
            "pub extern pure fn g(x: Int) -> Int = @py { return x } = @ts { return x }\n\n"
            "extern pure fn h(x: Int) -> Int = @py { return x } = @ts { return x }\n"),
        "b.rvl": (
            "pub extern pure fn k(x: Int) -> Int = @py { return x } = @ts { return x }\n\n"
            "extern pure fn h(x: Int) -> Int = @py { return x + 1 } = @ts { return x + 1n }\n"),
    }),
    # The producer writes no IR key for a `prop test`, which the reference
    # merges and the go, java, rust and wasm emitters print.
    "prop_test": ("`prop test", {
        "doc.rvl": 'use "a.rvl" { g }\n\nfn f(x: Int) -> Int { return g(x) }\n',
        "a.rvl": (
            "pub extern pure fn g(x: Int) -> Int = @py { return x } = @ts { return x }\n\n"
            'prop test "g is total" (a: Int) {\n  assert a == a\n}\n'),
    }),
    "pub_fn": ("`functions`", {
        "doc.rvl": 'use "a.rvl" { helper }\n\nfn f(x: Int) -> Int { return helper(x) }\n',
        "a.rvl": "pub fn helper(x: Int) -> Int { return x + 1 }\n",
    }),
    "type": ("`types`", {
        "doc.rvl": 'use "a.rvl" { Pt }\n\nfn f(p: Pt) -> Int { return p.x }\n',
        "a.rvl": "pub type Pt = { x: Int }\n",
    }),
    "secret": ("`secrets`", {
        "doc.rvl": 'use "a.rvl" { post }\n\nfn f(x: Int) -> Int { return x }\n',
        "a.rvl": (
            "secret api_key for net.send\n\n"
            "pub extern emission[net.send] fn post(m: Str) -> Int"
            " = @py { return len(m) + len(api_key) } = @ts { return 1n }\n"),
    }),
    "alias": ("by alias", {
        "doc.rvl": 'use "a.rvl" as m\n\nfn f(x: Int) -> Int { return m.helper(x) }\n',
        "a.rvl": "pub fn helper(x: Int) -> Int { return x + 1 }\n",
    }),
    # Issue #2308: the producer expands the alias `Al` in the service row and
    # keeps no `types` entry for it, where the reference carries `Al` itself.
    "carried_alias": ("did not lower natively", {
        "doc.rvl": 'use "lib.rvl" { Store }\n\n' + _STORE_COMPONENT % '"x"',
        "lib.rvl": (
            "pub type Cell = { b: Int }\n"
            "pub type Al = Cell\n"
            "pub service Store {\n"
            "  fn put(s: Str) -> Str\n"
            "  fn peek() -> Al\n"
            "}\n"),
    }),
    # Issue #2308: a PRIVATE alias is expanded by the producer too, in a
    # record field and in a service row. The reference keeps `id: Id`, keeps
    # `p: P` without carrying `Cell`, and carries nothing for `List[P]`.
    "private_alias_in_field": ("did not lower natively", {
        "doc.rvl": 'use "./lib.rvl" { Store }\n\n' + _STORE_COMPONENT % "[]",
        "lib.rvl": (
            "type Id = Int\n"
            "pub type Row = { id: Id }\n"
            "pub service Store { fn put(xs: List[Row]) -> Str }\n"),
    }),
    "private_alias_of_public_record": ("did not lower natively", {
        "doc.rvl": 'use "./lib.rvl" { Store }\n\n' + _STORE_COMPONENT % "[]",
        "lib.rvl": (
            "pub type Cell = { b: Int }\n"
            "type P = Cell\n"
            "pub type Row = { p: P }\n"
            "pub service Store { fn put(xs: List[Row]) -> Str }\n"),
    }),
    "private_alias_in_service_row": ("did not lower natively", {
        "doc.rvl": 'use "./lib.rvl" { Store }\n\n' + _STORE_COMPONENT % "[]",
        "lib.rvl": (
            "pub type Cell = { b: Int }\n"
            "type P = Cell\n"
            "pub service Store { fn put(xs: List[P]) -> Str }\n"),
    }),
    # A private alias declared indented, or on one line after the record
    # that names it, is a top-level declaration all the same.
    "private_alias_indented": ("did not lower natively", {
        "doc.rvl": 'use "./lib.rvl" { Store }\n\n' + _STORE_COMPONENT % "[]",
        "lib.rvl": (
            "  type Id = Int\n"
            "pub type Row = { id: Id }\n"
            "pub service Store { fn put(xs: List[Row]) -> Str }\n"),
    }),
    "private_alias_same_line": ("did not lower natively", {
        "doc.rvl": 'use "./lib.rvl" { Store }\n\n' + _STORE_COMPONENT % "[]",
        "lib.rvl": (
            "pub type Row = { id: Id }; type Id = Int\n"
            "pub service Store { fn put(xs: List[Row]) -> Str }\n"),
    }),
    # The reference carries `Row`; the native producer does not lower a
    # second declaration on one line, so the link refuses rather than drop it.
    "pub_type_same_line": ("did not lower natively", {
        "doc.rvl": 'use "./lib.rvl" { Store }\n\n' + _STORE_COMPONENT % "[]",
        "lib.rvl": (
            "type H = { h: Int }; pub type Row = { id: Int }\n"
            "pub service Store { fn put(xs: List[Row]) -> Str }\n"),
    }),
    # Issue #2308: braces and quotes inside every string form the reference
    # lexes ('...', """...""", backtick templates and their `${...}`) stay
    # inside the string, so the private alias `Id` after one is still seen and
    # the link refuses. Read as text, the `}` or `'` shifted the scan and `Row`
    # was emitted with `id: int`.
    "sq_close_brace_hides_alias": ("did not lower natively", {
        "doc.rvl": 'use "./lib.rvl" { Store }\n\n' + _STORE_COMPONENT % "[]",
        "lib.rvl": (
            "pub type Row = { id: Id }\n"
            "pub fn s() -> Str { return '}' }\n"
            "type Id = Int\n"
            "pub service Store { fn put(xs: List[Row]) -> Str }\n"),
    }),
    "sq_open_brace_hides_alias": ("did not lower natively", {
        "doc.rvl": 'use "./lib.rvl" { Store }\n\n' + _STORE_COMPONENT % "[]",
        "lib.rvl": (
            "pub fn s() -> Str { return '{' }\n"
            "pub type Row = { id: Id }\n"
            "type Id = Int\n"
            "pub service Store { fn put(xs: List[Row]) -> Str }\n"),
    }),
    "triple_quote_hides_alias": ("did not lower natively", {
        "doc.rvl": 'use "./lib.rvl" { Store }\n\n' + _STORE_COMPONENT % "[]",
        "lib.rvl": (
            'pub fn s() -> Str { return """a " { """ }\n'
            "pub type Row = { id: Id }\n"
            "type Id = Int\n"
            "pub service Store { fn put(xs: List[Row]) -> Str }\n"),
    }),
    "template_hides_alias": ("did not lower natively", {
        "doc.rvl": 'use "./lib.rvl" { Store }\n\n' + _STORE_COMPONENT % "[]",
        "lib.rvl": (
            "pub type Row = { id: Id }\n"
            "pub fn s(x: Str) -> Str { return `{ ${ x.concat(`}{`) } '` }\n"
            "type Id = Int\n"
            "pub service Store { fn put(xs: List[Row]) -> Str }\n"),
    }),
}


@pytest.mark.parametrize("case", sorted(_UNLINKABLE))
def test_native_driver_refuses_what_it_cannot_link(compile_rvl, tmp_path, case):
    """Issue #2181: where the native link would emit something other than the
    reference (two declarations under one name, or a dependency whose functions,
    types or secrets it does not carry), the driver refuses loudly with
    `REFUSED|LINK|` instead of emitting silently different source. The
    reference compiles every one of these documents."""
    why, files = _UNLINKABLE[case]
    for name, text in files.items():
        (tmp_path / name).write_text(text, encoding="utf-8")
    doc = tmp_path / "doc.rvl"
    compile_files([str(doc)])
    got = compile_rvl["compile_to_at"](doc.read_text(encoding="utf-8"), "py", str(doc))
    assert got.startswith("REFUSED|LINK|"), got[:120]
    assert why in got


_STORE_DOC = ('use "./lib.rvl" { Store }\n\n' + _STORE_COMPONENT)

# Issue #2332: case -> (the reference's parse error, files). The reference loads
# and parses every used module, at any depth, before it links anything, so each
# document below is a parse error there. The native link used to read only what
# it carries out of `lib.rvl` and emitted code for every one of them.
_MODULE_DOES_NOT_PARSE = {
    "comma_between_operations": ("expected fn, found ','", {
        "doc.rvl": _STORE_DOC % "1",
        "lib.rvl": "pub service Store { fn put(x: Int) -> Str, fn get() -> Int }\n",
    }),
    "where_after_a_record_type": ("expected a top-level declaration, found 'where'", {
        "doc.rvl": _STORE_DOC % "1",
        "lib.rvl": (
            'pub type St = { s: Str } where s != ""\n'
            "pub service Store { fn put(x: Int) -> Str }\n"),
    }),
    "a_line_that_is_no_declaration": ("expected a top-level declaration, found 'this'", {
        "doc.rvl": _STORE_DOC % "1",
        "lib.rvl": "pub service Store { fn put(x: Int) -> Str }\nthis is not revl\n",
    }),
    "block_comment_before_a_private_alias": ("expected a top-level declaration, found '/'", {
        "doc.rvl": _STORE_DOC % "[]",
        "lib.rvl": (
            "/* c */ type Id = Int\n"
            "pub type Row = { id: Id }\n"
            "pub service Store {\n  fn put(xs: List[Row]) -> Str\n}\n"),
    }),
    "block_comment_before_a_public_type": ("expected a top-level declaration, found '/'", {
        "doc.rvl": _STORE_DOC % "[]",
        "lib.rvl": (
            "/* c */ pub type Row = { id: Int }\n"
            "pub service Store {\n  fn put(xs: List[Row]) -> Str\n}\n"),
    }),
    # One `use` further down: the reference parses the module `lib.rvl` uses
    # too, though nothing of it is carried.
    "a_module_two_uses_away": ("expected a top-level declaration, found 'this'", {
        "doc.rvl": _STORE_DOC % "1",
        "lib.rvl": (
            'use "./inner.rvl" { Tool }\n\n'
            "pub service Store { fn put(x: Int) -> Str }\n"),
        "inner.rvl": "pub service Tool { fn run() -> Int }\nthis is not revl\n",
    }),
}


@pytest.mark.parametrize("tier", ("py", "go", "java", "rust"))
@pytest.mark.parametrize("case", sorted(_MODULE_DOES_NOT_PARSE))
def test_native_driver_refuses_a_used_module_that_does_not_parse(
        compile_rvl, tmp_path, case, tier):
    """Issue #2332: a used module the reference does not parse makes the native
    driver refuse with `REFUSED|BAD|<module>: ...` before it links anything, so
    a document the reference rejects is never turned into emitted code."""
    message, files = _MODULE_DOES_NOT_PARSE[case]
    for name, text in files.items():
        (tmp_path / name).write_text(text, encoding="utf-8")
    doc = tmp_path / "doc.rvl"
    with pytest.raises(RevlError, match=message):
        compile_files([str(doc)])
    got = compile_rvl["compile_to_at"](doc.read_text(encoding="utf-8"), tier, str(doc))
    assert got.startswith("REFUSED|BAD|"), got[:120]
    assert ".rvl: " in got, got[:200]


def _modules_the_reference_parses():
    from revl.parser import parse_text
    out = []
    for path in sorted((ROOT / "stdlib").glob("*.rvl")) + sorted(
            (ROOT / "src" / "revl" / "truc" / "components").glob("*.rvl")):
        text = path.read_text(encoding="utf-8")
        parse_text(text, str(path))
        out.append((path.relative_to(ROOT).as_posix(), text))
    return out


def test_native_module_admission_reads_every_stdlib_and_truc_module(compile_rvl):
    """Issue #2332, the positive control: the admission a used module now
    passes reads every stdlib module and every truc component (multi-line
    import lists, variants spread over lines, aliases, function types)
    without a refusal, so the check does not refuse what the reference
    parses on the modules documents actually import."""
    modules = _modules_the_reference_parses()
    assert len(modules) > 20
    refused = {name: why for name, text in modules
               if (why := compile_rvl["ad_module_refusal"](text)) != ""}
    assert refused == {}


# Issue #2363: case -> (the reference's refusal, files). Each document is
# refused by `compile_files`, and the native driver used to emit code for it.
# `doc.rvl` is written into `sub/`, so `use "./lib.rvl"` names a module that
# sits only in the PARENT directory: the reference resolves a `use` relative to
# the importer and then on its search path, never up through the importer's
# ancestors.
_IMPORT_REFUSED = {
    "module_only_in_the_parent_directory": ("cannot find imported module `./lib.rvl`", {
        "sub/doc.rvl": _STORE_DOC % "1",
        "lib.rvl": "pub service Store { fn put(x: Int) -> Str }\n",
    }),
    "a_name_the_module_does_not_declare": ("`Nope` is not a public declaration in", {
        "sub/doc.rvl": 'use "./lib.rvl" { Store, Nope }\n\n' + _STORE_COMPONENT % "1",
        "sub/lib.rvl": "pub service Store { fn put(x: Int) -> Str }\n",
    }),
    "a_private_fn": ("`helper` is module-private in", {
        "sub/doc.rvl": 'use "./lib.rvl" { Store, helper }\n\n' + _STORE_COMPONENT % "1",
        "sub/lib.rvl": (
            "fn helper() -> Int { return 1 }\n\n"
            "pub service Store { fn put(x: Int) -> Str }\n"),
    }),
    "a_private_type": ("`Row` is module-private in", {
        "sub/doc.rvl": 'use "./lib.rvl" { Store, Row }\n\n' + _STORE_COMPONENT % "1",
        "sub/lib.rvl": (
            "type Row = { id: Int }\n\n"
            "pub service Store { fn put(x: Int) -> Str }\n"),
    }),
    "a_private_extern": ("`raw` is module-private in", {
        "sub/doc.rvl": 'use "./lib.rvl" { Store, raw }\n\n' + _STORE_COMPONENT % "1",
        "sub/lib.rvl": (
            "extern pure fn raw(x: Int) -> Int = @py { return x }\n\n"
            "pub service Store { fn put(x: Int) -> Str }\n"),
    }),
    "a_component": ("`Impl` is not a public declaration in", {
        "sub/doc.rvl": 'use "./lib.rvl" { Store, Impl }\n\n' + _STORE_COMPONENT % "1",
        "sub/lib.rvl": (
            "pub service Store { fn put(x: Int) -> Str }\n\n"
            "component Impl provides store: Store {\n"
            "  provide store {\n"
            '    fn put(x: Int) = "a"\n'
            "  }\n"
            "}\n"),
    }),
    # The colour marker sits between `fn` and the extern's name (item 388), so
    # the private `helper` after it is still private.
    "a_private_fn_after_a_colour_polymorphic_extern": ("`helper` is module-private in", {
        "sub/doc.rvl": 'use "./lib.rvl" { Store, send, helper }\n\n' + _STORE_COMPONENT % "1",
        "sub/lib.rvl": (
            "pub extern emission fn|async send(x: Str) -> Str = @py { return x }\n\n"
            "fn helper() -> Int { return 1 }\n\n"
            "pub service Store { fn put(x: Int) -> Str }\n"),
    }),
    "an_event": ("`Placed` is module-private in", {
        "sub/doc.rvl": 'use "./lib.rvl" { Store, Placed }\n\n' + _STORE_COMPONENT % "1",
        "sub/lib.rvl": (
            "event Placed(key: id) { id: Str }\n\n"
            "pub service Store { fn put(x: Int) -> Str }\n"),
    }),
    # One `use` further down: the reference checks the names `lib.rvl` imports
    # too, though nothing of `inner.rvl` is carried.
    "a_private_fn_two_uses_away": ("`hidden` is module-private in", {
        "sub/doc.rvl": _STORE_DOC % "1",
        "sub/lib.rvl": (
            'use "./inner.rvl" { hidden }\n\n'
            "pub service Store { fn put(x: Int) -> Str }\n"),
        "sub/inner.rvl": "fn hidden() -> Int { return 1 }\n",
    }),
}


_LIB_STORE = "pub service Store { fn put(x: Int) -> Str }\n"

# Review of #2358: case -> (the reference's error, the self-host's, files). The
# reference loader refuses a `use` that resolves to nothing and an import
# cycle before it links anything; the native walk used to skip both and emit.
_MODULE_WALK_REFUSALS = {
    "missing_module_at_the_root": ("cannot find imported module", {
        "doc.rvl": 'use "./nope.rvl" { Gone }\n' + _STORE_DOC % "1",
        "lib.rvl": _LIB_STORE,
    }),
    "missing_module_nested": ("cannot find imported module", {
        "doc.rvl": _STORE_DOC % "1",
        "lib.rvl": 'use "./nope.rvl" { Gone }\n\n' + _LIB_STORE,
    }),
    "direct_import_cycle": ("import cycle", {
        "doc.rvl": _STORE_DOC % "1",
        "lib.rvl": 'use "./lib.rvl" { Store }\n\n' + _LIB_STORE,
    }),
    "indirect_import_cycle": ("import cycle", {
        "doc.rvl": _STORE_DOC % "1",
        "lib.rvl": 'use "./inner.rvl" { Tool }\n\n' + _LIB_STORE,
        "inner.rvl": ('use "./lib.rvl" { Store }\n\n'
                      "pub service Tool { fn run() -> Int }\n"),
    }),
}


@pytest.mark.parametrize("tier", ("py", "go", "java", "rust"))
@pytest.mark.parametrize("case", sorted(_IMPORT_REFUSED))
def test_native_driver_refuses_an_import_the_reference_refuses(
        compile_rvl, tmp_path, monkeypatch, case, tier):
    """Issue #2363: a `use` target the reference does not find, or an imported
    name the module does not export, makes the native driver refuse with
    `REFUSED|BAD|<importer>: line N: <the reference's message>` instead of
    emitting code for a document the reference rejects."""
    monkeypatch.delenv("REVL_IMPORT_PATH", raising=False)
    message, files = _IMPORT_REFUSED[case]
    for name, text in files.items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text(text, encoding="utf-8")
    doc = tmp_path / "sub" / "doc.rvl"
    with pytest.raises(RevlError, match=re.escape(message)):
        compile_files([str(doc)])
    got = compile_rvl["compile_to_at"](doc.read_text(encoding="utf-8"), tier, str(doc))
    assert got.startswith("REFUSED|BAD|"), got[:160]
    assert message in got, got[:300]


@pytest.mark.parametrize("tier", ("py", "rust"))
def test_native_driver_resolves_a_use_on_the_import_path(
        compile_rvl, reference_emit, tmp_path, monkeypatch, tier):
    """Issue #2363, the positive control: a module that is not beside the
    importer resolves through `REVL_IMPORT_PATH` exactly as the reference
    resolves it, and the native output is the reference's bytes."""
    (tmp_path / "lib").mkdir()
    (tmp_path / "lib" / "store.rvl").write_text(
        "pub service Store { fn put(x: Int) -> Str }\n", encoding="utf-8")
    (tmp_path / "app").mkdir()
    doc = tmp_path / "app" / "doc.rvl"
    doc.write_text('use "store.rvl" { Store }\n\n' + _STORE_COMPONENT % "1",
                   encoding="utf-8")
    monkeypatch.setenv("REVL_IMPORT_PATH", str(tmp_path / "lib"))
    want = reference_emit[tier](compile_files([str(doc)]))
    got = compile_rvl["compile_to_at"](doc.read_text(encoding="utf-8"), tier, str(doc))
    assert got == want


def test_native_import_check_passes_every_stdlib_and_truc_module(compile_rvl):
    """Issue #2363, the positive control: every `use` of every stdlib module
    and truc component resolves and names only what its module exports, so the
    check does not refuse an import the reference accepts."""
    modules = _modules_the_reference_parses()
    refused = {name: why for name, text in modules
               if (why := compile_rvl["ad_imports_refusal"](text, str(ROOT / name))) != ""}
    assert refused == {}


@pytest.mark.parametrize("case", sorted(_MODULE_WALK_REFUSALS))
def test_native_driver_refuses_a_missing_module_and_an_import_cycle(
        compile_rvl, tmp_path, case):
    """A `use` that resolves to nothing, at the document or in a used module,
    and an import cycle, direct or through another module, refuse with the
    reference loader's reason instead of emitting code."""
    message, files = _MODULE_WALK_REFUSALS[case]
    for name, text in files.items():
        (tmp_path / name).write_text(text, encoding="utf-8")
    doc = tmp_path / "doc.rvl"
    with pytest.raises(RevlError, match=message):
        compile_files([str(doc)])
    got = compile_rvl["compile_to_at"](doc.read_text(encoding="utf-8"), "py", str(doc))
    assert got.startswith("REFUSED|BAD|"), got[:160]
    assert message in got, got[:200]


def test_a_used_module_refusal_names_its_line(compile_rvl):
    """A program-grammar refusal of a used module says which line it is about."""
    text = "pub service Store { fn put(x: Int) -> Str }\nthis is not revl\n"
    assert compile_rvl["ad_module_refusal"](text).startswith("line 2: ")


def test_an_expression_bodied_fn_is_named_unsupported_not_malformed(compile_rvl, tmp_path):
    """`fn f(x: Int) -> Int = x` is valid revl, so a used module that declares
    one is refused as unsupported by the self-host, not as a user error."""
    (tmp_path / "doc.rvl").write_text(_STORE_DOC % "1", encoding="utf-8")
    (tmp_path / "lib.rvl").write_text(
        "pub fn twice(x: Int) -> Int = x * 2\n\n" + _LIB_STORE, encoding="utf-8")
    doc = tmp_path / "doc.rvl"
    compile_files([str(doc)])
    got = compile_rvl["compile_to_at"](doc.read_text(encoding="utf-8"), "py", str(doc))
    assert got.startswith("REFUSED|BAD|"), got[:160]
    assert "line 1: an expression-bodied fn" in got, got[:200]
    assert "not supported by the self-host" in got
    assert "expected {" not in got


def test_a_malformed_fn_before_an_expression_bodied_one_keeps_the_plain_refusal(compile_rvl):
    """The expression-fn message is given only when that fn is the one the
    program grammar refuses: a malformed fn earlier in the module keeps the
    plain refusal, at its own line."""
    text = ("pub fn broken(x: Int) -> Int\n\n"
            "pub fn twice(x: Int) -> Int = x * 2\n\n" + _LIB_STORE)
    assert compile_rvl["ad_module_refusal"](text) == "line 1: expected { after fn signature"


# A diamond (doc -> a, b -> c): `c.rvl` is read once, it is no cycle, and the
# link carries every extern the way `compile_files` merges them.
_DIAMOND = {
    "doc.rvl": ('use "./a.rvl" { a_one }\nuse "./b.rvl" { b_one }\n\n'
                "extern pure fn d_own(x: Int) -> Int = @py { return x }\n"),
    "a.rvl": ('use "./c.rvl" { c_one }\n\n'
              "pub extern pure fn a_one(x: Int) -> Int = @py { return x + 1 }\n"),
    "b.rvl": ('use "./c.rvl" { c_one }\n\n'
              "pub extern pure fn b_one(x: Int) -> Int = @py { return x + 2 }\n"),
    "c.rvl": "pub extern pure fn c_one(x: Int) -> Int = @py { return x * 3 }\n",
}


def test_native_driver_compiles_a_diamond_import_like_the_reference(
        compile_rvl, reference_emit, tmp_path):
    """The positive control of the depth-first walk: a module two imports
    reach is no cycle, and the document compiles as the reference compiles it."""
    for name, text in _DIAMOND.items():
        (tmp_path / name).write_text(text, encoding="utf-8")
    doc = tmp_path / "doc.rvl"
    want = reference_emit["py"](compile_files([str(doc)]))
    got = compile_rvl["compile_to_at"](doc.read_text(encoding="utf-8"), "py", str(doc))
    assert got == want


# Issue #2343: the whitespace the reference lexer skips between an `@backend`
# name and its `{` is Python's `str.isspace`, newlines included in any order.
_HOST_GAPS = {"space": " ", "form_feed": "\x0c", "vertical_tab": "\x0b",
              "file_separator": "\x1c", "nbsp": " ", "newline_then_space": "\n "}


@pytest.mark.parametrize("gap", sorted(_HOST_GAPS))
def test_native_compile_reads_any_isspace_gap_before_a_host_body(
        compile_to, reference_emit, gap):
    """Issue #2343, single file: `@py<gap>{ ... }` is a host body for the
    reference whatever `str.isspace` character the gap holds, and the native
    compile is the reference's, byte for byte."""
    src = "extern pure fn f() -> Int = @py" + _HOST_GAPS[gap] + "{ return 1 }\n"
    assert compile_to(src, "py") == reference_emit["py"](compile_source(src))


@pytest.mark.parametrize("tier", ("py", "go"))
@pytest.mark.parametrize("gap", sorted(_HOST_GAPS))
def test_native_link_reads_any_isspace_gap_before_a_host_body(
        compile_rvl, reference_emit, tmp_path, gap, tier):
    """Issue #2343, across a `use`: when the gap after `@py` was not read as
    whitespace, the backtick in the host comment opened a revl template that
    ran to the one on the `//` line and hid `Row`, so the native link emitted
    ir_version 1 without the type the reference carries."""
    (tmp_path / "doc.rvl").write_text(_STORE_DOC % "[]", encoding="utf-8")
    (tmp_path / "lib.rvl").write_text(
        "pub extern pure fn e(x: Int) -> Int = @py" + _HOST_GAPS[gap]
        + "{\n    return x  # `\n}\n"
        "pub type Row = { id: Int }\n"
        "// `\n"
        "pub service Store { fn put(xs: List[Row]) -> Str }\n", encoding="utf-8")
    doc = tmp_path / "doc.rvl"
    reference_ir = compile_files([str(doc)])
    assert "Row" in reference_ir["types"]
    got = compile_rvl["compile_to_at"](doc.read_text(encoding="utf-8"), tier, str(doc))
    assert got == reference_emit[tier](reference_ir), got[:200]


# case -> (tiers, files). The tiers are those whose native emitter covers the
# document once it is linked: wasm's emits no component yet, and only py and ts
# bodies are written for the extern case.
_LINKED_AS_REFERENCE = {
    # The issue's reproducer: a service-only import brings the record type its
    # operation names, so the reference IR has a `types` table and is v3.
    "service_record": (("py", "ts", "go", "java", "rust"), {
        "doc.rvl": 'use "./lib.rvl" { Store }\n\n'
                   + _STORE_COMPONENT % '{ ref: "a", qty: 1 }',
        "lib.rvl": (
            "pub type Item = { ref: Str, qty: Int }\n"
            "pub service Store { fn put(row: Item) -> Str }\n"),
    }),
    # Carried transitively through a field and a case payload, in the module's
    # declaration order (not discovery order), after the document's own type;
    # a private type and a public one no operation reaches stay behind.
    "service_closure": (("py", "ts", "go", "java", "rust"), {
        # (The call names no carried type: the native gate admits one document
        # and does not see the module's `Plain`.)
        "doc.rvl": 'use "./lib.rvl" { Store }\n\ntype Own = { o: Int }\n\n'
                   + _STORE_COMPONENT % "[]",
        "lib.rvl": (
            "pub type Row = { tag: Tag, n: Int }\n"
            "type Hidden = { h: Int }\n"
            "pub type Unused = { u: Int }\n"
            "pub type Tag = Plain | Marked(Cell)\n"
            "pub type Cell = { b: Int }\n"
            "pub service Store {\n"
            "  fn put(rows: List[Row]) -> Str\n"
            "  fn count() -> Int\n"
            "}\n"),
    }),
    # `pub` and `type` may be separated by any run of spaces or tabs.
    "service_record_tabbed": (("py", "ts", "go", "java", "rust"), {
        "doc.rvl": 'use "./lib.rvl" { Store }\n\n'
                   + _STORE_COMPONENT % '{ ref: "a", qty: 1 }',
        "lib.rvl": (
            "pub\ttype  Item = { ref: Str, qty: Int }\n"
            "pub service Store { fn put(row: Item) -> Str }\n"),
    }),
    # A `pub type` declared indented is carried like any other.
    "pub_type_indented": (("py", "ts", "go", "java", "rust"), {
        "doc.rvl": 'use "./lib.rvl" { Store }\n\n' + _STORE_COMPONENT % "[]",
        "lib.rvl": (
            "  pub type Row = { id: Int }\n"
            "pub service Store { fn put(xs: List[Row]) -> Str }\n"),
    }),
    # Issue #2308: a `pub type` after a string holding a brace or a quote is
    # carried; read as text, the opener left the scan inside a string and the
    # native IR had no `Row` (ir_version 1). One case per string form the
    # reference lexes, with the other quote kinds and escapes at the end.
    "sq_string_before_record": (("py", "go", "java", "rust"), {
        "doc.rvl": 'use "./lib.rvl" { Store }\n\n' + _STORE_COMPONENT % "[]",
        "lib.rvl": (
            "pub fn s() -> Str { return '\"' }\n"
            "pub fn t() -> Str { return 'a \" ` \"\"\" {\\\\' }\n"
            "pub type Row = { id: Int }\n"
            "pub service Store { fn put(xs: List[Row]) -> Str }\n"),
    }),
    "dq_and_triple_before_record": (("py", "go", "java", "rust"), {
        "doc.rvl": 'use "./lib.rvl" { Store }\n\n' + _STORE_COMPONENT % "[]",
        "lib.rvl": (
            "pub fn s() -> Str { return \"a ' ` {\\\\\" }\n"
            'pub fn t() -> Str { return """a \' " ` } """ }\n'
            "pub type Row = { id: Int }\n"
            "pub service Store { fn put(xs: List[Row]) -> Str }\n"),
    }),
    "template_before_record": (("py", "go", "java", "rust"), {
        "doc.rvl": 'use "./lib.rvl" { Store }\n\n' + _STORE_COMPONENT % "[]",
        "lib.rvl": (
            "pub fn s(m: Map[Str, Str]) -> Str { return `a ${ m.get(\"}\") } b` }\n"
            "pub fn t(x: Str) -> Str { return `{ ${ x.concat(`}`) } \" '` }\n"
            "pub type Row = { id: Int }\n"
            "pub service Store { fn put(xs: List[Row]) -> Str }\n"),
    }),
    # Issue #2374: the reference renames the document's private `Item` apart
    # (`Item__m0`) from the `Item` the imported service carries, and so does
    # the native link (issue #2308 refused it).
    "carried_type_collision": (("py", "ts", "go", "java", "rust"), {
        "doc.rvl": (
            'use "lib.rvl" { Store }\n\n'
            "type Item = { name: Str }\n\n"
            "service Front { fn go() -> Str }\n\n"
            "component App requires store: Store provides front: Front {\n"
            "  provide front {\n"
            '    fn go() = store.put({ ref: "a", qty: 1 })\n'
            "  }\n"
            "}\n"),
        "lib.rvl": (
            "pub type Item = { ref: Str, qty: Int }\n"
            "pub service Store { fn put(row: Item) -> Str }\n"),
    }),
    # A pure dependency whose extern bodies hold column-0 host lines: they are
    # inside the declaration, not top-level declarations of their own.
    "column0_extern_body": (("py", "ts"), {
        "doc.rvl": 'use "a.rvl" { g }\n\nfn f(x: Int) -> Int { return g(x) }\n',
        "a.rvl": (
            "pub extern pure fn g(x: Int) -> Int = @py {\n"
            "return x\n"
            "} = @ts {\n"
            "return x\n"
            "}\n"),
    }),
}


def _linked_cases():
    return [(case, tier) for case, (tiers, _files) in sorted(_LINKED_AS_REFERENCE.items())
            for tier in tiers]


@pytest.mark.parametrize("case, tier", _linked_cases())
def test_native_driver_links_as_the_reference_does(
        compile_rvl, reference_emit, tmp_path, case, tier):
    """Issue #2308: a service imported on its own carries the public record and
    variant types its operations name, closed over their fields and cases (the
    reference's `carried` list, issue #1899), and the IR that gains them is v3.
    A pure dependency whose extern body holds a column-0 host line links too.
    The native output is the reference's, byte for byte."""
    for name, text in _LINKED_AS_REFERENCE[case][1].items():
        (tmp_path / name).write_text(text, encoding="utf-8")
    doc = tmp_path / "doc.rvl"
    want = reference_emit[tier](compile_files([str(doc)]))
    got = compile_rvl["compile_to_at"](doc.read_text(encoding="utf-8"), tier, str(doc))
    assert got == want, got[:200]


@pytest.mark.parametrize("tier", ("py", "go", "java", "rust"))
def test_native_driver_links_the_truc_cli_as_the_reference_does(
        compile_rvl, reference_emit, tier):
    """Issue #2308: `src/revl/truc/components/cli.rvl` imports the services
    `Assembler` and `Shipper` on their own and links byte for byte today. The
    carried-type refusals must not start refusing it."""
    doc = ROOT / "src" / "revl" / "truc" / "components" / "cli.rvl"
    want = reference_emit[tier](compile_files([str(doc)]))
    got = compile_rvl["compile_to_at"](doc.read_text(encoding="utf-8"), tier, str(doc))
    assert got == want, got[:200]


# Issue #2329: a document's call to an operation of a service it imports, the
# module that declares the service, and what the native chain must give:
# "T1", the reference checker's own message as a native gate refusal; "LINK", a
# `REFUSED|LINK|` refusal that starts with the given text; "emit", the
# reference's bytes. The reference checker types the call against the module's
# declaration; the native gate used to read only the document.
_IMPORTED_CALLS = {
    # The issue's reproducer. The reference refuses a `Str` (and an `Int`) for
    # the private alias `Id`, which is out of the document's scope. Natively the
    # type link (issue #2308) refuses the module first, since the producer keeps
    # no `types` entry for an alias, so the gate never types this call: these
    # two pin that the alias stays refused by that link.
    "private_alias_str": ("type Id = Int\npub service Store { fn put(id: Id) -> Str }\n",
                          'store.put("x")', "LINK|type `Id`"),
    "private_alias_int": ("type Id = Int\npub service Store { fn put(id: Id) -> Str }\n",
                          "store.put(1)", "LINK|type `Id`"),
    # The gap itself: built-in and carried parameter types the gate types.
    "int_given_str": ("pub service Store { fn put(id: Int) -> Str }\n", 'store.put("x")',
                      "T1"),
    "list_given_wrong_items": ("pub service Store { fn put(xs: List[Int]) -> Str }\n",
                               'store.put(["a"])', "T1"),
    "record_given_int": ("pub type Row = { id: Int }\n"
                         "pub service Store { fn put(r: Row) -> Str }\n", "store.put(1)",
                         "T1"),
    # A brace in a comment ahead of the declaration does not hide it: a scan
    # that read the comment would stop at its `}` and refuse with LINK.
    "after_comment_brace": ("// } service Store {\n"
                            "pub service Store { fn put(id: Int) -> Str }\n",
                            'store.put("x")', "T1"),
    # Positive controls: the reference admits these, and the native output
    # must stay its bytes.
    "after_comment_brace_ok": ("// } service Store {\n"
                               "pub service Store { fn put(id: Int) -> Str }\n",
                               "store.put(1)", "emit"),
    "int_given_int": ("pub service Store { fn put(id: Int) -> Str }\n", "store.put(1)",
                      "emit"),
    "list_given_list": ("pub service Store { fn put(xs: List[Int]) -> Str }\n",
                        "store.put([1, 2])", "emit"),
    "record_given_record": ("pub type Row = { id: Int }\n"
                            "pub service Store { fn put(r: Row) -> Str }\n",
                            "store.put({ id: 1 })", "emit"),
}


def _write_imported_call(tmp_path, case):
    lib, call, _expect = _IMPORTED_CALLS[case]
    (tmp_path / "lib.rvl").write_text(lib, encoding="utf-8")
    doc = tmp_path / "doc.rvl"
    doc.write_text('use "./lib.rvl" { Store }\n\n'
                   + _STORE_COMPONENT.replace("store.put(%s)", call), encoding="utf-8")
    return doc


@pytest.mark.parametrize("case", sorted(_IMPORTED_CALLS))
def test_native_driver_types_calls_to_an_imported_service(
        compile_rvl, reference_emit, tmp_path, case):
    """Issue #2329: where the reference refuses a call to an imported
    service's operation, the native chain refuses too and never emits, with
    the reference's own message where the native gate is what refuses. Where
    the reference admits, the native output is its bytes."""
    expect = _IMPORTED_CALLS[case][2]
    doc = _write_imported_call(tmp_path, case)
    got = compile_rvl["compile_to_at"](doc.read_text(encoding="utf-8"), "py", str(doc))
    if expect == "emit":
        assert got == reference_emit["py"](compile_files([str(doc)])), got[:200]
        return
    with pytest.raises(RevlError) as refused:
        compile_files([str(doc)])
    if expect == "T1":
        assert got == "REFUSED|T1|" + refused.value.message, got[:200]
    else:
        assert got.startswith("REFUSED|" + expect), got[:200]


# Issue #2362: three call-site rules the native gate did not decide, in a
# single document and through an imported service. Each case is (the module's
# declarations, the provide method after `fn go()`, the outcome): "T1" is the
# reference's own message as a native T1 refusal, "emit" the reference's bytes.
_CALL_SITE_RULES = {
    "arity_extra": ("pub service Store { fn put(id: Int) -> Str }\n",
                    "= store.put(1, 2)", "T1"),
    "arity_missing": ("pub service Store { fn put(id: Int) -> Str }\n",
                      "= store.put()", "T1"),
    "arity_in_let": ("pub service Store { fn put(id: Int) -> Str }\n",
                     "{\n      let x = store.put(1, 2)\n      return x\n    }", "T1"),
    "return_arrow": ("pub service Store { fn put(id: Int) -> Int }\n",
                     "= store.put(1)", "T1"),
    "return_block": ("pub service Store { fn put(id: Int) -> Int }\n",
                     "{\n      return store.put(1)\n    }", "T1"),
    "return_literal_arrow": ("pub service Store { fn put(id: Int) -> Int }\n",
                             "= 1", "T1"),
    "argument_in_return": ("pub service Store { fn put(id: Int) -> Str }\n",
                           '{\n      return store.put("x")\n    }', "T1"),
    "record_field_type": ("pub type Row = { id: Int }\n"
                          "pub service Store { fn put(r: Row) -> Str }\n",
                          '= store.put({ id: "x" })', "T1"),
    "record_extra_field": ("pub type Row = { id: Int }\n"
                           "pub service Store { fn put(r: Row) -> Str }\n",
                           "= store.put({ id: 1, b: 2 })", "T1"),
    # the requirement call is decided before the return's own check
    "arity_under_compare": ("pub service Store { fn put(id: Int) -> Int }\n",
                            "= store.put(1, 2) == 1", "T1"),
    "argument_in_list": ("pub service Store { fn put(id: Int) -> Str }\n",
                         '{\n      return [store.put("x")]\n    }', "T1"),
    # a nested record field is held field-wise too
    "record_nested_field": ("pub type In = { a: Int }\npub type Row = { i: In }\n"
                            "pub service Store { fn put(r: Row) -> Str }\n",
                            '= store.put({ i: { a: "x" } })', "T1"),
    "record_list_field": ("pub type In = { a: Int }\npub type Row = { i: List[In] }\n"
                          "pub service Store { fn put(r: Row) -> Str }\n",
                          '= store.put({ i: [{ a: "x" }] })', "T1"),
    "record_opt_field": ("pub type In = { a: Int }\npub type Row = { i: Opt[In] }\n"
                         "pub service Store { fn put(r: Row) -> Str }\n",
                         '= store.put({ i: { a: "x" } })', "T1"),
    "record_opt_param": ("pub type Row = { id: Int }\n"
                         "pub service Store { fn put(r: Opt[Row]) -> Str }\n",
                         '= store.put({ id: "x" })', "T1"),
    "record_list_param": ("pub type Row = { id: Int }\n"
                          "pub service Store { fn put(r: List[Row]) -> Str }\n",
                          '= store.put([{ id: "x" }])', "T1"),
    "record_deep_field": ("pub type R0 = { a: R1 }\npub type R1 = { a: R2 }\npub type R2 = { a: R3 }\npub type R3 = { a: R4 }\npub type R4 = { a: R5 }\npub type R5 = { a: Int }\npub service Store { fn put(r: R0) -> Str }\n",
                          '= store.put({ a: { a: { a: { a: { a: { a: "x" } } } } } })', "T1"),
    # a requirement call inside a larger expression is typed at its return
    "nested_call_operand": ("pub service Store { fn put(id: Int) -> Int }\n",
                            "= store.put(1) + 1", "T1"),
    "nested_call_in_let": ("pub service Store { fn put(id: Int) -> Int }\n",
                           "{\n      let n = store.put(1) + 1\n      return n\n    }", "T1"),
    # a record literal for a variant or a generic record parameter
    "variant_param": ("pub type Shape = Dot | Box(Int)\n"
                      "pub service Store { fn put(r: Shape) -> Str }\n",
                      "= store.put({ a: 1 })", "T1"),
    "generic_param": ("pub type Cell[T] = { v: T }\n"
                      "pub service Store { fn put(r: Cell[Int]) -> Str }\n",
                      '= store.put({ v: "x" })', "T1"),
    # Positive controls: admitted by the reference, and by the native gate.
    "record_nested_matches": ("pub type In = { a: Int }\npub type Row = { i: In }\n"
                              "pub service Store { fn put(r: Row) -> Str }\n",
                              "= store.put({ i: { a: 1 } })", "emit"),
    "record_container_matches": ("pub type In = { a: Int }\n"
                                 "pub type Row = { i: Opt[In], j: List[In] }\n"
                                 "pub service Store { fn put(r: Row) -> Str }\n",
                                 "= store.put({ i: { a: 1 }, j: [{ a: 2 }] })", "emit"),
    "record_opt_param_matches": ("pub type Row = { id: Int }\n"
                                 "pub service Store { fn put(r: Opt[Row]) -> Str }\n",
                                 "= store.put({ id: 1 })", "emit"),
    "record_list_param_matches": ("pub type Row = { id: Int }\n"
                                  "pub service Store { fn put(r: List[Row]) -> Str }\n",
                                  "= store.put([{ id: 1 }])", "emit"),
    "nested_call_int_return": ("pub service Store { fn put(id: Int) -> Int }\n",
                               "= (store.put(1) + 1).to_str()", "emit"),
    "nested_call_matches": ("pub service Store { fn put(id: Int) -> Str }\n",
                            '= store.put(1).concat("x")', "emit"),
    "return_matches": ("pub service Store { fn put(id: Int) -> Str }\n",
                       "= store.put(1)", "emit"),
    "record_matches": ("pub type Row = { id: Int, n: Str }\n"
                       "pub service Store { fn put(r: Row) -> Str }\n",
                       '= store.put({ n: "a", id: 1 })', "emit"),
}

# In one document the native gate does not type a record literal against a
# generic record parameter; it refuses by name instead of emitting.
_CALL_SITE_NAMED_IN_ONE_DOC = {
    "generic_param": "TYPE|`store.put` argument `r`",
    "record_deep_field": "TYPE|`store.put` argument `r`: a record literal nested",
}

# Through an import these refuse by name where the reference has an answer:
# a literal deeper than the shape expansion, and an operator whose operand
# type only the imported declaration gives (the IR would lack `operands`).
_CALL_SITE_NAMED_THROUGH_IMPORT = {
    "record_deep_field": "REFUSED|TYPE|`store.put` argument `r`: a record literal nested",
    "nested_call_int_return": "REFUSED|LINK|an operator on an imported service call",
}

_CALL_SITE_COMPONENT = (
    "service Front { fn go() -> Str }\n\n"
    "component App requires store: Store provides front: Front {\n"
    "  provide front {\n"
    "    fn go() %s\n"
    "  }\n"
    "}\n")


def _reference_message(compile_one):
    try:
        compile_one()
    except RevlError as e:
        return e.message
    return ""


@pytest.mark.parametrize("case", sorted(_CALL_SITE_RULES))
def test_native_gate_decides_the_call_site_rules_in_one_document(
        admit, compile_rvl, reference_emit, case):
    """Issue #2362, single document: the service declared in the same text."""
    decls, body, expect = _CALL_SITE_RULES[case]
    src = decls + "\n" + _CALL_SITE_COMPONENT % body
    want = _reference_message(lambda: compile_source(src, "doc.rvl"))
    if expect == "emit":
        assert want == ""
        assert admit(src) == ""
        got = compile_rvl["compile_to"](src, "py")
        assert got == reference_emit["py"](compile_source(src, "doc.rvl")), got[:200]
    elif case in _CALL_SITE_NAMED_IN_ONE_DOC:
        assert want != ""
        assert admit(src).startswith(_CALL_SITE_NAMED_IN_ONE_DOC[case])
    else:
        assert want != ""
        assert admit(src) == "T1|" + want


@pytest.mark.parametrize("case", sorted(_CALL_SITE_RULES))
def test_native_driver_decides_the_call_site_rules_through_an_import(
        compile_rvl, reference_emit, tmp_path, case):
    """Issue #2362, imported: the service and the record it carries live in
    the module the document imports the service from."""
    decls, body, expect = _CALL_SITE_RULES[case]
    (tmp_path / "lib.rvl").write_text(decls, encoding="utf-8")
    doc = tmp_path / "doc.rvl"
    doc.write_text('use "./lib.rvl" { Store }\n\n' + _CALL_SITE_COMPONENT % body,
                   encoding="utf-8")
    got = compile_rvl["compile_to_at"](doc.read_text(encoding="utf-8"), "py", str(doc))
    if case in _CALL_SITE_NAMED_THROUGH_IMPORT:
        assert got.startswith(_CALL_SITE_NAMED_THROUGH_IMPORT[case]), got[:200]
    elif expect == "emit":
        assert got == reference_emit["py"](compile_files([str(doc)])), got[:200]
    else:
        want = _reference_message(lambda: compile_files([str(doc)]))
        assert want != ""
        assert got == "REFUSED|T1|" + want, got[:200]


# Issue #2374: types carried through an import that the native chain refused
# where the reference admits. Each case is (the module, the document's own
# declarations, the provide method after `fn go()`, the outcome): "emit" is the
# reference's bytes on every tier, "refuse" a native refusal where the
# reference refuses too (the negative controls: no new silent admit), "link" a
# loud `REFUSED|LINK|` where the reference admits but the native link cannot
# reproduce its bytes.
_CARRIED_LIB = (
    "pub type In = { a: Int }\n"
    "pub type Row = { i: In }\n"
    "pub type Shape = Dot | Box(Int)\n"
    "type Priv = P1 | P2\n"
    "pub type Lone = L1 | L2\n"
    "pub service Store {\n"
    "  fn put(r: Row) -> Str\n"
    "  fn draw(s: Shape) -> Str\n"
    "}\n")

_CARRIED_THROUGH_IMPORT = {
    # 1. a variant case carried from the service's module was refused as G1
    "case_without_payload": ("", "= store.draw(Dot)", "emit"),
    "case_with_payload": ("", "= store.draw(Box(1))", "emit"),
    "case_in_let": ("", "{\n      let s = Box(2)\n      return store.draw(s)\n    }",
                    "emit"),
    "case_unknown": ("", "= store.draw(Circle)", "refuse"),
    "case_of_private_type": ("", "= store.draw(P1)", "refuse"),
    "case_of_uncarried_type": ("", "= store.draw(L1)", "refuse"),
    # 2. a private document type named like a carried one was refused as LINK;
    # the reference renames it apart (`In__m0`)
    "own_type_carried_transitively": (
        "type In = { z: Str }\n\nfn mk() -> In { return { z: \"q\" } }\n\n",
        "= store.put({ i: { a: 1 } })", "emit"),
    "own_type_carried_directly": (
        "type Row = { z: Str }\n\nfn mk() -> List[Row] { return [{ z: \"q\" }] }\n\n",
        "= store.put({ i: { a: 1 } })", "emit"),
    "own_type_and_carried_case": (
        "type In = { z: Str }\n\n", "= store.draw(Box(mk_n()))", "emit"),
    "own_public_type": ("pub type In = { z: Str }\n\n", "= store.put({ i: { a: 1 } })",
                        "refuse"),
    # The reference renames a private type in functions, types and test bodies,
    # never in a service or a component, so the document's `Other.get` keeps
    # `In` there. The native link does not rename such a type: it refuses.
    "own_type_in_service_signature": (
        "type In = { z: Str }\n\nservice Other { fn get() -> In }\n\n",
        "= store.put({ i: { a: 1 } })", "link"),
    "own_type_wrong_literal": ("type In = { z: Str }\n\n",
                               '= store.put({ i: { z: "x" } })', "refuse"),
}


def _write_carried_case(tmp_path, case):
    own, body, _expect = _CARRIED_THROUGH_IMPORT[case]
    if "mk_n()" in body:
        own += "fn mk_n() -> Int { return 1 }\n\n"
    (tmp_path / "lib.rvl").write_text(_CARRIED_LIB, encoding="utf-8")
    doc = tmp_path / "doc.rvl"
    doc.write_text('use "./lib.rvl" { Store }\n\n' + own + _CALL_SITE_COMPONENT % body,
                   encoding="utf-8")
    return doc


# The go and java emitters do not cover a `let` in a provide body, in one
# document either, so that case is compared on the other tiers.
_CARRIED_TIERS = {"case_in_let": ("py", "ts", "rust")}


def _carried_cases():
    return [(case, tier) for case, (_o, _b, expect) in sorted(_CARRIED_THROUGH_IMPORT.items())
            for tier in _CARRIED_TIERS.get(
                case, ("py", "ts", "go", "java", "rust") if expect != "refuse" else ("py",))]


@pytest.mark.parametrize("case, tier", _carried_cases())
def test_native_driver_admits_types_carried_through_an_import(
        compile_rvl, reference_emit, tmp_path, case, tier):
    """Issue #2374: a variant case carried from an imported service's module,
    and a private document type named like a carried one, are admitted as the
    reference admits them, byte for byte. Where the reference refuses, the
    native chain refuses too."""
    doc = _write_carried_case(tmp_path, case)
    got = compile_rvl["compile_to_at"](doc.read_text(encoding="utf-8"), tier, str(doc))
    if _CARRIED_THROUGH_IMPORT[case][2] == "emit":
        assert got == reference_emit[tier](compile_files([str(doc)])), got[:200]
        return
    if _CARRIED_THROUGH_IMPORT[case][2] == "link":
        compile_files([str(doc)])
        assert got.startswith("REFUSED|LINK|"), got[:200]
        return
    with pytest.raises(RevlError):
        compile_files([str(doc)])
    assert got.startswith("REFUSED|"), got[:200]


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
    # `selfhost/lower.rvl`'s extern grammar now knows every extern classification
    # the reference does: `pure`/`acquire`/`emission` (reserved keywords) and the
    # contextual `witnessed` class (item 243), each with an optional `[caps]`
    # capability scope (item 343) and `(confined: p)` reach clause (item 373).
    # The `witnessed.rvl` false refusal (`BAD|expected fn after extern`) is CLOSED
    # — the gate admits it, and its `lower_to_ir` externs section was already
    # byte-exact (tests/test_selfhost_lower_ir.py's EXTERN_DECL_GAP is empty).
    #
    # The table is EMPTY, and that is the ratchet having worked rather than a
    # list nobody maintained: both entries it ever held closed by returning "",
    # and a closed entry is DELETED rather than reworded.
    #
    # The stream entry (`emit_rust_corpus/comp_stream.rvl`, pinned to
    # `G1|`sub` is not a declared requirement of Parked`) was the second of the
    # two, and the gap is worth recording because it was never the document's.
    # The REFERENCE admitted it throughout; `selfhost/lower.rvl` did not put a
    # `subscribe` acquisition's bind into the component scope it resolves call
    # heads against (`call_head_declared` reads `cx.scopeNames`), so the later
    # `sub.next()` read as an undeclared access and drew the shared G1
    # diagnostic, and it refused the tree's existing stream scenario
    # `backends/rust/scenarios/stream.rvl` with the identical verdict. That is
    # what made it a lower.rvl gap which the first stream document in the emit
    # corpus merely made visible (issue 1153) rather than one the document
    # introduced. The item-130 stream statement forms closed it (#1139, whose
    # own title records these false-reject/G1 verdicts to zero), the gate now
    # returns "" here, and the document's residual moved to the byte comparison
    # `LOWER_GAP_DOCS` above rather than leaving the corpus: the rust emit
    # oracle holds it byte-exact against the reference, stream runtime and all.
NATIVE_GATE_GAPS: dict[str, str] = {}


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


# Issue #2402: the reference checker now builds and matches values of a user
# generic variant (`type Mb[T] = Jj(T) | Nn`); `selfhost/lower.rvl` does not
# yet. The documents are the reference's own test table,
# tests/test_generic_variant_construction.py, and each entry records what the
# native chain gives for it on py, so the difference is NAMED rather than
# passing for agreement. Same ratchet as NATIVE_GATE_GAPS: the day the
# self-host follows the reference, these tests fail on the stale entry, and
# the entry is DELETED rather than reworded.
#
# The reference ADMITS these and the native gate refuses them loudly, with the
# message the reference gave before the fix (the bare head `Mb`).
GENERIC_VARIANT_LOUD_REFUSALS: dict[str, str] = {
    "argument": "REFUSED|T1|argument 1 of `un(...)` expects `Mb[Int]`, got `Mb`",
    "generic_fn": "REFUSED|T1|this function's return expects `Mb[T]`, got `Mb`",
    "inferred_let": "REFUSED|T1|this function's return expects `Mb[Int]`, got `Mb`",
    "issue_let": "REFUSED|T1|`let v: Mb[Int]` expects `Mb[Int]`, got `Mb`",
    "issue_nullary": "REFUSED|T1|`let v: Mb[Int]` expects `Mb[Int]`, got `Mb`",
    "issue_return": "REFUSED|T1|this function's return expects `Mb[Int]`, got `Mb`",
    "list_payload": "REFUSED|T1|this function's return expects `Bag[Int]`, got `Bag`",
    "nested": "REFUSED|T1|this function's return expects `Mb[Mb[Int]]`, got `Mb`",
    "nullary_without_expectation":
        "REFUSED|T1|this function's return expects `Mb[Int]`, got `Mb`",
    "two_parameters":
        "REFUSED|T1|this function's return expects `Ei[Int, Str]`, got `Ei`",
}

# The reference REFUSES these (an arm over `Mb[Int]` binds `Int`, so returning
# it as `Str` is a mismatch) and the native chain still ADMITS and emits them:
# a silent verdict difference, recorded so it cannot read as agreement. Both
# were admitted by the reference too before the fix.
GENERIC_VARIANT_SILENT_ADMITS = ("arm_binds_second_parameter",
                                 "arm_binds_substituted_payload")


def _generic_variant_docs():
    return load_by_path(
        "test_generic_variant_construction",
        ROOT / "tests" / "test_generic_variant_construction.py")


@pytest.mark.parametrize("name", sorted(GENERIC_VARIANT_LOUD_REFUSALS))
def test_generic_variant_program_is_refused_loudly_by_the_native_chain(
        compile_to, name):
    """Issue #2402: the reference admits it, the native chain refuses it with
    the recorded REFUSED verdict. A change in either answer fails here."""
    source = _generic_variant_docs().ADMITTED[name]
    compile_source(source)  # the reference admits it
    got = compile_to(source, "py")
    assert got == GENERIC_VARIANT_LOUD_REFUSALS[name], (
        f"{name}: recorded as {GENERIC_VARIANT_LOUD_REFUSALS[name]!r}, now "
        f"{got[:160]!r}. If the self-host now matches the reference, DELETE "
        f"the entry and compare bytes instead.")


@pytest.mark.parametrize("name", GENERIC_VARIANT_SILENT_ADMITS)
def test_generic_variant_arm_binding_is_a_known_silent_admit(compile_to, name):
    """Issue #2402: the reference refuses it, the native chain still emits.
    Fails the day the native chain refuses too, so the entry is removed."""
    source, message = _generic_variant_docs().REFUSED[name]
    with pytest.raises(RevlError) as refused:
        compile_source(source)
    assert message in str(refused.value)
    got = compile_to(source, "py")
    assert not got.startswith(("REFUSED|", "UNKNOWN_TIER|")), (
        f"{name}: the native chain now refuses ({got[:160]!r}); if that is "
        f"the reference's message, DELETE the entry from "
        f"GENERIC_VARIANT_SILENT_ADMITS.")


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
     "  provide cache { fn put(key) { let n = emit audit_write(key) } }\n"
     "}\n", "G4"),
    ("g2 two components provide one key",
     "service S { fn op(x: Str) -> Str }\n"
     "component A provides k: S { provide k { fn op(x) { return x } } }\n"
     "component B provides k: S { provide k { fn op(x) { return x } } }\n", "G2"),
    ("a1 async extern reached from a sync method",
     "extern emission async fn http_post(url: Str, body: Str) -> Str = @py { return url }\n"
     "service Http { emission fn post(url: Str, body: Str) -> Str }\n"
     "component Poster provides http: Http {\n"
     "  provide http { fn post(url, body) = emit http_post(url, body) }\n"
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


# --------------------------------------------------- issue #1633: predeclared

def test_native_go_renames_a_predeclared_parameter_in_the_signature(
        compile_to, reference_emit, tmp_path):
    """issue #1633, end to end. A parameter that shadows a host-predeclared name
    is renamed in the SIGNATURE, so the emitted Go declares and reads the same
    identifier.

    ``selfhost/lower.rvl`` renamed the BODY reference (``lir_expr``'s in-scope
    `var` arm) but wrote the source spelling into the parameter list, so the
    fully-native Go chain emitted ``func f(len int64, ...) { return
    revlAdd(len_, ...) }`` — a body naming a variable its signature never
    declared, which does not build. The reference frontend renames the binder
    where it is introduced; the native producer does now too.
    """
    source = "fn f(len: Int, error: Int) -> Int { return len + error }\n"
    doc = tmp_path / "predeclared_param.rvl"
    doc.write_text(source, encoding="utf-8")

    got = compile_to(source, "go")
    assert not got.startswith(("REFUSED|", "UNKNOWN_TIER|")), got[:80]

    want = reference_emit["go"](compile_files([str(doc)]))
    assert got == want, "native compile diverged from the reference"

    # the go tier escapes the already-renamed `len_` once more (`len__`), and
    # the signature and the body agree on it
    assert "func f(len__ int64, error_ int64) int64 {" in got
    assert "revlAdd(len__, error_)" in got

    go = shutil.which("go")
    if go is None:
        pytest.skip("Go compiler not installed")
    module = tmp_path / "predeclared_param.go"
    module.write_text(got, encoding="utf-8")
    result = subprocess.run(
        [go, "test", str(module)], capture_output=True, text=True,
        env={**os.environ, "GO111MODULE": "off"}, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr


def _arrows(node, acc):
    """Every ``{"kind": "arrow"}`` node in a lowered IR fragment, in document
    order. The arrow under test is nested in a `fn` body, not at the top level."""
    if isinstance(node, dict):
        if node.get("kind") == "arrow":
            acc.append(node)
        for value in node.values():
            _arrows(value, acc)
    elif isinstance(node, list):
        for value in node:
            _arrows(value, acc)
    return acc


def test_native_tiers_rename_a_predeclared_arrow_parameter_in_the_signature(
        compile_to, lower_to_ir, reference_emit, tmp_path):
    """issue #1633, the arrow half of the `fn` pin above — same defect, one level
    up: ``lir_arrow_at`` wrote the author's spelling into the closure's ``params``
    while its body went through ``lir_expr``'s in-scope `var` arm, which renames
    the emitted reference. So ``(len: Int) => len + 1`` produced a closure whose
    signature said ``len`` and whose body read ``len_`` — a binder and a use that
    disagree, which does not build in any tier. The reference frontend renames
    both, and now the native producer does too.
    """
    source = ("fn g(f: (Int) -> Int) -> Int { return f(1) }\n"
              "fn main() -> Int { return g((len: Int) => len + 1) }\n")
    doc = tmp_path / "predeclared_arrow_param.rvl"
    doc.write_text(source, encoding="utf-8")

    # the produced IR: the arrow's SIGNATURE and its BODY agree on `len_`
    arrow = _arrows(json.loads(lower_to_ir(source)), [])[0]
    assert arrow["params"] == ["len_"]
    assert arrow["body"]["left"] == {"kind": "var", "name": "len_"}

    # and every tier's bytes equal the reference's, so the disagreement is not
    # merely relocated: the go/python tiers escape the renamed `len_` once more
    # (`len__`) and rust keeps it (`len_`), in the declaration AND the use
    tiers = {"go": ("func(len__ int64)", "len__"),
             "py": ("lambda len__:", "len__"),
             "rust": ("|len_: i64|", "len_")}
    for tier, (declared, renamed) in tiers.items():
        got = compile_to(source, tier)
        assert not got.startswith(("REFUSED|", "UNKNOWN_TIER|")), got[:80]
        want = reference_emit[tier](compile_files([str(doc)]))
        assert got == want, "native %s diverged from the reference" % tier
        line = next(l for l in got.splitlines() if declared in l)
        assert line.count(renamed) >= 2, (tier, line)


# Issue #2359: every examples/rejections/ program the reference refuses at
# PARSE time. The self-host front end read statement bodies and type
# declarations too loosely to see the statement-level rules, so the native
# gate admitted 11 of them and the used-module admission 15, and emitted code.
def _reference_parse_refusals():
    from revl.parser import parse_text
    out = []
    for path in sorted((ROOT / "examples" / "rejections").glob("*.rvl")):
        text = path.read_text(encoding="utf-8")
        try:
            parse_text(text, str(path))
        except RevlError:
            out.append(path.name)
    return out


_PARSE_REFUSED = _reference_parse_refusals()


def test_the_rejection_corpus_has_parse_refusals():
    assert len(_PARSE_REFUSED) >= 30


@pytest.mark.parametrize("name", _PARSE_REFUSED)
def test_the_native_gate_refuses_every_reference_parse_refusal(compile_rvl, name):
    """The document path (`admit_src`, through `compile_to`) and the used-module
    path (`ad_module_refusal`) both refuse what the reference parser refuses."""
    text = (ROOT / "examples" / "rejections" / name).read_text(encoding="utf-8")
    got = compile_rvl["compile_to"](text, "py")
    assert got.startswith("REFUSED|"), got[:120]
    assert compile_rvl["ad_module_refusal"](text) != ""


def _reference_parseable_examples():
    from revl.parser import parse_text
    out = []
    corpora = [ROOT / "examples", ROOT / "stdlib"] + sorted(
        (ROOT / "tests" / "fixtures").glob("emit_*_corpus"))
    for base in corpora:
        for path in sorted(base.rglob("*.rvl")):
            text = path.read_text(encoding="utf-8")
            try:
                parse_text(text, str(path))
            except RevlError:
                continue
            out.append((path.relative_to(ROOT).as_posix(), text))
    return out


def test_the_statement_parse_rules_refuse_nothing_the_reference_parses(compile_rvl):
    """Issue #2359, the positive control: the statement-level parse rules
    refuse no program of examples/, stdlib/ or the emit corpora that the
    reference parses (optional calls `a?.m()`, `??` inside parentheses,
    classified externs, lifecycle words used as names, and so on)."""
    docs = _reference_parseable_examples()
    assert len(docs) > 300
    lex = compile_rvl["lex_src"]
    scan = compile_rvl["stmt_parse_scan"]
    refused = {rel: v["v"] for rel, text in docs if (v := scan(lex(text)))["v"] != ""}
    assert refused == {}


@pytest.mark.parametrize("text", [
    'extern pure fn h() -> Int = @py { return 1 }\nuse "./m.rvl" { abort }\n',
    'extern pure fn h() -> Int = @py { return 1 }\nuse "./m.rvl" { load call }\n',
    "fn f(a: Opt[Int], w: Bool, z: Bool) -> Bool {\n  let v = a ?? 0\n  return w && z\n}\n",
])
def test_the_statement_parse_rules_read_only_statement_bodies(compile_rvl, text):
    """An extern's `fn` opens no body, so the braces of a `use` list after it
    are no fn body, and a `??` does not reach across a statement boundary."""
    from revl.parser import parse_text
    parse_text(text, "x.rvl")
    assert compile_rvl["parse_refusal"](text) == ""


# Issue #2359, the statement grammar. Outside examples/rejections/, model
# attempts under bench/results/ that the reference refuses at parse time: a
# bare call in a provide method body, a stray `undo` line, a `provides key {`
# header with no service, an untyped `fn` parameter, a plain `let` in an
# activation body, a `'` string in a host body. Before the statement grammar
# was read, the document path admitted 9 of them and the used-module path 21.
def _bench_parse_refusals():
    from revl.parser import parse_text
    out = []
    for path in sorted((ROOT / "bench" / "results").rglob("*.rvl")):
        text = path.read_text(encoding="utf-8")
        try:
            parse_text(text, str(path))
        except RevlError:
            out.append(path.relative_to(ROOT).as_posix())
    return out


_BENCH_PARSE_REFUSED = _bench_parse_refusals()


def test_the_bench_results_have_parse_refusals():
    assert len(_BENCH_PARSE_REFUSED) >= 30


@pytest.mark.parametrize("rel", _BENCH_PARSE_REFUSED)
def test_the_native_gate_refuses_every_bench_parse_refusal(compile_rvl, rel):
    text = (ROOT / rel).read_text(encoding="utf-8")
    got = compile_rvl["compile_to"](text, "py")
    assert got.startswith("REFUSED|"), got[:120]
    assert compile_rvl["ad_module_refusal"](text) != ""


_SVC = "service K { fn put(k: Str) -> Int }\n"

_STMT_GRAMMAR_REFUSED = {
    "bare_call_in_method": _SVC + (
        "component C requires kv: K provides s: K {\n"
        "  provide s {\n    fn put(k: Str) -> Int {\n      kv.put(k)\n"
        "      return 1\n    }\n  }\n}\n"),
    "stray_undo_line": _SVC + (
        "component C requires kv: K provides s: K {\n"
        "  provide s {\n    fn put(k: Str) -> Int {\n"
        "      let r = kv.put(k)\n      undo kv.put(k)\n      return r\n"
        "    }\n  }\n}\n"),
    "header_provision_without_service": _SVC + (
        "component C requires kv: K provides s {\n"
        "  provide s { fn put(k: Str) -> Int = 1 }\n}\n"),
    "untyped_fn_parameter": "fn add(a, b) -> Int { return 1 }\n",
    "plain_let_in_activation_body": _SVC + (
        "component C requires kv: K {\n  let n = 1\n}\n"),
    "statement_after_method_equals": _SVC + (
        "component C requires kv: K provides s: K {\n"
        "  provide s { fn put(k: Str) -> Int = let n = 1 }\n}\n"),
    "statement_in_record_undo": _SVC + (
        "component C requires kv: K {\n"
        "  let h = effect { kv.put(\"a\") }\n    undo { emit kv.put(\"b\") }\n}\n"),
    "match_statement_in_method": _SVC + (
        "component C requires kv: K provides s: K {\n"
        "  provide s {\n    fn put(k: Str) -> Int {\n"
        "      match k { _ => 1 }\n      return 1\n    }\n  }\n}\n"),
    "semicolon_only_if_guard": _SVC + (
        "component C requires kv: K {\n  if (true) { ; }\n}\n"),
    "empty_else_guard": _SVC + (
        "component C requires kv: K {\n  if (true) { fail \"no\" } else { }\n}\n"),
}


@pytest.mark.parametrize("name", sorted(_STMT_GRAMMAR_REFUSED))
def test_the_statement_grammar_refuses_what_the_reference_refuses(compile_rvl, name):
    from revl.parser import parse_text
    text = _STMT_GRAMMAR_REFUSED[name]
    with pytest.raises(RevlError):
        parse_text(text, "x.rvl")
    assert compile_rvl["parse_refusal"](text) != ""
    assert compile_rvl["compile_to"](text, "py").startswith("REFUSED|")


_STMT_GRAMMAR_READ = {
    "if_and_match_tails": _SVC + (
        "component C requires kv: K provides s: K {\n"
        "  provide s {\n    fn put(k: Str) -> Int {\n"
        "      let o = Some(1)\n"
        "      let x = match o { Some(v) => { let z = v + 1; z }, None => 0 }\n"
        "      if (x > 1) { return x }\n"
        "      return if (x > 0) { 1 } else { 2 }\n    }\n  }\n}\n"),
    "method_loops_and_assignment": _SVC + (
        "component C requires kv: K provides s: K {\n"
        "  provide s {\n    fn put(k: Str) -> Int {\n      var n = 0\n"
        "      while (n < 3) { n += 1\n if (n == 2) { break } }\n"
        "      for (c of [1, 2]) { n = n + c }\n      return n\n"
        "    }\n  }\n}\n"),
    "activation_steps": _SVC + (
        "component C requires kv: K provides s: K {\n"
        "  isolate kv in realm(\"r\")\n"
        "  let h = effect kv.put(\"a\") undo kv.put(\"b\")\n"
        "  emit kv.put(\"c\")\n  if (true) { fail \"no\" }\n"
        "  provide s { fn put(k) = 1 }\n}\n"),
    "defaulted_parameters": "fn f(a: Int, b: Int = 2) -> Int { return a + b }\n",
    "extern_parameters": "extern pure fn g(a: Int) -> Int = @py { return a }\n",
}


@pytest.mark.parametrize("name", sorted(_STMT_GRAMMAR_READ))
def test_the_statement_grammar_refuses_nothing_the_reference_reads(compile_rvl, name):
    from revl.parser import parse_text
    text = _STMT_GRAMMAR_READ[name]
    parse_text(text, "x.rvl")
    scan = compile_rvl["stmt_parse_scan"](compile_rvl["lex_src"](text))
    assert scan["v"] == ""
    assert compile_rvl["rl_lex_refusal"](text) == ""


def test_a_host_body_the_self_host_lexer_closes_elsewhere_is_refused(compile_rvl):
    """`'` is a string in a ts host body, so `/'/g` opens one the reference
    lexer reads to the end of the text, while the self-host lexer, which skips
    only `"` strings, closed the body at its brace and admitted the module."""
    from revl.parser import parse_text
    text = ("extern pure fn q(s: Str) -> Str\n"
            "  = @ts { return s.replace(/'/g, \"x\") }\n"
            "  = @py { return s }\n")
    with pytest.raises(RevlError):
        parse_text(text, "x.rvl")
    assert compile_rvl["ad_module_refusal"](text) != ""
    assert compile_rvl["compile_to"](text, "py").startswith("REFUSED|")


# Issue #2396: where `expr_at` or `type_at` failed, the statement grammar left
# the rest unread, and top-level `fn` and `test` bodies were not read by it at
# all, so these forms the reference refuses were admitted and emitted.
def _method(body):
    return _SVC + (
        "component C requires kv: K provides s: K {\n  provide s {\n"
        "    fn put(k: Str) -> Int {\n" + body + "\n    }\n  }\n}\n")


def _fn(body):
    return "fn f(n: Int) -> Int {\n" + body + "\n}\n"


_ISSUE_2396 = {
    "else_if_expression": _method(
        "      let n = 3\n"
        "      return if (n > 2) { 1 } else if (n > 1) { 2 } else { 3 }"),
    "fn_type_annotation": _method(
        "      let f: fn(Int) -> Int = (x) => x\n      return 1"),
    "tuple_type_and_value": _method(
        "      let t: (Int, Str) = (1, \"a\")\n      return 1"),
    "match_arm_ending_in_if": _method(
        "      let v = match Some(1) { Some(x) => { if (x > 0) { x } else { 0 } }, None => 0 }\n"
        "      return v"),
}


@pytest.mark.parametrize("tier", ["py", "go", "java", "rust"])
@pytest.mark.parametrize("name", sorted(_ISSUE_2396))
def test_the_self_host_refuses_the_forms_of_issue_2396(compile_rvl, name, tier):
    from revl.parser import parse_text
    text = _ISSUE_2396[name]
    with pytest.raises(RevlError):
        parse_text(text, "x.rvl")
    got = compile_rvl["compile_to_at"](text, tier, "x.rvl")
    assert got.startswith("REFUSED|"), got[:120]


# The same shapes elsewhere: a top-level `fn` body, a plain `test` body, an
# effect block's setup, an extern signature, and the other rules of
# `type_`, `_if_expr`, `_match_expr`, `_match_block_arm` and `_primary`.
_TYPE_EXPR_REFUSED = {
    "fn_body_else_if": _fn("return if (n > 2) { 1 } else if (n > 1) { 2 } else { 3 }"),
    "fn_body_fn_type": _fn("let f: fn(Int) -> Int = (x) => x\nreturn 1"),
    "fn_body_tuple_type": _fn("let t: (Int, Str) = n\nreturn 1"),
    "fn_body_arm_ending_in_if": _fn(
        "let v = match Some(1) { Some(x) => { let y = 1; if (x > 0) { x } else { y } }, None => 0 }\n"
        "return v"),
    "test_body_else_if": (
        "test \"t\" {\n  let n = 1\n"
        "  assert (if (n > 2) { 1 } else if (n > 1) { 2 } else { 3 }) == 3\n}\n"),
    "nested_else_if": _fn("let v = [1, if (n > 1) { 2 } else if (n > 0) { 3 } else { 4 }]\nreturn 1"),
    "if_expression_without_else": _fn("let v = 1 + if (n > 1) { 2 }\nreturn v"),
    "arm_ending_in_while": _fn(
        "let v = match Some(1) { Some(x) => { while (x > 0) { } }, None => 0 }\nreturn v"),
    "arm_ending_in_let": _fn("let v = match Some(1) { Some(x) => { let y = x }, None => 0 }\nreturn v"),
    "return_in_arm": _fn("let v = match Some(1) { Some(x) => { return x }, None => 0 }\nreturn v"),
    "break_leaving_arm": _fn(
        "while (true) { let v = match Some(1) { Some(x) => { break; x }, None => 0 } }\nreturn 1"),
    "literal_match_pattern": _fn("let v = match n { 1 => 2, _ => 3 }\nreturn v"),
    "ternary_without_colon": _fn("let v = n > 1 ? 2 3\nreturn v"),
    "list_comprehension": _fn("let v = [x for x of [1]]\nreturn 1"),
    "keyword_type": _fn("let s: match = n\nreturn 1"),
    "type_arguments_without_comma": _method("      let s: List[Int Str] = []\n      return 1"),
    "stream_state_index": _method("      let s: Stream[Int, Foo] = k\n      return 1"),
    "arrow_parameter_fn_type": _fn("let g = (h: fn(Int) -> Int) => h(1)\nreturn 1"),
    "hole_tuple_type": _fn("let v = hole[(Int, Str)] \"x\"\nreturn 1"),
    "extern_parameter_tuple_type": "extern pure fn g(a: (Int, Str)) -> Int = @py { return 1 }\n",
    "effect_block_ending_in_if": _SVC + (
        "component C requires kv: K {\n"
        "  let h = effect { if (true) { kv.put(\"a\") } } undo kv.put(\"b\")\n}\n"),
    "fail_in_effect_block": _SVC + (
        "component C requires kv: K {\n"
        "  let h = effect { fail \"x\"\n kv.put(\"a\") } undo kv.put(\"b\")\n}\n"),
    "activation_let_tuple_type": _SVC + (
        "component C requires kv: K {\n"
        "  let h: (Int, Str) = effect kv.put(\"a\") undo kv.put(\"b\")\n}\n"),
}


@pytest.mark.parametrize("name", sorted(_TYPE_EXPR_REFUSED))
def test_the_type_and_expression_rules_refuse_what_the_reference_refuses(compile_rvl, name):
    from revl.parser import parse_text
    text = _TYPE_EXPR_REFUSED[name]
    with pytest.raises(RevlError):
        parse_text(text, "x.rvl")
    assert compile_rvl["parse_refusal"](text) != ""
    assert compile_rvl["ad_module_refusal"](text) != ""
    assert compile_rvl["compile_to"](text, "py").startswith("REFUSED|")


# Documents the reference reads, exercising each rule the readers above port:
# none of them gets a statement-grammar verdict.
_TYPE_EXPR_READ = {
    "function_types": (
        "fn f(g: (Int) -> Int, h: ((Int) -> Bool)?) -> (Int) -> Int {\n"
        "  let k: (Int, Str) -> List[Int] = (a: Int, b: Str): List[Int] => [a]\n"
        "  return g\n}\n"),
    "if_and_match_expressions": _fn(
        "let a = if (n > 1) { ; 2 ; } else { 3 }\n"
        "let b = match Some(n) { Some(x) => { let y = x + 1; y }, None => 0, }\n"
        "let c = match Some(n) { Some(x) => { var z = x\n z += 1\n z }, _ => { 0 } }\n"
        "if (a > 2) { return 1 } else if (a > 1) { return 2 } else { return b + c }"),
    "fn_body_statements": _fn(
        "let {x, y} = {x: 1, y: 2}\nlet [p, ...rest] = [1, 2]\nvar i = 0\n"
        "while (i < 3) { i += 1\n if (i == 2) { continue } }\n"
        "for (e of rest) { i = i + e }\nassert i > 0\n"
        "let o = Some(1)\nlet q = o?.map(v => v)\n"
        "let t = n > 1 ? \"a\" : \"b\"\nlet r = { a: 1, b: [1, 2][0] }\n"
        "let u = { r | a = 2 }\nreturn x + y + p + i"),
    "expression_bodies_and_tests": (
        "fn d(x: Int) -> Int = x * 2\n"
        "fn h() -> Int = hole[Int] \"later\"\n"
        "test \"d doubles\" {\n  let s: Stream[Int, Active]? = null\n  assert d(2) == 4\n}\n"),
    "extern_clause_arguments": (
        "extern emission deferred idempotent(key: msg) fn post(sink: Str, msg: Str) = @py { return }\n"
        "extern pure fn g(a: (Int) -> Int) -> Int = @py { return 1 }\n"),
    "effect_block_setup": _SVC + (
        "component C requires kv: K {\n"
        "  let h = effect { let a = \"x\"; kv.put(a) } undo kv.put(\"b\")\n}\n"),
}


@pytest.mark.parametrize("name", sorted(_TYPE_EXPR_READ))
def test_the_type_and_expression_rules_refuse_nothing_the_reference_reads(compile_rvl, name):
    from revl.parser import parse_text
    text = _TYPE_EXPR_READ[name]
    parse_text(text, "x.rvl")
    scan = compile_rvl["stmt_parse_scan"](compile_rvl["lex_src"](text))
    assert scan["v"] == ""


# Issue #2399: the reference lowers `test` and `prop test` bodies through the
# statement rules a `fn` body goes through, and `_lift_block_arm` refuses a
# block arm it cannot lift. The self-host checked neither, so it emitted these
# documents. Each one is refused by the reference at type-check or lowering,
# and the self-host now refuses it with the reference's own message.
def _test_doc(body):
    return ("fn d(n: Int) -> Int {\n  return n * 2\n}\n"
            "test \"d\" {\n" + body + "\n  assert true\n}\n")


def _prop_doc(body):
    return "prop test \"p\" (n: Int) {\n" + body + "\n  assert true\n}\n"


_ISSUE_2399_REFUSED = {
    "test_body_empty_map": _test_doc("  let m: Map[Str, Int] = {}"),
    "arm_reads_untyped_binder": (
        "fn f(o: Option[List[Int]]) -> Int {\n"
        "  return match o { Some(xs) => { let k = xs.length\n k }, None => 0 }\n}\n"),
    "test_body_empty_list_as_map": _test_doc("  let m: Map[Str, Int] = []"),
    "test_body_empty_record_as_list": _test_doc("  let m: List[Int] = {}"),
    "test_body_var_empty_map": _test_doc("  var m: Map[Str, Int] = {}"),
    "test_body_argument_empty_map": (
        "fn g(m: Map[Str, Int]) -> Int {\n  return 1\n}\n"
        "test \"t\" {\n  assert g({}) == 1\n}\n"),
    "test_body_let_mismatch": _test_doc("  let m: Int = \"s\""),
    "second_test_refuses": (
        "test \"a\" {\n  assert true\n}\n"
        "test \"b\" {\n  let m: Int = \"s\"\n  assert true\n}\n"),
    "prop_body_empty_map": _prop_doc("  let m: Map[Str, Int] = {}"),
    "prop_body_let_mismatch": _prop_doc("  let m: Str = n"),
    "test_body_block_arm": (
        "test \"t\" {\n  let o: Opt[Int] = Some(1)\n"
        "  let v = match o { Some(x) => { let k = x\n k }, None => 0 }\n"
        "  assert v == 1\n}\n"),
    "prop_body_block_arm": _prop_doc(
        "  let v = match Some(n) { Some(x) => { let k = x\n k }, None => 0 }"),
    "duplicate_test": "test \"t\" {\n  assert true\n}\ntest \"t\" {\n  assert true\n}\n",
    "duplicate_prop_test": _prop_doc("") + _prop_doc(""),
    "arm_reads_untyped_let": (
        "extern pure fn g() = @py { return 1 }\n"
        "fn f(n: Int) -> Int {\n  let a = g()\n"
        "  let v = match Some(n) { Some(x) => { let k = a\n x }, None => 0 }\n"
        "  return v\n}\n"),
    "arm_reads_outer_untyped_binder": (
        "fn f(o: Option[Int], p: Opt[Int]) -> Int {\n"
        "  return match o { Some(x) => match p { Some(y) => { let k = x\n k + y }, "
        "None => 0 }, None => 0 }\n}\n"),
    "arm_binder_of_undeclared_type": (
        "fn f(o: Maybe[Int]) -> Int {\n"
        "  return match o { Some(x) => { let k = x\n k }, None => 0 }\n}\n"),
}

_BOX = "type Box[T] = { v: T }\n"
_EXTERN = "extern pure fn g() = @py { return [1] }\n"

_ISSUE_2399_REFUSED.update({
    # an arm binder is typed only where `_arm_payload_type` types it
    "arm_opt_matched_with_ok": (
        "fn f(o: Opt[Int]) -> Int {\n"
        "  return match o { Ok(x) => { let k = x\n k }, _ => 0 }\n}\n"),
    "arm_result_matched_with_some": (
        "fn f(o: Result[Int, Str]) -> Int {\n"
        "  return match o { Some(x) => { let k = x\n k }, _ => 0 }\n}\n"),
    "arm_binder_of_applied_declared_generic": (
        "type Option[T] = Some(T) | None\nfn f(o: Option[Int]) -> Int {\n"
        "  return match o { Some(x) => { let k = x\n k }, None => 0 }\n}\n"),
    # a test written right after an extern is still checked
    "test_body_host_method_after_extern": (
        _EXTERN + "test \"t\" {\n  let a = g()\n  let n = a.length()\n  assert true\n}\n"),
    "prop_body_host_method_after_extern": (
        _EXTERN + "prop test \"p\" (n: Int) {\n  let a = g()\n"
        "  let k = a.length()\n  assert true\n}\n"),
    "test_body_let_mismatch_after_extern": (
        _EXTERN + "test \"t\" {\n  let m: Int = \"s\"\n  assert true\n}\n"),
    "duplicate_test_after_extern": (
        _EXTERN + "test \"t\" {\n  assert true\n}\ntest \"t\" {\n  assert true\n}\n"),
    # an alias leaves prop bodies undecided, not their names
    "duplicate_prop_test_with_an_alias": "type M = Int\n" + _prop_doc("") + _prop_doc(""),
    # the instantiated fields still refuse a record that does not fit
    "generic_record_field_mismatch_in_test": _BOX + _test_doc("  let b: Box[Int] = { v: \"s\" }"),
    "generic_record_field_mismatch_in_fn": (
        _BOX + "fn f(n: Int) -> Int {\n  let b: Box[Int] = { v: \"s\" }\n  return n\n}\n"),
    "generic_record_wrong_field": (
        _BOX + "fn f(n: Int) -> Int {\n  let b: Box[Int] = { w: n }\n  return n\n}\n"),
})

# A record literal bound to an applied generic record, which the reference
# checks against the instantiated fields, and the arm binders it types. The
# self-host must neither refuse these at admission nor when it compiles them.
_ISSUE_2399_COMPILED = {
    "generic_record_in_test": _BOX + _test_doc("  let b: Box[Int] = { v: 1 }"),
    "generic_record_in_prop": _BOX + _prop_doc("  let b: Box[Int] = { v: n }"),
    "generic_record_in_fn": (
        _BOX + "fn f(n: Int) -> Int {\n  let b: Box[Int] = { v: n }\n  return b.v\n}\n"),
    "generic_record_two_params": (
        "type P[A, B] = { a: A, b: B }\nfn f(n: Int) -> Int {\n"
        "  let p: P[Int, Str] = { a: n, b: \"s\" }\n  return p.a\n}\n"),
    "generic_record_nested_argument": (
        _BOX + "fn f(n: Int) -> Int {\n  let b: Box[List[Int]] = { v: [n] }\n  return n\n}\n"),
    "arm_result_ok_and_err": (
        "fn f(o: Result[Int, Str]) -> Int {\n"
        "  return match o { Ok(x) => { let k = x\n k }, Err(e) => { let m = e\n 0 } }\n}\n"),
}


_P = "type P[A, B] = { a: A, b: B }\n"
_G = "fn g(b: Box[Int]) -> Int {\n  return b.v\n}\n"


def _prop_params(params):
    return "prop test \"p\" (" + params + ") {\n  assert true\n}\n"


_ISSUE_2399_REFUSED.update({
    # a nested or argument-position generic record is instantiated too
    "generic_record_nested_mismatch": (
        _BOX + "fn f() -> Int {\n  let b: Box[Box[Int]] = { v: { v: \"s\" } }\n  return 1\n}\n"),
    "generic_record_argument_mismatch": (
        _BOX + _G + "fn f() -> Int {\n  return g({ v: \"s\" })\n}\n"),
    # an explicit type parameter list no longer hides a fn body
    "arm_binder_of_explicit_type_parameter": (
        "fn f[T](o: T) -> Int {\n"
        "  return match o { Some(x) => { let k = x\n 0 }, _ => 0 }\n}\n"),
    "let_mismatch_in_explicit_generic_fn": (
        "fn f[T](o: T) -> Int {\n  let m: Int = \"s\"\n  return 1\n}\n"),
    # `_check_generatable` over each prop test parameter
    "prop_param_map": _prop_params("m: Map[Str, Int]"),
    "prop_param_result": _prop_params("r: Result[Int, Str]"),
    "prop_param_list_of_undeclared": _prop_params("xs: List[Foo]"),
    "prop_param_second": _prop_params("n: Int, m: Map[Str, Int]"),
    "prop_param_record_field": "type R = { a: Int, m: Map[Str, Int] }\n" + _prop_params("r: R"),
    "prop_param_record_cycle": "type R = { s: S }\ntype S = { r: R }\n" + _prop_params("r: R"),
    "prop_param_variant_payload": "type Sh = Circle(Map[Str, Int]) | Pt\n" + _prop_params("s: Sh"),
    "prop_param_bare_generic_record": _BOX + _prop_params("b: Box"),
    "prop_param_applied_generic_record": _BOX + _prop_params("b: Box[Int]"),
})

_ISSUE_2399_COMPILED.update({
    # issue #2514: the alias sweep reaches a prop test's parameters and body,
    # so `n: M` generates as an `Int` and `let y: M = n` types as one
    "prop_param_alias": "type M = Int\n" + _prop_params("n: M"),
    "prop_body_alias": (
        "type M = Int\nprop test \"p\" (n: M) {\n  let y: M = n\n  assert y == n\n}\n"),
    "hole_alias": (
        "type M = Int\nfn helper(x: Int) -> Int { return hole[M] \"a function body\" }\n"),
    "generic_record_nested_in_test": _BOX + _test_doc("  let b: Box[Box[Int]] = { v: { v: 1 } }"),
    "generic_record_nested_in_prop": _BOX + _prop_doc("  let b: Box[Box[Int]] = { v: { v: n } }"),
    "generic_record_nested_in_fn": (
        _BOX + "fn f(n: Int) -> Int {\n  let b: Box[Box[Int]] = { v: { v: n } }\n  return n\n}\n"),
    "generic_record_argument_in_test": _BOX + _G + "test \"t\" {\n  assert g({ v: 1 }) == 1\n}\n",
    "generic_record_argument_in_prop": _BOX + _G + _prop_doc("  let k = g({ v: n })"),
    "generic_record_argument_in_fn": _BOX + _G + "fn f() -> Int {\n  return g({ v: 1 })\n}\n",
    "generic_record_two_params_in_test": _P + _test_doc("  let p: P[Int, Str] = { a: 1, b: \"s\" }"),
    "generic_record_two_params_in_prop": _P + _prop_doc("  let p: P[Int, Str] = { a: n, b: \"s\" }"),
    "generic_record_two_params_nested": (
        _P + _BOX + "fn f() -> Int {\n  let p: P[Box[Int], Str] = { a: { v: 1 }, b: \"s\" }\n"
        "  return 1\n}\n"),
    "explicit_generic_fn": (
        "fn first[T](xs: List[T], d: T) -> T {\n  if (xs.length() == 0) { return d }\n"
        "  return xs[0]\n}\nfn g() -> Int {\n  return first([1], 0)\n}\n"),
    "prop_param_generatable": _prop_params("o: Opt[List[Int]], f: Float, b: Bool, s: Str"),
    "prop_param_record": "type R = { a: Int, b: List[Str] }\n" + _prop_params("r: R"),
    "prop_param_variant": "type Sh = Circle(Int) | Pt\n" + _prop_params("s: Sh"),
    "prop_param_recursive_variant": "type L = Cons(L) | Nil\n" + _prop_params("l: L"),
    "prop_param_empty_record": "type E = {}\n" + _prop_params("e: E"),
})


@pytest.mark.parametrize("name", sorted(_ISSUE_2399_COMPILED))
def test_the_review_controls_of_issue_2399_compile_on_both_sides(compile_rvl, admit, name):
    text = _ISSUE_2399_COMPILED[name]
    assert _reference_refusal(text) == ""
    assert admit(text) == ""
    got = compile_rvl["compile_to_at"](text, "py", "x.rvl")
    assert not got.startswith("REFUSED|"), got[:160]


# Documents the reference compiles, each next to one of the rules above: the
# gate must not refuse any of them.
_ISSUE_2399_ADMITTED = {
    "test_body_empty_list": _test_doc("  let m: List[Int] = []"),
    "test_body_untyped_empty_record": _test_doc("  let m = {}"),
    "two_tests": "test \"a\" {\n  assert true\n}\ntest \"b\" {\n  assert 1 == 1\n}\n",
    "prop_body": _prop_doc("  let k: Int = n + 1"),
    "arm_typed_binder": (
        "fn f(o: Opt[List[Int]]) -> Int {\n"
        "  return match o { Some(xs) => { let k = xs.length\n k }, None => 0 }\n}\n"),
    "arm_does_not_read_binder": (
        "fn f(o: Option[List[Int]]) -> Int {\n"
        "  return match o { Some(xs) => { let k = 1\n k }, None => 0 }\n}\n"),
    "expression_arm_reads_binder": (
        "fn f(o: Option[List[Int]]) -> Int {\n"
        "  return match o { Some(xs) => xs.length, None => 0 }\n}\n"),
    "arm_binder_of_declared_variant": (
        "type Sh = Circle(Int) | Pt\nfn f(s: Sh) -> Int {\n"
        "  return match s { Circle(r) => { let k = r\n k }, Pt => 0 }\n}\n"),
    "arm_rebinds_untyped_name": (
        "fn f(o: Option[Int]) -> Int {\n"
        "  return match o { Some(x) => { let x2 = 1\n x2 }, None => 0 }\n}\n"),
}


def _reference_refusal(text):
    return _reference_message(lambda: compile_source(text, "x.rvl"))


@pytest.mark.parametrize("tier", ["py", "go", "java", "rust"])
@pytest.mark.parametrize("name", sorted(_ISSUE_2399_REFUSED))
def test_the_self_host_refuses_what_the_reference_checker_refuses(compile_rvl, name, tier):
    text = _ISSUE_2399_REFUSED[name]
    want = _reference_refusal(text)
    assert want != ""
    got = compile_rvl["compile_to_at"](text, tier, "x.rvl")
    assert got.startswith("REFUSED|"), got[:120]
    assert got.split("|", 2)[2] == want


@pytest.mark.parametrize("name", sorted(_ISSUE_2399_ADMITTED))
def test_the_checker_rules_of_issue_2399_admit_what_the_reference_compiles(admit, name):
    text = _ISSUE_2399_ADMITTED[name]
    assert _reference_refusal(text) == ""
    assert admit(text) == ""


# Issue #2403: two applied generic records are compared by their type
# arguments, the way `compatible` meets two same-head types elementwise. The
# self-host read `Box[Str]` as an opaque alias and admitted all of these.
_G2 = "fn g2(b: Box[Box[Int]]) -> Int {\n  return 1\n}\n"
_MK = "fn mk() -> Box[Str] {\n  return { v: \"s\" }\n}\n"
_S_LET = "  let s: Box[Str] = { v: \"s\" }\n"
_FIRST = "fn first[T](a: T, b: T) -> T {\n  return a\n}\n"
_L = "type L[T] = { h: T, t: Opt[L[T]] }\n"
_IF_LIT = "if (c) { b } else { { v: \"s\" } }"
_IF_S = "if (c) { b } else { s }"
_MATCH_LIT = "match o { Some(n) => b, None => { v: \"s\" } }"
_BLK_B = "match o { Some(n) => { let z = 1\n b }, None => b }"
_BLK_S = "match o { Some(n) => { let z = 1\n s }, None => b }"
_BLK_TEST = ("  let o: Opt[Int] = Some(1)\n  let b: Box[Int] = { v: 1 }\n"
             "  let s: Box[Str] = { v: \"s\" }\n")


def _box_fn(params, body, ret="Int"):
    return "fn f(" + params + ") -> " + ret + " {\n" + body + "\n}\n"


_ISSUE_2403_REFUSED = {
    "argument": _BOX + _G + _box_fn("s: Box[Str]", "  return g(s)"),
    "let": _BOX + _box_fn("s: Box[Str]", "  let t: Box[Int] = s\n  return 1"),
    "return": _BOX + _box_fn("s: Box[Str]", "  return s", "Box[Int]"),
    "nested_argument": _BOX + _G2 + _box_fn("s: Box[Box[Str]]", "  return g2(s)"),
    "nested_let": _BOX + _box_fn("s: Box[Box[Str]]", "  let t: Box[Box[Int]] = s\n  return 1"),
    "nested_return": _BOX + _box_fn("s: Box[Box[Str]]", "  return s", "Box[Box[Int]]"),
    "call_result_argument": _BOX + _G + _MK + "fn f() -> Int {\n  return g(mk())\n}\n",
    "list_argument": _BOX + (
        "fn h(xs: List[Box[Int]]) -> Int {\n  return 1\n}\n"
        + _box_fn("s: List[Box[Str]]", "  return h(s)")),
    "opt_argument": _BOX + (
        "fn h(o: Opt[Box[Int]]) -> Int {\n  return 1\n}\n"
        + _box_fn("s: Box[Str]", "  return h(s)")),
    "two_params_swapped": _P + (
        "fn h(p: P[Int, Str]) -> Int {\n  return 1\n}\n"
        + _box_fn("p: P[Str, Int]", "  return h(p)")),
    "test_argument": _BOX + _G + "test \"t\" {\n" + _S_LET + "  assert g(s) == 1\n}\n",
    "test_let": _BOX + "test \"t\" {\n" + _S_LET + "  let t: Box[Int] = s\n  assert true\n}\n",
    "test_nested_let": _BOX + (
        "test \"t\" {\n  let s: Box[Box[Str]] = { v: { v: \"s\" } }\n"
        "  let t: Box[Box[Int]] = s\n  assert true\n}\n"),
    "prop_argument": _BOX + _G + _prop_doc(_S_LET + "  let k = g(s)"),
    "prop_let": _BOX + _prop_doc(_S_LET + "  let t: Box[Int] = s"),
    # the same comparison at the other positions that meet two types
    "equality": _BOX + _box_fn("b: Box[Int], c: Box[Str]", "  return b == c", "Bool"),
    "builtin_argument": _BOX + _box_fn(
        "xs: List[Box[Int]], b: Box[Str]", "  let ys = xs.push(b)\n  return ys.length()"),
    "generic_fn_argument": _BOX + _FIRST + _box_fn(
        "b: Box[Int], c: Box[Str]", "  let r = first(b, c)\n  return 1"),
    "function_value_argument": _BOX + _box_fn(
        "c: Box[Str]", "  let k = (x: Box[Int]) => x.v\n  return k(c)"),
    "list_element": _BOX + _box_fn(
        "b: Box[Str]", "  let xs: List[Box[Int]] = [b]\n  return xs.length()"),
    # each branch of an `if` and each `match` arm against the expectation, and
    # the ternary rule where no expectation is given
    "if_let": _BOX + _box_fn("c: Bool, b: Box[Int]", "  let r: Box[Int] = " + _IF_LIT + "\n  return 1"),
    "if_let_records": _BOX + _box_fn(
        "c: Bool, b: Box[Int], s: Box[Str]", "  let r: Box[Int] = " + _IF_S + "\n  return 1"),
    "if_return": _BOX + _box_fn("c: Bool, b: Box[Int]", "  return " + _IF_LIT, "Box[Int]"),
    "if_argument": _BOX + _G + _box_fn("c: Bool, b: Box[Int]", "  return g(" + _IF_LIT + ")"),
    "if_untyped_let": _BOX + _box_fn(
        "c: Bool, b: Box[Int], s: Box[Str]", "  let r = " + _IF_S + "\n  return 1"),
    "if_in_test": _BOX + "test \"t\" {\n  let b: Box[Int] = { v: 1 }\n" + _S_LET
                  + "  let r = if (true) { b } else { s }\n  assert true\n}\n",
    "match_let": _BOX + _box_fn(
        "o: Opt[Int], b: Box[Int]", "  let r: Box[Int] = " + _MATCH_LIT + "\n  return 1"),
    "match_let_records": _BOX + _box_fn(
        "o: Opt[Int], b: Box[Int], s: Box[Str]",
        "  let r: Box[Int] = match o { Some(n) => b, None => s }\n  return 1"),
    # a block arm is checked through its tail, and a test or prop body still
    # refuses the block arm itself where the types agree
    "block_arm_let": _BOX + _box_fn(
        "o: Opt[Int], b: Box[Int], s: Box[Str]", "  let r: Box[Int] = " + _BLK_S + "\n  return 1"),
    "block_arm_return": _BOX + _box_fn(
        "o: Opt[Int], b: Box[Int], s: Box[Str]", "  return " + _BLK_S, "Box[Int]"),
    "block_arm_in_test": _BOX + _test_doc(_BLK_TEST + "  let r: Box[Int] = " + _BLK_S),
    "block_arm_in_prop": _BOX + _prop_doc(_BLK_TEST + "  let r: Box[Int] = " + _BLK_S),
    "block_arm_unlowerable_in_test": _BOX + _test_doc(_BLK_TEST + "  let r: Box[Int] = " + _BLK_B),
    "block_arm_unlowerable_in_prop": _BOX + _prop_doc(_BLK_TEST + "  let r: Box[Int] = " + _BLK_B),
    # a recursive generic record is compared by type argument, never expanded
    "recursive_record": _L + (
        "fn h(l: L[Int]) -> Int {\n  return 1\n}\n" + _box_fn("l: L[Str]", "  return h(l)")),
}

_ISSUE_2403_COMPILED = {
    "argument": _BOX + _G + _box_fn("s: Box[Int]", "  return g(s)"),
    "let_and_field": _BOX + _box_fn("s: Box[Int]", "  let t: Box[Int] = s\n  return t.v"),
    "return": _BOX + _box_fn("s: Box[Int]", "  return s", "Box[Int]"),
    "nested_argument": _BOX + _G2 + _box_fn("s: Box[Box[Int]]", "  return g2(s)"),
    "nested_field": _BOX + _box_fn("s: Box[Box[Int]]", "  return s.v.v"),
    "widening_argument": _BOX + (
        "fn h(b: Box[Float]) -> Int {\n  return 1\n}\n"
        + _box_fn("s: Box[Int]", "  return h(s)")),
    "list_argument": _BOX + (
        "fn h(xs: List[Box[Int]]) -> Int {\n  return xs.length()\n}\n"
        + _box_fn("s: List[Box[Int]]", "  return h(s)")),
    "two_params": _P + (
        "fn h(p: P[Int, Str]) -> Int {\n  return p.a\n}\n"
        + _box_fn("p: P[Int, Str]", "  return h(p)")),
    "test_argument": _BOX + _G + "test \"t\" {\n  let s: Box[Int] = { v: 1 }\n"
                     "  assert g(s) == 1\n}\n",
    "prop_argument": _BOX + _G + _prop_doc("  let s: Box[Int] = { v: n }\n  let k = g(s)"),
    # a record literal still meets an applied generic record field-wise
    "equality_with_literal": _BOX + _box_fn("b: Box[Int]", "  return b == { v: 1 }", "Bool"),
    "builtin_argument_literal": _BOX + _box_fn(
        "xs: List[Box[Int]]", "  let ys = xs.push({ v: 1 })\n  return ys.length()"),
    "generic_fn_literal": _BOX + _FIRST + _box_fn(
        "b: Box[Int]", "  let r = first(b, { v: 1 })\n  return r.v"),
    "function_value_literal": _BOX + _box_fn(
        "b: Box[Int]", "  let k = (x: Box[Int]) => x.v\n  return k({ v: 1 })"),
    "return_literal": _BOX + _box_fn("", "  return { v: 1 }", "Box[Int]"),
    "if_join": _BOX + _box_fn(
        "c: Bool, b: Box[Int]", "  let r = if (c) { b } else { { v: 1 } }\n  return r", "Box[Int]"),
    "recursive_record": _L + (
        "fn h(l: L[Int]) -> Int {\n  return 1\n}\n" + _box_fn("l: L[Int]", "  return h(l)")),
    "if_argument_literal": _BOX + _G + _box_fn(
        "c: Bool, b: Box[Int]", "  return g(if (c) { b } else { { v: 1 } })"),
    "if_widening": _BOX + _box_fn(
        "c: Bool, b: Box[Int], d: Box[Float]", "  let r = if (c) { b } else { d }\n  return 1"),
    "match_argument": _BOX + _G + _box_fn("o: Opt[Int], b: Box[Int]", "  return g(" + _MATCH_LIT + ")"),
    "block_arm_let": _BOX + _box_fn(
        "o: Opt[Int], b: Box[Int]", "  let r: Box[Int] = " + _BLK_B + "\n  return r.v"),
    "block_arm_return": _BOX + _box_fn("o: Opt[Int], b: Box[Int]", "  return " + _BLK_B, "Box[Int]"),
    "block_arm_reads_its_let": _BOX + _box_fn(
        "o: Opt[Int], b: Box[Int]",
        "  let r: Box[Int] = match o { Some(n) => { let z = b\n z }, None => b }\n  return r.v"),
    "match_let_literal": _BOX + _box_fn(
        "o: Opt[Int], b: Box[Int]",
        "  let r: Box[Int] = match o { Some(n) => b, None => { v: 1 } }\n  return r.v"),
}


@pytest.mark.parametrize("tier", ["py", "go", "java", "rust"])
@pytest.mark.parametrize("name", sorted(_ISSUE_2403_REFUSED))
def test_two_applied_generic_records_are_compared_by_type_argument(compile_rvl, name, tier):
    text = _ISSUE_2403_REFUSED[name]
    want = _reference_refusal(text)
    assert want != ""
    got = compile_rvl["compile_to_at"](text, tier, "x.rvl")
    assert got.startswith("REFUSED|"), got[:120]
    assert got.split("|", 2)[2] == want


@pytest.mark.parametrize("name", sorted(_ISSUE_2403_COMPILED))
def test_matching_generic_record_arguments_compile_on_both_sides(compile_rvl, admit, name):
    text = _ISSUE_2403_COMPILED[name]
    assert _reference_refusal(text) == ""
    assert admit(text) == ""
    got = compile_rvl["compile_to_at"](text, "py", "x.rvl")
    assert not got.startswith("REFUSED|"), got[:160]


# Issue #2397: a module `fn` parameter may declare a trailing default
# (`fn f(x: Int = 3)`, item 187). The self-host parse refused every such
# declaration with `expected { after fn signature`. The reference erases the
# default from the declaration's IR and expands it at each call site that omits
# the argument; the self-host does not mirror that expansion, so a use of a
# defaulted `fn` other than a full call is a named refusal, as is a default not
# built from literals (the reference types a default with no bindings in scope
# and refuses an effectful one, neither of which is mirrored).
_ISSUE_2397_COMPILED = {
    "after_applied_type": "fn f(r: Result[Int, Str], x: Int = 3) -> Int { return x }\n",
    "if_default": "fn g(x: Int = if (true) { 1 } else { 2 }) -> Int { return x }\n",
    "list_default": "fn h(xs: List[Int] = [1, 2]) -> Int { return 0 }\n",
    "defaulted_parameters": "fn f(a: Int, b: Int = 2) -> Int { return a + b }\n",
    "full_call": ("fn f(a: Int, b: Int = 2) -> Int { return a + b }\n"
                  "fn k() -> Int { return f(1, 3) }\n"),
    "generic": "fn f[T](x: T, n: Int = 1) -> Int { return n }\n",
    "widening": "fn f(x: Float = 1) -> Float { return x }\n",
    "none_default": "fn f(x: Opt[Int] = None) -> Int { return 1 }\n",
    "public": "pub fn f(x: Int = 1) -> Int { return x }\n",
}

_ISSUE_2397_REFUSED = {
    "wrong_type": "fn f(x: Int = \"s\") -> Int { return x }\n",
    "none_for_int": "fn f(x: Int = None) -> Int { return 1 }\n",
    "wrong_list": "fn f(xs: List[Int] = [\"a\"]) -> Int { return 0 }\n",
    "wrong_if": "fn f(x: Int = if (true) { \"a\" } else { \"b\" }) -> Int { return x }\n",
    "required_after_default": "fn f(a: Int = 1, x: Int) -> Int { return x }\n",
    "arrow_parameter": "fn f() -> Int {\n  let g = (x: Int = 1) => x\n  return 1\n}\n",
}

_ISSUE_2397_UNSUPPORTED = {
    "calls_a_fn": "fn g() -> Int { return 1 }\nfn f(x: Int = g()) -> Int { return x }\n",
    "names_a_parameter": "fn f(a: Int, x: Int = a) -> Int { return x }\n",
    "call_omits_default": ("fn f(a: Int, b: Int = 2) -> Int { return a + b }\n"
                           "fn k() -> Int { return f(1) }\n"),
    "test_omits_default": ("fn f(a: Int, b: Int = 2) -> Int { return a + b }\n"
                           "test \"t\" {\n  assert f(1) == 3\n}\n"),
    "fn_value": ("fn f(a: Int, b: Int = 2) -> Int { return a + b }\n"
                 "fn k() -> Int {\n  let g = f\n  return 1\n}\n"),
}


@pytest.mark.parametrize("tier", ["py", "go", "rust"])
@pytest.mark.parametrize("name", sorted(_ISSUE_2397_COMPILED))
def test_a_defaulted_parameter_compiles_as_the_reference_does(compile_to, reference_emit,
                                                              name, tier):
    text = _ISSUE_2397_COMPILED[name]
    assert compile_to(text, tier) == reference_emit[tier](compile_source(text))


@pytest.mark.parametrize("tier", ["py", "go", "java", "rust"])
@pytest.mark.parametrize("name", sorted(_ISSUE_2397_REFUSED))
def test_a_bad_parameter_default_draws_the_reference_refusal(compile_rvl, name, tier):
    text = _ISSUE_2397_REFUSED[name]
    want = _reference_refusal(text)
    assert want != ""
    got = compile_rvl["compile_to_at"](text, tier, "x.rvl")
    assert got.startswith("REFUSED|"), got[:120]
    assert got.split("|", 2)[2] == want


@pytest.mark.parametrize("name", sorted(_ISSUE_2397_UNSUPPORTED))
def test_an_unmirrored_default_is_a_named_refusal(compile_rvl, name):
    text = _ISSUE_2397_UNSUPPORTED[name]
    assert _reference_refusal(text) == ""
    got = compile_rvl["compile_to_at"](text, "py", "x.rvl")
    assert got.startswith("REFUSED|UNSUPPORTED|"), got[:160]
    assert "self-host compiler" in got


# Issue #2405, gaps 1 and 2: a `var` reassigned a value of another type, and an
# `if` whose branches disagree. The self-host admitted both. A reassignment is
# compared only where neither type carries an unknown or a bottom the
# reference would widen (`widen_bottom`); an `if` meets its expectation branch
# by branch where one is given, and the ternary `join` otherwise.
_ISSUE_2405_REFUSED = {
    "var_reassigned_str": "fn f() -> Int {\n  var x = 1\n  x = \"s\"\n  return 1\n}\n",
    "var_reassigned_float": "fn f() -> Int {\n  var x = 1\n  x = 2.5\n  return 1\n}\n",
    "var_reassigned_list": "fn f() -> Int {\n  var xs = [1]\n  xs = [\"a\"]\n  return 1\n}\n",
    "var_reassigned_in_branch": (
        "fn f(c: Bool) -> Int {\n  var x = 1\n  if (c) {\n    x = true\n  }\n  return x\n}\n"),
    "var_reassigned_in_test": "test \"t\" {\n  var x = 1\n  x = \"s\"\n  assert true\n}\n",
    "if_untyped_let": "fn f(c: Bool) -> Int {\n  let r = if (c) { 1 } else { \"s\" }\n  return 1\n}\n",
    "if_typed_let": "fn f(c: Bool) -> Int {\n  let r: Int = if (c) { 1 } else { \"s\" }\n  return r\n}\n",
    "if_return": "fn f(c: Bool) -> Int {\n  return if (c) { 1 } else { \"s\" }\n}\n",
    "if_argument": ("fn g(x: Int) -> Int { return x }\n"
                   "fn f(c: Bool) -> Int {\n  return g(if (c) { 1 } else { \"s\" })\n}\n"),
    "if_lists": "fn f(c: Bool) -> Int {\n  let r = if (c) { [1] } else { [\"s\"] }\n  return 1\n}\n",
    "ternary": "fn f(c: Bool) -> Int {\n  let r = c ? 1 : \"s\"\n  return 1\n}\n",
    "if_in_test": "test \"t\" {\n  let r = if (true) { 1 } else { \"s\" }\n  assert true\n}\n",
}

_ISSUE_2405_ADMITTED = {
    "var_same_type": "fn f() -> Int {\n  var x = 1\n  x = 2\n  return x\n}\n",
    "var_widening": "fn f() -> Float {\n  var x = 1.5\n  x = 2\n  return x\n}\n",
    "var_accumulator": "fn f() -> Int {\n  var xs = []\n  xs = xs.push(1)\n  return xs.length()\n}\n",
    "var_optional": "fn f() -> Int {\n  var o: Opt[Int] = None\n  o = Some(1)\n  return 1\n}\n",
    "if_same_type": "fn f(c: Bool) -> Int {\n  let r = if (c) { 1 } else { 2 }\n  return r\n}\n",
    "if_widening": "fn f(c: Bool) -> Float {\n  let r = if (c) { 1 } else { 2.5 }\n  return r\n}\n",
    "if_optional": "fn f(c: Bool) -> Int {\n  let r = if (c) { Some(1) } else { None }\n  return 1\n}\n",
}


@pytest.mark.parametrize("tier", ["py", "go", "java", "rust"])
@pytest.mark.parametrize("name", sorted(_ISSUE_2405_REFUSED))
def test_a_mistyped_var_or_if_draws_the_reference_refusal(compile_rvl, name, tier):
    text = _ISSUE_2405_REFUSED[name]
    want = _reference_refusal(text)
    assert want != ""
    got = compile_rvl["compile_to_at"](text, tier, "x.rvl")
    assert got.startswith("REFUSED|"), got[:120]
    assert got.split("|", 2)[2] == want


@pytest.mark.parametrize("name", sorted(_ISSUE_2405_ADMITTED))
def test_a_well_typed_var_or_if_is_admitted_on_both_sides(admit, name):
    text = _ISSUE_2405_ADMITTED[name]
    assert _reference_refusal(text) == ""
    assert admit(text) == ""


# Issue #2447: the reference refuses a variant case call with the wrong number
# of payload arguments. The documents are the reference's own table,
# tests/test_case_call_arity.py; `selfhost/*.rvl` does not count the arguments
# yet. Each entry records what the native chain gives on py, so a difference
# is NAMED rather than passing for agreement: "EMITS" is a silent admit (the
# native chain writes target code), anything else is its verdict string. The
# three generic documents are loud refusals, but with another message (the
# bare head `Mb`, issue #2402). The day the self-host gives the reference's
# refusal, the entry fails and is DELETED.
CASE_ARITY_NATIVE_VERDICTS: dict[str, str] = {
    "argument_to_nullary": "EMITS",
    "argument_to_nullary_as_argument": "EMITS",
    "err_missing": "EMITS",
    "extra_payload": "EMITS",
    "extra_payload_in_let": "EMITS",
    "generic_argument_to_nullary":
        "REFUSED|T1|this function's return expects `Mb[Int]`, got `Mb`",
    "generic_extra_payload":
        "REFUSED|T1|this function's return expects `Mb[Int]`, got `Mb`",
    "generic_missing_payload":
        "REFUSED|T1|this function's return expects `Mb[Int]`, got `Mb`",
    "missing_payload": "EMITS",
    "none_argument": "EMITS",
    "provide_block_body": "EMITS",
    "provide_expression_body": "EMITS",
    "ok_extra": "EMITS",
    "some_extra": "EMITS",
    "some_missing": "EMITS",
    "user_ok_missing": "EMITS",
}


def _case_arity_docs():
    from _load_by_path import load_by_path
    return load_by_path("test_case_call_arity",
                        ROOT / "tests" / "test_case_call_arity.py")


def test_every_case_arity_refusal_has_a_native_verdict():
    assert sorted(CASE_ARITY_NATIVE_VERDICTS) == sorted(_case_arity_docs().REFUSED)


@pytest.mark.parametrize("name", sorted(CASE_ARITY_NATIVE_VERDICTS))
def test_case_arity_refusal_is_a_recorded_native_difference(compile_to, name):
    source, message = _case_arity_docs().REFUSED[name]
    with pytest.raises(RevlError) as refused:
        compile_source(source)
    assert message in str(refused.value)
    got = compile_to(source, "py")
    verdict = ("EMITS" if not got.startswith(("REFUSED|", "UNKNOWN_TIER|"))
               else got)
    assert verdict == CASE_ARITY_NATIVE_VERDICTS[name], (
        f"{name}: recorded as {CASE_ARITY_NATIVE_VERDICTS[name]!r}, now "
        f"{verdict[:160]!r}. If the self-host now gives the reference's "
        f"refusal, DELETE the entry.")


# Issue #2483: the reference checks an effect call's arguments against the
# effect's declared parameter types, a case payload nested in them included.
# The documents are tests/test_423_component_body_call_checking.py's
# `EFFECT_ARG_REFUSED`. The self-host already gives the reference's refusal for
# a wrong-typed service argument; the rest it admits, so each is recorded as
# the native verdict on py ("EMITS" is a silent admit). The day the self-host
# gives the reference's refusal, the entry fails and is DELETED. A wrong
# payload type is issue #2448; a wrong payload count and a wrong-typed built-in
# case in an effect argument are issue #2489.
EFFECT_ARG_NATIVE_AGREES = ("service_argument_type",)
EFFECT_ARG_NATIVE_VERDICTS: dict[str, str] = {
    # issue #2489
    "acquire_err_payload_type": "EMITS",
    "acquire_ok_payload_type": "EMITS",
    "acquire_some_payload_type": "EMITS",
    "service_argument_to_nullary": "EMITS",
    "service_payload_extra": "EMITS",
    "service_payload_missing": "EMITS",
    # issue #2448
    "service_payload_in_provide_method": "EMITS",
    "service_payload_type": "EMITS",
    "service_some_payload_type": "EMITS",
}


def _effect_arg_docs():
    from _load_by_path import load_by_path
    return load_by_path(
        "test_423_component_body_call_checking",
        ROOT / "tests" / "test_423_component_body_call_checking.py")


def test_every_effect_arg_refusal_has_a_native_verdict():
    recorded = [*EFFECT_ARG_NATIVE_VERDICTS, *EFFECT_ARG_NATIVE_AGREES]
    assert sorted(recorded) == sorted(_effect_arg_docs().EFFECT_ARG_REFUSED)


@pytest.mark.parametrize("name", sorted(EFFECT_ARG_NATIVE_VERDICTS))
def test_effect_arg_refusal_is_a_recorded_native_difference(compile_to, name):
    source, message = _effect_arg_docs().EFFECT_ARG_REFUSED[name]
    with pytest.raises(RevlError) as refused:
        compile_source(source)
    assert message in str(refused.value)
    got = compile_to(source, "py")
    verdict = ("EMITS" if not got.startswith(("REFUSED|", "UNKNOWN_TIER|"))
               else got)
    assert verdict == EFFECT_ARG_NATIVE_VERDICTS[name], (
        f"{name}: recorded as {EFFECT_ARG_NATIVE_VERDICTS[name]!r}, now "
        f"{verdict[:160]!r}. If the self-host now gives the reference's "
        f"refusal, DELETE the entry.")


@pytest.mark.parametrize("name", EFFECT_ARG_NATIVE_AGREES)
def test_effect_arg_refusal_native_agrees(compile_to, name):
    source, message = _effect_arg_docs().EFFECT_ARG_REFUSED[name]
    assert compile_to(source, "py") == f"REFUSED|T1|{message}"


# Issue #2483 also moved the reference's provide-method verdict on a
# wrong-typed `Some`/`Ok` return to the `fn` body's. The self-host already gave
# that refusal, so the two now agree; a change on either side goes red here.
@pytest.mark.parametrize(
    "name", sorted(_effect_arg_docs().PROVIDE_RETURN_REFUSED))
def test_provide_return_refusal_native_agrees(compile_to, name):
    source, _twin, message = _effect_arg_docs().PROVIDE_RETURN_REFUSED[name]
    reference = _reference_refusal(source)
    assert message in reference
    assert compile_to(source, "py") == f"REFUSED|T1|{reference}"


# Issue #2490: a provide-method parameter or local named `Some`/`Ok` is the
# callee of `Some(1)`, in both strata and in both compilers. The documents are
# tests/test_2490_provide_local_constructor_name.py's: the native compile of
# the admitted program is the reference's, byte for byte, and a wrong call on
# the local meets the reference's refusal, since the self-host checker reads a
# local of function type ahead of the case table as the reference does.


def _local_callee_docs():
    from _load_by_path import load_by_path
    return load_by_path(
        "test_2490_provide_local_constructor_name",
        ROOT / "tests" / "test_2490_provide_local_constructor_name.py")


def test_a_local_constructor_name_callee_compiles_natively_as_the_reference(
        compile_to, reference_emit):
    source = _local_callee_docs().PROVIDE
    got = compile_to(source, "py")
    assert not got.startswith(("REFUSED|", "UNKNOWN_TIER|")), got[:200]
    assert got == reference_emit["py"](compile_source(source, "t.rvl"))


@pytest.mark.parametrize(
    "name", sorted(_local_callee_docs().LOCAL_CALLEE_REFUSED))
def test_local_callee_refusal_native_agrees(compile_to, name):
    source, _twin, message = _local_callee_docs().LOCAL_CALLEE_REFUSED[name]
    reference = _reference_refusal(source)
    assert message in reference
    assert compile_to(source, "py") == f"REFUSED|T1|{reference}"
