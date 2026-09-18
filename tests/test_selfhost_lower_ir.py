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

Issue #957 adds the `Map` SUBSCRIPT region. The reference frontend lowers a
subscript from the CONTAINER's declared type and stamps an `index` node on a
`Map[K, V]` with `key_type`/`value_type`; the self-host lowering did not, and
this oracle did not notice, because the corpus it globs indexed no `Map` at all.
`maps.rvl` is that document — keyed reads (variable key, literal key, a read
used as the next read's key, nested maps, a map reached through a record field)
next to the `List` reads that must carry neither annotation.

Roadmap item 391 (issue #106) closes the rest of that class. #957 was one
instance of a general shape: the reference frontend annotates the IR from
knowledge no backend has, the self-host lowering does not follow, and the oracle
stays green because the globbed corpus never exercises the path. Running both
frontends over every single-file `.rvl` in the tree (410 documents, not the 36
here) measured 53 such divergences; five families are closed with a corpus
document each, which is the half that stops them regressing:

  * `extern_neighbours.rvl` — the declaration written AFTER an extern. A `pub fn`
    lowered PRIVATE and a `type` dropped out of the `types` section, because
    `p_extern`'s body skip decides where every top-level walker resumes and its
    stop set named only the heads the admission gate reads. Every extern in this
    corpus sat last in its file.
  * `annotated_lets.rvl` — the two markers a declared type puts on its value:
    `_pin_empty_literal` (the annotation threaded onto an EMPTY `List`, which the
    checker types at bottom) and `_mark_widen` at the `let` position. Plus
    `Map.lookup`, the one `_BUILTIN_SIG` row the self-host method table did not
    answer.
  * `adt_inference.rvl` — a `match` whose scrutinee is a constructor APPLICATION
    or a bare nullary case, which the self-host's `infer` typed as unknown, so
    every arm over it lost its `payload_type`.
  * `async_colour.rvl` — the whole-program async colour on a fn entry, the stamp
    an emitter reads to render `async`/`await`.
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
    "maps.rvl", "annotated_lets.rvl", "adt_inference.rvl",
    "extern_neighbours.rvl", "async_colour.rvl",
    "generics.rvl", "variant_multiline.rvl", "else_if.rvl", "arrows.rvl",
    "async_arrow_arg.rvl",
    "events.rvl", "braceless.rvl", "record_writes.rvl", "reserved_keys.rvl",
    "destructure.rvl", "branch_values.rvl",
]

# The component documents whose whole activation/method body is now lowered
# byte-exact (item 242): effect/let-effect, emit+compensate sagas, if+fail
# guards, timers, and provide-method bodies over the component dialect. Every
# one carries a `body` and is emitter-ready end to end.
COMPONENT_DOCS = [
    "services_basic.rvl", "services_config.rvl", "services_body.rvl",
    "services_methods.rvl", "services_method_effects.rvl", "services_timers.rvl",
    "provide_returns.rvl",
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


# item 445: the reference frontend proves UNIQUE OWNERSHIP of an accumulation
# local once (`src/revl/ownership.py`) and stamps the answer on the IR —
# `"unique"` on a self-rebinding `assign`, `"unique_birth"` on the `let` that
# would need the defensive copy — instead of each emitter re-deriving the same
# aliasing rule (the go and python tiers had written it twice). The self-host
# lowering gate streams tokens straight to IR JSON in one pass, so it grew a
# SECOND scan over the same body tokens (`selfhost/lower.rvl`'s `own_*` block)
# that builds the small statement view the FORWARD DATAFLOW needs, runs the
# flow-sensitive analysis with a fixpoint over each loop back edge, and stamps the
# decided markers where the streaming producer emits the assign/let. The native
# IR now carries the same markers as the reference over the whole corpus — the
# one document with an in-place accumulation loop, `transforms.rvl`'s `list_map` /
# `list_filter`, agrees byte-for-byte, so the gap that stood strict-xfail here is
# CLOSED and the corpus runs through the same unmarked projection as every other
# document. (The whole-program retention summary — item 445 (b) — and the
# token-level record-update write shape are the remaining follow-ons; both only
# ever WITHHOLD a marker the corpus does not exercise, never add a wrong one.)
CORPUS_UNIQUE_AWARE = CORPUS

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


@pytest.fixture(scope="module")
def lower_to_ir_at(ns):
    return ns["lower_to_ir_at"]


def _reference_emit():
    spec = importlib.util.spec_from_file_location(
        "pyemit_reference_lower_ir", ROOT / "backends" / "python" / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _headers(components):
    """The declaration-header projection of a component list."""
    return [
        {"name": c["name"], "source": c.get("source"), "config": c["config"],
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


def test_native_ir_annotates_a_map_subscript(lower_to_ir):
    """Issue #957 — a subscript is lowered from the CONTAINER's declared type.

    An `index` node whose target is a declared `Map[K, V]` carries `key_type`
    and `value_type`, which is how a backend tells a keyed read from the
    positional `List` read every subscript arm was written for. The reference
    frontend started stamping them without the self-host lowering following, and
    the byte-agreement oracle stayed green because no corpus document indexed a
    `Map` at all — `tests/fixtures/emit_py_corpus/maps.rvl` is that document, and
    this pins the annotation itself next to the `List` read that must NOT carry
    it."""
    source = (
        "fn keyed(m: Map[Str, Int], k: Str) -> Int { return m[k] }\n"
        "fn positional(xs: List[Int], i: Int) -> Int { return xs[i] }\n"
    )
    reference = compile_source(source)["functions"]
    native = json.loads(lower_to_ir(source))["functions"]
    keyed = reference[0]["body"][-1]["expr"]
    assert keyed["key_type"] == "Str" and keyed["value_type"] == "Int"
    assert "key_type" not in reference[1]["body"][-1]["expr"]
    assert native == reference


def test_native_ir_keeps_the_declaration_after_an_extern(lower_to_ir):
    """Item 391 — `p_extern`'s body skip decides where every top-level walker
    RESUMES, and its stop set named only the heads the admission gate reads.

    So a `pub fn` written after an extern resumed at the `fn` with the `pub`
    already consumed and lowered PRIVATE, and a `type` written after one was
    skipped straight out of the `types` section. Both are silent — the IR stays
    well formed, it just is not the reference's — and every extern in the corpus
    sat last in its file, so no document held the ordering.
    `tests/fixtures/emit_py_corpus/extern_neighbours.rvl` is that document."""
    source = (
        "extern pure fn h(s: Str) -> Str = @py { return s }\n"
        "type T = { a: Int }\n"
        "pub fn render(l: Str) -> Str { return h(l) }\n"
    )
    reference = compile_source(source)
    native = json.loads(lower_to_ir(source))
    assert reference["functions"][0]["public"] is True
    assert reference["types"] == {"T": {"params": [], "kind": "record",
                                        "fields": {"a": "Int"}}}
    assert native["functions"] == reference["functions"]
    assert native["types"] == reference["types"]


def test_native_ir_pins_an_empty_collection_literal(lower_to_ir):
    """Item 391 — an annotated `let`/`var` is a checking position, so the
    reference threads the author's annotation onto an EMPTY collection literal
    (`_pin_empty_literal`): the checker types the literal at bottom and a
    positional emitter has no element to infer from. The self-host pinned the
    empty `Map` and not the empty `List`; a non-empty literal takes neither."""
    source = (
        "fn empty() -> List[Int] { let xs: List[Int] = [] return xs }\n"
        "fn full() -> List[Int] { let ys: List[Int] = [1] return ys }\n"
    )
    reference = compile_source(source)["functions"]
    native = json.loads(lower_to_ir(source))["functions"]
    assert reference[0]["body"][0]["value"]["expected"] == "List[Int]"
    assert "expected" not in reference[1]["body"][0]["value"]
    assert native == reference


def test_native_ir_marks_a_width_coercion_on_an_annotated_let(lower_to_ir):
    """Item 391 — `_mark_widen` runs at the same annotated-`let` position it runs
    at for a call argument and a return. The self-host carried the argument and
    return halves and not this one, so `let w: Int = <Int32>` reached the tiers
    that keep the widths apart as a bare node."""
    source = (
        "fn widened(n: Int32) -> Int { let w: Int = n return w }\n"
        "fn floated(n: Int) -> Float { let f: Float = n return f }\n"
        "fn plain(n: Int) -> Int { let k: Int = n return k }\n"
    )
    reference = compile_source(source)["functions"]
    native = json.loads(lower_to_ir(source))["functions"]
    assert reference[0]["body"][0]["value"]["widen"] == "Int"
    assert reference[1]["body"][0]["value"]["widen"] == "Float"
    assert "widen" not in reference[2]["body"][0]["value"]
    assert native == reference


def test_native_ir_types_a_constructor_in_scrutinee_position(lower_to_ir):
    """Item 391 — a match arm's `payload_type` is read off the scrutinee's
    INFERRED type, so a constructor APPLICATION used directly as the scrutinee
    has to type the same way a local holding one does.

    The self-host's `infer` had no ADT arm at all: a constructor call and a bare
    nullary case both typed as unknown, so every arm over them lost the
    annotation that gives its binder a type. Every match in the corpus went
    through a declared parameter, which is why the oracle never disagreed.
    `tests/fixtures/emit_py_corpus/adt_inference.rvl` is that document."""
    source = (
        "type Tree = Leaf | Node(Int)\n"
        "fn applied(x: Int) -> Int { return match Node(x) { Node(v) => v, Leaf => 0 } }\n"
        "fn builtin() -> Int { return match Ok(1) { Ok(o) => o, Err(e) => 0 } }\n"
    )
    reference = compile_source(source)["functions"]
    native = json.loads(lower_to_ir(source))["functions"]
    assert reference[0]["body"][0]["expr"]["arms"][0]["payload_type"] == "Int"
    # `Ok(1)` types `Result[Int, Any]`: the argument reaches the arm that binds
    # it, and the other side of the Result stays `Any`.
    ok_arms = reference[1]["body"][0]["expr"]["arms"]
    assert [a["payload_type"] for a in ok_arms] == ["Int", "Any"]
    assert native == reference


def test_native_ir_types_a_map_lookup(lower_to_ir):
    """Item 391 — `lookup` was the one `_BUILTIN_SIG` row the self-host method
    table did not answer, so the `Opt[V]` it returns typed as unknown and the
    `match` that always follows it lost the arm's `payload_type`."""
    source = (
        "fn find(m: Map[Str, Int], k: Str) -> Int {\n"
        "  let hit = m.lookup(k)\n"
        "  return match hit { Some(v) => v, None => 0 }\n"
        "}\n"
    )
    reference = compile_source(source)["functions"]
    native = json.loads(lower_to_ir(source))["functions"]
    assert reference[0]["body"][1]["expr"]["arms"][0]["payload_type"] == "Int"
    assert native == reference


def test_native_ir_colours_an_async_function(lower_to_ir):
    """Item 391 — the async colour is a whole-program fixed point over the call
    graph, and `"async": true` on a fn entry is the only thing that tells an
    emitter to render `async`/`await`. The self-host's streaming function
    producer decided each declaration on its own and so stamped nothing, even
    though the gate half of the same file already computes that closure for its
    A1 verdicts; it now reads that one rather than deriving a second.
    `tests/fixtures/emit_py_corpus/async_colour.rvl` is the corpus document."""
    source = (
        "extern emission async fn hf(p: Str) -> Str = @py { return p }\n"
        "fn one(p: Str) -> Str { return hf(p) }\n"
        "fn two(p: Str) -> Str { return one(p) }\n"
        "fn plain(n: Int) -> Int { return n + 1 }\n"
    )
    reference = compile_source(source)["functions"]
    native = json.loads(lower_to_ir(source))["functions"]
    assert [fn.get("async") for fn in reference] == [True, True, None]
    assert native == reference


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
    """Each component's source/config/requires/provides header is byte-identical."""
    src = (CORPUS_DIR / rel).read_text()
    native = json.loads(lower_to_ir(src))
    reference = compile_source(src)
    assert _headers(native["components"]) == _headers(reference["components"])


@pytest.mark.parametrize("rel", CORPUS)
def test_native_ir_stamps_the_component_source(lower_to_ir_at, rel):
    """The per-component `source` key, which four census passes excluded.

    It was excluded as an "environment artifact the covered emitter surface
    never reads". Half of that is measurably true and half was never the
    reason. TRUE: its one reader, `backends/typescript/emit_temporal.py`, puts
    it in REFUSAL TEXT and falls back to `"<source>"`, so no emitted byte moves
    when it is absent. NOT the reason: `source` is `comp.source or filename`,
    and `lower_to_ir(src)` took a string and nothing else, so the producer could
    not have stamped the key whatever any emitter did with it.

    `lower_to_ir_at(src, filename)` supplies the one input that is not the
    source text, and the key is then byte-identical to the reference for the
    same filename — over the whole corpus, not a chosen document."""
    src = (CORPUS_DIR / rel).read_text()
    native = json.loads(lower_to_ir_at(src, rel))
    reference = compile_source(src, rel)
    assert [c.get("source") for c in native["components"]] == \
           [c.get("source") for c in reference["components"]]
    assert [c["name"] for c in native["components"]] == \
           [c["name"] for c in reference["components"]]


def test_the_text_only_entries_agree_on_the_default_filename(lower_to_ir):
    """`lower_to_ir(src)` is the text-only entry on this side exactly as
    `compile_source(src)` is on the other, so they must agree on what a caller
    who named no file gets: `"<string>"`, `compile_source`'s own default. A
    producer that stamped something else here would look correct against every
    test that passes a filename and diverge on every one that does not."""
    src = ("service S { fn go() -> Int }\n"
           "component C provides s: S { provide s { fn go() = 1 } }\n")
    native = json.loads(lower_to_ir(src))
    assert [c["source"] for c in native["components"]] == \
           [c["source"] for c in compile_source(src)["components"]] == ["<string>"]


def test_no_native_ir_document_carries_a_manifest(lower_to_ir):
    """The `manifest` half of the same exclusion, pinned rather than assumed.

    `lower_to_ir` produces no `manifest` key at all, and the prior reading was
    that this is inert because no backend emitter reads one. That is measurably
    WRONG: `backends/wasm/emit.py` reads `manifest["templates"]` and passes
    `is_template=` into every `_ComponentEmitter`, so a component that is a
    spawn target emits a DIFFERENT module depending on the key. Eight documents
    in the tree carry a non-empty `templates`.

    Closing it needs the filename too — every `manifest["components"][i]` entry
    carries a `file` — plus the link order the reference derives in `_link`, so
    it is named here as an open gap rather than half-produced. A partial
    manifest is worse than none: a consumer reading `loadOrder` off one would
    get an empty list and no signal that the producer never had one.

    Item 391 went back and MEASURED what a complete one would take, over the
    528-document census corpus (`tools/gate_reference_census.py`'s directories);
    344 of those are reference-admitted and all 344 carry a manifest. Two things
    block it, and the first kills the feasibility note this docstring used to
    carry.

    1. `templates` is NOT separable. It reads as the easy shape — it is the one
       any emitter actually consumes, and the spawn targets are already
       collected — but the 8 documents that carry a non-empty `templates` are
       EXACTLY the 8 that carry `instances`, as sets, with neither difference
       non-empty. `instances` is `_check_spawn_attenuation`'s per-instance
       attenuation chain: a fold of structured `(T, P)` capabilities through
       `cap_order.covers` with the key-to-token bridge and `config.` symbol
       substitution. That is the cone/ceiling algebra this gate does not have,
       and the gate census still records its absence as the two standing
       `false-admit/G4` entries. So on every document where `templates` would
       change an emitted byte, producing it alone IS the partial manifest this
       test refuses.

    2. `loadOrder` would be WRONG, not merely incomplete. `ir_component` emits
       `name`/`source`/`config`/`requires`/`provides`/`body` and nothing else —
       no `isolate`, `intercept`, `routes` or `boot`. Those are not only
       conditional entry keys: `_link` partitions the provider table by
       `(key, realm)` with the realm read off `isolate`, and a `realms(...)`
       route contributes one graph edge per leg, so the Kahn order is computed
       over a different graph on any document that isolates or routes.
       Measured over the same 344: isolate on 28 documents, intercept on 7,
       routes on 5, boot on 1, templates/instances on 8 — 40 distinct documents
       needing a shape the native component IR does not carry. A manifest built
       from `{name, file, inject, provides}` plus a plain Kahn pass would be
       byte-identical on the other 304 and silently wrong on those 40.

    The producible order is therefore: the four missing component-header shapes
    first, then the attenuation algebra, then this key — not this key first.

    This fails the moment the key appears, which is the point — the next pass
    that produces it has to come here and say what it produces."""
    for rel in COMPONENT_DOCS:
        native = json.loads(lower_to_ir((CORPUS_DIR / rel).read_text()))
        assert "manifest" not in native, rel


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


# ======================================================== item 391, next tranche
#
# Each of the six below is a shape the self-host frontend did not lower, found by
# running BOTH frontends over every single-file `.rvl` in the tree (563 of them,
# against the 41 this oracle globs) and diffing the `functions`/`types`/`externs`
# projection. The document that pins each one enters `emit_py_corpus/`, so it
# joins every globbed projection here automatically — which is the half that
# stops the regression, and the reason these hid as long as they did.

def test_native_ir_lowers_a_generic_module_fn(lower_to_ir):
    """`fn name[T](…)` — a declaration the reference accepts and the self-host
    dropped out of `functions` entirely.

    Every declaration reader assumed the parameter list opened two tokens after
    the `fn` keyword, so a type-parameter list walked the parameter reader into
    `[T, U]`. Type parameters are ERASED: a generic declaration must lower to
    exactly the shape its monomorphic neighbour does."""
    source = (
        "pub fn identity[T](x: T) -> T { return x }\n"
        "pub fn identity_int(x: Int) -> Int { return x }\n"
    )
    reference = compile_source(source)["functions"]
    native = json.loads(lower_to_ir(source))["functions"]
    assert [fn["name"] for fn in reference] == ["identity", "identity_int"]
    assert reference[0]["params"] == [{"name": "x", "type": "T"}]
    assert native == reference


def test_native_ir_reads_a_variant_declared_over_several_lines(lower_to_ir):
    """A `type` declaration is not bounded by its first line.

    A case below the first line never entered the case table, so a bare nullary
    constructor lowered as a `var` — a reference to a binding that does not
    exist — rather than an `adt` node. `stdlib/a2a.rvl` and `stdlib/http.rvl`
    both declare their variants this way."""
    source = (
        "pub type Phase =\n"
        "    Queued\n"
        "  | Finished(Int)\n"
        "pub fn start() -> Phase { return Queued }\n"
    )
    reference = compile_source(source)
    native = json.loads(lower_to_ir(source))
    started = reference["functions"][0]["body"][0]["expr"]
    assert started == {"kind": "adt", "type": "Phase", "case": "Queued", "args": []}
    assert native["functions"] == reference["functions"]
    assert native["types"] == reference["types"]


def test_a_type_declaration_span_stops_at_the_pub_that_follows_it(lower_to_ir):
    """`pub` is not a declaration head, but it IS the first token of the next
    declaration.

    A span that only stops at heads runs one token past it, and hands the next
    reader a `fn` whose visibility prefix has already been eaten — an exported
    function lowered as private. Same shape as the extern-neighbour divergence,
    reached from the other side."""
    source = (
        "pub type Phase =\n"
        "    Queued\n"
        "  | Running\n"
        "pub fn start() -> Phase { return Queued }\n"
    )
    reference = compile_source(source)["functions"]
    native = json.loads(lower_to_ir(source))["functions"]
    assert reference[0]["public"] is True
    assert native == reference


def test_native_ir_lowers_an_else_if_chain(lower_to_ir):
    """`else if` nests the trailing `if` as the sole step of the else list.

    The reader recognised `else {` and nothing else, so the chain lowered as a
    bare `if` with an empty else AND stopped the enclosing block — the cursor
    came to rest on the word `else`, which is not a statement, so the branches
    below the first and every statement AFTER the chain went together."""
    source = (
        "pub fn grade(n: Int) -> Str {\n"
        "  var out = \"?\"\n"
        "  if (n >= 90) { out = \"A\" } else if (n >= 80) { out = \"B\" } else { out = \"C\" }\n"
        "  out = out.concat(\"!\")\n"
        "  return out\n"
        "}\n"
    )
    reference = compile_source(source)["functions"]
    native = json.loads(lower_to_ir(source))["functions"]
    body = reference[0]["body"]
    assert [step["step"] for step in body] == ["let", "if", "assign", "return"]
    assert body[1]["else"][0]["step"] == "if"
    assert native == reference


def test_native_ir_lists_the_mutable_names_an_arrow_captures(lower_to_ir):
    """`captures` is a VALUE, not an annotation that may be withheld.

    It is the list of mutable names the arrow snapshots at creation time, and
    every emitter binds them by value there — python, typescript and go would
    otherwise close over the enclosing local by reference and see a later write.
    The self-host wrote `[]` on every arrow, so this is the one entry in the
    projection a backend could act on and get a different answer from the
    reference's. The immutable neighbour is the control: it must stay empty."""
    source = (
        "pub fn snapshot() -> Int {\n"
        "  var n = 1\n"
        "  let f = x => x + n\n"
        "  n += 10\n"
        "  return f(5)\n"
        "}\n"
        "pub fn no_capture(base: Int) -> Int { let g = x => x + base  return g(5) }\n"
    )
    reference = compile_source(source)["functions"]
    native = json.loads(lower_to_ir(source))["functions"]
    assert reference[0]["body"][1]["value"]["captures"] == ["n"]
    assert reference[1]["body"][0]["value"]["captures"] == []
    assert native == reference


def test_native_ir_resolves_an_arrows_signature_from_its_position(lower_to_ir):
    """`param_types`/`returns` on an arrow that annotates nothing.

    A zero-parameter arrow declares no parameter type, so the "every declared
    type is known" condition is vacuously met and both keys go in — the
    self-host asked for at least one annotated parameter and so withheld them
    from every `() => …`. An arrow ARGUMENT gets its signature from the
    parameter it is checked against, which is the only place `Int` is written."""
    source = (
        "fn apply(f: (Int) -> Int, x: Int) -> Int { return f(x) }\n"
        "pub fn maker(s: Str) -> () -> Int { return () => s.length() }\n"
        "pub fn doubled(x: Int) -> Int { return apply(v => v * 2, x) }\n"
    )
    reference = compile_source(source)["functions"]
    native = json.loads(lower_to_ir(source))["functions"]
    made = reference[1]["body"][0]["expr"]
    assert made["param_types"] == [] and made["returns"] == "Int"
    assert reference[2]["body"][0]["expr"]["args"][0]["param_types"] == ["Int"]
    assert native == reference


def test_native_ir_colours_an_arrow_argument_async(lower_to_ir):
    """The value-position half of the colour item 391 stamps on a fn entry.

    `y => host_get(y)` says nothing about its own signature: the colour comes
    from the `(Str) -> Async[Str]` parameter it is checked against, which is
    also why it may never be self-declared (rule C2 — the flag is a certificate
    that a declaration promised to await this arrow). The uncoloured neighbour
    carries the resolved signature and no stamp."""
    source = (
        "extern emission async fn host_get(path: Str) -> Str = @py { return path }\n"
        "fn drive(cb: (Str) -> Async[Str], path: Str) -> Str { return cb(path) }\n"
        "fn run(cb: (Str) -> Int, s: Str) -> Int { return cb(s) }\n"
        "pub fn fetch(path: Str) -> Str { return drive(y => host_get(y), path) }\n"
        "pub fn measure(s: Str) -> Int { return run(y => y.length(), s) }\n"
    )
    reference = compile_source(source)["functions"]
    native = json.loads(lower_to_ir(source))["functions"]
    coloured = reference[2]["body"][0]["expr"]["args"][0]
    plain = reference[3]["body"][0]["expr"]["args"][0]
    assert coloured["async"] is True and coloured["returns"] == "Async[Str]"
    assert "async" not in plain and plain["returns"] == "Int"
    assert native == reference


# ================================================== item 391, the third tranche
#
# Measured the way the last two were: both frontends run over every single-file
# `.rvl` in the tree and their `functions`/`types`/`externs` projections diffed.
# 18 divergent before, 5 after, over 1015 documents (689 -> 702 byte-identical;
# no document moved the other way). Six documents enter the globbed corpus above,
# each failing against the unported lowering and each carrying its own negative
# controls, because a glob is what stops these coming back.


def test_native_ir_lowers_a_typed_event_declaration(lower_to_ir):
    """Item 130 §6 / item 391 — `event N(key: f) { fields }` IS a record with a
    contract, so the reference parser appends an ordinary record `TypeDecl`
    beside the `EventDecl` and the `types` section carries the same entry a
    `type` spelling would.

    `event` lexes as an `ident`, and every self-host top-level walker refuses a
    non-keyword head, so the gate answered `BAD|unexpected token at top level` —
    a FALSE REFUSAL of a program the reference compiles — and `lower_to_ir`
    dropped the record out of `types` entirely. The head is recognised on exactly
    the three tokens the reference recognises it on (`event IDENT (` /
    `event IDENT {`), so a parameter or record field named `event` is untouched;
    `tests/fixtures/emit_py_corpus/events.rvl` holds both halves."""
    source = (
        "event OrderPlaced(key: order_id) { order_id: Str, quantity: Int }\n"
        "fn line_total(o: OrderPlaced, unit: Int) -> Int { return o.quantity * unit }\n"
        "fn describe(event: Str) -> Str { return event }\n"
    )
    reference = compile_source(source)
    native = json.loads(lower_to_ir(source))
    assert reference["types"] == {
        "OrderPlaced": {"params": [], "kind": "record",
                        "fields": {"order_id": "Str", "quantity": "Int"}}}
    assert native["types"] == reference["types"]
    # the event's fields reach the nominal field table too, which is what stamps
    # `operands` on arithmetic over one of them
    assert reference["functions"][0]["body"][0]["expr"]["operands"] == "Int"
    assert native["functions"] == reference["functions"]


def test_native_ir_reads_a_braceless_statement_body(lower_to_ir):
    """Item 391 — the reference reads an `if`/`while`/`for` body as
    `self.block() if self.at("{") else [self.fn_stmt()]`.

    This reader demanded the brace, so a braceless `if (c) return x` was not a
    statement it recognised: `lir_stmts` stopped there and the WHOLE enclosing
    body lowered as `[]` — every statement, not only the conditional."""
    source = (
        "fn guarded(n: Int) -> Int {\n"
        "  if (n < 0) return 0 - n\n"
        "  return n\n"
        "}\n"
        "fn summed(xs: List[Int]) -> Int {\n"
        "  var t = 0\n"
        "  for (x of xs) t += x\n"
        "  return t\n"
        "}\n"
    )
    reference = compile_source(source)["functions"]
    native = json.loads(lower_to_ir(source))["functions"]
    assert reference[0]["body"][0]["step"] == "if"
    assert reference[0]["body"][0]["then"][0]["step"] == "return"
    assert len(reference[0]["body"]) == 2
    assert native == reference


def test_native_ir_lowers_a_record_update_written_as_an_rvalue(lower_to_ir):
    """Item 391 / item 445 — the token-level record-update as an RVALUE.

    The shared parser's expression grammar carries no node for `{ base | f = e }`,
    so the statement reader recognised it only after `let`/`var`: an `assign` or a
    `return` holding one failed to read and took the rest of its block with it.
    The ownership scan reads the same shape, so a self-rebinding spread carries
    the `unique` marker a `push` write already did; an update over ANOTHER name
    is not a self-rebind and carries none."""
    source = (
        "type Pt = { x: Int, y: Int }\n"
        "fn swapped(n: Int) -> Pt {\n"
        "  var p = { x: 1, y: 2 }\n"
        "  var i = 0\n"
        "  while (i < n) {\n"
        "    p = { p | x = p.y, y = p.x }\n"
        "    i = i + 1\n"
        "  }\n"
        "  return p\n"
        "}\n"
        "fn shifted(p: Pt, d: Int) -> Pt { return { p | x = p.x + d } }\n"
        "fn restated(p: Pt, q: Pt) -> Pt {\n"
        "  var r = p\n"
        "  r = { q | x = q.x + 1 }\n"
        "  return r\n"
        "}\n"
    )
    reference = compile_source(source)["functions"]
    native = json.loads(lower_to_ir(source))["functions"]
    write = reference[0]["body"][2]["body"][0]
    assert write["step"] == "assign" and write["unique"] is True
    assert write["value"]["kind"] == "record_update"
    assert reference[1]["body"][0]["expr"]["kind"] == "record_update"
    assert "unique" not in reference[2]["body"][1]
    assert native == reference


def test_native_ir_reads_a_keyword_in_record_field_position(lower_to_ir):
    """Items 158/237/391 — a field name is always followed by `:` / `=` /
    end-of-access, so no keyword reading is grammatically possible there and the
    reference broadens the plain `ident` with the contextual cordis-domain nouns
    plus the component-grammar ones (`_record_key_name`).

    The self-host expression parser read only `ident`, so a record literal, a
    `.field` read, a record-update clause and a record TYPE's field list all
    failed on a field named `config` — silently, because a failed statement
    lowers to nothing rather than to a diagnostic."""
    source = (
        "type Envelope = { config: Int, plain: Int }\n"
        "fn built(n: Int) -> Envelope { return { config: n, plain: n } }\n"
        "fn summed(e: Envelope) -> Int { return e.config + e.plain }\n"
        "fn bumped(e: Envelope) -> Envelope { return { e | config = e.config + 1 } }\n"
    )
    reference = compile_source(source)
    native = json.loads(lower_to_ir(source))
    assert reference["types"]["Envelope"]["fields"] == {"config": "Int", "plain": "Int"}
    assert reference["functions"][1]["body"][0]["expr"]["operands"] == "Int"
    assert native["types"] == reference["types"]
    assert native["functions"] == reference["functions"]


def test_a_type_declaration_span_is_stepped_over_by_every_walker(lower_to_ir):
    """Item 391 — the same `type_decl_end` span #1025 gave `p_top`, for the
    walkers it did not reach.

    `ir_walk`, `fns_walk` and `externs_walk` each stepped a `type` to the end of
    its first LINE, so a record written one field per line left the cursor INSIDE
    the body — where a field named with a keyword is a top-level declaration head.
    `type Envelope = {` followed by `component: Int,` on its own line minted a
    component named `":"` in the IR."""
    source = (
        "type Envelope = {\n"
        "  component: Int,\n"
        "  plain: Int,\n"
        "}\n"
        "fn plain_of(e: Envelope) -> Int { return e.plain }\n"
    )
    reference = compile_source(source)
    native = json.loads(lower_to_ir(source))
    assert reference["components"] == []
    assert native["components"] == reference["components"]
    assert native["types"] == reference["types"]
    assert native["functions"] == reference["functions"]


def test_native_ir_lowers_a_destructuring_let(lower_to_ir):
    """Item 391 — `let { a, b } = r` / `var [head, ...rest] = xs` lower to ONE
    `let_pattern` step (lower.py `_lower_let_pattern_stmt`).

    The statement reader read the token after `let` as the bound NAME, found `{`
    where it wanted `=`, and gave up on the whole enclosing block. The bound names
    carry their types too — a record pattern's from the record's field table, a
    list pattern's element names from the list's argument and its `...rest` from
    the whole list type — which is what stamps `operands` on arithmetic over
    them."""
    source = (
        "type Pair = { left: Int, right: Int }\n"
        "fn split(p: Pair) -> Int {\n"
        "  let { left, right } = p\n"
        "  return left * 10 + right\n"
        "}\n"
        "fn tailed(xs: List[Int]) -> Int {\n"
        "  var [head, ...rest] = xs\n"
        "  head = 7\n"
        "  return head + rest[0]\n"
        "}\n"
    )
    reference = compile_source(source)["functions"]
    native = json.loads(lower_to_ir(source))["functions"]
    assert reference[0]["body"][0] == {
        "step": "let_pattern", "pattern": "record",
        "names": ["left", "right"],
        "value": {"kind": "var", "name": "p"}, "mutable": False}
    assert reference[1]["body"][0]["pattern"] == "list"
    assert reference[1]["body"][0]["rest"] == "rest"
    assert reference[0]["body"][1]["expr"]["operands"] == "Int"
    assert native == reference


def test_native_ir_reads_a_block_bodied_if_in_value_position(lower_to_ir):
    """Item 196 / item 391 — `if (c) { a } else { b }` in EXPRESSION position is
    the ternary's block-bodied twin and lowers to the very same `if` node, so it
    sits under a `.field`, a subscript or a `??` the way any other primary can.

    The self-host expression parser had no branch for it, so the statement
    holding one lowered as nothing."""
    source = (
        "type Cell = { value: Int }\n"
        "fn picked(c: Bool, a: Cell, b: Cell) -> Int {\n"
        "  return (if (c) { a } else { b }).value\n"
        "}\n"
        "fn ternary_picked(c: Bool, a: Cell, b: Cell) -> Int {\n"
        "  return (c ? a : b).value\n"
        "}\n"
    )
    reference = compile_source(source)["functions"]
    native = json.loads(lower_to_ir(source))["functions"]
    assert reference[0]["body"][0]["expr"]["target"]["kind"] == "if"
    # the block form and the ternary are the SAME node
    assert reference[0]["body"][0] == reference[1]["body"][0]
    assert native == reference


def test_native_ir_lowers_an_empty_template(lower_to_ir):
    """Item 391 — an empty template carries no segments at all. Splitting its
    segment list yielded one empty segment whose two-character tag was neither
    `t:` nor `v:`, so the parse failed where the reference reads an `interp` with
    an empty part list."""
    source = "fn blank() -> Str { return `` }\nfn filled(n: Int) -> Str { return `n=${n}` }\n"
    reference = compile_source(source)["functions"]
    native = json.loads(lower_to_ir(source))["functions"]
    assert reference[0]["body"][0]["expr"] == {"kind": "interp", "parts": []}
    assert native == reference


def test_native_ir_types_a_match_used_as_a_value(lower_to_ir):
    """Item 391 — a `match` YIELDS a value, and the reference's checker types it
    by unifying the arm bodies.

    `infer` answered "" for one, so `t += match o { … }` lost the `operands` tag
    the lowering stamps on typed arithmetic — the one annotation in this
    projection a backend cannot re-derive. Each arm body is inferred under the
    arm's payload binding and joined with the same meet a ternary already uses,
    so a disagreement answers "" and stamps nothing."""
    source = (
        "fn accumulated(xs: List[Opt[Int]]) -> Int {\n"
        "  var t = 0\n"
        "  for (o of xs) { t += match o { Some(v) => v, None => 0 } }\n"
        "  return t\n"
        "}\n"
    )
    reference = compile_source(source)["functions"]
    native = json.loads(lower_to_ir(source))["functions"]
    assert reference[0]["body"][1]["body"][0]["value"]["operands"] == "Int"
    assert native == reference


# --------------------------------------------------------- item 391: the
# component dialect's host frontier and realm-placement prelude.
#
# Both shapes below were INVISIBLE to every oracle in this file before they were
# written, and that is the point of adding them. `cir_body` refuses a step it
# cannot lower, and a refused step drops the WHOLE `body` key rather than
# emitting a wrong one — so a component whose first statement is a host
# acquisition (or whose first line is an `isolate` pin) produced an IR entry with
# no `body` at all. The corpus oracle that walks bodies
# (`test_native_ir_matches_reference_bodies_where_covered`) iterates only the
# components that HAVE one, so a dropped body reads as "nothing to check" and the
# suite stayed green. These pin the presence as well as the content.

_HOST_SOURCE = """service Cache { fn put(key: Str, value: Str) -> Str }
component HonestCache provides cache: Cache {
  let store = effect Map.new() undo store.drop()
  provide cache {
    fn put(key, value) {
      effect store.insert(key, value)
      undo   store.remove(key)
      return key
    }
  }
}
"""


def test_native_ir_lowers_a_host_acquisition_in_a_component_body(lower_to_ir):
    """`let store = effect Map.new() undo store.drop()` — an upper-cased callable
    head is a HOST ACQUISITION (`{kind: "host", fn: "Map.new"}`), not a required
    service and not a scoped name. The native producer resolved neither, refused
    the step, and dropped the component body with it."""
    reference = compile_source(_HOST_SOURCE)["components"][0]
    native = json.loads(lower_to_ir(_HOST_SOURCE))["components"][0]
    assert "body" in native, "the component body is still dropped"
    assert reference["body"][0]["acquire"] == {"kind": "host", "fn": "Map.new",
                                               "args": []}
    assert native == reference


def test_a_host_locals_verb_is_a_call_and_not_a_stdlib_builtin(lower_to_ir):
    """The dual dispatch the acquisition buys: `remove` is spelled by BOTH the
    stdlib method table and the host Map surface, and on a host-acquired receiver
    it is the host verb — a plain `call` node selected by receiver KIND, never a
    `builtin`. Reading the builtin table first would produce a node the runtimes
    do not define for that receiver."""
    reference = compile_source(_HOST_SOURCE)["components"][0]
    undo = reference["body"][1]["methods"][0]["body"][0]["undo"]
    assert undo == {"kind": "call", "target": {"kind": "name", "id": "store"},
                    "method": "remove", "args": [{"kind": "name", "id": "key"}]}
    native = json.loads(lower_to_ir(_HOST_SOURCE))["components"][0]
    assert native["body"][1]["methods"][0]["body"][0]["undo"] == undo


def test_the_host_acquisition_oracle_is_not_vacuous(lower_to_ir):
    """NON-VACUITY for the two above: the comparison is a real one. A single
    byte changed in the expected acquisition — `Map.new` to `Map.nex` — must make
    it fail, and a component with no host acquisition must not gain a body key it
    did not have."""
    native = json.loads(lower_to_ir(_HOST_SOURCE))["components"][0]
    reference = compile_source(_HOST_SOURCE)["components"][0]
    corrupted = json.loads(json.dumps(reference))
    corrupted["body"][0]["acquire"]["fn"] = "Map.nex"
    # the `!=` is only evidence while the `==` holds: a producer that matched
    # nothing would pass the inequality on its own
    assert native == reference
    assert native != corrupted


_REALM_SOURCE = """service Store { fn put(key: Str, val: Str) }
service Reader { fn read(key: Str) -> Str }
component AuditReader requires store: Store provides reader: Reader {
  isolate reader in realm("r1")
  intercept store with { tags: ["audit", "v2"], level: 3, enabled: true, note: null }
  provide reader {
    fn read(key) { return "x" }
  }
}
"""


def test_native_ir_lowers_the_realm_placement_prelude(lower_to_ir):
    """`isolate <key> in realm("…")` and `intercept <key> with { … }` are
    component HEADER declarations that emit no activation step. The body walk had
    no arm for either, so it refused at the first one and the component lost its
    `body`, its `isolate` and its `intercept` together."""
    reference = compile_source(_REALM_SOURCE)["components"][0]
    native = json.loads(lower_to_ir(_REALM_SOURCE))["components"][0]
    assert reference["isolate"] == {"reader": "r1"}
    assert reference["intercept"] == {
        "store": {"tags": ["audit", "v2"], "level": 3, "enabled": True,
                  "note": None}}
    assert native == reference


def test_the_prelude_tables_keep_the_references_key_order(lower_to_ir):
    """`isolate` and `intercept` are stamped AFTER `body`, which is the order the
    reference writes them in. It is not cosmetic: `crates/revl-gate` reads the
    interchange document through `serde_json` with `preserve_order`, so a record's
    fields reach the native emitter in DOCUMENT order, and a table written before
    `body` would reach it in a different one than the reference's."""
    reference = compile_source(_REALM_SOURCE)["components"][0]
    native = json.loads(lower_to_ir(_REALM_SOURCE))["components"][0]
    assert list(reference) == ["name", "source", "config", "requires",
                              "provides", "body", "isolate", "intercept"]
    assert list(native) == list(reference)


def test_the_prelude_oracle_is_not_vacuous(lower_to_ir):
    """NON-VACUITY: one byte changed in the expected realm label fails the
    comparison, and a metadata value the record-literal grammar does not admit
    withholds the whole table rather than stamping a guess."""
    native = json.loads(lower_to_ir(_REALM_SOURCE))["components"][0]
    reference = compile_source(_REALM_SOURCE)["components"][0]
    corrupted = json.loads(json.dumps(reference))
    corrupted["isolate"]["reader"] = "r2"
    assert native == reference
    assert native != corrupted
    # and the metadata grammar itself is the reference's: a value outside
    # `Parser.record_literal` (a name rather than a literal) is refused at PARSE
    # time there, so the producer never has to guess at one
    with pytest.raises(Exception):
        compile_source(_REALM_SOURCE.replace("level: 3", "level: tag"))
