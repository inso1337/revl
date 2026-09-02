"""sync/async arrow polymorphism — roadmap item 342 (dual of item 92).

Item 92 let a callback declared `(…) -> Async[T]` color its receiving fn async,
so ONE loop can await an async callback. But that loop is then unconditionally
async, so a *sync* caller (a plain `emission fn` whose model crossing is a
blocking call) cannot reuse it — the A1 fence refuses a sync method reaching the
async-colored loop, forcing a duplicated `_sync` twin.

Item 342 (direction b): a sync `(…) -> T` arrow satisfies a `(…) -> Async[T]`
parameter (item 92's `compatible` already types this), and the receiving fn is
*monomorphized at the call site by the caller's color*: an async caller gets the
async loop (item 92, unchanged); a sync caller gets a sync clone of the loop
(no `async`, the callback awaited nowhere). One source loop, two call sites, no
twin.

The exit test is the roadmap's repro: ONE `loop(c: (X) -> Async[Y], x)` called
once from an `async fn` with `y => emit async_op(y)` and once from a sync
`emission fn` with `y => emit sync_op(y)`, executed on py and ts.
"""

import importlib.util
from pathlib import Path

import pytest

from revl.compiler import compile_source
from revl.errors import RevlError


ROOT = Path(__file__).resolve().parents[1]


def _emit(backend: str, ir):
    spec = importlib.util.spec_from_file_location(
        f"revl_{backend}_emit_342", ROOT / "backends" / backend / "emit.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m.emit(ir)


# The repro: ONE loop, an async caller AND a sync `emission fn` caller. -------

_REPRO = (
    "extern emission async fn async_op(x: Str) -> Str\n"
    '  = @py { return x + " (async)" }\n'
    '  = @ts { return x + " (async)" }\n'
    "extern emission fn sync_op(x: Str) -> Str\n"
    '  = @py { return x + " (sync)" }\n'
    '  = @ts { return x + " (sync)" }\n'
    "service Model { emission async fn ask(x: Str) -> Str }\n"
    "service Tool  { emission fn call(x: Str) -> Str }\n"
    "service ARun  { emission async fn go(x: Str) -> Str }\n"
    "service SRun  { emission fn go(x: Str) -> Str }\n"
    # THE shared loop — one source, an async-typed callback.
    "fn loop(c: (Str) -> Async[Str], x: Str) -> Str {\n"
    "  let r = c(x)\n"
    "  return r\n"
    "}\n"
    "component RealModel provides model: Model {\n"
    "  provide model { async fn ask(x) = async_op(x) }\n"
    "}\n"
    "component RealTool provides tool: Tool {\n"
    "  provide tool { fn call(x) = sync_op(x) }\n"
    "}\n"
    # async caller: arrow reaches an async op -> the async loop.
    "component AsyncAgent requires model: Model provides arun: ARun {\n"
    "  provide arun { async fn go(x) = loop(y => emit model.ask(y), x) }\n"
    "}\n"
    # sync caller: arrow reaches a SYNC op, from a plain `emission fn` -> the
    # sync monomorph. No twin loop authored.
    "component SyncAgent requires tool: Tool provides srun: SRun {\n"
    "  provide srun { fn go(x) = loop(y => emit tool.call(y), x) }\n"
    "}\n"
)

# name of the synthesized sync clone (frontend convention)
_SYNC_CLONE = "loop_revl_sync"


def _functions(ir):
    return {f["name"]: f for f in ir.get("functions", [])}


# -- frontend: the repro compiles, with a sync clone -------------------------

def test_repro_compiles_with_both_callers():
    ir = compile_source(_REPRO, "repro.rvl")
    fns = _functions(ir)
    # the async original survives for the async caller
    assert fns["loop"].get("async") is True
    # a sync monomorph was synthesized for the sync caller
    assert _SYNC_CLONE in fns
    clone = fns[_SYNC_CLONE]
    assert not clone.get("async")
    # its callback param lost the Async color: (Str) -> Str, not -> Async[Str]
    ctype = clone["params"][0]["type"]
    assert "Async" not in ctype, ctype


def test_sync_caller_calls_the_sync_clone_not_the_async_loop():
    ir = compile_source(_REPRO, "repro.rvl")
    sync_agent = [c for c in ir["components"] if c["name"] == "SyncAgent"][0]
    names: set = set()

    def walk(n):
        if isinstance(n, dict):
            if n.get("kind") == "fn":
                names.add(n.get("name"))
            for v in n.values():
                walk(v)
        elif isinstance(n, list):
            for v in n:
                walk(v)

    walk(sync_agent)
    assert _SYNC_CLONE in names
    assert "loop" not in names  # the sync caller must NOT reach the async loop


def test_async_caller_still_calls_the_async_loop():
    ir = compile_source(_REPRO, "repro.rvl")
    async_agent = [c for c in ir["components"] if c["name"] == "AsyncAgent"][0]
    names: set = set()

    def walk(n):
        if isinstance(n, dict):
            if n.get("kind") == "fn":
                names.add(n.get("name"))
            for v in n.values():
                walk(v)
        elif isinstance(n, list):
            for v in n:
                walk(v)

    walk(async_agent)
    assert "loop" in names
    assert _SYNC_CLONE not in names


# -- kept refusals -----------------------------------------------------------

def test_sync_caller_passing_an_async_arrow_is_still_refused():
    # A1 stays: a genuinely-suspending arrow from a sync method has no in-flight
    # window. Only a genuinely-sync arrow lifts.
    bad = (
        "extern emission async fn async_op(x: Str) -> Str = @py { return x }\n"
        "service Model { emission async fn ask(x: Str) -> Str }\n"
        "service SRun  { emission fn go(x: Str) -> Str }\n"
        "fn loop(c: (Str) -> Async[Str], x: Str) -> Str { let r = c(x)\n return r }\n"
        "component RealModel provides model: Model {\n"
        "  provide model { async fn ask(x) = async_op(x) }\n"
        "}\n"
        "component Bad requires model: Model provides srun: SRun {\n"
        "  provide srun { fn go(x) = loop(y => emit model.ask(y), x) }\n"
        "}\n"
    )
    with pytest.raises(RevlError, match="A1|async|in-flight"):
        compile_source(bad, "bad.rvl")


def test_item92_mock_coercion_unchanged_async_caller():
    # An async caller passing a sync-bodied arrow keeps item 92's behavior: the
    # loop stays async, the arrow is coerced (no sync monomorph is used here).
    src = (
        "service ARun { emission async fn go(x: Str) -> Str }\n"
        "fn loop(c: (Str) -> Async[Str], x: Str) -> Str { let r = c(x)\n return r }\n"
        "component MockAgent provides arun: ARun {\n"
        '  provide arun { async fn go(x) = loop(y => "mock", x) }\n'
        "}\n"
    )
    ir = compile_source(src, "mock.rvl")
    mock = [c for c in ir["components"] if c["name"] == "MockAgent"][0]
    names: set = set()

    def walk(n):
        if isinstance(n, dict):
            if n.get("kind") == "fn":
                names.add(n.get("name"))
            for v in n.values():
                walk(v)
        elif isinstance(n, list):
            for v in n:
                walk(v)

    walk(mock)
    assert "loop" in names
    assert _SYNC_CLONE not in names


# -- py / ts emit ------------------------------------------------------------

def test_py_emits_both_a_sync_and_an_async_loop():
    py = _emit("python", compile_source(_REPRO, "repro.rvl"))
    assert "async def loop(c, x):" in py
    assert f"def {_SYNC_CLONE}(c, x):" in py
    # the sync clone body does NOT await its callback
    assert "await c(x)" not in py.split(f"def {_SYNC_CLONE}")[1].split("def ")[0]


def test_ts_emits_both_a_sync_and_an_async_loop():
    ts = _emit("typescript", compile_source(_REPRO, "repro.rvl"))
    assert "export async function loop(" in ts
    assert f"export function {_SYNC_CLONE}(" in ts


# -- exit: live execution on py ---------------------------------------------

_EXIT = _REPRO + (
    'lifecycle test "async path awaits the async callback" {\n'
    "  load RealModel\n  load AsyncAgent\n"
    '  let out = call arun.go("hi")\n'
    '  assert out == "hi (async)"\n'
    "  unload AsyncAgent\n  unload RealModel\n  assert no_residue\n"
    "}\n"
    'lifecycle test "sync path runs the lifted sync callback" {\n'
    "  load RealTool\n  load SyncAgent\n"
    '  let out = call srun.go("hi")\n'
    '  assert out == "hi (sync)"\n'
    "  unload SyncAgent\n  unload RealTool\n  assert no_residue\n"
    "}\n"
)


def test_exit_py_executes_both_paths(tmp_path):
    pytest.importorskip(
        "cordis",
        reason="cordis-py runtime not installed (run `sh backends/python/setup.sh`)")
    import subprocess
    import sys

    # `runtime.py` reads its sibling `confidential.py` (the item-256
    # Slice 3 redaction choke point), so the scratch dir needs both.
    for _module in ("runtime.py", "confidential.py"):
        (tmp_path / _module).write_text(
            (ROOT / "backends" / "python" / _module).read_text())
    (tmp_path / "app.py").write_text(_emit("python", compile_source(_EXIT, "exit.rvl")))
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
        f"emitted py exit test failed:\n{result.stdout}\n{result.stderr}")
    assert result.stdout.count("PASS") == 2, result.stdout
