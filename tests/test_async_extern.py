"""async/await for extern bodies — roadmap item 80 (slices 1-3) + item 90
(phase-2 fn-coloring).

Design: docs/design/async-extern.md (the approved spec).

  slice 1  parser accepts `async` between the classification and `fn`
  slice 2  frontend validity rules (emission-only, no compensate), the additive
           `"async": True` IR flag (ir_version stays 3), and the v1 coloring
           rule (admitted only inside an `async fn` provide method)
  slice 3  the ts emitter: `async function …: Promise<T>` + awaited call sites
  item 90  transitive fn-coloring (design §3, "v2"): a module `fn` reaching an
           async callable — directly or transitively — becomes async-colored
           by a fixed point, gets `"async": True` stamped, and emits an
           `async function`; first-class async values stay refused, not widened.

The exit tests (the original harness bug, then its full agent-loop shape) are
the ts goldens compiling under `tsc --noEmit`; see
backends/typescript/tests/test_async_extern_ts.py.
"""

import pytest

from revl.parser import Parser
from revl.compiler import compile_source
from revl.errors import RevlError


# -- helpers ----------------------------------------------------------------

_EXT = (
    'extern emission async fn http_post(url: Str, body: Str) -> Str\n'
    '  = @ts {{ return await Promise.resolve(url + body) }}\n'
)


def _program(service_op: str, method_decl: str) -> str:
    return (
        _EXT
        + f'service Http {{ {service_op} }}\n'
        + 'component Poster provides http: Http {\n'
        + f'  provide http {{ {method_decl} = http_post(url, body) }}\n'
        + '}\n'
    )


# -- slice 1: parser --------------------------------------------------------

def test_parser_accepts_async_between_classification_and_fn():
    prog = Parser(
        'extern emission async fn f(x: Str) -> Str = @ts { return x }', 't.rvl'
    ).parse()
    assert prog.externs[0].async_ is True


def test_parser_plain_extern_is_not_async():
    prog = Parser(
        'extern emission fn f(x: Str) -> Str = @ts { return x }', 't.rvl'
    ).parse()
    assert prog.externs[0].async_ is False


def test_parser_async_before_classification_is_still_unclassified():
    # the classification stays first and mandatory, so `async` in front of it
    # trips the existing "unclassified extern" diagnostic, untouched.
    with pytest.raises(RevlError) as exc:
        Parser('extern async emission fn f() -> Str = @ts { return 1 }', 't.rvl').parse()
    assert "unclassified extern" in str(exc.value)


# -- slice 2: validity rules + IR flag --------------------------------------

def test_ir_flag_is_additive_and_version_stays_3():
    ir = compile_source(_program('emission async fn post(url: Str, body: Str) -> Str',
                                 'async fn post(url, body)'), 't.rvl')
    ext = [e for e in ir["externs"] if e["name"] == "http_post"][0]
    assert ext.get("async") is True
    assert ir["ir_version"] == 3


def test_plain_extern_has_no_async_key():
    ir = compile_source('extern emission fn f(x: Str) -> Str = @ts { return x }', 't.rvl')
    assert "async" not in ir["externs"][0]


def test_pure_async_extern_refused():
    with pytest.raises(RevlError) as exc:
        compile_source('extern pure async fn f(x: Str) -> Str = @ts { return x }', 't.rvl')
    assert "cannot be `async`" in str(exc.value)


def test_acquire_async_extern_refused():
    with pytest.raises(RevlError) as exc:
        compile_source(
            'extern acquire async fn f(x: Str) -> H undo drop(x) '
            '= @ts { return x }', 't.rvl')
    assert "cannot be `async`" in str(exc.value)


def test_async_extern_with_compensate_refused():
    with pytest.raises(RevlError) as exc:
        compile_source(
            'extern emission async fn f(x: Str) -> Str compensate cleanup(x) '
            '= @ts { return x }\n'
            'extern emission fn cleanup(x: Str) = @ts {}', 't.rvl')
    assert "cannot declare `compensate`" in str(exc.value)


# -- slice 2: v1 coloring ---------------------------------------------------

def test_async_fn_provide_method_admits_the_call():
    ir = compile_source(_program('emission async fn post(url: Str, body: Str) -> Str',
                                 'async fn post(url, body)'), 't.rvl')
    assert ir["ir_version"] == 3  # compiled cleanly


def test_sync_provide_method_reaching_async_extern_refused():
    with pytest.raises(RevlError) as exc:
        compile_source(_program('emission fn post(url: Str, body: Str) -> Str',
                                'fn post(url, body)'), 't.rvl')
    msg = str(exc.value)
    assert "is declared sync, but this implementation reaches async extern `http_post`" in msg
    assert "A1" in msg


def test_module_fn_reaching_async_extern_is_colored_phase2():
    # phase 2 (item 90): a module fn that reaches an async extern is no longer
    # refused — it becomes async-colored and gets `"async": True` stamped on
    # its IR entry (the fixed point in check_and_lower).
    ir = compile_source(
        _EXT + 'fn helper(u: Str, b: Str) -> Str { return http_post(u, b) }', 't.rvl')
    helper = [f for f in ir["functions"] if f["name"] == "helper"][0]
    assert helper.get("async") is True


def test_async_coloring_is_a_transitive_fixpoint():
    # a fn calling a fn calling an async extern colors transitively; a fn that
    # touches nothing async stays sync.
    ir = compile_source(
        _EXT
        + 'fn mid(u: Str, b: Str) -> Str { return http_post(u, b) }\n'
        + 'fn outer(u: Str, b: Str) -> Str { return mid(u, b) }\n'
        + 'fn pure_one(x: Str) -> Str { return x }\n', 't.rvl')
    flags = {f["name"]: f.get("async") for f in ir["functions"]}
    assert flags["mid"] is True
    assert flags["outer"] is True
    assert flags["pure_one"] is None  # untouched by the color


def test_first_class_async_value_in_module_fn_refused():
    # an arrow type carries no color, so passing an async callable as a value
    # is a compile error, not a coloring (async-extern.md §3, "refused, not
    # widened").
    with pytest.raises(RevlError) as exc:
        compile_source(
            _EXT
            + 'fn apply(f: (Str, Str) -> Str, u: Str, b: Str) -> Str { return f(u, b) }\n'
            + 'fn run(u: Str, b: Str) -> Str { return apply(http_post, u, b) }\n', 't.rvl')
    msg = str(exc.value)
    assert "has no arrow type" in msg
    assert "A1" in msg


def test_sync_provide_method_reaching_a_colored_fn_refused():
    # the provide-method admission tests membership in the colored set, not
    # just direct extern calls: a sync method calling a colored helper fn is
    # refused too.
    with pytest.raises(RevlError) as exc:
        compile_source(
            _EXT
            + 'fn helper(u: Str, b: Str) -> Str { return http_post(u, b) }\n'
            + 'service Http { emission fn post(url: Str, body: Str) -> Str }\n'
            + 'component Poster provides http: Http {\n'
            + '  provide http { fn post(url, body) = helper(url, body) }\n'
            + '}\n', 't.rvl')
    msg = str(exc.value)
    assert "declared sync, but this implementation reaches async function `helper`" in msg
    assert "A1" in msg


def test_async_fn_provide_method_may_call_a_colored_fn():
    # the mirror of the refusal above: an async provide method admits a call
    # to a colored helper fn.
    ir = compile_source(
        _EXT
        + 'fn helper(u: Str, b: Str) -> Str { return http_post(u, b) }\n'
        + 'service Http { emission async fn post(url: Str, body: Str) -> Str }\n'
        + 'component Poster provides http: Http {\n'
        + '  provide http { async fn post(url, body) = helper(url, body) }\n'
        + '}\n', 't.rvl')
    assert ir["ir_version"] == 3  # compiled cleanly


# -- slice 3: ts emitter ----------------------------------------------------

def _emit_ts(ir):
    import importlib.util
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "revl_ts_emit_async", root / "backends" / "typescript" / "emit.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m.emit(ir)


def test_ts_emits_async_signature_and_awaited_call():
    ir = compile_source(_program('emission async fn post(url: Str, body: Str) -> Str',
                                 'async fn post(url, body)'), 't.rvl')
    out = _emit_ts(ir)
    assert "export async function http_post(url: string, body: string): Promise<string> {" in out
    # the author never spells await; the emitter inserts it at the admitted site
    assert "return (await http_post(url, body))" in out


def test_ts_emits_colored_fn_as_async_with_awaited_transitive_call():
    # item 90: a colored module fn emits `async function …: Promise<T>`, and a
    # colored fn calling another colored fn awaits that transitive call site.
    ir = compile_source(
        _EXT
        + 'fn mid(u: Str, b: Str) -> Str { return http_post(u, b) }\n'
        + 'fn outer(u: Str, b: Str) -> Str { return mid(u, b) }\n', 't.rvl')
    out = _emit_ts(ir)
    assert "export async function mid(u: string, b: string): Promise<string> {" in out
    assert "export async function outer(u: string, b: string): Promise<string> {" in out
    assert "return (await http_post(u, b))" in out
    assert "return (await mid(u, b))" in out


# -- slice 4: py emit (roadmap item 115, async-extern.md §8) -----------------
#
# Closes harness finding #32: before item 115 the py tier erased EVERY extern —
# async or not — to a blocking `def`, so an async extern whose @py body wanted
# to `await` a host operation (dispose a fiber, settle a promise) was a syntax
# error, and its async caller leaked the coroutine by never awaiting. py now
# emits an async extern as `async def` and awaits it at every admitted site,
# mirroring the ts slice — a component CAN await a host operation on py.

def _emit_py(ir):
    import importlib.util
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "revl_py_emit_async", root / "backends" / "python" / "emit.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m.emit(ir)


# an async extern whose @py body genuinely `await`s a host coroutine — the
# shape that was a `SyntaxError` when erased to a blocking `def`.
_EXT_PY = (
    'extern emission async fn host_dispose(handle: Str) -> Str\n'
    '  = @py { return (await __import__("asyncio").sleep(0, handle + " gone")) }\n'
)


def test_py_emits_async_extern_as_async_def_and_awaited_call():
    ir = compile_source(
        _EXT_PY
        + 'service Loader { emission async fn unload(handle: Str) -> Str }\n'
        + 'component Plugin provides loader: Loader {\n'
        + '  provide loader { async fn unload(handle) = host_dispose(handle) }\n'
        + '}\n', 't.rvl')
    out = _emit_py(ir)
    # the extern is an `async def`, so its verbatim `await` body is legal python
    assert "async def host_dispose(handle):" in out
    # the async provide method awaits the async extern — no coroutine leak
    assert "return (await host_dispose(handle))" in out
    # and the whole module is syntactically valid python (the erased `def`
    # holding an `await` used to be a SyntaxError here)
    compile(out, "<emitted>", "exec")


def test_py_non_async_extern_stays_a_blocking_def_and_is_not_awaited():
    ir = compile_source(
        'extern emission fn tag(x: Str) -> Str = @py { return x + "!" }\n'
        + 'service Tagger { emission async fn go(x: Str) -> Str }\n'
        + 'component Tag provides tagger: Tagger {\n'
        + '  provide tagger { async fn go(x) = tag(x) }\n'
        + '}\n', 't.rvl')
    out = _emit_py(ir)
    # a NON-async extern is unchanged: a blocking `def`, never awaited
    assert "def tag(x):" in out
    assert "async def tag(" not in out
    assert "return tag(x)" in out
    assert "await tag(" not in out


def test_py_colored_module_fn_awaits_the_async_extern():
    # a module fn reaching the async extern is phase-2 colored -> `async def`,
    # and awaits the extern at the admitted call site (mirrors the ts item-90
    # transitive-await test above).
    ir = compile_source(
        _EXT_PY
        + 'fn mid(h: Str) -> Str { return host_dispose(h) }\n', 't.rvl')
    out = _emit_py(ir)
    assert "async def mid(h):" in out
    assert "return (await host_dispose(h))" in out


def test_exit_py_async_extern_await_runs_with_no_coroutine_leak(tmp_path):
    # exit test for finding #32: an async extern whose @py body awaits a real
    # host coroutine, awaited by its async caller, executed on the cordis-py
    # reference runtime. RuntimeWarning-as-error turns any unawaited coroutine
    # ("... was never awaited") into a failure — the leak the fix closes.
    pytest.importorskip(
        "cordis",
        reason="cordis-py runtime not installed (run `sh backends/python/setup.sh`)")
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    exit_src = (
        _EXT_PY
        + 'service Loader { emission async fn unload(handle: Str) -> Str }\n'
        + 'component Plugin provides loader: Loader {\n'
        + '  provide loader { async fn unload(handle) = host_dispose(handle) }\n'
        + '}\n'
        + 'lifecycle test "async extern awaited in unload" {\n'
        + '  load Plugin\n'
        + '  let out = call loader.unload("fiber-7")\n'
        + '  assert out == "fiber-7 gone"\n'
        + '  unload Plugin\n  assert no_residue\n'
        + '}\n'
    )
    # `runtime.py` reads its sibling `confidential.py` (the item-256
    # Slice 3 redaction choke point), so the scratch dir needs both.
    for _module in ("runtime.py", "confidential.py"):
        (tmp_path / _module).write_text(
            (root / "backends" / "python" / _module).read_text())
    (tmp_path / "app.py").write_text(_emit_py(compile_source(exit_src, "exit.rvl")))
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
        f"emitted py async-extern exit test failed:\n{result.stdout}\n{result.stderr}")
    assert result.stdout.count("PASS") == 1, result.stdout
