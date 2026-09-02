"""async colour through a SPAWNED HANDLE's emission — roadmap item 98 (harness
milestone 33, finding #27).

`emit w1.wtask.run(x)` reaches a suspension exactly as `emit model.complete(x)`
does, but the two receivers land in different IR slots: a required key sits in
the call's `target`, while a spawn handle's provision is lowered to an
`instance-get` reached through the `callee` field chain. The async-reach
predicate read only the `target` shape, so a handle emission was invisible to
it. The consequences, both reproduced on main before the fix:

  * a SYNC-typed arrow delegating to a spawned async worker was ADMITTED
    instead of refused — the emitted py handed its caller a bare coroutine and
    the run died with "coroutine ... was never awaited" (finding #27);
  * a SYNC provide method reaching one directly was admitted too, the
    handle twin of the item-117 blind spot.

`_async_service_op` now answers for both receivers, so the arrow-colour leak
refusal, the A1 provide-method verdict and the teardown/activation admission
rules all see a spawned instance's async op.
"""

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

from revl.compiler import compile_source
from revl.errors import RevlError

ROOT = Path(__file__).resolve().parents[1]


def _emit(backend: str, ir):
    spec = importlib.util.spec_from_file_location(
        f"revl_{backend}_emit_98", ROOT / "backends" / backend / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.emit(ir)


# The milestone-33 shape, parameterised over the callback's declared type: a
# spawner fans work out to a spawned worker through an arrow.
def _program(callback_type: str, go_class: str = "async fn") -> str:
    return (
        "extern emission async fn remote(p: Str) -> Str\n"
        '  = @py { return p + " -> done" }\n'
        '  = @ts { return p + " -> done" }\n'
        "service WTask { emission async fn run(prompt: Str) -> Str }\n"
        "service Runner { emission async fn go(prompt: Str) -> Str }\n"
        f"fn fan(task: Str, run_one: {callback_type}) -> Str {{\n"
        "  let r = run_one(task)\n"
        "  return r\n"
        "}\n"
        "component Worker provides wtask: WTask {\n"
        "  config { tag: Str }\n"
        "  provide wtask { async fn run(prompt) = remote(prompt) }\n"
        "}\n"
        "component Fanout provides runner: Runner {\n"
        '  let w1 = effect spawn Worker with { tag: "1" } undo w1.dispose()\n'
        "  provide runner {\n"
        f"    {go_class} go(prompt) = fan(prompt, t => emit w1.wtask.run(t))\n"
        "  }\n"
        "}\n"
    )


ASYNC_CB = "(Str) -> Async[Str]"


def _arrows(node, acc: list) -> list:
    if isinstance(node, dict):
        if node.get("kind") == "arrow":
            acc.append(node)
        for value in node.values():
            _arrows(value, acc)
    elif isinstance(node, list):
        for value in node:
            _arrows(value, acc)
    return acc


# -- the refusals -----------------------------------------------------------

def test_sync_typed_arrow_reaching_a_spawned_async_op_is_refused():
    # finding #27: this compiled clean on main and leaked a coroutine at run.
    with pytest.raises(RevlError, match="no async color"):
        compile_source(_program("(Str) -> Str"), "t.rvl")


def test_sync_provide_method_reaching_a_spawned_async_op_is_refused():
    # the handle twin of item 117's req-op blind spot, culprit named in full.
    src = (
        "service WTask { emission async fn run(prompt: Str) -> Str }\n"
        "service Runner { emission fn go(prompt: Str) -> Str }\n"
        "component Worker provides wtask: WTask {\n"
        "  config { tag: Str }\n"
        "  provide wtask { async fn run(prompt) { return prompt } }\n"
        "}\n"
        "component Fanout provides runner: Runner {\n"
        '  let w1 = effect spawn Worker with { tag: "1" } undo w1.dispose()\n'
        "  provide runner { fn go(prompt) { return emit w1.wtask.run(prompt) } }\n"
        "}\n"
    )
    with pytest.raises(RevlError, match=r"async operation `w1\.wtask\.run`"):
        compile_source(src, "t.rvl")


# -- the admitted, coloured shape -------------------------------------------

def test_async_typed_slot_colors_the_handle_arrow():
    ir = compile_source(_program(ASYNC_CB), "t.rvl")
    arrows = _arrows(ir["components"], [])
    assert arrows and all(a.get("async") for a in arrows)
    fan = [f for f in ir["functions"] if f["name"] == "fan"][0]
    assert fan.get("async") is True


def test_py_emits_a_tail_coroutine_lambda_for_the_handle_emission():
    py = _emit("python", compile_source(_program(ASYNC_CB), "t.rvl"))
    assert "async def fan(task, run_one):" in py
    assert "r = (await run_one(task))" in py
    # a tail call of a spawned async op is a plain lambda returning the
    # coroutine, NOT the `_revl_as_async` sync wrap that ate the await.
    assert "_revl_as_async" not in py


# -- the exit test: live execution, an unawaited coroutine is a failure ------

_EXIT = _program(ASYNC_CB) + (
    'lifecycle test "the spawned async worker is awaited through the arrow" {\n'
    "  load Fanout\n"
    '  let out = call runner.go("hi")\n'
    '  assert out == "hi -> done"\n'
    "  unload Fanout\n"
    "  assert no_residue\n"
    "}\n"
)


def test_exit_py_executes_with_no_coroutine_leak(tmp_path):
    pytest.importorskip(
        "cordis",
        reason="cordis-py runtime not installed (run `sh backends/python/setup.sh`)")
    # Subprocess-isolated for the same reason as item 92's exit test: the
    # emitted lifecycle test drives cordis-py through `asyncio.run`. With
    # `RuntimeWarning` raised as an error, the finding-#27 leak ("coroutine ...
    # was never awaited") fails the run instead of printing a warning.
    for module in ("runtime.py", "confidential.py"):
        (tmp_path / module).write_text(
            (ROOT / "backends" / "python" / module).read_text())
    (tmp_path / "app.py").write_text(
        _emit("python", compile_source(_EXIT, "exit.rvl")))
    (tmp_path / "driver.py").write_text(
        "import warnings\n"
        "warnings.simplefilter('error', RuntimeWarning)\n"
        "import app\n"
        "for _name, _fn in app.REVL_TESTS:\n"
        "    _fn()\n"
        "    print('PASS', _name)\n")
    result = subprocess.run(  # noqa: S603
        [sys.executable, "driver.py"], cwd=tmp_path,
        capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, (
        f"emitted py exit test failed:\n{result.stdout}\n{result.stderr}")
    assert result.stdout.count("PASS") == 1, result.stdout
