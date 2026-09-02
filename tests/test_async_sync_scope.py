"""Regression: an async `await` crossing must never land in a SYNC emitted
frame on the py tier (roadmap items 263 + 264 — the item-78 "compiles-implies-
runs" class).

Both items are one failure: the emitter produced Python the tier rejects at
import time (`SyntaxError: 'await' outside async function`), which `compile`
could not see because the frontend admitted the program green.

  263  a `match` arm in an async provide-method carries an async emission
       (`Go => emit store.get(...)`). The match binder rode a one-shot lambda,
       so the arm's `await` was trapped in that (sync) lambda. Fix: the binder
       switches to walrus assignments in an async frame, so the `await` lands
       at the enclosing `async def`'s top level (backends/python/emit.py
       `_match_expr`).

  264  a module fn re-passes its async arrow parameter (`h => complete(h)`).
       The module `_expr` had no in-arrow suppression (item 141 only wired it
       for the component emitter), so the await-seed fired inside the arrow and
       emitted `_revl_as_async(lambda h: (await complete(h)))` — an `await` in
       a lambda. Fix (emitter, NOT a checker refusal): suppress the await-seed
       inside an arrow body and thread the async-typed params so the tail call
       renders as a plain coroutine lambda the awaiting call site settles.

The exit tests emit each program to py and assert it now IMPORTS (the exact
failure was at import), and — where the cordis-py runtime is installed — run
the emitted lifecycle tests green with `RuntimeWarning` promoted to an error so
an unawaited coroutine fails the run.
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
        f"revl_{backend}_emit_263", ROOT / "backends" / backend / "emit.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m.emit(ir)


# -- item 263: async emission in a match-arm value position -----------------

_263 = (
    "type Cmd = Go | Halt(Str)\n"
    "service Store { emission async fn get(k: Str) -> Str }\n"
    "service Router { emission async fn route(c: Cmd) -> Str }\n"
    "extern emission async fn backend(k: Str) -> Str\n"
    '  = @py { return k + " -> got" }\n'
    '  = @ts { return k + " -> got" }\n'
    "component RealStore provides store: Store {\n"
    "  provide store { async fn get(k) = backend(k) }\n"
    "}\n"
    "component Facade requires store: Store provides router: Router {\n"
    "  provide router {\n"
    "    async fn route(c) = match c {\n"
    '      Go => emit store.get("go"),\n'
    "      Halt(why) => why,\n"
    "    }\n"
    "  }\n"
    "}\n"
    'lifecycle test "match arm awaits the async emission" {\n'
    "  load RealStore\n  load Facade\n"
    "  let a = call router.route(Go)\n"
    '  assert a == "go -> got"\n'
    '  let b = call router.route(Halt("stopped"))\n'
    '  assert b == "stopped"\n'
    "  unload Facade\n  unload RealStore\n  assert no_residue\n"
    "}\n"
)


def test_263_match_arm_async_emits_importable_py():
    # was `SyntaxError: 'await' outside async function` — the await was trapped
    # in the match binder's lambda.
    py = _emit("python", compile_source(_263, "i263.rvl"))
    compile(py, "<263>", "exec")


def test_263_await_lands_at_the_async_frame_via_walrus():
    py = _emit("python", compile_source(_263, "i263.rvl"))
    # scrutinee bound by walrus, arm await at the async-def top level, and NOT
    # a `lambda match:` wrapper (which would re-trap the await). Item 436 F3
    # moved the bind into the first arm's test, so it reads `(match := c)`
    # wherever that test is, rather than heading a `(<bind>, <chain>)[1]` tuple.
    assert "(match := c)" in py
    assert "(await _revl_ctx.store.get('go'))" in py
    assert "lambda match:" not in py


# -- item 264: async arrow re-passed from an async frame --------------------

_264 = (
    "service Model { emission async fn complete(msgs: Str) -> Str }\n"
    "service Runner { emission async fn run(prompt: Str) -> Str }\n"
    "extern emission async fn remote(msgs: Str) -> Str\n"
    '  = @py { return msgs + " -> reply" }\n'
    '  = @ts { return msgs + " -> reply" }\n'
    "fn g(msgs: Str, cb: (Str) -> Async[Str]) -> Str {\n"
    "  return cb(msgs)\n"
    "}\n"
    "fn agent_loop(prompt: Str, complete: (Str) -> Async[Str]) -> Str {\n"
    "  let t = g(prompt, h => complete(h))\n"
    "  return t\n"
    "}\n"
    "component RealModel provides model: Model {\n"
    "  provide model { async fn complete(msgs) = remote(msgs) }\n"
    "}\n"
    "component Agent requires model: Model provides runner: Runner {\n"
    "  provide runner { async fn run(prompt) = "
    "agent_loop(prompt, msgs => emit model.complete(msgs)) }\n"
    "}\n"
    'lifecycle test "helper re-passes the async arrow" {\n'
    "  load RealModel\n  load Agent\n"
    '  let out = call runner.run("hi")\n'
    '  assert out == "hi -> reply"\n'
    "  unload Agent\n  unload RealModel\n  assert no_residue\n"
    "}\n"
)


def test_264_repassed_async_arrow_is_admitted():
    # the frontend admits it (following the hint's own `x => f(x)` shape); the
    # bug was purely on the py emitter's side.
    ir = compile_source(_264, "i264.rvl")
    assert [f for f in ir["functions"] if f["name"] == "agent_loop"][0]["async"] is True


def test_264_repassed_async_arrow_emits_importable_py():
    # was `SyntaxError: 'await' outside async function` — the await was trapped
    # in `_revl_as_async(lambda h: (await complete(h)))`.
    py = _emit("python", compile_source(_264, "i264.rvl"))
    compile(py, "<264>", "exec")


def test_264_arrow_renders_as_a_plain_coroutine_lambda():
    py = _emit("python", compile_source(_264, "i264.rvl"))
    # the tail call of the async local is a plain lambda returning the
    # coroutine; the awaiting call site (`await g(...)`) settles it. No
    # `_revl_as_async` wrap, and no `await` inside the lambda.
    assert "lambda h: complete(h)" in py
    assert "_revl_as_async(lambda h:" not in py


def test_264_bare_async_arg_is_still_refused_with_the_hint():
    # guard the checker path the fix must NOT loosen: a BARE async value (not an
    # arrow) into an async slot stays a red compile with the wrap hint.
    src = (
        "fn apply_cb(cb: (Str) -> Async[Str]) -> Str { return cb(\"x\") }\n"
        "fn sync_cb(s: Str) -> Str { return s }\n"
        "fn caller() -> Str { return apply_cb(sync_cb) }\n"
    )
    with pytest.raises(RevlError, match="only an arrow may be passed"):
        compile_source(src, "bare.rvl")


# -- live execution on the cordis-py reference runtime ----------------------

def _run_emitted_lifecycle(src: str, filename: str, expected_passes: int, tmp_path):
    pytest.importorskip(
        "cordis",
        reason="cordis-py runtime not installed (run `sh backends/python/setup.sh`)")
    # `runtime.py` reads its sibling `confidential.py` (the item-256
    # Slice 3 redaction choke point), so the scratch dir needs both.
    for _module in ("runtime.py", "confidential.py"):
        (tmp_path / _module).write_text(
            (ROOT / "backends" / "python" / _module).read_text())
    (tmp_path / "app.py").write_text(_emit("python", compile_source(src, filename)))
    (tmp_path / "driver.py").write_text(
        "import warnings\n"
        "warnings.simplefilter('error', RuntimeWarning)\n"
        "import app\n"
        "for _name, _fn in app.REVL_TESTS:\n"
        "    _fn()\n"
        "    print('PASS', _name)\n")
    result = subprocess.run(
        [sys.executable, "driver.py"], cwd=tmp_path,
        capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, (
        f"emitted py lifecycle failed:\n{result.stdout}\n{result.stderr}")
    assert result.stdout.count("PASS") == expected_passes, result.stdout


def test_263_runs_green_on_cordis(tmp_path):
    _run_emitted_lifecycle(_263, "i263.rvl", 1, tmp_path)


def test_264_runs_green_on_cordis(tmp_path):
    _run_emitted_lifecycle(_264, "i264.rvl", 1, tmp_path)
