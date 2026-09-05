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

Roadmap item 232 extends this to the whole typed-expression SPINE of module
functions:
  * the `functions` section — every module `fn` with its full lowered body
    (statements + the typed-expression tree), byte-identical to the reference
    over the entire covered corpus. This exercises the annotations the IR
    carries and the checker's inference is projected to reproduce: the
    `operands` tag on typed arithmetic (`+ - * / %` and unary `-`), the `recv`
    tag on `to_int`, match-arm `payload_type` (Opt/Result), the arrow's
    resolved `param_types`/`returns`, `builtin`-vs-`call` dispatch (including
    host-root constructors and the stdlib method table), `len`/`index`/`field`/
    `record`/`record_update`/`interp`/`optcall`, and `let`/`var`/`assign`/`if`/
    `while`/`for`/`return` steps. Record-update (`{ r | .. }`) is read at the
    token level because the shared parser's expression grammar does not carry
    it;
  * the `types` section — user record/variant declarations
    (`{Name: {params, kind, fields|cases}}`), byte-identical.

Emitter-readiness is proven end to end for the function corpus: the reference
python emitter applied to the NATIVE IR produces the SAME bytes as applied to
the reference IR, for every function document.

Roadmap items 242 + 241 complete the last mile:
  * item 242 — the FULL typed COMPONENT/method expression body. The `ir_body`
    surface is no longer item 227's simple slice: it lowers the component
    dialect (`{kind:"name",id}` for a scoped name, `{kind:"config",field}` for a
    config read, a `req`-target call, plain `bin`/`un` with no `operands`,
    `builtin`) across let-effect/effect steps, `emit … compensate` sagas,
    `if`+`fail` guards, `every`/`after` timers (with `interval_ms`), and
    `provide` blocks with full method bodies — byte-identical to the reference
    over every component document. A body-level stdlib builtin now bumps
    `ir_version` to v3 (the lowered node is visible to `_has_builtin`), closing
    the last version gap, so ``VERSION_BODY_DEPENDENT`` is empty;
  * item 241 — the `externs` section. The lexer (selfhost/lexer.rvl) grew a
    `hostbody` token capturing the verbatim brace-balanced `@backend` body, so
    each extern's class/params/returns and raw bodies lower byte-identical.

Emitter-readiness is proven end to end for the component AND externs corpus too.
With that, `lower_to_ir` is COMPLETE for the whole covered surface — function,
component, and extern programs — up to the per-component `source` (input
filename) and top-level `manifest` (linker artifact), which are environment/link
artifacts the covered emitter surface never reads.
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

# ir_version triggers the header producer cannot see used to include the one
# body-level trigger (a stdlib-builtin call in a component body bumps the
# reference to v3, `_has_builtin`). Item 242 closes that gap: the now-lowered
# component body makes the `builtin`/`adt` node visible, so the native producer
# bumps v3 too and `ir_version` matches the reference on EVERY corpus document.
VERSION_BODY_DEPENDENT: set[str] = set()

# The function documents whose whole `functions` body is inside the covered
# emitter surface, so the reference python emitter renders the native IR to the
# same bytes as the reference IR (end-to-end emitter-readiness).
FUNCTION_EMIT_READY_DOCS = [
    "arith.rvl", "control.rvl", "strings.rvl", "records.rvl", "result.rvl",
    "optionals.rvl", "floats.rvl", "mixed.rvl", "hostroots.rvl", "types.rvl",
]

# The component documents whose whole activation/method body is now lowered
# byte-exact (item 242): effect/let-effect, emit+compensate sagas, if+fail
# guards, timers, and provide-method bodies over the component dialect. Every
# one carries a `body` and is emitter-ready end to end.
COMPONENT_DOCS = [
    "services_basic.rvl", "services_config.rvl", "services_body.rvl",
    "services_methods.rvl", "services_method_effects.rvl", "services_timers.rvl",
]

# The document whose `externs` section is lowered byte-exact (item 241): the
# verbatim `@py` bodies come from the lexer's new `hostbody` token.
EXTERN_DOCS = ["externs.rvl"]

# item 421 F6 / item 256 §7: the declaration-side `Secret[T]` MARKING, now
# carried by the self-host frontend (`selfhost/lower.rvl`) exactly as
# `src/revl/taint.py` carries it for the reference. `Secret[T]` is a qualifier,
# not a type constructor: it is stripped off every declared type and leaves a
# flag behind, so the native and reference IR agree on both halves — the bare
# `type`/`returns` spelling AND the four stamps a backend reads
# (`params[i]["secret"]`, `secret_return`, `secret_witness`, a config field's
# `secret`). The gap this replaces was strict-xfail on `secrets.rvl`; the
# marking lands with `secrets_nested.rvl` added FIRST and failing. Both
# documents now run through the SAME unmarked projections as every other corpus
# document, which is the only statement of parity worth having.


def _corpus_params(gap: dict[str, str]):
    """The corpus, with the documents a NAMED self-host gap covers marked
    strict-xfail so the gap cannot be forgotten OR silently outlived."""
    return [
        pytest.param(name, marks=pytest.mark.xfail(strict=True, reason=gap[name]))
        if name in gap else name
        for name in CORPUS
    ]


# item 445: the reference frontend now proves UNIQUE OWNERSHIP of an
# accumulation local once (`src/revl/ownership.py`) and stamps the answer on the
# IR — `"unique"` on a self-rebinding `assign`, `"unique_birth"` on the `let`
# that would need the defensive copy — instead of each emitter re-deriving the
# same aliasing rule (the go and python tiers had written it twice). The
# self-host lowering gate has no such stage: `selfhost/lower.rvl` streams tokens
# straight to IR JSON in one pass, and the analysis is a FORWARD DATAFLOW with a
# fixpoint over each loop back edge, which needs the statement tree the streaming
# producer never builds. So the native IR carries no marker and diverges from the
# reference on any document with an in-place accumulation loop — one in this
# corpus, `transforms.rvl`'s `list_map` / `list_filter`.
#
# Marked strict-xfail rather than dropped, on the terms the `Secret[T]` gap
# above was recorded on before it was closed: it is red
# for a NAMED reason and will XPASS loudly the day the pass is ported. The
# emit_py port item 445 DID land is unaffected and stays green — `emit_py.rvl`
# now READS the two markers where it used to carry ~130 lines of its own copy of
# the analysis, and the emitters consume the REFERENCE IR, which carries them.
UNIQUE_MARKER_GAP = {
    "transforms.rvl": (
        "item 445: selfhost/lower.rvl has no unique-ownership stage, so the "
        "native IR carries neither `unique` nor `unique_birth`"
    ),
}

CORPUS_UNIQUE_AWARE = _corpus_params(UNIQUE_MARKER_GAP)

# item 429 / item 386: the extern DECLARATION shapes `witnessed.rvl` carries.
# The `externs`-section half of the gap is CLOSED (`selfhost/lower.rvl`'s
# `ir_extern` now lowers them to the reference's `_lower_externs` shape):
#   * an `undo`/`compensate` clause on an extern lowers its inverse EXPRESSION
#     with the same `expr_at`/`lir_expr` ladder a fn body uses, plus the item-309
#     `undo_idempotent`/`undo_read`/`register` stamps;
#   * a capability tag on a `witnessed`/`emission` class (`witnessed[fs]`,
#     `emission[net]`, item 343) lowers to `capabilities: [...]`;
#   * the `witnessed` class lowers the item-243 transactional descriptor
#     (`entry_kind`/`revertible`/`ok_conditional`/`witness`).
# The REMAINING half is the admission gate's parse refusal of the `witnessed`
# class and the capability tag (`BAD|expected fn after extern`) — a FALSE REFUSAL
# recorded on the gate side in tests/test_selfhost_compile.py's NATIVE_GATE_GAPS.
# `lower_to_ir` runs additive to and independent of the gate, so the externs
# section is now byte-exact against the reference even while the gate half stands.
EXTERN_DECL_GAP: dict[str, str] = {}

CORPUS_EXTERN_AWARE = _corpus_params(EXTERN_DECL_GAP)


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


def test_native_ir_preserves_function_visibility(lower_to_ir):
    source = (
        "pub fn exported(n: Int) -> Int { return n + 1 }\n"
        "fn internal(n: Int) -> Int { return n - 1 }\n"
    )
    reference = compile_source(source)["functions"]
    native = json.loads(lower_to_ir(source))["functions"]
    assert [fn["public"] for fn in reference] == [True, False]
    assert native == reference


@pytest.mark.parametrize("prefix", ["", "pub "])
@pytest.mark.parametrize("signature", ["(n: Int) -> Int", "()"])
def test_native_ir_preserves_plain_function_cache(lower_to_ir, prefix, signature):
    body = "return n + 1" if "n:" in signature else ""
    source = (
        f"{prefix}fn cached{signature} cache pure {{ {body} }}\n"
        f"fn plain{signature} {{ {body} }}\n"
    )
    reference = compile_source(source)["functions"]
    native = json.loads(lower_to_ir(source))["functions"]
    assert reference[0]["cache"] == {"class": "pure_fn"}
    assert "cache" not in reference[1]
    assert native == reference


def test_native_ir_infers_nominal_record_field_operands(lower_to_ir):
    source = (
        "type Outer = { child: Pair, length: Float }\n"
        "type Pair = { x: Int, y: Int }\n"
        "fn sum(p: Outer, ps: List[Pair]) -> Int {\n"
        "  let pair = p.child\n"
        "  return pair.x + ps[0].y\n"
        "}\n"
        "fn widened(p: Outer) -> Float { return p.length + p.child.x }\n"
    )
    reference = compile_source(source)["functions"]
    native = json.loads(lower_to_ir(source))["functions"]
    assert reference[0]["body"][-1]["expr"]["operands"] == "Int"
    assert reference[1]["body"][-1]["expr"]["operands"] == "Float"
    assert native == reference


@pytest.mark.parametrize("source", [
    "fn caller(x: Int) -> Int { let f = later return f(x) + 1 }\n"
    "pub fn later(x: Int) -> Int { return x }\n",
    "fn caller(f: ((Int) -> Int) -> Int, g: (Int) -> Int) -> Int {\n"
    "  return f(g) + g(1)\n"
    "}\n",
    "fn caller(f: () -> (() -> Int)) -> Int { return f()() + 1 }\n",
    "type Handler = { run: () -> Int }\n"
    "fn caller(h: Handler) -> Int { let f = h.run return f() + 1 }\n",
])
def test_native_ir_infers_callable_results(lower_to_ir, source):
    reference = compile_source(source)["functions"]
    native = json.loads(lower_to_ir(source))["functions"]
    assert reference[0]["body"][-1]["expr"]["operands"] == "Int"
    assert native == reference


@pytest.mark.parametrize("actual,expected", [
    ("Int", "Float"), ("Int32", "Float"), ("Int32", "Int"),
])
def test_native_ir_marks_numeric_return_widening(lower_to_ir, actual, expected):
    source = f"fn widened(x: {actual}) -> {expected} {{ return x }}"
    reference = compile_source(source)["functions"]
    native = json.loads(lower_to_ir(source))["functions"]
    assert reference[0]["body"][0]["expr"]["widen"] == expected
    assert native == reference


@pytest.mark.parametrize("body", [
    "return Map.empty()",
    "let m: Map[Str, Int] = Map.empty() return m",
])
def test_native_ir_lowers_empty_map(lower_to_ir, body):
    source = f"fn empty_map() -> Map[Str, Int] {{ {body} }}"
    assert json.loads(lower_to_ir(source))["functions"] == compile_source(source)["functions"]


@pytest.mark.parametrize("modifiers", [
    "emission idempotent", "idempotent emission",
    "async emission idempotent", "idempotent emission[db] async",
])
def test_native_ir_preserves_idempotent_service_methods(lower_to_ir, modifiers):
    source = f"service Store {{ {modifiers} fn put(value: Int) -> Int }}"
    native = json.loads(lower_to_ir(source))
    reference = compile_source(source)
    assert native["services"] == reference["services"]
    assert native["ir_version"] == reference["ir_version"] == 3


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
    """Where the native producer emits a component `body` (item 242: the FULL
    typed component/method expression spine), it is byte-identical to the
    reference body."""
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


@pytest.mark.parametrize("rel", COMPONENT_DOCS)
def test_component_docs_emit_full_body(lower_to_ir, rel):
    """Item 242 — every component in the covered corpus now carries a `body`,
    byte-identical to the reference (not merely the simple-component slice): the
    activation/method spine (let-effect, effect, emit/compensate sagas, if+fail
    guards, timers, provide-method bodies) is complete."""
    src = (CORPUS_DIR / rel).read_text()
    native = json.loads(lower_to_ir(src))
    reference = compile_source(src)
    ref_by_name = {c["name"]: c for c in reference["components"]}
    assert native["components"], rel
    for comp in native["components"]:
        assert "body" in comp, f"{rel}:{comp['name']} lost its body"
        assert comp["body"] == ref_by_name[comp["name"]]["body"], comp["name"]


@pytest.mark.parametrize("rel", CORPUS_EXTERN_AWARE)
def test_native_ir_matches_reference_externs(lower_to_ir, rel):
    """The `externs` section (item 241) — each extern's class/params/returns and
    the verbatim `@backend` bodies (from the lexer's `hostbody` token) — is byte-
    identical to the reference IR on every corpus document (absent together on
    the extern-free documents)."""
    src = (CORPUS_DIR / rel).read_text()
    native = json.loads(lower_to_ir(src))
    reference = compile_source(src)
    assert native.get("externs") == reference.get("externs")


def test_native_component_and_extern_ir_is_emitter_ready(lower_to_ir):
    """End-to-end: the reference python emitter applied to the NATIVE IR produces
    the SAME bytes as applied to the reference IR, for every component document
    AND the externs document — the native component/method body and externs IR is
    emitter-ready, the completion proof for component programs (item 230)."""
    refemit = _reference_emit()
    stub = types.ModuleType("runtime")
    stub.__getattr__ = lambda name: (lambda *a, **k: None)
    had = "runtime" in sys.modules
    previous = sys.modules.get("runtime")
    sys.modules["runtime"] = stub
    try:
        for rel in COMPONENT_DOCS + EXTERN_DOCS:
            src = (CORPUS_DIR / rel).read_text()
            reference_ir = compile_source(src)
            native_ir = json.loads(lower_to_ir(src))
            assert refemit.emit(native_ir) == refemit.emit(reference_ir), rel
    finally:
        if had:
            sys.modules["runtime"] = previous
        else:
            del sys.modules["runtime"]


@pytest.mark.parametrize("rel", CORPUS_UNIQUE_AWARE)
def test_native_ir_matches_reference_functions(lower_to_ir, rel):
    """The `functions` section — every module `fn` with its full lowered body
    (statements + the typed-expression tree) — is byte-identical to the
    reference IR on every corpus document (absent together on the component-only
    documents)."""
    src = (CORPUS_DIR / rel).read_text()
    native = json.loads(lower_to_ir(src))
    reference = compile_source(src)
    assert native.get("functions") == reference.get("functions")


@pytest.mark.parametrize("rel", CORPUS)
def test_native_ir_matches_reference_types(lower_to_ir, rel):
    """The `types` section — user record/variant declarations — is byte-
    identical to the reference IR on every corpus document."""
    src = (CORPUS_DIR / rel).read_text()
    native = json.loads(lower_to_ir(src))
    reference = compile_source(src)
    assert native.get("types") == reference.get("types")


def test_native_function_ir_is_emitter_ready(lower_to_ir):
    """End-to-end: the reference python emitter applied to the NATIVE IR
    produces the SAME bytes as applied to the reference IR, for every function
    document — the native `functions`/`types` IR is emitter-ready."""
    refemit = _reference_emit()
    stub = types.ModuleType("runtime")
    stub.__getattr__ = lambda name: (lambda *a, **k: None)
    had = "runtime" in sys.modules
    previous = sys.modules.get("runtime")
    sys.modules["runtime"] = stub
    try:
        for rel in FUNCTION_EMIT_READY_DOCS:
            src = (CORPUS_DIR / rel).read_text()
            reference_ir = compile_source(src)
            native_ir = json.loads(lower_to_ir(src))
            assert refemit.emit(native_ir) == refemit.emit(reference_ir), rel
    finally:
        if had:
            sys.modules["runtime"] = previous
        else:
            del sys.modules["runtime"]


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
