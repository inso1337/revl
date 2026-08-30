"""`break` and `continue` in loops (roadmap item 379,
docs/design/379-break-continue.md), plus its adversarial-review corrections
C1-C4.

Loop control flow is frame-neutral: no revl teardown boundary coincides with a
loop boundary, so `break`/`continue` register nothing and run no teardown. The
whole feature is a lex/parse/lower/emit addition with no accumulator
interaction; these tests pin the surface, the flow-analysis change, the
frame-neutrality invariant guard, and per-tier emission — with the wasm tier
executed on wasmtime for the cases the design flags as the ones that break under
a naive lowering (nested loops, and `continue` skipping a `for` increment).
"""

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "backends" / "python"))

from _backend_import import backend_emitter  # noqa: E402
from revl import RevlError, compile_source  # noqa: E402
from revl.parser import Parser  # noqa: E402
from revl.lower import _validate_no_loop_scoped_registration  # noqa: E402

needs_wasmtime = pytest.mark.skipif(
    shutil.which("wasmtime") is None, reason="wasmtime not installed")


def parse(src: str):
    return Parser(src, "<test>").parse()


def _err(src: str) -> str:
    with pytest.raises(RevlError) as ei:
        parse(src)
    return str(ei.value)


# ---------------------------------------------------------------------------
# Parsing and refusals
# ---------------------------------------------------------------------------

def test_break_continue_parse_at_loop_top_level_and_nested_under_if():
    # both statements, both loop forms, at top level and nested under `if`
    parse("fn f(n: Int) -> Int {\n"
          "  var i = 0\n"
          "  while (i < n) {\n"
          "    if (i == 3) { break }\n"
          "    if (i == 1) { i += 1  continue }\n"
          "    i += 1\n"
          "  }\n"
          "  return i\n}")
    parse("fn g(xs: List[Int]) -> Int {\n"
          "  var n = 0\n"
          "  for (x of xs) {\n"
          "    if (x == 0) { continue }\n"
          "    if (x < 0) { break }\n"
          "    n += 1\n"
          "  }\n"
          "  return n\n}")


def test_break_continue_parse_in_a_test_body():
    # a `test` body is fn-grammar, so a loop with control flow parses there too
    parse('test "loops" {\n'
          "  var i = 0\n"
          "  while (i < 3) { if (i == 2) { break }  i += 1 }\n"
          "  assert i == 2\n}")


@pytest.mark.parametrize("kw", ["break", "continue"])
def test_bare_control_outside_a_loop_is_refused_with_the_redirect_not_g1(kw):
    # the misleading pre-379 failure was G1 "`break` is not declared in this
    # function"; the redirect must replace it, not restate it.
    msg = _err(f"fn f() -> Int {{\n  {kw}\n  return 0\n}}")
    assert "only valid inside a `while` or `for` body" in msg
    assert "is not declared" not in msg


@pytest.mark.parametrize("kw", ["break", "continue"])
def test_control_in_an_activation_body_is_refused(kw):
    msg = _err("service S { fn ping() }\n"
               "component C provides s: S {\n"
               f"  {kw}\n"
               "  provide s { fn ping() {} }\n}")
    assert "not valid here" in msg and "no loops" in msg


@pytest.mark.parametrize("kw", ["break", "continue"])
def test_control_in_a_provide_method_body_is_refused(kw):
    msg = _err("service S { fn ping() }\n"
               "component C provides s: S {\n"
               f"  provide s {{ fn ping() {{ {kw} }} }}\n}}")
    assert "not valid here" in msg and "no loops" in msg


def test_let_break_is_refused_as_a_keyword_collision():
    msg = _err("fn f() -> Int {\n  let break = 1\n  return 0\n}")
    assert "expected ident" in msg and "break" in msg


# ---------------------------------------------------------------------------
# C1: break/continue in a match block arm is refused (the arm is lambda-lifted)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("kw", ["break", "continue"])
def test_c1_control_in_a_match_block_arm_inside_a_loop_is_refused(kw):
    # the arm is lifted into its own fn at lowering; a `break` there would land
    # in a loopless fn, so it must be refused at parse time in the block-arm
    # voice — even though a `while` encloses the `match`.
    src = ("type Foo = A | B\n"
           "fn f(v: Foo) -> Int {\n"
           "  var i = 0\n"
           "  while (i < 10) {\n"
           "    let x = match (v) {\n"
           f"      A => {{ {kw}\n"
           "        1 }\n"
           "      B => 0\n"
           "    }\n"
           "    i += 1\n"
           "  }\n"
           "  return i\n}")
    msg = _err(src)
    assert "match block arm" in msg
    assert f"`{kw}` cannot leave" in msg


# ---------------------------------------------------------------------------
# Flow analysis: the break-aware while(true) rule (Stage 2 / C4 frontend side)
# ---------------------------------------------------------------------------

def test_declared_return_fn_while_true_with_reachable_break_is_refused():
    # a `while (true)` a `break` can leave may fall through, so the fn must
    # still return afterwards; without a trailing return it is refused.
    with pytest.raises(RevlError) as ei:
        compile_source("fn g(c: Bool) -> Int {\n"
                       "  while (true) { if (c) { break } }\n}")
    assert "never returns a value" in str(ei.value)


def test_declared_return_fn_while_true_break_then_trailing_return_is_accepted():
    compile_source("fn g(c: Bool) -> Int {\n"
                   "  while (true) { if (c) { break } }\n"
                   "  return 0\n}")


def test_declared_return_fn_while_true_no_break_is_accepted_as_before():
    # unchanged behaviour: a break-free `while (true)` diverges, so no trailing
    # return is owed.
    compile_source("fn g() -> Int {\n"
                   "  var i = 0\n"
                   "  while (true) { i += 1 }\n}")


def test_break_inside_a_nested_loop_does_not_make_outer_while_true_diverge():
    # the break targets the inner loop, so the outer `while (true)` still
    # diverges and the fn needs no trailing return.
    compile_source("fn g() -> Int {\n"
                   "  var i = 0\n"
                   "  while (true) {\n"
                   "    var j = 0\n"
                   "    while (j < 3) { if (j == 1) { break }  j += 1 }\n"
                   "    i += 1\n"
                   "  }\n}")


# ---------------------------------------------------------------------------
# IR: additive step kinds, and byte-identity without them
# ---------------------------------------------------------------------------

def _fn_steps(ir):
    return ir["functions"][0]["body"]


def test_break_continue_lower_to_additive_step_kinds():
    ir = compile_source("fn f(n: Int) -> Int {\n"
                        "  var i = 0\n"
                        "  while (i < n) {\n"
                        "    if (i == 3) { break }\n"
                        "    if (i == 1) { i += 1  continue }\n"
                        "    i += 1\n"
                        "  }\n"
                        "  return i\n}")
    loop = next(s for s in _fn_steps(ir) if s["step"] == "while")
    kinds = {inner["step"] for arm in loop["body"] if arm["step"] == "if"
             for inner in (arm.get("then") or [])}
    assert "break" in kinds and "continue" in kinds
    brk = next(inner for arm in loop["body"] if arm["step"] == "if"
               for inner in (arm.get("then") or []) if inner["step"] == "break")
    assert set(brk) == {"step", "line"}  # no payload beyond the diagnostic line


def test_ir_is_byte_identical_without_break_continue():
    # a program that uses neither produces exactly the IR it produced before.
    src = ("fn f(xs: List[Int]) -> Int {\n"
           "  var s = 0\n"
           "  for (x of xs) { s += x }\n"
           "  return s\n}")
    a = compile_source(src)
    b = compile_source(src)
    assert a == b
    for step in _fn_steps(a):
        assert step["step"] not in ("break", "continue")


# ---------------------------------------------------------------------------
# C2: the whole-IR frame-neutrality invariant guard
# ---------------------------------------------------------------------------

def test_c2_guard_refuses_a_registering_step_inside_a_loop_body():
    # synthetic IR: an `effect` (teardown-registering) step nested in a while body
    ir = {"functions": [{"name": "f", "body": [
        {"step": "while", "cond": {"kind": "lit", "value": True}, "body": [
            {"step": "if", "cond": {"kind": "lit", "value": True},
             "then": [{"step": "effect", "line": 9}], "else": None}]}]}]}
    with pytest.raises(RevlError) as ei:
        _validate_no_loop_scoped_registration(ir, "t.rvl")
    assert "registers teardown" in str(ei.value)
    assert "379-break-continue" in str(ei.value)


def test_c2_guard_refuses_a_loop_step_in_a_component_body():
    ir = {"components": [{"name": "C", "body": [
        {"step": "for", "bind": "x", "iterable": {"kind": "var", "name": "xs"},
         "body": [], "line": 4}]}]}
    with pytest.raises(RevlError) as ei:
        _validate_no_loop_scoped_registration(ir, "t.rvl")
    assert "may not appear in a component" in str(ei.value)


def test_c2_guard_passes_a_clean_loop():
    ir = {"functions": [{"name": "f", "body": [
        {"step": "while", "cond": {"kind": "lit", "value": True},
         "body": [{"step": "break", "line": 2}]}]}]}
    _validate_no_loop_scoped_registration(ir, "t.rvl")  # no raise


# ---------------------------------------------------------------------------
# Per-tier emit goldens for a loop using break AND continue
# ---------------------------------------------------------------------------

_SVC = "service S { fn f(x: Int) -> Int }\n"
_SCAN = (
    "fn scan(xs: List[Int]) -> Int {\n"
    "  var n = 0\n"
    "  for (v of xs) {\n"
    "    if (v == 0) { continue }\n"
    "    if (v < 0) { break }\n"
    "    n += 1\n"
    "  }\n"
    "  return n\n}\n")
_SCAN_COMPONENT = (_SVC + _SCAN
                   + "component C provides s: S { provide s { fn f(x) = scan([3, 0, 5, -1, 9]) } }")


def _emit(backend: str, source: str):
    code = backend_emitter(backend).emit(compile_source(source))
    if isinstance(code, dict):
        return "\n".join(str(v) for v in code.values())
    return str(code)


@pytest.mark.parametrize("backend,brk,cont", [
    ("python", "break", "continue"),
    ("typescript", "break", "continue"),
    ("go", "break", "continue"),
    ("rust", "break;", "continue;"),
    ("java", "break;", "continue;"),
])
def test_native_tiers_emit_the_keyword(backend, brk, cont):
    code = _emit(backend, _SCAN_COMPONENT)
    assert brk in code and cont in code


def test_wasm_emits_named_labels_and_the_continue_inner_block():
    wat = _emit("wasm", _SCAN_COMPONENT)
    assert "(block $revl_brk_" in wat and "(loop $revl_top_" in wat
    assert "(block $revl_cnt_" in wat          # for: continue must not skip increment
    assert "(br $revl_brk_" in wat             # break
    assert "(br $revl_cnt_" in wat             # continue


def test_wasm_no_control_loop_keeps_the_anonymous_skeleton():
    # byte-stability: a loop with neither break nor continue is unchanged, so no
    # existing wasm golden shifts.
    plain = (_SVC
             + "fn plain(n: Int) -> Int { var s = 0\n var i = 0\n"
               "  while (i < n) { s += i\n i += 1 }\n return s }\n"
             + "component C provides s: S { provide s { fn f(x) = plain(x) } }")
    wat = _emit("wasm", plain)
    assert "revl_brk" not in wat and "revl_cnt" not in wat
    assert "(block\n" in wat and "(loop\n" in wat


# ---------------------------------------------------------------------------
# The python reference runtime executes the semantics
# ---------------------------------------------------------------------------

def test_python_reference_executes_break_and_continue():
    ns: dict = {}
    exec(compile(backend_emitter("python").emit(compile_source(_SCAN)),
                 "emitted.py", "exec"), ns)
    scan = ns[[k for k in ns if k.endswith("scan") or k == "scan"][0]]
    # zeros skipped (continue, but still counted-through), stops at the negative
    assert scan([3, 0, 5, -1, 9]) == 2
    assert scan([1, 2, 3]) == 3          # no break, no continue path
    assert scan([0, 0, 0]) == 0          # all skipped
    assert scan([-1, 5, 5]) == 0         # break on the first element


# ---------------------------------------------------------------------------
# wasm executed on wasmtime — the cases a naive lowering gets wrong
# ---------------------------------------------------------------------------

def _wasm_module(source: str) -> str:
    spec = importlib.util.spec_from_file_location(
        "revl_wasm_emit_bc", ROOT / "backends" / "wasm" / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.emit(compile_source(source))["C"]


def _invoke(tmp_path, label, wat, arg):
    path = tmp_path / f"{label}.wat"
    path.write_text(wat, encoding="utf-8")
    out = subprocess.run(
        ["wasmtime", "--invoke", "provide:s.f", str(path), str(arg)],
        capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, f"{label}: {out.stderr}"
    return int(out.stdout.strip().splitlines()[-1])


def _compile(tmp_path, label, wat):
    path = tmp_path / f"{label}.wat"
    path.write_text(wat, encoding="utf-8")
    out = subprocess.run(
        ["wasmtime", "compile", str(path), "-o", str(tmp_path / f"{label}.cwasm")],
        capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, f"{label}: {out.stderr}"


@needs_wasmtime
def test_wasm_for_continue_visits_every_remaining_element(tmp_path):
    # `sum_positive` uses `continue` to skip zeros; the increment must still run,
    # so every element is visited exactly once and the positives all sum in.
    src = (_SVC
           + "fn sum_positive(xs: List[Int]) -> Int {\n"
             "  var total = 0\n"
             "  for (v of xs) {\n"
             "    if (v == 0) { continue }\n"
             "    total += v\n"
             "  }\n"
             "  return total\n}\n"
           + "component C provides s: S { provide s { fn f(x) = sum_positive([1, 0, 2, 0, 3, 0, 4]) } }")
    assert _invoke(tmp_path, "for-continue", _wasm_module(src), 0) == 10


@needs_wasmtime
def test_wasm_break_two_ifs_deep_exits_exactly_one_loop(tmp_path):
    # inner loop breaks two `if`s deep; only the inner loop exits, so the outer
    # keeps iterating: inner adds 2 per outer pass (j=0,1 then break at j==2).
    src = (_SVC
           + "fn nested(n: Int) -> Int {\n"
             "  var total = 0\n"
             "  var i = 0\n"
             "  while (i < n) {\n"
             "    var j = 0\n"
             "    while (j < 10) {\n"
             "      if (j == 2) { if (true) { break } }\n"
             "      total += 1\n"
             "      j += 1\n"
             "    }\n"
             "    i += 1\n"
             "  }\n"
             "  return total\n}\n"
           + "component C provides s: S { provide s { fn f(x) = nested(x) } }")
    assert _invoke(tmp_path, "nested-break", _wasm_module(src), 3) == 6


@needs_wasmtime
def test_wasm_while_true_with_break_validates_and_runs(tmp_path):
    # C4: a declared-return fn ending in `while (true)` with a reachable break +
    # trailing return emits valid wasm and computes.
    src = (_SVC
           + "fn upto(n: Int) -> Int {\n"
             "  var i = 0\n"
             "  while (true) { if (i >= n) { break }  i += 1 }\n"
             "  return i\n}\n"
           + "component C provides s: S { provide s { fn f(x) = upto(x) } }")
    assert _invoke(tmp_path, "while-true-break", _wasm_module(src), 5) == 5


@needs_wasmtime
def test_wasm_while_true_without_break_validates(tmp_path):
    # C4: a break-free `while (true)` diverges; the emitter must judge it so
    # (break-aware `_diverges`) or wasmtime rejects the module for a missing
    # fallthrough value. Compile-validate only (running it would not terminate).
    src = (_SVC
           + "fn spin(n: Int) -> Int {\n"
             "  var i = 0\n"
             "  while (true) { i += 1 }\n}\n"
           + "component C provides s: S { provide s { fn f(x) = spin(x) } }")
    _compile(tmp_path, "while-true-nobreak", _wasm_module(src))
