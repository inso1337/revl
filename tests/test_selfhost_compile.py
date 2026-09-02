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


def _load_reference_emit(tier: str):
    """The reference emitter for a tier, loaded by path — the exact file the
    self-host emitter mirrors, and the ground truth for ``compile to <tier>``."""
    subdir = {"py": "python", "rust": "rust", "ts": "typescript"}[tier]
    spec = importlib.util.spec_from_file_location(
        "ref_emit_" + tier, ROOT / "backends" / subdir / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
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
    return {"py": _load_reference_emit("py"),
            "rust": _load_reference_emit("rust"),
            "ts": _load_reference_emit("ts")}


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
    "adt.rvl",
]
RUST_FUNCTION_DOCS = [
    "arith.rvl", "control.rvl", "lists.rvl", "strings.rvl", "variants.rvl",
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
# IR, and the ts tier now LOWERS it — `out = out.push(f(x))` renders
# `out.push(f(x))` where the binding owns its object at that write.
# `selfhost/lower.rvl` has no such stage: it streams tokens straight to IR JSON
# in one pass, and the analysis is a forward dataflow with a fixpoint over each
# loop back edge, which needs the statement tree the streaming producer never
# builds. So the NATIVE IR carries no marker, the native chain emits the copying
# form the reference used to emit, and the two diverge on any document with an
# in-place accumulation loop.
#
# Recorded as a NAMED strict-xfail on the terms tests/test_selfhost_lower_ir.py
# records the same gap (`UNIQUE_MARKER_GAP`) — red for a stated reason, XPASSing
# loudly the day the pass is ported — rather than dropped from the corpus. The
# reference-IR oracle for the same tier (tests/test_selfhost_emit_ts.py) is
# unaffected and stays green: `selfhost/emit_ts.rvl` READS the marker, and that
# oracle feeds it the reference IR, which carries it.
NATIVE_UNIQUE_MARKER_GAP = {
    ("ts", "transforms.rvl"): (
        "item 445: selfhost/lower.rvl has no unique-ownership stage, so the "
        "native IR carries no `unique` marker and the ts tier's in-place "
        "lowering (item 435 (d)) does not fire on the native chain"
    ),
}

NATIVE_CORPUS_PARAMS = [
    pytest.param(
        tier, subdir, name,
        marks=pytest.mark.xfail(
            strict=True, reason=NATIVE_UNIQUE_MARKER_GAP[(tier, name)]))
    if (tier, name) in NATIVE_UNIQUE_MARKER_GAP else pytest.param(tier, subdir, name)
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


def test_three_way_composition_co_compiles(compile_rvl):
    """SEAM 2 is closed: lower.rvl + emit_py.rvl + emit_rust.rvl ``use``d together
    in compile.rvl compile into ONE artifact whose namespace exposes the driver.
    (If the composition failed — a duplicate public ``emit_src``, a leaked private
    ``Ctx``, a colliding test name — the ``compile_rvl`` fixture would have raised.)"""
    assert callable(compile_rvl["compile_to"])
    assert callable(compile_rvl["admit"])


def test_native_gate_admits_the_whole_emit_surface(admit):
    """The native frontend's admission verdict covers the ENTIRE emitter surface —
    every corpus document (functions AND components, both tiers) the reference
    admits, the native gate admits (``""``). The component documents are now also
    compiled byte-exact end to end (item 262); this checks the gate itself over the
    full admitted surface, including documents outside the byte-exact emit slice."""
    for subdir in ("emit_py_corpus", "emit_rust_corpus", "emit_ts_corpus"):
        for path in sorted((ROOT / "tests" / "fixtures" / subdir).glob("*.rvl")):
            compile_files([str(path)])  # the reference admits it
            verdict = admit(path.read_text(encoding="utf-8"))
            assert verdict == "", f"{subdir}/{path.name}: {verdict!r}"


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
    the pipeline covered it."""
    assert compile_to("fn id(x: Int) -> Int { return x }", "go") == "UNKNOWN_TIER|go"
    assert compile_to("fn id(x: Int) -> Int { return x }", "java") == "UNKNOWN_TIER|java"
    assert compile_to("fn id(x: Int) -> Int { return x }", "wasm") == "UNKNOWN_TIER|wasm"


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
