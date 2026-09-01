"""Typed function values (docs/function-types.md).

Arrows used to be on the checker's enumerated unchecked frontier: `infer_ast`
returned `None` for them, so a lambda had no type, the TypeScript backend
emitted `any` parameters as an admission, and higher-order composition was not
expressible. These tests hold the line on the four things that changed:

- `(Int, Str) -> Bool` is a type, usable wherever a type is written;
- an arrow in *checking* position gets its parameter and return types from the
  expectation, and a definite mismatch is a diagnostic;
- those types reach the IR (`param_types` / `returns` on the arrow node) and
  the tiers that can use them;
- the tiers that cannot say so explicitly, and the ones that already handled
  local arrows keep doing so.
"""

from __future__ import annotations

import importlib.util
import sys
import types as pytypes
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.compiler import compile_source  # noqa: E402
from revl.errors import RevlError  # noqa: E402
from revl.typecheck import FN_HEAD, compatible, format_type, parse_type  # noqa: E402


def _emitter(tier: str):
    spec = importlib.util.spec_from_file_location(
        f"revl_{tier}_emit_ft", ROOT / "backends" / tier / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_python(source: str) -> pytypes.ModuleType:
    """Compile `source` and execute the python tier's output."""
    emit = _emitter("python")
    module = pytypes.ModuleType("revl_ft")
    # `@dataclass` resolves string annotations through `sys.modules[cls.
    # __module__]`, so an emitted record type needs its module registered
    sys.modules["revl_ft"] = module
    try:
        exec(compile(emit.emit(compile_source(source)), "<function-types>", "exec"),
             module.__dict__)
    finally:
        sys.modules.pop("revl_ft", None)
    return module


def _fn_body(ir: dict, name: str) -> list:
    return next(fn["body"] for fn in ir["functions"] if fn["name"] == name)


# ---------------------------------------------------------------- the type

def test_function_type_parses_to_a_head_with_the_return_last():
    assert parse_type("(Int, Str) -> Bool") == (FN_HEAD, ["Int", "Str", "Bool"])
    assert parse_type("() -> Int") == (FN_HEAD, ["Int"])
    # right-nested: the return type is itself a function type
    assert parse_type("(Int) -> (Str) -> Bool") == (FN_HEAD, ["Int", "(Str) -> Bool"])
    # a function type inside a generic argument does not split at its comma
    assert parse_type("List[(Int, Str) -> Bool]") == ("List", ["(Int, Str) -> Bool"])


def test_format_type_round_trips_a_function_type():
    for spelling in ("(Int, Str) -> Bool", "() -> Int",
                     "(Int) -> (Str) -> Bool", "((Int) -> Int) -> Int"):
        head, args = parse_type(spelling)
        assert format_type(head, args) == spelling


def test_function_type_parameters_are_contravariant_and_results_covariant():
    # a function accepting Float can stand in where one accepting Int is
    # required (Int widens to Float on the way in) — but not the reverse
    assert compatible("(Int) -> Int", "(Float) -> Int")
    assert not compatible("(Float) -> Int", "(Int) -> Int")
    # results go the other way
    assert compatible("(Int) -> Float", "(Int) -> Int")
    assert not compatible("(Int) -> Int", "(Int) -> Float")
    # arity is part of the type
    assert not compatible("(Int) -> Int", "(Int, Int) -> Int")


def test_function_type_is_usable_wherever_a_type_is_written():
    ir = compile_source(
        "type Step = (Int) -> Int\n"
        "type Wrapped = { run: (Int) -> Str, next: Step }\n"
        "fn take(g: (Int, Str) -> Bool) -> Bool { return g(1, \"a\") }\n"
        "fn give(n: Int) -> (Int) -> Int { return v => v + n }\n"
        "service Svc { fn go(g: (Int) -> Int) -> Int }\n"
    )
    assert ir["types"]["Wrapped"]["fields"] == {"run": "(Int) -> Str", "next": "(Int) -> Int"}
    assert ir["services"]["Svc"]["methods"]["go"]["params"] == [
        {"name": "g", "type": "(Int) -> Int"}]
    take = next(fn for fn in ir["functions"] if fn["name"] == "take")
    assert take["params"] == [{"name": "g", "type": "(Int, Str) -> Bool"}]
    assert next(fn for fn in ir["functions"] if fn["name"] == "give")["returns"] == "(Int) -> Int"


def test_optional_function_type_needs_the_group_parentheses():
    # `(Int) -> Str?` is a function returning an optional …
    assert compile_source("fn f(g: (Int) -> Str?) -> Int { return 1 }"
                          )["functions"][0]["params"][0]["type"] == "(Int) -> Opt[Str]"
    # … and `((Int) -> Str)?` is an optional function
    assert compile_source("fn f(g: ((Int) -> Str)?) -> Int { return 1 }"
                          )["functions"][0]["params"][0]["type"] == "Opt[(Int) -> Str]"


def test_a_parenthesised_type_group_is_not_a_tuple():
    with pytest.raises(RevlError, match="revl has no tuples"):
        compile_source("fn f(g: (Int, Str)) -> Int { return 1 }")


def test_type_alias_to_a_function_type_is_transparent():
    ir = compile_source("type Step = (Int) -> Int\n"
                        "fn f(g: Step) -> Int { return g(1) }")
    # the alias is erased, exactly as `type Rows = List[Row]` is
    assert "Step" not in ir.get("types", {})
    assert ir["functions"][0]["params"][0]["type"] == "(Int) -> Int"


def test_alias_in_a_let_annotation_is_expanded_too():
    """A `let`/arrow annotation lives inside a body, not at a declaration
    site, so the alias sweep has to reach into function bodies as well."""
    ir = compile_source("type Step = (Int) -> Int\n"
                        "fn f(n: Int) -> Int { let g: Step = v => v + 1  return g(n) }")
    let_step = _fn_body(ir, "f")[0]
    assert let_step["value"]["param_types"] == ["Int"]


# ------------------------------------------------------------- checking

def test_arrow_passed_to_a_fn_is_typed_from_the_parameter():
    ir = compile_source(
        "fn apply_twice(g: (Int) -> Int, x: Int) -> Int { return g(g(x)) }\n"
        "fn demo(n: Int) -> Int { return apply_twice(v => v + 1, n) }\n")
    arrow = _fn_body(ir, "demo")[0]["expr"]["args"][0]
    assert arrow["kind"] == "arrow"
    assert arrow["param_types"] == ["Int"]
    assert arrow["returns"] == "Int"
    assert _run_python(
        "fn apply_twice(g: (Int) -> Int, x: Int) -> Int { return g(g(x)) }\n"
        "fn demo(n: Int) -> Int { return apply_twice(v => v + 1, n) }\n"
    ).demo(3) == 5


def test_arrow_stored_in_a_let_and_called():
    source = ("fn demo(n: Int) -> Int {\n"
              "  let double_: (Int) -> Int = v => v * 2\n"
              "  return double_(n)\n"
              "}\n")
    ir = compile_source(source)
    let_step = _fn_body(ir, "demo")[0]
    assert let_step["value"]["param_types"] == ["Int"]
    assert let_step["value"]["returns"] == "Int"
    assert _run_python(source).demo(21) == 42


def test_arrow_returned_from_a_fn():
    source = ("fn adder(n: Int) -> (Int) -> Int { return v => v + n }\n"
              "fn demo(n: Int) -> Int { let g: (Int) -> Int = adder(n)  return g(10) }\n")
    ir = compile_source(source)
    returned = _fn_body(ir, "adder")[0]["expr"]
    assert returned["kind"] == "arrow"
    assert returned["param_types"] == ["Int"] and returned["returns"] == "Int"
    assert _run_python(source).demo(5) == 15


def test_an_annotated_arrow_is_typed_without_any_expected_type():
    """`(v: Int) => …` is the other source of an arrow's types: with no
    checking position, the annotation is what takes it off the frontier."""
    ir = compile_source("fn demo(n: Int) -> Int { let g = (v: Int) => v + 1  return g(n) }")
    assert _fn_body(ir, "demo")[0]["value"]["param_types"] == ["Int"]


def test_an_unannotated_arrow_with_no_expected_type_stays_untyped():
    """The one arrow shape still on the frontier — and it must *stay* silent
    rather than acquire a guessed type."""
    ir = compile_source("fn demo(n: Int) -> Int { let g = v => v + 1  return g(n) }")
    arrow = _fn_body(ir, "demo")[0]["value"]
    assert arrow["kind"] == "arrow"
    assert "param_types" not in arrow and "returns" not in arrow


@pytest.mark.parametrize("source,message", [
    # the arrow's body disagrees with the expected return type
    ('fn f(g: (Int) -> Int, x: Int) -> Int { return g(x) }\n'
     'fn d() -> Int { return f(v => "s", 1) }',
     "expects `Int`, got `Str`"),
    # a written parameter annotation narrower than the position demands
    ('fn f(g: (Float) -> Float) -> Float { return g(1) }\n'
     'fn d() -> Float { return f((v: Int) => v) }',
     "expects `Float`, got `Int`"),
    # wrong number of parameters
    ('fn f(g: (Int, Int) -> Int) -> Int { return g(1, 2) }\n'
     'fn d() -> Int { return f(v => v) }',
     "2 parameter\\(s\\), but this arrow declares 1"),
    # an arrow where a non-function type is expected
    ('fn d() -> Int { let g: Int = v => v  return 1 }',
     "expects `Int`, got an arrow"),
    # calling a function value with the wrong arity
    ('fn d() -> Int { let g: (Int) -> Int = v => v  return g(1, 2) }',
     "takes 1 argument\\(s\\), 2 given"),
    # calling a function value with the wrong argument type
    ('fn d() -> Int { let g: (Int) -> Int = v => v  return g("x") }',
     "argument 1 of `g` expects `Int`, got `Str`"),
    # reassigning a `var` of function type with a mismatched arrow
    ('fn d(n: Int) -> Int { var g: (Int) -> Int = v => v  g = v => "x"  return g(n) }',
     "expects `Int`, got `Str`"),
])
def test_function_type_mismatches_are_rejected(source, message):
    with pytest.raises(RevlError, match=message):
        compile_source(source)


def test_the_arrow_body_is_checked_against_the_expected_return():
    """The body sees the parameter types the expectation supplied, so a
    mistake *inside* the arrow is now a diagnostic rather than silence."""
    with pytest.raises(RevlError, match="operand of `!`"):
        compile_source("fn f(g: (Int) -> Bool) -> Bool { return g(1) }\n"
                       "fn d() -> Bool { return f(v => !v) }")


def test_higher_order_composition_runs_end_to_end_on_the_python_tier():
    source = (
        "type Step = (Int) -> Int\n"
        "fn compose(f: Step, g: Step) -> Step { return v => g(f(v)) }\n"
        "fn repeat_(base: Step, times: Int) -> Int {\n"
        "  var acc = 0\n"
        "  var i = 0\n"
        "  while (i < times) { acc = base(acc)  i += 1 }\n"
        "  return acc\n"
        "}\n"
        "fn pipeline(n: Int) -> Int {\n"
        "  let inc: Step = v => v + 1\n"
        "  let dbl: Step = v => v * 2\n"
        "  let both = compose(inc, dbl)\n"
        "  return repeat_(both, n)\n"
        "}\n"
    )
    module = _run_python(source)
    # (x+1)*2 applied n times from 0: 2, 6, 14, 30
    assert [module.pipeline(n) for n in (1, 2, 3, 4)] == [2, 6, 14, 30]


def test_a_function_valued_record_field_round_trips_on_the_python_tier():
    module = _run_python(
        "type Handler = { name: Str, run: (Int) -> Str }\n"
        "fn make() -> Handler { return { name: \"h\", run: v => `n=${v}` } }\n"
        "fn use_(h: Handler) -> Str { let r: (Int) -> Str = h.run  return r(7) }\n")
    assert module.use_(module.make()) == "n=7"


# ------------------------------------------------------------- backends

def test_typescript_emits_real_parameter_types_for_a_typed_arrow():
    out = _emitter("typescript").emit(compile_source(
        "fn apply_(g: (Int) -> Int, x: Int) -> Int { return g(x) }\n"
        "fn demo(n: Int) -> Int { return apply_(v => v + 1, n) }\n"))
    # `Int` is `bigint` on this tier (docs/arithmetic.md).
    assert "((v: bigint) =>" in out
    assert "g: ((a0: bigint) => bigint)" in out


def test_typescript_still_writes_any_for_an_arrow_with_no_type():
    """The admission is still correct where there *is* no type — and
    `strict` rejects only an implicit any, so it is also what compiles."""
    out = _emitter("typescript").emit(compile_source(
        "fn demo(n: Int) -> Int { let g = v => v + 1  return g(n) }"))
    assert "((v: any) =>" in out


def test_typescript_renders_a_function_typed_record_field():
    out = _emitter("typescript").emit(compile_source(
        "type Handler = { run: (Int) -> Str }\n"
        "fn f(h: Handler) -> Str { let r: (Int) -> Str = h.run  return r(1) }"))
    assert "run: ((a0: bigint) => string)" in out


def test_python_renders_a_function_typed_record_field_as_callable():
    out = _emitter("python").emit(compile_source(
        "type Handler = { run: (Int, Str) -> Bool }\n"
        "fn f(h: Handler) -> Bool { let r: (Int, Str) -> Bool = h.run  return r(1, \"a\") }"))
    assert "run: Callable[[int, str], bool]" in out
    assert "from typing import Any, Callable, Optional, Union" in out


@pytest.mark.parametrize("tier", ["java", "wasm"])
def test_the_strict_tiers_refuse_a_declared_function_type_explicitly(tier):
    """A documented limit, not silently broken output: each of these tiers
    would otherwise erase a function type to its opaque fallback (`Object`, an
    i32) and emit code that does not mean what was written.

    rust is no longer among them: it lowers a declared function type in a
    `fn`/`extern` parameter or return to `impl Fn(..)` (item 91,
    docs/function-types.md §4). Its remaining escaping-position limit is pinned
    by `test_rust_still_refuses_a_function_type_that_escapes` below."""
    emitter = _emitter(tier)
    with pytest.raises(Exception) as excinfo:
        emitter.emit(compile_source(
            "service S { fn f(g: (Int) -> Int) -> Int }\n"
            "component C provides s: S { provide s { fn f(g) { return g(1) } } }"))
    message = str(excinfo.value)
    assert "function type" in message
    assert "docs/function-types.md" in message


def test_rust_lowers_a_declared_function_type_parameter_and_return():
    """Item 91: a declared function type in a `fn` parameter or return position
    lowers to `impl Fn(..)` on rust (rustc monomorphises it), instead of the
    old blanket refusal. The `agent_loop` shape — a top-level fn over effectful
    callback arrows — is the motivating case (harness multi-tier proof)."""
    out = _emitter("rust").emit(compile_source(
        "fn agent_loop(prompt: Str, complete: (Str) -> Str, "
        "call_tool: (Str) -> Str, max_steps: Int) -> Str {\n"
        "  let first: Str = complete(prompt)\n"
        "  return call_tool(first)\n"
        "}\n"
        "fn adder(n: Int) -> (Int) -> Int { return v => v + n }"))
    assert "complete: impl Fn(String) -> String" in out
    assert "call_tool: impl Fn(String) -> String" in out
    assert "-> impl Fn(i64) -> i64" in out  # return position


def test_rust_still_refuses_a_function_type_that_escapes():
    """The position-aware lowering is honest about its remaining limit: a
    function type that *escapes* — a record field, an ADT payload, a
    `List`/`Opt`/`Map` element — still wants `Box<dyn Fn(..)>` constructed where
    the arrow is created, which the emitter cannot yet do. It is refused by
    name, not erased."""
    with pytest.raises(Exception) as excinfo:
        _emitter("rust").emit(compile_source(
            "type Handler = { run: (Int) -> Str }\n"
            "fn f(h: Handler) -> Str { let r: (Int) -> Str = h.run  return r(1) }"))
    message = str(excinfo.value)
    assert "function type" in message
    assert "docs/function-types.md" in message


@pytest.mark.parametrize("tier", ["rust", "java", "wasm"])
def test_the_strict_tiers_keep_lowering_a_local_arrow(tier):
    """No regression: java beta-reduces an arrow at the call site, wasm
    inlines it, rust emits a closure rustc can infer. A function type in a
    *declaration* is what they refuse — not arrows as such."""
    out = _emitter(tier).emit(compile_source(
        "fn apply_(n: Int) -> Int { let g = v => v + 1  return g(n) }\n"
        "service S { fn f(x: Int) -> Int }\n"
        "component C provides s: S { provide s { fn f(x) = apply_(x) } }"))
    assert out


def test_emitted_function_type_code_survives_its_own_toolchain():
    """The matrix corpus has no function-type case (its 50 cases are fixed and
    counted elsewhere), so "did the emitter raise?" has the same blind spot
    here it has everywhere: emitted code no compiler ever saw. Hand three
    shapes to python's and TypeScript's real compilers.

    Skips *loudly* when a toolchain is absent — "nothing checked it" must
    never be recorded as "it passed" (docs/conformance.md)."""
    sys.path.insert(0, str(ROOT / "tools"))
    import conformance  # noqa: PLC0415
    from validate import VALIDATORS  # noqa: PLC0415

    sources = {
        "compose": (
            "type Step = (Int) -> Int\n"
            "fn compose(f: Step, g: Step) -> Step { return v => g(f(v)) }\n"
            "fn pipeline(n: Int) -> Int {\n"
            "  let inc: Step = v => v + 1\n"
            "  let dbl: Step = v => v * 2\n"
            "  let both = compose(inc, dbl)\n"
            "  return both(n)\n"
            "}\n"
            "service S { fn f(x: Int) -> Int }\n"
            "component C provides s: S { provide s { fn f(x) = pipeline(x) } }\n"
        ),
        "record-field": (
            "type Handler = { name: Str, run: (Int) -> Str }\n"
            'fn make() -> Handler { return { name: "h", run: v => `n=${v}` } }\n'
            "fn use_(h: Handler) -> Str { let r: (Int) -> Str = h.run  return r(7) }\n"
            "service S { fn f(x: Int) -> Str }\n"
            "component C provides s: S { provide s { fn f(x) = use_(make()) } }\n"
        ),
        # nested function types, a nullary one, and an arrow that is still
        # untyped — all inside one emitted module
        "nested-and-untyped": (
            "fn hof(f: ((Int) -> Int) -> Int) -> Int { return f(v => v + 1) }\n"
            "fn nullary(g: () -> Int) -> Int { return g() }\n"
            "fn untyped(n: Int) -> Int { let g = v => v + 1  return g(n) }\n"
            "fn demo(n: Int) -> Int {\n"
            "  return hof(g => g(n)) + nullary(() => 4) + untyped(n)\n"
            "}\n"
            "service S { fn f(x: Int) -> Int }\n"
            "component C provides s: S { provide s { fn f(x) = demo(x) } }\n"
        ),
    }

    checked = 0
    for tier in ("python", "typescript"):
        validator = VALIDATORS[tier]
        if validator.unavailable():
            continue
        artifacts = [(label, conformance.emitter(tier).emit(compile_source(src)))
                     for label, src in sources.items()]
        failures = {label: detail
                    for label, (status, detail) in validator.check(artifacts).items()
                    if status != "ok"}
        assert not failures, f"{tier} emitted code its own toolchain rejects: {failures}"
        checked += 1
    if not checked:
        pytest.skip("neither the python nor the TypeScript toolchain is available")


# ---------------------------------------------------------------------------
# Arrow parameters bind in provide-method scope (roadmap 77a / FR-1)
# docs/expressible-iteration.md — the pure-helper + callback-arrow escape.
# ---------------------------------------------------------------------------

FR1_LOOP_SRC = '''type ToolReq = { name: Str, args: Str }
type Step = Final(Str) | NeedTool(ToolReq)

fn decode_response(resp: Str) -> Step {
  if (resp.slice(0, 6) == "FINAL ") {
    return Final(resp.slice(6, resp.length()))
  }
  return NeedTool({ name: resp.slice(0, 10), args: "" })
}

fn run_loop(msgs: List[Str], step: (List[Str]) -> Step, n: Int) -> Step {
  if (n <= 0) { return Final("max_steps exhausted") }
  return match step(msgs) {
    Final(answer) => Final(answer),
    NeedTool(req) => run_loop(msgs.push(req.name), step, n - 1),
  }
}

service Model { emission fn complete(h: List[Str]) -> Str }
service Loop { emission fn run(p: Str) -> Str }
component Agent requires model: Model provides agent: Loop {
  config { max_steps: Int = 8 }
  provide agent {
    fn run(session_id) {
      let msgs = ["prompt"]
      let first = emit model.complete(msgs)
      return match decode_response(first) {
        Final(answer) => answer,
        NeedTool(req) => run_loop(msgs.push(req.name),
                                  msgs2 => decode_response(emit model.complete(msgs2)),
                                  config.max_steps - 1),
      }
    }
  }
}
'''


def test_arrow_param_binds_in_provide_method_scope():
    """FR-1 exit criterion 1+2: the callback-arrow's parameter resolves inside a
    provide-method body, and a bounded recursive loop with an emitting callback
    compiles (the harness's agent-loop shape)."""
    ir = compile_source(FR1_LOOP_SRC)
    assert ir["ir_version"] == 3
    import json
    text = json.dumps(ir)
    # the callback arrow binds its parameter and the emission flows through it
    assert '"arrow"' in text and '"msgs2"' in text
    assert "complete" in text


def test_arrow_param_emission_gate_stays_honest():
    """FR-1 exit criterion 3: a *plain* method whose callback reaches an
    emission is still refused with the G4 diagnostic — the gate is not
    weakened by the new binding."""
    src = FR1_LOOP_SRC.replace(
        "service Loop { emission fn run(p: Str) -> Str }",
        "service Loop { fn run(p: Str) -> Str }")
    with pytest.raises(RevlError) as excinfo:
        compile_source(src)
    assert "declared plain, but this implementation reaches" in str(excinfo.value)


def test_arrow_param_not_misread_as_a_requirement():
    """FR-1 exit criterion 4: an arrow parameter is no longer diagnosed as a
    missing component requirement (FR-12). The binding resolves it, so a
    well-formed callback compiles; the misdirected hint is gone."""
    src = '''service Model { emission fn complete(h: List[Str]) -> Str }
service Loop { emission fn run(p: Str) -> Str }
component App requires model: Model provides loop: Loop {
  provide loop {
    fn run(prompt) {
      let msgs = ["hi"]
      return apply(prompt, msgs, msgs2 => emit model.complete(msgs2))
    }
  }
}
fn apply(p: Str, ms: List[Str], f: (List[Str]) -> Str) -> Str {
  return f(ms)
}
'''
    ir = compile_source(src)
    assert ir["ir_version"] == 3
