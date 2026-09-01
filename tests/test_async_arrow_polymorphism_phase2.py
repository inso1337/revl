"""Item-342 phase-2 hole — roadmap item 387 (revl-harness H29).

342 hooked its sync->async monomorphization into `_lower_provide` alone. So when
the colour-polymorphic loop was called from a `test` block or a module `fn`
(not a provide method), 342 never fired, item-92 arrow-compat still admitted the
call, and A1 was never applied: py ran to a bare, un-awaited coroutine while ts
REFUSED at emit ("async callable called outside an async context — the frontend
async-coloring check should have refused this (A1)"). py-runs-while-ts-refuses is
worse than either failing.

Item 387 completes 342: the monomorphization now fires at module-`fn` AND
`test`-block call sites too, so a caller whose only async reach is a
genuinely-sync arrow into the loop stays sync on BOTH tiers; and a `test` body
that genuinely reaches an async callable it cannot await is refused (A1) on both
tiers. Either way py and ts agree — no divergence.
"""

import importlib.util
from pathlib import Path

import pytest

from revl.compiler import compile_source
from revl.errors import RevlError


ROOT = Path(__file__).resolve().parents[1]
_SYNC_CLONE = "loop_revl_sync"


def _emit(backend: str, ir):
    spec = importlib.util.spec_from_file_location(
        f"revl_{backend}_emit_387", ROOT / "backends" / backend / "emit.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m.emit(ir)


def _functions(ir):
    return {f["name"]: f for f in ir.get("functions", [])}


# The repro: ONE colour-polymorphic loop reached from a module `fn` AND directly
# from a `test` block, each with a genuinely-sync arrow. -----------------------

_REPRO = (
    "extern emission fn sync_op(x: Str) -> Str\n"
    '  = @py { return x + " (sync)" }\n'
    '  = @ts { return x + " (sync)" }\n'
    "fn loop(c: (Str) -> Async[Str], x: Str) -> Str {\n"
    "  let r = c(x)\n"
    "  return r\n"
    "}\n"
    # a module fn whose only async reach is a genuinely-sync arrow into the loop
    "fn caller(x: Str) -> Str {\n"
    "  let r = loop(y => sync_op(y), x)\n"
    "  return r\n"
    "}\n"
    # a test reaching the loop through that module fn ...
    'test "via module fn" {\n'
    '  let out = caller("hi")\n'
    '  assert out == "hi (sync)"\n'
    "}\n"
    # ... and a test reaching the loop DIRECTLY with a genuinely-sync arrow
    'test "directly" {\n'
    '  let out = loop(y => sync_op(y), "hi")\n'
    '  assert out == "hi (sync)"\n'
    "}\n"
)


def test_repro_compiles_module_fn_stays_sync():
    ir = compile_source(_REPRO, "repro.rvl")
    fns = _functions(ir)
    # the module fn is NOT auto-colored async: its only async reach was a
    # genuinely-sync arrow into the loop, now redirected to the sync clone
    assert not fns["caller"].get("async")
    # the async original survives for any async caller (item 92 unchanged)
    assert fns["loop"].get("async") is True
    # a sync monomorph was synthesized, its callback de-async'd
    assert _SYNC_CLONE in fns
    clone = fns[_SYNC_CLONE]
    assert not clone.get("async")
    assert "Async" not in clone["params"][0]["type"], clone["params"][0]["type"]


def test_module_fn_and_test_call_the_sync_clone_not_the_async_loop():
    ir = compile_source(_REPRO, "repro.rvl")

    def called_names(node, acc):
        if isinstance(node, dict):
            if node.get("kind") == "fn":
                acc.add(node.get("name"))
            callee = node.get("callee")
            if node.get("kind") == "call" and isinstance(callee, dict) \
                    and callee.get("kind") == "var":
                acc.add(callee.get("name"))
            for v in node.values():
                called_names(v, acc)
        elif isinstance(node, list):
            for v in node:
                called_names(v, acc)

    caller_calls: set = set()
    called_names(_functions(ir)["caller"]["body"], caller_calls)
    assert _SYNC_CLONE in caller_calls
    assert "loop" not in caller_calls  # must NOT reach the async loop

    test_calls: set = set()
    for t in ir.get("tests", []):
        called_names(t["body"], test_calls)
    assert _SYNC_CLONE in test_calls
    assert "loop" not in test_calls


# -- py / ts agree: both emit, both run sync ---------------------------------

def test_py_and_ts_both_emit_the_repro():
    ir = compile_source(_REPRO, "repro.rvl")
    py = _emit("python", ir)
    ts = _emit("typescript", ir)
    # the module fn and the sync clone are plain sync functions on both tiers
    assert "def caller(x):" in py and "async def caller" not in py
    assert f"def {_SYNC_CLONE}(c, x):" in py
    assert "export function caller(" in ts
    assert f"export function {_SYNC_CLONE}(" in ts
    # the async original is still async on both tiers (item 92)
    assert "async def loop(c, x):" in py
    assert "export async function loop(" in ts


def test_py_executes_both_sync_paths(tmp_path):
    import subprocess
    import sys

    py = _emit("python", compile_source(_REPRO, "repro.rvl"))
    (tmp_path / "app.py").write_text(py)
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
    # both tests ran to a concrete value (no un-awaited coroutine)
    assert result.stdout.count("PASS") == 2, result.stdout


# -- the genuinely-async cases: BOTH tiers refuse (no divergence) -------------

_ASYNC_EXTERN = (
    "extern emission async fn aop(x: Str) -> Str = @py { return x }\n"
    "  = @ts { return x }\n"
)


def test_test_calling_async_extern_is_refused():
    src = _ASYNC_EXTERN + 'test "t" { let r = aop("hi")\n assert r == "hi" }\n'
    with pytest.raises(RevlError, match="A1|synchronous context|in-flight"):
        compile_source(src, "bad.rvl")


def test_test_calling_genuinely_async_module_fn_is_refused():
    src = (_ASYNC_EXTERN
           + "fn wrap(x: Str) -> Str { let r = aop(x)\n return r }\n"
           + 'test "t" { let r = wrap("hi")\n assert r == "hi" }\n')
    with pytest.raises(RevlError, match="A1|synchronous context|in-flight"):
        compile_source(src, "bad.rvl")


def test_test_reaching_loop_with_a_genuinely_async_arrow_is_refused():
    # a genuinely-async arrow does NOT lift — the caller is genuinely async, and
    # a sync test cannot await it. Refused on both tiers.
    src = (_ASYNC_EXTERN
           + "fn loop(c: (Str) -> Async[Str], x: Str) -> Str { let r = c(x)\n return r }\n"
           + "fn caller(x: Str) -> Str { let r = loop(y => aop(y), x)\n return r }\n"
           + 'test "t" { let r = caller("hi")\n assert r == "hi" }\n')
    with pytest.raises(RevlError, match="A1|synchronous context|in-flight"):
        compile_source(src, "bad.rvl")


# -- kept behavior: an async module fn keeps the async loop (item 92) ---------

def test_genuinely_async_module_fn_keeps_the_async_loop():
    # `caller` also reaches a real async extern, so it is genuinely async; its
    # sync-arrow loop call must stay the async loop (item-92 coercion), NOT be
    # monomorphized to the sync clone.
    src = (_ASYNC_EXTERN
           + "extern emission fn sop(x: Str) -> Str = @py { return x }\n"
           + "  = @ts { return x }\n"
           + "fn loop(c: (Str) -> Async[Str], x: Str) -> Str { let r = c(x)\n return r }\n"
           + "fn caller(x: Str) -> Str {\n"
           + "  let a = aop(x)\n"
           + "  let r = loop(y => sop(y), a)\n"
           + "  return r\n"
           + "}\n"
           + "service ARun { emission async fn go(x: Str) -> Str }\n"
           + "component Agent provides arun: ARun {\n"
           + "  provide arun { async fn go(x) = caller(x) }\n"
           + "}\n")
    ir = compile_source(src, "keep.rvl")
    fns = _functions(ir)
    assert fns["caller"].get("async") is True  # genuinely async
    caller_calls: set = set()

    def walk(n):
        if isinstance(n, dict):
            callee = n.get("callee")
            if n.get("kind") == "call" and isinstance(callee, dict) \
                    and callee.get("kind") == "var":
                caller_calls.add(callee.get("name"))
            for v in n.values():
                walk(v)
        elif isinstance(n, list):
            for v in n:
                walk(v)

    walk(fns["caller"]["body"])
    assert "loop" in caller_calls           # the async loop, coerced
    assert _SYNC_CLONE not in caller_calls   # no sync clone here
