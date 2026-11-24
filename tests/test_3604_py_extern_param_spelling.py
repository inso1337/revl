"""An extern with an inline `@py` body keeps its parameter spelling (#3604).

The body is host text copied verbatim, so it names its parameters itself. The
emitter used to rename a parameter spelled like a builtin it emits bare
(`max` -> `max_`) while the body still read `max`, which is the Python builtin:
`bump(1)` raised `TypeError`. The parameters now keep their spelling, and a
spelling Python cannot bind as a parameter, or one the emitter binds in the same
body, is refused at emit naming the extern, the parameter and the tier. The
`@py ref` thunk path is unchanged.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for p in (ROOT / "src", ROOT / "tests"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from _backend_import import backend_emitter  # noqa: E402
from revl import compile_source  # noqa: E402

EMIT = backend_emitter("python")


def _emitted(source: str) -> str:
    emitted = EMIT.emit(compile_source(source, "extern_param_3604.rvl"))
    if isinstance(emitted, dict):
        return "\n".join(v for v in emitted.values() if isinstance(v, str))
    return str(emitted)


def _call(source: str, fn: str, *args):
    ns: dict = {}
    exec(compile(_emitted(source), "<extern_param_3604>", "exec"), ns)
    return ns[fn](*args)


@pytest.mark.parametrize("param", sorted(EMIT._EMITTED_BUILTINS - {"type"}))  # `type` is a revl keyword
def test_bodied_extern_param_named_like_an_emitted_builtin_runs(param):
    """The issue's `max` and `len` cases, and every other bare-emitted builtin."""
    source = (f"extern pure fn bump({param}: Int) -> Int = @py {{ return {param} + 1 }}\n")
    text = _emitted(source)
    assert f"def bump({param}):" in text
    assert _call(source, "bump", 1) == 2


def test_issue_example_max_emits_and_calls():
    source = "extern pure fn bump(max: Int) -> Int\n= @py {\n      return max + 1\n    }\n"
    assert _call(source, "bump", 1) == 2


def test_other_parameters_of_the_same_extern_keep_their_spelling():
    source = ("extern pure fn span(min: Int, max: Int, len: Int) -> Int\n"
              "= @py { return max - min + len }\n")
    assert _call(source, "span", 1, 5, 10) == 14


def test_call_through_a_revl_fn_runs():
    source = ("extern pure fn bump(max: Int) -> Int = @py { return max + 1 }\n"
              "pub fn f(x: Int) -> Int { return bump(x) }\n")
    assert _call(source, "f", 1) == 2


def test_revl_fn_parameter_named_max_is_still_renamed():
    """Unchanged: a revl body is renamed at its declaration and every use."""
    source = "pub fn bump(max: Int) -> Int { return max + 1 }\n"
    assert "def bump(max_):" in _emitted(source)
    assert _call(source, "bump", 1) == 2


@pytest.mark.parametrize("param", ["from", "class", "lambda", "case", "_"])
def test_keyword_or_soft_keyword_param_is_refused_by_name(param):
    source = f"extern pure fn f({param}: Int) -> Int = @py {{ return 1 }}\n"
    with pytest.raises(EMIT.EmitError) as err:
        _emitted(source)
    message = str(err.value)
    assert "extern `f`" in message
    assert f"parameter {param!r}" in message
    assert "py tier" in message


@pytest.mark.parametrize("param", ["_revl_config", "_revl", "_revl_secret"])
def test_scaffolding_namespace_param_is_refused_by_name(param):
    source = f"extern pure fn f({param}: Int) -> Int = @py {{ return 1 }}\n"
    with pytest.raises(EMIT.EmitError) as err:
        _emitted(source)
    message = str(err.value)
    assert "extern `f`" in message
    assert f"parameter {param!r}" in message
    assert "py tier" in message


def test_config_extern_keeps_a_builtin_named_param_beside_its_config_bind():
    source = ('extern emission fn f(max: Int) -> Int\n'
              '  config { limit: Int = 3 }\n'
              '  = @py { return max + _revl_config["limit"] }\n')
    text = _emitted(source)
    assert "def f(max):" in text
    assert "    _revl_config = _revl_extern_config('f'" in text


def test_async_extern_keeps_a_builtin_named_param():
    source = 'extern emission async fn f(len: Int) -> Int = @py { return len + 1 }\n'
    assert "async def f(len):" in _emitted(source)


@pytest.mark.parametrize("param", ["Map", "Pool", "self"])
def test_host_root_named_param_is_kept(param):
    """Not refused any more: local to the host text, it shadows nothing the
    emitter writes."""
    source = f"extern pure fn f({param}: Int) -> Int = @py {{ return {param} + 1 }}\n"
    assert _call(source, "f", 1) == 2


def test_scaffolding_namespace_fixture_is_refused():
    from revl import compile_files
    fixture = ROOT / "tests" / "fixtures" / "emit_py_refusals" / "extern_param_scaffolding.rvl"
    with pytest.raises(EMIT.EmitError, match="extern `g` parameter '_revl_config'"):
        EMIT.emit(compile_files([str(fixture)]))
