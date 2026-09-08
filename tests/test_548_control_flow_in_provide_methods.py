"""#548 review items 1 & 2 — `if`/`while`/`for` STATEMENTS in a provide-method
body.

The component-author review (REVL-REVIEW-2026-09-06, summarised in #548) ranked
"no `while`/`for` in provide-method bodies" and "no `if` statement in provide
bodies" its top two walls: a method could compute a value only by nesting
expressions or by sanctioned recursion, and recursion dies on rust/java/wasm.

This slice gives the provide-method grammar the same `if`/`while`/`for`
statements the module-`fn` grammar already has, with one deliberate bound: the
control-flow ARMS are PURE. A teardown-registering step (`effect`/`emit`/
`let-effect`/`await`) inside a branch is refused at lowering, so the
half-emitting-conditional and loop-teardown questions (design note
docs/design/478-component-author-ergonomics.md §Group 3) stay out of scope — the
activation frame still owns every inverse, registered at the method's top level.

The lowered IR is the SAME `if`/`while`/`for` step shape a module `fn` produces,
so each tier's method-body renderer mirrors its own fn-grammar renderer. All six
tiers emit; python/go/rust/java/wasm are compile-checked here (typescript when
its toolchain is present); the cordis-py runtime section executes the methods
and asserts their answers.

wasm now carries `if`/`while`/`for`/`break`/`continue`: item 458 closed the
tracked `for (x of xs)` remainder by re-spelling the fn `_emit_for` List-cursor
memory walk against the method path, so every tier lowers the same IR step shape.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for p in (ROOT / "src", ROOT / "tests", ROOT / "backends" / "python", ROOT / "tools"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from _backend_import import backend_emitter  # noqa: E402
from revl import RevlError, compile_source  # noqa: E402

BACKENDS = ["python", "typescript", "go", "java", "rust", "wasm"]


def _emit(source: str, backend: str):
    return backend_emitter(backend).emit(compile_source(source))


# ---------------------------------------------------------------------------
# frontend — realistic drafts compile
# ---------------------------------------------------------------------------

IF_DRAFT = """
service Grader { fn grade(n: Int) -> Str }
component G provides grader: Grader {
  provide grader {
    fn grade(n) {
      var label = `F`
      if (n >= 90) { label = `A` }
      else if (n >= 80) { label = `B` }
      else if (n >= 70) { label = `C` }
      return label
    }
  }
}
"""

WHILE_DRAFT = """
service Summer { fn triangular(n: Int) -> Int }
component S provides summer: Summer {
  provide summer {
    fn triangular(n) {
      var sum = 0
      var i = 1
      while (i <= n) { sum = sum + i; i = i + 1 }
      return sum
    }
  }
}
"""

FOR_DRAFT = """
service Totals { fn positive_sum(xs: List[Int]) -> Int }
component T provides totals: Totals {
  provide totals {
    fn positive_sum(xs) {
      var s = 0
      for (x of xs) { if (x > 0) { s = s + x } }
      return s
    }
  }
}
"""

BREAK_DRAFT = """
service Find { fn first_over(xs: List[Int], bound: Int) -> Int }
component F provides find: Find {
  provide find {
    fn first_over(xs, bound) {
      var found = 0 - 1
      for (x of xs) {
        if (x > bound) { found = x; break }
      }
      return found
    }
  }
}
"""

GUARD_THEN_EMIT_DRAFT = """
service Bus { emission fn publish(topic: Str) }
service Sink { emission[bus] fn accept(msg: Str) }
component Relay requires bus: Bus provides sink: Sink {
  provide sink {
    fn accept(msg) {
      if (msg.length() == 0) { return }
      emit bus.publish(msg)
    }
  }
}
"""

# a `break`/`continue` in a `while` — the loop-control shape every tier carries
# (wasm's method-body `for` is the tracked remainder, so its loop-control proof
# rides a `while`).
WHILE_BREAK_DRAFT = """
service Cap { fn cap_at(n: Int, ceiling: Int) -> Int }
component K provides cap: Cap {
  provide cap {
    fn cap_at(n, ceiling) {
      var acc = 0
      var i = 0
      while (i < n) {
        if (acc >= ceiling) { break }
        if (i % 2 == 0) { i = i + 1; continue }
        acc = acc + i
        i = i + 1
      }
      return acc
    }
  }
}
"""

# item 458 / issue #721: a dispatch written as PLAIN control flow — an
# `if`/`else if`/`else` that returns in every arm, with NO trailing `return`.
# This is the natural migration target away from a ternary chain
# (`n >= 90 ? `A` : n >= 80 ? `B` : …`), which is the exact "ternary-heavy
# dispatch" the harness carried only because the provide-method return checker
# used to demand a top-level trailing `return`. The method grammar now shares
# the module-`fn` terminator analysis (`_definitely_returns`): an `if`/`else`
# whose arms both return terminates the body, so the ternary and the plain
# control flow are interchangeable, byte-agreement holding on every tier.
DISPATCH_DRAFT = """
service Grader { fn grade(n: Int) -> Str }
component D provides grader: Grader {
  provide grader {
    fn grade(n) {
      if (n >= 90) { return `A` }
      else if (n >= 80) { return `B` }
      else if (n >= 70) { return `C` }
      else { return `F` }
    }
  }
}
"""

ALL_DRAFTS = {
    "if": IF_DRAFT,
    "while": WHILE_DRAFT,
    "for": FOR_DRAFT,
    "break": BREAK_DRAFT,
    "while_break": WHILE_BREAK_DRAFT,
    "guard_then_emit": GUARD_THEN_EMIT_DRAFT,
    "dispatch": DISPATCH_DRAFT,
}


@pytest.mark.parametrize("name", sorted(ALL_DRAFTS))
def test_realistic_draft_compiles_to_ir(name):
    ir = compile_source(ALL_DRAFTS[name], f"{name}.rvl")
    assert ir.get("components")


def test_method_control_flow_lowers_to_the_fn_grammar_step_shape():
    # the whole reuse argument: a method-body `if`/`while`/`for` is the SAME IR
    # step a module `fn` emits, so the emitters mirror their own fn renderers.
    ir = compile_source(FOR_DRAFT)

    def find(node, step):
        if isinstance(node, dict):
            if node.get("step") == step:
                return node
            for v in node.values():
                r = find(v, step)
                if r is not None:
                    return r
        elif isinstance(node, list):
            for v in node:
                r = find(v, step)
                if r is not None:
                    return r
        return None

    for_step = find(ir["components"][0], "for")
    assert for_step is not None
    assert set(for_step) >= {"bind", "iterable", "body"}
    assert find(for_step, "if") is not None  # nested control flow


# ---------------------------------------------------------------------------
# item 458 / issue #721 — a method whose control flow returns on EVERY path
# needs no trailing `return`, exactly as a module `fn` does. This is what lets a
# ternary-heavy dispatch migrate to plain `if`/`else` control flow.
# ---------------------------------------------------------------------------

def test_dispatch_returns_on_every_path_needs_no_trailing_return():
    # the plain-control-flow dispatch compiles with NO trailing `return`: the
    # `if`/`else if`/`else` returns in every arm, which terminates the method
    # body. It lowers to the ordinary `if` step shape (a nested `if` in the
    # `else`), no fabricated fall-through value.
    ir = compile_source(DISPATCH_DRAFT)
    comp = ir["components"][0]
    provide = next(s for s in comp["body"] if s.get("step") == "provide")
    grade = next(m for m in provide["methods"] if m["name"] == "grade")
    assert grade["body"][-1]["step"] == "if"
    assert grade["body"][-1]["else"] is not None


def test_method_terminator_analysis_matches_the_fn_grammar():
    # PARITY: the identical dispatch shape is accepted in a module `fn` and in a
    # provide method. Before item 458 the `fn` compiled and the method was
    # refused ("body never returns a value"), which is precisely why the harness
    # reached for a ternary in the method position.
    fn_src = """
fn grade(n: Int) -> Str {
  if (n >= 90) { return `A` }
  else if (n >= 80) { return `B` }
  else { return `F` }
}
"""
    method_src = """
service Grader { fn grade(n: Int) -> Str }
component D provides grader: Grader {
  provide grader {
    fn grade(n) {
      if (n >= 90) { return `A` }
      else if (n >= 80) { return `B` }
      else { return `F` }
    }
  }
}
"""
    assert compile_source(fn_src).get("functions")
    assert compile_source(method_src).get("components")


def test_missing_return_when_no_path_returns():
    # no `return` anywhere: the "never returns a value" message (T1), the same
    # the `fn` grammar and the pre-458 method check gave.
    src = """
service S { fn f(n: Int) -> Str }
component C provides s: S {
  provide s { fn f(n) { var r = `x`  r = r } }
}
"""
    with pytest.raises(RevlError) as ei:
        compile_source(src, "x.rvl")
    assert "never returns a value" in str(ei.value)


def test_missing_return_when_a_bare_if_can_fall_through():
    # a bare `if` (no `else`) returns on one path only, so control can reach the
    # end — the second, more precise message the `fn` grammar gives, now shared.
    src = """
service S { fn f(n: Int) -> Str }
component C provides s: S {
  provide s { fn f(n) { if (n > 0) { return `pos` } } }
}
"""
    with pytest.raises(RevlError) as ei:
        compile_source(src, "x.rvl")
    assert "reach the end of its body without a `return`" in str(ei.value)


def test_for_that_may_run_zero_times_is_not_a_terminator():
    # a `for`/`while` may run zero times, so a trailing loop never terminates the
    # body — matches `_definitely_returns` (rust E0308 / java missing-return).
    src = """
service S { fn f(xs: List[Int]) -> Int }
component C provides s: S {
  provide s { fn f(xs) { for (x of xs) { return x } } }
}
"""
    with pytest.raises(RevlError) as ei:
        compile_source(src, "x.rvl")
    assert "reach the end" in str(ei.value)


# ---------------------------------------------------------------------------
# every tier emits — `for`/`break` included (item 458 closed the wasm remainder)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize("name", sorted(ALL_DRAFTS))
def test_all_tiers_emit(backend, name):
    # every tier — wasm included — carries `if`/`while`/`for`/`break`/`continue`.
    out = _emit(ALL_DRAFTS[name], backend)
    assert out  # a non-empty artifact (str for most tiers, dict for wasm)


@pytest.mark.parametrize("backend", BACKENDS)
def test_for_emits_on_every_tier(backend):
    assert _emit(FOR_DRAFT, backend)


def test_wasm_lowers_method_for_to_a_cursor_walk():
    # item 458: the tracked wasm `for` remainder is gone. The method-body loop
    # lowers to the same in-memory List cursor the fn `for` walks: a `$for_ptr`
    # address, a `$for_cnt` count, a `$for_idx` slot index, and the `_SLOT`-
    # strided element load — mirroring backends/wasm/emit.py `_emit_for`.
    from _backend_import import backend_emitter as be  # noqa: PLC0415
    out = be("wasm").emit(compile_source(FOR_DRAFT))
    wat = out["T"] if isinstance(out, dict) else out
    assert "$for_ptr_1" in wat and "$for_cnt_1" in wat and "$for_idx_1" in wat
    # break/continue in a method `for` re-tests at the inner `$cnt` block
    brk = be("wasm").emit(compile_source(BREAK_DRAFT))
    wat_brk = brk["F"] if isinstance(brk, dict) else brk
    assert "$revl_mcnt_1" in wat_brk and "$revl_mbrk_1" in wat_brk


def test_python_emit_is_valid_python():
    for name in ALL_DRAFTS:
        src = _emit(ALL_DRAFTS[name], "python")
        compile(src, f"{name}.py", "exec")  # SyntaxError if the emit is broken


# ---------------------------------------------------------------------------
# refusals — the arms are pure, and the parser's redirects stand
# ---------------------------------------------------------------------------

def test_emit_inside_control_flow_is_refused():
    src = """
service Bus { emission fn publish(topic: Str) }
service Sink { emission[bus] fn accept(msg: Str) }
component Relay requires bus: Bus provides sink: Sink {
  provide sink {
    fn accept(msg) {
      if (msg.length() > 0) { emit bus.publish(msg) }
    }
  }
}
"""
    with pytest.raises(RevlError) as ei:
        compile_source(src, "x.rvl")
    assert "registering step" in str(ei.value)


def test_effect_inside_loop_is_refused():
    src = """
service Log { emission fn write(line: Str) }
service Sink { emission[log] fn dump(lines: List[Str]) }
component C requires log: Log provides sink: Sink {
  provide sink {
    fn dump(lines) {
      for (line of lines) { emit log.write(line) }
    }
  }
}
"""
    with pytest.raises(RevlError):
        compile_source(src, "x.rvl")


def test_break_outside_a_loop_is_refused():
    src = """
service S { fn f(n: Int) -> Int }
component C provides s: S {
  provide s { fn f(n) { if (n > 0) { break } return n } }
}
"""
    with pytest.raises(RevlError) as ei:
        compile_source(src, "x.rvl")
    assert "break" in str(ei.value)


def test_non_bool_condition_is_refused():
    src = """
service S { fn f(n: Int) -> Int }
component C provides s: S {
  provide s { fn f(n) { if (n) { return 1 } return 0 } }
}
"""
    with pytest.raises(RevlError) as ei:
        compile_source(src, "x.rvl")
    assert "Bool" in str(ei.value)


def test_for_over_a_non_list_is_refused():
    src = """
service S { fn f(n: Int) -> Int }
component C provides s: S {
  provide s { fn f(n) { var t = 0; for (x of n) { t = t + x } return t } }
}
"""
    with pytest.raises(RevlError) as ei:
        compile_source(src, "x.rvl")
    assert "List" in str(ei.value)


def test_statement_after_top_level_return_is_unreachable():
    src = """
service S { fn f(n: Int) -> Int }
component C provides s: S {
  provide s { fn f(n) { return n
      var x = 1 } }
}
"""
    with pytest.raises(RevlError) as ei:
        compile_source(src, "x.rvl")
    assert "unreachable" in str(ei.value)


def test_return_type_is_still_checked_through_control_flow():
    # the F4 return-check pushes into each arm: a Str where the service promises
    # Int is the compile error it always was.
    src = """
service S { fn f(n: Int) -> Int }
component C provides s: S {
  provide s { fn f(n) { if (n > 0) { return `nope` } return 0 } }
}
"""
    with pytest.raises(RevlError) as ei:
        compile_source(src, "x.rvl")
    assert "Int" in str(ei.value) and "Str" in str(ei.value)


# ---------------------------------------------------------------------------
# per-tier COMPILE validation (tools/validate.py) — skips a tier whose
# toolchain is absent; the CI matrix has them all.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("tier", ["python", "typescript", "go", "java", "rust"])
def test_control_flow_component_compiles_on_tier(tier):
    import validate  # noqa: PLC0415

    validator = validate.VALIDATORS[tier]
    reason = validator.unavailable()
    if reason:
        pytest.skip(f"{tier} toolchain unavailable: {reason}")
    src = """
service Cache { fn total(xs: List[Int]) -> Int  fn classify(n: Int) -> Str }
component C provides cache: Cache {
  provide cache {
    fn total(xs) {
      var s = 0
      for (x of xs) { if (x > 0) { s = s + x } }
      return s
    }
    fn classify(n) {
      var r = `small`
      if (n > 100) { r = `big` } else if (n > 10) { r = `mid` }
      return r
    }
  }
}
"""
    out = backend_emitter(tier).emit(compile_source(src))
    (status, detail) = validator.check([("cf", out)])["cf"]
    assert status == "ok", f"{tier} rejected the emit: {detail}"


def test_wasm_control_flow_component_validates():
    import validate  # noqa: PLC0415

    validator = validate.VALIDATORS["wasm"]
    reason = validator.unavailable()
    if reason:
        pytest.skip(f"wasm toolchain unavailable: {reason}")
    # `for`/`break` join `if`/`while` now that item 458 lowers method-body `for`.
    for draft in (WHILE_DRAFT, IF_DRAFT, WHILE_BREAK_DRAFT, FOR_DRAFT, BREAK_DRAFT):
        out = backend_emitter("wasm").emit(compile_source(draft))
        assert isinstance(out, dict)
        status, detail = validator.check([("cf", out)])["cf"]
        assert status == "ok", f"wasm rejected the emit: {detail}"


def test_wasm_method_for_runs_on_wasmtime(tmp_path):
    # the strongest proof: a method-body `for` with a `continue` executes on the
    # real substrate and returns the right scalar. It iterates an internal list
    # literal so the export takes only scalar args (wasmtime `--invoke`).
    import shutil  # noqa: PLC0415
    import subprocess  # noqa: PLC0415

    if shutil.which("wasmtime") is None:
        pytest.skip("wasmtime not installed")
    src = """
service S { fn f(n: Int) -> Int }
component C provides s: S {
  provide s {
    fn f(n) {
      var total = 0
      for (x of [10, 20, 30, 40]) {
        if (x == 30) { continue }
        total = total + x
      }
      return total + n
    }
  }
}
"""
    out = backend_emitter("wasm").emit(compile_source(src))
    wat = out["C"] if isinstance(out, dict) else out
    path = tmp_path / "c.wat"
    path.write_text(wat)
    proc = subprocess.run(
        ["wasmtime", "--invoke", "provide:s.f", str(path), "5"],
        capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    # 10 + 20 + 40 (30 skipped by `continue`) + 5
    assert proc.stdout.strip().splitlines()[-1].strip() == "75"


# ---------------------------------------------------------------------------
# cordis-py runtime — the methods RUN and their answers are asserted. Skips
# without the runtime (the `frontend-cordis` CI job installs it).
# ---------------------------------------------------------------------------

_CORDIS = importlib.util.find_spec("cordis") is not None
cordis_only = pytest.mark.skipif(not _CORDIS, reason="cordis-py runtime not installed")


async def _activate(source: str, component: str, key: str):
    """Plug `component`, settle the event loop, and return its provided
    service — the proven `plug` + settle + `reflect.get` shape from
    backends/python/tests/test_router_scenario.py. Runs only in the
    `frontend-cordis` CI job (pytest-asyncio + the runtime); skipped otherwise.
    """
    import asyncio  # noqa: PLC0415
    import types  # noqa: PLC0415

    import runtime as runtime_mod  # noqa: PLC0415
    from cordis import Context  # noqa: PLC0415

    module_src = backend_emitter("python").emit(compile_source(source))
    module = types.ModuleType("cf_exec")
    exec(compile(module_src, "cf_exec.py", "exec"), module.__dict__)
    root = Context()
    runtime_mod.plug(root, getattr(module, component))
    for _ in range(10):
        await asyncio.sleep(0)
    return root.reflect.get(key)


@cordis_only
async def test_runtime_if_grades():
    grader = await _activate(IF_DRAFT, "G", "grader")
    assert grader.grade(95) == "A"
    assert grader.grade(85) == "B"
    assert grader.grade(72) == "C"
    assert grader.grade(50) == "F"


@cordis_only
async def test_runtime_while_triangular():
    summer = await _activate(WHILE_DRAFT, "S", "summer")
    assert summer.triangular(5) == 15
    assert summer.triangular(0) == 0


@cordis_only
async def test_runtime_for_positive_sum():
    totals = await _activate(FOR_DRAFT, "T", "totals")
    assert totals.positive_sum([3, -2, 5, -1, 4]) == 12
    assert totals.positive_sum([]) == 0


@cordis_only
async def test_runtime_break_first_over():
    find = await _activate(BREAK_DRAFT, "F", "find")
    assert find.first_over([1, 2, 9, 4, 10], 5) == 9
    assert find.first_over([1, 2, 3], 5) == -1


@cordis_only
async def test_runtime_while_break_and_continue():
    cap = await _activate(WHILE_BREAK_DRAFT, "K", "cap")

    def reference(n, ceiling):  # the same logic, in python
        acc, i = 0, 0
        while i < n:
            if acc >= ceiling:
                break
            if i % 2 == 0:
                i += 1
                continue
            acc += i
            i += 1
        return acc

    for n, ceiling in ((10, 100), (10, 5), (0, 3), (8, 9)):
        assert cap.cap_at(n, ceiling) == reference(n, ceiling), (n, ceiling)


@cordis_only
async def test_runtime_dispatch_grades():
    # the plain-control-flow dispatch (no trailing return) runs and answers
    # exactly as the ternary/label spellings do.
    grader = await _activate(DISPATCH_DRAFT, "D", "grader")
    assert grader.grade(95) == "A"
    assert grader.grade(85) == "B"
    assert grader.grade(72) == "C"
    assert grader.grade(50) == "F"
