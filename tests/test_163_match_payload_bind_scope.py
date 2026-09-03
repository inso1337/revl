"""Roadmap item 163 — a match arm's payload bind is ARM-LOCAL on the py tier,
including the `awaited` (async-colored) binder.

`_match_expr` binds a payload arm's name with a one-shot `lambda`, which gives
the bind a scope of its own. An arm that crosses an async boundary cannot use a
lambda (`await` inside one is a py `SyntaxError`), so item 263 gave the awaited
path a WALRUS instead — and a walrus assigns in the ENCLOSING frame. A bind that
should be arm-local therefore wrote through to a local of the function holding
the match and silently clobbered it. Three shapes went wrong, all of them
correct in the non-awaited spelling:

  * an arm bind shadowing an enclosing local (`let v = 5` then `Some(v) => …`),
  * a nested match rebinding the same name,
  * an arrow in an arm body capturing the bind.

No fixture reached the awaited binder, which is why it sat there from item 263.
These are those fixtures. Each one is measured by RUNNING the emitted module:
the programs are module `fn`s over an async extern, so the emitted py module is
standalone and `asyncio.run` reaches the arm directly — no cordis-py runtime
needed, so the check never degrades to a skip.
"""

import asyncio
import importlib.util
import inspect
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402
from revl.test import RUNNERS  # noqa: E402

# `twice` is an async extern, so every module fn reaching it is colored async
# and its match renders through the AWAITED payload binder (item 263).
_HDR = (
    'extern emission async fn twice(n: Int) -> Int\n'
    '  = @ts { return await Promise.resolve(n * 2) }\n'
    '  = @py { return n * 2 }\n'
    'fn wrap(n: Int) -> Opt[Int] { return Some(n) }\n'
    'fn call1(f: (Int) -> Int) -> Int { return f(1) }\n'
)

# name -> (body, expected value at n = 7)
#
# `shadow`: the headline shape. The arm binds `v` over an enclosing `let v = 5`;
#   `r + v` must read 5, not the payload. Walrus binder: 14 + 7 = 21.
# `nested`: the inner arm rebinds `v`, then the outer arm reads its OWN `v`
#   (python evaluates the left operand of `+` first, so the inner bind has
#   already happened). Walrus binder: 2 + 1 = 3.
# `arrow`:  same nesting, but the outer `v` is read through an arrow that
#   captured the bind. Walrus binder: 2 + (1 + 1) = 4.
_CASES = {
    "shadow": (
        '  let v = 5\n'
        '  let r = match wrap(n) { Some(v) => twice(v), None => 0 }\n'
        '  return r + v\n',
        19,
    ),
    "nested": (
        '  let r = match wrap(n) {\n'
        '    Some(v) => (match wrap(1) { Some(v) => twice(v), None => 0 }) + v,\n'
        '    None => 0,\n'
        '  }\n'
        '  return r\n',
        9,
    ),
    "arrow": (
        '  let r = match wrap(n) {\n'
        '    Some(v) => (match wrap(1) { Some(v) => twice(v), None => 0 })\n'
        '               + call1((y) => y + v),\n'
        '    None => 0,\n'
        '  }\n'
        '  return r\n',
        10,
    ),
}

# The same three shapes with a SYNC arm body (`v * 2` instead of the async
# extern), which has always taken the lambda binder. They are the control: the
# awaited answers above must equal these, and these must not regress.
_SYNC_CASES = {
    "shadow": (
        '  let v = 5\n'
        '  let r = match wrap(n) { Some(v) => v * 2, None => 0 }\n'
        '  return r + v\n',
        19,
    ),
    "nested": (
        '  let r = match wrap(n) {\n'
        '    Some(v) => (match wrap(1) { Some(v) => v * 2, None => 0 }) + v,\n'
        '    None => 0,\n'
        '  }\n'
        '  return r\n',
        9,
    ),
    "arrow": (
        '  let r = match wrap(n) {\n'
        '    Some(v) => (match wrap(1) { Some(v) => v * 2, None => 0 })\n'
        '               + call1((y) => y + v),\n'
        '    None => 0,\n'
        '  }\n'
        '  return r\n',
        10,
    ),
}


def _py_emit(ir):
    spec = importlib.util.spec_from_file_location(
        "revl_py_emit_163", ROOT / "backends" / "python" / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.emit(ir)


def _source(name: str, body: str) -> str:
    return _HDR + f"fn {name}(n: Int) -> Int {{\n" + body + "}\n"


def _run_emitted(name: str, body: str, arg: int):
    """Emit the module, exec it, and call the fn. A module-fn-only program
    emits a standalone py module, so this measures the arm's real value."""
    out = _py_emit(compile_source(_source(name, body), "item163.rvl"))
    namespace: dict = {}
    exec(compile(out, f"<emitted:{name}>", "exec"), namespace)  # noqa: S102
    fn = namespace[name]
    return asyncio.run(fn(arg)) if inspect.iscoroutinefunction(fn) else fn(arg)


@pytest.mark.parametrize("name", sorted(_CASES))
def test_awaited_payload_bind_is_arm_local(name):
    """The bug: each of the three shapes read the payload back through the
    enclosing frame under the walrus binder."""
    body, expected = _CASES[name]
    assert _run_emitted(name, body, 7) == expected


@pytest.mark.parametrize("name", sorted(_SYNC_CASES))
def test_sync_payload_bind_is_arm_local(name):
    """The control: the lambda binder was already right, and stays right."""
    body, expected = _SYNC_CASES[name]
    assert _run_emitted(name, body, 7) == expected


@pytest.mark.parametrize("name", sorted(_CASES))
def test_awaited_and_sync_arms_agree(name):
    """An arm crossing an async boundary must not change what the match MEANS.
    Both spellings of a shape answer the same, which is the property the walrus
    binder broke."""
    assert _run_emitted(name, _CASES[name][0], 7) == \
        _run_emitted(name, _SYNC_CASES[name][0], 7)


def test_awaited_binder_does_not_assign_the_bind_in_the_enclosing_frame():
    """The structural half, so the regression is caught at emit and not only by
    a value: the awaited binder gives the payload a scope (a one-element
    comprehension) rather than a walrus that writes through to the frame."""
    body, _ = _CASES["shadow"]
    out = _py_emit(compile_source(_source("shadow", body), "item163.rvl"))
    line = next(ln for ln in out.splitlines() if ln.strip().startswith("r ="))
    assert "(v := " not in line, line
    assert "for v in (match,)" in line, line
    # the `await` still lands in the enclosing `async def`, never in a lambda
    assert "await twice(v)" in line, line
    assert "lambda" not in line, line


def test_a_payload_bound_under_an_emitter_walrus_temp_still_compiles():
    """The one name the comprehension cannot take: python refuses an assignment
    expression that rebinds a comprehension's iteration variable, and `_bi` is
    the emitter's own bounded-arithmetic walrus temp. Such a bind keeps the
    older binder — it is already clobbered by the scaffolding that owns the
    name, with or without a match, so the point here is that it EMITS AND RUNS
    rather than becoming a py `SyntaxError`."""
    body = ('  let r = match wrap(n) { Some(_bi) => twice(_bi + 1), None => 0 }\n'
            '  return r\n')
    assert _run_emitted("bitmp", body, 7) == 16


# -- the same shapes end to end, on both runtime tiers ----------------------

_RUN = (
    _HDR
    + "fn shadow(n: Int) -> Int {\n" + _CASES["shadow"][0] + "}\n"
    + "fn nested(n: Int) -> Int {\n" + _CASES["nested"][0] + "}\n"
    + "fn arrowed(n: Int) -> Int {\n" + _CASES["arrow"][0] + "}\n"
    + 'service S { emission async fn go(n: Int) -> Int }\n'
    + 'component C provides s: S {\n'
    + '  provide s {\n'
    + '    async fn go(n) { return shadow(n) + nested(n) + arrowed(n) }\n'
    + '  }\n'
    + '}\n'
    + 'lifecycle test "an arm payload bind never clobbers an enclosing local" {\n'
    + '  load C\n'
    + '  let got = call s.go(7)\n'
    + '  assert got == 38\n'
    + '  unload C\n'
    + '  assert no_residue\n'
    + '}\n'
)


@pytest.mark.parametrize("tier", ["py", "ts"])
def test_payload_bind_scope_executes(tier):
    status, message = RUNNERS[tier](compile_source(_RUN, "item163.rvl"))
    if status == "skip":
        pytest.skip(f"{tier}: {message}")
    assert status == "pass", f"{tier} failed: {message}"


# --------------------------------------------------------------------------
# roadmap item 436 F3, the other half of this decision: the two arm shapes
# that need NO binder, and the guard that keeps every shape above on one.
#
# These live here rather than in a file of their own because they are the SAME
# decision the three cases above pin: a binder-free arm is safe exactly when
# there is no name to shadow, clobber or share a cell with. `shadow`, `nested`
# and `arrow` are the arms that must NOT lose their binder, and the tests above
# already run them for their value.

_UNBOUND_HDR = 'fn wrap2(n: Int) -> Opt[Int] { return Some(n) }\n'


def _emit_fn(src: str) -> str:
    return _py_emit(compile_source(src, "item436f3.rvl"))


def _body_line(out: str, prefix: str) -> str:
    return next(ln for ln in out.splitlines() if ln.strip().startswith(prefix))


def test_body_that_is_the_bind_needs_no_binder():
    """`Some(v) => v` and `Ok(v) => v` — the unwrap `match` mostly exists for.
    The bind IS the body, so the arm is the payload expression and no function
    object is built or called (item 436 F3)."""
    out = _emit_fn(
        _UNBOUND_HDR
        + 'fn unwrap_or(n: Int, d: Int) -> Int {\n'
          '  return match wrap2(n) { Some(v) => v, None => d }\n'
          '}\n')
    line = _body_line(out, "return (match")
    assert "lambda" not in line, line
    assert line.strip().startswith("return (match if (match := "), line
    namespace: dict = {}
    exec(compile(out, "<436f3>", "exec"), namespace)  # noqa: S102
    assert namespace["unwrap_or"](7, 0) == 7


def test_bind_the_body_never_reads_needs_no_binder():
    """`Err(e) => -1` binds a payload nothing reads. The binder goes, and so
    does the payload read: `match.value` is a `__slots__` load on a case the
    `isinstance` already matched, so not performing it is not observable."""
    out = _emit_fn(
        'type Res = Good(Int) | Bad(Int)\n'
        'fn code(r: Res) -> Int { return match r { Good(v) => v, Bad(e) => 0 } }\n')
    line = _body_line(out, "return (match")
    assert "lambda" not in line, line
    assert "(0 if isinstance(match, Bad)" in line, line
    namespace: dict = {}
    exec(compile(out, "<436f3>", "exec"), namespace)  # noqa: S102
    assert namespace["code"](namespace["Good"](5)) == 5
    assert namespace["code"](namespace["Bad"](5)) == 0


def test_a_bind_read_inside_a_larger_body_keeps_its_binder():
    """The guard. `Some(v) => v + 1` reads the bind from inside an expression,
    so the arm keeps its one-shot lambda: dropping it would leave `v` free, and
    binding it with a walrus would write through to the enclosing frame, which
    is the item-163 bug the three cases above pin."""
    out = _emit_fn(
        _UNBOUND_HDR
        + 'fn classify(n: Int) -> Int {\n'
          '  return match wrap2(n) { Some(v) => v + 1, _ => 0 }\n'
          '}\n')
    line = _body_line(out, "return (")
    assert "(lambda v:" in line, line
    namespace: dict = {}
    exec(compile(out, "<436f3>", "exec"), namespace)  # noqa: S102
    assert namespace["classify"](7) == 8


@pytest.mark.parametrize("name", sorted(_SYNC_CASES))
def test_the_three_shadowing_shapes_keep_their_binder(name):
    """Structural, so the guard is caught at emit and not only by a value:
    each shape reads its bind from inside a larger body, so each keeps the
    lambda that gives the bind a scope."""
    body, _ = _SYNC_CASES[name]
    out = _py_emit(compile_source(_source(name, body), "item163.rvl"))
    line = _body_line(out, "r =" if name != "shadow" else "r =")
    assert "(lambda v:" in line, line


def test_a_component_body_match_decides_the_binder_the_same_way():
    """`_ComponentEmitter` renders arm bodies BEFORE `_match_expr` sees them,
    so the binder decision reads the node the text came from. Without that the
    rendered body is opaque, every arm reads as "never mentions the bind", and
    a live bind is deleted — which is what this pins."""
    out = _emit_fn(
        'type Step = Final(Int) | NeedTool(Int)\n'
        'service R { fn label(s: Step) -> Int\n  fn bump(s: Step) -> Int }\n'
        'component C provides r: R {\n'
        '  provide r {\n'
        '    fn label(s) = match s { Final(n) => n, NeedTool(t) => 0 }\n'
        '    fn bump(s) = match s { Final(n) => n + 1, NeedTool(t) => t }\n'
        '  }\n'
        '}\n')
    lines = [ln for ln in out.splitlines() if "isinstance((match := s), Final)" in ln]
    assert len(lines) == 2, out
    label, bump = lines
    assert "lambda" not in label, label
    assert "(lambda n: (n + 1))(match.value)" in bump, bump
