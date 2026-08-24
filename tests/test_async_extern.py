"""async/await for extern bodies — roadmap item 80, slices 1+2+3.

Design: docs/design/async-extern.md (the approved spec). This covers the first
landable slice:

  slice 1  parser accepts `async` between the classification and `fn`
  slice 2  frontend validity rules (emission-only, no compensate), the additive
           `"async": True` IR flag (ir_version stays 3), and the v1 coloring
           rule (admitted only inside an `async fn` provide method)
  slice 3  the ts emitter: `async function …: Promise<T>` + awaited call sites

The exit test (the original harness bug) is the ts golden compiling under
`tsc --noEmit`; see backends/typescript/tests/test_async_extern_ts.py.
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
            'extern acquire async fn f(x: Str) -> Str undo drop(x) '
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


def test_module_fn_reaching_async_extern_refused_phase2():
    with pytest.raises(RevlError) as exc:
        compile_source(
            _EXT + 'fn helper(u: Str, b: Str) -> Str { return http_post(u, b) }', 't.rvl')
    assert "cannot carry the async color yet" in str(exc.value)


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
