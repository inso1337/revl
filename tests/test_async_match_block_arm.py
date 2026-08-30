"""Roadmap item 361 — an ASYNC provide method may use a match BLOCK arm.

The gap (revl-harness CLI-engine `EngineModel.complete` dogfood): a match
*block* arm (`Some(e) => { let x = …; engine_run(x) }`) was refused inside a
provide-method body ("a block-bodied match arm is not lowerable here; block
arms lower inside a module `fn` body"), while the *expression* arm form
(`Some(e) => engine_run("…" + e)`) already compiled. Module fns reaching an
async extern are colored async (item 90 phase 2, already landed), but the
harness's block arm reads component `config`, so it cannot be lifted into a
module `fn` helper. The fix lowers the block arm *inline* in the component
body as a `do` expression (an async IIFE on ts, an awaited walrus sequence on
py), so the async-coloring fixed point and every A1 refusal are untouched: a
sync method reaching an async callable through a block arm is still refused,
an async method admits it and awaits it.

The exit test mirrors the harness `complete`: an `emission async fn` provide
method whose body is multi-step conditional async logic in a match BLOCK arm
that reads `config`, reaches the async extern, and returns — it COMPILES and
RUNS on py AND ts.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402
from revl.errors import RevlError  # noqa: E402
from revl.test import RUNNERS  # noqa: E402

_ENGINE_RUN = (
    'extern emission async fn engine_run(argv: Str, cwd: Str) -> Str\n'
    '  = @ts { return await Promise.resolve("ran:" + argv + "@" + cwd) }\n'
    '  = @py { return "ran:" + argv + "@" + cwd }\n'
)

# the harness `EngineModel.complete` shape, minimised: a match BLOCK arm that
# binds a `let`, reads component `config`, and calls the async extern in its
# tail; the other arm short-circuits with a plain string (a non-engine
# provider never spawns).
_REPRO = (
    _ENGINE_RUN
    + 'fn engine_by_name(n: Str) -> Opt[Str] {\n'
    + '  return (n == "real") ? Some("argv-" + n) : None\n'
    + '}\n'
    + 'service Engine { emission async fn complete(name: Str) -> Str }\n'
    + 'component EngineModel provides eng: Engine {\n'
    + '  config { cwd: Str = "/work" }\n'
    + '  provide eng {\n'
    + '    async fn complete(name) {\n'
    + '      let raw = match engine_by_name(name) {\n'
    + '        Some(argv) => {\n'
    + '          let full = argv + "!"\n'
    + '          engine_run(full, config.cwd)\n'
    + '        },\n'
    + '        None => "engine-error:not an engine",\n'
    + '      }\n'
    + '      return raw\n'
    + '    }\n'
    + '  }\n'
    + '}\n'
)


def test_async_provide_method_block_arm_compiles():
    """The core gap: the repro now compiles (was refused at lowering)."""
    ir = compile_source(_REPRO, "engine_model.rvl")
    assert ir["ir_version"] == 3


def test_sync_provide_method_block_arm_reaching_async_is_still_refused():
    """The A1 soundness refusal is preserved: a SYNC op whose block arm
    reaches the async extern has no in-flight window — still refused."""
    sync_src = _REPRO.replace(
        'emission async fn complete(name: Str) -> Str',
        'emission fn complete(name: Str) -> Str',
    ).replace('async fn complete(name)', 'fn complete(name)')
    with pytest.raises(RevlError) as exc:
        compile_source(sync_src, "engine_model.rvl")
    msg = str(exc.value)
    assert "declared sync" in msg
    assert "engine_run" in msg
    assert "A1" in msg


def test_block_arm_with_a_loop_is_refused_clearly():
    """A narrowed, still-sound refusal: a provide-method block arm supports
    `let` bindings and a final expression (what both tiers emit as an
    expression); a loop/reassignment is not lowered here yet and is refused
    with a clear message rather than mis-compiled."""
    loop_src = _REPRO.replace(
        '          let full = argv + "!"\n'
        '          engine_run(full, config.cwd)\n',
        '          var full = argv\n'
        '          full = full + "!"\n'
        '          engine_run(full, config.cwd)\n',
    )
    with pytest.raises(RevlError) as exc:
        compile_source(loop_src, "engine_model.rvl")
    assert "block arm" in str(exc.value)


def test_block_arm_ts_emits_awaited_async_iife():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "revl_ts_emit_361", ROOT / "backends" / "typescript" / "emit.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    out = m.emit(compile_source(_REPRO, "engine_model.rvl"))
    # the async extern is awaited inside an async IIFE arm body
    assert "await engine_run(" in out
    assert "async () =>" in out


def test_block_arm_py_emits_awaited_call():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "revl_py_emit_361", ROOT / "backends" / "python" / "emit.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    out = m.emit(compile_source(_REPRO, "engine_model.rvl"))
    assert "await engine_run(" in out


# -- execution: the harness repro RUNS on py and ts -------------------------

_RUN = (
    _REPRO
    + 'lifecycle test "async block arm runs the engine and returns its reply" {\n'
    + '  load EngineModel\n'
    + '  let reply = call eng.complete("real")\n'
    + '  assert reply == "ran:argv-real!@/work"\n'
    + '  unload EngineModel\n'
    + '  assert no_residue\n'
    + '}\n'
    + 'lifecycle test "the non-engine arm short-circuits without spawning" {\n'
    + '  load EngineModel\n'
    + '  let reply = call eng.complete("mock")\n'
    + '  assert reply == "engine-error:not an engine"\n'
    + '  unload EngineModel\n'
    + '  assert no_residue\n'
    + '}\n'
)


@pytest.mark.parametrize("tier", ["py", "ts"])
def test_async_block_arm_executes(tier):
    status, message = RUNNERS[tier](compile_source(_RUN, "engine_model.rvl"))
    if status == "skip":
        pytest.skip(f"{tier}: {message}")
    assert status == "pass", f"{tier} failed: {message}"
