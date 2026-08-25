"""THE CAPSTONE (roadmap item 224): the integrated native ``revl_compile`` —
selfhost/compile.rvl composed with the native emitters, proving revl compiles
revl to a target tier, end to end, BYTE-FOR-BYTE against the reference.

This test assembles the self-host pipeline from its native artifacts and holds
the composition to the strongest agreement an emitter can be held to — the
emitted target source must equal the reference's to the last byte — on the
function + simple-component surface (the intersection all six stages cover):

    source ──▶ compile.rvl `compile_to`        the native FRONTEND, one artifact:
                                               lexer.rvl → parser.rvl → the
                                               checker → the lowering ADMISSION
                                               gate (lower.rvl). Verdict only.
           ──▶ [interchange IR]                SEAM 1 — produced here by the
                                               reference lowering, because the
                                               native gate yields a VERDICT, not
                                               the IR (lower.rvl is an admission
                                               gate; it does not emit the IR).
           ──▶ emit_py.rvl / emit_rust.rvl     the native EMITTER, a separate
               `emit_src`                       artifact per tier: IR → target
                                               source, revl compiling revl.

What is proven native, end to end, on this surface:
  * the FRONTEND is native and its admission verdict AGREES with the reference on
    the whole emit surface (`test_native_gate_admits_the_emit_surface`), and a
    program the reference REJECTS the native driver also refuses — before any
    emitter runs (`test_refused_program_never_reaches_an_emitter`), the one revl
    promise carried through the composition;
  * the EMITTER is native and BYTE-IDENTICAL to the reference for py AND rust over
    every corpus document (`test_native_pipeline_is_byte_identical`).

The two seams that remain before a single-call in-file native ``source → target``
(both named in selfhost/compile.rvl's header, each a concrete follow-up):
  * SEAM 1 — lower.rvl must also EMIT the interchange IR, not only the verdict;
    until then the IR between the native front and the native tail is the
    reference's.
  * SEAM 2 — the stage modules do not co-compile into one revl composition (a
    duplicate private ``Ctx``, cross-module ``contains``/``for..of`` type
    interference), so the driver co-compiles only the frontend span and the
    emitters are chained here by the harness rather than by a single ``use``.

Ground truth is the reference; any divergence is a defect in a stage.
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
    subdir = {"py": "python", "rust": "rust"}[tier]
    spec = importlib.util.spec_from_file_location(
        "ref_emit_" + tier, ROOT / "backends" / subdir / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.emit


@pytest.fixture(scope="module")
def compile_rvl() -> dict:
    return _exec_selfhost("selfhost/compile.rvl")


@pytest.fixture(scope="module")
def compile_to(compile_rvl):
    """The capstone driver's front half: source + tier -> "ADMITTED|<tier>" |
    "REFUSED|<TAG>|<message>" | "UNKNOWN_TIER|<tier>"."""
    return compile_rvl["compile_to"]


@pytest.fixture(scope="module")
def native_emit() -> dict:
    """The native emitters, one artifact per tier (SEAM 2: they cannot be
    co-compiled, so each is compiled on its own, exactly as its own self-host
    test compiles it)."""
    return {
        "py": _exec_selfhost("selfhost/emit_py.rvl")["emit_src"],
        "rust": _exec_selfhost("selfhost/emit_rust.rvl")["emit_src"],
    }


@pytest.fixture(scope="module")
def reference_emit() -> dict:
    return {"py": _load_reference_emit("py"), "rust": _load_reference_emit("rust")}


# ---------------------------------------------------------------- corpus

# The function + simple-component surface — the checked-in emitter corpora, which
# are exactly the documents the self-host emitters are held byte-exact on. Each
# is (tier, relative-path); the fixture directory differs per tier.
def _corpus(tier: str, subdir: str) -> list[tuple[str, str]]:
    d = ROOT / "tests" / "fixtures" / subdir
    return [(tier, f.name) for f in sorted(d.glob("*.rvl"))]


CORPUS = _corpus("py", "emit_py_corpus") + _corpus("rust", "emit_rust_corpus")
_FIXTURE_DIR = {"py": "emit_py_corpus", "rust": "emit_rust_corpus"}


def _fixture_path(tier: str, name: str) -> Path:
    return ROOT / "tests" / "fixtures" / _FIXTURE_DIR[tier] / name


# ---------------------------------------------------- the byte-exact proof

@pytest.mark.parametrize("tier,name", CORPUS, ids=[f"{t}:{n}" for t, n in CORPUS])
def test_native_pipeline_is_byte_identical(compile_to, native_emit,
                                           reference_emit, tier, name):
    """The whole composed native chain, end to end, on one corpus document:
    the native FRONTEND admits the source, and the native EMITTER's target
    source equals the reference's BYTE-FOR-BYTE.

    (SEAM 1: the interchange IR the emitter consumes is produced by the
    reference lowering, because the native gate yields a verdict, not the IR.)
    """
    path = _fixture_path(tier, name)
    source = path.read_text(encoding="utf-8")

    # front half — the native frontend admits, in revl
    gate = compile_to(source, tier)
    assert gate == f"ADMITTED|{tier}", (
        f"native gate did not admit {tier}:{name}: {gate!r}")

    # the IR the emitter consumes (SEAM 1 — the reference's, for now)
    ir = compile_files([str(path)])

    # tail — the native emitter's bytes == the reference's, exactly
    want = reference_emit[tier](ir)
    got = native_emit[tier](ir)
    assert got == want, (
        f"native emitter diverged from the reference on {tier}:{name}\n"
        f"--- lengths ref={len(want)} got={len(got)} ---")


def test_native_gate_admits_the_emit_surface(compile_to):
    """The native frontend's admission verdict AGREES with the reference over the
    entire emitter surface: every corpus document the reference admits, the
    native gate stamps ``ADMITTED|<tier>``. (The gate's verdict-parity vs the
    reference on rejected programs is proven exhaustively in
    tests/test_selfhost_lower.py; here the claim is that the frontend covers the
    whole surface the emitter tail does.)"""
    for tier, name in CORPUS:
        path = _fixture_path(tier, name)
        # the reference admits every corpus document
        compile_files([str(path)])
        gate = compile_to(path.read_text(encoding="utf-8"), tier)
        assert gate == f"ADMITTED|{tier}", f"{tier}:{name}: {gate!r}"


# --------------------------------------------- the refusal composes too

# Programs the reference REJECTS. The composed driver must refuse them with a
# guarantee tag — and never reach an emitter. (name, source, expected tag.)
# `e.code` is authoritative for G4/A1; G2 carries its "(G2)" message marker.
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
@pytest.mark.parametrize("tier", ["py", "rust"])
def test_refused_program_never_reaches_an_emitter(compile_to, tier, case):
    """A program the reference rejects is refused by the composed native driver
    with the SAME guarantee tag, for every tier — and the ``REFUSED|`` verdict
    means the emitter is never reached (a rejected program cannot be compiled)."""
    name, src, tag = case
    # the reference rejects it with this guarantee
    assert _reference_tag(src) == tag, f"corpus bug: {name}"
    got = compile_to(src, tier)
    assert got.startswith("REFUSED|"), f"{name} ({tier}): {got!r}"
    got_tag = got.split("|")[1]
    assert got_tag == tag, f"{name} ({tier}): tag {got_tag!r} != {tag!r}"


def test_unknown_tier_is_reported(compile_to):
    """A tier outside the proven set is reported, never silently emitted as if
    the pipeline covered it."""
    assert compile_to("fn id(x: Int) -> Int { return x }", "go") == "UNKNOWN_TIER|go"
    assert compile_to("fn id(x: Int) -> Int { return x }", "java") == "UNKNOWN_TIER|java"


def test_compile_rvl_in_file_tests_pass(compile_rvl):
    """The driver's own `test` blocks run under the python backend."""
    tests = compile_rvl.get("REVL_TESTS")
    assert tests and len(tests) >= 4, "expected the driver's test blocks in REVL_TESTS"
    for entry in tests:
        fn = entry[-1] if isinstance(entry, tuple) else entry
        fn()
