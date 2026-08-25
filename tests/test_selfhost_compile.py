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

The boundary (honest scope):
  * FULLY NATIVE, byte-exact: the module-function + type surface — the emitter-ready
    documents item 232 proved ``lower_to_ir`` byte-exact on (py corpus), and the
    pure-function/type/variant slice of the rust corpus the rust emitter covers.
  * STILL NEEDS THE REFERENCE: the typed COMPONENT/method expression BODY. The
    native IR producer covers only the simple-component slice (item 227); the
    general component body is the item-242 gap. For those documents the native gate
    is still exercised (the refusal is native), but the component-body BYTES are not
    yet held byte-exact — verified here only that the gate admits them, parity with
    the reference.

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
    subdir = {"py": "python", "rust": "rust"}[tier]
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
    return {"py": _load_reference_emit("py"), "rust": _load_reference_emit("rust")}


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
]
RUST_FUNCTION_DOCS = [
    "arith.rvl", "control.rvl", "lists.rvl", "strings.rvl", "variants.rvl",
]

NATIVE_CORPUS = (
    [("py", "emit_py_corpus", n) for n in PY_FUNCTION_DOCS]
    + [("rust", "emit_rust_corpus", n) for n in RUST_FUNCTION_DOCS]
)


def _fixture_path(subdir: str, name: str) -> Path:
    return ROOT / "tests" / "fixtures" / subdir / name


# ---------------------------------------- the fully-native byte-exact proof

@pytest.mark.parametrize(
    "tier,subdir,name", NATIVE_CORPUS,
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
    admits, the native gate admits (``""``). This includes the component documents
    whose BODIES still need the reference (item 242): the gate is native for them
    even though their emitted bytes are not yet byte-exact."""
    for subdir in ("emit_py_corpus", "emit_rust_corpus"):
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
@pytest.mark.parametrize("tier", ["py", "rust"])
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


def test_compile_rvl_in_file_tests_pass(compile_rvl):
    """The composed artifact's own `test` blocks run under the python backend —
    including the driver's four (unsupported tier, native refusal, fully-native
    py/rust) and every block the three co-compiled stages contribute."""
    tests = compile_rvl.get("REVL_TESTS")
    assert tests and len(tests) >= 4, "expected the driver's test blocks in REVL_TESTS"
    for entry in tests:
        fn = entry[-1] if isinstance(entry, tuple) else entry
        fn()
