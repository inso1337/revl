"""SEAM 1 (roadmap item 227): the self-hosted lowering gate (selfhost/lower.rvl)
now also PRODUCES the emittable interchange IR, not only the admission verdict.

`lower.rvl::lower_to_ir(src)` is compiled by revl, emitted through the python
backend, executed, and cross-checked STRUCTURALLY against the reference lowering
(`revl.compile_source`, i.e. src/revl/lower.py's `check_and_lower`) over the
emit_py corpus — the exact covered surface the self-host emitters consume. This
is the producer half of what lets item 224's `compile.rvl` drop the reference
`compile_source` from the middle of the native pipeline.

The gate's IR is compared where the native producer covers it:
  * the SERVICES table — byte-identical on every corpus document (methods with
    params/returns/emission, + capabilities for a scoped `emission[..]`, +
    async);
  * each COMPONENT's declaration header — config/requires/provides, byte-
    identical;
  * the component BODY for the simple-component surface (effect/undo/provide over
    required-service calls + literals) — emitted only for the fixtures that stay
    inside that surface (services_basic), and byte-identical there;
  * `ir_version` — from the header-visible feature triggers. One reference
    trigger is body-level only (a stdlib builtin in a component/method body bumps
    the reference to v3, `_has_builtin`), which the header producer cannot see;
    that single corpus case (services_methods) is listed in
    ``VERSION_BODY_DEPENDENT`` and its version is asserted to under-approximate to
    1 rather than match.

The strongest proof is emitter-readiness: the reference python emitter
(backends/python/emit.py) applied to the NATIVE IR must produce the SAME bytes
as when applied to the reference IR (services_basic) — the native IR is emitter-
ready end to end. `source`/`manifest` are environment/link artifacts the covered
emitter surface does not read, so they are outside the projection.

Deferred (reported for the full capstone): the `functions`/`externs`/`types`
sections and the typed component/method expression body.
"""

import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_files, compile_source  # noqa: E402

CORPUS_DIR = ROOT / "tests" / "fixtures" / "emit_py_corpus"

# every emit_py corpus document — the covered surface (functions, types,
# externs, and the component/service documents)
CORPUS = sorted(p.name for p in CORPUS_DIR.glob("*.rvl"))

# ir_version triggers the header producer cannot see: a stdlib-builtin call in a
# component/method body bumps the reference to v3 (`_has_builtin`); the native
# producer under-approximates to 1. The only such corpus document.
VERSION_BODY_DEPENDENT = {"services_methods.rvl"}


# ---------------------------------------------------------------- harness

def _exec_emitted() -> dict:
    ir = compile_files([str(ROOT / "selfhost" / "lower.rvl")])
    assert ir["ir_version"] == 3
    spec = importlib.util.spec_from_file_location(
        "pyemit_selfhost_lower_ir", ROOT / "backends" / "python" / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    stub = types.ModuleType("runtime")
    stub.__getattr__ = lambda name: (lambda *a, **k: None)  # PEP 562
    had = "runtime" in sys.modules
    previous = sys.modules.get("runtime")
    sys.modules["runtime"] = stub
    try:
        namespace = {}
        exec(compile(module.emit(ir), "selfhost_lower_ir.py", "exec"), namespace)
    finally:
        if had:
            sys.modules["runtime"] = previous
        else:
            del sys.modules["runtime"]
    return namespace


@pytest.fixture(scope="module")
def ns():
    return _exec_emitted()


@pytest.fixture(scope="module")
def lower_to_ir(ns):
    return ns["lower_to_ir"]


def _reference_emit():
    spec = importlib.util.spec_from_file_location(
        "pyemit_reference_lower_ir", ROOT / "backends" / "python" / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _headers(components):
    """The declaration-header projection of a component list."""
    return [
        {"name": c["name"], "config": c["config"],
         "requires": c["requires"], "provides": c["provides"]}
        for c in components
    ]


def test_selfhosted_lower_ir_in_file_tests_pass(ns):
    """The .rvl file's own `test` blocks (incl. the new lower_to_ir cases, which
    pin the exact JSON) run — and pass — under the python backend."""
    tests = ns.get("REVL_TESTS")
    assert tests and len(tests) >= 16, "expected the file's test blocks in REVL_TESTS"
    lower_to_ir_cases = [name for name, _ in tests if "lower_to_ir" in name]
    assert len(lower_to_ir_cases) >= 4, lower_to_ir_cases
    for name, fn in tests:
        fn()  # the block's asserts fire here; a failure raises


@pytest.mark.parametrize("rel", CORPUS)
def test_native_ir_matches_reference_services(lower_to_ir, rel):
    """The SERVICES table is byte-identical to the reference IR on every corpus
    document (the empty `{}` on function/type/extern-only documents included)."""
    src = (CORPUS_DIR / rel).read_text()
    native = json.loads(lower_to_ir(src))
    reference = compile_source(src)
    assert native["services"] == reference["services"]


@pytest.mark.parametrize("rel", CORPUS)
def test_native_ir_matches_reference_component_headers(lower_to_ir, rel):
    """Each component's config/requires/provides header is byte-identical."""
    src = (CORPUS_DIR / rel).read_text()
    native = json.loads(lower_to_ir(src))
    reference = compile_source(src)
    assert _headers(native["components"]) == _headers(reference["components"])


@pytest.mark.parametrize("rel", CORPUS)
def test_native_ir_matches_reference_bodies_where_covered(lower_to_ir, rel):
    """Where the native producer emits a component `body` (the simple-component
    surface), it is byte-identical to the reference body."""
    src = (CORPUS_DIR / rel).read_text()
    native = json.loads(lower_to_ir(src))
    reference = compile_source(src)
    ref_by_name = {c["name"]: c for c in reference["components"]}
    covered = 0
    for comp in native["components"]:
        if "body" in comp:
            covered += 1
            assert comp["body"] == ref_by_name[comp["name"]]["body"], comp["name"]
    if rel == "services_basic.rvl":
        # the capstone-intersection document: both its components carry a body
        assert covered == 2


@pytest.mark.parametrize("rel", CORPUS)
def test_native_ir_version(lower_to_ir, rel):
    """`ir_version` matches the reference, except the one body-builtin document
    the header producer cannot see (it under-approximates to 1)."""
    src = (CORPUS_DIR / rel).read_text()
    native = json.loads(lower_to_ir(src))
    reference = compile_source(src)
    if rel in VERSION_BODY_DEPENDENT:
        assert native["ir_version"] == 1 and reference["ir_version"] == 3
    else:
        assert native["ir_version"] == reference["ir_version"]


def test_native_ir_is_emitter_ready(lower_to_ir):
    """The strongest proof: the reference python emitter applied to the NATIVE IR
    produces the SAME bytes as applied to the reference IR (services_basic) — the
    native IR is emitter-ready end to end."""
    refemit = _reference_emit()
    stub = types.ModuleType("runtime")
    stub.__getattr__ = lambda name: (lambda *a, **k: None)
    had = "runtime" in sys.modules
    previous = sys.modules.get("runtime")
    sys.modules["runtime"] = stub
    try:
        src = (CORPUS_DIR / "services_basic.rvl").read_text()
        reference_ir = compile_source(src)
        native_ir = json.loads(lower_to_ir(src))
        from_reference = refemit.emit(reference_ir)
        from_native = refemit.emit(native_ir)
    finally:
        if had:
            sys.modules["runtime"] = previous
        else:
            del sys.modules["runtime"]
    assert from_native == from_reference
